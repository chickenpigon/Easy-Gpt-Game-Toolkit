import json
import random
import tempfile
import unittest
import wave
from pathlib import Path

import numpy as np

from scpsl_cleanroom import demo, derive, fit, kb, media, timeline, ui_layout, unity_scene
from scpsl_cleanroom.cli import main

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"


def write_wav(path, x, sr=8000):
    with wave.open(str(path), "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(sr)
        f.writeframes((np.clip(x, -1, 1) * 32767).astype("<i2").tobytes())


class FitTest(unittest.TestCase):
    def test_recovers_capped_geometric_with_round_params(self):
        rng = np.random.default_rng(1)
        x = np.arange(0, 20.0)
        y = np.minimum(4 * 1.15 ** x, 30) + rng.normal(0, 0.1, 20)
        best = fit.discover(x, y, fit.GROWTH)[0]
        self.assertEqual(best.model, "geometric_capped")
        self.assertEqual(best.nice_params, {"a": 4.0, "b": 1.15, "c": 30.0})

    def test_recovers_inner_outer_falloff(self):
        rng = np.random.default_rng(2)
        d = rng.uniform(0, 10, 60)
        dmg = 200 * np.clip((8 - d) / 6, 0, 1) + rng.normal(0, 1, 60)
        best = fit.discover(d, dmg, fit.FALLOFF)[0]
        self.assertEqual(best.model, "inner_outer_falloff")
        self.assertEqual(best.equation(), "y = 200 for x<=2, linear to 0 at x=8")

    def test_prefers_simple_model(self):
        x = np.arange(1, 30.0)
        self.assertEqual(fit.discover(x, 3.7 + 0.25 * x)[0].equation(), "y = 3.7 + 0.25*x")


class DeriveTest(unittest.TestCase):
    def test_bounce_series(self):
        rows = demo.bouncing_ball(random.Random(0), 0.0)
        pts = derive.bounce_series(rows, id="scp018")
        speeds = dict(pts)
        self.assertAlmostEqual(speeds[0.0], 4.0, delta=0.05)
        self.assertAlmostEqual(speeds[5.0], 4 * 1.15 ** 5, delta=0.05)
        self.assertEqual(len(pts), 15)

    def test_counts_and_deltas(self):
        s = [{"t": i, "id": "p", "event": "shoot"} for i in range(1, 8)]
        s += [{"t": 0, "id": "p", "event": "reload_end"}, {"t": 10, "id": "p", "event": "reload_end"}]
        self.assertEqual(derive.counts_between(s, "shoot", "reload_end"), [7])
        h = [{"t": 0, "id": "p", "hp": 40}, {"t": 1, "id": "p", "event": "use_end"}, {"t": 1.5, "id": "p", "hp": 65}]
        self.assertEqual(derive.field_deltas(h, "hp", "use_end"), [25.0])


class TimelineTest(unittest.TestCase):
    def setUp(self):
        rng = random.Random(3)
        self.samples = kb.with_state_events([r for i in range(5) for r in demo.gate_force(rng, 30.0 * i)])

    def test_sequence_after_trigger(self):
        seq = timeline.sequence(self.samples, "gate_force_start", window=5, scope="tag")
        names = [s["event"] for s in seq["steps"]]
        self.assertEqual(names[:2], ["anim:scp096_gate_pry", "sound:gate_force"])
        self.assertIn("state:open", names)
        self.assertEqual(seq["occurrences"], 5)

    def test_rules(self):
        rules = {(r.cause, r.effect): r for r in timeline.mine_rules(self.samples, window=5)}
        r = rules[("anim:scp096_gate_pry", "sound:gate_force")]
        self.assertAlmostEqual(r.to_dict()["latency_median"], 0.25, delta=0.05)

    def test_state_machine(self):
        sm = timeline.state_machine(self.samples, tag="gate_a")
        pairs = {(t["from"], t["to"]) for t in sm["transitions"]}
        self.assertIn(("opening", "open"), pairs)


class AssetTest(unittest.TestCase):
    def test_ui_layout(self):
        guids = ui_layout.guid_index([EXAMPLES])
        trees = ui_layout.extract(unity_scene.Scene.load(EXAMPLES / "hud.prefab"), guids)
        texts = {t["unity_path"]: t for t in ui_layout.texts(trees)}
        hp = texts["HudCanvas/HealthText"]
        self.assertEqual(hp["Font"], "Bauhaus.ttf")
        self.assertEqual(hp["AnchorPoint"], [0.0, 1.0])
        self.assertEqual(hp["Position"], [0.0625, 0.0, 0.94444, 0.0])
        bar = texts["HudCanvas/TopBar"]
        self.assertEqual((bar["TextXAlignment"], bar["TextYAlignment"]), ("Center", "Center"))
        self.assertEqual(bar["Size"], [1.0, 0.0, 0.03704, 0.0])

    def test_animator_and_clip(self):
        guids = ui_layout.guid_index([EXAMPLES])
        ctrl = media.read_animator(EXAMPLES / "scp096.controller", guids)[0]
        states = {s["name"]: s for s in ctrl["layers"][0]["states"]}
        self.assertEqual(states["GatePry"]["motion"], "scp096_gate_pry.anim")
        self.assertEqual(states["GatePry"]["transitions"][0]["exit_time"], 0.95)
        self.assertIn("Run --> GatePry: ForceGate true", media.mermaid_animator(ctrl))
        clip = media.read_animation(EXAMPLES / "scp096_gate_pry.anim")[0]
        self.assertEqual(clip["length"], 2.4)
        self.assertEqual(clip["events"][0]["string"], "gate_force")

    def test_sound_match(self):
        sr = 8000
        rng = np.random.default_rng(0)
        t = np.arange(int(0.6 * sr)) / sr
        chirp = 0.5 * np.sin(2 * np.pi * (300 + 1500 * t) * t)
        rec = 0.05 * rng.normal(size=sr * 6)
        for at, gain in ((1.0, 0.6), (4.0, 0.3)):
            i = int(at * sr)
            rec[i:i + len(chirp)] += chirp * gain
        with tempfile.TemporaryDirectory() as d:
            write_wav(Path(d) / "rec.wav", rec)
            (Path(d) / "lib").mkdir()
            write_wav(Path(d) / "lib" / "beep.wav", chirp)
            found = media.match_sounds(Path(d) / "rec.wav", [Path(d) / "lib"], offset=10)
        self.assertEqual([(e["event"], round(e["t"], 1)) for e in found], [("sound:beep", 11.0), ("sound:beep", 14.0)])


class KnowledgeBaseTest(unittest.TestCase):
    def test_demo_project_end_to_end(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(main(["kb", "demo", "--project", d]), 0)
            self.assertEqual(main(["kb", "index", str(EXAMPLES), "--project", d]), 0)
            self.assertEqual(main(["kb", "build", "--project", d]), 0)
            self.assertEqual(main(["kb", "discover", "--project", d]), 0)
            res = json.loads((Path(d) / "spec" / "spec.json").read_text(encoding="utf-8"))
            bounce = res["scp018"]["speed_per_bounce"]
            self.assertEqual(bounce["status"], "answered")
            self.assertEqual(bounce["formula"]["model"], "geometric")
            self.assertAlmostEqual(bounce["formula"]["params"]["b"], 1.15, places=2)
            self.assertEqual(res["frag_grenade"]["damage_falloff"]["formula"]["model"], "inner_outer_falloff")
            steps = [s["event"] for s in res["Scp096"]["gate_force"]["sequence"]["steps"]]
            self.assertEqual(steps[:2], ["anim:scp096_gate_pry", "sound:gate_force"])
            self.assertEqual(res["ClassD"]["walk_speed"]["value"]["value"], 3.0)
            self.assertEqual(res["hud"]["texts"]["n"], 2)
            for f in ("index.md", "PLAN.md", "DISCOVERED.md", "GameSpec.luau", "plots/scp018__speed_per_bounce.svg"):
                self.assertTrue((Path(d) / "spec" / f).exists(), f)
            self.assertIn("speed_per_bounce", (Path(d) / "spec" / "GameSpec.luau").read_text(encoding="utf-8"))

    def test_sessions_do_not_interleave(self):
        with tempfile.TemporaryDirectory() as d:
            p = kb.Project(d)
            p.init()
            (p.sessions_dir / "a.jsonl").write_text('{"t": 1, "id": "x", "event": "A"}\n', encoding="utf-8")
            (p.sessions_dir / "a.sounds.jsonl").write_text('{"t": 1.5, "id": "audio", "event": "B"}\n', encoding="utf-8")
            (p.sessions_dir / "b.jsonl").write_text('{"t": 1.2, "id": "x", "event": "C"}\n', encoding="utf-8")
            seq = timeline.sequence(p.samples(), "A", window=5, min_confidence=0.5)
            self.assertEqual([s["event"] for s in seq["steps"]], ["B"])

    def test_hotkeys_cover_duration_pairs(self):
        keys = kb.hotkeys(kb.Project("does-not-exist"), "medkit")["keys"]
        events = {k["event"] for k in keys}
        self.assertTrue({"use_start", "use_end"} <= events)


if __name__ == "__main__":
    unittest.main()
