"""扁平 loop state：LangGraph 图全节点共享的状态结构。

用 TypedDict（LangGraph 原生支持，节点返回 dict 增量合并），不引入 Pydantic reducer 复杂性。
嵌套契约（BuildResult/CaseRunResult 等）各自用 Pydantic，这里只存 model_dump() 后的 dict。
"""
from __future__ import annotations

from typing import TypedDict


class LoopState(TypedDict, total=False):
    """闭环状态。total=False 表示所有字段可选，由各节点逐步填充。"""

    # 输入（validate 前必须有）
    task_id: str
    objective: str  # bug 描述
    max_attempts: int  # 重试上限，默认 5
    target: str  # simulator / hardware；hardware 首版只做复现诊断

    # 交互复现（interactive_reproduce 节点填）
    reproduction_attempts: int
    reproduction_reason: str
    reproduction_outcome: str  # ReproductionOutcome 的字符串值
    reproduction_trace: dict | None  # ReproductionTrace.model_dump()
    agent_test_commands: list[str]

    # 修复 Agent 产出（仅 DEFECT_REPRODUCED 后填）
    patch: dict | None  # Patch.model_dump()，含 file_path/before/after/reason/test_commands
    patch_reason: str  # Agent 给出的修改理由
    source_files: list[str]  # 相对 W30_SOURCE_ROOT 的路径（CLI 传入）
    designer_enabled: bool  # 是否启用隔离 Designer；关闭时修复阶段 fail closed
    repair_mode: str  # designer_vm（唯一 UI 修复模式）
    designer_plan: dict | None  # DesignerPlan.model_dump()
    designer_context: dict | None  # 页面名和 Designer 选择/降级原因
    designer_transaction: dict | None  # 多文件事务清单和改动文件

    # 兼容下游修复与历史展示的复现结果
    baseline_output: dict | None  # {defect_reproduced, reason, test_commands, terminal_json, evidence_issue}
    baseline_ready: bool  # 缺陷可复现、允许调用修复 Agent

    # patch 应用状态（apply/record 节点填）
    patch_applied: bool  # 本轮 patch 是否已写入源码
    patch_offset: int | None  # 兼容旧输出；Designer 事务固定为 None
    patch_retained: bool  # 最终是否保留源码修改（仅 PASS 为 True）

    # 构建结果（build 节点填）
    build_success: bool
    build_result: dict | None  # BuildResult.model_dump()
    patched_artifact: bool  # 当前 main.exe 是否基于 patch 源码构建（跨轮保留，供结束前恢复产物判断）

    # 测试结果（test 节点填）
    test_output: dict | None  # {results, evidence_issue}
    verdict: str  # PENDING / PASS / FAIL / CANNOT_VERIFY / ERROR

    # 重试控制（record 节点维护）
    attempts: int  # 已尝试轮数
    history: list[dict]  # 每轮记录（patch/命令/结果/回滚状态）

    # 测试用例（CLI 传入，仅显式 --test-case 时回退使用）
    test_cases: list[dict]  # [{"sheet": "计算器", "case_id": "CALC_001"}]

    # 缺陷判定依据（--defect/--objective 模式填，判定器用缺陷描述而非用例 expected_text）
    judge_criteria: str
    defect_image_paths: list[str]  # 缺陷原图，视觉判定时作为预期/故障参考证据

    # 错误（任何节点可填）
    error: str | None
    error_code: str | None  # SOURCE_INPUT_INVALID / LLM_UNAVAILABLE / AGENT_OUTPUT_INVALID / INPUT_INVALID
    rollback_error: str | None  # 回滚失败时禁止重试
    restore_build_result: dict | None  # 恢复构建产物结果
    restore_build_error: str | None  # 恢复构建产物失败时记录
