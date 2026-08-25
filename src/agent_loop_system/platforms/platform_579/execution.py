from __future__ import annotations

import shutil
import threading
import time
from pathlib import Path
from typing import Any, Callable

from agent_loop_system.platforms.contracts import PreflightResult, RunRequest

from .catalog import Platform579Catalog
from .gates import validate_private_plan
from .health import Platform579HealthProvider
from .observation import WatchObserver
from .policy import infrastructure_for
from .transport import AppBleTransport, Platform579TransportConfig


def _public_delivery(result: dict[str, Any]) -> dict[str, Any]:
    """Keep APP feedback while removing private raw protocol and command output."""
    parsed = result.get("parsed")
    allowed_parsed = {}
    if isinstance(parsed, dict):
        for key in ("ok", "written", "write_ok", "ble_write_ok", "bytes_written", "error", "message"):
            if key in parsed:
                allowed_parsed[key] = parsed[key]
    return {
        "ok": bool(result.get("ok")),
        "supported": result.get("supported") is not False,
        "delivery_status": str(result.get("delivery_status") or "FAILED"),
        "reason": result.get("reason"),
        "parsed": allowed_parsed,
        "device_action_count": int(result.get("device_action_count") or 0),
    }


class Platform579Gateway:
    """Deterministic 579 runner with fail-closed gates and evidence separation."""

    def __init__(
        self,
        *,
        catalog: Platform579Catalog | None = None,
        transport: Any | None = None,
        observer: Any | None = None,
        health: Platform579HealthProvider | None = None,
        judge: Callable[[str, list[dict[str, object]], list[str] | None], Any] | None = None,
        sleeper: Callable[[float], None] = time.sleep,
    ):
        self.catalog = catalog or Platform579Catalog()
        self.transport = transport or AppBleTransport()
        self.observer = observer or WatchObserver()
        self.health = health or Platform579HealthProvider(
            catalog=self.catalog,
            config=getattr(self.transport, "config", Platform579TransportConfig.from_env()),
        )
        if judge is None:
            from agent_loop_system.tools.test import judge_test_with_vision
            judge = judge_test_with_vision
        self.judge = judge
        self.sleeper = sleeper
        self._cancelled: set[str] = set()

    def preflight(self, request: RunRequest) -> PreflightResult:
        if request.platform_id != "579" or request.target_id != "579.o2":
            return PreflightResult(False, "ENV_BLOCKED", ("579 网关只接受 target 579.o2",))
        blockers: list[str] = []
        for case_id in request.case_ids:
            try:
                plan = self.catalog.private_plan(case_id)
                blockers.extend(validate_private_plan(plan))
            except ValueError as exc:
                blockers.append(str(exc))
        health = self.health.inspect()
        if health.get("readiness_status") != "ready":
            blockers.extend(
                str(check.get("detail") or check.get("label"))
                for check in health.get("checks", [])
                if check.get("status") != "pass"
            )
        return PreflightResult(
            ready=not blockers,
            infrastructure_status="READY" if not blockers else "ENV_BLOCKED",
            blockers=tuple(dict.fromkeys(blockers)),
            details={
                "target_id": "579.o2",
                "control_path": "ADB → APP Bridge → BLE",
                "serial_write": False,
                "health": health,
            },
        )

    def cancel(self, run_id: str) -> dict[str, Any]:
        self._cancelled.add(str(run_id))
        return {"status": "cancel_requested", "run_id": str(run_id)}

    def _observe_o1_around_action(
        self,
        *,
        artifact_dir: Path,
        evidence_name: str,
        seconds: float,
        patterns: list[str],
        send: Callable[[], dict[str, Any]],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        ready = threading.Event()
        container: dict[str, Any] = {}

        def observe() -> None:
            container["result"] = self.observer.observe_o1(
                artifact_dir=artifact_dir,
                name=evidence_name,
                seconds=seconds,
                patterns=patterns,
                ready_event=ready,
            )

        thread = threading.Thread(target=observe, daemon=True, name=f"579-o1-{evidence_name}")
        thread.start()
        if not ready.wait(timeout=3):
            return {"ok": False, "delivery_status": "BLOCKED", "reason": "O1 未在业务动作前就绪"}, {
                "ok": False, "reason": "O1 未在业务动作前就绪", "channel": "com3_readonly"
            }
        delivery = send()
        thread.join(timeout=seconds + 5)
        observation = container.get("result") or {
            "ok": False, "reason": "O1 观察线程超时", "channel": "com3_readonly"
        }
        return delivery, observation

    def _capture_assertion(
        self,
        *,
        artifact_dir: Path,
        name: str,
        expected: str,
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        ready = threading.Event()
        container: dict[str, Any] = {}

        def observe() -> None:
            container["result"] = self.observer.capture_o2(
                artifact_dir=artifact_dir,
                name=name,
                timeout=65,
                ready_event=ready,
            )

        thread = threading.Thread(target=observe, daemon=True, name=f"579-o2-{name}")
        thread.start()
        if not ready.wait(timeout=3):
            return (
                {"ok": False, "delivery_status": "BLOCKED", "reason": "O2 未在截图触发前就绪"},
                {"ok": False, "freshness_verified": False, "reason": "O2 未在截图触发前就绪"},
                {"verdict": "CANNOT_VERIFY", "reason": "O2 未在截图触发前就绪"},
            )
        trigger = dict(self.transport.trigger_screenshot() or {})
        thread.join(timeout=70)
        capture = dict(container.get("result") or {
            "ok": False, "freshness_verified": False, "reason": "O2 观察线程超时"
        })
        delivery_seen = trigger.get("delivery_status") in {"CONFIRMED", "UNCONFIRMED"}
        if not delivery_seen or not capture.get("ok") or not capture.get("freshness_verified"):
            return trigger, capture, {
                "verdict": "CANNOT_VERIFY",
                "reason": str(capture.get("reason") or trigger.get("reason") or "截图证据不完整"),
            }
        image_path = str(capture.get("image_path") or "")
        decision = self.judge(
            expected,
            [{"path": image_path, "label": expected}],
            [expected],
        )
        return trigger, capture, {
            "verdict": str(getattr(decision, "verdict", "CANNOT_VERIFY")),
            "reason": str(getattr(decision, "reason", "视觉判定未返回理由")),
        }

    def run_case(
        self,
        *,
        case: dict[str, Any],
        artifact_dir: Path,
        run_id: str = "",
    ) -> dict[str, Any]:
        case_id = str(case.get("case_id") or "")
        plan = self.catalog.private_plan(case_id)
        issues = validate_private_plan(plan)
        if issues:
            raise ValueError("; ".join(issues))
        artifact_dir = Path(artifact_dir)
        artifact_dir.mkdir(parents=True, exist_ok=True)
        trace: list[dict[str, Any]] = []
        deliveries: list[dict[str, Any]] = []
        observations: list[dict[str, Any]] = []
        screenshots: list[dict[str, Any]] = []
        decisions: list[dict[str, str]] = []
        execution_errors: list[str] = []
        cleanup_errors: list[str] = []
        cancelled = False

        def execute_items(phase: str, values: list[dict[str, Any]]) -> bool:
            nonlocal cancelled
            for index, item in enumerate(values, start=1):
                if run_id and run_id in self._cancelled:
                    cancelled = True
                    execution_errors.append("用户取消了 579 任务")
                    return False
                item_type = item.get("type")
                alias = f"{phase}.{index:03d}.{item_type}"
                if item_type == "wait":
                    duration = min(120.0, max(0.0, float(item.get("timeout") or 0)))
                    self.sleeper(duration)
                    trace.append({"phase": phase, "index": index, "kind": "wait", "duration_seconds": duration, "status": "completed"})
                    continue
                if item_type == "step":
                    args = item.get("args") or {}
                    source_plan = args.get("source_plan") or {}
                    expect = source_plan.get("o1_expect") or {}
                    patterns = [str(value) for value in expect.get("patterns", []) if str(value)]
                    seconds = float(expect.get("timeout") or 2.0)
                    evidence_name = str(expect.get("evidence_name") or alias).replace("/", "_")

                    def send() -> dict[str, Any]:
                        return dict(self.transport.raw_command(
                            int(str(args.get("cmd_id") or "0"), 0),
                            int(str(args.get("key_id") or "0"), 0),
                            str(args.get("data_hex") or ""),
                            logical_action=alias,
                        ) or {})

                    delivery, observation = self._observe_o1_around_action(
                        artifact_dir=artifact_dir,
                        evidence_name=evidence_name,
                        seconds=min(30.0, max(0.1, seconds)),
                        patterns=patterns,
                        send=send,
                    )
                    public = _public_delivery(delivery)
                    deliveries.append(public)
                    observations.append(observation)
                    delivery_seen = public["delivery_status"] in {"CONFIRMED", "UNCONFIRMED"}
                    trace.append({
                        "phase": phase,
                        "index": index,
                        "kind": "platform_action",
                        "alias": alias,
                        "transport": "app_ble",
                        "delivery_status": public["delivery_status"],
                        "o1_ok": bool(observation.get("ok")),
                        "status": "completed" if delivery_seen else "failed",
                    })
                    if not delivery_seen:
                        (cleanup_errors if phase == "teardown" else execution_errors).append(
                            str(public.get("reason") or "APP Bridge 交付失败")
                        )
                        return False
                    continue
                if item_type == "assert":
                    expected = str(item.get("expected") or "").strip()
                    evidence_name = str(item.get("evidence_name") or alias).replace("/", "_")
                    trigger, capture, decision = self._capture_assertion(
                        artifact_dir=artifact_dir,
                        name=evidence_name,
                        expected=expected,
                    )
                    deliveries.append(_public_delivery(trigger))
                    observations.append(capture)
                    decisions.append({**decision, "phase": phase})
                    if capture.get("image_path"):
                        screenshots.append({
                            "index": len(screenshots) + 1,
                            "label": str(item.get("label") or expected),
                            "phase": phase,
                            "command": "",
                            "path": str(capture["image_path"]),
                            "captured_at": time.time(),
                        })
                    trace.append({
                        "phase": phase,
                        "index": index,
                        "kind": "o2_assertion",
                        "evidence_name": evidence_name,
                        "freshness_verified": bool(capture.get("freshness_verified")),
                        "verdict": decision["verdict"],
                        "status": "completed" if decision["verdict"] in {"PASS", "FAIL"} else "incomplete",
                    })
                    if decision["verdict"] == "CANNOT_VERIFY":
                        (cleanup_errors if phase == "teardown" else execution_errors).append(decision["reason"])
                        return False
            return True

        setup_ok = execute_items("setup", list(plan.get("setup") or []))
        steps_ok = setup_ok and execute_items("steps", list(plan.get("steps") or []))
        teardown_ok = execute_items("teardown", list(plan.get("teardown") or []))
        o1_observations = [
            value for value in observations if value.get("kind") == "o1_log_observation"
        ]
        o2_observations = [
            value for value in observations if value.get("kind") == "o2_screenshot"
        ]
        observation_ok = bool(screenshots) and bool(o1_observations) and all(
            bool(value.get("ok")) for value in [*o1_observations, *o2_observations]
        )
        transport_ok = bool(deliveries) and all(
            value.get("delivery_status") in {"CONFIRMED", "UNCONFIRMED"}
            for value in deliveries
        )
        infrastructure = infrastructure_for(
            transport_ok=transport_ok,
            observation_ok=observation_ok,
            cleanup_ok=teardown_ok and not cleanup_errors,
        )
        product_decisions = [
            value for value in decisions
            if value.get("phase") != "teardown"
            and value.get("verdict") in {"PASS", "FAIL", "CANNOT_VERIFY"}
        ]
        if infrastructure != "READY" or not product_decisions:
            verdict = "CANNOT_VERIFY" if infrastructure != "CLEANUP_REQUIRED" else "ERROR"
        elif any(value["verdict"] == "FAIL" for value in product_decisions):
            verdict = "FAIL"
        elif all(value["verdict"] == "PASS" for value in product_decisions):
            verdict = "PASS"
        else:
            verdict = "CANNOT_VERIFY"
        reason = (
            (cleanup_errors or execution_errors)[0]
            if cleanup_errors or execution_errors
            else next((value["reason"] for value in product_decisions if value["verdict"] != "PASS"), "579 用例证据闭环完成")
        )
        primary = artifact_dir / "screenshot.bmp"
        if screenshots:
            source = Path(str(screenshots[-1]["path"]))
            if source.is_file() and source.resolve() != primary.resolve():
                shutil.copy2(source, primary)
        complete = infrastructure == "READY" and verdict in {"PASS", "FAIL"}
        return {
            "schema_version": 2,
            "case_id": case_id,
            "sheet": case.get("file_sheet") or case.get("sheet"),
            "verdict": verdict,
            "reason": reason,
            "product_verdict": "PASS" if verdict == "PASS" else "PRODUCT_FAIL" if verdict == "FAIL" else "CANNOT_VERIFY",
            "automation_maturity": "AUTO_READY",
            "infrastructure_status": infrastructure,
            "execution_mode": "579_deterministic",
            "execution_status": "completed" if complete else "incomplete",
            "provenance": {
                "platform_id": "579",
                "target_id": "579.o2",
                "execution_adapter": "platform_579",
                "plan_sha256": plan["_plan_sha256"],
                "binding_ref": plan.get("automation_case_id"),
                "serial_channel": "com3_readonly",
            },
            "planned_commands": {"setup": [], "action": [], "collect": []},
            "command_trace": trace,
            "delivery_feedback": deliveries,
            "observations": observations,
            "screenshots": screenshots,
            "verification_points": [str(value.get("expected") or "") for value in plan.get("assertions", []) if isinstance(value, dict) and str(value.get("expected") or "")],
            "evidence_contract": {
                "status": "COMPLETE" if complete else "INCOMPLETE",
                "complete": complete,
                "issues": [] if complete else [{"code": infrastructure, "message": reason}],
                "required_screenshots": len(product_decisions),
                "captured_screenshots": len(screenshots),
                "app_feedback_is_product_evidence": False,
                "serial_write": False,
            },
            "setup_errors": execution_errors if not setup_ok else [],
            "action_errors": execution_errors if setup_ok and not steps_ok else [],
            "collect_errors": cleanup_errors,
            "aborted": bool(cancelled or not setup_ok or not steps_ok),
        }
