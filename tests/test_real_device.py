from __future__ import annotations

import tempfile
import unittest
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from PIL import Image

from agent_loop_system.tools import real_device
from agent_loop_system.tools.mtp_screenshot import MtpCaptureProvider
from agent_loop_system.tools.real_device import RealDeviceSession, query_test_session_status
from agent_loop_system.tools.watch_capture import WatchCaptureProvider
from tests.test_mtp_screenshot import FakeMtpSystem
from tests.test_watch_capture import FakeSerialSession, make_stream


@dataclass
class FakeMetadata:
    sequence: int
    timestamp: float = 123.5
    width: int = 3
    height: int = 2
    pixel_format: object = field(
        default_factory=lambda: SimpleNamespace(name="BGR24")
    )
    data_size: int = 18
    stride: int = 9
    source: str = "watch_display"
    transport: str = "hardware_serial"
    payload_crc32: int = 0x1234ABCD
    chunk_bytes: int = 384
    chunks: int = 1608
    encoding: str = "base64"
    capture_duration_ms: int | None = 17
    device_uptime_ms: int | None = 123_456
    pixel_source: str | None = "vde_lcd_composite"


class FakeFrame:
    def __init__(self, metadata: FakeMetadata, color: tuple[int, int, int]) -> None:
        self.metadata = metadata
        self.image = Image.new("RGB", (metadata.width, metadata.height), color)

    def save_bmp(self, output_path) -> FakeMetadata:
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.image.save(path, format="BMP")
        return self.metadata


class FakeCaptureProvider:
    def __init__(self, *frames: FakeFrame, capture_error: BaseException | None = None):
        self.frames = list(frames)
        self.capture_error = capture_error
        self.capture_calls: list[dict] = []
        self.close_calls = 0
        self.close_error: BaseException | None = None

    def capture(self, *, timeout: float, after_sequence: int | None = None):
        self.capture_calls.append(
            {"timeout": timeout, "after_sequence": after_sequence}
        )
        if self.capture_error is not None:
            raise self.capture_error
        if not self.frames:
            raise AssertionError("no fake capture frame available")
        frame = self.frames.pop(0)
        if after_sequence is not None and frame.metadata.sequence <= after_sequence:
            raise AssertionError("fake frame is not newer than the baseline")
        return frame

    def close(self) -> None:
        self.close_calls += 1
        if self.close_error is not None:
            raise self.close_error


class FakeSerial:
    def __init__(self) -> None:
        self.started = False
        self.start_calls = 0
        self.stop_calls = 0
        self.send_calls: list[tuple[str, dict]] = []
        self.lines = ["boot", "ready"]
        self.start_error: BaseException | None = None
        self.send_error: BaseException | None = None
        self.send_result = None
        self.send_result_request: str | None = None
        self.stop_error: BaseException | None = None
        self.shell_lines: list[str] = []
        self.events: list[dict] = []

    def start(self) -> None:
        self.start_calls += 1
        self.started = True
        if self.start_error is not None:
            raise self.start_error

    def send(self, content: str, **kwargs):
        self.send_calls.append((content, kwargs))
        if self.send_error is not None:
            raise self.send_error
        if self.send_result is not None and (
            self.send_result_request is None
            or kwargs.get("request") == self.send_result_request
        ):
            return self.send_result
        raw = {
            "type": kwargs.get("expected_type"),
            "status": kwargs.get("expected_status"),
        }
        if (
            kwargs.get("request") == "test_session"
            and kwargs.get("expected_status") == "active"
        ):
            raw["lease_seconds"] = 86400
        return SimpleNamespace(
            request=kwargs.get("request") or "command",
            status=kwargs.get("expected_status") or "ok",
            raw=raw,
            lines=[],
            start_index=0,
        )

    def lines_since(self, start_index: int) -> list[str]:
        return self.lines[start_index:]

    @property
    def event_count(self) -> int:
        return len(self.events)

    def events_since(self, start_index: int) -> list[dict]:
        return [dict(event) for event in self.events[start_index:]]

    def write_shell_line(self, line: str) -> None:
        self.shell_lines.append(line)
        if "TOP5STEP:SCREENSHOT_CAPTURE_FILE:" in line:
            self.events.append(
                {
                    "protocol": "w30_test_bridge",
                    "version": 1,
                    "type": "command_result",
                    "request": "screenshot_capture_file",
                    "seq": None,
                    "status": "accepted",
                }
            )

    def stop(self) -> None:
        self.stop_calls += 1
        self.started = False
        if self.stop_error is not None:
            raise self.stop_error


class RealDeviceSessionTest(unittest.TestCase):
    def _session(
        self,
        serial: FakeSerial,
        provider: FakeCaptureProvider,
        root: str,
        *,
        capture_timeout: float = 1.5,
    ):
        return RealDeviceSession(
            evidence_dir=root,
            serial_session=serial,
            capture_provider=provider,
            startup_timeout=4.0,
            cmd_timeout=3.0,
            capture_timeout=capture_timeout,
        )

    def test_defaults_use_environment_and_evidence_serial_directory(self) -> None:
        fake_serial = FakeSerial()
        fake_provider = FakeCaptureProvider()
        with tempfile.TemporaryDirectory() as root, mock.patch.dict(
            "os.environ",
            {
                "W30_HARDWARE_PORT": "COM42",
                "W30_HARDWARE_BAUDRATE": "921600",
                "W30_HARDWARE_TRANSPORT": "serial",
                "W30_HARDWARE_CAPTURE_PROVIDER": "mtp",
            },
        ), mock.patch.object(
            real_device, "HardwareSerialSession", return_value=fake_serial
        ) as serial_class, mock.patch.object(
            real_device, "MtpCaptureProvider", return_value=fake_provider
        ) as provider_class:
            session = RealDeviceSession(evidence_dir=root, cmd_timeout=7.0)

        serial_class.assert_called_once_with(
            port="COM42",
            baudrate=921600,
            log_dir=Path(root) / "serial",
            cmd_timeout=7.0,
            transport=None,
            dtr=False,
            rts=False,
        )
        provider_class.assert_called_once_with(fake_serial)
        self.assertIs(session.serial_session, fake_serial)
        self.assertIs(session.capture_provider, fake_provider)
        self.assertEqual(session.capture_timeout, 12.0)

    def test_supercom_transport_is_selected_by_environment(self) -> None:
        fake_serial = FakeSerial()
        fake_provider = FakeCaptureProvider()
        fake_transport = object()
        with tempfile.TemporaryDirectory() as root, mock.patch.dict(
            "os.environ",
            {"W30_HARDWARE_PORT": "COM7", "W30_HARDWARE_TRANSPORT": "supercom"},
        ), mock.patch.object(
            real_device, "SuperComPipeTransport", return_value=fake_transport
        ) as transport_class, mock.patch.object(
            real_device, "HardwareSerialSession", return_value=fake_serial
        ) as serial_class, mock.patch.object(
            real_device, "MtpCaptureProvider", return_value=fake_provider
        ):
            RealDeviceSession(evidence_dir=root)

        transport_class.assert_called_once_with("COM7")
        self.assertIs(serial_class.call_args.kwargs["transport"], fake_transport)

    def test_missing_hardware_port_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as root, mock.patch.dict(
            "os.environ",
            {"W30_HARDWARE_TRANSPORT": "supercom"},
            clear=True,
        ):
            with self.assertRaisesRegex(
                ValueError,
                "W30_HARDWARE_PORT must be explicitly configured",
            ):
                RealDeviceSession(evidence_dir=root)

    def test_ble_capture_provider_accepts_explicit_address_override(
        self,
    ) -> None:
        fake_serial = FakeSerial()
        fake_provider = FakeCaptureProvider()
        with tempfile.TemporaryDirectory() as root, mock.patch.dict(
            "os.environ",
            {
                "W30_HARDWARE_CAPTURE_PROVIDER": "ble",
                "W30_HARDWARE_BLE_ADDRESS": "42:74:DC:C8:0A:02",
                "W30_HARDWARE_BLE_SCAN_TIMEOUT": "2.5",
            },
            clear=True,
        ), mock.patch.object(
            real_device, "BleCaptureProvider", return_value=fake_provider
        ) as provider_class:
            session = RealDeviceSession(
                evidence_dir=root,
                serial_session=fake_serial,
            )

        provider_class.assert_called_once_with(
            "42:74:DC:C8:0A:02",
            serial_session=fake_serial,
            scan_timeout=2.5,
        )
        self.assertIs(session.capture_provider, fake_provider)
        self.assertEqual(session.capture_provider_name, "ble")
        self.assertEqual(session.capture_timeout, 180.0)

    def test_ble_capture_provider_discovers_when_address_is_unset(self) -> None:
        fake_serial = FakeSerial()
        fake_provider = FakeCaptureProvider()
        with tempfile.TemporaryDirectory() as root, mock.patch.dict(
            "os.environ",
            {"W30_HARDWARE_CAPTURE_PROVIDER": "ble"},
            clear=True,
        ), mock.patch.object(
            real_device, "BleCaptureProvider", return_value=fake_provider
        ) as provider_class:
            session = RealDeviceSession(
                evidence_dir=root,
                serial_session=fake_serial,
            )

        provider_class.assert_called_once_with(
            None,
            serial_session=fake_serial,
            scan_timeout=15.0,
        )
        self.assertIs(session.capture_provider, fake_provider)
        self.assertEqual(session.capture_provider_name, "ble")

    def test_unknown_capture_provider_is_rejected(self) -> None:
        with mock.patch.dict(
            "os.environ",
            {"W30_HARDWARE_CAPTURE_PROVIDER": "phonecam"},
            clear=True,
        ):
            with self.assertRaisesRegex(
                ValueError,
                "W30_HARDWARE_CAPTURE_PROVIDER must be mtp or ble",
            ):
                RealDeviceSession(serial_session=FakeSerial())

    def test_start_uses_batch_test_session_without_mutating_it(self) -> None:
        serial = FakeSerial()
        camera = FakeCaptureProvider()
        with tempfile.TemporaryDirectory() as root:
            session = self._session(serial, camera, root)
            with mock.patch.object(real_device.time, "time_ns", return_value=123456789):
                session.start()
                session.start()

            self.assertTrue(session.started)
            self.assertEqual(serial.start_calls, 1)
            self.assertEqual(len(serial.send_calls), 1)
            command, kwargs = serial.send_calls[0]
            self.assertEqual(command, ":GUI_PING:123456789")
            self.assertEqual(
                kwargs,
                {
                    "request": "gui_ping",
                    "timeout": 4.0,
                    "expected_type": "gui_ack",
                    "expected_status": "processed",
                },
            )
            self.assertEqual(camera.capture_calls, [])
            self.assertEqual(session.lines_since(1), ["ready"])
            session.stop()
            self.assertEqual(len(serial.send_calls), 1)
            self.assertEqual(serial.stop_calls, 1)
            self.assertEqual(camera.close_calls, 1)

    def test_status_query_is_read_only_and_reports_remaining_lease(self) -> None:
        serial = FakeSerial()
        serial.send_result = SimpleNamespace(
            request="test_session",
            status="active",
            raw={
                "type": "test_session",
                "request": "test_session",
                "status": "active",
                "lease_seconds": 86395,
            },
            lines=[],
            start_index=0,
        )
        with tempfile.TemporaryDirectory() as root:
            status = query_test_session_status(
                evidence_dir=root,
                serial_session=serial,
            )

        self.assertTrue(status.active)
        self.assertEqual(status.lease_seconds, 86395)
        self.assertEqual(len(serial.send_calls), 1)
        self.assertEqual(serial.send_calls[0][0], ":TEST_SESSION:STATUS")
        self.assertEqual(serial.start_calls, 1)
        self.assertEqual(serial.stop_calls, 1)

    def test_explicit_capture_provider_is_supported(self) -> None:
        serial = FakeSerial()
        provider = FakeCaptureProvider(FakeFrame(FakeMetadata(sequence=1), (1, 2, 3)))
        with tempfile.TemporaryDirectory() as root:
            session = RealDeviceSession(
                evidence_dir=root,
                serial_session=serial,
                capture_provider=provider,
            )
            self.assertIs(session.capture_provider, provider)

    def test_capture_requires_new_frame_and_writes_generic_metadata_and_sidecar(self) -> None:
        serial = FakeSerial()
        camera = FakeCaptureProvider(
            FakeFrame(
                FakeMetadata(sequence=21, timestamp=456.25, width=4, height=3),
                (10, 20, 30),
            ),
        )
        with tempfile.TemporaryDirectory() as root:
            session = self._session(serial, camera, root)
            session.start()
            evidence_path = Path(root) / "step_01.bmp"

            self.assertTrue(session.capture_screenshot(evidence_path))

            raw_path = Path(root) / "step_01_capture_original.bmp"
            self.assertTrue(evidence_path.is_file())
            self.assertTrue(raw_path.is_file())
            self.assertEqual(evidence_path.read_bytes(), raw_path.read_bytes())
            self.assertEqual(
                camera.capture_calls[-1],
                {"timeout": 1.5, "after_sequence": 0},
            )
            self.assertEqual(
                session.last_capture_metadata,
                {
                    "sequence": 21,
                    "timestamp": 456.25,
                    "width": 4,
                    "height": 3,
                    "size": [4, 3],
                    "pixel_format": "BGR24",
                    "format": "BGR24",
                    "raw_path": str(raw_path.resolve()),
                    "evidence_path": str(evidence_path.resolve()),
                    "data_size": 18,
                    "stride": 9,
                    "payload_crc32": 0x1234ABCD,
                    "chunk_bytes": 384,
                    "chunks": 1608,
                    "capture_duration_ms": 17,
                    "device_uptime_ms": 123_456,
                    "source": "watch_display",
                    "transport": "hardware_serial",
                    "encoding": "base64",
                    "pixel_source": "vde_lcd_composite",
                },
            )
            session.stop()

    def test_configured_capture_timeout_reaches_first_screenshot(self) -> None:
        serial = FakeSerial()
        provider = FakeCaptureProvider(
            FakeFrame(FakeMetadata(sequence=31), (4, 5, 6)),
        )
        with tempfile.TemporaryDirectory() as root:
            session = self._session(
                serial, provider, root, capture_timeout=12.0
            )
            session.start()
            session.capture_screenshot(Path(root) / "step_00.bmp")
            session.stop()

        self.assertEqual(
            provider.capture_calls,
            [{"timeout": 12.0, "after_sequence": 0}],
        )

    def test_watch_capture_provider_integration_uses_borrowed_serial_and_metadata(self) -> None:
        payload = bytes(
            (
                0x00,
                0x00,
                0xFF,
                0x00,
                0xFF,
                0x00,
                0xFF,
                0x00,
                0x00,
                0xFF,
                0xFF,
                0xFF,
            )
        )

        class SessionSerial(FakeSerialSession):
            def __init__(self) -> None:
                super().__init__(
                    lambda sequence: make_stream(payload, sequence=sequence)
                )
                self.start_calls = 0
                self.lines: list[str] = []

            def start(self) -> None:
                self.start_calls += 1

            def send(self, content: str, **kwargs):
                if kwargs.get("request") == "gui_ping":
                    self.send_calls.append((content, kwargs))
                    return SimpleNamespace(
                        request="gui_ping",
                        status="processed",
                        raw={"type": "gui_ack", "status": "processed"},
                        lines=[],
                        start_index=0,
                    )
                if kwargs.get("request") == "test_session":
                    self.send_calls.append((content, kwargs))
                    active = kwargs.get("expected_status") == "active"
                    return SimpleNamespace(
                        request="test_session",
                        status=kwargs.get("expected_status"),
                        raw={
                            "type": "test_session",
                            "status": kwargs.get("expected_status"),
                            "lease_seconds": 86400 if active else 0,
                        },
                        lines=[],
                        start_index=0,
                    )
                return super().send(content, **kwargs)

            def lines_since(self, start_index: int) -> list[str]:
                return self.lines[start_index:]

        serial = SessionSerial()
        provider = WatchCaptureProvider(serial, sequence_start=100)
        with tempfile.TemporaryDirectory() as root:
            session = RealDeviceSession(
                evidence_dir=root,
                serial_session=serial,
                capture_provider=provider,
                capture_timeout=12.0,
            )
            session.start()
            self.assertTrue(
                session.capture_screenshot(Path(root) / "watch_step.bmp")
            )
            session.stop()

            self.assertTrue((Path(root) / "watch_step.bmp").is_file())
            self.assertTrue(
                (Path(root) / "watch_step_capture_original.bmp").is_file()
            )
        self.assertEqual(serial.start_calls, 1)
        self.assertEqual(serial.stop_calls, 1)
        self.assertTrue(provider.closed)
        self.assertEqual(session.last_capture_metadata["source"], "watch_display")
        self.assertEqual(
            session.last_capture_metadata["transport"], "hardware_serial"
        )
        self.assertEqual(session.last_capture_metadata["chunks"], 3)
        self.assertEqual(session.last_capture_metadata["capture_duration_ms"], 17)
        self.assertEqual(
            session.last_capture_metadata["pixel_source"],
            "vde_lcd_composite",
        )

    def test_mtp_provider_integration_uses_one_shared_serial_session(self) -> None:
        serial = FakeSerial()
        provider = MtpCaptureProvider(
            serial,
            sequence_start=200,
            usb_timeout=0.1,
            mtp_timeout=0.1,
            mtp_system=FakeMtpSystem(),
        )
        with tempfile.TemporaryDirectory() as root:
            session = RealDeviceSession(
                evidence_dir=root,
                serial_session=serial,
                capture_provider=provider,
                capture_timeout=0.01,
            )
            session.start()
            self.assertTrue(session.capture_screenshot(Path(root) / "mtp_step.bmp"))
            session.stop()

            self.assertTrue((Path(root) / "mtp_step.bmp").is_file())
            self.assertTrue(
                (Path(root) / "mtp_step_capture_original.bmp").is_file()
            )

        self.assertEqual(serial.start_calls, 1)
        self.assertEqual(serial.stop_calls, 1)
        self.assertTrue(provider.closed)
        self.assertEqual(
            serial.shell_lines,
            [
                "dal_usb close",
                'srv_quick_cmd send "TOP5STEP:SCREENSHOT_CAPTURE_FILE:200;"',
                "dal_usb open",
            ],
        )
        self.assertEqual(session.last_capture_metadata["sequence"], 200)
        self.assertEqual(session.last_capture_metadata["transport"], "mtp")
        self.assertEqual(session.last_capture_metadata["encoding"], "bmp")
        self.assertEqual(
            session.last_capture_metadata["mtp_file_name"],
            "agent_capture_200.bmp",
        )
        self.assertFalse(session.last_capture_metadata["receipt_verified"])
        self.assertEqual(session.last_capture_metadata["file_size"], 618_518)

    def test_business_command_explicitly_waits_for_accepted(self) -> None:
        serial = FakeSerial()
        camera = FakeCaptureProvider(FakeFrame(FakeMetadata(sequence=1), (1, 2, 3)))
        with tempfile.TemporaryDirectory() as root:
            session = self._session(serial, camera, root)
            session.start()
            serial.send_calls.clear()

            result = session.send(":TP_CLICK:10,20,0", request="tp_click")

            self.assertEqual(result.status, "accepted")
            self.assertEqual(
                serial.send_calls,
                [
                    (
                        ":TP_CLICK:10,20,0",
                        {
                            "request": "tp_click",
                            "timeout": None,
                            "expected_type": "command_result",
                            "expected_status": "accepted",
                        },
                    )
                ],
            )

            serial.send_calls.clear()
            session.send(":GUI_TREE:7", request="gui_tree")
            self.assertIsNone(serial.send_calls[0][1]["expected_type"])
            self.assertIsNone(serial.send_calls[0][1]["expected_status"])
            session.stop()

    def test_stop_attempts_both_sides_after_error_and_is_idempotent(self) -> None:
        serial = FakeSerial()
        camera = FakeCaptureProvider(FakeFrame(FakeMetadata(sequence=1), (1, 2, 3)))
        serial.stop_error = RuntimeError("serial close failed")
        with tempfile.TemporaryDirectory() as root:
            session = self._session(serial, camera, root)
            session.start()

            with self.assertRaisesRegex(RuntimeError, "serial close failed"):
                session.stop()
            session.stop()

        self.assertEqual(serial.stop_calls, 1)
        self.assertEqual(camera.close_calls, 1)
        self.assertFalse(session.started)

    def test_start_failure_cleans_up_serial_and_capture_provider(self) -> None:
        for failure_point in ("serial", "handshake"):
            with self.subTest(failure_point=failure_point), tempfile.TemporaryDirectory() as root:
                serial = FakeSerial()
                camera = FakeCaptureProvider(FakeFrame(FakeMetadata(sequence=1), (1, 2, 3)))
                if failure_point == "serial":
                    serial.send_error = RuntimeError("ping failed")
                elif failure_point == "handshake":
                    serial.send_result_request = "gui_ping"
                    serial.send_result = SimpleNamespace(
                        request="gui_ping",
                        status="rejected",
                        raw={"type": "command_result", "status": "rejected"},
                        lines=[],
                        start_index=0,
                    )
                session = self._session(serial, camera, root)

                expected_message = {
                    "serial": "ping failed",
                    "handshake": "GUI_PING handshake",
                }[failure_point]
                with self.assertRaisesRegex(RuntimeError, expected_message):
                    session.start()

                self.assertEqual(serial.stop_calls, 1)
                self.assertEqual(camera.close_calls, 1)
                self.assertFalse(session.started)
                session.stop()
                self.assertEqual(serial.stop_calls, 1)
                self.assertEqual(camera.close_calls, 1)

    def test_first_capture_failure_uses_initial_sequence_without_writing_evidence(
        self,
    ) -> None:
        serial = FakeSerial()
        camera = FakeCaptureProvider(capture_error=RuntimeError("capture failed"))
        with tempfile.TemporaryDirectory() as root:
            session = self._session(serial, camera, root)
            session.start()
            evidence_path = Path(root) / "step_00.bmp"

            with self.assertRaisesRegex(RuntimeError, "capture failed"):
                session.capture_screenshot(evidence_path)

            self.assertEqual(
                camera.capture_calls,
                [{"timeout": 1.5, "after_sequence": 0}],
            )
            self.assertFalse(evidence_path.exists())
            self.assertTrue(session.started)
            session.stop()


if __name__ == "__main__":
    unittest.main()
