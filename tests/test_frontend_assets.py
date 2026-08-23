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

    def test_hidden_attribute_always_hides_interactive_controls(self) -> None:
        self.assertIn("[hidden] { display: none !important; }", self.stylesheet)

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

    def test_all_evidence_images_use_the_shared_in_page_preview(self) -> None:
        for token in (
            'id="image-preview-dialog"',
            'id="image-preview-image"',
            'id="close-image-preview"',
        ):
            self.assertIn(token, self.index)
        for token in (
            "function imagePreviewLinkAttributes(url, label)",
            "function initImagePreview()",
            "data-image-preview",
            "dialog.showModal()",
            "event.target === dialog",
            "lastTrigger.focus()",
            "initImagePreview();",
        ):
            self.assertIn(token, self.javascript)
        self.assertEqual(self.javascript.count("imagePreviewLinkAttributes("), 5)
        self.assertNotIn('target="_blank"', self.javascript)
        self.assertNotIn("target='_blank'", self.javascript)
        for token in (".image-preview-trigger", "cursor: zoom-in;", ".image-preview-dialog", ".image-preview-stage"):
            self.assertIn(token, self.stylesheet)

    def test_defect_navigation_is_named_ones_defect_list(self) -> None:
        self.assertIn("ONES缺陷列表", self.index)
        self.assertIn("ONES缺陷列表", self.javascript)
        self.assertNotIn("缺陷闭环", self.index)
        self.assertNotIn("缺陷闭环", self.javascript)

    def test_run_form_submits_defect_and_explicit_project_only(self) -> None:
        self.assertIn("defect: String(defect.number)", self.javascript)
        self.assertIn("project: repairProject.project", self.javascript)
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

    def test_legacy_detail_renderers_ignore_stale_route_responses(self) -> None:
        for signature in (
            "renderTest(sheet, caseId)",
            "renderTestBatch(jobId)",
            "renderDefect(number)",
            "renderHistory(number, runId)",
            "renderTestHistory(sheet, caseId, runId)",
        ):
            with self.subTest(renderer=signature):
                start = self.javascript.index(f"async function {signature}")
                next_start = self.javascript.find("\nasync function ", start + 1)
                section = self.javascript[
                    start: next_start if next_start >= 0 else len(self.javascript)
                ]
                self.assertIn("const routeToken = routeRequestToken;", section)
                self.assertIn("if (routeToken !== routeRequestToken) return;", section)

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

    def test_hardware_settings_expose_real_on_demand_ble_device_manager(self) -> None:
        for token in (
            '<option value="ble">BLE 运行时自动发现 (实验)</option>',
            'id="cfg-hw-ble-options"',
            'id="cfg-hw-ble-address"',
            'id="cfg-hw-ble-scan-timeout"',
            'id="ble-device-search"',
            'placeholder="搜索设备名称或地址"',
            'id="btn-scan-ble"',
            'id="ble-connection-status"',
            'id="ble-discovered-list"',
            'id="ble-remembered-list"',
            "已连接过的设备",
            "不保持后台连接",
            "删除只清除本地记录",
        ):
            self.assertIn(token, self.index)
        for token in (
            "async function loadRememberedBleDevices()",
            "api('/api/hardware/ble/remembered')",
            "/api/hardware/ble/devices?timeout=",
            "api('/api/hardware/ble/connect'",
            "/api/hardware/ble/remembered/${encodeURIComponent(address)}",
            "data-ble-action=\"connect\"",
            "data-ble-action=\"delete\"",
            "bleDeviceMatches",
            "正在建立真实 GATT 连接并校验手表服务",
            "真实连接验证成功；连接已释放",
            "cfg.hardware?.ble_address || ''",
            "cfg.hardware?.ble_scan_timeout || 15",
            "ble_address: (document.querySelector('#cfg-hw-ble-address')?.value || '').trim()",
            "ble_scan_timeout: Number(document.querySelector('#cfg-hw-ble-scan-timeout')?.value) || 15",
        ):
            self.assertIn(token, self.javascript)
        load_start = self.javascript.index("async function loadSettings()")
        load_end = self.javascript.index("openBtn.addEventListener", load_start)
        initial_load = self.javascript[load_start:load_end]
        self.assertNotIn("/api/hardware/ble/devices", initial_load)
        self.assertNotIn("/api/hardware/ble/connect", initial_load)
        for token in (
            ".ble-device-columns",
            ".ble-device-card.is-selected",
            '.ble-connection-status[data-tone="success"]',
        ):
            self.assertIn(token, self.stylesheet)

    def test_hardware_settings_expose_supercom_port_selector(self) -> None:
        self.assertIn("SuperCom 端口设备", self.index)
        self.assertIn('<select id="cfg-hw-port" class="select" required', self.index)
        self.assertNotIn('<input id="cfg-hw-port"', self.index)
        self.assertIn('id="btn-refresh-serial-ports"', self.index)
        self.assertIn('id="cfg-hw-port-status"', self.index)

        for token in (
            "/api/hardware/serial-ports",
            "loadSerialPorts",
            "renderSerialPortOptions",
            "updateSerialPortStatus",
            "btnRefreshSerialPorts",
            "SuperCom 桥接管道已就绪",
            "SuperCom 桥接已开启",
            "data?.available === false",
            "Agent-loop 无法读取串口设备",
            "await loadSerialPorts()",
            "请选择 SuperCom 端口（检测到多个活动端口）",
            "未检测到 SuperCom 开启的串口（请在 SuperCom 中打开端口）",
        ):
            self.assertIn(token, self.javascript)

        for token in (
            ".serial-port-status-banner",
            '.serial-port-status-banner[data-tone="success"]',
            '.serial-port-status-banner[data-tone="warning"]',
            '.serial-port-status-banner[data-tone="error"]',
        ):
            self.assertIn(token, self.stylesheet)

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

    def test_report_quick_date_filters_keep_custom_dates_and_exact_24h_period(self) -> None:
        for token in (
            "const REPORT_DATE_PRESETS = [",
            "{value: '24h', label: '24小时', days: 2}",
            "{value: 'today', label: '今天', days: 1}",
            "{value: '7d', label: '7天', days: 7}",
            "{value: '30d', label: '30天', days: 30}",
            "function reportDatePresetRange(value)",
            "function activeReportDatePreset(filters = {})",
            "function reportQuery(project, filters = {})",
            "query.set('period', '24h')",
            "data-report-period=",
            "aria-label='快捷时间段'",
            "applyFilters({preservePeriod: true})",
            "root.querySelector('#report-from')?.addEventListener('change', () => applyFilters())",
            "root.querySelector('#report-to')?.addEventListener('change', () => applyFilters())",
        ):
            self.assertIn(token, self.javascript)
        for token in (
            ".report-period-options",
            ".report-period-option.is-active",
            ".report-period-field { grid-column: 1 / -1; }",
        ):
            self.assertIn(token, self.stylesheet)

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

    def test_agent_test_queue_uses_only_four_maturity_filters(self) -> None:
        for token in (
            "['all', 'all', '全部用例']",
            "['unexplored', 'unexplored', '尚未探索']",
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
        self.assertNotIn(
            "['externally_explored', 'externally_explored', '已经外部探索']",
            self.javascript,
        )
        self.assertIn("grid-template-columns: repeat(4, minmax(120px, 1fr));", self.stylesheet)
        self.assertIn("grid-template-columns: repeat(4, minmax(0, 1fr));", self.stylesheet)

    def test_agent_test_metrics_show_only_the_four_detailed_maturity_categories(self) -> None:
        for token in (
            "探索与固化",
            "分类依据探索账本和正式 case_map",
            "['all', 'all', '全部用例']",
            "['unexplored', 'unexplored', '尚未探索']",
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

    def test_interrupted_batch_notice_only_emphasizes_resume_message(self) -> None:
        self.assertIn(
            "job.resume_available ? `<strong>${escapeHtml(job.interruption_reason || '批次已中断')}，可从第 ${completed + 1} 条继续。</strong>`",
            self.javascript,
        )
        self.assertIn(
            "job.status === 'completed' ? '全部用例已执行完成。'",
            self.javascript,
        )
        self.assertIn(": '正在准备下一条用例…'", self.javascript)

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

    def test_overview_cold_start_defers_slow_secondary_queries(self) -> None:
        overview_start = self.javascript.index("function OverviewPage(")
        overview_end = self.javascript.index("function renderWorkflowStepper(", overview_start)
        overview_source = self.javascript[overview_start:overview_end]
        controller_start = overview_source.index("const controller = {")
        load_start = overview_source.index("async load()", controller_start)
        render_start = overview_source.index("render(data)", load_start)
        initial_load = overview_source[load_start:render_start]

        self.assertNotIn("/api/defects?page=1&page_size=6", initial_load)
        self.assertNotIn("/api/reports/summary?", initial_load)
        for token in (
            "async function loadDeferredSections(root)",
            "optionalApi('/api/defects?page=1&page_size=6')",
            "optionalApi(`/api/reports/summary?project=${encodeURIComponent(project)}`)",
            "patchRenderedSections(root, controller.render(nextData), 'data-overview-deferred')",
            "void loadDeferredSections(root);",
            "data-overview-deferred='report'",
            "data-overview-deferred='defects'",
            "data-overview-deferred='daily'",
        ):
            self.assertIn(token, overview_source)

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

    def test_detail_back_links_keep_safe_direct_origin(self) -> None:
        for token in (
            "function buildTestListUrl(query, page, state = DEFAULT_TEST_STATE, project = DEFAULT_TEST_PROJECT",
            "function safeInAppReturnUrl(raw, fallback = '')",
            "target.origin !== location.origin || !isInAppRoutePath(target.pathname)",
            "function withReturnContext(path, returnTo = currentRouteUrl())",
            "target.searchParams.set('from', safeReturnTo)",
            "link.hasAttribute('data-return-link') && canUseNativeBack(destination)",
            "const referrer = safeInAppReturnUrl(document.referrer);",
            "history.back();",
            "rememberReturnScroll(url);",
            "window.history.replaceState({...state, returnScrollUrl: current, returnScrollY: window.scrollY}, '', current);",
            "restoreRememberedScroll();",
            "testDetailHref(row.project, sheet, caseId, returnTo)",
            "testDetailHref(project, item.file_sheet || item.sheet, item.case_id, currentRouteUrl())",
            "const returnLabel = returnDestinationLabel(returnTo, '用例管理');",
            'data-return-link href="${escapeHtml(returnTo)}">← 返回${escapeHtml(returnLabel)}',
            "testHistoryHref(project, sheet, caseId, item.id, detailReturnTo)",
        ):
            self.assertIn(token, self.javascript)

    def test_batch_and_defect_detail_entries_keep_return_context(self) -> None:
        for token in (
            "function testBatchHref(jobId, returnTo = currentRouteUrl())",
            "returnTo && returnTo !== currentRouteUrl() ? returnTo : safeFallback",
            "window.location.href = testBatchHref(job.id);",
            "testBatchHref(activeBatch.id)",
            "const returnTo = currentRouteUrl();",
            "setActiveNav('runs');",
            "function defectDetailHref(number, returnTo = currentRouteUrl())",
            "const detailHref = defectDetailHref(row.number);",
            "pageReturnUrl(pageUrl('/defects', currentProject()))",
            "defectHistoryHref(number, item.id)",
            "const fallback = defectDetailHref(number, pageUrl('/defects', currentProject()));",
            "window.location.href = returnTo;",
        ):
            self.assertIn(token, self.javascript)

    def test_report_failure_rows_link_to_the_exact_run_and_keep_filters(self) -> None:
        for token in (
            "const historyId = item.history_id || item.run_id || '';",
            "testHistoryHref(project, sheet, caseId, historyId, returnTo)",
            "const reportReturnTo = currentRouteUrl();",
            "renderRecentFailures(report.recent_failures || [], project, reportReturnTo)",
            "const backHref = pageReturnUrl(detailFallback);",
            "const backLabel = returnDestinationLabel(backHref, '测试详情');",
            "'/reports': '测试报告'",
            "查看本次运行",
        ):
            self.assertIn(token, self.javascript)

    def test_report_keeps_product_failures_separate_from_execution_errors(self) -> None:
        for token in (
            "历史 ERROR",
            "高频产品失败模块",
            "仅按产品 FAIL 统计",
            "高频执行异常模块",
            "renderExecutionErrorModules(report.top_execution_error_modules || [])",
            "<th>产品 FAIL</th>",
            "<th>执行异常</th>",
            "executionAnomaly ? '执行异常' : workspaceVerdictLabel(verdict)",
        ):
            self.assertIn(token, self.javascript)
        self.assertNotIn("按 FAIL 与 ERROR 合计排序", self.javascript)

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
            "const cancellable = ['queued', 'running', 'orphaned'].includes(job.status);",
            "cancel.hidden = !cancellable;",
            "批次完成，有执行异常",
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

    def test_hardware_checkpoint_gallery_has_at_most_three_columns(self) -> None:
        for token in (
            "function screenshotGridClass(value)",
            "checkpoint-grid-hardware",
            'class="panel-body ${screenshotGridClass(projectMeta)}"',
            'class="panel-body ${screenshotGridClass(record)}"',
            ".checkpoint-grid-hardware { grid-template-columns: repeat(3, minmax(0, 1fr)); }",
            ".checkpoint-grid-hardware { grid-template-columns: repeat(2, minmax(0, 1fr)); }",
            ".checkpoint-grid-hardware { grid-template-columns: 1fr; }",
        ):
            self.assertIn(token, self.javascript + self.stylesheet)
        self.assertIn(
            ".checkpoint-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 15px; }",
            self.stylesheet,
        )


if __name__ == "__main__":
    unittest.main()
