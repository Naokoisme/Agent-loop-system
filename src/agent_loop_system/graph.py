"""LangGraph 图编排：先复现，再修复。

流程：validate → interactive_reproduce → agent → apply → build → test → record
                                                        失败且未超限 → agent

节点职责：
- validate：task_id 正则 + objective 校验，失败直接 CANNOT_VERIFY 结束。
- interactive_reproduce：一次构建和模拟器会话内小步观察、执行并判定复现结果。
- agent：仅在 DEFECT_REPRODUCED 后生成 Designer + 可选 VM USER 计划。
- apply：先让 Designer 生成 V/VM，再可选修改 VM USER 区。
- build：构建 patch 后源码；失败时用 before.bmp 占位 after.bmp。
- test：执行同一组命令 → 判定修复结果 → after.bmp → 三态聚合。
- record：非 PASS 回滚 Designer 多文件事务、必要时恢复构建产物、写入每轮 history。
"""
from __future__ import annotations

import re
import shutil
from pathlib import Path

from langgraph.graph import END, START, StateGraph

from agent_loop_system.reporting import tracked_node
from agent_loop_system.reproduction import (
    ReproductionOutcome,
    interactive_reproduce as run_interactive_reproduction,
    successful_reproduction_commands,
)
from agent_loop_system.state import LoopState
from agent_loop_system.runtime_root import RuntimePaths
from agent_loop_system.tools.build import BuildConfig, run_build
from agent_loop_system.tools.source_context import (
    SourceContextError,
    read_source_files,
    read_source_files_from_root,
)
from agent_loop_system.tools.workspace import WorkspaceConflictError, resolve_source_root

EVIDENCE_ROOT = RuntimePaths.from_root().evidence
_TASK_ID_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


# --------------------------------------------------------------------------- #
# validate
# --------------------------------------------------------------------------- #

def validate(state: LoopState) -> dict:
    """校验输入；task_id 或 objective 不合法：CANNOT_VERIFY 直接结束。"""
    task_id = state.get("task_id", "")
    if not task_id or not _TASK_ID_RE.fullmatch(task_id):
        return {
            "verdict": "CANNOT_VERIFY",
            "error": f"task_id 不合法: {task_id!r}",
            "error_code": "INPUT_INVALID",
            "attempts": 0,
            "history": [],
        }
    if not state.get("objective"):
        return {
            "verdict": "CANNOT_VERIFY",
            "error": "objective 必填",
            "error_code": "INPUT_INVALID",
            "attempts": 0,
            "history": [],
        }
    target = state.get("target", "simulator")
    if target not in {"simulator", "hardware"}:
        return {
            "verdict": "CANNOT_VERIFY",
            "error": f"执行目标不合法: {target!r}",
            "error_code": "INPUT_INVALID",
            "attempts": 0,
            "history": [],
        }
    return {
        "verdict": "PENDING",
        "attempts": 0,
        "history": [],
        "reproduction_attempts": 0,
        "max_attempts": state.get("max_attempts", 5),
        "error": None,
        "target": target,
    }


def _after_validate(state: LoopState) -> str:
    return END if state.get("verdict") == "CANNOT_VERIFY" else "interactive_reproduce"


# --------------------------------------------------------------------------- #
# shared preparation
# --------------------------------------------------------------------------- #

def _repair_round_clear() -> dict:
    """清空修复轮字段，保留已经验证过的复现命令和 baseline。"""
    return {
        "patch": None,
        "patch_reason": "",
        "repair_mode": "",
        "designer_plan": None,
        "designer_context": None,
        "designer_transaction": None,
        "build_success": None,
        "build_result": None,
        "test_output": None,
        "patch_applied": False,
        "patch_offset": None,
        "rollback_error": None,
        "restore_build_result": None,
        "restore_build_error": None,
        "verdict": "PENDING",
        "error": None,
        "error_code": None,
    }


def _source_invalid(reason: str) -> dict:
    """源码输入不合法：CANNOT_VERIFY 直接结束，不调用 LLM。"""
    update = _repair_round_clear()
    update["verdict"] = "CANNOT_VERIFY"
    update["error"] = reason
    update["error_code"] = "SOURCE_INPUT_INVALID"
    update["baseline_ready"] = False
    return update


def _load_source_files(state: LoopState) -> tuple[list[dict], list[str], dict | None]:
    """统一读取受边界保护的源码，供复现规划和修复共用。"""
    try:
        if state.get("target", "simulator") == "hardware":
            from agent_loop_system.tools.hardware_target import HardwareTargetConfig

            hardware_config = HardwareTargetConfig.from_env()
            source_files = read_source_files_from_root(
                list(state.get("source_files", [])), hardware_config.source_root
            )
        else:
            source_files = read_source_files(list(state.get("source_files", [])))
    except (SourceContextError, WorkspaceConflictError, ValueError) as exc:
        return [], [], _source_invalid(str(exc))
    loaded_paths = [item["path"] for item in source_files]
    return source_files, loaded_paths, None


def _augment_designer_repair_sources(
    source_files: list[dict], designer_page: dict, reproduction_trace: dict | None
) -> list[dict]:
    """补充运行时经过页面和 Designer CommonModule 源码，只读且去重。"""
    from agent_loop_system.tools.source_context import load_runtime_navigation_sources

    result = list(source_files)
    seen = {str(item.get("path") or "").casefold() for item in result}

    def append(items: list[dict]) -> None:
        for item in items:
            path = str(item.get("path") or "")
            if path and path.casefold() not in seen:
                result.append(item)
                seen.add(path.casefold())

    runtime_pairs: list[tuple[str, str]] = []
    for step in reversed((reproduction_trace or {}).get("steps") or []):
        decision = step.get("decision") or {}
        command = str(decision.get("command") or "")
        match = re.match(r"^:ENTER_PAGE:([A-Z][A-Z0-9_]*),", command)
        if not match:
            continue
        target = match.group(1)
        actual = str(step.get("popup_name") or step.get("window_name") or "").strip()
        pair = (target, actual)
        if pair not in runtime_pairs:
            runtime_pairs.append(pair)
        if len(runtime_pairs) >= 3:
            break
    for target, actual in runtime_pairs:
        try:
            items = load_runtime_navigation_sources(
                target,
                actual if actual and actual != target else "",
            )
            append(items[:1])
        except (SourceContextError, WorkspaceConflictError):
            continue

    bindings = designer_page.get("bindings") or {}
    services: list[str] = []
    for key in ("commonModuleProperties", "commonModuleEvents", "directProperties"):
        for binding in bindings.get(key) or []:
            service = str(binding.get("ServiceId") or binding.get("serviceId") or "").strip()
            if service and service not in services:
                services.append(service)
    source_root = resolve_source_root()
    # 页面 VM 与所有 CommonModule 都通过该注册表解析 app_id/window/icon。
    for common_path in (
        "app/comm/TuoBu/app/gui_comm_app.c",
        "app/comm/TuoBu/app/gui_comm_app.h",
    ):
        if (source_root / common_path).is_file():
            try:
                append(read_source_files([common_path]))
            except (SourceContextError, WorkspaceConflictError):
                pass
    for service in services[:8]:
        matches = sorted(
            source_root.glob(f"app/comm/*/{service}/gui_comm_{service}.c")
        )
        if not matches:
            continue
        relative = matches[0].relative_to(source_root).as_posix()
        try:
            append(read_source_files([relative]))
        except (SourceContextError, WorkspaceConflictError):
            continue
    return result


def _clean_evidence(task_id: str) -> None:
    """复现尝试前删除当前任务的 before.bmp/after.bmp。"""
    task_dir = (EVIDENCE_ROOT / task_id).resolve()
    try:
        task_dir.relative_to(EVIDENCE_ROOT.resolve())
    except ValueError:
        return
    for name in ("before.bmp", "after.bmp"):
        target = task_dir / name
        if target.exists():
            target.unlink()


def _clean_after_evidence(task_id: str) -> None:
    """修复重试只删除 after.bmp，保留已确认的 before.bmp。"""
    task_dir = (EVIDENCE_ROOT / task_id).resolve()
    try:
        task_dir.relative_to(EVIDENCE_ROOT.resolve())
    except ValueError:
        return
    target = task_dir / "after.bmp"
    if target.exists():
        target.unlink()


def _clean_interactive_evidence(task_id: str) -> Path:
    """只清理当前任务可重建的交互复现过程文件。"""
    task_dir = (EVIDENCE_ROOT / task_id).resolve()
    task_dir.relative_to(EVIDENCE_ROOT.resolve())
    reproduction_dir = task_dir / "reproduction"
    if reproduction_dir.is_dir():
        for pattern in ("step_*.bmp", "step_*.json", "trace.json", "trace.json.tmp"):
            for target in reproduction_dir.glob(pattern):
                if target.is_file():
                    target.unlink()
    return reproduction_dir


def _trace_terminal_json(trace) -> list[dict]:
    terminal_json: list[dict] = []
    for observation in trace.steps:
        terminal_json.extend(observation.command_results)
    return terminal_json


def interactive_reproduce_node(state: LoopState) -> dict:
    """读取一次源码上下文，运行单会话交互复现，并映射为现有修复阶段输入。"""
    source_files, _, source_error = _load_source_files(state)
    if source_error:
        return source_error

    task_id = state.get("task_id", "unknown")
    _clean_evidence(task_id)
    try:
        reproduction_dir = _clean_interactive_evidence(task_id)
    except ValueError:
        return _source_invalid(f"task_id 越出证据目录: {task_id}")

    trace = run_interactive_reproduction(
        task_id=task_id,
        objective=state.get("objective", ""),
        source_files=source_files,
        defect_image_paths=state.get("defect_image_paths", []),
        evidence_dir=reproduction_dir,
        max_actions=6,
        target=state.get("target", "simulator"),
    )
    trace_payload = trace.model_dump(mode="json")
    outcome = trace.outcome or ReproductionOutcome.SYSTEM_ERROR
    commands = successful_reproduction_commands(trace)
    terminal_json = _trace_terminal_json(trace)
    reason = trace.reason or "交互式复现没有返回终止原因"

    final_screenshot = next(
        (
            Path(observation.screenshot_path)
            for observation in reversed(trace.steps)
            if observation.screenshot_ok and observation.screenshot_path
        ),
        None,
    )
    before_path = EVIDENCE_ROOT / task_id / "before.bmp"
    evidence_issue = None
    if final_screenshot and final_screenshot.is_file():
        before_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(final_screenshot, before_path)
    else:
        evidence_issue = "交互式复现没有可用的最终截图"

    reproduced = outcome == ReproductionOutcome.DEFECT_REPRODUCED
    if reproduced and evidence_issue:
        outcome = ReproductionOutcome.SYSTEM_ERROR
        reproduced = False
        reason = evidence_issue

    baseline_output = {
        "defect_reproduced": True if reproduced else False if outcome == ReproductionOutcome.CURRENT_CONFORMS else None,
        "reason": reason,
        "test_commands": commands,
        "terminal_json": terminal_json,
        "evidence_issue": evidence_issue,
        "reproduction_outcome": outcome.value,
        "trace_path": str(reproduction_dir / "trace.json"),
    }
    error_codes = {
        ReproductionOutcome.TARGET_NOT_REACHED: "TARGET_NOT_REACHED",
        ReproductionOutcome.REFERENCE_AMBIGUOUS: "REFERENCE_AMBIGUOUS",
        ReproductionOutcome.CAPABILITY_MISSING: "CAPABILITY_MISSING",
        ReproductionOutcome.COMMAND_RUNTIME_ERROR: "COMMAND_RUNTIME_ERROR",
        ReproductionOutcome.SYSTEM_ERROR: "REPRODUCTION_SYSTEM_ERROR",
    }
    current_conforms = outcome == ReproductionOutcome.CURRENT_CONFORMS
    hardware_reproduced = (
        state.get("target", "simulator") == "hardware" and reproduced
    )
    if hardware_reproduced:
        reason = (
            f"{reason}；缺陷已在当前真机项目复现。自动修复已暂停："
            "本地源码修改尚未编译并由人工授权刷入设备，不能用旧固件画面验证修复"
        )
        baseline_output["reason"] = reason
    return {
        "reproduction_attempts": 1,
        "reproduction_reason": reason,
        "reproduction_outcome": outcome.value,
        "reproduction_trace": trace_payload,
        "agent_test_commands": commands,
        "baseline_output": baseline_output,
        "baseline_ready": reproduced and not hardware_reproduced,
        "patch": None,
        "patch_reason": "",
        "patch_applied": False,
        "patch_offset": None,
        "patch_retained": False,
        "patched_artifact": False,
        "verdict": (
            "CANNOT_VERIFY"
            if hardware_reproduced
            else "PENDING"
            if reproduced
            else "PASS"
            if current_conforms
            else "CANNOT_VERIFY"
        ),
        "error": reason if hardware_reproduced else None if reproduced or current_conforms else reason,
        "error_code": (
            "HARDWARE_FLASH_REQUIRED" if hardware_reproduced else error_codes.get(outcome)
        ),
    }


def _after_interactive_reproduce(state: LoopState) -> str:
    if (
        state.get("target", "simulator") == "hardware"
        and state.get("reproduction_outcome")
        == ReproductionOutcome.DEFECT_REPRODUCED.value
    ):
        return END
    if (
        state.get("reproduction_outcome") == ReproductionOutcome.DEFECT_REPRODUCED.value
        and state.get("baseline_ready")
    ):
        return "agent"
    return END


# --------------------------------------------------------------------------- #
# agent
# --------------------------------------------------------------------------- #


def agent_node(state: LoopState) -> dict:
    """可靠复现后生成强制 Designer -> 生成代码 -> VM USER 的修复计划。"""
    from agent_loop_system.tools.agent import generate_designer_plan
    from agent_loop_system.tools.defect_store import DEFECTS_IMG_ROOT
    from agent_loop_system.tools.designer import (
        DesignerIntegrationError,
        load_designer_context,
        validate_designer_plan,
    )
    from agent_loop_system.tools.llm_retry import LLMRetryError

    source_files, _, source_error = _load_source_files(state)
    if source_error:
        return source_error

    baseline_output = state.get("baseline_output") or {}
    if not state.get("baseline_ready") or baseline_output.get("defect_reproduced") is not True:
        update = _repair_round_clear()
        update["verdict"] = "CANNOT_VERIFY"
        update["error"] = "修复前缺陷尚未可靠复现，禁止调用修复 AI"
        update["error_code"] = "REPRODUCTION_REQUIRED"
        return update

    _clean_after_evidence(state.get("task_id", "unknown"))
    repair_context = {
        "baseline": baseline_output,
        "reproduction_trace": state.get("reproduction_trace"),
        "last_repair_test": state.get("test_output"),
    }
    img_dir = DEFECTS_IMG_ROOT / state.get("task_id", "unknown")
    if not state.get("designer_enabled"):
        update = _repair_round_clear()
        update["verdict"] = "CANNOT_VERIFY"
        update["error"] = "Designer 修复未启用；禁止降级为手改生成代码或普通源码 patch"
        update["error_code"] = "DESIGNER_REQUIRED"
        return update
    try:
        designer_context = load_designer_context(state.get("reproduction_trace"))
        if designer_context is None:
            update = _repair_round_clear()
            update["verdict"] = "CANNOT_VERIFY"
            update["error"] = "运行时缺陷页面不在当前 Designer Main layout 中，禁止源码绕过"
            update["error_code"] = "DESIGNER_PAGE_NOT_FOUND"
            return update
        designer_context_payload = {
            "available": True,
            "page_name": designer_context.page_name,
            "ui_layout_name": designer_context.ui_layout_name,
            "native_tool_count": len(designer_context.native_tool_schemas),
            "capability_groups": designer_context.capability_groups,
            "vm_user_files": sorted(designer_context.vm_user_sections),
        }
        repair_source_files = _augment_designer_repair_sources(
            source_files,
            designer_context.page,
            state.get("reproduction_trace"),
        )
        designer_context_payload["repair_source_files"] = [
            str(item.get("path") or "") for item in repair_source_files
        ]
        designer_plan = generate_designer_plan(
            objective=state.get("objective", ""),
            page_name=designer_context.page_name,
            page_context=designer_context.page,
            native_tool_schemas=designer_context.native_tool_schemas,
            capability_groups=designer_context.capability_groups,
            reference_data=designer_context.reference_data,
            vm_user_sections=designer_context.vm_user_sections,
            source_files=repair_source_files,
            last_test_output=repair_context,
            image_dir=str(img_dir) if img_dir.is_dir() else None,
            verified_test_commands=state.get("agent_test_commands", []),
        )
        if designer_plan is None:
            update = _repair_round_clear()
            update["verdict"] = "CANNOT_VERIFY"
            update["error"] = "LLM 配置不可用（API key 缺失或初始化失败）"
            update["error_code"] = "LLM_UNAVAILABLE"
            return update
        validate_designer_plan(designer_plan, designer_context)
        if designer_plan.blocked_capability is not None:
            capability = designer_context.capability_groups[
                designer_plan.blocked_capability
            ]
            update = _repair_round_clear()
            update["verdict"] = "CANNOT_VERIFY"
            update["error"] = (
                f"Designer Adapter 尚未提供完成本次修复所需能力: "
                f"{designer_plan.blocked_capability}; "
                f"缺少工具 {capability.get('missingTools') or []}"
            )
            update["error_code"] = "DESIGNER_CAPABILITY_MISSING"
            update["designer_context"] = designer_context_payload
            update["test_output"] = {
                "stage": "designer_capability",
                "capability": designer_plan.blocked_capability,
                "missing_tools": capability.get("missingTools") or [],
            }
            return update
    except LLMRetryError as exc:
        update = _repair_round_clear()
        update["verdict"] = "CANNOT_VERIFY"
        update["error"] = f"LLM 不可用: {exc}"
        update["error_code"] = "LLM_UNAVAILABLE"
        return update
    except (DesignerIntegrationError, OSError, ValueError) as exc:
        update = _repair_round_clear()
        update["verdict"] = "FAIL"
        update["error"] = f"Designer 修复计划无效: {exc}"
        update["error_code"] = "DESIGNER_PLAN_INVALID"
        update["test_output"] = {"stage": "designer_plan_validation", "error": str(exc)}
        return update
    update = _repair_round_clear()
    designer_context_payload["reason"] = designer_plan.reason
    update["designer_plan"] = designer_plan.model_dump()
    update["patch_reason"] = designer_plan.reason
    update["agent_test_commands"] = list(state.get("agent_test_commands", []))
    update["repair_mode"] = "designer_vm"
    update["designer_context"] = designer_context_payload
    return update


def _after_agent(state: LoopState) -> str:
    """只有通过边界校验的 Designer 计划才允许进入 apply。"""
    if state.get("designer_plan") is None:
        return "record"
    return "apply"


# --------------------------------------------------------------------------- #
# apply
# --------------------------------------------------------------------------- #

def apply_patch_node(state: LoopState) -> dict:
    """只执行 Designer 多文件事务；VM USER 补丁由事务在生成后应用。"""
    from agent_loop_system.tools.agent import Patch
    from agent_loop_system.tools.designer import DesignerPlan, apply_designer_plan

    designer_plan_dict = state.get("designer_plan")
    if not designer_plan_dict:
        return {
            "verdict": "FAIL",
            "error": "无 Designer 计划可应用；禁止源码降级",
            "error_code": "DESIGNER_PLAN_REQUIRED",
            "patch_applied": False,
        }
    plan = DesignerPlan(**designer_plan_dict)
    result = apply_designer_plan(
        task_id=state.get("task_id", "unknown"),
        plan=plan,
        evidence_root=EVIDENCE_ROOT,
    )
    if not result.success:
        return {
            "verdict": "FAIL",
            "error": f"Designer 应用失败: {result.error}",
            "error_code": "DESIGNER_APPLY_FAILED",
            "patch_applied": False,
            "designer_transaction": result.model_dump(),
        }
    display_patch = Patch(
        root_cause_analysis=plan.root_cause_analysis,
        file_path=result.page_json_path,
        before=f"Designer 多文件事务写前快照: {result.transaction_manifest}",
        after=(
            f"Designer 生成文件: {', '.join(result.changed_files)}"
            + (
                f"；生成后 VM USER 补丁: {result.vm_user_patch_path}"
                if result.vm_user_patch_path
                else ""
            )
        ),
        reason=plan.reason,
        test_commands=list(state.get("agent_test_commands", [])),
    )
    return {
        "patch": display_patch.model_dump(),
        "patch_reason": plan.reason,
        "patch_applied": True,
        "patch_offset": None,
        "repair_mode": "designer_vm",
        "designer_transaction": result.model_dump(),
        "error": None,
        "error_code": None,
    }


def _after_apply(state: LoopState) -> str:
    return "build" if state.get("patch_applied") else "record"


# --------------------------------------------------------------------------- #
# build
# --------------------------------------------------------------------------- #

def build(state: LoopState) -> dict:
    """构建 patch 后源码。失败时用 before.bmp 占位 after.bmp。"""
    try:
        config = BuildConfig.from_env()
    except KeyError as exc:
        return {
            "build_success": False,
            "verdict": "CANNOT_VERIFY",
            "error": f"环境变量缺失: {exc}",
        }
    result = run_build(config)
    if result.success:
        return {
            "build_success": True,
            "build_result": result.model_dump(),
            "patched_artifact": True,
        }

    # 构建失败：先占位 after.bmp，再进入 record
    shot_dir = EVIDENCE_ROOT / state.get("task_id", "unknown")
    before = shot_dir / "before.bmp"
    if before.exists():
        try:
            shutil.copy2(before, shot_dir / "after.bmp")
        except OSError:
            before = None
    if before is None:
        return {
            "build_success": False,
            "verdict": "CANNOT_VERIFY",
            "build_result": result.model_dump(),
            "test_output": {"evidence_issue": "构建失败，before.bmp 缺失或占位失败"},
            "error": f"构建失败且证据占位失败: {result.error_message}",
        }
    return {
        "build_success": False,
        "verdict": "FAIL",
        "build_result": result.model_dump(),
        "test_output": {"evidence_issue": "构建失败，after.bmp 为 before.bmp 占位副本"},
        "error": result.error_message,
    }


def _after_build(state: LoopState) -> str:
    return "test" if state.get("build_success") else "record"


# --------------------------------------------------------------------------- #
# test
# --------------------------------------------------------------------------- #

def test(state: LoopState) -> dict:
    """执行同一组命令 → 截 after.bmp → 视觉判定修复结果 → 三态聚合。"""
    from agent_loop_system.tools.command_protocol import collect_command_json, normalize_command
    from agent_loop_system.tools.simulator import SimulatorSession
    from agent_loop_system.tools.test import (
        aggregate_verdicts,
        get_simulator_exe,
        initialize_hardware_case_session,
        judge_case_result,
        judge_with_vision,
        run_single_case,
    )

    task_id = state.get("task_id", "unknown")
    shot_dir = EVIDENCE_ROOT / task_id
    shot_dir.mkdir(parents=True, exist_ok=True)
    after_path = str(shot_dir / "after.bmp")
    before_path = str(shot_dir / "before.bmp")
    agent_cmds = state.get("agent_test_commands", [])
    test_cases = state.get("test_cases", [])
    defect_criteria = state.get("judge_criteria", "")
    target = state.get("target", "simulator")

    # case_map 模式必须和前端/CLI 共用同一个入口；不能让未固化用例落入空映射执行器。
    if test_cases and not agent_cmds:
        results = []
        terminal_json_all: list[dict] = []
        shot_ok = False
        for case_index, tc in enumerate(test_cases, start=1):
            sheet = str(tc.get("sheet") or "")
            case_id = str(tc.get("case_id") or "")
            case_dir = shot_dir / f"case-{case_index:03d}-{case_id}"
            try:
                case_result = run_single_case(
                    sheet,
                    case_id,
                    str(case_dir / "screenshot.bmp"),
                    target=target,
                )
                decision = judge_case_result(case_result)
            except Exception as exc:
                results.append({
                    "case_id": case_id,
                    "sheet": sheet,
                    "verdict": "ERROR",
                    "reason": f"用例执行异常: {exc}",
                    "terminal_json": [],
                    "screenshots": [],
                })
                continue

            terminal_json_all.extend(case_result.terminal_json)
            results.append({
                "case_id": case_id,
                "sheet": sheet,
                "verdict": decision.verdict,
                "reason": decision.reason,
                "execution_mode": case_result.execution_mode,
                "expected_text": case_result.expected_text,
                "verification_points": case_result.verification_points,
                "planned_commands": case_result.planned_commands,
                "command_trace": case_result.command_trace,
                "evidence_contract": case_result.evidence_contract,
                "errors": (
                    case_result.setup_errors
                    + case_result.action_errors
                    + case_result.collect_errors
                ),
                "terminal_json": case_result.terminal_json,
                "screenshots": case_result.screenshots,
                "exploration_trace": case_result.exploration_trace,
            })
            for screenshot in reversed(case_result.screenshots):
                source = Path(str(screenshot.get("path") or ""))
                if not source.is_file():
                    continue
                shutil.copy2(source, after_path)
                shot_ok = True
                break

        if defect_criteria and shot_ok:
            ref = before_path if Path(before_path).is_file() else None
            verdict = judge_with_vision(
                after_path,
                defect_criteria,
                reference_screenshot=ref,
                defect_image_paths=state.get("defect_image_paths", []),
            )
            results.append({
                "case_id": "agent_generated",
                "sheet": "agent",
                "verdict": verdict.verdict,
                "reason": verdict.reason,
                "test_commands": [],
                "terminal_json": terminal_json_all,
            })
        elif defect_criteria and not shot_ok:
            results.append({
                "case_id": "agent_generated",
                "sheet": "agent",
                "verdict": "CANNOT_VERIFY",
                "reason": "after.bmp 截图失败，无法视觉判定",
                "test_commands": [],
                "terminal_json": terminal_json_all,
            })

        evidence_issue = None if shot_ok else "after.bmp 截图失败"
        final_verdict = aggregate_verdicts([item["verdict"] for item in results])
        if not shot_ok:
            final_verdict = "CANNOT_VERIFY"
        return {
            "verdict": final_verdict,
            "test_output": {"results": results, "evidence_issue": evidence_issue},
        }

    if target == "hardware":
        from agent_loop_system.tools.hardware_target import HardwareTargetConfig
        from agent_loop_system.tools.real_device import (
            RealDeviceSession,
            reset_hardware_case_state,
        )

        try:
            HardwareTargetConfig.from_env()
            reset_hardware_case_state(evidence_dir=shot_dir / "hardware-reset")
            session = RealDeviceSession(evidence_dir=shot_dir)
        except Exception as exc:
            return {
                "verdict": "CANNOT_VERIFY",
                "error": f"真机环境配置或状态清理失败: {exc}",
                "test_output": {
                    "results": [],
                    "evidence_issue": "真机环境配置或状态清理失败",
                },
            }
    else:
        session = SimulatorSession(get_simulator_exe())
    try:
        try:
            session.start()
            if target == "hardware" and agent_cmds:
                initialize_hardware_case_session(session)
        except Exception as exc:
            return {
                "verdict": "CANNOT_VERIFY",
                "error": f"{target} 会话启动失败: {exc}",
                "test_output": {"results": [], "evidence_issue": f"{target} 会话启动失败"},
            }
        results = []
        terminal_json_all: list[dict] = []
        try:
            if agent_cmds:
                terminal_json: list[dict] = []
                for cmd in agent_cmds:
                    resp = session.send(normalize_command(cmd))
                    terminal_json.extend(collect_command_json(resp))
                terminal_json_all = terminal_json
            # 没有业务命令表示缺陷在模拟器初始页面即可观察，继续走统一截图验证。
        except Exception as exc:
            return {
                "verdict": "CANNOT_VERIFY",
                "error": f"命令执行异常: {exc}",
                "test_output": {"results": results, "evidence_issue": "命令执行异常"},
            }

        # 复用统一系统观察：等待 GUI processed 后再取得权威截图。
        from agent_loop_system.reproduction import observe

        verification = observe(
            session,
            step=0,
            seq=9000,
            evidence_dir=shot_dir / "verification",
        )
        shot_ok = verification.screenshot_ok
        if shot_ok and verification.screenshot_path:
            shutil.copy2(verification.screenshot_path, after_path)
    finally:
        session.stop()

    # 缺陷命令模式：对统一的最终截图做视觉判定。
    if defect_criteria and shot_ok:
        # before_path 存在时传作参考（对比判定）
        ref = before_path if Path(before_path).is_file() else None
        verdict = judge_with_vision(
            after_path,
            defect_criteria,
            reference_screenshot=ref,
            defect_image_paths=state.get("defect_image_paths", []),
        )
        results.append({
            "case_id": "agent_generated",
            "sheet": "agent",
            "verdict": verdict.verdict,
            "reason": verdict.reason,
            "test_commands": agent_cmds,
            "terminal_json": terminal_json_all,
        })
    elif defect_criteria and not shot_ok:
        results.append({
            "case_id": "agent_generated",
            "sheet": "agent",
            "verdict": "CANNOT_VERIFY",
            "reason": "after.bmp 截图失败，无法视觉判定",
            "test_commands": agent_cmds,
            "terminal_json": terminal_json_all,
        })

    evidence_issue = None if shot_ok else "after.bmp 截图失败"
    final_verdict = aggregate_verdicts([r["verdict"] for r in results])
    if not shot_ok:
        final_verdict = "CANNOT_VERIFY"
    return {
        "verdict": final_verdict,
        "test_output": {"results": results, "evidence_issue": evidence_issue},
    }


# --------------------------------------------------------------------------- #
# record
# --------------------------------------------------------------------------- #

def record(state: LoopState) -> dict:
    """回滚非 PASS 的 patch、必要时恢复构建产物、写入每轮 history。"""
    from agent_loop_system.tools.designer import rollback_designer_transaction

    verdict = state.get("verdict", "PENDING")
    patch_ever_applied = state.get("patch_applied", False)
    patched_artifact = state.get("patched_artifact", False)
    attempt_number = state.get("attempts", 0) + 1
    max_attempts = state.get("max_attempts", 5)

    patch_retained = False
    patch_applied = patch_ever_applied
    rolled_back = False
    rollback_error = state.get("rollback_error")
    restore_build_result = state.get("restore_build_result")
    restore_build_error = state.get("restore_build_error")

    if verdict == "PASS":
        patch_retained = True
    elif patch_ever_applied:
        transaction = state.get("designer_transaction") or {}
        manifest = transaction.get("transaction_manifest")
        try:
            if not manifest:
                raise ValueError("Designer transaction_manifest 缺失")
            rollback_designer_transaction(manifest)
            patch_applied = False
            rolled_back = True
        except Exception as exc:
            rollback_error = str(exc)
            patch_applied = True

    will_retry = (
        verdict == "FAIL"
        and not rollback_error
        and attempt_number < max_attempts
    )

    # 非 PASS 且不再重试且构建产物可能仍是 patch 版：恢复未修改源码对应的构建产物
    needs_restore = (
        patched_artifact
        and not rollback_error
        and verdict in ("FAIL", "CANNOT_VERIFY", "ERROR")
        and not will_retry
    )
    if needs_restore:
        try:
            config = BuildConfig.from_env()
        except KeyError as exc:
            restore_build_error = f"恢复构建配置失败: {exc}"
        else:
            result = run_build(config)
            restore_build_result = result.model_dump()
            if result.success:
                patched_artifact = False
            else:
                restore_build_error = result.error_message

    history = list(state.get("history", []))
    history.append({
        "attempt": attempt_number,
        "verdict": verdict,
        "patch": state.get("patch"),
        "patch_reason": state.get("patch_reason", ""),
        "repair_mode": state.get("repair_mode", "designer_vm"),
        "designer_plan": state.get("designer_plan"),
        "designer_context": state.get("designer_context"),
        "designer_transaction": state.get("designer_transaction"),
        "test_commands": state.get("agent_test_commands", []),
        "baseline_output": state.get("baseline_output"),
        "build_success": state.get("build_success"),
        "build_result": state.get("build_result"),
        "test_output": state.get("test_output"),
        "error": state.get("error"),
        "rolled_back": rolled_back,
        "rollback_error": rollback_error,
        "restore_build_result": restore_build_result,
        "restore_build_error": restore_build_error,
    })

    return {
        "history": history,
        "attempts": attempt_number,
        "patch_retained": patch_retained,
        "patch_applied": patch_applied,
        "rolled_back": rolled_back,
        "rollback_error": rollback_error,
        "restore_build_result": restore_build_result,
        "restore_build_error": restore_build_error,
        "patched_artifact": patched_artifact,
    }


def _should_retry(state: LoopState) -> str:
    if state.get("rollback_error") or state.get("restore_build_error"):
        return END
    if state.get("verdict") in ("PASS", "CANNOT_VERIFY", "ERROR"):
        return END
    if state.get("attempts", 0) >= state.get("max_attempts", 5):
        return END
    return "agent"


# --------------------------------------------------------------------------- #
# graph
# --------------------------------------------------------------------------- #

def build_graph():
    """编译并返回 LangGraph 图。"""
    g = StateGraph(LoopState)
    g.add_node("validate", tracked_node("validate", validate))
    g.add_node(
        "interactive_reproduce",
        tracked_node("interactive_reproduce", interactive_reproduce_node),
    )
    g.add_node("agent", tracked_node("agent", agent_node))
    g.add_node("apply", tracked_node("apply", apply_patch_node))
    g.add_node("build", tracked_node("build", build))
    g.add_node("test", tracked_node("test", test))
    g.add_node("record", tracked_node("record", record))

    g.add_edge(START, "validate")
    g.add_conditional_edges(
        "validate",
        _after_validate,
        {END: END, "interactive_reproduce": "interactive_reproduce"},
    )
    g.add_conditional_edges(
        "interactive_reproduce",
        _after_interactive_reproduce,
        {END: END, "agent": "agent"},
    )
    g.add_conditional_edges("agent", _after_agent, {"apply": "apply", "record": "record"})
    g.add_conditional_edges("apply", _after_apply, {"build": "build", "record": "record"})
    g.add_conditional_edges("build", _after_build, {"test": "test", "record": "record"})
    g.add_edge("test", "record")
    g.add_conditional_edges("record", _should_retry, {END: END, "agent": "agent"})
    return g.compile()
