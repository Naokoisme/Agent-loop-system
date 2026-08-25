from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from agent_loop_system.tools.quick_command_source import (
    resolve_project_command_source,
)


def _test_dir(root: Path) -> Path:
    directory = root / "core" / "comm" / "srv" / "test"
    directory.mkdir(parents=True)
    return directory


@pytest.mark.parametrize(
    "filename",
    ["srv_quick_cmd_handler.c", "hlq_quick_cmd_handler.c"],
)
def test_resolves_the_only_project_owned_command_table(filename: str) -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        directory = _test_dir(root)
        expected = directory / filename
        expected.write_text("// active\n", encoding="utf-8")

        assert resolve_project_command_source(root) == expected.resolve()


def test_uses_cmake_exclusion_when_legacy_and_active_sources_coexist() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        directory = _test_dir(root)
        active = directory / "hlq_quick_cmd_handler.c"
        active.write_text("// active\n", encoding="utf-8")
        (directory / "srv_quick_cmd_handler.c").write_text(
            "// legacy\n", encoding="utf-8"
        )
        (directory / "CMakeLists.txt").write_text(
            'list(FILTER C_FILES EXCLUDE REGEX "srv_quick_cmd_handler\\\\.c$")\n',
            encoding="utf-8",
        )

        assert resolve_project_command_source(root) == active.resolve()


def test_rejects_an_ambiguous_command_table() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        directory = _test_dir(root)
        for filename in ("hlq_quick_cmd_handler.c", "srv_quick_cmd_handler.c"):
            (directory / filename).write_text("// candidate\n", encoding="utf-8")
        (directory / "CMakeLists.txt").write_text(
            "file(GLOB C_FILES *.c)\n", encoding="utf-8"
        )

        with pytest.raises(ValueError, match="选择不唯一"):
            resolve_project_command_source(root)
