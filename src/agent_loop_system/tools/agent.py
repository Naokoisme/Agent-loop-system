"""复现规划和修复生成。

先生成并验证复现命令；只有图状态确认缺陷可复现后，才生成单文件 patch。
"""
from __future__ import annotations

import base64
import json
import os
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

from agent_loop_system.tools.llm_retry import (
    LLMRetryError,
    get_llm_request_timeout,
    invoke_with_retry,
)
from agent_loop_system.tools.source_context import load_runtime_navigation_sources
from agent_loop_system.tools.designer import DesignerPlan

if TYPE_CHECKING:
    from agent_loop_system.reproduction import ReproductionDecision, ReproductionTrace

# 描述图片支持的扩展名 → MIME 映射
_IMG_EXTS = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}


class Patch(BaseModel):
    """单文件 patch + 测试命令。"""

    # 放第一个：利用 structured output 字段顺序，迫使 LLM 先推理根因再填 patch
    root_cause_analysis: str  # 先逐步推理根因（现象→推理→定位依据→修复方向）
    file_path: str  # 相对 W30_SOURCE_ROOT
    before: str  # 修改前文本（必须精确匹配源码中某段，且唯一）
    after: str  # 修改后文本
    reason: str  # 修改理由
    test_commands: list[str]  # hlq 测试命令序列，用于触发并验证 bug 条件


SIMULATOR_KB_DIR = Path(__file__).resolve().parents[3] / "sim_tools" / "kb"


def load_simulator_knowledge(kb_dir: Path = SIMULATOR_KB_DIR) -> str:
    """读取当前工作区的真实命令和窗口目录；显式传入目录时读取测试/离线文件。"""
    source_root_value = os.environ.get("W30_SOURCE_ROOT", "").strip()
    if kb_dir == SIMULATOR_KB_DIR and source_root_value:
        from sim_tools.extract_kb import extract_commands, extract_windows

        source_root = Path(source_root_value).resolve()
        project = os.environ.get("W30_PROJECT", "").strip()
        if not project:
            raise ValueError("W30_PROJECT 未配置，不能从真实源码生成模拟器能力目录")
        command_source = (
            source_root / "core" / "comm" / "srv" / "test" / "hlq_quick_cmd_handler.c"
        )
        project_cmake = source_root / "app" / "projects" / project / "Project.cmake"
        app_windows = source_root / "app" / "windows"
        app_root = source_root / "app" / "comm" / "TuoBu"
        app_quick_cmd = app_root / "quick_cmd" / "gui_comm_quick_cmd.c"
        commands = extract_commands(command_source, app_root=app_root).strip()
        windows = extract_windows(
            project_cmake=project_cmake,
            app_windows=app_windows,
            app_quick_cmd=app_quick_cmd,
        ).strip()
        if not commands or not windows:
            raise ValueError(f"真实源码能力目录为空: {source_root}")
        return f"## 命令与参数\n{commands}\n\n## 页面名\n{windows}"

    sections = []
    for title, name in (("命令与参数", "commands.txt"), ("页面名", "windows.txt")):
        path = kb_dir / name
        content = path.read_text(encoding="utf-8").strip()
        if not content:
            raise ValueError(f"模拟器知识库为空: {path}")
        sections.append(f"## {title}\n{content}")
    return "\n\n".join(sections)


def _create_llm():
    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key or api_key.startswith("暂时"):
        return None
    try:
        from langchain_openai import ChatOpenAI
    except ImportError:
        return None
    return ChatOpenAI(
        model=os.environ.get("OPENAI_MODEL", "gpt-4"),
        api_key=api_key,
        base_url=os.environ.get("OPENAI_BASE_URL") or None,
        timeout=get_llm_request_timeout(),
    )


def _load_images(image_dir: str | None) -> list[tuple[str, str]]:
    images: list[tuple[str, str]] = []
    if not image_dir:
        return images
    directory = Path(image_dir)
    if not directory.is_dir():
        return images
    for path in sorted(directory.iterdir()):
        mime = _IMG_EXTS.get(path.suffix.lower())
        if not mime:
            continue
        try:
            images.append((base64.b64encode(path.read_bytes()).decode("ascii"), mime))
        except OSError:
            continue
    return images


def _invoke_structured_with_images(
    llm,
    schema,
    prompt: str,
    images: list[tuple[str, str, str]],
    image_note: str = "",
    method: str | None = None,
):
    """统一发送带标签图片的 structured output 请求。"""
    def structured():
        if method:
            return llm.with_structured_output(schema, method=method)
        return llm.with_structured_output(schema)

    if not images:
        return invoke_with_retry(lambda: structured().invoke(prompt))

    from langchain_core.messages import HumanMessage

    text = prompt + ("\n\n" + image_note if image_note else "")
    content = [{"type": "text", "text": text}]
    for label, encoded, mime in images:
        if label:
            content.append({"type": "text", "text": label})
        content.append(
            {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{encoded}"}}
        )
    return invoke_with_retry(
        lambda: structured().invoke([HumanMessage(content=content)])
    )


def _invoke_structured(
    llm,
    schema,
    prompt: str,
    image_dir: str | None,
    image_note: str,
    *,
    method: str | None = None,
):
    images = _load_images(image_dir)
    return _invoke_structured_with_images(
        llm,
        schema,
        prompt,
        [("", encoded, mime) for encoded, mime in images],
        image_note,
        method,
    )


def decide_reproduction_action(
    objective: str,
    source_files: list[dict],
    trace: "ReproductionTrace",
    defect_image_paths: list[str] | None = None,
    *,
    execution_target: str = "simulator",
    capability_knowledge: str | None = None,
    navigation_source_root: str | None = None,
) -> "ReproductionDecision | None":
    """根据当前观察只决定下一步，不生成整套命令。"""
    from agent_loop_system.reproduction import ReproductionDecision
    from agent_loop_system.tools.test import VISUAL_RELEVANCE_RULES, _bmp_to_png_b64

    llm = _create_llm()
    if llm is None:
        return None

    files_text = "\n\n".join(
        f"=== 文件: {item['path']} ===\n{item['content']}" for item in source_files
    )
    observations = []
    for observation in trace.steps:
        observations.append(
            {
                "step": observation.step,
                "decision": (
                    observation.decision.model_dump(mode="json")
                    if observation.decision
                    else None
                ),
                "command_status": observation.command_status,
                "command_results": observation.command_results[-5:],
                "screenshot_ok": observation.screenshot_ok,
                "window_id": observation.window_id,
                "window_name": observation.window_name,
                "popup_id": observation.popup_id,
                "popup_name": observation.popup_name,
                "capture_metadata": observation.capture_metadata,
                "tree_status": observation.tree_status.value,
                "visible_texts": observation.visible_texts,
                "error": observation.error,
            }
        )

    target_window = ""
    for observation in reversed(trace.steps):
        decision = observation.decision
        command = decision.command if decision else None
        if not command or not command.startswith(":ENTER_PAGE:"):
            continue
        target_window = command[len(":ENTER_PAGE:") :].partition(",")[0].strip()
        if target_window:
            break
    current = trace.steps[-1] if trace.steps else None
    actual_window = ""
    if current:
        actual_window = current.popup_name or current.window_name or ""
    navigation_kwargs: dict[str, Any] = {
        "existing_paths": [item["path"] for item in source_files]
    }
    if navigation_source_root is not None:
        navigation_kwargs["source_root"] = navigation_source_root
    navigation_sources = load_runtime_navigation_sources(
        target_window,
        actual_window,
        **navigation_kwargs,
    )
    navigation_text = "\n\n".join(
        f"=== 运行时入口文件: {item['path']} ===\n{item['content']}"
        for item in navigation_sources
    )

    target_label = "6202 真机" if execution_target == "hardware" else "模拟器"
    target_knowledge = capability_knowledge or load_simulator_knowledge()
    target_rule = (
        "7. 当前是 6202 真机：不得选择任何 SIM_* 命令，也不得选择清空、恢复出厂、"
        "关机、重启或批量删除类命令。\n"
        if execution_target == "hardware"
        else "7. Windows 模拟器不得选择能力目录中注明仅供非 Windows 真机兼容的旧命令，"
        "必须使用目录给出的模拟器替代命令。\n"
    )
    prompt = (
        "你是嵌入式手表缺陷复现 Agent。你要根据当前截图和历史观察，只决定下一步一个动作，"
        "不能一次规划整套命令，也不能修改源码。\n\n"
        f"缺陷描述：\n{objective}\n\n"
        f"缺陷预定位源码（完整文件，仅供理解缺陷）：\n{files_text}\n\n"
        "运行时页面入口源码（系统根据目标窗口与实际落点自动补充，用于判断重定向和前置状态）：\n"
        f"{navigation_text or '本轮没有发生需要补充源码的页面重定向'}\n\n"
        f"{target_label}能力目录（命令和窗口只能从这里选择，禁止编造）：\n"
        f"{target_knowledge}\n\n"
        "历史观察：\n"
        f"{json.dumps(observations, ensure_ascii=False, indent=2)}\n\n"
        "动作规则：\n"
        "1. EXECUTE：只执行一条推进真实产品状态的业务命令，command 必须为 :CMD:args。\n"
        "2. OBSERVE_AGAIN：界面正在转场或截图时机不稳定时再观察一次，不得携带 command。\n"
        "3. READY_TO_JUDGE：当前截图已清楚显示缺陷相关目标内容，可以交给独立视觉判定。\n"
        "4. BLOCKED：真实能力目录明确缺少到达目标所需能力时停止；不要因为暂时没找到入口就阻塞。\n"
        "5. GUI_PING、GUI_TREE、GUI_STATE、SCREENSHOT_PRINT 由系统自动执行，禁止放进 command。\n"
        "6. 优先使用注册窗口的 ENTER_PAGE；目标窗口本身能展示文案、排版或图片时，不得额外读取"
        " BUSINESS_GET 或注入无关业务数据。\n"
        f"{target_rule}"
        "8. reason 用一句话说明为什么选择这个动作。\n\n"
        "视觉相关性规则：\n"
        f"{VISUAL_RELEVANCE_RULES}"
    )

    labeled_images: list[tuple[str, str, str]] = []
    for index, path in enumerate(defect_image_paths or [], start=1):
        encoded = _bmp_to_png_b64(path)
        if encoded:
            labeled_images.append((f"缺陷原图 {index}：", encoded, "image/png"))
    if trace.steps:
        current = trace.steps[-1]
        if current.screenshot_ok and current.screenshot_path:
            encoded = _bmp_to_png_b64(current.screenshot_path)
            if encoded:
                labeled_images.append(
                    (f"当前{target_label}截图（step {current.step}）：", encoded, "image/png")
                )

    return _invoke_structured_with_images(
        llm,
        ReproductionDecision,
        prompt,
        labeled_images,
    )


def generate_patch(
    objective: str,
    source_files: list[dict],  # [{"path": "...", "content": "..."}]
    last_test_output: dict | None = None,
    image_dir: str | None = None,
    verified_test_commands: list[str] | None = None,
) -> Patch:
    """缺陷可靠复现后，LLM 生成 patch。

    配置错误（API key 缺失/依赖未安装/初始化失败）返回 None；
    调用错误在 12 次重试耗尽后抛 LLMRetryError，由调用方映射 CANNOT_VERIFY。
    """
    llm = _create_llm()
    if llm is None:
        return None

    files_text = "\n\n".join(
        f"=== 文件: {f['path']} ===\n{f['content']}" for f in source_files
    )
    last_test = ""
    if last_test_output:
        last_test = f"\n\n复现结果或上次修复测试结果（供参考）：\n{last_test_output}"
    verified_commands = list(verified_test_commands or [])
    prompt = (
        "你是嵌入式固件 bug 修复 Agent。缺陷已经可靠复现；根据描述、复现结果和源码生成最小修复 patch。\n\n"
        f"bug 描述：\n{objective}\n\n"
        f"相关源码（注意：源码中的位置仅供参考，可能是错误定位，"
        f"你需要通读源码自主判断真正的 bug 位置）：\n{files_text}{last_test}\n\n"
        f"已经验证能复现缺陷的测试命令：\n{verified_commands}\n\n"
        "要求（逐条必须满足）：\n"
        "0. root_cause_analysis 必须先逐步推理（此字段最关键，必须先完成再填其他字段）：\n"
        "   - bug 现象：从描述+附件分析中提取实际可见的现象（不臆测，区分\"翻译问题\"与\"单位换算\"等不同类型）\n"
        "   - 根因推理：结合源码分析为什么会出这个现象，排除无关因素\n"
        "   - 定位依据：明确指出 bug 在源码中的确切位置及判断依据\n"
        "   - 修复方向：基于根因给出修复思路，再据此填写后续 patch 字段\n"
        "1. 通读源码，定位 bug 根因（不要被 bug 描述中的代码片段位置误导，自主判断）\n"
        "2. 给出最小修改（只改必要部分，不要重构周边代码）\n"
        "3. before 必须是源码中存在的精确文本（能直接定位到修改位置，且在文件中唯一出现）\n"
        "4. after 是修改后的文本\n"
        "5. file_path 必须非空，且必须从上面“=== 文件: ... ===”给出的路径中【原样复制一行】"
        "（相对源码根的路径），禁止留空、禁止改写、禁止拼接、禁止编造\n"
        "6. reason 说明为什么这样改\n"
        "7. test_commands 必须原样复制上面已经验证成功的测试命令，不得重新设计复现步骤。\n"
    )
    return _invoke_structured(
        llm,
        Patch,
        prompt,
        image_dir,
        "[附图] 以下是缺陷原图，供修复时核对具体可见现象。",
    )


def generate_designer_plan(
    objective: str,
    page_name: str,
    page_context: dict[str, Any],
    native_tool_schemas: dict[str, dict[str, Any]],
    capability_groups: dict[str, dict[str, Any]],
    reference_data: dict[str, Any],
    vm_user_sections: dict[str, list[dict[str, str]]],
    source_files: list[dict],
    last_test_output: dict | None = None,
    image_dir: str | None = None,
    verified_test_commands: list[str] | None = None,
) -> DesignerPlan | None:
    """生成强制先 Designer、后可选 VM USER 补丁的单一修复计划。"""
    llm = _create_llm()
    if llm is None:
        return None
    repair_summary = dict(last_test_output or {})
    trace = repair_summary.get("reproduction_trace")
    if isinstance(trace, dict):
        repair_summary["reproduction_trace"] = {
            "outcome": trace.get("outcome"),
            "reason": trace.get("reason"),
            "steps": [
                {
                    "step": step.get("step"),
                    "decision": step.get("decision"),
                    "command_status": step.get("command_status"),
                    "window_name": step.get("window_name"),
                    "popup_name": step.get("popup_name"),
                    "verified_observations": step.get("verified_observations"),
                }
                for step in (trace.get("steps") or [])
            ],
        }
    page_text = json.dumps(page_context, ensure_ascii=False)
    analysis_terms_text = page_text + json.dumps(vm_user_sections, ensure_ascii=False)
    terms = sorted({
        token.casefold()
        for token in re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", analysis_terms_text)
        if "_" in token or token.islower()
    }, key=lambda item: (-len(item), item))[:128]
    compact_native_schemas = {
        name: {
            "required": [item for item in schema.get("required", []) if item != "sessionId"],
            "properties": {
                key: {
                    field: value
                    for field, value in definition.items()
                    if field in {"type", "enum", "description"}
                }
                for key, definition in (schema.get("properties") or {}).items()
                if key != "sessionId"
            },
        }
        for name, schema in native_tool_schemas.items()
    }
    source_excerpts = []
    remaining_source_chars = 60_000
    for item in source_files:
        if remaining_source_chars <= 0:
            break
        content = str(item.get("content") or "")
        lines = content.splitlines()
        selected: set[int] = set(range(min(24, len(lines))))
        for index, line in enumerate(lines):
            folded = line.casefold()
            if any(term in folded for term in terms):
                selected.update(range(max(0, index - 5), min(len(lines), index + 6)))
        excerpt = "\n".join(f"{index + 1}: {lines[index]}" for index in sorted(selected))
        excerpt = excerpt[:min(12_000, remaining_source_chars)]
        remaining_source_chars -= len(excerpt)
        source_excerpts.append({"path": str(item.get("path") or ""), "excerpt": excerpt})
    prompt = (
        "你是 LVGL GUI Designer 修复 Agent。当前缺陷已经由模拟器可靠复现。"
        "这个页面属于 Designer 工程，不存在 Designer 与源码二选一：必须先完成 Designer 声明并重新生成，"
        "然后才可以选择性补充 VM USER 区代码。\n\n"
        f"缺陷描述：\n{objective}\n\n"
        f"实际缺陷页面：{page_name}\n\n"
        "Designer 页面结构与绑定快照（控件 ID、类型、几何信息和绑定来自当前真实工程）：\n"
        f"{page_text}\n\n"
        "Designer 原生写工具真实 schema（native_calls 只能从这里选，参数不含 sessionId）：\n"
        f"{json.dumps(compact_native_schemas, ensure_ascii=False)}\n\n"
        "Designer 适配能力状态（ready=false 表示当前 Adapter 尚未组合出安全能力，"
        "不得要求用户修改 Designer）：\n"
        f"{json.dumps(capability_groups, ensure_ascii=False)}\n\n"
        "Designer 只读参考数据（只来自当前 Designer session）：\n"
        f"{json.dumps(reference_data, ensure_ascii=False)[:40000]}\n\n"
        "当前页面生成的 VM USER 区（源码 Agent 唯一允许写入的范围）：\n"
        f"{json.dumps(vm_user_sections, ensure_ascii=False)}\n\n"
        "相关业务源码摘录（只用于定位根因，禁止把这些普通源码当成 UI 修补目标）：\n"
        f"{json.dumps(source_excerpts, ensure_ascii=False)}\n\n"
        f"最近一次修复/复现结果：\n{json.dumps(repair_summary, ensure_ascii=False)}\n\n"
        "已经验证的复现命令（修复后必须原样复用）：\n"
        f"{json.dumps(verified_test_commands or [], ensure_ascii=False)}\n\n"
        "规则：\n"
        "1. page_name 必须原样填写实际缺陷页面。\n"
        "2. layout_operations 负责页面 JSON、字体/尺寸/位置/样式、控件层级和普通属性，"
        "仅允许 add/set/move/resize/delete/reparent/reorder/clone/page_set。\n"
        "3. native_calls 负责属性绑定、点击事件/ActionList、CommonModule、翻译、图片与资源；"
        "tool_name 和 arguments 必须严格符合给出的真实 schema，不得传 sessionId。\n"
        "4. VM 非 USER 区和全部 V 层都由 Designer 重新生成，禁止输出针对这些区域的手工源码修改。\n"
        "5. vm_user_patch 只能在 Designer 生成之后执行，只能选上面列出的当前页面 VM 文件和 user_section；"
        "它只实现 Designer 已声明但模板无法完整表达的自定义处理逻辑。没有必要时填 null。"
        "如果 USER 区由本计划某个 binding native_call 新建，declared_by_native_call 填该调用的零基索引；"
        "现有 USER 区则填 null。\n"
        "6. 如果 Designer 声明已经正确而只有现有 USER 实现有 bug，可以不改声明，但必须在 root_cause_analysis 中"
        "指出对应 binding/event 与 user_section，且只输出 vm_user_patch；执行器仍会先重新生成。\n"
        "7. before 必须是 USER 区中完整、精确且全文件唯一的原文；after 不得含 USER_BEGIN/USER_END。\n"
        "8. 控件 ID、binding ID、属性路径、服务名和现有值只能来自快照，禁止编造。采用最小修改，"
        "不得把动态绑定改成固定图片或固定文字来伪造截图。\n"
        "9. 必须沿 数据源 -> CommonModule/属性 -> VM 处理 -> 控件属性 逐段核对因果。"
        "当画面显示的是另一个数据项（例如另一个应用的图标/名称）时，优先定位上游数据身份或索引；"
        "除非已有证据证明数据身份正确，否则禁止只替换 icon/thumbnail、图片路径、映射表或样式。\n"
        "10. root_cause_analysis 必须列出支持结论的截图现象、Designer binding 和源码符号，"
        "同时写明至少一个被排除的相邻假设；不得把猜测写成已验证事实。\n"
        "11. 如果完成根因修复必然依赖某个 ready=false 的高级能力组，禁止用其他 UI 修改冒充修复；"
        "此时 blocked_capability 填能力组原名，layout_operations/native_calls 置空、vm_user_patch 填 null。"
        "其他情况下 blocked_capability 必须填 null。\n"
    )
    return _invoke_structured(
        llm,
        DesignerPlan,
        prompt,
        image_dir,
        "[附图] 以下是缺陷原图，只用于确认目标视觉效果。",
        method="function_calling",
    )
