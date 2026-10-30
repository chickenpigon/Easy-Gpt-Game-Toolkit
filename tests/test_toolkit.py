import json
import math
import tempfile
import unittest
from pathlib import Path

from scpsl_cleanroom import guard, roblox, spec as specmod, telemetry, unity_scene
from scpsl_cleanroom.cli import main

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"


def synthetic_samples(walk=3.0, sprint=5.0, drain=-0.2, regen=0.25, regen_delay=1.0, dt=0.1):
    """Made-up numbers: the analyzers must recover exactly what was put in."""
    s, t, x, stamina = [], 0.0, 0.0, 1.0
    for state, speed, secs in (("walk", walk, 3.0), ("sprint", sprint, 4.0), ("idle", 0.0, 6.0)):
        state_t = 0.0
        while state_t < secs - 1e-9:
            if state == "sprint":
                stamina = max(0.0, stamina + drain * dt)
            elif state == "idle" and state_t >= regen_delay:
                stamina = min(1.0, stamina + regen * dt)
            s.append({"t": round(t, 3), "id": "p1", "role": "ClassD", "state": state,
                      "pos": [round(x, 4), 0.0, 0.0], "stamina": round(stamina, 5)})
            x += speed * dt
            t += dt
            state_t += dt
    hp = 100.0
    for i in range(5):
        s.append({"t": 20 + i, "id": "p2", "hp": hp})
        hp -= 12.5
        s.append({"t": 20 + i + 0.05, "id": "p2", "hp": hp, "event": "hit", "tag": "pistol"})
    for i in range(4):
        s.append({"t": 30 + i * 3, "id": "p3", "event": "door_open_start", "tag": "A"})
        s.append({"t": 30 + i * 3 + 1.2, "id": "p3", "event": "door_open_end", "tag": "A"})
        s.append({"t": 30 + i * 3 + 0.1, "id": "p3", "event": "use_item"})
    return s


def write_jsonl(samples, path):
    Path(path).write_text("\n".join(json.dumps(x) for x in samples), encoding="utf-8")


class UnitySceneTest(unittest.TestCase):
    def test_lights_world_transform(self):
        scene = unity_scene.Scene.load(EXAMPLES / "sample_room.unity")
        lights = {l.name: l for l in scene.lights()}
        lamp = lights["CeilingLamp"]
        self.assertEqual(lamp.type, "spot")
        self.assertEqual(lamp.path, "Room/CeilingLamp")
        # Parent at x=10 rotated 90deg about Y: local (0, 2.8, 1) -> world (11, 2.8, 0)
        for got, want in zip(lamp.position, (11.0, 2.8, 0.0)):
            self.assertAlmostEqual(got, want, places=4)
        self.assertEqual(lamp.shadows, "soft")
        self.assertEqual(lights["AlarmLight"].extra["m_LightUnit"], 0)

    def test_render_settings_and_material(self):
        rs = unity_scene.Scene.load(EXAMPLES / "sample_room.unity").render_settings()
        self.assertTrue(rs.fog)
        self.assertEqual(rs.fog_mode, "linear")
        mats = unity_scene.read_material(EXAMPLES / "emissive_panel.mat")
        self.assertEqual(mats[0].emission, (1.5, 1.4, 1.2))


class RobloxConvertTest(unittest.TestCase):
    def test_position_and_spot_direction(self):
        opt = roblox.ConvertOptions(meters_per_stud=0.5)
        self.assertEqual(roblox.to_roblox_position((1, 2, 3), opt), (2, 4, -6))
        # Unity spot pointing straight down (+90 about X) -> Roblox LookVector should be down too.
        q = roblox.to_roblox_rotation((math.sin(math.pi / 4), 0, 0, math.cos(math.pi / 4)))
        m = roblox.quat_to_matrix(q)
        look = (-m[0][2], -m[1][2], -m[2][2])
        for got, want in zip(look, (0, -1, 0)):
            self.assertAlmostEqual(got, want, places=5)

    def test_light_range_clamped(self):
        scene = unity_scene.Scene.load(EXAMPLES / "sample_room.unity")
        alarm = next(l for l in scene.lights() if l.name == "AlarmLight")
        rec = roblox.convert_light(alarm, roblox.ConvertOptions())
        self.assertEqual(rec["class"], "PointLight")
        self.assertEqual(rec["range"], roblox.ROBLOX_MAX_LIGHT_RANGE)
        self.assertTrue(rec["range_clamped"])

    def test_luau_emission(self):
        self.assertEqual(roblox.to_luau({"a": [1, 2], "b c": True}), '{\n\ta = {1, 2},\n\t["b c"] = true,\n}')


class TelemetryTest(unittest.TestCase):
    def setUp(self):
        self.samples = synthetic_samples()

    def test_speeds(self):
        ms = telemetry.speeds(self.samples, "x")
        self.assertAlmostEqual(ms["speed.ClassD.walk"].value, 3.0, places=2)
        self.assertAlmostEqual(ms["speed.ClassD.sprint"].value, 5.0, places=2)

    def test_stamina_rates_and_delay(self):
        ms = telemetry.rates(self.samples, "stamina", "x")
        self.assertAlmostEqual(ms["rate.stamina.ClassD.sprint"].value, -0.2, places=2)
        self.assertAlmostEqual(ms["rate.stamina.ClassD.idle"].value, 0.25, places=2)
        self.assertAlmostEqual(ms["regen_delay.stamina.ClassD.idle"].value, 1.0, delta=0.11)

    def test_events_and_damage(self):
        self.assertAlmostEqual(telemetry.durations(self.samples, "x")["duration.door_open"].value, 1.2)
        self.assertEqual(telemetry.intervals(self.samples, "x")["interval.use_item"].extra["min"], 3)
        dmg = telemetry.damage(self.samples, "x")["damage.pistol"]
        self.assertEqual(dmg.value, 12.5)
        self.assertEqual(dmg.n, 5)

    def test_roblox_output_prefix(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "out.txt"
            p.write_text('12:00:01.123  TLM {"t": 0, "id": "1"}\n', encoding="utf-8")
            self.assertEqual(telemetry.load(p)[0]["id"], "1")


class SpecTest(unittest.TestCase):
    def test_pooling_design_and_compare(self):
        s = specmod.new_spec("t", "target")
        a = telemetry.speeds(synthetic_samples(walk=3.0), "a.jsonl")
        b = telemetry.speeds(synthetic_samples(walk=3.2), "b.jsonl")
        specmod.merge_measurements(s, a)
        specmod.merge_measurements(s, b)
        pooled = s["values"]["speed.ClassD.walk"]
        self.assertAlmostEqual(pooled["value"], 3.1, places=2)
        self.assertIn("a.jsonl+b.jsonl", pooled["provenance"])

        specmod.set_design_value(s, "speed.ClassD.sprint", 5.5, "m/s", "feel")
        skipped = specmod.merge_measurements(s, telemetry.speeds(synthetic_samples(), "c.jsonl"))
        self.assertIn("speed.ClassD.sprint", skipped)

        rows, ok = specmod.compare(s, telemetry.speeds(synthetic_samples(walk=3.1, sprint=5.5), "port"))
        self.assertTrue(ok, rows)
        rows, ok = specmod.compare(s, telemetry.speeds(synthetic_samples(walk=4.0, sprint=5.5), "port"))
        self.assertFalse(ok)

    def test_luau_config_in_studs(self):
        s = specmod.new_spec("t", "target")
        specmod.set_design_value(s, "speed.ClassD.walk", 2.8, "m/s", "x")
        lua = specmod.to_luau_config(s, roblox.ConvertOptions(meters_per_stud=0.28))
        self.assertIn("walk = 10.0", lua)


class GuardTest(unittest.TestCase):
    def test_flags_code(self):
        text = "// Decompiled with ILSpy\nprivate float walkSpeed = 3.9f;\nusing UnityEngine;\n"
        rules = {h[1] for h in guard.scan_text(text)}
        self.assertTrue({"decompiler header", "C# member declaration", "C# float literal",
                         "C# using directive"} <= rules)

    def test_clean_spec_passes(self):
        s = specmod.new_spec("t", "target")
        specmod.merge_measurements(s, telemetry.analyze_samples(synthetic_samples(), "x"))
        self.assertEqual(guard.scan_text(specmod.to_markdown(s)), [])


class CliTest(unittest.TestCase):
    def test_end_to_end(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            write_jsonl(synthetic_samples(), d / "sl.jsonl")
            spec = str(d / "spec.json")
            self.assertEqual(main(["assets", str(EXAMPLES), "-o", str(d / "out"), "--spec", spec]), 0)
            self.assertEqual(main(["observe", str(d / "sl.jsonl"), "--spec", spec]), 0)
            self.assertEqual(main(["build", "--spec", spec, "-o", str(d / "out")]), 0)
            self.assertTrue((d / "out" / "GameConfig.luau").read_text().startswith("--!strict"))
            self.assertIn("SpotLight", (d / "out" / "LightingData.luau").read_text())
            self.assertEqual(main(["compare", str(d / "sl.jsonl"), "--spec", spec]), 0)
            self.assertEqual(main(["guard", str(d / "out")]), 0)


if __name__ == "__main__":
    unittest.main()
