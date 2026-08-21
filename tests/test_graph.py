from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_loop_system.graph import build_graph
from agent_loop_system.main import _normalize_result
from agent_loop_system.reproduction import (
    ReproductionAction,
    ReproductionDecision,
    ReproductionOutcome,
    ReproductionTrace,
    StepObservation,
)
from agent_loop_system.tools.agent import Patch
from agent_loop_system.tools.build import BuildResult
from agent_loop_system.tools.designer import (
    DesignerApplyResult,
    DesignerContext,
    DesignerPlan,
)
from agent_loop_system.tools.llm_retry import LLMRetryError
from agent_loop_system.tools.simulator import CommandResult
from agent_loop_system.tools.test import Verdict


class FakeSession:
    """修复后验证使用的模拟器替身，支持统一 observe() 协议。"""

    def __init__(self, exe):
        self.exe = exe
        self.sent: list[str] = []

    def start(self) -> None:
        pass

    def send(self, cmd: str, **_kwargs) -> CommandResult:
        self.sent.append(cmd)
        if cmd.startswith(":GUI_PING:"):
            return CommandResult(
                request="gui_ping",
                status="processed",
                raw={"request": "gui_ping", "type": "gui_ack", "status": "processed"},
            )
        if cmd.startswith(":GUI_STATE:"):
            return CommandResult(
                request="gui_state",
                status="ok",
                raw={
                    "request": "gui_state",
                    "type": "gui_state",
                    "status": "ok",
                    "current_page": {"id": 1, "name": "TEST"},
                    "popup": None,
                },
            )
        if cmd.startswith(":GUI_TREE:"):
            return CommandResult(
                request="gui_tree",
                status="ok",
                raw={"request": "gui_tree", "type": "gui_tree_end", "status": "ok"},
                lines=['{"request":"gui_tree","type":"gui_tree_end","status":"ok"}'],
            )
        request = cmd.lstrip(":").partition(":")[0].lower()
        return CommandResult(
            request=request,
            status="accepted",
            raw={"request": request, "type": "command_result", "status": "accepted"},
        )

    def capture_screenshot(self, path: str) -> bool:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"BMP")
        return True

    def stop(self) -> None:
        pass


def _patch(**overrides) -> Patch:
    fields = dict(
        root_cause_analysis="推理",
        file_path="app/a.c",
        before="before();",
        after="after();",
        reason="fix bug",
        test_commands=[],
    )
    fields.update(overrides)
    return Patch(**fields)


class GraphFlowTest(unittest.TestCase):
    def _run(
        self,
        *,
        reproduction_outcome: ReproductionOutcome = ReproductionOutcome.DEFECT_REPRODUCED,
        reproduction_commands: list[str] | None = None,
        test_verdicts: list[str] | None = None,
        patch_obj: Patch | None = None,
        generate_error: str | None = None,
        generate_invalid_on: int | None = None,
        build_results: list[BuildResult] | None = None,
        rollback_error: str | None = None,
        source_files: tuple[str, ...] = ("app/a.c",),
        max_attempts: int = 5,
        task_id: str = "T1",
        designer_enabled: bool = True,
        target: str | None = None,
    ) -> tuple[dict, Path, mock.Mock]:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        src = root / "app" / "a.c"
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_text("before();\n", encoding="utf-8")

        commands = (
            list(reproduction_commands)
            if reproduction_commands is not None
            else [":ENTER_PAGE:TEST,0"]
        )
        verdict_iter = iter(test_verdicts or ["PASS"])
        patch_obj = patch_obj or _patch()
        build_results = list(build_results or [])
        generate_calls = [0]

        def fake_interactive_reproduce(**kwargs) -> ReproductionTrace:
            evidence_dir = Path(kwargs["evidence_dir"])
            evidence_dir.mkdir(parents=True, exist_ok=True)
            screenshot = evidence_dir / "step_00.bmp"
            screenshot.write_bytes(b"BMP")
            steps: list[StepObservation] = []
            if commands:
                for index, command in enumerate(commands):
                    step_screenshot = evidence_dir / f"step_{index:02d}.bmp"
                    step_screenshot.write_bytes(b"BMP")
                    steps.append(
                        StepObservation(
                            step=index,
                            decision=ReproductionDecision(
                                action=ReproductionAction.EXECUTE,
                                command=command,
                                reason="测试动作",
                            ),
                            command_status="accepted",
                            command_results=[
                                {
                                    "type": "command_result",
                                    "request": command,
                                    "status": "accepted",
                                }
                            ],
                            screenshot_ok=True,
                            screenshot_path=str(step_screenshot),
                        )
                    )
            else:
                steps.append(
                    StepObservation(
                        step=0,
                        screenshot_ok=True,
                        screenshot_path=str(screenshot),
                    )
                )
            trace = ReproductionTrace(
                task_id=kwargs["task_id"],
                steps=steps,
                outcome=reproduction_outcome,
                reason=f"outcome={reproduction_outcome.value}",
            )
            (evidence_dir / "trace.json").write_text(
                json.dumps(trace.model_dump(mode="json"), ensure_ascii=False),
                encoding="utf-8",
            )
            return trace

        def fake_judge_vision(
            screenshot_path,
            defect_criteria,
            reference_screenshot=None,
            defect_image_paths=None,
        ) -> Verdict:
            return Verdict(verdict=next(verdict_iter), reason="judged")

        designer_context = DesignerContext(
            project_path=str(root / "app" / "projects" / "P"),
            ui_layout_name="layout",
            page_name="TEST",
            page={},
        )

        def fake_generate(**kwargs):
            generate_calls[0] += 1
            if generate_error:
                raise LLMRetryError(generate_error)
            if generate_invalid_on and generate_calls[0] == generate_invalid_on:
                return DesignerPlan(
                    root_cause_analysis="invalid",
                    page_name="OTHER",
                    layout_operations=[{"op": "set", "widgetId": "1", "properties": {}}],
                    reason="invalid",
                )
            return DesignerPlan(
                root_cause_analysis=patch_obj.root_cause_analysis,
                page_name="TEST",
                layout_operations=[{"op": "set", "widgetId": "1", "properties": {}}],
                reason=patch_obj.reason,
            )

        def fake_apply_designer_plan(**kwargs):
            content = src.read_text(encoding="utf-8")
            if patch_obj.before not in content:
                return DesignerApplyResult(success=False, error="before 文本在文件中未找到")
            src.write_text(
                content.replace(patch_obj.before, patch_obj.after, 1), encoding="utf-8"
            )
            return DesignerApplyResult(
                success=True,
                page_json_path="app/windows/layout/gui_win_test.json",
                changed_files=["app/a.c"],
                transaction_manifest="fake-manifest",
            )

        def fake_rollback_designer(_manifest):
            if rollback_error:
                raise RuntimeError(rollback_error)
            content = src.read_text(encoding="utf-8")
            if patch_obj.after not in content:
                raise RuntimeError("Designer 文件在回滚前又被修改")
            src.write_text(
                content.replace(patch_obj.after, patch_obj.before, 1), encoding="utf-8"
            )

        def fake_build(_config):
            if build_results:
                return build_results.pop(0)
            return BuildResult(success=True)

        run_build = mock.Mock(side_effect=fake_build)
        interactive = mock.Mock(side_effect=fake_interactive_reproduce)
        repair_generate = mock.Mock(side_effect=fake_generate)
        run_build.interactive_reproduce = interactive
        run_build.repair_generate = repair_generate

        environment = {"W30_SOURCE_ROOT": temporary.name}
        if target == "hardware":
            environment.update(
                {
                    "W30_HARDWARE_SOURCE_ROOT": temporary.name,
                    "W30_HARDWARE_WORKSPACE_ROOT": temporary.name,
                    "W30_HARDWARE_PROJECT": "6202_W5230",
                }
            )
            app = root / "app"
            (app / "projects" / "6202_W5230").mkdir(parents=True, exist_ok=True)
            (app / "comm" / "TuoBu" / "quick_cmd").mkdir(parents=True, exist_ok=True)
            (root / "core" / "comm" / "srv" / "test").mkdir(parents=True, exist_ok=True)
            (app / "ProjectConfig.cmake").write_text(
                "set(PROJECT 6202_W5230)\n", encoding="utf-8"
            )
            for path in (
                app / "projects" / "6202_W5230" / "Project.cmake",
                app / "comm" / "TuoBu" / "quick_cmd" / "gui_comm_quick_cmd.c",
                root / "core" / "comm" / "srv" / "test" / "srv_quick_cmd_handler.c",
                root / "core" / "comm" / "srv" / "test" / "hlq_quick_cmd_handler.c",
            ):
                path.write_text("// test\n", encoding="utf-8")

        with mock.patch.dict(os.environ, environment):
            with (
                mock.patch("agent_loop_system.graph.BuildConfig"),
                mock.patch("agent_loop_system.graph.run_build", run_build),
                mock.patch(
                    "agent_loop_system.graph.run_interactive_reproduction",
                    interactive,
                ),
                mock.patch(
                    "agent_loop_system.tools.simulator.SimulatorSession",
                    side_effect=lambda exe: FakeSession(exe),
                ),
                mock.patch(
                    "agent_loop_system.tools.test.judge_with_vision",
                    side_effect=fake_judge_vision,
                ),
                mock.patch(
                    "agent_loop_system.tools.agent.generate_designer_plan", repair_generate
                ),
                mock.patch(
                    "agent_loop_system.tools.designer.load_designer_context",
                    return_value=designer_context,
                ),
                mock.patch(
                    "agent_loop_system.tools.designer.apply_designer_plan",
                    side_effect=fake_apply_designer_plan,
                ),
                mock.patch(
                    "agent_loop_system.tools.designer.rollback_designer_transaction",
                    side_effect=fake_rollback_designer,
                ),
            ):
                invoke = {
                    "task_id": task_id,
                    "objective": "bug",
                    "judge_criteria": "bug",
                    "max_attempts": max_attempts,
                    "test_cases": [],
                    "source_files": list(source_files),
                    "designer_enabled": designer_enabled,
                }
                if target is not None:
                    invoke["target"] = target
                result = build_graph().invoke(invoke)
        return _normalize_result(result), src, run_build

    def _evidence(self, task_id: str = "T1") -> Path:
        return Path(r"d:\Agent-loop-system\evidence") / task_id

    def test_new_graph_calls_interactive_reproduce_once(self) -> None:
        result, _, run_build = self._run(
            reproduction_outcome=ReproductionOutcome.CURRENT_CONFORMS,
        )
        self.assertEqual(result["verdict"], "PASS")
        self.assertEqual(result["reproduction_outcome"], "CURRENT_CONFORMS")
        self.assertEqual(result["reproduction_attempts"], 1)
        run_build.interactive_reproduce.assert_called_once()
        self.assertEqual(
            run_build.interactive_reproduce.call_args.kwargs["target"], "simulator"
        )
        run_build.repair_generate.assert_not_called()
        self.assertEqual(result["history"], [])

    def test_hardware_target_is_forwarded_and_current_conforms_stops_cleanly(self) -> None:
        result, _, run_build = self._run(
            target="hardware",
            reproduction_outcome=ReproductionOutcome.CURRENT_CONFORMS,
        )
        self.assertEqual(result["verdict"], "PASS")
        self.assertEqual(
            run_build.interactive_reproduce.call_args.kwargs["target"], "hardware"
        )
        run_build.repair_generate.assert_not_called()

    def test_hardware_defect_reproduction_requires_flash_before_repair(self) -> None:
        result, src, run_build = self._run(target="hardware")
        self.assertEqual(result["verdict"], "CANNOT_VERIFY")
        self.assertEqual(result["error_code"], "HARDWARE_FLASH_REQUIRED")
        self.assertIn("人工授权刷入设备", result["error"])
        self.assertEqual(src.read_text(encoding="utf-8"), "before();\n")
        run_build.repair_generate.assert_not_called()

    def test_designer_disabled_fails_closed_without_source_patch(self) -> None:
        result, src, run_build = self._run(designer_enabled=False)
        self.assertEqual(result["verdict"], "CANNOT_VERIFY")
        self.assertEqual(result["error_code"], "DESIGNER_REQUIRED")
        self.assertEqual(src.read_text(encoding="utf-8"), "before();\n")
        run_build.repair_generate.assert_not_called()

    def test_only_defect_reproduced_enters_repair_agent(self) -> None:
        for outcome in (
            ReproductionOutcome.TARGET_NOT_REACHED,
            ReproductionOutcome.REFERENCE_AMBIGUOUS,
            ReproductionOutcome.CAPABILITY_MISSING,
            ReproductionOutcome.COMMAND_RUNTIME_ERROR,
            ReproductionOutcome.SYSTEM_ERROR,
        ):
            with self.subTest(outcome=outcome):
                result, src, run_build = self._run(reproduction_outcome=outcome)
                self.assertEqual(result["verdict"], "CANNOT_VERIFY")
                self.assertEqual(result["reproduction_outcome"], outcome.value)
                run_build.repair_generate.assert_not_called()
                self.assertEqual(src.read_text(encoding="utf-8"), "before();\n")

    def test_reproduced_defect_continues_and_reuses_successful_commands(self) -> None:
        result, src, run_build = self._run(test_verdicts=["PASS"])
        self.assertEqual(result["verdict"], "PASS")
        self.assertTrue(result["baseline_output"]["defect_reproduced"])
        self.assertEqual(result["agent_test_commands"], [":ENTER_PAGE:TEST,0"])
        self.assertEqual(result["history"][0]["test_commands"], [":ENTER_PAGE:TEST,0"])
        run_build.repair_generate.assert_called_once()
        self.assertEqual(src.read_text(encoding="utf-8"), "after();\n")

    def test_initial_visible_defect_allows_empty_business_command_list(self) -> None:
        result, src, run_build = self._run(reproduction_commands=[], test_verdicts=["PASS"])
        self.assertEqual(result["verdict"], "PASS")
        self.assertEqual(result["agent_test_commands"], [])
        run_build.repair_generate.assert_called_once()
        self.assertEqual(src.read_text(encoding="utf-8"), "after();\n")

    def test_agent_llm_failure_is_recorded_without_graph_reproduction_retry(self) -> None:
        result, _, run_build = self._run(generate_error="empty response", max_attempts=3)
        self.assertEqual(result["verdict"], "CANNOT_VERIFY")
        self.assertEqual(len(result["history"]), 1)
        run_build.interactive_reproduce.assert_called_once()
        self.assertEqual(run_build.call_count, 0)

    def test_apply_failure_skips_build_and_test(self) -> None:
        result, src, run_build = self._run(
            patch_obj=_patch(before="missing();"),
            max_attempts=1,
        )
        self.assertEqual(result["verdict"], "FAIL")
        self.assertEqual(src.read_text(encoding="utf-8"), "before();\n")
        self.assertEqual(run_build.call_count, 0)

    def test_final_state_is_json_serializable(self) -> None:
        result, _, _ = self._run(test_verdicts=["PASS"])
        json.dumps(result)

    def test_test_fail_rolls_back_and_retries(self) -> None:
        result, src, run_build = self._run(test_verdicts=["FAIL", "PASS"])
        self.assertEqual(result["verdict"], "PASS")
        self.assertEqual(len(result["history"]), 2)
        self.assertTrue(result["history"][0]["rolled_back"])
        self.assertEqual(run_build.interactive_reproduce.call_count, 1)
        self.assertEqual(run_build.call_count, 2)
        self.assertEqual(src.read_text(encoding="utf-8"), "after();\n")

    def test_test_cannot_verify_rolls_back_and_restores_artifact(self) -> None:
        result, src, run_build = self._run(test_verdicts=["CANNOT_VERIFY"])
        self.assertEqual(result["verdict"], "CANNOT_VERIFY")
        self.assertTrue(result["history"][0]["rolled_back"])
        self.assertEqual(src.read_text(encoding="utf-8"), "before();\n")
        self.assertIsNotNone(result["restore_build_result"])
        self.assertEqual(run_build.call_count, 2)

    def test_test_pass_retains_patch(self) -> None:
        result, src, _ = self._run(test_verdicts=["PASS"])
        self.assertEqual(result["patch_retained"], True)
        self.assertFalse(result["history"][0]["rolled_back"])
        self.assertEqual(src.read_text(encoding="utf-8"), "after();\n")

    def test_rollback_failure_blocks_retry(self) -> None:
        result, _, _ = self._run(
            test_verdicts=["FAIL"],
            rollback_error="offset 处内容不等于 after，拒绝回滚",
        )
        self.assertEqual(result["verdict"], "FAIL")
        self.assertEqual(len(result["history"]), 1)
        self.assertIn("拒绝回滚", result["rollback_error"])

    def test_second_repair_round_preserves_before_and_cleans_after(self) -> None:
        with mock.patch("agent_loop_system.graph._clean_after_evidence") as cleaner:
            result, _, _ = self._run(test_verdicts=["FAIL", "PASS"])
        self.assertEqual(cleaner.call_count, 2)
        self.assertEqual(result["verdict"], "PASS")
        self.assertTrue((self._evidence() / "before.bmp").exists())
        self.assertTrue((self._evidence() / "after.bmp").exists())

    def test_history_fields_complete(self) -> None:
        result, _, _ = self._run(test_verdicts=["FAIL"], max_attempts=1)
        expected = {
            "attempt", "verdict", "patch", "patch_reason", "test_commands",
            "baseline_output", "build_success", "build_result", "test_output",
            "error", "rolled_back", "rollback_error",
            "restore_build_result", "restore_build_error",
            "repair_mode", "designer_plan", "designer_context", "designer_transaction",
        }
        self.assertEqual(set(result["history"][0].keys()), expected)

    def test_restore_build_failure_is_recorded(self) -> None:
        result, _, _ = self._run(
            test_verdicts=["CANNOT_VERIFY"],
            build_results=[
                BuildResult(success=True),
                BuildResult(success=False, error_message="restore failed"),
            ],
        )
        self.assertIn("restore failed", result["restore_build_error"])
        self.assertIn("restore failed", result["history"][0]["restore_build_error"])

    def test_build_failure_uses_before_as_after_placeholder(self) -> None:
        result, _, run_build = self._run(
            build_results=[BuildResult(success=False, error_message="compile error")],
            max_attempts=1,
        )
        self.assertEqual(result["verdict"], "FAIL")
        evidence = self._evidence()
        self.assertEqual(
            (evidence / "before.bmp").read_bytes(),
            (evidence / "after.bmp").read_bytes(),
        )
        self.assertIn("占位", result["history"][0]["test_output"]["evidence_issue"])
        self.assertEqual(run_build.call_count, 1)

    def test_build_success_then_agent_failure_restores_artifact(self) -> None:
        result, _, run_build = self._run(
            test_verdicts=["FAIL"],
            build_results=[BuildResult(success=True), BuildResult(success=True)],
            max_attempts=2,
            generate_invalid_on=2,
        )
        self.assertEqual(result["verdict"], "FAIL")
        self.assertEqual(result["error_code"], "DESIGNER_PLAN_INVALID")
        self.assertIsNotNone(result["restore_build_result"])
        self.assertEqual(run_build.call_count, 2)

    def test_result_contains_new_reproduction_contract(self) -> None:
        result, _, _ = self._run(
            reproduction_outcome=ReproductionOutcome.CURRENT_CONFORMS,
        )
        for key in (
            "reproduction_outcome",
            "reproduction_trace",
            "baseline_output",
            "agent_test_commands",
        ):
            self.assertIn(key, result)

    def test_validate_rejects_bad_task_id_before_reproduction(self) -> None:
        result, _, run_build = self._run(task_id="../bad", generate_error="unused")
        self.assertEqual(result["verdict"], "CANNOT_VERIFY")
        self.assertEqual(result["history"], [])
        run_build.interactive_reproduce.assert_not_called()
        run_build.repair_generate.assert_not_called()


if __name__ == "__main__":
    unittest.main()
