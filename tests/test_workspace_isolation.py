from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_loop_system.tools.workspace import (
    WorkspaceConflictError,
    ensure_path_in_workspace,
    resolve_source_root,
)


class WorkspaceIsolationTests(unittest.TestCase):
    def _workspace(self, root: Path, project: str = "620C_W6830") -> Path:
        app = root / "app"
        app.mkdir(parents=True)
        (app / "ProjectConfig.cmake").write_text(
            f"set(PROJECT {project})\n", encoding="utf-8"
        )
        return root

    def test_accepts_dedicated_workspace_and_project(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._workspace(Path(tmp))
            env = {
                "W30_SOURCE_ROOT": str(root),
                "W30_AGENT_WORKSPACE_ROOT": str(root),
                "W30_PROJECT": "620C_W6830",
            }
            with mock.patch.dict(os.environ, env, clear=True):
                self.assertEqual(resolve_source_root(), root.resolve())
                self.assertEqual(
                    ensure_path_in_workspace(root / "build", "build"),
                    (root / "build").resolve(),
                )

    def test_rejects_source_root_outside_dedicated_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            source = self._workspace(base / "source")
            expected = self._workspace(base / "agent")
            env = {
                "W30_SOURCE_ROOT": str(source),
                "W30_AGENT_WORKSPACE_ROOT": str(expected),
                "W30_PROJECT": "620C_W6830",
            }
            with mock.patch.dict(os.environ, env, clear=True):
                with self.assertRaisesRegex(WorkspaceConflictError, "WORKSPACE_CONFLICT"):
                    resolve_source_root()

    def test_rejects_wrong_project_and_external_build_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = self._workspace(base / "agent", "6202_W5230")
            env = {
                "W30_SOURCE_ROOT": str(root),
                "W30_AGENT_WORKSPACE_ROOT": str(root),
                "W30_PROJECT": "620C_W6830",
            }
            with mock.patch.dict(os.environ, env, clear=True):
                with self.assertRaisesRegex(WorkspaceConflictError, "当前项目是 6202_W5230"):
                    resolve_source_root()
                with self.assertRaisesRegex(WorkspaceConflictError, "越出 Agent 专用工作区"):
                    ensure_path_in_workspace(base / "shared-build", "build")


if __name__ == "__main__":
    unittest.main()
