from __future__ import annotations

import tempfile
from pathlib import Path
from unittest import mock

from PIL import Image

from agent_loop_system.exploration_core import (
    PlatformExplorationRuntime,
    ReproductionAction,
    ReproductionDecision,
    ReproductionOutcome,
)
from agent_loop_system.reproduction import interactive_reproduce
from agent_loop_system.tools.simulator import CommandResult
from agent_loop_system.tools.test import Verdict


class Fake579Session:
    def __init__(self) -> None:
        self.last_capture_metadata = None
        self.started = False
        self.stopped = False
        self.commands: list[str] = []
        self._lines: list[str] = []

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True

    def lines_since(self, start_index: int) -> list[str]:
        return self._lines[start_index:]

    def capture_screenshot(self, output_path: str) -> bool:
        Image.new("RGB", (8, 8), (20, 30, 40)).save(output_path, format="BMP")
        self.last_capture_metadata = {
            "platform": "579",
            "transport": "app_ble_o2",
            "freshness_verified": True,
        }
        return True

    def send(self, content: str, **kwargs) -> CommandResult:
        request = str(kwargs.get("request") or content)
        start_index = len(self._lines)
        if content.startswith(":GUI_PING:"):
            raw = {"type": "gui_ack", "status": "processed"}
            status = "processed"
        elif content.startswith(":GUI_STATE:"):
            raw = {"type": "gui_state", "status": "unavailable", "reason": "579 uses O1/O2"}
            status = "unavailable"
        elif content.startswith(":GUI_TREE:"):
            raw = {"type": "gui_tree_end", "status": "unavailable", "reason": "579 has no GUI tree"}
            status = "unavailable"
        else:
            self.commands.append(content)
            raw = {"type": "platform_action", "status": "processed", "action": content}
            status = "processed"
        self._lines.append(str(raw))
        return CommandResult(
            request=request,
            status=status,
            raw=raw,
            lines=list(self._lines[start_index:]),
            start_index=start_index,
        )


def test_injected_platform_runtime_reuses_w30_agent_loop_without_w30_session() -> None:
    session = Fake579Session()
    validated: list[str] = []
    runtime = PlatformExplorationRuntime(
        platform_id="579",
        target_label="579真机",
        session=session,
        capabilities={"timer.start": {"verified": True}},
        capability_knowledge="timer.start -> :CAP_ACTION:timer.start",
        normalize_command=lambda value: value.strip(),
        validate_command=validated.append,
        platform_guidance=(
            "只允许 CAP_ACTION；通过 APP Bridge/BLE 控制，COM3 只读，禁止猜测 raw command。"
        ),
    )
    decisions = [
        ReproductionDecision(
            action=ReproductionAction.EXECUTE,
            command=":CAP_ACTION:timer.start",
            reason="启动计时器",
        ),
        ReproductionDecision(
            action=ReproductionAction.READY_TO_JUDGE,
            reason="操作和截图已经完整",
        ),
    ]

    with tempfile.TemporaryDirectory() as tempdir:
        with (
            mock.patch(
                "agent_loop_system.tools.agent.decide_reproduction_action",
                side_effect=decisions,
            ) as decide,
            mock.patch(
                "agent_loop_system.tools.test.judge_with_vision",
                return_value=Verdict(verdict="PASS", reason="目标状态可见"),
            ),
            mock.patch("agent_loop_system.tools.build.run_build") as run_build,
            mock.patch("agent_loop_system.reproduction.SimulatorSession") as simulator,
        ):
            trace = interactive_reproduce(
                task_id="compat-579",
                objective="启动计时器",
                source_files=[],
                defect_image_paths=[],
                evidence_dir=Path(tempdir),
                platform_runtime=runtime,
            )

    assert trace.outcome == ReproductionOutcome.CURRENT_CONFORMS
    assert session.started is True and session.stopped is True
    assert session.commands == [":CAP_ACTION:timer.start"]
    assert validated == [":CAP_ACTION:timer.start"]
    assert decide.call_args.kwargs["execution_target"] == "579"
    assert decide.call_args.kwargs["target_label"] == "579真机"
    assert "COM3 只读" in decide.call_args.kwargs["platform_guidance"]
    run_build.assert_not_called()
    simulator.assert_not_called()
