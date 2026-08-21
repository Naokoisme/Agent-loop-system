import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from agent_loop_system.tools.case_map import CaseEntry
from agent_loop_system.tools.watch_ble import WatchBleDevice
from agent_loop_system.tools.watch_ble_probe import (
    _build_set_164_case_result,
    _discover_watch,
    run_set_164_case,
)


def test_discovery_recovers_missing_explicit_address_via_uart() -> None:
    target = WatchBleDevice("54:C8:D4:D9:29:06", None, -54)
    args = SimpleNamespace(
        address="54:C8:D4:D9:29:06",
        name=None,
        scan_timeout=1.0,
        serial_port="COM7",
    )
    result = {}
    serial = mock.Mock()
    serial.started = False

    def start() -> None:
        serial.started = True

    serial.start.side_effect = start
    with mock.patch(
        "agent_loop_system.tools.watch_ble_probe.scan_watches",
        new=mock.AsyncMock(side_effect=[[], [target]]),
    ) as scan, mock.patch(
        "agent_loop_system.tools.watch_ble_probe.asyncio.sleep",
        new=mock.AsyncMock(),
    ):
        device = asyncio.run(_discover_watch(args, result, serial))

    assert device == target
    assert scan.await_count == 2
    serial.start.assert_called_once_with()
    assert serial.write_shell_line.call_args_list == [
        mock.call("btm le adv_start"),
    ]
    assert result["scan"]["attempts"] == 2
    assert result["ble_advertising_recovery"]["ok"] is True
    assert result["ble_advertising_recovery"]["strategy"] == "adv_start"
    assert result["ble_advertising_recovery"]["verified_by"] == "address_rescan"


def test_discovery_restarts_stale_advertising_when_start_is_not_enough() -> None:
    target = WatchBleDevice("54:C8:D4:D9:29:06", None, -54)
    args = SimpleNamespace(
        address="54:C8:D4:D9:29:06",
        name=None,
        scan_timeout=1.0,
        serial_port="COM7",
    )
    result = {}
    serial = mock.Mock()
    serial.started = False

    def start() -> None:
        serial.started = True

    serial.start.side_effect = start
    with mock.patch(
        "agent_loop_system.tools.watch_ble_probe.scan_watches",
        new=mock.AsyncMock(side_effect=[[], [], [target]]),
    ) as scan, mock.patch(
        "agent_loop_system.tools.watch_ble_probe.asyncio.sleep",
        new=mock.AsyncMock(),
    ):
        device = asyncio.run(_discover_watch(args, result, serial))

    assert device == target
    assert scan.await_count == 3
    assert serial.write_shell_line.call_args_list == [
        mock.call("btm le adv_start"),
        mock.call("btm le adv_stop"),
        mock.call("btm le adv_start"),
    ]
    assert result["scan"]["attempts"] == 3
    assert result["ble_advertising_recovery"]["strategy"] == "adv_restart"


def test_discovery_does_not_touch_uart_when_address_is_found() -> None:
    target = WatchBleDevice("54:C8:D4:D9:29:06", None, -54)
    args = SimpleNamespace(
        address="54:C8:D4:D9:29:06",
        name=None,
        scan_timeout=1.0,
        serial_port="COM7",
    )
    result = {}
    serial = mock.Mock(started=False)
    with mock.patch(
        "agent_loop_system.tools.watch_ble_probe.scan_watches",
        new=mock.AsyncMock(return_value=[target]),
    ) as scan:
        device = asyncio.run(_discover_watch(args, result, serial))

    assert device == target
    scan.assert_awaited_once()
    serial.start.assert_not_called()
    serial.write_shell_line.assert_not_called()
    assert "ble_advertising_recovery" not in result


def test_set_164_probe_builds_complete_agent_loop_contract(tmp_path: Path) -> None:
    screenshot = tmp_path / "find_watch.bmp"
    screenshot.write_bytes(b"BM" + bytes(32))
    original = {"periodStart": 1320, "periodEnd": 420}
    temporary = {**original, "isEnabled": True}
    probe_result = {
        "ok": True,
        "device": {"address": "54:C8:D4:D9:29:06"},
        "uart_background_errors": [],
        "find_watch_cycle": {
            "ok": True,
            "original_dnd": original,
            "temporary_dnd": temporary,
            "changed_dnd_readback": temporary,
            "find_wire_sequence": 5,
            "find_page_fence": {"uart_gui_ack": {"status": "processed"}},
            "screenshot": {
                "ok": True,
                "path": str(screenshot),
                "metadata": {"receipt_verified": True},
            },
            "stop_find": {"ok": True},
            "restore": {"ok": True, "readback": original},
        },
    }
    case = CaseEntry(
        case_id="SET_164",
        sheet="设置",
        steps_text="从App发起查找手表",
        expected_text="手表显示查找设备提醒",
    )

    result = _build_set_164_case_result(
        probe_result,
        tmp_path / "probe_result.json",
        case=case,
    )

    assert result.execution_mode == "external_ble_adapter"
    assert result.evidence_contract["complete"] is True
    assert result.evidence_contract["cleanup_verified"] is True
    assert result.evidence_contract["restore_verified"] is True
    assert result.command_trace[1]["command_name"] == "FIND_DEVICE"
    assert result.screenshots[0]["path"] == str(screenshot)


def test_standard_runner_adapter_executes_and_records_set_164(tmp_path: Path) -> None:
    original = {"periodStart": 1320, "periodEnd": 420}
    temporary = {**original, "isEnabled": True}

    async def fake_probe(args, probe_result) -> None:
        args.screenshot.write_bytes(b"BM" + bytes(32))
        probe_result.update(
            {
                "ok": True,
                "device": {"address": "54:C8:D4:D9:29:06"},
                "uart_background_errors": [],
                "find_watch_cycle": {
                    "ok": True,
                    "original_dnd": original,
                    "temporary_dnd": temporary,
                    "changed_dnd_readback": temporary,
                    "find_wire_sequence": 5,
                    "find_page_fence": {
                        "uart_gui_ack": {"status": "processed"}
                    },
                    "screenshot": {
                        "ok": True,
                        "path": str(args.screenshot),
                        "metadata": {"receipt_verified": True},
                    },
                    "stop_find": {"ok": True},
                    "restore": {"ok": True, "readback": original},
                },
            }
        )

    case = CaseEntry(
        case_id="SET_164",
        sheet="设置",
        steps_text="从App发起查找手表",
        expected_text="手表显示查找设备提醒",
    )
    screenshot = tmp_path / "screenshot.bmp"
    with mock.patch(
        "agent_loop_system.tools.watch_ble_probe.run_probe",
        side_effect=fake_probe,
    ):
        result = run_set_164_case(
            case,
            str(screenshot),
            address="54:C8:D4:D9:29:06",
        )

    probe = json.loads((tmp_path / "probe_result.json").read_text(encoding="utf-8"))
    assert probe["entrypoint"] == "agent_loop_standard_runner"
    assert result.evidence_contract["complete"] is True
    assert result.screenshots[0]["path"] == str(screenshot)


def test_set_164_contract_reports_the_actual_adapter_error_first(tmp_path: Path) -> None:
    case = CaseEntry(
        case_id="SET_164",
        sheet="设置",
        expected_text="手表显示查找设备提醒",
    )
    result = _build_set_164_case_result(
        {
            "ok": False,
            "error": {
                "type": "WatchBleDiscoveryError",
                "message": "no BLE device matched 'watch'",
            },
        },
        tmp_path / "probe_result.json",
        case=case,
    )

    assert result.evidence_contract["complete"] is False
    assert result.evidence_contract["issues"][0] == {
        "code": "ble_adapter_error",
        "message": "no BLE device matched 'watch'",
    }
