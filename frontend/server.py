"""W30 Agent 自闭环演示前端的零依赖 HTTP 服务。

启动：uv run python frontend/server.py --port 8765
"""
from __future__ import annotations

import argparse
import base64
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

from agent_loop_system.tools.case_map import validated_case_entries
from agent_loop_system.tools.external_execution_history import (
    read_external_execution_history,
)


WORKFLOW_NODES = (
    "validate", "interactive_reproduce", "agent", "apply", "build", "test", "record"
)
TEST_WORKFLOW_NODES = ("load", "execute", "judge", "record")
SAFE_SEGMENT = re.compile(r"^[A-Za-z0-9_.-]+$")
TEST_SCREENSHOT_FILE = re.compile(r"^screenshot(?:-\d{2,3})?\.bmp$")
MAX_BODY_BYTES = 64 * 1024
MAX_LOG_CHARS = 200_000
HISTORY_SCHEMA_VERSION = 2
DEFECT_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
BATCH_STATE_FILE = "batch-state.json"
BATCH_CASES_FILE = "batch-cases.json"
DEFAULT_TEST_PROJECT = "620C_W6830"
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
    },
}


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


def _test_project(value: str | None = None) -> dict[str, str]:
    """返回前端允许选择的项目；项目同时决定映射目录与执行目标。"""

    project = str(value or DEFAULT_TEST_PROJECT).strip()
    try:
        return dict(TEST_PROJECTS[project])
    except KeyError as exc:
        raise ValueError(f"测试项目不存在: {project or '空'}") from exc


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

    @classmethod
    def from_root(cls, root: Path) -> "AppPaths":
        root = root.resolve()
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
        run_dir.mkdir(parents=True, exist_ok=False)
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
            "verdict": result.get("verdict", "ERROR"),
            "reason": result.get("reason") or job.get("error") or "",
            "execution_mode": result.get("execution_mode", "fixed_mapping"),
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
    """case_map 的只读查询视图。"""

    FILTERS = {
        "all", "unexplored", "externally_explored",
        "explored_unsolidified", "solidified",
    }
    RUN_CATEGORIES = {"untested", "pass", "fail", "cannot_verify", "error"}
    BATCH_CATEGORIES = RUN_CATEGORIES - {"pass"}

    def __init__(self, paths: AppPaths, history: TestHistoryStore):
        self.paths = paths
        self.history = history

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
                latest = history.get("latest") if history else None
                row["last_run_at"] = latest.get("timestamp") if latest else None
                row["history_count"] = int(history.get("history_count") or 0) if history else 0
                latest_verdict = str(latest.get("verdict") or "").upper() if latest else ""
                if row["history_count"] == 0:
                    row["latest_verdict"] = "PENDING"
                elif latest_verdict == "SKIP":
                    row["latest_verdict"] = "CANNOT_VERIFY"
                elif latest_verdict in {"PASS", "FAIL", "CANNOT_VERIFY", "ERROR"}:
                    row["latest_verdict"] = latest_verdict
                else:
                    row["latest_verdict"] = "ERROR"
                row["external_explored"] = case_id in externally_explored_ids
                row["is_promoted"] = (
                    row["external_explored"]
                    and row["mapping_status"] == "PROMOTED"
                )
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
    ) -> dict[str, Any]:
        project_meta = _test_project(project)
        project = project_meta["project"]
        state_filter = str(state_filter or "all").strip().lower()
        if state_filter not in self.FILTERS:
            raise ValueError("state 参数不合法")
        rows = self._all(project)
        batch_summary = {
            category: sum(self.run_category(row) == category for row in rows)
            for category in self.RUN_CATEGORIES
        }
        summary = {
            "all": len(rows),
            "unexplored": sum(not row["external_explored"] for row in rows),
            "externally_explored": sum(row["external_explored"] for row in rows),
            "explored_unsolidified": sum(
                row["external_explored"] and not row["is_promoted"] for row in rows
            ),
            "solidified": sum(row["is_promoted"] for row in rows),
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
        if state_filter == "unexplored":
            rows = [row for row in rows if not row["external_explored"]]
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
            "state_filter": state_filter,
            **{key: project_meta[key] for key in (
                "project", "project_label", "execution_target", "execution_target_label"
            )},
            "projects": _test_project_options(),
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

    def __init__(self, paths: AppPaths, cases: CaseMapRepository, history: TestHistoryStore):
        self.paths = paths
        self.cases = cases
        self.history = history
        self._lock = threading.Lock()
        self._jobs: dict[str, dict[str, Any]] = {}
        self._active_job_ids: dict[str, str] = {}
        self._load_batches()

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
    ) -> dict[str, Any]:
        project_meta = _test_project(project)
        project = project_meta["project"]
        case = self.cases.get(sheet, case_id, project=project)
        if case is None:
            raise ValueError("测试用例不存在")
        with self._lock:
            job_id = uuid.uuid4().hex[:12]
            job = {
                "id": job_id,
                "type": "single",
                "sheet": sheet,
                "case_id": case_id,
                **{key: project_meta[key] for key in (
                    "project", "project_label", "execution_target", "execution_target_label"
                )},
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
            }
            self._claim_execution_slot_locked(job)
            self._jobs[job_id] = job
            self._persist_batch_locked(job)
        thread = threading.Thread(target=self._run, args=(job_id,), daemon=True, name=f"case-test-{job_id}")
        thread.start()
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
            self._persist_batch_locked(job)
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
                "current_runtime_archived",
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
    ) -> dict[str, Any]:
        project_meta = _test_project(str(case.get("project") or DEFAULT_TEST_PROJECT))
        job_dir.mkdir(parents=True, exist_ok=True)
        result_file = job_dir / "test_result.json"
        screenshot = job_dir / "screenshot.bmp"
        started_at = _now()
        argv = [
            sys.executable,
            "-m",
            "agent_loop_system.tools.test",
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
            job["error"] = (
                execution["execution_reason"] if execute_failed else None
            )
            job["nodes"]["execute"] = "fail" if execute_failed else "pass"
            job["nodes"]["judge"] = "pass" if verdict == "PASS" else "fail"
            job["nodes"]["record"] = "running"
            job["current_node"] = "record"
            job["status"] = "finalizing"

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

        with self._lock:
            job = self._jobs[job_id]
            job["history_id"] = history_id
            job["nodes"]["record"] = "pass" if history_id else "fail"
            job["current_node"] = None
            job["status"] = "completed" if history_id else "failed"
            job.pop("process", None)
            self._release_execution_slot_locked(job_id)

    def _run_batch(self, job_id: str) -> None:
        with self._lock:
            job = self._jobs[job_id]
            execution_target = str(job.get("execution_target") or "simulator")

        if execution_target == "hardware":
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
                    job["error"] = infrastructure_failure
                    job["interruption_reason"] = interruption_reason
                    job["finished_at"] = _now()
                    job["current_node"] = None
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
                job["completed"] = index
                job["verdict_counts"][execution["verdict"]] += 1
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

        argv = [
            sys.executable,
            "-m",
            "agent_loop_system",
            "--defect",
            job["defect"],
            "--task-id",
            job["defect"],
            "--progress-file",
            str(progress_file),
            "--result-file",
            str(result_file),
        ]
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
        self.history = HistoryStore(paths)
        self.test_history = TestHistoryStore(paths)
        self.defects = DefectRepository(paths, self.history)
        self.cases = CaseMapRepository(paths, self.test_history)
        self.jobs = JobManager(paths, self.defects, self.history)
        self.test_jobs = CaseTestManager(paths, self.cases, self.test_history)
        self._execution_lock = threading.Lock()
        self.import_jobs: dict[str, dict[str, Any]] = {}
        self._import_lock = threading.Lock()

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
    ) -> dict[str, Any]:
        with self._execution_lock:
            if self.jobs.active():
                raise RuntimeError("已有缺陷修复任务正在运行")
            return self.test_jobs.start(sheet=sheet, case_id=case_id, project=project)

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
                
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _parse_excel_cases(file_base64: str) -> list[dict[str, Any]]:
    """解析 base64 编码的 Excel 用例表格。"""
    try:
        data = base64.b64decode(file_base64)
    except Exception as exc:
        raise ValueError(f"Base64 数据解码失败: {exc}")
    try:
        wb = openpyxl.load_workbook(io.BytesIO(data), data_only=True)
    except Exception as exc:
        raise ValueError(f"Excel 文件无法解析: {exc}")
        
    sheet_name = "自动化测试用例_v1" if "自动化测试用例_v1" in wb.sheetnames else wb.sheetnames[0]
    ws = wb[sheet_name]
    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        raise ValueError("Excel 表格为空")
        
    header_idx = 0
    col_map: dict[str, int] = {}
    for idx, row in enumerate(rows[:5]):
        if not row:
            continue
        row_str = [str(c).strip() if c is not None else "" for c in row]
        temp_map = {}
        for c_idx, cell in enumerate(row_str):
            if not cell:
                continue
            if any(k in cell for k in ["模块", "Sheet", "sheet"]):
                temp_map["sheet"] = c_idx
            elif any(k in cell for k in ["用例编号", "用例ID", "case_id", "编号"]):
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
            
    if "case_id" not in col_map and "sheet" not in col_map:
        raise ValueError("未识别到有效的用例表头（需包含'用例编号'或'模块'）")
        
    parsed_cases: list[dict[str, Any]] = []
    for row_num, row in enumerate(rows[header_idx + 1:], start=header_idx + 2):
        if not row or all(c is None or str(c).strip() == "" for c in row):
            continue
        case_id = str(row[col_map["case_id"]]).strip() if "case_id" in col_map and col_map["case_id"] < len(row) and row[col_map["case_id"]] is not None else ""
        sheet = str(row[col_map["sheet"]]).strip() if "sheet" in col_map and col_map["sheet"] < len(row) and row[col_map["sheet"]] is not None else "通用"
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
            "mapping_status": mapping_status,
            "note": note,
            "_row_number": row_num,
        })
    return parsed_cases


def _get_system_config(paths: AppPaths) -> dict[str, Any]:
    """读取当前运行时系统配置。"""
    from agent_loop_system.tools.llm_config import get_llm_config
    llm_cfg = get_llm_config()
    return {
        "llm": {
            "provider": "builtin",
            "api_key": (llm_cfg["api_key"][:3] + "..." + llm_cfg["api_key"][-4:]) if llm_cfg["api_key"] else "",
            "base_url": llm_cfg["base_url"],
            "model": llm_cfg["model"],
            "timeout": int(llm_cfg["timeout"]),
            "is_builtin": llm_cfg["is_builtin"],
            "status": "ready",
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
    for proj_key, proj_meta in TEST_PROJECTS.items():
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


def _get_reports_summary_data(paths: AppPaths, history_store: TestHistoryStore, project: str, date_from: str | None, date_to: str | None, module_filter: str | None) -> dict[str, Any]:
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
    
    total_count = len(filtered_runs)
    pass_rate = round(dist["PASS"] * 100.0 / total_count, 1) if total_count > 0 else 0.0
    
    return {
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

        if path == "/api/tests":
            page = self._positive_int(query, "page", 1)
            page_size = self._positive_int(query, "page_size", 20, maximum=100)
            keyword = query.get("q", [""])[0]
            state_filter = query.get("state", ["all"])[0]
            project = query.get("project", [DEFAULT_TEST_PROJECT])[0]
            self._json(
                self.app.cases.list(
                    query=keyword,
                    page=page,
                    page_size=page_size,
                    state_filter=state_filter,
                    project=project,
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
                
                if j_status in {"queued", "running", "finalizing"}:
                    queued_cnt += 1
                if j_status in {"completed", "done"}:
                    fin = str(j.get("finished_at") or j.get("created_at") or "")
                    if fin.startswith(today_str):
                        completed_today += 1
                if j_verdict in {"FAIL", "ERROR"} or j_status == "failed":
                    error_cnt += 1
                    
                if project and j_proj != project and project != "all":
                    continue
                    
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
            summary_data = _get_reports_summary_data(self.app.paths, self.app.test_history, project, d_from, d_to, module)
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
            summary_data = _get_reports_summary_data(self.app.paths, self.app.test_history, project, d_from, d_to, module)
            
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
            self._json(self.app.test_jobs.cancel_batch(match.group(1)), HTTPStatus.ACCEPTED)
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
                
            project_meta = _test_project(project)
            latest_history = self.app.test_history.latest(sheet, case_id, project=project)
            if not latest_history:
                self._json({"status": "failed", "audit": {"passed": False, "issues": ["未找到历史运行记录，必须先运行并通过测试"]}}, HTTPStatus.BAD_REQUEST)
                return
                
            verdict = str(latest_history.get("verdict") or "ERROR").upper()
            if verdict != "PASS":
                self._json({"status": "failed", "audit": {"passed": False, "issues": [f"最新运行结果为 {verdict}，必须为 PASS 才能晋升"]}}, HTTPStatus.BAD_REQUEST)
                return
                
            case_root = self.app.paths.case_map / project_meta["case_map_dir"]
            sheet_f = case_root / f"{sheet}.json"
            raw = _read_json(sheet_f, [])
            is_env = isinstance(raw, dict) and "cases" in raw
            clist = raw.get("cases") if is_env else (raw if isinstance(raw, list) else [])
            found = False
            for c in clist:
                if isinstance(c, dict) and c.get("case_id") == case_id:
                    c["mapping_status"] = "PROMOTED"
                    found = True
                    break
            if found:
                if is_env:
                    raw["cases"] = clist
                    _write_json(sheet_f, raw)
                else:
                    _write_json(sheet_f, clist)
                    
            ledger_file = case_root / "external_execution_history.jsonl"
            ledger_record = {
                "case_id": case_id,
                "sheet": sheet,
                "target": project,
                "last_verified": datetime.now().astimezone().strftime("%Y-%m-%d"),
                "evidence_root": str(self.app.test_history._case_dir(sheet, case_id, project=project)),
                "evidence_paths": [f"{latest_history.get('id')}/run.json"],
            }
            with ledger_file.open("a", encoding="utf-8") as f:
                f.write(json.dumps(ledger_record, ensure_ascii=False) + "\n")
                
            self._json({"status": "ok", "audit": {"passed": True, "issues": []}})
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
                launch_update_script(self.app.paths.root, staging_dir, parent_pid=os.getpid())
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
        match = re.fullmatch(r"/api/environments/([^/]+)", path)
        if match:
            project = match.group(1)
            body = self._body_json()
            paths_obj = body.get("paths", {})
            if isinstance(paths_obj, dict):
                config_update = {"simulator": {}}
                if project == "6202_W5230":
                    if "source_root" in paths_obj:
                        config_update["simulator"]["hardware_source_root"] = paths_obj["source_root"]
                    if "workspace_root" in paths_obj:
                        config_update["simulator"]["hardware_workspace_root"] = paths_obj["workspace_root"]
                elif project == "6202_W5230_SIMULATOR":
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
        argv = [
            sys.executable, "-m", "agent_loop_system.tools.defect_store",
            "--import-all",
        ]
        if force:
            argv.append("--force")
        if include_completed:
            argv.append("--include-completed")
        if limit and limit > 0:
            argv.extend(["--limit", str(limit)])
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