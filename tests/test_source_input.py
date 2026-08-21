from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_loop_system.graph import agent_node
from agent_loop_system.main import _auto_source_files
from agent_loop_system.tools.designer import DesignerContext, DesignerPlan
from agent_loop_system.tools.source_context import load_runtime_navigation_sources


class AutoSourceFilesTest(unittest.TestCase):
    def test_preserves_order_dedups_case_insensitive_and_limits(self) -> None:
        defect = {
            "source_analysis": {
                "matches": [
                    {"path": "app/a.c"},
                    {"path": "app/ABC.c"},
                    {"path": "app/b.h"},
                    {"path": "app/a.C"},  # 与 app/a.c 不区分大小写重复
                    {"path": "app/c.c"},
                    {"path": "app/d.c"},
                    {"path": "app/e.c"},
                ]
            }
        }
        files = _auto_source_files(defect)
        self.assertEqual(files, ["app/a.c", "app/ABC.c", "app/b.h", "app/c.c", "app/d.c"])

    def test_empty_matches(self) -> None:
        self.assertEqual(_auto_source_files({"source_analysis": {"matches": []}}), [])
        self.assertEqual(_auto_source_files({}), [])


class RuntimeNavigationSourceTest(unittest.TestCase):
    def test_redirect_context_prefers_file_that_contains_target_and_actual_window(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            voice = root / "app" / "comm" / "TuoBu" / "voice_assistant" / "voice.c"
            voice.parent.mkdir(parents=True)
            voice.write_text(
                "static int voice_entry_redirect(void) {\n"
                "  if (!device->hfp_connect) return GUI_WIN_DISCONNECT_TIP;\n"
                "  return GUI_WIN_VOICE_ASSISTANT_INTERACTION;\n"
                "}\n",
                encoding="utf-8",
            )
            other = root / "app" / "comm" / "TuoBu" / "sidebar" / "sidebar.c"
            other.parent.mkdir(parents=True)
            other.write_text(
                "return GUI_WIN_VOICE_ASSISTANT_INTERACTION;\n",
                encoding="utf-8",
            )

            with mock.patch.dict(
                os.environ,
                {
                    "W30_SOURCE_ROOT": temporary,
                    "W30_PROJECT": "",
                    "W30_AGENT_WORKSPACE_ROOT": "",
                },
            ):
                files = load_runtime_navigation_sources(
                    "VOICE_ASSISTANT_INTERACTION",
                    "DISCONNECT_TIP",
                )

        self.assertGreaterEqual(len(files), 1)
        self.assertEqual(files[0]["path"], "app/comm/TuoBu/voice_assistant/voice.c")
        self.assertIn("hfp_connect", files[0]["content"])


class AgentNodeSourceValidationTest(unittest.TestCase):
    def _make_files(self, root: Path, names_chars: list[tuple[str, int]]) -> None:
        for name, chars in names_chars:
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("x" * chars, encoding="utf-8")

    def test_valid_relative_path_readable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            self._make_files(Path(temporary), [("app/a.c", 10)])
            fake = DesignerPlan(
                root_cause_analysis="analysis",
                page_name="TEST",
                layout_operations=[{"op": "set", "widgetId": "1", "properties": {}}],
                reason="r",
            )
            with mock.patch.dict(os.environ, {"W30_SOURCE_ROOT": temporary}):
                with (
                    mock.patch(
                        "agent_loop_system.tools.agent.generate_designer_plan",
                        return_value=fake,
                    ) as gp,
                    mock.patch(
                        "agent_loop_system.tools.designer.load_designer_context",
                        return_value=DesignerContext(
                            project_path=f"{temporary}/app/projects/P",
                            ui_layout_name="layout",
                            page_name="TEST",
                            page={},
                        ),
                    ),
                ):
                    update = agent_node({
                        "source_files": ["app/a.c"],
                        "objective": "bug",
                        "baseline_ready": True,
                        "baseline_output": {"defect_reproduced": True},
                        "agent_test_commands": [":ENTER_PAGE:TEST,0"],
                        "designer_enabled": True,
                    })
            gp.assert_called_once()
            self.assertEqual(update["designer_plan"]["page_name"], "TEST")
            self.assertEqual(gp.call_args.kwargs["source_files"][0]["content"], "x" * 10)

    def test_repair_ai_is_blocked_without_reliable_reproduction(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            self._make_files(Path(temporary), [("app/a.c", 10)])
            with mock.patch.dict(os.environ, {"W30_SOURCE_ROOT": temporary}):
                with mock.patch(
                    "agent_loop_system.tools.agent.generate_designer_plan"
                ) as gp:
                    update = agent_node({"source_files": ["app/a.c"], "objective": "bug"})
            gp.assert_not_called()
            self.assertEqual(update["error_code"], "REPRODUCTION_REQUIRED")

    def test_missing_designer_capability_is_reported_without_apply_plan(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            self._make_files(Path(temporary), [("app/a.c", 10)])
            blocked = DesignerPlan(
                root_cause_analysis="translation entry is wrong",
                page_name="TEST",
                blocked_capability="translation_entries",
                reason="Designer translation CRUD is unavailable",
            )
            context = DesignerContext(
                project_path=f"{temporary}/app/projects/P",
                ui_layout_name="layout",
                page_name="TEST",
                page={},
                capability_groups={
                    "translation_entries": {
                        "ready": False,
                        "missingTools": ["translation_get"],
                    }
                },
            )
            with mock.patch.dict(os.environ, {"W30_SOURCE_ROOT": temporary}):
                with (
                    mock.patch(
                        "agent_loop_system.tools.agent.generate_designer_plan",
                        return_value=blocked,
                    ),
                    mock.patch(
                        "agent_loop_system.tools.designer.load_designer_context",
                        return_value=context,
                    ),
                ):
                    update = agent_node(
                        {
                            "source_files": ["app/a.c"],
                            "objective": "wrong translation",
                            "baseline_ready": True,
                            "baseline_output": {"defect_reproduced": True},
                            "designer_enabled": True,
                        }
                    )
            self.assertEqual(update["verdict"], "CANNOT_VERIFY")
            self.assertEqual(update["error_code"], "DESIGNER_CAPABILITY_MISSING")
            self.assertEqual(
                update["test_output"]["missing_tools"], ["translation_get"]
            )

    def test_rejects_absolute_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with mock.patch.dict(os.environ, {"W30_SOURCE_ROOT": temporary}):
                update = agent_node({"source_files": [str(Path(temporary) / "a.c")], "objective": "bug"})
            self.assertEqual(update["verdict"], "CANNOT_VERIFY")
            self.assertEqual(update["error_code"], "SOURCE_INPUT_INVALID")

    def test_rejects_parent_escape(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with mock.patch.dict(os.environ, {"W30_SOURCE_ROOT": temporary}):
                update = agent_node({"source_files": ["../outside.c"], "objective": "bug"})
            self.assertEqual(update["verdict"], "CANNOT_VERIFY")
            self.assertIn("越出", update["error"])

    def test_rejects_missing_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with mock.patch.dict(os.environ, {"W30_SOURCE_ROOT": temporary}):
                update = agent_node({"source_files": ["app/missing.c"], "objective": "bug"})
            self.assertEqual(update["verdict"], "CANNOT_VERIFY")
            self.assertIn("不存在", update["error"])

    def test_rejects_non_c_h_suffix(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            self._make_files(Path(temporary), [("app/a.py", 10)])
            with mock.patch.dict(os.environ, {"W30_SOURCE_ROOT": temporary}):
                update = agent_node({"source_files": ["app/a.py"], "objective": "bug"})
            self.assertEqual(update["verdict"], "CANNOT_VERIFY")
            self.assertIn(".c/.h", update["error"])

    def test_rejects_single_file_over_150k(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            self._make_files(Path(temporary), [("app/big.c", 150001)])
            with mock.patch.dict(os.environ, {"W30_SOURCE_ROOT": temporary}):
                update = agent_node({"source_files": ["app/big.c"], "objective": "bug"})
            self.assertEqual(update["verdict"], "CANNOT_VERIFY")
            self.assertIn("150000", update["error"])

    def test_rejects_total_over_300k(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            self._make_files(
                Path(temporary),
                [
                    ("app/a.c", 100000),
                    ("app/b.c", 100000),
                    ("app/c.c", 100000),
                    ("app/d.c", 100000),
                ],
            )
            with mock.patch.dict(os.environ, {"W30_SOURCE_ROOT": temporary}):
                update = agent_node(
                    {"source_files": ["app/a.c", "app/b.c", "app/c.c", "app/d.c"], "objective": "bug"}
                )
            self.assertEqual(update["verdict"], "CANNOT_VERIFY")
            self.assertIn("300000", update["error"])

    def test_no_valid_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with mock.patch.dict(os.environ, {"W30_SOURCE_ROOT": temporary}):
                update = agent_node({"source_files": [], "objective": "bug"})
            self.assertEqual(update["verdict"], "CANNOT_VERIFY")
            self.assertIn("没有任何有效源码", update["error"])


if __name__ == "__main__":
    unittest.main()
