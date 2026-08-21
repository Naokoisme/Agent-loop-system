from __future__ import annotations

import unittest
from unittest import mock

from agent_loop_system.main import _normalize_result, main


class NormalizeResultTest(unittest.TestCase):
    def test_missing_keys_filled_with_none(self) -> None:
        result = _normalize_result({"task_id": "T1", "verdict": "PASS"})
        for key in (
            "patch", "patch_retained", "baseline_output", "build_success",
            "build_result", "test_output", "history", "error", "error_code",
            "rollback_error", "restore_build_result", "restore_build_error",
        ):
            self.assertIn(key, result)


class MainCliModeTest(unittest.TestCase):
    def _invoke(self, argv: list[str]) -> tuple[int, dict]:
        graph = mock.Mock()
        graph.invoke.return_value = {
            "task_id": "T1",
            "verdict": "PASS",
            "attempts": 1,
            "history": [],
        }
        with mock.patch.dict("os.environ"):
            with mock.patch("agent_loop_system.graph.build_graph", return_value=graph):
                code = main(argv)
        return code, graph.invoke.call_args.args[0]

    def test_objective_mode_uses_objective_as_judge_criteria(self) -> None:
        code, state = self._invoke(
            ["--objective", "天气显示异常", "--source-file", "app/a.c", "--task-id", "T1"]
        )
        self.assertEqual(code, 0)
        self.assertEqual(state["judge_criteria"], "天气显示异常")
        self.assertEqual(state["source_files"], ["app/a.c"])
        self.assertEqual(state["target"], "simulator")

    def test_explicit_hardware_target_is_forwarded_to_graph(self) -> None:
        code, state = self._invoke(
            [
                "--objective",
                "天气显示异常",
                "--source-file",
                "app/a.c",
                "--task-id",
                "T1",
                "--target",
                "hardware",
            ]
        )
        self.assertEqual(code, 0)
        self.assertEqual(state["target"], "hardware")

    def test_objective_mode_without_source_file_still_passes_empty(self) -> None:
        # 无 --source-file：由共享源码校验在图中返回 CANNOT_VERIFY
        code, state = self._invoke(["--objective", "bug", "--task-id", "T1"])
        self.assertEqual(state["source_files"], [])

    def test_defect_mode_auto_fills_source_files(self) -> None:
        defect = {
            "number": "196482",
            "title": "天气显示 unknown",
            "description": "broken clouds",
            "attachments": [
                {"name": "screen.png", "summary": "预期显示 Cloudy，实际显示 unknown"}
            ],
            "source_analysis": {
                "matches": [
                    {"path": "app/a.c"},
                    {"path": "app/a.C"},  # 大小写重复
                    {"path": "app/b.h"},
                ]
            },
        }
        with mock.patch(
            "agent_loop_system.tools.defect_store.load_defect", return_value=defect
        ), mock.patch(
            "agent_loop_system.tools.defect_store.build_objective", return_value="objective"
        ), mock.patch(
            "agent_loop_system.tools.defect_store.list_defect_images", return_value=[]
        ):
            code, state = self._invoke(["--defect", "196482", "--task-id", "196482"])
        self.assertEqual(code, 0)
        self.assertEqual(state["source_files"], ["app/a.c", "app/b.h"])
        self.assertIn("天气显示 unknown", state["judge_criteria"])
        self.assertIn("预期显示 Cloudy，实际显示 unknown", state["judge_criteria"])
        self.assertEqual(state["defect_image_paths"], [])

    def test_defect_mode_explicit_source_file_wins(self) -> None:
        defect = {
            "number": "196482",
            "title": "t",
            "description": "d",
            "source_analysis": {"matches": [{"path": "app/a.c"}]},
        }
        with mock.patch(
            "agent_loop_system.tools.defect_store.load_defect", return_value=defect
        ), mock.patch(
            "agent_loop_system.tools.defect_store.build_objective", return_value="objective"
        ):
            code, state = self._invoke(
                ["--defect", "196482", "--task-id", "196482", "--source-file", "app/custom.c"]
            )
        self.assertEqual(state["source_files"], ["app/custom.c"])


if __name__ == "__main__":
    unittest.main()
