from __future__ import annotations

import tempfile
from pathlib import Path
from unittest import mock

import pytest

from agent_loop_system.tools.case_map import CaseEntry, CaseRunResult
from agent_loop_system.tools.simulator import CommandResult
from agent_loop_system.tools.test import run_single_case


class _FakeHardwareSession:
    def __init__(self) -> None:
        self.started = False
        self.stopped = False
        self.calls: list[str] = []

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True

    def send(self, command: str, **_kwargs) -> CommandResult:
        self.calls.append(command)
        if command.startswith(":GUI_PING:"):
            return CommandResult(
                "gui_ping",
                "processed",
                {"type": "gui_ack", "request": "gui_ping", "status": "processed"},
            )
        return CommandResult(
            command[1:].partition(":")[0].lower(),
            "accepted",
            {"type": "command_result", "status": "accepted"},
        )

    def capture_screenshot(self, output_path: str) -> bool:
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"BM-hardware")
        return True


def test_hardware_single_case_resets_before_starting_6202_mapping() -> None:
    case = CaseEntry(
        case_id="CALC_001",
        sheet="计算器",
        setup=[":LANGUAGE_SET:1", ":ENTER_PAGE:DIAL,0"],
        actions=[":ENTER_PAGE:CALCULATOR,0"],
        collect=[":HOST_SCREENSHOT:1"],
        mapping_status="PROMOTED",
    )
    session = _FakeHardwareSession()
    available = mock.Mock(available=True, unavailable_reason=None)
    runtime_profile = mock.Mock(
        command_capabilities={
            "LANGUAGE_SET": available,
            "ENTER_PAGE": available,
        }
    )

    with (
        tempfile.TemporaryDirectory() as temporary,
        mock.patch(
            "agent_loop_system.tools.test.load_case_map",
            return_value={case.case_id: case},
        ) as loader,
        mock.patch(
            "agent_loop_system.tools.hardware_runtime_profile.load_hardware_runtime_profile",
            return_value=runtime_profile,
        ) as profile_loader,
        mock.patch(
            "agent_loop_system.tools.real_device.RealDeviceSession",
            return_value=session,
        ) as session_class,
        mock.patch(
            "agent_loop_system.tools.real_device.reset_hardware_case_state"
        ) as reset,
    ):
        result = run_single_case(
            "计算器",
            "CALC_001",
            str(Path(temporary) / "capture.bmp"),
            target="hardware",
        )

    loader.assert_called_once_with("计算器", target="hardware")
    profile_loader.assert_called_once_with(project="6202_W5230")
    reset.assert_called_once_with(
        evidence_dir=Path(temporary).resolve() / "hardware-reset"
    )
    self_kwargs = session_class.call_args.kwargs
    assert "manage_test_session" not in self_kwargs
    assert session.started
    assert session.stopped
    assert result.aborted is False
    assert session.calls[0] == ":LANGUAGE_SET:1"
    assert ":ENTER_PAGE:DIAL,0" in session.calls
    assert ":ENTER_PAGE:CALCULATOR,0" in session.calls
    assert all(not call.startswith(":HOST_SCREENSHOT:") for call in session.calls)
    assert len(result.screenshots) == 1


def test_hardware_fixed_case_rejects_a_command_missing_from_the_runtime_profile() -> None:
    case = CaseEntry(
        case_id="BAD_001",
        sheet="不兼容",
        actions=[":NOT_IN_FIRMWARE:1"],
        mapping_status="PROMOTED",
    )
    runtime_profile = mock.Mock(command_capabilities={})
    with (
        tempfile.TemporaryDirectory() as temporary,
        mock.patch(
            "agent_loop_system.tools.test.load_case_map",
            return_value={case.case_id: case},
        ),
        mock.patch(
            "agent_loop_system.tools.hardware_runtime_profile.load_hardware_runtime_profile",
            return_value=runtime_profile,
        ),
        mock.patch(
            "agent_loop_system.tools.real_device.RealDeviceSession"
        ) as session_class,
        mock.patch(
            "agent_loop_system.tools.real_device.reset_hardware_case_state"
        ) as reset,
    ):
        with pytest.raises(ValueError, match="运行时档案.*NOT_IN_FIRMWARE"):
            run_single_case(
                case.sheet,
                case.case_id,
                str(Path(temporary) / "capture.bmp"),
                target="hardware",
            )

    reset.assert_not_called()
    session_class.assert_not_called()


def test_hardware_single_case_can_use_an_explicit_external_executor() -> None:
    case = CaseEntry(
        case_id="SET_164",
        sheet="设置",
        steps_text="从App发起查找手表",
        expected_text="手表显示查找设备提醒",
    )
    expected = CaseRunResult(
        case_id=case.case_id,
        sheet=case.sheet,
        expected_text=case.expected_text,
        execution_mode="external_ble_adapter",
    )
    executor = mock.Mock(return_value=expected)

    with (
        tempfile.TemporaryDirectory() as temporary,
        mock.patch(
            "agent_loop_system.tools.test.load_case_map",
            return_value={case.case_id: case},
        ),
        mock.patch(
            "agent_loop_system.tools.hardware_runtime_profile.load_hardware_runtime_profile",
            return_value=mock.sentinel.runtime_profile,
        ) as profile_loader,
        mock.patch(
            "agent_loop_system.tools.test._run_agent_exploration"
        ) as exploration,
        mock.patch(
            "agent_loop_system.tools.real_device.reset_hardware_case_state"
        ) as reset,
    ):
        screenshot = str(Path(temporary) / "capture.bmp")
        result = run_single_case(
            "设置",
            case.case_id,
            screenshot,
            target="hardware",
            case_map_profile="6202_W5230",
            external_executor=executor,
        )

    assert result is expected
    assert result.provenance == {
        "target": "hardware",
        "case_map_profile": "6202_W5230",
        "project": "6202_W5230",
        "artifact_path": "",
        "artifact_sha256": "",
    }
    profile_loader.assert_called_once_with(project="6202_W5230")
    executor.assert_called_once_with(case, screenshot)
    exploration.assert_not_called()
    reset.assert_not_called()


def test_hardware_agent_exploration_honors_parent_reset_boundary() -> None:
    case = CaseEntry(
        case_id="DYNAMIC_001",
        sheet="动态",
        steps_text="探索页面",
        expected_text="显示结果",
    )
    expected = CaseRunResult(
        case_id=case.case_id,
        sheet=case.sheet,
        expected_text=case.expected_text,
        execution_mode="agent_exploration",
    )

    with (
        tempfile.TemporaryDirectory() as temporary,
        mock.patch(
            "agent_loop_system.tools.test.load_case_map",
            return_value={case.case_id: case},
        ),
        mock.patch(
            "agent_loop_system.tools.test._run_agent_exploration",
            return_value=expected,
        ) as exploration,
        mock.patch(
            "agent_loop_system.tools.hardware_runtime_profile.load_hardware_runtime_profile",
            return_value=mock.sentinel.runtime_profile,
        ) as profile_loader,
    ):
        screenshot = str(Path(temporary) / "capture.bmp")
        result = run_single_case(
            case.sheet,
            case.case_id,
            screenshot,
            target="hardware",
            case_map_profile="6202_W5230",
            reset_hardware=False,
        )

    assert result is expected
    profile_loader.assert_called_once_with(project="6202_W5230")
    exploration.assert_called_once_with(
        case,
        screenshot_path=screenshot,
        target="hardware",
        project="6202_W5230",
        hardware_runtime_profile=mock.sentinel.runtime_profile,
        reset_hardware=False,
    )


def test_cli_accepts_parent_hardware_reset_boundary() -> None:
    from agent_loop_system.tools import test as test_tool

    result = CaseRunResult(
        case_id="CALC_001",
        sheet="计算器",
        expected_text="显示计算器",
        execution_mode="fixed_mapping",
    )
    decision = mock.Mock(verdict="PASS", reason="符合预期")
    with (
        mock.patch("agent_loop_system.main._load_env"),
        mock.patch.object(test_tool, "run_single_case", return_value=result) as runner,
        mock.patch.object(test_tool, "judge_case_result", return_value=decision),
        mock.patch.object(
            test_tool,
            "save_evidence",
            return_value=Path("D:/evidence/test_result.json"),
        ),
    ):
        exit_code = test_tool.main([
            "--sheet",
            "计算器",
            "--case-id",
            "CALC_001",
            "--target",
            "hardware",
            "--case-map-profile",
            "6202_W5230",
            "--screenshot-path",
            "D:/evidence/capture.bmp",
            "--skip-hardware-reset",
        ])

    assert exit_code == 0
    runner.assert_called_once_with(
        "计算器",
        "CALC_001",
        "D:/evidence/capture.bmp",
        target="hardware",
        case_map_profile="6202_W5230",
        candidate_replay=False,
        external_executor=None,
        reset_hardware=False,
    )


def test_graph_case_mode_uses_the_same_single_case_runner() -> None:
    from agent_loop_system import graph as graph_module

    with tempfile.TemporaryDirectory() as temporary:
        evidence_root = Path(temporary)
        screenshot = evidence_root / "dynamic.bmp"
        screenshot.write_bytes(b"BM-dynamic")
        case_result = CaseRunResult(
            case_id="DEMO_001",
            sheet="demo",
            expected_text="显示结果",
            execution_mode="agent_exploration",
            precomputed_verdict="PASS",
            precomputed_reason="截图符合预期",
            evidence_contract={"complete": True, "issues": []},
            screenshots=[{"path": str(screenshot), "label": "结果页"}],
        )
        with (
            mock.patch.object(graph_module, "EVIDENCE_ROOT", evidence_root),
            mock.patch(
                "agent_loop_system.tools.test.run_single_case",
                return_value=case_result,
            ) as single_runner,
        ):
            result = graph_module.test({
                "task_id": "graph-case",
                "test_cases": [{"sheet": "demo", "case_id": "DEMO_001"}],
                "agent_test_commands": [],
                "judge_criteria": "",
                "target": "simulator",
            })

        single_runner.assert_called_once_with(
            "demo",
            "DEMO_001",
            str(evidence_root / "graph-case" / "case-001-DEMO_001" / "screenshot.bmp"),
            target="simulator",
        )
        self_result = result["test_output"]["results"][0]
        assert result["verdict"] == "PASS"
        assert self_result["execution_mode"] == "agent_exploration"
        assert (evidence_root / "graph-case" / "after.bmp").read_bytes() == b"BM-dynamic"


def test_graph_hardware_command_mode_uses_shared_case_reset() -> None:
    from agent_loop_system import graph as graph_module

    with tempfile.TemporaryDirectory() as temporary:
        evidence_root = Path(temporary)
        session = _FakeHardwareSession()
        observation = mock.Mock(screenshot_ok=False, screenshot_path=None)
        with (
            mock.patch.object(graph_module, "EVIDENCE_ROOT", evidence_root),
            mock.patch(
                "agent_loop_system.tools.hardware_target.HardwareTargetConfig.from_env"
            ),
            mock.patch(
                "agent_loop_system.tools.real_device.reset_hardware_case_state"
            ) as reset,
            mock.patch(
                "agent_loop_system.tools.real_device.RealDeviceSession",
                return_value=session,
            ),
            mock.patch(
                "agent_loop_system.reproduction.observe",
                return_value=observation,
            ),
        ):
            result = graph_module.test({
                "task_id": "graph-hardware",
                "test_cases": [],
                "agent_test_commands": [":ENTER_PAGE:CALCULATOR,0"],
                "judge_criteria": "",
                "target": "hardware",
            })

        reset.assert_called_once_with(
            evidence_dir=evidence_root / "graph-hardware" / "hardware-reset"
        )
        assert session.started
        assert session.stopped
        assert ":ENTER_PAGE:CALCULATOR,0" in session.calls
        assert result["verdict"] == "CANNOT_VERIFY"
