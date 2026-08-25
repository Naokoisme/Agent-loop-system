"""Agent-loop CaseSession adapter for the shared 579 BLE Broker."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from agent_loop_system.tools.case_map import CaseEntry, CaseRunResult, run_case
from agent_loop_system.tools.command_protocol import normalize_command
from agent_loop_system.tools.simulator import CommandResult
from agent_loop_system.tools.watch_579_protocol import (
    Watch579ProtocolError,
    format_hex,
    top5step_command,
)


INTERNAL_BASE_URL_ENV = "AGENT_LOOP_INTERNAL_BASE_URL"
LEASE_TOKEN_ENV = "WATCH_579_LEASE_TOKEN"

_SEMANTIC_COMMANDS = {
    "BUTTON_PRESS",
    "TP_CLICK",
    "TP_SWIPE",
    "SWIPE_SIM",
    "SCROLL_PAGE",
}


class Watch579CaseSession:
    """Translate stable semantic commands and call the app-owned Broker."""

    requires_gui_ping_barrier = False
    observation_available = False
    observation_unavailable_reason = "579 BLE 截图通道尚不可用"

    def __init__(
        self,
        *,
        base_url: str | None = None,
        lease_token: str | None = None,
        request_timeout: float = 15.0,
    ) -> None:
        self.base_url = str(
            base_url or os.environ.get(INTERNAL_BASE_URL_ENV, "")
        ).strip().rstrip("/")
        self.lease_token = str(
            lease_token or os.environ.get(LEASE_TOKEN_ENV, "")
        ).strip()
        self.request_timeout = float(request_timeout)
        self._started = False

    def start(self) -> None:
        if not self.base_url:
            raise RuntimeError(f"{INTERNAL_BASE_URL_ENV} is not configured")
        if not self.lease_token:
            raise RuntimeError(f"{LEASE_TOKEN_ENV} is not configured")
        self._started = True

    def stop(self) -> None:
        # The frontend process owns and intentionally keeps the GATT connection.
        self._started = False

    @staticmethod
    def _raw_payload(raw: str) -> dict[str, str]:
        command = normalize_command(raw)
        name, _, args = command[1:].partition(":")
        if name in _SEMANTIC_COMMANDS:
            # Preserve the known 52-byte calculator-click wire form.  Other
            # TOP5STEP commands use the canonical form without the legacy space.
            semantic = f":{name}:{args}{' ' if name == 'TP_CLICK' else ''};"
            encoded = top5step_command(semantic)
            return {
                "cmd": encoded.cmd_hex,
                "key": encoded.key_hex,
                "data": format_hex(encoded.data),
            }
        if name == "W579_RAW":
            parts = args.split(",", 2)
            if len(parts) not in (2, 3) or not parts[0] or not parts[1]:
                raise ValueError("W579_RAW expects cmd,key[,data_hex]")
            return {
                "cmd": parts[0].strip(),
                "key": parts[1].strip(),
                "data": parts[2].strip() if len(parts) == 3 else "",
            }
        raise ValueError(f"579 CaseSession does not support command {name}")

    def send(self, content: str, **_kwargs: Any) -> CommandResult:
        if not self._started:
            raise RuntimeError("579 CaseSession has not started")
        try:
            payload = self._raw_payload(content)
        except Watch579ProtocolError as exc:
            raise ValueError(str(exc)) from exc
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = Request(
            f"{self.base_url}/api/internal/hardware/579/send",
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "X-Agent-Loop-579-Lease": self.lease_token,
            },
        )
        try:
            with urlopen(request, timeout=self.request_timeout) as response:
                result = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            try:
                error = json.loads(exc.read().decode("utf-8"))
            except Exception:
                error = {}
            reason_code = str(error.get("reason_code") or "BLE_REQUEST_FAILED")
            message = str(error.get("error") or exc.reason)
            raise RuntimeError(f"{reason_code}: {message}") from exc
        except (OSError, URLError, TimeoutError) as exc:
            raise RuntimeError(f"BLE_BROKER_UNAVAILABLE: {exc}") from exc
        if not result.get("transport_acked"):
            raise RuntimeError("BLE_ACK_TIMEOUT: Broker did not return an L1 ACK")
        terminal = {
            "type": "watch_579_ble_ack",
            "status": "accepted",
            **result,
        }
        return CommandResult(
            request=content,
            status="accepted",
            raw=terminal,
            lines=[json.dumps(terminal, ensure_ascii=False)],
        )

    def capture_screenshot(self, output_path: str) -> bool:
        del output_path
        return False


def run_watch_579_case(
    case: CaseEntry,
    screenshot_path: str,
    *,
    base_url: str | None = None,
    lease_token: str | None = None,
) -> CaseRunResult:
    if case.mapping_status == "BLOCKED":
        reason_code = case.block_reason_code or "WATCH_579_EXECUTION_BLOCKED"
        raise ValueError(
            f"{reason_code}: current 579 firmware entry route is not executable"
        )
    if case.mapping_status not in {"EXECUTION_READY", "PROMOTED"}:
        raise ValueError(
            f"579 fixed Runner requires EXECUTION_READY mapping, got {case.mapping_status or 'empty'}"
        )
    session = Watch579CaseSession(
        base_url=base_url,
        lease_token=lease_token,
    )
    session.start()
    try:
        return run_case(
            session,
            case,
            screenshot_path=Path(screenshot_path),
        )
    finally:
        session.stop()


__all__ = [
    "INTERNAL_BASE_URL_ENV",
    "LEASE_TOKEN_ENV",
    "Watch579CaseSession",
    "run_watch_579_case",
]
