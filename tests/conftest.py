from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def isolate_workspace_boundary_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """让单元测试显式声明工作区约束，避免宿主机配置串入临时目录。"""
    monkeypatch.delenv("W30_AGENT_WORKSPACE_ROOT", raising=False)
    monkeypatch.delenv("W30_PROJECT", raising=False)
