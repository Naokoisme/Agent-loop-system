from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class RunRequest:
    project_id: str
    platform_id: str
    target_id: str
    case_ids: tuple[str, ...]
    run_mode: str = "deterministic"


@dataclass(frozen=True)
class PreflightResult:
    ready: bool
    infrastructure_status: str
    blockers: tuple[str, ...] = ()
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class UnifiedCaseResult:
    product_verdict: str
    automation_maturity: str
    infrastructure_status: str
    reason: str
    evidence: tuple[dict[str, Any], ...] = ()
    details: dict[str, Any] = field(default_factory=dict)


class PlatformExecutionGateway(Protocol):
    def preflight(self, request: RunRequest) -> PreflightResult: ...

    def run_case(self, *, case: dict[str, Any], artifact_dir: Any) -> UnifiedCaseResult: ...

    def cancel(self, run_id: str) -> dict[str, Any]: ...


class PlatformHealthProvider(Protocol):
    def inspect(self) -> dict[str, Any]: ...
