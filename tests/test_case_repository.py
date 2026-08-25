from __future__ import annotations

from pathlib import Path

import pytest

from agent_loop_system.case_management import CaseManagementRepository


def project579() -> dict:
    return {
        "project_id": "579_O2",
        "allowed_platforms": ["579"],
        "allowed_targets": ["579.o2"],
        "default_platform": "579",
        "case_catalog": {"type": "manifest_579"},
    }


def project_w30() -> dict:
    return {
        "project_id": "W30_SIM",
        "allowed_platforms": ["w30"],
        "allowed_targets": ["w30.simulator"],
        "default_platform": "w30",
        "case_catalog": {"type": "unified"},
    }


def case(case_id: str = "CASE-001", *, platforms: list[str] | None = None) -> dict:
    return {
        "case_id": case_id,
        "sheet": "计时器",
        "priority": "P1",
        "precondition_text": "设备在主表盘",
        "steps_text": "1. 打开计时器\n2. 点击开始",
        "expected_text": "计时开始运行",
        "applicable_platforms": platforms or ["579"],
    }


def test_manual_579_case_is_manageable_but_not_silently_runnable(tmp_path: Path) -> None:
    repository = CaseManagementRepository(tmp_path / "cases.sqlite3")
    created = repository.create_case(project579(), case())
    assert created == {"case_id": "CASE-001", "revision": 1}

    stored = repository.get_case("579_O2", "CASE-001")
    assert stored is not None
    assert stored["source_locked"] is False
    assert stored["applicable_platforms"] == ["579"]
    assert stored["platform_automation"]["579"]["runnable"] is False
    assert stored["platform_automation"]["579"]["blocker"] == "CASE_PLATFORM_MAPPING_MISSING"


def test_frozen_source_edit_creates_revision_and_preserves_baseline(tmp_path: Path) -> None:
    repository = CaseManagementRepository(tmp_path / "cases.sqlite3")
    source = {
        **case(),
        "automation_maturity": "AUTO_READY",
        "platform_automation": {"579": {"maturity": "AUTO_READY", "runnable": True}},
        "_source_file": "case_map/579_case_map/timer.json",
        "_source_file_sha256": "A" * 64,
    }
    first = repository.sync_source_cases(project579(), [source])
    second = repository.sync_source_cases(project579(), [source])
    assert first["created"] == 1
    assert second["created"] == 0
    assert second["updated"] == 0

    repository.create_revision(
        project579(), "CASE-001", {"expected_text": "计时数值持续增加"},
        change_summary="补充预期",
    )
    stored = repository.get_case("579_O2", "CASE-001")
    assert stored is not None
    assert stored["source_locked"] is True
    assert stored["has_managed_override"] is True
    assert stored["source_sha256"] == "A" * 64
    assert stored["current_revision"] == 2
    assert stored["expected_text"] == "计时数值持续增加"

    revisions = repository.revisions("579_O2", "CASE-001")
    assert [item["revision"] for item in revisions] == [2, 1]
    assert revisions[-1]["content"]["expected_text"] == "计时开始运行"

    changed_source = {**source, "expected_text": "上游基线后来发生变化", "_source_file_sha256": "B" * 64}
    repository.sync_source_cases(project579(), [changed_source])
    stored = repository.get_case("579_O2", "CASE-001")
    assert stored is not None
    assert stored["expected_text"] == "计时数值持续增加"
    assert stored["source_sha256"] == "B" * 64


def test_source_sync_fingerprint_survives_repository_restart(tmp_path: Path) -> None:
    database = tmp_path / "cases.sqlite3"
    repository = CaseManagementRepository(database)
    assert repository.source_sync_matches("579_O2", "catalog-v1") is False
    repository.sync_source_cases(
        project579(), [case()], source_fingerprint="catalog-v1"
    )
    assert repository.source_sync_matches("579_O2", "catalog-v1") is True

    reopened = CaseManagementRepository(database)
    assert reopened.source_sync_matches("579_O2", "catalog-v1") is True
    assert reopened.source_sync_matches("579_O2", "catalog-v2") is False


def test_crud_clone_archive_restore_and_platform_validation(tmp_path: Path) -> None:
    repository = CaseManagementRepository(tmp_path / "cases.sqlite3")
    repository.create_case(project_w30(), case(platforms=["w30"]))
    edited = repository.create_revision(
        project_w30(), "CASE-001", {"priority": "P0", "expected_text": "计时正常"}
    )
    assert edited["revision"] == 2
    cloned = repository.clone_case(project_w30(), "CASE-001", "CASE-002")
    assert cloned["revision"] == 1
    assert repository.get_case("W30_SIM", "CASE-002")["workflow_state"] == "DRAFT"

    repository.set_workflow_state(project_w30(), "CASE-001", "ARCHIVED")
    assert [item["case_id"] for item in repository.list_cases("W30_SIM")] == ["CASE-002"]
    repository.set_workflow_state(project_w30(), "CASE-001", "ACTIVE")
    assert len(repository.list_cases("W30_SIM")) == 2

    with pytest.raises(ValueError, match="PLATFORM_NOT_APPLICABLE"):
        repository.create_case(project579(), case("CASE-003", platforms=["w30"]))

    with pytest.raises(ValueError, match="CASE_ID_IMMUTABLE"):
        repository.create_revision(project_w30(), "CASE-001", {"case_id": "RENAMED"})


def test_exact_w30_promoted_semantics_are_preserved(tmp_path: Path) -> None:
    repository = CaseManagementRepository(tmp_path / "cases.sqlite3")
    promoted = {**case("W30-001", platforms=["w30"]), "mapping_status": "PROMOTED"}
    dirty = {**case("W30-002", platforms=["w30"]), "mapping_status": " promoted "}
    repository.sync_source_cases(project_w30(), [promoted, dirty])
    first = repository.get_case("W30_SIM", "W30-001")
    second = repository.get_case("W30_SIM", "W30-002")
    assert first["platform_automation"]["w30"]["maturity"] == "PROMOTED"
    assert first["platform_automation"]["w30"]["runnable"] is True
    assert second["mapping_status"] == " promoted "
    assert second["platform_automation"]["w30"]["maturity"] != "PROMOTED"


def test_579_binding_workflow_is_versioned_and_rejects_raw_actions(tmp_path: Path) -> None:
    repository = CaseManagementRepository(tmp_path / "cases.sqlite3")
    repository.create_case(project579(), case())
    with pytest.raises(ValueError, match="RAW_ACTION_FORBIDDEN"):
        repository.transition_binding(
            project579(), "CASE-001", "579", "candidate", {"raw_command": "secret"}
        )
    candidate = repository.transition_binding(
        project579(), "CASE-001", "579", "candidate",
        {"binding_ref": "AC-SAFE", "plan_sha256": "A" * 64, "action_ids": ["OPEN_TIMER"]},
    )
    assert candidate["binding_version"] == 2
    assert candidate["automation_maturity"] == "NEED_REVIEW"
    reviewed = repository.transition_binding(
        project579(), "CASE-001", "579", "review", {"approved": True, "review_note": "已核对"}
    )
    assert reviewed["review_status"] == "APPROVED"
    promoted = repository.transition_binding(project579(), "CASE-001", "579", "promote")
    assert promoted["automation_maturity"] == "AUTO_READY"
    assert promoted["runnable"] is True
    rolled_back = repository.transition_binding(project579(), "CASE-001", "579", "rollback")
    assert rolled_back["automation_maturity"] == "UNMAPPED"
    assert rolled_back["runnable"] is False
