"""Compose the hardware serial bridge and a screenshot source into one session."""

from __future__ import annotations

import math
import os
import shutil
import time
from collections.abc import Callable, Iterable, Mapping
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
from agent_loop_system.tools.mtp_screenshot import (
    MtpCaptureProvider,
    MtpSystem,
    WindowsMtpSystem,
)
from agent_loop_system.tools.watch_ble_provider import (
    DEFAULT_BLE_SCAN_TIMEOUT,
    BleCaptureProvider,
)


_GUI_COMMANDS = frozenset({"GUI_PING", "GUI_STATE", "GUI_TREE"})
_MAX_HANDSHAKE_SEQ = 2_147_483_647
_TEST_SESSION_START = ":TEST_SESSION:START"
_TEST_SESSION_STATUS = ":TEST_SESSION:STATUS"
_SYSTEM_REBOOT = ":SYSTEM_REBOOT:"
_CLEAR_BOOT_POPUP = ":BUTTON_PRESS:1,1,0"
_ENTER_DIAL = ":ENTER_PAGE:DIAL,0"
_OPEN_MAIN_MENU = _CLEAR_BOOT_POPUP
_CYCLE_MENU_STYLE = ":BUTTON_PRESS:1,3,0"
_MENU_STYLE_CHECK_LIMIT = 4
_LIST_MENU_EXPECTATION = (
    "当前截图显示手表主菜单的列表风格：应用以单列纵向列表排列，"
    "每行一个应用图标并带对应的应用名称；不得是蜂窝、瀑布或星环图标布局。"
)
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
    environment: Mapping[str, str] | None = None,
    allow_dangerous_commands: bool = False,
) -> HardwareSerialSession:
    settings = os.environ if environment is None else environment
    port = settings.get("W30_HARDWARE_PORT", DEFAULT_PORT).strip() or DEFAULT_PORT
    baudrate_text = settings.get(
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

    transport_name = settings.get(
        "W30_HARDWARE_TRANSPORT", "serial"
    ).strip().lower()
    if transport_name not in {"serial", "supercom"}:
        raise ValueError("W30_HARDWARE_TRANSPORT must be serial or supercom")
    transport = SuperComPipeTransport(port) if transport_name == "supercom" else None
    serial_kwargs: dict[str, object] = {
        "port": port,
        "baudrate": baudrate,
        "log_dir": Path(evidence_dir) / "serial",
        "cmd_timeout": cmd_timeout,
        "transport": transport,
        "dtr": False,
        "rts": False,
    }
    if allow_dangerous_commands:
        serial_kwargs["allow_dangerous_commands"] = True
    return HardwareSerialSession(
        **serial_kwargs,
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
        BleCaptureProvider(
            address,
            serial_session=serial_session,
            scan_timeout=scan_timeout,
        ),
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


@dataclass(frozen=True, slots=True)
class HardwareCaseResetResult:
    """Observable gates passed before one hardware case may start."""

    status: TestSessionStatus
    reboot_status: str
    gui_ping_attempts: int
    bootstrap_event_seen: bool
    current_page: str
    popup: object | None
    state_raw: dict[str, object]


class HardwareCaseResetError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


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
    """Read the current firmware test-session state without renewing it."""

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


def _activate_test_session(
    session: Any,
    *,
    startup_timeout: float,
    command_timeout: float,
    ready_event_start_index: int | None = None,
) -> TestSessionBootstrapResult:
    """Wait for the GUI after boot and activate the firmware test lease."""

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

    start_event_index = (
        session.event_count
        if ready_event_start_index is None
        else ready_event_start_index
    )
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
    return TestSessionBootstrapResult(
        status=status,
        start_sent=True,
        gui_ping_attempts=gui_ping_attempts,
        bootstrap_event_seen=bootstrap_event_seen,
    )


def bootstrap_test_session(
    *,
    evidence_dir: str | os.PathLike[str],
    startup_timeout: float = 180.0,
    command_timeout: float = 8.0,
    serial_session: Any | None = None,
) -> TestSessionBootstrapResult:
    """Establish the runner-controlled batch test session exactly once.

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
    try:
        session.start()
        bootstrap_result = _activate_test_session(
            session,
            startup_timeout=startup_timeout,
            command_timeout=command_timeout,
        )
    except BaseException as exc:
        try:
            session.stop()
        except BaseException as cleanup_exc:
            exc.add_note(f"test-session bootstrap cleanup also failed: {cleanup_exc!r}")
        raise
    session.stop()
    return bootstrap_result


def _require_accepted(result: HardwareCommandResult, command: str) -> None:
    if str(result.status).lower() != "accepted":
        reason = str(result.raw.get("reason", result.status or "not_accepted"))
        raise HardwareCaseResetError(
            "RESET_COMMAND_REJECTED",
            f"hardware case reset command failed: {command} -> {reason}",
        )


def _window_name(value: object) -> str:
    if isinstance(value, Mapping):
        return str(value.get("name") or "")
    return ""


def _wait_for_reset_gui(
    session: Any,
    *,
    command_timeout: float,
    context: str,
) -> None:
    sequence = _positive_handshake_sequence()
    barrier = session.send(
        f":GUI_PING:{sequence}",
        request="gui_ping",
        seq=sequence,
        timeout=command_timeout,
        expected_type="gui_ack",
        expected_status="processed",
    )
    if str(barrier.status).lower() != "processed":
        raise HardwareCaseResetError(
            "RESET_GUI_BARRIER_FAILED",
            f"GUI_PING did not complete {context}: {barrier.status}",
        )


def _judge_list_menu_style(screenshot_path: Path) -> tuple[str, str]:
    from agent_loop_system.tools.test import judge_test_with_vision

    verdict = judge_test_with_vision(
        _LIST_MENU_EXPECTATION,
        [{"path": str(screenshot_path), "label": "主菜单风格检查"}],
        ["确认当前主菜单为单列、图标加应用名称的列表风格"],
    )
    return str(verdict.verdict).upper(), str(verdict.reason)


def _restore_list_menu_style(
    session: Any,
    *,
    evidence_dir: Path,
    command_timeout: float,
    capture_timeout: float,
    usb_timeout: float,
    mtp_system: MtpSystem,
    capture_provider: CaptureProvider | None,
    menu_style_judge: Callable[[Path], tuple[str, str]],
) -> None:
    opened = session.send(
        _OPEN_MAIN_MENU,
        request="button_press",
        timeout=command_timeout,
        expected_type="command_result",
        expected_status="accepted",
    )
    _require_accepted(opened, _OPEN_MAIN_MENU)
    _wait_for_reset_gui(
        session,
        command_timeout=command_timeout,
        context="after opening the main menu",
    )

    provider = capture_provider or MtpCaptureProvider(
        session,
        usb_timeout=usb_timeout,
        mtp_system=mtp_system,
    )
    try:
        for check_index in range(1, _MENU_STYLE_CHECK_LIMIT + 1):
            screenshot_path = evidence_dir / f"menu-style-check-{check_index:02d}.bmp"
            try:
                frame = provider.capture(timeout=capture_timeout)
                frame.save_bmp(screenshot_path)
            except Exception as exc:
                raise HardwareCaseResetError(
                    "MENU_STYLE_CAPTURE_FAILED",
                    f"failed to capture menu-style check {check_index}: {exc}",
                ) from exc

            try:
                verdict, reason = menu_style_judge(screenshot_path)
            except Exception as exc:
                raise HardwareCaseResetError(
                    "MENU_STYLE_JUDGMENT_FAILED",
                    f"menu-style screenshot judgment failed at check {check_index}: {exc}",
                ) from exc
            verdict = str(verdict or "").strip().upper()
            reason = str(reason or "")
            if verdict == "PASS":
                break
            if verdict != "FAIL":
                raise HardwareCaseResetError(
                    "MENU_STYLE_UNVERIFIED",
                    "menu-style screenshot could not prove List style "
                    f"at check {check_index} (verdict={verdict or 'unknown'}, "
                    f"reason={reason or 'none'})",
                )
            if check_index == _MENU_STYLE_CHECK_LIMIT:
                raise HardwareCaseResetError(
                    "MENU_STYLE_NOT_LIST",
                    "main menu did not reach List style after checking all four "
                    f"configured styles (last_reason={reason or 'none'})",
                )

            switched = session.send(
                _CYCLE_MENU_STYLE,
                request="button_press",
                timeout=command_timeout,
                expected_type="command_result",
                expected_status="accepted",
            )
            _require_accepted(switched, _CYCLE_MENU_STYLE)
            _wait_for_reset_gui(
                session,
                command_timeout=command_timeout,
                context=f"after menu-style switch {check_index}",
            )
    finally:
        provider.close()

    dial = session.send(
        _ENTER_DIAL,
        request="enter_page",
        timeout=command_timeout,
        expected_type="command_result",
        expected_status="accepted",
    )
    _require_accepted(dial, _ENTER_DIAL)
    _wait_for_reset_gui(
        session,
        command_timeout=command_timeout,
        context="after restoring DIAL from the main menu",
    )


def reset_hardware_case_state(
    *,
    evidence_dir: str | os.PathLike[str],
    startup_timeout: float = 180.0,
    command_timeout: float = 8.0,
    usb_timeout: float = 30.0,
    capture_timeout: float = _DEFAULT_CAPTURE_TIMEOUT,
    serial_session: Any | None = None,
    mtp_system: MtpSystem | None = None,
    capture_provider: CaptureProvider | None = None,
    menu_style_judge: Callable[[Path], tuple[str, str]] | None = None,
    environment: Mapping[str, str] | None = None,
) -> HardwareCaseResetResult:
    """Reboot, restore List menu style, and prove DIAL before one hardware case."""

    if startup_timeout <= 0:
        raise ValueError("startup_timeout must be positive")
    if command_timeout <= 0:
        raise ValueError("command_timeout must be positive")
    if usb_timeout <= 0:
        raise ValueError("usb_timeout must be positive")
    if capture_timeout <= 0:
        raise ValueError("capture_timeout must be positive")
    settings = os.environ if environment is None else environment
    if serial_session is None and settings.get(
        "W30_HARDWARE_TRANSPORT", ""
    ).strip().lower() != "supercom":
        raise ValueError(
            "hardware case reset requires W30_HARDWARE_TRANSPORT=supercom"
        )

    session = serial_session or _create_hardware_serial_session(
        evidence_dir=evidence_dir,
        cmd_timeout=command_timeout,
        environment=settings,
        allow_dangerous_commands=True,
    )
    system = mtp_system or WindowsMtpSystem()
    try:
        session.start()
        ready_event_start_index = session.event_count
        reboot = session.send(
            _SYSTEM_REBOOT,
            request="system_reboot",
            timeout=command_timeout,
            expected_type="command_result",
            expected_status="accepted",
        )
        _require_accepted(reboot, _SYSTEM_REBOOT)
        try:
            system.wait_for_usb(present=False, timeout=usb_timeout)
        except Exception as exc:
            raise HardwareCaseResetError(
                "REBOOT_NOT_OBSERVED",
                "hardware reboot was accepted but USB did not disappear",
            ) from exc

        bootstrap = _activate_test_session(
            session,
            startup_timeout=startup_timeout,
            command_timeout=command_timeout,
            ready_event_start_index=ready_event_start_index,
        )

        session.write_shell_line("dal_usb open")
        try:
            system.wait_for_usb(present=True, timeout=usb_timeout)
        except Exception as exc:
            raise HardwareCaseResetError(
                "USB_RESTORE_FAILED",
                "dal_usb open did not restore the watch USB/MTP device",
            ) from exc

        button = session.send(
            _CLEAR_BOOT_POPUP,
            request="button_press",
            timeout=command_timeout,
            expected_type="command_result",
            expected_status="accepted",
        )
        _require_accepted(button, _CLEAR_BOOT_POPUP)
        dial = session.send(
            _ENTER_DIAL,
            request="enter_page",
            timeout=command_timeout,
            expected_type="command_result",
            expected_status="accepted",
        )
        _require_accepted(dial, _ENTER_DIAL)

        _wait_for_reset_gui(
            session,
            command_timeout=command_timeout,
            context="after DIAL reset",
        )

        state_sequence = _positive_handshake_sequence()
        state = session.send(
            f":GUI_STATE:{state_sequence}",
            request="gui_state",
            seq=state_sequence,
            timeout=command_timeout,
            expected_type="gui_state",
            expected_status="ok",
        )
        if str(state.status).lower() != "ok":
            raise HardwareCaseResetError(
                "RESET_STATE_UNAVAILABLE",
                f"GUI_STATE failed after DIAL reset: {state.status}",
            )
        state_raw = dict(state.raw)
        current_page = _window_name(state_raw.get("current_page"))
        popup = state_raw.get("popup")
        if current_page.upper() != "DIAL" or popup is not None:
            raise HardwareCaseResetError(
                "RESET_STATE_MISMATCH",
                "hardware case reset did not reach DIAL with popup=null "
                f"(current_page={current_page or 'unknown'!r}, popup={popup!r})",
            )

        reset_evidence_dir = Path(evidence_dir).resolve()
        reset_evidence_dir.mkdir(parents=True, exist_ok=True)
        _restore_list_menu_style(
            session,
            evidence_dir=reset_evidence_dir,
            command_timeout=command_timeout,
            capture_timeout=capture_timeout,
            usb_timeout=usb_timeout,
            mtp_system=system,
            capture_provider=capture_provider,
            menu_style_judge=menu_style_judge or _judge_list_menu_style,
        )

        state_sequence = _positive_handshake_sequence()
        state = session.send(
            f":GUI_STATE:{state_sequence}",
            request="gui_state",
            seq=state_sequence,
            timeout=command_timeout,
            expected_type="gui_state",
            expected_status="ok",
        )
        if str(state.status).lower() != "ok":
            raise HardwareCaseResetError(
                "RESET_STATE_UNAVAILABLE",
                f"GUI_STATE failed after menu-style reset: {state.status}",
            )
        state_raw = dict(state.raw)
        current_page = _window_name(state_raw.get("current_page"))
        popup = state_raw.get("popup")
        if current_page.upper() != "DIAL" or popup is not None:
            raise HardwareCaseResetError(
                "RESET_STATE_MISMATCH",
                "hardware case reset did not return to DIAL with popup=null "
                f"after menu-style reset (current_page={current_page or 'unknown'!r}, "
                f"popup={popup!r})",
            )
        reset_result = HardwareCaseResetResult(
            status=bootstrap.status,
            reboot_status=str(reboot.status),
            gui_ping_attempts=bootstrap.gui_ping_attempts,
            bootstrap_event_seen=bootstrap.bootstrap_event_seen,
            current_page=current_page,
            popup=popup,
            state_raw=state_raw,
        )
    except BaseException as exc:
        try:
            session.stop()
        except BaseException as cleanup_exc:
            exc.add_note(f"hardware case reset cleanup also failed: {cleanup_exc!r}")
        raise
    session.stop()
    return reset_result


class RealDeviceSession:
    """A ``SimulatorSession``-compatible facade for a physical W30 device.

    The batch controller establishes the 24-hour firmware test session before
    creating individual case sessions. ``start`` does not renew or stop that
    lease; it opens the transport and waits for ``GUI_PING`` to be processed.
    Capture providers allocate a new positive sequence for every requested
    evidence frame, so startup does not take an unused baseline screenshot.
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
            self._frame_sequence = 0
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
            raise RuntimeError("capture sequence is unavailable")

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
    "HardwareCaseResetError",
    "HardwareCaseResetResult",
    "RealDeviceSession",
    "TestSessionBootstrapError",
    "TestSessionBootstrapResult",
    "TestSessionStatus",
    "bootstrap_test_session",
    "query_test_session_status",
    "reset_hardware_case_state",
]
