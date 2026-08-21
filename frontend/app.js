const app = document.querySelector('#app');
const toast = document.querySelector('#toast');
let toastTimer = null;
let listRequestToken = 0;
let testListRequestToken = 0;
let importPollTimer = null;
let activeImportJobId = null;
let repairRestoreTimer = null;
let testPollTimer = null;
let batchTestPollTimer = null;
let latestBatchCandidateSummary = {};
const selectedTestCases = new Map();
let selectedTestProject = null;
let currentTestPageItems = [];

const PAGE_SIZE = 20;
const IMPORT_STORAGE_KEY = 'firmware-repair-import-job';
const RESULT_FILTER_LABELS = {
  all: '全部缺陷',
  pass: '已通过',
  fail: '失败',
  cannot_verify: '无法验证',
  pending: '未运行'
};
const TEST_FILTER_LABELS = {
  all: '全部用例',
  unexplored: '尚未外部探索',
  externally_explored: '已经外部探索',
  explored_unsolidified: '已探索但未固化',
  solidified: '已固化、Agent-loop 可执行'
};
const DEFAULT_TEST_PROJECT = '620C_W6830';
const DEFAULT_TEST_STATE = 'all';
const TEST_PROJECTS = {
  '620C_W6830': {
    project: '620C_W6830',
    projectLabel: '620C W6830',
    target: 'simulator',
    targetLabel: '模拟器'
  },
  '6202_W5230': {
    project: '6202_W5230',
    projectLabel: '6202 W5230',
    target: 'hardware',
    targetLabel: '真机'
  },
  '6202_W5230_SIMULATOR': {
    project: '6202_W5230_SIMULATOR',
    projectLabel: '6202 W5230',
    target: 'simulator',
    targetLabel: '模拟器'
  }
};

/** @typedef {'620C_W6830'|'6202_W5230_SIMULATOR'|'6202_W5230'} ProjectKey */
/** @typedef {'PASS'|'FAIL'|'ERROR'|'CANNOT_VERIFY'|'PENDING'|'RUNNING'} Verdict */
/** @typedef {'unexplored'|'externally_explored'|'explored_unsolidified'|'solidified'} MaturityState */

const FUNCTION_CATEGORIES = Object.freeze({
  '系统与导航': ['主表盘与导航', '主菜单', '控制中心', '负一屏', '设置', '全局交互与系统规则', '固件升级', '电源管理', '充电与低电', '开机绑定', '工厂与船运模式'],
  '健康监测': ['心率', '血氧', '压力', '睡眠', '女性健康', '一键测量', '呼吸训练', '久坐提醒', '喝水提醒', '活动记录'],
  '运动': ['运动', '运动记录'],
  '通讯与连接': ['消息', '通话', 'SOS', '语音助手', '查找手机', '查找手表'],
  '工具': ['计算器', '计时器', '秒表', '闹钟', '手电筒', '日历', '世界时钟', '二维码卡包'],
  '生活与多媒体': ['天气', '音乐', '遥控拍照']
});

const FUNCTION_CATEGORY_NAMES = Object.freeze(Object.keys(FUNCTION_CATEGORIES));
const ALL_FUNCTION_MODULES = Object.freeze(FUNCTION_CATEGORY_NAMES.flatMap(name => FUNCTION_CATEGORIES[name]));
const PROJECT_STORAGE_KEY = 'agent-loop-selected-project';
const CASE_CATALOG_PAGE_SIZE = 100;
const TOP_LEVEL_ROUTE_PATHS = Object.freeze(['/overview', '/cases', '/runs', '/reports', '/defects', '/environments']);
const caseCatalogCache = new Map();
let activePageController = null;
let routeRequestToken = 0;

const ENVIRONMENT_PROTOCOLS = Object.freeze({
  '620C_W6830': {
    transport: 'socket',
    transportLabel: 'Socket 命令通道',
    captureProvider: 'simulator',
    captureLabel: '模拟器截图'
  },
  '6202_W5230_SIMULATOR': {
    transport: 'socket',
    transportLabel: 'Socket 命令通道',
    captureProvider: 'simulator',
    captureLabel: '模拟器截图'
  },
  '6202_W5230': {
    transport: 'supercom',
    transportLabel: 'SuperCom 命名管道',
    captureProvider: 'mtp',
    captureLabel: 'USB MTP 截图'
  }
});
const BATCH_CATEGORY_LABELS = {
  untested: '尚未运行',
  fail: '最近运行失败',
  cannot_verify: '最近无法验证',
  error: '最近执行异常'
};
const WORKFLOW_NODES = ['validate', 'interactive_reproduce', 'agent', 'apply', 'build', 'test', 'record'];
const NODE_LABELS = {
  validate: '输入校验',
  interactive_reproduce: '交互复现',
  agent: '修复方案',
  apply: '应用改动',
  build: '固件构建',
  test: '模拟器验证',
  record: '归档结果'
};
const TEST_WORKFLOW_NODES = ['load', 'execute', 'judge', 'record'];
const TEST_NODE_LABELS = {
  load: '加载用例',
  execute: '执行命令',
  judge: '语义判定',
  record: '保存证据'
};

function escapeHtml(value) {
  return String(value ?? '')
    .replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;').replaceAll("'", '&#039;');
}

function friendlyAgentError(value) {
  const text = String(value ?? '').trim();
  if (!text) return text;
  if (text.includes('执行 Agent API 出错') || text.includes('识图 Agent API 出错')) return text;
  if (/交互式复现异常\s*[:：].*(APIConnectionError|LLMRetryError)/.test(text)) {
    return `执行 Agent API 出错：${text.replace(/^交互式复现异常\s*[:：]\s*/, '')}`;
  }
  if (/LLM 重试耗尽.*(APIConnectionError|LLMRetryError)/.test(text)) {
    return `识图 Agent API 出错：${text}`;
  }
  return text;
}

function showToast(message, type = '') {
  clearTimeout(toastTimer);
  toast.textContent = message;
  toast.className = `toast show ${type}`;
  toastTimer = setTimeout(() => { toast.className = 'toast'; }, 3600);
}

function startBrowserDownload(url, filename) {
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
}

async function api(url, options = {}) {
  const headers = { ...(options.headers || {}) };
  if (options.body) headers['Content-Type'] = 'application/json';
  const response = await fetch(url, { ...options, headers });
  let payload;
  try { payload = await response.json(); } catch { payload = {}; }
  if (!response.ok) {
    const error = new Error(payload.error || `请求失败（${response.status}）`);
    error.status = response.status;
    throw error;
  }
  return payload;
}

function icon(name, size = 20) {
  const paths = {
    overview: '<path d="M4 19V9l4-4 4 4 4-4 4 4v10"/><path d="M3 19h18M8 19v-5h3v5m5 0v-7h3v7"/>',
    cases: '<rect x="5" y="3" width="14" height="18" rx="2"/><path d="M9 3v3h6V3M9 11h6M9 15h6"/>',
    runs: '<circle cx="12" cy="12" r="9"/><path d="m10 8 6 4-6 4Z"/>',
    reports: '<rect x="4" y="3" width="16" height="18" rx="2"/><path d="M8 15v2m4-6v6m4-9v9M8 7h4"/>',
    defects: '<path d="M8 4h8M9 2v4m6-4v4M5 10h14M4 14h16M6 18h12"/><rect x="6" y="6" width="12" height="15" rx="6"/>',
    environments: '<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.9l.1.1-2.8 2.8-.1-.1a1.7 1.7 0 0 0-1.9-.3 1.7 1.7 0 0 0-1 1.6v.2h-4V21a1.7 1.7 0 0 0-1-1.6 1.7 1.7 0 0 0-1.9.3l-.1.1L4.2 17l.1-.1a1.7 1.7 0 0 0 .3-1.9A1.7 1.7 0 0 0 3 14H2.8v-4H3a1.7 1.7 0 0 0 1.6-1 1.7 1.7 0 0 0-.3-1.9L4.2 7 7 4.2l.1.1A1.7 1.7 0 0 0 9 4.6a1.7 1.7 0 0 0 1-1.6v-.2h4V3a1.7 1.7 0 0 0 1 1.6 1.7 1.7 0 0 0 1.9-.3l.1-.1L19.8 7l-.1.1a1.7 1.7 0 0 0-.3 1.9 1.7 1.7 0 0 0 1.6 1h.2v4H21a1.7 1.7 0 0 0-1.6 1Z"/>',
    check: '<path d="m5 12 4 4L19 6"/>',
    warning: '<path d="M12 3 2.5 20h19Z"/><path d="M12 9v4m0 3h.01"/>',
    refresh: '<path d="M20 6v5h-5M4 18v-5h5"/><path d="M18 9a7 7 0 0 0-12-3L4 8m2 7a7 7 0 0 0 12 3l2-2"/>',
    search: '<circle cx="11" cy="11" r="7"/><path d="m20 20-4-4"/>',
    plus: '<path d="M12 5v14M5 12h14"/>',
    pause: '<path d="M9 5v14m6-14v14"/>',
    evidence: '<rect x="3" y="5" width="18" height="14" rx="2"/><path d="m7 15 3-3 3 3 2-2 3 3M8 9h.01"/>'
  };
  const body = paths[name] || paths.overview;
  return `<svg class="ui-icon" width="${Number(size)}" height="${Number(size)}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${body}</svg>`;
}

function currentProject() {
  const queryProject = new URLSearchParams(location.search).get('project');
  if (queryProject && Object.hasOwn(TEST_PROJECTS, queryProject)) return queryProject;
  const stored = localStorage.getItem(PROJECT_STORAGE_KEY);
  return stored && Object.hasOwn(TEST_PROJECTS, stored) ? stored : DEFAULT_TEST_PROJECT;
}

function rememberProject(project) {
  const normalized = Object.hasOwn(TEST_PROJECTS, project) ? project : DEFAULT_TEST_PROJECT;
  localStorage.setItem(PROJECT_STORAGE_KEY, normalized);
  const select = document.querySelector('#global-target-select');
  if (select && select.value !== normalized) select.value = normalized;
  return normalized;
}

function setGlobalTargetHealth(status = 'unchecked', label = '尚未检查') {
  const element = document.querySelector('#global-target-status');
  if (!element) return;
  element.className = `target-health-inline is-${status}`;
  const text = element.querySelector('span');
  if (text) text.textContent = label;
}

function pageUrl(path, project = currentProject(), extra = {}) {
  const params = new URLSearchParams();
  if (project && Object.hasOwn(TEST_PROJECTS, project)) params.set('project', project);
  Object.entries(extra).forEach(([key, value]) => {
    if (value !== undefined && value !== null && String(value) !== '') params.set(key, String(value));
  });
  return `${path}${params.size ? `?${params.toString()}` : ''}`;
}

function initGlobalTargetSwitcher() {
  const select = document.querySelector('#global-target-select');
  if (!select || select.dataset.ready === 'true') return;
  select.dataset.ready = 'true';
  select.value = rememberProject(currentProject());
  select.addEventListener('change', () => {
    const project = rememberProject(select.value);
    selectedTestCases.clear();
    selectedTestProject = project;
    const targetPath = TOP_LEVEL_ROUTE_PATHS.includes(location.pathname) ? location.pathname : '/cases';
    history.pushState({}, '', pageUrl(targetPath, project));
    route();
  });
}

function initPrimaryNavigation() {
  if (document.documentElement.dataset.navigationReady === 'true') return;
  document.documentElement.dataset.navigationReady = 'true';
  document.addEventListener('click', event => {
    const link = event.target.closest?.('a[href]');
    if (!link || link.target === '_blank' || link.hasAttribute('download') || event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    const url = new URL(link.getAttribute('href'), location.href);
    if (url.origin !== location.origin) return;
    const targetPath = link.dataset.route || url.pathname;
    if (!TOP_LEVEL_ROUTE_PATHS.includes(targetPath)) return;
    event.preventDefault();
    const destination = link.dataset.route
      ? pageUrl(targetPath, currentProject())
      : `${url.pathname}${url.search}${url.hash}`;
    history.pushState({}, '', destination);
    route();
  });
}

function restoreCompatibleTopLevelRoute(parts) {
  if (!((parts.length === 0) || (parts.length === 1 && parts[0] === 'tests'))) return parts;
  const params = new URLSearchParams(location.search);
  const requested = params.get('view');
  if (!requested || !TOP_LEVEL_ROUTE_PATHS.includes(`/${requested}`)) return parts;
  const project = currentProject();
  const extra = {};
  params.forEach((value, key) => {
    if (key !== 'project' && key !== 'view') extra[key] = value;
  });
  history.replaceState({}, '', pageUrl(`/${requested}`, project, extra));
  return [requested];
}

async function optionalApi(url, options = {}) {
  try {
    return {available: true, data: await api(url, options), error: null};
  } catch (error) {
    return {available: error.status !== 404, data: null, error};
  }
}

async function mapWithLimit(values, limit, mapper) {
  const result = new Array(values.length);
  let cursor = 0;
  const workers = Array.from({length: Math.min(limit, values.length)}, async () => {
    while (cursor < values.length) {
      const index = cursor++;
      result[index] = await mapper(values[index], index);
    }
  });
  await Promise.all(workers);
  return result;
}

function invalidateCaseCatalog(project) {
  if (project) caseCatalogCache.delete(project);
  else caseCatalogCache.clear();
}

async function loadCaseCatalog(project = currentProject(), {force = false} = {}) {
  const normalized = testProject(project).project;
  if (force) invalidateCaseCatalog(normalized);
  if (caseCatalogCache.has(normalized)) return await caseCatalogCache.get(normalized);
  const pending = (async () => {
    const first = await api(`/api/tests?project=${encodeURIComponent(normalized)}&page=1&page_size=${CASE_CATALOG_PAGE_SIZE}`);
    const pages = Array.from({length: Math.max(0, Number(first.total_pages || 1) - 1)}, (_, index) => index + 2);
    const payloads = await mapWithLimit(pages, 6, page => api(`/api/tests?project=${encodeURIComponent(normalized)}&page=${page}&page_size=${CASE_CATALOG_PAGE_SIZE}`));
    return {
      ...first,
      items: [...(first.items || []), ...payloads.flatMap(payload => payload.items || [])]
    };
  })();
  caseCatalogCache.set(normalized, pending);
  try {
    return await pending;
  } catch (error) {
    caseCatalogCache.delete(normalized);
    throw error;
  }
}

function moduleCategory(moduleName) {
  return FUNCTION_CATEGORY_NAMES.find(name => FUNCTION_CATEGORIES[name].includes(moduleName)) || '其他';
}

function verdictCounts(items = []) {
  const counts = {PASS: 0, FAIL: 0, ERROR: 0, CANNOT_VERIFY: 0, PENDING: 0, RUNNING: 0};
  for (const item of items) {
    const raw = String(item.latest_verdict || item.verdict || 'PENDING').toUpperCase();
    const key = raw === 'SKIP' ? 'CANNOT_VERIFY' : (Object.hasOwn(counts, raw) ? raw : 'PENDING');
    counts[key] += 1;
  }
  return counts;
}

function destroyActivePageController() {
  if (!activePageController) return;
  try { activePageController.destroy?.(); } catch { /* 页面销毁不得阻塞路由 */ }
  activePageController = null;
}

async function mountPageController(controller) {
  destroyActivePageController();
  activePageController = controller;
  const token = ++routeRequestToken;
  app.innerHTML = Components.skeletonState('正在读取工作台数据…');
  try {
    const data = await controller.load();
    if (token !== routeRequestToken || activePageController !== controller) return;
    app.innerHTML = controller.render(data);
    controller.mount?.(app, data);
  } catch (error) {
    if (token !== routeRequestToken || activePageController !== controller) return;
    app.innerHTML = Components.errorState('页面读取失败', error.message, 'data-page-retry');
    app.querySelector('[data-page-retry]')?.addEventListener('click', () => route());
  }
}

const Components = Object.freeze({
  metricCard({label, value = '—', hint = '', tone = 'blue', iconName = 'reports'}) {
    return `<article class="workspace-metric is-${escapeHtml(tone)}"><span class="metric-icon">${icon(iconName, 22)}</span><div><span>${escapeHtml(label)}</span><strong>${escapeHtml(value)}</strong>${hint ? `<small>${escapeHtml(hint)}</small>` : ''}</div></article>`;
  },
  statusChip(status, label = '') {
    const normalized = String(status || 'PENDING').toUpperCase();
    const presentation = resultPresentation(normalized);
    return `<span class="${presentation.className}" data-status="${escapeHtml(normalized)}">${escapeHtml(label || presentation.label)}</span>`;
  },
  progressRing(percent = 0, label = '已完成') {
    const safe = Math.max(0, Math.min(100, Number(percent) || 0));
    return `<div class="progress-ring" style="--progress:${safe}" role="img" aria-label="${escapeHtml(label)} ${safe}%"><div><strong>${safe}%</strong><span>${escapeHtml(label)}</span></div></div>`;
  },
  subTabs(items, active) {
    return `<nav class="workspace-subtabs" aria-label="页面分类">${items.map(item => `<button type="button" class="${item.value === active ? 'is-active' : ''}" data-subtab="${escapeHtml(item.value)}" ${item.disabled ? 'disabled' : ''}>${escapeHtml(item.label)}</button>`).join('')}</nav>`;
  },
  emptyState(title, message = '') {
    return `<div class="workspace-empty"><strong>${escapeHtml(title)}</strong>${message ? `<p>${escapeHtml(message)}</p>` : ''}</div>`;
  },
  unavailableState(title, message) {
    return `<div class="workspace-unavailable">${icon('warning', 20)}<div><strong>${escapeHtml(title)}</strong><p>${escapeHtml(message)}</p></div></div>`;
  },
  skeletonState(message = '正在读取…') {
    return `<section class="loading-state workspace-loading"><div class="spinner"></div><p>${escapeHtml(message)}</p></section>`;
  },
  errorState(title, message, retryAttribute = '') {
    return `<section class="error-state"><h1>${escapeHtml(title)}</h1><p>${escapeHtml(message || '未知错误')}</p>${retryAttribute ? `<button class="button" type="button" ${retryAttribute}>重试</button>` : ''}</section>`;
  },
  pageHeader({title, intro, actions = '', updatedAt = ''}) {
    return `<header class="workspace-page-header"><div><h1>${escapeHtml(title)}</h1><p>${escapeHtml(intro)}</p></div><div class="workspace-page-actions">${updatedAt ? `<small>最后更新：${escapeHtml(updatedAt)}</small>` : ''}${actions}</div></header>`;
  }
});

function formatTime(value) {
  if (!value) return '—';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return escapeHtml(value);
  return new Intl.DateTimeFormat('zh-CN', {
    year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit'
  }).format(date);
}

function resultPresentation(value) {
  const normalized = String(value || 'PENDING').toUpperCase();
  if (normalized === 'PASS') return {className: 'chip chip-pass', label: '已通过'};
  if (normalized === 'FAIL') return {className: 'chip chip-fail', label: '失败'};
  if (normalized === 'CANNOT_VERIFY') return {className: 'chip chip-warning', label: '无法验证'};
  if (normalized === 'SKIP') return {className: 'chip chip-warning', label: '无法验证'};
  if (normalized === 'ERROR') return {className: 'chip chip-fail', label: '执行错误'};
  if (normalized === 'RUNNING' || normalized === 'QUEUED') return {className: 'chip chip-running', label: '运行中'};
  return {className: 'chip chip-pending', label: '未运行'};
}

function setActiveNav(section) {
  document.querySelectorAll('[data-nav]').forEach(link => {
    const active = link.dataset.nav === section;
    link.classList.toggle('is-active', active);
    if (active) link.setAttribute('aria-current', 'page');
    else link.removeAttribute('aria-current');
  });
}

function resultChip(value) {
  const presentation = resultPresentation(value);
  return `<span class="${presentation.className}">${presentation.label}</span>`;
}

function maturityChip(row = {}) {
  if (row.is_promoted || row.maturity_state === 'solidified') {
    return '<span class="chip chip-solidified">已固化</span>';
  }
  if (row.external_explored || row.maturity_state === 'explored_unsolidified') {
    return '<span class="chip chip-unsolidified">未固化</span>';
  }
  return '<span class="chip chip-unexplored">尚未探索</span>';
}

function caseStatusChip(row = {}) {
  if (row.is_promoted || row.maturity_state === 'solidified') {
    const historyCount = Number(row.history_count || 0);
    const rawVerdict = String(row.latest_verdict || 'PENDING').toUpperCase();
    const normalized = historyCount > 0
      ? ({SKIP: 'CANNOT_VERIFY'}[rawVerdict] || (
        ['PASS', 'FAIL', 'CANNOT_VERIFY', 'ERROR'].includes(rawVerdict) ? rawVerdict : 'ERROR'
      ))
      : 'PENDING';
    const presentation = resultPresentation(normalized);
    const label = {
      PASS: 'PASS',
      FAIL: 'FAIL',
      CANNOT_VERIFY: 'CANNOT_VERIFY',
      SKIP: 'CANNOT_VERIFY',
      ERROR: 'ERROR'
    }[normalized] || presentation.label;
    return `<span class="${presentation.className}">${label}</span>`;
  }
  return maturityChip(row);
}

function testExecutionModeLabel(value) {
  const labels = {
    fixed_mapping: '固化步骤',
    candidate_mapping: '外部候选复跑',
    agent_exploration: 'Agent-loop 临时探索'
  };
  return labels[String(value || '')] || '旧记录未标明';
}

function testProject(value = DEFAULT_TEST_PROJECT) {
  return TEST_PROJECTS[value] || TEST_PROJECTS[DEFAULT_TEST_PROJECT];
}

function testTargetChip(value) {
  const meta = typeof value === 'string' ? testProject(value) : value;
  const target = meta?.execution_target || meta?.target || 'simulator';
  const projectLabel = meta?.project_label || meta?.projectLabel || testProject().projectLabel;
  const targetLabel = meta?.execution_target_label || meta?.targetLabel || (target === 'hardware' ? '真机' : '模拟器');
  return `<span class="chip chip-target target-${escapeHtml(target)}">${escapeHtml(projectLabel)} · ${escapeHtml(targetLabel)}</span>`;
}

function screenshotLabel(value) {
  const meta = typeof value === 'string' ? testProject(value) : value;
  return (meta?.execution_target || meta?.target) === 'hardware' ? '真机截图' : '模拟器截图';
}

function activeTestJobs(payload = {}) {
  const jobs = Array.isArray(payload?.jobs)
    ? payload.jobs
    : (payload?.job ? [payload.job] : []);
  const seen = new Set();
  return jobs.filter(job => {
    const id = String(job?.id || '');
    if (!id || seen.has(id)) return false;
    seen.add(id);
    return true;
  });
}

function activeTestJobForProject(payload, project) {
  const target = testProject(project).target;
  return activeTestJobs(payload).find(job => testProject(job.project).target === target) || null;
}

function setResultChip(element, value) {
  const presentation = resultPresentation(value);
  element.className = presentation.className;
  element.textContent = presentation.label;
}

function renderError(error) {
  app.innerHTML = `
    <section class="error-state">
      <p class="eyebrow">读取失败</p>
      <h1>页面暂时打不开</h1>
      <p>${escapeHtml(error.message || error)}</p>
      <button class="button" type="button" onclick="location.reload()">重新加载</button>
      <a class="text-link" href="/">返回缺陷队列</a>
    </section>`;
}

function workflowSvg() {
  const x = [18, 172, 326, 480, 634, 788, 942];
  const lines = x.slice(0, -1).map((value, index) =>
    `<path class="flow-line" d="M ${value + 112} 74 L ${x[index + 1] - 8} 74" marker-end="url(#arrow)"/>`
  ).join('');
  const nodes = WORKFLOW_NODES.map((name, index) => `
    <g class="flow-node" data-node="${name}">
      <rect x="${x[index]}" y="43" width="112" height="62" rx="10"></rect>
      <text x="${x[index] + 56}" y="79">${NODE_LABELS[name]}</text>
    </g>`).join('');
  return `
    <div class="workflow-wrap">
      <svg class="workflow" viewBox="0 0 1080 185" role="img" aria-label="七步修复流程">
        <defs><marker id="arrow" markerWidth="8" markerHeight="8" refX="7" refY="3" orient="auto"><path d="M0,0 L0,6 L7,3 z" fill="#aaa398"></path></marker></defs>
        ${lines}
        <path class="flow-line" d="M 998 112 L 998 152 L 382 152 L 382 113" marker-end="url(#arrow)"/>
        <text x="690" y="175" class="retry-label" text-anchor="middle">修复失败后重试</text>
        ${nodes}
      </svg>
    </div>`;
}

function updateWorkflow(nodes = {}) {
  WORKFLOW_NODES.forEach(name => {
    const element = document.querySelector(`[data-node="${name}"]`);
    if (!element) return;
    element.classList.remove('node-running', 'node-pass', 'node-fail');
    const state = String(nodes[name] || 'pending').toLowerCase();
    if (['running', 'pass', 'fail'].includes(state)) element.classList.add(`node-${state}`);
  });
}

function testWorkflowSvg() {
  const x = [24, 274, 524, 774];
  const lines = x.slice(0, -1).map((value, index) =>
    `<path class="flow-line" d="M ${value + 154} 74 L ${x[index + 1] - 12} 74" marker-end="url(#test-arrow)"/>`
  ).join('');
  const nodes = TEST_WORKFLOW_NODES.map((name, index) => `
    <g class="flow-node" data-test-node="${name}">
      <rect x="${x[index]}" y="43" width="154" height="62" rx="10"></rect>
      <text x="${x[index] + 77}" y="79">${TEST_NODE_LABELS[name]}</text>
    </g>`).join('');
  return `
    <div class="workflow-wrap">
      <svg class="workflow test-workflow" viewBox="0 0 952 128" role="img" aria-label="Agent 测试流程">
        <defs><marker id="test-arrow" markerWidth="8" markerHeight="8" refX="7" refY="3" orient="auto"><path d="M0,0 L0,6 L7,3 z" fill="#aaa398"></path></marker></defs>
        ${lines}${nodes}
      </svg>
    </div>`;
}

function updateTestWorkflow(nodes = {}) {
  TEST_WORKFLOW_NODES.forEach(name => {
    const element = document.querySelector(`[data-test-node="${name}"]`);
    if (!element) return;
    element.classList.remove('node-running', 'node-pass', 'node-fail');
    const state = String(nodes[name] || 'pending').toLowerCase();
    if (['running', 'pass', 'fail'].includes(state)) element.classList.add(`node-${state}`);
  });
}

function listParams() {
  const params = new URLSearchParams(location.search);
  const rawPage = Number.parseInt(params.get('page') || '1', 10);
  const result = String(params.get('result') || 'all').toLowerCase();
  return {
    query: (params.get('q') || '').trim(),
    page: Number.isFinite(rawPage) && rawPage > 0 ? rawPage : 1,
    result: Object.hasOwn(RESULT_FILTER_LABELS, result) ? result : 'all'
  };
}

function setListUrl(query, page, resultFilter = 'all', mode = 'replace') {
  const params = new URLSearchParams();
  params.set('project', currentProject());
  if (query.trim()) params.set('q', query.trim());
  if (resultFilter !== 'all') params.set('result', resultFilter);
  if (page > 1) params.set('page', String(page));
  const url = `/defects?${params.toString()}`;
  history[mode === 'push' ? 'pushState' : 'replaceState']({}, '', url);
}

function metricCards(summary = {}, activeResult = 'all') {
  const cards = [
    ['all', 'all', '全部缺陷'],
    ['pass', 'passed', '已通过'],
    ['fail', 'failed', '失败'],
    ['cannot_verify', 'cannot_verify', '无法验证'],
    ['pending', 'pending', '未运行']
  ];
  return `
    ${cards.map(([result, countKey, label]) => `
      <button class="metric${activeResult === result ? ' is-active' : ''}" type="button" data-result-filter="${result}" aria-pressed="${activeResult === result}">
        <strong>${Number(summary[countKey] || 0)}</strong><span>${label}</span>
      </button>`).join('')}`;
}

function defectRows(items, query, resultFilter = 'all') {
  if (!items.length) {
    const isFiltered = resultFilter !== 'all';
    return `<div class="empty-state compact">
      <strong>${query ? '没有找到匹配的缺陷' : isFiltered ? `没有${RESULT_FILTER_LABELS[resultFilter]}的缺陷` : '缺陷库中暂无数据'}</strong>
      <p>${query ? '可以减少关键词，或清除当前搜索条件。' : isFiltered ? '可以选择其他分类查看缺陷。' : '缺陷导入后会显示在这里。'}</p>
      ${query ? '<button class="button button-secondary" type="button" data-clear-search>清除搜索条件</button>' : ''}
    </div>`;
  }
  const rows = items.map(row => `<tr><td><a class="case-id-link" href="/defect/${encodeURIComponent(row.number)}">#${escapeHtml(row.number)}</a></td><td><strong>${escapeHtml(row.title)}</strong><small>${escapeHtml(row.description || '')}</small></td><td><span class="chip chip-status">${escapeHtml(row.priority || row.status || '未知')}</span></td><td>${resultChip(row.repair_result)}</td><td>${row.last_run_at ? formatTime(row.last_run_at) : '—'}</td><td class="table-actions"><a class="table-icon-action" href="/defect/${encodeURIComponent(row.number)}" aria-label="查看缺陷 #${escapeHtml(row.number)}">${icon('runs', 17)}</a></td></tr>`).join('');
  return `<div class="workspace-table-scroll"><table class="workspace-table defect-table"><thead><tr><th>缺陷编号</th><th>标题</th><th>优先级/状态</th><th>最近结果</th><th>更新时间</th><th>操作</th></tr></thead><tbody>${rows}</tbody></table></div>`;
}

function pagination(payload) {
  const current = Number(payload.page || 1);
  const pages = Number(payload.total_pages || 0);
  const total = Number(payload.total || 0);
  if (!pages) return `<div class="pagination"><span>共 ${total} 条</span></div>`;
  const start = Math.max(1, Math.min(current - 2, pages - 4));
  const end = Math.min(pages, Math.max(current + 2, 5));
  const pageButtons = [];
  for (let page = start; page <= end; page += 1) {
    pageButtons.push(`<button type="button" class="page-button${page === current ? ' current' : ''}" data-page="${page}" ${page === current ? 'aria-current="page"' : ''}>${page}</button>`);
  }
  return `<div class="pagination">
    <span class="pagination-total">共 ${total} 条 · 第 ${current} / ${pages} 页</span>
    <div class="pagination-actions">
      <button type="button" class="page-button page-text" data-page="${current - 1}" ${current <= 1 ? 'disabled' : ''}>上一页</button>
      ${pageButtons.join('')}
      <button type="button" class="page-button page-text" data-page="${current + 1}" ${current >= pages ? 'disabled' : ''}>下一页</button>
    </div>
  </div>`;
}

async function refreshDefects(query, page, { urlMode = 'replace', resultFilter = listParams().result } = {}) {
  const token = ++listRequestToken;
  const list = document.querySelector('#defect-list');
  const pager = document.querySelector('#pagination');
  const count = document.querySelector('#queue-count');
  if (!list || !pager || !count) return;
  list.classList.add('is-loading');
  try {
    const payload = await api(`/api/defects?q=${encodeURIComponent(query)}&result=${encodeURIComponent(resultFilter)}&page=${page}&page_size=${PAGE_SIZE}`);
    if (token !== listRequestToken) return;
    const selectedResult = Object.hasOwn(RESULT_FILTER_LABELS, payload.result_filter) ? payload.result_filter : 'all';
    document.querySelector('#metrics').innerHTML = metricCards(payload.summary, selectedResult);
    document.querySelector('#queue-title').textContent = RESULT_FILTER_LABELS[selectedResult];
    list.innerHTML = defectRows(payload.items || [], query, selectedResult);
    pager.innerHTML = pagination(payload);
    count.textContent = query ? `找到 ${payload.total} 条` : `共 ${payload.total} 条`;
    setListUrl(query, payload.page || 1, selectedResult, urlMode);
  } catch (error) {
    if (token !== listRequestToken) return;
    list.innerHTML = `<div class="empty-state compact"><strong>缺陷队列读取失败</strong><p>${escapeHtml(error.message)}</p><button class="button button-secondary" type="button" data-retry-list>重试</button></div>`;
    pager.innerHTML = '';
    count.textContent = '读取失败';
  } finally {
    if (token === listRequestToken) list.classList.remove('is-loading');
  }
}

function importStatus(job) {
  if (job.status === 'done') return {label: '拉取完成', chip: 'chip-pass'};
  if (job.status === 'failed') return {label: '拉取失败', chip: 'chip-fail'};
  return {label: '正在拉取', chip: 'chip-running'};
}

function importLogLines(value, count = 5) {
  return String(value || '').split(/\r?\n/).filter(Boolean).slice(-count).join('\n');
}

function updateImportButton(job = null) {
  const button = document.querySelector('#open-import');
  if (!button) return;
  const running = job?.status === 'running';
  button.disabled = running;
  button.textContent = running ? '拉取中…' : '拉取缺陷';
}

function renderImportProgress(job, message = '') {
  const panel = document.querySelector('#import-progress');
  if (!panel) return;
  const status = importStatus(job);
  const fullLog = String(job.stdout_tail || '');
  const latestLog = importLogLines(fullLog) || (job.status === 'running' ? '任务已启动，等待拉取日志…' : '本次任务没有输出日志。');
  panel.hidden = false;
  panel.innerHTML = `
    <div class="import-progress-head">
      <div>
        <span class="chip ${status.chip}">${status.label}</span>
        <strong>缺陷拉取任务</strong>
        <span class="muted small">${escapeHtml(job.id || '')}</span>
      </div>
      <span class="muted small">${job.finished_at ? `结束于 ${formatTime(job.finished_at)}` : '每 3 秒刷新进度'}</span>
    </div>
    ${message ? `<div class="import-message">${escapeHtml(message)}</div>` : ''}
    ${job.error ? `<div class="notice notice-error">${escapeHtml(job.error)}</div>` : ''}
    <pre class="import-log-preview"><code>${escapeHtml(latestLog)}</code></pre>
    ${fullLog ? `<details class="import-log-details"><summary>展开完整日志</summary><pre><code>${escapeHtml(fullLog)}</code></pre></details>` : ''}`;
  updateImportButton(job);
}

function stopImportPolling() {
  clearTimeout(importPollTimer);
  importPollTimer = null;
}

async function pollImport(jobId) {
  stopImportPolling();
  if (!jobId || activeImportJobId !== jobId || !document.querySelector('#import-progress')) return;
  try {
    const job = await api(`/api/defects/import/${encodeURIComponent(jobId)}`);
    if (activeImportJobId !== jobId) return;
    renderImportProgress(job);
    if (job.status === 'running') {
      importPollTimer = setTimeout(() => pollImport(jobId), 3000);
      return;
    }
    localStorage.removeItem(IMPORT_STORAGE_KEY);
    activeImportJobId = null;
    updateImportButton(job);
    if (job.status === 'done') {
      showToast('缺陷拉取完成');
      const input = document.querySelector('#defect-search');
      await refreshDefects(input?.value.trim() || '', 1);
    } else {
      showToast(job.error || '缺陷拉取失败', 'error');
    }
  } catch (error) {
    if (activeImportJobId !== jobId) return;
    if (error.status === 404) {
      localStorage.removeItem(IMPORT_STORAGE_KEY);
      activeImportJobId = null;
      renderImportProgress({id: jobId, status: 'failed', error: '拉取任务不存在或服务已经重启。', stdout_tail: ''});
      updateImportButton();
      return;
    }
    renderImportProgress({id: jobId, status: 'running', stdout_tail: ''}, `进度读取失败：${error.message}，稍后自动重试。`);
    importPollTimer = setTimeout(() => pollImport(jobId), 3000);
  }
}

function restoreImportProgress() {
  const jobId = localStorage.getItem(IMPORT_STORAGE_KEY);
  if (!jobId) return;
  activeImportJobId = jobId;
  updateImportButton({status: 'running'});
  renderImportProgress({id: jobId, status: 'running', stdout_tail: ''});
  pollImport(jobId);
}

function renderRepairWorkflow(job = {}) {
  const nodes = job.nodes || {};
  const current = String(job.current_node || '');
  const currentIndex = Math.max(0, WORKFLOW_NODES.indexOf(current));
  return `<ol class="repair-stepper">${WORKFLOW_NODES.map((node, index) => {
    const raw = typeof nodes[node] === 'object' ? nodes[node].status : nodes[node];
    const status = String(raw || (index < currentIndex ? 'pass' : index === currentIndex ? 'running' : 'pending')).toLowerCase();
    const className = ['pass', 'done', 'completed', 'success'].includes(status) ? 'is-complete' : status === 'running' ? 'is-active' : ['fail', 'failed', 'error'].includes(status) ? 'is-failed' : '';
    return `<li class="${className}"><i>${className === 'is-complete' ? icon('check', 13) : index + 1}</i><span>${escapeHtml(NODE_LABELS[node])}</span></li>`;
  }).join('')}</ol>`;
}

async function refreshActiveRepairPanel() {
  const panel = document.querySelector('#current-repair-summary');
  if (!panel) return;
  const result = await optionalApi('/api/run/active');
  if (!panel.isConnected) return;
  const job = result.data?.job || null;
  if (!job) {
    panel.innerHTML = `<header><div><h2>当前修复任务</h2><p>自动修复流程</p></div><span class="chip chip-pending">空闲</span></header>${Components.emptyState('当前没有修复任务', '从缺陷队列进入详情后可启动自动修复。')}`;
    return;
  }
  const defectNumber = job.defect_number || job.number || '';
  panel.innerHTML = `<header><div><h2>当前修复任务</h2><p>${defectNumber ? `缺陷 #${escapeHtml(defectNumber)}` : escapeHtml(job.id || '')}</p></div>${Components.statusChip('RUNNING', '修复中')}</header>${renderRepairWorkflow(job)}<div class="repair-task-summary"><dl><div><dt>任务 ID</dt><dd>${escapeHtml(job.id || '—')}</dd></div><div><dt>当前阶段</dt><dd>${escapeHtml(NODE_LABELS[job.current_node] || job.current_node || '准备中')}</dd></div><div><dt>开始时间</dt><dd>${formatTime(job.started_at)}</dd></div></dl>${defectNumber ? `<a class="button button-secondary" href="/defect/${encodeURIComponent(defectNumber)}">查看任务</a>` : ''}</div>`;
}

async function renderList() {
  setActiveNav('defects');
  document.title = '缺陷闭环 · Agent-loop';
  const initial = listParams();
  app.innerHTML = `
    ${Components.pageHeader({title: '缺陷闭环', intro: '从缺陷同步、自动修复到构建验证的完整闭环', actions: `<button id="open-import" class="button" type="button">${icon('refresh', 17)} 拉取缺陷</button><a class="button button-secondary" href="${escapeHtml(pageUrl('/defects', currentProject()))}" title="请先选择缺陷后创建修复任务">${icon('plus', 17)} 新建修复任务</a>`})}
    ${Components.subTabs([{value: 'queue', label: '缺陷队列'}, {value: 'tasks', label: '修复任务', disabled: true}, {value: 'history', label: '修复历史', disabled: true}], 'queue')}
    <section class="defect-filter-bar" aria-label="缺陷队列工具"><div id="search-stage" class="search-box">${icon('search', 18)}<input id="defect-search" type="search" value="${escapeHtml(initial.query)}" placeholder="搜索编号、标题或描述" autocomplete="off"><button id="clear-search" class="clear-search" type="button" aria-label="清空搜索">清空</button></div><select aria-label="缺陷来源" disabled><option>ONES</option></select></section>
    <section id="import-progress" class="import-progress" aria-live="polite" hidden></section>
    <section id="metrics" class="metrics defect-kpis" aria-label="缺陷统计与筛选">${metricCards({}, initial.result)}</section>
    <section class="defect-loop-grid"><article class="panel queue-panel"><header class="panel-head"><div><h2 id="queue-title">${RESULT_FILTER_LABELS[initial.result]}</h2><p>按缺陷编号倒序排列</p></div><span id="queue-count" class="chip chip-pending">正在读取</span></header><div id="defect-list" class="defect-list"><div class="list-loading">正在读取缺陷…</div></div><div id="pagination"></div></article><aside id="current-repair-summary" class="workspace-panel current-repair-summary"><header><div><h2>当前修复任务</h2><p>正在读取活动任务</p></div></header><div class="list-loading">正在读取…</div></aside></section>
    <section class="workspace-panel repair-daily-bar"><header><div><h2>今日修复概览</h2><p>等待后端提供全局修复历史聚合接口</p></div></header><div class="digest-items"><div><span>今日修复</span><strong>—</strong></div><div><span>通过</span><strong>—</strong></div><div><span>失败</span><strong>—</strong></div><div><span>无法验证</span><strong>—</strong></div></div></section>
    <dialog id="import-dialog" class="import-dialog" aria-labelledby="import-dialog-title">
      <form id="import-form">
        <header class="dialog-head">
          <div><p class="eyebrow">更新本地缺陷库</p><h2 id="import-dialog-title">拉取缺陷</h2></div>
          <button id="close-import" class="dialog-close" type="button" aria-label="关闭">×</button>
        </header>
        <div class="dialog-body">
          <fieldset class="import-fieldset">
            <legend>类型</legend>
            <label class="choice-row"><input type="radio" name="import-type" value="open" checked><span><strong>仅开放缺陷</strong><small>忽略已经关闭的缺陷</small></span></label>
            <label class="choice-row"><input type="radio" name="import-type" value="all"><span><strong>含已关闭缺陷</strong><small>同时拉取已经关闭的缺陷</small></span></label>
          </fieldset>
          <label class="import-number-field" for="import-limit"><span>数量</span><input id="import-limit" name="limit" type="number" min="0" step="1" value="0" placeholder="0=不限制"><small>填写 0 表示不限制，也可以填写 50、100 等数量。</small></label>
          <label class="choice-row choice-checkbox"><input id="import-force" type="checkbox" checked><span><strong>强制重新入库</strong><small>已有 defect.json 也重新分析</small></span></label>
        </div>
        <footer class="dialog-actions">
          <button id="cancel-import" class="button button-secondary" type="button">取消</button>
          <button id="submit-import" class="button" type="submit">开始拉取</button>
        </footer>
      </form>
    </dialog>`;

  const input = document.querySelector('#defect-search');
  const dialog = document.querySelector('#import-dialog');
  const importForm = document.querySelector('#import-form');
  let debounceTimer = null;
  input.addEventListener('input', () => {
    clearTimeout(debounceTimer);
    listRequestToken += 1;
    const query = input.value.trim();
    debounceTimer = setTimeout(() => refreshDefects(query, 1, {resultFilter: listParams().result}), 260);
  });
  document.querySelector('#clear-search').addEventListener('click', () => {
    clearTimeout(debounceTimer);
    input.value = '';
    input.focus();
    refreshDefects('', 1, {resultFilter: listParams().result});
  });
  document.querySelector('#open-import').addEventListener('click', () => dialog.showModal());
  document.querySelector('#close-import').addEventListener('click', () => dialog.close());
  document.querySelector('#cancel-import').addEventListener('click', () => dialog.close());
  importForm.addEventListener('submit', async event => {
    event.preventDefault();
    const submit = document.querySelector('#submit-import');
    const limitInput = document.querySelector('#import-limit');
    const limit = Number(limitInput.value || 0);
    if (!Number.isInteger(limit) || limit < 0) {
      showToast('数量必须是大于或等于 0 的整数', 'error');
      limitInput.focus();
      return;
    }
    submit.disabled = true;
    submit.textContent = '正在提交…';
    try {
      const job = await api('/api/defects/import', {
        method: 'POST',
        body: JSON.stringify({
          include_completed: importForm.elements['import-type'].value === 'all',
          limit,
          force: document.querySelector('#import-force').checked
        })
      });
      dialog.close();
      activeImportJobId = job.id;
      localStorage.setItem(IMPORT_STORAGE_KEY, job.id);
      renderImportProgress(job);
      showToast('缺陷拉取任务已启动');
      pollImport(job.id);
    } catch (error) {
      showToast(error.status === 409 ? '已有拉取任务在运行' : error.message, error.status === 409 ? 'warning' : 'error');
    } finally {
      submit.disabled = false;
      submit.textContent = '开始拉取';
    }
  });
  app.onclick = event => {
    const resultButton = event.target.closest('[data-result-filter]');
    if (resultButton) {
      const resultFilter = resultButton.dataset.resultFilter;
      setListUrl(input.value.trim(), 1, resultFilter, 'push');
      refreshDefects(input.value.trim(), 1, {resultFilter});
      return;
    }
    const pageButton = event.target.closest('[data-page]');
    if (pageButton && !pageButton.disabled) {
      const page = Number(pageButton.dataset.page);
      const resultFilter = listParams().result;
      setListUrl(input.value.trim(), page, resultFilter, 'push');
      refreshDefects(input.value.trim(), page, {urlMode: 'replace', resultFilter});
      window.scrollTo({ top: 0, behavior: 'smooth' });
      return;
    }
    if (event.target.closest('[data-clear-search]')) {
      input.value = '';
      refreshDefects('', 1, {resultFilter: listParams().result});
      input.focus();
      return;
    }
    if (event.target.closest('[data-retry-list]')) {
      const params = listParams();
      refreshDefects(input.value.trim(), params.page, {resultFilter: params.result});
    }
  };
  await refreshDefects(initial.query, initial.page, {resultFilter: initial.result});
  restoreImportProgress();
  rememberProject(currentProject());
  await refreshActiveRepairPanel();
}

function testListParams() {
  const params = new URLSearchParams(location.search);
  const rawPage = Number.parseInt(params.get('page') || '1', 10);
  const state = String(params.get('state') || DEFAULT_TEST_STATE).toLowerCase();
  const project = String(params.get('project') || currentProject());
  const rawCategory = String(params.get('category') || 'all');
  const category = rawCategory === 'all' || Object.hasOwn(FUNCTION_CATEGORIES, rawCategory) ? rawCategory : 'all';
  const rawModule = String(params.get('module') || '');
  const module = ALL_FUNCTION_MODULES.includes(rawModule) ? rawModule : '';
  return {
    query: (params.get('q') || '').trim(),
    page: Number.isFinite(rawPage) && rawPage > 0 ? rawPage : 1,
    state: Object.hasOwn(TEST_FILTER_LABELS, state) ? state : DEFAULT_TEST_STATE,
    project: Object.hasOwn(TEST_PROJECTS, project) ? project : DEFAULT_TEST_PROJECT,
    category,
    module
  };
}

function buildTestListUrl(query, page, state = DEFAULT_TEST_STATE, project = DEFAULT_TEST_PROJECT, category = 'all', module = '') {
  const params = new URLSearchParams();
  params.set('project', testProject(project).project);
  if (query.trim()) params.set('q', query.trim());
  if (state !== DEFAULT_TEST_STATE) params.set('state', state);
  if (category !== 'all' && Object.hasOwn(FUNCTION_CATEGORIES, category)) params.set('category', category);
  if (module && ALL_FUNCTION_MODULES.includes(module)) params.set('module', module);
  if (page > 1) params.set('page', String(page));
  return params.size ? `/cases?${params.toString()}` : '/cases';
}

function setTestListUrl(query, page, state = DEFAULT_TEST_STATE, project = DEFAULT_TEST_PROJECT, mode = 'replace', category = 'all', module = '') {
  const url = buildTestListUrl(query, page, state, project, category, module);
  history[mode === 'push' ? 'pushState' : 'replaceState']({}, '', url);
}

function testReturnUrl() {
  const fallbackProject = testListParams().project;
  const fallback = buildTestListUrl('', 1, DEFAULT_TEST_STATE, fallbackProject);
  const raw = new URLSearchParams(location.search).get('from');
  if (!raw) return fallback;
  try {
    const target = new URL(raw, location.origin);
    if (target.origin !== location.origin || !['/tests', '/cases'].includes(target.pathname)) return fallback;
    const params = new URLSearchParams(target.search);
    const rawPage = Number.parseInt(params.get('page') || '1', 10);
    const page = Number.isFinite(rawPage) && rawPage > 0 ? rawPage : 1;
    const rawState = String(params.get('state') || DEFAULT_TEST_STATE).toLowerCase();
    const state = Object.hasOwn(TEST_FILTER_LABELS, rawState) ? rawState : DEFAULT_TEST_STATE;
    const project = String(params.get('project') || DEFAULT_TEST_PROJECT);
    return buildTestListUrl(
      (params.get('q') || '').trim(), page, state,
      Object.hasOwn(TEST_PROJECTS, project) ? project : DEFAULT_TEST_PROJECT,
      params.get('category') || 'all',
      params.get('module') || ''
    );
  } catch {
    return fallback;
  }
}

function testReportReturnUrl() {
  const raw = new URLSearchParams(location.search).get('from');
  if (!raw) return '';
  try {
    const target = new URL(raw, location.origin);
    if (target.origin !== location.origin || target.pathname !== '/reports') return '';
    const params = target.searchParams;
    const project = String(params.get('project') || currentProject());
    const view = ['overview', 'batches', 'cases', 'failures'].includes(params.get('view')) ? params.get('view') : 'overview';
    const from = /^\d{4}-\d{2}-\d{2}$/.test(params.get('from') || '') ? params.get('from') : '';
    const to = /^\d{4}-\d{2}-\d{2}$/.test(params.get('to') || '') ? params.get('to') : '';
    const module = ALL_FUNCTION_MODULES.includes(params.get('module')) ? params.get('module') : '';
    return pageUrl('/reports', testProject(project).project, {view, from, to, module});
  } catch {
    return '';
  }
}

function withTestReturn(path, project = DEFAULT_TEST_PROJECT, returnTo = '/cases') {
  const params = new URLSearchParams({project: testProject(project).project, from: returnTo});
  return `${path}?${params.toString()}`;
}

function testDetailHref(project, sheet, caseId, returnTo = '/cases') {
  return withTestReturn(`/test/${encodeURIComponent(sheet)}/${encodeURIComponent(caseId)}`, project, returnTo);
}

function testHistoryHref(project, sheet, caseId, runId, returnTo = '/cases') {
  return withTestReturn(`/test-history/${encodeURIComponent(sheet)}/${encodeURIComponent(caseId)}/${encodeURIComponent(runId)}`, project, returnTo);
}

function testMetricCards(summary = {}, activeState = DEFAULT_TEST_STATE) {
  const maturityCards = [
    ['all', 'all', '全部用例'],
    ['unexplored', 'unexplored', '尚未外部探索'],
    ['externally_explored', 'externally_explored', '已经外部探索'],
    ['explored_unsolidified', 'explored_unsolidified', '已探索但未固化'],
    ['solidified', 'solidified', '已固化、Agent-loop 可执行']
  ];
  const renderCards = cards => cards.map(([state, countKey, label]) => `
    <button class="metric${activeState === state ? ' is-active' : ''}" type="button" data-test-filter="${state}" aria-pressed="${activeState === state}">
      <strong>${Number(summary[countKey] || 0)}</strong><span>${label}</span>
    </button>`).join('');
  return `
    <section class="test-metric-group" aria-labelledby="maturity-status-title">
      <header class="test-metric-heading">
        <div><strong id="maturity-status-title">外部探索与固化</strong><span>分类只取外部账本和正式 case_map</span></div>
        <small>全部 ${Number(summary.all || 0)}</small>
      </header>
      <div class="metrics test-metric-grid maturity-metrics">${renderCards(maturityCards)}</div>
    </section>`;
}

function testRows(items, query, state, returnTo = '/cases') {
  if (!items.length) {
    return `<div class="empty-state compact">
      <strong>${query ? '没有找到匹配的测试用例' : `没有${TEST_FILTER_LABELS[state]}的用例`}</strong>
      <p>${query ? '可以减少关键词，或清除当前搜索条件。' : '可以选择其他分类查看用例。'}</p>
      ${query ? '<button class="button button-secondary" type="button" data-clear-test-search>清除搜索条件</button>' : ''}
    </div>`;
  }
  const rows = items.map(row => {
    const sheet = String(row.file_sheet || row.sheet || '');
    const caseId = String(row.case_id || '');
    const selected = selectedTestCases.has(testCaseSelectionKey(sheet, caseId));
    return `<tr class="test-row-shell${selected ? ' is-selected' : ''}">
      <td><label class="table-checkbox" title="${escapeHtml(`选择 ${caseId} 创建精确批次`)}"><input type="checkbox" data-test-case-select data-sheet="${escapeHtml(sheet)}" data-case-id="${escapeHtml(caseId)}" aria-label="选择用例 ${escapeHtml(caseId)}" ${selected ? 'checked' : ''}></label></td>
      <td><a class="case-id-link" href="${escapeHtml(testDetailHref(row.project, sheet, caseId, returnTo))}">${escapeHtml(caseId)}</a><small>${escapeHtml(sheet)}</small></td>
      <td class="case-purpose-cell"><strong>${escapeHtml(row.steps_text || row.expected_text || '未填写测试步骤')}</strong><small>${escapeHtml(row.expected_text || '未填写预期结果')}</small></td>
      <td><span class="chip chip-status priority-${escapeHtml(String(row.priority || '').toLowerCase())}">${escapeHtml(row.priority || '未分级')}</span></td>
      <td>${maturityChip(row)}</td>
      <td>${caseStatusChip(row)}</td>
      <td><time>${row.last_run_at ? formatTime(row.last_run_at) : '—'}</time>${row.history_count ? `<small>${Number(row.history_count)} 次</small>` : ''}</td>
      <td class="table-actions"><a class="table-icon-action" href="${escapeHtml(testDetailHref(row.project, sheet, caseId, returnTo))}" title="查看并运行 ${escapeHtml(caseId)}" aria-label="查看并运行 ${escapeHtml(caseId)}">${icon('runs', 17)}</a><button class="table-icon-action edit-case-btn" type="button" title="编辑用例 ${escapeHtml(caseId)}" data-edit-case data-sheet="${escapeHtml(sheet)}" data-case-id="${escapeHtml(caseId)}" data-priority="${escapeHtml(row.priority || 'P1')}" data-precondition="${escapeHtml(row.precondition_text || '')}" data-steps="${escapeHtml(row.steps_text || '')}" data-expected="${escapeHtml(row.expected_text || '')}" data-note="${escapeHtml(row.note || '')}" aria-label="编辑用例 ${escapeHtml(caseId)}">✎</button></td>
    </tr>`;
  }).join('');
  return `<div class="workspace-table-scroll"><table class="workspace-table case-table"><thead><tr><th class="checkbox-column"></th><th>用例编号</th><th>测试点</th><th>优先级</th><th>成熟度</th><th>最近结果</th><th>最近运行</th><th>操作</th></tr></thead><tbody>${rows}</tbody></table></div>`;
}

function testCaseSelectionKey(sheet, caseId) {
  return `${String(sheet || '')}\u001f${String(caseId || '')}`;
}

function ensureTestSelectionProject(project) {
  if (selectedTestProject === project) return;
  selectedTestProject = project;
  selectedTestCases.clear();
  currentTestPageItems = [];
}

function selectedTestCaseList() {
  return [...selectedTestCases.values()].sort((left, right) =>
    left.sheet.localeCompare(right.sheet, 'zh-CN')
    || left.case_id.localeCompare(right.case_id, 'zh-CN', {numeric: true})
  );
}

function updateSelectedTestCasesUi() {
  document.querySelectorAll('[data-test-case-select]').forEach(input => {
    const key = testCaseSelectionKey(input.dataset.sheet, input.dataset.caseId);
    const selected = selectedTestCases.has(key);
    input.checked = selected;
    const row = input.closest('.test-row-shell');
    if (row) {
      row.classList.toggle('is-selected', selected);
    }
  });

  const casesOnPage = currentTestPageItems;
  const selectedOnPage = casesOnPage.filter(item => selectedTestCases.has(
    testCaseSelectionKey(item.file_sheet || item.sheet, item.case_id)
  )).length;
  const selectPage = document.querySelector('#select-test-page');
  if (selectPage) {
    selectPage.disabled = casesOnPage.length < 1;
    selectPage.checked = casesOnPage.length > 0 && selectedOnPage === casesOnPage.length;
    selectPage.indeterminate = selectedOnPage > 0 && selectedOnPage < casesOnPage.length;
  }

  const count = selectedTestCases.size;
  const summary = document.querySelector('#selected-test-summary');
  if (summary) {
    summary.textContent = count
      ? `已勾选 ${count} 条，可继续翻页或搜索添加`
      : '尚未勾选用例；勾选项可跨分页保留';
  }
  const clear = document.querySelector('#clear-selected-tests');
  if (clear) clear.disabled = count < 1;
  const run = document.querySelector('#run-selected-tests');
  if (run) {
    run.disabled = count < 1;
    run.textContent = count ? `用已选用例创建批次（${count}）` : '用已选用例创建批次';
  }
}

function selectedBatchCategories() {
  return [...document.querySelectorAll('input[name="batch-category"]:checked')]
    .map(input => input.value)
    .filter(value => Object.hasOwn(BATCH_CATEGORY_LABELS, value));
}

function updateBatchLaunch(summary = latestBatchCandidateSummary) {
  latestBatchCandidateSummary = summary || {};
  for (const category of Object.keys(BATCH_CATEGORY_LABELS)) {
    const count = document.querySelector(`[data-batch-count="${category}"]`);
    if (count) count.textContent = Number(latestBatchCandidateSummary[category] || 0);
  }
  const selected = selectedBatchCategories();
  const total = selected.reduce(
    (sum, category) => sum + Number(latestBatchCandidateSummary[category] || 0),
    0
  );
  const button = document.querySelector('#test-batch-button');
  if (button) {
    button.disabled = total < 1;
    button.textContent = total ? `创建状态批次（${total}）` : '所选分类没有用例';
  }
  const note = document.querySelector('#batch-selection-note');
  if (note) note.textContent = `最新结果已通过的 ${Number(latestBatchCandidateSummary.pass || 0)} 条已排除`;
}

function renderCaseCategoryTabs(activeCategory = 'all') {
  return `<button type="button" class="${activeCategory === 'all' ? 'is-active' : ''}" data-case-category="all">全部</button>${FUNCTION_CATEGORY_NAMES.map(name => `<button type="button" class="${activeCategory === name ? 'is-active' : ''}" data-case-category="${escapeHtml(name)}">${escapeHtml(name)}</button>`).join('')}`;
}

function renderModuleSidebar(moduleCounts = {}, catalogTotal = 0, activeCategory = 'all', activeModule = '') {
  const counts = new Map(Object.entries(moduleCounts || {}));
  const modules = activeCategory === 'all' ? ALL_FUNCTION_MODULES : (FUNCTION_CATEGORIES[activeCategory] || []);
  const categoryTotal = modules.reduce((total, name) => total + Number(counts.get(name) || 0), 0);
  return `<button type="button" class="module-filter ${!activeModule ? 'is-active' : ''}" data-case-module=""><span>全部模块</span><strong>${activeCategory === 'all' ? Number(catalogTotal || 0) : categoryTotal}</strong></button>${modules.map(name => `<button type="button" class="module-filter ${activeModule === name ? 'is-active' : ''}" data-case-module="${escapeHtml(name)}"><span>${escapeHtml(name)}</span><strong>${Number(counts.get(name) || 0)}</strong></button>`).join('')}`;
}

async function refreshTests(query, page, {
  urlMode = 'replace',
  state = testListParams().state,
  project = testListParams().project,
  category = testListParams().category,
  module = testListParams().module,
  force = false
} = {}) {
  const token = ++testListRequestToken;
  const list = document.querySelector('#test-list');
  const pager = document.querySelector('#test-pagination');
  const count = document.querySelector('#test-count');
  if (!list || !pager || !count) return;
  list.classList.add('is-loading');
  try {
    if (category !== 'all' && !Object.hasOwn(FUNCTION_CATEGORIES, category)) category = 'all';
    if (module && (!ALL_FUNCTION_MODULES.includes(module) || (category !== 'all' && !FUNCTION_CATEGORIES[category].includes(module)))) module = '';
    if (force) invalidateCaseCatalog(project);
    const request = new URLSearchParams({
      project,
      q: query,
      state,
      page: String(page),
      page_size: String(PAGE_SIZE)
    });
    const requestedModules = module
      ? [module]
      : (category !== 'all' ? FUNCTION_CATEGORIES[category] : []);
    requestedModules.forEach(name => request.append('module', name));
    const payload = await api(`/api/tests?${request.toString()}`);
    if (token !== testListRequestToken) return;
    ensureTestSelectionProject(payload.project);
    rememberProject(payload.project);
    const projectTarget = document.querySelector('#test-project-target');
    if (projectTarget) projectTarget.innerHTML = testTargetChip(payload);
    currentTestPageItems = payload.items || [];
    const selected = Object.hasOwn(TEST_FILTER_LABELS, payload.state_filter) ? payload.state_filter : DEFAULT_TEST_STATE;
    document.querySelector('#test-metrics').innerHTML = testMetricCards(payload.summary, selected);
    document.querySelector('#test-queue-title').textContent = TEST_FILTER_LABELS[selected];
    const categoryTabs = document.querySelector('#case-category-tabs');
    if (categoryTabs) categoryTabs.innerHTML = renderCaseCategoryTabs(category);
    const moduleSidebar = document.querySelector('#case-module-sidebar');
    if (moduleSidebar) moduleSidebar.innerHTML = renderModuleSidebar(payload.module_counts, payload.catalog_total, category, module);
    updateBatchLaunch(payload.batch_summary);
    const returnTo = buildTestListUrl(query, payload.page || 1, selected, payload.project, category, module);
    list.innerHTML = testRows(currentTestPageItems, query, selected, returnTo);
    updateSelectedTestCasesUi();
    pager.innerHTML = pagination(payload);
    count.textContent = query ? `找到 ${payload.total} 条` : `共 ${payload.total} 条`;
    setTestListUrl(query, payload.page || 1, selected, payload.project, urlMode, category, module);
  } catch (error) {
    if (token !== testListRequestToken) return;
    currentTestPageItems = [];
    list.innerHTML = `<div class="empty-state compact"><strong>测试用例读取失败</strong><p>${escapeHtml(error.message)}</p><button class="button button-secondary" type="button" data-retry-tests>重试</button></div>`;
    updateSelectedTestCasesUi();
    pager.innerHTML = '';
    count.textContent = '读取失败';
  } finally {
    if (token === testListRequestToken) list.classList.remove('is-loading');
  }
}

async function renderTests() {
  setActiveNav('cases');
  document.title = '用例管理 · Agent-loop';
  const initial = testListParams();
  const initialProject = testProject(initial.project);
  ensureTestSelectionProject(initial.project);
  app.innerHTML = `
    ${Components.pageHeader({title: '用例管理', intro: '按功能模块组织、探索并固化自动化测试用例', actions: '<button id="open-case-create" class="button" type="button">' + icon('plus', 17) + ' 新建用例</button>'})}
    <section id="active-test-batch" class="batch-active" hidden></section>
    <section class="workspace-card case-management-card">
      <nav id="case-category-tabs" class="case-category-tabs" aria-label="功能分类">${renderCaseCategoryTabs(initial.category)}</nav>
      <div class="case-toolbar" aria-label="测试用例工具">
        <div class="project-stage">
          <label class="sr-only" for="test-project">项目与目标</label>
          <select id="test-project" aria-label="选择测试项目">
            ${Object.values(TEST_PROJECTS).map(item => `<option value="${escapeHtml(item.project)}" ${item.project === initial.project ? 'selected' : ''}>${escapeHtml(item.projectLabel)} · ${escapeHtml(item.targetLabel)}</option>`).join('')}
          </select>
          <small id="test-project-target">${testTargetChip(initialProject)}</small>
        </div>
        <div class="search-box case-search-box">
          ${icon('search', 18)}
          <input id="test-search" type="search" value="${escapeHtml(initial.query)}" placeholder="搜索用例编号、模块、步骤" autocomplete="off">
          <button id="clear-test-search" class="clear-search" type="button" aria-label="清空搜索">清空</button>
        </div>
        <div class="case-toolbar-actions">
          <button id="open-excel-import" class="button button-secondary" type="button">导入 Excel</button>
          <button id="export-excel-btn" class="button button-secondary" type="button">导出 Excel</button>
          <button id="open-migration-btn" class="button button-secondary" type="button">跨端迁移</button>
        </div>
      </div>
      <div class="case-workspace-grid">
        <aside id="case-module-sidebar" class="module-sidebar" aria-label="功能模块">${renderModuleSidebar({}, 0, initial.category, initial.module)}</aside>
        <div class="case-main-column">
          <section id="test-metrics" class="test-metric-groups" aria-label="测试用例统计与筛选">${testMetricCards({}, initial.state)}</section>
          <details class="batch-launch case-batch-launch">
            <summary><strong>按运行状态创建批次</strong><small id="batch-selection-note">正在统计历史结果…</small></summary>
            <div class="batch-scope" aria-label="选择批次范围">
              ${Object.entries(BATCH_CATEGORY_LABELS).map(([value, label]) => `<label class="batch-option"><input type="checkbox" name="batch-category" value="${value}" checked><span>${label}</span><strong data-batch-count="${value}">0</strong></label>`).join('')}
            </div>
            <div class="batch-launch-actions"><button id="test-batch-button" class="button" type="button">读取候选数量…</button></div>
          </details>
          <section class="panel queue-panel case-table-panel">
            <header class="panel-head">
              <div><h2 id="test-queue-title">${TEST_FILTER_LABELS[initial.state]}</h2><p>用例身份、成熟度与最近一次真实运行结果</p></div>
              <span id="test-count" class="chip chip-pending">正在读取</span>
            </header>
            <div id="test-list" class="defect-list"><div class="list-loading">正在读取测试用例…</div></div>
            <div id="test-pagination"></div>
          </section>
          <section class="test-selection-bar" aria-label="精确选择测试批次">
            <div class="test-selection-copy"><strong id="selected-test-summary">尚未勾选用例；勾选项可跨分页保留</strong><label class="test-page-selector"><input id="select-test-page" type="checkbox"><span>全选当前页</span></label></div>
            <div class="test-selection-actions"><button id="clear-selected-tests" class="button button-secondary" type="button" disabled>清空</button><button id="run-selected-tests" class="button" type="button" disabled>运行所选</button></div>
          </section>
        </div>
      </div>
    </section>
    <dialog id="excel-import-dialog" class="import-dialog" aria-labelledby="excel-dialog-title">
      <form id="excel-import-form">
        <header class="dialog-head">
          <div><p class="eyebrow">用例映射导入</p><h2 id="excel-dialog-title">导入 Excel 测试用例</h2></div>
          <button id="close-excel-import" class="dialog-close" type="button" aria-label="关闭">×</button>
        </header>
        <div class="dialog-body">
          <div class="import-project-badge" style="background: var(--surface-soft); padding: 8px 12px; border-radius: 6px; font-size: 13px;">
            <strong>目标项目：</strong>
            <span id="excel-target-project-label"></span>
          </div>
          <div id="excel-dropzone" class="excel-upload-zone">
            <input id="excel-file-input" type="file" accept=".xlsx" style="display: none;">
            <div class="dropzone-content">
              <div class="dropzone-title" id="dropzone-title-text">选择或拖拽 Excel 文件 (.xlsx) 至此处</div>
              <small class="dropzone-subtitle" id="dropzone-subtitle-text">必须包含「自动化测试用例_v1」工作表与 9 列表头</small>
              <div class="dropzone-prompt" id="dropzone-file-name">点击选择文件</div>
            </div>
          </div>
          <div id="excel-preview-box" class="excel-preview-box" style="display: none;">
            <div class="preview-stats" style="display: flex; gap: 8px; margin-bottom: 8px;">
              <span class="chip chip-pass">新增未固化: <strong id="preview-new-count">0</strong> 条</span>
              <span class="chip">已存在: <strong id="preview-skip-count">0</strong> 条</span>
              <span class="chip">总计: <strong id="preview-total-count">0</strong> 条</span>
            </div>
            <p id="preview-modules-info" style="font-size: 12px; color: var(--muted); margin: 4px 0;"></p>
            <div id="preview-sample-list" style="max-height: 120px; overflow-y: auto; font-size: 12px; background: var(--surface-soft); padding: 8px; border-radius: 4px;"></div>
            <div class="import-mode-selector" style="margin-top: 10px; padding: 8px 12px; background: var(--surface-soft); border-radius: 6px; font-size: 13px;">
              <strong>同名用例处理策略：</strong>
              <div style="display: flex; gap: 16px; margin-top: 6px;">
                <label style="display: flex; align-items: center; gap: 4px; cursor: pointer;">
                  <input type="radio" name="excel-import-overwrite-mode" value="skip" checked>
                  <span>增量导入（跳过已存在）</span>
                </label>
                <label style="display: flex; align-items: center; gap: 4px; cursor: pointer;">
                  <input type="radio" name="excel-import-overwrite-mode" value="overwrite">
                  <span>覆盖更新（替换同名内容）</span>
                </label>
              </div>
            </div>
          </div>
          <div id="excel-error-box" class="error-banner" style="display: none; color: #d32f2f; background: #ffebee; padding: 8px 12px; border-radius: 4px; font-size: 13px;"></div>
          <div id="excel-report-box" class="report-banner" style="display: none; color: #2e7d32; background: #e8f5e9; padding: 8px 12px; border-radius: 4px; font-size: 13px;"></div>
        </div>
        <footer class="dialog-actions">
          <button id="cancel-excel-import" class="button button-secondary" type="button">取消</button>
          <button id="submit-excel-import" class="button" type="button" disabled>确认导入</button>
        </footer>
      </form>
    </dialog>
    <dialog id="case-edit-dialog" class="import-dialog case-edit-dialog" aria-labelledby="case-dialog-title" style="max-width: 620px;">
      <form id="case-edit-form">
        <header class="dialog-head">
          <div>
            <p class="eyebrow" id="case-dialog-eyebrow">用例管理</p>
            <h2 id="case-dialog-title">添加测试用例</h2>
          </div>
          <button id="close-case-edit" class="dialog-close" type="button" aria-label="关闭">×</button>
        </header>
        <div class="dialog-body" style="display: flex; flex-direction: column; gap: 12px;">
          <div style="background: var(--surface-soft); padding: 8px 12px; border-radius: 6px; font-size: 13px;">
            <strong>目标项目：</strong>
            <span id="case-edit-project-label"></span>
          </div>
          <input type="hidden" id="case-edit-mode" value="create">
          <input type="hidden" id="case-edit-orig-id" value="">
          <div style="display: flex; gap: 12px;">
            <div style="flex: 1;">
              <label for="case-input-id" style="display: block; font-size: 12px; font-weight: bold; margin-bottom: 4px;">用例编号 *</label>
              <input id="case-input-id" type="text" class="input" placeholder="例如: CALC_002" required style="width: 100%; padding: 6px 10px; border-radius: 4px; border: 1px solid var(--border);">
            </div>
            <div style="flex: 1;">
              <label for="case-input-sheet" style="display: block; font-size: 12px; font-weight: bold; margin-bottom: 4px;">所属模块 *</label>
              <input id="case-input-sheet" type="text" class="input" placeholder="例如: 计算器" required style="width: 100%; padding: 6px 10px; border-radius: 4px; border: 1px solid var(--border);">
            </div>
            <div style="width: 90px;">
              <label for="case-input-priority" style="display: block; font-size: 12px; font-weight: bold; margin-bottom: 4px;">优先级</label>
              <select id="case-input-priority" style="width: 100%; padding: 6px; border-radius: 4px; border: 1px solid var(--border);">
                <option value="P0">P0</option>
                <option value="P1" selected>P1</option>
                <option value="P2">P2</option>
                <option value="P3">P3</option>
              </select>
            </div>
          </div>
          <div>
            <label for="case-input-precondition" style="display: block; font-size: 12px; font-weight: bold; margin-bottom: 4px;">前置条件</label>
            <input id="case-input-precondition" type="text" class="input" placeholder="例如: 手表已返回主表盘页面" style="width: 100%; padding: 6px 10px; border-radius: 4px; border: 1px solid var(--border);">
          </div>
          <div>
            <label for="case-input-steps" style="display: block; font-size: 12px; font-weight: bold; margin-bottom: 4px;">操作步骤 *</label>
            <textarea id="case-input-steps" rows="4" class="input" placeholder="1. 点击应用列表进入计算器&#10;2. 点击数字按键1&#10;3. 点击加号按键+&#10;4. 点击数字按键1&#10;5. 点击等号按键=" required style="width: 100%; padding: 6px 10px; border-radius: 4px; border: 1px solid var(--border); font-family: inherit; resize: vertical;"></textarea>
          </div>
          <div>
            <label for="case-input-expected" style="display: block; font-size: 12px; font-weight: bold; margin-bottom: 4px;">预期结果 *</label>
            <textarea id="case-input-expected" rows="3" class="input" placeholder="屏幕中央结果区域正确显示数值 2" required style="width: 100%; padding: 6px 10px; border-radius: 4px; border: 1px solid var(--border); font-family: inherit; resize: vertical;"></textarea>
          </div>
          <div>
            <label for="case-input-note" style="display: block; font-size: 12px; font-weight: bold; margin-bottom: 4px;">备注</label>
            <input id="case-input-note" type="text" class="input" placeholder="可选备注信息" style="width: 100%; padding: 6px 10px; border-radius: 4px; border: 1px solid var(--border);">
          </div>
          <div id="case-edit-error" class="error-banner" style="display: none; color: #d32f2f; background: #ffebee; padding: 8px 12px; border-radius: 4px; font-size: 13px;"></div>
        </div>
        <footer class="dialog-actions">
          <button id="cancel-case-edit" class="button button-secondary" type="button">取消</button>
          <button id="submit-case-edit" class="button" type="submit">保存用例</button>
        </footer>
      </form>
    </dialog>
    <dialog id="migration-dialog" class="import-dialog migration-dialog" aria-labelledby="migration-dialog-title" style="max-width: 760px; width: 92vw;">
      <div>
        <header class="dialog-head">
          <div>
            <p class="eyebrow">跨端用例迁移与独立固化</p>
            <h2 id="migration-dialog-title">6202 模拟器 → 6202 真机用例迁移</h2>
          </div>
          <button id="close-migration-dialog" class="dialog-close" type="button" aria-label="关闭">×</button>
        </header>
        <div class="dialog-body" style="display: flex; flex-direction: column; gap: 12px; max-height: 65vh; overflow-y: auto;">
          <div style="background: var(--surface-soft); padding: 10px 14px; border-radius: 6px; font-size: 13px; color: var(--ink-soft);">
            <strong>迁移规则说明：</strong>
            仅复用 6202 模拟器已 PROMOTED 用例的业务意图与检查点。真机将独立发命令、采 MTP 截图并走门禁审计。若两端 Excel 文本不一致，系统将触发安全防御拦截（DIVERGED）。
          </div>
          <div id="migration-loading" style="text-align: center; padding: 20px;">
            <div class="spinner" style="margin: 0 auto 8px;"></div>
            <p style="font-size: 13px; color: var(--ink-soft);">正在扫描模拟器与真机用例差集…</p>
          </div>
          <div id="migration-candidates-box" style="display: none;">
            <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px;">
              <span id="migration-summary-text" style="font-size: 13px; font-weight: bold;"></span>
              <button id="migrate-all-ready-btn" class="button" type="button" style="padding: 4px 12px; font-size: 12px;">一键迁移全部就绪用例</button>
            </div>
            <div id="migration-table-container" style="max-height: 280px; overflow-y: auto; border: 1px solid var(--line); border-radius: 6px;">
              <table style="width: 100%; border-collapse: collapse; font-size: 12px; text-align: left;">
                <thead style="background: var(--surface-soft); position: sticky; top: 0;">
                  <tr>
                    <th style="padding: 8px 10px; border-bottom: 1px solid var(--line);">用例编号</th>
                    <th style="padding: 8px 10px; border-bottom: 1px solid var(--line);">模块</th>
                    <th style="padding: 8px 10px; border-bottom: 1px solid var(--line);">迁移状态</th>
                    <th style="padding: 8px 10px; border-bottom: 1px solid var(--line); text-align: right;">操作</th>
                  </tr>
                </thead>
                <tbody id="migration-candidates-tbody"></tbody>
              </table>
            </div>
          </div>
        </div>
        <footer class="dialog-actions" style="margin-top: 12px;">
          <button id="cancel-migration-dialog" class="button button-secondary" type="button">关闭</button>
        </footer>
      </div>
    </dialog>`;

  const input = document.querySelector('#test-search');
  const projectSelect = document.querySelector('#test-project');
  const list = document.querySelector('#test-list');
  let debounceTimer = null;
  input.addEventListener('input', () => {
    clearTimeout(debounceTimer);
    testListRequestToken += 1;
    const query = input.value.trim();
    debounceTimer = setTimeout(() => {
      const params = testListParams();
      refreshTests(query, 1, {state: params.state, project: projectSelect.value, category: params.category, module: params.module});
    }, 260);
  });
  projectSelect.addEventListener('change', () => {
    const params = testListParams();
    const state = params.state;
    rememberProject(projectSelect.value);
    ensureTestSelectionProject(projectSelect.value);
    updateSelectedTestCasesUi();
    setTestListUrl(input.value.trim(), 1, state, projectSelect.value, 'push', params.category, params.module);
    refreshTests(input.value.trim(), 1, {state, project: projectSelect.value, category: params.category, module: params.module});
  });
  document.querySelector('#clear-test-search').addEventListener('click', () => {
    clearTimeout(debounceTimer);
    input.value = '';
    input.focus();
    const params = testListParams();
    refreshTests('', 1, {state: params.state, project: projectSelect.value, category: params.category, module: params.module});
  });
  document.querySelectorAll('input[name="batch-category"]').forEach(input => {
    input.addEventListener('change', () => updateBatchLaunch());
  });
  list.addEventListener('change', event => {
    const checkbox = event.target.closest('[data-test-case-select]');
    if (!checkbox || checkbox.disabled) return;
    const selectedCase = {
      sheet: checkbox.dataset.sheet,
      case_id: checkbox.dataset.caseId,
    };
    const key = testCaseSelectionKey(selectedCase.sheet, selectedCase.case_id);
    if (checkbox.checked) selectedTestCases.set(key, selectedCase);
    else selectedTestCases.delete(key);
    updateSelectedTestCasesUi();
  });
  document.querySelector('#select-test-page').addEventListener('change', event => {
    for (const item of currentTestPageItems) {
      const selectedCase = {
        sheet: String(item.file_sheet || item.sheet || ''),
        case_id: String(item.case_id || ''),
      };
      const key = testCaseSelectionKey(selectedCase.sheet, selectedCase.case_id);
      if (event.target.checked) selectedTestCases.set(key, selectedCase);
      else selectedTestCases.delete(key);
    }
    updateSelectedTestCasesUi();
  });
  document.querySelector('#clear-selected-tests').addEventListener('click', () => {
    selectedTestCases.clear();
    updateSelectedTestCasesUi();
  });
  document.querySelector('#run-selected-tests').addEventListener('click', async event => {
    const button = event.currentTarget;
    const cases = selectedTestCaseList();
    if (!cases.length) return;
    const projectMeta = testProject(projectSelect.value);
    if (!window.confirm(`确定在${projectMeta.targetLabel}运行 ${projectMeta.projectLabel} 的 ${cases.length} 条已勾选用例吗？本批次只运行勾选项，已通过用例也会重新执行。`)) return;
    button.disabled = true;
    button.textContent = '正在创建批次…';
    try {
      const job = await api('/api/tests/run-batch', {
        method: 'POST',
        body: JSON.stringify({project: projectSelect.value, cases}),
      });
      window.location.href = `/test-batch/${encodeURIComponent(job.id)}`;
    } catch (error) {
      showToast(error.message, error.status === 409 ? 'warning' : 'error');
      updateSelectedTestCasesUi();
    }
  });
  document.querySelector('#test-batch-button').addEventListener('click', async () => {
    const button = document.querySelector('#test-batch-button');
    const categories = selectedBatchCategories();
    const labels = categories.map(category => BATCH_CATEGORY_LABELS[category]);
    const total = categories.reduce(
      (sum, category) => sum + Number(latestBatchCandidateSummary[category] || 0),
      0
    );
    if (!categories.length || total < 1) {
      showToast('请至少选择一类有用例的测试范围', 'warning');
      return;
    }
    const projectMeta = testProject(projectSelect.value);
    if (!window.confirm(`确定在${projectMeta.targetLabel}运行 ${projectMeta.projectLabel} 的所选 ${total} 条吗？范围：${labels.join('、')}。最新结果已通过的用例不会重跑。`)) return;
    button.disabled = true;
    button.textContent = '正在创建批次…';
    try {
      const job = await api('/api/tests/run-batch', {
        method: 'POST',
        body: JSON.stringify({limit: 0, categories, project: projectSelect.value})
      });
      window.location.href = `/test-batch/${encodeURIComponent(job.id)}`;
    } catch (error) {
      showToast(error.message, error.status === 409 ? 'warning' : 'error');
      try {
        const payload = await api(`/api/tests?project=${encodeURIComponent(projectSelect.value)}&page=1&page_size=1`);
        updateBatchLaunch(payload.batch_summary);
      } catch (refreshError) {
        showToast(`批次范围刷新失败：${refreshError.message}`, 'warning');
        updateBatchLaunch();
      }
    }
  });
  app.onclick = event => {
    const categoryButton = event.target.closest('[data-case-category]');
    if (categoryButton) {
      const params = testListParams();
      const category = categoryButton.dataset.caseCategory;
      const module = params.module && category !== 'all' && !FUNCTION_CATEGORIES[category]?.includes(params.module) ? '' : params.module;
      setTestListUrl(input.value.trim(), 1, params.state, projectSelect.value, 'push', category, module);
      refreshTests(input.value.trim(), 1, {state: params.state, project: projectSelect.value, category, module});
      return;
    }
    const moduleButton = event.target.closest('[data-case-module]');
    if (moduleButton) {
      const params = testListParams();
      const module = moduleButton.dataset.caseModule || '';
      setTestListUrl(input.value.trim(), 1, params.state, projectSelect.value, 'push', params.category, module);
      refreshTests(input.value.trim(), 1, {state: params.state, project: projectSelect.value, category: params.category, module});
      return;
    }
    const stateButton = event.target.closest('[data-test-filter]');
    if (stateButton) {
      const params = testListParams();
      const state = stateButton.dataset.testFilter;
      setTestListUrl(input.value.trim(), 1, state, projectSelect.value, 'push', params.category, params.module);
      refreshTests(input.value.trim(), 1, {state, project: projectSelect.value, category: params.category, module: params.module});
      return;
    }
    const pageButton = event.target.closest('[data-page]');
    if (pageButton && !pageButton.disabled) {
      const page = Number(pageButton.dataset.page);
      const params = testListParams();
      const state = params.state;
      setTestListUrl(input.value.trim(), page, state, projectSelect.value, 'push', params.category, params.module);
      refreshTests(input.value.trim(), page, {urlMode: 'replace', state, project: projectSelect.value, category: params.category, module: params.module});
      window.scrollTo({top: 0, behavior: 'smooth'});
      return;
    }
    if (event.target.closest('[data-clear-test-search]')) {
      input.value = '';
      const params = testListParams();
      refreshTests('', 1, {state: params.state, project: projectSelect.value, category: params.category, module: params.module});
      input.focus();
      return;
    }
    if (event.target.closest('[data-retry-tests]')) {
      const params = testListParams();
      refreshTests(input.value.trim(), params.page, {state: params.state, project: params.project, category: params.category, module: params.module, force: true});
    }
  };

  const excelDialog = document.querySelector('#excel-import-dialog');
  const openExcelBtn = document.querySelector('#open-excel-import');
  const closeExcelBtn = document.querySelector('#close-excel-import');
  const cancelExcelBtn = document.querySelector('#cancel-excel-import');
  const submitExcelBtn = document.querySelector('#submit-excel-import');
  const excelDropzone = document.querySelector('#excel-dropzone');
  const excelFileInput = document.querySelector('#excel-file-input');
  const dropzoneTitle = document.querySelector('#dropzone-title-text');
  const dropzoneFileName = document.querySelector('#dropzone-file-name');
  const excelTargetLabel = document.querySelector('#excel-target-project-label');
  const excelPreviewBox = document.querySelector('#excel-preview-box');
  const excelErrorBox = document.querySelector('#excel-error-box');
  const excelReportBox = document.querySelector('#excel-report-box');
  let currentExcelBase64 = null;
  let currentExcelFileName = '';

  function resetExcelDialog() {
    currentExcelBase64 = null;
    currentExcelFileName = '';
    if (excelFileInput) excelFileInput.value = '';
    if (dropzoneTitle) dropzoneTitle.textContent = '选择或拖拽 Excel 文件 (.xlsx) 至此处';
    if (dropzoneFileName) dropzoneFileName.textContent = '点击选择文件';
    if (excelDropzone) excelDropzone.classList.remove('is-dragover');
    if (excelPreviewBox) excelPreviewBox.style.display = 'none';
    if (excelErrorBox) excelErrorBox.style.display = 'none';
    if (excelReportBox) excelReportBox.style.display = 'none';
    if (submitExcelBtn) {
      submitExcelBtn.disabled = true;
      submitExcelBtn.textContent = '确认导入';
    }
    const pMeta = testProject(projectSelect.value);
    if (excelTargetLabel) excelTargetLabel.textContent = `${pMeta.projectLabel} · ${pMeta.targetLabel}`;
  }

  function handleExcelFile(file) {
    if (!file) return;
    if (!file.name.toLowerCase().endsWith('.xlsx')) {
      if (excelErrorBox) {
        excelErrorBox.textContent = `[FORMAT_ERROR] 仅支持 .xlsx 格式文件，当前文件「${file.name}」格式不支持`;
        excelErrorBox.style.display = 'block';
      }
      if (dropzoneFileName) dropzoneFileName.textContent = `不支持格式: ${file.name}`;
      if (submitExcelBtn) {
        submitExcelBtn.disabled = true;
        submitExcelBtn.textContent = '无法导入';
      }
      return;
    }

    currentExcelFileName = file.name;
    if (dropzoneFileName) dropzoneFileName.textContent = `已选择: ${file.name}`;
    if (excelPreviewBox) excelPreviewBox.style.display = 'none';
    if (excelErrorBox) excelErrorBox.style.display = 'none';
    if (excelReportBox) excelReportBox.style.display = 'none';
    if (submitExcelBtn) {
      submitExcelBtn.disabled = true;
      submitExcelBtn.textContent = '正在解析预览…';
    }

    const reader = new FileReader();
    reader.onload = async () => {
      const base64Data = reader.result.split(',')[1];
      currentExcelBase64 = base64Data;
      try {
        const resp = await fetch('/api/excel/preview', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({
            project: projectSelect.value,
            file_name: currentExcelFileName,
            file_base64: currentExcelBase64,
          }),
        });
        const data = await resp.json();
        if (!resp.ok) {
          excelErrorBox.textContent = `[${data.error_code || '提示'}] ${data.row_number ? `第 ${data.row_number} 行: ` : ''}${data.message || data.error || '文件解析失败'}`;
          excelErrorBox.style.display = 'block';
          submitExcelBtn.textContent = '无法导入';
          submitExcelBtn.disabled = true;
          return;
        }
        document.querySelector('#preview-new-count').textContent = String(data.new_count);
        document.querySelector('#preview-skip-count').textContent = String(data.existing_count);
        document.querySelector('#preview-total-count').textContent = String(data.total_parsed);
        document.querySelector('#preview-modules-info').textContent = `涉及模块: ${data.modules.join(', ')}`;

        const sampleHtml = (data.new_cases || []).slice(0, 5).map(c => `<div><strong>${escapeHtml(c.case_id)}</strong> (${escapeHtml(c.sheet)}) - ${escapeHtml((c.expected_text || '').slice(0, 30))}</div>`).join('') || '<div style="color: var(--muted);">本次无新增用例（全部已存在）</div>';
        document.querySelector('#preview-sample-list').innerHTML = sampleHtml;
        excelPreviewBox.style.display = 'block';

        if (data.new_count > 0) {
          submitExcelBtn.disabled = false;
          submitExcelBtn.textContent = `确认导入 (${data.new_count} 条新增)`;
        } else {
          submitExcelBtn.disabled = true;
          submitExcelBtn.textContent = '无需导入 (0 条新增)';
        }
      } catch (err) {
        excelErrorBox.textContent = `网络或服务异常: ${err.message}`;
        excelErrorBox.style.display = 'block';
        submitExcelBtn.textContent = '无法导入';
      }
    };
    reader.readAsDataURL(file);
  }

  if (openExcelBtn) {
    openExcelBtn.addEventListener('click', () => {
      resetExcelDialog();
      excelDialog.showModal();
    });
  }
  if (closeExcelBtn) closeExcelBtn.addEventListener('click', () => excelDialog.close());
  if (cancelExcelBtn) cancelExcelBtn.addEventListener('click', () => excelDialog.close());

  if (excelDropzone) {
    excelDropzone.addEventListener('click', e => {
      if (e.target !== excelFileInput && excelFileInput) {
        excelFileInput.click();
      }
    });

    ['dragenter', 'dragover'].forEach(evtName => {
      excelDropzone.addEventListener(evtName, e => {
        e.preventDefault();
        e.stopPropagation();
        excelDropzone.classList.add('is-dragover');
        if (dropzoneTitle) dropzoneTitle.textContent = '释放文件以解析测试用例';
      });
    });

    ['dragleave', 'dragend'].forEach(evtName => {
      excelDropzone.addEventListener(evtName, e => {
        e.preventDefault();
        e.stopPropagation();
        excelDropzone.classList.remove('is-dragover');
        if (dropzoneTitle) dropzoneTitle.textContent = '选择或拖拽 Excel 文件 (.xlsx) 至此处';
      });
    });

    excelDropzone.addEventListener('drop', e => {
      e.preventDefault();
      e.stopPropagation();
      excelDropzone.classList.remove('is-dragover');
      if (dropzoneTitle) dropzoneTitle.textContent = '选择或拖拽 Excel 文件 (.xlsx) 至此处';
      const dt = e.dataTransfer;
      if (dt && dt.files && dt.files.length > 0) {
        handleExcelFile(dt.files[0]);
      }
    });
  }

  if (excelFileInput) {
    excelFileInput.addEventListener('change', () => {
      if (excelFileInput.files && excelFileInput.files.length > 0) {
        handleExcelFile(excelFileInput.files[0]);
      }
    });
  }

  if (submitExcelBtn) {
    submitExcelBtn.addEventListener('click', async () => {
      if (!currentExcelBase64) return;
      const isOverwrite = document.querySelector('input[name="excel-import-overwrite-mode"]:checked')?.value === 'overwrite';
      submitExcelBtn.disabled = true;
      submitExcelBtn.textContent = '正在写入 case_map…';
      try {
        const resp = await fetch('/api/excel/confirm', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({
            project: projectSelect.value,
            file_name: currentExcelFileName,
            file_base64: currentExcelBase64,
            overwrite_existing: isOverwrite,
          }),
        });
        const data = await resp.json();
        if (!resp.ok) {
          excelErrorBox.textContent = `写入失败: ${data.message || '未知错误'}`;
          excelErrorBox.style.display = 'block';
          submitExcelBtn.textContent = '重试导入';
          submitExcelBtn.disabled = false;
          return;
        }
        const summaryMsg = isOverwrite
          ? `导入成功！新增 ${data.imported_count} 条，覆盖更新 ${data.overwritten_count || 0} 条。模块: ${(data.modules_updated || []).join(', ')}`
          : `导入成功！新增 ${data.imported_count} 条用例，跳过 ${data.skipped_count} 条已有用例。模块: ${(data.modules_updated || []).join(', ')}`;
        excelReportBox.textContent = summaryMsg;
        excelReportBox.style.display = 'block';
        submitExcelBtn.textContent = '导入完成';
        showToast(summaryMsg);
        invalidateCaseCatalog(projectSelect.value);
        const params = testListParams();
        refreshTests(input.value.trim(), 1, {state: params.state, project: projectSelect.value, category: params.category, module: params.module});
        setTimeout(() => excelDialog.close(), 1600);
      } catch (err) {
        excelErrorBox.textContent = `网络错误: ${err.message}`;
        excelErrorBox.style.display = 'block';
        submitExcelBtn.disabled = false;
      }
    });
  }

  // --- F12: Excel 导出功能 ---
  const exportExcelBtn = document.querySelector('#export-excel-btn');
  if (exportExcelBtn) {
    exportExcelBtn.addEventListener('click', () => {
      const curProj = projectSelect ? projectSelect.value : (new URLSearchParams(window.location.search).get('project') || '620C_W6830');
      const url = `/api/cases/export?project=${encodeURIComponent(curProj)}`;
      const pLabel = testProject(curProj)?.projectLabel || curProj;
      showToast(`正在生成 ${pLabel} 的 Excel 测试用例表…`);
      startBrowserDownload(url, `test_cases_${curProj}.xlsx`);
      showToast(`✅ ${pLabel} 测试用例表下载已开始`);
    });
  }

  // --- F12: 单条用例新增与编辑弹窗逻辑 ---
  const caseEditDialog = document.querySelector('#case-edit-dialog');
  const openCaseCreateBtn = document.querySelector('#open-case-create');
  const closeCaseEditBtn = document.querySelector('#close-case-edit');
  const cancelCaseEditBtn = document.querySelector('#cancel-case-edit');
  const caseEditForm = document.querySelector('#case-edit-form');
  const caseEditProjectLabel = document.querySelector('#case-edit-project-label');
  const caseEditModeInput = document.querySelector('#case-edit-mode');
  const caseEditOrigIdInput = document.querySelector('#case-edit-orig-id');
  const caseDialogTitle = document.querySelector('#case-dialog-title');
  const caseInputId = document.querySelector('#case-input-id');
  const caseInputSheet = document.querySelector('#case-input-sheet');
  const caseInputPriority = document.querySelector('#case-input-priority');
  const caseInputPrecondition = document.querySelector('#case-input-precondition');
  const caseInputSteps = document.querySelector('#case-input-steps');
  const caseInputExpected = document.querySelector('#case-input-expected');
  const caseInputNote = document.querySelector('#case-input-note');
  const caseEditErrorBox = document.querySelector('#case-edit-error');
  const caseEditSubmitBtn = document.querySelector('#submit-case-edit');

  function openCaseModal(mode = 'create', data = {}) {
    if (!caseEditDialog) return;
    const pMeta = testProject(projectSelect.value);
    if (caseEditProjectLabel) caseEditProjectLabel.textContent = `${pMeta.projectLabel} · ${pMeta.targetLabel}`;
    if (caseEditErrorBox) {
      caseEditErrorBox.textContent = '';
      caseEditErrorBox.style.display = 'none';
    }
    if (caseEditModeInput) caseEditModeInput.value = mode;
    if (caseEditOrigIdInput) caseEditOrigIdInput.value = data.case_id || '';

    if (mode === 'create') {
      if (caseDialogTitle) caseDialogTitle.textContent = '添加测试用例';
      if (caseInputId) {
        caseInputId.value = '';
        caseInputId.readOnly = false;
      }
      if (caseInputSheet) caseInputSheet.value = data.sheet || '';
      if (caseInputPriority) caseInputPriority.value = 'P1';
      if (caseInputPrecondition) caseInputPrecondition.value = '';
      if (caseInputSteps) caseInputSteps.value = '';
      if (caseInputExpected) caseInputExpected.value = '';
      if (caseInputNote) caseInputNote.value = '';
      if (caseEditSubmitBtn) caseEditSubmitBtn.textContent = '保存用例';
    } else {
      if (caseDialogTitle) caseDialogTitle.textContent = `编辑测试用例 (${data.case_id || ''})`;
      if (caseInputId) {
        caseInputId.value = data.case_id || '';
        caseInputId.readOnly = false;
      }
      if (caseInputSheet) caseInputSheet.value = data.sheet || '';
      if (caseInputPriority) caseInputPriority.value = data.priority || 'P1';
      if (caseInputPrecondition) caseInputPrecondition.value = data.precondition_text || '';
      if (caseInputSteps) caseInputSteps.value = data.steps_text || '';
      if (caseInputExpected) caseInputExpected.value = data.expected_text || '';
      if (caseInputNote) caseInputNote.value = data.note || '';
      if (caseEditSubmitBtn) caseEditSubmitBtn.textContent = '保存修改';
    }
    caseEditDialog.showModal();
  }

  if (openCaseCreateBtn) {
    openCaseCreateBtn.addEventListener('click', () => {
      openCaseModal('create');
    });
  }
  if (closeCaseEditBtn) closeCaseEditBtn.addEventListener('click', () => caseEditDialog.close());
  if (cancelCaseEditBtn) cancelCaseEditBtn.addEventListener('click', () => caseEditDialog.close());

  if (list) {
    list.addEventListener('click', e => {
      const editBtn = e.target.closest('[data-edit-case]');
      if (editBtn) {
        e.preventDefault();
        e.stopPropagation();
        openCaseModal('edit', {
          case_id: editBtn.dataset.caseId,
          sheet: editBtn.dataset.sheet,
          priority: editBtn.dataset.priority,
          precondition_text: editBtn.dataset.precondition,
          steps_text: editBtn.dataset.steps,
          expected_text: editBtn.dataset.expected,
          note: editBtn.dataset.note,
        });
      }
    });
  }

  if (caseEditForm) {
    caseEditForm.addEventListener('submit', async e => {
      e.preventDefault();
      const mode = caseEditModeInput.value;
      const origId = caseEditOrigIdInput.value;
      const caseId = caseInputId.value.trim();
      const sheet = caseInputSheet.value.trim();
      const priority = caseInputPriority.value;
      const precondition_text = caseInputPrecondition.value.trim();
      const steps_text = caseInputSteps.value.trim();
      const expected_text = caseInputExpected.value.trim();
      const note = caseInputNote.value.trim();

      if (!caseId || !/^[A-Za-z0-9_]+$/.test(caseId)) {
        if (caseEditErrorBox) {
          caseEditErrorBox.textContent = '用例编号格式错误：只能包含字母、数字和下划线';
          caseEditErrorBox.style.display = 'block';
        }
        return;
      }
      if (!sheet) {
        if (caseEditErrorBox) {
          caseEditErrorBox.textContent = '所属模块不能为空';
          caseEditErrorBox.style.display = 'block';
        }
        return;
      }
      if (!steps_text || !expected_text) {
        if (caseEditErrorBox) {
          caseEditErrorBox.textContent = '操作步骤和预期结果为必填项';
          caseEditErrorBox.style.display = 'block';
        }
        return;
      }

      if (caseEditSubmitBtn) {
        caseEditSubmitBtn.disabled = true;
        caseEditSubmitBtn.textContent = '正在保存…';
      }
      if (caseEditErrorBox) caseEditErrorBox.style.display = 'none';

      try {
        const endpoint = mode === 'create' ? '/api/cases/create' : '/api/cases/update';
        const payload = mode === 'create' ? {
          project: projectSelect.value,
          case: {
            case_id: caseId,
            sheet,
            priority,
            precondition_text,
            steps_text,
            expected_text,
            note,
          }
        } : {
          project: projectSelect.value,
          orig_case_id: origId,
          case: {
            case_id: caseId,
            sheet,
            priority,
            precondition_text,
            steps_text,
            expected_text,
            note,
          }
        };

        const resp = await fetch(endpoint, {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify(payload),
        });
        const resData = await resp.json();
        if (!resp.ok) {
          if (caseEditErrorBox) {
            caseEditErrorBox.textContent = `保存失败: ${resData.error || resData.message || '未知错误'}`;
            caseEditErrorBox.style.display = 'block';
          }
          if (caseEditSubmitBtn) {
            caseEditSubmitBtn.disabled = false;
            caseEditSubmitBtn.textContent = mode === 'create' ? '保存用例' : '保存修改';
          }
          return;
        }

        showToast(mode === 'create' ? `用例 ${caseId} 添加成功！` : `用例 ${caseId} 更新成功！`);
        caseEditDialog.close();
        invalidateCaseCatalog(projectSelect.value);
        const params = testListParams();
        refreshTests(input.value.trim(), 1, {state: params.state, project: projectSelect.value, category: params.category, module: params.module});
      } catch (err) {
        if (caseEditErrorBox) {
          caseEditErrorBox.textContent = `网络错误: ${err.message}`;
          caseEditErrorBox.style.display = 'block';
        }
      } finally {
        if (caseEditSubmitBtn) {
          caseEditSubmitBtn.disabled = false;
          caseEditSubmitBtn.textContent = mode === 'create' ? '保存用例' : '保存修改';
        }
      }
    });
  }

  // --- F14: 跨端用例迁移对话框与交互 ---
  const migrationDialog = document.querySelector('#migration-dialog');
  const openMigrationBtn = document.querySelector('#open-migration-btn');
  const closeMigrationBtn = document.querySelector('#close-migration-dialog');
  const cancelMigrationBtn = document.querySelector('#cancel-migration-dialog');
  const migrationLoading = document.querySelector('#migration-loading');
  const migrationCandidatesBox = document.querySelector('#migration-candidates-box');
  const migrationSummaryText = document.querySelector('#migration-summary-text');
  const migrationTbody = document.querySelector('#migration-candidates-tbody');
  const migrateAllReadyBtn = document.querySelector('#migrate-all-ready-btn');

  async function loadMigrationCandidates() {
    if (!migrationLoading || !migrationCandidatesBox || !migrationTbody) return;
    migrationLoading.style.display = 'block';
    migrationCandidatesBox.style.display = 'none';
    migrationTbody.innerHTML = '';

    try {
      const resp = await fetch('/api/cases/migration-candidates?source=6202_W5230_SIMULATOR&target=6202_W5230');
      const data = await resp.json();
      const list = data.candidates || [];

      const readyList = list.filter(c => c.status === 'READY' || c.status === 'TARGET_MISSING');
      const divergedList = list.filter(c => c.status === 'DIVERGED');
      const alreadyList = list.filter(c => c.status === 'ALREADY_PROMOTED');

      if (migrationSummaryText) {
        migrationSummaryText.textContent = `共 ${list.length} 条模拟器固化用例 (就绪可迁移: ${readyList.length}, 文本分叉已防御: ${divergedList.length}, 真机已固化: ${alreadyList.length})`;
      }

      if (migrateAllReadyBtn) {
        migrateAllReadyBtn.disabled = readyList.length === 0;
        migrateAllReadyBtn.textContent = `一键迁移全部就绪用例 (${readyList.length} 条)`;
      }

      if (!list.length) {
        migrationTbody.innerHTML = `<tr><td colspan="4" style="text-align: center; padding: 20px; color: var(--ink-soft);">暂无 6202 模拟器已 PROMOTED 的固化用例</td></tr>`;
      } else {
        migrationTbody.innerHTML = list.map(c => {
          let statusBadge = '';
          let actionBtn = '';
          if (c.status === 'READY') {
            statusBadge = '<span class="chip chip-pass" style="font-size: 11px;">✅ 就绪可迁移</span>';
            actionBtn = `<button class="button button-secondary migrate-single-btn" data-case-id="${escapeHtml(c.case_id)}" data-sheet="${escapeHtml(c.sheet)}" style="padding: 2px 8px; font-size: 11px;">迁移实跑</button>`;
          } else if (c.status === 'TARGET_MISSING') {
            statusBadge = '<span class="chip chip-running" style="font-size: 11px;">➕ 待自动补齐</span>';
            actionBtn = `<button class="button button-secondary migrate-single-btn" data-case-id="${escapeHtml(c.case_id)}" data-sheet="${escapeHtml(c.sheet)}" style="padding: 2px 8px; font-size: 11px;">补齐并实跑</button>`;
          } else if (c.status === 'DIVERGED') {
            statusBadge = `<span class="chip chip-fail" style="font-size: 11px;" title="${escapeHtml(c.divergence_reason || '两端文本不一致')}">⚠️ 文本分叉 (已拦截)</span>`;
            actionBtn = `<span class="muted" style="font-size: 11px;">需先对齐文本</span>`;
          } else if (c.status === 'ALREADY_PROMOTED') {
            statusBadge = '<span class="chip" style="font-size: 11px;">✨ 真机已固化</span>';
            actionBtn = `<span class="muted" style="font-size: 11px;">无需迁移</span>`;
          }
          return `<tr>
            <td style="padding: 8px 10px; border-bottom: 1px solid var(--line); font-weight: bold;">${escapeHtml(c.case_id)}</td>
            <td style="padding: 8px 10px; border-bottom: 1px solid var(--line);">${escapeHtml(c.sheet)}</td>
            <td style="padding: 8px 10px; border-bottom: 1px solid var(--line);">${statusBadge}</td>
            <td style="padding: 8px 10px; border-bottom: 1px solid var(--line); text-align: right;">${actionBtn}</td>
          </tr>`;
        }).join('');
      }

      migrationLoading.style.display = 'none';
      migrationCandidatesBox.style.display = 'block';
    } catch (err) {
      if (migrationLoading) {
        migrationLoading.innerHTML = `<p style="color: #d32f2f;">加载迁移候选失败: ${escapeHtml(err.message)}</p>`;
      }
    }
  }

  if (openMigrationBtn) {
    openMigrationBtn.addEventListener('click', () => {
      migrationDialog.showModal();
      loadMigrationCandidates();
    });
  }
  if (closeMigrationBtn) closeMigrationBtn.addEventListener('click', () => migrationDialog.close());
  if (cancelMigrationBtn) cancelMigrationBtn.addEventListener('click', () => migrationDialog.close());

  if (migrationCandidatesBox) {
    migrationCandidatesBox.addEventListener('click', async e => {
      const btn = e.target.closest('.migrate-single-btn');
      if (btn) {
        const caseId = btn.dataset.caseId;
        const sheet = btn.dataset.sheet;
        btn.disabled = true;
        btn.textContent = '启动中…';
        try {
          const resp = await fetch('/api/cases/migrate', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({case_id: caseId, sheet, source_profile: '6202_W5230_SIMULATOR', target_profile: '6202_W5230'}),
          });
          const resData = await resp.json();
          if (!resp.ok) throw new Error(resData.error || resData.message || '迁移启动失败');
          showToast(`用例 ${caseId} 迁移任务已启动！`);
          migrationDialog.close();
          window.location.href = `/test/${encodeURIComponent(sheet)}/${encodeURIComponent(caseId)}?project=6202_W5230`;
        } catch (err) {
          showToast(err.message, 'error');
          btn.disabled = false;
          btn.textContent = '迁移实跑';
        }
      }
    });
  }

  if (migrateAllReadyBtn) {
    migrateAllReadyBtn.addEventListener('click', async () => {
      const singleBtns = migrationTbody.querySelectorAll('.migrate-single-btn');
      if (!singleBtns.length) return;
      if (!window.confirm(`确定要按顺序迁移这 ${singleBtns.length} 条用例到 6202 真机吗？`)) return;

      migrateAllReadyBtn.disabled = true;
      migrateAllReadyBtn.textContent = '正在批量排队…';
      try {
        for (const btn of singleBtns) {
          const caseId = btn.dataset.caseId;
          const sheet = btn.dataset.sheet;
          await fetch('/api/cases/migrate', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({case_id: caseId, sheet, source_profile: '6202_W5230_SIMULATOR', target_profile: '6202_W5230'}),
          });
        }
        showToast(`已成功为 ${singleBtns.length} 条用例创建真机迁移实跑任务！`);
        migrationDialog.close();
        invalidateCaseCatalog('6202_W5230');
        refreshTests('', 1, {state: 'all', project: '6202_W5230', category: 'all', module: ''});
      } catch (err) {
        showToast(`批量迁移异常: ${err.message}`, 'error');
      } finally {
        migrateAllReadyBtn.disabled = false;
        migrateAllReadyBtn.textContent = '一键迁移全部就绪用例';
      }
    });
  }

  await refreshTests(initial.query, initial.page, {state: initial.state, project: initial.project, category: initial.category, module: initial.module});
  await restoreActiveBatchBanner();
}

async function restoreActiveBatchBanner() {
  const panel = document.querySelector('#active-test-batch');
  if (!panel) return;
  try {
    const payload = await api('/api/tests/active');
    const jobs = activeTestJobs(payload).filter(job => job.type === 'batch');
    if (!jobs.length) {
      panel.hidden = true;
      return;
    }
    panel.hidden = false;
    panel.innerHTML = jobs.map(job => {
      const projectMeta = testProject(job.project);
      return `<article class="batch-active-row"><div><strong>${escapeHtml(projectMeta.projectLabel)} ${escapeHtml(projectMeta.targetLabel)}批次正在运行</strong><span>${Number(job.completed || 0)} / ${Number(job.total || 0)} 条已完成</span></div><a class="button button-secondary" href="/test-batch/${encodeURIComponent(job.id)}">查看实时进度</a></article>`;
    }).join('');
  } catch {
    panel.hidden = true;
  }
}

function caseText(label, value) {
  return `<article class="case-text"><span>${label}</span><p>${escapeHtml(value || '未填写')}</p></article>`;
}

function verificationPoints(items = []) {
  const values = Array.isArray(items) ? items.map(value => String(value || '').trim()).filter(Boolean) : [];
  if (!values.length) return '';
  return `<article class="case-text verification-points"><span>自动验证预期</span><ol>${values.map(value => `<li>${escapeHtml(value)}</li>`).join('')}</ol></article>`;
}

function commandPhase(title, description, commands = []) {
  const values = Array.isArray(commands) ? commands.filter(Boolean) : [];
  return `<section class="command-phase">
    <header><div><strong>${title}</strong><small>${description}</small></div><span class="chip chip-pending">${values.length} 条</span></header>
    ${values.length
      ? `<ol class="command-list">${values.map(command => `<li><code>${escapeHtml(command)}</code></li>`).join('')}</ol>`
      : '<p class="muted small">本阶段没有命令。</p>'}
  </section>`;
}

const TRACE_PHASE_LABELS = {
  setup: '准备环境',
  action: '执行操作',
  collect: '采集证据',
  final: '最终兜底'
};

const TRACE_SOURCE_LABELS = {
  case: '用例命令',
  runner: 'Runner 补充'
};

function actualCommandTrace(items = []) {
  const values = Array.isArray(items) ? items.filter(item => item && typeof item === 'object') : [];
  if (!values.length) {
    return '<div class="legacy-missing">本次记录没有保存实际执行轨迹。下面的命令映射只能说明计划，不能证明命令已经执行。</div>';
  }
  const phases = [...new Set(values.map(item => item.phase || 'unknown'))];
  return phases.map(phase => {
    const entries = values.filter(item => (item.phase || 'unknown') === phase);
    return `<section class="command-phase command-trace-phase">
      <header><div><strong>${escapeHtml(TRACE_PHASE_LABELS[phase] || phase)}</strong><small>按真实发生顺序保存</small></div><span class="chip chip-pending">${entries.length} 条</span></header>
      <ol class="command-trace-list">${entries.map(item => {
        const source = item.source === 'case' ? 'case' : 'runner';
        const displayed = source === 'case' && item.wire ? item.wire : (item.command || item.wire || '未记录命令');
        const normalized = item.command && item.command !== displayed ? item.command : '';
        const checkpoint = item.checkpoint_index
          ? `检查点 ${item.checkpoint_index}${item.checkpoint_label ? ` · ${item.checkpoint_label}` : ''}`
          : '';
        return `<li class="command-trace-item">
          <div class="trace-meta"><span>#${escapeHtml(item.index ?? '')}</span><span>${escapeHtml(TRACE_SOURCE_LABELS[source])}</span><span>${escapeHtml(item.kind || 'command')}</span><span class="${item.ok === false ? 'trace-fail' : 'trace-pass'}">${escapeHtml(item.status || 'unknown')}</span>${checkpoint ? `<span>${escapeHtml(checkpoint)}</span>` : ''}</div>
          <code>${escapeHtml(displayed)}</code>
          ${normalized ? `<small>实际执行：<code>${escapeHtml(normalized)}</code></small>` : ''}
          ${item.error ? `<small class="trace-fail">${escapeHtml(item.error)}</small>` : ''}
        </li>`;
      }).join('')}</ol>
    </section>`;
  }).join('');
}

function evidenceContractEvidence(record) {
  const contract = record?.evidence_contract;
  if (!contract || typeof contract !== 'object' || Array.isArray(contract)) {
    return '<div class="legacy-missing">旧记录未保存证据合同，不能确认动作、检查点和截图是否完整。</div>';
  }
  const issues = Array.isArray(contract.issues) ? contract.issues : [];
  const complete = contract.complete === true;
  return `<div class="notice ${complete ? '' : 'notice-error'}">
    <strong>${complete ? '证据合同完整' : '证据合同不完整'}</strong>
    <p>检查点截图：${Number(contract.captured_screenshots || 0)} / ${Number(contract.required_screenshots || 0)}；业务动作：${Number(contract.business_action_count || 0)}；actions 实际尝试：${Number(contract.attempted_action_count || 0)} / ${Number(contract.planned_action_count || 0)}</p>
    ${issues.length ? `<ul>${issues.map(item => `<li>${escapeHtml(item?.message || item?.code || '未说明的问题')}</li>`).join('')}</ul>` : ''}
  </div>`;
}

function testHistoryRows(items, project, sheet, caseId, returnTo = '/tests') {
  if (!items?.length) return '<p class="muted">还没有测试历史。完成第一次运行后，结果会出现在这里。</p>';
  return `<div class="history-list">${items.map(item => `
    <a class="history-row" href="${escapeHtml(testHistoryHref(project, sheet, caseId, item.id, returnTo))}">
      ${resultChip(item.verdict)}
      <span><strong>${escapeHtml(friendlyAgentError(item.reason || '未记录判定理由'))}</strong><small>${item.has_screenshot ? `${item.screenshot_count || 1} 张${screenshotLabel(item)}` : '无截图'} · ${escapeHtml(item.execution_target_label || testProject(project).targetLabel)}</small></span>
      <time>${formatTime(item.timestamp)}</time>
      <span class="row-arrow" aria-hidden="true">›</span>
    </a>`).join('')}</div>`;
}

async function renderTest(sheet, caseId) {
  setActiveNav('cases');
  const project = testListParams().project;
  const projectMeta = testProject(project);
  const returnTo = testReturnUrl();
  const testCase = await api(`/api/tests/${encodeURIComponent(sheet)}/${encodeURIComponent(caseId)}?project=${encodeURIComponent(project)}`);
  document.title = `${testCase.case_id} · Agent 测试`;
  const latest = testCase.history?.[0];
  const initialVerdict = latest?.verdict || 'PENDING';
  const usesFixedMapping = Boolean(testCase.is_promoted);
  app.innerHTML = `
    <a class="back-link test-list-back-link" href="${escapeHtml(returnTo)}">← 返回测试用例</a>
    <header class="page-header">
      <div>
        <p class="eyebrow">${escapeHtml(testCase.sheet)} · Agent 测试 · ${escapeHtml(testCase.execution_target_label)}</p>
        <h1 class="page-title detail-title">${escapeHtml(testCase.case_id)}</h1>
        <div class="meta-line">${testTargetChip(testCase)}<span class="chip chip-status">${escapeHtml(testCase.priority || '未分级')}</span>${caseStatusChip({...testCase, latest_verdict: initialVerdict})}<span class="muted small">${testCase.history?.length || 0} 次历史运行</span></div>
      </div>
    </header>
    <div class="detail-grid">
      <div class="stack">
        <section class="panel">
          <header class="panel-head"><div><h2>测试语义</h2><p>人工用例原文与本次自动验证预期</p></div></header>
          <div class="panel-body case-text-grid">
            ${caseText('前置条件', testCase.precondition_text)}
            ${caseText('操作步骤', testCase.steps_text)}
            ${caseText('人工预期原文', testCase.expected_text)}
            ${verificationPoints(testCase.verification_points)}
          </div>
        </section>
        <section class="panel">
          <header class="panel-head"><div><h2>${usesFixedMapping ? '固化步骤' : '动态探索'}</h2><p>${usesFixedMapping ? 'Runner 按准备、操作、采集的固定顺序运行' : '当前没有固化步骤，启动后由 Agent-loop 依据用例原文逐步探索'}</p></div></header>
          <div class="panel-body command-phases">
            ${usesFixedMapping ? `${commandPhase('准备环境', 'setup', testCase.setup)}${commandPhase('执行操作', 'actions', testCase.actions)}${commandPhase('采集证据', 'collect', testCase.collect)}` : '<div class="notice"><strong>本条将临时探索</strong><p>普通运行只写本次历史，不会写入外部探索账本。完成后可由你显式发起候选复跑；只有复跑证据通过审计才会固化到 case_map。</p></div>'}
          </div>
        </section>
        ${testCase.note ? `<section class="panel"><header class="panel-head"><div><h2>补充说明</h2><p>映射字段未表达的简短说明</p></div></header><div class="panel-body prose">${escapeHtml(testCase.note)}</div></section>` : ''}
      </div>
      <aside class="panel repair-panel">
        <header class="panel-head"><div><h2>启动测试</h2><p>使用 ${escapeHtml(testCase.project_label)} ${escapeHtml(testCase.execution_target_label)}执行并由 Agent 判定</p></div></header>
        <form id="test-run-form" class="panel-body">
          <ul class="run-notes">
            <li>${usesFixedMapping ? '逐条执行已固化命令' : 'Agent-loop 按原始前置、步骤和预期逐步探索'}</li>
            <li>每个截图检查点分别采集${escapeHtml(screenshotLabel(testCase))}</li>
            <li>保存判定理由与运行证据</li>
          </ul>
          <button id="test-run-button" class="button button-wide" type="submit">启动测试</button>
          ${!usesFixedMapping && testCase.history?.length ? `<button id="test-promote-button" class="button button-secondary button-wide" type="button" style="margin-top: 8px;">🔍 生成候选、复跑并晋升</button>` : ''}
          ${project === '6202_W5230' ? `<button id="test-migrate-button" class="button button-secondary button-wide" type="button" style="margin-top: 8px;">🔄 从 6202 模拟器迁移</button>` : ''}
          <p class="form-note">同一时间只运行一个测试或修复任务，避免${escapeHtml(testCase.execution_target_label)}链路冲突。</p>
        </form>
      </aside>
    </div>
    <section class="panel" id="test-workflow-panel">
      <header class="panel-head"><div><h2>测试流程</h2><p>运行期间每 2 秒刷新任务状态</p></div><span id="test-job-chip" class="chip chip-pending">等待启动</span></header>
      <div class="panel-body">
        ${testWorkflowSvg()}
        <div class="workflow-status"><div id="test-job-message" class="muted small">启动后将执行命令、完成语义判定并保存证据。</div><div class="legend"><span><i></i>待执行</span><span><i class="blue"></i>执行中</span><span><i class="green"></i>完成</span><span><i class="red"></i>失败</span></div></div>
      </div>
    </section>
    <section class="panel">
      <header class="panel-head"><div><h2>测试历史</h2><p>每次运行的判定、终端数据与截图快照</p></div></header>
      <div id="test-history-body" class="panel-body">${testHistoryRows(testCase.history, project, sheet, caseId, returnTo)}</div>
    </section>`;

  const promoteBtn = document.querySelector('#test-promote-button');
  if (promoteBtn) {
    promoteBtn.addEventListener('click', async () => {
      promoteBtn.disabled = true;
      promoteBtn.textContent = '正在生成候选…';
      try {
        const resp = await fetch('/api/cases/audit-and-promote', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({case_id: caseId, sheet, project}),
        });
        const resData = await resp.json();
        if (!resp.ok) throw new Error(resData.error || resData.message || (resData.audit?.issues || []).join('; ') || '候选生成未通过');
        if (resData.status === 'candidate_replay_started' && resData.job?.id) {
          updateTestWorkflow({});
          const chip = document.querySelector('#test-job-chip');
          chip.className = 'chip chip-running';
          chip.textContent = '候选复跑已创建';
          document.querySelector('#test-job-message').textContent = `任务 ${resData.job.id} 正在用候选映射正式复跑；通过审计后才会写入 PROMOTED。`;
          showToast(`用例 ${caseId} 的候选复跑已启动`);
          pollTestJob(resData.job.id, project, sheet, caseId, true);
          return;
        }
        showToast(`用例 ${caseId} 已是 PROMOTED`);
        window.location.reload();
      } catch (err) {
        showToast(err.message, 'error');
        promoteBtn.disabled = false;
        promoteBtn.textContent = '🔍 生成候选、复跑并晋升';
      }
    });
  }

  const migrateBtn = document.querySelector('#test-migrate-button');
  if (migrateBtn) {
    migrateBtn.addEventListener('click', async () => {
      migrateBtn.disabled = true;
      migrateBtn.textContent = '正在准备迁移…';
      try {
        const resp = await fetch('/api/cases/migrate', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({case_id: caseId, sheet, source_profile: '6202_W5230_SIMULATOR', target_profile: '6202_W5230'}),
        });
        const resData = await resp.json();
        if (!resp.ok) throw new Error(resData.error || resData.message || '迁移启动失败');
        showToast(`用例 ${caseId} 迁移任务已启动！`);
        window.location.reload();
      } catch (err) {
        showToast(err.message, 'error');
        migrateBtn.disabled = false;
        migrateBtn.textContent = '🔄 从 6202 模拟器迁移';
      }
    });
  }

  document.querySelector('#test-run-form').addEventListener('submit', async event => {
      event.preventDefault();
      const button = document.querySelector('#test-run-button');
      button.disabled = true;
      button.textContent = '正在启动…';
      try {
        const job = await api('/api/tests/run', {
          method: 'POST',
          body: JSON.stringify({project, sheet, case_id: caseId})
        });
        updateTestWorkflow({});
        const chip = document.querySelector('#test-job-chip');
        chip.className = 'chip chip-running';
        chip.textContent = '任务已创建';
        document.querySelector('#test-job-message').textContent = `任务 ${job.id} 已创建，正在启动${projectMeta.targetLabel}链路…`;
        showToast(`测试任务 ${job.id} 已启动`);
        pollTestJob(job.id, project, sheet, caseId);
      } catch (error) {
        showToast(error.message, error.status === 409 ? 'warning' : 'error');
        button.disabled = false;
        button.textContent = '启动测试';
      }
  });
  await restoreActiveTest(project, sheet, caseId);
}

function stopTestPolling() {
  clearTimeout(testPollTimer);
  testPollTimer = null;
}

async function restoreActiveTest(project, sheet, caseId) {
  stopTestPolling();
  const button = document.querySelector('#test-run-button');
  const chip = document.querySelector('#test-job-chip');
  const message = document.querySelector('#test-job-message');
  if (!button || !chip || !message) return;
  try {
    const payload = await api('/api/tests/active');
    const job = activeTestJobForProject(payload, project);
    if (!job) {
      button.disabled = false;
      button.textContent = '启动测试';
      return;
    }
    button.disabled = true;
    if (job.type === 'batch') {
      button.textContent = '全量批次运行中';
      chip.className = 'chip chip-warning';
      chip.textContent = '批次占用中';
      message.innerHTML = `全量批次已完成 ${Number(job.completed || 0)} / ${Number(job.total || 0)} 条。<a href="/test-batch/${encodeURIComponent(job.id)}">查看实时进度 →</a>`;
      testPollTimer = setTimeout(() => restoreActiveTest(project, sheet, caseId), 2000);
      return;
    }
    if (job.project === project && job.sheet === sheet && job.case_id === caseId) {
      button.textContent = job.status === 'finalizing' ? '保存记录中…' : '正在测试…';
      updateTestWorkflow(job.nodes);
      chip.className = 'chip chip-running';
      chip.textContent = job.status === 'finalizing' ? '保存记录中' : '任务运行中';
      message.textContent = `已找回任务 ${job.id}，正在读取最新状态…`;
      pollTestJob(job.id, project, sheet, caseId, Boolean(job.promotion_flow));
      return;
    }
    button.textContent = '其他测试运行中';
    chip.className = 'chip chip-warning';
    chip.textContent = '任务占用中';
    message.textContent = `${job.project_label || ''} ${job.case_id} 正在${job.execution_target_label || '测试链路'}测试，结束后才能启动当前用例。`;
    testPollTimer = setTimeout(() => restoreActiveTest(project, sheet, caseId), 2000);
  } catch (error) {
    button.disabled = false;
    button.textContent = '启动测试';
    message.textContent = `活动任务状态读取失败：${error.message}`;
  }
}

async function pollTestJob(jobId, project, sheet, caseId, promotionFlow = false) {
  const chip = document.querySelector('#test-job-chip');
  const message = document.querySelector('#test-job-message');
  const button = document.querySelector('#test-run-button');
  const promoteButton = document.querySelector('#test-promote-button');
  if (!chip || !message || !button) return;
  try {
    const job = await api(`/api/tests/jobs/${encodeURIComponent(jobId)}`);
    const isPromotionFlow = promotionFlow || Boolean(job.promotion_flow);
    updateTestWorkflow(job.nodes);
    if (['queued', 'running', 'finalizing'].includes(job.status)) {
      button.disabled = true;
      button.textContent = job.status === 'finalizing' ? '保存记录中…' : '正在测试…';
      if (promoteButton && isPromotionFlow) {
        promoteButton.disabled = true;
        promoteButton.textContent = job.status === 'finalizing' ? '正在审计候选…' : '正在正式复跑候选…';
      }
      chip.className = 'chip chip-running';
      chip.textContent = job.status === 'finalizing'
        ? (isPromotionFlow ? '正在审计候选' : '保存记录中')
        : (TEST_NODE_LABELS[job.current_node] || '任务运行中');
      message.textContent = job.status === 'finalizing'
        ? `任务 ${job.id} 已结束，正在保存历史并审计候选映射…`
        : `任务 ${job.id} · ${TEST_NODE_LABELS[job.current_node] || '正在执行'}`;
      testPollTimer = setTimeout(() => pollTestJob(jobId, project, sheet, caseId, isPromotionFlow), 2000);
      return;
    }
    setResultChip(chip, job.verdict);
    const evidenceLink = job.history_id
      ? `<a href="${escapeHtml(testHistoryHref(project, sheet, caseId, job.history_id, testReturnUrl()))}">查看本次测试证据 →</a>`
      : '';
    if (isPromotionFlow) {
      const promotionIssues = Array.isArray(job.promotion_issues) ? job.promotion_issues.filter(Boolean) : [];
      if (job.promotion_status === 'promoted') {
        message.innerHTML = `候选复跑及证据审计完成，映射已晋升为 PROMOTED。${evidenceLink}`;
        showToast(`🎉 用例 ${caseId} 的候选复跑达标，已晋升为 PROMOTED！`);
        window.location.reload();
        return;
      }
      message.innerHTML = `候选未晋升，临时候选${job.promotion_status === 'rolled_back' ? '已自动回滚' : '需要人工检查'}：${escapeHtml(promotionIssues.join('；') || '证据门禁未通过')} ${evidenceLink}`;
      showToast(job.promotion_status === 'rolled_back' ? '候选复跑未达晋升门禁，已自动回滚' : '候选回滚存在冲突，请查看详情', job.promotion_status === 'rolled_back' ? 'warning' : 'error');
      if (promoteButton) {
        promoteButton.disabled = false;
        promoteButton.textContent = '🔍 生成候选、复跑并晋升';
      }
    } else {
      message.innerHTML = job.history_id
        ? `任务 ${escapeHtml(job.id)} 已结束。${evidenceLink}`
        : `任务结束，但测试记录保存失败：${escapeHtml(friendlyAgentError(job.error || '未知原因'))}`;
    }
    button.disabled = false;
    button.textContent = '再次启动测试';
    const detail = await api(`/api/tests/${encodeURIComponent(sheet)}/${encodeURIComponent(caseId)}?project=${encodeURIComponent(project)}`);
    document.querySelector('#test-history-body').innerHTML = testHistoryRows(detail.history, project, sheet, caseId, testReturnUrl());
    if (!isPromotionFlow) {
      if (job.verdict === 'PASS') showToast('Agent 测试已通过');
      else if (job.verdict === 'CANNOT_VERIFY') showToast('测试无法验证，请查看判定理由和证据', 'warning');
      else showToast(job.verdict === 'ERROR' ? '测试执行出错' : 'Agent 测试未通过', 'error');
    }
  } catch (error) {
    chip.className = 'chip chip-fail';
    chip.textContent = '状态读取失败';
    message.textContent = error.message;
    button.disabled = false;
    button.textContent = '重新启动';
  }
}

function stopBatchTestPolling() {
  clearTimeout(batchTestPollTimer);
  batchTestPollTimer = null;
}

function batchRecentRows(items = [], project = DEFAULT_TEST_PROJECT) {
  if (!items.length) return '<p class="muted">首条用例完成后，这里会显示最新判定。</p>';
  const returnTo = buildTestListUrl('', 1, DEFAULT_TEST_STATE, project);
  return `<div class="batch-result-list">${items.map(item => {
    const content = `${resultChip(item.verdict)}<span><strong>${escapeHtml(item.case_id)}</strong><small>${escapeHtml(item.sheet)} · ${escapeHtml(friendlyAgentError(item.reason || '未记录判定理由'))}</small></span><time>${formatTime(item.finished_at)}</time>`;
    return item.history_id
      ? `<a class="batch-result-row" href="${escapeHtml(testHistoryHref(project, item.sheet, item.case_id, item.history_id, returnTo))}">${content}<span class="row-arrow" aria-hidden="true">›</span></a>`
      : `<div class="batch-result-row">${content}<span></span></div>`;
  }).join('')}</div>`;
}

function updateBatchScreenshots(items = [], project = DEFAULT_TEST_PROJECT) {
  const gallery = document.querySelector('#batch-live-screenshots');
  const count = document.querySelector('#batch-screenshot-count');
  if (!gallery || !count) return;
  const values = Array.isArray(items) ? items : [];
  const signature = values.map(item => `${item.url}:${item.label}`).join('|');
  count.textContent = `${values.length} 张`;
  if (gallery.dataset.signature === signature) return;
  gallery.dataset.signature = signature;
  gallery.innerHTML = values.length ? values.map(item => `
    <figure class="evidence-frame">
      <header>检查点 ${Number(item.index || 0)} · ${escapeHtml(item.label || '')}</header>
      <a href="${escapeHtml(item.url)}" target="_blank" rel="noopener"><img src="${escapeHtml(item.url)}" alt="${escapeHtml(item.label || screenshotLabel(project))}"></a>
    </figure>`).join('') : '<div class="evidence-missing">当前用例尚未生成截图</div>';
}

function updateBatchView(job) {
  const project = testProject(job.project).project;
  const completed = Number(job.completed || 0);
  const total = Number(job.total || 0);
  const percent = total ? Math.min(100, Math.round(completed * 1000 / total) / 10) : 0;
  const counts = job.verdict_counts || {};
  const active = ['queued', 'running', 'finalizing'].includes(job.status);
  const statusChip = document.querySelector('#batch-status-chip');
  if (statusChip) {
    if (active) {
      statusChip.className = 'chip chip-running';
      statusChip.textContent = job.cancel_requested ? '等待当前用例结束' : '运行中';
    } else if (job.status === 'completed') {
      statusChip.className = 'chip chip-pass';
      statusChip.textContent = '批次完成';
    } else {
      statusChip.className = 'chip chip-warning';
      statusChip.textContent = job.status === 'cancelled' ? '已暂停' : job.status === 'interrupted' ? '运行中断' : '批次异常';
    }
  }
  const value = document.querySelector('#batch-progress-value');
  const bar = document.querySelector('#batch-progress-bar');
  if (value) value.textContent = `${completed} / ${total} · ${percent}%`;
  if (bar) bar.style.width = `${percent}%`;
  for (const [key, id] of [['PASS', 'batch-pass'], ['FAIL', 'batch-fail'], ['ERROR', 'batch-error'], ['CANNOT_VERIFY', 'batch-cannot']]) {
    const element = document.querySelector(`#${id}`);
    if (element) element.textContent = Number(counts[key] || 0);
  }

  const current = document.querySelector('#batch-current-case');
  if (current) {
    const item = job.current_case;
    current.innerHTML = item ? `
      <div class="batch-case-head"><div><span>当前用例 ${Number(job.current_index || 0)} / ${total}</span><strong>${escapeHtml(item.case_id)}</strong><small>${escapeHtml(item.sheet)} · ${escapeHtml(item.priority || '未分级')}</small></div>${resultChip('RUNNING')}</div>
      <div class="batch-case-grid">
        <div><span>当前阶段</span><strong>${escapeHtml(TEST_NODE_LABELS[job.current_node] || job.current_node || '启动中')}</strong></div>
        <div><span>命令数量</span><strong>${Number(item.setup_count || 0)} + ${Number(item.action_count || 0)} + ${Number(item.collect_count || 0)}</strong></div>
      </div>
      <p>${escapeHtml(item.expected_text || '未填写预期结果')}</p>`
      : `<div class="notice">${job.status === 'completed' ? '全部用例已执行完成。' : job.resume_available ? `${escapeHtml(job.interruption_reason || '批次已中断')}，可从第 ${completed + 1} 条继续。` : '正在准备下一条用例…'}</div>`;
  }
  const target = document.querySelector('#batch-target');
  if (target) target.innerHTML = testTargetChip(job);
  updateBatchScreenshots(job.live_screenshots, project);
  const recent = document.querySelector('#batch-recent-results');
  if (recent) recent.innerHTML = batchRecentRows(job.recent_results, project);
  const cancel = document.querySelector('#batch-cancel-button');
  if (cancel) {
    cancel.disabled = !active || Boolean(job.cancel_requested);
    cancel.textContent = job.cancel_requested ? '当前用例结束后暂停' : '暂停批次';
  }
  const resume = document.querySelector('#batch-resume-button');
  if (resume) {
    resume.hidden = !job.resume_available;
    resume.disabled = !job.resume_available;
    resume.textContent = `继续运行剩余 ${Math.max(total - completed, 0)} 条`;
  }
  const timing = document.querySelector('#batch-timing');
  if (timing) timing.textContent = `开始：${formatTime(job.started_at)} · 结束：${formatTime(job.finished_at)} · 已完成 ${completed}，剩余 ${Math.max(total - completed, 0)}`;
  return active;
}

async function pollBatchTest(jobId) {
  stopBatchTestPolling();
  try {
    const job = await api(`/api/tests/jobs/${encodeURIComponent(jobId)}`);
    const active = updateBatchView(job);
    if (active) batchTestPollTimer = setTimeout(() => pollBatchTest(jobId), 2000);
  } catch (error) {
    const current = document.querySelector('#batch-current-case');
    if (current) current.innerHTML = `<div class="notice notice-error">批次状态读取失败：${escapeHtml(error.message)}</div>`;
  }
}

async function renderTestBatch(jobId) {
  setActiveNav('cases');
  stopBatchTestPolling();
  const initialJob = await api(`/api/tests/jobs/${encodeURIComponent(jobId)}`);
  const projectMeta = testProject(initialJob.project);
  const returnTo = buildTestListUrl('', 1, DEFAULT_TEST_STATE, projectMeta.project);
  document.title = `批次测试 ${jobId} · Agent 测试`;
  app.innerHTML = `
    <a class="back-link" href="${escapeHtml(returnTo)}">← 返回测试用例</a>
    <header class="page-header">
      <div>
        <p class="eyebrow">${escapeHtml(projectMeta.projectLabel)} · ${escapeHtml(projectMeta.targetLabel)}批次测试</p>
        <h1 class="page-title">批次运行</h1>
        <div class="meta-line"><span id="batch-target">${testTargetChip(projectMeta)}</span><span id="batch-status-chip" class="chip chip-running">读取状态</span><span id="batch-timing" class="muted small"></span></div>
      </div>
      <div class="batch-actions">
        <button id="batch-resume-button" class="button" type="button" hidden>继续运行</button>
        <button id="batch-cancel-button" class="button button-danger" type="button">暂停批次</button>
      </div>
    </header>
    <section class="panel">
      <header class="panel-head"><div><h2>总进度</h2><p>每条用例独立启动${escapeHtml(projectMeta.targetLabel)}链路，完成后立即归档历史和截图</p></div><strong id="batch-progress-value">0 / 0</strong></header>
      <div class="panel-body">
        <div class="batch-progress-track"><div id="batch-progress-bar" class="batch-progress-bar"></div></div>
        <div class="batch-summary-grid">
          <div><span>PASS</span><strong id="batch-pass">0</strong></div>
          <div><span>FAIL</span><strong id="batch-fail">0</strong></div>
          <div><span>ERROR</span><strong id="batch-error">0</strong></div>
          <div><span>无法验证</span><strong id="batch-cannot">0</strong></div>
        </div>
      </div>
    </section>
    <section class="panel">
      <header class="panel-head"><div><h2>当前运行信息</h2><p>用例、阶段、命令数量和人工预期</p></div></header>
      <div id="batch-current-case" class="panel-body"><div class="list-loading">正在读取当前用例…</div></div>
    </section>
    <section class="panel">
      <header class="panel-head"><div><h2>当前${escapeHtml(screenshotLabel(projectMeta))}</h2><p>验证点截图生成后自动出现；LLM 只依据这些截图判定</p></div><span id="batch-screenshot-count" class="chip chip-pending">0 张</span></header>
      <div id="batch-live-screenshots" class="panel-body checkpoint-grid"></div>
    </section>
    <section class="panel">
      <header class="panel-head"><div><h2>最新测试结果</h2><p>保留最近 30 条；点击可查看操作信息和全部判定截图</p></div></header>
      <div id="batch-recent-results" class="panel-body"><p class="muted">正在等待首条结果…</p></div>
    </section>`;
  document.querySelector('#batch-cancel-button').addEventListener('click', async () => {
    if (!window.confirm('暂停不会中断当前用例；当前用例完成并保存证据后，批次才会暂停。之后可继续剩余用例。确定继续吗？')) return;
    const button = document.querySelector('#batch-cancel-button');
    button.disabled = true;
    try {
      const job = await api(`/api/tests/jobs/${encodeURIComponent(jobId)}/cancel`, {method: 'POST', body: '{}'});
      updateBatchView(job);
      showToast('已请求暂停，等待当前用例完成', 'warning');
    } catch (error) {
      button.disabled = false;
      showToast(error.message, 'error');
    }
  });
  document.querySelector('#batch-resume-button').addEventListener('click', async () => {
    const button = document.querySelector('#batch-resume-button');
    button.disabled = true;
    try {
      const job = await api(`/api/tests/jobs/${encodeURIComponent(jobId)}/resume`, {method: 'POST', body: '{}'});
      updateBatchView(job);
      showToast('已从断点继续运行', 'success');
      await pollBatchTest(jobId);
    } catch (error) {
      button.disabled = false;
      showToast(error.message, 'error');
    }
  });
  const active = updateBatchView(initialJob);
  if (active) batchTestPollTimer = setTimeout(() => pollBatchTest(jobId), 2000);
}

function sourceCards(matches) {
  if (!matches?.length) return '<p class="muted">未定位到相关源码，修复助手启动后会返回明确原因。</p>';
  return `<div class="source-list">${matches.map(match => `
    <article class="source-item">
      <div class="source-meta"><strong>${escapeHtml(match.path)}</strong><span>L${escapeHtml(match.line_start)}–L${escapeHtml(match.line_end)}</span></div>
      <pre><code>${escapeHtml(match.snippet || '')}</code></pre>
    </article>`).join('')}</div>`;
}

function attachmentCards(attachments) {
  if (!attachments?.length) return '<p class="muted">没有附件分析结果。</p>';
  return `<div class="attachment-list">${attachments.map(item => `
    <article class="attachment-card">
      <strong>${escapeHtml(item.name)} <span class="chip chip-pending">${escapeHtml(item.kind || '附件')}</span></strong>
      <p>${escapeHtml(item.summary || '无摘要')}</p>
    </article>`).join('')}</div>`;
}

function descriptionImage(item) {
  const alt = item.summary || item.name || '缺陷原图';
  return `<figure class="defect-description-image">
    <a href="${escapeHtml(item.url)}" target="_blank" rel="noopener">
      <img src="${escapeHtml(item.url)}" alt="${escapeHtml(alt)}" onerror="this.closest('figure').innerHTML='<div class=\'evidence-missing\'>缺陷图片读取失败</div>'">
    </a>
    <figcaption>${escapeHtml(item.name || '缺陷原图')}</figcaption>
  </figure>`;
}

function defectDescription(defect) {
  const description = String(defect.description || '').trim();
  if (!description) return '<p class="muted">没有补充描述。</p>';
  if (!description.includes('[image]')) return escapeHtml(description);
  const images = (defect.attachments || []).filter(item => item?.kind === 'image' && item?.url);
  let imageIndex = 0;
  return description.split(/(\[image\])/g).map(part => {
    if (part !== '[image]') return escapeHtml(part);
    const image = images[imageIndex++];
    return image
      ? descriptionImage(image)
      : '<div class="evidence-missing">缺陷图片未找到</div>';
  }).join('');
}

function missingRecordText(record, field) {
  return record?.legacy_record ? `旧记录未保存${field}` : `本次记录保存异常：缺少${field}`;
}

function testMode(record) {
  if (record?.execution_mode === 'agent_generated') return '修复助手自主生成';
  return record?.test_case || missingRecordText(record, '测试方式');
}

function historyRows(historyItems, number) {
  if (!historyItems?.length) return '<p class="muted">还没有修复历史。完成第一次修复后，结果会出现在这里。</p>';
  return `<div class="history-list">${historyItems.map(item => `
    <a class="history-row" href="/history/${encodeURIComponent(number)}/${encodeURIComponent(item.id)}">
      ${resultChip(item.verdict)}
      <span><strong>${escapeHtml(testMode(item))}</strong><small>${escapeHtml(item.source_file || '实际修改文件未记录')}</small></span>
      <time>${formatTime(item.timestamp)}</time>
      <span class="row-arrow" aria-hidden="true">›</span>
    </a>`).join('')}</div>`;
}

async function renderDefect(number) {
  setActiveNav('defects');
  const defect = await api(`/api/defects/${encodeURIComponent(number)}`);
  document.title = `缺陷 #${defect.number} · Agent自动化测试平台`;
  const sourceMatches = defect.source_analysis?.matches || [];
  const sourcePanelOpen = sourceMatches.length < 3 ? ' open' : '';
  app.innerHTML = `
    <a class="back-link" href="/">← 返回缺陷队列</a>
    <header class="page-header">
      <div>
        <p class="eyebrow">缺陷 #${escapeHtml(defect.number)}</p>
        <h1 class="page-title detail-title">${escapeHtml(defect.title)}</h1>
        <div class="meta-line"><span class="chip chip-status">${escapeHtml(defect.status || '未知')}</span><span class="muted small">${defect.history.length} 次历史运行</span></div>
      </div>
    </header>
    <div class="detail-grid">
      <div class="stack">
        <section class="panel">
          <header class="panel-head"><div><h2>问题描述</h2><p>来自本地缺陷数据</p></div></header>
          <div class="panel-body"><div class="prose defect-description">${defectDescription(defect)}</div></div>
        </section>
        <section class="panel">
          <header class="panel-head"><div><h2>附件分析</h2><p>图片、视频与日志摘要</p></div><span class="chip chip-pending">${defect.attachments?.length || 0} 项</span></header>
          <div class="panel-body">${attachmentCards(defect.attachments)}</div>
        </section>
        <details class="panel collapsible-panel"${sourcePanelOpen}>
          <summary class="panel-head">
            <div><h2>源码定位</h2><p>只读参考；修复助手会自主判断实际修改位置</p></div>
            <span class="collapse-controls"><span class="chip chip-pending">${sourceMatches.length} 处</span><span class="collapse-action" aria-hidden="true"></span></span>
          </summary>
          <div class="panel-body">${sourceCards(sourceMatches)}</div>
        </details>
      </div>
      <aside class="panel repair-panel">
        <header class="panel-head"><div><h2>启动修复</h2><p>修复助手自主定位、生成命令并验证</p></div></header>
        <form id="run-form" class="panel-body">
          <ul class="run-notes">
            <li>自动分析候选源码</li>
            <li>自动生成模拟器测试命令</li>
            <li>保存代码改动、判定与证据</li>
          </ul>
          <button id="run-button" class="button button-wide" type="submit">启动修复</button>
          <p id="run-note" class="form-note">同一时间只运行一个任务，避免真实源码冲突。</p>
        </form>
      </aside>
    </div>
    <section class="panel" id="workflow-panel">
      <header class="panel-head"><div><h2>修复流程</h2><p>运行期间每 2 秒刷新节点状态</p></div><span id="job-chip" class="chip chip-pending">等待启动</span></header>
      <div class="panel-body">
        ${workflowSvg()}
        <div class="workflow-status"><div id="job-message" class="muted small">启动后将自动生成方案并完成验证。</div><div class="legend"><span><i></i>待执行</span><span><i class="blue"></i>执行中</span><span><i class="green"></i>完成</span><span><i class="red"></i>失败</span></div></div>
      </div>
    </section>
    <section class="panel">
      <header class="panel-head"><div><h2>修复历史</h2><p>每次运行的代码改动、测试输出与截图快照</p></div></header>
      <div id="history-body" class="panel-body">${historyRows(defect.history, defect.number)}</div>
    </section>`;

  document.querySelector('#run-form').addEventListener('submit', async event => {
    event.preventDefault();
    const button = document.querySelector('#run-button');
    button.disabled = true;
    button.textContent = '正在启动…';
    try {
      const job = await api('/api/run', {
        method: 'POST',
        body: JSON.stringify({ defect: String(defect.number) })
      });
      updateWorkflow({});
      const chip = document.querySelector('#job-chip');
      const message = document.querySelector('#job-message');
      chip.className = 'chip chip-running';
      chip.textContent = '任务已创建';
      message.textContent = `任务 ${job.id} 已创建，正在读取节点状态…`;
      showToast(`修复任务 ${job.id} 已启动`);
      pollJob(job.id, String(defect.number));
    } catch (error) {
      showToast(error.message, 'error');
      button.disabled = false;
      button.textContent = '启动修复';
    }
  });
  await restoreActiveRepair(String(defect.number));
}

function stopRepairRestore() {
  clearTimeout(repairRestoreTimer);
  repairRestoreTimer = null;
}

async function restoreActiveRepair(defectNumber) {
  stopRepairRestore();
  const button = document.querySelector('#run-button');
  const chip = document.querySelector('#job-chip');
  const message = document.querySelector('#job-message');
  if (!button || !chip || !message) return;
  try {
    const payload = await api('/api/run/active');
    const job = payload.job;
    if (!job) {
      button.disabled = false;
      button.textContent = '启动修复';
      return;
    }
    button.disabled = true;
    if (String(job.defect) === String(defectNumber)) {
      button.textContent = job.status === 'finalizing' ? '保存记录中…' : '正在修复…';
      updateWorkflow(job.nodes);
      chip.className = 'chip chip-running';
      chip.textContent = job.status === 'finalizing' ? '保存记录中' : '任务运行中';
      message.textContent = `已找回任务 ${job.id}，正在读取最新状态…`;
      pollJob(job.id, defectNumber);
      return;
    }
    button.textContent = '其他任务运行中';
    chip.className = 'chip chip-warning';
    chip.textContent = '任务占用中';
    message.textContent = `缺陷 #${job.defect} 正在修复，结束后才能启动当前缺陷。`;
    repairRestoreTimer = setTimeout(() => restoreActiveRepair(defectNumber), 2000);
  } catch (error) {
    button.disabled = false;
    button.textContent = '启动修复';
    message.textContent = `活动任务状态读取失败：${error.message}`;
  }
}

async function pollJob(jobId, defectNumber) {
  const chip = document.querySelector('#job-chip');
  const message = document.querySelector('#job-message');
  const button = document.querySelector('#run-button');
  if (!chip || !message || !button) return;
  try {
    const job = await api(`/api/run/${encodeURIComponent(jobId)}`);
    updateWorkflow(job.nodes);
    if (job.status === 'queued' || job.status === 'running' || job.status === 'finalizing') {
      button.disabled = true;
      button.textContent = job.status === 'finalizing' ? '保存记录中…' : '正在修复…';
      chip.className = 'chip chip-running';
      chip.textContent = job.status === 'finalizing'
        ? '保存记录中'
        : job.current_node ? `${NODE_LABELS[job.current_node] || job.current_node}中` : '任务运行中';
      message.textContent = job.status === 'finalizing'
        ? `任务 ${job.id} 已结束，正在保存修复历史…`
        : `任务 ${job.id} · 第 ${Math.max(1, Number(job.attempts || 0))} 轮${job.progress_updated_at ? ` · 更新于 ${formatTime(job.progress_updated_at)}` : ''}`;
      setTimeout(() => pollJob(jobId, defectNumber), 2000);
      return;
    }
    setResultChip(chip, job.verdict);
    message.innerHTML = job.history_id
      ? `任务 ${escapeHtml(job.id)} 已结束。<a href="/history/${encodeURIComponent(defectNumber)}/${encodeURIComponent(job.history_id)}">查看本次修复证据 →</a>`
      : `任务结束，但历史记录保存失败：${escapeHtml(job.error || '未知原因')}`;
    button.disabled = false;
    button.textContent = '再次启动修复';
    const historyPayload = await api(`/api/history/${encodeURIComponent(defectNumber)}`);
    document.querySelector('#history-body').innerHTML = historyRows(historyPayload.history, defectNumber);
    if (job.verdict === 'PASS') showToast('修复与验证均已通过');
    else if (job.verdict === 'CANNOT_VERIFY') showToast('无法验证，请查看测试命令和证据', 'warning');
    else showToast('修复未通过，请查看失败原因和证据', 'error');
  } catch (error) {
    chip.className = 'chip chip-fail';
    chip.textContent = '状态读取失败';
    message.textContent = error.message;
    button.disabled = false;
    button.textContent = '重新启动';
  }
}

function evidenceFrame(label, url, meta = '') {
  const safeLabel = escapeHtml(label || '模拟器截图');
  return `<article class="evidence-frame"><header><span>${safeLabel}</span>${meta ? `<small>${escapeHtml(meta)}</small>` : ''}</header>${url
    ? `<img src="${escapeHtml(url)}" alt="${safeLabel}" onerror="this.outerHTML='<div class=\'evidence-missing\'>截图读取失败</div>'">`
    : '<div class="evidence-missing">本次运行没有生成截图</div>'}</article>`;
}

function testScreenshotGallery(record) {
  let items = Array.isArray(record.screenshot_urls)
    ? record.screenshot_urls.filter(item => item && item.url)
    : [];
  if (!items.length && record.screenshot_url) {
    items = [{label: '最终画面', url: record.screenshot_url}];
  }
  if (!items.length) return evidenceFrame(screenshotLabel(record), '');
  return items.map((item, index) => {
    const phase = TRACE_PHASE_LABELS[item.phase] || item.phase || '未记录阶段';
    const command = item.command ? ` · ${item.command}` : '';
    return evidenceFrame(item.label || `检查点 ${index + 1}`, item.url, `${phase}${command}`);
  }).join('');
}

function recordCommands(record) {
  if (!Array.isArray(record.test_commands)) return [];
  return [...new Set(record.test_commands.map(value => String(value || '').trim()).filter(Boolean))];
}

function recordThinkingSteps(record) {
  if (!Array.isArray(record.llm_thinking_steps)) return [];
  return record.llm_thinking_steps.map(value => String(value || '').trim()).filter(Boolean);
}

function recordedPatch(record) {
  if (record.patch?.file_path) return record.patch;
  if (!Array.isArray(record.rounds)) return record.patch || {};
  for (let index = record.rounds.length - 1; index >= 0; index -= 1) {
    if (record.rounds[index]?.patch?.file_path) return record.rounds[index].patch;
  }
  return record.patch || {};
}

function commandEvidence(record, commands) {
  if (!commands.length) return `<div class="legacy-missing">${escapeHtml(missingRecordText(record, '测试命令'))}</div>`;
  return `<ol class="command-list">${commands.map(command => `<li><code>${escapeHtml(command)}</code></li>`).join('')}</ol>`;
}

function thinkingEvidence(record, thinkingSteps) {
  if (!thinkingSteps.length) return `<div class="legacy-missing">${escapeHtml(missingRecordText(record, 'LLM 思考步骤'))}</div>`;
  return `<ol class="command-list thinking-list">${thinkingSteps.map(step => `<li><code>${escapeHtml(step)}</code></li>`).join('')}</ol>`;
}

function verdictReasons(record) {
  const reasons = Array.isArray(record.verdict_reasons)
    ? [...new Set(record.verdict_reasons.map(value => String(value || '').trim()).filter(Boolean))]
    : [];
  return reasons.length
    ? reasons.map(reason => `<p>${escapeHtml(friendlyAgentError(reason))}</p>`).join('')
    : `<p>${escapeHtml(missingRecordText(record, '判定理由'))}</p>`;
}

function codeRetention(record) {
  if (record.reproduction_outcome === 'CURRENT_CONFORMS' || Number(record.attempts || 0) === 0) return '未修改';
  if (record.patch_retained === true) return '是';
  if (record.patch_retained === false) return '否，已回滚';
  return missingRecordText(record, '代码保留状态');
}

function codeChangeEvidence(record, patch) {
  if (record.reproduction_outcome === 'CURRENT_CONFORMS') {
    return '<div class="notice">当前版本符合预期，本次无需修改代码。</div>';
  }
  return `
    <div class="notice">${escapeHtml(patch.reason || missingRecordText(record, '修改理由'))}</div>
    <div class="patch-grid">
      <div class="patch-box"><strong>修改前</strong><pre class="code-block"><code>${escapeHtml(patch.before || missingRecordText(record, '修改前代码'))}</code></pre></div>
      <div class="patch-box"><strong>修改后</strong><pre class="code-block"><code>${escapeHtml(patch.after || missingRecordText(record, '修改后代码'))}</code></pre></div>
    </div>`;
}

async function renderHistory(number, runId) {
  setActiveNav('defects');
  const record = await api(`/api/history/${encodeURIComponent(number)}/${encodeURIComponent(runId)}`);
  document.title = `修复记录 · 缺陷 #${number} · Agent自动化测试平台`;
  const patch = recordedPatch(record);
  const commands = recordCommands(record);
  const thinkingSteps = recordThinkingSteps(record);
  const actualFile = patch.file_path || record.source_file || (record.reproduction_outcome === 'CURRENT_CONFORMS'
    ? '未修改代码'
    : missingRecordText(record, '实际修改文件'));
  app.innerHTML = `
    <a class="back-link" href="/defect/${encodeURIComponent(number)}">← 返回缺陷详情</a>
    <header class="page-header">
      <div>
        <p class="eyebrow">缺陷 #${escapeHtml(number)} · 修复证据</p>
        <h1 class="page-title">修复记录</h1>
        <div class="meta-line">${resultChip(record.verdict)}<span class="muted small">${formatTime(record.timestamp)}</span></div>
      </div>
      <button id="delete-run" class="button button-danger" type="button">删除本次记录</button>
    </header>
    <section class="panel">
      <header class="panel-head"><div><h2>运行摘要</h2><p>修复输入与最终判定</p></div></header>
      <div class="panel-body"><div class="kv-grid">
        <div class="kv"><span>测试方式</span><strong>${escapeHtml(testMode(record))}</strong></div>
        <div class="kv"><span>实际修改文件</span><strong title="${escapeHtml(actualFile)}">${escapeHtml(actualFile)}</strong></div>
        <div class="kv"><span>尝试轮数</span><strong>${escapeHtml(record.attempts ?? missingRecordText(record, '尝试轮数'))}</strong></div>
        <div class="kv"><span>代码是否保留</span><strong>${escapeHtml(codeRetention(record))}</strong></div>
        <div class="kv"><span>开始时间</span><strong>${formatTime(record.started_at)}</strong></div>
        <div class="kv"><span>结束时间</span><strong>${formatTime(record.finished_at)}</strong></div>
      </div>${record.error ? `<div class="notice notice-error">${escapeHtml(record.error)}</div>` : ''}</div>
    </section>
    <section class="panel">
      <header class="panel-head"><div><h2>修复流程</h2><p>七个节点的最终状态</p></div></header>
      <div class="panel-body">${workflowSvg()}</div>
    </section>
    <section class="panel">
      <header class="panel-head"><div><h2>判定理由</h2><p>来自本次模拟器验证结果</p></div></header>
      <div class="panel-body verdict-reasons">${verdictReasons(record)}</div>
    </section>
    <section class="panel">
      <header class="panel-head"><div><h2>实际测试命令</h2><p>由修复助手生成并执行</p></div><span class="chip chip-pending">${commands.length} 条</span></header>
      <div class="panel-body">${commandEvidence(record, commands)}</div>
    </section>
    <section class="panel">
      <header class="panel-head"><div><h2>截图对比</h2><p>修复前与修复后的模拟器画面；缺失时如实显示</p></div></header>
      <div class="panel-body"><div class="evidence-grid">${evidenceFrame('修复前', record.evidence?.before)}${evidenceFrame('修复后', record.evidence?.after)}</div></div>
    </section>
    <section class="panel">
      <header class="panel-head"><div><h2>代码改动</h2><p>${escapeHtml(actualFile)}</p></div></header>
      <div class="panel-body">${codeChangeEvidence(record, patch)}</div>
    </section>
    <section class="panel">
      <header class="panel-head"><div><h2>LLM 思考步骤</h2><p>复现 Agent 每轮输出的决策理由</p></div><span class="chip chip-pending">${thinkingSteps.length} 条</span></header>
      <div class="panel-body">${thinkingEvidence(record, thinkingSteps)}</div>
    </section>`;
  updateWorkflow(record.nodes);
  document.querySelector('#delete-run').addEventListener('click', async () => {
    if (!window.confirm('确定删除这次修复记录和截图快照吗？此操作不可撤销。')) return;
    const button = document.querySelector('#delete-run');
    button.disabled = true;
    try {
      await api(`/api/history/${encodeURIComponent(number)}/${encodeURIComponent(runId)}`, { method: 'DELETE' });
      window.location.href = `/defect/${encodeURIComponent(number)}`;
    } catch (error) {
      button.disabled = false;
      showToast(error.message, 'error');
    }
  });
}

function testErrorEvidence(record) {
  const groups = [
    ['准备阶段', record.setup_errors],
    ['操作阶段', record.action_errors],
    ['采集阶段', record.collect_errors]
  ].filter(([, values]) => Array.isArray(values) && values.length);
  if (!groups.length) return '<div class="notice">命令执行阶段没有记录错误。</div>';
  return groups.map(([label, values]) => `
    <div class="notice notice-error"><strong>${label}</strong>${values.map(value => `<p>${escapeHtml(friendlyAgentError(value))}</p>`).join('')}</div>`).join('');
}

async function renderTestHistory(sheet, caseId, runId) {
  setActiveNav('cases');
  const project = testListParams().project;
  const returnTo = testReturnUrl();
  const reportReturnTo = testReportReturnUrl();
  const backHref = reportReturnTo || testDetailHref(project, sheet, caseId, returnTo);
  const backLabel = reportReturnTo ? '← 返回测试报告' : '← 返回测试详情';
  const record = await api(`/api/test-history/${encodeURIComponent(sheet)}/${encodeURIComponent(caseId)}/${encodeURIComponent(runId)}?project=${encodeURIComponent(project)}`);
  document.title = `测试记录 · ${caseId} · Agent 测试`;
  app.innerHTML = `
    <a class="back-link" href="${escapeHtml(backHref)}">${backLabel}</a>
    <header class="page-header">
      <div>
        <p class="eyebrow">${escapeHtml(record.sheet)} · ${escapeHtml(record.case_id)} · ${escapeHtml(record.execution_target_label)}</p>
        <h1 class="page-title">测试记录</h1>
        <div class="meta-line">${testTargetChip(record)}${resultChip(record.verdict)}<span class="muted small">${formatTime(record.timestamp)}</span></div>
      </div>
      <button id="delete-test-run" class="button button-danger" type="button">删除本次记录</button>
    </header>
    <section class="panel">
      <header class="panel-head"><div><h2>运行摘要</h2><p>用例、耗时和最终判定</p></div></header>
      <div class="panel-body">
        <div class="kv-grid">
          <div class="kv"><span>测试项目</span><strong>${escapeHtml(record.project_label)}</strong></div>
          <div class="kv"><span>执行目标</span><strong>${escapeHtml(record.execution_target_label)}</strong></div>
          <div class="kv"><span>测试模块</span><strong>${escapeHtml(record.sheet)}</strong></div>
          <div class="kv"><span>用例编号</span><strong>${escapeHtml(record.case_id)}</strong></div>
          <div class="kv"><span>执行方式</span><strong>${escapeHtml(testExecutionModeLabel(record.execution_mode))}</strong></div>
          <div class="kv"><span>优先级</span><strong>${escapeHtml(record.priority || '未分级')}</strong></div>
          <div class="kv"><span>进程返回码</span><strong>${escapeHtml(record.return_code ?? '未记录')}</strong></div>
          <div class="kv"><span>开始时间</span><strong>${formatTime(record.started_at)}</strong></div>
          <div class="kv"><span>结束时间</span><strong>${formatTime(record.finished_at)}</strong></div>
        </div>
      </div>
    </section>
    <section class="panel">
      <header class="panel-head"><div><h2>判定理由</h2><p>只对照预期结果和截图给出的产品结论</p></div></header>
      <div class="panel-body verdict-reasons"><p>${escapeHtml(friendlyAgentError(record.reason || '未记录判定理由'))}</p></div>
    </section>
    <section class="panel">
      <header class="panel-head"><div><h2>证据门禁</h2><p>先确认动作、检查点和截图完整，再允许产品判定</p></div></header>
      <div class="panel-body">${evidenceContractEvidence(record)}</div>
    </section>
    <section class="panel">
      <header class="panel-head"><div><h2>测试语义</h2><p>本次运行时保存的人工原文与自动验证预期</p></div></header>
      <div class="panel-body case-text-grid">
        ${caseText('前置条件', record.precondition_text)}
        ${caseText('操作步骤', record.steps_text)}
        ${caseText('人工预期原文', record.expected_text)}
        ${verificationPoints(record.verification_points)}
      </div>
    </section>
    <section class="panel">
      <header class="panel-head"><div><h2>实际执行轨迹</h2><p>包含用例命令以及 Runner 自动加入的栅栏、等待和截图</p></div><span class="chip chip-pending">${Array.isArray(record.command_trace) ? record.command_trace.length : 0} 条</span></header>
      <div class="panel-body command-phases">${actualCommandTrace(record.command_trace)}</div>
    </section>
    <details class="panel collapsible-panel">
      <summary class="panel-head"><div><h2>命令映射快照</h2><p>本次运行采用的计划，不能替代上方实际轨迹</p></div><span class="collapse-controls"><span class="collapse-action" aria-hidden="true"></span></span></summary>
      <div class="panel-body command-phases">
        ${commandPhase('准备环境', 'setup', record.planned_commands?.setup || record.setup)}
        ${commandPhase('执行操作', 'actions', record.planned_commands?.action || record.actions)}
        ${commandPhase('采集证据', 'collect', record.planned_commands?.collect || record.collect)}
      </div>
    </details>
    <section class="panel">
      <header class="panel-head"><div><h2>执行错误</h2><p>按测试阶段归类，不用日志猜测失败位置</p></div></header>
      <div class="panel-body">${testErrorEvidence(record)}</div>
    </section>
    <section class="panel">
      <header class="panel-head"><div><h2>${escapeHtml(screenshotLabel(record))}</h2><p>每个验证检查点分别采集，顺序与自动验证预期一致</p></div><span class="chip chip-pending">${Array.isArray(record.screenshot_urls) ? record.screenshot_urls.length : (record.screenshot_url ? 1 : 0)} 张</span></header>
      <div class="panel-body checkpoint-grid">${testScreenshotGallery(record)}</div>
    </section>
    <details class="panel collapsible-panel">
      <summary class="panel-head"><div><h2>终端 JSON</h2><p>仅用于诊断执行链路，不参与产品判定</p></div><span class="collapse-controls"><span class="chip chip-pending">${Array.isArray(record.terminal_json) ? record.terminal_json.length : 0} 条</span><span class="collapse-action" aria-hidden="true"></span></span></summary>
      <div class="panel-body"><pre class="code-block raw-output"><code>${escapeHtml(JSON.stringify(record.terminal_json || [], null, 2))}</code></pre></div>
    </details>`;
  document.querySelector('#delete-test-run').addEventListener('click', async () => {
    if (!window.confirm('确定删除这次测试记录和截图快照吗？此操作不可撤销。')) return;
    const button = document.querySelector('#delete-test-run');
    button.disabled = true;
    try {
      await api(`/api/test-history/${encodeURIComponent(sheet)}/${encodeURIComponent(caseId)}/${encodeURIComponent(runId)}?project=${encodeURIComponent(project)}`, {method: 'DELETE'});
      window.location.href = testDetailHref(project, sheet, caseId, returnTo);
    } catch (error) {
      button.disabled = false;
      showToast(error.message, 'error');
    }
  });
}

// --- F13: 系统设置全局管理 ---
function initSystemSettings() {
  const dialog = document.querySelector('#system-settings-dialog');
  const openBtn = document.querySelector('#open-system-settings');
  const closeBtn = document.querySelector('#close-system-settings');
  const cancelBtn = document.querySelector('#cancel-system-settings');
  const form = document.querySelector('#system-settings-form');
  const errorBox = document.querySelector('#settings-error-box');
  const submitBtn = document.querySelector('#submit-system-settings');

  if (!dialog || !openBtn) return;

  const setLlmSignal = (element, text, tone = 'neutral') => {
    if (!element) return;
    element.textContent = text;
    element.dataset.tone = tone;
  };

  // Tab switching
  dialog.querySelectorAll('.settings-tab-btn').forEach(tabBtn => {
    tabBtn.addEventListener('click', () => {
      const tabName = tabBtn.dataset.settingsTab;
      dialog.querySelectorAll('.settings-tab-btn').forEach(b => b.classList.toggle('is-active', b === tabBtn));
      dialog.querySelectorAll('.settings-tab-pane').forEach(pane => {
        pane.style.display = (pane.dataset.settingsPane === tabName) ? 'flex' : 'none';
      });
    });
  });

  // Password / Secret eye toggle
  dialog.querySelectorAll('.toggle-secret-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      const targetId = btn.dataset.target;
      const input = document.getElementById(targetId);
      if (input) {
        if (input.type === 'password') {
          input.type = 'text';
          btn.textContent = '隐藏';
        } else {
          input.type = 'password';
          btn.textContent = '显示';
        }
      }
    });
  });

  async function loadSettings() {
    if (errorBox) errorBox.style.display = 'none';
    try {
      const resp = await fetch('/api/config');
      if (!resp.ok) throw new Error('读取系统配置失败');
      const cfg = await resp.json();

      // LLM
      const llmModel = document.querySelector('#llm-display-model');
      const llmGateway = document.querySelector('#llm-display-gateway');
      const llmConfigStatus = document.querySelector('#llm-config-status');
      const llmActualSuccess = document.querySelector('#llm-actual-success');
      const llmEngineDot = document.querySelector('#llm-engine-dot');
      const configured = cfg.llm?.configured === true;
      if (llmModel) llmModel.textContent = cfg.llm?.model || 'gpt-5.6-sol';
      if (llmGateway) llmGateway.textContent = cfg.llm?.base_url || 'https://api.onefaka.com/v1';
      setLlmSignal(
        llmConfigStatus,
        configured ? `已配置${cfg.llm?.is_builtin ? '（内置服务）' : ''}` : '配置不完整',
        configured ? 'success' : 'error'
      );
      setLlmSignal(
        llmActualSuccess,
        cfg.llm?.last_actual_success_at ? `成功于 ${formatTime(cfg.llm.last_actual_success_at)}` : '尚无成功记录',
        cfg.llm?.last_actual_success_at ? 'success' : 'neutral'
      );
      if (llmEngineDot) llmEngineDot.dataset.tone = configured ? 'success' : 'error';

      // ONES
      const onesBase = document.querySelector('#cfg-ones-base-url');
      const onesAuth = document.querySelector('#cfg-ones-auth-token');
      const onesTeam = document.querySelector('#cfg-ones-team-uuid');
      const onesUser = document.querySelector('#cfg-ones-user-id');
      if (onesBase) onesBase.value = cfg.ones?.base_url || 'https://ones.topstepht.com:8443';
      if (onesAuth) onesAuth.value = cfg.ones?.auth_token || '';
      if (onesTeam) onesTeam.value = cfg.ones?.team_uuid || '';
      if (onesUser) onesUser.value = cfg.ones?.user_id || '';

      // Hardware
      const hwPort = document.querySelector('#cfg-hw-port');
      const hwBaud = document.querySelector('#cfg-hw-baudrate');
      const hwTrans = document.querySelector('#cfg-hw-transport');
      const hwCap = document.querySelector('#cfg-hw-capture');
      if (hwPort) hwPort.value = cfg.hardware?.port || 'COM7';
      if (hwBaud) hwBaud.value = cfg.hardware?.baudrate || 1500000;
      if (hwTrans) hwTrans.value = cfg.hardware?.transport || 'supercom';
      if (hwCap) hwCap.value = cfg.hardware?.capture_provider || 'mtp';
    } catch (err) {
      if (errorBox) {
        errorBox.textContent = `读取配置失败: ${err.message}`;
        errorBox.style.display = 'block';
      }
    }
  }

  openBtn.addEventListener('click', () => {
    loadSettings();
    dialog.showModal();
  });
  if (closeBtn) closeBtn.addEventListener('click', () => dialog.close());
  if (cancelBtn) cancelBtn.addEventListener('click', () => dialog.close());

  // LLM Connectivity Test
  const btnTestLlm = document.querySelector('#btn-test-llm');
  const llmTestStatus = document.querySelector('#llm-test-status');
  if (btnTestLlm) {
    btnTestLlm.addEventListener('click', async () => {
      btnTestLlm.disabled = true;
      btnTestLlm.textContent = '⏳ 测试中…';
      setLlmSignal(llmTestStatus, '正在发起握手测试…', 'pending');
      try {
        const resp = await fetch('/api/config/test-llm', { method: 'POST' });
        const data = await resp.json();
        if (data.ok) {
          setLlmSignal(llmTestStatus, `探测成功 · ${data.latency_ms}ms · ${data.model}`, 'success');
        } else {
          const cat = data.error_category ? `[${data.error_category}] ` : '';
          const sug = data.suggestion ? ` · ${data.suggestion}` : '';
          const detail = data.error || data.message || '未知错误';
          setLlmSignal(llmTestStatus, `本次探测失败 · ${cat}${detail}${sug}`, 'warning');
        }
      } catch (err) {
        setLlmSignal(llmTestStatus, `本次探测请求异常 · ${err.message || err}`, 'warning');
      } finally {
        btnTestLlm.disabled = false;
        btnTestLlm.textContent = '⚡ 测试服务连通性';
      }
    });
  }

  // ONES 一键登录并自动同步凭据逻辑
  const btnOnesLogin = document.querySelector('#btn-ones-login');
  const onesLoginStatus = document.querySelector('#ones-login-status');
  if (btnOnesLogin) {
    btnOnesLogin.addEventListener('click', async () => {
      const email = document.querySelector('#cfg-ones-email')?.value?.trim();
      const password = document.querySelector('#cfg-ones-password')?.value;
      const baseUrl = (document.querySelector('#cfg-ones-base-url')?.value?.trim() || 'https://ones.topstepht.com:8443').replace(/\/+$/, '');

      if (!email || !password) {
        if (onesLoginStatus) {
          onesLoginStatus.style.color = '#ef4444';
          onesLoginStatus.textContent = '❌ 请先输入 ONES 账号(邮箱)和密码';
        }
        return;
      }

      if (onesLoginStatus) {
        onesLoginStatus.style.color = 'var(--ink-soft)';
        onesLoginStatus.textContent = '⏳ 正在登录 ONES 并获取凭据...';
      }
      btnOnesLogin.disabled = true;

      try {
        const resp = await fetch('/api/ones/login', {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
          },
          body: JSON.stringify({ email, password, base_url: baseUrl })
        });

        const data = await resp.json();
        if (!resp.ok || !data?.user?.token) {
          throw new Error(data?.error || data?.desc || data?.reason || `HTTP ${resp.status}`);
        }

        const userUuid = data.user.uuid || '';
        const userToken = data.user.token || '';
        let teamUuid = '';
        if (Array.isArray(data.teams) && data.teams.length > 0) {
          const active = data.teams.find(t => t.status === 1) || data.teams[0];
          teamUuid = active.uuid || '';
        }

        const onesAuth = document.querySelector('#cfg-ones-auth-token');
        const onesUser = document.querySelector('#cfg-ones-user-id');
        const onesTeam = document.querySelector('#cfg-ones-team-uuid');
        if (onesAuth) onesAuth.value = userToken;
        if (onesUser) onesUser.value = userUuid;
        if (onesTeam) onesTeam.value = teamUuid;

        if (onesLoginStatus) {
          onesLoginStatus.style.color = '#10b981';
          onesLoginStatus.textContent = `✅ 登录成功！已自动获取用户 [${data.user.name || userUuid}] 凭据`;
        }
        const pwdInput = document.querySelector('#cfg-ones-password');
        if (pwdInput) pwdInput.value = '';
      } catch (err) {
        if (onesLoginStatus) {
          onesLoginStatus.style.color = '#ef4444';
          onesLoginStatus.textContent = `❌ 登录失败: ${err.message || err}`;
        }
      } finally {
        btnOnesLogin.disabled = false;
      }
    });
  }

  if (form) {
    form.addEventListener('submit', async e => {
      e.preventDefault();
      if (submitBtn) {
        submitBtn.disabled = true;
        submitBtn.textContent = '正在保存…';
      }
      if (errorBox) errorBox.style.display = 'none';

      const payload = {
        ones: {
          base_url: (document.querySelector('#cfg-ones-base-url')?.value || '').trim(),
          auth_token: (document.querySelector('#cfg-ones-auth-token')?.value || '').trim(),
          team_uuid: (document.querySelector('#cfg-ones-team-uuid')?.value || '').trim(),
          user_id: (document.querySelector('#cfg-ones-user-id')?.value || '').trim(),
        },
        hardware: {
          port: (document.querySelector('#cfg-hw-port')?.value || '').trim(),
          baudrate: Number(document.querySelector('#cfg-hw-baudrate')?.value) || 1500000,
          transport: document.querySelector('#cfg-hw-transport')?.value || 'supercom',
          capture_provider: document.querySelector('#cfg-hw-capture')?.value || 'mtp',
        },
      };

      try {
        const resp = await fetch('/api/config', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify(payload),
        });
        const resData = await resp.json();
        if (!resp.ok) throw new Error(resData.error || resData.message || '保存失败');

        showToast('系统设置已保存并实时生效！');
        dialog.close();
      } catch (err) {
        if (errorBox) {
          errorBox.textContent = `保存失败: ${err.message}`;
          errorBox.style.display = 'block';
        }
      } finally {
        if (submitBtn) {
          submitBtn.disabled = false;
          submitBtn.textContent = '保存设置';
        }
      }
    });
  }
}

function workspaceVerdictLabel(value) {
  const key = String(value || 'PENDING').toUpperCase();
  return {PASS: 'PASS', FAIL: 'FAIL', ERROR: 'ERROR', CANNOT_VERIFY: '无法验证', SKIP: '无法验证', PENDING: '未运行', RUNNING: '运行中'}[key] || key;
}

function environmentHealth(item = null) {
  const raw = String(item?.status || item?.readiness_status || 'unchecked').toLowerCase();
  if (['ready', 'pass', 'healthy'].includes(raw)) return {status: 'ready', label: '就绪'};
  if (['partial', 'warning', 'degraded'].includes(raw)) return {status: 'partial', label: '部分就绪'};
  if (['error', 'fail', 'failed', 'unhealthy'].includes(raw)) return {status: 'error', label: '检查失败'};
  return {status: 'unchecked', label: '尚未检查'};
}

function renderEnvironmentTargetCard(profile, environmentItem = null, selectedProject = currentProject()) {
  const project = String(profile.project || '');
  const protocol = ENVIRONMENT_PROTOCOLS[project] || {
    transport: profile.transport || 'unknown',
    transportLabel: profile.transport || '未知',
    captureProvider: profile.capture_provider || 'unknown',
    captureLabel: profile.capture_provider || '未知'
  };
  const health = environmentHealth(environmentItem);
  const status = health.status;
  const statusLabel = health.label;
  const iconName = profile.execution_target === 'hardware' ? 'environments' : 'runs';
  return `<article class='target-health-card is-${escapeHtml(status)} ${project === selectedProject ? 'is-selected' : ''}'>
    <div class='target-health-icon'>${icon(iconName, 23)}</div>
    <div class='target-health-copy'><div><strong>${escapeHtml(profile.project_label || project)}</strong><span>${escapeHtml(profile.execution_target_label || '')}</span></div><p class='health-status'><i></i>${escapeHtml(statusLabel)}</p></div>
    <dl><div><dt>命令通道</dt><dd>${escapeHtml(protocol.transportLabel)}</dd></div><div><dt>截图方式</dt><dd>${escapeHtml(protocol.captureLabel)}</dd></div><div><dt>最后检查</dt><dd>${environmentItem?.last_checked_at ? formatTime(environmentItem.last_checked_at) : '—'}</dd></div></dl>
    <a class='button button-secondary target-card-action' href='${escapeHtml(pageUrl('/environments', project))}'>查看环境</a>
  </article>`;
}

function renderDistributionDonut(counts = {}) {
  const pass = Number(counts.PASS || counts.pass || 0);
  const fail = Number(counts.FAIL || counts.fail || 0);
  const error = Number(counts.ERROR || counts.error || 0);
  const cannot = Number(counts.CANNOT_VERIFY || counts.cannot_verify || 0);
  const total = pass + fail + error + cannot;
  const passEnd = total ? pass * 100 / total : 0;
  const failEnd = total ? passEnd + fail * 100 / total : 0;
  const errorEnd = total ? failEnd + error * 100 / total : 0;
  const background = total
    ? `conic-gradient(var(--green) 0 ${passEnd}%, var(--red) ${passEnd}% ${failEnd}%, #e6a11f ${failEnd}% ${errorEnd}%, #a4a7aa ${errorEnd}% 100%)`
    : 'conic-gradient(var(--line) 0 100%)';
  return `<div class='distribution-layout'><div class='distribution-donut' style='background:${background}' role='img' aria-label='PASS ${pass}，FAIL ${fail}，ERROR ${error}，无法验证 ${cannot}'><div><strong>${total.toLocaleString('zh-CN')}</strong><span>已运行用例</span></div></div><ul class='distribution-legend'><li><i class='is-pass'></i><span>PASS</span><strong>${pass.toLocaleString('zh-CN')}</strong></li><li><i class='is-fail'></i><span>FAIL</span><strong>${fail.toLocaleString('zh-CN')}</strong></li><li><i class='is-error'></i><span>ERROR</span><strong>${error.toLocaleString('zh-CN')}</strong></li><li><i class='is-cannot'></i><span>无法验证</span><strong>${cannot.toLocaleString('zh-CN')}</strong></li></ul></div>`;
}

function renderCaseSnapshotRows(items = [], project = currentProject(), limit = 6) {
  const rows = items.slice(0, limit).map(item => {
    const sheet = String(item.file_sheet || item.sheet || '');
    const caseId = String(item.case_id || '');
    return `<tr><td><a class='case-id-link' href='${escapeHtml(testDetailHref(project, sheet, caseId, pageUrl('/overview', project)))}'>${escapeHtml(caseId)}</a></td><td>${escapeHtml(sheet)}</td><td>${Components.statusChip(item.latest_verdict, workspaceVerdictLabel(item.latest_verdict))}</td><td><time>${item.last_run_at ? formatTime(item.last_run_at) : '—'}</time></td></tr>`;
  }).join('');
  if (!rows) return Components.emptyState('暂无运行记录', '完成首条用例后，这里会显示真实结果。');
  return `<div class='workspace-table-scroll'><table class='workspace-table compact-table'><thead><tr><th>用例</th><th>模块</th><th>结果</th><th>最近运行</th></tr></thead><tbody>${rows}</tbody></table></div>`;
}

function OverviewPage(project = currentProject()) {
  let refreshTimer = null;
  return {
    async load() {
      const [catalog, active, defects, repair, reports, environments] = await Promise.all([
        api(`/api/tests/overview?project=${encodeURIComponent(project)}&recent_limit=7&exception_limit=6`),
        optionalApi('/api/tests/active'),
        optionalApi('/api/defects?page=1&page_size=6'),
        optionalApi('/api/run/active'),
        optionalApi(`/api/reports/summary?project=${encodeURIComponent(project)}`),
        optionalApi('/api/environments')
      ]);
      return {catalog, active, defects, repair, reports, environments, refreshedAt: new Date()};
    },
    render(data) {
      const {catalog, active, defects, repair, reports, environments, refreshedAt} = data;
      const jobs = active.data ? activeTestJobs(active.data) : [];
      const activeBatch = jobs.find(job => job.type === 'batch') || null;
      const verdicts = catalog.verdict_summary || verdictCounts([]);
      const recentExceptions = catalog.recent_exceptions || [];
      const recentCases = catalog.recent_items || [];
      const profiles = catalog.projects || Object.values(TEST_PROJECTS);
      const environmentItems = environments.data?.items || [];
      const total = Number(catalog.summary?.all || 0);
      const completed = activeBatch ? Number(activeBatch.completed || 0) : 0;
      const batchTotal = activeBatch ? Number(activeBatch.total || 0) : 0;
      const percent = batchTotal ? Math.round(completed * 100 / batchTotal) : 0;
      const batchCounts = activeBatch?.verdict_counts || {};
      const activeBatchHtml = activeBatch ? `<article class='workspace-panel active-batch-card'><header><div><h2>执行批次（进行中）</h2><span class='chip chip-running'>${escapeHtml(activeBatch.id || '运行中')}</span></div><a class='button button-secondary' href='/test-batch/${encodeURIComponent(activeBatch.id)}'>查看详情</a></header><div class='active-batch-layout'>${Components.progressRing(percent)}<div class='batch-facts'><dl><div><dt>执行环境</dt><dd>${escapeHtml(testProject(activeBatch.project || project).projectLabel)} · ${escapeHtml(testProject(activeBatch.project || project).targetLabel)}</dd></div><div><dt>执行用例</dt><dd>${completed.toLocaleString('zh-CN')} / ${batchTotal.toLocaleString('zh-CN')}</dd></div><div><dt>开始时间</dt><dd>${formatTime(activeBatch.started_at)}</dd></div></dl><div class='batch-progress-track'><div class='batch-progress-bar' style='width:${percent}%'></div></div><div class='inline-verdicts'><span class='is-pass'>PASS ${Number(batchCounts.PASS || 0)}</span><span class='is-fail'>FAIL ${Number(batchCounts.FAIL || 0)}</span><span class='is-error'>ERROR ${Number(batchCounts.ERROR || 0)}</span><span class='is-cannot'>无法验证 ${Number(batchCounts.CANNOT_VERIFY || 0)}</span></div></div></div></article>` : `<article class='workspace-panel active-batch-card'><header><div><h2>执行批次</h2><span class='chip chip-pending'>当前空闲</span></div><a class='button' href='${escapeHtml(pageUrl('/cases', project))}'>新建执行</a></header>${Components.emptyState('当前没有运行中的批次', '可从用例管理勾选用例，或按状态创建批次。')}</article>`;
      const reportAvailable = Boolean(reports.data);
      const reportCounts = reports.data?.distribution || verdicts;
      const defectSummary = defects.data?.summary || {};
      const repairJob = repair.data?.job || null;
      return `${Components.pageHeader({title: '项目总览', intro: '实时掌握测试执行状态、用例质量与环境健康度', updatedAt: formatTime(refreshedAt), actions: `<button class='button button-secondary' type='button' data-overview-refresh>${icon('refresh', 17)} 刷新</button>`})}
        <section class='workspace-panel environment-overview-panel'><header><div><h2>环境就绪状态</h2><p>协议来自项目配置；就绪状态只采用真实环境检查结果</p></div><a class='button button-secondary' href='${escapeHtml(pageUrl('/environments', project))}'>环境中心 ›</a></header><div class='target-health-grid'>${profiles.map(profile => renderEnvironmentTargetCard(profile, environmentItems.find(item => item.id === profile.project || item.project === profile.project), project)).join('')}</div></section>
        <section class='workspace-kpi-grid'>${Components.metricCard({label: '用例总数', value: total.toLocaleString('zh-CN'), hint: '当前项目', tone: 'blue', iconName: 'cases'})}${Components.metricCard({label: '已固化', value: Number(catalog.summary?.solidified || 0).toLocaleString('zh-CN'), hint: total ? `${(Number(catalog.summary?.solidified || 0) * 100 / total).toFixed(1)}%` : '—', tone: 'green', iconName: 'check'})}${Components.metricCard({label: '运行中', value: String(jobs.length), hint: jobs.length ? '活动任务' : '当前空闲', tone: 'amber', iconName: 'runs'})}${Components.metricCard({label: '最新结果 PASS', value: verdicts.PASS.toLocaleString('zh-CN'), tone: 'green', iconName: 'check'})}${Components.metricCard({label: '最新结果 FAIL', value: verdicts.FAIL.toLocaleString('zh-CN'), tone: 'red', iconName: 'warning'})}${Components.metricCard({label: '最新结果 ERROR', value: verdicts.ERROR.toLocaleString('zh-CN'), tone: 'red', iconName: 'warning'})}</section>
        <section class='overview-primary-grid'>${activeBatchHtml}<article class='workspace-panel recent-exceptions-panel'><header><div><h2>最近异常</h2><p>按用例最近一次真实结果排序</p></div><a class='text-button' href='${escapeHtml(pageUrl('/reports', project, {view: 'failures'}))}'>查看更多</a></header>${renderCaseSnapshotRows(recentExceptions, project, 6)}</article><article class='workspace-panel quick-actions-panel'><header><div><h2>快捷操作</h2><p>常用入口</p></div></header><div class='quick-action-grid'><a href='${escapeHtml(pageUrl('/cases', project))}'>${icon('runs', 22)}<span><strong>新建执行</strong><small>选择并运行用例</small></span></a><a href='${escapeHtml(pageUrl('/cases', project))}'>${icon('plus', 22)}<span><strong>添加用例</strong><small>新建或导入</small></span></a><a href='${escapeHtml(pageUrl('/reports', project))}'>${icon('reports', 22)}<span><strong>测试报告</strong><small>查看结果快照</small></span></a><a href='${escapeHtml(pageUrl('/defects', project))}'>${icon('defects', 22)}<span><strong>缺陷闭环</strong><small>处理失败缺陷</small></span></a><a href='${escapeHtml(pageUrl('/environments', project))}'>${icon('environments', 22)}<span><strong>环境中心</strong><small>核对测试链路</small></span></a></div></article></section>
        <section class='overview-secondary-grid'><article class='workspace-panel'><header><div><h2>最新用例</h2><p>最近发生运行的用例</p></div><a class='text-button' href='${escapeHtml(pageUrl('/cases', project))}'>全部用例</a></header>${renderCaseSnapshotRows(recentCases, project, 7)}</article><article class='workspace-panel'><header><div><h2>报告概览</h2><p>${reportAvailable ? '报告聚合接口' : '当前用例最新结果快照'}</p></div><a class='text-button' href='${escapeHtml(pageUrl('/reports', project))}'>打开报告</a></header>${renderDistributionDonut(reportCounts)}${!reportAvailable ? Components.unavailableState('趋势数据暂不可用', '当前后端未提供 /api/reports/summary，未伪造历史趋势。') : ''}</article></section>
        <section class='workspace-panel defect-overview-strip'><header><div><h2>缺陷闭环概览</h2><p>缺陷队列与当前自动修复任务</p></div><a class='button button-secondary' href='${escapeHtml(pageUrl('/defects', project))}'>查看缺陷闭环</a></header><div class='digest-items'><div><span>全部缺陷</span><strong>${Number(defectSummary.all || 0)}</strong></div><div><span>已通过</span><strong>${Number(defectSummary.passed || 0)}</strong></div><div><span>失败</span><strong>${Number(defectSummary.failed || 0)}</strong></div><div><span>待处理</span><strong>${Number(defectSummary.pending || 0)}</strong></div><div><span>当前修复任务</span><strong>${repairJob ? escapeHtml(repairJob.id || '运行中') : '无'}</strong></div></div></section>
        <section class='workspace-panel daily-digest'><header><div><h2>今日运营概览</h2><p>${reportAvailable ? '来自报告聚合接口' : '报告接口尚未提供，空缺项不以 0 冒充'}</p></div></header><div class='digest-items'><div><span>运行批次</span><strong>${reportAvailable ? Number(reports.data.metrics?.batches || 0) : '—'}</strong></div><div><span>通过率</span><strong>${reportAvailable ? `${Number(reports.data.metrics?.pass_rate || 0).toFixed(1)}%` : '—'}</strong></div><div><span>缺陷修复</span><strong>${reportAvailable ? Number(reports.data.metrics?.repairs || 0) : '—'}</strong></div><div><span>无法验证</span><strong>${reportAvailable ? Number(reports.data.metrics?.cannot_verify || 0) : '—'}</strong></div></div></section>`;
    },
    mount(root, data) {
      rememberProject(project);
      const currentEnvironment = data.environments.data?.items?.find(item => item.id === project || item.project === project);
      const health = environmentHealth(currentEnvironment);
      setGlobalTargetHealth(health.status, health.label);
      root.querySelector('[data-overview-refresh]')?.addEventListener('click', () => {
        invalidateCaseCatalog(project);
        route();
      });
      if ((data.active.data ? activeTestJobs(data.active.data) : []).length) refreshTimer = setTimeout(() => route(), 2000);
    },
    destroy() { clearTimeout(refreshTimer); }
  };
}

function renderWorkflowStepper(job = {}) {
  const nodes = job.nodes || {};
  const currentNode = String(job.current_node || '');
  const currentIndex = Math.max(0, TEST_WORKFLOW_NODES.indexOf(currentNode));
  return `<ol class='workflow-stepper'>${TEST_WORKFLOW_NODES.map((node, index) => {
    const raw = typeof nodes[node] === 'object' ? nodes[node].status : nodes[node];
    const status = String(raw || (index < currentIndex ? 'completed' : index === currentIndex ? 'running' : 'pending')).toLowerCase();
    const className = ['completed', 'done', 'pass', 'success'].includes(status) ? 'is-complete' : ['running', 'active'].includes(status) ? 'is-active' : ['failed', 'error'].includes(status) ? 'is-failed' : '';
    return `<li class='${className}'><i>${className === 'is-complete' ? icon('check', 14) : index + 1}</i><span>${escapeHtml(TEST_NODE_LABELS[node])}</span></li>`;
  }).join('')}</ol>`;
}

function renderExecutionEvidence(job = {}) {
  const screenshots = Array.isArray(job.live_screenshots) ? job.live_screenshots : [];
  if (!screenshots.length) return Components.emptyState('尚无实时截图', '截图检查点生成后会自动显示。');
  return `<div class='execution-evidence-grid'>${screenshots.slice(-4).map((item, index) => `<figure><a href='${escapeHtml(item.url || '#')}' target='_blank' rel='noopener'><img src='${escapeHtml(item.url || '')}' alt='${escapeHtml(item.label || `检查点 ${index + 1}`)}'></a><figcaption>${escapeHtml(item.label || `检查点 ${index + 1}`)}</figcaption></figure>`).join('')}</div>`;
}

function renderActiveExecution(job, project) {
  if (!job) return `<article class='workspace-panel execution-main-panel'><header><div><h2>当前执行任务</h2><p>当前测试目标没有活动任务</p></div><a class='button' href='${escapeHtml(pageUrl('/cases', project))}'>${icon('plus', 17)} 新建执行任务</a></header>${Components.emptyState('执行队列为空', '从用例管理勾选用例后即可创建批次。')}</article>`;
  const completed = Number(job.completed || (job.status === 'completed' ? 1 : 0));
  const total = Number(job.total || 1);
  const percent = total ? Math.round(completed * 100 / total) : 0;
  const counts = job.verdict_counts || {};
  const currentCase = job.current_case || {case_id: job.case_id, sheet: job.sheet};
  const isActive = ['queued', 'running', 'finalizing'].includes(String(job.status));
  return `<article class='workspace-panel execution-main-panel'><header><div><h2>${escapeHtml(job.type === 'batch' ? `${testProject(job.project || project).projectLabel} 批次执行` : `用例 ${job.case_id || ''}`)}</h2>${Components.statusChip(isActive ? 'RUNNING' : job.verdict || job.status, isActive ? '运行中' : workspaceVerdictLabel(job.verdict || job.status))}</div><div class='panel-actions'>${job.type === 'batch' ? `<a class='button button-secondary' href='/test-batch/${encodeURIComponent(job.id)}'>查看详情</a>` : ''}${isActive && job.type === 'batch' ? `<button class='button button-secondary' type='button' data-job-pause='${escapeHtml(job.id)}'>${icon('pause', 16)} 暂停批次</button>` : ''}${job.resume_available ? `<button class='button' type='button' data-job-resume='${escapeHtml(job.id)}'>继续运行</button>` : ''}</div></header><div class='active-run-grid'><div class='active-run-progress'>${Components.progressRing(percent)}<div><strong>${completed.toLocaleString('zh-CN')} / ${total.toLocaleString('zh-CN')}</strong><div class='batch-progress-track'><div class='batch-progress-bar' style='width:${percent}%'></div></div><div class='inline-verdicts'><span class='is-pass'>PASS ${Number(counts.PASS || 0)}</span><span class='is-fail'>FAIL ${Number(counts.FAIL || 0)}</span><span class='is-error'>ERROR ${Number(counts.ERROR || 0)}</span><span class='is-cannot'>无法验证 ${Number(counts.CANNOT_VERIFY || 0)}</span></div></div></div><div class='current-case-card'><span>当前用例</span><strong>${escapeHtml(currentCase?.case_id || '正在准备')}</strong><small>${escapeHtml(currentCase?.sheet || testProject(job.project || project).targetLabel)}</small><div><span>当前阶段</span><strong>${escapeHtml(TEST_NODE_LABELS[job.current_node] || job.current_node || '等待调度')}</strong></div>${renderWorkflowStepper(job)}</div><aside class='live-evidence-card'><h3>实时证据（最新）</h3>${renderExecutionEvidence(job)}</aside></div></article>`;
}

function ExecutionPage(project = currentProject()) {
  let pollTimer = null;
  return {
    async load() {
      const view = ['queue', 'running', 'completed', 'interrupted'].includes(new URLSearchParams(location.search).get('view')) ? new URLSearchParams(location.search).get('view') : 'running';
      const [recent, active, jobs] = await Promise.all([
        optionalApi(`/api/tests/recent?project=${encodeURIComponent(project)}&limit=8`),
        optionalApi('/api/tests/active'),
        optionalApi(`/api/tests/jobs?project=${encodeURIComponent(project)}&status=${encodeURIComponent(view)}&page=1&page_size=20`)
      ]);
      return {recent, active, jobs, view};
    },
    render(data) {
      const activeJobs = data.active.data ? activeTestJobs(data.active.data).filter(job => !job.project || job.project === project) : [];
      const activeJob = activeJobs.find(job => job.type === 'batch') || activeJobs[0] || null;
      const jobItems = data.jobs.data?.items || [];
      const recentItems = data.recent.data?.items || [];
      const recentResults = recentItems.map(item => `<tr><td>${formatTime(item.last_run_at)}</td><td><a class='case-id-link' href='${escapeHtml(testDetailHref(project, item.file_sheet || item.sheet, item.case_id, pageUrl('/runs', project)))}'>${escapeHtml(item.case_id)}</a></td><td>${escapeHtml(item.sheet)}</td><td>${Components.statusChip(item.latest_verdict, workspaceVerdictLabel(item.latest_verdict))}</td><td>${Number(item.history_count || 0)} 次</td></tr>`).join('');
      const listArea = data.view === 'running'
        ? renderActiveExecution(activeJob, project)
        : data.jobs.data
          ? `<article class='workspace-panel'><header><div><h2>${escapeHtml({queue: '任务队列', completed: '已完成任务', interrupted: '已中断任务'}[data.view] || '任务')}</h2><p>来自任务列表接口</p></div></header>${jobItems.length ? `<div class='workspace-table-scroll'><table class='workspace-table'><thead><tr><th>任务</th><th>项目</th><th>状态</th><th>进度</th><th>时间</th></tr></thead><tbody>${jobItems.map(job => `<tr><td>${escapeHtml(job.id)}</td><td>${escapeHtml(job.project_label || job.project || '')}</td><td>${Components.statusChip(job.verdict || job.status, workspaceVerdictLabel(job.verdict || job.status))}</td><td>${Number(job.completed || 0)} / ${Number(job.total || 1)}</td><td>${formatTime(job.finished_at || job.started_at)}</td></tr>`).join('')}</tbody></table></div>` : Components.emptyState('当前分类没有任务')}</article>`
          : `<article class='workspace-panel'>${Components.unavailableState('任务列表暂不可用', '当前后端未提供 GET /api/tests/jobs 列表接口；活动任务仍由 /api/tests/active 正常显示。')}</article>`;
      return `${Components.pageHeader({title: '自动化执行', intro: '创建、监控和恢复单条或批量测试任务', actions: `<a class='button' href='${escapeHtml(pageUrl('/cases', project))}'>${icon('runs', 17)} 新建执行任务</a><a class='button button-secondary' href='${escapeHtml(pageUrl('/cases', project))}'>${icon('plus', 17)} 按状态创建批次</a>`})}${Components.subTabs([{value: 'queue', label: '任务队列'}, {value: 'running', label: '运行中'}, {value: 'completed', label: '已完成'}, {value: 'interrupted', label: '已中断'}], data.view)}<section class='workspace-kpi-grid is-four'>${Components.metricCard({label: '运行中', value: String(activeJobs.filter(job => ['queued', 'running', 'finalizing'].includes(job.status)).length), tone: 'green', iconName: 'runs'})}${Components.metricCard({label: '队列中', value: data.jobs.data ? String(Number(data.jobs.data.summary?.queued || 0)) : '—', hint: data.jobs.data ? '' : '接口待支持', tone: 'amber', iconName: 'runs'})}${Components.metricCard({label: '今日完成', value: data.jobs.data ? String(Number(data.jobs.data.summary?.completed_today || 0)) : '—', hint: data.jobs.data ? '' : '接口待支持', tone: 'green', iconName: 'check'})}${Components.metricCard({label: '执行异常', value: data.jobs.data ? String(Number(data.jobs.data.summary?.error || 0)) : '—', hint: data.jobs.data ? '' : '接口待支持', tone: 'red', iconName: 'warning'})}</section>${listArea}<article class='workspace-panel execution-results-panel'><header><div><h2>最近结果</h2><p>来自每条用例最近一次真实运行记录</p></div><a class='text-button' href='${escapeHtml(pageUrl('/reports', project))}'>测试报告</a></header>${recentResults ? `<div class='workspace-table-scroll'><table class='workspace-table'><thead><tr><th>时间</th><th>用例</th><th>模块</th><th>结果</th><th>历史</th></tr></thead><tbody>${recentResults}</tbody></table></div>` : Components.emptyState('暂无运行结果')}</article>`;
    },
    mount(root, data) {
      rememberProject(project);
      root.querySelectorAll('[data-subtab]').forEach(button => button.addEventListener('click', () => {
        history.pushState({}, '', pageUrl('/runs', project, {view: button.dataset.subtab}));
        route();
      }));
      root.querySelector('[data-job-pause]')?.addEventListener('click', async event => {
        if (!window.confirm('暂停不会中断当前用例；当前用例完成并保存证据后才会暂停。确定继续吗？')) return;
        event.currentTarget.disabled = true;
        try { await api(`/api/tests/jobs/${encodeURIComponent(event.currentTarget.dataset.jobPause)}/cancel`, {method: 'POST', body: '{}'}); showToast('已请求暂停', 'warning'); route(); } catch (error) { event.currentTarget.disabled = false; showToast(error.message, 'error'); }
      });
      root.querySelector('[data-job-resume]')?.addEventListener('click', async event => {
        event.currentTarget.disabled = true;
        try { await api(`/api/tests/jobs/${encodeURIComponent(event.currentTarget.dataset.jobResume)}/resume`, {method: 'POST', body: '{}'}); showToast('已继续运行', 'success'); route(); } catch (error) { event.currentTarget.disabled = false; showToast(error.message, 'error'); }
      });
      const hasActive = data.active.data && activeTestJobs(data.active.data).length > 0;
      if (data.view === 'running' && hasActive) pollTimer = setTimeout(() => route(), 2000);
    },
    destroy() { clearTimeout(pollTimer); }
  };
}

function isoDateOffset(days = 0) {
  const date = new Date();
  date.setDate(date.getDate() + days);
  return date.toISOString().slice(0, 10);
}

function reportParams() {
  const params = new URLSearchParams(location.search);
  const view = ['overview', 'batches', 'cases', 'failures'].includes(params.get('view')) ? params.get('view') : 'overview';
  const module = ALL_FUNCTION_MODULES.includes(params.get('module')) ? params.get('module') : '';
  const from = /^\d{4}-\d{2}-\d{2}$/.test(params.get('from') || '') ? params.get('from') : isoDateOffset(-6);
  const to = /^\d{4}-\d{2}-\d{2}$/.test(params.get('to') || '') ? params.get('to') : isoDateOffset(0);
  return {view, module, from, to};
}

function buildSnapshotReport(items = [], filters = {}) {
  const fromTime = filters.from ? new Date(`${filters.from}T00:00:00`).getTime() : 0;
  const toTime = filters.to ? new Date(`${filters.to}T23:59:59`).getTime() : Number.MAX_SAFE_INTEGER;
  const executed = items.filter(item => {
    if (!item.last_run_at) return false;
    if (filters.module && String(item.file_sheet || item.sheet) !== filters.module) return false;
    const time = new Date(item.last_run_at).getTime();
    return Number.isFinite(time) && time >= fromTime && time <= toTime;
  });
  const distribution = verdictCounts(executed);
  const total = distribution.PASS + distribution.FAIL + distribution.ERROR + distribution.CANNOT_VERIFY;
  const moduleMap = new Map();
  for (const item of executed) {
    const verdict = String(item.latest_verdict || '').toUpperCase();
    if (!['FAIL', 'ERROR'].includes(verdict)) continue;
    const moduleName = String(item.file_sheet || item.sheet || '未分类');
    const current = moduleMap.get(moduleName) || {module: moduleName, fail: 0, error: 0};
    current[verdict === 'FAIL' ? 'fail' : 'error'] += 1;
    moduleMap.set(moduleName, current);
  }
  const topFailModules = [...moduleMap.values()].map(item => ({...item, total: item.fail + item.error})).sort((a, b) => b.total - a.total).slice(0, 8);
  const recentFailures = executed.filter(item => ['FAIL', 'ERROR', 'CANNOT_VERIFY', 'SKIP'].includes(String(item.latest_verdict || '').toUpperCase())).sort((left, right) => String(right.last_run_at).localeCompare(String(left.last_run_at))).slice(0, 12);
  return {
    source: 'snapshot',
    metrics: {total, pass: distribution.PASS, fail: distribution.FAIL, error: distribution.ERROR, cannot_verify: distribution.CANNOT_VERIFY, pass_rate: total ? distribution.PASS * 100 / total : 0},
    distribution,
    trend: [],
    top_fail_modules: topFailModules,
    recent_failures: recentFailures,
    insight: null
  };
}

function renderTrendChart(trend = []) {
  if (!Array.isArray(trend) || !trend.length) return Components.unavailableState('结果趋势暂不可用', '当前后端没有返回按日期聚合的趋势数据；这里不会根据最新快照编造历史。');
  const values = trend.slice(-14).map(item => ({date: String(item.date || ''), pass: Number(item.pass || 0), fail: Number(item.fail || 0), error: Number(item.error || 0), total: Number(item.total || 0), passRate: Number(item.pass_rate || 0)}));
  const width = 760;
  const height = 250;
  const left = 48;
  const right = 26;
  const top = 24;
  const bottom = 42;
  const chartWidth = width - left - right;
  const chartHeight = height - top - bottom;
  const maxTotal = Math.max(1, ...values.map(item => item.total || item.pass + item.fail + item.error));
  const step = chartWidth / values.length;
  const barWidth = Math.min(42, step * .52);
  const bars = values.map((item, index) => {
    const x = left + step * index + (step - barWidth) / 2;
    const passHeight = item.pass * chartHeight / maxTotal;
    const failHeight = item.fail * chartHeight / maxTotal;
    const errorHeight = item.error * chartHeight / maxTotal;
    const base = top + chartHeight;
    return `<rect x='${x}' y='${base - passHeight}' width='${barWidth}' height='${passHeight}' rx='3' fill='var(--green)' opacity='.82'/><rect x='${x}' y='${base - passHeight - failHeight}' width='${barWidth}' height='${failHeight}' fill='var(--red)' opacity='.9'/><rect x='${x}' y='${base - passHeight - failHeight - errorHeight}' width='${barWidth}' height='${errorHeight}' fill='#e6a11f'/><text x='${x + barWidth / 2}' y='${height - 14}' text-anchor='middle'>${escapeHtml(item.date.slice(5))}</text>`;
  }).join('');
  const points = values.map((item, index) => `${left + step * index + step / 2},${top + chartHeight - Math.max(0, Math.min(100, item.passRate)) * chartHeight / 100}`).join(' ');
  const circles = values.map((item, index) => `<circle cx='${left + step * index + step / 2}' cy='${top + chartHeight - Math.max(0, Math.min(100, item.passRate)) * chartHeight / 100}' r='3.5' fill='var(--surface)' stroke='var(--green)' stroke-width='2'><title>${escapeHtml(item.date)} 通过率 ${item.passRate.toFixed(1)}%</title></circle>`).join('');
  return `<div class='trend-chart-wrap'><svg class='trend-chart' viewBox='0 0 ${width} ${height}' role='img' aria-label='测试结果趋势'><line x1='${left}' y1='${top + chartHeight}' x2='${width - right}' y2='${top + chartHeight}' stroke='var(--line)'/><line x1='${left}' y1='${top}' x2='${left}' y2='${top + chartHeight}' stroke='var(--line)'/>${bars}<polyline points='${points}' fill='none' stroke='var(--green)' stroke-width='2.5' stroke-linejoin='round'/>${circles}</svg></div>`;
}

function renderFailureModules(items = []) {
  if (!items.length) return Components.emptyState('当前范围没有失败模块');
  const rows = items.map(item => `<tr><td><strong>${escapeHtml(item.module || item.sheet || '未分类')}</strong></td><td>${Number(item.fail || item.count || 0)}</td><td>${Number(item.error || 0)}</td><td>${Number(item.total || Number(item.fail || 0) + Number(item.error || 0))}</td></tr>`).join('');
  return `<div class='workspace-table-scroll'><table class='workspace-table'><thead><tr><th>模块</th><th>FAIL</th><th>ERROR</th><th>异常合计</th></tr></thead><tbody>${rows}</tbody></table></div>`;
}

function renderRecentFailures(items = [], project = currentProject(), returnTo = pageUrl('/reports', project, {view: 'failures'})) {
  if (!items.length) return Components.emptyState('当前范围没有失败记录');
  const rows = items.map(item => {
    const caseId = item.case_id || item.caseId || '';
    const sheet = item.module || item.sheet || item.file_sheet || '';
    const historyId = item.history_id || item.run_id || '';
    const verdict = item.verdict || item.latest_verdict || 'FAIL';
    const time = item.at || item.timestamp || item.last_run_at;
    const historyHref = caseId && sheet && historyId ? testHistoryHref(project, sheet, caseId, historyId, returnTo) : '';
    const detailHref = historyHref || (caseId && sheet ? testDetailHref(project, sheet, caseId, returnTo) : '');
    return `<tr><td>${time ? formatTime(time) : '—'}</td><td>${escapeHtml(sheet)}</td><td>${detailHref ? `<a class='case-id-link' href='${escapeHtml(detailHref)}'>${escapeHtml(caseId)}</a>` : escapeHtml(caseId)}</td><td>${Components.statusChip(verdict, workspaceVerdictLabel(verdict))}</td><td>${escapeHtml(friendlyAgentError(item.message || item.reason || '未记录原因'))}</td><td class='table-actions'>${detailHref ? `<a class='text-button' href='${escapeHtml(detailHref)}'>${historyHref ? '查看本次运行' : '查看用例'} →</a>` : '—'}</td></tr>`;
  }).join('');
  return `<div class='workspace-table-scroll'><table class='workspace-table'><thead><tr><th>时间</th><th>模块</th><th>用例</th><th>状态</th><th>错误信息</th><th>操作</th></tr></thead><tbody>${rows}</tbody></table></div>`;
}

function ReportsPage(project = currentProject()) {
  return {
    async load() {
      const filters = reportParams();
      const query = new URLSearchParams({project, from: filters.from, to: filters.to});
      if (filters.module) query.set('module', filters.module);
      const [summary, runs] = await Promise.all([
        optionalApi(`/api/reports/summary?${query.toString()}`),
        optionalApi(`/api/reports/runs?scope=${filters.view === 'batches' ? 'batch' : 'case'}&${query.toString()}&page=1&page_size=20`)
      ]);
      let fallbackItems = [];
      if (!summary.data) {
        const catalog = await loadCaseCatalog(project);
        fallbackItems = catalog.items || [];
      }
      const report = summary.data || buildSnapshotReport(fallbackItems, filters);
      return {summary, runs, report, filters};
    },
    render(data) {
      const {report, filters, summary, runs} = data;
      const metrics = report.metrics || {};
      const distribution = report.distribution || metrics;
      const reportReturnTo = pageUrl('/reports', project, {view: filters.view, from: filters.from, to: filters.to, module: filters.module});
      let content;
      if (filters.view === 'overview') {
        content = `<section class='report-chart-grid'><article class='workspace-panel'><header><div><h2>结果趋势</h2><p>柱形为执行数量，折线为通过率</p></div><span class='chip chip-pending'>${escapeHtml(filters.from)} 至 ${escapeHtml(filters.to)}</span></header>${renderTrendChart(report.trend)}</article><article class='workspace-panel'><header><div><h2>结果分布</h2><p>${summary.data ? '报告聚合数据' : '用例最新结果快照'}</p></div></header>${renderDistributionDonut(distribution)}</article></section><section class='report-detail-grid'><article class='workspace-panel'><header><div><h2>高频失败模块</h2><p>按 FAIL 与 ERROR 合计排序</p></div></header>${renderFailureModules(report.top_fail_modules || [])}</article><article class='workspace-panel'><header><div><h2>最近失败记录</h2><p>当前筛选范围</p></div></header>${renderRecentFailures(report.recent_failures || [], project, reportReturnTo)}</article></section>${report.insight ? `<aside class='report-insight'>${icon('reports', 22)}<div><strong>${escapeHtml(report.insight.title || '分析结论')}</strong><p>${escapeHtml(report.insight.description || report.insight.message || '')}</p></div></aside>` : Components.unavailableState('自动分析结论暂不可用', '报告聚合接口尚未提供分析结论，页面不根据少量快照擅自下判断。')}`;
      } else if (filters.view === 'failures') {
        content = `<article class='workspace-panel'><header><div><h2>失败分析</h2><p>当前筛选范围内的真实失败与异常记录</p></div></header>${renderRecentFailures(report.recent_failures || [], project, reportReturnTo)}</article>`;
      } else {
        content = runs.data ? `<article class='workspace-panel'><header><div><h2>${filters.view === 'batches' ? '批次报告' : '单条记录'}</h2><p>来自报告运行列表接口</p></div></header>${runs.data.items?.length ? `<pre>${escapeHtml(JSON.stringify(runs.data.items, null, 2))}</pre>` : Components.emptyState('暂无记录')}</article>` : `<article class='workspace-panel'>${Components.unavailableState(filters.view === 'batches' ? '批次报告暂不可用' : '单条记录列表暂不可用', '当前后端未提供 /api/reports/runs，已保留页面结构和筛选条件。')}</article>`;
      }
      const exportHref = `/api/reports/export?${new URLSearchParams({project, from: filters.from, to: filters.to, ...(filters.module ? {module: filters.module} : {})}).toString()}`;
      return `${Components.pageHeader({title: '测试报告', intro: '查看结果趋势、批次结论和完整证据', actions: `<button class='button button-secondary' type='button' data-report-export ${summary.data ? '' : 'disabled title="当前后端未提供报告导出接口"'}>${icon('reports', 17)} 导出报告</button>`})}<section class='report-filter-bar'><label>项目<strong>${escapeHtml(testProject(project).projectLabel)}</strong></label><label>目标<strong>${escapeHtml(testProject(project).targetLabel)}</strong></label><label>开始日期<input id='report-from' type='date' value='${escapeHtml(filters.from)}'></label><label>结束日期<input id='report-to' type='date' value='${escapeHtml(filters.to)}'></label><label>模块<select id='report-module'><option value=''>全部模块</option>${ALL_FUNCTION_MODULES.map(name => `<option value='${escapeHtml(name)}' ${filters.module === name ? 'selected' : ''}>${escapeHtml(name)}</option>`).join('')}</select></label></section>${Components.subTabs([{value: 'overview', label: '报告概览'}, {value: 'batches', label: '批次报告'}, {value: 'cases', label: '单条记录'}, {value: 'failures', label: '失败分析'}], filters.view)}${!summary.data ? `<aside class='data-source-banner'>${icon('warning', 18)}<span>当前版本缺少报告聚合接口。可计算区域使用真实“用例最新结果快照”，历史趋势保持空白。</span></aside>` : ''}<section class='workspace-kpi-grid is-six'>${Components.metricCard({label: '已运行用例', value: Number(metrics.total || 0).toLocaleString('zh-CN'), tone: 'blue', iconName: 'cases'})}${Components.metricCard({label: 'PASS', value: Number(metrics.pass || distribution.PASS || 0).toLocaleString('zh-CN'), tone: 'green', iconName: 'check'})}${Components.metricCard({label: 'FAIL', value: Number(metrics.fail || distribution.FAIL || 0).toLocaleString('zh-CN'), tone: 'red', iconName: 'warning'})}${Components.metricCard({label: 'ERROR', value: Number(metrics.error || distribution.ERROR || 0).toLocaleString('zh-CN'), tone: 'amber', iconName: 'warning'})}${Components.metricCard({label: '无法验证', value: Number(metrics.cannot_verify || distribution.CANNOT_VERIFY || 0).toLocaleString('zh-CN'), tone: 'gray', iconName: 'warning'})}${Components.metricCard({label: '通过率', value: `${Number(metrics.pass_rate || 0).toFixed(1)}%`, tone: 'green', iconName: 'reports'})}</section>${content}`;
    },
    mount(root, data) {
      rememberProject(project);
      const applyFilters = () => {
        const from = root.querySelector('#report-from')?.value || data.filters.from;
        const to = root.querySelector('#report-to')?.value || data.filters.to;
        const module = root.querySelector('#report-module')?.value || '';
        history.pushState({}, '', pageUrl('/reports', project, {view: data.filters.view, from, to, module}));
        route();
      };
      root.querySelector('#report-from')?.addEventListener('change', applyFilters);
      root.querySelector('#report-to')?.addEventListener('change', applyFilters);
      root.querySelector('#report-module')?.addEventListener('change', applyFilters);
      root.querySelectorAll('[data-subtab]').forEach(button => button.addEventListener('click', () => {
        history.pushState({}, '', pageUrl('/reports', project, {view: button.dataset.subtab, from: data.filters.from, to: data.filters.to, module: data.filters.module}));
        route();
      }));
      root.querySelector('[data-report-export]:not(:disabled)')?.addEventListener('click', () => {
        const query = new URLSearchParams({project, from: data.filters.from, to: data.filters.to});
        if (data.filters.module) query.set('module', data.filters.module);
        const url = `/api/reports/export?${query.toString()}`;
        showToast('正在导出测试报告 Excel…');
        startBrowserDownload(url, `test_report_${project}.xlsx`);
        showToast('✅ 测试报告下载已开始');
      });
    },
    destroy() {}
  };
}

function environmentSection() {
  const value = new URLSearchParams(location.search).get('section');
  return ['targets', 'llm', 'ones', 'updates'].includes(value) ? value : 'targets';
}

function environmentCheckRows(checks = []) {
  const defaults = [
    ['source', '源码与工作区'],
    ['config', '项目配置'],
    ['artifact', '执行产物'],
    ['command', '命令接口'],
    ['capture', '截图能力'],
    ['llm', '大模型服务']
  ];
  const normalized = checks.length ? checks : defaults.map(([key, label]) => ({key, label, status: 'unchecked', detail: '尚未执行环境检查'}));
  return `<div class='environment-check-list'>${normalized.map(item => {
    const status = String(item.status || 'unchecked').toLowerCase();
    const statusLabel = {pass: '通过', ready: '通过', warning: '警告', fail: '失败', error: '失败', unchecked: '尚未检查'}[status] || '尚未检查';
    const iconName = ['pass', 'ready'].includes(status) ? 'check' : 'warning';
    return `<div class='environment-check-row is-${escapeHtml(status)}'><i>${icon(iconName, 17)}</i><strong>${escapeHtml(item.label || item.key || '检查项')}</strong><span>${escapeHtml(statusLabel)}</span><p>${escapeHtml(item.detail || '—')}</p></div>`;
  }).join('')}</div>`;
}

function EnvironmentPage(project = currentProject()) {
  return {
    async load() {
      const [projects, config, environments, update] = await Promise.all([
        api('/api/tests/projects'),
        optionalApi('/api/config'),
        optionalApi('/api/environments'),
        optionalApi('/api/update-check')
      ]);
      return {projects, config, environments, update, section: environmentSection()};
    },
    render(data) {
      const profiles = data.projects.items || [];
      const profile = profiles.find(item => item.project === project) || testProject(project);
      const environmentItems = data.environments.data?.items || [];
      const environmentItem = environmentItems.find(item => item.id === project || item.project === project) || null;
      const targetHealth = environmentHealth(environmentItem);
      const cfg = data.config.data || {};
      const protocol = ENVIRONMENT_PROTOCOLS[project];
      const simConfig = profile.simulator_config || {};
      const sourceRoot = project === '6202_W5230' ? cfg.simulator?.hardware_source_root : project === '6202_W5230_SIMULATOR' ? (profile.simulator_source_root || simConfig.source_root) : cfg.simulator?.source_root;
      const workspaceRoot = project === '6202_W5230' ? cfg.simulator?.hardware_workspace_root : project === '6202_W5230_SIMULATOR' ? (profile.simulator_source_root || simConfig.source_root) : cfg.simulator?.workspace_root;
      const buildDirectory = profile.simulator_build_directory || simConfig.build_directory || '';
      const artifactPath = profile.simulator_artifact_path || simConfig.artifact_path || cfg.simulator?.simulator_path || '';
      const checks = environmentItem?.checks || [];
      const readyChecks = checks.filter(item => ['pass', 'ready'].includes(String(item.status || '').toLowerCase())).length;
      const totalChecks = checks.length || 6;
      let content;
      if (data.section === 'llm') {
        content = `<article class='workspace-panel settings-summary-panel'><header><div><h2>大模型服务</h2><p>密钥只显示配置状态，不在页面回显</p></div><button class='button' type='button' data-open-settings>打开系统设置</button></header><dl class='settings-summary-grid'><div><dt>API Key</dt><dd>${cfg.llm?.api_key ? '已配置' : '未配置'}</dd></div><div><dt>Base URL</dt><dd>${escapeHtml(cfg.llm?.base_url || '未配置')}</dd></div><div><dt>模型</dt><dd>${escapeHtml(cfg.llm?.model || '未配置')}</dd></div><div><dt>超时</dt><dd>${Number(cfg.llm?.timeout || 0) || '—'} 秒</dd></div></dl></article>`;
      } else if (data.section === 'ones') {
        content = `<article class='workspace-panel settings-summary-panel'><header><div><h2>ONES 平台</h2><p>缺陷拉取与闭环平台连接</p></div><button class='button' type='button' data-open-settings>打开系统设置</button></header><dl class='settings-summary-grid'><div><dt>平台地址</dt><dd>${escapeHtml(cfg.ones?.base_url || '未配置')}</dd></div><div><dt>访问令牌</dt><dd>${cfg.ones?.auth_token ? '已配置' : '未配置'}</dd></div><div><dt>团队 UUID</dt><dd>${cfg.ones?.team_uuid ? '已配置' : '未配置'}</dd></div><div><dt>用户 ID</dt><dd>${cfg.ones?.user_id ? '已配置' : '未配置'}</dd></div></dl></article>`;
      } else if (data.section === 'updates') {
        const update = data.update.data || {};
        content = `<article class='workspace-panel settings-summary-panel'><header><div><h2>系统更新</h2><p>便携版程序与前端资源</p></div>${update.update_available ? `<button class='button' type='button' data-open-update>查看新版本</button>` : ''}</header><dl class='settings-summary-grid'><div><dt>当前版本</dt><dd>${escapeHtml(update.current_version || document.querySelector('#brand-system-version')?.textContent || '—')}</dd></div><div><dt>最新版本</dt><dd>${escapeHtml(update.latest_version || '—')}</dd></div><div><dt>更新状态</dt><dd>${data.update.data ? (update.update_available ? '发现新版本' : '当前已是最新') : '检查接口不可用'}</dd></div></dl></article>`;
      } else {
        const canManage = Boolean(data.environments.data);
        content = `<section class='target-health-grid environment-page-targets'>${profiles.map(item => renderEnvironmentTargetCard(item, environmentItems.find(env => env.id === item.project || env.project === item.project), project)).join('')}</section><section class='environment-main-grid'><article class='workspace-panel environment-config-panel'><header><div><h2>当前测试目标</h2><p>${escapeHtml(profile.project_label || project)} · ${escapeHtml(profile.execution_target_label || '')}</p></div>${Components.statusChip(targetHealth.status === 'ready' ? 'PASS' : targetHealth.status === 'error' ? 'ERROR' : 'PENDING', targetHealth.label)}</header><form id='environment-config-form'><label><span>命令通道</span><input type='text' value='${escapeHtml(protocol?.transportLabel || profile.transport || '未知')}' readonly></label><label><span>截图方式</span><input type='text' value='${escapeHtml(protocol?.captureLabel || profile.capture_provider || '未知')}' readonly></label>${project === '6202_W5230' ? `<label><span>SuperCom 管道</span><input type='text' value='${escapeHtml(profile.pipe_name || `\\\\.\\pipe\\SuperCom.AgentBridge.${cfg.hardware?.port || 'COM端口'}`)}' readonly></label>` : ''}<label><span>源码根目录</span><input name='source_root' type='text' value='${escapeHtml(sourceRoot || '')}' placeholder='尚未配置' ${canManage ? '' : 'readonly'}></label><label><span>工作区目录</span><input name='workspace_root' type='text' value='${escapeHtml(workspaceRoot || '')}' placeholder='尚未配置' ${canManage ? '' : 'readonly'}></label>${profile.execution_target === 'simulator' ? `<label><span>构建目录</span><input name='build_directory' type='text' value='${escapeHtml(buildDirectory)}' placeholder='尚未配置' ${canManage ? '' : 'readonly'}></label><label><span>模拟器产物</span><input name='artifact_path' type='text' value='${escapeHtml(artifactPath)}' placeholder='尚未配置' ${canManage ? '' : 'readonly'}></label>` : ''}<div class='environment-form-actions'><button class='button button-secondary' type='reset'>恢复当前值</button><button class='button' type='submit' ${canManage ? '' : 'disabled title="当前后端未提供环境配置接口"'}>保存配置</button></div></form>${!canManage ? Components.unavailableState('环境配置接口暂不可用', '当前后端未提供 /api/environments；这里以只读方式展示现有项目清单和配置。') : ''}</article><article class='workspace-panel environment-check-panel'><header><div><h2>环境检查</h2><p>没有真实检查结果时保持“尚未检查”</p></div><span class='readiness-score'>就绪评分 <strong>${checks.length ? `${readyChecks} / ${totalChecks}` : '—'}</strong></span></header>${environmentCheckRows(checks)}<div class='environment-form-actions'><button class='button' type='button' data-environment-check ${canManage ? '' : 'disabled title="当前后端未提供环境检查接口"'}>${icon('refresh', 16)} 立即检查</button></div></article></section><article class='workspace-panel environment-log-panel'><header><div><h2>检测日志</h2><p>仅显示后端真实返回的检查记录</p></div></header>${environmentItem?.logs?.length ? `<ol class='environment-log'>${environmentItem.logs.map(item => `<li><time>${formatTime(item.at || item.timestamp)}</time><span>${escapeHtml(item.message || '')}</span></li>`).join('')}</ol>` : Components.emptyState('尚无检测日志', '执行环境检查后，详细过程会显示在这里。')}</article>`;
      }
      return `${Components.pageHeader({title: '环境中心', intro: '统一管理模拟器、真机、模型服务和外部平台连接'})}${Components.subTabs([{value: 'targets', label: '测试目标'}, {value: 'llm', label: '大模型'}, {value: 'ones', label: 'ONES'}, {value: 'updates', label: '系统更新'}], data.section)}${content}`;
    },
    mount(root, data) {
      rememberProject(project);
      const currentEnvironment = data.environments.data?.items?.find(item => item.id === project || item.project === project);
      const health = environmentHealth(currentEnvironment);
      setGlobalTargetHealth(health.status, health.label);
      root.querySelectorAll('[data-subtab]').forEach(button => button.addEventListener('click', () => {
        history.pushState({}, '', pageUrl('/environments', project, {section: button.dataset.subtab}));
        route();
      }));
      root.querySelectorAll('[data-open-settings]').forEach(button => button.addEventListener('click', () => document.querySelector('#open-system-settings')?.click()));
      root.querySelector('[data-open-update]')?.addEventListener('click', () => document.querySelector('#update-badge')?.click());
      const form = root.querySelector('#environment-config-form');
      form?.addEventListener('submit', async event => {
        event.preventDefault();
        const button = form.querySelector('[type="submit"]');
        button.disabled = true;
        try {
          const values = Object.fromEntries(new FormData(form).entries());
          await api(`/api/environments/${encodeURIComponent(project)}`, {method: 'PUT', body: JSON.stringify({paths: values})});
          showToast('环境配置已保存', 'success');
          route();
        } catch (error) { button.disabled = false; showToast(error.message, 'error'); }
      });
      root.querySelector('[data-environment-check]')?.addEventListener('click', async event => {
        event.currentTarget.disabled = true;
        try { await api(`/api/environments/${encodeURIComponent(project)}/check`, {method: 'POST', body: '{}'}); showToast('环境检查已启动'); route(); } catch (error) { event.currentTarget.disabled = false; showToast(error.message, 'error'); }
      });
    },
    destroy() {}
  };
}

async function route() {
  try {
    routeRequestToken += 1;
    destroyActivePageController();
    stopImportPolling();
    stopRepairRestore();
    stopTestPolling();
    stopBatchTestPolling();
    app.onclick = null;
    initGlobalTargetSwitcher();
    let parts = location.pathname.split('/').filter(Boolean).map(decodeURIComponent);
    parts = restoreCompatibleTopLevelRoute(parts);
    const project = rememberProject(currentProject());
    if (!parts.length) {
      history.replaceState({}, '', pageUrl('/overview', project));
      setActiveNav('overview');
      document.title = '项目总览 · Agent-loop';
      return await mountPageController(OverviewPage(project));
    }
    if (parts[0] === 'overview' && parts.length === 1) {
      setActiveNav('overview');
      document.title = '项目总览 · Agent-loop';
      return await mountPageController(OverviewPage(project));
    }
    if (parts[0] === 'tests' && parts.length === 1) {
      history.replaceState({}, '', `/cases${location.search}`);
      return await renderTests();
    }
    if (parts[0] === 'cases' && parts.length === 1) return await renderTests();
    if (parts[0] === 'runs' && parts.length === 1) {
      setActiveNav('runs');
      document.title = '自动化执行 · Agent-loop';
      return await mountPageController(ExecutionPage(project));
    }
    if (parts[0] === 'reports' && parts.length === 1) {
      setActiveNav('reports');
      document.title = '测试报告 · Agent-loop';
      return await mountPageController(ReportsPage(project));
    }
    if (parts[0] === 'defects' && parts.length === 1) return await renderList();
    if (parts[0] === 'environments' && parts.length === 1) {
      setActiveNav('environments');
      document.title = '环境中心 · Agent-loop';
      return await mountPageController(EnvironmentPage(project));
    }
    if (parts[0] === 'test' && parts[1] && parts[2] && parts.length === 3) return await renderTest(parts[1], parts[2]);
    if (parts[0] === 'test-batch' && parts[1] && parts.length === 2) return await renderTestBatch(parts[1]);
    if (parts[0] === 'test-history' && parts[1] && parts[2] && parts[3] && parts.length === 4) return await renderTestHistory(parts[1], parts[2], parts[3]);
    if (parts[0] === 'defect' && parts[1] && parts.length === 2) return await renderDefect(parts[1]);
    if (parts[0] === 'history' && parts[1] && parts[2] && parts.length === 3) return await renderHistory(parts[1], parts[2]);
    throw new Error('页面不存在');
  } catch (error) {
    renderError(error);
  }
}

initSystemSettings();
initGlobalTargetSwitcher();
initPrimaryNavigation();
window.addEventListener('popstate', route);
route();


  // ==========================================
  // 自动化更新与一键升级
  // ==========================================
  const updateBadge = document.querySelector('#update-badge');
  const updateDialog = document.querySelector('#update-dialog');
  const closeUpdateDialogBtn = document.querySelector('#close-update-dialog');
  const cancelUpdateBtn = document.querySelector('#cancel-update-btn');
  const btnStartUpgrade = document.querySelector('#btn-start-upgrade');
  const updateTargetVersion = document.querySelector('#update-target-version');
  const updateCurrentVersion = document.querySelector('#update-current-version');
  const updateChangelogList = document.querySelector('#update-changelog-list');
  const upgradeOverlay = document.querySelector('#upgrade-overlay');
  const upgradeOverlayStatus = document.querySelector('#upgrade-overlay-status');

  let latestUpdateInfo = null;

  async function checkSystemUpdateSilently() {
    try {
      const resp = await fetch('/api/update-check');
      if (!resp.ok) return;
      const data = await resp.json();
        const brandSystemVersion = document.querySelector('#brand-system-version');
  if (brandSystemVersion && data && data.current_version) {
    brandSystemVersion.textContent = `v${data.current_version}`;
  }
      if (data && data.has_update) {
        latestUpdateInfo = data;
        if (updateBadge) {
          updateBadge.textContent = `✨ 发现新版本 v${data.latest_version}`;
          updateBadge.style.display = 'inline-block';
        }
      }
    } catch (_) {}
  }

  function openUpdateModal() {
    if (!latestUpdateInfo || !updateDialog) return;
    if (updateTargetVersion) updateTargetVersion.textContent = `v${latestUpdateInfo.latest_version}`;
    if (updateCurrentVersion) updateCurrentVersion.textContent = `v${latestUpdateInfo.current_version}`;
    if (updateChangelogList) {
      updateChangelogList.innerHTML = '';
      const logs = latestUpdateInfo.changelog || [];
      if (logs.length === 0) {
        const li = document.createElement('li');
        li.textContent = '性能优化与体验改进';
        updateChangelogList.appendChild(li);
      } else {
        logs.forEach(log => {
          const li = document.createElement('li');
          li.textContent = log;
          updateChangelogList.appendChild(li);
        });
      }
    }
    updateDialog.showModal();
  }

  if (updateBadge) updateBadge.addEventListener('click', openUpdateModal);
  if (closeUpdateDialogBtn) closeUpdateDialogBtn.addEventListener('click', () => updateDialog.close());
  if (cancelUpdateBtn) cancelUpdateBtn.addEventListener('click', () => updateDialog.close());

  if (btnStartUpgrade) {
    btnStartUpgrade.addEventListener('click', async () => {
      if (updateDialog) updateDialog.close();
      if (upgradeOverlay) upgradeOverlay.style.display = 'flex';
      if (upgradeOverlayStatus) upgradeOverlayStatus.textContent = '正在从 NAS 拉取最新安装包并校验...';

      try {
        const resp = await fetch('/api/system/upgrade', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({})
        });
        const data = await resp.json();
        if (!resp.ok) {
          throw new Error(data?.error || `HTTP ${resp.status}`);
        }
        if (upgradeOverlayStatus) {
          upgradeOverlayStatus.textContent = '正在热替换系统程序并重启工作台，请稍候...';
        }

        // 3 秒缓冲等待旧进程退出与文件覆盖
        await new Promise(r => setTimeout(r, 3000));

        let attempts = 0;
        const maxAttempts = 30;
        const pollTimer = setInterval(async () => {
          attempts++;
          try {
            const checkResp = await fetch('/api/config', { cache: 'no-store' });
            if (checkResp.ok) {
              clearInterval(pollTimer);
              if (upgradeOverlayStatus) upgradeOverlayStatus.textContent = '✅ 升级完成！正在刷新工作台...';
              setTimeout(() => {
                window.location.reload();
              }, 1200);
            }
          } catch (_) {
            if (attempts >= maxAttempts) {
              clearInterval(pollTimer);
              if (upgradeOverlayStatus) {
                upgradeOverlayStatus.innerHTML = '<span style="font-size: 14px; margin-bottom: 6px;">已完成文件替换与升级。请点击下方按钮进入新版本：</span><button onclick="window.location.reload()" class="button button-primary" style="background:#4f46e5;color:white;padding:10px 24px;cursor:pointer;border-radius:6px;font-size:14px;font-weight:600;display:inline-flex;align-items:center;justify-content:center;box-shadow:0 4px 12px rgba(79,70,229,0.3);margin:8px auto 0 auto;border:none;">🔄 立即进入工作台</button>';
              }
            }
          }
        }, 1500);

      } catch (err) {
        if (upgradeOverlay) upgradeOverlay.style.display = 'none';
        showToast(`❌ 自动升级失败: ${err.message}`);
      }
    });
  }

  setTimeout(checkSystemUpdateSilently, 1000);
