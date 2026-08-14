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

from agent_loop_system.tools.mtp_screenshot import (
    MtpCaptureProvider,
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
    def __init__(self, *, valid_bmp: bool = True) -> None:
        self.usb_states: list[bool] = []
        self.valid_bmp = valid_bmp

    def wait_for_usb(self, *, present: bool, timeout: float) -> None:
        self.usb_states.append(present)

    def copy_capture(
        self, destination_dir: Path, *, file_name: str, timeout: float
    ) -> Path:
        target = destination_dir / file_name
        if self.valid_bmp:
            make_bmp(target)
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
            if self.present_attempts == 1:
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

    def stop(self) -> None:
        self.stop_calls += 1


class MtpScreenshotTest(unittest.TestCase):
    def test_windows_mtp_copy_waits_for_shell_namespace_discovery(self) -> None:
        captured: dict[str, object] = {}

        def runner(argv, **kwargs):
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
        self.assertEqual(captured["timeout"], 10.2)

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
            self.assertEqual(mtp.usb_states, [False, True])
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

            self.assertEqual(mtp.usb_states, [False, True, True])
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
        self.assertEqual(mtp.usb_states, [False, True])
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


if __name__ == "__main__":
    unittest.main()
