from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from agent_loop_system.tools.external_execution_history import (
    EXTERNAL_EXECUTION_HISTORY_FIELDS,
    ExternalExecutionHistoryError,
    read_external_execution_history,
)
from frontend.server import AppPaths, _external_explored_ids
from sim_tools.audit_case_map import audit_case_maps


def _record(case_id: str = "DEMO_001", **overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "case_id": case_id,
        "sheet": "demo",
        "target": "620C_W6830",
        "last_verified": "2026-08-17",
        "evidence_root": "D:/external-evidence",
        "evidence_paths": [f"batch/{case_id}/result.json"],
    }
    value.update(overrides)
    return value


def _write_lines(path: Path, *values: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(
            value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
            for value in values
        ) + "\n",
        encoding="utf-8",
    )


def _assert_error(
    path: Path,
    expected_code: str,
    *,
    expected_line: int | None,
    expected_case_id: str | None,
    expected_target: str | None = None,
) -> ExternalExecutionHistoryError:
    with pytest.raises(ExternalExecutionHistoryError) as caught:
        read_external_execution_history(path, expected_target=expected_target)
    error = caught.value
    assert error.code == expected_code
    assert error.path == path
    assert error.line_number == expected_line
    assert error.case_id == expected_case_id
    assert error.message
    assert str(path) in str(error)
    return error


def test_valid_ledger_preserves_file_order_and_skips_blank_lines(tmp_path: Path) -> None:
    ledger = tmp_path / "external_execution_history.jsonl"
    _write_lines(ledger, _record("DEMO_002"), "", _record("DEMO_001"))

    records = read_external_execution_history(ledger, expected_target="620C_W6830")

    assert [record.case_id for record in records] == ["DEMO_002", "DEMO_001"]
    assert records[0].sheet == "demo"
    assert records[0].evidence_paths == ("batch/DEMO_002/result.json",)
    assert EXTERNAL_EXECUTION_HISTORY_FIELDS == {
        "case_id",
        "sheet",
        "target",
        "last_verified",
        "evidence_root",
        "evidence_paths",
    }


def test_empty_ledger_is_valid(tmp_path: Path) -> None:
    ledger = tmp_path / "external_execution_history.jsonl"
    ledger.write_text("\n\n", encoding="utf-8")

    assert read_external_execution_history(ledger, expected_target="620C_W6830") == []


def test_missing_ledger_is_rejected(tmp_path: Path) -> None:
    ledger = tmp_path / "external_execution_history.jsonl"
    _assert_error(
        ledger,
        "external_ledger_missing",
        expected_line=None,
        expected_case_id=None,
    )


def test_invalid_json_is_rejected_with_line_number(tmp_path: Path) -> None:
    ledger = tmp_path / "external_execution_history.jsonl"
    _write_lines(ledger, _record(), "{not-json")
    _assert_error(
        ledger,
        "invalid_external_ledger_json",
        expected_line=2,
        expected_case_id=None,
    )


def test_non_object_record_is_rejected(tmp_path: Path) -> None:
    ledger = tmp_path / "external_execution_history.jsonl"
    _write_lines(ledger, ["not", "an", "object"])
    _assert_error(
        ledger,
        "external_ledger_schema_mismatch",
        expected_line=1,
        expected_case_id=None,
    )


def test_duplicate_json_member_is_rejected_instead_of_last_value_winning(
    tmp_path: Path,
) -> None:
    ledger = tmp_path / "external_execution_history.jsonl"
    duplicate = (
        '{"case_id":"DEMO_001","case_id":"DEMO_002","sheet":"demo",'
        '"target":"620C_W6830","last_verified":"2026-08-17",'
        '"evidence_root":"D:/external-evidence",'
        '"evidence_paths":["batch/DEMO_001/result.json"]}'
    )
    _write_lines(ledger, duplicate)
    _assert_error(
        ledger,
        "external_ledger_schema_mismatch",
        expected_line=1,
        expected_case_id=None,
    )


@pytest.mark.parametrize(
    "mutate,expected_case_id",
    [
        (lambda value: {key: item for key, item in value.items() if key != "sheet"}, "DEMO_001"),
        (lambda value: {**value, "unexpected": True}, "DEMO_001"),
        (
            lambda value: {
                ("module" if key == "sheet" else key): item
                for key, item in value.items()
            },
            "DEMO_001",
        ),
    ],
    ids=("missing-field", "extra-field", "wrong-field"),
)
def test_record_fields_must_match_exactly(
    tmp_path: Path,
    mutate,
    expected_case_id: str,
) -> None:
    ledger = tmp_path / "external_execution_history.jsonl"
    _write_lines(ledger, mutate(_record()))
    _assert_error(
        ledger,
        "external_ledger_schema_mismatch",
        expected_line=1,
        expected_case_id=expected_case_id,
    )


@pytest.mark.parametrize(
    "field,value,expected_code,expected_case_id",
    [
        ("case_id", " ", "external_ledger_case_id_missing", None),
        ("sheet", "", "external_ledger_fact_missing", "DEMO_001"),
        ("target", " ", "external_ledger_fact_missing", "DEMO_001"),
        ("last_verified", 20260817, "external_ledger_fact_missing", "DEMO_001"),
        ("evidence_root", None, "external_ledger_fact_missing", "DEMO_001"),
    ],
)
def test_scalar_fields_must_be_nonempty_strings(
    tmp_path: Path,
    field: str,
    value: object,
    expected_code: str,
    expected_case_id: str | None,
) -> None:
    ledger = tmp_path / "external_execution_history.jsonl"
    _write_lines(ledger, _record(**{field: value}))
    _assert_error(
        ledger,
        expected_code,
        expected_line=1,
        expected_case_id=expected_case_id,
    )


@pytest.mark.parametrize("value", ["2026-8-17", "2026-02-30"])
def test_last_verified_must_be_a_valid_iso_date(tmp_path: Path, value: str) -> None:
    ledger = tmp_path / "external_execution_history.jsonl"
    _write_lines(ledger, _record(last_verified=value))
    _assert_error(
        ledger,
        "external_ledger_date_invalid",
        expected_line=1,
        expected_case_id="DEMO_001",
    )


@pytest.mark.parametrize(
    "value",
    ["result.json", [], [" "], [7]],
    ids=("not-list", "empty-list", "blank-path", "non-string-path"),
)
def test_evidence_paths_must_be_nonempty_strings(tmp_path: Path, value: object) -> None:
    ledger = tmp_path / "external_execution_history.jsonl"
    _write_lines(ledger, _record(evidence_paths=value))
    _assert_error(
        ledger,
        "external_ledger_evidence_missing",
        expected_line=1,
        expected_case_id="DEMO_001",
    )


def test_duplicate_case_id_is_rejected(tmp_path: Path) -> None:
    ledger = tmp_path / "external_execution_history.jsonl"
    _write_lines(ledger, _record(), _record())
    _assert_error(
        ledger,
        "duplicate_external_ledger_case_id",
        expected_line=2,
        expected_case_id="DEMO_001",
    )


def test_expected_target_must_match_exactly(tmp_path: Path) -> None:
    ledger = tmp_path / "external_execution_history.jsonl"
    _write_lines(ledger, _record(target="6202_W5230"))
    _assert_error(
        ledger,
        "external_ledger_target_mismatch",
        expected_line=1,
        expected_case_id="DEMO_001",
        expected_target="620C_W6830",
    )


def test_frontend_and_audit_accept_and_reject_the_same_ledger(tmp_path: Path) -> None:
    paths = AppPaths.from_root(tmp_path)
    case_map_dir = paths.case_map / "620C_simulator_case_map"
    case_map_dir.mkdir(parents=True)
    (case_map_dir / "demo.json").write_text(
        json.dumps([{
            "case_id": "DEMO_001",
            "sheet": "demo",
            "setup": [],
            "actions": [],
            "collect": [],
            "verification_points": [],
            "unable": False,
        }]),
        encoding="utf-8",
    )
    ledger = case_map_dir / "external_execution_history.jsonl"
    _write_lines(ledger, _record())

    assert _external_explored_ids(paths) == {"DEMO_001"}
    with patch("sim_tools.audit_case_map._live_capabilities", return_value=(set(), set())):
        counts, issues = audit_case_maps(case_map_dir)
    assert counts["externally_explored"] == 1
    assert issues == []

    _write_lines(ledger, {**_record(), "unexpected": True})
    frontend_error = _assert_error(
        ledger,
        "external_ledger_schema_mismatch",
        expected_line=1,
        expected_case_id="DEMO_001",
        expected_target="620C_W6830",
    )
    with pytest.raises(ExternalExecutionHistoryError) as caught:
        _external_explored_ids(paths)
    assert caught.value.code == frontend_error.code
    with patch("sim_tools.audit_case_map._live_capabilities", return_value=(set(), set())):
        _counts, issues = audit_case_maps(case_map_dir)
    assert [issue.code for issue in issues] == [frontend_error.code]
