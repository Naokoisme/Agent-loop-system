from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from threading import Event
from typing import Any, Callable

from .serial_readonly import WatchSerialReadonly


class WatchObserver:
    """O1/O2 evidence provider backed only by read methods."""

    def __init__(
        self,
        *,
        port: str | None = None,
        baud: int | None = None,
        width: int | None = None,
        height: int | None = None,
        serial_factory: Callable[[str, int], Any] | None = None,
    ):
        self.port = str(port or os.environ.get("PLATFORM_579_COM_PORT", "COM3")).strip()
        self.baud = int(baud or os.environ.get("PLATFORM_579_COM_BAUDRATE", "1500000"))
        self.width = int(width or os.environ.get("PLATFORM_579_SCREEN_WIDTH", "410"))
        self.height = int(height or os.environ.get("PLATFORM_579_SCREEN_HEIGHT", "502"))
        self._serial_factory = serial_factory or (lambda port, baud: WatchSerialReadonly(port, baud))

    def _open(self):
        return self._serial_factory(self.port, self.baud)

    @staticmethod
    def _sha(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest().upper()

    def observe_o1(
        self,
        *,
        artifact_dir: Path,
        name: str,
        seconds: float,
        patterns: list[str] | None = None,
        ready_event: Event | None = None,
    ) -> dict[str, Any]:
        serial = None
        try:
            artifact_dir.mkdir(parents=True, exist_ok=True)
            serial = self._open()
            if ready_event is not None:
                ready_event.set()
            observed = (
                serial.observe_patterns(patterns, timeout=seconds)
                if patterns else serial.observe_window(seconds)
            )
            lines = [str(value) for value in observed.get("lines", [])]
            log_path = artifact_dir / f"{name}.o1.log.txt"
            log_path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
            summary = {key: value for key, value in observed.items() if key != "lines"}
            summary_path = artifact_dir / f"{name}.o1.json"
            summary_path.write_text(
                json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            return {
                **summary,
                "kind": "o1_log_observation",
                "channel": "com3_readonly",
                "log_path": str(log_path),
                "log_sha256": self._sha(log_path),
                "summary_path": str(summary_path),
            }
        except Exception as exc:
            if ready_event is not None:
                ready_event.set()
            return {
                "ok": False,
                "kind": "o1_log_observation",
                "channel": "com3_readonly",
                "reason": f"O1 只读观察异常: {exc}",
            }
        finally:
            if serial is not None:
                serial.close()

    def capture_o2(
        self,
        *,
        artifact_dir: Path,
        name: str,
        timeout: float,
        ready_event: Event | None = None,
    ) -> dict[str, Any]:
        serial = None
        try:
            artifact_dir.mkdir(parents=True, exist_ok=True)
            serial = self._open()
            if ready_event is not None:
                ready_event.set()
            hex_text = serial.capture_screenshot_hex(timeout)
            if not hex_text:
                return {
                    "ok": False,
                    "kind": "o2_screenshot",
                    "channel": "com3_readonly",
                    "freshness_verified": False,
                    "reason": "未观察到新的 O2 截图帧",
                }
            hex_path = artifact_dir / f"{name}.o2.txt"
            image_path = artifact_dir / f"{name}.bmp"
            hex_path.write_text(hex_text, encoding="utf-8")
            if not serial.decode_rgb565(hex_text, self.width, self.height, image_path):
                return {
                    "ok": False,
                    "kind": "o2_screenshot",
                    "channel": "com3_readonly",
                    "freshness_verified": False,
                    "hex_path": str(hex_path),
                    "reason": "O2 RGB565 像素长度或解码结果不合法",
                }
            return {
                "ok": True,
                "kind": "o2_screenshot",
                "channel": "com3_readonly",
                "freshness_verified": True,
                "image_path": str(image_path),
                "image_sha256": self._sha(image_path),
                "hex_path": str(hex_path),
                "width": self.width,
                "height": self.height,
            }
        except Exception as exc:
            if ready_event is not None:
                ready_event.set()
            return {
                "ok": False,
                "kind": "o2_screenshot",
                "channel": "com3_readonly",
                "freshness_verified": False,
                "reason": f"O2 只读观察异常: {exc}",
            }
        finally:
            if serial is not None:
                serial.close()
