from __future__ import annotations

import base64
import copy
import struct
import tempfile
import unittest
import zlib
from pathlib import Path
from types import SimpleNamespace

from agent_loop_system.tools.watch_capture import (
    WatchCaptureDeviceError,
    WatchCaptureError,
    WatchCaptureProtocolError,
    WatchCaptureProvider,
    WatchCaptureSequenceError,
    WatchCaptureTimeoutError,
)


def make_stream(
    payload: bytes,
    *,
    sequence: int = 41,
    width: int = 2,
    height: int = 2,
    pixel_format: str = "bgr888",
    stride: int = 6,
    chunk_bytes: int = 5,
) -> list[dict]:
    checksum = zlib.crc32(payload) & 0xFFFFFFFF
    chunk_count = (len(payload) + chunk_bytes - 1) // chunk_bytes
    events: list[dict] = [
        {
            "protocol": "w30_test_bridge",
            "version": 1,
            "type": "command_result",
            "request": "screenshot_capture",
            "seq": sequence,
            "status": "accepted",
        },
        {
            "protocol": "w30_test_bridge",
            "version": 1,
            "type": "screenshot_begin",
            "request": "screenshot_capture",
            "seq": sequence,
            "status": "ok",
            "width": width,
            "height": height,
            "pixel_format": pixel_format,
            "encoding": "base64",
            "stride": stride,
            "data_size": len(payload),
            "chunk_bytes": chunk_bytes,
            "chunks": chunk_count,
            "crc32": f"{checksum:08x}",
            "device_uptime_ms": 1_234,
            "capture_duration_ms": 17,
            "pixel_source": "vde_lcd_composite",
        },
    ]
    for index, offset in enumerate(range(0, len(payload), chunk_bytes)):
        events.append(
            {
                "protocol": "w30_test_bridge",
                "version": 1,
                "type": "screenshot_chunk",
                "request": "screenshot_capture",
                "seq": sequence,
                "status": "ok",
                "index": index,
                "offset": offset,
                "data": base64.b64encode(
                    payload[offset : offset + chunk_bytes]
                ).decode("ascii"),
            }
        )
    events.append(
        {
            "protocol": "w30_test_bridge",
            "version": 1,
            "type": "screenshot_end",
            "request": "screenshot_capture",
            "seq": sequence,
            "status": "complete",
            "chunks": chunk_count,
            "data_size": len(payload),
            "crc32": f"{checksum:08x}",
        }
    )
    return events


class FakeSerialSession:
    def __init__(self, event_factory=None) -> None:
        self.events: list[dict] = []
        self.event_factory = event_factory
        self.send_calls: list[tuple[str, dict]] = []
        self.events_since_calls: list[int] = []
        self.stop_calls = 0
        self.send_error: BaseException | None = None

    @property
    def event_count(self) -> int:
        return len(self.events)

    def send(self, content: str, **kwargs):
        self.send_calls.append((content, kwargs))
        if self.send_error is not None:
            raise self.send_error
        sequence = kwargs["seq"]
        new_events = list(self.event_factory(sequence))
        self.events.extend(copy.deepcopy(new_events))
        terminal = next(
            (
                event
                for event in reversed(new_events)
                if event.get("type") in {"screenshot_end", "command_result"}
            ),
            None,
        )
        if terminal is None:
            raise TimeoutError("fake stream has no terminal")
        return SimpleNamespace(raw=copy.deepcopy(terminal))

    def events_since(self, start_index: int) -> list[dict]:
        self.events_since_calls.append(start_index)
        return copy.deepcopy(self.events[start_index:])

    def stop(self) -> None:
        self.stop_calls += 1


class WatchCaptureProviderTest(unittest.TestCase):
    def test_capture_uses_serial_cursor_and_returns_complete_metadata(self) -> None:
        payload = bytes(range(12))
        serial = FakeSerialSession(
            lambda sequence: make_stream(payload, sequence=sequence)
        )
        serial.events.append(
            {"request": "screenshot_capture", "seq": 40, "type": "screenshot_end"}
        )
        provider = WatchCaptureProvider(serial, sequence_start=41)

        frame = provider.capture(timeout=8.0, after_sequence=40)

        self.assertEqual(frame.pixels, payload)
        self.assertEqual(
            serial.send_calls[0][0],
            ":SCREENSHOT_CAPTURE:41",
        )
        send_kwargs = serial.send_calls[0][1]
        self.assertEqual(send_kwargs["request"], "screenshot_capture")
        self.assertEqual(send_kwargs["seq"], 41)
        self.assertEqual(send_kwargs["expected_type"], "screenshot_end")
        self.assertEqual(
            send_kwargs["expected_status"],
            ("complete", "error", "busy", "unavailable"),
        )
        self.assertEqual(serial.events_since_calls, [1])

        metadata = frame.metadata
        self.assertEqual(metadata.sequence, 41)
        self.assertEqual(metadata.timestamp, 1.234)
        self.assertEqual((metadata.width, metadata.height), (2, 2))
        self.assertEqual(metadata.pixel_format, "bgr888")
        self.assertEqual(metadata.data_size, 12)
        self.assertEqual(metadata.stride, 6)
        self.assertEqual(metadata.source, "watch_display")
        self.assertEqual(metadata.transport, "hardware_serial")
        self.assertEqual(metadata.payload_crc32, zlib.crc32(payload) & 0xFFFFFFFF)
        self.assertEqual(metadata.device_uptime_ms, 1_234)
        self.assertEqual(metadata.capture_duration_ms, 17)
        self.assertEqual(metadata.pixel_source, "vde_lcd_composite")

    def test_sequences_increase_and_honor_a_newer_after_sequence(self) -> None:
        payload = b"\x00" * 12
        serial = FakeSerialSession(
            lambda sequence: make_stream(payload, sequence=sequence)
        )
        provider = WatchCaptureProvider(serial, sequence_start=10)

        first = provider.capture(timeout=1)
        second = provider.capture(timeout=1, after_sequence=first.metadata.sequence)
        third = provider.capture(timeout=1, after_sequence=50)

        self.assertEqual(
            [first.metadata.sequence, second.metadata.sequence, third.metadata.sequence],
            [10, 11, 51],
        )
        self.assertEqual(
            [call[0] for call in serial.send_calls],
            [
                ":SCREENSHOT_CAPTURE:10",
                ":SCREENSHOT_CAPTURE:11",
                ":SCREENSHOT_CAPTURE:51",
            ],
        )

    def test_invalid_capture_arguments_are_rejected_before_serial_send(self) -> None:
        serial = FakeSerialSession(lambda sequence: [])
        provider = WatchCaptureProvider(serial, sequence_start=1)

        for timeout in (0, -1, float("inf"), float("nan"), True, "1"):
            with self.subTest(timeout=timeout), self.assertRaisesRegex(
                ValueError, "timeout"
            ):
                provider.capture(timeout=timeout)  # type: ignore[arg-type]
        for after in (-1, 0x1_0000_0000, True, "1"):
            with self.subTest(after=after), self.assertRaisesRegex(
                ValueError, "after_sequence"
            ):
                provider.capture(timeout=1, after_sequence=after)  # type: ignore[arg-type]
        with self.assertRaises(WatchCaptureSequenceError):
            provider.capture(timeout=1, after_sequence=0xFFFFFFFF)
        self.assertEqual(serial.send_calls, [])

    def test_close_is_idempotent_and_does_not_close_shared_serial(self) -> None:
        serial = FakeSerialSession(lambda sequence: [])
        provider = WatchCaptureProvider(serial, sequence_start=1)

        provider.close()
        provider.close()

        self.assertTrue(provider.closed)
        self.assertEqual(serial.stop_calls, 0)
        with self.assertRaisesRegex(WatchCaptureError, "closed"):
            provider.capture(timeout=1)

    def test_timeout_is_wrapped_with_capture_context(self) -> None:
        serial = FakeSerialSession(lambda sequence: [])
        serial.send_error = TimeoutError("wire timeout")
        provider = WatchCaptureProvider(serial, sequence_start=7)

        with self.assertRaisesRegex(WatchCaptureTimeoutError, "capture 7"):
            provider.capture(timeout=0.1)

    def test_terminal_device_statuses_raise_and_do_not_publish_a_frame(self) -> None:
        for status in ("busy", "unavailable", "error"):
            with self.subTest(status=status):
                def error_stream(sequence: int, status=status) -> list[dict]:
                    return [
                        {
                            "type": "command_result",
                            "request": "screenshot_capture",
                            "seq": sequence,
                            "status": "accepted",
                        },
                        {
                            "type": "screenshot_end",
                            "request": "screenshot_capture",
                            "seq": sequence,
                            "status": status,
                            "reason": "display_not_ready",
                        },
                    ]

                serial = FakeSerialSession(error_stream)
                provider = WatchCaptureProvider(serial, sequence_start=20)
                with self.assertRaisesRegex(
                    WatchCaptureDeviceError, f"{status}.*display_not_ready"
                ):
                    provider.capture(timeout=1)

    def test_rejected_command_is_an_explicit_device_error(self) -> None:
        def rejected(sequence: int) -> list[dict]:
            return [
                {
                    "type": "command_result",
                    "request": "screenshot_capture",
                    "seq": sequence,
                    "status": "rejected",
                    "reason": "test_command_disabled",
                }
            ]

        provider = WatchCaptureProvider(
            FakeSerialSession(rejected), sequence_start=3
        )
        with self.assertRaisesRegex(
            WatchCaptureDeviceError, "rejected.*test_command_disabled"
        ):
            provider.capture(timeout=1)

    def test_unrelated_request_and_sequence_events_are_ignored(self) -> None:
        payload = bytes(range(12))

        def interleaved(sequence: int) -> list[dict]:
            valid = make_stream(payload, sequence=sequence)
            valid.insert(
                2,
                {
                    "type": "screenshot_chunk",
                    "request": "other_request",
                    "seq": sequence,
                    "index": 99,
                },
            )
            valid.insert(
                3,
                {
                    "type": "screenshot_chunk",
                    "request": "screenshot_capture",
                    "seq": sequence + 1,
                    "index": 99,
                },
            )
            return valid

        provider = WatchCaptureProvider(
            FakeSerialSession(interleaved), sequence_start=30
        )
        self.assertEqual(provider.capture(timeout=1).pixels, payload)


class WatchCaptureProtocolValidationTest(unittest.TestCase):
    payload = bytes(range(12))

    def assert_stream_error(self, events: list[dict], pattern: str) -> None:
        serial = FakeSerialSession(lambda sequence: events)
        provider = WatchCaptureProvider(serial, sequence_start=41)
        with self.assertRaisesRegex(WatchCaptureProtocolError, pattern):
            provider.capture(timeout=1)

    def fresh(self) -> list[dict]:
        return make_stream(self.payload)

    @staticmethod
    def begin(events: list[dict]) -> dict:
        return next(event for event in events if event["type"] == "screenshot_begin")

    @staticmethod
    def chunks(events: list[dict]) -> list[dict]:
        return [event for event in events if event["type"] == "screenshot_chunk"]

    @staticmethod
    def end(events: list[dict]) -> dict:
        return next(event for event in events if event["type"] == "screenshot_end")

    def test_duplicate_missing_and_out_of_order_events_are_rejected(self) -> None:
        cases: list[tuple[str, list[dict], str]] = []

        duplicate_begin = self.fresh()
        duplicate_begin.insert(2, copy.deepcopy(self.begin(duplicate_begin)))
        cases.append(("duplicate begin", duplicate_begin, "duplicate screenshot_begin"))

        missing_begin = [
            event for event in self.fresh() if event["type"] != "screenshot_begin"
        ]
        cases.append(("missing begin", missing_begin, "before screenshot_begin"))

        duplicate_chunk = self.fresh()
        first_chunk = copy.deepcopy(self.chunks(duplicate_chunk)[0])
        duplicate_chunk.insert(3, first_chunk)
        cases.append(("duplicate chunk", duplicate_chunk, "duplicate.*index 0"))

        missing_chunk = self.fresh()
        missing_chunk.remove(self.chunks(missing_chunk)[-1])
        cases.append(("missing chunk", missing_chunk, "missing screenshot chunks"))

        out_of_order = self.fresh()
        chunk_positions = [
            index
            for index, event in enumerate(out_of_order)
            if event["type"] == "screenshot_chunk"
        ]
        out_of_order[chunk_positions[0]], out_of_order[chunk_positions[1]] = (
            out_of_order[chunk_positions[1]],
            out_of_order[chunk_positions[0]],
        )
        cases.append(("out of order", out_of_order, "out of order"))

        duplicate_end = self.fresh()
        duplicate_end.append(copy.deepcopy(self.end(duplicate_end)))
        cases.append(("duplicate end", duplicate_end, "duplicate screenshot_end"))

        chunk_after_end = self.fresh()
        chunk_after_end.append(copy.deepcopy(self.chunks(chunk_after_end)[0]))
        cases.append(("chunk after end", chunk_after_end, "after screenshot_end"))

        for name, events, pattern in cases:
            with self.subTest(name=name):
                self.assert_stream_error(events, pattern)

    def test_offset_base64_and_decoded_chunk_size_are_strict(self) -> None:
        bad_offset = self.fresh()
        self.chunks(bad_offset)[1]["offset"] += 1
        self.assert_stream_error(bad_offset, "offset must be")

        bad_base64 = self.fresh()
        self.chunks(bad_base64)[0]["data"] = "not+base64!"
        self.assert_stream_error(bad_base64, "invalid base64")

        empty_base64 = self.fresh()
        self.chunks(empty_base64)[0]["data"] = ""
        self.assert_stream_error(empty_base64, "non-empty base64")

        short_chunk = self.fresh()
        self.chunks(short_chunk)[0]["data"] = base64.b64encode(b"1234").decode()
        self.assert_stream_error(short_chunk, "decoded size must be 5")

    def test_geometry_layout_format_and_encoding_are_validated(self) -> None:
        mutations = (
            ("width", lambda begin: begin.update(width=0), "width must be"),
            ("height", lambda begin: begin.update(height=0), "height must be"),
            (
                "format",
                lambda begin: begin.update(pixel_format="jpeg"),
                "unsupported pixel_format",
            ),
            ("stride", lambda begin: begin.update(stride=5), "stride must be"),
            (
                "data size",
                lambda begin: begin.update(data_size=13),
                "decoded size|data_size must equal",
            ),
            ("chunk bytes", lambda begin: begin.update(chunk_bytes=0), "chunk_bytes"),
            ("chunks", lambda begin: begin.update(chunks=9), "chunks must be"),
            (
                "encoding",
                lambda begin: begin.update(encoding="hex"),
                "unsupported screenshot encoding",
            ),
            (
                "pixel source type",
                lambda begin: begin.update(pixel_source=123),
                "pixel_source must be text",
            ),
        )
        for name, mutate, pattern in mutations:
            with self.subTest(name=name):
                events = self.fresh()
                mutate(self.begin(events))
                self.assert_stream_error(events, pattern)

    def test_end_manifest_must_match_begin(self) -> None:
        mutations = (
            ("chunks", 99, "end chunks"),
            ("data_size", 99, "end data_size"),
            ("crc32", "00000000", "end crc32"),
        )
        for field, value, pattern in mutations:
            with self.subTest(field=field):
                events = self.fresh()
                self.end(events)[field] = value
                self.assert_stream_error(events, pattern)

    def test_payload_crc32_mismatch_is_rejected(self) -> None:
        events = self.fresh()
        self.begin(events)["crc32"] = "00000000"
        self.end(events)["crc32"] = "00000000"
        self.assert_stream_error(events, "payload CRC32 mismatch")

    def test_correlated_event_sequence_must_be_a_json_integer(self) -> None:
        events = self.fresh()
        self.chunks(events)[0]["seq"] = "41"
        self.assert_stream_error(events, "seq must be an integer")

    def test_crc32_requires_uint32_or_hex_text(self) -> None:
        for value in (-1, 0x1_0000_0000, "xyz", "123456789", True, None):
            with self.subTest(value=value):
                events = self.fresh()
                self.begin(events)["crc32"] = value
                self.end(events)["crc32"] = value
                self.assert_stream_error(events, "crc32")


class WatchCaptureBmpTest(unittest.TestCase):
    @staticmethod
    def bmp_pixels(path: Path) -> tuple[int, int, bytes]:
        content = path.read_bytes()
        if content[:2] != b"BM":
            raise AssertionError("not a BMP")
        pixel_offset = struct.unpack_from("<I", content, 10)[0]
        width, height = struct.unpack_from("<ii", content, 18)
        self_file_size = struct.unpack_from("<I", content, 2)[0]
        if self_file_size != len(content):
            raise AssertionError("BMP file size header is wrong")
        return width, height, content[pixel_offset:]

    def _capture(self, payload: bytes, pixel_format: str, stride: int):
        serial = FakeSerialSession(
            lambda sequence: make_stream(
                payload,
                sequence=sequence,
                pixel_format=pixel_format,
                stride=stride,
                chunk_bytes=4,
            )
        )
        return WatchCaptureProvider(serial, sequence_start=1).capture(timeout=1)

    def test_bgr888_is_copied_and_source_padding_is_ignored(self) -> None:
        # Source rows are top-to-bottom.  Each has two pixels and two bytes of
        # transport padding.  The BMP stores BGR rows bottom-to-top and uses
        # its own two-byte padding.
        expected_bmp_pixels = (
            b"\xff\x00\x00\xff\xff\xff\x00\x00"  # blue, white
            b"\x00\x00\xff\x00\xff\x00\x00\x00"  # red, green
        )
        payload = (
            b"\x00\x00\xff\x00\xff\x00\xaa\xbb"
            b"\xff\x00\x00\xff\xff\xff\xcc\xdd"
        )
        frame = self._capture(payload, "bgr888", stride=8)

        with tempfile.TemporaryDirectory() as root:
            output = Path(root) / "nested" / "frame.bmp"
            returned = frame.save_bmp(output)
            width, height, pixels = self.bmp_pixels(output)

        self.assertIs(returned, frame.metadata)
        self.assertEqual((width, height), (2, 2))
        self.assertEqual(pixels, expected_bmp_pixels)


if __name__ == "__main__":
    unittest.main()
