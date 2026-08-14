from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_loop_system.tools.hardware_target import (
    HardwareTargetConfig,
    hardware_command_allowed,
)


class HardwareTargetConfigTest(unittest.TestCase):
    def _tree(self, root: Path, project: str = "6202_W5230") -> None:
        app = root / "app"
        (app / "projects" / project).mkdir(parents=True)
        (app / "comm" / "TuoBu" / "quick_cmd").mkdir(parents=True)
        (root / "core" / "comm" / "srv" / "test").mkdir(parents=True)
        (app / "ProjectConfig.cmake").write_text(
            f"set(PROJECT {project})\n", encoding="utf-8"
        )
        for path in (
            app / "projects" / project / "Project.cmake",
            app / "comm" / "TuoBu" / "quick_cmd" / "gui_comm_quick_cmd.c",
            root / "core" / "comm" / "srv" / "test" / "hlq_quick_cmd_handler.c",
        ):
            path.write_text("// test\n", encoding="utf-8")

    def test_from_env_uses_separate_hardware_source_not_620c_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._tree(root)
            with mock.patch.dict(
                os.environ,
                {
                    "W30_SOURCE_ROOT": r"D:\Agent-loop-workspace\620C_W6830",
                    "W30_HARDWARE_SOURCE_ROOT": str(root),
                    "W30_HARDWARE_WORKSPACE_ROOT": str(root),
                },
                clear=True,
            ):
                config = HardwareTargetConfig.from_env()
            self.assertEqual(config.source_root, root.resolve())
            self.assertEqual(config.project, "6202_W5230")

    def test_project_mismatch_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._tree(root, project="620C_W6830")
            with mock.patch.dict(
                os.environ,
                {
                    "W30_HARDWARE_SOURCE_ROOT": str(root),
                    "W30_HARDWARE_WORKSPACE_ROOT": str(root),
                },
                clear=True,
            ):
                with self.assertRaisesRegex(ValueError, "HARDWARE_SOURCE_CONFLICT"):
                    HardwareTargetConfig.from_env()

    def test_source_outside_hardware_workspace_is_rejected(self) -> None:
        with (
            tempfile.TemporaryDirectory() as source_dir,
            tempfile.TemporaryDirectory() as workspace_dir,
        ):
            source = Path(source_dir)
            self._tree(source)
            with mock.patch.dict(
                os.environ,
                {
                    "W30_HARDWARE_SOURCE_ROOT": str(source),
                    "W30_HARDWARE_WORKSPACE_ROOT": workspace_dir,
                },
                clear=True,
            ):
                with self.assertRaisesRegex(ValueError, "HARDWARE_WORKSPACE_CONFLICT"):
                    HardwareTargetConfig.from_env()


class HardwareCommandPolicyTest(unittest.TestCase):
    def test_sim_and_dangerous_commands_are_rejected(self) -> None:
        for command in (
            "SIM_WAIT",
            "SIM_CHARGE",
            "FACTORY_RESET",
            "POWER_OFF",
            "TEST_SESSION",
        ):
            with self.subTest(command=command):
                allowed, reason = hardware_command_allowed(command)
                self.assertFalse(allowed)
                self.assertTrue(reason)

    def test_read_only_observation_commands_are_allowed(self) -> None:
        for command in ("GUI_PING", "GUI_STATE", "GUI_TREE", "ENTER_PAGE"):
            with self.subTest(command=command):
                self.assertEqual(hardware_command_allowed(command), (True, None))


if __name__ == "__main__":
    unittest.main()
