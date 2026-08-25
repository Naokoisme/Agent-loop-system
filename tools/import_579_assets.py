"""Import frozen 579 manifests into Agent-loop-owned versioned assets.

This tool is intentionally explicit: runtime code never reads crossend_harness.
It copies immutable source content, records SHA256, and emits an API-safe case map.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any


MODULES = (
    ("stopwatch", "stage03_stopwatch_pipeline_frozen"),
    ("flashlight", "stage04_flashlight_pipeline_frozen"),
)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest().upper()


def _read(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"manifest is not an object: {path}")
    return payload


def _portable_metadata(value: Any) -> Any:
    """Remove source-machine absolute paths while preserving audit-friendly names."""

    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if key == "source_path" and isinstance(item, str):
                result[key] = Path(item).name
            else:
                result[key] = _portable_metadata(item)
        return result
    if isinstance(value, list):
        return [_portable_metadata(item) for item in value]
    return value


def _write(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _lines(values: Any) -> str:
    if not isinstance(values, list):
        return ""
    return "\n".join(
        f"{item.get('index', index)}. {str(item.get('text') or '').strip()}"
        for index, item in enumerate(values, start=1)
        if isinstance(item, dict) and str(item.get("text") or "").strip()
    )


def import_assets(source_root: Path, repository_root: Path) -> dict[str, Any]:
    source_root = source_root.resolve()
    repository_root = repository_root.resolve()
    package_root = repository_root / "src" / "agent_loop_system" / "platform_data" / "579"
    catalog_root = package_root / "catalog"
    case_map_root = repository_root / "case_map" / "579_case_map"
    functional_sources: list[dict[str, Any]] = []
    automation_sources: list[dict[str, Any]] = []
    execution_sources: list[dict[str, Any]] = []
    audit_files: list[dict[str, Any]] = []
    case_map_by_module: dict[str, list[dict[str, Any]]] = {}

    for module_key, directory_name in MODULES:
        directory = source_root / directory_name
        paths = {
            "functional": directory / "functional_case_manifest.json",
            "automation": directory / "automation_case_manifest.json",
            "execution": directory / "execution_manifest.json",
        }
        for kind, path in paths.items():
            if not path.is_file():
                raise FileNotFoundError(path)
            audit_files.append({
                "module": module_key,
                "kind": kind,
                "source_file": str(path.relative_to(source_root)).replace("\\", "/"),
                "sha256": _sha(path),
                "size": path.stat().st_size,
            })

        functional = _portable_metadata(_read(paths["functional"]))
        automation = _portable_metadata(_read(paths["automation"]))
        execution = _portable_metadata(_read(paths["execution"]))
        functional_sources.append(functional)
        automation_sources.append(automation)
        execution_sources.append(execution)
        automation_by_no = {
            str(case.get("case_no") or ""): case
            for case in automation.get("cases", [])
            if isinstance(case, dict)
        }
        scheduled = {
            str(value) for value in execution.get("case_order", []) if str(value)
        }
        module_name = str(functional.get("module") or module_key)
        mapped: list[dict[str, Any]] = []
        for functional_case in functional.get("cases", []):
            if not isinstance(functional_case, dict):
                continue
            case_id = str(functional_case.get("case_no") or "").strip()
            auto = automation_by_no.get(case_id, {})
            maturity = str(auto.get("automation_status") or "UNMAPPED").upper()
            blockers = [str(value) for value in auto.get("blockers", []) if str(value).strip()]
            runnable = maturity == "AUTO_READY" and case_id in scheduled
            plan_payload = {
                key: auto.get(key)
                for key in (
                    "automation_case_id", "setup", "steps", "assertions", "teardown",
                    "execution_policy", "state_contract", "required_capabilities",
                )
            }
            plan_sha = hashlib.sha256(
                json.dumps(plan_payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
            ).hexdigest().upper()
            blocker = "" if runnable else (
                "; ".join(blockers)
                or ("未进入冻结执行清单" if maturity == "AUTO_READY" else f"成熟度为 {maturity}")
            )
            mapped.append({
                "case_id": case_id,
                "sheet": module_name,
                "priority": str(functional_case.get("priority") or ""),
                "precondition_text": _lines(functional_case.get("preconditions")),
                "steps_text": _lines(functional_case.get("steps")),
                "expected_text": _lines(functional_case.get("expected_results")),
                "verification_points": [
                    str(item.get("text") or "").strip()
                    for item in functional_case.get("expected_results", [])
                    if isinstance(item, dict) and str(item.get("text") or "").strip()
                ],
                "setup": [],
                "actions": [],
                "collect": [],
                "unable": not runnable,
                "mapping_status": maturity,
                "automation_maturity": maturity,
                "note": blocker,
                "blockers": blockers or ([blocker] if blocker else []),
                "platform_automation": {
                    "579": {
                        "maturity": maturity,
                        "runnable": runnable,
                        "blocker": blocker,
                        "binding_ref": str(auto.get("automation_case_id") or ""),
                        "plan_ref": f"catalog/automation_cases.json#{case_id}",
                        "plan_sha256": plan_sha,
                    }
                },
                "source_ref": {
                    "functional_case_id": functional_case.get("functional_case_id"),
                    "functional_manifest_id": functional.get("manifest_id"),
                    "source_sha256": _sha(paths["functional"]),
                },
                "execution_ref": {
                    "automation_case_id": auto.get("automation_case_id"),
                    "execution_manifest_id": execution.get("manifest_id"),
                    "plan_sha256": plan_sha,
                },
            })
        case_map_by_module[module_name] = mapped

    state_path = source_root / "first_batch_state_restore_registry.json"
    if not state_path.is_file():
        raise FileNotFoundError(state_path)
    audit_files.append({
        "module": "shared",
        "kind": "state_restore_registry",
        "source_file": str(state_path.relative_to(source_root)).replace("\\", "/"),
        "sha256": _sha(state_path),
        "size": state_path.stat().st_size,
    })

    _write(catalog_root / "functional_cases.json", {
        "kind": "AgentLoop579FunctionalCatalog",
        "schema_version": 1,
        "manifests": functional_sources,
    })
    _write(catalog_root / "automation_cases.json", {
        "kind": "AgentLoop579PrivateAutomationCatalog",
        "schema_version": 1,
        "visibility": "private_runtime_only",
        "manifests": automation_sources,
    })
    _write(catalog_root / "execution_manifest.json", {
        "kind": "AgentLoop579ExecutionCatalog",
        "schema_version": 1,
        "manifests": execution_sources,
    })
    _write(package_root / "state" / "restore_registry.v1.json", _read(state_path))
    _write(package_root / "bindings" / "action_bindings.v1.json", {
        "kind": "AgentLoop579ActionBindings",
        "schema_version": 1,
        "visibility": "private_runtime_only",
        "source": "frozen_automation_manifests",
        "cases": [
            {
                "case_id": case["case_id"],
                "binding_ref": case["platform_automation"]["579"]["binding_ref"],
                "plan_sha256": case["platform_automation"]["579"]["plan_sha256"],
                "maturity": case["platform_automation"]["579"]["maturity"],
                "runnable": case["platform_automation"]["579"]["runnable"],
            }
            for cases in case_map_by_module.values()
            for case in cases
        ],
    })
    _write(package_root / "schemas" / "README.json", {
        "schema_version": 1,
        "note": "579 资产通过 import_manifest.json 的 SHA256 审计；公共 API 不返回 raw payload。",
    })
    for module_name, cases in case_map_by_module.items():
        _write(case_map_root / f"{module_name}.json", {
            "profile": "579_O2",
            "sheet": module_name,
            "cases": cases,
        })
    _write(package_root / "import_manifest.json", {
        "kind": "AgentLoop579ImportManifest",
        "schema_version": 1,
        "converter_version": "1.0.0",
        "imported_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "source_label": "crossend_harness frozen manifests",
        "files": audit_files,
        "case_count": sum(len(values) for values in case_map_by_module.values()),
    })
    return {
        "case_count": sum(len(values) for values in case_map_by_module.values()),
        "modules": {key: len(value) for key, value in case_map_by_module.items()},
        "audit_file_count": len(audit_files),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument(
        "--repository-root", type=Path, default=Path(__file__).resolve().parents[1]
    )
    args = parser.parse_args()
    print(json.dumps(import_assets(args.source_root, args.repository_root), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
