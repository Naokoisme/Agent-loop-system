"""Compose the hardware serial bridge and a screenshot source into one session."""

from __future__ import annotations

import math
import os
import shutil
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from agent_loop_system.tools.command_protocol import normalize_command
from agent_loop_system.tools.hardware_serial import (
    DEFAULT_BAUDRATE,
    DEFAULT_PORT,
    HardwareCommandResult,
    HardwareSerialSession,
    HardwareSerialTimeoutError,
    SuperComPipeTransport,
)
from agent_loop_system.tools.mtp_screenshot import MtpCaptureProvider
from agent_loop_system.tools.watch_ble_provider import (
    DEFAULT_BLE_SCAN_TIMEOUT,
    BleCaptureProvider,
)


_GUI_COMMANDS = frozenset({"GUI_PING", "GUI_STATE", "GUI_TREE"})
_MAX_HANDSHAKE_SEQ = 2_147_483_647
_TEST_SESSION_START = ":TEST_SESSION:START"
_TEST_SESSION_STATUS = ":TEST_SESSION:STATUS"
_DEFAULT_CAPTURE_TIMEOUT = 12.0
_DEFAULT_BLE_CAPTURE_TIMEOUT = 180.0
_CAPTURE_PROVIDER_ENV = "W30_HARDWARE_CAPTURE_PROVIDER"
_BLE_ADDRESS_ENV = "W30_HARDWARE_BLE_ADDRESS"
_BLE_SCAN_TIMEOUT_ENV = "W30_HARDWARE_BLE_SCAN_TIMEOUT"


@runtime_checkable
class CaptureFrame(Protocol):
    """One capture result supplied by any screenshot source."""

    metadata: Any

    def save_bmp(self, output_path: str | os.PathLike[str]) -> Any: ...


@runtime_checkable
class CaptureProvider(Protocol):
    """Screenshot source used by :class:`RealDeviceSession`.

    Providers own freshness semantics through a monotonically increasing
    ``metadata.sequence``.
    """

    def capture(
        self,
        *,
        timeout: float,
        after_sequence: int | None = None,
    ) -> CaptureFrame: ...

    def close(self) -> None: ...


def _positive_handshake_sequence() -> int:
    """Return a changing positive sequence that remains safe for signed C ints."""

    return time.time_ns() % _MAX_HANDSHAKE_SEQ or 1


def _pixel_format_name(value: object) -> str:
    name = getattr(value, "name", None)
    return str(name if name is not None else value)


def _create_hardware_serial_session(
    *,
    evidence_dir: str | os.PathLike[str],
    cmd_timeout: float,
) -> HardwareSerialSession:
    port = os.environ.get("W30_HARDWARE_PORT", DEFAULT_PORT).strip() or DEFAULT_PORT
    baudrate_text = os.environ.get(
        "W30_HARDWARE_BAUDRATE", str(DEFAULT_BAUDRATE)
    ).strip()
    try:
        baudrate = int(baudrate_text, 10)
    except ValueError as exc:
        raise ValueError(
            "W30_HARDWARE_BAUDRATE must be a positive integer"
        ) from exc
    if baudrate <= 0:
        raise ValueError("W30_HARDWARE_BAUDRATE must be a positive integer")

    transport_name = os.environ.get(
        "W30_HARDWARE_TRANSPORT", "serial"
    ).strip().lower()
    if transport_name not in {"serial", "supercom"}:
        raise ValueError("W30_HARDWARE_TRANSPORT must be serial or supercom")
    transport = SuperComPipeTransport(port) if transport_name == "supercom" else None
    return HardwareSerialSession(
        port=port,
        baudrate=baudrate,
        log_dir=Path(evidence_dir) / "serial",
        cmd_timeout=cmd_timeout,
        transport=transport,
        dtr=False,
        rts=False,
    )


def _create_capture_provider(serial_session: Any) -> tuple[CaptureProvider, str]:
    provider_name = os.environ.get(_CAPTURE_PROVIDER_ENV, "mtp").strip().lower()
    if provider_name == "mtp":
        return MtpCaptureProvider(serial_session), provider_name
    if provider_name != "ble":
        raise ValueError(
            f"{_CAPTURE_PROVIDER_ENV} must be mtp or ble"
        )

    address = os.environ.get(_BLE_ADDRESS_ENV, "").strip()
    if not address:
        raise ValueError(
            f"{_BLE_ADDRESS_ENV} is required when {_CAPTURE_PROVIDER_ENV}=ble"
        )
    scan_timeout_text = os.environ.get(
        _BLE_SCAN_TIMEOUT_ENV, str(DEFAULT_BLE_SCAN_TIMEOUT)
    ).strip()
    try:
        scan_timeout = float(scan_timeout_text)
    except ValueError as exc:
        raise ValueError(f"{_BLE_SCAN_TIMEOUT_ENV} must be positive") from exc
    if not math.isfinite(scan_timeout) or scan_timeout <= 0:
        raise ValueError(f"{_BLE_SCAN_TIMEOUT_ENV} must be positive")
    return (
        BleCaptureProvider(address, scan_timeout=scan_timeout),
        provider_name,
    )


@dataclass(frozen=True, slots=True)
class TestSessionStatus:
    active: bool
    lease_seconds: int
    raw: dict[str, object]


@dataclass(frozen=True, slots=True)
class TestSessionBootstrapResult:
    status: TestSessionStatus
    start_sent: bool
    gui_ping_attempts: int
    bootstrap_event_seen: bool


class TestSessionBootstrapError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


def _parse_test_session_status(raw: dict[str, object]) -> TestSessionStatus:
    try:
        lease_seconds = int(raw.get("lease_seconds", 0))
    except (TypeError, ValueError) as exc:
        raise RuntimeError("TEST_SESSION returned an invalid lease_seconds") from exc
    status = str(raw.get("status", "")).lower()
    return TestSessionStatus(
        active=status == "active" and lease_seconds > 0,
        lease_seconds=max(lease_seconds, 0),
        raw=dict(raw),
    )


def query_test_session_status(
    *,
    evidence_dir: str | os.PathLike[str],
    timeout: float = 8.0,
    serial_session: Any | None = None,
) -> TestSessionStatus:
    """Read the externally managed firmware test-session state without renewing it."""

    if timeout <= 0:
        raise ValueError("timeout must be positive")
    session = serial_session or _create_hardware_serial_session(
        evidence_dir=evidence_dir,
        cmd_timeout=timeout,
    )
    try:
        session.start()
        result = session.send(
            _TEST_SESSION_STATUS,
            request="test_session",
            timeout=timeout,
            expected_type="test_session",
            expected_status=("active", "inactive"),
        )
    except BaseException as exc:
        try:
            session.stop()
        except BaseException as cleanup_exc:
            exc.add_note(f"test-session status cleanup also failed: {cleanup_exc!r}")
        raise
    session.stop()

    return _parse_test_session_status(dict(result.raw))


def bootstrap_test_session(
    *,
    evidence_dir: str | os.PathLike[str],
    startup_timeout: float = 180.0,
    command_timeout: float = 8.0,
    serial_session: Any | None = None,
) -> TestSessionBootstrapResult:
    """Establish the externally owned batch test session exactly once.

    This is deliberately separate from :class:`RealDeviceSession`.  It first
    proves that the GUI command subscriber is alive, then sends one idempotent
    ``TEST_SESSION:START`` and closes only its host-side transport.
    """

    if startup_timeout <= 0:
        raise ValueError("startup_timeout must be positive")
    if command_timeout <= 0:
        raise ValueError("command_timeout must be positive")
    if serial_session is None and os.environ.get(
        "W30_HARDWARE_TRANSPORT", ""
    ).strip().lower() != "supercom":
        raise ValueError(
            "hardware bootstrap requires W30_HARDWARE_TRANSPORT=supercom"
        )

    session = serial_session or _create_hardware_serial_session(
        evidence_dir=evidence_dir,
        cmd_timeout=command_timeout,
    )
    deadline = time.monotonic() + startup_timeout
    gui_ping_attempts = 0
    bootstrap_event_seen = False

    def remaining_timeout() -> float:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TestSessionBootstrapError(
                "BLOCKED_LOW_POWER_WAKE",
                "hardware bootstrap grace expired before the GUI became ready",
            )
        return min(command_timeout, remaining)

    def ping_gui() -> None:
        nonlocal gui_ping_attempts
        gui_ping_attempts += 1
        seq = _positive_handshake_sequence()
        result = session.send(
            f":GUI_PING:{seq}",
            request="gui_ping",
            seq=seq,
            timeout=remaining_timeout(),
            expected_type="gui_ack",
            expected_status="processed",
        )
        if str(result.status).lower() != "processed":
            reason = str(result.raw.get("reason", result.status or "not_processed"))
            raise TestSessionBootstrapError(
                "BOOTSTRAP_GUI_NOT_READY",
                f"GUI_PING did not reach the GUI thread: {reason}",
            )

    try:
        session.start()
        start_event_index = session.event_count
        try:
            ping_gui()
        except HardwareSerialTimeoutError:
            wait_timeout = deadline - time.monotonic()
            if wait_timeout <= 0:
                raise TestSessionBootstrapError(
                    "BLOCKED_LOW_POWER_WAKE",
                    "GUI_PING timed out before the hardware bootstrap was ready",
                ) from None
            try:
                event = session.wait_for_event(
                    request="test_bootstrap",
                    event_type="test_bootstrap",
                    status=("ready", "error", "expired"),
                    timeout=wait_timeout,
                    start_event_index=start_event_index,
                )
            except HardwareSerialTimeoutError as exc:
                raise TestSessionBootstrapError(
                    "BLOCKED_LOW_POWER_WAKE",
                    "no test_bootstrap ready event arrived before the startup timeout",
                ) from exc
            bootstrap_event_seen = True
            if str(event.get("status", "")).lower() != "ready":
                reason = str(event.get("reason", "bootstrap_not_ready"))
                raise TestSessionBootstrapError(
                    "BLOCKED_LOW_POWER_WAKE",
                    f"firmware bootstrap is unavailable: {reason}",
                )
            try:
                ping_gui()
            except HardwareSerialTimeoutError as exc:
                raise TestSessionBootstrapError(
                    "BLOCKED_LOW_POWER_WAKE",
                    "GUI_PING did not complete during the hardware bootstrap grace",
                ) from exc

        try:
            result = session.send(
                _TEST_SESSION_START,
                request="test_session",
                timeout=remaining_timeout(),
                expected_type="test_session",
                expected_status="active",
            )
        except HardwareSerialTimeoutError as exc:
            raise TestSessionBootstrapError(
                "BOOTSTRAP_START_TIMEOUT",
                "TEST_SESSION:START did not return an active session",
            ) from exc
        status = _parse_test_session_status(dict(result.raw))
        if not status.active:
            reason = str(result.raw.get("reason", result.status or "not_active"))
            raise TestSessionBootstrapError(
                "BOOTSTRAP_START_FAILED",
                f"TEST_SESSION:START did not activate the session: {reason}",
            )
        bootstrap_result = TestSessionBootstrapResult(
            status=status,
            start_sent=True,
            gui_ping_attempts=gui_ping_attempts,
            bootstrap_event_seen=bootstrap_event_seen,
        )
    except BaseException as exc:
        try:
            session.stop()
        except BaseException as cleanup_exc:
            exc.add_note(f"test-session bootstrap cleanup also failed: {cleanup_exc!r}")
        raise
    session.stop()
    return bootstrap_result


class RealDeviceSession:
    """A ``SimulatorSession``-compatible facade for a physical W30 device.

    The 24-hour firmware test session is an external batch precondition.
    ``start`` never starts, renews, or stops that lease.  It only opens the
    transport, waits for ``GUI_PING`` to be processed, then samples one capture
    frame as the baseline.  The baseline is not written as evidence; every later
    screenshot must use a strictly newer provider frame.
    """

    def __init__(
        self,
        *,
        evidence_dir: str | os.PathLike[str] = "evidence/real_device",
        serial_session: Any | None = None,
        capture_provider: CaptureProvider | None = None,
        startup_timeout: float = 8.0,
        cmd_timeout: float = 8.0,
        capture_timeout: float | None = None,
    ) -> None:
        if startup_timeout <= 0:
            raise ValueError("startup_timeout must be positive")
        if cmd_timeout <= 0:
            raise ValueError("cmd_timeout must be positive")
        if capture_timeout is not None and capture_timeout <= 0:
            raise ValueError("capture_timeout must be positive")
        self.evidence_dir = Path(evidence_dir)
        self.startup_timeout = startup_timeout
        self.cmd_timeout = cmd_timeout

        if serial_session is None:
            serial_session = _create_hardware_serial_session(
                evidence_dir=self.evidence_dir,
                cmd_timeout=cmd_timeout,
            )

        self.serial_session = serial_session
        if capture_provider is None:
            self.capture_provider, self.capture_provider_name = (
                _create_capture_provider(self.serial_session)
            )
        else:
            self.capture_provider = capture_provider
            self.capture_provider_name = "custom"

        if capture_timeout is None:
            capture_timeout = (
                _DEFAULT_BLE_CAPTURE_TIMEOUT
                if self.capture_provider_name == "ble"
                else _DEFAULT_CAPTURE_TIMEOUT
            )
        self.capture_timeout = capture_timeout

        self.last_capture_metadata: dict[str, object] = {}
        self._frame_sequence: int | None = None
        self._started = False
        self._closed = False

    @property
    def started(self) -> bool:
        return self._started

    def start(self) -> None:
        if self._started:
            return
        if self._closed:
            raise RuntimeError("real device session is already stopped")

        sequence = _positive_handshake_sequence()
        try:
            self.serial_session.start()
            handshake = self.serial_session.send(
                f":GUI_PING:{sequence}",
                request="gui_ping",
                timeout=self.startup_timeout,
                expected_type="gui_ack",
                expected_status="processed",
            )
            handshake_type = str(handshake.raw.get("type", "")).lower()
            handshake_status = str(handshake.status).lower()
            if handshake_type != "gui_ack" or handshake_status != "processed":
                raise RuntimeError(
                    "GUI_PING handshake did not reach gui_ack/processed "
                    f"(type={handshake_type!r}, status={handshake_status!r})"
                )
            baseline = self.capture_provider.capture(timeout=self.capture_timeout)
            self._frame_sequence = int(baseline.metadata.sequence)
            self._started = True
        except BaseException as exc:
            try:
                self.stop()
            except BaseException as cleanup_exc:
                exc.add_note(f"real device cleanup also failed: {cleanup_exc!r}")
            raise

    def send(
        self,
        content: str,
        *,
        request: str | None = None,
        timeout: float | None = None,
        expected_type: str | Iterable[str] | None = None,
        expected_status: str | Iterable[str] | None = None,
    ) -> HardwareCommandResult:
        """Send one command and wait for the appropriate serial acknowledgement.

        The serial layer already knows the terminal events for GUI_PING,
        GUI_STATE and GUI_TREE.  For every other command, an otherwise
        unspecified expectation is made explicit as command_result/accepted;
        the reproduction loop follows it with GUI_PING as its completion
        barrier.
        """

        if not self._started:
            raise RuntimeError("real device session not started")

        normalized = normalize_command(content)
        command_name = normalized[1:].partition(":")[0]
        if (
            command_name not in _GUI_COMMANDS
            and expected_type is None
            and expected_status is None
        ):
            expected_type = "command_result"
            expected_status = "accepted"

        return self.serial_session.send(
            content,
            request=request,
            timeout=timeout,
            expected_type=expected_type,
            expected_status=expected_status,
        )

    def lines_since(self, start_index: int) -> list[str]:
        return self.serial_session.lines_since(start_index)

    @staticmethod
    def _capture_sidecar_path(evidence_path: Path) -> Path:
        return evidence_path.with_name(
            f"{evidence_path.stem}_capture_original.bmp"
        )

    def capture_screenshot(self, output_path: str | os.PathLike[str]) -> bool:
        """Save a new full provider frame as evidence and an original sidecar.

        There is deliberately no crop option in the first version: the
        requested evidence BMP and ``*_capture_original.bmp`` are byte-for-byte
        copies of the same uncropped provider frame.
        """

        if not self._started:
            raise RuntimeError("real device session not started")
        if self._frame_sequence is None:
            raise RuntimeError("capture baseline is unavailable")

        evidence_path = Path(output_path).resolve()
        raw_path = self._capture_sidecar_path(evidence_path)
        frame = self.capture_provider.capture(
            timeout=self.capture_timeout,
            after_sequence=self._frame_sequence,
        )
        metadata = frame.metadata
        self._frame_sequence = int(metadata.sequence)

        frame.save_bmp(raw_path)
        evidence_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(raw_path, evidence_path)

        pixel_format = _pixel_format_name(metadata.pixel_format)
        width = int(metadata.width)
        height = int(metadata.height)
        capture_metadata: dict[str, object] = {
            "sequence": int(metadata.sequence),
            "timestamp": float(metadata.timestamp),
            "width": width,
            "height": height,
            "size": [width, height],
            "pixel_format": pixel_format,
            "format": pixel_format,
            "raw_path": str(raw_path),
            "evidence_path": str(evidence_path),
        }
        missing = object()
        integer_fields = (
            "data_size",
            "stride",
            "file_size",
            "payload_crc32",
            "file_crc32",
            "chunk_bytes",
            "chunks",
            "capture_duration_ms",
            "device_uptime_ms",
        )
        text_fields = (
            "source",
            "transport",
            "encoding",
            "pixel_source",
            "mtp_file_name",
            "ble_address",
        )
        for field in integer_fields:
            value = getattr(metadata, field, missing)
            if value is not missing:
                capture_metadata[field] = None if value is None else int(value)
        for field in text_fields:
            value = getattr(metadata, field, missing)
            if value is not missing:
                capture_metadata[field] = None if value is None else str(value)
        receipt_verified = getattr(metadata, "receipt_verified", missing)
        if receipt_verified is not missing:
            capture_metadata["receipt_verified"] = bool(receipt_verified)
        self.last_capture_metadata = capture_metadata
        return True

    def stop(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._started = False

        errors: list[BaseException] = []
        try:
            self.serial_session.stop()
        except BaseException as exc:
            errors.append(exc)
        try:
            self.capture_provider.close()
        except BaseException as exc:
            errors.append(exc)

        if errors:
            first = errors[0]
            for later in errors[1:]:
                first.add_note(f"additional cleanup failure: {later!r}")
            raise first

    def __enter__(self) -> "RealDeviceSession":
        self.start()
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        self.stop()


__all__ = [
    "CaptureFrame",
    "CaptureProvider",
    "RealDeviceSession",
    "TestSessionBootstrapError",
    "TestSessionBootstrapResult",
    "TestSessionStatus",
    "bootstrap_test_session",
    "query_test_session_status",
]
