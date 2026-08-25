"""Deterministically migrate the legacy standalone calculator plan to 579.

The source plan is a migration input only.  Generated case maps contain stable
Agent-loop semantic commands and have no runtime dependency on crossend_harness.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any


PROFILE = "579_Z1640"
SHEET = "计算器"
EXPECTED_CASE_COUNT = 38
ENTRY_GATE_REASON_CODE = "BUTTON_PRESS_UNSUPPORTED_AFTER_OTA"
MENU_RETURN_COMMAND = "TP_SWIPE:0,251,13,251,1"
DEFAULT_SOURCE = Path(
    r"D:\579完整方案-周俊贤\crossend_harness\cases\first_batch_calculator_38_automation_plan.json"
)

_SWIPE_DIRECTIONS = {"up": 0, "down": 1, "left": 2, "right": 3}


class MigrationError(ValueError):
    pass


def _wire(command: str) -> str:
    return f":{command}"


def _wait_wire(item: dict[str, Any]) -> str:
    try:
        milliseconds = round(float(item["timeout"]) * 1000)
    except (KeyError, TypeError, ValueError) as exc:
        raise MigrationError(f"invalid wait step: {item!r}") from exc
    if milliseconds < 0:
        raise MigrationError(f"wait timeout must be non-negative: {item!r}")
    return _wire(f"HOST_WAIT:{milliseconds}")


def _semantic_step(
    item: dict[str, Any],
    *,
    phase: str,
    state: str,
    previous_command: str,
) -> tuple[list[str], str, bool]:
    args = item.get("args")
    source = args.get("source_plan") if isinstance(args, dict) else None
    if not isinstance(source, dict):
        raise MigrationError(f"step is missing source_plan: {item!r}")
    step_type = str(source.get("type") or "").strip()

    if step_type == "click":
        try:
            x = int(source["x"])
            y = int(source["y"])
            click_type = int(source.get("click_type", 1))
        except (KeyError, TypeError, ValueError) as exc:
            raise MigrationError(f"invalid click step: {source!r}") from exc
        next_state = "calculator" if source.get("target") == "calculator_app_entry" else state
        return [_wire(f"TP_CLICK:{x},{y},{click_type}")], next_state, False

    if step_type == "scroll_page":
        direction = str(source.get("direction") or "").casefold()
        if direction not in {"up", "down"}:
            raise MigrationError(f"invalid scroll direction: {source!r}")
        return [_wire(f"SCROLL_PAGE:{0 if direction == 'up' else 1}")], state, False

    if step_type == "swipe":
        direction = str(source.get("direction") or "").casefold()
        if direction not in _SWIPE_DIRECTIONS:
            raise MigrationError(f"invalid swipe direction: {source!r}")
        try:
            length = int(source.get("length", 0))
        except (TypeError, ValueError) as exc:
            raise MigrationError(f"invalid swipe length: {source!r}") from exc
        return [
            _wire(f"SWIPE_SIM:{_SWIPE_DIRECTIONS[direction]},{length}")
        ], state, False

    if step_type == "tp_swipe":
        try:
            values = tuple(int(source[key]) for key in ("x1", "y1", "x2", "y2", "direction"))
        except (KeyError, TypeError, ValueError) as exc:
            raise MigrationError(f"invalid tp_swipe step: {source!r}") from exc
        return [_wire("TP_SWIPE:" + ",".join(str(value) for value in values))], "menu", False

    if step_type == "switch_window":
        window_name = str(source.get("window_name") or "").strip()
        if window_name == "表盘":
            if phase == "setup":
                # The API/UI supplies the explicit bright-watchface gate.
                return [], "dial", True
            if phase != "teardown":
                raise MigrationError("watchface transition is only valid in setup/teardown")
            recovery: list[str] = []
            if state == "calculator":
                recovery.extend((
                    _wire("TP_SWIPE:0,251,13,251,1"),
                    _wire("HOST_WAIT:500"),
                ))
            if state in {"calculator", "menu", "unknown"}:
                recovery.append(_wire("BUTTON_PRESS:1,1,0"))
            return recovery, "dial", not recovery
        if window_name == "菜单":
            if (
                bool(source.get("evidence_stabilization_only"))
                or previous_command.startswith(":TP_SWIPE:")
                or state == "menu"
            ):
                return [], "menu", True
            if state != "dial":
                raise MigrationError(
                    f"menu transition requires dial state, got {state!r}"
                )
            return [_wire("BUTTON_PRESS:1,1,0")], "menu", False
        raise MigrationError(f"unsupported window transition: {window_name!r}")

    raise MigrationError(f"unsupported source step type: {step_type or '<empty>'}")


def _convert_phase(
    items: Any,
    *,
    phase: str,
    initial_state: str,
    previous_command: str = "",
) -> tuple[list[str], list[str], str, str]:
    if not isinstance(items, list):
        raise MigrationError(f"{phase} must be a list")
    commands: list[str] = []
    verification_points: list[str] = []
    state = initial_state
    drop_next_wait = False
    last_command = previous_command
    for item in items:
        if not isinstance(item, dict):
            raise MigrationError(f"invalid {phase} item: {item!r}")
        item_type = str(item.get("type") or "").strip()
        if item_type == "assert":
            expected = str(item.get("expected") or "").strip()
            if not expected:
                raise MigrationError(f"assertion is missing expected text: {item!r}")
            verification_points.append(expected)
            drop_next_wait = False
            continue
        if item_type == "wait":
            if drop_next_wait:
                drop_next_wait = False
                continue
            command = _wait_wire(item)
            commands.append(command)
            last_command = command
            continue
        if item_type != "step":
            raise MigrationError(f"unsupported item type: {item_type or '<empty>'}")
        converted, state, drop_next_wait = _semantic_step(
            item,
            phase=phase,
            state=state,
            previous_command=last_command,
        )
        commands.extend(converted)
        if converted:
            last_command = converted[-1]
    return commands, verification_points, state, last_command


def migrate_plan(payload: dict[str, Any], *, source_sha256: str = "") -> dict[str, Any]:
    plans = payload.get("plans")
    if not isinstance(plans, list) or len(plans) != EXPECTED_CASE_COUNT:
        raise MigrationError(
            f"expected {EXPECTED_CASE_COUNT} source plans, got "
            f"{len(plans) if isinstance(plans, list) else 'invalid'}"
        )
    cases: list[dict[str, Any]] = []
    seen: set[str] = set()
    for plan in plans:
        if not isinstance(plan, dict):
            raise MigrationError("every plan must be an object")
        case_id = str(plan.get("case_no") or "").strip()
        batch_id = str(plan.get("batch_id") or "").strip()
        if not case_id or case_id in seen:
            raise MigrationError(f"missing or duplicate case id: {case_id!r}")
        if batch_id not in {f"CALC-B{index:02d}" for index in range(1, 9)}:
            raise MigrationError(f"invalid calculator batch: {batch_id!r}")
        seen.add(case_id)

        setup, setup_points, state, previous = _convert_phase(
            plan.get("setup", []),
            phase="setup",
            initial_state="dial",
        )
        actions, action_points, state, previous = _convert_phase(
            plan.get("steps", []),
            phase="steps",
            initial_state=state,
            previous_command=previous,
        )
        teardown, teardown_points, state, _ = _convert_phase(
            plan.get("teardown", []),
            phase="teardown",
            initial_state=state,
            previous_command=previous,
        )
        if state != "dial":
            raise MigrationError(f"{case_id} teardown did not converge to watchface")
        verification_points = setup_points + action_points + teardown_points
        metadata = plan.get("meta") if isinstance(plan.get("meta"), dict) else {}
        business_actions = metadata.get("business_actions", [])
        steps_text = "\n".join(
            f"{index}. {text}"
            for index, text in enumerate(business_actions, start=1)
            if str(text).strip()
        )
        cases.append({
            "case_id": case_id,
            "sheet": SHEET,
            "priority": "P1",
            "precondition_text": "手表位于亮屏表盘；单条启动时逐次确认，完整批次仅在开始时确认一次。",
            "steps_text": steps_text,
            "expected_text": str(plan.get("expected") or "").strip(),
            "verification_points": verification_points,
            "setup": setup,
            "actions": actions + teardown,
            "collect": [],
            "unable": True,
            "mapping_status": "BLOCKED",
            "block_reason_code": ENTRY_GATE_REASON_CODE,
            "batch_id": batch_id,
            "note": (
                "由既有计算器独立执行计划确定性迁移；2026-08-25 真机确认"
                "菜单右滑可返回表盘，但当前 OTA 固件不响应 BUTTON_PRESS:1,1,0，"
                f"因此按 {ENTRY_GATE_REASON_CODE} 阻止执行。"
            ),
        })

    result = {
        "profile": PROFILE,
        "sheet": SHEET,
        "source_kind": str(payload.get("kind") or "legacy_calculator_plan"),
        "source_sha256": source_sha256,
        "migration_version": 2,
        "execution_gate": {
            "status": "BLOCKED",
            "reason_code": ENTRY_GATE_REASON_CODE,
            "watchface_to_menu": {
                "command": "BUTTON_PRESS:1,1,0",
                "transport_acked": True,
                "effect_verified": False,
            },
            "menu_to_watchface": {
                "command": MENU_RETURN_COMMAND,
                "transport_acked": True,
                "effect_verified": True,
            },
            "checked_on": "2026-08-25",
        },
        "cases": cases,
    }
    serialized = json.dumps(result, ensure_ascii=False).casefold()
    forbidden = ("08/96", "switch_window")
    present = [token for token in forbidden if token in serialized]
    if present:
        raise MigrationError(f"generated mapping contains forbidden tokens: {present}")
    return result


def migrate_file(source: Path, output: Path) -> dict[str, Any]:
    source_bytes = source.read_bytes()
    try:
        payload = json.loads(source_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MigrationError(f"source plan is not valid UTF-8 JSON: {source}") from exc
    if not isinstance(payload, dict):
        raise MigrationError("source plan root must be an object")
    migrated = migrate_plan(
        payload,
        source_sha256=hashlib.sha256(source_bytes).hexdigest().upper(),
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(migrated, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return migrated


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="迁移 579 计算器 38 条固定动作映射")
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "case_map" / "579_case_map" / f"{SHEET}.json",
    )
    args = parser.parse_args(argv)
    migrated = migrate_file(args.source.resolve(), args.output.resolve())
    print(f"migrated {len(migrated['cases'])} cases -> {args.output.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
