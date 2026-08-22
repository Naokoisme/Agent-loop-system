"""Windows 真机串口会话。

串口同时承载 Zephyr 日志和 ``w30_test_bridge`` JSON。本模块保留原始字节，
从未清洗的逻辑行提取协议事件，再把去除 ANSI 的文本写入可读日志。通常只有带固定
协议标记的完整单行 JSON 才是事件；唯一例外是 ``command_result`` 的类型名被当前
固件已知的 voice-assistant 诊断打印插断，宿主会在严格白名单内恢复该回执。任意其他
损坏 JSON 都不会被拼接成事件。``accepted`` 只表示命令已进入队列，不表示 GUI 已处理完成。
"""
from __future__ import annotations

import codecs
import ctypes
import json
import os
import re
import threading
import time
from collections import deque
from collections.abc import Callable, Iterable
from ctypes import wintypes
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Protocol, runtime_checkable

from agent_loop_system.tools.command_protocol import normalize_command


DEFAULT_PORT = "COM7"
DEFAULT_BAUDRATE = 1_500_000
DEFAULT_READ_SIZE = 4096
DEFAULT_RX_BUFFER_SIZE = 1024 * 1024
DEFAULT_TX_BUFFER_SIZE = 16 * 1024
DEFAULT_LINE_CACHE_CAPACITY = 16_384
DEFAULT_EVENT_CACHE_CAPACITY = 8_192
DEFAULT_BACKGROUND_ERROR_CACHE_CAPACITY = 4_096
SHELL_WRITE_BURST_LIMIT = 64
BRIDGE_PROTOCOL = "w30_test_bridge"
BRIDGE_PROTOCOL_VERSION = 1
BRIDGE_MARKER = '{"protocol":"w30_test_bridge"'
BRIDGE_COMMAND_RESULT_PREFIX = (
    '{"protocol":"w30_test_bridge","version":1,"type":"command'
)
VOICE_ASSISTANT_INIT_BANNER = (
    "============_gui_comm_voice_assistant_init==============="
)
_INTERLEAVED_COMMAND_RESULT_RE = re.compile(
    r'^\{"protocol":"w30_test_bridge","version":1,'
    r'"type":"command(?P<middle>.*?)","request":"(?P<request>[a-z0-9_]+)",'
    r'"seq":null,"status":"(?P<status>accepted|rejected)"'
    r'(?P<reason>,"reason":"handler_failed")?\}',
    re.DOTALL,
)
_INTERLEAVED_CURRENT_APP_RE = re.compile(
    r"(?:uart:~\$\s*)?current_app\[\d+\]:\s*\d+"
)
_MAX_INTERLEAVED_COMMAND_RESULT_LINES = 16
_MAX_INTERLEAVED_COMMAND_RESULT_TEXT = 4096

# CSI、单字符 ESC 和 OSC（含 BEL/ST 结尾）。在完整逻辑行上执行，因而 ANSI
# 序列即使被底层 read() 分块也能被移除。
ANSI_ESCAPE_RE = re.compile(
    r"(?:\x1b\][^\x07\x1b]*(?:\x07|\x1b\\))|"
    r"(?:\x1b\[[0-?]*[ -/]*[@-~])|"
    r"(?:\x1b[ -/0-?@-Z\\-_])"
)
BACKGROUND_ERROR_RE = re.compile(
    r"(?:hard\s*fault|\bassert(?:ion)?\b|\bfatal(?:\s+error)?\b|"
    r"\bwatchdog\b|\breboot(?:ing|ed)?\b)",
    re.IGNORECASE,
)

_JSON_DECODER = json.JSONDecoder()
_MAX_PENDING_TEXT = 1024 * 1024
_DEFAULT_COMPLETIONS: dict[str, tuple[str, str]] = {
    "gui_ping": ("gui_ack", "processed"),
    "gui_state": ("gui_state", "ok"),
    "gui_tree": ("gui_tree_end", "ok"),
}


class SerialTransportError(RuntimeError):
    """打开或读写串口失败。"""


def write_shell_wire(write: Callable[[bytes], int | None], wire: bytes) -> None:
    """Write one complete shell wire or reject it before touching transport."""

    if len(wire) > SHELL_WRITE_BURST_LIMIT:
        raise SerialTransportError(
            f"shell wire is {len(wire)} bytes; maximum safe burst is "
            f"{SHELL_WRITE_BURST_LIMIT} bytes"
        )
    written = write(wire)
    if written is not None and written != len(wire):
        raise SerialTransportError(
            f"short shell write: {written}/{len(wire)} bytes"
        )


class HardwareSerialTimeoutError(TimeoutError):
    """在截止时间内没有收到对应的真机协议事件。"""


class HardwareSerialCursorExpiredError(IndexError):
    """The requested absolute cursor has fallen behind the in-memory window."""

    def __init__(
        self,
        *,
        stream: str,
        cursor: int,
        oldest_available: int,
        next_index: int,
    ) -> None:
        self.stream = stream
        self.cursor = cursor
        self.oldest_available = oldest_available
        self.next_index = next_index
        super().__init__(
            f"{stream} cursor {cursor} has expired; oldest available index is "
            f"{oldest_available} and next index is {next_index}"
        )


class UnsafeHardwareCommandError(ValueError):
    """命令被真机默认安全策略拒绝。"""


def _validate_cache_capacity(name: str, value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


@runtime_checkable
class ByteTransport(Protocol):
    """可注入的字节传输接口，便于单测以及以后替换串口实现。"""

    def open(self) -> None: ...

    def read(self, size: int) -> bytes: ...

    def write(self, data: bytes) -> int | None: ...

    def close(self) -> None: ...


class _DCB(ctypes.Structure):
    _fields_ = [
        ("DCBlength", wintypes.DWORD),
        ("BaudRate", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("wReserved", wintypes.WORD),
        ("XonLim", wintypes.WORD),
        ("XoffLim", wintypes.WORD),
        ("ByteSize", wintypes.BYTE),
        ("Parity", wintypes.BYTE),
        ("StopBits", wintypes.BYTE),
        ("XonChar", ctypes.c_char),
        ("XoffChar", ctypes.c_char),
        ("ErrorChar", ctypes.c_char),
        ("EofChar", ctypes.c_char),
        ("EvtChar", ctypes.c_char),
        ("wReserved1", wintypes.WORD),
    ]


class _COMMTIMEOUTS(ctypes.Structure):
    _fields_ = [
        ("ReadIntervalTimeout", wintypes.DWORD),
        ("ReadTotalTimeoutMultiplier", wintypes.DWORD),
        ("ReadTotalTimeoutConstant", wintypes.DWORD),
        ("WriteTotalTimeoutMultiplier", wintypes.DWORD),
        ("WriteTotalTimeoutConstant", wintypes.DWORD),
    ]


class Win32SerialTransport:
    """不依赖 pyserial 的 Win32 COM 传输，固定为 8N1、无流控。"""

    def __init__(
        self,
        port: str = DEFAULT_PORT,
        *,
        baudrate: int = DEFAULT_BAUDRATE,
        read_timeout_ms: int = 100,
        write_timeout_ms: int = 1000,
        dtr: bool = True,
        rts: bool = True,
    ) -> None:
        self.port = port
        self.baudrate = baudrate
        self.read_timeout_ms = read_timeout_ms
        self.write_timeout_ms = write_timeout_ms
        self.dtr = dtr
        self.rts = rts
        self.rx_buffer_size = DEFAULT_RX_BUFFER_SIZE
        self.tx_buffer_size = DEFAULT_TX_BUFFER_SIZE
        self._kernel32 = None
        self._handle = None
        self._write_lock = threading.Lock()

    @property
    def is_open(self) -> bool:
        return self._handle is not None

    def _win_error(self, operation: str) -> SerialTransportError:
        error = ctypes.get_last_error()
        detail = ctypes.FormatError(error).strip() if error else "unknown error"
        return SerialTransportError(f"{operation} {self.port} failed: [{error}] {detail}")

    def open(self) -> None:
        if self.is_open:
            return
        if os.name != "nt":
            raise SerialTransportError("Win32SerialTransport only supports Windows")
        if not self.port or "\r" in self.port or "\n" in self.port:
            raise SerialTransportError(f"invalid serial port: {self.port!r}")

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateFileW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        ]
        kernel32.CreateFileW.restype = wintypes.HANDLE
        kernel32.GetCommState.argtypes = [wintypes.HANDLE, ctypes.POINTER(_DCB)]
        kernel32.GetCommState.restype = wintypes.BOOL
        kernel32.SetCommState.argtypes = [wintypes.HANDLE, ctypes.POINTER(_DCB)]
        kernel32.SetCommState.restype = wintypes.BOOL
        kernel32.SetCommTimeouts.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(_COMMTIMEOUTS),
        ]
        kernel32.SetCommTimeouts.restype = wintypes.BOOL
        kernel32.SetupComm.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD]
        kernel32.SetupComm.restype = wintypes.BOOL
        kernel32.ReadFile.argtypes = [
            wintypes.HANDLE,
            wintypes.LPVOID,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
            wintypes.LPVOID,
        ]
        kernel32.ReadFile.restype = wintypes.BOOL
        kernel32.WriteFile.argtypes = [
            wintypes.HANDLE,
            wintypes.LPCVOID,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
            wintypes.LPVOID,
        ]
        kernel32.WriteFile.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        kernel32.CancelIoEx.argtypes = [wintypes.HANDLE, wintypes.LPVOID]
        kernel32.CancelIoEx.restype = wintypes.BOOL

        device = self.port if self.port.startswith("\\\\.\\") else f"\\\\.\\{self.port}"
        handle = kernel32.CreateFileW(
            device,
            0x80000000 | 0x40000000,  # GENERIC_READ | GENERIC_WRITE
            0,
            None,
            3,  # OPEN_EXISTING
            0,
            None,
        )
        if handle == ctypes.c_void_p(-1).value:
            self._kernel32 = kernel32
            raise self._win_error("open")

        self._kernel32 = kernel32
        self._handle = handle
        try:
            # The 6202 USB-serial driver only accepts the larger queue reliably
            # after the port settings have been applied.  Start with 64 KiB,
            # then resize below once DCB and timeouts are ready.
            initial_rx_buffer_size = min(self.rx_buffer_size, 64 * 1024)
            if not kernel32.SetupComm(
                handle, initial_rx_buffer_size, self.tx_buffer_size
            ):
                raise self._win_error("SetupComm")
            dcb = _DCB()
            dcb.DCBlength = ctypes.sizeof(_DCB)
            if not kernel32.GetCommState(handle, ctypes.byref(dcb)):
                raise self._win_error("GetCommState")
            dcb.BaudRate = self.baudrate
            # fBinary + fTXContinueOnXoff；CTS/DSR/XON/XOFF flow-control 位保持 0。
            # MTP 截图控制链路必须关闭 DTR/RTS，旧串口链路继续沿用默认开启值。
            dcb.flags = (1 << 0) | (1 << 7)
            if self.dtr:
                dcb.flags |= 1 << 4  # fDtrControl(ENABLE)
            if self.rts:
                dcb.flags |= 1 << 12  # fRtsControl(ENABLE)
            dcb.ByteSize = 8
            dcb.Parity = 0  # NOPARITY
            dcb.StopBits = 0  # ONESTOPBIT
            if not kernel32.SetCommState(handle, ctypes.byref(dcb)):
                raise self._win_error("SetCommState")

            timeouts = _COMMTIMEOUTS(
                0xFFFFFFFF,
                0,
                self.read_timeout_ms,
                0,
                self.write_timeout_ms,
            )
            if not kernel32.SetCommTimeouts(handle, ctypes.byref(timeouts)):
                raise self._win_error("SetCommTimeouts")

            # One 410x502 BGR888 screenshot produces roughly 1 MiB of JSON.
            # Resize after configuring the port so burst capture does not lose
            # chunks while Python persists each line.
            if self.rx_buffer_size != initial_rx_buffer_size and not kernel32.SetupComm(
                handle, self.rx_buffer_size, self.tx_buffer_size
            ):
                raise self._win_error("SetupComm")
        except Exception:
            self.close()
            raise

    def read(self, size: int) -> bytes:
        if not self.is_open or self._kernel32 is None:
            raise SerialTransportError("serial transport is not open")
        buffer = ctypes.create_string_buffer(size)
        received = wintypes.DWORD()
        if not self._kernel32.ReadFile(
            self._handle, buffer, size, ctypes.byref(received), None
        ):
            raise self._win_error("read")
        return buffer.raw[: received.value]

    def write(self, data: bytes) -> int:
        if not self.is_open or self._kernel32 is None:
            raise SerialTransportError("serial transport is not open")
        total = 0
        with self._write_lock:
            while total < len(data):
                remaining = data[total:]
                sent = wintypes.DWORD()
                buffer = ctypes.create_string_buffer(remaining)
                if not self._kernel32.WriteFile(
                    self._handle,
                    buffer,
                    len(remaining),
                    ctypes.byref(sent),
                    None,
                ):
                    raise self._win_error("write")
                if sent.value == 0:
                    raise SerialTransportError(f"write {self.port} made no progress")
                total += sent.value
        return total

    def close(self) -> None:
        handle, self._handle = self._handle, None
        if handle is not None and self._kernel32 is not None:
            self._kernel32.CloseHandle(handle)


class _WinOverlapped(ctypes.Structure):
    _fields_ = [
        ("Internal", ctypes.c_size_t),
        ("InternalHigh", ctypes.c_size_t),
        ("Offset", wintypes.DWORD),
        ("OffsetHigh", wintypes.DWORD),
        ("hEvent", wintypes.HANDLE),
    ]


class SuperComPipeTransport:
    """Transparent byte transport through a SuperCom-owned COM port."""

    PIPE_PREFIX = "SuperCom.AgentBridge."

    def __init__(self, port: str = DEFAULT_PORT, *, connect_timeout_ms: int = 2000) -> None:
        if not port or "\r" in port or "\n" in port:
            raise ValueError(f"invalid serial port: {port!r}")
        if (
            isinstance(connect_timeout_ms, bool)
            or not isinstance(connect_timeout_ms, int)
            or connect_timeout_ms <= 0
        ):
            raise ValueError("connect_timeout_ms must be a positive integer")
        self.port = port
        safe_port = "".join(
            value if value.isalnum() or value in "_-" else "_"
            for value in port.strip().upper()
        )
        self.pipe_name = f"{self.PIPE_PREFIX}{safe_port}"
        self.pipe_path = rf"\\.\pipe\{self.pipe_name}"
        self.connect_timeout_ms = connect_timeout_ms
        self._kernel32 = None
        self._handle = None
        self._write_lock = threading.Lock()

    @property
    def is_open(self) -> bool:
        return self._handle is not None

    def _win_error(self, operation: str) -> SerialTransportError:
        error = ctypes.get_last_error()
        detail = ctypes.FormatError(error).strip() if error else "unknown error"
        return SerialTransportError(
            f"{operation} SuperCom bridge for {self.port} failed: [{error}] {detail}"
        )

    def open(self) -> None:
        if self.is_open:
            return
        if os.name != "nt":
            raise SerialTransportError("SuperComPipeTransport only supports Windows")

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.WaitNamedPipeW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD]
        kernel32.WaitNamedPipeW.restype = wintypes.BOOL
        kernel32.CreateFileW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        ]
        kernel32.CreateFileW.restype = wintypes.HANDLE
        kernel32.ReadFile.argtypes = [
            wintypes.HANDLE,
            wintypes.LPVOID,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
            wintypes.LPVOID,
        ]
        kernel32.ReadFile.restype = wintypes.BOOL
        kernel32.WriteFile.argtypes = [
            wintypes.HANDLE,
            wintypes.LPCVOID,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
            wintypes.LPVOID,
        ]
        kernel32.WriteFile.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        kernel32.CancelIoEx.argtypes = [wintypes.HANDLE, wintypes.LPVOID]
        kernel32.CancelIoEx.restype = wintypes.BOOL
        kernel32.CreateEventW.argtypes = [
            wintypes.LPVOID,
            wintypes.BOOL,
            wintypes.BOOL,
            wintypes.LPCWSTR,
        ]
        kernel32.CreateEventW.restype = wintypes.HANDLE
        kernel32.GetOverlappedResult.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(_WinOverlapped),
            ctypes.POINTER(wintypes.DWORD),
            wintypes.BOOL,
        ]
        kernel32.GetOverlappedResult.restype = wintypes.BOOL

        self._kernel32 = kernel32
        deadline = time.monotonic() + self.connect_timeout_ms / 1000.0
        while not kernel32.WaitNamedPipeW(self.pipe_path, 100):
            error = ctypes.get_last_error()
            if error not in (2, 121, 231) or time.monotonic() >= deadline:
                raise self._win_error("connect")
            time.sleep(0.02)

        handle = kernel32.CreateFileW(
            self.pipe_path,
            0x80000000 | 0x40000000,  # GENERIC_READ | GENERIC_WRITE
            0,
            None,
            3,  # OPEN_EXISTING
            0x40000000,  # FILE_FLAG_OVERLAPPED
            None,
        )
        if handle == ctypes.c_void_p(-1).value:
            raise self._win_error("open")
        self._handle = handle

    def read(self, size: int) -> bytes:
        if not self.is_open or self._kernel32 is None:
            raise SerialTransportError("SuperCom bridge is not open")
        if size <= 0:
            return b""
        buffer = ctypes.create_string_buffer(size)
        event = self._kernel32.CreateEventW(None, True, False, None)
        if not event:
            raise self._win_error("create read event for")
        overlapped = _WinOverlapped(hEvent=event)
        received = wintypes.DWORD()
        try:
            if not self._kernel32.ReadFile(
                self._handle, buffer, size, None, ctypes.byref(overlapped)
            ) and ctypes.get_last_error() != 997:  # ERROR_IO_PENDING
                raise self._win_error("read")
            if not self._kernel32.GetOverlappedResult(
                self._handle, ctypes.byref(overlapped), ctypes.byref(received), True
            ):
                raise self._win_error("read")
            return buffer.raw[: received.value]
        finally:
            self._kernel32.CloseHandle(event)

    def write(self, data: bytes) -> int:
        if not self.is_open or self._kernel32 is None:
            raise SerialTransportError("SuperCom bridge is not open")
        payload = bytes(data)
        if not payload:
            return 0
        with self._write_lock:
            event = self._kernel32.CreateEventW(None, True, False, None)
            if not event:
                raise self._win_error("create write event for")
            overlapped = _WinOverlapped(hEvent=event)
            sent = wintypes.DWORD()
            buffer = ctypes.create_string_buffer(payload)
            try:
                if not self._kernel32.WriteFile(
                    self._handle,
                    buffer,
                    len(payload),
                    None,
                    ctypes.byref(overlapped),
                ) and ctypes.get_last_error() != 997:  # ERROR_IO_PENDING
                    raise self._win_error("write")
                if not self._kernel32.GetOverlappedResult(
                    self._handle, ctypes.byref(overlapped), ctypes.byref(sent), True
                ):
                    raise self._win_error("write")
                if sent.value != len(payload):
                    raise SerialTransportError(
                        f"short write to SuperCom bridge for {self.port}: "
                        f"{sent.value}/{len(payload)} bytes"
                    )
                return sent.value
            finally:
                self._kernel32.CloseHandle(event)

    def close(self) -> None:
        handle, self._handle = self._handle, None
        if handle is not None and self._kernel32 is not None:
            self._kernel32.CancelIoEx(handle, None)
            self._kernel32.CloseHandle(handle)


@dataclass
class HardwareCommandResult:
    """字段与模拟器 ``CommandResult`` 兼容。"""

    request: str
    status: str
    raw: dict
    lines: list[str] = field(default_factory=list)
    start_index: int | None = None


@dataclass(frozen=True)
class _EventRecord:
    event: dict
    line_index: int


def strip_ansi(text: str) -> str:
    """移除串口日志里的 ANSI 终端控制序列。"""

    return ANSI_ESCAPE_RE.sub("", text)


def _command_parts(content: str) -> tuple[str, str, str]:
    normalized = normalize_command(content)
    name, _, arguments = normalized[1:].partition(":")
    return normalized, name, arguments


def dangerous_command_reason(content: str) -> str | None:
    """返回真机命令被拒绝的原因；安全命令返回 ``None``。"""

    _normalized, name, arguments = _command_parts(content)
    upper = name.upper()
    tokens = {token for token in upper.split("_") if token}

    if upper == "FACTORY_RESET" or {"FACTORY", "RESET"} <= tokens:
        return "factory reset"
    if (
        "SHUTDOWN" in upper
        or "POWER_OFF" in upper
        or "POWEROFF" in upper
        or "REBOOT" in upper
        or "RESTART" in upper
    ):
        return "shutdown or restart"
    if tokens.intersection({"CLEAR", "ERASE", "WIPE", "FORMAT"}):
        return "clear or erase"
    if "DELETE" in tokens and "ALL" in tokens:
        return "delete all"
    if upper == "POWER_PERCENT_SET":
        first_argument = arguments.partition(",")[0].strip()
        try:
            if int(first_argument, 10) == 0:
                return "POWER_PERCENT_SET:0 may force a power-off path"
        except ValueError:
            pass
    return None


def is_dangerous_command(content: str) -> bool:
    """判断命令是否命中默认真机危险命令策略。"""

    return dangerous_command_reason(content) is not None


class HardwareSerialSession:
    """后台读取真机串口、持久化日志并关联命令回执。"""

    def __init__(
        self,
        *,
        port: str = DEFAULT_PORT,
        baudrate: int = DEFAULT_BAUDRATE,
        log_dir: str | os.PathLike = "artifacts/hardware_serial",
        cmd_timeout: float = 8.0,
        transport: ByteTransport | None = None,
        dtr: bool = True,
        rts: bool = True,
        allow_dangerous_commands: bool = False,
        line_cache_capacity: int = DEFAULT_LINE_CACHE_CAPACITY,
        event_cache_capacity: int = DEFAULT_EVENT_CACHE_CAPACITY,
        background_error_cache_capacity: int = DEFAULT_BACKGROUND_ERROR_CACHE_CAPACITY,
    ) -> None:
        self.port = port
        self.baudrate = baudrate
        self.log_dir = Path(log_dir)
        self.cmd_timeout = cmd_timeout
        self.transport: ByteTransport = transport or Win32SerialTransport(
            port, baudrate=baudrate, dtr=dtr, rts=rts
        )
        self.allow_dangerous_commands = allow_dangerous_commands
        self.line_cache_capacity = _validate_cache_capacity(
            "line_cache_capacity", line_cache_capacity
        )
        self.event_cache_capacity = _validate_cache_capacity(
            "event_cache_capacity", event_cache_capacity
        )
        self.background_error_cache_capacity = _validate_cache_capacity(
            "background_error_cache_capacity", background_error_cache_capacity
        )

        self._condition = threading.Condition()
        self._send_lock = threading.Lock()
        self._lines: deque[str] = deque(maxlen=self.line_cache_capacity)
        self._events: deque[_EventRecord] = deque(maxlen=self.event_cache_capacity)
        self._line_count = 0
        self._event_count = 0
        self._background_errors: deque[str] = deque(
            maxlen=self.background_error_cache_capacity
        )
        self._background_error_count = 0
        self._stop_event = threading.Event()
        self._reader: threading.Thread | None = None
        self._reader_error: BaseException | None = None
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._text_buffer = ""
        self._pending_command_result = ""
        self._pending_command_result_line_index: int | None = None
        self._pending_command_result_line_count = 0
        self._started = False

        self._raw_stream = None
        self._text_stream = None
        self._event_stream = None
        self._error_stream = None

    @property
    def started(self) -> bool:
        return self._started

    @property
    def line_count(self) -> int:
        with self._condition:
            return self._line_count

    @property
    def event_count(self) -> int:
        with self._condition:
            return self._event_count

    @property
    def background_error_count(self) -> int:
        with self._condition:
            return self._background_error_count

    @property
    def line_cache_start_index(self) -> int:
        """Absolute index of the oldest line still retained in memory."""

        with self._condition:
            return self._line_count - len(self._lines)

    @property
    def event_cache_start_index(self) -> int:
        """Absolute index of the oldest event still retained in memory."""

        with self._condition:
            return self._event_count - len(self._events)

    @property
    def background_error_cache_start_index(self) -> int:
        """Absolute index of the oldest background error retained in memory."""

        with self._condition:
            return self._background_error_count - len(self._background_errors)

    def start(self) -> None:
        if self._started:
            return
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self._raw_stream = (self.log_dir / "raw.bin").open("wb")
        self._text_stream = (self.log_dir / "raw.log").open(
            "w", encoding="utf-8", newline="\n"
        )
        self._event_stream = (self.log_dir / "events.jsonl").open(
            "w", encoding="utf-8", newline="\n"
        )
        self._error_stream = (self.log_dir / "errors.log").open(
            "w", encoding="utf-8", newline="\n"
        )
        try:
            self.transport.open()
        except Exception as exc:
            self._close_log_streams()
            if isinstance(exc, SerialTransportError):
                raise
            raise SerialTransportError(f"open {self.port} failed: {exc}") from exc

        self._stop_event.clear()
        self._reader_error = None
        self._reader = threading.Thread(
            target=self._consume, name=f"hardware-serial-{self.port}", daemon=True
        )
        self._started = True
        self._reader.start()

    def _consume(self) -> None:
        try:
            while not self._stop_event.is_set():
                chunk = self.transport.read(DEFAULT_READ_SIZE)
                if chunk is None:
                    continue
                if not isinstance(chunk, (bytes, bytearray, memoryview)):
                    raise TypeError(f"transport.read returned {type(chunk).__name__}, not bytes")
                raw = bytes(chunk)
                if not raw:
                    continue
                assert self._raw_stream is not None
                self._raw_stream.write(raw)
                self._raw_stream.flush()
                self._feed_text(self._decoder.decode(raw, final=False))
        except Exception as exc:
            if not self._stop_event.is_set():
                with self._condition:
                    self._reader_error = exc
                    self._write_error_locked("transport_error", repr(exc))
                    self._condition.notify_all()
        finally:
            tail = self._decoder.decode(b"", final=True)
            if tail:
                self._feed_text(tail)
            self._flush_partial_line()

    def _feed_text(self, text: str) -> None:
        if not text:
            return
        self._text_buffer += text
        while True:
            cr = self._text_buffer.find("\r")
            lf = self._text_buffer.find("\n")
            positions = [position for position in (cr, lf) if position >= 0]
            if not positions:
                break
            end = min(positions)
            separator_length = 2 if self._text_buffer[end : end + 2] == "\r\n" else 1
            line = self._text_buffer[:end]
            self._text_buffer = self._text_buffer[end + separator_length :]
            self._record_line(line)

        if len(self._text_buffer) > _MAX_PENDING_TEXT:
            overflow = strip_ansi(self._text_buffer)
            self._text_buffer = ""
            with self._condition:
                self._write_error_locked(
                    "line_overflow", f"discarded unterminated text ({len(overflow)} chars)"
                )
                self._condition.notify_all()

    def _flush_partial_line(self) -> None:
        if self._text_buffer:
            line, self._text_buffer = self._text_buffer, ""
            self._record_line(line)

    def _record_line(self, raw_line: str) -> None:
        line = strip_ansi(raw_line)
        with self._condition:
            line_index = self._line_count
            self._lines.append(line)
            self._line_count += 1
            if self._text_stream is not None:
                self._text_stream.write(line + "\n")
                self._text_stream.flush()

            if BACKGROUND_ERROR_RE.search(line):
                self._background_errors.append(line)
                self._background_error_count += 1
                self._write_error_locked("device_background_error", line)

            # 协议必须从原始逻辑行提取。若 ANSI 颜色码只输出了 ``ESC[3`` 就被
            # GUI JSON 插入，先 strip_ansi 会把随后的 ``{`` 当作 CSI 结束符一起
            # 删除，造成已经 processed 的 GUI_ACK 被误判为超时。
            self._extract_bridge_events_locked(raw_line, line_index)
            self._condition.notify_all()

    def _extract_bridge_events_locked(self, line: str, line_index: int) -> None:
        if self._pending_command_result:
            if BRIDGE_MARKER in line:
                self._abandon_pending_command_result_locked("new bridge marker")
            else:
                self._pending_command_result += "\n" + line
                self._pending_command_result_line_count += 1
                if self._try_recover_pending_command_result_locked():
                    return
                if (
                    "}" in line
                    or self._pending_command_result_line_count
                    >= _MAX_INTERLEAVED_COMMAND_RESULT_LINES
                    or len(self._pending_command_result)
                    >= _MAX_INTERLEAVED_COMMAND_RESULT_TEXT
                ):
                    self._abandon_pending_command_result_locked(
                        "recovery boundary reached"
                    )
                return

        offset = 0
        while True:
            start = line.find(BRIDGE_MARKER, offset)
            if start < 0:
                return
            candidate = line[start:]
            try:
                value, consumed = _JSON_DECODER.raw_decode(candidate)
            except json.JSONDecodeError as exc:
                self._write_error_locked(
                    "protocol_json_error",
                    f"column={start + exc.pos + 1} line={line}",
                )
                if (
                    candidate.startswith(BRIDGE_COMMAND_RESULT_PREFIX)
                    and candidate.count(BRIDGE_MARKER) == 1
                ):
                    self._pending_command_result = candidate
                    self._pending_command_result_line_index = line_index
                    self._pending_command_result_line_count = 1
                    if self._try_recover_pending_command_result_locked():
                        return
                    if "}" in candidate:
                        self._abandon_pending_command_result_locked(
                            "complete candidate did not match recovery allowlist"
                        )
                    else:
                        return
                # 同一物理行后面仍可能有另一条独立桥接事件；除上面的严格
                # command_result 白名单外，不删除任意日志来“修复”损坏 JSON。
                offset = start + len(BRIDGE_MARKER)
                continue

            offset = start + max(consumed, 1)
            if not isinstance(value, dict) or value.get("protocol") != BRIDGE_PROTOCOL:
                self._write_error_locked("protocol_value_error", candidate[:consumed])
                continue
            parsed_value = dict(value)
            if self._try_recover_parsed_command_result_locked(
                parsed_value, line_index
            ):
                continue
            self._record_bridge_event_locked(parsed_value, line_index)

    def _record_bridge_event_locked(self, value: dict, line_index: int) -> None:
        record = _EventRecord(dict(value), line_index)
        self._events.append(record)
        self._event_count += 1
        if self._event_stream is not None:
            self._event_stream.write(
                json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n"
            )
            self._event_stream.flush()

    def _try_recover_pending_command_result_locked(self) -> bool:
        block = self._pending_command_result
        match = _INTERLEAVED_COMMAND_RESULT_RE.match(block)
        if match is None:
            return False

        middle = match.group("middle")
        middle = middle.replace(VOICE_ASSISTANT_INIT_BANNER, "")
        middle = _INTERLEAVED_CURRENT_APP_RE.sub("", middle)
        middle = middle.replace("gui_list_real_set_header", "")
        middle = middle.replace("uart:~$", "")
        if re.sub(r"\s+", "", middle) != "_result":
            return False

        value: dict[str, object] = {
            "protocol": BRIDGE_PROTOCOL,
            "version": 1,
            "type": "command_result",
            "request": match.group("request"),
            "seq": None,
            "status": match.group("status"),
        }
        if match.group("reason"):
            value["reason"] = "handler_failed"
        start_line_index = self._pending_command_result_line_index
        if start_line_index is None:
            return False
        line_count = self._pending_command_result_line_count
        self._record_bridge_event_locked(value, start_line_index)
        self._write_error_locked(
            "protocol_json_recovered",
            f"request={value['request']} status={value['status']} lines={line_count}",
        )
        self._clear_pending_command_result_locked()
        return True

    def _try_recover_parsed_command_result_locked(
        self, value: dict, line_index: int
    ) -> bool:
        event_type = value.get("type")
        if (
            not isinstance(event_type, str)
            or VOICE_ASSISTANT_INIT_BANNER not in event_type
            or event_type.replace(VOICE_ASSISTANT_INIT_BANNER, "")
            != "command_result"
            or value.get("version") != 1
            or value.get("seq") is not None
            or not re.fullmatch(r"[a-z0-9_]+", str(value.get("request", "")))
            or str(value.get("status", "")) not in {"accepted", "rejected"}
        ):
            return False

        recovered = dict(value)
        recovered["type"] = "command_result"
        self._record_bridge_event_locked(recovered, line_index)
        self._write_error_locked(
            "protocol_json_recovered",
            f"request={recovered['request']} status={recovered['status']} lines=1",
        )
        return True

    def _clear_pending_command_result_locked(self) -> None:
        self._pending_command_result = ""
        self._pending_command_result_line_index = None
        self._pending_command_result_line_count = 0

    def _abandon_pending_command_result_locked(self, reason: str) -> None:
        self._write_error_locked(
            "protocol_json_recovery_abandoned",
            f"{reason}; lines={self._pending_command_result_line_count}",
        )
        self._clear_pending_command_result_locked()

    def _write_error_locked(self, kind: str, detail: str) -> None:
        if self._error_stream is None:
            return
        timestamp = datetime.now().astimezone().isoformat(timespec="milliseconds")
        single_line = detail.replace("\r", "\\r").replace("\n", "\\n")
        self._error_stream.write(f"{timestamp}\t{kind}\t{single_line}\n")
        self._error_stream.flush()

    def _snapshot_lines(self) -> list[str]:
        with self._condition:
            return list(self._lines)

    def _lines_since_locked(self, start_index: int) -> list[str]:
        oldest_available = self._line_count - len(self._lines)
        if start_index < oldest_available:
            raise HardwareSerialCursorExpiredError(
                stream="line",
                cursor=start_index,
                oldest_available=oldest_available,
                next_index=self._line_count,
            )
        offset = start_index - oldest_available
        return list(self._lines)[offset:]

    def _event_records_since_locked(self, start_index: int) -> list[_EventRecord]:
        oldest_available = self._event_count - len(self._events)
        if start_index < oldest_available:
            raise HardwareSerialCursorExpiredError(
                stream="event",
                cursor=start_index,
                oldest_available=oldest_available,
                next_index=self._event_count,
            )
        offset = start_index - oldest_available
        return list(self._events)[offset:]

    def _background_errors_since_locked(self, start_index: int) -> list[str]:
        oldest_available = self._background_error_count - len(self._background_errors)
        if start_index < oldest_available:
            raise HardwareSerialCursorExpiredError(
                stream="background_error",
                cursor=start_index,
                oldest_available=oldest_available,
                next_index=self._background_error_count,
            )
        offset = start_index - oldest_available
        return list(self._background_errors)[offset:]

    def lines_since(self, start_index: int) -> list[str]:
        """返回指定逻辑行之后的已清洗串口文本。"""

        if start_index < 0:
            raise ValueError("start_index must be non-negative")
        with self._condition:
            return self._lines_since_locked(start_index)

    def events_since(self, start_index: int) -> list[dict]:
        """返回指定协议事件序号之后的桥接 JSON。"""

        if start_index < 0:
            raise ValueError("start_index must be non-negative")
        with self._condition:
            return [
                dict(record.event)
                for record in self._event_records_since_locked(start_index)
            ]

    def background_errors_since(self, start_index: int = 0) -> list[str]:
        """读取 HardFault/ASSERT/Fatal/watchdog/reboot 旁路记录。"""

        if start_index < 0:
            raise ValueError("start_index must be non-negative")
        with self._condition:
            return self._background_errors_since_locked(start_index)

    @staticmethod
    def _matches(value: object, expected: object | Iterable[object] | None) -> bool:
        if expected is None:
            return True
        if isinstance(expected, (str, bytes)):
            return str(value).lower() == str(expected).lower()
        try:
            return any(HardwareSerialSession._matches(value, item) for item in expected)
        except TypeError:
            return value == expected

    def _event_matches(
        self,
        event: dict,
        *,
        request: str | None,
        seq: int | str | None,
        event_type: str | Iterable[str] | None,
        status: str | Iterable[str] | None,
    ) -> bool:
        if request is not None and str(event.get("request", "")).lower() != request.lower():
            return False
        if seq is not None and str(event.get("seq")) != str(seq):
            return False
        return self._matches(event.get("type"), event_type) and self._matches(
            event.get("status", ""), status
        )

    def wait_for_event(
        self,
        *,
        request: str | None = None,
        seq: int | str | None = None,
        event_type: str | Iterable[str] | None = None,
        status: str | Iterable[str] | None = None,
        timeout: float | None = None,
        start_event_index: int = 0,
    ) -> dict:
        """按 request/seq/type/status 等待一条新桥接事件。"""

        if not self._started:
            raise RuntimeError("hardware serial session not started")
        if start_event_index < 0:
            raise ValueError("start_event_index must be non-negative")
        timeout_value = self.cmd_timeout if timeout is None else timeout
        deadline = time.monotonic() + timeout_value
        next_index = start_event_index
        with self._condition:
            while True:
                for record in self._event_records_since_locked(next_index):
                    event = record.event
                    next_index += 1
                    if self._event_matches(
                        event,
                        request=request,
                        seq=seq,
                        event_type=event_type,
                        status=status,
                    ):
                        return dict(event)
                if self._reader_error is not None:
                    raise SerialTransportError(
                        f"serial reader stopped: {self._reader_error}"
                    ) from self._reader_error
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise HardwareSerialTimeoutError(
                        "no matching w30_test_bridge event within "
                        f"{timeout_value}s (request={request!r}, seq={seq!r}, "
                        f"type={event_type!r}, status={status!r})"
                    )
                self._condition.wait(remaining)

    @staticmethod
    def _infer_seq(request: str, arguments: str) -> int | None:
        if request not in _DEFAULT_COMPLETIONS:
            return None
        first = arguments.partition(",")[0].strip()
        if not first.isdecimal():
            return None
        return int(first, 10)

    def _write_wire_locked(self, wire: bytes) -> None:
        try:
            written = self.transport.write(wire)
        except Exception as exc:
            with self._condition:
                self._write_error_locked("transport_write_error", repr(exc))
            if isinstance(exc, SerialTransportError):
                raise
            raise SerialTransportError(f"write {self.port} failed: {exc}") from exc
        if written is not None and written != len(wire):
            raise SerialTransportError(
                f"short write on {self.port}: {written}/{len(wire)} bytes"
            )

    def send(
        self,
        content: str,
        *,
        request: str | None = None,
        seq: int | str | None = None,
        timeout: float | None = None,
        expected_type: str | Iterable[str] | None = None,
        expected_status: str | Iterable[str] | None = None,
    ) -> HardwareCommandResult:
        """发送 Quick Command 并等待相关回执。

        GUI_PING/STATE/TREE 默认等待各自的 GUI 终态。其他命令不会把
        ``accepted`` 当完成；如果调用方只需要确认命令已收到，必须显式传入
        ``expected_type="command_result", expected_status="accepted"``，随后再用
        GUI_PING 同步 GUI 线程。
        """

        if not self._started:
            raise RuntimeError("hardware serial session not started")
        normalized, command_name, arguments = _command_parts(content)
        reason = dangerous_command_reason(normalized)
        if reason is not None and not self.allow_dangerous_commands:
            raise UnsafeHardwareCommandError(
                f"hardware command {command_name} refused: {reason}"
            )

        request_value = request or command_name.lower()
        seq_value = seq if seq is not None else self._infer_seq(request_value, arguments)
        if expected_type is None and expected_status is None:
            default_completion = _DEFAULT_COMPLETIONS.get(request_value)
            if default_completion is not None:
                expected_type, expected_status = default_completion

        wire = f'srv_quick_cmd send "TOP5STEP:{normalized[1:]};"\r\n'.encode("utf-8")
        timeout_value = self.cmd_timeout if timeout is None else timeout

        with self._send_lock:
            with self._condition:
                start_line_index = self._line_count
                start_event_index = self._event_count
            write_shell_wire(self._write_wire_locked, wire)

            deadline = time.monotonic() + timeout_value
            next_event_index = start_event_index
            matched: dict | None = None
            with self._condition:
                while matched is None:
                    for record in self._event_records_since_locked(next_event_index):
                        event = record.event
                        next_event_index += 1
                        if str(event.get("request", "")).lower() != request_value.lower():
                            continue
                        if seq_value is not None and str(event.get("seq")) != str(seq_value):
                            continue

                        exact_match = self._event_matches(
                            event,
                            request=request_value,
                            seq=seq_value,
                            event_type=expected_type,
                            status=expected_status,
                        )
                        if exact_match:
                            # accepted 只有在调用方明确要求 accepted 时才会到这里。
                            if str(event.get("status", "")).lower() != "accepted" or (
                                expected_status is not None
                                and self._matches("accepted", expected_status)
                            ):
                                matched = dict(event)
                                break

                        event_status = str(event.get("status", "")).lower()
                        event_type_value = str(event.get("type", "")).lower()
                        type_matches = self._matches(event.get("type"), expected_type)
                        # rejected/error/busy 等终态要立即交给调用方；accepted 继续等。
                        if event_status != "accepted" and (
                            event_type_value == "command_result" or type_matches
                        ):
                            matched = dict(event)
                            break

                    if matched is not None:
                        break
                    if self._reader_error is not None:
                        raise SerialTransportError(
                            f"serial reader stopped: {self._reader_error}"
                        ) from self._reader_error
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        tail = list(self._lines)[-20:]
                        raise HardwareSerialTimeoutError(
                            f"no result for {normalized} within {timeout_value}s "
                            f"(request={request_value!r}, seq={seq_value!r}, "
                            f"type={expected_type!r}, status={expected_status!r}); "
                            f"tail={tail!r}"
                        )
                    self._condition.wait(remaining)

                lines = self._lines_since_locked(start_line_index)

        return HardwareCommandResult(
            request=str(matched.get("request", "")),
            status=str(matched.get("status", "")),
            raw=matched,
            lines=lines,
            start_index=start_line_index,
        )

    def write_shell_line(self, line: str) -> None:
        """Write one CRLF-terminated shell line through the shared port.

        This is intentionally lower-level than :meth:`send`: it is used for
        engineering shell commands such as ``dal_usb close`` that are outside
        the Quick Command protocol.  The shared send lock prevents byte-level
        interleaving with normal commands.
        """

        if not self._started:
            raise RuntimeError("hardware serial session not started")
        if not isinstance(line, str) or not line or "\r" in line or "\n" in line:
            raise ValueError("line must be one non-empty shell line")
        wire = (line + "\r\n").encode("utf-8")
        with self._send_lock:
            write_shell_wire(self._write_wire_locked, wire)

    def stop(self) -> None:
        if not self._started:
            return
        self._stop_event.set()
        try:
            self.transport.close()
        finally:
            reader = self._reader
            if reader is not None and reader is not threading.current_thread():
                reader.join(timeout=2.0)
            self._started = False
            self._close_log_streams()

    def _close_log_streams(self) -> None:
        for name in ("_raw_stream", "_text_stream", "_event_stream", "_error_stream"):
            stream = getattr(self, name)
            if stream is not None:
                stream.close()
                setattr(self, name, None)

    def __enter__(self) -> "HardwareSerialSession":
        self.start()
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        self.stop()
