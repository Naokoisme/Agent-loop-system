"""W30 Agent 自闭环演示前端的零依赖 HTTP 服务。

启动：uv run python frontend/server.py --port 8765
"""
from __future__ import annotations

import argparse
import base64
import copy
import hashlib
import io
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
import struct
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlparse

from agent_loop_system.case_management import CaseManagementRepository
from agent_loop_system.internal_dispatcher import build_child_command
from agent_loop_system.platforms.registry import PlatformRegistry
from agent_loop_system.projects.registry import ProjectRegistry
from agent_loop_system.tools.case_map import (
    OBSERVATION_ONLY_COMMANDS,
    validated_case_entries,
)
from agent_loop_system.tools.command_protocol import normalize_command
from agent_loop_system.tools.external_execution_history import (
    read_external_execution_history,
)


WORKFLOW_NODES = (
    "validate", "interactive_reproduce", "agent", "apply", "build", "test", "record"
)
TEST_WORKFLOW_NODES = ("load", "execute", "judge", "record")
SAFE_SEGMENT = re.compile(r"^[A-Za-z0-9_.-]+$")
TEST_SCREENSHOT_FILE = re.compile(r"^screenshot(?:-\d{2,3})?\.bmp$")
MAX_BODY_BYTES = 50 * 1024 * 1024  # 50 MB 支持大容量 Excel/用例数据上传
MAX_PORT_SEARCH_ATTEMPTS = 500_000
MAX_LOG_CHARS = 200_000
HISTORY_SCHEMA_VERSION = 2
DEFECT_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
BATCH_STATE_FILE = "batch-state.json"
BATCH_CASES_FILE = "batch-cases.json"
PROMOTION_STATE_FILE = "promotion-state.json"
DEFAULT_TEST_PROJECT = "620C_W6830"
_PLATFORM_REGISTRY = PlatformRegistry()
_PROJECT_REGISTRY: ProjectRegistry | None = None


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


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


def _activate_project_registry(root: Path) -> ProjectRegistry:
    global _PROJECT_REGISTRY
    _PROJECT_REGISTRY = ProjectRegistry(root, _PLATFORM_REGISTRY)
    return _PROJECT_REGISTRY


def _project_registry() -> ProjectRegistry:
    if _PROJECT_REGISTRY is None:
        return _activate_project_registry(Path.cwd())
    return _PROJECT_REGISTRY


def _test_project(
    value: str | None = None,
    *,
    platform_id: str | None = None,
    target_id: str | None = None,
) -> dict[str, Any]:
    """Resolve Project + Platform + Target without any silent fallback."""

    project = str(value or DEFAULT_TEST_PROJECT).strip()
    result = _project_registry().resolve(
        project,
        platform_id=platform_id,
        target_id=target_id,
    )
    if result.get("target_id") == "w30.6202.simulator":
        result.update({
            "simulator_source_root": os.environ.get(
                "W30_6202_SIMULATOR_SOURCE_ROOT",
                r"D:\Agent-loop-workspace\6202_W5230",
            ),
            "simulator_project": "6202_W5230",
            "simulator_build_directory": os.environ.get(
                "W30_6202_SIMULATOR_BUILD_DIRECTORY",
                r"D:\Agent-loop-workspace\6202_W5230\core\gui\simulator\out\build\6202_W5230",
            ),
            "simulator_artifact_path": os.environ.get(
                "W30_6202_SIMULATOR_ARTIFACT_PATH",
                r"D:\Agent-loop-workspace\6202_W5230\core\gui\simulator\bin\main.exe",
            ),
        })
    return result


def _test_project_options() -> list[dict[str, Any]]:
    return [
        _test_project(str(project["project_id"]))
        for project in _project_registry().list()
    ]


def _load_test_runtime_environment() -> None:
    """Use the same repository ``.env`` loader as the per-case test CLI."""

    from agent_loop_system.main import _load_env

    _load_env()


def _test_process_environment(project_meta: dict[str, str]) -> dict[str, str]:
    """Build one child-process environment for both single and batch cases."""

    _load_test_runtime_environment()
    execution_env = dict(os.environ)
    if project_meta.get("execution_target") == "hardware":
        hardware_source_root = execution_env.get(
            "W30_HARDWARE_SOURCE_ROOT", ""
        ).strip()
        hardware_workspace_root = execution_env.get(
            "W30_HARDWARE_WORKSPACE_ROOT", ""
        ).strip()
        hardware_project = str(project_meta["project"])
        execution_env.update({
            "W30_PROJECT": hardware_project,
            "W30_HARDWARE_PROJECT": hardware_project,
        })
        if hardware_source_root:
            execution_env["W30_SOURCE_ROOT"] = hardware_source_root
        if hardware_workspace_root:
            execution_env["W30_AGENT_WORKSPACE_ROOT"] = hardware_workspace_root
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
    config: Path
    project_data: Path

    @classmethod
    def from_root(cls, root: Path) -> "AppPaths":
        root = root.resolve()
        _activate_project_registry(root)
        return cls(
            root=root,
            frontend=root / "frontend",
            defects=root / "defects",
            defect_images=root / "defects_img",
            history=root / "history",
            test_history=root / "history" / "tests",
            evidence=root / "evidence",
            case_map=root / "case_map",
            runtime_jobs=root / ".runtime" / "jobs",
            config=root / "config",
            project_data=root / "project_data",
        )


def _case_catalog_root(paths: AppPaths, project_meta: dict[str, Any]) -> Path:
    catalog_root = (paths.root / str(project_meta["case_catalog_path"])).resolve()
    catalog_root.relative_to(paths.root)
    return catalog_root


def _require_mutable_case_catalog(project_meta: dict[str, Any]) -> None:
    """Frozen 579 manifests are imported artifacts, never Web-editable case files."""

    catalog = project_meta.get("case_catalog") or {}
    if isinstance(catalog, dict) and catalog.get("type") == "manifest_579":
        raise ValueError("CASE_CATALOG_READ_ONLY: 579 冻结用例目录不能在网页中直接修改")


def _new_case_envelope(project_meta: dict[str, Any], sheet: str) -> dict[str, Any]:
    return {
        "profile": str(project_meta["case_map_profile"]),
        "sheet": sheet,
        "cases": [],
    }


def _write_unified_compat_shadow(
    paths: AppPaths,
    project_meta: dict[str, Any],
    case: dict[str, Any],
) -> None:
    """Keep the pre-SQLite JSON view for user-created unified projects.

    The SQLite store is authoritative.  This shadow only preserves compatibility
    with older portable builds and does not apply to frozen 579 or built-in W30
    source catalogs.
    """

    catalog = project_meta.get("case_catalog") or {}
    if catalog.get("type") != "unified":
        return
    sheet = str(case.get("sheet") or "").strip()
    case_id = str(case.get("case_id") or "").strip()
    root = _case_catalog_root(paths, project_meta)
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{sheet}.json"
    raw = _read_json(path, _new_case_envelope(project_meta, sheet))
    entries = raw.get("cases") if isinstance(raw, dict) else None
    if not isinstance(entries, list):
        raw = _new_case_envelope(project_meta, sheet)
        entries = raw["cases"]
    payload = {
        key: copy.deepcopy(value)
        for key, value in case.items()
        if key in {
            "case_id", "sheet", "priority", "precondition_text", "steps_text",
            "expected_text", "verification_points", "setup", "actions", "collect",
            "unable", "mapping_status", "note", "automation_maturity", "blockers",
            "platform_automation", "source_ref", "execution_ref",
            "applicable_platforms", "workflow_state",
        }
    }
    existing = next((item for item in entries if isinstance(item, dict) and item.get("case_id") == case_id), None)
    if existing is None:
        entries.append(payload)
    else:
        existing.clear()
        existing.update(payload)
    _write_json(path, raw)


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
    case_map_root = _case_catalog_root(paths, project_meta)
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
    ledger = _case_catalog_root(paths, project_meta) / "external_execution_history.jsonl"
    if not ledger.is_file() and project_meta.get("case_catalog_adapter") != "w30_case_map":
        return set()
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
        run_dir.mkdir(parents=True, exist_ok=False)

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
            "verdict": result.get("verdict", "FAIL"),
            "reproduction_outcome": result.get("reproduction_outcome"),
            "attempts": result.get("attempts", 0),
            "execution_mode": job.get("execution_mode", "agent_generated"),
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
        _write_json(run_dir / "run.json", run_payload)
        _write_json(run_dir / "patch.json", patch)
        _write_json(run_dir / "test_result.json", test_output or {})

        evidence_dir = self.paths.evidence / defect
        for kind in ("before", "after"):
            source = evidence_dir / f"{kind}.bmp"
            if source.is_file():
                shutil.copy2(source, run_dir / f"{kind}.bmp")
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
        case_map_root = _case_catalog_root(self.paths, _test_project())
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
        project_meta = _test_project(
            str(job.get("project") or DEFAULT_TEST_PROJECT),
            platform_id=str(job.get("requested_platform_id") or job.get("platform_id") or "") or None,
            target_id=str(job.get("target_id") or "") or None,
        )
        project = project_meta["project"]
        run_id = datetime.now().astimezone().strftime("%Y%m%dT%H%M%S%f")
        run_dir = self._run_dir(sheet, case_id, run_id, project=project)
        run_dir.mkdir(parents=True, exist_ok=False)
        case = job.get("case") if isinstance(job.get("case"), dict) else {}
        payload = {
            "schema_version": 2,
            "id": run_id,
            "sheet": sheet,
            "case_id": case_id,
            **{key: project_meta[key] for key in (
                "project", "project_label", "platform_id", "target_id",
                "execution_target", "execution_target_label", "execution_adapter",
                "execution_resource", "profile_version",
            )},
            "requested_platform_id": str(
                job.get("requested_platform_id") or project_meta["platform_id"]
            ),
            "resolved_execution_adapter": str(
                job.get("resolved_execution_adapter") or project_meta["execution_adapter"]
            ),
            "timestamp": job.get("finished_at") or _now(),
            "started_at": job.get("started_at"),
            "finished_at": job.get("finished_at"),
            "verdict": result.get("verdict", "ERROR"),
            "reason": result.get("reason") or job.get("error") or "",
            "execution_mode": result.get("execution_mode", "fixed_mapping"),
            "result_schema_version": result.get("schema_version"),
            "provenance": result.get("provenance", {}),
            "execution_status": result.get("execution_status"),
            "product_verdict": result.get("product_verdict"),
            "automation_maturity": result.get("automation_maturity"),
            "infrastructure_status": result.get("infrastructure_status"),
            "delivery_feedback": result.get("delivery_feedback", []),
            "observations": result.get("observations", []),
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
        if job.get("batch_id"):
            payload["batch_id"] = str(job["batch_id"])
        if job.get("batch_token"):
            payload["batch_token"] = str(job["batch_token"])
        archived_screenshots: list[dict[str, Any]] = []
        raw_screenshots = result.get("screenshots", [])
        if isinstance(raw_screenshots, list):
            for item in raw_screenshots:
                if not isinstance(item, dict):
                    continue
                source = Path(str(item.get("path") or ""))
                if not source.is_file():
                    continue
                file_name = f"screenshot-{len(archived_screenshots) + 1:02d}.bmp"
                shutil.copy2(source, run_dir / file_name)
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
            shutil.copy2(screenshot, run_dir / "screenshot.bmp")
            archived_screenshots.append({
                "index": 1,
                "label": "最终画面",
                "phase": "final",
                "command": "",
                "file": "screenshot.bmp",
            })
        payload["screenshots"] = archived_screenshots
        _write_json(run_dir / "run.json", payload)
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
        for path in self.paths.test_history.rglob("run.json"):
            run = _read_json(path)
            if not isinstance(run, dict):
                continue
            records = records_by_batch.get(str(run.get("batch_id") or ""))
            if records is None:
                continue
            token = str(run.get("batch_token") or "")
            if token and SAFE_SEGMENT.fullmatch(token):
                records[token] = run
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
    """统一用例查询视图，并保留现有 W30 候选复跑兼容事务。"""

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

    def __init__(
        self,
        paths: AppPaths,
        history: TestHistoryStore,
        managed: CaseManagementRepository | None = None,
    ):
        self.paths = paths
        self.history = history
        self.managed = managed or CaseManagementRepository(
            paths.project_data / "case_management.sqlite3"
        )
        self._write_lock = threading.RLock()
        self._source_sync_lock = threading.RLock()
        self._source_sync_fingerprints: dict[str, tuple[tuple[str, int, int], ...]] = {}

    def _source_catalog_fingerprint(self, project_meta: dict[str, Any]) -> tuple[tuple[str, int, int], ...]:
        root = _case_catalog_root(self.paths, project_meta)
        return tuple(
            (str(path.relative_to(root)).replace("\\", "/"), path.stat().st_size, path.stat().st_mtime_ns)
            for path in sorted(root.glob("*.json"), key=lambda item: item.name)
            if path.is_file()
        )

    @staticmethod
    def _source_catalog_signature(
        fingerprint: tuple[tuple[str, int, int], ...],
    ) -> str:
        encoded = json.dumps(
            fingerprint,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest().upper()

    def _ensure_source_synced(self, project_meta: dict[str, Any]) -> None:
        project_id = str(project_meta["project"])
        fingerprint = self._source_catalog_fingerprint(project_meta)
        if self._source_sync_fingerprints.get(project_id) == fingerprint:
            return
        signature = self._source_catalog_signature(fingerprint)
        with self._source_sync_lock:
            if self._source_sync_fingerprints.get(project_id) == fingerprint:
                return
            if not self.managed.source_sync_matches(project_id, signature):
                source_rows = self._source_rows(project_id)
                stable_fields = {
                    "case_id", "sheet", "file_sheet", "title", "priority",
                    "precondition_text", "steps_text", "expected_text",
                    "verification_points", "setup", "actions", "collect", "unable",
                    "mapping_status", "note", "automation_maturity", "blockers",
                    "platform_automation", "source_ref", "execution_ref",
                    "applicable_platforms", "workflow_state", "_source_file",
                    "_source_file_sha256",
                }
                self.managed.sync_source_cases(
                    project_meta,
                    [
                        {key: copy.deepcopy(value) for key, value in row.items() if key in stable_fields}
                        for row in source_rows
                    ],
                    source_fingerprint=signature,
                )
            self._source_sync_fingerprints[project_id] = fingerprint

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
        managed_current = self.managed.get_case(project_meta["project"], case_id)
        if managed_current and (
            not managed_current.get("source_locked")
            or managed_current.get("has_managed_override")
        ):
            if managed_current.get("mapping_status") == "PROMOTED":
                raise ValueError("该用例已经是 PROMOTED")
            occupied = [
                field for field in self.CANDIDATE_FIELDS
                if managed_current.get(field) not in (None, "", [])
            ]
            if occupied:
                raise RuntimeError("统一用例库已存在未完成候选，拒绝覆盖: " + ", ".join(occupied))
            original_fields = {
                field: {
                    "present": field in managed_current,
                    "value": copy.deepcopy(managed_current.get(field)),
                }
                for field in (*self.CANDIDATE_FIELDS, "mapping_status")
            }
            changes = {field: copy.deepcopy(candidate[field]) for field in self.CANDIDATE_FIELDS}
            changes["mapping_status"] = ""
            staged = self.managed.create_revision(
                project_meta,
                case_id,
                changes,
                change_type="AUTOMATION_CANDIDATE",
                change_summary=f"自主探索候选 {source_history.get('id') or ''}",
            )
            return {
                "storage_backend": "sqlite",
                "project": project_meta["project"],
                "sheet": sheet,
                "case_id": case_id,
                "source_history_id": str(source_history.get("id") or ""),
                "candidate_fields": copy.deepcopy(candidate),
                "original_fields": original_fields,
                "staged_revision": staged["revision"],
            }
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

        if context.get("storage_backend") == "sqlite":
            project_id = str(context.get("project") or "")
            case_id = str(context.get("case_id") or "")
            current = self.managed.get_case(project_id, case_id)
            if current is None:
                return {"status": "rollback_conflict", "issues": ["统一用例库中的候选已不存在"]}
            if current.get("mapping_status") == "PROMOTED":
                return {"status": "promoted", "issues": []}
            if (
                int(current.get("current_revision") or 0) != int(context.get("staged_revision") or -1)
                or not self._candidate_matches(current, context)
            ):
                return {
                    "status": "rollback_conflict",
                    "issues": ["候选复跑期间当前用例已被其他操作修改，未自动覆盖"],
                }
            original = context.get("original_fields")
            if not isinstance(original, dict):
                return {"status": "rollback_conflict", "issues": ["缺少候选写入前快照，未自动覆盖"]}
            restored: dict[str, Any] = {}
            for field in (*self.CANDIDATE_FIELDS, "mapping_status"):
                state = original.get(field)
                if isinstance(state, dict) and state.get("present") is True:
                    restored[field] = copy.deepcopy(state.get("value"))
                else:
                    restored[field] = [] if field in {"setup", "actions", "collect", "verification_points"} else ""
            self.managed.create_revision(
                _test_project(project_id), case_id, restored,
                change_type="AUTOMATION_CANDIDATE_ROLLBACK",
                change_summary="候选复跑未通过，恢复候选前内容",
            )
            return {"status": "rolled_back", "issues": []}

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
        if context.get("storage_backend") == "sqlite":
            project_id = str(context.get("project") or "")
            case_id = str(context.get("case_id") or "")
            current = self.managed.get_case(project_id, case_id)
            if current is None:
                issues.append("统一用例库中的候选已不存在")
            elif int(current.get("current_revision") or 0) != int(context.get("staged_revision") or -1):
                issues.append("统一用例库中的候选在复跑期间发生变化")
            elif not self._candidate_matches(current, context):
                issues.append("统一用例库当前候选与本次复跑上下文不一致")
            issues = self._unique_issues(issues)
            if not issues:
                self.managed.create_revision(
                    _test_project(project_id), case_id,
                    {"mapping_status": "PROMOTED", "automation_maturity": "PROMOTED"},
                    change_type="AUTOMATION_PROMOTE",
                    change_summary="候选复跑与证据门禁通过",
                )
                return {"status": "promoted", "issues": []}
            rollback = self.rollback_agent_candidate(context)
            rollback_issues = rollback.get("issues") if isinstance(rollback, dict) else []
            return {
                "status": "rolled_back" if rollback.get("status") == "rolled_back" else "rollback_conflict",
                "issues": self._unique_issues(issues + list(rollback_issues or [])),
            }
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

    def _source_rows(
        self,
        project: str = DEFAULT_TEST_PROJECT,
    ) -> list[dict[str, Any]]:
        project_meta = _test_project(project)
        project = project_meta["project"]
        rows: list[dict[str, Any]] = []
        history_index = self.history.summary_index(project=project)
        case_map_root = _case_catalog_root(self.paths, project_meta)
        externally_explored_ids = _external_explored_ids(self.paths, project)
        for path in sorted(case_map_root.glob("*.json"), key=lambda item: item.stem):
            source_file_sha256 = hashlib.sha256(path.read_bytes()).hexdigest().upper()
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
                    "automation_maturity": str(
                        item.get("automation_maturity")
                        or (item.get("mapping_status") if item.get("mapping_status") in {"PROMOTED", "AUTO_READY"} else "")
                        or "UNMAPPED"
                    ),
                    "blockers": [
                        str(value) for value in item.get("blockers", []) if str(value).strip()
                    ] if isinstance(item.get("blockers"), list) else [],
                    "platform_automation": copy.deepcopy(item.get("platform_automation", {}))
                    if isinstance(item.get("platform_automation"), dict) else {},
                    "source_ref": copy.deepcopy(item.get("source_ref", {}))
                    if isinstance(item.get("source_ref"), dict) else {},
                    "execution_ref": copy.deepcopy(item.get("execution_ref", {}))
                    if isinstance(item.get("execution_ref"), dict) else {},
                    "_source_file": str(path.relative_to(self.paths.root)).replace("\\", "/"),
                    "_source_file_sha256": source_file_sha256,
                    **{key: project_meta[key] for key in (
                        "project", "project_label", "platform_id", "target_id",
                        "execution_target", "execution_target_label",
                    )},
                }
                history = history_index.get((path.stem, case_id))
                latest = history.get("latest") if history else None
                row["last_run_at"] = latest.get("timestamp") if latest else None
                row["history_count"] = int(history.get("history_count") or 0) if history else 0
                latest_verdict = str(latest.get("verdict") or "").upper() if latest else ""
                row["last_platform_id"] = str(
                    (latest or {}).get("requested_platform_id")
                    or (latest or {}).get("platform_id")
                    or row.get("platform_id")
                    or ""
                )
                row["last_execution_adapter"] = str(
                    (latest or {}).get("resolved_execution_adapter")
                    or (latest or {}).get("execution_adapter")
                    or ""
                )
                row["last_product_verdict"] = str(
                    (latest or {}).get("product_verdict") or latest_verdict or "PENDING"
                ).upper()
                row["last_automation_maturity"] = str(
                    (latest or {}).get("automation_maturity")
                    or row.get("automation_maturity")
                    or row.get("mapping_status")
                    or "UNMAPPED"
                ).upper()
                row["last_infrastructure_status"] = str(
                    (latest or {}).get("infrastructure_status") or "UNKNOWN"
                ).upper()
                if row["history_count"] == 0:
                    row["latest_verdict"] = "PENDING"
                elif latest_verdict == "SKIP":
                    row["latest_verdict"] = "CANNOT_VERIFY"
                elif latest_verdict in {"PASS", "FAIL", "CANNOT_VERIFY", "ERROR"}:
                    row["latest_verdict"] = latest_verdict
                else:
                    row["latest_verdict"] = "ERROR"
                row["external_explored"] = case_id in externally_explored_ids
                # 外部探索账本与映射固化是两条独立事实轴。显式的站内候选
                # 复跑也能形成 PROMOTED，但绝不能伪造一条“外部探索”记录。
                row["is_promoted"] = row["mapping_status"] in {"PROMOTED", "AUTO_READY"}
                row["maturity_state"] = (
                    "solidified"
                    if row["is_promoted"]
                    else "explored_unsolidified"
                    if row["external_explored"]
                    else "unexplored"
                )
                rows.append(row)
        return rows

    def _all(
        self,
        project: str = DEFAULT_TEST_PROJECT,
    ) -> list[dict[str, Any]]:
        """合并冻结/Case Map 基线与人员新增、导入和版本覆盖。"""

        project_meta = _test_project(project)
        project_id = str(project_meta["project"])
        self._ensure_source_synced(project_meta)
        rows = self.managed.list_cases(project_id)
        history_index = self.history.summary_index(project=project_id)
        externally_explored_ids = _external_explored_ids(self.paths, project_id)
        for row in rows:
            row.update({key: project_meta[key] for key in (
                "project", "project_label", "platform_id", "target_id",
                "execution_target", "execution_target_label",
            )})
            sheet = str(row.get("file_sheet") or row.get("sheet") or "")
            case_id = str(row.get("case_id") or "")
            history = history_index.get((sheet, case_id))
            latest = history.get("latest") if history else None
            row["last_run_at"] = latest.get("timestamp") if latest else None
            row["history_count"] = int(history.get("history_count") or 0) if history else 0
            latest_verdict = str(latest.get("verdict") or "").upper() if latest else ""
            row["last_platform_id"] = str(
                (latest or {}).get("requested_platform_id")
                or (latest or {}).get("platform_id")
                or row.get("platform_id") or ""
            )
            row["last_execution_adapter"] = str(
                (latest or {}).get("resolved_execution_adapter")
                or (latest or {}).get("execution_adapter") or ""
            )
            row["last_product_verdict"] = str(
                (latest or {}).get("product_verdict") or latest_verdict or "PENDING"
            ).upper()
            row["last_automation_maturity"] = str(
                (latest or {}).get("automation_maturity")
                or row.get("automation_maturity")
                or row.get("mapping_status") or "UNMAPPED"
            ).upper()
            row["last_infrastructure_status"] = str(
                (latest or {}).get("infrastructure_status") or "UNKNOWN"
            ).upper()
            if row["history_count"] == 0:
                row["latest_verdict"] = "PENDING"
            elif latest_verdict == "SKIP":
                row["latest_verdict"] = "CANNOT_VERIFY"
            elif latest_verdict in {"PASS", "FAIL", "CANNOT_VERIFY", "ERROR"}:
                row["latest_verdict"] = latest_verdict
            else:
                row["latest_verdict"] = "ERROR"
            row["external_explored"] = case_id in externally_explored_ids
            binding_promoted = any(
                isinstance(binding, dict)
                and binding.get("runnable") is True
                and str(binding.get("maturity") or "") in {"PROMOTED", "AUTO_READY"}
                for binding in (row.get("platform_automation") or {}).values()
            )
            row["is_promoted"] = (
                str(row.get("mapping_status") or "") in {"PROMOTED", "AUTO_READY"}
                or binding_promoted
            )
            row["maturity_state"] = (
                "solidified" if row["is_promoted"]
                else "explored_unsolidified" if row["external_explored"]
                else "unexplored"
            )
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
        module_counts: dict[str, int] = {}
        for row in rows:
            module_name = str(row.get("file_sheet") or row.get("sheet") or "未分类")
            module_counts[module_name] = module_counts.get(module_name, 0) + 1
        batch_summary = {
            category: sum(self.run_category(row) == category for row in rows)
            for category in self.RUN_CATEGORIES
        }
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
        if modules:
            rows = [
                row for row in rows
                if str(row.get("file_sheet") or row.get("sheet") or "") in modules
            ]
        summary = {
            "all": len(rows),
            "unexplored": sum(row["maturity_state"] == "unexplored" for row in rows),
            "externally_explored": sum(row["external_explored"] for row in rows),
            "explored_unsolidified": sum(
                row["maturity_state"] == "explored_unsolidified" for row in rows
            ),
            "solidified": sum(row["is_promoted"] for row in rows),
        }
        maturity_counts: dict[str, int] = {}
        for row in rows:
            maturity = str(row.get("automation_maturity") or "UNMAPPED").upper()
            maturity_counts[maturity] = maturity_counts.get(maturity, 0) + 1
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
            "automation_maturity_counts": maturity_counts,
            "batch_summary": batch_summary,
            "module_counts": module_counts,
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
        limit: int = 20,
    ) -> dict[str, Any]:
        """Return the small aggregate needed by the overview page in one request."""

        project_meta = _test_project(project)
        project = project_meta["project"]
        rows = self._all(project)
        summary = {
            "all": len(rows),
            "unexplored": sum(row["maturity_state"] == "unexplored" for row in rows),
            "externally_explored": sum(row["external_explored"] for row in rows),
            "explored_unsolidified": sum(
                row["maturity_state"] == "explored_unsolidified" for row in rows
            ),
            "solidified": sum(row["is_promoted"] for row in rows),
        }
        maturity_counts: dict[str, int] = {}
        verdict_counts: dict[str, int] = {
            "PASS": 0,
            "FAIL": 0,
            "ERROR": 0,
            "CANNOT_VERIFY": 0,
            "SKIP": 0,
        }
        for row in rows:
            maturity = str(row.get("automation_maturity") or "UNMAPPED").upper()
            maturity_counts[maturity] = maturity_counts.get(maturity, 0) + 1
            verdict = str(row.get("latest_verdict") or "").upper()
            if verdict:
                verdict_counts[verdict] = verdict_counts.get(verdict, 0) + 1
        recent_cases = sorted(
            (row for row in rows if row.get("last_run_at")),
            key=lambda row: str(row.get("last_run_at") or ""),
            reverse=True,
        )[:limit]
        recent_exceptions = sorted(
            (
                row for row in rows
                if str(row.get("latest_verdict") or "").upper()
                in {"FAIL", "ERROR", "CANNOT_VERIFY", "SKIP"}
            ),
            key=lambda row: str(row.get("last_run_at") or ""),
            reverse=True,
        )[:limit]
        return {
            "summary": summary,
            "automation_maturity_counts": maturity_counts,
            "verdict_counts": verdict_counts,
            "recent_cases": recent_cases,
            "recent_exceptions": recent_exceptions,
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

    ACTIVE_STATUSES = {"queued", "running", "finalizing"}
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

    def __init__(
        self,
        paths: AppPaths,
        cases: CaseMapRepository,
        history: TestHistoryStore,
        *,
        platform_gateways: dict[str, Any] | None = None,
    ):
        self.paths = paths
        self.cases = cases
        self.history = history
        if platform_gateways is None:
            from agent_loop_system.platforms.platform_579 import Platform579Gateway
            platform_gateways = {"579": Platform579Gateway()}
        self.platform_gateways = dict(platform_gateways)
        self._lock = threading.Lock()
        self._jobs: dict[str, dict[str, Any]] = {}
        self._active_job_ids: dict[str, str] = {}
        self._load_batches()
        self._recover_stale_promotions()

    @staticmethod
    def _execution_slot(job: dict[str, Any]) -> str:
        resource = str(job.get("execution_resource") or "").strip().lower()
        if not resource:
            target = str(job.get("execution_target") or "simulator").strip().lower()
            if target not in {"hardware", "simulator"}:
                raise ValueError(f"未知测试目标: {target or '空'}")
            resource = f"w30.{target}"
        return resource

    @staticmethod
    def _execution_slot_label(slot: str) -> str:
        if slot == "579.hardware":
            return "579 O2 真机"
        return "真机" if "hardware" in slot else "模拟器"

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
                "platform_id": str(run.get("platform_id") or case.get("platform_id") or job.get("platform_id") or "w30"),
                "target_id": str(run.get("target_id") or case.get("target_id") or job.get("target_id") or ""),
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
                "project", "project_label", "platform_id", "target_id",
                "execution_target", "execution_target_label", "execution_adapter",
                "execution_resource", "profile_version",
            ):
                job.setdefault(key, project_meta[key])
            job.setdefault("verdict_counts", {key: 0 for key in ("PASS", "FAIL", "ERROR", "CANNOT_VERIFY")})
            job.setdefault("recent_results", [])
            job.setdefault("completed", 0)
            job.setdefault("total", len(cases))
            job.setdefault("case_attempts", {})
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
                job["status"] = "interrupted"
                job["interruption_reason"] = "前端服务重启或测试进程中断"
                job["finished_at"] = _now()
                job["current_node"] = None
                job["cancel_requested"] = False
            self._jobs[job_id] = job
            self._persist_batch_locked(job)

    def start(
        self,
        *,
        sheet: str,
        case_id: str,
        project: str = DEFAULT_TEST_PROJECT,
        platform_id: str | None = None,
        target_id: str | None = None,
        candidate_replay: bool = False,
        promotion_source: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        project_meta = _test_project(
            project,
            platform_id=platform_id,
            target_id=target_id,
        )
        project = project_meta["project"]
        case = self.cases.get(sheet, case_id, project=project)
        if case is None:
            raise ValueError("测试用例不存在")
        if candidate_replay and not isinstance(promotion_source, dict):
            raise ValueError("候选复跑缺少自主探索来源记录")
        if project_meta["platform_id"] == "579":
            automation = (case.get("platform_automation") or {}).get("579", {})
            if not automation.get("runnable"):
                raise ValueError(
                    f"CASE_NOT_RUNNABLE: {case_id} "
                    f"{automation.get('blocker') or case.get('note') or '579 映射不可执行'}"
                )
            gateway = self.platform_gateways.get("579")
            if gateway is None:
                raise ValueError("CAPABILITY_MISSING: 579 执行网关未安装")
            from agent_loop_system.platforms.contracts import RunRequest
            preflight = gateway.preflight(RunRequest(
                project_id=project,
                platform_id="579",
                target_id=project_meta["target_id"],
                case_ids=(case_id,),
            ))
            if not preflight.ready:
                raise ValueError(
                    "ENV_BLOCKED: " + "; ".join(preflight.blockers)
                )
        case = {
            **case,
            **{key: project_meta[key] for key in (
                "project", "project_label", "platform_id", "target_id",
                "execution_target", "execution_target_label", "execution_adapter",
                "execution_resource", "profile_version",
            )},
            "requested_platform_id": project_meta["platform_id"],
        }
        promotion_context: dict[str, Any] | None = None
        with self._lock:
            job_id = uuid.uuid4().hex[:12]
            slot_claim = {
                "id": job_id,
                **{key: project_meta[key] for key in (
                    "project", "project_label", "platform_id", "target_id",
                    "execution_target", "execution_target_label", "execution_adapter",
                    "execution_resource", "profile_version",
                )},
                "requested_platform_id": project_meta["platform_id"],
                "resolved_execution_adapter": project_meta["execution_adapter"],
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
                self._release_execution_slot_locked(job_id)
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
        platform_id: str | None = None,
        target_id: str | None = None,
    ) -> dict[str, Any]:
        project_meta = _test_project(
            project,
            platform_id=platform_id,
            target_id=target_id,
        )
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
        if project_meta["platform_id"] == "579":
            blocked = []
            for case in cases:
                automation = (case.get("platform_automation") or {}).get("579", {})
                if not automation.get("runnable"):
                    blocked.append(
                        f"{case['case_id']}: {automation.get('blocker') or case.get('note') or '579 映射不可执行'}"
                    )
            if blocked:
                raise ValueError("CASE_NOT_RUNNABLE: " + "; ".join(blocked))
            gateway = self.platform_gateways.get("579")
            if gateway is None:
                raise ValueError("CAPABILITY_MISSING: 579 执行网关未安装")
            from agent_loop_system.platforms.contracts import RunRequest
            preflight = gateway.preflight(RunRequest(
                project_id=project,
                platform_id="579",
                target_id=project_meta["target_id"],
                case_ids=tuple(str(case["case_id"]) for case in cases),
            ))
            if not preflight.ready:
                raise ValueError("ENV_BLOCKED: " + "; ".join(preflight.blockers))
        cases = [
            {
                **case,
                **{key: project_meta[key] for key in (
                    "project", "project_label", "platform_id", "target_id",
                    "execution_target", "execution_target_label", "execution_adapter",
                    "execution_resource", "profile_version",
                )},
                "requested_platform_id": project_meta["platform_id"],
            }
            for case in cases
        ]
        with self._lock:
            job_id = uuid.uuid4().hex[:12]
            job = {
                "id": job_id,
                "type": "batch",
                **{key: project_meta[key] for key in (
                    "project", "project_label", "platform_id", "target_id",
                    "execution_target", "execution_target_label", "execution_adapter",
                    "execution_resource", "profile_version",
                )},
                "requested_platform_id": project_meta["platform_id"],
                "resolved_execution_adapter": project_meta["execution_adapter"],
                "status": "queued",
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
                "recent_results": [],
                "cancel_requested": False,
                "error": None,
                "case_attempts": {},
                "selected_categories": sorted(categories) if categories is not None else [],
                "cases": cases,
            }
            self._claim_execution_slot_locked(job)
            self._jobs[job_id] = job
        thread = threading.Thread(
            target=self._run_batch,
            args=(job_id,),
            daemon=True,
            name=f"case-test-batch-{job_id}",
        )
        thread.start()
        return self.get(job_id) or job

    def cancel_batch(self, job_id: str) -> dict[str, Any]:
        job_id = _safe_segment(job_id, "任务编号")
        gateway = None
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise ValueError("测试任务不存在")
            if job.get("type") != "batch":
                raise ValueError("只能停止批次测试")
            if job.get("status") not in self.ACTIVE_STATUSES:
                raise ValueError("批次测试已经结束")
            job["cancel_requested"] = True
            job["interruption_reason"] = "用户请求在当前用例结束后暂停"
            if job.get("platform_id") == "579":
                gateway = self.platform_gateways.get("579")
            self._persist_batch_locked(job)
        if gateway is not None:
            gateway.cancel(job_id)
        return self.get(job_id) or {}

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
            if job.get("platform_id") == "579":
                gateway = self.platform_gateways.get("579")
                if gateway is None:
                    raise ValueError("CAPABILITY_MISSING: 579 执行网关未安装")
                from agent_loop_system.platforms.contracts import RunRequest
                remaining = job.get("cases", [])[int(job.get("completed") or 0):]
                preflight = gateway.preflight(RunRequest(
                    project_id=str(job.get("project") or ""),
                    platform_id="579",
                    target_id=str(job.get("target_id") or ""),
                    case_ids=tuple(str(case.get("case_id") or "") for case in remaining),
                ))
                if not preflight.ready:
                    raise ValueError("ENV_BLOCKED: " + "; ".join(preflight.blockers))
            job["status"] = "queued"
            job["cancel_requested"] = False
            job["finished_at"] = None
            job["current_case"] = None
            job["current_node"] = "load"
            job["resumed_at"] = _now()
            job["resume_count"] = int(job.get("resume_count") or 0) + 1
            job["error"] = None
            job.pop("interruption_reason", None)
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
            thread.start()
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
            for index, path in enumerate(sorted(current_dir.glob("screenshot*.bmp")), start=1):
                if not TEST_SCREENSHOT_FILE.fullmatch(path.name):
                    continue
                screenshots.append({
                    "index": index,
                    "label": str(labels[index - 1]) if index <= len(labels) else f"检查点 {index}",
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
        if not TEST_SCREENSHOT_FILE.fullmatch(file_name):
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
                for slot in list(self._active_job_ids)
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
    ) -> dict[str, Any]:
        project_meta = _test_project(
            str(case.get("project") or DEFAULT_TEST_PROJECT),
            platform_id=str(case.get("requested_platform_id") or case.get("platform_id") or "") or None,
            target_id=str(case.get("target_id") or "") or None,
        )
        job_dir.mkdir(parents=True, exist_ok=True)
        result_file = job_dir / "test_result.json"
        screenshot = job_dir / "screenshot.bmp"
        started_at = _now()
        if project_meta["execution_adapter"] == "platform_579":
            gateway = self.platform_gateways.get("579")
            if gateway is None:
                result = {
                    "verdict": "ERROR",
                    "reason": "CAPABILITY_MISSING: 579 执行网关未安装",
                    "infrastructure_status": "CAPABILITY_MISSING",
                    "evidence_contract": {
                        "complete": False,
                        "issues": [{"message": "579 执行网关未安装"}],
                    },
                }
            else:
                try:
                    result = gateway.run_case(
                        case=case,
                        artifact_dir=job_dir,
                        run_id=job_id,
                    )
                except BaseException as exc:
                    result = {
                        "verdict": "ERROR",
                        "reason": f"579 Runner 异常: {exc}",
                        "infrastructure_status": "RUNNER_ERROR",
                        "evidence_contract": {
                            "complete": False,
                            "issues": [{"message": f"579 Runner 异常: {exc}"}],
                        },
                        "setup_errors": [],
                        "action_errors": [str(exc)],
                        "collect_errors": [],
                        "screenshots": [],
                    }
            _write_json(result_file, result)
            verdict = str(result.get("verdict") or "ERROR").upper()
            if verdict not in {"PASS", "FAIL", "CANNOT_VERIFY", "ERROR"}:
                verdict = "ERROR"
            execution_status = str(result.get("infrastructure_status") or "RUNNER_ERROR")
            execute_failed = bool(
                execution_status != "READY"
                or result.get("aborted")
                or result.get("setup_errors")
                or result.get("action_errors")
                or result.get("collect_errors")
            )
            return {
                "result": result,
                "stdout": "",
                "stderr": "",
                "return_code": 0 if not execute_failed else 1,
                "verdict": verdict,
                "reason": str(result.get("reason") or "579 Runner 未提供理由"),
                "execution_reason": str(result.get("reason") or execution_status),
                "execute_failed": execute_failed,
                "started_at": started_at,
                "finished_at": _now(),
                "screenshot": screenshot,
            }
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
        argv = build_child_command("test", child_args)
        stdout = ""
        stderr = ""
        return_code: int | None = None
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
                self._jobs[job_id]["process"] = process
            stdout, stderr = process.communicate()
            return_code = process.returncode
        except BaseException as exc:
            stderr = f"{type(exc).__name__}: {exc}"

        loaded = _read_json(result_file, {})
        result = loaded if isinstance(loaded, dict) else {}
        if result.get("skipped"):
            verdict = "CANNOT_VERIFY"
        else:
            verdict = str(result.get("verdict") or "ERROR").upper()
        if verdict not in {"PASS", "FAIL", "CANNOT_VERIFY"}:
            verdict = "ERROR"
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
            not result
            or result.get("aborted")
            or result.get("setup_errors")
            or result.get("action_errors")
            or result.get("collect_errors")
            or evidence_incomplete
        )
        error = "" if result else (stderr.strip() or "测试进程未生成结果文件")
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
        return {
            "result": result,
            "stdout": stdout,
            "stderr": stderr,
            "return_code": return_code,
            "verdict": verdict,
            "reason": reason,
            "execution_reason": execution_reason,
            "execute_failed": execute_failed,
            "started_at": started_at,
            "finished_at": _now(),
            "screenshot": screenshot,
        }

    def _run(self, job_id: str) -> None:
        with self._lock:
            job = self._jobs[job_id]
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

        execution = self._execute_case(job_id=job_id, case=job["case"], job_dir=job_dir)
        result = execution["result"]
        verdict = execution["verdict"]
        reason = execution["reason"]
        execute_failed = execution["execute_failed"]
        with self._lock:
            job = self._jobs[job_id]
            job["return_code"] = execution["return_code"]
            job["finished_at"] = execution["finished_at"]
            job["verdict"] = verdict
            job["reason"] = reason
            job["product_verdict"] = result.get("product_verdict")
            job["automation_maturity"] = result.get("automation_maturity")
            job["infrastructure_status"] = result.get("infrastructure_status")
            job["delivery_feedback"] = result.get("delivery_feedback", [])
            job["observations"] = result.get("observations", [])
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

        try:
            history_id = self.history.create(
                job=job,
                result={**result, "verdict": verdict, "reason": reason},
                stdout=execution["stdout"],
                stderr=execution["stderr"],
                screenshot=execution["screenshot"],
            )
        except BaseException as exc:
            history_id = None
            with self._lock:
                job["error"] = f"测试记录保存失败: {exc}"

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
            job["status"] = "completed" if history_id else "failed"
            job.pop("process", None)
            self._release_execution_slot_locked(job_id)

    def _run_batch(self, job_id: str) -> None:
        with self._lock:
            job = self._jobs[job_id]
            execution_target = str(job.get("execution_target") or "simulator")
            platform_id = str(job.get("platform_id") or "w30")

        if execution_target == "hardware" and platform_id == "w30":
            try:
                from agent_loop_system.tools.hardware_target import HardwareTargetConfig
                from agent_loop_system.tools.real_device import query_test_session_status

                _load_test_runtime_environment()
                HardwareTargetConfig.from_env()
                status = query_test_session_status(
                    evidence_dir=(
                        self.paths.runtime_jobs
                        / job_id
                        / "preflight"
                        / "test-session"
                    ),
                )
                check = {
                    "checked_at": _now(),
                    "active": status.active,
                    "lease_seconds": status.lease_seconds,
                }
                if not status.active:
                    raise RuntimeError(
                        "手表 24 小时测试模式未开启或已到期；"
                        "请先在手表端开启，再启动批次"
                    )
            except Exception as exc:
                with self._lock:
                    job = self._jobs[job_id]
                    job["test_session_check"] = {
                        "checked_at": _now(),
                        "active": False,
                        "lease_seconds": 0,
                        "error": str(exc),
                    }
                    job["status"] = "failed"
                    job["error"] = f"真机批次前置检查失败: {exc}"
                    job["finished_at"] = _now()
                    job["current_node"] = None
                    self._release_execution_slot_locked(job_id)
                    self._persist_batch_locked(job)
                return
            with self._lock:
                self._jobs[job_id]["test_session_check"] = check

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
                "platform_id": case.get("platform_id", "w30"),
                "target_id": case.get("target_id", ""),
                "execution_adapter": case.get("execution_adapter", "w30_cli"),
                "execution_resource": case.get("execution_resource", ""),
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
                job["current_node"] = "execute"
                self._persist_batch_locked(job)

            execution = self._execute_case(job_id=job_id, case=case, job_dir=case_dir)
            if platform_id == "579":
                infrastructure_status = str(
                    (execution.get("result") or {}).get("infrastructure_status") or ""
                )
                infrastructure_failure = (
                    str(execution.get("execution_reason") or infrastructure_status)
                    if infrastructure_status in {
                        "CLEANUP_REQUIRED", "TRANSPORT_ERROR", "CAPABILITY_MISSING",
                        "ENV_BLOCKED", "RUNNER_ERROR",
                    }
                    else None
                )
            else:
                infrastructure_failure = (
                    self._hardware_infrastructure_failure(execution)
                    if execution_target == "hardware"
                    else None
                )
            history_job = {
                "sheet": case["file_sheet"],
                "case_id": case["case_id"],
                "project": case.get("project", job.get("project", DEFAULT_TEST_PROJECT)),
                "requested_platform_id": case.get("requested_platform_id", job.get("requested_platform_id")),
                "platform_id": case.get("platform_id", job.get("platform_id")),
                "target_id": case.get("target_id", job.get("target_id")),
                "resolved_execution_adapter": case.get("execution_adapter", job.get("resolved_execution_adapter")),
                "case": case,
                "started_at": execution["started_at"],
                "finished_at": execution["finished_at"],
                "return_code": execution["return_code"],
                "error": (
                    execution["execution_reason"]
                    if execution["execute_failed"]
                    else None
                ),
            }
            if not infrastructure_failure:
                history_job["batch_id"] = job_id
                history_job["batch_token"] = token
            with self._lock:
                self._jobs[job_id]["current_node"] = "record"
            try:
                history_id = self.history.create(
                    job=history_job,
                    result={
                        **execution["result"],
                        "verdict": execution["verdict"],
                        "reason": execution["reason"],
                    },
                    stdout=execution["stdout"],
                    stderr=execution["stderr"],
                    screenshot=execution["screenshot"],
                )
            except BaseException as exc:
                history_id = None
                execution["verdict"] = "ERROR"
                execution["reason"] = f"测试记录保存失败: {exc}"

            record = {
                "sheet": case["file_sheet"],
                "case_id": case["case_id"],
                "project": case.get("project", job.get("project", DEFAULT_TEST_PROJECT)),
                "project_label": case.get("project_label", job.get("project_label", "")),
                "platform_id": case.get("platform_id", job.get("platform_id", "w30")),
                "target_id": case.get("target_id", job.get("target_id", "")),
                "execution_target": case.get("execution_target", job.get("execution_target", "simulator")),
                "execution_target_label": case.get("execution_target_label", job.get("execution_target_label", "模拟器")),
                "verdict": execution["verdict"],
                "reason": execution["reason"],
                "history_id": history_id,
                "finished_at": execution["finished_at"],
                "screenshot_count": len(execution["result"].get("screenshots", [])),
            }
            with self._lock:
                job = self._jobs[job_id]
                job["completed"] = offset if infrastructure_failure else index
                if not infrastructure_failure:
                    job["verdict_counts"][execution["verdict"]] += 1
                job["recent_results"] = ([record] + job["recent_results"])[:30]
                job["current_runtime_archived"] = history_id is not None
                job.pop("process", None)
                cancel_requested = bool(job.get("cancel_requested"))
                self._persist_batch_locked(job)
            if infrastructure_failure:
                interruption_reason = (
                    f"{platform_id} 平台链路异常，已在第 {index} 条 {case['case_id']} 中断："
                    f"{infrastructure_failure}"
                )
                with self._lock:
                    job = self._jobs[job_id]
                    job["status"] = "interrupted"
                    job["error"] = infrastructure_failure
                    job["interruption_reason"] = interruption_reason
                    job["finished_at"] = _now()
                    job["current_node"] = None
                    job.pop("process", None)
                    self._release_execution_slot_locked(job_id)
                    self._persist_batch_locked(job)
                return
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
            job.pop("process", None)
            self._release_execution_slot_locked(job_id)
            self._persist_batch_locked(job)


class JobManager:
    def __init__(self, paths: AppPaths, defects: DefectRepository, history: HistoryStore):
        self.paths = paths
        self.defects = defects
        self.history = history
        self._lock = threading.Lock()
        self._jobs: dict[str, dict[str, Any]] = {}
        self._active_job_id: str | None = None

    def start(self, *, defect: str) -> dict[str, Any]:
        if self.defects.get(defect) is None:
            raise ValueError("缺陷不存在")

        with self._lock:
            if self._active_job_id:
                active = self._jobs.get(self._active_job_id, {})
                if active.get("status") in {"queued", "running", "finalizing"}:
                    raise RuntimeError(f"已有修复任务 {self._active_job_id} 正在运行")

            job_id = uuid.uuid4().hex[:12]
            job = {
                "id": job_id,
                "defect": defect,
                "execution_mode": "agent_generated",
                "status": "queued",
                "current_node": None,
                "nodes": {node: "pending" for node in WORKFLOW_NODES},
                "verdict": "PENDING",
                "created_at": _now(),
                "started_at": None,
                "finished_at": None,
                "history_id": None,
                "error": None,
            }
            self._jobs[job_id] = job
            self._active_job_id = job_id

        thread = threading.Thread(target=self._run, args=(job_id,), daemon=True, name=f"repair-{job_id}")
        thread.start()
        return self.get(job_id) or job

    def get(self, job_id: str) -> dict[str, Any] | None:
        job_id = _safe_segment(job_id, "任务编号")
        with self._lock:
            job = self._jobs.get(job_id)
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
        if job and job.get("status") in {"queued", "running", "finalizing"}:
            return job
        return None

    def _run(self, job_id: str) -> None:
        with self._lock:
            job = self._jobs[job_id]
            job["status"] = "running"
            job["started_at"] = _now()
            job_dir = self.paths.runtime_jobs / job_id
            job_dir.mkdir(parents=True, exist_ok=True)
            progress_file = job_dir / "progress.json"
            result_file = job_dir / "result.json"
            job["progress_file"] = str(progress_file)
            job["result_file"] = str(result_file)

        argv = build_child_command(
            "agent",
            [
                "--defect",
                job["defect"],
                "--task-id",
                job["defect"],
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
        try:
            process = subprocess.Popen(
                argv,
                cwd=self.paths.root,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            with self._lock:
                job["process"] = process
            stdout, stderr = process.communicate()
            return_code = process.returncode
            loaded = _read_json(result_file, {})
            result = loaded if isinstance(loaded, dict) else {}
            if not result:
                result = {"verdict": "FAIL", "attempts": 0, "error": stderr.strip() or "修复进程未生成结果文件"}
        except BaseException as exc:
            result = {"verdict": "FAIL", "attempts": 0, "error": f"{type(exc).__name__}: {exc}"}
            stderr = (stderr + "\n" + result["error"]).strip()

        progress = _read_json(progress_file, {})
        with self._lock:
            job = self._jobs[job_id]
            job["finished_at"] = _now()
            job["return_code"] = return_code
            job["progress"] = progress if isinstance(progress, dict) else {}
            job["nodes"] = job["progress"].get("nodes", job["nodes"])
            job["verdict"] = result.get("verdict", "FAIL")
            job["error"] = result.get("error")
            job["status"] = "finalizing"

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

        with self._lock:
            job["history_id"] = history_id
            if history_id is not None:
                job["status"] = "completed" if result_file.is_file() else "failed"
            job.pop("process", None)
            if self._active_job_id == job_id:
                self._active_job_id = None


class WebApplication:
    def __init__(self, paths: AppPaths):
        self.paths = paths
        self.platforms = _PLATFORM_REGISTRY
        self.projects = _activate_project_registry(paths.root)
        self.history = HistoryStore(paths)
        self.test_history = TestHistoryStore(paths)
        self.defects = DefectRepository(paths, self.history)
        self.case_store = CaseManagementRepository(
            paths.project_data / "case_management.sqlite3"
        )
        self.cases = CaseMapRepository(paths, self.test_history, self.case_store)
        self.jobs = JobManager(paths, self.defects, self.history)
        self.test_jobs = CaseTestManager(paths, self.cases, self.test_history)
        self._execution_lock = threading.Lock()
        self.import_jobs: dict[str, dict[str, Any]] = {}
        self._import_lock = threading.Lock()

    def execution_options(
        self,
        project_id: str,
        *,
        case_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        project = self.projects.get(project_id, include_archived=False)
        rows = self.cases._all(project=project_id)
        wanted = {str(value).strip() for value in (case_ids or []) if str(value).strip()}
        selected = [row for row in rows if not wanted or row["case_id"] in wanted]
        options: list[dict[str, Any]] = []
        for platform_id in project["allowed_platforms"]:
            targets = [
                self.platforms.target(target_id)
                for target_id in project["allowed_targets"]
                if self.platforms.target(target_id)["platform_id"] == platform_id
            ]
            blockers: list[dict[str, str]] = []
            runnable_count = 0
            case_results: list[dict[str, Any]] = []
            environment_status = "READY"
            environment_reason_code = ""
            environment_reason_label = ""
            if not targets:
                environment_status = "BLOCKED"
                environment_reason_code = "CAPABILITY_MISSING"
                environment_reason_label = "当前平台没有可用测试目标"
            if platform_id == "579" and targets:
                gateway = self.test_jobs.platform_gateways.get("579")
                config = getattr(getattr(gateway, "transport", None), "config", None)
                if not bool(getattr(config, "enabled", False)):
                    environment_status = "BLOCKED"
                    environment_reason_code = "ENVIRONMENT_BLOCKED"
                    environment_reason_label = "579 运行环境尚未启用，请先在环境中心完成配置与受控验收"
                elif not bool(getattr(config, "device_actions_enabled", False)):
                    environment_status = "BLOCKED"
                    environment_reason_code = "DEVICE_ACTIONS_DISABLED"
                    environment_reason_label = "579 实机动作默认关闭，需完成只读健康检查并单独授权 Canary"
            for row in selected:
                automation = (row.get("platform_automation") or {}).get(platform_id, {})
                applicable = platform_id in (row.get("applicable_platforms") or [])
                runnable = applicable and bool(automation.get("runnable"))
                if applicable and platform_id == "w30" and not automation:
                    runnable = not bool(row.get("unable"))
                reason_code = ""
                reason_label = "可执行"
                if not applicable:
                    reason_code = "PLATFORM_NOT_APPLICABLE"
                    reason_label = "该用例未声明适用于当前平台"
                elif not runnable:
                    reason_code = "BINDING_NOT_READY"
                    reason_label = str(
                        automation.get("blocker_label")
                        or row.get("note")
                        or "尚未完成平台自动化绑定"
                    )
                elif environment_status != "READY":
                    runnable = False
                    reason_code = environment_reason_code
                    reason_label = environment_reason_label
                if runnable:
                    runnable_count += 1
                else:
                    legacy_reason = str(
                        automation.get("blocker")
                        or row.get("note")
                        or reason_code
                    )
                    blockers.append({
                        "case_id": str(row["case_id"]),
                        "reason": legacy_reason,
                        "reason_code": reason_code,
                        "reason_label": reason_label,
                    })
                case_results.append({
                    "case_id": str(row["case_id"]),
                    "runnable": runnable,
                    "reason_code": reason_code,
                    "reason_label": reason_label,
                    "automation_maturity": str(automation.get("maturity") or "UNMAPPED"),
                    "environment_status": environment_status,
                    "binding_version": int(automation.get("binding_version") or 0),
                })
            options.append({
                "platform_id": platform_id,
                "platform_label": self.platforms.platform(platform_id).get("platform_label", platform_id),
                "targets": targets,
                "selected_count": len(selected),
                "runnable_count": runnable_count,
                "blocked_count": len(blockers),
                "runnable": bool(selected) and not blockers and bool(targets),
                "blockers": blockers,
                "cases": case_results,
                "environment_status": environment_status,
            })
        return {
            "project_id": project_id,
            "case_ids": sorted(wanted),
            "options": options,
        }

    def start_repair(self, *, defect: str) -> dict[str, Any]:
        with self._execution_lock:
            if self.test_jobs.active():
                raise RuntimeError("已有 Agent 测试正在运行")
            return self.jobs.start(defect=defect)

    def start_case_test(
        self,
        *,
        sheet: str,
        case_id: str,
        project: str = DEFAULT_TEST_PROJECT,
        platform_id: str | None = None,
        target_id: str | None = None,
    ) -> dict[str, Any]:
        with self._execution_lock:
            if self.jobs.active():
                raise RuntimeError("已有缺陷修复任务正在运行")
            return self.test_jobs.start(
                sheet=sheet,
                case_id=case_id,
                project=project,
                platform_id=platform_id,
                target_id=target_id,
            )

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
        platform_id: str | None = None,
        target_id: str | None = None,
    ) -> dict[str, Any]:
        with self._execution_lock:
            if self.jobs.active():
                raise RuntimeError("已有缺陷修复任务正在运行")
            return self.test_jobs.start_batch(
                limit=limit,
                case_refs=case_refs,
                categories=categories,
                project=project,
                platform_id=platform_id,
                target_id=target_id,
            )

    def resume_batch_test(self, job_id: str) -> dict[str, Any]:
        with self._execution_lock:
            if self.jobs.active():
                raise RuntimeError("已有缺陷修复任务正在运行")
            return self.test_jobs.resume_batch(job_id)



# --- 0.4.0 增强业务辅助方法与协议实现 ---

def _export_cases_xlsx(paths: AppPaths, project: str, cases: CaseMapRepository | None = None) -> bytes:
    """生成标准 9 列表头的 Excel 测试用例工作簿。"""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "自动化测试用例_v1"
    headers = [
        "模块/Sheet", "用例编号", "优先级", "前置条件", "测试步骤", "预期结果",
        "不可自动化", "固化状态", "备注", "适用平台", "用例状态", "当前版本", "来源",
    ]
    ws.append(headers)
    
    rows = cases._all(project=project) if cases is not None else []
    if cases is None:
        project_meta = _test_project(project)
        case_map_root = _case_catalog_root(paths, project_meta)
        for path in sorted(case_map_root.glob("*.json"), key=lambda p: p.stem):
            rows.extend(_case_entries(paths, path.stem, project))
    for item in rows:
        case_id = str(item.get("case_id") or "").strip()
        if not case_id:
            continue
        ws.append([
            str(item.get("sheet") or item.get("file_sheet") or ""),
            case_id,
            str(item.get("priority") or ""),
            str(item.get("precondition_text") or ""),
            str(item.get("steps_text") or ""),
            str(item.get("expected_text") or ""),
            "是" if item.get("unable") else "否",
            str(item.get("mapping_status") or ""),
            str(item.get("note") or ""),
            ",".join(str(value) for value in item.get("applicable_platforms", []) if str(value)),
            str(item.get("workflow_state") or "ACTIVE"),
            int(item.get("current_revision") or 1),
            str(item.get("source_type") or ""),
        ])
                
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
    return {
        "llm": {
            "provider": "builtin",
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
            "port": os.environ.get("W30_HARDWARE_PORT", "COM7"),
            "baudrate": int(os.environ.get("W30_HARDWARE_BAUDRATE", 1500000)),
            "transport": os.environ.get("W30_HARDWARE_TRANSPORT", "supercom"),
            "capture_provider": os.environ.get("W30_HARDWARE_CAPTURE_PROVIDER", "mtp"),
        },
        "simulator": {
            "source_root": os.environ.get("W30_SIMULATOR_SOURCE_ROOT", r"D:\Agent-loop-workspace\620C_W6830"),
            "workspace_root": os.environ.get("W30_SIMULATOR_WORKSPACE_ROOT", r"D:\Agent-loop-workspace\620C_W6830"),
            "simulator_path": os.environ.get("W30_SIMULATOR_PATH", r"D:\Agent-loop-workspace\620C_W6830\core\gui\simulator\bin\main.exe"),
            "hardware_source_root": os.environ.get("W30_HARDWARE_SOURCE_ROOT", r"D:\Agent-loop-workspace\6202_W5230"),
            "hardware_workspace_root": os.environ.get("W30_HARDWARE_WORKSPACE_ROOT", r"D:\Agent-loop-workspace\6202_W5230"),
        },
        "platform_579": {
            "enabled": os.environ.get("PLATFORM_579_ENABLED", "false").lower() == "true",
            "device_actions_enabled": os.environ.get("PLATFORM_579_DEVICE_ACTIONS_ENABLED", "false").lower() == "true",
            "adb_path": os.environ.get("PLATFORM_579_ADB_PATH", ""),
            "adb_serial": (
                "***" + os.environ.get("PLATFORM_579_ADB_SERIAL", "")[-4:]
                if len(os.environ.get("PLATFORM_579_ADB_SERIAL", "")) > 4
                else os.environ.get("PLATFORM_579_ADB_SERIAL", "")
            ),
            "app_package": os.environ.get("PLATFORM_579_APP_PACKAGE", ""),
            "bridge_component": os.environ.get("PLATFORM_579_BRIDGE_COMPONENT", ""),
            "bridge_action": os.environ.get("PLATFORM_579_BRIDGE_ACTION", ""),
            "com_port": os.environ.get("PLATFORM_579_COM_PORT", "COM3"),
            "com_baudrate": int(os.environ.get("PLATFORM_579_COM_BAUDRATE", "1500000")),
            "artifact_root": os.environ.get("PLATFORM_579_ARTIFACT_ROOT", ""),
            "serial_mode": "read_only",
        },
    }


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
            
    if "simulator" in cfg and isinstance(cfg["simulator"], dict):
        sim = cfg["simulator"]
        if "source_root" in sim and sim["source_root"] is not None:
            env_updates["W30_SIMULATOR_SOURCE_ROOT"] = str(sim["source_root"])
        if "workspace_root" in sim and sim["workspace_root"] is not None:
            env_updates["W30_SIMULATOR_WORKSPACE_ROOT"] = str(sim["workspace_root"])
        if "simulator_path" in sim and sim["simulator_path"] is not None:
            env_updates["W30_SIMULATOR_PATH"] = str(sim["simulator_path"])
        if "hardware_source_root" in sim and sim["hardware_source_root"] is not None:
            env_updates["W30_HARDWARE_SOURCE_ROOT"] = str(sim["hardware_source_root"])
        if "hardware_workspace_root" in sim and sim["hardware_workspace_root"] is not None:
            env_updates["W30_HARDWARE_WORKSPACE_ROOT"] = str(sim["hardware_workspace_root"])

    if "platform_579" in cfg and isinstance(cfg["platform_579"], dict):
        platform_579 = cfg["platform_579"]
        if "device_actions_enabled" in platform_579:
            raise ValueError("网页普通配置不能修改 PLATFORM_579_DEVICE_ACTIONS_ENABLED")
        field_map = {
            "enabled": "PLATFORM_579_ENABLED",
            "adb_path": "PLATFORM_579_ADB_PATH",
            "adb_serial": "PLATFORM_579_ADB_SERIAL",
            "app_package": "PLATFORM_579_APP_PACKAGE",
            "bridge_component": "PLATFORM_579_BRIDGE_COMPONENT",
            "bridge_action": "PLATFORM_579_BRIDGE_ACTION",
            "com_port": "PLATFORM_579_COM_PORT",
            "com_baudrate": "PLATFORM_579_COM_BAUDRATE",
            "artifact_root": "PLATFORM_579_ARTIFACT_ROOT",
        }
        unknown = set(platform_579) - set(field_map)
        if unknown:
            raise ValueError(f"579 配置字段不允许: {', '.join(sorted(unknown))}")
        for key, env_key in field_map.items():
            if key in platform_579 and platform_579[key] is not None:
                value = platform_579[key]
                env_updates[env_key] = (
                    "true" if value is True else "false" if value is False else str(value)
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


def _get_environments_status(paths: AppPaths) -> list[dict[str, Any]]:
    """生成全量测试目标的环境就绪状态与检查清单。"""
    items = []
    cfg = _get_system_config(paths)
    for proj_meta in _test_project_options():
        proj_key = str(proj_meta["project"])
        if proj_meta["platform_id"] == "579":
            from agent_loop_system.platforms.platform_579 import Platform579HealthProvider
            item = Platform579HealthProvider().inspect()
            item.update({
                "project": proj_key,
                "project_label": proj_meta["project_label"],
                "last_checked_at": _now(),
                "logs": [{
                    "at": _now(),
                    "message": f"{proj_meta['project_label']} 只读环境自检完成，状态: {item['status']}",
                }],
            })
            items.append(item)
            continue
        checks = []
        is_hardware = proj_meta["execution_target"] == "hardware"
        
        # 1. 源码与工作区
        if proj_key == "620C_W6830":
            src_p = Path(cfg["simulator"]["source_root"])
            status = "pass" if src_p.is_dir() else "warning"
            detail = f"工作区就绪: {src_p}" if status == "pass" else f"工作区目录不存在: {src_p}"
        elif proj_key == "6202_W5230_SIMULATOR":
            src_p = Path(proj_meta.get("simulator_source_root", cfg["simulator"]["source_root"]))
            status = "pass" if src_p.is_dir() else "warning"
            detail = f"工作区就绪: {src_p}" if status == "pass" else f"工作区目录不存在: {src_p}"
        else:
            src_p = Path(cfg["simulator"]["hardware_source_root"])
            status = "pass" if src_p.is_dir() else "warning"
            detail = f"真机工作区就绪: {src_p}" if status == "pass" else f"真机源码目录不存在: {src_p}"
        checks.append({"key": "source", "label": "源码与工作区", "status": status, "detail": detail})
        
        # 2. 项目配置
        case_map_p = _case_catalog_root(paths, proj_meta)
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
            "platform_id": proj_meta["platform_id"],
            "target_id": proj_meta["target_id"],
            "execution_target": proj_meta["execution_target"],
            "execution_target_label": proj_meta["execution_target_label"],
            "transport": proj_meta.get("transport"),
            "transport_label": proj_meta.get("transport_label"),
            "capture_provider": proj_meta.get("capture_provider"),
            "capture_label": proj_meta.get("capture_label"),
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
    return {
        "timestamp": timestamp,
        "sheet": sheet,
        "case_id": _report_cell_text(run.get("case_id")),
        "priority": _report_cell_text(run.get("priority")),
        "verdict": verdict,
        "verdict_label": REPORT_VERDICT_LABELS[verdict],
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


def _style_report_overview_sheets(
    summary_sheet: Any,
    module_sheet: Any,
) -> None:
    summary_sheet.sheet_view.showGridLines = False
    summary_sheet.column_dimensions["A"].width = 20
    summary_sheet.column_dimensions["B"].width = 30
    summary_fill = openpyxl.styles.PatternFill("solid", fgColor="D9EAF7")
    for row in summary_sheet.iter_rows(min_row=1, max_col=2):
        row[0].fill = summary_fill
        row[0].font = openpyxl.styles.Font(bold=True)
        for cell in row:
            cell.alignment = openpyxl.styles.Alignment(vertical="center")

    module_sheet.sheet_view.showGridLines = False
    module_sheet.freeze_panes = "A2"
    module_sheet.auto_filter.ref = f"A1:D{max(module_sheet.max_row, 1)}"
    for column, width in {"A": 20, "B": 14, "C": 14, "D": 14}.items():
        module_sheet.column_dimensions[column].width = width
    header_fill = openpyxl.styles.PatternFill("solid", fgColor="1F4E78")
    header_font = openpyxl.styles.Font(color="FFFFFF", bold=True)
    for cell in module_sheet[1]:
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = openpyxl.styles.Alignment(horizontal="center", vertical="center")


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

    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = f"A1:O{max(sheet.max_row, 1)}"
    sheet.sheet_view.showGridLines = False
    sheet.row_dimensions[1].height = 24
    widths = {
        "A": 20, "B": 14, "C": 16, "D": 10, "E": 14, "F": 14,
        "G": 24, "H": 32, "I": 32, "J": 42, "K": 60, "L": 18,
        "M": 24, "N": 60, "O": 14,
    }
    for column, width in widths.items():
        sheet.column_dimensions[column].width = width

    header_fill = openpyxl.styles.PatternFill("solid", fgColor="1F4E78")
    header_font = openpyxl.styles.Font(color="FFFFFF", bold=True)
    for cell in sheet[1]:
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = openpyxl.styles.Alignment(horizontal="center", vertical="center")

    verdict_fills = {
        "FAIL": openpyxl.styles.PatternFill("solid", fgColor="FCE8E6"),
        "ERROR": openpyxl.styles.PatternFill("solid", fgColor="FEF3C7"),
        "CANNOT_VERIFY": openpyxl.styles.PatternFill("solid", fgColor="E5E7EB"),
    }
    for row in sheet.iter_rows(min_row=2):
        for cell in row:
            cell.alignment = openpyxl.styles.Alignment(vertical="top", wrap_text=True)
        row[0].number_format = "yyyy-mm-dd hh:mm:ss"
        row[4].fill = verdict_fills.get(str(row[4].value or ""), verdict_fills["ERROR"])
        row[4].font = openpyxl.styles.Font(bold=True)
        row[14].alignment = openpyxl.styles.Alignment(horizontal="center", vertical="top")


def _get_reports_summary_data(
    paths: AppPaths,
    history_store: TestHistoryStore,
    project: str,
    date_from: str | None,
    date_to: str | None,
    module_filter: str | None,
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
                        
    # Filter by date
    filtered_runs = []
    for r in all_runs:
        ts = str(r.get("timestamp") or r.get("started_at") or "")
        date_str = ts[:10] if len(ts) >= 10 else ""
        if date_from and date_str and date_str < date_from:
            continue
        if date_to and date_str and date_str > date_to:
            continue
        filtered_runs.append(r)
        
    dist = {"PASS": 0, "FAIL": 0, "ERROR": 0, "CANNOT_VERIFY": 0}
    by_date: dict[str, dict[str, int]] = {}
    module_fails: dict[str, dict[str, int]] = {}
    recent_fails: list[dict[str, Any]] = []
    abnormal_runs: list[dict[str, Any]] = []
    
    for r in filtered_runs:
        v = str(r.get("verdict") or "ERROR").upper()
        if v == "SKIP":
            v = "CANNOT_VERIFY"
        if v not in dist:
            v = "ERROR"
        dist[v] += 1
        
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
        if v in {"FAIL", "ERROR"}:
            if sheet not in module_fails:
                module_fails[sheet] = {"module": sheet, "fail": 0, "error": 0, "total": 0}
            if v == "FAIL":
                module_fails[sheet]["fail"] += 1
            else:
                module_fails[sheet]["error"] += 1
            module_fails[sheet]["total"] += 1
            
            recent_fails.append({
                "case_id": str(r.get("case_id") or ""),
                "sheet": sheet,
                "module": sheet,
                "history_id": str(r.get("id") or ""),
                "verdict": v,
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
            "pass_rate": pass_rate,
            "batches": sum(1 for r in filtered_runs if r.get("batch_id")),
            "repairs": sum(1 for r in filtered_runs if r.get("execution_mode") == "agent_generated"),
        },
        "distribution": dist,
        "trend": trend,
        "top_fail_modules": top_fail_modules,
        "recent_failures": recent_fails[:15],
        "insight": {
            "title": "测试稳定性与质量态势",
            "description": f"已完成 {total_count} 次测试运行，综合通过率为 {pass_rate}%。" + (f" 建议优先关注高频异常模块: {top_fail_modules[0]['module']}。" if top_fail_modules else " 当前运行状态平稳。"),
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

    @staticmethod
    def _error_payload(exc: BaseException, *, fallback: str = "REQUEST_FAILED") -> dict[str, str]:
        message = str(exc)
        match = re.match(r"^([A-Z][A-Z0-9_]+)(?::|$)", message)
        return {"error": message, "error_code": match.group(1) if match else fallback}

    def do_GET(self) -> None:
        try:
            self._get()
        except ValueError as exc:
            self._json(self._error_payload(exc, fallback="INVALID_REQUEST"), HTTPStatus.BAD_REQUEST)
        except BaseException as exc:
            self._json({"error": f"服务器错误: {exc}", "error_code": "INTERNAL_ERROR"}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def do_POST(self) -> None:
        try:
            self._post()
        except ValueError as exc:
            self._json(self._error_payload(exc, fallback="INVALID_REQUEST"), HTTPStatus.BAD_REQUEST)
        except RuntimeError as exc:
            self._json(self._error_payload(exc, fallback="RESOURCE_CONFLICT"), HTTPStatus.CONFLICT)
        except BaseException as exc:
            self._json({"error": f"服务器错误: {exc}", "error_code": "INTERNAL_ERROR"}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def do_PUT(self) -> None:
        try:
            self._put()
        except ValueError as exc:
            self._json(self._error_payload(exc, fallback="INVALID_REQUEST"), HTTPStatus.BAD_REQUEST)
        except RuntimeError as exc:
            self._json(self._error_payload(exc, fallback="RESOURCE_CONFLICT"), HTTPStatus.CONFLICT)
        except BaseException as exc:
            self._json({"error": f"服务器错误: {exc}", "error_code": "INTERNAL_ERROR"}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def do_DELETE(self) -> None:
        try:
            self._delete()
        except ValueError as exc:
            self._json(self._error_payload(exc, fallback="INVALID_REQUEST"), HTTPStatus.BAD_REQUEST)
        except BaseException as exc:
            self._json({"error": f"服务器错误: {exc}", "error_code": "INTERNAL_ERROR"}, HTTPStatus.INTERNAL_SERVER_ERROR)

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

        if path == "/api/platforms":
            self._json(self.app.platforms.public_payload())
            return

        match = re.fullmatch(r"/api/platforms/([^/]+)/capabilities", path)
        if match:
            platform_id = match.group(1)
            platform = self.app.platforms.platform(platform_id)
            targets = self.app.platforms.targets_for(platform_id)
            projects = [
                project for project in self.app.projects.list()
                if platform_id in project["allowed_platforms"]
            ]
            cases = [
                case
                for project in projects
                for case in self.app.cases._all(project=project["project_id"])
            ]
            maturities: dict[str, int] = {}
            for case in cases:
                value = (case.get("platform_automation") or {}).get(platform_id, {})
                maturity = str(value.get("maturity") or "UNMAPPED")
                maturities[maturity] = maturities.get(maturity, 0) + 1
            self._json({
                "platform": platform,
                "targets": targets,
                "project_count": len(projects),
                "maturity_counts": maturities,
            })
            return

        if path == "/api/projects":
            include_archived = query.get("include_archived", ["false"])[0].lower() == "true"
            keyword = query.get("q", [""])[0]
            projects = self.app.projects.list(
                include_archived=include_archived,
                query=keyword,
            )
            self._json({"items": projects, "total": len(projects)})
            return

        match = re.fullmatch(r"/api/projects/([^/]+)/execution-options", path)
        if match:
            raw_case_ids = query.get("case_id", [])
            if not raw_case_ids:
                comma_values = query.get("case_ids", [""])[0]
                raw_case_ids = [value for value in comma_values.split(",") if value]
            self._json(self.app.execution_options(match.group(1), case_ids=raw_case_ids))
            return

        match = re.fullmatch(r"/api/projects/([^/]+)", path)
        if match:
            project = self.app.projects.get(match.group(1))
            project["execution_options"] = self.app.execution_options(match.group(1))["options"]
            self._json(project)
            return

        if path == "/api/tests/overview":
            project = query.get("project_id", query.get("project", [DEFAULT_TEST_PROJECT]))[0]
            platform_id = query.get("platform_id", [None])[0]
            if platform_id:
                _test_project(project, platform_id=platform_id)
            limit = self._positive_int(query, "limit", 20, maximum=100)
            payload = self.app.cases.overview(project=project, limit=limit)
            if platform_id:
                payload["platform_id"] = platform_id
            self._json(payload)
            return

        if path in {"/api/tests", "/api/cases"}:
            page = self._positive_int(query, "page", 1)
            page_size = self._positive_int(query, "page_size", 20, maximum=100)
            keyword = query.get("q", [""])[0]
            state_filter = query.get("state", ["all"])[0]
            project = query.get("project_id", query.get("project", [DEFAULT_TEST_PROJECT]))[0]
            platform_id = query.get("platform_id", [None])[0]
            modules = {
                str(value).strip()
                for value in query.get("module", [])
                if str(value).strip()
            }
            if platform_id:
                _test_project(project, platform_id=platform_id)
            payload = self.app.cases.list(
                    query=keyword,
                    page=page,
                    page_size=page_size,
                    state_filter=state_filter,
                    project=project,
                    modules=modules or None,
                )
            if platform_id:
                payload["platform_id"] = platform_id
            self._json(payload)
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

                if project and j_proj != project and project != "all":
                    continue

                if j_status in {"queued", "running", "finalizing"}:
                    queued_cnt += 1
                if j_status in {"completed", "done"}:
                    fin = str(j.get("finished_at") or j.get("created_at") or "")
                    if fin.startswith(today_str):
                        completed_today += 1
                if j_verdict in {"FAIL", "ERROR"} or j_status == "failed":
                    error_cnt += 1
                    
                if status_filter == "queue":
                    if j_status != "queued":
                        continue
                elif status_filter == "running":
                    if j_status not in {"running", "finalizing", "queued"}:
                        continue
                elif status_filter == "completed":
                    if j_status not in {"completed", "done"}:
                        continue
                elif status_filter == "interrupted":
                    if j_status not in {"cancelled", "interrupted", "failed"}:
                        continue
                        
                meta = _test_project(j_proj)
                items.append({
                    "id": jid,
                    "project": j_proj,
                    "project_label": meta["project_label"],
                    "status": j_status,
                    "verdict": j_verdict,
                    "completed": j.get("completed", 1 if j_status in {"completed", "done"} else 0),
                    "total": j.get("total", 1),
                    "started_at": j.get("started_at") or j.get("created_at"),
                    "finished_at": j.get("finished_at"),
                    "requested_platform_id": str(
                        j.get("requested_platform_id") or j.get("platform_id") or meta["platform_id"]
                    ),
                    "target_id": str(j.get("target_id") or meta["target_id"]),
                    "resolved_execution_adapter": str(
                        j.get("resolved_execution_adapter")
                        or j.get("execution_adapter")
                        or meta["execution_adapter"]
                    ),
                    "automation_maturity": str(j.get("automation_maturity") or "UNKNOWN"),
                    "infrastructure_status": str(j.get("infrastructure_status") or "UNKNOWN"),
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
            project = query.get("project_id", query.get("project", [DEFAULT_TEST_PROJECT]))[0]
            platform_id = query.get("platform_id", [None])[0]
            target_id = query.get("target_id", [None])[0]
            if platform_id or target_id:
                _test_project(project, platform_id=platform_id, target_id=target_id)
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
            data = _export_cases_xlsx(self.app.paths, project, self.app.cases)
            self._serve_binary(data, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", f"test_cases_{project}.xlsx")
            return

        match = re.fullmatch(r"/api/cases/([^/]+)/revisions", path)
        if match:
            project = query.get("project_id", query.get("project", [DEFAULT_TEST_PROJECT]))[0]
            self._json({
                "project_id": project,
                "case_id": match.group(1),
                "items": self.app.case_store.revisions(project, match.group(1)),
            })
            return

        match = re.fullmatch(r"/api/cases/([^/]+)/bindings", path)
        if match:
            project = query.get("project_id", query.get("project", [DEFAULT_TEST_PROJECT]))[0]
            case = self.app.case_store.get_case(project, match.group(1))
            self._json(
                {"project_id": project, "case_id": match.group(1), "bindings": case.get("platform_automation", {})}
                if case else {"error": "CASE_NOT_FOUND"},
                HTTPStatus.OK if case else HTTPStatus.NOT_FOUND,
            )
            return

        match = re.fullmatch(r"/api/cases/import/([^/]+)/errors", path)
        if match:
            self._json({"batch_id": match.group(1), "items": self.app.case_store.import_errors(match.group(1))})
            return

        match = re.fullmatch(r"/api/cases/import/([^/]+)", path)
        if match:
            batch = self.app.case_store.import_batch(match.group(1))
            self._json(batch if batch else {"error": "IMPORT_BATCH_NOT_FOUND"}, HTTPStatus.OK if batch else HTTPStatus.NOT_FOUND)
            return

        match = re.fullmatch(r"/api/cases/([^/]+)/audit", path)
        if match:
            project = query.get("project_id", query.get("project", [DEFAULT_TEST_PROJECT]))[0]
            self._json({
                "project_id": project,
                "case_id": match.group(1),
                "items": self.app.case_store.audit_events(project, match.group(1)),
            })
            return

        match = re.fullmatch(r"/api/cases/([^/]+)", path)
        if match and match.group(1) not in {"migration-candidates"}:
            project = query.get("project_id", query.get("project", [DEFAULT_TEST_PROJECT]))[0]
            self.app.cases._all(project=project)
            case = self.app.case_store.get_case(project, match.group(1))
            self._json(case if case else {"error": "CASE_NOT_FOUND"}, HTTPStatus.OK if case else HTTPStatus.NOT_FOUND)
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
            summary_data = _get_reports_summary_data(
                self.app.paths,
                self.app.test_history,
                project,
                d_from,
                d_to,
                module,
            )
            self._json(summary_data)
            return

        if path == "/api/reports/runs":
            project = query.get("project", [DEFAULT_TEST_PROJECT])[0]
            scope = query.get("scope", ["batch"])[0]
            d_from = query.get("from", [None])[0]
            d_to = query.get("to", [None])[0]
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
                            d_str = ts[:10] if len(ts) >= 10 else ""
                            if d_from and d_str and d_str < d_from:
                                continue
                            if d_to and d_str and d_str > d_to:
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
                                    d_str = ts[:10] if len(ts) >= 10 else ""
                                    if d_from and d_str and d_str < d_from:
                                        continue
                                    if d_to and d_str and d_str > d_to:
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
            summary_data = _get_reports_summary_data(
                self.app.paths,
                self.app.test_history,
                project,
                d_from,
                d_to,
                module,
                include_abnormal_runs=True,
            )
            
            wb = openpyxl.Workbook()
            ws_summary = wb.active
            ws_summary.title = "测试报告概览"
            ws_summary.append(["测试项目", summary_data.get("project", project)])
            ws_summary.append(["统计周期", f"{d_from or '全部'} 至 {d_to or '全部'}"])
            ws_summary.append(["总执行数", summary_data["metrics"]["total"]])
            ws_summary.append(["通过 (PASS)", summary_data["metrics"]["pass"]])
            ws_summary.append(["失败 (FAIL)", summary_data["metrics"]["fail"]])
            ws_summary.append(["错误 (ERROR)", summary_data["metrics"]["error"]])
            ws_summary.append(["无法验证", summary_data["metrics"]["cannot_verify"]])
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
        if path == "/api/projects":
            body = self._body_json()
            project = self.app.projects.create(body)
            self._json(project, HTTPStatus.CREATED)
            return

        match = re.fullmatch(r"/api/projects/([^/]+)/archive", path)
        if match:
            self._json(self.app.projects.archive(match.group(1)))
            return

        if path == "/api/tests/run-batch":
            body = self._body_json()
            has_explicit_routing = "platform_id" in body or "target_id" in body
            project_value = str(body.get("project_id") or body.get("project") or DEFAULT_TEST_PROJECT)
            platform_id = str(body.get("platform_id") or "").strip() or None
            target_id = str(body.get("target_id") or "").strip() or None
            resolved = _test_project(
                project_value,
                platform_id=platform_id,
                target_id=target_id,
            )
            project = resolved["project"]
            platform_id = resolved["platform_id"]
            target_id = resolved["target_id"]
            routing_kwargs = (
                {"platform_id": platform_id, "target_id": target_id}
                if has_explicit_routing else {}
            )
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
                job = self.app.start_batch_test(
                    limit=limit, project=project, **routing_kwargs,
                )
            elif categories is not None:
                job = self.app.start_batch_test(
                    limit=limit, categories=categories, project=project,
                    **routing_kwargs,
                )
            else:
                job = self.app.start_batch_test(
                    limit=limit, case_refs=case_refs, project=project,
                    **routing_kwargs,
                )
            self._json(job, HTTPStatus.ACCEPTED)
            return

        match = re.fullmatch(r"/api/tests/jobs/([^/]+)/cancel", path)
        if match:
            self._json(self.app.test_jobs.cancel_batch(match.group(1)), HTTPStatus.ACCEPTED)
            return

        match = re.fullmatch(r"/api/tests/jobs/([^/]+)/resume", path)
        if match:
            self._json(self.app.resume_batch_test(match.group(1)), HTTPStatus.ACCEPTED)
            return

        if path == "/api/cases/execution-options":
            body = self._body_json()
            project = str(body.get("project_id") or body.get("project") or DEFAULT_TEST_PROJECT)
            raw_case_ids = body.get("case_ids") or body.get("case_id") or []
            if isinstance(raw_case_ids, str):
                raw_case_ids = [raw_case_ids]
            if not isinstance(raw_case_ids, list):
                raise ValueError("case_ids 必须为数组")
            self._json(self.app.execution_options(project, case_ids=raw_case_ids))
            return

        if path == "/api/tests/run":
            body = self._body_json()
            has_explicit_routing = "platform_id" in body or "target_id" in body
            project_value = str(body.get("project_id") or body.get("project") or DEFAULT_TEST_PROJECT)
            platform_id = str(body.get("platform_id") or "").strip() or None
            target_id = str(body.get("target_id") or "").strip() or None
            resolved = _test_project(
                project_value,
                platform_id=platform_id,
                target_id=target_id,
            )
            project = resolved["project"]
            sheet = body.get("sheet")
            case_id = body.get("case_id")
            if not isinstance(sheet, str) or not sheet.strip():
                raise ValueError("sheet 必填")
            if not isinstance(case_id, str) or not case_id.strip():
                raise ValueError("case_id 必填")
            routing_kwargs = (
                {
                    "platform_id": resolved["platform_id"],
                    "target_id": resolved["target_id"],
                }
                if has_explicit_routing else {}
            )
            job = self.app.start_case_test(
                sheet=sheet.strip(), case_id=case_id.strip(), project=project,
                **routing_kwargs,
            )
            self._json(job, HTTPStatus.ACCEPTED)
            return

        if path in {"/api/cases", "/api/cases/create"}:
            body = self._body_json()
            project = str(body.get("project_id") or body.get("project") or DEFAULT_TEST_PROJECT)
            case_obj = body.get("case")
            if case_obj is None and path == "/api/cases":
                case_obj = body
            if not isinstance(case_obj, dict):
                raise ValueError("case 字段必填且必须为对象")
            project_meta = _test_project(project)
            created = self.app.case_store.create_case(project_meta, case_obj)
            managed_case = self.app.case_store.get_case(project, created["case_id"])
            if managed_case and path == "/api/cases/create":
                _write_unified_compat_shadow(self.app.paths, project_meta, managed_case)
            self._json({"status": "ok", **created}, HTTPStatus.CREATED if path == "/api/cases" else HTTPStatus.OK)
            return

        if path == "/api/cases/update":
            body = self._body_json()
            project = str(body.get("project_id") or body.get("project") or DEFAULT_TEST_PROJECT)
            orig_case_id = str(body.get("orig_case_id") or "").strip()
            case_obj = body.get("case")
            if not isinstance(case_obj, dict):
                raise ValueError("case 字段必填且必须为对象")
            if not orig_case_id:
                raise ValueError("原用例编号 orig_case_id 必填")
            project_meta = _test_project(project)
            updated = self.app.case_store.create_revision(
                project_meta, orig_case_id, case_obj,
                change_summary=str(body.get("change_summary") or "网页编辑"),
            )
            managed_case = self.app.case_store.get_case(project, orig_case_id)
            if managed_case:
                _write_unified_compat_shadow(self.app.paths, project_meta, managed_case)
            self._json({"status": "ok", **updated})
            return

        match = re.fullmatch(r"/api/cases/([^/]+)/revisions", path)
        if match:
            body = self._body_json()
            project = str(body.get("project_id") or body.get("project") or DEFAULT_TEST_PROJECT)
            project_meta = _test_project(project)
            changes = body.get("case") if isinstance(body.get("case"), dict) else body.get("changes")
            if not isinstance(changes, dict):
                raise ValueError("case 或 changes 字段必填")
            result = self.app.case_store.create_revision(
                project_meta, match.group(1), changes,
                change_summary=str(body.get("change_summary") or "创建新版本"),
            )
            self._json({"status": "ok", **result}, HTTPStatus.CREATED)
            return

        match = re.fullmatch(r"/api/cases/([^/]+)/(archive|restore)", path)
        if match:
            body = self._body_json() if self.headers.get("Content-Length") else {}
            project = str(body.get("project_id") or body.get("project") or DEFAULT_TEST_PROJECT)
            result = self.app.case_store.set_workflow_state(
                _test_project(project), match.group(1),
                "ARCHIVED" if match.group(2) == "archive" else "ACTIVE",
            )
            self._json({"status": "ok", **result})
            return

        match = re.fullmatch(r"/api/cases/([^/]+)/clone", path)
        if match:
            body = self._body_json()
            project = str(body.get("project_id") or body.get("project") or DEFAULT_TEST_PROJECT)
            result = self.app.case_store.clone_case(
                _test_project(project), match.group(1), str(body.get("new_case_id") or ""),
            )
            self._json({"status": "ok", **result}, HTTPStatus.CREATED)
            return

        match = re.fullmatch(
            r"/api/cases/([^/]+)/bindings/([^/]+)/(candidate|review|promote|rollback)",
            path,
        )
        if match:
            body = self._body_json()
            project = str(body.get("project_id") or body.get("project") or DEFAULT_TEST_PROJECT)
            case_id, platform_id, action = match.groups()
            self.app.cases._all(project=project)
            if action == "promote" and platform_id == "579":
                case = self.app.case_store.get_case(project, case_id)
                binding = ((case or {}).get("platform_automation") or {}).get("579", {})
                candidate = binding.get("candidate") if isinstance(binding, dict) else None
                gateway = self.app.test_jobs.platform_gateways.get("579")
                catalog = getattr(gateway, "catalog", None)
                if not isinstance(candidate, dict) or catalog is None:
                    raise ValueError("CAPABILITY_MISSING: 579 正式动作注册表尚未绑定")
                plan = catalog.private_plan(case_id)
                if (
                    str(candidate.get("binding_ref") or "") != str(plan.get("automation_case_id") or "")
                    or str(candidate.get("plan_sha256") or "").upper() != str(plan.get("_plan_sha256") or "").upper()
                ):
                    raise ValueError("CAPABILITY_MISSING: 候选引用与受保护动作注册表不一致")
            result = self.app.case_store.transition_binding(
                _test_project(project), case_id, platform_id, action,
                body.get("binding") if isinstance(body.get("binding"), dict)
                else {key: value for key, value in body.items() if key not in {"project", "project_id"}},
            )
            self._json({"status": "ok", **result}, HTTPStatus.CREATED if action == "candidate" else HTTPStatus.OK)
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
                
            tgt_root = _case_catalog_root(self.app.paths, tgt_meta)
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

        if path in {"/api/excel/preview", "/api/cases/import/preview"}:
            body = self._body_json()
            project = str(body.get("project_id") or body.get("project") or DEFAULT_TEST_PROJECT)
            b64_data = str(body.get("file_base64") or "").strip()
            if not b64_data:
                raise ValueError("未提供 Excel 文件内容 (file_base64)")

            project_meta = _test_project(project)
            # 导入冲突判断必须同时覆盖尚未写入业务库的冻结/Case Map 基线。
            self.app.cases._all(project=project)
            parsed = _parse_excel_cases(b64_data)
            applicable_platforms = body.get("applicable_platforms")
            if not isinstance(applicable_platforms, list) or not applicable_platforms:
                if path == "/api/cases/import/preview":
                    raise ValueError("IMPORT_PLATFORM_REQUIRED: 导入前必须明确选择适用平台")
                applicable_platforms = list(project_meta.get("allowed_platforms") or [])
            for case in parsed:
                case["applicable_platforms"] = applicable_platforms
            try:
                file_bytes = base64.b64decode(b64_data, validate=True)
            except ValueError as exc:
                raise ValueError("EXCEL_BASE64_INVALID") from exc
            source_sha256 = hashlib.sha256(file_bytes).hexdigest().upper()
            conflict_strategy = str(body.get("conflict_strategy") or "").strip().upper()
            if not conflict_strategy:
                conflict_strategy = "NEW_REVISION" if bool(body.get("overwrite_existing")) else "SKIP"
            preview = self.app.case_store.preview_import(
                project_meta,
                parsed,
                source_filename=str(body.get("filename") or body.get("file_name") or "import.xlsx"),
                source_sha256=source_sha256,
                conflict_strategy=conflict_strategy,
            )
            self._json(preview)
            return

        if path in {"/api/excel/confirm", "/api/cases/import/commit"}:
            body = self._body_json()
            project = str(body.get("project_id") or body.get("project") or DEFAULT_TEST_PROJECT)
            project_meta = _test_project(project)
            self.app.cases._all(project=project)
            batch_id = str(body.get("batch_id") or "").strip()
            preview_token = str(body.get("preview_token") or "").strip()
            source_sha256 = str(body.get("source_sha256") or "").strip()

            # 保留旧客户端兼容：未携带预览令牌时，先在服务端重新生成一次预览，
            # 但仍只提交 SQLite 业务库，绝不覆盖冻结 Manifest / Case Map。
            if not batch_id or not preview_token or not source_sha256:
                b64_data = str(body.get("file_base64") or "").strip()
                if not b64_data:
                    raise ValueError("IMPORT_PREVIEW_REQUIRED: 请先完成导入预览")
                applicable_platforms = body.get("applicable_platforms")
                if not isinstance(applicable_platforms, list) or not applicable_platforms:
                    applicable_platforms = list(project_meta.get("allowed_platforms") or [])
                parsed = _parse_excel_cases(b64_data)
                for case in parsed:
                    case["applicable_platforms"] = applicable_platforms
                try:
                    file_bytes = base64.b64decode(b64_data, validate=True)
                except ValueError as exc:
                    raise ValueError("EXCEL_BASE64_INVALID") from exc
                source_sha256 = hashlib.sha256(file_bytes).hexdigest().upper()
                strategy = "NEW_REVISION" if bool(body.get("overwrite_existing")) else "SKIP"
                preview = self.app.case_store.preview_import(
                    project_meta,
                    parsed,
                    source_filename=str(body.get("filename") or body.get("file_name") or "import.xlsx"),
                    source_sha256=source_sha256,
                    conflict_strategy=str(body.get("conflict_strategy") or strategy),
                )
                batch_id = preview["batch_id"]
                preview_token = preview["preview_token"]

            result = self.app.case_store.commit_import(
                project_meta,
                batch_id=batch_id,
                preview_token=preview_token,
                source_sha256=source_sha256,
            )
            self._json(result)
            return

        match = re.fullmatch(r"/api/environments/([^/]+)/check", path)
        if match:
            proj = match.group(1)
            envs = _get_environments_status(self.app.paths)
            target_env = next((
                e for e in envs
                if e.get("id") == proj or e.get("project") == proj or e.get("target_id") == proj
            ), None)
            if target_env is None:
                raise ValueError(f"TARGET_NOT_FOUND: {proj}")
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
            base_url = str(body.get("base_url", "")).strip() or "https://ones.topstepht.com:8443"
            if not email or not password:
                raise ValueError("请提供 ONES 账号和密码")

            import urllib.request
            import urllib.error
            import ssl
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE

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

        if path != "/api/run":
            self._json({"error": "接口不存在"}, HTTPStatus.NOT_FOUND)
            return

        body = self._body_json()
        if not isinstance(body.get("defect"), str) or not body["defect"].strip():
            raise ValueError("defect 必填")
        job = self.app.start_repair(defect=body["defect"].strip())
        self._json(job, HTTPStatus.ACCEPTED)

    def _put(self) -> None:
        path = urlparse(self.path).path
        project_match = re.fullmatch(r"/api/projects/([^/]+)", path)
        if project_match:
            self._json(
                self.app.projects.update(project_match.group(1), self._body_json())
            )
            return
        match = re.fullmatch(r"/api/environments/([^/]+)", path)
        if match:
            requested_environment = match.group(1)
            try:
                target_id = self.app.platforms.target(requested_environment)["target_id"]
            except ValueError:
                target_id = _test_project(requested_environment)["target_id"]
            body = self._body_json()
            if target_id == "579.o2":
                payload = body.get("platform_579", body)
                if not isinstance(payload, dict):
                    raise ValueError("platform_579 配置必须是对象")
                _save_system_config(self.app.paths, {"platform_579": payload})
                self._json({
                    "status": "ok",
                    "message": "579 环境配置已保存；实机动作总开关未改变",
                })
                return
            paths_obj = body.get("paths", {})
            if isinstance(paths_obj, dict):
                config_update = {"simulator": {}}
                if target_id == "w30.6202.hardware":
                    if "source_root" in paths_obj:
                        config_update["simulator"]["hardware_source_root"] = paths_obj["source_root"]
                    if "workspace_root" in paths_obj:
                        config_update["simulator"]["hardware_workspace_root"] = paths_obj["workspace_root"]
                elif target_id == "w30.6202.simulator":
                    if "source_root" in paths_obj:
                        os.environ["W30_6202_SIMULATOR_SOURCE_ROOT"] = str(paths_obj["source_root"])
                    if "build_directory" in paths_obj:
                        os.environ["W30_6202_SIMULATOR_BUILD_DIRECTORY"] = str(paths_obj["build_directory"])
                    if "artifact_path" in paths_obj:
                        os.environ["W30_6202_SIMULATOR_ARTIFACT_PATH"] = str(paths_obj["artifact_path"])
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
        match = re.fullmatch(r"/api/cases/([^/]+)", path)
        if match:
            project = query.get("project_id", query.get("project", [DEFAULT_TEST_PROJECT]))[0]
            result = self.app.case_store.set_workflow_state(
                _test_project(project), match.group(1), "ARCHIVED"
            )
            self._json({"status": "ok", **result})
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="W30 Agent 自闭环前端服务")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址")
    parser.add_argument("--port", type=int, default=8765, help="监听端口")
    args = parser.parse_args(argv)

    root = Path(__file__).resolve().parent.parent
    app = WebApplication(AppPaths.from_root(root))
    server = FrontendHTTPServer((args.host, args.port), make_handler(app))
    print(f"W30 Agent UI: http://{args.host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n服务已停止")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
