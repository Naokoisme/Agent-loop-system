from __future__ import annotations

import json
import unittest
from pathlib import Path

from agent_loop_system.tools.command_protocol import normalize_command
from agent_loop_system.tools.external_execution_history import (
    ExternalExecutionRecord,
    read_external_execution_history,
)


CASE_MAP_ROOT = Path(__file__).resolve().parents[1] / "case_map"
TARGETS = {
    "620C_simulator_case_map": {
        "profile": "620C_W6830",
        "solidified": 0,
    },
    "6202_case_map": {
        "profile": "6202_W5230",
        "solidified": 63,
    },
    "6202_simulator_case_map": {
        "profile": "6202_W5230_SIMULATOR",
        "solidified": 110,
    },
}
EXECUTION_FIELDS = ("setup", "actions", "collect", "verification_points")
SOURCE_FIELDS = (
    "case_id",
    "sheet",
    "priority",
    "precondition_text",
    "steps_text",
    "expected_text",
)


def _module_files(directory_name: str) -> list[Path]:
    return sorted((CASE_MAP_ROOT / directory_name).glob("*.json"))


def _raw_cases(path: Path) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    rows = data.get("cases", data) if isinstance(data, dict) else data
    if not isinstance(rows, list):
        raise AssertionError(f"{path}: cases 必须是数组")
    return rows


def _cases(directory_name: str) -> dict[str, dict]:
    result: dict[str, dict] = {}
    for path in _module_files(directory_name):
        for item in _raw_cases(path):
            case_id = str(item.get("case_id") or "").strip()
            if not case_id:
                raise AssertionError(f"{path}: case_id 不能为空")
            if case_id in result:
                raise AssertionError(f"{directory_name}: case_id 重复: {case_id}")
            result[case_id] = item
    return result


def _ledger(directory_name: str) -> dict[str, ExternalExecutionRecord]:
    path = CASE_MAP_ROOT / directory_name / "external_execution_history.jsonl"
    records = read_external_execution_history(
        path,
        expected_target=TARGETS[directory_name]["profile"],
    )
    return {record.case_id: record for record in records}


class CaseMapDataContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if not any((CASE_MAP_ROOT / target).is_dir() for target in TARGETS):
            raise unittest.SkipTest("框架纯净基线中未挂载业务 case_map 目录，自动跳过业务用例契约校验")
    def test_module_files_do_not_keep_legacy_execution_gates(self) -> None:
        legacy_fields = {"supported", "execution_supported", "unavailable_reason"}
        for directory_name in TARGETS:
            for path in _module_files(directory_name):
                data = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    self.assertFalse(
                        legacy_fields.intersection(data),
                        f"{directory_name}:{path.name}",
                    )

    def test_each_target_contains_the_same_3164_case_ids(self) -> None:
        case_sets: list[set[str]] = []
        for directory_name in TARGETS:
            with self.subTest(target=directory_name):
                self.assertEqual(len(_module_files(directory_name)), 40)
                cases = _cases(directory_name)
                self.assertEqual(len(cases), 3164)
                case_sets.append(set(cases))
                for case_id, item in cases.items():
                    for field in SOURCE_FIELDS:
                        self.assertIn(field, item, f"{directory_name}:{case_id}:{field}")

        self.assertTrue(case_sets)
        self.assertTrue(all(case_ids == case_sets[0] for case_ids in case_sets[1:]))

    def test_external_ledgers_are_minimal_unique_and_target_local(self) -> None:
        for directory_name, expected in TARGETS.items():
            with self.subTest(target=directory_name):
                cases = _cases(directory_name)
                ledger = _ledger(directory_name)
                # 外部探索账本对所有目标都可自然增长；这里锁结构、目标和归属，
                # 不把包括当前空账本的 620C 探索数量误当成永久常量。
                self.assertLessEqual(set(ledger), set(cases))
                for case_id, item in ledger.items():
                    self.assertEqual(item.case_id, case_id)
                    self.assertEqual(item.target, expected["profile"])
                    self.assertEqual(item.sheet, cases[case_id]["sheet"])
                    self.assertTrue(item.last_verified, case_id)
                    self.assertTrue(item.evidence_root, case_id)
                    self.assertTrue(item.evidence_paths, case_id)
                    self.assertTrue(
                        all(path.strip() for path in item.evidence_paths),
                        case_id,
                    )

    def test_promotion_baseline_is_locked_while_exploration_can_grow(self) -> None:
        for directory_name, expected in TARGETS.items():
            with self.subTest(target=directory_name):
                cases = _cases(directory_name)
                ledger_ids = set(_ledger(directory_name))
                promoted_ids = {
                    case_id
                    for case_id, item in cases.items()
                    if item.get("mapping_status") == "PROMOTED"
                }
                self.assertEqual(len(promoted_ids), expected["solidified"])
                self.assertLessEqual(promoted_ids, ledger_ids)

    def test_unsolidified_cases_do_not_keep_fixed_steps(self) -> None:
        for directory_name in TARGETS:
            for case_id, item in _cases(directory_name).items():
                if item.get("mapping_status") == "PROMOTED":
                    continue
                with self.subTest(target=directory_name, case_id=case_id):
                    self.assertNotIn("mapping_status", item)
                    for field in EXECUTION_FIELDS:
                        self.assertEqual(item.get(field), [], field)
                    self.assertFalse(item.get("unable", False))
                    self.assertEqual(str(item.get("note") or ""), "")

    def test_promoted_steps_are_real_simulator_mappings(self) -> None:
        cases = _cases("6202_simulator_case_map")
        promoted = [item for item in cases.values() if item.get("mapping_status") == "PROMOTED"]
        self.assertEqual(len(promoted), 110)

        forbidden = {"SCREENSHOT_PRINT", "GUI_TREE"}
        for item in promoted:
            case_id = item["case_id"]
            with self.subTest(case_id=case_id):
                self.assertFalse(item.get("unable", False))
                self.assertTrue(item.get("actions"), case_id)
                self.assertTrue(item.get("verification_points"), case_id)
                for phase in ("setup", "actions", "collect"):
                    for wire in item.get(phase, []):
                        self.assertTrue(
                            wire.startswith("srv_quick_cmd send TOP5STEP:"),
                            f"{case_id}:{phase}:{wire}",
                        )
                        self.assertTrue(wire.endswith(";"), f"{case_id}:{phase}:{wire}")
                        command_name = normalize_command(wire)[1:].partition(":")[0]
                        self.assertNotIn(command_name, forbidden, case_id)

    def test_hardware_promoted_steps_are_real_mappings(self) -> None:
        cases = _cases("6202_case_map")
        promoted = [item for item in cases.values() if item.get("mapping_status") == "PROMOTED"]
        self.assertEqual(len(promoted), TARGETS["6202_case_map"]["solidified"])

        for item in promoted:
            case_id = item["case_id"]
            with self.subTest(case_id=case_id):
                self.assertFalse(item.get("unable", False))
                self.assertTrue(item.get("actions"), case_id)
                self.assertTrue(item.get("verification_points"), case_id)
                checkpoint_triggers = sum(
                    normalize_command(wire)[1:].partition(":")[0]
                    in {"HOST_SCREENSHOT", "GUI_TREE"}
                    for field in ("setup", "actions", "collect")
                    for wire in item.get(field, [])
                )
                self.assertLessEqual(
                    checkpoint_triggers,
                    len(item.get("verification_points", [])),
                    case_id,
                )

    def test_620c_has_no_solidified_steps(self) -> None:
        cases = _cases("620C_simulator_case_map")
        self.assertTrue(
            all(item.get("mapping_status") != "PROMOTED" for item in cases.values())
        )
        self.assertTrue(
            all(
                item.get(field) == []
                for item in cases.values()
                for field in EXECUTION_FIELDS
            )
        )


if __name__ == "__main__":
    unittest.main()
