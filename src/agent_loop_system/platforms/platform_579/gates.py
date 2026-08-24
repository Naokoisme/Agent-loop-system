from __future__ import annotations

from typing import Any


ALLOWED_TYPES = {"step", "wait", "assert"}


def validate_private_plan(plan: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    if str(plan.get("automation_status") or "").upper() != "AUTO_READY":
        issues.append("CASE_NOT_RUNNABLE: maturity is not AUTO_READY")
    policy = plan.get("execution_policy") or {}
    if policy.get("evidence_eligible") is not True:
        issues.append("CASE_NOT_RUNNABLE: plan is not evidence eligible")
    for phase in ("setup", "steps", "teardown"):
        values = plan.get(phase)
        if not isinstance(values, list):
            issues.append(f"579_PLAN_INVALID: {phase} must be a list")
            continue
        for index, item in enumerate(values, start=1):
            if not isinstance(item, dict) or item.get("type") not in ALLOWED_TYPES:
                issues.append(f"579_PLAN_INVALID: {phase}[{index}] type")
                continue
            if item.get("type") == "step":
                if item.get("device") != "app_ble" or item.get("action") != "raw_command":
                    issues.append(f"579_PLAN_INVALID: {phase}[{index}] control path")
            elif item.get("type") == "assert":
                if item.get("src") != "watch_screen" or item.get("op") != "ai_visual_match":
                    issues.append(f"579_PLAN_INVALID: {phase}[{index}] assertion")
            elif item.get("type") == "wait":
                try:
                    timeout = float(item.get("timeout") or 0)
                except (TypeError, ValueError):
                    timeout = -1
                if not 0 <= timeout <= 120:
                    issues.append(f"579_PLAN_INVALID: {phase}[{index}] wait")
    return issues
