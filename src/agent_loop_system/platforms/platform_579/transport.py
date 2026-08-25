from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass
from typing import Any, Callable


def _flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _parse_json(text: str) -> dict[str, Any] | None:
    candidates = [text]
    if "data=" in text:
        value = text.split("data=", 1)[1].strip().strip('"')
        candidates.extend([value, value.replace('\\"', '"')])
    for candidate in candidates:
        try:
            payload = json.loads(candidate)
        except (json.JSONDecodeError, TypeError):
            continue
        return payload if isinstance(payload, dict) else {"value": payload}
    return None


def _write_confirmed(payload: dict[str, Any] | None) -> bool:
    if not payload:
        return False
    if payload.get("ok") is True or payload.get("written") is True:
        return True
    if payload.get("ble_write_ok") is True or payload.get("write_ok") is True:
        return True
    try:
        return int(payload.get("bytes_written") or 0) > 0
    except (TypeError, ValueError):
        return False


def _compact_hex(value: str) -> str:
    return re.sub(r"[^0-9a-fA-F]", "", str(value or "")).upper()


@dataclass(frozen=True)
class Platform579TransportConfig:
    enabled: bool
    device_actions_enabled: bool
    adb_path: str
    adb_serial: str
    app_package: str
    bridge_component: str
    bridge_action: str
    timeout_seconds: float

    @classmethod
    def from_env(cls) -> "Platform579TransportConfig":
        return cls(
            enabled=_flag("PLATFORM_579_ENABLED", False),
            device_actions_enabled=_flag("PLATFORM_579_DEVICE_ACTIONS_ENABLED", False),
            adb_path=os.environ.get("PLATFORM_579_ADB_PATH", "").strip() or "adb",
            adb_serial=os.environ.get("PLATFORM_579_ADB_SERIAL", "").strip(),
            app_package=os.environ.get("PLATFORM_579_APP_PACKAGE", "").strip(),
            bridge_component=os.environ.get("PLATFORM_579_BRIDGE_COMPONENT", "").strip(),
            bridge_action=os.environ.get("PLATFORM_579_BRIDGE_ACTION", "").strip(),
            timeout_seconds=float(os.environ.get("PLATFORM_579_ADB_TIMEOUT", "12") or 12),
        )

    def blockers(self) -> list[str]:
        blockers: list[str] = []
        if not self.enabled:
            blockers.append("PLATFORM_579_ENABLED=false")
        if not self.device_actions_enabled:
            blockers.append("PLATFORM_579_DEVICE_ACTIONS_ENABLED=false")
        if not self.app_package:
            blockers.append("PLATFORM_579_APP_PACKAGE 未配置")
        if not (self.bridge_action or self.bridge_component):
            blockers.append("PLATFORM_579_BRIDGE_ACTION/COMPONENT 未配置")
        return blockers


class AppBleTransport:
    """579 control path. This class has no BLE client and no serial handle."""

    def __init__(
        self,
        config: Platform579TransportConfig | None = None,
        *,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    ):
        self.config = config or Platform579TransportConfig.from_env()
        self._runner = runner

    def _prefix(self) -> list[str]:
        result = [self.config.adb_path]
        if self.config.adb_serial:
            result += ["-s", self.config.adb_serial]
        return result

    def _broadcast_args(self) -> list[str]:
        result = ["shell", "am", "broadcast", "--receiver-foreground"]
        if self.config.bridge_component:
            result += ["-n", self.config.bridge_component]
        if self.config.bridge_action:
            result += ["-a", self.config.bridge_action]
        if self.config.app_package:
            result += ["-p", self.config.app_package]
        return result

    def raw_command(
        self,
        cmd_id: int,
        key_id: int,
        data_hex: str,
        *,
        logical_action: str,
    ) -> dict[str, Any]:
        blockers = self.config.blockers()
        if blockers:
            return {
                "ok": False,
                "supported": False,
                "delivery_status": "BLOCKED",
                "reason": "; ".join(blockers),
                "device_action_count": 0,
            }
        wire = _compact_hex(data_hex)
        argv = self._prefix() + self._broadcast_args() + [
            "--es", "action", "raw_command",
            "--ei", "cmd_id", str(int(cmd_id)),
            "--ei", "key_id", str(int(key_id)),
            "--ei", "cmdId", str(int(cmd_id)),
            "--ei", "keyId", str(int(key_id)),
        ]
        if wire:
            argv += [
                "--es", "data_hex", wire,
                "--es", "dataHex", wire,
                "--es", "data", wire,
            ]
        try:
            completed = self._runner(
                argv,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.config.timeout_seconds,
            )
            raw = (completed.stdout or "") + (completed.stderr or "")
            adb_ok = completed.returncode == 0
        except Exception as exc:
            return {
                "ok": False,
                "supported": True,
                "delivery_status": "FAILED",
                "reason": f"ADB 执行异常: {exc}",
                "device_action_count": 1,
            }
        parsed = _parse_json(raw)
        confirmed = _write_confirmed(parsed)
        unconfirmed = bool(adb_ok and not confirmed and not (parsed and parsed.get("ok") is False))
        return {
            "ok": confirmed,
            "supported": True,
            "action": logical_action,
            "delivery_status": "CONFIRMED" if confirmed else "UNCONFIRMED" if unconfirmed else "FAILED",
            "delivery_unconfirmed": unconfirmed,
            "adb_delivered": adb_ok,
            "reason": None if confirmed else "APP Bridge 未返回可验证的 BLE 写入回执" if adb_ok else "ADB 广播失败",
            "parsed": parsed,
            "raw_response": raw[-4000:],
            "device_action_count": 1,
        }

    @staticmethod
    def _notification_data(title: str, body: str) -> str:
        title_bytes = title.encode("utf-8")
        body_bytes = body.encode("utf-8")
        value = bytes([len(title_bytes)]) + title_bytes + len(body_bytes).to_bytes(2, "big") + body_bytes
        return " ".join(f"{byte:02X}" for byte in value)

    def trigger_screenshot(self) -> dict[str, Any]:
        return self.raw_command(
            0x04,
            0x05,
            self._notification_data("TOP5STEP", "TOP5STEP:SCREEN_SHOT_PRINT:1;"),
            logical_action="trigger_screenshot",
        )
