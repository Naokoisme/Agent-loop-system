from __future__ import annotations

import tempfile
import unittest
import zlib
from pathlib import Path
from types import SimpleNamespace

from agent_loop_system.tools.watch_ble import (
    WatchBleAmbiguousDeviceError,
    WatchBleScreenshotResult,
)
from agent_loop_system.tools.watch_ble_provider import (
    BleCaptureProvider,
    BleCaptureProviderError,
    BleCaptureSequenceError,
)
from tests.test_watch_ble_screenshot import make_watch_bmp


def payload_crc32(bmp: bytes) -> int:
    result = 0
    stride = ((410 * 3 + 3) // 4) * 4
    for row in range(502):
        start = 54 + row * stride
        result = zlib.crc32(bmp[start : start + 410 * 3], result)
    return result & 0xFFFFFFFF


class FakeScanner:
    calls: list[dict[str, object]] = []

    @classmethod
    async def discover(cls, **kwargs):
        cls.calls.append(kwargs)
        device = SimpleNamespace(address="42:74:DC:C8:0A:02", name=None)
        advertisement = SimpleNamespace(
            local_name="oraimo Watch Tank N_0A02",
            rssi=-45,
            service_uuids=[],
        )
        return {"target": (device, advertisement)}


class FakeAmbiguousScanner:
    @classmethod
    async def discover(cls, **kwargs):
        return {
            "first": (
                SimpleNamespace(address="42:74:DC:C8:0A:02", name=None),
                SimpleNamespace(
                    local_name="oraimo Watch Tank N_0A02",
                    rssi=-45,
                    service_uuids=[],
                ),
            ),
            "second": (
                SimpleNamespace(address="54:C8:D4:D9:29:06", name=None),
                SimpleNamespace(
                    local_name="oraimo Watch Tank N_2906",
                    rssi=-50,
                    service_uuids=[],
                ),
            ),
        }


class FakeSerialSession:
    def __init__(self) -> None:
        self.shell_lines: list[str] = []

    def write_shell_line(self, line: str) -> None:
        self.shell_lines.append(line)


class FakeUsbSystem:
    def __init__(self, *, restore_failures: int = 0) -> None:
        self.wait_calls: list[dict[str, object]] = []
        self.restore_failures = restore_failures

    def wait_for_usb(self, *, present: bool, timeout: float) -> None:
        self.wait_calls.append({"present": present, "timeout": timeout})
        if present and self.restore_failures > 0:
            self.restore_failures -= 1
            raise RuntimeError("USB did not return")


class FakeScreenshotClient:
    def __init__(self, bmp: bytes, *, capture_error: Exception | None = None) -> None:
        self.bmp = bmp
        self.capture_error = capture_error
        self.connect_calls: list[bool] = []
        self.capture_calls: list[dict[str, object]] = []
        self.close_calls = 0

    async def connect(self, *, pair: bool = False) -> None:
        self.connect_calls.append(pair)

    async def capture_screenshot(
        self,
        output_path,
        *,
        sequence: int | None = None,
        timeout: float = 180.0,
    ) -> WatchBleScreenshotResult:
        if self.capture_error is not None:
            raise self.capture_error
        assert sequence is not None
        output = Path(output_path)
        output.write_bytes(self.bmp)
        self.capture_calls.append(
            {"sequence": sequence, "timeout": timeout, "output": output.name}
        )
        return WatchBleScreenshotResult(
            sequence=sequence,
            path=str(output.resolve()),
            file_size=len(self.bmp),
            crc32=f"{zlib.crc32(self.bmp) & 0xFFFFFFFF:08x}",
            payload_crc32=f"{payload_crc32(self.bmp):08x}",
            chunks=645,
            chunk_size=960,
            width=410,
            height=502,
        )

    async def close(self) -> None:
        self.close_calls += 1


class FakeClientBuilder:
    def __init__(
        self,
        bmp: bytes,
        *,
        capture_error: Exception | None = None,
    ) -> None:
        self.bmp = bmp
        self.capture_error = capture_error
        self.calls: list[tuple[object, float]] = []
        self.clients: list[FakeScreenshotClient] = []

    def __call__(self, device, timeout: float) -> FakeScreenshotClient:
        self.calls.append((device, timeout))
        client = FakeScreenshotClient(
            self.bmp,
            capture_error=self.capture_error,
        )
        self.clients.append(client)
        return client


class BleCaptureProviderTest(unittest.TestCase):
    def setUp(self) -> None:
        FakeScanner.calls = []

    def test_provider_returns_generic_fresh_frames_and_closes_each_connection(
        self,
    ) -> None:
        bmp = make_watch_bmp()
        builder = FakeClientBuilder(bmp)
        serial = FakeSerialSession()
        usb = FakeUsbSystem()
        provider = BleCaptureProvider(
            "42:74:DC:C8:0A:02",
            serial_session=serial,
            sequence_start=100,
            scan_timeout=3.5,
            usb_timeout=2.0,
            usb_system=usb,
            scanner=FakeScanner,
            client_builder=builder,
        )
        try:
            first = provider.capture(timeout=4.0)
            second = provider.capture(timeout=4.0, after_sequence=100)

            self.assertEqual((first.metadata.sequence, second.metadata.sequence), (100, 101))
            self.assertEqual(first.bmp, bmp)
            self.assertEqual(first.metadata.transport, "ble_gatt")
            self.assertEqual(first.metadata.ble_address, "42:74:DC:C8:0A:02")
            self.assertEqual(first.metadata.file_size, 618_518)
            self.assertEqual(first.metadata.data_size, 410 * 502 * 3)
            self.assertEqual(first.metadata.stride, 410 * 3)
            self.assertEqual(first.metadata.chunk_bytes, 960)
            self.assertEqual(first.metadata.chunks, 645)
            self.assertEqual(first.metadata.payload_crc32, payload_crc32(bmp))
            self.assertEqual(
                first.metadata.file_crc32,
                zlib.crc32(bmp) & 0xFFFFFFFF,
            )
            self.assertTrue(first.metadata.receipt_verified)
            self.assertEqual(
                FakeScanner.calls,
                [
                    {"timeout": 3.5, "return_adv": True},
                    {"timeout": 3.5, "return_adv": True},
                ],
            )
            self.assertEqual(len(builder.clients), 2)
            self.assertTrue(
                all(client.connect_calls == [False] for client in builder.clients)
            )
            self.assertTrue(
                all(client.close_calls == 1 for client in builder.clients)
            )
            self.assertEqual(
                serial.shell_lines,
                [
                    "dal_usb close",
                    "dal_usb open",
                    "dal_usb close",
                    "dal_usb open",
                ],
            )
            self.assertEqual(
                usb.wait_calls,
                [
                    {"present": False, "timeout": 2.0},
                    {"present": True, "timeout": 2.0},
                    {"present": False, "timeout": 2.0},
                    {"present": True, "timeout": 2.0},
                ],
            )

            with tempfile.TemporaryDirectory() as root:
                output = Path(root) / "nested" / "watch.bmp"
                self.assertIs(first.save_bmp(output), first.metadata)
                self.assertEqual(output.read_bytes(), bmp)
        finally:
            provider.close()
        self.assertTrue(provider.closed)
        provider.close()

    def test_provider_discovers_the_only_matching_watch_without_address(self) -> None:
        bmp = make_watch_bmp()
        builder = FakeClientBuilder(bmp)
        provider = BleCaptureProvider(
            None,
            serial_session=FakeSerialSession(),
            sequence_start=100,
            usb_system=FakeUsbSystem(),
            scanner=FakeScanner,
            client_builder=builder,
        )
        try:
            frame = provider.capture(timeout=4.0)
        finally:
            provider.close()

        self.assertEqual(frame.metadata.ble_address, "42:74:DC:C8:0A:02")
        self.assertEqual(builder.calls[0][0].address, "42:74:DC:C8:0A:02")

    def test_provider_fails_closed_when_dynamic_discovery_is_ambiguous(self) -> None:
        serial = FakeSerialSession()
        usb = FakeUsbSystem()
        provider = BleCaptureProvider(
            None,
            serial_session=serial,
            usb_system=usb,
            scanner=FakeAmbiguousScanner,
            client_builder=FakeClientBuilder(make_watch_bmp()),
        )
        try:
            with self.assertRaisesRegex(
                WatchBleAmbiguousDeviceError,
                "matched multiple BLE devices",
            ):
                provider.capture(timeout=4.0)
        finally:
            provider.close()

        self.assertEqual(serial.shell_lines, ["dal_usb close", "dal_usb open"])

    def test_provider_rejects_capture_after_close(self) -> None:
        provider = BleCaptureProvider(
            "42:74:DC:C8:0A:02",
            serial_session=FakeSerialSession(),
            usb_system=FakeUsbSystem(),
            scanner=FakeScanner,
            client_builder=FakeClientBuilder(make_watch_bmp()),
        )
        provider.close()

        with self.assertRaisesRegex(BleCaptureProviderError, "provider is closed"):
            provider.capture(timeout=1)

    def test_provider_rejects_sequence_newer_than_uint32(self) -> None:
        provider = BleCaptureProvider(
            "42:74:DC:C8:0A:02",
            serial_session=FakeSerialSession(),
            usb_system=FakeUsbSystem(),
            sequence_start=1,
            scanner=FakeScanner,
            client_builder=FakeClientBuilder(make_watch_bmp()),
        )
        try:
            with self.assertRaises(BleCaptureSequenceError):
                provider.capture(timeout=1, after_sequence=0xFFFFFFFF)
        finally:
            provider.close()

    def test_capture_failure_still_restores_usb(self) -> None:
        serial = FakeSerialSession()
        usb = FakeUsbSystem()
        provider = BleCaptureProvider(
            "42:74:DC:C8:0A:02",
            serial_session=serial,
            usb_timeout=2.0,
            usb_system=usb,
            scanner=FakeScanner,
            client_builder=FakeClientBuilder(
                make_watch_bmp(),
                capture_error=RuntimeError("capture failed"),
            ),
        )
        try:
            with self.assertRaisesRegex(RuntimeError, "capture failed"):
                provider.capture(timeout=4.0)
        finally:
            provider.close()

        self.assertEqual(
            serial.shell_lines,
            ["dal_usb close", "dal_usb open"],
        )
        self.assertEqual(
            usb.wait_calls,
            [
                {"present": False, "timeout": 2.0},
                {"present": True, "timeout": 2.0},
            ],
        )

    def test_restore_usb_retries_then_reports_failure(self) -> None:
        serial = FakeSerialSession()
        usb = FakeUsbSystem(restore_failures=2)
        provider = BleCaptureProvider(
            "42:74:DC:C8:0A:02",
            serial_session=serial,
            usb_timeout=2.0,
            usb_system=usb,
            scanner=FakeScanner,
            client_builder=FakeClientBuilder(make_watch_bmp()),
        )
        try:
            with self.assertRaisesRegex(
                BleCaptureProviderError,
                "could not restore USB after BLE screenshot",
            ):
                provider.capture(timeout=4.0)
        finally:
            provider.close()

        self.assertEqual(
            serial.shell_lines,
            ["dal_usb close", "dal_usb open", "dal_usb open"],
        )


if __name__ == "__main__":
    unittest.main()
