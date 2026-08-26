"""一键自动升级引擎与事务替换执行器。

严格遵循阶段 F7 规范与用户数据保护安全准则：
- 自动从 NAS 读取 update-manifest.json 并拉取最新版本安装包
- 本地流式加速拉取与高速解压（耗时从 20s+ 缩短至 2s 内）
- 独立 PowerShell 更新脚本，采用 utf-8-sig 编码与 WScript.Shell 顶级脱离启动
- 记录详细更新日志 (.runtime/update.log) 供排查
- 事务替换程序目录并在失败时回滚，严格保护用户数据
- 将 SuperCom 的 user_data.sqlite 迁移到稳定用户目录
- 平滑重启并自动加载新版本
"""
from __future__ import annotations

import json
import hashlib
import os
import re
import shutil
import stat
import subprocess
import sys
import threading
import time
import zipfile
from pathlib import Path
from typing import Any
from urllib.parse import urljoin
from urllib.request import Request, urlopen

from agent_loop_system.tools.update_checker import (
    DEFAULT_MANIFEST_PATH,
    DEFAULT_NAS_ROOT,
    DATA_SAFETY_NOTICE,
    _parse_version_tuple,
    check_for_updates,
    get_current_system_version,
    get_manifest_source,
)


class AutoUpdaterError(Exception):
    def __init__(self, message: str, *, error_code: str = "UPDATE_ERROR"):
        super().__init__(message)
        self.message = message
        self.error_code = error_code


INTERNAL_REQUIRED_PATHS = {
    ".env",
    "Agent-loop.exe",
    "frontend/index.html",
    "frontend/app.js",
    "frontend/styles.css",
    "tools/SuperCom/SuperCom.exe",
    "tools/SuperCom/user_data.sqlite",
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validated_full_package(update_info: dict[str, Any]) -> dict[str, Any]:
    packages = update_info.get("packages")
    full_pkg = packages.get("full_system") if isinstance(packages, dict) else None
    if not isinstance(full_pkg, dict):
        raise AutoUpdaterError("更新清单缺少 full_system 包", error_code="INVALID_MANIFEST")

    filename = str(full_pkg.get("filename") or "").strip()
    relative_path = str(full_pkg.get("relative_path") or "").strip()
    sha256 = str(full_pkg.get("sha256") or "").strip().lower()
    try:
        size_bytes = int(full_pkg.get("size_bytes"))
    except (TypeError, ValueError) as exc:
        raise AutoUpdaterError("更新包大小字段不合法", error_code="INVALID_MANIFEST") from exc

    if not filename or Path(filename).name != filename:
        raise AutoUpdaterError("更新包文件名不合法", error_code="INVALID_MANIFEST")
    rel_path = Path(relative_path.replace("/", os.sep))
    if (
        not relative_path
        or rel_path.is_absolute()
        or ".." in rel_path.parts
        or rel_path.name != filename
    ):
        raise AutoUpdaterError("更新包相对路径不合法", error_code="INVALID_MANIFEST")
    if size_bytes <= 0 or not re.fullmatch(r"[0-9a-f]{64}", sha256):
        raise AutoUpdaterError("更新包大小或 SHA256 不合法", error_code="INVALID_MANIFEST")
    return {
        **full_pkg,
        "filename": filename,
        "relative_path": relative_path.replace("\\", "/"),
        "size_bytes": size_bytes,
        "sha256": sha256,
    }


def _copy_package_to_local(
    manifest_source: str,
    package: dict[str, Any],
    destination: Path,
) -> None:
    relative_path = str(package["relative_path"])
    if manifest_source.startswith(("http://", "https://")):
        package_url = urljoin(manifest_source, relative_path)
        request = Request(package_url, headers={"User-Agent": "AgentLoopUpdater/1"})
        try:
            with urlopen(request, timeout=30.0) as response, destination.open("wb") as output:
                shutil.copyfileobj(response, output, length=1024 * 1024)
        except Exception as exc:
            raise AutoUpdaterError(
                f"下载安装包失败: {exc}",
                error_code="PACKAGE_NOT_FOUND",
            ) from exc
        return

    package_path = Path(manifest_source).parent / Path(
        relative_path.replace("/", os.sep)
    )
    if not package_path.is_file():
        raise AutoUpdaterError(
            f"NAS 上未找到安装包: {package_path}",
            error_code="PACKAGE_NOT_FOUND",
        )
    shutil.copyfile(package_path, destination)


def _locate_payload(staging_dir: Path) -> Path:
    if (staging_dir / "Agent-loop.exe").is_file():
        return staging_dir
    candidates = [
        child
        for child in staging_dir.iterdir()
        if child.is_dir() and (child / "Agent-loop.exe").is_file()
    ]
    if len(candidates) != 1:
        raise AutoUpdaterError(
            "更新包没有唯一的 Agent-loop 载荷目录",
            error_code="INVALID_PACKAGE",
        )
    return candidates[0]


def _validate_release_payload(payload_dir: Path, target_version: str) -> dict[str, Any]:
    manifest_path = payload_dir / "release_manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise AutoUpdaterError(
            "更新包缺少或无法读取 release_manifest.json",
            error_code="INVALID_PACKAGE",
        ) from exc

    if str(manifest.get("version") or "").strip() != target_version:
        raise AutoUpdaterError(
            "更新包内部版本与更新清单不一致",
            error_code="PACKAGE_VERSION_MISMATCH",
        )
    if manifest.get("package_kind") != "internal":
        raise AutoUpdaterError(
            "正式更新包不是内部预配置包",
            error_code="INVALID_PACKAGE",
        )
    source = manifest.get("source")
    if not isinstance(source, dict) or source.get("git_dirty") is not False:
        raise AutoUpdaterError(
            "正式更新包不是干净源码构建",
            error_code="INVALID_PACKAGE",
        )

    entries = manifest.get("files")
    if not isinstance(entries, list) or manifest.get("total_files") != len(entries):
        raise AutoUpdaterError("发布文件清单数量不一致", error_code="INVALID_PACKAGE")

    expected: dict[str, tuple[int, str]] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise AutoUpdaterError("发布文件清单格式不合法", error_code="INVALID_PACKAGE")
        relative = str(entry.get("path") or "").replace("\\", "/")
        relative_path = Path(relative)
        digest = str(entry.get("sha256") or "").lower()
        try:
            size = int(entry.get("size_bytes"))
        except (TypeError, ValueError) as exc:
            raise AutoUpdaterError("发布文件大小不合法", error_code="INVALID_PACKAGE") from exc
        if (
            not relative
            or relative_path.is_absolute()
            or ".." in relative_path.parts
            or relative in expected
            or size < 0
            or not re.fullmatch(r"[0-9a-f]{64}", digest)
        ):
            raise AutoUpdaterError("发布文件清单包含非法条目", error_code="INVALID_PACKAGE")
        expected[relative] = (size, digest)

    actual = {
        path.relative_to(payload_dir).as_posix(): path
        for path in payload_dir.rglob("*")
        if path.is_file() and path != manifest_path
    }
    missing_required = sorted(INTERNAL_REQUIRED_PATHS - set(actual))
    if missing_required:
        raise AutoUpdaterError(
            f"更新包缺少必需组件: {missing_required}",
            error_code="INCOMPLETE_PACKAGE",
        )
    if set(actual) != set(expected):
        raise AutoUpdaterError(
            "更新包实际文件与 release_manifest.json 不一致",
            error_code="PACKAGE_INVENTORY_MISMATCH",
        )
    for relative, path in actual.items():
        expected_size, expected_hash = expected[relative]
        if path.stat().st_size != expected_size or _sha256(path) != expected_hash:
            raise AutoUpdaterError(
                f"更新包文件校验失败: {relative}",
                error_code="PACKAGE_FILE_MISMATCH",
            )
    return manifest


def _extract_zip_safely(archive: zipfile.ZipFile, destination: Path) -> None:
    """Reject path traversal and symlink entries before extracting an update."""

    root = destination.resolve()
    for member in archive.infolist():
        member_path = Path(member.filename.replace("\\", "/"))
        unix_mode = (member.external_attr >> 16) & 0o170000
        if member_path.is_absolute() or ".." in member_path.parts or unix_mode == stat.S_IFLNK:
            raise AutoUpdaterError(
                f"更新包包含不安全路径: {member.filename}",
                error_code="UNSAFE_ARCHIVE",
            )
        try:
            (root / member_path).resolve().relative_to(root)
        except ValueError as exc:
            raise AutoUpdaterError(
                f"更新包路径越出暂存区: {member.filename}",
                error_code="UNSAFE_ARCHIVE",
            ) from exc
    archive.extractall(root)


def _powershell_literal(value: object) -> str:
    """Quote data as one non-interpolating PowerShell string literal."""

    return "'" + str(value).replace("'", "''") + "'"


def ensure_supercom_is_closed() -> None:
    """Fail before downloading when the companion executable would lock its files."""

    if sys.platform != "win32":
        return
    try:
        result = subprocess.run(
            [
                "tasklist.exe",
                "/FI",
                "IMAGENAME eq SuperCom.exe",
                "/FO",
                "CSV",
                "/NH",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return
    if '"supercom.exe"' in result.stdout.lower():
        raise AutoUpdaterError(
            "请先关闭 SuperCom 再升级；串口与保存命令配置会自动迁移并保留",
            error_code="SUPERCOM_RUNNING",
        )


def prepare_upgrade(
    app_root: Path,
    manifest_source: str | None = None,
) -> dict[str, Any]:
    """准备升级：极速拉取新版文件并解压到本地暂存区，返回升级包信息。"""
    source = get_manifest_source(manifest_source)
    cur_ver = get_current_system_version()
    update_info = check_for_updates(manifest_source=source, current_version=cur_ver)

    if not update_info.get("has_update"):
        status = update_info.get("status", "up_to_date")
        if status == "offline":
            raise AutoUpdaterError(f"无法访问更新源: {update_info.get('error', '未知错误')}", error_code="SOURCE_OFFLINE")
        raise AutoUpdaterError(f"当前版本 ({cur_ver}) 已是最新版本，无需升级", error_code="ALREADY_UP_TO_DATE")

    latest_ver = str(update_info["latest_version"])
    if not re.fullmatch(r"\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.-]+)?", latest_ver):
        raise AutoUpdaterError("更新清单版本号格式不合法", error_code="INVALID_MANIFEST")
    full_pkg = _validated_full_package(update_info)

    staging_dir = app_root / ".runtime" / "update_staging"
    if staging_dir.exists():
        shutil.rmtree(staging_dir, ignore_errors=True)
    staging_dir.mkdir(parents=True, exist_ok=True)

    local_temp_zip = staging_dir / str(full_pkg["filename"])
    try:
        _copy_package_to_local(source, full_pkg, local_temp_zip)
        actual_size = local_temp_zip.stat().st_size
        if actual_size != full_pkg["size_bytes"]:
            raise AutoUpdaterError(
                f"更新包大小校验失败: expected={full_pkg['size_bytes']}, actual={actual_size}",
                error_code="PACKAGE_SIZE_MISMATCH",
            )
        actual_hash = _sha256(local_temp_zip)
        if actual_hash != full_pkg["sha256"]:
            raise AutoUpdaterError(
                "更新包 SHA256 校验失败",
                error_code="PACKAGE_HASH_MISMATCH",
            )
        with zipfile.ZipFile(local_temp_zip, "r") as archive:
            _extract_zip_safely(archive, staging_dir)
    except AutoUpdaterError:
        raise
    except Exception as exc:
        raise AutoUpdaterError(
            f"拉取或解压安装包失败: {exc}",
            error_code="EXTRACTION_FAILED",
        ) from exc
    finally:
        local_temp_zip.unlink(missing_ok=True)

    payload_dir = _locate_payload(staging_dir)
    release_manifest = _validate_release_payload(payload_dir, latest_ver)

    return {
        "status": "ready",
        "current_version": cur_ver,
        "target_version": latest_ver,
        "staging_dir": str(staging_dir),
        "payload_dir": str(payload_dir),
        "package_sha256": full_pkg["sha256"],
        "source_commit": (release_manifest.get("source") or {}).get("git_commit"),
        "changelog": update_info.get("changelog", []),
        "data_safety_notice": DATA_SAFETY_NOTICE,
    }


def launch_update_script(
    app_root: Path,
    staging_dir: Path,
    parent_pid: int | None = None,
    target_version: str | None = None,
) -> None:
    """生成并启动独立的 PowerShell 热更新脚本，然后安排当前进程优雅退出。"""
    pid = parent_pid or os.getpid()
    runtime_dir = app_root / ".runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    script_path = runtime_dir / "apply_update.ps1"
    log_path = runtime_dir / "update.log"

    app_root_literal = _powershell_literal(app_root.resolve())
    staging_dir_literal = _powershell_literal(staging_dir.resolve())
    log_path_literal = _powershell_literal(log_path.resolve())
    target_version_literal = _powershell_literal(str(target_version or "").strip())

    ps_content = fr"""# Agent-loop 独立事务升级脚本 (PID: {pid})
$ErrorActionPreference = "Stop"
$ParentPid = {pid}
$AppRoot = {app_root_literal}
$StagingDir = {staging_dir_literal}
$LogPath = {log_path_literal}
$TargetVer = {target_version_literal}

function Log-Msg($msg) {{
    $time = (Get-Date).ToString("yyyy-MM-dd HH:mm:ss")
    Add-Content -LiteralPath $LogPath -Value "[$time] $msg" -Encoding UTF8
}}

function Start-AgentLoop {{
    if ($env:AGENT_LOOP_UPDATE_TEST_MODE -eq "1") {{ return }}
    $exePath = Join-Path $AppRoot "Agent-loop.exe"
    if (Test-Path -LiteralPath $exePath -PathType Leaf) {{
        $wsh = New-Object -ComObject WScript.Shell
        $wsh.CurrentDirectory = $AppRoot
        [void]$wsh.Run(('"' + $exePath + '"'), 0, $false)
        return
    }}
    $pyLauncher = Join-Path $AppRoot "start_ui.py"
    if (Test-Path -LiteralPath $pyLauncher -PathType Leaf) {{
        $wsh = New-Object -ComObject WScript.Shell
        $wsh.CurrentDirectory = $AppRoot
        [void]$wsh.Run(('python "' + $pyLauncher + '"'), 0, $false)
    }}
}}

trap {{
    $failure = $_.Exception.Message
    try {{ Log-Msg "升级脚本在事务外失败: $failure" }} catch {{}}
    Start-AgentLoop
    exit 1
}}

Log-Msg "=== 开始执行 Agent-loop 事务升级 (主进程 PID: $ParentPid) ==="

$maxWaitSec = 20
$waited = 0
while ((Get-Process -Id $ParentPid -ErrorAction SilentlyContinue) -and ($waited -lt $maxWaitSec)) {{
    Start-Sleep -Milliseconds 300
    $waited += 0.3
}}
Stop-Process -Id $ParentPid -Force -ErrorAction SilentlyContinue
Start-Sleep -Milliseconds 400

$payloadDir = $StagingDir
if (-not (Test-Path -LiteralPath (Join-Path $payloadDir "Agent-loop.exe") -PathType Leaf)) {{
    $candidates = @(Get-ChildItem -LiteralPath $StagingDir -Directory | Where-Object {{
        Test-Path -LiteralPath (Join-Path $_.FullName "Agent-loop.exe") -PathType Leaf
    }})
    if ($candidates.Count -ne 1) {{
        throw "升级载荷目录数量异常: $($candidates.Count)"
    }}
    $payloadDir = $candidates[0].FullName
}}
Log-Msg "升级载荷目录: $payloadDir"

$manifestPath = Join-Path $payloadDir "release_manifest.json"
$payloadManifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
if ([string]$payloadManifest.version -ne $TargetVer) {{
    throw "升级载荷版本不一致: $($payloadManifest.version) != $TargetVer"
}}

# 在替换 SuperCom 程序前，将旧版同目录数据库一次性迁移到稳定用户数据目录。
$configuredSuperComRoot = [Environment]::GetEnvironmentVariable("SUPERCOM_DATA_ROOT")
if ([string]::IsNullOrWhiteSpace($configuredSuperComRoot)) {{
    $localData = [Environment]::GetFolderPath([Environment+SpecialFolder]::LocalApplicationData)
    $superComDataRoot = Join-Path $localData "TOPSTEP\Agent-loop\SuperCom"
}} else {{
    $superComDataRoot = [Environment]::ExpandEnvironmentVariables($configuredSuperComRoot)
    if (-not [IO.Path]::IsPathRooted($superComDataRoot)) {{
        $localData = [Environment]::GetFolderPath([Environment+SpecialFolder]::LocalApplicationData)
        $superComDataRoot = Join-Path $localData $superComDataRoot
    }}
}}
New-Item -ItemType Directory -Path $superComDataRoot -Force | Out-Null
$stableSuperComDb = Join-Path $superComDataRoot "user_data.sqlite"
$legacySuperComDb = Join-Path $AppRoot "tools\SuperCom\user_data.sqlite"
if ((Test-Path -LiteralPath $legacySuperComDb -PathType Leaf) -and -not (Test-Path -LiteralPath $stableSuperComDb)) {{
    Copy-Item -LiteralPath $legacySuperComDb -Destination $stableSuperComDb
    Log-Msg "已迁移 SuperCom 用户数据库到稳定数据目录"
}}

$transactionRoot = Join-Path (Join-Path $AppRoot ".runtime") ("update-transaction-" + $TargetVer + "-" + (Get-Date -Format "yyyyMMddHHmmss"))
$backupDir = Join-Path $transactionRoot "backup"
$failedDir = Join-Path $transactionRoot "failed-new"
New-Item -ItemType Directory -Path $backupDir -Force | Out-Null
New-Item -ItemType Directory -Path $failedDir -Force | Out-Null

$managed = @("Agent-loop.exe", "_internal", "frontend", "profiles", "firmware-patches", "sim_tools", "templates", "tools", "release_manifest.json", "start_ui.bat", "start_ui.py", "README.md", "README-先看我.md", "AGENTS.md")
$backedUp = New-Object System.Collections.Generic.List[string]
$installed = New-Object System.Collections.Generic.List[string]
$initializedConfig = $false

try {{
    foreach ($name in $managed) {{
        $src = Join-Path $payloadDir $name
        $dst = Join-Path $AppRoot $name
        $backup = Join-Path $backupDir $name
        if (Test-Path -LiteralPath $dst) {{
            $backupParent = Split-Path -Parent $backup
            if ($backupParent) {{ New-Item -ItemType Directory -Path $backupParent -Force | Out-Null }}
            Move-Item -LiteralPath $dst -Destination $backup
            $backedUp.Add($name)
        }}
        if (Test-Path -LiteralPath $src) {{
            Move-Item -LiteralPath $src -Destination $dst
            $installed.Add($name)
        }}
    }}

    # 内部包的 .env 只初始化新安装，绝不覆盖已有本机配置。
    $configSrc = Join-Path $payloadDir ".env"
    $configDst = Join-Path $AppRoot ".env"
    if (-not (Test-Path -LiteralPath $configDst -PathType Leaf)) {{
        Copy-Item -LiteralPath $configSrc -Destination $configDst
        $initializedConfig = $true
        Log-Msg "已初始化内部 .env"
    }}

    $installedManifest = Get-Content -LiteralPath (Join-Path $AppRoot "release_manifest.json") -Raw | ConvertFrom-Json
    if ([string]$installedManifest.version -ne $TargetVer) {{
        throw "安装后版本校验失败"
    }}
    if (-not (Test-Path -LiteralPath (Join-Path $AppRoot "tools\SuperCom\SuperCom.exe") -PathType Leaf)) {{
        throw "安装后缺少 SuperCom.exe"
    }}
    if (-not (Test-Path -LiteralPath (Join-Path $AppRoot ".env") -PathType Leaf)) {{
        throw "安装后缺少内部 .env"
    }}

    $state = @{{
        status = "applied"
        target_version = $TargetVer
        backup_dir = $backupDir
        applied_at = (Get-Date).ToString("o")
    }} | ConvertTo-Json
    Set-Content -LiteralPath (Join-Path $transactionRoot "state.json") -Value $state -Encoding UTF8
    if (Test-Path -LiteralPath $StagingDir) {{
        Remove-Item -LiteralPath $StagingDir -Recurse -Force -ErrorAction SilentlyContinue
    }}
    Log-Msg "事务升级文件切换成功，备份保留于: $backupDir"
}} catch {{
    $failure = $_.Exception.Message
    Log-Msg "升级失败，开始回滚: $failure"
    for ($index = $installed.Count - 1; $index -ge 0; $index--) {{
        $name = $installed[$index]
        $dst = Join-Path $AppRoot $name
        $failed = Join-Path $failedDir $name
        if (Test-Path -LiteralPath $dst) {{
            $failedParent = Split-Path -Parent $failed
            if ($failedParent) {{ New-Item -ItemType Directory -Path $failedParent -Force | Out-Null }}
            Move-Item -LiteralPath $dst -Destination $failed -Force
        }}
    }}
    for ($index = $backedUp.Count - 1; $index -ge 0; $index--) {{
        $name = $backedUp[$index]
        $backup = Join-Path $backupDir $name
        $dst = Join-Path $AppRoot $name
        if (Test-Path -LiteralPath $backup) {{
            Move-Item -LiteralPath $backup -Destination $dst -Force
        }}
    }}
    if ($initializedConfig -and (Test-Path -LiteralPath (Join-Path $AppRoot ".env") -PathType Leaf)) {{
        Remove-Item -LiteralPath (Join-Path $AppRoot ".env") -Force
    }}
    Log-Msg "已恢复升级前版本"
    Start-AgentLoop
    exit 1
}}

Start-AgentLoop
Log-Msg "=== v$TargetVer 升级与重启流程结束 ==="
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
