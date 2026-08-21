from __future__ import annotations

import json
import zlib
from pathlib import Path
from typing import Any

import pytest

from agent_loop_system.tools.watch_capture import (
    WatchCaptureDeviceError,
    WatchCaptureFrame,
    WatchCaptureMetadata,
    WatchCaptureProtocolError,
    WatchCaptureTimeoutError,
)
from agent_loop_system.tools.watch_capture_soak import (
    DAY_SECONDS,
    WatchCaptureSoak,
    run_watch_capture_soak,
)


class FakeClock:
    def __init__(self, *, epoch: float = 1_700_000_000.0) -> None:
        self.epoch = epoch
        self.current = 0.0
        self.sleeps: list[float] = []

    def time(self) -> float:
        return self.epoch + self.current

    def monotonic(self) -> float:
        return self.current

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.current += seconds

    def advance_ms(self, milliseconds: float) -> None:
        self.current += milliseconds / 1000.0


class FakeSerialSession:
    def __init__(self, *existing_errors: str) -> None:
        self.background_errors = list(existing_errors)
        self.stop_calls = 0

    @property
    def background_error_count(self) -> int:
        return len(self.background_errors)

    def background_errors_since(self, start_index: int = 0) -> list[str]:
        return list(self.background_errors[start_index:])

    def stop(self) -> None:
        self.stop_calls += 1


def make_frame(
    sequence: int,
    *,
    uptime_ms: int | None,
    capture_duration_ms: int | None = 7,
    pixels: bytes = b"watch-frame",
    crc32: int | None = None,
) -> WatchCaptureFrame:
    payload_crc32 = zlib.crc32(pixels) & 0xFFFFFFFF if crc32 is None else crc32
    metadata = WatchCaptureMetadata(
        sequence=sequence,
        timestamp=0.0,
        width=1,
        height=1,
        pixel_format="bgr888",
        data_size=len(pixels),
        stride=3,
        source="watch_display",
        transport="hardware_serial",
        payload_crc32=payload_crc32,
        chunk_bytes=len(pixels),
        chunks=1,
        encoding="base64",
        device_uptime_ms=uptime_ms,
        capture_duration_ms=capture_duration_ms,
    )
    return WatchCaptureFrame(pixels=pixels, metadata=metadata)


class CountingProvider:
    def __init__(self, clock: FakeClock, serial: FakeSerialSession) -> None:
        self.clock = clock
        self.serial = serial
        self.calls: list[dict[str, Any]] = []
        self.close_calls = 0

    def capture(self, *, timeout: float, after_sequence: int | None):
        index = len(self.calls) + 1
        self.calls.append(
            {"timeout": timeout, "after_sequence": after_sequence}
        )
        self.clock.advance_ms(index)
        if index == 2:
            self.serial.background_errors.append("watchdog expired in gui task")
        if index == 3:
            self.serial.background_errors.append("Rebooting after recovery")

        if index == 1:
            uptime = 1_000
        elif index == 2:
            uptime = 2_000
        else:
            uptime = (index - 2) * 100
        return make_frame(
            99 + index,
            uptime_ms=uptime,
            capture_duration_ms=index,
        )

    def close(self) -> None:
        self.close_calls += 1


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_count_soak_writes_durable_records_summary_and_background_faults(
    tmp_path: Path,
) -> None:
    clock = FakeClock()
    serial = FakeSerialSession("old HardFault must stay outside this run")
    provider = CountingProvider(clock, serial)
    runner = WatchCaptureSoak(provider, serial_session=serial, clock=clock)
    jsonl_path = tmp_path / "captures.jsonl"
    summary_path = tmp_path / "summary.json"

    summary = runner.run(
        jsonl_path=jsonl_path,
        summary_path=summary_path,
        count=100,
        capture_timeout=5,
    )
    records = read_jsonl(jsonl_path)
    on_disk_summary = json.loads(summary_path.read_text(encoding="utf-8"))

    assert len(records) == 100
    assert records[0] == {
        "type": "capture_attempt",
        "attempt": 1,
        "host_time": "2023-11-15T06:13:20.000+08:00",
        "host_time_epoch_s": 1_700_000_000.0,
        "after_sequence": None,
        "sequence": 100,
        "device_uptime_ms": 1_000,
        "capture_duration_ms": 1,
        "end_to_end_duration_ms": 1.0,
        "payload_crc32": zlib.crc32(b"watch-frame") & 0xFFFFFFFF,
        "crc32": f"{zlib.crc32(b'watch-frame') & 0xFFFFFFFF:08x}",
        "calculated_crc32": zlib.crc32(b"watch-frame") & 0xFFFFFFFF,
        "success": True,
        "classification": "success",
        "error_type": None,
        "error_message": None,
        "background_errors": [],
        "reboot_detected": False,
        "reboot_signals": [],
        "monitor_error": None,
    }
    assert records[1]["background_errors"] == [
        {
            "line": "watchdog expired in gui task",
            "categories": ["watchdog"],
        }
    ]
    assert records[2]["reboot_detected"] is True
    assert {signal["source"] for signal in records[2]["reboot_signals"]} == {
        "serial_log",
        "device_uptime",
    }
    assert [call["after_sequence"] for call in provider.calls[:4]] == [
        None,
        100,
        101,
        102,
    ]

    assert summary == on_disk_summary
    assert summary["attempt_count"] == 100
    assert summary["success_count"] == 100
    assert summary["failure_count"] == 0
    assert summary["success_rate"] == 1.0
    assert summary["error_counts"] == {
        "timeout": 0,
        "protocol": 0,
        "crc": 0,
        "device_error": 0,
        "unexpected": 0,
    }
    assert summary["p50_ms"] == 50.5
    assert summary["p95_ms"] == 95.05
    assert summary["p99_ms"] == 99.01
    assert summary["uptime_reboot_count"] == 1
    assert summary["serial_reboot_count"] == 1
    assert summary["reboot_count"] == 2
    assert summary["background_error_log_count"] == 2
    assert summary["background_fault_count"] == 1
    assert summary["background_fault_counts"] == {
        "reboot": 1,
        "watchdog": 1,
    }
    assert "old HardFault" not in jsonl_path.read_text(encoding="utf-8")

    runner.close()
    assert provider.close_calls == 0
    assert serial.stop_calls == 0


class ErrorProvider:
    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self.calls: list[int | None] = []
        self.first_frame = make_frame(10, uptime_ms=1_000)

    def capture(self, *, timeout: float, after_sequence: int | None):
        del timeout
        index = len(self.calls) + 1
        self.calls.append(after_sequence)
        self.clock.advance_ms(10)
        if index == 1:
            return self.first_frame
        if index == 2:
            raise WatchCaptureTimeoutError("capture timed out")
        if index == 3:
            raise WatchCaptureProtocolError("missing screenshot_begin")
        if index == 4:
            return make_frame(13, uptime_ms=1_300, crc32=0)
        if index == 5:
            raise WatchCaptureDeviceError("display unavailable")
        if index == 6:
            return self.first_frame
        assert after_sequence is not None
        return make_frame(after_sequence + 1, uptime_ms=1_000 + index * 10)


def test_failures_are_classified_and_stale_frame_is_never_returned_as_success(
    tmp_path: Path,
) -> None:
    clock = FakeClock()
    provider = ErrorProvider(clock)
    jsonl_path = tmp_path / "attempts.jsonl"

    summary = run_watch_capture_soak(
        provider=provider,
        serial_session=None,
        clock=clock,
        jsonl_path=jsonl_path,
        summary_path=tmp_path / "summary.json",
        count=100,
    )
    records = read_jsonl(jsonl_path)

    assert [record["classification"] for record in records[:7]] == [
        "success",
        "timeout",
        "protocol",
        "crc",
        "device_error",
        "protocol",
        "success",
    ]
    assert records[5]["sequence"] == 10
    assert records[5]["success"] is False
    assert "not newer" in records[5]["error_message"]
    assert provider.calls[:7] == [None, 10, 11, 12, 13, 14, 15]
    assert records[6]["sequence"] == 16

    assert summary["attempt_count"] == 100
    assert summary["success_count"] == 95
    assert summary["success_rate"] == 0.95
    assert summary["error_counts"] == {
        "timeout": 1,
        "protocol": 2,
        "crc": 1,
        "device_error": 1,
        "unexpected": 0,
    }
    assert summary["sequence_violation_count"] == 1
    assert summary["strictly_increasing_sequences"] is False
    assert summary["crc_error_count"] == 1


class DurationProvider:
    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self.calls: list[int | None] = []
        self.timeouts: list[float] = []

    def capture(self, *, timeout: float, after_sequence: int | None):
        self.timeouts.append(timeout)
        self.calls.append(after_sequence)
        self.clock.current += DAY_SECONDS / 2
        sequence = 1 if after_sequence is None else after_sequence + 1
        return make_frame(sequence, uptime_ms=sequence * 1_000)


def test_duration_mode_runs_for_24_hours_with_injected_clock(tmp_path: Path) -> None:
    clock = FakeClock()
    provider = DurationProvider(clock)
    runner = WatchCaptureSoak(provider, clock=clock)

    summary = runner.run(
        jsonl_path=tmp_path / "duration.jsonl",
        summary_path=tmp_path / "duration-summary.json",
        duration=DAY_SECONDS,
    )

    assert provider.calls == [None, 1]
    assert provider.timeouts == [12.0, 12.0]
    assert summary["mode"] == {
        "type": "duration",
        "duration_seconds": float(DAY_SECONDS),
    }
    assert summary["termination_reason"] == "duration_elapsed"
    assert summary["elapsed_seconds"] == DAY_SECONDS
    assert summary["attempt_count"] == 2
    assert summary["success_rate"] == 1.0


def test_run_mode_validation_rejects_short_or_ambiguous_soaks(tmp_path: Path) -> None:
    runner = WatchCaptureSoak(DurationProvider(FakeClock()), clock=FakeClock())
    arguments = {
        "jsonl_path": tmp_path / "records.jsonl",
        "summary_path": tmp_path / "summary.json",
    }

    with pytest.raises(ValueError, match="exactly one"):
        runner.run(**arguments)
    with pytest.raises(ValueError, match="at least 100"):
        runner.run(**arguments, count=99)
    with pytest.raises(ValueError, match="exactly one"):
        runner.run(**arguments, count=100, duration=DAY_SECONDS)


def test_background_monitor_starts_from_absolute_cursor_after_cache_trim(
    tmp_path: Path,
) -> None:
    class TrimmedSerialSession:
        def __init__(self) -> None:
            self._total = 5
            self._retained = ["old watchdog 3", "old watchdog 4"]
            self.requested_cursors: list[int] = []

        @property
        def background_error_count(self) -> int:
            return self._total

        def append(self, line: str) -> None:
            self._total += 1
            self._retained.append(line)
            self._retained = self._retained[-2:]

        def background_errors_since(self, start_index: int = 0) -> list[str]:
            self.requested_cursors.append(start_index)
            oldest_available = self._total - len(self._retained)
            if start_index < oldest_available:
                raise RuntimeError("absolute cursor expired")
            return list(self._retained[start_index - oldest_available :])

    class OneCaptureProvider:
        def __init__(
            self, clock: FakeClock, serial: TrimmedSerialSession
        ) -> None:
            self.clock = clock
            self.serial = serial

        def capture(self, *, timeout: float, after_sequence: int | None):
            assert timeout == 12.0
            assert after_sequence is None
            self.serial.append("new watchdog after soak start")
            self.clock.current += DAY_SECONDS
            return make_frame(1, uptime_ms=1_000)

    clock = FakeClock()
    serial = TrimmedSerialSession()
    provider = OneCaptureProvider(clock, serial)
    runner = WatchCaptureSoak(provider, serial_session=serial, clock=clock)

    summary = runner.run(
        jsonl_path=tmp_path / "trimmed.jsonl",
        summary_path=tmp_path / "trimmed-summary.json",
        duration=DAY_SECONDS,
    )

    assert serial.requested_cursors == [5, 6]
    assert summary["background_error_log_count"] == 1
    assert summary["background_fault_counts"] == {"watchdog": 1}
    record = read_jsonl(tmp_path / "trimmed.jsonl")[0]
    assert record["background_errors"] == [
        {
            "line": "new watchdog after soak start",
            "categories": ["watchdog"],
        }
    ]
