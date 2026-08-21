"""内部子命令分发器与子进程命令构造器。

实现 E2 要求：
- 源码模式使用 `python -m <module>`
- Frozen EXE 模式使用 `Agent-loop.exe --internal <subcommand>`
- 白名单分发，拒绝未知命令，非零退出
- EXE 子进程绝不递归启动 Web 界面
"""
from __future__ import annotations

import sys
from collections.abc import Callable
from typing import NamedTuple

from agent_loop_system.runtime_root import is_frozen


class InternalCommandSpec(NamedTuple):
    name: str
    module_name: str
    entry_point: str
    description: str


INTERNAL_COMMANDS: dict[str, InternalCommandSpec] = {
    "test": InternalCommandSpec(
        name="test",
        module_name="agent_loop_system.tools.test",
        entry_point="main",
        description="单条用例执行与视觉判定",
    ),
    "test-batch": InternalCommandSpec(
        name="test-batch",
        module_name="agent_loop_system.tools.test_batch",
        entry_point="main",
        description="批量用例并发/顺序执行",
    ),
    "agent": InternalCommandSpec(
        name="agent",
        module_name="agent_loop_system.main",
        entry_point="main",
        description="缺陷修复与验证 Agent 闭环",
    ),
    "defect-store": InternalCommandSpec(
        name="defect-store",
        module_name="agent_loop_system.tools.defect_store",
        entry_point="main",
        description="缺陷拉取与解析",
    ),
    "watch-mtp-screenshot": InternalCommandSpec(
        name="watch-mtp-screenshot",
        module_name="agent_loop_system.tools.mtp_screenshot",
        entry_point="main",
        description="MTP 截图监控独立工具",
    ),
}


def build_child_command(
    internal_command: str,
    extra_args: list[str] | None = None,
    *,
    force_frozen: bool | None = None,
    executable: str | None = None,
) -> list[str]:
    """根据运行模式构建子进程执行参数列表。"""
    spec = INTERNAL_COMMANDS.get(internal_command)
    if spec is None:
        raise ValueError(f"未在白名单中登记的内部子命令: {internal_command!r}")

    exe = executable or sys.executable
    args = list(extra_args or [])
    frozen_mode = is_frozen() if force_frozen is None else force_frozen

    if frozen_mode:
        return [exe, "--internal", internal_command, *args]
    return [exe, "-m", spec.module_name, *args]


def dispatch_internal_command(argv: list[str] | None = None) -> int:
    """分发并执行 --internal 子命令。"""
    args = list(argv if argv is not None else sys.argv[1:])
    if not args or args[0] != "--internal":
        return 0

    if len(args) < 2:
        sys.stderr.write("[错误] --internal 必须指定子命令名称\n")
        return 2

    command_name = args[1].strip()
    command_args = args[2:]

    spec = INTERNAL_COMMANDS.get(command_name)
    if spec is None:
        sys.stderr.write(
            f"[错误] 未知的内部子命令: '{command_name}'\n"
            f"允许的内部子命令: {', '.join(sorted(INTERNAL_COMMANDS.keys()))}\n"
        )
        return 2

    try:
        import importlib
        mod = importlib.import_module(spec.module_name)
        entry_fn: Callable[[list[str]], int] = getattr(mod, spec.entry_point)
    except Exception as exc:
        sys.stderr.write(f"[错误] 加载子命令模块 '{spec.module_name}' 失败: {exc}\n")
        return 1

    try:
        result = entry_fn(command_args)
        return int(result or 0)
    except SystemExit as exc:
        return int(exc.code or 0) if isinstance(exc.code, int) else (0 if exc.code is None else 1)
    except Exception as exc:
        sys.stderr.write(f"[错误] 执行子命令 '{command_name}' 异常: {exc}\n")
        return 1
