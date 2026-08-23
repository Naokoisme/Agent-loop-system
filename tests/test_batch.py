from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from agent_loop_system.tools.case_map import CaseEntry, CaseRunResult
from agent_loop_system.tools.test import CaseDecision
from agent_loop_system.tools.test_batch import _judgement_text, run_batch


def test_judgement_text_includes_ordered_verification_points() -> None:
    result = CaseRunResult(
        case_id="DEMO_001",
        sheet="demo",
        expected_text="人工预期",
        verification_points=["状态一", "状态二"],
    )
    text = _judgement_text(result)
    assert "人工预期" in text
    assert "1. 状态一" in text
    assert "2. 状态二" in text


def test_visual_prompt_accepts_visible_icon_for_function_named_control() -> None:
    source = Path("src/agent_loop_system/tools/test.py").read_text(encoding="utf-8")
    assert "不得仅因截图没有显示同名文字而判 FAIL" in source


def test_visual_prompt_accepts_stable_sparse_fullscreen_page_identity() -> None:
    source = Path("src/agent_loop_system/tools/test.py").read_text(encoding="utf-8")
    assert "视觉稀疏页面" in source
    assert "不得仅因没有页面标题而判 CANNOT_VERIFY" in source


def test_run_batch_routes_every_case_through_single_case_runner(tmp_path: Path) -> None:
    case = CaseEntry(
        case_id="DEMO_001",
        sheet="demo",
        expected_text="结果正确",
        setup=[],
        actions=[],
        collect=[],
    )
    result = CaseRunResult(
        case_id=case.case_id,
        sheet=case.sheet,
        expected_text=case.expected_text,
        terminal_json=[{"type": "gui_tree_end"}],
    )

    def fake_save(_result, verdict, output_path):
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"verdict": verdict.verdict}), encoding="utf-8")
        return path

    with (
        patch("agent_loop_system.tools.test_batch.load_case_map", return_value={case.case_id: case}),
        patch(
            "agent_loop_system.tools.test_batch.run_single_case",
            return_value=result,
        ) as single_runner,
        patch(
            "agent_loop_system.tools.test_batch.judge_case_result",
            return_value=CaseDecision(verdict="PASS", reason="证据完整"),
        ),
        patch("agent_loop_system.tools.test_batch.save_evidence", side_effect=fake_save),
    ):
        summary = run_batch(sheets=["demo"], output_root=tmp_path, judge_workers=2)

    single_runner.assert_called_once_with(
        "demo",
        case.case_id,
        str(tmp_path / "demo" / case.case_id / "screenshot.bmp"),
    )
    assert summary["executed"] == 1
    assert summary["verdict_counts"] == {"PASS": 1}
    assert summary["execution_status"] == "OK"
    assert summary["evidence_status"] == "COMPLETE"
    assert summary["records"][0]["evidence_status"] == "COMPLETE"
    assert json.loads((tmp_path / "summary.json").read_text(encoding="utf-8"))["record_count"] == 1
