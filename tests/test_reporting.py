from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from agent_loop_system import reporting


class ReportingTest(unittest.TestCase):
    def tearDown(self) -> None:
        reporting.configure(None)

    def test_tracked_node_writes_progress_and_final_result(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            progress_file = Path(temporary) / "progress.json"
            result_file = Path(temporary) / "result.json"
            reporting.configure(progress_file, task_id="T1")
            node = reporting.tracked_node("validate", lambda state: {"verdict": "PENDING", "error": None})
            update = node({"task_id": "T1", "attempts": 0})
            reporting.finish({"verdict": "PASS", "attempts": 1})
            reporting.write_result(result_file, {"verdict": "PASS"})

            progress = json.loads(progress_file.read_text(encoding="utf-8"))
            result = json.loads(result_file.read_text(encoding="utf-8"))
            self.assertEqual(update["verdict"], "PENDING")
            self.assertEqual(progress["nodes"]["validate"], "pass")
            self.assertEqual(progress["status"], "completed")
            self.assertEqual(progress["verdict"], "PASS")
            self.assertEqual(result["verdict"], "PASS")


if __name__ == "__main__":
    unittest.main()
