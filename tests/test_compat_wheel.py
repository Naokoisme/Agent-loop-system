from __future__ import annotations

from pathlib import Path
import json
import zipfile

from tools.build_compat_wheel import (
    build_lock_payload,
    python_with_pip,
    source_tree_sha256,
)
from tools.verify_compat_wheel import REQUIRED, audit_wheel


def test_source_tree_hash_is_stable_and_lock_marks_dirty_build_non_release(tmp_path: Path) -> None:
    first = source_tree_sha256()
    second = source_tree_sha256()
    assert first == second and len(first) == 64
    wheel = tmp_path / "agent_loop_system-0.4.0-py3-none-any.whl"
    wheel.write_bytes(b"wheel-fixture")
    lock = build_lock_payload(
        wheel=wheel,
        source_commit="a" * 40,
        source_dirty=True,
    )
    assert lock["release_eligible"] is False
    assert lock["source_dirty"] is True
    assert len(lock["wheel_sha256"]) == 64
    assert len(lock["platform_579_import_manifest_sha256"]) == 64


def test_builder_falls_back_to_a_python_312_with_pip() -> None:
    selected = python_with_pip()
    assert Path(selected).is_file()


def test_wheel_audit_requires_579_assets_and_rejects_desktop_entries(tmp_path: Path) -> None:
    wheel = tmp_path / "fixture.whl"
    bindings = {"cases": [{"runnable": True} for _ in range(37)]}
    with zipfile.ZipFile(wheel, "w") as archive:
        for name in REQUIRED:
            content = json.dumps(bindings) if name.endswith("action_bindings.v1.json") else "{}"
            archive.writestr(name, content)
        archive.writestr("agent_loop_system/desktop_qt.py", "from PySide6 import QtCore")
    result = audit_wheel(wheel)
    assert result["ok"] is False
    assert any("FORBIDDEN_DESKTOP_ENTRY" in issue for issue in result["issues"])
