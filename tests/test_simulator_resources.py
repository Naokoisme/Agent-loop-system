from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_loop_system.tools.simulator import verify_simulator_resource_provenance
from agent_loop_system.tools.test import DEFAULT_SIM_EXE, get_simulator_exe


class SimulatorResourceTests(unittest.TestCase):
    def _workspace(self) -> tuple[tempfile.TemporaryDirectory, Path, Path, Path]:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        root = Path(tempdir.name)
        exe = root / "core" / "gui" / "simulator" / "bin" / "main.exe"
        source_res = root / "app" / "projects" / "620C_W6830" / "assets" / "res"
        runtime_res = exe.parent.parent / "fs_dir" / "res"
        exe.parent.mkdir(parents=True)
        source_res.mkdir(parents=True)
        runtime_res.mkdir(parents=True)
        exe.write_bytes(b"exe")
        (root / "app" / "ProjectConfig.cmake").write_text(
            "set(PROJECT 620C_W6830)\n", encoding="utf-8"
        )
        for name in ("str_res.bin", "img_res.bin"):
            (source_res / name).write_bytes(name.encode("ascii"))
            (runtime_res / name).write_bytes(name.encode("ascii"))
        return tempdir, root, exe, runtime_res

    def test_matching_project_and_resources_pass(self) -> None:
        _, root, exe, _ = self._workspace()

        hashes = verify_simulator_resource_provenance(
            exe, source_root=root, project="620C_W6830"
        )

        self.assertEqual(set(hashes), {"str_res.bin", "img_res.bin"})

    def test_mismatched_runtime_resource_is_rejected(self) -> None:
        _, root, exe, runtime_res = self._workspace()
        (runtime_res / "str_res.bin").write_bytes(b"wrong project")

        with self.assertRaisesRegex(RuntimeError, "str_res.bin"):
            verify_simulator_resource_provenance(
                exe, source_root=root, project="620C_W6830"
            )

    def test_executable_outside_workspace_is_rejected(self) -> None:
        _, root, _, _ = self._workspace()
        outside = root.parent / "other" / "main.exe"

        with self.assertRaisesRegex(RuntimeError, "不属于 Agent 工作区"):
            verify_simulator_resource_provenance(
                outside, source_root=root, project="620C_W6830"
            )

    def test_simulator_path_is_read_from_environment_at_call_time(self) -> None:
        with mock.patch.dict(
            "os.environ", {"SIMULATOR_ARTIFACT_PATH": r"D:\isolated\main.exe"}
        ):
            self.assertEqual(get_simulator_exe(), r"D:\isolated\main.exe")
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertEqual(get_simulator_exe(), DEFAULT_SIM_EXE)


if __name__ == "__main__":
    unittest.main()
