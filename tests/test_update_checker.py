import json
from pathlib import Path

from agent_loop_system.tools.update_checker import (
    DEFAULT_MANIFEST_PATH,
    check_for_updates,
    get_current_system_version,
    get_manifest_source,
)


def test_manifest_source_defaults_to_official_nas(monkeypatch) -> None:
    monkeypatch.delenv("W30_UPDATE_MANIFEST_URL", raising=False)
    monkeypatch.delenv("W30_NAS_MANIFEST_PATH", raising=False)

    assert get_manifest_source() == DEFAULT_MANIFEST_PATH


def test_current_version_falls_back_to_source_version(monkeypatch, tmp_path: Path) -> None:
    from agent_loop_system import runtime_root

    monkeypatch.delenv("AGENT_LOOP_VERSION", raising=False)
    monkeypatch.setattr(runtime_root, "resolve_app_root", lambda: tmp_path)

    assert get_current_system_version() == "0.4.2"


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
