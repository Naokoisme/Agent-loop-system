import json
import subprocess
import zipfile
from pathlib import Path
from unittest.mock import patch

from agent_loop_system.tools.auto_updater import (
    launch_update_script,
    prepare_upgrade,
)


def test_prepare_upgrade_extracts_local_release(tmp_path: Path) -> None:
    package_source = tmp_path / "package-source"
    package_source.mkdir()
    (package_source / "Agent-loop.exe").write_bytes(b"portable-executable")
    (package_source / "release_manifest.json").write_text(
        json.dumps({"version": "0.4.2"}),
        encoding="utf-8",
    )
    package_path = tmp_path / "release.zip"
    with zipfile.ZipFile(package_path, "w") as archive:
        archive.write(package_source / "Agent-loop.exe", "Agent-loop.exe")
        archive.write(
            package_source / "release_manifest.json",
            "release_manifest.json",
        )
    manifest_path = tmp_path / "update-manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "latest_version": "0.4.2",
                "packages": {
                    "full_system": {
                        "relative_path": "release.zip",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    app_root = tmp_path / "installed"
    app_root.mkdir()

    with patch(
        "agent_loop_system.tools.auto_updater.get_current_system_version",
        return_value="0.4.1",
    ):
        result = prepare_upgrade(app_root, str(manifest_path))

    payload = Path(result["payload_dir"])
    assert result["status"] == "ready"
    assert result["current_version"] == "0.4.1"
    assert result["target_version"] == "0.4.2"
    assert (payload / "Agent-loop.exe").read_bytes() == b"portable-executable"


def test_launch_update_script_writes_parseable_command_quoting(
    tmp_path: Path,
) -> None:
    app_root = tmp_path / "Agent-loop-system-0.4.1-windows-x64"
    staging = app_root / ".runtime" / "update_staging"
    staging.mkdir(parents=True)

    with (
        patch("agent_loop_system.tools.auto_updater.subprocess.Popen") as popen,
        patch("agent_loop_system.tools.auto_updater.threading.Thread") as thread,
    ):
        launch_update_script(
            app_root,
            staging,
            parent_pid=1234,
            target_version="0.4.2",
        )

    script = (app_root / ".runtime" / "apply_update.ps1").read_text(
        encoding="utf-8-sig"
    )
    assert "$wsh.Run(('\"' + $exePath + '\"'), 0, $false)" in script
    assert "$wsh.Run(('python \"' + $pyLauncher + '\"'), 0, $false)" in script
    assert '$curDirName -match "([0-9]+[.][0-9]+[.][0-9]+)"' in script
    assert '$configSrc = Join-Path $payloadDir ".env"' in script
    assert '$configDst = Join-Path $AppRoot ".env"' in script
    assert "-not (Test-Path $configDst)" in script
    script_path = app_root / ".runtime" / "apply_update.ps1"
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
    popen.assert_called_once()
    thread.return_value.start.assert_called_once_with()
