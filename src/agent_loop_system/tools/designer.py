"""通过 designer_mcp_adapter 执行隔离、可回滚的 Designer 多文件修复。"""
from __future__ import annotations

import difflib
import hashlib
import json
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from agent_loop_system.runtime_root import resolve_config_path
from agent_loop_system.tools.workspace import resolve_source_root

DEFAULT_ADAPTER_PATH = Path(r"D:\designer_mcp_adapter\server.py")
_ALLOWED_OPERATIONS = {
    "add", "set", "move", "resize", "delete", "reparent", "reorder", "clone", "page_set"
}
_READ_ONLY_NATIVE_TOOLS = {
    "action_catalog",
    "action_schema",
    "binding_list",
    "module_get",
    "module_list",
    "preset_list",
    "project_settings_get",
    "resource_font_families",
    "resource_font_get",
    "resource_font_list",
    "resource_image_get_info",
    "resource_image_refresh",
    "resource_image_tree",
    "translation_get",
}
_EXECUTOR_OWNED_NATIVE_TOOLS = {
    "codegen_generate_to_project",
    "codegen_preview",
    "module_codegen_generate_to_project",
    "module_codegen_preview",
    "module_generate_code",
    "project_close",
    "project_history_clear",
    "project_history_delete",
    "project_open",
    "project_redo",
    "project_save",
    "project_undo",
}
_DESIGNER_OWNED_NATIVE_PREFIXES = (
    "binding_",
    "module_",
    "preset_",
    "resource_",
    "translation_",
)


class DesignerIntegrationError(RuntimeError):
    """Designer 连接、计划或事务校验失败。"""


class DesignerNativeCall(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool_name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class VmUserPatch(BaseModel):
    """Designer 生成后才允许应用的 VM USER 区补丁。"""

    model_config = ConfigDict(extra="forbid")

    file_path: str
    user_section: str
    declared_by_native_call: int | None = None
    before: str
    after: str
    reason: str


class DesignerPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    root_cause_analysis: str
    page_name: str
    layout_operations: list[dict[str, Any]] = Field(default_factory=list)
    native_calls: list[DesignerNativeCall] = Field(default_factory=list)
    vm_user_patch: VmUserPatch | None = None
    blocked_capability: str | None = None
    reason: str


class DesignerContext(BaseModel):
    project_path: str
    ui_layout_name: str
    page_name: str
    page: dict[str, Any]
    native_tool_schemas: dict[str, dict[str, Any]] = Field(default_factory=dict)
    capability_groups: dict[str, dict[str, Any]] = Field(default_factory=dict)
    reference_data: dict[str, Any] = Field(default_factory=dict)
    vm_user_sections: dict[str, list[dict[str, str]]] = Field(default_factory=dict)


class DesignerApplyResult(BaseModel):
    success: bool
    error: str = ""
    page_json_path: str = ""
    changed_files: list[str] = Field(default_factory=list)
    transaction_manifest: str | None = None
    apply_result: dict[str, Any] | None = None
    native_results: list[dict[str, Any]] = Field(default_factory=list)
    vm_user_patch_path: str | None = None
    preview_result: dict[str, Any] | None = None
    generation_result: dict[str, Any] | None = None


def designer_enabled(value: str | None = None) -> bool:
    raw = os.environ.get("DESIGNER_ENABLED", "0") if value is None else value
    return str(raw).strip().casefold() in {"1", "true", "yes", "on"}


def designer_adapter_path() -> Path:
    return resolve_config_path(
        os.environ.get("DESIGNER_MCP_ADAPTER_PATH", str(DEFAULT_ADAPTER_PATH))
    )


def designer_project_path() -> Path:
    root = resolve_source_root()
    project = os.environ.get("W30_PROJECT", "").strip()
    if not project:
        raise DesignerIntegrationError("W30_PROJECT 未配置")
    path = (root / "app" / "projects" / project).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise DesignerIntegrationError("Designer 项目越出隔离工作区") from exc
    if not path.is_dir():
        raise DesignerIntegrationError(f"Designer 隔离项目不存在: {path}")
    return path


class DesignerMcpSession:
    """极小 JSON-lines MCP 客户端；所有 Designer 语义留在独立适配器中。"""

    def __init__(self, project_path: Path, *, adapter_path: Path | None = None) -> None:
        adapter = (adapter_path or designer_adapter_path()).resolve()
        if not adapter.is_file():
            raise DesignerIntegrationError(f"Designer MCP 适配器不存在: {adapter}")
        self._next_id = 0
        self._process = subprocess.Popen(
            [sys.executable, str(adapter), "--project", str(project_path), "--timeout", "180"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            bufsize=1,
        )
        self._request(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "agent-loop-system", "version": "0.1.0"},
            },
        )

    def _request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if not self._process.stdin or not self._process.stdout:
            raise DesignerIntegrationError("Designer MCP stdio 未建立")
        self._next_id += 1
        payload = {"jsonrpc": "2.0", "id": self._next_id, "method": method, "params": params}
        try:
            self._process.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
            self._process.stdin.flush()
            line = self._process.stdout.readline()
        except (BrokenPipeError, OSError) as exc:
            raise DesignerIntegrationError(f"Designer MCP stdio 失败: {exc}") from exc
        if not line:
            raise DesignerIntegrationError("Designer MCP 适配器提前退出")
        response = json.loads(line)
        if response.get("id") != self._next_id:
            raise DesignerIntegrationError("Designer MCP 响应 id 不匹配")
        if response.get("error"):
            raise DesignerIntegrationError(f"Designer MCP 协议错误: {response['error']}")
        return response.get("result") or {}

    def call(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
        result = self._request("tools/call", {"name": name, "arguments": arguments or {}})
        content = result.get("content") or []
        text_items = [item.get("text", "") for item in content if item.get("type") == "text"]
        value: Any = None
        if text_items:
            value = json.loads(text_items[-1])
        if result.get("isError"):
            raise DesignerIntegrationError(f"Designer 工具 {name} 失败: {value}")
        return value

    def close(self) -> None:
        process = getattr(self, "_process", None)
        if not process:
            return
        if process.stdin:
            try:
                process.stdin.close()
            except OSError:
                pass
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
        self._process = None

    def __enter__(self) -> "DesignerMcpSession":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()


def page_name_from_trace(trace: dict[str, Any] | None) -> str:
    candidates = page_candidates_from_trace(trace)
    return candidates[0] if candidates else ""


def page_candidates_from_trace(trace: dict[str, Any] | None) -> list[str]:
    """按运行时可见优先级返回页面候选，保留窗口作为 popup 的后备。"""
    candidates: list[str] = []
    for step in reversed((trace or {}).get("steps") or []):
        for key in ("popup_name", "window_name"):
            name = str(step.get(key) or "").strip()
            if name and name not in candidates:
                candidates.append(name)
    return candidates


def _native_tool_is_designer_owned(name: str) -> bool:
    if name in _READ_ONLY_NATIVE_TOOLS or name in _EXECUTOR_OWNED_NATIVE_TOOLS:
        return False
    if name == "project_settings_update":
        return True
    return name.startswith(_DESIGNER_OWNED_NATIVE_PREFIXES)


def _extract_user_section_records(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        return []
    text = path.read_text(encoding="utf-8", errors="replace")
    records: list[dict[str, str]] = []
    cursor = 0
    while True:
        marker_start = text.find("//@USER_BEGIN", cursor)
        if marker_start < 0:
            return records
        marker_end = text.find("\n", marker_start)
        if marker_end < 0:
            return records
        end = text.find("//@USER_END", marker_end)
        if end < 0:
            return records
        marker = text[marker_start:marker_end].strip()
        section = marker.partition(":")[2].strip()
        records.append({"section": section, "content": text[marker_end + 1:end]})
        cursor = end + len("//@USER_END")


def _native_read_arguments(
    schema: dict[str, Any], *, page_name: str
) -> dict[str, Any]:
    """为只读原生工具填充当前页面和显式 nullable 默认值。"""
    properties = schema.get("properties") or {}
    arguments: dict[str, Any] = {}
    if "pageName" in properties:
        arguments["pageName"] = page_name
    for name in set(schema.get("required") or []) - {"sessionId"}:
        if name in arguments:
            continue
        definition = properties.get(name) or {}
        field_type = definition.get("type")
        if "default" in definition:
            arguments[name] = definition["default"]
        elif field_type == "null" or (
            isinstance(field_type, list) and "null" in field_type
        ):
            arguments[name] = None
    return arguments


def _action_names_from_catalog(catalog: Any) -> list[str]:
    """兼容常见 catalog 包装，提取稳定的动作类型名。"""
    items = catalog
    if isinstance(catalog, dict):
        items = catalog.get("actions") or catalog.get("items") or []
    if not isinstance(items, list):
        return []
    names: list[str] = []
    for item in items:
        if isinstance(item, str):
            name = item
        elif isinstance(item, dict):
            name = str(
                item.get("actionType")
                or item.get("name")
                or item.get("type")
                or ""
            )
        else:
            name = ""
        if name and name not in names:
            names.append(name)
    return names


def _action_schema_arguments(schema: dict[str, Any], action_name: str) -> dict[str, Any]:
    properties = schema.get("properties") or {}
    for key in ("actionType", "actionName", "name"):
        if key in properties:
            return {key: action_name}
    raise DesignerIntegrationError(
        "action_schema 必须声明 actionType、actionName 或 name 参数"
    )


def _module_items(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, dict):
        value = value.get("modules") or value.get("items") or []
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _service_ids_from_page(value: Any) -> set[str]:
    service_ids: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key).casefold() == "serviceid" and item:
                service_ids.add(str(item).casefold())
            else:
                service_ids.update(_service_ids_from_page(item))
    elif isinstance(value, list):
        for item in value:
            service_ids.update(_service_ids_from_page(item))
    return service_ids


def _module_mutation_names(plan: DesignerPlan) -> list[str | None]:
    names: list[str] = []
    has_mutation = False
    for call in plan.native_calls:
        if not call.tool_name.startswith("module_"):
            continue
        has_mutation = True
        name = str(call.arguments.get("moduleName") or call.arguments.get("name") or "")
        if name and name not in names:
            names.append(name)
    if not has_mutation:
        return []
    return names or [None]


def _codegen_result_verified(result: Any, *, project: bool) -> bool:
    if not isinstance(result, dict):
        return False
    if project:
        return bool(
            result.get("projectFilesVerified")
            or result.get("filesVerified")
            or (result.get("project") or {}).get("filesVerified")
        )
    return bool(
        result.get("filesVerified")
        or (result.get("preview") or {}).get("filesVerified")
    )


def load_designer_context(trace: dict[str, Any] | None) -> DesignerContext | None:
    page_candidates = page_candidates_from_trace(trace)
    if not page_candidates:
        return None
    project_path = designer_project_path()
    with DesignerMcpSession(project_path) as client:
        status = client.call("designer_status", {})
        if not status.get("available") or not status.get("ready"):
            return None
        opened = client.call("designer_open_project", {"uiLayoutType": "Main"})
        pages = client.call("designer_list_pages", {})
        available = {str(item.get("name")) for item in pages.get("pages", [])}
        page_name = next((name for name in page_candidates if name in available), "")
        if not page_name:
            return None
        page = client.call(
            "designer_get_page",
            {"pageName": page_name, "includeBindings": True, "includeProperties": False},
        )
        native = client.call(
            "designer_native_tools", {"prefix": "", "includeSchemas": True}
        )
        native_tool_schemas = {
            str(item.get("name")): item.get("inputSchema") or {}
            for item in native.get("tools", [])
            if _native_tool_is_designer_owned(str(item.get("name") or ""))
        }
        all_native_schemas = {
            str(item.get("name")): item.get("inputSchema") or {}
            for item in native.get("tools", [])
        }
        capability_groups = dict(status.get("capabilityGroups") or {})
        reference_data: dict[str, Any] = {}
        translation = capability_groups.get("translation_entries") or {}
        if translation.get("ready") and "translation_get" in all_native_schemas:
            schema = all_native_schemas["translation_get"]
            reference_data["translation"] = client.call(
                "designer_native_call",
                {
                    "toolName": "translation_get",
                    "arguments": _native_read_arguments(schema, page_name=page_name),
                },
            )
        action_list = capability_groups.get("action_list") or {}
        if action_list.get("ready") and "action_catalog" in all_native_schemas:
            catalog = client.call(
                "designer_native_call",
                {
                    "toolName": "action_catalog",
                    "arguments": _native_read_arguments(
                        all_native_schemas["action_catalog"], page_name=page_name
                    ),
                },
            )
            action_schemas: dict[str, Any] = {}
            schema = all_native_schemas.get("action_schema") or {}
            for action_name in _action_names_from_catalog(catalog)[:64]:
                action_schemas[action_name] = client.call(
                    "designer_native_call",
                    {
                        "toolName": "action_schema",
                        "arguments": _action_schema_arguments(schema, action_name),
                    },
                )
            reference_data["actions"] = {
                "catalog": catalog,
                "schemas": action_schemas,
            }
        resources: dict[str, Any] = {}
        for tool_name, key in (
            ("resource_image_tree", "images"),
            ("resource_font_list", "fonts"),
        ):
            schema = all_native_schemas.get(tool_name)
            if schema is None:
                continue
            resources[key] = client.call(
                "designer_native_call",
                {
                    "toolName": tool_name,
                    "arguments": _native_read_arguments(schema, page_name=page_name),
                },
            )
        if resources:
            reference_data["resources"] = resources
        resource_capability = capability_groups.get("resources") or {}
        local_settings = project_path / "setting.local.json"
        if local_settings.is_file():
            settings = json.loads(local_settings.read_text(encoding="utf-8"))
            configured_image_path = str(settings.get("ImagePath") or "").strip()
            isolation_ready = False
            if configured_image_path:
                try:
                    Path(configured_image_path).resolve().relative_to(resolve_source_root())
                    isolation_ready = True
                except ValueError:
                    pass
            resource_capability["isolationReady"] = isolation_ready
            if not isolation_ready:
                resource_capability["ready"] = False
                resource_capability["isolationIssue"] = (
                    f"ImagePath 越出隔离工作区: {configured_image_path or '<empty>'}"
                )
            capability_groups["resources"] = resource_capability
        if "module_list" in all_native_schemas:
            modules = client.call(
                "designer_native_call",
                {"toolName": "module_list", "arguments": {}},
            )
            service_ids = _service_ids_from_page(page)
            definitions: dict[str, Any] = {}
            resolved_service_ids: set[str] = set()
            for item in _module_items(modules):
                module_name = str(item.get("Name") or item.get("name") or "")
                if not module_name or module_name.casefold() not in service_ids:
                    continue
                resolved_service_ids.add(module_name.casefold())
                definitions[module_name] = client.call(
                    "designer_native_call",
                    {
                        "toolName": "module_get",
                        "arguments": {"moduleName": module_name},
                    },
                )
            unresolved = sorted(service_ids - resolved_service_ids)
            reference_data["common_modules"] = {
                "list": modules,
                "referencedServiceIds": sorted(service_ids),
                "referencedDefinitions": definitions,
                "unresolvedServiceIds": unresolved,
            }
            if unresolved:
                module_capability = capability_groups.get("common_module_codegen") or {}
                module_capability["ready"] = False
                module_capability["unresolvedServiceIds"] = unresolved
                capability_groups["common_module_codegen"] = module_capability
        ui_layout_name = str(opened.get("uiLayoutName") or "")
        layout_dir = (project_path.parent.parent / "windows" / ui_layout_name).resolve()
        page_stem = f"gui_win_{page_name.casefold()}_vm"
        vm_user_sections: dict[str, list[dict[str, str]]] = {}
        source_root = resolve_source_root()
        for suffix in (".c", ".h"):
            vm_path = layout_dir / f"{page_stem}{suffix}"
            if vm_path.is_file():
                relative = vm_path.relative_to(source_root).as_posix()
                vm_user_sections[relative] = _extract_user_section_records(vm_path)
        return DesignerContext(
            project_path=str(project_path),
            ui_layout_name=ui_layout_name,
            page_name=page_name,
            page=page,
            native_tool_schemas=native_tool_schemas,
            capability_groups=capability_groups,
            reference_data=reference_data,
            vm_user_sections=vm_user_sections,
        )


def validate_designer_plan(plan: DesignerPlan, context: DesignerContext) -> None:
    if plan.page_name != context.page_name:
        raise DesignerIntegrationError(
            f"Designer page_name 必须等于实际缺陷页面: {context.page_name}"
        )
    has_changes = bool(
        plan.layout_operations or plan.native_calls or plan.vm_user_patch is not None
    )
    if plan.blocked_capability is not None:
        if has_changes:
            raise DesignerIntegrationError("能力阻塞方案不得同时包含修改")
        capability = context.capability_groups.get(plan.blocked_capability)
        if not capability:
            raise DesignerIntegrationError(
                f"未知 Designer 能力组: {plan.blocked_capability}"
            )
        if capability.get("ready"):
            raise DesignerIntegrationError(
                f"Designer 能力组实际可用，不得声明阻塞: {plan.blocked_capability}"
            )
        return
    if not has_changes:
        raise DesignerIntegrationError("Designer 方案没有任何修改")
    for index, operation in enumerate(plan.layout_operations):
        name = str(operation.get("op") or "")
        if name not in _ALLOWED_OPERATIONS:
            raise DesignerIntegrationError(f"Designer operation[{index}] 不允许: {name}")
    for index, native_call in enumerate(plan.native_calls):
        name = native_call.tool_name
        schema = context.native_tool_schemas.get(name)
        if not schema or not _native_tool_is_designer_owned(name):
            raise DesignerIntegrationError(f"Designer native_call[{index}] 不允许: {name}")
        arguments = native_call.arguments
        if "sessionId" in arguments:
            raise DesignerIntegrationError(
                f"Designer native_call[{index}] 不得自行传 sessionId"
            )
        properties = schema.get("properties") or {}
        unknown = sorted(set(arguments) - (set(properties) - {"sessionId"}))
        if unknown:
            raise DesignerIntegrationError(
                f"Designer native_call[{index}] 含 schema 外参数: {unknown}"
            )
        required = set(schema.get("required") or []) - {"sessionId"}
        missing = sorted(required - set(arguments))
        if missing:
            raise DesignerIntegrationError(
                f"Designer native_call[{index}] 缺少参数: {missing}"
            )
        page_name = arguments.get("pageName")
        if page_name is not None and page_name != context.page_name:
            raise DesignerIntegrationError(
                f"Designer native_call[{index}] pageName 越出当前页面: {page_name}"
            )
        action_list_field = None
        actions_field = None
        if name == "binding_event_bind":
            action_list_field = arguments.get("isActionList")
            actions_field = arguments.get("actionsJson")
        elif name == "binding_event_edit":
            action_list_field = arguments.get("newIsActionList")
            actions_field = arguments.get("newActionsJson")
        if action_list_field is True:
            capability = context.capability_groups.get("action_list") or {}
            if not capability.get("ready"):
                raise DesignerIntegrationError(
                    "ActionList 绑定必须先取得 Designer 动作目录和参数 schema"
                )
            try:
                actions = (
                    json.loads(actions_field)
                    if isinstance(actions_field, str)
                    else actions_field
                )
            except json.JSONDecodeError as exc:
                raise DesignerIntegrationError("actionsJson 不是有效 JSON") from exc
            if not isinstance(actions, list) or not actions:
                raise DesignerIntegrationError("ActionList 必须包含至少一个动作")
        if name.startswith("module_"):
            capability = context.capability_groups.get("common_module_codegen") or {}
            if not capability.get("ready"):
                raise DesignerIntegrationError(
                    "CommonModule 修改必须由 Adapter 提供 preview 和 generate_to_project 能力"
                )
        if name in {
            "resource_image_import",
            "resource_image_replace",
            "resource_image_move",
            "resource_image_delete",
        }:
            capability = context.capability_groups.get("resources") or {}
            if capability.get("isolationReady") is False:
                raise DesignerIntegrationError(
                    str(capability.get("isolationIssue") or "资源目录未隔离")
                )
    patch = plan.vm_user_patch
    if patch is not None:
        sections = context.vm_user_sections.get(patch.file_path)
        if sections is None:
            raise DesignerIntegrationError(
                f"源码 Agent 只能修改当前页面生成的 VM 文件: {patch.file_path}"
            )
        matching_sections = [
            item for item in sections if item["section"] == patch.user_section
        ]
        if len(matching_sections) != 1:
            call_index = patch.declared_by_native_call
            if call_index is None or call_index < 0 or call_index >= len(plan.native_calls):
                raise DesignerIntegrationError(
                    f"VM USER 区不是现有或本计划 Designer 声明的区段: {patch.user_section}"
                )
            declaration = plan.native_calls[call_index]
            if not declaration.tool_name.startswith("binding_"):
                raise DesignerIntegrationError(
                    "vm_user_patch.declared_by_native_call 必须指向 binding 声明"
                )
        if not patch.before or not patch.after or patch.before == patch.after:
            raise DesignerIntegrationError("VM USER patch 的 before/after 无效")
        if "//@USER_BEGIN" in patch.before + patch.after or "//@USER_END" in patch.before + patch.after:
            raise DesignerIntegrationError("VM USER patch 不得修改 USER 边界标记")
        if matching_sections and matching_sections[0]["content"].count(patch.before) != 1:
            raise DesignerIntegrationError(
                f"VM USER patch 的 before 不在声明区段内或不唯一: {patch.user_section}"
            )


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _managed_hashes(source_root: Path, managed_relative_paths: list[str]) -> dict[str, str]:
    hashes: dict[str, str] = {}
    for relative in managed_relative_paths:
        target = source_root / relative
        if target.is_file():
            hashes[target.relative_to(source_root).as_posix()] = _sha256(target)
        elif target.is_dir():
            for path in sorted(target.rglob("*")):
                if path.is_file():
                    hashes[path.relative_to(source_root).as_posix()] = _sha256(path)
    return hashes


def _write_json(path: Path, value: dict[str, Any]) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def begin_designer_transaction(
    *,
    task_id: str,
    layout_dir: Path,
    evidence_root: Path,
    extra_paths: list[Path] | None = None,
) -> Path:
    source_root = resolve_source_root()
    layout_dir = layout_dir.resolve()
    try:
        layout_relative = layout_dir.relative_to(source_root).as_posix()
    except ValueError as exc:
        raise DesignerIntegrationError("Designer layout 目录越出隔离工作区") from exc
    managed_paths = [layout_dir, *(extra_paths or [])]
    managed_relative_paths: list[str] = []
    for managed in managed_paths:
        resolved = managed.resolve()
        try:
            relative = resolved.relative_to(source_root).as_posix()
        except ValueError as exc:
            raise DesignerIntegrationError(
                f"Designer 事务路径越出隔离工作区: {resolved}"
            ) from exc
        if not any(
            relative == existing or relative.startswith(existing + "/")
            for existing in managed_relative_paths
        ):
            managed_relative_paths = [
                existing
                for existing in managed_relative_paths
                if not existing.startswith(relative + "/")
            ]
            managed_relative_paths.append(relative)
    transaction_dir = (
        evidence_root / task_id / "designer_transactions" / uuid.uuid4().hex
    ).resolve()
    transaction_dir.relative_to(evidence_root.resolve())
    backup_dir = transaction_dir / "before"
    backup_dir.mkdir(parents=True, exist_ok=False)
    before = _managed_hashes(source_root, managed_relative_paths)
    for relative in before:
        source = source_root / relative
        target = backup_dir / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    manifest = {
        "source_root": str(source_root),
        "layout_relative": layout_relative,
        "managed_relative_paths": managed_relative_paths,
        "backup_dir": str(backup_dir),
        "before": before,
        "after": {},
        "changed_files": [],
        "allowed_files": [],
    }
    manifest_path = transaction_dir / "manifest.json"
    _write_json(manifest_path, manifest)
    return manifest_path


def finalize_designer_transaction(
    manifest_path: Path,
    allowed_files: set[str],
    allowed_roots: set[str] | None = None,
) -> list[str]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    source_root = Path(manifest["source_root"])
    before = manifest["before"]
    after = _managed_hashes(source_root, manifest["managed_relative_paths"])
    changed = sorted(
        path for path in set(before) | set(after) if before.get(path) != after.get(path)
    )
    roots = allowed_roots or set()
    unexpected = sorted(
        path
        for path in set(changed) - allowed_files
        if not any(path == root or path.startswith(root.rstrip("/") + "/") for root in roots)
    )
    manifest.update(
        {
            "after": after,
            "changed_files": changed,
            "allowed_files": sorted(allowed_files),
            "allowed_roots": sorted(roots),
        }
    )
    _write_json(manifest_path, manifest)
    if unexpected:
        raise DesignerIntegrationError(f"Designer 修改了允许范围外文件: {unexpected}")
    if not changed:
        raise DesignerIntegrationError("Designer 保存和生成后没有文件发生变化")
    return changed


def rollback_designer_transaction(manifest_path: str | Path) -> None:
    path = Path(manifest_path)
    manifest = json.loads(path.read_text(encoding="utf-8"))
    source_root = Path(manifest["source_root"])
    backup_dir = Path(manifest["backup_dir"])
    before: dict[str, str] = manifest["before"]
    after: dict[str, str] = manifest["after"]
    changed: list[str] = manifest["changed_files"]
    current = _managed_hashes(source_root, manifest["managed_relative_paths"])
    drifted = [item for item in changed if current.get(item) != after.get(item)]
    if drifted:
        raise DesignerIntegrationError(f"Designer 文件在回滚前又被修改，拒绝覆盖: {drifted}")
    for relative in changed:
        target = source_root / relative
        if relative in before:
            source = backup_dir / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_name(f".{target.name}.{os.getpid()}.rollback")
            shutil.copy2(source, tmp)
            os.replace(tmp, target)
        elif target.is_file():
            target.unlink()
    restored = _managed_hashes(source_root, manifest["managed_relative_paths"])
    failed = [item for item in changed if restored.get(item) != before.get(item)]
    if failed:
        raise DesignerIntegrationError(f"Designer 多文件回滚校验失败: {failed}")


def _user_section_spans(content: bytes) -> list[dict[str, Any]]:
    text = content.decode("utf-8", "replace")
    sections: list[dict[str, Any]] = []
    names: set[str] = set()
    unnamed = 0
    cursor = 0
    while True:
        marker_start = text.find("//@USER_BEGIN", cursor)
        if marker_start < 0:
            return sections
        marker_end = text.find("\n", marker_start)
        if marker_end < 0:
            raise DesignerIntegrationError("USER_BEGIN 标记后缺少换行")
        marker = text[marker_start:marker_end].rstrip("\r").strip()
        suffix = marker[len("//@USER_BEGIN"):].strip()
        if suffix:
            if not suffix.startswith(":") or not suffix[1:].strip():
                raise DesignerIntegrationError(f"USER_BEGIN 标记格式错误: {marker}")
            section = suffix[1:].strip()
        else:
            unnamed += 1
            section = f"<unnamed:{unnamed}>"
        if section in names:
            raise DesignerIntegrationError(f"USER 区名称不唯一: {section}")
        body_start = marker_end + 1
        body_end = text.find("//@USER_END", body_start)
        nested = text.find("//@USER_BEGIN", body_start)
        if body_end < 0 or (nested >= 0 and nested < body_end):
            raise DesignerIntegrationError(f"USER 区边界不完整: {section}")
        names.add(section)
        sections.append(
            {
                "section": section,
                "body": text[body_start:body_end],
                "body_start": body_start,
                "body_end": body_end,
            }
        )
        cursor = body_end + len("//@USER_END")


def _normalize_user_body(value: str) -> str:
    return value.replace("\r\n", "\n").replace("\r", "\n")


def _user_sections(content: bytes) -> dict[str, str]:
    return {
        str(item["section"]): _normalize_user_body(str(item["body"]))
        for item in _user_section_spans(content)
    }


def _read_user_sections(path: Path) -> tuple[dict[str, str], str | None]:
    if not path.is_file():
        return {}, None
    try:
        return _user_sections(path.read_bytes()), None
    except DesignerIntegrationError as exc:
        return {}, str(exc)


def _user_section_file_diff(relative: str, before_path: Path, after_path: Path) -> dict[str, Any]:
    before, before_error = _read_user_sections(before_path)
    after, after_error = _read_user_sections(after_path)
    added = [
        {"section": name, "after": after[name]}
        for name in sorted(set(after) - set(before))
    ]
    removed = [
        {"section": name, "before": before[name]}
        for name in sorted(set(before) - set(after))
    ]
    modified: list[dict[str, str]] = []
    for name in sorted(set(before) & set(after)):
        if before[name] == after[name]:
            continue
        diff = "".join(
            difflib.unified_diff(
                before[name].splitlines(keepends=True),
                after[name].splitlines(keepends=True),
                fromfile=f"before:{relative}#{name}",
                tofile=f"after:{relative}#{name}",
            )
        )
        modified.append(
            {"section": name, "before": before[name], "after": after[name], "diff": diff}
        )
    return {
        "path": relative,
        "before_exists": before_path.is_file(),
        "after_exists": after_path.is_file(),
        "before_parse_error": before_error,
        "after_parse_error": after_error,
        "added_sections": added,
        "removed_sections": removed,
        "modified_sections": modified,
    }


def _capture_user_stage(
    manifest_path: Path,
    *,
    stage: str,
    baseline_dir: Path,
    relative_files: set[str],
    diff_filename: str,
) -> tuple[dict[str, Any], Path]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    source_root = Path(manifest["source_root"])
    transaction_dir = manifest_path.parent
    snapshot_dir = transaction_dir / stage
    snapshot_dir.mkdir(parents=True, exist_ok=False)
    user_files = sorted(
        relative for relative in relative_files if relative.lower().endswith((".c", ".h"))
    )
    for relative in user_files:
        source = source_root / relative
        if source.is_file():
            target = snapshot_dir / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
    payload = {
        "schema_version": 1,
        "stage": stage,
        "baseline_dir": str(baseline_dir),
        "snapshot_dir": str(snapshot_dir),
        "files": [
            _user_section_file_diff(
                relative, baseline_dir / relative, snapshot_dir / relative
            )
            for relative in user_files
        ],
    }
    diff_path = transaction_dir / diff_filename
    _write_json(diff_path, payload)
    stages = manifest.setdefault("stages", {})
    stages[stage] = {
        "snapshot_dir": str(snapshot_dir),
        "hashes": _managed_hashes(source_root, manifest["managed_relative_paths"]),
        "files": user_files,
        "user_diff": str(diff_path),
    }
    _write_json(manifest_path, manifest)
    return payload, diff_path


def _verify_designer_user_sections(
    payload: dict[str, Any], diff_path: Path, plan: DesignerPlan
) -> None:
    parse_errors: list[str] = []
    removed: list[str] = []
    modified: list[str] = []
    added: list[str] = []
    for item in payload["files"]:
        relative = str(item["path"])
        for key in ("before_parse_error", "after_parse_error"):
            if item.get(key):
                parse_errors.append(f"{relative}: {item[key]}")
        removed.extend(
            f"{relative}#{section['section']}" for section in item["removed_sections"]
        )
        modified.extend(
            f"{relative}#{section['section']}" for section in item["modified_sections"]
        )
        added.extend(
            f"{relative}#{section['section']}" for section in item["added_sections"]
        )
    if parse_errors:
        raise DesignerIntegrationError(
            f"Designer 生成了无法解析的 USER 区: {parse_errors}; 差异: {diff_path}"
        )
    if removed:
        raise DesignerIntegrationError(
            f"Designer 删除了已有 USER 区: {removed}; 差异: {diff_path}"
        )
    if modified:
        raise DesignerIntegrationError(
            f"Designer 修改了已有 USER 区: {modified}; 差异: {diff_path}"
        )
    has_binding_mutation = any(call.tool_name.startswith("binding_") for call in plan.native_calls)
    if added and not has_binding_mutation:
        raise DesignerIntegrationError(
            f"Designer 新增了未由本次 binding 声明的 USER 区: {added}; 差异: {diff_path}"
        )
    patch = plan.vm_user_patch
    if patch is not None and f"{patch.file_path}#{patch.user_section}" in added:
        call_index = patch.declared_by_native_call
        if (
            call_index is None
            or call_index < 0
            or call_index >= len(plan.native_calls)
            or not plan.native_calls[call_index].tool_name.startswith("binding_")
        ):
            raise DesignerIntegrationError(
                "Agent 要修改的新增 USER 区没有对应的 Designer binding 声明: "
                f"{patch.file_path}#{patch.user_section}; 差异: {diff_path}"
            )


def _verify_agent_user_patch(
    manifest_path: Path,
    payload: dict[str, Any],
    diff_path: Path,
    patch: VmUserPatch | None,
) -> None:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    stages = manifest.get("stages") or {}
    codegen_hashes = (stages.get("after_codegen") or {}).get("hashes") or {}
    agent_hashes = (stages.get("after_agent") or {}).get("hashes") or {}
    changed_files = sorted(
        relative
        for relative in set(codegen_hashes) | set(agent_hashes)
        if codegen_hashes.get(relative) != agent_hashes.get(relative)
    )
    expected_files = [] if patch is None else [patch.file_path]
    if changed_files != expected_files:
        raise DesignerIntegrationError(
            f"Agent USER patch 修改范围不正确: 期望 {expected_files}, 实际 {changed_files}; 差异: {diff_path}"
        )
    changed_sections: list[str] = []
    structural_changes: list[str] = []
    parse_errors: list[str] = []
    for item in payload["files"]:
        relative = str(item["path"])
        for key in ("before_parse_error", "after_parse_error"):
            if item.get(key):
                parse_errors.append(f"{relative}: {item[key]}")
        structural_changes.extend(
            f"{relative}#{section['section']}"
            for key in ("added_sections", "removed_sections")
            for section in item[key]
        )
        changed_sections.extend(
            f"{relative}#{section['section']}" for section in item["modified_sections"]
        )
    if parse_errors or structural_changes:
        raise DesignerIntegrationError(
            f"Agent USER patch 改变了 USER 区边界: {parse_errors + structural_changes}; 差异: {diff_path}"
        )
    expected_sections = [] if patch is None else [f"{patch.file_path}#{patch.user_section}"]
    if changed_sections != expected_sections:
        raise DesignerIntegrationError(
            f"Agent USER patch 修改区段不正确: 期望 {expected_sections}, 实际 {changed_sections}; 差异: {diff_path}"
        )
    if patch is not None:
        codegen_file = Path(stages["after_codegen"]["snapshot_dir"]) / patch.file_path
        agent_file = Path(stages["after_agent"]["snapshot_dir"]) / patch.file_path
        expected = _patched_user_content(codegen_file.read_bytes(), patch)
        if not agent_file.is_file() or agent_file.read_bytes() != expected:
            raise DesignerIntegrationError(
                f"Agent USER patch 不等于计划中的 before/after: {patch.file_path}#{patch.user_section}; 差异: {diff_path}"
            )


def _current_transaction_changes(manifest_path: Path) -> list[str]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    source_root = Path(manifest["source_root"])
    current = _managed_hashes(source_root, manifest["managed_relative_paths"])
    before = manifest["before"]
    return sorted(
        relative
        for relative in set(before) | set(current)
        if before.get(relative) != current.get(relative)
    )


def _require_isolated_path(value: str, *, label: str) -> Path:
    source_root = resolve_source_root()
    path = Path(value).resolve()
    try:
        path.relative_to(source_root)
    except ValueError as exc:
        raise DesignerIntegrationError(f"{label} 越出隔离工作区: {path}") from exc
    return path


def _designer_transaction_scope(
    plan: DesignerPlan, project_path: Path
) -> tuple[list[Path], set[str], set[str]]:
    """为会写布局目录之外的原生工具计算可回滚范围。"""
    source_root = resolve_source_root()
    extra_paths: list[Path] = []
    allowed_files: set[str] = set()
    allowed_roots: set[str] = set()

    def add_path(path: Path, *, root: bool) -> None:
        resolved = _require_isolated_path(str(path), label="Designer 写入路径")
        relative = resolved.relative_to(source_root).as_posix()
        extra_paths.append(resolved)
        (allowed_roots if root else allowed_files).add(relative)

    def image_root() -> Path:
        local_settings = project_path / "setting.local.json"
        settings = json.loads(local_settings.read_text(encoding="utf-8"))
        image_root_value = str(settings.get("ImagePath") or "").strip()
        if not image_root_value:
            raise DesignerIntegrationError("Designer ImagePath 未配置")
        return _require_isolated_path(image_root_value, label="Designer ImagePath")

    def image_target(root: Path, value: Any, *, label: str) -> Path:
        relative = Path(str(value or ""))
        if not str(value or "").strip() or relative.is_absolute():
            raise DesignerIntegrationError(f"{label} 必须是图片根目录内的相对路径")
        target = (root / relative).resolve()
        try:
            target.relative_to(root)
        except ValueError as exc:
            raise DesignerIntegrationError(f"{label} 越出图片根目录") from exc
        return target

    for call in plan.native_calls:
        name = call.tool_name
        args = call.arguments
        for key in (
            "outputDirectory",
            "outputPath",
            "headerFilePath",
            "binaryFilePath",
            "fontPath",
        ):
            value = args.get(key)
            if value:
                _require_isolated_path(str(value), label=f"{name}.{key}")
        if name == "project_settings_update":
            for key, value in (args.get("properties") or {}).items():
                if "path" in str(key).casefold() and value and Path(str(value)).is_absolute():
                    _require_isolated_path(str(value), label=f"{name}.{key}")
            add_path(project_path, root=True)
        elif name.startswith("module_"):
            output = args.get("outputPath")
            if output:
                add_path(Path(str(output)), root=True)
            else:
                add_path(source_root / "app" / "comm", root=True)
            add_path(project_path, root=True)
        elif name.startswith("preset_"):
            add_path(project_path, root=True)
        elif name == "resource_image_import":
            root = image_root()
            screen_name = str(args.get("screenName") or "").strip()
            if not screen_name or Path(screen_name).name != screen_name:
                raise DesignerIntegrationError("resource_image_import.screenName 非法")
            add_path(root / screen_name, root=True)
        elif name in {"resource_image_replace", "resource_image_delete"}:
            root = image_root()
            add_path(
                image_target(root, args.get("imagePath"), label=f"{name}.imagePath"),
                root=False,
            )
        elif name == "resource_image_move":
            root = image_root()
            for key in ("sourceImagePath", "destinationImagePath"):
                add_path(
                    image_target(root, args.get(key), label=f"{name}.{key}"),
                    root=False,
                )
        elif name == "resource_font_import":
            output = args.get("outputDirectory")
            if not output:
                raise DesignerIntegrationError(
                    "resource_font_import 必须显式提供隔离工作区内 outputDirectory"
                )
            add_path(Path(str(output)), root=True)
        elif name == "resource_font_delete":
            font_path = args.get("fontPath")
            if not font_path:
                raise DesignerIntegrationError(
                    "resource_font_delete 必须显式提供隔离工作区内 fontPath"
                )
            add_path(Path(str(font_path)), root=False)
        elif name == "resource_generate":
            output = args.get("outputDirectory")
            if not output:
                raise DesignerIntegrationError(
                    "resource_generate 必须显式提供隔离工作区内 outputDirectory"
                )
            add_path(Path(str(output)), root=True)
        elif name == "translation_generate":
            header = args.get("headerFilePath")
            binary = args.get("binaryFilePath")
            if not header or not binary:
                raise DesignerIntegrationError(
                    "translation_generate 必须显式提供隔离工作区内输出文件"
                )
            header_path = Path(str(header))
            add_path(header_path, root=False)
            add_path(header_path.with_suffix(".c"), root=False)
            add_path(Path(str(binary)), root=False)
    return extra_paths, allowed_files, allowed_roots


def _resolve_native_refs(value: Any, refs: dict[str, str]) -> Any:
    if isinstance(value, str) and value.startswith("$"):
        ref_name = value[1:]
        if ref_name not in refs:
            raise DesignerIntegrationError(f"Designer native_call 引用了未知控件: {value}")
        return refs[ref_name]
    if isinstance(value, list):
        return [_resolve_native_refs(item, refs) for item in value]
    if isinstance(value, dict):
        return {key: _resolve_native_refs(item, refs) for key, item in value.items()}
    return value


def _patch_text_newlines(value: str, body: str) -> str:
    normalized = _normalize_user_body(value)
    newline = "\r\n" if "\r\n" in body else "\n"
    return normalized.replace("\n", newline)


def _patched_user_content(content: bytes, patch: VmUserPatch) -> bytes:
    text = content.decode("utf-8", "replace")
    matching = [
        item for item in _user_section_spans(content) if item["section"] == patch.user_section
    ]
    if len(matching) != 1:
        raise DesignerIntegrationError(f"VM USER 区标记不唯一: {patch.user_section}")
    section = matching[0]
    body = str(section["body"])
    before = _patch_text_newlines(patch.before, body)
    after = _patch_text_newlines(patch.after, body)
    if body.count(before) != 1:
        raise DesignerIntegrationError(
            f"VM USER patch 的 before 不在声明区段内或不唯一: {patch.user_section}"
        )
    updated_body = body.replace(before, after, 1)
    updated = (
        text[: int(section["body_start"])]
        + updated_body
        + text[int(section["body_end"]):]
    )
    return updated.encode("utf-8")


def _apply_vm_user_patch(layout_dir: Path, page_name: str, patch: VmUserPatch) -> str:
    source_root = resolve_source_root()
    target = (source_root / patch.file_path).resolve()
    layout_dir = layout_dir.resolve()
    expected_stem = f"gui_win_{page_name.casefold()}_vm"
    if target.parent != layout_dir or target.stem != expected_stem or target.suffix not in {".c", ".h"}:
        raise DesignerIntegrationError(
            f"源码 Agent 只能修改当前页面 VM 文件: {patch.file_path}"
        )
    updated = _patched_user_content(target.read_bytes(), patch)
    tmp = target.with_name(f".{target.name}.{os.getpid()}.user.tmp")
    tmp.write_bytes(updated)
    os.replace(tmp, target)
    return target.relative_to(source_root).as_posix()


def apply_designer_plan(
    *, task_id: str, plan: DesignerPlan, evidence_root: Path
) -> DesignerApplyResult:
    project_path = designer_project_path()
    manifest_path: Path | None = None
    try:
        with DesignerMcpSession(project_path) as client:
            status = client.call("designer_status", {})
            if not status.get("available") or not status.get("ready"):
                raise DesignerIntegrationError(f"Designer 未就绪: {status}")
            opened = client.call("designer_open_project", {"uiLayoutType": "Main"})
            ui_layout_name = str(opened.get("uiLayoutName") or "")
            layout_dir = (project_path.parent.parent / "windows" / ui_layout_name).resolve()
            if not layout_dir.is_dir():
                raise DesignerIntegrationError(f"Designer layout 目录不存在: {layout_dir}")
            extra_paths, extra_allowed_files, extra_allowed_roots = (
                _designer_transaction_scope(plan, project_path)
            )
            manifest_path = begin_designer_transaction(
                task_id=task_id,
                layout_dir=layout_dir,
                evidence_root=evidence_root,
                extra_paths=extra_paths,
            )
            applied: dict[str, Any] = {"applied": 0, "saved": False, "refs": {}, "results": []}
            if plan.layout_operations:
                applied = client.call(
                    "designer_apply_layout",
                    {
                        "pageName": plan.page_name,
                        "operations": plan.layout_operations,
                        "save": False,
                    },
                )
            refs = {str(key): str(value) for key, value in (applied.get("refs") or {}).items()}
            native_results: list[dict[str, Any]] = []
            for call in plan.native_calls:
                arguments = _resolve_native_refs(call.arguments, refs)
                result = client.call(
                    "designer_native_call",
                    {"toolName": call.tool_name, "arguments": arguments},
                )
                native_results.append({"tool_name": call.tool_name, "result": result})
            module_targets = _module_mutation_names(plan)
            if module_targets:
                capability = (status.get("capabilityGroups") or {}).get(
                    "common_module_codegen"
                ) or {}
                if not capability.get("ready"):
                    raise DesignerIntegrationError("Designer Adapter 的 CommonModule 生成能力不完整")
                for module_name in module_targets:
                    arguments = {"moduleName": module_name}
                    module_preview = client.call(
                        "designer_native_call",
                        {
                            "toolName": "module_codegen_preview",
                            "arguments": arguments,
                        },
                    )
                    if not _codegen_result_verified(module_preview, project=False):
                        raise DesignerIntegrationError(
                            f"CommonModule preview 文件验证失败: {module_name or 'ALL'}"
                        )
                    native_results.append(
                        {
                            "tool_name": "module_codegen_preview",
                            "result": module_preview,
                        }
                    )
            client.call(
                "designer_get_page",
                {"pageName": plan.page_name, "includeBindings": True, "includeProperties": False},
            )
            preview = client.call(
                "designer_generate_code",
                {
                    "pageName": plan.page_name,
                    "mode": "preview",
                    "includePreviews": False,
                    "includeDiff": True,
                },
            )
            preview_files = preview.get("freshPreviewComparison") or []
            if not preview.get("preview", {}).get("filesVerified") or not preview_files:
                raise DesignerIntegrationError("Designer preview 文件验证失败")
            relative_files = {
                str(item.get("relativePath")) for item in preview_files if item.get("relativePath")
            }
            page_json = f"gui_win_{plan.page_name.casefold()}.json"
            layout_relative = layout_dir.relative_to(resolve_source_root()).as_posix()
            generated_files = {
                f"{layout_relative}/{relative}" for relative in relative_files
            }
            allowed_files = generated_files | {f"{layout_relative}/{page_json}"}
            allowed_files |= extra_allowed_files
            for module_name in module_targets:
                arguments = {"moduleName": module_name}
                module_generated = client.call(
                    "designer_native_call",
                    {
                        "toolName": "module_codegen_generate_to_project",
                        "arguments": arguments,
                    },
                )
                if not _codegen_result_verified(module_generated, project=True):
                    raise DesignerIntegrationError(
                        f"CommonModule 项目生成文件验证失败: {module_name or 'ALL'}"
                    )
                native_results.append(
                    {
                        "tool_name": "module_codegen_generate_to_project",
                        "result": module_generated,
                    }
                )
            generated = client.call(
                "designer_generate_code",
                {
                    "pageName": plan.page_name,
                    "mode": "project",
                    "save": True,
                    "includePreviews": False,
                    "includeDiff": True,
                },
            )
            if not generated.get("projectFilesVerified"):
                raise DesignerIntegrationError("Designer 项目生成文件验证失败")
            codegen_changed = set(_current_transaction_changes(manifest_path))
            user_evidence_files = set(codegen_changed)
            if plan.vm_user_patch is not None:
                user_evidence_files.add(plan.vm_user_patch.file_path)
            codegen_payload, codegen_diff_path = _capture_user_stage(
                manifest_path,
                stage="after_codegen",
                baseline_dir=Path(
                    json.loads(manifest_path.read_text(encoding="utf-8"))["backup_dir"]
                ),
                relative_files=user_evidence_files,
                diff_filename="user_sections_codegen_diff.json",
            )
            _verify_designer_user_sections(codegen_payload, codegen_diff_path, plan)
            vm_user_patch_path = None
            if plan.vm_user_patch is not None:
                vm_user_patch_path = _apply_vm_user_patch(
                    layout_dir, plan.page_name, plan.vm_user_patch
                )
            agent_payload, agent_diff_path = _capture_user_stage(
                manifest_path,
                stage="after_agent",
                baseline_dir=manifest_path.parent / "after_codegen",
                relative_files=user_evidence_files,
                diff_filename="user_sections_agent_diff.json",
            )
            _verify_agent_user_patch(
                manifest_path,
                agent_payload,
                agent_diff_path,
                plan.vm_user_patch,
            )
            changed = finalize_designer_transaction(
                manifest_path, allowed_files, extra_allowed_roots
            )
            return DesignerApplyResult(
                success=True,
                page_json_path=(layout_dir / page_json).relative_to(resolve_source_root()).as_posix(),
                changed_files=changed,
                transaction_manifest=str(manifest_path),
                apply_result=applied,
                native_results=native_results,
                vm_user_patch_path=vm_user_patch_path,
                preview_result=preview,
                generation_result=generated,
            )
    except Exception as exc:
        rollback_error = ""
        if manifest_path and manifest_path.is_file():
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                if not manifest.get("after"):
                    source_root = Path(manifest["source_root"])
                    manifest["after"] = _managed_hashes(
                        source_root, manifest["managed_relative_paths"]
                    )
                    manifest["changed_files"] = sorted(
                        item
                        for item in set(manifest["before"]) | set(manifest["after"])
                        if manifest["before"].get(item) != manifest["after"].get(item)
                    )
                    _write_json(manifest_path, manifest)
                rollback_designer_transaction(manifest_path)
            except Exception as rollback_exc:
                rollback_error = f"；自动回滚失败: {rollback_exc}"
        return DesignerApplyResult(
            success=False,
            error=f"{exc}{rollback_error}",
            transaction_manifest=str(manifest_path) if manifest_path else None,
        )
    model_config = ConfigDict(extra="forbid")
