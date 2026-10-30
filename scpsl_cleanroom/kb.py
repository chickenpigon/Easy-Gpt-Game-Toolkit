"""Knowledge base: entity catalog + evidence -> detailed spec documents.

Project layout (``kb init`` creates it):

    project/
      catalog/        entity + question definitions (YAML, editable)
      sessions/       evidence: telemetry, annotations, sound events (*.jsonl)
      notes/          manual answers (YAML): {entity_id: {question_id: {answer, evidence}}}
      assets_index.json   built by ``kb index`` from licensed assets
      spec/           generated: index.md, PLAN.md, DISCOVERED.md, entities/*.md, plots/, spec.json, GameSpec.luau

Every question is re-analysed from all evidence on every build, so recording
more play simply sharpens the spec.
"""

from __future__ import annotations

import json
import re
import shutil
from collections import Counter, defaultdict
from pathlib import Path

import yaml

from . import derive, fit, guard, media, svgplot, telemetry, timeline, ui_layout, unity_scene
from .measure import Measurement, mode_bin, summarize
from .roblox import to_luau

PACKAGE_CATALOG = Path(__file__).parent / "catalog"
STATUS_ICON = {"answered": "✅", "partial": "🟡", "no_data": "⬜", "manual": "📝", "todo": "⬜"}
SELECTOR_KEYS = ("id", "role", "tag")
SESSION_SPACING = 100_000.0  # seconds between sessions on the merged time axis
MAX_INTERVAL = 600.0
EVENT_KEYS = ("event", "trigger", "from", "to", "reset", "start_event")


# --- project files ----------------------------------------------------------------

class Project:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.catalog_dir = self.root / "catalog"
        self.sessions_dir = self.root / "sessions"
        self.notes_dir = self.root / "notes"
        self.spec_dir = self.root / "spec"
        self.assets_path = self.root / "assets_index.json"
        self._samples = None

    def init(self) -> list[str]:
        created = []
        for d in (self.catalog_dir, self.sessions_dir, self.notes_dir, self.spec_dir):
            d.mkdir(parents=True, exist_ok=True)
        for f in PACKAGE_CATALOG.glob("*.yaml"):
            dst = self.catalog_dir / f.name
            if not dst.exists():
                shutil.copy(f, dst)
                created.append(str(dst))
        example = self.notes_dir / "notes.yaml"
        if not example.exists():
            example.write_text(
                "# 수동 답변: 자동 측정이 어려운 항목을 직접 관찰해서 적습니다.\n"
                "# Scp096:\n"
                "#   gate_force_conditions:\n"
                "#     answer: 분노 상태에서만 가능. 닫힌 게이트에 접근 시 자동 발동.\n"
                "#     evidence: sessions/096_gate.mp4 01:12-01:20\n", encoding="utf-8")
            created.append(str(example))
        return created

    # -- catalog
    def catalog(self) -> list[dict]:
        src = self.catalog_dir if self.catalog_dir.exists() else PACKAGE_CATALOG
        templates = {}
        entities = []
        for f in sorted(src.glob("*.yaml")):
            data = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
            if f.name.startswith("_"):
                templates.update(data)
            else:
                entities += data
        out = []
        for e in entities:
            e = dict(e)
            e.setdefault("name", e["id"])
            e.setdefault("tag", re.escape(str(e["id"])))
            qs = []
            for t in e.get("templates") or []:
                qs += [_expand(q, e) for q in templates.get(t, [])]
            own = [_expand(q, e) for q in e.get("questions") or []]
            own_ids = {q["id"] for q in own}
            e["questions"] = [q for q in qs if q["id"] not in own_ids] + own
            out.append(e)
        return out

    # -- evidence
    def samples(self) -> list[dict]:
        """All evidence. Files sharing the name part before the first dot form one session
        (``play3.jsonl``, ``play3.annotations.jsonl``, ``play3.sounds.jsonl``); sessions are
        moved apart on the time axis so events of different plays never interleave."""
        if self._samples is None:
            out = []
            files = sorted(self.sessions_dir.rglob("*.jsonl")) if self.sessions_dir.exists() else []
            sessions = sorted({f.name.split(".")[0] for f in files})
            for f in files:
                session = f.name.split(".")[0]
                shift = _read_offset(f) + SESSION_SPACING * sessions.index(session)
                for s in telemetry.load(f):
                    s["t"] = float(s["t"]) + shift
                    s["_session"] = session
                    out.append(s)
            self._samples = out
        return self._samples

    def notes(self) -> dict:
        out: dict = {}
        if self.notes_dir.exists():
            for f in sorted(self.notes_dir.glob("*.y*ml")):
                for ent, qs in (yaml.safe_load(f.read_text(encoding="utf-8")) or {}).items():
                    out.setdefault(str(ent), {}).update(qs or {})
        return out

    def assets(self) -> dict:
        if self.assets_path.exists():
            return json.loads(self.assets_path.read_text(encoding="utf-8"))
        return {}


def _read_offset(path: Path) -> float:
    """``# offset: 12.5`` in the first lines shifts a file's timestamps (align video to telemetry)."""
    with open(path, encoding="utf-8") as f:
        for _ in range(5):
            line = f.readline()
            m = re.match(r"#\s*offset:\s*(-?[\d.]+)", line)
            if m:
                return float(m.group(1))
    return 0.0


def _expand(q, entity):
    if isinstance(q, str):
        for k in ("id", "name", "tag"):
            q = q.replace("{" + k + "}", str(entity.get(k, "")))
        return q
    if isinstance(q, dict):
        return {k: _expand(v, entity) for k, v in q.items()}
    if isinstance(q, list):
        return [_expand(v, entity) for v in q]
    return q


# --- asset index ---------------------------------------------------------------------

def build_asset_index(paths: list[str], meters_per_stud=0.28) -> dict:
    files = []
    for p in map(Path, paths):
        files += [f for f in p.rglob("*") if f.is_file()] if p.is_dir() else [p]
    guids = ui_layout.guid_index(paths)
    idx: dict[str, list] = defaultdict(list)
    for f in sorted(files):
        suf = f.suffix.lower()
        try:
            if suf in (".unity", ".prefab"):
                scene = unity_scene.Scene.load(f)
                idx["lights"] += [{**l.__dict__, "path": l.path} for l in scene.lights()]
                rs = scene.render_settings()
                if rs:
                    idx["render_settings"].append(rs.__dict__)
                trees = ui_layout.extract(scene, guids)
                if any(t["root"]["Children"] for t in trees):
                    idx["ui"] += trees
                    idx["ui_text"] += [{**t, "source": f.name} for t in ui_layout.texts(trees)]
                idx["audio_sources"] += media.audio_sources(scene, guids, meters_per_stud)
            elif suf == ".mat":
                idx["materials"] += [m.__dict__ for m in unity_scene.read_material(f)]
            elif suf == ".anim":
                idx["animations"] += media.read_animation(f)
            elif suf == ".controller":
                idx["animators"] += media.read_animator(f, guids)
            elif suf in media.AUDIO_SUFFIXES:
                idx["audio_files"].append({**media.probe_audio(f), "path": str(f)})
            elif suf in (".ttf", ".otf"):
                idx["fonts"].append({"name": f.stem, "file": f.name, "path": str(f)})
        except (OSError, ValueError, yaml.YAMLError) as e:
            idx["errors"].append(f"{f}: {e}")
    used = Counter(t.get("Font") for t in idx["ui_text"] if t.get("Font"))
    known = {x["name"] for x in idx["fonts"]}
    for name, n in used.items():
        stem = Path(str(name)).stem
        if stem not in known:
            idx["fonts"].append({"name": stem, "file": None, "path": None})
            known.add(stem)
    for fnt in idx["fonts"]:
        fnt["used_by"] = sum(n for k, n in used.items() if Path(str(k)).stem == fnt["name"])
    return dict(idx)


# --- analyses -----------------------------------------------------------------------

def _sel(a: dict) -> dict:
    return {k: a.get(k) for k in SELECTOR_KEYS if a.get(k) is not None}


def _status(n: int, min_n: int) -> str:
    return "no_data" if n == 0 else "answered" if n >= min_n else "partial"


def _value_result(m: Measurement | None, min_n: int, label="") -> dict:
    if m is None:
        return {"status": "no_data"}
    spread = f" (p10–p90 {m.p10}–{m.p90})" if m.p10 is not None else ""
    return {"status": _status(m.n, min_n), "n": m.n, "value": m.to_dict(),
            "summary": f"{label}{m.value} {m.unit}{spread}, n={m.n}"}


def _pick(ms: dict, prefix: str, state=None) -> Measurement | None:
    keys = [k for k in ms if k.startswith(prefix) and (state is None or k.endswith("." + str(state)))]
    if not keys:
        return None
    return max((ms[k] for k in keys), key=lambda m: m.n)


def with_state_events(samples) -> list[dict]:
    """Add ``state:<value>`` pseudo-events where an entity's state changes."""
    out = list(samples)
    last: dict[str, str] = {}
    for s in sorted(samples, key=lambda s: float(s["t"])):
        if "state" in s:
            k = str(s["id"])
            v = str(s["state"])
            if last.get(k) not in (None, v):
                ev = {"t": s["t"], "id": s["id"], "event": f"state:{v}"}
                if "tag" in s:
                    ev["tag"] = s["tag"]
                out.append(ev)
            last[k] = v
    return out


def _latencies(samples, frm, to, tag=None, window=120.0):
    evs = timeline.events(samples)
    out = []
    for i, a in enumerate(evs):
        if a["event"] != frm or (tag is not None and str(a.get("tag")) != str(tag)):
            continue
        for b in evs[i + 1:]:
            dt = float(b["t"]) - float(a["t"])
            if dt > window:
                break
            if b["event"] == to and (b.get("tag") == a.get("tag") or b.get("id") == a.get("id")):
                out.append(dt)
                break
    return out


def _intervals(samples, event, **sel):
    last: dict[str, float] = {}
    gaps = []
    for s in sorted(derive.select(samples, event=event, **sel), key=lambda s: float(s["t"])):
        k = str(s["id"])
        if k in last and float(s["t"]) - last[k] <= MAX_INTERVAL:
            gaps.append(float(s["t"]) - last[k])
        last[k] = float(s["t"])
    return gaps


def _formula_points(a: dict, samples):
    ex = a.get("extract", "fields")
    if ex == "bounce":
        return derive.bounce_series(samples, id=a.get("id"), tag=a.get("tag") if a.get("id") is None else None)
    if ex == "falloff":
        return derive.falloff_pairs(samples, tag=a.get("tag"), event=a.get("event", "hit"),
                                    dist_field=a.get("dist_field", "dist"), value_field=a.get("value_field", "damage"))
    if ex == "time":
        sel = {k: a[k] for k in ("id", "role") if a.get(k) is not None}
        return derive.time_series(samples, y=a.get("y_field", "speed"), start_event=a.get("start_event"),
                                  start_tag=a.get("tag"), **sel)
    sel = {k: a[k] for k in ("id", "role", "tag", "event") if a.get(k) is not None}
    return derive.field_pairs(samples, a["x_field"], a["y_field"], **sel)


def run_question(ent: dict, q: dict, project: Project, plots_dir: Path | None) -> dict:
    a = q.get("analysis") or {"type": "manual"}
    kind = a.get("type", "manual")
    samples = project.samples()
    min_n = int(q.get("min_n", 10 if kind == "formula" else 5))
    sel = _sel(a)

    if kind == "manual":
        note = project.notes().get(ent["id"], {}).get(q["id"])
        if note:
            return {"status": "manual", "summary": str(note.get("answer", "")).strip(), "note": note}
        return {"status": "todo"}

    if kind == "speed":
        m = _pick(telemetry.speeds(derive.select(samples, **sel), "sessions"), "speed.", a.get("state"))
        return _value_result(m, min_n)

    if kind in ("rate", "regen_delay"):
        prefix = "rate." if kind == "rate" else "regen_delay."
        ms = telemetry.rates(derive.select(samples, **sel), a["field"], "sessions")
        return _value_result(_pick(ms, prefix + a["field"] + ".", a.get("state")), min_n)

    if kind == "field_max":
        vals = [float(s[a["field"]]) for s in derive.select(samples, **sel) if isinstance(s.get(a["field"]), (int, float))]
        if not vals:
            return {"status": "no_data"}
        per_id = defaultdict(float)
        for s in derive.select(samples, **sel):
            if isinstance(s.get(a["field"]), (int, float)):
                per_id[str(s["id"])] = max(per_id[str(s["id"])], float(s[a["field"]]))
        mx = list(per_id.values())
        m = Measurement(value=mode_bin(mx, 0.5), unit=a["field"], provenance="observed:sessions", n=len(mx),
                        note="most common per-entity maximum", extra={"max": max(vals)})
        return _value_result(m, 1)

    if kind == "duration":
        d = telemetry.durations(derive.select(samples, **sel) if sel else samples, "sessions")
        return _value_result(d.get(f"duration.{a['event']}"), min_n)

    if kind == "interval":
        gaps = _intervals(samples, a["event"], **sel)
        if not gaps:
            return {"status": "no_data"}
        m = summarize(gaps, "s", "observed:sessions", "min = cooldown upper bound when spammed")
        m.extra["min"] = round(min(gaps), 4)
        r = _value_result(m, min_n)
        r["summary"] += f", min {m.extra['min']} s"
        return r

    if kind == "damage":
        return _value_result(telemetry.damage(samples, "sessions").get(f"damage.{a['tag']}"), min_n)

    if kind == "latency":
        lat = _latencies(samples, a["from"], a["to"], a.get("tag"), float(a.get("window", 120)))
        return _value_result(summarize(lat, "s", "observed:sessions") if lat else None, min_n)

    if kind == "count_between":
        counts = derive.counts_between(samples, a["event"], a["reset"], **sel)
        if not counts:
            return {"status": "no_data"}
        m = Measurement(value=max(counts), unit="count", provenance="observed:sessions", n=len(counts),
                        extra={"mode": Counter(counts).most_common(1)[0][0]})
        return _value_result(m, 3)

    if kind == "field_delta":
        deltas = derive.field_deltas(samples, a["field"], a["event"], **sel)
        return _value_result(summarize(deltas, a["field"], "observed:sessions") if deltas else None, min_n)

    if kind == "jump":
        js = derive.jumps(samples, **sel)
        if not js:
            return {"status": "no_data"}
        h = summarize([j["height"] for j in js], "m", "observed:sessions")
        air = [j["airtime"] for j in js if j["airtime"] is not None]
        r = _value_result(h, min_n, "높이 ")
        if air:
            at = summarize(air, "s", "observed:sessions")
            r["airtime"] = at.to_dict()
            r["summary"] += f" / 체공 {at.value} s"
        return r

    if kind == "formula":
        pts = _formula_points(a, samples)
        if len(pts) < 3:
            return {"status": "no_data" if not pts else "partial", "n": len(pts)}
        models = {"growth": fit.GROWTH, "falloff": fit.FALLOFF}.get(a.get("models"), a.get("models") or fit.DEFAULT)
        fits = fit.discover([p[0] for p in pts], [p[1] for p in pts], models)
        if not fits:
            return {"status": "partial", "n": len(pts), "summary": "no model converged"}
        best = fits[0]
        status = "answered" if len(pts) >= min_n and best.r2 >= 0.95 else "partial"
        r = {"status": status, "n": len(pts), "formula": best.to_dict(),
             "alternatives": [f.to_dict() for f in fits[1:]],
             "summary": f"{best.equation()} (R²={best.r2:.4f}, n={len(pts)})"}
        if plots_dir is not None:
            plots_dir.mkdir(parents=True, exist_ok=True)
            name = f"{ent['id']}__{q['id']}.svg"
            (plots_dir / name).write_text(svgplot.scatter(pts, fits, a.get("x", "x"), a.get("y", "y"), q["ask"]),
                                          encoding="utf-8")
            r["plot"] = name
        return r

    if kind == "sequence":
        seq = timeline.sequence(with_state_events(samples), a["trigger"], float(a.get("window", 10)),
                                a.get("scope", "any"), a.get("tag"), float(a.get("min_confidence", 0.6)))
        if not seq["occurrences"]:
            return {"status": "no_data"}
        status = _status(seq["occurrences"], int(q.get("min_n", 3))) if seq["steps"] else "partial"
        summary = " → ".join(f"{s['event']}@{s['offset_median']}s" for s in seq["steps"][:6])
        return {"status": status, "n": seq["occurrences"], "sequence": seq,
                "mermaid": timeline.mermaid_sequence(seq), "summary": summary or "반복되는 후속 이벤트 없음"}

    if kind == "states":
        sm = timeline.state_machine(samples, key=a.get("key", "state"), **sel)
        if not sm["transitions"]:
            return {"status": "no_data"}
        n = sum(t["count"] for t in sm["transitions"])
        return {"status": _status(n, min_n), "n": n, "states": sm, "mermaid": timeline.mermaid_states(sm),
                "summary": f"{len(sm['dwell'])} states, {len(sm['transitions'])} transitions"}

    if kind == "context":
        ctx = timeline.context(samples, a["event"])
        n = sum(1 for e in timeline.events(samples) if e["event"] == a["event"])
        return {"status": _status(n, 3), "n": n, "context": ctx,
                "summary": "; ".join(f"{k}: {v}" for k, v in ctx.items()) or "no context fields"}

    if kind == "asset":
        items = project.assets().get(a["kind"], [])
        rx = re.compile(a["match"]) if a.get("match") else None
        if rx:
            items = [i for i in items if rx.search(" ".join(str(i.get(k, "")) for k in
                                                            ("name", "path", "source", "clip", "file", "unity_path")))]
        return {"status": "answered" if items else "no_data", "n": len(items), "items": items,
                "summary": f"{len(items)} {a['kind']}"}

    return {"status": "todo", "summary": f"unknown analysis type '{kind}'"}


# --- documents -------------------------------------------------------------------------

def _md_value(r):
    v = r.get("value") or {}
    rows = ["| 값 | 단위 | p10 | p90 | n | 출처 |", "|---|---|---|---|---|---|",
            f"| {v.get('value')} | {v.get('unit')} | {v.get('p10', '')} | {v.get('p90', '')} | {v.get('n')} | {v.get('provenance')} |"]
    if v.get("extra"):
        rows.append(f"\n추가: `{json.dumps(v['extra'], ensure_ascii=False)}`")
    if r.get("airtime"):
        rows.append(f"\n체공 시간: {r['airtime']['value']} s (p10–p90 {r['airtime'].get('p10')}–{r['airtime'].get('p90')})")
    return "\n".join(rows)


def _md_formula(r):
    f = r["formula"]
    lines = [f"**{f['equation']}**", "",
             f"- 모델: `{f['model']}` — `{f['formula']}`",
             f"- 원시 파라미터: `{json.dumps(f['params'])}`",
             f"- R² = {f['r2']}, RMSE = {f['rmse']}, n = {f['n']}"]
    if f.get("nice_params"):
        lines.append(f"- 반올림 파라미터(노이즈 범위 내): `{json.dumps(f['nice_params'])}` (RMSE {f['nice_rmse']})")
    if r.get("alternatives"):
        lines += ["", "| 대안 모델 | 식 | R² | ΔAICc |", "|---|---|---|---|"]
        for alt in r["alternatives"]:
            lines.append(f"| {alt['model']} | {alt['equation']} | {alt['r2']} | {round(alt['aicc'] - f['aicc'], 2)} |")
    if r.get("plot"):
        lines += ["", f"![plot](../plots/{r['plot']})"]
    return "\n".join(lines)


def _md_sequence(r):
    seq = r["sequence"]
    lines = [f"트리거 `{seq['trigger']}` 관측 {seq['occurrences']}회", "",
             "| 이벤트 | 오프셋 중앙값 (s) | p10 | p90 | 재현율 | n |", "|---|---|---|---|---|---|"]
    for s in seq["steps"]:
        lines.append(f"| `{s['event']}` | {s['offset_median']} | {s['offset_p10']} | {s['offset_p90']} | "
                     f"{int(s['confidence'] * 100)}% | {s['n']} |")
    lines += ["", "```mermaid", r["mermaid"], "```"]
    return "\n".join(lines)


def _md_states(r):
    sm = r["states"]
    lines = ["```mermaid", r["mermaid"], "```", "", "| 상태 | 체류 중앙값 (s) | 최소 | 최대 | n |", "|---|---|---|---|---|"]
    for k, d in sm["dwell"].items():
        lines.append(f"| {k} | {d['median']} | {d['min']} | {d['max']} | {d['n']} |")
    return "\n".join(lines)


ASSET_COLUMNS = {
    "audio_files": ("file", "duration", "sample_rate", "channels"),
    "animations": ("name", "length", "loop", "events"),
    "animators": ("name", "parameters"),
    "audio_sources": ("path", "clip", "volume", "pitch", "loop", "min_distance_m", "max_distance_m", "rolloff"),
    "ui_text": ("unity_path", "Text", "Font", "TextSize", "TextXAlignment", "TextYAlignment", "Position", "Size"),
    "fonts": ("name", "file", "used_by"),
    "lights": ("path", "type", "intensity", "range", "color"),
}


def _md_asset(r, kind, limit=60):
    items = r["items"]
    cols = ASSET_COLUMNS.get(kind, ("name", "source"))
    lines = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    for it in items[:limit]:
        cells = []
        for c in cols:
            v = it.get(c)
            if isinstance(v, (list, dict)):
                v = json.dumps(v, ensure_ascii=False)
                v = v if len(v) < 120 else v[:117] + "..."
            cells.append(str(v).replace("|", "\\|").replace("\n", " ") if v is not None else "")
        lines.append("| " + " | ".join(cells) + " |")
    if len(items) > limit:
        lines.append(f"\n… 외 {len(items) - limit}개 (spec.json 참고)")
    if kind == "animators":
        for ctrl in items[:5]:
            lines += ["", f"**{ctrl['name']}**", "", "```mermaid", media.mermaid_animator(ctrl), "```"]
    return "\n".join(lines)


def entity_markdown(ent: dict, results: dict) -> str:
    done = sum(1 for r in results.values() if r["status"] in ("answered", "manual"))
    lines = [f"# {ent['name']}", "", f"`{ent['id']}` · {ent.get('category', '')} · 완료 {done}/{len(results)}", "",
             "| | 항목 | 요약 |", "|---|---|---|"]
    for q in ent["questions"]:
        r = results[q["id"]]
        lines.append(f"| {STATUS_ICON[r['status']]} | [{q['ask']}](#{q['id']}) | {r.get('summary', '').replace('|', '/')} |")
    for q in ent["questions"]:
        r = results[q["id"]]
        lines += ["", f"<a id=\"{q['id']}\"></a>", f"## {q['ask']}", "",
                  f"{STATUS_ICON[r['status']]} `{r['status']}` · 분석 `{q.get('analysis', {}).get('type', 'manual')}`", ""]
        if "formula" in r:
            lines.append(_md_formula(r))
        elif "sequence" in r:
            lines.append(_md_sequence(r))
        elif "states" in r:
            lines.append(_md_states(r))
        elif "items" in r and r["items"]:
            lines.append(_md_asset(r, q["analysis"]["kind"]))
        elif "value" in r:
            lines.append(_md_value(r))
        elif "context" in r:
            lines.append("```json\n" + json.dumps(r["context"], ensure_ascii=False, indent=2) + "\n```")
        elif r["status"] == "manual":
            note = r["note"]
            lines.append(str(note.get("answer", "")).strip())
            if note.get("evidence"):
                lines.append(f"\n근거: {note['evidence']}")
        if r["status"] in ("no_data", "todo", "partial") and q.get("record"):
            lines.append(f"\n> 기록 방법: {q['record']}")
    return "\n".join(lines) + "\n"


def build(project: Project, meters_per_stud=0.28) -> dict:
    spec_dir = project.spec_dir
    (spec_dir / "entities").mkdir(parents=True, exist_ok=True)
    plots = spec_dir / "plots"
    catalog = project.catalog()
    all_results = {}
    for ent in catalog:
        res = {q["id"]: run_question(ent, q, project, plots) for q in ent["questions"]}
        all_results[ent["id"]] = res
        _write(spec_dir / "entities" / f"{ent['id']}.md", entity_markdown(ent, res))
    _write(spec_dir / "index.md", index_markdown(catalog, all_results, len(project.samples())))
    _write(spec_dir / "PLAN.md", plan_markdown(catalog, all_results))
    _write(spec_dir / "GameSpec.luau", luau_spec(catalog, all_results, meters_per_stud))
    (spec_dir / "spec.json").write_text(json.dumps(all_results, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    return all_results


def _write(path: Path, text: str):
    hits = guard.scan_text(text)
    if hits:
        raise SystemExit(f"guard: {path} would contain code-like content: {hits[:3]}")
    path.write_text(text, encoding="utf-8")


def index_markdown(catalog, results, n_samples) -> str:
    total = sum(len(r) for r in results.values())
    done = sum(1 for r in results.values() for x in r.values() if x["status"] in ("answered", "manual"))
    partial = sum(1 for r in results.values() for x in r.values() if x["status"] == "partial")
    lines = ["# 명세서 현황", "", f"증거 샘플 {n_samples}개 · 전체 {total}항목 · 완료 {done} · 부분 {partial} · "
             f"진행률 {round(100 * done / max(total, 1))}%", "",
             "다음 플레이 세션에서 할 일은 [PLAN.md](PLAN.md), 카탈로그에 없는 새 규칙 후보는 [DISCOVERED.md](DISCOVERED.md).", "",
             "| 엔티티 | 분류 | 완료 | 부분 | 미측정 |", "|---|---|---|---|---|"]
    for ent in catalog:
        r = results[ent["id"]].values()
        c = Counter(x["status"] for x in r)
        lines.append(f"| [{ent['name']}](entities/{ent['id']}.md) | {ent.get('category', '')} | "
                     f"{c['answered'] + c['manual']} | {c['partial']} | {c['no_data'] + c['todo']} |")
    return "\n".join(lines) + "\n"


def plan_markdown(catalog, results) -> str:
    lines = ["# 다음 플레이 세션 체크리스트", "",
             "부분(🟡) 항목은 샘플을 더 모으면 확정됩니다. 미측정(⬜) 항목은 아래 방법대로 기록하세요.",
             "어노테이터 단축키 프리셋: `python -m scpsl_cleanroom kb hotkeys <엔티티id>`", ""]
    for ent in catalog:
        todo = [(q, results[ent["id"]][q["id"]]) for q in ent["questions"]
                if results[ent["id"]][q["id"]]["status"] in ("partial", "no_data", "todo")]
        todo = [(q, r) for q, r in todo if q.get("analysis", {}).get("type") != "asset"]
        if not todo:
            continue
        lines += ["", f"## {ent['name']} (`{ent['id']}`)", ""]
        for q, r in todo:
            how = q.get("record") or ("notes/에 직접 기록" if q.get("analysis", {}).get("type") == "manual" else "")
            n = f" (현재 n={r['n']})" if r.get("n") else ""
            lines.append(f"- [ ] {STATUS_ICON[r['status']]} **{q['ask']}**{n} — {how}")
    return "\n".join(lines) + "\n"


def luau_spec(catalog, results, meters_per_stud) -> str:
    root: dict = {"METERS_PER_STUD": meters_per_stud}
    for ent in catalog:
        node = {}
        for q in ent["questions"]:
            r = results[ent["id"]][q["id"]]
            if r["status"] not in ("answered", "partial"):
                continue
            if "formula" in r:
                f = r["formula"]
                node[q["id"]] = {"model": f["model"], "formula": f["formula"], **(f.get("nice_params") or f["params"])}
            elif "value" in r:
                node[q["id"]] = r["value"]["value"]
            elif "sequence" in r:
                node[q["id"]] = [{"event": s["event"], "offset": s["offset_median"]} for s in r["sequence"]["steps"]]
        if node:
            root[ent["id"]] = node
    return ("--!strict\n-- Generated from the clean-room spec (observed/asset/design values only).\n"
            "-- Distances are in meters; multiply by 1 / METERS_PER_STUD for studs.\n"
            f"return {to_luau(root)}\n")


# --- discovery & tooling --------------------------------------------------------------------

def referenced_events(catalog) -> set[str]:
    out = set()
    for ent in catalog:
        for q in ent["questions"]:
            a = q.get("analysis") or {}
            for k in EVENT_KEYS:
                if a.get(k):
                    ev = str(a[k])
                    out |= {ev, ev + "_start", ev + "_end"}
    return out


def discover(project: Project, window=10.0) -> str:
    samples = with_state_events(project.samples())
    catalog = project.catalog()
    known = referenced_events(catalog)
    counts = Counter(e["event"] for e in timeline.events(samples))
    lines = ["# 자동 발견된 규칙 후보", "",
             "카탈로그 질문이 다루지 않는 반복 패턴입니다. 의미 있는 것은 catalog/에 질문으로 추가하세요.", "",
             "## 관측된 이벤트", "", "| 이벤트 | 횟수 | 카탈로그 |", "|---|---|---|"]
    for ev, n in counts.most_common():
        lines.append(f"| `{ev}` | {n} | {'✓' if ev in known else '새 이벤트'} |")
    for scope in ("id", "tag", "any"):
        rules = [r for r in timeline.mine_rules(samples, window, scope)
                 if not (r.cause in known and r.effect in known)]
        if not rules:
            continue
        lines += ["", f"## 'A 다음엔 B' 규칙 (범위: {scope})", "",
                  "| A | B | 재현율 | A 횟수 | 지연 중앙값 (s) | p10–p90 | 표준편차 |", "|---|---|---|---|---|---|---|"]
        for r in rules[:60]:
            d = r.to_dict()
            lines.append(f"| `{d['cause']}` | `{d['effect']}` | {int(d['confidence'] * 100)}% | {d['count']} | "
                         f"{d['latency_median']} | {d['latency_p10']}–{d['latency_p90']} | {d['latency_stdev']} |")
        lines += ["", "추가 예시:", "```yaml", "questions:"]
        for r in rules[:3]:
            lines += [f"  - id: after_{re.sub(r'[^a-z0-9]+', '_', r.cause.lower()).strip('_')}",
                      f"    ask: {r.cause} 이후 타임라인",
                      f"    analysis: {{type: sequence, trigger: \"{r.cause}\", scope: {scope}}}"]
        lines.append("```")
    roles = sorted({str(s["role"]) for s in samples if "role" in s and "state" in s})
    for role in roles:
        sm = timeline.state_machine(samples, role=role)
        if sm["transitions"]:
            lines += ["", f"## 상태 머신: {role}", "", "```mermaid", timeline.mermaid_states(sm), "```"]
    return "\n".join(lines) + "\n"


HOTKEYS = "1234567890qwertyuiopasdfghjkl"


def hotkeys(project: Project, entity_id: str) -> dict:
    ent = next((e for e in project.catalog() if e["id"] == entity_id), None)
    if ent is None:
        raise SystemExit(f"unknown entity {entity_id}")
    events: list[tuple[str, str | None]] = []
    for q in ent["questions"]:
        a = q.get("analysis") or {}
        kind = a.get("type")
        tag = a.get("tag")
        if kind == "duration":
            events += [(a["event"] + "_start", tag), (a["event"] + "_end", tag)]
        for k in ("event", "trigger", "from", "to", "reset", "start_event"):
            if a.get(k) and kind != "duration":
                events.append((str(a[k]), tag))
    seen, keys = set(), []
    for ev, tag in events:
        if (ev, tag) in seen:
            continue
        seen.add((ev, tag))
        keys.append({"key": HOTKEYS[len(keys)], "event": ev, **({"tag": tag} if tag else {})})
        if len(keys) == len(HOTKEYS):
            break
    return {"entity": entity_id, "id": entity_id, "keys": keys}
