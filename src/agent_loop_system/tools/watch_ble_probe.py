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
    command_id,
)
from agent_loop_system.tools.hardware_serial import (
    DEFAULT_PORT,
    HardwareSerialSession,
    SuperComPipeTransport,
)
from agent_loop_system.tools.mtp_screenshot import MtpCaptureProvider
from agent_loop_system.tools.watch_ble import (
    WatchBleClient,
    WatchBleDevice,
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
_MAX_GUI_SEQUENCE = 2_147_483_647


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


async def run_probe(
    args: argparse.Namespace,
    result: dict[str, object],
) -> None:
    devices = await scan_watches(timeout=args.scan_timeout)
    result["scan"] = {
        "ok": True,
        "devices": [asdict(device) for device in devices],
    }
    device: WatchBleDevice = select_watch(
        devices,
        address=args.address,
        name=args.name,
    )
    result["device"] = asdict(device)

    serial = HardwareSerialSession(
        port=args.serial_port,
        log_dir=Path(args.evidence_dir) / "serial",
        cmd_timeout=args.uart_timeout,
        transport=SuperComPipeTransport(args.serial_port),
        dtr=False,
        rts=False,
    )
    client = WatchBleClient(device, timeout=args.ble_timeout)

    try:
        serial.start()
        result["uart_observer"] = {
            "ok": True,
            "transport": "supercom_pipe",
            "port": args.serial_port,
        }

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
        result["ok"] = True
    finally:
        await client.close()
        if serial.started:
            result["uart_background_errors"] = serial.background_errors_since()
            serial.stop()


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
    parser.add_argument(
        "--goal-cycle",
        action="store_true",
        help="temporarily change the step goal, capture DATA, then restore it",
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

    result_path = evidence_dir / "result.json"
    result["result_path"] = str(result_path)
    rendered = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True)
    result_path.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return exit_code


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
