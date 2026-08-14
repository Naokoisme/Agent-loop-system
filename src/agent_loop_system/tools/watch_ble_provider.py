"""Optional Agent-loop CaptureProvider backed by the watch BLE GATT route.

The provider is deliberately opt-in.  It reuses :mod:`watch_ble` for one
scan/connect/capture transaction per requested frame, which keeps lifecycle
ownership simple for the small number of screenshots used by hardware cases.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import math
import os
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from agent_loop_system.tools.watch_ble import (
    WatchBleClient,
    WatchBleDevice,
    WatchBleError,
    WatchBleScreenshotResult,
    scan_watches,
    select_watch,
)


DEFAULT_BLE_SCAN_TIMEOUT = 15.0
_UINT32_MAX = 0xFFFFFFFF
_CLOSE_TIMEOUT = 5.0


class BleCaptureProviderError(RuntimeError):
    """The optional BLE CaptureProvider could not return a verified frame."""


class BleCaptureProviderTimeoutError(BleCaptureProviderError, TimeoutError):
    """The complete scan/connect/capture operation exceeded its deadline."""


class BleCaptureSequenceError(BleCaptureProviderError, ValueError):
    """No uint32 sequence remains newer than the requested baseline."""


class _ScreenshotClient(Protocol):
    async def connect(self, *, pair: bool = False) -> None: ...

    async def capture_screenshot(
        self,
        output_path: str | Path,
        *,
        sequence: int | None = None,
        timeout: float = 180.0,
    ) -> WatchBleScreenshotResult: ...

    async def close(self) -> None: ...


ClientBuilder = Callable[[WatchBleDevice, float], _ScreenshotClient]


@dataclass(frozen=True, slots=True)
class BleCaptureMetadata:
    """Metadata exposed through the generic Agent-loop CaptureProvider API."""

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
    file_crc32: int
    chunk_bytes: int
    chunks: int
    encoding: str
    file_size: int
    receipt_verified: bool
    ble_address: str
    device_uptime_ms: int | None = None
    capture_duration_ms: int | None = None
    pixel_source: str | None = None


@dataclass(frozen=True, slots=True)
class BleCaptureFrame:
    """One fully verified BMP returned by the BLE screenshot protocol."""

    bmp: bytes
    metadata: BleCaptureMetadata

    def save_bmp(
        self, output_path: str | os.PathLike[str]
    ) -> BleCaptureMetadata:
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(self.bmp)
        return self.metadata


def _positive_timeout(name: str, value: float) -> float:
    result = float(value)
    if not math.isfinite(result) or result <= 0:
        raise ValueError(f"{name} must be positive")
    return result


def _default_client_builder(
    device: WatchBleDevice, timeout: float
) -> _ScreenshotClient:
    return WatchBleClient(device, timeout=timeout)


def _parse_crc32(name: str, value: str) -> int:
    try:
        result = int(value, 16)
    except (TypeError, ValueError) as exc:
        raise BleCaptureProviderError(f"{name} is not a hexadecimal CRC32") from exc
    if not 0 <= result <= _UINT32_MAX:
        raise BleCaptureProviderError(f"{name} is outside uint32 range")
    return result


class BleCaptureProvider:
    """Capture fresh watch BMPs through FF02 writes and FF03 notifications.

    Each capture owns a short BLE connection and closes it before returning.
    This avoids sharing an asyncio/Bleak lifecycle with the synchronous hardware
    Runner and is sufficient for the deliberately low screenshot volume.
    """

    def __init__(
        self,
        address: str,
        *,
        sequence_start: int | None = None,
        scan_timeout: float = DEFAULT_BLE_SCAN_TIMEOUT,
        scanner: object | None = None,
        client_builder: ClientBuilder | None = None,
    ) -> None:
        address_value = str(address or "").strip()
        if not address_value:
            raise ValueError("address is required")
        if sequence_start is None:
            sequence_start = time.time_ns() % 2_147_483_646 + 1
        if (
            isinstance(sequence_start, bool)
            or not isinstance(sequence_start, int)
            or not 0 <= sequence_start <= _UINT32_MAX
        ):
            raise ValueError("sequence_start must be a uint32")

        self.address = address_value
        self.scan_timeout = _positive_timeout("scan_timeout", scan_timeout)
        self.scanner = scanner
        self.client_builder = client_builder or _default_client_builder
        self._next_sequence = sequence_start
        self._last_sequence: int | None = None
        self._closed = False
        self._lock = threading.Lock()
        self._executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="watch-ble-capture",
        )

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
            raise BleCaptureSequenceError(
                "cannot allocate a BLE capture sequence newer than after_sequence"
            )
        self._next_sequence = sequence + 1
        return sequence

    async def _capture_once(
        self,
        output_path: Path,
        *,
        sequence: int,
        timeout: float,
    ) -> tuple[WatchBleScreenshotResult, WatchBleDevice]:
        client: _ScreenshotClient | None = None

        async def operation() -> tuple[WatchBleScreenshotResult, WatchBleDevice]:
            nonlocal client
            devices = await scan_watches(
                timeout=min(self.scan_timeout, timeout),
                scanner=self.scanner,
            )
            device = select_watch(devices, address=self.address)
            client = self.client_builder(device, timeout)
            await client.connect(pair=False)
            result = await client.capture_screenshot(
                output_path,
                sequence=sequence,
                timeout=timeout,
            )
            return result, device

        try:
            return await asyncio.wait_for(operation(), timeout=timeout)
        except WatchBleError:
            raise
        except TimeoutError as exc:
            raise BleCaptureProviderTimeoutError(
                f"BLE screenshot sequence={sequence} exceeded {timeout:g}s"
            ) from exc
        finally:
            if client is not None:
                await asyncio.wait_for(client.close(), timeout=_CLOSE_TIMEOUT)

    def capture(
        self,
        *,
        timeout: float,
        after_sequence: int | None = None,
    ) -> BleCaptureFrame:
        timeout_value = _positive_timeout("timeout", timeout)
        with self._lock:
            if self._closed:
                raise BleCaptureProviderError("BLE capture provider is closed")
            sequence = self._allocate_sequence(after_sequence)
            started = time.monotonic()
            with tempfile.TemporaryDirectory(prefix="watch-ble-provider-") as root:
                output = Path(root) / f"agent_capture_{sequence}.bmp"

                def run_capture() -> tuple[WatchBleScreenshotResult, WatchBleDevice]:
                    return asyncio.run(
                        self._capture_once(
                            output,
                            sequence=sequence,
                            timeout=timeout_value,
                        )
                    )

                future = self._executor.submit(run_capture)
                try:
                    result, device = future.result(
                        timeout=timeout_value + _CLOSE_TIMEOUT + 1.0
                    )
                except concurrent.futures.TimeoutError as exc:
                    future.cancel()
                    raise BleCaptureProviderTimeoutError(
                        f"BLE screenshot sequence={sequence} did not stop after timeout"
                    ) from exc
                bmp = output.read_bytes()

            if result.sequence != sequence:
                raise BleCaptureProviderError(
                    f"BLE screenshot returned sequence={result.sequence}, "
                    f"expected {sequence}"
                )
            if len(bmp) != result.file_size:
                raise BleCaptureProviderError(
                    f"BLE screenshot file size is {len(bmp)}, "
                    f"expected {result.file_size}"
                )

            metadata = BleCaptureMetadata(
                sequence=sequence,
                timestamp=time.monotonic(),
                width=result.width,
                height=result.height,
                pixel_format="bgr888",
                data_size=result.width * result.height * 3,
                stride=result.width * 3,
                source="watch_display",
                transport="ble_gatt",
                payload_crc32=_parse_crc32(
                    "payload_crc32", result.payload_crc32
                ),
                file_crc32=_parse_crc32("crc32", result.crc32),
                chunk_bytes=result.chunk_size,
                chunks=result.chunks,
                encoding="bmp",
                file_size=result.file_size,
                receipt_verified=True,
                ble_address=device.address,
                capture_duration_ms=round((time.monotonic() - started) * 1000),
                pixel_source="vde_lcd_composite",
            )
            self._last_sequence = sequence
            return BleCaptureFrame(bmp=bmp, metadata=metadata)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._executor.shutdown(wait=True, cancel_futures=True)


__all__ = [
    "BleCaptureFrame",
    "BleCaptureMetadata",
    "BleCaptureProvider",
    "BleCaptureProviderError",
    "BleCaptureProviderTimeoutError",
    "BleCaptureSequenceError",
    "DEFAULT_BLE_SCAN_TIMEOUT",
]
