"""Publish one immutable offline profile release.

The publisher deliberately knows nothing about a specific watch model.  It
copies one firmware artifact and caller-supplied metadata through a local
staging directory into ``.incoming/<publish-id>``, verifies every published
file, renames the verified directory to ``releases/<version>``, and only then
atomically updates ``latest.json``.  When ``download_root`` is supplied, it
also refreshes a verified, human-friendly latest-firmware entry after the
immutable release is complete.

It never writes to legacy ``firmware/`` paths and never replaces an existing
release directory.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping, Sequence


_SAFE_SEGMENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,127}\Z")
_BUFFER_SIZE = 1024 * 1024
_RUNTIME_ASSET_PATHS = {
    "commands": "runtime/commands.json",
    "pages": "runtime/pages.json",
}
_REQUIRED_RUNTIME_METADATA = (
    "project",
    "target",
    "firmware_version",
    "firmware_sha256",
    "automation_protocol_version",
    "agent_loop_min_version",
    "case_map_version",
    "verified_capabilities",
)


class PublishError(RuntimeError):
    """Raised when publishing cannot proceed without risking an old release."""


@dataclass(frozen=True)
class FileRecord:
    path: str
    size: int
    sha256: str


@dataclass(frozen=True)
class PublishResult:
    dry_run: bool
    profile_id: str
    version: str
    publish_id: str
    local_staging: str
    incoming: str
    release: str
    latest: str
    artifact: FileRecord
    runtime_assets: tuple[FileRecord, ...]
    download_artifact: str | None
    download_version_info: str | None


@dataclass(frozen=True)
class DownloadEntryResult:
    profile_id: str
    version: str
    directory: str
    artifact: FileRecord
    version_info: str


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(_BUFFER_SIZE):
            digest.update(chunk)
    return digest.hexdigest().upper()


def _record(path: Path, relative_path: str) -> FileRecord:
    stat = path.stat()
    if not path.is_file():
        raise PublishError(f"expected a regular file: {path}")
    return FileRecord(path=relative_path, size=stat.st_size, sha256=_sha256(path))


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _safe_child(root: Path, *segments: str) -> Path:
    """Resolve a child and reject traversal or a symlink escape."""

    if not segments or any(not segment for segment in segments):
        raise PublishError("destination path contains an empty segment")
    if any(Path(segment).is_absolute() for segment in segments):
        raise PublishError("destination path must be relative to the profile root")
    candidate = root.joinpath(*segments).resolve(strict=False)
    if candidate == root or not _is_within(candidate, root):
        raise PublishError(f"destination escapes profile root: {candidate}")
    return candidate


def _safe_segment(value: str, label: str) -> str:
    if value in {".", ".."} or not _SAFE_SEGMENT.fullmatch(value):
        raise PublishError(
            f"{label} must be one safe path segment containing only letters, "
            "digits, dot, underscore, plus, or hyphen"
        )
    return value


def _safe_filename(value: str, label: str) -> str:
    """Accept a Unicode Windows filename while rejecting traversal/reserved chars."""

    if (
        not value
        or value in {".", ".."}
        or len(value) > 128
        or value != Path(value).name
        or value[-1] in {" ", "."}
        or any(character in '<>:"/\\|?*' for character in value)
        or any(ord(character) < 32 for character in value)
    ):
        raise PublishError(f"{label} must be one safe Windows filename")
    return value


def _lexists(path: Path) -> bool:
    return os.path.lexists(path)


def _ensure_directory(root: Path, *segments: str) -> Path:
    path = _safe_child(root, *segments)
    if _lexists(path):
        if not path.is_dir():
            raise PublishError(f"publish destination is not a directory: {path}")
    else:
        path.mkdir(parents=False)
    # Resolve again after creation so a raced symlink cannot redirect writes.
    resolved = path.resolve(strict=True)
    if not _is_within(resolved, root):
        raise PublishError(f"publish directory escapes profile root: {resolved}")
    return resolved


def _ensure_root_directory(path: Path) -> Path:
    """Create one explicitly requested root below an existing real directory."""

    path = Path(path).resolve(strict=False)
    if _lexists(path):
        if not path.is_dir() or path.is_symlink():
            raise PublishError(f"publish root is not a real directory: {path}")
        return path.resolve(strict=True)
    parent = path.parent.resolve(strict=True)
    path.mkdir(parents=False)
    resolved = path.resolve(strict=True)
    if resolved.parent != parent or resolved.is_symlink():
        raise PublishError(f"publish root was redirected during creation: {resolved}")
    return resolved


def _metadata_object(value: Mapping[str, Any], label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise PublishError(f"{label} metadata must be a JSON object")
    return copy.deepcopy(dict(value))


def _prepare_runtime_payload(
    *,
    runtime_assets: Mapping[str, Path] | None,
    runtime_metadata: Mapping[str, Any] | None,
    profile_id: str,
    artifact_record: FileRecord,
    firmware_metadata: Mapping[str, Any],
) -> tuple[dict[str, Any] | None, dict[str, Path], tuple[FileRecord, ...]]:
    """Validate optional runtime assets and bind them to the published firmware."""

    if runtime_assets is None and runtime_metadata is None:
        return None, {}, ()
    if runtime_assets is None or runtime_metadata is None:
        raise PublishError("runtime assets and runtime metadata must be supplied together")
    if set(runtime_assets) != set(_RUNTIME_ASSET_PATHS):
        raise PublishError(
            "runtime assets must contain exactly: "
            + ", ".join(sorted(_RUNTIME_ASSET_PATHS))
        )

    sources: dict[str, Path] = {}
    records: list[FileRecord] = []
    for name, relative_path in _RUNTIME_ASSET_PATHS.items():
        source = Path(runtime_assets[name]).resolve(strict=True)
        if not source.is_file():
            raise PublishError(f"runtime asset is not a regular file: {source}")
        sources[name] = source
        records.append(_record(source, relative_path))
    if len(set(sources.values())) != len(sources):
        raise PublishError("runtime command and page assets must be distinct files")
    runtime_documents: dict[str, dict[str, Any]] = {}
    for name, source in sources.items():
        try:
            document = json.loads(source.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as exc:
            raise PublishError(f"runtime {name} asset is not valid JSON: {source}") from exc
        if not isinstance(document, dict):
            raise PublishError(f"runtime {name} asset must be a JSON object")
        if document.get("schema_version") != 1 or document.get("project") != profile_id:
            raise PublishError(
                f"runtime {name} asset schema/project does not match the profile"
            )
        runtime_documents[name] = document
    commands_document = runtime_documents["commands"]
    if (
        not isinstance(commands_document.get("capabilities"), list)
        or not commands_document["capabilities"]
        or not str(commands_document.get("catalog") or "").strip()
    ):
        raise PublishError("runtime commands asset must contain capabilities and catalog")
    if not str(runtime_documents["pages"].get("catalog") or "").strip():
        raise PublishError("runtime pages asset must contain a non-empty catalog")

    runtime = _metadata_object(runtime_metadata, "runtime")
    if "assets" in runtime:
        raise PublishError("runtime metadata key 'assets' is reserved")
    for field in _REQUIRED_RUNTIME_METADATA:
        if field not in runtime:
            raise PublishError(f"runtime metadata must contain '{field}'")
    if str(runtime.get("project") or "").strip() != profile_id:
        raise PublishError("runtime project must match the profile id")
    if str(runtime.get("target") or "").strip() != "hardware":
        raise PublishError("runtime target must be 'hardware'")
    expected_hash = artifact_record.sha256
    if str(runtime.get("firmware_sha256") or "").strip().upper() != expected_hash:
        raise PublishError("runtime firmware_sha256 must match the published artifact")
    runtime_version = str(runtime.get("firmware_version") or "").strip()
    if not runtime_version:
        raise PublishError("runtime firmware_version must be non-empty")
    published_value = firmware_metadata.get("runtime_version")
    if isinstance(published_value, Mapping):
        published_version = str(
            published_value.get("ui_firmware_version")
            or published_value.get("project_semver")
            or ""
        ).strip()
    else:
        published_version = str(published_value or "").strip()
    if published_version and runtime_version != published_version:
        raise PublishError("runtime firmware_version must match firmware runtime_version")
    for field in (
        "automation_protocol_version",
        "agent_loop_min_version",
        "case_map_version",
    ):
        if not str(runtime.get(field) or "").strip():
            raise PublishError(f"runtime {field} must be non-empty")
    verified = runtime.get("verified_capabilities")
    if (
        not isinstance(verified, list)
        or not verified
        or any(not isinstance(item, str) or not item.strip() for item in verified)
    ):
        raise PublishError("runtime verified_capabilities must be a non-empty string list")
    runtime["firmware_sha256"] = expected_hash
    runtime["assets"] = {
        name: asdict(record)
        for name, record in zip(_RUNTIME_ASSET_PATHS, records, strict=True)
    }
    return runtime, sources, tuple(records)


def _json_bytes(payload: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def _write_new_file(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def _copy_new_file(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with source.open("rb") as source_stream, destination.open("xb") as target_stream:
        shutil.copyfileobj(source_stream, target_stream, length=_BUFFER_SIZE)
        target_stream.flush()
        os.fsync(target_stream.fileno())


def _verify_files(root: Path, expected: Sequence[FileRecord]) -> None:
    expected_paths = {item.path for item in expected}
    actual_paths = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file()
    }
    if actual_paths != expected_paths:
        raise PublishError(
            "published file inventory mismatch: "
            f"expected={sorted(expected_paths)}, actual={sorted(actual_paths)}"
        )
    for item in expected:
        path = _safe_child(root, *item.path.split("/"))
        actual = _record(path, item.path)
        if actual.size != item.size or actual.sha256 != item.sha256:
            raise PublishError(
                f"published file verification failed for {item.path}: "
                f"expected size/hash {item.size}/{item.sha256}, "
                f"got {actual.size}/{actual.sha256}"
            )


def _load_json_object(argument: str, label: str) -> dict[str, Any]:
    raw = argument.strip()
    if raw.startswith("{"):
        payload = json.loads(raw)
    else:
        metadata_path = Path(raw[1:] if raw.startswith("@") else raw).resolve(strict=True)
        payload = json.loads(metadata_path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict):
        raise PublishError(f"{label} metadata must be a JSON object")
    return payload


def _relative_or_absolute(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return str(path)


def _status_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return value.get("status")
    return value


def publish_download_entry(
    *,
    download_root: Path,
    release: Path,
    latest_artifact_name: str | None = None,
    publish_id: str | None = None,
) -> DownloadEntryResult:
    """Refresh a short, verified latest-firmware entry from an immutable release."""

    publish_id = _safe_segment(publish_id or uuid.uuid4().hex, "publish_id")
    release = Path(release).resolve(strict=True)
    if not release.is_dir() or release.is_symlink():
        raise PublishError(f"release is not a real directory: {release}")

    manifest_path = _safe_child(release, "profile_manifest.json")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PublishError(f"cannot read release profile manifest: {manifest_path}") from exc
    if not isinstance(manifest, dict):
        raise PublishError("release profile manifest must be a JSON object")

    profile = manifest.get("profile")
    firmware = manifest.get("firmware")
    release_metadata = manifest.get("release")
    if not isinstance(profile, Mapping) or not isinstance(firmware, Mapping):
        raise PublishError("release profile manifest is missing profile or firmware metadata")
    if not isinstance(release_metadata, Mapping):
        raise PublishError("release profile manifest is missing release metadata")

    profile_id = _safe_segment(str(profile.get("id", "")).strip(), "profile id")
    version = _safe_segment(str(profile.get("version", "")).strip(), "version")
    if release.name != version:
        raise PublishError("release directory name does not match profile version")

    artifact_metadata = firmware.get("artifact")
    if not isinstance(artifact_metadata, Mapping):
        raise PublishError("release profile manifest is missing firmware artifact metadata")
    relative_artifact = str(artifact_metadata.get("path", "")).strip()
    expected_size = artifact_metadata.get("size")
    expected_hash = str(artifact_metadata.get("sha256", "")).upper()
    if not relative_artifact or not isinstance(expected_size, int) or not expected_hash:
        raise PublishError("release firmware artifact metadata is incomplete")
    artifact_parts = Path(relative_artifact).parts
    if (
        Path(relative_artifact).is_absolute()
        or not artifact_parts
        or any(part in {"", ".", ".."} for part in artifact_parts)
    ):
        raise PublishError("release firmware artifact path is unsafe")
    artifact = _safe_child(release, *artifact_parts)
    actual = _record(artifact, relative_artifact.replace("\\", "/"))
    if actual.size != expected_size or actual.sha256 != expected_hash:
        raise PublishError(
            "immutable firmware verification failed before updating download entry"
        )

    suffix = artifact.suffix or ".bin"
    alias_name = _safe_filename(
        latest_artifact_name or f"{profile_id}_latest{suffix}",
        "latest artifact name",
    )
    download_root = _ensure_root_directory(download_root)
    target_dir = _ensure_directory(download_root, profile_id)
    alias = _safe_child(target_dir, alias_name)
    version_info = _safe_child(target_dir, "版本信息.json")
    alias_record = FileRecord(path=alias_name, size=actual.size, sha256=actual.sha256)

    platform_root = download_root.parent.resolve(strict=True)
    validation = manifest.get("validation")
    validation = validation if isinstance(validation, Mapping) else {}
    baseline = firmware.get("baseline")
    baseline = baseline if isinstance(baseline, Mapping) else {}
    source = manifest.get("source")
    source = source if isinstance(source, Mapping) else {}
    root_commit = baseline.get("root_commit") or source.get("root_commit")
    if not root_commit and isinstance(source.get("repositories"), Sequence):
        root_repository = next(
            (
                repository
                for repository in source["repositories"]
                if isinstance(repository, Mapping) and repository.get("id") == "root"
            ),
            None,
        )
        if root_repository:
            root_commit = root_repository.get("head")
    info_payload = {
        "schema_version": 1,
        "profile_id": profile_id,
        "profile_version": version,
        "release_status": profile.get("release_status"),
        "runtime_version": firmware.get("runtime_version"),
        "root_commit": root_commit,
        "artifact": asdict(alias_record),
        "immutable_release": _relative_or_absolute(release, platform_root),
        "immutable_artifact": _relative_or_absolute(artifact, platform_root),
        "profile_manifest_sha256": _sha256(manifest_path),
        "validation": {
            "build": _status_value(validation.get("build")),
            "flash": _status_value(validation.get("flash")),
            "device": _status_value(validation.get("device")),
        },
        "updated_at": release_metadata.get("published_at"),
    }

    lock_path = _safe_child(target_dir, ".download-entry.lock")
    artifact_temp = _safe_child(target_dir, f".{alias_name}.{publish_id}.tmp")
    info_temp = _safe_child(target_dir, f".version-info.{publish_id}.tmp")
    try:
        _write_new_file(lock_path, (publish_id + "\n").encode("ascii"))
    except FileExistsError as exc:
        raise PublishError(f"another publisher holds the download-entry lock: {lock_path}") from exc

    try:
        for replaceable in (alias, version_info):
            if _lexists(replaceable) and (
                not replaceable.is_file() or replaceable.is_symlink()
            ):
                raise PublishError(
                    f"download entry is not a replaceable regular file: {replaceable}"
                )

        _copy_new_file(artifact, artifact_temp)
        copied = _record(artifact_temp, alias_name)
        if copied.size != alias_record.size or copied.sha256 != alias_record.sha256:
            raise PublishError("download artifact temporary-file verification failed")
        _write_new_file(info_temp, _json_bytes(info_payload))
        if json.loads(info_temp.read_text(encoding="utf-8")) != info_payload:
            raise PublishError("download version-info temporary-file verification failed")

        os.replace(artifact_temp, alias)
        published = _record(alias, alias_name)
        if published != alias_record:
            raise PublishError("download artifact verification failed after atomic replacement")
        os.replace(info_temp, version_info)
        if json.loads(version_info.read_text(encoding="utf-8")) != info_payload:
            raise PublishError("download version-info verification failed after atomic replacement")
    except FileExistsError as exc:
        raise PublishError(f"refusing to replace an unsafe download-entry path: {exc}") from exc
    finally:
        for transient in (artifact_temp, info_temp, lock_path):
            if _lexists(transient) and transient.is_file() and not transient.is_symlink():
                transient.unlink()

    return DownloadEntryResult(
        profile_id=profile_id,
        version=version,
        directory=str(target_dir),
        artifact=alias_record,
        version_info=str(version_info),
    )


def publish_profile(
    *,
    profile_root: Path,
    version: str,
    artifact: Path,
    profile_metadata: Mapping[str, Any],
    firmware_metadata: Mapping[str, Any],
    source_metadata: Mapping[str, Any],
    validation_metadata: Mapping[str, Any],
    runtime_assets: Mapping[str, Path] | None = None,
    runtime_metadata: Mapping[str, Any] | None = None,
    artifact_name: str | None = None,
    download_root: Path | None = None,
    latest_artifact_name: str | None = None,
    staging_root: Path | None = None,
    dry_run: bool = False,
    publish_id: str | None = None,
    published_at: str | None = None,
) -> PublishResult:
    """Publish one version, or return the fully validated plan in dry-run mode."""

    version = _safe_segment(version, "version")
    publish_id = _safe_segment(publish_id or uuid.uuid4().hex, "publish_id")
    artifact = Path(artifact).resolve(strict=True)
    if not artifact.is_file():
        raise PublishError(f"artifact is not a regular file: {artifact}")
    artifact_name = _safe_segment(artifact_name or artifact.name, "artifact_name")

    profile_root = Path(profile_root).resolve(strict=True)
    if not profile_root.is_dir():
        raise PublishError(f"profile root is not a directory: {profile_root}")

    profile = _metadata_object(profile_metadata, "profile")
    firmware = _metadata_object(firmware_metadata, "firmware")
    source = _metadata_object(source_metadata, "source")
    validation = _metadata_object(validation_metadata, "validation")
    profile_id = str(profile.get("id", "")).strip()
    if not profile_id:
        raise PublishError("profile metadata must contain a non-empty 'id'")
    profile_id = _safe_segment(profile_id, "profile id")
    supplied_version = profile.get("version")
    if supplied_version is not None and supplied_version != version:
        raise PublishError("profile metadata version does not match --version")
    if "artifact" in firmware:
        raise PublishError("firmware metadata key 'artifact' is reserved")
    profile["version"] = version

    relative_artifact = f"firmware/{artifact_name}"
    artifact_record = _record(artifact, relative_artifact)
    runtime_payload, runtime_sources, runtime_records = _prepare_runtime_payload(
        runtime_assets=runtime_assets,
        runtime_metadata=runtime_metadata,
        profile_id=profile_id,
        artifact_record=artifact_record,
        firmware_metadata=firmware,
    )
    published_at = published_at or datetime.now(UTC).isoformat().replace("+00:00", "Z")
    incoming = _safe_child(profile_root, ".incoming", publish_id)
    release = _safe_child(profile_root, "releases", version)
    latest = _safe_child(profile_root, "latest.json")
    download_artifact: str | None = None
    download_version_info: str | None = None
    if download_root is not None:
        download_root = Path(download_root).resolve(strict=False)
        alias_name = _safe_filename(
            latest_artifact_name or f"{profile_id}_latest{artifact.suffix or '.bin'}",
            "latest artifact name",
        )
        download_directory = download_root / profile_id
        download_artifact = str(download_directory / alias_name)
        download_version_info = str(download_directory / "版本信息.json")
    elif latest_artifact_name is not None:
        raise PublishError("latest_artifact_name requires download_root")

    if _lexists(release):
        raise PublishError(f"immutable release already exists; refusing to overwrite: {release}")
    if _lexists(incoming):
        raise PublishError(f"incoming publish id already exists: {incoming}")

    staging_root = Path(
        staging_root
        or Path(tempfile.gettempdir()) / "agent-loop-profile-publisher"
    ).resolve(strict=False)
    if staging_root == profile_root or _is_within(staging_root, profile_root):
        raise PublishError("local staging root must be outside the profile root")
    local_staging = staging_root / publish_id
    if _lexists(local_staging):
        raise PublishError(f"local staging path already exists: {local_staging}")

    result = PublishResult(
        dry_run=dry_run,
        profile_id=profile_id,
        version=version,
        publish_id=publish_id,
        local_staging=str(local_staging),
        incoming=str(incoming),
        release=str(release),
        latest=str(latest),
        artifact=artifact_record,
        runtime_assets=runtime_records,
        download_artifact=download_artifact,
        download_version_info=download_version_info,
    )
    if dry_run:
        return result

    # Create destination control directories only after all dry-run validation.
    _ensure_directory(profile_root, "releases")
    _ensure_directory(profile_root, ".incoming")
    locks_dir = _ensure_directory(profile_root, ".publish-locks")
    lock_path = _safe_child(locks_dir, f"{version}.lock")
    try:
        _write_new_file(lock_path, (publish_id + "\n").encode("ascii"))
    except FileExistsError as exc:
        raise PublishError(f"another publisher holds the version lock: {lock_path}") from exc

    try:
        # Recheck under the cooperative per-version lock.
        if _lexists(release):
            raise PublishError(
                f"immutable release already exists; refusing to overwrite: {release}"
            )

        local_staging.mkdir(parents=True, exist_ok=False)
        local_artifact = local_staging / relative_artifact
        _copy_new_file(artifact, local_artifact)
        for name, source_path in runtime_sources.items():
            _copy_new_file(
                source_path,
                local_staging / _RUNTIME_ASSET_PATHS[name],
            )

        firmware_payload = copy.deepcopy(firmware)
        firmware_payload["artifact"] = asdict(artifact_record)
        profile_manifest = {
            "schema_version": 1,
            "profile": profile,
            "firmware": firmware_payload,
            "source": source,
            "validation": validation,
            "release": {
                "immutable": True,
                "path": f"releases/{version}",
                "publish_id": publish_id,
                "published_at": published_at,
            },
        }
        if runtime_payload is not None:
            profile_manifest["runtime"] = runtime_payload
        firmware_manifest = {
            "schema_version": 1,
            "profile_id": profile_id,
            "profile_version": version,
            "firmware": firmware_payload,
        }
        _write_new_file(
            local_staging / "profile_manifest.json", _json_bytes(profile_manifest)
        )
        _write_new_file(
            local_staging / "firmware_manifest.json", _json_bytes(firmware_manifest)
        )

        payload_records = [
            _record(local_staging / relative_artifact, relative_artifact),
            *(
                _record(local_staging / item.path, item.path)
                for item in runtime_records
            ),
            _record(local_staging / "firmware_manifest.json", "firmware_manifest.json"),
            _record(local_staging / "profile_manifest.json", "profile_manifest.json"),
        ]
        sums = "".join(f"{item.sha256}  {item.path}\n" for item in payload_records)
        _write_new_file(local_staging / "SHA256SUMS.txt", sums.encode("ascii"))
        expected_records = [
            *payload_records,
            _record(local_staging / "SHA256SUMS.txt", "SHA256SUMS.txt"),
        ]
        _verify_files(local_staging, expected_records)

        # Copy explicit verified files instead of recursively moving an
        # untrusted caller-provided directory.
        incoming.mkdir(parents=False, exist_ok=False)
        for item in expected_records:
            source_path = _safe_child(local_staging, *item.path.split("/"))
            destination_path = _safe_child(incoming, *item.path.split("/"))
            _copy_new_file(source_path, destination_path)
        _verify_files(incoming, expected_records)

        if _lexists(release):
            raise PublishError(
                f"immutable release appeared during publish; refusing overwrite: {release}"
            )
        # On Windows (the supported NAS publishing host), os.rename does not
        # replace an existing destination.  The lock plus the final existence
        # check protects cooperating publishers on other platforms as well.
        os.rename(incoming, release)
        _verify_files(release, expected_records)

        profile_manifest_record = next(
            item for item in expected_records if item.path == "profile_manifest.json"
        )
        latest_payload = {
            "schema_version": 1,
            "profile_id": profile_id,
            "version": version,
            "release": f"releases/{version}",
            "profile_manifest_sha256": profile_manifest_record.sha256,
            "updated_at": published_at,
        }
        latest_temp = _safe_child(profile_root, f".latest.{publish_id}.tmp")
        _write_new_file(latest_temp, _json_bytes(latest_payload))
        if json.loads(latest_temp.read_text(encoding="utf-8")) != latest_payload:
            raise PublishError("latest.json temporary file verification failed")
        if _lexists(latest) and (not latest.is_file() or latest.is_symlink()):
            raise PublishError(f"latest.json is not a replaceable regular file: {latest}")
        os.replace(latest_temp, latest)
        if json.loads(latest.read_text(encoding="utf-8")) != latest_payload:
            raise PublishError("latest.json verification failed after atomic replacement")

        if download_root is not None:
            download_result = publish_download_entry(
                download_root=download_root,
                release=release,
                latest_artifact_name=latest_artifact_name,
                publish_id=publish_id,
            )
            if (
                str(Path(download_result.directory) / download_result.artifact.path)
                != download_artifact
                or download_result.version_info != download_version_info
            ):
                raise PublishError("download-entry destination differed from validated plan")
    except FileExistsError as exc:
        raise PublishError(f"refusing to replace an existing publish path: {exc}") from exc
    finally:
        # The lock is transient coordination state, not release history.
        if _lexists(lock_path) and lock_path.is_file() and not lock_path.is_symlink():
            lock_path.unlink()

    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Publish an immutable offline profile with verified manifests"
    )
    parser.add_argument("--profile-root", required=True, type=Path)
    parser.add_argument("--version", required=True)
    parser.add_argument("--artifact", required=True, type=Path)
    parser.add_argument("--artifact-name")
    parser.add_argument(
        "--download-root",
        type=Path,
        help="optional human-facing download root updated after immutable publishing",
    )
    parser.add_argument(
        "--latest-artifact-name",
        help="filename under <download-root>/<profile-id> (supports Unicode)",
    )
    parser.add_argument("--staging-root", type=Path)
    parser.add_argument("--profile-metadata", required=True)
    parser.add_argument("--firmware-metadata", required=True)
    parser.add_argument("--source-metadata", required=True)
    parser.add_argument("--validation-metadata", required=True)
    parser.add_argument("--runtime-commands", type=Path)
    parser.add_argument("--runtime-pages", type=Path)
    parser.add_argument(
        "--runtime-metadata",
        help="JSON object, JSON file, or @JSON-file for the source-free hardware runtime profile",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="validate inputs and print destinations without writing anything",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        runtime_args = (
            args.runtime_commands,
            args.runtime_pages,
            args.runtime_metadata,
        )
        if any(value is not None for value in runtime_args) and not all(
            value is not None for value in runtime_args
        ):
            raise PublishError(
                "--runtime-commands, --runtime-pages and --runtime-metadata must be supplied together"
            )
        result = publish_profile(
            profile_root=args.profile_root,
            version=args.version,
            artifact=args.artifact,
            artifact_name=args.artifact_name,
            download_root=args.download_root,
            latest_artifact_name=args.latest_artifact_name,
            staging_root=args.staging_root,
            profile_metadata=_load_json_object(args.profile_metadata, "profile"),
            firmware_metadata=_load_json_object(args.firmware_metadata, "firmware"),
            source_metadata=_load_json_object(args.source_metadata, "source"),
            validation_metadata=_load_json_object(args.validation_metadata, "validation"),
            runtime_assets=(
                {"commands": args.runtime_commands, "pages": args.runtime_pages}
                if args.runtime_commands is not None and args.runtime_pages is not None
                else None
            ),
            runtime_metadata=(
                _load_json_object(args.runtime_metadata, "runtime")
                if args.runtime_metadata is not None
                else None
            ),
            dry_run=args.dry_run,
        )
    except (OSError, ValueError, json.JSONDecodeError, PublishError) as exc:
        print(f"publish failed: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(asdict(result), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
