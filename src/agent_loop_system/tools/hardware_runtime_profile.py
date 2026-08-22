"""Source-free runtime capabilities for an already-built hardware release.

Ordinary fixed-case and exploration runs consume this immutable profile.  The
firmware source workspace remains an engineering input used only when the
profile is generated, diagnosed, repaired, or rebuilt.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

from agent_loop_system.runtime_root import RuntimePaths
from agent_loop_system.tools.hardware_serial import (
    BRIDGE_PROTOCOL,
    BRIDGE_PROTOCOL_VERSION,
)
from agent_loop_system.tools.hardware_target import (
    DEFAULT_HARDWARE_PROJECT,
    hardware_command_allowed,
)
from agent_loop_system.version import __version__ as AGENT_LOOP_VERSION


_SAFE_SEGMENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,127}\Z")
_SHA256 = re.compile(r"[0-9A-F]{64}\Z")
_REQUIRED_RUNTIME_FIELDS = (
    "project",
    "target",
    "firmware_version",
    "firmware_sha256",
    "automation_protocol_version",
    "agent_loop_min_version",
    "case_map_version",
    "verified_capabilities",
    "assets",
)
_REQUIRED_ASSETS = {
    "commands": "runtime/commands.json",
    "pages": "runtime/pages.json",
}


class HardwareRuntimeProfileError(RuntimeError):
    """Raised when a hardware runtime profile is absent or incompatible."""


@dataclass(frozen=True, slots=True)
class RuntimeCommandCapability:
    name: str
    handler: str
    available: bool
    unavailable_reason: str | None = None


@dataclass(frozen=True, slots=True)
class HardwareRuntimeProfile:
    profile_id: str
    version: str
    project: str
    target: str
    firmware_version: str
    firmware_sha256: str
    automation_protocol_version: str
    agent_loop_min_version: str
    case_map_version: str
    verified_capabilities: tuple[str, ...]
    release_dir: Path
    command_capabilities: Mapping[str, RuntimeCommandCapability]
    command_catalog: str
    page_catalog: str

    @property
    def agent_knowledge(self) -> str:
        return (
            f"## 当前 {self.project} 真机命令与参数（运行时档案 {self.version}）\n"
            f"{self.command_catalog.strip()}\n\n"
            f"## 当前 {self.project} 注册页面\n"
            f"{self.page_catalog.strip()}"
        )

    @property
    def compatibility_summary(self) -> dict[str, Any]:
        return {
            "profile_id": self.profile_id,
            "profile_version": self.version,
            "project": self.project,
            "target": self.target,
            "firmware_version": self.firmware_version,
            "firmware_sha256": self.firmware_sha256,
            "automation_protocol_version": self.automation_protocol_version,
            "agent_loop_min_version": self.agent_loop_min_version,
            "case_map_version": self.case_map_version,
            "verified_capabilities": list(self.verified_capabilities),
        }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest().upper()


def _safe_segment(value: str, label: str) -> str:
    value = str(value or "").strip()
    if value in {".", ".."} or not _SAFE_SEGMENT.fullmatch(value):
        raise HardwareRuntimeProfileError(f"{label} 不是安全的单段名称: {value!r}")
    return value


def _read_json_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError as exc:
        raise HardwareRuntimeProfileError(f"{label} 不存在: {path}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise HardwareRuntimeProfileError(f"{label} 无法读取: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise HardwareRuntimeProfileError(f"{label} 必须是 JSON 对象: {path}")
    return value


def _release_manifest_hash(release: Path) -> str:
    sums_path = release / "SHA256SUMS.txt"
    try:
        lines = sums_path.read_text(encoding="ascii").splitlines()
    except OSError as exc:
        raise HardwareRuntimeProfileError(f"发布校验清单无法读取: {sums_path}: {exc}") from exc
    matches = [
        line.split("  ", 1)[0].strip().upper()
        for line in lines
        if line.endswith("  profile_manifest.json") and "  " in line
    ]
    if len(matches) != 1 or not _SHA256.fullmatch(matches[0]):
        raise HardwareRuntimeProfileError("发布校验清单缺少唯一的 profile_manifest.json 哈希")
    return matches[0]


def _asset_path(release: Path, relative: str, label: str) -> Path:
    expected = _REQUIRED_ASSETS[label]
    normalized = str(relative or "").replace("\\", "/")
    if normalized != expected:
        raise HardwareRuntimeProfileError(
            f"运行时档案 {label} 路径不符合约定: {normalized!r}，应为 {expected!r}"
        )
    candidate = (release / Path(*normalized.split("/"))).resolve(strict=False)
    try:
        candidate.relative_to(release)
    except ValueError as exc:
        raise HardwareRuntimeProfileError(f"运行时档案路径越界: {normalized}") from exc
    return candidate


def _verify_asset(release: Path, metadata: object, label: str) -> Path:
    if not isinstance(metadata, Mapping):
        raise HardwareRuntimeProfileError(f"运行时档案缺少 {label} 文件记录")
    path = _asset_path(release, str(metadata.get("path") or ""), label)
    expected_size = metadata.get("size")
    expected_hash = str(metadata.get("sha256") or "").upper()
    if not isinstance(expected_size, int) or expected_size < 0 or not _SHA256.fullmatch(expected_hash):
        raise HardwareRuntimeProfileError(f"运行时档案 {label} 文件记录不完整")
    if not path.is_file() or path.stat().st_size != expected_size or _sha256(path) != expected_hash:
        raise HardwareRuntimeProfileError(f"运行时档案 {label} 文件完整性校验失败: {path}")
    return path


def _non_empty_string(value: object, label: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise HardwareRuntimeProfileError(f"运行时档案缺少 {label}")
    return text


def _version_key(value: str, label: str) -> tuple[int, ...]:
    match = re.fullmatch(
        r"(\d+(?:\.\d+){1,3})(?:[-+][A-Za-z0-9.-]+)?",
        str(value or "").strip(),
    )
    if not match:
        raise HardwareRuntimeProfileError(f"{label} 不是可比较版本号: {value!r}")
    parts = tuple(int(item) for item in match.group(1).split("."))
    return parts + (0,) * (4 - len(parts))


def _load_commands(path: Path, project: str) -> tuple[Mapping[str, RuntimeCommandCapability], str]:
    payload = _read_json_object(path, "命令能力目录")
    if payload.get("schema_version") != 1 or payload.get("project") != project:
        raise HardwareRuntimeProfileError("命令能力目录版本或项目不匹配")
    raw_capabilities = payload.get("capabilities")
    catalog = _non_empty_string(payload.get("catalog"), "命令知识目录")
    if not isinstance(raw_capabilities, list) or not raw_capabilities:
        raise HardwareRuntimeProfileError("命令能力目录为空")

    capabilities: dict[str, RuntimeCommandCapability] = {}
    for item in raw_capabilities:
        if not isinstance(item, Mapping):
            raise HardwareRuntimeProfileError("命令能力目录包含非对象条目")
        name = str(item.get("name") or "").strip().upper()
        allowed, reason = hardware_command_allowed(name)
        if not allowed:
            raise HardwareRuntimeProfileError(
                f"运行时档案包含禁止交给真机 Agent 的命令 {name!r}: {reason}"
            )
        if name in capabilities:
            raise HardwareRuntimeProfileError(f"运行时档案包含重复命令: {name}")
        if not isinstance(item.get("available"), bool):
            raise HardwareRuntimeProfileError(f"命令 {name} 的 available 必须是布尔值")
        capabilities[name] = RuntimeCommandCapability(
            name=name,
            handler=str(item.get("handler") or "").strip(),
            available=bool(item.get("available")),
            unavailable_reason=(
                str(item.get("unavailable_reason")).strip()
                if item.get("unavailable_reason")
                else None
            ),
        )

    for line in catalog.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        name = stripped.partition("|")[0].strip().upper()
        if name not in capabilities:
            raise HardwareRuntimeProfileError(
                f"命令知识目录引用了未登记能力: {name or stripped!r}"
            )
    return MappingProxyType(capabilities), catalog


def _load_pages(path: Path, project: str) -> str:
    payload = _read_json_object(path, "页面能力目录")
    if payload.get("schema_version") != 1 or payload.get("project") != project:
        raise HardwareRuntimeProfileError("页面能力目录版本或项目不匹配")
    return _non_empty_string(payload.get("catalog"), "页面知识目录")


def load_hardware_runtime_profile(
    *,
    project: str | None = None,
    version: str | None = None,
    profiles_root: Path | str | None = None,
) -> HardwareRuntimeProfile:
    """Load and verify one immutable hardware runtime profile without source I/O."""

    selected_project = _safe_segment(
        project or os.environ.get("W30_HARDWARE_PROJECT") or DEFAULT_HARDWARE_PROJECT,
        "真机项目",
    )
    root_value = profiles_root or os.environ.get("W30_HARDWARE_PROFILE_ROOT")
    root = Path(root_value).resolve() if root_value else RuntimePaths.from_root().profiles.resolve()
    profile_root = (root / selected_project).resolve(strict=False)
    try:
        profile_root.relative_to(root)
    except ValueError as exc:
        raise HardwareRuntimeProfileError("真机运行时档案目录越界") from exc

    requested_version = version or os.environ.get("W30_HARDWARE_PROFILE_VERSION")
    latest: dict[str, Any] | None = None
    if requested_version:
        selected_version = _safe_segment(requested_version, "档案版本")
    else:
        latest = _read_json_object(profile_root / "latest.json", "真机运行时 latest.json")
        if latest.get("schema_version") != 1 or latest.get("profile_id") != selected_project:
            raise HardwareRuntimeProfileError("真机运行时 latest.json 的项目或版本格式不匹配")
        selected_version = _safe_segment(str(latest.get("version") or ""), "档案版本")
        expected_release = f"releases/{selected_version}"
        if latest.get("release") != expected_release:
            raise HardwareRuntimeProfileError("真机运行时 latest.json 的 release 路径不匹配")

    release = (profile_root / "releases" / selected_version).resolve(strict=False)
    expected_parent = (profile_root / "releases").resolve(strict=False)
    if release.parent != expected_parent or not release.is_dir():
        raise HardwareRuntimeProfileError(f"真机运行时档案版本不存在: {release}")
    manifest_path = release / "profile_manifest.json"
    sums_manifest_hash = _release_manifest_hash(release)
    try:
        actual_manifest_hash = _sha256(manifest_path)
    except OSError as exc:
        raise HardwareRuntimeProfileError(
            f"真机运行时 profile_manifest.json 无法读取: {manifest_path}: {exc}"
        ) from exc
    if actual_manifest_hash != sums_manifest_hash:
        raise HardwareRuntimeProfileError("真机运行时 profile_manifest.json 与发布校验清单不匹配")
    if latest is not None:
        expected_manifest_hash = str(latest.get("profile_manifest_sha256") or "").upper()
        if not _SHA256.fullmatch(expected_manifest_hash) or actual_manifest_hash != expected_manifest_hash:
            raise HardwareRuntimeProfileError("真机运行时 profile_manifest.json 完整性校验失败")

    manifest = _read_json_object(manifest_path, "真机运行时 profile_manifest.json")
    if manifest.get("schema_version") != 1:
        raise HardwareRuntimeProfileError("真机运行时 profile_manifest.json schema_version 不兼容")
    profile = manifest.get("profile")
    firmware = manifest.get("firmware")
    runtime = manifest.get("runtime")
    release_metadata = manifest.get("release")
    if not isinstance(profile, Mapping) or not isinstance(firmware, Mapping) or not isinstance(runtime, Mapping):
        raise HardwareRuntimeProfileError("真机运行时档案缺少 profile、firmware 或 runtime 元数据")
    if (
        not isinstance(release_metadata, Mapping)
        or release_metadata.get("immutable") is not True
        or release_metadata.get("path") != f"releases/{selected_version}"
    ):
        raise HardwareRuntimeProfileError("真机运行时档案的不可变发布路径不匹配")
    for field in _REQUIRED_RUNTIME_FIELDS:
        if field not in runtime:
            raise HardwareRuntimeProfileError(f"真机运行时档案缺少 runtime.{field}")

    profile_id = _non_empty_string(profile.get("id"), "profile.id")
    profile_version = _non_empty_string(profile.get("version"), "profile.version")
    runtime_project = _non_empty_string(runtime.get("project"), "runtime.project")
    target = _non_empty_string(runtime.get("target"), "runtime.target")
    if profile_id != selected_project or runtime_project != selected_project:
        raise HardwareRuntimeProfileError("真机运行时档案项目与所选项目不匹配")
    if profile_version != selected_version:
        raise HardwareRuntimeProfileError("真机运行时档案版本与发布目录不匹配")
    if target != "hardware":
        raise HardwareRuntimeProfileError(f"运行时档案 target 必须为 hardware，实际为 {target!r}")

    firmware_version = _non_empty_string(runtime.get("firmware_version"), "runtime.firmware_version")
    firmware_sha256 = str(runtime.get("firmware_sha256") or "").strip().upper()
    firmware_artifact = firmware.get("artifact")
    if not isinstance(firmware_artifact, Mapping):
        raise HardwareRuntimeProfileError("真机运行时档案缺少固件文件记录")
    firmware_path = str(firmware_artifact.get("path") or "").replace("\\", "/")
    firmware_size = firmware_artifact.get("size")
    manifest_firmware_hash = str(firmware_artifact.get("sha256") or "").upper()
    if (
        not firmware_path.startswith("firmware/")
        or ".." in Path(firmware_path).parts
        or not isinstance(firmware_size, int)
        or firmware_size <= 0
        or not _SHA256.fullmatch(manifest_firmware_hash)
    ):
        raise HardwareRuntimeProfileError("真机运行时档案的固件文件记录不完整")
    if not _SHA256.fullmatch(firmware_sha256) or firmware_sha256 != manifest_firmware_hash:
        raise HardwareRuntimeProfileError("运行时档案绑定的固件 SHA256 与发布固件不匹配")
    manifest_version_value = firmware.get("runtime_version")
    if isinstance(manifest_version_value, Mapping):
        manifest_firmware_version = str(
            manifest_version_value.get("ui_firmware_version")
            or manifest_version_value.get("project_semver")
            or ""
        ).strip()
    else:
        manifest_firmware_version = str(manifest_version_value or "").strip()
    if manifest_firmware_version and firmware_version != manifest_firmware_version:
        raise HardwareRuntimeProfileError("运行时档案绑定的固件版本与发布固件不匹配")

    verified = runtime.get("verified_capabilities")
    if (
        not isinstance(verified, list)
        or not verified
        or any(not isinstance(item, str) or not item.strip() for item in verified)
    ):
        raise HardwareRuntimeProfileError("runtime.verified_capabilities 必须是非空字符串列表")
    assets = runtime.get("assets")
    if not isinstance(assets, Mapping):
        raise HardwareRuntimeProfileError("运行时档案缺少 runtime.assets")
    commands_path = _verify_asset(release, assets.get("commands"), "commands")
    pages_path = _verify_asset(release, assets.get("pages"), "pages")
    capabilities, command_catalog = _load_commands(commands_path, selected_project)
    page_catalog = _load_pages(pages_path, selected_project)

    automation_protocol_version = _non_empty_string(
        runtime.get("automation_protocol_version"),
        "runtime.automation_protocol_version",
    )
    expected_protocol = f"{BRIDGE_PROTOCOL}/{BRIDGE_PROTOCOL_VERSION}"
    if automation_protocol_version != expected_protocol:
        raise HardwareRuntimeProfileError(
            f"自动化协议不兼容: 档案={automation_protocol_version}, 当前={expected_protocol}"
        )
    agent_loop_min_version = _non_empty_string(
        runtime.get("agent_loop_min_version"),
        "runtime.agent_loop_min_version",
    )
    if _version_key(AGENT_LOOP_VERSION, "当前 Agent-loop 版本") < _version_key(
        agent_loop_min_version,
        "runtime.agent_loop_min_version",
    ):
        raise HardwareRuntimeProfileError(
            f"Agent-loop 版本过低: 当前={AGENT_LOOP_VERSION}, 档案要求>={agent_loop_min_version}"
        )

    return HardwareRuntimeProfile(
        profile_id=profile_id,
        version=profile_version,
        project=runtime_project,
        target=target,
        firmware_version=firmware_version,
        firmware_sha256=firmware_sha256,
        automation_protocol_version=automation_protocol_version,
        agent_loop_min_version=agent_loop_min_version,
        case_map_version=_non_empty_string(runtime.get("case_map_version"), "runtime.case_map_version"),
        verified_capabilities=tuple(str(item).strip() for item in verified),
        release_dir=release,
        command_capabilities=capabilities,
        command_catalog=command_catalog,
        page_catalog=page_catalog,
    )


__all__ = [
    "HardwareRuntimeProfile",
    "HardwareRuntimeProfileError",
    "RuntimeCommandCapability",
    "load_hardware_runtime_profile",
]
