"""Command line entry point: ``python -m scpsl_cleanroom <command> ...``."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import guard, roblox, spec as specmod, telemetry, unity_scene
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

    sp = sub.add_parser("validate", help="check spec provenance and sample counts")
    sp.add_argument("--spec", required=True)
    sp.set_defaults(fn=cmd_validate)
    return p


def main(argv=None) -> int:
    a = build_parser().parse_args(argv)
    return a.fn(a)
