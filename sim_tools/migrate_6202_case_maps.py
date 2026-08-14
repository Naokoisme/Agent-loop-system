"""Conservatively migrate 620C case maps to the 6202 hardware profile.

This is a data migration helper.  It never opens hardware or edits firmware.
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from agent_loop_system.tools.command_protocol import normalize_command
from agent_loop_system.tools.hardware_serial import dangerous_command_reason
from agent_loop_system.tools.hardware_target import (
    HardwareTargetConfig,
    hardware_command_allowed,
)
from sim_tools.extract_kb import extract_commands


ROOT = Path(__file__).resolve().parents[1]
SOURCE_DIR = ROOT / "case_map" / "620C_case_map"
TARGET_DIR = ROOT / "case_map" / "6202_case_map"

DELEGATED_SHEETS = {
    "呼吸训练",
    "计时器",
    "秒表",
    "闹钟",
    "日历",
    "手电筒",
    "心率",
    "血氧",
    "压力",
    "一键测量",
    "睡眠",
    "世界时钟",
}
SKIP_SHEETS = {"计算器"}
TOUCH_COMMANDS = {"TP_CLICK", "TP_PRESS", "TP_SWIPE", "SWIPE_SIM"}
EXTERNAL_RE = re.compile(
    r"手机|蓝牙|来电|通话|通知|音乐|相机|APP|网络|GPS",
    re.IGNORECASE,
)
BASELINE = [
    "srv_quick_cmd send TOP5STEP:LANGUAGE_SET:1;",
    "srv_quick_cmd send TOP5STEP:ENTER_PAGE:DIAL,0;",
]


def _command_name(wire: str) -> str:
    raw = normalize_command(wire)
    return raw[1:].partition(":")[0].upper()


def _wire(name: str, arguments: str) -> str:
    return f"srv_quick_cmd send TOP5STEP:{name}:{arguments};"


def _capabilities(config: HardwareTargetConfig) -> set[str]:
    catalog = extract_commands(config.command_source, app_root=config.source_root / "app")
    return {
        line.split(" |", 1)[0].strip().upper()
        for line in catalog.splitlines()
        if line and not line.startswith("#")
    }


def _convert_wire(wire: str) -> tuple[str | None, str | None]:
    normalized = normalize_command(wire)
    name, _, arguments = normalized[1:].partition(":")
    name = name.upper()
    if name == "GUI_TREE":
        return _wire("HOST_SCREENSHOT", arguments or "1"), None
    if name == "SCREENSHOT_PRINT":
        return None, None
    if name == "SIM_WAIT":
        return _wire("HOST_WAIT", arguments), None
    if name.startswith("SIM_"):
        return None, f"{name} 仅用于620C模拟器"
    if name == "TEST_SESSION":
        return None, "TEST_SESSION 由真机会话层管理"
    if name in TOUCH_COMMANDS:
        return None, f"{name} 缺少6202已验证坐标"
    unsafe = dangerous_command_reason(normalized)
    if unsafe:
        return None, f"危险真机命令 {name}: {unsafe}"
    allowed, reason = hardware_command_allowed(name)
    if not allowed:
        return None, reason or f"{name} 不允许发送到真机"
    return _wire(name, arguments), None


def _migrate_case(raw: dict, live_commands: set[str]) -> dict:
    migrated = dict(raw)
    converted: dict[str, list[str]] = {"setup": [], "actions": [], "collect": []}
    reasons: list[str] = []

    semantic_text = "\n".join(
        str(raw.get(key) or "")
        for key in ("precondition_text", "steps_text", "expected_text")
    )
    if EXTERNAL_RE.search(semantic_text):
        reasons.append("需要手机、蓝牙、网络或其他外部条件")

    for phase in converted:
        for original in raw.get(phase, []):
            wire, reason = _convert_wire(str(original))
            if reason:
                reasons.append(reason)
                continue
            if wire is None:
                continue
            name = _command_name(wire)
            if name not in {"HOST_WAIT", "HOST_SCREENSHOT"} and name not in live_commands:
                reasons.append(f"6202当前固件未注册 {name}")
                continue
            converted[phase].append(wire)

    reasons = list(dict.fromkeys(reasons))
    if reasons:
        migrated["setup"] = []
        migrated["actions"] = []
        migrated["collect"] = []
        migrated["unable"] = True
        existing_note = str(raw.get("note") or "").strip()
        migration_note = "6202首轮迁移暂不可执行：" + "；".join(reasons) + "。"
        migrated["note"] = f"{existing_note}；{migration_note}" if existing_note else migration_note
        return migrated

    setup = list(BASELINE)
    for wire in converted["setup"]:
        if _command_name(wire) == "LANGUAGE_SET":
            continue
        if wire == BASELINE[1]:
            continue
        setup.append(wire)
    converted["setup"] = setup
    if not any(
        _command_name(wire) == "HOST_SCREENSHOT"
        for phase in converted.values()
        for wire in phase
    ):
        converted["collect"].append(_wire("HOST_SCREENSHOT", "1"))

    migrated.update(converted)
    migrated["unable"] = False
    existing_note = str(raw.get("note") or "").strip()
    migration_note = "6202首轮命令迁移；产品结果仅按真机截图判定。"
    migrated["note"] = f"{existing_note}；{migration_note}" if existing_note else migration_note
    return migrated


def _apply_audit_blockers(owned_files: set[str]) -> None:
    """Turn statically invalid executable drafts into explicit unavailable cases."""

    from sim_tools.audit_case_map import audit_case_maps

    _counts, issues = audit_case_maps(TARGET_DIR, target="hardware")
    blockers: dict[tuple[str, str], list[str]] = {}
    for issue in issues:
        if issue.severity != "error" or issue.file not in owned_files:
            continue
        blockers.setdefault((issue.file, issue.case_id), []).append(
            f"{issue.code}: {issue.detail}"
        )

    by_file: dict[str, dict[str, list[str]]] = {}
    for (file_name, case_id), details in blockers.items():
        by_file.setdefault(file_name, {})[case_id] = list(dict.fromkeys(details))

    for file_name, case_blockers in by_file.items():
        path = TARGET_DIR / file_name
        payload = json.loads(path.read_text(encoding="utf-8"))
        for case in payload["cases"]:
            details = case_blockers.get(str(case.get("case_id") or ""))
            if not details:
                continue
            case["setup"] = []
            case["actions"] = []
            case["collect"] = []
            case["unable"] = True
            note = str(case.get("note") or "").strip()
            blocker_note = "6202静态审计暂不可执行：" + "；".join(details) + "。"
            case["note"] = f"{note}；{blocker_note}" if note else blocker_note
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )


def _summary_for(files: set[str]) -> dict[str, dict[str, int]]:
    result: dict[str, dict[str, int]] = {}
    for file_name in sorted(files):
        payload = json.loads((TARGET_DIR / file_name).read_text(encoding="utf-8"))
        cases = payload["cases"]
        result[file_name] = {
            "total": len(cases),
            "executable": sum(not case["unable"] for case in cases),
            "unable": sum(bool(case["unable"]) for case in cases),
        }
    return result


def migrate(*, dry_run: bool = False) -> dict[str, dict[str, int]]:
    config = HardwareTargetConfig.from_env()
    live_commands = _capabilities(config)
    TARGET_DIR.mkdir(parents=True, exist_ok=True)
    summary: dict[str, dict[str, int]] = {}
    owned_files: set[str] = set()

    for source in sorted(SOURCE_DIR.glob("*.json"), key=lambda path: path.name):
        if source.stem in SKIP_SHEETS:
            continue
        data = json.loads(source.read_text(encoding="utf-8"))
        entries = data.get("cases", data) if isinstance(data, dict) else data
        cases = [_migrate_case(dict(raw), live_commands) for raw in entries]
        payload = {"profile": "6202_W5230", "sheet": source.stem, "cases": cases}
        target = TARGET_DIR / source.name
        owned_files.add(source.name)
        if not dry_run:
            target.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        summary[source.name] = {
            "total": len(cases),
            "executable": sum(not case["unable"] for case in cases),
            "unable": sum(bool(case["unable"]) for case in cases),
        }
    if not dry_run:
        _apply_audit_blockers(owned_files)
        return _summary_for(owned_files)
    return summary


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    print(json.dumps(migrate(dry_run=args.dry_run), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
