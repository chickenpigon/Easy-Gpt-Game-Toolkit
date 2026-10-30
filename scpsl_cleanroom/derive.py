"""Turn raw telemetry into (x, y) series that formula discovery can fit.

Selectors used by every extractor (all optional): ``id``, ``role``, ``tag``,
``event`` - a sample must match all given ones.
"""

from __future__ import annotations

import math
from collections import defaultdict


def select(samples, **sel) -> list[dict]:
    out = []
    for s in samples:
        if all(v is None or str(s.get(k)) == str(v) for k, v in sel.items()):
            out.append(s)
    return out


def _dist(a, b) -> float:
    return math.dist(a, b)


def episodes(samples, gap=1.0) -> list[list[dict]]:
    """Split one entity's samples wherever time jumps by more than ``gap`` seconds."""
    out: list[list[dict]] = []
    for s in sorted(samples, key=lambda s: float(s["t"])):
        if out and float(s["t"]) - float(out[-1][-1]["t"]) <= gap:
            out[-1].append(s)
        else:
            out.append([s])
    return out


def bounce_series(samples, id=None, tag=None, angle_deg=45.0, min_speed=0.05, gap=1.0):
    """Speed of a bouncing object per bounce: x = bounce index (0 = before first bounce), y = speed.

    Bounces come from ``event: "bounce"`` samples when present, otherwise from sharp
    direction changes of the position track.
    """
    points: list[tuple[float, float]] = []
    by_id = defaultdict(list)
    for s in select(samples, id=id, tag=tag):
        if "pos" in s or s.get("event") == "bounce":
            by_id[str(s["id"])].append(s)
    cos_lim = math.cos(math.radians(angle_deg))
    for track in by_id.values():
        for ep in episodes(track, gap):
            pos = [s for s in ep if "pos" in s]
            marks = sorted(float(s["t"]) for s in ep if s.get("event") == "bounce")
            vel = []  # (t_mid, vector, speed)
            for a, b in zip(pos, pos[1:]):
                dt = float(b["t"]) - float(a["t"])
                if dt <= 0:
                    continue
                v = [(b["pos"][i] - a["pos"][i]) / dt for i in range(3)]
                sp = math.sqrt(sum(c * c for c in v))
                if sp >= min_speed:
                    vel.append(((float(a["t"]) + float(b["t"])) / 2, v, sp))
            dts = sorted(float(b["t"]) - float(a["t"]) for a, b in zip(pos, pos[1:]))
            step = dts[len(dts) // 2] if dts else 0.0
            if not marks:
                # A bounce between two samples bends the track twice (into and out of the
                # straddling sample): merge turns closer than ~2.5 samples into one bounce.
                for (t1, v1, s1), (t2, v2, s2) in zip(vel, vel[1:]):
                    if sum(p * q for p, q in zip(v1, v2)) / (s1 * s2) < cos_lim:
                        t = (t1 + t2) / 2
                        if marks and t - marks[-1] <= 2.5 * step:
                            marks[-1] = (marks[-1] + t) / 2
                        else:
                            marks.append(t)
            edges = [-math.inf, *marks, math.inf]
            for i in range(len(edges) - 1):
                # skip samples straddling a bounce: their displacement is folded
                seg = sorted(sp for t, _, sp in vel
                             if edges[i] + 1.5 * step < t < edges[i + 1] - 1.5 * step)
                if seg:
                    points.append((float(i), seg[len(seg) // 2]))
    return points


def falloff_pairs(samples, tag=None, event="hit", dist_field="dist", value_field="damage"):
    """(distance, value) per hit. Distance from ``dist`` or ``src_pos``→``pos``; value from
    ``damage`` or the hp drop since the entity's previous sample."""
    points = []
    by_id = defaultdict(list)
    for s in samples:
        by_id[str(s["id"])].append(s)
    for track in by_id.values():
        track.sort(key=lambda s: float(s["t"]))
        prev_hp = None
        for s in track:
            if s.get("event") == event and (tag is None or str(s.get("tag")) == str(tag)):
                d = s.get(dist_field)
                if d is None and "src_pos" in s and "pos" in s:
                    d = _dist(s["src_pos"], s["pos"])
                v = s.get(value_field)
                if v is None and prev_hp is not None and "hp" in s:
                    v = prev_hp - float(s["hp"])
                if d is not None and v is not None:
                    points.append((float(d), float(v)))
            if "hp" in s:
                prev_hp = float(s["hp"])
    return points


def field_pairs(samples, x, y, **sel):
    """Any two numeric fields from the same sample (``x`` may also be ``"t"``)."""
    return [(float(s[x]), float(s[y])) for s in select(samples, **sel)
            if isinstance(s.get(x), (int, float)) and isinstance(s.get(y), (int, float))]


def time_series(samples, y="speed", gap=1.0, start_event=None, start_tag=None, **sel):
    """y over time since episode start, or since the last ``start_event`` (optionally only
    one carrying ``start_tag``; points before it are skipped).

    ``y="speed"`` derives speed from positions; otherwise reads the field.
    """
    points = []
    by_id = defaultdict(list)
    for s in select(samples, **sel):
        by_id[str(s["id"])].append(s)
    for track in by_id.values():
        for ep in episodes(track, gap):
            t0 = None if start_event else float(ep[0]["t"])
            prev = None
            for s in ep:
                if start_event and s.get("event") == start_event and (
                        start_tag is None or str(s.get("tag")) == str(start_tag)):
                    t0 = float(s["t"])
                if t0 is None:
                    continue
                if y == "speed":
                    if "pos" in s and prev is not None:
                        dt = float(s["t"]) - float(prev["t"])
                        if dt > 0:
                            points.append((float(s["t"]) - t0 - dt / 2, _dist(s["pos"], prev["pos"]) / dt))
                    if "pos" in s:
                        prev = s
                elif isinstance(s.get(y), (int, float)):
                    points.append((float(s["t"]) - t0, float(s[y])))
    return points


def jumps(samples, window=2.0, **sel):
    """Per ``jump`` event: height above take-off and time until back at take-off height."""
    out = []
    by_id = defaultdict(list)
    for s in select(samples, **sel):
        by_id[str(s["id"])].append(s)
    for track in by_id.values():
        track.sort(key=lambda s: float(s["t"]))
        for i, s in enumerate(track):
            if s.get("event") != "jump":
                continue
            pos = [x for x in track[i:] if "pos" in x and float(x["t"]) - float(s["t"]) <= window]
            if len(pos) < 3:
                continue
            y0 = pos[0]["pos"][1]
            peak = max(p["pos"][1] for p in pos)
            land = next((float(p["t"]) for p in pos[1:] if p["pos"][1] <= y0 and p["pos"][1] < peak), None)
            out.append({"height": peak - y0, "airtime": None if land is None else land - float(pos[0]["t"])})
    return out


def counts_between(samples, event, reset, **sel):
    """How many ``event`` happen between consecutive ``reset`` events (e.g. shots per magazine)."""
    out = []
    by_id = defaultdict(list)
    for s in select(samples, **sel):
        by_id[str(s["id"])].append(s)
    for track in by_id.values():
        n, armed = 0, False
        for s in sorted(track, key=lambda s: float(s["t"])):
            if s.get("event") == reset:
                if armed and n:
                    out.append(n)
                n, armed = 0, True
            elif s.get("event") == event:
                n += 1
    return out


def field_deltas(samples, field, event, before=1.0, after=1.0, **sel):
    """Change of ``field`` across ``event``: last value up to ``before`` s earlier vs the
    first value from the event up to ``after`` s later."""
    out = []
    by_id = defaultdict(list)
    for s in samples:
        by_id[str(s["id"])].append(s)
    for track in by_id.values():
        track.sort(key=lambda s: float(s["t"]))
        for i, s in enumerate(track):
            if s.get("event") != event or not all(v is None or str(s.get(k)) == str(v) for k, v in sel.items()):
                continue
            t = float(s["t"])
            prev = [x for x in track[:i] if field in x and t - float(x["t"]) <= before]
            nxt = [x for x in track[i:] if field in x and 0 <= float(x["t"]) - t <= after]
            if prev and nxt:
                out.append(float(nxt[-1][field]) - float(prev[-1][field]))
    return out
