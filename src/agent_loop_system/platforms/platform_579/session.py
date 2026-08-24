from __future__ import annotations

import json
import re
import shutil
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping


CAP_PREFIX = ":CAP_ACTION:"
ALIAS = re.compile(r"[a-z0-9][a-z0-9._-]{0,95}")


def normalize_cap_action(value: str) -> str:
    if not isinstance(value, str) or "\n" in value or "\r" in value:
        raise ValueError("579 探索命令必须是一行字符串")
    command = value.strip()
    if not command.startswith(CAP_PREFIX):
        raise ValueError("579 探索只接受 :CAP_ACTION:<verified-alias>")
    alias = command[len(CAP_PREFIX):]
    if not ALIAS.fullmatch(alias):
        raise ValueError(f"579 能力别名非法: {alias!r}")
    return command


@dataclass(frozen=True)
class ActionBinding579:
    alias: str
    description: str
    cmd_id: int
    key_id: int
    data_hex: str
    o1_patterns: tuple[str, ...] = ()
    o1_seconds: float = 2.0

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ActionBinding579":
        alias = str(value.get("alias") or "")
        if not ALIAS.fullmatch(alias):
            raise ValueError("579 binding alias 非法")
        if str(value.get("status") or "").lower() != "verified":
            raise ValueError(f"579 能力 {alias} 尚未 verified")
        if value.get("transport") != "app_ble":
            raise ValueError(f"579 能力 {alias} transport 必须为 app_ble")
        constraints = value.get("constraints") or {}
        if constraints.get("serial_write") is not False:
            raise ValueError(f"579 能力 {alias} 必须声明 serial_write=false")
        evidence = value.get("evidence") or []
        if not evidence:
            raise ValueError(f"579 能力 {alias} verified 但缺少 evidence")
        payload = value.get("payload") or value
        return cls(
            alias=alias,
            description=str(value.get("description") or alias),
            cmd_id=int(str(payload.get("cmd_id") or "0"), 0),
            key_id=int(str(payload.get("key_id") or "0"), 0),
            data_hex=str(payload.get("data_hex") or ""),
            o1_patterns=tuple(str(item) for item in payload.get("o1_patterns", []) if str(item)),
            o1_seconds=float(payload.get("o1_seconds") or 2.0),
        )


@dataclass
class SessionResult:
    request: str
    status: str
    raw: dict[str, Any]
    lines: list[str] = field(default_factory=list)
    start_index: int | None = None


class Platform579Session:
    """Agent-facing alias session. Concrete bytes never cross its command API."""

    def __init__(
        self,
        *,
        transport: Any,
        observer: Any,
        bindings: Mapping[str, ActionBinding579 | Mapping[str, Any]],
        artifact_dir: Path,
        allow_device_actions: bool = False,
    ):
        self.transport = transport
        self.observer = observer
        self.bindings = {
            alias: binding if isinstance(binding, ActionBinding579) else ActionBinding579.from_mapping(binding)
            for alias, binding in bindings.items()
        }
        self.artifact_dir = Path(artifact_dir)
        self.allow_device_actions = bool(allow_device_actions)
        self.last_capture_metadata: dict[str, Any] | None = None
        self._lines: list[str] = []
        self._started = False

    @property
    def capability_knowledge(self) -> str:
        lines = [
            "579 只允许使用已验证的 :CAP_ACTION:<alias>；不得输出 raw 参数或写串口。"
        ]
        lines.extend(
            f"- {CAP_PREFIX}{alias} | {binding.description}"
            for alias, binding in sorted(self.bindings.items())
        )
        return "\n".join(lines)

    def start(self) -> None:
        if self._started:
            raise RuntimeError("579 探索会话已启动")
        self._started = True

    def stop(self) -> None:
        self._started = False

    def lines_since(self, start_index: int) -> list[str]:
        return self._lines[max(0, int(start_index)):]

    def validate_command(self, command: str) -> None:
        alias = normalize_cap_action(command)[len(CAP_PREFIX):]
        if alias not in self.bindings:
            raise ValueError(f"579 能力别名未注册或未验证: {alias}")

    def _append(self, request: str, status: str, raw: dict[str, Any], start: int) -> SessionResult:
        self._lines.append(json.dumps(raw, ensure_ascii=False, sort_keys=True))
        return SessionResult(request, status, raw, self.lines_since(start), start)

    def send(self, content: str, **kwargs: Any) -> SessionResult:
        if not self._started:
            raise RuntimeError("579 探索会话尚未启动")
        request = str(kwargs.get("request") or content)
        start = len(self._lines)
        if content.startswith(":GUI_PING:"):
            return self._append(request, "processed", {
                "type": "gui_ack", "status": "processed", "platform": "579"
            }, start)
        if content.startswith(":GUI_STATE:") or content.startswith(":GUI_TREE:"):
            kind = "gui_state" if "STATE" in content else "gui_tree_end"
            return self._append(request, "unavailable", {
                "type": kind,
                "status": "unavailable",
                "platform": "579",
                "reason": "579 以 COM3/O1 和 O2 截图观察，不提供 W30 GUI_TREE/STATE",
            }, start)
        try:
            command = normalize_cap_action(content)
            self.validate_command(command)
        except ValueError as exc:
            return self._append(request, "rejected", {
                "type": "platform_action", "status": "rejected", "reason": str(exc)
            }, start)
        alias = command[len(CAP_PREFIX):]
        if not self.allow_device_actions:
            return self._append(request, "unavailable", {
                "type": "platform_action",
                "status": "unavailable",
                "platform": "579",
                "alias": alias,
                "reason": "579 实机动作默认关闭",
                "device_action_count": 0,
            }, start)
        binding = self.bindings[alias]
        ready = threading.Event()
        observed: dict[str, Any] = {}

        def observe() -> None:
            observed.update(self.observer.observe_o1(
                artifact_dir=self.artifact_dir,
                name=f"agent_{alias.replace('.', '_')}",
                seconds=binding.o1_seconds,
                patterns=list(binding.o1_patterns),
                ready_event=ready,
            ))

        thread = threading.Thread(target=observe, daemon=True)
        thread.start()
        if not ready.wait(3):
            return self._append(request, "unavailable", {
                "type": "platform_action", "status": "unavailable",
                "reason": "O1 未在业务动作前就绪", "device_action_count": 0,
            }, start)
        delivery = self.transport.raw_command(
            binding.cmd_id,
            binding.key_id,
            binding.data_hex,
            logical_action=alias,
        )
        thread.join(binding.o1_seconds + 5)
        delivery_status = str(delivery.get("delivery_status") or "FAILED")
        status = "processed" if delivery_status == "CONFIRMED" else "accepted" if delivery_status == "UNCONFIRMED" else "error"
        return self._append(request, status, {
            "type": "platform_action",
            "status": status,
            "platform": "579",
            "alias": alias,
            "delivery_status": delivery_status,
            "o1_observation": observed,
            "device_action_count": int(delivery.get("device_action_count") or 0),
        }, start)

    def capture_screenshot(self, output_path: str) -> bool:
        self.last_capture_metadata = None
        if not self._started or not self.allow_device_actions:
            return False
        ready = threading.Event()
        captured: dict[str, Any] = {}

        def observe() -> None:
            captured.update(self.observer.capture_o2(
                artifact_dir=self.artifact_dir,
                name=f"agent_{len(self._lines):04d}",
                timeout=65,
                ready_event=ready,
            ))

        thread = threading.Thread(target=observe, daemon=True)
        thread.start()
        if not ready.wait(3):
            return False
        trigger = self.transport.trigger_screenshot()
        thread.join(70)
        fresh = bool(
            trigger.get("delivery_status") in {"CONFIRMED", "UNCONFIRMED"}
            and captured.get("ok") and captured.get("freshness_verified")
        )
        self.last_capture_metadata = {
            "platform": "579",
            "freshness_verified": fresh,
            "bridge_delivery_status": trigger.get("delivery_status"),
            "o2_capture_ok": bool(captured.get("ok")),
            "serial_write": False,
        }
        source = Path(str(captured.get("image_path") or ""))
        if not fresh or not source.is_file():
            return False
        destination = Path(output_path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        return destination.is_file() and destination.stat().st_size > 0


def build_agent_loop_runtime(session: Platform579Session):
    from agent_loop_system.exploration_core import PlatformExplorationRuntime

    return PlatformExplorationRuntime(
        platform_id="579",
        target_label="579 O2 真机",
        session=session,
        capabilities=dict(session.bindings),
        capability_knowledge=session.capability_knowledge,
        normalize_command=normalize_cap_action,
        validate_command=session.validate_command,
        platform_guidance=(
            "只允许 CAP_ACTION；控制链必须为 ADB→APP Bridge→BLE；"
            "COM3 严格只读；APP 回执不能替代 O1/O2 产品证据。"
        ),
    )
