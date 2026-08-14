from __future__ import annotations

import tempfile
import unittest
import zlib
from pathlib import Path
from types import SimpleNamespace

from agent_loop_system.tools.watch_ble import WatchBleScreenshotResult
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


class FakeScreenshotClient:
    def __init__(self, bmp: bytes) -> None:
        self.bmp = bmp
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
    def __init__(self, bmp: bytes) -> None:
        self.bmp = bmp
        self.calls: list[tuple[object, float]] = []
        self.clients: list[FakeScreenshotClient] = []

    def __call__(self, device, timeout: float) -> FakeScreenshotClient:
        self.calls.append((device, timeout))
        client = FakeScreenshotClient(self.bmp)
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
        provider = BleCaptureProvider(
            "42:74:DC:C8:0A:02",
            sequence_start=100,
            scan_timeout=3.5,
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

            with tempfile.TemporaryDirectory() as root:
                output = Path(root) / "nested" / "watch.bmp"
                self.assertIs(first.save_bmp(output), first.metadata)
                self.assertEqual(output.read_bytes(), bmp)
        finally:
            provider.close()
        self.assertTrue(provider.closed)
        provider.close()

    def test_provider_rejects_capture_after_close(self) -> None:
        provider = BleCaptureProvider(
            "42:74:DC:C8:0A:02",
            scanner=FakeScanner,
            client_builder=FakeClientBuilder(make_watch_bmp()),
        )
        provider.close()

        with self.assertRaisesRegex(BleCaptureProviderError, "provider is closed"):
            provider.capture(timeout=1)

    def test_provider_rejects_sequence_newer_than_uint32(self) -> None:
        provider = BleCaptureProvider(
            "42:74:DC:C8:0A:02",
            sequence_start=1,
            scanner=FakeScanner,
            client_builder=FakeClientBuilder(make_watch_bmp()),
        )
        try:
            with self.assertRaises(BleCaptureSequenceError):
                provider.capture(timeout=1, after_sequence=0xFFFFFFFF)
        finally:
            provider.close()


if __name__ == "__main__":
    unittest.main()
