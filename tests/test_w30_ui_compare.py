from __future__ import annotations

import contextlib
import hashlib
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_loop_system.tools.w30_ui_compare import (
    EXIT_OK,
    W30UiCompareContractError,
    W30UiCompareInputError,
    W30UiCompareResult,
    W30UiCompareToolError,
    compare_w30_ui,
    main,
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _tool_payload(
    actual: Path,
    reference: Path,
    *,
    passed: bool,
) -> dict[str, object]:
    score = 1.0 if passed else 0.0
    return {
        "schema_version": 1,
        "tool": {
            "name": "ui_compare_cli",
            "version": "1.0.5",
            "qt_version": "6.8.3",
        },
        "command": "compare",
        "execution_status": "completed",
        "duration_ms": 12,
        "case_name": "adapter_test",
        "mode": "pixel",
        "provider": None,
        "inputs": {
            "actual": {
                "path": str(actual.resolve()),
                "sha256": _sha256(actual),
                "bytes": actual.stat().st_size,
                "width": 8,
                "height": 8,
                "file_extension": "png",
            },
            "reference": {
                "path": str(reference.resolve()),
                "sha256": _sha256(reference),
                "bytes": reference.stat().st_size,
                "width": 8,
                "height": 8,
                "file_extension": "png",
            },
        },
        "thresholds": {
            "pixel_pass": 0.8,
            "ai_pass": 0.9,
            "both_pixel_veto": 0.6,
        },
        "result": {
            "status": "PASS" if passed else "FAIL",
            "passed": passed,
            "score": score,
            "summary": "same" if passed else "different",
            "differences": [] if passed else ["different pixels"],
            "decision_source": "automatic_w30_engine",
            "manual_review_applied": False,
        },
        "pixel_result": {
            "passed": passed,
            "score": score,
            "summary": "same" if passed else "different",
            "differences": [] if passed else ["different pixels"],
        },
        "ai_result": None,
    }


class W30UiCompareAdapterTest(unittest.TestCase):
    def _files(self, root: Path) -> tuple[Path, Path, Path]:
        tool = root / "ui_compare_cli.exe"
        actual = root / "actual.png"
        reference = root / "reference.png"
        tool.write_bytes(b"MZ-fake-tool")
        actual.write_bytes(b"actual-image")
        reference.write_bytes(b"reference-image")
        return tool, actual, reference

    def test_pass_and_fail_remain_tool_owned_completed_results(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            tool, actual, reference = self._files(Path(temporary_dir))
            for passed in (True, False):
                with self.subTest(passed=passed):
                    payload = _tool_payload(actual, reference, passed=passed)
                    calls: list[tuple[list[str], dict[str, object]]] = []

                    def runner(argv, **kwargs):
                        calls.append((list(argv), dict(kwargs)))
                        return subprocess.CompletedProcess(
                            argv,
                            0,
                            json.dumps(payload),
                            "engine diagnostic\n",
                        )

                    result = compare_w30_ui(
                        actual,
                        reference,
                        tool_path=tool,
                        case_name="CASE_001",
                        timeout_seconds=20,
                        runner=runner,
                    )

                    self.assertIs(result.passed, passed)
                    self.assertEqual(result.score, 1.0 if passed else 0.0)
                    self.assertEqual(result.diagnostics, ("engine diagnostic",))
                    envelope = result.to_dict()
                    self.assertEqual(envelope["execution_status"], "completed")
                    self.assertEqual(envelope["tool_exit_code"], 0)
                    self.assertEqual(
                        envelope["adapter"]["comparison_profile"],
                        "W30",
                    )
                    self.assertNotIn("target", envelope["adapter"])
                    self.assertNotIn("passed", envelope)
                    self.assertIs(
                        envelope["tool_result"]["result"]["passed"],
                        passed,
                    )

                    argv, kwargs = calls[0]
                    self.assertEqual(argv[0], str(tool.resolve()))
                    self.assertIn("pixel", argv)
                    self.assertIn("CASE_001", argv)
                    self.assertNotIn("ai", argv)
                    self.assertEqual(kwargs["timeout"], 30)
                    self.assertTrue(kwargs["capture_output"])
                    self.assertFalse(kwargs["check"])

    def test_nonzero_tool_exit_is_not_converted_to_visual_fail(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            tool, actual, reference = self._files(Path(temporary_dir))
            error_payload = {
                "schema_version": 1,
                "execution_status": "error",
                "error": {"code": "actual_image_invalid", "message": "bad image"},
            }

            def runner(argv, **_kwargs):
                return subprocess.CompletedProcess(
                    argv,
                    2,
                    json.dumps(error_payload),
                    "input rejected\n",
                )

            with self.assertRaises(W30UiCompareToolError) as context:
                compare_w30_ui(
                    actual,
                    reference,
                    tool_path=tool,
                    runner=runner,
                )

        self.assertEqual(context.exception.code, "tool_exit_nonzero")
        self.assertEqual(context.exception.details["tool_exit_code"], 2)
        self.assertEqual(
            context.exception.details["tool_error"]["code"],
            "actual_image_invalid",
        )
        self.assertNotIn("passed", context.exception.details)

    def test_contract_rejects_changed_input_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            tool, actual, reference = self._files(Path(temporary_dir))
            payload = _tool_payload(actual, reference, passed=True)
            payload["inputs"]["actual"]["sha256"] = "0" * 64

            def runner(argv, **_kwargs):
                return subprocess.CompletedProcess(argv, 0, json.dumps(payload), "")

            with self.assertRaises(W30UiCompareContractError) as context:
                compare_w30_ui(
                    actual,
                    reference,
                    tool_path=tool,
                    runner=runner,
                )

        self.assertEqual(context.exception.code, "actual_sha256_mismatch")

    def test_contract_rejects_manual_or_ai_result_on_pixel_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            tool, actual, reference = self._files(Path(temporary_dir))
            for mutation, expected_code in (
                ("manual", "tool_manual_review_invalid"),
                ("ai", "tool_ai_result_unexpected"),
            ):
                with self.subTest(mutation=mutation):
                    payload = _tool_payload(actual, reference, passed=True)
                    if mutation == "manual":
                        payload["result"]["manual_review_applied"] = True
                    else:
                        payload["ai_result"] = {"passed": True, "score": 1.0}

                    def runner(argv, **_kwargs):
                        return subprocess.CompletedProcess(
                            argv,
                            0,
                            json.dumps(payload),
                            "",
                        )

                    with self.assertRaises(W30UiCompareContractError) as context:
                        compare_w30_ui(
                            actual,
                            reference,
                            tool_path=tool,
                            runner=runner,
                        )
                    self.assertEqual(context.exception.code, expected_code)

    def test_missing_tool_is_rejected_before_process_launch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            actual = root / "actual.png"
            reference = root / "reference.png"
            actual.write_bytes(b"actual")
            reference.write_bytes(b"reference")
            called = False

            def runner(*_args, **_kwargs):
                nonlocal called
                called = True
                raise AssertionError("runner must not be called")

            with self.assertRaises(W30UiCompareInputError) as context:
                compare_w30_ui(
                    actual,
                    reference,
                    tool_path=root / "missing.exe",
                    runner=runner,
                )

        self.assertEqual(context.exception.code, "tool_not_found")
        self.assertFalse(called)

    def test_cli_visual_fail_writes_json_and_returns_zero(self) -> None:
        result = W30UiCompareResult(
            tool_path="D:/QtTools/ui_compare_cli.exe",
            diagnostics=("different",),
            tool_result={"result": {"passed": False, "score": 0.0}},
        )
        with tempfile.TemporaryDirectory() as temporary_dir:
            output_path = Path(temporary_dir) / "evidence" / "comparison.json"
            stdout = io.StringIO()
            with (
                patch(
                    "agent_loop_system.tools.w30_ui_compare.compare_w30_ui",
                    return_value=result,
                ),
                contextlib.redirect_stdout(stdout),
            ):
                exit_code = main(
                    [
                        "compare",
                        "--actual",
                        "ignored-actual.png",
                        "--reference",
                        "ignored-reference.png",
                        "--output",
                        str(output_path),
                    ]
                )

            stdout_payload = json.loads(stdout.getvalue())
            file_payload = json.loads(output_path.read_text(encoding="utf-8"))

        self.assertEqual(exit_code, EXIT_OK)
        self.assertEqual(stdout_payload, file_payload)
        self.assertEqual(stdout_payload["execution_status"], "completed")
        self.assertFalse(stdout_payload["tool_result"]["result"]["passed"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
