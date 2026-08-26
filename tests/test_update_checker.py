import json
from pathlib import Path
from types import SimpleNamespace
import zipfile

import pytest
from agent_loop_system.tools import auto_updater
from agent_loop_system.tools.update_checker import (
    DEFAULT_MANIFEST_PATH,
    check_for_updates,
    get_current_system_version,
    get_manifest_source,
)


def test_auto_updater_module_is_importable() -> None:
    assert callable(auto_updater.launch_update_script)


def test_auto_updater_quotes_powershell_data_without_interpolation() -> None:
    assert auto_updater._powershell_literal(r"C:\Agent's $root") == (
        r"'C:\Agent''s $root'"
    )


def test_auto_updater_rejects_zip_path_traversal(tmp_path: Path) -> None:
    archive_path = tmp_path / "unsafe.zip"
    destination = tmp_path / "staging"
    destination.mkdir()
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("../escaped.txt", "unsafe")

    with zipfile.ZipFile(archive_path, "r") as archive:
        with pytest.raises(auto_updater.AutoUpdaterError) as raised:
            auto_updater._extract_zip_safely(archive, destination)

    assert raised.value.error_code == "UNSAFE_ARCHIVE"
    assert not (tmp_path / "escaped.txt").exists()


def test_auto_updater_writes_quoted_restart_commands(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(auto_updater.subprocess, "Popen", lambda *args, **kwargs: object())
    monkeypatch.setattr(
        auto_updater.threading,
        "Thread",
        lambda *args, **kwargs: SimpleNamespace(start=lambda: None),
    )

    auto_updater.launch_update_script(
        tmp_path,
        tmp_path / "staging",
        parent_pid=123,
        target_version="0.4.3",
    )

    script = (tmp_path / ".runtime" / "apply_update.ps1").read_text(
        encoding="utf-8-sig"
    )
    assert "$wsh.Run(('\"' + $exePath + '\"'), 0, $false)" in script
    assert "$wsh.Run(('python \"' + $pyLauncher + '\"'), 0, $false)" in script
    assert '"tools"' in script
    assert "update-transaction-" in script
    assert "trap {" in script
    assert "Move-Item -LiteralPath $dst -Destination $backup" in script
    assert "已恢复升级前版本" in script


def test_manifest_source_defaults_to_official_nas(monkeypatch) -> None:
    monkeypatch.delenv("W30_UPDATE_MANIFEST_URL", raising=False)
    monkeypatch.delenv("W30_NAS_MANIFEST_PATH", raising=False)

    assert get_manifest_source() == DEFAULT_MANIFEST_PATH


def test_current_version_falls_back_to_source_version(monkeypatch, tmp_path: Path) -> None:
    from agent_loop_system import runtime_root

    monkeypatch.delenv("AGENT_LOOP_VERSION", raising=False)
    monkeypatch.setattr(runtime_root, "resolve_app_root", lambda: tmp_path)

    assert get_current_system_version() == "0.4.8"


def test_manifest_source_precedence(monkeypatch) -> None:
    monkeypatch.setenv("W30_NAS_MANIFEST_PATH", "nas-manifest.json")
    monkeypatch.setenv("W30_UPDATE_MANIFEST_URL", "https://example.invalid/update.json")

    assert get_manifest_source() == "https://example.invalid/update.json"
    assert get_manifest_source("explicit-manifest.json") == "explicit-manifest.json"


def test_check_for_updates_reads_explicit_manifest(tmp_path: Path) -> None:
    manifest_path = tmp_path / "update-manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "latest_version": "0.4.2",
                "packages": {"full_system": {"relative_path": "release.zip"}},
            }
        ),
        encoding="utf-8",
    )

    result = check_for_updates(manifest_path, current_version="0.4.1")

    assert result["has_update"] is True
    assert result["status"] == "update_available"
    assert result["latest_version"] == "0.4.2"
