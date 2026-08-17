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
  solidified: '已固化、Agent-loop 可执行',
  untested: '尚未运行',
  fail: '最近失败',
  cannot_verify: '最近无法验证',
  error: '最近执行异常',
  pass: '最近通过'
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

function showToast(message, type = '') {
  clearTimeout(toastTimer);
  toast.textContent = message;
  toast.className = `toast show ${type}`;
  toastTimer = setTimeout(() => { toast.className = 'toast'; }, 3600);
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
  if (query.trim()) params.set('q', query.trim());
  if (resultFilter !== 'all') params.set('result', resultFilter);
  if (page > 1) params.set('page', String(page));
  const url = params.size ? `/?${params.toString()}` : '/';
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
  return items.map(row => `
    <a class="defect-row" href="/defect/${encodeURIComponent(row.number)}">
      <span class="defect-number">#${escapeHtml(row.number)}</span>
      <span class="defect-copy">
        <strong>${escapeHtml(row.title)}</strong>
        <small>${row.history_count ? `${row.history_count} 次运行 · ${formatTime(row.last_run_at)}` : '尚无运行记录'}</small>
      </span>
      <span class="chip chip-status">${escapeHtml(row.status || '未知')}</span>
      ${resultChip(row.repair_result)}
      <span class="row-arrow" aria-hidden="true">›</span>
    </a>`).join('');
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

async function renderList() {
  setActiveNav('defects');
  document.title = '缺陷队列 · 固件修复工作台';
  const initial = listParams();
  app.innerHTML = `
    <header class="page-header queue-heading">
      <div>
        <p class="eyebrow">本地修复工作台</p>
        <h1 class="page-title">缺陷队列</h1>
        <p class="page-intro">查找缺陷，启动自动修复，并复查每次构建与验证结果。</p>
      </div>
    </header>
    <section class="queue-toolbar" aria-label="缺陷队列工具">
      <div id="search-stage" class="search-stage">
        <label for="defect-search">搜索缺陷</label>
        <div class="search-box">
          <span class="search-icon" aria-hidden="true"></span>
          <input id="defect-search" type="search" value="${escapeHtml(initial.query)}" placeholder="输入编号、标题、描述或状态" autocomplete="off">
          <button id="clear-search" class="clear-search" type="button" aria-label="清空搜索">清空</button>
        </div>
      </div>
      <button id="open-import" class="button import-open-button" type="button">拉取缺陷</button>
    </section>
    <section id="import-progress" class="import-progress" aria-live="polite" hidden></section>
    <section id="metrics" class="metrics" aria-label="缺陷统计与筛选">${metricCards({}, initial.result)}</section>
    <section class="panel queue-panel">
      <header class="panel-head">
        <div><h2 id="queue-title">${RESULT_FILTER_LABELS[initial.result]}</h2><p>按缺陷编号倒序排列</p></div>
        <span id="queue-count" class="chip chip-pending">正在读取</span>
      </header>
      <div id="defect-list" class="defect-list"><div class="list-loading">正在读取缺陷…</div></div>
      <div id="pagination"></div>
    </section>
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
}

function testListParams() {
  const params = new URLSearchParams(location.search);
  const rawPage = Number.parseInt(params.get('page') || '1', 10);
  const state = String(params.get('state') || DEFAULT_TEST_STATE).toLowerCase();
  const project = String(params.get('project') || DEFAULT_TEST_PROJECT);
  return {
    query: (params.get('q') || '').trim(),
    page: Number.isFinite(rawPage) && rawPage > 0 ? rawPage : 1,
    state: Object.hasOwn(TEST_FILTER_LABELS, state) ? state : DEFAULT_TEST_STATE,
    project: Object.hasOwn(TEST_PROJECTS, project) ? project : DEFAULT_TEST_PROJECT
  };
}

function buildTestListUrl(query, page, state = DEFAULT_TEST_STATE, project = DEFAULT_TEST_PROJECT) {
  const params = new URLSearchParams();
  params.set('project', testProject(project).project);
  if (query.trim()) params.set('q', query.trim());
  if (state !== DEFAULT_TEST_STATE) params.set('state', state);
  if (page > 1) params.set('page', String(page));
  return params.size ? `/tests?${params.toString()}` : '/tests';
}

function setTestListUrl(query, page, state = DEFAULT_TEST_STATE, project = DEFAULT_TEST_PROJECT, mode = 'replace') {
  const url = buildTestListUrl(query, page, state, project);
  history[mode === 'push' ? 'pushState' : 'replaceState']({}, '', url);
}

function testReturnUrl() {
  const fallbackProject = testListParams().project;
  const fallback = buildTestListUrl('', 1, DEFAULT_TEST_STATE, fallbackProject);
  const raw = new URLSearchParams(location.search).get('from');
  if (!raw) return fallback;
  try {
    const target = new URL(raw, location.origin);
    if (target.origin !== location.origin || target.pathname !== '/tests') return fallback;
    const params = new URLSearchParams(target.search);
    const rawPage = Number.parseInt(params.get('page') || '1', 10);
    const page = Number.isFinite(rawPage) && rawPage > 0 ? rawPage : 1;
    const rawState = String(params.get('state') || DEFAULT_TEST_STATE).toLowerCase();
    const state = Object.hasOwn(TEST_FILTER_LABELS, rawState) ? rawState : DEFAULT_TEST_STATE;
    const project = String(params.get('project') || DEFAULT_TEST_PROJECT);
    return buildTestListUrl(
      (params.get('q') || '').trim(), page, state,
      Object.hasOwn(TEST_PROJECTS, project) ? project : DEFAULT_TEST_PROJECT
    );
  } catch {
    return fallback;
  }
}

function withTestReturn(path, project = DEFAULT_TEST_PROJECT, returnTo = '/tests') {
  const params = new URLSearchParams({project: testProject(project).project, from: returnTo});
  return `${path}?${params.toString()}`;
}

function testDetailHref(project, sheet, caseId, returnTo = '/tests') {
  return withTestReturn(`/test/${encodeURIComponent(sheet)}/${encodeURIComponent(caseId)}`, project, returnTo);
}

function testHistoryHref(project, sheet, caseId, runId, returnTo = '/tests') {
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
  const runCards = [
    ['untested', 'untested', '尚未运行'],
    ['pass', 'pass', '最近通过'],
    ['fail', 'fail', '最近失败'],
    ['cannot_verify', 'cannot_verify', '最近无法验证'],
    ['error', 'error', '执行异常']
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
    </section>
    <section class="test-metric-group" aria-labelledby="run-status-title">
      <header class="test-metric-heading">
        <div><strong id="run-status-title">最近一次 Agent-loop 结果</strong><span>运行结果不改变外部探索或固化状态</span></div>
        <small>全部用例都可运行</small>
      </header>
      <div class="metrics test-metric-grid run-metrics">${renderCards(runCards)}</div>
    </section>`;
}

function testRows(items, query, state, returnTo = '/tests') {
  if (!items.length) {
    return `<div class="empty-state compact">
      <strong>${query ? '没有找到匹配的测试用例' : `没有${TEST_FILTER_LABELS[state]}的用例`}</strong>
      <p>${query ? '可以减少关键词，或清除当前搜索条件。' : '可以选择其他分类查看用例。'}</p>
      ${query ? '<button class="button button-secondary" type="button" data-clear-test-search>清除搜索条件</button>' : ''}
    </div>`;
  }
  return items.map(row => {
    const sheet = String(row.file_sheet || row.sheet || '');
    const caseId = String(row.case_id || '');
    const selected = selectedTestCases.has(testCaseSelectionKey(sheet, caseId));
    const historyText = row.history_count
      ? `${row.history_count} 次运行 · ${formatTime(row.last_run_at)}`
      : 'Agent-loop 尚未运行';
    return `
      <div class="test-row-shell${selected ? ' is-selected' : ''}">
        <label class="test-case-selector" title="${escapeHtml(`选择 ${caseId} 创建精确批次`)}">
          <input type="checkbox" data-test-case-select data-sheet="${escapeHtml(sheet)}" data-case-id="${escapeHtml(caseId)}" aria-label="选择用例 ${escapeHtml(caseId)}" ${selected ? 'checked' : ''}>
        </label>
        <a class="defect-row test-row" href="${escapeHtml(testDetailHref(row.project, sheet, caseId, returnTo))}">
          <span class="defect-number">${escapeHtml(caseId)}</span>
          <span class="defect-copy">
            <strong>${escapeHtml(row.steps_text || row.expected_text || '未填写测试步骤')}</strong>
            <small>${escapeHtml(row.sheet)} · ${historyText}</small>
          </span>
          <span class="chip chip-status">${escapeHtml(row.priority || '未分级')}</span>
          ${maturityChip(row)}
          ${resultChip(row.latest_verdict)}
          <span class="row-arrow" aria-hidden="true">›</span>
        </a>
      </div>`;
  }).join('');
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

async function refreshTests(query, page, {
  urlMode = 'replace',
  state = testListParams().state,
  project = testListParams().project
} = {}) {
  const token = ++testListRequestToken;
  const list = document.querySelector('#test-list');
  const pager = document.querySelector('#test-pagination');
  const count = document.querySelector('#test-count');
  if (!list || !pager || !count) return;
  list.classList.add('is-loading');
  try {
    const payload = await api(`/api/tests?project=${encodeURIComponent(project)}&q=${encodeURIComponent(query)}&state=${encodeURIComponent(state)}&page=${page}&page_size=${PAGE_SIZE}`);
    if (token !== testListRequestToken) return;
    ensureTestSelectionProject(payload.project);
    const projectTarget = document.querySelector('#test-project-target');
    if (projectTarget) projectTarget.innerHTML = testTargetChip(payload);
    currentTestPageItems = payload.items || [];
    const selected = Object.hasOwn(TEST_FILTER_LABELS, payload.state_filter) ? payload.state_filter : DEFAULT_TEST_STATE;
    document.querySelector('#test-metrics').innerHTML = testMetricCards(payload.summary, selected);
    document.querySelector('#test-queue-title').textContent = TEST_FILTER_LABELS[selected];
    updateBatchLaunch(payload.summary);
    const returnTo = buildTestListUrl(query, payload.page || 1, selected, payload.project);
    list.innerHTML = testRows(currentTestPageItems, query, selected, returnTo);
    updateSelectedTestCasesUi();
    pager.innerHTML = pagination(payload);
    count.textContent = query ? `找到 ${payload.total} 条` : `共 ${payload.total} 条`;
    setTestListUrl(query, payload.page || 1, selected, payload.project, urlMode);
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
  setActiveNav('tests');
  document.title = 'Agent 测试 · 固件修复工作台';
  const initial = testListParams();
  const initialProject = testProject(initial.project);
  ensureTestSelectionProject(initial.project);
  app.innerHTML = `
    <header class="page-header queue-heading">
      <div>
        <p class="eyebrow">本地测试工作台</p>
        <h1 class="page-title">Agent 测试</h1>
        <p class="page-intro">全部用例都可以运行；有固化步骤时固定执行，没有固化步骤时由 Agent-loop 临时探索。</p>
      </div>
      <div class="batch-launch">
        <div class="batch-launch-heading">
          <strong>按运行状态自动选例</strong>
          <small>勾选分类后创建一个批次</small>
        </div>
        <div class="batch-scope" aria-label="选择批次范围">
          ${Object.entries(BATCH_CATEGORY_LABELS).map(([value, label]) => `
            <label class="batch-option">
              <input type="checkbox" name="batch-category" value="${value}" checked>
              <span>${label}</span>
              <strong data-batch-count="${value}">0</strong>
            </label>`).join('')}
        </div>
        <div class="batch-launch-actions">
          <small id="batch-selection-note">正在统计历史结果…</small>
          <button id="test-batch-button" class="button" type="button">读取候选数量…</button>
        </div>
      </div>
    </header>
    <section id="active-test-batch" class="batch-active" hidden></section>
    <section class="queue-toolbar test-toolbar" aria-label="测试用例工具">
      <div class="project-stage">
        <label for="test-project">项目</label>
        <select id="test-project" aria-label="选择测试项目">
          ${Object.values(TEST_PROJECTS).map(item => `<option value="${escapeHtml(item.project)}" ${item.project === initial.project ? 'selected' : ''}>${escapeHtml(item.projectLabel)} · ${escapeHtml(item.targetLabel)}</option>`).join('')}
        </select>
        <small id="test-project-target">${testTargetChip(initialProject)}</small>
      </div>
      <div class="search-stage">
        <label for="test-search">搜索测试用例</label>
        <div class="search-box">
          <span class="search-icon" aria-hidden="true"></span>
          <input id="test-search" type="search" value="${escapeHtml(initial.query)}" placeholder="输入用例编号、模块、步骤或预期结果" autocomplete="off">
          <button id="clear-test-search" class="clear-search" type="button" aria-label="清空搜索">清空</button>
        </div>
      </div>
    </section>
    <section class="test-selection-bar" aria-label="精确选择测试批次">
      <div class="test-selection-copy">
        <strong>手动勾选用例</strong>
        <span id="selected-test-summary">尚未勾选用例；勾选项可跨分页保留</span>
      </div>
      <div class="test-selection-actions">
        <label class="test-page-selector">
          <input id="select-test-page" type="checkbox">
          <span>全选当前页用例</span>
        </label>
        <button id="clear-selected-tests" class="button button-secondary" type="button" disabled>清空</button>
        <button id="run-selected-tests" class="button" type="button" disabled>用已选用例创建批次</button>
      </div>
    </section>
    <section id="test-metrics" class="test-metric-groups" aria-label="测试用例统计与筛选">${testMetricCards({}, initial.state)}</section>
    <section class="panel queue-panel">
      <header class="panel-head">
        <div><h2 id="test-queue-title">${TEST_FILTER_LABELS[initial.state]}</h2><p>按模块和用例编号排列，结果取最近一次 Agent-loop 运行记录</p></div>
        <span id="test-count" class="chip chip-pending">正在读取</span>
      </header>
      <div id="test-list" class="defect-list"><div class="list-loading">正在读取测试用例…</div></div>
      <div id="test-pagination"></div>
    </section>`;

  const input = document.querySelector('#test-search');
  const projectSelect = document.querySelector('#test-project');
  const list = document.querySelector('#test-list');
  let debounceTimer = null;
  input.addEventListener('input', () => {
    clearTimeout(debounceTimer);
    testListRequestToken += 1;
    const query = input.value.trim();
    debounceTimer = setTimeout(() => refreshTests(query, 1, {
      state: testListParams().state, project: projectSelect.value
    }), 260);
  });
  projectSelect.addEventListener('change', () => {
    const state = testListParams().state;
    ensureTestSelectionProject(projectSelect.value);
    updateSelectedTestCasesUi();
    setTestListUrl(input.value.trim(), 1, state, projectSelect.value, 'push');
    refreshTests(input.value.trim(), 1, {state, project: projectSelect.value});
  });
  document.querySelector('#clear-test-search').addEventListener('click', () => {
    clearTimeout(debounceTimer);
    input.value = '';
    input.focus();
    refreshTests('', 1, {state: testListParams().state, project: projectSelect.value});
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
      const summary = await api(`/api/tests?project=${encodeURIComponent(projectSelect.value)}&page=1&page_size=1`);
      updateBatchLaunch(summary.summary);
    }
  });
  app.onclick = event => {
    const stateButton = event.target.closest('[data-test-filter]');
    if (stateButton) {
      const state = stateButton.dataset.testFilter;
      setTestListUrl(input.value.trim(), 1, state, projectSelect.value, 'push');
      refreshTests(input.value.trim(), 1, {state, project: projectSelect.value});
      return;
    }
    const pageButton = event.target.closest('[data-page]');
    if (pageButton && !pageButton.disabled) {
      const page = Number(pageButton.dataset.page);
      const state = testListParams().state;
      setTestListUrl(input.value.trim(), page, state, projectSelect.value, 'push');
      refreshTests(input.value.trim(), page, {urlMode: 'replace', state, project: projectSelect.value});
      window.scrollTo({top: 0, behavior: 'smooth'});
      return;
    }
    if (event.target.closest('[data-clear-test-search]')) {
      input.value = '';
      refreshTests('', 1, {state: testListParams().state, project: projectSelect.value});
      input.focus();
      return;
    }
    if (event.target.closest('[data-retry-tests]')) {
      const params = testListParams();
      refreshTests(input.value.trim(), params.page, {state: params.state, project: params.project});
    }
  };
  await refreshTests(initial.query, initial.page, {state: initial.state, project: initial.project});
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
      <span><strong>${escapeHtml(item.reason || '未记录判定理由')}</strong><small>${item.has_screenshot ? `${item.screenshot_count || 1} 张${screenshotLabel(item)}` : '无截图'} · ${escapeHtml(item.execution_target_label || testProject(project).targetLabel)}</small></span>
      <time>${formatTime(item.timestamp)}</time>
      <span class="row-arrow" aria-hidden="true">›</span>
    </a>`).join('')}</div>`;
}

async function renderTest(sheet, caseId) {
  setActiveNav('tests');
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
        <div class="meta-line">${testTargetChip(testCase)}${maturityChip(testCase)}<span class="chip chip-status">${escapeHtml(testCase.priority || '未分级')}</span>${resultChip(initialVerdict)}<span class="muted small">${testCase.history?.length || 0} 次历史运行</span></div>
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
            ${usesFixedMapping ? `${commandPhase('准备环境', 'setup', testCase.setup)}${commandPhase('执行操作', 'actions', testCase.actions)}${commandPhase('采集证据', 'collect', testCase.collect)}` : '<div class="notice"><strong>本条将临时探索</strong><p>探索命令和截图只写入本次运行历史，不会写入外部探索账本，也不会自动固化到 case_map。</p></div>'}
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
      pollTestJob(job.id, project, sheet, caseId);
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

async function pollTestJob(jobId, project, sheet, caseId) {
  const chip = document.querySelector('#test-job-chip');
  const message = document.querySelector('#test-job-message');
  const button = document.querySelector('#test-run-button');
  if (!chip || !message || !button) return;
  try {
    const job = await api(`/api/tests/jobs/${encodeURIComponent(jobId)}`);
    updateTestWorkflow(job.nodes);
    if (['queued', 'running', 'finalizing'].includes(job.status)) {
      button.disabled = true;
      button.textContent = job.status === 'finalizing' ? '保存记录中…' : '正在测试…';
      chip.className = 'chip chip-running';
      chip.textContent = job.status === 'finalizing' ? '保存记录中' : (TEST_NODE_LABELS[job.current_node] || '任务运行中');
      message.textContent = job.status === 'finalizing'
        ? `任务 ${job.id} 已结束，正在保存测试历史…`
        : `任务 ${job.id} · ${TEST_NODE_LABELS[job.current_node] || '正在执行'}`;
      testPollTimer = setTimeout(() => pollTestJob(jobId, project, sheet, caseId), 2000);
      return;
    }
    setResultChip(chip, job.verdict);
    message.innerHTML = job.history_id
      ? `任务 ${escapeHtml(job.id)} 已结束。<a href="${escapeHtml(testHistoryHref(project, sheet, caseId, job.history_id, testReturnUrl()))}">查看本次测试证据 →</a>`
      : `任务结束，但测试记录保存失败：${escapeHtml(job.error || '未知原因')}`;
    button.disabled = false;
    button.textContent = '再次启动测试';
    const detail = await api(`/api/tests/${encodeURIComponent(sheet)}/${encodeURIComponent(caseId)}?project=${encodeURIComponent(project)}`);
    document.querySelector('#test-history-body').innerHTML = testHistoryRows(detail.history, project, sheet, caseId, testReturnUrl());
    if (job.verdict === 'PASS') showToast('Agent 测试已通过');
    else if (job.verdict === 'CANNOT_VERIFY') showToast('测试无法验证，请查看判定理由和证据', 'warning');
    else showToast(job.verdict === 'ERROR' ? '测试执行出错' : 'Agent 测试未通过', 'error');
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
    const content = `${resultChip(item.verdict)}<span><strong>${escapeHtml(item.case_id)}</strong><small>${escapeHtml(item.sheet)} · ${escapeHtml(item.reason || '未记录判定理由')}</small></span><time>${formatTime(item.finished_at)}</time>`;
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
  setActiveNav('tests');
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
  document.title = `缺陷 #${defect.number} · 固件修复工作台`;
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
    ? reasons.map(reason => `<p>${escapeHtml(reason)}</p>`).join('')
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
  document.title = `修复记录 · 缺陷 #${number} · 固件修复工作台`;
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
    <div class="notice notice-error"><strong>${label}</strong>${values.map(value => `<p>${escapeHtml(value)}</p>`).join('')}</div>`).join('');
}

async function renderTestHistory(sheet, caseId, runId) {
  setActiveNav('tests');
  const project = testListParams().project;
  const returnTo = testReturnUrl();
  const record = await api(`/api/test-history/${encodeURIComponent(sheet)}/${encodeURIComponent(caseId)}/${encodeURIComponent(runId)}?project=${encodeURIComponent(project)}`);
  document.title = `测试记录 · ${caseId} · Agent 测试`;
  app.innerHTML = `
    <a class="back-link" href="${escapeHtml(testDetailHref(project, sheet, caseId, returnTo))}">← 返回测试详情</a>
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
      <div class="panel-body verdict-reasons"><p>${escapeHtml(record.reason || '未记录判定理由')}</p></div>
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

async function route() {
  try {
    stopImportPolling();
    stopRepairRestore();
    stopTestPolling();
    stopBatchTestPolling();
    app.onclick = null;
    const parts = location.pathname.split('/').filter(Boolean).map(decodeURIComponent);
    if (!parts.length) return await renderList();
    if (parts[0] === 'tests' && parts.length === 1) return await renderTests();
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

window.addEventListener('popstate', route);
route();
