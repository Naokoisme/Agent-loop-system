from __future__ import annotations

import unittest
from pathlib import Path


class FrontendAssetsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        root = Path(__file__).resolve().parents[1]
        cls.index = (root / "frontend" / "index.html").read_text(encoding="utf-8")
        cls.javascript = (root / "frontend" / "app.js").read_text(encoding="utf-8")
        cls.stylesheet = (root / "frontend" / "styles.css").read_text(encoding="utf-8")

    def test_source_location_panel_is_collapsible_and_large_results_start_closed(self) -> None:
        self.assertIn('<details class="panel collapsible-panel"', self.javascript)
        self.assertIn("sourceMatches.length < 3", self.javascript)
        self.assertIn('.collapse-action::after { content: "展开 ↓"; }', self.stylesheet)
        self.assertIn(".collapsible-panel[open]", self.stylesheet)

    def test_description_image_placeholder_uses_local_defect_image(self) -> None:
        for token in (
            "function defectDescription(defect)",
            "description.split(/(\\[image\\])/g)",
            "item?.kind === 'image' && item?.url",
            "缺陷图片未找到",
            "defect-description-image",
        ):
            self.assertIn(token, self.javascript if token != "defect-description-image" else self.stylesheet)

    def test_run_form_only_submits_defect(self) -> None:
        self.assertIn("JSON.stringify({ defect: String(defect.number) })", self.javascript)
        self.assertNotIn('id="sheet"', self.javascript)
        self.assertNotIn('id="test-case"', self.javascript)
        self.assertNotIn('id="source-file"', self.javascript)
        self.assertNotIn("/api/cases?", self.javascript)

    def test_second_run_resets_workflow_without_replacing_status_node(self) -> None:
        self.assertIn("updateWorkflow({});", self.javascript)
        self.assertIn("setResultChip(chip, job.verdict);", self.javascript)
        self.assertIn("chip.textContent = '任务已创建';", self.javascript)
        self.assertNotIn("chip.outerHTML = resultChip", self.javascript)

    def test_history_refresh_waits_until_record_is_saved(self) -> None:
        self.assertIn("job.status === 'finalizing'", self.javascript)
        self.assertIn("保存记录中", self.javascript)
        self.assertIn("正在保存修复历史", self.javascript)
        self.assertIn("historyPayload.history", self.javascript)

    def test_history_uses_canonical_evidence_and_explicit_legacy_state(self) -> None:
        for token in (
            "record.verdict_reasons",
            "record.test_commands",
            "record.llm_thinking_steps",
            "record?.legacy_record",
            "本次记录保存异常：缺少",
            "当前版本符合预期，本次无需修改代码",
            "LLM 思考步骤",
            "复现 Agent 每轮输出的决策理由",
        ):
            self.assertIn(token, self.javascript)
        self.assertNotIn("record.test_result?.results", self.javascript)
        self.assertNotIn("终端原始数据与判定", self.javascript)
        self.assertNotIn("JSON.stringify(rawEvidence", self.javascript)

    def test_active_repair_is_restored_after_page_reload(self) -> None:
        for token in (
            "/api/run/active",
            "restoreActiveRepair",
            "await restoreActiveRepair(String(defect.number))",
            "已找回任务",
            "其他任务运行中",
            "正在修复…",
        ):
            self.assertIn(token, self.javascript)

    def test_visible_product_copy_is_chinese_and_tristate_is_distinct(self) -> None:
        self.assertIn("固件修复工作台", self.index)
        self.assertNotIn("Agent Loop", self.index)
        for text in ("已通过", "失败", "无法验证", "未运行", "生成方案", "代码改动", "修复前", "修复后"):
            self.assertIn(text, self.javascript)
        self.assertIn("chip-warning", self.javascript)

    def test_search_stays_inline_and_async_results_never_overwrite_input(self) -> None:
        for token in ("260", "data-page", "page_size=${PAGE_SIZE}", "listRequestToken += 1"):
            self.assertIn(token, self.javascript)
        self.assertNotIn("search-active", self.javascript)
        self.assertNotIn("search-backdrop", self.javascript)
        self.assertNotIn("input.value = query", self.javascript)
        self.assertNotIn("body.search-active", self.stylesheet)
        self.assertIn(".pagination", self.stylesheet)

    def test_metric_cards_filter_the_defect_queue_and_keep_url_state(self) -> None:
        for token in (
            'data-result-filter="${result}"',
            "result=${encodeURIComponent(resultFilter)}",
            "params.set('result', resultFilter)",
            "RESULT_FILTER_LABELS[selectedResult]",
            "metric.is-active",
        ):
            self.assertIn(token, self.javascript if token != "metric.is-active" else self.stylesheet)

    def test_defect_rows_use_compact_vertical_spacing(self) -> None:
        self.assertIn("min-height: 72px", self.stylesheet)
        self.assertIn("padding: 12px 20px", self.stylesheet)
        self.assertNotIn("min-height: 90px", self.stylesheet)

    def test_defect_import_dialog_and_persistent_progress_are_implemented(self) -> None:
        for token in (
            'id="open-import"', 'id="import-dialog"', 'name="import-type"',
            'id="import-limit"', 'id="import-force"', '/api/defects/import',
            "setTimeout(() => pollImport(jobId), 3000)", "localStorage.setItem",
            "展开完整日志", "已有拉取任务在运行",
        ):
            self.assertIn(token, self.javascript)
        self.assertIn(".import-dialog", self.stylesheet)
        self.assertIn(".import-progress", self.stylesheet)

    def test_neutral_workbench_visual_drops_grid_and_large_green_brand_block(self) -> None:
        self.assertIn("--canvas: #f3efe7", self.stylesheet)
        self.assertIn("--surface: #fffdf9", self.stylesheet)
        self.assertNotIn("linear-gradient(", self.stylesheet)
        self.assertNotIn("background: var(--brand)", self.stylesheet)

    def test_agent_test_pages_reuse_workbench_patterns(self) -> None:
        for token in (
            'data-nav="tests"',
            "async function renderTests()",
            "async function renderTest(sheet, caseId)",
            "async function renderTestHistory(sheet, caseId, runId)",
            "async function renderTestBatch(jobId)",
            "/api/tests/run",
            "/api/tests/run-batch",
            "batch-live-screenshots",
            "restoreActiveTest",
            "testMetricCards",
            "testWorkflowSvg",
            "测试语义",
            "命令映射",
            "测试历史",
        ):
            target = self.index if token == 'data-nav="tests"' else self.javascript
            self.assertIn(token, target)
        for token in (".primary-nav", ".test-row", ".case-text", ".command-phase", ".batch-progress-bar", ".batch-result-row"):
            self.assertIn(token, self.stylesheet)

    def test_agent_test_history_shows_actual_trace_and_evidence_gate(self) -> None:
        for token in (
            "function actualCommandTrace(items = [])",
            "record.command_trace",
            "Runner 自动加入的栅栏、等待和截图",
            "function evidenceContractEvidence(record)",
            "证据合同不完整",
            "命令映射只能说明计划",
            "item.checkpoint_index",
        ):
            self.assertIn(token, self.javascript)
        for token in (".command-trace-list", ".command-trace-item", ".trace-meta"):
            self.assertIn(token, self.stylesheet)

    def test_agent_test_queue_can_filter_latest_pass_results(self) -> None:
        for token in (
            "pass: '已通过'",
            "['pass', 'pass', '已通过']",
            'data-test-filter="${state}"',
        ):
            self.assertIn(token, self.javascript)
        self.assertIn("grid-template-columns: repeat(6, minmax(110px, 1fr))", self.stylesheet)

    def test_agent_test_detail_back_link_keeps_list_filter_and_page(self) -> None:
        for token in (
            "function buildTestListUrl(query, page, state = 'all', project = DEFAULT_TEST_PROJECT)",
            "function testReturnUrl()",
            "new URLSearchParams({project: testProject(project).project, from: returnTo})",
            "testDetailHref(row.project, sheet, caseId, returnTo)",
            'href="${escapeHtml(returnTo)}">← 返回测试用例',
            "testHistoryHref(project, sheet, caseId, item.id, returnTo)",
            'class="back-link test-list-back-link"',
        ):
            self.assertIn(token, self.javascript)
        # 批次页也使用携带 project 的 returnTo，不再写死到默认项目列表。
        self.assertEqual(
            self.javascript.count(
                '<a class="back-link" href="${escapeHtml(returnTo)}">← 返回测试用例</a>'
            ),
            1,
        )
        for token in (
            ".test-list-back-link {",
            "position: sticky",
            "top: 80px",
            "z-index: 15",
        ):
            self.assertIn(token, self.stylesheet)

    def test_batch_launch_selects_latest_result_categories_and_excludes_pass(self) -> None:
        for token in (
            'name="batch-category"',
            "untested: '未测过'",
            "fail: '测过 FAIL'",
            "cannot_verify: '测过无法验证'",
            "latestBatchCandidateSummary.pass",
            "最新结果为 PASS 的用例不会重跑",
            "JSON.stringify({limit: 0, categories, project: projectSelect.value})",
            "batch-resume-button",
            "/resume",
            "继续运行剩余",
        ):
            self.assertIn(token, self.javascript)
        for token in (".batch-scope", ".batch-option", ".batch-launch-actions"):
            self.assertIn(token, self.stylesheet)

    def test_hardware_and_simulator_active_jobs_are_rendered_independently(self) -> None:
        for token in (
            "function activeTestJobs(payload = {})",
            "function activeTestJobForProject(payload, project)",
            "payload.jobs",
            "activeTestJobForProject(payload, project)",
            'class="batch-active-row"',
        ):
            self.assertIn(token, self.javascript)
        self.assertIn(".batch-active-row", self.stylesheet)


if __name__ == "__main__":
    unittest.main()
