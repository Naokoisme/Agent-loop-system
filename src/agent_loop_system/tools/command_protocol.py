"""Quick Command 的统一格式与安全校验。

三种输入形式统一转成裸命令 ``:CMD:args``：
    srv_quick_cmd send TOP5STEP:GUI_TREE:1;
    srv_quick_cmd send "TOP5STEP:GUI_TREE:1;"
    :GUI_TREE:1

本模块只做发送前的安全检查，并从当前固件源码确认命令是否注册、是否明确不可用。
参数数量、参数含义和参数值均交给固件 handler 判断。
"""
from __future__ import annotations

import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING

from agent_loop_system.tools.simulator import CommandResult, extract_json_objects

if TYPE_CHECKING:
    from sim_tools.extract_kb import CommandCapability

_WIRE_PREFIX = "srv_quick_cmd send "
_WIRE_HEAD = "TOP5STEP:"
_MAX_WIRE_BYTES = 128  # 与固件 CMD_DATA_STR_LEN=128 一致
_COMMAND_NAME = re.compile(r"[A-Z][A-Z0-9_]*")


def _parse_bare(bare: str) -> tuple[str, str]:
    """把裸命令 ``:CMD:args`` 拆为（命令名，参数字符串）。"""
    cmd, _, args = bare[1:].partition(":")
    return cmd, args


def normalize_command(value: str) -> str:
    """将完整 wire 或裸命令转为 ``:CMD:args``，并检查格式和注入风险。"""
    if not isinstance(value, str):
        raise ValueError("命令必须是字符串")
    if "\r" in value or "\n" in value:
        raise ValueError("命令包含换行符")

    command = value.strip()
    if not command:
        raise ValueError("命令为空")
    if command.startswith(_WIRE_PREFIX):
        command = command[len(_WIRE_PREFIX):].strip()
    if command.startswith('"') and command.endswith('"') and len(command) >= 2:
        command = command[1:-1].strip()
    if command.startswith(_WIRE_HEAD):
        command = ":" + command[len(_WIRE_HEAD):]
    if command.endswith(";"):
        command = command[:-1]

    if not command.startswith(":"):
        raise ValueError(f"不符合 :CMD:args 结构: {value!r}")
    if '"' in command or ";" in command or _WIRE_PREFIX in command:
        raise ValueError(f"规范化后仍含引号、分号或重复 srv_quick_cmd 包装: {value!r}")

    name, separator, _args = command[1:].partition(":")
    if not separator:
        command += ":"
    if not _COMMAND_NAME.fullmatch(name):
        raise ValueError(f"命令名格式非法: {name!r}")

    wrapped = f"{_WIRE_HEAD}{command[1:]};"
    if len(wrapped.encode("utf-8")) > _MAX_WIRE_BYTES:
        raise ValueError(f"包装后命令超过 {_MAX_WIRE_BYTES} 字节")
    return command


def _current_command_source() -> Path:
    source_root = os.environ.get("W30_SOURCE_ROOT", "").strip()
    if not source_root:
        raise ValueError("W30_SOURCE_ROOT 未配置，无法校验当前真实源码命令")
    source = (
        Path(source_root)
        / "core"
        / "comm"
        / "srv"
        / "test"
        / "hlq_quick_cmd_handler.c"
    )
    if not source.is_file():
        raise ValueError(f"当前真实源码命令表不存在: {source}")
    return source


def load_command_capabilities_from_source(
    source: str | os.PathLike[str],
) -> dict[str, CommandCapability]:
    """从指定源码命令表提取能力；调用者负责先核对目标工程。"""
    from sim_tools.extract_kb import extract_command_capabilities

    source_path = Path(source).resolve()
    if not source_path.is_file():
        raise ValueError(f"当前真实源码命令表不存在: {source_path}")
    try:
        return extract_command_capabilities(source_path)
    except (OSError, ValueError) as exc:
        raise ValueError(f"无法加载当前真实源码命令目录: {exc}") from exc


def load_current_command_capabilities() -> dict[str, CommandCapability]:
    """每次从当前 W30_SOURCE_ROOT 提取命令，避免维护或缓存另一份名单。"""
    return load_command_capabilities_from_source(_current_command_source())


def validate_agent_command(
    raw: str,
    capabilities: Mapping[str, CommandCapability] | None = None,
) -> None:
    """校验命令安全性、真实源码注册状态和明确的不可用状态。"""
    bare = normalize_command(raw)
    name, _args = _parse_bare(bare)
    current = capabilities if capabilities is not None else load_current_command_capabilities()
    capability = current.get(name)
    if capability is None:
        raise ValueError(f"未知命令（当前真实源码未注册）: {name}")
    if not capability.available:
        reason = capability.unavailable_reason or "源码明确标记不可用"
        raise ValueError(f"命令 {name} 不可用: {reason}")


def validate_agent_commands(
    commands: list[str],
    capabilities: Mapping[str, CommandCapability] | None = None,
) -> None:
    """校验命令序列；不限制业务参数，也不限制命令顺序。"""
    if not commands:
        raise ValueError("命令序列为空")
    current = capabilities if capabilities is not None else load_current_command_capabilities()
    for index, raw in enumerate(commands, 1):
        try:
            validate_agent_command(raw, current)
        except ValueError as exc:
            raise ValueError(f"第 {index} 条命令无效: {exc}") from exc


def collect_command_json(result: CommandResult) -> list[dict]:
    """把单条命令回执转为可 JSON 序列化的终端对象列表。

    1. 先加入 result.raw。
    2. 遍历 result.lines，只解析以 ``{`` 开头的合法 JSON 行。
    3. 只保留 dict。
    4. 去重但保持原顺序。
    """
    out: list[dict] = []
    if isinstance(result.raw, dict):
        out.append(result.raw)
    for line in result.lines:
        for obj in extract_json_objects(line):
            if obj not in out:
                out.append(obj)
    return out
