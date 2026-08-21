"""Standalone real-watch probe for the 6202 PB-over-BLE path.

The probe deliberately stays outside case maps and Runner integration.  It
uses BLE for both requests and the existing SuperCom pipe only as an observer
for the engineering ``GUI_PING`` completion event.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

from google.protobuf.json_format import MessageToDict

from agent_loop_system.protocol import pb_config_pb2, pb_notice_pb2
from agent_loop_system.protocol.pb_commands import (
    ConfigKey,
    NoticeKey,
    PbCommandGroup,
    SettingKey,
    command_id,
)
from agent_loop_system.tools.case_map import CaseEntry, CaseRunResult
from agent_loop_system.tools.hardware_serial import (
    DEFAULT_PORT,
    HardwareSerialSession,
    SuperComPipeTransport,
)
from agent_loop_system.tools.mtp_screenshot import MtpCaptureProvider
from agent_loop_system.tools.watch_ble import (
    WatchBleClient,
    WatchBleDevice,
    WatchBleDiscoveryError,
    scan_watches,
    select_watch,
)


QUICK_TEST_COMMAND = command_id(PbCommandGroup.NOTICE, NoticeKey.QUICK_TEST)
GET_DEVICE_INFO_COMMAND = command_id(
    PbCommandGroup.SETTING,
    ConfigKey.GET_DEVICE_INFO,
)
GET_GOAL_CONFIG_COMMAND = command_id(
    PbCommandGroup.SETTING,
    ConfigKey.GET_GOAL_CONFIG,
)
SET_GOAL_CONFIG_COMMAND = command_id(
    PbCommandGroup.SETTING,
    ConfigKey.SET_GOAL_CONFIG,
)
GET_DND_CONFIG_COMMAND = command_id(
    PbCommandGroup.SETTING,
    ConfigKey.GET_DND_CONFIG,
)
SET_DND_CONFIG_COMMAND = command_id(
    PbCommandGroup.SETTING,
    ConfigKey.SET_DND_CONFIG,
)
FIND_DEVICE_COMMAND = command_id(
    PbCommandGroup.SETTING,
    SettingKey.FIND_DEVICE,
)
STOP_FIND_DEVICE_COMMAND = command_id(
    PbCommandGroup.SETTING,
    SettingKey.STOP_FIND_DEVICE,
)
_MAX_GUI_SEQUENCE = 2_147_483_647
_SET_164_VERIFICATION_POINT = "手表显示查找设备提醒"
_ADV_STOP_SETTLE_SECONDS = 2.0
_ADV_START_SETTLE_SECONDS = 4.0


def _positive_gui_sequence() -> int:
    return time.time_ns() % _MAX_GUI_SEQUENCE or 1


def _error_payload(exc: Exception) -> dict[str, object]:
    payload: dict[str, object] = {
        "type": type(exc).__name__,
        "message": str(exc),
    }
    code = getattr(exc, "code", None)
    if isinstance(code, str):
        payload["code"] = code
    return payload


def _default_evidence_dir() -> Path:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    return Path("artifacts") / "watch_ble_probe" / stamp


def _ensure_uart_observer(
    serial: HardwareSerialSession,
    args: argparse.Namespace,
    result: dict[str, object],
) -> None:
    if serial.started:
        return
    serial.start()
    result["uart_observer"] = {
        "ok": True,
        "transport": "supercom_pipe",
        "port": args.serial_port,
    }


async def _discover_watch(
    args: argparse.Namespace,
    result: dict[str, object],
    serial: HardwareSerialSession,
) -> WatchBleDevice:
    async def scan(attempt: int) -> list[WatchBleDevice]:
        devices = await scan_watches(
            timeout=args.scan_timeout,
            address=args.address,
        )
        result["scan"] = {
            "ok": True,
            "attempts": attempt,
            "devices": [asdict(device) for device in devices],
        }
        return devices

    devices = await scan(1)
    try:
        return select_watch(devices, address=args.address, name=args.name)
    except WatchBleDiscoveryError as exc:
        if not args.address:
            raise
        initial_error_message = str(exc)

    recovery_commands: list[str] = []
    recovery: dict[str, object] = {
        "attempted": True,
        "ok": False,
        "reason": initial_error_message,
        "commands": recovery_commands,
    }
    result["ble_advertising_recovery"] = recovery
    _ensure_uart_observer(serial, args, result)

    serial.write_shell_line("btm le adv_start")
    recovery_commands.append("btm le adv_start")
    await asyncio.sleep(_ADV_START_SETTLE_SECONDS)
    devices = await scan(2)
    try:
        device = select_watch(devices, address=args.address, name=args.name)
    except WatchBleDiscoveryError as start_error:
        recovery["adv_start_error"] = _error_payload(start_error)
    else:
        recovery.update(
            {
                "ok": True,
                "strategy": "adv_start",
                "verified_by": "address_rescan",
                "device": asdict(device),
            }
        )
        return device

    serial.write_shell_line("btm le adv_stop")
    recovery_commands.append("btm le adv_stop")
    await asyncio.sleep(_ADV_STOP_SETTLE_SECONDS)
    serial.write_shell_line("btm le adv_start")
    recovery_commands.append("btm le adv_start")
    await asyncio.sleep(_ADV_START_SETTLE_SECONDS)

    devices = await scan(3)
    try:
        device = select_watch(devices, address=args.address, name=args.name)
    except WatchBleDiscoveryError as recovery_error:
        recovery["error"] = _error_payload(recovery_error)
        raise
    recovery.update(
        {
            "ok": True,
            "strategy": "adv_restart",
            "verified_by": "address_rescan",
            "device": asdict(device),
        }
    )
    return device


async def _wait_for_uart_event(
    session: HardwareSerialSession,
    **filters: Any,
) -> dict:
    return await asyncio.to_thread(session.wait_for_event, **filters)


def _message_dict(message: Any) -> dict[str, object]:
    return MessageToDict(message, preserving_proto_field_name=True)


def _without_steps(message: Any) -> bytes:
    comparable = pb_config_pb2._GoalConfig()
    comparable.CopyFrom(message)
    comparable.ClearField("steps")
    return comparable.SerializeToString(deterministic=True)


def _alternate_step_target(current: int) -> int:
    if current <= 0:
        raise RuntimeError(
            "current step goal is not a positive value and cannot be restored "
            "through the production SET_GOAL_CONFIG handler"
        )
    return 8_000 if current == 10_000 else 10_000


async def _send_quick_command(
    client: WatchBleClient,
    serial: HardwareSerialSession,
    command: str,
    *,
    request: str,
    timeout: float,
) -> dict[str, object]:
    start_event_index = serial.event_count
    wire_sequence = await client.send_protobuf(
        QUICK_TEST_COMMAND,
        pb_notice_pb2._QuickCmd(cmd=command),
    )
    accepted = await _wait_for_uart_event(
        serial,
        request=request,
        event_type="command_result",
        status="accepted",
        timeout=timeout,
        start_event_index=start_event_index,
    )
    return {
        "wire_sequence": wire_sequence,
        "quick_command": command,
        "uart_command_result": accepted,
    }


async def _gui_fence(
    client: WatchBleClient,
    serial: HardwareSerialSession,
    *,
    timeout: float,
) -> dict[str, object]:
    gui_sequence = _positive_gui_sequence()
    start_event_index = serial.event_count
    wire_sequence = await client.send_protobuf(
        QUICK_TEST_COMMAND,
        pb_notice_pb2._QuickCmd(
            cmd=f"TOP5STEP:GUI_PING:{gui_sequence};"
        ),
    )
    processed = await _wait_for_uart_event(
        serial,
        request="gui_ping",
        seq=gui_sequence,
        event_type="gui_ack",
        status="processed",
        timeout=timeout,
        start_event_index=start_event_index,
    )
    return {
        "wire_sequence": wire_sequence,
        "gui_sequence": gui_sequence,
        "uart_gui_ack": processed,
    }


async def _get_goal_config(
    client: WatchBleClient,
    *,
    timeout: float,
) -> Any:
    return await client.exchange_protobuf(
        GET_GOAL_CONFIG_COMMAND,
        None,
        pb_config_pb2._GoalConfig,
        timeout=timeout,
    )


async def _set_goal_config(
    client: WatchBleClient,
    config: Any,
    *,
    timeout: float,
) -> dict[str, object]:
    response = await client.exchange_protobuf(
        SET_GOAL_CONFIG_COMMAND,
        config,
        pb_config_pb2._CommonResponse,
        timeout=timeout,
    )
    if response.result != 0:
        raise RuntimeError(
            f"SET_GOAL_CONFIG returned result={response.result}"
        )
    return _message_dict(response)


async def _get_dnd_config(
    client: WatchBleClient,
    *,
    timeout: float,
) -> Any:
    return await client.exchange_protobuf(
        GET_DND_CONFIG_COMMAND,
        None,
        pb_config_pb2._DndConfig,
        timeout=timeout,
    )


async def _set_dnd_config(
    client: WatchBleClient,
    config: Any,
    *,
    timeout: float,
) -> dict[str, object]:
    response = await client.exchange_protobuf(
        SET_DND_CONFIG_COMMAND,
        config,
        pb_config_pb2._CommonResponse,
        timeout=timeout,
    )
    if response.result != 0:
        raise RuntimeError(
            f"SET_DND_CONFIG returned result={response.result}"
        )
    return _message_dict(response)


async def _run_goal_cycle(
    args: argparse.Namespace,
    result: dict[str, object],
    client: WatchBleClient,
    serial: HardwareSerialSession,
) -> None:
    cycle: dict[str, object] = {
        "ok": False,
        "get_command": f"0x{GET_GOAL_CONFIG_COMMAND:04X}",
        "set_command": f"0x{SET_GOAL_CONFIG_COMMAND:04X}",
        "page": "DATA",
    }
    result["goal_cycle"] = cycle

    original = await _get_goal_config(client, timeout=args.pb_timeout)
    original_dict = _message_dict(original)
    old_steps = int(original.steps)
    new_steps = (
        args.goal_step
        if args.goal_step is not None
        else _alternate_step_target(old_steps)
    )
    if new_steps <= 0:
        raise RuntimeError("the temporary step goal must be positive")
    if new_steps == old_steps:
        raise RuntimeError("the temporary step goal must differ from the old value")

    original_path = Path(args.evidence_dir) / "goal_original.json"
    original_path.write_text(
        json.dumps(original_dict, ensure_ascii=False, indent=2, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )
    cycle.update(
        {
            "old_steps": old_steps,
            "temporary_steps": new_steps,
            "original": original_dict,
            "original_path": str(original_path),
        }
    )

    temporary = pb_config_pb2._GoalConfig()
    temporary.CopyFrom(original)
    temporary.steps = new_steps
    cycle["temporary_request"] = _message_dict(temporary)
    restore_required = False
    try:
        # A response timeout does not prove that the write was rejected.  From
        # this point onward restoration is mandatory even if SET raises.
        restore_required = True
        cycle["set_response"] = await _set_goal_config(
            client,
            temporary,
            timeout=args.pb_timeout,
        )

        changed = await _get_goal_config(client, timeout=args.pb_timeout)
        if changed.steps != new_steps:
            raise RuntimeError(
                f"step goal read-back mismatch: expected {new_steps}, "
                f"got {changed.steps}"
            )
        if _without_steps(changed) != _without_steps(original):
            raise RuntimeError("SET_GOAL_CONFIG changed fields other than steps")
        cycle["changed_readback"] = _message_dict(changed)

        cycle["page_reset"] = await _send_quick_command(
            client,
            serial,
            "TOP5STEP:ENTER_PAGE:DIAL,0;",
            request="enter_page",
            timeout=args.uart_timeout,
        )
        cycle["page_reset_fence"] = await _gui_fence(
            client,
            serial,
            timeout=args.uart_timeout,
        )
        cycle["page_open"] = await _send_quick_command(
            client,
            serial,
            "TOP5STEP:ENTER_PAGE:DATA,0;",
            request="enter_page",
            timeout=args.uart_timeout,
        )
        cycle["page_fence"] = await _gui_fence(
            client,
            serial,
            timeout=args.uart_timeout,
        )
        cycle["target_page_navigation"] = await _send_quick_command(
            client,
            serial,
            "TOP5STEP:QDEC_SET:1;",
            request="qdec_set",
            timeout=args.uart_timeout,
        )
        cycle["target_page_fence"] = await _gui_fence(
            client,
            serial,
            timeout=args.uart_timeout,
        )
        await asyncio.sleep(0.8)

        screenshot_path = (
            args.screenshot.resolve()
            if args.screenshot is not None
            else Path(args.evidence_dir) / "goal_changed.bmp"
        )
        provider = MtpCaptureProvider(serial)
        try:
            frame = await asyncio.to_thread(
                provider.capture,
                timeout=args.capture_timeout,
            )
            await asyncio.to_thread(frame.save_bmp, screenshot_path)
        finally:
            provider.close()
        cycle["screenshot"] = {
            "ok": True,
            "path": str(screenshot_path),
            "metadata": asdict(frame.metadata),
        }
    finally:
        if restore_required:
            try:
                restore_response = await _set_goal_config(
                    client,
                    original,
                    timeout=args.pb_timeout,
                )
                restored = await _get_goal_config(
                    client,
                    timeout=args.pb_timeout,
                )
                if restored != original:
                    raise RuntimeError(
                        "restored GoalConfig does not exactly match the backup"
                    )
                cycle["restore"] = {
                    "ok": True,
                    "transport": "ble_pb",
                    "response": restore_response,
                    "readback": _message_dict(restored),
                }
            except Exception as restore_exc:
                fallback: dict[str, object] = {
                    "ok": False,
                    "transport": "ble_pb",
                    "error": _error_payload(restore_exc),
                }
                try:
                    fallback_result = await asyncio.to_thread(
                        serial.send,
                        f":SET_DAILY_TARGET:1,{old_steps}",
                        request="set_daily_target",
                        timeout=args.uart_timeout,
                        expected_type="command_result",
                        expected_status="accepted",
                    )
                    fallback["uart_fallback"] = {
                        "applied": True,
                        "event": fallback_result.raw,
                    }
                except Exception as fallback_exc:
                    fallback["uart_fallback"] = {
                        "applied": False,
                        "error": _error_payload(fallback_exc),
                    }
                cycle["restore"] = fallback
                raise RuntimeError(
                    "BLE restoration failed; inspect goal_cycle.restore before "
                    "continuing"
                ) from restore_exc

    cycle["ok"] = True


async def _run_find_watch_cycle(
    args: argparse.Namespace,
    result: dict[str, object],
    client: WatchBleClient,
    serial: HardwareSerialSession,
) -> None:
    """Run the reversible watch-side checkpoint from hardware case SET_164."""

    cycle: dict[str, object] = {
        "ok": False,
        "case_id": "SET_164",
        "get_dnd_command": f"0x{GET_DND_CONFIG_COMMAND:04X}",
        "set_dnd_command": f"0x{SET_DND_CONFIG_COMMAND:04X}",
        "find_device_command": f"0x{FIND_DEVICE_COMMAND:04X}",
        "stop_find_device_command": f"0x{STOP_FIND_DEVICE_COMMAND:04X}",
    }
    result["find_watch_cycle"] = cycle

    original = await _get_dnd_config(client, timeout=args.pb_timeout)
    original_path = Path(args.evidence_dir) / "dnd_original.json"
    original_path.write_text(
        json.dumps(
            _message_dict(original),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    cycle.update(
        {
            "original_dnd": _message_dict(original),
            "original_path": str(original_path),
        }
    )

    temporary = pb_config_pb2._DndConfig()
    temporary.CopyFrom(original)
    temporary.isEnabled = True
    temporary.mode = 0  # Firmware value for All Day.
    cycle["temporary_dnd"] = _message_dict(temporary)

    restore_required = False
    find_started = False
    try:
        restore_required = True
        cycle["set_dnd_response"] = await _set_dnd_config(
            client,
            temporary,
            timeout=args.pb_timeout,
        )
        changed = await _get_dnd_config(client, timeout=args.pb_timeout)
        if changed != temporary:
            raise RuntimeError("All Day DND read-back does not match the request")
        cycle["changed_dnd_readback"] = _message_dict(changed)

        find_started = True
        cycle["find_wire_sequence"] = await client.send_protobuf(
            FIND_DEVICE_COMMAND,
        )
        await asyncio.sleep(0.8)
        cycle["find_page_fence"] = await _gui_fence(
            client,
            serial,
            timeout=args.uart_timeout,
        )
        await asyncio.sleep(0.8)

        screenshot_path = (
            args.screenshot.resolve()
            if args.screenshot is not None
            else Path(args.evidence_dir) / "find_watch.bmp"
        )
        provider = MtpCaptureProvider(serial)
        try:
            frame = await asyncio.to_thread(
                provider.capture,
                timeout=args.capture_timeout,
            )
            await asyncio.to_thread(frame.save_bmp, screenshot_path)
        finally:
            provider.close()
        cycle["screenshot"] = {
            "ok": True,
            "path": str(screenshot_path),
            "metadata": asdict(frame.metadata),
        }
    finally:
        if find_started:
            try:
                cycle["stop_find_wire_sequence"] = await client.send_protobuf(
                    STOP_FIND_DEVICE_COMMAND,
                )
                await asyncio.sleep(0.4)
                cycle["stop_page_fence"] = await _gui_fence(
                    client,
                    serial,
                    timeout=args.uart_timeout,
                )
                cycle["stop_find"] = {"ok": True}
            except Exception as stop_exc:
                cycle["stop_find"] = {
                    "ok": False,
                    "error": _error_payload(stop_exc),
                }
        if restore_required:
            try:
                restore_response = await _set_dnd_config(
                    client,
                    original,
                    timeout=args.pb_timeout,
                )
                restored = await _get_dnd_config(
                    client,
                    timeout=args.pb_timeout,
                )
                if restored != original:
                    raise RuntimeError(
                        "restored DndConfig does not exactly match the backup"
                    )
                cycle["restore"] = {
                    "ok": True,
                    "transport": "ble_pb",
                    "response": restore_response,
                    "readback": _message_dict(restored),
                }
            except Exception as restore_exc:
                cycle["restore"] = {
                    "ok": False,
                    "transport": "ble_pb",
                    "error": _error_payload(restore_exc),
                }
                raise RuntimeError(
                    "BLE DND restoration failed; inspect "
                    "find_watch_cycle.restore before continuing"
                ) from restore_exc

    if not cycle.get("stop_find", {}).get("ok"):
        raise RuntimeError("STOP_FIND_DEVICE did not complete cleanly")
    cycle["ok"] = True


async def run_probe(
    args: argparse.Namespace,
    result: dict[str, object],
) -> None:
    serial = HardwareSerialSession(
        port=args.serial_port,
        log_dir=Path(args.evidence_dir) / "serial",
        cmd_timeout=args.uart_timeout,
        transport=SuperComPipeTransport(args.serial_port),
        dtr=False,
        rts=False,
    )
    client: WatchBleClient | None = None

    try:
        device = await _discover_watch(args, result, serial)
        result["device"] = asdict(device)
        _ensure_uart_observer(serial, args, result)
        client = WatchBleClient(device, timeout=args.ble_timeout)

        await client.connect(pair=False)
        result["gatt"] = {
            "ok": True,
            "connected": True,
            "pairing_attempted": False,
        }

        gui_sequence = args.gui_sequence or _positive_gui_sequence()
        quick_command = f"TOP5STEP:GUI_PING:{gui_sequence};"
        start_event_index = serial.event_count
        wire_sequence = await client.send_protobuf(
            QUICK_TEST_COMMAND,
            pb_notice_pb2._QuickCmd(cmd=quick_command),
        )
        gui_ping: dict[str, object] = {
            "ok": False,
            "pb_command": f"0x{QUICK_TEST_COMMAND:04X}",
            "wire_sequence": wire_sequence,
            "gui_sequence": gui_sequence,
            "quick_command": quick_command,
        }
        result["gui_ping"] = gui_ping

        accepted = await _wait_for_uart_event(
            serial,
            request="gui_ping",
            event_type="command_result",
            status="accepted",
            timeout=args.uart_timeout,
            start_event_index=start_event_index,
        )
        processed = await _wait_for_uart_event(
            serial,
            request="gui_ping",
            seq=gui_sequence,
            event_type="gui_ack",
            status="processed",
            timeout=args.uart_timeout,
            start_event_index=start_event_index,
        )
        gui_ping.update(
            {
                "ok": True,
                "uart_command_result": accepted,
                "uart_gui_ack": processed,
            }
        )

        device_info = await client.exchange_protobuf(
            GET_DEVICE_INFO_COMMAND,
            None,
            pb_config_pb2._DeviceInfo,
            timeout=args.pb_timeout,
        )
        result["read_only_pb"] = {
            "ok": True,
            "pb_command": f"0x{GET_DEVICE_INFO_COMMAND:04X}",
            "response_type": "_DeviceInfo",
            "device_info": _message_dict(device_info),
        }
        if args.goal_cycle:
            await _run_goal_cycle(args, result, client, serial)
        if args.find_watch_cycle:
            await _run_find_watch_cycle(args, result, client, serial)
        result["ok"] = True
    finally:
        try:
            if client is not None:
                await client.close()
        finally:
            if serial.started:
                result["uart_background_errors"] = serial.background_errors_since()
                serial.stop()


def _build_set_164_case_result(
    probe_result: dict[str, object],
    probe_result_path: Path,
    *,
    case: CaseEntry | None = None,
) -> CaseRunResult:
    """Convert one completed BLE transaction into the normal Agent-loop result contract."""

    if case is None:
        from agent_loop_system.tools.case_map import load_case_map

        case = load_case_map(
            "设置",
            target="hardware",
            profile="6202_W5230",
        )["SET_164"]

    cycle = probe_result.get("find_watch_cycle")
    cycle = cycle if isinstance(cycle, dict) else {}
    original = cycle.get("original_dnd")
    changed = cycle.get("changed_dnd_readback")
    temporary = cycle.get("temporary_dnd")
    screenshot = cycle.get("screenshot")
    screenshot = screenshot if isinstance(screenshot, dict) else {}
    metadata = screenshot.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    screenshot_path = Path(str(screenshot.get("path") or ""))
    fence = cycle.get("find_page_fence")
    fence = fence if isinstance(fence, dict) else {}
    ack = fence.get("uart_gui_ack")
    ack = ack if isinstance(ack, dict) else {}
    stop = cycle.get("stop_find")
    stop = stop if isinstance(stop, dict) else {}
    restore = cycle.get("restore")
    restore = restore if isinstance(restore, dict) else {}

    checks = {
        "dnd_prepared": (
            isinstance(original, dict)
            and isinstance(changed, dict)
            and isinstance(temporary, dict)
            and changed == temporary
            and changed.get("isEnabled") is True
        ),
        "find_processed": (
            isinstance(cycle.get("find_wire_sequence"), int)
            and ack.get("status") == "processed"
        ),
        "screenshot_verified": (
            screenshot.get("ok") is True
            and screenshot_path.is_file()
            and screenshot_path.stat().st_size > 0
            and metadata.get("receipt_verified") is True
        ),
        "stop_verified": stop.get("ok") is True,
        "restore_verified": (
            restore.get("ok") is True
            and isinstance(original, dict)
            and restore.get("readback") == original
        ),
        "transport_clean": probe_result.get("uart_background_errors") == [],
        "transaction_complete": (
            probe_result.get("ok") is True and cycle.get("ok") is True
        ),
    }
    issue_messages = {
        "dnd_prepared": "All Day 勿扰设置或回读未完成",
        "find_processed": "FIND_DEVICE 未到达已处理状态",
        "screenshot_verified": "查找手表截图缺失或 MTP 回执未验证",
        "stop_verified": "STOP_FIND_DEVICE 未完成",
        "restore_verified": "勿扰旧值未得到精确恢复回读",
        "transport_clean": "串口观察器存在后台错误",
        "transaction_complete": "SET_164 BLE 事务未完整结束",
    }
    issues = [
        {"code": name, "message": issue_messages[name]}
        for name, ok in checks.items()
        if not ok
    ]
    probe_error = probe_result.get("error")
    if isinstance(probe_error, dict) and probe_error.get("message"):
        issues.insert(
            0,
            {
                "code": "ble_adapter_error",
                "message": str(probe_error["message"]),
            },
        )
    result = CaseRunResult(
        case_id=case.case_id,
        sheet=case.sheet,
        expected_text=case.expected_text,
        execution_mode="external_ble_adapter",
        precondition_text=case.precondition_text,
        steps_text=case.steps_text,
        verification_points=[_SET_164_VERIFICATION_POINT],
        planned_commands={
            "setup": ["BLE_PB 0x0209 GET_DND_CONFIG", "BLE_PB 0x020A SET_DND_CONFIG"],
            "action": ["BLE_PB 0x0230 FIND_DEVICE"],
            "collect": ["MTP SCREENSHOT", "BLE_PB 0x0231 STOP + restore DND"],
        },
        terminal_json=[
            {
                "type": "external_ble_case_evidence",
                "probe_result_path": str(probe_result_path),
                "device": probe_result.get("device"),
                "find_watch_cycle": cycle,
            }
        ],
    )
    trace_specs = (
        ("setup", "SET_DND_CONFIG", "0x0209 + 0x020A", checks["dnd_prepared"]),
        ("action", "FIND_DEVICE", "0x0230", checks["find_processed"]),
        (
            "collect",
            "HOST_SCREENSHOT",
            ":HOST_SCREENSHOT:SET_164",
            checks["screenshot_verified"],
        ),
        (
            "collect",
            "STOP_AND_RESTORE",
            "0x0231 + 0x020A + 0x0209",
            checks["stop_verified"] and checks["restore_verified"],
        ),
    )
    result.command_trace = [
        {
            "index": index,
            "phase": phase,
            "source": "external_ble_adapter",
            "kind": "screenshot" if name == "HOST_SCREENSHOT" else "ble_protobuf",
            "wire": command,
            "command": command,
            "command_name": name,
            "status": "completed" if ok else "error",
            "ok": ok,
        }
        for index, (phase, name, command, ok) in enumerate(trace_specs, 1)
    ]
    if checks["screenshot_verified"]:
        result.screenshots = [
            {
                "index": 1,
                "label": _SET_164_VERIFICATION_POINT,
                "phase": "collect",
                "command": ":HOST_SCREENSHOT:SET_164",
                "path": str(screenshot_path),
                "capture_metadata": metadata,
                "trace_index": 3,
            }
        ]
    result.exploration_trace = {
        "adapter": "watch_ble_probe",
        "probe_result_path": str(probe_result_path),
    }
    result.evidence_contract = {
        "status": "COMPLETE" if not issues else "ERROR",
        "complete": not issues,
        "required_screenshots": 1,
        "captured_screenshots": len(result.screenshots),
        "planned_action_count": 1,
        "attempted_action_count": int(checks["find_processed"]),
        "business_action_count": int(checks["find_processed"]),
        "cleanup_verified": checks["stop_verified"],
        "restore_verified": checks["restore_verified"],
        "issues": issues,
    }
    return result


def run_set_164_case(
    case: CaseEntry,
    screenshot_path: str,
    *,
    address: str | None = None,
    name: str | None = None,
) -> CaseRunResult:
    """Execute SET_164 for the standard Agent-loop single-case entrypoint."""

    if case.case_id != "SET_164":
        raise ValueError("watch_ble execution adapter currently supports only SET_164")
    if address and name:
        raise ValueError("select the BLE watch by either address or name, not both")

    screenshot = Path(screenshot_path).resolve()
    evidence_dir = screenshot.parent
    evidence_dir.mkdir(parents=True, exist_ok=True)
    screenshot.unlink(missing_ok=True)
    try:
        scan_timeout = float(
            os.environ.get("W30_HARDWARE_BLE_SCAN_TIMEOUT", "15")
        )
    except ValueError as exc:
        raise ValueError("W30_HARDWARE_BLE_SCAN_TIMEOUT must be positive") from exc
    if scan_timeout <= 0:
        raise ValueError("W30_HARDWARE_BLE_SCAN_TIMEOUT must be positive")

    args = argparse.Namespace(
        address=address,
        name=name,
        scan_timeout=scan_timeout,
        ble_timeout=15.0,
        uart_timeout=12.0,
        pb_timeout=15.0,
        goal_cycle=False,
        find_watch_cycle=True,
        goal_step=None,
        capture_timeout=30.0,
        screenshot=screenshot,
        serial_port=(
            os.environ.get("W30_HARDWARE_PORT", "").strip() or DEFAULT_PORT
        ),
        gui_sequence=None,
        evidence_dir=evidence_dir,
        agent_loop_result_file=None,
    )
    probe_result: dict[str, object] = {
        "probe": "6202_pb_over_ble",
        "entrypoint": "agent_loop_standard_runner",
        "ok": False,
        "evidence_dir": str(evidence_dir),
    }
    try:
        asyncio.run(run_probe(args, probe_result))
    except Exception as exc:
        probe_result["error"] = _error_payload(exc)

    probe_result_path = evidence_dir / "probe_result.json"
    probe_result["result_path"] = str(probe_result_path)
    probe_result_path.write_text(
        json.dumps(probe_result, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return _build_set_164_case_result(
        probe_result,
        probe_result_path,
        case=case,
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Verify 6202 GUI_PING and one read-only PB command over BLE",
    )
    selector = parser.add_mutually_exclusive_group()
    selector.add_argument("--address")
    selector.add_argument("--name")
    parser.add_argument("--scan-timeout", type=float, default=10.0)
    parser.add_argument("--ble-timeout", type=float, default=15.0)
    parser.add_argument("--uart-timeout", type=float, default=12.0)
    parser.add_argument("--pb-timeout", type=float, default=15.0)
    cycle = parser.add_mutually_exclusive_group()
    cycle.add_argument(
        "--goal-cycle",
        action="store_true",
        help="temporarily change the step goal, capture DATA, then restore it",
    )
    cycle.add_argument(
        "--find-watch-cycle",
        action="store_true",
        help=(
            "run SET_164 by enabling All Day DND, starting Find Watch, "
            "capturing the watch, then stopping and restoring DND"
        ),
    )
    parser.add_argument(
        "--goal-step",
        type=int,
        help="temporary positive step goal; defaults to 10000 or 8000",
    )
    parser.add_argument("--capture-timeout", type=float, default=30.0)
    parser.add_argument("--screenshot", type=Path)
    parser.add_argument(
        "--serial-port",
        default=os.environ.get("W30_HARDWARE_PORT", DEFAULT_PORT),
    )
    parser.add_argument("--gui-sequence", type=lambda value: int(value, 0))
    parser.add_argument("--evidence-dir", type=Path)
    parser.add_argument(
        "--agent-loop-result-file",
        type=Path,
        help="for --find-watch-cycle, also write the normal Agent-loop result schema",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.gui_sequence is not None and not 1 <= args.gui_sequence <= _MAX_GUI_SEQUENCE:
        raise SystemExit("--gui-sequence must be between 1 and 2147483647")
    for name in (
        "scan_timeout",
        "ble_timeout",
        "uart_timeout",
        "pb_timeout",
        "capture_timeout",
    ):
        if getattr(args, name) <= 0:
            raise SystemExit(f"--{name.replace('_', '-')} must be positive")
    if args.agent_loop_result_file is not None and not args.find_watch_cycle:
        raise SystemExit("--agent-loop-result-file requires --find-watch-cycle")

    evidence_dir = (args.evidence_dir or _default_evidence_dir()).resolve()
    evidence_dir.mkdir(parents=True, exist_ok=True)
    args.evidence_dir = evidence_dir
    result: dict[str, object] = {
        "probe": "6202_pb_over_ble",
        "ok": False,
        "evidence_dir": str(evidence_dir),
    }
    exit_code = 0
    try:
        asyncio.run(run_probe(args, result))
    except Exception as exc:
        result["error"] = _error_payload(exc)
        exit_code = 1

    result_path = evidence_dir / (
        "probe_result.json" if args.agent_loop_result_file is not None else "result.json"
    )
    result["result_path"] = str(result_path)
    if exit_code == 0 and args.agent_loop_result_file is not None:
        try:
            from agent_loop_system.main import _load_env
            from agent_loop_system.tools.test import judge_case_result, save_evidence

            _load_env()
            case_result = _build_set_164_case_result(result, result_path)
            decision = judge_case_result(case_result)
            evidence_path = save_evidence(
                case_result,
                decision,
                args.agent_loop_result_file.resolve(),
            )
            result["agent_loop_evidence"] = {
                "path": str(evidence_path),
                "verdict": decision.verdict,
                "reason": decision.reason,
            }
            if decision.verdict != "PASS":
                exit_code = 1
        except Exception as exc:
            result["agent_loop_evidence"] = {
                "path": str(args.agent_loop_result_file.resolve()),
                "error": _error_payload(exc),
            }
            exit_code = 1
    rendered = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True)
    result_path.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return exit_code


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
