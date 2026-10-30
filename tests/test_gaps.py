import csv
import json
import tempfile
import unittest
from pathlib import Path

from scpsl_cleanroom import gaps
from scpsl_cleanroom.cli import main

HEADER = ["id", "category", "subcategory", "item", "unit", "status", "current_value", "current_source",
          "fill_method", "priority", "notes"]
ROWS = [
    ["G0005", "Roles", "Class-D", "sneak speed", "m/s", "PARTIAL", "2.5 candidate", "", "play", "P1", ""],
    ["G0006", "Roles", "Class-D", "jump height", "m", "MISSING", "", "", "play", "P1", ""],
    ["G0015", "Roles", "Class-D", "HP bar colour", "rgb", "PARTIAL", "orange", "", "play", "P2", ""],
    ["G0347", "Weapons", "COM-15", "reload time empty vs tactical", "s", "MISSING", "", "", "play", "P1", ""],
    ["G0351", "Weapons", "COM-15", "hip spread", "deg", "PARTIAL", "2 candidate", "", "play", "P1", ""],
    ["G0528", "Grenades", "Frag", "falloff curve shape", "curve", "MISSING", "", "", "play", "P1", ""],
    ["G0574", "Items", "Adrenaline", "HP regen", "HP/s", "PARTIAL", "4 HP/s", "", "play", "P2", ""],
    ["G0558", "Grenades", "MicroHID", "movement while charging", "rule", "MISSING", "", "", "play", "P2", ""],
    ["G0016", "Roles", "Class-D", "spawn inventory", "items", "PARTIAL", "empty", "", "user", "P1", ""],
    ["G0001", "Roles", "Class-D", "max HP", "HP", "PRESENT", "100", "", "wiki", "P1", ""],
]
PLAN = """# plan
## 세션 1 — 이동
- 준비: 복도
|id|항목|
|-|-|
|G0005|sneak|
|G0006|jump|
|G0015|colour|
## 세션 2 — 총기
|G0347|reload|
|G0351|spread|
|G0528|frag|
## 사용자 기억으로 확인할 질문
|1|G0016|inv|
"""


def fill(sheet: Path, updates: dict):
    with open(sheet, encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    used = {}
    for r in rows:
        key = (r["id"], r["variant"])
        queue = updates.get(key, [])
        i = used.get(key, 0)
        if i < len(queue):
            r.update(queue[i])
            used[key] = i + 1
    with open(sheet, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, gaps.SHEET_COLUMNS)
        w.writeheader()
        w.writerows(rows)


class GapsTest(unittest.TestCase):
    def test_kinds_and_variants(self):
        rows = [dict(zip(HEADER, r)) for r in ROWS]
        kinds = {r["id"]: gaps.kind_of(r) for r in rows}
        self.assertEqual(kinds["G0005"], "speed")
        self.assertEqual(kinds["G0528"], "curve")
        self.assertEqual(kinds["G0558"], "observation")
        self.assertEqual(gaps.variants_of(rows[3]), ["empty", "tactical"])

    def test_plan_record_apply(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            src = d / "gaps.csv"
            with open(src, "w", encoding="utf-8", newline="") as f:
                w = csv.writer(f)
                w.writerow(HEADER)
                w.writerows(ROWS)
            (d / "plan.md").write_text(PLAN, encoding="utf-8")
            proj = d / "proj"
            self.assertEqual(main(["gaps", "plan", str(src), "--plan", str(d / "plan.md"), "--project", str(proj)]), 0)
            m = proj / "measurements"
            self.assertTrue((m / "session_01.csv").exists())
            tasks = json.loads((m / "session_02.tasks.json").read_text(encoding="utf-8"))
            self.assertEqual(tasks["tasks"][0]["variants"], ["empty", "tactical"])
            self.assertIn("기타", (m / "README.md").read_text(encoding="utf-8"))  # G0574/G0558 not in plan

            fill(m / "session_01.csv", {
                ("CAL_CELL", ""): [{"value": "3.0"}],
                ("G0005", ""): [{"cells": "4", "time_s": "5.0"}, {"distance_m": "12", "time_s": "4.9"},
                                {"cells": "4", "time_s": "5.1"}],
                ("G0006", ""): [{"distance_m": "0.9"}, {"distance_m": "1.0"}, {"distance_m": "0.95"}],
                ("G0015", ""): [{"value": "#FF8E00"}, {"value": "#FF8F02"}],
            })
            curve = [{"x": str(x), "y": str(max(0, min(200, 200 * (8 - x) / 6)))} for x in range(0, 11)]
            fill(m / "session_02.csv", {("G0528", ""): curve})
            events = [{"t": 1.0, "id": "p", "event": "G0347_start", "variant": "empty"},
                      {"t": 3.5, "id": "p", "event": "G0347_end", "variant": "empty"},
                      {"t": 5.0, "id": "p", "event": "G0347_start", "variant": "tactical"},
                      {"t": 6.8, "id": "p", "event": "G0347_end", "variant": "tactical"}]
            events += [{"t": 10 + i, "id": "p", "event": "measure", "gap": "G0351", "deg": 1.5 + 0.1 * i} for i in range(5)]
            (proj / "sessions" / "session_02.annotations.jsonl").write_text(
                "\n".join(json.dumps(e) for e in events), encoding="utf-8")
            q = (proj / "notes" / "user_questions.yaml").read_text(encoding="utf-8")
            (proj / "notes" / "user_questions.yaml").write_text(
                q.replace('answer: ""', 'answer: "keycard only"', 1), encoding="utf-8")

            out = d / "out.csv"
            self.assertEqual(main(["gaps", "apply", str(src), "--project", str(proj), "-o", str(out)]), 0)
            with open(out, encoding="utf-8-sig") as f:
                got = {r["id"]: r for r in csv.DictReader(f)}
            self.assertEqual(got["G0005"]["measured_value"], "2.4")
            self.assertEqual(got["G0006"]["measured_value"], "0.95")
            self.assertEqual(got["G0015"]["measured_value"], "#FF8E01")
            self.assertEqual(got["G0347"]["measured_value"], "empty=2.5; tactical=1.8")
            self.assertEqual(got["G0351"]["measured_value"], "1.7")
            self.assertEqual(got["G0528"]["measured_value"], "y = 200 for x<=2, linear to 0 at x=8")
            self.assertEqual(got["G0016"]["measured_value"], "keycard only")
            self.assertTrue(got["G0016"]["measured_source"].startswith("user:"))
            self.assertEqual(got["G0001"]["measured_value"], "")
            report = (proj / "spec" / "MEASURED.md").read_text(encoding="utf-8")
            self.assertIn("G0351", report)  # 1.7 vs candidate 2 -> -15%
            self.assertIn("G0574", report.split("아직 남은 항목")[1])


if __name__ == "__main__":
    unittest.main()
