from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from frontend.server import (
    AppPaths,
    CaseMapRepository,
    CaseTestManager,
    DefectRepository,
    FrontendHTTPServer,
    HistoryStore,
    JobManager,
    RequestHandler,
    TestHistoryStore,
    ThreadingHTTPServer,
    WebApplication,
    make_handler,
)


class FrontendDataTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.paths = AppPaths.from_root(root)
        for path in (
            self.paths.frontend,
            self.paths.defects / "100",
            self.paths.defect_images,
            self.paths.evidence / "100",
            self.paths.case_map / "620C_case_map",
            self.paths.case_map / "6202_case_map",
            self.paths.case_map / "6202_simulator_case_map",
        ):
            path.mkdir(parents=True, exist_ok=True)
        (self.paths.frontend / "index.html").write_text("<h1>正常</h1>", encoding="utf-8")

        self._write_defect(
            "100",
            title="计算器显示错误",
            description="复现描述：点击等号后显示异常",
            status="处理中",
        )
        (self.paths.case_map / "620C_case_map" / "计算器.json").write_text(
            json.dumps(
                [
                    {
                        "case_id": "CALC_001",
                        "sheet": "计算器",
                        "priority": "P0",
                        "precondition_text": "已进入计算器",
                        "steps_text": "点击等号",
                        "expected_text": "显示正确",
                        "verification_points": ["结果区域显示正确数值"],
                        "setup": ["srv_quick_cmd send TOP5STEP:ENTER_PAGE:CALCULATOR,0;"],
                        "actions": ["srv_quick_cmd send TOP5STEP:TP_CLICK:10,20,1;"],
                        "collect": ["srv_quick_cmd send TOP5STEP:GUI_TREE:1;"],
                        "unable": False,
                    },
                    {
                        "case_id": "CALC_002",
                        "sheet": "计算器",
                        "priority": "P1",
                        "precondition_text": "连接手机 App",
                        "steps_text": "从 App 下发数据",
                        "expected_text": "显示同步结果",
                        "setup": [],
                        "actions": [],
                        "collect": ["srv_quick_cmd send TOP5STEP:GUI_TREE:1;"],
                        "unable": True,
                        "note": "依赖手机 App",
                    }
                ],
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        (self.paths.case_map / "6202_case_map" / "计算器.json").write_text(
            json.dumps(
                [
                    {
                        "case_id": "CALC_001",
                        "sheet": "计算器",
                        "priority": "P0",
                        "precondition_text": "真机已进入计算器",
                        "steps_text": "在真机点击等号",
                        "expected_text": "真机显示正确",
                        "verification_points": ["真机结果区域显示正确数值"],
                        "setup": ["srv_quick_cmd send TOP5STEP:ENTER_PAGE:CALCULATOR,0;"],
                        "actions": ["srv_quick_cmd send TOP5STEP:TP_CLICK:10,20,1;"],
                        "collect": ["srv_quick_cmd send TOP5STEP:GUI_TREE:1;"],
                        "unable": False,
                    }
                ],
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        (self.paths.case_map / "6202_simulator_case_map" / "计算器.json").write_text(
            json.dumps(
                [{
                    "case_id": "CALC_001",
                    "sheet": "计算器",
                    "priority": "P0",
                    "precondition_text": "6202 模拟器已启动",
                    "steps_text": "在 6202 模拟器点击等号",
                    "expected_text": "6202 模拟器显示正确",
                    "verification_points": ["截图中结果区域显示正确数值"],
                    "setup": ["srv_quick_cmd send TOP5STEP:ENTER_PAGE:CALCULATOR,0;"],
                    "actions": ["srv_quick_cmd send TOP5STEP:TP_CLICK:10,20,1;"],
                    "collect": ["srv_quick_cmd send TOP5STEP:HOST_SCREENSHOT:1;"],
                    "unable": False,
                }],
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        (self.paths.evidence / "100" / "before.bmp").write_bytes(b"BM-before")
        (self.paths.evidence / "100" / "after.bmp").write_bytes(b"BM-after")
        self.history = HistoryStore(self.paths)
        self.defects = DefectRepository(self.paths, self.history)
        self.test_history = TestHistoryStore(self.paths)
        self.cases = CaseMapRepository(self.paths, self.test_history)

    def test_frontend_server_rejects_a_second_process_on_the_same_port(self) -> None:
        application = WebApplication(self.paths)
        first = FrontendHTTPServer(
            ("127.0.0.1", 0),
            make_handler(application),
        )
        self.addCleanup(first.server_close)

        with self.assertRaises(OSError):
            FrontendHTTPServer(
                ("127.0.0.1", first.server_address[1]),
                make_handler(application),
            )

    def _write_defect(
        self,
        number: str,
        *,
        title: str,
        description: str = "普通缺陷",
        status: str = "待处理",
        attachments: list[dict[str, object]] | None = None,
    ) -> None:
        defect_dir = self.paths.defects / number
        defect_dir.mkdir(parents=True, exist_ok=True)
        (defect_dir / "defect.json").write_text(
            json.dumps(
                {
                    "number": number,
                    "title": title,
                    "description": description,
                    "status": status,
                    "attachments": attachments or [],
                    "source_analysis": {
                        "matches": [
                            {
                                "path": "app/calculator.c",
                                "line_start": 10,
                                "line_end": 12,
                                "snippet": "old();",
                            }
                        ]
                    },
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    def _create_history(self, verdict: str = "CANNOT_VERIFY", defect: str = "100") -> str:
        return self.history.create(
            defect=defect,
            job={
                "started_at": "2026-08-08T10:00:00+08:00",
                "finished_at": "2026-08-08T10:01:00+08:00",
                "execution_mode": "agent_generated",
                "progress": {"nodes": {"validate": "pass", "test": "fail"}},
            },
            result={
                "verdict": verdict,
                "attempts": 1,
                "patch_retained": False,
                "patch": {
                    "file_path": "app/calculator.c",
                    "before": "old",
                    "after": "new",
                    "reason": "修复显示逻辑",
                    "test_commands": [":GUI_TREE:1"],
                },
                "test_output": {
                    "results": [
                        {
                            "verdict": verdict,
                            "reason": "模拟器未返回完整数据",
                            "test_commands": [":GUI_TREE:1", ":GET_CURRENT_WIN_ID"],
                            "terminal_json": [{"ok": False}],
                        }
                    ]
                },
            },
            stdout="done",
            stderr="",
        )

    def _write_history_record(
        self,
        run_id: str,
        run: dict[str, object],
        *,
        test_result: dict[str, object] | None = None,
    ) -> None:
        run_dir = self.paths.history / "100" / run_id
        run_dir.mkdir(parents=True)
        (run_dir / "run.json").write_text(
            json.dumps(run, ensure_ascii=False),
            encoding="utf-8",
        )
        if test_result is not None:
            (run_dir / "test_result.json").write_text(
                json.dumps(test_result, ensure_ascii=False),
                encoding="utf-8",
            )

    def _server(self) -> tuple[WebApplication, str]:
        application = WebApplication(self.paths)
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(application))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return application, f"http://127.0.0.1:{server.server_port}"

    @staticmethod
    def _post_json(url: str, payload: dict[str, object]):
        request = Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        return urlopen(request, timeout=3)

    def test_defect_detail_keeps_read_only_source_and_case_compatibility(self) -> None:
        detail = self.defects.get("100")
        self.assertIsNotNone(detail)
        self.assertEqual(detail["source_files"][0]["path"], "app/calculator.c")
        self.assertIn("1 处命中", detail["source_files"][0]["label"])
        self.assertEqual(detail["case_sheets"][0]["name"], "计算器")
        self.assertEqual(self.defects.cases("计算器")[0]["case_id"], "CALC_001")

    def test_case_map_list_filters_and_keeps_full_detail(self) -> None:
        payload = self.cases.list()
        self.assertEqual(payload["summary"], {
            "all": 2,
            "executable": 1,
            "unable": 1,
            "p0": 1,
            "p1": 1,
            "untested": 1,
            "fail": 0,
            "cannot_verify": 0,
            "pass": 0,
        })
        self.assertEqual([row["case_id"] for row in self.cases.list(state_filter="executable")["items"]], ["CALC_001"])
        self.assertEqual([row["case_id"] for row in self.cases.list(state_filter="unable")["items"]], ["CALC_002"])
        self.assertEqual(self.cases.list(state_filter="pass")["items"], [])
        self.assertEqual([row["case_id"] for row in self.cases.list(query="显示正确")["items"]], ["CALC_001"])
        detail = self.cases.get("计算器", "CALC_001")
        self.assertEqual(detail["precondition_text"], "已进入计算器")
        self.assertEqual(detail["actions"], ["srv_quick_cmd send TOP5STEP:TP_CLICK:10,20,1;"])
        self.assertEqual(detail["verification_points"], ["结果区域显示正确数值"])
        with self.assertRaisesRegex(ValueError, "state 参数不合法"):
            self.cases.list(state_filter="unknown")

    def test_case_map_project_selects_matching_map_and_execution_target(self) -> None:
        simulator = self.cases.list(project="620C_W6830")
        hardware = self.cases.list(project="6202_W5230")
        simulator_6202 = self.cases.list(project="6202_W5230_SIMULATOR")

        self.assertEqual(simulator["project_label"], "620C W6830")
        self.assertEqual(simulator["execution_target"], "simulator")
        self.assertEqual([item["case_id"] for item in simulator["items"]], ["CALC_001", "CALC_002"])
        self.assertEqual(hardware["project_label"], "6202 W5230")
        self.assertEqual(hardware["execution_target"], "hardware")
        self.assertEqual(hardware["execution_target_label"], "真机")
        self.assertEqual([item["case_id"] for item in hardware["items"]], ["CALC_001"])
        self.assertEqual(hardware["items"][0]["steps_text"], "在真机点击等号")
        self.assertEqual(simulator_6202["execution_target"], "simulator")
        self.assertEqual(simulator_6202["execution_target_label"], "模拟器")
        self.assertEqual(
            simulator_6202["items"][0]["steps_text"],
            "在 6202 模拟器点击等号",
        )
        self.assertEqual(len(hardware["projects"]), 3)
        with self.assertRaisesRegex(ValueError, "测试项目不存在"):
            self.cases.list(project="unknown")

    def test_agent_test_history_is_isolated_by_project(self) -> None:
        hardware_case = self.cases.get("计算器", "CALC_001", project="6202_W5230")
        run_id = self.test_history.create(
            job={
                "project": "6202_W5230",
                "sheet": "计算器",
                "case_id": "CALC_001",
                "case": hardware_case,
                "started_at": "2026-08-13T10:00:00+08:00",
                "finished_at": "2026-08-13T10:00:05+08:00",
                "return_code": 0,
            },
            result={"verdict": "PASS", "reason": "真机符合预期"},
            stdout="done",
            stderr="",
        )

        self.assertEqual(self.test_history.list("计算器", "CALC_001"), [])
        hardware_history = self.test_history.list(
            "计算器", "CALC_001", project="6202_W5230"
        )
        self.assertEqual([item["id"] for item in hardware_history], [run_id])
        self.assertEqual(hardware_history[0]["execution_target"], "hardware")
        self.assertTrue(
            (
                self.paths.test_history
                / "6202_W5230"
                / "计算器"
                / "CALC_001"
                / run_id
                / "run.json"
            ).is_file()
        )

    def test_case_map_list_reuses_history_summary_and_updates_after_new_run(self) -> None:
        def record(verdict: str) -> str:
            return self.test_history.create(
                job={
                    "sheet": "计算器",
                    "case_id": "CALC_001",
                    "case": {"priority": "P0"},
                    "started_at": "2026-08-12T10:00:00+08:00",
                    "finished_at": "2026-08-12T10:01:00+08:00",
                    "return_code": 0,
                },
                result={"verdict": verdict, "reason": verdict},
                stdout="",
                stderr="",
            )

        first_id = record("FAIL")
        with patch.object(
            self.test_history,
            "_case_summary_from_dir",
            wraps=self.test_history._case_summary_from_dir,
        ) as summarize:
            first = self.cases.list()
            second = self.cases.list(state_filter="executable")
            self.assertEqual(summarize.call_count, 1)
            self.assertEqual(first["items"][0]["latest_verdict"], "FAIL")
            self.assertEqual(second["items"][0]["history_count"], 1)

            second_id = record("PASS")
            updated = self.cases.list()
            self.assertEqual(summarize.call_count, 1)
            self.assertEqual(updated["items"][0]["latest_verdict"], "PASS")
            self.assertEqual(updated["items"][0]["history_count"], 2)
            self.assertEqual(
                [row["case_id"] for row in self.cases.list(state_filter="pass")["items"]],
                ["CALC_001"],
            )

            self.assertTrue(self.test_history.delete("计算器", "CALC_001", second_id))
            after_delete = self.cases.list()
            self.assertEqual(summarize.call_count, 2)
            self.assertEqual(after_delete["items"][0]["latest_verdict"], "FAIL")
            self.assertEqual(after_delete["items"][0]["history_count"], 1)
            self.assertEqual(self.cases.list(state_filter="pass")["items"], [])
        self.assertIsNotNone(self.test_history.get("计算器", "CALC_001", first_id))

    def test_batch_candidates_use_latest_history_and_exclude_pass(self) -> None:
        case_map_path = self.paths.case_map / "620C_case_map" / "计算器.json"
        entries = json.loads(case_map_path.read_text(encoding="utf-8"))
        template = dict(entries[0])
        for case_id in ("CALC_003", "CALC_004", "CALC_005"):
            entries.append({**template, "case_id": case_id})
        case_map_path.write_text(json.dumps(entries, ensure_ascii=False), encoding="utf-8")

        def record(case_id: str, verdict: str) -> None:
            self.test_history.create(
                job={
                    "sheet": "计算器",
                    "case_id": case_id,
                    "case": {"priority": "P1"},
                    "started_at": "2026-08-11T10:00:00+08:00",
                    "finished_at": "2026-08-11T10:01:00+08:00",
                    "return_code": 0,
                },
                result={"verdict": verdict, "reason": verdict},
                stdout="",
                stderr="",
            )

        record("CALC_003", "FAIL")
        record("CALC_004", "CANNOT_VERIFY")
        record("CALC_005", "FAIL")
        record("CALC_005", "PASS")

        self.assertEqual(
            [row["case_id"] for row in self.cases.executable({"untested"})],
            ["CALC_001"],
        )
        self.assertEqual(
            [row["case_id"] for row in self.cases.executable({"fail"})],
            ["CALC_003"],
        )
        self.assertEqual(
            [row["case_id"] for row in self.cases.executable({"cannot_verify"})],
            ["CALC_004"],
        )
        summary = self.cases.list()["summary"]
        self.assertEqual(
            {key: summary[key] for key in ("untested", "fail", "cannot_verify", "pass")},
            {"untested": 1, "fail": 1, "cannot_verify": 1, "pass": 1},
        )

    def test_explicit_batch_selection_can_rerun_a_passed_case(self) -> None:
        case = self.cases.get("计算器", "CALC_001")
        self.assertIsNotNone(case)
        self.test_history.create(
            job={
                "sheet": "计算器",
                "case_id": "CALC_001",
                "case": case,
                "started_at": "2026-08-14T10:00:00+08:00",
                "finished_at": "2026-08-14T10:01:00+08:00",
                "return_code": 0,
            },
            result={"verdict": "PASS", "reason": "已通过"},
            stdout="",
            stderr="",
        )

        manager = CaseTestManager(self.paths, self.cases, self.test_history)
        with patch("frontend.server.threading.Thread.start") as start:
            job = manager.start_batch(
                case_refs=[{"sheet": "计算器", "case_id": "CALC_001"}],
            )

        queued_cases = manager._jobs[job["id"]]["cases"]
        self.assertEqual(job["total"], 1)
        self.assertEqual([item["case_id"] for item in queued_cases], ["CALC_001"])
        self.assertEqual(queued_cases[0]["latest_verdict"], "PASS")
        start.assert_called_once_with()

    def test_agent_test_history_saves_result_and_screenshot(self) -> None:
        screenshot = self.paths.evidence / "case.bmp"
        screenshot.write_bytes(b"BM-test")
        run_id = self.test_history.create(
            job={
                "sheet": "计算器",
                "case_id": "CALC_001",
                "started_at": "2026-08-11T10:00:00+08:00",
                "finished_at": "2026-08-11T10:00:05+08:00",
                "return_code": 0,
                "case": self.cases.get("计算器", "CALC_001"),
            },
            result={
                "verdict": "PASS",
                "reason": "GUI 树包含正确结果",
                "terminal_json": [{"type": "gui_tree_end", "status": "ok"}],
            },
            stdout="done",
            stderr="",
            screenshot=screenshot,
        )
        latest = self.test_history.latest("计算器", "CALC_001")
        self.assertEqual(latest["verdict"], "PASS")
        self.assertTrue(latest["has_screenshot"])
        record = self.test_history.get("计算器", "CALC_001", run_id)
        self.assertEqual(record["reason"], "GUI 树包含正确结果")
        self.assertEqual(record["terminal_json"][0]["status"], "ok")
        self.assertIn("screenshot_url", record)
        self.assertEqual(record["screenshot_urls"][0]["label"], "最终画面")

    def test_agent_test_history_archives_multiple_checkpoint_screenshots(self) -> None:
        first = self.paths.evidence / "checkpoint-1.bmp"
        second = self.paths.evidence / "checkpoint-2.bmp"
        first.write_bytes(b"BM-one")
        second.write_bytes(b"BM-two")
        run_id = self.test_history.create(
            job={
                "sheet": "计算器",
                "case_id": "CALC_001",
                "started_at": "2026-08-11T10:00:00+08:00",
                "finished_at": "2026-08-11T10:00:05+08:00",
                "return_code": 0,
                "case": self.cases.get("计算器", "CALC_001"),
            },
            result={
                "verdict": "PASS",
                "reason": "两个状态均符合预期",
                "terminal_json": [],
                "planned_commands": {
                    "setup": [":ENTER_PAGE:CALCULATOR,0"],
                    "action": [":TP_CLICK:10,20,1"],
                    "collect": [":GUI_TREE:1"],
                },
                "command_trace": [
                    {
                        "index": 1,
                        "phase": "action",
                        "source": "case",
                        "kind": "device",
                        "wire": "srv_quick_cmd send TOP5STEP:TP_CLICK:10,20,1;",
                        "command": ":TP_CLICK:10,20,1",
                        "command_name": "TP_CLICK",
                        "status": "accepted",
                        "ok": True,
                    },
                    {
                        "index": 2,
                        "phase": "action",
                        "source": "runner",
                        "kind": "screenshot",
                        "wire": ":GUI_TREE:1",
                        "command": ":HOST_SCREENSHOT:GUI_TREE_CHECKPOINT",
                        "command_name": "HOST_SCREENSHOT",
                        "status": "completed",
                        "ok": True,
                        "checkpoint_index": 1,
                        "checkpoint_label": "状态一",
                    },
                ],
                "evidence_contract": {
                    "status": "COMPLETE",
                    "complete": True,
                    "required_screenshots": 2,
                    "captured_screenshots": 2,
                    "planned_action_count": 1,
                    "attempted_action_count": 1,
                    "business_action_count": 1,
                    "issues": [],
                },
                "screenshots": [
                    {"path": str(first), "label": "状态一", "phase": "action", "command": ":GUI_TREE:1", "trace_index": 2},
                    {"path": str(second), "label": "状态二", "phase": "action", "command": ":GUI_TREE:2", "trace_index": 4},
                ],
            },
            stdout="done",
            stderr="",
        )

        latest = self.test_history.latest("计算器", "CALC_001")
        self.assertEqual(latest["screenshot_count"], 2)
        record = self.test_history.get("计算器", "CALC_001", run_id)
        self.assertEqual(
            [item["label"] for item in record["screenshot_urls"]],
            ["状态一", "状态二"],
        )
        self.assertEqual(len(record["command_trace"]), 2)
        self.assertTrue(record["evidence_contract"]["complete"])
        self.assertEqual(record["screenshot_urls"][0]["trace_index"], 2)
        run_dir = self.test_history._run_dir("计算器", "CALC_001", run_id)
        self.assertEqual((run_dir / "screenshot-01.bmp").read_bytes(), b"BM-one")
        self.assertEqual((run_dir / "screenshot-02.bmp").read_bytes(), b"BM-two")

    def test_defect_detail_exposes_local_description_image(self) -> None:
        self._write_defect(
            "100",
            title="图片描述缺陷",
            description="[image]",
            attachments=[
                {
                    "name": "desc_image_0.png",
                    "kind": "image",
                    "summary": "本地缺陷原图",
                }
            ],
        )
        image_dir = self.paths.defect_images / "100"
        image_dir.mkdir(parents=True)
        image_bytes = b"\x89PNG\r\n\x1a\nlocal-image"
        (image_dir / "desc_image_0.png").write_bytes(image_bytes)

        detail = self.defects.get("100")
        self.assertEqual(
            detail["attachments"][0]["url"],
            "/api/defects/100/images/desc_image_0.png",
        )
        _, base = self._server()
        with urlopen(base + "/api/defects/100", timeout=3) as response:
            payload = json.loads(response.read().decode("utf-8"))
        self.assertEqual(payload["attachments"][0]["url"], "/api/defects/100/images/desc_image_0.png")
        with urlopen(base + payload["attachments"][0]["url"], timeout=3) as response:
            self.assertEqual(response.headers.get_content_type(), "image/png")
            self.assertEqual(response.read(), image_bytes)

    def test_defect_image_path_rejects_parent_segments(self) -> None:
        with self.assertRaisesRegex(ValueError, "图片名称格式不合法"):
            self.defects.defect_image_path("100", "..\\secret.png")

    def test_history_saves_agent_commands_actual_file_and_tristate(self) -> None:
        run_id = self._create_history()
        record = self.history.get("100", run_id)
        self.assertEqual(record["verdict"], "CANNOT_VERIFY")
        self.assertEqual(record["execution_mode"], "agent_generated")
        self.assertEqual(record["schema_version"], 2)
        self.assertFalse(record["legacy_record"])
        self.assertEqual(record["source_file"], "app/calculator.c")
        self.assertEqual(record["test_commands"], [":GUI_TREE:1", ":GET_CURRENT_WIN_ID"])
        self.assertEqual(record["verdict_reasons"], ["模拟器未返回完整数据"])
        self.assertEqual(record["llm_thinking_steps"], [])
        self.assertEqual(record["patch"]["after"], "new")
        self.assertIn("before", record["evidence"])
        self.assertEqual(self.defects.list()["items"][0]["repair_result"], "CANNOT_VERIFY")
        self.assertTrue(self.history.delete("100", run_id))
        self.assertIsNone(self.history.get("100", run_id))

    def test_history_reads_legacy_record_without_new_fields(self) -> None:
        self._write_history_record(
            "legacy",
            {"id": "legacy", "defect": "100", "verdict": "FAIL"},
        )
        record = self.history.get("100", "legacy")
        self.assertEqual(record["verdict"], "FAIL")
        self.assertTrue(record["legacy_record"])
        self.assertEqual(record["test_commands"], [])
        self.assertEqual(record["verdict_reasons"], [])
        self.assertEqual(record["llm_thinking_steps"], [])
        self.assertEqual(record["evidence"], {})

    def test_history_exposes_llm_thinking_steps_from_reproduction_trace(self) -> None:
        run_id = self.history.create(
            defect="100",
            job={"execution_mode": "agent_generated", "progress": {}},
            result={
                "verdict": "PASS",
                "reproduction_trace": {
                    "steps": [
                        {"step": 0, "decision": None},
                        {
                            "step": 1,
                            "decision": {
                                "action": "EXECUTE",
                                "command": ":ENTER_PAGE:VOICE_ASSISTANT_INTERACTION,0",
                                "reason": "先进入语音助手页面，确认初始界面。",
                            },
                        },
                        {
                            "step": 2,
                            "decision": {
                                "action": "EXECUTE",
                                "command": ":SIM_CONNECTION_SET:2,app,1",
                                "reason": "再建立连接，观察页面是否更新。",
                            },
                        },
                    ]
                },
            },
            stdout="",
            stderr="",
        )

        record = self.history.get("100", run_id)
        self.assertEqual(
            record["llm_thinking_steps"],
            ["先进入语音助手页面，确认初始界面。", "再建立连接，观察页面是否更新。"],
        )

    def test_history_saves_current_conforms_reason_and_commands_without_patch(self) -> None:
        run_id = self.history.create(
            defect="100",
            job={"execution_mode": "agent_generated", "progress": {}},
            result={
                "verdict": "PASS",
                "attempts": 0,
                "reproduction_outcome": "CURRENT_CONFORMS",
                "reproduction_reason": "当前界面符合预期，缺陷未复现。",
                "agent_test_commands": [":ENTER_PAGE:MESSAGE_LIST,0", ":ENTER_PAGE:SIDEBAR,0"],
                "patch": None,
                "test_output": None,
                "history": [],
                "patch_retained": False,
            },
            stdout="",
            stderr="",
        )

        record = self.history.get("100", run_id)
        self.assertEqual(record["schema_version"], 2)
        self.assertFalse(record["legacy_record"])
        self.assertEqual(record["verdict_reasons"], ["当前界面符合预期，缺陷未复现。"])
        self.assertEqual(
            record["test_commands"],
            [":ENTER_PAGE:MESSAGE_LIST,0", ":ENTER_PAGE:SIDEBAR,0"],
        )
        self.assertEqual(record["patch"], {})
        self.assertEqual(record["test_result"], {})

    def test_history_recovers_evidence_from_legacy_baseline_output(self) -> None:
        self._write_history_record(
            "legacy-current-conforms",
            {
                "id": "legacy-current-conforms",
                "defect": "100",
                "verdict": "PASS",
                "reproduction_outcome": "CURRENT_CONFORMS",
                "test_commands": [],
                "baseline_output": {
                    "reason": "旧格式中保存的判定理由",
                    "test_commands": [":ENTER_PAGE:SIDEBAR,0"],
                },
                "rounds": [],
            },
            test_result={},
        )

        record = self.history.get("100", "legacy-current-conforms")
        self.assertTrue(record["legacy_record"])
        self.assertEqual(record["verdict_reasons"], ["旧格式中保存的判定理由"])
        self.assertEqual(record["test_commands"], [":ENTER_PAGE:SIDEBAR,0"])

    def test_current_schema_missing_evidence_is_not_labeled_legacy(self) -> None:
        self._write_history_record(
            "current-incomplete",
            {
                "schema_version": 2,
                "id": "current-incomplete",
                "defect": "100",
                "verdict": "FAIL",
            },
        )

        record = self.history.get("100", "current-incomplete")
        self.assertFalse(record["legacy_record"])
        self.assertEqual(record["verdict_reasons"], [])
        self.assertEqual(record["test_commands"], [])
        self.assertEqual(record["llm_thinking_steps"], [])

    def test_history_recovers_commands_and_patch_from_previous_round(self) -> None:
        run_id = self.history.create(
            defect="100",
            job={"execution_mode": "agent_generated", "progress": {}},
            result={
                "verdict": "CANNOT_VERIFY",
                "patch": None,
                "test_output": None,
                "history": [
                    {
                        "patch": {"file_path": "app/calculator.c", "before": "old", "after": "new"},
                        "test_commands": [":GUI_TREE:1"],
                        "test_output": {"results": [{"verdict": "FAIL", "terminal_json": [{"ok": False}]}]},
                    }
                ],
            },
            stdout="",
            stderr="",
        )
        record = self.history.get("100", run_id)
        self.assertEqual(record["source_file"], "app/calculator.c")
        self.assertEqual(record["test_commands"], [":GUI_TREE:1"])
        self.assertEqual(record["patch"]["after"], "new")
        self.assertEqual(record["test_result"]["results"][0]["verdict"], "FAIL")

    def test_history_rejects_parent_directory_segments(self) -> None:
        with self.assertRaisesRegex(ValueError, "格式不合法"):
            self.history.get("..", "run")
        with self.assertRaisesRegex(ValueError, "格式不合法"):
            self.history.delete("100", "..")

    def test_defect_list_paginates_searches_and_clamps_last_page(self) -> None:
        for number in range(101, 125):
            self._write_defect(
                str(number),
                title=f"{'计算器' if number in {121, 122} else '活动记录'} 缺陷 {number}",
                description="按钮 显示异常" if number == 122 else "列表描述",
                status="处理中" if number in {121, 122} else "待处理",
            )

        first = self.defects.list()
        self.assertEqual(first["total"], 25)
        self.assertEqual(first["page_size"], 20)
        self.assertEqual(first["items"][0]["number"], "124")

        last = self.defects.list(page=999, page_size=10)
        self.assertEqual(last["page"], 3)
        self.assertEqual(len(last["items"]), 5)

        chinese = self.defects.list(query="计算器", page_size=10)
        self.assertEqual([row["number"] for row in chinese["items"]], ["122", "121", "100"])
        numbered = self.defects.list(query="122")
        self.assertEqual([row["number"] for row in numbered["items"]], ["122"])
        multiple = self.defects.list(query="  计算器   处理中 ")
        self.assertEqual([row["number"] for row in multiple["items"]], ["122", "121", "100"])
        empty = self.defects.list(query="完全不存在")
        self.assertEqual(empty["items"], [])
        self.assertEqual(empty["total_pages"], 0)

    def test_defect_list_filters_by_latest_repair_result(self) -> None:
        for number, verdict in (("101", "PASS"), ("102", "FAIL"), ("103", "CANNOT_VERIFY")):
            self._write_defect(number, title=f"筛选测试 {number}")
            self._create_history(verdict, defect=number)

        self.assertEqual([row["number"] for row in self.defects.list(result_filter="pass")["items"]], ["101"])
        self.assertEqual([row["number"] for row in self.defects.list(result_filter="fail")["items"]], ["102"])
        self.assertEqual([row["number"] for row in self.defects.list(result_filter="cannot_verify")["items"]], ["103"])
        self.assertEqual([row["number"] for row in self.defects.list(result_filter="pending")["items"]], ["100"])
        with self.assertRaisesRegex(ValueError, "result 参数不合法"):
            self.defects.list(result_filter="unknown")

    def test_http_api_page_routes_and_invalid_pagination(self) -> None:
        _, base = self._server()
        query = urlencode({"q": "计算器", "page": 1, "page_size": 20})
        with urlopen(base + "/api/defects?" + query, timeout=3) as response:
            payload = json.loads(response.read().decode("utf-8"))
            self.assertEqual(payload["items"][0]["number"], "100")
            self.assertEqual(payload["total"], 1)
        with urlopen(base + "/defect/100", timeout=3) as response:
            self.assertIn("正常".encode("utf-8"), response.read())
        for path in ("/api/defects?page=0", "/api/defects?page_size=101", "/api/defects?page=abc", "/api/defects?result=unknown"):
            with self.assertRaises(HTTPError) as raised:
                urlopen(base + path, timeout=3)
            self.assertEqual(raised.exception.code, 400)
        with self.assertRaises(HTTPError) as raised:
            urlopen(base + "/api/defects/missing", timeout=3)
        self.assertEqual(raised.exception.code, 404)

    def test_agent_test_api_lists_opens_and_starts_cases(self) -> None:
        application, base = self._server()
        with urlopen(base + "/api/tests?state=executable&page=1&page_size=20", timeout=3) as response:
            payload = json.loads(response.read().decode("utf-8"))
        self.assertEqual([item["case_id"] for item in payload["items"]], ["CALC_001"])
        with urlopen(base + "/api/tests/%E8%AE%A1%E7%AE%97%E5%99%A8/CALC_001", timeout=3) as response:
            detail = json.loads(response.read().decode("utf-8"))
        self.assertEqual(detail["expected_text"], "显示正确")
        with urlopen(
            base + "/api/tests?project=6202_W5230&state=executable&page=1&page_size=20",
            timeout=3,
        ) as response:
            hardware = json.loads(response.read().decode("utf-8"))
        self.assertEqual(hardware["execution_target"], "hardware")
        self.assertEqual(hardware["items"][0]["expected_text"], "真机显示正确")
        for page_path in ("/tests", "/test/%E8%AE%A1%E7%AE%97%E5%99%A8/CALC_001"):
            with urlopen(base + page_path, timeout=3) as response:
                self.assertIn("正常".encode("utf-8"), response.read())

        job = {"id": "test1", "sheet": "计算器", "case_id": "CALC_001", "status": "queued"}
        with patch.object(application, "start_case_test", return_value=job) as start:
            with self._post_json(base + "/api/tests/run", {"sheet": "计算器", "case_id": "CALC_001"}) as response:
                started = json.loads(response.read().decode("utf-8"))
        self.assertEqual(response.status, 202)
        self.assertEqual(started["id"], "test1")
        start.assert_called_once_with(
            sheet="计算器", case_id="CALC_001", project="620C_W6830"
        )

        hardware_job = {
            "id": "test2",
            "project": "6202_W5230",
            "execution_target": "hardware",
            "sheet": "计算器",
            "case_id": "CALC_001",
            "status": "queued",
        }
        with patch.object(application, "start_case_test", return_value=hardware_job) as start_hardware:
            with self._post_json(
                base + "/api/tests/run",
                {"project": "6202_W5230", "sheet": "计算器", "case_id": "CALC_001"},
            ) as response:
                started_hardware = json.loads(response.read().decode("utf-8"))
        self.assertEqual(started_hardware["execution_target"], "hardware")
        start_hardware.assert_called_once_with(
            sheet="计算器", case_id="CALC_001", project="6202_W5230"
        )

    def test_agent_test_api_starts_batch_and_serves_batch_page(self) -> None:
        application, base = self._server()
        job = {"id": "batch1", "type": "batch", "status": "queued", "total": 1}
        with patch.object(application, "start_batch_test", return_value=job) as start:
            with self._post_json(base + "/api/tests/run-batch", {"limit": 0}) as response:
                started = json.loads(response.read().decode("utf-8"))
        self.assertEqual(response.status, 202)
        self.assertEqual(started["id"], "batch1")
        start.assert_called_once_with(limit=0, project="620C_W6830")

        with patch.object(application, "start_batch_test", return_value=job) as start_selected:
            with self._post_json(base + "/api/tests/run-batch", {
                "cases": [{"sheet": "计算器", "case_id": "CALC_001"}]
            }) as response:
                selected = json.loads(response.read().decode("utf-8"))
        self.assertEqual(response.status, 202)
        self.assertEqual(selected["id"], "batch1")
        start_selected.assert_called_once_with(
            limit=0,
            case_refs=[{"sheet": "计算器", "case_id": "CALC_001"}],
            project="620C_W6830",
        )

        with patch.object(application, "start_batch_test", return_value=job) as start_categories:
            with self._post_json(base + "/api/tests/run-batch", {
                "categories": ["untested", "fail", "cannot_verify"]
            }) as response:
                selected = json.loads(response.read().decode("utf-8"))
        self.assertEqual(response.status, 202)
        self.assertEqual(selected["id"], "batch1")
        start_categories.assert_called_once_with(
            limit=0,
            categories={"untested", "fail", "cannot_verify"},
            project="620C_W6830",
        )

        with self.assertRaises(HTTPError) as raised:
            self._post_json(base + "/api/tests/run-batch", {"categories": ["pass"]})
        self.assertEqual(raised.exception.code, 400)
        with urlopen(base + "/test-batch/batch1", timeout=3) as response:
            self.assertIn("正常".encode("utf-8"), response.read())

        with self.assertRaises(HTTPError) as raised:
            self._post_json(base + "/api/tests/run-batch", {"limit": -1})
        self.assertEqual(raised.exception.code, 400)

    def test_case_test_manager_allows_one_hardware_and_one_simulator_job(self) -> None:
        manager = CaseTestManager(self.paths, self.cases, self.test_history)

        with patch("frontend.server.threading.Thread.start"):
            simulator = manager.start_batch(
                limit=1,
                project="6202_W5230_SIMULATOR",
            )
            hardware = manager.start_batch(
                limit=1,
                project="6202_W5230",
            )

            with self.assertRaisesRegex(RuntimeError, "模拟器资源已占用"):
                manager.start_batch(limit=1, project="620C_W6830")
            with self.assertRaisesRegex(RuntimeError, "真机资源已占用"):
                manager.start(
                    sheet="计算器",
                    case_id="CALC_001",
                    project="6202_W5230",
                )

        active = {
            job["execution_target"]: job
            for job in manager.active_jobs()
        }
        self.assertEqual(active["simulator"]["id"], simulator["id"])
        self.assertEqual(active["hardware"]["id"], hardware["id"])

    def test_active_test_api_returns_both_resource_slots(self) -> None:
        application, base = self._server()
        jobs = [
            {
                "id": "hardware-live",
                "project": "6202_W5230",
                "execution_target": "hardware",
                "status": "running",
            },
            {
                "id": "simulator-live",
                "project": "6202_W5230_SIMULATOR",
                "execution_target": "simulator",
                "status": "running",
            },
        ]
        with patch.object(
            application.test_jobs,
            "active_jobs",
            return_value=jobs,
        ) as active_jobs:
            with urlopen(base + "/api/tests/active", timeout=3) as response:
                payload = json.loads(response.read().decode("utf-8"))

        self.assertEqual(response.status, 200)
        self.assertEqual(payload["job"], jobs[0])
        self.assertEqual(payload["jobs"], jobs)
        active_jobs.assert_called_once_with()

    def test_case_test_manager_reuses_cli_and_archives_result(self) -> None:
        manager = CaseTestManager(self.paths, self.cases, self.test_history)
        job_id = "case-job"
        manager._jobs[job_id] = {
            "id": job_id,
            "sheet": "计算器",
            "case_id": "CALC_001",
            "case": self.cases.get("计算器", "CALC_001"),
            "status": "queued",
            "current_node": "load",
            "nodes": {node: "pending" for node in ("load", "execute", "judge", "record")},
            "verdict": "PENDING",
            "reason": "",
            "created_at": "2026-08-11T10:00:00+08:00",
            "started_at": None,
            "finished_at": None,
            "history_id": None,
            "error": None,
        }
        manager._active_job_ids["simulator"] = job_id

        class FakeProcess:
            returncode = 0

            def __init__(self, argv: list[str]):
                self.argv = argv

            def communicate(self) -> tuple[str, str]:
                result_file = Path(self.argv[self.argv.index("--result-file") + 1])
                screenshot = Path(self.argv[self.argv.index("--screenshot-path") + 1])
                result_file.write_text(
                    json.dumps({
                        "verdict": "PASS", "reason": "符合预期", "terminal_json": [{"status": "ok"}],
                        "setup_errors": [], "action_errors": [], "collect_errors": [],
                    }, ensure_ascii=False),
                    encoding="utf-8",
                )
                screenshot.write_bytes(b"BM-test")
                return "PASS", ""

        with patch("frontend.server.subprocess.Popen", side_effect=lambda argv, **_: FakeProcess(argv)):
            manager._run(job_id)

        snapshot = manager.get(job_id)
        self.assertEqual(snapshot["status"], "completed")
        self.assertEqual(snapshot["verdict"], "PASS")
        self.assertEqual(snapshot["nodes"], {"load": "pass", "execute": "pass", "judge": "pass", "record": "pass"})
        self.assertIsNotNone(snapshot["history_id"])

    def test_case_test_manager_uses_hardware_target_for_6202(self) -> None:
        manager = CaseTestManager(self.paths, self.cases, self.test_history)
        case = self.cases.get("计算器", "CALC_001", project="6202_W5230")
        manager._jobs["hardware-job"] = {"process": None}
        captured_argv: list[str] = []
        captured_env: dict[str, str] = {}

        class FakeProcess:
            returncode = 0

            def __init__(self, argv: list[str], env: dict[str, str]):
                captured_argv.extend(argv)
                captured_env.update(env)

            def communicate(self) -> tuple[str, str]:
                result_file = Path(captured_argv[captured_argv.index("--result-file") + 1])
                result_file.write_text(
                    json.dumps({"verdict": "PASS", "reason": "真机通过"}, ensure_ascii=False),
                    encoding="utf-8",
                )
                return "PASS", ""

        hardware_root = r"D:\Agent-loop-workspace\6202_W5230"
        with patch.dict(os.environ, {
            "W30_SOURCE_ROOT": r"D:\Agent-loop-workspace\620C_W6830",
            "W30_AGENT_WORKSPACE_ROOT": r"D:\Agent-loop-workspace\620C_W6830",
            "W30_PROJECT": "620C_W6830",
            "W30_HARDWARE_SOURCE_ROOT": hardware_root,
            "W30_HARDWARE_WORKSPACE_ROOT": hardware_root,
            "W30_HARDWARE_PROJECT": "6202_W5230",
        }, clear=False):
            with (
                patch("agent_loop_system.main._load_env") as load_env,
                patch(
                    "frontend.server.subprocess.Popen",
                    side_effect=lambda argv, **kwargs: FakeProcess(
                        argv, kwargs["env"]
                    ),
                ),
            ):
                result = manager._execute_case(
                    job_id="hardware-job",
                    case=case,
                    job_dir=self.paths.runtime_jobs / "hardware-job" / "single",
                )

        load_env.assert_called_once_with()
        self.assertEqual(captured_argv[captured_argv.index("--target") + 1], "hardware")
        self.assertNotIn("--preserve-test-session", captured_argv)
        self.assertEqual(captured_env["W30_SOURCE_ROOT"], hardware_root)
        self.assertEqual(captured_env["W30_AGENT_WORKSPACE_ROOT"], hardware_root)
        self.assertEqual(captured_env["W30_PROJECT"], "6202_W5230")
        self.assertEqual(captured_env["W30_HARDWARE_PROJECT"], "6202_W5230")
        self.assertEqual(result["verdict"], "PASS")

    def test_case_test_manager_uses_isolated_6202_simulator_profile(self) -> None:
        manager = CaseTestManager(self.paths, self.cases, self.test_history)
        case = self.cases.get(
            "计算器", "CALC_001", project="6202_W5230_SIMULATOR"
        )
        manager._jobs["simulator-6202-job"] = {"process": None}
        captured_argv: list[str] = []
        captured_env: dict[str, str] = {}

        class FakeProcess:
            returncode = 0

            def __init__(self, argv: list[str], env: dict[str, str]):
                captured_argv.extend(argv)
                captured_env.update(env)

            def communicate(self) -> tuple[str, str]:
                result_file = Path(captured_argv[captured_argv.index("--result-file") + 1])
                result_file.write_text(
                    json.dumps({"verdict": "PASS", "reason": "截图通过"}, ensure_ascii=False),
                    encoding="utf-8",
                )
                return "PASS", ""

        with patch(
            "frontend.server.subprocess.Popen",
            side_effect=lambda argv, **kwargs: FakeProcess(argv, kwargs["env"]),
        ):
            result = manager._execute_case(
                job_id="simulator-6202-job",
                case=case,
                job_dir=self.paths.runtime_jobs / "simulator-6202-job" / "single",
            )

        self.assertEqual(captured_argv[captured_argv.index("--target") + 1], "simulator")
        self.assertEqual(
            captured_argv[captured_argv.index("--case-map-profile") + 1],
            "6202_W5230_SIMULATOR",
        )
        self.assertNotIn("--preserve-test-session", captured_argv)
        self.assertEqual(captured_env["W30_PROJECT"], "6202_W5230")
        self.assertEqual(
            captured_env["W30_SOURCE_ROOT"],
            r"D:\Agent-loop-workspace\6202_W5230",
        )
        self.assertEqual(result["verdict"], "PASS")

    def test_collection_error_does_not_override_screenshot_verdict(self) -> None:
        manager = CaseTestManager(self.paths, self.cases, self.test_history)
        case = self.cases.get("计算器", "CALC_001", project="6202_W5230")
        manager._jobs["collect-warning"] = {"process": None}

        class FakeProcess:
            returncode = 0

            def __init__(self, argv: list[str]):
                self.argv = argv

            def communicate(self) -> tuple[str, str]:
                result_file = Path(self.argv[self.argv.index("--result-file") + 1])
                result_file.write_text(
                    json.dumps({
                        "verdict": "PASS",
                        "reason": "截图符合预期",
                        "setup_errors": [],
                        "action_errors": [],
                        "collect_errors": ["GUI_TREE 回包不完整"],
                    }, ensure_ascii=False),
                    encoding="utf-8",
                )
                return "PASS", ""

        with patch("frontend.server.subprocess.Popen", side_effect=lambda argv, **_: FakeProcess(argv)):
            result = manager._execute_case(
                job_id="collect-warning",
                case=case,
                job_dir=self.paths.runtime_jobs / "collect-warning" / "single",
            )

        self.assertEqual(result["verdict"], "PASS")
        self.assertTrue(result["execute_failed"])
        self.assertEqual(result["execution_reason"], "GUI_TREE 回包不完整")

    def test_incomplete_evidence_contract_marks_execution_failed(self) -> None:
        manager = CaseTestManager(self.paths, self.cases, self.test_history)
        case = self.cases.get("计算器", "CALC_001")
        manager._jobs["evidence-error"] = {"process": None}

        class FakeProcess:
            returncode = 1

            def __init__(self, argv: list[str]):
                self.argv = argv

            def communicate(self) -> tuple[str, str]:
                result_file = Path(self.argv[self.argv.index("--result-file") + 1])
                result_file.write_text(
                    json.dumps({
                        "schema_version": 2,
                        "verdict": "ERROR",
                        "reason": "业务动作未执行",
                        "evidence_contract": {
                            "complete": False,
                            "issues": [{
                                "code": "business_action_missing",
                                "message": "业务动作未执行",
                            }],
                        },
                        "setup_errors": [],
                        "action_errors": [],
                        "collect_errors": [],
                    }, ensure_ascii=False),
                    encoding="utf-8",
                )
                return "ERROR", ""

        with patch("frontend.server.subprocess.Popen", side_effect=lambda argv, **_: FakeProcess(argv)):
            result = manager._execute_case(
                job_id="evidence-error",
                case=case,
                job_dir=self.paths.runtime_jobs / "evidence-error" / "single",
            )

        self.assertEqual(result["verdict"], "ERROR")
        self.assertTrue(result["execute_failed"])
        self.assertEqual(result["execution_reason"], "业务动作未执行")

    def test_case_test_manager_runs_executable_batch_and_reports_progress(self) -> None:
        manager = CaseTestManager(self.paths, self.cases, self.test_history)
        job_id = "batch-job"
        case = self.cases.executable()[0]
        manager._jobs[job_id] = {
            "id": job_id,
            "type": "batch",
            "status": "queued",
            "created_at": "2026-08-11T10:00:00+08:00",
            "started_at": None,
            "finished_at": None,
            "total": 1,
            "completed": 0,
            "current_index": 0,
            "current_case": None,
            "current_node": "load",
            "verdict_counts": {"PASS": 0, "FAIL": 0, "ERROR": 0, "CANNOT_VERIFY": 0, "SKIP": 0},
            "recent_results": [],
            "cancel_requested": False,
            "error": None,
            "cases": [case],
        }
        manager._active_job_ids["simulator"] = job_id

        class FakeProcess:
            returncode = 0

            def __init__(self, argv: list[str]):
                self.argv = argv

            def communicate(self) -> tuple[str, str]:
                result_file = Path(self.argv[self.argv.index("--result-file") + 1])
                screenshot = Path(self.argv[self.argv.index("--screenshot-path") + 1])
                checkpoint = screenshot.with_name("screenshot-01.bmp")
                result_file.write_text(
                    json.dumps({
                        "verdict": "PASS",
                        "reason": "批次符合预期",
                        "terminal_json": [{"status": "ok"}],
                        "screenshots": [{"path": str(checkpoint), "label": "结果正确"}],
                        "setup_errors": [],
                        "action_errors": [],
                        "collect_errors": [],
                    }, ensure_ascii=False),
                    encoding="utf-8",
                )
                checkpoint.write_bytes(b"BM-checkpoint")
                return "PASS", ""

        with patch("frontend.server.subprocess.Popen", side_effect=lambda argv, **_: FakeProcess(argv)):
            manager._run_batch(job_id)

        snapshot = manager.get(job_id)
        self.assertEqual(snapshot["status"], "completed")
        self.assertEqual(snapshot["completed"], 1)
        self.assertEqual(snapshot["verdict_counts"]["PASS"], 1)
        self.assertEqual(snapshot["recent_results"][0]["case_id"], "CALC_001")
        self.assertIsNotNone(snapshot["recent_results"][0]["history_id"])
        self.assertEqual(len(snapshot["live_screenshots"]), 1)

    def test_hardware_batch_stops_before_cases_when_external_session_is_inactive(self) -> None:
        manager = CaseTestManager(self.paths, self.cases, self.test_history)
        job_id = "hardware-preflight"
        case = self.cases.get("计算器", "CALC_001", project="6202_W5230")
        self.assertIsNotNone(case)
        manager._jobs[job_id] = {
            "id": job_id,
            "type": "batch",
            "project": "6202_W5230",
            "execution_target": "hardware",
            "status": "queued",
            "created_at": "2026-08-14T00:00:00+08:00",
            "started_at": None,
            "finished_at": None,
            "total": 1,
            "completed": 0,
            "current_index": 0,
            "current_case": None,
            "current_node": "load",
            "verdict_counts": {
                "PASS": 0,
                "FAIL": 0,
                "ERROR": 0,
                "CANNOT_VERIFY": 0,
                "SKIP": 0,
            },
            "recent_results": [],
            "cancel_requested": False,
            "error": None,
            "cases": [case],
        }
        manager._active_job_ids["hardware"] = job_id

        with (
            patch("agent_loop_system.main._load_env") as load_env,
            patch(
                "agent_loop_system.tools.hardware_target.HardwareTargetConfig.from_env"
            ),
            patch(
                "agent_loop_system.tools.real_device.query_test_session_status",
                return_value=SimpleNamespace(active=False, lease_seconds=0),
            ) as status_query,
            patch("frontend.server.subprocess.Popen") as popen,
        ):
            manager._run_batch(job_id)

        snapshot = manager.get(job_id)
        load_env.assert_called_once_with()
        status_query.assert_called_once()
        popen.assert_not_called()
        self.assertEqual(snapshot["status"], "failed")
        self.assertEqual(snapshot["completed"], 0)
        self.assertIn("24 小时测试模式未开启", snapshot["error"])
        self.assertNotIn("hardware", manager._active_job_ids)

    def test_hardware_batch_interrupts_on_infrastructure_failure_and_retries_case(self) -> None:
        manager = CaseTestManager(self.paths, self.cases, self.test_history)
        first = dict(self.cases.get("计算器", "CALC_001", project="6202_W5230"))
        second = {**first, "case_id": "CALC_002"}
        job_id = "hardware-interruption"
        manager._jobs[job_id] = {
            "id": job_id,
            "type": "batch",
            "project": "6202_W5230",
            "project_label": "6202 W5230",
            "execution_target": "hardware",
            "execution_target_label": "真机",
            "status": "queued",
            "created_at": "2026-08-14T00:00:00+08:00",
            "started_at": None,
            "finished_at": None,
            "total": 2,
            "completed": 0,
            "current_index": 0,
            "current_case": None,
            "current_node": "load",
            "verdict_counts": {
                "PASS": 0,
                "FAIL": 0,
                "ERROR": 0,
                "CANNOT_VERIFY": 0,
                "SKIP": 0,
            },
            "recent_results": [],
            "cancel_requested": False,
            "error": None,
            "case_attempts": {},
            "cases": [first, second],
        }
        manager._active_job_ids["hardware"] = job_id
        executed_cases: list[str] = []

        class BrokenProcess:
            returncode = 1

            def __init__(self, argv: list[str]):
                executed_cases.append(argv[argv.index("--case-id") + 1])

            def communicate(self) -> tuple[str, str]:
                return "", "HardwareSerialTimeoutError: no result for :GUI_PING:1"

        preflight = SimpleNamespace(active=True, lease_seconds=86000)
        with (
            patch("agent_loop_system.main._load_env"),
            patch("agent_loop_system.tools.hardware_target.HardwareTargetConfig.from_env"),
            patch(
                "agent_loop_system.tools.real_device.query_test_session_status",
                return_value=preflight,
            ),
            patch(
                "frontend.server.subprocess.Popen",
                side_effect=lambda argv, **_: BrokenProcess(argv),
            ) as popen,
        ):
            manager._run_batch(job_id)

        interrupted = manager.get(job_id)
        self.assertEqual(popen.call_count, 1)
        self.assertEqual(executed_cases, ["CALC_001"])
        self.assertEqual(interrupted["status"], "interrupted")
        self.assertEqual(interrupted["completed"], 0)
        self.assertEqual(interrupted["current_index"], 1)
        self.assertTrue(interrupted["resume_available"])
        self.assertIn("CALC_001", interrupted["interruption_reason"])
        self.assertIn("GUI_PING", interrupted["error"])
        self.assertEqual(
            manager._jobs[job_id]["case_attempts"],
            {"0001-CALC_001": 1},
        )
        self.assertEqual(
            self.test_history.list("计算器", "CALC_001", project="6202_W5230"),
            [],
        )

        with patch("frontend.server.threading.Thread.start"):
            resumed = manager.resume_batch(job_id)
        self.assertEqual(resumed["status"], "queued")
        self.assertEqual(resumed["completed"], 0)
        self.assertIsNone(resumed["error"])

        class CompletedProcess:
            def __init__(self, argv: list[str]):
                self.argv = argv
                self.case_id = argv[argv.index("--case-id") + 1]
                self.returncode = 1 if self.case_id == "CALC_001" else 0
                executed_cases.append(self.case_id)

            def communicate(self) -> tuple[str, str]:
                result_file = Path(self.argv[self.argv.index("--result-file") + 1])
                screenshot = Path(self.argv[self.argv.index("--screenshot-path") + 1])
                checkpoint = screenshot.with_name("screenshot-01.bmp")
                verdict = "FAIL" if self.case_id == "CALC_001" else "PASS"
                result_file.write_text(json.dumps({
                    "verdict": verdict,
                    "reason": "产品结果已形成",
                    "screenshots": [{"path": str(checkpoint), "label": "结果"}],
                    "setup_errors": [],
                    "action_errors": [],
                    "collect_errors": [],
                }, ensure_ascii=False), encoding="utf-8")
                checkpoint.write_bytes(b"BM-completed")
                return verdict, ""

        with (
            patch("agent_loop_system.main._load_env"),
            patch("agent_loop_system.tools.hardware_target.HardwareTargetConfig.from_env"),
            patch(
                "agent_loop_system.tools.real_device.query_test_session_status",
                return_value=preflight,
            ),
            patch(
                "frontend.server.subprocess.Popen",
                side_effect=lambda argv, **_: CompletedProcess(argv),
            ),
        ):
            manager._run_batch(job_id)

        completed = manager.get(job_id)
        self.assertEqual(executed_cases, ["CALC_001", "CALC_001", "CALC_002"])
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(completed["completed"], 2)
        self.assertEqual(completed["verdict_counts"]["FAIL"], 1)
        self.assertEqual(completed["verdict_counts"]["PASS"], 1)
        self.assertEqual(
            manager._jobs[job_id]["case_attempts"],
            {"0001-CALC_001": 2, "0002-CALC_002": 1},
        )
        self.assertTrue(
            (self.paths.runtime_jobs / job_id / "0001-CALC_001").is_dir()
        )

    def test_batch_restart_recovers_same_id_and_runs_only_remaining_case(self) -> None:
        manager = CaseTestManager(self.paths, self.cases, self.test_history)
        first = dict(self.cases.executable()[0])
        second = {**first, "case_id": "CALC_002"}
        job_id = "restart-batch"
        manager._jobs[job_id] = {
            "id": job_id,
            "type": "batch",
            "status": "running",
            "created_at": "2026-08-12T00:00:00+08:00",
            "started_at": "2026-08-12T00:00:01+08:00",
            "finished_at": None,
            "total": 2,
            "completed": 1,
            "current_index": 2,
            "current_case": {"sheet": "计算器", "case_id": "CALC_002"},
            "current_node": "execute",
            "verdict_counts": {"PASS": 1, "FAIL": 0, "ERROR": 0, "CANNOT_VERIFY": 0, "SKIP": 0},
            "recent_results": [],
            "cancel_requested": False,
            "error": None,
            "cases": [first, second],
        }
        with manager._lock:
            manager._persist_batch_locked(manager._jobs[job_id])

        restarted = CaseTestManager(self.paths, self.cases, self.test_history)
        interrupted = restarted.get(job_id)
        self.assertEqual(interrupted["status"], "interrupted")
        self.assertTrue(interrupted["resume_available"])
        self.assertEqual(interrupted["completed"], 1)

        with patch("frontend.server.threading.Thread.start") as start:
            resumed = restarted.resume_batch(job_id)
            repeated = restarted.resume_batch(job_id)
        self.assertEqual(resumed["id"], job_id)
        self.assertEqual(repeated["id"], job_id)
        self.assertEqual(start.call_count, 1)

        executed_cases: list[str] = []

        class FakeProcess:
            returncode = 0

            def __init__(self, argv: list[str]):
                self.argv = argv
                executed_cases.append(argv[argv.index("--case-id") + 1])

            def communicate(self) -> tuple[str, str]:
                result_file = Path(self.argv[self.argv.index("--result-file") + 1])
                screenshot = Path(self.argv[self.argv.index("--screenshot-path") + 1])
                checkpoint = screenshot.with_name("screenshot-01.bmp")
                result_file.write_text(json.dumps({
                    "verdict": "PASS", "reason": "恢复后通过", "screenshots": [{"path": str(checkpoint)}],
                    "setup_errors": [], "action_errors": [], "collect_errors": [],
                }, ensure_ascii=False), encoding="utf-8")
                checkpoint.write_bytes(b"BM-resumed")
                return "PASS", ""

        with patch("frontend.server.subprocess.Popen", side_effect=lambda argv, **_: FakeProcess(argv)):
            restarted._run_batch(job_id)

        completed = restarted.get(job_id)
        self.assertEqual(executed_cases, ["CALC_002"])
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(completed["completed"], 2)
        self.assertFalse(completed["resume_available"])

        loaded_again = CaseTestManager(self.paths, self.cases, self.test_history)
        self.assertEqual(loaded_again.get(job_id)["status"], "completed")
        with self.assertRaisesRegex(ValueError, "全部完成"):
            loaded_again.resume_batch(job_id)

    def test_cancelled_batch_is_resumable_without_resetting_progress(self) -> None:
        manager = CaseTestManager(self.paths, self.cases, self.test_history)
        case = dict(self.cases.executable()[0])
        job_id = "paused-batch"
        manager._jobs[job_id] = {
            "id": job_id, "type": "batch", "status": "cancelled",
            "created_at": "2026-08-12T00:00:00+08:00", "started_at": "2026-08-12T00:00:01+08:00",
            "finished_at": "2026-08-12T00:01:00+08:00", "total": 2, "completed": 1,
            "current_index": 1, "current_case": None, "current_node": None,
            "verdict_counts": {"PASS": 1, "FAIL": 0, "ERROR": 0, "CANNOT_VERIFY": 0, "SKIP": 0},
            "recent_results": [], "cancel_requested": True, "error": None,
            "interruption_reason": "用户请求在当前用例结束后暂停",
            "cases": [case, {**case, "case_id": "CALC_002"}],
        }
        with patch("frontend.server.threading.Thread.start") as start:
            snapshot = manager.resume_batch(job_id)
        self.assertEqual(snapshot["status"], "queued")
        self.assertEqual(snapshot["completed"], 1)
        self.assertEqual(snapshot["current_index"], 1)
        self.assertFalse(snapshot["cancel_requested"])
        start.assert_called_once_with()

    def test_resume_batch_api_uses_existing_batch(self) -> None:
        application, base = self._server()
        job = {"id": "batch1", "type": "batch", "status": "queued", "completed": 3, "total": 5}
        with patch.object(application, "resume_batch_test", return_value=job) as resume:
            with self._post_json(base + "/api/tests/jobs/batch1/resume", {}) as response:
                payload = json.loads(response.read().decode("utf-8"))
        self.assertEqual(response.status, 202)
        self.assertEqual(payload["id"], "batch1")
        resume.assert_called_once_with("batch1")

    def test_run_api_accepts_only_defect_and_returns_conflict(self) -> None:
        application, base = self._server()
        job = {"id": "job1", "defect": "100", "status": "queued", "verdict": "PENDING"}
        with patch.object(application.jobs, "start", return_value=job) as start:
            with self._post_json(base + "/api/run", {"defect": "100"}) as response:
                payload = json.loads(response.read().decode("utf-8"))
            self.assertEqual(response.status, 202)
            self.assertEqual(payload["id"], "job1")
            start.assert_called_once_with(defect="100")

        for payload in ({}, {"defect": ""}, {"defect": 100}):
            with self.assertRaises(HTTPError) as raised:
                self._post_json(base + "/api/run", payload)
            self.assertEqual(raised.exception.code, 400)

        with patch.object(application.jobs, "start", side_effect=RuntimeError("已有修复任务正在运行")):
            with self.assertRaises(HTTPError) as raised:
                self._post_json(base + "/api/run", {"defect": "100"})
            self.assertEqual(raised.exception.code, 409)

    def test_active_run_api_returns_current_job(self) -> None:
        application, base = self._server()
        job = {"id": "job-live", "defect": "100", "status": "running"}
        with patch.object(application.jobs, "active", return_value=job) as active:
            with urlopen(base + "/api/run/active", timeout=3) as response:
                payload = json.loads(response.read().decode("utf-8"))
        self.assertEqual(response.status, 200)
        self.assertEqual(payload["job"], job)
        active.assert_called_once_with()

    def test_active_job_snapshot_only_returns_live_job(self) -> None:
        manager = JobManager(self.paths, self.defects, self.history)
        job_id = "job-live"
        manager._active_job_id = job_id
        manager._jobs[job_id] = {
            "id": job_id,
            "defect": "100",
            "status": "running",
            "nodes": {node: "pending" for node in ("validate", "interactive_reproduce", "agent", "apply", "build", "test", "record")},
            "verdict": "PENDING",
        }
        self.assertEqual(manager.active()["id"], job_id)
        manager._jobs[job_id]["status"] = "finalizing"
        self.assertEqual(manager.active()["status"], "finalizing")
        manager._jobs[job_id]["status"] = "completed"
        self.assertIsNone(manager.active())

    def test_job_stays_finalizing_until_history_is_saved(self) -> None:
        manager = JobManager(self.paths, self.defects, self.history)
        job_id = "job-finalize"
        manager._jobs[job_id] = {
            "id": job_id,
            "defect": "100",
            "status": "queued",
            "current_node": None,
            "nodes": {
                node: "pending"
                for node in ("validate", "interactive_reproduce", "agent", "apply", "build", "test", "record")
            },
            "verdict": "PENDING",
            "created_at": "2026-08-08T15:00:00+08:00",
            "started_at": None,
            "finished_at": None,
            "history_id": None,
            "error": None,
        }
        manager._active_job_id = job_id
        history_started = threading.Event()
        allow_history_finish = threading.Event()

        class FakeProcess:
            returncode = 0

            def __init__(self, argv: list[str]):
                self.argv = argv

            def communicate(self) -> tuple[str, str]:
                result_file = Path(self.argv[self.argv.index("--result-file") + 1])
                result_file.write_text(
                    json.dumps({"verdict": "FAIL", "attempts": 1, "error": None}),
                    encoding="utf-8",
                )
                return "done", ""

        def save_history(**_: object) -> str:
            history_started.set()
            self.assertTrue(allow_history_finish.wait(timeout=3))
            return "history-new"

        with (
            patch("frontend.server.subprocess.Popen", side_effect=lambda argv, **_: FakeProcess(argv)),
            patch.object(manager.history, "create", side_effect=save_history),
        ):
            worker = threading.Thread(target=manager._run, args=(job_id,), daemon=True)
            worker.start()
            self.assertTrue(history_started.wait(timeout=3))
            snapshot = manager.get(job_id)
            self.assertEqual(snapshot["status"], "finalizing")
            self.assertIsNone(snapshot["history_id"])
            allow_history_finish.set()
            worker.join(timeout=3)

        snapshot = manager.get(job_id)
        self.assertEqual(snapshot["status"], "completed")
        self.assertEqual(snapshot["history_id"], "history-new")

    def test_finalizing_job_still_blocks_a_second_repair(self) -> None:
        manager = JobManager(self.paths, self.defects, self.history)
        manager._active_job_id = "active"
        manager._jobs["active"] = {"id": "active", "status": "finalizing"}
        with self.assertRaisesRegex(RuntimeError, "已有修复任务"):
            manager.start(defect="100")

    def test_import_api_accepts_options_reports_progress_and_returns_conflict(self) -> None:
        application, base = self._server()
        job = {
            "id": "import1", "type": "import", "status": "running",
            "include_completed": True, "limit": 50, "force": True,
        }
        with patch("frontend.server.RequestHandler._start_import", return_value=job) as start:
            with self._post_json(
                base + "/api/defects/import",
                {"include_completed": True, "limit": 50, "force": True},
            ) as response:
                payload = json.loads(response.read().decode("utf-8"))
            self.assertEqual(response.status, 202)
            self.assertEqual(payload["id"], "import1")
            start.assert_called_once_with(include_completed=True, limit=50, force=True)

        for payload in (
            {"include_completed": "true", "limit": 0, "force": True},
            {"include_completed": False, "limit": -1, "force": True},
            {"include_completed": False, "limit": 0, "force": 1},
        ):
            with self.assertRaises(HTTPError) as raised:
                self._post_json(base + "/api/defects/import", payload)
            self.assertEqual(raised.exception.code, 400)

        with patch(
            "frontend.server.RequestHandler._start_import",
            side_effect=RuntimeError("已有导入任务 import1 正在运行"),
        ):
            with self.assertRaises(HTTPError) as raised:
                self._post_json(
                    base + "/api/defects/import",
                    {"include_completed": False, "limit": 0, "force": True},
                )
            self.assertEqual(raised.exception.code, 409)

        log_file = self.paths.runtime_jobs / "import1" / "import.log"
        log_file.parent.mkdir(parents=True, exist_ok=True)
        log_file.write_text("[import] 处理缺陷 100\n[import] 完成\n", encoding="utf-8")
        application.import_jobs["import1"] = {
            **job, "log_file": str(log_file), "finished_at": None, "error": None,
        }
        with urlopen(base + "/api/defects/import/import1", timeout=3) as response:
            progress = json.loads(response.read().decode("utf-8"))
        self.assertIn("处理缺陷 100", progress["stdout_tail"])

    def test_import_runner_respects_force_include_completed_and_limit(self) -> None:
        application = WebApplication(self.paths)
        handler = object.__new__(RequestHandler)
        handler.app = application
        log_file = self.paths.runtime_jobs / "runner" / "import.log"
        log_file.parent.mkdir(parents=True, exist_ok=True)

        application.import_jobs["runner"] = {"id": "runner", "status": "running"}
        with patch("frontend.server.subprocess.run", return_value=SimpleNamespace(returncode=0)) as run:
            handler._run_import("runner", True, 50, False, str(log_file))
        argv = run.call_args.args[0]
        self.assertIn("--include-completed", argv)
        self.assertEqual(argv[argv.index("--limit") + 1], "50")
        self.assertNotIn("--force", argv)

        application.import_jobs["runner2"] = {"id": "runner2", "status": "running"}
        with patch("frontend.server.subprocess.run", return_value=SimpleNamespace(returncode=0)) as run:
            handler._run_import("runner2", False, 0, True, str(log_file))
        argv = run.call_args.args[0]
        self.assertIn("--force", argv)
        self.assertNotIn("--include-completed", argv)
        self.assertNotIn("--limit", argv)


if __name__ == "__main__":
    unittest.main()
