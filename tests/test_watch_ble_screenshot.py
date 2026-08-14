from __future__ import annotations

import struct
import tempfile
import unittest
import zlib
from pathlib import Path

from agent_loop_system.tools.watch_ble_screenshot import (
    BLE_SCREENSHOT_CHUNK_SIZE,
    BLE_SCREENSHOT_DATA,
    BLE_SCREENSHOT_END,
    BLE_SCREENSHOT_ERROR,
    BLE_SCREENSHOT_PROTOCOL_VERSION,
    BLE_SCREENSHOT_START,
    WatchBleScreenshotAssembler,
    WatchBleScreenshotProtocolError,
    WatchBleScreenshotRemoteError,
    encode_screenshot_request,
    write_verified_screenshot,
)


def make_watch_bmp() -> bytes:
    width = 410
    height = 502
    stride = ((width * 3 + 3) // 4) * 4
    pixel_size = stride * height
    file_size = 54 + pixel_size
    header = bytearray(54)
    header[:2] = b"BM"
    struct.pack_into("<I", header, 2, file_size)
    struct.pack_into("<I", header, 10, 54)
    struct.pack_into("<I", header, 14, 40)
    struct.pack_into("<ii", header, 18, width, -height)
    struct.pack_into("<HH", header, 26, 1, 24)
    struct.pack_into("<I", header, 30, 0)
    struct.pack_into("<I", header, 34, pixel_size)
    pixels = bytes((index * 17) & 0xFF for index in range(pixel_size))
    return bytes(header) + pixels


def screenshot_messages(sequence: int, bmp: bytes) -> list[bytes]:
    count = (len(bmp) + BLE_SCREENSHOT_CHUNK_SIZE - 1) // BLE_SCREENSHOT_CHUNK_SIZE
    messages = [
        struct.pack(
            "<BBIIHH",
            BLE_SCREENSHOT_PROTOCOL_VERSION,
            BLE_SCREENSHOT_START,
            sequence,
            len(bmp),
            BLE_SCREENSHOT_CHUNK_SIZE,
            count,
        )
    ]
    for index, offset in enumerate(range(0, len(bmp), BLE_SCREENSHOT_CHUNK_SIZE)):
        data = bmp[offset : offset + BLE_SCREENSHOT_CHUNK_SIZE]
        messages.append(
            struct.pack(
                "<BBIHIH",
                BLE_SCREENSHOT_PROTOCOL_VERSION,
                BLE_SCREENSHOT_DATA,
                sequence,
                index,
                offset,
                len(data),
            )
            + data
        )
    messages.append(
        struct.pack(
            "<BBIIIH",
            BLE_SCREENSHOT_PROTOCOL_VERSION,
            BLE_SCREENSHOT_END,
            sequence,
            len(bmp),
            zlib.crc32(bmp) & 0xFFFFFFFF,
            count,
        )
    )
    return messages


class WatchBleScreenshotProtocolTests(unittest.TestCase):
    def test_request_is_fixed_little_endian_payload(self) -> None:
        self.assertEqual(
            encode_screenshot_request(0x12345678),
            bytes.fromhex("01 00 78 56 34 12"),
        )

    def test_complete_bmp_is_strictly_reassembled_and_written(self) -> None:
        sequence = 0x10203040
        bmp = make_watch_bmp()
        assembler = WatchBleScreenshotAssembler(sequence)
        result = None
        for message in screenshot_messages(sequence, bmp):
            result = assembler.feed(message)

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.data, bmp)
        self.assertEqual(result.file_size, 618_518)
        self.assertEqual(result.crc32, zlib.crc32(bmp) & 0xFFFFFFFF)
        payload_crc32 = 0
        stride = ((410 * 3 + 3) // 4) * 4
        for row in range(502):
            start = 54 + row * stride
            payload_crc32 = zlib.crc32(bmp[start : start + 410 * 3], payload_crc32)
        self.assertEqual(result.payload_crc32, payload_crc32 & 0xFFFFFFFF)
        self.assertEqual(result.chunk_count, 645)
        self.assertEqual((result.width, result.height), (410, 502))
        self.assertTrue(result.top_down)

        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "nested" / "watch.bmp"
            self.assertEqual(write_verified_screenshot(result, output), output.resolve())
            self.assertEqual(output.read_bytes(), bmp)
            self.assertEqual(list(output.parent.glob("*.tmp")), [])

    def test_stale_sequence_is_ignored_without_mutating_active_capture(self) -> None:
        bmp = make_watch_bmp()
        assembler = WatchBleScreenshotAssembler(22)
        stale = screenshot_messages(21, bmp)[0]
        self.assertIsNone(assembler.feed(stale))

        result = None
        for message in screenshot_messages(22, bmp):
            result = assembler.feed(message)
        self.assertIsNotNone(result)

    def test_missing_or_out_of_order_chunk_is_rejected(self) -> None:
        messages = screenshot_messages(5, make_watch_bmp())
        assembler = WatchBleScreenshotAssembler(5)
        assembler.feed(messages[0])
        with self.assertRaisesRegex(
            WatchBleScreenshotProtocolError, "chunk index 1.*expected 0"
        ):
            assembler.feed(messages[2])

    def test_end_crc_mismatch_is_rejected(self) -> None:
        messages = screenshot_messages(7, make_watch_bmp())
        messages[-1] = messages[-1][:-6] + struct.pack("<I", 1) + messages[-1][-2:]
        assembler = WatchBleScreenshotAssembler(7)
        with self.assertRaisesRegex(WatchBleScreenshotProtocolError, "CRC32 mismatch"):
            for message in messages:
                assembler.feed(message)

    def test_remote_error_keeps_numeric_code_and_reason(self) -> None:
        payload = struct.pack(
            "<BBIH",
            BLE_SCREENSHOT_PROTOCOL_VERSION,
            BLE_SCREENSHOT_ERROR,
            99,
            2,
        )
        with self.assertRaises(WatchBleScreenshotRemoteError) as caught:
            WatchBleScreenshotAssembler(99).feed(payload)
        self.assertEqual(caught.exception.code, 2)
        self.assertEqual(caught.exception.reason, "busy")

    def test_wrong_bmp_dimensions_are_rejected_after_integrity_passes(self) -> None:
        bmp = bytearray(make_watch_bmp())
        struct.pack_into("<i", bmp, 18, 409)
        assembler = WatchBleScreenshotAssembler(11)
        with self.assertRaisesRegex(
            WatchBleScreenshotProtocolError, "dimensions must be 410x502"
        ):
            for message in screenshot_messages(11, bytes(bmp)):
                assembler.feed(message)


if __name__ == "__main__":
    unittest.main()
