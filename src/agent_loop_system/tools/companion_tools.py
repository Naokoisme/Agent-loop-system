"""Install and repair companion programs bundled inside the frozen package.

The 0.4.6/0.4.7 updater copied a fixed set of top-level paths and omitted
``tools``. A v0.4.8 package therefore carries an immutable SuperCom seed under
``_internal``. On first start this module transactionally installs that seed,
after migrating the legacy database to a stable per-user data directory.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Callable


SUPERCOM_PAYLOAD_MANIFEST = "agent_loop_payload_manifest.json"
SUPERCOM_DATABASE_NAME = "user_data.sqlite"
SUPERCOM_SEED_RELATIVE_PATH = Path(
    "_internal/agent_loop_system/companion_payloads/SuperCom"
)


class CompanionToolError(RuntimeError):
    """A bundled companion program could not be validated or installed."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_relative_path(value: object) -> str:
    raw = str(value or "").replace("\\", "/").strip()
    candidate = PurePosixPath(raw)
    if (
        not raw
        or candidate.is_absolute()
        or ".." in candidate.parts
        or ":" in raw
    ):
        raise CompanionToolError(f"SuperCom payload contains unsafe path: {raw!r}")
    return candidate.as_posix()


def build_supercom_payload_manifest(
    payload_root: Path,
    *,
    release_version: str,
) -> dict[str, object]:
    """Build a deterministic content manifest for one copied SuperCom payload."""

    root = payload_root.resolve()
    files: list[dict[str, object]] = []
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        relative = path.relative_to(root).as_posix()
        if relative == SUPERCOM_PAYLOAD_MANIFEST:
            continue
        files.append(
            {
                "path": relative,
                "size_bytes": path.stat().st_size,
                "sha256": _sha256(path),
            }
        )
    if not files:
        raise CompanionToolError("SuperCom payload is empty")
    return {
        "schema_version": 1,
        "tool": "SuperCom",
        "release_version": str(release_version).strip(),
        "files": files,
    }


def write_supercom_payload_manifest(
    payload_root: Path,
    *,
    release_version: str,
) -> Path:
    manifest_path = payload_root / SUPERCOM_PAYLOAD_MANIFEST
    manifest = build_supercom_payload_manifest(
        payload_root,
        release_version=release_version,
    )
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return manifest_path


def _validated_manifest(
    payload_root: Path,
    *,
    require_exact_files: bool = True,
    mutable_files: frozenset[str] = frozenset(),
) -> dict[str, object]:
    manifest_path = payload_root / SUPERCOM_PAYLOAD_MANIFEST
    if not manifest_path.is_file():
        raise CompanionToolError(
            f"SuperCom payload manifest is missing: {manifest_path}"
        )
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CompanionToolError(f"Invalid SuperCom payload manifest: {exc}") from exc
    if manifest.get("schema_version") != 1 or manifest.get("tool") != "SuperCom":
        raise CompanionToolError("Unsupported SuperCom payload manifest")
    if not str(manifest.get("release_version") or "").strip():
        raise CompanionToolError("SuperCom payload release_version is missing")

    entries = manifest.get("files")
    if not isinstance(entries, list) or not entries:
        raise CompanionToolError("SuperCom payload file list is empty")
    expected: set[str] = set()
    for item in entries:
        if not isinstance(item, dict):
            raise CompanionToolError("Invalid SuperCom payload file entry")
        relative = _safe_relative_path(item.get("path"))
        if relative in expected:
            raise CompanionToolError(f"Duplicate SuperCom payload path: {relative}")
        expected.add(relative)
        path = payload_root / Path(relative)
        if not path.is_file():
            raise CompanionToolError(f"SuperCom payload file is missing: {relative}")
        try:
            expected_size = int(item.get("size_bytes"))
        except (TypeError, ValueError) as exc:
            raise CompanionToolError(
                f"Invalid SuperCom payload size: {relative}"
            ) from exc
        expected_hash = str(item.get("sha256") or "").lower()
        if relative not in mutable_files and (
            path.stat().st_size != expected_size or _sha256(path) != expected_hash
        ):
            raise CompanionToolError(f"SuperCom payload hash mismatch: {relative}")

    actual = {
        path.relative_to(payload_root).as_posix()
        for path in payload_root.rglob("*")
        if path.is_file()
        and path.relative_to(payload_root).as_posix() != SUPERCOM_PAYLOAD_MANIFEST
    }
    if require_exact_files and actual != expected:
        raise CompanionToolError(
            "SuperCom payload file set does not match its manifest"
        )
    return manifest


def _payload_matches(install_root: Path, expected_manifest: dict[str, object]) -> bool:
    manifest_path = install_root / SUPERCOM_PAYLOAD_MANIFEST
    if not manifest_path.is_file():
        return False
    try:
        installed_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if installed_manifest != expected_manifest:
            return False
        _validated_manifest(
            install_root,
            require_exact_files=False,
            mutable_files=frozenset({SUPERCOM_DATABASE_NAME}),
        )
    except (OSError, json.JSONDecodeError, CompanionToolError):
        return False
    return True


def resolve_supercom_data_root(explicit_root: Path | None = None) -> Path:
    if explicit_root is not None:
        return explicit_root.resolve()
    configured = os.environ.get("SUPERCOM_DATA_ROOT", "").strip()
    if configured:
        return Path(os.path.expandvars(os.path.expanduser(configured))).resolve()
    local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
    if not local_app_data:
        raise CompanionToolError(
            "LOCALAPPDATA is unavailable; cannot resolve stable SuperCom data path"
        )
    return Path(local_app_data).resolve() / "TOPSTEP" / "Agent-loop" / "SuperCom"


def _supercom_is_running() -> bool:
    if os.name != "nt":
        return False
    try:
        result = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq SuperCom.exe", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return "supercom.exe" in result.stdout.lower()


def _write_state(app_root: Path, state: dict[str, object]) -> None:
    state_path = app_root / ".runtime" / "supercom-bootstrap.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = state_path.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(state, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(state_path)


def ensure_supercom_installation(
    app_root: Path,
    *,
    embedded_root: Path | None = None,
    data_root: Path | None = None,
    process_is_running: Callable[[], bool] = _supercom_is_running,
) -> dict[str, object]:
    """Install the bundled SuperCom payload without overwriting user data."""

    root = app_root.resolve()
    seed = (embedded_root or (root / SUPERCOM_SEED_RELATIVE_PATH)).resolve()
    if not seed.is_dir():
        return {"status": "not_bundled", "updated": False, "migrated_data": False}

    state: dict[str, object] = {
        "status": "starting",
        "updated": False,
        "migrated_data": False,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    try:
        manifest = _validated_manifest(seed)
        state["release_version"] = manifest["release_version"]
        install_root = root / "tools" / "SuperCom"
        stable_root = resolve_supercom_data_root(data_root)
        stable_database = stable_root / SUPERCOM_DATABASE_NAME
        legacy_database = install_root / SUPERCOM_DATABASE_NAME

        if legacy_database.is_file() and not stable_database.exists():
            stable_root.mkdir(parents=True, exist_ok=True)
            shutil.copy2(legacy_database, stable_database)
            state["migrated_data"] = True

        if _payload_matches(install_root, manifest):
            state["status"] = "current"
            _write_state(root, state)
            return state

        if process_is_running():
            raise CompanionToolError(
                "SuperCom is running; close it and restart Agent-loop to finish the program update"
            )

        transaction_root = root / ".runtime" / f"supercom-update-{uuid.uuid4().hex}"
        staged = transaction_root / "new"
        backup = transaction_root / "backup"
        transaction_root.mkdir(parents=True, exist_ok=False)
        shutil.copytree(seed, staged)
        _validated_manifest(staged)
        install_root.parent.mkdir(parents=True, exist_ok=True)

        moved_old = False
        try:
            if install_root.exists():
                shutil.move(str(install_root), str(backup))
                moved_old = True
            shutil.move(str(staged), str(install_root))
            _validated_manifest(install_root)
        except Exception:
            if install_root.exists():
                failed = transaction_root / "failed-new"
                if not failed.exists():
                    shutil.move(str(install_root), str(failed))
            if moved_old and backup.exists() and not install_root.exists():
                shutil.move(str(backup), str(install_root))
            raise

        state.update(
            {
                "status": "updated",
                "updated": True,
                "backup_path": str(backup) if backup.exists() else None,
            }
        )
        _write_state(root, state)
        return state
    except Exception as exc:
        state.update({"status": "error", "error": str(exc)})
        try:
            _write_state(root, state)
        except OSError:
            pass
        if isinstance(exc, CompanionToolError):
            raise
        raise CompanionToolError(f"SuperCom bootstrap failed: {exc}") from exc
