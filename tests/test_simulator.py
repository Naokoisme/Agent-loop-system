from __future__ import annotations

import json
import os
import unittest

from agent_loop_system.tools.simulator import READY_MARKERS, SimulatorSession


class SimulatorCompletionTest(unittest.TestCase):
    def test_sessions_use_distinct_command_inboxes(self) -> None:
        first = SimulatorSession("fake.exe")
        second = SimulatorSession("fake.exe")

        self.assertNotEqual(first.inbox, second.inbox)
        self.assertEqual(os.path.dirname(first.inbox), os.path.join(first.cwd, "data"))
        self.assertTrue(os.path.basename(first.inbox).startswith("w30-test-command-"))

    def test_start_waits_until_first_window_is_open(self) -> None:
        self.assertIn("gui_comm_system_open_first_window", READY_MARKERS)
        self.assertIn("============= [End] create new win", READY_MARKERS)
        self.assertNotIn("app init done", READY_MARKERS)
        self.assertNotIn("shell> ", READY_MARKERS)

    def test_explicit_environment_is_scoped_to_the_session(self) -> None:
        session = SimulatorSession(
            "fake.exe",
            environment={
                "W30_PROJECT": "6202_W5230",
                "SIMULATOR_SHELL_READY_MARKER": "shell-ready",
                "SIMULATOR_GUI_COMMAND_READY_MARKER": "gui-ready",
            },
        )

        self.assertEqual(session.environment["W30_PROJECT"], "6202_W5230")
        self.assertEqual(session._automation_ready_marker, "shell-ready")
        self.assertEqual(session._gui_command_ready_marker, "gui-ready")

    def _session_with_lines(self, *objects: dict) -> SimulatorSession:
        session = SimulatorSession("fake.exe", cmd_timeout=0.2)
        session.process = object()  # send() 只要求会话已经启动

        def write(_command: str) -> None:
            with session._lock:
                session._lines.extend(json.dumps(obj) for obj in objects)

        session._write = write
        return session

    def _session_with_raw_lines(self, *lines: str) -> SimulatorSession:
        session = SimulatorSession("fake.exe", cmd_timeout=0.2)
        session.process = object()

        def write(_command: str) -> None:
            with session._lock:
                session._lines.extend(lines)

        session._write = write
        return session

    def test_default_behavior_still_returns_accepted(self) -> None:
        session = self._session_with_lines(
            {"type": "command_result", "request": "gui_ping", "status": "accepted"},
            {"type": "gui_ack", "request": "gui_ping", "status": "processed"},
        )

        result = session.send(":GUI_PING:1")

        self.assertEqual(result.status, "accepted")
        self.assertEqual(result.raw["type"], "command_result")

    def test_expected_completion_skips_accepted_and_waits_for_processed(self) -> None:
        session = self._session_with_lines(
            {"type": "command_result", "request": "gui_ping", "status": "accepted"},
            {"type": "gui_ack", "request": "gui_ping", "status": "processed"},
        )

        result = session.send(
            ":GUI_PING:2",
            expected_type="gui_ack",
            expected_status="processed",
        )

        self.assertEqual(result.status, "processed")
        self.assertEqual(result.raw["type"], "gui_ack")

    def test_expected_completion_reads_json_after_shell_and_log_prefixes(self) -> None:
        session = self._session_with_raw_lines(
            'shell> {"type":"command_result","request":"gui_ping","status":"accepted"}',
            'gui_thread_status: 1{"type":"gui_ack","request":"gui_ping","status":"processed"}',
        )

        result = session.send(
            ":GUI_PING:22",
            expected_type="gui_ack",
            expected_status="processed",
        )

        self.assertEqual(result.status, "processed")
        self.assertEqual(result.raw["type"], "gui_ack")

    def test_expected_completion_returns_terminal_failure_without_timeout(self) -> None:
        session = self._session_with_lines(
            {
                "type": "command_result",
                "request": "gui_tree",
                "status": "busy",
                "reason": "transitioning",
            }
        )

        result = session.send(
            ":GUI_TREE:3",
            expected_type="gui_tree_end",
            expected_status="ok",
        )

        self.assertEqual(result.status, "busy")
        self.assertEqual(result.raw["reason"], "transitioning")

    def test_expected_type_returns_its_error_status_without_timeout(self) -> None:
        session = self._session_with_lines(
            {
                "type": "gui_tree_end",
                "request": "gui_tree",
                "status": "error",
                "reason": "node_serialization_failed",
            }
        )

        result = session.send(
            ":GUI_TREE:4",
            expected_type="gui_tree_end",
            expected_status="ok",
        )

        self.assertEqual(result.status, "error")
        self.assertEqual(result.raw["type"], "gui_tree_end")


if __name__ == "__main__":
    unittest.main()
