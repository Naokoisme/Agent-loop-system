"""Persistent PC-to-579 BLE broker shared by the UI and test Runner."""

from __future__ import annotations

import asyncio
from collections import deque
from concurrent.futures import Future as ConcurrentFuture
from datetime import datetime
import math
import secrets
import threading
from typing import Any, Callable
import uuid

from agent_loop_system.tools.watch_579_protocol import (
    DEFAULT_SEQUENCE,
    WATCH_579_NOTIFY_UUID,
    WATCH_579_SERVICE_UUID,
    WATCH_579_WRITE_UUID,
    Watch579FrameDecoder,
    Watch579Message,
    Watch579ProtocolError,
    Watch579RawCommand,
    Watch579TransportAck,
    encode_raw_command,
    encode_transport_ack,
    format_hex,
    normalize_raw_command,
)


class Watch579BleError(RuntimeError):
    reason_code = "BLE_ERROR"

    def __init__(self, message: str, *, reason_code: str | None = None) -> None:
        self.reason_code = reason_code or type(self).reason_code
        super().__init__(message)


class Watch579BleUnavailable(Watch579BleError):
    reason_code = "BLE_UNAVAILABLE"


class Watch579DeviceNotFound(Watch579BleError):
    reason_code = "BLE_DEVICE_NOT_FOUND"


class Watch579ConnectTimeout(Watch579BleError):
    reason_code = "BLE_CONNECT_TIMEOUT"


class Watch579GattProfileMismatch(Watch579BleError):
    reason_code = "BLE_GATT_PROFILE_MISMATCH"


class Watch579Disconnected(Watch579BleError):
    reason_code = "BLE_DISCONNECTED"


class Watch579AckTimeout(Watch579BleError):
    reason_code = "BLE_ACK_TIMEOUT"


class Watch579TargetBusy(Watch579BleError):
    reason_code = "TARGET_BUSY"


class Watch579InvalidRawCommand(Watch579BleError):
    reason_code = "INVALID_RAW_COMMAND"


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


def _address_key(value: object) -> str:
    return str(value or "").strip().casefold()


def _positive_timeout(value: object, *, label: str, maximum: float = 120.0) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{label} must be a positive number")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a positive number") from exc
    if not math.isfinite(parsed) or not 0 < parsed <= maximum:
        raise ValueError(f"{label} must be in 0..{maximum:g} seconds")
    return parsed


def _load_bleak() -> tuple[Any, Any]:
    try:
        from bleak import BleakClient, BleakScanner
    except (ImportError, OSError) as exc:
        raise Watch579BleUnavailable(f"Bleak runtime is unavailable: {exc}") from exc
    return BleakScanner, BleakClient


def _advertisement_values(discovered: object) -> list[tuple[object, object | None]]:
    if isinstance(discovered, dict):
        values = list(discovered.values())
    else:
        values = list(discovered or [])  # type: ignore[arg-type]
    normalized: list[tuple[object, object | None]] = []
    for value in values:
        if isinstance(value, tuple) and len(value) >= 2:
            normalized.append((value[0], value[1]))
        else:
            normalized.append((value, None))
    return normalized


class Watch579BleBroker:
    """Own one BLE event loop, one GATT connection, and one serialized writer.

    Public methods are synchronous because ``frontend.server`` uses a threaded
    standard-library HTTP server.  BLE operations stay on the broker's private
    asyncio loop and both page requests and child Runner requests call the same
    object through HTTP.
    """

    def __init__(
        self,
        *,
        scanner: object | None = None,
        client_factory: Callable[..., object] | None = None,
        ack_timeout: float = 5.0,
        event_capacity: int = 2000,
    ) -> None:
        self.ack_timeout = _positive_timeout(
            ack_timeout, label="ACK timeout", maximum=60
        )
        if event_capacity < 100:
            raise ValueError("event_capacity must be at least 100")
        self._scanner = scanner
        self._client_factory = client_factory
        self._event_capacity = int(event_capacity)
        self._loop = asyncio.new_event_loop()
        self._loop_ready = threading.Event()
        self._thread = threading.Thread(
            target=self._run_loop,
            name="watch-579-ble-broker",
            daemon=True,
        )
        self._thread.start()
        if not self._loop_ready.wait(5.0):
            raise RuntimeError("579 BLE broker event loop did not start")
        self._call(self._initialize())

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop_ready.set()
        self._loop.run_forever()
        pending = asyncio.all_tasks(self._loop)
        for task in pending:
            task.cancel()
        if pending:
            self._loop.run_until_complete(
                asyncio.gather(*pending, return_exceptions=True)
            )
        self._loop.close()

    async def _initialize(self) -> None:
        self._client: object | None = None
        self._device: object | None = None
        self._address = ""
        self._name = ""
        self._rssi: int | None = None
        self._state = "disconnected"
        self._last_error: dict[str, str] | None = None
        self._decoder = Watch579FrameDecoder()
        self._command_lock = asyncio.Lock()
        self._gatt_write_lock = asyncio.Lock()
        self._pending_ack: asyncio.Future[bytes] | None = None
        self._pending_sequence: int | None = None
        self._lease_token: str | None = None
        self._lease_owner: str | None = None
        self._events: deque[dict[str, Any]] = deque(maxlen=self._event_capacity)
        self._next_cursor = 1
        self._closed = False
        self._emit("BROKER_STARTED", message="579 BLE Broker 已启动")

    def _call(self, coroutine, *, timeout: float | None = None):
        if not self._thread.is_alive():
            raise RuntimeError("579 BLE broker is closed")
        future: ConcurrentFuture = asyncio.run_coroutine_threadsafe(
            coroutine, self._loop
        )
        return future.result(timeout)

    def _emit(self, kind: str, **payload: Any) -> dict[str, Any]:
        event = {
            "cursor": self._next_cursor,
            "at": _now(),
            "kind": kind,
            **payload,
        }
        self._next_cursor += 1
        self._events.append(event)
        return event

    @staticmethod
    def _device_view(device: object, advertisement: object | None) -> dict[str, Any]:
        address = str(getattr(device, "address", "") or "").strip()
        advertised_name = getattr(advertisement, "local_name", None)
        name = str(advertised_name or getattr(device, "name", "") or "").strip()
        rssi = getattr(advertisement, "rssi", None)
        if rssi is None:
            rssi = getattr(device, "rssi", None)
        return {
            "address": address,
            "name": name or None,
            "rssi": int(rssi) if rssi is not None else None,
        }

    async def _discover(self, timeout: float) -> list[tuple[object, object | None]]:
        scanner = self._scanner
        if scanner is None:
            scanner, _ = _load_bleak()
        discover = getattr(scanner, "discover", None)
        if not callable(discover):
            raise Watch579BleUnavailable("BLE scanner has no discover method")
        try:
            discovered = await discover(timeout=timeout, return_adv=True)
        except Watch579BleError:
            raise
        except Exception as exc:
            raise Watch579BleError(
                f"BLE scan failed: {exc}", reason_code="BLE_SCAN_FAILED"
            ) from exc
        return _advertisement_values(discovered)

    def scan(self, *, timeout: object = 8, query: object = "") -> dict[str, Any]:
        return self._call(self._scan(timeout=timeout, query=query))

    def check_dependencies(self) -> dict[str, bool]:
        """Verify that production scan/client dependencies can be loaded.

        Tests may inject both sides of the BLE boundary; production instances
        still exercise the real Bleak import during preflight instead of
        reporting it available before the first connection attempt.
        """

        if self._scanner is None or self._client_factory is None:
            _load_bleak()
        return {"bleak": True}

    async def _scan(self, *, timeout: object, query: object) -> dict[str, Any]:
        scan_timeout = _positive_timeout(timeout, label="scan timeout", maximum=60)
        needle = str(query or "").strip().casefold()
        if len(needle) > 200:
            raise ValueError("BLE scan query is too long")
        self._emit("SCAN_STARTED", timeout=scan_timeout)
        values = await self._discover(scan_timeout)
        items = [self._device_view(device, advertisement) for device, advertisement in values]
        items = [item for item in items if item["address"]]
        if needle:
            items = [
                item
                for item in items
                if needle in str(item["address"]).casefold()
                or needle in str(item.get("name") or "").casefold()
            ]
        items.sort(
            key=lambda item: (
                item.get("rssi") is None,
                -(item.get("rssi") or -999),
                str(item.get("name") or "").casefold(),
                str(item["address"]).casefold(),
            )
        )
        self._emit("SCAN_COMPLETED", count=len(items))
        return {"items": items, "scanned_at": _now(), "query": str(query or "")}

    @property
    def connected(self) -> bool:
        return bool(
            self._client is not None
            and getattr(self._client, "is_connected", False)
            and self._state == "ready"
        )

    def status(self) -> dict[str, Any]:
        return self._call(self._status())

    async def _status(self) -> dict[str, Any]:
        return {
            "state": self._state,
            "connected": self.connected,
            "ready": self.connected,
            "address": self._address or None,
            "name": self._name or None,
            "rssi": self._rssi,
            "service_uuid": WATCH_579_SERVICE_UUID,
            "write_uuid": WATCH_579_WRITE_UUID,
            "notify_uuid": WATCH_579_NOTIFY_UUID,
            "write_with_response": True,
            "lease": {
                "active": self._lease_token is not None,
                "owner": self._lease_owner,
            },
            "last_error": dict(self._last_error) if self._last_error else None,
            "latest_cursor": self._next_cursor - 1,
        }

    def events(self, *, after: object = 0, limit: object = 200) -> dict[str, Any]:
        return self._call(self._events_after(after=after, limit=limit))

    async def _events_after(self, *, after: object, limit: object) -> dict[str, Any]:
        if isinstance(after, bool) or isinstance(limit, bool):
            raise ValueError("event cursor and limit must be integers")
        try:
            cursor = int(after)
            count = int(limit)
        except (TypeError, ValueError) as exc:
            raise ValueError("event cursor and limit must be integers") from exc
        if cursor < 0 or not 1 <= count <= 1000:
            raise ValueError("event cursor must be >= 0 and limit must be in 1..1000")
        items = [dict(item) for item in self._events if item["cursor"] > cursor][:count]
        return {
            "items": items,
            "next_cursor": items[-1]["cursor"] if items else cursor,
            "latest_cursor": self._next_cursor - 1,
        }

    def connect(self, *, address: object, timeout: object = 15) -> dict[str, Any]:
        return self._call(
            self._connect(address=address, timeout=timeout, lease_token=None)
        )

    def connect_internal(
        self,
        *,
        lease_token: object,
        address: object,
        timeout: object = 15,
    ) -> dict[str, Any]:
        return self._call(
            self._connect(
                address=address,
                timeout=timeout,
                lease_token=str(lease_token or ""),
            )
        )

    async def _connect(
        self,
        *,
        address: object,
        timeout: object,
        lease_token: str | None,
    ) -> dict[str, Any]:
        wanted = str(address or "").strip()
        if not wanted:
            raise Watch579DeviceNotFound("579 BLE address is not configured")
        if len(wanted) > 200:
            raise ValueError("BLE address is too long")
        connect_timeout = _positive_timeout(
            timeout, label="connect timeout", maximum=60
        )
        if self._lease_token:
            if not lease_token or not secrets.compare_digest(
                lease_token, self._lease_token
            ):
                raise Watch579TargetBusy(
                    f"579 target is owned by automation task {self._lease_owner or ''}".strip()
                )
        if self.connected and _address_key(self._address) == _address_key(wanted):
            self._emit("CONNECTION_REUSED", address=self._address)
            return await self._status()
        if self._client is not None:
            await self._disconnect(force=True)

        self._state = "scanning"
        self._last_error = None
        self._emit("CONNECT_REQUESTED", address=wanted, timeout=connect_timeout)
        values = await self._discover(connect_timeout)
        matches = [
            (device, advertisement)
            for device, advertisement in values
            if _address_key(getattr(device, "address", "")) == _address_key(wanted)
        ]
        if not matches:
            self._state = "disconnected"
            raise Watch579DeviceNotFound(
                f"exact BLE address {wanted} was not found during the scan"
            )
        # Native BLEDevice is deliberately retained and passed to BleakClient;
        # a duplicate advertised name can never redirect this connection.
        device, advertisement = matches[0]
        view = self._device_view(device, advertisement)
        client_factory = self._client_factory
        if client_factory is None:
            _, client_factory = _load_bleak()
        self._state = "connecting"
        client = client_factory(
            device,
            disconnected_callback=self._on_disconnected,
            timeout=connect_timeout,
        )
        self._client = client
        try:
            await asyncio.wait_for(client.connect(), timeout=connect_timeout)
            if not getattr(client, "is_connected", False):
                raise Watch579Disconnected("Bleak connected call returned disconnected")
            services = getattr(client, "services", None)
            if services is None:
                raise Watch579GattProfileMismatch("connected device exposes no GATT services")
            service = services.get_service(WATCH_579_SERVICE_UUID)
            write = services.get_characteristic(WATCH_579_WRITE_UUID)
            notify = services.get_characteristic(WATCH_579_NOTIFY_UUID)
            if service is None or write is None or notify is None:
                missing = [
                    value
                    for value, found in (
                        (WATCH_579_SERVICE_UUID, service),
                        (WATCH_579_WRITE_UUID, write),
                        (WATCH_579_NOTIFY_UUID, notify),
                    )
                    if found is None
                ]
                raise Watch579GattProfileMismatch(
                    "579 GATT profile is missing: " + ", ".join(missing)
                )
            self._write_characteristic = write
            self._notify_characteristic = notify
            self._decoder.reset()
            # Notify subscription is the final readiness gate and always occurs
            # before the first write can be accepted.
            await asyncio.wait_for(
                client.start_notify(notify, self._notification_callback),
                timeout=connect_timeout,
            )
            self._device = device
            self._address = view["address"]
            self._name = str(view.get("name") or "")
            self._rssi = view.get("rssi")
            self._state = "ready"
            self._emit(
                "CONNECTED",
                address=self._address,
                name=self._name or None,
                rssi=self._rssi,
                notify_subscribed=True,
            )
            return await self._status()
        except asyncio.TimeoutError as exc:
            await self._disconnect(force=True)
            self._set_error("BLE_CONNECT_TIMEOUT", f"BLE connect timed out: {exc}")
            raise Watch579ConnectTimeout("BLE connect or GATT discovery timed out") from exc
        except Watch579BleError as exc:
            await self._disconnect(force=True)
            self._set_error(exc.reason_code, str(exc))
            raise
        except Exception as exc:
            await self._disconnect(force=True)
            self._set_error("BLE_CONNECT_FAILED", str(exc))
            raise Watch579BleError(
                f"BLE connection failed: {exc}", reason_code="BLE_CONNECT_FAILED"
            ) from exc

    def _set_error(self, code: str, message: str) -> None:
        self._last_error = {"reason_code": code, "message": message}
        self._emit("ERROR", reason_code=code, message=message)

    def disconnect(self) -> dict[str, Any]:
        return self._call(self._disconnect(force=False))

    async def _disconnect(self, *, force: bool) -> dict[str, Any]:
        if self._lease_token and not force:
            raise Watch579TargetBusy(
                f"579 target is owned by automation task {self._lease_owner or ''}".strip()
            )
        client = self._client
        self._client = None
        self._state = "disconnecting" if client is not None else "disconnected"
        pending = self._pending_ack
        if pending is not None and not pending.done():
            pending.set_exception(Watch579Disconnected("watch disconnected"))
        self._pending_ack = None
        self._pending_sequence = None
        if client is not None:
            try:
                stop_notify = getattr(client, "stop_notify", None)
                if callable(stop_notify) and hasattr(self, "_notify_characteristic"):
                    await stop_notify(self._notify_characteristic)
            except Exception:
                pass
            try:
                await client.disconnect()
            except Exception:
                pass
        old_address = self._address
        self._device = None
        self._address = ""
        self._name = ""
        self._rssi = None
        self._decoder.reset()
        self._state = "disconnected"
        self._emit("DISCONNECTED", address=old_address or None)
        return await self._status()

    def _on_disconnected(self, client: object) -> None:
        if self._closed:
            return
        self._loop.call_soon_threadsafe(self._handle_disconnected, client)

    def _handle_disconnected(self, client: object) -> None:
        # An explicit target switch can deliver the old client's callback after
        # the new client is already ready. Never let that stale callback tear
        # down the replacement session.
        if client is not self._client:
            return
        self._client = None
        self._device = None
        self._decoder.reset()
        self._state = "disconnected"
        pending = self._pending_ack
        if pending is not None and not pending.done():
            pending.set_exception(
                Watch579Disconnected("watch disconnected while waiting for ACK")
            )
        self._emit("DISCONNECTED", address=self._address or None, unexpected=True)

    def _notification_callback(self, _sender: object, data: bytearray) -> None:
        if self._closed:
            return
        asyncio.run_coroutine_threadsafe(
            self._process_notification(bytes(data)), self._loop
        )

    async def _process_notification(self, data: bytes) -> None:
        self._emit(
            "RX_NOTIFY",
            byte_count=len(data),
            packet_hex=format_hex(data),
        )
        try:
            frames = self._decoder.feed(data)
        except Watch579ProtocolError as exc:
            self._set_error("BLE_RX_CRC_OR_LENGTH_ERROR", str(exc))
            pending = self._pending_ack
            if pending is not None and not pending.done():
                pending.set_exception(exc)
            return
        for frame in frames:
            if isinstance(frame, Watch579TransportAck):
                self._emit(
                    "RX_TRANSPORT_ACK",
                    sequence=frame.sequence,
                    ack_hex=format_hex(frame.packet),
                    message="手表已返回 L1 ACK；这不等于业务效果成功",
                )
                pending = self._pending_ack
                if (
                    pending is not None
                    and not pending.done()
                    and self._pending_sequence == frame.sequence
                ):
                    pending.set_result(frame.packet)
                continue
            if isinstance(frame, Watch579Message):
                command = frame.command
                self._emit(
                    "RX_DATA",
                    sequence=frame.sequence,
                    cmd=command.cmd_hex,
                    key=command.key_hex,
                    data_hex=command.data_hex,
                    packet_hex=format_hex(frame.packet),
                )
                ack = encode_transport_ack(frame.sequence)
                try:
                    async with self._gatt_write_lock:
                        client = self._require_client()
                        await client.write_gatt_char(
                            self._write_characteristic,
                            ack,
                            response=True,
                        )
                    self._emit(
                        "TX_HOST_ACK",
                        sequence=frame.sequence,
                        packet_hex=format_hex(ack),
                    )
                except Exception as exc:
                    self._set_error("BLE_DISCONNECTED", f"host ACK write failed: {exc}")
                    pending = self._pending_ack
                    if pending is not None and not pending.done():
                        pending.set_exception(
                            Watch579Disconnected(f"host ACK write failed: {exc}")
                        )

    def _require_client(self):
        if not self.connected or self._client is None:
            raise Watch579Disconnected("579 watch is not connected and ready")
        return self._client

    def preview(self, *, cmd: object, key: object, data: object = "") -> dict[str, Any]:
        try:
            command = normalize_raw_command(cmd, key, data)
            packet = encode_raw_command(command)
        except Watch579ProtocolError as exc:
            raise Watch579InvalidRawCommand(str(exc)) from exc
        return {
            "cmd": command.cmd_hex,
            "key": command.key_hex,
            "data_hex": command.data_hex,
            "data_length": len(command.data),
            "packet_hex": format_hex(packet),
            "packet_length": len(packet),
            "write_with_response": True,
        }

    def send_manual(self, *, cmd: object, key: object, data: object = "") -> dict[str, Any]:
        command = self._normalize_for_send(cmd, key, data)
        return self._call(self._send(command, source="page", lease_token=None))

    def send_internal(
        self,
        *,
        lease_token: object,
        cmd: object,
        key: object,
        data: object = "",
    ) -> dict[str, Any]:
        command = self._normalize_for_send(cmd, key, data)
        return self._call(
            self._send(
                command,
                source="automation",
                lease_token=str(lease_token or ""),
            )
        )

    @staticmethod
    def _normalize_for_send(cmd: object, key: object, data: object) -> Watch579RawCommand:
        try:
            return normalize_raw_command(cmd, key, data)
        except Watch579ProtocolError as exc:
            raise Watch579InvalidRawCommand(str(exc)) from exc

    async def _send(
        self,
        command: Watch579RawCommand,
        *,
        source: str,
        lease_token: str | None,
    ) -> dict[str, Any]:
        self._authorize_write(source=source, lease_token=lease_token)
        async with self._command_lock:
            self._authorize_write(source=source, lease_token=lease_token)
            client = self._require_client()
            packet = encode_raw_command(command, sequence=DEFAULT_SEQUENCE)
            tx_id = uuid.uuid4().hex
            start_cursor = self._next_cursor - 1
            loop = asyncio.get_running_loop()
            pending: asyncio.Future[bytes] = loop.create_future()
            self._pending_ack = pending
            self._pending_sequence = DEFAULT_SEQUENCE
            self._emit(
                "TX_REQUEST",
                tx_id=tx_id,
                source=source,
                cmd=command.cmd_hex,
                key=command.key_hex,
                data_hex=command.data_hex,
                packet_hex=format_hex(packet),
                byte_count=len(packet),
                write_with_response=True,
            )
            try:
                async with self._gatt_write_lock:
                    await client.write_gatt_char(
                        self._write_characteristic,
                        packet,
                        response=True,
                    )
                ack = await asyncio.wait_for(pending, timeout=self.ack_timeout)
            except asyncio.TimeoutError as exc:
                self._set_error(
                    "BLE_ACK_TIMEOUT",
                    f"no L1 ACK within {self.ack_timeout:g}s for tx {tx_id}",
                )
                raise Watch579AckTimeout(
                    f"no L1 ACK within {self.ack_timeout:g}s"
                ) from exc
            except Watch579BleError as exc:
                self._set_error(exc.reason_code, str(exc))
                raise
            except Watch579ProtocolError as exc:
                error = Watch579BleError(
                    str(exc), reason_code="BLE_RX_CRC_OR_LENGTH_ERROR"
                )
                self._set_error(error.reason_code, str(error))
                raise error from exc
            except Exception as exc:
                if not getattr(client, "is_connected", False):
                    error = Watch579Disconnected(
                        f"watch disconnected during write: {exc}"
                    )
                else:
                    error = Watch579BleError(
                        f"BLE write failed: {exc}", reason_code="BLE_WRITE_FAILED"
                    )
                self._set_error(error.reason_code, str(error))
                raise error from exc
            finally:
                if self._pending_ack is pending:
                    self._pending_ack = None
                    self._pending_sequence = None
                if not pending.done():
                    pending.cancel()
            notifications = [
                dict(event)
                for event in self._events
                if event["cursor"] > start_cursor
                and event["kind"]
                in {"RX_NOTIFY", "RX_TRANSPORT_ACK", "RX_DATA", "TX_HOST_ACK"}
            ]
            self._last_error = None
            return {
                "tx_id": tx_id,
                "cmd": command.cmd_hex,
                "key": command.key_hex,
                "data_hex": command.data_hex,
                "packet_hex": format_hex(packet),
                "write_with_response": True,
                "transport_acked": True,
                "ack_hex": format_hex(ack),
                "effect_verified": False,
                "notifications": notifications,
            }

    def _authorize_write(self, *, source: str, lease_token: str | None) -> None:
        if source == "page":
            if self._lease_token is not None:
                raise Watch579TargetBusy(
                    f"579 target is owned by automation task {self._lease_owner or ''}".strip()
                )
            return
        if (
            source != "automation"
            or not lease_token
            or not secrets.compare_digest(lease_token, self._lease_token or "")
        ):
            raise Watch579TargetBusy("automation lease token is missing or no longer current")

    def acquire_lease(self, *, owner: object) -> str:
        clean_owner = str(owner or "").strip()
        if not clean_owner:
            raise ValueError("lease owner is required")
        return self._call(self._acquire_lease(clean_owner))

    async def _acquire_lease(self, owner: str) -> str:
        if self._lease_token is not None:
            raise Watch579TargetBusy(
                f"579 target is already owned by {self._lease_owner or 'automation'}"
            )
        # Waiting for the command lock lets a manual write already in flight
        # finish before the lease becomes visible.
        async with self._command_lock:
            if self._lease_token is not None:
                raise Watch579TargetBusy(
                    f"579 target is already owned by {self._lease_owner or 'automation'}"
                )
            token = secrets.token_urlsafe(32)
            self._lease_token = token
            self._lease_owner = owner
            self._emit("LEASE_ACQUIRED", owner=owner)
            return token

    def release_lease(self, lease_token: object) -> bool:
        return bool(self._call(self._release_lease(str(lease_token or ""))))

    async def _release_lease(self, token: str) -> bool:
        if not token or not self._lease_token:
            return False
        if not secrets.compare_digest(token, self._lease_token):
            return False
        owner = self._lease_owner
        self._lease_token = None
        self._lease_owner = None
        self._emit("LEASE_RELEASED", owner=owner)
        return True

    def shutdown(self) -> None:
        if not self._thread.is_alive():
            return
        try:
            self._call(self._shutdown_async(), timeout=10)
        finally:
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._thread.join(timeout=10)

    async def _shutdown_async(self) -> None:
        self._closed = True
        self._lease_token = None
        self._lease_owner = None
        await self._disconnect(force=True)


__all__ = [
    "Watch579AckTimeout",
    "Watch579BleBroker",
    "Watch579BleError",
    "Watch579BleUnavailable",
    "Watch579ConnectTimeout",
    "Watch579DeviceNotFound",
    "Watch579Disconnected",
    "Watch579GattProfileMismatch",
    "Watch579InvalidRawCommand",
    "Watch579TargetBusy",
]
