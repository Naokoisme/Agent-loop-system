"""Capture the watch display directly through ``HardwareSerialSession``.

The engineering firmware emits one JSON ``screenshot_begin`` event, ordered
base64 ``screenshot_chunk`` events, and one ``screenshot_end`` event.  This
module treats that stream as an untrusted transport: it validates the complete
layout and checksum before exposing a frame, then writes a plain 24-bit BMP
without relying on Pillow.

``WatchCaptureProvider`` borrows its serial session.  Calling :meth:`close`
only closes the provider; ownership of the serial port remains with the caller.
"""

from __future__ import annotations

import base64
import binascii
import math
import os
import struct
import threading
import time
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any


_REQUEST = "screenshot_capture"
_COMMAND = "SCREENSHOT_CAPTURE"
_UINT32_MAX = 0xFFFFFFFF
_MAX_DIMENSION = 16_384
_MAX_DATA_SIZE = 256 * 1024 * 1024
_PIXEL_FORMAT = "bgr888"


class WatchCaptureError(RuntimeError):
    """Base class for direct watch-capture failures."""


class WatchCaptureTimeoutError(WatchCaptureError, TimeoutError):
    """The watch did not finish a capture before the caller's deadline."""


class WatchCaptureDeviceError(WatchCaptureError):
    """The watch explicitly rejected or failed a capture request."""


class WatchCaptureProtocolError(WatchCaptureError):
    """The watch screenshot event stream was malformed or corrupted."""


class WatchCaptureSequenceError(WatchCaptureError, ValueError):
    """No strictly newer uint32 request sequence can be produced."""


@dataclass(frozen=True, slots=True)
class WatchCaptureMetadata:
    """Validated identity and layout of one watch frame."""

    sequence: int
    timestamp: float
    width: int
    height: int
    pixel_format: str
    data_size: int
    stride: int
    source: str
    transport: str
    payload_crc32: int
    chunk_bytes: int
    chunks: int
    encoding: str
    device_uptime_ms: int | None = None
    capture_duration_ms: int | None = None
    pixel_source: str | None = None


@dataclass(frozen=True, slots=True)
class WatchCaptureFrame:
    """One checksum-verified watch frame in its original pixel format."""

    pixels: bytes
    metadata: WatchCaptureMetadata

    def save_bmp(
        self, output_path: str | os.PathLike[str]
    ) -> WatchCaptureMetadata:
        """Convert the frame to a bottom-up 24-bit BMP and save it."""

        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)

        width = self.metadata.width
        height = self.metadata.height
        source_stride = self.metadata.stride
        bmp_stride = (width * 3 + 3) & ~3
        image_size = bmp_stride * height
        pixel_offset = 14 + 40
        file_size = pixel_offset + image_size

        file_header = struct.pack("<2sIHHI", b"BM", file_size, 0, 0, pixel_offset)
        info_header = struct.pack(
            "<IiiHHIIiiII",
            40,
            width,
            height,
            1,
            24,
            0,
            image_size,
            2_835,
            2_835,
            0,
            0,
        )
        padding = b"\x00" * (bmp_stride - width * 3)

        with path.open("wb") as stream:
            stream.write(file_header)
            stream.write(info_header)
            for row_index in range(height - 1, -1, -1):
                row_start = row_index * source_stride
                row = memoryview(self.pixels)[row_start : row_start + source_stride]
                stream.write(row[: width * 3])
                stream.write(padding)

        return self.metadata


def _protocol_error(detail: str) -> WatchCaptureProtocolError:
    return WatchCaptureProtocolError(f"invalid screenshot stream: {detail}")


def _required_int(
    event: dict[str, Any],
    field: str,
    *,
    minimum: int = 0,
    maximum: int | None = None,
) -> int:
    value = event.get(field)
    if isinstance(value, bool) or not isinstance(value, int):
        raise _protocol_error(f"{field} must be an integer, got {value!r}")
    if value < minimum or (maximum is not None and value > maximum):
        range_text = f">={minimum}" if maximum is None else f"{minimum}..{maximum}"
        raise _protocol_error(f"{field} must be {range_text}, got {value}")
    return value


def _optional_int(event: dict[str, Any], field: str) -> int | None:
    if field not in event:
        return None
    return _required_int(event, field)


def _crc32_value(event: dict[str, Any], field: str = "crc32") -> int:
    value = event.get(field)
    if isinstance(value, bool):
        raise _protocol_error(f"{field} must be a uint32 or hexadecimal string")
    if isinstance(value, int):
        if 0 <= value <= _UINT32_MAX:
            return value
        raise _protocol_error(f"{field} is outside uint32 range: {value}")
    if isinstance(value, str):
        text = value.strip().lower()
        if text.startswith("0x"):
            text = text[2:]
        if not 1 <= len(text) <= 8 or any(
            character not in "0123456789abcdef" for character in text
        ):
            raise _protocol_error(f"{field} is not a uint32 hexadecimal value: {value!r}")
        return int(text, 16)
    raise _protocol_error(f"{field} must be a uint32 or hexadecimal string")


def _timestamp_from_begin(begin: dict[str, Any]) -> float:
    if "timestamp" in begin:
        value = begin["timestamp"]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise _protocol_error(f"timestamp must be numeric, got {value!r}")
        timestamp = float(value)
        if not math.isfinite(timestamp) or timestamp < 0:
            raise _protocol_error(f"timestamp must be finite and non-negative, got {value!r}")
        return timestamp
    for field in ("timestamp_ms", "device_uptime_ms"):
        if field in begin:
            return _required_int(begin, field) / 1000.0
    return time.time()


def _device_error(event: dict[str, Any]) -> WatchCaptureDeviceError:
    status = str(event.get("status", "unknown"))
    reason = event.get("reason")
    detail = f"watch screenshot capture ended with status {status!r}"
    if reason not in (None, ""):
        detail += f": {reason}"
    return WatchCaptureDeviceError(detail)


class WatchCaptureProvider:
    """Capture checksum-verified display pixels over a borrowed serial session."""

    def __init__(self, serial_session: Any, *, sequence_start: int | None = None) -> None:
        if serial_session is None:
            raise ValueError("serial_session is required")
        if sequence_start is None:
            # Stay in the positive signed range so logs remain convenient on
            # firmware builds that still print sequences through signed tools.
            sequence_start = time.time_ns() % 2_147_483_647 or 1
        if (
            isinstance(sequence_start, bool)
            or not isinstance(sequence_start, int)
            or not 0 <= sequence_start <= _UINT32_MAX
        ):
            raise ValueError("sequence_start must be a uint32")
        self.serial_session = serial_session
        self._next_sequence = sequence_start
        self._last_sequence: int | None = None
        self._closed = False
        self._lock = threading.Lock()

    @property
    def closed(self) -> bool:
        return self._closed

    def _allocate_sequence(self, after_sequence: int | None) -> int:
        if after_sequence is not None:
            if (
                isinstance(after_sequence, bool)
                or not isinstance(after_sequence, int)
                or not 0 <= after_sequence <= _UINT32_MAX
            ):
                raise ValueError("after_sequence must be a uint32 or None")

        floor = -1 if self._last_sequence is None else self._last_sequence
        if after_sequence is not None:
            floor = max(floor, after_sequence)
        sequence = max(self._next_sequence, floor + 1)
        if sequence > _UINT32_MAX:
            raise WatchCaptureSequenceError(
                "cannot allocate a screenshot sequence newer than after_sequence"
            )
        self._next_sequence = sequence + 1
        return sequence

    def capture(
        self,
        *,
        timeout: float,
        after_sequence: int | None = None,
    ) -> WatchCaptureFrame:
        """Request and validate one frame newer than ``after_sequence``."""

        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
            raise ValueError("timeout must be a positive finite number")
        timeout_value = float(timeout)
        if not math.isfinite(timeout_value) or timeout_value <= 0:
            raise ValueError("timeout must be a positive finite number")

        with self._lock:
            if self._closed:
                raise WatchCaptureError("watch capture provider is closed")
            sequence = self._allocate_sequence(after_sequence)
            deadline = time.monotonic() + timeout_value
            start_event_index = self.serial_session.event_count

            try:
                result = self.serial_session.send(
                    f":{_COMMAND}:{sequence}",
                    request=_REQUEST,
                    seq=sequence,
                    timeout=max(0.0, deadline - time.monotonic()),
                    expected_type="screenshot_end",
                    expected_status=("complete", "error", "busy", "unavailable"),
                )
            except TimeoutError as exc:
                raise WatchCaptureTimeoutError(
                    f"watch screenshot capture {sequence} timed out after {timeout_value}s"
                ) from exc

            terminal = getattr(result, "raw", None)
            if not isinstance(terminal, dict):
                raise _protocol_error("serial send returned no terminal event")
            terminal_type = str(terminal.get("type", "")).lower()
            if terminal_type != "screenshot_end":
                raise _device_error(terminal)

            if str(terminal.get("status", "")).lower() != "complete":
                raise _device_error(terminal)

            events = self.serial_session.events_since(start_event_index)
            frame = self._assemble(sequence, events)
            if frame.metadata.sequence <= (
                -1 if after_sequence is None else after_sequence
            ):
                raise WatchCaptureSequenceError(
                    f"capture sequence {frame.metadata.sequence} is not newer than "
                    f"after_sequence {after_sequence}"
                )
            if self._last_sequence is not None and (
                frame.metadata.sequence <= self._last_sequence
            ):
                raise WatchCaptureSequenceError(
                    f"capture sequence {frame.metadata.sequence} did not increase from "
                    f"{self._last_sequence}"
                )
            self._last_sequence = frame.metadata.sequence
            return frame

    @staticmethod
    def _assemble(sequence: int, events: list[dict[str, Any]]) -> WatchCaptureFrame:
        relevant: list[dict[str, Any]] = []
        for event in events:
            if not isinstance(event, dict):
                raise _protocol_error(
                    f"events_since returned {type(event).__name__}, expected dict"
                )
            if str(event.get("request", "")).lower() != _REQUEST:
                continue
            event_sequence = event.get("seq")
            if isinstance(event_sequence, bool) or not isinstance(
                event_sequence, int
            ):
                if str(event_sequence) == str(sequence):
                    raise _protocol_error(
                        f"seq must be an integer, got {event_sequence!r}"
                    )
                continue
            if event_sequence != sequence:
                continue
            relevant.append(event)

        begin: dict[str, Any] | None = None
        end: dict[str, Any] | None = None
        payload = bytearray()
        seen_indices: set[int] = set()
        expected_index = 0
        command_result_seen = False

        for event in relevant:
            event_type = str(event.get("type", "")).lower()
            if event_type == "command_result":
                if command_result_seen:
                    raise _protocol_error("duplicate command_result")
                if begin is not None or end is not None:
                    raise _protocol_error("command_result appeared after screenshot data")
                if str(event.get("status", "")).lower() != "accepted":
                    raise _device_error(event)
                command_result_seen = True
                continue
            if event_type == "screenshot_begin":
                if begin is not None:
                    raise _protocol_error("duplicate screenshot_begin")
                if end is not None or payload:
                    raise _protocol_error("screenshot_begin is out of order")
                if str(event.get("status", "")).lower() != "ok":
                    raise _device_error(event)
                begin = event
                continue
            if event_type == "screenshot_chunk":
                if begin is None:
                    raise _protocol_error("screenshot_chunk appeared before screenshot_begin")
                if end is not None:
                    raise _protocol_error("screenshot_chunk appeared after screenshot_end")
                index = _required_int(event, "index")
                if index in seen_indices:
                    raise _protocol_error(f"duplicate screenshot_chunk index {index}")
                if index != expected_index:
                    raise _protocol_error(
                        f"screenshot_chunk index is out of order: expected "
                        f"{expected_index}, got {index}"
                    )
                offset = _required_int(event, "offset")
                if offset != len(payload):
                    raise _protocol_error(
                        f"screenshot_chunk {index} offset must be {len(payload)}, got {offset}"
                    )
                encoded = event.get("data")
                if not isinstance(encoded, str) or not encoded:
                    raise _protocol_error(
                        f"screenshot_chunk {index} data must be non-empty base64 text"
                    )
                try:
                    decoded = base64.b64decode(encoded, validate=True)
                except (binascii.Error, ValueError) as exc:
                    raise _protocol_error(
                        f"screenshot_chunk {index} contains invalid base64"
                    ) from exc
                chunk_bytes = _required_int(begin, "chunk_bytes", minimum=1)
                data_size = _required_int(
                    begin, "data_size", minimum=1, maximum=_MAX_DATA_SIZE
                )
                expected_length = min(chunk_bytes, data_size - offset)
                if expected_length <= 0 or len(decoded) != expected_length:
                    raise _protocol_error(
                        f"screenshot_chunk {index} decoded size must be "
                        f"{max(expected_length, 0)}, got {len(decoded)}"
                    )
                payload.extend(decoded)
                seen_indices.add(index)
                expected_index += 1
                continue
            if event_type == "screenshot_end":
                if end is not None:
                    raise _protocol_error("duplicate screenshot_end")
                if begin is None:
                    raise _protocol_error("screenshot_end appeared before screenshot_begin")
                end = event
                continue
            raise _protocol_error(f"unexpected event type {event_type!r}")

        if begin is None:
            raise _protocol_error("missing screenshot_begin")
        if end is None:
            raise _protocol_error("missing screenshot_end")
        if str(end.get("status", "")).lower() != "complete":
            raise _device_error(end)

        width = _required_int(begin, "width", minimum=1, maximum=_MAX_DIMENSION)
        height = _required_int(begin, "height", minimum=1, maximum=_MAX_DIMENSION)
        pixel_format = begin.get("pixel_format")
        if not isinstance(pixel_format, str):
            raise _protocol_error("pixel_format must be text")
        pixel_format = pixel_format.lower()
        if pixel_format != _PIXEL_FORMAT:
            raise _protocol_error(f"unsupported pixel_format {pixel_format!r}")
        stride = _required_int(begin, "stride", minimum=1, maximum=_MAX_DATA_SIZE)
        minimum_stride = width * 3
        if stride < minimum_stride:
            raise _protocol_error(
                f"stride must be at least {minimum_stride} for {pixel_format}, got {stride}"
            )
        data_size = _required_int(
            begin, "data_size", minimum=1, maximum=_MAX_DATA_SIZE
        )
        if data_size != stride * height:
            raise _protocol_error(
                f"data_size must equal stride * height ({stride * height}), got {data_size}"
            )
        chunk_bytes = _required_int(begin, "chunk_bytes", minimum=1)
        chunks = _required_int(begin, "chunks", minimum=1)
        expected_chunks = (data_size + chunk_bytes - 1) // chunk_bytes
        if chunks != expected_chunks:
            raise _protocol_error(
                f"chunks must be {expected_chunks} for data_size/chunk_bytes, got {chunks}"
            )
        encoding_value = begin.get("encoding", "base64")
        if not isinstance(encoding_value, str) or encoding_value.lower() != "base64":
            raise _protocol_error(f"unsupported screenshot encoding {encoding_value!r}")

        begin_crc = _crc32_value(begin)
        if _required_int(end, "chunks", minimum=0) != chunks:
            raise _protocol_error("screenshot_end chunks does not match screenshot_begin")
        if _required_int(end, "data_size", minimum=0) != data_size:
            raise _protocol_error("screenshot_end data_size does not match screenshot_begin")
        if _crc32_value(end) != begin_crc:
            raise _protocol_error("screenshot_end crc32 does not match screenshot_begin")
        if len(payload) != data_size:
            raise _protocol_error(
                f"missing screenshot chunks: assembled {len(payload)} of {data_size} bytes"
            )
        if len(seen_indices) != chunks:
            raise _protocol_error(
                f"missing screenshot chunks: received {len(seen_indices)} of {chunks}"
            )
        payload_crc = zlib.crc32(payload) & _UINT32_MAX
        if payload_crc != begin_crc:
            raise _protocol_error(
                f"payload CRC32 mismatch: expected {begin_crc:08x}, got {payload_crc:08x}"
            )

        begin_sequence = _required_int(begin, "seq", maximum=_UINT32_MAX)
        end_sequence = _required_int(end, "seq", maximum=_UINT32_MAX)
        if begin_sequence != sequence or end_sequence != sequence:
            raise _protocol_error("screenshot event sequence changed within the stream")

        pixel_source = begin.get("pixel_source")
        if pixel_source is not None and not isinstance(pixel_source, str):
            raise _protocol_error(
                f"pixel_source must be text when present, got {pixel_source!r}"
            )

        metadata = WatchCaptureMetadata(
            sequence=sequence,
            timestamp=_timestamp_from_begin(begin),
            width=width,
            height=height,
            pixel_format=pixel_format,
            data_size=data_size,
            stride=stride,
            source="watch_display",
            transport="hardware_serial",
            payload_crc32=payload_crc,
            chunk_bytes=chunk_bytes,
            chunks=chunks,
            encoding="base64",
            device_uptime_ms=_optional_int(begin, "device_uptime_ms"),
            capture_duration_ms=_optional_int(begin, "capture_duration_ms"),
            pixel_source=pixel_source,
        )
        return WatchCaptureFrame(pixels=bytes(payload), metadata=metadata)

    def close(self) -> None:
        """Mark this provider closed without touching the borrowed serial session."""

        with self._lock:
            self._closed = True


__all__ = [
    "WatchCaptureDeviceError",
    "WatchCaptureError",
    "WatchCaptureFrame",
    "WatchCaptureMetadata",
    "WatchCaptureProtocolError",
    "WatchCaptureProvider",
    "WatchCaptureSequenceError",
    "WatchCaptureTimeoutError",
]
