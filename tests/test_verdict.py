from __future__ import annotations

import json
import unittest
import tempfile
from pathlib import Path
from unittest import mock

from PIL import Image

from agent_loop_system.tools.test import (
    Verdict,
    _configure_console_output,
    aggregate_verdicts,
    judge_case_result,
    judge_test_with_vision,
    judge_with_llm,
    judge_with_vision,
    save_evidence,
)
from agent_loop_system.tools.case_map import CaseRunResult
from agent_loop_system.tools.llm_retry import LLMRetryError


class AggregateVerdictsTest(unittest.TestCase):
    def test_error_beats_product_verdicts(self) -> None:
        self.assertEqual(
            aggregate_verdicts(["FAIL", "ERROR", "CANNOT_VERIFY", "PASS"]),
            "ERROR",
        )

    def test_fail_beats_cannot_verify_and_pass(self) -> None:
        self.assertEqual(
            aggregate_verdicts(["FAIL", "CANNOT_VERIFY", "PASS"]), "FAIL"
        )

    def test_cannot_verify_beats_pass(self) -> None:
        self.assertEqual(aggregate_verdicts(["CANNOT_VERIFY", "PASS"]), "CANNOT_VERIFY")

    def test_all_pass_yields_pass(self) -> None:
        self.assertEqual(aggregate_verdicts(["PASS", "PASS"]), "PASS")

    def test_empty_yields_cannot_verify(self) -> None:
        self.assertEqual(aggregate_verdicts([]), "CANNOT_VERIFY")

    def test_skip_never_yields_pass(self) -> None:
        self.assertEqual(aggregate_verdicts(["SKIP"]), "CANNOT_VERIFY")
        self.assertEqual(aggregate_verdicts(["PASS", "SKIP"]), "CANNOT_VERIFY")


class ConsoleOutputTest(unittest.TestCase):
    def test_console_output_uses_utf8_and_replacement(self) -> None:
        stream = mock.Mock()
        with mock.patch("agent_loop_system.tools.test.sys.stdout", stream), mock.patch(
            "agent_loop_system.tools.test.sys.stderr", stream
        ):
            _configure_console_output()
        stream.reconfigure.assert_called_with(encoding="utf-8", errors="replace")


class TerminalJsonJudgeDisabledTest(unittest.TestCase):
    def test_terminal_json_never_reaches_llm(self) -> None:
        verdict = judge_with_llm("expected", [{"type": "gui_tree_node"}])
        self.assertEqual(verdict.verdict, "CANNOT_VERIFY")
        self.assertIn("终端 JSON 判定已禁用", verdict.reason)


class SaveEvidenceTest(unittest.TestCase):
    def test_custom_path_and_execution_status_are_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "job" / "result.json"
            result = CaseRunResult(
                case_id="CALC_001",
                sheet="计算器",
                expected_text="显示正确",
                provenance={
                    "target": "simulator",
                    "case_map_profile": "620C_W6830",
                    "project": "620C_W6830",
                    "artifact_path": "D:/sim/main.exe",
                    "artifact_sha256": "A" * 64,
                },
            )
            save_evidence(result, Verdict(verdict="PASS", reason="符合预期"), output)
            payload = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(payload["schema_version"], 3)
        self.assertEqual(payload["provenance"], result.provenance)
        self.assertEqual(payload["verdict"], "PASS")
        self.assertEqual(payload["reason"], "符合预期")

    def test_action_execution_error_overrides_visual_pass(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "result.json"
            result = CaseRunResult(
                case_id="CALC_001",
                sheet="计算器",
                expected_text="显示正确",
                aborted=True,
                action_errors=["点击命令被拒绝"],
            )
            save_evidence(
                result,
                Verdict(verdict="PASS", reason="截图符合预期"),
                output,
            )
            payload = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(payload["verdict"], "ERROR")
        self.assertEqual(payload["reason"], "点击命令被拒绝")
        self.assertEqual(payload["execution_status"], "ERROR")
        self.assertEqual(payload["execution_reason"], "点击命令被拒绝")

    def test_diagnostic_collection_error_does_not_override_complete_visual_pass(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "result.json"
            result = CaseRunResult(
                case_id="CALC_001",
                sheet="计算器",
                expected_text="显示正确",
                collect_errors=["GUI_TREE 回包不完整"],
                evidence_contract={"complete": True, "issues": []},
            )
            save_evidence(
                result,
                Verdict(verdict="PASS", reason="截图符合预期"),
                output,
            )
            payload = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(payload["verdict"], "PASS")
        self.assertEqual(payload["execution_status"], "ERROR")
        self.assertEqual(payload["execution_reason"], "GUI_TREE 回包不完整")

    def test_incomplete_evidence_overrides_precomputed_agent_pass(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "result.json"
            result = CaseRunResult(
                case_id="DYNAMIC_001",
                sheet="demo",
                expected_text="显示正确",
                precomputed_verdict="PASS",
                precomputed_reason="Agent 判断符合预期",
                evidence_contract={
                    "complete": False,
                    "issues": [{"message": "Agent-loop 探索没有取得截图"}],
                },
            )
            save_evidence(result, None, output)
            payload = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(payload["verdict"], "ERROR")
        self.assertEqual(payload["reason"], "Agent-loop 探索没有取得截图")


class VisionEvidenceTest(unittest.TestCase):
    def _images(self, root: Path) -> tuple[str, str, str]:
        defect = root / "defect.png"
        before = root / "before.bmp"
        after = root / "after.bmp"
        for path in (defect, before, after):
            Image.new("RGB", (4, 4), "white").save(path)
        return str(defect), str(before), str(after)

    def _judge(self, *args, **kwargs) -> tuple[Verdict, list[dict]]:
        captured: dict[str, list[dict]] = {}

        class Structured:
            def invoke(self, messages):
                captured["content"] = messages[0].content
                return Verdict(verdict="PASS", reason="ok")

        class LLM:
            def with_structured_output(self, schema):
                return Structured()

        with mock.patch.dict("os.environ", {"OPENAI_API_KEY": "sk-test"}), mock.patch(
            "langchain_openai.ChatOpenAI", return_value=LLM()
        ), mock.patch(
            "agent_loop_system.tools.test.invoke_with_retry", side_effect=lambda fn: fn()
        ):
            verdict = judge_with_vision(*args, **kwargs)
        return verdict, captured["content"]

    def test_baseline_labels_defect_original_and_current_screenshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            defect, _, current = self._images(Path(tmp))
            verdict, content = self._judge(
                current,
                "文案不一致",
                defect_image_paths=[defect],
            )

        labels = [item["text"] for item in content if item["type"] == "text"]
        self.assertEqual(verdict.verdict, "PASS")
        self.assertTrue(any("缺陷原图 1" in label for label in labels))
        self.assertIn("当前模拟器截图：", labels)
        self.assertTrue(any("与原图冲突时以原图为准" in label for label in labels))
        self.assertTrue(any("当前时间、日期、电量、信号" in label for label in labels))
        self.assertTrue(any("不得从参考图中扩展出新的故障点" in label for label in labels))
        self.assertTrue(any("说明文案" in label and "定义预期" in label for label in labels))
        self.assertTrue(any("系统记录的截图采集时间" in label for label in labels))
        self.assertTrue(any("设备当前时钟" in label for label in labels))

    def test_baseline_includes_verified_observations_without_replacing_visual_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            defect, _, current = self._images(Path(tmp))
            _, content = self._judge(
                current,
                "最近应用图标错误",
                defect_image_paths=[defect],
                verified_observations=[
                    "步骤 1；执行命令 :ENTER_PAGE:VOICE_ASSISTANT_INTERACTION,0；观察窗口 VOICE_ASSISTANT_INTERACTION",
                    "步骤 2；执行命令 :ENTER_PAGE:SIDEBAR,0；观察窗口 SIDEBAR",
                ],
            )

        prompt = "\n".join(item["text"] for item in content if item["type"] == "text")
        self.assertIn("已验证的复现事实", prompt)
        self.assertIn("观察窗口 VOICE_ASSISTANT_INTERACTION", prompt)
        self.assertIn("缺陷是否存在仍必须以截图和缺陷原始证据为准", prompt)

    def test_repair_labels_original_before_and_after_in_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            defect, before, after = self._images(Path(tmp))
            _, content = self._judge(
                after,
                "文案不一致",
                reference_screenshot=before,
                defect_image_paths=[defect],
            )

        labels = [item["text"] for item in content if item["type"] == "text"]
        defect_index = next(i for i, text in enumerate(labels) if "缺陷原图 1" in text)
        before_index = labels.index("修复前模拟器截图：")
        after_index = labels.index("修复后模拟器截图：")
        self.assertLess(defect_index, before_index)
        self.assertLess(before_index, after_index)
        capture_notes = [label for label in labels if "系统记录的截图采集时间" in label]
        self.assertEqual(len(capture_notes), 2)


class TestCaseVisionEvidenceTest(unittest.TestCase):
    def _judge(self, screenshots, verification_points):
        captured: dict[str, list[dict]] = {}

        class Structured:
            def invoke(self, messages):
                captured["content"] = messages[0].content
                return Verdict(verdict="PASS", reason="两张截图均符合预期")

        class LLM:
            def with_structured_output(self, schema):
                return Structured()

        with mock.patch.dict("os.environ", {"OPENAI_API_KEY": "sk-test"}), mock.patch(
            "langchain_openai.ChatOpenAI", return_value=LLM()
        ), mock.patch(
            "agent_loop_system.tools.test.invoke_with_retry", side_effect=lambda fn: fn()
        ):
            verdict = judge_test_with_vision("先显示按钮，点击后显示测量中", screenshots, verification_points)
        return verdict, captured.get("content", [])

    def test_each_verification_point_is_paired_with_one_screenshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = root / "first.bmp"
            second = root / "second.bmp"
            Image.new("RGB", (4, 4), "white").save(first)
            Image.new("RGB", (4, 4), "black").save(second)
            verdict, content = self._judge(
                [
                    {"path": str(first), "label": "旧标签"},
                    {"path": str(second), "label": "旧标签"},
                ],
                ["显示 Measure 按钮", "显示 Measuring"],
            )

        labels = [item["text"] for item in content if item["type"] == "text"]
        self.assertEqual(verdict.verdict, "PASS")
        self.assertEqual(sum(item["type"] == "image_url" for item in content), 2)
        self.assertIn("判定截图 1，对应验证点：显示 Measure 按钮", labels)
        self.assertIn("判定截图 2，对应验证点：显示 Measuring", labels)
        prompt = labels[0]
        self.assertIn("唯一证据是下方模拟器截图", prompt)
        self.assertIn("不得假设或索要 GUI_TREE", prompt)
        self.assertIn("有效数值和明确完成时间", prompt)
        self.assertIn("不得把旁边的再次测量按钮或操作提示误读为仍在测量", prompt)
        self.assertIn("不得把相邻用例或先前截图的页面内容套用", prompt)
        self.assertIn("不得臆造图中不存在的列表、文字或按钮", prompt)
        self.assertNotIn("terminal_json", prompt)

    def test_missing_checkpoint_screenshot_cannot_verify(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            first = Path(tmp) / "first.bmp"
            Image.new("RGB", (4, 4), "white").save(first)
            with mock.patch.dict("os.environ", {"OPENAI_API_KEY": "sk-test"}):
                verdict = judge_test_with_vision(
                    "两个状态",
                    [{"path": str(first)}],
                    ["状态一", "状态二"],
                )
        self.assertEqual(verdict.verdict, "CANNOT_VERIFY")
        self.assertIn("需要 2 张截图", verdict.reason)

    def test_vision_api_retry_error_is_labeled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            screenshot = Path(tmp) / "screenshot.bmp"
            Image.new("RGB", (4, 4), "white").save(screenshot)

            class FakeLLM:
                def with_structured_output(self, _schema):
                    return object()

            with mock.patch.dict("os.environ", {"OPENAI_API_KEY": "sk-test"}), mock.patch(
                "langchain_openai.ChatOpenAI", return_value=FakeLLM()
            ), mock.patch(
                "agent_loop_system.tools.test.invoke_with_retry",
                side_effect=LLMRetryError("APIConnectionError: Connection error."),
            ):
                verdict = judge_test_with_vision(
                    "显示控制中心",
                    [{"path": str(screenshot)}],
                    ["显示控制中心"],
                )

        self.assertEqual(verdict.verdict, "CANNOT_VERIFY")
        self.assertIn("识图 Agent API 出错", verdict.reason)

    def test_runner_gate_returns_error_before_visual_judgement(self) -> None:
        result = CaseRunResult(
            case_id="DEMO_GATE",
            sheet="demo",
            expected_text="显示结果页",
            evidence_contract={
                "complete": False,
                "issues": [{"code": "business_action_missing", "message": "业务动作未执行"}],
            },
        )
        with mock.patch(
            "agent_loop_system.tools.test.judge_test_with_vision"
        ) as visual:
            decision = judge_case_result(result)

        self.assertEqual(decision.verdict, "ERROR")
        self.assertEqual(decision.reason, "业务动作未执行")
        visual.assert_not_called()

    def test_runner_gate_also_precedes_precomputed_agent_verdict(self) -> None:
        result = CaseRunResult(
            case_id="DYNAMIC_GATE",
            sheet="demo",
            expected_text="显示结果页",
            precomputed_verdict="PASS",
            precomputed_reason="Agent 判断符合预期",
            evidence_contract={
                "complete": False,
                "issues": [{"message": "Agent-loop 探索没有取得截图"}],
            },
        )
        decision = judge_case_result(result)

        self.assertEqual(decision.verdict, "ERROR")
        self.assertEqual(decision.reason, "Agent-loop 探索没有取得截图")

    def test_runner_gate_keeps_cannot_verify_for_complete_but_unreadable_visual_evidence(self) -> None:
        result = CaseRunResult(
            case_id="DEMO_VISUAL",
            sheet="demo",
            expected_text="显示结果页",
            evidence_contract={"complete": True, "issues": []},
            screenshots=[{"path": "screenshot.bmp", "label": "结果页"}],
            verification_points=["结果页"],
        )
        with mock.patch(
            "agent_loop_system.tools.test.judge_test_with_vision",
            return_value=Verdict(verdict="CANNOT_VERIFY", reason="截图内容无法辨认"),
        ):
            decision = judge_case_result(result)

        self.assertEqual(decision.verdict, "CANNOT_VERIFY")
        self.assertEqual(decision.reason, "截图内容无法辨认")


if __name__ == "__main__":
    unittest.main()
