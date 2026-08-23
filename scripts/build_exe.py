"""Build the standalone Windows x64 PyInstaller onedir distribution into dist/agent-loop-windows-x64."""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = REPOSITORY_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from agent_loop_system.version import __version__


RELEASE_VERSION = __version__


RELEASE_ENV_COPY_KEYS = {
    "OPENAI_API_KEY",
    "OPENAI_API_KEY_EXPLORATION",
    "OPENAI_API_KEY_FIXED",
    "OPENAI_BASE_URL",
    "OPENAI_MODEL",
    "OPENAI_EXPLORATION_MODEL",
    "OPENAI_FIXED_MODEL",
    "OPENAI_REQUEST_TIMEOUT",
    "ONES_BASE_URL",
    "ONES_AUTH_TOKEN",
    "ONES_TEAM_UUID",
    "ONES_USER_ID",
    "W30_HARDWARE_PORT",
    "W30_HARDWARE_BAUDRATE",
    "W30_HARDWARE_TRANSPORT",
    "W30_HARDWARE_CAPTURE_PROVIDER",
    "W30_HARDWARE_PROJECT",
    "W30_HARDWARE_BLE_ADDRESS",
    "W30_HARDWARE_BLE_SCAN_TIMEOUT",
}

RELEASE_ENV_CLEAR_KEYS = {
    "W30_SOURCE_ROOT",
    "W30_AGENT_WORKSPACE_ROOT",
    "SIMULATOR_TOOL_PATH",
    "SIMULATOR_BUILD_DIRECTORY",
    "SIMULATOR_ARTIFACT_PATH",
    "W30_HARDWARE_SOURCE_ROOT",
    "W30_HARDWARE_WORKSPACE_ROOT",
    "W30_6202_SIMULATOR_SOURCE_ROOT",
    "W30_6202_SIMULATOR_BUILD_DIRECTORY",
    "W30_6202_SIMULATOR_ARTIFACT_PATH",
    "DESIGNER_MCP_ADAPTER_PATH",
}


def read_dotenv_assignments(path: Path) -> dict[str, str]:
    """Read raw dotenv assignment values without evaluating or logging them."""
    assignments: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in line:
            continue
        key, raw_value = line.split("=", 1)
        key = key.strip()
        if key:
            assignments[key] = raw_value
    return assignments


def write_release_env_example(template: Path, source: Path, destination: Path) -> None:
    """Render the distributable env template with credentials but no local paths."""
    source_values = read_dotenv_assignments(source)
    replacements = {
        key: source_values[key]
        for key in RELEASE_ENV_COPY_KEYS
        if key in source_values
    }
    replacements.update({key: "" for key in RELEASE_ENV_CLEAR_KEYS})
    replacements["W30_HARDWARE_PROFILE_ROOT"] = "profiles"
    replacements["W30_HARDWARE_PROFILE_VERSION"] = source_values.get(
        "W30_HARDWARE_PROFILE_VERSION", ""
    )

    rendered: list[str] = []
    seen: set[str] = set()
    for line in template.read_text(encoding="utf-8-sig").splitlines():
        if line.strip() and not line.lstrip().startswith("#") and "=" in line:
            key, _ = line.split("=", 1)
            key = key.strip()
            if key in replacements:
                line = f"{key}={replacements[key]}"
                seen.add(key)
        rendered.append(line)

    missing = sorted(set(replacements) - seen)
    if missing:
        rendered.extend(["", "# Release-specific settings"])
        rendered.extend(f"{key}={replacements[key]}" for key in missing)
    destination.write_text("\n".join(rendered) + "\n", encoding="utf-8")


def calc_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


def get_git_source_info(root: Path) -> dict[str, object]:
    """Return the exact Git source state embedded in release manifests."""
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(root),
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
        ).stdout.strip()
        tag_proc = subprocess.run(
            ["git", "describe", "--tags", "--exact-match", "HEAD"],
            cwd=str(root),
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        status = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=all"],
            cwd=str(root),
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
        ).stdout.strip()
        return {
            "git_commit": commit,
            "git_tag": tag_proc.stdout.strip() if tag_proc.returncode == 0 else None,
            "git_dirty": bool(status),
        }
    except (OSError, subprocess.SubprocessError):
        return {"git_commit": None, "git_tag": None, "git_dirty": None}


def build_exe(
    workspace_root: Path | None = None,
    output_dir: Path | None = None,
    profile_source: Path | None = None,
    release_env_source: Path | None = None,
    clean: bool = True,
) -> dict[str, object]:
    root = (workspace_root or Path(__file__).resolve().parents[1]).resolve()
    target_dir = (output_dir or (root / "dist" / "agent-loop-windows-x64")).resolve()
    work_dir = root / ".work" / "pyinstaller"
    if profile_source is not None and not profile_source.resolve().is_dir():
        raise FileNotFoundError(f"Runtime Profile directory not found: {profile_source}")
    if release_env_source is not None and not release_env_source.resolve().is_file():
        raise FileNotFoundError(f"Release env source not found: {release_env_source}")

    if clean:
        if target_dir.exists():
            shutil.rmtree(target_dir)
        if work_dir.exists():
            shutil.rmtree(work_dir)
        for pattern in (
            "Agent-loop-system-*-windows-x64.zip",
            "agent-loop-base-*-windows-x64.zip",
            "agent-loop-profile-6202-*.zip",
            "agent-loop-profile-620C-*.zip",
        ):
            for stale_archive in target_dir.parent.glob(pattern):
                stale_archive.unlink()

    target_dir.mkdir(parents=True, exist_ok=True)
    work_dir.mkdir(parents=True, exist_ok=True)

    entry_script = root / "src" / "agent_loop_system" / "exe_entry.py"

    hidden_imports = [
        "agent_loop_system",
        "agent_loop_system.graph",
        "agent_loop_system.main",
        "agent_loop_system.runtime_root",
        "agent_loop_system.version",
        "agent_loop_system.internal_dispatcher",
        "agent_loop_system.tools.test",
        "agent_loop_system.tools.test_batch",
        "agent_loop_system.tools.defect_store",
        "agent_loop_system.tools.mtp_screenshot",
        "agent_loop_system.tools.excel_importer",
        "agent_loop_system.tools.case_map",
        "agent_loop_system.tools.command_protocol",
        "agent_loop_system.tools.hardware_bootstrap",
        "agent_loop_system.tools.hardware_runtime_profile",
        "agent_loop_system.tools.hardware_target",
        "agent_loop_system.tools.real_device",
        "agent_loop_system.tools.simulator",
        "agent_loop_system.tools.w30_ui_compare",
        "agent_loop_system.tools.watch_ble_probe",
        "agent_loop_system.tools.build",
        "agent_loop_system.tools.ones",
        "agent_loop_system.tools.update_checker",
        "agent_loop_system.tools.auto_updater",
        "agent_loop_system.tools.designer",
        "agent_loop_system.tools.llm_config",
        "frontend.server",
        "openpyxl",
        "PIL",
        "pydantic",
        "langgraph",
        "langchain_openai",
        "langchain_core",
    ]

    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--name",
        "Agent-loop",
        "--onedir",
        "--contents-directory",
        "_internal",
        "--distpath",
        str(target_dir.parent),
        "--workpath",
        str(work_dir),
        "--specpath",
        str(work_dir),
        "--noconfirm",
        "--paths",
        str(root / "src"),
        "--paths",
        str(root),
    ]

    for hi in hidden_imports:
        cmd.extend(["--hidden-import", hi])

    cmd.append(str(entry_script))

    print(f"Executing PyInstaller build...")
    proc = subprocess.run(cmd, cwd=str(root), capture_output=True, text=True, encoding="utf-8")
    if proc.returncode != 0:
        print(f"PyInstaller build failed (exit code {proc.returncode}):")
        print("STDOUT:\n", proc.stdout[-2000:] if proc.stdout else "")
        print("STDERR:\n", proc.stderr[-2000:] if proc.stderr else "")
        raise RuntimeError(f"PyInstaller build failed: exit code {proc.returncode}")

    # Move output if PyInstaller created dist/Agent-loop instead of dist/agent-loop-windows-x64
    pyinstaller_out = target_dir.parent / "Agent-loop"
    if pyinstaller_out.exists() and pyinstaller_out != target_dir:
        for item in pyinstaller_out.iterdir():
            dst = target_dir / item.name
            if dst.exists():
                if dst.is_dir():
                    shutil.rmtree(dst)
                else:
                    dst.unlink()
            shutil.move(str(item), str(dst))
        shutil.rmtree(pyinstaller_out)

    # Copy external companion directories
    # 1. case_map
    case_map_src = root / "case_map"
    if case_map_src.exists():
        for root_dir, _, filenames in os.walk(case_map_src):
            rel_sub = Path(root_dir).relative_to(root)
            dst_sub = target_dir / rel_sub
            dst_sub.mkdir(parents=True, exist_ok=True)
            for fname in filenames:
                if fname.endswith(".json") or fname.endswith(".jsonl") or fname.endswith(".md"):
                    shutil.copy2(Path(root_dir) / fname, dst_sub / fname)

    # 2. templates
    templates_src = root / "templates"
    if templates_src.exists():
        for root_dir, _, filenames in os.walk(templates_src):
            rel_sub = Path(root_dir).relative_to(root)
            dst_sub = target_dir / rel_sub
            dst_sub.mkdir(parents=True, exist_ok=True)
            for fname in filenames:
                if not fname.startswith("~$") and not fname.endswith(".tmp"):
                    shutil.copy2(Path(root_dir) / fname, dst_sub / fname)

    # 3. profiles
    profiles_src = (profile_source or (root / "profiles")).resolve()
    if profiles_src.exists():
        for root_dir, _, filenames in os.walk(profiles_src):
            rel_sub = Path(root_dir).relative_to(profiles_src)
            dst_sub = target_dir / "profiles" / rel_sub
            dst_sub.mkdir(parents=True, exist_ok=True)
            for fname in filenames:
                if fname.startswith("~$") or fname.endswith((".tmp", ".pyc")):
                    continue
                shutil.copy2(Path(root_dir) / fname, dst_sub / fname)

    # 4. firmware-patches
    patches_src = root / "firmware-patches"
    if patches_src.exists():
        for root_dir, _, filenames in os.walk(patches_src):
            rel_sub = Path(root_dir).relative_to(root)
            dst_sub = target_dir / rel_sub
            dst_sub.mkdir(parents=True, exist_ok=True)
            for fname in filenames:
                if fname.endswith(".json") or fname.endswith(".md") or fname.endswith(".patch") or fname.endswith(".diff"):
                    shutil.copy2(Path(root_dir) / fname, dst_sub / fname)

    # 3. frontend (ONLY static assets, NO server.py or python source files)
    frontend_dst = target_dir / "frontend"
    frontend_dst.mkdir(parents=True, exist_ok=True)
    frontend_src = root / "frontend"
    for static_file in ["index.html", "app.js", "styles.css"]:
        src_f = frontend_src / static_file
        if src_f.is_file():
            shutil.copy2(src_f, frontend_dst / static_file)

    # 5. tools/SuperCom (F11: Built-in SuperCom-AgentBridge serial bridge tool)
    supercom_src = Path(r"D:\Agent-loop-workspace\SuperCom-AgentBridge\SuperCom\bin\Release")
    if not supercom_src.exists():
        supercom_src = Path(r"D:\Agent-loop-workspace\SuperCom-AgentBridge\SuperCom\bin\Debug")

    supercom_dst = target_dir / "tools" / "SuperCom"
    if supercom_src.exists():
        supercom_dst.mkdir(parents=True, exist_ok=True)
        for item in supercom_src.iterdir():
            if item.is_file():
                if item.suffix.lower() in [".pdb", ".xml"]:
                    continue
                shutil.copy2(item, supercom_dst / item.name)
            elif item.is_dir():
                if item.name.lower() in ["installer", "logs", "backup", "app_logs", "monitor_data", ".vs"]:
                    continue
                shutil.copytree(item, supercom_dst / item.name, dirs_exist_ok=True)

        # Ensure default user_data.sqlite with saved commands is included
        user_data_db = supercom_src / "user_data.sqlite"
        if not user_data_db.exists():
            user_data_db = Path(r"D:\Agent-loop-workspace\SuperCom-AgentBridge\user_data.sqlite")
        if user_data_db.exists():
            shutil.copy2(user_data_db, supercom_dst / "user_data.sqlite")

    # Root files: only .env.example
    env_example = root / ".env.example"
    if env_example.is_file():
        if release_env_source is not None:
            write_release_env_example(
                env_example,
                release_env_source.resolve(),
                target_dir / ".env.example",
            )
        else:
            shutil.copy2(env_example, target_dir / ".env.example")

    # Empty runtime directories
    for runtime_dir in ["history", "history/tests", "evidence", "defects", "defects_img", ".runtime/jobs"]:
        (target_dir / runtime_dir).mkdir(parents=True, exist_ok=True)

    # Audit for leaks in outer distribution directory
    sensitive_leaks = []
    if (target_dir / ".env").is_file():
        sensitive_leaks.append(".env")
    if (target_dir / ".git").exists():
        sensitive_leaks.append(".git")
    if (target_dir / ".venv").exists():
        sensitive_leaks.append(".venv")
    if (target_dir / "AGENTS.md").exists():
        sensitive_leaks.append("AGENTS.md")
    if (target_dir / "pyproject.toml").exists():
        sensitive_leaks.append("pyproject.toml")
    if (target_dir / "sim_tools").exists():
        sensitive_leaks.append("sim_tools (should be bundled in _internal)")
    if (target_dir / "frontend" / "server.py").exists():
        sensitive_leaks.append("frontend/server.py (should be bundled in _internal)")

    for r_dir, _, fnames in os.walk(target_dir):
        for fname in fnames:
            if fname.endswith(".py") and not Path(r_dir).is_relative_to(target_dir / "_internal"):
                sensitive_leaks.append(f"Exposed python source: {Path(r_dir) / fname}")

    if sensitive_leaks:
        raise RuntimeError(f"Package contains forbidden files/leaks: {sensitive_leaks}")

    # Write user-friendly README
    readme_content = (
        "# Agent-loop 自动化测试平台（Windows x64 便携版）\n\n"
        "无需安装 Python、开发环境或编译工具链，解压后双击 `Agent-loop.exe` 即可使用。\n"
        "已内置 6202 真机 Runtime Profile、用例库、标准模板及配套串口助手 `tools/SuperCom`。\n\n"
        "---\n\n"
        "## 1. 快速上手\n\n"
        "### 场景 A：用例管理 / Excel 导入\n"
        "1. 直接双击运行 `Agent-loop.exe`。\n"
        "2. 程序将自动启动本地 Web 服务并在默认浏览器中打开控制台：`http://127.0.0.1:8765`（遇端口占用自动顺延）。\n"
        "3. 模拟器执行仍需单独配置与固件版本匹配的模拟器源码及产物路径。\n\n"
        "### 场景 B：6202 真机自动化测试\n"
        "1. **第一步（打开串口助手）**：\n"
        "   - 打开本目录下的 `tools\\SuperCom\\SuperCom.exe`；\n"
        "   - 选择手表的调试串口（如 `COM7`，波特率 `1500000`），点击打开串口。\n"
        "   - *说明*：内置的 SuperCom 已自动启用后台 AgentBridge 管道服务（`\\\\.\\pipe\\SuperCom.AgentBridge.COM7`），无需手动输入命令。\n"
        "2. **第二步（连接 USB MTP）**：\n"
        "   - 手表通过 USB 数据线连接电脑，确保 Windows 资源管理器中可识别到 MTP 手表存储设备（用于自动化截图下载）。\n"
        "3. **第三步（启动测试平台）**：\n"
        "   - 双击运行 `Agent-loop.exe`，在 Web 界面选择 `6202_W5230`，勾选需要测试的用例点击「执行用例」即可。\n\n"
        "---\n\n"
        "## 2. 首次大模型环境配置（.env）\n\n"
        "发布包已预填当前环境所需配置：\n"
        "1. 将本目录下的 `.env.example` 复制一份并重命名为 `.env`。\n"
        "2. 6202 真机探索和固化用例使用包内相对路径 `profiles`，不需要固件源码工作区。\n"
        "3. 如需修改密钥、模型或串口参数，再用记事本编辑 `.env`。\n\n"
        "---\n\n"
        "## 3. 功能使用指南\n\n"
        "### ① 导入 Excel 测试用例（支持拖拽）\n"
        "- 进入「Agent 自动化测试」页面，在工具栏点击「导入 Excel 用例」。\n"
        "- 可直接将 `.xlsx` 文件拖入虚线框（Drag & Drop）或点击选择文件。\n"
        "- 系统将校验表头与内容并生成预览。点击「确认导入」后安全增量写入，已有固化用例 100% 保持受保护。\n\n"
        "### ② 执行测试用例\n"
        "- 选择目标项目，在用例列表中勾选或单选（如 `CALC_001`），点击「执行用例」。\n"
        "- 页面实时显示执行日志、屏幕截图对比与判定结果。\n\n"
        "### ③ 数据持久化与安全升级\n"
        "- **测试记录与证据**：保存于 `evidence/` 与 `history/` 目录。\n"
        "- **用例定义与映射**：保存于 `case_map/` 目录。\n"
        "- **升级平台**：直接覆盖核心文件即可，所有用户测试数据、证据和用例均独立安全保留。\n\n"
        "---\n\n"
        "## 4. 停止服务\n\n"
        "- 在启动控制台窗口中按下 `Ctrl + C`，或直接关闭控制台窗口即可安全退出。\n"
    )
    (target_dir / "README.md").write_text(readme_content, encoding="utf-8")

    # Generate Manifest
    files_manifest: list[dict[str, object]] = []
    for r_dir, _, fnames in os.walk(target_dir):
        for fname in fnames:
            if fname == "release_manifest.json":
                continue
            f_path = Path(r_dir) / fname
            rel_p = f_path.relative_to(target_dir).as_posix()
            f_stat = f_path.stat()
            files_manifest.append({
                "path": rel_p,
                "size_bytes": f_stat.st_size,
                "sha256": calc_sha256(f_path),
            })

    manifest = {
        "version": RELEASE_VERSION,
        "build_timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "format": "windows-x64-onedir",
        "entry_point": "Agent-loop.exe",
        "source": get_git_source_info(root),
        "total_files": len(files_manifest),
        "files": sorted(files_manifest, key=lambda x: str(x["path"])),
    }

    manifest_path = target_dir / "release_manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    # Generate ZIP Archives
    import zipfile

    # 1. Full Assembled ZIP Archive
    zip_path = target_dir.parent / f"Agent-loop-system-{RELEASE_VERSION}-windows-x64.zip"
    if zip_path.exists():
        zip_path.unlink()

    print(f"Creating full release ZIP archive at {zip_path}...")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for r_dir, _, fnames in os.walk(target_dir):
            for fname in fnames:
                f_path = Path(r_dir) / fname
                rel_p = f_path.relative_to(target_dir).as_posix()
                zf.write(f_path, arcname=rel_p)

    # 2. Generic Base Package (Strictly 100% generic, zero project metadata or case maps)
    base_zip_path = target_dir.parent / f"agent-loop-base-{RELEASE_VERSION}-windows-x64.zip"
    if base_zip_path.exists():
        base_zip_path.unlink()

    print(f"Creating strictly generic base ZIP archive at {base_zip_path}...")
    with zipfile.ZipFile(base_zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for r_dir, _, fnames in os.walk(target_dir):
            for fname in fnames:
                f_path = Path(r_dir) / fname
                rel_p = f_path.relative_to(target_dir).as_posix()
                # Exclude specific project case_maps, project profiles, and project firmware patches
                if (
                    rel_p.startswith("case_map/6202_")
                    or rel_p.startswith("case_map/620C_")
                    or rel_p.startswith("profiles/6202_")
                    or rel_p.startswith("profiles/620C_")
                    or rel_p.startswith("firmware-patches/6202_")
                    or rel_p.startswith("firmware-patches/620C_")
                ):
                    continue
                zf.write(f_path, arcname=rel_p)

    # 3. 6202 Project Adapter Pack
    profile_6202_zip = target_dir.parent / f"agent-loop-profile-6202-{RELEASE_VERSION}.zip"
    if profile_6202_zip.exists():
        profile_6202_zip.unlink()

    print(f"Creating 6202 profile adapter pack at {profile_6202_zip}...")
    with zipfile.ZipFile(profile_6202_zip, "w", zipfile.ZIP_DEFLATED) as zf:
        for r_dir, _, fnames in os.walk(target_dir):
            for fname in fnames:
                f_path = Path(r_dir) / fname
                rel_p = f_path.relative_to(target_dir).as_posix()
                if (
                    rel_p.startswith("profiles/6202_")
                    or rel_p.startswith("case_map/6202_")
                    or rel_p.startswith("firmware-patches/6202_")
                ):
                    zf.write(f_path, arcname=rel_p)

    # 4. 620C Project Adapter Pack
    profile_620c_zip = target_dir.parent / f"agent-loop-profile-620C-{RELEASE_VERSION}.zip"
    if profile_620c_zip.exists():
        profile_620c_zip.unlink()

    print(f"Creating 620C profile adapter pack at {profile_620c_zip}...")
    with zipfile.ZipFile(profile_620c_zip, "w", zipfile.ZIP_DEFLATED) as zf:
        for r_dir, _, fnames in os.walk(target_dir):
            for fname in fnames:
                f_path = Path(r_dir) / fname
                rel_p = f_path.relative_to(target_dir).as_posix()
                if (
                    rel_p.startswith("profiles/620C_")
                    or rel_p.startswith("case_map/620C_")
                    or rel_p.startswith("firmware-patches/620C_")
                ):
                    zf.write(f_path, arcname=rel_p)

    exe_file = target_dir / "Agent-loop.exe"
    return {
        "version": RELEASE_VERSION,
        "target_dir": str(target_dir),
        "total_files": len(files_manifest) + 1,
        "exe_path": str(exe_file),
        "exe_size_bytes": exe_file.stat().st_size if exe_file.exists() else 0,
        "exe_sha256": calc_sha256(exe_file) if exe_file.exists() else "",
        "zip_path": str(zip_path),
        "zip_size_bytes": zip_path.stat().st_size if zip_path.exists() else 0,
        "zip_sha256": calc_sha256(zip_path) if zip_path.exists() else "",
        "base_zip_path": str(base_zip_path),
        "base_zip_size_bytes": base_zip_path.stat().st_size if base_zip_path.exists() else 0,
        "base_zip_sha256": calc_sha256(base_zip_path) if base_zip_path.exists() else "",
        "profile_6202_zip": str(profile_6202_zip),
        "profile_6202_size_bytes": profile_6202_zip.stat().st_size if profile_6202_zip.exists() else 0,
        "profile_6202_sha256": calc_sha256(profile_6202_zip) if profile_6202_zip.exists() else "",
        "profile_620c_zip": str(profile_620c_zip),
        "profile_620c_size_bytes": profile_620c_zip.stat().st_size if profile_620c_zip.exists() else 0,
        "profile_620c_sha256": calc_sha256(profile_620c_zip) if profile_620c_zip.exists() else "",
        "manifest_path": str(manifest_path),
        "manifest_sha256": calc_sha256(manifest_path) if manifest_path.exists() else "",
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build PyInstaller onedir distribution")
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--profile-source", type=Path, default=None)
    parser.add_argument("--release-env-source", type=Path, default=None)
    parser.add_argument("--no-clean", dest="clean", action="store_false", default=True)
    args = parser.parse_args()

    res = build_exe(
        output_dir=args.output_dir,
        profile_source=args.profile_source,
        release_env_source=args.release_env_source,
        clean=args.clean,
    )
    print(f"EXE Distribution successfully built at: {res['target_dir']}")
    print(f"Executable: {res['exe_path']} ({res['exe_size_bytes']} bytes, SHA256: {res['exe_sha256']})")
    print(f"Release ZIP: {res['zip_path']} ({res['zip_size_bytes']} bytes, SHA256: {res['zip_sha256']})")
    print(f"Total packaged files: {res['total_files']}")
