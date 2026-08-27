from __future__ import annotations

import base64
import json
import subprocess
import struct
import tempfile
import unittest
import zlib
from collections import deque
from pathlib import Path
from unittest import mock

from agent_loop_system.tools.hardware_serial import SHELL_WRITE_BURST_LIMIT
from agent_loop_system.tools.mtp_screenshot import (
    MAX_SHELL_SAFE_CAPTURE_SEQUENCE,
    MtpCaptureProvider,
    MtpScreenshotCommandError,
    MtpScreenshotError,
    MtpScreenshotDeviceError,
    MtpScreenshotTimeoutError,
    MtpScreenshotValidationError,
    WindowsMtpSystem,
    capture_filename,
    capture_mtp_screenshot,
    validate_bmp,
)


def make_bmp(path: Path, *, width: int = 410, height: int = 502) -> int:
    row_bytes = width * 3
    stride = (row_bytes + 3) & ~3
    image_size = stride * height
    file_size = 54 + image_size
    header = struct.pack("<2sIHHI", b"BM", file_size, 0, 0, 54)
    info = struct.pack(
        "<IiiHHIIiiII",
        40,
        width,
        -height,
        1,
        24,
        0,
        image_size,
        2835,
        2835,
        0,
        0,
    )
    crc32 = 0
    with path.open("wb") as stream:
        stream.write(header)
        stream.write(info)
        for row in range(height):
            pixels = bytes(((row + column) % 251 for column in range(row_bytes)))
            crc32 = zlib.crc32(pixels, crc32)
            stream.write(pixels)
            stream.write(b"\x00" * (stride - row_bytes))
    return crc32 & 0xFFFFFFFF


class FakeTransport:
    def __init__(self, reads: list[bytes] | None = None) -> None:
        self.reads = deque(reads or [])
        self.writes: list[bytes] = []
        self.opened = False
        self.closed = False

    def open(self) -> None:
        self.opened = True

    def read(self, _size: int) -> bytes:
        return self.reads.popleft() if self.reads else b""

    def write(self, data: bytes) -> int:
        self.writes.append(data)
        return len(data)

    def close(self) -> None:
        self.closed = True


class FakeMtpSystem:
    def __init__(
        self, *, valid_bmp: bool = True, width: int = 410, height: int = 502
    ) -> None:
        self.usb_states: list[bool] = []
        self.copy_calls: list[str] = []
        self.valid_bmp = valid_bmp
        self.width = width
        self.height = height

    def wait_for_usb(self, *, present: bool, timeout: float) -> None:
        self.usb_states.append(present)

    def inspect_usb_devices(self, *, timeout: float):
        return [{
            "instance_id": "USB\\VID_301A&PID_6808\\TEST",
            "status": "OK",
            "friendly_name": "ZORA",
            "class": "WPD",
        }]

    def probe_namespace(self, *, timeout: float):
        return {"device": "ZORA", "storage": "storage", "folder": "download"}

    def copy_capture(
        self, destination_dir: Path, *, file_name: str, timeout: float
    ) -> Path:
        self.copy_calls.append(file_name)
        target = destination_dir / file_name
        if self.valid_bmp:
            make_bmp(target, width=self.width, height=self.height)
        else:
            target.write_bytes(b"old-or-broken")
        return target


class RetryOpenMtpSystem(FakeMtpSystem):
    def __init__(self) -> None:
        super().__init__()
        self.present_attempts = 0

    def wait_for_usb(self, *, present: bool, timeout: float) -> None:
        self.usb_states.append(present)
        if present:
            self.present_attempts += 1
            if self.present_attempts == 2:
                raise MtpScreenshotTimeoutError("first USB reopen was missed")


class RetryCopyMtpSystem(FakeMtpSystem):
    def __init__(self) -> None:
        super().__init__()
        self.copy_attempts: list[str] = []

    def copy_capture(
        self, destination_dir: Path, *, file_name: str, timeout: float
    ) -> Path:
        self.copy_attempts.append(file_name)
        if len(self.copy_attempts) == 1:
            raise MtpScreenshotError("Windows MTP operation failed: MTP capture not found")
        return super().copy_capture(
            destination_dir, file_name=file_name, timeout=timeout
        )


class FakeBorrowedSession:
    def __init__(self) -> None:
        self.started = True
        self.events: list[dict] = []
        self.shell_lines: list[str] = []
        self.stop_calls = 0

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


class MtpScreenshotTest(unittest.TestCase):
    def test_windows_mtp_copy_waits_for_shell_namespace_discovery(self) -> None:
        captured: dict[str, object] = {}

        def runner(argv, **kwargs):
            captured["argv"] = argv
            captured["script"] = base64.b64decode(argv[-1]).decode("utf-16le")
            captured["timeout"] = kwargs["timeout"]
            env = kwargs["env"]
            target = Path(env["WATCH_MTP_DESTINATION"]) / env["WATCH_MTP_FILE"]
            make_bmp(target)
            return subprocess.CompletedProcess(argv, 0, "", "")

        system = WindowsMtpSystem(runner=runner)
        with tempfile.TemporaryDirectory() as temporary_dir:
            target = system.copy_capture(
                Path(temporary_dir), file_name="agent_capture_9.bmp", timeout=0.1
            )

        self.assertEqual(target.name, "agent_capture_9.bmp")
        self.assertIn("$discoveryDeadline", str(captured["script"]))
        self.assertIn("Start-Sleep -Milliseconds 500", str(captured["script"]))
        self.assertIn(
            "$ProgressPreference = 'SilentlyContinue'",
            str(captured["script"]),
        )
        self.assertIn("[Console]::OutputEncoding", str(captured["script"]))
        self.assertIn("-NoLogo", captured["argv"])
        self.assertIn("-OutputFormat", captured["argv"])
        self.assertIn("Text", captured["argv"])
        self.assertEqual(captured["timeout"], 10.2)

    def test_windows_mtp_error_filters_progress_clixml(self) -> None:
        clixml = """#< CLIXML
<Objs Version="1.1.0.1" xmlns="http://schemas.microsoft.com/powershell/2004/04">
  <Obj S="progress" RefId="0"><MS><S N="Activity">Preparing modules</S></MS></Obj>
  <S S="Error">USB state did not reach the requested value_x000D__x000A_</S>
</Objs>"""

        def runner(argv, **_kwargs):
            return subprocess.CompletedProcess(argv, 2, "", clixml)

        system = WindowsMtpSystem(runner=runner)
        with self.assertRaises(MtpScreenshotError) as context:
            system._run("exit 2", timeout=0.1)

        message = str(context.exception)
        self.assertIn("USB state did not reach the requested value", message)
        self.assertNotIn("#< CLIXML", message)
        self.assertNotIn("Preparing modules", message)

    def test_windows_mtp_error_hides_progress_only_clixml(self) -> None:
        clixml = """#< CLIXML
<Objs Version="1.1.0.1" xmlns="http://schemas.microsoft.com/powershell/2004/04">
  <Obj S="progress" RefId="0"><MS><S N="Activity">Preparing modules</S></MS></Obj>
</Objs>"""

        def runner(argv, **_kwargs):
            return subprocess.CompletedProcess(argv, 2, "", clixml)

        system = WindowsMtpSystem(runner=runner)
        with self.assertRaises(MtpScreenshotError) as context:
            system._run("exit 2", timeout=0.1)

        message = str(context.exception)
        self.assertIn("PowerShell exited with code 2 without diagnostics", message)
        self.assertNotIn("#< CLIXML", message)
        self.assertNotIn("Preparing modules", message)

    def test_capture_runs_full_flow_and_validates_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            expected = root / "expected.bmp"
            crc32 = make_bmp(expected)
            size = expected.stat().st_size
            expected.unlink()
            receipt = {
                "protocol": "w30_test_bridge",
                "version": 1,
                "type": "screenshot_end",
                "request": "screenshot_capture_file",
                "seq": 123,
                "status": "complete",
                "width": 410,
                "height": 502,
                "transport": "mtp",
                "encoding": "bmp",
                "file_size": size,
                "path": "download/agent_capture_123.bmp",
                "crc32": f"{crc32:08x}",
                "device_uptime_ms": 4567,
                "capture_duration_ms": 89,
            }
            transport = FakeTransport(
                [(json.dumps(receipt, separators=(",", ":")) + "\n").encode()]
            )
            mtp = FakeMtpSystem()
            output = root / "capture.bmp"

            result = capture_mtp_screenshot(
                output,
                sequence=123,
                capture_timeout=0.1,
                usb_timeout=0.1,
                mtp_timeout=0.1,
                mtp_system=mtp,
                transport_factory=lambda: transport,
            )

            self.assertTrue(output.is_file())
            self.assertTrue(result.receipt_verified)
            self.assertEqual(result.payload_crc32, crc32)
            self.assertEqual(result.device_uptime_ms, 4567)
            self.assertEqual(result.capture_duration_ms, 89)
            self.assertEqual(mtp.usb_states, [True, False, True])
            self.assertEqual(
                transport.writes,
                [
                    b"dal_usb close\r\n",
                    b'srv_quick_cmd send "TOP5STEP:SCREENSHOT_CAPTURE_FILE:123;"\r\n',
                    b"dal_usb open\r\n",
                ],
            )
            self.assertTrue(transport.closed)

    def test_capture_retries_idempotent_usb_open_once(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            expected = root / "expected.bmp"
            crc32 = make_bmp(expected)
            size = expected.stat().st_size
            expected.unlink()
            receipt = {
                "protocol": "w30_test_bridge",
                "version": 1,
                "type": "screenshot_end",
                "request": "screenshot_capture_file",
                "seq": 124,
                "status": "complete",
                "width": 410,
                "height": 502,
                "transport": "mtp",
                "encoding": "bmp",
                "file_size": size,
                "path": "download/agent_capture_124.bmp",
                "crc32": f"{crc32:08x}",
            }
            transport = FakeTransport(
                [(json.dumps(receipt, separators=(",", ":")) + "\n").encode()]
            )
            mtp = RetryOpenMtpSystem()

            capture_mtp_screenshot(
                root / "capture.bmp",
                sequence=124,
                capture_timeout=0.1,
                usb_timeout=0.1,
                mtp_timeout=0.1,
                mtp_system=mtp,
                transport_factory=lambda: transport,
            )

            self.assertEqual(mtp.usb_states, [True, False, True, True])
            self.assertEqual(transport.writes[-2:], [b"dal_usb open\r\n"] * 2)
            self.assertTrue(transport.closed)

    def test_capture_retries_same_mtp_filename_after_namespace_miss(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            expected = root / "expected.bmp"
            crc32 = make_bmp(expected)
            size = expected.stat().st_size
            expected.unlink()
            receipt = {
                "protocol": "w30_test_bridge",
                "version": 1,
                "type": "screenshot_end",
                "request": "screenshot_capture_file",
                "seq": 125,
                "status": "complete",
                "width": 410,
                "height": 502,
                "transport": "mtp",
                "encoding": "bmp",
                "file_size": size,
                "path": "download/agent_capture_125.bmp",
                "crc32": f"{crc32:08x}",
            }
            transport = FakeTransport(
                [(json.dumps(receipt, separators=(",", ":")) + "\n").encode()]
            )
            mtp = RetryCopyMtpSystem()

            capture_mtp_screenshot(
                root / "capture.bmp",
                sequence=125,
                capture_timeout=0.1,
                usb_timeout=0.1,
                mtp_timeout=0.1,
                mtp_system=mtp,
                transport_factory=lambda: transport,
            )

            self.assertEqual(
                mtp.copy_attempts,
                ["agent_capture_125.bmp", "agent_capture_125.bmp"],
            )
            self.assertTrue(transport.closed)

    def test_capture_filename_keeps_full_uint32_sequence(self) -> None:
        self.assertEqual(capture_filename(1), "agent_capture_1.bmp")
        self.assertEqual(
            capture_filename(0xFFFFFFFF), "agent_capture_4294967295.bmp"
        )
        with self.assertRaises(ValueError):
            capture_filename(0)

    def test_capture_accepts_largest_shell_safe_sequence_atomically(self) -> None:
        accepted = {
            "protocol": "w30_test_bridge",
            "version": 1,
            "type": "command_result",
            "request": "screenshot_capture_file",
            "seq": None,
            "status": "accepted",
        }
        sequence = MAX_SHELL_SAFE_CAPTURE_SEQUENCE
        with tempfile.TemporaryDirectory() as root:
            transport = FakeTransport([(json.dumps(accepted) + "\n").encode()])
            mtp = FakeMtpSystem()
            result = capture_mtp_screenshot(
                Path(root) / "capture.bmp",
                sequence=sequence,
                capture_timeout=0.001,
                usb_timeout=0.1,
                mtp_timeout=0.1,
                mtp_system=mtp,
                transport_factory=lambda: transport,
            )

        capture_wire = (
            'srv_quick_cmd send "TOP5STEP:SCREENSHOT_CAPTURE_FILE:'
            f'{sequence};"\r\n'
        ).encode("utf-8")
        self.assertEqual(len(capture_wire), SHELL_WRITE_BURST_LIMIT)
        self.assertEqual(transport.writes[1:-1], [capture_wire])
        self.assertEqual(result.sequence, sequence)
        self.assertEqual(mtp.copy_calls, [capture_filename(sequence)])

    def test_capture_rejects_shell_unsafe_sequence_before_transport_or_usb(self) -> None:
        transport_calls: list[bool] = []
        mtp = FakeMtpSystem()

        def transport_factory():
            transport_calls.append(True)
            return FakeTransport()

        with tempfile.TemporaryDirectory() as root:
            with self.assertRaisesRegex(
                MtpScreenshotValidationError,
                "exceeds the shell-safe maximum 9999999",
            ):
                capture_mtp_screenshot(
                    Path(root) / "capture.bmp",
                    sequence=MAX_SHELL_SAFE_CAPTURE_SEQUENCE + 1,
                    capture_timeout=0.1,
                    usb_timeout=0.1,
                    mtp_timeout=0.1,
                    mtp_system=mtp,
                    transport_factory=transport_factory,
                )

        self.assertEqual(transport_calls, [])
        self.assertEqual(mtp.usb_states, [])

    def test_capture_without_acceptance_stops_before_mtp_lookup(self) -> None:
        transport = FakeTransport()
        mtp = FakeMtpSystem()
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaisesRegex(MtpScreenshotCommandError, "not accepted"):
                capture_mtp_screenshot(
                    Path(root) / "capture.bmp",
                    sequence=MAX_SHELL_SAFE_CAPTURE_SEQUENCE,
                    capture_timeout=0.001,
                    usb_timeout=0.1,
                    mtp_timeout=0.1,
                    mtp_system=mtp,
                    transport_factory=lambda: transport,
                )

        self.assertEqual(mtp.copy_calls, [])
        self.assertEqual(mtp.usb_states, [True, False, True])
        self.assertEqual(transport.writes[-1], b"dal_usb open\r\n")
        self.assertTrue(transport.closed)

    def test_device_error_reopens_usb(self) -> None:
        receipt = {
            "protocol": "w30_test_bridge",
            "type": "screenshot_end",
            "request": "screenshot_capture_file",
            "seq": 7,
            "status": "busy",
            "reason": "capture_busy",
        }
        transport = FakeTransport([(json.dumps(receipt) + "\n").encode()])
        mtp = FakeMtpSystem()
        with tempfile.TemporaryDirectory() as temporary_dir:
            with self.assertRaisesRegex(MtpScreenshotDeviceError, "capture_busy"):
                capture_mtp_screenshot(
                    Path(temporary_dir) / "capture.bmp",
                    sequence=7,
                    capture_timeout=0.1,
                    usb_timeout=0.1,
                    mtp_timeout=0.1,
                    mtp_system=mtp,
                    transport_factory=lambda: transport,
                )
        self.assertEqual(transport.writes[-1], b"dal_usb open\r\n")
        self.assertEqual(mtp.usb_states, [True, False, True])
        self.assertTrue(transport.closed)

    def test_invalid_download_is_rejected(self) -> None:
        receipt = {
            "protocol": "w30_test_bridge",
            "type": "screenshot_end",
            "request": "screenshot_capture_file",
            "seq": 8,
            "status": "complete",
        }
        transport = FakeTransport([(json.dumps(receipt) + "\n").encode()])
        mtp = FakeMtpSystem(valid_bmp=False)
        with tempfile.TemporaryDirectory() as temporary_dir:
            output = Path(temporary_dir) / "capture.bmp"
            with self.assertRaises(MtpScreenshotValidationError):
                capture_mtp_screenshot(
                    output,
                    sequence=8,
                    capture_timeout=0.1,
                    usb_timeout=0.1,
                    mtp_timeout=0.1,
                    mtp_system=mtp,
                    transport_factory=lambda: transport,
                )
            self.assertFalse(output.exists())

    def test_validate_bmp_rejects_bottom_up_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            path = Path(temporary_dir) / "bottom-up.bmp"
            make_bmp(path)
            data = bytearray(path.read_bytes())
            struct.pack_into("<i", data, 22, 502)
            path.write_bytes(data)
            with self.assertRaisesRegex(MtpScreenshotValidationError, "top-down"):
                validate_bmp(path)


class MtpCaptureProviderTest(unittest.TestCase):
    def test_provider_default_sequence_stays_within_shell_safe_range(self) -> None:
        serial = FakeBorrowedSession()
        mtp = FakeMtpSystem()
        with mock.patch(
            "agent_loop_system.tools.mtp_screenshot.time.time_ns",
            return_value=MAX_SHELL_SAFE_CAPTURE_SEQUENCE - 1,
        ):
            provider = MtpCaptureProvider(
                serial,
                usb_timeout=0.1,
                mtp_timeout=0.1,
                mtp_system=mtp,
            )
        frame = provider.capture(timeout=0.01)

        self.assertEqual(
            frame.metadata.sequence,
            MAX_SHELL_SAFE_CAPTURE_SEQUENCE,
        )
        capture_line = serial.shell_lines[1]
        self.assertEqual(
            len((capture_line + "\r\n").encode("utf-8")),
            SHELL_WRITE_BURST_LIMIT,
        )

    def test_provider_rejects_shell_unsafe_sequence_start(self) -> None:
        with self.assertRaisesRegex(
            MtpScreenshotValidationError,
            "exceeds the shell-safe maximum 9999999",
        ):
            MtpCaptureProvider(
                FakeBorrowedSession(),
                sequence_start=MAX_SHELL_SAFE_CAPTURE_SEQUENCE + 1,
            )

    def test_provider_uses_6204_project_geometry(self) -> None:
        serial = FakeBorrowedSession()
        mtp = FakeMtpSystem(width=466, height=466)
        with mock.patch.dict(
            "os.environ", {"W30_HARDWARE_PROJECT": "6204_W5230"}
        ):
            provider = MtpCaptureProvider(
                serial,
                sequence_start=6204,
                usb_timeout=0.1,
                mtp_timeout=0.1,
                mtp_system=mtp,
            )
            frame = provider.capture(timeout=0.01)

        self.assertEqual((frame.metadata.width, frame.metadata.height), (466, 466))
        self.assertEqual(frame.metadata.data_size, 466 * 466 * 3)

    def test_provider_uses_620f_project_geometry(self) -> None:
        serial = FakeBorrowedSession()
        mtp = FakeMtpSystem(width=466, height=466)
        with mock.patch.dict(
            "os.environ", {"W30_HARDWARE_PROJECT": "620F_W7830"}
        ):
            provider = MtpCaptureProvider(
                serial,
                sequence_start=6200,
                usb_timeout=0.1,
                mtp_timeout=0.1,
                mtp_system=mtp,
            )
            frame = provider.capture(timeout=0.01)

        self.assertEqual((frame.metadata.width, frame.metadata.height), (466, 466))
        self.assertEqual(frame.metadata.data_size, 466 * 466 * 3)

    def test_provider_borrows_serial_and_returns_generic_frame(self) -> None:
        serial = FakeBorrowedSession()
        mtp = FakeMtpSystem()
        provider = MtpCaptureProvider(
            serial,
            sequence_start=40,
            usb_timeout=0.1,
            mtp_timeout=0.1,
            mtp_system=mtp,
        )

        first = provider.capture(timeout=0.01, after_sequence=39)
        second = provider.capture(timeout=0.01, after_sequence=first.metadata.sequence)

        self.assertEqual(first.metadata.sequence, 40)
        self.assertEqual(second.metadata.sequence, 41)
        self.assertEqual(first.metadata.transport, "mtp")
        self.assertEqual(first.metadata.encoding, "bmp")
        self.assertEqual(first.metadata.pixel_format, "bgr888")
        self.assertEqual(first.metadata.data_size, 410 * 502 * 3)
        self.assertEqual(first.metadata.file_size, len(first.bmp))
        self.assertEqual(first.metadata.mtp_file_name, "agent_capture_40.bmp")
        self.assertFalse(first.metadata.receipt_verified)
        self.assertEqual(
            serial.shell_lines,
            [
                "dal_usb close",
                'srv_quick_cmd send "TOP5STEP:SCREENSHOT_CAPTURE_FILE:40;"',
                "dal_usb open",
                "dal_usb close",
                'srv_quick_cmd send "TOP5STEP:SCREENSHOT_CAPTURE_FILE:41;"',
                "dal_usb open",
            ],
        )
        self.assertEqual(serial.stop_calls, 0)

        with tempfile.TemporaryDirectory() as temporary_dir:
            output = Path(temporary_dir) / "frame.bmp"
            first.save_bmp(output)
            self.assertEqual(output.read_bytes(), first.bmp)

        provider.close()
        provider.close()
        self.assertEqual(serial.stop_calls, 0)
        with self.assertRaisesRegex(MtpScreenshotError, "closed"):
            provider.capture(timeout=0.01)

    def test_provider_accepts_zero_after_sequence_and_rejects_invalid_values(self) -> None:
        provider = MtpCaptureProvider(
            FakeBorrowedSession(),
            sequence_start=1,
            usb_timeout=0.1,
            mtp_timeout=0.1,
            mtp_system=FakeMtpSystem(),
        )
        self.assertEqual(
            provider.capture(timeout=0.01, after_sequence=0).metadata.sequence,
            1,
        )
        for invalid in (-1, 0x1_0000_0000, True, "1"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                provider.capture(timeout=0.01, after_sequence=invalid)

    def test_windows_read_only_usb_and_namespace_probes(self) -> None:
        system = WindowsMtpSystem()
        with mock.patch.object(
            system,
            "_run",
            side_effect=[
                json.dumps([{
                    "instance_id": "USB\\VID_301A&PID_6808\\SERIAL",
                    "status": "OK",
                    "friendly_name": "ZORA",
                    "class": "WPD",
                }]),
                json.dumps({
                    "device": "ZORA",
                    "storage": "storage",
                    "folder": "download",
                }),
            ],
        ):
            devices = system.inspect_usb_devices()
            namespace = system.probe_namespace()

        self.assertEqual(len(devices), 1)
        self.assertEqual(namespace["folder"], "download")

    def test_windows_namespace_probe_falls_back_to_volume_containing_download(
        self,
    ) -> None:
        system = WindowsMtpSystem()
        with mock.patch.object(
            system,
            "_run",
            return_value=json.dumps({
                "device": "ZORA",
                "storage": "ZORA MTP Storage Volume",
                "folder": "download",
            }),
        ) as run:
            namespace = system.probe_namespace()

        script = run.call_args.args[0]
        self.assertIn("$storageItems", script)
        self.assertIn("$env:WATCH_MTP_FOLDER", script)
        self.assertEqual(namespace["storage"], "ZORA MTP Storage Volume")

    def test_windows_copy_uses_mtp_filename_when_shell_hides_extension(
        self,
    ) -> None:
        system = WindowsMtpSystem()
        with tempfile.TemporaryDirectory() as root:
            target = Path(root) / "agent_capture_7.bmp"

            def fake_run(script, **_kwargs):
                self.assertIn("System.FileName", script)
                target.write_bytes(b"BM" + bytes(52))
                return ""

            with mock.patch.object(system, "_run", side_effect=fake_run):
                copied = system.copy_capture(
                    Path(root),
                    file_name="agent_capture_7.bmp",
                    timeout=0.1,
                )

        self.assertEqual(copied.name, "agent_capture_7.bmp")

    def test_restore_usb_retries_once_and_records_pnp_stage(self) -> None:
        from agent_loop_system.tools.mtp_screenshot import restore_usb_device

        class RetryGate(FakeMtpSystem):
            def __init__(self) -> None:
                super().__init__()
                self.attempt = 0

            def wait_for_usb(self, *, present: bool, timeout: float) -> None:
                self.usb_states.append(present)
                self.attempt += 1
                if self.attempt == 1:
                    raise MtpScreenshotTimeoutError("PnP absent")

            def inspect_usb_devices(self, *, timeout: float):
                return [{
                    "instance_id": "USB\\VID_301A&PID_6808\\PRIVATE-SERIAL",
                    "status": "OK",
                    "friendly_name": "ZORA",
                    "class": "WPD",
                }]

        writes: list[str] = []
        gate = RetryGate()
        diagnostics = restore_usb_device(
            writes.append,
            gate,
            usb_timeout=0.1,
        )

        self.assertEqual(writes, ["dal_usb open", "dal_usb open"])
        self.assertEqual([item["status"] for item in diagnostics], ["failed", "ready"])
        self.assertEqual(diagnostics[0]["stage"], "pnp")
        self.assertEqual(diagnostics[0]["pnp_probe_status"], "ok")
        self.assertEqual(len(diagnostics[0]["pnp_instances"]), 1)
        self.assertNotIn("PRIVATE-SERIAL", json.dumps(diagnostics))

    def test_restore_usb_distinguishes_wpd_not_ready(self) -> None:
        from agent_loop_system.tools.mtp_screenshot import (
            MtpUsbRestoreError,
            restore_usb_device,
        )

        class WpdGate(FakeMtpSystem):
            def probe_namespace(self, *, timeout: float):
                raise MtpScreenshotError("WPD namespace missing")

        with self.assertRaises(MtpUsbRestoreError) as raised:
            restore_usb_device(
                lambda _line: None,
                WpdGate(),
                usb_timeout=0.1,
                namespace_timeout=0.1,
            )

        self.assertEqual(len(raised.exception.attempts), 2)
        self.assertTrue(all(
            item["stage"] == "wpd" for item in raised.exception.attempts
        ))
        self.assertIn("attempts=", str(raised.exception))

    def test_capture_does_not_close_usb_when_initial_presence_is_unproven(self) -> None:
        class MissingInitialUsb(FakeMtpSystem):
            def wait_for_usb(self, *, present: bool, timeout: float) -> None:
                self.usb_states.append(present)
                raise MtpScreenshotTimeoutError("initial USB absent")

        transport = FakeTransport()
        gate = MissingInitialUsb()
        with tempfile.TemporaryDirectory() as root, self.assertRaises(
            MtpScreenshotTimeoutError
        ):
            capture_mtp_screenshot(
                Path(root) / "capture.bmp",
                sequence=1,
                capture_timeout=0.1,
                usb_timeout=0.1,
                mtp_timeout=0.1,
                mtp_system=gate,
                transport_factory=lambda: transport,
            )

        self.assertEqual(gate.usb_states, [True])
        self.assertEqual(transport.writes, [])


if __name__ == "__main__":
    unittest.main()
