"""6202 真机诊断所使用的只读源码与命令能力配置。"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from agent_loop_system.tools.hardware_serial import dangerous_command_reason


_PROJECT_RE = re.compile(r"^\s*set\(\s*PROJECT\s+([^\s)]+)\s*\)", re.MULTILINE)
DEFAULT_HARDWARE_PROJECT = "6202_W5230"


@dataclass(frozen=True, slots=True)
class HardwareTargetConfig:
    """已经核对为当前 6202 工程的只读源码位置。"""

    source_root: Path
    project: str

    @property
    def command_source(self) -> Path:
        return (
            self.source_root
            / "core"
            / "comm"
            / "srv"
            / "test"
            / "hlq_quick_cmd_handler.c"
        )

    @property
    def project_cmake(self) -> Path:
        return self.source_root / "app" / "projects" / self.project / "Project.cmake"

    @property
    def app_windows(self) -> Path:
        return self.source_root / "app" / "windows"

    @property
    def app_quick_cmd(self) -> Path:
        return (
            self.source_root
            / "app"
            / "comm"
            / "TuoBu"
            / "quick_cmd"
            / "gui_comm_quick_cmd.c"
        )

    @classmethod
    def from_env(cls) -> "HardwareTargetConfig":
        root_value = os.environ.get("W30_HARDWARE_SOURCE_ROOT", "").strip()
        if not root_value:
            raise ValueError(
                "W30_HARDWARE_SOURCE_ROOT 未配置，无法核对 6202 真机命令表"
            )
        workspace_value = os.environ.get(
            "W30_HARDWARE_WORKSPACE_ROOT", ""
        ).strip()
        if not workspace_value:
            raise ValueError(
                "W30_HARDWARE_WORKSPACE_ROOT 未配置，无法确认 6202 隔离工作区"
            )
        expected_project = (
            os.environ.get("W30_HARDWARE_PROJECT", "").strip()
            or DEFAULT_HARDWARE_PROJECT
        )
        source_root = Path(root_value).resolve()
        workspace_root = Path(workspace_value).resolve()
        if source_root != workspace_root:
            raise ValueError(
                "HARDWARE_WORKSPACE_CONFLICT: W30_HARDWARE_SOURCE_ROOT "
                f"必须指向 6202 隔离工作区 {workspace_root}"
            )
        if not source_root.is_dir():
            raise ValueError(f"6202 真机源码目录不存在: {source_root}")

        active_config = source_root / "app" / "ProjectConfig.cmake"
        if not active_config.is_file():
            raise ValueError(f"6202 真机源码缺少当前项目配置: {active_config}")
        match = _PROJECT_RE.search(
            active_config.read_text(encoding="utf-8", errors="replace")
        )
        actual_project = match.group(1) if match else ""
        if actual_project != expected_project:
            raise ValueError(
                "HARDWARE_SOURCE_CONFLICT: "
                f"期望 {expected_project}，当前源码项目是 {actual_project or '未设置'}"
            )

        result = cls(source_root=source_root, project=expected_project)
        required = (result.command_source, result.project_cmake, result.app_quick_cmd)
        missing = [str(path) for path in required if not path.is_file()]
        if missing:
            raise ValueError("6202 真机源码能力文件缺失: " + ", ".join(missing))
        return result


def hardware_command_allowed(command_name: str) -> tuple[bool, str | None]:
    """真机 Agent 的额外边界；串口层还会进行最终危险命令拦截。"""

    name = str(command_name or "").strip().upper()
    if not name:
        return False, "命令名为空"
    if name == "TEST_SESSION":
        return False, "TEST_SESSION 由用户在批次前外部管理，不交给 Agent 直接调用"
    if name.startswith("SIM_"):
        return False, "SIM_* 仅用于 PC 模拟器，不发送到真机"
    reason = dangerous_command_reason(f":{name}:")
    if reason:
        return False, f"危险真机命令: {reason}"
    return True, None


def load_hardware_command_capabilities(config: HardwareTargetConfig):
    """直接从当前 6202 源码提取命令表，不复用 620C 缓存目录。"""

    from sim_tools.extract_kb import extract_command_capabilities

    return extract_command_capabilities(config.command_source)


def build_hardware_agent_knowledge(config: HardwareTargetConfig) -> str:
    """为单步复现 Agent 生成当前 6202 的命令和页面目录。"""

    from sim_tools.extract_kb import extract_commands, extract_windows

    command_text = extract_commands(config.command_source)
    command_lines: list[str] = []
    for line in command_text.splitlines():
        if line.startswith("#"):
            command_lines.append(line)
            continue
        name = line.partition("|")[0].strip()
        allowed, _reason = hardware_command_allowed(name)
        if allowed:
            command_lines.append(line)

    windows = extract_windows(
        project_cmake=config.project_cmake,
        app_windows=config.app_windows,
        app_quick_cmd=config.app_quick_cmd,
    )
    return (
        "## 当前 6202 真机命令与参数（从源码即时提取）\n"
        + "\n".join(command_lines)
        + "\n\n## 当前 6202 注册页面\n"
        + windows
    )


__all__ = [
    "DEFAULT_HARDWARE_PROJECT",
    "HardwareTargetConfig",
    "build_hardware_agent_knowledge",
    "hardware_command_allowed",
    "load_hardware_command_capabilities",
]
