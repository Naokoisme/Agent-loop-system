"""Bounded child-process communication and restart-safe ownership checks."""

from __future__ import annotations

import math
import os
from pathlib import Path
import subprocess
import time
from typing import Any


DEFAULT_TERMINATION_GRACE_SECONDS = 5.0


def positive_timeout(name: str, default: float) -> float:
    """Read a positive timeout without letting malformed env values break startup."""

    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    return value if math.isfinite(value) and value > 0 else default


def process_identity(pid: int | None) -> str | None:
    """Return an OS process-start identity so a persisted PID is never trusted alone."""

    if not isinstance(pid, int) or pid <= 0:
        return None
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            kernel32.OpenProcess.restype = wintypes.HANDLE
            kernel32.GetProcessTimes.argtypes = [
                wintypes.HANDLE,
                ctypes.POINTER(wintypes.FILETIME),
                ctypes.POINTER(wintypes.FILETIME),
                ctypes.POINTER(wintypes.FILETIME),
                ctypes.POINTER(wintypes.FILETIME),
            ]
            kernel32.GetProcessTimes.restype = wintypes.BOOL
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel32.CloseHandle.restype = wintypes.BOOL
            process = kernel32.OpenProcess(0x1000, False, pid)
            if not process:
                return None
            creation = wintypes.FILETIME()
            exit_time = wintypes.FILETIME()
            kernel = wintypes.FILETIME()
            user = wintypes.FILETIME()
            try:
                if not kernel32.GetProcessTimes(
                    process,
                    ctypes.byref(creation),
                    ctypes.byref(exit_time),
                    ctypes.byref(kernel),
                    ctypes.byref(user),
                ):
                    return None
                ticks = (creation.dwHighDateTime << 32) | creation.dwLowDateTime
                return f"windows-filetime:{ticks}"
            finally:
                kernel32.CloseHandle(process)
        except (AttributeError, OSError, ValueError):
            return None
    stat_path = Path(f"/proc/{pid}/stat")
    try:
        fields = stat_path.read_text(encoding="utf-8").split()
    except OSError:
        return None
    return f"proc-start:{fields[21]}" if len(fields) > 21 else None


def process_is_alive(pid: int | None) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            kernel32.OpenProcess.restype = wintypes.HANDLE
            kernel32.GetExitCodeProcess.argtypes = [
                wintypes.HANDLE,
                ctypes.POINTER(wintypes.DWORD),
            ]
            kernel32.GetExitCodeProcess.restype = wintypes.BOOL
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel32.CloseHandle.restype = wintypes.BOOL
            process = kernel32.OpenProcess(0x1000, False, pid)
            if not process:
                return ctypes.get_last_error() == 5  # Access denied still means it exists.
            exit_code = wintypes.DWORD()
            try:
                return bool(kernel32.GetExitCodeProcess(process, ctypes.byref(exit_code))) and (
                    exit_code.value == 259  # STILL_ACTIVE
                )
            finally:
                kernel32.CloseHandle(process)
        except (AttributeError, OSError, ValueError):
            return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def terminate_pid_tree(
    pid: int | None,
    *,
    expected_identity: str | None,
    timeout: float = DEFAULT_TERMINATION_GRACE_SECONDS,
) -> bool:
    """Terminate a persisted child only if its process-start identity still matches."""

    if not isinstance(pid, int) or pid <= 0 or pid == os.getpid():
        return False
    if not process_is_alive(pid):
        return True
    if not expected_identity or process_identity(pid) != expected_identity:
        return False
    try:
        if os.name == "nt":
            completed = subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=timeout,
                check=False,
            )
            return completed.returncode == 0 or not process_is_alive(pid)

        import signal

        os.kill(pid, signal.SIGTERM)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and process_is_alive(pid):
            time.sleep(0.05)
        if process_is_alive(pid):
            os.kill(pid, signal.SIGKILL)
        return not process_is_alive(pid)
    except (OSError, subprocess.SubprocessError):
        return not process_is_alive(pid)


def terminate_process_tree(
    process: Any,
    timeout: float = DEFAULT_TERMINATION_GRACE_SECONDS,
) -> bool:
    """Best-effort bounded termination for a child created by this process."""

    if process is None:
        return True
    poll = getattr(process, "poll", None)
    if callable(poll):
        try:
            if poll() is not None:
                return True
        except Exception:
            pass
    pid = getattr(process, "pid", None)
    if os.name == "nt" and isinstance(pid, int) and pid > 0:
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=timeout,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            pass
    terminate = getattr(process, "terminate", None)
    if callable(terminate):
        try:
            terminate()
        except OSError:
            pass
    wait = getattr(process, "wait", None)
    if callable(wait):
        try:
            wait(timeout=timeout)
        except (OSError, subprocess.TimeoutExpired):
            kill = getattr(process, "kill", None)
            if callable(kill):
                try:
                    kill()
                    wait(timeout=timeout)
                except (OSError, subprocess.TimeoutExpired):
                    pass
    if callable(poll):
        try:
            return poll() is not None
        except Exception:
            return False
    return True


def communicate_process(process: Any, timeout: float) -> tuple[str, str]:
    """Use Popen timeouts while remaining compatible with small test doubles."""

    try:
        return process.communicate(timeout=timeout)
    except TypeError:
        return process.communicate()
