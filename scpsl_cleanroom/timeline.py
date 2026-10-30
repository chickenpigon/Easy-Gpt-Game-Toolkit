"""Mine event logs for timing rules, action sequences and state machines.

Events can come from any source merged into one stream: telemetry, the video
annotator, or sound detection (``event: "sound:<clip>"``). Nothing about the
game is assumed - rules are discovered from what repeatedly happens.
"""

from __future__ import annotations

import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from .measure import percentile


def events(samples) -> list[dict]:
    return sorted((s for s in samples if isinstance(s.get("event"), str)), key=lambda s: float(s["t"]))


def _scope_key(s, scope: str):
    if scope == "id":
        return str(s.get("id"))
    if scope == "tag":
        return str(s.get("tag", ""))
    return ""


@dataclass
class Rule:
    cause: str
    effect: str
    count: int  # occurrences of cause
    support: int  # causes followed by effect within the window
    latencies: list[float] = field(repr=False, default_factory=list)

    @property
    def confidence(self) -> float:
        return self.support / self.count if self.count else 0.0

    def to_dict(self) -> dict:
        lat = self.latencies
        return {
            "cause": self.cause, "effect": self.effect, "count": self.count, "support": self.support,
            "confidence": round(self.confidence, 3),
            "latency_median": round(statistics.median(lat), 4),
            "latency_p10": round(percentile(lat, 10), 4),
            "latency_p90": round(percentile(lat, 90), 4),
            "latency_stdev": round(statistics.pstdev(lat), 4),
        }


def mine_rules(samples, window=10.0, scope="any", min_count=3, min_confidence=0.8) -> list[Rule]:
    """"A is followed by B within ``window`` s" rules, with latency statistics.

    ``scope``: "any" (global stream), "id" (same entity) or "tag" (same object, e.g. door).
    Only the first B after each A counts, so the latency is the reaction time.
    """
    evs = events(samples)
    streams: dict[str, list[dict]] = defaultdict(list)
    for e in evs:
        streams[_scope_key(e, scope)].append(e)
    count = Counter(e["event"] for e in evs)
    found: dict[tuple, list[float]] = defaultdict(list)
    for stream in streams.values():
        for i, a in enumerate(stream):
            ta = float(a["t"])
            seen = set()
            for b in stream[i + 1:]:
                dt = float(b["t"]) - ta
                if dt > window:
                    break
                if b["event"] == a["event"] or b["event"] in seen:
                    continue
                seen.add(b["event"])
                found[(a["event"], b["event"])].append(dt)
    rules = []
    for (ca, eb), lat in found.items():
        r = Rule(ca, eb, count[ca], len(lat), lat)
        if r.count >= min_count and r.confidence >= min_confidence:
            rules.append(r)
    rules.sort(key=lambda r: (-r.confidence, -r.support, statistics.median(r.latencies)))
    return rules


def sequence(samples, trigger: str, window=10.0, scope="any", tag=None, min_confidence=0.6) -> dict:
    """Ordered timeline of what reliably follows ``trigger`` (e.g. 096 forcing a gate):
    offsets from the trigger for animations, sounds and state changes.

    Only triggers carrying ``tag`` count when given. With ``scope`` "id"/"tag", effects must
    share the trigger's id/tag - except events without that key (sounds, annotations),
    which are global and always considered.
    """
    evs = events(samples)
    triggers = [e for e in evs if e["event"] == trigger and (tag is None or str(e.get("tag")) == str(tag))]
    found: dict[str, list[float]] = defaultdict(list)
    for trig in triggers:
        t0 = float(trig["t"])
        key = None if scope == "any" else scope
        seen = set()
        for e in evs:
            dt = float(e["t"]) - t0
            if dt < 0 or e is trig:
                continue
            if dt > window:
                break
            if e["event"] == trigger or e["event"] in seen:
                continue
            if key and key in e and str(e.get(key)) != str(trig.get(key)):
                continue
            seen.add(e["event"])
            found[e["event"]].append(dt)
    steps = []
    for name, lat in found.items():
        conf = len(lat) / len(triggers)
        if conf >= min_confidence:
            steps.append({"event": name, "offset_median": round(statistics.median(lat), 4),
                          "offset_p10": round(percentile(lat, 10), 4), "offset_p90": round(percentile(lat, 90), 4),
                          "confidence": round(conf, 3), "n": len(lat)})
    steps.sort(key=lambda s: s["offset_median"])
    return {"trigger": trigger, "occurrences": len(triggers), "steps": steps}


def context(samples, event: str, fields=("role", "state", "tag")) -> dict[str, dict[str, int]]:
    """What was true when ``event`` happened (preconditions)."""
    out: dict[str, Counter] = defaultdict(Counter)
    for e in events(samples):
        if e["event"] == event:
            for f in fields:
                if f in e:
                    out[f][str(e[f])] += 1
    return {k: dict(v) for k, v in out.items()}


def state_machine(samples, key="state", trigger_window=0.3, **sel) -> dict:
    """Transitions of a per-entity state field, dwell times and the events that trigger them."""
    by_id = defaultdict(list)
    for s in samples:
        if all(v is None or str(s.get(k)) == str(v) for k, v in sel.items()):
            by_id[str(s["id"])].append(s)
    transitions: dict[tuple, dict] = {}
    dwell = defaultdict(list)
    for track in by_id.values():
        track.sort(key=lambda s: float(s["t"]))
        cur, since = None, None
        recent: list[dict] = []
        for s in track:
            if isinstance(s.get("event"), str):
                recent.append(s)
            if key not in s:
                continue
            st = str(s[key])
            t = float(s["t"])
            if cur is not None and st != cur:
                tr = transitions.setdefault((cur, st), {"count": 0, "triggers": Counter()})
                tr["count"] += 1
                for e in recent:
                    if t - float(e["t"]) <= trigger_window:
                        tr["triggers"][e["event"]] += 1
                dwell[cur].append(t - since)
            if st != cur:
                cur, since = st, t
                recent = [e for e in recent if t - float(e["t"]) <= trigger_window]
    return {
        "transitions": [
            {"from": a, "to": b, "count": d["count"],
             "triggers": {e: n for e, n in d["triggers"].most_common() if n / d["count"] >= 0.5}}
            for (a, b), d in sorted(transitions.items(), key=lambda kv: -kv[1]["count"])
        ],
        "dwell": {k: {"median": round(statistics.median(v), 4), "min": round(min(v), 4),
                      "max": round(max(v), 4), "n": len(v)} for k, v in dwell.items()},
    }


def _mid(name: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in name)


def mermaid_states(sm: dict) -> str:
    lines = ["stateDiagram-v2"]
    for tr in sm["transitions"]:
        label = ", ".join(tr["triggers"]) or f"x{tr['count']}"
        lines.append(f"    {_mid(tr['from'])} --> {_mid(tr['to'])}: {label}")
    return "\n".join(lines)


def mermaid_sequence(seq: dict) -> str:
    lines = ["gantt", "    dateFormat x", "    axisFormat %S.%L s", f"    section {seq['trigger']}"]
    for st in seq["steps"]:
        start = int(round(st["offset_median"] * 1000))
        lines.append(f"    {st['event'].replace(':', ' ')} : {start}, {start + 50}")
    return "\n".join(lines)
