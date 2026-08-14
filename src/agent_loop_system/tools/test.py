"""测试工具：加载用例 → 启动模拟器 → 执行命令 → 截图 → LLM 视觉判定。

判定方式：LLM 只对照 Excel「预期结果」与检查点截图判定，GUI_TREE 不参与判决。
证据落盘：截图、终端 JSON 和判定理由写到 evidence/{sheet}_{case_id}.json，供人工查看。
网页端可通过可选参数指定独立结果文件和截图路径。

用法：
    uv run python -m agent_loop_system.tools.test --sheet 计算器 --case-id CALC_001
"""
from __future__ import annotations

import argparse
import base64
import io
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from agent_loop_system.tools.case_map import (
    CASE_MAP_PROFILE_DIRS,
    CaseRunResult,
    load_case_map,
    run_case,
)
from agent_loop_system.tools.llm_retry import (
    LLMRetryError,
    get_llm_request_timeout,
    invoke_with_retry,
)
from agent_loop_system.tools.simulator import SimulatorSession

DEFAULT_SIM_EXE = r"D:\TOPSTEP\shenju_w30\core\gui\simulator\bin\main.exe"
EVIDENCE_DIR = Path(r"d:\Agent-loop-system\evidence")
VISUAL_RELEVANCE_RULES = (
    "- 只比较缺陷标题、描述和验收条件明确涉及的界面属性，不得从参考图中扩展出新的故障点。\n"
    "- 复合需求图中，明确标注为“说明文案”“预期结果”“需求描述”等规格文字的内容定义预期；"
    "未标注为预期的模拟器截图或实物照片只是现象示例，不得反过来覆盖明确规格。\n"
    "- 状态栏当前时间、日期、电量、信号等运行时动态状态通常会自然变化；"
    "除非缺陷描述明确涉及该状态，否则它与参考图不同是正常现象，不得据此判定 FAIL。\n"
    "- 系统会提供模拟器截图的真实采集时间；界面中的时分若与采集时间吻合，"
    "应识别为设备当前时钟，而不是业务记录时间。\n"
    "- 例如缺陷只涉及文案时，应核对相关文案及其排版，不得把无关的当前时间差异当成文案缺陷。\n"
)


def _configure_console_output() -> None:
    """Keep Windows CLI output from failing on Unicode verdict text."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            pass


def _screenshot_capture_note(path: str) -> str:
    """给视觉模型补充截图采集事实，帮助它识别状态栏当前时间。"""
    try:
        captured_at = datetime.fromtimestamp(Path(path).stat().st_mtime).astimezone()
    except OSError:
        return "系统未能读取这张截图的采集时间。"
    return (
        f"系统记录的截图采集时间：{captured_at.strftime('%Y-%m-%d %H:%M:%S %z')}。"
        "若界面右上角时分与该时间吻合，它通常是设备当前时钟。"
    )


def get_simulator_exe() -> str:
    """每次启动时从环境读取模拟器，避免服务进程长期持有旧工作区路径。"""
    return os.environ.get("SIMULATOR_ARTIFACT_PATH", DEFAULT_SIM_EXE)


class Verdict(BaseModel):
    """LLM 判定结果。"""

    verdict: Literal["PASS", "FAIL", "CANNOT_VERIFY"]
    reason: str


class CaseDecision(BaseModel):
    """Runner 最终判定；ERROR/SKIP 由证据门禁产生，不交给视觉模型猜。"""

    verdict: Literal["PASS", "FAIL", "CANNOT_VERIFY", "ERROR", "SKIP"]
    reason: str


def aggregate_verdicts(verdicts: list[str]) -> str:
    """聚合多个判定：执行/证据错误优先，其次产品失败和无法验证。"""
    if not verdicts:
        return "CANNOT_VERIFY"
    if "ERROR" in verdicts:
        return "ERROR"
    if "FAIL" in verdicts:
        return "FAIL"
    if "CANNOT_VERIFY" in verdicts:
        return "CANNOT_VERIFY"
    if all(v == "PASS" for v in verdicts):
        return "PASS"
    return "CANNOT_VERIFY"


def judge_with_llm(
    expected_text: str,
    terminal_json: list[dict],
    defect_criteria: str = "",
) -> Verdict:
    """兼容旧调用；终端 JSON 判定已禁用，防止 GUI_TREE 再进入 LLM。"""
    _ = expected_text, terminal_json, defect_criteria
    return Verdict(
        verdict="CANNOT_VERIFY",
        reason="终端 JSON 判定已禁用；界面结果必须使用模拟器截图判定",
    )


def _bmp_to_png_b64(path: str) -> str | None:
    """读取本地图片并转成 PNG base64（兼容 BMP/PNG/JPEG/WebP）。"""
    try:
        from PIL import Image
    except ImportError:
        return base64.b64encode(Path(path).read_bytes()).decode("ascii")
    try:
        img = Image.open(path)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return base64.b64encode(buf.getvalue()).decode("ascii")
    except Exception:
        return None


def judge_with_vision(
    screenshot_path: str,
    defect_criteria: str,
    reference_screenshot: str | None = None,
    defect_image_paths: list[str] | None = None,
    verified_observations: list[str] | None = None,
) -> Verdict:
    """LLM 视觉判定：看截图判断缺陷现象是否仍存在。

    defect_image_paths 为缺陷原图/规格参考图；
    reference_screenshot 非空时为修复前截图，用于对比判定修复效果。
    """
    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key or api_key.startswith("暂时"):
        return Verdict(verdict="CANNOT_VERIFY", reason="API key 不可用")
    try:
        from langchain_core.messages import HumanMessage
        from langchain_openai import ChatOpenAI
    except ImportError:
        return Verdict(verdict="CANNOT_VERIFY", reason="langchain 未安装")

    shot_b64 = _bmp_to_png_b64(screenshot_path)
    if not shot_b64:
        return Verdict(verdict="CANNOT_VERIFY", reason=f"截图读取失败: {screenshot_path}")

    try:
        llm = ChatOpenAI(
            model=os.environ.get("OPENAI_MODEL", "gpt-4"),
            api_key=api_key,
            base_url=os.environ.get("OPENAI_BASE_URL") or None,
            timeout=get_llm_request_timeout(),
        )
    except Exception as exc:
        return Verdict(verdict="CANNOT_VERIFY", reason=f"LLM 初始化失败: {exc}")

    has_ref = False
    ref_b64 = ""
    if reference_screenshot:
        ref_b64 = _bmp_to_png_b64(reference_screenshot) or ""
        has_ref = bool(ref_b64)

    defect_images: list[tuple[str, str]] = []
    for path in defect_image_paths or []:
        encoded = _bmp_to_png_b64(path)
        if encoded:
            defect_images.append((Path(path).name, encoded))

    evidence_rules = (
        "判定依据：\n"
        f"{VISUAL_RELEVANCE_RULES}"
        "- 缺陷原图是直接证据；附件文字摘要只作提示，与原图冲突时以原图为准。\n"
        "- 原图是复合图时，先根据标题、标注和版面区分规格/预期 UI 与故障截图，逐项核对可见文案和布局。\n"
        "- 如果目标窗口及缺陷相关内容已经清楚可见，不得因为缺少无关业务数据、其他页面或隐藏属性而返回 CANNOT_VERIFY。\n"
        "- CANNOT_VERIFY 只用于目标内容不可见，或原始证据本身确实无法确定预期与故障差异。\n"
        "- 理由必须明确写出从原图识别到的预期、截图中的实际表现和二者差异。\n"
    )
    observation_context = ""
    facts = [str(item).strip() for item in verified_observations or [] if str(item).strip()]
    if facts:
        observation_context = (
            "已验证的复现事实（由程序根据命令结果和实际窗口记录）：\n"
            + "\n".join(f"- {item}" for item in facts)
            + "\n这些事实只用于确认当前截图中对象或状态的来源；"
            "缺陷是否存在仍必须以截图和缺陷原始证据为准，不得把操作成功直接当成缺陷复现。\n\n"
        )

    if has_ref:
        text = (
            "你是嵌入式手表缺陷修复验证器。对照缺陷原图和缺陷描述，比较修复前、修复后截图，"
            "判断缺陷现象是否已修复。\n\n"
            f"缺陷描述：\n{defect_criteria}\n\n"
            f"{observation_context}"
            f"{evidence_rules}\n"
            "判定规则：\n"
            "- PASS：修复后截图中缺陷现象消失\n"
            "- FAIL：修复后截图中缺陷现象仍存在\n"
            "- CANNOT_VERIFY：按上述依据仍不能比较\n"
        )
        content = [{"type": "text", "text": text}]
        for index, (name, encoded) in enumerate(defect_images, start=1):
            content.extend([
                {"type": "text", "text": f"缺陷原图 {index}（{name}）："},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{encoded}"}},
            ])
        content.extend([
            {"type": "text", "text": "修复前模拟器截图："},
            {"type": "text", "text": _screenshot_capture_note(reference_screenshot)},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{ref_b64}"}},
            {"type": "text", "text": "修复后模拟器截图："},
            {"type": "text", "text": _screenshot_capture_note(screenshot_path)},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{shot_b64}"}},
        ])
    else:
        text = (
            "你是嵌入式手表缺陷复现验证器。对照缺陷原图和缺陷描述，看当前模拟器截图判断缺陷现象是否存在。\n\n"
            f"缺陷描述：\n{defect_criteria}\n\n"
            f"{observation_context}"
            f"{evidence_rules}\n"
            "判定规则：\n"
            "- FAIL：当前截图复现了原图所示缺陷\n"
            "- PASS：当前截图清楚显示目标内容，且符合原图中的预期、没有复现缺陷\n"
            "- CANNOT_VERIFY：按上述依据仍不能比较\n"
        )
        content = [{"type": "text", "text": text}]
        for index, (name, encoded) in enumerate(defect_images, start=1):
            content.extend([
                {"type": "text", "text": f"缺陷原图 {index}（{name}）："},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{encoded}"}},
            ])
        content.extend([
            {"type": "text", "text": "当前模拟器截图："},
            {"type": "text", "text": _screenshot_capture_note(screenshot_path)},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{shot_b64}"}},
        ])

    msg = HumanMessage(content=content)
    try:
        return invoke_with_retry(
            lambda: llm.with_structured_output(Verdict).invoke([msg])
        )
    except LLMRetryError as exc:
        return Verdict(verdict="CANNOT_VERIFY", reason=f"LLM 重试耗尽: {exc}")


def judge_test_with_vision(
    expected_text: str,
    screenshots: list[dict[str, object]],
    verification_points: list[str] | None = None,
) -> Verdict:
    """只根据检查点截图判定普通测试；命令输出和 GUI_TREE 不进入 LLM。"""
    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key or api_key.startswith("暂时"):
        return Verdict(verdict="CANNOT_VERIFY", reason="API key 不可用")
    try:
        from langchain_core.messages import HumanMessage
        from langchain_openai import ChatOpenAI
    except ImportError:
        return Verdict(verdict="CANNOT_VERIFY", reason="langchain 未安装")

    points = [str(point).strip() for point in verification_points or [] if str(point).strip()]
    if not screenshots:
        return Verdict(verdict="CANNOT_VERIFY", reason="没有采集到判定截图")
    if points and len(screenshots) < len(points):
        return Verdict(
            verdict="CANNOT_VERIFY",
            reason=f"验证点需要 {len(points)} 张截图，实际只有 {len(screenshots)} 张",
        )

    encoded_screenshots: list[tuple[int, str, str, str]] = []
    for index, item in enumerate(screenshots, start=1):
        path = str(item.get("path") or "")
        encoded = _bmp_to_png_b64(path) if path else None
        if not encoded:
            return Verdict(verdict="CANNOT_VERIFY", reason=f"第 {index} 张判定截图读取失败")
        label = points[index - 1] if index <= len(points) else str(item.get("label") or "最终画面")
        encoded_screenshots.append((index, label, path, encoded))

    try:
        llm = ChatOpenAI(
            model=os.environ.get("OPENAI_MODEL", "gpt-4"),
            api_key=api_key,
            base_url=os.environ.get("OPENAI_BASE_URL") or None,
            timeout=get_llm_request_timeout(),
        )
    except Exception as exc:
        return Verdict(verdict="CANNOT_VERIFY", reason=f"LLM 初始化失败: {exc}")

    point_text = "\n".join(
        f"{index}. {point}" for index, point in enumerate(points, start=1)
    ) or "未单列验证点；使用最终截图核对完整预期结果。"
    prompt = (
        "你是嵌入式手表自动测试的视觉判定器。产品 PASS/FAIL 的唯一证据是下方模拟器截图。\n\n"
        f"预期结果：\n{expected_text}\n\n"
        f"按顺序对应的验证点：\n{point_text}\n\n"
        "判定约束：\n"
        "- 只能依据截图中肉眼可见的界面内容判定，不得假设或索要 GUI_TREE、控件属性、页面名、终端 JSON 或命令结果。\n"
        "- 验证点与截图按序一一对应；有多个验证点时，必须逐张核对。\n"
        "- PASS：对应截图清楚显示全部预期结果。\n"
        "- FAIL：对应截图清楚显示与至少一项预期结果直接矛盾的界面。\n"
        "- CANNOT_VERIFY：截图缺少目标状态、内容被遮挡/裁切/无法辨认，或缺少必要的前后对比截图。\n"
        "- 每条用例必须独立看当前截图，不得把相邻用例或先前截图的页面内容套用到当前图。\n"
        "- 若截图大面积黑屏、空白或只有单个数字/图标，理由必须如实描述这些内容，不得臆造图中不存在的列表、文字或按钮。\n"
        "- 对图标、按钮等非文字界面元素，应按其清楚可见的形状、颜色和位置核对；预期使用功能名称描述控件时，不得仅因截图没有显示同名文字而判 FAIL。\n"
        "- 对全屏颜色、图标居中等视觉稀疏页面，若预期只要求页面保持，前后截图中稳定一致的全屏颜色、中心图标和布局就是可见页面身份与保持证据，不得仅因没有页面标题而判 CANNOT_VERIFY。\n"
        "- 缺少不可见的内部字段不能判 FAIL；命令执行成功也不能直接判 PASS。\n"
        "- 对测量完成状态，截图中出现有效数值和明确完成时间（例如 Just now）可作为已经产出结果的可见证据；不得把旁边的再次测量按钮或操作提示误读为仍在测量。\n"
        "- 理由必须写明第几张截图、肉眼看到什么，以及它与哪条预期一致、矛盾或不足。\n"
        f"{VISUAL_RELEVANCE_RULES}"
    )
    content: list[dict[str, object]] = [{"type": "text", "text": prompt}]
    for index, label, path, encoded in encoded_screenshots:
        content.extend([
            {"type": "text", "text": f"判定截图 {index}，对应验证点：{label}"},
            {"type": "text", "text": _screenshot_capture_note(path)},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{encoded}"}},
        ])

    try:
        message = HumanMessage(content=content)
        return invoke_with_retry(
            lambda: llm.with_structured_output(Verdict).invoke([message])
        )
    except LLMRetryError as exc:
        return Verdict(verdict="CANNOT_VERIFY", reason=f"LLM 重试耗尽: {exc}")


def judge_case_result(result: CaseRunResult) -> CaseDecision:
    """先执行证据门禁，再把完整截图交给视觉模型判产品结果。"""

    if result.skipped:
        return CaseDecision(verdict="SKIP", reason="用例标记为 unable，未执行")

    contract = result.evidence_contract if isinstance(result.evidence_contract, dict) else {}
    if contract.get("complete") is not True:
        issues = contract.get("issues", [])
        messages = [
            str(item.get("message") or "").strip()
            for item in issues
            if isinstance(item, dict) and str(item.get("message") or "").strip()
        ]
        reason = messages[0] if messages else "证据合同不完整，禁止进入产品 PASS 判定"
        return CaseDecision(verdict="ERROR", reason=reason)

    visual = judge_test_with_vision(
        result.expected_text,
        result.screenshots,
        result.verification_points,
    )
    return CaseDecision(verdict=visual.verdict, reason=visual.reason)


def save_evidence(
    result: CaseRunResult,
    verdict: CaseDecision | Verdict | None,
    output_path: str | Path | None = None,
) -> Path:
    """落盘截图判定，并把执行异常作为独立诊断信息保存。"""
    path = Path(output_path) if output_path else EVIDENCE_DIR / f"{result.sheet}_{result.case_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    contract_issues = (
        result.evidence_contract.get("issues", [])
        if isinstance(result.evidence_contract, dict)
        else []
    )
    evidence_error = next((
        str(item.get("message") or "").strip()
        for item in contract_issues
        if isinstance(item, dict) and str(item.get("message") or "").strip()
    ), "")
    if result.skipped:
        result_verdict = "SKIP"
        reason = "用例标记为 unable，未执行"
    elif result.aborted or result.setup_errors or result.action_errors or evidence_error:
        result_verdict = "ERROR"
        reason = (
            (result.setup_errors + result.action_errors)[0]
            if result.setup_errors or result.action_errors
            else evidence_error or "准备或操作阶段未完整执行"
        )
    else:
        result_verdict = verdict.verdict if verdict else "CANNOT_VERIFY"
        reason = verdict.reason if verdict else "LLM 不可用，需人工判定"
    execution_errors = (
        result.setup_errors + result.action_errors + result.collect_errors
    )
    payload = {
        "schema_version": 2,
        "case_id": result.case_id,
        "sheet": result.sheet,
        "precondition_text": result.precondition_text,
        "steps_text": result.steps_text,
        "expected_text": result.expected_text,
        "verification_points": result.verification_points,
        "planned_commands": result.planned_commands,
        "command_trace": result.command_trace,
        "evidence_contract": result.evidence_contract,
        "skipped": result.skipped,
        "aborted": result.aborted,
        "setup_errors": result.setup_errors,
        "action_errors": result.action_errors,
        "collect_errors": result.collect_errors,
        "execution_status": "ERROR" if execution_errors or evidence_error else "OK",
        "execution_reason": execution_errors[0] if execution_errors else evidence_error,
        "terminal_json": result.terminal_json,
        "screenshots": result.screenshots,
        "verdict": result_verdict,
        "reason": reason,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def initialize_hardware_case_session(session) -> None:
    """在已启动的真机会话中设置语言并进入表盘，不管理测试租约。"""

    for index, command in enumerate((":LANGUAGE_SET:1", ":ENTER_PAGE:DIAL,0")):
        result = session.send(command)
        if str(result.status).lower() != "accepted":
            raise RuntimeError(f"真机初始化命令失败: {command} -> {result.status}")
        sequence = (time.time_ns() + index) % 2_147_483_647 or 1
        barrier = session.send(
            f":GUI_PING:{sequence}",
            request="gui_ping",
            expected_type="gui_ack",
            expected_status="processed",
        )
        if str(barrier.status).lower() != "processed":
            raise RuntimeError(f"真机初始化未完成: {command} -> {barrier.status}")


def run_single_case(
    sheet_name: str,
    case_id: str,
    screenshot_path: str | None = None,
    *,
    target: str = "simulator",
    case_map_profile: str | None = None,
) -> CaseRunResult:
    """加载并在指定目标执行单条用例。

    screenshot_path 不为 None 时，在每个 GUI_TREE 检查点保存一张截图；
    没有 GUI_TREE 时保存最终画面。
    """
    load_kwargs = {"target": target}
    if case_map_profile:
        load_kwargs["profile"] = case_map_profile
    cases = load_case_map(sheet_name, **load_kwargs)
    if case_id not in cases:
        location = case_map_profile or target
        raise KeyError(f"case_id {case_id} 不在 {location}/{sheet_name}.json 中")
    case = cases[case_id]

    if target == "hardware":
        from agent_loop_system.tools.hardware_target import HardwareTargetConfig
        from agent_loop_system.tools.real_device import RealDeviceSession

        HardwareTargetConfig.from_env()
        if screenshot_path is None:
            screenshot_path = str(
                EVIDENCE_DIR / "hardware" / f"{sheet_name}_{case_id}.bmp"
            )
        evidence_dir = Path(screenshot_path).resolve().parent
        session = RealDeviceSession(evidence_dir=evidence_dir)
    elif target == "simulator":
        session = SimulatorSession(get_simulator_exe())
    else:
        raise ValueError(f"未知执行目标: {target!r}")

    session.start()
    try:
        result = run_case(session, case, screenshot_path=screenshot_path)
        return result
    finally:
        session.stop()


def main(argv: list[str] | None = None) -> int:
    from agent_loop_system.main import _load_env

    _configure_console_output()
    _load_env()
    parser = argparse.ArgumentParser(prog="agent_loop_system.tools.test")
    parser.add_argument("--sheet", required=True, help="sheet 名（映射表文件名）")
    parser.add_argument("--case-id", required=True, help="用例编号")
    parser.add_argument(
        "--target",
        choices=("simulator", "hardware"),
        default="simulator",
        help="执行方式：模拟器或真机",
    )
    parser.add_argument(
        "--case-map-profile",
        choices=tuple(CASE_MAP_PROFILE_DIRS),
        help="可选：明确选择项目专用 case_map；不传时保持原有目标默认值",
    )
    parser.add_argument("--result-file", help="可选：把本次结果写入指定 JSON")
    parser.add_argument("--screenshot-path", help="可选：保存测试目标截图")
    args = parser.parse_args(argv)

    print(
        f"[test] 加载用例: target={args.target} "
        f"sheet={args.sheet} case_id={args.case_id}"
    )
    result = run_single_case(
        args.sheet,
        args.case_id,
        args.screenshot_path,
        target=args.target,
        case_map_profile=args.case_map_profile,
    )

    print(f"[test] skipped={result.skipped} aborted={result.aborted}")
    print(f"[test] setup_errors={result.setup_errors}")
    print(f"[test] action_errors={result.action_errors}")
    print(f"[test] collect_errors={result.collect_errors}")
    print(f"[test] terminal_json count={len(result.terminal_json)}")
    print(f"[test] expected_text: {result.expected_text}")
    print(f"[test] verification_points: {result.verification_points}")
    print(f"[test] screenshots: {len(result.screenshots)}")

    decision = judge_case_result(result)
    evidence_path = save_evidence(result, decision, args.result_file)
    print(f"[test] verdict: {decision.verdict}")
    print(f"[test] reason: {decision.reason}")

    print(f"[test] 证据已落盘: {evidence_path}")
    if decision.verdict == "SKIP":
        return 2
    return 0 if decision.verdict == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
