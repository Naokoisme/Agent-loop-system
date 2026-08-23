"""前端运行进度报告。

CLI 默认不启用；Web 后端通过 ``--progress-file`` 指定文件后，图节点会把
当前节点和八节点状态原子写入 JSON，供 HTTP API 轮询。
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Callable


WORKFLOW_NODES = (
    "validate", "interactive_reproduce", "agent", "apply", "build", "test", "record"
)

_lock = threading.Lock()
_progress_path: Path | None = None
_progress: dict[str, Any] = {}


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    os.replace(temporary, path)


def configure(
    progress_file: str | Path | None,
    *,
    task_id: str = "",
    execution_context: dict[str, Any] | None = None,
) -> None:
    """启用或关闭当前进程的进度文件。"""
    global _progress_path, _progress
    with _lock:
        _progress_path = Path(progress_file).resolve() if progress_file else None
        _progress = {
            "task_id": task_id,
            "execution_context": dict(execution_context or {}),
            "status": "running",
            "current_node": None,
            "nodes": {node: "pending" for node in WORKFLOW_NODES},
            "attempts": 0,
            "verdict": "PENDING",
            "workflow_status": "running",
            "execution_status": "PENDING",
            "evidence_status": "PENDING",
            "mapping_status": "NOT_APPLICABLE",
            "reason_code": None,
            "error": None,
            "updated_at": _now(),
        }
        if _progress_path:
            _write_json_atomic(_progress_path, _progress)


def _flush() -> None:
    if _progress_path:
        _progress["updated_at"] = _now()
        _write_json_atomic(_progress_path, _progress)


def node_started(name: str, state: dict[str, Any]) -> None:
    if not _progress_path:
        return
    with _lock:
        attempts = int(state.get("attempts", 0) or 0)
        if name == "agent" and attempts > 0:
            for retry_node in ("agent", "apply", "build", "test", "record"):
                _progress["nodes"][retry_node] = "pending"
        _progress["status"] = "running"
        _progress["current_node"] = name
        _progress["nodes"][name] = "running"
        _progress["attempts"] = attempts
        _flush()


def _node_failed(name: str, update: dict[str, Any]) -> bool:
    if name == "interactive_reproduce" and update.get("error"):
        return True
    # CANNOT_VERIFY 不是节点执行失败；最终 verdict 单独显示无法验证
    if update.get("verdict") == "CANNOT_VERIFY":
        return False
    if name in {"validate", "agent", "apply"} and update.get("error"):
        return True
    if name == "build" and update.get("build_success") is False:
        return True
    if name == "test" and update.get("verdict") in {"FAIL", "ERROR"}:
        return True
    return False


def node_finished(name: str, state: dict[str, Any], update: dict[str, Any]) -> None:
    if not _progress_path:
        return
    with _lock:
        _progress["nodes"][name] = "fail" if _node_failed(name, update) else "pass"
        _progress["current_node"] = None
        _progress["attempts"] = int(update.get("attempts", state.get("attempts", 0)) or 0)
        if "verdict" in update:
            _progress["verdict"] = update["verdict"]
        if update.get("error"):
            _progress["error"] = update["error"]
        _flush()


def node_crashed(name: str, error: BaseException) -> None:
    if not _progress_path:
        return
    with _lock:
        _progress["nodes"][name] = "fail"
        _progress["current_node"] = name
        _progress["status"] = "failed"
        _progress["error"] = str(error)
        _flush()


def finish(result: dict[str, Any] | None = None, *, error: str | None = None) -> None:
    if not _progress_path:
        return
    with _lock:
        result = result or {}
        workflow_status = str(
            result.get("workflow_status") or ("failed" if error else "completed")
        ).lower()
        _progress["status"] = workflow_status
        _progress["workflow_status"] = workflow_status
        _progress["current_node"] = None
        _progress["verdict"] = result.get("verdict", _progress["verdict"])
        for key in (
            "execution_status", "evidence_status", "mapping_status", "reason_code",
        ):
            if result.get(key) is not None:
                _progress[key] = result[key]
        _progress["attempts"] = int(result.get("attempts", _progress["attempts"]) or 0)
        _progress["error"] = error or result.get("error")
        _progress["finished_at"] = _now()
        _flush()


def tracked_node(name: str, function: Callable[[dict[str, Any]], dict[str, Any]]):
    """给 LangGraph 节点增加轻量进度上报，不改变节点输入输出。"""
    def wrapped(state: dict[str, Any]) -> dict[str, Any]:
        node_started(name, state)
        try:
            update = function(state)
        except BaseException as exc:
            node_crashed(name, exc)
            raise
        node_finished(name, state, update)
        return update

    wrapped.__name__ = function.__name__
    wrapped.__doc__ = function.__doc__
    return wrapped


def write_result(path: str | Path, result: dict[str, Any]) -> None:
    _write_json_atomic(Path(path).resolve(), result)
