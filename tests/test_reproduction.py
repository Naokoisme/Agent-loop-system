from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pydantic import ValidationError

from agent_loop_system.reproduction import (
    ReproductionAction,
    ReproductionDecision,
    ReproductionOutcome,
    ReproductionTrace,
    StepObservation,
    TreeStatus,
    interactive_reproduce,
    observe,
    successful_reproduction_commands,
)
from agent_loop_system.tools.build import BuildResult
from agent_loop_system.tools.simulator import CommandResult
from agent_loop_system.tools.test import Verdict


class FakeObservationSession:
    def __init__(self, *, tree_status: str = "ok", screenshot_ok: bool = True):
        self.tree_status = tree_status
        self.screenshot_ok = screenshot_ok
        self.events: list[tuple[str, dict]] = []

    def send(self, command: str, **kwargs) -> CommandResult:
        self.events.append((command, kwargs))
        if command.startswith(":GUI_PING:"):
            return CommandResult(
                request="gui_ping",
                status="processed",
                raw={"type": "gui_ack", "request": "gui_ping", "status": "processed"},
            )
        if command.startswith(":GUI_STATE:"):
            return CommandResult(
                request="gui_state",
                status="ok",
                raw={
                    "type": "gui_state",
                    "request": "gui_state",
                    "status": "ok",
                    "current_page": {"id": 415, "name": "NAP_TIPS"},
                    "popup": {"id": 901, "name": "NOTICE_POPUP"},
                },
            )
        if self.tree_status != "ok":
            return CommandResult(
                request="gui_tree",
                status=self.tree_status,
                raw={
                    "type": "command_result",
                    "request": "gui_tree",
                    "status": self.tree_status,
                    "reason": "transitioning",
                },
            )
        return CommandResult(
            request="gui_tree",
            status="ok",
            raw={"type": "gui_tree_end", "request": "gui_tree", "status": "ok"},
            lines=[
                json.dumps(
                    {
                        "type": "gui_tree_node",
                        "request": "gui_tree",
                        "effective_visible": True,
                        "text": "Nap Sleep",
                    }
                ),
                json.dumps(
                    {
                        "type": "gui_tree_node",
                        "request": "gui_tree",
                        "effective_visible": False,
                        "text": "Hidden",
                    }
                ),
                '{"type":"gui_tree_end","request":"gui_tree","status":"ok"}',
            ],
        )

    def capture_screenshot(self, path: str) -> bool:
        self.events.append(("capture", {"path": path}))
        if not self.screenshot_ok:
            return False
        Path(path).write_bytes(b"BMP")
        return True

    def lines_since(self, start_index: int) -> list[str]:
        return [
            '{"type":"command_result","request":"msg_test","status":"accepted"}',
            '{"type":"motor_event","event":"start","motor_type":5,"count":255}',
            '{"type":"audio_event","event":"prompt_play","value":70}',
            '{"type":"gui_ack","request":"gui_ping","status":"processed"}',
        ]


class FakeInteractiveSession(FakeObservationSession):
    def __init__(self, *, constant_screenshot: bool = False):
        super().__init__()
        self.constant_screenshot = constant_screenshot
        self.business_commands: list[str] = []
        self.system_commands: list[str] = []
        self.start_calls = 0
        self.stop_calls = 0

    def start(self) -> None:
        self.start_calls += 1

    def stop(self) -> None:
        self.stop_calls += 1

    def send(self, command: str, **kwargs) -> CommandResult:
        if command.startswith(":GUI_"):
            return super().send(command, **kwargs)
        self.events.append((command, kwargs))
        if command.startswith(":DISPLAY_TIME_SET:"):
            self.system_commands.append(command)
            return CommandResult(
                request="display_time_set",
                status="accepted",
                raw={
                    "type": "command_result",
                    "request": "display_time_set",
                    "status": "accepted",
                },
            )
        self.business_commands.append(command)
        request = command.lstrip(":").partition(":")[0].lower()
        status = "applied" if request == "sim_connection_set" else "accepted"
        return CommandResult(
            request=request,
            status=status,
            raw={"type": "command_result", "request": request, "status": status},
        )

    def capture_screenshot(self, path: str) -> bool:
        self.events.append(("capture", {"path": path}))
        marker = 0 if self.constant_screenshot else len(self.business_commands)
        Path(path).write_bytes(f"BMP-{marker}".encode())
        return True


class ReproductionDecisionTest(unittest.TestCase):
    def test_execute_requires_exactly_one_command_field(self) -> None:
        decision = ReproductionDecision(
            action=ReproductionAction.EXECUTE,
            command="  :ENTER_PAGE:NAP_TIPS,0  ",
            reason="进入目标窗口",
        )
        self.assertEqual(decision.command, ":ENTER_PAGE:NAP_TIPS,0")

        with self.assertRaisesRegex(ValidationError, "必须提供一条业务命令"):
            ReproductionDecision(
                action=ReproductionAction.EXECUTE,
                reason="进入目标窗口",
            )

    def test_non_execute_action_rejects_command(self) -> None:
        with self.assertRaisesRegex(ValidationError, "不得携带命令"):
            ReproductionDecision(
                action=ReproductionAction.READY_TO_JUDGE,
                command=":GUI_TREE:1",
                reason="证据已经充分",
            )


class StepObservationTest(unittest.TestCase):
    def test_screenshot_is_required_when_capture_succeeds(self) -> None:
        with self.assertRaisesRegex(ValidationError, "必须记录 screenshot_path"):
            StepObservation(step=1, screenshot_ok=True)

    def test_tree_unavailable_does_not_invalidate_screenshot(self) -> None:
        observation = StepObservation(
            step=1,
            screenshot_ok=True,
            screenshot_path="evidence/196883/reproduction/step_01.bmp",
            window_id=415,
            tree_status=TreeStatus.UNAVAILABLE,
            error="GUI_TREE transitioning",
        )
        self.assertTrue(observation.screenshot_ok)
        self.assertEqual(observation.tree_status, TreeStatus.UNAVAILABLE)

    def test_observe_waits_for_gui_and_writes_independent_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            session = FakeObservationSession()
            command_result = CommandResult(
                request="enter_page",
                status="accepted",
                raw={"type": "command_result", "request": "enter_page", "status": "accepted"},
            )
            decision = ReproductionDecision(
                action=ReproductionAction.EXECUTE,
                command=":ENTER_PAGE:NAP_TIPS,0",
                reason="进入目标窗口",
            )

            observation = observe(
                session,
                step=1,
                seq=100,
                evidence_dir=tempdir,
                decision=decision,
                command_result=command_result,
            )

            self.assertTrue(observation.screenshot_ok)
            self.assertEqual(observation.window_id, 415)
            self.assertEqual(observation.window_name, "NAP_TIPS")
            self.assertEqual(observation.popup_id, 901)
            self.assertEqual(observation.tree_status, TreeStatus.OK)
            self.assertEqual(observation.visible_texts, ["Nap Sleep"])
            self.assertEqual(observation.command_status, "accepted")
            self.assertEqual(
                [event[0] for event in session.events],
                [":GUI_PING:100", "capture", ":GUI_STATE:101", ":GUI_TREE:102"],
            )
            ping_options = session.events[0][1]
            self.assertEqual(ping_options["expected_type"], "gui_ack")
            self.assertEqual(ping_options["expected_status"], "processed")

            screenshot = Path(tempdir) / "step_01.bmp"
            evidence = Path(tempdir) / "step_01.json"
            self.assertTrue(screenshot.is_file())
            payload = json.loads(evidence.read_text(encoding="utf-8"))
            self.assertEqual(payload["screenshot_path"], str(screenshot.resolve()))
            self.assertEqual(payload["tree_status"], "OK")

    def test_observe_keeps_async_audio_and_motor_effects(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            session = FakeObservationSession()
            command_result = CommandResult(
                request="msg_test",
                status="accepted",
                raw={"type": "command_result", "request": "msg_test", "status": "accepted"},
                start_index=10,
            )

            observation = observe(
                session,
                step=1,
                seq=100,
                evidence_dir=tempdir,
                command_result=command_result,
            )

            self.assertEqual(
                [item["type"] for item in observation.command_results],
                ["command_result", "motor_event", "audio_event"],
            )

    def test_observe_persists_device_capture_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            session = FakeObservationSession()
            session.last_capture_metadata = {
                "sequence": 9436,
                "timestamp": 123.5,
                "width": 1280,
                "height": 720,
                "pixel_format": "NV12",
                "raw_path": str(Path(tempdir) / "step_00_capture_original.bmp"),
                "source": "watch_display",
                "transport": "hardware_serial",
                "payload_crc32": 0x1234ABCD,
                "chunks": 1608,
                "capture_duration_ms": 17,
                "device_uptime_ms": 123456,
                "pixel_source": "vde_lcd_composite",
            }
            observation = observe(
                session,
                step=0,
                seq=200,
                evidence_dir=tempdir,
            )
            self.assertEqual(observation.capture_metadata["sequence"], 9436)
            payload = json.loads(
                (Path(tempdir) / "step_00.json").read_text(encoding="utf-8")
            )
            self.assertEqual(payload["capture_metadata"]["pixel_format"], "NV12")
            self.assertEqual(payload["capture_metadata"]["source"], "watch_display")
            self.assertEqual(payload["capture_metadata"]["chunks"], 1608)
            self.assertEqual(
                payload["capture_metadata"]["pixel_source"],
                "vde_lcd_composite",
            )

    def test_tree_unavailable_does_not_block_screenshot_or_json(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            observation = observe(
                FakeObservationSession(tree_status="busy"),
                step=2,
                seq=200,
                evidence_dir=tempdir,
            )

            self.assertTrue(observation.screenshot_ok)
            self.assertEqual(observation.tree_status, TreeStatus.UNAVAILABLE)
            self.assertIn("GUI_TREE 不可用", observation.error or "")
            self.assertTrue((Path(tempdir) / "step_02.json").is_file())

    def test_screenshot_failure_still_writes_step_json(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            observation = observe(
                FakeObservationSession(screenshot_ok=False),
                step=3,
                seq=300,
                evidence_dir=tempdir,
            )

            self.assertFalse(observation.screenshot_ok)
            self.assertIsNone(observation.screenshot_path)
            self.assertTrue((Path(tempdir) / "step_03.json").is_file())


class ReproductionTraceTest(unittest.TestCase):
    def test_terminal_trace_is_json_serializable(self) -> None:
        decision = ReproductionDecision(
            action=ReproductionAction.READY_TO_JUDGE,
            reason="目标页面和文案已经清楚可见",
        )
        trace = ReproductionTrace(
            task_id="196883",
            steps=[
                StepObservation(
                    step=1,
                    decision=decision,
                    screenshot_ok=True,
                    screenshot_path="step_01.bmp",
                    window_id=415,
                    visible_texts=["Nap Sleep"],
                )
            ],
            outcome=ReproductionOutcome.CURRENT_CONFORMS,
            reason="当前截图符合缺陷原图中的预期",
        )

        payload = trace.model_dump(mode="json")
        json.dumps(payload, ensure_ascii=False)
        self.assertEqual(payload["outcome"], "CURRENT_CONFORMS")
        self.assertEqual(payload["steps"][0]["decision"]["action"], "READY_TO_JUDGE")

    def test_terminal_outcome_requires_reason(self) -> None:
        with self.assertRaisesRegex(ValidationError, "必须提供 reason"):
            ReproductionTrace(
                task_id="196883",
                outcome=ReproductionOutcome.SYSTEM_ERROR,
            )

    def test_successful_commands_exclude_failures_and_system_observation(self) -> None:
        trace = ReproductionTrace(
            task_id="T1",
            steps=[
                StepObservation(
                    step=0,
                    decision=ReproductionDecision(
                        action=ReproductionAction.EXECUTE,
                        command=":BAD:1",
                        reason="失败命令",
                    ),
                    command_status="error",
                    screenshot_ok=False,
                ),
                StepObservation(
                    step=1,
                    decision=ReproductionDecision(
                        action=ReproductionAction.EXECUTE,
                        command=":ENTER_PAGE:NAP_TIPS,0",
                        reason="进入目标",
                    ),
                    command_status="accepted",
                    screenshot_ok=False,
                ),
                StepObservation(
                    step=2,
                    decision=ReproductionDecision(
                        action=ReproductionAction.EXECUTE,
                        command=":GUI_TREE:3",
                        reason="不应由 Agent 执行",
                    ),
                    command_status="accepted",
                    screenshot_ok=False,
                ),
            ],
        )

        self.assertEqual(
            successful_reproduction_commands(trace),
            [":ENTER_PAGE:NAP_TIPS,0"],
        )


class InteractiveReproduceTest(unittest.TestCase):
    def _run(
        self,
        tempdir: str,
        session: FakeInteractiveSession,
        decisions: list[ReproductionDecision],
        *,
        judge: Verdict | None = None,
        validate_side_effect=None,
    ) -> tuple[ReproductionTrace, mock.Mock, mock.Mock]:
        decision_mock = mock.Mock(side_effect=decisions)
        build_mock = mock.Mock(
            return_value=BuildResult(success=True, artifact_path="D:/isolated/main.exe")
        )
        judge_mock = mock.Mock(
            return_value=judge or Verdict(verdict="PASS", reason="当前符合预期")
        )
        with (
            mock.patch(
                "agent_loop_system.tools.build.BuildConfig.from_env",
                return_value=object(),
            ),
            mock.patch("agent_loop_system.tools.build.run_build", build_mock),
            mock.patch(
                "agent_loop_system.tools.agent.decide_reproduction_action",
                decision_mock,
            ),
            mock.patch("agent_loop_system.tools.test.judge_with_vision", judge_mock),
            mock.patch(
                "agent_loop_system.reproduction.SimulatorSession",
                return_value=session,
            ),
            mock.patch(
                "agent_loop_system.reproduction.load_current_command_capabilities",
                return_value={},
            ),
            mock.patch(
                "agent_loop_system.reproduction.validate_agent_command",
                side_effect=validate_side_effect,
            ),
        ):
            trace = interactive_reproduce(
                task_id="196883",
                objective="Nap 页面描述与 UI 不一致",
                source_files=[{"path": "app/nap.c", "content": "void nap(void) {}"}],
                defect_image_paths=[],
                evidence_dir=tempdir,
            )
        self.judge_mock = judge_mock
        return trace, build_mock, decision_mock

    def test_builds_and_starts_once_then_returns_current_conforms(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            session = FakeInteractiveSession()
            trace, build_mock, decision_mock = self._run(
                tempdir,
                session,
                [
                    ReproductionDecision(
                        action=ReproductionAction.EXECUTE,
                        command=":ENTER_PAGE:NAP_TIPS,0",
                        reason="进入 Nap 注册窗口",
                    ),
                    ReproductionDecision(
                        action=ReproductionAction.READY_TO_JUDGE,
                        reason="目标文案已经清楚可见",
                    ),
                ],
            )

            self.assertEqual(trace.outcome, ReproductionOutcome.CURRENT_CONFORMS)
            self.assertEqual(build_mock.call_count, 1)
            self.assertEqual(session.start_calls, 1)
            self.assertEqual(session.stop_calls, 1)
            self.assertEqual(session.system_commands, [":DISPLAY_TIME_SET:300"])
            self.assertEqual(session.business_commands, [":ENTER_PAGE:NAP_TIPS,0"])
            self.assertNotIn("BUSINESS_GET", " ".join(session.business_commands))
            self.assertEqual(len(trace.steps), 2)
            self.assertEqual(decision_mock.call_count, 2)
            verified = self.judge_mock.call_args.kwargs["verified_observations"]
            self.assertTrue(any("执行命令 :ENTER_PAGE:NAP_TIPS,0" in item for item in verified))
            self.assertTrue(any("观察窗口 NAP_TIPS" in item for item in verified))
            payload = json.loads((Path(tempdir) / "trace.json").read_text(encoding="utf-8"))
            self.assertEqual(payload["outcome"], "CURRENT_CONFORMS")

    def test_test_case_visual_judgement_forwards_project(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            session = FakeInteractiveSession()
            visual_judge = mock.Mock(
                return_value=Verdict(verdict="PASS", reason="中英文文案等价")
            )
            with (
                mock.patch(
                    "agent_loop_system.tools.build.BuildConfig.from_env",
                    return_value=object(),
                ),
                mock.patch(
                    "agent_loop_system.tools.build.run_build",
                    return_value=BuildResult(
                        success=True,
                        artifact_path="D:/isolated/main.exe",
                    ),
                ),
                mock.patch(
                    "agent_loop_system.tools.agent.decide_reproduction_action",
                    return_value=ReproductionDecision(
                        action=ReproductionAction.READY_TO_JUDGE,
                        reason="目标文案已经清楚可见",
                    ),
                ),
                mock.patch(
                    "agent_loop_system.tools.test.judge_test_with_vision",
                    visual_judge,
                ),
                mock.patch(
                    "agent_loop_system.reproduction.SimulatorSession",
                    return_value=session,
                ),
                mock.patch(
                    "agent_loop_system.reproduction.load_current_command_capabilities",
                    return_value={},
                ),
            ):
                trace = interactive_reproduce(
                    task_id="6202-locale",
                    objective="验证 Timer 入口",
                    source_files=[],
                    defect_image_paths=[],
                    evidence_dir=tempdir,
                    test_case={
                        "expected_text": "显示 Timer 入口",
                        "project": "6202_W5230",
                    },
                )

            self.assertEqual(trace.outcome, ReproductionOutcome.CURRENT_CONFORMS)
            visual_judge.assert_called_once()
            self.assertEqual(visual_judge.call_args.args[0], "显示 Timer 入口")
            self.assertEqual(
                visual_judge.call_args.kwargs["project"],
                "6202_W5230",
            )

    def test_hardware_target_skips_simulator_build_and_display_time_change(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            session = FakeInteractiveSession()
            decision = ReproductionDecision(
                action=ReproductionAction.READY_TO_JUDGE,
                reason="真机当前画面足够清楚",
            )
            hardware_config = mock.Mock(source_root=Path(tempdir))
            with (
                mock.patch(
                    "agent_loop_system.tools.build.BuildConfig.from_env"
                ) as build_config,
                mock.patch("agent_loop_system.tools.build.run_build") as run_build,
                mock.patch(
                    "agent_loop_system.tools.hardware_target.HardwareTargetConfig.from_env",
                    return_value=hardware_config,
                ),
                mock.patch(
                    "agent_loop_system.tools.hardware_target.load_hardware_command_capabilities",
                    return_value={},
                ),
                mock.patch(
                    "agent_loop_system.tools.hardware_target.build_hardware_agent_knowledge",
                    return_value="GUI_PING | GUI_STATE | GUI_TREE | ENTER_PAGE",
                ),
                mock.patch(
                    "agent_loop_system.tools.real_device.RealDeviceSession",
                    return_value=session,
                ) as real_session,
                mock.patch(
                    "agent_loop_system.tools.agent.decide_reproduction_action",
                    return_value=decision,
                ) as decide,
                mock.patch(
                    "agent_loop_system.tools.test.judge_with_vision",
                    return_value=Verdict(verdict="PASS", reason="当前符合预期"),
                ),
            ):
                trace = interactive_reproduce(
                    task_id="6202-smoke",
                    objective="真机页面检查",
                    source_files=[{"path": "app/a.c", "content": "void a(void) {}"}],
                    defect_image_paths=[],
                    evidence_dir=tempdir,
                    target="hardware",
                )

            self.assertEqual(trace.outcome, ReproductionOutcome.CURRENT_CONFORMS)
            build_config.assert_not_called()
            run_build.assert_not_called()
            real_session.assert_called_once_with(evidence_dir=Path(tempdir).resolve())
            self.assertEqual(session.system_commands, [])
            self.assertEqual(session.start_calls, 1)
            self.assertEqual(session.stop_calls, 1)
            self.assertEqual(decide.call_args.kwargs["execution_target"], "hardware")
            self.assertEqual(
                decide.call_args.kwargs["capability_knowledge"],
                "GUI_PING | GUI_STATE | GUI_TREE | ENTER_PAGE",
            )

    def test_two_unchanged_steps_stop_as_target_not_reached(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            session = FakeInteractiveSession(constant_screenshot=True)
            decisions = [
                ReproductionDecision(
                    action=ReproductionAction.EXECUTE,
                    command=":ENTER_PAGE:UNKNOWN,0",
                    reason="继续寻找目标",
                )
                for _ in range(3)
            ]
            trace, _, decision_mock = self._run(tempdir, session, decisions)

            self.assertEqual(trace.outcome, ReproductionOutcome.TARGET_NOT_REACHED)
            self.assertIn("连续 2 步", trace.reason or "")
            self.assertEqual(decision_mock.call_count, 2)
            self.assertEqual(len(trace.steps), 3)

    def test_successful_background_state_command_does_not_count_as_no_progress(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            session = FakeInteractiveSession(constant_screenshot=True)
            trace, _, decision_mock = self._run(
                tempdir,
                session,
                [
                    ReproductionDecision(
                        action=ReproductionAction.EXECUTE,
                        command=":SIM_CONNECTION_SET:2,app,1",
                        reason="准备连接状态",
                    ),
                    ReproductionDecision(
                        action=ReproductionAction.EXECUTE,
                        command=":ENTER_PAGE:VOICE_ASSISTANT_INTERACTION,0",
                        reason="连接后重新进入目标页面",
                    ),
                    ReproductionDecision(
                        action=ReproductionAction.READY_TO_JUDGE,
                        reason="继续判断当前页面",
                    ),
                ],
            )

            self.assertEqual(trace.outcome, ReproductionOutcome.CURRENT_CONFORMS)
            self.assertEqual(decision_mock.call_count, 3)
            self.assertEqual(
                session.business_commands,
                [
                    ":SIM_CONNECTION_SET:2,app,1",
                    ":ENTER_PAGE:VOICE_ASSISTANT_INTERACTION,0",
                ],
            )

    def test_only_one_command_error_correction_is_allowed(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            session = FakeInteractiveSession(constant_screenshot=True)
            decisions = [
                ReproductionDecision(
                    action=ReproductionAction.EXECUTE,
                    command=":BAD:1",
                    reason="尝试命令",
                )
                for _ in range(2)
            ]
            trace, _, _ = self._run(
                tempdir,
                session,
                decisions,
                validate_side_effect=ValueError("当前源码未注册"),
            )

            self.assertEqual(trace.outcome, ReproductionOutcome.COMMAND_RUNTIME_ERROR)
            self.assertEqual(trace.steps[-1].command_status, "error")
            self.assertEqual(session.business_commands, [])

    def test_successful_command_resets_consecutive_error_count(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            session = FakeInteractiveSession()
            decisions = [
                ReproductionDecision(
                    action=ReproductionAction.EXECUTE,
                    command=":BAD_FIRST:1",
                    reason="第一次命令错误",
                ),
                ReproductionDecision(
                    action=ReproductionAction.EXECUTE,
                    command=":GOOD:1",
                    reason="修正后命令成功",
                ),
                ReproductionDecision(
                    action=ReproductionAction.EXECUTE,
                    command=":BAD_LATER:1",
                    reason="之后出现新的独立错误",
                ),
                ReproductionDecision(
                    action=ReproductionAction.READY_TO_JUDGE,
                    reason="目标内容已经可见",
                ),
            ]
            trace, _, decision_mock = self._run(
                tempdir,
                session,
                decisions,
                validate_side_effect=[
                    ValueError("第一次失败"),
                    None,
                    ValueError("第二次失败"),
                ],
            )

            self.assertEqual(trace.outcome, ReproductionOutcome.CURRENT_CONFORMS)
            self.assertEqual(decision_mock.call_count, 4)
            self.assertEqual(session.business_commands, [":GOOD:1"])


if __name__ == "__main__":
    unittest.main()
