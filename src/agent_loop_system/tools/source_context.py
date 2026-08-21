"""受工作区边界保护的源码读取与运行时页面入口上下文补充。"""
from __future__ import annotations

import os
import re
from functools import lru_cache
from pathlib import Path

from agent_loop_system.tools.workspace import WorkspaceConflictError, resolve_source_root

DEFAULT_MAX_CHARS_PER_FILE = 150_000
DEFAULT_MAX_CHARS_TOTAL = 300_000
_RUNTIME_MAX_FILES = 2
_RUNTIME_MAX_CHARS_TOTAL = 100_000
_WINDOW_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")


class SourceContextError(ValueError):
    """源码输入不满足安全边界或大小限制。"""


def read_source_files(
    relative_paths: list[str],
    *,
    max_files: int | None = None,
    max_chars_per_file: int = DEFAULT_MAX_CHARS_PER_FILE,
    max_chars_total: int = DEFAULT_MAX_CHARS_TOTAL,
) -> list[dict[str, str]]:
    """从当前 Agent 工作区读取完整源码文件，统一执行路径与大小校验。"""
    root = resolve_source_root()
    return read_source_files_from_root(
        relative_paths,
        root,
        max_files=max_files,
        max_chars_per_file=max_chars_per_file,
        max_chars_total=max_chars_total,
    )


def read_source_files_from_root(
    relative_paths: list[str],
    source_root: str | os.PathLike[str],
    *,
    max_files: int | None = None,
    max_chars_per_file: int = DEFAULT_MAX_CHARS_PER_FILE,
    max_chars_total: int = DEFAULT_MAX_CHARS_TOTAL,
) -> list[dict[str, str]]:
    """从已由上层核对的只读源码根读取文件，并保留相同的路径/大小保护。"""

    root = Path(source_root).resolve()
    if not root.is_dir():
        raise SourceContextError(f"源码根目录不存在: {root}")
    if not relative_paths:
        raise SourceContextError("没有任何有效源码")
    if max_files is not None and len(relative_paths) > max_files:
        raise SourceContextError(f"源码文件数超限: {len(relative_paths)} > {max_files}")

    source_files: list[dict[str, str]] = []
    total_chars = 0
    for rel in relative_paths:
        if not isinstance(rel, str) or not rel:
            raise SourceContextError(f"源码路径为空: {rel!r}")
        if os.path.isabs(rel):
            raise SourceContextError(f"源码路径必须是相对路径: {rel}")
        if not rel.lower().endswith((".c", ".h")):
            raise SourceContextError(f"源码后缀必须为 .c/.h: {rel}")
        candidate = (root / rel).resolve()
        try:
            candidate.relative_to(root)
        except ValueError as exc:
            raise SourceContextError(f"源码路径越出 W30_SOURCE_ROOT: {rel}") from exc
        if not candidate.is_file():
            raise SourceContextError(f"源码文件不存在: {rel}")

        try:
            content = candidate.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise SourceContextError(f"源码文件读取失败: {rel}: {exc}") from exc
        if len(content) > max_chars_per_file:
            raise SourceContextError(
                f"单文件字符数超限: {rel} ({len(content)} 字符 > {max_chars_per_file})"
            )
        total_chars += len(content)
        if total_chars > max_chars_total:
            raise SourceContextError(
                f"源码总字符数超限: {total_chars} > {max_chars_total}"
            )
        source_files.append({"path": rel, "content": content})
    return source_files


@lru_cache(maxsize=64)
def _navigation_source_paths(
    root_value: str,
    target_window: str,
    actual_window: str,
) -> tuple[str, ...]:
    """找同时解释目标窗口和实际落点的公共模块源码，优先入口重定向。"""
    root = Path(root_value)
    comm_root = root / "app" / "comm" / "TuoBu"
    if not comm_root.is_dir():
        return ()

    target_symbol = f"GUI_WIN_{target_window}"
    actual_symbol = f"GUI_WIN_{actual_window}" if actual_window else ""
    candidates: list[tuple[int, str]] = []
    for path in comm_root.rglob("*.c"):
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if target_symbol not in content:
            continue
        score = 0
        if actual_symbol and actual_symbol in content:
            score += 100
        if "entry_redirect" in content:
            score += 60
        if ".windows" in content:
            score += 20
        normalized_stem = path.stem.casefold().replace("gui_comm_", "")
        normalized_target = target_window.casefold().replace("_interaction", "")
        if normalized_stem and normalized_stem in normalized_target:
            # 直接同名模块优先于仅引用该窗口的其他模块；重定向场景仍由 actual_symbol 加权。
            score += 120
        candidates.append((score, path.relative_to(root).as_posix()))

    candidates.sort(key=lambda item: (-item[0], item[1].casefold()))
    return tuple(path for _score, path in candidates[:_RUNTIME_MAX_FILES])


def load_runtime_navigation_sources(
    target_window: str,
    actual_window: str | None,
    *,
    existing_paths: list[str] | None = None,
    source_root: str | os.PathLike[str] | None = None,
) -> list[dict[str, str]]:
    """按运行时窗口重定向事实补充源码；失败时降级为空，不阻断截图复现。"""
    target = str(target_window or "").strip()
    actual = str(actual_window or "").strip()
    if not _WINDOW_NAME_RE.fullmatch(target):
        return []
    if actual and not _WINDOW_NAME_RE.fullmatch(actual):
        return []
    if actual == target:
        return []

    try:
        root = Path(source_root).resolve() if source_root is not None else resolve_source_root()
        if not root.is_dir():
            return []
        paths = list(_navigation_source_paths(str(root), target, actual))
        existing = {str(path).casefold() for path in existing_paths or []}
        paths = [path for path in paths if path.casefold() not in existing]
        if not paths:
            return []
        if source_root is None:
            return read_source_files(
                paths,
                max_files=_RUNTIME_MAX_FILES,
                max_chars_total=_RUNTIME_MAX_CHARS_TOTAL,
            )
        return read_source_files_from_root(
            paths,
            root,
            max_files=_RUNTIME_MAX_FILES,
            max_chars_total=_RUNTIME_MAX_CHARS_TOTAL,
        )
    except (OSError, SourceContextError, WorkspaceConflictError):
        return []
