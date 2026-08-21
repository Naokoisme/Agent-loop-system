from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_loop_system.tools.hardware_target import (
    HardwareTargetConfig,
    find_hardware_workspace,
    hardware_command_allowed,
    is_forbidden_hardware_workspace,
)


class HardwareTargetConfigTest(unittest.TestCase):
    def _tree(self, root: Path, project: str = "6202_W5230") -> None:
        app = root / "app"
        (app / "projects" / project).mkdir(parents=True)
        quick_cmd_dir = (
            app / "comm" / "quick_cmd"
            if project == "6204_W5230"
            else app / "comm" / "TuoBu" / "quick_cmd"
        )
        quick_cmd_dir.mkdir(parents=True)
        (root / "core" / "comm" / "srv" / "test").mkdir(parents=True)
        (app / "ProjectConfig.cmake").write_text(
            f"set(PROJECT {project})\n", encoding="utf-8"
        )
        for path in (
            app / "projects" / project / "Project.cmake",
            quick_cmd_dir / "gui_comm_quick_cmd.c",
            root / "core" / "comm" / "srv" / "test" / "srv_quick_cmd_handler.c",
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
            self.assertEqual(
                config.command_source,
                root.resolve()
                / "core"
                / "comm"
                / "srv"
                / "test"
                / "srv_quick_cmd_handler.c",
            )

    def test_6204_uses_its_project_specific_quick_cmd_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._tree(root, project="6204_W5230")
            with mock.patch.dict(
                os.environ,
                {
                    "W30_HARDWARE_SOURCE_ROOT": str(root),
                    "W30_HARDWARE_WORKSPACE_ROOT": str(root),
                    "W30_HARDWARE_PROJECT": "6204_W5230",
                },
                clear=True,
            ):
                config = HardwareTargetConfig.from_env()
            self.assertEqual(config.project, "6204_W5230")
            self.assertEqual(
                config.app_quick_cmd,
                root.resolve()
                / "app"
                / "comm"
                / "quick_cmd"
                / "gui_comm_quick_cmd.c",
            )

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

    def test_single_workspace_root_variable_is_sufficient(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._tree(root)
            with mock.patch.dict(
                os.environ,
                {
                    "W30_HARDWARE_WORKSPACE_ROOT": str(root),
                },
                clear=True,
            ):
                config = HardwareTargetConfig.from_env()
            self.assertEqual(config.source_root, root.resolve())
            self.assertEqual(config.project, "6202_W5230")

    def test_single_hardware_workspace_variable_is_sufficient(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._tree(root)
            with mock.patch.dict(
                os.environ,
                {
                    "W30_HARDWARE_WORKSPACE": str(root),
                },
                clear=True,
            ):
                config = HardwareTargetConfig.from_env()
            self.assertEqual(config.source_root, root.resolve())

    def test_single_hardware_source_root_variable_is_sufficient(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._tree(root)
            with mock.patch.dict(
                os.environ,
                {
                    "W30_HARDWARE_SOURCE_ROOT": str(root),
                },
                clear=True,
            ):
                config = HardwareTargetConfig.from_env()
            self.assertEqual(config.source_root, root.resolve())

    def test_auto_discovery_via_base_dir(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            proj_dir = base / "6202_W5230"
            proj_dir.mkdir(parents=True)
            self._tree(proj_dir)
            with mock.patch.dict(
                os.environ,
                {
                    "AGENT_LOOP_WORKSPACE_BASE": str(base),
                },
                clear=True,
            ):
                config = HardwareTargetConfig.from_env()
            self.assertEqual(config.source_root, proj_dir.resolve())

    def test_auto_discovery_fails_when_workspace_not_found(self) -> None:
        with mock.patch(
            "agent_loop_system.tools.hardware_target.find_hardware_workspace",
            return_value=None,
        ):
            with mock.patch.dict(os.environ, {}, clear=True):
                with self.assertRaisesRegex(ValueError, "HARDWARE_WORKSPACE_NOT_FOUND"):
                    HardwareTargetConfig.from_env()

    def test_find_hardware_workspace_direct(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            proj_dir = base / "6202_W5230"
            proj_dir.mkdir(parents=True)
            self._tree(proj_dir)

            found = find_hardware_workspace("6202_W5230", search_roots=[base])
            self.assertEqual(found, proj_dir.resolve())

            not_found = find_hardware_workspace("nonexistent_project", search_roots=[base])
            self.assertIsNone(not_found)

            self.assertTrue(is_forbidden_hardware_workspace(Path(r"D:\TOPSTEP\shenju_w30")))
            self.assertFalse(is_forbidden_hardware_workspace(proj_dir))

    def test_forbidden_upstream_shenju_w30_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            upstream = Path(directory) / "shenju_w30"
            upstream.mkdir(parents=True)
            self._tree(upstream)
            with mock.patch.dict(
                os.environ,
                {
                    "W30_HARDWARE_WORKSPACE_ROOT": str(upstream),
                },
                clear=True,
            ):
                with self.assertRaisesRegex(ValueError, "HARDWARE_WORKSPACE_FORBIDDEN"):
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
