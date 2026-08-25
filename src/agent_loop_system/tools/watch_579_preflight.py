"""Execution-only preflight for the 579 PC-BLE target."""

from __future__ import annotations

from datetime import datetime
import os
from typing import Any, Mapping

from agent_loop_system.tools.hardware_preflight import (
    HardwarePreflightCheck,
    HardwarePreflightResult,
)
from agent_loop_system.tools.watch_579_ble import Watch579BleBroker, Watch579BleError
from agent_loop_system.tools.watch_579_protocol import (
    WATCH_579_NOTIFY_UUID,
    WATCH_579_SERVICE_UUID,
    WATCH_579_WRITE_UUID,
)


WATCH_579_PROJECT = "579_Z1640"


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _check(
    key: str,
    label: str,
    status: str,
    *,
    blocking: bool,
    code: str | None,
    detail: str,
    action: str = "",
    diagnostics: dict[str, Any] | None = None,
) -> HardwarePreflightCheck:
    return HardwarePreflightCheck(
        key=key,
        label=label,
        status=status,
        blocking=blocking,
        code=code,
        detail=detail,
        action=action if status != "pass" else "",
        diagnostics=dict(diagnostics or {}),
    )


def run_watch_579_preflight(
    *,
    project: str = WATCH_579_PROJECT,
    evidence_dir: object | None = None,
    environment: Mapping[str, str] | None = None,
    broker: Watch579BleBroker,
    lease_token: str | None = None,
    connect_timeout: float = 15.0,
    **_unused: Any,
) -> HardwarePreflightResult:
    """Check only the execution path required by 579.

    ``evidence_dir`` is accepted for parity with the existing hardware gate but
    is deliberately unused: 579 currently has no screenshot transport.
    """

    del evidence_dir
    settings = os.environ if environment is None else environment
    checked_at = _now()
    checks: list[HardwarePreflightCheck] = []
    address = str(settings.get("WATCH_579_BLE_ADDRESS", "")).strip()
    dependency_check = getattr(broker, "check_dependencies", None)
    try:
        if callable(dependency_check):
            dependency_check()
    except Watch579BleError as exc:
        checks.append(
            _check(
                "bleak",
                "Windows BLE/Bleak",
                "error",
                blocking=True,
                code=exc.reason_code,
                detail=str(exc),
                action="安装或修复当前 Agent-loop 环境中的 Windows BLE 依赖后重试",
            )
        )
        return HardwarePreflightResult(
            project=project,
            ready=False,
            readiness_status="blocked",
            checked_at=checked_at,
            checks=tuple(checks),
            execution_ready=False,
            observation_ready=False,
        )
    checks.append(
        _check(
            "bleak",
            "Windows BLE/Bleak",
            "pass",
            blocking=True,
            code=None,
            detail="579 BLE Broker 已加载",
        )
    )
    if not address:
        checks.append(
            _check(
                "address",
                "579 精确设备地址",
                "error",
                blocking=True,
                code="BLE_DEVICE_NOT_FOUND",
                detail="WATCH_579_BLE_ADDRESS 尚未配置",
                action="前往蓝牙工作台扫描并选择当前 579 手表",
            )
        )
        return HardwarePreflightResult(
            project=project,
            ready=False,
            readiness_status="needs_user",
            checked_at=checked_at,
            checks=tuple(checks),
            execution_ready=False,
            observation_ready=False,
        )
    checks.append(
        _check(
            "address",
            "579 精确设备地址",
            "pass",
            blocking=True,
            code=None,
            detail=f"已配置 {address}",
            diagnostics={"address": address},
        )
    )

    try:
        if lease_token:
            status = broker.connect_internal(
                lease_token=lease_token,
                address=address,
                timeout=connect_timeout,
            )
        else:
            status = broker.connect(address=address, timeout=connect_timeout)
    except Watch579BleError as exc:
        checks.append(
            _check(
                "connection",
                "精确扫描与 GATT 连接",
                "error",
                blocking=True,
                code=exc.reason_code,
                detail=str(exc),
                action=(
                    "关闭手机蓝牙和 ble_test 等占用方后重试；平台不会自动抢占连接"
                    if exc.reason_code in {
                        "BLE_CONNECT_TIMEOUT",
                        "BLE_CONNECT_FAILED",
                        "BLE_DEVICE_NOT_FOUND",
                    }
                    else "检查目标地址和手表固件 GATT 服务后重试"
                ),
                diagnostics={"address": address},
            )
        )
        return HardwarePreflightResult(
            project=project,
            ready=False,
            readiness_status="needs_user",
            checked_at=checked_at,
            checks=tuple(checks),
            execution_ready=False,
            observation_ready=False,
        )
    except Exception as exc:
        checks.append(
            _check(
                "connection",
                "精确扫描与 GATT 连接",
                "error",
                blocking=True,
                code="PREFLIGHT_INTERNAL_ERROR",
                detail=f"579 BLE 环境检查异常: {exc}",
                action="重新检查；如再次出现，展开诊断信息并联系维护人员",
            )
        )
        return HardwarePreflightResult(
            project=project,
            ready=False,
            readiness_status="blocked",
            checked_at=checked_at,
            checks=tuple(checks),
            execution_ready=False,
            observation_ready=False,
        )

    checks.extend(
        (
            _check(
                "connection",
                "精确扫描与 GATT 连接",
                "pass",
                blocking=True,
                code=None,
                detail=f"已连接 {status.get('address') or address}",
                diagnostics={
                    "address": status.get("address"),
                    "name": status.get("name"),
                    "rssi": status.get("rssi"),
                },
            ),
            _check(
                "gatt_profile",
                "579 GATT 服务与特征",
                "pass",
                blocking=True,
                code=None,
                detail="Service/Write/Notify UUID 均已发现",
                diagnostics={
                    "service_uuid": WATCH_579_SERVICE_UUID,
                    "write_uuid": WATCH_579_WRITE_UUID,
                    "notify_uuid": WATCH_579_NOTIFY_UUID,
                },
            ),
            _check(
                "notify",
                "579 Notify 订阅",
                "pass",
                blocking=True,
                code=None,
                detail="Notify 已订阅，允许开始写入",
            ),
            _check(
                "observation",
                "579 屏幕观察",
                "warning",
                blocking=False,
                code="OBSERVATION_UNAVAILABLE",
                detail="当前固件没有可用的 579 BLE 截图通道",
                action="本阶段仅记录动作与 L1 ACK，结果固定为无法视觉验证",
            ),
        )
    )
    return HardwarePreflightResult(
        project=project,
        ready=True,
        readiness_status="ready",
        checked_at=checked_at,
        checks=tuple(checks),
        execution_ready=True,
        observation_ready=False,
    )


__all__ = ["WATCH_579_PROJECT", "run_watch_579_preflight"]
