from __future__ import annotations

import hashlib
import json
from importlib.resources import files
from pathlib import Path
from typing import Any


class Platform579Catalog:
    """Loads the private frozen plan catalog; never returns raw plans to Web APIs."""

    def __init__(self, data_root: Path | None = None):
        if data_root is None:
            data_root = Path(str(files("agent_loop_system.platform_data"))) / "579"
        self.data_root = Path(data_root)
        self.automation_path = self.data_root / "catalog" / "automation_cases.json"
        self.execution_path = self.data_root / "catalog" / "execution_manifest.json"
        self.binding_path = self.data_root / "bindings" / "action_bindings.v1.json"
        self.import_path = self.data_root / "import_manifest.json"
        self._automation = self._load(self.automation_path)
        self._execution = self._load(self.execution_path)
        self._bindings = self._load(self.binding_path)
        self._plans = {
            str(case.get("case_no") or ""): dict(case)
            for manifest in self._automation.get("manifests", [])
            if isinstance(manifest, dict)
            for case in manifest.get("cases", [])
            if isinstance(case, dict) and str(case.get("case_no") or "")
        }
        self._scheduled = {
            str(value)
            for manifest in self._execution.get("manifests", [])
            if isinstance(manifest, dict)
            for value in manifest.get("case_order", [])
            if str(value)
        }
        self._binding_index = {
            str(item.get("case_id") or ""): dict(item)
            for item in self._bindings.get("cases", [])
            if isinstance(item, dict) and str(item.get("case_id") or "")
        }

    @staticmethod
    def _load(path: Path) -> dict[str, Any]:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError(f"579 资产不是 JSON 对象: {path}")
        return payload

    @staticmethod
    def _plan_digest(plan: dict[str, Any]) -> str:
        payload = {
            key: plan.get(key)
            for key in (
                "automation_case_id", "setup", "steps", "assertions", "teardown",
                "execution_policy", "state_contract", "required_capabilities",
            )
        }
        return hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest().upper()

    def private_plan(self, case_id: str) -> dict[str, Any]:
        case_id = str(case_id).strip()
        try:
            plan = dict(self._plans[case_id])
            binding = self._binding_index[case_id]
        except KeyError as exc:
            raise ValueError(f"CASE_PLATFORM_MAPPING_MISSING: {case_id}") from exc
        digest = self._plan_digest(plan)
        if digest != str(binding.get("plan_sha256") or "").upper():
            raise ValueError(f"579_PLAN_SHA256_MISMATCH: {case_id}")
        maturity = str(plan.get("automation_status") or "UNMAPPED").upper()
        if maturity != "AUTO_READY" or case_id not in self._scheduled or not binding.get("runnable"):
            blockers = "; ".join(str(value) for value in plan.get("blockers", []) if str(value))
            raise ValueError(f"CASE_NOT_RUNNABLE: {case_id} {maturity} {blockers}".strip())
        plan["_plan_sha256"] = digest
        return plan

    def audit(self) -> dict[str, Any]:
        imported = self._load(self.import_path)
        issues: list[str] = []
        for case_id in sorted(self._scheduled):
            try:
                self.private_plan(case_id)
            except ValueError as exc:
                issues.append(str(exc))
        return {
            "ok": not issues,
            "case_count": len(self._plans),
            "runnable_count": len(self._scheduled),
            "binding_count": len(self._binding_index),
            "source_file_count": len(imported.get("files", [])),
            "converter_version": imported.get("converter_version"),
            "issues": issues,
        }
