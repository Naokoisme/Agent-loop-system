import hashlib
import json
import os
import subprocess
import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest

from agent_loop_system.tools.auto_updater import (
    AutoUpdaterError,
    INTERNAL_REQUIRED_PATHS,
    ensure_supercom_is_closed,
    launch_update_script,
    prepare_upgrade,
)


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write_valid_release(
    root: Path,
    *,
    version: str = "0.4.8",
    executable: bytes = b"portable-executable",
) -> None:
    payloads = {relative: b"test" for relative in INTERNAL_REQUIRED_PATHS}
    payloads["Agent-loop.exe"] = executable
    payloads["_internal/runtime.bin"] = b"runtime"
    payloads["profiles/6202_W5230/latest.json"] = b"{}"
    for relative, data in payloads.items():
        path = root / Path(relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    files = [
        {
            "path": relative,
            "size_bytes": len(data),
            "sha256": _sha256_bytes(data),
        }
        for relative, data in sorted(payloads.items())
    ]
    (root / "release_manifest.json").write_text(
        json.dumps(
            {
                "version": version,
                "package_kind": "internal",
                "source": {"git_commit": "a" * 40, "git_dirty": False},
                "total_files": len(files),
                "files": files,
            }
        ),
        encoding="utf-8",
    )


def _write_update_manifest(manifest_path: Path, release_zip: Path, version: str) -> None:
    archive_bytes = release_zip.read_bytes()
    manifest_path.write_text(
        json.dumps(
            {
                "latest_version": version,
                "packages": {
                    "full_system": {
                        "filename": release_zip.name,
                        "relative_path": release_zip.name,
                        "size_bytes": len(archive_bytes),
                        "sha256": _sha256_bytes(archive_bytes),
                    }
                },
            }
        ),
        encoding="utf-8",
    )


def _build_release_zip(tmp_path: Path, *, version: str = "0.4.8") -> tuple[Path, Path]:
    package_source = tmp_path / "package-source"
    package_source.mkdir()
    _write_valid_release(package_source, version=version)
    package_path = tmp_path / f"Agent-loop-system-{version}-windows-x64.zip"
    with zipfile.ZipFile(package_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in package_source.rglob("*"):
            if path.is_file():
                archive.write(path, path.relative_to(package_source).as_posix())
    manifest_path = tmp_path / "update-manifest.json"
    _write_update_manifest(manifest_path, package_path, version)
    return package_path, manifest_path


def test_prepare_upgrade_extracts_and_verifies_local_release(tmp_path: Path) -> None:
    _, manifest_path = _build_release_zip(tmp_path)
    app_root = tmp_path / "installed"
    app_root.mkdir()

    with patch(
        "agent_loop_system.tools.auto_updater.get_current_system_version",
        return_value="0.4.7",
    ):
        result = prepare_upgrade(app_root, str(manifest_path))

    payload = Path(result["payload_dir"])
    assert result["status"] == "ready"
    assert result["current_version"] == "0.4.7"
    assert result["target_version"] == "0.4.8"
    assert result["source_commit"] == "a" * 40
    assert (payload / "Agent-loop.exe").read_bytes() == b"portable-executable"


def test_prepare_upgrade_rejects_package_hash_mismatch(tmp_path: Path) -> None:
    _, manifest_path = _build_release_zip(tmp_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["packages"]["full_system"]["sha256"] = "0" * 64
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    app_root = tmp_path / "installed"
    app_root.mkdir()

    with (
        patch(
            "agent_loop_system.tools.auto_updater.get_current_system_version",
            return_value="0.4.7",
        ),
        pytest.raises(AutoUpdaterError) as raised,
    ):
        prepare_upgrade(app_root, str(manifest_path))

    assert raised.value.error_code == "PACKAGE_HASH_MISMATCH"


def test_prepare_upgrade_rejects_internal_version_mismatch(tmp_path: Path) -> None:
    package_path, manifest_path = _build_release_zip(tmp_path, version="0.4.7")
    _write_update_manifest(manifest_path, package_path, "0.4.8")
    app_root = tmp_path / "installed"
    app_root.mkdir()

    with (
        patch(
            "agent_loop_system.tools.auto_updater.get_current_system_version",
            return_value="0.4.7",
        ),
        pytest.raises(AutoUpdaterError) as raised,
    ):
        prepare_upgrade(app_root, str(manifest_path))

    assert raised.value.error_code == "PACKAGE_VERSION_MISMATCH"


def test_upgrade_requires_supercom_to_be_closed() -> None:
    completed = subprocess.CompletedProcess(
        args=[],
        returncode=0,
        stdout='"SuperCom.exe","1234","Console","1","10,000 K"',
        stderr="",
    )
    with (
        patch("agent_loop_system.tools.auto_updater.sys.platform", "win32"),
        patch("agent_loop_system.tools.auto_updater.subprocess.run", return_value=completed),
        pytest.raises(AutoUpdaterError) as raised,
    ):
        ensure_supercom_is_closed()

    assert raised.value.error_code == "SUPERCOM_RUNNING"


def _write_transaction_payload(staging: Path, *, include_supercom: bool = True) -> None:
    (staging / "frontend").mkdir(parents=True)
    (staging / "tools" / "SuperCom").mkdir(parents=True)
    (staging / "Agent-loop.exe").write_bytes(b"new-exe")
    (staging / "frontend" / "app.js").write_text("new", encoding="utf-8")
    if include_supercom:
        (staging / "tools" / "SuperCom" / "SuperCom.exe").write_bytes(b"new-supercom")
    (staging / "tools" / "SuperCom" / "user_data.sqlite").write_bytes(b"template-db")
    (staging / ".env").write_text("OPENAI_API_KEY=internal\n", encoding="utf-8")
    (staging / "release_manifest.json").write_text(
        json.dumps({"version": "0.4.8"}),
        encoding="utf-8",
    )


def _write_update_script(app_root: Path, staging: Path) -> Path:
    with (
        patch("agent_loop_system.tools.auto_updater.subprocess.Popen"),
        patch("agent_loop_system.tools.auto_updater.threading.Thread") as thread,
    ):
        launch_update_script(
            app_root,
            staging,
            parent_pid=999_999,
            target_version="0.4.8",
        )
    thread.return_value.start.assert_called_once_with()
    return app_root / ".runtime" / "apply_update.ps1"


def _run_update_script(script_path: Path, supercom_data_root: Path) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["AGENT_LOOP_UPDATE_TEST_MODE"] = "1"
    environment["SUPERCOM_DATA_ROOT"] = str(supercom_data_root)
    return subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script_path),
        ],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )


def test_transaction_update_migrates_supercom_data_and_preserves_env(tmp_path: Path) -> None:
    app_root = tmp_path / "Agent-loop-system-0.4.6-windows-x64"
    staging = app_root / ".runtime" / "update_staging"
    (app_root / "tools" / "SuperCom").mkdir(parents=True)
    (app_root / "frontend").mkdir()
    (app_root / "Agent-loop.exe").write_bytes(b"old-exe")
    (app_root / "frontend" / "app.js").write_text("old", encoding="utf-8")
    (app_root / "tools" / "SuperCom" / "SuperCom.exe").write_bytes(b"old-supercom")
    (app_root / "tools" / "SuperCom" / "user_data.sqlite").write_bytes(b"user-db")
    (app_root / ".env").write_text("OPENAI_API_KEY=user\n", encoding="utf-8")
    staging.mkdir(parents=True)
    _write_transaction_payload(staging)
    script_path = _write_update_script(app_root, staging)
    stable_data = tmp_path / "stable-supercom"

    result = _run_update_script(script_path, stable_data)

    assert result.returncode == 0, result.stderr
    assert (app_root / "Agent-loop.exe").read_bytes() == b"new-exe"
    assert (app_root / "tools" / "SuperCom" / "SuperCom.exe").read_bytes() == b"new-supercom"
    assert (stable_data / "user_data.sqlite").read_bytes() == b"user-db"
    assert (app_root / ".env").read_text(encoding="utf-8") == "OPENAI_API_KEY=user\n"
    assert list((app_root / ".runtime").glob("update-transaction-*/backup/Agent-loop.exe"))


def test_transaction_update_rolls_back_when_new_supercom_is_missing(tmp_path: Path) -> None:
    app_root = tmp_path / "Agent-loop-system-0.4.7-windows-x64"
    staging = app_root / ".runtime" / "update_staging"
    (app_root / "tools" / "SuperCom").mkdir(parents=True)
    (app_root / "Agent-loop.exe").write_bytes(b"old-exe")
    (app_root / "tools" / "SuperCom" / "SuperCom.exe").write_bytes(b"old-supercom")
    staging.mkdir(parents=True)
    _write_transaction_payload(staging, include_supercom=False)
    script_path = _write_update_script(app_root, staging)

    result = _run_update_script(script_path, tmp_path / "stable-supercom")

    assert result.returncode == 1
    assert (app_root / "Agent-loop.exe").read_bytes() == b"old-exe"
    assert (app_root / "tools" / "SuperCom" / "SuperCom.exe").read_bytes() == b"old-supercom"
    assert not (app_root / ".env").exists()


def test_launch_update_script_writes_parseable_command_quoting(tmp_path: Path) -> None:
    app_root = tmp_path / "Agent-loop-system-0.4.7-windows-x64"
    staging = app_root / ".runtime" / "update_staging"
    staging.mkdir(parents=True)

    script_path = _write_update_script(app_root, staging)
    script = script_path.read_text(encoding="utf-8-sig")

    assert "$wsh.Run(('\"' + $exePath + '\"'), 0, $false)" in script
    assert "$wsh.Run(('python \"' + $pyLauncher + '\"'), 0, $false)" in script
    assert '"tools"' in script
    assert "update-transaction-" in script
    assert "SUPERCOM_DATA_ROOT" in script
    escaped_script_path = str(script_path).replace("'", "''")
    subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            (
                "[void][ScriptBlock]::Create([IO.File]::ReadAllText("
                f"'{escaped_script_path}'))"
            ),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
