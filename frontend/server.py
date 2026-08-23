"""W30 Agent 自闭环演示前端的零依赖 HTTP 服务。

启动：uv run python frontend/server.py --port 8765
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import copy
from contextlib import contextmanager
import hashlib
import ipaddress
import io
import math
import openpyxl
from agent_loop_system.tools.update_checker import (
    check_for_updates,
    get_current_system_version,
)
import json
import mimetypes
import os
import re
import shutil
import socket
import ssl
import struct
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlparse

from agent_loop_system.internal_dispatcher import build_child_command
from agent_loop_system.outcome import outcome_fields
from agent_loop_system.process_lifecycle import (
    DEFAULT_TERMINATION_GRACE_SECONDS as PROCESS_TERMINATION_GRACE_SECONDS,
    communicate_process as _communicate_process,
    positive_timeout as _positive_timeout,
    process_identity as _process_identity,
    process_is_alive as _process_is_alive,
    terminate_pid_tree as _terminate_pid_tree,
    terminate_process_tree as _terminate_process_tree,
)
from agent_loop_system.runtime_root import (
    RuntimePaths,
    load_app_env,
    resolve_app_root,
    resolve_config_path,
)
from agent_loop_system.tools.case_map import (
    OBSERVATION_ONLY_COMMANDS,
    validated_case_entries,
)
from agent_loop_system.tools.command_protocol import normalize_command
from agent_loop_system.tools.external_execution_history import (
    read_external_execution_history,
)
from agent_loop_system.tools.watch_ble import (
    WatchBleClient,
    WatchBleConnectionError,
    WatchBleDevice,
    scan_watches,
)


WORKFLOW_NODES = (
    "validate", "interactive_reproduce", "agent", "apply", "build", "test", "record"
)
TEST_WORKFLOW_NODES = ("load", "execute", "judge", "record")
SAFE_SEGMENT = re.compile(r"^[A-Za-z0-9_.-]+$")
TEST_SCREENSHOT_FILE = re.compile(r"^screenshot(?:-\d{2,3})?\.bmp$")
LIVE_TEST_SCREENSHOT_FILE = re.compile(
    r"^(?:screenshot(?:-\d{2,3})?|step_\d{2,3})\.bmp$"
)
MAX_BODY_BYTES = 50 * 1024 * 1024  # 50 MB 支持大容量 Excel/用例数据上传
MAX_PORT_SEARCH_ATTEMPTS = 500_000
MAX_LOG_CHARS = 200_000
HISTORY_SCHEMA_VERSION = 2
DEFECT_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
BATCH_STATE_FILE = "batch-state.json"
BATCH_CASES_FILE = "batch-cases.json"
SINGLE_TEST_STATE_FILE = "single-test-state.json"
REPAIR_JOB_STATE_FILE = "repair-job-state.json"
PROMOTION_STATE_FILE = "promotion-state.json"
BLE_DEVICE_STORE_FILE = "ble-devices.json"
DEFAULT_TEST_PROJECT = "620C_W6830"
CASE_TEST_TIMEOUT_SECONDS = 1800.0
REPAIR_JOB_TIMEOUT_SECONDS = 3600.0


def _reload_runtime_limits() -> None:
    """Refresh limits after the portable app's .env has been loaded."""

    global CASE_TEST_TIMEOUT_SECONDS, REPAIR_JOB_TIMEOUT_SECONDS
    CASE_TEST_TIMEOUT_SECONDS = _positive_timeout("AGENT_LOOP_CASE_TIMEOUT", 1800.0)
    REPAIR_JOB_TIMEOUT_SECONDS = _positive_timeout("AGENT_LOOP_REPAIR_TIMEOUT", 3600.0)


_reload_runtime_limits()


TEST_PROJECTS: dict[str, dict[str, str]] = {
    "620C_W6830": {
        "project": "620C_W6830",
        "project_label": "620C W6830",
        "execution_target": "simulator",
        "execution_target_label": "模拟器",
        "case_map_dir": "620C_simulator_case_map",
        "case_map_profile": "620C_W6830",
    },
    "6202_W5230": {
        "project": "6202_W5230",
        "project_label": "6202 W5230",
        "execution_target": "hardware",
        "execution_target_label": "真机",
        "case_map_dir": "6202_case_map",
        "case_map_profile": "6202_W5230",
    },
    "6202_W5230_SIMULATOR": {
        "project": "6202_W5230_SIMULATOR",
        "project_label": "6202 W5230",
        "execution_target": "simulator",
        "execution_target_label": "模拟器",
        "case_map_dir": "6202_simulator_case_map",
        "case_map_profile": "6202_W5230_SIMULATOR",
        "simulator_source_root": "../workspaces/firmware/6202_W5230",
        "simulator_project": "6202_W5230",
        "simulator_build_directory": "../workspaces/firmware/6202_W5230/core/gui/simulator/out/build/6202_W5230",
        "simulator_artifact_path": "../workspaces/firmware/6202_W5230/core/gui/simulator/bin/main.exe",
    },
}


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _is_loopback_host(host: str | None) -> bool:
    value = str(host or "").strip().strip("[]").casefold()
    if value == "localhost":
        return True
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return False


def _ones_ssl_context() -> ssl.SSLContext:
    ca_bundle = os.environ.get("ONES_CA_BUNDLE", "").strip() or None
    return ssl.create_default_context(cafile=ca_bundle)


def _read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    os.replace(temporary, path)


@contextmanager
def _staged_directory(final_path: Path):
    """Build one artifact set off to the side, then publish it as one directory."""

    final_path.parent.mkdir(parents=True, exist_ok=True)
    staging = final_path.with_name(
        f".{final_path.name}.staging-{uuid.uuid4().hex[:12]}"
    )
    staging.mkdir(parents=False, exist_ok=False)
    try:
        yield staging
        os.replace(staging, final_path)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def _test_commands(result: dict[str, Any]) -> list[str]:
    """按实际执行顺序提取测试命令，兼容新旧结果结构。"""
    for field in ("test_commands", "agent_test_commands"):
        values = result.get(field)
        if isinstance(values, list):
            commands = [str(value).strip() for value in values if str(value).strip()]
            if commands:
                return list(dict.fromkeys(commands))

    commands: list[str] = []
    test_output = result.get("test_output") or result.get("test_result")
    if isinstance(test_output, dict):
        for item in test_output.get("results", []):
            if not isinstance(item, dict):
                continue
            values = item.get("test_commands", [])
            if isinstance(values, list):
                commands.extend(str(value).strip() for value in values if str(value).strip())
    rounds = result.get("history") or result.get("rounds") or []
    for round_result in rounds:
        if not isinstance(round_result, dict):
            continue
        values = round_result.get("test_commands", [])
        if isinstance(values, list):
            commands.extend(str(value).strip() for value in values if str(value).strip())
    patch = result.get("patch")
    if not commands and isinstance(patch, dict):
        values = patch.get("test_commands", [])
        if isinstance(values, list):
            commands.extend(str(value).strip() for value in values if str(value).strip())
    baseline_output = result.get("baseline_output")
    if not commands and isinstance(baseline_output, dict):
        values = baseline_output.get("test_commands", [])
        if isinstance(values, list):
            commands.extend(str(value).strip() for value in values if str(value).strip())
    return list(dict.fromkeys(commands))


def _verdict_reasons(result: dict[str, Any]) -> list[str]:
    """提取最终判定理由，供所有历史结果使用同一展示契约。"""
    values = result.get("verdict_reasons")
    if isinstance(values, list):
        reasons = [str(value).strip() for value in values if str(value).strip()]
        if reasons:
            return list(dict.fromkeys(reasons))

    reasons: list[str] = []

    def add_test_output(output: Any) -> None:
        if not isinstance(output, dict):
            return
        for item in output.get("results", []):
            if not isinstance(item, dict):
                continue
            reason = str(item.get("reason") or "").strip()
            if reason:
                reasons.append(reason)

    add_test_output(result.get("test_output") or result.get("test_result"))
    rounds = result.get("history") or result.get("rounds") or []
    for round_result in rounds:
        if isinstance(round_result, dict):
            add_test_output(round_result.get("test_output"))
    if reasons:
        return list(dict.fromkeys(reasons))

    reproduction_reason = str(result.get("reproduction_reason") or "").strip()
    if reproduction_reason:
        return [reproduction_reason]
    baseline_output = result.get("baseline_output")
    if isinstance(baseline_output, dict):
        baseline_reason = str(baseline_output.get("reason") or "").strip()
        if baseline_reason:
            return [baseline_reason]
    error = str(result.get("error") or "").strip()
    return [error] if error else []


def _llm_thinking_steps(result: dict[str, Any]) -> list[str]:
    """提取复现 Agent 每轮显式输出的决策理由。"""
    values = result.get("llm_thinking_steps")
    if isinstance(values, list):
        thinking_steps = [str(value).strip() for value in values if str(value).strip()]
        if thinking_steps:
            return thinking_steps

    trace = result.get("reproduction_trace")
    if not isinstance(trace, dict):
        return []

    thinking_steps: list[str] = []
    for observation in trace.get("steps", []):
        if not isinstance(observation, dict):
            continue
        decision = observation.get("decision")
        if not isinstance(decision, dict):
            continue
        reason = str(decision.get("reason") or "").strip()
        if reason:
            thinking_steps.append(reason)
    return thinking_steps


def _latest_result_value(result: dict[str, Any], field: str) -> Any:
    value = result.get(field)
    if value is not None:
        return value
    for round_result in reversed(result.get("history", [])):
        if isinstance(round_result, dict) and round_result.get(field) is not None:
            return round_result[field]
    return None


def _actual_patch(result: dict[str, Any]) -> dict[str, Any]:
    patch = _latest_result_value(result, "patch")
    return patch if isinstance(patch, dict) else {}


def _safe_segment(value: str, label: str) -> str:
    if not value or value in {".", ".."} or not SAFE_SEGMENT.fullmatch(value):
        raise ValueError(f"{label} 格式不合法")
    return value


def _test_project(value: str | None = None) -> dict[str, str]:
    """返回前端允许选择的项目；项目同时决定映射目录与执行目标。"""

    project = str(value or DEFAULT_TEST_PROJECT).strip()
    try:
        metadata = dict(TEST_PROJECTS[project])
    except KeyError as exc:
        raise ValueError(f"测试项目不存在: {project or '空'}") from exc
    if project == "6202_W5230_SIMULATOR":
        _load_test_runtime_environment()
        for field, environment_key in (
            ("simulator_source_root", "W30_6202_SIMULATOR_SOURCE_ROOT"),
            ("simulator_build_directory", "W30_6202_SIMULATOR_BUILD_DIRECTORY"),
            ("simulator_artifact_path", "W30_6202_SIMULATOR_ARTIFACT_PATH"),
        ):
            metadata[field] = str(resolve_config_path(
                os.environ.get(environment_key, metadata[field])
            ))
    return metadata


def _test_project_options() -> list[dict[str, str]]:
    return [_test_project(project) for project in TEST_PROJECTS]


def _load_test_runtime_environment() -> None:
    """Use the same repository ``.env`` loader as the per-case test CLI."""

    from agent_loop_system.main import _load_env

    _load_env()


def _test_process_environment(project_meta: dict[str, str]) -> dict[str, str]:
    """Build one child-process environment for both single and batch cases."""

    _load_test_runtime_environment()
    execution_env = dict(os.environ)
    execution_env["W30_PROJECT"] = str(project_meta["project"])
    if project_meta.get("execution_target") == "hardware":
        hardware_project = str(project_meta["project"])
        execution_env.update({
            "W30_PROJECT": hardware_project,
            "W30_HARDWARE_PROJECT": hardware_project,
        })
        for source_key in (
            "W30_SOURCE_ROOT",
            "W30_AGENT_WORKSPACE_ROOT",
            "W30_HARDWARE_SOURCE_ROOT",
            "W30_HARDWARE_WORKSPACE_ROOT",
        ):
            execution_env.pop(source_key, None)
    elif project_meta.get("simulator_source_root"):
        source_root = project_meta["simulator_source_root"]
        execution_env.update({
            "W30_SOURCE_ROOT": source_root,
            "W30_AGENT_WORKSPACE_ROOT": source_root,
            "W30_PROJECT": project_meta["simulator_project"],
            "SIMULATOR_BUILD_DIRECTORY": project_meta["simulator_build_directory"],
            "SIMULATOR_ARTIFACT_PATH": project_meta["simulator_artifact_path"],
            "SIMULATOR_SHELL_READY_MARKER": "W30_SIM_SHELL_READY",
            "SIMULATOR_GUI_COMMAND_READY_MARKER": "W30_QUICK_CMD_GUI_READY",
        })
        execution_env.pop("W30_HARDWARE_PROJECT", None)
    else:
        execution_env.pop("W30_HARDWARE_PROJECT", None)
    return execution_env


@dataclass(frozen=True)
class AppPaths:
    root: Path
    frontend: Path
    defects: Path
    defect_images: Path
    history: Path
    test_history: Path
    evidence: Path
    case_map: Path
    runtime_jobs: Path

    @classmethod
    def from_root(cls, root: Path) -> "AppPaths":
        runtime_paths = RuntimePaths.from_root(root)
        return cls(
            root=runtime_paths.root,
            frontend=runtime_paths.frontend,
            defects=runtime_paths.defects,
            defect_images=runtime_paths.defect_images,
            history=runtime_paths.history,
            test_history=runtime_paths.test_history,
            evidence=runtime_paths.evidence,
            case_map=runtime_paths.case_map,
            runtime_jobs=runtime_paths.runtime_jobs,
        )


def _case_map_path(
    paths: AppPaths,
    sheet: str,
    project: str = DEFAULT_TEST_PROJECT,
) -> Path:
    """只读取项目专用 case_map，绝不跨项目静默回退。"""

    project_meta = _test_project(project)
    sheet = str(sheet or "").strip()
    if not sheet or Path(sheet).name != sheet or "/" in sheet or "\\" in sheet:
        raise ValueError("测试模块格式不合法")
    case_map_root = (paths.case_map / project_meta["case_map_dir"]).resolve()
    path = (case_map_root / f"{sheet}.json").resolve()
    if path.parent != case_map_root or not path.is_file():
        raise ValueError("测试模块不存在")
    return path


def _case_entries(
    paths: AppPaths,
    sheet: str,
    project: str = DEFAULT_TEST_PROJECT,
) -> list[dict[str, Any]]:
    path = _case_map_path(paths, sheet, project)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"case_map JSON 无法读取: {path}") from exc
    return validated_case_entries(
        raw,
        sheet_name=sheet,
        expected_profile=_test_project(project)["case_map_profile"],
        path=path,
    )


def _external_explored_ids(
    paths: AppPaths,
    project: str = DEFAULT_TEST_PROJECT,
) -> set[str]:
    """外部探索事实只来自目标目录自己的 JSONL 账本。"""

    project_meta = _test_project(project)
    ledger = paths.case_map / project_meta["case_map_dir"] / "external_execution_history.jsonl"
    records = read_external_execution_history(
        ledger,
        expected_target=project_meta["case_map_profile"],
    )
    return {record.case_id for record in records}


class HistoryStore:
    def __init__(self, paths: AppPaths):
        self.paths = paths

    def _run_dir(self, defect: str, run_id: str) -> Path:
        defect = _safe_segment(defect, "缺陷编号")
        run_id = _safe_segment(run_id, "历史记录编号")
        return self.paths.history / defect / run_id

    def create(
        self,
        *,
        defect: str,
        job: dict[str, Any],
        result: dict[str, Any],
        stdout: str,
        stderr: str,
    ) -> str:
        defect = _safe_segment(defect, "缺陷编号")
        run_id = datetime.now().astimezone().strftime("%Y%m%dT%H%M%S%f")
        run_dir = self._run_dir(defect, run_id)

        progress = job.get("progress") or {}
        patch = _actual_patch(result)
        test_output = _latest_result_value(result, "test_output")
        run_payload = {
            "schema_version": HISTORY_SCHEMA_VERSION,
            "id": run_id,
            "defect": defect,
            "timestamp": job.get("finished_at") or _now(),
            "started_at": job.get("started_at"),
            "finished_at": job.get("finished_at"),
            "verdict": result.get("verdict", "CANNOT_VERIFY"),
            "reproduction_outcome": result.get("reproduction_outcome"),
            "attempts": result.get("attempts", 0),
            "execution_mode": job.get("execution_mode", "agent_generated"),
            "project": job.get("project"),
            "project_label": job.get("project_label"),
            "execution_target": job.get("execution_target"),
            "execution_target_label": job.get("execution_target_label"),
            "profile": job.get("case_map_profile") or result.get("profile"),
            "test_case": job.get("test_case"),
            "source_file": patch.get("file_path") or job.get("source_file"),
            "test_commands": _test_commands(result),
            "verdict_reasons": _verdict_reasons(result),
            "llm_thinking_steps": _llm_thinking_steps(result),
            "patch_retained": result.get("patch_retained"),
            "error": result.get("error") or job.get("error"),
            "error_code": result.get("error_code"),
            "rollback_error": result.get("rollback_error"),
            "restore_build_error": result.get("restore_build_error"),
            "nodes": progress.get("nodes", job.get("nodes", {})),
            "baseline_output": _latest_result_value(result, "baseline_output"),
            "reproduction_trace": result.get("reproduction_trace"),
            "build_result": result.get("build_result"),
            "restore_build_result": result.get("restore_build_result"),
            "rounds": result.get("history", []),
            "stdout": stdout[-MAX_LOG_CHARS:],
            "stderr": stderr[-MAX_LOG_CHARS:],
        }
        run_payload.update(outcome_fields({**result, **{
            key: job.get(key)
            for key in (
                "workflow_status", "execution_status", "evidence_status",
                "mapping_status", "reason_code",
            )
            if job.get(key) is not None
        }}))
        with _staged_directory(run_dir) as staging:
            _write_json(staging / "run.json", run_payload)
            _write_json(staging / "patch.json", patch)
            _write_json(staging / "test_result.json", test_output or {})

            evidence_dir = self.paths.evidence / defect
            for kind in ("before", "after"):
                source = evidence_dir / f"{kind}.bmp"
                if source.is_file():
                    shutil.copy2(source, staging / f"{kind}.bmp")
        return run_id

    def list(self, defect: str) -> list[dict[str, Any]]:
        defect = _safe_segment(defect, "缺陷编号")
        defect_dir = self.paths.history / defect
        if not defect_dir.is_dir():
            return []
        records: list[dict[str, Any]] = []
        for run_dir in sorted(defect_dir.iterdir(), key=lambda item: item.name, reverse=True):
            if not run_dir.is_dir() or not SAFE_SEGMENT.fullmatch(run_dir.name):
                continue
            run = _read_json(run_dir / "run.json", {})
            if not isinstance(run, dict):
                continue
            summary = {
                key: value
                for key, value in run.items()
                if key not in {
                    "stdout", "stderr", "baseline_output", "build_result",
                    "restore_build_result", "reproduction_trace", "rounds",
                }
            }
            summary["legacy_record"] = run.get("schema_version") != HISTORY_SCHEMA_VERSION
            summary["has_before"] = (run_dir / "before.bmp").is_file()
            summary["has_after"] = (run_dir / "after.bmp").is_file()
            records.append(summary)
        return records

    def get(self, defect: str, run_id: str) -> dict[str, Any] | None:
        run_dir = self._run_dir(defect, run_id)
        run = _read_json(run_dir / "run.json")
        if not isinstance(run, dict):
            return None
        run["patch"] = _read_json(run_dir / "patch.json", {})
        run["test_result"] = _read_json(run_dir / "test_result.json", {})
        run["legacy_record"] = run.get("schema_version") != HISTORY_SCHEMA_VERSION
        normalized = dict(run)
        normalized["test_output"] = run["test_result"]
        normalized["history"] = run.get("rounds", [])
        run["test_commands"] = _test_commands(normalized)
        run["verdict_reasons"] = _verdict_reasons(normalized)
        run["llm_thinking_steps"] = _llm_thinking_steps(normalized)
        run["evidence"] = {
            kind: f"/api/history/{defect}/{run_id}/evidence/{kind}"
            for kind in ("before", "after")
            if (run_dir / f"{kind}.bmp").is_file()
        }
        return run

    def delete(self, defect: str, run_id: str) -> bool:
        run_dir = self._run_dir(defect, run_id)
        if not run_dir.is_dir():
            return False
        resolved = run_dir.resolve()
        resolved.relative_to(self.paths.history.resolve())
        shutil.rmtree(resolved)
        defect_dir = resolved.parent
        if defect_dir.is_dir() and not any(defect_dir.iterdir()):
            defect_dir.rmdir()
        return True


class DefectRepository:
    def __init__(self, paths: AppPaths, history: HistoryStore):
        self.paths = paths
        self.history = history

    def get(self, number: str) -> dict[str, Any] | None:
        number = _safe_segment(number, "缺陷编号")
        data = _read_json(self.paths.defects / number / "defect.json")
        if not isinstance(data, dict):
            return None
        result = dict(data)
        result["attachments"] = self._attachments(number, data)
        result["source_files"] = self._source_files(data)
        result["case_sheets"] = self._case_sheets(data)
        result["history"] = self.history.list(number)
        return result

    def defect_image_path(self, number: str, name: str) -> Path:
        number = _safe_segment(number, "缺陷编号")
        name = str(name or "").strip()
        if not name or Path(name).name != name or "/" in name or "\\" in name:
            raise ValueError("缺陷图片名称格式不合法")
        path = (self.paths.defect_images / number / name).resolve()
        path.relative_to((self.paths.defect_images / number).resolve())
        if path.suffix.lower() not in DEFECT_IMAGE_SUFFIXES:
            raise ValueError("缺陷图片格式不支持")
        return path

    def _attachments(self, number: str, defect: dict[str, Any]) -> list[dict[str, Any]]:
        attachments: list[dict[str, Any]] = []
        for raw in defect.get("attachments", []):
            if not isinstance(raw, dict):
                continue
            item = dict(raw)
            if item.get("kind") == "image" and item.get("name"):
                try:
                    image_path = self.defect_image_path(number, str(item["name"]))
                except ValueError:
                    image_path = None
                if image_path is not None and image_path.is_file():
                    item["url"] = (
                        f"/api/defects/{quote(number, safe='')}/images/"
                        f"{quote(str(item['name']), safe='')}"
                    )
            attachments.append(item)
        return attachments

    def list(
        self,
        *,
        query: str = "",
        page: int = 1,
        page_size: int = 20,
        result_filter: str = "all",
    ) -> dict[str, Any]:
        result_filter = str(result_filter or "all").strip().lower()
        if result_filter not in {"all", "pass", "fail", "cannot_verify", "pending"}:
            raise ValueError("result 参数不合法")
        if not self.paths.defects.is_dir():
            return {
                "items": [], "page": 1, "page_size": page_size, "total": 0,
                "total_pages": 0,
                "summary": {"all": 0, "passed": 0, "failed": 0, "cannot_verify": 0, "pending": 0},
                "result_filter": result_filter,
            }
        rows: list[dict[str, Any]] = []
        for defect_dir in self.paths.defects.iterdir():
            if not defect_dir.is_dir() or not SAFE_SEGMENT.fullmatch(defect_dir.name):
                continue
            data = _read_json(defect_dir / "defect.json")
            if not isinstance(data, dict):
                continue
            history = self.history.list(defect_dir.name)
            latest = history[0] if history else None
            rows.append(
                {
                    "number": str(data.get("number") or defect_dir.name),
                    "title": data.get("title", "未命名缺陷"),
                    "description": data.get("description", ""),
                    "status": data.get("status", "未知"),
                    "repair_result": latest.get("verdict", "未修复") if latest else "未修复",
                    "last_run_at": latest.get("timestamp") if latest else None,
                    "history_count": len(history),
                }
            )
        rows = sorted(rows, key=lambda row: (not row["number"].isdigit(), -(int(row["number"]) if row["number"].isdigit() else 0), row["number"]))
        summary = {
            "all": len(rows),
            "passed": sum(str(row["repair_result"]).upper() == "PASS" for row in rows),
            "failed": sum(str(row["repair_result"]).upper() == "FAIL" for row in rows),
            "cannot_verify": sum(str(row["repair_result"]).upper() == "CANNOT_VERIFY" for row in rows),
            "pending": sum(str(row["repair_result"]).upper() not in {"PASS", "FAIL", "CANNOT_VERIFY"} for row in rows),
        }
        keywords = [word.casefold() for word in query.strip().split() if word]
        if keywords:
            rows = [
                row for row in rows
                if all(
                    word in " ".join(
                        str(row.get(field, ""))
                        for field in ("number", "title", "description", "status")
                    ).casefold()
                    for word in keywords
                )
            ]
        if result_filter != "all":
            def matches_result(row: dict[str, Any]) -> bool:
                verdict = str(row["repair_result"]).upper()
                if result_filter == "pending":
                    return verdict not in {"PASS", "FAIL", "CANNOT_VERIFY"}
                return verdict == result_filter.upper()

            rows = [row for row in rows if matches_result(row)]
        total = len(rows)
        total_pages = (total + page_size - 1) // page_size
        actual_page = min(page, total_pages) if total_pages else 1
        offset = (actual_page - 1) * page_size
        return {
            "items": rows[offset:offset + page_size],
            "page": actual_page,
            "page_size": page_size,
            "total": total,
            "total_pages": total_pages,
            "summary": summary,
            "result_filter": result_filter,
        }

    def cases(self, sheet: str) -> list[dict[str, Any]]:
        return [
            {
                "case_id": str(item.get("case_id", "")),
                "sheet": str(item.get("sheet") or sheet),
                "priority": item.get("priority", ""),
                "expected_text": item.get("expected_text", ""),
                "unable": bool(item.get("unable", False)),
                "note": item.get("note", ""),
            }
            for item in _case_entries(self.paths, sheet)
            if isinstance(item, dict) and item.get("case_id")
        ]

    def _case_sheets(self, defect: dict[str, Any]) -> list[dict[str, Any]]:
        case_map_root = self.paths.case_map / _test_project()["case_map_dir"]
        files = sorted(case_map_root.glob("*.json"), key=lambda path: path.stem)
        haystack = " ".join(
            [str(defect.get("title", "")), str(defect.get("description", ""))]
            + [str(match.get("path", "")) for match in defect.get("source_analysis", {}).get("matches", [])]
        ).lower()
        result = []
        for path in files:
            raw = _read_json(path, [])
            entries = raw.get("cases", []) if isinstance(raw, dict) else raw
            count = len(entries) if isinstance(entries, list) else 0
            result.append({"name": path.stem, "count": count, "recommended": path.stem.lower() in haystack})
        return sorted(result, key=lambda item: (not item["recommended"], item["name"]))

    @staticmethod
    def _source_files(defect: dict[str, Any]) -> list[dict[str, str]]:
        paths: dict[str, dict[str, Any]] = {}
        for match in defect.get("source_analysis", {}).get("matches", []):
            path = str(match.get("path", "")).strip()
            if not path:
                continue
            if path not in paths:
                paths[path] = {"path": path, "line": match.get("line_start", "?"), "matches": 0}
            paths[path]["matches"] += 1
        ranked = sorted(paths.values(), key=lambda item: (-item["matches"], item["path"]))
        return [
            {
                "path": item["path"],
                "label": f"{item['path']} · L{item['line']} · {item['matches']} 处命中",
            }
            for item in ranked
        ]


class TestHistoryStore:
    """Agent 测试运行记录；沿用本地 JSON，不引入数据库。"""

    def __init__(self, paths: AppPaths):
        self.paths = paths
        self._summary_index_cache: dict[
            str, dict[tuple[str, str], dict[str, Any]]
        ] = {}
        self._summary_index_lock = threading.RLock()

    def _project_root(self, project: str = DEFAULT_TEST_PROJECT) -> Path:
        project_meta = _test_project(project)
        # 620C 沿用原目录，保留既有历史；6202 单独分区，避免相同 case_id 串记录。
        if project_meta["project"] == DEFAULT_TEST_PROJECT:
            return self.paths.test_history
        return self.paths.test_history / project_meta["project"]

    def _case_dir(
        self,
        sheet: str,
        case_id: str,
        *,
        project: str = DEFAULT_TEST_PROJECT,
    ) -> Path:
        _case_map_path(self.paths, sheet, project)
        case_id = _safe_segment(case_id, "测试用例编号")
        project_root = self._project_root(project).resolve()
        path = (project_root / sheet / case_id).resolve()
        path.relative_to(project_root)
        return path

    def _run_dir(
        self,
        sheet: str,
        case_id: str,
        run_id: str,
        *,
        project: str = DEFAULT_TEST_PROJECT,
    ) -> Path:
        run_id = _safe_segment(run_id, "测试记录编号")
        return self._case_dir(sheet, case_id, project=project) / run_id

    def create(
        self,
        *,
        job: dict[str, Any],
        result: dict[str, Any],
        stdout: str,
        stderr: str,
        screenshot: Path | None = None,
    ) -> str:
        # 和索引构建串行，避免页面刷新恰好撞上记录落盘时缓存出半份数据。
        with self._summary_index_lock:
            return self._create(
                job=job,
                result=result,
                stdout=stdout,
                stderr=stderr,
                screenshot=screenshot,
            )

    def _create(
        self,
        *,
        job: dict[str, Any],
        result: dict[str, Any],
        stdout: str,
        stderr: str,
        screenshot: Path | None = None,
    ) -> str:
        sheet = str(job["sheet"])
        case_id = str(job["case_id"])
        project_meta = _test_project(str(job.get("project") or DEFAULT_TEST_PROJECT))
        project = project_meta["project"]
        run_id = datetime.now().astimezone().strftime("%Y%m%dT%H%M%S%f")
        run_dir = self._run_dir(sheet, case_id, run_id, project=project)
        case = job.get("case") if isinstance(job.get("case"), dict) else {}
        payload = {
            "schema_version": 2,
            "id": run_id,
            "sheet": sheet,
            "case_id": case_id,
            **{key: project_meta[key] for key in (
                "project", "project_label", "execution_target", "execution_target_label"
            )},
            "timestamp": job.get("finished_at") or _now(),
            "started_at": job.get("started_at"),
            "finished_at": job.get("finished_at"),
            "verdict": result.get("verdict", "CANNOT_VERIFY"),
            "reason": result.get("reason") or job.get("error") or "",
            "execution_mode": result.get("execution_mode", "fixed_mapping"),
            "result_schema_version": result.get("schema_version"),
            "provenance": result.get("provenance", {}),
            "execution_status": result.get("execution_status"),
            "workflow_status": result.get("workflow_status") or job.get("workflow_status"),
            "evidence_status": result.get("evidence_status"),
            "mapping_status": result.get("mapping_status"),
            "reason_code": result.get("reason_code"),
            "priority": case.get("priority", ""),
            "precondition_text": case.get("precondition_text", ""),
            "steps_text": case.get("steps_text", ""),
            "expected_text": case.get("expected_text", ""),
            "verification_points": result.get(
                "verification_points", case.get("verification_points", [])
            ),
            "setup": case.get("setup", []),
            "actions": case.get("actions", []),
            "collect": case.get("collect", []),
            "planned_commands": result.get("planned_commands", {
                "setup": case.get("setup", []),
                "action": case.get("actions", []),
                "collect": case.get("collect", []),
            }),
            "command_trace": result.get("command_trace", []),
            "evidence_contract": result.get("evidence_contract", {}),
            "skipped": bool(result.get("skipped", False)),
            "aborted": bool(result.get("aborted", False)),
            "setup_errors": result.get("setup_errors", []),
            "action_errors": result.get("action_errors", []),
            "collect_errors": result.get("collect_errors", []),
            "terminal_json": result.get("terminal_json", []),
            "exploration_trace": result.get("exploration_trace"),
            "return_code": job.get("return_code"),
            "stdout": stdout[-MAX_LOG_CHARS:],
            "stderr": stderr[-MAX_LOG_CHARS:],
        }
        payload.update(outcome_fields(
            payload,
            workflow_default=str(job.get("workflow_status") or "completed"),
            mapping_default=str(case.get("mapping_status") or "NOT_RECORDED"),
        ))
        if job.get("batch_id"):
            payload["batch_id"] = str(job["batch_id"])
        if job.get("batch_token"):
            payload["batch_token"] = str(job["batch_token"])
        archived_screenshots: list[dict[str, Any]] = []
        with _staged_directory(run_dir) as staging:
            raw_screenshots = result.get("screenshots", [])
            if isinstance(raw_screenshots, list):
                for item in raw_screenshots:
                    if not isinstance(item, dict):
                        continue
                    source = Path(str(item.get("path") or ""))
                    if not source.is_file():
                        continue
                    file_name = f"screenshot-{len(archived_screenshots) + 1:02d}.bmp"
                    shutil.copy2(source, staging / file_name)
                    archived_screenshots.append({
                        "index": len(archived_screenshots) + 1,
                        "label": str(item.get("label") or f"检查点 {len(archived_screenshots) + 1}"),
                        "phase": str(item.get("phase") or ""),
                        "command": str(item.get("command") or ""),
                        "captured_at": str(item.get("captured_at") or ""),
                        "trace_index": item.get("trace_index"),
                        "file": file_name,
                    })
            if not archived_screenshots and screenshot and screenshot.is_file():
                shutil.copy2(screenshot, staging / "screenshot.bmp")
                archived_screenshots.append({
                    "index": 1,
                    "label": "最终画面",
                    "phase": "final",
                    "command": "",
                    "file": "screenshot.bmp",
                })
            payload["screenshots"] = archived_screenshots
            _write_json(staging / "run.json", payload)
        project_cache = self._summary_index_cache.get(project)
        if project_cache is not None:
            key = (sheet, case_id)
            previous = project_cache.get(key)
            project_cache[key] = {
                "latest": self._compact_summary(payload),
                "history_count": int((previous or {}).get("history_count") or 0) + 1,
            }
        return run_id

    def batch_records(self, batch_id: str) -> dict[str, dict[str, Any]]:
        """读取同一批次已归档的结果，用于进程崩溃后的幂等恢复。"""
        batch_id = _safe_segment(batch_id, "批次编号")
        return self.batch_records_many({batch_id})[batch_id]

    def batch_records_many(
        self,
        batch_ids: set[str],
    ) -> dict[str, dict[str, dict[str, Any]]]:
        """一次扫描测试历史，按批次返回崩溃恢复记录。"""
        normalized_ids = {
            _safe_segment(batch_id, "批次编号")
            for batch_id in batch_ids
        }
        records_by_batch: dict[str, dict[str, dict[str, Any]]] = {
            batch_id: {} for batch_id in normalized_ids
        }
        if not normalized_ids or not self.paths.test_history.is_dir():
            return records_by_batch
        summary_indexes: dict[
            str, dict[tuple[str, str], dict[str, Any]]
        ] = {project: {} for project in TEST_PROJECTS}
        checked_sheets: dict[tuple[str, str], bool] = {}
        with self._summary_index_lock:
            for path in self.paths.test_history.rglob("run.json"):
                run = _read_json(path)
                if isinstance(run, dict):
                    records = records_by_batch.get(str(run.get("batch_id") or ""))
                    if records is not None:
                        token = str(run.get("batch_token") or "")
                        if token and SAFE_SEGMENT.fullmatch(token):
                            records[token] = run

                relative_parts = path.relative_to(self.paths.test_history).parts
                if (
                    len(relative_parts) == 5
                    and relative_parts[0] in TEST_PROJECTS
                    and relative_parts[0] != DEFAULT_TEST_PROJECT
                ):
                    project, sheet, case_id, run_id, _ = relative_parts
                elif len(relative_parts) == 4:
                    project = DEFAULT_TEST_PROJECT
                    sheet, case_id, run_id, _ = relative_parts
                else:
                    continue
                if not SAFE_SEGMENT.fullmatch(case_id) or not SAFE_SEGMENT.fullmatch(run_id):
                    continue
                sheet_key = (project, sheet)
                if sheet_key not in checked_sheets:
                    try:
                        _case_map_path(self.paths, sheet, project)
                    except ValueError:
                        checked_sheets[sheet_key] = False
                    else:
                        checked_sheets[sheet_key] = True
                if not checked_sheets[sheet_key]:
                    continue

                key = (sheet, case_id)
                summary = summary_indexes[project].setdefault(
                    key,
                    {"latest": None, "history_count": 0, "latest_run_id": ""},
                )
                summary["history_count"] += 1

                if not isinstance(run, dict):
                    continue
                if run_id > summary["latest_run_id"]:
                    summary["latest"] = self._compact_summary(run)
                    summary["latest_run_id"] = run_id

            self._summary_index_cache = {
                project: {
                    key: {
                        "latest": summary["latest"],
                        "history_count": summary["history_count"],
                    }
                    for key, summary in index.items()
                    if summary["latest"] is not None
                }
                for project, index in summary_indexes.items()
            }
        return records_by_batch

    def list(
        self,
        sheet: str,
        case_id: str,
        *,
        project: str = DEFAULT_TEST_PROJECT,
    ) -> list[dict[str, Any]]:
        project_meta = _test_project(project)
        case_dir = self._case_dir(sheet, case_id, project=project_meta["project"])
        if not case_dir.is_dir():
            return []
        records: list[dict[str, Any]] = []
        for run_dir in sorted(case_dir.iterdir(), key=lambda item: item.name, reverse=True):
            if not run_dir.is_dir() or not SAFE_SEGMENT.fullmatch(run_dir.name):
                continue
            run = _read_json(run_dir / "run.json")
            if not isinstance(run, dict):
                continue
            summary = {
                key: value
                for key, value in run.items()
                if key not in {
                    "precondition_text", "steps_text", "expected_text", "verification_points",
                    "setup", "actions", "collect", "terminal_json", "screenshots", "stdout", "stderr",
                    "planned_commands", "command_trace", "evidence_contract", "exploration_trace",
                    "setup_errors", "action_errors", "collect_errors",
                }
            }
            screenshot_count = sum(1 for path in run_dir.glob("screenshot*.bmp") if path.is_file())
            summary["screenshot_count"] = screenshot_count
            summary["has_screenshot"] = screenshot_count > 0
            for key in (
                "project", "project_label", "execution_target", "execution_target_label"
            ):
                summary.setdefault(key, project_meta[key])
            records.append(summary)
        return records

    def latest(
        self,
        sheet: str,
        case_id: str,
        *,
        project: str = DEFAULT_TEST_PROJECT,
    ) -> dict[str, Any] | None:
        records = self.list(sheet, case_id, project=project)
        return records[0] if records else None

    def index(
        self,
        *,
        project: str = DEFAULT_TEST_PROJECT,
    ) -> dict[tuple[str, str], list[dict[str, Any]]]:
        """只扫描实际存在的测试历史，避免为全部 case 逐个访问空目录。"""
        result: dict[tuple[str, str], list[dict[str, Any]]] = {}
        project_root = self._project_root(project)
        if not project_root.is_dir():
            return result
        for sheet_dir in project_root.iterdir():
            if not sheet_dir.is_dir():
                continue
            try:
                _case_map_path(self.paths, sheet_dir.name, project)
            except ValueError:
                continue
            for case_dir in sheet_dir.iterdir():
                if not case_dir.is_dir() or not SAFE_SEGMENT.fullmatch(case_dir.name):
                    continue
                records = self.list(sheet_dir.name, case_dir.name, project=project)
                if records:
                    result[(sheet_dir.name, case_dir.name)] = records
        return result

    @staticmethod
    def _compact_summary(run: dict[str, Any]) -> dict[str, Any]:
        """列表页只需要这些字段，不能为它读取每次运行的完整证据。"""
        return {
            "id": run.get("id"),
            "verdict": run.get("verdict"),
            "workflow_status": run.get("workflow_status"),
            "execution_status": run.get("execution_status"),
            "evidence_status": run.get("evidence_status"),
            "mapping_status": run.get("mapping_status"),
            "reason_code": run.get("reason_code"),
            "timestamp": run.get("timestamp"),
            "project": run.get("project"),
            "execution_target": run.get("execution_target"),
        }

    def _case_summary(
        self,
        sheet: str,
        case_id: str,
        *,
        project: str = DEFAULT_TEST_PROJECT,
    ) -> dict[str, Any] | None:
        return self._case_summary_from_dir(
            self._case_dir(sheet, case_id, project=project)
        )

    def _case_summary_from_dir(self, case_dir: Path) -> dict[str, Any] | None:
        if not case_dir.is_dir():
            return None
        run_dirs = sorted(
            (
                run_dir for run_dir in case_dir.iterdir()
                if run_dir.is_dir()
                and SAFE_SEGMENT.fullmatch(run_dir.name)
                and (run_dir / "run.json").is_file()
            ),
            key=lambda item: item.name,
            reverse=True,
        )
        for run_dir in run_dirs:
            run = _read_json(run_dir / "run.json")
            if isinstance(run, dict):
                return {
                    "latest": self._compact_summary(run),
                    "history_count": len(run_dirs),
                }
        return None

    def summary_index(
        self,
        *,
        project: str = DEFAULT_TEST_PROJECT,
    ) -> dict[tuple[str, str], dict[str, Any]]:
        """缓存列表页历史摘要；同一服务进程内刷新不再重扫全部 run.json。"""
        project = _test_project(project)["project"]
        with self._summary_index_lock:
            if project in self._summary_index_cache:
                return self._summary_index_cache[project]
            result: dict[tuple[str, str], dict[str, Any]] = {}
            project_root = self._project_root(project)
            if project_root.is_dir():
                for sheet_dir in project_root.iterdir():
                    if not sheet_dir.is_dir():
                        continue
                    try:
                        _case_map_path(self.paths, sheet_dir.name, project)
                    except ValueError:
                        continue
                    for case_dir in sheet_dir.iterdir():
                        if not case_dir.is_dir() or not SAFE_SEGMENT.fullmatch(case_dir.name):
                            continue
                        summary = self._case_summary_from_dir(case_dir)
                        if summary is not None:
                            result[(sheet_dir.name, case_dir.name)] = summary
            self._summary_index_cache[project] = result
            return result

    def get(
        self,
        sheet: str,
        case_id: str,
        run_id: str,
        *,
        project: str = DEFAULT_TEST_PROJECT,
    ) -> dict[str, Any] | None:
        project_meta = _test_project(project)
        project = project_meta["project"]
        run_dir = self._run_dir(sheet, case_id, run_id, project=project)
        run = _read_json(run_dir / "run.json")
        if not isinstance(run, dict):
            return None
        screenshot_urls: list[dict[str, Any]] = []
        raw_screenshots = run.get("screenshots", [])
        if isinstance(raw_screenshots, list):
            for item in raw_screenshots:
                if not isinstance(item, dict):
                    continue
                file_name = str(item.get("file") or "")
                if not TEST_SCREENSHOT_FILE.fullmatch(file_name) or not (run_dir / file_name).is_file():
                    continue
                screenshot_urls.append({
                    **item,
                    "url": (
                        f"/api/test-history/{quote(sheet, safe='')}/{quote(case_id, safe='')}/"
                        f"{quote(run_id, safe='')}/screenshot/{quote(file_name, safe='')}"
                        f"?project={quote(project, safe='')}"
                    ),
                })
        legacy = run_dir / "screenshot.bmp"
        if not screenshot_urls and legacy.is_file():
            screenshot_urls.append({
                "index": 1,
                "label": "最终画面",
                "phase": "final",
                "command": "",
                "file": "screenshot.bmp",
                "url": (
                    f"/api/test-history/{quote(sheet, safe='')}/{quote(case_id, safe='')}/"
                    f"{quote(run_id, safe='')}/screenshot?project={quote(project, safe='')}"
                ),
            })
        run["screenshot_urls"] = screenshot_urls
        if screenshot_urls:
            run["screenshot_url"] = screenshot_urls[-1]["url"]
        for key in (
            "project", "project_label", "execution_target", "execution_target_label"
        ):
            run.setdefault(key, project_meta[key])
        return run

    def delete(
        self,
        sheet: str,
        case_id: str,
        run_id: str,
        *,
        project: str = DEFAULT_TEST_PROJECT,
    ) -> bool:
        project = _test_project(project)["project"]
        with self._summary_index_lock:
            run_dir = self._run_dir(sheet, case_id, run_id, project=project)
            if not run_dir.is_dir():
                return False
            resolved = run_dir.resolve()
            resolved.relative_to(self.paths.test_history.resolve())
            shutil.rmtree(resolved)
            project_cache = self._summary_index_cache.get(project)
            if project_cache is not None:
                key = (sheet, case_id)
                summary = self._case_summary(sheet, case_id, project=project)
                if summary is None:
                    project_cache.pop(key, None)
                else:
                    project_cache[key] = summary
            return True


class CaseMapRepository:
    """case_map 查询视图，以及显式候选复跑所需的最小写入事务。"""

    FILTERS = {
        "all", "unexplored", "externally_explored",
        "explored_unsolidified", "solidified",
    }
    RUN_CATEGORIES = {"untested", "pass", "fail", "cannot_verify", "error"}
    BATCH_CATEGORIES = RUN_CATEGORIES - {"pass"}
    PROMOTABLE_VERDICTS = {"PASS", "FAIL"}
    CANDIDATE_FIELDS = (
        "setup", "actions", "collect", "verification_points", "note",
    )

    def __init__(self, paths: AppPaths, history: TestHistoryStore):
        self.paths = paths
        self.history = history
        self._write_lock = threading.RLock()

    @staticmethod
    def _command_name(command: str) -> str:
        return normalize_command(command)[1:].partition(":")[0]

    @staticmethod
    def _unique_issues(issues: list[str]) -> list[str]:
        return list(dict.fromkeys(issue for issue in issues if issue))

    def _load_writable_case(
        self,
        *,
        sheet: str,
        case_id: str,
        project: str,
    ) -> tuple[Path, str, Any, list[dict[str, Any]], dict[str, Any]]:
        """严格读取一次目标模块，并保证待修改 case_id 唯一。"""

        project_meta = _test_project(project)
        project = project_meta["project"]
        case_id = _safe_segment(case_id, "测试用例编号")
        path = _case_map_path(self.paths, sheet, project)
        try:
            source_text = path.read_text(encoding="utf-8")
            raw = json.loads(source_text)
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"case_map JSON 无法读取: {path}") from exc
        entries = validated_case_entries(
            raw,
            sheet_name=sheet,
            expected_profile=project_meta["case_map_profile"],
            path=path,
        )
        matches = [item for item in entries if item.get("case_id") == case_id]
        if len(matches) != 1:
            raise ValueError(
                f"case_map 中用例 {case_id} 应唯一，实际找到 {len(matches)} 条"
            )
        return path, source_text, raw, entries, matches[0]

    @staticmethod
    def _write_if_unchanged(path: Path, source_text: str, payload: Any) -> None:
        """在原子替换前拒绝覆盖同一文件的并发修改。"""

        try:
            if path.read_text(encoding="utf-8") != source_text:
                raise RuntimeError("case_map 在写入前发生并发变化")
        except OSError as exc:
            raise RuntimeError(f"case_map 写入前无法复核: {exc}") from exc
        _write_json(path, payload)

    def build_agent_candidate(
        self,
        *,
        sheet: str,
        case_id: str,
        project: str,
        source_history: dict[str, Any],
    ) -> dict[str, Any]:
        """从一轮完整自主探索中提取可正式复跑的临时候选。"""

        project_meta = _test_project(project)
        issues: list[str] = []
        if str(source_history.get("sheet") or "") != sheet:
            issues.append("自主探索记录的模块与当前用例不一致")
        if str(source_history.get("case_id") or "") != case_id:
            issues.append("自主探索记录的 case_id 与当前用例不一致")
        if str(source_history.get("project") or "") != project_meta["project"]:
            issues.append("自主探索记录的项目与当前用例不一致")
        if source_history.get("execution_mode") != "agent_exploration":
            issues.append("最新运行不是 Agent-loop 自主探索结果")

        verdict = str(source_history.get("verdict") or "ERROR").upper()
        if verdict not in self.PROMOTABLE_VERDICTS:
            issues.append(f"自主探索结果为 {verdict}，没有形成可复跑的确定结论")
        if not str(source_history.get("reason") or "").strip():
            issues.append("自主探索结果缺少判定理由")
        if source_history.get("skipped") or source_history.get("aborted"):
            issues.append("自主探索运行被跳过或中止")
        if any(
            source_history.get(field)
            for field in ("setup_errors", "action_errors", "collect_errors")
        ):
            issues.append("自主探索运行存在命令或截图错误")

        contract = source_history.get("evidence_contract")
        if not isinstance(contract, dict):
            contract = {}
        if contract.get("complete") is not True or contract.get("status") != "COMPLETE":
            issues.append("自主探索的证据合同不完整")
        if contract.get("issues") not in ([], None):
            issues.append("自主探索的证据合同仍有未解决问题")

        planned = source_history.get("planned_commands")
        raw_actions = planned.get("action") if isinstance(planned, dict) else None
        if not isinstance(raw_actions, list) or not raw_actions:
            issues.append("自主探索没有记录实际 action")
            raw_actions = []
        normalized_actions: list[str] = []
        for index, command in enumerate(raw_actions, start=1):
            try:
                normalized = normalize_command(command)
            except ValueError as exc:
                issues.append(f"自主探索 action {index} 格式无效: {exc}")
                continue
            name = self._command_name(normalized)
            if project_meta["execution_target"] == "hardware" and (
                name == "TEST_SESSION" or name.startswith("SIM_")
            ):
                issues.append(f"自主探索 action {index} 含真机禁用命令 {name}")
            normalized_actions.append(normalized)
        if not any(
            self._command_name(command) not in OBSERVATION_ONLY_COMMANDS
            for command in normalized_actions
        ):
            issues.append("自主探索没有成功执行真实业务 action")

        trace = source_history.get("command_trace")
        trace = trace if isinstance(trace, list) else []
        traced_actions: list[str] = []
        captures: list[dict[str, Any]] = []
        for item in trace:
            if not isinstance(item, dict):
                issues.append("自主探索 command_trace 含非对象条目")
                continue
            if item.get("source") == "agent" and item.get("phase") == "action":
                if item.get("ok") is not True:
                    issues.append("自主探索 action trace 存在失败动作")
                    continue
                try:
                    traced_actions.append(normalize_command(str(item.get("command") or "")))
                except ValueError as exc:
                    issues.append(f"自主探索 action trace 格式无效: {exc}")
            if (
                item.get("command_name") == "HOST_SCREENSHOT"
                and item.get("ok") is True
                and item.get("kind") in {"capture", "screenshot"}
            ):
                captures.append(item)
        if traced_actions != normalized_actions:
            issues.append("自主探索的计划 actions 与成功 action trace 不一致")

        points = source_history.get("verification_points")
        points = (
            [str(point).strip() for point in points]
            if isinstance(points, list)
            else []
        )
        if not points or any(not point for point in points):
            issues.append("自主探索没有完整的视觉检查点标签")
        capture_labels = [str(item.get("checkpoint_label") or "").strip() for item in captures]
        if capture_labels != points:
            issues.append("自主探索的截图 trace 与视觉检查点不一致")
        archived_screenshots = source_history.get("screenshot_urls")
        if not isinstance(archived_screenshots, list):
            archived_screenshots = source_history.get("screenshots")
        screenshot_count = len(archived_screenshots) if isinstance(archived_screenshots, list) else 0
        if screenshot_count != len(points):
            issues.append(
                f"自主探索需要 {len(points)} 张历史截图，实际可读取 {screenshot_count} 张"
            )
        for field, actual in (
            ("required_screenshots", len(points)),
            ("captured_screenshots", screenshot_count),
            ("planned_action_count", len(normalized_actions)),
            ("attempted_action_count", len(traced_actions)),
        ):
            if contract.get(field) != actual:
                issues.append(f"自主探索证据计数 {field} 与实际记录不一致")

        issues = self._unique_issues(issues)
        if issues:
            raise ValueError("；".join(issues))

        setup: list[str] = []
        exploration = source_history.get("exploration_trace")
        exploration_steps = exploration.get("steps") if isinstance(exploration, dict) else []
        initial_window = ""
        if isinstance(exploration_steps, list):
            initial = next((item for item in exploration_steps if isinstance(item, dict)), None)
            if initial:
                initial_window = str(initial.get("window_name") or "").strip()
        if (
            re.fullmatch(r"[A-Z][A-Z0-9_]*", initial_window)
            and not any(self._command_name(command) == "ENTER_PAGE" for command in normalized_actions)
        ):
            setup.append(f":ENTER_PAGE:{initial_window},0")

        candidate_actions: list[str] = []
        action_cursor = 0
        action_started = False
        checkpoint_cursor = 0
        for item in trace:
            if not isinstance(item, dict):
                continue
            if item.get("source") == "agent" and item.get("phase") == "action" and item.get("ok") is True:
                command = normalized_actions[action_cursor]
                action_cursor += 1
                candidate_actions.append(command)
                action_started = True
                continue
            if (
                item.get("command_name") == "HOST_SCREENSHOT"
                and item.get("ok") is True
                and item.get("kind") in {"capture", "screenshot"}
            ):
                checkpoint_cursor += 1
                screenshot_command = f":HOST_SCREENSHOT:{checkpoint_cursor}"
                if action_started:
                    candidate_actions.append(screenshot_command)
                else:
                    setup.append(screenshot_command)

        return {
            "setup": setup,
            "actions": candidate_actions,
            "collect": [],
            "verification_points": points,
            "note": (
                "Agent-loop 自主探索候选，来源运行 "
                f"{str(source_history.get('id') or '').strip()}"
            ),
        }

    def stage_agent_candidate(
        self,
        *,
        sheet: str,
        case_id: str,
        project: str,
        source_history: dict[str, Any],
    ) -> dict[str, Any]:
        """显式用户操作后临时写入候选；不会写外部探索账本。"""

        project_meta = _test_project(project)
        candidate = self.build_agent_candidate(
            sheet=sheet,
            case_id=case_id,
            project=project_meta["project"],
            source_history=source_history,
        )
        with self._write_lock:
            path, source_text, raw, _entries, current = self._load_writable_case(
                sheet=sheet,
                case_id=case_id,
                project=project_meta["project"],
            )
            if current.get("mapping_status") == "PROMOTED":
                raise ValueError("该用例已经是 PROMOTED")
            occupied = [
                field
                for field in self.CANDIDATE_FIELDS
                if current.get(field) not in (None, "", [])
            ]
            if occupied:
                raise RuntimeError(
                    "case_map 已存在未完成候选，拒绝覆盖: " + ", ".join(occupied)
                )
            original_fields = {
                field: {
                    "present": field in current,
                    "value": copy.deepcopy(current.get(field)),
                }
                for field in (*self.CANDIDATE_FIELDS, "mapping_status")
            }
            for field in self.CANDIDATE_FIELDS:
                current[field] = copy.deepcopy(candidate[field])
            current.pop("mapping_status", None)
            self._write_if_unchanged(path, source_text, raw)
            staged_bytes = path.read_bytes()
        return {
            "project": project_meta["project"],
            "sheet": sheet,
            "case_id": case_id,
            "source_history_id": str(source_history.get("id") or ""),
            "candidate_fields": copy.deepcopy(candidate),
            "original_fields": original_fields,
            "staged_file_sha256": hashlib.sha256(staged_bytes).hexdigest().upper(),
        }

    @classmethod
    def _candidate_matches(
        cls,
        current: dict[str, Any],
        context: dict[str, Any],
    ) -> bool:
        expected = context.get("candidate_fields")
        return bool(
            isinstance(expected, dict)
            and current.get("mapping_status") != "PROMOTED"
            and all(current.get(field) == expected.get(field) for field in cls.CANDIDATE_FIELDS)
        )

    def rollback_agent_candidate(
        self,
        context: dict[str, Any],
    ) -> dict[str, Any]:
        """只在当前 case 仍等于本次候选时恢复写入前字段。"""

        with self._write_lock:
            path, source_text, raw, _entries, current = self._load_writable_case(
                sheet=str(context.get("sheet") or ""),
                case_id=str(context.get("case_id") or ""),
                project=str(context.get("project") or ""),
            )
            if current.get("mapping_status") == "PROMOTED":
                return {"status": "promoted", "issues": []}
            if not self._candidate_matches(current, context):
                return {
                    "status": "rollback_conflict",
                    "issues": ["候选复跑期间当前 case 已被其他操作修改，未自动覆盖"],
                }
            original = context.get("original_fields")
            if not isinstance(original, dict):
                return {
                    "status": "rollback_conflict",
                    "issues": ["缺少候选写入前快照，未自动覆盖"],
                }
            for field in (*self.CANDIDATE_FIELDS, "mapping_status"):
                state = original.get(field)
                if isinstance(state, dict) and state.get("present") is True:
                    current[field] = copy.deepcopy(state.get("value"))
                else:
                    current.pop(field, None)
            self._write_if_unchanged(path, source_text, raw)
        return {"status": "rolled_back", "issues": []}

    @staticmethod
    def _hardware_bmp_issue(path: Path) -> str:
        """6202 正式证据必须是可读的 410x502 top-down 24-bit BMP。"""

        try:
            data = path.read_bytes()
        except OSError as exc:
            return f"截图无法读取: {exc}"
        if len(data) < 54 or data[:2] != b"BM":
            return "截图不是完整 BMP"
        try:
            declared_size = struct.unpack_from("<I", data, 2)[0]
            pixel_offset = struct.unpack_from("<I", data, 10)[0]
            dib_size = struct.unpack_from("<I", data, 14)[0]
            width, signed_height = struct.unpack_from("<ii", data, 18)
            planes, bits_per_pixel = struct.unpack_from("<HH", data, 26)
            compression = struct.unpack_from("<I", data, 30)[0]
        except struct.error:
            return "截图 BMP 头不完整"
        if declared_size != len(data):
            return "截图 BMP 声明长度与实际长度不一致"
        if dib_size < 40 or pixel_offset < 14 + dib_size or pixel_offset >= len(data):
            return "截图 BMP 头或像素偏移无效"
        if (width, signed_height) != (410, -502):
            return f"截图尺寸必须为 410x502 top-down，实际为 {width}x{abs(signed_height)}"
        if (planes, bits_per_pixel, compression) != (1, 24, 0):
            return "截图必须为未压缩 24-bit BGR BMP"
        row_stride = ((410 * 3) + 3) & ~3
        if pixel_offset + row_stride * 502 != len(data):
            return "截图 BMP 像素布局不可读"
        return ""

    def _audit_candidate_result(
        self,
        *,
        context: dict[str, Any],
        result: dict[str, Any],
    ) -> list[str]:
        """核对 Runner 原始结果；verdict 与映射是否可固化保持独立。"""

        issues: list[str] = []
        project_meta = _test_project(str(context.get("project") or ""))
        candidate = context.get("candidate_fields")
        candidate = candidate if isinstance(candidate, dict) else {}
        if result.get("schema_version") != 3:
            issues.append("候选复跑结果 schema_version 不是 3")
        if result.get("execution_mode") != "candidate_mapping":
            issues.append("候选复跑没有使用 execution_mode=candidate_mapping")
        if str(result.get("case_id") or "") != context.get("case_id"):
            issues.append("候选复跑结果的 case_id 不一致")
        if str(result.get("sheet") or "") != context.get("sheet"):
            issues.append("候选复跑结果的模块不一致")
        verdict = str(result.get("verdict") or "ERROR").upper()
        if verdict not in self.PROMOTABLE_VERDICTS:
            issues.append(f"候选复跑结果为 {verdict}，没有形成确定产品结论")
        if not str(result.get("reason") or "").strip():
            issues.append("候选复跑结果缺少判定理由")
        if result.get("skipped") or result.get("aborted"):
            issues.append("候选复跑被跳过或中止")
        if result.get("execution_status") not in (None, "OK"):
            issues.append("候选复跑执行状态不是 OK")
        if any(result.get(field) for field in ("setup_errors", "action_errors", "collect_errors")):
            issues.append("候选复跑存在命令或截图错误")

        provenance = result.get("provenance")
        expected_runtime_project = (
            "6202_W5230"
            if project_meta["case_map_profile"] == "6202_W5230_SIMULATOR"
            else project_meta["project"]
        )
        expected_provenance = {
            "target": project_meta["execution_target"],
            "case_map_profile": project_meta["case_map_profile"],
            "project": expected_runtime_project,
        }
        if not isinstance(provenance, dict):
            issues.append("候选复跑缺少目标 provenance")
        else:
            for field, expected in expected_provenance.items():
                if provenance.get(field) != expected:
                    issues.append(f"候选复跑 provenance.{field} 与目标不一致")
            if project_meta["execution_target"] == "hardware" and (
                provenance.get("artifact_path") not in (None, "")
                or provenance.get("artifact_sha256") not in (None, "")
            ):
                issues.append("真机候选复跑不应声明模拟器产物")

        planned = result.get("planned_commands")
        comparisons = (
            ("setup", candidate.get("setup"), planned.get("setup") if isinstance(planned, dict) else None),
            ("actions", candidate.get("actions"), planned.get("action") if isinstance(planned, dict) else None),
            ("collect", candidate.get("collect"), planned.get("collect") if isinstance(planned, dict) else None),
            ("verification_points", candidate.get("verification_points"), result.get("verification_points")),
        )
        for field, expected, actual in comparisons:
            if expected != actual:
                issues.append(f"候选复跑的 {field} 与暂存候选不一致")

        planned_actions = candidate.get("actions")
        planned_actions = planned_actions if isinstance(planned_actions, list) else []
        trace = result.get("command_trace")
        trace = trace if isinstance(trace, list) else []
        action_traces: dict[int, dict[str, Any]] = {}
        successful_business = False
        for item in trace:
            if not isinstance(item, dict):
                issues.append("候选复跑 command_trace 含非对象条目")
                continue
            if item.get("source") != "case" or item.get("phase") != "action":
                continue
            planned_index = item.get("planned_index")
            if type(planned_index) is not int or planned_index in action_traces:
                issues.append("候选复跑 action trace 的 planned_index 缺失或重复")
                continue
            action_traces[planned_index] = item
            if not 1 <= planned_index <= len(planned_actions):
                issues.append("候选复跑 action trace 的 planned_index 超出范围")
                continue
            try:
                expected = normalize_command(planned_actions[planned_index - 1])
                command = normalize_command(str(item.get("command") or ""))
                wire = normalize_command(str(item.get("wire") or ""))
            except ValueError as exc:
                issues.append(f"候选复跑 action trace 命令格式无效: {exc}")
                continue
            if command != expected or wire != expected:
                issues.append("候选复跑 action trace 与计划命令不一致")
                continue
            if item.get("ok") is not True:
                issues.append("候选复跑 action trace 存在失败命令")
            elif self._command_name(command) not in OBSERVATION_ONLY_COMMANDS:
                successful_business = True
        if set(action_traces) != set(range(1, len(planned_actions) + 1)):
            issues.append("候选复跑 action trace 未精确覆盖全部计划 actions")
        if not successful_business:
            issues.append("候选复跑没有成功的真实业务 action trace")

        contract = result.get("evidence_contract")
        contract = contract if isinstance(contract, dict) else {}
        if (
            contract.get("complete") is not True
            or contract.get("status") != "COMPLETE"
            or contract.get("issues") not in ([], None)
        ):
            issues.append("候选复跑的证据合同不完整")
        points = result.get("verification_points")
        points = points if isinstance(points, list) else []
        screenshots = result.get("screenshots")
        screenshots = screenshots if isinstance(screenshots, list) else []
        for field, actual in (
            ("planned_action_count", len(planned_actions)),
            ("attempted_action_count", len(planned_actions)),
            ("required_screenshots", len(points)),
            ("captured_screenshots", len(screenshots)),
        ):
            if contract.get(field) != actual:
                issues.append(f"候选复跑证据计数 {field} 与实际记录不一致")
        if not isinstance(contract.get("business_action_count"), int) or contract.get("business_action_count", 0) < 1:
            issues.append("候选复跑证据没有真实业务动作")
        if not points or len(points) != len(screenshots):
            issues.append("候选复跑截图没有与视觉检查点一一对应")

        seen_paths: set[Path] = set()
        seen_screenshot_trace_indexes: set[int] = set()
        job_id = str(context.get("job_id") or "")
        expected_evidence_root = (
            (self.paths.runtime_jobs / job_id).resolve()
            if SAFE_SEGMENT.fullmatch(job_id)
            else None
        )
        trace_by_index = {
            item.get("index"): item
            for item in trace
            if isinstance(item, dict) and type(item.get("index")) is int
        }
        for index, screenshot in enumerate(screenshots, start=1):
            if not isinstance(screenshot, dict):
                issues.append(f"候选复跑第 {index} 张截图记录无效")
                continue
            path_text = str(screenshot.get("path") or "")
            path = Path(path_text).resolve() if path_text else None
            if path is None or not path.is_file() or path.stat().st_size <= 0:
                issues.append(f"候选复跑第 {index} 张截图不存在或为空")
            elif path in seen_paths:
                issues.append(f"候选复跑第 {index} 张截图复用了旧路径")
            else:
                seen_paths.add(path)
                if expected_evidence_root is None:
                    issues.append("候选复跑缺少唯一任务证据目录")
                else:
                    try:
                        path.relative_to(expected_evidence_root)
                    except ValueError:
                        issues.append(f"候选复跑第 {index} 张截图不属于本次任务目录")
                if project_meta["execution_target"] == "hardware":
                    bmp_issue = self._hardware_bmp_issue(path)
                    if bmp_issue:
                        issues.append(f"候选复跑第 {index} 张截图无效: {bmp_issue}")
            expected_label = str(points[index - 1]) if index <= len(points) else ""
            if str(screenshot.get("label") or "") != expected_label:
                issues.append(f"候选复跑第 {index} 张截图标签不匹配")
            captured_at = str(screenshot.get("captured_at") or "").strip()
            try:
                captured_time = datetime.fromisoformat(captured_at)
                if captured_time.tzinfo is None:
                    raise ValueError("missing timezone")
            except ValueError:
                issues.append(f"候选复跑第 {index} 张截图缺少带时区的采集时间")
            trace_index = screenshot.get("trace_index")
            if type(trace_index) is not int:
                issues.append(f"候选复跑第 {index} 张截图缺少整数 trace_index")
            elif trace_index in seen_screenshot_trace_indexes:
                issues.append(f"候选复跑第 {index} 张截图复用了 trace_index")
            else:
                seen_screenshot_trace_indexes.add(trace_index)
            trace_item = trace_by_index.get(trace_index)
            if not isinstance(trace_item, dict) or (
                trace_item.get("source") != "case"
                or trace_item.get("kind") != "screenshot"
                or trace_item.get("ok") is not True
                or trace_item.get("checkpoint_index") != index
                or str(trace_item.get("checkpoint_label") or "") != expected_label
            ):
                issues.append(f"候选复跑第 {index} 张截图缺少对应成功 trace")
        return self._unique_issues(issues)

    def finalize_agent_candidate(
        self,
        *,
        context: dict[str, Any],
        result: dict[str, Any],
    ) -> dict[str, Any]:
        """审计候选复跑并晋升；任一门禁失败都尝试精确回滚。"""

        issues = self._audit_candidate_result(context=context, result=result)
        with self._write_lock:
            path, source_text, raw, _entries, current = self._load_writable_case(
                sheet=str(context.get("sheet") or ""),
                case_id=str(context.get("case_id") or ""),
                project=str(context.get("project") or ""),
            )
            current_hash = hashlib.sha256(path.read_bytes()).hexdigest().upper()
            if current_hash != context.get("staged_file_sha256"):
                issues.append("case_map 在候选复跑期间发生变化")
            if not self._candidate_matches(current, context):
                issues.append("case_map 当前候选与本次复跑上下文不一致")
            issues = self._unique_issues(issues)
            if not issues:
                current["mapping_status"] = "PROMOTED"
                self._write_if_unchanged(path, source_text, raw)
                return {"status": "promoted", "issues": []}

        rollback = self.rollback_agent_candidate(context)
        rollback_issues = rollback.get("issues") if isinstance(rollback, dict) else []
        return {
            "status": (
                "rolled_back"
                if rollback.get("status") == "rolled_back"
                else "rollback_conflict"
            ),
            "issues": self._unique_issues(issues + list(rollback_issues or [])),
        }

    @staticmethod
    def _history_fields(history: dict[str, Any] | None) -> dict[str, Any]:
        latest = history.get("latest") if history else None
        history_count = int(history.get("history_count") or 0) if history else 0
        latest_verdict = str(latest.get("verdict") or "").upper() if latest else ""
        if history_count == 0:
            normalized_verdict = "PENDING"
        elif latest_verdict == "SKIP":
            normalized_verdict = "CANNOT_VERIFY"
        elif latest_verdict in {"PASS", "FAIL", "CANNOT_VERIFY", "ERROR"}:
            normalized_verdict = latest_verdict
        else:
            normalized_verdict = "ERROR"
        return {
            "last_run_at": latest.get("timestamp") if latest else None,
            "history_count": history_count,
            "latest_verdict": normalized_verdict,
            "latest_workflow_status": latest.get("workflow_status") if latest else None,
            "latest_execution_status": latest.get("execution_status") if latest else None,
            "latest_evidence_status": latest.get("evidence_status") if latest else None,
            "latest_mapping_status": latest.get("mapping_status") if latest else None,
            "latest_reason_code": latest.get("reason_code") if latest else None,
        }

    @staticmethod
    def _maturity_summary(rows: list[dict[str, Any]]) -> dict[str, int]:
        return {
            "all": len(rows),
            "unexplored": sum(row["maturity_state"] == "unexplored" for row in rows),
            "externally_explored": sum(bool(row["external_explored"]) for row in rows),
            "explored_unsolidified": sum(
                row["maturity_state"] == "explored_unsolidified" for row in rows
            ),
            "solidified": sum(bool(row["is_promoted"]) for row in rows),
        }

    @classmethod
    def _batch_summary(cls, rows: list[dict[str, Any]]) -> dict[str, int]:
        return {
            category: sum(cls.run_category(row) == category for row in rows)
            for category in cls.RUN_CATEGORIES
        }

    @staticmethod
    def _module_counts(rows: list[dict[str, Any]]) -> dict[str, int]:
        counts: dict[str, int] = {}
        for row in rows:
            sheet = str(row.get("file_sheet") or row.get("sheet") or "未分类")
            counts[sheet] = counts.get(sheet, 0) + 1
        return counts

    @staticmethod
    def _verdict_summary(rows: list[dict[str, Any]]) -> dict[str, int]:
        counts = {
            "PASS": 0,
            "FAIL": 0,
            "ERROR": 0,
            "CANNOT_VERIFY": 0,
            "PENDING": 0,
            "RUNNING": 0,
        }
        for row in rows:
            verdict = str(row.get("latest_verdict") or "PENDING").upper()
            key = verdict if verdict in counts else "PENDING"
            counts[key] += 1
        return counts

    @staticmethod
    def _compact_result(row: dict[str, Any]) -> dict[str, Any]:
        return {
            key: row.get(key)
            for key in (
                "project", "case_id", "sheet", "file_sheet", "last_run_at",
                "history_count", "latest_verdict", "latest_workflow_status",
                "latest_execution_status", "latest_evidence_status",
                "latest_mapping_status", "latest_reason_code",
            )
        }

    def _all(
        self,
        project: str = DEFAULT_TEST_PROJECT,
    ) -> list[dict[str, Any]]:
        project_meta = _test_project(project)
        project = project_meta["project"]
        rows: list[dict[str, Any]] = []
        history_index = self.history.summary_index(project=project)
        case_map_root = self.paths.case_map / project_meta["case_map_dir"]
        externally_explored_ids = _external_explored_ids(self.paths, project)
        for path in sorted(case_map_root.glob("*.json"), key=lambda item: item.stem):
            for item in _case_entries(self.paths, path.stem, project):
                case_id = str(item.get("case_id") or "").strip()
                if not case_id:
                    continue
                row = {
                    "case_id": case_id,
                    "sheet": str(item.get("sheet") or path.stem),
                    "file_sheet": path.stem,
                    "priority": str(item.get("priority") or ""),
                    "precondition_text": str(item.get("precondition_text") or ""),
                    "steps_text": str(item.get("steps_text") or ""),
                    "expected_text": str(item.get("expected_text") or ""),
                    "verification_points": [
                        str(point) for point in item.get("verification_points", [])
                        if str(point).strip()
                    ] if isinstance(item.get("verification_points", []), list) else [],
                    "setup": item.get("setup", []) if isinstance(item.get("setup", []), list) else [],
                    "actions": item.get("actions", []) if isinstance(item.get("actions", []), list) else [],
                    "collect": item.get("collect", []) if isinstance(item.get("collect", []), list) else [],
                    "unable": bool(item.get("unable", False)),
                    "mapping_status": (
                        item["mapping_status"]
                        if isinstance(item.get("mapping_status"), str)
                        else ""
                    ),
                    "note": str(item.get("note") or ""),
                    **{key: project_meta[key] for key in (
                        "project", "project_label", "execution_target", "execution_target_label"
                    )},
                }
                history = history_index.get((path.stem, case_id))
                row.update(self._history_fields(history))
                row["external_explored"] = case_id in externally_explored_ids
                # 外部探索账本与映射固化是两条独立事实轴。显式的站内候选
                # 复跑也能形成 PROMOTED，但绝不能伪造一条“外部探索”记录。
                row["is_promoted"] = row["mapping_status"] == "PROMOTED"
                row["maturity_state"] = (
                    "solidified"
                    if row["is_promoted"]
                    else "explored_unsolidified"
                    if row["external_explored"]
                    else "unexplored"
                )
                rows.append(row)
        return rows

    def list(
        self,
        *,
        query: str = "",
        page: int = 1,
        page_size: int = 20,
        state_filter: str = "all",
        project: str = DEFAULT_TEST_PROJECT,
        modules: set[str] | None = None,
    ) -> dict[str, Any]:
        project_meta = _test_project(project)
        project = project_meta["project"]
        state_filter = str(state_filter or "all").strip().lower()
        if state_filter not in self.FILTERS:
            raise ValueError("state 参数不合法")
        rows = self._all(project)
        batch_summary = self._batch_summary(rows)
        module_counts = self._module_counts(rows)
        verdict_summary = self._verdict_summary(rows)
        catalog_total = len(rows)
        keywords = [word.casefold() for word in query.strip().split() if word]
        if keywords:
            rows = [
                row for row in rows
                if all(
                    word in " ".join(
                        str(row.get(field, ""))
                        for field in (
                            "case_id", "sheet", "priority", "precondition_text",
                            "steps_text", "expected_text", "verification_points",
                            "mapping_status", "note",
                        )
                    ).casefold()
                    for word in keywords
                )
            ]
        normalized_modules = {
            str(module).strip() for module in (modules or set()) if str(module).strip()
        }
        if normalized_modules:
            rows = [row for row in rows if row["file_sheet"] in normalized_modules]
        summary = self._maturity_summary(rows)
        if state_filter == "unexplored":
            rows = [row for row in rows if row["maturity_state"] == "unexplored"]
        elif state_filter == "externally_explored":
            rows = [row for row in rows if row["external_explored"]]
        elif state_filter == "explored_unsolidified":
            rows = [row for row in rows if row["maturity_state"] == state_filter]
        elif state_filter == "solidified":
            rows = [row for row in rows if row["is_promoted"]]
        rows.sort(key=lambda row: (row["file_sheet"], row["case_id"]))
        total = len(rows)
        total_pages = (total + page_size - 1) // page_size
        actual_page = min(page, total_pages) if total_pages else 1
        offset = (actual_page - 1) * page_size
        return {
            "items": rows[offset:offset + page_size],
            "page": actual_page,
            "page_size": page_size,
            "total": total,
            "total_pages": total_pages,
            "summary": summary,
            "batch_summary": batch_summary,
            "module_counts": module_counts,
            "verdict_summary": verdict_summary,
            "catalog_total": catalog_total,
            "state_filter": state_filter,
            **{key: project_meta[key] for key in (
                "project", "project_label", "execution_target", "execution_target_label"
            )},
            "projects": _test_project_options(),
        }

    def overview(
        self,
        *,
        project: str = DEFAULT_TEST_PROJECT,
        recent_limit: int = 7,
        exception_limit: int = 6,
    ) -> dict[str, Any]:
        project_meta = _test_project(project)
        project = project_meta["project"]
        rows = self._all(project)
        recent = sorted(
            (row for row in rows if row.get("last_run_at")),
            key=lambda row: str(row.get("last_run_at") or ""),
            reverse=True,
        )
        exceptions = [
            row for row in recent
            if str(row.get("latest_verdict") or "").upper()
            in {"FAIL", "ERROR", "CANNOT_VERIFY", "SKIP"}
        ]
        return {
            "summary": self._maturity_summary(rows),
            "batch_summary": self._batch_summary(rows),
            "module_counts": self._module_counts(rows),
            "verdict_summary": self._verdict_summary(rows),
            "catalog_total": len(rows),
            "recent_items": [
                self._compact_result(row) for row in recent[:recent_limit]
            ],
            "recent_exceptions": [
                self._compact_result(row) for row in exceptions[:exception_limit]
            ],
            **{key: project_meta[key] for key in (
                "project", "project_label", "execution_target", "execution_target_label"
            )},
            "projects": _test_project_options(),
        }

    def recent(
        self,
        *,
        project: str = DEFAULT_TEST_PROJECT,
        limit: int = 8,
    ) -> dict[str, Any]:
        project_meta = _test_project(project)
        project = project_meta["project"]
        rows: list[dict[str, Any]] = []
        for (sheet, case_id), history in self.history.summary_index(project=project).items():
            row = {
                "project": project,
                "case_id": case_id,
                "sheet": sheet,
                "file_sheet": sheet,
                **self._history_fields(history),
            }
            if row["last_run_at"]:
                rows.append(row)
        rows.sort(key=lambda row: str(row.get("last_run_at") or ""), reverse=True)
        return {
            "items": rows[:limit],
            **{key: project_meta[key] for key in (
                "project", "project_label", "execution_target", "execution_target_label"
            )},
        }

    def get(
        self,
        sheet: str,
        case_id: str,
        *,
        project: str = DEFAULT_TEST_PROJECT,
    ) -> dict[str, Any] | None:
        project_meta = _test_project(project)
        project = project_meta["project"]
        case_id = _safe_segment(case_id, "测试用例编号")
        for item in self._all(project):
            if item["file_sheet"] != sheet or item["case_id"] != case_id:
                continue
            result = dict(item)
            result["history"] = self.history.list(sheet, case_id, project=project)
            result["history_count"] = len(result["history"])
            return result
        return None

    @staticmethod
    def run_category(row: dict[str, Any]) -> str:
        """所有用例都归入一个独立的最近运行结果分类。"""
        if int(row.get("history_count") or 0) == 0:
            return "untested"
        verdict = str(row.get("latest_verdict") or "").upper()
        if verdict == "PASS":
            return "pass"
        if verdict == "FAIL":
            return "fail"
        if verdict == "CANNOT_VERIFY":
            return "cannot_verify"
        if verdict == "SKIP":
            return "cannot_verify"
        return "error"

    @classmethod
    def batch_category(cls, row: dict[str, Any]) -> str | None:
        """已通过用例默认不重跑；其余最近运行分类均可组成批次。"""
        category = cls.run_category(row)
        return category if category in cls.BATCH_CATEGORIES else None

    def executable(
        self,
        categories: set[str] | None = None,
        *,
        project: str = DEFAULT_TEST_PROJECT,
    ) -> list[dict[str, Any]]:
        """返回全部可运行用例；可按最新测试状态筛选。"""
        rows = self._all(project)
        if categories is None:
            return rows
        unknown = categories - self.BATCH_CATEGORIES
        if unknown:
            raise ValueError(f"批次分类不合法: {', '.join(sorted(unknown))}")
        if not categories:
            raise ValueError("至少选择一类测试用例")
        return [row for row in rows if self.batch_category(row) in categories]


class CaseTestManager:
    """复用现有测试 CLI，负责网页端单条及批次测试。"""

    ACTIVE_STATUSES = {"queued", "running", "finalizing", "orphaned"}
    RESUMABLE_STATUSES = {"cancelled", "interrupted", "failed"}
    HARDWARE_INFRASTRUCTURE_MARKERS = (
        "gui_ping",
        "hardware serial",
        "hardwareserial",
        "serialtransport",
        "supercom",
        "named pipe",
        "命名管道",
        "windows mtp",
        "mtp screenshot",
        "mtp operation",
        "usb device",
        "vid_301a&pid_6808",
        "capture provider",
        "no result for",
    )

    def __init__(self, paths: AppPaths, cases: CaseMapRepository, history: TestHistoryStore):
        self.paths = paths
        self.cases = cases
        self.history = history
        self._lock = threading.Lock()
        self._jobs: dict[str, dict[str, Any]] = {}
        self._active_job_ids: dict[str, str] = {}
        self._load_batches()
        self._recover_stale_promotions()

    @staticmethod
    def _execution_slot(job: dict[str, Any]) -> str:
        target = str(job.get("execution_target") or "simulator").strip().lower()
        if target not in {"hardware", "simulator"}:
            raise ValueError(f"未知测试目标: {target or '空'}")
        return target

    @staticmethod
    def _execution_slot_label(slot: str) -> str:
        return "真机" if slot == "hardware" else "模拟器"

    def _active_job_id_for_slot_locked(self, slot: str) -> str | None:
        job_id = self._active_job_ids.get(slot)
        if not job_id:
            return None
        job = self._jobs.get(job_id)
        if job and job.get("status") in self.ACTIVE_STATUSES:
            return job_id
        self._active_job_ids.pop(slot, None)
        return None

    def _claim_execution_slot_locked(self, job: dict[str, Any]) -> None:
        slot = self._execution_slot(job)
        active_job_id = self._active_job_id_for_slot_locked(slot)
        if active_job_id and active_job_id != job["id"]:
            label = self._execution_slot_label(slot)
            raise RuntimeError(
                f"已有测试任务 {active_job_id} 正在运行（{label}资源已占用）"
            )
        self._active_job_ids[slot] = str(job["id"])

    def _release_execution_slot_locked(self, job_id: str) -> None:
        for slot, active_job_id in list(self._active_job_ids.items()):
            if active_job_id == job_id:
                self._active_job_ids.pop(slot, None)

    def _batch_state_path(self, job_id: str) -> Path:
        return self.paths.runtime_jobs / job_id / BATCH_STATE_FILE

    def _batch_cases_path(self, job_id: str) -> Path:
        return self.paths.runtime_jobs / job_id / BATCH_CASES_FILE

    def _promotion_state_path(self, job_id: str) -> Path:
        return self.paths.runtime_jobs / job_id / PROMOTION_STATE_FILE

    def _single_test_state_path(self, job_id: str) -> Path:
        return self.paths.runtime_jobs / job_id / SINGLE_TEST_STATE_FILE

    def _persist_single_test_locked(self, job: dict[str, Any]) -> None:
        if job.get("type") != "single":
            return
        job_id = str(job["id"])
        _write_json(
            self._single_test_state_path(job_id),
            {"state_schema_version": 1, **self._job_snapshot_locked(job)},
        )

    def _persist_test_job_locked(self, job: dict[str, Any]) -> None:
        if job.get("type") == "batch":
            self._persist_batch_locked(job)
        elif job.get("type") == "single":
            self._persist_single_test_locked(job)

    def _recover_loaded_test_job(self, job: dict[str, Any]) -> None:
        """Reconcile a persisted active task without trusting a possibly reused PID."""

        pid = job.get("process_pid")
        identity = str(job.get("process_identity") or "") or None
        process_stopped = not _process_is_alive(pid) or _terminate_pid_tree(
            pid,
            expected_identity=identity,
        )
        job["finished_at"] = _now()
        job["current_node"] = None
        job["cancel_requested"] = False
        job["workflow_status"] = "interrupted"
        job["execution_status"] = "ERROR"
        if str(job.get("verdict") or "PENDING").upper() == "PENDING":
            job["verdict"] = "CANNOT_VERIFY"
        if process_stopped:
            job["status"] = "interrupted"
            job["reason_code"] = "SERVICE_RESTART"
            job["interruption_reason"] = "前端服务重启，原执行进程已结束"
        else:
            job["status"] = "orphaned"
            job["reason_code"] = "ORPHAN_PROCESS"
            job["interruption_reason"] = (
                "前端服务重启后仍检测到无法安全确认或停止的原执行进程"
            )
            self._claim_execution_slot_locked(job)
        job["error"] = job["interruption_reason"]

    def _persist_promotion_state(
        self,
        job: dict[str, Any],
        *,
        status: str,
        issues: list[str] | None = None,
    ) -> None:
        context = job.get("promotion_context")
        if not isinstance(context, dict):
            return
        _write_json(self._promotion_state_path(str(job["id"])), {
            "job_id": str(job["id"]),
            "project": str(job.get("project") or ""),
            "sheet": str(job.get("sheet") or ""),
            "case_id": str(job.get("case_id") or ""),
            "status": status,
            "updated_at": _now(),
            "issues": list(issues or []),
            "context": context,
        })

    def _recover_stale_promotions(self) -> None:
        """服务重启时回滚尚未形成终态的临时候选。"""

        if not self.paths.runtime_jobs.is_dir():
            return
        for state_path in self.paths.runtime_jobs.glob(f"*/{PROMOTION_STATE_FILE}"):
            state = _read_json(state_path)
            if not isinstance(state, dict) or state.get("status") not in {
                "staged", "queued", "running", "finalizing",
            }:
                continue
            context = state.get("context")
            if not isinstance(context, dict):
                continue
            try:
                rollback = self.cases.rollback_agent_candidate(context)
                status = str(rollback.get("status") or "rollback_conflict")
                issues = list(rollback.get("issues") or [])
            except BaseException as exc:
                status = "rollback_conflict"
                issues = [f"服务重启后的候选回滚失败: {exc}"]
            state["status"] = (
                "rolled_back_on_restart" if status == "rolled_back" else status
            )
            state["issues"] = issues
            state["updated_at"] = _now()
            _write_json(state_path, state)
            job_id = str(state.get("job_id") or "")
            with self._lock:
                job = self._jobs.get(job_id)
                if job is not None:
                    job["promotion_status"] = state["status"]
                    job["promotion_issues"] = issues
                    self._persist_single_test_locked(job)

    def _persist_batch_locked(self, job: dict[str, Any]) -> None:
        if job.get("type") != "batch":
            return
        job_id = str(job["id"])
        cases_path = self._batch_cases_path(job_id)
        if not cases_path.is_file():
            _write_json(cases_path, job.get("cases", []))
        state = {
            key: value
            for key, value in job.items()
            if key not in {
                "process", "cases", "current_case_data", "current_runtime_dir",
                "current_runtime_archived",
            }
        }
        state["state_schema_version"] = 1
        _write_json(self._batch_state_path(job_id), state)

    def _reconcile_batch_history(
        self,
        job: dict[str, Any],
        *,
        records: dict[str, dict[str, Any]] | None = None,
    ) -> bool:
        """补回“历史已保存但进度文件尚未落盘”的极小崩溃窗口。"""
        changed = False
        if records is None:
            records = self.history.batch_records(str(job["id"]))
        cases = job.get("cases", [])
        completed = min(max(int(job.get("completed") or 0), 0), len(cases))
        while completed < len(cases):
            case = cases[completed]
            token = f"{completed + 1:04d}-{case['case_id']}"
            run = records.get(token)
            if run is None:
                break
            verdict = str(run.get("verdict") or "ERROR").upper()
            if verdict == "SKIP":
                verdict = "CANNOT_VERIFY"
            if verdict not in job["verdict_counts"]:
                verdict = "ERROR"
            job["verdict_counts"][verdict] += 1
            history_id = str(run.get("id") or "") or None
            recent = {
                "sheet": str(run.get("sheet") or case.get("file_sheet") or ""),
                "case_id": str(run.get("case_id") or case.get("case_id") or ""),
                "project": str(run.get("project") or case.get("project") or job.get("project") or DEFAULT_TEST_PROJECT),
                "project_label": str(run.get("project_label") or case.get("project_label") or job.get("project_label") or ""),
                "execution_target": str(run.get("execution_target") or case.get("execution_target") or job.get("execution_target") or "simulator"),
                "execution_target_label": str(run.get("execution_target_label") or case.get("execution_target_label") or job.get("execution_target_label") or "模拟器"),
                "verdict": verdict,
                "reason": str(run.get("reason") or ""),
                "history_id": history_id,
                "finished_at": run.get("finished_at"),
                "screenshot_count": len(run.get("screenshots", [])) if isinstance(run.get("screenshots"), list) else 0,
            }
            known = {str(item.get("history_id") or "") for item in job.get("recent_results", [])}
            if history_id and history_id not in known:
                job["recent_results"] = ([recent] + job.get("recent_results", []))[:30]
            completed += 1
            changed = True
        if completed != int(job.get("completed") or 0):
            job["completed"] = completed
            job["current_index"] = completed
        return changed

    def _load_batches(self) -> None:
        if not self.paths.runtime_jobs.is_dir():
            return
        loaded_jobs: list[dict[str, Any]] = []
        for state_path in self.paths.runtime_jobs.glob(f"*/{BATCH_STATE_FILE}"):
            state = _read_json(state_path)
            cases = _read_json(state_path.with_name(BATCH_CASES_FILE))
            if not isinstance(state, dict) or state.get("type") != "batch" or not isinstance(cases, list):
                continue
            job_id = str(state.get("id") or "")
            if not SAFE_SEGMENT.fullmatch(job_id) or state_path.parent.name != job_id:
                continue
            job = {**state, "cases": cases}
            project_meta = _test_project(str(job.get("project") or DEFAULT_TEST_PROJECT))
            for key in (
                "project", "project_label", "execution_target", "execution_target_label"
            ):
                job.setdefault(key, project_meta[key])
            job.setdefault("verdict_counts", {key: 0 for key in ("PASS", "FAIL", "ERROR", "CANNOT_VERIFY")})
            job.setdefault("recent_results", [])
            job.setdefault("completed", 0)
            job.setdefault("total", len(cases))
            job.setdefault("case_attempts", {})
            job.setdefault("execution_error_count", 0)
            job.setdefault("evidence_error_count", 0)
            job.setdefault("evidence_status", "NOT_RECORDED")
            job.setdefault("mapping_status", "NOT_APPLICABLE")
            loaded_jobs.append(job)

        records_by_batch = self.history.batch_records_many({
            str(job["id"]) for job in loaded_jobs
        })
        for job in loaded_jobs:
            job_id = str(job["id"])
            self._reconcile_batch_history(
                job,
                records=records_by_batch[job_id],
            )
            if job.get("status") in self.ACTIVE_STATUSES:
                self._recover_loaded_test_job(job)
            self._jobs[job_id] = job
            self._persist_batch_locked(job)

        for state_path in self.paths.runtime_jobs.glob(f"*/{SINGLE_TEST_STATE_FILE}"):
            state = _read_json(state_path)
            if not isinstance(state, dict) or state.get("type") != "single":
                continue
            job_id = str(state.get("id") or "")
            if not SAFE_SEGMENT.fullmatch(job_id) or state_path.parent.name != job_id:
                continue
            project_meta = _test_project(str(state.get("project") or DEFAULT_TEST_PROJECT))
            for key in (
                "project", "project_label", "execution_target", "execution_target_label",
            ):
                state.setdefault(key, project_meta[key])
            if state.get("status") in self.ACTIVE_STATUSES:
                self._recover_loaded_test_job(state)
            self._jobs[job_id] = state
            self._persist_single_test_locked(state)

    def start(
        self,
        *,
        sheet: str,
        case_id: str,
        project: str = DEFAULT_TEST_PROJECT,
        candidate_replay: bool = False,
        promotion_source: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        project_meta = _test_project(project)
        project = project_meta["project"]
        case = self.cases.get(sheet, case_id, project=project)
        if case is None:
            raise ValueError("测试用例不存在")
        if candidate_replay and not isinstance(promotion_source, dict):
            raise ValueError("候选复跑缺少自主探索来源记录")
        promotion_context: dict[str, Any] | None = None
        with self._lock:
            job_id = uuid.uuid4().hex[:12]
            slot_claim = {
                "id": job_id,
                **{key: project_meta[key] for key in (
                    "project", "project_label", "execution_target", "execution_target_label"
                )},
            }
            self._claim_execution_slot_locked(slot_claim)
            try:
                if candidate_replay:
                    promotion_context = self.cases.stage_agent_candidate(
                        sheet=sheet,
                        case_id=case_id,
                        project=project,
                        source_history=promotion_source or {},
                    )
                    promotion_context["job_id"] = job_id
                    case = self.cases.get(sheet, case_id, project=project)
                    if case is None:
                        raise RuntimeError("候选写入后无法重新读取测试用例")
                job = {
                    **slot_claim,
                    "type": "single",
                    "sheet": sheet,
                    "case_id": case_id,
                    "case": case,
                    "status": "queued",
                    "current_node": "load",
                    "nodes": {node: "pending" for node in TEST_WORKFLOW_NODES},
                    "verdict": "PENDING",
                    "workflow_status": "queued",
                    "execution_status": "PENDING",
                    "evidence_status": "PENDING",
                    "mapping_status": str(case.get("mapping_status") or "NOT_RECORDED"),
                    "reason_code": None,
                    "reason": "",
                    "created_at": _now(),
                    "started_at": None,
                    "finished_at": None,
                    "history_id": None,
                    "error": None,
                    "candidate_replay": bool(candidate_replay),
                    "promotion_flow": bool(candidate_replay),
                    "promotion_status": "pending" if candidate_replay else None,
                    "promotion_issues": [],
                    "promotion_source_history_id": (
                        str((promotion_source or {}).get("id") or "")
                        if candidate_replay else None
                    ),
                    "promotion_context": promotion_context,
                }
                self._jobs[job_id] = job
                self._persist_single_test_locked(job)
                if candidate_replay:
                    self._persist_promotion_state(job, status="queued")
            except BaseException:
                self._jobs.pop(job_id, None)
                self._release_execution_slot_locked(job_id)
                if promotion_context is not None:
                    self.cases.rollback_agent_candidate(promotion_context)
                raise
        thread = threading.Thread(target=self._run, args=(job_id,), daemon=True, name=f"case-test-{job_id}")
        try:
            thread.start()
        except BaseException:
            with self._lock:
                job = self._jobs[job_id]
                job["status"] = "failed"
                job["error"] = "候选复跑线程启动失败" if candidate_replay else "测试线程启动失败"
                job["workflow_status"] = "failed"
                job["execution_status"] = "ERROR"
                job["reason_code"] = "THREAD_START_FAILED"
                job["finished_at"] = _now()
                self._release_execution_slot_locked(job_id)
                self._persist_single_test_locked(job)
            if promotion_context is not None:
                rollback = self.cases.rollback_agent_candidate(promotion_context)
                with self._lock:
                    job["promotion_status"] = rollback.get("status")
                    job["promotion_issues"] = list(rollback.get("issues") or [])
                    self._persist_promotion_state(
                        job,
                        status=str(job["promotion_status"] or "rollback_conflict"),
                        issues=job["promotion_issues"],
                    )
            raise
        return self.get(job_id) or job

    def start_batch(
        self,
        *,
        limit: int = 0,
        case_refs: list[dict[str, str]] | None = None,
        categories: set[str] | None = None,
        project: str = DEFAULT_TEST_PROJECT,
    ) -> dict[str, Any]:
        project_meta = _test_project(project)
        project = project_meta["project"]
        if case_refs is not None and categories is not None:
            raise ValueError("cases 和 categories 不能同时使用")
        cases = self.cases.executable(categories, project=project)
        if case_refs is not None:
            by_key = {
                (str(case["file_sheet"]), str(case["case_id"])): case
                for case in cases
            }
            selected: list[dict[str, Any]] = []
            seen: set[tuple[str, str]] = set()
            for ref in case_refs:
                key = (ref["sheet"], ref["case_id"])
                if key in seen:
                    continue
                case = by_key.get(key)
                if case is None:
                    raise ValueError(f"用例不存在: {key[0]} / {key[1]}")
                seen.add(key)
                selected.append(case)
            cases = selected
        if limit:
            cases = cases[:limit]
        if not cases:
            raise ValueError("没有可执行测试用例")
        with self._lock:
            job_id = uuid.uuid4().hex[:12]
            job = {
                "id": job_id,
                "type": "batch",
                **{key: project_meta[key] for key in (
                    "project", "project_label", "execution_target", "execution_target_label"
                )},
                "status": "queued",
                "workflow_status": "queued",
                "execution_status": "PENDING",
                "evidence_status": "PENDING",
                "mapping_status": "NOT_APPLICABLE",
                "reason_code": None,
                "created_at": _now(),
                "started_at": None,
                "finished_at": None,
                "total": len(cases),
                "completed": 0,
                "current_index": 0,
                "current_case": None,
                "current_node": "load",
                "verdict_counts": {
                    "PASS": 0,
                    "FAIL": 0,
                    "ERROR": 0,
                    "CANNOT_VERIFY": 0,
                },
                "execution_error_count": 0,
                "evidence_error_count": 0,
                "recent_results": [],
                "cancel_requested": False,
                "error": None,
                "case_attempts": {},
                "selected_categories": sorted(categories) if categories is not None else [],
                "cases": cases,
            }
            self._claim_execution_slot_locked(job)
            self._jobs[job_id] = job
            self._persist_batch_locked(job)
        thread = threading.Thread(
            target=self._run_batch,
            args=(job_id,),
            daemon=True,
            name=f"case-test-batch-{job_id}",
        )
        try:
            thread.start()
        except BaseException:
            with self._lock:
                job = self._jobs[job_id]
                job["status"] = "failed"
                job["workflow_status"] = "failed"
                job["execution_status"] = "ERROR"
                job["reason_code"] = "THREAD_START_FAILED"
                job["error"] = "批次测试线程启动失败"
                job["finished_at"] = _now()
                self._release_execution_slot_locked(job_id)
                self._persist_batch_locked(job)
            raise
        return self.get(job_id) or job

    def cancel(self, job_id: str) -> dict[str, Any]:
        job_id = _safe_segment(job_id, "任务编号")
        process: Any = None
        orphan_pid: int | None = None
        orphan_identity: str | None = None
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise ValueError("测试任务不存在")
            if job.get("status") == "finalizing":
                raise ValueError("业务执行已结束，正在保存测试记录，不能再取消")
            if job.get("status") not in self.ACTIVE_STATUSES:
                raise ValueError("测试任务已经结束")
            job["cancel_requested"] = True
            job["interruption_reason"] = "用户请求取消当前测试任务"
            job["reason_code"] = "USER_CANCELLED"
            process = job.get("process")
            if job.get("status") == "orphaned":
                orphan_pid = job.get("process_pid")
                orphan_identity = str(job.get("process_identity") or "") or None
            self._persist_test_job_locked(job)

        stopped = True
        if process is not None:
            stopped = _terminate_process_tree(process)
        elif orphan_pid is not None:
            stopped = _terminate_pid_tree(
                orphan_pid,
                expected_identity=orphan_identity,
            )

        if orphan_pid is not None:
            with self._lock:
                job = self._jobs[job_id]
                if stopped:
                    job["status"] = "cancelled"
                    job["workflow_status"] = "cancelled"
                    job["execution_status"] = "ERROR"
                    if str(job.get("verdict") or "PENDING").upper() == "PENDING":
                        job["verdict"] = "CANNOT_VERIFY"
                    job["finished_at"] = _now()
                    job["current_node"] = None
                    self._release_execution_slot_locked(job_id)
                else:
                    job["cancel_requested"] = False
                    job["error"] = "无法确认原执行进程身份或停止进程，任务仍保持隔离"
                self._persist_test_job_locked(job)
            if not stopped:
                raise RuntimeError("无法安全停止重启前遗留的测试进程")
        return self.get(job_id) or {}

    def cancel_batch(self, job_id: str) -> dict[str, Any]:
        return self.cancel(job_id)

    def resume_batch(self, job_id: str) -> dict[str, Any]:
        job_id = _safe_segment(job_id, "任务编号")
        should_start = False
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise ValueError("测试任务不存在")
            if job.get("type") != "batch":
                raise ValueError("只能继续批次测试")
            slot = self._execution_slot(job)
            active_job_id = self._active_job_id_for_slot_locked(slot)
            if job.get("status") in self.ACTIVE_STATUSES:
                if active_job_id in {None, job_id}:
                    self._active_job_ids[slot] = job_id
                    return self._job_snapshot_locked(job)
                raise RuntimeError(f"批次 {job_id} 已经在运行")
            if int(job.get("completed") or 0) >= int(job.get("total") or 0):
                raise ValueError("批次已经全部完成")
            if job.get("status") not in self.RESUMABLE_STATUSES:
                raise ValueError("当前批次状态不能继续")
            if active_job_id:
                label = self._execution_slot_label(slot)
                raise RuntimeError(
                    f"已有测试任务 {active_job_id} 正在运行（{label}资源已占用）"
                )
            job["status"] = "queued"
            job["cancel_requested"] = False
            job["finished_at"] = None
            job["current_case"] = None
            job["current_node"] = "load"
            job["resumed_at"] = _now()
            job["resume_count"] = int(job.get("resume_count") or 0) + 1
            job["error"] = None
            job.pop("interruption_reason", None)
            job.pop("last_interrupted_case", None)
            self._claim_execution_slot_locked(job)
            self._persist_batch_locked(job)
            should_start = True
        if should_start:
            thread = threading.Thread(
                target=self._run_batch,
                args=(job_id,),
                daemon=True,
                name=f"case-test-batch-{job_id}",
            )
            try:
                thread.start()
            except BaseException:
                with self._lock:
                    job = self._jobs[job_id]
                    job["status"] = "failed"
                    job["workflow_status"] = "failed"
                    job["execution_status"] = "ERROR"
                    job["reason_code"] = "THREAD_START_FAILED"
                    job["error"] = "批次恢复线程启动失败"
                    job["finished_at"] = _now()
                    self._release_execution_slot_locked(job_id)
                    self._persist_batch_locked(job)
                raise
        return self.get(job_id) or {}

    @staticmethod
    def _job_snapshot_locked(job: dict[str, Any]) -> dict[str, Any]:
        return {
            key: value
            for key, value in job.items()
            if key not in {
                "process", "case", "cases", "current_case_data", "current_runtime_dir",
                "current_runtime_archived", "promotion_context",
            }
        }

    def get(self, job_id: str) -> dict[str, Any] | None:
        job_id = _safe_segment(job_id, "任务编号")
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return None
            snapshot = self._job_snapshot_locked(job)
            current_dir = Path(job["current_runtime_dir"]) if job.get("current_runtime_dir") else None
            current_case = job.get("current_case_data") if isinstance(job.get("current_case_data"), dict) else {}
            current_token = str(job.get("current_case_token") or "")
        screenshots: list[dict[str, Any]] = []
        if current_dir and current_dir.is_dir() and current_token:
            points = current_case.get("verification_points", [])
            labels = points if isinstance(points, list) else []
            live_paths = sorted(
                (
                    path
                    for path in current_dir.iterdir()
                    if path.is_file() and LIVE_TEST_SCREENSHOT_FILE.fullmatch(path.name)
                ),
                key=lambda path: path.name,
            )
            for index, path in enumerate(live_paths, start=1):
                step_match = re.fullmatch(r"step_(\d{2,3})\.bmp", path.name)
                fallback_label = (
                    f"探索步骤 {int(step_match.group(1)) + 1}"
                    if step_match
                    else f"检查点 {index}"
                )
                screenshots.append({
                    "index": index,
                    "label": str(labels[index - 1]) if index <= len(labels) else fallback_label,
                    "url": (
                        f"/api/tests/jobs/{quote(job_id, safe='')}/screenshots/"
                        f"{quote(current_token, safe='')}/{quote(path.name, safe='')}"
                    ),
                })
        snapshot["live_screenshots"] = screenshots
        snapshot["resume_available"] = bool(
            snapshot.get("type") == "batch"
            and snapshot.get("status") in self.RESUMABLE_STATUSES
            and int(snapshot.get("completed") or 0) < int(snapshot.get("total") or 0)
        )
        return snapshot

    def screenshot_path(self, job_id: str, token: str, file_name: str) -> Path:
        job_id = _safe_segment(job_id, "任务编号")
        token = _safe_segment(token, "用例运行编号")
        if not LIVE_TEST_SCREENSHOT_FILE.fullmatch(file_name):
            raise ValueError("截图文件名不合法")
        with self._lock:
            if job_id not in self._jobs:
                raise ValueError("测试任务不存在")
        root = (self.paths.runtime_jobs / job_id).resolve()
        path = (root / token / file_name).resolve()
        path.relative_to(root)
        return path

    def active_jobs(self) -> list[dict[str, Any]]:
        with self._lock:
            job_ids = [
                job_id
                for slot in ("hardware", "simulator")
                if (job_id := self._active_job_id_for_slot_locked(slot))
            ]
        jobs = [self.get(job_id) for job_id in job_ids]
        return [job for job in jobs if job is not None]

    def active(self, execution_target: str | None = None) -> dict[str, Any] | None:
        jobs = self.active_jobs()
        if execution_target is None:
            return jobs[0] if jobs else None
        target = str(execution_target).strip().lower()
        return next(
            (job for job in jobs if job.get("execution_target") == target),
            None,
        )

    @staticmethod
    def _concise_execution_error(value: object) -> str:
        lines = [line.strip() for line in str(value or "").splitlines() if line.strip()]
        return (lines[-1] if lines else "真机 Runner 未生成有效结果")[:1000]

    @classmethod
    def _hardware_infrastructure_failure(cls, execution: dict[str, Any]) -> str | None:
        """Return a batch-stopping host/device failure, never a product verdict."""

        result = execution.get("result")
        if not isinstance(result, dict) or not result:
            return cls._concise_execution_error(
                execution.get("execution_reason") or execution.get("stderr")
            )
        if not execution.get("execute_failed"):
            return None

        messages = [
            str(execution.get("execution_reason") or ""),
            *(
                str(value)
                for key in ("setup_errors", "action_errors", "collect_errors")
                for value in (result.get(key) or [])
                if str(value).strip()
            ),
        ]
        combined = "\n".join(messages).casefold()
        if not any(marker in combined for marker in cls.HARDWARE_INFRASTRUCTURE_MARKERS):
            return None
        matching = next((
            message
            for message in messages
            if any(marker in message.casefold() for marker in cls.HARDWARE_INFRASTRUCTURE_MARKERS)
        ), execution.get("execution_reason"))
        return cls._concise_execution_error(matching)

    def _execute_case(
        self,
        *,
        job_id: str,
        case: dict[str, Any],
        job_dir: Path,
        hardware_reset_completed: bool = False,
    ) -> dict[str, Any]:
        project_meta = _test_project(str(case.get("project") or DEFAULT_TEST_PROJECT))
        job_dir.mkdir(parents=True, exist_ok=True)
        result_file = job_dir / "test_result.json"
        screenshot = job_dir / "screenshot.bmp"
        started_at = _now()
        child_args = [
            "--sheet",
            str(case["file_sheet"]),
            "--case-id",
            str(case["case_id"]),
            "--target",
            project_meta["execution_target"],
            "--case-map-profile",
            project_meta["case_map_profile"],
            "--result-file",
            str(result_file),
            "--screenshot-path",
            str(screenshot),
        ]
        with self._lock:
            candidate_replay = bool(self._jobs[job_id].get("candidate_replay"))
        if candidate_replay:
            child_args.append("--candidate-replay")
        if hardware_reset_completed:
            if project_meta["execution_target"] != "hardware":
                raise ValueError("hardware_reset_completed 只适用于真机用例")
            child_args.append("--skip-hardware-reset")
        argv = build_child_command("test", child_args)
        stdout = ""
        stderr = ""
        return_code: int | None = None
        process: Any = None
        timed_out = False
        execution_env = _test_process_environment(project_meta)
        try:
            process = subprocess.Popen(
                argv,
                cwd=self.paths.root,
                env=execution_env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            with self._lock:
                active_job = self._jobs[job_id]
                active_job["process"] = process
                active_job["process_pid"] = getattr(process, "pid", None)
                active_job["process_identity"] = _process_identity(
                    active_job["process_pid"]
                )
                self._persist_test_job_locked(active_job)
            try:
                stdout, stderr = _communicate_process(
                    process,
                    CASE_TEST_TIMEOUT_SECONDS,
                )
                return_code = getattr(process, "returncode", None)
            except subprocess.TimeoutExpired:
                timed_out = True
                _terminate_process_tree(process)
                try:
                    stdout, stderr = _communicate_process(
                        process,
                        PROCESS_TERMINATION_GRACE_SECONDS,
                    )
                except (OSError, subprocess.SubprocessError):
                    stdout, stderr = "", ""
                return_code = getattr(process, "returncode", None)
                stderr = (
                    f"{stderr}\n测试进程执行超时（上限 {CASE_TEST_TIMEOUT_SECONDS:g}s）"
                ).strip()
        except BaseException as exc:
            _terminate_process_tree(process)
            stderr = f"{type(exc).__name__}: {exc}"

        with self._lock:
            cancelled = bool(self._jobs[job_id].get("cancel_requested"))

        loaded = _read_json(result_file, {})
        result = loaded if isinstance(loaded, dict) else {}
        raw_verdict = str(result.get("verdict") or "ERROR").upper()
        if result.get("skipped"):
            verdict = "CANNOT_VERIFY"
        elif raw_verdict in {"PASS", "FAIL", "CANNOT_VERIFY"}:
            verdict = raw_verdict
        else:
            # ERROR is an execution outcome, not a product verdict.
            verdict = "CANNOT_VERIFY"
        evidence_contract = result.get("evidence_contract")
        evidence_issues = (
            evidence_contract.get("issues", [])
            if isinstance(evidence_contract, dict)
            else []
        )
        evidence_error = next((
            str(item.get("message") or "").strip()
            for item in evidence_issues
            if isinstance(item, dict) and str(item.get("message") or "").strip()
        ), "")
        evidence_incomplete = bool(
            isinstance(evidence_contract, dict)
            and evidence_contract
            and evidence_contract.get("complete") is not True
            and not result.get("skipped")
        )
        execute_failed = bool(
            cancelled
            or timed_out
            or not result
            or result.get("aborted")
            or result.get("setup_errors")
            or result.get("action_errors")
            or result.get("collect_errors")
            or evidence_incomplete
            or raw_verdict not in {"PASS", "FAIL", "CANNOT_VERIFY"}
        )
        error = "" if result else (stderr.strip() or "测试进程未生成结果文件")
        if cancelled:
            error = "用户取消测试任务"
            verdict = "CANNOT_VERIFY"
        elif timed_out:
            error = f"测试进程执行超时（上限 {CASE_TEST_TIMEOUT_SECONDS:g}s）"
            verdict = "CANNOT_VERIFY"
        reason = str(result.get("reason") or error)
        execution_errors = [
            str(value)
            for key in ("setup_errors", "action_errors", "collect_errors")
            for value in (result.get(key) or [])
            if str(value).strip()
        ]
        execution_reason = str(
            result.get("execution_reason")
            or (execution_errors[0] if execution_errors else evidence_error or error)
        )
        if cancelled or timed_out:
            execution_reason = error
        outcome = outcome_fields(
            result,
            workflow_default="failed" if not result else "completed",
            mapping_default=str(case.get("mapping_status") or "NOT_RECORDED"),
        )
        if cancelled:
            outcome.update({
                "workflow_status": "cancelled",
                "execution_status": "ERROR",
                "reason_code": "USER_CANCELLED",
            })
        elif timed_out:
            outcome.update({
                "workflow_status": "failed",
                "execution_status": "ERROR",
                "reason_code": "PROCESS_TIMEOUT",
            })
        elif execute_failed:
            outcome.update({
                "workflow_status": "failed",
                "execution_status": "ERROR",
                "reason_code": (
                    "EVIDENCE_INCOMPLETE"
                    if evidence_incomplete
                    else str(result.get("reason_code") or "CASE_EXECUTION_ERROR")
                ),
            })
        return {
            "result": result,
            "stdout": stdout,
            "stderr": stderr,
            "return_code": return_code,
            "verdict": verdict,
            "reason": reason,
            "execution_reason": execution_reason,
            "execute_failed": execute_failed,
            "cancelled": cancelled,
            "timed_out": timed_out,
            **outcome,
            "started_at": started_at,
            "finished_at": _now(),
            "screenshot": screenshot,
        }

    def _run(self, job_id: str) -> None:
        promotion_context: dict[str, Any] | None = None
        promotion_needs_rollback = False
        try:
            self._run_single_body(job_id)
        except BaseException as exc:
            with self._lock:
                job = self._jobs.get(job_id)
                if job is not None:
                    job["status"] = "failed"
                    job["workflow_status"] = "failed"
                    job["execution_status"] = "ERROR"
                    job["reason_code"] = "UNHANDLED_EXCEPTION"
                    job["error"] = f"{type(exc).__name__}: {exc}"
                    job["finished_at"] = _now()
                    job["current_node"] = None
                    promotion_context = (
                        job.get("promotion_context")
                        if isinstance(job.get("promotion_context"), dict)
                        else None
                    )
                    promotion_needs_rollback = bool(
                        job.get("promotion_flow")
                        and job.get("promotion_status")
                        not in {"promoted", "rolled_back", "rollback_conflict"}
                    )
                    self._persist_single_test_locked(job)
        finally:
            with self._lock:
                job = self._jobs.get(job_id)
                process = job.get("process") if job is not None else None
                if job is not None and (
                    job.get("cancel_requested") or job.get("status") == "failed"
                ):
                    context = job.get("promotion_context")
                    if isinstance(context, dict) and job.get("promotion_status") not in {
                        "promoted", "rolled_back", "rollback_conflict",
                    }:
                        promotion_context = context
                        promotion_needs_rollback = True
            _terminate_process_tree(process)
            if promotion_needs_rollback and promotion_context is not None:
                try:
                    rollback = self.cases.rollback_agent_candidate(promotion_context)
                except BaseException as exc:
                    rollback = {
                        "status": "rollback_conflict",
                        "issues": [f"异常终止后的候选回滚失败: {exc}"],
                    }
                with self._lock:
                    job = self._jobs.get(job_id)
                    if job is not None:
                        job["promotion_status"] = str(
                            rollback.get("status") or "rollback_conflict"
                        )
                        job["promotion_issues"] = list(rollback.get("issues") or [])
                        self._persist_promotion_state(
                            job,
                            status=job["promotion_status"],
                            issues=job["promotion_issues"],
                        )
            with self._lock:
                job = self._jobs.get(job_id)
                if job is not None:
                    if job.get("status") in self.ACTIVE_STATUSES:
                        if job.get("cancel_requested"):
                            job["status"] = "cancelled"
                            job["workflow_status"] = "cancelled"
                            job["execution_status"] = "ERROR"
                            job["reason_code"] = "USER_CANCELLED"
                            if str(job.get("verdict") or "PENDING").upper() == "PENDING":
                                job["verdict"] = "CANNOT_VERIFY"
                        else:
                            job["status"] = "failed"
                            job["workflow_status"] = "failed"
                            job["execution_status"] = "ERROR"
                            job["reason_code"] = (
                                job.get("reason_code") or "UNHANDLED_EXCEPTION"
                            )
                            job["error"] = job.get("error") or "测试任务异常结束"
                        job["finished_at"] = job.get("finished_at") or _now()
                        job["current_node"] = None
                    job.pop("process", None)
                    self._release_execution_slot_locked(job_id)
                    self._persist_single_test_locked(job)

    def _run_single_body(self, job_id: str) -> None:
        with self._lock:
            job = self._jobs[job_id]
            if job.get("cancel_requested"):
                job["status"] = "cancelled"
                job["workflow_status"] = "cancelled"
                job["execution_status"] = "ERROR"
                job["reason_code"] = "USER_CANCELLED"
                job["verdict"] = "CANNOT_VERIFY"
                job["finished_at"] = _now()
                return
            project_meta = _test_project(str(job.get("project") or DEFAULT_TEST_PROJECT))
            for key in (
                "project", "project_label", "execution_target", "execution_target_label",
                "case_map_profile",
            ):
                job.setdefault(key, project_meta[key])
            job["status"] = "running"
            job["started_at"] = _now()
            job["current_node"] = "execute"
            job["nodes"]["load"] = "pass"
            job["nodes"]["execute"] = "running"
            job_dir = self.paths.runtime_jobs / job_id
            job["current_runtime_dir"] = str(job_dir)
            job["current_case_token"] = ""
            job["current_case_data"] = job["case"]
            if job.get("promotion_flow"):
                job["promotion_status"] = "running"
                self._persist_promotion_state(job, status="running")
            self._persist_single_test_locked(job)

        execution = self._execute_case(job_id=job_id, case=job["case"], job_dir=job_dir)
        result = execution["result"]
        for key, value in outcome_fields(
            result,
            workflow_default="failed" if not result else "completed",
            mapping_default=str(job["case"].get("mapping_status") or "NOT_RECORDED"),
        ).items():
            execution.setdefault(key, value)
        verdict = execution["verdict"]
        reason = execution["reason"]
        execute_failed = execution["execute_failed"]
        with self._lock:
            job = self._jobs[job_id]
            job["return_code"] = execution["return_code"]
            job["finished_at"] = execution["finished_at"]
            job["verdict"] = verdict
            job["reason"] = reason
            for key in (
                "workflow_status", "execution_status", "evidence_status",
                "mapping_status", "reason_code",
            ):
                job[key] = execution[key]
            job["error"] = (
                execution["execution_reason"] if execute_failed else None
            )
            job["nodes"]["execute"] = "fail" if execute_failed else "pass"
            job["nodes"]["judge"] = "pass" if verdict == "PASS" else "fail"
            job["nodes"]["record"] = "running"
            job["current_node"] = "record"
            job["status"] = "finalizing"
            if job.get("promotion_flow"):
                job["promotion_status"] = "finalizing"
                self._persist_promotion_state(job, status="finalizing")
            self._persist_single_test_locked(job)

        try:
            history_id = self.history.create(
                job=job,
                result={
                    **result,
                    "verdict": verdict,
                    "reason": reason,
                    **{
                        key: execution[key]
                        for key in (
                            "workflow_status", "execution_status", "evidence_status",
                            "mapping_status", "reason_code",
                        )
                    },
                },
                stdout=execution["stdout"],
                stderr=execution["stderr"],
                screenshot=execution["screenshot"],
            )
        except BaseException as exc:
            history_id = None
            with self._lock:
                job["error"] = f"测试记录保存失败: {exc}"
                job["verdict"] = "CANNOT_VERIFY"
                job["workflow_status"] = "failed"
                job["execution_status"] = "ERROR"
                job["evidence_status"] = "ERROR"
                job["reason_code"] = "HISTORY_WRITE_FAILED"

        promotion: dict[str, Any] | None = None
        with self._lock:
            promotion_context = job.get("promotion_context")
            promotion_flow = bool(job.get("promotion_flow"))
        if promotion_flow and isinstance(promotion_context, dict):
            if history_id:
                try:
                    promotion = self.cases.finalize_agent_candidate(
                        context=promotion_context,
                        result=result,
                    )
                except BaseException as exc:
                    try:
                        rollback = self.cases.rollback_agent_candidate(promotion_context)
                    except BaseException as rollback_exc:
                        rollback = {
                            "status": "rollback_conflict",
                            "issues": [f"候选回滚失败: {rollback_exc}"],
                        }
                    promotion = {
                        "status": str(rollback.get("status") or "rollback_conflict"),
                        "issues": [
                            f"候选晋升审计异常: {exc}",
                            *list(rollback.get("issues") or []),
                        ],
                    }
            else:
                try:
                    promotion = self.cases.rollback_agent_candidate(promotion_context)
                except BaseException as exc:
                    promotion = {
                        "status": "rollback_conflict",
                        "issues": [f"历史保存失败后的候选回滚失败: {exc}"],
                    }
                promotion["issues"] = [
                    "候选复跑历史未成功保存，禁止晋升",
                    *list(promotion.get("issues") or []),
                ]

        with self._lock:
            job = self._jobs[job_id]
            job["history_id"] = history_id
            job["nodes"]["record"] = "pass" if history_id else "fail"
            if promotion is not None:
                job["promotion_status"] = str(
                    promotion.get("status") or "rollback_conflict"
                )
                job["promotion_issues"] = list(promotion.get("issues") or [])
                self._persist_promotion_state(
                    job,
                    status=job["promotion_status"],
                    issues=job["promotion_issues"],
                )
            job["current_node"] = None
            if job.get("cancel_requested") or execution.get("cancelled"):
                job["status"] = "cancelled"
                job["workflow_status"] = "cancelled"
                job["execution_status"] = "ERROR"
                job["reason_code"] = "USER_CANCELLED"
            elif not history_id or execution.get("workflow_status") in {
                "failed", "interrupted", "orphaned",
            }:
                job["status"] = "failed"
                job["workflow_status"] = "failed"
            else:
                job["status"] = "completed"
            job.pop("process", None)
            self._release_execution_slot_locked(job_id)
            self._persist_single_test_locked(job)

    def _run_batch(self, job_id: str) -> None:
        try:
            self._run_batch_body(job_id)
        except BaseException as exc:
            with self._lock:
                job = self._jobs.get(job_id)
                if job is not None:
                    job["status"] = "failed"
                    job["workflow_status"] = "failed"
                    job["execution_status"] = "ERROR"
                    job["reason_code"] = "UNHANDLED_EXCEPTION"
                    job["error"] = f"{type(exc).__name__}: {exc}"
                    job["finished_at"] = _now()
                    job["current_node"] = None
                    self._persist_batch_locked(job)
        finally:
            with self._lock:
                job = self._jobs.get(job_id)
                process = job.get("process") if job is not None else None
            _terminate_process_tree(process)
            with self._lock:
                job = self._jobs.get(job_id)
                if job is not None:
                    if job.get("status") in self.ACTIVE_STATUSES:
                        if job.get("cancel_requested"):
                            job["status"] = "cancelled"
                            job["workflow_status"] = "cancelled"
                            job["reason_code"] = "USER_CANCELLED"
                        else:
                            job["status"] = "failed"
                            job["workflow_status"] = "failed"
                            job["execution_status"] = "ERROR"
                            job["reason_code"] = (
                                job.get("reason_code") or "UNHANDLED_EXCEPTION"
                            )
                            job["error"] = job.get("error") or "批次任务异常结束"
                        job["finished_at"] = job.get("finished_at") or _now()
                        job["current_node"] = None
                    job.pop("process", None)
                    self._release_execution_slot_locked(job_id)
                    self._persist_batch_locked(job)

    def _run_batch_body(self, job_id: str) -> None:
        with self._lock:
            job = self._jobs[job_id]
            if job.get("cancel_requested"):
                job["status"] = "cancelled"
                job["workflow_status"] = "cancelled"
                job["reason_code"] = "USER_CANCELLED"
                job["finished_at"] = _now()
                self._persist_batch_locked(job)
                return
            execution_target = str(job.get("execution_target") or "simulator")

        hardware_environment: dict[str, str] | None = None
        if execution_target == "hardware":
            try:
                from agent_loop_system.tools.hardware_runtime_profile import (
                    load_hardware_runtime_profile,
                )

                project_meta = _test_project(
                    str(job.get("project") or DEFAULT_TEST_PROJECT)
                )
                hardware_environment = _test_process_environment(project_meta)
                load_hardware_runtime_profile(
                    project=project_meta["project"],
                    profiles_root=hardware_environment.get("W30_HARDWARE_PROFILE_ROOT") or None,
                    version=hardware_environment.get("W30_HARDWARE_PROFILE_VERSION") or None,
                )
            except Exception as exc:
                with self._lock:
                    job = self._jobs[job_id]
                    job["hardware_reset"] = {
                        "checked_at": _now(),
                        "status": "failed",
                        "error": str(exc),
                    }
                    job["status"] = "failed"
                    job["error"] = f"真机运行时档案初始化失败: {exc}"
                    job["finished_at"] = _now()
                    job["current_node"] = None
                    self._release_execution_slot_locked(job_id)
                    self._persist_batch_locked(job)
                return

        with self._lock:
            job = self._jobs[job_id]
            job["status"] = "running"
            if not job.get("started_at"):
                job["started_at"] = _now()
            cases = list(job["cases"])
            completed = min(max(int(job.get("completed") or 0), 0), len(cases))
            self._persist_batch_locked(job)

        for offset, case in enumerate(cases[completed:], start=completed):
            index = offset + 1
            token = f"{index:04d}-{case['case_id']}"
            with self._lock:
                attempts = self._jobs[job_id].setdefault("case_attempts", {})
                attempt = int(attempts.get(token) or 0) + 1
                attempts[token] = attempt
            runtime_token = token if attempt == 1 else f"{token}-attempt-{attempt:02d}"
            case_dir = self.paths.runtime_jobs / job_id / runtime_token
            with self._lock:
                previous_runtime_dir = self._jobs[job_id].get("current_runtime_dir")
                previous_archived = bool(self._jobs[job_id].get("current_runtime_archived"))
            if previous_runtime_dir and previous_archived:
                previous = Path(previous_runtime_dir).resolve()
                runtime_root = (self.paths.runtime_jobs / job_id).resolve()
                previous.relative_to(runtime_root)
                if previous.is_dir():
                    shutil.rmtree(previous)
            current_case = {
                "sheet": case["file_sheet"],
                "case_id": case["case_id"],
                "project": case.get("project", DEFAULT_TEST_PROJECT),
                "project_label": case.get("project_label", ""),
                "execution_target": case.get("execution_target", "simulator"),
                "execution_target_label": case.get("execution_target_label", "模拟器"),
                "priority": case.get("priority", ""),
                "expected_text": case.get("expected_text", ""),
                "verification_points": case.get("verification_points", []),
                "setup_count": len(case.get("setup", [])),
                "action_count": len(case.get("actions", [])),
                "collect_count": len(case.get("collect", [])),
            }
            with self._lock:
                job = self._jobs[job_id]
                job["current_index"] = index
                job["current_case"] = current_case
                job["current_case_data"] = case
                job["current_case_token"] = runtime_token
                job["current_runtime_dir"] = str(case_dir)
                job["current_runtime_archived"] = False
                job["current_node"] = (
                    "reset" if execution_target == "hardware" else "execute"
                )
                self._persist_batch_locked(job)

            if execution_target == "hardware":
                try:
                    from agent_loop_system.tools.real_device import (
                        reset_hardware_case_state,
                    )

                    reset = reset_hardware_case_state(
                        evidence_dir=case_dir / "hardware-reset",
                        environment=hardware_environment,
                    )
                except Exception as exc:
                    interruption_reason = (
                        f"真机清理失败，未启动第 {index} 条 {case['case_id']}：{exc}"
                    )
                    with self._lock:
                        job = self._jobs[job_id]
                        job["hardware_reset"] = {
                            "checked_at": _now(),
                            "status": "failed",
                            "case_id": case["case_id"],
                            "error": str(exc),
                        }
                        job["status"] = "interrupted"
                        job["error"] = str(exc)
                        job["interruption_reason"] = interruption_reason
                        job["finished_at"] = _now()
                        job["current_node"] = None
                        job["last_interrupted_case"] = job.get("current_case")
                        job["current_case"] = None
                        job.pop("process", None)
                        self._release_execution_slot_locked(job_id)
                        self._persist_batch_locked(job)
                    return
                with self._lock:
                    job = self._jobs[job_id]
                    job["hardware_reset"] = {
                        "checked_at": _now(),
                        "status": "ready",
                        "case_id": case["case_id"],
                        "reboot_status": reset.reboot_status,
                        "lease_seconds": reset.status.lease_seconds,
                        "gui_ping_attempts": reset.gui_ping_attempts,
                        "bootstrap_event_seen": reset.bootstrap_event_seen,
                        "current_page": reset.current_page,
                        "popup": reset.popup,
                    }
                    job["hardware_reset_count"] = int(
                        job.get("hardware_reset_count") or 0
                    ) + 1
                    job["current_node"] = "execute"
                    self._persist_batch_locked(job)

            execution = self._execute_case(
                job_id=job_id,
                case=case,
                job_dir=case_dir,
                hardware_reset_completed=execution_target == "hardware",
            )
            with self._lock:
                cancel_requested = bool(self._jobs[job_id].get("cancel_requested"))
            if cancel_requested:
                with self._lock:
                    job = self._jobs[job_id]
                    job["status"] = "cancelled"
                    job["workflow_status"] = "cancelled"
                    job["execution_status"] = "ERROR"
                    job["reason_code"] = "USER_CANCELLED"
                    job["interruption_reason"] = "用户取消批次测试；当前未完成用例可在恢复后重试"
                    job["finished_at"] = _now()
                    job["current_node"] = None
                    job["last_interrupted_case"] = job.get("current_case")
                    job["current_case"] = None
                    job["current_runtime_archived"] = False
                    self._persist_batch_locked(job)
                return
            if execution.get("timed_out"):
                with self._lock:
                    job = self._jobs[job_id]
                    job["status"] = "interrupted"
                    job["workflow_status"] = "failed"
                    job["execution_status"] = "ERROR"
                    job["reason_code"] = "PROCESS_TIMEOUT"
                    job["error"] = execution["execution_reason"]
                    job["interruption_reason"] = (
                        f"第 {index} 条 {case['case_id']} 执行超时；未计入完成数，可恢复重试"
                    )
                    job["finished_at"] = _now()
                    job["current_node"] = None
                    job["last_interrupted_case"] = job.get("current_case")
                    job["current_case"] = None
                    job["current_runtime_archived"] = False
                    self._persist_batch_locked(job)
                return
            infrastructure_failure = (
                self._hardware_infrastructure_failure(execution)
                if execution_target == "hardware"
                else None
            )
            if infrastructure_failure:
                interruption_reason = (
                    f"真机链路异常，已在第 {index} 条 {case['case_id']} 中断："
                    f"{infrastructure_failure}"
                )
                with self._lock:
                    job = self._jobs[job_id]
                    job["status"] = "interrupted"
                    job["workflow_status"] = "failed"
                    job["execution_status"] = "ERROR"
                    job["reason_code"] = "HARDWARE_INFRASTRUCTURE_FAILURE"
                    job["error"] = infrastructure_failure
                    job["interruption_reason"] = interruption_reason
                    job["finished_at"] = _now()
                    job["current_node"] = None
                    job["last_interrupted_case"] = job.get("current_case")
                    job["current_case"] = None
                    job["current_runtime_archived"] = False
                    job.pop("process", None)
                    self._release_execution_slot_locked(job_id)
                    self._persist_batch_locked(job)
                return
            history_job = {
                "sheet": case["file_sheet"],
                "case_id": case["case_id"],
                "project": case.get("project", job.get("project", DEFAULT_TEST_PROJECT)),
                "case": case,
                "started_at": execution["started_at"],
                "finished_at": execution["finished_at"],
                "return_code": execution["return_code"],
                "error": (
                    execution["execution_reason"]
                    if execution["execute_failed"]
                    else None
                ),
                "batch_id": job_id,
                "batch_token": token,
            }
            with self._lock:
                self._jobs[job_id]["current_node"] = "record"
            try:
                history_id = self.history.create(
                    job=history_job,
                    result={
                        **execution["result"],
                        "verdict": execution["verdict"],
                        "reason": execution["reason"],
                        **{
                            key: execution[key]
                            for key in (
                                "workflow_status", "execution_status", "evidence_status",
                                "mapping_status", "reason_code",
                            )
                        },
                    },
                    stdout=execution["stdout"],
                    stderr=execution["stderr"],
                    screenshot=execution["screenshot"],
                )
            except BaseException as exc:
                history_id = None
                execution["verdict"] = "CANNOT_VERIFY"
                execution["workflow_status"] = "failed"
                execution["execution_status"] = "ERROR"
                execution["evidence_status"] = "ERROR"
                execution["reason_code"] = "HISTORY_WRITE_FAILED"
                execution["reason"] = f"测试记录保存失败: {exc}"

            record = {
                "sheet": case["file_sheet"],
                "case_id": case["case_id"],
                "project": case.get("project", job.get("project", DEFAULT_TEST_PROJECT)),
                "project_label": case.get("project_label", job.get("project_label", "")),
                "execution_target": case.get("execution_target", job.get("execution_target", "simulator")),
                "execution_target_label": case.get("execution_target_label", job.get("execution_target_label", "模拟器")),
                "verdict": execution["verdict"],
                "workflow_status": execution["workflow_status"],
                "execution_status": execution["execution_status"],
                "evidence_status": execution["evidence_status"],
                "mapping_status": execution["mapping_status"],
                "reason_code": execution["reason_code"],
                "reason": execution["reason"],
                "history_id": history_id,
                "finished_at": execution["finished_at"],
                "screenshot_count": len(execution["result"].get("screenshots", [])),
            }
            with self._lock:
                job = self._jobs[job_id]
                job["completed"] = index
                job["verdict_counts"][execution["verdict"]] += 1
                if execution["execution_status"] == "ERROR":
                    job["execution_error_count"] = int(
                        job.get("execution_error_count") or 0
                    ) + 1
                if execution["evidence_status"] in {"ERROR", "INCOMPLETE", "MISSING"}:
                    job["evidence_error_count"] = int(
                        job.get("evidence_error_count") or 0
                    ) + 1
                job["recent_results"] = ([record] + job["recent_results"])[:30]
                job["current_runtime_archived"] = history_id is not None
                job.pop("process", None)
                cancel_requested = bool(job.get("cancel_requested"))
                self._persist_batch_locked(job)
            if cancel_requested:
                break

        with self._lock:
            job = self._jobs[job_id]
            job["finished_at"] = _now()
            job["current_case"] = None
            job["current_node"] = None
            job["status"] = "cancelled" if job.get("cancel_requested") else "completed"
            if job["status"] == "completed":
                job.pop("interruption_reason", None)
                job.pop("last_interrupted_case", None)
                job["workflow_status"] = "completed"
                job["execution_status"] = (
                    "ERROR" if int(job.get("execution_error_count") or 0) else "OK"
                )
                job["evidence_status"] = (
                    "ERROR" if int(job.get("evidence_error_count") or 0) else "COMPLETE"
                )
                job["reason_code"] = (
                    "CASE_EXECUTION_ERROR"
                    if int(job.get("execution_error_count") or 0)
                    else None
                )
            else:
                job["workflow_status"] = "cancelled"
                job["reason_code"] = "USER_CANCELLED"
            job.pop("process", None)
            self._release_execution_slot_locked(job_id)
            self._persist_batch_locked(job)


class JobManager:
    ACTIVE_STATUSES = {"queued", "running", "finalizing", "orphaned"}

    def __init__(self, paths: AppPaths, defects: DefectRepository, history: HistoryStore):
        self.paths = paths
        self.defects = defects
        self.history = history
        self._lock = threading.Lock()
        self._jobs: dict[str, dict[str, Any]] = {}
        self._active_job_id: str | None = None
        self._load_jobs()

    def _job_state_path(self, job_id: str) -> Path:
        return self.paths.runtime_jobs / job_id / REPAIR_JOB_STATE_FILE

    def _persist_job_locked(self, job: dict[str, Any]) -> None:
        state = {
            key: value
            for key, value in job.items()
            if key not in {"process", "progress"}
        }
        state["state_schema_version"] = 1
        _write_json(self._job_state_path(str(job["id"])), state)

    def _load_jobs(self) -> None:
        if not self.paths.runtime_jobs.is_dir():
            return
        for state_path in self.paths.runtime_jobs.glob(f"*/{REPAIR_JOB_STATE_FILE}"):
            state = _read_json(state_path)
            if not isinstance(state, dict):
                continue
            job_id = str(state.get("id") or "")
            if not SAFE_SEGMENT.fullmatch(job_id) or state_path.parent.name != job_id:
                continue
            if state.get("status") in self.ACTIVE_STATUSES:
                pid = state.get("process_pid")
                identity = str(state.get("process_identity") or "") or None
                process_stopped = not _process_is_alive(pid) or _terminate_pid_tree(
                    pid,
                    expected_identity=identity,
                )
                state["finished_at"] = _now()
                state["current_node"] = None
                state["cancel_requested"] = False
                state["workflow_status"] = "interrupted"
                state["execution_status"] = "ERROR"
                if str(state.get("verdict") or "PENDING").upper() == "PENDING":
                    state["verdict"] = "CANNOT_VERIFY"
                if process_stopped:
                    state["status"] = "interrupted"
                    state["reason_code"] = "SERVICE_RESTART"
                    state["interruption_reason"] = "前端服务重启，原修复进程已结束"
                else:
                    state["status"] = "orphaned"
                    state["reason_code"] = "ORPHAN_PROCESS"
                    state["interruption_reason"] = (
                        "前端服务重启后仍检测到无法安全确认或停止的原修复进程"
                    )
                    self._active_job_id = job_id
                state["error"] = state["interruption_reason"]
                _write_json(state_path, state)
            self._jobs[job_id] = state

    def start(self, *, defect: str, project: str = DEFAULT_TEST_PROJECT) -> dict[str, Any]:
        if self.defects.get(defect) is None:
            raise ValueError("缺陷不存在")
        project_meta = _test_project(project)

        with self._lock:
            if self._active_job_id:
                active = self._jobs.get(self._active_job_id, {})
                if active.get("status") in self.ACTIVE_STATUSES:
                    raise RuntimeError(f"已有修复任务 {self._active_job_id} 正在运行")

            job_id = uuid.uuid4().hex[:12]
            job = {
                "id": job_id,
                "defect": defect,
                **{key: project_meta[key] for key in (
                    "project", "project_label", "execution_target", "execution_target_label",
                    "case_map_profile",
                )},
                "execution_mode": "agent_generated",
                "status": "queued",
                "current_node": None,
                "nodes": {node: "pending" for node in WORKFLOW_NODES},
                "verdict": "PENDING",
                "workflow_status": "queued",
                "execution_status": "PENDING",
                "evidence_status": "PENDING",
                "mapping_status": "NOT_APPLICABLE",
                "reason_code": None,
                "created_at": _now(),
                "started_at": None,
                "finished_at": None,
                "history_id": None,
                "error": None,
                "cancel_requested": False,
            }
            self._jobs[job_id] = job
            self._active_job_id = job_id
            self._persist_job_locked(job)

        thread = threading.Thread(target=self._run, args=(job_id,), daemon=True, name=f"repair-{job_id}")
        try:
            thread.start()
        except BaseException:
            with self._lock:
                job = self._jobs[job_id]
                job["status"] = "failed"
                job["workflow_status"] = "failed"
                job["execution_status"] = "ERROR"
                job["reason_code"] = "THREAD_START_FAILED"
                job["error"] = "修复线程启动失败"
                job["finished_at"] = _now()
                if self._active_job_id == job_id:
                    self._active_job_id = None
                self._persist_job_locked(job)
            raise
        return self.get(job_id) or job

    def cancel(self, job_id: str) -> dict[str, Any]:
        job_id = _safe_segment(job_id, "任务编号")
        process: Any = None
        orphan_pid: int | None = None
        orphan_identity: str | None = None
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise ValueError("修复任务不存在")
            if job.get("status") == "finalizing":
                raise ValueError("修复执行已结束，正在保存历史记录，不能再取消")
            if job.get("status") not in self.ACTIVE_STATUSES:
                raise ValueError("修复任务已经结束")
            job["cancel_requested"] = True
            job["reason_code"] = "USER_CANCELLED"
            job["interruption_reason"] = "用户取消修复任务"
            process = job.get("process")
            if job.get("status") == "orphaned":
                orphan_pid = job.get("process_pid")
                orphan_identity = str(job.get("process_identity") or "") or None
            self._persist_job_locked(job)

        stopped = True
        if process is not None:
            stopped = _terminate_process_tree(process)
        elif orphan_pid is not None:
            stopped = _terminate_pid_tree(
                orphan_pid,
                expected_identity=orphan_identity,
            )

        if orphan_pid is not None:
            with self._lock:
                job = self._jobs[job_id]
                if stopped:
                    job["status"] = "cancelled"
                    job["workflow_status"] = "cancelled"
                    job["execution_status"] = "ERROR"
                    if str(job.get("verdict") or "PENDING").upper() == "PENDING":
                        job["verdict"] = "CANNOT_VERIFY"
                    job["finished_at"] = _now()
                    job["current_node"] = None
                    if self._active_job_id == job_id:
                        self._active_job_id = None
                else:
                    job["cancel_requested"] = False
                    job["error"] = "无法确认原修复进程身份或停止进程，任务仍保持隔离"
                self._persist_job_locked(job)
            if not stopped:
                raise RuntimeError("无法安全停止重启前遗留的修复进程")
        return self.get(job_id) or {}

    def get(self, job_id: str) -> dict[str, Any] | None:
        job_id = _safe_segment(job_id, "任务编号")
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                state_path = self._job_state_path(job_id)
                loaded = _read_json(state_path)
                if isinstance(loaded, dict) and str(loaded.get("id") or "") == job_id:
                    job = loaded
                    self._jobs[job_id] = job
            if job is None:
                return None
            snapshot = {key: value for key, value in job.items() if key not in {"process", "progress"}}
            progress_file = Path(job["progress_file"]) if job.get("progress_file") else None
        progress = _read_json(progress_file) if progress_file and progress_file.is_file() else None
        if isinstance(progress, dict):
            snapshot.update(
                {
                    "current_node": progress.get("current_node"),
                    "nodes": progress.get("nodes", snapshot["nodes"]),
                    "verdict": progress.get("verdict", snapshot["verdict"]),
                    "workflow_status": progress.get(
                        "workflow_status", snapshot.get("workflow_status")
                    ),
                    "execution_status": progress.get(
                        "execution_status", snapshot.get("execution_status")
                    ),
                    "evidence_status": progress.get(
                        "evidence_status", snapshot.get("evidence_status")
                    ),
                    "mapping_status": progress.get(
                        "mapping_status", snapshot.get("mapping_status")
                    ),
                    "reason_code": progress.get(
                        "reason_code", snapshot.get("reason_code")
                    ),
                    "attempts": progress.get("attempts", 0),
                    "progress_updated_at": progress.get("updated_at"),
                }
            )
            if snapshot["status"] in {"queued", "running"}:
                snapshot["status"] = progress.get("status", snapshot["status"])
            if progress.get("error"):
                snapshot["error"] = progress["error"]
        return snapshot

    def active(self) -> dict[str, Any] | None:
        with self._lock:
            job_id = self._active_job_id
        if not job_id:
            return None
        job = self.get(job_id)
        if job and job.get("status") in self.ACTIVE_STATUSES:
            return job
        return None

    def _run(self, job_id: str) -> None:
        try:
            self._run_body(job_id)
        except BaseException as exc:
            with self._lock:
                job = self._jobs.get(job_id)
                if job is not None:
                    job["status"] = "failed"
                    job["workflow_status"] = "failed"
                    job["execution_status"] = "ERROR"
                    job["reason_code"] = "UNHANDLED_EXCEPTION"
                    job["error"] = f"{type(exc).__name__}: {exc}"
                    job["finished_at"] = _now()
                    job["current_node"] = None
                    self._persist_job_locked(job)
        finally:
            with self._lock:
                job = self._jobs.get(job_id)
                process = job.get("process") if job is not None else None
            _terminate_process_tree(process)
            with self._lock:
                job = self._jobs.get(job_id)
                if job is not None:
                    if job.get("status") in self.ACTIVE_STATUSES:
                        if job.get("cancel_requested"):
                            job["status"] = "cancelled"
                            job["workflow_status"] = "cancelled"
                            job["execution_status"] = "ERROR"
                            job["reason_code"] = "USER_CANCELLED"
                            if str(job.get("verdict") or "PENDING").upper() == "PENDING":
                                job["verdict"] = "CANNOT_VERIFY"
                        else:
                            job["status"] = "failed"
                            job["workflow_status"] = "failed"
                            job["execution_status"] = "ERROR"
                            job["reason_code"] = (
                                job.get("reason_code") or "UNHANDLED_EXCEPTION"
                            )
                            job["error"] = job.get("error") or "修复任务异常结束"
                        job["finished_at"] = job.get("finished_at") or _now()
                        job["current_node"] = None
                    job.pop("process", None)
                    if self._active_job_id == job_id:
                        self._active_job_id = None
                    self._persist_job_locked(job)

    def _run_body(self, job_id: str) -> None:
        with self._lock:
            job = self._jobs[job_id]
            if job.get("cancel_requested"):
                job["status"] = "cancelled"
                job["workflow_status"] = "cancelled"
                job["execution_status"] = "ERROR"
                job["reason_code"] = "USER_CANCELLED"
                job["verdict"] = "CANNOT_VERIFY"
                job["finished_at"] = _now()
                self._persist_job_locked(job)
                return
            project_meta = _test_project(str(job.get("project") or DEFAULT_TEST_PROJECT))
            for key in (
                "project", "project_label", "execution_target", "execution_target_label",
                "case_map_profile",
            ):
                job.setdefault(key, project_meta[key])
            job["status"] = "running"
            job["started_at"] = _now()
            job_dir = self.paths.runtime_jobs / job_id
            job_dir.mkdir(parents=True, exist_ok=True)
            progress_file = job_dir / "progress.json"
            result_file = job_dir / "result.json"
            job["progress_file"] = str(progress_file)
            job["result_file"] = str(result_file)
            self._persist_job_locked(job)

        argv = build_child_command(
            "agent",
            [
                "--defect",
                job["defect"],
                "--task-id",
                job["defect"],
                "--project",
                job["project"],
                "--profile",
                job["case_map_profile"],
                "--target",
                job["execution_target"],
                "--progress-file",
                str(progress_file),
                "--result-file",
                str(result_file),
            ],
        )
        stdout = ""
        stderr = ""
        return_code: int | None = None
        result: dict[str, Any] = {}
        process: Any = None
        timed_out = False
        try:
            process = subprocess.Popen(
                argv,
                cwd=self.paths.root,
                env=_test_process_environment(project_meta),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            with self._lock:
                job["process"] = process
                job["process_pid"] = getattr(process, "pid", None)
                job["process_identity"] = _process_identity(job["process_pid"])
                self._persist_job_locked(job)
            try:
                stdout, stderr = _communicate_process(
                    process,
                    REPAIR_JOB_TIMEOUT_SECONDS,
                )
                return_code = getattr(process, "returncode", None)
            except subprocess.TimeoutExpired:
                timed_out = True
                _terminate_process_tree(process)
                try:
                    stdout, stderr = _communicate_process(
                        process,
                        PROCESS_TERMINATION_GRACE_SECONDS,
                    )
                except (OSError, subprocess.SubprocessError):
                    stdout, stderr = "", ""
                return_code = getattr(process, "returncode", None)
                stderr = (
                    f"{stderr}\n修复进程执行超时（上限 {REPAIR_JOB_TIMEOUT_SECONDS:g}s）"
                ).strip()
            loaded = _read_json(result_file, {})
            result = loaded if isinstance(loaded, dict) else {}
            if not result:
                result = {
                    "verdict": "CANNOT_VERIFY",
                    "attempts": 0,
                    "error": stderr.strip() or "修复进程未生成结果文件",
                    "workflow_status": "failed",
                    "execution_status": "ERROR",
                    "reason_code": "RESULT_MISSING",
                }
        except BaseException as exc:
            _terminate_process_tree(process)
            result = {
                "verdict": "CANNOT_VERIFY",
                "attempts": 0,
                "error": f"{type(exc).__name__}: {exc}",
                "workflow_status": "failed",
                "execution_status": "ERROR",
                "reason_code": "PROCESS_EXCEPTION",
            }
            stderr = (stderr + "\n" + result["error"]).strip()

        with self._lock:
            cancelled = bool(self._jobs[job_id].get("cancel_requested"))
        if cancelled:
            result = {
                **result,
                "verdict": "CANNOT_VERIFY",
                "error": "用户取消修复任务",
                "workflow_status": "cancelled",
                "execution_status": "ERROR",
                "reason_code": "USER_CANCELLED",
            }
        elif timed_out:
            result = {
                **result,
                "verdict": "CANNOT_VERIFY",
                "error": f"修复进程执行超时（上限 {REPAIR_JOB_TIMEOUT_SECONDS:g}s）",
                "workflow_status": "failed",
                "execution_status": "ERROR",
                "reason_code": "PROCESS_TIMEOUT",
            }

        if str(result.get("verdict") or "").upper() == "ERROR":
            result["verdict"] = "CANNOT_VERIFY"
            result.setdefault("workflow_status", "failed")
            result.setdefault("execution_status", "ERROR")
            result.setdefault("reason_code", "JUDGEMENT_ERROR")
        result.update(outcome_fields(
            result,
            workflow_default="failed" if result.get("error") else "completed",
        ))

        progress = _read_json(progress_file, {})
        with self._lock:
            job = self._jobs[job_id]
            job["finished_at"] = _now()
            job["return_code"] = return_code
            job["progress"] = progress if isinstance(progress, dict) else {}
            job["nodes"] = job["progress"].get("nodes", job["nodes"])
            job["verdict"] = result.get("verdict", "CANNOT_VERIFY")
            job["error"] = result.get("error")
            for key in (
                "workflow_status", "execution_status", "evidence_status",
                "mapping_status", "reason_code",
            ):
                job[key] = result.get(key)
            job["status"] = "finalizing"
            self._persist_job_locked(job)

        try:
            history_id = self.history.create(
                defect=job["defect"],
                job=job,
                result=result,
                stdout=stdout,
                stderr=stderr,
            )
        except BaseException as exc:
            history_id = None
            with self._lock:
                job["status"] = "failed"
                job["error"] = f"历史记录保存失败: {exc}"
                job["verdict"] = "CANNOT_VERIFY"
                job["workflow_status"] = "failed"
                job["execution_status"] = "ERROR"
                job["evidence_status"] = "ERROR"
                job["reason_code"] = "HISTORY_WRITE_FAILED"

        with self._lock:
            job["history_id"] = history_id
            if history_id is not None:
                if cancelled:
                    job["status"] = "cancelled"
                elif result.get("workflow_status") in {"failed", "interrupted", "orphaned"}:
                    job["status"] = "failed"
                else:
                    job["status"] = "completed"
            if job["status"] == "failed":
                job["workflow_status"] = "failed"
            job.pop("process", None)
            if self._active_job_id == job_id:
                self._active_job_id = None
            self._persist_job_locked(job)


class WebApplication:
    def __init__(self, paths: AppPaths):
        self.paths = paths
        self.history = HistoryStore(paths)
        self.test_history = TestHistoryStore(paths)
        self.defects = DefectRepository(paths, self.history)
        self.cases = CaseMapRepository(paths, self.test_history)
        self.jobs = JobManager(paths, self.defects, self.history)
        self.test_jobs = CaseTestManager(paths, self.cases, self.test_history)
        self.ble_devices = BleDeviceManager(paths)
        self._execution_lock = threading.Lock()
        self.import_jobs: dict[str, dict[str, Any]] = {}
        self._import_lock = threading.Lock()

    def start_repair(
        self,
        *,
        defect: str,
        project: str = DEFAULT_TEST_PROJECT,
    ) -> dict[str, Any]:
        with self._execution_lock:
            if self.test_jobs.active():
                raise RuntimeError("已有 Agent 测试正在运行")
            return self.jobs.start(defect=defect, project=project)

    def start_case_test(
        self,
        *,
        sheet: str,
        case_id: str,
        project: str = DEFAULT_TEST_PROJECT,
    ) -> dict[str, Any]:
        with self._execution_lock:
            if self.jobs.active():
                raise RuntimeError("已有缺陷修复任务正在运行")
            return self.test_jobs.start(sheet=sheet, case_id=case_id, project=project)

    def start_candidate_replay(
        self,
        *,
        sheet: str,
        case_id: str,
        project: str,
        source_history: dict[str, Any],
    ) -> dict[str, Any]:
        with self._execution_lock:
            if self.jobs.active():
                raise RuntimeError("已有缺陷修复任务正在运行")
            return self.test_jobs.start(
                sheet=sheet,
                case_id=case_id,
                project=project,
                candidate_replay=True,
                promotion_source=source_history,
            )

    def start_batch_test(
        self,
        *,
        limit: int = 0,
        case_refs: list[dict[str, str]] | None = None,
        categories: set[str] | None = None,
        project: str = DEFAULT_TEST_PROJECT,
    ) -> dict[str, Any]:
        with self._execution_lock:
            if self.jobs.active():
                raise RuntimeError("已有缺陷修复任务正在运行")
            return self.test_jobs.start_batch(
                limit=limit,
                case_refs=case_refs,
                categories=categories,
                project=project,
            )

    def resume_batch_test(self, job_id: str) -> dict[str, Any]:
        with self._execution_lock:
            if self.jobs.active():
                raise RuntimeError("已有缺陷修复任务正在运行")
            return self.test_jobs.resume_batch(job_id)



# --- 0.4.0 增强业务辅助方法与协议实现 ---


_EXCEL_CHINESE_FONT_NAME = "宋体"
_EXCEL_LATIN_FONT_NAME = "Times New Roman"
_EXCEL_HEADER_FILL = openpyxl.styles.PatternFill("solid", fgColor="1F4E78")
_EXCEL_SUMMARY_FILL = openpyxl.styles.PatternFill("solid", fgColor="D9EAF7")
_EXCEL_ZEBRA_FILL = openpyxl.styles.PatternFill("solid", fgColor="F7F9FC")
_EXCEL_THIN_BORDER = openpyxl.styles.Border(
    left=openpyxl.styles.Side(style="thin", color="D9D9D9"),
    right=openpyxl.styles.Side(style="thin", color="D9D9D9"),
    top=openpyxl.styles.Side(style="thin", color="D9D9D9"),
    bottom=openpyxl.styles.Side(style="thin", color="D9D9D9"),
)
_EXCEL_HEADER_FONT = openpyxl.styles.Font(
    name=_EXCEL_CHINESE_FONT_NAME,
    size=11,
    bold=True,
    color="FFFFFF",
)
_EXCEL_SUMMARY_FONT = openpyxl.styles.Font(
    name=_EXCEL_CHINESE_FONT_NAME,
    size=11,
    bold=True,
)
_EXCEL_BODY_FONTS = {
    (True, False): openpyxl.styles.Font(name=_EXCEL_CHINESE_FONT_NAME, size=10),
    (True, True): openpyxl.styles.Font(name=_EXCEL_CHINESE_FONT_NAME, size=10, bold=True),
    (False, False): openpyxl.styles.Font(name=_EXCEL_LATIN_FONT_NAME, size=10),
    (False, True): openpyxl.styles.Font(name=_EXCEL_LATIN_FONT_NAME, size=10, bold=True),
}


def _excel_body_font(value: Any, *, bold: bool = False) -> openpyxl.styles.Font:
    """按单元格主要内容选择中文或西文字体。"""
    text = str(value or "")
    has_chinese = any(
        "\u3400" <= character <= "\u9fff" or "\uf900" <= character <= "\ufaff"
        for character in text
    )
    return _EXCEL_BODY_FONTS[(has_chinese, bold)]


def _excel_display_units(value: Any) -> int:
    """估算 Excel 中一段文本占用的半角字符宽度。"""
    return sum(2 if ord(character) > 127 else 1 for character in str(value or ""))


def _excel_wrapped_line_count(value: Any, column_width: float) -> int:
    usable_width = max(1, int(column_width) - 2)
    lines = str(value or "").splitlines() or [""]
    return sum(max(1, (_excel_display_units(line) + usable_width - 1) // usable_width) for line in lines)


def _excel_set_adaptive_row_heights(
    sheet: Any,
    *,
    widths: dict[str, float],
    wrap_columns: set[int],
    min_height: float = 22,
    max_height: float = 96,
) -> None:
    """给长文本留足显示空间，同时限制异常堆栈造成的超高行。"""
    for row_index in range(2, sheet.max_row + 1):
        wrapped_lines = 1
        for column_index in wrap_columns:
            column = openpyxl.utils.get_column_letter(column_index)
            wrapped_lines = max(
                wrapped_lines,
                _excel_wrapped_line_count(sheet.cell(row_index, column_index).value, widths[column]),
            )
        sheet.row_dimensions[row_index].height = min(
            max_height,
            max(min_height, 6 + 15 * wrapped_lines),
        )


def _excel_configure_sheet(
    sheet: Any,
    *,
    widths: dict[str, float],
    orientation: str,
    zoom: int,
    repeat_header: bool,
) -> None:
    sheet.sheet_view.showGridLines = False
    sheet.sheet_view.zoomScale = zoom
    for column, width in widths.items():
        sheet.column_dimensions[column].width = width
    sheet.page_setup.orientation = orientation
    sheet.page_setup.paperSize = sheet.PAPERSIZE_A4
    sheet.page_setup.fitToWidth = 1
    sheet.page_setup.fitToHeight = 0
    sheet.sheet_properties.pageSetUpPr.fitToPage = True
    sheet.page_margins = openpyxl.worksheet.page.PageMargins(
        left=0.3,
        right=0.3,
        top=0.5,
        bottom=0.5,
        header=0.2,
        footer=0.2,
    )
    if repeat_header:
        sheet.print_title_rows = "1:1"


def _excel_style_table(
    sheet: Any,
    *,
    widths: dict[str, float],
    wrap_columns: set[int],
    center_columns: set[int],
    orientation: str = "landscape",
    zoom: int = 90,
    max_row_height: float = 96,
) -> None:
    """为普通表格统一应用字体、边框、对齐、筛选和打印布局。"""
    _excel_configure_sheet(
        sheet,
        widths=widths,
        orientation=orientation,
        zoom=zoom,
        repeat_header=True,
    )
    sheet.freeze_panes = "A2"
    last_column = openpyxl.utils.get_column_letter(len(widths))
    sheet.auto_filter.ref = f"A1:{last_column}{max(sheet.max_row, 1)}"
    sheet.row_dimensions[1].height = 28

    for cell in sheet[1]:
        cell.fill = _EXCEL_HEADER_FILL
        cell.font = _EXCEL_HEADER_FONT
        cell.border = _EXCEL_THIN_BORDER
        cell.alignment = openpyxl.styles.Alignment(
            horizontal="center",
            vertical="center",
            wrap_text=True,
        )

    for row_index, row in enumerate(sheet.iter_rows(min_row=2), start=2):
        for column_index, cell in enumerate(row, start=1):
            cell.font = _excel_body_font(cell.value)
            cell.border = _EXCEL_THIN_BORDER
            if row_index % 2 == 0:
                cell.fill = _EXCEL_ZEBRA_FILL
            if column_index in wrap_columns:
                cell.alignment = openpyxl.styles.Alignment(vertical="top", wrap_text=True)
            elif column_index in center_columns:
                cell.alignment = openpyxl.styles.Alignment(horizontal="center", vertical="center")
            else:
                cell.alignment = openpyxl.styles.Alignment(vertical="center")

    _excel_set_adaptive_row_heights(
        sheet,
        widths=widths,
        wrap_columns=wrap_columns,
        max_height=max_row_height,
    )


def _export_cases_xlsx(paths: AppPaths, project: str) -> bytes:
    """生成标准 9 列表头的 Excel 测试用例工作簿。"""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "自动化测试用例_v1"
    headers = ["模块/Sheet", "用例编号", "优先级", "前置条件", "测试步骤", "预期结果", "不可自动化", "固化状态", "备注"]
    ws.append(headers)
    
    project_meta = _test_project(project)
    case_map_root = paths.case_map / project_meta["case_map_dir"]
    if case_map_root.is_dir():
        for path in sorted(case_map_root.glob("*.json"), key=lambda p: p.stem):
            for item in _case_entries(paths, path.stem, project):
                case_id = str(item.get("case_id") or "").strip()
                if not case_id:
                    continue
                ws.append([
                    str(item.get("sheet") or path.stem),
                    case_id,
                    str(item.get("priority") or ""),
                    str(item.get("precondition_text") or ""),
                    str(item.get("steps_text") or ""),
                    str(item.get("expected_text") or ""),
                    "是" if item.get("unable") else "否",
                    str(item.get("mapping_status") or ""),
                    str(item.get("note") or ""),
                ])

    _excel_style_table(
        ws,
        widths={
            "A": 18, "B": 16, "C": 10, "D": 28, "E": 46,
            "F": 46, "G": 12, "H": 14, "I": 30,
        },
        wrap_columns={4, 5, 6, 9},
        center_columns={1, 2, 3, 7, 8},
    )

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _parse_excel_cases(file_base64: str) -> list[dict[str, Any]]:
    """解析 base64 编码的 Excel 用例表格（支持单 Sheet 或多 Sheet 工作簿）。"""
    try:
        data = base64.b64decode(file_base64)
    except Exception as exc:
        raise ValueError(f"Base64 数据解码失败: {exc}")
    try:
        wb = openpyxl.load_workbook(io.BytesIO(data), data_only=True)
    except Exception as exc:
        raise ValueError(f"Excel 文件无法解析: {exc}")
        
    target_sheets = ["自动化测试用例_v1"] if "自动化测试用例_v1" in wb.sheetnames else wb.sheetnames
    parsed_cases: list[dict[str, Any]] = []

    for sheet_name in target_sheets:
        ws = wb[sheet_name]
        rows = list(ws.iter_rows(values_only=True))
        if not rows:
            continue
            
        header_idx = -1
        col_map: dict[str, int] = {}
        for idx, row in enumerate(rows[:10]):
            if not row:
                continue
            row_str = [str(c).strip() if c is not None else "" for c in row]
            temp_map = {}
            for c_idx, cell in enumerate(row_str):
                if not cell:
                    continue
                if any(k in cell for k in ["模块", "Sheet", "sheet"]):
                    temp_map["sheet"] = c_idx
                elif any(k in cell for k in ["用例编号", "用例ID", "case_id", "编号", "用例标识"]):
                    temp_map["case_id"] = c_idx
                elif any(k in cell for k in ["优先级", "priority"]):
                    temp_map["priority"] = c_idx
                elif any(k in cell for k in ["前置条件", "前置", "precondition"]):
                    temp_map["precondition_text"] = c_idx
                elif any(k in cell for k in ["操作步骤", "测试步骤", "步骤", "steps"]):
                    temp_map["steps_text"] = c_idx
                elif any(k in cell for k in ["预期结果", "预期", "expected"]):
                    temp_map["expected_text"] = c_idx
                elif any(k in cell for k in ["不可自动化", "unable"]):
                    temp_map["unable"] = c_idx
                elif any(k in cell for k in ["固化状态", "mapping_status"]):
                    temp_map["mapping_status"] = c_idx
                elif any(k in cell for k in ["备注", "note"]):
                    temp_map["note"] = c_idx
            if "case_id" in temp_map or "expected_text" in temp_map:
                header_idx = idx
                col_map = temp_map
                break
                
        if header_idx == -1 or "case_id" not in col_map:
            continue
            
        for row_num, row in enumerate(rows[header_idx + 1:], start=header_idx + 2):
            if not row or all(c is None or str(c).strip() == "" for c in row):
                continue
            case_id = str(row[col_map["case_id"]]).strip() if "case_id" in col_map and col_map["case_id"] < len(row) and row[col_map["case_id"]] is not None else ""
            sheet = str(row[col_map["sheet"]]).strip() if "sheet" in col_map and col_map["sheet"] < len(row) and row[col_map["sheet"]] is not None else sheet_name
            if not case_id:
                continue
            
            priority = str(row[col_map["priority"]]).strip() if "priority" in col_map and col_map["priority"] < len(row) and row[col_map["priority"]] is not None else "P1"
            precondition = str(row[col_map["precondition_text"]]).strip() if "precondition_text" in col_map and col_map["precondition_text"] < len(row) and row[col_map["precondition_text"]] is not None else ""
            steps = str(row[col_map["steps_text"]]).strip() if "steps_text" in col_map and col_map["steps_text"] < len(row) and row[col_map["steps_text"]] is not None else ""
            expected = str(row[col_map["expected_text"]]).strip() if "expected_text" in col_map and col_map["expected_text"] < len(row) and row[col_map["expected_text"]] is not None else ""
            note = str(row[col_map["note"]]).strip() if "note" in col_map and col_map["note"] < len(row) and row[col_map["note"]] is not None else ""
            unable_raw = str(row[col_map["unable"]]).strip() if "unable" in col_map and col_map["unable"] < len(row) and row[col_map["unable"]] is not None else ""
            unable = unable_raw in {"是", "true", "True", "1", "Y", "yes"}
            mapping_status = str(row[col_map["mapping_status"]]).strip() if "mapping_status" in col_map and col_map["mapping_status"] < len(row) and row[col_map["mapping_status"]] is not None else ""
            
            parsed_cases.append({
                "case_id": case_id,
                "sheet": sheet,
                "priority": priority,
                "precondition_text": precondition,
                "steps_text": steps,
                "expected_text": expected,
                "verification_points": [expected] if expected else [],
                "setup": [],
                "actions": [],
                "collect": ["srv_quick_cmd send TOP5STEP:GUI_TREE:1;"],
                "unable": unable,
                "mapping_status": mapping_status or ("PROMOTED" if unable else "UNKNOWN"),
                "note": note,
                "_row_number": row_num,
            })
            
    if not parsed_cases:
        raise ValueError("未识别到有效的用例数据（需包含'用例编号'表头且有内容）")
    return parsed_cases


def _get_system_config(paths: AppPaths) -> dict[str, Any]:
    """读取当前运行时系统配置。"""
    from agent_loop_system.tools.llm_config import get_llm_config
    from agent_loop_system.tools.llm_retry import get_llm_runtime_status

    llm_cfg = get_llm_config()
    llm_runtime = get_llm_runtime_status(paths.root)
    llm_configured = bool(
        llm_cfg["api_key"] and llm_cfg["base_url"] and llm_cfg["model"]
    )
    runtime_paths = RuntimePaths.from_root(paths.root)
    simulator_root = runtime_paths.firmware_workspaces / "620C_W6830"
    return {
        "llm": {
            "provider": "environment",
            "api_key": (llm_cfg["api_key"][:3] + "..." + llm_cfg["api_key"][-4:]) if llm_cfg["api_key"] else "",
            "base_url": llm_cfg["base_url"],
            "model": llm_cfg["model"],
            "timeout": int(llm_cfg["timeout"]),
            "is_builtin": llm_cfg["is_builtin"],
            "configured": llm_configured,
            "status": "configured" if llm_configured else "unconfigured",
            "last_actual_success_at": llm_runtime["last_actual_success_at"],
        },
        "ones": {
            "base_url": os.environ.get("ONES_BASE_URL", "https://ones.topstepht.com:8443"),
            "auth_token": os.environ.get("ONES_AUTH_TOKEN", ""),
            "team_uuid": os.environ.get("ONES_TEAM_UUID", ""),
            "user_id": os.environ.get("ONES_USER_ID", ""),
        },
        "hardware": {
            "port": os.environ.get("W30_HARDWARE_PORT", ""),
            "baudrate": int(os.environ.get("W30_HARDWARE_BAUDRATE", 1500000)),
            "transport": os.environ.get("W30_HARDWARE_TRANSPORT", "supercom"),
            "capture_provider": os.environ.get("W30_HARDWARE_CAPTURE_PROVIDER", "mtp"),
            "ble_address": os.environ.get("W30_HARDWARE_BLE_ADDRESS", ""),
            "ble_scan_timeout": float(
                os.environ.get("W30_HARDWARE_BLE_SCAN_TIMEOUT", "15")
            ),
            "profile_root": str(resolve_config_path(
                os.environ.get(
                    "W30_HARDWARE_PROFILE_ROOT",
                    str(paths.root / "profiles"),
                ),
                app_root=paths.root,
            )),
            "profile_version": os.environ.get("W30_HARDWARE_PROFILE_VERSION", ""),
        },
        "simulator": {
            "source_root": str(resolve_config_path(
                os.environ.get("W30_SIMULATOR_SOURCE_ROOT", simulator_root),
                app_root=paths.root,
            )),
            "workspace_root": str(resolve_config_path(
                os.environ.get("W30_SIMULATOR_WORKSPACE_ROOT", simulator_root),
                app_root=paths.root,
            )),
            "simulator_path": str(resolve_config_path(
                os.environ.get(
                    "W30_SIMULATOR_PATH",
                    simulator_root / "core" / "gui" / "simulator" / "bin" / "main.exe",
                ),
                app_root=paths.root,
            )),
        },
    }


def _config_path_for_storage(value: Any, *, app_root: Path) -> str:
    """Keep paths inside the unified layout portable in the local .env."""

    raw = str(value or "").strip()
    if not raw:
        return ""
    resolved = resolve_config_path(raw, app_root=app_root)
    layout_root = RuntimePaths.from_root(app_root).layout_root.resolve()
    try:
        resolved.relative_to(layout_root)
    except ValueError:
        return str(resolved)
    return os.path.relpath(resolved, app_root)


def _save_system_config(paths: AppPaths, cfg: dict[str, Any]) -> None:
    """更新运行时环境变量并同步写入本地 .env。"""
    env_updates: dict[str, str] = {}
    if "llm" in cfg and isinstance(cfg["llm"], dict):
        llm = cfg["llm"]
        if "api_key" in llm and llm["api_key"] is not None:
            env_updates["OPENAI_API_KEY"] = str(llm["api_key"])
        if "base_url" in llm and llm["base_url"] is not None:
            env_updates["OPENAI_BASE_URL"] = str(llm["base_url"])
        if "model" in llm and llm["model"] is not None:
            env_updates["OPENAI_MODEL"] = str(llm["model"])
        if "timeout" in llm and llm["timeout"] is not None:
            env_updates["OPENAI_TIMEOUT"] = str(llm["timeout"])
            
    if "ones" in cfg and isinstance(cfg["ones"], dict):
        ones = cfg["ones"]
        if "base_url" in ones and ones["base_url"] is not None:
            env_updates["ONES_BASE_URL"] = str(ones["base_url"])
        if "auth_token" in ones and ones["auth_token"] is not None:
            env_updates["ONES_AUTH_TOKEN"] = str(ones["auth_token"])
        if "team_uuid" in ones and ones["team_uuid"] is not None:
            env_updates["ONES_TEAM_UUID"] = str(ones["team_uuid"])
        if "user_id" in ones and ones["user_id"] is not None:
            env_updates["ONES_USER_ID"] = str(ones["user_id"])
            
    if "hardware" in cfg and isinstance(cfg["hardware"], dict):
        hw = cfg["hardware"]
        if "port" in hw and hw["port"] is not None:
            env_updates["W30_HARDWARE_PORT"] = str(hw["port"])
        if "baudrate" in hw and hw["baudrate"] is not None:
            env_updates["W30_HARDWARE_BAUDRATE"] = str(hw["baudrate"])
        if "transport" in hw and hw["transport"] is not None:
            env_updates["W30_HARDWARE_TRANSPORT"] = str(hw["transport"])
        if "capture_provider" in hw and hw["capture_provider"] is not None:
            env_updates["W30_HARDWARE_CAPTURE_PROVIDER"] = str(hw["capture_provider"])
        if "ble_address" in hw and hw["ble_address"] is not None:
            env_updates["W30_HARDWARE_BLE_ADDRESS"] = str(hw["ble_address"])
        if "ble_scan_timeout" in hw and hw["ble_scan_timeout"] is not None:
            env_updates["W30_HARDWARE_BLE_SCAN_TIMEOUT"] = str(hw["ble_scan_timeout"])
        if "profile_root" in hw and hw["profile_root"] is not None:
            env_updates["W30_HARDWARE_PROFILE_ROOT"] = _config_path_for_storage(
                hw["profile_root"], app_root=paths.root
            )
        if "profile_version" in hw and hw["profile_version"] is not None:
            env_updates["W30_HARDWARE_PROFILE_VERSION"] = str(hw["profile_version"])
            
    if "simulator" in cfg and isinstance(cfg["simulator"], dict):
        sim = cfg["simulator"]
        if "source_root" in sim and sim["source_root"] is not None:
            env_updates["W30_SIMULATOR_SOURCE_ROOT"] = _config_path_for_storage(
                sim["source_root"], app_root=paths.root
            )
        if "workspace_root" in sim and sim["workspace_root"] is not None:
            env_updates["W30_SIMULATOR_WORKSPACE_ROOT"] = _config_path_for_storage(
                sim["workspace_root"], app_root=paths.root
            )
        if "simulator_path" in sim and sim["simulator_path"] is not None:
            env_updates["W30_SIMULATOR_PATH"] = _config_path_for_storage(
                sim["simulator_path"], app_root=paths.root
            )

    if "simulator_6202" in cfg and isinstance(cfg["simulator_6202"], dict):
        simulator_6202 = cfg["simulator_6202"]
        for field, environment_key in (
            ("source_root", "W30_6202_SIMULATOR_SOURCE_ROOT"),
            ("build_directory", "W30_6202_SIMULATOR_BUILD_DIRECTORY"),
            ("artifact_path", "W30_6202_SIMULATOR_ARTIFACT_PATH"),
        ):
            if field in simulator_6202 and simulator_6202[field] is not None:
                env_updates[environment_key] = _config_path_for_storage(
                    simulator_6202[field], app_root=paths.root
                )
            
    for k, v in env_updates.items():
        os.environ[k] = v
        
    env_file = paths.root / ".env"
    lines = []
    if env_file.is_file():
        lines = env_file.read_text(encoding="utf-8", errors="replace").splitlines()
        
    existing_keys = set()
    new_lines = []
    for line in lines:
        if "=" in line and not line.strip().startswith("#"):
            k = line.split("=", 1)[0].strip()
            existing_keys.add(k)
            if k in env_updates:
                new_lines.append(f"{k}={env_updates[k]}")
            else:
                new_lines.append(line)
        else:
            new_lines.append(line)
            
    for k, v in env_updates.items():
        if k not in existing_keys:
            new_lines.append(f"{k}={v}")
            
    env_file.write_text("\n".join(new_lines) + "\n", encoding="utf-8")


class BleDeviceManager:
    """On-demand BLE discovery, real connection checks, and local device memory."""

    def __init__(self, paths: AppPaths):
        self.paths = paths
        self._operation_lock = threading.Lock()
        self._store_lock = threading.Lock()

    @property
    def store_path(self) -> Path:
        return self.paths.root / ".runtime" / BLE_DEVICE_STORE_FILE

    @staticmethod
    def _timeout(value: Any) -> float:
        if isinstance(value, bool):
            raise ValueError("BLE 超时必须是 1 到 60 秒之间的数字")
        try:
            timeout = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("BLE 超时必须是 1 到 60 秒之间的数字") from exc
        if not math.isfinite(timeout) or not 1 <= timeout <= 60:
            raise ValueError("BLE 超时必须是 1 到 60 秒之间的数字")
        return timeout

    @staticmethod
    def _address(value: Any) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("BLE 设备地址不能为空")
        address = value.strip()
        if len(address) > 200:
            raise ValueError("BLE 设备地址过长")
        return address

    @staticmethod
    def _name(value: Any) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError("BLE 设备名称必须是字符串")
        name = value.strip()
        if len(name) > 200:
            raise ValueError("BLE 设备名称过长")
        return name or None

    @staticmethod
    def _address_key(address: str) -> str:
        return address.strip().casefold()

    def _load_unlocked(self) -> list[dict[str, Any]]:
        raw = _read_json(self.store_path, {})
        values = raw.get("items", []) if isinstance(raw, dict) else []
        if not isinstance(values, list):
            return []
        items: dict[str, dict[str, Any]] = {}
        for value in values:
            if not isinstance(value, dict):
                continue
            address = value.get("address")
            if not isinstance(address, str) or not address.strip():
                continue
            clean_address = address.strip()
            key = self._address_key(clean_address)
            items[key] = {
                "address": clean_address,
                "name": str(value.get("name") or "").strip() or None,
                "first_connected_at": str(value.get("first_connected_at") or ""),
                "last_connected_at": str(value.get("last_connected_at") or ""),
            }
        return sorted(
            items.values(),
            key=lambda item: str(item.get("last_connected_at") or ""),
            reverse=True,
        )

    def _write_unlocked(self, items: list[dict[str, Any]]) -> None:
        _write_json(
            self.store_path,
            {"schema_version": 1, "items": items},
        )

    def remembered(self) -> dict[str, Any]:
        with self._store_lock:
            items = self._load_unlocked()
        selected_address = os.environ.get("W30_HARDWARE_BLE_ADDRESS", "").strip()
        selected_key = self._address_key(selected_address)
        return {
            "items": [
                {
                    **item,
                    "status": "verified",
                    "connected": False,
                    "selected": bool(
                        selected_key
                        and self._address_key(str(item["address"])) == selected_key
                    ),
                }
                for item in items
            ],
            "selected_address": selected_address,
            "connection_mode": "on_demand",
        }

    def scan(self, *, query: str = "", timeout: Any = 15) -> dict[str, Any]:
        query = str(query or "").strip()
        if len(query) > 200:
            raise ValueError("BLE 搜索内容过长")
        scan_timeout = self._timeout(timeout)
        address_hint = (
            query
            if re.fullmatch(r"(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}", query)
            else None
        )
        with self._operation_lock:
            devices = asyncio.run(
                scan_watches(timeout=scan_timeout, address=address_hint)
            )
        items = [
            {
                "address": device.address,
                "name": device.name,
                "rssi": device.rssi,
                "status": "discovered",
                "connected": False,
            }
            for device in devices
        ]
        if query:
            needle = query.casefold()
            items = [
                item
                for item in items
                if needle in str(item["address"]).casefold()
                or needle in str(item.get("name") or "").casefold()
            ]
        return {"items": items, "scanned_at": _now(), "query": query}

    @staticmethod
    async def _verify_connection(
        *,
        address: str,
        name: str | None,
        timeout: float,
    ) -> None:
        client = WatchBleClient(
            WatchBleDevice(address=address, name=name),
            timeout=timeout,
        )
        try:
            await asyncio.wait_for(client.connect(pair=False), timeout=timeout)
            if not client.connected:
                raise WatchBleConnectionError("BLE GATT 连接未进入已连接状态")
        finally:
            try:
                await asyncio.wait_for(client.close(), timeout=min(timeout, 5.0))
            except Exception:
                # The explicit connection probe must not leave cleanup failures unobserved
                # by holding the HTTP request forever. WatchBleClient.close is itself
                # idempotent and best-effort.
                pass

    def connect(
        self,
        *,
        address: Any,
        name: Any = None,
        timeout: Any = 15,
    ) -> dict[str, Any]:
        clean_address = self._address(address)
        clean_name = self._name(name)
        connect_timeout = self._timeout(timeout)
        with self._operation_lock:
            asyncio.run(
                self._verify_connection(
                    address=clean_address,
                    name=clean_name,
                    timeout=connect_timeout,
                )
            )

        connected_at = _now()
        key = self._address_key(clean_address)
        with self._store_lock:
            items = self._load_unlocked()
            existing = next(
                (
                    item
                    for item in items
                    if self._address_key(str(item["address"])) == key
                ),
                None,
            )
            item = {
                "address": clean_address,
                "name": clean_name or (existing or {}).get("name"),
                "first_connected_at": (existing or {}).get("first_connected_at")
                or connected_at,
                "last_connected_at": connected_at,
            }
            remaining = [
                value
                for value in items
                if self._address_key(str(value["address"])) != key
            ]
            self._write_unlocked([item, *remaining])

        # A successful explicit connection also selects the device for later
        # on-demand BLE screenshots. No persistent GATT connection is kept.
        _save_system_config(
            self.paths,
            {"hardware": {"ble_address": clean_address}},
        )
        return {
            **item,
            "status": "verified",
            "verified": True,
            "connected": False,
            "selected": True,
            "connection_mode": "on_demand",
        }

    def forget(self, address: Any) -> dict[str, Any]:
        clean_address = self._address(address)
        key = self._address_key(clean_address)
        with self._store_lock:
            items = self._load_unlocked()
            remaining = [
                item
                for item in items
                if self._address_key(str(item["address"])) != key
            ]
            deleted = len(remaining) != len(items)
            if deleted:
                self._write_unlocked(remaining)

        selected_address = os.environ.get("W30_HARDWARE_BLE_ADDRESS", "").strip()
        if selected_address and self._address_key(selected_address) == key:
            _save_system_config(self.paths, {"hardware": {"ble_address": ""}})
            selected_address = ""
        return {"deleted": deleted, "selected_address": selected_address}


def _get_environments_status(paths: AppPaths) -> list[dict[str, Any]]:
    """生成全量测试目标的环境就绪状态与检查清单。"""
    items = []
    cfg = _get_system_config(paths)
    for proj_key, proj_meta in TEST_PROJECTS.items():
        checks = []
        is_hardware = proj_meta["execution_target"] == "hardware"
        
        # 1. 普通真机运行只校验版本绑定档案；模拟器仍校验工程工作区。
        if is_hardware:
            try:
                from agent_loop_system.tools.hardware_runtime_profile import (
                    load_hardware_runtime_profile,
                )

                runtime_profile = load_hardware_runtime_profile(
                    project=proj_key,
                    profiles_root=cfg["hardware"]["profile_root"],
                    version=cfg["hardware"]["profile_version"] or None,
                )
            except Exception as exc:
                status = "error"
                detail = f"运行时档案不可用: {exc}"
            else:
                status = "pass"
                detail = (
                    f"档案 {runtime_profile.version} · 固件 "
                    f"{runtime_profile.firmware_version} · {runtime_profile.project}"
                )
            checks.append({
                "key": "profile",
                "label": "真机运行时档案",
                "status": status,
                "detail": detail,
            })
        elif proj_key == "620C_W6830":
            src_p = Path(cfg["simulator"]["source_root"])
            status = "pass" if src_p.is_dir() else "warning"
            detail = f"工作区就绪: {src_p}" if status == "pass" else f"工作区目录不存在: {src_p}"
            checks.append({"key": "source", "label": "源码与工作区", "status": status, "detail": detail})
        else:
            src_p = Path(proj_meta.get("simulator_source_root", cfg["simulator"]["source_root"]))
            status = "pass" if src_p.is_dir() else "warning"
            detail = f"工作区就绪: {src_p}" if status == "pass" else f"工作区目录不存在: {src_p}"
            checks.append({"key": "source", "label": "源码与工作区", "status": status, "detail": detail})
        
        # 2. 项目配置
        case_map_p = paths.case_map / proj_meta["case_map_dir"]
        c_status = "pass" if case_map_p.is_dir() else "warning"
        c_detail = f"用例库已加载 ({len(list(case_map_p.glob('*.json')))} 个模块)" if c_status == "pass" else "用例库目录未找到"
        checks.append({"key": "config", "label": "项目配置", "status": c_status, "detail": c_detail})
        
        # 3. 执行产物
        if is_hardware:
            port = cfg["hardware"]["port"]
            checks.append({"key": "artifact", "label": "执行产物", "status": "pass", "detail": f"真机调试端口: {port}"})
        else:
            art_p = Path(proj_meta.get("simulator_artifact_path", cfg["simulator"]["simulator_path"]))
            a_status = "pass" if art_p.is_file() else "warning"
            a_detail = f"模拟器产物就绪: {art_p.name}" if a_status == "pass" else f"产物尚未生成: {art_p}"
            checks.append({"key": "artifact", "label": "执行产物", "status": a_status, "detail": a_detail})
            
        # 4. 命令接口
        cmd_label = "SuperCom 命名管道" if is_hardware else "QuickCmd 协议接口"
        checks.append({"key": "command", "label": "命令接口", "status": "pass", "detail": f"{cmd_label} 已启用"})
        
        # 5. 截图能力
        cap_label = "Windows MTP 传输" if is_hardware else "模拟器宿主窗口捕获"
        checks.append({"key": "capture", "label": "截图能力", "status": "pass", "detail": f"{cap_label} 已配置"})
        
        # 6. 大模型服务
        llm_ready = bool(cfg["llm"]["api_key"] and cfg["llm"]["base_url"])
        checks.append({
            "key": "llm",
            "label": "大模型服务",
            "status": "pass" if llm_ready else "warning",
            "detail": f"模型 {cfg['llm']['model']} (已配置)" if llm_ready else "未配置 OPENAI_API_KEY",
        })
        
        has_error = any(c["status"] == "error" for c in checks)
        has_warning = any(c["status"] == "warning" for c in checks)
        overall_status = "error" if has_error else ("partial" if has_warning else "ready")
        
        items.append({
            "id": proj_key,
            "project": proj_key,
            "project_label": proj_meta["project_label"],
            "execution_target": proj_meta["execution_target"],
            "execution_target_label": proj_meta["execution_target_label"],
            "status": overall_status,
            "readiness_status": overall_status,
            "last_checked_at": _now(),
            "checks": checks,
            "logs": [{"at": _now(), "message": f"{proj_meta['project_label']} 环境自检完成，状态: {overall_status}"}],
        })
    return items


REPORT_ABNORMAL_HEADERS = (
    "运行时间", "模块", "用例编号", "优先级", "结果", "异常类别",
    "前置条件", "测试步骤", "预期结果", "原因摘要", "原始判定/错误详情",
    "批次编号", "运行记录编号", "执行命令", "截图证据数量",
)
REPORT_VERDICT_LABELS = {
    "FAIL": "产品失败",
    "ERROR": "执行异常",
    "CANNOT_VERIFY": "无法验证",
}
EXCEL_ILLEGAL_CELL_CHARACTERS = re.compile(r"[\x00-\x08\x0B\x0C\x0E-\x1F]")


def _report_cell_text(value: Any, *, limit: int = 32_000) -> str:
    text = EXCEL_ILLEGAL_CELL_CHARACTERS.sub("", str(value or "").strip())
    if len(text) <= limit:
        return text
    return text[:limit - 1] + "…"


def _report_run_reason(run: dict[str, Any]) -> str:
    detail = _report_cell_text(run.get("reason") or run.get("error"))
    if detail:
        return detail
    errors: list[str] = []
    for field in ("setup_errors", "action_errors", "collect_errors"):
        values = run.get(field)
        if isinstance(values, list):
            errors.extend(str(value).strip() for value in values if str(value).strip())
        elif str(values or "").strip():
            errors.append(str(values).strip())
    if errors:
        return _report_cell_text("\n".join(errors))
    return _report_cell_text(run.get("stderr")) or "未记录失败原因"


def _report_reason_summary(detail: str) -> str:
    lines = [line.strip() for line in detail.splitlines() if line.strip()]
    if not lines:
        return "未记录失败原因"
    if detail.lstrip().startswith("Traceback"):
        summary = lines[-1]
    else:
        summary = " ".join(lines)
    return _report_cell_text(summary, limit=500)


def _report_command_trace_text(run: dict[str, Any]) -> str:
    trace = run.get("command_trace")
    if not isinstance(trace, list):
        return ""
    lines: list[str] = []
    for item in trace:
        if isinstance(item, dict):
            command = str(item.get("command") or item.get("wire") or "").strip()
            if not command:
                continue
            index = item.get("index") or len(lines) + 1
            metadata = "/".join(
                value for value in (
                    str(item.get("phase") or "").strip(),
                    str(item.get("status") or "").strip(),
                ) if value
            )
            suffix = f" [{metadata}]" if metadata else ""
            lines.append(f"{index}. {command}{suffix}")
        elif str(item or "").strip():
            lines.append(f"{len(lines) + 1}. {str(item).strip()}")
    return _report_cell_text("\n".join(lines))


def _report_abnormal_run(
    run: dict[str, Any],
    *,
    verdict: str,
    sheet: str,
    timestamp: str,
) -> dict[str, Any]:
    detail = _report_run_reason(run)
    screenshots = run.get("screenshots")
    workflow_status = str(run.get("workflow_status") or "").casefold()
    execution_status = str(run.get("execution_status") or "").upper()
    reason_code = str(run.get("reason_code") or "").upper()
    execution_anomaly = reason_code != "USER_CANCELLED" and (
        execution_status == "ERROR"
        or workflow_status in {"failed", "interrupted", "orphaned"}
    )
    return {
        "timestamp": timestamp,
        "sheet": sheet,
        "case_id": _report_cell_text(run.get("case_id")),
        "priority": _report_cell_text(run.get("priority")),
        "verdict": verdict,
        "verdict_label": (
            "执行异常" if execution_anomaly else REPORT_VERDICT_LABELS[verdict]
        ),
        "precondition_text": _report_cell_text(run.get("precondition_text")),
        "steps_text": _report_cell_text(run.get("steps_text")),
        "expected_text": _report_cell_text(run.get("expected_text")),
        "reason_summary": _report_reason_summary(detail),
        "reason_detail": detail,
        "batch_id": _report_cell_text(run.get("batch_id")),
        "run_id": _report_cell_text(run.get("id")),
        "commands": _report_command_trace_text(run),
        "screenshot_count": len(screenshots) if isinstance(screenshots, list) else 0,
    }


def _report_excel_datetime(value: Any) -> datetime | str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone().replace(tzinfo=None)
    return parsed


def _report_timestamp_in_range(
    value: Any,
    date_from: str | None,
    date_to: str | None,
    period: str | None = None,
    *,
    now: datetime | None = None,
) -> bool:
    text = str(value or "").strip()
    if period == "24h":
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return False
        local_now = now or datetime.now().astimezone()
        if local_now.tzinfo is None:
            local_now = local_now.astimezone()
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=local_now.tzinfo)
        else:
            parsed = parsed.astimezone(local_now.tzinfo)
        return local_now - timedelta(hours=24) <= parsed <= local_now

    date_str = text[:10] if len(text) >= 10 else ""
    if date_from and date_str and date_str < date_from:
        return False
    if date_to and date_str and date_str > date_to:
        return False
    return True


def _style_report_overview_sheets(
    summary_sheet: Any,
    module_sheet: Any,
) -> None:
    _excel_configure_sheet(
        summary_sheet,
        widths={"A": 22, "B": 34},
        orientation="portrait",
        zoom=100,
        repeat_header=False,
    )
    for row in summary_sheet.iter_rows(min_row=1, max_col=2):
        summary_sheet.row_dimensions[row[0].row].height = 24
        for cell in row:
            cell.font = _excel_body_font(cell.value)
            cell.border = _EXCEL_THIN_BORDER
            cell.alignment = openpyxl.styles.Alignment(vertical="center", wrap_text=True)
        row[0].fill = _EXCEL_SUMMARY_FILL
        row[0].font = _EXCEL_SUMMARY_FONT

    _excel_style_table(
        module_sheet,
        widths={"A": 22, "B": 14, "C": 14, "D": 14},
        wrap_columns={1},
        center_columns={2, 3, 4},
        orientation="portrait",
        zoom=100,
    )


def _add_report_abnormal_sheet(
    workbook: openpyxl.Workbook,
    abnormal_runs: list[dict[str, Any]],
) -> None:
    sheet = workbook.create_sheet(title="异常用例明细")
    sheet.append(list(REPORT_ABNORMAL_HEADERS))
    for item in abnormal_runs:
        sheet.append([
            _report_excel_datetime(item.get("timestamp")),
            item.get("sheet", ""),
            item.get("case_id", ""),
            item.get("priority", ""),
            item.get("verdict", ""),
            item.get("verdict_label", ""),
            item.get("precondition_text", ""),
            item.get("steps_text", ""),
            item.get("expected_text", ""),
            item.get("reason_summary", ""),
            item.get("reason_detail", ""),
            item.get("batch_id", ""),
            item.get("run_id", ""),
            item.get("commands", ""),
            item.get("screenshot_count", 0),
        ])

    widths = {
        "A": 20, "B": 14, "C": 16, "D": 10, "E": 14, "F": 14,
        "G": 24, "H": 32, "I": 32, "J": 42, "K": 60, "L": 18,
        "M": 24, "N": 60, "O": 14,
    }
    _excel_style_table(
        sheet,
        widths=widths,
        wrap_columns={2, 6, 7, 8, 9, 10, 11, 14},
        center_columns={1, 3, 4, 5, 12, 13, 15},
        max_row_height=120,
    )

    verdict_fills = {
        "FAIL": openpyxl.styles.PatternFill("solid", fgColor="FCE8E6"),
        "ERROR": openpyxl.styles.PatternFill("solid", fgColor="FEF3C7"),
        "CANNOT_VERIFY": openpyxl.styles.PatternFill("solid", fgColor="E5E7EB"),
    }
    for row in sheet.iter_rows(min_row=2):
        row[0].number_format = "yyyy-mm-dd hh:mm:ss"
        row[4].fill = verdict_fills.get(str(row[4].value or ""), verdict_fills["ERROR"])
        row[4].font = _excel_body_font(row[4].value, bold=True)


def _get_reports_summary_data(
    paths: AppPaths,
    history_store: TestHistoryStore,
    project: str,
    date_from: str | None,
    date_to: str | None,
    module_filter: str | None,
    period: str | None = None,
    *,
    include_abnormal_runs: bool = False,
) -> dict[str, Any]:
    """聚合计算测试报告总览、分布与趋势数据。"""
    project_meta = _test_project(project)
    project_name = project_meta["project"]
    proj_root = history_store._project_root(project_name)
    
    all_runs: list[dict[str, Any]] = []
    if proj_root.is_dir():
        for sheet_dir in proj_root.iterdir():
            if not sheet_dir.is_dir():
                continue
            if module_filter and sheet_dir.name != module_filter:
                continue
            for case_dir in sheet_dir.iterdir():
                if not case_dir.is_dir() or not SAFE_SEGMENT.fullmatch(case_dir.name):
                    continue
                for run_dir in case_dir.iterdir():
                    if not run_dir.is_dir() or not (run_dir / "run.json").is_file():
                        continue
                    run_data = _read_json(run_dir / "run.json")
                    if isinstance(run_data, dict):
                        all_runs.append(run_data)
                        
    # Filter by date or an explicit rolling period.
    filtered_runs = []
    for r in all_runs:
        ts = str(r.get("timestamp") or r.get("started_at") or "")
        if not _report_timestamp_in_range(ts, date_from, date_to, period):
            continue
        filtered_runs.append(r)
        
    dist = {"PASS": 0, "FAIL": 0, "ERROR": 0, "CANNOT_VERIFY": 0}
    by_date: dict[str, dict[str, int]] = {}
    module_fails: dict[str, dict[str, int]] = {}
    module_execution_errors: dict[str, dict[str, int]] = {}
    recent_fails: list[dict[str, Any]] = []
    abnormal_runs: list[dict[str, Any]] = []
    execution_error_count = 0
    
    for r in filtered_runs:
        v = str(r.get("verdict") or "ERROR").upper()
        if v == "SKIP":
            v = "CANNOT_VERIFY"
        if v not in dist:
            v = "ERROR"
        dist[v] += 1
        workflow_status = str(r.get("workflow_status") or "").casefold()
        execution_status = str(r.get("execution_status") or "").upper()
        reason_code = str(r.get("reason_code") or "").upper()
        is_execution_error = reason_code != "USER_CANCELLED" and (
            execution_status == "ERROR"
            or workflow_status in {"failed", "interrupted", "orphaned"}
        )
        if is_execution_error:
            execution_error_count += 1
        
        ts = str(r.get("timestamp") or r.get("started_at") or "")
        date_str = ts[:10] if len(ts) >= 10 else "未知"
        if date_str not in by_date:
            by_date[date_str] = {"pass": 0, "fail": 0, "error": 0, "cannot_verify": 0, "total": 0}
        by_date[date_str]["total"] += 1
        if v == "PASS":
            by_date[date_str]["pass"] += 1
        elif v == "FAIL":
            by_date[date_str]["fail"] += 1
        elif v == "ERROR":
            by_date[date_str]["error"] += 1
        else:
            by_date[date_str]["cannot_verify"] += 1
            
        sheet = str(r.get("sheet") or "通用")
        if include_abnormal_runs and v != "PASS":
            abnormal_runs.append(_report_abnormal_run(
                r,
                verdict=v,
                sheet=sheet,
                timestamp=ts,
            ))
        if v == "FAIL":
            if sheet not in module_fails:
                module_fails[sheet] = {"module": sheet, "fail": 0, "error": 0, "total": 0}
            module_fails[sheet]["fail"] += 1
            module_fails[sheet]["total"] += 1

        if is_execution_error:
            current = module_execution_errors.setdefault(
                sheet,
                {"module": sheet, "execution_error": 0},
            )
            current["execution_error"] += 1

        if v == "FAIL" or is_execution_error:
            recent_fails.append({
                "case_id": str(r.get("case_id") or ""),
                "sheet": sheet,
                "module": sheet,
                "history_id": str(r.get("id") or ""),
                "verdict": v,
                "workflow_status": workflow_status or None,
                "execution_status": execution_status or None,
                "reason_code": reason_code or None,
                "at": ts,
                "timestamp": ts,
                "message": str(r.get("reason") or r.get("error") or "测试未通过"),
            })
            
    trend = []
    for d_str in sorted(by_date.keys()):
        d_data = by_date[d_str]
        tot = d_data["total"]
        pass_rate = round(d_data["pass"] * 100.0 / tot, 1) if tot > 0 else 0.0
        trend.append({
            "date": d_str,
            "pass": d_data["pass"],
            "fail": d_data["fail"],
            "error": d_data["error"],
            "total": tot,
            "pass_rate": pass_rate,
        })
        
    top_fail_modules = sorted(module_fails.values(), key=lambda m: m["total"], reverse=True)[:8]
    top_execution_error_modules = sorted(
        module_execution_errors.values(),
        key=lambda item: item["execution_error"],
        reverse=True,
    )[:8]
    recent_fails.sort(key=lambda item: str(item.get("at") or ""), reverse=True)
    abnormal_runs.sort(key=lambda item: str(item.get("timestamp") or ""), reverse=True)
    
    total_count = len(filtered_runs)
    pass_rate = round(dist["PASS"] * 100.0 / total_count, 1) if total_count > 0 else 0.0
    
    report = {
        "metrics": {
            "total": total_count,
            "pass": dist["PASS"],
            "fail": dist["FAIL"],
            "error": dist["ERROR"],
            "cannot_verify": dist["CANNOT_VERIFY"],
            "execution_error": execution_error_count,
            "pass_rate": pass_rate,
            "batches": sum(1 for r in filtered_runs if r.get("batch_id")),
            "repairs": sum(1 for r in filtered_runs if r.get("execution_mode") == "agent_generated"),
        },
        "distribution": dist,
        "trend": trend,
        "top_fail_modules": top_fail_modules,
        "top_execution_error_modules": top_execution_error_modules,
        "recent_failures": recent_fails[:15],
        "insight": {
            "title": "测试稳定性与质量态势",
            "description": (
                f"已完成 {total_count} 次测试运行，综合通过率为 {pass_rate}%，"
                f"其中框架执行异常 {execution_error_count} 次。"
                + (
                    f" 建议优先关注高频产品失败模块: {top_fail_modules[0]['module']}。"
                    if top_fail_modules else " 当前未发现产品失败热点。"
                )
            ),
        },
    }
    if include_abnormal_runs:
        report["abnormal_runs"] = abnormal_runs
    return report


class RequestHandler(BaseHTTPRequestHandler):
    server_version = "W30AgentUI/0.4"
    app: WebApplication

    def log_message(self, format: str, *args: Any) -> None:
        timestamp = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")
        sys.stderr.write(f"[{timestamp}] {self.address_string()} {format % args}\n")

    def do_GET(self) -> None:
        try:
            self._get()
        except ValueError as exc:
            self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        except BaseException as exc:
            self._json({"error": f"服务器错误: {exc}"}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def do_POST(self) -> None:
        try:
            self._post()
        except ValueError as exc:
            self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        except RuntimeError as exc:
            self._json({"error": str(exc)}, HTTPStatus.CONFLICT)
        except BaseException as exc:
            self._json({"error": f"服务器错误: {exc}"}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def do_PUT(self) -> None:
        try:
            self._put()
        except ValueError as exc:
            self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        except BaseException as exc:
            self._json({"error": f"服务器错误: {exc}"}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def do_DELETE(self) -> None:
        try:
            self._delete()
        except ValueError as exc:
            self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        except BaseException as exc:
            self._json({"error": f"服务器错误: {exc}"}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def _get(self) -> None:
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        query = parse_qs(parsed.query)

        if path == "/api/system/heartbeat":
            global _last_heartbeat_time, _has_received_heartbeat
            _last_heartbeat_time = time.time()
            _has_received_heartbeat = True
            self._json({"status": "ok"})
            return

        if path == "/api/tests/projects":
            self._json({"items": _test_project_options()})
            return

        if path == "/api/tests/overview":
            project = query.get("project", [DEFAULT_TEST_PROJECT])[0]
            recent_limit = self._positive_int(query, "recent_limit", 7, maximum=20)
            exception_limit = self._positive_int(query, "exception_limit", 6, maximum=20)
            self._json(self.app.cases.overview(
                project=project,
                recent_limit=recent_limit,
                exception_limit=exception_limit,
            ))
            return

        if path == "/api/tests/recent":
            project = query.get("project", [DEFAULT_TEST_PROJECT])[0]
            limit = self._positive_int(query, "limit", 8, maximum=50)
            self._json(self.app.cases.recent(project=project, limit=limit))
            return

        if path == "/api/tests":
            page = self._positive_int(query, "page", 1)
            page_size = self._positive_int(query, "page_size", 20, maximum=100)
            keyword = query.get("q", [""])[0]
            state_filter = query.get("state", ["all"])[0]
            project = query.get("project", [DEFAULT_TEST_PROJECT])[0]
            modules = {
                str(value).strip() for value in query.get("module", [])
                if str(value).strip()
            }
            self._json(
                self.app.cases.list(
                    query=keyword,
                    page=page,
                    page_size=page_size,
                    state_filter=state_filter,
                    project=project,
                    modules=modules,
                )
            )
            return

        if path == "/api/tests/active":
            jobs = self.app.test_jobs.active_jobs()
            self._json({"job": jobs[0] if jobs else None, "jobs": jobs})
            return

        if path == "/api/tests/jobs":
            project = query.get("project", [DEFAULT_TEST_PROJECT])[0]
            status_filter = query.get("status", ["running"])[0]
            page = self._positive_int(query, "page", 1)
            page_size = self._positive_int(query, "page_size", 20, maximum=100)
            
            all_jobs_dict = dict(self.app.test_jobs._jobs)
            if self.app.paths.runtime_jobs.is_dir():
                for j_dir in self.app.paths.runtime_jobs.iterdir():
                    if j_dir.is_dir() and j_dir.name not in all_jobs_dict:
                        st = _read_json(j_dir / BATCH_STATE_FILE)
                        if isinstance(st, dict):
                            all_jobs_dict[j_dir.name] = st
                            
            items = []
            today_str = datetime.now().astimezone().strftime("%Y-%m-%d")
            completed_today = 0
            error_cnt = 0
            queued_cnt = 0
            
            for jid, j in all_jobs_dict.items():
                j_proj = str(j.get("project") or DEFAULT_TEST_PROJECT)
                j_status = str(j.get("status") or "completed")
                j_verdict = str(j.get("verdict") or "ERROR")
                j_workflow = str(j.get("workflow_status") or "").casefold()
                j_execution = str(j.get("execution_status") or "").upper()
                j_reason_code = str(j.get("reason_code") or "").upper()

                if project and j_proj != project and project != "all":
                    continue
                
                if j_status in {"queued", "running", "finalizing", "orphaned"}:
                    queued_cnt += 1
                if j_status in {"completed", "done"}:
                    fin = str(j.get("finished_at") or j.get("created_at") or "")
                    if fin.startswith(today_str):
                        completed_today += 1
                if j_reason_code != "USER_CANCELLED" and (
                    j_execution == "ERROR"
                    or j_workflow in {"failed", "interrupted", "orphaned"}
                    or j_status in {"failed", "interrupted", "orphaned"}
                    or int(j.get("execution_error_count") or 0) > 0
                ):
                    error_cnt += 1
                    
                if status_filter == "queue":
                    if j_status != "queued":
                        continue
                elif status_filter == "running":
                    if j_status not in {"running", "finalizing", "queued", "orphaned"}:
                        continue
                elif status_filter == "completed":
                    if j_status not in {"completed", "done"}:
                        continue
                elif status_filter == "interrupted":
                    if j_status not in {"cancelled", "interrupted", "failed", "orphaned"}:
                        continue
                        
                meta = _test_project(j_proj)
                items.append({
                    "id": jid,
                    "project": j_proj,
                    "project_label": meta["project_label"],
                    "status": j_status,
                    "verdict": j_verdict,
                    "workflow_status": j_workflow or None,
                    "execution_status": j_execution or None,
                    "reason_code": j_reason_code or None,
                    "completed": j.get("completed", 1 if j_status in {"completed", "done"} else 0),
                    "total": j.get("total", 1),
                    "started_at": j.get("started_at") or j.get("created_at"),
                    "finished_at": j.get("finished_at"),
                })
                
            items.sort(key=lambda x: str(x.get("finished_at") or x.get("started_at") or ""), reverse=True)
            total = len(items)
            offset = (page - 1) * page_size
            paged = items[offset:offset + page_size]
            
            self._json({
                "items": paged,
                "total": total,
                "page": page,
                "page_size": page_size,
                "summary": {
                    "queued": queued_cnt,
                    "completed_today": completed_today,
                    "error": error_cnt,
                }
            })
            return

        match = re.fullmatch(r"/api/tests/jobs/([^/]+)/screenshots/([^/]+)/([^/]+)", path)
        if match:
            self._serve_file(
                self.app.test_jobs.screenshot_path(
                    match.group(1), match.group(2), match.group(3)
                )
            )
            return

        match = re.fullmatch(r"/api/tests/jobs/([^/]+)", path)
        if match:
            job = self.app.test_jobs.get(match.group(1))
            self._json(job if job is not None else {"error": "测试任务不存在"}, HTTPStatus.OK if job else HTTPStatus.NOT_FOUND)
            return

        match = re.fullmatch(r"/api/tests/([^/]+)/([^/]+)", path)
        if match:
            project = query.get("project", [DEFAULT_TEST_PROJECT])[0]
            data = self.app.cases.get(match.group(1), match.group(2), project=project)
            self._json(data if data is not None else {"error": "测试用例不存在"}, HTTPStatus.OK if data else HTTPStatus.NOT_FOUND)
            return

        match = re.fullmatch(r"/api/test-history/([^/]+)/([^/]+)/([^/]+)/screenshot", path)
        if match:
            project = query.get("project", [DEFAULT_TEST_PROJECT])[0]
            self._serve_file(self.app.test_history._run_dir(
                match.group(1), match.group(2), match.group(3), project=project
            ) / "screenshot.bmp")
            return

        match = re.fullmatch(r"/api/test-history/([^/]+)/([^/]+)/([^/]+)/screenshot/([^/]+)", path)
        if match:
            project = query.get("project", [DEFAULT_TEST_PROJECT])[0]
            file_name = unquote(match.group(4))
            if not TEST_SCREENSHOT_FILE.fullmatch(file_name):
                self._json({"error": "截图文件名不合法"}, HTTPStatus.BAD_REQUEST)
                return
            self._serve_file(
                self.app.test_history._run_dir(
                    match.group(1), match.group(2), match.group(3), project=project
                ) / file_name
            )
            return

        match = re.fullmatch(r"/api/test-history/([^/]+)/([^/]+)/([^/]+)", path)
        if match:
            project = query.get("project", [DEFAULT_TEST_PROJECT])[0]
            data = self.app.test_history.get(
                match.group(1), match.group(2), match.group(3), project=project
            )
            self._json(data if data is not None else {"error": "测试记录不存在"}, HTTPStatus.OK if data else HTTPStatus.NOT_FOUND)
            return

        if path == "/api/cases/export":
            project = query.get("project", [DEFAULT_TEST_PROJECT])[0]
            data = _export_cases_xlsx(self.app.paths, project)
            self._serve_binary(data, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", f"test_cases_{project}.xlsx")
            return

        if path == "/api/cases/migration-candidates":
            source = query.get("source", ["6202_W5230_SIMULATOR"])[0]
            target = query.get("target", ["6202_W5230"])[0]
            src_cases = self.app.cases._all(project=source)
            tgt_cases = self.app.cases._all(project=target)
            tgt_map = {c["case_id"]: c for c in tgt_cases}
            
            candidates = []
            for c in src_cases:
                if not c.get("is_promoted") and c.get("mapping_status") != "PROMOTED":
                    continue
                cid = c["case_id"]
                sheet = c["sheet"]
                tgt = tgt_map.get(cid)
                if tgt is None:
                    candidates.append({
                        "case_id": cid,
                        "sheet": sheet,
                        "status": "TARGET_MISSING",
                        "divergence_reason": None,
                    })
                elif tgt.get("is_promoted") or tgt.get("mapping_status") == "PROMOTED":
                    candidates.append({
                        "case_id": cid,
                        "sheet": sheet,
                        "status": "ALREADY_PROMOTED",
                        "divergence_reason": None,
                    })
                elif c.get("expected_text", "").strip() != tgt.get("expected_text", "").strip():
                    candidates.append({
                        "case_id": cid,
                        "sheet": sheet,
                        "status": "DIVERGED",
                        "divergence_reason": f"两端预期文本分叉: 模拟器='{c.get('expected_text')}' vs 真机='{tgt.get('expected_text')}'",
                    })
                else:
                    candidates.append({
                        "case_id": cid,
                        "sheet": sheet,
                        "status": "READY",
                        "divergence_reason": None,
                    })
            self._json({"candidates": candidates})
            return

        if path == "/api/reports/summary":
            project = query.get("project", [DEFAULT_TEST_PROJECT])[0]
            d_from = query.get("from", [None])[0]
            d_to = query.get("to", [None])[0]
            module = query.get("module", [None])[0]
            period = query.get("period", [None])[0]
            summary_data = _get_reports_summary_data(
                self.app.paths,
                self.app.test_history,
                project,
                d_from,
                d_to,
                module,
                period,
            )
            self._json(summary_data)
            return

        if path == "/api/reports/runs":
            project = query.get("project", [DEFAULT_TEST_PROJECT])[0]
            scope = query.get("scope", ["batch"])[0]
            d_from = query.get("from", [None])[0]
            d_to = query.get("to", [None])[0]
            period = query.get("period", [None])[0]
            page = self._positive_int(query, "page", 1)
            page_size = self._positive_int(query, "page_size", 20, maximum=100)
            
            items = []
            if scope == "batch":
                if self.app.paths.runtime_jobs.is_dir():
                    for j_dir in self.app.paths.runtime_jobs.iterdir():
                        if not j_dir.is_dir():
                            continue
                        st = _read_json(j_dir / BATCH_STATE_FILE)
                        if isinstance(st, dict):
                            ts = str(st.get("started_at") or st.get("created_at") or "")
                            if not _report_timestamp_in_range(ts, d_from, d_to, period):
                                continue
                            meta = _test_project(str(st.get("project") or DEFAULT_TEST_PROJECT))
                            items.append({
                                "id": j_dir.name,
                                "batch_id": j_dir.name,
                                "project": meta["project"],
                                "project_label": meta["project_label"],
                                "status": st.get("status", "completed"),
                                "verdict": st.get("verdict", "PASS"),
                                "completed": st.get("completed", 0),
                                "total": st.get("total", 0),
                                "started_at": st.get("started_at"),
                                "finished_at": st.get("finished_at"),
                                "verdict_counts": st.get("verdict_counts", {}),
                            })
            else:
                proj_meta = _test_project(project)
                proj_root = self.app.test_history._project_root(proj_meta["project"])
                if proj_root.is_dir():
                    for s_dir in proj_root.iterdir():
                        if not s_dir.is_dir():
                            continue
                        for c_dir in s_dir.iterdir():
                            if not c_dir.is_dir():
                                continue
                            for r_dir in c_dir.iterdir():
                                if not r_dir.is_dir() or not (r_dir / "run.json").is_file():
                                    continue
                                r_json = _read_json(r_dir / "run.json")
                                if isinstance(r_json, dict):
                                    ts = str(r_json.get("timestamp") or r_json.get("started_at") or "")
                                    if not _report_timestamp_in_range(ts, d_from, d_to, period):
                                        continue
                                    items.append({
                                        "id": r_json.get("id", r_dir.name),
                                        "case_id": r_json.get("case_id", c_dir.name),
                                        "sheet": r_json.get("sheet", s_dir.name),
                                        "project": proj_meta["project"],
                                        "project_label": proj_meta["project_label"],
                                        "verdict": r_json.get("verdict", "ERROR"),
                                        "reason": r_json.get("reason", ""),
                                        "timestamp": ts,
                                    })
            items.sort(key=lambda x: str(x.get("started_at") or x.get("timestamp") or ""), reverse=True)
            total = len(items)
            offset = (page - 1) * page_size
            paged = items[offset:offset + page_size]
            self._json({"items": paged, "total": total, "page": page, "page_size": page_size})
            return

        if path == "/api/reports/export":
            project = query.get("project", [DEFAULT_TEST_PROJECT])[0]
            d_from = query.get("from", [None])[0]
            d_to = query.get("to", [None])[0]
            module = query.get("module", [None])[0]
            period = query.get("period", [None])[0]
            summary_data = _get_reports_summary_data(
                self.app.paths,
                self.app.test_history,
                project,
                d_from,
                d_to,
                module,
                period,
                include_abnormal_runs=True,
            )
            
            wb = openpyxl.Workbook()
            ws_summary = wb.active
            ws_summary.title = "测试报告概览"
            ws_summary.append(["测试项目", summary_data.get("project", project)])
            ws_summary.append(["统计周期", "最近24小时" if period == "24h" else f"{d_from or '全部'} 至 {d_to or '全部'}"])
            ws_summary.append(["总执行数", summary_data["metrics"]["total"]])
            ws_summary.append(["通过 (PASS)", summary_data["metrics"]["pass"]])
            ws_summary.append(["失败 (FAIL)", summary_data["metrics"]["fail"]])
            ws_summary.append(["错误 (ERROR)", summary_data["metrics"]["error"]])
            ws_summary.append(["无法验证", summary_data["metrics"]["cannot_verify"]])
            ws_summary.append(["框架执行异常", summary_data["metrics"]["execution_error"]])
            ws_summary.append(["通过率", f"{summary_data['metrics']['pass_rate']}%"])
            
            ws_fail = wb.create_sheet(title="高频失败模块")
            ws_fail.append(["模块名称", "FAIL 数量", "ERROR 数量", "异常合计"])
            for m in summary_data.get("top_fail_modules", []):
                ws_fail.append([m.get("module", ""), m.get("fail", 0), m.get("error", 0), m.get("total", 0)])

            _style_report_overview_sheets(ws_summary, ws_fail)
            _add_report_abnormal_sheet(wb, summary_data.get("abnormal_runs", []))
                 
            buf = io.BytesIO()
            wb.save(buf)
            data = buf.getvalue()
            self._serve_binary(data, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", f"test_report_{project}.xlsx")
            return

        if path == "/api/environments":
            envs = _get_environments_status(self.app.paths)
            self._json({"items": envs})
            return

        if path == "/api/config/test-llm":
            from agent_loop_system.tools.llm_config import test_llm_connectivity
            res = test_llm_connectivity()
            self._json(res, HTTPStatus.OK if res.get("ok") else HTTPStatus.BAD_GATEWAY)
            return

        if path == "/api/config":
            self._json(_get_system_config(self.app.paths))
            return

        if path == "/api/hardware/serial-ports":
            configured_port = os.environ.get("W30_HARDWARE_PORT", "")
            try:
                from agent_loop_system.tools.hardware_serial_ports import (
                    get_serial_ports_status,
                )
                payload = get_serial_ports_status(configured_port)
            except Exception as exc:
                payload = {
                    "configured_port": configured_port,
                    "selected_port": configured_port,
                    "default_port": None,
                    "active_count": 0,
                    "items": [],
                    "available": False,
                    "error": str(exc),
                }
            self._json(payload)
            return

        if path == "/api/hardware/ble/devices":
            query_text = query.get("q", [""])[0]
            timeout = query.get(
                "timeout",
                [os.environ.get("W30_HARDWARE_BLE_SCAN_TIMEOUT", "15")],
            )[0]
            try:
                result = self.app.ble_devices.scan(
                    query=query_text,
                    timeout=timeout,
                )
            except ValueError:
                raise
            except Exception as exc:
                self._json(
                    {"error": f"BLE 扫描失败: {exc}"},
                    HTTPStatus.BAD_GATEWAY,
                )
                return
            self._json(result)
            return

        if path == "/api/hardware/ble/remembered":
            self._json(self.app.ble_devices.remembered())
            return

        if path == "/api/update-check":
            cur_ver = get_current_system_version()
            try:
                update_info = check_for_updates(current_version=cur_ver)
                self._json(update_info)
            except Exception as exc:
                self._json({
                    "current_version": cur_ver,
                    "latest_version": cur_ver,
                    "has_update": False,
                    "update_available": False,
                    "changelog": [],
                    "error": str(exc),
                })
            return

        if path == "/api/defects":
            page = self._positive_int(query, "page", 1)
            page_size = self._positive_int(query, "page_size", 20, maximum=100)
            keyword = query.get("q", [""])[0]
            result_filter = query.get("result", ["all"])[0]
            self._json(
                self.app.defects.list(
                    query=keyword,
                    page=page,
                    page_size=page_size,
                    result_filter=result_filter,
                )
            )
            return

        match = re.fullmatch(r"/api/defects/import/([^/]+)", path)
        if match:
            job_id = match.group(1)
            with self.app._import_lock:
                job = dict(self.app.import_jobs.get(job_id) or {})
            if not job:
                self._json({"error": "导入任务不存在"}, HTTPStatus.NOT_FOUND)
                return
            log_file = Path(job["log_file"]) if job.get("log_file") else None
            stdout_tail = ""
            if log_file and log_file.is_file():
                stdout_tail = log_file.read_text(encoding="utf-8", errors="replace")[-3000:]
            job["stdout_tail"] = stdout_tail
            self._json(job)
            return

        match = re.fullmatch(r"/api/defects/([^/]+)/images/([^/]+)", path)
        if match:
            image_path = self.app.defects.defect_image_path(match.group(1), match.group(2))
            self._serve_file(image_path)
            return

        if path.startswith("/api/defects/"):
            number = path.removeprefix("/api/defects/")
            data = self.app.defects.get(number)
            self._json(data if data is not None else {"error": "缺陷不存在"}, HTTPStatus.OK if data else HTTPStatus.NOT_FOUND)
            return

        if path == "/api/cases":
            sheet = query.get("sheet", [""])[0]
            self._json({"sheet": sheet, "cases": self.app.defects.cases(sheet)})
            return

        match = re.fullmatch(r"/api/history/([^/]+)/([^/]+)/evidence/(before|after)", path)
        if match:
            self._serve_file(self.app.history._run_dir(match.group(1), match.group(2)) / f"{match.group(3)}.bmp")
            return

        match = re.fullmatch(r"/api/history/([^/]+)/([^/]+)", path)
        if match:
            data = self.app.history.get(match.group(1), match.group(2))
            self._json(data if data is not None else {"error": "历史记录不存在"}, HTTPStatus.OK if data else HTTPStatus.NOT_FOUND)
            return

        match = re.fullmatch(r"/api/history/([^/]+)", path)
        if match:
            self._json({"history": self.app.history.list(match.group(1))})
            return

        match = re.fullmatch(r"/api/evidence/([^/]+)/(before|after)", path)
        if match:
            number = _safe_segment(match.group(1), "缺陷编号")
            self._serve_file(self.app.paths.evidence / number / f"{match.group(2)}.bmp")
            return

        if path == "/api/run/active":
            self._json({"job": self.app.jobs.active()})
            return

        match = re.fullmatch(r"/api/run/([^/]+)", path)
        if match:
            job = self.app.jobs.get(match.group(1))
            self._json(job if job is not None else {"error": "任务不存在"}, HTTPStatus.OK if job else HTTPStatus.NOT_FOUND)
            return

        if path == "/assets/styles.css":
            self._serve_file(self.app.paths.frontend / "styles.css")
            return
        if path == "/assets/app.js":
            self._serve_file(self.app.paths.frontend / "app.js")
            return

        if (
            path in {"/", "/tests", "/overview", "/cases", "/runs", "/reports", "/defects", "/environments"}
            or re.fullmatch(r"/(defect|history)/[^/]+(?:/[^/]+)?", path)
            or re.fullmatch(r"/test/[^/]+/[^/]+", path)
            or re.fullmatch(r"/test-batch/[^/]+", path)
            or re.fullmatch(r"/test-history/[^/]+/[^/]+/[^/]+", path)
        ):
            self._serve_file(self.app.paths.frontend / "index.html")
            return

        self._json({"error": "页面不存在"}, HTTPStatus.NOT_FOUND)

    def _post(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/tests/run-batch":
            body = self._body_json()
            project = _test_project(str(body.get("project") or DEFAULT_TEST_PROJECT))["project"]
            limit = body.get("limit", 0)
            if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0 or limit > 5000:
                raise ValueError("limit 必须是 0 到 5000 的整数")
            raw_cases = body.get("cases")
            case_refs: list[dict[str, str]] | None = None
            if raw_cases is not None:
                if not isinstance(raw_cases, list) or not raw_cases or len(raw_cases) > 5000:
                    raise ValueError("cases 必须是 1 到 5000 条用例")
                case_refs = []
                for item in raw_cases:
                    if not isinstance(item, dict):
                        raise ValueError("cases 中每项必须包含 sheet 和 case_id")
                    sheet = item.get("sheet")
                    case_id = item.get("case_id")
                    if not isinstance(sheet, str) or not sheet.strip():
                        raise ValueError("cases.sheet 必填")
                    if not isinstance(case_id, str) or not case_id.strip():
                        raise ValueError("cases.case_id 必填")
                    case_refs.append({"sheet": sheet.strip(), "case_id": case_id.strip()})
            raw_categories = body.get("categories")
            categories: set[str] | None = None
            if raw_categories is not None:
                if not isinstance(raw_categories, list) or not raw_categories:
                    raise ValueError("categories 至少选择一项")
                if not all(isinstance(item, str) for item in raw_categories):
                    raise ValueError("categories 中每项必须是字符串")
                categories = {item.strip().lower() for item in raw_categories if item.strip()}
                unknown = categories - CaseMapRepository.BATCH_CATEGORIES
                if unknown:
                    raise ValueError(f"批次分类不合法: {', '.join(sorted(unknown))}")
                if not categories:
                    raise ValueError("categories 至少选择一项")
            if case_refs is not None and categories is not None:
                raise ValueError("cases 和 categories 不能同时使用")
            if case_refs is None and categories is None:
                job = self.app.start_batch_test(limit=limit, project=project)
            elif categories is not None:
                job = self.app.start_batch_test(
                    limit=limit, categories=categories, project=project
                )
            else:
                job = self.app.start_batch_test(
                    limit=limit, case_refs=case_refs, project=project
                )
            self._json(job, HTTPStatus.ACCEPTED)
            return

        match = re.fullmatch(r"/api/tests/jobs/([^/]+)/cancel", path)
        if match:
            self._json(self.app.test_jobs.cancel(match.group(1)), HTTPStatus.ACCEPTED)
            return

        match = re.fullmatch(r"/api/tests/jobs/([^/]+)/resume", path)
        if match:
            self._json(self.app.resume_batch_test(match.group(1)), HTTPStatus.ACCEPTED)
            return

        if path == "/api/tests/run":
            body = self._body_json()
            project = _test_project(str(body.get("project") or DEFAULT_TEST_PROJECT))["project"]
            sheet = body.get("sheet")
            case_id = body.get("case_id")
            if not isinstance(sheet, str) or not sheet.strip():
                raise ValueError("sheet 必填")
            if not isinstance(case_id, str) or not case_id.strip():
                raise ValueError("case_id 必填")
            job = self.app.start_case_test(
                sheet=sheet.strip(), case_id=case_id.strip(), project=project
            )
            self._json(job, HTTPStatus.ACCEPTED)
            return

        if path == "/api/cases/create":
            body = self._body_json()
            project = str(body.get("project") or DEFAULT_TEST_PROJECT)
            case_obj = body.get("case")
            if not isinstance(case_obj, dict):
                raise ValueError("case 字段必填且必须为对象")
            case_id = str(case_obj.get("case_id") or "").strip()
            sheet = str(case_obj.get("sheet") or "").strip()
            if not case_id:
                raise ValueError("用例编号 case_id 必填")
            if not sheet:
                raise ValueError("模块 sheet 必填")
                
            project_meta = _test_project(project)
            case_map_root = self.app.paths.case_map / project_meta["case_map_dir"]
            case_map_root.mkdir(parents=True, exist_ok=True)
            sheet_file = case_map_root / f"{sheet}.json"
            
            entries = _case_entries(self.app.paths, sheet, project=project)
            for item in entries:
                if isinstance(item, dict) and item.get("case_id") == case_id:
                    raise ValueError(f"用例编号 {case_id} 在模块 {sheet} 中已存在")
                    
            new_entry = {
                "case_id": case_id,
                "sheet": sheet,
                "priority": str(case_obj.get("priority") or "P1").strip(),
                "precondition_text": str(case_obj.get("precondition_text") or "").strip(),
                "steps_text": str(case_obj.get("steps_text") or "").strip(),
                "expected_text": str(case_obj.get("expected_text") or "").strip(),
                "verification_points": [str(case_obj.get("expected_text")).strip()] if case_obj.get("expected_text") else [],
                "setup": [],
                "actions": [],
                "collect": ["srv_quick_cmd send TOP5STEP:GUI_TREE:1;"],
                "unable": bool(case_obj.get("unable", False)),
                "mapping_status": str(case_obj.get("mapping_status") or ""),
                "note": str(case_obj.get("note") or "").strip(),
            }
            
            raw = _read_json(sheet_file, [])
            if isinstance(raw, dict) and "cases" in raw:
                raw["cases"].append(new_entry)
                _write_json(sheet_file, raw)
            else:
                if not isinstance(raw, list):
                    raw = []
                raw.append(new_entry)
                _write_json(sheet_file, raw)
                
            self._json({"status": "ok", "case_id": case_id})
            return

        if path == "/api/cases/update":
            body = self._body_json()
            project = str(body.get("project") or DEFAULT_TEST_PROJECT)
            orig_case_id = str(body.get("orig_case_id") or "").strip()
            case_obj = body.get("case")
            if not isinstance(case_obj, dict):
                raise ValueError("case 字段必填且必须为对象")
            case_id = str(case_obj.get("case_id") or orig_case_id).strip()
            sheet = str(case_obj.get("sheet") or "").strip()
            if not orig_case_id:
                raise ValueError("原用例编号 orig_case_id 必填")
            if not sheet:
                raise ValueError("模块 sheet 必填")
                
            project_meta = _test_project(project)
            case_map_root = self.app.paths.case_map / project_meta["case_map_dir"]
            sheet_file = case_map_root / f"{sheet}.json"
            if not sheet_file.is_file():
                raise ValueError(f"模块文件 {sheet}.json 不存在")
                
            raw = _read_json(sheet_file, [])
            case_list = raw.get("cases") if isinstance(raw, dict) and isinstance(raw.get("cases"), list) else (raw if isinstance(raw, list) else [])
            
            target_idx = None
            for idx, item in enumerate(case_list):
                if isinstance(item, dict) and item.get("case_id") == orig_case_id:
                    target_idx = idx
                    break
            if target_idx is None:
                raise ValueError(f"未找到原用例 {orig_case_id}")
                
            if case_id != orig_case_id:
                for idx, item in enumerate(case_list):
                    if idx != target_idx and isinstance(item, dict) and item.get("case_id") == case_id:
                        raise ValueError(f"目标编号 {case_id} 已被其他用例占用")
                        
            item = case_list[target_idx]
            item["case_id"] = case_id
            item["sheet"] = sheet
            if "priority" in case_obj:
                item["priority"] = str(case_obj["priority"]).strip()
            if "precondition_text" in case_obj:
                item["precondition_text"] = str(case_obj["precondition_text"]).strip()
            if "steps_text" in case_obj:
                item["steps_text"] = str(case_obj["steps_text"]).strip()
            if "expected_text" in case_obj:
                item["expected_text"] = str(case_obj["expected_text"]).strip()
                if not item.get("verification_points") or item["verification_points"] == [item.get("expected_text")]:
                    item["verification_points"] = [item["expected_text"]] if item["expected_text"] else []
            if "note" in case_obj:
                item["note"] = str(case_obj["note"]).strip()
            if "unable" in case_obj:
                item["unable"] = bool(case_obj["unable"])
                
            _write_json(sheet_file, raw)
            self._json({"status": "ok", "case_id": case_id})
            return

        if path == "/api/cases/migrate":
            body = self._body_json()
            case_id = str(body.get("case_id") or "").strip()
            sheet = str(body.get("sheet") or "").strip()
            src_prof = str(body.get("source_profile") or "6202_W5230_SIMULATOR")
            tgt_prof = str(body.get("target_profile") or "6202_W5230")
            if not case_id or not sheet:
                raise ValueError("case_id 与 sheet 必填")
                
            tgt_meta = _test_project(tgt_prof)
            src_cases = _case_entries(self.app.paths, sheet, project=src_prof)
            src_case = next((c for c in src_cases if isinstance(c, dict) and c.get("case_id") == case_id), None)
            if not src_case:
                raise ValueError(f"在源项目 {src_prof} 中未找到用例 {case_id}")
                
            tgt_root = self.app.paths.case_map / tgt_meta["case_map_dir"]
            tgt_root.mkdir(parents=True, exist_ok=True)
            tgt_sheet_f = tgt_root / f"{sheet}.json"
            tgt_raw = _read_json(tgt_sheet_f, [])
            is_envelope = isinstance(tgt_raw, dict) and "cases" in tgt_raw
            tgt_list = tgt_raw.get("cases") if is_envelope else (tgt_raw if isinstance(tgt_raw, list) else [])
            tgt_existing = next((c for c in tgt_list if isinstance(c, dict) and c.get("case_id") == case_id), None)
            
            if tgt_existing is None:
                new_tgt_case = dict(src_case)
                new_tgt_case["mapping_status"] = ""
                tgt_list.append(new_tgt_case)
                if is_envelope:
                    tgt_raw["cases"] = tgt_list
                    _write_json(tgt_sheet_f, tgt_raw)
                else:
                    _write_json(tgt_sheet_f, tgt_list)
                    
            job = self.app.start_case_test(sheet=sheet, case_id=case_id, project=tgt_prof)
            self._json({"status": "ok", "job": job}, HTTPStatus.ACCEPTED)
            return

        if path == "/api/cases/audit-and-promote":
            body = self._body_json()
            case_id = str(body.get("case_id") or "").strip()
            sheet = str(body.get("sheet") or "").strip()
            project = str(body.get("project") or DEFAULT_TEST_PROJECT).strip()
            if not case_id or not sheet:
                raise ValueError("case_id 与 sheet 必填")

            current = self.app.cases.get(sheet, case_id, project=project)
            if current is None:
                raise ValueError("测试用例不存在")
            if current.get("is_promoted"):
                self._json({
                    "status": "already_promoted",
                    "audit": {"passed": True, "issues": []},
                })
                return

            latest_summary = self.app.test_history.latest(sheet, case_id, project=project)
            if not latest_summary:
                self._json({
                    "status": "failed",
                    "audit": {
                        "passed": False,
                        "issues": ["未找到自主探索历史，必须先运行该用例"],
                    },
                }, HTTPStatus.BAD_REQUEST)
                return
            latest_history = self.app.test_history.get(
                sheet,
                case_id,
                str(latest_summary["id"]),
                project=project,
            )
            if not latest_history:
                self._json({
                    "status": "failed",
                    "audit": {"passed": False, "issues": ["最新历史运行记录无法读取"]},
                }, HTTPStatus.BAD_REQUEST)
                return

            try:
                job = self.app.start_candidate_replay(
                    sheet=sheet,
                    case_id=case_id,
                    project=project,
                    source_history=latest_history,
                )
            except ValueError as exc:
                self._json({
                    "status": "failed",
                    "audit": {"passed": False, "issues": [str(exc)]},
                }, HTTPStatus.BAD_REQUEST)
                return
            self._json({
                "status": "candidate_replay_started",
                "audit": {
                    "passed": False,
                    "pending": True,
                    "issues": [],
                },
                "job": job,
            }, HTTPStatus.ACCEPTED)
            return

        if path == "/api/excel/preview":
            body = self._body_json()
            project = str(body.get("project") or DEFAULT_TEST_PROJECT)
            b64_data = str(body.get("file_base64") or "").strip()
            if not b64_data:
                raise ValueError("未提供 Excel 文件内容 (file_base64)")
                
            parsed = _parse_excel_cases(b64_data)
            existing_cases = {c["case_id"] for c in self.app.cases._all(project=project)}
            new_cases = [c for c in parsed if c["case_id"] not in existing_cases]
            existing_count = len(parsed) - len(new_cases)
            modules = sorted({c["sheet"] for c in parsed})
            
            self._json({
                "new_count": len(new_cases),
                "existing_count": existing_count,
                "total_parsed": len(parsed),
                "modules": modules,
                "new_cases": [
                    {"case_id": c["case_id"], "sheet": c["sheet"], "expected_text": c["expected_text"]}
                    for c in new_cases
                ]
            })
            return

        if path == "/api/excel/confirm":
            body = self._body_json()
            project = str(body.get("project") or DEFAULT_TEST_PROJECT)
            b64_data = str(body.get("file_base64") or "").strip()
            overwrite_existing = bool(body.get("overwrite_existing", False))
            if not b64_data:
                raise ValueError("未提供 Excel 文件内容 (file_base64)")
                
            parsed = _parse_excel_cases(b64_data)
            project_meta = _test_project(project)
            case_map_root = self.app.paths.case_map / project_meta["case_map_dir"]
            case_map_root.mkdir(parents=True, exist_ok=True)
            
            by_sheet: dict[str, list[dict[str, Any]]] = {}
            for c in parsed:
                by_sheet.setdefault(c["sheet"], []).append(c)
                
            imported_count = 0
            overwritten_count = 0
            skipped_count = 0
            modules_updated = set()
            
            for sheet_name, sheet_cases in by_sheet.items():
                sheet_file = case_map_root / f"{sheet_name}.json"
                raw = _read_json(sheet_file, [])
                is_dict_envelope = isinstance(raw, dict) and "cases" in raw
                existing_list = raw.get("cases") if is_dict_envelope else (raw if isinstance(raw, list) else [])
                existing_map = {item["case_id"]: item for item in existing_list if isinstance(item, dict) and item.get("case_id")}
                
                updated = False
                for c in sheet_cases:
                    cid = c["case_id"]
                    clean_c = {k: v for k, v in c.items() if not k.startswith("_")}
                    if cid in existing_map:
                        if overwrite_existing:
                            existing_map[cid].update(clean_c)
                            overwritten_count += 1
                            updated = True
                        else:
                            skipped_count += 1
                    else:
                        existing_list.append(clean_c)
                        existing_map[cid] = clean_c
                        imported_count += 1
                        updated = True
                        
                if updated:
                    modules_updated.add(sheet_name)
                    if is_dict_envelope:
                        raw["cases"] = existing_list
                        _write_json(sheet_file, raw)
                    else:
                        _write_json(sheet_file, existing_list)
                        
            self._json({
                "imported_count": imported_count,
                "overwritten_count": overwritten_count,
                "skipped_count": skipped_count,
                "modules_updated": sorted(modules_updated),
            })
            return

        match = re.fullmatch(r"/api/environments/([^/]+)/check", path)
        if match:
            proj = match.group(1)
            envs = _get_environments_status(self.app.paths)
            target_env = next((e for e in envs if e["id"] == proj or e["project"] == proj), envs[0] if envs else {})
            self._json({"status": "ok", "result": target_env})
            return

        if path == "/api/config/test-llm":
            from agent_loop_system.tools.llm_config import test_llm_connectivity
            res = test_llm_connectivity()
            self._json(res, HTTPStatus.OK if res.get("ok") else HTTPStatus.BAD_GATEWAY)
            return

        if path == "/api/config":
            body = self._body_json()
            _save_system_config(self.app.paths, body)
            self._json({"status": "ok", "message": "系统设置已保存并生效"})
            return

        if path == "/api/hardware/ble/connect":
            body = self._body_json()
            try:
                device = self.app.ble_devices.connect(
                    address=body.get("address"),
                    name=body.get("name"),
                    timeout=body.get(
                        "timeout",
                        os.environ.get("W30_HARDWARE_BLE_SCAN_TIMEOUT", "15"),
                    ),
                )
            except ValueError:
                raise
            except Exception as exc:
                self._json(
                    {
                        "ok": False,
                        "verified": False,
                        "connected": False,
                        "connection_mode": "on_demand",
                        "error": f"BLE 连接验证失败: {exc}",
                    },
                    HTTPStatus.BAD_GATEWAY,
                )
                return
            self._json(
                {
                    "ok": True,
                    "verified": True,
                    "connected": False,
                    "connection_mode": "on_demand",
                    "device": device,
                    "message": "真实 BLE 连接验证成功；连接已释放，后续按需使用",
                }
            )
            return

        if path == "/api/defects/import":
            body = self._body_json()
            include_completed = body.get("include_completed", False)
            force = body.get("force", False)
            limit = body.get("limit", 0)
            if not isinstance(include_completed, bool):
                raise ValueError("include_completed 必须是布尔值")
            if not isinstance(force, bool):
                raise ValueError("force 必须是布尔值")
            if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
                raise ValueError("limit 必须是大于或等于 0 的整数")
            job = self._start_import(
                include_completed=include_completed, limit=limit, force=force
            )
            self._json(job, HTTPStatus.ACCEPTED)
            return

        if path == "/api/system/upgrade":
            from agent_loop_system.tools.auto_updater import (
                AutoUpdaterError,
                launch_update_script,
                prepare_upgrade,
            )
            body = self._body_json() if self.headers.get("Content-Length") else {}
            manifest_source = str(body.get("manifest_source", "")).strip() or None
            try:
                upgrade_info = prepare_upgrade(self.app.paths.root, manifest_source=manifest_source)
                staging_dir = Path(upgrade_info["staging_dir"])
                launch_update_script(
                    self.app.paths.root,
                    staging_dir,
                    parent_pid=os.getpid(),
                    target_version=upgrade_info["target_version"],
                )
                self._json({
                    "status": "upgrading",
                    "target_version": upgrade_info["target_version"],
                    "message": f"新版本 v{upgrade_info['target_version']} 已就绪，系统正在平滑重启并完成升级...",
                })
            except AutoUpdaterError as exc:
                self._json({"error": exc.message, "error_code": exc.error_code}, HTTPStatus.BAD_REQUEST)
            except Exception as exc:
                self._json({"error": f"升级准备失败: {exc}"}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return

        if path == "/api/ones/login":
            body = self._body_json()
            email = str(body.get("email", "")).strip()
            password = str(body.get("password", "")).strip()
            base_url = (
                str(body.get("base_url", "")).strip()
                or os.environ.get("ONES_BASE_URL", "").strip()
                or "https://ones.topstepht.com:8443"
            )
            if not email or not password:
                raise ValueError("请提供 ONES 账号和密码")

            import urllib.request
            import urllib.error
            parsed_base_url = urlparse(base_url)
            if parsed_base_url.scheme not in {"http", "https"} or not parsed_base_url.hostname:
                raise ValueError("ONES 地址必须是有效的 http(s) URL")
            if parsed_base_url.scheme != "https" and not _is_loopback_host(
                parsed_base_url.hostname
            ):
                raise ValueError("ONES 登录包含密码，非本机地址必须使用 HTTPS")
            ctx = _ones_ssl_context()

            login_url = f"{base_url.rstrip('/')}/project/api/project/auth/login"
            req_data = json.dumps({"email": email, "password": password}).encode("utf-8")
            req = urllib.request.Request(
                login_url,
                data=req_data,
                headers={
                    "Content-Type": "application/json",
                    "Referer": base_url.rstrip("/"),
                    "User-Agent": "w30-agent-loop/0.1.0",
                },
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, context=ctx, timeout=15) as resp:
                    resp_data = json.loads(resp.read().decode("utf-8"))
                    self._json(resp_data)
            except urllib.error.HTTPError as exc:
                err_text = exc.read().decode("utf-8", errors="ignore")
                try:
                    err_json = json.loads(err_text)
                    msg = err_json.get("desc") or err_json.get("reason") or f"HTTP {exc.code}"
                except Exception:
                    msg = f"HTTP {exc.code}"
                raise ValueError(f"ONES 登录失败: {msg}")
            except Exception as exc:
                raise ValueError(f"连接 ONES 服务器失败: {exc}")
            return

        match = re.fullmatch(r"/api/run/([^/]+)/cancel", path)
        if match:
            self._json(self.app.jobs.cancel(match.group(1)), HTTPStatus.ACCEPTED)
            return

        if path != "/api/run":
            self._json({"error": "接口不存在"}, HTTPStatus.NOT_FOUND)
            return

        body = self._body_json()
        if not isinstance(body.get("defect"), str) or not body["defect"].strip():
            raise ValueError("defect 必填")
        project = _test_project(str(body.get("project") or DEFAULT_TEST_PROJECT))["project"]
        job = self.app.start_repair(
            defect=body["defect"].strip(),
            project=project,
        )
        self._json(job, HTTPStatus.ACCEPTED)

    def _put(self) -> None:
        path = urlparse(self.path).path
        match = re.fullmatch(r"/api/environments/([^/]+)", path)
        if match:
            project = match.group(1)
            body = self._body_json()
            paths_obj = body.get("paths", {})
            if isinstance(paths_obj, dict):
                config_update = {"simulator": {}}
                if project == "6202_W5230":
                    hardware_update: dict[str, Any] = {}
                    if "profile_root" in paths_obj:
                        hardware_update["profile_root"] = paths_obj["profile_root"]
                    if "profile_version" in paths_obj:
                        hardware_update["profile_version"] = paths_obj["profile_version"]
                    if hardware_update:
                        _save_system_config(
                            self.app.paths,
                            {"hardware": hardware_update},
                        )
                elif project == "6202_W5230_SIMULATOR":
                    simulator_6202_update = {
                        key: paths_obj[key]
                        for key in ("source_root", "build_directory", "artifact_path")
                        if key in paths_obj
                    }
                    if simulator_6202_update:
                        _save_system_config(
                            self.app.paths,
                            {"simulator_6202": simulator_6202_update},
                        )
                else:
                    if "source_root" in paths_obj:
                        config_update["simulator"]["source_root"] = paths_obj["source_root"]
                    if "workspace_root" in paths_obj:
                        config_update["simulator"]["workspace_root"] = paths_obj["workspace_root"]
                    if "artifact_path" in paths_obj:
                        config_update["simulator"]["simulator_path"] = paths_obj["artifact_path"]
                if config_update["simulator"]:
                    _save_system_config(self.app.paths, config_update)
            self._json({"status": "ok", "message": "环境配置已保存"})
            return
        self._json({"error": "接口不存在"}, HTTPStatus.NOT_FOUND)

    def _start_import(
        self, *, include_completed: bool, limit: int, force: bool
    ) -> dict[str, Any]:
        with self.app._import_lock:
            for j in self.app.import_jobs.values():
                if j.get("status") == "running":
                    raise RuntimeError(f"已有导入任务 {j['id']} 正在运行")
            job_id = uuid.uuid4().hex[:12]
            job_dir = self.app.paths.runtime_jobs / job_id
            job_dir.mkdir(parents=True, exist_ok=True)
            log_file = job_dir / "import.log"
            job: dict[str, Any] = {
                "id": job_id,
                "type": "import",
                "status": "running",
                "include_completed": include_completed,
                "limit": limit,
                "force": force,
                "created_at": _now(),
                "finished_at": None,
                "log_file": str(log_file),
                "error": None,
            }
            self.app.import_jobs[job_id] = job
        thread = threading.Thread(
            target=self._run_import,
            args=(job_id, include_completed, limit, force, str(log_file)),
            daemon=True,
            name=f"import-{job_id}",
        )
        thread.start()
        return job

    def _run_import(
        self,
        job_id: str,
        include_completed: bool,
        limit: int,
        force: bool,
        log_file: str,
    ) -> None:
        extra = ["--import-all"]
        if force:
            extra.append("--force")
        if include_completed:
            extra.append("--include-completed")
        if limit and limit > 0:
            extra.extend(["--limit", str(limit)])
        argv = build_child_command("defect-store", extra)
        try:
            with open(log_file, "w", encoding="utf-8") as f:
                process = subprocess.run(
                    argv,
                    cwd=self.app.paths.root,
                    stdout=f,
                    stderr=subprocess.STDOUT,
                    timeout=6 * 3600,
                )
            rc = process.returncode
        except Exception as exc:
            rc = -1
            with open(log_file, "a", encoding="utf-8") as f:
                f.write(f"\n[import] 异常: {exc}\n")
        with self.app._import_lock:
            job = self.app.import_jobs.get(job_id)
            if job:
                job["status"] = "done" if rc == 0 else "failed"
                job["finished_at"] = _now()
                job["error"] = None if rc == 0 else f"退出码 {rc}"

    @staticmethod
    def _positive_int(
        query: dict[str, list[str]],
        name: str,
        default: int,
        *,
        maximum: int | None = None,
    ) -> int:
        raw = query.get(name, [str(default)])[0]
        try:
            value = int(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{name} 必须是正整数") from exc
        if value < 1 or (maximum is not None and value > maximum):
            suffix = f"且不大于 {maximum}" if maximum is not None else ""
            raise ValueError(f"{name} 必须是正整数{suffix}")
        return value

    def _delete(self) -> None:
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        query = parse_qs(parsed.query)
        match = re.fullmatch(r"/api/hardware/ble/remembered/([^/]+)", path)
        if match:
            result = self.app.ble_devices.forget(match.group(1))
            self._json(
                result,
                HTTPStatus.OK if result["deleted"] else HTTPStatus.NOT_FOUND,
            )
            return
        match = re.fullmatch(r"/api/test-history/([^/]+)/([^/]+)/([^/]+)", path)
        if match:
            project = query.get("project", [DEFAULT_TEST_PROJECT])[0]
            deleted = self.app.test_history.delete(
                match.group(1), match.group(2), match.group(3), project=project
            )
            self._json({"deleted": deleted}, HTTPStatus.OK if deleted else HTTPStatus.NOT_FOUND)
            return
        match = re.fullmatch(r"/api/history/([^/]+)/([^/]+)", path)
        if not match:
            self._json({"error": "接口不存在"}, HTTPStatus.NOT_FOUND)
            return
        deleted = self.app.history.delete(match.group(1), match.group(2))
        self._json({"deleted": deleted}, HTTPStatus.OK if deleted else HTTPStatus.NOT_FOUND)

    def _body_json(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValueError("Content-Length 不合法") from exc
        if length <= 0 or length > MAX_BODY_BYTES:
            raise ValueError("请求体为空或过大")
        try:
            data = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("请求体必须是 UTF-8 JSON") from exc
        if not isinstance(data, dict):
            raise ValueError("请求体必须是 JSON 对象")
        return data

    def _json(self, payload: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status.value)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _serve_binary(self, body: bytes, mime: str = "application/octet-stream", filename: str | None = None) -> None:
        self.send_response(HTTPStatus.OK.value)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if filename:
            safe_fn = quote(filename)
            self.send_header("Content-Disposition", f'attachment; filename="{safe_fn}"; filename*=UTF-8\'\'{safe_fn}')
        self.end_headers()
        self.wfile.write(body)

    def _serve_file(self, path: Path) -> None:
        if not path.is_file():
            self._json({"error": "文件不存在"}, HTTPStatus.NOT_FOUND)
            return
        body = path.read_bytes()
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK.value)
        self.send_header("Content-Type", f"{mime}; charset=utf-8" if mime.startswith("text/") or mime == "application/javascript" else mime)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)



def make_handler(app: WebApplication):
    class BoundHandler(RequestHandler):
        pass

    BoundHandler.app = app
    return BoundHandler



_last_heartbeat_time = time.time()
_has_received_heartbeat = False

def _start_heartbeat_watchdog():
    global _last_heartbeat_time
    _last_heartbeat_time = time.time()
    def _watchdog():
        time.sleep(45.0)  # 启动给予 45 秒首次打开页面宽限期
        while True:
            time.sleep(4.0)
            # 只有当用户确实打开过页面后，或者启动超过 45s 无任何连接，才在 15s 无心跳时退出
            if _has_received_heartbeat and (time.time() - _last_heartbeat_time > 15.0):
                print("[系统] 检测到前端页面已全部关闭，后台服务正在优雅退出...")
                os._exit(0)
    t = threading.Thread(target=_watchdog, daemon=True)
    t.start()


class FrontendHTTPServer(ThreadingHTTPServer):
    """Keep one Windows frontend process authoritative for a listening port."""

    allow_reuse_address = False

    def server_bind(self) -> None:
        if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(
                socket.SOL_SOCKET,
                socket.SO_EXCLUSIVEADDRUSE,
                1,
            )
        super().server_bind()


class FrontendIPv6HTTPServer(FrontendHTTPServer):
    address_family = socket.AF_INET6


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="W30 Agent 自闭环前端服务")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址")
    parser.add_argument("--port", type=int, default=8765, help="监听端口")
    args = parser.parse_args(argv)
    if not _is_loopback_host(args.host):
        parser.error("前端接口当前没有远程认证，只允许监听 localhost/127.0.0.1/::1")

    root = resolve_app_root()
    load_app_env(app_root=root)
    _reload_runtime_limits()
    app = WebApplication(AppPaths.from_root(root))
    server_class = FrontendIPv6HTTPServer if ":" in args.host else FrontendHTTPServer
    server = server_class((args.host, args.port), make_handler(app))
    display_host = f"[{args.host}]" if ":" in args.host else args.host
    print(f"W30 Agent UI: http://{display_host}:{server.server_address[1]}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n服务已停止")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
