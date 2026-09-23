/**
 * AstrBot 插件页面：RSS 订阅增强版配置页
 * 所有后端调用都通过 window.AstrBotPluginPage bridge 完成。
 */

const bridge = window.AstrBotPluginPage;

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

/** 显示/隐藏：同时设置 hidden 属性和内联 !important，避免被其它样式覆盖 */
function show(el) {
  if (!el) return;
  el.hidden = false;
  el.style.removeProperty('display');
}

function hide(el) {
  if (!el) return;
  el.hidden = true;
  el.style.setProperty('display', 'none', 'important');
}

let state = {
  config: {},
  providers: [],
  status: null,
  subs: [],
  endpoints: [],
};

/* ------------------------------------------------------------------ 小工具 */
function getPath(obj, path) {
  return path.split('.').reduce((acc, key) => (acc == null ? acc : acc[key]), obj);
}

function setPath(obj, path, value) {
  const keys = path.split('.');
  let cur = obj;
  keys.slice(0, -1).forEach((key) => {
    if (typeof cur[key] !== 'object' || cur[key] === null) cur[key] = {};
    cur = cur[key];
  });
  cur[keys[keys.length - 1]] = value;
}

function toast(message, kind = '') {
  const el = $('#toast');
  el.textContent = message;
  el.className = `toast show ${kind}`;
  clearTimeout(toast._timer);
  toast._timer = setTimeout(() => {
    el.className = 'toast';
  }, 2600);
}

function busy(button, on, text) {
  if (!button) return;
  if (on) {
    button.dataset.label = button.textContent;
    button.textContent = text || '处理中…';
    button.disabled = true;
  } else {
    if (button.dataset.label) button.textContent = button.dataset.label;
    button.disabled = false;
  }
}

/**
 * 受限 iframe 里不能用 window.confirm / alert：
 * AstrBot 的插件 Pages iframe sandbox 只有 allow-scripts / allow-forms / allow-downloads，
 * 没有 allow-modals，confirm() 会被浏览器静默忽略并返回 false（点了删除像没反应一样）。
 * 所以危险操作改成「按钮自己变成确认按钮，再点一次才真执行」。
 */
function armedConfirm(button, label = '再点一次删除', timeout = 4000) {
  if (!button) return true;
  if (button.dataset.armed === '1') return true;
  button.dataset.armed = '1';
  if (!button.dataset.originalLabel) button.dataset.originalLabel = button.textContent;
  button.textContent = label;
  button.classList.add('armed');
  clearTimeout(button._armTimer);
  button._armTimer = setTimeout(() => disarm(button), timeout);
  return false;
}

function disarm(button) {
  if (!button) return;
  clearTimeout(button._armTimer);
  if (button.dataset.originalLabel) button.textContent = button.dataset.originalLabel;
  button.dataset.armed = '';
  button.classList.remove('armed');
}

function escapeHtml(str) {
  return String(str ?? '').replace(/[&<>"']/g, (ch) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[ch]));
}

/* ------------------------------------------------------------------ 表单 */
function fillForms() {
  $$('[data-path]').forEach((el) => {
    const path = el.dataset.path;
    const type = el.dataset.type || 'string';
    const value = getPath(state.config, path);
    if (value === undefined || value === null) return;
    if (type === 'bool') {
      el.checked = Boolean(value);
    } else if (type === 'list') {
      el.value = Array.isArray(value) ? value.join('\n') : String(value);
    } else {
      el.value = value;
    }
  });
}

function collectForms() {
  const payload = {};
  $$('[data-path]').forEach((el) => {
    const path = el.dataset.path;
    const type = el.dataset.type || 'string';
    let value;
    if (type === 'bool') {
      value = el.checked;
    } else if (type === 'list') {
      value = el.value
        .split('\n')
        .map((s) => s.trim())
        .filter(Boolean);
    } else if (type === 'int') {
      const n = parseInt(el.value, 10);
      if (Number.isNaN(n)) return;
      value = n;
    } else if (type === 'float') {
      const n = parseFloat(el.value);
      if (Number.isNaN(n)) return;
      value = n;
    } else if (type === 'text') {
      // 多行模板：保留换行，不做 trim（首尾空白由后端处理）
      value = el.value.replace(/\r\n/g, '\n');
    } else {
      value = el.value.trim();
    }
    setPath(payload, path, value);
  });
  return payload;
}

/* ------------------------------------------------------------------ 渲染 */
function renderProviders() {
  const select = $('#providerSelect');
  const current = state.config?.translate?.provider_id || '';
  const options = ['<option value="">跟随会话 / 默认模型</option>'];
  state.providers.forEach((p) => {
    const label = `${p.model || p.id}${p.type ? ` · ${p.type}` : ''}`;
    options.push(
      `<option value="${escapeHtml(p.id)}">${escapeHtml(label)}</option>`,
    );
  });
  select.innerHTML = options.join('');
  select.value = current;
}

function renderChips() {
  const st = state.status;
  if (!st) return;
  const tr = st.translator || {};
  const providerText = st.provider?.configured_provider_id
    || st.provider?.session_provider_id
    || st.provider?.default_provider?.id
    || '未配置';
  const chips = [
    `<span class="chip ${st.scheduler_running ? 'ok' : 'bad'}">调度器 <b>${st.scheduler_running ? '运行中' : '已停止'}</b></span>`,
    `<span class="chip">频道 <b>${st.channel_count}</b></span>`,
    `<span class="chip">订阅 <b>${st.subscriber_count}</b></span>`,
    `<span class="chip ${tr.enabled ? 'ok' : 'bad'}">翻译 <b>${tr.enabled ? '开启' : '关闭'}</b></span>`,
    `<span class="chip">模型 <b>${escapeHtml(providerText)}</b></span>`,
  ];
  $('#statusChips').innerHTML = chips.join('');
}

function renderSubs() {
  const tbody = $('#subsTable tbody');
  if (!state.subs.length) {
    tbody.innerHTML =
      '<tr><td colspan="5" class="empty">还没有任何订阅，先在聊天里用 /rss add-url 添加吧。</td></tr>';
    return;
  }
  const rows = [];
  state.subs.forEach((item) => {
    const details =
      item.subscriber_details && item.subscriber_details.length
        ? item.subscriber_details
        : (item.subscribers || ['']).map((user) => ({
            user,
            cron_expr: item.cron_expr,
            max_pic_item: null,
          }));

    details.forEach((detail) => {
      const user = detail.user || '';
      const pic =
        detail.max_pic_item === null || detail.max_pic_item === undefined
          ? ''
          : String(detail.max_pic_item);
      rows.push(`
        <tr>
          <td>
            <div><b>${escapeHtml(item.title)}</b></div>
            <div class="url">${escapeHtml(item.url)}</div>
          </td>
          <td><span class="muted">${escapeHtml(user || '—')}</span></td>
          <td>
            <input type="text" class="cron-input" value="${escapeHtml(detail.cron_expr || item.cron_expr)}"
              data-url="${escapeHtml(item.url)}" data-user="${escapeHtml(user)}" />
          </td>
          <td>
            <input type="text" class="pic-input" inputmode="numeric" placeholder="跟随全局"
              value="${escapeHtml(pic)}" title="留空=跟随全局，0=不发送，-1=不限制，n=最多 n 张"
              data-url="${escapeHtml(item.url)}" data-user="${escapeHtml(user)}" />
          </td>
          <td class="right">
            <div class="actions">
              <button class="btn mini" data-action="save-row" data-url="${escapeHtml(item.url)}" data-user="${escapeHtml(user)}">保存</button>
              <button class="btn mini danger" data-action="delete" data-url="${escapeHtml(item.url)}" data-user="${escapeHtml(user)}">删除</button>
            </div>
          </td>
        </tr>`);
    });
  });
  tbody.innerHTML = rows.join('');
}

function renderUmoOptions() {
  const list = $('#umoList');
  const users = new Set();
  state.subs.forEach((item) => {
    (item.subscribers || []).forEach((u) => u && users.add(u));
    (item.subscriber_details || []).forEach((d) => d.user && users.add(d.user));
  });
  list.innerHTML = Array.from(users)
    .sort()
    .map((u) => `<option value="${escapeHtml(u)}"></option>`)
    .join('');
}

function renderAddEndpoints() {
  const select = $('#addEndpoint');
  if (!state.endpoints.length) {
    select.innerHTML = '<option value="">（还没有 RSSHub 端点）</option>';
    return;
  }
  select.innerHTML = state.endpoints
    .map((url, idx) => `<option value="${idx}">${escapeHtml(url)}</option>`)
    .join('');
}

function renderEndpoints() {
  const list = $('#endpointList');
  if (!state.endpoints.length) {
    list.innerHTML = '<li class="empty">暂无 RSSHub 端点</li>';
    return;
  }
  renderAddEndpoints();
  list.innerHTML = state.endpoints
    .map(
      (url, idx) => `
      <li>
        <span>${idx}. ${escapeHtml(url)}</span>
        <button class="btn mini danger" data-endpoint-index="${idx}">删除</button>
      </li>`,
    )
    .join('');
}

function renderStatus() {
  const st = state.status;
  if (!st) return;
  const tr = st.translator || {};
  const provider = st.provider || {};
  const defaultProvider = provider.default_provider
    ? `${provider.default_provider.model || provider.default_provider.id}`
    : '未获取到';

  $('#statusKv').innerHTML = [
    ['插件版本', escapeHtml(st.version)],
    ['调度器', st.scheduler_running ? '运行中' : '已停止'],
    ['频道数量', st.channel_count],
    ['订阅数量', st.subscriber_count],
    ['RSSHub 端点', st.endpoint_count],
    ['数据文件', `<span class="muted">${escapeHtml(st.data_file)}</span>`],
    ['翻译开关', tr.enabled ? '开启' : '关闭'],
    ['目标语言', escapeHtml(tr.target_lang)],
    ['翻译域名过滤', tr.sources && tr.sources.length ? escapeHtml(tr.sources.join(', ')) : '全部翻译'],
    ['翻译缓存条数', tr.cache_entries],
    ['指定的模型', escapeHtml(provider.configured_provider_id || '（跟随会话 / 默认）')],
    ['当前会话模型', escapeHtml(provider.session_provider_id || '—')],
    ['AstrBot 默认模型', escapeHtml(defaultProvider)],
  ]
    .map(([k, v]) => `<div><span>${k}</span><span>${v}</span></div>`)
    .join('');

  const jobs = st.jobs || [];
  $('#jobsTable tbody').innerHTML = jobs.length
    ? jobs
        .map(
          (job) =>
            `<tr><td class="muted">${escapeHtml(job.id)}</td><td>${escapeHtml(job.next_run_time || '—')}</td></tr>`,
        )
        .join('')
    : '<tr><td colspan="2" class="empty">当前没有定时任务</td></tr>';

  $('#aboutKv').innerHTML = [
    ['插件名', 'astrbot_plugin_rss_plus'],
    ['版本', st.version || '—'],
    ['翻译实现', '复用 AstrBot 已配置的模型，无需第三方 API Key'],
    ['配置存储', '插件页面（与 _conf_schema.json 共用同一份配置）'],
    ['常用指令', '/rss · /rss rsshub · /rss pic · /rss translate'],
    ['图片诊断', '/rss pic-test <图片链接>'],
    ['调试屏蔽词', '/rss get <订阅序号>（手动获取不受屏蔽词限制，会标注命中的词）'],
  ]
    .map(([k, v]) => `<div><span>${k}</span><span>${escapeHtml(v)}</span></div>`)
    .join('');
}

/* ------------------------------------------------------------------ 数据加载 */
async function loadConfig() {
  const data = await bridge.apiGet('page/config');
  state.config = data.config || {};
  state.providers = data.providers || [];
  renderProviders();
  fillForms();
}

async function loadStatus() {
  state.status = await bridge.apiGet('page/status');
  renderChips();
  renderStatus();
}

async function loadSubs() {
  const data = await bridge.apiGet('page/subscriptions');
  state.subs = data.items || [];
  renderSubs();
}

async function loadEndpoints() {
  const data = await bridge.apiGet('page/rsshub');
  state.endpoints = data.endpoints || [];
  renderEndpoints();
}

/* ------------------------------------------------------------------ 交互 */
function bindTabs() {
  $$('.tab').forEach((tab) => {
    tab.addEventListener('click', () => {
      $$('.tab').forEach((t) => t.classList.toggle('active', t === tab));
      $$('.panel').forEach((p) =>
        p.classList.toggle('active', p.dataset.panel === tab.dataset.tab),
      );
    });
  });
}

/* ---- 配置页：保存 / 重置 / 翻译测试 / 图片诊断 / 样式预览 ---- */

// 保存配置并让插件立刻生效
async function onSaveConfig(e) {
  const btn = e.currentTarget;
  busy(btn, true, '保存中…');
  try {
    const payload = collectForms();
    const res = await bridge.apiPost('page/config', payload);
    state.config = res.config || state.config;
    renderProviders();
    fillForms();
    await loadStatus();
    toast('配置已保存并生效', 'ok');
  } catch (err) {
    toast(`保存失败：${err.message}`, 'error');
  } finally {
    busy(btn, false);
  }
}

// 放弃表单里的改动，重新拉一次已保存的配置
async function onResetConfig() {
  await loadConfig();
  toast('已恢复到上次保存的配置');
}

// 在线测试翻译（显示耗时与所用模型）
async function onTestTranslate(e) {
  const btn = e.currentTarget;
  const text = $('#testText').value.trim();
  const result = $('#testResult');
  if (!text) {
    toast('请先输入要翻译的文本', 'error');
    return;
  }
  busy(btn, true, '翻译中…');
  hide(result);
  try {
    const data = await bridge.apiPost('page/translate/test', { text });
    show(result);
    result.textContent = data.result || '（空结果）';
    const provider = data.provider?.configured_provider_id
      || data.provider?.session_provider_id
      || data.provider?.default_provider?.model
      || '默认模型';
    $('#testMeta').textContent = data.translated
      ? `耗时 ${data.elapsed_ms} ms · 模型：${provider}`
      : `耗时 ${data.elapsed_ms} ms · 判断为无需翻译（已是目标语言）`;
  } catch (err) {
    toast(`翻译失败：${err.message}`, 'error');
    $('#testMeta').textContent = '';
  } finally {
    busy(btn, false);
  }
}

// 单张图片读取诊断
async function onTestPicture(e) {
  const btn = e.currentTarget;
  const url = $('#picTestInput').value.trim();
  const box = $('#picTestResult');
  if (!url) {
    toast('请输入图片链接', 'error');
    return;
  }
  busy(btn, true, '诊断中…');
  try {
    const data = await bridge.apiPost('page/pic/test', { url });
    const r = data.result || {};
    show(box);
    box.textContent = [
      `地址：${r.url}`,
      `结果：${r.ok ? '✅ 成功' : '❌ 失败'} - ${r.reason}`,
      `HTTP：${r.status ?? '—'}    类型：${r.content_type || '无'}    大小：${r.size} 字节`,
      r.detected_type ? `实际格式：${r.detected_type}` : '',
      r.image ? `图像信息：${r.image.format} ${r.image.size} ${r.image.mode}` : '',
      r.image_error ? `PIL 解析失败：${r.image_error}` : '',
    ]
      .filter(Boolean)
      .join('\n');
  } catch (err) {
    hide(box);
    toast(`诊断失败：${err.message}`, 'error');
  } finally {
    busy(btn, false);
  }
}

// 不保存直接预览推送样式
async function onPreviewStyle(e) {
  const btn = e.currentTarget;
  const box = $('#stylePreviewResult');
  const payload = collectForms();
  payload.sample = {
    title: $('#styleSampleTitle').value,
    link: $('#styleSampleLink').value,
    video: $('#styleSampleVideo').value,
    content: $('#styleSampleContent').value,
  };
  busy(btn, true, '渲染中…');
  try {
    const data = await bridge.apiPost('page/style/preview', payload);
    show(box);
    box.textContent = data.text || '（空结果）';
    const tpl = data.used_template === 'template_hide_url' ? '隐藏链接模板' : '显示链接模板';
    const coverHint = data.cover_placeholder
      ? '封面按 {video_cover} 的位置插入'
      : '模板里没有 {video_cover}，封面会作为第一张图跟在文字后面';
    $('#stylePreviewMeta').textContent =
      `${tpl} · 显示标题：${data.show_title ? '是' : '否'} · ${data.line_count} 行 / ${data.char_count} 字 · ${coverHint}`;
  } catch (err) {
    hide(box);
    toast(`预览失败：${err.message}`, 'error');
    $('#stylePreviewMeta').textContent = '';
  } finally {
    busy(btn, false);
  }
}

function bindConfigActions() {
  $('#btnSave').addEventListener('click', onSaveConfig);
  $('#btnReset').addEventListener('click', onResetConfig);
  $('#btnTest').addEventListener('click', onTestTranslate);
  $('#btnPicTest').addEventListener('click', onTestPicture);
  $('#btnStylePreview').addEventListener('click', onPreviewStyle);
}

/* ---- 新增订阅：模式切换 / 添加 / 刷新列表 ---- */

// 订阅源模式切换：直链 / RSSHub 路由
function applyAddMode() {
  const mode = $('#addMode').value;
  const rsshub = mode === 'rsshub';
  $('#addUrl').hidden = rsshub;
  $('#addEndpoint').hidden = !rsshub;
  $('#addRoute').hidden = !rsshub;
}

// 收集「新增订阅」表单并校验；校验不通过时提示用户并返回 null
function collectAddSubscriptionPayload() {
  const mode = $('#addMode').value;
  const payload = {
    user: $('#addUser').value.trim(),
    cron_expr: $('#addCron').value.trim(),
    max_pic_item: $('#addPic').value.trim(),
    force: $('#addForce').checked,
  };
  if (mode === 'rsshub') {
    payload.endpoint_index = $('#addEndpoint').value;
    payload.route = $('#addRoute').value.trim();
    if (!state.endpoints.length) {
      toast('请先到 RSSHub 页签添加一个端点', 'error');
      return null;
    }
    if (!payload.route) {
      toast('请填写 RSSHub 路由，例如 /weibo/user/1234567890', 'error');
      return null;
    }
  } else {
    payload.url = $('#addUrl').value.trim();
    if (!payload.url) {
      toast('请填写 Feed 直链', 'error');
      return null;
    }
  }
  if (!payload.user) {
    toast('请填写推送会话（umo）', 'error');
    return null;
  }
  if (!payload.cron_expr) {
    toast('请填写推送频率（cron）', 'error');
    return null;
  }
  return payload;
}

async function onAddSubscription(e) {
  const btn = e.currentTarget;
  const payload = collectAddSubscriptionPayload();
  if (!payload) return;
  busy(btn, true, '添加中…');
  $('#addHint').textContent = '正在抓取订阅源…';
  try {
    const res = await bridge.apiPost('page/subscriptions/add', payload);
    toast(res.message || '添加成功', 'ok');
    $('#addHint').textContent = '';
    $('#addUrl').value = '';
    $('#addRoute').value = '';
    await Promise.all([loadSubs(), loadStatus()]);
  } catch (err) {
    $('#addHint').textContent = '';
    toast(`添加失败：${err.message}`, 'error');
  } finally {
    busy(btn, false);
  }
}

async function onReloadSubs() {
  try {
    await loadSubs();
    toast('订阅列表已刷新', 'ok');
  } catch (err) {
    toast(`刷新失败：${err.message}`, 'error');
  }
}

function bindAddSubscriptionActions() {
  $('#addMode').addEventListener('change', applyAddMode);
  applyAddMode();
  $('#btnAddSub').addEventListener('click', onAddSubscription);
  $('#btnReloadSubs').addEventListener('click', onReloadSubs);
}

/* ---- 维护操作：重载插件 / RSSHub 端点增删 ---- */

async function onReloadPlugin(e) {
  const btn = e.currentTarget;
  busy(btn, true, '重载中…');
  try {
    const res = await bridge.apiPost('page/actions/reload', {});
    await Promise.all([loadStatus(), loadSubs()]);
    toast(res.message || '已重载', 'ok');
  } catch (err) {
    toast(`重载失败：${err.message}`, 'error');
  } finally {
    busy(btn, false);
  }
}

async function onAddEndpoint(e) {
  const btn = e.currentTarget;
  const input = $('#endpointInput');
  const url = input.value.trim();
  if (!url) {
    toast('请输入 RSSHub 地址', 'error');
    return;
  }
  busy(btn, true, '添加中…');
  try {
    const res = await bridge.apiPost('page/rsshub/add', { url });
    state.endpoints = res.endpoints || [];
    renderEndpoints();
    input.value = '';
    await loadStatus();
    toast('添加成功', 'ok');
  } catch (err) {
    toast(`添加失败：${err.message}`, 'error');
  } finally {
    busy(btn, false);
  }
}

async function onEndpointListClick(e) {
  const btn = e.target.closest('[data-endpoint-index]');
  if (!btn) return;
  const index = Number(btn.dataset.endpointIndex);
  if (!armedConfirm(btn, `再点一次删除 ${index} 号端点`)) return;
  busy(btn, true, '…');
  try {
    const res = await bridge.apiPost('page/rsshub/remove', { index });
    state.endpoints = res.endpoints || [];
    renderEndpoints();
    await Promise.all([loadStatus(), loadSubs()]);
    toast('已删除', 'ok');
  } catch (err) {
    toast(`删除失败：${err.message}`, 'error');
    busy(btn, false);
    disarm(btn);
  }
}

function bindMaintenanceActions() {
  $('#btnReload').addEventListener('click', onReloadPlugin);
  $('#btnAddEndpoint').addEventListener('click', onAddEndpoint);
  $('#endpointList').addEventListener('click', onEndpointListClick);
}

/* ---- 订阅列表：删除单条 / 保存单行设置 ---- */

async function handleDeleteSubscription(btn) {
  // 受限 iframe 里 confirm() 无效，用按钮二次确认代替
  if (!armedConfirm(btn, '再点一次删除该订阅')) return;
  const { url, user } = btn.dataset;
  busy(btn, true, '…');
  try {
    const res = await bridge.apiPost('page/subscriptions/delete', { url, user });
    if (res && res.ok === false) {
      throw new Error(res.error || res.message || '后端未删除');
    }
    await Promise.all([loadSubs(), loadStatus()]);
    toast('已删除订阅', 'ok');
  } catch (err) {
    toast(`删除失败：${err.message}`, 'error');
    busy(btn, false);
    disarm(btn);
  }
}

async function handleSaveSubscriptionRow(btn) {
  const { url, user } = btn.dataset;
  const row = btn.closest('tr');
  const cron = row.querySelector('.cron-input').value.trim();
  const pic = row.querySelector('.pic-input').value.trim();
  busy(btn, true, '…');
  try {
    await bridge.apiPost('page/subscriptions/update', {
      url,
      user,
      cron_expr: cron,
      max_pic_item: pic, // 空字符串 = 清除独立设置，跟随全局
    });
    await Promise.all([loadSubs(), loadStatus()]);
    toast('已保存该订阅的设置', 'ok');
  } catch (err) {
    toast(`保存失败：${err.message}`, 'error');
  } finally {
    busy(btn, false);
  }
}

async function onSubsTableClick(e) {
  const btn = e.target.closest('[data-action]');
  if (!btn) return;
  if (btn.dataset.action === 'delete') {
    await handleDeleteSubscription(btn);
    return;
  }
  if (btn.dataset.action === 'save-row') {
    await handleSaveSubscriptionRow(btn);
  }
}

function bindSubscriptionTableActions() {
  $('#subsTable').addEventListener('click', onSubsTableClick);
}

// 事件绑定入口：各页签的处理器分组注册
function bindActions() {
  bindConfigActions();
  bindAddSubscriptionActions();
  bindMaintenanceActions();
  bindSubscriptionTableActions();
}

/* ------------------------------------------------------------------ 启动 */
async function main() {
  if (!bridge) {
    $('#loading').innerHTML =
      '<p>未检测到 AstrBot 插件页面 bridge，请从 AstrBot WebUI 的插件详情页打开此页面。</p>';
    return;
  }
  try {
    const context = await bridge.ready();
    // 标题走插件 i18n（.astrbot-plugin/i18n/<locale>.json 的 pages.settings.title）
    const pageTitle = bridge.t('pages.settings.title', 'RSS 订阅增强版');
    document.title = `${pageTitle} · 配置`;
    const h1 = document.querySelector('.hdr h1');
    if (h1) h1.textContent = pageTitle;
    const sub = document.querySelector('.hdr .sub');
    if (sub) {
      sub.textContent = bridge.t(
        'pages.settings.subtitle',
        sub.textContent,
      );
    }
  } catch (err) {
    $('#loading').innerHTML = `<p>连接失败：${escapeHtml(err.message)}</p>`;
    return;
  }

  bindTabs();
  bindActions();

  try {
    await Promise.all([loadConfig(), loadStatus(), loadSubs(), loadEndpoints()]);
  } catch (err) {
    toast(`加载失败：${err.message}`, 'error');
  }

  hide($('#loading'));
  show($('#app'));
}

main();
