"""Binary protocol for engineering screenshots transported over watch BLE.

The firmware carries these messages inside the existing ten-byte watch App
frame.  This module owns only the screenshot payload and reconstruction rules;
it deliberately has no Bleak dependency.
"""

from __future__ import annotations

import os
import struct
import tempfile
import zlib
from dataclasses import dataclass
from pathlib import Path


BLE_SCREENSHOT_COMMAND = 0x7F01
BLE_SCREENSHOT_PROTOCOL_VERSION = 1
BLE_SCREENSHOT_CHUNK_SIZE = 960
BLE_SCREENSHOT_MAX_FILE_SIZE = 1024 * 1024

BLE_SCREENSHOT_REQUEST = 0
BLE_SCREENSHOT_START = 1
BLE_SCREENSHOT_DATA = 2
BLE_SCREENSHOT_END = 3
BLE_SCREENSHOT_ERROR = 4

EXPECTED_SCREENSHOT_WIDTH = 410
EXPECTED_SCREENSHOT_HEIGHT = 502

_REQUEST = struct.Struct("<BBI")
_START = struct.Struct("<BBIIHH")
_DATA = struct.Struct("<BBIHIH")
_END = struct.Struct("<BBIIIH")
_ERROR = struct.Struct("<BBIH")

REMOTE_ERROR_NAMES = {
    1: "invalid_request",
    2: "busy",
    3: "capture_rejected",
    4: "capture_file_timeout",
    5: "capture_file_io",
    6: "ble_send_timeout",
    7: "invalid_bmp",
    8: "request_queue_full",
}


class WatchBleScreenshotProtocolError(ValueError):
    """The screenshot payload stream violated the engineering protocol."""


class WatchBleScreenshotRemoteError(WatchBleScreenshotProtocolError):
    """The watch reported a screenshot failure."""

    def __init__(self, sequence: int, code: int) -> None:
        self.sequence = sequence
        self.code = code
        self.reason = REMOTE_ERROR_NAMES.get(code, "unknown")
        super().__init__(
            f"watch screenshot failed: sequence={sequence}, "
            f"code={code}, reason={self.reason}"
        )


@dataclass(frozen=True, slots=True)
class WatchBleScreenshot:
    """A fully reconstructed and validated watch screenshot."""

    sequence: int
    data: bytes
    crc32: int
    payload_crc32: int
    chunk_size: int
    chunk_count: int
    width: int
    height: int
    top_down: bool

    @property
    def file_size(self) -> int:
        return len(self.data)


def _validate_uint32(name: str, value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise WatchBleScreenshotProtocolError(f"{name} must be an integer")
    if not 0 <= value <= 0xFFFFFFFF:
        raise WatchBleScreenshotProtocolError(
            f"{name} must be between 0 and 4294967295"
        )


def encode_screenshot_request(sequence: int) -> bytes:
    """Encode one request for command ``0x7F01``."""

    _validate_uint32("sequence", sequence)
    return _REQUEST.pack(
        BLE_SCREENSHOT_PROTOCOL_VERSION,
        BLE_SCREENSHOT_REQUEST,
        sequence,
    )


def _validate_bmp(data: bytes) -> tuple[int, int, bool, int]:
    if len(data) < 54 or data[:2] != b"BM":
        raise WatchBleScreenshotProtocolError("screenshot is not a BMP file")

    declared_size = struct.unpack_from("<I", data, 2)[0]
    pixel_offset = struct.unpack_from("<I", data, 10)[0]
    dib_size = struct.unpack_from("<I", data, 14)[0]
    width, signed_height = struct.unpack_from("<ii", data, 18)
    planes, bits_per_pixel = struct.unpack_from("<HH", data, 26)
    compression = struct.unpack_from("<I", data, 30)[0]

    if declared_size != len(data):
        raise WatchBleScreenshotProtocolError(
            f"BMP header size {declared_size} does not match {len(data)} bytes"
        )
    if dib_size < 40 or pixel_offset < 54 or pixel_offset > len(data):
        raise WatchBleScreenshotProtocolError("BMP header layout is invalid")
    if width != EXPECTED_SCREENSHOT_WIDTH or signed_height != -EXPECTED_SCREENSHOT_HEIGHT:
        raise WatchBleScreenshotProtocolError(
            "BMP dimensions must be 410x502 in top-down layout"
        )
    if planes != 1 or bits_per_pixel != 24 or compression != 0:
        raise WatchBleScreenshotProtocolError(
            "BMP must be uncompressed 24-bit BGR"
        )

    stride = ((width * 3 + 3) // 4) * 4
    expected_size = pixel_offset + stride * EXPECTED_SCREENSHOT_HEIGHT
    if expected_size != len(data):
        raise WatchBleScreenshotProtocolError(
            f"BMP pixel payload requires {expected_size} bytes, got {len(data)}"
        )
    row_bytes = width * 3
    payload_crc32 = 0
    for row in range(EXPECTED_SCREENSHOT_HEIGHT):
        start = pixel_offset + row * stride
        payload_crc32 = zlib.crc32(data[start : start + row_bytes], payload_crc32)
    return width, EXPECTED_SCREENSHOT_HEIGHT, True, payload_crc32 & 0xFFFFFFFF


class WatchBleScreenshotAssembler:
    """Strictly assemble START -> ordered DATA -> END messages."""

    __slots__ = (
        "sequence",
        "_buffer",
        "_chunk_count",
        "_chunk_size",
        "_file_size",
        "_finished",
        "_next_chunk",
    )

    def __init__(self, sequence: int) -> None:
        _validate_uint32("sequence", sequence)
        self.sequence = sequence
        self._buffer: bytearray | None = None
        self._chunk_count = 0
        self._chunk_size = 0
        self._file_size = 0
        self._next_chunk = 0
        self._finished = False

    def feed(self, payload: bytes) -> WatchBleScreenshot | None:
        """Consume one screenshot payload; return the image after a valid END."""

        try:
            raw = memoryview(payload).cast("B").tobytes()
        except (TypeError, ValueError) as exc:
            raise WatchBleScreenshotProtocolError(
                "screenshot payload must be bytes-like"
            ) from exc
        if len(raw) < _REQUEST.size:
            raise WatchBleScreenshotProtocolError("screenshot payload is too short")

        version, message_type, sequence = _REQUEST.unpack_from(raw)
        if version != BLE_SCREENSHOT_PROTOCOL_VERSION:
            raise WatchBleScreenshotProtocolError(
                f"unsupported screenshot protocol version {version}"
            )
        if sequence != self.sequence:
            # A late packet from an earlier request must not corrupt the active
            # reconstruction. There is only one active capture per connection.
            return None
        if self._finished:
            raise WatchBleScreenshotProtocolError("screenshot message arrived after END")

        if message_type == BLE_SCREENSHOT_ERROR:
            if len(raw) != _ERROR.size:
                raise WatchBleScreenshotProtocolError(
                    "screenshot ERROR payload has an invalid length"
                )
            _, _, _, code = _ERROR.unpack(raw)
            raise WatchBleScreenshotRemoteError(sequence, code)
        if message_type == BLE_SCREENSHOT_START:
            return self._feed_start(raw)
        if message_type == BLE_SCREENSHOT_DATA:
            return self._feed_data(raw)
        if message_type == BLE_SCREENSHOT_END:
            return self._feed_end(raw)
        raise WatchBleScreenshotProtocolError(
            f"unexpected screenshot message type {message_type}"
        )

    def _feed_start(self, raw: bytes) -> None:
        if len(raw) != _START.size:
            raise WatchBleScreenshotProtocolError(
                "screenshot START payload has an invalid length"
            )
        if self._buffer is not None:
            raise WatchBleScreenshotProtocolError("duplicate screenshot START")
        _, _, _, file_size, chunk_size, chunk_count = _START.unpack(raw)
        if not 54 <= file_size <= BLE_SCREENSHOT_MAX_FILE_SIZE:
            raise WatchBleScreenshotProtocolError(
                f"screenshot file size {file_size} is outside the accepted range"
            )
        if chunk_size != BLE_SCREENSHOT_CHUNK_SIZE:
            raise WatchBleScreenshotProtocolError(
                f"unexpected screenshot chunk size {chunk_size}"
            )
        expected_count = (file_size + chunk_size - 1) // chunk_size
        if chunk_count != expected_count:
            raise WatchBleScreenshotProtocolError(
                f"START declares {chunk_count} chunks; {expected_count} are required"
            )
        self._file_size = file_size
        self._chunk_size = chunk_size
        self._chunk_count = chunk_count
        self._buffer = bytearray()
        return None

    def _feed_data(self, raw: bytes) -> None:
        if self._buffer is None:
            raise WatchBleScreenshotProtocolError("screenshot DATA arrived before START")
        if len(raw) < _DATA.size:
            raise WatchBleScreenshotProtocolError(
                "screenshot DATA payload is shorter than its header"
            )
        _, _, _, index, offset, data_length = _DATA.unpack_from(raw)
        if len(raw) != _DATA.size + data_length:
            raise WatchBleScreenshotProtocolError(
                "screenshot DATA length does not match its header"
            )
        if index != self._next_chunk:
            raise WatchBleScreenshotProtocolError(
                f"screenshot chunk index {index} arrived; expected {self._next_chunk}"
            )
        if offset != len(self._buffer):
            raise WatchBleScreenshotProtocolError(
                f"screenshot chunk offset {offset} arrived; expected {len(self._buffer)}"
            )
        remaining = self._file_size - offset
        expected_length = min(self._chunk_size, remaining)
        if data_length != expected_length:
            raise WatchBleScreenshotProtocolError(
                f"screenshot chunk {index} has {data_length} bytes; "
                f"expected {expected_length}"
            )
        self._buffer.extend(raw[_DATA.size :])
        self._next_chunk += 1
        return None

    def _feed_end(self, raw: bytes) -> WatchBleScreenshot:
        if len(raw) != _END.size:
            raise WatchBleScreenshotProtocolError(
                "screenshot END payload has an invalid length"
            )
        if self._buffer is None:
            raise WatchBleScreenshotProtocolError("screenshot END arrived before START")
        _, _, _, file_size, expected_crc, chunk_count = _END.unpack(raw)
        if file_size != self._file_size or chunk_count != self._chunk_count:
            raise WatchBleScreenshotProtocolError(
                "screenshot END metadata does not match START"
            )
        if self._next_chunk != self._chunk_count:
            raise WatchBleScreenshotProtocolError(
                f"screenshot ended after {self._next_chunk} of "
                f"{self._chunk_count} chunks"
            )
        if len(self._buffer) != self._file_size:
            raise WatchBleScreenshotProtocolError(
                f"screenshot ended at {len(self._buffer)} of {self._file_size} bytes"
            )

        data = bytes(self._buffer)
        actual_crc = zlib.crc32(data) & 0xFFFFFFFF
        if actual_crc != expected_crc:
            raise WatchBleScreenshotProtocolError(
                f"screenshot CRC32 mismatch: watch=0x{expected_crc:08x}, "
                f"PC=0x{actual_crc:08x}"
            )
        width, height, top_down, payload_crc32 = _validate_bmp(data)
        self._finished = True
        return WatchBleScreenshot(
            sequence=self.sequence,
            data=data,
            crc32=actual_crc,
            payload_crc32=payload_crc32,
            chunk_size=self._chunk_size,
            chunk_count=self._chunk_count,
            width=width,
            height=height,
            top_down=top_down,
        )


def write_verified_screenshot(
    screenshot: WatchBleScreenshot,
    output_path: str | os.PathLike[str],
) -> Path:
    """Atomically write a screenshot that has already passed all checks."""

    if not isinstance(screenshot, WatchBleScreenshot):
        raise TypeError("screenshot must be a WatchBleScreenshot")
    output = Path(output_path).expanduser().resolve()
    if output.exists() and output.is_dir():
        raise IsADirectoryError(output)
    output.parent.mkdir(parents=True, exist_ok=True)

    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=f".{output.name}.",
            suffix=".tmp",
            dir=output.parent,
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            stream.write(screenshot.data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, output)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return output
