"""真机诊断所使用的项目源码与命令能力配置。"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from agent_loop_system.tools.hardware_serial import dangerous_command_reason


_PROJECT_RE = re.compile(r"^\s*set\(\s*PROJECT\s+([^\s)]+)\s*\)", re.MULTILINE)
DEFAULT_HARDWARE_PROJECT = "6202_W5230"
_APP_QUICK_CMD_PATHS = {
    "6202_W5230": Path("app/comm/TuoBu/quick_cmd/gui_comm_quick_cmd.c"),
    "6204_W5230": Path("app/comm/quick_cmd/gui_comm_quick_cmd.c"),
}


def is_forbidden_hardware_workspace(path: Path) -> bool:
    """检查是否为禁止直接作为真机测试工作区的上游或主线仓库。"""
    try:
        resolved = path.resolve()
    except Exception:
        resolved = path
    parts_lower = [p.lower() for p in resolved.parts]
    return "shenju_w30" in parts_lower


def find_hardware_workspace(
    project: str,
    search_roots: list[Path] | None = None,
) -> Path | None:
    """根据项目名称与特征指纹自动探测隔离工作区。

    探测顺序：
    1. search_roots（如调用者指定）
    2. AGENT_LOOP_WORKSPACE_BASE / W30_WORKSPACE_BASE 环境变量目录下的项目子目录
    3. Agent-loop-system 仓库的同级目录 ../Agent-loop-workspace/{project} 或 ../{project}
    4. 当前工作目录下的 workspaces/{project} 或 ../Agent-loop-workspace/{project}
    """
    candidates: list[Path] = []

    if search_roots:
        for root in search_roots:
            candidates.append(Path(root) / project)
            candidates.append(Path(root))

    env_base = (
        os.environ.get("AGENT_LOOP_WORKSPACE_BASE", "").strip()
        or os.environ.get("W30_WORKSPACE_BASE", "").strip()
    )
    if env_base:
        candidates.append(Path(env_base).resolve() / project)

    # 仓库同级目录
    repo_root = Path(__file__).resolve().parents[3]
    candidates.append(repo_root.parent / "Agent-loop-workspace" / project)
    candidates.append(repo_root.parent / project)

    # 当前执行目录相关
    cwd = Path.cwd().resolve()
    candidates.append(cwd.parent / "Agent-loop-workspace" / project)
    candidates.append(cwd / "workspaces" / project)
    candidates.append(cwd / project)

    seen: set[Path] = set()
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except Exception:
            continue
        if resolved in seen:
            continue
        seen.add(resolved)

        if not resolved.is_dir():
            continue
        if is_forbidden_hardware_workspace(resolved):
            continue

        active_config = resolved / "app" / "ProjectConfig.cmake"
        if not active_config.is_file():
            continue

        try:
            content = active_config.read_text(encoding="utf-8", errors="replace")
            match = _PROJECT_RE.search(content)
            if match and match.group(1) == project:
                return resolved
        except Exception:
            continue

    return None


@dataclass(frozen=True, slots=True)
class HardwareTargetConfig:
    """已核对项目身份且源码根与隔离工作区一致的真机配置。"""

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
            / "srv_quick_cmd_handler.c"
        )

    @property
    def project_cmake(self) -> Path:
        return self.source_root / "app" / "projects" / self.project / "Project.cmake"

    @property
    def app_windows(self) -> Path:
        return self.source_root / "app" / "windows"

    @property
    def app_quick_cmd(self) -> Path:
        relative = _APP_QUICK_CMD_PATHS.get(self.project)
        if relative is None:
            raise ValueError(f"尚未登记的真机项目: {self.project}")
        return self.source_root / relative

    @classmethod
    def from_env(cls) -> "HardwareTargetConfig":
        expected_project = (
            os.environ.get("W30_HARDWARE_PROJECT", "").strip()
            or DEFAULT_HARDWARE_PROJECT
        )
        if expected_project not in _APP_QUICK_CMD_PATHS:
            raise ValueError(f"尚未登记的真机项目: {expected_project}")

        root_value = os.environ.get("W30_HARDWARE_SOURCE_ROOT", "").strip()
        workspace_value = (
            os.environ.get("W30_HARDWARE_WORKSPACE_ROOT", "").strip()
            or os.environ.get("W30_HARDWARE_WORKSPACE", "").strip()
        )

        source_root: Path | None = None

        if root_value and workspace_value:
            src_path = Path(root_value).resolve()
            ws_path = Path(workspace_value).resolve()
            if src_path != ws_path:
                raise ValueError(
                    "HARDWARE_WORKSPACE_CONFLICT: W30_HARDWARE_SOURCE_ROOT "
                    f"必须指向当前真机隔离工作区 {ws_path}"
                )
            source_root = src_path
        elif root_value:
            source_root = Path(root_value).resolve()
        elif workspace_value:
            source_root = Path(workspace_value).resolve()
        else:
            discovered = find_hardware_workspace(expected_project)
            if discovered is not None:
                source_root = discovered
            else:
                raise ValueError(
                    f"HARDWARE_WORKSPACE_NOT_FOUND: 未配置真机工作区路径，且未能在标准候选目录"
                    f"（如 ../Agent-loop-workspace/{expected_project}）自动探测到有效的隔离工作区。"
                    f"请在 .env 中设置 W30_HARDWARE_WORKSPACE 或 W30_HARDWARE_WORKSPACE_ROOT"
                )

        if is_forbidden_hardware_workspace(source_root):
            raise ValueError(
                f"HARDWARE_WORKSPACE_FORBIDDEN: 路径 {source_root} 指向上游只读参考库 (shenju_w30)，"
                "禁止直接作为真机工作区执行测试"
            )

        if not source_root.is_dir():
            raise ValueError(f"真机源码目录不存在: {source_root}")

        active_config = source_root / "app" / "ProjectConfig.cmake"
        if not active_config.is_file():
            raise ValueError(f"真机源码缺少当前项目配置: {active_config}")
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
            raise ValueError("真机源码能力文件缺失: " + ", ".join(missing))
        return result


def hardware_command_allowed(command_name: str) -> tuple[bool, str | None]:
    """真机 Agent 的额外边界；串口层还会进行最终危险命令拦截。"""

    name = str(command_name or "").strip().upper()
    if not name:
        return False, "命令名为空"
    if name.startswith("SIM_"):
        return False, "SIM_* 仅用于 PC 模拟器，不发送到真机"
    reason = dangerous_command_reason(f":{name}:")
    if reason:
        return False, f"危险真机命令: {reason}"
    return True, None


def load_hardware_command_capabilities(config: HardwareTargetConfig):
    """直接从当前真机源码提取命令表，不复用其他项目缓存目录。"""

    from sim_tools.extract_kb import extract_command_capabilities

    return extract_command_capabilities(config.command_source)


def build_hardware_agent_knowledge(config: HardwareTargetConfig) -> str:
    """为单步复现 Agent 生成当前真机项目的命令和页面目录。"""

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
        f"## 当前 {config.project} 真机命令与参数（从源码即时提取）\n"
        + "\n".join(command_lines)
        + f"\n\n## 当前 {config.project} 注册页面\n"
        + windows
    )


__all__ = [
    "DEFAULT_HARDWARE_PROJECT",
    "HardwareTargetConfig",
    "build_hardware_agent_knowledge",
    "find_hardware_workspace",
    "hardware_command_allowed",
    "is_forbidden_hardware_workspace",
    "load_hardware_command_capabilities",
]
