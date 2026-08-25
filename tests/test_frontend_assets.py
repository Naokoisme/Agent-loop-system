from __future__ import annotations

import re
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
        self.assertIn("<div><h2>诊断信息</h2><p>源码位置</p></div>", self.javascript)
        self.assertNotIn("sourceMatches.length < 3", self.javascript)
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
        self.assertIn("ONES 缺陷", self.index)
        self.assertIn("ONES 缺陷", self.javascript)
        self.assertNotIn("ONES缺陷列表", self.index)
        self.assertNotIn("ONES缺陷列表", self.javascript)
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
        self.assertIn("chip.textContent = '正在启动';", self.javascript)
        self.assertNotIn("chip.textContent = '任务已创建';", self.javascript)
        self.assertNotIn("chip.outerHTML = resultChip", self.javascript)

    def test_history_refresh_waits_until_record_is_saved(self) -> None:
        self.assertIn("job.status === 'finalizing'", self.javascript)
        self.assertIn("保存记录中", self.javascript)
        self.assertIn("修复已结束，正在保存结果", self.javascript)
        self.assertIn("historyPayload.history", self.javascript)

    def test_history_uses_canonical_evidence_and_explicit_legacy_state(self) -> None:
        for token in (
            "record.verdict_reasons",
            "record.test_commands",
            "record.llm_thinking_steps",
            "record?.legacy_record",
            "本次记录保存异常：缺少",
            "当前版本符合预期，本次无需修改代码",
            "分析过程",
            "每一步的判断依据",
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
            "修复仍在运行，已恢复最新进度",
            "其他任务运行中",
            "正在修复…",
        ):
            self.assertIn(token, self.javascript)

    def test_visible_product_copy_is_chinese_and_tristate_is_distinct(self) -> None:
        self.assertIn("Agent-loop 自动化测试", self.index)
        for text in ("已通过", "失败", "执行异常", "无法验证", "未运行", "生成自动化步骤", "代码改动", "修复前", "修复后"):
            self.assertIn(text, self.javascript)
        self.assertIn("chip-warning", self.javascript)

    def test_visible_chinese_copy_uses_chinese_punctuation(self) -> None:
        combined = self.index + self.javascript
        for text in (
            "模型：",
            "服务地址：",
            "账号（邮箱）",
            "例如：",
            "新增用例：",
            "文件（.xlsx）",
        ):
            with self.subTest(text=text):
                self.assertIn(text, combined)

        for text in ("模型:", "服务地址:", "账号 (邮箱)", "例如:", "新增用例:", "文件 (.xlsx)"):
            with self.subTest(text=text):
                self.assertNotIn(text, combined)

    def test_default_markup_does_not_expose_api_routes_or_engineering_explanations(self) -> None:
        html_line = re.compile(
            r"<(?:article|aside|button|details|div|form|h[1-6]|header|label|li|p|section|small|span|summary|table)\b"
        )
        visible_markup = "\n".join(
            line
            for source in (self.index, self.javascript)
            for line in source.splitlines()
            if html_line.search(line)
        )
        self.assertNotIn("/api/", visible_markup)

        for text in (
            "已固化、Agent-loop 可执行",
            "分类依据探索账本和正式 case_map",
            "当前后端未提供",
            "未伪造历史趋势",
            "空缺项不以 0 冒充",
            "运行期间每 2 秒",
            "Runner 自动加入",
            "写入 PROMOTED",
            "安全防御拦截（DIVERGED）",
            "actions 实际尝试",
            "证据合同完整",
            "Socket 命令通道",
            "SuperCom 命名管道",
            "USB MTP 截图",
            "历史执行异常",
            "新建修复任务",
            "任务 ID",
        ):
            with self.subTest(text=text):
                self.assertNotIn(text, self.index + self.javascript)

    def test_test_detail_primary_markup_uses_product_language(self) -> None:
        render_start = self.javascript.index("async function renderTest(sheet, caseId)")
        markup_start = self.javascript.index("app.innerHTML = `", render_start)
        markup_end = self.javascript.index("\n\n  const promoteBtn", markup_start)
        primary_markup = self.javascript[markup_start:markup_end]

        for text in ("用例内容", "自动化步骤", "首次运行", "测试进度", "验证并保存步骤"):
            self.assertIn(text, primary_markup)
        for text in (
            "/api/",
            "PROMOTED",
            "DIVERGED",
            "case_map",
            "Runner",
            "evidence_contract",
            "command_trace",
            "promotion_flow",
            "job_id",
        ):
            with self.subTest(text=text):
                self.assertNotIn(text, primary_markup)

    def test_errors_keep_raw_diagnostics_behind_product_summaries(self) -> None:
        for token in (
            "function issuePresentation(",
            "function knownIssueSummary(",
            "function safeProductCopy(",
            "本次操作没有完成，平台暂时无法确定具体原因。",
            "请重新操作；如再次出现，展开诊断信息并联系维护人员。",
            "平台暂时无法连接本机服务。",
            "平台找到了连接入口，但没有收到手表响应。",
            "请确认 SuperCom 连接的是当前手表，并唤醒手表屏幕后重试。",
            "电脑检测到了手表，但暂时无法读取截图。",
            "请重新连接 USB，并确认电脑能够打开手表存储后重试。",
            "error.diagnosticMessage = diagnosticMessage;",
            "error.code = 'NETWORK_ERROR';",
            "error.payload = payload;",
            "error.presentation = presentation;",
            "TECHNICAL_DIAGNOSTIC_PATTERN",
            "(?:Error|Exception)",
            '<details class="import-log-details"><summary>诊断信息',
            '<details class="inline-diagnostics"><summary>诊断信息</summary>',
        ):
            self.assertIn(token, self.javascript)

    def test_destructive_confirmations_explain_irreversible_effects(self) -> None:
        for token in (
            "已执行的设备操作无法撤回",
            "之后可继续剩余用例",
            "删除后无法恢复",
            "当前用例完成后暂停",
        ):
            self.assertIn(token, self.javascript)

    def test_environment_checks_use_product_summaries_with_collapsed_diagnostics(self) -> None:
        for token in (
            "const ENVIRONMENT_CHECK_COPY",
            "function environmentCheckPresentation(",
            "function environmentLogRows(",
            "项目文件需要配置",
            "测试程序尚未准备好",
            "模型服务尚未配置或不可用",
            "正在检查…",
            "const refreshedHealth = environmentHealth(response?.result);",
            "环境检查完成：${refreshedHealth.label}",
            "environmentLogRows(environmentItem.logs, targetHealth)",
            "检查操作、截图和必要服务是否可用",
            "可用项",
            "<summary>诊断信息</summary>",
        ):
            self.assertIn(token, self.javascript)

        check_copy_start = self.javascript.index("const ENVIRONMENT_CHECK_COPY")
        check_copy_end = self.javascript.index("function environmentLogRows(", check_copy_start)
        check_copy = self.javascript[check_copy_start:check_copy_end]
        for token in (
            "label: '手表连接'",
            "label: 'USB 连接'",
            "label: '截图读取'",
            "label: '手表响应'",
            "resolveIssueDefinition(item.code)",
            "问题原因：",
            "处理方法：",
        ):
            with self.subTest(token=token):
                self.assertIn(token, check_copy)
        for token in (
            "SuperCom 管道",
            "MTP 命名空间",
            "UART/GUI 数据面",
            "environment-check-code",
            "固件",
            "命令接口",
            "截图服务",
        ):
            with self.subTest(token=token):
                self.assertNotIn(token, check_copy)
        for token in (".environment-check-detail", ".environment-log .inline-diagnostics", ".check-field-label"):
            self.assertIn(token, self.stylesheet)

    def test_bluetooth_is_a_top_level_workbench_not_a_settings_manager(self) -> None:
        for token in (
            'href="/bluetooth" data-route="/bluetooth" data-nav="bluetooth"',
            'id="global-project-switch"',
            "前往蓝牙工作台",
        ):
            self.assertIn(token, self.index)

        for token in (
            "当前固件阻塞",
            "WATCH_579_EXECUTION_BLOCKED",
            "平台已阻止单条、批次和候选复跑",
        ):
            self.assertIn(token, self.javascript)
        for old_id in (
            'id="cfg-hw-ble-options"',
            'id="cfg-hw-ble-address"',
            'id="cfg-hw-ble-scan-timeout"',
            'id="ble-discovered-list"',
            'id="ble-remembered-list"',
        ):
            self.assertNotIn(old_id, self.index)
        for token in (
            "function BluetoothPage()",
            "'/api/hardware/579/status'",
            "'/api/hardware/579/connect'",
            "'/api/hardware/579/disconnect'",
            "'/api/hardware/579/preview'",
            "'/api/hardware/579/send'",
            "'/api/hardware/ble/connect'",
            "hardware_579: {ble_address:",
            "hardware: {ble_address:",
            "data-bt-preset=\"find\"",
            "data-bt-preset=\"calc\"",
            "data-bt-preset=\"button\"",
            "effect_verified=false",
            "latestStatus?.lease?.active",
            "data-ble-mutable",
        ):
            self.assertIn(token, self.javascript)
        for token in (
            ".bluetooth-workbench-grid",
            ".bluetooth-command-fields",
            ".bluetooth-event-log",
        ):
            self.assertIn(token, self.stylesheet)

    def test_ble_discovery_copy_uses_plain_device_language(self) -> None:
        for token in (
            "扫描结果只用于精确选择地址",
            "找到 ${devices.length} 个蓝牙设备",
            "没有匹配的扫描结果",
            "未命名设备",
            "点击“扫描”查找附近设备",
        ):
            self.assertIn(token, self.javascript)
        for token in (
            "正在查找附近的手表",
            "发现 ${bleDiscoveredDevices.length} 台手表",
            "本次扫描未发现匹配手表",
            "BLE 超时必须",
        ):
            self.assertNotIn(token, self.javascript)

    def test_hardware_settings_expose_supercom_port_selector(self) -> None:
        self.assertIn("SuperCom 端口", self.index)
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
            "SuperCom 已就绪",
            "data?.available === false",
            "无法读取串口设备，请检查系统权限后重试",
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
            "data-llm-test-summary",
            "data-llm-test-action",
            "data-llm-test-diagnostics",
            "配置状态",
            "最近调用",
            "连接测试",
            "只检查当前连接，不影响已有运行记录。",
        ):
            self.assertIn(token, self.index)
        for token in (
            "cfg.llm?.configured === true",
            "cfg.llm?.last_actual_success_at",
            "setLlmTestIssue(",
            "data-llm-test-diagnostics",
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
            "测试用例表已开始下载",
            "[data-report-export]:not(:disabled)')?.addEventListener('click', () => {",
            "startBrowserDownload(url, `test_report_${project}.xlsx`);",
            "测试报告已开始下载",
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

    def test_case_management_uses_one_server_paginated_request(self) -> None:
        for token in (
            "async function loadCasePage(project",
            "page_size: String(PAGE_SIZE)",
            "modules.forEach(name => params.append('module', name))",
            "const payload = await loadCasePage(project",
            "renderModuleSidebar(payload.module_counts",
        ):
            self.assertIn(token, self.javascript)

    def test_case_management_requires_platform_selection_before_loading_cases(self) -> None:
        for token in (
            "async function renderCasePlatformLanding()",
            "if (!['w30', '579'].includes(requestedPlatform)) return renderCasePlatformLanding();",
            'data-case-platform="w30"',
            'data-case-platform="579"',
            "进入 W30 用例管理",
            "进入 579 用例管理",
            "caseProjectsForPlatform(initialPlatform).map",
            "切换平台后，只加载该平台下的项目与用例",
        ):
            self.assertIn(token, self.javascript)
        for selector in (
            ".case-platform-gateway-grid",
            ".case-platform-entry",
            ".case-platform-context",
        ):
            self.assertIn(selector, self.stylesheet)
        self.assertIn('<form class="case-platform-entry is-w30" method="get" action="/cases"', self.javascript)
        self.assertIn('<form class="case-platform-entry is-579" method="get" action="/cases"', self.javascript)
        self.assertIn('<input type="hidden" name="platform_id" value="w30">', self.javascript)
        self.assertIn('<input type="hidden" name="platform_id" value="579">', self.javascript)
        self.assertIn('<button class="case-platform-entry-submit" type="submit">', self.javascript)
        self.assertIn('.case-platform-entry-submit', self.stylesheet)
        self.assertIn('<form class="case-platform-switch-form" method="get" action="/cases"', self.javascript)
        self.assertIn('.case-platform-switch-form', self.stylesheet)
        self.assertNotIn('data-native-navigation="true"', self.javascript)

    def test_case_project_switch_keeps_or_selects_a_compatible_platform(self) -> None:
        for token in (
            "const requestedPlatform = new URLSearchParams(location.search).get('platform_id');",
            "const allowedPlatforms = projectProfile.allowed_platforms || [];",
            "allowedPlatforms.includes(requestedPlatform)",
            "{platform_id: compatiblePlatform}",
            "pageUrl('/cases', project, {platform_id: initialPlatform})",
        ):
            self.assertIn(token, self.javascript)

    def test_frontend_assets_are_versioned_for_external_browser_refresh(self) -> None:
        self.assertIn('/assets/styles.css?v=20260824-3', self.index)
        self.assertIn('/assets/app.js?v=20260824-3', self.index)

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
            "诊断信息", "已有同步任务在运行",
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
            "用例内容",
            "执行计划",
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
            "系统补充",
            "function evidenceContractEvidence(record)",
            "证据不完整",
            "本次记录没有保存执行步骤。下面仅显示运行计划，不能证明已经执行。",
            "item.checkpoint_index",
        ):
            self.assertIn(token, self.javascript)
        for token in (".command-trace-list", ".command-trace-item", ".trace-meta"):
            self.assertIn(token, self.stylesheet)

    def test_agent_test_queue_uses_only_four_maturity_filters(self) -> None:
        for token in (
            "['all', 'all', '全部用例']",
            "['unexplored', 'unexplored', '待生成步骤']",
            "['explored_unsolidified', 'explored_unsolidified', '步骤待确认']",
            "['solidified', 'solidified', '可直接运行']",
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
            "自动化状态",
            "按用例当前准备情况分类",
            "['all', 'all', '全部用例']",
            "['unexplored', 'unexplored', '待生成步骤']",
            "['explored_unsolidified', 'explored_unsolidified', '步骤待确认']",
            "['solidified', 'solidified', '可直接运行']",
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
            "问题原因：",
            self.javascript,
        )
        self.assertIn(
            "处理方法：",
            self.javascript,
        )
        self.assertIn(
            "可从第 ${completed + 1} 条继续。",
            self.javascript,
        )
        self.assertIn("current.innerHTML = isInterrupted", self.javascript)
        self.assertIn(
            "job.status === 'completed'",
            self.javascript,
        )
        self.assertIn("全部用例已完成。", self.javascript)
        self.assertIn("正在准备下一条用例…", self.javascript)

    def test_single_issue_presentation_table_covers_all_codes_with_cause_and_action(self) -> None:
        for token in (
            "const ISSUE_TABLE",
            "function resolveIssueDefinition(",
            "function issueNoticeHtml(",
            "BLE_UNAVAILABLE",
            "BLE_RUNTIME_UNAVAILABLE",
            "BLE_SCAN_FAILED",
            "BLE_CONNECT_TIMEOUT",
            "BLE_CONNECT_FAILED",
            "LLM_TLS_ERROR",
            "LLM_TIMEOUT",
            "LLM_AUTH_FAILED",
            "LLM_MODEL_NOT_FOUND",
            "LLM_QUOTA_EXCEEDED",
            "LLM_REQUEST_FAILED",
            "SUPERCOM_PIPE_UNAVAILABLE",
            "SUPERCOM_NO_UART",
            "USB_DEVICE_NOT_PRESENT",
            "USB_TARGET_AMBIGUOUS",
            "MTP_NAMESPACE_NOT_READY",
            "LLM_NOT_READY",
            "TARGET_BUSY",
            "PREFLIGHT_INTERNAL_ERROR",
            "HARDWARE_PREPARATION_FAILED",
            "平台未能启动本次任务。",
            "请稍后重试；如仍失败，请重新启动平台。",
            "任务在规定时间内没有完成。",
            "请检查目标设备连接后重试。",
            "任务运行过程中出现平台异常，没有得到完整结果。",
            "请重新运行；如再次出现，展开诊断信息并联系维护人员。",
            "测试程序已经结束，但没有返回可用结果。",
            "请重新运行；如仍失败，保留诊断信息并联系维护人员。",
            "测试已经执行，但结果没有保存成功。",
            "请确认电脑存储空间充足后重试。",
            "测试过程中与手表的连接中断。",
            "请重新执行环境检查，恢复连接后从该用例重试。",
            "手表没有进入可开始测试的状态，因此用例尚未执行。",
            "请重新执行环境检查，确认连接和手表界面正常后重试。",
            "用于判断结果的截图或检查点没有收集完整。",
            "请确认截图连接正常后重新运行该用例。",
            "电脑无法与判定服务建立安全连接。",
            "判定服务在规定时间内没有响应。",
            "判定服务没有接受当前账号信息。",
            "请在系统设置中重新核对判定服务账号或密钥。",
            "当前选择的判定模型不可用。",
            "判定服务暂时无法接受更多请求，或当前账号可用额度不足。",
            "本次操作没有完成，平台暂时无法确定具体原因。",
            "请重新操作；如再次出现，展开诊断信息并联系维护人员。",
        ):
            with self.subTest(token=token):
                self.assertIn(token, self.javascript)

        issue_table_start = self.javascript.index("const ISSUE_TABLE")
        issue_table_end = self.javascript.index("const ISSUE_CODE_ALIASES", issue_table_start)
        issue_table_block = self.javascript[issue_table_start:issue_table_end]
        issue_entries = {
            code: (cause, action)
            for code, cause, action in re.findall(
                r"^\s{2}([A-Z0-9_]+): \{\s+cause: '([^']+)',\s+action: '([^']+)'\s+\}",
                issue_table_block,
                re.MULTILINE,
            )
        }
        for code in (
            "THREAD_START_FAILED",
            "PROCESS_TIMEOUT",
            "PROCESS_EXCEPTION",
            "UNHANDLED_EXCEPTION",
            "RESULT_MISSING",
            "HISTORY_WRITE_FAILED",
            "HARDWARE_INFRASTRUCTURE_FAILURE",
            "HARDWARE_PREPARATION_FAILED",
            "EVIDENCE_INCOMPLETE",
            "PROFILE_INVALID",
            "PORT_NOT_SELECTED",
            "SUPERCOM_PIPE_UNAVAILABLE",
            "SUPERCOM_NO_UART",
            "USB_DEVICE_NOT_PRESENT",
            "USB_TARGET_AMBIGUOUS",
            "MTP_NAMESPACE_NOT_READY",
            "LLM_NOT_READY",
            "TARGET_BUSY",
            "PREFLIGHT_INTERNAL_ERROR",
            "BLE_UNAVAILABLE",
            "BLE_SCAN_FAILED",
            "BLE_CONNECT_TIMEOUT",
            "BLE_CONNECT_FAILED",
            "LLM_TLS_ERROR",
            "LLM_TIMEOUT",
            "LLM_AUTH_FAILED",
            "LLM_MODEL_NOT_FOUND",
            "LLM_QUOTA_EXCEEDED",
            "LLM_REQUEST_FAILED",
        ):
            with self.subTest(code=code):
                self.assertIn(code, issue_entries)
                self.assertTrue(issue_entries[code][0])
                self.assertTrue(issue_entries[code][1])
        issue_copy_texts = [text for values in issue_entries.values() for text in values]
        combined_primary_copy = "\n".join(issue_copy_texts)
        for forbidden in (
            "安装依赖",
            "SSL/TLS",
            "SSL",
            "TLS",
            "证书握手",
            "代理",
            "API Key",
            "鉴权",
            "UART",
            "GUI_PING",
            "gui_ack",
            "MTP",
            "PnP",
            "VID/PID",
            "FileNotFoundError",
            "RuntimeError",
            "TypeError",
            "ValueError",
            "KeyError",
            "固件",
            "命令接口",
            "截图服务",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, combined_primary_copy)

        for token in (
            "summary: known.cause",
            "const defaultCause = safeProductCopy(fallback) || UNKNOWN_ISSUE.cause;",
            "known?.action || safeProductCopy(item.action) || copy.action",
            "error.presentation = presentation;",
        ):
            self.assertIn(token, self.javascript)
        for token in (
            "action || known.action",
            "const defaultCause = rawDetail || fallback",
            "summary: formatIssueSummary",
        ):
            self.assertNotIn(token, self.javascript)

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
            "可直接运行",
            "步骤待确认",
            "待生成步骤",
            "${caseStatusChip(row)}",
            "caseStatusChip({...testCase, latest_verdict: initialVerdict})",
            "const usesFixedMapping = Boolean(testCase.is_fixed_runnable || testCase.is_promoted);",
            "动作已保存，观察证据待补齐",
            "本次将尝试生成自动化步骤",
            "结果只用于本次运行，不会自动保存",
            "验证并保存步骤",
            "candidate_replay_started",
            "Boolean(job.promotion_flow)",
            "job.promotion_status === 'promoted'",
            "步骤验证未通过，未保存",
            "historyCount > 0",
            "? rawVerdict : 'ERROR'",
        ):
            self.assertIn(token, self.javascript)
        self.assertIn("适用平台：${platforms}", self.javascript)
        self.assertIn("<th>自动化成熟度</th><th>最近结果</th>", self.javascript)
        for duplicated_label in (
            "579 · 自动化就绪",
            "579 · 待评审",
            "579 · 需人工",
            "579 · 不支持",
        ):
            self.assertNotIn(duplicated_label, self.javascript)
        status_function = self.javascript.split("function caseStatusChip(row = {})", 1)[1].split(
            "function testExecutionModeLabel", 1
        )[0]
        self.assertNotIn("return maturityChip(row);", status_function)
        for token in (
            "['PASS', 'FAIL', 'CANNOT_VERIFY', 'ERROR'].includes(rawVerdict)",
            "{SKIP: 'CANNOT_VERIFY'}[rawVerdict]",
            "const presentation = resultPresentation(normalized);",
            "${presentation.label}",
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
            "const reportReturnTo = pageUrl('/reports', project, {view: filters.view, from: filters.from, to: filters.to, module: filters.module, platform_id: filters.platform, result: filters.result, maturity: filters.maturity, infrastructure: filters.infrastructure});",
            "renderRecentFailures(report.recent_failures || [], project, reportReturnTo)",
            "const backHref = pageReturnUrl(detailFallback);",
            "const backLabel = returnDestinationLabel(backHref, '测试详情');",
            "'/reports': '测试报告'",
            "查看本次运行",
        ):
            self.assertIn(token, self.javascript)

    def test_report_keeps_product_failures_separate_from_execution_errors(self) -> None:
        for token in (
            "function reportInsightPresentation(",
            "测试概况",
            "失败较多的模块",
            "仅统计产品功能失败",
            "执行异常较多的模块",
            "renderExecutionErrorModules(report.top_execution_error_modules || [])",
            "<th>产品失败</th>",
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
            "最新结果已通过的 ${Number(latestBatchCandidateSummary.pass || 0)} 条已排除",
            "project_id: projectSelect.value",
            "platform_id: platformId",
            "target_id: targetId",
            "watchface_ready: projectSelect.value === '579_Z1640'",
            "batch-resume-button",
            "/resume",
            "继续运行剩余",
            "const cancellable = ['queued', 'running', 'orphaned'].includes(job.status);",
            "cancel.hidden = !cancellable;",
            "批次已完成，部分用例异常",
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

    def test_environment_tabs_have_real_links_and_do_not_depend_on_click_handlers(self) -> None:
        for token in (
            "if (item.href)",
            "environmentTabHref('llm')",
            "environmentTabHref('ones')",
            "environmentTabHref('updates')",
            'aria-current="page"',
        ):
            self.assertIn(token, self.javascript)
        self.assertIn(".workspace-subtabs a", self.stylesheet)

    def test_automation_maturity_is_shown_in_chinese(self) -> None:
        for text in ("自动化就绪", "待评审", "需人工执行", "暂不支持", "未绑定"):
            self.assertIn(text, self.javascript)
        for raw_label in (
            "<span>AUTO_READY</span>",
            "<span>NEED_REVIEW</span>",
            "<span>MANUAL_REQUIRED</span>",
            "<span>UNSUPPORTED</span>",
        ):
            self.assertNotIn(raw_label, self.javascript)

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
