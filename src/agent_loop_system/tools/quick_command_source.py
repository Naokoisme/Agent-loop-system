"""Resolve the one project-owned quick-command source selected by firmware."""
from __future__ import annotations

from pathlib import Path


_CANDIDATE_NAMES = (
    "hlq_quick_cmd_handler.c",
    "srv_quick_cmd_handler.c",
)


def resolve_project_command_source(source_root: str | Path) -> Path:
    """Return the authoritative command table without cross-project fallback.

    Projects may keep a legacy source beside the active HLQ source.  When both
    exist, the local CMake exclusion is used to identify the one that is linked;
    an ambiguous tree is rejected instead of guessing.
    """

    test_dir = (
        Path(source_root).resolve()
        / "core"
        / "comm"
        / "srv"
        / "test"
    )
    existing = [test_dir / name for name in _CANDIDATE_NAMES if (test_dir / name).is_file()]
    if len(existing) == 1:
        return existing[0]
    if not existing:
        raise ValueError(
            "当前真实源码命令表不存在: "
            + ", ".join(str(test_dir / name) for name in _CANDIDATE_NAMES)
        )

    cmake_path = test_dir / "CMakeLists.txt"
    try:
        cmake = cmake_path.read_text(encoding="utf-8", errors="ignore")
    except OSError as exc:
        raise ValueError(
            f"当前源码同时存在两份命令表，且无法读取选择规则: {cmake_path}: {exc}"
        ) from exc
    active = [
        path
        for path in existing
        if not any(
            "EXCLUDE REGEX" in line and path.stem in line
            for line in cmake.splitlines()
        )
    ]
    if len(active) != 1:
        raise ValueError(
            "当前源码命令表选择不唯一，禁止猜测: "
            + ", ".join(str(path) for path in existing)
        )
    return active[0]


__all__ = ["resolve_project_command_source"]
