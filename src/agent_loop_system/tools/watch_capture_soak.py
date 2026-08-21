"""Unattended soak runner for direct watch screenshots.

The runner borrows both the capture provider and the serial session.  It never
closes either dependency; their lifetime remains the caller's responsibility.
Every attempt is written and flushed to JSONL before the next attempt starts,
and a machine-readable summary is written when the requested run completes.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import time
import zlib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Protocol, TextIO

from agent_loop_system.tools.watch_capture import (
    WatchCaptureDeviceError,
    WatchCaptureProtocolError,
    WatchCaptureProvider,
    WatchCaptureSequenceError,
    WatchCaptureTimeoutError,
)


MIN_CAPTURE_COUNT = 100
DAY_SECONDS = 24 * 60 * 60
_UINT32_MAX = 0xFFFFFFFF
_HOST_TIMEZONE = timezone(timedelta(hours=8), name="Asia/Shanghai")

_LOG_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("hard_fault", re.compile(r"hard\s*fault", re.IGNORECASE)),
    ("watchdog", re.compile(r"\bwatchdog\b", re.IGNORECASE)),
    ("assertion", re.compile(r"\bassert(?:ion)?\b", re.IGNORECASE)),
    ("fatal", re.compile(r"\bfatal(?:\s+error)?\b", re.IGNORECASE)),
    ("reboot", re.compile(r"\breboot(?:ing|ed)?\b", re.IGNORECASE)),
)
_ANY_BACKGROUND_LOG_RE = re.compile(
    r"(?:hard\s*fault|\bassert(?:ion)?\b|\bfatal(?:\s+error)?\b|"
    r"\bwatchdog\b|\breboot(?:ing|ed)?\b)",
    re.IGNORECASE,
)


class SoakClock(Protocol):
    """Clock surface used by the runner and deterministic tests."""

    def time(self) -> float: ...

    def monotonic(self) -> float: ...

    def sleep(self, seconds: float) -> None: ...


class SystemClock:
    """Production clock backed by :mod:`time`."""

    @staticmethod
    def time() -> float:
        return time.time()

    @staticmethod
    def monotonic() -> float:
        return time.monotonic()

    @staticmethod
    def sleep(seconds: float) -> None:
        time.sleep(seconds)


class _SoakSequenceError(WatchCaptureProtocolError):
    """A provider returned a frame that was not strictly newer."""


class _SerialLogMonitor:
    """Read only serial log entries that arrive after construction."""

    def __init__(self, serial_session: Any | None) -> None:
        self._serial_session = serial_session
        self._source: str | None = None
        self._cursor = 0

        if serial_session is None:
            return
        background_reader = getattr(serial_session, "background_errors_since", None)
        background_count = getattr(serial_session, "background_error_count", None)
        if (
            callable(background_reader)
            and isinstance(background_count, int)
            and not isinstance(background_count, bool)
            and background_count >= 0
        ):
            self._source = "background_errors"
            # This is an absolute stream cursor.  The retained error cache may
            # already have been trimmed, so its current length is not a valid
            # substitute for the session's monotonic total.
            self._cursor = background_count
            return
        lines_reader = getattr(serial_session, "lines_since", None)
        if callable(lines_reader):
            self._source = "lines"
            line_count = getattr(serial_session, "line_count", None)
            if isinstance(line_count, int) and not isinstance(line_count, bool):
                self._cursor = line_count
            else:
                self._cursor = len(lines_reader(0))

    @property
    def enabled(self) -> bool:
        return self._source is not None

    def poll(self) -> list[str]:
        if self._source is None or self._serial_session is None:
            return []
        if self._source == "background_errors":
            values = self._serial_session.background_errors_since(self._cursor)
            lines = [str(value) for value in values]
        else:
            values = self._serial_session.lines_since(self._cursor)
            lines = [
                str(value)
                for value in values
                if _ANY_BACKGROUND_LOG_RE.search(str(value))
            ]
        self._cursor += len(values)
        return lines


def _finite_number(value: object, name: str, *, positive: bool) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        qualifier = "positive" if positive else "non-negative"
        raise ValueError(f"{name} must be a {qualifier} finite number")
    result = float(value)
    invalid_sign = result <= 0 if positive else result < 0
    if not math.isfinite(result) or invalid_sign:
        qualifier = "positive" if positive else "non-negative"
        raise ValueError(f"{name} must be a {qualifier} finite number")
    return result


def _host_time(epoch_seconds: float) -> str:
    return datetime.fromtimestamp(epoch_seconds, tz=_HOST_TIMEZONE).isoformat(
        timespec="milliseconds"
    )


def _required_uint32(value: object, name: str) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 0 <= value <= _UINT32_MAX
    ):
        raise WatchCaptureProtocolError(f"{name} must be a uint32, got {value!r}")
    return value


def _optional_non_negative_int(value: object, name: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise WatchCaptureProtocolError(
            f"{name} must be a non-negative integer or null, got {value!r}"
        )
    return value


def _percentile(values: list[float], fraction: float) -> float | None:
    """Return a linearly interpolated percentile rounded to microseconds."""

    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return round(ordered[0], 3)
    position = (len(ordered) - 1) * fraction
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return round(ordered[lower], 3)
    weight = position - lower
    result = ordered[lower] + (ordered[upper] - ordered[lower]) * weight
    return round(result, 3)


def _latency_summary(values: list[float]) -> dict[str, int | float | None]:
    return {
        "sample_count": len(values),
        "p50": _percentile(values, 0.50),
        "p95": _percentile(values, 0.95),
        "p99": _percentile(values, 0.99),
    }


def _classify_exception(error: Exception) -> str:
    if isinstance(error, (WatchCaptureTimeoutError, TimeoutError)):
        return "timeout"
    if isinstance(error, WatchCaptureDeviceError):
        return "device_error"
    if isinstance(error, (WatchCaptureProtocolError, WatchCaptureSequenceError)):
        detail = str(error).lower()
        if "crc" in detail or "checksum" in detail:
            return "crc"
        return "protocol"
    return "unexpected"


def _classify_log(line: str) -> list[str]:
    categories = [name for name, pattern in _LOG_PATTERNS if pattern.search(line)]
    return categories or ["other"]


def _write_jsonl(stream: TextIO, value: dict[str, Any]) -> None:
    stream.write(json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n")
    stream.flush()


class WatchCaptureSoak:
    """Run repeated, checksum-verified watch captures.

    ``provider`` and ``serial_session`` are borrowed.  :meth:`close` only
    releases a currently open JSONL stream and never calls ``close``/``stop``
    on either dependency.
    """

    def __init__(
        self,
        provider: Any,
        *,
        serial_session: Any | None = None,
        clock: SoakClock | None = None,
    ) -> None:
        if provider is None:
            raise ValueError("provider is required")
        self.provider = provider
        self.serial_session = (
            serial_session
            if serial_session is not None
            else getattr(provider, "serial_session", None)
        )
        self.clock: SoakClock = clock or SystemClock()
        self._stream: TextIO | None = None
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed

    def close(self) -> None:
        """Close only this runner's output stream, never borrowed hardware."""

        stream, self._stream = self._stream, None
        if stream is not None and not stream.closed:
            stream.close()
        self._closed = True

    def __enter__(self) -> "WatchCaptureSoak":
        if self._closed:
            raise RuntimeError("watch capture soak runner is closed")
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        self.close()

    def run(
        self,
        *,
        jsonl_path: str | Path,
        summary_path: str | Path,
        count: int | None = None,
        duration: float | None = None,
        capture_timeout: float = 12.0,
        interval: float = 0.0,
    ) -> dict[str, Any]:
        """Run until ``count`` captures were attempted or ``duration`` elapsed.

        Exactly one stopping mode is required.  Count mode deliberately starts
        at 100 attempts so a short smoke test cannot be mistaken for a soak.
        ``duration`` is expressed in seconds; pass :data:`DAY_SECONDS` for 24h.
        Per-attempt capture exceptions are recorded and classified, then the
        next request continues with a newer sequence floor.
        """

        if self._closed:
            raise RuntimeError("watch capture soak runner is closed")
        if (count is None) == (duration is None):
            raise ValueError("exactly one of count or duration is required")
        if count is not None:
            if isinstance(count, bool) or not isinstance(count, int):
                raise ValueError("count must be an integer")
            if count < MIN_CAPTURE_COUNT:
                raise ValueError(f"count must be at least {MIN_CAPTURE_COUNT}")
        duration_value = (
            None
            if duration is None
            else _finite_number(duration, "duration", positive=True)
        )
        timeout_value = _finite_number(
            capture_timeout, "capture_timeout", positive=True
        )
        interval_value = _finite_number(interval, "interval", positive=False)

        jsonl = Path(jsonl_path).resolve()
        summary_file = Path(summary_path).resolve()
        if jsonl == summary_file:
            raise ValueError("jsonl_path and summary_path must be different files")
        jsonl.parent.mkdir(parents=True, exist_ok=True)
        summary_file.parent.mkdir(parents=True, exist_ok=True)

        monitor = _SerialLogMonitor(self.serial_session)
        started_epoch = float(self.clock.time())
        started_monotonic = float(self.clock.monotonic())
        attempt_count = 0
        success_count = 0
        error_counts = {
            "timeout": 0,
            "protocol": 0,
            "crc": 0,
            "device_error": 0,
            "unexpected": 0,
        }
        sequence_floor: int | None = None
        sequence_violation_count = 0
        last_uptime_ms: int | None = None
        uptime_reboot_count = 0
        serial_reboot_count = 0
        background_error_log_count = 0
        background_fault_count = 0
        background_fault_counts: dict[str, int] = {}
        monitor_error_count = 0
        successful_latencies: list[float] = []
        device_capture_durations: list[float] = []
        jsonl_record_count = 0

        self._stream = jsonl.open("w", encoding="utf-8", newline="\n")
        try:
            while True:
                elapsed = float(self.clock.monotonic()) - started_monotonic
                if count is not None and attempt_count >= count:
                    break
                if duration_value is not None and attempt_count > 0 and elapsed >= duration_value:
                    break

                attempt_count += 1
                attempt_host_epoch = float(self.clock.time())
                attempt_started = float(self.clock.monotonic())
                requested_after_sequence = sequence_floor
                sequence: int | None = None
                device_uptime_ms: int | None = None
                capture_duration_ms: int | None = None
                payload_crc32: int | None = None
                calculated_crc32: int | None = None
                error: Exception | None = None

                try:
                    frame = self.provider.capture(
                        timeout=timeout_value,
                        after_sequence=requested_after_sequence,
                    )
                    metadata = getattr(frame, "metadata", None)
                    if metadata is None:
                        raise WatchCaptureProtocolError(
                            "capture provider returned a frame without metadata"
                        )
                    sequence = _required_uint32(
                        getattr(metadata, "sequence", None), "sequence"
                    )
                    if (
                        requested_after_sequence is not None
                        and sequence <= requested_after_sequence
                    ):
                        raise _SoakSequenceError(
                            f"capture sequence {sequence} is not newer than "
                            f"after_sequence {requested_after_sequence}"
                        )

                    payload_crc32 = _required_uint32(
                        getattr(metadata, "payload_crc32", None), "payload_crc32"
                    )
                    pixels = getattr(frame, "pixels", None)
                    if not isinstance(pixels, (bytes, bytearray, memoryview)):
                        raise WatchCaptureProtocolError(
                            "capture provider returned non-bytes pixels"
                        )
                    calculated_crc32 = zlib.crc32(bytes(pixels)) & _UINT32_MAX
                    if calculated_crc32 != payload_crc32:
                        raise WatchCaptureProtocolError(
                            "payload CRC32 mismatch in soak validation: "
                            f"expected {payload_crc32:08x}, got {calculated_crc32:08x}"
                        )
                    device_uptime_ms = _optional_non_negative_int(
                        getattr(metadata, "device_uptime_ms", None),
                        "device_uptime_ms",
                    )
                    capture_duration_ms = _optional_non_negative_int(
                        getattr(metadata, "capture_duration_ms", None),
                        "capture_duration_ms",
                    )
                except Exception as exc:
                    error = exc

                attempt_finished = float(self.clock.monotonic())
                end_to_end_ms = round(
                    max(0.0, attempt_finished - attempt_started) * 1000.0, 3
                )

                serial_lines: list[str] = []
                monitor_error: Exception | None = None
                try:
                    serial_lines = monitor.poll()
                except Exception as exc:
                    monitor_error = exc
                    monitor_error_count += 1

                background_entries: list[dict[str, Any]] = []
                reboot_signals: list[dict[str, Any]] = []
                for line in serial_lines:
                    categories = _classify_log(line)
                    background_error_log_count += 1
                    is_background_fault = any(
                        category != "reboot" for category in categories
                    )
                    if is_background_fault:
                        background_fault_count += 1
                    for category in categories:
                        background_fault_counts[category] = (
                            background_fault_counts.get(category, 0) + 1
                        )
                    if "reboot" in categories:
                        serial_reboot_count += 1
                        reboot_signals.append(
                            {"source": "serial_log", "line": line}
                        )
                    background_entries.append(
                        {"line": line, "categories": categories}
                    )

                if error is None and device_uptime_ms is not None:
                    if last_uptime_ms is not None and device_uptime_ms < last_uptime_ms:
                        uptime_reboot_count += 1
                        reboot_signals.append(
                            {
                                "source": "device_uptime",
                                "previous_ms": last_uptime_ms,
                                "current_ms": device_uptime_ms,
                            }
                        )
                    last_uptime_ms = device_uptime_ms

                if error is None:
                    assert sequence is not None
                    sequence_floor = sequence
                    success_count += 1
                    successful_latencies.append(end_to_end_ms)
                    if capture_duration_ms is not None:
                        device_capture_durations.append(float(capture_duration_ms))
                    classification = "success"
                else:
                    classification = _classify_exception(error)
                    error_counts[classification] += 1
                    if isinstance(error, _SoakSequenceError):
                        sequence_violation_count += 1
                    if sequence is not None and (
                        sequence_floor is None or sequence > sequence_floor
                    ):
                        sequence_floor = sequence
                    elif sequence_floor is not None and sequence_floor < _UINT32_MAX:
                        # Reserve the failed request's next floor.  The concrete
                        # provider also advances its own request sequence before
                        # transport I/O, so a retry cannot reuse an old request.
                        sequence_floor += 1

                record: dict[str, Any] = {
                    "type": "capture_attempt",
                    "attempt": attempt_count,
                    "host_time": _host_time(attempt_host_epoch),
                    "host_time_epoch_s": attempt_host_epoch,
                    "after_sequence": requested_after_sequence,
                    "sequence": sequence,
                    "device_uptime_ms": device_uptime_ms,
                    "capture_duration_ms": capture_duration_ms,
                    "end_to_end_duration_ms": end_to_end_ms,
                    "payload_crc32": payload_crc32,
                    "crc32": (
                        None if payload_crc32 is None else f"{payload_crc32:08x}"
                    ),
                    "calculated_crc32": calculated_crc32,
                    "success": error is None,
                    "classification": classification,
                    "error_type": None if error is None else type(error).__name__,
                    "error_message": None if error is None else str(error),
                    "background_errors": background_entries,
                    "reboot_detected": bool(reboot_signals),
                    "reboot_signals": reboot_signals,
                    "monitor_error": (
                        None
                        if monitor_error is None
                        else {
                            "type": type(monitor_error).__name__,
                            "message": str(monitor_error),
                        }
                    ),
                }
                assert self._stream is not None
                _write_jsonl(self._stream, record)
                jsonl_record_count += 1

                if count is not None and attempt_count >= count:
                    continue
                now = float(self.clock.monotonic())
                if duration_value is not None and now - started_monotonic >= duration_value:
                    continue
                if interval_value > 0:
                    sleep_for = interval_value
                    if duration_value is not None:
                        remaining = duration_value - (now - started_monotonic)
                        sleep_for = min(sleep_for, max(0.0, remaining))
                    if sleep_for > 0:
                        self.clock.sleep(sleep_for)

            # Capture logs that arrived after the last attempt completed.
            try:
                trailing_lines = monitor.poll()
            except Exception as exc:
                trailing_lines = []
                monitor_error_count += 1
                assert self._stream is not None
                _write_jsonl(
                    self._stream,
                    {
                        "type": "serial_monitor_error",
                        "host_time": _host_time(float(self.clock.time())),
                        "error_type": type(exc).__name__,
                        "error_message": str(exc),
                    },
                )
                jsonl_record_count += 1

            if trailing_lines:
                entries: list[dict[str, Any]] = []
                for line in trailing_lines:
                    categories = _classify_log(line)
                    background_error_log_count += 1
                    if any(category != "reboot" for category in categories):
                        background_fault_count += 1
                    for category in categories:
                        background_fault_counts[category] = (
                            background_fault_counts.get(category, 0) + 1
                        )
                    if "reboot" in categories:
                        serial_reboot_count += 1
                    entries.append({"line": line, "categories": categories})
                assert self._stream is not None
                _write_jsonl(
                    self._stream,
                    {
                        "type": "serial_observation",
                        "host_time": _host_time(float(self.clock.time())),
                        "background_errors": entries,
                    },
                )
                jsonl_record_count += 1
        finally:
            stream, self._stream = self._stream, None
            if stream is not None:
                stream.close()

        finished_epoch = float(self.clock.time())
        elapsed_seconds = max(
            0.0, float(self.clock.monotonic()) - started_monotonic
        )
        failure_count = attempt_count - success_count
        success_rate = success_count / attempt_count if attempt_count else 0.0
        latency = _latency_summary(successful_latencies)
        device_latency = _latency_summary(device_capture_durations)
        summary: dict[str, Any] = {
            "type": "watch_capture_soak_summary",
            "status": "completed",
            "started_at": _host_time(started_epoch),
            "finished_at": _host_time(finished_epoch),
            "elapsed_seconds": round(elapsed_seconds, 6),
            "termination_reason": (
                "count_reached" if count is not None else "duration_elapsed"
            ),
            "mode": (
                {"type": "count", "count": count}
                if count is not None
                else {"type": "duration", "duration_seconds": duration_value}
            ),
            "capture_timeout_seconds": timeout_value,
            "interval_seconds": interval_value,
            "attempt_count": attempt_count,
            "success_count": success_count,
            "failure_count": failure_count,
            "success_rate": round(success_rate, 6),
            "success_rate_percent": round(success_rate * 100.0, 4),
            "end_to_end_duration_ms": latency,
            "device_capture_duration_ms": device_latency,
            "p50_ms": latency["p50"],
            "p95_ms": latency["p95"],
            "p99_ms": latency["p99"],
            "error_counts": error_counts,
            "timeout_count": error_counts["timeout"],
            "protocol_error_count": error_counts["protocol"],
            "crc_error_count": error_counts["crc"],
            "device_error_count": error_counts["device_error"],
            "unexpected_error_count": error_counts["unexpected"],
            "strictly_increasing_sequences": sequence_violation_count == 0,
            "sequence_violation_count": sequence_violation_count,
            "reboot_count": uptime_reboot_count + serial_reboot_count,
            "uptime_reboot_count": uptime_reboot_count,
            "serial_reboot_count": serial_reboot_count,
            "background_error_log_count": background_error_log_count,
            "background_fault_count": background_fault_count,
            "background_fault_counts": dict(sorted(background_fault_counts.items())),
            "serial_log_monitoring": monitor.enabled,
            "monitor_error_count": monitor_error_count,
            "jsonl_record_count": jsonl_record_count,
            "jsonl_path": str(jsonl),
            "summary_path": str(summary_file),
        }
        summary_file.write_text(
            json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return summary


def run_watch_capture_soak(
    *,
    provider: Any,
    serial_session: Any | None = None,
    jsonl_path: str | Path,
    summary_path: str | Path,
    count: int | None = None,
    duration: float | None = None,
    capture_timeout: float = 12.0,
    interval: float = 0.0,
    clock: SoakClock | None = None,
) -> dict[str, Any]:
    """Functional wrapper around :class:`WatchCaptureSoak`."""

    runner = WatchCaptureSoak(
        provider, serial_session=serial_session, clock=clock
    )
    try:
        return runner.run(
            jsonl_path=jsonl_path,
            summary_path=summary_path,
            count=count,
            duration=duration,
            capture_timeout=capture_timeout,
            interval=interval,
        )
    finally:
        runner.close()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m agent_loop_system.tools.watch_capture_soak",
        description="Run unattended direct-watch screenshot capture soak testing.",
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--count", type=int, help="attempt count (minimum: 100)")
    mode.add_argument(
        "--duration-hours",
        type=float,
        help="run duration in hours (use 24 for a one-day soak)",
    )
    parser.add_argument("--port", default="COM7")
    parser.add_argument("--baudrate", type=int, default=1_500_000)
    parser.add_argument(
        "--timeout",
        type=float,
        default=12.0,
        help="per-capture timeout in seconds (default: 12)",
    )
    parser.add_argument("--interval", type=float, default=0.0)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("artifacts/watch_capture_soak"),
    )
    parser.add_argument("--sequence-start", type=int)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.count is not None and args.count < MIN_CAPTURE_COUNT:
        parser.error(f"--count must be at least {MIN_CAPTURE_COUNT}")
    if args.duration_hours is not None and (
        not math.isfinite(args.duration_hours) or args.duration_hours <= 0
    ):
        parser.error("--duration-hours must be a positive finite number")

    from agent_loop_system.tools.hardware_serial import HardwareSerialSession

    output_dir = args.output_dir.resolve()
    serial_session = HardwareSerialSession(
        port=args.port,
        baudrate=args.baudrate,
        log_dir=output_dir / "serial",
    )
    provider = WatchCaptureProvider(
        serial_session, sequence_start=args.sequence_start
    )
    runner = WatchCaptureSoak(
        provider, serial_session=serial_session
    )
    summary: dict[str, Any] | None = None
    serial_session.start()
    try:
        summary = runner.run(
            jsonl_path=output_dir / "captures.jsonl",
            summary_path=output_dir / "summary.json",
            count=args.count,
            duration=(
                None
                if args.duration_hours is None
                else args.duration_hours * 60.0 * 60.0
            ),
            capture_timeout=args.timeout,
            interval=args.interval,
        )
    finally:
        runner.close()
        provider.close()
        serial_session.stop()

    assert summary is not None
    print(
        json.dumps(
            {
                "summary": summary["summary_path"],
                "attempts": summary["attempt_count"],
                "success_rate": summary["success_rate"],
                "errors": summary["error_counts"],
                "reboots": summary["reboot_count"],
                "background_faults": summary["background_fault_count"],
            },
            ensure_ascii=False,
        )
    )
    clean = (
        summary["failure_count"] == 0
        and summary["reboot_count"] == 0
        and summary["background_fault_count"] == 0
        and summary["monitor_error_count"] == 0
    )
    return 0 if clean else 1


__all__ = [
    "DAY_SECONDS",
    "MIN_CAPTURE_COUNT",
    "SoakClock",
    "SystemClock",
    "WatchCaptureSoak",
    "main",
    "run_watch_capture_soak",
]


if __name__ == "__main__":
    raise SystemExit(main())
