"""Read-only readiness probe for the 6202_W5230 hardware data path.

The probe deliberately stops before state preparation.  Its only firmware
command is one sequence-matched ``GUI_PING``; it never reboots the watch,
starts/stops a test session, changes a page, presses a button, toggles USB, or
captures a screenshot.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Iterable, Mapping
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from agent_loop_system.tools.hardware_serial_ports import get_serial_ports_status
from agent_loop_system.tools.mtp_screenshot import (
    WindowsMtpSystem,
    summarize_usb_devices,
)


HARDWARE_PREFLIGHT_PROJECT = "6202_W5230"
PREFLIGHT_SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class HardwarePreflightCheck:
    key: str
    label: str
    status: str
    blocking: bool
    code: str | None
    detail: str
    action: str = ""
    diagnostics: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class HardwarePreflightResult:
    project: str
    ready: bool
    readiness_status: str
    checked_at: str | None
    checks: tuple[HardwarePreflightCheck, ...]
    execution_ready: bool | None = None
    observation_ready: bool | None = None

    @property
    def primary_code(self) -> str | None:
        return next(
            (
                check.code
                for check in self.checks
                if check.blocking and check.status == "error" and check.code
            ),
            None,
        )

    @property
    def primary_detail(self) -> str:
        return next(
            (
                check.detail
                for check in self.checks
                if check.blocking and check.status == "error"
            ),
            "真机环境尚未检查" if not self.ready else "真机环境已就绪",
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": PREFLIGHT_SCHEMA_VERSION,
            "project": self.project,
            "ready": self.ready,
            "execution_ready": (
                self.ready if self.execution_ready is None else self.execution_ready
            ),
            "observation_ready": (
                self.ready if self.observation_ready is None else self.observation_ready
            ),
            "readiness_status": self.readiness_status,
            "checked_at": self.checked_at,
            "checks": [check.to_dict() for check in self.checks],
        }


class HardwarePreflightFailed(RuntimeError):
    def __init__(self, result: HardwarePreflightResult) -> None:
        self.result = result
        self.code = result.primary_code or "PREFLIGHT_INTERNAL_ERROR"
        super().__init__(f"{self.code}: {result.primary_detail}")


_CHECKS: dict[str, tuple[str, str]] = {
    "profile": ("真机运行时档案", "重新选择测试项目；如仍失败，联系维护人员"),
    "supercom_pipe": (
        "SuperCom 命名管道",
        "在 SuperCom 中打开一个手表串口后重试，程序会自动识别端口",
    ),
    "usb_pnp": ("Windows USB/PnP", "重新插拔 USB，确认电脑能够识别手表后重试"),
    "mtp_namespace": ("Windows MTP 命名空间", "重新连接 USB，并确认电脑能够打开手表存储后重试"),
    "gui_ping": ("UART/GUI 数据面", "确认 SuperCom 连接的是当前手表，并唤醒手表屏幕后重试"),
    "llm": ("大模型服务", "检查网络和判定服务设置后重试"),
}
_ORDER = tuple(_CHECKS)


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _check(
    key: str,
    status: str,
    *,
    code: str | None = None,
    detail: str,
    action: str | None = None,
    diagnostics: Mapping[str, Any] | None = None,
) -> HardwarePreflightCheck:
    label, default_action = _CHECKS[key]
    return HardwarePreflightCheck(
        key=key,
        label=label,
        status=status,
        blocking=True,
        code=code,
        detail=detail,
        action=(action or default_action) if status == "error" else "",
        diagnostics=dict(diagnostics or {}),
    )


def _finish(
    checks: list[HardwarePreflightCheck],
    *,
    project: str,
    checked_at: str | None,
    readiness_status: str | None = None,
) -> HardwarePreflightResult:
    present = {check.key for check in checks}
    for key in _ORDER:
        if key not in present:
            checks.append(
                _check(
                    key,
                    "unchecked",
                    detail="前置门禁未通过，本项未执行",
                )
            )
    ready = all(check.status == "pass" for check in checks)
    if readiness_status is None:
        code = next((check.code for check in checks if check.status == "error"), None)
        readiness_status = (
            "ready"
            if ready
            else "blocked"
            if code in {"TARGET_BUSY", "PREFLIGHT_INTERNAL_ERROR"}
            else "needs_user"
        )
    return HardwarePreflightResult(
        project=project,
        ready=ready,
        readiness_status=readiness_status,
        checked_at=checked_at,
        checks=tuple(checks),
    )


def unchecked_hardware_preflight(
    project: str = HARDWARE_PREFLIGHT_PROJECT,
) -> HardwarePreflightResult:
    return _finish(
        [],
        project=project,
        checked_at=None,
        readiness_status="unchecked",
    )


def target_busy_preflight(
    *,
    project: str = HARDWARE_PREFLIGHT_PROJECT,
    job_id: str | None = None,
) -> HardwarePreflightResult:
    detail = "真机目标正被测试任务占用，环境检查未执行"
    if job_id:
        detail += f"（任务 {job_id}）"
    return _finish(
        [
            HardwarePreflightCheck(
                key="target_busy",
                label="真机目标占用",
                status="error",
                blocking=True,
                code="TARGET_BUSY",
                detail=detail,
                action="等待该任务结束或停止后再检查",
            )
        ],
        project=project,
        checked_at=_now(),
        readiness_status="blocked",
    )


def internal_error_preflight(
    error: BaseException | str,
    *,
    project: str = HARDWARE_PREFLIGHT_PROJECT,
) -> HardwarePreflightResult:
    return _finish(
        [
            HardwarePreflightCheck(
                key="internal",
                label="环境探测服务",
                status="error",
                blocking=True,
                code="PREFLIGHT_INTERNAL_ERROR",
                detail=f"环境探测内部异常: {error}",
                action="重新检查；如再次出现，展开诊断信息并联系维护人员",
            )
        ],
        project=project,
        checked_at=_now(),
        readiness_status="blocked",
    )


def persist_hardware_preflight(
    result: HardwarePreflightResult,
    path: str | os.PathLike[str],
) -> Path:
    target = Path(path).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(
        json.dumps(result.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(temporary, target)
    return target


def load_cached_hardware_preflight(
    path: str | os.PathLike[str],
    *,
    project: str = HARDWARE_PREFLIGHT_PROJECT,
) -> HardwarePreflightResult:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        raw_checks = payload["checks"]
        checks = tuple(
            HardwarePreflightCheck(
                key=str(item["key"]),
                label=str(item.get("label") or _CHECKS.get(str(item["key"]), (str(item["key"]), ""))[0]),
                status=str(item["status"]),
                blocking=bool(item.get("blocking", True)),
                code=str(item["code"]) if item.get("code") else None,
                detail=str(item.get("detail") or ""),
                action=str(item.get("action") or ""),
                diagnostics=dict(item.get("diagnostics") or {}),
            )
            for item in raw_checks
            if isinstance(item, dict)
        )
        return HardwarePreflightResult(
            project=str(payload.get("project") or project),
            ready=bool(payload.get("ready")),
            readiness_status=str(payload.get("readiness_status") or "unchecked"),
            checked_at=(str(payload["checked_at"]) if payload.get("checked_at") else None),
            checks=checks,
            execution_ready=(
                bool(payload["execution_ready"])
                if "execution_ready" in payload
                else None
            ),
            observation_ready=(
                bool(payload["observation_ready"])
                if "observation_ready" in payload
                else None
            ),
        )
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return unchecked_hardware_preflight(project)


def _probe_gui_data_plane(
    *,
    evidence_dir: str | os.PathLike[str],
    settings: Mapping[str, str],
    serial_session_factory: Callable[..., Any] | None,
    gui_timeout: float,
) -> HardwarePreflightCheck:
    """Probe the read-only UART data plane and always close its session."""

    session: Any | None = None
    try:
        if serial_session_factory is None:
            from agent_loop_system.tools.real_device import (
                _create_hardware_serial_session,
                _positive_handshake_sequence,
            )

            session_factory = _create_hardware_serial_session
            sequence_factory = _positive_handshake_sequence
        else:
            session_factory = serial_session_factory
            from agent_loop_system.tools.real_device import _positive_handshake_sequence

            sequence_factory = _positive_handshake_sequence
        session = session_factory(
            evidence_dir=evidence_dir,
            cmd_timeout=gui_timeout,
            environment=settings,
            allow_dangerous_commands=False,
        )
        session.start()
        sequence = sequence_factory()
        response = session.send(
            f":GUI_PING:{sequence}",
            request="gui_ping",
            seq=sequence,
            timeout=gui_timeout,
            expected_type="gui_ack",
            expected_status="processed",
        )
        event_type = str(response.raw.get("type") or "").lower()
        status = str(response.status or "").lower()
        if event_type != "gui_ack" or status != "processed":
            raise RuntimeError(
                f"GUI_PING returned type={event_type!r}, status={status!r}"
            )
        return _check(
            "gui_ping",
            "pass",
            detail="GUI_PING 收到匹配序号的 gui_ack/processed",
            diagnostics={"event_type": event_type, "status": status},
        )
    except Exception as exc:
        return _check(
            "gui_ping",
            "error",
            code="SUPERCOM_NO_UART",
            detail=f"SuperCom 管道存在，但 UART/GUI 无有效回包: {exc}",
        )
    finally:
        if session is not None:
            try:
                session.stop()
            except Exception:
                pass


def run_hardware_preflight(
    *,
    project: str = HARDWARE_PREFLIGHT_PROJECT,
    evidence_dir: str | os.PathLike[str],
    environment: Mapping[str, str] | None = None,
    profile_loader: Callable[..., Any] | None = None,
    serial_ports_probe: Callable[..., dict[str, Any]] | None = None,
    mtp_system: Any | None = None,
    serial_session_factory: Callable[..., Any] | None = None,
    llm_probe: Callable[..., dict[str, Any]] | None = None,
    llm_scopes: Iterable[str] = (),
    gui_timeout: float = 8.0,
    llm_timeout: float = 20.0,
) -> HardwarePreflightResult:
    """Execute a fresh, fail-closed hardware readiness probe."""

    settings = dict(os.environ if environment is None else environment)
    checks: list[HardwarePreflightCheck] = []
    checked_at = _now()
    profile_ready = False

    try:
        from agent_loop_system.tools.hardware_runtime_profile import (
            load_hardware_runtime_profile,
        )

        loader = profile_loader or load_hardware_runtime_profile
        transport = str(settings.get("W30_HARDWARE_TRANSPORT", "")).strip().lower()
        capture_provider = str(
            settings.get("W30_HARDWARE_CAPTURE_PROVIDER", "mtp")
        ).strip().lower()
        if transport != "supercom":
            raise ValueError("W30_HARDWARE_TRANSPORT must be supercom")
        if capture_provider != "mtp":
            raise ValueError("W30_HARDWARE_CAPTURE_PROVIDER must be mtp")
        profile = loader(
            project=project,
            profiles_root=settings.get("W30_HARDWARE_PROFILE_ROOT") or None,
            version=settings.get("W30_HARDWARE_PROFILE_VERSION") or None,
        )
        checks.append(
            _check(
                "profile",
                "pass",
                detail=f"档案 {profile.version} · 固件 {profile.firmware_version} · {profile.project}",
                diagnostics={
                    "profile_version": str(profile.version),
                    "firmware_version": str(profile.firmware_version),
                    "project": str(profile.project),
                    "transport": transport,
                    "capture_provider": capture_provider,
                },
            )
        )
        profile_ready = True
    except Exception as exc:
        checks.append(
            _check(
                "profile",
                "error",
                code="PROFILE_INVALID",
                detail=f"真机运行时档案或传输配置不可用: {exc}",
            )
        )

    configured_port = str(settings.get("W30_HARDWARE_PORT", "")).strip().upper()
    pipe_ready = False
    try:
        ports = (serial_ports_probe or get_serial_ports_status)(configured_port)
        items = [item for item in ports.get("items", []) if isinstance(item, dict)]
        active_ports = [
            str(item.get("port") or "").upper()
            for item in items
            if item.get("present") and item.get("supercom_open")
        ]
        selected_port = str(ports.get("selected_port") or "").strip().upper()
        if len(active_ports) == 1:
            selected_port = active_ports[0]
        elif configured_port in active_ports:
            selected_port = configured_port
        if not selected_port:
            if len(active_ports) > 1:
                raise RuntimeError(
                    "检测到多个已开启的 SuperCom 手表连接，无法自动确定执行目标: "
                    + "、".join(active_ports)
                )
            raise RuntimeError("未检测到已开启的 SuperCom 手表连接")
        selected = next(
            (
                item
                for item in items
                if str(item.get("port") or "").upper() == selected_port
            ),
            None,
        )
        if not selected or not selected.get("present") or not selected.get("supercom_open"):
            raise RuntimeError(f"{selected_port} 对应的 SuperCom 管道不存在或不可用")
        auto_selected = selected_port != configured_port
        settings["W30_HARDWARE_PORT"] = selected_port
        settings["W30_HARDWARE_PORT_SOURCE"] = (
            "auto_discovered" if auto_selected else "configured"
        )
        checks.append(
            _check(
                "supercom_pipe",
                "pass",
                detail=(
                    f"自动发现 {selected_port}，SuperCom 管道已存在"
                    if auto_selected
                    else f"{selected_port} 对应的 SuperCom 管道已存在"
                ),
                diagnostics={
                    "configured_port": configured_port,
                    "selected_port": selected_port,
                    "selection_source": (
                        "auto_discovered" if auto_selected else "configured"
                    ),
                    "active_ports": active_ports,
                    "active_count": len(active_ports),
                    "pipe_path": str(selected.get("pipe_path") or ""),
                    "ports": [
                        {
                            "port": str(item.get("port") or ""),
                            "friendly_name": str(item.get("friendly_name") or ""),
                            "present": bool(item.get("present")),
                            "supercom_open": bool(item.get("supercom_open")),
                        }
                        for item in items
                    ],
                },
            )
        )
        pipe_ready = True
    except Exception as exc:
        checks.append(
            _check(
                "supercom_pipe",
                "error",
                code="SUPERCOM_PIPE_UNAVAILABLE",
                detail=str(exc),
                diagnostics={
                    "configured_port": configured_port,
                    "active_ports": active_ports if "active_ports" in locals() else [],
                    "ports": [
                        {
                            "port": str(item.get("port") or ""),
                            "friendly_name": str(item.get("friendly_name") or ""),
                            "present": bool(item.get("present")),
                            "supercom_open": bool(item.get("supercom_open")),
                        }
                        for item in (items if "items" in locals() else [])
                    ],
                },
            )
        )

    system = mtp_system or WindowsMtpSystem()
    try:
        devices = list(system.inspect_usb_devices(timeout=5.0))
        online_devices = [
            item
            for item in devices
            if str(item.get("status") or item.get("Status") or "").casefold()
            == "ok"
        ]
        diagnostics = {
            "count": len(devices),
            "online_count": len(online_devices),
            "devices": summarize_usb_devices(devices),
        }
        if not devices:
            checks.append(
                _check(
                    "usb_pnp",
                    "error",
                    code="USB_DEVICE_NOT_PRESENT",
                    detail="Windows 当前未枚举到 VID_301A&PID_6808",
                    diagnostics=diagnostics,
                )
            )
        elif len(devices) != 1:
            checks.append(
                _check(
                    "usb_pnp",
                    "error",
                    code="USB_TARGET_AMBIGUOUS",
                    detail=f"Windows 当前枚举到 {len(devices)} 个目标 USB 实例",
                    diagnostics=diagnostics,
                )
            )
        elif len(online_devices) != 1:
            checks.append(
                _check(
                    "usb_pnp",
                    "error",
                    code="USB_DEVICE_NOT_PRESENT",
                    detail="Windows 已发现目标 USB 实例，但设备状态不是 OK",
                    diagnostics=diagnostics,
                )
            )
        else:
            checks.append(
                _check(
                    "usb_pnp",
                    "pass",
                    detail="Windows 当前恰好枚举到一个目标 USB 实例",
                    diagnostics=diagnostics,
                )
            )
    except Exception as exc:
        checks.append(
            HardwarePreflightCheck(
                key="usb_pnp",
                label="Windows USB/PnP",
                status="error",
                blocking=True,
                code="PREFLIGHT_INTERNAL_ERROR",
                detail=f"Windows USB/PnP 探测失败: {exc}",
                action="保留 preflight.json 与日志并联系平台维护人员",
            )
        )

    try:
        namespace = dict(system.probe_namespace(timeout=10.0))
        namespace_device = str(namespace.get("device") or "")[:100]
        namespace_storage = str(namespace.get("storage") or "")[:100]
        namespace_folder = str(namespace.get("folder") or "")[:100]
        checks.append(
            _check(
                "mtp_namespace",
                "pass",
                detail=(
                    f"{namespace_device or 'ZORA'} → "
                    f"{namespace_storage or '存储卷'} → "
                    f"{namespace_folder or 'download'} 命名空间可浏览"
                ),
                diagnostics={
                    "device": namespace_device,
                    "storage": namespace_storage,
                    "folder": namespace_folder,
                },
            )
        )
    except Exception as exc:
        checks.append(
            _check(
                "mtp_namespace",
                "error",
                code="MTP_NAMESPACE_NOT_READY",
                detail=f"Windows MTP 命名空间不可浏览: {exc}",
            )
        )

    if profile_ready and pipe_ready:
        checks.append(
            _probe_gui_data_plane(
                evidence_dir=evidence_dir,
                settings=settings,
                serial_session_factory=serial_session_factory,
                gui_timeout=gui_timeout,
            )
        )
    else:
        dependencies = []
        if not profile_ready:
            dependencies.append("运行时档案")
        if not pipe_ready:
            dependencies.append("SuperCom 连接")
        checks.append(
            _check(
                "gui_ping",
                "unchecked",
                detail="、".join(dependencies) + "不可用，未发送 GUI_PING",
            )
        )

    try:
        llm_results: list[tuple[str, dict[str, Any]]] = []
        if llm_probe is not None:
            llm_results.append(("current", dict(llm_probe(timeout=llm_timeout))))
        else:
            from agent_loop_system.tools.llm_config import test_llm_connectivity
            from agent_loop_system.tools.llm_config import llm_api_key_scope

            scopes = tuple(dict.fromkeys(str(scope) for scope in llm_scopes))
            if scopes:
                for scope in scopes:
                    with llm_api_key_scope(scope):
                        llm_results.append(
                            (scope, dict(test_llm_connectivity(timeout=llm_timeout)))
                        )
            else:
                llm_results.append(
                    ("current", dict(test_llm_connectivity(timeout=llm_timeout)))
                )
        failures = [
            (scope, result)
            for scope, result in llm_results
            if not result.get("ok")
        ]
        if failures:
            scope, failure = failures[0]
            message = str(
                failure.get("message") or failure.get("error") or "未知错误"
            )
            raise RuntimeError(f"{scope} scope: {message}")
        llm = llm_results[0][1]
        checks.append(
            _check(
                "llm",
                "pass",
                detail=(
                    f"{len(llm_results)} 个模型作用域连通"
                    if len(llm_results) > 1
                    else f"模型 {llm.get('model') or 'unknown'} 连通"
                    f"（{llm.get('latency_ms', '?')}ms）"
                ),
                diagnostics={
                    "model": str(llm.get("model") or ""),
                    "base_url": str(llm.get("base_url") or ""),
                    "latency_ms": llm.get("latency_ms"),
                    "scopes": [
                        {
                            "scope": scope,
                            "model": str(result.get("model") or ""),
                            "base_url": str(result.get("base_url") or ""),
                            "latency_ms": result.get("latency_ms"),
                        }
                        for scope, result in llm_results
                    ],
                },
            )
        )
    except Exception as exc:
        checks.append(
            _check(
                "llm",
                "error",
                code="LLM_NOT_READY",
                detail=f"大模型服务不可用: {exc}",
                diagnostics={
                    "scopes": [
                        {
                            "scope": scope,
                            "ok": bool(result.get("ok")),
                            "model": str(result.get("model") or ""),
                            "base_url": str(result.get("base_url") or ""),
                            "latency_ms": result.get("latency_ms"),
                            "error_type": str(result.get("error_type") or ""),
                            "error_category": str(
                                result.get("error_category") or ""
                            ),
                        }
                        for scope, result in llm_results
                    ],
                },
            )
        )
        return _finish(checks, project=project, checked_at=checked_at)

    return _finish(checks, project=project, checked_at=checked_at)


def require_hardware_preflight(
    *,
    persist_paths: Iterable[str | os.PathLike[str]] = (),
    **kwargs: Any,
) -> HardwarePreflightResult:
    result = run_hardware_preflight(**kwargs)
    for path in persist_paths:
        persist_hardware_preflight(result, path)
    if not result.ready:
        raise HardwarePreflightFailed(result)
    return result


__all__ = [
    "HARDWARE_PREFLIGHT_PROJECT",
    "HardwarePreflightCheck",
    "HardwarePreflightFailed",
    "HardwarePreflightResult",
    "load_cached_hardware_preflight",
    "internal_error_preflight",
    "persist_hardware_preflight",
    "require_hardware_preflight",
    "run_hardware_preflight",
    "target_busy_preflight",
    "unchecked_hardware_preflight",
]
