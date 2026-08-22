"""交互式缺陷复现的数据合同和有界执行循环。

Agent 每轮只决定一个动作；普通程序负责构建、命令执行、观察和证据落盘。
"""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, Field, model_validator

from agent_loop_system.tools.command_protocol import (
    collect_command_json,
    load_current_command_capabilities,
    normalize_command,
    validate_agent_command,
)
from agent_loop_system.tools.simulator import CommandResult, SimulatorSession

SHANGHAI_TIMEZONE = timezone(timedelta(hours=8), name="Asia/Shanghai")
_REPRODUCTION_SCREEN_ON_SECONDS = 300
_SYSTEM_OBSERVATION_COMMANDS = {"GUI_PING", "GUI_STATE", "GUI_TREE", "SCREENSHOT_PRINT"}
_BACKGROUND_STATE_COMMANDS = {"SIM_CONNECTION_SET"}
_COMMAND_FAILURE_STATUSES = {
    "busy",
    "error",
    "failed",
    "rejected",
    "unavailable",
    "unsupported",
}


class DeviceSession(Protocol):
    """模拟器与真机复用的最小会话合同。"""

    last_capture_metadata: dict[str, Any] | None

    def start(self) -> None: ...

    def send(self, content: str, **kwargs: Any) -> CommandResult: ...

    def lines_since(self, start_index: int) -> list[str]: ...

    def capture_screenshot(self, output_path: str) -> bool: ...

    def stop(self) -> None: ...


class ReproductionAction(StrEnum):
    """复现 Agent 每轮唯一允许返回的动作。"""

    EXECUTE = "EXECUTE"
    OBSERVE_AGAIN = "OBSERVE_AGAIN"
    READY_TO_JUDGE = "READY_TO_JUDGE"
    BLOCKED = "BLOCKED"


class ReproductionOutcome(StrEnum):
    """交互式复现的结构化终止状态。"""

    DEFECT_REPRODUCED = "DEFECT_REPRODUCED"
    CURRENT_CONFORMS = "CURRENT_CONFORMS"
    TARGET_NOT_REACHED = "TARGET_NOT_REACHED"
    REFERENCE_AMBIGUOUS = "REFERENCE_AMBIGUOUS"
    CAPABILITY_MISSING = "CAPABILITY_MISSING"
    COMMAND_RUNTIME_ERROR = "COMMAND_RUNTIME_ERROR"
    SYSTEM_ERROR = "SYSTEM_ERROR"


class TreeStatus(StrEnum):
    """GUI_TREE 是辅助证据；不可用不等于截图观察失败。"""

    OK = "OK"
    UNAVAILABLE = "UNAVAILABLE"
    ERROR = "ERROR"
    NOT_COLLECTED = "NOT_COLLECTED"


class ReproductionDecision(BaseModel):
    """同一个复现 Agent 在一轮中做出的单步决定。"""

    action: ReproductionAction
    command: str | None = None
    reason: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_single_command(self) -> "ReproductionDecision":
        command = self.command.strip() if self.command else None
        if self.action == ReproductionAction.EXECUTE:
            if not command:
                raise ValueError("EXECUTE 必须提供一条业务命令")
            self.command = command
        elif command:
            raise ValueError(f"{self.action.value} 不得携带命令")
        else:
            self.command = None
        return self


class StepObservation(BaseModel):
    """执行一个动作后由普通程序采集的事实，不包含系统推理。"""

    step: int = Field(ge=0)
    decision: ReproductionDecision | None = None
    observed_at: str | None = None

    command_status: str | None = None
    command_results: list[dict[str, Any]] = Field(default_factory=list)

    screenshot_ok: bool
    screenshot_path: str | None = None
    capture_metadata: dict[str, Any] | None = None
    window_id: int | None = None
    window_name: str | None = None
    popup_id: int | None = None
    popup_name: str | None = None

    tree_status: TreeStatus = TreeStatus.NOT_COLLECTED
    gui_tree: list[dict[str, Any]] = Field(default_factory=list)
    visible_texts: list[str] = Field(default_factory=list)
    error: str | None = None

    @model_validator(mode="after")
    def validate_screenshot_evidence(self) -> "StepObservation":
        if self.screenshot_ok and not (self.screenshot_path or "").strip():
            raise ValueError("截图成功时必须记录 screenshot_path")
        return self


class ReproductionTrace(BaseModel):
    """一次交互式复现的完整、可序列化证据。"""

    task_id: str = Field(min_length=1)
    max_steps: int = Field(default=6, ge=1)
    started_at: str | None = None
    finished_at: str | None = None
    steps: list[StepObservation] = Field(default_factory=list)
    outcome: ReproductionOutcome | None = None
    verdict: str | None = None
    reason: str | None = None

    @model_validator(mode="after")
    def validate_terminal_reason(self) -> "ReproductionTrace":
        if self.outcome is not None and not (self.reason or "").strip():
            raise ValueError("设置终止状态时必须提供 reason")
        return self


def _response_reason(result: CommandResult) -> str:
    reason = result.raw.get("reason") if isinstance(result.raw, dict) else None
    return f"{result.status}: {reason}" if reason else result.status


def _window_identity(value: Any) -> tuple[int | None, str | None]:
    if not isinstance(value, dict):
        return None, None
    raw_id = value.get("id")
    window_id = raw_id if isinstance(raw_id, int) and not isinstance(raw_id, bool) else None
    raw_name = value.get("name")
    window_name = raw_name if isinstance(raw_name, str) and raw_name else None
    return window_id, window_name


def _collect_action_results(
    session: DeviceSession,
    command_result: CommandResult | None,
) -> list[dict[str, Any]]:
    """收集命令回执以及随后异步产生的音频、震动等可验证输出。"""
    if command_result is None:
        return []
    results = collect_command_json(command_result)
    if command_result.start_index is None:
        return results

    complete = CommandResult(
        request=command_result.request,
        status=command_result.status,
        raw=command_result.raw,
        lines=session.lines_since(command_result.start_index),
    )
    for item in collect_command_json(complete):
        event_type = item.get("type")
        belongs_to_action = item.get("request") == command_result.request
        is_effect = isinstance(event_type, str) and event_type.endswith("_event")
        if (belongs_to_action or is_effect) and item not in results:
            results.append(item)
    return results


def _write_model(path: Path, model: BaseModel) -> None:
    """原子写入模型 JSON，避免进程中断后留下半个证据文件。"""
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(model.model_dump(mode="json"), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(temporary, path)


def observe(
    session: DeviceSession,
    *,
    step: int,
    seq: int,
    evidence_dir: str | os.PathLike[str],
    decision: ReproductionDecision | None = None,
    command_result: CommandResult | None = None,
) -> StepObservation:
    """采集一次系统观察；截图优先，GUI_TREE 失败只降级辅助证据。

    调用方负责执行本轮唯一一条业务命令，并把回执作为 ``command_result``
    传入。该函数固定完成 GUI 同步、截图、窗口/Popup 和 GUI_TREE 采集，随后
    立即写出 ``step_XX.json``。
    """
    output_dir = Path(evidence_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    screenshot_file = output_dir / f"step_{step:02d}.bmp"
    json_file = output_dir / f"step_{step:02d}.json"

    errors: list[str] = []
    command_results = collect_command_json(command_result) if command_result else []

    # accepted 只代表入队。这里必须等到 GUI 线程返回 processed，失败时仍继续截图。
    try:
        ping = session.send(
            f":GUI_PING:{seq}",
            request="gui_ping",
            expected_type="gui_ack",
            expected_status="processed",
        )
        if ping.raw.get("type") != "gui_ack" or ping.status != "processed":
            errors.append(f"GUI_PING 未完成（{_response_reason(ping)}）")
    except Exception as exc:
        errors.append(f"GUI_PING 异常: {exc}")

    # 截图是可见 UI 的权威证据，放在 Tree 之前采集，避免 Tree 故障阻断截图。
    screenshot_ok = False
    capture_metadata: dict[str, Any] | None = None
    try:
        screenshot_file.unlink(missing_ok=True)
        captured = session.capture_screenshot(str(screenshot_file))
        screenshot_ok = bool(
            captured and screenshot_file.is_file() and screenshot_file.stat().st_size > 0
        )
        if not screenshot_ok:
            screenshot_file.unlink(missing_ok=True)
            errors.append("截图失败")
        else:
            value = getattr(session, "last_capture_metadata", None)
            capture_metadata = dict(value) if isinstance(value, dict) else None
    except Exception as exc:
        screenshot_file.unlink(missing_ok=True)
        errors.append(f"截图异常: {exc}")

    window_id = None
    window_name = None
    popup_id = None
    popup_name = None
    try:
        state = session.send(
            f":GUI_STATE:{seq + 1}",
            request="gui_state",
            expected_type="gui_state",
            expected_status="ok",
        )
        if state.raw.get("type") == "gui_state" and state.status == "ok":
            window_id, window_name = _window_identity(state.raw.get("current_page"))
            popup_id, popup_name = _window_identity(state.raw.get("popup"))
        else:
            errors.append(f"GUI_STATE 不可用（{_response_reason(state)}）")
    except Exception as exc:
        errors.append(f"GUI_STATE 异常: {exc}")

    tree_status = TreeStatus.NOT_COLLECTED
    gui_tree: list[dict[str, Any]] = []
    visible_texts: list[str] = []
    try:
        tree = session.send(
            f":GUI_TREE:{seq + 2}",
            request="gui_tree",
            expected_type="gui_tree_end",
            expected_status="ok",
        )
        tree_objects = collect_command_json(tree)
        gui_tree = [obj for obj in tree_objects if obj.get("type") == "gui_tree_node"]
        if tree.raw.get("type") == "gui_tree_end" and tree.status == "ok":
            tree_status = TreeStatus.OK
            for node in gui_tree:
                text = node.get("text")
                if node.get("effective_visible") is True and isinstance(text, str) and text:
                    if text not in visible_texts:
                        visible_texts.append(text)
        elif tree.status in {"busy", "unavailable"}:
            tree_status = TreeStatus.UNAVAILABLE
            errors.append(f"GUI_TREE 不可用（{_response_reason(tree)}）")
        else:
            tree_status = TreeStatus.ERROR
            errors.append(f"GUI_TREE 失败（{_response_reason(tree)}）")
    except Exception as exc:
        tree_status = TreeStatus.ERROR
        errors.append(f"GUI_TREE 异常: {exc}")

    # 外设动作通常在命令 accepted 之后异步完成。等截图、页面状态和 Tree 都采集完，
    # 再回看本动作发出后的输出，避免漏掉 motor_event/audio_event。
    command_results = _collect_action_results(session, command_result)

    observation = StepObservation(
        step=step,
        decision=decision,
        observed_at=datetime.now(SHANGHAI_TIMEZONE).isoformat(timespec="seconds"),
        command_status=command_result.status if command_result else None,
        command_results=command_results,
        screenshot_ok=screenshot_ok,
        screenshot_path=str(screenshot_file) if screenshot_ok else None,
        capture_metadata=capture_metadata,
        window_id=window_id,
        window_name=window_name,
        popup_id=popup_id,
        popup_name=popup_name,
        tree_status=tree_status,
        gui_tree=gui_tree,
        visible_texts=visible_texts,
        error="；".join(errors) if errors else None,
    )
    _write_model(json_file, observation)
    return observation


def _now() -> str:
    return datetime.now(SHANGHAI_TIMEZONE).isoformat(timespec="seconds")


def _save_trace(output_dir: Path, trace: ReproductionTrace) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    _write_model(output_dir / "trace.json", trace)


def _finish_trace(
    output_dir: Path,
    trace: ReproductionTrace,
    outcome: ReproductionOutcome,
    reason: str,
) -> ReproductionTrace:
    trace.outcome = outcome
    trace.reason = reason
    trace.finished_at = _now()
    try:
        _save_trace(output_dir, trace)
    except Exception as exc:
        trace.outcome = ReproductionOutcome.SYSTEM_ERROR
        trace.reason = f"复现证据写入失败: {exc}"
    return trace


def _observation_signature(observation: StepObservation) -> tuple[Any, ...] | None:
    """用截图内容和窗口状态识别连续无进展，不依赖 GUI_TREE。"""
    if not observation.screenshot_ok or not observation.screenshot_path:
        return None
    try:
        image_hash = hashlib.sha256(Path(observation.screenshot_path).read_bytes()).hexdigest()
    except OSError:
        return None
    return (
        image_hash,
        observation.window_id,
        observation.window_name,
        observation.popup_id,
        observation.popup_name,
    )


def _is_successful_background_state_action(observation: StepObservation) -> bool:
    """后台状态准备本来就可能不改画面，成功时不累计界面无进展。"""
    decision = observation.decision
    if not decision or decision.action != ReproductionAction.EXECUTE:
        return False
    if observation.command_status in _COMMAND_FAILURE_STATUSES:
        return False
    try:
        command = normalize_command(decision.command or "")
    except ValueError:
        return False
    command_name = command[1:].partition(":")[0]
    return command_name in _BACKGROUND_STATE_COMMANDS


def _command_error_result(command: str, reason: str) -> CommandResult:
    request = command.lstrip(":").partition(":")[0].lower() or "unknown"
    raw = {
        "type": "host_command_error",
        "request": request,
        "status": "error",
        "reason": reason,
    }
    return CommandResult(request=request, status="error", raw=raw)


def _stop_session(session: DeviceSession) -> None:
    try:
        session.stop()
    except Exception:
        pass


def successful_reproduction_commands(trace: ReproductionTrace) -> list[str]:
    """按实际执行顺序提取可复用的业务命令，不包含系统观察命令和失败命令。"""
    commands: list[str] = []
    for observation in trace.steps:
        decision = observation.decision
        if not decision or decision.action != ReproductionAction.EXECUTE:
            continue
        if observation.command_status in _COMMAND_FAILURE_STATUSES:
            continue
        try:
            command = normalize_command(decision.command or "")
        except ValueError:
            continue
        command_name = command[1:].partition(":")[0]
        if command_name not in _SYSTEM_OBSERVATION_COMMANDS:
            commands.append(command)
    return commands


def verified_observation_facts(trace: ReproductionTrace) -> list[str]:
    """只提取程序采集的复现事实，供视觉裁决确认截图对象来源。"""
    facts: list[str] = []
    for observation in trace.steps:
        parts = [f"步骤 {observation.step}"]
        decision = observation.decision
        if decision and decision.action == ReproductionAction.EXECUTE and decision.command:
            parts.append(f"执行命令 {decision.command}")
        if observation.command_status:
            parts.append(f"命令状态 {observation.command_status}")
        if observation.window_name:
            parts.append(f"观察窗口 {observation.window_name}")
        if observation.popup_name:
            parts.append(f"观察弹窗 {observation.popup_name}")
        facts.append("；".join(parts))
    return facts


def interactive_reproduce(
    *,
    task_id: str,
    objective: str,
    source_files: list[dict],
    defect_image_paths: list[str] | None,
    evidence_dir: str | os.PathLike[str],
    max_actions: int = 6,
    target: str = "simulator",
    test_case: dict[str, str] | None = None,
    build_simulator: bool = True,
    reset_hardware: bool = True,
) -> ReproductionTrace:
    """在选定目标上按“观察→一个动作→再观察”完成缺陷复现或普通测试。

    simulator 保持原有的一次构建、一次会话；hardware 跳过构建，直接使用
    当前已烧录的 6202 Debug 固件，并从核对后的 6202 源码即时加载命令表；
    默认在首次观察前调用统一真机状态清理入口。
    """
    from agent_loop_system.tools.agent import decide_reproduction_action
    from agent_loop_system.tools.build import BuildConfig, run_build
    from agent_loop_system.tools.test import (
        get_simulator_exe,
        judge_test_with_vision,
        judge_with_vision,
    )

    if max_actions < 1:
        raise ValueError("max_actions 必须大于 0")
    if target not in {"simulator", "hardware"}:
        raise ValueError(f"未知执行目标: {target}")

    output_dir = Path(evidence_dir).resolve()
    trace = ReproductionTrace(
        task_id=task_id,
        max_steps=max_actions,
        started_at=_now(),
    )
    try:
        _save_trace(output_dir, trace)
    except Exception as exc:
        trace.outcome = ReproductionOutcome.SYSTEM_ERROR
        trace.reason = f"复现证据目录不可写: {exc}"
        trace.finished_at = _now()
        return trace

    capability_knowledge: str | None = None
    navigation_source_root: str | None = None
    try:
        if target == "hardware":
            from agent_loop_system.tools.hardware_target import (
                HardwareTargetConfig,
                build_hardware_agent_knowledge,
                load_hardware_command_capabilities,
            )
            from agent_loop_system.tools.real_device import (
                RealDeviceSession,
                reset_hardware_case_state,
            )

            hardware_config = HardwareTargetConfig.from_env()
            capabilities = load_hardware_command_capabilities(hardware_config)
            capability_knowledge = build_hardware_agent_knowledge(hardware_config)
            navigation_source_root = str(hardware_config.source_root)
            if reset_hardware:
                reset_hardware_case_state(evidence_dir=output_dir / "hardware-reset")
            session: DeviceSession = RealDeviceSession(evidence_dir=output_dir)
        else:
            capabilities = load_current_command_capabilities()
            if build_simulator:
                config = BuildConfig.from_env()
                build_result = run_build(config)
                if not build_result.success:
                    return _finish_trace(
                        output_dir,
                        trace,
                        ReproductionOutcome.SYSTEM_ERROR,
                        f"修复前源码构建失败: {build_result.error_message or '未知错误'}",
                    )
                simulator_exe = build_result.artifact_path or get_simulator_exe()
            else:
                simulator_exe = get_simulator_exe()
            session = SimulatorSession(simulator_exe)
    except Exception as exc:
        return _finish_trace(
            output_dir,
            trace,
            ReproductionOutcome.SYSTEM_ERROR,
            f"复现环境配置或构建失败: {exc}",
        )

    try:
        session.start()
        if target == "simulator":
            screen_result = session.send(
                f":DISPLAY_TIME_SET:{_REPRODUCTION_SCREEN_ON_SECONDS}"
            )
            if screen_result.status in _COMMAND_FAILURE_STATUSES:
                raise RuntimeError(_response_reason(screen_result))
    except Exception as exc:
        _stop_session(session)
        return _finish_trace(
            output_dir,
            trace,
            ReproductionOutcome.SYSTEM_ERROR,
            f"{target} 会话启动失败: {exc}",
        )

    try:
        observation = observe(
            session,
            step=0,
            seq=1000,
            evidence_dir=output_dir,
        )
        trace.steps.append(observation)
        _save_trace(output_dir, trace)
        if not observation.screenshot_ok:
            return _finish_trace(
                output_dir,
                trace,
                ReproductionOutcome.SYSTEM_ERROR,
                "初始截图失败，无法进行视觉复现",
            )

        previous_signature = _observation_signature(observation)
        unchanged_steps = 0
        command_errors = 0
        business_actions = 0
        decision_rounds = 0
        max_decision_rounds = max_actions + 2

        while decision_rounds < max_decision_rounds:
            decision_rounds += 1
            decision = decide_reproduction_action(
                objective=objective,
                source_files=source_files,
                trace=trace,
                defect_image_paths=defect_image_paths,
                execution_target=target,
                capability_knowledge=capability_knowledge,
                navigation_source_root=navigation_source_root,
                test_case=test_case,
            )
            if decision is None:
                return _finish_trace(
                    output_dir,
                    trace,
                    ReproductionOutcome.SYSTEM_ERROR,
                    "执行 Agent 配置不可用：未能初始化执行 Agent API",
                )

            if decision.action == ReproductionAction.BLOCKED:
                return _finish_trace(
                    output_dir,
                    trace,
                    ReproductionOutcome.CAPABILITY_MISSING,
                    decision.reason,
                )

            if decision.action == ReproductionAction.READY_TO_JUDGE:
                current = trace.steps[-1]
                if not current.screenshot_ok or not current.screenshot_path:
                    return _finish_trace(
                        output_dir,
                        trace,
                        ReproductionOutcome.SYSTEM_ERROR,
                        "最终截图不可用，无法视觉判定",
                    )
                if test_case is None:
                    verdict = judge_with_vision(
                        current.screenshot_path,
                        objective,
                        defect_image_paths=defect_image_paths,
                        verified_observations=verified_observation_facts(trace),
                    )
                else:
                    screenshots = [
                        {
                            "path": item.screenshot_path,
                            "label": (
                                item.decision.reason
                                if item.decision is not None
                                else "执行前初始画面"
                            ),
                        }
                        for item in trace.steps
                        if item.screenshot_ok and item.screenshot_path
                    ]
                    verdict = judge_test_with_vision(
                        str(test_case.get("expected_text") or ""),
                        screenshots,
                        project=str(test_case.get("project") or ""),
                    )
                trace.verdict = verdict.verdict
                outcome = {
                    "FAIL": ReproductionOutcome.DEFECT_REPRODUCED,
                    "PASS": ReproductionOutcome.CURRENT_CONFORMS,
                    "CANNOT_VERIFY": ReproductionOutcome.REFERENCE_AMBIGUOUS,
                }.get(verdict.verdict, ReproductionOutcome.SYSTEM_ERROR)
                return _finish_trace(
                    output_dir,
                    trace,
                    outcome,
                    f"{decision.reason}；视觉判定：{verdict.reason}",
                )

            command_result = None
            if decision.action == ReproductionAction.EXECUTE:
                if business_actions >= max_actions:
                    return _finish_trace(
                        output_dir,
                        trace,
                        ReproductionOutcome.TARGET_NOT_REACHED,
                        f"已达到 {max_actions} 个业务动作上限",
                    )
                business_actions += 1
                try:
                    command = normalize_command(decision.command or "")
                    command_name = command[1:].partition(":")[0]
                    if command_name in _SYSTEM_OBSERVATION_COMMANDS:
                        raise ValueError(f"{command_name} 由系统观察负责，Agent 不得执行")
                    if target == "hardware":
                        from agent_loop_system.tools.hardware_target import (
                            hardware_command_allowed,
                        )

                        allowed, reason = hardware_command_allowed(command_name)
                        if not allowed:
                            raise ValueError(reason or f"真机命令 {command_name} 被拒绝")
                    validate_agent_command(command, capabilities)
                    command_result = session.send(command)
                    if command_result.status in _COMMAND_FAILURE_STATUSES:
                        command_errors += 1
                    else:
                        command_errors = 0
                except Exception as exc:
                    command_errors += 1
                    command_result = _command_error_result(
                        decision.command or "",
                        str(exc),
                    )

            step = len(trace.steps)
            observation = observe(
                session,
                step=step,
                seq=1000 + step * 10,
                evidence_dir=output_dir,
                decision=decision,
                command_result=command_result,
            )
            trace.steps.append(observation)
            _save_trace(output_dir, trace)

            if not observation.screenshot_ok:
                return _finish_trace(
                    output_dir,
                    trace,
                    ReproductionOutcome.SYSTEM_ERROR,
                    f"step {step} 截图失败，无法继续视觉复现",
                )
            if command_errors > 1:
                return _finish_trace(
                    output_dir,
                    trace,
                    ReproductionOutcome.COMMAND_RUNTIME_ERROR,
                    "业务命令连续修正一次后仍失败",
                )

            signature = _observation_signature(observation)
            if signature is not None and signature == previous_signature:
                if not _is_successful_background_state_action(observation):
                    unchanged_steps += 1
            else:
                unchanged_steps = 0
            previous_signature = signature
            if unchanged_steps >= 2:
                return _finish_trace(
                    output_dir,
                    trace,
                    ReproductionOutcome.TARGET_NOT_REACHED,
                    "连续 2 步截图和窗口状态没有变化",
                )

        return _finish_trace(
            output_dir,
            trace,
            ReproductionOutcome.TARGET_NOT_REACHED,
            f"达到 {max_decision_rounds} 轮决策上限",
        )
    except Exception as exc:
        message = str(exc)
        if not message.startswith("执行 Agent API 出错"):
            message = f"交互式复现异常：{message}"
        return _finish_trace(
            output_dir,
            trace,
            ReproductionOutcome.SYSTEM_ERROR,
            message,
        )
    finally:
        _stop_session(session)
