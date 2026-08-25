"""Injectable platform runtime for the shared W30 Agent decision loop."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from agent_loop_system.exploration_core.contracts import DeviceSession


@dataclass(slots=True)
class PlatformExplorationRuntime:
    """Everything platform-specific that the common exploration loop needs.

    The runtime deliberately uses callables and a structural ``DeviceSession``
    so another repository can provide an adapter without importing W30
    transport implementations.
    """

    platform_id: str
    target_label: str
    session: DeviceSession
    capabilities: Any
    capability_knowledge: str
    normalize_command: Callable[[str], str]
    validate_command: Callable[[str], None]
    platform_guidance: str
    navigation_source_root: str | None = None
    prepare_session: Callable[[DeviceSession], None] | None = None
    system_observation_commands: frozenset[str] = field(
        default_factory=lambda: frozenset(
            {"GUI_PING", "GUI_STATE", "GUI_TREE", "SCREENSHOT_PRINT"}
        )
    )

    def __post_init__(self) -> None:
        self.platform_id = str(self.platform_id or "").strip()
        self.target_label = str(self.target_label or "").strip()
        self.capability_knowledge = str(self.capability_knowledge or "").strip()
        self.platform_guidance = str(self.platform_guidance or "").strip()
        if not self.platform_id:
            raise ValueError("platform_id 不能为空")
        if not self.target_label:
            raise ValueError("target_label 不能为空")
        if not self.capability_knowledge:
            raise ValueError("capability_knowledge 不能为空")
        if not self.platform_guidance:
            raise ValueError("platform_guidance 不能为空")
