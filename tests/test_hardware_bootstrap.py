from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from agent_loop_system.tools import hardware_bootstrap, real_device
from agent_loop_system.tools.hardware_serial import (
    HardwareCommandResult,
    HardwareSerialTimeoutError,
)
from agent_loop_system.tools.real_device import (
    TestSessionBootstrapError as BootstrapError,
    TestSessionBootstrapResult as BootstrapResult,
    TestSessionStatus as SessionStatus,
    bootstrap_test_session,
)


def _result(
    request: str,
    status: str,
    event_type: str,
    **extra: object,
) -> HardwareCommandResult:
    raw: dict[str, object] = {
        "type": event_type,
        "request": request,
        "status": status,
        **extra,
    }
    return HardwareCommandResult(
        request=request,
        status=status,
        raw=raw,
        lines=[],
        start_index=0,
    )


class FakeBootstrapSerial:
    def __init__(
        self,
        *responses: HardwareCommandResult | BaseException,
        ready_event: dict[str, object] | BaseException | None = None,
    ) -> None:
        self.responses = list(responses)
        self.ready_event = ready_event
        self.send_calls: list[tuple[str, dict[str, object]]] = []
        self.wait_calls: list[dict[str, object]] = []
        self.start_calls = 0
        self.stop_calls = 0
        self.shell_lines: list[str] = []

    @property
    def event_count(self) -> int:
        return 0

    def start(self) -> None:
        self.start_calls += 1

    def send(self, command: str, **kwargs: object) -> HardwareCommandResult:
        self.send_calls.append((command, dict(kwargs)))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response

    def wait_for_event(self, **kwargs: object) -> dict[str, object]:
        self.wait_calls.append(dict(kwargs))
        if isinstance(self.ready_event, BaseException):
            raise self.ready_event
        assert self.ready_event is not None
        return dict(self.ready_event)

    def stop(self) -> None:
        self.stop_calls += 1

    def write_shell_line(self, line: str) -> None:
        self.shell_lines.append(line)


class FakeMtpGate:
    def __init__(self) -> None:
        self.wait_calls: list[tuple[bool, float]] = []

    def wait_for_usb(self, *, present: bool, timeout: float) -> None:
        self.wait_calls.append((present, timeout))

    def inspect_usb_devices(self, *, timeout: float):
        return [{
            "instance_id": "USB\\VID_301A&PID_6808\\TEST",
            "status": "OK",
            "friendly_name": "ZORA",
            "class": "WPD",
        }]

    def probe_namespace(self, *, timeout: float):
        return {"device": "ZORA", "storage": "storage", "folder": "download"}


class FakeCaptureFrame:
    def __init__(self, index: int) -> None:
        self.index = index

    def save_bmp(self, output_path: str | os.PathLike[str]) -> None:
        Path(output_path).write_bytes(f"menu-style-{self.index}".encode("ascii"))


class FakeCaptureProvider:
    def __init__(self) -> None:
        self.capture_calls: list[dict[str, object]] = []
        self.close_calls = 0

    def capture(self, **kwargs: object) -> FakeCaptureFrame:
        self.capture_calls.append(dict(kwargs))
        return FakeCaptureFrame(len(self.capture_calls))

    def close(self) -> None:
        self.close_calls += 1


class BootstrapTestSessionTest(unittest.TestCase):
    def test_owned_session_refuses_direct_com_transport(self) -> None:
        with tempfile.TemporaryDirectory() as root, mock.patch.dict(
            os.environ,
            {"W30_HARDWARE_TRANSPORT": "serial"},
            clear=True,
        ), mock.patch.object(real_device, "_create_hardware_serial_session") as create:
            with self.assertRaisesRegex(ValueError, "supercom"):
                bootstrap_test_session(evidence_dir=root)

        create.assert_not_called()

    def test_ping_then_sends_exactly_one_start_without_status_or_stop(self) -> None:
        serial = FakeBootstrapSerial(
            _result("gui_ping", "processed", "gui_ack", seq=41),
            _result(
                "test_session",
                "active",
                "test_session",
                lease_seconds=86400,
            ),
        )
        with tempfile.TemporaryDirectory() as root, mock.patch.object(
            real_device, "_positive_handshake_sequence", return_value=41
        ):
            result = bootstrap_test_session(
                evidence_dir=root,
                serial_session=serial,
            )

        self.assertTrue(result.status.active)
        self.assertEqual(result.status.lease_seconds, 86400)
        self.assertTrue(result.start_sent)
        self.assertEqual(result.gui_ping_attempts, 1)
        self.assertFalse(result.bootstrap_event_seen)
        self.assertEqual(
            [command for command, _kwargs in serial.send_calls],
            [":GUI_PING:41", ":TEST_SESSION:START"],
        )
        self.assertNotIn(":TEST_SESSION:STATUS", [call[0] for call in serial.send_calls])
        self.assertEqual(serial.start_calls, 1)
        self.assertEqual(serial.stop_calls, 1)

    def test_waits_for_bootstrap_ready_then_retries_only_gui_ping(self) -> None:
        serial = FakeBootstrapSerial(
            HardwareSerialTimeoutError("not ready"),
            _result("gui_ping", "processed", "gui_ack", seq=52),
            _result(
                "test_session",
                "active",
                "test_session",
                lease_seconds=86399,
            ),
            ready_event={
                "type": "test_bootstrap",
                "request": "test_bootstrap",
                "status": "ready",
                "grace_seconds": 180,
            },
        )
        with tempfile.TemporaryDirectory() as root, mock.patch.object(
            real_device, "_positive_handshake_sequence", side_effect=(51, 52)
        ):
            result = bootstrap_test_session(
                evidence_dir=root,
                serial_session=serial,
            )

        self.assertEqual(result.gui_ping_attempts, 2)
        self.assertTrue(result.bootstrap_event_seen)
        self.assertEqual(
            [command for command, _kwargs in serial.send_calls],
            [":GUI_PING:51", ":GUI_PING:52", ":TEST_SESSION:START"],
        )
        self.assertEqual(len(serial.wait_calls), 1)
        self.assertEqual(serial.stop_calls, 1)

    def test_expired_bootstrap_never_sends_start(self) -> None:
        serial = FakeBootstrapSerial(
            HardwareSerialTimeoutError("asleep"),
            ready_event={
                "type": "test_bootstrap",
                "request": "test_bootstrap",
                "status": "expired",
                "reason": "grace_expired",
            },
        )
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaises(BootstrapError) as raised:
                bootstrap_test_session(
                    evidence_dir=root,
                    serial_session=serial,
                )

        self.assertEqual(raised.exception.code, "BLOCKED_LOW_POWER_WAKE")
        self.assertEqual(len(serial.send_calls), 1)
        self.assertTrue(serial.send_calls[0][0].startswith(":GUI_PING:"))
        self.assertEqual(serial.stop_calls, 1)

    def test_already_active_reply_is_success_without_renewal_protocol(self) -> None:
        serial = FakeBootstrapSerial(
            _result("gui_ping", "processed", "gui_ack", seq=61),
            _result(
                "test_session",
                "active",
                "test_session",
                lease_seconds=43210,
                reason="already_active",
            ),
        )
        with tempfile.TemporaryDirectory() as root, mock.patch.object(
            real_device, "_positive_handshake_sequence", return_value=61
        ):
            result = bootstrap_test_session(
                evidence_dir=root,
                serial_session=serial,
            )

        self.assertEqual(result.status.lease_seconds, 43210)
        self.assertEqual(result.status.raw["reason"], "already_active")
        self.assertEqual([call[0] for call in serial.send_calls].count(":TEST_SESSION:START"), 1)


class HardwareCaseResetTest(unittest.TestCase):
    @staticmethod
    def _serial(
        *,
        popup: object | None = None,
        menu_switches: int = 0,
    ) -> FakeBootstrapSerial:
        responses = [
            _result("gui_ping", "processed", "gui_ack", seq=101),
            _result("system_reboot", "accepted", "command_result"),
            _result("gui_ping", "processed", "gui_ack", seq=102),
            _result(
                "test_session",
                "active",
                "test_session",
                lease_seconds=86400,
            ),
            _result("button_press", "accepted", "command_result"),
            _result("enter_page", "accepted", "command_result"),
            _result("gui_ping", "processed", "gui_ack", seq=103),
            _result(
                "gui_state",
                "ok",
                "gui_state",
                seq=104,
                current_page={"id": 2, "name": "DIAL"},
                popup=popup,
            ),
            _result("button_press", "accepted", "command_result"),
            _result("gui_ping", "processed", "gui_ack", seq=105),
        ]
        for index in range(menu_switches):
            responses.extend([
                _result("button_press", "accepted", "command_result"),
                _result("gui_ping", "processed", "gui_ack", seq=106 + index),
            ])
        final_ping_sequence = 106 + menu_switches
        responses.extend([
            _result("enter_page", "accepted", "command_result"),
            _result("gui_ping", "processed", "gui_ack", seq=final_ping_sequence),
            _result(
                "gui_state",
                "ok",
                "gui_state",
                seq=final_ping_sequence + 1,
                current_page={"id": 2, "name": "DIAL"},
                popup=None,
            ),
        ])
        return FakeBootstrapSerial(*responses)

    def test_reboots_and_proves_clean_dial_before_returning(self) -> None:
        serial = self._serial()
        mtp = FakeMtpGate()
        capture = FakeCaptureProvider()
        judge = mock.Mock(return_value=("LIST_RADIUS", "截图显示单列列表"))
        environment = {"W30_HARDWARE_TRANSPORT": "supercom"}
        with tempfile.TemporaryDirectory() as root, mock.patch.object(
            real_device,
            "_create_hardware_serial_session",
            return_value=serial,
        ) as create_serial, mock.patch.object(
            real_device,
            "_positive_handshake_sequence",
            side_effect=range(101, 120),
        ):
            result = real_device.reset_hardware_case_state(
                evidence_dir=root,
                mtp_system=mtp,
                capture_provider=capture,
                menu_style_judge=judge,
                environment=environment,
            )
            self.assertTrue(Path(judge.call_args.args[0]).is_file())

        create_serial.assert_called_once_with(
            evidence_dir=root,
            cmd_timeout=8.0,
            environment=environment,
            allow_dangerous_commands=True,
        )
        self.assertEqual(
            [command for command, _kwargs in serial.send_calls],
            [
                ":GUI_PING:101",
                ":SYSTEM_REBOOT:",
                ":GUI_PING:102",
                ":TEST_SESSION:START",
                ":BUTTON_PRESS:1,1,0",
                ":ENTER_PAGE:DIAL,0",
                ":GUI_PING:103",
                ":GUI_STATE:104",
                ":BUTTON_PRESS:1,1,0",
                ":GUI_PING:105",
                ":ENTER_PAGE:DIAL,0",
                ":GUI_PING:106",
                ":GUI_STATE:107",
            ],
        )
        self.assertEqual(
            mtp.wait_calls,
            [(True, 30.0), (False, 30.0), (True, 30.0)],
        )
        self.assertEqual(serial.shell_lines, ["dal_usb open"])
        self.assertEqual(serial.stop_calls, 1)
        self.assertEqual(result.reboot_status, "accepted")
        self.assertEqual(result.status.lease_seconds, 86400)
        self.assertEqual(result.current_page, "DIAL")
        self.assertIsNone(result.popup)
        self.assertEqual(capture.capture_calls, [{"timeout": 12.0}])
        self.assertEqual(capture.close_calls, 1)
        judge.assert_called_once()

    def test_default_menu_style_classifier_returns_structured_style(self) -> None:
        screenshot = Path("menu-style.bmp")
        visual = SimpleNamespace(style="HONEYCOMB", reason="截图显示蜂窝风格")
        with mock.patch(
            "agent_loop_system.tools.test.classify_menu_style_with_vision",
            return_value=visual,
        ) as classify:
            style, reason = real_device._classify_menu_style(screenshot)

        self.assertEqual((style, reason), ("HONEYCOMB", "截图显示蜂窝风格"))
        classify.assert_called_once_with(str(screenshot))

    def test_popup_state_mismatch_blocks_the_case(self) -> None:
        serial = self._serial(popup={"id": 901, "name": "CHARGING"})
        mtp = FakeMtpGate()
        with tempfile.TemporaryDirectory() as root, mock.patch.object(
            real_device,
            "_positive_handshake_sequence",
            side_effect=range(101, 120),
        ):
            with self.assertRaises(real_device.HardwareCaseResetError) as raised:
                real_device.reset_hardware_case_state(
                    evidence_dir=root,
                    serial_session=serial,
                    mtp_system=mtp,
                )

        self.assertEqual(raised.exception.code, "RESET_STATE_MISMATCH")
        self.assertIn("popup", str(raised.exception))
        self.assertEqual(serial.stop_calls, 1)

    def test_exact_style_uses_minimum_switches_and_one_final_confirmation(self) -> None:
        expected_switches = {
            "LIST_RADIUS": 0,
            "HONEYCOMB": 3,
            "WATERFALL": 2,
            "GALACTIC_RING": 1,
        }
        for initial_style, switch_count in expected_switches.items():
            with self.subTest(initial_style=initial_style):
                serial = self._serial(menu_switches=switch_count)
                capture = FakeCaptureProvider()
                classifications = [(initial_style, "初始风格")]
                if switch_count:
                    classifications.append(("LIST_RADIUS", "已切换到列表风格"))
                judge = mock.Mock(side_effect=classifications)
                with tempfile.TemporaryDirectory() as root, mock.patch.object(
                    real_device,
                    "_positive_handshake_sequence",
                    side_effect=range(101, 140),
                ):
                    result = real_device.reset_hardware_case_state(
                        evidence_dir=root,
                        serial_session=serial,
                        mtp_system=FakeMtpGate(),
                        capture_provider=capture,
                        menu_style_judge=judge,
                    )
                    screenshots = sorted(Path(root).glob("menu-style-*.bmp"))
                    screenshot_contents = [
                        screenshot.read_bytes() for screenshot in screenshots
                    ]

                commands = [command for command, _kwargs in serial.send_calls]
                expected_captures = 1 if switch_count == 0 else 2
                self.assertEqual(
                    commands.count(":BUTTON_PRESS:1,3,0"),
                    switch_count,
                )
                self.assertEqual(commands.count(":ENTER_PAGE:DIAL,0"), 2)
                self.assertEqual(len(capture.capture_calls), expected_captures)
                self.assertEqual(len(screenshots), expected_captures)
                self.assertEqual(judge.call_count, expected_captures)
                self.assertEqual(capture.close_calls, 1)
                self.assertEqual(serial.stop_calls, 1)
                self.assertEqual(result.current_page, "DIAL")
                if switch_count:
                    self.assertEqual(
                        [path.name for path in screenshots],
                        ["menu-style-final.bmp", "menu-style-initial.bmp"],
                    )
                    self.assertNotEqual(
                        screenshot_contents[0],
                        screenshot_contents[1],
                    )

    def test_final_non_list_or_unknown_blocks_before_returning_to_dial(self) -> None:
        for final_style, expected_code in (
            ("HONEYCOMB", "MENU_STYLE_NOT_LIST"),
            ("UNKNOWN", "MENU_STYLE_UNVERIFIED"),
        ):
            with self.subTest(final_style=final_style):
                serial = self._serial(menu_switches=1)
                capture = FakeCaptureProvider()
                judge = mock.Mock(side_effect=[
                    ("GALACTIC_RING", "初始为星环风格"),
                    (final_style, "最终截图未确认列表风格"),
                ])
                with tempfile.TemporaryDirectory() as root, mock.patch.object(
                    real_device,
                    "_positive_handshake_sequence",
                    side_effect=range(101, 130),
                ):
                    with self.assertRaises(
                        real_device.HardwareCaseResetError
                    ) as raised:
                        real_device.reset_hardware_case_state(
                            evidence_dir=root,
                            serial_session=serial,
                            mtp_system=FakeMtpGate(),
                            capture_provider=capture,
                            menu_style_judge=judge,
                        )

                commands = [command for command, _kwargs in serial.send_calls]
                self.assertEqual(raised.exception.code, expected_code)
                self.assertEqual(commands.count(":BUTTON_PRESS:1,3,0"), 1)
                self.assertEqual(commands.count(":ENTER_PAGE:DIAL,0"), 1)
                self.assertEqual(len(capture.capture_calls), 2)
                self.assertEqual(judge.call_count, 2)
                self.assertEqual(capture.close_calls, 1)
                self.assertEqual(serial.stop_calls, 1)

    def test_unknown_initial_style_blocks_without_cycling(self) -> None:
        serial = self._serial()
        capture = FakeCaptureProvider()
        with tempfile.TemporaryDirectory() as root, mock.patch.object(
            real_device,
            "_positive_handshake_sequence",
            side_effect=range(101, 120),
        ):
            with self.assertRaises(real_device.HardwareCaseResetError) as raised:
                real_device.reset_hardware_case_state(
                    evidence_dir=root,
                    serial_session=serial,
                    mtp_system=FakeMtpGate(),
                    capture_provider=capture,
                    menu_style_judge=lambda _path: (
                        "UNKNOWN",
                        "截图被遮挡",
                    ),
                )

        commands = [command for command, _kwargs in serial.send_calls]
        self.assertEqual(raised.exception.code, "MENU_STYLE_UNVERIFIED")
        self.assertNotIn(":BUTTON_PRESS:1,3,0", commands)
        self.assertEqual(capture.close_calls, 1)
        self.assertEqual(serial.stop_calls, 1)

    def test_explicit_reboot_is_blocked_when_uart_is_unresponsive(self) -> None:
        serial = FakeBootstrapSerial(
            _result("gui_ping", "timeout", "error", seq=101),
        )
        mtp = FakeMtpGate()
        with tempfile.TemporaryDirectory() as root, mock.patch.object(
            real_device,
            "_positive_handshake_sequence",
            side_effect=range(101, 110),
        ):
            with self.assertRaises(real_device.HardwareCaseResetError) as raised:
                real_device.reset_hardware_case_state(
                    evidence_dir=root,
                    serial_session=serial,
                    mtp_system=mtp,
                )

        self.assertEqual(raised.exception.code, "REBOOT_UART_UNRESPONSIVE")
        self.assertEqual([call[0] for call in serial.send_calls], [":GUI_PING:101"])
        self.assertNotIn(":SYSTEM_REBOOT:", [call[0] for call in serial.send_calls])
        self.assertEqual(mtp.wait_calls, [])

    def test_explicit_reboot_is_blocked_when_initial_usb_is_absent(self) -> None:
        class MissingUsbGate(FakeMtpGate):
            def wait_for_usb(self, *, present: bool, timeout: float) -> None:
                super().wait_for_usb(present=present, timeout=timeout)
                raise TimeoutError("USB absent")

        serial = FakeBootstrapSerial(
            _result("gui_ping", "processed", "gui_ack", seq=101),
        )
        mtp = MissingUsbGate()
        with tempfile.TemporaryDirectory() as root, mock.patch.object(
            real_device,
            "_positive_handshake_sequence",
            side_effect=range(101, 110),
        ):
            with self.assertRaises(real_device.HardwareCaseResetError) as raised:
                real_device.reset_hardware_case_state(
                    evidence_dir=root,
                    serial_session=serial,
                    mtp_system=mtp,
                )

        self.assertEqual(raised.exception.code, "REBOOT_INITIAL_USB_ABSENT")
        self.assertNotIn(":SYSTEM_REBOOT:", [call[0] for call in serial.send_calls])
        self.assertEqual(mtp.wait_calls, [(True, 30.0)])

    def test_soft_preparation_never_sends_system_reboot(self) -> None:
        serial = FakeBootstrapSerial(
            _result("gui_ping", "processed", "gui_ack", seq=101),
            _result(
                "test_session",
                "active",
                "test_session",
                lease_seconds=86400,
            ),
            _result("button_press", "accepted", "command_result"),
            _result("enter_page", "accepted", "command_result"),
            _result("gui_ping", "processed", "gui_ack", seq=102),
            _result(
                "gui_state",
                "ok",
                "gui_state",
                seq=103,
                current_page={"id": 2, "name": "DIAL"},
                popup=None,
            ),
            _result("button_press", "accepted", "command_result"),
            _result("gui_ping", "processed", "gui_ack", seq=104),
            _result("enter_page", "accepted", "command_result"),
            _result("gui_ping", "processed", "gui_ack", seq=105),
            _result(
                "gui_state",
                "ok",
                "gui_state",
                seq=106,
                current_page={"id": 2, "name": "DIAL"},
                popup=None,
            ),
        )
        mtp = FakeMtpGate()
        capture = FakeCaptureProvider()
        environment = {"W30_HARDWARE_TRANSPORT": "supercom"}
        with tempfile.TemporaryDirectory() as root, mock.patch.object(
            real_device,
            "_create_hardware_serial_session",
            return_value=serial,
        ) as create_serial, mock.patch.object(
            real_device,
            "_positive_handshake_sequence",
            side_effect=range(101, 120),
        ):
            result = real_device.prepare_hardware_case_state(
                evidence_dir=root,
                mtp_system=mtp,
                capture_provider=capture,
                menu_style_judge=lambda _path: ("LIST_RADIUS", "list"),
                environment=environment,
            )

        create_serial.assert_called_once_with(
            evidence_dir=root,
            cmd_timeout=8.0,
            environment=environment,
            allow_dangerous_commands=False,
        )
        commands = [call[0] for call in serial.send_calls]
        self.assertNotIn(":SYSTEM_REBOOT:", commands)
        self.assertEqual(result.reboot_status, "not_requested")
        self.assertEqual(result.current_page, "DIAL")
        self.assertEqual(mtp.wait_calls, [])


class HardwareBootstrapCliTest(unittest.TestCase):
    def test_environment_requires_the_shared_workspace_and_supercom(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            config = SimpleNamespace(
                project="6202_W5230",
                source_root=Path(root).resolve(),
            )
            environment = {
                "W30_SOURCE_ROOT": root,
                "W30_AGENT_WORKSPACE_ROOT": root,
                "W30_PROJECT": "6202_W5230",
                "W30_HARDWARE_PROJECT": "6202_W5230",
                "W30_HARDWARE_TRANSPORT": "supercom",
                "W30_HARDWARE_PORT": "COM7",
            }
            with mock.patch.object(
                hardware_bootstrap.HardwareTargetConfig,
                "from_env",
                return_value=config,
            ), mock.patch.dict(os.environ, environment, clear=True):
                validated, port = hardware_bootstrap._validate_environment()

        self.assertIs(validated, config)
        self.assertEqual(port, "COM7")

    def test_environment_rejects_direct_serial_transport(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            config = SimpleNamespace(
                project="6202_W5230",
                source_root=Path(root).resolve(),
            )
            environment = {
                "W30_SOURCE_ROOT": root,
                "W30_AGENT_WORKSPACE_ROOT": root,
                "W30_PROJECT": "6202_W5230",
                "W30_HARDWARE_PROJECT": "6202_W5230",
                "W30_HARDWARE_TRANSPORT": "serial",
                "W30_HARDWARE_PORT": "COM7",
            }
            with mock.patch.object(
                hardware_bootstrap.HardwareTargetConfig,
                "from_env",
                return_value=config,
            ), mock.patch.dict(os.environ, environment, clear=True):
                with self.assertRaisesRegex(ValueError, "supercom"):
                    hardware_bootstrap._validate_environment()

    def test_cli_prints_machine_readable_success(self) -> None:
        bootstrap_result = BootstrapResult(
            status=SessionStatus(
                active=True,
                lease_seconds=86400,
                raw={"status": "active", "lease_seconds": 86400},
            ),
            start_sent=True,
            gui_ping_attempts=1,
            bootstrap_event_seen=False,
        )
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as root, mock.patch.object(
            hardware_bootstrap,
            "_validate_environment",
            return_value=(SimpleNamespace(project="6202_W5230"), "COM7"),
        ), mock.patch.object(
            hardware_bootstrap,
            "bootstrap_test_session",
            return_value=bootstrap_result,
        ) as bootstrap, redirect_stdout(output):
            exit_code = hardware_bootstrap.main(
                ["--evidence-dir", str(Path(root) / "evidence")]
            )

        payload = json.loads(output.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["status"], "active")
        self.assertEqual(payload["port"], "COM7")
        bootstrap.assert_called_once()


if __name__ == "__main__":
    unittest.main()
