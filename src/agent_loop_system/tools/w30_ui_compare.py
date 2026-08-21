"""Isolated Agent-loop adapter for the standalone W30 UI comparison CLI.

The Qt tool owns the visual score and PASS/FAIL decision.  This module only
launches it, validates its versioned JSON/evidence identity, and preserves the
result for Agent-loop callers.  It deliberately exposes local ``pixel`` mode
only; AI credentials and W30 device control are outside this adapter.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


ADAPTER_NAME = "agent_loop_w30_ui_compare"
ADAPTER_SCHEMA_VERSION = 1
TOOL_SCHEMA_VERSION = 1
TOOL_NAME = "ui_compare_cli"
TOOL_ENVIRONMENT_VARIABLE = "W30_UI_COMPARE_CLI_PATH"
DEFAULT_TIMEOUT_SECONDS = 30
PROCESS_TIMEOUT_GRACE_SECONDS = 10

EXIT_OK = 0
EXIT_INVALID_INPUT = 2
EXIT_TOOL_ERROR = 3
EXIT_CONTRACT_ERROR = 4
EXIT_OUTPUT_ERROR = 5

Runner = Callable[..., subprocess.CompletedProcess[str]]


class W30UiCompareError(RuntimeError):
    """Base error with a stable machine-readable code and CLI exit code."""

    cli_exit_code = EXIT_TOOL_ERROR

    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.details = dict(details or {})


class W30UiCompareInputError(W30UiCompareError, ValueError):
    """The adapter configuration or local input is invalid."""

    cli_exit_code = EXIT_INVALID_INPUT


class W30UiCompareToolError(W30UiCompareError):
    """The external tool could not be launched or complete successfully."""

    cli_exit_code = EXIT_TOOL_ERROR


class W30UiCompareContractError(W30UiCompareError):
    """The external tool returned data that violates the integration contract."""

    cli_exit_code = EXIT_CONTRACT_ERROR


@dataclass(frozen=True, slots=True)
class W30UiCompareResult:
    """One contract-validated W30 automatic comparison result."""

    tool_path: str
    diagnostics: tuple[str, ...]
    tool_result: dict[str, Any]

    @property
    def passed(self) -> bool:
        return bool(self.tool_result["result"]["passed"])

    @property
    def score(self) -> float:
        return float(self.tool_result["result"]["score"])

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": ADAPTER_SCHEMA_VERSION,
            "adapter": {
                "name": ADAPTER_NAME,
                "comparison_profile": "W30",
                "mode": "pixel",
                "verdict_owner": TOOL_NAME,
                "tool_path": self.tool_path,
            },
            "execution_status": "completed",
            "tool_exit_code": 0,
            "diagnostics": list(self.diagnostics),
            "tool_result": self.tool_result,
        }


def _input_file(value: str | os.PathLike[str], field: str) -> Path:
    try:
        path = Path(value).expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise W30UiCompareInputError(
            f"{field}_not_found",
            f"{field} file does not exist: {value}",
        ) from exc
    if not path.is_file():
        raise W30UiCompareInputError(
            f"{field}_not_file",
            f"{field} path is not a file: {path}",
        )
    return path


def _tool_file(value: str | os.PathLike[str] | None) -> Path:
    configured = value
    if configured is None or not str(configured).strip():
        configured = os.environ.get(TOOL_ENVIRONMENT_VARIABLE, "").strip()
    if not configured:
        raise W30UiCompareInputError(
            "tool_not_configured",
            "W30 UI comparison tool is not configured; pass --tool or set "
            f"{TOOL_ENVIRONMENT_VARIABLE}",
        )
    return _input_file(str(configured).strip().strip('"'), "tool")


def _timeout_seconds(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 3600:
        raise W30UiCompareInputError(
            "invalid_timeout",
            "timeout_seconds must be an integer from 1 to 3600",
        )
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _same_path(value: object, expected: Path) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    try:
        returned = Path(value).expanduser().resolve(strict=False)
    except (OSError, RuntimeError):
        return False
    return os.path.normcase(str(returned)) == os.path.normcase(str(expected))


def _diagnostics(stderr: str | None) -> tuple[str, ...]:
    lines = [line.strip() for line in (stderr or "").splitlines() if line.strip()]
    # Keep the evidence useful without allowing an external process to create an
    # unbounded Agent-loop result. Pixel mode currently emits only two lines.
    return tuple(line[:2_000] for line in lines[:200])


def _json_object(stdout: str, *, returncode: int) -> dict[str, Any]:
    try:
        value = json.loads(stdout)
    except (json.JSONDecodeError, TypeError) as exc:
        error_type: type[W30UiCompareError]
        error_type = (
            W30UiCompareToolError
            if returncode != 0
            else W30UiCompareContractError
        )
        raise error_type(
            "tool_json_invalid",
            "ui_compare_cli did not return one valid JSON document",
            details={"tool_exit_code": returncode},
        ) from exc
    if not isinstance(value, dict):
        raise W30UiCompareContractError(
            "tool_json_not_object",
            "ui_compare_cli JSON root must be an object",
        )
    return value


def _contract_failure(code: str, message: str) -> None:
    raise W30UiCompareContractError(code, message)


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _validate_image_identity(
    payload: object,
    *,
    label: str,
    expected: Path,
) -> None:
    if not isinstance(payload, dict):
        _contract_failure(
            f"{label}_identity_missing",
            f"tool result is missing inputs.{label}",
        )
    if not _same_path(payload.get("path"), expected):
        _contract_failure(
            f"{label}_path_mismatch",
            f"tool result {label} path does not match the requested file",
        )
    if str(payload.get("sha256", "")).casefold() != _sha256(expected):
        _contract_failure(
            f"{label}_sha256_mismatch",
            f"tool result {label} SHA-256 does not match the current file",
        )
    byte_count = payload.get("bytes")
    if (
        isinstance(byte_count, bool)
        or not isinstance(byte_count, (int, float))
        or int(byte_count) != expected.stat().st_size
    ):
        _contract_failure(
            f"{label}_size_mismatch",
            f"tool result {label} byte count does not match the current file",
        )
    for dimension in ("width", "height"):
        value = payload.get(dimension)
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            _contract_failure(
                f"{label}_{dimension}_invalid",
                f"tool result {label} {dimension} must be a positive integer",
            )


def _validate_tool_contract(
    payload: dict[str, Any],
    *,
    actual: Path,
    reference: Path,
) -> None:
    schema_version = payload.get("schema_version")
    if (
        isinstance(schema_version, bool)
        or schema_version != TOOL_SCHEMA_VERSION
    ):
        _contract_failure(
            "tool_schema_unsupported",
            f"ui_compare_cli schema_version must be {TOOL_SCHEMA_VERSION}",
        )
    tool = payload.get("tool")
    if not isinstance(tool, dict) or tool.get("name") != TOOL_NAME:
        _contract_failure("tool_identity_invalid", "unexpected comparison tool identity")
    for field in ("version", "qt_version"):
        if not isinstance(tool.get(field), str) or not tool[field].strip():
            _contract_failure(
                f"tool_{field}_missing",
                f"ui_compare_cli tool.{field} must be present",
            )
    if payload.get("command") != "compare":
        _contract_failure("tool_command_invalid", "tool result command must be compare")
    if payload.get("execution_status") != "completed":
        _contract_failure(
            "tool_execution_incomplete",
            "tool returned exit code 0 without execution_status=completed",
        )
    if payload.get("mode") != "pixel" or payload.get("provider") is not None:
        _contract_failure(
            "tool_mode_invalid",
            "Agent-loop adapter accepts only offline pixel results",
        )

    inputs = payload.get("inputs")
    if not isinstance(inputs, dict):
        _contract_failure("tool_inputs_missing", "tool result inputs must be present")
    _validate_image_identity(inputs.get("actual"), label="actual", expected=actual)
    _validate_image_identity(
        inputs.get("reference"),
        label="reference",
        expected=reference,
    )

    thresholds = payload.get("thresholds")
    if not isinstance(thresholds, dict) or _number(thresholds.get("pixel_pass")) is None:
        _contract_failure(
            "tool_thresholds_missing",
            "tool result must record the engine-owned pixel threshold",
        )

    result = payload.get("result")
    if not isinstance(result, dict):
        _contract_failure("tool_result_missing", "tool result object is missing")
    passed = result.get("passed")
    if not isinstance(passed, bool):
        _contract_failure("tool_passed_invalid", "result.passed must be boolean")
    expected_status = "PASS" if passed else "FAIL"
    if result.get("status") != expected_status:
        _contract_failure(
            "tool_status_inconsistent",
            "result.status does not agree with result.passed",
        )
    score = _number(result.get("score"))
    if score is None or not 0.0 <= score <= 1.0:
        _contract_failure("tool_score_invalid", "result.score must be within 0..1")
    if result.get("decision_source") != "automatic_w30_engine":
        _contract_failure(
            "tool_decision_source_invalid",
            "result must come from automatic_w30_engine",
        )
    if result.get("manual_review_applied") is not False:
        _contract_failure(
            "tool_manual_review_invalid",
            "pixel CLI result must declare manual_review_applied=false",
        )

    pixel_result = payload.get("pixel_result")
    if not isinstance(pixel_result, dict):
        _contract_failure("tool_pixel_result_missing", "pixel_result must be present")
    if pixel_result.get("passed") is not passed:
        _contract_failure(
            "tool_pixel_result_inconsistent",
            "pixel_result.passed does not agree with result.passed",
        )
    pixel_score = _number(pixel_result.get("score"))
    if pixel_score is None or not math.isclose(pixel_score, score, abs_tol=1e-12):
        _contract_failure(
            "tool_pixel_score_inconsistent",
            "pixel_result.score does not agree with result.score",
        )
    if payload.get("ai_result") is not None:
        _contract_failure(
            "tool_ai_result_unexpected",
            "offline pixel result must not contain an AI result",
        )


def compare_w30_ui(
    actual_path: str | os.PathLike[str],
    reference_path: str | os.PathLike[str],
    *,
    tool_path: str | os.PathLike[str] | None = None,
    case_name: str | None = None,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    runner: Runner = subprocess.run,
) -> W30UiCompareResult:
    """Run and audit one offline W30 comparison without rejudging it."""

    tool = _tool_file(tool_path)
    actual = _input_file(actual_path, "actual")
    reference = _input_file(reference_path, "reference")
    timeout = _timeout_seconds(timeout_seconds)

    command = [
        str(tool),
        "compare",
        "--actual",
        str(actual),
        "--reference",
        str(reference),
        "--mode",
        "pixel",
        "--timeout-seconds",
        str(timeout),
        "--json",
    ]
    if case_name and case_name.strip():
        command[2:2] = ["--name", case_name.strip()]

    run_kwargs: dict[str, Any] = {
        "capture_output": True,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "timeout": timeout + PROCESS_TIMEOUT_GRACE_SECONDS,
        "check": False,
    }
    if os.name == "nt":
        run_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

    try:
        completed = runner(command, **run_kwargs)
    except subprocess.TimeoutExpired as exc:
        raise W30UiCompareToolError(
            "tool_process_timeout",
            "ui_compare_cli process exceeded the adapter deadline",
            details={"timeout_seconds": timeout + PROCESS_TIMEOUT_GRACE_SECONDS},
        ) from exc
    except OSError as exc:
        raise W30UiCompareToolError(
            "tool_launch_failed",
            f"could not launch ui_compare_cli: {exc}",
        ) from exc

    payload = _json_object(completed.stdout, returncode=completed.returncode)
    diagnostics = _diagnostics(completed.stderr)
    if completed.returncode != 0:
        tool_error = payload.get("error") if isinstance(payload.get("error"), dict) else {}
        raise W30UiCompareToolError(
            "tool_exit_nonzero",
            f"ui_compare_cli exited with code {completed.returncode}",
            details={
                "tool_exit_code": completed.returncode,
                "tool_error": tool_error,
                "diagnostics": list(diagnostics),
            },
        )

    _validate_tool_contract(payload, actual=actual, reference=reference)
    return W30UiCompareResult(
        tool_path=str(tool),
        diagnostics=diagnostics,
        tool_result=payload,
    )


def _error_payload(exc: W30UiCompareError) -> dict[str, Any]:
    error: dict[str, Any] = {"code": exc.code, "message": str(exc)}
    if exc.details:
        error["details"] = exc.details
    return {
        "schema_version": ADAPTER_SCHEMA_VERSION,
        "adapter": {
            "name": ADAPTER_NAME,
            "comparison_profile": "W30",
            "mode": "pixel",
            "verdict_owner": TOOL_NAME,
        },
        "execution_status": "error",
        "error": error,
    }


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    destination = path.expanduser().resolve(strict=False)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            prefix=f".{destination.name}.",
            suffix=".tmp",
            dir=destination.parent,
            delete=False,
        ) as stream:
            temporary_path = Path(stream.name)
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, destination)
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Call and audit the standalone W30 ui_compare_cli in offline pixel mode"
        )
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    compare = subparsers.add_parser("compare")
    compare.add_argument("--tool", help=f"Defaults to {TOOL_ENVIRONMENT_VARIABLE}")
    compare.add_argument("--actual", required=True)
    compare.add_argument("--reference", required=True)
    compare.add_argument("--name")
    compare.add_argument(
        "--timeout-seconds",
        type=int,
        default=DEFAULT_TIMEOUT_SECONDS,
    )
    compare.add_argument("--output", help="Atomically write the adapter JSON")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = compare_w30_ui(
            args.actual,
            args.reference,
            tool_path=args.tool,
            case_name=args.name,
            timeout_seconds=args.timeout_seconds,
        )
        payload = result.to_dict()
        exit_code = EXIT_OK
    except W30UiCompareError as exc:
        payload = _error_payload(exc)
        exit_code = exc.cli_exit_code

    if args.output:
        try:
            _write_json_atomic(Path(args.output), payload)
        except OSError as exc:
            payload = _error_payload(
                W30UiCompareError(
                    "output_write_failed",
                    f"could not write adapter output: {exc}",
                )
            )
            exit_code = EXIT_OUTPUT_ERROR

    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    return exit_code


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
