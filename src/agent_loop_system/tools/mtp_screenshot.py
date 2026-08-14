"""独立的 Windows MTP 真机截图工具。

控制命令走调试 UART，图片走 Windows MTP。模块不读取固件源码目录，也不依赖
Agent-loop Runner；安装包后可从任意工作目录调用 ``watch-mtp-screenshot``。
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import math
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
import zlib
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Protocol

from agent_loop_system.tools.hardware_serial import (
    BRIDGE_MARKER,
    DEFAULT_BAUDRATE,
    DEFAULT_PORT,
    SerialTransportError,
    Win32SerialTransport,
)


USB_INSTANCE_PATTERN = "VID_301A&PID_6808"
MTP_DEVICE_NAME = "ZORA"
MTP_FOLDER_NAME = "download"
MTP_CAPTURE_PREFIX = "agent_capture_"
MTP_CAPTURE_SUFFIX = ".bmp"
CAPTURE_REQUEST = "screenshot_capture_file"
CAPTURE_COMMAND = "SCREENSHOT_CAPTURE_FILE"
EXPECTED_WIDTH = 410
EXPECTED_HEIGHT = 502
DEFAULT_USB_TIMEOUT = 30.0
DEFAULT_MTP_TIMEOUT = 30.0


class MtpScreenshotError(RuntimeError):
    """截图流程失败。"""


class MtpScreenshotTimeoutError(MtpScreenshotError, TimeoutError):
    """USB、截图或 MTP 操作超时。"""


class MtpScreenshotDeviceError(MtpScreenshotError):
    """固件明确拒绝或终止截图。"""


class MtpScreenshotValidationError(MtpScreenshotError, ValueError):
    """下载文件或固件回执不符合协议。"""


class MtpSystem(Protocol):
    """可注入的 Windows USB/MTP 边界。"""

    def wait_for_usb(self, *, present: bool, timeout: float) -> None: ...

    def copy_capture(
        self, destination_dir: Path, *, file_name: str, timeout: float
    ) -> Path: ...


@dataclass(frozen=True, slots=True)
class BmpInfo:
    actual_size: int
    declared_size: int
    width: int
    height: int
    top_down: bool
    bits_per_pixel: int
    payload_crc32: int
    sha256: str


@dataclass(frozen=True, slots=True)
class MtpScreenshotResult:
    status: str
    sequence: int
    output_path: str
    actual_size: int
    width: int
    height: int
    bits_per_pixel: int
    top_down: bool
    payload_crc32: int
    sha256: str
    receipt_verified: bool
    device_uptime_ms: int | None = None
    capture_duration_ms: int | None = None

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["payload_crc32"] = f"{self.payload_crc32:08x}"
        return value


@dataclass(frozen=True, slots=True)
class MtpCaptureMetadata:
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
    encoding: str
    file_size: int
    receipt_verified: bool
    mtp_file_name: str
    device_uptime_ms: int | None = None
    capture_duration_ms: int | None = None
    pixel_source: str | None = None


@dataclass(frozen=True, slots=True)
class MtpCaptureFrame:
    """One validated BMP held independently of its temporary MTP download."""

    bmp: bytes
    metadata: MtpCaptureMetadata

    def save_bmp(
        self, output_path: str | os.PathLike[str]
    ) -> MtpCaptureMetadata:
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(self.bmp)
        return self.metadata


class WindowsMtpSystem:
    """通过 Windows 自带 PowerShell 和 Shell.Application 访问 WPD/MTP。"""

    def __init__(
        self,
        *,
        device_name: str = MTP_DEVICE_NAME,
        usb_instance_pattern: str = USB_INSTANCE_PATTERN,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    ) -> None:
        self.device_name = device_name
        self.usb_instance_pattern = usb_instance_pattern
        self._runner = runner
        self._powershell = shutil.which("powershell.exe") or "powershell.exe"

    def _run(
        self,
        script: str,
        *,
        timeout: float,
        extra_env: dict[str, str] | None = None,
    ) -> str:
        if os.name != "nt":
            raise MtpScreenshotError("MTP screenshot currently supports Windows only")
        encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
        env = os.environ.copy()
        if extra_env:
            env.update(extra_env)
        try:
            completed = self._runner(
                [
                    self._powershell,
                    "-NoProfile",
                    "-NonInteractive",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-EncodedCommand",
                    encoded,
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                env=env,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise MtpScreenshotTimeoutError("Windows MTP operation timed out") from exc
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "unknown error").strip()
            raise MtpScreenshotError(f"Windows MTP operation failed: {detail}")
        return completed.stdout.strip()

    def wait_for_usb(self, *, present: bool, timeout: float) -> None:
        script = r"""
$wanted = $env:WATCH_USB_PRESENT -eq '1'
$pattern = $env:WATCH_USB_PATTERN
$deadline = (Get-Date).AddMilliseconds([double]$env:WATCH_USB_TIMEOUT_MS)
do {
    $found = @(
        Get-PnpDevice -PresentOnly -ErrorAction SilentlyContinue |
        Where-Object { $_.InstanceId -match $pattern }
    ).Count -gt 0
    if ($found -eq $wanted) { exit 0 }
    Start-Sleep -Milliseconds 250
} while ((Get-Date) -lt $deadline)
[Console]::Error.WriteLine('USB state did not reach the requested value')
exit 2
"""
        try:
            self._run(
                script,
                timeout=timeout + 10.0,
                extra_env={
                    "WATCH_USB_PRESENT": "1" if present else "0",
                    "WATCH_USB_PATTERN": self.usb_instance_pattern,
                    "WATCH_USB_TIMEOUT_MS": str(round(timeout * 1000)),
                },
            )
        except MtpScreenshotError as exc:
            raise MtpScreenshotTimeoutError(
                f"USB device {self.usb_instance_pattern} did not become "
                f"{'present' if present else 'absent'} within {timeout}s"
            ) from exc

    def copy_capture(
        self, destination_dir: Path, *, file_name: str, timeout: float
    ) -> Path:
        script = r"""
$discoveryDeadline = (Get-Date).AddMilliseconds([double]$env:WATCH_MTP_TIMEOUT_MS)
$lastDiscoveryError = 'MTP device not found'
do {
    $shell = New-Object -ComObject Shell.Application
    $thisPc = $shell.Namespace(17)
    $device = @($thisPc.Items()) |
        Where-Object { $_.Name -eq $env:WATCH_MTP_DEVICE } |
        Select-Object -First 1
    if (-not $device) {
        $lastDiscoveryError = 'MTP device not found'
        Start-Sleep -Milliseconds 500
        continue
    }
    $storage = @($device.GetFolder.Items()) | Select-Object -First 1
    if (-not $storage) {
        $lastDiscoveryError = 'MTP storage not found'
        Start-Sleep -Milliseconds 500
        continue
    }
    $folder = @($storage.GetFolder.Items()) |
        Where-Object { $_.Name -eq $env:WATCH_MTP_FOLDER } |
        Select-Object -First 1
    if (-not $folder) {
        $lastDiscoveryError = 'MTP folder not found'
        Start-Sleep -Milliseconds 500
        continue
    }
    $capture = @($folder.GetFolder.Items()) |
        Where-Object { $_.Name -eq $env:WATCH_MTP_FILE } |
        Select-Object -First 1
    if (-not $capture) {
        $lastDiscoveryError = 'MTP capture not found'
        Start-Sleep -Milliseconds 500
        continue
    }
    break
} while ((Get-Date) -lt $discoveryDeadline)
if (-not $capture) { [Console]::Error.WriteLine($lastDiscoveryError); exit 6 }
$destination = $shell.Namespace($env:WATCH_MTP_DESTINATION)
if (-not $destination) { [Console]::Error.WriteLine('Destination not found'); exit 7 }
$destination.CopyHere($capture, 1044)
$target = Join-Path $env:WATCH_MTP_DESTINATION $env:WATCH_MTP_FILE
$deadline = (Get-Date).AddMilliseconds([double]$env:WATCH_MTP_TIMEOUT_MS)
$lastSize = -1
$stable = 0
do {
    Start-Sleep -Milliseconds 250
    if (Test-Path -LiteralPath $target) {
        $size = (Get-Item -LiteralPath $target).Length
        if ($size -ge 54 -and $size -eq $lastSize) { $stable++ } else { $stable = 0 }
        $lastSize = $size
        if ($stable -ge 2) { exit 0 }
    }
} while ((Get-Date) -lt $deadline)
[Console]::Error.WriteLine('MTP copy did not complete')
exit 8
"""
        destination_dir.mkdir(parents=True, exist_ok=True)
        self._run(
            script,
            timeout=timeout * 2.0 + 10.0,
            extra_env={
                "WATCH_MTP_DEVICE": self.device_name,
                "WATCH_MTP_FOLDER": MTP_FOLDER_NAME,
                "WATCH_MTP_FILE": file_name,
                "WATCH_MTP_DESTINATION": str(destination_dir.resolve()),
                "WATCH_MTP_TIMEOUT_MS": str(round(timeout * 1000)),
            },
        )
        target = destination_dir / file_name
        if not target.is_file():
            raise MtpScreenshotError(f"MTP copy did not create {target}")
        return target


class _BorrowedSessionTransport:
    """Adapt a running HardwareSerialSession without taking port ownership."""

    def __init__(self, serial_session: Any) -> None:
        self.serial_session = serial_session
        self._event_cursor = 0

    def open(self) -> None:
        if not bool(getattr(self.serial_session, "started", False)):
            raise MtpScreenshotError("shared hardware serial session is not started")
        self._event_cursor = int(self.serial_session.event_count)

    def write(self, data: bytes) -> int:
        payload = bytes(data)
        if not payload.endswith(b"\r\n"):
            raise MtpScreenshotError("shared serial write must end with CRLF")
        try:
            line = payload[:-2].decode("utf-8")
        except UnicodeDecodeError as exc:
            raise MtpScreenshotError("shared serial command is not UTF-8") from exc
        self.serial_session.write_shell_line(line)
        return len(payload)

    def read(self, _size: int) -> bytes:
        events = self.serial_session.events_since(self._event_cursor)
        self._event_cursor += len(events)
        if not events:
            return b""
        return b"\n".join(
            json.dumps(event, ensure_ascii=False, separators=(",", ":")).encode(
                "utf-8"
            )
            for event in events
        ) + b"\n"

    def close(self) -> None:
        # The RealDeviceSession owns the shared serial session.
        return


def _positive_timeout(name: str, value: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a positive finite number")
    result = float(value)
    if not math.isfinite(result) or result <= 0:
        raise ValueError(f"{name} must be a positive finite number")
    return result


def capture_filename(sequence: int) -> str:
    """返回固件与主机共同使用的精确截图文件名。"""

    if (
        isinstance(sequence, bool)
        or not isinstance(sequence, int)
        or not 1 <= sequence <= 0xFFFFFFFF
    ):
        raise ValueError("sequence must be an integer in 1..4294967295")
    return f"{MTP_CAPTURE_PREFIX}{sequence}{MTP_CAPTURE_SUFFIX}"


def _write_line(transport: Any, line: str) -> None:
    payload = (line + "\r\n").encode("utf-8")
    try:
        written = transport.write(payload)
    except Exception as exc:
        if isinstance(exc, SerialTransportError):
            raise
        raise SerialTransportError(f"serial write failed: {exc}") from exc
    if written is not None and written != len(payload):
        raise SerialTransportError(f"short serial write: {written}/{len(payload)}")


def _matching_terminal(text: str, sequence: int) -> dict[str, Any] | None:
    decoder = json.JSONDecoder()
    offset = 0
    while True:
        compact_start = text.find(BRIDGE_MARKER, offset)
        generic_start = text.find("{", offset)
        starts = [value for value in (compact_start, generic_start) if value >= 0]
        start = min(starts) if starts else -1
        if start < 0:
            return None
        try:
            event, consumed = decoder.raw_decode(text[start:])
        except json.JSONDecodeError:
            offset = start + 1
            continue
        offset = start + max(consumed, 1)
        if not isinstance(event, dict):
            continue
        if event.get("protocol") != "w30_test_bridge":
            continue
        if (
            event.get("request") == CAPTURE_REQUEST
            and str(event.get("seq")) == str(sequence)
            and event.get("type") == "screenshot_end"
        ):
            return event


def _wait_for_capture_terminal(
    transport: Any, *, sequence: int, timeout: float
) -> dict[str, Any] | None:
    deadline = time.monotonic() + timeout
    received = bytearray()
    while time.monotonic() < deadline:
        chunk = transport.read(4096)
        if chunk:
            received.extend(chunk)
            event = _matching_terminal(received.decode("utf-8", errors="replace"), sequence)
            if event is not None:
                status = str(event.get("status", "unknown")).lower()
                if status != "complete":
                    reason = event.get("reason", "unknown")
                    raise MtpScreenshotDeviceError(
                        f"capture {sequence} ended with {status}: {reason}"
                    )
                return event
        else:
            time.sleep(0.01)
    return None


def validate_bmp(
    path: str | os.PathLike[str],
    *,
    expected_width: int = EXPECTED_WIDTH,
    expected_height: int = EXPECTED_HEIGHT,
) -> BmpInfo:
    """严格校验当前固件生成的 top-down 24-bit BMP。"""

    source = Path(path)
    data = source.read_bytes()
    if len(data) < 54 or data[:2] != b"BM":
        raise MtpScreenshotValidationError("capture is not a BMP file")
    declared_size = struct.unpack_from("<I", data, 2)[0]
    pixel_offset = struct.unpack_from("<I", data, 10)[0]
    info_size = struct.unpack_from("<I", data, 14)[0]
    width, signed_height = struct.unpack_from("<ii", data, 18)
    planes, bits_per_pixel = struct.unpack_from("<HH", data, 26)
    compression = struct.unpack_from("<I", data, 30)[0]
    height = abs(signed_height)
    if declared_size != len(data):
        raise MtpScreenshotValidationError(
            f"BMP size mismatch: header={declared_size}, actual={len(data)}"
        )
    if info_size < 40 or planes != 1 or bits_per_pixel != 24 or compression != 0:
        raise MtpScreenshotValidationError("BMP must be uncompressed 24-bit RGB")
    if width != expected_width or height != expected_height:
        raise MtpScreenshotValidationError(
            f"unexpected BMP geometry: {width}x{height}, expected "
            f"{expected_width}x{expected_height}"
        )
    if signed_height >= 0:
        raise MtpScreenshotValidationError("BMP must be top-down")
    row_bytes = width * 3
    bmp_stride = (row_bytes + 3) & ~3
    required_size = pixel_offset + bmp_stride * height
    if pixel_offset < 54 or required_size != len(data):
        raise MtpScreenshotValidationError(
            f"invalid BMP pixel layout: required={required_size}, actual={len(data)}"
        )
    crc32 = 0
    for row in range(height):
        start = pixel_offset + row * bmp_stride
        crc32 = zlib.crc32(data[start : start + row_bytes], crc32)
    return BmpInfo(
        actual_size=len(data),
        declared_size=declared_size,
        width=width,
        height=height,
        top_down=True,
        bits_per_pixel=bits_per_pixel,
        payload_crc32=crc32 & 0xFFFFFFFF,
        sha256=hashlib.sha256(data).hexdigest().upper(),
    )


def _receipt_crc32(receipt: dict[str, Any]) -> int:
    value = receipt.get("crc32")
    if isinstance(value, int) and not isinstance(value, bool):
        result = value
    elif isinstance(value, str):
        text = value.strip().lower()
        if text.startswith("0x"):
            text = text[2:]
        try:
            result = int(text, 16)
        except ValueError as exc:
            raise MtpScreenshotValidationError(f"invalid receipt crc32: {value!r}") from exc
    else:
        raise MtpScreenshotValidationError(f"invalid receipt crc32: {value!r}")
    if not 0 <= result <= 0xFFFFFFFF:
        raise MtpScreenshotValidationError(f"invalid receipt crc32: {value!r}")
    return result


def _validate_receipt(
    receipt: dict[str, Any], bmp: BmpInfo, *, file_name: str
) -> None:
    expected = {
        "width": bmp.width,
        "height": bmp.height,
        "file_size": bmp.actual_size,
        "encoding": "bmp",
        "transport": "mtp",
        "path": f"{MTP_FOLDER_NAME}/{file_name}",
    }
    for field, value in expected.items():
        if receipt.get(field) != value:
            raise MtpScreenshotValidationError(
                f"receipt {field} mismatch: {receipt.get(field)!r} != {value!r}"
            )
    if _receipt_crc32(receipt) != bmp.payload_crc32:
        raise MtpScreenshotValidationError("receipt CRC32 does not match BMP pixels")


def capture_mtp_screenshot(
    output_path: str | os.PathLike[str],
    *,
    port: str = DEFAULT_PORT,
    baudrate: int = DEFAULT_BAUDRATE,
    sequence: int | None = None,
    capture_timeout: float = 20.0,
    usb_timeout: float = DEFAULT_USB_TIMEOUT,
    mtp_timeout: float = DEFAULT_MTP_TIMEOUT,
    overwrite: bool = False,
    mtp_system: MtpSystem | None = None,
    transport_factory: Callable[[], Any] | None = None,
) -> MtpScreenshotResult:
    """执行一次完整截图，并在任何中途失败后尽力重新打开 USB。"""

    capture_timeout = _positive_timeout("capture_timeout", capture_timeout)
    usb_timeout = _positive_timeout("usb_timeout", usb_timeout)
    mtp_timeout = _positive_timeout("mtp_timeout", mtp_timeout)
    if sequence is None:
        sequence = time.time_ns() % 2_147_483_646 + 1
    file_name = capture_filename(sequence)

    output = Path(output_path).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists() and not overwrite:
        raise MtpScreenshotError(f"output already exists: {output}")

    system = mtp_system or WindowsMtpSystem()
    factory = transport_factory or (
        lambda: Win32SerialTransport(
            port,
            baudrate=baudrate,
            dtr=False,
            rts=False,
        )
    )
    transport = factory()
    usb_closed = False
    receipt: dict[str, Any] | None = None
    try:
        transport.open()
        _write_line(transport, "dal_usb close")
        usb_closed = True
        system.wait_for_usb(present=False, timeout=usb_timeout)

        _write_line(
            transport,
            f'srv_quick_cmd send "TOP5STEP:{CAPTURE_COMMAND}:{sequence};"',
        )
        receipt = _wait_for_capture_terminal(
            transport, sequence=sequence, timeout=capture_timeout
        )

        _write_line(transport, "dal_usb open")
        try:
            system.wait_for_usb(present=True, timeout=usb_timeout)
        except MtpScreenshotTimeoutError:
            # ``dal_usb open`` is idempotent.  On the real watch the first
            # request can occasionally be missed while USB is re-enumerating,
            # so retry the command once before failing the capture.
            _write_line(transport, "dal_usb open")
            system.wait_for_usb(present=True, timeout=usb_timeout)
        usb_closed = False

        with tempfile.TemporaryDirectory(
            prefix="watch-mtp-", dir=str(output.parent)
        ) as temporary_dir:
            try:
                downloaded = system.copy_capture(
                    Path(temporary_dir), file_name=file_name, timeout=mtp_timeout
                )
            except MtpScreenshotError as exc:
                if "MTP capture not found" not in str(exc):
                    raise
                # Windows Shell can retain a stale MTP namespace immediately
                # after USB re-enumeration.  Start one fresh lookup for the
                # same sequence-specific file; never fall back to an older BMP.
                downloaded = system.copy_capture(
                    Path(temporary_dir), file_name=file_name, timeout=mtp_timeout
                )
            bmp = validate_bmp(downloaded)
            if receipt is not None:
                _validate_receipt(receipt, bmp, file_name=file_name)
            os.replace(downloaded, output)
    finally:
        if usb_closed:
            try:
                _write_line(transport, "dal_usb open")
                system.wait_for_usb(present=True, timeout=usb_timeout)
            except Exception:
                pass
        try:
            transport.close()
        except Exception:
            pass

    return MtpScreenshotResult(
        status="ok",
        sequence=sequence,
        output_path=str(output),
        actual_size=bmp.actual_size,
        width=bmp.width,
        height=bmp.height,
        bits_per_pixel=bmp.bits_per_pixel,
        top_down=bmp.top_down,
        payload_crc32=bmp.payload_crc32,
        sha256=bmp.sha256,
        receipt_verified=receipt is not None,
        device_uptime_ms=(receipt or {}).get("device_uptime_ms"),
        capture_duration_ms=(receipt or {}).get("capture_duration_ms"),
    )


class MtpCaptureProvider:
    """Capture new watch frames through a borrowed UART session and MTP."""

    def __init__(
        self,
        serial_session: Any,
        *,
        sequence_start: int | None = None,
        usb_timeout: float = DEFAULT_USB_TIMEOUT,
        mtp_timeout: float = DEFAULT_MTP_TIMEOUT,
        mtp_system: MtpSystem | None = None,
    ) -> None:
        if serial_session is None:
            raise ValueError("serial_session is required")
        if sequence_start is None:
            sequence_start = time.time_ns() % 2_147_483_646 + 1
        capture_filename(sequence_start)
        self.serial_session = serial_session
        self.usb_timeout = _positive_timeout("usb_timeout", usb_timeout)
        self.mtp_timeout = _positive_timeout("mtp_timeout", mtp_timeout)
        self.mtp_system = mtp_system or WindowsMtpSystem()
        self._next_sequence = sequence_start
        self._last_sequence: int | None = None
        self._closed = False
        self._lock = threading.Lock()

    @property
    def closed(self) -> bool:
        return self._closed

    def _allocate_sequence(self, after_sequence: int | None) -> int:
        if after_sequence is not None:
            if (
                isinstance(after_sequence, bool)
                or not isinstance(after_sequence, int)
                or not 0 <= after_sequence <= 0xFFFFFFFF
            ):
                raise ValueError("after_sequence must be a uint32 or None")
        floor = -1 if self._last_sequence is None else self._last_sequence
        if after_sequence is not None:
            floor = max(floor, after_sequence)
        sequence = max(self._next_sequence, floor + 1)
        if sequence > 0xFFFFFFFF:
            raise MtpScreenshotValidationError(
                "cannot allocate a capture sequence newer than after_sequence"
            )
        self._next_sequence = sequence + 1
        return sequence

    def capture(
        self,
        *,
        timeout: float,
        after_sequence: int | None = None,
    ) -> MtpCaptureFrame:
        timeout_value = _positive_timeout("timeout", timeout)
        with self._lock:
            if self._closed:
                raise MtpScreenshotError("MTP capture provider is closed")
            sequence = self._allocate_sequence(after_sequence)
            transport = _BorrowedSessionTransport(self.serial_session)
            with tempfile.TemporaryDirectory(prefix="watch-mtp-provider-") as root:
                output = Path(root) / capture_filename(sequence)
                result = capture_mtp_screenshot(
                    output,
                    sequence=sequence,
                    capture_timeout=timeout_value,
                    usb_timeout=self.usb_timeout,
                    mtp_timeout=self.mtp_timeout,
                    overwrite=False,
                    mtp_system=self.mtp_system,
                    transport_factory=lambda: transport,
                )
                bmp = output.read_bytes()

            timestamp = (
                result.device_uptime_ms / 1000.0
                if result.device_uptime_ms is not None
                else time.monotonic()
            )
            metadata = MtpCaptureMetadata(
                sequence=result.sequence,
                timestamp=timestamp,
                width=result.width,
                height=result.height,
                pixel_format="bgr888",
                data_size=result.width * result.height * 3,
                stride=result.width * 3,
                source="watch_display",
                transport="mtp",
                payload_crc32=result.payload_crc32,
                encoding="bmp",
                file_size=result.actual_size,
                receipt_verified=result.receipt_verified,
                mtp_file_name=capture_filename(result.sequence),
                device_uptime_ms=result.device_uptime_ms,
                capture_duration_ms=result.capture_duration_ms,
                pixel_source="vde_lcd_composite",
            )
            self._last_sequence = result.sequence
            return MtpCaptureFrame(bmp=bmp, metadata=metadata)

    def close(self) -> None:
        self._closed = True


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="watch-mtp-screenshot",
        description="通过后台 UART 控制和 Windows MTP 获取一张手表截图",
    )
    parser.add_argument("--port", default=DEFAULT_PORT, help="调试串口，默认 COM7")
    parser.add_argument("--baudrate", type=int, default=DEFAULT_BAUDRATE)
    parser.add_argument("--output", required=True, help="输出 BMP 路径")
    parser.add_argument("--sequence", type=int, help="可选的 uint32 请求序号")
    parser.add_argument("--capture-timeout", type=float, default=20.0)
    parser.add_argument("--usb-timeout", type=float, default=DEFAULT_USB_TIMEOUT)
    parser.add_argument("--mtp-timeout", type=float, default=DEFAULT_MTP_TIMEOUT)
    parser.add_argument("--overwrite", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = capture_mtp_screenshot(
            args.output,
            port=args.port,
            baudrate=args.baudrate,
            sequence=args.sequence,
            capture_timeout=args.capture_timeout,
            usb_timeout=args.usb_timeout,
            mtp_timeout=args.mtp_timeout,
            overwrite=args.overwrite,
        )
    except (MtpScreenshotError, SerialTransportError, OSError, ValueError) as exc:
        print(
            json.dumps(
                {"status": "error", "error": str(exc)},
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            file=sys.stderr,
        )
        return 1
    print(json.dumps(result.to_dict(), ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "BmpInfo",
    "MtpCaptureFrame",
    "MtpCaptureMetadata",
    "MtpCaptureProvider",
    "MtpScreenshotDeviceError",
    "MtpScreenshotError",
    "MtpScreenshotResult",
    "MtpScreenshotTimeoutError",
    "MtpScreenshotValidationError",
    "WindowsMtpSystem",
    "capture_filename",
    "capture_mtp_screenshot",
    "main",
    "validate_bmp",
]
