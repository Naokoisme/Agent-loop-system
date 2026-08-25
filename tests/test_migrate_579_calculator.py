from __future__ import annotations

from collections import Counter
import json
import tempfile
import unittest
from pathlib import Path

from sim_tools.migrate_579_calculator import (
    DEFAULT_SOURCE,
    MigrationError,
    migrate_file,
    migrate_plan,
)


ROOT = Path(__file__).resolve().parents[1]
CHECKED_IN_MAP = ROOT / "case_map" / "579_case_map" / "计算器.json"


def _minimal_source() -> dict:
    plans = []
    batch_sizes = (5, 5, 5, 5, 5, 5, 4, 4)
    case_number = 1
    for batch_number, size in enumerate(batch_sizes, start=1):
        for _ in range(size):
            plans.append({
                "case_no": f"CALC-{case_number:03d}",
                "batch_id": f"CALC-B{batch_number:02d}",
                "setup": [{
                    "type": "step",
                    "args": {"source_plan": {
                        "type": "switch_window",
                        "window_name": "表盘",
                    }},
                }, {"type": "wait", "timeout": 0.1}],
                "steps": [{
                    "type": "step",
                    "args": {"source_plan": {
                        "type": "switch_window",
                        "window_name": "菜单",
                    }},
                }, {
                    "type": "step",
                    "args": {"source_plan": {
                        "type": "click",
                        "x": 51,
                        "y": 156,
                    }},
                }, {"type": "assert", "expected": "计算器显示 1"}],
                "teardown": [{
                    "type": "step",
                    "args": {"source_plan": {
                        "type": "switch_window",
                        "window_name": "表盘",
                    }},
                }],
                "expected": "计算器显示 1",
                "meta": {"business_actions": ["进入计算器", "点击 1"]},
            })
            case_number += 1
    return {"kind": "test_source", "plans": plans}


class Migrate579CalculatorTests(unittest.TestCase):
    def test_migration_is_blocked_after_ota_entry_failure_and_forbidden_commands_are_gone(self) -> None:
        migrated = migrate_plan(_minimal_source(), source_sha256="ABC")
        cases = migrated["cases"]
        self.assertEqual(len(cases), 38)
        self.assertEqual(
            Counter(case["batch_id"] for case in cases),
            Counter({
                "CALC-B01": 5,
                "CALC-B02": 5,
                "CALC-B03": 5,
                "CALC-B04": 5,
                "CALC-B05": 5,
                "CALC-B06": 5,
                "CALC-B07": 4,
                "CALC-B08": 4,
            }),
        )
        self.assertEqual(migrated["migration_version"], 2)
        self.assertEqual(migrated["execution_gate"]["status"], "BLOCKED")
        self.assertEqual(
            migrated["execution_gate"]["reason_code"],
            "BUTTON_PRESS_UNSUPPORTED_AFTER_OTA",
        )
        self.assertTrue(
            migrated["execution_gate"]["menu_to_watchface"]["effect_verified"]
        )
        self.assertFalse(
            migrated["execution_gate"]["watchface_to_menu"]["effect_verified"]
        )
        self.assertTrue(all(case["mapping_status"] == "BLOCKED" for case in cases))
        self.assertTrue(all(case["unable"] is True for case in cases))
        self.assertTrue(all(
            case["block_reason_code"] == "BUTTON_PRESS_UNSUPPORTED_AFTER_OTA"
            for case in cases
        ))
        self.assertTrue(all(case["collect"] == [] for case in cases))
        self.assertTrue(all(case["actions"][0] == ":BUTTON_PRESS:1,1,0" for case in cases))
        serialized = json.dumps(migrated, ensure_ascii=False).casefold()
        self.assertNotIn("08/96", serialized)
        self.assertNotIn("switch_window", serialized)

    def test_unknown_source_step_fails_instead_of_being_skipped(self) -> None:
        payload = _minimal_source()
        payload["plans"][0]["steps"][1]["args"]["source_plan"]["type"] = "mystery"
        with self.assertRaisesRegex(MigrationError, "unsupported source step type"):
            migrate_plan(payload)

    @unittest.skipUnless(DEFAULT_SOURCE.is_file(), "legacy 579 calculator source is unavailable")
    def test_checked_in_map_exactly_matches_the_legacy_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "calculator.json"
            regenerated = migrate_file(DEFAULT_SOURCE, output)
        checked_in = json.loads(CHECKED_IN_MAP.read_text(encoding="utf-8"))
        self.assertEqual(regenerated, checked_in)


if __name__ == "__main__":
    unittest.main()
