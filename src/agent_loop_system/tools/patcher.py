"""应用 patch：before 文本比较 + 写入产品源码。

安全策略：before 必须在文件中精确匹配且唯一出现，否则拒绝应用（避免误改）。
写入使用同目录临时文件 + os.replace 原子替换；回滚校验 offset 处仍等于 after。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from agent_loop_system.tools.agent import Patch
from agent_loop_system.tools.workspace import WorkspaceConflictError, resolve_source_root


@dataclass
class ApplyResult:
    success: bool
    file_path: str
    error: str = ""
    offset: int | None = None


def _atomic_write(path: Path, content: str) -> None:
    """写同目录临时文件，再用 os.replace 原子替换。"""
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)


def _resolve_target(file_path: str) -> tuple[Path | None, ApplyResult | None]:
    """解析目标文件并做 containment 校验。失败时返回 (None, ApplyResult)。"""
    try:
        root = resolve_source_root()
    except WorkspaceConflictError as exc:
        return None, ApplyResult(False, file_path, str(exc))
    abs_path = (root / file_path).resolve()
    try:
        abs_path.relative_to(root)
    except ValueError:
        return None, ApplyResult(False, file_path, "patch 文件路径越出 W30_SOURCE_ROOT")
    if not abs_path.is_file():
        return None, ApplyResult(False, file_path, f"文件不存在: {abs_path}")
    return abs_path, None


def apply_patch(patch: Patch) -> ApplyResult:
    """应用 patch 到 W30_SOURCE_ROOT/file_path。

    before 必须在文件中精确匹配且唯一出现，否则拒绝应用。
    成功返回唯一匹配的字符偏移 offset。
    """
    abs_path, error_result = _resolve_target(patch.file_path)
    if error_result is not None:
        return error_result

    content = abs_path.read_text(encoding="utf-8", errors="replace")
    offset = content.find(patch.before)
    if offset == -1:
        return ApplyResult(False, patch.file_path, "before 文本在文件中未找到")
    if content.count(patch.before) > 1:
        return ApplyResult(False, patch.file_path, "before 文本在文件中出现多次，需更精确")

    new_content = content[:offset] + patch.after + content[offset + len(patch.before):]
    _atomic_write(abs_path, new_content)
    return ApplyResult(True, patch.file_path, offset=offset)


def rollback_patch(patch: Patch, offset: int) -> ApplyResult:
    """校验 offset 处仍精确等于 patch.after，再原子替换回 patch.before。

    若 offset 处内容不等于 after，拒绝回滚，不能猜测位置。
    """
    abs_path, error_result = _resolve_target(patch.file_path)
    if error_result is not None:
        return error_result

    content = abs_path.read_text(encoding="utf-8", errors="replace")
    if content[offset:offset + len(patch.after)] != patch.after:
        return ApplyResult(
            False, patch.file_path, f"offset 处内容不等于 after，拒绝回滚: {offset}"
        )
    new_content = content[:offset] + patch.before + content[offset + len(patch.after):]
    _atomic_write(abs_path, new_content)
    return ApplyResult(True, patch.file_path, offset=offset)
