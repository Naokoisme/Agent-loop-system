from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from agent_loop_system.tools.watch_app_protocol import (
    WatchAppFrame,
    decode_frame,
    encode_frame,
)
from agent_loop_system.tools.watch_ble import (
    WatchBleClient,
    WatchBleProtocolError,
    _build_parser,
)
from agent_loop_system.tools.watch_ble_screenshot import (
    BLE_SCREENSHOT_COMMAND,
    encode_screenshot_request,
)
from tests.test_watch_ble import FakeClientFactory
from tests.test_watch_ble_screenshot import make_watch_bmp, screenshot_messages


class WatchBleScreenshotClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_client_uses_ff02_request_and_ff03_stream_to_save_verified_bmp(
        self,
    ) -> None:
        capture_sequence = 0x12345678
        bmp = make_watch_bmp()
        factory = FakeClientFactory(chunk_size=20)
        wrapper = WatchBleClient("AA:01", client_factory=factory)
        await wrapper.connect()
        fake = factory.instances[0]
        fake.response_bytes = b"".join(
            encode_frame(
                WatchAppFrame(
                    command=BLE_SCREENSHOT_COMMAND,
                    sequence=index & 0xFFFF,
                    payload=payload,
                )
            )
            for index, payload in enumerate(
                screenshot_messages(capture_sequence, bmp), start=100
            )
        )
        fake.response_slices = (7, 13, 233, 1024, 4096)
        fake.respond_on_write = True

        try:
            with tempfile.TemporaryDirectory() as temporary:
                output = Path(temporary) / "ble" / "watch.bmp"
                result = await wrapper.capture_screenshot(
                    output,
                    sequence=capture_sequence,
                    timeout=5,
                )

                self.assertEqual(output.read_bytes(), bmp)
                self.assertEqual(result.sequence, capture_sequence)
                self.assertEqual(result.file_size, 618_518)
                self.assertEqual(result.chunks, 645)
                self.assertEqual(result.path, str(output.resolve()))

            request = decode_frame(b"".join(data for data, _ in fake.writes))
            self.assertEqual(request.command, BLE_SCREENSHOT_COMMAND)
            self.assertEqual(
                request.payload,
                encode_screenshot_request(capture_sequence),
            )
            self.assertTrue(all(response is False for _, response in fake.writes))
        finally:
            await wrapper.close()

    def test_screenshot_cli_has_explicit_output_and_long_transfer_timeout(self) -> None:
        args = _build_parser().parse_args(
            ["screenshot", "--address", "AA:01", "--output", "watch.bmp"]
        )
        self.assertEqual(args.output, "watch.bmp")
        self.assertEqual(args.timeout, 180.0)
        self.assertIsNone(args.sequence)

    async def test_client_maps_invalid_capture_sequence_to_public_protocol_error(
        self,
    ) -> None:
        wrapper = WatchBleClient("AA:01", client_factory=FakeClientFactory())

        with self.assertRaisesRegex(
            WatchBleProtocolError,
            "sequence must be between 0 and 4294967295",
        ):
            await wrapper.capture_screenshot("watch.bmp", sequence=-1)


if __name__ == "__main__":
    unittest.main()
