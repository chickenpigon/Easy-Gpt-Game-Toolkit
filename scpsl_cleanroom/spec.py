"""Spec documents: the only artifact that crosses from the observing team to the porting team."""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from .measure import ALLOWED_PROVENANCE, Measurement
from .roblox import ConvertOptions, to_luau

SPEC_VERSION = 1


def new_spec(title: str, target: str) -> dict:
    return {"spec_version": SPEC_VERSION, "title": title, "target": target, "values": {}, "assets": {}}


def load_spec(path: str | Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def save_spec(spec: dict, path: str | Path) -> None:
    Path(path).write_text(json.dumps(spec, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def merge_measurements(spec: dict, measurements: dict[str, Measurement], overwrite=False) -> list[str]:
    """Add measurements; existing keys are kept unless ``overwrite`` (returns skipped keys)."""
    skipped = []
    for key, m in sorted(measurements.items()):
        if m.kind not in ALLOWED_PROVENANCE:
            raise ValueError(f"{key}: provenance '{m.provenance}' is not one of {ALLOWED_PROVENANCE}")
        if key in spec["values"] and not overwrite:
            prev = Measurement.from_dict(spec["values"][key])
            if prev.kind == "design":  # deliberate choices are never replaced by data
                skipped.append(key)
                continue
            # Pool observations from several sessions, weighting by sample count.
            if prev.kind == m.kind == "observed" and prev.unit == m.unit:
                total = prev.n + m.n
                m = Measurement(
                    value=round((prev.value * prev.n + m.value * m.n) / total, 4),
                    unit=m.unit,
                    provenance=f"{prev.provenance}+{m.provenance.split(':', 1)[1]}",
                    n=total,
                    p10=_pick(min, prev.p10, m.p10),
                    p90=_pick(max, prev.p90, m.p90),
                    note=m.note,
                    extra={**prev.extra, **m.extra},
                )
        spec["values"][key] = m.to_dict()
    return skipped


def _pick(fn, *xs):
    xs = [x for x in xs if x is not None]
    return fn(xs) if xs else None


def set_design_value(spec: dict, key: str, value: float, unit: str, note: str) -> None:
    spec["values"][key] = Measurement(value=value, unit=unit, provenance=f"design:{note or 'chosen'}").to_dict()


def validate(spec: dict) -> list[str]:
    problems = []
    for key, d in spec.get("values", {}).items():
        try:
            m = Measurement.from_dict(d)
        except TypeError as e:
            problems.append(f"{key}: malformed ({e})")
            continue
        if m.kind not in ALLOWED_PROVENANCE:
            problems.append(f"{key}: provenance '{m.provenance}' not allowed")
        if m.kind == "observed" and m.n < 5:
            problems.append(f"{key}: only {m.n} samples, record more")
    return problems


# --- outputs for the porting team -------------------------------------------

def _to_studs(value: float, unit: str, opt: ConvertOptions) -> tuple[float, str]:
    if unit.startswith("m/") or unit == "m":
        return round(value / opt.meters_per_stud, 4), unit.replace("m", "studs", 1)
    return value, unit


def to_markdown(spec: dict) -> str:
    lines = [f"# {spec['title']}", "", f"Target: {spec['target']}", "",
             "| key | value | unit | p10 – p90 | n | provenance | note |",
             "|---|---|---|---|---|---|---|"]
    for key, d in sorted(spec["values"].items()):
        m = Measurement.from_dict(d)
        spread = f"{m.p10} – {m.p90}" if m.p10 is not None else ""
        lines.append(f"| `{key}` | {m.value} | {m.unit} | {spread} | {m.n} | {m.provenance} | {m.note} |")
    for name, items in spec.get("assets", {}).items():
        lines += ["", f"## Asset data: {name}", "", f"{len(items)} entries (see spec.json)."]
    return "\n".join(lines) + "\n"


def to_luau_config(spec: dict, opt: ConvertOptions) -> str:
    """Nested Luau table: ``speed.ClassD.walk`` -> ``Config.speed.ClassD.walk``. Distances in studs."""
    root: dict = {}
    for key, d in sorted(spec["values"].items()):
        m = Measurement.from_dict(d)
        value, _unit = _to_studs(m.value, m.unit, opt)
        node = root
        parts = key.split(".")
        for p in parts[:-1]:
            nxt = node.setdefault(p, {})
            if not isinstance(nxt, dict):  # a leaf already sits here; keep both
                nxt = node[p] = {"value": nxt}
            node = nxt
        node[parts[-1]] = value
    return (
        "--!strict\n"
        f"-- Generated from spec '{spec['title']}'. Distances converted to studs "
        f"({opt.meters_per_stud} m/stud).\n"
        f"return {to_luau(root)}\n"
    )


def compare(spec: dict, port: dict[str, Measurement], tolerance=0.05) -> tuple[list[dict], bool]:
    """Compare measurements taken from the Roblox port (in meters) against the spec."""
    rows, ok = [], True
    for key, d in sorted(spec["values"].items()):
        want = Measurement.from_dict(d)
        got = port.get(key)
        if got is None:
            rows.append({"key": key, "status": "missing", "spec": want.value})
            continue
        if want.value == 0:
            err = abs(got.value)
        else:
            err = abs(got.value - want.value) / abs(want.value)
        within_spread = want.p10 is not None and want.p10 <= got.value <= want.p90
        status = "ok" if err <= tolerance or within_spread else "off"
        ok = ok and status == "ok"
        rows.append({"key": key, "status": status, "spec": want.value, "port": got.value,
                     "error_pct": round(err * 100, 2), "unit": want.unit})
    return rows, ok


def assets_payload(scan_result: dict, converted: dict) -> dict:
    return {
        "lights": converted["lights"],
        "lighting": converted["lighting"],
        "materials": converted["materials"],
        "unity_raw_lights": [asdict(l) for l in scan_result["lights"]],
    }
