from __future__ import annotations

import json
import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_loop_system.tools import case_map
from agent_loop_system.tools.case_map import CaseEntry, CaseRunResult, load_case_map, run_case
from agent_loop_system.tools.simulator import CommandResult


class _FakeSession:
    def __init__(self, rejected: str = "", unavailable_screen_off: bool = False) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.captures: list[str] = []
        self.rejected = rejected
        self.unavailable_screen_off = unavailable_screen_off

    def send(self, raw: str, **kwargs) -> CommandResult:
        self.calls.append((raw, kwargs))
        name = raw[1:].partition(":")[0]
        if name == self.rejected:
            return CommandResult(name.lower(), "rejected", {"status": "rejected"})
        if name == "GUI_PING":
            return CommandResult(
                "gui_ping",
                "processed",
                {"type": "gui_ack", "request": "gui_ping", "status": "processed"},
            )
        if name == "GUI_TREE":
            if self.unavailable_screen_off:
                return CommandResult(
                    "gui_tree",
                    "unavailable",
                    {
                        "type": "command_result",
                        "request": "gui_tree",
                        "status": "unavailable",
                        "reason": "screen_off",
                    },
                )
            return CommandResult(
                "gui_tree",
                "ok",
                {"type": "gui_tree_end", "request": "gui_tree", "status": "ok"},
            )
        return CommandResult(
            name.lower(),
            "accepted",
            {"type": "command_result", "request": name.lower(), "status": "accepted"},
        )

    def capture_screenshot(self, output_path: str) -> bool:
        self.captures.append(output_path)
        Path(output_path).write_bytes(b"BM-test")
        return True


class CaseMapExecutionTest(unittest.TestCase):
    def test_default_case_map_root_follows_the_runtime_root(self) -> None:
        from agent_loop_system.runtime_root import RuntimePaths

        self.assertEqual(case_map.CASE_MAP_DIR, RuntimePaths.from_root().case_map)

    def test_target_specific_loader_does_not_fall_back(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            simulator = root / "620C_simulator_case_map"
            hardware = root / "6202_case_map"
            simulator.mkdir()
            hardware.mkdir()
            (simulator / "demo.json").write_text(
                json.dumps([{"case_id": "SIM_001", "sheet": "demo"}]),
                encoding="utf-8",
            )
            with mock.patch.object(case_map, "CASE_MAP_TARGET_DIRS", {
                "simulator": simulator,
                "hardware": hardware,
            }):
                self.assertIn("SIM_001", load_case_map("demo", target="simulator"))
                with self.assertRaisesRegex(FileNotFoundError, "尚未迁移"):
                    load_case_map("demo", target="hardware")

    def test_6202_simulator_profile_is_separate_from_hardware(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            simulator = root / "6202_simulator_case_map"
            simulator.mkdir()
            (simulator / "demo.json").write_text(
                json.dumps({
                    "profile": "6202_W5230_SIMULATOR",
                    "sheet": "demo",
                    "cases": [{"case_id": "SIM6202_001", "sheet": "demo"}],
                }),
                encoding="utf-8",
            )
            with (
                mock.patch.object(case_map, "CASE_MAP_PROFILE_DIRS", {
                    **case_map.CASE_MAP_PROFILE_DIRS,
                    "6202_W5230_SIMULATOR": simulator,
                }),
                mock.patch.object(case_map, "CASE_MAP_PROFILE_TARGETS", {
                    **case_map.CASE_MAP_PROFILE_TARGETS,
                    "6202_W5230_SIMULATOR": "simulator",
                }),
            ):
                loaded = load_case_map(
                    "demo",
                    target="simulator",
                    profile="6202_W5230_SIMULATOR",
                )
                self.assertIn("SIM6202_001", loaded)
                with self.assertRaisesRegex(ValueError, "不属于执行目标"):
                    load_case_map(
                        "demo",
                        target="hardware",
                        profile="6202_W5230_SIMULATOR",
                    )

    def test_legacy_unsupported_sheet_preserves_case_for_dynamic_execution(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            simulator = Path(temporary)
            (simulator / "removed.json").write_text(
                json.dumps({
                    "profile": "6202_W5230_SIMULATOR",
                    "sheet": "removed",
                    "supported": False,
                    "unavailable_reason": "项目未启用该模块",
                    "cases": [{
                        "case_id": "REMOVED_001",
                        "sheet": "removed",
                        "setup": [":ENTER_PAGE:REMOVED,0"],
                        "collect": [":HOST_SCREENSHOT:1"],
                    }],
                }, ensure_ascii=False),
                encoding="utf-8",
            )
            with mock.patch.object(case_map, "CASE_MAP_PROFILE_DIRS", {
                **case_map.CASE_MAP_PROFILE_DIRS,
                "6202_W5230_SIMULATOR": simulator,
            }):
                loaded = load_case_map(
                    "removed",
                    target="simulator",
                    profile="6202_W5230_SIMULATOR",
                )["REMOVED_001"]

            self.assertFalse(loaded.unable)
            self.assertEqual(loaded.setup, [":ENTER_PAGE:REMOVED,0"])
            self.assertEqual(loaded.collect, [":HOST_SCREENSHOT:1"])

    def test_loader_rejects_sheet_path_traversal_before_reading_another_profile(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            selected = root / "620C_simulator_case_map"
            other = root / "6202_simulator_case_map"
            selected.mkdir()
            other.mkdir()
            (other / "secret.json").write_text(
                json.dumps({
                    "profile": "6202_W5230_SIMULATOR",
                    "sheet": "secret",
                    "cases": [{"case_id": "OTHER_001", "sheet": "secret"}],
                }),
                encoding="utf-8",
            )
            with mock.patch.object(case_map, "CASE_MAP_PROFILE_DIRS", {
                **case_map.CASE_MAP_PROFILE_DIRS,
                "620C_W6830": selected,
                "6202_W5230_SIMULATOR": other,
            }):
                with self.assertRaisesRegex(ValueError, "单一模块名"):
                    load_case_map(
                        r"..\6202_simulator_case_map\secret",
                        target="simulator",
                        profile="620C_W6830",
                    )

    def test_loader_rejects_profile_and_sheet_metadata_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            selected = Path(temporary)
            map_path = selected / "demo.json"
            with mock.patch.object(case_map, "CASE_MAP_PROFILE_DIRS", {
                **case_map.CASE_MAP_PROFILE_DIRS,
                "6202_W5230_SIMULATOR": selected,
            }):
                map_path.write_text(
                    json.dumps({
                        "profile": "620C_W6830",
                        "sheet": "demo",
                        "cases": [{"case_id": "DEMO_001", "sheet": "demo"}],
                    }),
                    encoding="utf-8",
                )
                with self.assertRaisesRegex(ValueError, "profile 不匹配"):
                    load_case_map(
                        "demo",
                        target="simulator",
                        profile="6202_W5230_SIMULATOR",
                    )

                for invalid_cases in (
                    "not-an-array",
                    [{"case_id": "DEMO_001", "sheet": "demo"}, 42],
                    [],
                ):
                    with self.subTest(invalid_cases=invalid_cases):
                        map_path.write_text(
                            json.dumps({
                                "profile": "6202_W5230_SIMULATOR",
                                "sheet": "demo",
                                "cases": invalid_cases,
                            }),
                            encoding="utf-8",
                        )
                        with self.assertRaisesRegex(ValueError, "cases"):
                            load_case_map(
                                "demo",
                                target="simulator",
                                profile="6202_W5230_SIMULATOR",
                            )

                map_path.write_text(
                    json.dumps({
                        "profile": "6202_W5230_SIMULATOR",
                        "sheet": "other",
                        "cases": [{"case_id": "DEMO_001", "sheet": "other"}],
                    }),
                    encoding="utf-8",
                )
                with self.assertRaisesRegex(ValueError, "sheet 不匹配"):
                    load_case_map(
                        "demo",
                        target="simulator",
                        profile="6202_W5230_SIMULATOR",
                    )

                map_path.write_text(
                    json.dumps([{"case_id": "DEMO_001", "sheet": "demo"}]),
                    encoding="utf-8",
                )
                with self.assertRaisesRegex(ValueError, "profile 不匹配"):
                    load_case_map(
                        "demo",
                        target="simulator",
                        profile="6202_W5230_SIMULATOR",
                    )

    def test_setup_and_action_wait_for_processed_before_continuing(self) -> None:
        session = _FakeSession()
        case = CaseEntry(
            case_id="DEMO_001",
            setup=[":ENTER_PAGE:HEART_RATE,0"],
            actions=[":TP_CLICK:312,202,1"],
            collect=[":SCREENSHOT_PRINT:", ":GUI_TREE:1"],
        )

        with mock.patch("agent_loop_system.tools.case_map.time.sleep") as wait:
            result = run_case(session, case)

        self.assertFalse(result.aborted)
        self.assertEqual(
            [raw[1:].partition(":")[0] for raw, _ in session.calls],
            [
                "ENTER_PAGE",
                "GUI_PING",
                "TP_CLICK",
                "GUI_PING",
                "SCREENSHOT_PRINT",
                "GUI_TREE",
            ],
        )
        for raw, kwargs in session.calls:
            if raw.startswith(":GUI_PING:"):
                self.assertEqual(kwargs["expected_type"], "gui_ack")
                self.assertEqual(kwargs["expected_status"], "processed")
            if raw.startswith(":GUI_TREE:"):
                self.assertEqual(kwargs["expected_type"], "gui_tree_end")
        wait.assert_called_once_with(1.0)

    def test_rotary_input_settles_before_followup_click(self) -> None:
        session = _FakeSession()
        case = CaseEntry(
            case_id="DEMO_ROTARY_CLICK",
            actions=[":QDEC_SET:1", ":TP_CLICK:205,385,1"],
        )

        with mock.patch("agent_loop_system.tools.case_map.time.sleep") as wait:
            result = run_case(session, case)

        self.assertFalse(result.aborted)
        wait.assert_called_once_with(0.25)
        self.assertIn(
            {
                "index": 3,
                "phase": "action",
                "source": "runner",
                "kind": "wait",
                "wire": "",
                "command": ":HOST_WAIT:ROTARY_INPUT_SETTLE,250",
                "command_name": "HOST_WAIT",
                "status": "completed",
                "ok": True,
            },
            result.command_trace,
        )

    def test_firmware_rejection_aborts_before_next_phase(self) -> None:
        session = _FakeSession(rejected="ENTER_PAGE")
        case = CaseEntry(
            case_id="DEMO_002",
            setup=[":ENTER_PAGE:NOT_A_WINDOW,0"],
            actions=[":TP_CLICK:1,1,1"],
        )

        result = run_case(session, case)

        self.assertTrue(result.aborted)
        self.assertIn("固件返回 rejected", result.setup_errors[0])
        self.assertEqual(len(session.calls), 1)

    def test_legacy_unable_flag_does_not_gate_fixed_execution(self) -> None:
        session = _FakeSession()
        result = run_case(
            session,
            CaseEntry(case_id="DEMO_003", unable=True, actions=[":TP_CLICK:1,1,1"]),
        )

        self.assertFalse(result.skipped)
        self.assertEqual(session.calls[0], (":TP_CLICK:1,1,1", {}))
        self.assertTrue(session.calls[1][0].startswith(":GUI_PING:"))
        self.assertEqual(session.calls[1][1]["expected_type"], "gui_ack")
        self.assertEqual(session.calls[1][1]["expected_status"], "processed")

    def test_sim_wait_uses_duration_aware_timeout(self) -> None:
        session = _FakeSession()
        result = run_case(
            session,
            CaseEntry(case_id="DEMO_WAIT", actions=[":SIM_WAIT:42,30000"]),
        )

        self.assertFalse(result.aborted)
        self.assertEqual(session.calls, [(":SIM_WAIT:42,30000", {"timeout": 35.0})])

    def test_host_wait_never_sends_a_firmware_command(self) -> None:
        session = _FakeSession()
        with mock.patch("agent_loop_system.tools.case_map.time.sleep") as wait:
            result = run_case(
                session,
                CaseEntry(
                    case_id="DEMO_HOST_WAIT",
                    actions=[":HOST_WAIT:500", ":GUI_TREE:43"],
                ),
            )

        self.assertFalse(result.aborted)
        wait.assert_any_call(0.5)
        self.assertEqual(
            [raw[1:].partition(":")[0] for raw, _ in session.calls],
            ["GUI_TREE"],
        )

    def test_host_wait_accepts_optional_checkpoint_id(self) -> None:
        session = _FakeSession()
        with mock.patch("agent_loop_system.tools.case_map.time.sleep") as wait:
            result = run_case(
                session,
                CaseEntry(
                    case_id="DEMO_HOST_WAIT_CHECKPOINT",
                    actions=[":HOST_WAIT:3801,500"],
                ),
            )

        self.assertFalse(result.aborted)
        self.assertEqual(result.action_errors, [])
        wait.assert_called_once_with(0.5)
        self.assertEqual(session.calls, [])

    def test_host_screenshot_captures_without_sending_a_firmware_command(self) -> None:
        session = _FakeSession()
        case = CaseEntry(
            case_id="DEMO_HOST_SCREENSHOT",
            collect=[":HOST_SCREENSHOT:1"],
        )
        with tempfile.TemporaryDirectory() as temporary, mock.patch(
            "agent_loop_system.tools.case_map.time.sleep"
        ):
            result = run_case(
                session,
                case,
                screenshot_path=Path(temporary) / "screenshot.bmp",
            )

        self.assertEqual(session.calls, [])
        self.assertEqual(len(result.screenshots), 1)
        self.assertEqual(result.screenshots[0]["command"], ":HOST_SCREENSHOT:1")

    def test_passive_checkpoints_do_not_wake_screen_with_a_followup_barrier(self) -> None:
        session = _FakeSession()
        result = run_case(
            session,
            CaseEntry(
                case_id="DEMO_SCREEN_OFF",
                actions=[":SIM_WAIT:42,5500", ":GUI_TREE:43"],
                verification_points=["等待后屏幕熄灭"],
            ),
        )

        self.assertFalse(result.aborted)
        self.assertEqual(
            [raw[1:].partition(":")[0] for raw, _ in session.calls],
            ["SIM_WAIT", "GUI_TREE"],
        )

    def test_long_button_press_uses_duration_aware_timeout(self) -> None:
        session = _FakeSession()
        result = run_case(
            session,
            CaseEntry(case_id="DEMO_BUTTON", actions=[":BUTTON_PRESS:1,2,5000"]),
        )

        self.assertFalse(result.aborted)
        self.assertEqual(
            session.calls[0],
            (":BUTTON_PRESS:1,2,5000", {"timeout": 10.0}),
        )

    def test_each_gui_tree_captures_a_labeled_screenshot(self) -> None:
        session = _FakeSession()
        case = CaseEntry(
            case_id="DEMO_004",
            actions=[":GUI_TREE:1", ":GUI_TREE:2"],
            verification_points=["状态一", "状态二"],
        )
        with tempfile.TemporaryDirectory() as temporary, mock.patch(
            "agent_loop_system.tools.case_map.time.sleep"
        ) as settle:
            result = run_case(
                session,
                case,
                screenshot_path=Path(temporary) / "screenshot.bmp",
            )
            self.assertEqual(
                [Path(item["path"]).name for item in result.screenshots],
                ["screenshot-01.bmp", "screenshot-02.bmp"],
            )
            self.assertTrue(all(Path(path).is_file() for path in session.captures))
            self.assertEqual(settle.call_count, 2)

        self.assertEqual(
            [item["label"] for item in result.screenshots],
            ["状态一", "状态二"],
        )

    def test_actual_trace_includes_case_commands_runner_barriers_waits_and_capture(self) -> None:
        session = _FakeSession()
        case = CaseEntry(
            case_id="DEMO_TRACE",
            steps_text="1.点击结果按钮",
            setup=[":ENTER_PAGE:DEMO,0"],
            actions=[":TP_CLICK:10,20,1"],
            collect=[":GUI_TREE:1"],
            verification_points=["结果页面可见"],
        )
        with tempfile.TemporaryDirectory() as temporary, mock.patch(
            "agent_loop_system.tools.case_map.time.sleep"
        ):
            result = run_case(
                session,
                case,
                screenshot_path=Path(temporary) / "screenshot.bmp",
            )

        self.assertTrue(result.evidence_contract["complete"])
        self.assertEqual(
            [
                (item["phase"], item["source"], item["kind"], item["command_name"])
                for item in result.command_trace
            ],
            [
                ("setup", "case", "device", "ENTER_PAGE"),
                ("setup", "runner", "barrier", "GUI_PING"),
                ("setup", "runner", "wait", "HOST_WAIT"),
                ("action", "case", "device", "TP_CLICK"),
                ("action", "runner", "barrier", "GUI_PING"),
                ("collect", "case", "device", "GUI_TREE"),
                ("collect", "runner", "wait", "HOST_WAIT"),
                ("collect", "runner", "screenshot", "HOST_SCREENSHOT"),
            ],
        )
        self.assertEqual(result.screenshots[0]["trace_index"], 8)

    def test_nonempty_steps_without_business_action_fail_the_evidence_contract(self) -> None:
        session = _FakeSession()
        case = CaseEntry(
            case_id="DEMO_MISSING_ACTION",
            steps_text="1.连续重启20轮",
            actions=[":HOST_WAIT:1"],
            collect=[":GUI_TREE:1"],
            verification_points=["每轮结束均显示主表盘"],
        )
        with tempfile.TemporaryDirectory() as temporary, mock.patch(
            "agent_loop_system.tools.case_map.time.sleep"
        ):
            result = run_case(
                session,
                case,
                screenshot_path=Path(temporary) / "screenshot.bmp",
            )

        self.assertFalse(result.evidence_contract["complete"])
        self.assertIn(
            "business_action_missing",
            [item["code"] for item in result.evidence_contract["issues"]],
        )

    def test_multiple_expected_items_require_explicit_verification_points(self) -> None:
        case = CaseEntry(
            case_id="DEMO_EXPECTED_POINTS",
            expected_text="1.显示联系人\n2.不显示连接提示",
            actions=[":HOST_WAIT:1"],
            collect=[":GUI_TREE:1"],
        )
        with tempfile.TemporaryDirectory() as temporary, mock.patch(
            "agent_loop_system.tools.case_map.time.sleep"
        ):
            result = run_case(
                _FakeSession(),
                case,
                screenshot_path=Path(temporary) / "screenshot.bmp",
            )

        self.assertFalse(result.evidence_contract["complete"])
        self.assertIn(
            "verification_points_missing",
            [item["code"] for item in result.evidence_contract["issues"]],
        )

    def test_missing_required_checkpoint_screenshot_is_an_evidence_error(self) -> None:
        class FailedCaptureSession(_FakeSession):
            def capture_screenshot(self, output_path: str) -> bool:
                self.captures.append(output_path)
                return False

        case = CaseEntry(
            case_id="DEMO_MISSING_SCREENSHOT",
            actions=[":HOST_WAIT:1"],
            collect=[":HOST_SCREENSHOT:1"],
            verification_points=["结果页面可见"],
        )
        with tempfile.TemporaryDirectory() as temporary, mock.patch(
            "agent_loop_system.tools.case_map.time.sleep"
        ):
            result = run_case(
                FailedCaptureSession(),
                case,
                screenshot_path=Path(temporary) / "screenshot.bmp",
            )

        self.assertFalse(result.evidence_contract["complete"])
        self.assertEqual(result.evidence_contract["captured_screenshots"], 0)
        self.assertIn(
            "checkpoint_screenshot_mismatch",
            [item["code"] for item in result.evidence_contract["issues"]],
        )

    def test_620c_unsolidified_cases_have_no_fixed_mapping(self) -> None:
        cases = load_case_map(
            "SOS",
            target="simulator",
            profile="620C_W6830",
        )
        for case_id in ("SOS_001", "SOS_005"):
            case = cases[case_id]
            self.assertFalse(case.is_promoted)
            self.assertEqual(case.setup, [])
            self.assertEqual(case.actions, [])
            self.assertEqual(case.collect, [])
            self.assertEqual(case.verification_points, [])

    def test_screen_off_gui_tree_still_captures_the_verdict_screenshot(self) -> None:
        session = _FakeSession(unavailable_screen_off=True)
        case = CaseEntry(
            case_id="DEMO_SCREEN_OFF_CAPTURE",
            collect=[":GUI_TREE:1"],
            verification_points=["屏幕保持熄灭"],
        )
        with tempfile.TemporaryDirectory() as temporary, mock.patch(
            "agent_loop_system.tools.case_map.time.sleep"
        ):
            result = run_case(
                session,
                case,
                screenshot_path=Path(temporary) / "screenshot.bmp",
            )

            self.assertEqual(len(result.screenshots), 1)
            self.assertEqual(result.screenshots[0]["label"], "屏幕保持熄灭")
            self.assertTrue(Path(result.screenshots[0]["path"]).is_file())
            self.assertEqual(result.collect_errors, [])


class RunnerSelectionTest(unittest.TestCase):
    def test_result_provenance_locks_simulator_artifact_hash(self) -> None:
        from agent_loop_system.tools import test as test_tool

        with tempfile.TemporaryDirectory() as temporary:
            artifact = Path(temporary) / "main.exe"
            artifact.write_bytes(b"simulator-artifact")
            with mock.patch.object(
                test_tool,
                "get_simulator_exe",
                return_value=str(artifact),
            ):
                provenance = test_tool._result_provenance(
                    target="simulator",
                    case_map_profile="6202_W5230_SIMULATOR",
                )

        self.assertEqual(provenance["target"], "simulator")
        self.assertEqual(provenance["case_map_profile"], "6202_W5230_SIMULATOR")
        self.assertEqual(provenance["project"], "6202_W5230")
        self.assertEqual(provenance["artifact_path"], str(artifact.resolve()))
        self.assertEqual(
            provenance["artifact_sha256"],
            hashlib.sha256(b"simulator-artifact").hexdigest().upper(),
        )

    def test_result_provenance_rejects_project_profile_mismatch(self) -> None:
        from agent_loop_system.tools import test as test_tool

        with mock.patch.dict(
            test_tool.os.environ,
            {"W30_PROJECT": "620C_W6830"},
        ):
            with self.assertRaisesRegex(ValueError, "与 case_map profile .* 不匹配"):
                test_tool._result_provenance(
                    target="simulator",
                    case_map_profile="6202_W5230_SIMULATOR",
                )

    def test_hardware_provenance_ignores_simulator_project_environment(self) -> None:
        from agent_loop_system.tools import test as test_tool

        with mock.patch.dict(
            test_tool.os.environ,
            {
                "W30_PROJECT": "620C_W6830",
                "W30_HARDWARE_PROJECT": "6202_W5230",
            },
        ):
            provenance = test_tool._result_provenance(
                target="hardware",
                case_map_profile="6202_W5230",
            )

        self.assertEqual(provenance["project"], "6202_W5230")
        self.assertEqual(provenance["artifact_path"], "")
        self.assertEqual(provenance["artifact_sha256"], "")

    def test_project_mismatch_fails_before_agent_exploration(self) -> None:
        from agent_loop_system.tools import test as test_tool

        case = CaseEntry(case_id="DYNAMIC_000", sheet="演示")
        with (
            mock.patch.dict(test_tool.os.environ, {"W30_PROJECT": "620C_W6830"}),
            mock.patch.object(
                test_tool,
                "load_case_map",
                return_value={case.case_id: case},
            ),
            mock.patch.object(test_tool, "_run_agent_exploration") as explore,
            mock.patch.object(test_tool, "SimulatorSession") as simulator,
        ):
            with self.assertRaisesRegex(ValueError, "与 case_map profile .* 不匹配"):
                test_tool.run_single_case(
                    "演示",
                    case.case_id,
                    "D:/evidence/dynamic.bmp",
                    target="simulator",
                    case_map_profile="6202_W5230_SIMULATOR",
                )

        explore.assert_not_called()
        simulator.assert_not_called()

    def test_unsolidified_mapping_uses_agent_exploration_without_replay_flag(self) -> None:
        from agent_loop_system.tools import test as test_tool

        case = CaseEntry(
            case_id="DYNAMIC_001",
            sheet="演示",
            steps_text="完成原始操作",
            expected_text="显示结果",
            actions=[":TP_CLICK:1,1,1"],
            mapping_status=" PROMOTED ",
        )
        expected = CaseRunResult(
            case_id=case.case_id,
            sheet=case.sheet,
            expected_text=case.expected_text,
            execution_mode="agent_exploration",
        )
        with (
            mock.patch.object(test_tool, "load_case_map", return_value={case.case_id: case}),
            mock.patch.object(
                test_tool,
                "_run_agent_exploration",
                return_value=expected,
            ) as explore,
            mock.patch.object(test_tool, "SimulatorSession") as simulator,
        ):
            result = test_tool.run_single_case(
                "演示",
                case.case_id,
                "D:/evidence/dynamic.bmp",
                target="simulator",
                case_map_profile="620C_W6830",
            )

        self.assertIs(result, expected)
        self.assertEqual(result.provenance["target"], "simulator")
        self.assertEqual(result.provenance["case_map_profile"], "620C_W6830")
        self.assertEqual(result.provenance["project"], "620C_W6830")
        explore.assert_called_once_with(
            case,
            screenshot_path="D:/evidence/dynamic.bmp",
            target="simulator",
            project="620C_W6830",
            reset_hardware=True,
        )
        simulator.assert_not_called()

    def test_candidate_replay_and_promoted_mappings_use_fixed_runner(self) -> None:
        from agent_loop_system.tools import test as test_tool

        class LifecycleSession(_FakeSession):
            def __init__(self) -> None:
                super().__init__()
                self.started = False
                self.stopped = False

            def start(self) -> None:
                self.started = True

            def stop(self) -> None:
                self.stopped = True

        for mapping_status, expected_mode in (
            ("", "candidate_mapping"),
            ("PROMOTED", "fixed_mapping"),
        ):
            with self.subTest(mapping_status=mapping_status or "candidate"):
                case = CaseEntry(
                    case_id="FIXED_001",
                    sheet="演示",
                    actions=[":TP_CLICK:1,1,1"],
                    mapping_status=mapping_status,
                )
                session = LifecycleSession()
                with (
                    mock.patch.object(
                        test_tool,
                        "load_case_map",
                        return_value={case.case_id: case},
                    ),
                    mock.patch.object(test_tool, "SimulatorSession", return_value=session),
                ):
                    result = test_tool.run_single_case(
                        "演示",
                        case.case_id,
                        "D:/evidence/fixed.bmp",
                        target="simulator",
                        case_map_profile="6202_W5230_SIMULATOR",
                        candidate_replay=not bool(mapping_status),
                    )

                self.assertEqual(result.execution_mode, expected_mode)
                self.assertEqual(result.provenance["target"], "simulator")
                self.assertEqual(
                    result.provenance["case_map_profile"],
                    "6202_W5230_SIMULATOR",
                )
                self.assertEqual(result.provenance["project"], "6202_W5230")
                self.assertTrue(session.started)
                self.assertTrue(session.stopped)
                self.assertTrue(any(raw.startswith(":TP_CLICK:") for raw, _ in session.calls))

    def test_candidate_replay_fails_before_exploration_or_session_without_candidate(
        self,
    ) -> None:
        from agent_loop_system.tools import test as test_tool

        for case in (
            CaseEntry(case_id="MISSING_001", sheet="演示", actions=[]),
            CaseEntry(
                case_id="PROMOTED_001",
                sheet="演示",
                actions=[":TP_CLICK:1,1,1"],
                mapping_status="PROMOTED",
            ),
        ):
            with (
                self.subTest(case_id=case.case_id),
                mock.patch.object(
                    test_tool,
                    "load_case_map",
                    return_value={case.case_id: case},
                ),
                mock.patch.object(test_tool, "_run_agent_exploration") as explore,
                mock.patch.object(test_tool, "SimulatorSession") as simulator,
            ):
                with self.assertRaisesRegex(ValueError, "--candidate-replay"):
                    test_tool.run_single_case(
                        "演示",
                        case.case_id,
                        "D:/evidence/candidate.bmp",
                        target="simulator",
                        case_map_profile="620C_W6830",
                        candidate_replay=True,
                    )
                explore.assert_not_called()
                simulator.assert_not_called()


if __name__ == "__main__":
    unittest.main()
