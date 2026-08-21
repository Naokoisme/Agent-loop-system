from __future__ import annotations

import io
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_loop_system.internal_dispatcher import (
    INTERNAL_COMMANDS,
    build_child_command,
    dispatch_internal_command,
)


class InternalDispatcherTests(unittest.TestCase):
    def test_whitelist_contains_required_subcommands(self) -> None:
        expected = {"test", "test-batch", "agent", "defect-store", "watch-mtp-screenshot"}
        self.assertTrue(expected.issubset(set(INTERNAL_COMMANDS.keys())))

    def test_source_mode_command_generation(self) -> None:
        cmd = build_child_command(
            "test",
            ["--sheet", "计算器", "--case-id", "CALC_001"],
            force_frozen=False,
            executable="C:/Python312/python.exe",
        )
        self.assertEqual(
            cmd,
            [
                "C:/Python312/python.exe",
                "-m",
                "agent_loop_system.tools.test",
                "--sheet",
                "计算器",
                "--case-id",
                "CALC_001",
            ],
        )

    def test_frozen_mode_command_generation(self) -> None:
        cmd = build_child_command(
            "test",
            ["--sheet", "计算器", "--case-id", "CALC_001"],
            force_frozen=True,
            executable="C:/dist/Agent-loop.exe",
        )
        self.assertEqual(
            cmd,
            [
                "C:/dist/Agent-loop.exe",
                "--internal",
                "test",
                "--sheet",
                "计算器",
                "--case-id",
                "CALC_001",
            ],
        )

    def test_agent_command_generation_frozen(self) -> None:
        cmd = build_child_command(
            "agent",
            ["--defect", "CALC_BUG"],
            force_frozen=True,
            executable="C:/dist/Agent-loop.exe",
        )
        self.assertEqual(
            cmd,
            ["C:/dist/Agent-loop.exe", "--internal", "agent", "--defect", "CALC_BUG"],
        )

    def test_defect_store_command_generation_frozen(self) -> None:
        cmd = build_child_command(
            "defect-store",
            ["--import-all"],
            force_frozen=True,
            executable="C:/dist/Agent-loop.exe",
        )
        self.assertEqual(
            cmd,
            ["C:/dist/Agent-loop.exe", "--internal", "defect-store", "--import-all"],
        )

    def test_unknown_command_in_builder_raises(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            build_child_command("non_existent_command", ["--arg"])
        self.assertIn("未在白名单中登记", str(ctx.exception))

    def test_dispatch_unknown_internal_command(self) -> None:
        stderr_capture = io.StringIO()
        with patch("sys.stderr", stderr_capture):
            code = dispatch_internal_command(["--internal", "invalid_subcmd"])
        self.assertEqual(code, 2)
        self.assertIn("未知的内部子命令", stderr_capture.getvalue())

    def test_dispatch_missing_command_name(self) -> None:
        stderr_capture = io.StringIO()
        with patch("sys.stderr", stderr_capture):
            code = dispatch_internal_command(["--internal"])
        self.assertEqual(code, 2)
        self.assertIn("必须指定子命令名称", stderr_capture.getvalue())

    def test_dispatch_non_internal_is_noop(self) -> None:
        code = dispatch_internal_command(["--host", "127.0.0.1"])
        self.assertEqual(code, 0)


if __name__ == "__main__":
    unittest.main()
