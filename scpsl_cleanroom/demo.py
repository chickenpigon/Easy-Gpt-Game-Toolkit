"""Synthetic demo sessions (made-up numbers) to show the pipeline end to end.

None of these values describe the real game - they only exercise each analysis
so you can see what the generated spec looks like before recording real play.
"""

from __future__ import annotations

import json
import math
import random
from pathlib import Path

FAKE = "# synthetic demo data - values are invented, not from the game\n"


def _write(path: Path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(FAKE + "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


def bouncing_ball(rng, t0, v0=4.0, growth=1.15, cap=30.0, room=6.0, hz=120, bounces=14):
    rows = [{"t": t0, "id": "scp018", "event": "throw", "tag": "scp018"}]
    x, d, t, n = 0.5, 1, t0, 0
    v = v0
    while n <= bounces:
        x += d * v / hz
        t += 1 / hz
        if x >= room or x <= 0:
            x = min(max(x, 0), room)
            d = -d
            n += 1
            v = min(v0 * growth ** n, cap)
        rows.append({"t": round(t, 4), "id": "scp018", "pos": [round(x, 4), 1.0, round(2 + 0.001 * rng.random(), 4)]})
    rows.append({"t": round(t + 0.01, 4), "id": "scp018", "event": "despawn", "tag": "scp018"})
    return rows


def grenade(rng, t0, inner=2.0, outer=8.0, peak=200.0):
    rows = [{"t": t0, "id": "p1", "event": "throw", "tag": "frag_grenade"},
            {"t": t0 + 5.0 + rng.uniform(-0.01, 0.01), "id": "p1", "event": "explode", "tag": "frag_grenade"}]
    for k in range(6):
        d = rng.uniform(0, 10)
        dmg = peak * min(max((outer - d) / (outer - inner), 0), 1)
        vid = f"dummy{k}"
        rows.append({"t": t0 + 4.9, "id": vid, "hp": 1000.0})
        rows.append({"t": t0 + 5.01, "id": vid, "hp": round(1000.0 - dmg + rng.gauss(0, 0.5), 2), "event": "hit",
                     "tag": "frag_grenade", "dist": round(d, 3)})
    return rows


def gate_force(rng, t0):
    j = lambda: rng.uniform(-0.02, 0.02)
    return [
        {"t": t0, "id": "096", "role": "Scp096", "event": "gate_force_start", "tag": "gate_a"},
        {"t": t0 + 0.05 + j(), "id": "096", "event": "anim:scp096_gate_pry", "tag": "gate_a"},
        {"t": t0 + 0.30 + j(), "id": "audio", "event": "sound:gate_force"},
        {"t": t0 + 1.0, "id": "gate_a", "tag": "gate_a", "state": "closed"},
        {"t": t0 + 2.1 + j(), "id": "gate_a", "tag": "gate_a", "state": "opening"},
        {"t": t0 + 3.3 + j(), "id": "gate_a", "tag": "gate_a", "state": "open"},
    ]


def walking(rng, t0, role="ClassD", walk=3.0, sprint=5.0, drain=-0.2, regen=0.25, delay=1.0, dt=0.1):
    rows, t, x, st = [], t0, 0.0, 1.0
    for state, speed, secs in (("walk", walk, 4.0), ("sprint", sprint, 4.0), ("idle", 0.0, 6.0)):
        k = 0.0
        while k < secs - 1e-9:
            if state == "sprint":
                st = max(0.0, st + drain * dt)
            elif state == "idle" and k >= delay:
                st = min(1.0, st + regen * dt)
            rows.append({"t": round(t, 3), "id": f"{role}-1", "role": role, "state": state,
                         "pos": [round(x, 4), 0.0, 0.0], "stamina": round(st, 5), "hp": 100})
            x += speed * dt
            t += dt
            k += dt
    return rows


def make_demo(project_dir: str | Path, seed=7) -> list[Path]:
    rng = random.Random(seed)
    s = Path(project_dir) / "sessions"
    files = []
    rows = []
    for i in range(5):
        rows += bouncing_ball(rng, 30.0 * i)
    files.append(s / "demo_scp018.jsonl")
    _write(files[-1], rows)
    rows = []
    for i in range(8):
        rows += grenade(rng, 20.0 * i)
    files.append(s / "demo_grenade.jsonl")
    _write(files[-1], rows)
    rows = []
    for i in range(5):
        rows += gate_force(rng, 30.0 * i)
    files.append(s / "demo_096_gate.jsonl")
    _write(files[-1], rows)
    rows = []
    for i in range(6):
        rows += walking(rng, 20.0 * i)
    files.append(s / "demo_classd.jsonl")
    _write(files[-1], rows)
    return files
