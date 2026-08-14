"""CMake 构建工具：在已 configure 的 build 目录上执行 cmake --build。"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

from pydantic import BaseModel

from agent_loop_system.tools.workspace import (
    WorkspaceConflictError,
    ensure_path_in_workspace,
    resolve_source_root,
)

LOG_TAIL_BYTES = 65536


class BuildConfig(BaseModel):
    """构建配置，从环境变量加载。"""

    cmake_executable: str
    build_directory: str
    artifact_path: str
    tool_path: str  # 子进程 PATH（需含 cmake + msys2 ucrt64）
    parallel: int = 8
    timeout: int = 600
    clean: bool = False

    @classmethod
    def from_env(cls) -> "BuildConfig":
        return cls(
            cmake_executable=os.environ["SIMULATOR_CMAKE_EXECUTABLE"],
            build_directory=os.environ["SIMULATOR_BUILD_DIRECTORY"],
            artifact_path=os.environ["SIMULATOR_ARTIFACT_PATH"],
            tool_path=os.environ["SIMULATOR_TOOL_PATH"],
            parallel=int(os.environ.get("SIMULATOR_BUILD_PARALLEL", "8")),
            timeout=int(os.environ.get("SIMULATOR_BUILD_TIMEOUT", "600")),
            clean=os.environ.get("SIMULATOR_BUILD_CLEAN", "").lower() in ("1", "true", "yes"),
        )


class BuildResult(BaseModel):
    """构建结果。"""

    success: bool
    exit_code: int | None = None
    artifact_path: str | None = None
    stdout_tail: str = ""
    stderr_tail: str = ""
    error_message: str | None = None


def run_build(config: BuildConfig) -> BuildResult:
    """执行 cmake --build，返回构建结果。

    前提：build_directory 已 configure（含 CMakeCache.txt）。
    成功判定：exit_code==0 且 artifact_path 文件存在。
    """
    try:
        if os.environ.get("W30_AGENT_WORKSPACE_ROOT", "").strip():
            resolve_source_root()
        build_dir = ensure_path_in_workspace(config.build_directory, "SIMULATOR_BUILD_DIRECTORY")
        artifact = ensure_path_in_workspace(config.artifact_path, "SIMULATOR_ARTIFACT_PATH")
    except WorkspaceConflictError as exc:
        return BuildResult(success=False, error_message=str(exc))
    if not (build_dir / "CMakeCache.txt").exists():
        return BuildResult(
            success=False,
            error_message=f"build 目录未 configure，缺少 CMakeCache.txt: {build_dir}",
        )

    argv = [config.cmake_executable, "--build", str(build_dir)]
    if config.clean:
        argv.append("--clean-first")
    argv.extend(["--parallel", str(config.parallel)])

    env = _make_env(config.tool_path)

    try:
        proc = subprocess.run(
            argv,
            env=env,
            capture_output=True,
            timeout=config.timeout,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except subprocess.TimeoutExpired:
        return BuildResult(success=False, error_message=f"构建超时（{config.timeout}s）")
    except FileNotFoundError:
        return BuildResult(
            success=False,
            error_message=f"cmake 可执行文件未找到: {config.cmake_executable}",
        )

    stdout_tail = _tail(proc.stdout)
    stderr_tail = _tail(proc.stderr)

    artifact_exists = artifact.exists()

    error_message = None
    if proc.returncode != 0:
        error_message = f"cmake 返回非零退出码 {proc.returncode}"
    elif not artifact_exists:
        error_message = f"构建退出码为 0 但产物未找到: {artifact}"

    return BuildResult(
        success=proc.returncode == 0 and artifact_exists,
        exit_code=proc.returncode,
        artifact_path=str(artifact) if artifact_exists else None,
        stdout_tail=stdout_tail,
        stderr_tail=stderr_tail,
        error_message=error_message,
    )


def _make_env(tool_path: str) -> dict[str, str]:
    """构造子进程环境：PATH 用 tool_path，继承构建必需的系统变量。"""
    return {
        "PATH": tool_path,
        "SystemRoot": os.environ.get("SystemRoot", r"C:\Windows"),
        "WINDIR": os.environ.get("WINDIR", r"C:\Windows"),
        "TEMP": os.environ.get("TEMP", ""),
        "TMP": os.environ.get("TMP", ""),
        "COMSPEC": os.environ.get("COMSPEC", ""),
        "PATHEXT": os.environ.get("PATHEXT", ""),
    }


def _tail(text: str) -> str:
    """截取文本尾部，避免日志过大。"""
    encoded = text.encode("utf-8")
    if len(encoded) <= LOG_TAIL_BYTES:
        return text
    return encoded[-LOG_TAIL_BYTES:].decode("utf-8", errors="replace")
