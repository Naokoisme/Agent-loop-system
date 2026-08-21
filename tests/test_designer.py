from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pydantic import ValidationError

from agent_loop_system.tools.designer import (
    DesignerContext,
    DesignerIntegrationError,
    DesignerNativeCall,
    DesignerPlan,
    VmUserPatch,
    _apply_vm_user_patch,
    _capture_user_stage,
    _designer_transaction_scope,
    _verify_agent_user_patch,
    _verify_designer_user_sections,
    begin_designer_transaction,
    finalize_designer_transaction,
    page_name_from_trace,
    rollback_designer_transaction,
    validate_designer_plan,
)


class DesignerContextTest(unittest.TestCase):
    def test_final_popup_or_window_selects_page(self) -> None:
        trace = {
            "steps": [
                {"window_name": "DIAL"},
                {"window_name": "SIDEBAR", "popup_name": "ACTIVE_GOAL"},
            ]
        }
        self.assertEqual(page_name_from_trace(trace), "ACTIVE_GOAL")

    def test_plan_must_use_actual_page_and_allowed_operations(self) -> None:
        context = DesignerContext(
            project_path="D:/isolated/app/projects/P",
            ui_layout_name="layout",
            page_name="SIDEBAR",
            page={},
        )
        validate_designer_plan(
            DesignerPlan(
                root_cause_analysis="layout",
                page_name="SIDEBAR",
                layout_operations=[
                    {"op": "resize", "widgetId": "1", "width": 20, "height": 20}
                ],
                reason="resize",
            ),
            context,
        )
        with self.assertRaises(DesignerIntegrationError):
            validate_designer_plan(
                DesignerPlan(
                    root_cause_analysis="layout",
                    page_name="OTHER",
                    layout_operations=[{"op": "resize"}],
                    reason="bad page",
                ),
                context,
            )

    def test_legacy_designer_choice_fields_are_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            DesignerPlan(
                root_cause_analysis="legacy",
                page_name="SIDEBAR",
                reason="legacy",
                use_designer=False,
                operations=[],
            )

    def test_missing_capability_can_fail_closed_without_fake_change(self) -> None:
        context = DesignerContext(
            project_path="D:/isolated/app/projects/P",
            ui_layout_name="layout",
            page_name="SIDEBAR",
            page={},
            capability_groups={
                "translation_entries": {
                    "ready": False,
                    "missingTools": ["translation_get"],
                }
            },
        )
        validate_designer_plan(
            DesignerPlan(
                root_cause_analysis="必须修改翻译条目",
                page_name="SIDEBAR",
                blocked_capability="translation_entries",
                reason="Designer 未暴露翻译条目接口",
            ),
            context,
        )

    def test_ready_capability_cannot_be_used_as_blocker(self) -> None:
        context = DesignerContext(
            project_path="D:/isolated/app/projects/P",
            ui_layout_name="layout",
            page_name="SIDEBAR",
            page={},
            capability_groups={"translation_entries": {"ready": True}},
        )
        with self.assertRaises(DesignerIntegrationError):
            validate_designer_plan(
                DesignerPlan(
                    root_cause_analysis="invalid blocker",
                    page_name="SIDEBAR",
                    blocked_capability="translation_entries",
                    reason="invalid",
                ),
                context,
            )


class DesignerTransactionTest(unittest.TestCase):
    def test_multi_file_transaction_rolls_back_exact_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            layout = root / "app" / "windows" / "layout"
            layout.mkdir(parents=True)
            (layout / "page.json").write_text("before-json", encoding="utf-8")
            (layout / "page_vm.c").write_text(
                "generated\n//@USER_BEGIN\nkeep();\n//@USER_END\n", encoding="utf-8"
            )
            deleted = layout / "old.h"
            deleted.write_text("old", encoding="utf-8")
            evidence = root / "evidence"
            evidence.mkdir()

            with mock.patch.dict("os.environ", {"W30_SOURCE_ROOT": str(root)}):
                manifest = begin_designer_transaction(
                    task_id="T1", layout_dir=layout, evidence_root=evidence
                )
                (layout / "page.json").write_text("after-json", encoding="utf-8")
                (layout / "page_vm.c").write_text(
                    "new-generated\n//@USER_BEGIN\nkeep();\n//@USER_END\n", encoding="utf-8"
                )
                deleted.unlink()
                (layout / "new.h").write_text("new", encoding="utf-8")
                changed = finalize_designer_transaction(
                    manifest,
                    {
                        "app/windows/layout/page.json",
                        "app/windows/layout/page_vm.c",
                        "app/windows/layout/old.h",
                        "app/windows/layout/new.h",
                    },
                )
                self.assertEqual(
                    changed,
                    [
                        "app/windows/layout/new.h",
                        "app/windows/layout/old.h",
                        "app/windows/layout/page.json",
                        "app/windows/layout/page_vm.c",
                    ],
                )
                rollback_designer_transaction(manifest)

            self.assertEqual((layout / "page.json").read_text(encoding="utf-8"), "before-json")
            self.assertTrue(deleted.is_file())
            self.assertFalse((layout / "new.h").exists())

    def test_unexpected_file_is_rejected_and_manifest_remains_rollbackable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            layout = root / "app" / "windows" / "layout"
            layout.mkdir(parents=True)
            target = layout / "page.json"
            target.write_text("before", encoding="utf-8")
            evidence = root / "evidence"
            evidence.mkdir()
            with mock.patch.dict("os.environ", {"W30_SOURCE_ROOT": str(root)}):
                manifest = begin_designer_transaction(
                    task_id="T1", layout_dir=layout, evidence_root=evidence
                )
                target.write_text("after", encoding="utf-8")
                with self.assertRaises(DesignerIntegrationError):
                    finalize_designer_transaction(manifest, {"another.json"})
                payload = json.loads(manifest.read_text(encoding="utf-8"))
                self.assertEqual(
                    payload["changed_files"], ["app/windows/layout/page.json"]
                )
                rollback_designer_transaction(manifest)
            self.assertEqual(target.read_text(encoding="utf-8"), "before")

    def test_binding_may_add_user_section_then_agent_may_patch_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            layout = root / "app" / "windows" / "layout"
            layout.mkdir(parents=True)
            relative = "app/windows/layout/gui_win_sidebar_vm.c"
            vm = root / relative
            vm.write_text(
                "generated_before();\n"
                "//@USER_BEGIN:Keep\nkeep();\n//@USER_END\n",
                encoding="utf-8",
            )
            evidence = root / "evidence"
            evidence.mkdir()
            patch = VmUserPatch(
                file_path=relative,
                user_section="NewHandler",
                declared_by_native_call=0,
                before="todo();",
                after="safe();",
                reason="custom validation",
            )
            plan = DesignerPlan(
                root_cause_analysis="binding needs custom validation",
                page_name="SIDEBAR",
                native_calls=[
                    DesignerNativeCall(
                        tool_name="binding_module_property_bind", arguments={}
                    )
                ],
                vm_user_patch=patch,
                reason="bind and patch",
            )
            with mock.patch.dict("os.environ", {"W30_SOURCE_ROOT": str(root)}):
                manifest = begin_designer_transaction(
                    task_id="T1", layout_dir=layout, evidence_root=evidence
                )
                vm.write_text(
                    "generated_after();\n"
                    "//@USER_BEGIN:Keep\nkeep();\n//@USER_END\n"
                    "//@USER_BEGIN:NewHandler\ntodo();\n//@USER_END\n",
                    encoding="utf-8",
                )
                codegen_payload, codegen_diff = _capture_user_stage(
                    manifest,
                    stage="after_codegen",
                    baseline_dir=manifest.parent / "before",
                    relative_files={relative},
                    diff_filename="user_sections_codegen_diff.json",
                )
                _verify_designer_user_sections(codegen_payload, codegen_diff, plan)
                _apply_vm_user_patch(layout, "SIDEBAR", patch)
                agent_payload, agent_diff = _capture_user_stage(
                    manifest,
                    stage="after_agent",
                    baseline_dir=manifest.parent / "after_codegen",
                    relative_files={relative},
                    diff_filename="user_sections_agent_diff.json",
                )
                _verify_agent_user_patch(manifest, agent_payload, agent_diff, patch)

            self.assertIn("safe();", vm.read_text(encoding="utf-8"))
            self.assertTrue((manifest.parent / "after_codegen" / relative).is_file())
            diff = json.loads(codegen_diff.read_text(encoding="utf-8"))
            self.assertEqual(
                diff["files"][0]["added_sections"][0]["section"], "NewHandler"
            )

    def test_designer_cannot_modify_existing_user_section_and_keeps_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            layout = root / "app" / "windows" / "layout"
            layout.mkdir(parents=True)
            relative = "app/windows/layout/gui_win_sidebar_vm.c"
            vm = root / relative
            vm.write_text(
                "generated();\n//@USER_BEGIN:Handler\nkeep();\n//@USER_END\n",
                encoding="utf-8",
            )
            evidence = root / "evidence"
            evidence.mkdir()
            plan = DesignerPlan(
                root_cause_analysis="layout", page_name="SIDEBAR", reason="layout"
            )
            with mock.patch.dict("os.environ", {"W30_SOURCE_ROOT": str(root)}):
                manifest = begin_designer_transaction(
                    task_id="T1", layout_dir=layout, evidence_root=evidence
                )
                vm.write_text(
                    "generated();\n//@USER_BEGIN:Handler\nchanged();\n//@USER_END\n",
                    encoding="utf-8",
                )
                payload, diff_path = _capture_user_stage(
                    manifest,
                    stage="after_codegen",
                    baseline_dir=manifest.parent / "before",
                    relative_files={relative},
                    diff_filename="user_sections_codegen_diff.json",
                )
                with self.assertRaisesRegex(DesignerIntegrationError, "修改了已有 USER 区"):
                    _verify_designer_user_sections(payload, diff_path, plan)

            self.assertTrue((manifest.parent / "after_codegen" / relative).is_file())
            diff = json.loads(diff_path.read_text(encoding="utf-8"))
            self.assertEqual(
                diff["files"][0]["modified_sections"][0]["section"], "Handler"
            )

    def test_agent_may_patch_an_existing_user_section_after_codegen(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            layout = root / "app" / "windows" / "layout"
            layout.mkdir(parents=True)
            relative = "app/windows/layout/gui_win_sidebar_vm.c"
            vm = root / relative
            vm.write_text(
                "generated();\n//@USER_BEGIN:Handler\nwrong();\n//@USER_END\n",
                encoding="utf-8",
            )
            evidence = root / "evidence"
            evidence.mkdir()
            patch = VmUserPatch(
                file_path=relative,
                user_section="Handler",
                before="wrong();",
                after="fixed();",
                reason="fix existing logic",
            )
            plan = DesignerPlan(
                root_cause_analysis="existing USER logic is wrong",
                page_name="SIDEBAR",
                vm_user_patch=patch,
                reason="patch existing USER section",
            )
            with mock.patch.dict("os.environ", {"W30_SOURCE_ROOT": str(root)}):
                manifest = begin_designer_transaction(
                    task_id="T1", layout_dir=layout, evidence_root=evidence
                )
                codegen_payload, codegen_diff = _capture_user_stage(
                    manifest,
                    stage="after_codegen",
                    baseline_dir=manifest.parent / "before",
                    relative_files={relative},
                    diff_filename="user_sections_codegen_diff.json",
                )
                _verify_designer_user_sections(codegen_payload, codegen_diff, plan)
                _apply_vm_user_patch(layout, "SIDEBAR", patch)
                agent_payload, agent_diff = _capture_user_stage(
                    manifest,
                    stage="after_agent",
                    baseline_dir=manifest.parent / "after_codegen",
                    relative_files={relative},
                    diff_filename="user_sections_agent_diff.json",
                )
                _verify_agent_user_patch(manifest, agent_payload, agent_diff, patch)

            self.assertIn("fixed();", vm.read_text(encoding="utf-8"))

    def test_agent_patch_may_only_change_its_named_user_section(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            layout = root / "app" / "windows" / "layout"
            layout.mkdir(parents=True)
            relative = "app/windows/layout/gui_win_sidebar_vm.c"
            vm = root / relative
            generated = (
                "generated();\n"
                "//@USER_BEGIN:Handler\nold();\n//@USER_END\n"
                "//@USER_BEGIN:Other\nkeep();\n//@USER_END\n"
            )
            vm.write_text(generated, encoding="utf-8")
            evidence = root / "evidence"
            evidence.mkdir()
            patch = VmUserPatch(
                file_path=relative,
                user_section="Handler",
                before="old();",
                after="new();",
                reason="fix handler",
            )
            with mock.patch.dict("os.environ", {"W30_SOURCE_ROOT": str(root)}):
                manifest = begin_designer_transaction(
                    task_id="T1", layout_dir=layout, evidence_root=evidence
                )
                _capture_user_stage(
                    manifest,
                    stage="after_codegen",
                    baseline_dir=manifest.parent / "before",
                    relative_files={relative},
                    diff_filename="user_sections_codegen_diff.json",
                )
                _apply_vm_user_patch(layout, "SIDEBAR", patch)
                vm.write_text(
                    vm.read_text(encoding="utf-8").replace("keep();", "also_changed();"),
                    encoding="utf-8",
                )
                payload, diff_path = _capture_user_stage(
                    manifest,
                    stage="after_agent",
                    baseline_dir=manifest.parent / "after_codegen",
                    relative_files={relative},
                    diff_filename="user_sections_agent_diff.json",
                )
                with self.assertRaisesRegex(DesignerIntegrationError, "修改区段不正确"):
                    _verify_agent_user_patch(manifest, payload, diff_path, patch)

    def test_resource_image_target_is_included_in_isolated_transaction(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "app" / "projects" / "P"
            image_root = root / "designer_assets"
            project.mkdir(parents=True)
            image_root.mkdir()
            (project / "setting.local.json").write_text(
                json.dumps({"ImagePath": str(image_root)}), encoding="utf-8"
            )
            plan = DesignerPlan(
                root_cause_analysis="replace image",
                page_name="SIDEBAR",
                native_calls=[
                    DesignerNativeCall(
                        tool_name="resource_image_replace",
                        arguments={"imagePath": "SIDEBAR/icon.png", "sourcePath": "x"},
                    )
                ],
                reason="replace",
            )
            with mock.patch.dict("os.environ", {"W30_SOURCE_ROOT": str(root)}):
                paths, files, roots = _designer_transaction_scope(plan, project)
            self.assertEqual(paths, [(image_root / "SIDEBAR" / "icon.png").resolve()])
            self.assertEqual(files, {"designer_assets/SIDEBAR/icon.png"})
            self.assertEqual(roots, set())

    def test_external_designer_image_root_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as external:
            root = Path(tmp)
            project = root / "app" / "projects" / "P"
            project.mkdir(parents=True)
            (project / "setting.local.json").write_text(
                json.dumps({"ImagePath": external}), encoding="utf-8"
            )
            plan = DesignerPlan(
                root_cause_analysis="delete image",
                page_name="SIDEBAR",
                native_calls=[
                    DesignerNativeCall(
                        tool_name="resource_image_delete",
                        arguments={"imagePath": "SIDEBAR/icon.png"},
                    )
                ],
                reason="delete",
            )
            with mock.patch.dict("os.environ", {"W30_SOURCE_ROOT": str(root)}):
                with self.assertRaisesRegex(DesignerIntegrationError, "越出隔离工作区"):
                    _designer_transaction_scope(plan, project)

    def test_vm_patch_is_limited_to_named_user_section(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            layout = root / "app" / "windows" / "layout"
            layout.mkdir(parents=True)
            vm = layout / "gui_win_sidebar_vm.c"
            vm.write_text(
                "generated();\n//@USER_BEGIN:Handler\nold();\n//@USER_END\n",
                encoding="utf-8",
            )
            patch = VmUserPatch(
                file_path="app/windows/layout/gui_win_sidebar_vm.c",
                user_section="Handler",
                before="old();",
                after="new();",
                reason="custom handler",
            )
            with mock.patch.dict("os.environ", {"W30_SOURCE_ROOT": str(root)}):
                changed = _apply_vm_user_patch(layout, "SIDEBAR", patch)
            self.assertEqual(changed, patch.file_path)
            self.assertIn("new();", vm.read_text(encoding="utf-8"))

            patch.file_path = "app/windows/layout/gui_win_sidebar_v.c"
            with mock.patch.dict("os.environ", {"W30_SOURCE_ROOT": str(root)}):
                with self.assertRaises(DesignerIntegrationError):
                    _apply_vm_user_patch(layout, "SIDEBAR", patch)

    def test_vm_patch_cannot_touch_non_user_generated_code(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            layout = root / "app" / "windows" / "layout"
            layout.mkdir(parents=True)
            vm = layout / "gui_win_sidebar_vm.c"
            vm.write_text(
                "generated();\n//@USER_BEGIN:Handler\nkeep();\n//@USER_END\n",
                encoding="utf-8",
            )
            patch = VmUserPatch(
                file_path="app/windows/layout/gui_win_sidebar_vm.c",
                user_section="Handler",
                before="generated();",
                after="changed();",
                reason="forbidden",
            )
            with mock.patch.dict("os.environ", {"W30_SOURCE_ROOT": str(root)}):
                with self.assertRaises(DesignerIntegrationError):
                    _apply_vm_user_patch(layout, "SIDEBAR", patch)
            self.assertIn("generated();", vm.read_text(encoding="utf-8"))

    def test_native_call_must_use_live_schema(self) -> None:
        context = DesignerContext(
            project_path="D:/isolated/app/projects/P",
            ui_layout_name="layout",
            page_name="SIDEBAR",
            page={},
            native_tool_schemas={
                "binding_event_bind": {
                    "properties": {
                        "sessionId": {},
                        "pageName": {},
                        "widgetId": {},
                        "eventType": {},
                    },
                    "required": ["sessionId", "pageName", "widgetId", "eventType"],
                }
            },
        )
        plan = DesignerPlan(
            root_cause_analysis="event",
            page_name="SIDEBAR",
            native_calls=[
                DesignerNativeCall(
                    tool_name="binding_event_bind",
                    arguments={
                        "pageName": "SIDEBAR",
                        "widgetId": "1",
                        "eventType": "Clicked",
                    },
                )
            ],
            reason="bind click",
        )
        validate_designer_plan(plan, context)
        plan.native_calls[0].arguments["sessionId"] = "forbidden"
        with self.assertRaises(DesignerIntegrationError):
            validate_designer_plan(plan, context)

    def test_action_list_requires_catalog_and_schema_capability(self) -> None:
        schema = {
            "properties": {
                "sessionId": {},
                "pageName": {},
                "widgetId": {},
                "eventType": {},
                "handlerName": {},
                "isActionList": {},
                "actionsJson": {},
            },
            "required": [
                "sessionId",
                "pageName",
                "widgetId",
                "eventType",
                "handlerName",
                "isActionList",
                "actionsJson",
            ],
        }
        context = DesignerContext(
            project_path="D:/isolated/app/projects/P",
            ui_layout_name="layout",
            page_name="SIDEBAR",
            page={},
            native_tool_schemas={"binding_event_bind": schema},
            capability_groups={
                "action_list": {
                    "ready": False,
                    "missingTools": ["action_catalog", "action_schema"],
                }
            },
        )
        plan = DesignerPlan(
            root_cause_analysis="declarative navigation",
            page_name="SIDEBAR",
            native_calls=[
                DesignerNativeCall(
                    tool_name="binding_event_bind",
                    arguments={
                        "pageName": "SIDEBAR",
                        "widgetId": "1",
                        "eventType": "Clicked",
                        "handlerName": None,
                        "isActionList": True,
                        "actionsJson": '[{"Parameters":{"navPage":"DETAIL"}}]',
                    },
                )
            ],
            reason="bind action",
        )
        with self.assertRaisesRegex(DesignerIntegrationError, "动作目录"):
            validate_designer_plan(plan, context)

        context.capability_groups["action_list"]["ready"] = True
        validate_designer_plan(plan, context)

    def test_common_module_mutation_requires_safe_codegen_capability(self) -> None:
        schema = {
            "properties": {
                "sessionId": {},
                "moduleName": {},
                "properties": {},
            },
            "required": ["sessionId", "moduleName", "properties"],
        }
        context = DesignerContext(
            project_path="D:/isolated/app/projects/P",
            ui_layout_name="layout",
            page_name="SIDEBAR",
            page={},
            native_tool_schemas={"module_update": schema},
            capability_groups={
                "common_module_codegen": {
                    "ready": False,
                    "missingTools": ["module_codegen_preview"],
                }
            },
        )
        plan = DesignerPlan(
            root_cause_analysis="module data definition",
            page_name="SIDEBAR",
            native_calls=[
                DesignerNativeCall(
                    tool_name="module_update",
                    arguments={
                        "moduleName": "background_task",
                        "properties": {"Description": "updated"},
                    },
                )
            ],
            reason="update module",
        )
        with self.assertRaisesRegex(DesignerIntegrationError, "generate_to_project"):
            validate_designer_plan(plan, context)

        context.capability_groups["common_module_codegen"]["ready"] = True
        validate_designer_plan(plan, context)

    def test_resource_mutation_rejects_nonisolated_designer_root_during_plan(self) -> None:
        schema = {
            "properties": {
                "sessionId": {},
                "screenName": {},
                "sourcePath": {},
            },
            "required": ["sessionId", "screenName", "sourcePath"],
        }
        context = DesignerContext(
            project_path="D:/isolated/app/projects/P",
            ui_layout_name="layout",
            page_name="SIDEBAR",
            page={},
            native_tool_schemas={"resource_image_import": schema},
            capability_groups={
                "resources": {
                    "ready": False,
                    "isolationReady": False,
                    "isolationIssue": "ImagePath 越出隔离工作区",
                }
            },
        )
        plan = DesignerPlan(
            root_cause_analysis="new image",
            page_name="SIDEBAR",
            native_calls=[
                DesignerNativeCall(
                    tool_name="resource_image_import",
                    arguments={"screenName": "SIDEBAR", "sourcePath": "input.png"},
                )
            ],
            reason="import",
        )
        with self.assertRaisesRegex(DesignerIntegrationError, "越出隔离工作区"):
            validate_designer_plan(plan, context)


if __name__ == "__main__":
    unittest.main()
