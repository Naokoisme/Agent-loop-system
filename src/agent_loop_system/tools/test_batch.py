"""批量执行 case_map：每个模块复用一个 Simulator，会话外并发做 LLM 判定。"""
from __future__ import annotations

import argparse
import json
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

from agent_loop_system.main import _load_env
from agent_loop_system.tools.case_map import (
    CaseRunResult,
    case_map_dir_for_target,
    load_case_map,
    run_case,
)
from agent_loop_system.tools.simulator import SimulatorSession
from agent_loop_system.tools.test import (
    CaseDecision,
    get_simulator_exe,
    judge_case_result,
    save_evidence,
)


CASE_MAP_DIR = case_map_dir_for_target("simulator")
DEFAULT_OUTPUT_ROOT = Path(r"D:\Agent-loop-system\evidence\batch")


def _judgement_text(result: CaseRunResult) -> str:
    if not result.verification_points:
        return result.expected_text
    points = "\n".join(
        f"{index}. {point}"
        for index, point in enumerate(result.verification_points, start=1)
    )
    return f"{result.expected_text}\n\n本次自动验证点：\n{points}"


def _sheet_names(requested: list[str] | None) -> list[str]:
    if requested:
        return requested
    return sorted(path.stem for path in CASE_MAP_DIR.glob("*.json"))


def run_batch(
    *,
    sheets: list[str] | None = None,
    output_root: Path,
    judge_workers: int = 4,
    screenshots: bool = True,
    judge: bool = True,
    max_cases: int | None = None,
) -> dict[str, object]:
    output_root.mkdir(parents=True, exist_ok=True)
    futures: dict[Future[CaseDecision], tuple[CaseRunResult, Path]] = {}
    records: list[dict[str, object]] = []
    executed = 0

    with ThreadPoolExecutor(max_workers=max(1, judge_workers)) as executor:
        for sheet_name in _sheet_names(sheets):
            cases = load_case_map(sheet_name)
            runnable = [case for case in cases.values() if not case.unable]
            if max_cases is not None:
                remaining = max_cases - executed
                if remaining <= 0:
                    break
                runnable = runnable[:remaining]
            if not runnable:
                continue

            print(f"[batch] {sheet_name}: {len(runnable)} cases")
            session = SimulatorSession(get_simulator_exe())
            session.start()
            try:
                for index, case in enumerate(runnable, start=1):
                    case_dir = output_root / sheet_name / case.case_id
                    screenshot_path = case_dir / "screenshot.bmp" if screenshots else None
                    result = run_case(session, case, screenshot_path=screenshot_path)
                    executed += 1
                    evidence_path = case_dir / "result.json"
                    print(
                        f"[batch] {sheet_name} {index}/{len(runnable)} {case.case_id} "
                        f"aborted={result.aborted} evidence={len(result.terminal_json)}"
                    )
                    if not judge:
                        decision = CaseDecision(
                            verdict="SKIP" if result.skipped else "CANNOT_VERIFY",
                            reason="用例标记为 unable，未执行" if result.skipped else "本次批量运行关闭了视觉判定",
                        )
                        save_evidence(result, decision, evidence_path)
                        records.append({
                            "sheet": sheet_name,
                            "case_id": case.case_id,
                            "verdict": decision.verdict,
                            "reason": decision.reason,
                            "evidence": str(evidence_path),
                        })
                        continue
                    future = executor.submit(judge_case_result, result)
                    futures[future] = (result, evidence_path)
            finally:
                session.stop()

        for future in as_completed(futures):
            result, evidence_path = futures[future]
            try:
                decision = future.result()
            except Exception as exc:  # 单条模型异常不能丢失整批证据
                decision = CaseDecision(verdict="CANNOT_VERIFY", reason=f"批量判定异常: {exc}")
            save_evidence(result, decision, evidence_path)
            records.append({
                "sheet": result.sheet,
                "case_id": result.case_id,
                "verdict": decision.verdict,
                "reason": decision.reason,
                "evidence": str(evidence_path),
            })

    verdict_counts: dict[str, int] = {}
    for record in records:
        verdict = str(record["verdict"])
        verdict_counts[verdict] = verdict_counts.get(verdict, 0) + 1
    summary: dict[str, object] = {
        "executed": executed,
        "record_count": len(records),
        "verdict_counts": verdict_counts,
        "records": sorted(records, key=lambda item: (str(item["sheet"]), str(item["case_id"]))),
    }
    (output_root / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return summary


def main(argv: list[str] | None = None) -> int:
    _load_env()
    parser = argparse.ArgumentParser(prog="agent_loop_system.tools.test_batch")
    parser.add_argument("--sheet", action="append", dest="sheets")
    parser.add_argument("--output-root")
    parser.add_argument("--judge-workers", type=int, default=4)
    parser.add_argument("--max-cases", type=int)
    parser.add_argument("--no-screenshot", action="store_true")
    parser.add_argument("--no-judge", action="store_true")
    args = parser.parse_args(argv)
    stamp = datetime.now().astimezone().strftime("%Y%m%dT%H%M%S")
    output_root = Path(args.output_root) if args.output_root else DEFAULT_OUTPUT_ROOT / stamp
    summary = run_batch(
        sheets=args.sheets,
        output_root=output_root,
        judge_workers=args.judge_workers,
        screenshots=not args.no_screenshot,
        judge=not args.no_judge,
        max_cases=args.max_cases,
    )
    print(f"[batch] summary={output_root / 'summary.json'}")
    print(f"[batch] verdict_counts={summary['verdict_counts']}")
    return 0 if set(summary["verdict_counts"]) <= {"PASS", "SKIP"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
