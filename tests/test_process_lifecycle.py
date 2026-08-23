from __future__ import annotations

import os
import unittest
from unittest.mock import Mock, patch

from agent_loop_system.process_lifecycle import (
    communicate_process,
    positive_timeout,
    process_identity,
    process_is_alive,
    terminate_pid_tree,
)


class ProcessLifecycleTests(unittest.TestCase):
    def test_current_process_has_a_stable_os_identity(self) -> None:
        self.assertTrue(process_is_alive(os.getpid()))
        self.assertIsNotNone(process_identity(os.getpid()))

    def test_invalid_timeout_environment_falls_back_without_breaking_startup(self) -> None:
        for value in ("invalid", "0", "-1", "nan", "inf"):
            with self.subTest(value=value), patch.dict(
                os.environ,
                {"AGENT_LOOP_TEST_TIMEOUT": value},
                clear=False,
            ):
                self.assertEqual(
                    positive_timeout("AGENT_LOOP_TEST_TIMEOUT", 30.0),
                    30.0,
                )

    def test_persisted_pid_is_not_terminated_when_start_identity_changed(self) -> None:
        with (
            patch(
                "agent_loop_system.process_lifecycle.process_is_alive",
                return_value=True,
            ),
            patch(
                "agent_loop_system.process_lifecycle.process_identity",
                return_value="new-process",
            ),
            patch("agent_loop_system.process_lifecycle.subprocess.run") as run,
        ):
            stopped = terminate_pid_tree(
                12345,
                expected_identity="original-process",
            )

        self.assertFalse(stopped)
        run.assert_not_called()

    def test_communicate_forwards_the_timeout(self) -> None:
        process = Mock()
        process.communicate.return_value = ("out", "err")

        self.assertEqual(communicate_process(process, 12.5), ("out", "err"))
        process.communicate.assert_called_once_with(timeout=12.5)


if __name__ == "__main__":
    unittest.main()
