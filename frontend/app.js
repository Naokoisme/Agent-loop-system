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
let selectedRunPlatform = '';
let selectedRunTarget = '';
let executionOptionsToken = 0;

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
  unexplored: '待生成步骤',
  externally_explored: '步骤待确认',
  explored_unsolidified: '步骤待确认',
  solidified: '可直接运行'
};
let DEFAULT_TEST_PROJECT = '';
const DEFAULT_TEST_STATE = 'all';
let TEST_PROJECTS = Object.create(null);
let PLATFORM_PROFILES = Object.create(null);
let PLATFORM_TARGETS = Object.create(null);
let ENVIRONMENT_PROTOCOLS = Object.create(null);

/** @typedef {string} ProjectKey */
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
const TOP_LEVEL_ROUTE_PATHS = Object.freeze(['/overview', '/cases', '/runs', '/reports', '/defects', '/environments', '/bluetooth']);
const caseCatalogCache = new Map();
let activePageController = null;
let routeRequestToken = 0;

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
  load: '准备测试',
  reset: '准备设备',
  prepare: '准备设备',
  execute: '执行操作',
  judge: '检查结果',
  record: '保存结果'
};

function escapeHtml(value) {
  return String(value ?? '')
    .replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;').replaceAll("'", '&#039;');
}

function imagePreviewLinkAttributes(url, label) {
  const displayLabel = String(label || '图片预览');
  return `class="image-preview-trigger" href="${escapeHtml(url)}" data-image-preview data-image-preview-label="${escapeHtml(displayLabel)}" aria-label="${escapeHtml(`放大查看：${displayLabel}`)}"`;
}

const UNKNOWN_ISSUE = Object.freeze({
  cause: '本次操作没有完成，平台暂时无法确定具体原因。',
  action: '请重新操作；如再次出现，展开诊断信息并联系维护人员。'
});

const TECHNICAL_DIAGNOSTIC_PATTERN = /(?:Traceback|\b[A-Za-z_$][\w$]*(?:Error|Exception)\b|\b[A-Za-z]:[\\/]|\\\\[^\\\r\n]+\\|\/(?:Users|home|var|tmp|opt|etc)\/|https?:\/\/|\b(?:UART|MTP|PnP|GUI_PING|gui_ack)\b|\b(?:VID|PID)[_/:=-]?[0-9A-F]+\b|\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+\b|\b[\w.-]+\.(?:json|log|txt|py|js|exe|zip)\b)/i;

function safeProductCopy(value) {
  const text = String(value ?? '').trim();
  if (!text || TECHNICAL_DIAGNOSTIC_PATTERN.test(text)) return '';
  return text;
}

function friendlyAgentError(value) {
  const text = String(value ?? '').trim();
  if (!text) return text;
  if (/执行 Agent API 出错|交互式复现异常\s*[:：].*(APIConnectionError|LLMRetryError)/.test(text)) {
    return 'AI 服务暂时无法连接，请检查设置后重试。';
  }
  if (/识图 Agent API 出错|LLM 重试耗尽.*(APIConnectionError|LLMRetryError)/.test(text)) {
    return '图像判定服务暂时无法连接，请稍后重试。';
  }
  return safeProductCopy(text);
}

const ISSUE_TABLE = Object.freeze({
  NETWORK_ERROR: {
    cause: '平台暂时无法连接本机服务。',
    action: '请确认平台仍在运行后重试；如已停止，请重新启动平台。'
  },
  USER_CANCELLED: {
    cause: '任务已取消。',
    action: '如需继续，请重新启动任务。'
  },
  SERVICE_RESTART: {
    cause: '服务已重启，本次任务未完成。',
    action: '请重新发起任务。'
  },
  ORPHAN_PROCESS: {
    cause: '检测到上次遗留的任务。',
    action: '请先停止上次任务后再试。'
  },
  PROCESS_TIMEOUT: {
    cause: '任务在规定时间内没有完成。',
    action: '请检查目标设备连接后重试。'
  },
  THREAD_START_FAILED: {
    cause: '平台未能启动本次任务。',
    action: '请稍后重试；如仍失败，请重新启动平台。'
  },
  PROCESS_EXCEPTION: {
    cause: '任务运行过程中出现平台异常，没有得到完整结果。',
    action: '请重新运行；如再次出现，展开诊断信息并联系维护人员。'
  },
  UNHANDLED_EXCEPTION: {
    cause: '任务运行过程中出现平台异常，没有得到完整结果。',
    action: '请重新运行；如再次出现，展开诊断信息并联系维护人员。'
  },
  RESULT_MISSING: {
    cause: '测试程序已经结束，但没有返回可用结果。',
    action: '请重新运行；如仍失败，保留诊断信息并联系维护人员。'
  },
  HISTORY_WRITE_FAILED: {
    cause: '测试已经执行，但结果没有保存成功。',
    action: '请确认电脑存储空间充足后重试。'
  },
  HARDWARE_INFRASTRUCTURE_FAILURE: {
    cause: '测试过程中与手表的连接中断。',
    action: '请重新执行环境检查，恢复连接后从该用例重试。'
  },
  HARDWARE_PREPARATION_FAILED: {
    cause: '手表没有进入可开始测试的状态，因此用例尚未执行。',
    action: '请重新执行环境检查，确认连接和手表界面正常后重试。'
  },
  EVIDENCE_INCOMPLETE: {
    cause: '用于判断结果的截图或检查点没有收集完整。',
    action: '请确认截图连接正常后重新运行该用例。'
  },
  OBSERVATION_UNAVAILABLE: {
    cause: '动作已经执行，但当前 579 没有可用的手表截图通道。',
    action: '查看每条 TX/L1 ACK 记录并人工确认现象；不要把本次结果当作正式通过。'
  },
  INVALID_RAW_COMMAND: {
    cause: 'Cmd、Key 或 Data 的格式或长度不符合 579 协议要求。',
    action: '检查十六进制输入；Data 可留空，非空时最多 499 字节。'
  },
  BLE_DEVICE_NOT_FOUND: {
    cause: '扫描结果中没有找到已配置精确地址的手表。',
    action: '确认手表地址、距离和广播状态后重新扫描。'
  },
  BLE_CONNECT_TIMEOUT: {
    cause: '电脑未能在限定时间内连接 579 手表。',
    action: '请关闭手机蓝牙和 ble_test 等占用方，然后手工重试。'
  },
  BLE_GATT_PROFILE_MISMATCH: {
    cause: '已连接设备没有提供 579 所需的服务或写入、通知特征。',
    action: '核对精确 MAC 和当前手表版本后重试。'
  },
  BLE_DISCONNECTED: {
    cause: '579 BLE 会话已经断开。',
    action: '恢复手表连接后重新执行当前命令；平台不会自动重发。'
  },
  BLE_ACK_TIMEOUT: {
    cause: '命令写入后 5 秒内没有收到手表 L1 ACK。',
    action: '检查手表连接和运行状态后，由你决定是否重新执行。'
  },
  TARGET_BUSY: {
    cause: '579 正由自动化任务独占写入。',
    action: '等待任务结束或取消后再从蓝牙工作台操作。'
  },
  PROFILE_INVALID: {
    cause: '当前测试项目的运行配置缺失或无法读取。',
    action: '请重新选择测试项目；如仍失败，请联系维护人员。'
  },
  PORT_NOT_SELECTED: {
    cause: '当前没有发现唯一可用的 SuperCom 手表串口。',
    action: '请在 SuperCom 中打开当前手表串口后重试，平台会自动识别，无需手动配置端口。'
  },
  SUPERCOM_PIPE_UNAVAILABLE: {
    cause: '平台没有检测到 SuperCom 中已打开的手表连接。',
    action: '请打开 SuperCom，连接当前手表对应的串口后重新检查。'
  },
  SUPERCOM_NO_UART: {
    cause: '平台找到了连接入口，但没有收到手表响应。',
    action: '请确认 SuperCom 连接的是当前手表，并唤醒手表屏幕后重试。'
  },
  USB_DEVICE_NOT_PRESENT: {
    cause: '电脑当前没有检测到手表的 USB 连接。',
    action: '请重新插拔 USB，确认电脑能够识别手表后重试。'
  },
  USB_TARGET_AMBIGUOUS: {
    cause: '电脑同时检测到多台可测试手表，无法确定目标。',
    action: '请只保留当前要测试的一台手表后重试。'
  },
  MTP_NAMESPACE_NOT_READY: {
    cause: '电脑检测到了手表，但暂时无法读取截图。',
    action: '请重新连接 USB，并确认电脑能够打开手表存储后重试。'
  },
  LLM_NOT_READY: {
    cause: '结果判定服务当前不可用。',
    action: '请检查网络和判定服务设置后重试。'
  },
  TARGET_BUSY: {
    cause: '当前手表正在执行另一个任务。',
    action: '请等待该任务结束或停止后再检查。'
  },
  PREFLIGHT_INTERNAL_ERROR: {
    cause: '环境检查没有正常完成。',
    action: '请重新检查；如再次出现，展开诊断信息并联系维护人员。'
  },
  BLE_UNAVAILABLE: {
    cause: '当前电脑的蓝牙功能不可用。',
    action: '请开启系统蓝牙并允许平台使用蓝牙，让手表保持亮屏且靠近电脑，确认手表未被其他设备占用后重新查找或连接。'
  },
  BLE_SCAN_FAILED: {
    cause: '电脑没有完成本次蓝牙查找。',
    action: '请开启系统蓝牙并允许平台使用蓝牙，让手表保持亮屏且靠近电脑，确认手表未被其他设备占用后重新查找或连接。'
  },
  BLE_CONNECT_TIMEOUT: {
    cause: '在规定时间内没有与这台手表建立连接。',
    action: '请开启系统蓝牙并允许平台使用蓝牙，让手表保持亮屏且靠近电脑，确认手表未被其他设备占用后重新查找或连接。'
  },
  BLE_CONNECT_FAILED: {
    cause: '电脑没有与这台手表建立可用连接。',
    action: '请开启系统蓝牙并允许平台使用蓝牙，让手表保持亮屏且靠近电脑，确认手表未被其他设备占用后重新查找或连接。'
  },
  LLM_TLS_ERROR: {
    cause: '电脑无法与判定服务建立安全连接。',
    action: '请检查网络后重试；如仍失败，请联系维护人员检查判定服务。'
  },
  LLM_TIMEOUT: {
    cause: '判定服务在规定时间内没有响应。',
    action: '请检查网络连接，稍后重新测试。'
  },
  LLM_AUTH_FAILED: {
    cause: '判定服务没有接受当前账号信息。',
    action: '请在系统设置中重新核对判定服务账号或密钥。'
  },
  LLM_MODEL_NOT_FOUND: {
    cause: '当前选择的判定模型不可用。',
    action: '请在系统设置中重新选择或核对判定模型。'
  },
  LLM_QUOTA_EXCEEDED: {
    cause: '判定服务暂时无法接受更多请求，或当前账号可用额度不足。',
    action: '请稍后重试，并检查判定服务账号状态。'
  },
  LLM_REQUEST_FAILED: {
    cause: '电脑没有完成本次判定服务连接测试。',
    action: '请检查网络和判定服务设置后重试。'
  }
});

const ISSUE_CODE_ALIASES = Object.freeze({
  BLE_RUNTIME_UNAVAILABLE: 'BLE_UNAVAILABLE',
  BLE_CONNECTION_TIMEOUT: 'BLE_CONNECT_TIMEOUT',
  BLE_CONNECTION_FAILED: 'BLE_CONNECT_FAILED',
  BLE_DISCOVERY_FAILED: 'BLE_SCAN_FAILED',
  'SSL/TLS 握手失败': 'LLM_TLS_ERROR',
  '网络连接超时': 'LLM_TIMEOUT',
  'API KEY 鉴权失败': 'LLM_AUTH_FAILED',
  '模型不存在': 'LLM_MODEL_NOT_FOUND',
  '额度不足或频次超限': 'LLM_QUOTA_EXCEEDED',
  '请求异常': 'LLM_REQUEST_FAILED'
});

function resolveIssueDefinition(reasonCode = '') {
  const rawKey = String(reasonCode || '').trim();
  if (!rawKey) return null;
  const upper = rawKey.toUpperCase();
  const direct = ISSUE_TABLE[upper] || ISSUE_TABLE[rawKey];
  if (direct) return direct;
  const aliasKey = ISSUE_CODE_ALIASES[upper] || ISSUE_CODE_ALIASES[rawKey];
  if (aliasKey && ISSUE_TABLE[aliasKey]) return ISSUE_TABLE[aliasKey];
  return null;
}

function knownIssueSummary(reasonCode = '') {
  const def = resolveIssueDefinition(reasonCode);
  return def ? def.cause : '';
}

function issuePresentation({fallback = '', status = 0, reasonCode = '', detail = '', action = ''} = {}) {
  const rawDetail = String(detail ?? '').trim();
  const code = String(reasonCode || '').trim().toUpperCase();
  const known = resolveIssueDefinition(code || reasonCode);
  if (known) {
    return {
      cause: known.cause,
      action: known.action,
      summary: known.cause,
      detail: rawDetail,
      code
    };
  }
  const friendly = friendlyAgentError(rawDetail);
  if (friendly && friendly !== rawDetail) {
    const finalAction = safeProductCopy(action) || '请检查设置后重试。';
    return {
      cause: friendly,
      action: finalAction,
      summary: friendly,
      detail: rawDetail,
      code
    };
  }
  const defaultCause = safeProductCopy(fallback) || UNKNOWN_ISSUE.cause;
  const defaultAction = safeProductCopy(action) || UNKNOWN_ISSUE.action;
  return {
    cause: defaultCause,
    action: defaultAction,
    summary: defaultCause,
    detail: rawDetail,
    code
  };
}

function issueNoticeHtml({reasonCode = '', detail = '', action = '', fallback = ''} = {}) {
  const issue = issuePresentation({reasonCode, detail, action, fallback});
  const causeHtml = `<p><strong>问题原因：</strong>${escapeHtml(issue.cause)}</p>`;
  const actionHtml = issue.action ? `<p><strong>处理方法：</strong>${escapeHtml(issue.action)}</p>` : '';
  const diagnosticsHtml = issue.detail ? `<details class="inline-diagnostics"><summary>诊断信息</summary><pre><code>${escapeHtml(issue.detail)}</code></pre></details>` : '';
  return `<div class="notice notice-error">${causeHtml}${actionHtml}</div>${diagnosticsHtml}`;
}

function productApiError(value, status = 0, reasonCode = '') {
  return productApiPresentation(value, status, reasonCode).summary;
}

function productApiPresentation(value, status = 0, reasonCode = '') {
  const safeFallback = status >= 400 && status < 500 ? safeProductCopy(value) : '';
  return issuePresentation({fallback: safeFallback, status, reasonCode, detail: value});
}

function presentationFromError(error, fallback = '') {
  if (error?.presentation) return error.presentation;
  return issuePresentation({
    fallback,
    status: Number(error?.status || 0),
    reasonCode: error?.code || '',
    detail: error?.diagnosticMessage || error?.message || ''
  });
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
  let response;
  try {
    response = await fetch(url, { ...options, headers });
  } catch (cause) {
    const diagnosticMessage = String(cause?.message || cause || '').trim();
    const presentation = issuePresentation({reasonCode: 'NETWORK_ERROR', detail: diagnosticMessage});
    const error = new Error(presentation.summary);
    error.status = 0;
    error.diagnosticMessage = diagnosticMessage;
    error.code = 'NETWORK_ERROR';
    error.payload = {};
    error.presentation = presentation;
    throw error;
  }
  let payload;
  try { payload = await response.json(); } catch { payload = {}; }
  if (!response.ok) {
    const diagnosticMessage = payload.error || `请求失败（${response.status}）`;
    const reasonCode = payload.reason_code || payload.error_code || '';
    const presentation = productApiPresentation(diagnosticMessage, response.status, reasonCode);
    const error = new Error(presentation.summary);
    error.status = response.status;
    error.diagnosticMessage = diagnosticMessage;
    error.code = reasonCode;
    error.payload = payload;
    error.presentation = presentation;
    throw error;
  }
  return payload;
}

function normalizeProjectProfile(project) {
  const projectId = String(project.project_id || project.project || '');
  const defaultTargetId = String(project.default_target || project.allowed_targets?.[0] || '');
  const target = PLATFORM_TARGETS[defaultTargetId] || {};
  return {
    ...project,
    ...target,
    project: projectId,
    project_id: projectId,
    projectLabel: String(project.project_name || project.project_label || projectId),
    project_label: String(project.project_name || project.project_label || projectId),
    target: String(target.execution_target || 'unknown'),
    targetLabel: String(target.execution_target_label || target.target_label || '未配置目标'),
    platform_id: String(project.default_platform || target.platform_id || ''),
    target_id: defaultTargetId
  };
}

async function loadPlatformRegistries() {
  const [platformPayload, projectPayload] = await Promise.all([
    api('/api/platforms'),
    api('/api/projects')
  ]);
  PLATFORM_PROFILES = Object.fromEntries(
    (platformPayload.platforms || []).map(item => [String(item.platform_id), item])
  );
  PLATFORM_TARGETS = Object.fromEntries(
    (platformPayload.targets || []).map(item => [String(item.target_id), item])
  );
  TEST_PROJECTS = Object.fromEntries(
    (projectPayload.items || []).map(item => {
      const normalized = normalizeProjectProfile(item);
      return [normalized.project, normalized];
    })
  );
  ENVIRONMENT_PROTOCOLS = Object.fromEntries(
    Object.values(TEST_PROJECTS).map(project => {
      const target = PLATFORM_TARGETS[project.target_id] || {};
      return [project.project, {
        transport: target.transport,
        transportLabel: target.transport_label,
        captureProvider: target.capture_provider,
        captureLabel: target.capture_label,
        serialProvider: target.serial_provider,
        serialLabel: target.serial_label,
        feedbackProvider: target.feedback_provider
      }];
    })
  );
  const projects = Object.values(TEST_PROJECTS);
  if (!projects.length) throw new Error('项目注册表为空，请先检查 /api/projects');
  DEFAULT_TEST_PROJECT = projects[0].project;
  renderGlobalProjectControls();
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
    close: '<path d="m6 6 12 12M18 6 6 18"/>',
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
  const params = new URLSearchParams(location.search);
  const queryProject = params.get('project_id') || params.get('project');
  if (queryProject) return queryProject;
  const stored = localStorage.getItem(PROJECT_STORAGE_KEY);
  return stored || DEFAULT_TEST_PROJECT;
}

function rememberProject(project) {
  const normalized = String(project || '');
  if (!Object.hasOwn(TEST_PROJECTS, normalized)) {
    throw new Error(`项目不存在或已归档：${normalized || '空'}`);
  }
  localStorage.setItem(PROJECT_STORAGE_KEY, normalized);
  const button = document.querySelector('#global-project-switch');
  if (button) button.textContent = `当前项目：${TEST_PROJECTS[normalized].projectLabel} ▾`;
  return normalized;
}

function renderGlobalProjectControls() {
  const current = Object.hasOwn(TEST_PROJECTS, currentProject()) ? currentProject() : DEFAULT_TEST_PROJECT;
  const button = document.querySelector('#global-project-switch');
  if (button) button.textContent = `当前项目：${TEST_PROJECTS[current]?.projectLabel || current} ▾`;
  renderProjectSwitchList('');
  renderCreateProjectOptions();
}

function renderProjectSwitchList(query = '') {
  const host = document.querySelector('#project-switch-list');
  if (!host) return;
  const needle = String(query).trim().toLocaleLowerCase('zh-CN');
  const current = currentProject();
  const projects = Object.values(TEST_PROJECTS).filter(project =>
    !needle || `${project.projectLabel} ${project.project}`.toLocaleLowerCase('zh-CN').includes(needle)
  );
  host.innerHTML = projects.length ? projects.map(project => `
    <button type="button" class="project-switch-item ${project.project === current ? 'is-current' : ''}" data-switch-project="${escapeHtml(project.project)}">
      <span><strong>${escapeHtml(project.projectLabel)}</strong><small>${escapeHtml(project.project)}</small></span>
      <span>${escapeHtml((project.allowed_platforms || []).map(id => PLATFORM_PROFILES[id]?.platform_label || id).join(' / '))}${project.project === current ? ' · 当前' : ''}</span>
    </button>`).join('') : Components.emptyState('没有匹配的项目');
}

function renderCreateProjectOptions() {
  const platformHost = document.querySelector('#create-project-platforms');
  const targetHost = document.querySelector('#create-project-targets');
  if (!platformHost || !targetHost) return;
  const preferredProject = Object.hasOwn(TEST_PROJECTS, currentProject()) ? currentProject() : DEFAULT_TEST_PROJECT;
  const preferredPlatform = currentPlatformFor(preferredProject);
  const preferredTarget = currentTargetFor(preferredProject, preferredPlatform);
  const platforms = Object.values(PLATFORM_PROFILES).sort((left, right) => {
    if (left.platform_id === 'w30') return -1;
    if (right.platform_id === 'w30') return 1;
    return String(left.platform_label || left.platform_id).localeCompare(String(right.platform_label || right.platform_id), 'zh-CN');
  });
  platformHost.innerHTML = platforms.map(platform => `
    <label><input type="checkbox" name="allowed_platforms" value="${escapeHtml(platform.platform_id)}" ${platform.platform_id === preferredPlatform ? 'checked' : ''}><span>${escapeHtml(platform.platform_label || platform.platform_id)}</span></label>`).join('');
  targetHost.innerHTML = Object.values(PLATFORM_TARGETS).map(target => `
    <label data-target-platform="${escapeHtml(target.platform_id)}"><input type="checkbox" name="allowed_targets" value="${escapeHtml(target.target_id)}" ${target.target_id === preferredTarget ? 'checked' : ''}><span>${escapeHtml(target.target_label || target.target_id)}</span></label>`).join('');
  syncCreateProjectOptions();
}

function syncCreateProjectOptions() {
  const form = document.querySelector('#create-project-form');
  if (!form) return;
  const platforms = [...form.querySelectorAll('input[name="allowed_platforms"]:checked')].map(input => input.value);
  const defaultPlatform = form.querySelector('#create-project-default-platform');
  const previousPlatform = defaultPlatform.value;
  defaultPlatform.innerHTML = platforms.map(id => `<option value="${escapeHtml(id)}">${escapeHtml(PLATFORM_PROFILES[id]?.platform_label || id)}</option>`).join('');
  if (platforms.includes(previousPlatform)) defaultPlatform.value = previousPlatform;
  form.querySelectorAll('[data-target-platform]').forEach(label => {
    const allowed = platforms.includes(label.dataset.targetPlatform);
    label.hidden = !allowed;
    if (!allowed) label.querySelector('input').checked = false;
  });
  const targets = [...form.querySelectorAll('input[name="allowed_targets"]:checked')]
    .map(input => input.value)
    .filter(id => PLATFORM_TARGETS[id]?.platform_id === defaultPlatform.value);
  const defaultTarget = form.querySelector('#create-project-default-target');
  defaultTarget.innerHTML = targets.map(id => `<option value="${escapeHtml(id)}">${escapeHtml(PLATFORM_TARGETS[id]?.target_label || id)}</option>`).join('');
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

function currentRouteUrl() {
  return `${location.pathname}${location.search}${location.hash}`;
}

function isInAppRoutePath(pathname) {
  if (pathname === '/tests' || TOP_LEVEL_ROUTE_PATHS.includes(pathname)) return true;
  const parts = pathname.split('/').filter(Boolean);
  return (parts[0] === 'test' && parts.length === 3)
    || (parts[0] === 'test-batch' && parts.length === 2)
    || (parts[0] === 'test-history' && parts.length === 4)
    || (parts[0] === 'defect' && parts.length === 2)
    || (parts[0] === 'history' && parts.length === 3);
}

function normalizedInAppUrl(raw) {
  if (!raw) return '';
  try {
    const target = new URL(String(raw), location.origin);
    if (target.origin !== location.origin || !isInAppRoutePath(target.pathname)) return '';
    return `${target.pathname}${target.search}${target.hash}`;
  } catch {
    return '';
  }
}

function safeInAppReturnUrl(raw, fallback = '') {
  return normalizedInAppUrl(raw) || normalizedInAppUrl(fallback);
}

function pageReturnUrl(fallback) {
  const safeFallback = safeInAppReturnUrl(fallback);
  const returnTo = safeInAppReturnUrl(new URLSearchParams(location.search).get('from'));
  return returnTo && returnTo !== currentRouteUrl() ? returnTo : safeFallback;
}

function withReturnContext(path, returnTo = currentRouteUrl()) {
  const target = new URL(path, location.origin);
  const safeReturnTo = safeInAppReturnUrl(returnTo);
  if (safeReturnTo) target.searchParams.set('from', safeReturnTo);
  return `${target.pathname}${target.search}${target.hash}`;
}

function returnDestinationLabel(returnTo, fallback = '上一页') {
  const pathname = new URL(returnTo, location.origin).pathname;
  const labels = {
    '/overview': '项目总览',
    '/tests': '用例管理',
    '/cases': '用例管理',
    '/runs': '自动化执行',
    '/reports': '测试报告',
    '/defects': '缺陷队列',
    '/environments': '环境中心',
    '/bluetooth': '蓝牙工作台',
  };
  if (labels[pathname]) return labels[pathname];
  if (pathname.startsWith('/test-batch/')) return '批次运行';
  if (pathname.startsWith('/test/')) return '测试详情';
  if (pathname.startsWith('/defect/')) return '缺陷详情';
  if (pathname.startsWith('/history/')) return '修复记录';
  return fallback;
}

function canUseNativeBack(returnTo) {
  const target = safeInAppReturnUrl(returnTo);
  const referrer = safeInAppReturnUrl(document.referrer);
  return Boolean(target && referrer && target === referrer && history.length > 1);
}

function rememberReturnScroll(targetUrl) {
  const returnTo = safeInAppReturnUrl(targetUrl.searchParams.get('from'));
  const current = currentRouteUrl();
  if (!returnTo || returnTo !== current) return;
  const state = window.history.state && typeof window.history.state === 'object' ? window.history.state : {};
  window.history.replaceState({...state, returnScrollUrl: current, returnScrollY: window.scrollY}, '', current);
}

function restoreRememberedScroll() {
  const state = window.history.state;
  if (!state || state.returnScrollUrl !== currentRouteUrl() || !Number.isFinite(state.returnScrollY)) return;
  const scrollY = Math.max(0, Number(state.returnScrollY));
  const nextState = {...state};
  delete nextState.returnScrollUrl;
  delete nextState.returnScrollY;
  window.history.replaceState(nextState, '', currentRouteUrl());
  const restore = () => window.scrollTo(0, scrollY);
  requestAnimationFrame(() => requestAnimationFrame(() => {
    restore();
    setTimeout(restore, 120);
  }));
}

function initGlobalTargetSwitcher() {
  const button = document.querySelector('#global-project-switch');
  if (!button || button.dataset.ready === 'true') return;
  button.dataset.ready = 'true';
  const switchDialog = document.querySelector('#project-switch-dialog');
  const createDialog = document.querySelector('#create-project-dialog');
  const switchToProject = projectValue => {
    const project = rememberProject(projectValue);
    selectedTestCases.clear();
    selectedTestProject = project;
    selectedRunPlatform = '';
    selectedRunTarget = '';
    invalidateCaseCatalog();
    const targetPath = TOP_LEVEL_ROUTE_PATHS.includes(location.pathname) ? location.pathname : '/cases';
    const requestedPlatform = new URLSearchParams(location.search).get('platform_id');
    const projectProfile = testProject(project);
    const allowedPlatforms = projectProfile.allowed_platforms || [];
    const compatiblePlatform = allowedPlatforms.includes(requestedPlatform)
      ? requestedPlatform
      : String(projectProfile.default_platform || allowedPlatforms[0] || '');
    const extra = targetPath === '/cases' && compatiblePlatform
      ? {platform_id: compatiblePlatform}
      : {};
    history.pushState({}, '', pageUrl(targetPath, project, extra));
    switchDialog?.close();
    route();
  };
  button.addEventListener('click', () => { renderProjectSwitchList(''); switchDialog?.showModal(); });
  document.querySelector('#open-create-project')?.addEventListener('click', () => { renderCreateProjectOptions(); createDialog?.showModal(); });
  document.querySelector('#project-switch-create')?.addEventListener('click', () => { switchDialog?.close(); renderCreateProjectOptions(); createDialog?.showModal(); });
  document.querySelectorAll('[data-close-project-switch]').forEach(item => item.addEventListener('click', () => switchDialog?.close()));
  document.querySelectorAll('[data-close-create-project]').forEach(item => item.addEventListener('click', () => createDialog?.close()));
  document.querySelector('#project-switch-search')?.addEventListener('input', event => renderProjectSwitchList(event.target.value));
  document.querySelector('#project-switch-list')?.addEventListener('click', event => {
    const item = event.target.closest('[data-switch-project]');
    if (item) switchToProject(item.dataset.switchProject);
  });
  document.querySelector('#create-project-form')?.addEventListener('change', event => {
    if (event.target.matches('input[name="allowed_platforms"], input[name="allowed_targets"], #create-project-default-platform')) syncCreateProjectOptions();
  });
  document.querySelector('#create-project-form')?.addEventListener('submit', async event => {
    event.preventDefault();
    const form = event.currentTarget;
    const error = form.querySelector('#create-project-error');
    const submit = form.querySelector('[type="submit"]');
    error.hidden = true;
    submit.disabled = true;
    try {
      const data = new FormData(form);
      const payload = {
        project_name: String(data.get('project_name') || '').trim(),
        project_id: String(data.get('project_id') || '').trim(),
        allowed_platforms: data.getAll('allowed_platforms'),
        default_platform: String(data.get('default_platform') || ''),
        allowed_targets: data.getAll('allowed_targets'),
        default_target: String(data.get('default_target') || '')
      };
      const created = await api('/api/projects', {method: 'POST', body: JSON.stringify(payload)});
      await loadPlatformRegistries();
      form.reset();
      createDialog?.close();
      showToast(`项目“${created.project_name}”已创建`, 'success');
      switchToProject(created.project_id);
    } catch (caught) {
      error.textContent = caught.message;
      error.hidden = false;
    } finally { submit.disabled = false; }
  });
  rememberProject(currentProject());
}

function initPrimaryNavigation() {
  if (document.documentElement.dataset.navigationReady === 'true') return;
  document.documentElement.dataset.navigationReady = 'true';
  document.addEventListener('click', event => {
    const link = event.target.closest?.('a[href]');
    if (!link || link.target === '_blank' || link.hasAttribute('download') || event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    const url = new URL(link.getAttribute('href'), location.href);
    if (url.origin !== location.origin) return;
    rememberReturnScroll(url);
    const destination = `${url.pathname}${url.search}${url.hash}`;
    if (link.hasAttribute('data-return-link') && canUseNativeBack(destination)) {
      event.preventDefault();
      history.back();
      return;
    }
    const targetPath = link.dataset.route || url.pathname;
    if (!TOP_LEVEL_ROUTE_PATHS.includes(targetPath)) return;
    event.preventDefault();
    const routeDestination = link.dataset.route
      ? pageUrl(targetPath, currentProject())
      : destination;
    history.pushState({}, '', routeDestination);
    route();
  });
}

function initImagePreview() {
  const dialog = document.querySelector('#image-preview-dialog');
  const previewImage = document.querySelector('#image-preview-image');
  const title = document.querySelector('#image-preview-title');
  const closeButton = document.querySelector('#close-image-preview');
  if (!dialog || !previewImage || !title || !closeButton || dialog.dataset.ready === 'true') return;
  dialog.dataset.ready = 'true';
  let lastTrigger = null;

  const closePreview = () => {
    if (dialog.open) dialog.close();
  };

  document.addEventListener('click', event => {
    const trigger = event.target.closest?.('[data-image-preview]');
    if (!trigger || event.defaultPrevented || event.button !== 0) return;
    const url = trigger.getAttribute('href');
    if (!url || url === '#') return;
    event.preventDefault();
    lastTrigger = trigger;
    const label = trigger.dataset.imagePreviewLabel || '图片预览';
    previewImage.src = url;
    previewImage.alt = label;
    title.textContent = label;
    if (!dialog.open) dialog.showModal();
    closeButton.focus();
  });

  closeButton.addEventListener('click', closePreview);
  dialog.addEventListener('click', event => {
    if (event.target === dialog) closePreview();
  });
  dialog.addEventListener('cancel', event => {
    event.preventDefault();
    closePreview();
  });
  dialog.addEventListener('close', () => {
    previewImage.removeAttribute('src');
    previewImage.alt = '';
    if (lastTrigger?.isConnected) lastTrigger.focus();
    lastTrigger = null;
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
  if (!project) {
    caseCatalogCache.clear();
    return;
  }
  for (const key of caseCatalogCache.keys()) {
    if (key.startsWith(`${project}\u001f`)) caseCatalogCache.delete(key);
  }
}

async function loadCaseCatalog(project = currentProject(), {force = false} = {}) {
  const normalized = testProject(project).project;
  const platformId = currentPlatformFor(normalized);
  const cacheKey = `${normalized}\u001f${platformId}`;
  if (force) invalidateCaseCatalog();
  if (caseCatalogCache.has(cacheKey)) return await caseCatalogCache.get(cacheKey);
  const pending = (async () => {
    const first = await api(`/api/tests?project_id=${encodeURIComponent(normalized)}&platform_id=${encodeURIComponent(platformId)}&page=1&page_size=${CASE_CATALOG_PAGE_SIZE}`);
    const pages = Array.from({length: Math.max(0, Number(first.total_pages || 1) - 1)}, (_, index) => index + 2);
    const payloads = await mapWithLimit(pages, 6, page => api(`/api/tests?project_id=${encodeURIComponent(normalized)}&platform_id=${encodeURIComponent(platformId)}&page=${page}&page_size=${CASE_CATALOG_PAGE_SIZE}`));
    return {
      ...first,
      items: [...(first.items || []), ...payloads.flatMap(payload => payload.items || [])]
    };
  })();
  caseCatalogCache.set(cacheKey, pending);
  try {
    return await pending;
  } catch (error) {
    caseCatalogCache.delete(cacheKey);
    throw error;
  }
}

async function loadCaseOverview(project = currentProject()) {
  const normalized = testProject(project).project;
  const platformId = currentPlatformFor(normalized);
  return await api(`/api/tests/overview?project_id=${encodeURIComponent(normalized)}&platform_id=${encodeURIComponent(platformId)}&limit=20`);
}

async function loadCasePage(project, {query = '', page = 1, state = 'all', category = 'all', module = ''} = {}) {
  const normalized = testProject(project).project;
  const platformId = currentPlatformFor(normalized);
  const params = new URLSearchParams({
    project_id: normalized,
    platform_id: platformId,
    q: query,
    page: String(page),
    page_size: String(PAGE_SIZE),
    state
  });
  const modules = module
    ? [module]
    : (category !== 'all' ? (FUNCTION_CATEGORIES[category] || []) : []);
  modules.forEach(name => params.append('module', name));
  const request = new URLSearchParams(params);
  modules.forEach(name => {
    if (!request.getAll('module').includes(name)) request.append('module', name);
  });
  const payload = await api(`/api/tests?${request.toString()}`);
  return payload;
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

function patchRenderedSections(root, markup, attributeName) {
  const template = document.createElement('template');
  template.innerHTML = String(markup || '');
  const selector = `[${attributeName}]`;
  const replacements = new Map(
    [...template.content.querySelectorAll(selector)].map(element => [element.getAttribute(attributeName), element])
  );
  const scrollX = window.scrollX;
  const scrollY = window.scrollY;
  let updated = false;
  root.querySelectorAll(selector).forEach(current => {
    const replacement = replacements.get(current.getAttribute(attributeName));
    if (!replacement || current.outerHTML === replacement.outerHTML) return;
    current.replaceWith(replacement);
    updated = true;
  });
  if (updated) window.scrollTo(scrollX, scrollY);
  return updated;
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
    return `<nav class="workspace-subtabs" aria-label="页面分类">${items.map(item => {
      const className = item.value === active ? 'is-active' : '';
      if (item.href) {
        return `<a href="${escapeHtml(item.href)}" class="${className}" ${item.value === active ? 'aria-current="page"' : ''}>${escapeHtml(item.label)}</a>`;
      }
      return `<button type="button" class="${className}" data-subtab="${escapeHtml(item.value)}" ${item.disabled ? 'disabled' : ''}>${escapeHtml(item.label)}</button>`;
    }).join('')}</nav>`;
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
  if (normalized === 'ERROR') return {className: 'chip chip-fail', label: '执行异常'};
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
  const maturity = String(row.automation_maturity || row.mapping_status || '').toUpperCase();
  if (maturity === 'AUTO_READY') return '<span class="chip chip-solidified">自动化就绪</span>';
  if (maturity === 'NEED_REVIEW') return '<span class="chip chip-unsolidified">待评审</span>';
  if (maturity === 'MANUAL_REQUIRED') return '<span class="chip chip-warning">需人工执行</span>';
  if (maturity === 'UNSUPPORTED') return '<span class="chip chip-fail">暂不支持</span>';
  if (row.is_promoted || row.maturity_state === 'solidified') {
    return '<span class="chip chip-solidified">可直接运行</span>';
  }
  if (row.is_execution_blocked || row.mapping_status === 'BLOCKED') {
    return '<span class="chip chip-fail">当前固件阻塞</span>';
  }
  if (row.is_execution_ready || row.mapping_status === 'EXECUTION_READY') {
    return '<span class="chip chip-unsolidified">动作可运行</span>';
  }
  if (row.external_explored || row.maturity_state === 'explored_unsolidified') {
    return '<span class="chip chip-unsolidified">步骤待确认</span>';
  }
  return '<span class="chip chip-unexplored">待生成步骤</span>';
}

function automationMaturityLabel(value) {
  return ({
    AUTO_READY: '自动化就绪',
    PROMOTED: '已固化',
    NEED_REVIEW: '待评审',
    MANUAL_REQUIRED: '需人工执行',
    UNSUPPORTED: '暂不支持',
    UNMAPPED: '未绑定',
  })[String(value || '').toUpperCase()] || String(value || '未知');
}

function caseSourceLabel(value) {
  return ({
    MANIFEST_579: '579 冻结基线',
    W30_CASE_MAP: 'W30 Case Map 基线',
    MANUAL: '人员新增',
    EXCEL: 'Excel 导入',
    CLONE: '复制创建',
  })[String(value || '').toUpperCase()] || '统一用例库';
}

function platformApplicabilityMarkup(projectId, selected = [], inputName = 'case-applicable-platform') {
  const project = testProject(projectId);
  const active = new Set((selected || []).map(String));
  return (project.allowed_platforms || []).map(platformId => {
    const label = PLATFORM_PROFILES[platformId]?.platform_label || platformId;
    return `<label class="platform-applicability-option"><input type="checkbox" name="${escapeHtml(inputName)}" value="${escapeHtml(platformId)}" ${active.has(platformId) ? 'checked' : ''}><span>${escapeHtml(label)}</span></label>`;
  }).join('');
}

function selectedPlatformValues(name) {
  return [...document.querySelectorAll(`input[name="${name}"]:checked`)].map(input => input.value);
}

function caseStatusChip(row = {}) {
  const historyCount = Math.max(
    Number(row.history_count || 0),
    Array.isArray(row.history) ? row.history.length : 0
  );
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
    ERROR: 'ERROR'
  }[normalized] || presentation.label;
  return `<span class="${presentation.className}">${label}</span>`;
}

function testExecutionModeLabel(value) {
  const labels = {
    fixed_mapping: '固化步骤',
    candidate_mapping: '外部候选复跑',
    agent_exploration: 'Agent-loop 临时探索',
    '579_deterministic': '579 冻结计划'
  };
  return labels[String(value || '')] || '旧记录未标明';
}

function testProject(value = DEFAULT_TEST_PROJECT) {
  const project = TEST_PROJECTS[value];
  if (!project) throw new Error(`项目不存在或已归档：${value || '空'}`);
  return project;
}

function currentPlatformFor(project = currentProject()) {
  const meta = testProject(project);
  const requested = new URLSearchParams(location.search).get('platform_id');
  if (requested && (meta.allowed_platforms || []).includes(requested)) return requested;
  return String(meta.default_platform || meta.platform_id || '');
}

function targetsFor(project, platformId) {
  const meta = testProject(project);
  return (meta.allowed_targets || [])
    .map(targetId => PLATFORM_TARGETS[targetId])
    .filter(target => target?.platform_id === platformId);
}

function currentTargetFor(project, platformId = currentPlatformFor(project)) {
  const requested = new URLSearchParams(location.search).get('target_id');
  const targets = targetsFor(project, platformId);
  if (requested && targets.some(target => target.target_id === requested)) return requested;
  const meta = testProject(project);
  if (targets.some(target => target.target_id === meta.default_target)) return meta.default_target;
  return targets[0]?.target_id || '';
}

function targetProfile(project, platformId = currentPlatformFor(project), targetId = currentTargetFor(project, platformId)) {
  const meta = testProject(project);
  const target = PLATFORM_TARGETS[targetId] || {};
  return {
    ...meta,
    ...target,
    project: meta.project,
    project_id: meta.project,
    project_label: meta.projectLabel,
    platform_id: platformId,
    target_id: targetId,
    target: target.execution_target || meta.target,
    targetLabel: target.execution_target_label || target.target_label || meta.targetLabel,
  };
}

function platformSwitchButtons(project, activePlatform = currentPlatformFor(project), attribute = 'data-platform-view') {
  return (testProject(project).allowed_platforms || []).map(platformId => {
    const platform = PLATFORM_PROFILES[platformId] || {};
    return `<button type="button" class="platform-choice ${platformId === activePlatform ? 'is-active' : ''}" ${attribute}="${escapeHtml(platformId)}">${escapeHtml(platform.platform_label || platformId)}</button>`;
  }).join('');
}

function runPlatformSwitchButtons(project, activePlatform = currentPlatformFor(project), attribute = 'data-run-platform') {
  const allowed = new Set(testProject(project).allowed_platforms || []);
  const platforms = Object.values(PLATFORM_PROFILES).sort((left, right) => {
    if (left.platform_id === 'w30') return -1;
    if (right.platform_id === 'w30') return 1;
    return String(left.platform_label || left.platform_id).localeCompare(String(right.platform_label || right.platform_id), 'zh-CN');
  });
  return platforms.map(platform => {
    const platformId = String(platform.platform_id || '');
    const enabled = allowed.has(platformId);
    const title = enabled ? '' : `当前项目未启用 ${platform.platform_label || platformId}`;
    return `<button type="button" class="platform-choice ${platformId === activePlatform ? 'is-active' : ''}" ${attribute}="${escapeHtml(platformId)}" ${enabled ? '' : 'disabled'} title="${escapeHtml(title)}">${escapeHtml(platform.platform_label || platformId)}</button>`;
  }).join('');
}

function caseProjectsForPlatform(platformId) {
  return Object.values(TEST_PROJECTS).filter(project =>
    (project.allowed_platforms || []).includes(platformId)
  );
}

function caseProjectForPlatform(platformId, preferredProject = currentProject()) {
  const projects = caseProjectsForPlatform(platformId);
  const preferred = projects.find(project => project.project === preferredProject);
  if (preferred) return preferred.project;
  const canonicalId = platformId === '579' ? '579_O2' : DEFAULT_TEST_PROJECT;
  return projects.find(project => project.project === canonicalId)?.project || projects[0]?.project || '';
}

function caseManagementPlatformButtons(activePlatform = '') {
  return ['w30', '579'].map(platformId => {
    const label = platformId === 'w30' ? 'W30' : '579';
    const project = caseProjectForPlatform(platformId);
    if (!project) return `<span class="platform-choice is-disabled">${label}</span>`;
    return `<form class="case-platform-switch-form" method="get" action="/cases" data-case-platform="${platformId}"><input type="hidden" name="project" value="${escapeHtml(project)}"><input type="hidden" name="platform_id" value="${platformId}"><button class="platform-choice ${platformId === activePlatform ? 'is-active' : ''}" type="submit" ${platformId === activePlatform ? 'aria-current="page"' : ''}>${label}</button></form>`;
  }).join('');
}

function confirm579Watchface(project) {
  if (project !== '579_Z1640') return true;
  return window.confirm('请先确认 579 手表当前位于亮屏表盘。确认后，Runner 会直接发送侧键和触控命令；若 OTA 后侧键路径失效，请取消并停止迁移。');
}

function testTargetChip(value) {
  const meta = typeof value === 'string' ? testProject(value) : value;
  const target = meta?.execution_target || meta?.target || 'simulator';
  const projectLabel = meta?.project_label || meta?.projectLabel || testProject().projectLabel;
  const targetLabel = meta?.execution_target_label || meta?.targetLabel || (target === 'hardware' ? '真机' : '模拟器');
  return `<span class="chip chip-target target-${escapeHtml(target)}">${escapeHtml(projectLabel)} · ${escapeHtml(targetLabel)}</span>`;
}

function isHardwareTestTarget(value) {
  const meta = typeof value === 'string' ? testProject(value) : value;
  return (meta?.execution_target || meta?.target) === 'hardware';
}

function screenshotLabel(value) {
  const meta = typeof value === 'string' ? testProject(value) : value;
  if (meta?.capture_provider === 'o2' || meta?.target_id === '579.o2' || meta?.platform_id === '579') return 'O2 手表截图';
  return isHardwareTestTarget(meta) ? '真机截图' : '模拟器截图';
}

function screenshotGridClass(value) {
  return `checkpoint-grid${isHardwareTestTarget(value) ? ' checkpoint-grid-hardware' : ''}`;
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
      <svg class="workflow test-workflow" viewBox="0 0 952 128" role="img" aria-label="自动化测试流程">
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
  const rows = items.map(row => {
    const detailHref = defectDetailHref(row.number);
    return `<tr><td><a class="case-id-link" href="${escapeHtml(detailHref)}">#${escapeHtml(row.number)}</a></td><td><strong>${escapeHtml(row.title)}</strong><small>${escapeHtml(row.description || '')}</small></td><td><span class="chip chip-status">${escapeHtml(row.priority || row.status || '未知')}</span></td><td>${resultChip(row.repair_result)}</td><td>${row.last_run_at ? formatTime(row.last_run_at) : '—'}</td><td class="table-actions"><a class="table-icon-action" href="${escapeHtml(detailHref)}" aria-label="查看缺陷 #${escapeHtml(row.number)}">${icon('runs', 17)}</a></td></tr>`;
  }).join('');
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
  if (job.status === 'done') return {label: '同步完成', chip: 'chip-pass'};
  if (job.status === 'failed') return {label: '同步失败', chip: 'chip-fail'};
  return {label: '正在同步', chip: 'chip-running'};
}

function updateImportButton(job = null) {
  const button = document.querySelector('#open-import');
  if (!button) return;
  const running = job?.status === 'running';
  button.disabled = running;
  button.textContent = running ? '同步中…' : '同步缺陷';
}

function renderImportProgress(job, message = '') {
  const panel = document.querySelector('#import-progress');
  if (!panel) return;
  const status = importStatus(job);
  const fullLog = String(job.stdout_tail || '');
  const diagnosticText = [job.error, fullLog].filter(Boolean).join('\n\n');
  panel.hidden = false;
  panel.innerHTML = `
    <div class="import-progress-head">
      <div>
        <span class="chip ${status.chip}">${status.label}</span>
        <strong>缺陷同步</strong>
      </div>
      <span class="muted small">${job.finished_at ? `完成于 ${formatTime(job.finished_at)}` : '正在处理'}</span>
    </div>
    ${message ? `<div class="import-message">${escapeHtml(message)}</div>` : ''}
    ${job.error ? `<div class="notice notice-error">${escapeHtml(productApiError(job.error, 500))}</div>` : ''}
    ${diagnosticText ? `<details class="import-log-details"><summary>诊断信息${job.id ? ` · ${escapeHtml(job.id)}` : ''}</summary><pre><code>${escapeHtml(diagnosticText)}</code></pre></details>` : ''}`;
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
      showToast('缺陷同步完成');
      const input = document.querySelector('#defect-search');
      await refreshDefects(input?.value.trim() || '', 1);
    } else {
      showToast(productApiError(job.error || '缺陷同步失败', 500), 'error');
    }
  } catch (error) {
    if (activeImportJobId !== jobId) return;
    if (error.status === 404) {
      localStorage.removeItem(IMPORT_STORAGE_KEY);
      activeImportJobId = null;
      renderImportProgress({id: jobId, status: 'failed', error: '同步任务不存在，可能是服务已重启。', stdout_tail: ''});
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
  panel.innerHTML = `<header><div><h2>当前修复任务</h2><p>${defectNumber ? `缺陷 #${escapeHtml(defectNumber)}` : '正在修复'}</p></div>${Components.statusChip('RUNNING', '修复中')}</header>${renderRepairWorkflow(job)}<div class="repair-task-summary"><dl><div><dt>当前阶段</dt><dd>${escapeHtml(NODE_LABELS[job.current_node] || job.current_node || '准备中')}</dd></div><div><dt>开始时间</dt><dd>${formatTime(job.started_at)}</dd></div></dl>${defectNumber ? `<a class="button button-secondary" href="${escapeHtml(defectDetailHref(defectNumber))}">查看任务</a>` : ''}</div>`;
}

async function renderList() {
  setActiveNav('defects');
  document.title = 'ONES 缺陷 · Agent-loop';
  const initial = listParams();
  app.innerHTML = `
    ${Components.pageHeader({title: 'ONES 缺陷', intro: '同步并处理 ONES 缺陷', actions: `<button id="open-import" class="button" type="button">${icon('refresh', 17)} 同步缺陷</button>`})}
    <section class="defect-filter-bar" aria-label="缺陷队列工具"><div id="search-stage" class="search-box">${icon('search', 18)}<input id="defect-search" type="search" value="${escapeHtml(initial.query)}" placeholder="搜索编号、标题或描述" autocomplete="off"><button id="clear-search" class="clear-search" type="button" aria-label="清空搜索">清空</button></div><select aria-label="缺陷来源" disabled><option>ONES</option></select></section>
    <section id="import-progress" class="import-progress" aria-live="polite" hidden></section>
    <section id="metrics" class="metrics defect-kpis" aria-label="缺陷统计与筛选">${metricCards({}, initial.result)}</section>
    <section class="defect-loop-grid"><article class="panel queue-panel"><header class="panel-head"><div><h2 id="queue-title">${RESULT_FILTER_LABELS[initial.result]}</h2><p>查看同步结果和处理状态</p></div><span id="queue-count" class="chip chip-pending">正在读取</span></header><div id="defect-list" class="defect-list"><div class="list-loading">正在读取缺陷…</div></div><div id="pagination"></div></article><aside id="current-repair-summary" class="workspace-panel current-repair-summary"><header><div><h2>当前修复任务</h2><p>正在读取活动任务</p></div></header><div class="list-loading">正在读取…</div></aside></section>
    <dialog id="import-dialog" class="import-dialog" aria-labelledby="import-dialog-title">
      <form id="import-form">
        <header class="dialog-head">
          <div><p class="eyebrow">更新缺陷列表</p><h2 id="import-dialog-title">同步 ONES 缺陷</h2></div>
          <button id="close-import" class="dialog-close" type="button" aria-label="关闭">×</button>
        </header>
        <div class="dialog-body">
          <fieldset class="import-fieldset">
            <legend>类型</legend>
            <label class="choice-row"><input type="radio" name="import-type" value="open" checked><span><strong>仅开放缺陷</strong><small>忽略已经关闭的缺陷</small></span></label>
            <label class="choice-row"><input type="radio" name="import-type" value="all"><span><strong>含已关闭缺陷</strong><small>同时同步已经关闭的缺陷</small></span></label>
          </fieldset>
          <label class="import-number-field" for="import-limit"><span>数量</span><input id="import-limit" name="limit" type="number" min="0" step="1" value="0" placeholder="0=不限制"><small>填写 0 表示不限制，也可以填写 50、100 等数量。</small></label>
          <label class="choice-row choice-checkbox"><input id="import-force" type="checkbox" checked><span><strong>重新分析已有缺陷</strong><small>已同步的缺陷也重新分析</small></span></label>
        </div>
        <footer class="dialog-actions">
          <button id="cancel-import" class="button button-secondary" type="button">取消</button>
          <button id="submit-import" class="button" type="submit">开始同步</button>
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
      showToast('缺陷同步已开始');
      pollImport(job.id);
    } catch (error) {
      showToast(error.status === 409 ? '已有同步任务在运行' : error.message, error.status === 409 ? 'warning' : 'error');
    } finally {
      submit.disabled = false;
      submit.textContent = '开始同步';
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
  const project = String(params.get('project_id') || params.get('project') || currentProject());
  const rawCategory = String(params.get('category') || 'all');
  const category = rawCategory === 'all' || Object.hasOwn(FUNCTION_CATEGORIES, rawCategory) ? rawCategory : 'all';
  const rawModule = String(params.get('module') || '');
  const module = ALL_FUNCTION_MODULES.includes(rawModule) ? rawModule : '';
  const normalizedProject = testProject(project).project;
  return {
    query: (params.get('q') || '').trim(),
    page: Number.isFinite(rawPage) && rawPage > 0 ? rawPage : 1,
    state: Object.hasOwn(TEST_FILTER_LABELS, state) ? state : DEFAULT_TEST_STATE,
    project: normalizedProject,
    platform: currentPlatformFor(normalizedProject),
    category,
    module
  };
}

function buildTestListUrl(query, page, state = DEFAULT_TEST_STATE, project = DEFAULT_TEST_PROJECT, category = 'all', module = '') {
  const params = new URLSearchParams();
  params.set('project', testProject(project).project);
  params.set('platform_id', currentPlatformFor(project));
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
  return pageReturnUrl(fallback);
}

function withTestReturn(path, project = DEFAULT_TEST_PROJECT, returnTo = '/cases') {
  const target = new URL(path, location.origin);
  target.searchParams.set('project', testProject(project).project);
  return withReturnContext(`${target.pathname}${target.search}`, returnTo);
}

function testDetailHref(project, sheet, caseId, returnTo = '/cases') {
  return withTestReturn(`/test/${encodeURIComponent(sheet)}/${encodeURIComponent(caseId)}`, project, returnTo);
}

function testBatchHref(jobId, returnTo = currentRouteUrl()) {
  return withReturnContext(`/test-batch/${encodeURIComponent(jobId)}`, returnTo);
}

function defectDetailHref(number, returnTo = currentRouteUrl()) {
  return withReturnContext(pageUrl(`/defect/${encodeURIComponent(number)}`, currentProject()), returnTo);
}

function defectHistoryHref(number, runId, returnTo = currentRouteUrl()) {
  return withReturnContext(pageUrl(`/history/${encodeURIComponent(number)}/${encodeURIComponent(runId)}`, currentProject()), returnTo);
}

function testHistoryHref(project, sheet, caseId, runId, returnTo = '/cases') {
  return withTestReturn(`/test-history/${encodeURIComponent(sheet)}/${encodeURIComponent(caseId)}/${encodeURIComponent(runId)}`, project, returnTo);
}

function testMetricCards(summary = null, activeState = DEFAULT_TEST_STATE) {
  const loading = summary === null;
  const values = summary || {};
  const maturityCards = [
    ['all', 'all', '全部用例'],
    ['unexplored', 'unexplored', '待生成步骤'],
    ['explored_unsolidified', 'explored_unsolidified', '步骤待确认'],
    ['solidified', 'solidified', '可直接运行']
  ];
  const renderCards = cards => cards.map(([state, countKey, label]) => `
    <button class="metric${activeState === state ? ' is-active' : ''}" type="button" data-test-filter="${state}" aria-pressed="${activeState === state}">
      <strong>${loading ? '—' : Number(values[countKey] || 0)}</strong><span>${label}</span>
    </button>`).join('');
  return `
    <section class="test-metric-group" aria-labelledby="maturity-status-title">
      <header class="test-metric-heading">
        <div><strong id="maturity-status-title">自动化状态</strong><span>按用例当前准备情况分类</span></div>
        <small>${loading ? '正在读取用例统计' : `全部 ${Number(values.all || 0)}`}</small>
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
    const sourceLocked = Boolean(row.source_locked || row.is_frozen_source);
    const platforms = (row.applicable_platforms || []).map(platformId => PLATFORM_PROFILES[platformId]?.platform_label || platformId).join(' / ');
    const revision = Number(row.current_revision || 1);
    const testItem = String(row.test_item || '').trim() || '待补充';
    const testPoint = String(row.test_point || '').trim() || '待补充';
    const actualResult = String(row.actual_result || '').trim()
      || (Number(row.history_count || 0) > 0
        ? workspaceVerdictLabel(row.last_product_verdict || row.latest_verdict)
        : '尚未运行');
    return `<tr class="test-row-shell case-summary-row${selected ? ' is-selected' : ''}" data-case-expand tabindex="0" aria-expanded="false">
      <td><label class="table-checkbox" title="${escapeHtml(`选择 ${caseId} 创建精确批次`)}"><input type="checkbox" data-test-case-select data-sheet="${escapeHtml(sheet)}" data-case-id="${escapeHtml(caseId)}" aria-label="选择用例 ${escapeHtml(caseId)}" ${selected ? 'checked' : ''}></label></td>
      <td class="case-id-cell"><span class="case-expand-chevron" aria-hidden="true">›</span><a class="case-id-link" href="${escapeHtml(testDetailHref(row.project, sheet, caseId, returnTo))}">${escapeHtml(caseId)}</a><small>${escapeHtml(sheet)} · ${escapeHtml(caseSourceLabel(row.source_type))} · v${revision}${row.batch_id ? ` · ${escapeHtml(row.batch_id)}` : ''}</small></td>
      <td class="case-summary-text"><strong>${escapeHtml(testItem)}</strong></td>
      <td class="case-summary-text"><strong>${escapeHtml(testPoint)}</strong></td>
      <td><span class="chip chip-status priority-${escapeHtml(String(row.priority || '').toLowerCase())}">${escapeHtml(row.priority || '未分级')}</span></td>
      <td>${maturityChip(row)}<small>${escapeHtml(platforms ? `适用平台：${platforms}` : '未指定适用平台')}</small></td>
      <td>${caseStatusChip(row)}</td>
      <td><time>${row.last_run_at ? formatTime(row.last_run_at) : '—'}</time>${row.history_count ? `<small>${Number(row.history_count)} 次</small>` : ''}</td>
      <td class="table-actions"><a class="table-icon-action" href="${escapeHtml(testDetailHref(row.project, sheet, caseId, returnTo))}" title="查看并运行 ${escapeHtml(caseId)}" aria-label="查看并运行 ${escapeHtml(caseId)}">${icon('runs', 17)}</a><button class="table-icon-action edit-case-btn" type="button" title="${sourceLocked ? '创建新版本' : '编辑用例'} ${escapeHtml(caseId)}" data-edit-case data-sheet="${escapeHtml(sheet)}" data-case-id="${escapeHtml(caseId)}" data-priority="${escapeHtml(row.priority || 'P1')}" data-test-item="${escapeHtml(row.test_item || '')}" data-test-point="${escapeHtml(row.test_point || '')}" data-precondition="${escapeHtml(row.precondition_text || '')}" data-steps="${escapeHtml(row.steps_text || '')}" data-expected="${escapeHtml(row.expected_text || '')}" data-note="${escapeHtml(row.note || '')}" data-applicable-platforms="${escapeHtml(JSON.stringify(row.applicable_platforms || []))}" data-workflow-state="${escapeHtml(row.workflow_state || 'ACTIVE')}" data-source-locked="${sourceLocked ? 'true' : 'false'}" aria-label="${sourceLocked ? '为冻结用例创建新版本' : '编辑用例'} ${escapeHtml(caseId)}">${sourceLocked ? '版' : '✎'}</button></td>
    </tr>
    <tr class="case-detail-row" hidden>
      <td colspan="9">
        <section class="case-inline-detail" aria-label="用例 ${escapeHtml(caseId)} 完整详情">
          <div class="case-detail-field"><span>测试项</span><p>${escapeHtml(testItem)}</p></div>
          <div class="case-detail-field"><span>测试点</span><p>${escapeHtml(testPoint)}</p></div>
          <div class="case-detail-field"><span>前置条件</span><p>${escapeHtml(row.precondition_text || '无')}</p></div>
          <div class="case-detail-field is-wide"><span>操作步骤</span><p>${escapeHtml(row.steps_text || '未填写')}</p></div>
          <div class="case-detail-field is-wide"><span>预期结果</span><p>${escapeHtml(row.expected_text || '未填写')}</p></div>
          <div class="case-detail-field"><span>实际结果</span><p>${escapeHtml(actualResult)}</p></div>
        </section>
      </td>
    </tr>`;
  }).join('');
  return `<div class="workspace-table-scroll"><table class="workspace-table case-table"><thead><tr><th class="checkbox-column"></th><th>用例编号</th><th>测试项</th><th>测试点</th><th>优先级</th><th>自动化成熟度</th><th>最近结果</th><th>最近运行</th><th>操作</th></tr></thead><tbody>${rows}</tbody></table></div>`;
}

function toggleCaseInlineDetail(summaryRow) {
  if (!summaryRow?.matches('[data-case-expand]')) return;
  const detailRow = summaryRow.nextElementSibling;
  if (!detailRow?.classList.contains('case-detail-row')) return;
  const expanded = summaryRow.getAttribute('aria-expanded') === 'true';
  summaryRow.setAttribute('aria-expanded', String(!expanded));
  summaryRow.classList.toggle('is-expanded', !expanded);
  detailRow.hidden = expanded;
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

async function refreshExecutionOptions() {
  const buttons = [...document.querySelectorAll('[data-run-platform]')];
  const targetSelect = document.querySelector('#run-target-select');
  const blocker = document.querySelector('#run-platform-blocker');
  const run = document.querySelector('#run-selected-tests');
  if (!targetSelect || !blocker || !run) return;
  const project = selectedTestProject || currentProject();
  const selected = selectedTestCaseList();
  const token = ++executionOptionsToken;
  if (!selected.length) {
    selectedRunPlatform = currentPlatformFor(project);
    const targets = targetsFor(project, selectedRunPlatform);
    selectedRunTarget = targets[0]?.target_id || '';
    targetSelect.innerHTML = targets.map(target => `<option value="${escapeHtml(target.target_id)}">${escapeHtml(target.target_label || target.target_id)}</option>`).join('');
    const allowed = new Set(testProject(project).allowed_platforms || []);
    buttons.forEach(button => {
      button.disabled = !allowed.has(button.dataset.runPlatform);
      button.classList.toggle('is-active', button.dataset.runPlatform === selectedRunPlatform);
      button.title = button.disabled ? `当前项目未启用 ${PLATFORM_PROFILES[button.dataset.runPlatform]?.platform_label || button.dataset.runPlatform}` : '';
    });
    blocker.textContent = '勾选用例后校验平台映射';
    run.disabled = true;
    return;
  }
  run.disabled = true;
  blocker.textContent = '正在校验所选用例的平台映射…';
  try {
    const params = new URLSearchParams();
    params.set('case_ids', selected.map(item => item.case_id).join(','));
    const payload = await api(`/api/projects/${encodeURIComponent(project)}/execution-options?${params.toString()}`);
    if (token !== executionOptionsToken) return;
    const options = payload.options || [];
    let option = options.find(item => item.platform_id === selectedRunPlatform);
    if (!option) option = options.find(item => item.platform_id === currentPlatformFor(project)) || options[0];
    selectedRunPlatform = String(option?.platform_id || '');
    buttons.forEach(button => {
      const match = options.find(item => item.platform_id === button.dataset.runPlatform);
      button.disabled = !match;
      button.classList.toggle('is-active', button.dataset.runPlatform === selectedRunPlatform);
      button.title = !match
        ? `当前项目未启用 ${PLATFORM_PROFILES[button.dataset.runPlatform]?.platform_label || button.dataset.runPlatform}`
        : match.runnable ? '' : (match.blockers || []).map(item => `${item.case_id}: ${item.reason_label || item.reason}`).join('\n');
    });
    const targets = option?.targets || [];
    if (!targets.some(target => target.target_id === selectedRunTarget)) selectedRunTarget = targets[0]?.target_id || '';
    targetSelect.innerHTML = targets.map(target => `<option value="${escapeHtml(target.target_id)}" ${target.target_id === selectedRunTarget ? 'selected' : ''}>${escapeHtml(target.target_label || target.target_id)}</option>`).join('');
    targetSelect.disabled = !option?.runnable;
    const blocked = Number(option?.blocked_count || 0);
    blocker.textContent = option?.runnable
      ? `${option.runnable_count} 条用例可使用 ${option.platform_label} 逻辑运行`
      : blocked
        ? `${blocked} 条用例在 ${option?.platform_label || '所选平台'} 被门禁阻止`
        : '当前项目没有可用执行目标';
    run.disabled = !option?.runnable || !selectedRunTarget;
  } catch (error) {
    if (token !== executionOptionsToken) return;
    buttons.forEach(button => { button.disabled = true; });
    targetSelect.disabled = true;
    blocker.textContent = `平台校验失败：${error.message}`;
    run.disabled = true;
  }
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
      ? `已选 ${count} 条用例`
      : '尚未选择用例';
  }
  const clear = document.querySelector('#clear-selected-tests');
  if (clear) clear.disabled = count < 1;
  const run = document.querySelector('#run-selected-tests');
  if (run) {
    run.disabled = true;
    run.textContent = count ? `用已选用例创建批次（${count}）` : '用已选用例创建批次';
  }
  void refreshExecutionOptions();
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

function renderModuleSidebar(itemsOrCounts = [], activeCategory = 'all', activeModule = '') {
  const loading = itemsOrCounts === null;
  const counts = new Map();
  if (Array.isArray(itemsOrCounts)) {
    for (const item of itemsOrCounts) {
      const sheet = String(item.file_sheet || item.sheet || '未分类');
      counts.set(sheet, (counts.get(sheet) || 0) + 1);
    }
  } else if (itemsOrCounts) {
    for (const [sheet, value] of Object.entries(itemsOrCounts || {})) {
      counts.set(sheet, Number(value || 0));
    }
  }
  const modules = activeCategory === 'all' ? ALL_FUNCTION_MODULES : (FUNCTION_CATEGORIES[activeCategory] || []);
  const allTotal = [...counts.values()].reduce((total, value) => total + Number(value || 0), 0);
  const categoryTotal = modules.reduce((total, name) => total + Number(counts.get(name) || 0), 0);
  return `<button type="button" class="module-filter ${!activeModule ? 'is-active' : ''}" data-case-module=""><span>全部模块</span><strong>${loading ? '—' : (activeCategory === 'all' ? allTotal : categoryTotal)}</strong></button>${modules.map(name => `<button type="button" class="module-filter ${activeModule === name ? 'is-active' : ''}" data-case-module="${escapeHtml(name)}"><span>${escapeHtml(name)}</span><strong>${loading ? '—' : Number(counts.get(name) || 0)}</strong></button>`).join('')}`;
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
    const payload = await loadCasePage(project, {query, page, state, category, module});
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
    if (moduleSidebar) moduleSidebar.innerHTML = renderModuleSidebar(payload.module_counts, category, module);
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

async function renderCasePlatformLanding() {
  setActiveNav('cases');
  document.title = '选择用例平台 · Agent-loop';
  selectedTestCases.clear();
  currentTestPageItems = [];
  const w30Project = caseProjectForPlatform('w30');
  const platform579Project = caseProjectForPlatform('579');
  app.innerHTML = `
    ${Components.pageHeader({title: '用例管理', intro: '请先选择需要管理的测试平台；选择后才会读取对应平台的项目与用例'})}
    <section class="workspace-card case-platform-gateway" aria-labelledby="case-platform-gateway-title">
      <header><p class="eyebrow">平台入口</p><h2 id="case-platform-gateway-title">选择用例平台</h2><p>W30 与 579 使用独立的项目范围、自动化绑定和执行链路。</p></header>
      <div class="case-platform-gateway-grid">
        <form class="case-platform-entry is-w30" method="get" action="/cases" data-case-platform="w30">
          <input type="hidden" name="project" value="${escapeHtml(w30Project)}">
          <input type="hidden" name="platform_id" value="w30">
          <button class="case-platform-entry-submit" type="submit">
            <span class="case-platform-entry-code">W30</span>
            <strong>进入 W30 用例管理</strong>
            <small>管理 W30 模拟器与真机项目用例，执行时使用 W30 命令链路。</small>
          </button>
        </form>
        <form class="case-platform-entry is-579" method="get" action="/cases" data-case-platform="579">
          <input type="hidden" name="project" value="${escapeHtml(platform579Project)}">
          <input type="hidden" name="platform_id" value="579">
          <button class="case-platform-entry-submit" type="submit">
            <span class="case-platform-entry-code">579</span>
            <strong>进入 579 用例管理</strong>
            <small>管理 579 O2 用例，执行时使用 APP Bridge → BLE 与 O1/O2 证据链路。</small>
          </button>
        </form>
      </div>
    </section>`;
}

async function renderTests() {
  setActiveNav('cases');
  document.title = '用例管理 · Agent-loop';
  const requestedPlatform = new URLSearchParams(location.search).get('platform_id');
  if (!['w30', '579'].includes(requestedPlatform)) return renderCasePlatformLanding();
  const compatibleProject = caseProjectForPlatform(requestedPlatform, currentProject());
  if (!compatibleProject) return renderCasePlatformLanding();
  if (compatibleProject !== currentProject()) {
    history.replaceState({}, '', pageUrl('/cases', compatibleProject, {platform_id: requestedPlatform}));
  }
  rememberProject(compatibleProject);
  const initial = testListParams();
  const initialProject = testProject(initial.project);
  const initialPlatform = currentPlatformFor(initial.project);
  ensureTestSelectionProject(initial.project);
  if (!(initialProject.allowed_platforms || []).includes(selectedRunPlatform)) {
    selectedRunPlatform = initialPlatform;
    selectedRunTarget = '';
  }
  app.innerHTML = `
    ${Components.pageHeader({title: '用例管理', intro: '按功能模块组织、探索并固化自动化测试用例', actions: `<button id="open-case-create" class="button" type="button">${icon('plus', 17)} 新建用例</button>`})}
    <section id="active-test-batch" class="batch-active" hidden></section>
    <section class="workspace-card case-management-card">
      <div class="case-platform-context"><div><span>用例平台</span><div class="platform-choice-group">${caseManagementPlatformButtons(initialPlatform)}</div></div><small>切换平台后，只加载该平台下的项目与用例</small></div>
      <nav id="case-category-tabs" class="case-category-tabs" aria-label="功能分类">${renderCaseCategoryTabs(initial.category)}</nav>
      <div class="case-toolbar" aria-label="测试用例工具">
        <div class="project-stage">
          <label class="sr-only" for="test-project">项目与目标</label>
          <select id="test-project" aria-label="选择测试项目">
            ${caseProjectsForPlatform(initialPlatform).map(item => `<option value="${escapeHtml(item.project)}" ${item.project === initial.project ? 'selected' : ''}>${escapeHtml(item.projectLabel)} · ${escapeHtml(item.targetLabel)}</option>`).join('')}
          </select>
          <small id="test-project-target">${testTargetChip(initialProject)}</small>
        </div>
        <div class="search-box case-search-box">
          ${icon('search', 18)}
          <input id="test-search" type="search" value="${escapeHtml(initial.query)}" placeholder="搜索用例编号、测试项、测试点、模块" autocomplete="off">
          <button id="clear-test-search" class="clear-search" type="button" aria-label="清空搜索">清空</button>
        </div>
        <div class="case-toolbar-actions">
          <button id="open-excel-import" class="button button-secondary" type="button">导入 Excel</button>
          <button id="export-excel-btn" class="button button-secondary" type="button">导出 Excel</button>
          <button id="open-migration-btn" class="button button-secondary" type="button">跨端迁移</button>
        </div>
      </div>
      <div class="case-workspace-grid">
        <aside id="case-module-sidebar" class="module-sidebar" aria-label="功能模块">${renderModuleSidebar(null, initial.category, initial.module)}</aside>
        <div class="case-main-column">
          <section id="test-metrics" class="test-metric-groups" aria-label="测试用例统计与筛选">${testMetricCards(null, initial.state)}</section>
          <details class="batch-launch case-batch-launch">
            <summary><strong>按运行状态创建批次</strong><small id="batch-selection-note">正在统计可运行用例…</small></summary>
            <div class="batch-scope" aria-label="选择批次范围">
              ${Object.entries(BATCH_CATEGORY_LABELS).map(([value, label]) => `<label class="batch-option"><input type="checkbox" name="batch-category" value="${value}" checked><span>${label}</span><strong data-batch-count="${value}">0</strong></label>`).join('')}
            </div>
            <div class="batch-launch-actions"><button id="test-batch-button" class="button" type="button">读取可运行用例…</button></div>
          </details>
          <section class="panel queue-panel case-table-panel">
            <header class="panel-head">
              <div><h2 id="test-queue-title">${TEST_FILTER_LABELS[initial.state]}</h2><p>查看自动化状态和最近结果</p></div>
              <span id="test-count" class="chip chip-pending">正在读取</span>
            </header>
            <div id="test-list" class="defect-list"><div class="list-loading">正在读取测试用例…</div></div>
            <div id="test-pagination"></div>
          </section>
          <section class="test-selection-bar" aria-label="精确选择测试批次">
            <div class="test-selection-copy"><strong id="selected-test-summary">尚未勾选用例；勾选项可跨分页保留</strong><label class="test-page-selector"><input id="select-test-page" type="checkbox"><span>全选当前页</span></label></div>
            <div class="selected-run-routing"><span>运行平台</span><div id="run-platform-buttons" class="platform-choice-group">${runPlatformSwitchButtons(initial.project, initialPlatform, 'data-run-platform')}</div><label>执行目标<select id="run-target-select" aria-label="本次执行目标"></select></label><small id="run-platform-blocker">勾选用例后校验平台映射</small></div>
            <div class="test-selection-actions"><button id="clear-selected-tests" class="button button-secondary" type="button" disabled>清空</button><button id="run-selected-tests" class="button" type="button" disabled>运行所选</button></div>
          </section>
        </div>
      </div>
    </section>
    <dialog id="excel-import-dialog" class="import-dialog" aria-labelledby="excel-dialog-title">
      <form id="excel-import-form">
        <header class="dialog-head">
          <div><p class="eyebrow">导入测试用例</p><h2 id="excel-dialog-title">导入 Excel 测试用例</h2></div>
          <button id="close-excel-import" class="dialog-close" type="button" aria-label="关闭">×</button>
        </header>
        <div class="dialog-body">
          <div class="import-project-badge" style="background: var(--surface-soft); padding: 8px 12px; border-radius: 6px; font-size: 13px;">
            <strong>目标项目：</strong>
            <span id="excel-target-project-label"></span>
          </div>
          <fieldset class="platform-applicability-fieldset">
            <legend>适用平台 *</legend>
            <div id="excel-applicable-platforms" class="platform-applicability-options">${platformApplicabilityMarkup(initial.project, [initialPlatform], 'excel-applicable-platform')}</div>
            <small>导入前必须明确选择，平台只决定后续使用哪套自动化绑定与执行逻辑。</small>
          </fieldset>
          <div id="excel-dropzone" class="excel-upload-zone">
            <input id="excel-file-input" type="file" accept=".xlsx" style="display: none;">
            <div class="dropzone-content">
              <div class="dropzone-title" id="dropzone-title-text">选择或拖拽 Excel 文件（.xlsx）至此处</div>
              <small class="dropzone-subtitle" id="dropzone-subtitle-text">必须包含「自动化测试用例_v1」工作表与 9 列表头</small>
              <div class="dropzone-prompt" id="dropzone-file-name">点击选择文件</div>
            </div>
          </div>
          <div id="excel-preview-box" class="excel-preview-box" style="display: none;">
            <div class="preview-stats" style="display: flex; gap: 8px; margin-bottom: 8px;">
              <span class="chip chip-pass">新增用例：<strong id="preview-new-count">0</strong> 条</span>
              <span class="chip">已有用例：<strong id="preview-skip-count">0</strong> 条</span>
              <span class="chip">总计：<strong id="preview-total-count">0</strong> 条</span>
            </div>
            <p id="preview-modules-info" style="font-size: 12px; color: var(--muted); margin: 4px 0;"></p>
            <div id="preview-sample-list" style="max-height: 120px; overflow-y: auto; font-size: 12px; background: var(--surface-soft); padding: 8px; border-radius: 4px;"></div>
            <div class="import-mode-selector" style="margin-top: 10px; padding: 8px 12px; background: var(--surface-soft); border-radius: 6px; font-size: 13px;">
              <strong>已有用例：</strong>
              <div style="display: flex; gap: 16px; margin-top: 6px;">
                <label style="display: flex; align-items: center; gap: 4px; cursor: pointer;">
                  <input type="radio" name="excel-import-overwrite-mode" value="skip" checked>
                  <span>增量导入（跳过已存在）</span>
                </label>
                <label style="display: flex; align-items: center; gap: 4px; cursor: pointer;">
                  <input type="radio" name="excel-import-overwrite-mode" value="revision">
                  <span>创建新版本（保留历史和冻结基线）</span>
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
              <input id="case-input-id" type="text" class="input" placeholder="例如：CALC_002" required style="width: 100%; padding: 6px 10px; border-radius: 4px; border: 1px solid var(--border);">
            </div>
            <div style="flex: 1;">
              <label for="case-input-sheet" style="display: block; font-size: 12px; font-weight: bold; margin-bottom: 4px;">所属模块 *</label>
              <input id="case-input-sheet" type="text" class="input" placeholder="例如：计算器" required style="width: 100%; padding: 6px 10px; border-radius: 4px; border: 1px solid var(--border);">
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
          <div class="case-lifecycle-row">
            <fieldset class="platform-applicability-fieldset">
              <legend>适用平台 *</legend>
              <div id="case-input-platforms" class="platform-applicability-options">${platformApplicabilityMarkup(initial.project, [initialPlatform], 'case-applicable-platform')}</div>
            </fieldset>
            <label for="case-input-workflow"><span>用例状态</span><select id="case-input-workflow"><option value="DRAFT">草稿</option><option value="REVIEWING">评审中</option><option value="ACTIVE" selected>生效</option></select></label>
          </div>
          <div class="case-semantic-fields">
            <div>
              <label for="case-input-test-item">测试项 *</label>
              <input id="case-input-test-item" type="text" class="input" placeholder="例如：入口" required>
            </div>
            <div>
              <label for="case-input-test-point">测试点 *</label>
              <input id="case-input-test-point" type="text" class="input" placeholder="例如：点击入口进入计算器页面" required>
            </div>
          </div>
          <div>
            <label for="case-input-precondition" style="display: block; font-size: 12px; font-weight: bold; margin-bottom: 4px;">前置条件</label>
            <input id="case-input-precondition" type="text" class="input" placeholder="例如：手表已返回主表盘页面" style="width: 100%; padding: 6px 10px; border-radius: 4px; border: 1px solid var(--border);">
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
            <p class="eyebrow">跨端用例迁移</p>
            <h2 id="migration-dialog-title">6202 模拟器 → 6202 真机用例迁移</h2>
          </div>
          <button id="close-migration-dialog" class="dialog-close" type="button" aria-label="关闭">×</button>
        </header>
        <div class="dialog-body" style="display: flex; flex-direction: column; gap: 12px; max-height: 65vh; overflow-y: auto;">
          <div style="background: var(--surface-soft); padding: 10px 14px; border-radius: 6px; font-size: 13px; color: var(--ink-soft);">
            <strong>迁移说明：</strong>
            只迁移已验证的模拟器用例。真机会重新执行并采集截图；两端用例内容不一致时不会迁移。
          </div>
          <div id="migration-loading" style="text-align: center; padding: 20px;">
            <div class="spinner" style="margin: 0 auto 8px;"></div>
            <p style="font-size: 13px; color: var(--ink-soft);">正在检查可迁移用例…</p>
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
    const project = rememberProject(projectSelect.value);
    ensureTestSelectionProject(project);
    selectedRunPlatform = '';
    selectedRunTarget = '';
    history.pushState({}, '', pageUrl('/cases', project, {platform_id: initialPlatform}));
    route();
  });
  document.querySelectorAll('[data-run-platform]').forEach(button => button.addEventListener('click', () => {
    if (button.disabled) return;
    selectedRunPlatform = button.dataset.runPlatform;
    selectedRunTarget = '';
    void refreshExecutionOptions();
  }));
  document.querySelector('#run-target-select')?.addEventListener('change', event => {
    selectedRunTarget = event.target.value;
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
    const platformLabel = PLATFORM_PROFILES[selectedRunPlatform]?.platform_label || selectedRunPlatform;
    const targetLabel = PLATFORM_TARGETS[selectedRunTarget]?.target_label || selectedRunTarget;
    if (!window.confirm(`项目：${projectMeta.projectLabel}\n运行平台：${platformLabel}\n执行目标：${targetLabel}\n用例数量：${cases.length}\n运行模式：确定性执行\n\n本批次只运行勾选项，平台不可用时不会自动改用另一套逻辑。`)) return;
    if (!confirm579Watchface(projectSelect.value)) return;
    button.disabled = true;
    button.textContent = '正在创建批次…';
    try {
      const job = await api('/api/tests/run-batch', {
        method: 'POST',
        body: JSON.stringify({
          project_id: projectSelect.value,
          platform_id: selectedRunPlatform,
          target_id: selectedRunTarget,
          run_mode: 'deterministic',
          cases,
          watchface_ready: projectSelect.value === '579_Z1640'
        }),
      });
      window.location.href = testBatchHref(job.id);
    } catch (error) {
      showToast(error.message, error.status === 409 ? 'warning' : 'error');
      updateSelectedTestCasesUi();
    }
  });
  document.querySelector('#test-batch-button').addEventListener('click', async () => {
    const button = document.querySelector('#test-batch-button');
    const categories = selectedBatchCategories();
    const total = categories.reduce(
      (sum, category) => sum + Number(latestBatchCandidateSummary[category] || 0),
      0
    );
    if (!categories.length || total < 1) {
      showToast('请至少选择一类有用例的测试范围', 'warning');
      return;
    }
    const projectMeta = testProject(projectSelect.value);
    const platformId = selectedRunPlatform || currentPlatformFor(projectSelect.value);
    const targetId = selectedRunTarget || targetsFor(projectSelect.value, platformId)[0]?.target_id || '';
    const platformLabel = PLATFORM_PROFILES[platformId]?.platform_label || platformId;
    const targetLabel = PLATFORM_TARGETS[targetId]?.target_label || targetId;
    if (!window.confirm(`项目：${projectMeta.projectLabel}\n运行平台：${platformLabel}\n执行目标：${targetLabel}\n用例数量：${total}\n范围：${labels.join('、')}\n\n最新结果已通过的用例不会重跑，平台不可用时不会自动回退。`)) return;
    if (!confirm579Watchface(projectSelect.value)) return;
    button.disabled = true;
    button.textContent = '正在创建批次…';
    try {
      const job = await api('/api/tests/run-batch', {
        method: 'POST',
        body: JSON.stringify({
          limit: 0,
          categories,
          project_id: projectSelect.value,
          platform_id: platformId,
          target_id: targetId,
          run_mode: 'deterministic',
          watchface_ready: projectSelect.value === '579_Z1640'
        })
      });
      window.location.href = testBatchHref(job.id);
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
  let currentExcelFile = null;
  let currentExcelPreview = null;

  function resetExcelDialog() {
    currentExcelBase64 = null;
    currentExcelFileName = '';
    currentExcelFile = null;
    currentExcelPreview = null;
    if (excelFileInput) excelFileInput.value = '';
    if (dropzoneTitle) dropzoneTitle.textContent = '选择或拖拽 Excel 文件（.xlsx）至此处';
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
    currentExcelPreview = null;
    if (!file.name.toLowerCase().endsWith('.xlsx')) {
      if (excelErrorBox) {
        excelErrorBox.textContent = `仅支持 .xlsx 文件，无法导入「${file.name}」。`;
        excelErrorBox.style.display = 'block';
      }
      if (dropzoneFileName) dropzoneFileName.textContent = `不支持格式：${file.name}`;
      if (submitExcelBtn) {
        submitExcelBtn.disabled = true;
        submitExcelBtn.textContent = '无法导入';
      }
      return;
    }

    currentExcelFile = file;
    currentExcelFileName = file.name;
    if (dropzoneFileName) dropzoneFileName.textContent = `已选择：${file.name}`;
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
        const applicablePlatforms = selectedPlatformValues('excel-applicable-platform');
        if (!applicablePlatforms.length) {
          throw new Error('请至少选择一个适用平台');
        }
        const conflictStrategy = document.querySelector('input[name="excel-import-overwrite-mode"]:checked')?.value === 'revision' ? 'NEW_REVISION' : 'SKIP';
        const resp = await fetch('/api/cases/import/preview', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({
            project_id: projectSelect.value,
            filename: currentExcelFileName,
            file_base64: currentExcelBase64,
            applicable_platforms: applicablePlatforms,
            conflict_strategy: conflictStrategy,
          }),
        });
        const data = await resp.json();
        if (!resp.ok) {
          excelErrorBox.textContent = `${data.row_number ? `第 ${data.row_number} 行：` : ''}${data.message || data.error || '文件解析失败'}`;
          excelErrorBox.style.display = 'block';
          submitExcelBtn.textContent = '无法导入';
          submitExcelBtn.disabled = true;
          return;
        }
        currentExcelPreview = data;
        document.querySelector('#preview-new-count').textContent = String(data.new_count);
        document.querySelector('#preview-skip-count').textContent = String(data.existing_count);
        document.querySelector('#preview-total-count').textContent = String(data.total_parsed);
        document.querySelector('#preview-modules-info').textContent = `涉及模块：${data.modules.join('、')}`;

        const sampleHtml = (data.new_cases || []).slice(0, 5).map(c => `<div><strong>${escapeHtml(c.case_id)}</strong>（${escapeHtml(c.sheet)}）· ${escapeHtml((c.expected_text || '').slice(0, 30))}</div>`).join('') || '<div style="color: var(--muted);">本次无新增用例（全部已存在）</div>';
        document.querySelector('#preview-sample-list').innerHTML = sampleHtml;
        excelPreviewBox.style.display = 'block';

        if (data.new_count > 0 || (conflictStrategy === 'NEW_REVISION' && data.existing_count > 0)) {
          submitExcelBtn.disabled = false;
          submitExcelBtn.textContent = conflictStrategy === 'NEW_REVISION'
            ? `确认导入（${data.new_count} 条新增，${data.existing_count} 条新版本）`
            : `确认导入 (${data.new_count} 条新增)`;
        } else {
          submitExcelBtn.disabled = true;
          submitExcelBtn.textContent = '无需导入（无新增）';
        }
      } catch (err) {
        excelErrorBox.textContent = `暂时无法解析文件：${err.message}`;
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
        if (dropzoneTitle) dropzoneTitle.textContent = '选择或拖拽 Excel 文件（.xlsx）至此处';
      });
    });

    excelDropzone.addEventListener('drop', e => {
      e.preventDefault();
      e.stopPropagation();
      excelDropzone.classList.remove('is-dragover');
      if (dropzoneTitle) dropzoneTitle.textContent = '选择或拖拽 Excel 文件（.xlsx）至此处';
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

  document.querySelectorAll('input[name="excel-import-overwrite-mode"], input[name="excel-applicable-platform"]').forEach(control => {
    control.addEventListener('change', () => {
      if (currentExcelFile) handleExcelFile(currentExcelFile);
    });
  });

  if (submitExcelBtn) {
    submitExcelBtn.addEventListener('click', async () => {
      if (!currentExcelBase64) return;
      const createsRevision = document.querySelector('input[name="excel-import-overwrite-mode"]:checked')?.value === 'revision';
      if (!currentExcelPreview) return;
      submitExcelBtn.disabled = true;
      submitExcelBtn.textContent = '正在写入统一用例库…';
      try {
        const resp = await fetch('/api/cases/import/commit', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({
            project_id: projectSelect.value,
            batch_id: currentExcelPreview.batch_id,
            preview_token: currentExcelPreview.preview_token,
            source_sha256: currentExcelPreview.source_sha256,
          }),
        });
        const data = await resp.json();
        if (!resp.ok) {
          excelErrorBox.textContent = `导入失败：${productApiError(data.message || data.error || '未知错误', resp.status, data.error_code)}`;
          excelErrorBox.style.display = 'block';
          submitExcelBtn.textContent = '重试导入';
          submitExcelBtn.disabled = false;
          return;
        }
        const summaryMsg = createsRevision
          ? `导入成功！新增 ${data.imported_count} 条，创建新版本 ${data.new_revision_count || 0} 条。模块: ${(data.modules_updated || []).join(', ')}`
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
        excelErrorBox.textContent = `导入失败：${err.message}`;
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
      showToast(`${pLabel} 测试用例表已开始下载`);
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
  const caseInputTestItem = document.querySelector('#case-input-test-item');
  const caseInputTestPoint = document.querySelector('#case-input-test-point');
  const caseInputPrecondition = document.querySelector('#case-input-precondition');
  const caseInputSteps = document.querySelector('#case-input-steps');
  const caseInputExpected = document.querySelector('#case-input-expected');
  const caseInputNote = document.querySelector('#case-input-note');
  const caseInputPlatforms = document.querySelector('#case-input-platforms');
  const caseInputWorkflow = document.querySelector('#case-input-workflow');
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
    const defaultPlatforms = [currentPlatformFor(projectSelect.value)];
    let applicablePlatforms = Array.isArray(data.applicable_platforms) ? data.applicable_platforms : defaultPlatforms;
    if (caseInputPlatforms) caseInputPlatforms.innerHTML = platformApplicabilityMarkup(projectSelect.value, applicablePlatforms, 'case-applicable-platform');
    if (caseInputWorkflow) caseInputWorkflow.value = data.workflow_state || 'ACTIVE';

    if (mode === 'create') {
      if (caseDialogTitle) caseDialogTitle.textContent = '添加测试用例';
      if (caseInputId) {
        caseInputId.value = '';
        caseInputId.readOnly = false;
      }
      if (caseInputSheet) caseInputSheet.value = data.sheet || '';
      if (caseInputPriority) caseInputPriority.value = 'P1';
      if (caseInputTestItem) caseInputTestItem.value = '';
      if (caseInputTestPoint) caseInputTestPoint.value = '';
      if (caseInputPrecondition) caseInputPrecondition.value = '';
      if (caseInputSteps) caseInputSteps.value = '';
      if (caseInputExpected) caseInputExpected.value = '';
      if (caseInputNote) caseInputNote.value = '';
      if (caseEditSubmitBtn) caseEditSubmitBtn.textContent = '保存用例';
    } else {
      if (caseDialogTitle) caseDialogTitle.textContent = data.source_locked
        ? `为冻结用例创建新版本 (${data.case_id || ''})`
        : `编辑测试用例 (${data.case_id || ''})`;
      if (caseInputId) {
        caseInputId.value = data.case_id || '';
        caseInputId.readOnly = true;
      }
      if (caseInputSheet) caseInputSheet.value = data.sheet || '';
      if (caseInputPriority) caseInputPriority.value = data.priority || 'P1';
      if (caseInputTestItem) caseInputTestItem.value = data.test_item || '';
      if (caseInputTestPoint) caseInputTestPoint.value = data.test_point || '';
      if (caseInputPrecondition) caseInputPrecondition.value = data.precondition_text || '';
      if (caseInputSteps) caseInputSteps.value = data.steps_text || '';
      if (caseInputExpected) caseInputExpected.value = data.expected_text || '';
      if (caseInputNote) caseInputNote.value = data.note || '';
      if (caseEditSubmitBtn) caseEditSubmitBtn.textContent = data.source_locked ? '创建新版本' : '保存新版本';
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
          test_item: editBtn.dataset.testItem,
          test_point: editBtn.dataset.testPoint,
          precondition_text: editBtn.dataset.precondition,
          steps_text: editBtn.dataset.steps,
          expected_text: editBtn.dataset.expected,
          note: editBtn.dataset.note,
          applicable_platforms: JSON.parse(editBtn.dataset.applicablePlatforms || '[]'),
          workflow_state: editBtn.dataset.workflowState || 'ACTIVE',
          source_locked: editBtn.dataset.sourceLocked === 'true',
        });
        return;
      }
      const summaryRow = e.target.closest('[data-case-expand]');
      if (!summaryRow || e.target.closest('a, button, input, label, select')) return;
      toggleCaseInlineDetail(summaryRow);
    });
    list.addEventListener('keydown', e => {
      if (!['Enter', ' '].includes(e.key) || e.target.closest('a, button, input, label, select')) return;
      const summaryRow = e.target.closest('[data-case-expand]');
      if (!summaryRow) return;
      e.preventDefault();
      toggleCaseInlineDetail(summaryRow);
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
      const test_item = caseInputTestItem.value.trim();
      const test_point = caseInputTestPoint.value.trim();
      const precondition_text = caseInputPrecondition.value.trim();
      const steps_text = caseInputSteps.value.trim();
      const expected_text = caseInputExpected.value.trim();
      const note = caseInputNote.value.trim();
      const applicable_platforms = selectedPlatformValues('case-applicable-platform');
      const workflow_state = caseInputWorkflow?.value || 'ACTIVE';

      if (!caseId || !/^[A-Za-z0-9_.-]+$/.test(caseId)) {
        if (caseEditErrorBox) {
          caseEditErrorBox.textContent = '用例编号格式错误：只能包含字母、数字、点、下划线和连字符';
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
      if (!test_item || !test_point) {
        if (caseEditErrorBox) {
          caseEditErrorBox.textContent = '测试项和测试点为必填项，且必须分别描述验证主题与具体检查目标';
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
      if (!applicable_platforms.length) {
        if (caseEditErrorBox) {
          caseEditErrorBox.textContent = '请至少选择一个适用平台';
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
        const endpoint = mode === 'create'
          ? '/api/cases'
          : `/api/cases/${encodeURIComponent(origId)}/revisions`;
        const payload = {
          project_id: projectSelect.value,
          case: {
            case_id: caseId,
            sheet,
            priority,
            test_item,
            test_point,
            precondition_text,
            steps_text,
            expected_text,
            note,
            applicable_platforms,
            workflow_state,
          },
          change_summary: mode === 'create' ? '网页新增' : '网页编辑创建新版本',
        };

        const resp = await fetch(endpoint, {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify(payload),
        });
        const resData = await resp.json();
        if (!resp.ok) {
          if (caseEditErrorBox) {
            caseEditErrorBox.textContent = `保存失败：${productApiError(resData.error || resData.message || '未知错误', resp.status, resData.reason_code)}`;
            caseEditErrorBox.style.display = 'block';
          }
          if (caseEditSubmitBtn) {
            caseEditSubmitBtn.disabled = false;
            caseEditSubmitBtn.textContent = mode === 'create' ? '保存用例' : '保存新版本';
          }
          return;
        }

        showToast(mode === 'create' ? `用例 ${caseId} 添加成功！` : `用例 ${caseId} 已创建新版本！`);
        caseEditDialog.close();
        invalidateCaseCatalog(projectSelect.value);
        const params = testListParams();
        refreshTests(input.value.trim(), 1, {state: params.state, project: projectSelect.value, category: params.category, module: params.module});
      } catch (err) {
        if (caseEditErrorBox) {
          caseEditErrorBox.textContent = `保存失败：${err.message}`;
          caseEditErrorBox.style.display = 'block';
        }
      } finally {
        if (caseEditSubmitBtn) {
          caseEditSubmitBtn.disabled = false;
          caseEditSubmitBtn.textContent = mode === 'create' ? '保存用例' : '保存新版本';
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
        migrationSummaryText.textContent = `共 ${list.length} 条已验证用例（可迁移 ${readyList.length} 条，内容不一致 ${divergedList.length} 条，真机已有 ${alreadyList.length} 条）`;
      }

      if (migrateAllReadyBtn) {
        migrateAllReadyBtn.disabled = readyList.length === 0;
        migrateAllReadyBtn.textContent = `迁移全部就绪用例（${readyList.length} 条）`;
      }

      if (!list.length) {
        migrationTbody.innerHTML = `<tr><td colspan="4" style="text-align: center; padding: 20px; color: var(--ink-soft);">暂无可迁移的模拟器用例</td></tr>`;
      } else {
        migrationTbody.innerHTML = list.map(c => {
          let statusBadge = '';
          let actionBtn = '';
          if (c.status === 'READY') {
            statusBadge = '<span class="chip chip-pass" style="font-size: 11px;">可迁移</span>';
            actionBtn = `<button class="button button-secondary migrate-single-btn" data-case-id="${escapeHtml(c.case_id)}" data-sheet="${escapeHtml(c.sheet)}" style="padding: 2px 8px; font-size: 11px;">迁移并验证</button>`;
          } else if (c.status === 'TARGET_MISSING') {
            statusBadge = '<span class="chip chip-running" style="font-size: 11px;">真机待创建</span>';
            actionBtn = `<button class="button button-secondary migrate-single-btn" data-case-id="${escapeHtml(c.case_id)}" data-sheet="${escapeHtml(c.sheet)}" style="padding: 2px 8px; font-size: 11px;">创建并验证</button>`;
          } else if (c.status === 'DIVERGED') {
            statusBadge = `<span class="chip chip-fail" style="font-size: 11px;" title="${escapeHtml(c.divergence_reason || '两端内容不一致')}">内容不一致</span>`;
            actionBtn = `<span class="muted" style="font-size: 11px;">请先统一内容</span>`;
          } else if (c.status === 'ALREADY_PROMOTED') {
            statusBadge = '<span class="chip" style="font-size: 11px;">真机已有</span>';
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
        migrationLoading.innerHTML = `<p style="color: #d32f2f;">无法读取可迁移用例：${escapeHtml(err.message)}</p>`;
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
          if (!resp.ok) throw new Error(productApiError(resData.error || resData.message || '迁移启动失败', resp.status, resData.reason_code));
          showToast(`用例 ${caseId} 已开始迁移`);
          migrationDialog.close();
          window.location.href = testDetailHref('6202_W5230', sheet, caseId, currentRouteUrl());
        } catch (err) {
          showToast(err.message, 'error');
          btn.disabled = false;
          btn.textContent = '迁移并验证';
        }
      }
    });
  }

  if (migrateAllReadyBtn) {
    migrateAllReadyBtn.addEventListener('click', async () => {
      const singleBtns = migrationTbody.querySelectorAll('.migrate-single-btn');
      if (!singleBtns.length) return;
      if (!window.confirm(`将把 ${singleBtns.length} 条用例迁移到 6202 真机并依次验证。继续吗？`)) return;

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
        showToast(`${singleBtns.length} 条用例已加入迁移队列`);
        migrationDialog.close();
        invalidateCaseCatalog('6202_W5230');
        refreshTests('', 1, {state: 'all', project: '6202_W5230', category: 'all', module: ''});
      } catch (err) {
        showToast(`批量迁移失败：${err.message}`, 'error');
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
      return `<article class="batch-active-row"><div><strong>${escapeHtml(projectMeta.projectLabel)} ${escapeHtml(projectMeta.targetLabel)}批次正在运行</strong><span>${Number(job.completed || 0)} / ${Number(job.total || 0)} 条已完成</span></div><a class="button button-secondary" href="${escapeHtml(testBatchHref(job.id))}">查看实时进度</a></article>`;
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
  return `<article class="case-text verification-points"><span>检查点</span><ol>${values.map(value => `<li>${escapeHtml(value)}</li>`).join('')}</ol></article>`;
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
  final: '结束处理'
};

const TRACE_SOURCE_LABELS = {
  case: '用例命令',
  runner: '系统补充'
};

function actualCommandTrace(items = []) {
  const values = Array.isArray(items) ? items.filter(item => item && typeof item === 'object') : [];
  if (!values.length) {
    return '<div class="legacy-missing">本次记录没有保存执行步骤。下面仅显示运行计划，不能证明已经执行。</div>';
  }
  const phases = [...new Set(values.map(item => item.phase || 'unknown'))];
  return phases.map(phase => {
    const entries = values.filter(item => (item.phase || 'unknown') === phase);
    return `<section class="command-phase command-trace-phase">
      <header><div><strong>${escapeHtml(TRACE_PHASE_LABELS[phase] || phase)}</strong><small>按执行顺序</small></div><span class="chip chip-pending">${entries.length} 条</span></header>
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
    return '<div class="legacy-missing">旧记录没有完整的证据信息，无法确认操作、检查点和截图是否齐全。</div>';
  }
  const issues = Array.isArray(contract.issues) ? contract.issues : [];
  const complete = contract.complete === true;
  return `<div class="notice ${complete ? '' : 'notice-error'}">
    <strong>${complete ? '证据完整' : '证据不完整'}</strong>
    <p>检查点截图 ${Number(contract.captured_screenshots || 0)} / ${Number(contract.required_screenshots || 0)}；业务操作 ${Number(contract.business_action_count || 0)}；执行步骤 ${Number(contract.attempted_action_count || 0)} / ${Number(contract.planned_action_count || 0)}</p>
    ${issues.length ? `<ul>${issues.map(item => `<li>${escapeHtml(item?.message || '有一项证据未完成')}</li>`).join('')}</ul>` : ''}
  </div>`;
}

function testResultReason(record = {}, fallback = '未记录判定理由') {
  const detail = record.reason || record.error || record.interruption_reason || '';
  const workflowStatus = String(record.workflow_status || record.status || '').toLowerCase();
  const verdict = String(record.verdict || '').toUpperCase();
  if ((!workflowStatus || workflowStatus === 'completed') && ['PASS', 'FAIL', 'CANNOT_VERIFY'].includes(verdict)) {
    return friendlyAgentError(detail || fallback) || fallback;
  }
  return issuePresentation({
    fallback,
    reasonCode: record.reason_code || record.error_code,
    detail
  }).summary;
}

function testHistoryRows(items, project, sheet, caseId, returnTo = '/tests') {
  if (!items?.length) return '<p class="muted">还没有测试历史。完成第一次运行后，结果会出现在这里。</p>';
  const detailReturnTo = testDetailHref(project, sheet, caseId, returnTo);
  return `<div class="history-list">${items.map(item => `
    <a class="history-row" href="${escapeHtml(testHistoryHref(project, sheet, caseId, item.id, detailReturnTo))}">
      ${resultChip(item.verdict)}
      <span><strong>${escapeHtml(testResultReason(item))}</strong><small>${item.has_screenshot ? `${item.screenshot_count || 1} 张${screenshotLabel(item)}` : '无截图'} · ${escapeHtml(item.execution_target_label || testProject(project).targetLabel)}</small></span>
      <time>${formatTime(item.timestamp)}</time>
      <span class="row-arrow" aria-hidden="true">›</span>
    </a>`).join('')}</div>`;
}

async function renderTest(sheet, caseId) {
  setActiveNav('cases');
  const routeToken = routeRequestToken;
  const project = testListParams().project;
  const projectMeta = testProject(project);
  let detailPlatform = currentPlatformFor(project);
  let detailTarget = '';
  const returnTo = testReturnUrl();
  const returnLabel = returnDestinationLabel(returnTo, '用例管理');
  const [testCase, executionOptions] = await Promise.all([
    api(`/api/tests/${encodeURIComponent(sheet)}/${encodeURIComponent(caseId)}?project=${encodeURIComponent(project)}&platform_id=${encodeURIComponent(detailPlatform)}`),
    api(`/api/projects/${encodeURIComponent(project)}/execution-options?case_ids=${encodeURIComponent(caseId)}`)
  ]);
  if (routeToken !== routeRequestToken) return;
  document.title = `${testCase.case_id} · Agent 测试`;
  const latest = testCase.history?.[0];
  const initialVerdict = latest?.verdict || 'PENDING';
  const usesFixedMapping = Boolean(testCase.is_fixed_runnable || testCase.is_promoted);
  const executionOnly579 = project === '579_Z1640';
  const executionBlocked = Boolean(testCase.is_execution_blocked || testCase.mapping_status === 'BLOCKED');
  const blockReasonCode = testCase.block_reason_code || 'WATCH_579_EXECUTION_BLOCKED';
  app.innerHTML = `
    <a class="back-link test-list-back-link" data-return-link href="${escapeHtml(returnTo)}">← 返回${escapeHtml(returnLabel)}</a>
    <header class="page-header">
      <div>
        <p class="eyebrow">${escapeHtml(testCase.sheet)} · 自动化测试 · ${escapeHtml(testCase.execution_target_label)}</p>
        <h1 class="page-title detail-title">${escapeHtml(testCase.case_id)}</h1>
        <div class="meta-line">${testTargetChip(testCase)}<span class="chip chip-status">${escapeHtml(testCase.priority || '未分级')}</span>${testCase.batch_id ? `<span class="chip chip-status">${escapeHtml(testCase.batch_id)}</span>` : ''}${caseStatusChip({...testCase, latest_verdict: initialVerdict})}<span class="muted small">${testCase.history?.length || 0} 次历史运行</span></div>
      </div>
    </header>
    <div class="detail-grid">
      <div class="stack">
        <section class="panel">
          <header class="panel-head"><div><h2>用例内容</h2><p>完整测试语义与最近实际结果</p></div></header>
          <div class="panel-body case-text-grid">
            ${caseText('测试项', testCase.test_item || '待补充')}
            ${caseText('测试点', testCase.test_point || '待补充')}
            ${caseText('前置条件', testCase.precondition_text)}
            ${caseText('操作步骤', testCase.steps_text)}
            ${caseText('预期结果', testCase.expected_text)}
            ${caseText('实际结果', testCase.actual_result || (latest ? workspaceVerdictLabel(latest.product_verdict || latest.verdict) : '尚未运行'))}
            ${verificationPoints(testCase.verification_points)}
          </div>
        </section>
        ${executionBlocked ? `<section class="panel">
          <header class="panel-head"><div><h2>当前固件入口阻塞</h2><p>${escapeHtml(blockReasonCode)}</p></div></header>
          <div class="panel-body command-phases"><div class="notice notice-warning"><strong>本批计算器用例暂不可启动</strong><p>真机已确认菜单右滑能够返回表盘，但当前 OTA 固件不响应表盘侧键入口。平台已阻止单条、批次和候选复跑，且不会退回 08/96。</p></div></div>
        </section>` : usesFixedMapping ? `<details class="panel collapsible-panel">
          <summary class="panel-head"><div><h2>自动化步骤</h2><p>${testCase.is_execution_ready ? '动作已保存，观察证据待补齐' : '已保存，可直接运行'}</p></div><span class="collapse-controls"><span class="collapse-action" aria-hidden="true"></span></span></summary>
          <div class="panel-body command-phases">${commandPhase('准备环境', '运行前', testCase.setup)}${commandPhase('执行操作', '测试步骤', testCase.actions)}${commandPhase('采集证据', '检查结果', testCase.collect)}</div>
        </details>` : `<section class="panel">
          <header class="panel-head"><div><h2>首次运行</h2><p>本次将根据用例内容尝试执行</p></div></header>
          <div class="panel-body command-phases"><div class="notice"><strong>本次将尝试生成自动化步骤</strong><p>结果只用于本次运行，不会自动保存。验证通过后可手动保存为可复用步骤。</p></div></div>
        </section>`}
        ${testCase.note ? `<section class="panel"><header class="panel-head"><div><h2>补充说明</h2></div></header><div class="panel-body prose">${escapeHtml(testCase.note)}</div></section>` : ''}
      </div>
      <aside class="panel repair-panel">
        <header class="panel-head"><div><h2>启动测试</h2><p>${escapeHtml(testCase.project_label)} · ${escapeHtml(testCase.execution_target_label)}</p></div></header>
        <form id="test-run-form" class="panel-body">
          <div class="detail-run-routing">
            <span>本次运行平台</span>
            <div class="platform-choice-group">${runPlatformSwitchButtons(project, detailPlatform, 'data-detail-platform')}</div>
            <label>执行目标<select id="detail-run-target" aria-label="本次执行目标"></select></label>
            <small id="detail-run-blocker">正在校验平台映射…</small>
          </div>
          <ul class="run-notes">
            <li>${executionBlocked ? `已阻止执行：${escapeHtml(blockReasonCode)}` : usesFixedMapping ? '按已保存步骤运行' : '根据用例内容尝试执行'}</li>
            ${executionOnly579
              ? '<li>记录每条 TX 与 L1 ACK，不将 ACK 当作业务效果</li><li>当前无截图通道，结果固定为观察不完整</li>'
              : `<li>在每个检查点采集${escapeHtml(screenshotLabel(testCase))}</li><li>根据截图判定并保存结果</li>`}
          </ul>
          <button id="test-run-button" class="button button-wide" type="submit" ${executionBlocked ? 'disabled' : ''}>${executionBlocked ? '当前固件入口阻塞' : '启动测试'}</button>
          <button id="test-cancel-button" class="button button-danger button-wide" type="button" hidden>取消当前测试</button>
          ${!executionBlocked && !usesFixedMapping && testCase.history?.length ? `<button id="test-promote-button" class="button button-secondary button-wide" type="button" style="margin-top: 8px;">验证并保存步骤</button>` : ''}
          ${project === '6202_W5230' ? `<button id="test-migrate-button" class="button button-secondary button-wide" type="button" style="margin-top: 8px;">从 6202 模拟器迁移</button>` : ''}
          <p class="form-note">当前目标一次只能运行一个测试或修复任务。</p>
        </form>
      </aside>
    </div>
    <section class="panel" id="test-workflow-panel">
      <header class="panel-head"><div><h2>测试进度</h2><p>启动后可在这里查看进度</p></div><span id="test-job-chip" class="chip chip-pending">等待启动</span></header>
      <div class="panel-body">
        ${testWorkflowSvg()}
        <div class="workflow-status"><div id="test-job-message" class="muted small">等待启动</div><div class="legend"><span><i></i>待执行</span><span><i class="blue"></i>执行中</span><span><i class="green"></i>完成</span><span><i class="red"></i>失败</span></div></div>
      </div>
    </section>
    <section class="panel">
      <header class="panel-head"><div><h2>测试历史</h2><p>每次运行的结果和截图</p></div></header>
      <div id="test-history-body" class="panel-body">${testHistoryRows(testCase.history, project, sheet, caseId, returnTo)}</div>
    </section>`;

  const detailOptions = executionOptions.options || [];
  const applyDetailOption = platformId => {
    detailPlatform = String(platformId || '');
    const option = detailOptions.find(item => item.platform_id === detailPlatform);
    const targetSelect = document.querySelector('#detail-run-target');
    const blocker = document.querySelector('#detail-run-blocker');
    const runButton = document.querySelector('#test-run-button');
    document.querySelectorAll('[data-detail-platform]').forEach(button => {
      const match = detailOptions.find(item => item.platform_id === button.dataset.detailPlatform);
      button.disabled = !match;
      button.classList.toggle('is-active', button.dataset.detailPlatform === detailPlatform);
      button.title = !match
        ? `当前项目未启用 ${PLATFORM_PROFILES[button.dataset.detailPlatform]?.platform_label || button.dataset.detailPlatform}`
        : match.runnable ? '' : (match.blockers || []).map(item => `${item.case_id}: ${item.reason}`).join('\n');
    });
    const targets = option?.targets || [];
    if (!targets.some(target => target.target_id === detailTarget)) detailTarget = targets[0]?.target_id || '';
    targetSelect.innerHTML = targets.map(target => `<option value="${escapeHtml(target.target_id)}" ${target.target_id === detailTarget ? 'selected' : ''}>${escapeHtml(target.target_label || target.target_id)}</option>`).join('');
    targetSelect.disabled = !option?.runnable;
    blocker.textContent = option?.runnable
      ? `将使用 ${option.platform_label} 逻辑运行`
      : (option?.blockers || []).map(item => item.reason_label || item.reason).join('；') || '当前平台没有可用执行目标';
    runButton.disabled = !option?.runnable || !detailTarget;
  };
  document.querySelectorAll('[data-detail-platform]').forEach(button => button.addEventListener('click', () => applyDetailOption(button.dataset.detailPlatform)));
  document.querySelector('#detail-run-target')?.addEventListener('change', event => { detailTarget = event.target.value; });
  applyDetailOption(detailPlatform);

  const promoteBtn = document.querySelector('#test-promote-button');
  if (promoteBtn) {
    promoteBtn.addEventListener('click', async () => {
      promoteBtn.disabled = true;
      promoteBtn.textContent = '正在验证步骤…';
      try {
        const resp = await fetch('/api/cases/audit-and-promote', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({case_id: caseId, sheet, project}),
        });
        const resData = await resp.json();
        if (!resp.ok) throw new Error(productApiError(resData.error || resData.message || (resData.audit?.issues || []).join('; ') || '步骤验证未通过', resp.status, resData.reason_code));
        if (resData.status === 'candidate_replay_started' && resData.job?.id) {
          updateTestWorkflow({});
          const chip = document.querySelector('#test-job-chip');
          chip.className = 'chip chip-running';
          chip.textContent = '正在验证步骤';
          document.querySelector('#test-job-message').textContent = '正在重新运行并验证自动化步骤；通过后会保存为可复用步骤。';
          pollTestJob(resData.job.id, project, sheet, caseId, true);
          return;
        }
        showToast('自动化步骤已保存');
        window.location.reload();
      } catch (err) {
        showToast(err.message, 'error');
        promoteBtn.disabled = false;
        promoteBtn.textContent = '验证并保存步骤';
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
        if (!resp.ok) throw new Error(productApiError(resData.error || resData.message || '迁移启动失败', resp.status, resData.reason_code));
        showToast(`用例 ${caseId} 已开始迁移`);
        window.location.reload();
      } catch (err) {
        showToast(err.message, 'error');
        migrateBtn.disabled = false;
        migrateBtn.textContent = '从 6202 模拟器迁移';
      }
    });
  }

  document.querySelector('#test-run-form').addEventListener('submit', async event => {
      event.preventDefault();
      if (executionBlocked) {
        showToast(`${blockReasonCode}: 当前固件入口阻塞`, 'warning');
        return;
      }
      if (!confirm579Watchface(project)) return;
      const button = document.querySelector('#test-run-button');
      button.disabled = true;
      button.textContent = '正在启动…';
      try {
        const job = await api('/api/tests/run', {
          method: 'POST',
          body: JSON.stringify({
            project_id: project,
            platform_id: detailPlatform,
            target_id: detailTarget,
            sheet,
            case_id: caseId,
            watchface_ready: project === '579_Z1640'
          })
        });
        updateTestWorkflow({});
        const chip = document.querySelector('#test-job-chip');
        chip.className = 'chip chip-running';
        chip.textContent = '正在启动';
        document.querySelector('#test-job-message').textContent = `任务 ${job.id} 已创建，正在启动 ${PLATFORM_PROFILES[detailPlatform]?.platform_label || detailPlatform} / ${PLATFORM_TARGETS[detailTarget]?.target_label || detailTarget} 链路…`;
        showToast(`测试任务 ${job.id} 已启动`);
        pollTestJob(job.id, project, sheet, caseId);
      } catch (error) {
        showToast(error.message, error.status === 409 ? 'warning' : 'error');
        button.disabled = false;
        button.textContent = '启动测试';
      }
  });
  document.querySelector('#test-cancel-button').addEventListener('click', async event => {
    const button = event.currentTarget;
    const jobId = button.dataset.jobId;
    if (!jobId) return;
    if (!window.confirm('取消后将停止当前测试；已执行的设备操作无法撤回，也不会保存本次结果。继续吗？')) return;
    button.disabled = true;
    button.textContent = '正在取消…';
    try {
      await api(`/api/tests/jobs/${encodeURIComponent(jobId)}/cancel`, {method: 'POST', body: '{}'});
      document.querySelector('#test-job-message').textContent = '正在取消测试…';
    } catch (error) {
      button.disabled = false;
      button.textContent = '取消当前测试';
      showToast(error.message, 'error');
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
  const cancelButton = document.querySelector('#test-cancel-button');
  const chip = document.querySelector('#test-job-chip');
  const message = document.querySelector('#test-job-message');
  if (!button || !chip || !message) return;
  try {
    const payload = await api('/api/tests/active');
    const job = activeTestJobForProject(payload, project);
    if (!job) {
      button.disabled = false;
      button.textContent = '启动测试';
      if (cancelButton) cancelButton.hidden = true;
      return;
    }
    button.disabled = true;
    if (job.type === 'batch') {
      button.textContent = '批次运行中';
      chip.className = 'chip chip-warning';
      chip.textContent = '批次占用中';
      if (cancelButton) cancelButton.hidden = true;
      message.innerHTML = `批次已完成 ${Number(job.completed || 0)} / ${Number(job.total || 0)} 条。<a href="${escapeHtml(testBatchHref(job.id))}">查看进度 →</a>`;
      testPollTimer = setTimeout(() => restoreActiveTest(project, sheet, caseId), 2000);
      return;
    }
    if (job.project === project && job.sheet === sheet && job.case_id === caseId) {
      button.textContent = job.status === 'finalizing' ? '保存记录中…' : '正在测试…';
      updateTestWorkflow(job.nodes);
      chip.className = 'chip chip-running';
      chip.textContent = job.status === 'finalizing' ? '保存记录中' : '任务运行中';
      message.textContent = '测试仍在运行，已恢复最新进度。';
      if (cancelButton) {
        cancelButton.hidden = job.status === 'finalizing';
        cancelButton.disabled = false;
        cancelButton.textContent = job.status === 'orphaned' ? '停止上次任务' : '取消当前测试';
        cancelButton.dataset.jobId = job.id;
      }
      pollTestJob(job.id, project, sheet, caseId, Boolean(job.promotion_flow));
      return;
    }
    button.textContent = '其他测试运行中';
    if (cancelButton) cancelButton.hidden = true;
    chip.className = 'chip chip-warning';
    chip.textContent = '任务占用中';
    message.textContent = `${job.project_label || ''} ${job.case_id} 正在运行，结束后才能启动当前用例。`;
    testPollTimer = setTimeout(() => restoreActiveTest(project, sheet, caseId), 2000);
  } catch (error) {
    button.disabled = false;
    button.textContent = '启动测试';
    message.innerHTML = issueNoticeHtml({
      reasonCode: error.code,
      detail: error.diagnosticMessage || error.message,
      fallback: '暂时无法读取任务状态。'
    });
  }
}

async function pollTestJob(jobId, project, sheet, caseId, promotionFlow = false) {
  const chip = document.querySelector('#test-job-chip');
  const message = document.querySelector('#test-job-message');
  const button = document.querySelector('#test-run-button');
  const cancelButton = document.querySelector('#test-cancel-button');
  const promoteButton = document.querySelector('#test-promote-button');
  if (!chip || !message || !button) return;
  try {
    const job = await api(`/api/tests/jobs/${encodeURIComponent(jobId)}`);
    const isPromotionFlow = promotionFlow || Boolean(job.promotion_flow);
    updateTestWorkflow(job.nodes);
    if (['queued', 'running', 'finalizing', 'orphaned'].includes(job.status)) {
      button.disabled = true;
      button.textContent = job.status === 'finalizing' ? '保存记录中…' : '正在测试…';
      if (promoteButton && isPromotionFlow) {
        promoteButton.disabled = true;
        promoteButton.textContent = job.status === 'finalizing' ? '正在保存步骤…' : '正在验证步骤…';
      }
      chip.className = job.status === 'orphaned' ? 'chip chip-warning' : 'chip chip-running';
      if (job.status === 'orphaned') {
        chip.textContent = '上次任务未结束';
        message.innerHTML = issueNoticeHtml({reasonCode: job.reason_code || 'ORPHAN_PROCESS', detail: job.interruption_reason});
      } else if (job.status === 'finalizing') {
        chip.textContent = isPromotionFlow ? '正在保存步骤' : '保存结果中';
        message.textContent = isPromotionFlow ? '测试已结束，正在验证并保存自动化步骤…' : '测试已结束，正在保存结果…';
      } else {
        chip.textContent = TEST_NODE_LABELS[job.current_node] || '任务运行中';
        message.textContent = TEST_NODE_LABELS[job.current_node] || '正在执行';
      }
      if (cancelButton) {
        cancelButton.hidden = job.status === 'finalizing';
        cancelButton.disabled = false;
        cancelButton.textContent = job.status === 'orphaned' ? '停止上次任务' : '取消当前测试';
        cancelButton.dataset.jobId = job.id;
      }
      testPollTimer = setTimeout(() => pollTestJob(jobId, project, sheet, caseId, isPromotionFlow), 2000);
      return;
    }
    if (job.status === 'cancelled') {
      chip.className = 'chip chip-warning';
      chip.textContent = '已取消';
    } else if (['failed', 'interrupted', 'orphaned'].includes(String(job.workflow_status || job.status).toLowerCase())) {
      chip.className = 'chip chip-fail';
      chip.textContent = '执行异常';
    } else {
      setResultChip(chip, job.verdict);
    }
    const evidenceLink = job.history_id
      ? `<a href="${escapeHtml(testHistoryHref(project, sheet, caseId, job.history_id, currentRouteUrl()))}">查看本次测试证据 →</a>`
      : '';
    const interruptionStatus = String(job.workflow_status || job.status || '').toLowerCase();
    const isInterrupted = ['failed', 'interrupted', 'orphaned'].includes(interruptionStatus)
      || Boolean(job.error || job.interruption_reason);
    const jobIssue = isInterrupted ? issuePresentation({
      reasonCode: job.reason_code,
      detail: job.interruption_reason || job.error,
      fallback: '本次测试没有正常完成。'
    }) : null;
    if (isPromotionFlow) {
      const promotionIssues = Array.isArray(job.promotion_issues) ? job.promotion_issues.filter(Boolean) : [];
      if (job.promotion_status === 'promoted') {
        message.innerHTML = `自动化步骤已验证并保存。${evidenceLink}`;
        showToast('自动化步骤已验证并保存');
        window.location.reload();
        return;
      }
      message.innerHTML = `步骤验证未通过，未保存。${evidenceLink}`;
      showToast(promotionIssues.length ? '步骤验证未通过，请查看本次证据' : '步骤验证未通过，未保存', job.promotion_status === 'rolled_back' ? 'warning' : 'error');
      if (promoteButton) {
        promoteButton.disabled = false;
        promoteButton.textContent = '验证并保存步骤';
      }
    } else {
      if (isInterrupted) {
        message.innerHTML = `${issueNoticeHtml({reasonCode: job.reason_code, detail: job.interruption_reason || job.error, fallback: '本次测试没有正常完成。'})}${evidenceLink}`;
      } else if (job.history_id) {
        message.innerHTML = `测试已结束。${evidenceLink}`;
      } else if (job.status === 'cancelled') {
        message.textContent = '测试已取消，本次没有保存结果。';
      } else {
        message.textContent = testResultReason(job, '测试未生成可用结果，请重试。');
      }
    }
    button.disabled = false;
    button.textContent = '再次启动测试';
    if (cancelButton) {
      cancelButton.hidden = true;
      cancelButton.disabled = false;
      cancelButton.textContent = '取消当前测试';
      delete cancelButton.dataset.jobId;
    }
    const detail = await api(`/api/tests/${encodeURIComponent(sheet)}/${encodeURIComponent(caseId)}?project=${encodeURIComponent(project)}`);
    document.querySelector('#test-history-body').innerHTML = testHistoryRows(detail.history, project, sheet, caseId, testReturnUrl());
    if (!isPromotionFlow) {
      if (job.status === 'cancelled') showToast('测试任务已取消', 'warning');
      else if (jobIssue) showToast(jobIssue.summary, 'error');
      else if (job.verdict === 'PASS') showToast('测试通过');
      else if (job.verdict === 'CANNOT_VERIFY') showToast('测试无法验证，请查看判定理由和证据', 'warning');
      else showToast(job.verdict === 'ERROR' ? '测试执行异常' : '测试未通过', 'error');
    }
  } catch (error) {
    chip.className = 'chip chip-fail';
    chip.textContent = '状态读取失败';
    message.innerHTML = issueNoticeHtml({
      reasonCode: error.code,
      detail: error.diagnosticMessage || error.message,
      fallback: '暂时无法读取任务状态。'
    });
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
  const returnTo = currentRouteUrl();
  return `<div class="batch-result-list">${items.map(item => {
    const content = `${resultChip(item.verdict)}<span><strong>${escapeHtml(item.case_id)}</strong><small>${escapeHtml(item.sheet)} · ${escapeHtml(testResultReason(item))}</small></span><time>${formatTime(item.finished_at)}</time>`;
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
      <a ${imagePreviewLinkAttributes(item.url, item.label || screenshotLabel(project))}><img src="${escapeHtml(item.url)}" alt="${escapeHtml(item.label || screenshotLabel(project))}"></a>
    </figure>`).join('') : '<div class="evidence-missing">当前用例尚未生成截图</div>';
}

function updateBatchView(job) {
  const project = testProject(job.project).project;
  const completed = Number(job.completed || 0);
  const total = Number(job.total || 0);
  const percent = total ? Math.min(100, Math.round(completed * 1000 / total) / 10) : 0;
  const counts = job.verdict_counts || {};
  const active = ['queued', 'running', 'finalizing', 'orphaned'].includes(job.status);
  const cancellable = ['queued', 'running', 'orphaned'].includes(job.status);
  const statusChip = document.querySelector('#batch-status-chip');
  if (statusChip) {
    if (active) {
      statusChip.className = job.status === 'orphaned' ? 'chip chip-warning' : 'chip chip-running';
      statusChip.textContent = job.status === 'orphaned'
        ? '上次任务未结束'
        : job.status === 'finalizing'
          ? '保存记录中'
          : job.cancel_requested ? '正在停止当前用例' : '运行中';
    } else if (job.status === 'completed') {
      const hasExecutionAnomaly = Number(job.execution_error_count || 0) > 0
        || ['ERROR', 'INCOMPLETE', 'MISSING'].includes(String(job.evidence_status || '').toUpperCase());
      statusChip.className = hasExecutionAnomaly ? 'chip chip-warning' : 'chip chip-pass';
      statusChip.textContent = hasExecutionAnomaly ? '批次已完成，部分用例异常' : '批次完成';
    } else {
      statusChip.className = 'chip chip-warning';
      statusChip.textContent = job.status === 'cancelled' ? '已取消' : job.status === 'interrupted' ? '运行中断' : '批次异常';
    }
  }
  const value = document.querySelector('#batch-progress-value');
  const bar = document.querySelector('#batch-progress-bar');
  if (value) value.textContent = `${completed} / ${total} · ${percent}%`;
  if (bar) bar.style.width = `${percent}%`;
  for (const [key, id] of [['PASS', 'batch-pass'], ['FAIL', 'batch-fail'], ['CANNOT_VERIFY', 'batch-cannot']]) {
    const element = document.querySelector(`#${id}`);
    if (element) element.textContent = Number(counts[key] || 0);
  }
  const executionErrors = document.querySelector('#batch-error');
  if (executionErrors) executionErrors.textContent = Number(job.execution_error_count || 0);

  const current = document.querySelector('#batch-current-case');
  if (current) {
    const item = job.current_case;
    const isInterrupted = Boolean(job.status === 'interrupted' || job.status === 'orphaned' || job.status === 'failed' || job.resume_available || job.interruption_reason || job.error);
    const batchIssue = isInterrupted ? issuePresentation({reasonCode: job.reason_code, detail: job.interruption_reason || job.error, fallback: '批次已中断。'}) : null;
    current.innerHTML = isInterrupted
      ? `<div class="notice notice-error"><p><strong>问题原因：</strong>${escapeHtml(batchIssue.cause)}</p>${batchIssue.action ? `<p><strong>处理方法：</strong>${escapeHtml(batchIssue.action)}</p>` : ''}${job.resume_available ? `<p><strong>可从第 ${completed + 1} 条继续。</strong></p>` : ''}</div>${(job.interruption_reason || job.error) ? `<details class="inline-diagnostics"><summary>诊断信息</summary><pre><code>${escapeHtml(job.interruption_reason || job.error)}</code></pre></details>` : ''}`
      : item ? `
      <div class="batch-case-head"><div><span>当前用例 ${Number(job.current_index || 0)} / ${total}</span><strong>${escapeHtml(item.case_id)}</strong><small>${escapeHtml(item.sheet)} · ${escapeHtml(item.priority || '未分级')}</small></div>${resultChip('RUNNING')}</div>
      <div class="batch-case-grid">
        <div><span>当前阶段</span><strong>${escapeHtml(TEST_NODE_LABELS[job.current_node] || job.current_node || '启动中')}</strong></div>
      </div>
      <p>${escapeHtml(item.expected_text || '未填写预期结果')}</p>`
      : job.status === 'completed'
        ? '<div class="notice">全部用例已完成。</div>'
        : '<div class="notice">正在准备下一条用例…</div>';
  }
  const target = document.querySelector('#batch-target');
  if (target) target.innerHTML = testTargetChip(job);
  updateBatchScreenshots(job.live_screenshots, project);
  const recent = document.querySelector('#batch-recent-results');
  if (recent) recent.innerHTML = batchRecentRows(job.recent_results, project);
  const cancel = document.querySelector('#batch-cancel-button');
  if (cancel) {
    cancel.hidden = !cancellable;
    cancel.disabled = !cancellable || Boolean(job.cancel_requested);
    cancel.textContent = job.cancel_requested
      ? '正在停止当前用例'
      : job.status === 'orphaned' ? '停止上次任务' : '取消批次';
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
    if (current) current.innerHTML = issueNoticeHtml({
      reasonCode: error.code,
      detail: error.diagnosticMessage || error.message,
      fallback: '暂时无法读取批次状态。'
    });
  }
}

async function renderTestBatch(jobId) {
  setActiveNav('runs');
  stopBatchTestPolling();
  const routeToken = routeRequestToken;
  const initialJob = await api(`/api/tests/jobs/${encodeURIComponent(jobId)}`);
  if (routeToken !== routeRequestToken) return;
  const projectMeta = testProject(initialJob.project);
  const fallback = buildTestListUrl('', 1, DEFAULT_TEST_STATE, projectMeta.project);
  const returnTo = pageReturnUrl(fallback);
  const returnLabel = returnDestinationLabel(returnTo, '用例管理');
  document.title = `批次运行 · 自动化测试`;
  app.innerHTML = `
    <a class="back-link" data-return-link href="${escapeHtml(returnTo)}">← 返回${escapeHtml(returnLabel)}</a>
    <header class="page-header">
      <div>
        <p class="eyebrow">${escapeHtml(projectMeta.projectLabel)} · ${escapeHtml(projectMeta.targetLabel)}批次测试</p>
        <h1 class="page-title">批次运行</h1>
        <div class="meta-line"><span id="batch-target">${testTargetChip(projectMeta)}</span><span id="batch-status-chip" class="chip chip-running">读取状态</span><span id="batch-timing" class="muted small"></span></div>
      </div>
      <div class="batch-actions">
        <button id="batch-resume-button" class="button" type="button" hidden>继续运行</button>
        <button id="batch-cancel-button" class="button button-danger" type="button">取消批次</button>
      </div>
    </header>
    <section class="panel">
      <header class="panel-head"><div><h2>总进度</h2><p>每条用例完成后保存结果</p></div><strong id="batch-progress-value">0 / 0</strong></header>
      <div class="panel-body">
        <div class="batch-progress-track"><div id="batch-progress-bar" class="batch-progress-bar"></div></div>
        <div class="batch-summary-grid">
          <div><span>通过</span><strong id="batch-pass">0</strong></div>
          <div><span>失败</span><strong id="batch-fail">0</strong></div>
          <div><span>执行异常</span><strong id="batch-error">0</strong></div>
          <div><span>无法验证</span><strong id="batch-cannot">0</strong></div>
        </div>
      </div>
    </section>
    <section class="panel">
      <header class="panel-head"><div><h2>当前用例</h2><p>执行阶段和预期结果</p></div></header>
      <div id="batch-current-case" class="panel-body"><div class="list-loading">正在读取当前用例…</div></div>
    </section>
    <section class="panel">
      <header class="panel-head"><div><h2>当前${escapeHtml(screenshotLabel(projectMeta))}</h2><p>截图会在检查点完成后显示</p></div><span id="batch-screenshot-count" class="chip chip-pending">0 张</span></header>
      <div id="batch-live-screenshots" class="panel-body ${screenshotGridClass(projectMeta)}"></div>
    </section>
    <section class="panel">
      <header class="panel-head"><div><h2>最新测试结果</h2><p>显示最近 30 条，点击查看详情</p></div></header>
      <div id="batch-recent-results" class="panel-body"><p class="muted">正在等待首条结果…</p></div>
    </section>`;
  document.querySelector('#batch-cancel-button').addEventListener('click', async () => {
    if (!window.confirm('将停止当前用例并保留已完成结果；已执行的设备操作无法撤回，之后可继续剩余用例。继续吗？')) return;
    const button = document.querySelector('#batch-cancel-button');
    button.disabled = true;
    try {
      const job = await api(`/api/tests/jobs/${encodeURIComponent(jobId)}/cancel`, {method: 'POST', body: '{}'});
      updateBatchView(job);
      showToast('已请求取消，正在停止当前用例', 'warning');
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
  if (!matches?.length) return '<p class="muted">暂未定位到相关源码，启动修复后将继续分析。</p>';
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
    <a ${imagePreviewLinkAttributes(item.url, item.name || '缺陷原图')}>
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
    <a class="history-row" href="${escapeHtml(defectHistoryHref(number, item.id))}">
      ${resultChip(item.verdict)}
      <span><strong>${escapeHtml(testMode(item))}</strong><small>${escapeHtml(item.source_file || '实际修改文件未记录')}</small></span>
      <time>${formatTime(item.timestamp)}</time>
      <span class="row-arrow" aria-hidden="true">›</span>
    </a>`).join('')}</div>`;
}

async function renderDefect(number) {
  setActiveNav('defects');
  const routeToken = routeRequestToken;
  const repairProject = testProject(currentProject());
  const returnTo = pageReturnUrl(pageUrl('/defects', currentProject()));
  const returnLabel = returnDestinationLabel(returnTo, '缺陷队列');
  const defect = await api(`/api/defects/${encodeURIComponent(number)}`);
  if (routeToken !== routeRequestToken) return;
  document.title = `缺陷 #${defect.number} · Agent-loop`;
  const sourceMatches = defect.source_analysis?.matches || [];
  app.innerHTML = `
    <a class="back-link" data-return-link href="${escapeHtml(returnTo)}">← 返回${escapeHtml(returnLabel)}</a>
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
        <details class="panel collapsible-panel">
          <summary class="panel-head">
            <div><h2>诊断信息</h2><p>源码位置</p></div>
            <span class="collapse-controls"><span class="chip chip-pending">${sourceMatches.length} 处</span><span class="collapse-action" aria-hidden="true"></span></span>
          </summary>
          <div class="panel-body">${sourceCards(sourceMatches)}</div>
        </details>
      </div>
      <aside class="panel repair-panel">
        <header class="panel-head"><div><h2>启动修复</h2><p>分析问题、尝试修复并验证结果</p></div></header>
        <form id="run-form" class="panel-body">
          <ul class="run-notes">
            <li>执行项目：${escapeHtml(repairProject.projectLabel)} · ${escapeHtml(repairProject.targetLabel)}</li>
            <li>分析问题并定位相关代码</li>
            <li>尝试修复并运行验证</li>
            <li>保存代码改动和验证结果</li>
          </ul>
          <button id="run-button" class="button button-wide" type="submit">启动修复</button>
          <button id="cancel-run-button" class="button button-secondary button-wide" type="button" hidden>取消当前任务</button>
          <p id="run-note" class="form-note">一次只能运行一个修复任务。</p>
        </form>
      </aside>
    </div>
    <section class="panel" id="workflow-panel">
      <header class="panel-head"><div><h2>修复进度</h2><p>启动后可在这里查看进度</p></div><span id="job-chip" class="chip chip-pending">等待启动</span></header>
      <div class="panel-body">
        ${workflowSvg()}
        <div class="workflow-status"><div id="job-message" class="muted small">等待启动</div><div class="legend"><span><i></i>待执行</span><span><i class="blue"></i>执行中</span><span><i class="green"></i>完成</span><span><i class="red"></i>失败</span></div></div>
      </div>
    </section>
    <section class="panel">
      <header class="panel-head"><div><h2>修复历史</h2><p>每次修复的结果和证据</p></div></header>
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
        body: JSON.stringify({
          defect: String(defect.number),
          project: repairProject.project
        })
      });
      updateWorkflow({});
      const chip = document.querySelector('#job-chip');
      const message = document.querySelector('#job-message');
      chip.className = 'chip chip-running';
      chip.textContent = '正在启动';
      message.textContent = '正在启动修复…';
      const cancelButton = document.querySelector('#cancel-run-button');
      if (cancelButton) {
        cancelButton.hidden = false;
        cancelButton.dataset.jobId = job.id;
      }
      pollJob(job.id, String(defect.number));
    } catch (error) {
      showToast(error.message, 'error');
      button.disabled = false;
      button.textContent = '启动修复';
    }
  });
  document.querySelector('#cancel-run-button').addEventListener('click', async event => {
    const cancelButton = event.currentTarget;
    const jobId = cancelButton.dataset.jobId;
    if (!jobId) return;
    cancelButton.disabled = true;
    cancelButton.textContent = '正在取消…';
    try {
      await api(`/api/run/${encodeURIComponent(jobId)}/cancel`, {method: 'POST'});
      document.querySelector('#job-message').textContent = '正在取消修复…';
    } catch (error) {
      cancelButton.disabled = false;
      cancelButton.textContent = '取消当前任务';
      showToast(error.message, 'error');
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
  const cancelButton = document.querySelector('#cancel-run-button');
  const chip = document.querySelector('#job-chip');
  const message = document.querySelector('#job-message');
  if (!button || !chip || !message) return;
  try {
    const payload = await api('/api/run/active');
    const job = payload.job;
    if (!job) {
      button.disabled = false;
      button.textContent = '启动修复';
      if (cancelButton) cancelButton.hidden = true;
      return;
    }
    button.disabled = true;
    if (String(job.defect) === String(defectNumber)) {
      button.textContent = job.status === 'finalizing' ? '保存记录中…' : '正在修复…';
      updateWorkflow(job.nodes);
      chip.className = 'chip chip-running';
      chip.textContent = job.status === 'finalizing' ? '保存记录中' : '任务运行中';
      message.textContent = '修复仍在运行，已恢复最新进度。';
      if (cancelButton) {
        cancelButton.hidden = job.status === 'finalizing';
        cancelButton.disabled = false;
        cancelButton.textContent = job.status === 'orphaned' ? '停止上次任务' : '取消当前任务';
        cancelButton.dataset.jobId = job.id;
      }
      pollJob(job.id, defectNumber);
      return;
    }
    button.textContent = '其他任务运行中';
    if (cancelButton) cancelButton.hidden = true;
    chip.className = 'chip chip-warning';
    chip.textContent = '任务占用中';
    message.textContent = `缺陷 #${job.defect} 正在修复，结束后才能启动当前缺陷。`;
    repairRestoreTimer = setTimeout(() => restoreActiveRepair(defectNumber), 2000);
  } catch (error) {
    button.disabled = false;
    button.textContent = '启动修复';
    message.innerHTML = issueNoticeHtml({
      reasonCode: error.code,
      detail: error.diagnosticMessage || error.message,
      fallback: '暂时无法读取任务状态。'
    });
  }
}

async function pollJob(jobId, defectNumber) {
  const chip = document.querySelector('#job-chip');
  const message = document.querySelector('#job-message');
  const button = document.querySelector('#run-button');
  const cancelButton = document.querySelector('#cancel-run-button');
  if (!chip || !message || !button) return;
  try {
    const job = await api(`/api/run/${encodeURIComponent(jobId)}`);
    updateWorkflow(job.nodes);
    if (job.status === 'queued' || job.status === 'running' || job.status === 'finalizing' || job.status === 'orphaned') {
      button.disabled = true;
      button.textContent = job.status === 'finalizing' ? '保存记录中…' : '正在修复…';
      chip.className = job.status === 'orphaned' ? 'chip chip-warning' : 'chip chip-running';
      if (job.status === 'orphaned') {
        chip.textContent = '上次任务未结束';
        message.innerHTML = issueNoticeHtml({reasonCode: job.reason_code || 'ORPHAN_PROCESS', detail: job.interruption_reason});
      } else if (job.status === 'finalizing') {
        chip.textContent = '保存结果中';
        message.textContent = '修复已结束，正在保存结果…';
      } else {
        chip.textContent = job.current_node ? `${NODE_LABELS[job.current_node] || job.current_node}中` : '任务运行中';
        message.textContent = `${NODE_LABELS[job.current_node] || '正在处理'} · 第 ${Math.max(1, Number(job.attempts || 0))} 轮`;
      }
      if (cancelButton) {
        cancelButton.hidden = job.status === 'finalizing';
        cancelButton.disabled = false;
        cancelButton.textContent = job.status === 'orphaned' ? '停止上次任务' : '取消当前任务';
        cancelButton.dataset.jobId = job.id;
      }
      setTimeout(() => pollJob(jobId, defectNumber), 2000);
      return;
    }
    const interruptionStatus = String(job.workflow_status || job.status || '').toLowerCase();
    const isInterrupted = job.status !== 'cancelled' && (
      ['failed', 'interrupted', 'orphaned'].includes(interruptionStatus)
      || (Boolean(job.workflow_status) && interruptionStatus !== 'completed')
      || String(job.verdict || '').toUpperCase() === 'ERROR'
      || Boolean(job.error || job.interruption_reason)
    );
    const jobIssue = isInterrupted ? issuePresentation({
      reasonCode: job.reason_code,
      detail: job.interruption_reason || job.error,
      fallback: '本次任务没有正常完成。'
    }) : null;
    if (isInterrupted) {
      chip.className = 'chip chip-fail';
      chip.textContent = '执行异常';
    } else {
      setResultChip(chip, job.verdict);
    }
    const evidenceLink = job.history_id
      ? `<a href="${escapeHtml(defectHistoryHref(defectNumber, job.history_id))}">查看本次修复证据 →</a>`
      : '';
    if (isInterrupted) {
      message.innerHTML = `${issueNoticeHtml({reasonCode: job.reason_code, detail: job.interruption_reason || job.error, fallback: '本次任务没有正常完成。'})}${evidenceLink}`;
    } else if (job.history_id) {
      message.innerHTML = `修复已结束。${evidenceLink}`;
    } else if (job.status === 'cancelled') {
      message.textContent = '修复已取消，本次没有保存结果。';
    } else {
      message.textContent = testResultReason(job, '修复未生成可用结果，请重试。');
    }
    button.disabled = false;
    button.textContent = '再次启动修复';
    if (cancelButton) {
      cancelButton.hidden = true;
      cancelButton.disabled = false;
      cancelButton.textContent = '取消当前任务';
      delete cancelButton.dataset.jobId;
    }
    const historyPayload = await api(`/api/history/${encodeURIComponent(defectNumber)}`);
    document.querySelector('#history-body').innerHTML = historyRows(historyPayload.history, defectNumber);
    if (job.status === 'cancelled') showToast('修复任务已取消', 'warning');
    else if (jobIssue) showToast(jobIssue.summary, 'error');
    else if (job.verdict === 'PASS') showToast('修复与验证均已通过');
    else if (job.verdict === 'CANNOT_VERIFY') showToast('无法验证，请查看测试命令和证据', 'warning');
    else showToast('修复未通过，请查看失败原因和证据', 'error');
  } catch (error) {
    chip.className = 'chip chip-fail';
    chip.textContent = '状态读取失败';
    message.innerHTML = issueNoticeHtml({
      reasonCode: error.code,
      detail: error.diagnosticMessage || error.message,
      fallback: '暂时无法读取任务状态。'
    });
    button.disabled = false;
    button.textContent = '重新启动';
  }
}

function evidenceFrame(label, url, meta = '') {
  const displayLabel = label || '模拟器截图';
  const safeLabel = escapeHtml(displayLabel);
  return `<article class="evidence-frame"><header><span>${safeLabel}</span>${meta ? `<small>${escapeHtml(meta)}</small>` : ''}</header>${url
    ? `<a ${imagePreviewLinkAttributes(url, displayLabel)}><img src="${escapeHtml(url)}" alt="${safeLabel}" onerror="this.closest('a').outerHTML='<div class=\'evidence-missing\'>截图读取失败</div>'"></a>`
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
  if (!thinkingSteps.length) return `<div class="legacy-missing">${escapeHtml(missingRecordText(record, '分析过程'))}</div>`;
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
  const routeToken = routeRequestToken;
  const fallback = defectDetailHref(number, pageUrl('/defects', currentProject()));
  const returnTo = pageReturnUrl(fallback);
  const returnLabel = returnDestinationLabel(returnTo, '缺陷详情');
  const record = await api(`/api/history/${encodeURIComponent(number)}/${encodeURIComponent(runId)}`);
  if (routeToken !== routeRequestToken) return;
  document.title = `修复记录 · 缺陷 #${number} · Agent-loop`;
  const patch = recordedPatch(record);
  const commands = recordCommands(record);
  const thinkingSteps = recordThinkingSteps(record);
  const actualFile = patch.file_path || record.source_file || (record.reproduction_outcome === 'CURRENT_CONFORMS'
    ? '未修改代码'
    : missingRecordText(record, '实际修改文件'));
  app.innerHTML = `
    <a class="back-link" data-return-link href="${escapeHtml(returnTo)}">← 返回${escapeHtml(returnLabel)}</a>
    <header class="page-header">
      <div>
        <p class="eyebrow">缺陷 #${escapeHtml(number)} · 修复证据</p>
        <h1 class="page-title">修复记录</h1>
        <div class="meta-line">${resultChip(record.verdict)}<span class="muted small">${formatTime(record.timestamp)}</span></div>
      </div>
      <button id="delete-run" class="button button-danger" type="button">删除本次记录</button>
    </header>
    <section class="panel">
      <header class="panel-head"><div><h2>结果摘要</h2><p>修复内容和最终结果</p></div></header>
      <div class="panel-body"><div class="kv-grid">
        <div class="kv"><span>测试方式</span><strong>${escapeHtml(testMode(record))}</strong></div>
        <div class="kv"><span>实际修改文件</span><strong title="${escapeHtml(actualFile)}">${escapeHtml(actualFile)}</strong></div>
        <div class="kv"><span>尝试轮数</span><strong>${escapeHtml(record.attempts ?? missingRecordText(record, '尝试轮数'))}</strong></div>
        <div class="kv"><span>代码是否保留</span><strong>${escapeHtml(codeRetention(record))}</strong></div>
        <div class="kv"><span>开始时间</span><strong>${formatTime(record.started_at)}</strong></div>
        <div class="kv"><span>结束时间</span><strong>${formatTime(record.finished_at)}</strong></div>
      </div>${(record.error || record.interruption_reason) ? issueNoticeHtml({reasonCode: record.reason_code, detail: record.error || record.interruption_reason}) : ''}</div>
    </section>
    <details class="panel collapsible-panel">
      <summary class="panel-head"><div><h2>执行详情</h2><p>各阶段最终状态</p></div><span class="collapse-controls"><span class="collapse-action" aria-hidden="true"></span></span></summary>
      <div class="panel-body">${workflowSvg()}</div>
    </details>
    <section class="panel">
      <header class="panel-head"><div><h2>判定理由</h2><p>本次验证结果</p></div></header>
      <div class="panel-body verdict-reasons">${verdictReasons(record)}</div>
    </section>
    <details class="panel collapsible-panel">
      <summary class="panel-head"><div><h2>测试命令</h2><p>诊断信息</p></div><span class="collapse-controls"><span class="chip chip-pending">${commands.length} 条</span><span class="collapse-action" aria-hidden="true"></span></span></summary>
      <div class="panel-body">${commandEvidence(record, commands)}</div>
    </details>
    <section class="panel">
      <header class="panel-head"><div><h2>截图对比</h2><p>修复前后的模拟器画面</p></div></header>
      <div class="panel-body"><div class="evidence-grid">${evidenceFrame('修复前', record.evidence?.before)}${evidenceFrame('修复后', record.evidence?.after)}</div></div>
    </section>
    <section class="panel">
      <header class="panel-head"><div><h2>代码改动</h2><p>${escapeHtml(actualFile)}</p></div></header>
      <div class="panel-body">${codeChangeEvidence(record, patch)}</div>
    </section>
    <details class="panel collapsible-panel">
      <summary class="panel-head"><div><h2>分析过程</h2><p>每一步的判断依据</p></div><span class="collapse-controls"><span class="chip chip-pending">${thinkingSteps.length} 条</span><span class="collapse-action" aria-hidden="true"></span></span></summary>
      <div class="panel-body">${thinkingEvidence(record, thinkingSteps)}</div>
    </details>`;
  updateWorkflow(record.nodes);
  document.querySelector('#delete-run').addEventListener('click', async () => {
    if (!window.confirm('确定删除这次修复记录及截图？删除后无法恢复。')) return;
    const button = document.querySelector('#delete-run');
    button.disabled = true;
    try {
      await api(`/api/history/${encodeURIComponent(number)}/${encodeURIComponent(runId)}`, { method: 'DELETE' });
      window.location.href = returnTo;
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
  const routeToken = routeRequestToken;
  const project = testListParams().project;
  const listFallback = buildTestListUrl('', 1, DEFAULT_TEST_STATE, project);
  const detailFallback = testDetailHref(project, sheet, caseId, listFallback);
  const backHref = pageReturnUrl(detailFallback);
  const backLabel = returnDestinationLabel(backHref, '测试详情');
  const record = await api(`/api/test-history/${encodeURIComponent(sheet)}/${encodeURIComponent(caseId)}/${encodeURIComponent(runId)}?project=${encodeURIComponent(project)}`);
  if (routeToken !== routeRequestToken) return;
  document.title = `测试记录 · ${caseId} · 自动化测试`;
  app.innerHTML = `
    <a class="back-link" data-return-link href="${escapeHtml(backHref)}">← 返回${escapeHtml(backLabel)}</a>
    <header class="page-header">
      <div>
        <p class="eyebrow">${escapeHtml(record.sheet)} · ${escapeHtml(record.case_id)} · ${escapeHtml(record.execution_target_label)}</p>
        <h1 class="page-title">测试记录</h1>
        <div class="meta-line">${testTargetChip(record)}${resultChip(record.verdict)}<span class="muted small">${formatTime(record.timestamp)}</span></div>
      </div>
      <button id="delete-test-run" class="button button-danger" type="button">删除本次记录</button>
    </header>
    <section class="panel">
      <header class="panel-head"><div><h2>结果摘要</h2><p>用例、时间和最终结果</p></div></header>
      <div class="panel-body">
        <div class="kv-grid">
          <div class="kv"><span>测试项目</span><strong>${escapeHtml(record.project_label)}</strong></div>
          <div class="kv"><span>请求平台</span><strong>${escapeHtml(PLATFORM_PROFILES[record.requested_platform_id || record.platform_id]?.platform_label || record.requested_platform_id || record.platform_id || '旧记录未标明')}</strong></div>
          <div class="kv"><span>实际执行适配器</span><strong>${escapeHtml(record.resolved_execution_adapter || record.execution_adapter || '旧记录未标明')}</strong></div>
          <div class="kv"><span>执行目标</span><strong>${escapeHtml(record.execution_target_label)}</strong></div>
          <div class="kv"><span>测试模块</span><strong>${escapeHtml(record.sheet)}</strong></div>
          <div class="kv"><span>用例编号</span><strong>${escapeHtml(record.case_id)}</strong></div>
          <div class="kv"><span>执行方式</span><strong>${escapeHtml(testExecutionModeLabel(record.execution_mode))}</strong></div>
          <div class="kv"><span>产品结果</span><strong>${escapeHtml(record.product_verdict || record.verdict || '未记录')}</strong></div>
          <div class="kv"><span>自动化成熟度</span><strong>${escapeHtml(record.automation_maturity || record.mapping_status || '旧记录未标明')}</strong></div>
          <div class="kv"><span>基础设施状态</span><strong>${escapeHtml(record.infrastructure_status || '旧记录未标明')}</strong></div>
          <div class="kv"><span>优先级</span><strong>${escapeHtml(record.priority || '未分级')}</strong></div>
          <div class="kv"><span>开始时间</span><strong>${formatTime(record.started_at)}</strong></div>
          <div class="kv"><span>结束时间</span><strong>${formatTime(record.finished_at)}</strong></div>
        </div>${(record.error || record.interruption_reason || (record.verdict === 'ERROR' && record.reason)) ? issueNoticeHtml({reasonCode: record.reason_code, detail: record.error || record.interruption_reason || record.reason}) : ''}
      </div>
    </section>
    <section class="panel">
      <header class="panel-head"><div><h2>判定理由</h2><p>根据预期结果和截图得出的结论</p></div></header>
      <div class="panel-body verdict-reasons"><p>${escapeHtml(testResultReason(record))}</p></div>
    </section>
    <section class="panel">
      <header class="panel-head"><div><h2>证据完整性</h2><p>检查操作、检查点和截图是否齐全</p></div></header>
      <div class="panel-body">${evidenceContractEvidence(record)}</div>
    </section>
    ${(record.requested_platform_id || record.platform_id) === '579' ? `<details class="panel collapsible-panel"><summary class="panel-head"><div><h2>579 三链路证据</h2><p>APP Bridge 仅代表动作交付；O1/O2 才用于设备效果判断</p></div><span class="collapse-controls"><span class="chip chip-pending">交付 ${Array.isArray(record.delivery_feedback) ? record.delivery_feedback.length : 0} / 观察 ${Array.isArray(record.observations) ? record.observations.length : 0}</span><span class="collapse-action" aria-hidden="true"></span></span></summary><div class="panel-body"><h3>APP Bridge 反馈</h3><pre class="code-block raw-output"><code>${escapeHtml(JSON.stringify(record.delivery_feedback || [], null, 2))}</code></pre><h3>COM3/O1 与 O2 只读观察</h3><pre class="code-block raw-output"><code>${escapeHtml(JSON.stringify(record.observations || [], null, 2))}</code></pre></div></details>` : ''}
    <section class="panel">
      <header class="panel-head"><div><h2>用例内容</h2><p>本次运行使用的步骤和预期结果</p></div></header>
      <div class="panel-body case-text-grid">
        ${caseText('前置条件', record.precondition_text)}
        ${caseText('操作步骤', record.steps_text)}
        ${caseText('预期结果', record.expected_text)}
        ${verificationPoints(record.verification_points)}
      </div>
    </section>
    <details class="panel collapsible-panel">
      <summary class="panel-head"><div><h2>实际执行步骤</h2><p>本次执行的操作、等待和截图</p></div><span class="collapse-controls"><span class="chip chip-pending">${Array.isArray(record.command_trace) ? record.command_trace.length : 0} 条</span><span class="collapse-action" aria-hidden="true"></span></span></summary>
      <div class="panel-body command-phases">${actualCommandTrace(record.command_trace)}</div>
    </details>
    <details class="panel collapsible-panel">
      <summary class="panel-head"><div><h2>执行计划</h2><p>本次运行采用的计划</p></div><span class="collapse-controls"><span class="collapse-action" aria-hidden="true"></span></span></summary>
      <div class="panel-body command-phases">
        ${commandPhase('准备环境', '运行前', record.planned_commands?.setup || record.setup)}
        ${commandPhase('执行操作', '测试步骤', record.planned_commands?.action || record.actions)}
        ${commandPhase('采集证据', '检查结果', record.planned_commands?.collect || record.collect)}
      </div>
    </details>
    <details class="panel collapsible-panel">
      <summary class="panel-head"><div><h2>执行问题</h2><p>按测试阶段分类</p></div><span class="collapse-controls"><span class="collapse-action" aria-hidden="true"></span></span></summary>
      <div class="panel-body">${testErrorEvidence(record)}</div>
    </details>
    <section class="panel">
      <header class="panel-head"><div><h2>${escapeHtml(screenshotLabel(record))}</h2><p>按检查点顺序显示</p></div><span class="chip chip-pending">${Array.isArray(record.screenshot_urls) ? record.screenshot_urls.length : (record.screenshot_url ? 1 : 0)} 张</span></header>
      <div class="panel-body ${screenshotGridClass(record)}">${testScreenshotGallery(record)}</div>
    </section>
    <details class="panel collapsible-panel">
      <summary class="panel-head"><div><h2>诊断数据</h2><p>原始返回信息</p></div><span class="collapse-controls"><span class="chip chip-pending">${Array.isArray(record.terminal_json) ? record.terminal_json.length : 0} 条</span><span class="collapse-action" aria-hidden="true"></span></span></summary>
      <div class="panel-body"><p class="muted small">进程返回码：${escapeHtml(record.return_code ?? '未记录')}</p><pre class="code-block raw-output"><code>${escapeHtml(JSON.stringify(record.terminal_json || [], null, 2))}</code></pre></div>
    </details>`;
  document.querySelector('#delete-test-run').addEventListener('click', async () => {
    if (!window.confirm('确定删除这次测试记录及截图？删除后无法恢复。')) return;
    const button = document.querySelector('#delete-test-run');
    button.disabled = true;
    try {
      await api(`/api/test-history/${encodeURIComponent(sheet)}/${encodeURIComponent(caseId)}/${encodeURIComponent(runId)}?project=${encodeURIComponent(project)}`, {method: 'DELETE'});
      window.location.href = backHref;
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

  const hwCaptureSelect = dialog.querySelector('#cfg-hw-capture');
  const hwPortSelect = dialog.querySelector('#cfg-hw-port');
  const btnRefreshSerialPorts = dialog.querySelector('#btn-refresh-serial-ports');
  const hwPortStatus = dialog.querySelector('#cfg-hw-port-status');
  const hwBleOptions = dialog.querySelector('#cfg-hw-ble-options');
  const hwBleAddress = dialog.querySelector('#cfg-hw-ble-address');
  const hwBleScanTimeout = dialog.querySelector('#cfg-hw-ble-scan-timeout');
  const bleSearchInput = dialog.querySelector('#ble-device-search');
  const bleScanButton = dialog.querySelector('#btn-scan-ble');
  const bleConnectionStatus = dialog.querySelector('#ble-connection-status');
  const bleDiscoveredList = dialog.querySelector('#ble-discovered-list');
  const bleRememberedList = dialog.querySelector('#ble-remembered-list');
  const bleDiscoveredCount = dialog.querySelector('#ble-discovered-count');
  const bleRememberedCount = dialog.querySelector('#ble-remembered-count');
  let serialPortsData = null;
  let serialPortsLoading = false;
  let bleDiscoveredDevices = [];
  let bleRememberedDevices = [];
  let bleRememberedLoaded = false;
  let bleHasScanned = false;
  let bleBusy = false;

  const bleAddressKey = value => String(value || '').trim().toLocaleLowerCase();
  const bleTimeout = () => {
    const value = Number(hwBleScanTimeout?.value || 15);
    if (!Number.isFinite(value) || value < 1 || value > 60) {
      throw new Error('查找和连接超时必须是 1 到 60 秒之间的数字');
    }
    return value;
  };
  const setBleConnectionStatus = (tone, title, detail, diagnostic = '') => {
    if (!bleConnectionStatus) return;
    bleConnectionStatus.dataset.tone = tone;
    const titleNode = bleConnectionStatus.querySelector('strong');
    const detailNode = bleConnectionStatus.querySelector('small');
    const diagnosticNode = bleConnectionStatus.querySelector('[data-ble-diagnostics]');
    const diagnosticCode = diagnosticNode?.querySelector('code');
    if (titleNode) titleNode.textContent = title;
    if (detailNode) detailNode.textContent = detail;
    if (diagnosticNode) {
      diagnosticNode.hidden = !diagnostic;
      diagnosticNode.open = false;
    }
    if (diagnosticCode) diagnosticCode.textContent = diagnostic;
  };
  const setBleIssue = issue => {
    setBleConnectionStatus(
      'error',
      `问题原因：${issue.cause}`,
      `处理方法：${issue.action}`,
      issue.detail
    );
  };
  const refreshSelectedBleStatus = () => {
    const address = String(hwBleAddress?.value || '').trim();
    if (!address) {
      setBleConnectionStatus('neutral', '未选择手表', '截图时会自动查找唯一匹配的手表');
      return;
    }
    const remembered = bleRememberedDevices.find(device => bleAddressKey(device.address) === bleAddressKey(address));
    if (remembered) {
      const name = remembered.name || '未命名手表';
      const at = remembered.last_connected_at ? formatTime(remembered.last_connected_at) : '时间未知';
      setBleConnectionStatus('success', `${name} · ${address}`, `最近连接：${at}`);
      return;
    }
    setBleConnectionStatus('neutral', `已选择目标 · ${address}`, '尚无成功连接记录');
  };
  const setBleBusy = busy => {
    bleBusy = busy;
    if (hwBleOptions) hwBleOptions.setAttribute('aria-busy', busy ? 'true' : 'false');
    if (bleScanButton) bleScanButton.disabled = busy;
    hwBleOptions?.querySelectorAll('[data-ble-action]').forEach(button => {
      button.disabled = busy;
    });
  };
  const bleDeviceMatches = (device, query) => {
    const needle = String(query || '').trim().toLocaleLowerCase();
    if (!needle) return true;
    return String(device.address || '').toLocaleLowerCase().includes(needle)
      || String(device.name || '').toLocaleLowerCase().includes(needle);
  };
  const bleDeviceRow = (device, kind) => {
    const address = String(device.address || '').trim();
    const rawName = String(device.name || '').trim();
    const name = rawName || '未命名设备';
    const selected = bleAddressKey(address) === bleAddressKey(hwBleAddress?.value);
    const remembered = bleRememberedDevices.some(item => bleAddressKey(item.address) === bleAddressKey(address));
    const hasRssi = device.rssi !== null && device.rssi !== undefined && Number.isFinite(Number(device.rssi));
    const meta = kind === 'discovered'
      ? (hasRssi ? `信号 ${Number(device.rssi)} dBm` : '信号强度未知')
      : `最近连接 ${device.last_connected_at ? formatTime(device.last_connected_at) : '时间未知'}`;
    const actions = kind === 'discovered'
      ? `<button class="ble-device-action is-primary" type="button" data-ble-action="connect" data-address="${escapeHtml(address)}" data-name="${escapeHtml(rawName)}" ${bleBusy ? 'disabled' : ''}>${remembered ? '重新连接' : '连接'}</button>`
      : `<button class="ble-device-action" type="button" data-ble-action="select" data-address="${escapeHtml(address)}" ${bleBusy ? 'disabled' : ''}>${selected ? '当前目标' : '设为目标'}</button><button class="ble-device-action is-danger" type="button" data-ble-action="delete" data-address="${escapeHtml(address)}" ${bleBusy ? 'disabled' : ''}>删除</button>`;
    return `<article class="ble-device-card ${selected ? 'is-selected' : ''}">
      <div class="ble-device-card-copy"><strong>${escapeHtml(name)}</strong><code>${escapeHtml(address)}</code><small>${escapeHtml(meta)} · ${kind === 'discovered' ? '未连接' : '已保存'}</small></div>
      <div class="ble-device-card-actions">${actions}</div>
    </article>`;
  };
  const renderBleDevices = () => {
    const query = bleSearchInput?.value || '';
    const discovered = bleDiscoveredDevices.filter(device => bleDeviceMatches(device, query));
    const remembered = bleRememberedDevices.filter(device => bleDeviceMatches(device, query));
    if (bleDiscoveredCount) bleDiscoveredCount.textContent = `${discovered.length} 个`;
    if (bleRememberedCount) bleRememberedCount.textContent = `${remembered.length} 个`;
    if (bleDiscoveredList) {
      const emptyText = !bleHasScanned
        ? '点击“查找设备”开始'
        : (bleDiscoveredDevices.length && query ? '没有找到匹配名称或地址的设备' : '本次未发现蓝牙设备');
      bleDiscoveredList.innerHTML = discovered.length
        ? discovered.map(device => bleDeviceRow(device, 'discovered')).join('')
        : `<div class="ble-device-empty">${escapeHtml(emptyText)}</div>`;
    }
    if (bleRememberedList) {
      const emptyText = bleRememberedDevices.length && query
        ? '没有匹配名称或地址的连接记录'
        : '还没有连接过的设备';
      bleRememberedList.innerHTML = remembered.length
        ? remembered.map(device => bleDeviceRow(device, 'remembered')).join('')
        : `<div class="ble-device-empty">${escapeHtml(emptyText)}</div>`;
    }
  };
  async function loadRememberedBleDevices() {
    try {
      const data = await api('/api/hardware/ble/remembered');
      bleRememberedDevices = Array.isArray(data.items) ? data.items : [];
      bleRememberedLoaded = true;
      renderBleDevices();
      refreshSelectedBleStatus();
    } catch (error) {
      bleRememberedDevices = [];
      bleRememberedLoaded = false;
      const issue = presentationFromError(error, '无法读取已保存的蓝牙设备。');
      if (bleRememberedList) {
        bleRememberedList.innerHTML = `<div class="ble-device-empty">${escapeHtml(issue.cause)}</div>`;
      }
      setBleIssue(issue);
    }
  }
  bleSearchInput?.addEventListener('input', renderBleDevices);
  hwBleAddress?.addEventListener('input', () => {
    renderBleDevices();
    refreshSelectedBleStatus();
  });

  const updateSerialPortStatus = selectedPort => {
    if (!hwPortStatus) return;
    const cleanPort = String(selectedPort || hwPortSelect?.value || '').trim().toUpperCase();
    const activeCount = Number(serialPortsData?.active_count || 0);

    if (!cleanPort) {
      hwPortStatus.dataset.tone = 'warning';
      if (activeCount > 1) {
        hwPortStatus.textContent = '检测到多个 SuperCom 端口，请选择要使用的端口';
      } else if (activeCount === 0) {
        hwPortStatus.textContent = '未发现可用串口，请先在 SuperCom 中打开端口';
      } else {
        hwPortStatus.textContent = '请选择 SuperCom 端口';
      }
      return;
    }

    const item = serialPortsData?.items?.find(i => String(i.port || '').toUpperCase() === cleanPort);
    if (!item) {
      hwPortStatus.dataset.tone = 'warning';
      hwPortStatus.textContent = `未发现 ${cleanPort}，请刷新后重新选择`;
      return;
    }
    if (item.missing || item.present === false) {
      hwPortStatus.dataset.tone = 'error';
      hwPortStatus.textContent = `${item.port} 当前不可用`;
      return;
    }
    if (item.supercom_open) {
      hwPortStatus.dataset.tone = 'success';
      hwPortStatus.textContent = 'SuperCom 已就绪';
    } else {
      hwPortStatus.dataset.tone = 'warning';
      if (item.kind === 'system') {
        hwPortStatus.textContent = `${item.port} 尚未在 SuperCom 中打开`;
      } else {
        hwPortStatus.textContent = `请在 SuperCom 中打开 ${item.port}`;
      }
    }
  };

  const renderSerialPortOptions = (data, explicitSelection = null) => {
    if (!hwPortSelect) return;
    const items = Array.isArray(data?.items) ? data.items : [];
    const activeCount = Number(data?.active_count || 0);
    const chosenPort = explicitSelection !== null
      ? String(explicitSelection || '').trim().toUpperCase()
      : String(data?.selected_port || '').trim().toUpperCase();

    if (!items.length) {
      hwPortSelect.innerHTML = `<option value="" disabled selected>未能检测到可用串口设备</option>`;
      updateSerialPortStatus('');
      return;
    }

    const hasMatchingChosen = chosenPort && items.some(item => String(item.port || '').toUpperCase() === chosenPort);
    let placeholderHtml = '';

    if (!hasMatchingChosen) {
      let placeholderText = '请选择 SuperCom 端口设备';
      if (activeCount > 1) {
        placeholderText = '请选择 SuperCom 端口（检测到多个活动端口）';
      } else if (activeCount === 0) {
        placeholderText = '未检测到 SuperCom 开启的串口（请在 SuperCom 中打开端口）';
      }
      placeholderHtml = `<option value="" disabled selected>${escapeHtml(placeholderText)}</option>`;
    }

    const optionsHtml = items.map(item => {
      const port = item.port || '';
      const isSelected = hasMatchingChosen && String(port).toUpperCase() === chosenPort;
      let statusIndicator = 'SuperCom 未开启';
      if (item.missing || item.present === false) {
        statusIndicator = '不可用';
      } else if (item.supercom_open) {
        statusIndicator = '可用';
      }
      const label = `${port} · ${item.friendly_name || port} · ${statusIndicator}`;
      return `<option value="${escapeHtml(port)}" ${isSelected ? 'selected' : ''}>${escapeHtml(label)}</option>`;
    }).join('');

    hwPortSelect.innerHTML = placeholderHtml + optionsHtml;

    if (hasMatchingChosen) {
      hwPortSelect.value = chosenPort;
    } else {
      hwPortSelect.value = '';
    }
    updateSerialPortStatus(hwPortSelect.value);
  };

  async function loadSerialPorts(explicitSelection = null) {
    if (serialPortsLoading) return;
    serialPortsLoading = true;
    if (btnRefreshSerialPorts) btnRefreshSerialPorts.disabled = true;
    try {
      const resp = await fetch('/api/hardware/serial-ports');
      if (!resp.ok) throw new Error('读取串口列表失败');
      const data = await resp.json();
      serialPortsData = data;
      renderSerialPortOptions(data, explicitSelection);
      if (data?.available === false && hwPortStatus) {
        hwPortStatus.dataset.tone = 'error';
        hwPortStatus.textContent = '无法读取串口设备，请检查系统权限后重试';
      }
    } catch (err) {
      if (hwPortStatus) {
        hwPortStatus.dataset.tone = 'error';
        hwPortStatus.textContent = `读取串口失败：${err.message}`;
      }
    } finally {
      serialPortsLoading = false;
      if (btnRefreshSerialPorts) btnRefreshSerialPorts.disabled = false;
    }
  }

  hwPortSelect?.addEventListener('change', () => {
    updateSerialPortStatus(hwPortSelect.value);
  });

  btnRefreshSerialPorts?.addEventListener('click', async () => {
    await loadSerialPorts(hwPortSelect?.value);
    showToast('串口列表已刷新');
  });

  bleScanButton?.addEventListener('click', async () => {
    const originalText = bleScanButton.textContent;
    try {
      const timeout = bleTimeout();
      setBleBusy(true);
      bleScanButton.textContent = '正在查找…';
      setBleConnectionStatus('pending', '正在查找附近的蓝牙设备', '查找过程不会连接任何设备');
      const query = String(bleSearchInput?.value || '').trim();
      const data = await api(`/api/hardware/ble/devices?timeout=${encodeURIComponent(timeout)}&q=${encodeURIComponent(query)}`);
      bleDiscoveredDevices = Array.isArray(data.items) ? data.items : [];
      bleHasScanned = true;
      renderBleDevices();
      refreshSelectedBleStatus();
      showToast(`找到 ${bleDiscoveredDevices.length} 个蓝牙设备`);
    } catch (error) {
      const issue = presentationFromError(error, '电脑没有完成本次蓝牙查找。');
      setBleIssue(issue);
      showToast(issue.summary, 'error');
    } finally {
      setBleBusy(false);
      bleScanButton.textContent = originalText;
      renderBleDevices();
    }
  });

  hwBleOptions?.addEventListener('click', async event => {
    const button = event.target.closest('button[data-ble-action]');
    if (!button || bleBusy) return;
    const action = button.dataset.bleAction;
    const address = String(button.dataset.address || '').trim();
    if (!address) return;
    try {
      setBleBusy(true);
      if (action === 'connect') {
        const name = String(button.dataset.name || '').trim();
        setBleConnectionStatus('pending', `正在连接 ${name || address}`, '正在连接并检查手表…');
        const data = await api('/api/hardware/ble/connect', {
          method: 'POST',
          body: JSON.stringify({address, name, timeout: bleTimeout()}),
        });
        if (!data.verified) {
          const issue = issuePresentation({reasonCode: data.reason_code || 'BLE_CONNECT_FAILED', detail: data.error});
          const connectionError = new Error(issue.summary);
          connectionError.presentation = issue;
          throw connectionError;
        }
        if (hwBleAddress) hwBleAddress.value = data.device?.address || address;
        await loadRememberedBleDevices();
        const verifiedName = data.device?.name || name || '未命名手表';
        setBleConnectionStatus('success', `${verifiedName} · ${address}`, '连接成功，已设为截图设备');
        showToast('手表连接成功，已设为截图设备');
      } else if (action === 'select') {
        await api('/api/config', {
          method: 'POST',
          body: JSON.stringify({hardware: {ble_address: address}}),
        });
        if (hwBleAddress) hwBleAddress.value = address;
        bleRememberedDevices = bleRememberedDevices.map(device => ({
          ...device,
          selected: bleAddressKey(device.address) === bleAddressKey(address),
        }));
        renderBleDevices();
        refreshSelectedBleStatus();
        showToast('已设为截图设备');
      } else if (action === 'delete') {
        const data = await api(`/api/hardware/ble/remembered/${encodeURIComponent(address)}`, {method: 'DELETE'});
        bleRememberedDevices = bleRememberedDevices.filter(device => bleAddressKey(device.address) !== bleAddressKey(address));
        if (hwBleAddress && data.selected_address !== undefined) hwBleAddress.value = data.selected_address;
        renderBleDevices();
        refreshSelectedBleStatus();
        showToast('已删除设备记录');
      }
    } catch (error) {
      const issue = presentationFromError(error, action === 'connect' ? '电脑没有与这台手表建立可用连接。' : '本次蓝牙操作没有完成。');
      setBleIssue(issue);
      showToast(issue.summary, 'error');
    } finally {
      setBleBusy(false);
      renderBleDevices();
    }
  });

  const setLlmSignal = (element, text, tone = 'neutral') => {
    if (!element) return;
    element.textContent = text;
    element.dataset.tone = tone;
  };
  const setLlmTestStatus = (tone, summary, action = '', diagnostic = '') => {
    if (!llmTestStatus) return;
    llmTestStatus.dataset.tone = tone;
    const summaryNode = llmTestStatus.querySelector('[data-llm-test-summary]');
    const actionNode = llmTestStatus.querySelector('[data-llm-test-action]');
    const diagnosticNode = llmTestStatus.querySelector('[data-llm-test-diagnostics]');
    const diagnosticCode = diagnosticNode?.querySelector('code');
    if (summaryNode) summaryNode.textContent = summary;
    if (actionNode) {
      actionNode.textContent = action;
      actionNode.hidden = !action;
    }
    if (diagnosticNode) {
      diagnosticNode.hidden = !diagnostic;
      diagnosticNode.open = false;
    }
    if (diagnosticCode) diagnosticCode.textContent = diagnostic;
  };
  const setLlmTestIssue = issue => setLlmTestStatus(
    'warning',
    `问题原因：${issue.cause}`,
    `处理方法：${issue.action}`,
    issue.detail
  );

  // Tab switching
  dialog.querySelectorAll('.settings-tab-btn').forEach(tabBtn => {
    tabBtn.addEventListener('click', () => {
      const tabName = tabBtn.dataset.settingsTab;
      dialog.querySelectorAll('.settings-tab-btn').forEach(b => b.classList.toggle('is-active', b === tabBtn));
      dialog.querySelectorAll('.settings-tab-pane').forEach(pane => {
        pane.style.display = (pane.dataset.settingsPane === tabName) ? 'flex' : 'none';
      });
      if (tabName === 'hardware') {
        void loadSerialPorts(hwPortSelect?.value);
      }
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
    bleDiscoveredDevices = [];
    bleRememberedDevices = [];
    bleRememberedLoaded = false;
    bleHasScanned = false;
    if (bleSearchInput) bleSearchInput.value = '';
    renderBleDevices();
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
      const hwBaud = document.querySelector('#cfg-hw-baudrate');
      const hwTrans = document.querySelector('#cfg-hw-transport');
      const hwCap = document.querySelector('#cfg-hw-capture');
      if (hwBaud) hwBaud.value = cfg.hardware?.baudrate || 1500000;
      if (hwTrans) hwTrans.value = cfg.hardware?.transport || 'supercom';
      if (hwCap) hwCap.value = cfg.hardware?.capture_provider || 'mtp';
      await loadSerialPorts();
    } catch (err) {
      if (errorBox) {
        errorBox.textContent = `读取配置失败：${err.message}`;
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
      btnTestLlm.textContent = '正在测试…';
      setLlmTestStatus('pending', '正在测试连接…');
      try {
        const resp = await fetch('/api/config/test-llm', { method: 'POST' });
        const data = await resp.json();
        if (data.ok) {
          setLlmTestStatus('success', `连接成功 · ${data.latency_ms}ms`);
        } else {
          const detail = data.error || data.message || '未知错误';
          const reasonCode = data.error_code || data.reason_code || data.error_category || '';
          setLlmTestIssue(issuePresentation({reasonCode, detail, status: resp.status}));
        }
      } catch (err) {
        setLlmTestIssue(issuePresentation({reasonCode: 'LLM_REQUEST_FAILED', detail: err?.message || err}));
      } finally {
        btnTestLlm.disabled = false;
        btnTestLlm.textContent = '测试连接';
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
          onesLoginStatus.textContent = '请先输入 ONES 账号和密码';
        }
        return;
      }

      if (onesLoginStatus) {
        onesLoginStatus.style.color = 'var(--ink-soft)';
        onesLoginStatus.textContent = '正在登录 ONES…';
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
          throw new Error(productApiError(data?.error || data?.desc || data?.reason || '登录失败', resp.status, data?.reason_code));
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
          onesLoginStatus.textContent = '登录成功，凭据已保存';
        }
        const pwdInput = document.querySelector('#cfg-ones-password');
        if (pwdInput) pwdInput.value = '';
      } catch (err) {
        if (onesLoginStatus) {
          onesLoginStatus.style.color = '#ef4444';
          onesLoginStatus.textContent = `登录失败：${err.message || err}`;
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
        if (!resp.ok) throw new Error(productApiError(resData.error || resData.message || '保存失败', resp.status, resData.reason_code));

        showToast('设置已保存');
        dialog.close();
      } catch (err) {
        if (errorBox) {
          errorBox.textContent = `保存失败：${err.message}`;
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
  return {PASS: '通过', FAIL: '失败', ERROR: '执行异常', CANNOT_VERIFY: '无法验证', SKIP: '无法验证', PENDING: '未运行', RUNNING: '运行中'}[key] || key;
}

function environmentHealth(item = null) {
  const raw = String(item?.status || item?.readiness_status || 'unchecked').toLowerCase();
  if (['ready', 'pass', 'healthy'].includes(raw)) return {status: 'ready', label: '就绪'};
  if (['partial', 'warning', 'degraded'].includes(raw)) return {status: 'partial', label: '部分就绪'};
  if (['blocked'].includes(raw)) return {status: 'error', label: '受门禁阻止'};
  if (['error', 'fail', 'failed', 'unhealthy'].includes(raw)) return {status: 'error', label: '检查失败'};
  return {status: 'unchecked', label: '尚未检查'};
}

function renderEnvironmentTargetCard(profile, environmentItem = null, selectedProject = currentProject(), selectedTarget = '') {
  const project = String(profile.project || '');
  const projectProtocol = ENVIRONMENT_PROTOCOLS[project] || {};
  const protocol = {
    transport: profile.transport || projectProtocol.transport || 'unknown',
    transportLabel: profile.transport_label || projectProtocol.transportLabel || profile.transport || '未知',
    captureProvider: profile.capture_provider || projectProtocol.captureProvider || 'unknown',
    captureLabel: profile.capture_label || projectProtocol.captureLabel || profile.capture_provider || '未知'
  };
  const health = environmentHealth(environmentItem);
  const status = health.status;
  const statusLabel = health.label;
  const iconName = profile.execution_target === 'hardware' ? 'environments' : 'runs';
  const selected = selectedTarget ? profile.target_id === selectedTarget : project === selectedProject;
  return `<article class='target-health-card is-${escapeHtml(status)} ${selected ? 'is-selected' : ''}'>
    <div class='target-health-icon'>${icon(iconName, 23)}</div>
    <div class='target-health-copy'><div><strong>${escapeHtml(profile.project_label || project)}</strong><span>${escapeHtml(profile.execution_target_label || '')}</span></div><p class='health-status'><i></i>${escapeHtml(statusLabel)}</p></div>
    <dl><div><dt>命令通道</dt><dd>${escapeHtml(protocol.transportLabel)}</dd></div><div><dt>截图方式</dt><dd>${escapeHtml(protocol.captureLabel)}</dd></div><div><dt>最后检查</dt><dd>${environmentItem?.last_checked_at ? formatTime(environmentItem.last_checked_at) : '—'}</dd></div></dl>
    <a class='button button-secondary target-card-action' href='${escapeHtml(pageUrl('/environments', project, {platform_id: profile.platform_id, target_id: profile.target_id}))}'>查看环境</a>
  </article>`;
}

function renderDistributionDonut(counts = {}) {
  const pass = Number(counts.PASS || counts.pass || 0);
  const fail = Number(counts.FAIL || counts.fail || 0);
  const legacyError = Number(counts.ERROR || counts.error || 0);
  const cannot = Number(counts.CANNOT_VERIFY || counts.cannot_verify || 0);
  const total = pass + fail + legacyError + cannot;
  const passEnd = total ? pass * 100 / total : 0;
  const failEnd = total ? passEnd + fail * 100 / total : 0;
  const errorEnd = total ? failEnd + legacyError * 100 / total : 0;
  const background = total
    ? `conic-gradient(var(--green) 0 ${passEnd}%, var(--red) ${passEnd}% ${failEnd}%, #e6a11f ${failEnd}% ${errorEnd}%, #a4a7aa ${errorEnd}% 100%)`
    : 'conic-gradient(var(--line) 0 100%)';
  return `<div class='distribution-layout'><div class='distribution-donut' style='background:${background}' role='img' aria-label='通过 ${pass}，失败 ${fail}，执行异常 ${legacyError}，无法验证 ${cannot}'><div><strong>${total.toLocaleString('zh-CN')}</strong><span>已运行用例</span></div></div><ul class='distribution-legend'><li><i class='is-pass'></i><span>通过</span><strong>${pass.toLocaleString('zh-CN')}</strong></li><li><i class='is-fail'></i><span>失败</span><strong>${fail.toLocaleString('zh-CN')}</strong></li><li><i class='is-error'></i><span>执行异常</span><strong>${legacyError.toLocaleString('zh-CN')}</strong></li><li><i class='is-cannot'></i><span>无法验证</span><strong>${cannot.toLocaleString('zh-CN')}</strong></li></ul></div>`;
}

function renderCaseSnapshotRows(items = [], project = currentProject(), limit = 6) {
  const rows = items.slice(0, limit).map(item => {
    const sheet = String(item.file_sheet || item.sheet || '');
    const caseId = String(item.case_id || '');
    return `<tr><td><a class='case-id-link' href='${escapeHtml(testDetailHref(project, sheet, caseId, pageUrl('/overview', project)))}'>${escapeHtml(caseId)}</a></td><td>${escapeHtml(sheet)}</td><td>${Components.statusChip(item.latest_verdict, workspaceVerdictLabel(item.latest_verdict))}</td><td><time>${item.last_run_at ? formatTime(item.last_run_at) : '—'}</time></td></tr>`;
  }).join('');
  if (!rows) return Components.emptyState('暂无运行记录', '完成首条用例后，结果会显示在这里。');
  return `<div class='workspace-table-scroll'><table class='workspace-table compact-table'><thead><tr><th>用例</th><th>模块</th><th>结果</th><th>最近运行</th></tr></thead><tbody>${rows}</tbody></table></div>`;
}

function OverviewPage(project = currentProject()) {
  let refreshTimer = null;
  let refreshPromise = null;
  let deferredPromise = null;
  let currentData = null;
  let destroyed = false;

  function hasActiveJobs(data) {
    return Boolean(data?.active?.data && activeTestJobs(data.active.data).length);
  }

  async function loadLiveData() {
    const [catalog, active, reports] = await Promise.all([
      api(`/api/tests/overview?project=${encodeURIComponent(project)}&recent_limit=7&exception_limit=6`),
      optionalApi('/api/tests/active'),
      optionalApi(`/api/reports/summary?project=${encodeURIComponent(project)}`)
    ]);
    return {...currentData, catalog, active, reports, refreshedAt: new Date()};
  }

  async function loadDeferredSections(root) {
    if (destroyed) return;
    if (deferredPromise) return deferredPromise;
    deferredPromise = (async () => {
      const [defects, reports] = await Promise.all([
        optionalApi('/api/defects?page=1&page_size=6'),
        optionalApi(`/api/reports/summary?project=${encodeURIComponent(project)}`)
      ]);
      if (destroyed) return;
      const nextData = {...currentData, defects, reports};
      patchRenderedSections(root, controller.render(nextData), 'data-overview-deferred');
      currentData = nextData;
    })();
    try {
      await deferredPromise;
    } finally {
      deferredPromise = null;
    }
  }

  function scheduleRefresh(root) {
    clearTimeout(refreshTimer);
    refreshTimer = null;
    if (!destroyed && hasActiveJobs(currentData)) {
      refreshTimer = setTimeout(() => refreshLiveSections(root), 2000);
    }
  }

  async function refreshLiveSections(root) {
    clearTimeout(refreshTimer);
    refreshTimer = null;
    if (destroyed) return;
    if (refreshPromise) return refreshPromise;
    refreshPromise = (async () => {
      try {
        const nextData = await loadLiveData();
        if (destroyed) return;
        patchRenderedSections(root, controller.render(nextData), 'data-overview-live');
        currentData = nextData;
      } catch { /* 后台轮询失败时保留当前页面，下一轮继续尝试 */ }
    })();
    try {
      await refreshPromise;
    } finally {
      refreshPromise = null;
      scheduleRefresh(root);
    }
  }

  const controller = {
    async load() {
      const [catalog, active, repair, environments] = await Promise.all([
        api(`/api/tests/overview?project=${encodeURIComponent(project)}&recent_limit=7&exception_limit=6`),
        optionalApi('/api/tests/active'),
        optionalApi('/api/run/active'),
        optionalApi('/api/environments')
      ]);
      const pending = {available: true, data: null, error: null, pending: true};
      return {
        catalog,
        active,
        defects: {...pending},
        repair,
        reports: {...pending},
        environments,
        refreshedAt: new Date()
      };
    },
    render(data) {
      const {catalog, active, defects, repair, reports, environments, refreshedAt} = data;
      const platformId = currentPlatformFor(project);
      const jobs = active.data ? activeTestJobs(active.data).filter(job => !job.project || job.project === project) : [];
      const activeBatch = jobs.find(job => job.type === 'batch') || null;
      const verdicts = catalog.verdict_counts || {};
      const recentExceptions = catalog.recent_exceptions || [];
      const recentCases = catalog.recent_cases || [];
      const projectMeta = testProject(project);
      const profiles = targetsFor(project, platformId).map(target => ({
        ...projectMeta,
        ...target,
        project,
        project_label: projectMeta.projectLabel,
        platform_id: platformId,
      }));
      const environmentItems = environments.data?.items || [];
      const total = Number(catalog.summary?.all || 0);
      const completed = activeBatch ? Number(activeBatch.completed || 0) : 0;
      const batchTotal = activeBatch ? Number(activeBatch.total || 0) : 0;
      const percent = batchTotal ? Math.round(completed * 100 / batchTotal) : 0;
      const batchCounts = activeBatch?.verdict_counts || {};
      const activeBatchHtml = activeBatch ? `<article class='workspace-panel active-batch-card'><header><div><h2>执行批次</h2><span class='chip chip-running'>进行中</span></div><a class='button button-secondary' href='${escapeHtml(testBatchHref(activeBatch.id))}'>查看详情</a></header><div class='active-batch-layout'>${Components.progressRing(percent)}<div class='batch-facts'><dl><div><dt>执行环境</dt><dd>${escapeHtml(testProject(activeBatch.project || project).projectLabel)} · ${escapeHtml(testProject(activeBatch.project || project).targetLabel)}</dd></div><div><dt>执行用例</dt><dd>${completed.toLocaleString('zh-CN')} / ${batchTotal.toLocaleString('zh-CN')}</dd></div><div><dt>开始时间</dt><dd>${formatTime(activeBatch.started_at)}</dd></div></dl><div class='batch-progress-track'><div class='batch-progress-bar' style='width:${percent}%'></div></div><div class='inline-verdicts'><span class='is-pass'>通过 ${Number(batchCounts.PASS || 0)}</span><span class='is-fail'>失败 ${Number(batchCounts.FAIL || 0)}</span><span class='is-error'>执行异常 ${Number(batchCounts.ERROR || 0)}</span><span class='is-cannot'>无法验证 ${Number(batchCounts.CANNOT_VERIFY || 0)}</span></div></div></div></article>` : `<article class='workspace-panel active-batch-card'><header><div><h2>执行批次</h2><span class='chip chip-pending'>当前空闲</span></div><a class='button' href='${escapeHtml(pageUrl('/cases', project))}'>新建执行</a></header>${Components.emptyState('当前没有运行中的批次', '可从用例管理勾选用例，或按状态创建批次。')}</article>`;
      const reportAvailable = Boolean(reports.data);
      const reportPending = Boolean(reports.pending);
      const reportCounts = reports.data?.distribution || verdicts;
      const defectAvailable = Boolean(defects.data);
      const defectSummary = defects.data?.summary || {};
      const defectValue = key => defectAvailable ? Number(defectSummary[key] || 0) : '—';
      const repairJob = repair.data?.job || null;
      const maturity = catalog.automation_maturity_counts || {};
      const platformSummary = platformId === '579' ? `<section class='workspace-panel platform-summary-panel'><header><div><h2>579 自动化成熟度与链路</h2><p>APP 交付与设备效果证据分开显示</p></div><a class='button button-secondary' href='${escapeHtml(pageUrl('/environments', project, {platform_id: '579', target_id: '579.o2'}))}'>检查 579 环境</a></header><div class='digest-items'><div><span>${automationMaturityLabel('AUTO_READY')}</span><strong>${Number(maturity.AUTO_READY || 0)}</strong></div><div><span>${automationMaturityLabel('NEED_REVIEW')}</span><strong>${Number(maturity.NEED_REVIEW || 0)}</strong></div><div><span>${automationMaturityLabel('MANUAL_REQUIRED')}</span><strong>${Number(maturity.MANUAL_REQUIRED || 0)}</strong></div><div><span>${automationMaturityLabel('UNSUPPORTED')}</span><strong>${Number(maturity.UNSUPPORTED || 0)}</strong></div><div><span>控制链</span><strong>APP Bridge</strong></div><div><span>观察链</span><strong>COM3/O1 + O2</strong></div></div></section>` : '';
      return `${Components.pageHeader({title: '项目总览', intro: '实时掌握测试执行状态、用例质量与环境健康度', updatedAt: formatTime(refreshedAt), actions: `<div class='platform-choice-group'>${platformSwitchButtons(project, platformId, 'data-overview-platform')}</div><button class='button button-secondary' type='button' data-overview-refresh>${icon('refresh', 17)} 刷新</button>`})}
        ${platformSummary}
        <section class='workspace-panel environment-overview-panel'><header><div><h2>环境就绪状态</h2><p>协议来自项目配置；就绪状态只采用真实环境检查结果</p></div><a class='button button-secondary' href='${escapeHtml(pageUrl('/environments', project, {platform_id: platformId}))}'>环境中心 ›</a></header><div class='target-health-grid'>${profiles.map(profile => renderEnvironmentTargetCard(profile, environmentItems.find(item => item.target_id === profile.target_id || item.id === profile.target_id || item.project === profile.project), project, currentTargetFor(project, platformId))).join('')}</div></section>
        <section class='workspace-kpi-grid' data-overview-live='metrics'>${Components.metricCard({label: '用例总数', value: total.toLocaleString('zh-CN'), hint: '当前项目', tone: 'blue', iconName: 'cases'})}${Components.metricCard({label: '已固化', value: Number(catalog.summary?.solidified || 0).toLocaleString('zh-CN'), hint: total ? `${(Number(catalog.summary?.solidified || 0) * 100 / total).toFixed(1)}%` : '—', tone: 'green', iconName: 'check'})}${Components.metricCard({label: '运行中', value: String(jobs.length), hint: jobs.length ? '活动任务' : '当前空闲', tone: 'amber', iconName: 'runs'})}${Components.metricCard({label: '最新结果 PASS', value: verdicts.PASS.toLocaleString('zh-CN'), tone: 'green', iconName: 'check'})}${Components.metricCard({label: '最新结果 FAIL', value: verdicts.FAIL.toLocaleString('zh-CN'), tone: 'red', iconName: 'warning'})}${Components.metricCard({label: '最新结果 ERROR', value: verdicts.ERROR.toLocaleString('zh-CN'), tone: 'red', iconName: 'warning'})}</section>
        <section class='overview-primary-grid' data-overview-live='primary'>${activeBatchHtml}<article class='workspace-panel recent-exceptions-panel'><header><div><h2>最近异常</h2><p>按用例最近一次真实结果排序</p></div><a class='text-button' href='${escapeHtml(pageUrl('/reports', project, {view: 'failures'}))}'>查看更多</a></header>${renderCaseSnapshotRows(recentExceptions, project, 6)}</article></section>
        <section class='overview-secondary-grid' data-overview-live='secondary'><article class='workspace-panel'><header><div><h2>最新用例</h2><p>最近发生运行的用例</p></div><a class='text-button' href='${escapeHtml(pageUrl('/cases', project))}'>全部用例</a></header>${renderCaseSnapshotRows(recentCases, project, 7)}</article><article class='workspace-panel' data-overview-deferred='report'><header><div><h2>报告概览</h2><p>${reportAvailable ? '汇总结果' : '当前用例最新结果'}</p></div><a class='text-button' href='${escapeHtml(pageUrl('/reports', project))}'>打开报告</a></header>${renderDistributionDonut(reportCounts)}${!reportAvailable ? Components.unavailableState('趋势数据暂不可用', '当前可继续查看最近一次测试结果。') : ''}</article></section>
        <section class='workspace-panel defect-overview-strip' data-overview-deferred='defects'><header><div><h2>ONES 缺陷概览</h2><p>缺陷队列与当前自动修复任务</p></div><a class='button button-secondary' href='${escapeHtml(pageUrl('/defects', project))}'>查看 ONES 缺陷</a></header><div class='digest-items'><div><span>全部缺陷</span><strong>${Number(defectSummary.all || 0)}</strong></div><div><span>已通过</span><strong>${Number(defectSummary.passed || 0)}</strong></div><div><span>失败</span><strong>${Number(defectSummary.failed || 0)}</strong></div><div><span>待处理</span><strong>${Number(defectSummary.pending || 0)}</strong></div><div><span>当前修复任务</span><strong>${repairJob ? escapeHtml(repairJob.id || '运行中') : '无'}</strong></div></div></section>
        <section class='workspace-panel daily-digest' data-overview-deferred='daily'><header><div><h2>今日运营概览</h2><p>${reportAvailable ? '测试数据汇总' : '部分统计暂不可用'}</p></div></header><div class='digest-items'><div><span>运行批次</span><strong>${reportAvailable ? Number(reports.data.metrics?.batches || 0) : '—'}</strong></div><div><span>通过率</span><strong>${reportAvailable ? `${Number(reports.data.metrics?.pass_rate || 0).toFixed(1)}%` : '—'}</strong></div><div><span>缺陷修复</span><strong>${reportAvailable ? Number(reports.data.metrics?.repairs || 0) : '—'}</strong></div><div><span>无法验证</span><strong>${reportAvailable ? Number(reports.data.metrics?.cannot_verify || 0) : '—'}</strong></div></div></section>`;
    },
    mount(root, data) {
      rememberProject(project);
      currentData = data;
      const currentEnvironment = data.environments.data?.items?.find(item => item.id === project || item.project === project);
      const health = environmentHealth(currentEnvironment);
      setGlobalTargetHealth(health.status, health.label);
      root.querySelector('[data-overview-refresh]')?.addEventListener('click', () => {
        invalidateCaseCatalog(project);
        route();
      });
      root.querySelectorAll('[data-overview-platform]').forEach(button => button.addEventListener('click', () => {
        history.pushState({}, '', pageUrl('/overview', project, {platform_id: button.dataset.overviewPlatform}));
        invalidateCaseCatalog(project);
        route();
      }));
      void loadDeferredSections(root);
      scheduleRefresh(root);
    },
    destroy() {
      destroyed = true;
      clearTimeout(refreshTimer);
      refreshTimer = null;
    }
  };
  return controller;
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
  const is579 = String(job.requested_platform_id || job.platform_id || '') === '579';
  const screenshotCards = `<div class='execution-evidence-grid'>${screenshots.slice(-4).map((item, index) => {
    const label = item.label || `${is579 ? 'O2 ' : ''}检查点 ${index + 1}`;
    return `<figure><a ${imagePreviewLinkAttributes(item.url || '#', label)}><img src='${escapeHtml(item.url || '')}' alt='${escapeHtml(label)}'></a><figcaption>${escapeHtml(label)}</figcaption></figure>`;
  }).join('')}</div>`;
  if (!is579) {
    if (!screenshots.length) return Components.emptyState('尚无实时截图', '截图检查点生成后会自动显示。');
    return screenshotCards;
  }
  const deliveries = Array.isArray(job.delivery_feedback) ? job.delivery_feedback : [];
  const observations = Array.isArray(job.observations) ? job.observations : [];
  const o1 = observations.filter(item => item.kind === 'o1_log_observation');
  const o2 = observations.filter(item => item.kind === 'o2_screenshot');
  const status = items => items.length ? `${items.length} 条` : '等待证据';
  return `<div class='execution-link-evidence'>
    <div><span>APP Bridge / BLE 交付</span><strong>${escapeHtml(status(deliveries))}</strong><small>仅代表动作交付，不等于产品 PASS</small></div>
    <div><span>COM3 / O1 只读日志</span><strong>${escapeHtml(status(o1))}</strong><small>串口仅观察，禁止写入</small></div>
    <div><span>O2 手表截图</span><strong>${escapeHtml(status(o2.length ? o2 : screenshots))}</strong><small>截图新鲜度与设备效果用于最终判定</small></div>
  </div>${screenshots.length ? screenshotCards : ''}`;
}

function executionRouteMeta(job = {}) {
  const requested = String(job.requested_platform_id || job.platform_id || 'w30');
  const adapter = String(job.resolved_execution_adapter || job.execution_adapter || '旧任务未标明');
  const target = PLATFORM_TARGETS[job.target_id]?.target_label || job.execution_target_label || job.target_id || '旧任务未标明';
  return `<div class='execution-route-meta'><span>请求平台 <strong>${escapeHtml(PLATFORM_PROFILES[requested]?.platform_label || requested)}</strong></span><span>实际适配器 <strong>${escapeHtml(adapter)}</strong></span><span>执行目标 <strong>${escapeHtml(target)}</strong></span>${job.product_verdict ? `<span>产品结果 <strong>${escapeHtml(job.product_verdict)}</strong></span>` : ''}${job.automation_maturity ? `<span>成熟度 <strong>${escapeHtml(job.automation_maturity)}</strong></span>` : ''}${job.infrastructure_status ? `<span>基础设施 <strong>${escapeHtml(job.infrastructure_status)}</strong></span>` : ''}</div>`;
}

function renderActiveExecution(job, project) {
  if (!job) return `<article class='workspace-panel execution-main-panel' data-execution-live='task'><header><div><h2>当前执行任务</h2><p>当前测试目标没有活动任务</p></div><a class='button' href='${escapeHtml(pageUrl('/cases', project))}'>${icon('plus', 17)} 新建执行任务</a></header>${Components.emptyState('执行队列为空', '从用例管理勾选用例后即可创建批次。')}</article>`;
  const completed = Number(job.completed || (job.status === 'completed' ? 1 : 0));
  const total = Number(job.total || 1);
  const percent = total ? Math.round(completed * 100 / total) : 0;
  const counts = job.verdict_counts || {};
  const currentCase = job.current_case || {case_id: job.case_id, sheet: job.sheet};
  const isActive = ['queued', 'running', 'finalizing'].includes(String(job.status));
  return `<article class='workspace-panel execution-main-panel' data-execution-live='task'><header><div><h2>${escapeHtml(job.type === 'batch' ? `${testProject(job.project || project).projectLabel} 批次执行` : `用例 ${job.case_id || ''}`)}</h2>${Components.statusChip(isActive ? 'RUNNING' : job.verdict || job.status, isActive ? '运行中' : workspaceVerdictLabel(job.verdict || job.status))}</div><div class='panel-actions'>${job.type === 'batch' ? `<a class='button button-secondary' href='${escapeHtml(testBatchHref(job.id))}'>查看详情</a>` : ''}${isActive && job.type === 'batch' ? `<button class='button button-secondary' type='button' data-job-pause='${escapeHtml(job.id)}'>${icon('pause', 16)} 暂停批次</button>` : ''}${job.resume_available ? `<button class='button' type='button' data-job-resume='${escapeHtml(job.id)}'>继续运行</button>` : ''}</div></header>${executionRouteMeta(job)}<div class='active-run-grid'><div class='active-run-progress'>${Components.progressRing(percent)}<div><strong>${completed.toLocaleString('zh-CN')} / ${total.toLocaleString('zh-CN')}</strong><div class='batch-progress-track'><div class='batch-progress-bar' style='width:${percent}%'></div></div><div class='inline-verdicts'><span class='is-pass'>通过 ${Number(counts.PASS || 0)}</span><span class='is-fail'>失败 ${Number(counts.FAIL || 0)}</span><span class='is-error'>执行异常 ${Number(counts.ERROR || 0)}</span><span class='is-cannot'>无法验证 ${Number(counts.CANNOT_VERIFY || 0)}</span></div></div></div><div class='current-case-card'><span>当前用例</span><strong>${escapeHtml(currentCase?.case_id || '正在准备')}</strong><small>${escapeHtml(currentCase?.sheet || testProject(job.project || project).targetLabel)}</small><div><span>当前阶段</span><strong>${escapeHtml(TEST_NODE_LABELS[job.current_node] || job.current_node || '等待调度')}</strong></div>${renderWorkflowStepper(job)}</div><aside class='live-evidence-card'><h3>最新证据</h3>${renderExecutionEvidence(job)}</aside></div></article>`;
}

function ExecutionPage(project = currentProject()) {
  let pollTimer = null;
  let refreshPromise = null;
  let currentData = null;
  let clickHandler = null;
  let mountedRoot = null;
  let destroyed = false;

  function hasActiveJobs(data) {
    return Boolean(data?.view === 'running' && data.active?.data && activeTestJobs(data.active.data).length);
  }

  function scheduleRefresh(root) {
    clearTimeout(pollTimer);
    pollTimer = null;
    if (!destroyed && hasActiveJobs(currentData)) {
      pollTimer = setTimeout(() => refreshLiveSections(root), 2000);
    }
  }

  async function refreshLiveSections(root) {
    clearTimeout(pollTimer);
    pollTimer = null;
    if (destroyed) return;
    if (refreshPromise) return refreshPromise;
    refreshPromise = (async () => {
      try {
        const nextData = await controller.load();
        if (destroyed) return;
        patchRenderedSections(root, controller.render(nextData), 'data-execution-live');
        currentData = nextData;
      } catch { /* 后台轮询失败时保留当前页面，下一轮继续尝试 */ }
    })();
    try {
      await refreshPromise;
    } finally {
      refreshPromise = null;
      scheduleRefresh(root);
    }
  }

  async function handleClick(event, root) {
    const subtab = event.target.closest?.('[data-subtab]');
    if (subtab && root.contains(subtab)) {
      history.pushState({}, '', pageUrl('/runs', project, {view: subtab.dataset.subtab}));
      route();
      return;
    }
    const pause = event.target.closest?.('[data-job-pause]');
    if (pause && root.contains(pause)) {
      if (!window.confirm('将在当前用例完成后暂停批次。继续吗？')) return;
      pause.disabled = true;
      try {
        await api(`/api/tests/jobs/${encodeURIComponent(pause.dataset.jobPause)}/cancel`, {method: 'POST', body: '{}'});
        showToast('已请求暂停', 'warning');
        await refreshLiveSections(root);
      } catch (error) {
        pause.disabled = false;
        showToast(error.message, 'error');
      }
      return;
    }
    const resume = event.target.closest?.('[data-job-resume]');
    if (resume && root.contains(resume)) {
      resume.disabled = true;
      try {
        await api(`/api/tests/jobs/${encodeURIComponent(resume.dataset.jobResume)}/resume`, {method: 'POST', body: '{}'});
        showToast('已继续运行', 'success');
        await refreshLiveSections(root);
      } catch (error) {
        resume.disabled = false;
        showToast(error.message, 'error');
      }
    }
  }

  const controller = {
    async load() {
      const view = ['queue', 'running', 'completed', 'interrupted'].includes(new URLSearchParams(location.search).get('view')) ? new URLSearchParams(location.search).get('view') : 'running';
      const platformId = currentPlatformFor(project);
      const [recent, active, jobs] = await Promise.all([
        optionalApi(`/api/tests/recent?project=${encodeURIComponent(project)}&platform_id=${encodeURIComponent(platformId)}&limit=8`),
        optionalApi('/api/tests/active'),
        optionalApi(`/api/tests/jobs?project=${encodeURIComponent(project)}&status=${encodeURIComponent(view)}&page=1&page_size=20`)
      ]);
      return {recent, active, jobs, view};
    },
    render(data) {
      const platformId = currentPlatformFor(project);
      const activeJobs = data.active.data ? activeTestJobs(data.active.data).filter(job => !job.project || job.project === project) : [];
      const activeJob = activeJobs.find(job => job.type === 'batch') || activeJobs[0] || null;
      const jobItems = data.jobs.data?.items || [];
      const recentItems = data.recent.data?.items || [];
      const recentResults = recentItems.map(item => `<tr><td>${formatTime(item.last_run_at)}</td><td><a class='case-id-link' href='${escapeHtml(testDetailHref(project, item.file_sheet || item.sheet, item.case_id, currentRouteUrl()))}'>${escapeHtml(item.case_id)}</a></td><td>${escapeHtml(item.sheet)}</td><td>${escapeHtml(PLATFORM_PROFILES[item.last_platform_id]?.platform_label || item.last_platform_id || '旧记录')}</td><td>${Components.statusChip(item.latest_verdict, workspaceVerdictLabel(item.latest_verdict))}</td><td>${Number(item.history_count || 0)} 次</td></tr>`).join('');
      const listArea = data.view === 'running'
        ? renderActiveExecution(activeJob, project)
        : data.jobs.data
          ? `<article class='workspace-panel'><header><div><h2>${escapeHtml({queue: '任务队列', completed: '已完成任务', interrupted: '已中断任务'}[data.view] || '任务')}</h2><p>来自任务列表接口</p></div></header>${jobItems.length ? `<div class='workspace-table-scroll'><table class='workspace-table'><thead><tr><th>任务</th><th>项目</th><th>请求平台</th><th>实际适配器</th><th>状态</th><th>进度</th><th>时间</th></tr></thead><tbody>${jobItems.map(job => `<tr><td>${escapeHtml(job.id)}</td><td>${escapeHtml(job.project_label || job.project || '')}</td><td>${escapeHtml(PLATFORM_PROFILES[job.requested_platform_id]?.platform_label || job.requested_platform_id || '旧任务')}</td><td>${escapeHtml(job.resolved_execution_adapter || '旧任务未标明')}</td><td>${Components.statusChip(job.verdict || job.status, workspaceVerdictLabel(job.verdict || job.status))}</td><td>${Number(job.completed || 0)} / ${Number(job.total || 1)}</td><td>${formatTime(job.finished_at || job.started_at)}</td></tr>`).join('')}</tbody></table></div>` : Components.emptyState('当前分类没有任务')}</article>`
          : `<article class='workspace-panel'>${Components.unavailableState('任务列表暂不可用', '当前仍可查看正在运行的任务。')}</article>`;
      return `${Components.pageHeader({title: '自动化执行', intro: '创建、监控和恢复单条或批量测试任务', actions: `<div class='platform-choice-group' aria-label='执行页平台视图'>${platformSwitchButtons(project, platformId, 'data-runs-platform')}</div><a class='button' href='${escapeHtml(pageUrl('/cases', project, {platform_id: platformId}))}'>${icon('runs', 17)} 新建执行任务</a><a class='button button-secondary' href='${escapeHtml(pageUrl('/cases', project, {platform_id: platformId}))}'>${icon('plus', 17)} 按状态创建批次</a>`})}${Components.subTabs([{value: 'queue', label: '任务队列'}, {value: 'running', label: '运行中'}, {value: 'completed', label: '已完成'}, {value: 'interrupted', label: '已中断'}], data.view)}<section class='workspace-kpi-grid is-four' data-execution-live='metrics'>${Components.metricCard({label: '运行中', value: String(activeJobs.filter(job => ['queued', 'running', 'finalizing'].includes(job.status)).length), tone: 'green', iconName: 'runs'})}${Components.metricCard({label: '队列中', value: data.jobs.data ? String(Number(data.jobs.data.summary?.queued || 0)) : '—', tone: 'amber', iconName: 'runs'})}${Components.metricCard({label: '今日完成', value: data.jobs.data ? String(Number(data.jobs.data.summary?.completed_today || 0)) : '—', tone: 'green', iconName: 'check'})}${Components.metricCard({label: '执行异常', value: data.jobs.data ? String(Number(data.jobs.data.summary?.error || 0)) : '—', tone: 'red', iconName: 'warning'})}</section>${listArea}<article class='workspace-panel execution-results-panel' data-execution-live='results'><header><div><h2>最近结果</h2><p>每条用例的最近一次结果</p></div><a class='text-button' href='${escapeHtml(pageUrl('/reports', project, {platform_id: platformId}))}'>测试报告</a></header>${recentResults ? `<div class='workspace-table-scroll'><table class='workspace-table'><thead><tr><th>时间</th><th>用例</th><th>模块</th><th>平台</th><th>结果</th><th>历史</th></tr></thead><tbody>${recentResults}</tbody></table></div>` : Components.emptyState('暂无运行结果')}</article>`;
    },
    mount(root, data) {
      rememberProject(project);
      currentData = data;
      mountedRoot = root;
      clickHandler = event => { void handleClick(event, root); };
      root.addEventListener('click', clickHandler);
      root.querySelectorAll('[data-runs-platform]').forEach(button => button.addEventListener('click', () => {
        history.pushState({}, '', pageUrl('/runs', project, {view: data.view, platform_id: button.dataset.runsPlatform}));
        invalidateCaseCatalog(project);
        route();
      }));
      scheduleRefresh(root);
    },
    destroy() {
      destroyed = true;
      clearTimeout(pollTimer);
      pollTimer = null;
      if (clickHandler && mountedRoot) mountedRoot.removeEventListener('click', clickHandler);
      mountedRoot = null;
    }
  };
  return controller;
}

function isoDateOffset(days = 0) {
  const date = new Date();
  date.setDate(date.getDate() + days);
  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, '0');
  const day = String(date.getDate()).padStart(2, '0');
  return `${year}-${month}-${day}`;
}

const REPORT_DATE_PRESETS = [
  {value: '24h', label: '24小时', days: 2},
  {value: 'today', label: '今天', days: 1},
  {value: '7d', label: '7天', days: 7},
  {value: '30d', label: '30天', days: 30}
];

function reportDatePresetRange(value) {
  const preset = REPORT_DATE_PRESETS.find(item => item.value === value) || REPORT_DATE_PRESETS[2];
  return {from: isoDateOffset(1 - preset.days), to: isoDateOffset(0)};
}

function reportParams() {
  const params = new URLSearchParams(location.search);
  const view = ['overview', 'batches', 'cases', 'failures'].includes(params.get('view')) ? params.get('view') : 'overview';
  const module = ALL_FUNCTION_MODULES.includes(params.get('module')) ? params.get('module') : '';
  const period = params.get('period') === '24h' ? '24h' : '';
  const defaults = reportDatePresetRange(period || '7d');
  const from = !period && /^\d{4}-\d{2}-\d{2}$/.test(params.get('from') || '') ? params.get('from') : defaults.from;
  const to = !period && /^\d{4}-\d{2}-\d{2}$/.test(params.get('to') || '') ? params.get('to') : defaults.to;
  const platform = currentPlatformFor(currentProject());
  const result = ['PASS', 'FAIL', 'ERROR', 'CANNOT_VERIFY'].includes(params.get('result')) ? params.get('result') : '';
  const maturity = ['AUTO_READY', 'PROMOTED', 'NEED_REVIEW', 'MANUAL_REQUIRED', 'UNSUPPORTED', 'UNMAPPED'].includes(params.get('maturity')) ? params.get('maturity') : '';
  const infrastructure = ['READY', 'ENV_BLOCKED', 'OBSERVATION_INCOMPLETE', 'CLEANUP_REQUIRED', 'RUNNER_ERROR', 'CAPABILITY_MISSING', 'UNKNOWN'].includes(params.get('infrastructure')) ? params.get('infrastructure') : '';
  return {view, module, from, to, period, platform, result, maturity, infrastructure};
}

function activeReportDatePreset(filters = {}) {
  if (filters.period === '24h') return '24h';
  const match = REPORT_DATE_PRESETS.filter(item => item.value !== '24h').find(item => {
    const range = reportDatePresetRange(item.value);
    return filters.from === range.from && filters.to === range.to;
  });
  return match?.value || '';
}

function reportQuery(project, filters = {}) {
  const query = new URLSearchParams({project, from: filters.from, to: filters.to});
  if (filters.period === '24h') query.set('period', '24h');
  if (filters.module) query.set('module', filters.module);
  if (filters.platform) query.set('platform_id', filters.platform);
  if (filters.result) query.set('result', filters.result);
  if (filters.maturity) query.set('maturity', filters.maturity);
  if (filters.infrastructure) query.set('infrastructure', filters.infrastructure);
  return query;
}

function reportRangeLabel(filters = {}) {
  return filters.period === '24h' ? '最近24小时' : `${filters.from} 至 ${filters.to}`;
}

function buildSnapshotReport(items = [], filters = {}) {
  const now = Date.now();
  const fromTime = filters.period === '24h' ? now - 24 * 60 * 60 * 1000 : filters.from ? new Date(`${filters.from}T00:00:00`).getTime() : 0;
  const toTime = filters.period === '24h' ? now : filters.to ? new Date(`${filters.to}T23:59:59.999`).getTime() : Number.MAX_SAFE_INTEGER;
  const executed = items.filter(item => {
    if (!item.last_run_at) return false;
    if (filters.module && String(item.file_sheet || item.sheet) !== filters.module) return false;
    if (filters.platform && String(item.last_platform_id || item.platform_id) !== filters.platform) return false;
    if (filters.result && String(item.last_product_verdict || item.latest_verdict).toUpperCase() !== filters.result) return false;
    if (filters.maturity && String(item.last_automation_maturity || item.automation_maturity || item.mapping_status).toUpperCase() !== filters.maturity) return false;
    if (filters.infrastructure && String(item.last_infrastructure_status || 'UNKNOWN').toUpperCase() !== filters.infrastructure) return false;
    const time = new Date(item.last_run_at).getTime();
    return Number.isFinite(time) && time >= fromTime && time <= toTime;
  });
  const distribution = verdictCounts(executed);
  const total = distribution.PASS + distribution.FAIL + distribution.ERROR + distribution.CANNOT_VERIFY;
  const executionError = executed.filter(item => (
    String(item.latest_reason_code || '').toUpperCase() !== 'USER_CANCELLED'
    && (
      String(item.latest_execution_status || '').toUpperCase() === 'ERROR'
      || ['failed', 'interrupted', 'orphaned'].includes(String(item.latest_workflow_status || '').toLowerCase())
    )
  )).length;
  const moduleMap = new Map();
  const executionModuleMap = new Map();
  for (const item of executed) {
    const verdict = String(item.latest_verdict || '').toUpperCase();
    const moduleName = String(item.file_sheet || item.sheet || '未分类');
    if (verdict === 'FAIL') {
      const current = moduleMap.get(moduleName) || {module: moduleName, fail: 0, total: 0};
      current.fail += 1;
      current.total += 1;
      moduleMap.set(moduleName, current);
    }
    const isExecutionError = String(item.latest_reason_code || '').toUpperCase() !== 'USER_CANCELLED'
      && (String(item.latest_execution_status || '').toUpperCase() === 'ERROR'
        || ['failed', 'interrupted', 'orphaned'].includes(String(item.latest_workflow_status || '').toLowerCase()));
    if (isExecutionError) {
      const current = executionModuleMap.get(moduleName) || {module: moduleName, execution_error: 0};
      current.execution_error += 1;
      executionModuleMap.set(moduleName, current);
    }
  }
  const topFailModules = [...moduleMap.values()].sort((a, b) => b.total - a.total).slice(0, 8);
  const topExecutionErrorModules = [...executionModuleMap.values()].sort((a, b) => b.execution_error - a.execution_error).slice(0, 8);
  const recentFailures = executed.filter(item => ['FAIL', 'ERROR', 'CANNOT_VERIFY', 'SKIP'].includes(String(item.latest_verdict || '').toUpperCase())).sort((left, right) => String(right.last_run_at).localeCompare(String(left.last_run_at))).slice(0, 12);
  return {
    source: 'snapshot',
    metrics: {total, pass: distribution.PASS, fail: distribution.FAIL, error: distribution.ERROR, execution_error: executionError, cannot_verify: distribution.CANNOT_VERIFY, pass_rate: total ? distribution.PASS * 100 / total : 0},
    distribution,
    trend: [],
    top_fail_modules: topFailModules,
    top_execution_error_modules: topExecutionErrorModules,
    recent_failures: recentFailures,
    insight: null
  };
}

function renderTrendChart(trend = []) {
  if (!Array.isArray(trend) || !trend.length) return Components.unavailableState('暂无趋势数据', '当前仅显示最近测试结果。');
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
  const rows = items.map(item => `<tr><td><strong>${escapeHtml(item.module || item.sheet || '未分类')}</strong></td><td>${Number(item.fail || item.count || item.total || 0)}</td></tr>`).join('');
  return `<div class='workspace-table-scroll'><table class='workspace-table'><thead><tr><th>模块</th><th>产品失败</th></tr></thead><tbody>${rows}</tbody></table></div>`;
}

function renderExecutionErrorModules(items = []) {
  if (!items.length) return Components.emptyState('当前范围没有执行异常');
  const rows = items.map(item => `<tr><td><strong>${escapeHtml(item.module || item.sheet || '未分类')}</strong></td><td>${Number(item.execution_error || item.count || 0)}</td></tr>`).join('');
  return `<div class='workspace-table-scroll'><table class='workspace-table'><thead><tr><th>模块</th><th>执行异常</th></tr></thead><tbody>${rows}</tbody></table></div>`;
}

function renderRecentFailures(items = [], project = currentProject(), returnTo = pageUrl('/reports', project, {view: 'failures'})) {
  if (!items.length) return Components.emptyState('当前范围没有失败记录');
  const rows = items.map(item => {
    const caseId = item.case_id || item.caseId || '';
    const sheet = item.module || item.sheet || item.file_sheet || '';
    const historyId = item.history_id || item.run_id || '';
    const verdict = item.verdict || item.latest_verdict || 'FAIL';
    const executionAnomaly = String(item.reason_code || item.latest_reason_code || '').toUpperCase() !== 'USER_CANCELLED'
      && (String(item.execution_status || item.latest_execution_status || '').toUpperCase() === 'ERROR'
        || ['failed', 'interrupted', 'orphaned'].includes(String(item.workflow_status || item.latest_workflow_status || '').toLowerCase()));
    const time = item.at || item.timestamp || item.last_run_at;
    const historyHref = caseId && sheet && historyId ? testHistoryHref(project, sheet, caseId, historyId, returnTo) : '';
    const detailHref = historyHref || (caseId && sheet ? testDetailHref(project, sheet, caseId, returnTo) : '');
    const reason = testResultReason({
      ...item,
      verdict,
      reason: item.message || item.reason,
      reason_code: item.reason_code || item.latest_reason_code,
      workflow_status: item.workflow_status || item.latest_workflow_status,
      execution_status: item.execution_status || item.latest_execution_status
    }, '未记录原因');
    return `<tr><td>${time ? formatTime(time) : '—'}</td><td>${escapeHtml(sheet)}</td><td>${detailHref ? `<a class='case-id-link' href='${escapeHtml(detailHref)}'>${escapeHtml(caseId)}</a>` : escapeHtml(caseId)}</td><td>${Components.statusChip(executionAnomaly ? 'ERROR' : verdict, executionAnomaly ? '执行异常' : workspaceVerdictLabel(verdict))}</td><td>${escapeHtml(reason)}</td><td class='table-actions'>${detailHref ? `<a class='text-button' href='${escapeHtml(detailHref)}'>${historyHref ? '查看本次运行' : '查看用例'} →</a>` : '—'}</td></tr>`;
  }).join('');
  return `<div class='workspace-table-scroll'><table class='workspace-table'><thead><tr><th>时间</th><th>模块</th><th>用例</th><th>状态</th><th>原因</th><th>操作</th></tr></thead><tbody>${rows}</tbody></table></div>`;
}

function reportInsightPresentation(value = {}) {
  const rawTitle = String(value.title || '测试概况');
  const rawDescription = String(value.description || value.message || '');
  return {
    title: rawTitle === '测试稳定性与质量态势' ? '测试概况' : rawTitle,
    description: rawDescription
      .replaceAll('框架执行异常', '执行异常')
      .replaceAll('产品失败热点', '失败集中模块')
      .replace(/。\s+/g, '。')
  };
}

function ReportsPage(project = currentProject()) {
  return {
    async load() {
      const filters = reportParams();
      const query = reportQuery(project, filters);
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
      const insight = report.insight ? reportInsightPresentation(report.insight) : null;
      const reportReturnTo = pageUrl('/reports', project, {view: filters.view, from: filters.from, to: filters.to, module: filters.module, platform_id: filters.platform, result: filters.result, maturity: filters.maturity, infrastructure: filters.infrastructure});
      let content;
      if (filters.view === 'overview') {
        content = `<section class='report-chart-grid'><article class='workspace-panel'><header><div><h2>结果趋势</h2><p>执行数量和通过率</p></div><span class='chip chip-pending'>${escapeHtml(reportRangeLabel(filters))}</span></header>${renderTrendChart(report.trend)}</article><article class='workspace-panel'><header><div><h2>结果分布</h2><p>产品结果与执行异常分开统计</p></div></header>${renderDistributionDonut(distribution)}</article></section><section class='report-detail-grid'><article class='workspace-panel'><header><div><h2>失败较多的模块</h2><p>仅统计产品功能失败</p></div></header>${renderFailureModules(report.top_fail_modules || [])}</article><article class='workspace-panel'><header><div><h2>执行异常较多的模块</h2><p>测试未能正常完成</p></div></header>${renderExecutionErrorModules(report.top_execution_error_modules || [])}</article></section><article class='workspace-panel'><header><div><h2>最近失败与执行异常</h2><p>当前筛选范围</p></div></header>${renderRecentFailures(report.recent_failures || [], project, reportReturnTo)}</article>${insight ? `<aside class='report-insight'>${icon('reports', 22)}<div><strong>${escapeHtml(insight.title)}</strong><p>${escapeHtml(insight.description)}</p></div></aside>` : Components.unavailableState('暂无分析结论')}`;
      } else if (filters.view === 'failures') {
        content = `<article class='workspace-panel'><header><div><h2>失败分析</h2><p>当前筛选范围内的失败与异常记录</p></div></header>${renderRecentFailures(report.recent_failures || [], project, reportReturnTo)}</article>`;
      } else {
        content = `<article class='workspace-panel'>${Components.unavailableState(filters.view === 'batches' ? '暂不支持查看批次报告' : '暂不支持查看单条记录列表', '可在用例详情中查看每次运行记录。')}</article>`;
      }
      const activePreset = activeReportDatePreset(filters);
      const presetButtons = REPORT_DATE_PRESETS.map(item => `<button class='report-period-option ${activePreset === item.value ? 'is-active' : ''}' type='button' data-report-period='${escapeHtml(item.value)}' aria-pressed='${activePreset === item.value}'>${escapeHtml(item.label)}</button>`).join('');
      const resultOptions = ['PASS', 'FAIL', 'ERROR', 'CANNOT_VERIFY'].map(value => `<option value='${value}' ${filters.result === value ? 'selected' : ''}>${escapeHtml(workspaceVerdictLabel(value))}</option>`).join('');
      const maturityOptions = ['AUTO_READY', 'PROMOTED', 'NEED_REVIEW', 'MANUAL_REQUIRED', 'UNSUPPORTED', 'UNMAPPED'].map(value => `<option value='${value}' ${filters.maturity === value ? 'selected' : ''}>${escapeHtml(automationMaturityLabel(value))}</option>`).join('');
      const infrastructureOptions = ['READY', 'ENV_BLOCKED', 'OBSERVATION_INCOMPLETE', 'CLEANUP_REQUIRED', 'RUNNER_ERROR', 'CAPABILITY_MISSING', 'UNKNOWN'].map(value => `<option value='${value}' ${filters.infrastructure === value ? 'selected' : ''}>${escapeHtml(value)}</option>`).join('');
      return `${Components.pageHeader({title: '测试报告', intro: '查看测试结果、趋势和证据', actions: `<div class='platform-choice-group' aria-label='报告平台筛选'>${platformSwitchButtons(project, filters.platform, 'data-report-platform')}</div><button class='button button-secondary' type='button' data-report-export ${summary.data ? '' : 'disabled title="当前暂无可导出的完整报告"'}>${icon('reports', 17)} 导出报告</button>`})}
        <section class='report-filter-bar'>
          <label>项目<strong>${escapeHtml(testProject(project).projectLabel)}</strong></label>
          <label>平台<strong>${escapeHtml(PLATFORM_PROFILES[filters.platform]?.platform_label || filters.platform)}</strong></label>
          <label>开始日期<input id='report-from' type='date' value='${escapeHtml(filters.from)}'></label>
          <label>结束日期<input id='report-to' type='date' value='${escapeHtml(filters.to)}'></label>
          <div class='report-period-field'><span>快捷筛选</span><div class='report-period-options' role='group' aria-label='快捷时间段'>${presetButtons}</div></div>
          <label>产品结果<select id='report-result'><option value=''>全部结果</option>${resultOptions}</select></label>
          <label>自动化成熟度<select id='report-maturity'><option value=''>全部成熟度</option>${maturityOptions}</select></label>
          <label>基础设施<select id='report-infrastructure'><option value=''>全部状态</option>${infrastructureOptions}</select></label>
          <label>模块<select id='report-module'><option value=''>全部模块</option>${ALL_FUNCTION_MODULES.map(name => `<option value='${escapeHtml(name)}' ${filters.module === name ? 'selected' : ''}>${escapeHtml(name)}</option>`).join('')}</select></label>
        </section>
        ${Components.subTabs([{value: 'overview', label: '报告概览'}, {value: 'batches', label: '批次报告'}, {value: 'cases', label: '单条记录'}, {value: 'failures', label: '失败分析'}], filters.view)}
        ${!summary.data ? `<aside class='data-source-banner'>${icon('warning', 18)}<span>部分统计暂不可用，当前仅显示最近结果。</span></aside>` : ''}
        <section class='workspace-kpi-grid is-six'>${Components.metricCard({label: '已运行用例', value: Number(metrics.total || 0).toLocaleString('zh-CN'), tone: 'blue', iconName: 'cases'})}${Components.metricCard({label: '通过', value: Number(metrics.pass || distribution.PASS || 0).toLocaleString('zh-CN'), tone: 'green', iconName: 'check'})}${Components.metricCard({label: '失败', value: Number(metrics.fail || distribution.FAIL || 0).toLocaleString('zh-CN'), tone: 'red', iconName: 'warning'})}${Components.metricCard({label: '执行异常', value: Number(metrics.execution_error || metrics.error || distribution.ERROR || 0).toLocaleString('zh-CN'), tone: 'amber', iconName: 'warning'})}${Components.metricCard({label: '无法验证', value: Number(metrics.cannot_verify || distribution.CANNOT_VERIFY || 0).toLocaleString('zh-CN'), tone: 'gray', iconName: 'warning'})}${Components.metricCard({label: '通过率', value: `${Number(metrics.pass_rate || 0).toFixed(1)}%`, tone: 'green', iconName: 'reports'})}</section>${content}`;
    },
    mount(root, data) {
      rememberProject(project);
      const applyFilters = ({preservePeriod = false} = {}) => {
        const from = root.querySelector('#report-from')?.value || data.filters.from;
        const to = root.querySelector('#report-to')?.value || data.filters.to;
        const module = root.querySelector('#report-module')?.value || '';
        const result = root.querySelector('#report-result')?.value || '';
        const maturity = root.querySelector('#report-maturity')?.value || '';
        const infrastructure = root.querySelector('#report-infrastructure')?.value || '';
        history.pushState({}, '', pageUrl('/reports', project, {view: data.filters.view, from, to, module, platform_id: data.filters.platform, result, maturity, infrastructure, ...(preservePeriod && data.filters.period ? {period: data.filters.period} : {})}));
        route();
      };
      root.querySelector('#report-from')?.addEventListener('change', () => applyFilters());
      root.querySelector('#report-to')?.addEventListener('change', () => applyFilters());
      root.querySelector('#report-module')?.addEventListener('change', () => applyFilters({preservePeriod: true}));
      root.querySelector('#report-result')?.addEventListener('change', applyFilters);
      root.querySelector('#report-maturity')?.addEventListener('change', applyFilters);
      root.querySelector('#report-infrastructure')?.addEventListener('change', applyFilters);
      root.querySelectorAll('[data-report-period]').forEach(button => button.addEventListener('click', () => {
        const period = button.dataset.reportPeriod || '7d';
        const range = reportDatePresetRange(period);
        history.pushState({}, '', pageUrl('/reports', project, {view: data.filters.view, from: range.from, to: range.to, module: data.filters.module, platform_id: data.filters.platform, result: data.filters.result, maturity: data.filters.maturity, infrastructure: data.filters.infrastructure, ...(period === '24h' ? {period} : {})}));
        route();
      }));
      root.querySelectorAll('[data-report-platform]').forEach(button => button.addEventListener('click', () => {
        history.pushState({}, '', pageUrl('/reports', project, {...data.filters, platform_id: button.dataset.reportPlatform, platform: undefined}));
        invalidateCaseCatalog(project);
        route();
      }));
      root.querySelectorAll('[data-subtab]').forEach(button => button.addEventListener('click', () => {
        history.pushState({}, '', pageUrl('/reports', project, {view: button.dataset.subtab, from: data.filters.from, to: data.filters.to, module: data.filters.module, platform_id: data.filters.platform, result: data.filters.result, maturity: data.filters.maturity, infrastructure: data.filters.infrastructure, ...(data.filters.period ? {period: data.filters.period} : {})}));
        route();
      }));
      root.querySelector('[data-report-export]:not(:disabled)')?.addEventListener('click', () => {
        const query = reportQuery(project, data.filters);
        const url = `/api/reports/export?${query.toString()}`;
        startBrowserDownload(url, `test_report_${project}.xlsx`);
        showToast('测试报告已开始下载');
      });
    },
    destroy() {}
  };
}

function environmentSection() {
  const value = new URLSearchParams(location.search).get('section');
  return ['targets', 'llm', 'ones', 'updates'].includes(value) ? value : 'targets';
}

const ENVIRONMENT_CHECK_COPY = Object.freeze({
  source: {label: '项目文件', pass: '项目文件可用', warning: '项目文件需要配置', fail: '项目文件不可用', action: '请在系统设置中配置项目路径。'},
  profile: {label: '真机配置', pass: '真机配置可用', warning: '真机配置需要完善', fail: '真机配置不可用', action: '请重新选择测试项目；如仍失败，请联系维护人员。'},
  supercom_pipe: {label: '手表连接', pass: '手表连接可用', warning: '手表连接需要检查', fail: '手表连接不可用', action: '请打开 SuperCom，连接当前手表对应的串口后重新检查。'},
  usb_pnp: {label: 'USB 连接', pass: '手表已通过 USB 连接', warning: 'USB 连接需要检查', fail: 'USB 连接不可用', action: '请重新插拔 USB，确认电脑能够识别手表后重试。'},
  mtp_namespace: {label: '截图读取', pass: '可以读取手表截图', warning: '截图读取需要检查', fail: '暂时无法读取手表截图', action: '请重新连接 USB，并确认电脑能够打开手表存储后重试。'},
  gui_ping: {label: '手表响应', pass: '手表可以接收测试操作', warning: '手表响应需要检查', fail: '手表暂未响应测试操作', action: '请确认 SuperCom 连接的是当前手表，并唤醒手表屏幕后重试。'},
  target_busy: {label: '当前手表', pass: '当前手表可用', warning: '当前手表需要检查', fail: '当前手表正在使用中', action: '请等待该任务结束或停止后再检查。'},
  internal: {label: '环境检查', pass: '环境检查可用', warning: '环境检查需要重试', fail: '环境检查未完成', action: '请重新检查；如再次出现，展开诊断信息并联系维护人员。'},
  config: {label: '用例配置', pass: '用例配置可用', warning: '用例配置需要完善', fail: '用例配置不可用', action: '请在系统设置中检查用例配置。'},
  artifact: {label: '测试程序', pass: '测试程序可用', warning: '测试程序尚未准备好', fail: '测试程序不可用', action: '请在系统设置中检查测试程序路径或重新准备测试程序。'},
  command: {label: '操作能力', pass: '操作能力可用', warning: '操作能力需要检查', fail: '操作能力不可用', action: '请在系统设置中检查设备连接与操作设置。'},
  capture: {label: '截图能力', pass: '截图能力可用', warning: '截图能力需要检查', fail: '截图能力不可用', action: '请在系统设置中检查截图连接设置。'},
  llm: {label: '模型服务', pass: '模型服务可用', warning: '模型服务尚未配置或不可用', fail: '模型服务不可用', action: '请在系统设置中检查网络和模型服务配置。'}
});

function environmentCheckPresentation(item = {}, status = 'unchecked') {
  const key = String(item.key || '');
  const copy = ENVIRONMENT_CHECK_COPY[key] || {
    label: safeProductCopy(item.label) || '检查项',
    pass: '当前可用',
    warning: '需要检查',
    fail: '当前不可用',
    action: '请检查相关配置后重试。'
  };
  const diagnosticPayload = item.diagnostics && typeof item.diagnostics === 'object' && Object.keys(item.diagnostics).length
    ? JSON.stringify(item.diagnostics, null, 2)
    : '';
  const detail = [String(item.detail || '').trim(), diagnosticPayload].filter(Boolean).join('\n\n');
  const label = copy.label;
  if (['pass', 'ready'].includes(status)) {
    return {label, cause: copy.pass, action: '', summary: copy.pass, detail};
  }
  if (status === 'checking') {
    return {label, cause: '正在检查', action: '', summary: '正在读取本机状态…', detail: ''};
  }
  if (status === 'unchecked') {
    return {label, cause: '尚未检查', action: '', summary: '尚未检查', detail: ''};
  }
  const fallbackCause = status === 'warning' ? copy.warning : copy.fail;
  const known = resolveIssueDefinition(item.code);
  const cause = known ? known.cause : fallbackCause;
  const action = known?.action || safeProductCopy(item.action) || copy.action;
  return {
    label,
    cause,
    action,
    summary: cause,
    detail
  };
}

function environmentCheckRows(checks = [], isHardware = false) {
  const defaults = [
    [isHardware ? 'profile' : 'source', isHardware ? '真机配置' : '项目文件'],
    ['config', '用例配置'],
    ['artifact', '测试程序'],
    ['command', '操作能力'],
    ['capture', '截图能力'],
    ['llm', '模型服务']
  ];
  const normalized = checks.length ? checks : defaults.map(([key, label]) => ({key, label, status: 'unchecked', detail: '尚未执行环境检查'}));
  return `<div class='environment-check-list' aria-live='polite'>${normalized.map(item => {
    const status = String(item.status || 'unchecked').toLowerCase();
    const statusLabel = {pass: '可用', ready: '可用', checking: '检查中', warning: '需处理', fail: '不可用', error: '不可用', unchecked: '未检查'}[status] || '未检查';
    const statusIcon = status === 'checking'
      ? `<span class='environment-check-spinner' aria-hidden='true'></span>`
      : ['pass', 'ready'].includes(status)
        ? icon('check', 17)
        : ['fail', 'error'].includes(status)
          ? icon('close', 17)
          : status === 'warning'
            ? icon('warning', 17)
            : `<span class='environment-check-unchecked' aria-hidden='true'>—</span>`;
    const presentation = environmentCheckPresentation(item, status);
    const diagnostics = presentation.detail && presentation.detail !== presentation.summary
      ? `<details class='inline-diagnostics'><summary>诊断信息</summary><pre><code>${escapeHtml(presentation.detail)}</code></pre></details>`
      : '';
    const isProblem = ['warning', 'fail', 'error'].includes(status);
    const content = isProblem
      ? `<div class='environment-check-detail'><p class='check-cause'><span class='check-field-label'>问题原因：</span>${escapeHtml(presentation.cause)}</p>${presentation.action ? `<p class='check-action'><span class='check-field-label'>处理方法：</span>${escapeHtml(presentation.action)}</p>` : ''}${diagnostics}</div>`
      : `<div class='environment-check-detail'><p>${escapeHtml(presentation.summary)}</p>${diagnostics}</div>`;
    return `<div class='environment-check-row is-${escapeHtml(status)}' data-environment-check-key='${escapeHtml(item.key || '')}' data-environment-check-label='${escapeHtml(presentation.label)}'><i aria-label='${escapeHtml(statusLabel)}'>${statusIcon}</i><strong>${escapeHtml(presentation.label)}</strong><span>${escapeHtml(statusLabel)}</span>${content}</div>`;
  }).join('')}</div>`;
}

function setEnvironmentChecksLoading(root, isHardware = false) {
  const list = root.querySelector('.environment-check-list');
  if (!list) return;
  const checks = [...list.querySelectorAll('[data-environment-check-key]')].map(row => ({
    key: row.dataset.environmentCheckKey,
    label: row.dataset.environmentCheckLabel,
    status: 'checking'
  }));
  list.outerHTML = environmentCheckRows(checks, isHardware);
  const score = root.querySelector('.readiness-score strong');
  if (score) score.textContent = '检查中…';
}

function setEnvironmentChecksFailed(root, error, isHardware = false) {
  const list = root.querySelector('.environment-check-list');
  if (!list) return;
  const detail = String(error?.diagnosticMessage || error?.message || '环境检查请求失败');
  const checks = [...list.querySelectorAll('[data-environment-check-key]')].map(row => ({
    key: row.dataset.environmentCheckKey,
    label: row.dataset.environmentCheckLabel,
    status: 'error',
    code: error?.code || 'PREFLIGHT_INTERNAL_ERROR',
    detail
  }));
  list.outerHTML = environmentCheckRows(checks, isHardware);
  const score = root.querySelector('.readiness-score strong');
  if (score) score.textContent = `0 / ${checks.length}`;
}

function environmentLogRows(items = [], health = {label: '尚未检查'}) {
  return `<ol class='environment-log'>${items.map(item => {
    const detail = String(item.message || '').trim();
    const summary = /(?:失败|异常|\berror\b|\bfail(?:ed)?\b)/i.test(detail)
      ? `环境检查发现问题：${health.label}`
      : `环境检查完成：${health.label}`;
    return `<li><time>${formatTime(item.at || item.timestamp)}</time><div><span>${escapeHtml(summary)}</span>${detail ? `<details class='inline-diagnostics'><summary>诊断信息</summary><pre><code>${escapeHtml(detail)}</code></pre></details>` : ''}</div></li>`;
  }).join('')}</ol>`;
}

function mount579EnvironmentConfig(root, cfg = {}) {
  const form = root.querySelector('#environment-config-form');
  if (!form) return;
  form.innerHTML = `
    <div class='environment-safety-notice'><strong>579 无界面链路</strong><p>动作仅经 ADB → APP Bridge → BLE 下发；COM3 只用于 O1/O2 观察，网页不提供串口写入或原始命令入口。</p></div>
    <label><span>控制链</span><input type='text' value='ADB → APP Bridge → BLE' readonly></label>
    <label><span>串口观察</span><input type='text' value='${escapeHtml(cfg.serial_mode === 'read_only' ? `${cfg.com_port || 'COM3'}（严格只读）` : 'COM3（严格只读）')}' readonly></label>
    <label><span>截图方式</span><input type='text' value='O2 手表截图' readonly></label>
    <label><span>设备反馈</span><input type='text' value='APP Bridge 回执 + O1/O2 设备效果证据' readonly></label>
    <label><span>579 配置启用</span><select name='enabled'><option value='false' ${cfg.enabled ? '' : 'selected'}>关闭</option><option value='true' ${cfg.enabled ? 'selected' : ''}>启用配置</option></select></label>
    <label><span>ADB 路径</span><input name='adb_path' type='text' value='${escapeHtml(cfg.adb_path || '')}' placeholder='例如 adb 或 adb.exe 绝对路径'></label>
    <label><span>APP 包名</span><input name='app_package' type='text' value='${escapeHtml(cfg.app_package || '')}' placeholder='APP Bridge 包名'></label>
    <label><span>Bridge 组件</span><input name='bridge_component' type='text' value='${escapeHtml(cfg.bridge_component || '')}' placeholder='可选，完整组件名'></label>
    <label><span>Bridge Action</span><input name='bridge_action' type='text' value='${escapeHtml(cfg.bridge_action || '')}' placeholder='广播 Action'></label>
    <label><span>COM 端口</span><input name='com_port' type='text' value='${escapeHtml(cfg.com_port || 'COM3')}'></label>
    <label><span>只读波特率</span><input name='com_baudrate' type='number' min='1' value='${escapeHtml(cfg.com_baudrate || 1500000)}'></label>
    <label><span>证据目录</span><input name='artifact_root' type='text' value='${escapeHtml(cfg.artifact_root || '')}' placeholder='留空使用任务证据目录'></label>
    <label><span>实机动作门禁</span><input type='text' value='${cfg.device_actions_enabled ? '已由受控授权开启' : '默认关闭，等待受控 Canary 授权'}' readonly></label>
    <div class='environment-form-actions'><button class='button button-secondary' type='reset'>恢复当前值</button><button class='button' type='submit'>保存 579 配置</button></div>`;
}

function BluetoothPage() {
  let pollTimer = null;
  let destroyed = false;
  let eventCursor = 0;
  let events = [];
  let devices = [];
  let latestStatus = null;

  const calculatorData = '08 54 4F 50 35 53 54 45 50 00 1C 54 4F 50 35 53 54 45 50 3A 54 50 5F 43 4C 49 43 4B 3A 35 31 2C 31 35 36 2C 31 20 3B';
  const buttonData = '08 54 4F 50 35 53 54 45 50 00 1C 54 4F 50 35 53 54 45 50 3A 42 55 54 54 4F 4E 5F 50 52 45 53 53 3A 31 2C 31 2C 30 3B';

  return {
    async load() {
      const [config, status, remembered] = await Promise.all([
        api('/api/config'),
        optionalApi('/api/hardware/579/status'),
        optionalApi('/api/hardware/ble/remembered'),
      ]);
      latestStatus = status.data;
      return {config, status, remembered};
    },
    render(data) {
      const initialPurpose = currentProject() === '6202_W5230' ? '6202' : '579';
      const address579 = data.config.hardware_579?.ble_address || '';
      const address6202 = data.config.hardware?.ble_address || '';
      const remembered = data.remembered.data?.items || [];
      return `${Components.pageHeader({title: '蓝牙工作台', intro: '管理独立蓝牙目标、复用 579 GATT 连接并发送原始命令'})}
        <section class="bluetooth-workbench-grid" data-bluetooth-workbench data-purpose="${initialPurpose}" data-address-579="${escapeHtml(address579)}" data-address-6202="${escapeHtml(address6202)}">
          <article class="workspace-panel bluetooth-device-panel">
            <header><div><h2>设备与连接</h2><p>扫描结果只用于精确选择地址，不按名称猜测目标。</p></div><span id="bt-lease-chip" class="chip chip-pending">手工可用</span></header>
            <div class="bluetooth-form-grid">
              <label><span>用途</span><select id="bt-purpose" data-ble-mutable><option value="579" ${initialPurpose === '579' ? 'selected' : ''}>579 指令执行</option><option value="6202" ${initialPurpose === '6202' ? 'selected' : ''}>6202 BLE 截图</option></select></label>
              <label><span>扫描超时（秒）</span><input id="bt-timeout" type="number" min="1" max="60" value="${Number(data.config.hardware_579?.ble_scan_timeout || data.config.hardware?.ble_scan_timeout || 15)}" data-ble-mutable></label>
              <label class="bluetooth-address-field"><span>精确目标地址</span><input id="bt-address" type="text" value="${escapeHtml(initialPurpose === '579' ? address579 : address6202)}" placeholder="例如 41:42:72:6A:93:2D" autocomplete="off" data-ble-mutable></label>
            </div>
            <div class="bluetooth-actions">
              <input id="bt-search" type="search" placeholder="按名称或地址过滤" autocomplete="off" data-ble-mutable>
              <button id="bt-scan" class="button button-secondary" type="button" data-ble-mutable>扫描</button>
              <button id="bt-save-target" class="button button-secondary" type="button" data-ble-mutable>保存目标</button>
              <button id="bt-connect" class="button" type="button" data-ble-mutable>连接</button>
              <button id="bt-disconnect" class="button button-secondary" type="button" data-ble-mutable>断开</button>
            </div>
            <div id="bt-status" class="ble-connection-status" data-tone="neutral" role="status" aria-live="polite"><span class="ble-connection-dot" aria-hidden="true"></span><div><strong>正在读取连接状态</strong><small></small></div></div>
            <div id="bt-device-list" class="bluetooth-device-list">${remembered.length ? remembered.map(item => `<button type="button" class="bluetooth-device-choice" data-device-address="${escapeHtml(item.address)}" data-device-name="${escapeHtml(item.name || '')}" data-ble-mutable><strong>${escapeHtml(item.name || '未命名设备')}</strong><code>${escapeHtml(item.address)}</code><small>6202 已记住设备</small></button>`).join('') : '<div class="ble-device-empty">点击“扫描”查找附近设备</div>'}</div>
          </article>

          <article id="bt-command-panel" class="workspace-panel bluetooth-command-panel">
            <header><div><h2>579 原始命令</h2><p>Cmd / Key / Data 由后端规范化并封包。</p></div><span class="chip chip-warning">effect_verified=false</span></header>
            <div class="notice notice-warning bluetooth-risk"><p><strong>风险说明：</strong>此处不设命令白名单，也不对高风险 Cmd/Key 二次确认。仅发送你明确了解的命令；L1 ACK 只证明传输回执，不证明手表业务效果。</p></div>
            <div class="bluetooth-presets" aria-label="已验证预置">
              <button type="button" class="button button-secondary" data-bt-preset="find" data-ble-mutable>查找手表 02/3B</button>
              <button type="button" class="button button-secondary" data-bt-preset="calc" data-ble-mutable>计算器点击 0→1</button>
              <button type="button" class="button button-secondary" data-bt-preset="button" data-ble-mutable>侧键短按</button>
            </div>
            <div class="bluetooth-command-fields">
              <label><span>Cmd</span><input id="bt-cmd" value="02" placeholder="02 或 0x02" autocomplete="off" data-ble-mutable></label>
              <label><span>Key</span><input id="bt-key" value="3B" placeholder="3B 或 0x3b" autocomplete="off" data-ble-mutable></label>
              <label class="bluetooth-data-field"><span>Data（HEX，可留空）</span><textarea id="bt-data" rows="5" placeholder="连续 HEX 或空格/逗号分隔" data-ble-mutable></textarea></label>
            </div>
            <div class="bluetooth-actions"><button id="bt-preview" class="button button-secondary" type="button">生成预览</button><button id="bt-send" class="button" type="button" data-ble-mutable>发送并等待 L1 ACK</button></div>
            <dl id="bt-preview-result" class="bluetooth-packet-preview"><div><dt>规范化命令</dt><dd>尚未生成</dd></div><div><dt>完整 Packet</dt><dd>—</dd></div></dl>
          </article>
        </section>
        <article class="workspace-panel bluetooth-log-panel"><header><div><h2>579 TX / RX 日志</h2><p>cursor 增量轮询；包含 L1 ACK、设备主动上报与主机 ACK。</p></div><button id="bt-clear-log" class="button button-secondary" type="button">清空显示</button></header><ol id="bt-event-log" class="bluetooth-event-log"><li><span>等待 Broker 事件…</span></li></ol></article>`;
    },
    mount(root, data) {
      const workbench = root.querySelector('[data-bluetooth-workbench]');
      const purpose = root.querySelector('#bt-purpose');
      const address = root.querySelector('#bt-address');
      const timeout = root.querySelector('#bt-timeout');
      const search = root.querySelector('#bt-search');
      const list = root.querySelector('#bt-device-list');
      const statusBox = root.querySelector('#bt-status');
      const commandPanel = root.querySelector('#bt-command-panel');
      const eventLog = root.querySelector('#bt-event-log');
      const cmd = root.querySelector('#bt-cmd');
      const key = root.querySelector('#bt-key');
      const rawData = root.querySelector('#bt-data');
      const previewResult = root.querySelector('#bt-preview-result');
      let namesByAddress = new Map();
      devices = (data.remembered.data?.items || []).map(item => ({...item, rssi: null}));
      for (const item of devices) namesByAddress.set(String(item.address || '').toLowerCase(), item.name || '');

      const configuredAddress = selectedPurpose => selectedPurpose === '579'
        ? workbench.dataset.address579
        : workbench.dataset.address6202;
      const scanTimeout = () => {
        const value = Number(timeout.value);
        if (!Number.isFinite(value) || value < 1 || value > 60) throw new Error('扫描超时必须在 1 到 60 秒之间');
        return value;
      };
      const leaseActive = () => Boolean(latestStatus?.lease?.active);
      const setStatus = (tone, title, detail = '') => {
        statusBox.dataset.tone = tone;
        statusBox.querySelector('strong').textContent = title;
        statusBox.querySelector('small').textContent = detail;
      };
      const applyLease = () => {
        const active = leaseActive();
        const chip = root.querySelector('#bt-lease-chip');
        chip.className = `chip ${active ? 'chip-running' : 'chip-pending'}`;
        chip.textContent = active ? `自动化占用 · ${latestStatus.lease.owner || '任务'}` : '手工可用';
        root.querySelectorAll('[data-ble-mutable]').forEach(control => { control.disabled = active; });
      };
      const renderConnection = () => {
        const is579 = purpose.value === '579';
        commandPanel.hidden = !is579;
        root.querySelector('#bt-disconnect').hidden = !is579;
        if (is579) {
          if (latestStatus?.connected) setStatus('success', `${latestStatus.name || '579 手表'} · ${latestStatus.address}`, 'GATT ready；Notify 已订阅');
          else if (latestStatus?.last_error) setStatus('error', latestStatus.last_error.reason_code || '连接异常', latestStatus.last_error.message || '');
          else setStatus('neutral', '579 尚未连接', '连接后 Broker 会保持并复用这一条 GATT 会话');
        } else {
          setStatus('neutral', '6202 按需连接', '连接检查后会断开；运行截图时按已保存地址重新连接');
        }
        applyLease();
      };
      const renderDevices = () => {
        const query = String(search.value || '').trim().toLowerCase();
        const filtered = devices.filter(item => !query || String(item.address || '').toLowerCase().includes(query) || String(item.name || '').toLowerCase().includes(query));
        list.innerHTML = filtered.length ? filtered.map(item => `<button type="button" class="bluetooth-device-choice ${String(item.address || '').toLowerCase() === String(address.value || '').toLowerCase() ? 'is-selected' : ''}" data-device-address="${escapeHtml(item.address || '')}" data-device-name="${escapeHtml(item.name || '')}" data-ble-mutable ${leaseActive() ? 'disabled' : ''}><strong>${escapeHtml(item.name || '未命名设备')}</strong><code>${escapeHtml(item.address || '')}</code><small>${item.rssi === null || item.rssi === undefined ? 'RSSI 未知' : `${Number(item.rssi)} dBm`}</small></button>`).join('') : '<div class="ble-device-empty">没有匹配的扫描结果</div>';
      };
      const renderEvents = () => {
        eventLog.innerHTML = events.length ? events.map(item => `<li><time>${escapeHtml(String(item.at || ''))}</time><strong>${escapeHtml(item.kind || 'EVENT')}</strong><code>${escapeHtml(JSON.stringify(item))}</code></li>`).join('') : '<li><span>等待 Broker 事件…</span></li>';
        eventLog.scrollTop = eventLog.scrollHeight;
      };
      const saveTarget = async () => {
        const value = String(address.value || '').trim();
        if (!value) throw new Error('请先填写或选择精确目标地址');
        if (purpose.value === '579') {
          await api('/api/config', {method: 'POST', body: JSON.stringify({hardware_579: {ble_address: value, ble_scan_timeout: scanTimeout()}})});
          workbench.dataset.address579 = value;
        } else {
          await api('/api/config', {method: 'POST', body: JSON.stringify({hardware: {ble_address: value, ble_scan_timeout: scanTimeout()}})});
          workbench.dataset.address6202 = value;
        }
      };
      const preview = async () => {
        const result = await api('/api/hardware/579/preview', {method: 'POST', body: JSON.stringify({cmd: cmd.value, key: key.value, data: rawData.value})});
        cmd.value = result.cmd;
        key.value = result.key;
        rawData.value = result.data_hex;
        previewResult.innerHTML = `<div><dt>规范化命令</dt><dd><code>Cmd=${escapeHtml(result.cmd)} · Key=${escapeHtml(result.key)} · Data=${escapeHtml(result.data_hex || '空')} · ${Number(result.packet_length)} B</code></dd></div><div><dt>完整 Packet</dt><dd><code>${escapeHtml(result.packet_hex)}</code></dd></div>`;
        return result;
      };
      const poll = async () => {
        if (destroyed) return;
        try {
          const [status, eventBatch] = await Promise.all([
            api('/api/hardware/579/status'),
            api(`/api/hardware/579/events?after=${eventCursor}&limit=200`),
          ]);
          latestStatus = status;
          const incoming = eventBatch.items || [];
          if (incoming.length) {
            events = [...events, ...incoming].slice(-300);
            eventCursor = Number(eventBatch.next_cursor || eventCursor);
            renderEvents();
          }
          renderConnection();
          renderDevices();
        } catch (error) {
          if (purpose.value === '579') setStatus('error', 'Broker 状态读取失败', error.message);
        } finally {
          if (!destroyed) pollTimer = setTimeout(poll, 1000);
        }
      };

      purpose.addEventListener('change', () => {
        address.value = configuredAddress(purpose.value) || '';
        renderConnection();
        renderDevices();
      });
      address.addEventListener('input', renderDevices);
      search.addEventListener('input', renderDevices);
      list.addEventListener('click', event => {
        const choice = event.target.closest('[data-device-address]');
        if (!choice || leaseActive()) return;
        address.value = choice.dataset.deviceAddress || '';
        namesByAddress.set(address.value.toLowerCase(), choice.dataset.deviceName || '');
        renderDevices();
      });
      root.querySelector('#bt-scan').addEventListener('click', async event => {
        event.currentTarget.disabled = true;
        try {
          const result = await api(`/api/hardware/ble/devices?timeout=${encodeURIComponent(scanTimeout())}&q=${encodeURIComponent(search.value.trim())}`);
          devices = result.items || [];
          renderDevices();
          showToast(`找到 ${devices.length} 个蓝牙设备`);
        } catch (error) { showToast(error.message, 'error'); }
        finally { applyLease(); }
      });
      root.querySelector('#bt-save-target').addEventListener('click', async () => {
        try { await saveTarget(); showToast('蓝牙目标已保存'); } catch (error) { showToast(error.message, 'error'); }
      });
      root.querySelector('#bt-connect').addEventListener('click', async event => {
        event.currentTarget.disabled = true;
        try {
          const value = String(address.value || '').trim();
          if (!value) throw new Error('请先填写或选择精确目标地址');
          if (purpose.value === '579') {
            latestStatus = await api('/api/hardware/579/connect', {method: 'POST', body: JSON.stringify({address: value, timeout: scanTimeout()})});
            workbench.dataset.address579 = latestStatus.address || value;
          } else {
            await api('/api/hardware/ble/connect', {method: 'POST', body: JSON.stringify({address: value, name: namesByAddress.get(value.toLowerCase()) || '', timeout: scanTimeout()})});
            workbench.dataset.address6202 = value;
          }
          renderConnection();
          showToast('蓝牙目标连接成功');
        } catch (error) { showToast(error.message, error.status === 409 ? 'warning' : 'error'); }
        finally { applyLease(); }
      });
      root.querySelector('#bt-disconnect').addEventListener('click', async () => {
        try { latestStatus = await api('/api/hardware/579/disconnect', {method: 'POST', body: '{}'}); renderConnection(); showToast('579 BLE 已断开'); } catch (error) { showToast(error.message, error.status === 409 ? 'warning' : 'error'); }
      });
      root.querySelectorAll('[data-bt-preset]').forEach(button => button.addEventListener('click', () => {
        const preset = button.dataset.btPreset;
        cmd.value = preset === 'find' ? '02' : '04';
        key.value = preset === 'find' ? '3B' : '05';
        rawData.value = preset === 'find' ? '' : preset === 'calc' ? calculatorData : buttonData;
        preview().catch(error => showToast(error.message, 'error'));
      }));
      root.querySelector('#bt-preview').addEventListener('click', () => preview().catch(error => showToast(error.message, 'error')));
      root.querySelector('#bt-send').addEventListener('click', async event => {
        event.currentTarget.disabled = true;
        try {
          await preview();
          const result = await api('/api/hardware/579/send', {method: 'POST', body: JSON.stringify({cmd: cmd.value, key: key.value, data: rawData.value})});
          showToast(result.transport_acked ? '收到 L1 ACK；请人工确认手表业务现象' : '未收到 L1 ACK', result.transport_acked ? 'success' : 'warning');
        } catch (error) { showToast(error.message, error.status === 409 ? 'warning' : 'error'); }
        finally { applyLease(); }
      });
      root.querySelector('#bt-clear-log').addEventListener('click', () => { events = []; renderEvents(); });
      renderConnection();
      void poll();
    },
    destroy() {
      destroyed = true;
      if (pollTimer) clearTimeout(pollTimer);
    }
  };
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
      const platformId = currentPlatformFor(project);
      const targetId = currentTargetFor(project, platformId);
      const projectMeta = testProject(project);
      const profiles = (projectMeta.allowed_targets || []).map(id => targetProfile(project, PLATFORM_TARGETS[id]?.platform_id, id));
      const profile = targetProfile(project, platformId, targetId);
      const environmentItems = data.environments.data?.items || [];
      const environmentItem = environmentItems.find(item => item.id === project || item.project === project || item.target_id === profile.target_id) || null;
      const targetHealth = environmentHealth(environmentItem);
      const cfg = data.config.data || {};
      const protocol = ENVIRONMENT_PROTOCOLS[project];
      const simConfig = profile.simulator_config || {};
      const isHardware = profile.execution_target === 'hardware';
      const sourceRoot = project === '6202_W5230_SIMULATOR' ? (profile.simulator_source_root || simConfig.source_root) : cfg.simulator?.source_root;
      const workspaceRoot = project === '6202_W5230_SIMULATOR' ? (profile.simulator_source_root || simConfig.source_root) : cfg.simulator?.workspace_root;
      const runtimeProfileRoot = cfg.hardware?.profile_root || '';
      const runtimeProfileVersion = cfg.hardware?.profile_version || '';
      const buildDirectory = profile.simulator_build_directory || simConfig.build_directory || '';
      const artifactPath = profile.simulator_artifact_path || simConfig.artifact_path || cfg.simulator?.simulator_path || '';
      const checks = environmentItem?.checks || [];
      const readyChecks = checks.filter(item => ['pass', 'ready'].includes(String(item.status || '').toLowerCase())).length;
      const totalChecks = checks.length || 6;
      let content;
      if (data.section === 'llm') {
        content = `<article class='workspace-panel settings-summary-panel'><header><div><h2>模型服务</h2><p>当前连接配置</p></div><button class='button' type='button' data-open-settings>打开系统设置</button></header><dl class='settings-summary-grid'><div><dt>密钥</dt><dd>${cfg.llm?.api_key ? '已配置' : '未配置'}</dd></div><div><dt>服务地址</dt><dd>${escapeHtml(cfg.llm?.base_url || '未配置')}</dd></div><div><dt>模型</dt><dd>${escapeHtml(cfg.llm?.model || '未配置')}</dd></div><div><dt>超时</dt><dd>${Number(cfg.llm?.timeout || 0) || '—'} 秒</dd></div></dl></article>`;
      } else if (data.section === 'ones') {
        content = `<article class='workspace-panel settings-summary-panel'><header><div><h2>ONES 平台</h2><p>缺陷同步配置</p></div><button class='button' type='button' data-open-settings>打开系统设置</button></header><dl class='settings-summary-grid'><div><dt>平台地址</dt><dd>${escapeHtml(cfg.ones?.base_url || '未配置')}</dd></div><div><dt>登录状态</dt><dd>${cfg.ones?.auth_token ? '已配置' : '未配置'}</dd></div><div><dt>团队</dt><dd>${cfg.ones?.team_uuid ? '已配置' : '未配置'}</dd></div><div><dt>用户</dt><dd>${cfg.ones?.user_id ? '已配置' : '未配置'}</dd></div></dl></article>`;
      } else if (data.section === 'updates') {
        const update = data.update.data || {};
        content = `<article class='workspace-panel settings-summary-panel'><header><div><h2>系统更新</h2><p>检查并安装新版本</p></div>${update.update_available ? `<button class='button' type='button' data-open-update>查看新版本</button>` : ''}</header><dl class='settings-summary-grid'><div><dt>当前版本</dt><dd>${escapeHtml(update.current_version || document.querySelector('#brand-system-version')?.textContent || '—')}</dd></div><div><dt>最新版本</dt><dd>${escapeHtml(update.latest_version || '—')}</dd></div><div><dt>更新状态</dt><dd>${data.update.data ? (update.update_available ? '发现新版本' : '当前已是最新') : '暂时无法检查更新'}</dd></div></dl></article>`;
      } else {
        const canManage = Boolean(data.environments.data);
        content = `
          <section class='target-health-grid environment-page-targets'>${profiles.map(item => renderEnvironmentTargetCard(item, environmentItems.find(env => env.id === item.project || env.project === item.project || env.target_id === item.target_id), project)).join('')}</section>
          <section class='environment-main-grid'>
            <article class='workspace-panel environment-config-panel'>
              <header><div><h2>当前测试目标</h2><p>${escapeHtml(profile.project_label || project)} · ${escapeHtml(profile.execution_target_label || '')}</p></div>${Components.statusChip(targetHealth.status === 'ready' ? 'PASS' : targetHealth.status === 'error' ? 'ERROR' : 'PENDING', targetHealth.label)}</header>
              <form id='environment-config-form'>
                <label><span>连接方式</span><input type='text' value='${escapeHtml(protocol?.transportLabel || profile.transport_label || profile.transport || '未知')}' readonly></label>
                <label><span>截图方式</span><input type='text' value='${escapeHtml(protocol?.captureLabel || profile.capture_label || profile.capture_provider || '未知')}' readonly></label>
                <details class='environment-advanced'><summary>高级设置</summary><div class='environment-advanced-fields'>
                  ${project === '6202_W5230' ? `<label><span>SuperCom 管道</span><input type='text' value='${escapeHtml(profile.pipe_name || `\\\\.\\pipe\\SuperCom.AgentBridge.${cfg.hardware?.port || 'COM端口'}`)}' readonly></label>` : ''}
                  ${isHardware ? `<label><span>运行档案目录</span><input name='profile_root' type='text' value='${escapeHtml(runtimeProfileRoot)}' placeholder='例如 D:\\Agent-loop\\profiles' ${canManage ? '' : 'readonly'}></label><label><span>档案版本</span><input name='profile_version' type='text' value='${escapeHtml(runtimeProfileVersion)}' placeholder='留空自动选择' ${canManage ? '' : 'readonly'}></label>` : `<label><span>源码目录</span><input name='source_root' type='text' value='${escapeHtml(sourceRoot || '')}' placeholder='尚未配置' ${canManage ? '' : 'readonly'}></label><label><span>工作区目录</span><input name='workspace_root' type='text' value='${escapeHtml(workspaceRoot || '')}' placeholder='尚未配置' ${canManage ? '' : 'readonly'}></label>`}
                  ${profile.execution_target === 'simulator' ? `<label><span>构建目录</span><input name='build_directory' type='text' value='${escapeHtml(buildDirectory)}' placeholder='尚未配置' ${canManage ? '' : 'readonly'}></label><label><span>模拟器程序</span><input name='artifact_path' type='text' value='${escapeHtml(artifactPath)}' placeholder='尚未配置' ${canManage ? '' : 'readonly'}></label>` : ''}
                </div></details>
                <div class='environment-form-actions'><button class='button button-secondary' type='reset'>恢复当前值</button><button class='button' type='submit' ${canManage ? '' : 'disabled title="暂不支持修改环境配置"'}>保存配置</button></div>
              </form>
              ${!canManage ? Components.unavailableState('暂不支持修改环境配置', '当前以只读方式显示已有配置。') : ''}
            </article>
            <article class='workspace-panel environment-check-panel'>
              <header><div><h2>环境检查</h2><p>检查操作、截图和必要服务是否可用</p></div><span class='readiness-score'>可用项 <strong>${checks.length ? `${readyChecks} / ${totalChecks}` : '—'}</strong></span></header>
              ${environmentCheckRows(checks, isHardware)}
              <div class='environment-form-actions'><button class='button' type='button' data-environment-check ${canManage ? '' : 'disabled title="暂不支持环境检查"'}>${icon('refresh', 16)} 立即检查</button></div>
            </article>
          </section>
          <article class='workspace-panel environment-log-panel'><header><div><h2>检查记录</h2><p>最近的环境检查结果</p></div></header>${environmentItem?.logs?.length ? environmentLogRows(environmentItem.logs, targetHealth) : Components.emptyState('尚无检查记录', '执行环境检查后，详细过程会显示在这里。')}</article>`;
        content = `<div class='environment-platform-switch'><span>平台视图</span><div class='platform-choice-group'>${platformSwitchButtons(project, platformId, 'data-environment-platform')}</div></div>` + content;
      }
      const environmentTabHref = section => pageUrl('/environments', project, {platform_id: platformId, target_id: targetId, section});
      return `${Components.pageHeader({title: '环境中心', intro: '统一管理模拟器、真机、模型服务和外部平台连接'})}${Components.subTabs([{value: 'targets', label: '测试目标', href: environmentTabHref('targets')}, {value: 'llm', label: '大模型', href: environmentTabHref('llm')}, {value: 'ones', label: 'ONES', href: environmentTabHref('ones')}, {value: 'updates', label: '系统更新', href: environmentTabHref('updates')}], data.section)}${content}`;
    },
    mount(root, data) {
      rememberProject(project);
      const platformId = currentPlatformFor(project);
      const profile = targetProfile(project, platformId, currentTargetFor(project, platformId));
      const is579 = profile.platform_id === '579';
      const currentEnvironment = data.environments.data?.items?.find(item => item.id === project || item.project === project || item.target_id === profile.target_id);
      const health = environmentHealth(currentEnvironment);
      setGlobalTargetHealth(health.status, health.label);
      if (is579) {
        mount579EnvironmentConfig(root, data.config.data?.platform_579 || {});
      } else {
        root.querySelectorAll('#environment-config-form label').forEach(label => {
          const field = label.querySelector('span')?.textContent;
          const input = label.querySelector('input');
          if (field === '连接方式' && input) input.value = profile.transport_label || profile.transport || '未知';
          if (field === '截图方式' && input) input.value = profile.capture_label || profile.capture_provider || '未知';
        });
      }
      root.querySelectorAll('[data-environment-platform]').forEach(button => button.addEventListener('click', () => {
        const nextPlatform = button.dataset.environmentPlatform;
        const nextTarget = targetsFor(project, nextPlatform)[0]?.target_id || '';
        history.pushState({}, '', pageUrl('/environments', project, {platform_id: nextPlatform, target_id: nextTarget}));
        route();
      }));
      root.querySelectorAll('[data-subtab]').forEach(button => button.addEventListener('click', () => {
        history.pushState({}, '', pageUrl('/environments', project, {platform_id: platformId, target_id: profile.target_id, section: button.dataset.subtab}));
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
          if (is579) {
            values.enabled = values.enabled === 'true';
            values.com_baudrate = Number(values.com_baudrate || 1500000);
          }
          const environmentId = profile.target_id;
          const payload = is579 ? {platform_579: values} : {paths: values};
          await api(`/api/environments/${encodeURIComponent(environmentId)}`, {method: 'PUT', body: JSON.stringify(payload)});
          showToast('环境配置已保存', 'success');
          route();
        } catch (error) { button.disabled = false; showToast(error.message, 'error'); }
      });
      root.querySelector('[data-environment-check]')?.addEventListener('click', async event => {
        const button = event.currentTarget;
        const idleMarkup = button.innerHTML;
        const checkingStartedAt = performance.now();
        const minimumLoadingMs = 450;
        button.disabled = true;
        button.setAttribute('aria-busy', 'true');
        button.innerHTML = `${icon('refresh', 16)} 正在检查…`;
        setEnvironmentChecksLoading(root, profile.execution_target === 'hardware');
        const environmentId = profile.target_id;
        try {
          const response = await api(`/api/environments/${encodeURIComponent(environmentId)}/check`, {method: 'POST', body: '{}'});
          const remainingLoadingMs = minimumLoadingMs - (performance.now() - checkingStartedAt);
          if (remainingLoadingMs > 0) await new Promise(resolve => setTimeout(resolve, remainingLoadingMs));
          const refreshedHealth = environmentHealth(response?.result);
          await route();
          showToast(`环境检查完成：${refreshedHealth.label}`, refreshedHealth.status === 'ready' ? 'success' : 'warning');
        } catch (error) {
          const remainingLoadingMs = minimumLoadingMs - (performance.now() - checkingStartedAt);
          if (remainingLoadingMs > 0) await new Promise(resolve => setTimeout(resolve, remainingLoadingMs));
          setEnvironmentChecksFailed(root, error, profile.execution_target === 'hardware');
          button.disabled = false;
          button.removeAttribute('aria-busy');
          button.innerHTML = idleMarkup;
          showToast(error.message, 'error');
        }
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
    if (parts[0] === 'bluetooth' && parts.length === 1) {
      setActiveNav('bluetooth');
      document.title = '蓝牙工作台 · Agent-loop';
      return await mountPageController(BluetoothPage());
    }
    if (parts[0] === 'test' && parts[1] && parts[2] && parts.length === 3) return await renderTest(parts[1], parts[2]);
    if (parts[0] === 'test-batch' && parts[1] && parts.length === 2) return await renderTestBatch(parts[1]);
    if (parts[0] === 'test-history' && parts[1] && parts[2] && parts[3] && parts.length === 4) return await renderTestHistory(parts[1], parts[2], parts[3]);
    if (parts[0] === 'defect' && parts[1] && parts.length === 2) return await renderDefect(parts[1]);
    if (parts[0] === 'history' && parts[1] && parts[2] && parts.length === 3) return await renderHistory(parts[1], parts[2]);
    throw new Error('页面不存在');
  } catch (error) {
    renderError(error);
  } finally {
    restoreRememberedScroll();
  }
}

initSystemSettings();
initPrimaryNavigation();
initImagePreview();
window.addEventListener('popstate', route);
loadPlatformRegistries()
  .then(() => {
    initGlobalTargetSwitcher();
    return route();
  })
  .catch(renderError);


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
          updateBadge.textContent = `发现新版本 v${data.latest_version}`;
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
      if (upgradeOverlayStatus) upgradeOverlayStatus.textContent = '正在下载并安装更新…';

      try {
        const resp = await fetch('/api/system/upgrade', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({})
        });
        const data = await resp.json();
        if (!resp.ok) {
          throw new Error(productApiError(data?.error || '更新失败', resp.status, data?.reason_code));
        }
        if (upgradeOverlayStatus) {
          upgradeOverlayStatus.textContent = '正在重启，请稍候…';
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
              if (upgradeOverlayStatus) upgradeOverlayStatus.textContent = '更新完成，正在刷新…';
              setTimeout(() => {
                window.location.reload();
              }, 1200);
            }
          } catch (_) {
            if (attempts >= maxAttempts) {
              clearInterval(pollTimer);
              if (upgradeOverlayStatus) {
                upgradeOverlayStatus.innerHTML = '<span style="font-size: 14px; margin-bottom: 6px;">更新已完成，请进入新版本。</span><button onclick="window.location.reload()" class="button button-primary" style="background:#4f46e5;color:white;padding:10px 24px;cursor:pointer;border-radius:6px;font-size:14px;font-weight:600;display:inline-flex;align-items:center;justify-content:center;box-shadow:0 4px 12px rgba(79,70,229,0.3);margin:8px auto 0 auto;border:none;">进入工作台</button>';
              }
            }
          }
        }, 1500);

      } catch (err) {
        if (upgradeOverlay) upgradeOverlay.style.display = 'none';
        showToast(`更新失败：${err.message}`, 'error');
      }
    });
  }

  setTimeout(checkSystemUpdateSilently, 1000);
