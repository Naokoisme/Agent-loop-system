"""映射表加载 + 用例执行：查表顺序发命令 + 采集终端 JSON。

映射按执行项目隔离在 ``case_map/620C_simulator_case_map``、
``case_map/6202_case_map`` 和 ``case_map/6202_simulator_case_map``，
不得跨项目静默回退。
实际格式为扁平三段式：setup（前置）→ actions（操作）→ collect（采集判定依据）。
本模块加载并按 case_id 查表，用兼容会话顺序发命令，采集终端 JSON。

判定由 test.py 的 LLM 完成，本模块只负责执行和采集，不判定 PASS/FAIL。
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from itertools import count
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel

from agent_loop_system.runtime_root import resolve_app_root
from agent_loop_system.tools.command_protocol import collect_command_json, normalize_command
CASE_MAP_DIR = resolve_app_root() / "case_map"
CASE_MAP_TARGET_DIRS = {
    "simulator": CASE_MAP_DIR / "620C_simulator_case_map",
    "hardware": CASE_MAP_DIR / "6202_case_map",
}
CASE_MAP_PROFILE_DIRS = {
    "620C_W6830": CASE_MAP_DIR / "620C_simulator_case_map",
    "6202_W5230": CASE_MAP_DIR / "6202_case_map",
    "6202_W5230_SIMULATOR": CASE_MAP_DIR / "6202_simulator_case_map",
}
CASE_MAP_PROFILE_TARGETS = {
    "620C_W6830": "simulator",
    "6202_W5230": "hardware",
    "6202_W5230_SIMULATOR": "simulator",
}
CASE_MAP_PROFILE_PROJECTS = {
    "620C_W6830": "620C_W6830",
    "6202_W5230": "6202_W5230",
    "6202_W5230_SIMULATOR": "6202_W5230",
}
CASE_MAP_TARGET_DEFAULT_PROFILES = {
    "simulator": "620C_W6830",
    "hardware": "6202_W5230",
}
_BARRIER_SEQ = count(1_000_000)
_ERROR_STATUSES = {"error", "failed", "rejected", "unavailable"}
_SCREENSHOT_SETTLE_SECONDS = 0.2
_ENTER_PAGE_SETTLE_SECONDS = 1.0
_ROTARY_INPUT_SETTLE_SECONDS = 0.25
_SIM_WAIT_TIMEOUT_MARGIN_SECONDS = 5.0
# 这些被动命令的完成回执本身就是执行栅栏。再追加 GUI_PING 会在设备刚
# 自动熄屏后把它重新点亮，污染下一张截图。
_SELF_SYNCHRONIZING_COMMANDS = {
    "GUI_PING",
    "GUI_STATE",
    "GUI_TREE",
    "SIM_WAIT",
    "HOST_WAIT",
    "HOST_SCREENSHOT",
}
OBSERVATION_ONLY_COMMANDS = frozenset({
    "BUSINESS_GET",
    "GET_CURRENT_WIN_ID",
    "GUI_PING",
    "GUI_STATE",
    "GUI_TREE",
    "HOST_SCREENSHOT",
    "HOST_WAIT",
    "SCREENSHOT_PRINT",
})


class CaseSession(Protocol):
    """Simulator 与真机会话执行 case_map 所需的最小接口。"""

    def send(self, content: str, **kwargs): ...

    def capture_screenshot(self, output_path: str) -> bool: ...


class CaseEntry(BaseModel):
    """单条测试用例的映射表项（适配外部 Agent 实际生成的格式）。"""

    case_id: str
    sheet: str = ""
    priority: str = ""
    precondition_text: str = ""
    steps_text: str = ""
    expected_text: str = ""
    verification_points: list[str] = []
    setup: list[str] = []  # 完整 wire 格式，如 'srv_quick_cmd send TOP5STEP:ENTER_PAGE:CALCULATOR,0;'
    actions: list[str] = []
    collect: list[str] = []
    unable: bool = False  # 旧数据兼容；不再作为 Runner 入口闸门
    mapping_status: str = ""
    note: str = ""

    @property
    def is_promoted(self) -> bool:
        """只有精确的 PROMOTED 才使用固化步骤。"""

        return self.mapping_status == "PROMOTED"

    @property
    def has_candidate_mapping(self) -> bool:
        """外部 Agent 正式复跑前写入的临时候选至少要有一个业务动作。"""

        return bool(self.actions)


@dataclass
class CaseRunResult:
    """单条用例的执行结果。terminal_json 供 LLM 判定，其余供人工查看。"""

    case_id: str
    sheet: str
    expected_text: str
    execution_mode: str = "fixed_mapping"
    precondition_text: str = ""
    steps_text: str = ""
    verification_points: list[str] = field(default_factory=list)
    planned_commands: dict[str, list[str]] = field(default_factory=dict)
    command_trace: list[dict[str, object]] = field(default_factory=list)
    evidence_contract: dict[str, object] = field(default_factory=dict)
    skipped: bool = False  # 只兼容旧结果；当前 Runner 不再因 unable 跳过用例
    setup_errors: list[str] = field(default_factory=list)
    action_errors: list[str] = field(default_factory=list)
    collect_errors: list[str] = field(default_factory=list)
    terminal_json: list[dict] = field(default_factory=list)  # 全程采集的终端 JSON
    screenshots: list[dict[str, object]] = field(default_factory=list)
    aborted: bool = False  # 是否因 setup/actions 失败而中止
    precomputed_verdict: str = ""
    precomputed_reason: str = ""
    exploration_trace: dict[str, object] | None = None
    provenance: dict[str, str] = field(default_factory=dict)


def effective_case_entries(data: object) -> list[dict]:
    """读取原始条目；所有用例都可以交给 Runner 尝试执行。"""

    raw_entries = data.get("cases", data) if isinstance(data, dict) else data
    if not isinstance(raw_entries, list):
        return []
    return [item for item in raw_entries if isinstance(item, dict)]


def case_map_dir_for_target(target: str = "simulator") -> Path:
    """返回目标专用映射目录；未知目标直接拒绝。"""

    normalized = str(target or "").strip().lower()
    try:
        return CASE_MAP_TARGET_DIRS[normalized]
    except KeyError as exc:
        raise ValueError(f"未知 case_map 目标: {target!r}") from exc


def _validated_sheet_name(sheet_name: str) -> str:
    """只接受 case-map 目录内的单一模块名，不把输入当作路径。"""

    value = str(sheet_name or "")
    if (
        not value
        or value != value.strip()
        or value in {".", ".."}
        or any(character in value for character in ("/", "\\", ":", "\x00"))
        or Path(value).name != value
    ):
        raise ValueError(f"sheet 必须是单一模块名，不能包含路径或首尾空白: {sheet_name!r}")
    return value


def validated_case_entries(
    data: object,
    *,
    sheet_name: str,
    expected_profile: str | None,
    path: Path,
) -> list[dict]:
    """确认文件元数据和每条用例仍属于所选 profile/sheet。"""

    if isinstance(data, dict):
        if expected_profile is not None and data.get("profile") != expected_profile:
            raise ValueError(
                f"case_map profile 不匹配: {path} 声明 {data.get('profile')!r}，"
                f"预期 {expected_profile!r}"
            )
        if data.get("sheet") != sheet_name:
            raise ValueError(
                f"case_map sheet 不匹配: {path} 声明 {data.get('sheet')!r}，"
                f"预期 {sheet_name!r}"
            )
        raw_entries = data.get("cases")
    elif expected_profile not in (None, "620C_W6830"):
        raise ValueError(
            f"case_map profile 不匹配: {path} 的 {expected_profile!r} 映射必须声明"
            "顶层 profile、sheet 和 cases"
        )
    else:
        raw_entries = data

    if not isinstance(raw_entries, list) or not raw_entries:
        raise ValueError(f"case_map cases 必须是非空数组: {path}")
    if not all(isinstance(item, dict) for item in raw_entries):
        raise ValueError(f"case_map cases 只能包含 JSON 对象: {path}")
    mismatched = [
        str(item.get("case_id") or "<unknown>")
        for item in raw_entries
        if item.get("sheet") != sheet_name
    ]
    if mismatched:
        preview = ", ".join(mismatched[:3])
        raise ValueError(
            f"case_map 条目 sheet 不匹配: {path} 中 {preview} 不属于 {sheet_name!r}"
        )
    return raw_entries


def load_case_map(
    sheet_name: str,
    *,
    target: str = "simulator",
    profile: str | None = None,
) -> dict[str, CaseEntry]:
    """加载目标专用的单 sheet 映射，不跨目标回退。"""

    normalized_target = str(target or "").strip().lower()
    validated_sheet = _validated_sheet_name(sheet_name)
    if profile is None:
        case_map_dir = case_map_dir_for_target(normalized_target)
        location = normalized_target
        expected_profile = CASE_MAP_TARGET_DEFAULT_PROFILES[normalized_target]
    else:
        normalized_profile = str(profile or "").strip()
        try:
            expected_target = CASE_MAP_PROFILE_TARGETS[normalized_profile]
            case_map_dir = CASE_MAP_PROFILE_DIRS[normalized_profile]
        except KeyError as exc:
            raise ValueError(f"未知 case_map profile: {profile!r}") from exc
        if expected_target != normalized_target:
            raise ValueError(
                f"case_map profile {normalized_profile!r} 不属于执行目标 {normalized_target!r}"
            )
        location = normalized_profile
        expected_profile = normalized_profile

    resolved_directory = case_map_dir.resolve(strict=True)
    requested_path = resolved_directory / f"{validated_sheet}.json"
    if not requested_path.is_file():
        raise FileNotFoundError(f"{location} case_map 尚未迁移该模块: {requested_path}")
    path = requested_path.resolve(strict=True)
    if path.parent != resolved_directory:
        raise ValueError(f"case_map 文件越过目标 profile 目录: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    raw_entries = validated_case_entries(
        data,
        sheet_name=validated_sheet,
        expected_profile=expected_profile,
        path=path,
    )
    return {e.case_id: e for e in (CaseEntry(**e) for e in raw_entries)}


def run_case(
    session: CaseSession,
    case: CaseEntry,
    screenshot_path: str | Path | None = None,
) -> CaseRunResult:
    """顺序发命令执行用例，采集终端 JSON。

    流程：setup（失败中止）→ actions（失败中止）→ collect（失败不中止，仅采集）。
    本函数只执行传入的固定命令。非固化用例的 Agent 探索分支由 test.py 选择。
    """
    result = CaseRunResult(
        case_id=case.case_id,
        sheet=case.sheet,
        expected_text=case.expected_text,
        execution_mode=("fixed_mapping" if case.is_promoted else "candidate_mapping"),
        precondition_text=case.precondition_text,
        steps_text=case.steps_text,
        verification_points=list(case.verification_points),
        planned_commands={
            "setup": list(case.setup),
            "action": list(case.actions),
            "collect": list(case.collect),
        },
    )

    # setup：前置条件，失败则中止
    screenshot_base = Path(screenshot_path) if screenshot_path else None
    if screenshot_base:
        screenshot_base.parent.mkdir(parents=True, exist_ok=True)

    err = _run_commands(session, case.setup, result, "setup", screenshot_base)
    if err:
        result.setup_errors.append(err)
        result.aborted = True
        if screenshot_base and not result.screenshots:
            _capture_checkpoint(session, result, screenshot_base, "final", "")
        return _finish_case_run(case, result)

    # actions：操作步骤，失败则中止
    err = _run_commands(session, case.actions, result, "action", screenshot_base)
    if err:
        result.action_errors.append(err)
        result.aborted = True
        if screenshot_base and not result.screenshots:
            _capture_checkpoint(session, result, screenshot_base, "final", "")
        return _finish_case_run(case, result)

    # collect：采集判定依据，失败不中止
    _run_commands(session, case.collect, result, "collect", screenshot_base)

    if screenshot_base and not result.screenshots:
        _capture_checkpoint(session, result, screenshot_base, "final", "")
    return _finish_case_run(case, result)


def _run_commands(
    session: CaseSession,
    wires: list[str],
    result: CaseRunResult,
    phase: str,
    screenshot_base: Path | None = None,
) -> str:
    """顺序发送命令并采集终端 JSON。返回第一条失败的错误信息（空串=全部成功）。

    collect 阶段的命令失败（含超时）只记录不中止，但仍尝试采集已输出的 JSON。
    """
    for planned_index, wire in enumerate(wires, start=1):
        try:
            raw = normalize_command(wire)
        except ValueError as e:
            _append_command_trace(
                result,
                phase=phase,
                source="case",
                kind="command",
                wire=wire,
                command="",
                command_name="",
                status="error",
                ok=False,
                planned_index=planned_index,
                error=str(e),
            )
            if phase == "collect":
                result.collect_errors.append(f"{wire}: {e}")
                continue
            return f"{wire}: {e}"
        command_name = raw[1:].partition(":")[0]
        try:
            if command_name == "HOST_SCREENSHOT":
                if screenshot_base is None:
                    raise ValueError("HOST_SCREENSHOT 缺少截图输出路径")
                _capture_checkpoint(
                    session,
                    result,
                    screenshot_base,
                    phase,
                    wire,
                    source="case",
                    command=raw,
                    planned_index=planned_index,
                )
                continue
            if command_name == "GUI_PING":
                r = session.send(
                    raw,
                    request="gui_ping",
                    expected_type="gui_ack",
                    expected_status="processed",
                )
            elif command_name == "GUI_TREE":
                r = session.send(
                    raw,
                    request="gui_tree",
                    expected_type="gui_tree_end",
                )
            elif command_name == "SIM_WAIT":
                # SIM_WAIT intentionally returns only after the requested idle
                # interval.  Give that shared command enough time to report its
                # terminal result instead of applying the normal 5 s command
                # timeout to every duration.
                milliseconds = int(raw.rsplit(",", 1)[1])
                r = session.send(
                    raw,
                    timeout=milliseconds / 1000.0 + _SIM_WAIT_TIMEOUT_MARGIN_SECONDS,
                )
            elif command_name == "HOST_WAIT":
                payload = raw.rsplit(":", 1)[1]
                args = payload.split(",")
                if len(args) not in (1, 2) or any(not arg for arg in args):
                    raise ValueError(
                        "HOST_WAIT expects milliseconds or checkpoint,milliseconds"
                    )
                if len(args) == 2:
                    int(args[0])
                milliseconds = int(args[-1])
                if not 0 <= milliseconds <= 120_000:
                    raise ValueError("HOST_WAIT milliseconds must be in 0..120000")
                _perform_host_wait(
                    result,
                    milliseconds=milliseconds,
                    phase=phase,
                    source="case",
                    wire=wire,
                    command=raw,
                    planned_index=planned_index,
                )
                continue
            elif command_name == "BUTTON_PRESS":
                # A type-2 simulator key press follows the physical lifecycle
                # and returns only after the requested hold duration.
                args = raw.rsplit(":", 1)[1].split(",")
                press_type = int(args[1])
                press_time = int(args[2])
                if press_type == 2:
                    r = session.send(
                        raw,
                        timeout=press_time / 1000.0 + _SIM_WAIT_TIMEOUT_MARGIN_SECONDS,
                    )
                else:
                    r = session.send(raw)
            else:
                r = session.send(raw)
        except Exception as e:
            _append_command_trace(
                result,
                phase=phase,
                source="case",
                kind="host" if command_name.startswith("HOST_") else "device",
                wire=wire,
                command=raw,
                command_name=command_name,
                status="error",
                ok=False,
                planned_index=planned_index,
                error=str(e),
            )
            if phase == "collect":
                result.collect_errors.append(f"{wire}: {e}")
                continue
            return f"{wire}: {e}"
        # 采集终端 JSON：raw 是匹配的结束事件，lines 是执行期间所有 stdout 行
        result.terminal_json.extend(collect_command_json(r))
        screen_off_observation = bool(
            command_name == "GUI_TREE"
            and r.status == "unavailable"
            and r.raw.get("reason") == "screen_off"
        )
        _append_command_trace(
            result,
            phase=phase,
            source="case",
            kind="device",
            wire=wire,
            command=raw,
            command_name=command_name,
            status=str(r.status),
            ok=r.status not in _ERROR_STATUSES or screen_off_observation,
            planned_index=planned_index,
            error="" if r.status not in _ERROR_STATUSES or screen_off_observation else f"固件返回 {r.status}",
        )
        # 熄屏时 GUI_TREE 会正确返回 unavailable/screen_off，但桌面截图仍是
        # 唯一产品判据，不能因为诊断树不可读就丢掉对应检查点。
        if command_name == "GUI_TREE" and screenshot_base:
            _capture_checkpoint(
                session,
                result,
                screenshot_base,
                phase,
                wire,
                source="runner",
                command=":HOST_SCREENSHOT:GUI_TREE_CHECKPOINT",
            )
        if r.status in _ERROR_STATUSES:
            if screen_off_observation:
                # 熄屏时没有可遍历的控件树是正常状态；截图已保留，继续交给
                # 视觉判据，不能把产品正确熄屏改写成执行 ERROR。
                continue
            error = f"{wire}: 固件返回 {r.status}"
            if phase == "collect":
                result.collect_errors.append(error)
                continue
            return error

        # accepted 只表示命令入队。setup/action 后用 GUI_PING 等队列真正处理完，
        # 避免下一条命令或采集抢在界面更新前面。
        if phase in {"setup", "action"} and command_name not in _SELF_SYNCHRONIZING_COMMANDS:
            seq = next(_BARRIER_SEQ)
            barrier_command = f":GUI_PING:{seq}"
            try:
                barrier = session.send(
                    barrier_command,
                    request="gui_ping",
                    expected_type="gui_ack",
                    expected_status="processed",
                )
            except Exception as e:
                _append_command_trace(
                    result,
                    phase=phase,
                    source="runner",
                    kind="barrier",
                    wire="",
                    command=barrier_command,
                    command_name="GUI_PING",
                    status="error",
                    ok=False,
                    error=str(e),
                )
                return f"{wire}: 等待 GUI 处理完成失败: {e}"
            result.terminal_json.extend(collect_command_json(barrier))
            _append_command_trace(
                result,
                phase=phase,
                source="runner",
                kind="barrier",
                wire="",
                command=barrier_command,
                command_name="GUI_PING",
                status=str(barrier.status),
                ok=barrier.status == "processed",
                error="" if barrier.status == "processed" else f"GUI 未处理完成（{barrier.status}）",
            )
            if barrier.status != "processed":
                return f"{wire}: GUI 未处理完成（{barrier.status}）"
            # ENTER_PAGE 的消息处理完成只代表窗口创建请求已消费；首帧布局和
            # 输入命中区域还会晚一个 GUI 周期。统一留出短暂稳定时间，避免
            # 后续第一下触控落在旧页面上。
            if command_name == "ENTER_PAGE":
                _perform_host_wait(
                    result,
                    milliseconds=round(_ENTER_PAGE_SETTLE_SECONDS * 1000),
                    phase=phase,
                    source="runner",
                    wire="",
                    command=":HOST_WAIT:ENTER_PAGE_SETTLE,1000",
                )
            # GUI_PING only proves that the rotary event left the command queue.
            # List scrolling and focus animation can still be moving, so a
            # following click may land on the previous geometry.  Apply one
            # project-agnostic settle to both rotary directions.
            elif command_name in {"QDEC_SET", "QINC_SET"}:
                _perform_host_wait(
                    result,
                    milliseconds=round(_ROTARY_INPUT_SETTLE_SECONDS * 1000),
                    phase=phase,
                    source="runner",
                    wire="",
                    command=":HOST_WAIT:ROTARY_INPUT_SETTLE,250",
                )
    return ""


def _append_command_trace(
    result: CaseRunResult,
    *,
    phase: str,
    source: str,
    kind: str,
    wire: str,
    command: str,
    command_name: str,
    status: str,
    ok: bool,
    planned_index: int | None = None,
    checkpoint_index: int | None = None,
    checkpoint_label: str = "",
    error: str = "",
) -> dict[str, object]:
    """记录本次真实发生的命令、Runner 栅栏、等待和截图动作。"""

    entry: dict[str, object] = {
        "index": len(result.command_trace) + 1,
        "phase": phase,
        "source": source,
        "kind": kind,
        "wire": wire,
        "command": command,
        "command_name": command_name,
        "status": status,
        "ok": ok,
    }
    if planned_index is not None:
        entry["planned_index"] = planned_index
    if checkpoint_index is not None:
        entry["checkpoint_index"] = checkpoint_index
    if checkpoint_label:
        entry["checkpoint_label"] = checkpoint_label
    if error:
        entry["error"] = error
    result.command_trace.append(entry)
    return entry


def _perform_host_wait(
    result: CaseRunResult,
    *,
    milliseconds: int,
    phase: str,
    source: str,
    wire: str,
    command: str,
    planned_index: int | None = None,
) -> None:
    """执行并记录电脑端等待，避免隐式等待从前端证据里消失。"""

    time.sleep(milliseconds / 1000.0)
    _append_command_trace(
        result,
        phase=phase,
        source=source,
        kind="wait",
        wire=wire,
        command=command,
        command_name="HOST_WAIT",
        status="completed",
        ok=True,
        planned_index=planned_index,
    )


def _capture_checkpoint(
    session: CaseSession,
    result: CaseRunResult,
    screenshot_base: Path,
    phase: str,
    wire: str,
    *,
    source: str = "runner",
    command: str = ":HOST_SCREENSHOT:AUTO_FINAL",
    planned_index: int | None = None,
) -> bool:
    """在每个 GUI_TREE 检查点保存独立截图，并与验证点按顺序关联。"""
    # GUI_PING/GUI_TREE 只能保证事件已处理；窗口绘制和桌面合成可能仍晚一帧。
    # 所有检查点统一留出很短的稳定时间，避免抓到操作前的旧画面。
    _perform_host_wait(
        result,
        milliseconds=round(_SCREENSHOT_SETTLE_SECONDS * 1000),
        phase=phase,
        source="runner",
        wire="",
        command=":HOST_WAIT:SCREENSHOT_SETTLE,200",
    )
    index = len(result.screenshots) + 1
    suffix = screenshot_base.suffix or ".bmp"
    path = screenshot_base.with_name(f"{screenshot_base.stem}-{index:02d}{suffix}")
    label = (
        result.verification_points[index - 1]
        if index <= len(result.verification_points)
        else ("最终画面" if phase == "final" else f"检查点 {index}")
    )
    error = ""
    try:
        path.unlink(missing_ok=True)
        captured = session.capture_screenshot(str(path))
        if not captured or not path.is_file() or path.stat().st_size <= 0:
            error = f"第 {index} 个测试目标截图采集失败"
    except Exception as exc:
        error = f"第 {index} 个测试目标截图采集失败: {exc}"
    trace = _append_command_trace(
        result,
        phase=phase,
        source=source,
        kind="screenshot",
        wire=wire,
        command=command,
        command_name="HOST_SCREENSHOT",
        status="error" if error else "completed",
        ok=not error,
        planned_index=planned_index,
        checkpoint_index=index,
        checkpoint_label=label,
        error=error,
    )
    if error:
        result.collect_errors.append(error)
        return False
    result.screenshots.append(
        {
            "index": index,
            "label": label,
            "phase": phase,
            "command": wire,
            "path": str(path),
            "captured_at": datetime.now().astimezone().isoformat(),
            "trace_index": trace["index"],
        }
    )
    return True


def _finish_case_run(case: CaseEntry, result: CaseRunResult) -> CaseRunResult:
    """在视觉判定前固化通用证据合同；合同不完整时后续不得 PASS。"""

    if result.skipped:
        result.evidence_contract = {
            "status": "SKIPPED",
            "complete": False,
            "required_screenshots": 0,
            "captured_screenshots": 0,
            "planned_action_count": len(case.actions),
            "attempted_action_count": 0,
            "business_action_count": 0,
            "issues": [],
        }
        return result

    issues: list[dict[str, str]] = []

    def add_issue(code: str, message: str) -> None:
        issues.append({"code": code, "message": message})

    if result.aborted or result.setup_errors or result.action_errors:
        detail = (result.setup_errors + result.action_errors)[0] if (
            result.setup_errors or result.action_errors
        ) else "准备或操作阶段未完整执行"
        add_issue("execution_incomplete", detail)

    action_names: list[str] = []
    for wire in case.actions:
        try:
            action_names.append(normalize_command(wire)[1:].partition(":")[0])
        except ValueError:
            continue
    business_action_count = sum(
        name not in OBSERVATION_ONLY_COMMANDS for name in action_names
    )
    if case.steps_text.strip() and business_action_count == 0:
        add_issue(
            "business_action_missing",
            "人工步骤不为空，但 actions 没有实际业务操作；不能用截图代替未执行的步骤",
        )

    attempted_actions = {
        int(item["planned_index"])
        for item in result.command_trace
        if item.get("source") == "case"
        and item.get("phase") == "action"
        and isinstance(item.get("planned_index"), int)
    }
    if len(attempted_actions) < len(case.actions):
        add_issue(
            "action_trace_incomplete",
            f"actions 计划 {len(case.actions)} 条，实际只尝试 {len(attempted_actions)} 条",
        )

    expected_items = len(re.findall(r"(?m)^\s*\d+[.、）]", case.expected_text))
    if expected_items > 1 and not result.verification_points:
        add_issue(
            "verification_points_missing",
            f"人工预期包含 {expected_items} 项，但没有 verification_points；必须明确这些预期由一张还是多张截图证明",
        )

    required_screenshots = len(result.verification_points) or 1
    captured_screenshots = len(result.screenshots)
    if captured_screenshots != required_screenshots:
        add_issue(
            "checkpoint_screenshot_mismatch",
            f"需要 {required_screenshots} 张检查点截图，实际采集 {captured_screenshots} 张",
        )

    if not result.verification_points and captured_screenshots > 1:
        add_issue(
            "unlabeled_multiple_checkpoints",
            "采集了多张截图但没有 verification_points，无法一一对应视觉预期",
        )

    seen_paths: set[str] = set()
    for index, screenshot in enumerate(result.screenshots, start=1):
        path_text = str(screenshot.get("path") or "")
        path = Path(path_text) if path_text else None
        try:
            screenshot_exists = bool(path and path.is_file() and path.stat().st_size > 0)
        except OSError:
            screenshot_exists = False
        if not screenshot_exists:
            add_issue("screenshot_missing", f"第 {index} 张检查点截图文件不存在或为空")
        if path_text in seen_paths:
            add_issue("screenshot_not_fresh", f"第 {index} 张检查点截图复用了同一路径")
        seen_paths.add(path_text)
        if index <= len(result.verification_points):
            expected_label = result.verification_points[index - 1]
            if str(screenshot.get("label") or "") != expected_label:
                add_issue(
                    "checkpoint_label_mismatch",
                    f"第 {index} 张截图没有绑定到对应 verification_point",
                )

    result.evidence_contract = {
        "status": "COMPLETE" if not issues else "ERROR",
        "complete": not issues,
        "required_screenshots": required_screenshots,
        "captured_screenshots": captured_screenshots,
        "planned_action_count": len(case.actions),
        "attempted_action_count": len(attempted_actions),
        "business_action_count": business_action_count,
        "issues": issues,
    }
    return result
