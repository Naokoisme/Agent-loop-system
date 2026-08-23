from __future__ import annotations

import os
import unittest
from unittest.mock import MagicMock, patch

from agent_loop_system.tools.hardware_serial_ports import (
    SUPERCOM_PIPE_PREFIX,
    SerialPortInfo,
    _classify_port_kind,
    _port_sort_key,
    check_supercom_pipe_open,
    enumerate_serial_ports,
    get_serial_ports_status,
    get_supercom_pipe_name,
    get_supercom_pipe_path,
    normalize_serial_port_name,
)


class HardwareSerialPortsTest(unittest.TestCase):
    def test_normalize_serial_port_name(self) -> None:
        self.assertEqual(normalize_serial_port_name("com7"), "COM7")
        self.assertEqual(normalize_serial_port_name("COM7"), "COM7")
        self.assertEqual(normalize_serial_port_name(r"\\.\COM7"), "COM7")
        self.assertEqual(normalize_serial_port_name(r"\\.\com10"), "COM10")
        self.assertEqual(normalize_serial_port_name("  com3  "), "COM3")
        self.assertEqual(normalize_serial_port_name(""), "")
        self.assertEqual(normalize_serial_port_name(None), "")

    def test_get_supercom_pipe_name_and_path(self) -> None:
        self.assertEqual(get_supercom_pipe_name("COM7"), "SuperCom.AgentBridge.COM7")
        self.assertEqual(get_supercom_pipe_name("com7"), "SuperCom.AgentBridge.COM7")
        self.assertEqual(get_supercom_pipe_path("COM7"), r"\\.\pipe\SuperCom.AgentBridge.COM7")
        self.assertEqual(get_supercom_pipe_name("COM_1-A"), "SuperCom.AgentBridge.COM_1-A")
        with self.assertRaises(ValueError):
            get_supercom_pipe_name("")
        with self.assertRaises(ValueError):
            get_supercom_pipe_name("COM7\n")

    def test_check_supercom_pipe_open_available(self) -> None:
        mock_kernel32 = MagicMock()
        mock_kernel32.WaitNamedPipeW.return_value = 1  # TRUE (pipe is available)

        with (
            patch("agent_loop_system.tools.hardware_serial_ports.os.name", "nt"),
            patch("ctypes.WinDLL", return_value=mock_kernel32),
            patch("ctypes.get_last_error", return_value=0),
        ):
            self.assertTrue(check_supercom_pipe_open("COM7"))
            mock_kernel32.WaitNamedPipeW.assert_called_once()
            args = mock_kernel32.WaitNamedPipeW.call_args[0]
            self.assertEqual(args[0], r"\\.\pipe\SuperCom.AgentBridge.COM7")

    def test_check_supercom_pipe_open_busy_or_timeout_means_open(self) -> None:
        mock_kernel32 = MagicMock()
        mock_kernel32.WaitNamedPipeW.return_value = 0  # FALSE

        # 231 = ERROR_PIPE_BUSY (all instances open/connected)
        with (
            patch("agent_loop_system.tools.hardware_serial_ports.os.name", "nt"),
            patch("ctypes.WinDLL", return_value=mock_kernel32),
            patch("ctypes.get_last_error", return_value=231),
        ):
            self.assertTrue(check_supercom_pipe_open("COM7"))

        # 121 = ERROR_SEM_TIMEOUT (pipe exists, timeout elapsed waiting)
        with (
            patch("agent_loop_system.tools.hardware_serial_ports.os.name", "nt"),
            patch("ctypes.WinDLL", return_value=mock_kernel32),
            patch("ctypes.get_last_error", return_value=121),
        ):
            self.assertTrue(check_supercom_pipe_open("COM7"))

        # 5 = ERROR_ACCESS_DENIED (pipe exists)
        with (
            patch("agent_loop_system.tools.hardware_serial_ports.os.name", "nt"),
            patch("ctypes.WinDLL", return_value=mock_kernel32),
            patch("ctypes.get_last_error", return_value=5),
        ):
            self.assertTrue(check_supercom_pipe_open("COM7"))

        # 535/536 = ERROR_PIPE_CONNECTED / ERROR_PIPE_LISTENING
        for error_code in (535, 536):
            with (
                patch("agent_loop_system.tools.hardware_serial_ports.os.name", "nt"),
                patch("ctypes.WinDLL", return_value=mock_kernel32),
                patch("ctypes.get_last_error", return_value=error_code),
            ):
                self.assertTrue(check_supercom_pipe_open("COM7"))

    def test_check_supercom_pipe_open_file_not_found_means_absent(self) -> None:
        mock_kernel32 = MagicMock()
        mock_kernel32.WaitNamedPipeW.return_value = 0  # FALSE

        # 2 = ERROR_FILE_NOT_FOUND (pipe does not exist)
        with (
            patch("agent_loop_system.tools.hardware_serial_ports.os.name", "nt"),
            patch("ctypes.WinDLL", return_value=mock_kernel32),
            patch("ctypes.get_last_error", return_value=2),
        ):
            self.assertFalse(check_supercom_pipe_open("COM7"))

    def test_check_supercom_pipe_open_non_windows(self) -> None:
        with patch("agent_loop_system.tools.hardware_serial_ports.os.name", "posix"):
            self.assertFalse(check_supercom_pipe_open("COM7"))

    def test_classify_port_kind(self) -> None:
        # FTDI USB device
        kind, label = _classify_port_kind(
            "COM7",
            "USB Serial Port (COM7)",
            "USB Serial Port",
            r"FTDIBUS\VID_0403+PID_6001+A50285BIA\0000",
            "FTDI",
        )
        self.assertEqual(kind, "usb")
        self.assertEqual(label, "USB 串口")

        # Silicon Labs / CH340 USB device
        kind, label = _classify_port_kind(
            "COM3",
            "Silicon Labs CP210x USB to UART Bridge (COM3)",
            "CP210x USB to UART Bridge",
            r"USB\VID_10C4&PID_EA60\0001",
            "Silicon Laboratories",
        )
        self.assertEqual(kind, "usb")
        self.assertEqual(label, "USB 串口")

        # Motherboard ACPI COM1 port
        kind, label = _classify_port_kind(
            "COM1",
            "Communications Port (COM1)",
            "Communications Port",
            r"ACPI\PNP0501\1",
            "(Standard port types)",
        )
        self.assertEqual(kind, "system")
        self.assertEqual(label, "系统/板载串口")

    def test_get_serial_ports_status_ordering_and_single_active_selection(self) -> None:
        mock_discovered = [
            {
                "port": "COM1",
                "friendly_name": "Communications Port (COM1)",
                "description": "Communications Port",
                "hardware_id": r"ACPI\PNP0501\1",
                "manufacturer": "Standard",
                "kind": "system",
                "kind_label": "系统/板载串口",
                "present": True,
            },
            {
                "port": "COM3",
                "friendly_name": "CH340 USB Serial (COM3)",
                "description": "CH340 USB Serial",
                "hardware_id": r"USB\VID_1A86&PID_7523\5&2C",
                "manufacturer": "wch.cn",
                "kind": "usb",
                "kind_label": "USB 串口",
                "present": True,
            },
            {
                "port": "COM7",
                "friendly_name": "USB Serial Port (COM7)",
                "description": "USB Serial Port",
                "hardware_id": r"FTDIBUS\VID_0403+PID_6001\0000",
                "manufacturer": "FTDI",
                "kind": "usb",
                "kind_label": "USB 串口",
                "present": True,
            },
        ]

        def fake_probe(port: str) -> bool:
            return port == "COM7"

        status = get_serial_ports_status(
            configured_port="COM7",
            probe_fn=fake_probe,
            enumeration_fn=lambda: mock_discovered,
        )

        self.assertTrue(status["available"])
        self.assertEqual(status["configured_port"], "COM7")
        self.assertEqual(status["selected_port"], "COM7")
        self.assertEqual(status["default_port"], "COM7")
        self.assertEqual(status["active_count"], 1)

        items = status["items"]
        self.assertEqual(len(items), 3)
        # Order: 1st Active SuperCom (COM7), 2nd Other USB (COM3), 3rd System/Legacy (COM1)
        self.assertEqual(items[0]["port"], "COM7")
        self.assertTrue(items[0]["supercom_open"])
        self.assertEqual(items[0]["kind"], "usb")

        self.assertEqual(items[1]["port"], "COM3")
        self.assertFalse(items[1]["supercom_open"])
        self.assertEqual(items[1]["kind"], "usb")

        self.assertEqual(items[2]["port"], "COM1")
        self.assertFalse(items[2]["supercom_open"])
        self.assertEqual(items[2]["kind"], "system")

    def test_multiple_active_ports_preserves_configured_selection(self) -> None:
        mock_discovered = [
            {"port": "COM1", "friendly_name": "COM1", "kind": "system", "kind_label": "系统/板载串口", "present": True},
            {"port": "COM7", "friendly_name": "COM7 (FTDI)", "kind": "usb", "kind_label": "USB 串口", "present": True},
            {"port": "COM8", "friendly_name": "COM8 (FTDI)", "kind": "usb", "kind_label": "USB 串口", "present": True},
        ]

        def fake_probe(port: str) -> bool:
            return port in ("COM7", "COM8")

        # If COM8 is configured, preserves COM8
        status = get_serial_ports_status(
            configured_port="COM8",
            probe_fn=fake_probe,
            enumeration_fn=lambda: mock_discovered,
        )
        self.assertEqual(status["active_count"], 2)
        self.assertEqual(status["selected_port"], "COM8")
        self.assertEqual(status["default_port"], "COM8")

        # If configured port is not among active ports, requires explicit choice
        status_unselected = get_serial_ports_status(
            configured_port="COM9",
            probe_fn=fake_probe,
            enumeration_fn=lambda: mock_discovered,
        )
        self.assertEqual(status_unselected["active_count"], 2)
        self.assertEqual(status_unselected["selected_port"], "")
        self.assertIsNone(status_unselected["default_port"])

    def test_no_active_supercom_port_does_not_select_com1(self) -> None:
        mock_discovered = [
            {"port": "COM1", "friendly_name": "Communications Port (COM1)", "kind": "system", "kind_label": "系统/板载串口", "present": True},
            {"port": "COM7", "friendly_name": "USB Serial Port (COM7)", "kind": "usb", "kind_label": "USB 串口", "present": True},
        ]

        def fake_probe(_port: str) -> bool:
            return False  # No SuperCom pipe open

        status = get_serial_ports_status(
            configured_port="COM7",
            probe_fn=fake_probe,
            enumeration_fn=lambda: mock_discovered,
        )
        self.assertEqual(status["active_count"], 0)
        self.assertEqual(status["selected_port"], "COM7")
        self.assertIsNone(status["default_port"])

        # Discovered items: USB COM7 is before system COM1
        self.assertEqual(status["items"][0]["port"], "COM7")
        self.assertFalse(status["items"][0]["supercom_open"])
        self.assertEqual(status["items"][1]["port"], "COM1")
        self.assertFalse(status["items"][1]["supercom_open"])

    def test_check_supercom_pipe_open_unknown_error_means_absent(self) -> None:
        mock_kernel32 = MagicMock()
        mock_kernel32.WaitNamedPipeW.return_value = 0  # FALSE

        # 123 = ERROR_INVALID_NAME (unknown error)
        with (
            patch("agent_loop_system.tools.hardware_serial_ports.os.name", "nt"),
            patch("ctypes.WinDLL", return_value=mock_kernel32),
            patch("ctypes.get_last_error", return_value=123),
        ):
            self.assertFalse(check_supercom_pipe_open("COM7"))

        # 6 = ERROR_INVALID_HANDLE (unknown error)
        with (
            patch("agent_loop_system.tools.hardware_serial_ports.os.name", "nt"),
            patch("ctypes.WinDLL", return_value=mock_kernel32),
            patch("ctypes.get_last_error", return_value=6),
        ):
            self.assertFalse(check_supercom_pipe_open("COM7"))

    def test_classify_port_kind_unknown_device(self) -> None:
        kind, label = _classify_port_kind(
            "COM4",
            "PCI Serial Port (COM4)",
            "PCI Serial Port",
            r"PCI\VEN_8086&DEV_1234\0000",
            "Intel",
        )
        self.assertEqual(kind, "unknown")
        self.assertEqual(label, "其他串口")

    def test_port_sorting_places_unknown_after_system(self) -> None:
        mock_discovered = [
            {"port": "COM4", "friendly_name": "PCI Port (COM4)", "kind": "unknown", "kind_label": "其他串口", "present": True},
            {"port": "COM1", "friendly_name": "Communications Port (COM1)", "kind": "system", "kind_label": "系统/板载串口", "present": True},
            {"port": "COM3", "friendly_name": "CH340 (COM3)", "kind": "usb", "kind_label": "USB 串口", "present": True},
            {"port": "COM7", "friendly_name": "FTDI (COM7)", "kind": "usb", "kind_label": "USB 串口", "present": True},
        ]

        def fake_probe(port: str) -> bool:
            return port == "COM7"

        status = get_serial_ports_status(
            configured_port="COM7",
            probe_fn=fake_probe,
            enumeration_fn=lambda: mock_discovered,
        )

        ports = [item["port"] for item in status["items"]]
        # Order: COM7 (open usb), COM3 (usb), COM1 (system), COM4 (unknown)
        self.assertEqual(ports, ["COM7", "COM3", "COM1", "COM4"])

    def test_missing_configured_port_is_retained_as_unavailable(self) -> None:
        mock_discovered = [
            {"port": "COM7", "friendly_name": "USB Serial Port (COM7)", "kind": "usb", "kind_label": "USB 串口", "present": True},
            {"port": "COM1", "friendly_name": "Communications Port (COM1)", "kind": "system", "kind_label": "系统/板载串口", "present": True},
        ]

        status = get_serial_ports_status(
            configured_port="COM9",
            probe_fn=lambda _p: False,
            enumeration_fn=lambda: mock_discovered,
        )

        ports = [item["port"] for item in status["items"]]
        self.assertIn("COM9", ports)
        com9_item = next(item for item in status["items"] if item["port"] == "COM9")
        self.assertFalse(com9_item["present"])
        self.assertTrue(com9_item["missing"])
        self.assertEqual(com9_item["kind"], "unknown")
        self.assertEqual(com9_item["kind_label"], "不可用")
        self.assertIn("未检测到设备", com9_item["friendly_name"])


if __name__ == "__main__":
    unittest.main()
