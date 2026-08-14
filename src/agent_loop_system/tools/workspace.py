"""Agent 专用 W30 工作区边界检查。"""
from __future__ import annotations

import os
import re
from pathlib import Path


class WorkspaceConflictError(ValueError):
    """运行配置指向了非 Agent 专用工作区。"""


_PROJECT_RE = re.compile(r"^\s*set\(\s*PROJECT\s+([^\s)]+)\s*\)", re.MULTILINE)


def resolve_source_root() -> Path:
    """返回已校验的 W30_SOURCE_ROOT；配置冲突时拒绝继续。"""
    source_value = os.environ.get("W30_SOURCE_ROOT", "").strip()
    if not source_value:
        raise WorkspaceConflictError("W30_SOURCE_ROOT 未配置")

    source_root = Path(source_value).resolve()
    if not source_root.is_dir():
        raise WorkspaceConflictError(f"W30_SOURCE_ROOT 不存在: {source_root}")

    workspace_value = os.environ.get("W30_AGENT_WORKSPACE_ROOT", "").strip()
    if workspace_value:
        workspace_root = Path(workspace_value).resolve()
        if source_root != workspace_root:
            raise WorkspaceConflictError(
                f"WORKSPACE_CONFLICT: W30_SOURCE_ROOT 必须指向 Agent 专用工作区 {workspace_root}"
            )

    project = os.environ.get("W30_PROJECT", "").strip()
    if project:
        config_path = source_root / "app" / "ProjectConfig.cmake"
        if not config_path.is_file():
            raise WorkspaceConflictError(f"Agent 工作区缺少项目配置: {config_path}")
        match = _PROJECT_RE.search(config_path.read_text(encoding="utf-8", errors="replace"))
        actual = match.group(1) if match else ""
        if actual != project:
            raise WorkspaceConflictError(
                f"WORKSPACE_CONFLICT: Agent 需要 {project}，当前项目是 {actual or '未设置'}"
            )

    return source_root


def ensure_path_in_workspace(path: str | Path, label: str) -> Path:
    """启用隔离配置时，确保构建目录和产物也位于专用工作区。"""
    resolved = Path(path).resolve()
    workspace_value = os.environ.get("W30_AGENT_WORKSPACE_ROOT", "").strip()
    if not workspace_value:
        return resolved

    workspace_root = Path(workspace_value).resolve()
    try:
        resolved.relative_to(workspace_root)
    except ValueError as exc:
        raise WorkspaceConflictError(
            f"WORKSPACE_CONFLICT: {label} 越出 Agent 专用工作区: {resolved}"
        ) from exc
    return resolved
