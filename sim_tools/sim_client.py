"""Minimal simulator interaction client for case-map coordinate collection.

Protocol: spawn main.exe with W30_SIM_SHELL_COMMAND_FILE inbox; each raw HLQ
command is wrapped as srv_quick_cmd/TOP5STEP and written to the inbox; bridge JSON lines appear on
stdout with marker "w30_test_bridge". A command completes when its
command_result (request == cmd name, status accepted/rejected) arrives, or for
:GUI_TREE: when gui_tree_end arrives.
"""
from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from pathlib import Path

from agent_loop_system.main import _load_env
from agent_loop_system.runtime_root import RuntimePaths, resolve_config_path

BRIDGE_MARKER = '{"protocol":"w30_test_bridge"'

_load_env()
_PATHS = RuntimePaths.from_root()
SIM_EXE = str(resolve_config_path(os.environ.get(
    "SIMULATOR_ARTIFACT_PATH",
    _PATHS.firmware_workspaces / "620C_W6830" / "core" / "gui" / "simulator" / "bin" / "main.exe",
)))
INBOX = _PATHS.root / "sim_tools" / "w30-test-command.txt"
READY_MARKERS = ("gui_comm_system_open_first_window",)


class SimClient:
    def __init__(self, exe: str = SIM_EXE, inbox: Path = INBOX, startup_timeout: float = 90.0):
        self.exe = exe
        self.sim_bin = str(Path(exe).resolve().parent)
        self.inbox = Path(inbox)
        self.startup_timeout = startup_timeout
        self.proc: subprocess.Popen | None = None
        self._lines: list[str] = []
        self._lock = threading.Lock()
        self._ready = threading.Event()
        self._waiters: list[dict] = []
        self._reader_errors: list[str] = []

    # ---------- lifecycle ----------

    def start(self) -> None:
        if self.proc is not None:
            return
        self.inbox.parent.mkdir(parents=True, exist_ok=True)
        if self.inbox.exists():
            self.inbox.unlink()
        env = dict(os.environ)
        env["W30_SIM_SHELL_COMMAND_FILE"] = str(self.inbox)
        env["PATH"] = r"C:\msys64\ucrt64\bin;C:\Windows\System32" + os.pathsep + env.get("PATH", "")
        self.proc = subprocess.Popen(
            [self.exe],
            cwd=self.sim_bin,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            shell=False,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
        )
        threading.Thread(target=self._read_stream, args=(self.proc.stdout,), daemon=True).start()
        threading.Thread(target=self._read_stream, args=(self.proc.stderr,), daemon=True).start()
        if not self._ready.wait(self.startup_timeout):
            raise RuntimeError(f"simulator startup timeout; markers={READY_MARKERS}")
        if self.proc.poll() is not None:
            raise RuntimeError(f"simulator exited during startup: {self.proc.returncode}")
        self.send(":GUI_PING:900001", "gui_ping", "gui_ack", self.startup_timeout)
        print(f"[sim] ready pid={self.proc.pid}", flush=True)

    def stop(self) -> None:
        if self.proc is None:
            return
        try:
            self.proc.terminate()
        except Exception:
            pass
        try:
            self.proc.wait(timeout=5)
        except Exception:
            self.proc.kill()
        self.proc = None

    # ---------- stream ----------

    def _read_stream(self, stream) -> None:
        try:
            for raw in iter(stream.readline, ""):
                line = raw.rstrip("\r\n")
                with self._lock:
                    self._lines.append(line)
                    for w in self._waiters:
                        w["sink"].append(line)
                        if self._matches(w, line):
                            w["done"].set()
                if self._ready.is_set() is False and any(m in line for m in READY_MARKERS):
                    self._ready.set()
        except Exception as exc:  # pragma: no cover
            with self._lock:
                self._reader_errors.append(str(exc))

    @staticmethod
    def _parse(line: str) -> dict | None:
        idx = line.find(BRIDGE_MARKER)
        if idx < 0:
            return None
        try:
            value = json.loads(line[idx:])
        except Exception:
            return None
        return value if isinstance(value, dict) else None

    @staticmethod
    def _matches(waiter: dict, line: str) -> bool:
        event = SimClient._parse(line)
        if event is None:
            return False
        if event.get("type") == waiter["end_type"] and event.get("request") == waiter["request"]:
            return True
        return False

    # ---------- command ----------

    def _write_command(self, command: str, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        while self.inbox.exists():
            if self.proc is None or self.proc.poll() is not None:
                raise RuntimeError("simulator exited while waiting for inbox")
            if time.monotonic() >= deadline:
                raise RuntimeError(f"simulator did not consume inbox within {timeout}s")
            time.sleep(0.01)
        tmp = self.inbox.with_name(f"{self.inbox.name}.{os.getpid()}.{time.time_ns()}.tmp")
        inner = command.lstrip(":")
        wire = f'srv_quick_cmd send "TOP5STEP:{inner};"'
        tmp.write_text(f"{wire}\n", encoding="utf-8")
        os.replace(tmp, self.inbox)

    def send(self, command: str, request: str, end_type: str = "command_result", timeout: float = 15.0) -> list[dict]:
        """Send one command, wait for its terminal bridge event, return events."""
        waiter = {"request": request, "end_type": end_type, "done": threading.Event(), "sink": []}
        with self._lock:
            if self.proc is None or self.proc.poll() is not None:
                raise RuntimeError("simulator not running")
            self._waiters.append(waiter)
        try:
            self._write_command(command, timeout)
            if not waiter["done"].wait(timeout):
                raise RuntimeError(f"timeout waiting for {request} after {command!r}")
            events = [e for e in (self._parse(l) for l in waiter["sink"]) if e is not None]
            return events
        finally:
            with self._lock:
                self._waiters.remove(waiter)

    def recent_lines(self, n: int = 50) -> list[str]:
        with self._lock:
            return list(self._lines[-n:])


if __name__ == "__main__":
    import sys

    client = SimClient()
    client.start()
    try:
        cmds = [c.strip() for c in sys.argv[1:]] or [":GUI_TREE:1"]
        for raw in cmds:
            if not raw:
                continue
            req = raw.split(":", 2)[1].lower()
            end_type = "gui_tree_end" if req == "gui_tree" else "command_result"
            events = client.send(raw, req, end_type)
            print(f"### CMD {raw} -> {len(events)} events")
            for e in events:
                print(json.dumps(e, ensure_ascii=False))
    finally:
        client.stop()
