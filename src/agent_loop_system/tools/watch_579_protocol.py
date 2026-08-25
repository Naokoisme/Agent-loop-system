"""579 legacy L1/L2 BLE protocol codec.

This module is intentionally independent from the 6202 protobuf protocol.  It
implements the byte layout used by the current 579 firmware and contains no
BLE or frontend code, so packet generation and notification reassembly can be
verified deterministically.
"""

from __future__ import annotations

from dataclasses import dataclass
import re


WATCH_579_SERVICE_UUID = "000001ff-3c17-d293-8e48-14fe2e4da212"
WATCH_579_WRITE_UUID = "0000ff02-0000-1000-8000-00805f9b34fb"
WATCH_579_NOTIFY_UUID = "0000ff03-0000-1000-8000-00805f9b34fb"

L1_MAGIC = 0xAB
L1_HEADER_SIZE = 8
L1_ACK_FLAG = 0x10
L2_HEADER_SIZE = 5
DEFAULT_SEQUENCE = 0x0002
MAX_DATA_LENGTH = 499

_HEX_TOKEN = re.compile(r"^[0-9A-Fa-f]+$")
_DATA_SEPARATORS = re.compile(r"[\s,:;\-_]+")


class Watch579ProtocolError(ValueError):
    """A malformed 579 packet or raw command value."""


@dataclass(frozen=True, slots=True)
class Watch579RawCommand:
    cmd: int
    key: int
    data: bytes = b""

    @property
    def cmd_hex(self) -> str:
        return f"{self.cmd:02X}"

    @property
    def key_hex(self) -> str:
        return f"{self.key:02X}"

    @property
    def data_hex(self) -> str:
        return format_hex(self.data)


@dataclass(frozen=True, slots=True)
class Watch579TransportAck:
    sequence: int
    packet: bytes


@dataclass(frozen=True, slots=True)
class Watch579Message:
    sequence: int
    flags: int
    crc: int
    command: Watch579RawCommand
    packet: bytes


Watch579DecodedFrame = Watch579TransportAck | Watch579Message


def format_hex(value: bytes | bytearray | memoryview) -> str:
    return " ".join(f"{byte:02X}" for byte in bytes(value))


def parse_byte(value: object, *, field: str) -> int:
    """Parse Cmd/Key input as one hexadecimal byte.

    Strings accept both ``02`` and ``0x02`` in either case.  Integer callers
    pass the numeric byte directly.
    """

    if isinstance(value, bool):
        raise Watch579ProtocolError(f"{field} must be one hexadecimal byte")
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, str):
        token = value.strip()
        if token.lower().startswith("0x"):
            token = token[2:]
        if not 1 <= len(token) <= 2 or not _HEX_TOKEN.fullmatch(token):
            raise Watch579ProtocolError(
                f"{field} must be one hexadecimal byte, for example 02 or 0x02"
            )
        parsed = int(token, 16)
    else:
        raise Watch579ProtocolError(f"{field} must be one hexadecimal byte")
    if not 0 <= parsed <= 0xFF:
        raise Watch579ProtocolError(f"{field} must be in 00..FF")
    return parsed


def parse_data_hex(value: object, *, max_length: int = MAX_DATA_LENGTH) -> bytes:
    """Parse empty, continuous, or separator-delimited hexadecimal data."""

    if value is None:
        return b""
    if isinstance(value, (bytes, bytearray, memoryview)):
        data = bytes(value)
    elif isinstance(value, str):
        raw = value.strip()
        if not raw:
            return b""
        parts = [part for part in _DATA_SEPARATORS.split(raw) if part]
        normalized: list[str] = []
        for part in parts:
            token = part[2:] if part.lower().startswith("0x") else part
            if not token or not _HEX_TOKEN.fullmatch(token):
                raise Watch579ProtocolError(
                    "Data must contain hexadecimal bytes only"
                )
            normalized.append(token)
        packed = "".join(normalized)
        if len(packed) % 2:
            raise Watch579ProtocolError("Data must contain an even number of hex digits")
        data = bytes.fromhex(packed)
    else:
        raise Watch579ProtocolError("Data must be hexadecimal text or bytes")
    if len(data) > max_length:
        raise Watch579ProtocolError(
            f"Data is {len(data)} bytes; maximum is {max_length} bytes"
        )
    return data


def normalize_raw_command(cmd: object, key: object, data: object = "") -> Watch579RawCommand:
    return Watch579RawCommand(
        cmd=parse_byte(cmd, field="Cmd"),
        key=parse_byte(key, field="Key"),
        data=parse_data_hex(data),
    )


def crc16_arc(data: bytes | bytearray | memoryview) -> int:
    """CRC-16/ARC (poly 0xA001, init 0x0000)."""

    crc = 0
    for byte in bytes(data):
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc & 0xFFFF


def encode_raw_command(
    command: Watch579RawCommand,
    *,
    sequence: int = DEFAULT_SEQUENCE,
) -> bytes:
    if not 0 <= sequence <= 0xFFFF:
        raise Watch579ProtocolError("sequence must be in 0000..FFFF")
    if len(command.data) > MAX_DATA_LENGTH:
        raise Watch579ProtocolError(
            f"Data is {len(command.data)} bytes; maximum is {MAX_DATA_LENGTH} bytes"
        )
    body = bytes(
        (
            command.cmd,
            0,
            command.key,
            (len(command.data) >> 8) & 0xFF,
            len(command.data) & 0xFF,
        )
    ) + command.data
    checksum = crc16_arc(body)
    packet = bytes(
        (
            L1_MAGIC,
            0,
            (len(body) >> 8) & 0xFF,
            len(body) & 0xFF,
            (checksum >> 8) & 0xFF,
            checksum & 0xFF,
            (sequence >> 8) & 0xFF,
            sequence & 0xFF,
        )
    ) + body
    # PacketBuilder::buildSimpleCmd writes sizeof(pkg), retaining the one-byte
    # key_value placeholder even though the declared L2 data length is zero.
    if not command.data:
        packet += b"\x00"
    return packet


def build_raw_packet(
    cmd: object,
    key: object,
    data: object = "",
    *,
    sequence: int = DEFAULT_SEQUENCE,
) -> bytes:
    return encode_raw_command(
        normalize_raw_command(cmd, key, data),
        sequence=sequence,
    )


def encode_transport_ack(sequence: int) -> bytes:
    if not 0 <= sequence <= 0xFFFF:
        raise Watch579ProtocolError("sequence must be in 0000..FFFF")
    return bytes(
        (
            L1_MAGIC,
            L1_ACK_FLAG,
            0,
            0,
            0,
            0,
            (sequence >> 8) & 0xFF,
            sequence & 0xFF,
        )
    )


def _decode_message(header: bytes, body: bytes) -> Watch579Message:
    declared_crc = int.from_bytes(header[4:6], "big")
    actual_crc = crc16_arc(body)
    if actual_crc != declared_crc:
        raise Watch579ProtocolError(
            f"L2 CRC mismatch: declared={declared_crc:04X}, actual={actual_crc:04X}"
        )
    if len(body) < L2_HEADER_SIZE:
        raise Watch579ProtocolError("L2 payload is shorter than its 5-byte header")
    data_length = int.from_bytes(body[3:5], "big")
    if data_length > MAX_DATA_LENGTH:
        raise Watch579ProtocolError(
            f"L2 data length {data_length} exceeds {MAX_DATA_LENGTH}"
        )
    if len(body) != L2_HEADER_SIZE + data_length:
        raise Watch579ProtocolError(
            "L2 data length does not match the L1 payload length"
        )
    if body[1] != 0:
        raise Watch579ProtocolError(f"unsupported L2 version/reserve byte {body[1]:02X}")
    sequence = int.from_bytes(header[6:8], "big")
    command = Watch579RawCommand(body[0], body[2], body[5:])
    return Watch579Message(
        sequence=sequence,
        flags=header[1],
        crc=declared_crc,
        command=command,
        packet=header + body,
    )


class Watch579FrameDecoder:
    """Incrementally decode split or coalesced L1/L2 notifications."""

    def __init__(self) -> None:
        self._buffer = bytearray()
        self._optional_zero_placeholder = False

    def reset(self) -> None:
        self._buffer.clear()
        self._optional_zero_placeholder = False

    def feed(self, data: bytes | bytearray | memoryview) -> list[Watch579DecodedFrame]:
        if not isinstance(data, (bytes, bytearray, memoryview)):
            raise TypeError("notification data must be bytes-like")
        self._buffer.extend(data)
        frames: list[Watch579DecodedFrame] = []
        try:
            if self._optional_zero_placeholder and self._buffer:
                if self._buffer[0] == 0:
                    del self._buffer[:1]
                self._optional_zero_placeholder = False
            while len(self._buffer) >= L1_HEADER_SIZE:
                if self._buffer[0] != L1_MAGIC:
                    raise Watch579ProtocolError(
                        f"unexpected notification byte {self._buffer[0]:02X}; expected AB"
                    )
                header = bytes(self._buffer[:L1_HEADER_SIZE])
                flags = header[1]
                payload_length = int.from_bytes(header[2:4], "big")
                sequence = int.from_bytes(header[6:8], "big")
                if flags & L1_ACK_FLAG:
                    if payload_length or header[4:6] != b"\x00\x00":
                        raise Watch579ProtocolError("invalid L1 transport ACK header")
                    del self._buffer[:L1_HEADER_SIZE]
                    frames.append(Watch579TransportAck(sequence, header))
                    continue
                if payload_length < L2_HEADER_SIZE:
                    raise Watch579ProtocolError(
                        f"invalid L1 payload length {payload_length}"
                    )
                if payload_length > L2_HEADER_SIZE + MAX_DATA_LENGTH:
                    raise Watch579ProtocolError(
                        f"L1 payload length {payload_length} exceeds protocol maximum"
                    )
                frame_length = L1_HEADER_SIZE + payload_length
                if len(self._buffer) < frame_length:
                    break
                body = bytes(self._buffer[L1_HEADER_SIZE:frame_length])
                del self._buffer[:frame_length]
                message = _decode_message(header, body)
                frames.append(message)
                # Legacy zero-data structures may carry a one-byte key_value
                # placeholder beyond the declared L1 payload length.
                if not message.command.data:
                    if self._buffer[:1] == b"\x00":
                        del self._buffer[:1]
                    elif not self._buffer:
                        self._optional_zero_placeholder = True
        except Exception:
            self.reset()
            raise
        return frames


def decode_packet(packet: bytes | bytearray | memoryview) -> Watch579DecodedFrame:
    decoder = Watch579FrameDecoder()
    frames = decoder.feed(packet)
    if len(frames) != 1 or decoder._buffer:
        raise Watch579ProtocolError("packet does not contain exactly one complete frame")
    return frames[0]


def top5step_data(command: str) -> bytes:
    """Build the 579 notification value for one TOP5STEP command."""

    body = str(command).strip()
    if body.startswith(":"):
        body = body[1:]
    if body.startswith("TOP5STEP:"):
        body = body[len("TOP5STEP:"):]
    body = f"TOP5STEP:{body.rstrip(';')};"
    title = b"TOP5STEP"
    payload = body.encode("utf-8")
    value = bytes((len(title),)) + title + len(payload).to_bytes(2, "big") + payload
    if len(value) > MAX_DATA_LENGTH:
        raise Watch579ProtocolError("TOP5STEP command exceeds the 579 data limit")
    return value


def top5step_command(command: str) -> Watch579RawCommand:
    return Watch579RawCommand(0x04, 0x05, top5step_data(command))


__all__ = [
    "DEFAULT_SEQUENCE",
    "MAX_DATA_LENGTH",
    "WATCH_579_NOTIFY_UUID",
    "WATCH_579_SERVICE_UUID",
    "WATCH_579_WRITE_UUID",
    "Watch579DecodedFrame",
    "Watch579FrameDecoder",
    "Watch579Message",
    "Watch579ProtocolError",
    "Watch579RawCommand",
    "Watch579TransportAck",
    "build_raw_packet",
    "crc16_arc",
    "decode_packet",
    "encode_raw_command",
    "encode_transport_ack",
    "format_hex",
    "normalize_raw_command",
    "parse_byte",
    "parse_data_hex",
    "top5step_command",
    "top5step_data",
]
