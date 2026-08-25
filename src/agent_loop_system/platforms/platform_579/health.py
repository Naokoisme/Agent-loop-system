from __future__ import annotations

import os
import shutil
import subprocess
from typing import Any, Callable

from .catalog import Platform579Catalog
from .transport import Platform579TransportConfig


class Platform579HealthProvider:
    """Read-only health inspection. It never sends a watch business action."""

    def __init__(
        self,
        *,
        catalog: Platform579Catalog | None = None,
        config: Platform579TransportConfig | None = None,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    ):
        self.catalog = catalog or Platform579Catalog()
        self.config = config or Platform579TransportConfig.from_env()
        self._runner = runner

    @staticmethod
    def _masked(value: str) -> str:
        if not value:
            return ""
        return value if len(value) <= 4 else f"***{value[-4:]}"

    def inspect(self) -> dict[str, Any]:
        checks: list[dict[str, Any]] = []
        catalog = self.catalog.audit()
        checks.append({
            "key": "catalog",
            "label": "579 冻结用例与绑定",
            "status": "pass" if catalog["ok"] else "error",
            "detail": f"{catalog['case_count']} 条用例，{catalog['runnable_count']} 条 AUTO_READY",
        })
        adb = shutil.which(self.config.adb_path) or (
            self.config.adb_path if os.path.isfile(self.config.adb_path) else ""
        )
        adb_ready = False
        adb_detail = "ADB 未找到"
        if adb:
            argv = [self.config.adb_path]
            if self.config.adb_serial:
                argv += ["-s", self.config.adb_serial]
            argv += ["get-state"]
            try:
                result = self._runner(
                    argv, capture_output=True, text=True, encoding="utf-8",
                    errors="replace", timeout=5,
                )
                adb_ready = result.returncode == 0 and "device" in (result.stdout or "").lower()
                adb_detail = "ADB 设备在线" if adb_ready else "ADB 可执行但目标设备未就绪"
            except Exception as exc:
                adb_detail = f"ADB 只读探测失败: {exc}"
        checks.append({
            "key": "adb",
            "label": "ADB 与 APP Bridge",
            "status": "pass" if adb_ready else "warning",
            "detail": adb_detail,
        })
        try:
            from serial.tools import list_ports
            ports = {item.device.upper() for item in list_ports.comports()}
            com_ready = os.environ.get("PLATFORM_579_COM_PORT", "COM3").upper() in ports
            com_detail = "COM3 已枚举（仅允许只读观察）" if com_ready else "COM3 未枚举"
        except Exception as exc:
            com_ready = False
            com_detail = f"pyserial 不可用: {exc}"
        checks.append({
            "key": "com3",
            "label": "COM3/O1/O2 只读通道",
            "status": "pass" if com_ready else "warning",
            "detail": com_detail,
            "read_only": True,
        })
        checks.append({
            "key": "live_gate",
            "label": "579 实机动作门禁",
            "status": "pass" if self.config.device_actions_enabled else "warning",
            "detail": "实机动作已显式开启" if self.config.device_actions_enabled else "默认关闭，等待受控 Canary 授权",
        })
        ready = bool(
            catalog["ok"] and adb_ready and com_ready
            and self.config.enabled and self.config.device_actions_enabled
            and self.config.app_package
            and (self.config.bridge_action or self.config.bridge_component)
        )
        return {
            "id": "579.o2",
            "project": "579_O2",
            "platform_id": "579",
            "target_id": "579.o2",
            "project_label": "579 O2 真机",
            "execution_target": "hardware",
            "execution_target_label": "真机",
            "status": "ready" if ready else "blocked",
            "readiness_status": "ready" if ready else "blocked",
            "live_actions_enabled": self.config.device_actions_enabled,
            "adb_serial": self._masked(self.config.adb_serial),
            "control_path": "ADB → APP Bridge → BLE",
            "observation_path": "579 手表 → COM3（只读）→ O1/O2",
            "checks": checks,
        }
