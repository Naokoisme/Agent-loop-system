"""Windows BLE GATT client for the watch new-platform PB protocol.

The firmware may still gate authentication on Bluetooth state outside this
GATT transport.  A successful GATT connection alone is therefore not treated
as proof that the complete phone-App Bluetooth environment is available.
"""

from __future__ import annotations

import argparse
import asyncio
import inspect
import json
import secrets
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable, Sequence, TypeVar

from google.protobuf.message import Message

from agent_loop_system.tools.watch_app_protocol import (
    APP_MAX_DATA_LENGTH,
    BIND_COMMAND,
    LOGIN_COMMAND,
    WatchAppFrame,
    WatchAppFrameDecoder,
    WatchAppMessage,
    WatchAppMessageAssembler,
    WatchAppProtocolError,
    WatchAuthRequest,
    WatchAuthResponse,
    decode_auth_response,
    decode_protobuf,
    encode_multipart_chunks,
    encode_auth_request,
    encode_frame,
    encode_protobuf,
    validate_protobuf,
)
from agent_loop_system.tools.watch_ble_screenshot import (
    BLE_SCREENSHOT_COMMAND,
    BLE_SCREENSHOT_COMPLETE_ACK,
    WatchBleScreenshot,
    WatchBleScreenshotAssembler,
    WatchBleScreenshotFeedResult,
    WatchBleScreenshotProtocolError,
    WatchBleScreenshotRemoteError as WatchBleScreenshotProtocolRemoteError,
    encode_screenshot_ack,
    encode_screenshot_request,
    write_verified_screenshot,
)


WATCH_SERVICE_UUID = "000001ff-3c17-d293-8e48-14fe2e4da212"
WATCH_WRITE_UUID = "0000ff02-0000-1000-8000-00805f9b34fb"
WATCH_NOTIFY_UUID = "0000ff03-0000-1000-8000-00805f9b34fb"
DEFAULT_NAME_PREFIX = "oraimo Watch Tank N"
BLOCKED_BY_CLASSIC_BT_GATE = "BLOCKED_BY_CLASSIC_BT_GATE"


_MessageT = TypeVar("_MessageT", bound=Message)


def _encode_pb_payload(message: Message) -> bytes:
    try:
        validate_protobuf(message)
        return encode_protobuf(message)
    except WatchAppProtocolError as exc:
        raise WatchBleProtocolError(str(exc)) from exc


def _decode_pb_payload(data: bytes, message_type: type[_MessageT]) -> _MessageT:
    try:
        return decode_protobuf(data, message_type)
    except WatchAppProtocolError as exc:
        raise WatchBleProtocolError(str(exc)) from exc


class WatchBleError(RuntimeError):
    """Base error for watch discovery, connection, and exchanges."""


class WatchBleDependencyError(WatchBleError):
    """The optional BLE runtime is not installed."""


class WatchBleDiscoveryError(WatchBleError):
    """Discovery failed or no requested watch was found."""


class WatchBleAmbiguousDeviceError(WatchBleDiscoveryError):
    """A selector matched more than one watch."""


class WatchBleConnectionError(WatchBleError):
    """The GATT connection is unavailable or incomplete."""


class WatchBleDisconnectedError(WatchBleConnectionError):
    """The watch disconnected during an operation."""


class WatchBleTimeoutError(WatchBleError, TimeoutError):
    """The watch did not return the expected response in time."""

    def __init__(self, message: str, *, code: str = "RESPONSE_TIMEOUT") -> None:
        super().__init__(message)
        self.code = code


class WatchBlePairingError(WatchBleConnectionError):
    """The host could not complete BLE pairing after a bind request."""


class WatchBlePairingTimeoutError(WatchBleTimeoutError):
    """BLE pairing did not finish within the bind operation timeout."""

    def __init__(self, message: str) -> None:
        super().__init__(message, code="PAIR_TIMEOUT")


class WatchBleClassicGateTimeoutError(WatchBleTimeoutError):
    """No auth reply arrived where a firmware classic-Bluetooth gate may apply."""

    def __init__(self, operation: str) -> None:
        super().__init__(
            f"{operation} received no App-protocol response after the BLE GATT "
            "exchange; the firmware classic-Bluetooth gate may still be closed",
            code=BLOCKED_BY_CLASSIC_BT_GATE,
        )


class WatchBleProtocolError(WatchBleError):
    """The watch returned malformed or unexpected protocol data."""


class WatchBleScreenshotError(WatchBleError):
    """The BLE screenshot could not be captured or persisted."""


class WatchBleScreenshotRemoteError(WatchBleScreenshotError):
    """The watch rejected or failed a BLE screenshot request."""

    code = "SCREENSHOT_REMOTE_ERROR"

    def __init__(self, error: WatchBleScreenshotProtocolRemoteError) -> None:
        self.sequence = error.sequence
        self.remote_code = error.code
        self.reason = error.reason
        super().__init__(str(error))


class WatchBleAuthError(WatchBleError):
    """The App bind or login request returned a failure result."""

    def __init__(self, operation: str, response: WatchAuthResponse) -> None:
        super().__init__(
            f"{operation} failed with result={response.result}"
        )
        self.operation = operation
        self.response = response


@dataclass(frozen=True, slots=True)
class WatchBleDevice:
    """Stable discovery result independent of Bleak backend objects."""

    address: str
    name: str | None
    rssi: int | None = None
    service_uuids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class WatchBleAuthResult:
    """Successful App-protocol authentication result."""

    operation: str
    result: int
    ever_bound: int
    bind_time: int
    status: str
    transport: str = "ble_gatt"
    classic_bluetooth_status: str = "unknown"


@dataclass(frozen=True, slots=True)
class WatchBleScreenshotResult:
    """Metadata for a screenshot transferred and verified on the PC."""

    sequence: int
    path: str
    file_size: int
    crc32: str
    payload_crc32: str
    chunks: int
    chunk_size: int
    width: int
    height: int
    pixel_format: str = "BGR888"
    encoding: str = "bmp"
    transport: str = "ble_gatt"


def _load_bleak() -> tuple[Any, Any]:
    try:
        from bleak import BleakClient, BleakScanner
    except ImportError as exc:  # pragma: no cover - depends on local install
        raise WatchBleDependencyError(
            "Bleak is required; install the project dependencies first"
        ) from exc
    return BleakScanner, BleakClient


def _normalise_uuid(value: object) -> str:
    return str(value).lower()


def _normalise_address(value: object) -> str:
    return str(value).strip().lower()


def _advertisement_values(
    discovered: object,
) -> Iterable[tuple[object, object | None]]:
    if isinstance(discovered, dict):
        for value in discovered.values():
            if isinstance(value, tuple) and len(value) == 2:
                yield value[0], value[1]
            else:
                yield value, None
        return
    for value in discovered if isinstance(discovered, Sequence) else ():
        if isinstance(value, tuple) and len(value) == 2:
            yield value[0], value[1]
        else:
            yield value, None


def _to_scan_result(device: object, advertisement: object | None) -> WatchBleDevice:
    address = str(getattr(device, "address", "") or "").strip()
    if not address:
        raise WatchBleDiscoveryError("scanner returned a device without an address")
    adv_name = getattr(advertisement, "local_name", None)
    name = adv_name or getattr(device, "name", None)
    rssi = getattr(advertisement, "rssi", None)
    if rssi is None:
        rssi = getattr(device, "rssi", None)
    raw_uuids = getattr(advertisement, "service_uuids", ()) or ()
    return WatchBleDevice(
        address=address,
        name=str(name) if name is not None else None,
        rssi=int(rssi) if rssi is not None else None,
        service_uuids=tuple(sorted({_normalise_uuid(item) for item in raw_uuids})),
    )


async def discover_ble_devices(
    *,
    timeout: float = 5.0,
    scanner: object | None = None,
) -> list[WatchBleDevice]:
    """Discover every nearby BLE device without assuming watch compatibility."""

    if timeout <= 0:
        raise ValueError("timeout must be positive")
    if scanner is None:
        scanner_type, _ = _load_bleak()
        scanner = scanner_type
    discover = getattr(scanner, "discover", None)
    if not callable(discover):
        raise TypeError("scanner must provide an async discover method")
    try:
        discovered = await discover(timeout=timeout, return_adv=True)
    except Exception as exc:
        raise WatchBleDiscoveryError(f"BLE scan failed: {exc}") from exc

    found: dict[str, WatchBleDevice] = {}
    for device, advertisement in _advertisement_values(discovered):
        result = _to_scan_result(device, advertisement)
        found[_normalise_address(result.address)] = result
    return sorted(found.values(), key=lambda item: item.address.lower())


async def scan_watches(
    *,
    timeout: float = 5.0,
    service_uuid: str = WATCH_SERVICE_UUID,
    name_prefix: str = DEFAULT_NAME_PREFIX,
    address: str | None = None,
    scanner: object | None = None,
) -> list[WatchBleDevice]:
    """Discover watches by explicit address, service UUID, or product-name prefix.

    The name path is required because current 6202 advertisements do not always
    include the custom service UUID.  The address path covers advertisements
    that expose neither a local name nor the service UUID.
    """

    service_uuid = _normalise_uuid(service_uuid)
    wanted_address = _normalise_address(address) if address else None
    discovered = await discover_ble_devices(timeout=timeout, scanner=scanner)
    found: list[WatchBleDevice] = []
    for result in discovered:
        result_address = _normalise_address(result.address)
        address_match = bool(wanted_address and result_address == wanted_address)
        name_match = bool(result.name and result.name.startswith(name_prefix))
        service_match = service_uuid in result.service_uuids
        if address_match or name_match or service_match:
            found.append(result)
    return found


def select_watch(
    devices: Iterable[WatchBleDevice],
    *,
    address: str | None = None,
    name: str | None = None,
) -> WatchBleDevice:
    """Select exactly one watch, rejecting missing and ambiguous matches."""

    if address and name:
        raise ValueError("select by either address or name, not both")
    candidates = list(devices)
    if address:
        wanted = _normalise_address(address)
        candidates = [
            item for item in candidates if _normalise_address(item.address) == wanted
        ]
    elif name:
        candidates = [item for item in candidates if item.name == name]

    selector = address or name or "watch"
    if not candidates:
        raise WatchBleDiscoveryError(f"no BLE device matched {selector!r}")
    if len(candidates) > 1:
        addresses = ", ".join(item.address for item in candidates)
        raise WatchBleAmbiguousDeviceError(
            f"{selector!r} matched multiple BLE devices: {addresses}"
        )
    return candidates[0]


class WatchBleClient:
    """One BLE connection with sequence-routed concurrent PB exchanges."""

    def __init__(
        self,
        device: WatchBleDevice | str,
        *,
        client_factory: Callable[..., object] | None = None,
        timeout: float = 10.0,
    ) -> None:
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        self.device = device
        self.timeout = float(timeout)
        if client_factory is None:
            _, client_factory = _load_bleak()
        self._client_factory = client_factory
        self._client: object | None = None
        self._write_characteristic: object | None = None
        self._notify_characteristic: object | None = None
        self._decoder = WatchAppFrameDecoder()
        self._message_assembler = WatchAppMessageAssembler()
        self._pending_requests: dict[
            tuple[int, int], asyncio.Future[WatchAppMessage]
        ] = {}
        self._notifications: asyncio.Queue[WatchAppMessage] = asyncio.Queue(
            maxsize=256
        )
        self._dropped_notifications = 0
        self._screenshot_future: asyncio.Future[WatchBleScreenshot] | None = None
        self._screenshot_assembler: WatchBleScreenshotAssembler | None = None
        self._screenshot_ack_queue: (
            asyncio.Queue[WatchBleScreenshotFeedResult | None] | None
        ) = None
        self._screenshot_ack_task: asyncio.Task[None] | None = None
        self._write_lock = asyncio.Lock()
        self._screenshot_lock = asyncio.Lock()
        self._next_sequence = 0
        self._closed = False
        self._disconnected = False
        self._notify_started = False
        self._paired_before_connect = False

    @property
    def connected(self) -> bool:
        return bool(
            self._client is not None
            and getattr(self._client, "is_connected", False)
            and not self._disconnected
        )

    @property
    def _pending(self) -> asyncio.Future[WatchAppMessage] | None:
        """Legacy single-request view retained for existing diagnostics/tests."""

        if len(self._pending_requests) != 1:
            return None
        return next(iter(self._pending_requests.values()))

    @property
    def pending_exchange_count(self) -> int:
        return len(self._pending_requests)

    @property
    def dropped_notification_count(self) -> int:
        return self._dropped_notifications

    def _allocate_sequence(self) -> int:
        active_sequences = {sequence for _, sequence in self._pending_requests}
        for _ in range(0x10000):
            sequence = self._next_sequence
            self._next_sequence = (sequence + 1) & 0xFFFF
            if sequence not in active_sequences:
                return sequence
        raise WatchBleProtocolError("all PB sequence ids are currently in use")

    def _fail_pending_exchanges(self, error: BaseException) -> None:
        for pending in tuple(self._pending_requests.values()):
            if not pending.done():
                pending.set_exception(error)
        self._pending_requests.clear()

    async def connect(self, *, pair: bool = False) -> None:
        if self.connected:
            return
        self._closed = False
        self._disconnected = False
        self._paired_before_connect = pair
        target = (
            self.device.address
            if isinstance(self.device, WatchBleDevice)
            else self.device
        )
        try:
            self._client = self._client_factory(
                target,
                disconnected_callback=self._on_disconnected,
                timeout=self.timeout,
                pair=pair,
            )
            await self._client.connect()
            services = getattr(self._client, "services", None)
            if services is None:
                raise WatchBleConnectionError("connected client has no GATT services")
            service = services.get_service(WATCH_SERVICE_UUID)
            write = services.get_characteristic(WATCH_WRITE_UUID)
            notify = services.get_characteristic(WATCH_NOTIFY_UUID)
            missing = []
            if service is None:
                missing.append(WATCH_SERVICE_UUID)
            if write is None:
                missing.append(WATCH_WRITE_UUID)
            if notify is None:
                missing.append(WATCH_NOTIFY_UUID)
            if missing:
                raise WatchBleConnectionError(
                    "watch GATT UUIDs are missing: " + ", ".join(missing)
                )
            self._write_characteristic = write
            self._notify_characteristic = notify
            self._decoder = WatchAppFrameDecoder()
            self._message_assembler = WatchAppMessageAssembler()
            self._notifications = asyncio.Queue(maxsize=256)
            self._dropped_notifications = 0
            await self._client.start_notify(notify, self._on_notification)
            self._notify_started = True
        except WatchBleError:
            await self.close()
            raise
        except Exception as exc:
            await self.close()
            raise WatchBleConnectionError(f"BLE connection failed: {exc}") from exc

    def _on_disconnected(self, _client: object) -> None:
        self._disconnected = True
        self._fail_pending_exchanges(
            WatchBleDisconnectedError("watch disconnected during exchange")
        )
        screenshot = self._screenshot_future
        if screenshot is not None and not screenshot.done():
            screenshot.set_exception(
                WatchBleDisconnectedError("watch disconnected during screenshot")
            )
        self._screenshot_assembler = None

    def _on_notification(self, _sender: object, data: bytearray) -> None:
        screenshot = self._screenshot_future
        screenshot_active = bool(
            self._screenshot_assembler is not None
            and self._screenshot_ack_queue is not None
        )
        try:
            frames = self._decoder.feed(bytes(data))
        except (WatchAppProtocolError, ValueError) as exc:
            error = WatchBleProtocolError(str(exc))
            self._fail_pending_exchanges(error)
            if screenshot is not None and not screenshot.done():
                screenshot.set_exception(error)
                self._screenshot_assembler = None
            self._decoder = WatchAppFrameDecoder()
            self._message_assembler = WatchAppMessageAssembler()
            return
        for frame in frames:
            try:
                message = self._message_assembler.feed(frame)
            except WatchAppProtocolError as exc:
                error = WatchBleProtocolError(str(exc))
                self._fail_pending_exchanges(error)
                if screenshot is not None and not screenshot.done():
                    screenshot.set_exception(error)
                    self._screenshot_assembler = None
                self._message_assembler = WatchAppMessageAssembler()
                return

            is_screenshot_frame = bool(
                screenshot_active
                and frame.command == BLE_SCREENSHOT_COMMAND
                and self._screenshot_assembler is not None
                and self._screenshot_ack_queue is not None
            )
            if is_screenshot_frame:
                try:
                    result = self._screenshot_assembler.feed(frame.payload)
                except WatchBleScreenshotProtocolRemoteError as exc:
                    if screenshot is not None and not screenshot.done():
                        screenshot.set_exception(WatchBleScreenshotRemoteError(exc))
                    self._screenshot_assembler = None
                except WatchBleScreenshotProtocolError as exc:
                    if screenshot is not None and not screenshot.done():
                        screenshot.set_exception(WatchBleProtocolError(str(exc)))
                    self._screenshot_assembler = None
                else:
                    if result is not None:
                        self._screenshot_ack_queue.put_nowait(result)

            if message is None:
                continue
            pending = self._pending_requests.get(
                (message.command, message.sequence)
            )
            if pending is not None and not pending.done():
                pending.set_result(message)
            elif not is_screenshot_frame:
                if self._notifications.full():
                    self._notifications.get_nowait()
                    self._notifications.task_done()
                    self._dropped_notifications += 1
                self._notifications.put_nowait(message)

    async def _write_screenshot_acks(
        self,
        *,
        capture_sequence: int,
        outer_sequence: int,
        queue: asyncio.Queue[WatchBleScreenshotFeedResult | None],
        screenshot: asyncio.Future[WatchBleScreenshot],
    ) -> None:
        """Write screenshot ACKs in notification order on one BLE task."""

        try:
            while True:
                result = await queue.get()
                try:
                    if result is None:
                        return
                    await self._write_app_frame(
                        WatchAppFrame(
                            command=BLE_SCREENSHOT_COMMAND,
                            sequence=outer_sequence,
                            payload=encode_screenshot_ack(
                                capture_sequence,
                                result.next_chunk,
                            ),
                        )
                    )
                    if result.screenshot is not None:
                        if result.next_chunk != BLE_SCREENSHOT_COMPLETE_ACK:
                            raise WatchBleProtocolError(
                                "completed screenshot has a non-final ACK"
                            )
                        if not screenshot.done():
                            screenshot.set_result(result.screenshot)
                finally:
                    queue.task_done()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if not screenshot.done():
                error = WatchBleConnectionError(
                    f"BLE screenshot ACK write failed: {exc}"
                )
                error.__cause__ = exc
                screenshot.set_exception(error)
            self._screenshot_assembler = None

    async def _stop_screenshot_ack_writer(self, *, drain: bool) -> None:
        """Detach and stop the per-capture ACK writer without leaking a task."""

        task = self._screenshot_ack_task
        queue = self._screenshot_ack_queue
        self._screenshot_ack_task = None
        self._screenshot_ack_queue = None
        if task is None:
            return
        if not task.done():
            if drain and queue is not None:
                queue.put_nowait(None)
            else:
                task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def _write_app_frame(self, frame: WatchAppFrame) -> None:
        async with self._write_lock:
            await self._write_app_frame_unlocked(frame)

    async def _write_app_frame_unlocked(self, frame: WatchAppFrame) -> None:
        if not self.connected or self._client is None:
            raise WatchBleConnectionError("watch is not connected")
        if self._write_characteristic is None or not self._notify_started:
            raise WatchBleConnectionError("watch GATT channel is not ready")

        chunk_size = int(
            getattr(
                self._write_characteristic,
                "max_write_without_response_size",
                20,
            )
            or 20
        )
        if chunk_size <= 0:
            raise WatchBleConnectionError(
                "invalid write-without-response chunk size"
            )
        wire_data = encode_frame(frame)
        for offset in range(0, len(wire_data), chunk_size):
            await self._client.write_gatt_char(
                self._write_characteristic,
                wire_data[offset : offset + chunk_size],
                response=False,
            )

    async def _write_app_message(
        self,
        command: int,
        sequence: int,
        payload: bytes,
        *,
        flags: int = 0,
    ) -> None:
        """Write one complete App payload that fits the single-frame limit."""

        if len(payload) > APP_MAX_DATA_LENGTH:
            raise WatchAppProtocolError(
                f"App payload is {len(payload)} bytes; single-frame limit is "
                f"{APP_MAX_DATA_LENGTH}. Use semantic multipart chunks"
            )
        await self._write_app_frames(
            (
                WatchAppFrame(
                    command=command,
                    sequence=sequence,
                    payload=payload,
                    flags=flags,
                ),
            )
        )

    async def _write_app_frames(
        self, frames: tuple[WatchAppFrame, ...]
    ) -> None:
        async with self._write_lock:
            for frame in frames:
                await self._write_app_frame_unlocked(frame)

    async def _write_multipart_message(
        self,
        command: int,
        sequence: int,
        chunks: tuple[bytes, ...],
        *,
        flags: int = 0,
    ) -> None:
        await self._write_app_frames(
            encode_multipart_chunks(
                command,
                sequence,
                chunks,
                flags=flags,
            )
        )

    async def send(
        self,
        command: int,
        payload: bytes = b"",
        *,
        flags: int = 0,
    ) -> int:
        """Send a normal non-blocking PB command and return its sequence id."""

        if not self.connected:
            raise WatchBleConnectionError("watch is not connected")
        sequence = self._allocate_sequence()
        try:
            await self._write_app_message(
                command,
                sequence,
                payload,
                flags=flags,
            )
        except WatchAppProtocolError as exc:
            raise WatchBleProtocolError(str(exc)) from exc
        except WatchBleError:
            raise
        except Exception as exc:
            raise WatchBleConnectionError(f"BLE send failed: {exc}") from exc
        return sequence

    async def send_multipart(
        self,
        command: int,
        chunks: list[bytes] | tuple[bytes, ...],
        *,
        flags: int = 0,
    ) -> int:
        """Send independently valid semantic object/list fragments."""

        if not self.connected:
            raise WatchBleConnectionError("watch is not connected")
        if not isinstance(chunks, (list, tuple)):
            raise WatchBleProtocolError("chunks must be a list or tuple of bytes")
        sequence = self._allocate_sequence()
        try:
            await self._write_multipart_message(
                command,
                sequence,
                tuple(chunks),
                flags=flags,
            )
        except WatchAppProtocolError as exc:
            raise WatchBleProtocolError(str(exc)) from exc
        except WatchBleError:
            raise
        except Exception as exc:
            raise WatchBleConnectionError(f"BLE send failed: {exc}") from exc
        return sequence

    async def send_protobuf(
        self,
        command: int,
        message: Message | None = None,
        *,
        flags: int = 0,
    ) -> int:
        """Serialize and send a normal non-blocking PB command."""

        payload = b"" if message is None else _encode_pb_payload(message)
        return await self.send(command, payload, flags=flags)

    async def send_protobuf_chunks(
        self,
        command: int,
        messages: list[Message] | tuple[Message, ...],
        *,
        flags: int = 0,
    ) -> int:
        """Send semantic multipart PB objects that are valid independently."""

        if not isinstance(messages, (list, tuple)):
            raise WatchBleProtocolError(
                "messages must be a list or tuple of protobuf messages"
            )
        return await self.send_multipart(
            command,
            tuple(_encode_pb_payload(message) for message in messages),
            flags=flags,
        )

    async def receive(self, *, timeout: float | None = None) -> WatchAppMessage:
        """Receive the next complete unsolicited Device PB message."""

        if timeout is not None and timeout <= 0:
            raise ValueError("timeout must be positive")
        try:
            if timeout is None:
                message = await self._notifications.get()
            else:
                message = await asyncio.wait_for(
                    self._notifications.get(), timeout
                )
        except asyncio.TimeoutError as exc:
            raise WatchBleTimeoutError(
                "timed out waiting for an unsolicited PB message",
                code="NOTIFICATION_TIMEOUT",
            ) from exc
        self._notifications.task_done()
        return message

    async def receive_protobuf(
        self,
        message_type: type[_MessageT],
        *,
        timeout: float | None = None,
    ) -> tuple[WatchAppMessage, _MessageT]:
        """Receive a Device message and decode its protobuf payload."""

        message = await self.receive(timeout=timeout)
        return message, _decode_pb_payload(message.payload, message_type)

    async def exchange(
        self,
        command: int,
        payload: bytes,
        *,
        timeout: float | None = None,
    ) -> WatchAppMessage:
        return await self._exchange(command, payload, timeout=timeout)

    async def exchange_multipart(
        self,
        command: int,
        chunks: list[bytes] | tuple[bytes, ...],
        *,
        timeout: float | None = None,
    ) -> WatchAppMessage:
        """Run a blocking request made of valid semantic object/list chunks."""

        if not isinstance(chunks, (list, tuple)):
            raise WatchBleProtocolError("chunks must be a list or tuple of bytes")
        return await self._exchange(
            command,
            b"",
            timeout=timeout,
            multipart_chunks=tuple(chunks),
        )

    async def exchange_protobuf(
        self,
        command: int,
        request: Message | None,
        response_type: type[_MessageT],
        *,
        timeout: float | None = None,
    ) -> _MessageT:
        """Run an App blocking request with generated protobuf messages."""

        payload = b"" if request is None else _encode_pb_payload(request)
        response = await self.exchange(command, payload, timeout=timeout)
        return _decode_pb_payload(response.payload, response_type)

    async def exchange_protobuf_chunks(
        self,
        command: int,
        requests: list[Message] | tuple[Message, ...],
        response_type: type[_MessageT],
        *,
        timeout: float | None = None,
    ) -> _MessageT:
        """Run a blocking request with semantic multipart PB objects."""

        if not isinstance(requests, (list, tuple)):
            raise WatchBleProtocolError(
                "requests must be a list or tuple of protobuf messages"
            )
        response = await self.exchange_multipart(
            command,
            tuple(_encode_pb_payload(request) for request in requests),
            timeout=timeout,
        )
        return _decode_pb_payload(response.payload, response_type)

    async def _exchange(
        self,
        command: int,
        payload: bytes,
        *,
        timeout: float | None,
        after_write: Callable[[float], Awaitable[None]] | None = None,
        multipart_chunks: tuple[bytes, ...] | None = None,
    ) -> WatchAppMessage:
        if not self.connected or self._client is None:
            raise WatchBleConnectionError("watch is not connected")
        if self._write_characteristic is None or not self._notify_started:
            raise WatchBleConnectionError("watch GATT channel is not ready")
        wait_seconds = self.timeout if timeout is None else float(timeout)
        if wait_seconds <= 0:
            raise ValueError("timeout must be positive")

        if not self.connected:
            raise WatchBleDisconnectedError("watch disconnected before exchange")
        sequence = self._allocate_sequence()
        try:
            if multipart_chunks is None:
                if len(payload) > APP_MAX_DATA_LENGTH:
                    raise WatchAppProtocolError(
                        f"App payload is {len(payload)} bytes; single-frame "
                        f"limit is {APP_MAX_DATA_LENGTH}. Use semantic "
                        "multipart chunks"
                    )
                frames = (
                    WatchAppFrame(
                        command=command,
                        sequence=sequence,
                        payload=payload,
                    ),
                )
            else:
                frames = encode_multipart_chunks(
                    command,
                    sequence,
                    multipart_chunks,
                )
        except WatchAppProtocolError as exc:
            raise WatchBleProtocolError(str(exc)) from exc
        loop = asyncio.get_running_loop()
        deadline = loop.time() + wait_seconds
        pending: asyncio.Future[WatchAppMessage] = loop.create_future()
        request_key = (command, sequence)
        self._pending_requests[request_key] = pending
        try:
            if after_write is None:
                await self._write_app_frames(frames)
            else:
                async with self._write_lock:
                    for frame in frames:
                        await self._write_app_frame_unlocked(frame)
                    remaining = deadline - loop.time()
                    if remaining <= 0:
                        raise WatchBlePairingTimeoutError(
                            "BLE pairing could not start before bind timed out"
                        )
                    await after_write(remaining)
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise WatchBleTimeoutError(
                    f"timed out waiting for command=0x{command:04x} "
                    f"sequence={sequence}"
                )
            try:
                return await asyncio.wait_for(pending, remaining)
            except asyncio.TimeoutError as exc:
                raise WatchBleTimeoutError(
                    f"timed out waiting for command=0x{command:04x} "
                    f"sequence={sequence}"
                ) from exc
        except WatchBleError:
            raise
        except Exception as exc:
            raise WatchBleConnectionError(f"BLE exchange failed: {exc}") from exc
        finally:
            # A disconnect can complete the response future while a separate
            # write/pairing error is already being propagated. Consume or
            # cancel it so asyncio does not report an orphaned exception.
            if pending.done() and not pending.cancelled():
                pending.exception()
            else:
                pending.cancel()
            if self._pending_requests.get(request_key) is pending:
                del self._pending_requests[request_key]

    async def capture_screenshot(
        self,
        output_path: str | Path,
        *,
        sequence: int | None = None,
        timeout: float = 180.0,
    ) -> WatchBleScreenshotResult:
        """Request, reassemble, verify, and atomically save one BLE screenshot."""

        if timeout <= 0:
            raise ValueError("timeout must be positive")
        capture_sequence = secrets.randbits(32) if sequence is None else sequence
        try:
            request_payload = encode_screenshot_request(capture_sequence)
        except WatchBleScreenshotProtocolError as exc:
            raise WatchBleProtocolError(str(exc)) from exc

        async with self._screenshot_lock:
            if not self.connected:
                raise WatchBleDisconnectedError(
                    "watch disconnected before screenshot request"
                )
            outer_sequence = self._allocate_sequence()
            loop = asyncio.get_running_loop()
            screenshot: asyncio.Future[WatchBleScreenshot] = loop.create_future()
            self._screenshot_future = screenshot
            self._screenshot_assembler = WatchBleScreenshotAssembler(capture_sequence)
            ack_queue: asyncio.Queue[
                WatchBleScreenshotFeedResult | None
            ] = asyncio.Queue()
            self._screenshot_ack_queue = ack_queue
            self._screenshot_ack_task = asyncio.create_task(
                self._write_screenshot_acks(
                    capture_sequence=capture_sequence,
                    outer_sequence=outer_sequence,
                    queue=ack_queue,
                    screenshot=screenshot,
                ),
                name=f"watch-ble-screenshot-ack-{capture_sequence}",
            )
            try:
                await self._write_app_frame(
                    WatchAppFrame(
                        command=BLE_SCREENSHOT_COMMAND,
                        sequence=outer_sequence,
                        payload=request_payload,
                    )
                )
                try:
                    captured = await asyncio.wait_for(screenshot, timeout)
                except asyncio.TimeoutError as exc:
                    raise WatchBleTimeoutError(
                        f"timed out waiting for BLE screenshot "
                        f"sequence={capture_sequence}",
                        code="SCREENSHOT_TIMEOUT",
                    ) from exc
                try:
                    saved = write_verified_screenshot(captured, output_path)
                except OSError as exc:
                    raise WatchBleScreenshotError(
                        f"cannot save BLE screenshot to {output_path}: {exc}"
                    ) from exc
                return WatchBleScreenshotResult(
                    sequence=captured.sequence,
                    path=str(saved),
                    file_size=captured.file_size,
                    crc32=f"{captured.crc32:08x}",
                    payload_crc32=f"{captured.payload_crc32:08x}",
                    chunks=captured.chunk_count,
                    chunk_size=captured.chunk_size,
                    width=captured.width,
                    height=captured.height,
                )
            except WatchBleError:
                raise
            except (WatchBleScreenshotProtocolError, ValueError) as exc:
                raise WatchBleProtocolError(str(exc)) from exc
            except Exception as exc:
                raise WatchBleConnectionError(
                    f"BLE screenshot exchange failed: {exc}"
                ) from exc
            finally:
                completed = bool(
                    screenshot.done()
                    and not screenshot.cancelled()
                    and screenshot.exception() is None
                )
                self._screenshot_assembler = None
                await self._stop_screenshot_ack_writer(drain=completed)
                if screenshot.done() and not screenshot.cancelled():
                    screenshot.exception()
                else:
                    screenshot.cancel()
                self._screenshot_future = None

    async def _pair_after_bind_write(self, timeout: float) -> None:
        client = self._client
        pair = getattr(client, "pair", None) if client is not None else None
        if not callable(pair):
            raise WatchBlePairingError("BLE client does not support pairing")
        try:
            await asyncio.wait_for(pair(), timeout)
        except asyncio.TimeoutError as exc:
            raise WatchBlePairingTimeoutError(
                "BLE pairing timed out after the bind request was written"
            ) from exc
        except WatchBleError:
            raise
        except Exception as exc:
            raise WatchBlePairingError(f"BLE pairing failed: {exc}") from exc

    async def bind(
        self,
        request: WatchAuthRequest,
        *,
        timeout: float | None = None,
    ) -> WatchBleAuthResult:
        was_paired_before_connect = self._paired_before_connect
        return await self._authenticate(
            "bind",
            BIND_COMMAND,
            request,
            timeout,
            after_write=(
                None if was_paired_before_connect else self._pair_after_bind_write
            ),
            classify_classic_gate=was_paired_before_connect,
        )

    async def login(
        self,
        request: WatchAuthRequest,
        *,
        timeout: float | None = None,
    ) -> WatchBleAuthResult:
        return await self._authenticate(
            "login",
            LOGIN_COMMAND,
            request,
            timeout,
            classify_classic_gate=True,
        )

    async def _authenticate(
        self,
        operation: str,
        command: int,
        request: WatchAuthRequest,
        timeout: float | None,
        *,
        after_write: Callable[[float], Awaitable[None]] | None = None,
        classify_classic_gate: bool = False,
    ) -> WatchBleAuthResult:
        try:
            frame = await self._exchange(
                command,
                encode_auth_request(request),
                timeout=timeout,
                after_write=after_write,
            )
        except WatchBleTimeoutError as exc:
            if exc.code == "RESPONSE_TIMEOUT" and classify_classic_gate:
                raise WatchBleClassicGateTimeoutError(operation) from exc
            raise
        try:
            response = decode_auth_response(frame.payload)
        except (WatchAppProtocolError, ValueError) as exc:
            raise WatchBleProtocolError(str(exc)) from exc
        allowed = {0, 4} if operation == "bind" else {0}
        if response.result not in allowed:
            raise WatchBleAuthError(operation, response)
        return WatchBleAuthResult(
            operation=operation,
            result=response.result,
            ever_bound=response.ever_bound,
            bind_time=response.bind_time,
            status=(
                "success_result_4" if response.result == 4 else "success"
            ),
        )

    async def close(self) -> None:
        if self._closed and self._client is None:
            return
        self._closed = True
        self._fail_pending_exchanges(
            WatchBleDisconnectedError("BLE client closed")
        )
        screenshot = self._screenshot_future
        if screenshot is not None and not screenshot.done():
            screenshot.set_exception(WatchBleDisconnectedError("BLE client closed"))
        self._screenshot_assembler = None
        self._message_assembler.reset()
        await self._stop_screenshot_ack_writer(drain=False)
        client = self._client
        notify_characteristic = self._notify_characteristic
        self._client = None
        self._write_characteristic = None
        self._notify_characteristic = None
        self._notify_started = False
        self._paired_before_connect = False
        if client is None:
            return
        try:
            if getattr(client, "is_connected", False):
                stop_notify = getattr(client, "stop_notify", None)
                if callable(stop_notify) and notify_characteristic is not None:
                    value = stop_notify(notify_characteristic)
                    if inspect.isawaitable(value):
                        await value
                await client.disconnect()
        except Exception:
            # close is deliberately idempotent and best-effort.
            return

    async def __aenter__(self) -> WatchBleClient:
        await self.connect()
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.close()


def _json_print(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Watch BLE GATT client")
    subparsers = parser.add_subparsers(dest="command", required=True)
    scan = subparsers.add_parser("scan")
    scan.add_argument("--timeout", type=float, default=5.0)

    for name in ("bind", "login", "status", "screenshot"):
        command = subparsers.add_parser(name)
        command.add_argument("--address")
        command.add_argument("--name")
        command.add_argument("--scan-timeout", type=float, default=5.0)
        command.add_argument(
            "--timeout",
            type=float,
            default=180.0 if name == "screenshot" else 15.0,
        )
        if name in {"bind", "login"}:
            command.add_argument("--user-id", default="agent-loop-system")
            command.add_argument("--auth-code", required=name == "bind")
        elif name == "screenshot":
            command.add_argument("--output", required=True)
            command.add_argument("--sequence", type=lambda value: int(value, 0))
    return parser


async def _run_cli(
    args: argparse.Namespace,
    *,
    scanner: object | None = None,
    client_factory: Callable[..., object] | None = None,
) -> dict[str, object]:
    scan_timeout = (
        args.timeout if args.command == "scan" else args.scan_timeout
    )
    devices = await scan_watches(
        timeout=scan_timeout,
        address=getattr(args, "address", None),
        scanner=scanner,
    )
    if args.command == "scan":
        return {"ok": True, "devices": [asdict(item) for item in devices]}
    device = select_watch(devices, address=args.address, name=args.name)
    client = WatchBleClient(
        device,
        timeout=args.timeout,
        client_factory=client_factory,
    )
    try:
        # Bind must send 0x0301 before initiating pairing so the firmware can
        # cache that request until the pairing event arrives. Login may use an
        # existing bond; status is always non-pairing.
        await client.connect(pair=args.command == "login")
        if args.command == "status":
            return {
                "ok": True,
                "connected": True,
                "transport": "ble_gatt",
                "pairing_attempted": False,
                "device": asdict(device),
            }
        if args.command == "screenshot":
            result = await client.capture_screenshot(
                args.output,
                sequence=args.sequence,
                timeout=args.timeout,
            )
            return {
                "ok": True,
                "device": asdict(device),
                "screenshot": asdict(result),
            }
        request = WatchAuthRequest(
            user_id=args.user_id,
            auth_code=args.auth_code or "",
        )
        result = (
            await client.bind(request)
            if args.command == "bind"
            else await client.login(request)
        )
        return {
            "ok": True,
            "device": asdict(device),
            "auth": asdict(result),
        }
    finally:
        await client.close()


def _error_payload(exc: WatchBleError) -> dict[str, object]:
    error: dict[str, object] = {
        "type": type(exc).__name__,
        "message": str(exc),
    }
    code = getattr(exc, "code", None)
    if isinstance(code, str):
        error["code"] = code
    remote_code = getattr(exc, "remote_code", None)
    if isinstance(remote_code, int):
        error["remote_code"] = remote_code
        error["reason"] = getattr(exc, "reason", "unknown")
    return {"ok": False, "error": error}


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        _json_print(asyncio.run(_run_cli(args)))
        return 0
    except WatchBleError as exc:
        _json_print(_error_payload(exc))
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
