/* 真实浏览器验证：流式思维链块 / 折叠开关 / 执行流程里的思维链步骤 / JS 异常。

   前置（无头 Chrome 带调试端口）——Start-Process 单条执行，别和别的语句串联：
     Start-Process -FilePath 'C:\Program Files (x86)\Google\Chrome\Application\chrome.exe' `
       -ArgumentList '--headless=new','--disable-gpu','--no-sandbox',
       '--remote-debugging-port=9333','--user-data-dir=<临时目录>','about:blank' -WindowStyle Hidden

   用法：
     node tools/browser_verify.js http://127.0.0.1:8787 "提示词" [target] [采样间隔ms]
     node tools/browser_verify.js --inspect-only        # 只检查执行流程里的思维链步骤
     node tools/browser_verify.js --shutdown-test       # 真点一次「关闭」键，会把中枢进程关掉（测完自己重启）

   注意：脚本会往群里真投一条消息（有 token 成本）；--inspect-only 不投递。
*/
const http = require("http");

const ARG2 = process.argv[2] || "";
const DEFAULT_HUB = "http://127.0.0.1:8787";
const INSPECT_ONLY = ARG2 === "--inspect-only";
const SHUTDOWN_TEST = ARG2 === "--shutdown-test";
const HUB = ARG2.startsWith("--") ? DEFAULT_HUB : (ARG2 || DEFAULT_HUB);
const PROMPT = process.argv[3] || "只回复两个字：在线";
const TARGET = process.argv[4] || "49f01bdb";
const STEP_MS = parseInt(process.argv[5] || "2000", 10);
const PORT = 9333;

const sleep = ms => new Promise(r => setTimeout(r, ms));

function req(path, method) {
  return new Promise((resolve, reject) => {
    http.request({ host: "127.0.0.1", port: PORT, path, method: method || "GET" }, res => {
      let b = "";
      res.on("data", d => (b += d));
      res.on("end", () => { try { resolve(JSON.parse(b)); } catch (e) { reject(e); } });
    }).on("error", reject).end();
  });
}

async function main() {
  let tab;
  if (INSPECT_ONLY) {
    const list = await req("/json/list");
    tab = list.find(t => t.type === "page" && (t.url || "").includes("8787"));
    if (!tab) throw new Error("没有已打开的 hub 页签");
  } else {
    tab = await req("/json/new?" + encodeURIComponent(HUB + "/"), "PUT");
  }

  const ws = new WebSocket(tab.webSocketDebuggerUrl);
  let id = 0;
  const pending = new Map();
  const errors = [];
  const logs = [];
  ws.onmessage = ev => {
    const m = JSON.parse(ev.data);
    if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); return; }
    if (m.method === "Runtime.exceptionThrown") {
      errors.push(m.params.exceptionDetails.text + " " +
                  ((m.params.exceptionDetails.exception || {}).description || ""));
    }
    if (m.method === "Runtime.consoleAPICalled" && m.params.type === "error") {
      logs.push(m.params.args.map(a => a.value || a.description || "").join(" "));
    }
  };
  await new Promise(r => (ws.onopen = r));
  await send("Runtime.enable");
  await sleep(3000);

  function send(method, params) {
    return new Promise(res => {
      const mid = ++id;
      pending.set(mid, res);
      ws.send(JSON.stringify({ id: mid, method, params: params || {} }));
    });
  }
  const evalJs = async expr => {
    const r = await send("Runtime.evaluate", { expression: expr, returnByValue: true, awaitPromise: true });
    if (r.result && r.result.exceptionDetails) return "EXC: " + r.result.exceptionDetails.text;
    return r.result && r.result.result ? r.result.result.value : undefined;
  };

  // 有些 Chrome 版本会忽略 /json/new?url=，那就显式导航一次，别在 about:blank 上瞎测
  const href = await evalJs("location.href");
  if (!String(href || "").startsWith(HUB)) {
    await send("Page.enable");
    await send("Page.navigate", { url: HUB + "/" });
    await sleep(2500);
  }

  if (SHUTDOWN_TEST) {
    console.log("页面标题:", await evalJs("document.title"));
    console.log("关闭键存在:", await evalJs("!!document.getElementById('shutdown')"));
    console.log("点前 连接状态:", await evalJs("document.getElementById('conn').textContent"));
    await evalJs("window.confirm = () => true");            // 无头浏览器里 confirm 默认返回 false
    await evalJs("document.getElementById('shutdown').click()");
    await sleep(2500);
    console.log("点后 连接状态:", await evalJs("document.getElementById('conn').textContent"));
    console.log("点后 顶部提示:", await evalJs("document.getElementById('banner').textContent.trim()"));
    console.log("点后 发送键禁用:", await evalJs("document.getElementById('send').disabled"));
    console.log("点后 页内提示:", await evalJs(`[...document.querySelectorAll('.sys')]
      .map(e=>e.textContent.trim()).filter(t=>t.includes('关闭')).slice(-1)`));
    console.log("提示里的重连按钮已移除:", await evalJs("!document.getElementById('btnReconnect2')"));
    ws.close();
    return;
  }

  if (!INSPECT_ONLY) {
    console.log("页面标题:", await evalJs("document.title"));
    console.log("连接状态:", await evalJs("document.getElementById('conn').textContent"));
    await evalJs(`fetch('/api/send',{method:'POST',headers:{'Content-Type':'application/json'},` +
                 `body:JSON.stringify({text:${JSON.stringify(PROMPT)},target:${JSON.stringify(TARGET)}})}).then(r=>r.json())`);
    console.log("已投递提示词，开始采样（流式卡 = 思维链 + 正文两条通道）：");

    let sawThink = 0, maxThink = 0, toggle = null;
    for (let i = 0; i < 30; i++) {
      await sleep(STEP_MS);
      const snap = await evalJs(`(()=>{const n=document.querySelector('[data-stream]');
        const b=n&&n.querySelector('.thinkbody'); const l=n&&n.querySelector('.body');
        return {stream:!!n, think:b?b.textContent.length:0, live:l?l.textContent.length:0,
                scrollable: b? (b.parentElement.scrollHeight > b.parentElement.clientHeight) : false,
                toggle: !!(n&&n.querySelector('.thtoggle'))};})()`);
      if (typeof snap === "string") { console.log("  采样异常:", snap); break; }
      if (snap && snap.think) { sawThink++; maxThink = Math.max(maxThink, snap.think); }
      if (snap && (snap.stream || snap.think)) {
        console.log(`  t=${((i + 1) * STEP_MS / 1000).toFixed(1)}s 流式卡=${snap.stream} ` +
                    `思维链=${snap.think}字 正文=${snap.live}字 思维链可滚动=${snap.scrollable} 折叠钮=${snap.toggle}`);
      }
      if (!toggle && snap && snap.think > 300) {
        toggle = await evalJs(`(()=>{const n=document.querySelector('[data-stream]');
          const t=n&&n.querySelector('.thtoggle'); if(!t) return 'no-toggle';
          const box=n.querySelector('.thinkbox'); const h0=Math.round(box.getBoundingClientRect().height);
          t.click(); const c=box.classList.contains('collapsed'); const h1=Math.round(box.getBoundingClientRect().height);
          t.click(); const h2=Math.round(box.getBoundingClientRect().height);
          return {展开高:h0, 点一下后collapsed:c, 折叠高:h1, 再点后高:h2, 按钮文字:t.textContent.trim()};})()`);
      }
      if (sawThink && !snap.stream) break;
    }
    console.log("思维链采样次数:", sawThink, "最大长度:", maxThink);
    console.log("折叠开关（流式中实测）:", JSON.stringify(toggle));
  }

  console.log("执行流程里的思维链步骤标签:", await evalJs(`[...document.querySelectorAll('.tstep')]
    .map(e=>e.firstElementChild&&e.firstElementChild.nextElementSibling?e.firstElementChild.nextElementSibling.textContent.trim():'')
    .filter(t=>t.includes('思考')).slice(-3)`));
  console.log("其中的思维链块:", await evalJs(`(()=>{const row=[...document.querySelectorAll('.tstep')].reverse()
      .find(e=>e.textContent.includes('思考')&&e.classList.contains('hasdetail'));
    if(!row) return 'no-row'; row.click();
    const box=row.querySelector('.thinkbox'); const tb=row.querySelector('.thinkbody');
    return {展开:row.classList.contains('open'), 字数:tb?tb.textContent.length:0,
            块高:box?Math.round(box.getBoundingClientRect().height):0,
            内容高:box?box.scrollHeight:0, 可滚动:box?box.scrollHeight>box.clientHeight:false};})()`));
  console.log("JS 异常:", errors.length ? errors : "无");
  console.log("console.error:", logs.length ? logs : "无");
  ws.close();
}

main().catch(e => { console.error("FAILED:", e.message); process.exit(1); });
