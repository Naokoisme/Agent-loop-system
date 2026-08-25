"""Verify that the unified wheel contains 579 assets and no desktop runtime."""

from __future__ import annotations

import argparse
import json
import zipfile
from pathlib import Path
from typing import Sequence


REQUIRED = {
    "agent_loop_system/case_management/__init__.py",
    "agent_loop_system/case_management/repository.py",
    "agent_loop_system/platform_data/platform_profiles.v1.json",
    "agent_loop_system/platform_data/579/import_manifest.json",
    "agent_loop_system/platform_data/579/bindings/action_bindings.v1.json",
    "agent_loop_system/platform_data/579/catalog/automation_cases.json",
    "agent_loop_system/platform_data/579/catalog/execution_manifest.json",
    "agent_loop_system/platform_data/579/catalog/functional_cases.json",
    "agent_loop_system/platform_data/579/state/restore_registry.v1.json",
    "agent_loop_system/platforms/platform_579/execution.py",
    "agent_loop_system/platforms/platform_579/serial_readonly.py",
    "agent_loop_system/platforms/platform_579/transport.py",
}
FORBIDDEN_PATH_TOKENS = (
    "desktop_qt.py",
    "desktop_workbench.py",
    "pyside6",
    "tkinter",
)
FORBIDDEN_CONTENT = (
    b"D:\\\\automated\\\\tools\\\\crossend_harness",
    "D:\\自动化\\tools\\crossend_harness".encode("utf-8"),
    b"D:/automation/tools/crossend_harness",
    "D:/自动化/tools/crossend_harness".encode("utf-8"),
)


def audit_wheel(path: Path) -> dict[str, object]:
    path = Path(path).resolve()
    issues: list[str] = []
    with zipfile.ZipFile(path) as archive:
        names = set(archive.namelist())
        for required in sorted(REQUIRED - names):
            issues.append(f"MISSING_REQUIRED_ENTRY: {required}")
        for name in sorted(names):
            lowered = name.lower()
            if any(token in lowered for token in FORBIDDEN_PATH_TOKENS):
                issues.append(f"FORBIDDEN_DESKTOP_ENTRY: {name}")
            if not name.endswith((".py", ".json", ".txt", ".md")):
                continue
            content = archive.read(name)
            if any(token in content for token in FORBIDDEN_CONTENT):
                issues.append(f"FORBIDDEN_ABSOLUTE_DEPENDENCY: {name}")
        binding_name = "agent_loop_system/platform_data/579/bindings/action_bindings.v1.json"
        bindings = json.loads(archive.read(binding_name).decode("utf-8")) if binding_name in names else {}
        runnable = sum(bool(item.get("runnable")) for item in bindings.get("cases", []))
        if runnable != 37:
            issues.append(f"579_BINDING_COUNT_MISMATCH: {runnable}")
    return {
        "ok": not issues,
        "wheel": str(path),
        "required_entry_count": len(REQUIRED),
        "runnable_579_binding_count": runnable if "runnable" in locals() else 0,
        "issues": issues,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("wheel")
    args = parser.parse_args(list(argv) if argv is not None else None)
    result = audit_wheel(Path(args.wheel))
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
