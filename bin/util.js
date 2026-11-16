"use strict";
/**
 * 跨平台工具层。**本文件禁止出现任何机器专属绝对路径**（Node.js / Python 装在哪个盘都不写死）。
 * 一切路径都从「环境变量 / npm 全局前缀 / 用户目录 / 系统 PATH」在运行时推导。
 */
const fs = require("fs");
const path = require("path");
const os = require("os");
const cp = require("child_process");
const http = require("http");

const IS_WIN = process.platform === "win32";

/** 展开 %VAR% / $VAR / ~，未定义的变量原样保留（便于上层判断"没配"）。 */
function expand(value, env = process.env) {
  if (!value) return "";
  let out = String(value);
  out = out.replace(/%([^%]+)%/g, (m, key) => {
    const hit = env[key] || env[key.toUpperCase()] || env[key.toLowerCase()];
    return hit === undefined ? m : hit;
  });
  out = out.replace(/\$\{([^}]+)\}|\$([A-Za-z_][A-Za-z0-9_]*)/g, (m, a, b) => {
    const key = a || b;
    const hit = env[key];
    return hit === undefined ? m : hit;
  });
  if (out === "~" || out.startsWith("~/") || out.startsWith("~\\")) {
    out = path.join(os.homedir(), out.slice(1));
  }
  return out;
}

/** 展开后仍含未解析占位符 → 视为"没配置"，不要当成路径用。 */
function unresolved(value) {
  return /%[^%]+%|\$\{?[A-Za-z_]/.test(String(value || ""));
}

function isFile(p) {
  try { return fs.statSync(p).isFile(); } catch { return false; }
}

function isDir(p) {
  try { return fs.statSync(p).isDirectory(); } catch { return false; }
}

/**
 * 执行外部命令。Windows 上 `.cmd/.bat`（以及 npm）无法被直接 spawn（ENOENT），
 * 必须回退到 shell —— 这是"npm 全局根识别不出来"的根因。
 */
function run(cmd, args, opts = {}) {
  const base = {
    encoding: "utf8",
    timeout: opts.timeout || 5000,
    windowsHide: true,
    cwd: opts.cwd,
    env: opts.env,
  };
  let res = cp.spawnSync(cmd, args || [], base);
  const needShell = IS_WIN && (
    /\.(cmd|bat)$/i.test(cmd) ||
    (res.error && (res.error.code === "ENOENT" || res.error.code === "EACCES"))
  );
  if (needShell) {
    // 不用 shell+args（Node 会告警 DEP0190，且不转义）：自己拼一条命令行再交给 shell
    const line = [cmd, ...(args || [])].map(quoteCmd).join(" ");
    res = cp.spawnSync(line, Object.assign({}, base, { shell: true }));
  }
  const out = (res.stdout || "") + (res.stderr || "");
  return { ok: res.status === 0, status: res.status, out, error: res.error };
}

/** cmd.exe 下的最小引号处理（只用于 npm/py 这类简单参数）。 */
function quoteCmd(s) {
  const v = String(s == null ? "" : s);
  if (!v) return '""';
  return /[\s"&|<>^]/.test(v) ? '"' + v.replace(/"/g, '""') + '"' : v;
}

/** 可执行文件候选名（Windows 需要扩展名变体）。 */
function binVariants(name) {
  const base = String(name || "");
  if (!base) return [];
  if (!IS_WIN) return [base];
  if (/\.(exe|cmd|bat|ps1)$/i.test(base)) return [base];
  return [base + ".exe", base + ".cmd", base + ".bat", base];
}

/** PATH 查找（不用 `where/which`，避免依赖外部命令与编码问题）。 */
function findOnPath(name, env = process.env) {
  const dirs = String(env.PATH || env.Path || "").split(IS_WIN ? ";" : ":").filter(Boolean);
  for (const d of dirs) {
    for (const v of binVariants(name)) {
      const p = path.join(d, v);
      if (isFile(p)) return p;
    }
  }
  return "";
}

function which(name) {
  if (!name) return "";
  const hit = findOnPath(name);
  if (hit) return hit;
  // 兜底：有些环境只装了系统查找工具
  const res = run(IS_WIN ? "where" : "which", [name], { timeout: 4000 });
  if (!res.ok) return "";
  return res.out.split(/\r?\n/).map((s) => s.trim()).find(Boolean) || "";
}

/**
 * ── 并发保护原语（多进程同时 setup / attach / env 时不让写操作互相覆盖）──
 *
 * 背景：cli 的 setup/start、attach 的账本、env 的变量文件都是「读 → 改 → 写」。
 * 两个进程同时跑时，后写的那个会拿着旧快照覆盖前一个的改动（丢更新）。
 * 做法：目录锁（mkdir 原子）串行化 + 写前内容比对（CAS）兜住「不拿我们锁的写者」
 *       —— 例如中枢 Web UI 直接改 config.json。锁只圈住写操作，绝不跨外部命令持有。
 */

function sleepSync(ms) {
  try {
    Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, Math.max(0, ms | 0));
  } catch {
    const end = Date.now() + ms;
    while (Date.now() < end) { /* 极端环境兜底忙等 */ }
  }
}

/** 锁目录：与目标文件同目录（保证同一卷、可原子 rename/mkdir）。 */
function lockDirFor(file) {
  return path.join(path.dirname(file), "." + path.basename(file) + ".lock");
}

function pidAlive(pid) {
  if (!pid) return false;
  try { process.kill(pid, 0); return true; }
  catch (err) { return err.code === "EPERM"; }   // 存在但没权限 → 视为活着
}

function lockOwner(lockDir) {
  try {
    const owner = readJson(path.join(lockDir, "owner.json"), null);
    if (owner && owner.pid) return owner;
  } catch { /* ignore */ }
  return null;
}

/**
 * 同步目录锁：fs.mkdirSync 在 Windows / POSIX 上都是原子的（已存在则 EEXIST）。
 * opts: { timeoutMs=5000, staleMs=30000, attempts=... }
 * 残留锁回收：owner 的 pid 已死，或超过 staleMs（临界区只做读写，正常 <10ms）。
 * 环境变量 CLAUDELINK_NO_LOCK=1 可整体关掉（应急排障用）。
 */
function withFileLock(file, fn, opts = {}) {
  if (process.env.CLAUDELINK_NO_LOCK === "1") return fn();
  const lockDir = lockDirFor(file);
  // 目标文件的父目录可能还不存在（首次 setup / 自定义 CLAUDELINK_HOME）
  try { fs.mkdirSync(path.dirname(lockDir), { recursive: true }); } catch { /* 交给下面的 mkdir 报错 */ }
  const timeoutMs = opts.timeoutMs != null ? opts.timeoutMs : 5000;
  const staleMs = opts.staleMs != null ? opts.staleMs : 30000;
  const started = Date.now();
  let acquired = false;
  let held = "";

  while (Date.now() - started <= timeoutMs) {
    try {
      fs.mkdirSync(lockDir);
      acquired = true;
      break;
    } catch (err) {
      if (err.code !== "EEXIST") throw err;
    }
    const owner = lockOwner(lockDir);
    let age;
    if (owner && owner.at) age = Date.now() - Date.parse(owner.at);
    else {
      try { age = Date.now() - fs.statSync(lockDir).mtimeMs; } catch { age = 0; }
    }
    const dead = owner && owner.pid && !pidAlive(owner.pid);
    if (dead || age > staleMs) {
      // 上一个持有者崩了：清掉再抢
      try { fs.rmSync(lockDir, { recursive: true, force: true }); } catch { /* 别人先清了 */ }
      continue;
    }
    held = owner && owner.pid ? "pid " + owner.pid + "，" + (age / 1000).toFixed(1) + "s" : (age / 1000).toFixed(1) + "s";
    sleepSync(40 + Math.floor(Math.random() * 40));
  }

  if (!acquired) {
    const err = new Error("等待文件锁超时：" + lockDir + (held ? "（持有者 " + held + "）" : "") +
      "；另一个 chorum 进程正在写同一份配置。稍后重试；确认无进程在跑时可删除该目录");
    err.code = "LOCK_TIMEOUT";
    throw err;
  }
  try {
    try {
      fs.writeFileSync(path.join(lockDir, "owner.json"),
        JSON.stringify({ pid: process.pid, host: os.hostname(), at: new Date().toISOString() }), "utf8");
    } catch { /* 写不了 owner 也能跑，回收会用 mtime 判定 */ }
    return fn();
  } finally {
    try { fs.rmSync(lockDir, { recursive: true, force: true }); } catch { /* ignore */ }
  }
}

/** 读文件快照（文本 + 解析结果）；解析失败时 text 保留、data 为 null。 */
function readTextSnapshot(file) {
  try {
    const text = fs.readFileSync(file, "utf8");
    let data = null;
    try { data = JSON.parse(text); } catch { data = null; }
    return { exists: true, text, data };
  } catch (err) {
    if (err.code === "ENOENT") return { exists: false, text: "", data: null };
    throw err;
  }
}

/**
 * 锁内「读 → mutate → 原子写」，写前用文本 CAS 兜住不拿锁的写者。
 * mutate(data, snapshot) 返回新对象；返回 null/undefined 表示"别写"。
 * 冲突（期间文件被外部改过）→ 重读重算，最多 attempts 次。
 * 返回 { ok, attempts, unchanged?, skipped?, reason? }
 */
function updateJsonLocked(file, mutate, opts = {}) {
  const attempts = opts.attempts || 3;
  try {
    return withFileLock(file, () => {
      for (let i = 0; i < attempts; i++) {
        const snap = readTextSnapshot(file);
        const next = mutate(snap.data, snap);
        if (next === undefined || next === null) return { ok: true, skipped: true, attempts: i };
        const text = JSON.stringify(next, null, 2) + "\n";
        if (snap.exists && snap.text === text) return { ok: true, unchanged: true, attempts: i, text };
        const now = readTextSnapshot(file);          // 写前最后确认：没被外部改过
        if (now.text !== snap.text) { sleepSync(10); continue; }
        if (opts.onBeforeWrite) opts.onBeforeWrite(snap);
        writeJsonAtomic(file, next);
        return { ok: true, attempts: i, text };
      }
      return { ok: false, attempts, reason: "conflict" };
    }, opts);
  } catch (err) {
    if (err && err.code === "LOCK_TIMEOUT") return { ok: false, reason: "lock", error: err };
    throw err;
  }
}

/**
 * 运行时数据目录（config / hub.db / 日志 / attach-state.json）。
 * - 包目录可写（本地 clone / 开发）→ 沿用包目录（向后兼容）
 * - 系统级 npm 全局安装（只读）→ 落到 ~/.claudelink-hub
 * 可用 CLAUDELINK_HOME 覆盖。**不写死任何机器路径**。
 */
function dataDir(pkgRoot) {
  if (process.env.CLAUDELINK_HOME) return path.resolve(expand(process.env.CLAUDELINK_HOME));
  if (process.env.CLAUDELINK_CONFIG) return path.dirname(path.resolve(expand(process.env.CLAUDELINK_CONFIG)));
  const installed = path.resolve(pkgRoot).split(path.sep).includes("node_modules");
  if (installed) {
    const registry = process.env.CLAUDELINK_RUNTIME_DIR || path.join(os.homedir(), ".claudelink", "instances");
    try {
      const record=JSON.parse(fs.readFileSync(path.join(registry,"default.json"),"utf8"));
      if (path.isAbsolute(record.data_dir) && fs.existsSync(record.config_path)) return record.data_dir;
    } catch {}
    if (!fs.existsSync(path.join(pkgRoot,"config.json"))) {
      return path.join(os.homedir(),".claudelink-hub");
    }
  }
  try {
    fs.accessSync(pkgRoot, fs.constants.W_OK);
    return path.resolve(pkgRoot);
  } catch {
    const d = path.join(os.homedir(), ".claudelink-hub");
    try { fs.mkdirSync(d, { recursive: true }); } catch { /* 交给后续报错 */ }
    return d;
  }
}

let _npmPrefixCache = null;

/** npm 全局前缀（跨平台；Windows 上必须经 shell 才能跑 npm.cmd）。结果缓存，避免反复 spawn。 */
function npmPrefix() {
  if (_npmPrefixCache !== null) return _npmPrefixCache;
  if (process.env.CLAUDELINK_NPM_PREFIX) return (_npmPrefixCache = expand(process.env.CLAUDELINK_NPM_PREFIX));
  const fromEnv = process.env.npm_config_prefix || process.env.NPM_CONFIG_PREFIX;
  if (fromEnv) return (_npmPrefixCache = fromEnv);
  const res = run("npm", ["prefix", "-g"], { timeout: 8000 });
  if (res.ok) {
    const line = res.out.split(/\r?\n/).map((s) => s.trim()).filter(Boolean).pop() || "";
    if (line && isDir(line)) return (_npmPrefixCache = line);
  }
  return (_npmPrefixCache = "");
}

function npmGlobalRoot() {
  if (process.env.CLAUDELINK_NPM_ROOT) return process.env.CLAUDELINK_NPM_ROOT;
  const prefix = npmPrefix();
  if (prefix) {
    const root = IS_WIN ? path.join(prefix, "node_modules") : path.join(prefix, "lib", "node_modules");
    if (isDir(root)) return root;
  }
  const fallback = IS_WIN
    ? path.join(process.env.APPDATA || os.homedir(), "npm", "node_modules")
    : path.join(os.homedir(), ".npm-global", "lib", "node_modules");
  return isDir(fallback) ? fallback : "";
}

/**
 * 候选搜索根：**全部运行时推导**，不含机器字面量。
 * 顺序 = 越"用户自己的"越靠前（用户装的东西优先于系统）。
 */
function searchRoots() {
  const roots = [];
  const push = (p) => {
    if (!p) return;
    const v = expand(p);
    if (!v || unresolved(v)) return;
    if (!roots.some((x) => path.resolve(x) === path.resolve(v))) roots.push(v);
  };

  const prefix = npmPrefix();
  if (prefix) push(IS_WIN ? prefix : path.join(prefix, "bin"));
  push(IS_WIN ? "%APPDATA%/npm" : "~/.npm-global/bin");
  push("~/.local/bin");
  push("~/.bun/bin");
  push("~/.volta/bin");
  push("~/.cargo/bin");
  push("~/.deno/bin");

  if (IS_WIN) {
    push("%LOCALAPPDATA%/Programs");
    push("%LOCALAPPDATA%/Microsoft/WinGet/Links");
    push("%USERPROFILE%/scoop/shims");
    push("%ProgramFiles%");
    push("%ProgramFiles(x86)%");
  } else {
    push("/usr/local/bin");
    push("/opt/homebrew/bin");
    push("/snap/bin");
    push("/usr/bin");
  }
  return uniquePaths(roots).filter(isDir);
}

/**
 * 终端显示宽度。`"成员".length === 2`，但终端里占 4 列 —— 直接用 padEnd 补出来的表头
 * 会整体右移。这里用 wcwidth 的极简版（不引依赖）：CJK/全角/emoji 记 2 列，组合符记 0 列。
 */
function isWideCode(cp) {
  return (cp >= 0x1100 && cp <= 0x115f) ||      // Hangul Jamo
    (cp >= 0x2e80 && cp <= 0xa4cf) ||           // CJK 部首 / 汉字 / 假名 / 注音
    (cp >= 0xac00 && cp <= 0xd7a3) ||           // Hangul 音节
    (cp >= 0xf900 && cp <= 0xfaff) ||           // CJK 兼容汉字
    (cp >= 0xfe30 && cp <= 0xfe6f) ||           // CJK 兼容形式
    (cp >= 0xff00 && cp <= 0xff60) ||           // 全角 ASCII
    (cp >= 0xffe0 && cp <= 0xffe6) ||           // 全角符号
    (cp >= 0x1f300 && cp <= 0x1f64f) ||         // emoji
    (cp >= 0x1f900 && cp <= 0x1f9ff) ||
    (cp >= 0x20000 && cp <= 0x3fffd);           // CJK 扩展 B+
}

function cellWidth(value) {
  let w = 0;
  for (const ch of String(value == null ? "" : value)) {
    const cp = ch.codePointAt(0) || 0;
    if (cp === 0x200b || (cp >= 0x0300 && cp <= 0x036f)) continue;   // 零宽空格 / 组合符
    w += isWideCode(cp) ? 2 : 1;
  }
  return w;
}

/** 按「显示宽度」右侧补空格到 n 列（超长则原样返回）。 */
function padCell(value, n) {
  const v = String(value == null ? "" : value);
  const w = cellWidth(v);
  return w >= n ? v : v + " ".repeat(n - w);
}

/**
 * 极简等宽表：列宽 = max(表头, 本批数据) 的显示宽度 + 2，CJK 也算对。
 * cols: [{ title, get(row) }]；header=false 时不输出表头（postinstall 那种紧凑输出）。
 * 返回字符串数组，调用方逐行 console.log 即可。
 */
function table(rows, cols, opts = {}) {
  const list = Array.isArray(rows) ? rows : [];
  const cells = list.map((row) => cols.map((c) => {
    const v = c.get ? c.get(row) : row[c.key];
    return v == null ? "" : String(v);
  }));
  const widths = cols.map((c, i) => Math.max(
    cellWidth(c.title || ""),
    ...cells.map((line) => cellWidth(line[i])),
  ) + 2);
  const render = (vals) => vals.map((v, i) => (i === vals.length - 1 ? v : padCell(v, widths[i]))).join("").trimEnd();
  const out = [];
  if (opts.header !== false) out.push(render(cols.map((c) => c.title || "")));
  for (const line of cells) out.push(render(line));
  return out;
}

function uniquePaths(list) {
  const seen = new Set();
  const out = [];
  for (const p of list) {
    if (!p) continue;
    const norm = path.resolve(p);
    const key = IS_WIN ? norm.toLowerCase() : norm;
    if (seen.has(key)) continue;
    seen.add(key);
    out.push(norm);
  }
  return out;
}

function versionOf(file, args) {
  const useArgs = args && args.length ? args : ["--version"];
  const res = run(file, useArgs, { timeout: 4000 });
  if (!res.ok) return "";
  const text = (res.out || "").replace(/\x1b\[[0-9;]*m/g, "").trim();
  const line = text.split(/\r?\n/).map((s) => s.trim()).find((s) => /\d+\.\d+/.test(s)) || "";
  return line.slice(0, 120);
}

/**
 * 检测结果缓存（安装时由 detect --write 生成，hub.py 也会读）。
 * ★ 与「用户自定义 Agent 注册表」**必须分开**，否则写缓存会把用户手写的条目整份覆盖掉。
 */
function agentsCachePath() {
  if (process.env.CLAUDELINK_AGENTS) return path.resolve(expand(process.env.CLAUDELINK_AGENTS));
  return path.join(os.homedir(), ".claudelink", "agents.detected.json");
}

/** 用户自定义 Agent 注册表（数组或 {agents:[...]}；只读，任何情况下都不会被写入）。 */
function userAgentsPath() {
  if (process.env.CLAUDELINK_USER_AGENTS) return path.resolve(expand(process.env.CLAUDELINK_USER_AGENTS));
  return path.join(os.homedir(), ".claudelink", "agents.json");
}

function writeJsonAtomic(file, data) {
  const dir = path.dirname(file);
  fs.mkdirSync(dir, { recursive: true });
  const tmp = file + ".tmp-" + process.pid;
  fs.writeFileSync(tmp, JSON.stringify(data, null, 2) + "\n", "utf8");
  fs.renameSync(tmp, file);
}

function readJson(file, fallback) {
  try { return JSON.parse(fs.readFileSync(file, "utf8")); } catch { return fallback; }
}

function httpJson(url, timeout = 1500) {
  return new Promise((resolve) => {
    const req = http.get(url, { timeout }, (res) => {
      let body = "";
      res.setEncoding("utf8");
      res.on("data", (c) => { body += c; });
      res.on("end", () => {
        try { resolve({ ok: res.statusCode === 200, status: res.statusCode, json: JSON.parse(body) }); }
        catch { resolve({ ok: false, status: res.statusCode, json: null }); }
      });
    });
    req.on("timeout", () => { req.destroy(); resolve({ ok: false, status: 0, json: null }); });
    req.on("error", () => resolve({ ok: false, status: 0, json: null }));
  });
}

/**
 * 带方法/头/体的 HTTP 调用（POST /api/* 需要 token 头与 JSON 体）。
 * 返回 { ok, status, json, text, error }，**永不抛异常**：调用方统一看 ok。
 * 连接级失败（ECONNREFUSED 等）给 status=0 与 error.code，便于区分「服务没起来」和「服务拒绝」。
 */
function httpRequest(url, opts = {}) {
  return new Promise((resolve) => {
    let target;
    try { target = new URL(url); } catch (err) { return resolve({ ok: false, status: 0, json: null, text: "", error: err }); }
    const mod = target.protocol === "https:" ? require("https") : http;
    const payload = opts.body === undefined ? null
      : (typeof opts.body === "string" ? opts.body : JSON.stringify(opts.body));
    const headers = Object.assign({}, opts.headers || {});
    if (payload !== null && !headers["Content-Type"]) headers["Content-Type"] = "application/json";
    if (payload !== null) headers["Content-Length"] = Buffer.byteLength(payload);
    const req = mod.request({
      protocol: target.protocol, hostname: target.hostname, port: target.port,
      path: target.pathname + target.search, method: opts.method || (payload ? "POST" : "GET"),
      headers, timeout: opts.timeout || 20000,
    }, (res) => {
      let body = "";
      res.setEncoding("utf8");
      res.on("data", (c) => { body += c; });
      res.on("end", () => {
        let json = null;
        try { json = JSON.parse(body); } catch { /* 非 JSON 响应保持 null */ }
        const ok = res.statusCode >= 200 && res.statusCode < 300 && (!json || json.ok !== false);
        resolve({ ok, status: res.statusCode, json, text: body });
      });
    });
    req.on("timeout", () => { req.destroy(Object.assign(new Error("timeout"), { code: "ETIMEDOUT" })); });
    req.on("error", (err) => resolve({ ok: false, status: 0, json: null, text: "", error: err }));
    if (payload !== null) req.write(payload);
    req.end();
  });
}

function openBrowser(url) {
  const opts = { detached: true, stdio: "ignore", windowsHide: true };
  // 没有打开器（服务器/容器/WSL）时 spawn 会异步抛 error —— 必须吞掉，否则整个 CLI 退出码变 1
  const spawnDetached = (cmd, args) => {
    try {
      const child = cp.spawn(cmd, args, opts);
      child.on("error", () => {});
      child.unref();
      return true;
    } catch {
      return false;
    }
  };
  if (IS_WIN) return spawnDetached(process.env.ComSpec || "cmd.exe", ["/c", "start", "", url]);
  if (process.platform === "darwin") return spawnDetached("open", [url]);
  return spawnDetached("xdg-open", [url]);
}

function tail(file, lines = 20) {
  try {
    const data = fs.readFileSync(file, "utf8").split(/\r?\n/);
    return data.slice(-lines).join("\n");
  } catch {
    return "";
  }
}

/**
 * 找 Python：**不写任何机器路径**。
 * 顺序：CLAUDELINK_PYTHON → py 启动器 → python3/python（PATH）→ 常见安装根下扫描。
 * 需要 ≥3.8 且带 sqlite3（hub.py 只用标准库；Windows Store 的 python 存根会被 ok=false 淘汰）。
 */
function findPython(minMinor = 9) {
  const tried = [];
  const candidates = [];
  const addExe = (p) => {
    if (!p) return;
    const v = expand(p);
    if (!v || unresolved(v)) return;
    // POSIX 下 conda/pyenv 的解释器在 <env>/bin/python；Windows 直接在 <env>\python.exe
    const bases = IS_WIN ? [v] : [v, path.join(v, "bin")];
    for (const base of bases) {
      for (const name of binVariants("python")) {
        const cand = path.join(base, name);
        if (isFile(cand) && !candidates.some((c) => path.resolve(c[0]) === path.resolve(cand))) candidates.push([cand]);
      }
    }
  };

  // 1) 显式指定
  if (process.env.CLAUDELINK_PYTHON) candidates.push([expand(process.env.CLAUDELINK_PYTHON)]);
  // 2) 当前 conda 环境 / pyenv
  addExe(process.env.CONDA_PREFIX);
  // 3) 启动器与 PATH 上的常见名（Windows Store 存根会被探针淘汰）
  if (IS_WIN) {
    candidates.push(["py", "-3"]);
    candidates.push(["python3"]);
    candidates.push(["python"]);
  } else {
    candidates.push(["python3"]);
    candidates.push(["python"]);
  }
  // 4) **便携线索**：conda 自己维护的环境清单 ~/.conda/environments.txt
  try {
    const listFile = path.join(os.homedir(), ".conda", "environments.txt");
    if (isFile(listFile)) {
      const roots = fs.readFileSync(listFile, "utf8").split(/\r?\n/).map((s) => s.trim()).filter(Boolean);
      for (const r of roots) {
        if (isDir(r)) addExe(r);
      }
    }
  } catch { /* ignore */ }
  // 5) 各平台常见安装根（只扫目录结构，不含机器字面量）
  if (IS_WIN) {
    for (const r of ["%LOCALAPPDATA%/Programs/Python", "%ProgramFiles%"]) {
      const dir = expand(r);
      if (!dir || unresolved(dir) || !isDir(dir)) continue;
      let entries = [];
      try { entries = fs.readdirSync(dir, { withFileTypes: true }); } catch { continue; }
      for (const ent of entries) {
        if (!ent.isDirectory() || !/^Python\d{2,3}$/i.test(ent.name)) continue;
        addExe(path.join(dir, ent.name));
      }
    }
  } else {
    for (const r of ["/usr/local/bin", "/usr/bin", "~/.pyenv/shims", "~/.local/bin"]) {
      const dir = expand(r);
      if (!dir || unresolved(dir) || !isDir(dir)) continue;
      for (const name of ["python3", "python"]) {
        const cand = path.join(dir, name);
        if (isFile(cand) && !candidates.some((c) => path.resolve(c[0]) === path.resolve(cand))) candidates.push([cand]);
      }
    }
  }

  for (const cmd of candidates) {
    const [exe, ...pre] = cmd;
    if (!exe) continue;
    if ((exe.includes("\\") || exe.includes("/")) && !isFile(exe)) continue;
    tried.push(exe);
    const res = run(exe, [...pre, "-c", "import sys,sqlite3;print(sys.version.split()[0], sqlite3.sqlite_version)"], { timeout: 5000 });
    if (!res.ok) continue;
    const line = res.out.split(/\r?\n/).map((s) => s.trim()).find(Boolean) || "";
    const [version, sqlite] = line.split(/\s+/);
    if (!version) continue;
    const [maj, min] = version.split(".").map(Number);
    if (!Number.isFinite(maj) || !Number.isFinite(min)) continue;   // 探针输出不是版本号
    if (maj < 3 || (maj === 3 && min < minMinor)) continue;
    return { cmd: [exe, ...pre], exe, version, sqlite, tried };
  }
  return { cmd: null, exe: "", version: "", sqlite: "", tried };
}

module.exports = {
  IS_WIN,
  expand, unresolved, isFile, isDir, run, which, findOnPath, binVariants,
  uniquePaths, versionOf, npmPrefix, npmGlobalRoot, searchRoots,
  cellWidth, padCell, table,
  agentsCachePath, userAgentsPath, writeJsonAtomic, readJson, dataDir,
  withFileLock, updateJsonLocked, readTextSnapshot, lockDirFor, sleepSync,
  httpJson, httpRequest, openBrowser, tail, findPython,
};
