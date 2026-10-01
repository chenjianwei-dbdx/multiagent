/* OMAS 控制台（ADR 0002 修正案）— 聊天式任务控制台，vanilla JS，无构建。
 *
 * 边界（与后端一致）：
 * - 输入框只喂 SubmitTask.intent，永不成为正文来源（I2）；
 * - 页面只消费 /api/* 的程序生成状态：task_id / span_handle / sha256
 *   一律来自服务器，前端只做缩短展示，不生成、不校验、不臆测；
 * - 事件轮询 1.2s 一次，view.status ∈ {created, running} 持续，
 *   其余状态（awaiting_user / completed / failed / cancelled / parked）停止。 */
(() => {
  'use strict';

  // ------------------------------------------------------------------ 常量

  const POLL_MS = 1200;
  const LIVE_STATUSES = new Set(['created', 'running']);

  const STATUS_LABELS = {
    created: '已创建',
    running: '执行中',
    awaiting_user: '等待输入',
    parked: '驻留',
    completed: '已完成',
    failed: '失败',
    cancelled: '已取消',
  };

  const TOOL_LABELS = {
    list_materials: '列出材料',
    search_materials: '检索材料',
    read_material: '读取材料',
    resolve_span: '解析 span',
  };

  const EVENT_ICONS = {
    task_submitted: '▸',
    inventory: '☰',
    tool_call: '⚙',
    plan: '✎',
    assembled: '◈',
    binding_committed: '✓',
    awaiting_user: '⚠',
    decision_provide_material: '↥',
    decision_omit_slot: '⊘',
    exported: '↓',
    execution_failed: '✕',
    task_cancelled: '—',
    task_recovered: '⟳',
  };

  // ------------------------------------------------------------------ 工具

  const $ = (sel, root) => (root || document).querySelector(sel);

  function el(tag, cls, text) {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function clear(node) {
    while (node.firstChild) node.removeChild(node.firstChild);
  }

  function shortId(value, n) {
    const len = n || 12;
    const s = String(value || '');
    return s.length <= len ? s : s.slice(0, len);
  }

  function fmtTime(iso) {
    if (!iso) return '';
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return '';
    const pad = (x) => String(x).padStart(2, '0');
    return (
      pad(d.getMonth() + 1) + '-' + pad(d.getDate()) + ' ' +
      pad(d.getHours()) + ':' + pad(d.getMinutes())
    );
  }

  class ApiError extends Error {
    constructor(code, message) {
      super(message);
      this.code = code;
    }
  }

  async function apiFetch(url, options) {
    let resp;
    try {
      resp = await fetch(url, options || {});
    } catch (err) {
      throw new ApiError('NETWORK', '无法连接控制台服务：' + err.message);
    }
    const text = await resp.text();
    let payload = null;
    if (text) {
      try { payload = JSON.parse(text); } catch (err) { payload = null; }
    }
    if (!resp.ok) {
      const info = payload && payload.error ? payload.error : null;
      throw new ApiError(
        info ? info.code : 'HTTP_' + resp.status,
        info ? info.message : 'HTTP ' + resp.status
      );
    }
    return payload;
  }

  function toast(message, kind, code) {
    const region = $('#toast-region');
    const item = el('div', 'toast ' + (kind || 'error'));
    if (code) item.append(el('span', 'toast-code', code));
    item.append(el('span', 'toast-msg', message));
    const closeBtn = el('button', 'toast-close', '✕');
    closeBtn.type = 'button';
    const dismiss = () => item.remove();
    closeBtn.addEventListener('click', dismiss);
    item.append(closeBtn);
    region.append(item);
    window.setTimeout(dismiss, kind === 'error' ? 9000 : 5000);
  }

  function showError(err) {
    if (err instanceof ApiError) toast(err.message, 'error', err.code);
    else toast(String((err && err.message) || err), 'error');
  }

  // ------------------------------------------------------------------ 状态

  const state = {
    conversations: [],
    templates: [],
    current: null, // 当前会话 {conversation_id, title, template_version_id, data_policy}
    renderedMsgIds: new Set(), // 已渲染消息 id（防轮询 diff 重复渲染）
    pendingUserText: null, // 本地已渲染、等服务端确认的用户消息文本
    turnTimer: null, // 对话轮次轮询句柄
    turnConvId: null, // 正在轮询的会话 id
  };

  const taskCards = new Set();
  let pendingFiles = [];

  // ------------------------------------------------------------------ 任务卡片

  class TaskCard {
    constructor(taskId) {
      this.taskId = String(taskId);
      this.lastSeq = 0;
      this.events = [];
      this.view = null;
      this.missing = []; // 最近一次已知的缺槽列表（来自 view.awaiting）
      this.awaitHandledFor = null; // 已应答过的 awaiting_event_id（防重复展示）
      this.timer = null;
      this.stopped = false;
      this.node = this.build();
    }

    build() {
      const card = el('article', 'task-card');
      card.dataset.taskId = this.taskId;
      const head = el('div', 'task-head');
      head.append(
        el('span', 'task-glyph', '⚙'),
        el('span', 'task-title', '装配任务'),
        el('code', 'task-id mono', shortId(this.taskId, 18))
      );
      this.chip = el('span', 'task-chip', '…');
      head.append(this.chip);
      this.timeline = el('ol', 'timeline');
      this.awaitPanel = el('div', 'task-await');
      this.awaitPanel.hidden = true;
      const foot = el('div', 'task-foot');
      this.stageEl = el('span', 'task-stage', '准备中');
      this.dotsEl = el('span', 'dots');
      for (let i = 0; i < 3; i++) this.dotsEl.append(el('span'));
      this.dotsEl.hidden = true;
      this.footExtra = el('div', 'task-foot-extra');
      foot.append(this.stageEl, this.dotsEl, this.footExtra);
      card.append(head, this.timeline, this.awaitPanel, foot);
      return card;
    }

    async refresh() {
      const data = await apiFetch(
        '/api/tasks/' + encodeURIComponent(this.taskId) + '/feed?after=' + this.lastSeq
      );
      for (const ev of data.events) this.events.push(ev);
      this.lastSeq = data.last_seq;
      this.view = data.view;
      if (this.view && this.view.awaiting) {
        this.missing = (this.view.awaiting.missing_slot_ids || []).slice();
      }
      this.render();
      return this.view ? this.view.status : null;
    }

    render() {
      const view = this.view;
      const status = view ? view.status : 'created';
      this.chip.textContent = STATUS_LABELS[status] || status;
      this.chip.className = 'task-chip st-' + status;

      clear(this.timeline);
      for (const ev of this.events) this.timeline.append(renderEventRow(ev, this));

      const awaiting = view && view.awaiting && status === 'awaiting_user' ? view.awaiting : null;
      if (awaiting && awaiting.awaiting_event_id !== this.awaitHandledFor) {
        renderAwaitForm(this);
        this.awaitPanel.hidden = false;
      } else {
        this.awaitPanel.hidden = true;
      }

      const stage = inferStage(this);
      this.stageEl.textContent = stage.label;
      this.stageEl.className = 'task-stage ' + stage.tone;
      this.dotsEl.hidden = status !== 'running';

      clear(this.footExtra);
      if (status === 'completed' && view && view.delivery) {
        const link = el('a', 'btn primary small', '下载成品 .docx');
        link.href = '/api/tasks/' + encodeURIComponent(this.taskId) + '/download';
        link.setAttribute('download', this.taskId + '.docx');
        this.footExtra.append(link);
        this.footExtra.append(
          el('span', 'chip mono', 'sha256:' + shortId(view.delivery.final_sha256, 16))
        );
      } else if (status === 'failed') {
        this.footExtra.append(
          el('span', 'chip danger', '执行失败 — 见时间线 execution_failed；可在 CLI 用 omas task events 复查')
        );
      }
    }

    async respond(action, slotId, files) {
      const fd = new FormData();
      if (action === 'omit_slot') fd.append('omit_slot', slotId);
      for (const f of files) fd.append('materials', f, f.name);
      try {
        await apiFetch(
          '/api/tasks/' + encodeURIComponent(this.taskId) + '/respond',
          { method: 'POST', body: fd }
        );
        const note = action === 'omit_slot'
          ? '已豁免槽位 ' + slotId + '，任务继续执行'
          : '已补充 ' + files.length + ' 份材料，任务继续执行';
        addSystemNote(note);
        if (this.view && this.view.awaiting) {
          this.awaitHandledFor = this.view.awaiting.awaiting_event_id;
        }
        toast(note, 'ok');
        this.start();
      } catch (err) {
        showError(err);
        this.render(); // 恢复按钮可用
      }
    }

    start() {
      this.stop();
      this.stopped = false;
      this.tick();
    }

    async tick() {
      if (this.stopped) return;
      let status = null;
      try {
        status = await this.refresh();
      } catch (err) {
        this.stopped = true;
        showError(err);
        return;
      }
      if (this.stopped) return;
      if (status !== null && LIVE_STATUSES.has(status)) {
        this.timer = window.setTimeout(() => this.tick(), POLL_MS);
      } else {
        this.stopped = true;
        loadConversationsQuiet(); // 会话排序可能已更新
      }
    }

    stop() {
      this.stopped = true;
      if (this.timer) {
        window.clearTimeout(this.timer);
        this.timer = null;
      }
    }
  }

  // ---------------------------------------------------- 事件 → 展示映射

  function countsLabel(counts) {
    if (!counts) return '';
    const parts = [];
    for (const key of Object.keys(counts)) parts.push(key + '=' + counts[key]);
    return parts.join(' ');
  }

  function renderEventRow(ev, card) {
    if (ev.event_code === 'tool_call') return renderToolRow(ev);
    const refs = ev.refs || {};
    const counts = ev.counts || {};
    const row = el('li', 'ev ev-' + ev.event_code);
    row.append(el('span', 'ev-ico', EVENT_ICONS[ev.event_code] || '·'));
    let text = ev.event_code;
    let sub = '';
    switch (ev.event_code) {
      case 'task_submitted':
        text = '任务已提交';
        sub = counts.materials !== undefined ? '材料 ' + counts.materials + ' 份' : '';
        break;
      case 'inventory':
        text = '盘点材料 (' + (counts.materials !== undefined ? counts.materials : '—') + ' 份)';
        break;
      case 'plan':
        text = '生成内容计划';
        break;
      case 'assembled':
        text = '装配槽位绑定' + (counts.slots !== undefined ? ' (' + counts.slots + ' 槽)' : '');
        break;
      case 'binding_committed':
        text = '绑定已提交 (' + (counts.slots !== undefined ? counts.slots : '—') + ' 槽)';
        sub = 'bound=' + (counts.bound !== undefined ? counts.bound : '?') +
          ' missing=' + (counts.missing !== undefined ? counts.missing : '?') +
          ' invalid=' + (counts.invalid !== undefined ? counts.invalid : '?');
        break;
      case 'awaiting_user': {
        const list = card.missing.length
          ? card.missing.join(', ')
          : (counts.missing !== undefined ? counts.missing + ' 个槽位' : '?');
        text = '等待补充：缺槽 [ ' + list + ' ]';
        row.classList.add('warn');
        break;
      }
      case 'decision_provide_material':
        text = '已补充材料，任务继续';
        break;
      case 'decision_omit_slot':
        text = '已豁免槽位，任务继续';
        break;
      case 'exported':
        text = '交付物已导出';
        row.classList.add('ok');
        break;
      case 'execution_failed':
        text = '执行失败';
        sub = 'code=' + (counts.code !== undefined ? counts.code : '?');
        row.classList.add('err');
        break;
      case 'task_cancelled':
        text = '任务已取消';
        row.classList.add('dim');
        break;
      case 'task_recovered':
        text = '任务已恢复';
        break;
      default:
        text = ev.event_code;
        break;
    }
    if (refs.awaiting_event && ev.event_code === 'awaiting_user') {
      sub = 'awaiting_event: ' + shortId(refs.awaiting_event, 12);
    }
    row.append(el('span', 'ev-text', text));
    if (sub) row.append(el('span', 'ev-sub', sub));
    return row;
  }

  function renderToolRow(ev) {
    const refs = ev.refs || {};
    const counts = ev.counts || {};
    const tool = refs.tool || 'tool';
    const row = el('li', 'ev ev-tool_call tool');
    row.append(el('span', 'ev-ico', '⚙'));
    row.append(el('code', 'tool-badge', tool));
    row.append(el('span', 'ev-text', TOOL_LABELS[tool] || '工具调用'));
    const sub = [];
    if (refs.artifact_id) sub.push('art:' + shortId(refs.artifact_id, 12));
    if (refs.span_handle) sub.push('span:' + shortId(refs.span_handle, 12));
    const countsText = countsLabel(counts);
    if (countsText) sub.push(countsText);
    if (sub.length) row.append(el('span', 'ev-sub', sub.join(' · ')));
    return row;
  }

  function inferStage(card) {
    const status = card.view ? card.view.status : 'created';
    const last = card.events.length ? card.events[card.events.length - 1].event_code : null;
    if (status === 'completed') return { label: '已交付', tone: 'ok' };
    if (status === 'failed') return { label: '执行失败', tone: 'err' };
    if (status === 'cancelled') return { label: '已取消', tone: 'dim' };
    if (status === 'awaiting_user') return { label: '等待补充材料', tone: 'warn' };
    if (status === 'parked') return { label: '已驻留（等待恢复）', tone: 'dim' };
    switch (last) {
      case null: return { label: '已提交，排队中', tone: '' };
      case 'task_submitted': return { label: '已提交', tone: '' };
      case 'inventory': return { label: '盘点材料', tone: '' };
      case 'tool_call': return { label: '模型正在调用工具…', tone: '' };
      case 'plan': return { label: '生成内容计划', tone: '' };
      case 'assembled': return { label: '装配槽位绑定', tone: '' };
      case 'binding_committed': return { label: '渲染与门禁校验', tone: '' };
      case 'exported': return { label: '已交付', tone: 'ok' };
      case 'execution_failed': return { label: '执行失败', tone: 'err' };
      case 'task_cancelled': return { label: '已取消', tone: 'dim' };
      case 'task_recovered': return { label: '恢复执行中', tone: '' };
      case 'decision_provide_material':
      case 'decision_omit_slot':
        return { label: '应答已受理，重新盘点', tone: '' };
      default: return { label: '执行中', tone: '' };
    }
  }

  // ---------------------------------------------------- 缺槽应答表单

  function renderAwaitForm(card) {
    const awaiting = card.view.awaiting;
    clear(card.awaitPanel);
    card.awaitPanel.append(
      el('div', 'await-title', '等待补充：缺槽 [ ' + awaiting.missing_slot_ids.join(', ') + ' ]')
    );

    const fileInput = document.createElement('input');
    fileInput.type = 'file';
    fileInput.multiple = true;
    fileInput.hidden = true;

    const row = el('div', 'await-row');
    const pick = el('button', 'btn small', '选择补料文件…');
    pick.type = 'button';
    const fileLabel = el('span', 'await-files', '未选择文件');
    const send = el('button', 'btn primary small', '上传补料并发送');
    send.type = 'button';
    send.disabled = true;
    let files = [];
    pick.addEventListener('click', () => fileInput.click());
    fileInput.addEventListener('change', () => {
      files = Array.prototype.slice.call(fileInput.files || []);
      fileLabel.textContent = files.length ? files.map((f) => f.name).join('、') : '未选择文件';
      send.disabled = !files.length;
    });
    send.addEventListener('click', () => {
      if (!files.length) return;
      send.disabled = true;
      pick.disabled = true;
      card.respond('provide_material', null, files);
    });
    row.append(pick, fileLabel, send);

    const omitWrap = el('div', 'await-omit');
    const omittable = awaiting.omittable_slot_ids || null;
    const candidates = awaiting.missing_slot_ids.filter(
      (slot) => omittable === null || omittable.includes(slot)
    );
    if (candidates.length) {
      omitWrap.append(el('span', 'await-omit-label', '或豁免某个模板允许省略的槽：'));
    }
    for (const slot of candidates) {
      const btn = el('button', 'btn ghost small', '豁免 ' + slot);
      btn.type = 'button';
      btn.addEventListener('click', () => {
        btn.disabled = true;
        card.respond('omit_slot', slot, []);
      });
      omitWrap.append(btn);
    }

    card.awaitPanel.append(row, omitWrap, fileInput);
  }

  // ------------------------------------------------------------------ 消息流

  function scrollBottom() {
    const box = $('#messages');
    box.scrollTop = box.scrollHeight;
  }

  function removeGuide() {
    const guide = $('#messages .empty-guide');
    if (guide) guide.remove();
  }

  function addTextMessage(role, content, fileNames) {
    removeGuide();
    const wrap = el('div', 'msg ' + (role === 'user' ? 'user' : 'assistant'));
    const bubble = el('div', 'bubble');
    bubble.append(el('div', 'msg-text', content));
    if (fileNames && fileNames.length) {
      const list = el('ul', 'msg-files');
      for (const name of fileNames) list.append(el('li', 'mono', name));
      bubble.append(list);
    }
    wrap.append(bubble);
    $('#messages').append(wrap);
    scrollBottom();
  }

  function linkifySourceLine(line) {
    // 来源行形如 "标题 | https://host/path"：仅把 URL 变成可点链接
    const m = /^(.*?)\s*\|\s*(https?:\/\/\S+)$/i.exec(line.trim());
    const node = el('li');
    if (m) {
      const a = el('a', null, (m[1] ? m[1] + ' — ' : '') + m[2]);
      a.href = m[2];
      a.target = '_blank';
      a.rel = 'noopener noreferrer';
      node.append(a);
    } else {
      node.textContent = line;
    }
    return node;
  }

  function addAnswerMessage(content) {
    removeGuide();
    const wrap = el('div', 'msg assistant');
    const bubble = el('div', 'bubble answer');
    const marker = '\n\n来源：\n';
    const idx = content.indexOf(marker);
    const body = idx >= 0 ? content.slice(0, idx) : content;
    const tail = idx >= 0 ? content.slice(idx + marker.length) : '';
    bubble.append(el('div', 'msg-text', body.trimEnd()));
    if (tail.trim()) {
      const sources = el('ul', 'msg-sources');
      for (const line of tail.split('\n')) {
        if (line.trim()) sources.append(linkifySourceLine(line));
      }
      if (sources.childNodes.length) {
        bubble.append(el('div', 'sources-label', '来源'));
        bubble.append(sources);
      }
    }
    wrap.append(bubble);
    $('#messages').append(wrap);
    scrollBottom();
  }

  function addClarifyMessage(content) {
    removeGuide();
    const wrap = el('div', 'msg assistant');
    const bubble = el('div', 'bubble clarify');
    bubble.append(el('div', 'clarify-icon', '❓'));
    bubble.append(el('div', 'msg-text', content));
    wrap.append(bubble);
    $('#messages').append(wrap);
    scrollBottom();
  }

  // 按消息类型渲染一条消息（会话详情与轮询 diff 共用）；返回是否实际渲染
  function renderMessage(msg) {
    if (state.renderedMsgIds.has(msg.message_id)) return false;
    state.renderedMsgIds.add(msg.message_id);
    if (msg.kind === 'text' && msg.content) {
      if (msg.role === 'user' && msg.content === state.pendingUserText) {
        state.pendingUserText = null; // 本地已渲染过这条用户消息，跳过重复气泡
        return true;
      }
      addTextMessage(msg.role, msg.content);
      return true;
    }
    if (msg.kind === 'answer' && msg.content) {
      addAnswerMessage(msg.content);
      return true;
    }
    if (msg.kind === 'clarify' && msg.content) {
      addClarifyMessage(msg.content);
      return true;
    }
    if (msg.kind === 'note' && msg.content) {
      addSystemNote(msg.content);
      return true;
    }
    if (msg.kind === 'task_started' && msg.task_id) {
      mountTaskCard(msg.task_id, state.turnTimer !== null);
      return true;
    }
    return false;
  }

  // ------------------------------------------------------------------ 对话轮次轮询

  function showThinking() {
    if ($('#think-bubble')) return;
    removeGuide();
    const wrap = el('div', 'msg assistant');
    wrap.id = 'think-bubble';
    const bubble = el('div', 'bubble thinking');
    bubble.append(el('span', 'think-label', '正在思考'));
    const dots = el('span', 'dots');
    dots.append(el('span'), el('span'), el('span'));
    bubble.append(dots);
    wrap.append(bubble);
    $('#messages').append(wrap);
    scrollBottom();
  }

  function hideThinking() {
    const node = $('#think-bubble');
    if (node) node.remove();
  }

  function stopTurnPolling() {
    if (state.turnTimer) {
      clearInterval(state.turnTimer);
      state.turnTimer = null;
    }
    state.turnConvId = null;
    state.pendingUserText = null;
    hideThinking();
    setComposerBusy(false); // 切走或结束时恢复输入区（轮询中曾禁用）
  }

  function startTurnPolling(conversationId) {
    stopTurnPolling();
    state.turnConvId = conversationId;
    showThinking();
    setComposerBusy(true);
    state.turnTimer = setInterval(() => {
      pollTurnOnce(conversationId).catch((err) => {
        console.warn('turn poll failed', err); // 轮询失败不弹错，下一拍重试
      });
    }, POLL_MS);
  }

  async function pollTurnOnce(conversationId) {
    const detail = await apiFetch('/api/conversations/' + encodeURIComponent(conversationId));
    if (state.turnConvId !== conversationId) return; // 已切走
    let rendered = false;
    for (const msg of detail.messages || []) {
      if (renderMessage(msg)) rendered = true;
    }
    if (rendered) loadConversationsQuiet();
    if (detail.busy) {
      showThinking();
    } else {
      stopTurnPolling();
    }
    scrollBottom();
  }

  function setComposerBusy(busy) {
    const btn = $('#btn-send');
    const input = $('#intent-input');
    btn.disabled = busy;
    input.disabled = busy;
    if (!busy) input.focus();
  }

  function addSystemNote(text) {
    const wrap = el('div', 'sys');
    wrap.append(el('span', 'sys-line', text));
    $('#messages').append(wrap);
    scrollBottom();
  }

  function mountTaskCard(taskId, live) {
    const card = new TaskCard(taskId);
    taskCards.add(card);
    removeGuide();
    $('#messages').append(card.node);
    if (live) {
      card.start();
    } else {
      card.refresh()
        .then(() => {
          if (!card.stopped && card.view && LIVE_STATUSES.has(card.view.status)) {
            card.start(); // 历史里仍在跑的任务恢复轮询
          }
        })
        .catch(showError);
    }
    scrollBottom();
    return card;
  }

  function stopAllTaskCards() {
    for (const card of taskCards) card.stop();
    taskCards.clear();
  }

  function guideNode() {
    const guide = el('div', 'empty-guide');
    guide.append(el('div', 'empty-title', '开始一次对话'));
    const steps = el('ol', 'empty-steps');
    const items = [
      ['选模板', '点击左侧「＋ 新会话」，选择模板与数据策略（默认 local_only，不上传到远端）'],
      ['发意图', '描述要生成的文档，或直接提问；可附带材料文件（Markdown 等）'],
      ['等装配', '任务时间线实时展示装配过程；缺槽时会请求补充材料或豁免槽位'],
    ];
    for (const pair of items) {
      const li = el('li');
      li.append(el('b', null, pair[0]));
      li.append(el('span', null, ' — ' + pair[1]));
      steps.append(li);
    }
    guide.append(steps);
    guide.append(el('div', 'empty-note', '输入只作为意图或问题，不会成为文档正文。'));
    return guide;
  }

  function showGuide() {
    const box = $('#messages');
    clear(box);
    box.append(guideNode());
  }

  // ------------------------------------------------------------------ 侧栏

  async function loadConversations() {
    const data = await apiFetch('/api/conversations');
    state.conversations = data.conversations || [];
    renderConversationList();
  }

  function loadConversationsQuiet() {
    loadConversations().catch(() => { /* 静默：列表刷新失败不打扰聊天 */ });
  }

  function renderConversationList() {
    const list = $('#conversation-list');
    clear(list);
    if (!state.conversations.length) {
      list.append(el('li', 'side-empty', '暂无会话 — 点击「＋ 新会话」开始'));
      return;
    }
    for (const conv of state.conversations) {
      const li = el('li', 'conv-item');
      li.dataset.id = conv.conversation_id;
      if (state.current && state.current.conversation_id === conv.conversation_id) {
        li.classList.add('active');
      }
      li.append(el('div', 'conv-title', conv.title || '未命名会话'));
      li.append(el('div', 'conv-time', fmtTime(conv.updated_at)));
      li.addEventListener('click', () => openConversation(conv.conversation_id));
      list.append(li);
    }
  }

  async function loadTemplates() {
    const data = await apiFetch('/api/templates');
    state.templates = data.templates || [];
    renderTemplateList();
  }

  function renderTemplateList() {
    const box = $('#template-list');
    clear(box);
    if (!state.templates.length) {
      box.append(el('div', 'side-empty', '暂无模板 — 用「上传模板」注册 .docx'));
      return;
    }
    for (const t of state.templates) {
      const card = el('div', 'tpl-card');
      card.dataset.templateId = t.template_id;
      const head = el('div', 'tpl-head');
      head.append(el('span', 'tpl-name', t.display_name || t.template_id));
      head.append(el('span', 'chip mono', 'v' + t.version));
      head.append(
        el('span', 'chip ' + (t.activatable ? 'ok' : 'warn'), t.activatable ? '可激活' : '不可激活')
      );
      card.append(head);
      card.append(el('div', 'tpl-desc', t.description || '（无简介 — 点击卡片补充）'));
      card.append(el('div', 'tpl-meta', t.template_id + ' · ' + (t.slots || []).length + ' 槽位'));
      card.addEventListener('click', () => openMetaModal(t));
      box.append(card);
    }
  }

  function findTemplateByVersion(versionId) {
    for (const t of state.templates) {
      if (t.template_version_id === versionId) return t;
    }
    return null;
  }

  // ------------------------------------------------------------------ 会话

  function renderChatHead() {
    const conv = state.current;
    const titleEl = $('#current-title');
    const tplChip = $('#current-tpl');
    const policyChip = $('#current-policy');
    if (!conv) {
      titleEl.textContent = '未选择会话';
      tplChip.hidden = true;
      policyChip.hidden = true;
      return;
    }
    titleEl.textContent = conv.title || '未命名会话';
    const t = findTemplateByVersion(conv.template_version_id);
    tplChip.textContent = t ? t.display_name : shortId(conv.template_version_id, 14);
    tplChip.hidden = false;
    if (conv.data_policy === 'llm_allowed') {
      policyChip.textContent = '远端模型 llm_allowed';
      policyChip.className = 'chip warn';
    } else {
      policyChip.textContent = '本地 local_only';
      policyChip.className = 'chip ok';
    }
    policyChip.hidden = false;
  }

  async function openConversation(conversationId) {
    stopAllTaskCards();
    stopTurnPolling();
    let detail;
    try {
      detail = await apiFetch(
        '/api/conversations/' + encodeURIComponent(conversationId)
      );
    } catch (err) {
      showError(err);
      return;
    }
    state.current = detail.conversation;
    renderChatHead();
    renderConversationList();

    const box = $('#messages');
    clear(box);
    state.renderedMsgIds = new Set();
    state.pendingUserText = null;
    let any = false;
    for (const msg of detail.messages || []) {
      if (renderMessage(msg)) any = true;
    }
    if (!any && !detail.busy) {
      showGuide();
    } else {
      removeGuide();
    }
    if (detail.busy) {
      // 上一轮还在服务端处理（分流/问答/装配），恢复轮询
      startTurnPolling(conversationId);
    }
    scrollBottom();
  }

  async function sendMessage() {
    const conv = state.current;
    if (!conv) {
      toast('请先创建或选择一个会话（左侧「＋ 新会话」）', 'warn');
      return;
    }
    const input = $('#intent-input');
    const intent = input.value.trim();
    if (!intent) {
      toast('意图不能为空', 'warn');
      input.focus();
      return;
    }
    const btn = $('#btn-send');
    btn.disabled = true;
    const fileNames = pendingFiles.map((f) => f.name);
    try {
      const fd = new FormData();
      fd.append('intent', intent);
      for (const f of pendingFiles) fd.append('materials', f, f.name);
      await apiFetch(
        '/api/conversations/' + encodeURIComponent(conv.conversation_id) + '/messages',
        { method: 'POST', body: fd }
      );
      // 202 turn_started：服务端先做意图分流，再出回答/追问/任务卡，全部经会话轮询渲染
      state.pendingUserText = intent;
      addTextMessage('user', intent, fileNames);
      input.value = '';
      autoGrow(input);
      setPendingFiles([]);
      startTurnPolling(conv.conversation_id);
      loadConversationsQuiet();
    } catch (err) {
      if (err && err.code === 'TURN_BUSY') {
        toast('上一轮仍在处理中，请稍候', 'warn', 'TURN_BUSY');
        btn.disabled = false;
        return;
      }
      showError(err);
      btn.disabled = false;
    }
    // 成功路径的按钮状态由 setComposerBusy 在轮询结束时恢复
  }

  // ------------------------------------------------------------------ 输入区

  function setPendingFiles(files) {
    pendingFiles = files;
    const box = $('#material-chips');
    clear(box);
    box.hidden = !files.length;
    files.forEach((f, index) => {
      const chip = el('span', 'file-chip', f.name);
      const x = el('button', 'chip-x', '✕');
      x.type = 'button';
      x.title = '移除 ' + f.name;
      x.addEventListener('click', () => {
        const next = pendingFiles.slice();
        next.splice(index, 1);
        setPendingFiles(next);
      });
      chip.append(x);
      box.append(chip);
    });
  }

  function autoGrow(textarea) {
    textarea.style.height = 'auto';
    textarea.style.height = Math.min(textarea.scrollHeight, 200) + 'px';
  }

  // ------------------------------------------------------------------ 弹层

  function openOverlay(id) {
    document.getElementById(id).hidden = false;
  }

  function closeOverlay(id) {
    document.getElementById(id).hidden = true;
  }

  // ---- 新会话

  function fillNewConversationTemplates() {
    const box = $('#new-tpl-list');
    clear(box);
    if (!state.templates.length) {
      box.append(el('div', 'side-empty', '模板库为空 — 请先上传模板'));
      return;
    }
    for (const t of state.templates) {
      const label = el('label', 'choose-item');
      const radio = document.createElement('input');
      radio.type = 'radio';
      radio.name = 'new-template';
      radio.value = t.template_version_id;
      const name = el('span', 'choose-name', t.display_name || t.template_id);
      const desc = el('span', 'choose-desc', t.description || '');
      label.append(radio, name, el('span', 'chip mono', 'v' + t.version));
      label.append(
        el('span', 'chip ' + (t.activatable ? 'ok' : 'warn'), t.activatable ? '可激活' : '不可激活'),
        desc
      );
      box.append(label);
    }
  }

  async function createConversation() {
    const chosen = document.querySelector('input[name="new-template"]:checked');
    if (!chosen) {
      toast('请选择一个模板', 'warn');
      return;
    }
    const payload = {
      template_version_id: chosen.value,
      data_policy: $('#new-llm').checked ? 'llm_allowed' : 'local_only',
    };
    const title = $('#new-title-input').value.trim();
    if (title) payload.title = title;
    try {
      const data = await apiFetch('/api/conversations', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      closeOverlay('overlay-new');
      toast('会话已创建：' + data.title, 'ok');
      await loadConversations();
      openConversation(data.conversation_id);
    } catch (err) {
      showError(err);
    }
  }

  // ---- 上传模板（三步）

  const uploadState = { file: null, slots: [] };

  function showUploadStep(n) {
    $('#upload-step1').hidden = n !== 1;
    $('#upload-step2').hidden = n !== 2;
    $('#upload-step3').hidden = n !== 3;
  }

  function resetUploadModal() {
    uploadState.file = null;
    uploadState.slots = [];
    showUploadStep(1);
    const fileInput = $('#upload-file');
    fileInput.value = '';
    const dz = $('#dropzone');
    dz.classList.remove('has-file');
    dz.textContent = '拖拽 .docx 到此处，或点击选择文件';
    const result = $('#precheck-result');
    clear(result);
    result.hidden = true;
    $('#btn-upload-next').disabled = true;
  }

  function handleUploadFile(file) {
    if (!/\.docx$/i.test(file.name)) {
      toast('只支持 .docx 文件：' + file.name, 'warn');
      return;
    }
    uploadState.file = file;
    const dz = $('#dropzone');
    dz.classList.add('has-file');
    dz.textContent = '已选择：' + file.name + '（点击可重选）';
    precheckFile(file);
  }

  async function precheckFile(file) {
    const box = $('#precheck-result');
    clear(box);
    box.hidden = false;
    box.append(el('div', 'muted', '正在分析 ' + file.name + ' …'));
    $('#btn-upload-next').disabled = true;
    try {
      const fd = new FormData();
      fd.append('docx', file, file.name);
      const data = await apiFetch('/api/templates/precheck', { method: 'POST', body: fd });
      uploadState.slots = data.slots || [];
      clear(box);
      if (!uploadState.slots.length) {
        box.append(el('div', 'warn-text', '未发现 {{ 槽位名 }} 占位符'));
        box.append(
          el('div', 'muted small', '请先在文档中需要填充的段落写 {{ 槽位名 }} 再上传')
        );
      } else {
        box.append(el('div', 'ok-text', '发现 ' + uploadState.slots.length + ' 个槽位：'));
        const list = el('ul', 'slot-found');
        for (const slot of uploadState.slots) list.append(el('li', null, '{{ ' + slot + ' }}'));
        box.append(list);
        $('#btn-upload-next').disabled = false;
      }
    } catch (err) {
      clear(box);
      box.append(el('div', 'warn-text', '预检失败'));
      showError(err);
    }
  }

  function buildUploadStep2() {
    const suggested = uploadState.file
      ? uploadState.file.name
          .replace(/\.docx$/i, '')
          .replace(/[^0-9A-Za-z_-]+/g, '-')
          .replace(/^-+|-+$/g, '')
      : '';
    $('#tpl-id-input').value = suggested;
    $('#tpl-display-input').value = '';
    $('#tpl-desc-input').value = '';
    const box = $('#slot-semantics');
    clear(box);
    for (const slot of uploadState.slots) {
      const field = el('label', 'field');
      field.append(el('span', 'field-label', '{{ ' + slot + ' }} 的语义说明（必填）'));
      const input = document.createElement('input');
      input.dataset.slot = slot;
      input.placeholder = '如：本周销售情况小结';
      field.append(input);
      box.append(field);
    }
    showUploadStep(2);
  }

  async function submitTemplate() {
    const templateId = $('#tpl-id-input').value.trim();
    const displayName = $('#tpl-display-input').value.trim();
    const description = $('#tpl-desc-input').value.trim();
    if (!/^[0-9A-Za-z_-]+$/.test(templateId)) {
      toast('模板 ID 仅限字母 / 数字 / - / _', 'warn');
      return;
    }
    if (!displayName) {
      toast('请填写显示名称', 'warn');
      return;
    }
    const semantics = {};
    let missing = false;
    for (const input of document.querySelectorAll('#slot-semantics input')) {
      const value = input.value.trim();
      if (!value) {
        missing = true;
        input.classList.add('invalid');
      } else {
        input.classList.remove('invalid');
      }
      semantics[input.dataset.slot] = value;
    }
    if (missing) {
      toast('每个槽位的语义说明都是必填的', 'warn');
      return;
    }
    const btn = $('#btn-upload-submit');
    btn.disabled = true;
    try {
      const fd = new FormData();
      fd.append('docx', uploadState.file, uploadState.file.name);
      fd.append('template_id', templateId);
      fd.append('display_name', displayName);
      fd.append('description', description);
      fd.append('slot_semantics', JSON.stringify(semantics));
      const data = await apiFetch('/api/templates', { method: 'POST', body: fd });
      renderUploadResult(data);
      loadTemplates().catch(showError);
    } catch (err) {
      showError(err);
    } finally {
      btn.disabled = false;
    }
  }

  function renderUploadResult(data) {
    showUploadStep(3);
    const box = $('#upload-result');
    clear(box);
    box.append(el('div', 'ok-text', '模板已注册：' + data.template_id + ' · v' + data.version));
    box.append(el('div', 'mono muted small', 'version_id: ' + data.template_version_id));
    if (!data.activatable) {
      const warn = el('div', 'warnbox');
      warn.append(el('div', 'warn-text', '警告：模板暂不可激活（activatable=false）'));
      const findings = el('ul', 'findings');
      const list = data.findings && data.findings.length ? data.findings : ['（服务端未返回具体 findings）'];
      for (const f of list) findings.append(el('li', null, f));
      warn.append(findings);
      box.append(warn);
    } else {
      box.append(el('div', 'ok-text small', 'activatable — 可直接用于新会话'));
    }
  }

  // ---- 编辑模板信息

  function openMetaModal(t) {
    $('#meta-tid').value = t.template_id;
    $('#meta-display').value = t.display_name || '';
    $('#meta-desc').value = t.description || '';
    openOverlay('overlay-meta');
  }

  async function saveMeta() {
    const displayName = $('#meta-display').value.trim();
    if (!displayName) {
      toast('显示名称必填', 'warn');
      return;
    }
    const templateId = $('#meta-tid').value;
    try {
      await apiFetch('/api/templates/' + encodeURIComponent(templateId) + '/meta', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          display_name: displayName,
          description: $('#meta-desc').value.trim(),
        }),
      });
      closeOverlay('overlay-meta');
      toast('模板信息已更新', 'ok');
      await loadTemplates();
      renderChatHead(); // 顶栏模板名可能随之变化
    } catch (err) {
      showError(err);
    }
  }

  // ------------------------------------------------------------------ 事件绑定

  function wireEvents() {
    $('#btn-new-conv').addEventListener('click', () => {
      fillNewConversationTemplates();
      $('#new-title-input').value = '';
      $('#new-llm').checked = false;
      openOverlay('overlay-new');
    });
    $('#btn-create-conv').addEventListener('click', createConversation);

    $('#btn-upload-tpl').addEventListener('click', () => {
      resetUploadModal();
      openOverlay('overlay-upload');
    });
    const dropzone = $('#dropzone');
    dropzone.addEventListener('click', () => $('#upload-file').click());
    dropzone.addEventListener('dragover', (e) => {
      e.preventDefault();
      dropzone.classList.add('drag');
    });
    dropzone.addEventListener('dragleave', () => dropzone.classList.remove('drag'));
    dropzone.addEventListener('drop', (e) => {
      e.preventDefault();
      dropzone.classList.remove('drag');
      const file = e.dataTransfer.files && e.dataTransfer.files[0];
      if (file) handleUploadFile(file);
    });
    $('#upload-file').addEventListener('change', (e) => {
      const file = e.target.files && e.target.files[0];
      if (file) handleUploadFile(file);
    });
    $('#btn-upload-next').addEventListener('click', buildUploadStep2);
    $('#btn-upload-back').addEventListener('click', () => showUploadStep(1));
    $('#btn-upload-submit').addEventListener('click', submitTemplate);

    $('#btn-meta-save').addEventListener('click', saveMeta);

    for (const btn of document.querySelectorAll('.close-overlay')) {
      btn.addEventListener('click', () => closeOverlay(btn.dataset.close));
    }
    for (const overlay of document.querySelectorAll('.overlay')) {
      overlay.addEventListener('click', (e) => {
        if (e.target === overlay) closeOverlay(overlay.id);
      });
    }
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') {
        for (const overlay of document.querySelectorAll('.overlay')) {
          if (!overlay.hidden) closeOverlay(overlay.id);
        }
      }
    });

    // 输入区
    const input = $('#intent-input');
    input.addEventListener('keydown', (e) => {
      if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') {
        e.preventDefault();
        sendMessage();
      }
    });
    input.addEventListener('input', () => autoGrow(input));
    $('#btn-send').addEventListener('click', sendMessage);
    $('#btn-attach').addEventListener('click', () => $('#material-input').click());
    $('#material-input').addEventListener('change', (e) => {
      const files = Array.prototype.slice.call(e.target.files || []);
      if (files.length) setPendingFiles(pendingFiles.concat(files));
      e.target.value = '';
    });

    const composer = $('#composer');
    composer.addEventListener('dragover', (e) => {
      e.preventDefault();
      composer.classList.add('drag-over');
    });
    composer.addEventListener('dragleave', () => composer.classList.remove('drag-over'));
    composer.addEventListener('drop', (e) => {
      e.preventDefault();
      composer.classList.remove('drag-over');
      const files = Array.prototype.slice.call(e.dataTransfer.files || []);
      if (files.length) setPendingFiles(pendingFiles.concat(files));
    });
  }

  // ------------------------------------------------------------------ 启动

  async function init() {
    wireEvents();
    showGuide();
    try {
      await loadTemplates();
    } catch (err) {
      showError(err);
    }
    try {
      await loadConversations();
    } catch (err) {
      showError(err);
    }
    if (state.conversations.length) {
      openConversation(state.conversations[0].conversation_id);
    }
    $('#intent-input').focus();
  }

  document.addEventListener('DOMContentLoaded', init);
})();
