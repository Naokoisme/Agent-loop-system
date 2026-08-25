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
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
import xml.etree.ElementTree as ET
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
    write_shell_wire,
)


USB_INSTANCE_PATTERN = "VID_301A&PID_6808"
MTP_DEVICE_NAME = "ZORA"
MTP_STORAGE_NAME = "storage"
MTP_FOLDER_NAME = "download"
MTP_CAPTURE_PREFIX = "agent_capture_"
MTP_CAPTURE_SUFFIX = ".bmp"
CAPTURE_REQUEST = "screenshot_capture_file"
CAPTURE_COMMAND = "SCREENSHOT_CAPTURE_FILE"
EXPECTED_WIDTH = 410
EXPECTED_HEIGHT = 502
PROJECT_SCREEN_GEOMETRY = {
    "6202_W5230": (EXPECTED_WIDTH, EXPECTED_HEIGHT),
    "6204_W5230": (466, 466),
}
DEFAULT_USB_TIMEOUT = 30.0
DEFAULT_MTP_TIMEOUT = 30.0
DEFAULT_CAPTURE_ACCEPT_TIMEOUT = 2.0
# Including CRLF, sequence 9_999_999 makes the capture shell wire exactly
# 64 bytes.  Larger values cannot be sent atomically through the current watch
# shell and must never be split into independent transport writes.
MAX_SHELL_SAFE_CAPTURE_SEQUENCE = 9_999_999
_CLIXML_PREFIX = "#< CLIXML"
_CLIXML_ESCAPE_RE = re.compile(r"_x([0-9A-Fa-f]{4})_")
_MAX_POWERSHELL_ERROR_LENGTH = 2_000
_POWERSHELL_PREAMBLE = r"""
$ProgressPreference = 'SilentlyContinue'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$OutputEncoding = [Console]::OutputEncoding
""".strip()


class MtpScreenshotError(RuntimeError):
    """截图流程失败。"""


class MtpScreenshotTimeoutError(MtpScreenshotError, TimeoutError):
    """USB、截图或 MTP 操作超时。"""


class MtpUsbRestoreError(MtpScreenshotTimeoutError):
    """USB open retries were exhausted, with structured transition evidence."""

    def __init__(self, message: str, attempts: list[dict[str, Any]]) -> None:
        self.attempts = [dict(item) for item in attempts]
        serialized = json.dumps(
            self.attempts,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        super().__init__(f"{message}; attempts={serialized}")


class MtpScreenshotDeviceError(MtpScreenshotError):
    """固件明确拒绝或终止截图。"""


class MtpScreenshotCommandError(MtpScreenshotError):
    """截图命令没有被固件接收。"""


class MtpScreenshotValidationError(MtpScreenshotError, ValueError):
    """下载文件或固件回执不符合协议。"""


def _expected_geometry(
    expected_width: int | None, expected_height: int | None
) -> tuple[int, int]:
    if expected_width is None and expected_height is None:
        project = os.environ.get("W30_HARDWARE_PROJECT", "6202_W5230").strip()
        try:
            return PROJECT_SCREEN_GEOMETRY[project]
        except KeyError as exc:
            raise ValueError(
                f"未知真机项目 {project or '未设置'}，请显式提供截图宽高"
            ) from exc
    if expected_width is None or expected_height is None:
        raise ValueError("expected_width and expected_height must be set together")
    if (
        not isinstance(expected_width, int)
        or isinstance(expected_width, bool)
        or not isinstance(expected_height, int)
        or isinstance(expected_height, bool)
        or expected_width <= 0
        or expected_height <= 0
    ):
        raise ValueError("expected screenshot geometry must be positive integers")
    return int(expected_width), int(expected_height)


def _decode_clixml_escapes(value: str) -> str:
    return _CLIXML_ESCAPE_RE.sub(
        lambda match: chr(int(match.group(1), 16)),
        value,
    )


def _xml_local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _powershell_error_detail(value: str | None) -> str:
    """Return useful PowerShell diagnostics without serialized progress records."""

    text = (value or "").strip()
    if not text:
        return ""

    marker = text.find(_CLIXML_PREFIX)
    if marker < 0:
        detail = text
    else:
        messages: list[str] = []
        plain_prefix = text[:marker].strip()
        if plain_prefix:
            messages.append(plain_prefix)

        xml_payload = text[marker + len(_CLIXML_PREFIX) :].strip()
        try:
            root = ET.fromstring(xml_payload)
        except ET.ParseError:
            if not messages:
                messages.append("PowerShell returned unreadable encoded diagnostics")
        else:
            for item in root:
                if str(item.attrib.get("S", "")).casefold() == "progress":
                    continue

                item_messages: list[str] = []
                if _xml_local_name(str(item.tag)) == "S" and item.text:
                    item_messages.append(item.text)
                for element in item.iter():
                    if element is item or not element.text:
                        continue
                    stream = str(element.attrib.get("S", "")).casefold()
                    if _xml_local_name(str(element.tag)) == "ToString" or stream == "error":
                        item_messages.append(element.text)
                if not item_messages:
                    combined = " ".join(
                        fragment.strip()
                        for fragment in item.itertext()
                        if fragment.strip()
                    )
                    if combined:
                        item_messages.append(combined)
                messages.extend(item_messages)

        decoded: list[str] = []
        seen: set[str] = set()
        for message in messages:
            normalized = _decode_clixml_escapes(message).strip()
            if normalized and normalized not in seen:
                seen.add(normalized)
                decoded.append(normalized)
        detail = "\n".join(decoded)

    if len(detail) > _MAX_POWERSHELL_ERROR_LENGTH:
        detail = f"{detail[:_MAX_POWERSHELL_ERROR_LENGTH]}..."
    return detail


class MtpSystem(Protocol):
    """可注入的 Windows USB/MTP 边界。"""

    def inspect_usb_devices(self, *, timeout: float = 5.0) -> list[dict[str, Any]]: ...

    def probe_namespace(self, *, timeout: float = 10.0) -> dict[str, Any]: ...

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
        wrapped_script = f"{_POWERSHELL_PREAMBLE}\n{script}"
        encoded = base64.b64encode(wrapped_script.encode("utf-16le")).decode("ascii")
        env = os.environ.copy()
        if extra_env:
            env.update(extra_env)
        try:
            completed = self._runner(
                [
                    self._powershell,
                    "-NoLogo",
                    "-NoProfile",
                    "-NonInteractive",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-OutputFormat",
                    "Text",
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
            detail = _powershell_error_detail(completed.stderr)
            if not detail:
                detail = _powershell_error_detail(completed.stdout)
            if not detail:
                detail = (
                    f"PowerShell exited with code {completed.returncode} "
                    "without diagnostics"
                )
            raise MtpScreenshotError(f"Windows MTP operation failed: {detail}")
        return completed.stdout.strip()

    def inspect_usb_devices(
        self,
        *,
        timeout: float = 5.0,
    ) -> list[dict[str, Any]]:
        """Return the currently present watch PnP instances without changing USB."""

        script = r"""
$pattern = $env:WATCH_USB_PATTERN
if (-not (Get-Command Get-PnpDevice -ErrorAction SilentlyContinue)) {
    [Console]::Error.WriteLine('Get-PnpDevice is unavailable')
    exit 6
}
$items = @(
    Get-PnpDevice -PresentOnly -ErrorAction SilentlyContinue |
    Where-Object { $_.InstanceId -match $pattern } |
    ForEach-Object {
        [PSCustomObject]@{
            instance_id = [string]$_.InstanceId
            status = [string]$_.Status
            friendly_name = [string]$_.FriendlyName
            class = [string]$_.Class
        }
    }
)
ConvertTo-Json -InputObject $items -Compress -Depth 3
"""
        output = self._run(
            script,
            timeout=timeout,
            extra_env={"WATCH_USB_PATTERN": self.usb_instance_pattern},
        )
        if not output:
            return []
        try:
            payload = json.loads(output)
        except json.JSONDecodeError as exc:
            raise MtpScreenshotError(
                "Windows PnP probe returned invalid JSON"
            ) from exc
        if isinstance(payload, dict):
            payload = [payload]
        if not isinstance(payload, list):
            raise MtpScreenshotError("Windows PnP probe returned an invalid payload")
        return [dict(item) for item in payload if isinstance(item, dict)]

    def probe_namespace(self, *, timeout: float = 10.0) -> dict[str, Any]:
        """Read ``ZORA -> <storage volume> -> download`` via a fresh Shell namespace.

        Older firmware exposed the volume as ``storage``.  Current 6202 builds
        use a product label such as ``ZORA MTP Storage Volume`` instead.  Keep
        the legacy exact-name preference, then accept the single volume that
        contains the expected folder so a firmware label change does not make
        an otherwise healthy MTP link fail preflight.
        """

        script = r"""
$deadline = (Get-Date).AddMilliseconds([double]$env:WATCH_MTP_TIMEOUT_MS)
$lastError = 'MTP namespace not ready'
do {
    $shell = New-Object -ComObject Shell.Application
    $thisPc = $shell.Namespace(17)
    if (-not $thisPc) {
        $lastError = 'This PC namespace not found'
    } else {
        $devices = @($thisPc.Items() | Where-Object { $_.Name -eq $env:WATCH_MTP_DEVICE })
        if ($devices.Count -ne 1) {
            $lastError = "MTP device count was $($devices.Count), expected 1"
        } else {
            $device = $devices[0]
            $storageItems = @($device.GetFolder.Items())
            $storages = @($storageItems | Where-Object { $_.Name -eq $env:WATCH_MTP_STORAGE })
            if ($storages.Count -eq 0) {
                $storages = @(
                    $storageItems | Where-Object {
                        try {
                            @($_.GetFolder.Items() | Where-Object {
                                $_.Name -eq $env:WATCH_MTP_FOLDER
                            }).Count -eq 1
                        } catch {
                            $false
                        }
                    }
                )
            }
            if ($storages.Count -ne 1) {
                $names = @($storageItems | ForEach-Object { [string]$_.Name }) -join ', '
                $lastError = "MTP storage count was $($storages.Count), expected 1; available=[$names]"
            } else {
                $storage = $storages[0]
                $folders = @($storage.GetFolder.Items() | Where-Object { $_.Name -eq $env:WATCH_MTP_FOLDER })
                if ($folders.Count -eq 1) {
                    [PSCustomObject]@{
                        device = [string]$device.Name
                        storage = [string]$storage.Name
                        folder = [string]$folders[0].Name
                    } | ConvertTo-Json -Compress
                    exit 0
                }
                $lastError = "MTP folder count was $($folders.Count), expected 1"
            }
        }
    }
    Start-Sleep -Milliseconds 500
} while ((Get-Date) -lt $deadline)
[Console]::Error.WriteLine($lastError)
exit 5
"""
        output = self._run(
            script,
            timeout=timeout + 10.0,
            extra_env={
                "WATCH_MTP_DEVICE": self.device_name,
                "WATCH_MTP_STORAGE": MTP_STORAGE_NAME,
                "WATCH_MTP_FOLDER": MTP_FOLDER_NAME,
                "WATCH_MTP_TIMEOUT_MS": str(round(timeout * 1000)),
            },
        )
        try:
            payload = json.loads(output)
        except json.JSONDecodeError as exc:
            raise MtpScreenshotError(
                "Windows MTP namespace probe returned invalid JSON"
            ) from exc
        if not isinstance(payload, dict):
            raise MtpScreenshotError(
                "Windows MTP namespace probe returned an invalid payload"
            )
        return dict(payload)

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
    $storageItems = @($device.GetFolder.Items())
    $storage = @($storageItems) |
        Where-Object { $_.Name -eq $env:WATCH_MTP_STORAGE } |
        Select-Object -First 1
    if (-not $storage) {
        $storage = @(
            $storageItems | Where-Object {
                try {
                    @($_.GetFolder.Items() | Where-Object {
                        $_.Name -eq $env:WATCH_MTP_FOLDER
                    }).Count -eq 1
                } catch {
                    $false
                }
            }
        ) | Select-Object -First 1
    }
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
        Where-Object {
            $_.Name -eq $env:WATCH_MTP_FILE -or
            $_.ExtendedProperty('System.FileName') -eq $env:WATCH_MTP_FILE
        } |
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
                "WATCH_MTP_STORAGE": MTP_STORAGE_NAME,
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
        self.write_shell_line(line)
        return len(payload)

    def write_shell_line(self, line: str) -> None:
        self.serial_session.write_shell_line(line)

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


def _default_capture_sequence() -> int:
    return time.time_ns() % MAX_SHELL_SAFE_CAPTURE_SEQUENCE + 1


def _validate_shell_safe_capture_sequence(sequence: int) -> int:
    capture_filename(sequence)
    if sequence > MAX_SHELL_SAFE_CAPTURE_SEQUENCE:
        raise MtpScreenshotValidationError(
            f"capture sequence {sequence} exceeds the shell-safe maximum "
            f"{MAX_SHELL_SAFE_CAPTURE_SEQUENCE}"
        )
    return sequence


def _write_line(transport: Any, line: str) -> None:
    shared_writer = getattr(transport, "write_shell_line", None)
    if callable(shared_writer):
        shared_writer(line)
        return
    payload = (line + "\r\n").encode("utf-8")
    try:
        write_shell_wire(transport.write, payload)
    except Exception as exc:
        if isinstance(exc, SerialTransportError):
            raise
        raise SerialTransportError(f"serial write failed: {exc}") from exc


def summarize_usb_devices(devices: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep useful PnP evidence while redacting machine-specific instance IDs."""

    summarized: list[dict[str, Any]] = []
    for item in devices:
        instance = str(item.get("instance_id") or item.get("InstanceId") or "")
        digest = hashlib.sha256(
            instance.encode("utf-8", errors="replace")
        ).hexdigest()[:12]
        summarized.append({
            "instance_id_hash": digest if instance else "",
            "status": str(item.get("status") or item.get("Status") or "")[:80],
            "friendly_name": str(
                item.get("friendly_name") or item.get("FriendlyName") or ""
            )[:200],
            "class": str(item.get("class") or item.get("Class") or "")[:80],
        })
    return summarized


def _record_usb_instances(
    record: dict[str, Any],
    system: MtpSystem,
    *,
    timeout: float,
) -> None:
    probe = getattr(system, "inspect_usb_devices", None)
    if not callable(probe):
        record["pnp_instances"] = []
        record["pnp_probe_status"] = "unavailable"
        return
    try:
        devices = list(probe(timeout=min(timeout, 5.0)))
    except Exception as exc:
        record["pnp_instances"] = []
        record["pnp_probe_status"] = "error"
        record["pnp_probe_error"] = str(exc)[:2000]
    else:
        record["pnp_instances"] = summarize_usb_devices(devices)
        record["pnp_probe_status"] = "ok"


def restore_usb_device(
    write_line: Callable[[str], None],
    system: MtpSystem,
    *,
    usb_timeout: float,
    namespace_timeout: float | None = None,
    attempts: int = 2,
) -> list[dict[str, Any]]:
    """Open USB, wait for PnP, then force one fresh WPD namespace lookup.

    ``dal_usb open`` is idempotent, so one retry is permitted.  Every attempt
    records whether it failed before PnP appeared or while the WPD namespace
    was still unavailable; callers can persist this evidence verbatim.
    """

    if attempts < 1:
        raise ValueError("attempts must be at least 1")
    usb_timeout = _positive_timeout("usb_timeout", usb_timeout)
    if namespace_timeout is not None:
        namespace_timeout = _positive_timeout(
            "namespace_timeout", namespace_timeout
        )

    diagnostics: list[dict[str, Any]] = []
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        started = time.monotonic()
        record: dict[str, Any] = {
            "attempt": attempt,
            "pnp_status": "pending",
            "wpd_status": "unchecked",
        }
        try:
            write_line("dal_usb open")
            system.wait_for_usb(present=True, timeout=usb_timeout)
            record["pnp_status"] = "present"
            _record_usb_instances(record, system, timeout=usb_timeout)
            probe = getattr(system, "probe_namespace", None)
            if namespace_timeout is not None:
                if not callable(probe):
                    raise MtpScreenshotError(
                        "MTP namespace probe is unavailable"
                    )
                record["namespace"] = dict(
                    probe(timeout=namespace_timeout)
                )
                record["wpd_status"] = "ready"
            record["status"] = "ready"
            record["elapsed_ms"] = round((time.monotonic() - started) * 1000)
            diagnostics.append(record)
            return diagnostics
        except Exception as exc:
            last_error = exc
            if record["pnp_status"] == "pending":
                record["pnp_status"] = "not_present"
                record["stage"] = "pnp"
                _record_usb_instances(record, system, timeout=usb_timeout)
            else:
                record["wpd_status"] = "not_ready"
                record["stage"] = "wpd"
            record["status"] = "failed"
            record["error_type"] = type(exc).__name__
            record["error"] = str(exc)[:2000]
            record["elapsed_ms"] = round((time.monotonic() - started) * 1000)
            diagnostics.append(record)

    stage = str(diagnostics[-1].get("stage") or "pnp")
    message = (
        "dal_usb open restored Windows PnP but the MTP namespace stayed unavailable"
        if stage == "wpd"
        else "dal_usb open did not restore the watch USB/PnP device"
    )
    raise MtpUsbRestoreError(message, diagnostics) from last_error


def _capture_response_state(
    text: str, sequence: int
) -> tuple[bool, dict[str, Any] | None, dict[str, Any] | None]:
    decoder = json.JSONDecoder()
    offset = 0
    accepted = False
    terminal: dict[str, Any] | None = None
    command_failure: dict[str, Any] | None = None
    while True:
        compact_start = text.find(BRIDGE_MARKER, offset)
        generic_start = text.find("{", offset)
        starts = [value for value in (compact_start, generic_start) if value >= 0]
        start = min(starts) if starts else -1
        if start < 0:
            return accepted, terminal, command_failure
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
        if event.get("request") != CAPTURE_REQUEST:
            continue
        event_type = str(event.get("type", "")).lower()
        status = str(event.get("status", "unknown")).lower()
        if event_type == "command_result":
            if status == "accepted":
                accepted = True
            else:
                command_failure = event
        elif event_type == "screenshot_end" and str(event.get("seq")) == str(
            sequence
        ):
            terminal = event


def _wait_for_capture_terminal(
    transport: Any, *, sequence: int, timeout: float
) -> dict[str, Any] | None:
    started = time.monotonic()
    deadline = started + timeout
    acceptance_deadline = started + min(timeout, DEFAULT_CAPTURE_ACCEPT_TIMEOUT)
    received = bytearray()
    command_accepted = False
    while time.monotonic() < deadline:
        chunk = transport.read(4096)
        if chunk:
            received.extend(chunk)
            accepted, terminal, command_failure = _capture_response_state(
                received.decode("utf-8", errors="replace"), sequence
            )
            command_accepted = command_accepted or accepted
            if command_failure is not None:
                status = str(command_failure.get("status", "unknown")).lower()
                reason = command_failure.get("reason", "unknown")
                raise MtpScreenshotDeviceError(
                    f"capture {sequence} command ended with {status}: {reason}"
                )
            if terminal is not None:
                status = str(terminal.get("status", "unknown")).lower()
                if status != "complete":
                    reason = terminal.get("reason", "unknown")
                    raise MtpScreenshotDeviceError(
                        f"capture {sequence} ended with {status}: {reason}"
                    )
                return terminal
        if not command_accepted and time.monotonic() >= acceptance_deadline:
            raise MtpScreenshotCommandError(
                f"capture {sequence} command was not accepted within "
                f"{min(timeout, DEFAULT_CAPTURE_ACCEPT_TIMEOUT):g}s; "
                "UART shell input may have been truncated"
            )
        if not chunk:
            remaining = deadline - time.monotonic()
            if remaining > 0:
                time.sleep(min(0.01, remaining))
    if not command_accepted:
        raise MtpScreenshotCommandError(
            f"capture {sequence} command was not accepted within {timeout:g}s; "
            "UART shell input may have been truncated"
        )
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
    expected_width: int | None = None,
    expected_height: int | None = None,
    mtp_system: MtpSystem | None = None,
    transport_factory: Callable[[], Any] | None = None,
) -> MtpScreenshotResult:
    """执行一次完整截图，并在任何中途失败后尽力重新打开 USB。"""

    capture_timeout = _positive_timeout("capture_timeout", capture_timeout)
    usb_timeout = _positive_timeout("usb_timeout", usb_timeout)
    mtp_timeout = _positive_timeout("mtp_timeout", mtp_timeout)
    expected_width, expected_height = _expected_geometry(
        expected_width, expected_height
    )
    if sequence is None:
        sequence = _default_capture_sequence()
    sequence = _validate_shell_safe_capture_sequence(sequence)
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
    usb_restore_attempted = False
    receipt: dict[str, Any] | None = None
    try:
        transport.open()
        # Never close an unproven USB path.  This keeps a missing/ambiguous
        # host environment from being turned into a destructive recovery loop.
        system.wait_for_usb(present=True, timeout=usb_timeout)
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

        usb_restore_attempted = True
        restore_usb_device(
            lambda line: _write_line(transport, line),
            system,
            usb_timeout=usb_timeout,
            namespace_timeout=mtp_timeout,
        )
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
            bmp = validate_bmp(
                downloaded,
                expected_width=expected_width,
                expected_height=expected_height,
            )
            if receipt is not None:
                _validate_receipt(receipt, bmp, file_name=file_name)
            os.replace(downloaded, output)
    finally:
        pending_error = sys.exc_info()[1]
        cleanup_error: Exception | None = None
        if usb_closed and not usb_restore_attempted:
            try:
                restore_usb_device(
                    lambda line: _write_line(transport, line),
                    system,
                    usb_timeout=usb_timeout,
                    namespace_timeout=mtp_timeout,
                )
            except Exception as exc:
                cleanup_error = exc
        try:
            transport.close()
        except Exception:
            pass
        if cleanup_error is not None:
            if pending_error is not None:
                pending_error.add_note(
                    f"USB cleanup also failed: {cleanup_error}"
                )
            else:
                raise cleanup_error

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
        expected_width: int | None = None,
        expected_height: int | None = None,
        mtp_system: MtpSystem | None = None,
    ) -> None:
        if serial_session is None:
            raise ValueError("serial_session is required")
        if sequence_start is None:
            sequence_start = _default_capture_sequence()
        sequence_start = _validate_shell_safe_capture_sequence(sequence_start)
        self.serial_session = serial_session
        self.usb_timeout = _positive_timeout("usb_timeout", usb_timeout)
        self.mtp_timeout = _positive_timeout("mtp_timeout", mtp_timeout)
        self.expected_width, self.expected_height = _expected_geometry(
            expected_width, expected_height
        )
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
                or not 0 <= after_sequence <= MAX_SHELL_SAFE_CAPTURE_SEQUENCE
            ):
                raise ValueError(
                    "after_sequence must be an integer in "
                    f"0..{MAX_SHELL_SAFE_CAPTURE_SEQUENCE} or None"
                )
        floor = -1 if self._last_sequence is None else self._last_sequence
        if after_sequence is not None:
            floor = max(floor, after_sequence)
        sequence = max(self._next_sequence, floor + 1)
        if sequence > MAX_SHELL_SAFE_CAPTURE_SEQUENCE:
            raise MtpScreenshotValidationError(
                "cannot allocate a shell-safe capture sequence newer than "
                "after_sequence"
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
                    expected_width=self.expected_width,
                    expected_height=self.expected_height,
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
    parser.add_argument(
        "--sequence",
        type=int,
        help=f"可选请求序号，范围 1..{MAX_SHELL_SAFE_CAPTURE_SEQUENCE}",
    )
    parser.add_argument("--capture-timeout", type=float, default=20.0)
    parser.add_argument("--usb-timeout", type=float, default=DEFAULT_USB_TIMEOUT)
    parser.add_argument("--mtp-timeout", type=float, default=DEFAULT_MTP_TIMEOUT)
    parser.add_argument("--expected-width", type=int)
    parser.add_argument("--expected-height", type=int)
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
            expected_width=args.expected_width,
            expected_height=args.expected_height,
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
    "MAX_SHELL_SAFE_CAPTURE_SEQUENCE",
    "MtpCaptureFrame",
    "MtpCaptureMetadata",
    "MtpCaptureProvider",
    "MtpScreenshotCommandError",
    "MtpScreenshotDeviceError",
    "MtpScreenshotError",
    "MtpScreenshotResult",
    "MtpScreenshotTimeoutError",
    "MtpUsbRestoreError",
    "MtpScreenshotValidationError",
    "WindowsMtpSystem",
    "capture_filename",
    "capture_mtp_screenshot",
    "main",
    "restore_usb_device",
    "summarize_usb_devices",
    "validate_bmp",
]
