from __future__ import annotations

import json
import unittest
from pathlib import Path

from agent_loop_system.tools.command_protocol import normalize_command


CASE_MAP_ROOT = Path(__file__).resolve().parents[1] / "case_map"
TARGETS = {
    "620C_simulator_case_map": {
        "profile": "620C_W6830",
        "explored": 0,
        "solidified": 0,
    },
    "6202_case_map": {
        "profile": "6202_W5230",
        "explored": 11,
        "solidified": 0,
    },
    "6202_simulator_case_map": {
        "profile": "6202_W5230_SIMULATOR",
        "explored": 563,
        "solidified": 98,
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
LEDGER_FIELDS = {
    "case_id",
    "sheet",
    "target",
    "last_verified",
    "evidence_root",
    "evidence_paths",
}


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


def _ledger(directory_name: str) -> dict[str, dict]:
    path = CASE_MAP_ROOT / directory_name / "external_execution_history.jsonl"
    if not path.is_file():
        raise AssertionError(f"缺少外部探索账本: {path}")
    result: dict[str, dict] = {}
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        item = json.loads(line)
        if not isinstance(item, dict):
            raise AssertionError(f"{path}:{line_number}: 记录必须是对象")
        case_id = str(item.get("case_id") or "").strip()
        if not case_id:
            raise AssertionError(f"{path}:{line_number}: case_id 不能为空")
        if case_id in result:
            raise AssertionError(f"{path}:{line_number}: case_id 重复: {case_id}")
        result[case_id] = item
    return result


class CaseMapDataContractTest(unittest.TestCase):
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
                self.assertEqual(len(ledger), expected["explored"])
                self.assertLessEqual(set(ledger), set(cases))
                for case_id, item in ledger.items():
                    self.assertEqual(set(item), LEDGER_FIELDS, case_id)
                    self.assertEqual(item["case_id"], case_id)
                    self.assertEqual(item["target"], expected["profile"])
                    self.assertEqual(item["sheet"], cases[case_id]["sheet"])
                    self.assertTrue(str(item["last_verified"]).strip(), case_id)
                    self.assertTrue(str(item["evidence_root"]).strip(), case_id)
                    self.assertIsInstance(item["evidence_paths"], list, case_id)
                    self.assertTrue(item["evidence_paths"], case_id)
                    self.assertTrue(
                        all(str(path).strip() for path in item["evidence_paths"]),
                        case_id,
                    )

    def test_exact_maturity_baseline_is_locked(self) -> None:
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
                self.assertEqual(len(cases) - len(ledger_ids), 3164 - expected["explored"])
                self.assertEqual(
                    len(ledger_ids - promoted_ids),
                    expected["explored"] - expected["solidified"],
                )

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
        self.assertEqual(len(promoted), 98)

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

    def test_hardware_and_620c_have_no_solidified_steps(self) -> None:
        for directory_name in ("6202_case_map", "620C_simulator_case_map"):
            with self.subTest(target=directory_name):
                cases = _cases(directory_name)
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
