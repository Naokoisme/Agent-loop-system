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
        self.assertIn("自动化测试平台", self.index)
        for text in ("已通过", "失败", "无法验证", "未运行", "生成方案", "代码改动", "修复前", "修复后"):
            self.assertIn(text, self.javascript)
        self.assertIn("chip-warning", self.javascript)

    def test_llm_settings_keep_configuration_runtime_and_probe_independent(self) -> None:
        for token in (
            'id="llm-config-status"',
            'id="llm-actual-success"',
            'id="llm-test-status"',
            "服务已配置",
            "最近实际调用成功",
            "本次快速探测",
            "快速探测只反映本次请求，不会覆盖真实 Agent 调用状态。",
        ):
            self.assertIn(token, self.index)
        for token in (
            "cfg.llm?.configured === true",
            "cfg.llm?.last_actual_success_at",
            "本次探测失败",
            "setLlmSignal(llmTestStatus",
        ):
            self.assertIn(token, self.javascript)
        self.assertNotIn("已就绪 (开箱即用)", self.index)
        self.assertNotIn("❌ 连接失败", self.javascript)
        self.assertIn(".llm-signal-list", self.stylesheet)

    def test_global_toast_host_exists_for_action_feedback(self) -> None:
        self.assertIn('<div id="toast" class="toast" role="status" aria-live="polite"></div>', self.index)
        self.assertIn("const toast = document.querySelector('#toast');", self.javascript)

    def test_case_and_report_exports_use_direct_browser_downloads(self) -> None:
        for token in (
            "function startBrowserDownload(url, filename)",
            "a.href = url;",
            "a.download = filename;",
            "exportExcelBtn.addEventListener('click', () => {",
            "startBrowserDownload(url, `test_cases_${curProj}.xlsx`);",
            "测试用例表下载已开始",
            "[data-report-export]:not(:disabled)')?.addEventListener('click', () => {",
            "startBrowserDownload(url, `test_report_${project}.xlsx`);",
            "测试报告下载已开始",
        ):
            self.assertIn(token, self.javascript)
        self.assertEqual(self.javascript.count("startBrowserDownload(url, `"), 2)

    def test_search_stays_inline_and_async_results_never_overwrite_input(self) -> None:
        for token in ("data-page", "page_size=${PAGE_SIZE}"):
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
        ):
            self.assertIn(token, self.javascript)

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

    def test_agent_test_pages_reuse_workbench_patterns(self) -> None:
        for token in (
            'data-nav="cases"',
            "async function renderTests()",
            "async function renderTest(sheet, caseId)",
            "async function renderTestHistory(sheet, caseId, runId)",
            "async function renderTestBatch(jobId)",
            "/api/tests/run",
            "/api/tests/run-batch",
            "restoreActiveTest",
            "测试语义",
            "命令映射",
            "测试历史",
        ):
            target = self.index if token == 'data-nav="cases"' else self.javascript
            self.assertIn(token, target)
        for token in (".primary-nav", ".case-text", ".batch-progress-bar"):
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

    def test_agent_test_queue_uses_only_five_maturity_filters(self) -> None:
        for token in (
            "['all', 'all', '全部用例']",
            "['unexplored', 'unexplored', '尚未外部探索']",
            "['externally_explored', 'externally_explored', '已经外部探索']",
            "['explored_unsolidified', 'explored_unsolidified', '已探索但未固化']",
            "['solidified', 'solidified', '已固化、Agent-loop 可执行']",
            'data-test-filter="${state}"',
        ):
            self.assertIn(token, self.javascript)
        for token in (
            "['untested', 'untested', '尚未运行']",
            "['pass', 'pass', '最近通过']",
            "['fail', 'fail', '最近失败']",
            "['cannot_verify', 'cannot_verify', '最近无法验证']",
            "['error', 'error', '执行异常']",
        ):
            self.assertNotIn(token, self.javascript)
        self.assertIn("grid-template-columns: repeat(5, minmax(120px, 1fr));", self.stylesheet)

    def test_agent_test_metrics_show_only_the_five_maturity_categories(self) -> None:
        for token in (
            "外部探索与固化",
            "分类只取外部账本和正式 case_map",
            "['all', 'all', '全部用例']",
            "['unexplored', 'unexplored', '尚未外部探索']",
            "['externally_explored', 'externally_explored', '已经外部探索']",
            "['explored_unsolidified', 'explored_unsolidified', '已探索但未固化']",
            "['solidified', 'solidified', '已固化、Agent-loop 可执行']",
        ):
            self.assertIn(token, self.javascript)
        self.assertIn('class="test-metric-groups"', self.javascript)
        self.assertIn('class="metrics test-metric-grid maturity-metrics"', self.javascript)
        self.assertNotIn('class="metrics test-metric-grid run-metrics"', self.javascript)
        self.assertNotIn("最近一次 Agent-loop 结果", self.javascript)
        self.assertIn("updateBatchLaunch(payload.batch_summary)", self.javascript)
        self.assertNotIn("updateBatchLaunch(payload.summary)", self.javascript)
        self.assertNotIn("updateBatchLaunch(summary.summary)", self.javascript)
        self.assertIn("catch (refreshError)", self.javascript)
        self.assertIn("updateBatchLaunch();", self.javascript)
        self.assertIn(".test-metric-group {", self.stylesheet)
        self.assertNotIn(".asset-metrics", self.stylesheet)

    def test_case_catalog_cold_start_uses_compact_server_queries(self) -> None:
        for token in (
            "const payload = await api(`/api/tests?${request.toString()}`);",
            "request.append('module', name)",
            "payload.module_counts",
            "/api/tests/overview?",
            "/api/tests/recent?",
            "api('/api/tests/projects')",
        ):
            self.assertIn(token, self.javascript)
        self.assertNotIn(
            "const catalog = await loadCaseCatalog(project, {force});",
            self.javascript,
        )
        self.assertEqual(self.javascript.count("loadCaseCatalog(project)"), 1)

    def test_workspace_typography_scale_and_overview_shortcuts_are_clean(self) -> None:
        for token in (
            "--font-size-micro: 11px",
            "--font-size-caption: 12px",
            "--font-size-secondary: 13px",
            "--font-size-table: 14px",
            "--font-size-body: 15px",
            "--font-size-section-title: 17px",
            "font-size: var(--font-size-body);",
            "font-size: var(--font-size-table);",
            "grid-template-columns: 1.08fr .92fr;",
        ):
            self.assertIn(token, self.stylesheet)
        self.assertNotIn("快捷操作", self.javascript)
        self.assertNotIn(".quick-action-grid", self.stylesheet)
        self.assertNotIn(".quick-actions-panel", self.stylesheet)

    def test_agent_test_queue_defaults_to_all_cases(self) -> None:
        for token in (
            "const DEFAULT_TEST_STATE = 'all';",
            "DEFAULT_TEST_STATE",
        ):
            self.assertIn(token, self.javascript)
        for token in (
            "const DEFAULT_TEST_STATE = 'executable';",
            "当前只展示能被 Agent-loop 执行的用例",
            "未固化已运行",
            "暂不可执行",
        ):
            self.assertNotIn(token, self.javascript)

    def test_agent_test_rows_show_one_derived_status_and_execution_mode(self) -> None:
        for token in (
            "function maturityChip(row = {})",
            "function caseStatusChip(row = {})",
            "已固化",
            "未固化",
            "尚未探索",
            "${caseStatusChip(row)}",
            "caseStatusChip({...testCase, latest_verdict: initialVerdict})",
            "const usesFixedMapping = Boolean(testCase.is_promoted);",
            "本条将临时探索",
            "不会写入外部探索账本",
            "生成候选、复跑并晋升",
            "candidate_replay_started",
            "Boolean(job.promotion_flow)",
            "job.promotion_status === 'promoted'",
            "候选复跑未达晋升门禁，已自动回滚",
            "historyCount > 0",
            "? rawVerdict : 'ERROR'",
        ):
            self.assertIn(token, self.javascript)
        for token in (
            "PASS: 'PASS'",
            "FAIL: 'FAIL'",
            "CANNOT_VERIFY: 'CANNOT_VERIFY'",
            "SKIP: 'CANNOT_VERIFY'",
            "ERROR: 'ERROR'",
        ):
            self.assertIn(token, self.javascript)
        self.assertNotIn("function executionCapabilityChip", self.javascript)
        self.assertNotIn("if (row.unable)", self.javascript)
        self.assertIn(".chip-solidified", self.stylesheet)
        self.assertIn(".chip-unsolidified", self.stylesheet)
        self.assertIn(".chip-unexplored", self.stylesheet)

    def test_agent_test_project_hint_tracks_the_loaded_project(self) -> None:
        self.assertIn('id="test-project-target"', self.javascript)
        self.assertIn(
            "projectTarget.innerHTML = testTargetChip(payload)",
            self.javascript,
        )

    def test_agent_test_detail_back_link_keeps_list_filter_and_page(self) -> None:
        for token in (
            "function buildTestListUrl(query, page, state = DEFAULT_TEST_STATE, project = DEFAULT_TEST_PROJECT",
            "function testReturnUrl()",
            "new URLSearchParams({project: testProject(project).project, from: returnTo})",
            "testDetailHref(row.project, sheet, caseId, returnTo)",
            'href="${escapeHtml(returnTo)}">← 返回测试用例',
            "testHistoryHref(project, sheet, caseId, item.id, returnTo)",
        ):
            self.assertIn(token, self.javascript)

    def test_report_failure_rows_link_to_the_exact_run_and_keep_filters(self) -> None:
        for token in (
            "const historyId = item.history_id || item.run_id || '';",
            "testHistoryHref(project, sheet, caseId, historyId, returnTo)",
            "const reportReturnTo = pageUrl('/reports', project, {view: filters.view, from: filters.from, to: filters.to, module: filters.module});",
            "renderRecentFailures(report.recent_failures || [], project, reportReturnTo)",
            "function testReportReturnUrl()",
            "const backHref = reportReturnTo || testDetailHref(project, sheet, caseId, returnTo);",
            "← 返回测试报告",
            "查看本次运行",
        ):
            self.assertIn(token, self.javascript)

    def test_batch_launch_selects_latest_result_categories_and_excludes_pass(self) -> None:
        for token in (
            'name="batch-category"',
            "untested: '尚未运行'",
            "fail: '最近运行失败'",
            "cannot_verify: '最近无法验证'",
            "error: '最近执行异常'",
            "latestBatchCandidateSummary.pass",
            "最新结果已通过的用例不会重跑",
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

    def test_workspace_live_polling_updates_sections_without_rerouting(self) -> None:
        for token in (
            "function patchRenderedSections(root, markup, attributeName)",
            "patchRenderedSections(root, controller.render(nextData), 'data-overview-live')",
            "patchRenderedSections(root, controller.render(nextData), 'data-execution-live')",
            "refreshTimer = setTimeout(() => refreshLiveSections(root), 2000)",
            "pollTimer = setTimeout(() => refreshLiveSections(root), 2000)",
            "root.addEventListener('click', clickHandler)",
            "window.scrollTo(scrollX, scrollY)",
        ):
            self.assertIn(token, self.javascript)
        self.assertNotIn("setTimeout(() => route(), 2000)", self.javascript)

    def test_empty_checkpoint_gallery_is_compact(self) -> None:
        for token in (
            ".checkpoint-grid .evidence-frame img { aspect-ratio: 402 / 256; }",
            ".checkpoint-grid .evidence-missing {",
            "grid-column: 1 / -1;",
            "min-height: 96px;",
            "aspect-ratio: auto;",
        ):
            self.assertIn(token, self.stylesheet)


if __name__ == "__main__":
    unittest.main()
