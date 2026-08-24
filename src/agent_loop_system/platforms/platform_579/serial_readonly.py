from __future__ import annotations

import re
import time
from pathlib import Path


SHOT_START = "screen_shot_print start"
SHOT_END = "screen_shot_print end"
HEX_BYTE = re.compile(r"^[0-9A-Fa-f]{2}$")


class WatchSerialReadonly:
    """COM3 observation handle. Intentionally exposes no write/send/flush API."""

    def __init__(self, port: str, baud: int = 1_500_000, read_timeout: float = 0.2):
        import serial

        self._serial = serial.Serial(
            port=port,
            baudrate=baud,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            timeout=read_timeout,
        )
        self._raw = b""

    def close(self) -> None:
        try:
            self._serial.close()
        except Exception:
            pass

    def _readline(self, deadline: float) -> str | None:
        while time.monotonic() < deadline:
            index = self._raw.find(b"\n")
            if index >= 0:
                line = self._raw[:index]
                self._raw = self._raw[index + 1:]
                return line.rstrip(b"\r").decode("utf-8", errors="replace")
            chunk = self._serial.read(4096)
            if chunk:
                self._raw += chunk
        return None

    def observe_window(self, seconds: float, max_lines: int = 6000) -> dict:
        deadline = time.monotonic() + max(0.1, float(seconds))
        lines: list[str] = []
        total = 0
        while time.monotonic() < deadline:
            line = self._readline(deadline)
            if line is None:
                break
            total += 1
            if len(lines) < max_lines:
                lines.append(line)
        return {"ok": total > 0, "lines": lines, "line_count": total}

    def observe_patterns(self, patterns: list[str], timeout: float) -> dict:
        wanted = [str(value) for value in patterns if str(value)]
        deadline = time.monotonic() + max(0.1, float(timeout))
        matched: list[str] = []
        lines: list[str] = []
        cursor = 0
        while time.monotonic() < deadline and cursor < len(wanted):
            line = self._readline(deadline)
            if line is None:
                break
            lines.append(line)
            while cursor < len(wanted) and wanted[cursor] in line:
                matched.append(wanted[cursor])
                cursor += 1
        return {
            "ok": cursor == len(wanted),
            "patterns": wanted,
            "matched_patterns": matched,
            "missing_patterns": wanted[cursor:],
            "lines": lines,
            "line_count": len(lines),
        }

    def capture_screenshot_hex(self, timeout: float) -> str | None:
        deadline = time.monotonic() + max(0.1, float(timeout))
        capturing = False
        lines: list[str] = []
        while time.monotonic() < deadline:
            line = self._readline(deadline)
            if line is None:
                return None
            if SHOT_START in line:
                capturing = True
                lines = []
            elif capturing and SHOT_END in line:
                return "\n".join(lines)
            elif capturing:
                lines.append(line)
        return None

    @staticmethod
    def decode_rgb565(hex_text: str, width: int, height: int, output_path: Path) -> bool:
        from PIL import Image
        import numpy as np

        raw = bytes(int(token, 16) for token in hex_text.split() if HEX_BYTE.fullmatch(token))
        if len(raw) != width * height * 2:
            return False
        values = np.frombuffer(raw, dtype="<u2").reshape(height, width)
        red = ((values >> 11) & 0x1F)
        green = ((values >> 5) & 0x3F)
        blue = (values & 0x1F)
        rgb = np.dstack([
            (red << 3) | (red >> 2),
            (green << 2) | (green >> 4),
            (blue << 3) | (blue >> 2),
        ]).astype("uint8")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        Image.fromarray(rgb, "RGB").save(output_path)
        return True
