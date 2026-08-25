from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any


def _sha256_files(root: Path) -> dict[str, str]:
    if not root.is_dir():
        return {}
    return {
        str(path.relative_to(root)).replace("\\", "/"): hashlib.sha256(path.read_bytes()).hexdigest().upper()
        for path in sorted(root.rglob("*.json"))
        if path.is_file()
    }


def migrate(root: Path) -> dict[str, Any]:
    root = root.resolve()
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    src_root = root / "src"
    if str(src_root) not in sys.path:
        sys.path.insert(0, str(src_root))

    from frontend.server import AppPaths, WebApplication, _case_catalog_root, _test_project

    app = WebApplication(AppPaths.from_root(root))
    stable_fields = {
        "case_id", "sheet", "file_sheet", "title", "priority",
        "precondition_text", "steps_text", "expected_text",
        "verification_points", "setup", "actions", "collect", "unable",
        "mapping_status", "note", "automation_maturity", "blockers",
        "platform_automation", "source_ref", "execution_ref",
        "applicable_platforms", "workflow_state", "_source_file", "_source_file_sha256",
    }
    projects: list[dict[str, Any]] = []
    ok = True
    for registered_project in app.projects.list(include_archived=True):
        project_id = str(registered_project["project_id"])
        project = _test_project(project_id)
        source_root = _case_catalog_root(app.paths, project)
        hashes_before = _sha256_files(source_root)
        source_rows = app.cases._source_rows(project=project_id)
        source_cases = [
            {key: value for key, value in row.items() if key in stable_fields}
            for row in source_rows
        ]
        fingerprint = app.cases._source_catalog_fingerprint(project)
        signature = app.cases._source_catalog_signature(fingerprint)
        first = app.case_store.sync_source_cases(
            project, source_cases, source_fingerprint=signature
        )
        second = app.case_store.sync_source_cases(
            project, source_cases, source_fingerprint=signature
        )
        hashes_after = _sha256_files(source_root)
        managed_rows = app.case_store.list_cases(project_id, include_archived=True)
        source_ids = {str(row.get("case_id") or "") for row in source_rows}
        managed_source_ids = {
            str(row.get("case_id") or "")
            for row in managed_rows
            if row.get("source_type") in {"MANIFEST_579", "W30_CASE_MAP"}
        }
        project_ok = (
            hashes_before == hashes_after
            and source_ids == managed_source_ids
            and second["created"] == 0
            and second["updated"] == 0
        )
        ok = ok and project_ok
        projects.append({
            "project_id": project_id,
            "source_type": (project.get("case_catalog") or {}).get("type"),
            "source_case_count": len(source_rows),
            "managed_case_count": len(managed_rows),
            "first_sync": first,
            "idempotency_sync": second,
            "source_sha_unchanged": hashes_before == hashes_after,
            "source_identity_match": source_ids == managed_source_ids,
            "ok": project_ok,
        })
    return {
        "ok": ok,
        "schema_version": app.case_store.SCHEMA_VERSION,
        "database": str(app.case_store.path),
        "projects": projects,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="迁移并审计 Agent-loop 统一用例库")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent.parent)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    result = migrate(args.root)
    rendered = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
