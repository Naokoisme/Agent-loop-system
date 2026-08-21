from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch as mock_patch

from agent_loop_system.tools.agent import Patch
from agent_loop_system.tools.patcher import apply_patch, rollback_patch


def _patch(**overrides) -> Patch:
    fields = dict(
        root_cause_analysis="test",
        file_path="app/inside.c",
        before="before();",
        after="after();",
        reason="test",
        test_commands=[":GUI_TREE:1"],
    )
    fields.update(overrides)
    return Patch(**fields)


class PatcherSecurityTest(unittest.TestCase):
    def test_rejects_path_outside_source_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with mock_patch.dict(os.environ, {"W30_SOURCE_ROOT": temporary}):
                result = apply_patch(_patch(file_path="../outside.c"))
        self.assertFalse(result.success)
        self.assertIn("越出", result.error)

    def test_applies_unique_replacement_inside_source_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "app" / "inside.c"
            source.parent.mkdir(parents=True)
            source.write_text("before();\n", encoding="utf-8")
            with mock_patch.dict(os.environ, {"W30_SOURCE_ROOT": temporary}):
                result = apply_patch(_patch())
            self.assertTrue(result.success)
            self.assertEqual(result.offset, 0)
            self.assertEqual(source.read_text(encoding="utf-8"), "after();\n")

    def test_before_not_found_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "app" / "inside.c"
            source.parent.mkdir(parents=True)
            source.write_text("other();\n", encoding="utf-8")
            with mock_patch.dict(os.environ, {"W30_SOURCE_ROOT": temporary}):
                result = apply_patch(_patch(before="missing();"))
            self.assertFalse(result.success)
            self.assertIn("未找到", result.error)
            self.assertEqual(source.read_text(encoding="utf-8"), "other();\n")

    def test_before_duplicated_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "app" / "inside.c"
            source.parent.mkdir(parents=True)
            source.write_text("before();\nbefore();\n", encoding="utf-8")
            with mock_patch.dict(os.environ, {"W30_SOURCE_ROOT": temporary}):
                result = apply_patch(_patch())
            self.assertFalse(result.success)
            self.assertIn("多次", result.error)
            self.assertEqual(source.read_text(encoding="utf-8"), "before();\nbefore();\n")

    def test_rollback_restores_original_at_matching_offset(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "app" / "inside.c"
            source.parent.mkdir(parents=True)
            source.write_text("prefix\nbefore();\nsuffix\n", encoding="utf-8")
            with mock_patch.dict(os.environ, {"W30_SOURCE_ROOT": temporary}):
                applied = apply_patch(_patch())
                self.assertTrue(applied.success)
                rolled = rollback_patch(_patch(), applied.offset)
            self.assertTrue(rolled.success)
            self.assertEqual(
                source.read_text(encoding="utf-8"), "prefix\nbefore();\nsuffix\n"
            )

    def test_rollback_rejects_when_offset_content_changed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "app" / "inside.c"
            source.parent.mkdir(parents=True)
            source.write_text("before();\n", encoding="utf-8")
            with mock_patch.dict(os.environ, {"W30_SOURCE_ROOT": temporary}):
                applied = apply_patch(_patch())
                self.assertTrue(applied.success)
                # 篡改文件，使 offset 处不再是 after
                source.write_text("tampered();\n", encoding="utf-8")
                rolled = rollback_patch(_patch(), applied.offset)
            self.assertFalse(rolled.success)
            self.assertIn("拒绝回滚", rolled.error)
            self.assertEqual(source.read_text(encoding="utf-8"), "tampered();\n")

    def test_non_pass_path_restores_original_bytes(self) -> None:
        """模拟 应用→校验失败→回滚 完整闭环，文件最终恢复原始字节内容（CRLF 与产品源码一致）。"""
        original = b"before();\r\nline2();\r\n"
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "app" / "inside.c"
            source.parent.mkdir(parents=True)
            source.write_bytes(original)
            with mock_patch.dict(os.environ, {"W30_SOURCE_ROOT": temporary}):
                applied = apply_patch(_patch())
                self.assertTrue(applied.success)
                self.assertNotEqual(source.read_bytes(), original)
                rolled = rollback_patch(_patch(), applied.offset)
            self.assertTrue(rolled.success)
            self.assertEqual(source.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
