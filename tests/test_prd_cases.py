from __future__ import annotations

import base64
import shutil
from pathlib import Path

import openpyxl
import pytest

from agent_loop_system.case_management import CaseManagementRepository
from agent_loop_system.prd_cases import PrdCaseService, QA_SKILL_SHA256


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
    source = Path(__file__).resolve().parents[1] / "resources" / "skills" / "xiaozhou-portable-skill-execution-quality-20260825.zip"
    target = tmp_path / "resources" / "skills" / source.name
    target.parent.mkdir(parents=True)
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
    assert payload["items"][0]["feature"] == "登录"
    assert payload["items"][0]["stable_id"] == "LOGIN-A-001"

    approved = service.review(
        job["job_id"], action="approve", reviewer="测试负责人", comment="已逐条核对"
    )
    assert approved["release"]["decision"] == "GO"
    result = service.sync(job["job_id"])
    assert result == {
        "created": 2,
        "updated": 0,
        "unchanged": 0,
        "conflicts": 0,
        "total": 2,
        "job_id": job["job_id"],
        "project_id": "QA_DEMO",
        "synced_at": result["synced_at"],
    }
    stored = service.case_repository.get_case("QA_DEMO", "LOGIN-A-001")
    assert stored is not None
    assert stored["source_type"] == "PRD_APPROVED"
    assert stored["source_locked"] is True
    assert stored["feature"] == "登录"
    assert stored["source_ref"]["job_id"] == job["job_id"]
    assert service.sync(job["job_id"]) == result


def test_rejection_requires_reason_and_regeneration_uses_feedback(service: PrdCaseService) -> None:
    job = _create_and_run(service)
    with pytest.raises(ValueError, match="PRD_REJECTION_REASON_REQUIRED"):
        service.review(job["job_id"], action="reject", reviewer="QA")
    rejected = service.review(
        job["job_id"], action="reject", reviewer="QA", comment="补充异常场景"
    )
    assert rejected["status"] == "REJECTED"
    regenerated = service.regenerate(job["job_id"], "补充异常场景")
    assert regenerated["status"] == "READY_FOR_REVIEW"
    assert service.get_cases(job["job_id"])["items"][0]["case_id"] == "LOGIN-R-001"


def test_workbook_change_invalidates_review_and_sync_requires_approval(service: PrdCaseService) -> None:
    job = _create_and_run(service)
    with pytest.raises(ValueError, match="PRD_SYNC_REQUIRES_APPROVAL"):
        service.sync(job["job_id"])
    path = service.workbook_path(job["job_id"])
    workbook = openpyxl.load_workbook(path)
    workbook["测试用例"][2][5].value = "被外部修改的标题"
    workbook.save(path)
    workbook.close()
    with pytest.raises(ValueError, match="PRD_WORKBOOK_CHANGED"):
        service.review(job["job_id"], action="approve", reviewer="QA")


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
    service.review(job["job_id"], action="approve", reviewer="QA")
    result = service.sync(job["job_id"])
    assert result["created"] == 1
    assert result["conflicts"] == 1
    assert service.get_job(job["job_id"])["status"] == "SYNCED_WITH_CONFLICTS"
    assert service.sync(job["job_id"]) == result
