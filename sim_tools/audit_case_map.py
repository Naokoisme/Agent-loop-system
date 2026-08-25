"""对 case_map 做快速、可重复的静态验收。

只检查能由源码和 JSON 确认的事实，不替代 Simulator 产品验收。
"""
from __future__ import annotations

import argparse
import json
import os
import re
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path

from pydantic import ValidationError

from agent_loop_system.runtime_root import RuntimePaths, resolve_config_path
from agent_loop_system.tools.quick_command_source import (
    resolve_project_command_source,
)
from agent_loop_system.tools.case_map import (
    CaseEntry,
    OBSERVATION_ONLY_COMMANDS,
    case_map_dir_for_target,
    validated_case_entries,
)
from agent_loop_system.tools.command_protocol import normalize_command
from agent_loop_system.tools.external_execution_history import (
    ExternalExecutionHistoryError,
    ExternalExecutionRecord,
    read_external_execution_history,
)
from sim_tools.extract_kb import extract_commands, extract_windows


ROOT = Path(__file__).resolve().parents[1]
CASE_MAP_DIR = case_map_dir_for_target("simulator")

CLICK_TERMS = ("点击", "单击", "点按", "确认按钮", "取消按钮")
SWIPE_TERMS = ("滑动", "上滑", "下滑", "左滑", "右滑", "拖动")
BUTTON_TERMS = (
    "实体按键",
    "按压编码器",
    "单击编码器",
    "双击编码器",
    "长按编码器",
    "短按编码器",
    "按下按键",
    "长按按键",
    "短按按键",
)
TOUCH_PRESS_TERMS = ("长按",)
ROTATE_TERMS = ("旋转编码器", "顺时针旋转", "逆时针旋转", "编码器一格")
WAIT_TERMS = ("等待", "保持无操作", "静置", "无操作")
LEDGER_TARGETS = {
    "620C_simulator_case_map": "620C_W6830",
    "6202_case_map": "6202_W5230",
    "6202_simulator_case_map": "6202_W5230_SIMULATOR",
}


@dataclass(frozen=True)
class AuditIssue:
    severity: str
    code: str
    file: str
    case_id: str
    detail: str


def _contains(text: str, terms: tuple[str, ...]) -> bool:
    return any(term in text for term in terms)


def _requires_click(text: str) -> bool:
    if _contains(text, CLICK_TERMS):
        return True
    # “滚动选择”描述滚轮/编码器改变选中项，不要求额外点击确认。
    if "滚动选择" in text:
        return False
    # “选择页/选择器”是控件或页面名；“停止触摸”描述抬手，均不是点击动作。
    if re.search(r"选择(?!页|界面|器|列表)", text):
        return True
    return bool(re.search(r"(?<!停止)触摸", text))


def _command_name(wire: str) -> str:
    raw = normalize_command(wire)
    return raw[1:].partition(":")[0]


def _live_capabilities(target: str = "simulator") -> tuple[set[str], set[str]]:
    normalized_target = str(target or "").strip().lower()
    if normalized_target == "hardware":
        from agent_loop_system.tools.hardware_target import (
            HardwareTargetConfig,
            hardware_command_allowed,
        )

        config = HardwareTargetConfig.from_env()
        source_root = config.source_root
        command_source = config.command_source
        project_cmake = config.project_cmake
        app_windows = config.app_windows
        app_quick_cmd = config.app_quick_cmd
    elif normalized_target == "simulator":
        source_root = resolve_config_path(
            os.environ.get(
                "W30_SOURCE_ROOT",
                RuntimePaths.from_root().firmware_workspaces / "620C_W6830",
            )
        )
        project = os.environ.get("W30_PROJECT", "620C_W6830")
        command_source = resolve_project_command_source(source_root)
        project_cmake = source_root / f"app/projects/{project}/Project.cmake"
        app_windows = source_root / "app/windows"
        app_quick_cmd = source_root / "app/comm/TuoBu/quick_cmd/gui_comm_quick_cmd.c"
    else:
        raise ValueError(f"未知 case_map 目标: {target!r}")

    command_text = extract_commands(
        command_source,
        app_root=source_root / "app",
    )
    window_text = extract_windows(
        project_cmake=project_cmake,
        app_windows=app_windows,
        app_quick_cmd=app_quick_cmd,
    )
    commands = {
        line.split(" |", 1)[0].strip()
        for line in command_text.splitlines()
        if line and not line.startswith("#")
    }
    if normalized_target == "hardware":
        commands = {
            name for name in commands if hardware_command_allowed(name)[0]
        }
    # HOST_WAIT/HOST_SCREENSHOT 由 Runner 在电脑端执行，不属于固件命令表。
    commands.update({"HOST_WAIT", "HOST_SCREENSHOT"})
    windows = {
        line.split(" -> ", 1)[1].split(" |", 1)[0].strip()
        for line in window_text.splitlines()
        if " -> " in line and " |" in line
    }
    return commands, windows


def audit_case_maps(
    case_map_dir: Path | None = None,
    *,
    target: str = "simulator",
) -> tuple[dict[str, int], list[AuditIssue]]:
    if case_map_dir is None:
        case_map_dir = case_map_dir_for_target(target)
    live_commands, live_windows = (
        _live_capabilities() if target == "simulator" else _live_capabilities(target)
    )
    issues: list[AuditIssue] = []
    counts = Counter(
        files=0,
        total=0,
        unexplored=0,
        externally_explored=0,
        explored_unsolidified=0,
        solidified=0,
    )
    seen_case_ids: dict[str, str] = {}
    seen_case_sheets: dict[str, str] = {}
    ledger_ids: set[str] = set()
    ledger_records: dict[str, ExternalExecutionRecord] = {}
    ledger_path = case_map_dir / "external_execution_history.jsonl"
    ledger_valid = True
    try:
        records = read_external_execution_history(
            ledger_path,
            expected_target=LEDGER_TARGETS.get(case_map_dir.name),
        )
    except ExternalExecutionHistoryError as exc:
        ledger_valid = False
        location = (
            exc.case_id
            or (f"line:{exc.line_number}" if exc.line_number is not None else "<ledger>")
        )
        issues.append(AuditIssue(
            "error",
            exc.code,
            ledger_path.name,
            location,
            exc.message,
        ))
    else:
        ledger_ids = {record.case_id for record in records}
        ledger_records = {record.case_id: record for record in records}

    for path in sorted(case_map_dir.glob("*.json")):
        counts["files"] += 1
        data = json.loads(path.read_text(encoding="utf-8"))
        try:
            raw_entries = validated_case_entries(
                data,
                sheet_name=path.stem,
                expected_profile=LEDGER_TARGETS.get(case_map_dir.name),
                path=path.resolve(),
            )
        except ValueError as exc:
            issues.append(AuditIssue(
                "error",
                "case_map_metadata_mismatch",
                path.name,
                "<file>",
                str(exc),
            ))
            continue
        for raw in raw_entries:
            counts["total"] += 1
            raw_case_id = str(raw.get("case_id", "<missing>"))
            previous_file = seen_case_ids.get(raw_case_id)
            if previous_file is not None:
                issues.append(AuditIssue(
                    "error",
                    "duplicate_case_id",
                    path.name,
                    raw_case_id,
                    f"already defined in {previous_file}",
                ))
            else:
                seen_case_ids[raw_case_id] = path.name
                seen_case_sheets[raw_case_id] = str(raw.get("sheet") or path.stem)
            try:
                case = CaseEntry(**raw)
            except ValidationError as exc:
                issues.append(AuditIssue(
                    "error",
                    "invalid_case_schema",
                    path.name,
                    str(raw.get("case_id", "<missing>")),
                    str(exc).replace("\n", " "),
                ))
                continue
            label = (path.name, case.case_id)

            external_explored = case.case_id in ledger_ids
            if external_explored:
                counts["externally_explored"] += 1
            if case.is_promoted and external_explored:
                counts["solidified"] += 1
            elif external_explored:
                counts["explored_unsolidified"] += 1
            else:
                counts["unexplored"] += 1

            if ledger_valid and case.is_promoted and not external_explored:
                issues.append(AuditIssue(
                    "error",
                    "promoted_without_external_exploration",
                    *label,
                    "PROMOTED 用例必须先存在于同目标外部探索账本",
                ))

            if not case.is_promoted:
                if case.mapping_status:
                    issues.append(AuditIssue(
                        "error",
                        "invalid_mapping_status",
                        *label,
                        "正式状态只允许精确的 PROMOTED",
                    ))
                if any(getattr(case, field) for field in (
                    "setup", "actions", "collect", "verification_points"
                )):
                    issues.append(AuditIssue(
                        "error",
                        "unsolidified_has_mapping",
                        *label,
                        "未固化用例不得长期保留固定步骤或验证点",
                    ))
                continue

            phase_names: dict[str, list[str]] = {}
            for phase in ("setup", "actions", "collect"):
                phase_names[phase] = []
                for wire in getattr(case, phase):
                    try:
                        name = _command_name(wire)
                    except ValueError as exc:
                        issues.append(AuditIssue("error", "invalid_wire", *label, f"{phase}: {exc}"))
                        continue
                    phase_names[phase].append(name)
                    if name not in live_commands:
                        issues.append(AuditIssue("error", "unknown_command", *label, f"{phase}: {name}"))
                    if name == "SLEEP_RECORD_CREATE":
                        raw_command = normalize_command(wire)
                        args = [
                            item.strip()
                            for item in raw_command.split(":", 2)[2].split(",")
                        ]
                        first_arg = args[0] if args else ""
                        if first_arg not in {"0", "1"}:
                            issues.append(AuditIssue(
                                "error",
                                "invalid_sleep_record_mode",
                                *label,
                                f"{phase}: first argument must be 0 or 1, got {first_arg!r}",
                            ))
                        elif (first_arg == "0" and len(args) != 1) or (
                            first_arg == "1" and len(args) != 6
                        ):
                            issues.append(AuditIssue(
                                "error",
                                "invalid_sleep_record_arguments",
                                *label,
                                f"{phase}: mode 0 takes no values; mode 1 takes five values",
                            ))
                    if name == "ENTER_PAGE":
                        raw_command = normalize_command(wire)
                        window = raw_command.split(":", 2)[2].split(",", 1)[0]
                        if window not in live_windows:
                            issues.append(AuditIssue("error", "unknown_window", *label, f"{phase}: {window}"))

            action_names = set(phase_names["actions"])
            steps = case.steps_text
            operation_rules = (
                ("missing_swipe", SWIPE_TERMS, {"SWIPE_SIM", "TP_SWIPE"}),
                ("missing_button", BUTTON_TERMS, {"BUTTON_PRESS"}),
                ("missing_rotate", ROTATE_TERMS, {"QDEC_SET"}),
                ("missing_wait", WAIT_TERMS, {"HOST_WAIT", "SIM_WAIT"}),
            )
            all_names = set(sum(phase_names.values(), []))
            if _requires_click(steps) and not action_names.intersection({"TP_CLICK", "BUTTON_PRESS"}):
                issues.append(AuditIssue(
                    "error", "missing_click", *label, "人工步骤缺少 ['BUTTON_PRESS', 'TP_CLICK']"
                ))
            if (
                _contains(steps, TOUCH_PRESS_TERMS)
                and not _contains(steps, BUTTON_TERMS)
                and "TP_PRESS" not in action_names
            ):
                issues.append(AuditIssue(
                    "error", "missing_touch_press", *label, "人工长按屏幕步骤缺少 ['TP_PRESS']"
                ))
            for code, terms, required in operation_rules:
                available = all_names if code == "missing_wait" else action_names
                if _contains(steps, terms) and not available.intersection(required):
                    issues.append(AuditIssue("error", code, *label, f"人工步骤缺少 {sorted(required)}"))

            checkpoint_count = sum(
                name in {"GUI_TREE", "HOST_SCREENSHOT"}
                for names in phase_names.values()
                for name in names
            )
            if case.verification_points and len(case.verification_points) != checkpoint_count:
                issues.append(AuditIssue(
                    "error",
                    "verification_checkpoint_mismatch",
                    *label,
                    f"verification_points={len(case.verification_points)}, checkpoints={checkpoint_count}",
                ))

            expected_items = len(re.findall(r"(?m)^\s*\d+[.、）]", case.expected_text))
            business_actions = sum(
                name not in OBSERVATION_ONLY_COMMANDS
                for name in phase_names["actions"]
            )
            if (
                expected_items >= 2
                and business_actions >= 2
                and checkpoint_count == 1
                and not case.verification_points
            ):
                issues.append(AuditIssue(
                    "warning",
                    "multi_stage_single_checkpoint",
                    *label,
                    f"expected_items={expected_items}, actions={business_actions}",
                ))

    for case_id in sorted(ledger_ids - set(seen_case_ids)):
        issues.append(AuditIssue(
            "error",
            "external_ledger_case_missing",
            ledger_path.name,
            case_id,
            "外部探索账本引用了当前 case_map 中不存在的 case_id",
        ))
    for case_id in sorted(ledger_ids.intersection(seen_case_ids)):
        record = ledger_records[case_id]
        expected_sheet = seen_case_sheets[case_id]
        if record.sheet != expected_sheet:
            issues.append(AuditIssue(
                "error",
                "external_ledger_sheet_mismatch",
                ledger_path.name,
                case_id,
                f"sheet 必须是 {expected_sheet}",
            ))

    return dict(counts), issues


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    parser.add_argument(
        "--target",
        choices=("simulator", "hardware"),
        default="simulator",
        help="检查 620C 模拟器或 6202 真机映射",
    )
    args = parser.parse_args(argv)
    counts, issues = audit_case_maps(target=args.target)
    errors = [issue for issue in issues if issue.severity == "error"]
    warnings = [issue for issue in issues if issue.severity == "warning"]
    if args.json:
        print(json.dumps({
            "counts": counts,
            "error_count": len(errors),
            "warning_count": len(warnings),
            "issues": [asdict(issue) for issue in issues],
        }, ensure_ascii=False, indent=2))
    else:
        print(f"counts={counts}")
        print(f"errors={len(errors)} warnings={len(warnings)}")
        for issue in issues:
            print(f"{issue.severity}\t{issue.code}\t{issue.file}\t{issue.case_id}\t{issue.detail}")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
