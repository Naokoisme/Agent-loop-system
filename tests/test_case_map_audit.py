from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from sim_tools.audit_case_map import audit_case_maps


def _write_case(path: Path, **overrides) -> None:
    payload = {
        "case_id": "DEMO_001",
        "sheet": "demo",
        "steps_text": "1.点击按钮",
        "expected_text": "1.进入结果页",
        "setup": ["srv_quick_cmd send TOP5STEP:ENTER_PAGE:DEMO,0;"],
        "actions": ["srv_quick_cmd send TOP5STEP:TP_CLICK:100,100,1;"],
        "collect": [
            "srv_quick_cmd send TOP5STEP:GUI_TREE:1;",
            "srv_quick_cmd send TOP5STEP:SCREENSHOT_PRINT:;",
        ],
        "unable": False,
        "mapping_status": "PROMOTED",
        "note": "",
    }
    payload.update(overrides)
    path.write_text(json.dumps([payload], ensure_ascii=False), encoding="utf-8")
    (path.parent / "external_execution_history.jsonl").write_text(
        json.dumps({
            "case_id": payload["case_id"],
            "sheet": payload["sheet"],
            "target": "TEST_PROFILE",
            "last_verified": "2026-08-17",
            "evidence_root": str(path.parent),
            "evidence_paths": ["result.json"],
        }, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def test_audit_accepts_matching_operation_and_checkpoint(tmp_path: Path) -> None:
    _write_case(tmp_path / "demo.json", verification_points=["进入结果页"])
    with patch(
        "sim_tools.audit_case_map._live_capabilities",
        return_value=({"ENTER_PAGE", "TP_CLICK", "GUI_TREE", "SCREENSHOT_PRINT"}, {"DEMO"}),
    ):
        counts, issues = audit_case_maps(tmp_path)
    assert counts["solidified"] == 1
    assert issues == []


def test_audit_accepts_empty_dynamic_case_without_external_history(tmp_path: Path) -> None:
    payload = {
        "case_id": "DEMO_DYNAMIC",
        "sheet": "demo",
        "steps_text": "1.点击按钮",
        "expected_text": "1.进入结果页",
        "setup": [],
        "actions": [],
        "collect": [],
        "verification_points": [],
        "unable": False,
        "note": "",
    }
    (tmp_path / "demo.json").write_text(
        json.dumps([payload], ensure_ascii=False),
        encoding="utf-8",
    )
    (tmp_path / "external_execution_history.jsonl").write_text("", encoding="utf-8")
    with patch(
        "sim_tools.audit_case_map._live_capabilities",
        return_value=(set(), set()),
    ):
        counts, issues = audit_case_maps(tmp_path)
    assert counts["unexplored"] == 1
    assert counts["solidified"] == 0
    assert issues == []


def test_audit_rejects_case_map_profile_metadata_mismatch(tmp_path: Path) -> None:
    profile_dir = tmp_path / "6202_simulator_case_map"
    profile_dir.mkdir()
    (profile_dir / "demo.json").write_text(
        json.dumps({
            "profile": "620C_W6830",
            "sheet": "demo",
            "cases": [{"case_id": "DEMO_001", "sheet": "demo"}],
        }),
        encoding="utf-8",
    )
    (profile_dir / "external_execution_history.jsonl").write_text("", encoding="utf-8")
    with patch("sim_tools.audit_case_map._live_capabilities", return_value=(set(), set())):
        counts, issues = audit_case_maps(profile_dir)
    assert counts["total"] == 0
    assert [issue.code for issue in issues] == ["case_map_metadata_mismatch"]


def test_audit_rejects_non_array_or_non_object_cases(tmp_path: Path) -> None:
    for position, invalid_cases in enumerate(("not-an-array", [{"sheet": "demo"}, 42])):
        profile_dir = tmp_path / str(position) / "6202_simulator_case_map"
        profile_dir.mkdir(parents=True)
        (profile_dir / "demo.json").write_text(
            json.dumps({
                "profile": "6202_W5230_SIMULATOR",
                "sheet": "demo",
                "cases": invalid_cases,
            }),
            encoding="utf-8",
        )
        (profile_dir / "external_execution_history.jsonl").write_text("", encoding="utf-8")
        with patch("sim_tools.audit_case_map._live_capabilities", return_value=(set(), set())):
            counts, issues = audit_case_maps(profile_dir)
        assert counts["total"] == 0
        assert [issue.code for issue in issues] == ["case_map_metadata_mismatch"]


def test_audit_rejects_mapping_status_with_surrounding_whitespace(tmp_path: Path) -> None:
    _write_case(
        tmp_path / "demo.json",
        mapping_status=" PROMOTED ",
        setup=[],
        actions=[],
        collect=[],
        verification_points=[],
    )
    with patch("sim_tools.audit_case_map._live_capabilities", return_value=(set(), set())):
        counts, issues = audit_case_maps(tmp_path)
    assert counts["explored_unsolidified"] == 1
    assert [issue.code for issue in issues] == ["invalid_mapping_status"]


def test_audit_reads_but_ignores_legacy_unable_flag(tmp_path: Path) -> None:
    _write_case(
        tmp_path / "demo.json",
        unable=True,
        mapping_status="",
        setup=[],
        actions=[],
        collect=[],
        verification_points=[],
    )
    with patch("sim_tools.audit_case_map._live_capabilities", return_value=(set(), set())):
        counts, issues = audit_case_maps(tmp_path)
    assert counts["explored_unsolidified"] == 1
    assert issues == []


def test_audit_reports_enter_page_used_instead_of_click(tmp_path: Path) -> None:
    _write_case(
        tmp_path / "demo.json",
        actions=["srv_quick_cmd send TOP5STEP:ENTER_PAGE:DEMO,0;"],
    )
    with patch(
        "sim_tools.audit_case_map._live_capabilities",
        return_value=({"ENTER_PAGE", "GUI_TREE", "SCREENSHOT_PRINT"}, {"DEMO"}),
    ):
        _counts, issues = audit_case_maps(tmp_path)
    assert [issue.code for issue in issues] == ["missing_click"]


def test_audit_reports_verification_checkpoint_mismatch(tmp_path: Path) -> None:
    _write_case(tmp_path / "demo.json", verification_points=["状态一", "状态二"])
    with patch(
        "sim_tools.audit_case_map._live_capabilities",
        return_value=({"ENTER_PAGE", "TP_CLICK", "GUI_TREE", "SCREENSHOT_PRINT"}, {"DEMO"}),
    ):
        _counts, issues = audit_case_maps(tmp_path)
    assert [issue.code for issue in issues] == ["verification_checkpoint_mismatch"]


def test_audit_does_not_treat_selection_page_or_touch_release_as_click(tmp_path: Path) -> None:
    _write_case(
        tmp_path / "demo.json",
        steps_text="1.观察关机重启选择页\n2.停止触摸屏幕",
        actions=[],
    )
    with patch(
        "sim_tools.audit_case_map._live_capabilities",
        return_value=({"ENTER_PAGE", "GUI_TREE", "SCREENSHOT_PRINT"}, {"DEMO"}),
    ):
        _counts, issues = audit_case_maps(tmp_path)
    assert issues == []


def test_audit_accepts_encoder_for_roller_selection(tmp_path: Path) -> None:
    _write_case(
        tmp_path / "demo.json",
        steps_text="1.滚动选择10s",
        actions=["srv_quick_cmd send TOP5STEP:QDEC_SET:1;"],
    )
    with patch(
        "sim_tools.audit_case_map._live_capabilities",
        return_value=({"ENTER_PAGE", "QDEC_SET", "GUI_TREE", "SCREENSHOT_PRINT"}, {"DEMO"}),
    ):
        _counts, issues = audit_case_maps(tmp_path)
    assert issues == []


def test_audit_accepts_touch_press_for_screen_long_press(tmp_path: Path) -> None:
    _write_case(
        tmp_path / "demo.json",
        steps_text="1.在解锁页长按3秒",
        actions=[
            "srv_quick_cmd send TOP5STEP:TP_PRESS:100,100,1;",
            "srv_quick_cmd send TOP5STEP:TP_PRESS:100,100,0;",
        ],
    )
    with patch(
        "sim_tools.audit_case_map._live_capabilities",
        return_value=({"ENTER_PAGE", "TP_PRESS", "GUI_TREE", "SCREENSHOT_PRINT"}, {"DEMO"}),
    ):
        _counts, issues = audit_case_maps(tmp_path)
    assert issues == []


def test_audit_still_requires_button_press_for_encoder(tmp_path: Path) -> None:
    _write_case(
        tmp_path / "demo.json",
        steps_text="1.长按编码器5秒",
        actions=["srv_quick_cmd send TOP5STEP:TP_PRESS:100,100,1;"],
    )
    with patch(
        "sim_tools.audit_case_map._live_capabilities",
        return_value=({"ENTER_PAGE", "TP_PRESS", "GUI_TREE", "SCREENSHOT_PRINT"}, {"DEMO"}),
    ):
        _counts, issues = audit_case_maps(tmp_path)
    assert [issue.code for issue in issues] == ["missing_button"]


def test_audit_treats_long_press_button_as_physical_button(tmp_path: Path) -> None:
    _write_case(
        tmp_path / "demo.json",
        steps_text="1.在设置列表长按按键3秒",
        actions=["srv_quick_cmd send TOP5STEP:BUTTON_PRESS:1,2,3000;"],
    )
    with patch(
        "sim_tools.audit_case_map._live_capabilities",
        return_value=({"ENTER_PAGE", "BUTTON_PRESS", "GUI_TREE", "SCREENSHOT_PRINT"}, {"DEMO"}),
    ):
        _counts, issues = audit_case_maps(tmp_path)
    assert issues == []


def test_explicit_single_final_verification_suppresses_multi_stage_warning(tmp_path: Path) -> None:
    _write_case(
        tmp_path / "demo.json",
        steps_text="1.点击第一项\n2.点击第二项",
        expected_text="1.最终列表保留两项\n2.第二项位于顶部",
        verification_points=["最终列表同时证明数量和顺序"],
        actions=[
            "srv_quick_cmd send TOP5STEP:TP_CLICK:100,100,1;",
            "srv_quick_cmd send TOP5STEP:TP_CLICK:100,120,1;",
        ],
    )
    with patch(
        "sim_tools.audit_case_map._live_capabilities",
        return_value=({"ENTER_PAGE", "TP_CLICK", "GUI_TREE", "SCREENSHOT_PRINT"}, {"DEMO"}),
    ):
        _counts, issues = audit_case_maps(tmp_path)
    assert issues == []


def test_invalid_case_schema_is_reported_without_aborting_the_audit(tmp_path: Path) -> None:
    _write_case(tmp_path / "demo.json", verification_points=[{"expected": "错误结构"}])
    with patch(
        "sim_tools.audit_case_map._live_capabilities",
        return_value=({"ENTER_PAGE", "TP_CLICK", "GUI_TREE", "SCREENSHOT_PRINT"}, {"DEMO"}),
    ):
        counts, issues = audit_case_maps(tmp_path)
    assert counts["total"] == 1
    assert [issue.code for issue in issues] == ["invalid_case_schema"]


def test_duplicate_case_id_is_reported_across_files(tmp_path: Path) -> None:
    _write_case(tmp_path / "one.json", sheet="one")
    _write_case(tmp_path / "two.json", sheet="two")
    (tmp_path / "external_execution_history.jsonl").write_text(
        json.dumps({
            "case_id": "DEMO_001",
            "sheet": "one",
            "target": "TEST_PROFILE",
            "last_verified": "2026-08-17",
            "evidence_root": str(tmp_path),
            "evidence_paths": ["result.json"],
        }) + "\n",
        encoding="utf-8",
    )
    with patch(
        "sim_tools.audit_case_map._live_capabilities",
        return_value=({"ENTER_PAGE", "TP_CLICK", "GUI_TREE", "SCREENSHOT_PRINT"}, {"DEMO"}),
    ):
        counts, issues = audit_case_maps(tmp_path)
    assert counts["total"] == 2
    assert [issue.code for issue in issues] == ["duplicate_case_id"]


def test_audit_rejects_sleep_record_create_without_mode_argument(tmp_path: Path) -> None:
    _write_case(
        tmp_path / "demo.json",
        setup=["srv_quick_cmd send TOP5STEP:SLEEP_RECORD_CREATE:480,120,60,60,30;"],
    )
    with patch(
        "sim_tools.audit_case_map._live_capabilities",
        return_value=({"SLEEP_RECORD_CREATE", "TP_CLICK", "GUI_TREE", "SCREENSHOT_PRINT"}, set()),
    ):
        _counts, issues = audit_case_maps(tmp_path)
    assert [issue.code for issue in issues] == ["invalid_sleep_record_mode"]


def test_audit_rejects_sleep_record_create_with_wrong_mode_arity(tmp_path: Path) -> None:
    _write_case(
        tmp_path / "demo.json",
        setup=["srv_quick_cmd send TOP5STEP:SLEEP_RECORD_CREATE:1,120,240;"],
    )
    with patch(
        "sim_tools.audit_case_map._live_capabilities",
        return_value=({"SLEEP_RECORD_CREATE", "TP_CLICK", "GUI_TREE", "SCREENSHOT_PRINT"}, set()),
    ):
        _counts, issues = audit_case_maps(tmp_path)
    assert [issue.code for issue in issues] == ["invalid_sleep_record_arguments"]


def test_audit_accepts_host_wait_without_firmware_registration(tmp_path: Path) -> None:
    _write_case(
        tmp_path / "demo.json",
        steps_text="1.等待界面稳定",
        actions=["srv_quick_cmd send TOP5STEP:HOST_WAIT:500;"],
    )
    with patch(
        "sim_tools.audit_case_map._live_capabilities",
        return_value=({"ENTER_PAGE", "TP_CLICK", "GUI_TREE", "SCREENSHOT_PRINT", "HOST_WAIT"}, {"DEMO"}),
    ):
        _counts, issues = audit_case_maps(tmp_path)
    assert issues == []


def test_host_wait_is_not_counted_as_a_business_action_for_checkpoint_warnings(
    tmp_path: Path,
) -> None:
    _write_case(
        tmp_path / "demo.json",
        steps_text="1.等待界面稳定\n2.点击按钮",
        expected_text="1.界面稳定\n2.进入结果页",
        actions=[
            "srv_quick_cmd send TOP5STEP:HOST_WAIT:500;",
            "srv_quick_cmd send TOP5STEP:TP_CLICK:100,100,1;",
        ],
    )
    with patch(
        "sim_tools.audit_case_map._live_capabilities",
        return_value=(
            {"ENTER_PAGE", "HOST_WAIT", "TP_CLICK", "GUI_TREE", "SCREENSHOT_PRINT"},
            {"DEMO"},
        ),
    ):
        _counts, issues = audit_case_maps(tmp_path)
    assert issues == []
