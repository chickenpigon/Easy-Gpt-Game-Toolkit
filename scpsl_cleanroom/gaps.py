"""Spec gap list (CSV) -> measurement sessions -> results written back into the list.

Input CSV columns: id, category, subcategory, item, unit, status, current_value,
current_source, fill_method (wiki|play|asset|user), priority, notes.

``plan``  creates, per play session: a fill-in sheet (CSV), a checklist (MD) and a
          task file for tools/annotator.html; user rows become a questionnaire YAML.
``apply`` reads everything recorded (sheets, annotator exports, notes) and writes
          the CSV back with measured_* columns plus a comparison report.
"""

from __future__ import annotations

import csv
import json
import math
import re
import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from . import fit, svgplot, telemetry
from .measure import percentile

SHEET_COLUMNS = ["id", "item", "unit", "how", "variant", "value", "before", "after", "time_s",
                 "distance_m", "cells", "lateral_m", "x", "y", "note"]
READONLY = {"item", "unit", "how"}
CAL_CELL = "CAL_CELL"  # sheet row id holding the measured corridor cell length (m)

# kind -> (what to fill, trials)
KINDS = {
    "time": ("annotator s/e 키로 시작·끝 프레임, 또는 time_s", 5),
    "distance": ("distance_m 또는 cells(복도 칸 수)", 3),
    "speed": ("distance_m(또는 cells) + time_s, 또는 value", 3),
    "angle": ("annotator 각도 측정(클릭 2번), 또는 lateral_m + distance_m", 5),
    "color": ("annotator 색 추출(클릭), 또는 value=#RRGGBB", 3),
    "amount": ("before/after(RA로 읽은 값), 또는 value", 5),
    "rate": ("before/after + time_s, 또는 value", 3),
    "curve": ("x, y 쌍 (예: 거리 m, 피해 HP) 최소 8개 x", 10),
    "ratio": ("value (배율)", 3),
    "observation": ("note에 관찰 결과를 문장으로", 1),
}

VARIANT_SPLIT = re.compile(r"\b(\w+)\s+vs\s+(\w+)\b|\b(\w+)/(\w+)\b")


def kind_of(row: dict) -> str:
    unit = row["unit"].strip()
    item = row["item"].lower()
    if unit in ("curve", "HP per m/s", "m/s vs HP", "HP/m") or "curve" in item and unit not in ("s",):
        return "curve"
    if unit == "s":
        return "time"
    if unit in ("m", "m/stud"):
        return "distance"
    if unit == "m/s":
        return "speed"
    if unit == "deg":
        return "angle"
    if unit == "rgb":
        return "color"
    if unit in ("HP", "AHP", "AP", "fraction", "count", "rounds"):
        return "amount"
    if unit.endswith("/s"):
        return "rate"
    if unit == "x":
        return "ratio"
    return "observation"


def variants_of(row: dict) -> list[str]:
    """``reload time empty vs tactical`` -> [empty, tactical]; ``open/close time`` -> [open, close]."""
    item = row["item"]
    if kind_of(row) in ("curve", "observation"):
        return [""]
    m = re.search(r"(\w+)\s+vs\s+(\w+)", item)
    if m:
        return [m.group(1), m.group(2)]
    m = re.search(r"\b([a-z]+)/([a-z]+)\b", item)
    if m and kind_of(row) in ("time", "distance", "speed", "angle", "amount", "rate"):
        return [m.group(1), m.group(2)]
    return [""]


def curve_axes(row: dict) -> str:
    """Which x/y to record for a curve row."""
    unit, item = row["unit"], row["item"].lower()
    m = re.match(r"(.+?)\s+(?:vs|per)\s+(.+)", unit)
    if m:
        return f"x={m.group(2)}, y={m.group(1)}"
    if unit == "HP/m":
        return "x=낙하 높이 m, y=피해 HP"
    if "falloff" in item:
        return "x=거리 m, y=피해 HP (같은 부위)"
    if "stamina" in item:
        return "x=회복 시작 후 시간 s, y=스태미나 0~1"
    return "x=입력값, y=결과값"


def how_to(row: dict) -> str:
    k = kind_of(row)
    return f"{curve_axes(row)} 쌍, 최소 8개 x" if k == "curve" else KINDS[k][0]


def load_gaps(path: str | Path) -> list[dict]:
    with open(path, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def label(row: dict) -> str:
    sub, item = row["subcategory"], row["item"]
    return item if sub == item else f"{sub} / {item}"


def parse_plan_sessions(path: str | Path) -> list[dict]:
    """Sessions from a plan markdown: ``## 세션 N — title``, a ``준비:`` line and table rows with ids."""
    sessions, cur = [], None
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        m = re.match(r"##\s+세션\s*(\d+)\s*[—-]\s*(.+)", line)
        if m:
            cur = {"no": int(m.group(1)), "title": m.group(2).strip(), "prep": "", "ids": []}
            sessions.append(cur)
            continue
        if line.startswith("## "):
            cur = None
            continue
        if cur is None:
            continue
        if "준비:" in line:
            cur["prep"] = line.split("준비:", 1)[1].strip()
        m = re.match(r"\|\s*(G\d{4})\s*\|", line)
        if m:
            cur["ids"].append(m.group(1))
    return sessions


def default_sessions(rows: list[dict]) -> list[dict]:
    by_cat = defaultdict(list)
    for r in rows:
        if r["fill_method"] == "play":
            by_cat[r["category"]].append(r["id"])
    return [{"no": i + 1, "title": cat, "prep": "", "ids": ids} for i, (cat, ids) in enumerate(sorted(by_cat.items()))]


# --- plan ---------------------------------------------------------------------------

def plan(gaps_csv, project_dir, plan_md=None, priorities=("P1", "P2", "P3")) -> dict:
    rows = load_gaps(gaps_csv)
    by_id = {r["id"]: r for r in rows}
    sessions = parse_plan_sessions(plan_md) if plan_md else default_sessions(rows)
    planned = {i for s in sessions for i in s["ids"]}
    leftovers = [r["id"] for r in rows if r["fill_method"] == "play" and r["id"] not in planned]
    if leftovers:
        sessions.append({"no": len(sessions) + 1, "title": "기타 (계획에 없던 play 행)", "prep": "", "ids": leftovers})

    mdir = Path(project_dir) / "measurements"
    ndir = Path(project_dir) / "notes"
    sdir = Path(project_dir) / "sessions"
    for d in (mdir, ndir, sdir):
        d.mkdir(parents=True, exist_ok=True)
    written = []
    for s in sessions:
        ids = [i for i in s["ids"] if i in by_id and by_id[i]["priority"] in priorities]
        if not ids:
            continue
        stem = f"session_{s['no']:02d}"
        sheet = mdir / f"{stem}.csv"
        if not sheet.exists():  # never overwrite recorded measurements
            _write_sheet(sheet, [by_id[i] for i in ids], with_calibration=any(
                kind_of(by_id[i]) in ("distance", "speed") for i in ids))
            written.append(sheet)
        tasks = mdir / f"{stem}.tasks.json"
        tasks.write_text(json.dumps(_tasks(s, [by_id[i] for i in ids]), ensure_ascii=False, indent=1), encoding="utf-8")
        written.append(tasks)
        md = mdir / f"{stem}.md"
        md.write_text(_session_md(s, stem, [by_id[i] for i in ids]), encoding="utf-8")
        written.append(md)

    obs = [r for s in sessions for i in s["ids"] if (r := by_id.get(i)) and kind_of(r) == "observation"]
    users = [r for r in rows if r["fill_method"] == "user"]
    for name, items, title in (("user_questions.yaml", users, "기억으로 답할 질문 (fill_method=user)"),
                               ("observations.yaml", obs, "플레이 중 관찰로 답할 규칙 (play 중 문장형)")):
        path = ndir / name
        if not path.exists():
            path.write_text(_questionnaire(items, title), encoding="utf-8")
            written.append(path)
    (mdir / "README.md").write_text(_readme(sessions, by_id), encoding="utf-8")
    written.append(mdir / "README.md")
    return {"sessions": len(sessions), "files": [str(w) for w in written]}


def _write_sheet(path: Path, rows: list[dict], with_calibration: bool):
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, SHEET_COLUMNS)
        w.writeheader()
        if with_calibration:
            for _ in range(3):
                w.writerow({"id": CAL_CELL, "item": "복도 한 칸 길이 (cells 환산용)", "unit": "m",
                            "how": "줄자로 1칸 측정 → value"})
        for r in rows:
            k = kind_of(r)
            trials = KINDS[k][1]
            for v in variants_of(r):
                for _ in range(trials):
                    w.writerow({"id": r["id"], "item": label(r), "unit": r["unit"], "how": how_to(r), "variant": v})


def _tasks(session: dict, rows: list[dict]) -> dict:
    return {
        "session": session["no"], "title": session["title"],
        "tasks": [{"id": r["id"], "label": label(r), "unit": r["unit"], "kind": kind_of(r),
                   "variants": [v for v in variants_of(r) if v], "candidate": r["current_value"]}
                  for r in rows],
    }


def _session_md(s, stem, rows) -> str:
    lines = [f"# 세션 {s['no']} — {s['title']}", ""]
    if s["prep"]:
        lines += [f"준비: {s['prep']}", ""]
    lines += [f"- 기입 시트: `{stem}.csv` (엑셀/구글시트로 열어 빈 칸만 채우기)",
              f"- 영상 작업: `tools/annotator.html` → '과제 불러오기'로 `{stem}.tasks.json`",
              f"- 어노테이터 저장 파일은 `sessions/{stem}.annotations.jsonl` 로", "",
              "| | id | 항목 | 단위 | 방법 | 변형 | 현재 후보 |", "|---|---|---|---|---|---|---|"]
    for r in rows:
        vs = ", ".join(v for v in variants_of(r) if v)
        lines.append(f"| [ ] | {r['id']} | {label(r)} | {r['unit']} | {how_to(r)} | {vs} | "
                     f"{r['current_value'].replace('|', '/')} |")
    return "\n".join(lines) + "\n"


def _questionnaire(rows, title) -> str:
    out = [f"# {title}", "# answer 칸만 채우세요. 비워 두면 미답으로 남습니다. evidence는 선택.", ""]
    for r in rows:
        out.append(f"{r['id']}:  # {label(r)} ({r['unit']}) — {r['category']}")
        cand = r["current_value"].replace('"', "'")
        out.append(f'  candidate: "{cand}"')
        if r.get("notes"):
            out.append(f'  context: "{r["notes"].replace(chr(34), chr(39))}"')
        out.append('  answer: ""')
        out.append('  evidence: ""')
        out.append("")
    return "\n".join(out)


def _readme(sessions, by_id) -> str:
    lines = ["# 측정 키트", "",
             "1. 세션별 `session_NN.md` 체크리스트를 보고 녹화/측정",
             "2. 숫자는 `session_NN.csv` 에 시도마다 한 줄씩 (빈 줄 그대로 두면 무시)",
             "3. 영상 프레임 측정은 annotator에서 과제 파일을 불러와 s(시작)/e(끝), 색·각도는 클릭",
             "4. 문장 답은 `../notes/*.yaml`",
             "5. `python -m scpsl_cleanroom gaps apply <gaps.csv> --project <project>` 로 CSV에 반영", "",
             "| 세션 | 제목 | 행 | 시간 | 거리·속도 | 각도 | 색 | 양·비율 | 곡선 | 관찰 |", "|---|---|---|---|---|---|---|---|---|---|"]
    for s in sessions:
        c = Counter(kind_of(by_id[i]) for i in s["ids"] if i in by_id)
        lines.append(f"| {s['no']} | {s['title']} | {sum(c.values())} | {c['time']} | {c['distance'] + c['speed']} | "
                     f"{c['angle']} | {c['color']} | {c['amount'] + c['rate'] + c['ratio']} | {c['curve']} | {c['observation']} |")
    return "\n".join(lines) + "\n"


# --- collect results -------------------------------------------------------------------

@dataclass
class Result:
    id: str
    kind: str
    values: dict = field(default_factory=lambda: defaultdict(list))  # variant -> [float]
    colors: dict = field(default_factory=lambda: defaultdict(list))  # variant -> [(r, g, b)]
    pairs: list = field(default_factory=list)
    texts: list = field(default_factory=list)
    sources: set = field(default_factory=set)


def _num(s) -> float | None:
    if s is None:
        return None
    s = str(s).strip().replace(",", ".")
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _hex(s) -> tuple | None:
    m = re.match(r"#?([0-9a-fA-F]{6})$", str(s).strip())
    if not m:
        return None
    h = m.group(1)
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def collect(project_dir, rows: list[dict]) -> dict[str, Result]:
    by_id = {r["id"]: r for r in rows}
    res: dict[str, Result] = {}

    def get(gid):
        if gid not in res:
            res[gid] = Result(gid, kind_of(by_id[gid]))
        return res[gid]

    mdir = Path(project_dir) / "measurements"
    for sheet in sorted(mdir.glob("*.csv")) if mdir.exists() else []:
        with open(sheet, encoding="utf-8-sig", newline="") as f:
            lines = list(csv.DictReader(f))
        cells = [v for ln in lines if ln.get("id") == CAL_CELL and (v := _num(ln.get("value"))) is not None]
        cell_m = statistics.median(cells) if cells else None
        for ln in lines:
            gid = (ln.get("id") or "").strip()
            if gid not in by_id:
                continue
            r = get(gid)
            v = _row_value(ln, by_id[gid], r.kind, cell_m)
            var = (ln.get("variant") or "").strip()
            if r.kind == "curve":
                x, y = _num(ln.get("x")), _num(ln.get("y"))
                if x is not None and y is not None:
                    r.pairs.append((x, y))
                    r.sources.add(sheet.name)
            elif r.kind == "color":
                c = _hex(ln.get("value") or "")
                if c:
                    r.colors[var].append(c)
                    r.sources.add(sheet.name)
            elif v is not None:
                r.values[var].append(v)
                r.sources.add(sheet.name)
            note = (ln.get("note") or "").strip()
            if note and r.kind == "observation":
                r.texts.append(note)
                r.sources.add(sheet.name)

    sdir = Path(project_dir) / "sessions"
    for f in sorted(sdir.rglob("*.jsonl")) if sdir.exists() else []:
        samples = telemetry.load(f)
        opened: dict[tuple, float] = {}
        for s in sorted(samples, key=lambda s: float(s["t"])):
            ev = str(s.get("event", ""))
            gid = s.get("gap")
            m = re.match(r"(G\d{4})_(start|end)$", ev)
            if m and m.group(1) in by_id:
                key = (m.group(1), str(s.get("variant", s.get("tag", ""))))
                if m.group(2) == "start":
                    opened[key] = float(s["t"])
                elif key in opened:
                    r = get(m.group(1))
                    r.values[key[1]].append(round(float(s["t"]) - opened.pop(key), 4))
                    r.sources.add(f.name)
            elif ev == "measure" and gid in by_id:
                r = get(gid)
                var = str(s.get("variant", ""))
                if "rgb" in s:
                    r.colors[var].append(tuple(int(c) for c in s["rgb"][:3]))
                elif "deg" in s:
                    r.values[var].append(float(s["deg"]))
                elif "value" in s:
                    r.values[var].append(float(s["value"]))
                r.sources.add(f.name)

    ndir = Path(project_dir) / "notes"
    for f in sorted(ndir.glob("*.y*ml")) if ndir.exists() else []:
        data = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
        for gid, d in data.items():
            if gid in by_id and isinstance(d, dict) and str(d.get("answer", "")).strip():
                r = get(gid)
                r.texts.append(str(d["answer"]).strip() + (f" [{d['evidence']}]" if d.get("evidence") else ""))
                r.sources.add(f.name)
    return res


def _row_value(ln, row, kind, cell_m):
    v = _num(ln.get("value"))
    if v is not None:
        return v
    before, after, t = _num(ln.get("before")), _num(ln.get("after")), _num(ln.get("time_s"))
    dist = _num(ln.get("distance_m"))
    cells = _num(ln.get("cells"))
    if dist is None and cells is not None and cell_m:
        dist = cells * cell_m
    if kind == "time":
        return t
    if kind == "distance":
        return dist
    if kind == "speed":
        return dist / t if dist is not None and t else None
    if kind == "angle":
        lat = _num(ln.get("lateral_m"))
        return math.degrees(math.atan2(lat, dist)) if lat is not None and dist else None
    if kind in ("amount", "rate"):
        if before is None or after is None:
            return None
        delta = after - before
        if kind == "rate" or row["unit"].endswith("/s"):
            return delta / t if t else None
        return abs(delta)
    return None


def summarize_result(r: Result, plots_dir: Path | None = None) -> dict:
    out = {"id": r.id, "kind": r.kind, "sources": sorted(r.sources)}
    if r.kind == "curve":
        if len(r.pairs) < 3:
            return {**out, "status": "partial" if r.pairs else "empty", "n": len(r.pairs)}
        fits = fit.discover([p[0] for p in r.pairs], [p[1] for p in r.pairs])
        best = fits[0]
        if plots_dir is not None:
            plots_dir.mkdir(parents=True, exist_ok=True)
            (plots_dir / f"{r.id}.svg").write_text(svgplot.scatter(r.pairs, fits, "x", "y", r.id), encoding="utf-8")
        return {**out, "status": "measured" if len(r.pairs) >= 8 and best.r2 >= 0.95 else "partial",
                "n": len(r.pairs), "value": best.equation(), "r2": round(best.r2, 4),
                "alternatives": [f.equation() for f in fits[1:]]}
    if r.kind == "color":
        parts, n = [], 0
        for var, cs in sorted(r.colors.items()):
            med = tuple(int(statistics.median(c[i] for c in cs)) for i in range(3))
            parts.append((f"{var}=" if var else "") + "#%02X%02X%02X" % med)
            n += len(cs)
        if not parts:
            return {**out, "status": "empty", "n": 0}
        return {**out, "status": "measured" if n >= 2 else "partial", "n": n, "value": "; ".join(parts)}
    if r.kind == "observation":
        if not r.texts:
            return {**out, "status": "empty", "n": 0}
        return {**out, "status": "answered", "n": len(r.texts), "value": " | ".join(r.texts)}
    parts, n, spreads = [], 0, []
    need = KINDS[r.kind][1]
    for var, vals in sorted(r.values.items()):
        if not vals:
            continue
        med = statistics.median(vals)
        parts.append((f"{var}=" if var else "") + f"{round(med, 4):g}")
        spreads.append((f"{var}=" if var else "") + f"{round(percentile(vals, 10), 4):g}–{round(percentile(vals, 90), 4):g}")
        n += len(vals)
    if r.texts:
        parts.append(" | ".join(r.texts))
    if not parts:
        return {**out, "status": "empty", "n": 0}
    status = "measured" if min((len(v) for v in r.values.values() if v), default=0) >= min(need, 3) or r.texts else "partial"
    return {**out, "status": status, "n": n, "value": "; ".join(parts), "spread": "; ".join(spreads)}


def _first_number(s: str) -> float | None:
    m = re.search(r"-?\d+(?:\.\d+)?", s or "")
    return float(m.group()) if m else None


def apply(gaps_csv, project_dir, out_csv, report_md=None) -> dict:
    rows = load_gaps(gaps_csv)
    plots = Path(project_dir) / "spec" / "gap_plots"
    results = {gid: summarize_result(r, plots) for gid, r in collect(project_dir, rows).items()}
    extra = ["measured_status", "measured_value", "measured_spread", "measured_n", "measured_source"]
    fields = list(rows[0].keys()) + [c for c in extra if c not in rows[0]]
    counts = Counter()
    diffs = []
    with open(out_csv, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fields)
        w.writeheader()
        for row in rows:
            r = results.get(row["id"])
            if r and r["status"] != "empty":
                src = "user" if row["fill_method"] == "user" else "observed"
                row.update({"measured_status": r["status"], "measured_value": r.get("value", ""),
                            "measured_spread": r.get("spread", ""), "measured_n": r.get("n", ""),
                            "measured_source": f"{src}:{','.join(r['sources'])}"})
                counts[r["status"]] += 1
                cand, got = _first_number(row["current_value"]), _first_number(str(r.get("value", "")))
                if cand and got is not None and r["kind"] not in ("color", "observation", "curve"):
                    diffs.append((row, r, cand, got, (got - cand) / cand * 100))
            w.writerow(row)
    if report_md:
        Path(report_md).parent.mkdir(parents=True, exist_ok=True)
        Path(report_md).write_text(_report(rows, results, diffs), encoding="utf-8")
    return {"counts": dict(counts), "results": len(results)}


def _report(rows, results, diffs) -> str:
    todo = [r for r in rows if r["fill_method"] in ("play", "user")]
    done = [r for r in todo if results.get(r["id"], {}).get("status") in ("measured", "answered")]
    part = [r for r in todo if results.get(r["id"], {}).get("status") == "partial"]
    lines = ["# 측정 반영 결과", "",
             f"play+user {len(todo)}행 중 확정 {len(done)} · 부분 {len(part)} · 남음 {len(todo) - len(done) - len(part)}", "",
             "## 후보값과 크게 다른 측정 (|차이| ≥ 10%)", "",
             "| id | 항목 | 후보 | 측정 | 차이 |", "|---|---|---|---|---|"]
    for row, r, cand, got, pct in sorted(diffs, key=lambda d: -abs(d[4])):
        if abs(pct) >= 10:
            lines.append(f"| {row['id']} | {label(row)} | {row['current_value']} | {r['value']} | {pct:+.0f}% |")
    lines += ["", "## 측정값 전체", "", "| id | 항목 | 단위 | 상태 | 측정 | 범위 | n | 후보 |", "|---|---|---|---|---|---|---|---|"]
    for row in todo:
        r = results.get(row["id"])
        if not r or r["status"] == "empty":
            continue
        val = str(r.get("value", "")).replace("|", "/")
        lines.append(f"| {row['id']} | {label(row)} | {row['unit']} | {r['status']} | {val} | {r.get('spread', '')} | "
                     f"{r.get('n', '')} | {row['current_value'].replace('|', '/')} |")
    lines += ["", "## 아직 남은 항목", ""]
    for row in todo:
        if results.get(row["id"], {}).get("status") in (None, "empty"):
            lines.append(f"- {row['id']} {label(row)} ({row['unit']}, {row['priority']})")
    return "\n".join(lines) + "\n"


def status(gaps_csv, project_dir) -> str:
    rows = load_gaps(gaps_csv)
    results = {gid: summarize_result(r) for gid, r in collect(project_dir, rows).items()}
    mdir = Path(project_dir) / "measurements"
    lines = []
    for tasks in sorted(mdir.glob("*.tasks.json")):
        t = json.loads(tasks.read_text(encoding="utf-8"))
        ids = [x["id"] for x in t["tasks"]]
        c = Counter(results.get(i, {}).get("status", "empty") for i in ids)
        lines.append(f"세션 {t['session']:>2} {t['title']}: {c['measured'] + c['answered']}/{len(ids)} 확정, {c['partial']} 부분")
    users = [r["id"] for r in rows if r["fill_method"] == "user"]
    lines.append(f"사용자 질문: {sum(1 for i in users if results.get(i, {}).get('status') == 'answered')}/{len(users)} 답변")
    return "\n".join(lines)
