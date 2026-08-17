"""Transport-independent codec for the watch new-platform PB protocol.

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
from enum import IntFlag
from typing import Hashable, TypeVar

from google.protobuf.descriptor import FieldDescriptor
from google.protobuf.message import DecodeError, EncodeError, Message

from agent_loop_system.protocol import watch_app_pb2
from agent_loop_system.protocol.pb_commands import (
    EnvironmentKey,
    PbCommandGroup,
    command_id,
)


BIND_COMMAND = command_id(PbCommandGroup.ENVIRONMENT, EnvironmentKey.BIND)
LOGIN_COMMAND = command_id(PbCommandGroup.ENVIRONMENT, EnvironmentKey.LOGIN)
FRAME_MAGIC = 0xAB
FRAME_HEADER_SIZE = 10
MULTIPART_HEADER_SIZE = 8
APP_MAX_DATA_LENGTH = 1024
DEVICE_MAX_DATA_LENGTH = 4096
# The physical payload includes the eight-byte multipart header.  Keep the
# historical name because callers imported it before multipart support existed.
MAX_PAYLOAD_LENGTH = DEVICE_MAX_DATA_LENGTH + MULTIPART_HEADER_SIZE
DEFAULT_MAX_REASSEMBLED_PAYLOAD_LENGTH = 16 * 1024 * 1024
DEFAULT_MAX_MULTIPART_FRAGMENTS = 4096
DEFAULT_MAX_PENDING_MESSAGES = 128
_EPOCH_2000_UNIX_SECONDS = 946_684_800
_INT32_MIN = -(1 << 31)
_INT32_MAX = (1 << 31) - 1
_INT64_MAX = (1 << 63) - 1
_AUTH_RESULTS = frozenset((*range(8), 127))


class WatchAppFrameFlag(IntFlag):
    """Defined flag bits in byte three of the ten-byte envelope."""

    MULTIPART = 1 << 5
    RETRANSMISSION = 1 << 6
    # v0.1.2 calls bit 7 reserved.  Current 6202 firmware names it data_type,
    # so the codec preserves and exposes it without relying on its semantics.
    DATA_TYPE = 1 << 7


MULTIPART_FLAG = int(WatchAppFrameFlag.MULTIPART)
RETRANSMISSION_FLAG = int(WatchAppFrameFlag.RETRANSMISSION)
DATA_TYPE_FLAG = int(WatchAppFrameFlag.DATA_TYPE)


_MessageT = TypeVar("_MessageT", bound=Message)


_DEFAULT_STRING_BYTE_LIMITS = {
    "agent_loop_system/protocol/pb_config.proto": 16,
    "agent_loop_system/protocol/pb_setting.proto": 64,
    "agent_loop_system/protocol/pb_env.proto": 16,
    "agent_loop_system/protocol/pb_stream.proto": 127,
}
_DEFAULT_BYTES_LIMITS = {
    "agent_loop_system/protocol/pb_config.proto": 4,
}
_FIELD_BYTE_LIMITS = {
    "_NotificationConfig.flags": 8,
    "_DeviceInfo.ability": 16,
    "_DeviceInfo.activity": 3,
    "_DeviceInfo.notifications": 8,
    "_AlarmItem.label": 63,
    "_ContactItem.name": 31,
    "_ContactItem.phone": 31,
    "_RemindItem.name": 32,
    "_Weather.city": 31,
    "_WorldClockItem.city": 31,
    "_WorldClockItem.zoneId": 31,
    "_LockObj.password": 6,
    "_OfflineMapAuthInfo.key": 127,
    "_OfflineMapAuthInfo.license": 255,
    "_OfflineMapAuthInfo.device": 127,
    "_OfflineMapAuthInfo.zoom": 31,
    "_QrCodeSet.content": 511,
    "_AiResult.text": 799,
    "_TestDeviceInfo.deviceName": 15,
    "_TestDeviceInfo.bluetoothName": 15,
    "_TestDeviceInfo.model": 15,
    "_TestDeviceInfo.version": 15,
    "_AuthRequest.userId": 32,
    "_LanguageList.items": 64,
    "_BluetoothInfo.ble_mac": 32,
    "_BluetoothInfo.ble_name": 32,
    "_BluetoothInfo.bt_mac": 32,
    "_BluetoothInfo.bt_name": 32,
    "_AppNotice.title": 100,
    "_AppNotice.content": 800,
    "_TelephonyNotice.phone": 31,
    "_TelephonyNotice.name": 31,
    "_HangupRequest.phone": 31,
    "_HangupRequest.sendSms": 400,
    "_SOSRequest.phone": 31,
    "_QuickCmd.cmd": 128,
    "_HeartRateDay.auto_data": 288,
    "_PressureDay.auto_data": 288,
    "_BloodOxygenDay.auto_data": 288,
    "_BloodPressureDay.auto_data": 576,
    "_SportRecord.display_configs": 32,
    "_SportRecord.file_detail": 31,
    "_FileData.data": 960,
    "_BytesData.data": 960,
    "_HsdIce.labels": 63,
    "_HsdTask.label": 32,
    "_HsdTask.description": 100,
    "_HsdHabit.label": 32,
}
_FIELD_COUNT_LIMITS = {
    "_DeviceInfo.Limits.remind": 8,
    "_AlarmList.items": 10,
    "_ContactCommon.items": 14,
    "_ContactEmergency.items": 14,
    "_RemindItem.times": 10,
    "_RemindList.items": 10,
    "_WeatherDayList.items": 7,
    "_WeatherHourList.items": 12,
    "_DialList.items": 100,
    "_WorldClockList.items": 10,
    "_PrayerConfig.items": 7,
    "_PrayerDay.items": 7,
    "_PrayerDayList.days": 7,
    "_ActivityDay.steps": 48,
    "_ActivityDay.calories": 48,
    "_ActivityDay.distance": 48,
    "_ActivityDay.duration": 48,
    "_ActivityDay.sport_duration": 48,
    "_ActivityDay.number": 48,
    "_HeartRateDay.manual_data": 200,
    "_HeartRateDay.resting_data": 10,
    "_HeartRateDay.max_min_data": 2,
    "_PressureDay.manual_data": 200,
    "_PressureDay.max_min_data": 2,
    "_BloodOxygenDay.manual_data": 200,
    "_BloodOxygenDay.max_min_data": 2,
    "_BloodPressureDay.manual_data": 200,
    "_BloodPressureDay.max_min_data": 2,
    "_SleepDay.items": 100,
    "_FileList.files": 7,
    "_HsdIce.labels": 3,
    "_HsdTaskInfo.items": 5,
    "_HsdHabitList.items": 10,
    "_HsdUsageInfoList.items": 100,
    "_HsdGameRecordList.items": 10,
    "_HsdGameRankingTrendList.items": 30,
}


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


def _local_field_name(field_descriptor: FieldDescriptor) -> str:
    message_name = field_descriptor.containing_type.full_name
    prefix = "agent_loop_system.protocol."
    if message_name.startswith(prefix):
        message_name = message_name[len(prefix) :]
    return f"{message_name}.{field_descriptor.name}"


def _field_byte_limit(field_descriptor: FieldDescriptor) -> int | None:
    local_name = _local_field_name(field_descriptor)
    if local_name in _FIELD_BYTE_LIMITS:
        return _FIELD_BYTE_LIMITS[local_name]
    if field_descriptor.type == FieldDescriptor.TYPE_STRING:
        return _DEFAULT_STRING_BYTE_LIMITS.get(field_descriptor.file.name)
    if field_descriptor.type == FieldDescriptor.TYPE_BYTES:
        return _DEFAULT_BYTES_LIMITS.get(field_descriptor.file.name)
    return None


def validate_protobuf(message: Message) -> None:
    """Validate the document's string, bytes, and repeated-field limits."""

    if not isinstance(message, Message):
        raise WatchAppProtocolError("message must be a protobuf Message")
    populated = {
        descriptor.number for descriptor, _ in message.ListFields()
    }
    for descriptor in message.DESCRIPTOR.fields:
        local_name = _local_field_name(descriptor)
        value = getattr(message, descriptor.name)
        count_limit = _FIELD_COUNT_LIMITS.get(local_name)
        if descriptor.is_repeated:
            if count_limit is not None and len(value) > count_limit:
                raise WatchAppProtocolError(
                    f"{local_name} has {len(value)} items; firmware limit is "
                    f"{count_limit}"
                )
            values = value
        else:
            values = (value,)

        byte_limit = _field_byte_limit(descriptor)
        if byte_limit is not None:
            for index, item in enumerate(values):
                if descriptor.type == FieldDescriptor.TYPE_STRING:
                    try:
                        item_length = len(item.encode("utf-8"))
                    except UnicodeEncodeError as exc:
                        raise WatchAppProtocolError(
                            f"{local_name} must be valid UTF-8 text"
                        ) from exc
                else:
                    item_length = len(item)
                if item_length > byte_limit:
                    suffix = f"[{index}]" if descriptor.is_repeated else ""
                    raise WatchAppProtocolError(
                        f"{local_name}{suffix} is {item_length} bytes; "
                        f"firmware limit is {byte_limit}"
                    )

        if descriptor.type != FieldDescriptor.TYPE_MESSAGE:
            continue
        if descriptor.is_repeated:
            for item in value:
                validate_protobuf(item)
        elif descriptor.number in populated:
            validate_protobuf(value)


def make_command(command_group: int, key: int) -> int:
    """Combine the one-byte ``cmd`` and ``key`` fields into one command id."""

    _validate_int("command_group", command_group, 0, 0xFF)
    _validate_int("key", key, 0, 0xFF)
    return command_id(command_group, key)


def split_command(command: int) -> tuple[int, int]:
    """Return the one-byte ``(cmd, key)`` pair for a combined command id."""

    _validate_int("command", command, 0, 0xFFFF)
    return command >> 8, command & 0xFF


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
        payload_limit = DEVICE_MAX_DATA_LENGTH + (
            MULTIPART_HEADER_SIZE if self.is_multipart else 0
        )
        if len(self.payload) > payload_limit:
            raise WatchAppProtocolError(
                f"payload exceeds firmware limit of {payload_limit} bytes"
            )

    @property
    def command_group(self) -> int:
        return self.command >> 8

    @property
    def key(self) -> int:
        return self.command & 0xFF

    @property
    def is_multipart(self) -> bool:
        return bool(self.flags & MULTIPART_FLAG)

    @property
    def is_retransmission(self) -> bool:
        return bool(self.flags & RETRANSMISSION_FLAG)


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

    flags = raw[3]
    payload_length = int.from_bytes(raw[4:6], "big")
    payload_limit = DEVICE_MAX_DATA_LENGTH + (
        MULTIPART_HEADER_SIZE if flags & MULTIPART_FLAG else 0
    )
    if payload_length > payload_limit:
        raise WatchAppProtocolError(
            f"payload length {payload_length} exceeds firmware limit "
            f"of {payload_limit}"
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
        flags=flags,
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

            flags = self._buffer[3]
            payload_length = int.from_bytes(self._buffer[4:6], "big")
            payload_limit = DEVICE_MAX_DATA_LENGTH + (
                MULTIPART_HEADER_SIZE if flags & MULTIPART_FLAG else 0
            )
            if payload_length > payload_limit:
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
class WatchAppFragment:
    """The eight-byte multipart header and its protobuf data chunk."""

    total_packets: int
    packet_index: int
    data: bytes

    def __post_init__(self) -> None:
        _validate_int("total_packets", self.total_packets, 1, 0xFFFFFFFF)
        _validate_int("packet_index", self.packet_index, 0, 0xFFFFFFFF)
        if self.packet_index >= self.total_packets:
            raise WatchAppProtocolError(
                "packet_index must be smaller than total_packets"
            )
        if not isinstance(self.data, bytes):
            raise WatchAppProtocolError("fragment data must be bytes")
        if len(self.data) > DEVICE_MAX_DATA_LENGTH:
            raise WatchAppProtocolError(
                f"fragment data exceeds protocol limit of "
                f"{DEVICE_MAX_DATA_LENGTH} bytes"
            )

    def encode(self) -> bytes:
        return struct.pack(">II", self.total_packets, self.packet_index) + self.data


def decode_multipart_fragment(payload: bytes) -> WatchAppFragment:
    """Decode one multipart frame payload, including its eight-byte header."""

    try:
        raw = memoryview(payload).cast("B").tobytes()
    except (TypeError, ValueError) as exc:
        raise WatchAppProtocolError("multipart payload must be bytes-like") from exc
    if len(raw) < MULTIPART_HEADER_SIZE:
        raise WatchAppProtocolError(
            "multipart payload is shorter than the 8-byte multipart header"
        )
    total_packets, packet_index = struct.unpack_from(">II", raw)
    return WatchAppFragment(
        total_packets=total_packets,
        packet_index=packet_index,
        data=raw[MULTIPART_HEADER_SIZE:],
    )


def encode_multipart_chunks(
    command: int,
    sequence: int,
    chunks: list[bytes] | tuple[bytes, ...],
    *,
    flags: int = 0,
    max_data_length: int = APP_MAX_DATA_LENGTH,
) -> tuple[WatchAppFrame, ...]:
    """Wrap already encoded object/list chunks in numbered multipart frames.

    Each chunk is a complete protobuf wire fragment.  Parsing their ordered
    concatenation uses protobuf's normal merge semantics, which covers both
    object-field splitting and repeated-list splitting from the specification.
    """

    _validate_int("command", command, 0, 0xFFFF)
    _validate_int("sequence", sequence, 0, 0xFFFF)
    _validate_int("flags", flags, 0, 0xFF)
    _validate_int(
        "max_data_length", max_data_length, 1, DEVICE_MAX_DATA_LENGTH
    )
    if not isinstance(chunks, (list, tuple)):
        raise WatchAppProtocolError("chunks must be a list or tuple of bytes")
    if not chunks:
        raise WatchAppProtocolError("multipart chunks must not be empty")
    if len(chunks) > 0xFFFFFFFF:
        raise WatchAppProtocolError("multipart chunk count exceeds uint32")

    total_packets = len(chunks)
    frames: list[WatchAppFrame] = []
    multipart_flags = flags | MULTIPART_FLAG
    for packet_index, chunk in enumerate(chunks):
        if not isinstance(chunk, bytes):
            raise WatchAppProtocolError(
                f"chunks[{packet_index}] must be bytes"
            )
        if len(chunk) > max_data_length:
            raise WatchAppProtocolError(
                f"chunks[{packet_index}] exceeds data limit of "
                f"{max_data_length} bytes"
            )
        fragment = WatchAppFragment(total_packets, packet_index, chunk)
        frames.append(
            WatchAppFrame(
                command=command,
                sequence=sequence,
                payload=fragment.encode(),
                flags=multipart_flags,
            )
        )
    return tuple(frames)


def fragment_payload(
    command: int,
    sequence: int,
    payload: bytes,
    *,
    max_data_length: int = APP_MAX_DATA_LENGTH,
    flags: int = 0,
    force_multipart: bool = False,
) -> tuple[WatchAppFrame, ...]:
    """Split opaque bytes into one or more protocol frames.

    The default 1024-byte data limit is the App-to-device limit in v0.1.2.
    ``max_data_length`` may be raised to 4096 when modelling device output.
    Do not use byte slicing for protobuf business objects: the firmware decodes
    each fragment independently. Use ``encode_protobuf_chunks`` with complete
    partial object/list messages instead.
    """

    try:
        raw = memoryview(payload).cast("B").tobytes()
    except (TypeError, ValueError) as exc:
        raise WatchAppProtocolError("payload must be bytes-like") from exc
    _validate_int(
        "max_data_length", max_data_length, 1, DEVICE_MAX_DATA_LENGTH
    )
    _validate_int("flags", flags, 0, 0xFF)
    if len(raw) <= max_data_length and not force_multipart:
        return (
            WatchAppFrame(
                command=command,
                sequence=sequence,
                payload=raw,
                flags=flags & ~MULTIPART_FLAG,
            ),
        )

    chunks = tuple(
        raw[offset : offset + max_data_length]
        for offset in range(0, len(raw), max_data_length)
    )
    if not chunks:
        chunks = (b"",)
    return encode_multipart_chunks(
        command,
        sequence,
        chunks,
        flags=flags,
        max_data_length=max_data_length,
    )


@dataclass(frozen=True, slots=True)
class WatchAppMessage:
    """One complete PB command after any protocol-level reassembly."""

    command: int
    sequence: int
    payload: bytes
    flags: int = 0
    fragment_count: int = 1

    def __post_init__(self) -> None:
        _validate_int("command", self.command, 0, 0xFFFF)
        _validate_int("sequence", self.sequence, 0, 0xFFFF)
        _validate_int("flags", self.flags, 0, 0xFF)
        _validate_int("fragment_count", self.fragment_count, 1, 0xFFFFFFFF)
        if not isinstance(self.payload, bytes):
            raise WatchAppProtocolError("payload must be bytes")

    @property
    def command_group(self) -> int:
        return self.command >> 8

    @property
    def key(self) -> int:
        return self.command & 0xFF

    @property
    def is_multipart(self) -> bool:
        return bool(self.flags & MULTIPART_FLAG)

    @property
    def is_retransmission(self) -> bool:
        return bool(self.flags & RETRANSMISSION_FLAG)


@dataclass(slots=True)
class _MultipartState:
    total_packets: int
    base_flags: int
    chunks: dict[int, bytes] = field(default_factory=dict)
    byte_count: int = 0
    saw_retransmission: bool = False


class WatchAppMessageAssembler:
    """Reassemble interleaved PB fragments by channel, command, and sequence."""

    __slots__ = (
        "_max_fragments",
        "_max_payload_length",
        "_max_pending",
        "_pending",
    )

    def __init__(
        self,
        *,
        max_fragments: int = DEFAULT_MAX_MULTIPART_FRAGMENTS,
        max_payload_length: int = DEFAULT_MAX_REASSEMBLED_PAYLOAD_LENGTH,
        max_pending_messages: int = DEFAULT_MAX_PENDING_MESSAGES,
    ) -> None:
        _validate_int("max_fragments", max_fragments, 1, 0xFFFFFFFF)
        _validate_int("max_payload_length", max_payload_length, 1, 0x7FFFFFFF)
        _validate_int("max_pending_messages", max_pending_messages, 1, 0x7FFFFFFF)
        self._max_fragments = max_fragments
        self._max_payload_length = max_payload_length
        self._max_pending = max_pending_messages
        self._pending: dict[
            tuple[Hashable | None, int, int, int], _MultipartState
        ] = {}

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    def reset(self) -> None:
        self._pending.clear()

    @staticmethod
    def _state_key(
        channel: Hashable | None, frame: WatchAppFrame
    ) -> tuple[Hashable | None, int, int, int]:
        try:
            hash(channel)
        except TypeError as exc:
            raise WatchAppProtocolError("channel must be hashable") from exc
        base_flags = frame.flags & ~(MULTIPART_FLAG | RETRANSMISSION_FLAG)
        return channel, frame.command, frame.sequence, base_flags

    def feed(
        self,
        frame: WatchAppFrame,
        *,
        channel: Hashable | None = None,
    ) -> WatchAppMessage | None:
        """Consume a physical frame and return a complete logical message."""

        if not isinstance(frame, WatchAppFrame):
            raise WatchAppProtocolError("frame must be a WatchAppFrame")
        state_key = self._state_key(channel, frame)

        if not frame.is_multipart:
            if state_key in self._pending:
                del self._pending[state_key]
                raise WatchAppProtocolError(
                    "single frame conflicts with an incomplete multipart message"
                )
            return WatchAppMessage(
                command=frame.command,
                sequence=frame.sequence,
                payload=frame.payload,
                flags=frame.flags,
            )

        fragment = decode_multipart_fragment(frame.payload)
        if fragment.total_packets > self._max_fragments:
            self._pending.pop(state_key, None)
            raise WatchAppProtocolError(
                f"multipart total {fragment.total_packets} exceeds configured "
                f"limit of {self._max_fragments}"
            )

        state = self._pending.get(state_key)
        if state is None:
            if len(self._pending) >= self._max_pending:
                raise WatchAppProtocolError(
                    "too many incomplete multipart messages"
                )
            state = _MultipartState(
                total_packets=fragment.total_packets,
                base_flags=state_key[3],
            )
            self._pending[state_key] = state
        elif state.total_packets != fragment.total_packets:
            del self._pending[state_key]
            raise WatchAppProtocolError(
                "multipart total_packets changed before completion"
            )

        previous = state.chunks.get(fragment.packet_index)
        if previous is not None:
            if previous != fragment.data:
                del self._pending[state_key]
                raise WatchAppProtocolError(
                    "multipart retransmission changed an existing chunk"
                )
            state.saw_retransmission = (
                state.saw_retransmission or frame.is_retransmission
            )
            return None

        next_size = state.byte_count + len(fragment.data)
        if next_size > self._max_payload_length:
            del self._pending[state_key]
            raise WatchAppProtocolError(
                f"reassembled payload exceeds configured limit of "
                f"{self._max_payload_length} bytes"
            )
        state.chunks[fragment.packet_index] = fragment.data
        state.byte_count = next_size
        state.saw_retransmission = (
            state.saw_retransmission or frame.is_retransmission
        )

        if len(state.chunks) != state.total_packets:
            return None
        try:
            payload = b"".join(
                state.chunks[index] for index in range(state.total_packets)
            )
        except KeyError as exc:
            del self._pending[state_key]
            raise WatchAppProtocolError(
                "multipart message completed with a missing packet index"
            ) from exc
        del self._pending[state_key]
        flags = state.base_flags | MULTIPART_FLAG
        if state.saw_retransmission:
            flags |= RETRANSMISSION_FLAG
        return WatchAppMessage(
            command=frame.command,
            sequence=frame.sequence,
            payload=payload,
            flags=flags,
            fragment_count=state.total_packets,
        )


class WatchAppMessageDecoder:
    """Decode byte streams and reassemble messages on independent channels."""

    __slots__ = ("_assembler", "_frame_decoders")

    def __init__(self, **assembler_options: int) -> None:
        self._assembler = WatchAppMessageAssembler(**assembler_options)
        self._frame_decoders: dict[
            Hashable | None, WatchAppFrameDecoder
        ] = {}

    @property
    def pending_count(self) -> int:
        return self._assembler.pending_count

    def reset(self) -> None:
        self._frame_decoders.clear()
        self._assembler.reset()

    def feed(
        self,
        data: bytes,
        *,
        channel: Hashable | None = None,
    ) -> list[WatchAppMessage]:
        try:
            hash(channel)
        except TypeError as exc:
            raise WatchAppProtocolError("channel must be hashable") from exc
        decoder = self._frame_decoders.get(channel)
        if decoder is None:
            decoder = WatchAppFrameDecoder()
            self._frame_decoders[channel] = decoder
        messages: list[WatchAppMessage] = []
        for frame in decoder.feed(data):
            message = self._assembler.feed(frame, channel=channel)
            if message is not None:
                messages.append(message)
        return messages


def encode_protobuf(message: Message) -> bytes:
    """Serialize any generated PB message deterministically."""

    if not isinstance(message, Message):
        raise WatchAppProtocolError("message must be a protobuf Message")
    try:
        return message.SerializeToString(deterministic=True)
    except (EncodeError, TypeError, ValueError) as exc:
        raise WatchAppProtocolError(f"cannot encode protobuf message: {exc}") from exc


def decode_protobuf(data: bytes, message_type: type[_MessageT]) -> _MessageT:
    """Parse bytes into a requested generated PB message class."""

    try:
        raw = memoryview(data).cast("B").tobytes()
    except (TypeError, ValueError) as exc:
        raise WatchAppProtocolError("protobuf data must be bytes-like") from exc
    try:
        message = message_type()
    except (TypeError, ValueError) as exc:
        raise WatchAppProtocolError(
            "message_type must be a protobuf message class"
        ) from exc
    if not isinstance(message, Message):
        raise WatchAppProtocolError(
            "message_type must be a protobuf message class"
        )
    try:
        message.ParseFromString(raw)
    except DecodeError as exc:
        raise WatchAppProtocolError(f"invalid protobuf payload: {exc}") from exc
    return message


def _copy_protobuf_field(
    source: Message,
    target: Message,
    field_descriptor: FieldDescriptor,
) -> None:
    name = field_descriptor.name
    value = getattr(source, name)
    if field_descriptor.is_repeated:
        target_value = getattr(target, name)
        if (
            field_descriptor.message_type is not None
            and field_descriptor.message_type.GetOptions().map_entry
        ):
            target_value.update(value)
        elif field_descriptor.message_type is not None:
            for item in value:
                target_value.add().CopyFrom(item)
        else:
            target_value.extend(value)
    elif field_descriptor.message_type is not None:
        getattr(target, name).CopyFrom(value)
    else:
        setattr(target, name, value)


def split_protobuf_object(
    message: _MessageT,
    *,
    max_data_length: int = APP_MAX_DATA_LENGTH,
) -> tuple[_MessageT, ...]:
    """Greedily split populated top-level fields into valid PB objects."""

    if not isinstance(message, Message):
        raise WatchAppProtocolError("message must be a protobuf Message")
    validate_protobuf(message)
    _validate_int(
        "max_data_length", max_data_length, 1, DEVICE_MAX_DATA_LENGTH
    )
    message_type = type(message)
    populated_fields = tuple(descriptor for descriptor, _ in message.ListFields())
    if not populated_fields:
        return (message_type(),)

    chunks: list[_MessageT] = []
    current = message_type()
    for descriptor in populated_fields:
        candidate = message_type()
        candidate.CopyFrom(current)
        _copy_protobuf_field(message, candidate, descriptor)
        if len(encode_protobuf(candidate)) <= max_data_length:
            current = candidate
            continue
        if current.ListFields():
            chunks.append(current)
            current = message_type()
            _copy_protobuf_field(message, current, descriptor)
        else:
            current = candidate
        encoded_length = len(encode_protobuf(current))
        if encoded_length > max_data_length:
            raise WatchAppProtocolError(
                f"field {descriptor.name!r} encodes to {encoded_length} bytes; "
                f"object chunk limit is {max_data_length}. Split that field "
                "with a structure-specific strategy"
            )
    if current.ListFields():
        chunks.append(current)
    return tuple(chunks)


def split_protobuf_list(
    message: _MessageT,
    field_name: str,
    *,
    max_data_length: int = APP_MAX_DATA_LENGTH,
) -> tuple[_MessageT, ...]:
    """Split one repeated field into valid, size-bounded PB list messages."""

    if not isinstance(message, Message):
        raise WatchAppProtocolError("message must be a protobuf Message")
    if not isinstance(field_name, str) or not field_name:
        raise WatchAppProtocolError("field_name must be a non-empty string")
    _validate_int(
        "max_data_length", max_data_length, 1, DEVICE_MAX_DATA_LENGTH
    )
    descriptor = message.DESCRIPTOR.fields_by_name.get(field_name)
    if descriptor is None:
        raise WatchAppProtocolError(
            f"protobuf message has no field {field_name!r}"
        )
    if not descriptor.is_repeated:
        raise WatchAppProtocolError(f"field {field_name!r} is not repeated")
    if (
        descriptor.message_type is not None
        and descriptor.message_type.GetOptions().map_entry
    ):
        raise WatchAppProtocolError(
            f"field {field_name!r} is a map; use object chunks explicitly"
        )

    message_type = type(message)
    base = message_type()
    base.CopyFrom(message)
    base.ClearField(field_name)
    validate_protobuf(base)
    base_length = len(encode_protobuf(base))
    if base_length > max_data_length:
        raise WatchAppProtocolError(
            f"non-list fields encode to {base_length} bytes; chunk limit is "
            f"{max_data_length}"
        )
    source_items = getattr(message, field_name)
    if not source_items:
        return (base,)

    def append_item(target: Message, item: object) -> None:
        target_items = getattr(target, field_name)
        if descriptor.message_type is not None:
            target_items.add().CopyFrom(item)
        else:
            target_items.append(item)

    chunks: list[_MessageT] = []
    current = message_type()
    current.CopyFrom(base)
    for item in source_items:
        candidate = message_type()
        candidate.CopyFrom(current)
        append_item(candidate, item)
        candidate_is_valid = True
        try:
            validate_protobuf(candidate)
        except WatchAppProtocolError:
            candidate_is_valid = False
        if (
            candidate_is_valid
            and len(encode_protobuf(candidate)) <= max_data_length
        ):
            current = candidate
            continue
        if len(getattr(current, field_name)):
            chunks.append(current)
            current = message_type()
            current.CopyFrom(base)
            append_item(current, item)
        else:
            current = candidate
        validate_protobuf(current)
        encoded_length = len(encode_protobuf(current))
        if encoded_length > max_data_length:
            raise WatchAppProtocolError(
                f"one {field_name!r} item encodes to {encoded_length} bytes; "
                f"list chunk limit is {max_data_length}"
            )
    chunks.append(current)
    return tuple(chunks)


def encode_protobuf_frames(
    command: int,
    sequence: int,
    message: Message,
    *,
    max_data_length: int = APP_MAX_DATA_LENGTH,
    flags: int = 0,
    force_multipart: bool = False,
) -> tuple[WatchAppFrame, ...]:
    """Serialize one complete PB message into a compliant outbound frame.

    Protobuf wire bytes cannot be split at arbitrary byte offsets because the
    device decodes each protocol fragment independently.  For a message that
    exceeds the per-frame limit, build semantically complete partial messages
    and pass them to :func:`encode_protobuf_chunks`.
    """

    validate_protobuf(message)
    _validate_int(
        "max_data_length", max_data_length, 1, DEVICE_MAX_DATA_LENGTH
    )
    payload = encode_protobuf(message)
    if len(payload) > max_data_length:
        raise WatchAppProtocolError(
            f"encoded protobuf is {len(payload)} bytes; limit is "
            f"{max_data_length}. Split it into complete object/list chunks"
        )
    if force_multipart:
        return encode_multipart_chunks(
            command,
            sequence,
            (payload,),
            flags=flags,
            max_data_length=max_data_length,
        )
    return (
        WatchAppFrame(
            command=command,
            sequence=sequence,
            payload=payload,
            flags=flags & ~MULTIPART_FLAG,
        ),
    )


def encode_protobuf_chunks(
    command: int,
    sequence: int,
    messages: list[Message] | tuple[Message, ...],
    *,
    max_data_length: int = APP_MAX_DATA_LENGTH,
    flags: int = 0,
) -> tuple[WatchAppFrame, ...]:
    """Encode semantic object/list fragments as independently valid PB chunks."""

    if not isinstance(messages, (list, tuple)):
        raise WatchAppProtocolError(
            "messages must be a list or tuple of protobuf messages"
        )
    if not messages:
        raise WatchAppProtocolError("protobuf message chunks must not be empty")
    for message in messages:
        validate_protobuf(message)
    return encode_multipart_chunks(
        command,
        sequence,
        tuple(encode_protobuf(message) for message in messages),
        flags=flags,
        max_data_length=max_data_length,
    )


@dataclass(frozen=True, slots=True)
class WatchUserProfile:
    gender: int = 1
    age: int = 30
    height_cm: int = 170
    weight_kg: float = 65.0

    def __post_init__(self) -> None:
        # v0.1.2 adds value 2 for an unknown/unspecified gender.
        _validate_int("gender", self.gender, 0, 2)
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
    validate_protobuf(message)
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
