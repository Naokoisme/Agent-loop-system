"""从当前真实固件源码提取 LLM 所需的命令表和窗口名表。

输出到 sim_tools/kb/。case_map 的业务语义和坐标不从脚本反向推测，
必须来自当前源码、GUI 树和实际截图验证。
"""
from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path

from agent_loop_system.runtime_root import (
    RuntimePaths,
    load_app_env,
    resolve_config_path,
)
from agent_loop_system.tools.enter_page_contract import (
    EnterPageParamValue,
    render_enter_page_knowledge,
)

SIM_TOOLS = Path(__file__).parent
KB_DIR = SIM_TOOLS / "kb"
load_app_env()
W30_ROOT = resolve_config_path(
    os.environ.get(
        "W30_SOURCE_ROOT",
        RuntimePaths.from_root().firmware_workspaces / "620C_W6830",
    )
)
PROJECT_NAME = os.environ.get("W30_PROJECT", "620C_W6830")
C_FILE = W30_ROOT / "core" / "comm" / "srv" / "test" / "hlq_quick_cmd_handler.c"
PROJECT_CMAKE = W30_ROOT / "app" / "projects" / PROJECT_NAME / "Project.cmake"
APP_WINDOWS = W30_ROOT / "app" / "windows"
APP_QUICK_CMD = W30_ROOT / "app" / "comm" / "TuoBu" / "quick_cmd" / "gui_comm_quick_cmd.c"


@dataclass(frozen=True)
class CommandCapability:
    """从固件注册表和 handler 得到的命令可用性。"""

    name: str
    handler: str
    available: bool
    unavailable_reason: str | None = None


def _balanced_body(source: str, brace_start: int) -> tuple[str, int]:
    """返回 brace_start 对应花括号内的文本和右花括号位置。"""
    depth = 0
    for index in range(brace_start, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[brace_start + 1:index], index
    return "", brace_start


def _function_body_any(source: str, function_name: str) -> str:
    """按函数名提取普通 C 函数正文；不解释 C，只做保守的花括号配对。"""
    match = re.search(
        rf"\b{re.escape(function_name)}\s*\([^)]*\)\s*\{{",
        source,
    )
    if not match:
        return ""
    body, _ = _balanced_body(source, match.end() - 1)
    return body


def _command_entries(source: str) -> list[tuple[str, str, str]]:
    """返回 (命令名, handler, 注册表注释)，顺序与固件注册表一致。"""
    table = re.search(
        r"quick_test_cmd_table\s*\[\s*\]\s*=\s*\{(?P<body>.*?)\n\s*\};",
        source,
        re.DOTALL,
    )
    if not table:
        raise ValueError("quick_test_cmd_table[] 未找到")
    entries = []
    for line in table.group("body").splitlines():
        match = re.match(
            r'\s*\{\s*"(:[^"]+)"\s*,\s*([A-Za-z0-9_]+)\s*\}\s*,?'
            r'(?:\s*//\s*(.*))?$',
            line,
        )
        if match:
            entries.append(
                (match.group(1).strip(":"), match.group(2), (match.group(3) or "").strip())
            )
    return entries


def _argument_name(expression: str) -> str:
    expression = re.sub(r"\([^)]*\)", "", expression).strip()
    fields = re.findall(r"(?:->|\.)([A-Za-z_]\w*)", expression)
    if fields:
        return fields[-1]
    names = re.findall(r"[A-Za-z_]\w*", expression)
    return names[-1] if names else expression


def _semantic_scan_name(body: str, parsed_name: str) -> str:
    """把 v1/value2 这类 scanf 临时变量映射到随后写入的业务变量名。"""
    parsed = re.fullmatch(r"parsed_(.+)_val", parsed_name)
    if parsed:
        return parsed.group(1)
    if not re.fullmatch(r"(?:v|value|arg)\d+", parsed_name, re.IGNORECASE):
        return parsed_name
    for assignment in re.finditer(
        r"\b([A-Za-z_]\w*)\s*=\s*(?P<value>[^;]+);",
        body,
    ):
        target = assignment.group(1)
        if "sscanf(" in assignment.group("value"):
            continue
        if target != parsed_name and re.search(
            rf"\b{re.escape(parsed_name)}\b", assignment.group("value")
        ):
            return target
    return parsed_name


def _leading_string_argument(body: str, input_name: str) -> str | None:
    """识别 ``cmd=atoi(str)`` 后再从逗号后的别名解析 CSV 的协议。"""
    if input_name == "str" or not re.search(
        rf"\b{re.escape(input_name)}\s*=\s*strchr\(\s*str\s*,\s*['\"]\s*,\s*['\"]\s*\)",
        body,
    ):
        return None
    assignment = re.search(
        r"([A-Za-z_]\w*(?:->\w+)?)\s*=\s*(?:\([^;=]+\)\s*)?atoi\s*\(\s*str\s*\)",
        body,
    )
    return _argument_name(assignment.group(1)) if assignment else None


def _parameter_contract(body: str, description: str) -> str:
    """从 handler 的真实解析语句提取参数名；提取不到时明确标未知。"""
    if "无参数" in description:
        return "无"
    if not body:
        return "handler未找到"
    if re.search(r"\(void\)\s*str\s*;", body):
        return "无（handler明确忽略参数）"
    if "_quick_cmd_parse_24_data" in body:
        clear = "0=清除，" if "_quick_cmd_is_clear_data" in body else ""
        return clear + "否则24个逗号分隔数值"
    if "12位" in description and "时间" in description:
        return "YYMMDDHHmmss（12位，按源码示例）"
    contracts: list[str] = []
    for match in re.finditer(
        r'sscanf\s*\(\s*(?P<input>[A-Za-z_]\w*)\s*,\s*"([^"]*)"\s*,(?P<args>.*?)\)\s*'
        r'(?:[!<>=]=?|;)',
        body,
        re.DOTALL,
    ):
        args = [_argument_name(item) for item in match.group("args").split(",")]
        args = [_semantic_scan_name(body, item) for item in args]
        # %n 只用于确认 sscanf 是否完整消费输入，不是协议参数。
        args = [item for item in args if item and item != "consumed"]
        leading = _leading_string_argument(body, match.group("input"))
        if leading and (not args or args[0] != leading):
            args.insert(0, leading)
        if args:
            contracts.append(",".join(args))

    if not contracts and re.search(r"\batoi\s*\(\s*str\s*\)", body):
        assignment = re.search(
            r"([A-Za-z_]\w*(?:->\w+)?)\s*=\s*(?:\([^;=]+\)\s*)?atoi\s*\(\s*str\s*\)",
            body,
        )
        contracts.append(_argument_name(assignment.group(1)) if assignment else "value")

    if not contracts:
        chunks = []
        for match in re.finditer(
            r"memcpy\(\s*([A-Za-z_]\w*)\s*,\s*str(?:\s*\+\s*(\d+))?\s*,\s*(\d+)\s*\)",
            body,
        ):
            chunks.append((int(match.group(2) or 0), match.group(1), int(match.group(3))))
        if len(chunks) >= 2:
            contracts.append(
                "+".join(f"{name}[{length}]" for _offset, name, length in sorted(chunks))
            )

    if not contracts and ("comma_pos" in body or re.search(r"comma\s*\[\s*\d+\s*\]", body)):
        candidates: list[tuple[int, str]] = []
        patterns = (
            r"_quick_cmd_parse_u32_seq\([^;]*?&([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)?)\)",
            r"memcpy\(\s*([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)?)\s*,\s*str",
            r"(?:const\s+)?char\s*\*\s*([A-Za-z_]\w*)\s*=\s*str\s*\+",
            r"\b(?:uint\d+_t|int|bool)\s+([A-Za-z_]\w*)\s*=\s*[^;]*str\[",
        )
        for pattern in patterns:
            for match in re.finditer(pattern, body, re.DOTALL):
                candidates.append((match.start(), _argument_name(match.group(1))))
        ordered = []
        for _position, name in sorted(candidates):
            if name not in ordered:
                ordered.append(name)
        comma_array = re.search(r"comma\s*\[\s*(\d+)\s*\]", body)
        expected = int(comma_array.group(1)) + 1 if comma_array else 2
        if len(ordered) >= expected:
            contracts.append(",".join(ordered[:expected]))
        elif ordered:
            contracts.append(",".join(ordered) + f",...（共{expected}项）")
        else:
            contracts.append(f"CSV共{expected}项（字段名未显式声明）")

    if not contracts and "_quick_cmd_parse_u32_seq" in body:
        contracts.append("seq")

    if contracts:
        return " / ".join(dict.fromkeys(contracts))
    example = re.search(r"TOP5STEP:[A-Z0-9_]+:([^;]*)", description)
    if example:
        value = example.group(1).strip()
        return f"源码示例={value}" if value else "无"
    body_without_logs = re.sub(r"^.*(?:LOG_|printf\().*$", "", body, flags=re.MULTILINE)
    if not re.search(r"\bstr\b|\blen\b", body_without_logs):
        return "无（handler不读取参数）"
    return "raw（handler未给字段命名）"


def _if_value_variants(body: str) -> list[str]:
    """提取简单数值 if 分支中源码明确表达的有效值和清空语义。"""
    variants = []
    pattern = re.compile(
        r"\bif\s*\(\s*([A-Za-z_]\w*)\s*(==|!=)\s*(-?(?:0[xX][0-9A-Fa-f]+|\d+))\s*\)\s*\{"
    )
    for condition in pattern.finditer(body):
        field, operator, value = condition.group(1), condition.group(2), condition.group(3)
        block, _ = _balanced_body(body, condition.end() - 1)
        label = None
        if operator == "==" and re.search(r"\bmemset\s*\([^,]+,\s*0\s*,", block):
            label = "清空或重置数据"
        elif operator == "!=" and '"rejected"' in block and "return false;" in block:
            label = "有效值（其他值rejected）"
        elif operator == "==" and '"accepted"' in block and "return true;" in block:
            label = "直接成功返回"
        if label:
            variants.append(f"{field}={value}:{label}")
    return list(dict.fromkeys(variants))


def _strcmp_string_variants(body: str) -> list[str]:
    """提取 handler 中 strcmp/strncmp 明确支持的字符串取值。"""
    variants = []
    patterns = (
        r'\bstrcmp\(\s*([A-Za-z_]\w*)\s*,\s*"([^"]+)"\s*\)\s*==\s*0',
        r'\bstrncmp\(\s*([A-Za-z_]\w*)\s*,\s*"([^"]+)"\s*,\s*[^)]+\)\s*==\s*0',
    )
    for pattern in patterns:
        for match in re.finditer(pattern, body):
            variants.append(f"{match.group(1)}={match.group(2)}:源码支持")
    return list(dict.fromkeys(variants))


def _switch_cases(body: str) -> list[tuple[str, str, str, str]]:
    """提取顶层 switch case，返回 (switch表达式, case值, 注释, case正文)。"""
    result: list[tuple[str, str, str, str]] = []
    for switch in re.finditer(r"switch\s*\(([^)]+)\)\s*\{", body):
        switch_body, _ = _balanced_body(body, switch.end() - 1)
        candidates = []
        for case in re.finditer(r"\bcase\s+([^:]+)\s*:", switch_body):
            prefix = switch_body[:case.start()]
            if prefix.count("{") != prefix.count("}"):
                continue
            line_end = switch_body.find("\n", case.end())
            if line_end < 0:
                line_end = len(switch_body)
            comment_match = re.search(r"//\s*(.*)", switch_body[case.end():line_end])
            candidates.append(
                (case.start(), case.end(), case.group(1).strip(),
                 (comment_match.group(1).strip() if comment_match else ""))
            )
        for index, (start, end, value, comment) in enumerate(candidates):
            block_end = candidates[index + 1][0] if index + 1 < len(candidates) else len(switch_body)
            result.append((switch.group(1).strip(), value, comment, switch_body[end:block_end]))
    return result


def _compact_calls(block: str) -> list[str]:
    names = re.compile(
        r"\b((?:(?:srv_|dal_|sim_|shell_|gui_comm_|_quick_cmd_)[A-Za-z0-9_]*|"
        r"gui_open_new_win(?:_with_user_data)?|_messenger_send))\s*\("
    )
    calls = []
    for match in names.finditer(block):
        paren_start = block.find("(", match.start())
        depth = 0
        for index in range(paren_start, len(block)):
            if block[index] == "(":
                depth += 1
            elif block[index] == ")":
                depth -= 1
                if depth == 0:
                    calls.append(block[match.start():index + 1])
                    break
    ignored_prefixes = (
        "srv_quick_cmd_handler_check(",
        "dal_rtc_get_",
        "srv_msg_handler_",
        "_quick_cmd_parse_",
        "_quick_cmd_is_clear_data(",
    )
    compact = [re.sub(r"\s+", "", call) for call in calls]
    return [call for call in compact if not call.startswith(ignored_prefixes)]


def _state_writes(block: str) -> set[str]:
    return set(re.findall(r"->([A-Za-z_]\w*)\s*=", block))


def _unavailable_reason(body: str, description: str) -> str | None:
    if not body:
        return "handler未找到"
    if (
        '"rejected"' in body
        and "return false;" in body
        and '"accepted"' not in body
        and "return true;" not in body
        and not _compact_calls(body)
        and not _state_writes(body)
    ):
        return "handler当前只返回rejected"
    if "未实现" in description or "不可用" in description:
        return "注册表明确标记未实现"
    return None


def _event_parts(call: str) -> tuple[str, str] | None:
    match = re.match(r"srv_msg_event_send\((.*)\)$", call)
    if not match:
        return None
    args = [item.strip() for item in match.group(1).split(",")]
    if len(args) != 3:
        return None
    return args[1], args[2]


def _consumer_fields(
    app_root: Path | None,
    event_name: str,
    data_name: str,
    source_index: list[tuple[Path, list[str]]] | None = None,
) -> list[tuple[str, str]]:
    """查找事件消费方实际读取的 sys_info 字段，返回字段及源码位置。"""
    if app_root is None or not app_root.is_dir():
        return []
    facts: list[tuple[str, str]] = []
    sources = source_index
    if sources is None:
        sources = [
            (path, path.read_text(encoding="utf-8", errors="ignore").splitlines())
            for path in app_root.rglob("*.c")
        ]
    for source_file, lines in sources:
        for index, line in enumerate(lines):
            if event_name not in line:
                continue
            window = lines[index:index + 100]
            focus_start = 0
            if data_name not in {"0", "NULL"}:
                for offset, candidate in enumerate(window):
                    if data_name in candidate:
                        focus_start = offset
                        break
            focus_lines = []
            for offset, candidate in enumerate(window[focus_start:focus_start + 16]):
                if offset > 0 and re.search(r"}\s*else(?:\s+if)?\b", candidate):
                    break
                focus_lines.append(candidate)
            focus = "\n".join(focus_lines)
            for field in re.findall(
                r"sys_info_[A-Za-z0-9_]+\([^;\n]*?\)->([A-Za-z_]\w*)",
                focus,
            ):
                relative = source_file.relative_to(app_root.parent.parent.parent)
                fact = (field, f"{relative.as_posix()}:{index + focus_start + 1}")
                if fact not in facts:
                    facts.append(fact)
    return facts


def _consumer_event_index(
    source_index: list[tuple[Path, list[str]]] | None,
) -> dict[str, list[str]]:
    """一次扫描建立事件消费代码索引，避免每条命令重复遍历 app 源码。"""
    result: dict[str, list[str]] = {}
    for _source_file, lines in source_index or []:
        for index, line in enumerate(lines):
            match = re.search(r"\bcase\s+([A-Z][A-Z0-9_]+)\s*:", line)
            if match:
                result.setdefault(match.group(1), []).append("\n".join(lines[index:index + 120]))
    return result


def _consumer_value_variants(
    event_name: str,
    data_name: str,
    event_index: dict[str, list[str]],
) -> list[str]:
    """从事件消费端 ``switch(msg->data)`` 提取发送参数的真实取值含义。"""
    if data_name in {"0", "NULL"}:
        return []
    variants = []
    for consumer in event_index.get(event_name, []):
        for switch in re.finditer(
            r"switch\s*\(\s*(?:\([^)]+\)\s*)?msg->data\s*\)\s*\{",
            consumer,
        ):
            switch_body, _ = _balanced_body(consumer, switch.end() - 1)
            cases = list(re.finditer(r"\bcase\s+([^:]+)\s*:", switch_body))
            for case_index, case in enumerate(cases):
                end = cases[case_index + 1].start() if case_index + 1 < len(cases) else len(switch_body)
                block = switch_body[case.end():end]
                line_end = switch_body.find("\n", case.end())
                if line_end < 0:
                    line_end = len(switch_body)
                comment_match = re.search(r"//\s*(.*)", switch_body[case.end():line_end])
                value = case.group(1).strip()
                comment = comment_match.group(1).strip() if comment_match else ""
                label = comment
                if not label:
                    calls = re.findall(r"\b([A-Za-z_]\w*)\s*\(", block)
                    calls = [
                        name
                        for name in calls
                        if name not in {"if", "switch", "while", "for"}
                    ]
                    label = calls[0] if calls else "源码无注释"
                variants.append(f"{data_name}={value}:{label}")
    return list(dict.fromkeys(variants))


def extract_command_capabilities(c_file: Path = C_FILE) -> dict[str, CommandCapability]:
    """从当前固件源码生成命令注册表；不推断参数数量或业务取值。"""
    source = c_file.read_text(encoding="utf-8", errors="ignore")
    capabilities: dict[str, CommandCapability] = {}
    for name, handler, description in _command_entries(source):
        reason = _unavailable_reason(_function_body_any(source, handler), description)
        capabilities[name] = CommandCapability(
            name=name,
            handler=handler,
            available=reason is None,
            unavailable_reason=reason,
        )
    return capabilities


def extract_commands(c_file: Path = C_FILE, *, app_root: Path | None = None) -> str:
    """从注册表和真实 handler 生成带参数、变体、动作、前置数据、限制的目录。"""
    source = c_file.read_text(encoding="utf-8", errors="ignore")
    entries = _command_entries(source)
    analyses = []
    setters: dict[str, list[str]] = {}
    source_index = None
    if app_root is not None and app_root.is_dir():
        source_index = [
            (path, path.read_text(encoding="utf-8", errors="ignore").splitlines())
            for path in app_root.rglob("*.c")
        ]
    event_index = _consumer_event_index(source_index)
    consumer_cache: dict[tuple[str, str], list[tuple[str, str]]] = {}
    consumer_variant_cache: dict[tuple[str, str], list[str]] = {}

    for cmd, handler, description in entries:
        body = _function_body_any(source, handler)
        cases = _switch_cases(body)
        for switch_name, value, _comment, block in cases:
            for field in _state_writes(block):
                setters.setdefault(field, []).append(f"{cmd}[{switch_name}={value}]")
        for field in _state_writes(body) if not cases else set():
            setters.setdefault(field, []).append(cmd)
        analyses.append((cmd, handler, description, body, cases))

    out = [
        f"# source={c_file}",
        f"# source_sha256={hashlib.sha256(source.encode('utf-8')).hexdigest()}",
        "# wire=srv_quick_cmd send \"TOP5STEP:<COMMAND>:<ARGS>;\" | limit=128 UTF-8 bytes",
        "# order=先执行写入前置数据的命令，再执行触发命令；按需要用 GUI_PING/GUI_TREE 观察",
    ]
    for cmd, handler, description, body, cases in analyses:
        parts = [cmd, f"handler={handler}"]
        parts.append(f"说明={description or '源码注册表未说明'}")
        parts.append(f"参数={_parameter_contract(body, description)}")

        variants = _if_value_variants(body)
        variants.extend(_strcmp_string_variants(body))
        actions = []
        writes = []
        prerequisites = []
        for switch_name, value, comment, block in cases:
            label = comment or "源码无注释"
            variants.append(f"{switch_name}={value}:{label}")
            for field in sorted(_state_writes(block)):
                writes.append(f"{switch_name}={value}->{field}")
            for call in _compact_calls(block):
                actions.append(f"{switch_name}={value}->{call}")
                event = _event_parts(call)
                if event:
                    if event not in consumer_variant_cache:
                        consumer_variant_cache[event] = _consumer_value_variants(
                            *event, event_index
                        )
                    variants.extend(consumer_variant_cache[event])
                    if event not in consumer_cache:
                        consumer_cache[event] = _consumer_fields(
                            app_root, *event, source_index=source_index
                        )
                    for field, location in consumer_cache[event]:
                        writers = setters.get(field, [])
                        setup = ",".join(dict.fromkeys(writers)) if writers else "无已注册写入命令"
                        prerequisites.append(
                            f"{switch_name}={value}->{field}<=({setup});consumer={location}"
                        )

        overall_calls = _compact_calls(body) if not cases else []
        actions.extend(overall_calls)
        for call in overall_calls:
            event = _event_parts(call)
            if not event:
                continue
            if event not in consumer_variant_cache:
                consumer_variant_cache[event] = _consumer_value_variants(*event, event_index)
            variants.extend(consumer_variant_cache[event])
        if variants:
            parts.append("参数值=" + ";".join(dict.fromkeys(variants)))
        if not cases:
            writes.extend(sorted(_state_writes(body)))
        if writes:
            parts.append("写入=" + ";".join(dict.fromkeys(writes)))
        if actions:
            parts.append("触发=" + ";".join(dict.fromkeys(actions)))
        if prerequisites:
            parts.append("前置数据=" + ";".join(dict.fromkeys(prerequisites)))

        limits = ["固件注册"]
        if '"rejected"' in body:
            limits.append("参数解析或状态不满足时rejected")
        directives = re.findall(r"^\s*#(?:if|ifdef|ifndef)\s+([^\r\n]+)", body, re.MULTILINE)
        if directives:
            limits.append("编译条件=" + ",".join(dict.fromkeys(item.strip() for item in directives)))
        if re.search(r"^\s*#if\s+0\b", body, re.MULTILINE):
            limits.append("包含#if 0禁用代码，禁用分支不可作为能力")
        if re.search(r"\(\s*uint32_t\s*\)\s*&", body):
            limits.append("64位模拟器存在指针截断风险")
        unavailable_reason = _unavailable_reason(body, description)
        if unavailable_reason:
            limits.append("不可用=" + unavailable_reason)
        parts.append("限制=" + ";".join(limits))
        out.append(" | ".join(parts))
    return "\n".join(out)


def _function_body(source: str, function_name: str) -> str:
    """返回简单 C 函数的花括号正文；找不到时返回空串。"""
    return _function_body_any(source, function_name)


def _special_window_params(source: str) -> dict[str, tuple[EnterPageParamValue, ...]]:
    """从 special_win 表及其 handler 的 switch(param) 提取参数含义。"""
    table = re.search(
        r"special_win\s*\[\s*\]\s*=\s*\{(?P<body>.*?)\n\};",
        source,
        re.DOTALL,
    )
    if not table:
        return {}

    result: dict[str, tuple[EnterPageParamValue, ...]] = {}
    for name, handler in re.findall(
        r'\{\s*"([A-Z0-9_]+)"\s*,\s*([A-Za-z0-9_]+)\s*\}',
        table.group("body"),
    ):
        body = _function_body(source, handler)
        cases = list(re.finditer(r"case\s+(\d+)\s*:\s*(?://\s*([^\r\n]*))?", body))
        values: list[EnterPageParamValue] = []
        for index, case in enumerate(cases):
            end = cases[index + 1].start() if index + 1 < len(cases) else len(body)
            block = body[case.end():end]
            label = (case.group(2) or "").strip()
            if not label:
                constants = re.findall(r"=\s*([A-Z][A-Z0-9_]+)\s*;", block)
                label = constants[0] if constants else ""
            if label:
                values.append(EnterPageParamValue(int(case.group(1)), label))
        if values:
            result[name] = tuple(values)
    return result


def extract_windows(
    *,
    project_cmake: Path = PROJECT_CMAKE,
    app_windows: Path = APP_WINDOWS,
    app_quick_cmd: Path = APP_QUICK_CMD,
) -> str:
    """汇总当前项目真实注册窗口和特殊窗口参数。"""
    project_text = project_cmake.read_text(encoding="utf-8", errors="ignore")
    version_match = re.search(r"set\s*\(\s*WINDOWS_VERSION\s+([A-Za-z0-9_]+)", project_text)
    if not version_match:
        raise ValueError(f"WINDOWS_VERSION 未找到: {project_cmake}")

    quick_source = app_quick_cmd.read_text(encoding="utf-8", errors="ignore")
    special_params = _special_window_params(quick_source)
    entries: dict[str, str] = {}
    window_dir = app_windows / version_match.group(1)
    for source_file in sorted(window_dir.rglob("*.c")):
        source = source_file.read_text(encoding="utf-8", errors="ignore")
        for win_id, name, win_type in re.findall(
            r'GUI_WIN_DEFINE\(\s*([A-Z0-9_]+)\s*,\s*"([^"]+)"\s*,\s*([A-Z0-9_]+)',
            source,
        ):
            entries[name] = (
                f"{name} -> {name} | id={win_id} | {win_type} | "
                + render_enter_page_knowledge(
                    name,
                    special_values=special_params.get(name, ()),
                )
            )

    return "\n".join(entries[key] for key in sorted(entries))


def main():
    # 与修复流程共用 .env，避免手工运行时退回用户正在开发的原始目录。
    try:
        from agent_loop_system.main import _load_env

        _load_env()
    except ImportError:
        pass

    source_root = Path(os.environ.get("W30_SOURCE_ROOT", str(W30_ROOT)))
    project_name = os.environ.get("W30_PROJECT", PROJECT_NAME)
    c_file = source_root / "core" / "comm" / "srv" / "test" / "hlq_quick_cmd_handler.c"
    project_cmake = source_root / "app" / "projects" / project_name / "Project.cmake"
    app_windows = source_root / "app" / "windows"
    app_quick_cmd = source_root / "app" / "comm" / "TuoBu" / "quick_cmd" / "gui_comm_quick_cmd.c"
    app_root = source_root / "app" / "comm" / "TuoBu"

    commands = extract_commands(c_file, app_root=app_root)
    windows = extract_windows(
        project_cmake=project_cmake,
        app_windows=app_windows,
        app_quick_cmd=app_quick_cmd,
    )
    KB_DIR.mkdir(exist_ok=True)
    (KB_DIR / "commands.txt").write_text(commands, encoding="utf-8")
    (KB_DIR / "windows.txt").write_text(windows, encoding="utf-8")
    print(f"source:   {source_root} ({project_name})")
    print(f"commands: {len(commands.splitlines()) - 4} 条")
    print(f"windows:  {len(windows.splitlines())} 条")
    print(f"输出到 {KB_DIR}")


if __name__ == "__main__":
    main()
