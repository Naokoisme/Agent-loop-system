"""Agent-loop 独立 EXE 入口。

如果以 --internal <subcommand> 启动，则进入内部子命令分发（单条测试、批量、Agent闭环等）；
否则启动 Web UI 主界面。
"""
from __future__ import annotations

import sys
from pathlib import Path

from agent_loop_system.internal_dispatcher import (
    INTERNAL_COMMANDS,
    dispatch_internal_command,
)
from agent_loop_system.runtime_root import load_app_env, resolve_app_root


def main() -> int:
    args = sys.argv[1:]
    root = resolve_app_root()
    load_app_env(app_root=root)

    # 确保 app_root 位于 sys.path 首位，保证动态载入 frontend.server 与插件模块
    root_str = str(root)
    if root_str not in sys.path:
        sys.path.insert(0, root_str)

    if args and args[0] == "--internal":
        return dispatch_internal_command(args)

    if args and args[0] == "-m" and len(args) >= 2:
        mod = args[1].strip()
        for cmd_name, spec in INTERNAL_COMMANDS.items():
            if spec.module_name == mod or spec.module_name.endswith(f".{mod}") or mod in spec.module_name:
                return dispatch_internal_command(["--internal", cmd_name, *args[2:]])

    # Launch Web UI
    from frontend.server import main as server_main
    return server_main(args)


if __name__ == "__main__":
    sys.exit(main())

