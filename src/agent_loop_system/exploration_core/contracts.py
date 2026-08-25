"""Stable, platform-neutral data contracts used by the Agent decision loop."""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Protocol

from pydantic import BaseModel, Field, model_validator


class DeviceSession(Protocol):
    """Minimum session contract shared by simulator, W30 and platform adapters."""

    last_capture_metadata: dict[str, Any] | None

    def start(self) -> None: ...

    def send(self, content: str, **kwargs: Any) -> Any: ...

    def lines_since(self, start_index: int) -> list[str]: ...

    def capture_screenshot(self, output_path: str) -> bool: ...

    def stop(self) -> None: ...


class ReproductionAction(StrEnum):
    """The single decision an Agent may make in one exploration round."""

    EXECUTE = "EXECUTE"
    OBSERVE_AGAIN = "OBSERVE_AGAIN"
    READY_TO_JUDGE = "READY_TO_JUDGE"
    BLOCKED = "BLOCKED"


class ReproductionOutcome(StrEnum):
    """Structured terminal states for an exploration run."""

    DEFECT_REPRODUCED = "DEFECT_REPRODUCED"
    CURRENT_CONFORMS = "CURRENT_CONFORMS"
    TARGET_NOT_REACHED = "TARGET_NOT_REACHED"
    REFERENCE_AMBIGUOUS = "REFERENCE_AMBIGUOUS"
    CAPABILITY_MISSING = "CAPABILITY_MISSING"
    COMMAND_RUNTIME_ERROR = "COMMAND_RUNTIME_ERROR"
    SYSTEM_ERROR = "SYSTEM_ERROR"


class TreeStatus(StrEnum):
    """GUI tree status; tree absence does not invalidate screenshot evidence."""

    OK = "OK"
    UNAVAILABLE = "UNAVAILABLE"
    ERROR = "ERROR"
    NOT_COLLECTED = "NOT_COLLECTED"


class ReproductionDecision(BaseModel):
    """One bounded decision produced by the shared Agent."""

    action: ReproductionAction
    command: str | None = None
    reason: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_single_command(self) -> "ReproductionDecision":
        command = self.command.strip() if self.command else None
        if self.action == ReproductionAction.EXECUTE:
            if not command:
                raise ValueError("EXECUTE 必须提供一条业务命令")
            self.command = command
        elif command:
            raise ValueError(f"{self.action.value} 不得携带命令")
        else:
            self.command = None
        return self

class StepObservation(BaseModel):
    """Facts collected after one decision, without Agent reasoning."""

    step: int = Field(ge=0)
    decision: ReproductionDecision | None = None
    observed_at: str | None = None

    command_status: str | None = None
    command_results: list[dict[str, Any]] = Field(default_factory=list)

    screenshot_ok: bool
    screenshot_path: str | None = None
    capture_metadata: dict[str, Any] | None = None
    window_id: int | None = None
    window_name: str | None = None
    popup_id: int | None = None
    popup_name: str | None = None

    tree_status: TreeStatus = TreeStatus.NOT_COLLECTED
    gui_tree: list[dict[str, Any]] = Field(default_factory=list)
    visible_texts: list[str] = Field(default_factory=list)
    error: str | None = None

    @model_validator(mode="after")
    def validate_screenshot_evidence(self) -> "StepObservation":
        if self.screenshot_ok and not (self.screenshot_path or "").strip():
            raise ValueError("截图成功时必须记录 screenshot_path")
        return self


class ReproductionTrace(BaseModel):
    """Complete serializable trace for one exploration run."""

    task_id: str = Field(min_length=1)
    max_steps: int = Field(default=6, ge=1)
    started_at: str | None = None
    finished_at: str | None = None
    steps: list[StepObservation] = Field(default_factory=list)
    outcome: ReproductionOutcome | None = None
    verdict: str | None = None
    reason: str | None = None

    @model_validator(mode="after")
    def validate_terminal_reason(self) -> "ReproductionTrace":
        if self.outcome is not None and not (self.reason or "").strip():
            raise ValueError("设置终止状态时必须提供 reason")
        return self
