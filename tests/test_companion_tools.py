from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from agent_loop_system.tools.companion_tools import (
    CompanionToolError,
    SUPERCOM_PAYLOAD_MANIFEST,
    ensure_supercom_installation,
    write_supercom_payload_manifest,
)


def _write_seed(root: Path) -> Path:
    root.mkdir(parents=True)
    (root / "SuperCom.exe").write_bytes(b"new-supercom")
    (root / "SuperCom.exe.config").write_bytes(b"new-config")
    (root / "user_data.sqlite").write_bytes(b"template-data")
    write_supercom_payload_manifest(root, release_version="0.4.8")
    return root


def test_bootstrap_replaces_legacy_program_and_migrates_database_once(
    tmp_path: Path,
) -> None:
    app_root = tmp_path / "Agent-loop-system-0.4.6"
    seed = _write_seed(tmp_path / "seed")
    installed = app_root / "tools" / "SuperCom"
    installed.mkdir(parents=True)
    (installed / "SuperCom.exe").write_bytes(b"old-supercom")
    (installed / "user_data.sqlite").write_bytes(b"user-commands")
    data_root = tmp_path / "stable-data"

    state = ensure_supercom_installation(
        app_root,
        embedded_root=seed,
        data_root=data_root,
        process_is_running=lambda: False,
    )

    assert state["status"] == "updated"
    assert state["updated"] is True
    assert state["migrated_data"] is True
    assert (data_root / "user_data.sqlite").read_bytes() == b"user-commands"
    assert (installed / "SuperCom.exe").read_bytes() == b"new-supercom"
    backup = Path(str(state["backup_path"]))
    assert (backup / "SuperCom.exe").read_bytes() == b"old-supercom"
    assert (backup / "user_data.sqlite").read_bytes() == b"user-commands"


def test_bootstrap_is_idempotent_and_never_overwrites_stable_database(
    tmp_path: Path,
) -> None:
    app_root = tmp_path / "app"
    seed = _write_seed(tmp_path / "seed")
    installed = app_root / "tools" / "SuperCom"
    installed.parent.mkdir(parents=True)
    shutil.copytree(seed, installed)
    data_root = tmp_path / "stable-data"
    data_root.mkdir()
    (data_root / "user_data.sqlite").write_bytes(b"saved-user-data")

    state = ensure_supercom_installation(
        app_root,
        embedded_root=seed,
        data_root=data_root,
        process_is_running=lambda: False,
    )

    assert state["status"] == "current"
    assert state["updated"] is False
    assert state["migrated_data"] is False
    assert (data_root / "user_data.sqlite").read_bytes() == b"saved-user-data"

    # Mutable user data and runtime logs must not trigger a program reinstall.
    (installed / "user_data.sqlite").write_bytes(b"runtime-database-change")
    (installed / "app_logs").mkdir()
    (installed / "app_logs" / "latest.log").write_text("runtime log")
    second = ensure_supercom_installation(
        app_root,
        embedded_root=seed,
        data_root=data_root,
        process_is_running=lambda: False,
    )
    assert second["status"] == "current"
    assert (installed / "user_data.sqlite").read_bytes() == b"runtime-database-change"


def test_bootstrap_records_pending_error_when_supercom_is_running(
    tmp_path: Path,
) -> None:
    app_root = tmp_path / "app"
    seed = _write_seed(tmp_path / "seed")
    installed = app_root / "tools" / "SuperCom"
    installed.mkdir(parents=True)
    (installed / "SuperCom.exe").write_bytes(b"old-supercom")
    (installed / "user_data.sqlite").write_bytes(b"user-commands")

    with pytest.raises(CompanionToolError, match="SuperCom is running"):
        ensure_supercom_installation(
            app_root,
            embedded_root=seed,
            data_root=tmp_path / "stable-data",
            process_is_running=lambda: True,
        )

    assert (installed / "SuperCom.exe").read_bytes() == b"old-supercom"
    assert (
        tmp_path / "stable-data" / "user_data.sqlite"
    ).read_bytes() == b"user-commands"
    state = json.loads(
        (app_root / ".runtime" / "supercom-bootstrap.json").read_text(
            encoding="utf-8"
        )
    )
    assert state["status"] == "error"
    assert "close it and restart" in state["error"]


def test_bootstrap_rejects_tampered_embedded_payload_before_install(
    tmp_path: Path,
) -> None:
    app_root = tmp_path / "app"
    seed = _write_seed(tmp_path / "seed")
    (seed / "SuperCom.exe").write_bytes(b"tampered")

    with pytest.raises(CompanionToolError, match="hash mismatch"):
        ensure_supercom_installation(
            app_root,
            embedded_root=seed,
            data_root=tmp_path / "stable-data",
            process_is_running=lambda: False,
        )

    assert not (app_root / "tools" / "SuperCom").exists()
    assert SUPERCOM_PAYLOAD_MANIFEST in {
        path.name for path in seed.iterdir() if path.is_file()
    }
