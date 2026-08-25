r"""最小模拟器会话：inbox 文件发命令 + 读 stdout JSON 回执。

命令格式来自当前隔离固件工作区的模拟器测试约定：
    srv_quick_cmd send "TOP5STEP:<COMMAND>:<PARAM>;"
调用方传入裸 hlq 命令（如 ":GUI_PING:1"），本模块负责去掉前导冒号、
加 TOP5STEP 前缀、加 ; 后缀、用双引号包裹。
"""
from __future__ import annotations

import ctypes
import hashlib
import json
import os
import re
import struct
import subprocess
import threading
import time
import uuid
from collections.abc import Mapping
from ctypes import wintypes
from dataclasses import dataclass, field
from pathlib import Path

from agent_loop_system.runtime_root import resolve_config_path

# 不同项目有两种首窗启动路径：调用 system helper，或直接 gui_open_new_win。
# 两种标记都只表示可以开始 GUI_PING；最终就绪仍以 gui_ack/processed 为准。
READY_MARKERS = (
    "gui_comm_system_open_first_window",
    "============= [End] create new win",
)
RESOURCE_FILES = ("str_res.bin", "img_res.bin")
_JSON_DECODER = json.JSONDecoder()


def extract_json_objects(line: str) -> list[dict]:
    """提取一行中完整的 JSON 对象，兼容 shell 提示符和日志前缀。"""
    objects: list[dict] = []
    offset = 0
    while True:
        start = line.find("{", offset)
        if start < 0:
            return objects
        try:
            value, consumed = _JSON_DECODER.raw_decode(line[start:])
        except json.JSONDecodeError:
            offset = start + 1
            continue
        if isinstance(value, dict):
            objects.append(value)
        offset = start + consumed


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_simulator_resource_provenance(
    exe: str | os.PathLike,
    *,
    source_root: str | os.PathLike | None = None,
    project: str | None = None,
) -> dict[str, str]:
    """确认模拟器、项目配置以及运行时资源来自同一个 W30 项目。"""
    source_value = str(source_root or os.environ.get("W30_SOURCE_ROOT", "")).strip()
    project_value = str(project or os.environ.get("W30_PROJECT", "")).strip()
    if not source_value or not project_value:
        raise RuntimeError("资源校验配置缺失: W30_SOURCE_ROOT/W30_PROJECT")

    root = resolve_config_path(source_value)
    exe_path = resolve_config_path(exe)
    if not exe_path.is_relative_to(root):
        raise RuntimeError(f"模拟器不属于 Agent 工作区: {exe_path}（期望位于 {root}）")

    project_config = root / "app" / "ProjectConfig.cmake"
    if not project_config.is_file():
        raise RuntimeError(f"项目配置不存在: {project_config}")
    config_text = project_config.read_text(encoding="utf-8", errors="ignore")
    match = re.search(r"^\s*set\(PROJECT\s+([^\s\)]+)\s*\)", config_text, re.MULTILINE)
    active_project = match.group(1) if match else ""
    if active_project != project_value:
        raise RuntimeError(
            f"项目配置不一致: W30_PROJECT={project_value}, ProjectConfig={active_project or '未设置'}"
        )

    source_res = root / "app" / "projects" / project_value / "assets" / "res"
    runtime_res = exe_path.parent.parent / "fs_dir" / "res"
    hashes: dict[str, str] = {}
    for name in RESOURCE_FILES:
        expected = source_res / name
        actual = runtime_res / name
        if not expected.is_file() or not actual.is_file():
            raise RuntimeError(f"模拟器资源缺失: source={expected}, runtime={actual}")
        expected_hash = _sha256(expected)
        actual_hash = _sha256(actual)
        if actual_hash != expected_hash:
            raise RuntimeError(
                f"模拟器资源来源不一致: {name}, source={expected_hash}, runtime={actual_hash}"
            )
        hashes[name] = actual_hash
    return hashes


def _find_window_by_pid(pid: int) -> int:
    """枚举窗口，返回属于 pid 的最大可见窗口句柄。"""
    user32 = ctypes.windll.user32
    best_hwnd = 0
    best_area = 0

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    def callback(hwnd, _lparam):
        nonlocal best_hwnd, best_area
        wpid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(wpid))
        if wpid.value != pid or not user32.IsWindowVisible(hwnd):
            return True
        rect = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        area = (rect.right - rect.left) * (rect.bottom - rect.top)
        if area > best_area:
            best_area = area
            best_hwnd = hwnd
        return True

    user32.EnumWindows(callback, 0)
    return best_hwnd


def _capture_window_bmp(hwnd: int, output_path: str) -> bool:
    """用 PrintWindow 捕获窗口为 BMP 文件。"""
    user32 = ctypes.windll.user32
    gdi32 = ctypes.windll.gdi32

    rect = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    w = rect.right - rect.left
    h = rect.bottom - rect.top
    if w <= 0 or h <= 0:
        return False

    hwnd_dc = user32.GetWindowDC(hwnd)
    if not hwnd_dc:
        return False

    ok = False
    try:
        mem_dc = gdi32.CreateCompatibleDC(hwnd_dc)
        bmp = gdi32.CreateCompatibleBitmap(hwnd_dc, w, h)
        old = gdi32.SelectObject(mem_dc, bmp)

        # PW_RENDERFULLCONTENT=3: 即使窗口被遮挡也能捕获
        ok = bool(user32.PrintWindow(hwnd, mem_dc, 3))

        if ok:
            # 读取像素数据
            class BMPINFOHEADER(ctypes.Structure):
                _fields_ = [
                    ("biSize", wintypes.DWORD),
                    ("biWidth", wintypes.LONG),
                    ("biHeight", wintypes.LONG),
                    ("biPlanes", wintypes.WORD),
                    ("biBitCount", wintypes.WORD),
                    ("biCompression", wintypes.DWORD),
                    ("biSizeImage", wintypes.DWORD),
                    ("biXPelsPerMeter", wintypes.LONG),
                    ("biYPelsPerMeter", wintypes.LONG),
                    ("biClrUsed", wintypes.DWORD),
                    ("biClrImportant", wintypes.DWORD),
                ]

            bi = BMPINFOHEADER()
            bi.biSize = ctypes.sizeof(BMPINFOHEADER)
            bi.biWidth = w
            bi.biHeight = -h  # 负值=自顶向下
            bi.biPlanes = 1
            bi.biBitCount = 32
            bi.biCompression = 0  # BI_RGB

            buf_size = w * h * 4
            buf = ctypes.create_string_buffer(buf_size)
            gdi32.GetDIBits(mem_dc, bmp, 0, h, buf, ctypes.byref(bi), 0)

            # 写 BMP 文件
            Path(output_path).parent.mkdir(parents=True, exist_ok=True)
            # 截图后处理上下翻转
            row_size = w * 4
            with open(output_path, "wb") as f:
                f.write(struct.pack("<2sIHHI", b"BM", 54 + buf_size, 0, 0, 54))
                f.write(struct.pack("<IiiHHIIiiII",
                                    40, w, h, 1, 32, 0, buf_size, 2835, 2835, 0, 0))
                for y in range(h - 1, -1, -1):
                    f.write(buf.raw[y * row_size : (y + 1) * row_size])

        gdi32.SelectObject(mem_dc, old)
        gdi32.DeleteObject(bmp)
        gdi32.DeleteDC(mem_dc)
    finally:
        user32.ReleaseDC(hwnd, hwnd_dc)

    return ok


@dataclass
class CommandResult:
    request: str
    status: str
    raw: dict
    lines: list[str] = field(default_factory=list)
    start_index: int | None = None


class SimulatorSession:
    def __init__(
        self,
        exe: str | os.PathLike,
        *,
        startup_timeout: float = 90.0,
        cmd_timeout: float = 5.0,
        environment: Mapping[str, str] | None = None,
    ):
        self.exe = os.path.abspath(str(exe))
        self.cwd = os.path.dirname(self.exe)
        self.environment = dict(environment) if environment is not None else None
        settings = os.environ if self.environment is None else self.environment
        # 每个模拟器会话使用独立收件箱。若上一次运行异常退出并残留 main.exe，
        # 固定文件名会让旧进程抢走新会话的命令，导致当前 runner 等不到回执。
        self.inbox = os.path.join(
            self.cwd,
            "data",
            f"w30-test-command-{os.getpid()}-{uuid.uuid4().hex}.txt",
        )
        self.startup_timeout = startup_timeout
        self.cmd_timeout = cmd_timeout
        self.process: subprocess.Popen[str] | None = None
        self._lines: list[str] = []
        self._lock = threading.Lock()
        self._ready = threading.Event()
        self._automation_ready = threading.Event()
        self._gui_command_ready = threading.Event()
        self._automation_ready_marker = settings.get(
            "SIMULATOR_SHELL_READY_MARKER", ""
        ).strip()
        self._gui_command_ready_marker = settings.get(
            "SIMULATOR_GUI_COMMAND_READY_MARKER", ""
        ).strip()
        self._reader: threading.Thread | None = None

    def start(self) -> None:
        settings = os.environ if self.environment is None else self.environment
        verify_simulator_resource_provenance(
            self.exe,
            source_root=settings.get("W30_SOURCE_ROOT"),
            project=settings.get("W30_PROJECT"),
        )
        env = dict(settings)
        env["W30_SIM_SHELL_COMMAND_FILE"] = self.inbox
        env["PATH"] = os.pathsep.join(
            p
            for p in [self.cwd, r"C:\msys64\ucrt64\bin", r"C:\Windows\System32", env.get("PATH", "")]
            if p
        )
        os.makedirs(os.path.dirname(self.inbox), exist_ok=True)
        if os.path.exists(self.inbox):
            os.unlink(self.inbox)
        self.process = subprocess.Popen(
            [self.exe],
            cwd=self.cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
        )
        self._reader = threading.Thread(target=self._consume, daemon=True)
        self._reader.start()
        if not self._ready.wait(self.startup_timeout):
            tail = self._snapshot()[-30:]
            self.stop()
            raise TimeoutError(
                f"simulator not ready within {self.startup_timeout}s; tail:\n" + "\n".join(tail)
            )
        if (
            self._automation_ready_marker
            and not self._automation_ready.wait(self.startup_timeout)
        ):
            tail = self._snapshot()[-30:]
            self.stop()
            raise TimeoutError(
                "simulator command bridge not ready within "
                f"{self.startup_timeout}s; tail:\n" + "\n".join(tail)
            )
        if (
            self._gui_command_ready_marker
            and not self._gui_command_ready.wait(self.startup_timeout)
        ):
            tail = self._snapshot()[-30:]
            self.stop()
            raise TimeoutError(
                "simulator GUI command consumer not ready within "
                f"{self.startup_timeout}s; tail:\n" + "\n".join(tail)
            )
        deadline = time.monotonic() + self.startup_timeout
        attempt = 0
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            attempt += 1
            try:
                self.send(
                    f":GUI_PING:{900000 + attempt}",
                    request="gui_ping",
                    timeout=min(5.0, max(0.1, deadline - time.monotonic())),
                    expected_type="gui_ack",
                    expected_status="processed",
                )
                break
            except TimeoutError as exc:
                # The shell can become ready slightly before the GUI message
                # consumer is registered. GUI_PING is idempotent, so retry it
                # until the shared startup deadline instead of waiting on one
                # command that was accepted too early.
                last_error = exc
        else:
            self.stop()
            raise TimeoutError(
                f"simulator GUI did not acknowledge startup after {attempt} attempts"
            ) from last_error

    def _consume(self) -> None:
        assert self.process and self.process.stdout
        for raw in iter(self.process.stdout.readline, ""):
            line = raw.rstrip("\r\n")
            with self._lock:
                self._lines.append(line)
            if any(m in line for m in READY_MARKERS):
                self._ready.set()
            if (
                self._automation_ready_marker
                and self._automation_ready_marker in line
            ):
                self._automation_ready.set()
            if (
                self._gui_command_ready_marker
                and self._gui_command_ready_marker in line
            ):
                self._gui_command_ready.set()

    def _snapshot(self) -> list[str]:
        with self._lock:
            return list(self._lines)

    def lines_since(self, start_index: int) -> list[str]:
        """返回某条命令发出后至今的输出，供系统观察收集异步外设事件。"""
        return self._snapshot()[start_index:]

    def _write(self, command: str) -> None:
        tmp = f"{self.inbox}.{os.getpid()}.{time.time_ns()}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(f"{command}\n")
        os.replace(tmp, self.inbox)

    def send(
        self,
        content: str,
        *,
        request: str | None = None,
        timeout: float | None = None,
        expected_type: str | None = None,
        expected_status: str | None = None,
    ) -> CommandResult:
        """发送一条裸 hlq 命令（如 ":GUI_PING:1"），等待并返回 JSON 回执。

        content 约定：带前导冒号的裸命令，如 ":GUI_PING:1"、":GUI_TREE:1"、
        ":TP_CLICK:200,120,0"。本方法内部包装为
        srv_quick_cmd send "TOP5STEP:<CMD>:<PARAM>;" 写入 inbox。

        expected_type/expected_status 用于等待异步完成回执。例如 GUI_PING 应等待
        gui_ack/processed，而不是把前置 command_result/accepted 当成完成。未传入时
        保持原有行为，返回第一个受支持的回执。
        """
        if self.process is None:
            raise RuntimeError("session not started")
        if request is None:
            parts = content.split(":")
            request = parts[1].lower() if len(parts) >= 2 else content.lower()
        timeout = timeout if timeout is not None else self.cmd_timeout
        inner = content.lstrip(":")
        wire = f'srv_quick_cmd send "TOP5STEP:{inner};"'
        start_idx = len(self._snapshot())
        self._write(wire)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            new_lines = self._snapshot()[start_idx:]
            for line in new_lines:
                for obj in extract_json_objects(line):
                    if obj.get("request") != request:
                        continue

                    response_type = obj.get("type")
                    status = obj.get("status", "")
                    if expected_type is None and expected_status is None:
                        matched = response_type in (
                            "command_result",
                            "gui_state",
                            "gui_tree_end",
                        )
                    else:
                        type_matched = expected_type is None or response_type == expected_type
                        status_matched = expected_status is None or status == expected_status
                        matched = type_matched and status_matched

                        # accepted 只表示命令已入队；等待 GUI 完成时继续读取后续回执。
                        # 同类型的其他状态，以及其他 command_result，都是终态错误或
                        # 不可用状态，应立即交给调用方而不是等到超时。
                        if (
                            not matched
                            and (type_matched or response_type == "command_result")
                            and status != "accepted"
                        ):
                            matched = True

                    if matched:
                        return CommandResult(
                            request=obj.get("request", ""),
                            status=status,
                            raw=obj,
                            lines=new_lines,
                            start_index=start_idx,
                        )
            time.sleep(0.05)
        raise TimeoutError(
            f"no result for {content} (request={request}) within {timeout}s; tail:\n"
            + "\n".join(self._snapshot()[-30:])
        )

    def capture_screenshot(self, output_path: str) -> bool:
        """捕获模拟器窗口为 BMP 文件。须在 session.start() 之后调用。"""
        if not self.process:
            return False
        hwnd = _find_window_by_pid(self.process.pid)
        if not hwnd:
            return False
        return _capture_window_bmp(hwnd, output_path)

    def stop(self) -> None:
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=3)
        try:
            os.unlink(self.inbox)
        except FileNotFoundError:
            pass


if __name__ == "__main__":
    from agent_loop_system.runtime_root import RuntimePaths

    _EXE = str(
        RuntimePaths.from_root().firmware_workspaces
        / "620C_W6830"
        / "core"
        / "gui"
        / "simulator"
        / "bin"
        / "main.exe"
    )
    _sess = SimulatorSession(_EXE)
    try:
        print("[probe] starting simulator...")
        _sess.start()
        print("[probe] ready. sending :GUI_PING:1")
        _r = _sess.send(":GUI_PING:1")
        print(f"[probe] gui_ping -> status={_r.status} raw={_r.raw}")
        print("[probe] sending :GUI_STATE:1")
        _r = _sess.send(":GUI_STATE:1", request="gui_state", timeout=8)
        print(f"[probe] gui_state -> status={_r.status}")
        print(json.dumps(_r.raw, ensure_ascii=False, indent=2))
    finally:
        _sess.stop()
        print("[probe] stopped.")
