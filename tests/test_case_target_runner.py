from __future__ import annotations

import tempfile
from pathlib import Path
from unittest import mock

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


def test_hardware_single_case_uses_6202_map_and_preserves_external_lease() -> None:
    case = CaseEntry(
        case_id="CALC_001",
        sheet="计算器",
        setup=[":LANGUAGE_SET:1", ":ENTER_PAGE:DIAL,0"],
        actions=[":ENTER_PAGE:CALCULATOR,0"],
        collect=[":HOST_SCREENSHOT:1"],
        mapping_status="PROMOTED",
    )
    session = _FakeHardwareSession()

    with (
        tempfile.TemporaryDirectory() as temporary,
        mock.patch(
            "agent_loop_system.tools.test.load_case_map",
            return_value={case.case_id: case},
        ) as loader,
        mock.patch(
            "agent_loop_system.tools.hardware_target.HardwareTargetConfig.from_env"
        ),
        mock.patch(
            "agent_loop_system.tools.real_device.RealDeviceSession",
            return_value=session,
        ) as session_class,
    ):
        result = run_single_case(
            "计算器",
            "CALC_001",
            str(Path(temporary) / "capture.bmp"),
            target="hardware",
        )

    loader.assert_called_once_with("计算器", target="hardware")
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
