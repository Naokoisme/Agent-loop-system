from __future__ import annotations

from pathlib import Path

import pytest

from agent_loop_system.case_management import CaseManagementRepository


PROJECT = {
    "project_id": "579_O2",
    "allowed_platforms": ["579"],
    "allowed_targets": ["579.o2"],
    "default_platform": "579",
    "case_catalog": {"type": "manifest_579"},
}


def payload(case_id: str, expected: str = "页面正确显示") -> dict:
    return {
        "case_id": case_id,
        "sheet": "设置",
        "priority": "P1",
        "steps_text": "1. 打开设置",
        "expected_text": expected,
        "applicable_platforms": ["579"],
    }


def test_preview_is_read_only_and_commit_requires_token_and_sha(tmp_path: Path) -> None:
    repository = CaseManagementRepository(tmp_path / "cases.sqlite3")
    preview = repository.preview_import(
        PROJECT, [payload("SET-001")], source_filename="cases.xlsx",
        source_sha256="A" * 64,
    )
    assert preview["new_count"] == 1
    assert repository.list_cases("579_O2") == []

    with pytest.raises(ValueError, match="IMPORT_PREVIEW_EXPIRED"):
        repository.commit_import(
            PROJECT, batch_id=preview["batch_id"], preview_token="wrong",
            source_sha256=preview["source_sha256"],
        )
    with pytest.raises(ValueError, match="IMPORT_SOURCE_CHANGED"):
        repository.commit_import(
            PROJECT, batch_id=preview["batch_id"], preview_token=preview["preview_token"],
            source_sha256="B" * 64,
        )

    result = repository.commit_import(
        PROJECT, batch_id=preview["batch_id"], preview_token=preview["preview_token"],
        source_sha256=preview["source_sha256"],
    )
    assert result["imported_count"] == 1
    assert repository.get_case("579_O2", "SET-001")["source_type"] == "EXCEL"
    with pytest.raises(ValueError, match="IMPORT_ALREADY_COMMITTED"):
        repository.commit_import(
            PROJECT, batch_id=preview["batch_id"], preview_token=preview["preview_token"],
            source_sha256=preview["source_sha256"],
        )


def test_explicit_conflict_strategies_skip_or_create_revision(tmp_path: Path) -> None:
    repository = CaseManagementRepository(tmp_path / "cases.sqlite3")
    repository.create_case(PROJECT, payload("SET-001", "旧预期"))

    skipped = repository.preview_import(
        PROJECT, [payload("SET-001", "新预期")], source_filename="skip.xlsx",
        source_sha256="C" * 64, conflict_strategy="SKIP",
    )
    result = repository.commit_import(
        PROJECT, batch_id=skipped["batch_id"], preview_token=skipped["preview_token"],
        source_sha256=skipped["source_sha256"],
    )
    assert result["skipped_count"] == 1
    assert repository.get_case("579_O2", "SET-001")["expected_text"] == "旧预期"

    revised = repository.preview_import(
        PROJECT, [payload("SET-001", "新预期")], source_filename="revision.xlsx",
        source_sha256="D" * 64, conflict_strategy="NEW_REVISION",
    )
    result = repository.commit_import(
        PROJECT, batch_id=revised["batch_id"], preview_token=revised["preview_token"],
        source_sha256=revised["source_sha256"],
    )
    assert result["new_revision_count"] == 1
    assert repository.get_case("579_O2", "SET-001")["current_revision"] == 2
    assert repository.get_case("579_O2", "SET-001")["expected_text"] == "新预期"


def test_duplicate_rows_are_rejected_before_preview_write(tmp_path: Path) -> None:
    repository = CaseManagementRepository(tmp_path / "cases.sqlite3")
    with pytest.raises(ValueError, match="IMPORT_DUPLICATE_CASE_ID"):
        repository.preview_import(
            PROJECT, [payload("SET-001"), payload("SET-001")],
            source_filename="duplicate.xlsx", source_sha256="E" * 64,
        )
    assert repository.list_cases("579_O2") == []
