"""Public offline CLI for the shared W30-owned exploration contracts."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from typing import Any, Sequence

from agent_loop_system.exploration_core.action_registry import (
    ExplorationActionRegistry,
    audit_registry,
    normalize_platform_id,
    sha256_file,
)


_CASE_ID_FIELDS = ("case_id", "case_no", "id", "task_id")
_PLATFORM_IR_KEYS = {
    "cmd_id", "key_id", "data_hex", "compiled_ir", "setup", "teardown",
}
_FUNCTIONAL_CASE_FIELDS = (
    "case_id", "case_no", "id", "title", "name", "module", "feature",
    "test_item", "test_point", "objective", "precondition", "preconditions",
    "steps", "expected", "priority", "required_capabilities",
    "parent_source_cases", "source_traceability",
)


def _json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)


def _find_case(value: Any, case_id: str) -> dict[str, Any] | None:
    if isinstance(value, dict):
        if any(str(value.get(key) or "").strip() == case_id for key in _CASE_ID_FIELDS):
            return value
        for nested in value.values():
            found = _find_case(nested, case_id)
            if found is not None:
                return found
    elif isinstance(value, list):
        for nested in value:
            found = _find_case(nested, case_id)
            if found is not None:
                return found
    return None


def _load_case(path: Path, case_id: str) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    found = _find_case(payload, case_id)
    if found is None:
        raise ValueError(f"case-source 中找不到用例 {case_id}")
    if _contains_platform_ir(found):
        raise ValueError(
            "case-source 包含平台执行 IR；请提供功能层用例，Agent 不得读取 cmd_id/key_id/data_hex"
        )
    projected = {
        key: found[key]
        for key in _FUNCTIONAL_CASE_FIELDS
        if key in found and found[key] not in (None, "", [], {})
    }
    if not any(key in projected for key in ("expected", "objective", "test_point")):
        raise ValueError("功能层用例缺少 expected/objective/test_point")
    return projected


def _contains_platform_ir(value: Any) -> bool:
    if isinstance(value, dict):
        if _PLATFORM_IR_KEYS.intersection(value):
            return True
        if str(value.get("action") or "").strip().lower() in {
            "raw_command", "raw_command_repeat",
        }:
            return True
        return any(_contains_platform_ir(nested) for nested in value.values())
    if isinstance(value, list):
        return any(_contains_platform_ir(nested) for nested in value)
    return False


def _registry_validate(args: argparse.Namespace) -> int:
    registry_path = Path(args.registry)
    registry = ExplorationActionRegistry.load(registry_path)
    audit = audit_registry(
        registry,
        platform_id=args.platform,
        evidence_root=args.evidence_root or registry_path.parent,
        verify_evidence=args.verify_evidence,
    )
    print(_json_text(audit))
    return 0 if audit["status"] == "PASS" else 1


def _list_actions(args: argparse.Namespace) -> int:
    registry = ExplorationActionRegistry.load(args.registry)
    platform = normalize_platform_id(args.platform)
    audit = audit_registry(registry, platform_id=platform)
    if audit["status"] != "PASS":
        print(_json_text(audit))
        return 1
    rows = []
    for alias, (action, binding) in sorted(
        registry.platform_action_index(
            platform, verified_only=not args.include_non_verified,
        ).items()
    ):
        rows.append({
            "alias": alias,
            "command": f":CAP_ACTION:{alias}",
            "description": action.description,
            "status": binding.status.value,
            "transport": binding.transport,
            "required_capabilities": list(binding.required_capabilities),
        })
    print(_json_text({
        "kind": "ExplorationActionList",
        "platform": platform,
        "actions": rows,
        "device_action_count": 0,
    }))
    return 0


def _explore_case(args: argparse.Namespace) -> int:
    if args.mode != "shadow":
        raise ValueError("公共 CLI 当前只允许 shadow；实机由平台门禁另行授权")
    registry_path = Path(args.registry)
    case_path = Path(args.case_source)
    platform = normalize_platform_id(args.platform)
    registry = ExplorationActionRegistry.load(registry_path)
    audit = audit_registry(
        registry,
        platform_id=platform,
        evidence_root=args.evidence_root or registry_path.parent,
        verify_evidence=True,
    )
    if audit["status"] != "PASS":
        print(_json_text(audit))
        return 1
    actions = registry.platform_action_index(platform)
    if not actions:
        print(_json_text({
            **audit,
            "status": "BLOCKED",
            "blocking_count": 1,
            "issues": [
                *audit["issues"],
                {
                    "severity": "blocking",
                    "code": "VERIFIED_PLATFORM_ACTIONS_MISSING",
                    "platform": platform,
                },
            ],
        }))
        return 1
    selected_case = _load_case(case_path, args.case_id)
    output_dir = Path(args.out)
    if output_dir.exists():
        raise FileExistsError(f"Shadow 输出目录已存在，拒绝覆盖: {output_dir}")
    output_dir.mkdir(parents=True)
    request = {
        "kind": "ShadowExplorationRequest",
        "schema_version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "status": "SHADOW_READY",
        "mode": "shadow",
        "platform": platform,
        "case_id": args.case_id,
        "max_actions": args.max_actions,
        "device_actions_enabled": False,
        "formal_evidence_eligible": False,
        "promotion_status": "EXPLORING",
        "case": selected_case,
        "available_actions": [
            {
                "alias": alias,
                "command": f":CAP_ACTION:{alias}",
                "description": action.description,
                "transport": binding.transport,
                "required_capabilities": list(binding.required_capabilities),
            }
            for alias, (action, binding) in sorted(actions.items())
        ],
        "source_files": {
            "registry": registry_path.name,
            "registry_sha256": sha256_file(registry_path),
            "case_source": case_path.name,
            "case_source_sha256": sha256_file(case_path),
        },
        "registry_audit": audit,
    }
    destination = output_dir / "shadow-request.json"
    destination.write_text(_json_text(request) + "\n", encoding="utf-8")
    print(_json_text({
        "status": "SHADOW_READY",
        "artifact": str(destination),
        "device_action_count": 0,
    }))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agent-exploration")
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate = subparsers.add_parser("registry-validate")
    validate.add_argument("--registry", required=True)
    validate.add_argument("--platform")
    validate.add_argument("--evidence-root")
    validate.add_argument("--verify-evidence", action="store_true")
    validate.set_defaults(handler=_registry_validate)

    listing = subparsers.add_parser("list-actions")
    listing.add_argument("--registry", required=True)
    listing.add_argument("--platform", required=True)
    listing.add_argument("--include-non-verified", action="store_true")
    listing.set_defaults(handler=_list_actions)

    explore = subparsers.add_parser("explore-case")
    explore.add_argument("--platform", required=True)
    explore.add_argument("--registry", required=True)
    explore.add_argument("--evidence-root")
    explore.add_argument("--case-source", required=True)
    explore.add_argument("--case-id", required=True)
    explore.add_argument("--mode", choices=("shadow",), default="shadow")
    explore.add_argument("--max-actions", type=int, choices=range(1, 51), default=12)
    explore.add_argument("--out", required=True)
    explore.set_defaults(handler=_explore_case)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(list(argv) if argv is not None else None)
        return int(args.handler(args))
    except (FileExistsError, FileNotFoundError, ValueError, json.JSONDecodeError) as exc:
        print(_json_text({
            "status": "BLOCKED",
            "reason": str(exc),
            "device_action_count": 0,
        }), file=sys.stderr)
        return 2


def entrypoint() -> None:
    raise SystemExit(main())


if __name__ == "__main__":
    entrypoint()
