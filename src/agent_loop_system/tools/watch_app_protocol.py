"""6202 watch App authentication wire protocol.

The firmware carries protobuf payloads in a fixed ten-byte ``0xAB`` envelope.
This module deliberately contains no BLE code so the same codec can be tested
without hardware and reused by any later transport.
"""

from __future__ import annotations

import math
import struct
import time
from dataclasses import dataclass, field
from datetime import datetime

from google.protobuf.message import DecodeError

from agent_loop_system.protocol import watch_app_pb2


BIND_COMMAND = 0x0301
LOGIN_COMMAND = 0x0302
FRAME_MAGIC = 0xAB
FRAME_HEADER_SIZE = 10
MAX_PAYLOAD_LENGTH = 2048
_EPOCH_2000_UNIX_SECONDS = 946_684_800
_INT32_MIN = -(1 << 31)
_INT32_MAX = (1 << 31) - 1
_INT64_MAX = (1 << 63) - 1
_AUTH_RESULTS = frozenset((*range(8), 127))


class WatchAppProtocolError(ValueError):
    """The watch App payload or frame is invalid."""


def _validate_int(name: str, value: int, minimum: int, maximum: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise WatchAppProtocolError(f"{name} must be an integer")
    if not minimum <= value <= maximum:
        raise WatchAppProtocolError(
            f"{name} must be between {minimum} and {maximum}"
        )


def _validate_text(
    name: str, value: str, max_bytes: int, *, allow_empty: bool = True
) -> None:
    if not isinstance(value, str):
        raise WatchAppProtocolError(f"{name} must be a string")
    if not allow_empty and not value:
        raise WatchAppProtocolError(f"{name} must not be empty")
    if "\x00" in value:
        raise WatchAppProtocolError(f"{name} must not contain NUL")
    try:
        encoded_length = len(value.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise WatchAppProtocolError(f"{name} must be valid UTF-8 text") from exc
    if encoded_length > max_bytes:
        raise WatchAppProtocolError(
            f"{name} is {encoded_length} UTF-8 bytes; firmware limit is {max_bytes}"
        )


@dataclass(frozen=True, slots=True)
class WatchAppFrame:
    """One protobuf command in the firmware's ten-byte envelope."""

    command: int
    sequence: int
    payload: bytes
    flags: int = 0

    def __post_init__(self) -> None:
        _validate_int("command", self.command, 0, 0xFFFF)
        _validate_int("sequence", self.sequence, 0, 0xFFFF)
        _validate_int("flags", self.flags, 0, 0xFF)
        if not isinstance(self.payload, bytes):
            raise WatchAppProtocolError("payload must be bytes")
        if len(self.payload) > MAX_PAYLOAD_LENGTH:
            raise WatchAppProtocolError(
                f"payload exceeds firmware limit of {MAX_PAYLOAD_LENGTH} bytes"
            )


def crc16_ibm(data: bytes) -> int:
    """Return the firmware CRC-16/IBM value (init 0, polynomial 0xA001)."""

    try:
        view = memoryview(data).cast("B")
    except (TypeError, ValueError) as exc:
        raise WatchAppProtocolError("CRC input must be bytes-like") from exc

    crc = 0
    for value in view:
        crc ^= value
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


def encode_frame(frame: WatchAppFrame) -> bytes:
    """Encode one frame exactly as ``srv_com_pack_data`` does."""

    if not isinstance(frame, WatchAppFrame):
        raise WatchAppProtocolError("frame must be a WatchAppFrame")
    payload_length = len(frame.payload)
    header = struct.pack(
        ">BBBBHHH",
        FRAME_MAGIC,
        frame.command >> 8,
        frame.command & 0xFF,
        frame.flags,
        payload_length,
        crc16_ibm(frame.payload),
        frame.sequence,
    )
    return header + frame.payload


def decode_frame(data: bytes) -> WatchAppFrame:
    """Strictly decode exactly one complete frame."""

    try:
        raw = memoryview(data).cast("B").tobytes()
    except (TypeError, ValueError) as exc:
        raise WatchAppProtocolError("frame data must be bytes-like") from exc

    if len(raw) < FRAME_HEADER_SIZE:
        raise WatchAppProtocolError("frame is shorter than the 10-byte header")
    if raw[0] != FRAME_MAGIC:
        raise WatchAppProtocolError(f"invalid frame magic 0x{raw[0]:02X}")

    payload_length = int.from_bytes(raw[4:6], "big")
    if payload_length > MAX_PAYLOAD_LENGTH:
        raise WatchAppProtocolError(
            f"payload length {payload_length} exceeds firmware limit "
            f"of {MAX_PAYLOAD_LENGTH}"
        )
    expected_length = FRAME_HEADER_SIZE + payload_length
    if len(raw) != expected_length:
        raise WatchAppProtocolError(
            f"frame length is {len(raw)} bytes; header requires {expected_length}"
        )

    payload = raw[FRAME_HEADER_SIZE:]
    expected_crc = int.from_bytes(raw[6:8], "big")
    actual_crc = crc16_ibm(payload)
    if actual_crc != expected_crc:
        raise WatchAppProtocolError(
            f"CRC mismatch: frame=0x{expected_crc:04X}, calculated=0x{actual_crc:04X}"
        )

    return WatchAppFrame(
        command=(raw[1] << 8) | raw[2],
        sequence=int.from_bytes(raw[8:10], "big"),
        payload=payload,
        flags=raw[3],
    )


class WatchAppFrameDecoder:
    """Incrementally decode fragmented BLE notifications into complete frames."""

    __slots__ = ("_buffer",)

    def __init__(self) -> None:
        self._buffer = bytearray()

    def feed(self, data: bytes) -> list[WatchAppFrame]:
        """Consume bytes and return all frames completed by this call.

        Bytes before the next ``0xAB`` magic byte are ignored. A complete frame
        with a bad CRC is removed from the buffer and reported explicitly.
        """

        try:
            chunk = memoryview(data).cast("B").tobytes()
        except (TypeError, ValueError) as exc:
            raise WatchAppProtocolError("decoder input must be bytes-like") from exc
        self._buffer.extend(chunk)

        frames: list[WatchAppFrame] = []
        while self._buffer:
            magic_index = self._buffer.find(FRAME_MAGIC)
            if magic_index < 0:
                self._buffer.clear()
                break
            if magic_index:
                del self._buffer[:magic_index]
            if len(self._buffer) < FRAME_HEADER_SIZE:
                break

            payload_length = int.from_bytes(self._buffer[4:6], "big")
            if payload_length > MAX_PAYLOAD_LENGTH:
                # This cannot be a firmware frame. Drop only the false magic so
                # a valid frame later in the same notification can be found.
                del self._buffer[0]
                continue
            frame_length = FRAME_HEADER_SIZE + payload_length
            if len(self._buffer) < frame_length:
                break

            candidate = bytes(self._buffer[:frame_length])
            del self._buffer[:frame_length]
            frames.append(decode_frame(candidate))

        return frames


@dataclass(frozen=True, slots=True)
class WatchUserProfile:
    gender: int = 1
    age: int = 30
    height_cm: int = 170
    weight_kg: float = 65.0

    def __post_init__(self) -> None:
        _validate_int("gender", self.gender, 0, 1)
        _validate_int("age", self.age, 1, 120)
        _validate_int("height_cm", self.height_cm, 50, 250)
        if (
            isinstance(self.weight_kg, bool)
            or not isinstance(self.weight_kg, (int, float))
            or not math.isfinite(self.weight_kg)
        ):
            raise WatchAppProtocolError("weight_kg must be a finite number")
        if not 20 <= self.weight_kg <= 300:
            raise WatchAppProtocolError(
                "weight_kg must be between 20 and 300"
            )


@dataclass(frozen=True, slots=True)
class WatchAuthRequest:
    user_id: str
    auth_code: str
    version: str = "Windows"
    brand: str = "Agent-loop"
    model: str = "Windows"
    user: WatchUserProfile = field(default_factory=WatchUserProfile)
    timestamp_2000: int | None = None
    zone_offset_seconds: int | None = None

    def __post_init__(self) -> None:
        _validate_text("user_id", self.user_id, 32, allow_empty=False)
        _validate_text("auth_code", self.auth_code, 16)
        _validate_text("version", self.version, 16)
        _validate_text("brand", self.brand, 16)
        _validate_text("model", self.model, 16)
        if not isinstance(self.user, WatchUserProfile):
            raise WatchAppProtocolError("user must be a WatchUserProfile")
        if self.timestamp_2000 is not None:
            _validate_int(
                "timestamp_2000", self.timestamp_2000, _INT32_MIN, _INT32_MAX
            )
        if self.zone_offset_seconds is not None:
            _validate_int(
                "zone_offset_seconds",
                self.zone_offset_seconds,
                _INT32_MIN,
                _INT32_MAX,
            )


@dataclass(frozen=True, slots=True)
class WatchAuthResponse:
    result: int
    ever_bound: int
    bind_time: int

    def __post_init__(self) -> None:
        _validate_int("result", self.result, _INT32_MIN, _INT32_MAX)
        if self.result not in _AUTH_RESULTS:
            raise WatchAppProtocolError(
                f"result must be one of {sorted(_AUTH_RESULTS)}"
            )
        _validate_int("ever_bound", self.ever_bound, 0, 1)
        _validate_int("bind_time", self.bind_time, 0, _INT64_MAX)


def _local_zone_offset_seconds() -> int:
    offset = datetime.now().astimezone().utcoffset()
    return 0 if offset is None else int(offset.total_seconds())


def encode_auth_request(request: WatchAuthRequest) -> bytes:
    """Encode the firmware ``_AuthRequest`` with iOS/BLE-only system value 0."""

    if not isinstance(request, WatchAuthRequest):
        raise WatchAppProtocolError("request must be a WatchAuthRequest")

    timestamp = request.timestamp_2000
    if timestamp is None:
        timestamp = int(time.time()) - _EPOCH_2000_UNIX_SECONDS
    _validate_int("timestamp_2000", timestamp, _INT32_MIN, _INT32_MAX)

    zone_offset = request.zone_offset_seconds
    if zone_offset is None:
        zone_offset = _local_zone_offset_seconds()
    _validate_int("zone_offset_seconds", zone_offset, _INT32_MIN, _INT32_MAX)

    message = watch_app_pb2._AuthRequest()
    message.userId = request.user_id
    message.system = 0
    message.version = request.version
    message.brand = request.brand
    message.model = request.model
    message.authCode = request.auth_code
    message.user.gender = request.user.gender
    message.user.age = request.user.age
    message.user.height = request.user.height_cm
    message.user.weight = request.user.weight_kg
    message.time.timestamp = timestamp
    message.time.zoneOffset = zone_offset
    try:
        return message.SerializeToString(deterministic=True)
    except (TypeError, ValueError) as exc:
        raise WatchAppProtocolError(f"cannot encode auth request: {exc}") from exc


def decode_auth_response(data: bytes) -> WatchAuthResponse:
    """Decode the firmware ``_AuthResponse`` protobuf payload."""

    try:
        raw = memoryview(data).cast("B").tobytes()
    except (TypeError, ValueError) as exc:
        raise WatchAppProtocolError("auth response must be bytes-like") from exc

    message = watch_app_pb2._AuthResponse()
    try:
        message.ParseFromString(raw)
    except DecodeError as exc:
        raise WatchAppProtocolError(f"invalid auth response protobuf: {exc}") from exc
    return WatchAuthResponse(
        result=message.result,
        ever_bound=message.everBind,
        bind_time=message.bindTime,
    )
