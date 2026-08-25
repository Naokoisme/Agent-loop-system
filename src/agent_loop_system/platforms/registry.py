from __future__ import annotations

import json
from importlib.resources import files
from pathlib import Path
from typing import Any


class PlatformRegistryError(ValueError):
    """A platform/target profile is invalid or cannot be resolved."""


class PlatformRegistry:
    """Read-only registry for platforms and concrete execution targets."""

    def __init__(self, profile_path: Path | None = None):
        if profile_path is None:
            profile_path = Path(str(
                files("agent_loop_system.platform_data").joinpath(
                    "platform_profiles.v1.json"
                )
            ))
        self.profile_path = Path(profile_path)
        payload = json.loads(self.profile_path.read_text(encoding="utf-8"))
        self.version = str(payload.get("version") or "")
        platforms = payload.get("platforms")
        targets = payload.get("targets")
        if not self.version or not isinstance(platforms, list) or not isinstance(targets, list):
            raise PlatformRegistryError("平台配置缺少 version、platforms 或 targets")
        self._platforms = self._index(platforms, "platform_id", "平台")
        self._targets = self._index(targets, "target_id", "执行目标")
        self._validate()

    @staticmethod
    def _index(items: list[Any], field: str, label: str) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        for raw in items:
            if not isinstance(raw, dict):
                raise PlatformRegistryError(f"{label}配置必须是对象")
            key = str(raw.get(field) or "").strip()
            if not key or key in result:
                raise PlatformRegistryError(f"{label}{field} 为空或重复: {key or '空'}")
            result[key] = dict(raw)
        return result

    def _validate(self) -> None:
        for target_id, target in self._targets.items():
            platform_id = str(target.get("platform_id") or "")
            if platform_id not in self._platforms:
                raise PlatformRegistryError(f"执行目标 {target_id} 引用了未知平台 {platform_id}")
            for field in (
                "execution_adapter", "case_catalog_adapter", "health_adapter",
                "execution_resource",
            ):
                if not str(target.get(field) or "").strip():
                    raise PlatformRegistryError(f"执行目标 {target_id} 缺少 {field}")
            if (
                platform_id == "w30"
                and target.get("execution_target") == "hardware"
                and not str(target.get("runtime_profile_id") or "").strip()
            ):
                raise PlatformRegistryError(
                    f"W30 真机执行目标 {target_id} 缺少 runtime_profile_id"
                )
            if platform_id == "579":
                if target.get("transport") != "app_ble":
                    raise PlatformRegistryError("579 transport 必须为 app_ble")
                if target.get("serial_provider") != "com3_readonly":
                    raise PlatformRegistryError("579 serial provider 必须为 com3_readonly")
                if target.get("serial_write_provider"):
                    raise PlatformRegistryError("579 禁止配置 serial write provider")

    def platform(self, platform_id: str) -> dict[str, Any]:
        try:
            return dict(self._platforms[str(platform_id).strip()])
        except KeyError as exc:
            raise PlatformRegistryError(f"PLATFORM_NOT_FOUND: {platform_id or '空'}") from exc

    def target(self, target_id: str) -> dict[str, Any]:
        try:
            return dict(self._targets[str(target_id).strip()])
        except KeyError as exc:
            raise PlatformRegistryError(f"TARGET_NOT_FOUND: {target_id or '空'}") from exc

    def targets_for(self, platform_id: str) -> list[dict[str, Any]]:
        self.platform(platform_id)
        return [dict(item) for item in self._targets.values() if item["platform_id"] == platform_id]

    def public_payload(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "platforms": [dict(item) for item in self._platforms.values()],
            "targets": [dict(item) for item in self._targets.values()],
        }
