"""Small shared helpers for keeping workflow and product outcomes orthogonal."""

from __future__ import annotations

from typing import Any


FAILED_WORKFLOW_STATUSES = {"failed", "cancelled", "interrupted", "orphaned"}


def evidence_status(payload: dict[str, Any]) -> str:
    explicit = str(payload.get("evidence_status") or "").strip().upper()
    if explicit:
        return explicit
    contract = payload.get("evidence_contract")
    if not isinstance(contract, dict) or not contract:
        return "NOT_RECORDED"
    status = str(contract.get("status") or "").strip().upper()
    if status:
        return status
    if contract.get("complete") is True:
        return "COMPLETE"
    if contract.get("complete") is False:
        return "INCOMPLETE"
    return "NOT_RECORDED"


def outcome_fields(
    payload: dict[str, Any],
    *,
    workflow_default: str = "completed",
    mapping_default: str = "NOT_APPLICABLE",
) -> dict[str, Any]:
    """Return additive status fields without rewriting the product verdict."""

    workflow_status = str(
        payload.get("workflow_status") or workflow_default
    ).strip().lower()
    execution_status = str(payload.get("execution_status") or "").strip().upper()
    if not execution_status:
        execution_status = (
            "ERROR"
            if workflow_status in FAILED_WORKFLOW_STATUSES
            or payload.get("error")
            or str(payload.get("verdict") or "").upper() == "ERROR"
            else "OK"
        )
    resolved_evidence_status = evidence_status(payload)
    mapping_status = str(
        payload.get("mapping_status") or mapping_default
    ).strip().upper()
    reason_code = payload.get("reason_code") or payload.get("error_code")
    if not reason_code:
        if workflow_status in FAILED_WORKFLOW_STATUSES:
            reason_code = "WORKFLOW_FAILED"
        elif execution_status == "ERROR":
            reason_code = "EXECUTION_ERROR"
        elif resolved_evidence_status in {"INCOMPLETE", "MISSING", "ERROR"}:
            reason_code = "EVIDENCE_INCOMPLETE"

    return {
        "workflow_status": workflow_status,
        "execution_status": execution_status,
        "evidence_status": resolved_evidence_status,
        "mapping_status": mapping_status,
        "reason_code": reason_code,
    }
