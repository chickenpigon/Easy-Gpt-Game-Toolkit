"""Black-box analysis of recorded gameplay telemetry.

Telemetry is JSON Lines, one sample per line. Only ``t`` and ``id`` are required:

    {"t": 12.31, "id": "p1", "role": "ClassD", "state": "walk", "pos": [x, y, z],
     "hp": 100, "stamina": 0.82, "event": "door_open_start", "tag": "LCZ_A"}

- ``pos`` in meters (game world units), ``t`` in seconds.
- ``event`` names ending in ``_start`` / ``_end`` are paired into durations.
- ``event: "hit"`` with ``tag`` (damage source) marks the sample right after a hit.

The same format is produced by ``roblox/TelemetryRecorder.luau`` so the port
can be measured with the exact same analyzers and compared against the spec.
"""

from __future__ import annotations

import json
import math
from collections import defaultdict
from pathlib import Path

from .measure import Measurement, linear_fit, mode_bin, summarize


def load(path: str | Path) -> list[dict]:
    samples = []
    with open(path, encoding="utf-8") as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if "TLM {" in line:  # copied from Roblox Output (may carry a timestamp prefix)
                line = line[line.index("TLM {") + 4:]
            if not line or line.startswith("#"):
                continue
            try:
                s = json.loads(line)
            except json.JSONDecodeError as e:
                raise ValueError(f"{path}:{lineno}: {e}") from e
            if "t" not in s or "id" not in s:
                raise ValueError(f"{path}:{lineno}: sample needs 't' and 'id'")
            samples.append(s)
    samples.sort(key=lambda s: (str(s["id"]), float(s["t"])))
    return samples


def _by_entity(samples):
    groups = defaultdict(list)
    for s in samples:
        groups[str(s["id"])].append(s)
    for g in groups.values():
        g.sort(key=lambda s: float(s["t"]))
    return groups


def _key(s, *fields):
    return ".".join(str(s.get(f, "any")) for f in fields)


def speeds(samples, source: str, max_dt=0.5, min_speed=0.2, max_speed=40.0) -> dict[str, Measurement]:
    """Horizontal and vertical speed per role.state, from consecutive position samples."""
    horiz, vert = defaultdict(list), defaultdict(list)
    for g in _by_entity(samples).values():
        g = [s for s in g if "pos" in s]
        for a, b in zip(g, g[1:]):
            dt = float(b["t"]) - float(a["t"])
            if dt <= 0 or dt > max_dt or _key(a, "role", "state") != _key(b, "role", "state"):
                continue
            dx, dy, dz = (b["pos"][i] - a["pos"][i] for i in range(3))
            h = math.hypot(dx, dz) / dt
            if min_speed <= h <= max_speed:  # drop idle samples and teleports
                horiz[_key(a, "role", "state")].append(h)
            if abs(dy) / dt >= min_speed:
                vert[_key(a, "role", "state")].append(dy / dt)
    out = {}
    for k, v in horiz.items():
        out[f"speed.{k}"] = summarize(v, "m/s", f"observed:{source}", "horizontal, idle excluded")
    for k, v in vert.items():
        if len(v) >= 3:
            out[f"vertical_speed.{k}"] = summarize(v, "m/s", f"observed:{source}", "signed, + is up")
    return out


def rates(samples, field_name: str, source: str, min_points=3, min_duration=0.5) -> dict[str, Measurement]:
    """Rate of change of a scalar (stamina, hp, ...) per role.state, plus regen delay."""
    slopes = defaultdict(list)
    delays = defaultdict(list)
    for g in _by_entity(samples).values():
        g = [s for s in g if field_name in s]
        seg: list[dict] = []
        for s in g + [None]:
            if s is not None and seg and _key(s, "role", "state") == _key(seg[0], "role", "state"):
                seg.append(s)
                continue
            if len(seg) >= min_points:
                ts = [float(x["t"]) for x in seg]
                if ts[-1] - ts[0] >= min_duration:
                    _measure_segment(seg, ts, field_name, slopes, delays)
            seg = [s] if s is not None else []
    out = {}
    for k, v in slopes.items():
        out[f"rate.{field_name}.{k}"] = summarize(v, f"{field_name}/s", f"observed:{source}", "linear fit per segment")
    for k, v in delays.items():
        out[f"regen_delay.{field_name}.{k}"] = summarize(v, "s", f"observed:{source}", "state entry -> first increase")
    return out


def _measure_segment(seg, ts, field_name, slopes, delays):
    ys = [float(x[field_name]) for x in seg]
    key = _key(seg[0], "role", "state")
    first_up = next((i for i in range(1, len(ys)) if ys[i] > ys[i - 1] + 1e-9), None)
    if first_up is not None and ys[-1] > ys[0]:
        delays[key].append(ts[first_up - 1] - ts[0])
    start = first_up - 1 if first_up else 0
    # Fit only while the value is not pinned at its min/max.
    lo, hi = min(ys), max(ys)
    idx = [i for i in range(start, len(ys)) if lo < ys[i] < hi or i == start]
    if len(idx) < 2:
        return
    try:
        slope, _, r2 = linear_fit([ts[i] for i in idx], [ys[i] for i in idx])
    except ValueError:
        return
    if r2 >= 0.9 and slope != 0:
        slopes[key].append(slope)


def durations(samples, source: str) -> dict[str, Measurement]:
    """Pair ``X_start`` / ``X_end`` events per entity and tag."""
    open_at: dict[tuple, float] = {}
    found = defaultdict(list)
    for s in sorted(samples, key=lambda s: float(s["t"])):
        ev = s.get("event")
        if not isinstance(ev, str):
            continue
        for suffix in ("_start", "_end"):
            if ev.endswith(suffix):
                base = ev[: -len(suffix)]
                k = (str(s["id"]), str(s.get("tag", "")), base)
                if suffix == "_start":
                    open_at[k] = float(s["t"])
                elif k in open_at:
                    found[base].append(float(s["t"]) - open_at.pop(k))
    return {f"duration.{k}": summarize(v, "s", f"observed:{source}") for k, v in found.items()}


def intervals(samples, source: str, min_count=3) -> dict[str, Measurement]:
    """Minimum gap between repeats of the same event by one entity (spam it to find cooldowns)."""
    last: dict[tuple, float] = {}
    gaps = defaultdict(list)
    for s in sorted(samples, key=lambda s: float(s["t"])):
        ev = s.get("event")
        if not isinstance(ev, str) or ev.endswith(("_start", "_end")) or ev == "hit":
            continue
        k = (str(s["id"]), ev)
        if k in last:
            gaps[ev].append(float(s["t"]) - last[k])
        last[k] = float(s["t"])
    out = {}
    for ev, v in gaps.items():
        if len(v) >= min_count:
            m = summarize(v, "s", f"observed:{source}", "use min as cooldown upper bound when spammed")
            m.extra["min"] = round(min(v), 4)
            out[f"interval.{ev}"] = m
    return out


def damage(samples, source: str, resolution=0.5) -> dict[str, Measurement]:
    """HP lost per hit, grouped by hit tag; also hits-to-kill from full HP."""
    per_tag = defaultdict(list)
    for g in _by_entity(samples).values():
        prev = None
        for s in g:
            if prev is not None and s.get("event") == "hit" and "hp" in s and "hp" in prev:
                lost = float(prev["hp"]) - float(s["hp"])
                if lost > 0:
                    per_tag[str(s.get("tag", "unknown"))].append(lost)
            if "hp" in s:
                prev = s
    out = {}
    for tag, v in per_tag.items():
        m = summarize(v, "hp", f"observed:{source}", "hp before - hp after per hit")
        m.extra["mode"] = mode_bin(v, resolution)
        out[f"damage.{tag}"] = m
    return out


def analyze(path: str | Path) -> dict[str, Measurement]:
    return analyze_samples(load(path), Path(path).name)


def analyze_samples(samples: list[dict], src: str) -> dict[str, Measurement]:
    out: dict[str, Measurement] = {}
    out.update(speeds(samples, src))
    scalar_fields = {k for s in samples for k, v in s.items()
                     if isinstance(v, (int, float)) and not isinstance(v, bool) and k not in ("t",)}
    for f in sorted(scalar_fields):
        out.update(rates(samples, f, src))
    out.update(durations(samples, src))
    out.update(intervals(samples, src))
    out.update(damage(samples, src))
    return out
