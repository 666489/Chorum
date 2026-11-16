/* Chorum 工作台：讨论 / 工具记录 / 推理摘要 / 持续执行 / 记忆。
   同一套后端接口，不轮询（只由事件触发的轻量刷新），不引外部资源。 */
(() => {
  const $ = id => document.getElementById(id);
  const VIEWS = [['discussion', '讨论'], ['tools', '工具记录'], ['thinking', '推理摘要'], ['tasks', '持续执行'], ['memory', '记忆']];
  let view = 'discussion', snapshot = { agents: [] }, workflows = [], memories = [], generation = 0, refreshTimer, pending = 0;

  // 文本节点转义
  const safe = s => { const el = document.createElement('span'); el.textContent = String(s ?? ''); return el.innerHTML; };
  // 属性值转义：safe() 走 textContent→innerHTML 时**不转义引号**，直接拼进 value="…" 会被引号截断
  const attr = s => safe(s).replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  // 状态 → CSS 钩子：只走白名单，绝不把后端字符串直接拼进属性
  const STATUS_SLUG = { done: 'done', succeeded: 'done', complete: 'done', running: 'running', planning: 'running', pending: 'running', error: 'error', failed: 'error', paused: 'paused', blocked: 'blocked' };
  const stSlug = s => STATUS_SLUG[s] || 'other';

  const toolbar = document.createElement('div');
  toolbar.id = 'workbenchBar';
  toolbar.innerHTML = `<div class="wb-brand">Chorum<span id="wbVersion" class="wb-chip">读版本…</span><i id="wbDot" class="wb-dot" title="中枢状态"></i></div>
    <nav aria-label="工作区视图">${VIEWS.map(([v, l], i) => `<button data-wb-view="${v}" title="Ctrl+${i + 1}" class="${v === 'discussion' ? 'on' : ''}">${l}</button>`).join('')}</nav>
    <div class="wb-actions"><button id="wbRound" title="多个成员按顺序多轮讨论">发起圆桌</button><button id="wbWork" class="primary" title="主代理规划一次，执行代理持续工作">创建执行任务</button></div>`;
  document.querySelector('main header').after(toolbar);

  const panel = document.createElement('section');
  panel.id = 'workbenchPanel'; panel.hidden = true; panel.setAttribute('aria-live', 'polite');
  toolbar.after(panel);

  const error = document.createElement('div');
  error.id = 'wbError'; error.hidden = true;
  error.innerHTML = `<span id="wbErrorText"></span><button id="wbErrorClose" aria-label="关闭提示">×</button>`;
  toolbar.before(error);
  function showError(message) {
    const text = $('wbErrorText');
    if (text) text.textContent = String(message || '');
    error.hidden = false;
  }
  function clearError() { error.hidden = true; const t = $('wbErrorText'); if (t) t.textContent = ''; }
  $('wbErrorClose').onclick = clearError;
  window.showWorkbenchError = showError;

  /** 统一请求：错误优先取服务端 error；用 pending 计数驱动状态灯。 */
  async function api(path, body) {
    pending++;
    stamp();
    try {
      const response = await fetch(path, {
        method: body === undefined ? 'GET' : 'POST',
        headers: { 'Content-Type': 'application/json', 'X-ClaudeLink-Token': window.__HUB_TOKEN__ || '' },
        ...(body === undefined ? {} : { body: JSON.stringify(body) }),
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok || data.ok === false) throw new Error(data.error || ('HTTP ' + response.status));
      return data;
    } finally {
      pending = Math.max(0, pending - 1);
      stamp();
    }
  }

  const card = (title, body, meta = '') => `<article class="wb-card"><div class="wb-card-title"><span>${safe(title)}</span><small>${safe(meta)}</small></div>${body}</article>`;
  const empty = (text, hint = '') => `<div class="wb-empty">${safe(text)}${hint ? `<div class="wb-hint">${safe(hint)}</div>` : ''}</div>`;
  const copyable = content => `<div class="wb-code"><button class="wb-copy" data-wb-copy type="button" aria-label="复制这段内容">复制</button><pre>${safe(content)}</pre></div>`;

  function stamp() {
    const h = snapshot.health || {};
    const chip = $('wbVersion'), dot = $('wbDot');
    if (chip) chip.textContent = h.version ? 'v' + h.version : '版本未知';
    if (dot) {
      dot.className = 'wb-dot ' + (pending ? 'busy' : 'on');
      dot.title = pending ? '正在与中枢通信…' : '中枢在线';
    }
  }

  async function refresh() {
    if (view === 'discussion') return;
    const requestedView = view, request = ++generation;
    const current = () => request === generation && requestedView === view;
    if (!panel.dataset.ready) panel.innerHTML = `<div class="wb-loading">加载中…</div>`;
    panel.dataset.ready = '1';
    if (view === 'tasks') {
      workflows = (await api('/api/workflows')).workflows || [];
      if (!current()) return;
      panel.innerHTML = `<div class="wb-section-head"><div><h2>一次调度，持续执行</h2><p>主代理只生成一次计划，执行代理按依赖持续工作并保存检查点。</p></div><button data-wb-new-work>创建任务</button></div>` +
        (workflows.length ? workflows.map(w => card(w.topic || w.id,
          `<div class="wb-meta">${safe(w.id)} · <span data-st="${stSlug(w.status)}">${safe(w.status)}</span> · 主代理 ${safe(w.planner)}</div>
           ${w.error ? `<p class="wb-error">${safe(w.error)}</p>` : ''}
           ${(w.tasks || []).map(t => `<div class="wb-task"><b>${safe(t.id)} · ${safe(t.agent)}</b><span data-st="${stSlug(t.status)}">${safe(t.status)} · ${t.steps || 0} 步</span><p>${safe(t.instruction)}</p><pre>${safe(t.summary || t.error || '等待依赖完成')}</pre></div>`).join('')}
           <div class="wb-actions">${['error', 'paused'].includes(w.status) ? `<button data-wb-resume="${attr(w.id)}">恢复未完成任务</button>` : ''}${['planning', 'running'].includes(w.status) ? `<button data-wb-stop="${attr(w.id)}">暂停</button>` : ''}</div>`)).join('')
          : empty('尚无执行任务。', '创建任务后在这里看主代理计划与子任务检查点。'));
    } else if (view === 'memory') {
      const query = panel.querySelector('#wbMemoryQuery')?.value ?? '';
      const found = (await api('/api/memory?q=' + encodeURIComponent(query))).memories || [];
      memories = found;
      if (!current()) return;
      panel.innerHTML = `<div class="wb-section-head"><div><h2>项目记忆</h2><p>短期记忆按会话隔离；长期记忆在同一工作区复用，保留来源。</p></div></div>
        <div class="wb-memory-tools"><input id="wbMemoryQuery" placeholder="检索关键词，回车搜索" value="${attr(query)}"><button data-wb-search>检索</button><button data-wb-new-memory>保存记忆</button></div>` +
        (memories.length ? memories.map(m => card(m.kind === 'long' ? '长期记忆' : '短期记忆',
          copyable(m.text) + `<div class="wb-meta">${safe(m.source)} · ${safe(m.id)}</div><button data-wb-forget="${attr(m.id)}">删除这条记忆</button>`)).join('')
          : empty(query ? '没有匹配的记忆。' : '还没有记忆。', '保存已确认的经验；执行任务的检查点会自动写入短期记忆。'));
      const input = panel.querySelector('#wbMemoryQuery');
      if (input) {
        input.onkeydown = e => { if (e.key === 'Enter') { e.preventDefault(); refresh().catch(err => showError(err.message)); } };
        if (query) { input.focus(); input.setSelectionRange(query.length, query.length); }
      }
    } else {
      const activity = await api('/api/activity');
      if (!current()) return;
      const turns = new Map((activity.turns || []).map(t => [t.id, t]));
      const all = Object.values(activity.steps || {}).flat();
      const steps = all.filter(s => view === 'thinking' ? s.kind === 'thinking' : ['tool_call', 'tool_result', 'note'].includes(s.kind));
      panel.innerHTML = `<div class="wb-section-head"><div><h2>${view === 'thinking' ? '推理摘要' : '工具执行记录'}</h2><p>${view === 'thinking' ? '只展示服务商实际返回的可见推理内容。' : '真实调用参数、结果和失败；工具错误不会被计为成功。'}</p></div><span class="wb-count">${steps.length} 条</span></div>` +
        (steps.length ? steps.map(s => {
          let d = s.detail || '';
          try { d = JSON.parse(d); } catch { /* 原文就留着 */ }
          const text = typeof d === 'object' ? (d.text || JSON.stringify(d, null, 2)) : d;
          return `<details class="wb-card" ${view === 'thinking' ? 'open' : ''}><summary><span>${safe(turns.get(s.turn_id)?.agent || '')} · ${safe(s.name || s.kind)}</span><small data-st="${stSlug(s.status)}">${safe(s.status)}</small></summary>${copyable(text)}</details>`;
        }).join('') : empty('尚无记录。', '执行一次讨论或任务后，实际的工具调用与推理摘要会出现在这里。'));
    }
  }

  async function setView(next) {
    view = next;
    clearError();
    toolbar.querySelectorAll('[data-wb-view]').forEach(b => {
      const on = b.dataset.wbView === view;
      b.classList.toggle('on', on);
      b.setAttribute('aria-current', on ? 'page' : 'false');
    });
    $('log').hidden = view !== 'discussion';
    $('composer').hidden = view !== 'discussion';
    panel.hidden = view === 'discussion';
    if (view !== 'discussion') panel.dataset.ready = '';
    try { await refresh(); } catch (e) { showError(e.message); }
  }

  toolbar.addEventListener('click', e => {
    const button = e.target.closest('[data-wb-view]');
    if (button) setView(button.dataset.wbView).catch(err => showError(err.message));
  });

  // Ctrl+1..5 切换视图，Esc 回讨论（对话框打开时不抢 Esc）
  document.addEventListener('keydown', e => {
    if (e.ctrlKey && /^[1-5]$/.test(e.key)) {
      e.preventDefault();
      setView(VIEWS[Number(e.key) - 1][0]).catch(err => showError(err.message));
    } else if (e.key === 'Escape' && view !== 'discussion' && !document.querySelector('dialog[open]')) {
      setView('discussion').catch(err => showError(err.message));
    }
  });

  /** 对话框：自动聚焦第一个字段，Ctrl/⌘+Enter 提交，Esc 关闭由 <dialog> 自带。 */
  function dialog(title, html, submitText = '开始') {
    const el = document.createElement('dialog');
    el.className = 'wb-dialog';
    el.innerHTML = `<form><div class="wb-dialog-head"><h2>${safe(title)}</h2><button type="button" data-wb-close aria-label="关闭">×</button></div>
      ${html}<p class="wb-error" data-wb-form-error></p>
      <div class="wb-actions"><button type="button" data-wb-close>取消</button><button type="submit" class="primary">${safe(submitText)}</button></div></form>`;
    document.body.append(el);
    el.querySelectorAll('[data-wb-close]').forEach(b => { b.onclick = () => el.close(); });
    el.onclose = () => el.remove();
    el.showModal();
    const first = el.querySelector('textarea, input, select');
    if (first) first.focus();
    el.addEventListener('keydown', e => {
      if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') { e.preventDefault(); el.querySelector('form').requestSubmit(); }
    });
    return el;
  }

  /** 提交包装：按钮进入 busy 态，失败文案写进对话框自己的错误行。 */
  function submit(el, handler) {
    const button = el.querySelector('[type=submit]');
    el.querySelector('form').onsubmit = async e => {
      e.preventDefault();
      const note = el.querySelector('[data-wb-form-error]');
      note.textContent = '';
      const label = button ? button.textContent : '';
      if (button) { button.disabled = true; button.textContent = '提交中…'; }
      try {
        await handler(el);
      } catch (err) {
        note.textContent = err.message || String(err);
      } finally {
        if (button) { button.disabled = false; button.textContent = label; }
      }
    };
  }

  async function getSnapshot() { snapshot = await api('/api/state'); return snapshot; }
  const enabled = () => (snapshot.agents || []).filter(a => a.enabled);
  const agentOptions = () => enabled().map(a => `<option value="${attr(a.name)}">${safe(a.label)}（${a.kind === 'cli' ? 'CLI' : 'API'}）</option>`).join('');

  async function workDialog() {
    try {
      await getSnapshot();
      if (!enabled().length) throw new Error('先添加或启用至少一位成员（也可以用 chorum model add 加一个 API 模型）');
      const el = dialog('创建持续执行任务', `
        <label>原始目标与验收条件<textarea id="wbWorkTopic" required rows="5" placeholder="需要完成什么，交付哪些实际结果"></textarea></label>
        <label>主代理（只调度一次）<select id="wbPlanner">${agentOptions()}</select></label>
        <fieldset><legend>执行代理</legend>${enabled().map(a => `<label class="wb-check"><input type="checkbox" name="workers" value="${attr(a.name)}" checked>${safe(a.label)}</label>`).join('')}</fieldset>
        <label>每个子任务最多步数<input id="wbSteps" type="number" value="4" min="1" max="20"></label>
        <p class="wb-tip">Ctrl+Enter 提交。</p>`);
      submit(el, async () => {
        await api('/api/workflows/start', {
          topic: el.querySelector('#wbWorkTopic').value,
          planner: el.querySelector('#wbPlanner').value,
          agents: [...el.querySelectorAll('[name=workers]:checked')].map(n => n.value),
          max_steps: Number(el.querySelector('#wbSteps').value),
        });
        el.close();
        await setView('tasks');
      });
    } catch (e) { showError(e.message); }
  }

  async function roundDialog() {
    try {
      await getSnapshot();
      if (!enabled().length) throw new Error('先添加或启用成员；同一成员可以添加多个独立角色');
      const el = dialog('多智能体圆桌讨论', `
        <label>原始议题<textarea id="wbRoundTopic" required rows="5" placeholder="讨论对象、必须回答的问题、材料范围与禁止事项"></textarea></label>
        <div id="wbParticipants"></div><button type="button" id="wbAddParticipant">添加角色实例</button>
        <label>讨论轮数<input id="wbRounds" type="number" min="1" max="20" value="2"></label>
        <p class="wb-tip">每个实例是一份独立的角色设定与会话；从上到下的顺序就是发言顺序。</p>`);
      const add = agent => {
        const row = document.createElement('div');
        row.className = 'wb-participant';
        row.innerHTML = `<select aria-label="成员">${agentOptions()}</select><input aria-label="角色" placeholder="角色名称"><button type="button" aria-label="移除此角色">移除</button>`;
        if (agent) row.querySelector('select').value = agent;
        row.querySelector('button').onclick = () => row.remove();
        el.querySelector('#wbParticipants').append(row);
      };
      enabled().slice(0, 3).forEach(a => add(a.name));
      if (enabled().length === 1) add(enabled()[0].name);
      el.querySelector('#wbAddParticipant').onclick = () => add();
      submit(el, async () => {
        const participants = [...el.querySelectorAll('.wb-participant')]
          .map(row => ({ agent: row.querySelector('select').value, role: row.querySelector('input').value }));
        await api('/api/roundtable/start', {
          topic: el.querySelector('#wbRoundTopic').value,
          rounds: Number(el.querySelector('#wbRounds').value),
          participants, new_conversation: true,
        });
        el.close();
        await setView('discussion');
        if (typeof reloadAll === 'function') await reloadAll();
      });
    } catch (e) { showError(e.message); }
  }

  function memoryDialog() {
    const el = dialog('保存项目记忆', `
      <label>类型<select id="wbMemoryKind"><option value="short">短期：仅当前会话</option><option value="long">长期：同一工作区复用</option></select></label>
      <label>已确认的内容<textarea id="wbMemoryText" rows="5" required></textarea></label>`, '保存');
    submit(el, async () => {
      await api('/api/memory', {
        kind: el.querySelector('#wbMemoryKind').value,
        text: el.querySelector('#wbMemoryText').value,
        source: 'user',
      });
      el.close();
      await refresh();
    });
  }

  $('wbWork').onclick = workDialog;
  $('wbRound').onclick = roundDialog;

  panel.addEventListener('click', async e => {
    const copy = e.target.closest('[data-wb-copy]');
    if (copy) {
      const text = copy.parentElement.querySelector('pre')?.textContent || '';
      try {
        await navigator.clipboard.writeText(text);
        copy.textContent = '已复制';
      } catch {
        copy.textContent = '复制失败';
      }
      setTimeout(() => { copy.textContent = '复制'; }, 1400);
      return;
    }
    const b = e.target.closest('button');
    if (!b) return;
    try {
      if (b.hasAttribute('data-wb-new-work')) return workDialog();
      if (b.hasAttribute('data-wb-new-memory')) return memoryDialog();
      if (b.hasAttribute('data-wb-search')) return refresh();
      if (b.dataset.wbForget) await api('/api/memory', { action: 'delete', id: b.dataset.wbForget });
      if (b.dataset.wbResume) await api('/api/workflows/resume', { id: b.dataset.wbResume });
      if (b.dataset.wbStop) await api('/api/workflows/stop', { id: b.dataset.wbStop });
      await refresh();
    } catch (err) { showError(err.message); }
  });

  // 成员面板的「推理强度」块：面板 HTML 由 index.html 生成，这里只追加，不改其它字段
  window.enhanceMemberPanel = async (a, body) => {
    try {
      const caps = await api('/api/capabilities');
      if (!body.isConnected || $('mpEffort')) return;
      const block = document.createElement('div');
      block.className = 'mp-sec';
      block.innerHTML = `<h3>推理强度 effort</h3><select id="mpEffort">${(caps.effort[a.name] || ['']).map(v => `<option value="${attr(v)}">${safe(v || 'auto（跟随默认）')}</option>`).join('')}</select><span class="note" id="mpEffortNote"></span>`;
      body.append(block);
      block.querySelector('select').value = a.effort || '';
      block.querySelector('select').onchange = async e => {
        const note = block.querySelector('#mpEffortNote');
        note.textContent = '保存中…';
        try {
          await api('/api/agents/effort', { agent: a.name, effort: e.target.value });
          note.textContent = '已应用';
          if (typeof loadAgents === 'function') await loadAgents();
        } catch (err) { note.textContent = err.message; }
      };
    } catch (e) { showError(e.message); }
  };

  window.addEventListener('hub-event', event => {
    if (view !== 'discussion' && ['workflow', 'step', 'turn_ended', 'conversation', 'cleared'].includes(event.detail.type)) {
      clearTimeout(refreshTimer);
      refreshTimer = setTimeout(() => refresh().catch(e => showError(e.message)), 120);
    }
  });
  window.addEventListener('unhandledrejection', e => showError('界面操作失败：' + (e.reason?.message || e.reason)));

  stamp();
  getSnapshot().catch(e => showError('初始化失败：' + e.message));
})();
