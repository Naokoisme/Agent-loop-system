from __future__ import annotations

import unittest
from unittest import mock

from agent_loop_system.tools.llm_retry import (
    LLMRetryError,
    get_llm_request_timeout,
    invoke_with_retry,
)


class LLMRetryTest(unittest.TestCase):
    def test_request_timeout_uses_environment_value(self) -> None:
        with mock.patch.dict("os.environ", {"OPENAI_REQUEST_TIMEOUT": "45"}):
            self.assertEqual(get_llm_request_timeout(), 45.0)

    def test_invalid_request_timeout_uses_safe_default(self) -> None:
        for value in ("invalid", "0", "-1"):
            with self.subTest(value=value):
                with mock.patch.dict("os.environ", {"OPENAI_REQUEST_TIMEOUT": value}):
                    self.assertEqual(get_llm_request_timeout(), 120.0)

    def test_first_attempt_success_does_not_sleep(self) -> None:
        with mock.patch("agent_loop_system.tools.llm_retry.time.sleep") as sleep:
            result = invoke_with_retry(lambda: "ok")
        self.assertEqual(result, "ok")
        sleep.assert_not_called()

    def test_retries_with_backoff_until_success(self) -> None:
        calls = []

        def flaky():
            calls.append(1)
            if len(calls) < 12:
                raise RuntimeError("empty response")
            return "ok"

        with mock.patch("agent_loop_system.tools.llm_retry.time.sleep") as sleep:
            result = invoke_with_retry(flaky)
        self.assertEqual(result, "ok")
        self.assertEqual(len(calls), 12)
        self.assertEqual(
            [c.args[0] for c in sleep.call_args_list],
            [1, 2, 4, 8, 16, 32, 60, 60, 60, 60, 60],
        )

    def test_exhausted_raises_and_does_not_sleep_after_last(self) -> None:
        def always_fail():
            raise RuntimeError("boom")

        with mock.patch("agent_loop_system.tools.llm_retry.time.sleep") as sleep:
            with self.assertRaises(LLMRetryError):
                invoke_with_retry(always_fail)
        self.assertEqual(sleep.call_count, 11)

    def test_permanent_account_balance_error_fails_without_retry(self) -> None:
        calls = []

        def insufficient_balance():
            calls.append(1)
            raise RuntimeError("403: Insufficient account balance")

        with mock.patch("agent_loop_system.tools.llm_retry.time.sleep") as sleep:
            with self.assertRaisesRegex(LLMRetryError, "Insufficient account balance"):
                invoke_with_retry(insufficient_balance)
        self.assertEqual(len(calls), 1)
        sleep.assert_not_called()

    def test_invalid_result_counts_as_failure(self) -> None:
        calls = []

        def flaky():
            calls.append(1)
            return "ok" if len(calls) >= 2 else None

        with mock.patch("agent_loop_system.tools.llm_retry.time.sleep") as sleep:
            result = invoke_with_retry(flaky, is_valid=lambda v: v == "ok")
        self.assertEqual(result, "ok")
        self.assertEqual(len(calls), 2)
        sleep.assert_called_once_with(1)


if __name__ == "__main__":
    unittest.main()
