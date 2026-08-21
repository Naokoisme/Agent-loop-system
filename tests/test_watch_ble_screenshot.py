from __future__ import annotations

import struct
import tempfile
import unittest
import zlib
from pathlib import Path

from agent_loop_system.tools.watch_ble_screenshot import (
    BLE_SCREENSHOT_ACK,
    BLE_SCREENSHOT_CHUNK_SIZE,
    BLE_SCREENSHOT_COMPLETE_ACK,
    BLE_SCREENSHOT_DATA,
    BLE_SCREENSHOT_END,
    BLE_SCREENSHOT_ERROR,
    BLE_SCREENSHOT_PROTOCOL_VERSION,
    BLE_SCREENSHOT_START,
    WatchBleScreenshotAssembler,
    WatchBleScreenshotProtocolError,
    WatchBleScreenshotRemoteError,
    encode_screenshot_ack,
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
            bytes.fromhex("02 00 78 56 34 12"),
        )

    def test_ack_is_fixed_eight_byte_little_endian_payload(self) -> None:
        self.assertEqual(
            encode_screenshot_ack(0x12345678, BLE_SCREENSHOT_COMPLETE_ACK),
            bytes.fromhex("02 05 78 56 34 12 ff ff"),
        )
        self.assertEqual(BLE_SCREENSHOT_ACK, 5)

    def test_request_and_ack_reject_non_integer_or_out_of_range_fields(self) -> None:
        invalid_uint32 = (-1, 0x1_0000_0000, True, 1.5)
        for value in invalid_uint32:
            with self.subTest(field="request_sequence", value=value):
                with self.assertRaises(WatchBleScreenshotProtocolError):
                    encode_screenshot_request(value)  # type: ignore[arg-type]
            with self.subTest(field="ack_sequence", value=value):
                with self.assertRaises(WatchBleScreenshotProtocolError):
                    encode_screenshot_ack(value, 0)  # type: ignore[arg-type]

        for value in (-1, 0x1_0000, True, 1.5):
            with self.subTest(field="next_chunk", value=value):
                with self.assertRaises(WatchBleScreenshotProtocolError):
                    encode_screenshot_ack(1, value)  # type: ignore[arg-type]

        self.assertEqual(
            encode_screenshot_ack(0xFFFFFFFF, 0xFFFF),
            bytes.fromhex("02 05 ff ff ff ff ff ff"),
        )

    def test_complete_bmp_returns_cumulative_acks_and_is_written(self) -> None:
        sequence = 0x10203040
        bmp = make_watch_bmp()
        assembler = WatchBleScreenshotAssembler(sequence)
        messages = screenshot_messages(sequence, bmp)

        start_result = assembler.feed(messages[0])
        self.assertIsNotNone(start_result)
        assert start_result is not None
        self.assertEqual(start_result.next_chunk, 0)
        self.assertIsNone(start_result.screenshot)

        for expected_next_chunk, message in enumerate(messages[1:-1], start=1):
            data_result = assembler.feed(message)
            self.assertIsNotNone(data_result)
            assert data_result is not None
            self.assertEqual(data_result.next_chunk, expected_next_chunk)
            self.assertIsNone(data_result.screenshot)

        end_result = assembler.feed(messages[-1])
        self.assertIsNotNone(end_result)
        assert end_result is not None
        self.assertEqual(end_result.next_chunk, BLE_SCREENSHOT_COMPLETE_ACK)
        result = end_result.screenshot

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
        assert result is not None
        self.assertIsNotNone(result.screenshot)

    def test_duplicate_start_is_idempotent_without_clearing_received_data(self) -> None:
        sequence = 5
        messages = screenshot_messages(sequence, make_watch_bmp())
        assembler = WatchBleScreenshotAssembler(sequence)

        self.assertEqual(assembler.feed(messages[0]).next_chunk, 0)  # type: ignore[union-attr]
        self.assertEqual(assembler.feed(messages[1]).next_chunk, 1)  # type: ignore[union-attr]
        duplicate = assembler.feed(messages[0])
        self.assertIsNotNone(duplicate)
        assert duplicate is not None
        self.assertEqual(duplicate.next_chunk, 0)

        # The duplicate START did not erase chunk zero.
        self.assertEqual(assembler.feed(messages[1]).next_chunk, 1)  # type: ignore[union-attr]

    def test_conflicting_duplicate_start_is_rejected(self) -> None:
        sequence = 6
        bmp = make_watch_bmp()
        messages = screenshot_messages(sequence, bmp)
        assembler = WatchBleScreenshotAssembler(sequence)
        assembler.feed(messages[0])
        _, _, _, file_size, chunk_size, chunk_count = struct.unpack(
            "<BBIIHH", messages[0]
        )
        conflicting = struct.pack(
            "<BBIIHH",
            BLE_SCREENSHOT_PROTOCOL_VERSION,
            BLE_SCREENSHOT_START,
            sequence,
            file_size + 1,
            chunk_size,
            chunk_count,
        )
        with self.assertRaisesRegex(
            WatchBleScreenshotProtocolError,
            "duplicate screenshot START metadata does not match",
        ):
            assembler.feed(conflicting)

    def test_duplicate_data_is_idempotent_but_conflicting_data_is_rejected(self) -> None:
        sequence = 7
        messages = screenshot_messages(sequence, make_watch_bmp())
        assembler = WatchBleScreenshotAssembler(sequence)
        assembler.feed(messages[0])

        first = assembler.feed(messages[1])
        self.assertIsNotNone(first)
        assert first is not None
        self.assertEqual(first.next_chunk, 1)
        duplicate = assembler.feed(messages[1])
        self.assertIsNotNone(duplicate)
        assert duplicate is not None
        self.assertEqual(duplicate.next_chunk, 1)

        conflicting = messages[1][:-1] + bytes((messages[1][-1] ^ 0xFF,))
        with self.assertRaisesRegex(
            WatchBleScreenshotProtocolError,
            "duplicate screenshot chunk 0 data does not match",
        ):
            assembler.feed(conflicting)

    def test_future_data_returns_current_ack_without_mutating_state(self) -> None:
        sequence = 8
        messages = screenshot_messages(sequence, make_watch_bmp())
        assembler = WatchBleScreenshotAssembler(sequence)
        assembler.feed(messages[0])

        future = assembler.feed(messages[2])
        self.assertIsNotNone(future)
        assert future is not None
        self.assertEqual(future.next_chunk, 0)
        self.assertIsNone(future.screenshot)

        self.assertEqual(assembler.feed(messages[1]).next_chunk, 1)  # type: ignore[union-attr]
        self.assertEqual(assembler.feed(messages[2]).next_chunk, 2)  # type: ignore[union-attr]

    def test_duplicate_end_returns_complete_ack_without_second_screenshot(self) -> None:
        sequence = 9
        messages = screenshot_messages(sequence, make_watch_bmp())
        assembler = WatchBleScreenshotAssembler(sequence)
        first_end = None
        for message in messages:
            first_end = assembler.feed(message)
        self.assertIsNotNone(first_end)
        assert first_end is not None
        self.assertIsNotNone(first_end.screenshot)

        duplicate_end = assembler.feed(messages[-1])
        self.assertIsNotNone(duplicate_end)
        assert duplicate_end is not None
        self.assertEqual(duplicate_end.next_chunk, BLE_SCREENSHOT_COMPLETE_ACK)
        self.assertIsNone(duplicate_end.screenshot)

        _, _, _, file_size, crc32, chunk_count = struct.unpack(
            "<BBIIIH", messages[-1]
        )
        conflicting_end = struct.pack(
            "<BBIIIH",
            BLE_SCREENSHOT_PROTOCOL_VERSION,
            BLE_SCREENSHOT_END,
            sequence,
            file_size,
            crc32 ^ 1,
            chunk_count,
        )
        with self.assertRaisesRegex(
            WatchBleScreenshotProtocolError,
            "duplicate screenshot END metadata does not match",
        ):
            assembler.feed(conflicting_end)

    def test_unsupported_protocol_version_is_rejected(self) -> None:
        start = bytearray(screenshot_messages(10, make_watch_bmp())[0])
        start[0] = BLE_SCREENSHOT_PROTOCOL_VERSION - 1
        with self.assertRaisesRegex(
            WatchBleScreenshotProtocolError,
            "unsupported screenshot protocol version 1",
        ):
            WatchBleScreenshotAssembler(10).feed(start)

    def test_end_crc_mismatch_is_rejected(self) -> None:
        messages = screenshot_messages(17, make_watch_bmp())
        messages[-1] = messages[-1][:-6] + struct.pack("<I", 1) + messages[-1][-2:]
        assembler = WatchBleScreenshotAssembler(17)
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
