"""一键自动升级引擎与热替换执行器（终极加固版）。

严格遵循阶段 F7 规范与用户数据保护安全准则：
- 自动从 NAS 读取 update-manifest.json 并拉取最新版本安装包
- 解压至本地独立暂存区，严格校验完整性
- 独立 PowerShell 更新脚本，采用 utf-8-sig 编码与 WScript.Shell 顶级脱离启动
- 记录详细更新日志 (.runtime/update.log) 供排查
- 升级过程严格保护用户数据 (case_map/, history/, evidence/, .env, .runtime/jobs/)
- 平滑重启并自动加载新版本
"""
from __future__ import annotations

import json
import hashlib
import os
import shutil
import subprocess
import sys
import threading
import time
import zipfile
from pathlib import Path
from typing import Any

from agent_loop_system.tools.update_checker import (
    DATA_SAFETY_NOTICE,
    _parse_version_tuple,
    check_for_updates,
    get_current_system_version,
)

DEFAULT_NAS_ROOT = r"\\nas.topstepht.com\TOPSTEP\公用文件夹\软件工具\拓步自研工具\Agent-loop自动化测试平台"
DEFAULT_MANIFEST_PATH = os.path.join(DEFAULT_NAS_ROOT, "update-manifest.json")


class AutoUpdaterError(Exception):
    def __init__(self, message: str, *, error_code: str = "UPDATE_ERROR"):
        super().__init__(message)
        self.message = message
        self.error_code = error_code


def get_manifest_source() -> str:
    """获取更新清单来源（优先环境变量，默认指向 NAS 公共目录）。"""
    return (
        os.environ.get("W30_UPDATE_MANIFEST_URL")
        or os.environ.get("W30_NAS_MANIFEST_PATH")
        or DEFAULT_MANIFEST_PATH
    ).strip()


def _calculate_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _verify_update_package(package_path: Path, package_info: dict[str, Any]) -> None:
    """Require and verify the size and SHA-256 declared by the update manifest."""
    expected_sha256 = str(package_info.get("sha256") or "").strip().lower()
    expected_size = package_info.get("size_bytes")
    if len(expected_sha256) != 64 or expected_size is None:
        raise AutoUpdaterError(
            "更新清单缺少完整的安装包大小或 SHA-256",
            error_code="INVALID_PACKAGE_MANIFEST",
        )

    try:
        expected_size_int = int(expected_size)
    except (TypeError, ValueError) as exc:
        raise AutoUpdaterError(
            "更新清单中的安装包大小无效",
            error_code="INVALID_PACKAGE_MANIFEST",
        ) from exc

    actual_size = package_path.stat().st_size
    if actual_size != expected_size_int:
        raise AutoUpdaterError(
            f"安装包大小校验失败：期望 {expected_size_int}，实际 {actual_size}",
            error_code="PACKAGE_SIZE_MISMATCH",
        )

    actual_sha256 = _calculate_sha256(package_path)
    if actual_sha256 != expected_sha256:
        raise AutoUpdaterError(
            "安装包 SHA-256 校验失败，已拒绝升级",
            error_code="PACKAGE_HASH_MISMATCH",
        )


def _safe_extract_zip(package_path: Path, staging_dir: Path) -> None:
    """Extract a ZIP only when every member remains inside the staging directory."""
    staging_root = staging_dir.resolve()
    try:
        with zipfile.ZipFile(package_path, "r") as zf:
            for member in zf.infolist():
                target = (staging_dir / member.filename).resolve()
                if target != staging_root and staging_root not in target.parents:
                    raise AutoUpdaterError(
                        f"安装包包含越界路径: {member.filename}",
                        error_code="UNSAFE_ARCHIVE_PATH",
                    )
            zf.extractall(staging_dir)
    except AutoUpdaterError:
        raise
    except Exception as exc:
        raise AutoUpdaterError(
            f"解压安装包失败: {exc}",
            error_code="EXTRACTION_FAILED",
        ) from exc


def prepare_upgrade(
    app_root: Path,
    manifest_source: str | None = None,
) -> dict[str, Any]:
    """准备升级：拉取新版文件并解压到暂存区，返回升级包信息。"""
    source = manifest_source or get_manifest_source()
    cur_ver = get_current_system_version()
    update_info = check_for_updates(manifest_source=source, current_version=cur_ver)

    if not update_info.get("has_update"):
        status = update_info.get("status", "up_to_date")
        if status == "offline":
            raise AutoUpdaterError(f"无法访问更新源: {update_info.get('error', '未知错误')}", error_code="SOURCE_OFFLINE")
        raise AutoUpdaterError(f"当前版本 ({cur_ver}) 已是最新版本，无需升级", error_code="ALREADY_UP_TO_DATE")

    latest_ver = update_info["latest_version"]
    packages = update_info.get("packages", {})
    full_pkg = packages.get("full_system", {})
    rel_path = full_pkg.get("relative_path", f"releases/v{latest_ver}/Agent-loop-system-{latest_ver}-windows-x64.zip")

    nas_base = Path(source).parent
    pkg_path = nas_base / rel_path

    staging_dir = app_root / ".runtime" / "update_staging"
    if staging_dir.exists():
        shutil.rmtree(staging_dir, ignore_errors=True)
    staging_dir.mkdir(parents=True, exist_ok=True)

    if pkg_path.is_file():
        _verify_update_package(pkg_path, full_pkg)
        _safe_extract_zip(pkg_path, staging_dir)
    else:
        pkg_dir = nas_base / f"releases/v{latest_ver}/Agent-loop-system-{latest_ver}-windows-x64"
        if pkg_dir.is_dir():
            shutil.copytree(pkg_dir, staging_dir / f"Agent-loop-system-{latest_ver}-windows-x64", dirs_exist_ok=True)
        else:
            raise AutoUpdaterError(f"NAS 上未找到版本 {latest_ver} 的安装包或目录: {pkg_path}", error_code="PACKAGE_NOT_FOUND")

    payload_dir = staging_dir
    if not (staging_dir / "Agent-loop.exe").exists() and not (staging_dir / "frontend").exists():
        for sub in staging_dir.iterdir():
            if sub.is_dir() and ((sub / "Agent-loop.exe").exists() or (sub / "frontend").exists()):
                payload_dir = sub
                break

    release_manifest_path = payload_dir / "release_manifest.json"
    if not release_manifest_path.is_file():
        raise AutoUpdaterError(
            "安装包缺少 release_manifest.json",
            error_code="MISSING_RELEASE_MANIFEST",
        )
    try:
        release_manifest = json.loads(release_manifest_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise AutoUpdaterError(
            f"release_manifest.json 无法解析: {exc}",
            error_code="INVALID_RELEASE_MANIFEST",
        ) from exc
    packaged_version = str(release_manifest.get("version") or "").strip()
    if packaged_version != latest_ver:
        raise AutoUpdaterError(
            f"安装包版本不匹配：清单为 {latest_ver}，包内为 {packaged_version or '缺失'}",
            error_code="PACKAGE_VERSION_MISMATCH",
        )

    return {
        "status": "ready",
        "current_version": cur_ver,
        "target_version": latest_ver,
        "staging_dir": str(staging_dir),
        "payload_dir": str(payload_dir),
        "changelog": update_info.get("changelog", []),
        "data_safety_notice": DATA_SAFETY_NOTICE,
    }


def launch_update_script(
    app_root: Path,
    staging_dir: Path,
    parent_pid: int | None = None,
) -> None:
    """生成并启动独立的 PowerShell 热更新脚本，然后安排当前进程优雅退出。"""
    pid = parent_pid or os.getpid()
    runtime_dir = app_root / ".runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    script_path = runtime_dir / "apply_update.ps1"
    log_path = runtime_dir / "update.log"

    app_root_str = str(app_root.resolve())
    staging_dir_str = str(staging_dir.resolve())
    log_path_str = str(log_path.resolve())

    ps_content = f"""# Agent-loop 独立热更新脚本 (PID: {pid})
$ErrorActionPreference = "Continue"
$ParentPid = {pid}
$AppRoot = "{app_root_str}"
$StagingDir = "{staging_dir_str}"
$LogPath = "{log_path_str}"

function Log-Msg($msg) {{
    $time = (Get-Date).ToString("yyyy-MM-dd HH:mm:ss")
    Add-Content -Path $LogPath -Value "[$time] $msg" -Encoding UTF8
}}

Log-Msg "=== 开始执行 Agent-loop 热升级脚本 (主进程 PID: $ParentPid) ==="

# 1. 等待主进程退出
$maxWaitSec = 20
$waited = 0
while ((Get-Process -Id $ParentPid -ErrorAction SilentlyContinue) -and ($waited -lt $maxWaitSec)) {{
    Start-Sleep -Milliseconds 300
    $waited += 0.3
}}
Stop-Process -Id $ParentPid -Force -ErrorAction SilentlyContinue
Start-Sleep -Milliseconds 400

# 清理其他可能残留的 Agent-loop 进程，确保端口完全释放
Get-Process -Name "Agent-loop" -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
Start-Sleep -Milliseconds 300

# 2. 定位真实载荷目录
$payloadDir = $StagingDir
$hasExe = Test-Path (Join-Path $StagingDir "Agent-loop.exe")
$hasFrontend = Test-Path (Join-Path $StagingDir "frontend")
if (-not $hasExe -and -not $hasFrontend) {{
    $subs = Get-ChildItem -Path $StagingDir -Directory
    foreach ($sub in $subs) {{
        if ((Test-Path (Join-Path $sub.FullName "Agent-loop.exe")) -or (Test-Path (Join-Path $sub.FullName "frontend"))) {{
            $payloadDir = $sub.FullName
            break
        }}
    }}
}}
Log-Msg "定位到升级载荷目录: $payloadDir"

# 3. 白名单原子覆盖程序文件（绝对严禁覆盖 case_map/, history/, evidence/, .env, .runtime）
$whitelist = @("Agent-loop.exe", "_internal", "frontend", "profiles", "firmware-patches", "sim_tools", "templates", "release_manifest.json", "start_ui.bat", "start_ui.py", "README.md", "README-先看我.md", "AGENTS.md")
foreach ($name in $whitelist) {{
    $src = Join-Path $payloadDir $name
    if (Test-Path $src) {{
        $dst = Join-Path $AppRoot $name
        if (Test-Path $src -PathType Container) {{
            if (-not (Test-Path $dst)) {{
                New-Item -ItemType Directory -Path $dst -Force | Out-Null
            }}
            Copy-Item -Path "$src\\*" -Destination "$dst\\" -Recurse -Force -ErrorAction SilentlyContinue
            Log-Msg "覆盖目录: $name"
        }} else {{
            Copy-Item -Path $src -Destination $dst -Force -ErrorAction SilentlyContinue
            Log-Msg "覆盖文件: $name"
        }}
    }}
}}

# 4. 清理暂存区
Remove-Item -Path $StagingDir -Recurse -Force -ErrorAction SilentlyContinue
Log-Msg "清理暂存区完成"

# 5. 重新拉起新版本（采用 Windows WScript.Shell 顶级脱离启动）
$exePath = Join-Path $AppRoot "Agent-loop.exe"
if (Test-Path $exePath) {{
    Log-Msg "正在通过 WScript.Shell 顶级脱离启动新版本: $exePath"
    $wsh = New-Object -ComObject WScript.Shell
    $wsh.CurrentDirectory = $AppRoot
    $wsh.Run('"' + $exePath + '"', 0, $false)
}} else {{
    $pyLauncher = Join-Path $AppRoot "start_ui.py"
    if (Test-Path $pyLauncher) {{
        Log-Msg "正在通过 Python 启动新版本: $pyLauncher"
        $wsh = New-Object -ComObject WScript.Shell
        $wsh.CurrentDirectory = $AppRoot
        $wsh.Run('python "' + $pyLauncher + '"', 0, $false)
    }}
}}
Log-Msg "=== 升级与重启流程结束 ==="
"""
    # 必须使用 utf-8-sig (带有 UTF-8 BOM 签名)，防止 PowerShell 5.1 解析中文字符时吞引号报错
    script_path.write_text(ps_content, encoding="utf-8-sig")

    cmd = [
        "powershell.exe",
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-WindowStyle",
        "Hidden",
        "-File",
        str(script_path),
    ]

    subprocess.Popen(
        cmd,
        cwd=str(app_root),
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        close_fds=True,
    )

    def _delayed_exit():
        time.sleep(1.0)
        os._exit(0)

    t = threading.Thread(target=_delayed_exit, daemon=True)
    t.start()
