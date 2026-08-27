from __future__ import annotations

import base64
import json
import shutil
from pathlib import Path

import openpyxl
import pytest

from agent_loop_system.case_management import CaseManagementRepository
from agent_loop_system.prd_cases import PrdCaseService, QA_SKILL_SHA256, QA_SKILL_FILENAME


def _project() -> dict:
    return {
        "project_id": "QA_DEMO",
        "project_name": "QA 演示",
        "allowed_platforms": ["w30"],
        "allowed_targets": ["w30.simulator"],
        "default_platform": "w30",
        "case_catalog": {"type": "unified"},
    }


def _generated_cases(_: str, context: dict) -> list[dict]:
    suffix = "R" if context.get("feedback") else "A"
    return [
        {
            "case_id": f"LOGIN-{suffix}-001",
            "functional_module": "账号",
            "feature": "登录",
            "test_item": "密码登录",
            "test_point": "有效账号登录成功",
            "title": "输入有效账号和密码后进入首页",
            "priority": "P0",
            "preconditions": "账号已注册且未锁定",
            "steps": ["打开登录页", "输入有效账号和密码", "点击登录按钮"],
            "expected_results": ["显示登录页", "账号和密码被接受", "进入首页并显示账号头像"],
            "test_type": "功能",
            "requirement_ids": ["REQ-LOGIN-01"],
            "note": "",
        },
        {
            "case_id": f"LOGIN-{suffix}-002",
            "functional_module": "账号",
            "feature": "登录",
            "test_item": "密码登录",
            "test_point": "密码错误时阻止登录",
            "title": "输入错误密码后停留在登录页",
            "priority": "P1",
            "preconditions": "账号已注册且未锁定",
            "steps": ["打开登录页", "输入有效账号和错误密码", "点击登录按钮"],
            "expected_results": ["显示登录页", "输入内容可见", "停留在登录页并显示密码错误提示"],
            "test_type": "异常",
            "requirement_ids": ["REQ-LOGIN-02"],
            "note": "",
        },
    ]


@pytest.fixture
def service(tmp_path: Path) -> PrdCaseService:
    source = Path(__file__).resolve().parents[1] / "resources" / "skills" / QA_SKILL_FILENAME
    target = tmp_path / "resources" / "skills" / source.name
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
    repository = CaseManagementRepository(tmp_path / "project_data" / "case_management.sqlite3")
    return PrdCaseService(
        tmp_path,
        repository,
        project_lookup=lambda project_id: _project() if project_id == "QA_DEMO" else (_ for _ in ()).throw(ValueError("PROJECT_NOT_FOUND")),
        generator=_generated_cases,
        start_threads=False,
    )


def _create_and_run(service: PrdCaseService) -> dict:
    content = "# 登录需求\n用户可以使用账号和密码登录。密码错误时应提示且不进入首页。"
    created = service.create_job(
        project_id="QA_DEMO",
        filename="登录需求.md",
        file_base64=base64.b64encode(content.encode()).decode(),
    )
    assert created["status"] == "UPLOADED"
    service.run_job(created["job_id"])
    return service.get_job(created["job_id"])


def test_prd_generation_reopens_excel_then_approves_and_syncs(service: PrdCaseService) -> None:
    job = _create_and_run(service)
    assert job["status"] == "READY_FOR_REVIEW"
    assert job["case_count"] == 2
    assert job["skill"]["sha256"] == QA_SKILL_SHA256
    assert job["quality_gate"]["passed"] is True

    payload = service.get_cases(job["job_id"])
    assert payload["workbook_sha256"] == job["workbook_sha256"]
    assert payload["snapshot_sha256"] == job["snapshot_sha256"]
    assert payload["items"][0]["feature"] == "登录"
    assert payload["items"][0]["stable_id"] == "LOGIN-A-001"

    # Explicit four P0 dimensions are required
    approved = service.review(
        job["job_id"],
        action="approve",
        reviewer="测试负责人",
        comment="已逐条核对",
        dimensions=["wording", "classification", "order", "feature_boundary"],
    )
    assert approved["release"]["decision"] == "GO"
    assert approved["release"]["readiness"] == "READY_WITH_RISKS"
    assert approved["release"]["dimensions"] == {
        "wording": "PASS",
        "classification": "PASS",
        "order": "PASS",
        "feature_boundary": "PASS",
    }
    assert approved["review"]["dimensions"] == approved["release"]["dimensions"]

    result = service.sync(job["job_id"])
    assert result["created"] == 2
    assert result["updated"] == 0
    assert result["unchanged"] == 0
    assert result["conflicts"] == 0
    assert result["total"] == 2

    stored = service.case_repository.get_case("QA_DEMO", "LOGIN-A-001")
    assert stored is not None
    assert stored["source_type"] == "PRD_APPROVED"
    assert stored["source_locked"] is True
    assert stored["automation_maturity"] == "UNMAPPED"
    assert stored["feature"] == "登录"
    assert stored["source_ref"]["job_id"] == job["job_id"]
    assert stored["source_sha256"] == job["workbook_sha256"]

    # Idempotent sync
    assert service.sync(job["job_id"]) == result


def test_p0_review_dimensions_required_for_approval(service: PrdCaseService) -> None:
    job = _create_and_run(service)

    # Missing dimensions entirely fails
    with pytest.raises(ValueError, match="PRD_P0_REVIEW_REQUIRED"):
        service.review(job["job_id"], action="approve", reviewer="QA", comment="通过")

    # Incomplete dimensions fails
    with pytest.raises(ValueError, match="PRD_P0_REVIEW_REQUIRED"):
        service.review(
            job["job_id"],
            action="approve",
            reviewer="QA",
            dimensions=["wording", "classification"],
        )

    # False dimension in dict fails
    with pytest.raises(ValueError, match="PRD_P0_REVIEW_REQUIRED"):
        service.review(
            job["job_id"],
            action="approve",
            reviewer="QA",
            dimensions={
                "wording": True,
                "classification": True,
                "order": False,
                "feature_boundary": True,
            },
        )

    with pytest.raises(ValueError, match="PRD_P0_REVIEW_REQUIRED"):
        service.review(
            job["job_id"],
            action="approve",
            reviewer="QA",
            dimensions={
                "wording": "false",
                "classification": True,
                "order": True,
                "feature_boundary": True,
            },
        )


def test_row_only_rejection_and_regeneration_uses_case_comments(service: PrdCaseService) -> None:
    job = _create_and_run(service)

    # Rejection with no general comment and no row comments fails
    with pytest.raises(ValueError, match="PRD_REJECTION_REASON_REQUIRED"):
        service.review(job["job_id"], action="reject", reviewer="QA")

    with pytest.raises(ValueError, match="PRD_CASE_COMMENT_INVALID"):
        service.review(
            job["job_id"],
            action="reject",
            reviewer="QA",
            case_comments=[{"stable_id": "UNKNOWN", "comment": "无效行"}],
        )

    # Rejection with ONLY row comments (blank general comment) succeeds!
    rejected = service.review(
        job["job_id"],
        action="reject",
        reviewer="QA",
        comment="",
        case_comments=[
            {"stable_id": "LOGIN-A-001", "comment": "请补充极端异常场景"},
            {"stable_id": "LOGIN-A-002", "comment": "操作步骤需要细化"},
        ],
    )
    assert rejected["status"] == "REJECTED"
    assert rejected["review"]["case_comments"][0]["stable_id"] == "LOGIN-A-001"
    assert rejected["review"]["case_comments"][0]["comment"] == "请补充极端异常场景"

    # Regenerating with empty feedback string automatically uses saved row comments!
    regenerated = service.regenerate(job["job_id"], "")
    assert regenerated["status"] == "READY_FOR_REVIEW"
    cases = service.get_cases(job["job_id"])["items"]
    assert cases[0]["case_id"] == "LOGIN-R-001"


def test_snapshot_tamper_fails_closed_before_sync(service: PrdCaseService) -> None:
    job = _create_and_run(service)
    service.review(
        job["job_id"],
        action="approve",
        reviewer="QA",
        dimensions=["wording", "classification", "order", "feature_boundary"],
    )

    # Tamper with review-snapshot.json after approval
    snapshot_path = service.snapshot_path(job["job_id"])
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    snapshot["cases"][0]["title"] = "未经审查被篡改的标题"
    snapshot_path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")

    # Sync MUST fail closed and not write to DB
    with pytest.raises(ValueError, match="PRD_SNAPSHOT_CHANGED"):
        service.sync(job["job_id"])

    # Ensure no cases were written
    assert service.case_repository.get_case("QA_DEMO", "LOGIN-A-001") is None


def test_workbook_change_invalidates_review_and_sync(service: PrdCaseService) -> None:
    job = _create_and_run(service)
    path = service.workbook_path(job["job_id"])
    workbook = openpyxl.load_workbook(path)
    workbook["测试用例"][2][5].value = "被外部修改的标题"
    workbook.save(path)
    workbook.close()

    with pytest.raises(ValueError, match="PRD_WORKBOOK_CHANGED"):
        service.review(
            job["job_id"],
            action="approve",
            reviewer="QA",
            dimensions=["wording", "classification", "order", "feature_boundary"],
        )


def test_workbook_tamper_post_approval_fails_closed(service: PrdCaseService) -> None:
    job = _create_and_run(service)
    service.review(
        job["job_id"],
        action="approve",
        reviewer="QA",
        dimensions=["wording", "classification", "order", "feature_boundary"],
    )

    # Tamper with workbook after approval
    path = service.workbook_path(job["job_id"])
    workbook = openpyxl.load_workbook(path)
    workbook["测试用例"][2][5].value = "审批后篡改的标题"
    workbook.save(path)
    workbook.close()

    # Sync fails closed
    with pytest.raises(ValueError, match="PRD_WORKBOOK_CHANGED"):
        service.sync(job["job_id"])

    # Ensure no cases were written
    assert service.case_repository.get_case("QA_DEMO", "LOGIN-A-001") is None


def test_tampered_skill_bundle_fails_closed(service: PrdCaseService) -> None:
    service.skill_bundle.write_bytes(b"not-the-approved-skill")
    with pytest.raises(RuntimeError, match="QA_SKILL_HASH_MISMATCH"):
        service.verify_skill_bundle()


def test_existing_non_prd_case_is_reported_as_partial_sync_conflict(service: PrdCaseService) -> None:
    service.case_repository.create_case(
        _project(),
        {
            "case_id": "LOGIN-A-001",
            "sheet": "账号",
            "title": "人工维护用例",
            "test_item": "登录",
            "test_point": "人工基线",
            "precondition_text": "账号存在",
            "steps_text": "1. 打开登录页",
            "expected_text": "1. 显示登录页",
            "applicable_platforms": ["w30"],
        },
    )
    job = _create_and_run(service)
    service.review(
        job["job_id"],
        action="approve",
        reviewer="QA",
        dimensions=["wording", "classification", "order", "feature_boundary"],
    )
    result = service.sync(job["job_id"])
    assert result["created"] == 1
    assert result["conflicts"] == 1
    assert service.get_job(job["job_id"])["status"] == "SYNCED_WITH_CONFLICTS"
    assert service.sync(job["job_id"]) == result


def test_prd_resync_preserves_managed_override(service: PrdCaseService) -> None:
    first = _create_and_run(service)
    service.review(
        first["job_id"],
        action="approve",
        reviewer="QA",
        dimensions=["wording", "classification", "order", "feature_boundary"],
    )
    service.sync(first["job_id"])
    service.case_repository.create_revision(
        _project(),
        "LOGIN-A-001",
        {"title": "人工调整后的标题"},
        change_summary="人工修订",
    )

    second = _create_and_run(service)
    service.review(
        second["job_id"],
        action="approve",
        reviewer="QA",
        dimensions=["wording", "classification", "order", "feature_boundary"],
    )
    result = service.sync(second["job_id"])

    assert result["conflicts"] == 1
    assert service.case_repository.get_case("QA_DEMO", "LOGIN-A-001")["title"] == "人工调整后的标题"
