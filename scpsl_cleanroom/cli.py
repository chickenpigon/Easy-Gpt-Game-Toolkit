"""Command line entry point: ``python -m scpsl_cleanroom <command> ...``."""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

from . import derive, fit, gaps, guard, kb, media, roblox, spec as specmod, telemetry, timeline, ui_layout, unity_scene
from .measure import Measurement


def _options(a) -> roblox.ConvertOptions:
    return roblox.ConvertOptions(meters_per_stud=a.meters_per_stud, brightness_scale=a.brightness_scale)


def _open_spec(a) -> dict:
    p = Path(a.spec)
    return specmod.load_spec(p) if p.exists() else specmod.new_spec(a.title, a.target)


def _guarded_write(path: Path, text: str) -> None:
    hits = guard.scan_text(text)
    if hits:
        for line, rule, frag in hits:
            print(f"  {path}:{line}: {rule}: {frag}", file=sys.stderr)
        raise SystemExit(f"guard: refusing to write {path}, it contains code-like content")
    path.write_text(text, encoding="utf-8")


def cmd_assets(a) -> int:
    opt = _options(a)
    result = unity_scene.scan(a.paths)
    converted = {
        "lights": [roblox.convert_light(l, opt) for l in result["lights"]],
        "lighting": [roblox.convert_render_settings(r, opt) for r in result["render_settings"]],
        "materials": [roblox.convert_material(m) for m in result["materials"]],
    }
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    payload = specmod.assets_payload(result, converted)
    (out / "assets.json").write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    _guarded_write(out / "LightingData.luau",
                   roblox.lighting_module(converted["lights"], converted["lighting"], converted["materials"]))
    if a.spec:
        s = _open_spec(a)
        s["assets"] = {k: payload[k] for k in ("lights", "lighting", "materials")}
        specmod.save_spec(s, a.spec)
    clamped = sum(1 for l in converted["lights"] if l["range_clamped"])
    print(f"lights: {len(converted['lights'])} ({clamped} range-clamped to {roblox.ROBLOX_MAX_LIGHT_RANGE} studs), "
          f"render settings: {len(converted['lighting'])}, materials: {len(converted['materials'])}")
    for e in result["errors"]:
        print(f"warning: {e}", file=sys.stderr)
    print(f"wrote {out / 'assets.json'} and {out / 'LightingData.luau'}")
    return 0


def _print_measurements(ms: dict[str, Measurement]) -> None:
    for k, m in sorted(ms.items()):
        spread = f" [{m.p10} .. {m.p90}]" if m.p10 is not None else ""
        print(f"  {k:<48} {m.value:>10} {m.unit:<10}{spread} n={m.n}")


def cmd_observe(a) -> int:
    s = _open_spec(a)
    for path in a.telemetry:
        ms = telemetry.analyze(path)
        print(f"{path}: {len(ms)} values")
        _print_measurements(ms)
        skipped = specmod.merge_measurements(s, ms, overwrite=a.overwrite)
        for k in skipped:
            print(f"  kept design value for {k}")
    specmod.save_spec(s, a.spec)
    for p in specmod.validate(s):
        print(f"note: {p}")
    return 0


def cmd_design(a) -> int:
    s = _open_spec(a)
    specmod.set_design_value(s, a.key, a.value, a.unit, a.note)
    specmod.save_spec(s, a.spec)
    return 0


def cmd_build(a) -> int:
    s = specmod.load_spec(a.spec)
    problems = specmod.validate(s)
    for p in problems:
        print(f"note: {p}")
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    _guarded_write(out / "SPEC.md", specmod.to_markdown(s))
    _guarded_write(out / "GameConfig.luau", specmod.to_luau_config(s, _options(a)))
    print(f"wrote {out / 'SPEC.md'} and {out / 'GameConfig.luau'}")
    return 0


def cmd_compare(a) -> int:
    s = specmod.load_spec(a.spec)
    port = telemetry.analyze(a.telemetry)
    if a.port_meters_per_stud:  # telemetry recorded in studs -> convert to meters
        k = a.port_meters_per_stud
        for m in port.values():
            if m.unit.startswith("m/") or m.unit == "m":
                m.value *= k
                m.p10 = m.p10 * k if m.p10 is not None else None
                m.p90 = m.p90 * k if m.p90 is not None else None
    rows, ok = specmod.compare(s, port, a.tolerance)
    for r in rows:
        if r["status"] == "missing":
            print(f"  MISSING {r['key']:<48} spec={r['spec']}")
        else:
            flag = "ok " if r["status"] == "ok" else "OFF"
            print(f"  {flag}     {r['key']:<48} spec={r['spec']:<10} port={round(r['port'], 4):<10} "
                  f"err={r['error_pct']}%")
    return 0 if ok else 1


def cmd_guard(a) -> int:
    report = guard.scan_paths(a.paths)
    for f, hits in report.items():
        for line, rule, frag in hits:
            print(f"{f}:{line}: {rule}: {frag}")
    print("clean" if not report else f"{len(report)} file(s) flagged")
    return 1 if report else 0


def cmd_validate(a) -> int:
    problems = specmod.validate(specmod.load_spec(a.spec))
    for p in problems:
        print(p)
    return 1 if any("not allowed" in p or "malformed" in p for p in problems) else 0


def _write_jsonl(path, rows):
    Path(path).write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")


def cmd_fit(a) -> int:
    samples = []
    for f in a.files:
        samples += telemetry.load(f)
    sel = {k: getattr(a, k) for k in ("id", "role", "tag", "event") if getattr(a, k)}
    if a.extract == "bounce":
        pts = derive.bounce_series(samples, id=a.id, tag=a.tag)
    elif a.extract == "falloff":
        pts = derive.falloff_pairs(samples, tag=a.tag, dist_field=a.x or "dist", value_field=a.y or "damage")
    elif a.extract == "time":
        pts = derive.time_series(samples, y=a.y or "speed", start_event=a.event, start_tag=a.tag,
                                 **{k: v for k, v in sel.items() if k in ("id", "role")})
    else:
        pts = derive.field_pairs(samples, a.x, a.y, **sel)
    if len(pts) < 3:
        raise SystemExit(f"only {len(pts)} points")
    models = {"growth": fit.GROWTH, "falloff": fit.FALLOFF, "all": fit.DEFAULT}.get(a.models) or a.models.split(",")
    fits = fit.discover([p[0] for p in pts], [p[1] for p in pts], models, top=a.top)
    print(f"{len(pts)} points")
    for i, f in enumerate(fits):
        print(f"{i + 1}. {f.equation():<48} R2={f.r2:.5f} RMSE={f.rmse:.4g} AICc={f.aicc:.2f}  [{f.model}: {f.formula}]")
        print(f"   raw {json.dumps({k: float(f'{v:.6g}') for k, v in f.params.items()})}")
    if a.plot:
        from . import svgplot
        Path(a.plot).write_text(svgplot.scatter(pts, fits, a.x or "x", a.y or "y"), encoding="utf-8")
        print(f"wrote {a.plot}")
    return 0


def cmd_timeline(a) -> int:
    samples = []
    for f in a.files:
        samples += telemetry.load(f)
    samples = kb.with_state_events(samples)
    if a.trigger:
        seq = timeline.sequence(samples, a.trigger, a.window, a.scope, a.tag, a.min_confidence)
        print(f"trigger {a.trigger}: {seq['occurrences']} occurrences")
        for s in seq["steps"]:
            print(f"  +{s['offset_median']:>8.3f}s  [{s['offset_p10']:.3f} .. {s['offset_p90']:.3f}]  "
                  f"{int(s['confidence'] * 100):>3}%  {s['event']}")
        print("\n" + timeline.mermaid_sequence(seq))
    else:
        for r in timeline.mine_rules(samples, a.window, a.scope, min_confidence=a.min_confidence):
            d = r.to_dict()
            print(f"  {d['cause']} -> {d['effect']}: {int(d['confidence'] * 100)}% of {d['count']}, "
                  f"+{d['latency_median']}s [{d['latency_p10']} .. {d['latency_p90']}]")
    return 0


def cmd_ui(a) -> int:
    guids = ui_layout.guid_index(a.paths)
    trees = []
    for p in map(Path, a.paths):
        files = [f for f in p.rglob("*") if f.suffix in (".prefab", ".unity")] if p.is_dir() else [p]
        for f in sorted(files):
            trees += [t for t in ui_layout.extract(unity_scene.Scene.load(f), guids) if t["root"]["Children"]]
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "ui.json").write_text(json.dumps(trees, indent=1, ensure_ascii=False), encoding="utf-8")
    _guarded_write(out / "UiLayout.luau", "--!strict\n-- Generated from licensed UI prefabs.\nreturn "
                   + roblox.to_luau(trees) + "\n")
    texts = ui_layout.texts(trees)
    fonts = sorted({str(t["Font"]) for t in texts if t.get("Font")})
    print(f"{len(trees)} canvases, {len(texts)} text elements, fonts: {', '.join(fonts) or '-'}")
    print(f"wrote {out / 'ui.json'} and {out / 'UiLayout.luau'}")
    return 0


def cmd_sounds(a) -> int:
    if a.action == "match":
        rows = media.match_sounds(a.recording, a.library, a.threshold, a.offset)
    else:
        rows = media.onsets(a.recording, a.sensitivity, a.offset)
    for r in rows:
        print(f"  {r['t']:>9.3f}s  {r['event']}  {r.get('score', r.get('strength', ''))}")
    if a.out:
        _write_jsonl(a.out, rows)
        print(f"wrote {len(rows)} events to {a.out}")
    return 0


def cmd_kb(a) -> int:
    project = kb.Project(a.project)
    if a.action == "init":
        for f in project.init():
            print(f"created {f}")
        print(f"project ready at {project.root}")
    elif a.action == "index":
        idx = kb.build_asset_index(a.paths, a.meters_per_stud)
        project.assets_path.write_text(json.dumps(idx, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
        print(", ".join(f"{k}: {len(v)}" for k, v in idx.items()))
    elif a.action == "build":
        res = kb.build(project, a.meters_per_stud)
        c = Counter(x["status"] for r in res.values() for x in r.values())
        print(f"{sum(c.values())} questions: " + ", ".join(f"{k} {v}" for k, v in c.most_common()))
        print(f"wrote {project.spec_dir}/index.md, PLAN.md, entities/, GameSpec.luau")
    elif a.action == "discover":
        project.spec_dir.mkdir(parents=True, exist_ok=True)
        kb._write(project.spec_dir / "DISCOVERED.md", kb.discover(project, a.window))
        print(f"wrote {project.spec_dir / 'DISCOVERED.md'}")
    elif a.action == "demo":
        from . import demo
        project.init()
        for f in demo.make_demo(project.root):
            print(f"wrote {f} (synthetic)")
    elif a.action == "hotkeys":
        print(json.dumps(kb.hotkeys(project, a.entity), ensure_ascii=False, indent=1))
    return 0


def cmd_gaps(a) -> int:
    if a.action == "plan":
        r = gaps.plan(a.csv, a.project, a.plan, tuple(a.priority.split(",")))
        print(f"{r['sessions']} sessions")
        for f in r["files"]:
            print(f"  {f}")
    elif a.action == "apply":
        out = a.out or str(Path(a.csv).with_suffix("")) + ".measured.csv"
        report = Path(a.project) / "spec" / "MEASURED.md"
        r = gaps.apply(a.csv, a.project, out, report)
        print(f"{r['results']} rows with data: " + ", ".join(f"{k} {v}" for k, v in sorted(r["counts"].items())))
        print(f"wrote {out} and {report}")
    else:
        print(gaps.status(a.csv, a.project))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="scpsl_cleanroom", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    def spec_args(sp, required=True):
        sp.add_argument("--spec", required=required, help="spec.json (created if missing)")
        sp.add_argument("--title", default="SCP:SL port spec")
        sp.add_argument("--target", default="SCP: Secret Laboratory 10.2.2")

    def unit_args(sp):
        sp.add_argument("--meters-per-stud", type=float, default=0.28)
        sp.add_argument("--brightness-scale", type=float, default=1.0)

    sp = sub.add_parser("assets", help="convert lights/fog/materials from exported scenes")
    sp.add_argument("paths", nargs="+")
    sp.add_argument("-o", "--out", default="out")
    spec_args(sp, required=False)
    unit_args(sp)
    sp.set_defaults(fn=cmd_assets)

    sp = sub.add_parser("observe", help="analyze telemetry and merge values into the spec")
    sp.add_argument("telemetry", nargs="+")
    sp.add_argument("--overwrite", action="store_true", help="replace instead of pooling")
    spec_args(sp)
    sp.set_defaults(fn=cmd_observe)

    sp = sub.add_parser("design", help="record a value the porting team chose itself")
    sp.add_argument("key")
    sp.add_argument("value", type=float)
    sp.add_argument("unit")
    sp.add_argument("--note", default="")
    spec_args(sp)
    sp.set_defaults(fn=cmd_design)

    sp = sub.add_parser("build", help="write SPEC.md and GameConfig.luau")
    sp.add_argument("--spec", required=True)
    sp.add_argument("-o", "--out", default="out")
    unit_args(sp)
    sp.set_defaults(fn=cmd_build)

    sp = sub.add_parser("compare", help="measure the Roblox port and diff it against the spec")
    sp.add_argument("telemetry")
    sp.add_argument("--spec", required=True)
    sp.add_argument("--tolerance", type=float, default=0.05)
    sp.add_argument("--port-meters-per-stud", type=float, default=0.0,
                    help="set if port telemetry positions are in studs")
    sp.set_defaults(fn=cmd_compare)

    sp = sub.add_parser("guard", help="flag code-like content in files")
    sp.add_argument("paths", nargs="+")
    sp.set_defaults(fn=cmd_guard)

    sp = sub.add_parser("fit", help="discover the formula behind observed data")
    sp.add_argument("files", nargs="+")
    sp.add_argument("--extract", choices=("fields", "bounce", "falloff", "time"), default="fields")
    sp.add_argument("-x", help="x field (fields) / distance field (falloff)")
    sp.add_argument("-y", help="y field")
    for k in ("id", "role", "tag", "event"):
        sp.add_argument(f"--{k}")
    sp.add_argument("--models", default="all", help="growth | falloff | all | comma list")
    sp.add_argument("--top", type=int, default=3)
    sp.add_argument("--plot", help="write an SVG plot")
    sp.set_defaults(fn=cmd_fit)

    sp = sub.add_parser("timeline", help="mine 'A then B' rules or the sequence after a trigger")
    sp.add_argument("files", nargs="+")
    sp.add_argument("--trigger")
    sp.add_argument("--tag")
    sp.add_argument("--scope", choices=("any", "id", "tag"), default="any")
    sp.add_argument("--window", type=float, default=10.0)
    sp.add_argument("--min-confidence", type=float, default=0.8)
    sp.set_defaults(fn=cmd_timeline)

    sp = sub.add_parser("ui", help="UI layout and fonts from exported prefabs -> Roblox")
    sp.add_argument("paths", nargs="+")
    sp.add_argument("-o", "--out", default="out")
    sp.set_defaults(fn=cmd_ui)

    sp = sub.add_parser("sounds", help="find library sounds (or raw onsets) in a gameplay recording")
    sp.add_argument("action", choices=("match", "onsets"))
    sp.add_argument("recording")
    sp.add_argument("--library", nargs="*", default=[])
    sp.add_argument("--threshold", type=float, default=0.6)
    sp.add_argument("--sensitivity", type=float, default=6.0)
    sp.add_argument("--offset", type=float, default=0.0, help="seconds added to every event time")
    sp.add_argument("-o", "--out")
    sp.set_defaults(fn=cmd_sounds)

    sp = sub.add_parser("kb", help="entity catalog -> detailed spec documents")
    sp.add_argument("action", choices=("init", "index", "build", "discover", "hotkeys", "demo"))
    sp.add_argument("paths", nargs="*", help="asset folders (index)")
    sp.add_argument("--project", default="project")
    sp.add_argument("--entity", help="entity id (hotkeys)")
    sp.add_argument("--window", type=float, default=10.0)
    sp.add_argument("--meters-per-stud", type=float, default=0.28)
    sp.set_defaults(fn=cmd_kb)

    sp = sub.add_parser("gaps", help="spec gap list -> measurement sessions -> results back into the list")
    sp.add_argument("action", choices=("plan", "status", "apply"))
    sp.add_argument("csv", help="gap list CSV (id, category, subcategory, item, unit, ..., fill_method, priority)")
    sp.add_argument("--project", default="project")
    sp.add_argument("--plan", help="measurement plan markdown with '## 세션 N — title' sections")
    sp.add_argument("--priority", default="P1,P2,P3", help="priorities to include (plan)")
    sp.add_argument("-o", "--out", help="output CSV (apply), default <csv>.measured.csv")
    sp.set_defaults(fn=cmd_gaps)

    sp = sub.add_parser("validate", help="check spec provenance and sample counts")
    sp.add_argument("--spec", required=True)
    sp.set_defaults(fn=cmd_validate)
    return p


def main(argv=None) -> int:
    a = build_parser().parse_args(argv)
    return a.fn(a)
