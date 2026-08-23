from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_loop_system.runtime_root import (
    RuntimePaths,
    is_frozen,
    load_app_env,
    resolve_app_root,
    resolve_config_path,
    resolve_layout_root,
)


class RuntimeRootTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.workspace_root = Path(__file__).resolve().parents[1]

    def test_source_mode_resolution(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AGENT_LOOP_ROOT", None)
            root = resolve_app_root()
            self.assertEqual(root, self.workspace_root.resolve())

            paths = RuntimePaths.from_root()
            self.assertEqual(paths.root, self.workspace_root.resolve())
            self.assertEqual(paths.frontend, self.workspace_root.resolve() / "frontend")
            self.assertEqual(paths.case_map, self.workspace_root.resolve() / "case_map")
            self.assertEqual(paths.templates, self.workspace_root.resolve() / "templates")
            self.assertEqual(paths.profiles, self.workspace_root.resolve() / "profiles")
            self.assertEqual(paths.history, self.workspace_root.resolve() / "history")
            self.assertEqual(paths.evidence, self.workspace_root.resolve() / "evidence")
            self.assertEqual(
                paths.runtime_jobs,
                self.workspace_root.resolve() / ".runtime" / "jobs",
            )

    def test_simulated_frozen_exe_mode_resolution(self) -> None:
        fake_portable_dir = Path("C:/portable_test_dist/agent-loop-windows-x64").resolve()
        fake_exe_path = fake_portable_dir / "Agent-loop.exe"
        fake_meipass = Path("C:/Users/TestUser/AppData/Local/Temp/_MEI99999").resolve()

        with patch.object(sys, "frozen", True, create=True), \
             patch.object(sys, "executable", str(fake_exe_path)), \
             patch.object(sys, "_MEIPASS", str(fake_meipass), create=True), \
             patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AGENT_LOOP_ROOT", None)

            self.assertTrue(is_frozen())
            root = resolve_app_root()
            self.assertEqual(root, fake_portable_dir)
            self.assertNotEqual(root, fake_meipass, "App root must NOT be sys._MEIPASS")

            paths = RuntimePaths.from_root()
            self.assertEqual(paths.root, fake_portable_dir)
            self.assertEqual(paths.frontend, fake_portable_dir / "frontend")
            self.assertEqual(paths.case_map, fake_portable_dir / "case_map")
            self.assertEqual(paths.templates, fake_portable_dir / "templates")
            self.assertEqual(paths.profiles, fake_portable_dir / "profiles")
            self.assertEqual(paths.history, fake_portable_dir / "history")
            self.assertEqual(paths.evidence, fake_portable_dir / "evidence")
            self.assertEqual(paths.runtime_jobs, fake_portable_dir / ".runtime" / "jobs")

            # Verify no paths point to _MEIPASS
            for p in (paths.case_map, paths.history, paths.evidence, paths.runtime_jobs, paths.templates):
                self.assertNotIn("_MEI", str(p))

    def test_explicit_root_and_environment_override(self) -> None:
        custom_root = Path("D:/custom_test_root").resolve()
        # Explicit argument
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AGENT_LOOP_ROOT", None)
            res1 = resolve_app_root(custom_root)
            self.assertEqual(res1, custom_root)

        # Environment variable override
        with patch.dict(os.environ, {"AGENT_LOOP_ROOT": str(custom_root)}):
            res2 = resolve_app_root()
            self.assertEqual(res2, custom_root)

    def test_cwd_independence(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AGENT_LOOP_ROOT", None)
            orig_cwd = os.getcwd()
            try:
                os.chdir("C:\\")
                root = resolve_app_root()
                self.assertEqual(root, self.workspace_root.resolve())
            finally:
                os.chdir(orig_cwd)

    def test_relative_config_paths_are_anchored_at_app_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            app_root = Path(tmpdir) / "system"
            app_root.mkdir()
            expected = (app_root / ".." / "workspaces" / "firmware").resolve()
            with patch.dict(
                os.environ,
                {"AGENT_LOOP_ROOT": str(app_root)},
                clear=True,
            ):
                orig_cwd = os.getcwd()
                try:
                    os.chdir("C:\\")
                    self.assertEqual(
                        resolve_config_path("../workspaces/firmware"),
                        expected,
                    )
                finally:
                    os.chdir(orig_cwd)

    def test_layout_root_and_shared_workspace_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            layout_root = Path(tmpdir)
            app_root = layout_root / "system"
            app_root.mkdir()
            with patch.dict(
                os.environ,
                {
                    "AGENT_LOOP_ROOT": str(app_root),
                    "AGENT_LOOP_LAYOUT_ROOT": "..",
                },
                clear=True,
            ):
                self.assertEqual(resolve_layout_root(), layout_root.resolve())
                paths = RuntimePaths.from_root()
                self.assertEqual(
                    paths.firmware_workspaces,
                    layout_root.resolve() / "workspaces" / "firmware",
                )
                self.assertEqual(
                    paths.tool_workspaces,
                    layout_root.resolve() / "workspaces" / "tools",
                )
                self.assertEqual(
                    paths.legacy_evidence,
                    layout_root.resolve() / "data" / "legacy-evidence",
                )

    def test_frozen_layout_remains_self_contained_without_override(self) -> None:
        fake_portable_dir = Path("C:/portable_test_dist/agent-loop-windows-x64").resolve()
        with patch.object(sys, "frozen", True, create=True), patch.dict(
            os.environ,
            {"AGENT_LOOP_ROOT": str(fake_portable_dir)},
            clear=True,
        ):
            self.assertEqual(resolve_layout_root(), fake_portable_dir)

    def test_load_app_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_root = Path(tmpdir)
            env_file = tmp_root / ".env"
            env_file.write_text("TEST_KEY_E1=TEST_VALUE_123\n# Comment\n", encoding="utf-8")

            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("TEST_KEY_E1", None)
                loaded = load_app_env(app_root=tmp_root)
                self.assertIsNotNone(loaded)
                self.assertEqual(loaded.resolve(), env_file.resolve())
                self.assertEqual(
                    os.environ.get("TEST_KEY_E1"), "TEST_VALUE_123"
                )

        # Test missing .env does not raise
        with tempfile.TemporaryDirectory() as tmpdir2:
            tmp_root2 = Path(tmpdir2)
            loaded2 = load_app_env(app_root=tmp_root2)
            self.assertIsNone(loaded2)


if __name__ == "__main__":
    unittest.main()
