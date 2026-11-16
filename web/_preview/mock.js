/* mock.js — 预览用假数据（不属于产品代码，仅用于颜色/版式评审） */
(() => {
  const q = new URLSearchParams(location.search);
  const panel = q.get('panel') || 'conv';
  const state = q.get('state') || 'chat';

  const ICONS = {
    conv: '<path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z"/>',
    agents: '<path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/>',
    health: '<path d="M22 12h-4l-3 9L9 3l-3 9H2"/>',
    share: '<circle cx="18" cy="5" r="3"/><circle cx="6" cy="12" r="3"/><circle cx="18" cy="19" r="3"/><path d="M8.6 13.5l6.8 4M15.4 6.5l-6.8 4"/>',
    filter: '<path d="M22 3H2l8 9.5V19l4 2v-8.5z"/>',
    collapse: '<path d="M11 17l-5-5 5-5"/><path d="M18 17l-5-5 5-5"/>'
  };

  /* ---- 活动栏 ---- */
  const activity = document.getElementById('activity');
  if (activity) {
    const ids = ['conv', 'agents', 'health', 'share', 'filter'];
    activity.innerHTML = ids.map(k =>
      `<button class="act${k === panel ? ' on' : ''}" data-act="${k}" title="${k}" aria-label="${k}">
         <svg viewBox="0 0 24 24">${ICONS[k]}</svg></button>`).join('')
      + '<span class="spacer"></span>'
      + `<button class="act" data-act="collapse"><svg viewBox="0 0 24 24">${ICONS.collapse}</svg></button>`;
  }

  /* ---- 侧栏面板 ---- */
  document.querySelectorAll('.panel').forEach(p => p.classList.toggle('on', p.dataset.panel === panel));

  const cvList = document.getElementById('cvList');
  if (cvList) {
    const rows = [
      ['今天', '多智能体群聊窗口的美化方案', 1],
      ['今天', 'workbench 视图接 roundtable', 0],
      ['今天', '冷启动超时排查', 0],
      ['昨天', 'hybrid 路由改写', 0],
      ['10月28日', 'memory.db 迁移脚本', 0]
    ];
    let html = '';
    let group = '';
    rows.forEach(([g, t, on]) => {
      if (g !== group) { html += `<div class="cv-group">${g}</div>`; group = g; }
      html += `<div class="cv-item${on ? ' on' : ''}"><span class="t">${t}</span>${on ? '<span class="pin">★</span>' : ''}<button class="more">⋯</button></div>`;
    });
    cvList.innerHTML = html;
  }

  const agents = document.getElementById('agents');
  if (agents) {
    const list = [
      ['Claude', 'cli', 'on', 'claude-opus-4 · 已就绪', ''],
      ['Codex', 'cli', 'run', 'gpt-5-codex · 正在执行 step 3/4', ''],
      ['CodeBuddy', 'api', 'on', 'cb-large · 已就绪', ''],
      ['deepseek', 'api', 'idle', 'v4.1-flash · 空闲', '冷启动 42s · 预热中']
    ];
    agents.innerHTML = list.map(([name, kind, dot, meta, cold], i) => `
      <div class="box agent${i === 0 ? ' active' : ''}">
        <div class="row"><span class="dot ${dot}"></span><span class="name">${name}</span><span class="kind">${kind}</span></div>
        <div class="meta">${meta}${cold ? ` <span class="cold">${cold}</span>` : ''}</div>
        <div class="ops"><button>单发</button><button>设主代理</button><button class="danger">移除</button></div>
      </div>`).join('');
  }

  const dsp = document.getElementById('dispatchBox');
  if (dsp) {
    dsp.innerHTML = `<h3>执行顺序</h3><p class="dsp-note">发给「全体」时的先后，可上下调整。</p>
      ${['Claude', 'Codex', 'CodeBuddy'].map((n, i) => `<div class="dsp-item"><span class="ord">${i + 1}</span><span class="who">${n}</span><button>↑</button><button>↓</button></div>`).join('')}
      <div class="dsp-planner"><span>主代理</span><select><option>Claude</option><option>Codex</option></select></div>`;
  }

  const health = document.getElementById('health');
  if (health) {
    health.innerHTML = `<div class="kv"><span>hub</span><b>running · 12m</b></div>
      <div class="kv"><span>连接</span><b>ws · 3 clients</b></div>
      <div class="kv"><span>队列</span><b>2 待处理</b></div>
      <div class="kv"><span>今日 token</span><b>1,284,930</b></div>`;
  }

  /* ---- 主区消息流 ---- */
  const log = document.getElementById('log');
  // 注意：.msg 是 white-space:pre-wrap，模板里任何换行/缩进都会被渲染成空行，
  // 所以这里必须拼成单行字符串，不能为了好读而换行。
  const msg = (cls, who, meta, body, extra = '') =>
    `<div class="msg ${cls}"><div class="hd"><span class="who">${who}</span>${meta}<button title="改写">改写</button><button title="删除">删除</button></div><div class="body">${body}</div>${extra}</div>`;

  if (log) {
    const steps = '<div class="steps"><div class="tbody">'
      + '<div class="tstep"><span class="ico">✓</span><span class="lb">读取 web/index.html</span><span class="dur">0.4s</span></div>'
      + '<div class="tstep hasdetail"><span class="ico">✓</span><span class="lb">grep -n "border-left" web/index.html</span><span class="dur">0.2s</span></div>'
      + '<div class="tstep running"><span class="ico">◐</span><span class="lb">改写 20-chat.css</span><span class="dur">3.1s</span></div>'
      + '</div></div>';

    log.innerHTML = [
      msg('user', '我', '<span class="chip">Claude</span><span class="chip">Codex</span>',
        '把消息气泡的左色条做细一点，其余颜色不要动。顺便看下暗背景下的弱文字还读不读得清。'),
      msg('claude', 'Claude', '<span class="chip">opus-4</span><span class="tstat ok">完成 · 18s</span>',
        '左色条从 3px 收到 2px，并把身份色统一到同一明度；<b>#66718a</b> 这类三级灰在深底上只有 3.1:1，已提到 4.9:1。', steps),
      msg('codex', 'Codex', '<span class="chip">gpt-5-codex</span><span class="tstat warn">运行中</span>',
        '正在核对 status bar 的对比度。'),
      msg('model', 'deepseek', '<span class="chip">v4.1-flash</span><span class="tstat ok">完成 · 6s</span>',
        '补一条：diff 里的增删底色不要用纯红绿，降到 12% 透明度更稳。'),
      msg('error', 'CodeBuddy', '<span class="chip">cb-large</span><span class="tstat err">失败</span>',
        'HTTP 502：上游在执行第 3 步时超时。'),
      msg('dsh hl', 'deepseek', '<span class="chip">v4.1-flash</span><span class="star">★ 重要</span><span class="tstat ok">完成 · 6s</span>',
        '这条被标了「重要」：高亮气泡用金 wash + 金内描边，和普通完成态区分开。'),
      msg('perm', 'CodeBuddy', '<span class="chip">权限申请</span>',
        '<b>请求写入 web/theme/10-shell.css</b><div class="note">允许？拒绝？这一步需要人拍板，所以用金而不是红。</div>'),
      msg('streaming', 'Claude', '<span class="chip">opus-4</span><span class="chip typing">正在输入…</span>',
        '接下来我会把 composer 的发送键从纯蓝改成冰蓝主色，并在 hover 时…')
    ].join('');

    if (state === 'empty') log.innerHTML = '<div class="sys">还没有消息。选择成员后开始。</div>';
  }

  /* ---- 输入区 ---- */
  const targets = document.getElementById('targets');
  if (targets) {
    targets.innerHTML = ['全体', 'Claude', 'Codex', 'CodeBuddy', 'deepseek']
      .map((n, i) => `<button class="${i === 0 ? 'on' : ''}">${n}</button>`).join('');
  }
  const status = document.getElementById('status');
  if (status) {
    status.innerHTML = `<span class="st dot-green">已连接</span><span class="st">3 位成员</span>
      <span class="st">队列 2</span><span class="spacer"></span>
      <span class="st">第 3 轮 · 耗时 18.4s</span><span class="st dot-amber">1 条高亮</span>`;
  }

  /* ---- 工作台视图 ---- */
  if (state === 'wb') {
    const bar = document.createElement('div');
    bar.id = 'workbenchBar';
    bar.innerHTML = `<div class="wb-brand">ClaudeLink <span>0.4.0</span></div>
      <nav><button class="on">讨论</button><button>工具记录</button><button>推理摘要</button><button>持续执行</button><button>记忆</button></nav>
      <div class="wb-actions"><button>发起圆桌</button><button class="primary">创建执行任务</button></div>`;
    document.querySelector('main header').after(bar);
    const panelEl = document.createElement('section');
    panelEl.id = 'workbenchPanel';
    panelEl.innerHTML = `
      <div class="wb-section-head"><div><h2>一次调度，持续执行</h2><p>主代理只生成一次计划，执行代理按依赖持续工作并保存检查点。</p></div><button>创建任务</button></div>
      <article class="wb-card"><div class="wb-card-title">把日报改成站点<small>wf_8f21 · running</small></div>
        <div class="wb-meta">wf_8f21 · <span data-st="running">running</span> · 主代理 Claude</div>
        <div class="wb-task"><b>t1 · Codex</b><span data-st="done">done · 4 步</span><p>抽出 A4 版式的公共排版规则</p><pre>已生成 tokens 草案，等待评审。</pre></div>
        <div class="wb-task"><b>t2 · CodeBuddy</b><span data-st="error">error · 2 步</span><p>把灰条改为数据卡</p><pre>HTTP 502：上游超时</pre></div>
        <div class="wb-actions"><button>暂停</button></div>
      </article>
      <article class="wb-card"><div class="wb-card-title">核对配色对比度<small>wf_7c02 · paused</small></div>
        <div class="wb-meta">wf_7c02 · <span data-st="paused">paused</span> · 主代理 Codex</div>
        <p class="wb-error">上游 502：第 3 步超时</p>
      </article>`;
    bar.after(panelEl);
    const lg = document.getElementById('log');
    if (lg) lg.hidden = true;
    const cp = document.getElementById('composer');
    if (cp) cp.hidden = true;
  }

  /* ---- 对话框 ---- */
  if (state === 'dialog') {
    const mask = document.getElementById('mask');
    if (mask) {
      mask.classList.add('show');
      const dlg = mask.querySelector('.dialog');
      if (dlg) {
        dlg.innerHTML = `<h2>添加模型</h2>
          <div class="grid">
            <label>显示名</label><input value="deepseek">
            <label>类型</label><select><option>api</option><option>cli</option></select>
            <label>模型</label><input value="v4.1-flash">
            <label>接口地址</label><input value="https://api.deepseek.com/v1">
            <label>备注</label><textarea>按 token 计费，长上下文。</textarea>
          </div>
          <div class="note">密钥只保存在本机 config.json。</div>
          <div class="ops"><button>取消</button><button class="primary">保存</button></div>`;
      }
    }
  }
})();
