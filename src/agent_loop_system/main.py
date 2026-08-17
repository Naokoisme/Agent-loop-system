"""CLI 入口：解析参数 → 加载 .env → invoke graph → 打印 verdict。

用法：
    uv run python -m agent_loop_system --task-id T1 --objective "修复计算器 2+3=6 的 bug"
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


def _load_env(env_path: Path | None = None) -> None:
    """从 .env 文件加载环境变量到 os.environ（不覆盖已存在的）。

    用标准库 parse，避免引入 python-dotenv 依赖。
    """
    path = env_path or Path(__file__).resolve().parents[2] / ".env"
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def _auto_source_files(defect: dict, limit: int = 5) -> list[str]:
    """从 defect.source_analysis.matches 按原顺序提取路径，Windows 不区分大小写去重。"""
    files: list[str] = []
    seen: set[str] = set()
    for m in defect.get("source_analysis", {}).get("matches", []):
        rel = str(m.get("path", "")).strip()
        key = rel.casefold()
        if rel and key not in seen:
            seen.add(key)
            files.append(rel)
        if len(files) >= limit:
            break
    return files


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agent_loop_system")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--defect", help="ONES 缺陷编号，从 ONES 获取 objective")
    group.add_argument("--objective", help="bug 描述/修复目标")
    parser.add_argument("--task-id", default=None, help="任务标识（默认用 defect 编号）")
    parser.add_argument("--max-attempts", type=int, default=5, help="重试上限")
    parser.add_argument(
        "--target",
        choices=("simulator", "hardware"),
        default="simulator",
        help="执行目标；hardware 使用当前真机项目的 UART 控制并通过 MTP 获取截图",
    )
    parser.add_argument(
        "--test-case",
        action="append",
        default=[],
        help="测试用例 sheet:case_id，可重复指定多个",
    )
    parser.add_argument(
        "--source-file",
        action="append",
        default=[],
        help="相关源码文件路径（相对 W30_SOURCE_ROOT），可重复",
    )
    parser.add_argument(
        "--progress-file",
        default=None,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--result-file",
        default=None,
        help=argparse.SUPPRESS,
    )
    args = parser.parse_args(argv)

    _load_env()

    # 缺陷来源：--defect 从本地缺陷库读取，或 --objective 直接传入
    if args.defect:
        from agent_loop_system.tools.defect_store import (
            build_judge_criteria,
            build_objective,
            list_defect_images,
            load_defect,
        )

        defect = load_defect(args.defect)
        if defect is None:
            print(f"[main] 本地缺陷库未找到 {args.defect}")
            print(
                f"[main] 请先入库: "
                f"uv run python -m agent_loop_system.tools.defect_store "
                f"--import {args.defect}"
            )
            return 1
        objective = build_objective(defect)
        task_id = args.task_id or args.defect
        judge_criteria = build_judge_criteria(defect)
        defect_image_paths = [str(path) for path in list_defect_images(defect["number"])]
        print(f"[main] 缺陷 {defect['number']}: {defect['title']}")
        if args.source_file:
            source_files = list(args.source_file)
        else:
            # 自动填充：按 matches 原顺序提取路径，Windows 不区分大小写去重，最多 5 个
            source_files = _auto_source_files(defect)
    else:
        objective = args.objective
        task_id = args.task_id or "default"
        # --objective 模式：以 objective 原文作为判定依据，不再置空
        judge_criteria = (args.objective or "").strip()
        defect_image_paths = []
        source_files = list(args.source_file)

    test_cases = []
    for tc in args.test_case:
        sheet, _, case_id = tc.partition(":")
        if sheet and case_id:
            test_cases.append({"sheet": sheet, "case_id": case_id})

    from agent_loop_system import reporting
    from agent_loop_system.graph import build_graph

    reporting.configure(args.progress_file, task_id=task_id)
    graph = build_graph()
    try:
        result = graph.invoke(
            {
                "task_id": task_id,
                "objective": objective,
                "judge_criteria": judge_criteria,
                "defect_image_paths": defect_image_paths,
                "max_attempts": args.max_attempts,
                "target": args.target,
                "test_cases": test_cases,
                "source_files": source_files,
                "designer_enabled": os.environ.get("DESIGNER_ENABLED", "0").strip().casefold()
                in {"1", "true", "yes", "on"},
            }
        )
    except BaseException as exc:
        error_result = {
            "task_id": task_id,
            "verdict": "FAIL",
            "attempts": 0,
            "history": [],
            "error": f"{type(exc).__name__}: {exc}",
        }
        error_result = _normalize_result(error_result)
        reporting.finish(error_result, error=error_result["error"])
        if args.result_file:
            reporting.write_result(args.result_file, error_result)
        print(f"error: {error_result['error']}", file=sys.stderr)
        return 1

    result = _normalize_result(result)
    reporting.finish(result)
    if args.result_file:
        reporting.write_result(args.result_file, result)

    print(f"task_id: {result.get('task_id')}")
    print(f"verdict: {result.get('verdict')}")
    print(f"attempts: {result.get('attempts')}")
    print(f"reproduction_attempts: {result.get('reproduction_attempts')}")
    print(f"reproduction_outcome: {result.get('reproduction_outcome')}")
    if result.get("error"):
        print(f"error: {result['error']}")
    test_output = result.get("test_output")
    if test_output:
        print(f"test_output: {test_output}")
    print(f"history rounds: {len(result.get('history', []))}")
    return 0 if result.get("verdict") == "PASS" else 1


_RESULT_KEYS = (
    "task_id",
    "verdict",
    "attempts",
    "reproduction_attempts",
    "reproduction_reason",
    "reproduction_outcome",
    "reproduction_trace",
    "agent_test_commands",
    "patch",
    "patch_retained",
    "baseline_output",
    "build_success",
    "build_result",
    "test_output",
    "history",
    "error",
    "error_code",
    "rollback_error",
    "restore_build_result",
    "restore_build_error",
)


def _normalize_result(result: dict) -> dict:
    """对齐第 13 节结果契约：未执行的阶段字段保留但值为 None，不省略。"""
    return {key: result.get(key) for key in _RESULT_KEYS}


if __name__ == "__main__":
    sys.exit(main())
