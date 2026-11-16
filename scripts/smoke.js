#!/usr/bin/env node
"use strict";
/**
 * 冒烟测试（npm run smoke）：在**临时目录**里模拟一次真实安装后的运行。
 *
 * 步骤：
 *   1. 按 package.json 的 files 白名单把要发布的文件拷到临时目录
 *   2. 在临时数据目录写一份 config（从 config.example.json 改端口为空闲端口）
 *   3. `node bin/cli.js start --no-open --detach`
 *   4. 轮询 /api/health；断言 200；断言数据文件落在数据目录、**包目录保持干净**
 *   5. `stop`；清理
 *
 * 全程只碰系统临时目录。若本机可用 Agent < 2（中枢拒绝启动），以 SKIP（退出码 0）结束。
 */
const fs = require("fs");
const os = require("os");
const path = require("path");
const net = require("net");
const http = require("http");
const cp = require("child_process");

const ROOT = path.resolve(__dirname, "..");
const pkg = require(path.join(ROOT, "package.json"));
const TMP = fs.mkdtempSync(path.join(os.tmpdir(), "clh-smoke-"));

let failures = 0;
const check = (name, cond, detail) => {
  if (cond) console.log("  ✓ " + name);
  else { failures++; console.log("  ✗ " + name + (detail ? "  → " + detail : "")); }
};

function walkCopy(entry, destRoot) {
  const src = path.join(ROOT, entry);
  const dst = path.join(destRoot, entry);
  let st;
  try { st = fs.statSync(src); } catch { return; }
  if (st.isDirectory()) {
    fs.mkdirSync(dst, { recursive: true });
    for (const name of fs.readdirSync(src)) {
      if (name === "node_modules" || name === "__pycache__" || name.startsWith(".")) continue;
      walkCopy(path.join(entry, name), destRoot);
    }
  } else if (st.isFile()) {
    fs.mkdirSync(path.dirname(dst), { recursive: true });
    fs.copyFileSync(src, dst);
  }
}

function freePort() {
  return new Promise((resolve, reject) => {
    const srv = net.createServer();
    srv.once("error", reject);
    srv.listen(0, "127.0.0.1", () => {
      const p = srv.address().port;
      srv.close(() => resolve(p));
    });
  });
}

function health(port) {
  return new Promise((resolve) => {
    const req = http.get({ host: "127.0.0.1", port, path: "/api/health", timeout: 1200 }, (res) => {
      res.resume();
      resolve(res.statusCode === 200);
    });
    req.on("timeout", () => { req.destroy(); resolve(false); });
    req.on("error", () => resolve(false));
  });
}

function requestRaw(port, method, urlPath, body, headers) {
  return new Promise((resolve) => {
    const payload = body == null ? null : String(body);
    const req = http.request({
      host: "127.0.0.1", port, path: urlPath, method, timeout: 5000,
      headers: Object.assign(payload == null ? {} : { "Content-Length": Buffer.byteLength(payload) }, headers || {}),
    }, (res) => {
      let text = "";
      res.setEncoding("utf8");
      res.on("data", (c) => { text += c; });
      res.on("end", () => resolve({ status: res.statusCode, headers: res.headers, text }));
    });
    req.on("timeout", () => { req.destroy(); resolve({ status: 0, headers: {}, text: "" }); });
    req.on("error", () => resolve({ status: 0, headers: {}, text: "" }));
    if (payload != null) req.write(payload);
    req.end();
  });
}

async function postJson(port, urlPath, payload, headers) {
  const r = await requestRaw(port, "POST", urlPath, JSON.stringify(payload || {}),
    Object.assign({ "Content-Type": "application/json" }, headers || {}));
  try { return JSON.parse(r.text); } catch { return null; }
}

function readToken(home) {
  try { return fs.readFileSync(path.join(home, "hub.token"), "utf8").trim(); }
  catch { return ""; }
}

(async () => {
  const pkgDir = path.join(TMP, "pkg");
  const home = path.join(TMP, "home");
  fs.mkdirSync(pkgDir, { recursive: true });
  fs.mkdirSync(home, { recursive: true });

  console.log("== 1) 拷贝发布清单到临时目录 ==");
  for (const entry of pkg.files) walkCopy(entry, pkgDir);
  walkCopy("package.json", pkgDir); // npm includes this implicitly, outside the files whitelist.
  check("hub.py 已拷贝", fs.existsSync(path.join(pkgDir, "hub.py")));
  check("extract.py 已拷贝（H4）", fs.existsSync(path.join(pkgDir, "extract.py")));
  check("bin/cli.js 已拷贝", fs.existsSync(path.join(pkgDir, "bin", "cli.js")));

  const port = await freePort();
  const example = JSON.parse(fs.readFileSync(path.join(pkgDir, "config.example.json"), "utf8"));
  example.server = Object.assign({}, example.server, { host: "127.0.0.1", port, allow_shutdown: true });
  fs.writeFileSync(path.join(home, "config.json"), JSON.stringify(example, null, 2));
  console.log("== 2) 临时数据目录：" + home + " | 端口 " + port + " ==");

  const env = Object.assign({}, process.env, {
    CLAUDELINK_HOME: home,
    CLAUDELINK_CONFIG: path.join(home, "config.json"),
    CLAUDELINK_AGENTS: path.join(home, "agents.detected.json"),
    CLAUDELINK_RUNTIME_DIR: path.join(home, "instances"),
  });

  console.log("== 3) start --no-open --detach ==");
  const started = cp.spawnSync(process.execPath, [path.join(pkgDir, "bin", "cli.js"), "start", "--no-open", "--no-setup", "--detach"], {
    cwd: pkgDir, env, encoding: "utf8", timeout: 90000,
  });
  const out = (started.stdout || "") + (started.stderr || "");
  if (/没有识别到AIAgent|可用 Agent 少于 2/.test(out)) {
    console.log("  … 本机可用 Agent 少于 2 个 —— 跳过（中枢按设计不启动）");
    console.log(out.trim().split("\n").slice(-3).join("\n"));
    fs.rmSync(TMP, { recursive: true, force: true });
    process.exitCode = 0;
    return;
  }
  if (started.status !== 0) {
    console.error(out);
    check("start 退出码为 0", false);
  }

  console.log("== 4) 等 /api/health ==");
  let ok = false;
  for (let i = 0; i < 60 && !ok; i++) {
    ok = await health(port);
    if (!ok) await new Promise((r) => setTimeout(r, 500));
  }
  check("/api/health 返回 200", ok);
  if (ok) {
    const tui = cp.spawnSync(process.execPath, [path.join(pkgDir, "bin", "cli.js"), "tui", "--check"], {
      cwd: pkgDir, env, encoding: "utf8", timeout: 15000,
    });
    check("npm 终端入口 tui --check 完成鉴权", tui.status === 0 && /"authenticated": true/.test(tui.stdout), tui.stderr);
    const rendered = cp.spawnSync(process.execPath, [path.join(pkgDir, "bin", "cli.js"), "tui", "--render-once", "--no-start"], {
      cwd: pkgDir, env, encoding: "utf8", timeout: 15000,
    });
    check("安装目录 TUI 完成状态读取与首帧渲染", rendered.status === 0 && /ClaudeLink/.test(rendered.stdout) && !/Traceback/.test(rendered.stderr), rendered.stderr);
    const token = readToken(home);
    check("token 文件已生成", token.length >= 20);
    const stateNo = await requestRaw(port, "GET", "/api/state");
    check("无 token GET /api/state → 401", stateNo.status === 401, String(stateNo.status));
    const stateOk = await requestRaw(port, "GET", "/api/state", null, { "X-ClaudeLink-Token": token });
    check("带 token GET /api/state → 200", stateOk.status === 200, String(stateOk.status));
    const relayNo = await requestRaw(port, "POST", "/api/relay", "{}", { "Content-Type": "application/json" });
    check("无 token POST /api/relay → 401", relayNo.status === 401, String(relayNo.status));
    const evilOrigin = await requestRaw(port, "POST", "/api/relay", "{}", {
      "Content-Type": "application/json", "X-ClaudeLink-Token": token, "Origin": "http://evil.com",
    });
    check("Origin: evil.com → 403", evilOrigin.status === 403, String(evilOrigin.status));
    const evilHost = await requestRaw(port, "POST", "/api/relay", "{}", {
      "Content-Type": "application/json", "X-ClaudeLink-Token": token, "Host": "evil.com",
    });
    check("Host: evil.com → 403", evilHost.status === 403, String(evilHost.status));
    const plain = await requestRaw(port, "POST", "/api/relay", "{}", {
      "Content-Type": "text/plain", "X-ClaudeLink-Token": token,
    });
    check("text/plain POST → 415", plain.status === 415, String(plain.status));
    const root = await requestRaw(port, "GET", "/");
    const cookie = (root.headers["set-cookie"] || []).join(";");
    check("根页面注入了 token 且设置 cookie",
      root.status === 200 && !root.text.includes("__CLAUDELINK_TOKEN__") && cookie.includes("hub_token="),
      String(root.status));
    const script = [...root.text.matchAll(/<script[^>]*>([\s\S]*?)<\/script>/g)].map(m=>m[1]).join("\n");
    const syntax = cp.spawnSync(process.execPath,["--check"],{input:script,encoding:"utf8",timeout:5000});
    check("服务端注入后的网页 JavaScript 语法正确",syntax.status===0);
    const jsAsset = await requestRaw(port,"GET","/assets/workbench.js");
    const cssAsset = await requestRaw(port,"GET","/assets/workbench.css");
    check("新版 Web 工作台脚本与样式可读取",jsAsset.status===200&&cssAsset.status===200);
    check("hub.db 落在数据目录", fs.existsSync(path.join(home, "hub.db")));
    check("--no-setup 不重建检测缓存", !fs.existsSync(path.join(home, "agents.detected.json")));
    check("hub.log 落在数据目录", fs.existsSync(path.join(home, "hub.log")));
    // 包目录里不该出现任何"运行时产物"（config/db/日志/上传/临时文件）
    const dirty = ["hub.db", "hub.db-wal", "memory.db", "memory.db-wal", "config.json", "hub.log", "hub.errors.log",
                   "hub.pids.json", "agents.detected.json", "uploads"]
      .filter((n) => fs.existsSync(path.join(pkgDir, n)));
    const tmpLeft = fs.readdirSync(pkgDir).filter((n) => n.startsWith("config.json.tmp"));
    check("**包目录未被写脏**", dirty.length === 0 && tmpLeft.length === 0,
      (dirty.concat(tmpLeft)).join("、"));
  } else {
    const log = path.join(home, "hub.log");
    if (fs.existsSync(log)) console.error(fs.readFileSync(log, "utf8").split(/\r?\n/).slice(-15).join("\n"));
  }

  console.log("== 4b) hub 保存不覆盖 CLI 改动（同一把文件锁 + 三方合并）==");
  // 模拟「hub 运行期间 CLI 改了配置」：直接往磁盘写一个 hub 不认识的键，
  // 再触发一次 hub 自己的保存（POST /api/relay → save_config）。
  const cfgFile = path.join(home, "config.json");
  try {
    const before = JSON.parse(fs.readFileSync(cfgFile, "utf8"));
    before.__cli_marker = { by: "cli", at: Date.now() };
    fs.writeFileSync(cfgFile, JSON.stringify(before, null, 2) + "\n");
    const posted = await postJson(port, "/api/relay", { enabled: false },
      { "X-ClaudeLink-Token": readToken(home) });
    check("POST /api/relay 成功", !!posted && posted.ok === true, JSON.stringify(posted));
    const after = JSON.parse(fs.readFileSync(cfgFile, "utf8"));
    check("CLI 写的键没被 hub 整份覆盖", !!after.__cli_marker && after.__cli_marker.by === "cli", JSON.stringify(after.__cli_marker || null));
    check("hub 自己的改动照样落盘", !!after.relay && after.relay.enabled === false, JSON.stringify(after.relay || null));
    check("没有留下锁目录", !fs.readdirSync(home).some((n) => n.endsWith(".lock")));
  } catch (err) {
    check("hub 保存回归用例可执行", false, err.message);
  }

  console.log("== 5) stop ==");
  const stopped = cp.spawnSync(process.execPath, [path.join(pkgDir, "bin", "cli.js"), "stop"], { cwd: pkgDir, env, encoding: "utf8", timeout: 30000 });
  check("stop 退出码为 0", stopped.status === 0, (stopped.stderr || "").trim());
  await new Promise((r) => setTimeout(r, 800));
  check("停止后 health 不再 200", !(await health(port)));

  console.log("== 6) setup --attach dry-run（默认零副作用）==");
  // 用空的 CLAUDE_CONFIG_DIR / CODEX_HOME 跑，让 claude/codex 一定处于"未接入"，
  // 这样断言与这台机器上是否已手工接入过无关。
  const attachEnv = Object.assign({}, env, {
    CLAUDE_CONFIG_DIR: path.join(TMP, "claude-config"),
    CODEX_HOME: path.join(TMP, "codex-home"),
  });
  fs.mkdirSync(attachEnv.CLAUDE_CONFIG_DIR, { recursive: true });
  fs.mkdirSync(attachEnv.CODEX_HOME, { recursive: true });
  const attachRun = cp.spawnSync(process.execPath, [path.join(pkgDir, "bin", "cli.js"), "setup", "--attach", "--json"], {
    cwd: pkgDir, env: attachEnv, encoding: "utf8", timeout: 240000,
  });
  let plan = null;
  try { plan = JSON.parse(attachRun.stdout || ""); } catch { /* 下面统一报 */ }
  check("attach dry-run 退出码为 0", attachRun.status === 0, (attachRun.stderr || "").trim().slice(0, 200));
  check("attach dry-run 输出可解析（--json）", !!plan, (attachRun.stdout || "").slice(0, 160));
  if (plan) {
    const agents = plan.agents || [];
    check("默认不执行（applied=false）", plan.applied === false);
    // 「每个 Agent 的待执行命令」：pending 的必须给出完整命令，且可执行文件是绝对路径
    const pending = agents.filter((a) => a.state === "pending");
    check("至少有一个待执行的 Agent", pending.length >= 1, JSON.stringify(agents.map((a) => a.id + ":" + a.state)));
    const notAbsolute = pending.filter((a) => !a.cmd || !path.isAbsolute(a.cmd[0]) || !fs.existsSync(a.cmd[0]));
    check("待执行命令都用解析出的绝对路径", notAbsolute.length === 0,
      notAbsolute.map((a) => a.id + " → " + (a.cmd && a.cmd[0])).join("；"));
    const shown = pending.filter((a) => !a.display || a.display.indexOf(a.cmd[0]) < 0);
    check("展示串里带着同一条命令", shown.length === 0, shown.map((a) => a.id).join("、"));
    // codex 在本机不在 PATH（只能靠 scan-dir 找到）→ 必须显示绝对路径而不是裸名
    const codex = agents.find((a) => a.id === "codex");
    if (codex && codex.state !== "missing") {
      check("codex 用 detect 解析出的绝对路径", !!codex.cmd && path.isAbsolute(codex.cmd[0]) && fs.existsSync(codex.cmd[0]),
        JSON.stringify(codex.cmd && codex.cmd.slice(0, 2)));
    }
    const unknown = agents.filter((a) => a.state === "unknown");
    check("没有把超时/失败误判成未接入", unknown.every((a) => /未知/.test(a.note || "")), JSON.stringify(unknown.map((a) => a.id)));
  }
  check("dry-run 后没有 attach-state.json", !fs.existsSync(path.join(home, "attach-state.json")),
    path.join(home, "attach-state.json"));

  cleanup();
  console.log("\n" + (failures ? "✗ 冒烟失败 " + failures + " 项" : "✅ 冒烟通过"));
  process.exitCode = failures ? 1 : 0;
})().catch((err) => {
  console.error(err && err.stack || err);
  cleanup();
  process.exitCode = 1;
});

/** 兜底清理：先尽量 stop（别留后台 hub 占着 hub.db/log），再删临时目录。
 *  Windows 上文件句柄释放有延迟，rmSync 用重试。 */
function cleanup() {
  try {
    const pkgDir = path.join(TMP, "pkg");
    const cli = path.join(pkgDir, "bin", "cli.js");
    if (fs.existsSync(cli)) {
      cp.spawnSync(process.execPath, [cli, "stop"], {
        cwd: pkgDir, encoding: "utf8", timeout: 20000,
        env: Object.assign({}, process.env, {
          CLAUDELINK_HOME: path.join(TMP, "home"),
          CLAUDELINK_CONFIG: path.join(TMP, "home", "config.json"),
        }),
      });
    }
  } catch { /* ignore */ }
  try { fs.rmSync(TMP, { recursive: true, force: true, maxRetries: 6, retryDelay: 250 }); } catch { /* ignore */ }
}
process.on("exit", cleanup);
