#!/usr/bin/env node
"use strict";
const fs = require("fs");
const path = require("path");
const cp = require("child_process");
const { detect, writeCache, exploreFor, contextFor, versionFor } = require("./detect");
const { loadAgents } = require("./registry");
const { loadConfig, patchConfig, backup, writeSetupConfig } = require("./setup");
const { findPython, httpJson, httpRequest, openBrowser, tail, agentsCachePath, userAgentsPath, npmPrefix, expand, IS_WIN, dataDir: resolveDataDir, updateJsonLocked, withFileLock, table } = require("./util");
const { runAttach } = require("./attach");
const { runEnv, loadManagedEnv } = require("./env");
const { cmdModel, cmdAsk } = require("./model");

const ROOT = path.resolve(__dirname, "..");
const EXAMPLE = path.join(ROOT, "config.example.json");
const PACKAGE_VERSION = require("../package.json").version;
const CODE_REVISION = require("crypto").createHash("sha256").update(fs.readFileSync(path.join(ROOT,"hub.py"))).digest("hex").slice(0,12);
let runtimePython = null;
function pythonForRuntime(){ if(!runtimePython)runtimePython=findPython(); return runtimePython; }
function homeFor(opts){
  if(process.env.CLAUDELINK_HOME)return dataDir();
  return opts.config||process.env.CLAUDELINK_CONFIG?path.dirname(configPath(opts)):dataDir();
}
function resolveRuntime(opts, extra={}){
  const py=pythonForRuntime();
  if(!py.cmd)throw new Error("需要 Python 3.9+；可设置 CLAUDELINK_PYTHON");
  const file=extra.connection?.config_path||configPath(opts);
  const cfg=loadConfig(file,EXAMPLE);
  const home=extra.connection?.data_dir||homeFor(opts);
  const args=[...py.cmd.slice(1),path.join(ROOT,"hub_connection.py"),"--host",hostOf(cfg),"--port",String(portOf(cfg,opts)),"--data-dir",home,"--config",file];
  if(extra.shutdown)args.push("--shutdown");
  if(extra.allowBusy)args.push("--allow-busy");
  const result=require("./util").run(py.cmd[0],args,{timeout:22000});
  if(!result.ok)throw new Error("读取中枢连接状态失败："+(result.out||"").slice(-500));
  try{return JSON.parse(result.out);}catch{throw new Error("中枢连接状态格式错误");}
}

/**
 * 运行时数据目录（config / hub.db / 日志 / 上传）。
 * - 包目录可写（本地 clone、开发）→ 沿用包目录（向后兼容）
 * - 系统级 npm 全局安装（/usr/lib/node_modules，只读）→ 落到 ~/.claudelink-hub
 * 可用 CLAUDELINK_HOME 覆盖。
 */
function dataDir() {
  return resolveDataDir(ROOT);
}

/**
 * 写检测缓存也加锁：doctor / start / detect --write 可能同时跑，
 * 而 detect 的迁移逻辑会 rename 旧文件 —— 不加锁会把别人刚写好的缓存换回旧内容。
 * 缓存是派生物，锁只防「互相踩」，不需要 CAS。
 */
function writeCacheLocked(found) {
  return withFileLock(agentsCachePath(), () => writeCache(found), { timeoutMs: 3000 });
}

const DEFAULT_CONFIG = path.join(dataDir(), "config.json");

function parseArgs(argv) {
  const opts = { _: [] };
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a === "--json") opts.json = true;
    else if (a === "--dry-run") opts.dryRun = true;
    else if (a === "--no-open") opts.noOpen = true;
    else if (a === "--no-setup") opts.noSetup = true;
    else if (a === "--detach") opts.detach = true;
    else if (a === "--write") opts.write = true;
    else if (a === "--explain") {
      const v = argv[++i];
      if (v && !v.startsWith("-")) opts.explain = v;
      else console.error("--explain 缺少 Agent id，已忽略");
    }
    else if (a === "--quiet") opts.quiet = true;
    else if (a === "--yes" || a === "-y") opts.yes = true;
    else if (a === "--attach") opts.attach = true;
    else if (a === "--show") opts.show = true;
    else if (a === "--set") {
      const v = argv[++i];
      if (v && !v.startsWith("--")) (opts.set = opts.set || []).push(v);
      else console.error("--set 缺少 KEY=VALUE，已忽略");
    }
    else if (a === "--unset") {
      const v = argv[++i];
      if (v && !v.startsWith("--")) (opts.unset = opts.unset || []).push(v);
      else console.error("--unset 缺少 KEY，已忽略");
    }
    else if (a === "--only") {
      const v = argv[++i];
      if (v && !v.startsWith("--")) opts.only = v;
      else console.error("--only 缺少 Agent id，已忽略");
    }
    else if (a === "--port") {
      const v = Number(argv[++i]);
      if (Number.isFinite(v) && v > 0) opts.port = v;
      else console.error("忽略无效的 --port 值");
    }
    else if (a === "--config") {
      const v = argv[++i];
      if (v && !v.startsWith("-")) opts.config = v;
      else console.error("--config 缺少路径，已忽略");
    }
    else if (a === "--help" || a === "-h") opts.help = true;
    else opts._.push(a);
  }
  return opts;
}

function configPath(opts) {
  return path.resolve(opts.config || process.env.CLAUDELINK_CONFIG || path.join(dataDir(), "config.json"));
}

function portOf(cfg, opts) {
  if (opts.port) return Number(opts.port);
  const p = cfg && cfg.server && cfg.server.port;
  return Number(p) || 8787;
}

function hostOf(cfg) {
  let h = (cfg && cfg.server && cfg.server.host) || "127.0.0.1";
  if (h === "0.0.0.0" || h === "::") h = "127.0.0.1";
  return h;
}

function urlFor(host, port) {
  return "http://" + host + ":" + port;
}

function tokenFrom(dir) {
  const env = (process.env.CLAUDELINK_TOKEN || "").trim();
  if (env) return env;
  try {
    return fs.readFileSync(path.join(dir || dataDir(), "hub.token"), "utf8").trim();
  } catch {
    return "";
  }
}

function readToken() {
  return tokenFrom(dataDir());
}

/**
 * 「中枢必须是就绪的」这一步：model / ask / status 都靠它。
 * 与 ui/tui 同一条启动路径（版本不同才重启，空闲才升级），失败时给可以说清原因的 error，
 * 而不是让调用方看到一个 fetch 失败。
 */
async function ensureReady(opts) {
  // quiet：model / ask 会把自己的结果写 stdout（可管道），启动进度不该混进去
  const code = await cmdStart({ ...opts, noOpen: true, detach: true, quiet: true, noSetup: fs.existsSync(configPath(opts)) });
  if (code) return { error: "中枢未能启动（退出码 " + code + "）；先跑 `chorum doctor` 看缺什么" };
  const current = resolveRuntime(opts);
  if (!current.running) return { error: "中枢没有在运行" };
  if (!current.ok) return { error: current.error || "中枢在线，但当前配置无法鉴权" };
  const cfgFile = current.config_path || configPath(opts);
  const cfg = loadConfig(cfgFile, EXAMPLE);
  const dataHome = current.data_dir || path.dirname(cfgFile);
  const token = tokenFrom(dataHome);
  if (!token) return { error: "读不到 token（" + path.join(dataHome, "hub.token") + "）；若关闭了鉴权，请设 CLAUDELINK_TOKEN" };
  return { url: urlFor(hostOf(cfg), current.port || portOf(cfg, opts)), token, home: dataHome };
}

/** 子模块返回 {code,message} 时的统一收口：错误进 stderr，退出码照传。 */
async function settle(result) {
  const r = await result;
  if (r && typeof r === "object") {
    if (r.message) console.error(r.message);
    return r.code || 0;
  }
  return r;
}

function pad(s, n) {
  s = String(s == null ? "" : s);
  return s + " ".repeat(Math.max(0, n - s.length));
}

/** source 列很重要：排障时要一眼看出它是从 env / config / PATH / 版本目录 哪找到的。 */
function printTable(found) {
  const lines = table(found, [
    { title: "成员", get: (a) => a.id },
    { title: "版本", get: (a) => a.version },
    { title: "来源", get: (a) => a.source || "-" },
    { title: "状态", get: (a) => (a.usable ? "可用" : "未支持") },
    { title: "路径", get: (a) => a.path },
  ]);
  for (const line of lines) console.log(line);
}

function everythingLine(pr) {
  if (!pr) return "未检测";
  if (pr.status === "ok") return "已启用（" + pr.count + " 条命中，es 来自 " + (pr.how || "?") + "）";
  if (pr.status === "empty") return "已启用但没命中（" + (pr.reason || "无结果") + "）";
  return "未使用（" + (pr.reason || pr.status) + "）";
}

function summary(res) {
  if (res.enabled.length) console.log("启用：" + res.enabled.join(", "));
  if (res.disabled.length) console.log("停用：" + res.disabled.join(", "));
  if (res.unsupported.length) {
    console.log("已识别但未支持：" + res.unsupported.map((u) => u.id + "（" + u.reason + "）").join("；"));
  }
}

function detectOrReport(opts, cfg) {
  // 把现有 config 传进去：用户显式写的 agents.<id>.exe 优先级最高
  const found = detect(loadAgents(), { config: cfg });
  const usable = found.filter((a) => a.usable);
  // --write：把检测结果落成 ~/.claudelink/agents.json，hub.py 会优先用它（安装时自动识别）
  if (opts.write) {
    try {
      const cache = writeCacheLocked(found);
      if (!opts.json && !opts.quiet) console.log("检测缓存：" + cache);
    } catch (err) {
      // 缓存写不了不能拖垮主流程（比如只读 HOME）
      if (!opts.quiet) console.error("检测缓存写入失败（已忽略）：" + err.message);
    }
  }
  if (opts.json) {
    console.log(JSON.stringify(found.map(({ agent, ...rest }) => rest), null, 2));
  } else if (!opts.quiet) {
    if (found.length) printTable(found);
    if (usable.length < 1) console.log("没有识别到AIAgent");
  }
  return { found, usable, ok: usable.length >= 1, cache: opts.write ? agentsCachePath() : "" };
}

const LAYER_ORDER = ["env", "config", "path", "known-dir", "npm-root", "search-root", "extra-root", "scan-dir", "everything"];
const LAYER_LABEL = {
  env: "1 env", config: "2 config", path: "3 path", "known-dir": "4 known-dir",
  "npm-root": "5 npm-root", "search-root": "6 search-root", "extra-root": "7 extra-root",
  "scan-dir": "8 scan-dir", everything: "9 everything",
};
const LAYER_DESC = {
  env: "环境变量 CLAUDELINK_<ID>",
  config: "config.json 的 agents.<id>.exe",
  path: "PATH 里的裸命令名",
  "known-dir": "注册表声明的便携位置",
  "npm-root": "npm 全局根",
  "search-root": "通用搜索根（用户目录优先）",
  "extra-root": "config 的 detect.extra_roots（用户声明，浅扫）",
  "scan-dir": "注册表声明的扫描目录（含版本子目录）",
  everything: "Everything 的 es.exe（可选加速）",
};
const TRIED_WORD = {
  hit: "命中", missing: "不存在", unresolved: "占位符无法展开",
  "not-on-path": "PATH 里没有", "no-match": "没匹配到", skipped: "已跳过",
};

function layerNo(source) {
  const i = LAYER_ORDER.indexOf(source);
  return i >= 0 ? String(i + 1) : "?";
}

/** 给出一条可复制的「钉死这个 Agent」命令（不写死任何机器路径）。 */
function pinHint(agent, target) {
  const name = agent.env || ("CLAUDELINK_" + String(agent.id).toUpperCase().replace(/[^A-Z0-9]+/g, "_"));
  const val = target || "<可执行文件绝对路径>";
  return IS_WIN ? ("setx " + name + " \"" + val + "\"") : ("export " + name + "=\"" + val + "\"");
}

function suggestionFor(agent, target) {
  return pinHint(agent, target) + "    （或把可执行文件所在目录加进 config.json 的 detect.extra_roots 数组）";
}

/** detect --explain <id>：逐层说明「试过哪 → 命中/不存在/不可用(原因)」+ 结论 + 可复制补法。 */
async function cmdExplain(opts, cfg) {
  const registry = loadAgents();
  const agent = registry.find((a) => a.id === String(opts.explain));
  if (!agent) {
    console.error("注册表里没有 id=" + opts.explain + "；可用：" + registry.map((a) => a.id).join(", "));
    return 2;
  }
  const ctx = contextFor(registry, { config: cfg, collectTrace: true });
  const res = exploreFor(agent, ctx);
  const top = res.cands[0];
  const version = top ? (versionFor(top.path, agent) || "?") : "";
  const data = {
    id: agent.id, label: agent.label, support: agent.support,
    found: !!top, path: top ? top.path : "", source: top ? top.source : "", version: version,
    layers: res.trace.map((t) => ({
      layer: t.layer, status: t.status, reason: t.reason,
      triedCount: t.triedCount, tried: t.tried,
    })),
    suggestion: suggestionFor(agent, top ? top.path : ""),
  };
  if (opts.json) {
    console.log(JSON.stringify(data, null, 2));
    return 0;
  }
  console.log("== detect --explain " + agent.id + " ==");
  console.log((agent.label || agent.id) + "    support=" + (agent.support || "detect"));
  console.log("命令名：" + (agent.bin || []).join(" / ") + (agent.env ? "    环境变量：" + agent.env : ""));
  if (agent.note) console.log("说明：" + agent.note);
  console.log("");
  for (const t of res.trace) {
    const label = pad(LAYER_LABEL[t.layer] || t.layer, 14);
    let head;
    if (t.status === "hit") head = "命中 " + t.hits.length + " 处";
    else if (t.status === "unavailable") head = "不可用：" + t.reason;
    else head = "不存在" + (t.reason ? "：" + t.reason : "");
    console.log("  " + label + head + "    [" + (LAYER_DESC[t.layer] || "") + "]");
    for (const item of t.tried) {
      const mark = item.status === "hit" ? "✓" : "✗";
      console.log("        " + mark + " " + item.path + (item.status === "hit" ? "" : "  （" + (TRIED_WORD[item.status] || item.status) + "）"));
    }
    if (t.triedCount > t.tried.length) console.log("        … 另有 " + (t.triedCount - t.tried.length) + " 个位置未列出");
  }
  console.log("");
  if (top) {
    console.log("结论：已找到 —— 第 " + layerNo(top.source) + " 层（" + top.source + "）：" + top.path +
      (version && version !== "?" ? "    版本 " + version : ""));
    console.log("补法：如需钉死这个位置，" + pinHint(agent, top.path));
  } else {
    console.log("结论：没找到 —— 1–9 层都没有可用候选，中枢启动时不会装配它");
    console.log("补法 1：" + suggestionFor(agent, ""));
    console.log("补法 2：确认它确实装在本机（绿色版请用补法 1 直接指路径）");
    if (ctx.everything && ctx.everything.status !== "ok") {
      console.log("提示：everything 加速层当前不可用（" + (ctx.everything.reason || ctx.everything.status) + "）");
    }
  }
  return 0;
}

async function cmdDetect(opts) {
  const file = configPath(opts);
  // 与 setup/start/doctor 一致：用户显式写的 agents.<id>.exe 优先
  const cfg = loadConfig(file, EXAMPLE);
  if (opts.explain) return cmdExplain(opts, cfg);
  const r = detectOrReport(opts, cfg);
  return r.ok ? 0 : 1;
}

/** doctor：把「装没装好」一次说清（检测 / Python / npm / 配置 / 缓存）。`--json` 时 stdout 只有 JSON。 */
async function cmdDoctor(opts) {
  const file = configPath(opts);
  const py = findPython();
  const registry = loadAgents();
  const cfg = loadConfig(file, EXAMPLE);
  // 同一个 context 复用：Everything 只探测一次，missing 的逐层 trace 也用它（json/quiet 下不打印人类输出）
  const ctx = contextFor(registry, { config: cfg, collectTrace: true });
  const found = detect(registry, ctx);
  const usable = found.filter((a) => a.usable);
  const r = { found, usable, ok: usable.length >= 1 };
  // 工作目录：成员的实际 cwd、API 模型的工具根都从这里来。配成不存在的旧路径时
  // 中枢会回退到数据目录，但用户需要知道「你以为它在 A 里工作，其实在 B」。
  const workspaceCwd = (cfg && cfg.workspace && cfg.workspace.cwd) || "";
  const workspaceExists = workspaceCwd ? fs.existsSync(workspaceCwd) : null;
  if (opts.write !== false) {
    try {
      writeCacheLocked(found);
    } catch (err) {
      if (!opts.quiet) console.error("检测缓存写入失败（已忽略）：" + err.message);
    }
  }
  // 找不到的 Agent 不再「静默消失」：逐个给出已试层清单 + 可复制的补法
  const foundIds = new Set(found.map((a) => a.id));
  const missing = registry.filter((a) => !foundIds.has(a.id)).map((a) => {
    const res = exploreFor(a, ctx);
    return {
      id: a.id,
      label: a.label,
      support: a.support,
      layers: res.trace.map((t) => ({
        layer: t.layer, status: t.status, reason: t.reason,
        triedCount: t.triedCount, tried: t.tried.slice(0, 6),
      })),
      suggestion: suggestionFor(a, ""),
    };
  });
  const data = {
    ok: !!(r.ok && py.cmd),
    packageDir: ROOT,
    dataDir: dataDir(),
    config: { path: file, exists: fs.existsSync(file) },
    workspace: { cwd: workspaceCwd, exists: workspaceExists },
    agentsCache: process.env.CLAUDELINK_AGENTS || agentsCachePath(),
    userAgents: process.env.CLAUDELINK_USER_AGENTS || userAgentsPath(),
    npmPrefix: npmPrefix() || "",
    python: py.cmd ? { version: py.version, sqlite: py.sqlite, exe: py.exe } : null,
    agents: r.usable.map((a) => ({ id: a.id, version: a.version, source: a.source, path: a.path })),
    unsupported: r.found.filter((a) => !a.usable).map((a) => a.id),
    // T10：检测层自证材料（缺什么 / 试过哪 / 怎么补）
    detected: {
      found: r.found.map((a) => ({ id: a.id, label: a.label, source: a.source, path: a.path, usable: a.usable })),
      missing: missing,
    },
    everything: ctx.everything ? {
      status: ctx.everything.status, reason: ctx.everything.reason,
      exe: ctx.everything.exe, how: ctx.everything.how,
      roots: ctx.everything.roots, count: ctx.everything.count, ms: ctx.everything.ms,
    } : null,
  };
  // 用户注册表是否被真正用上（旧版本把检测缓存写在这里，形态是 {schema,byId}）
  data.userAgentsNote = "（可选，自己加 Agent 时用）";
  try {
    if (fs.existsSync(data.userAgents)) {
      const u = JSON.parse(fs.readFileSync(data.userAgents, "utf8"));
      data.userAgentsNote = (u && typeof u === "object" && (u.byId || u.schema))
        ? "（旧版检测缓存，可安全删除）"
        : (Array.isArray(u) || Array.isArray(u.agents) ? "（已加载）" : "（形态不认识）");
    }
  } catch { data.userAgentsNote = "（存在但无法解析）"; }
  if (opts.json) {
    console.log(JSON.stringify(data, null, 2));
    return 0;
  }
  if (opts.quiet) return 0;

  console.log("== chorum doctor ==");
  console.log("包目录      : " + ROOT);
  console.log("数据目录    : " + data.dataDir + (data.dataDir === ROOT ? "（与包目录相同）" : "（包目录只读，已切到用户目录）"));
  console.log("配置文件    : " + file + (data.config.exists ? "（存在）" : "（不存在，首次 start 会生成）"));
  if (workspaceCwd) {
    console.log("工作目录    : " + workspaceCwd +
      (workspaceExists ? "" : "  ✗ 不存在：中枢会回退到数据目录 " + data.dataDir + "，成员的 cwd 与工具根都跟着变"));
  } else {
    console.log("工作目录    : 未配置（workspace.cwd 缺省 = 数据目录 " + data.dataDir + "）");
  }
  console.log("检测缓存    : " + data.agentsCache);
  console.log("用户注册表  : " + data.userAgents + data.userAgentsNote);
  console.log("npm 全局前缀: " + (data.npmPrefix || "（未识别，可设 CLAUDELINK_NPM_PREFIX）"));
  if (py.cmd) console.log("Python      : " + py.version + "（sqlite " + py.sqlite + "）-> " + py.exe);
  else console.log("Python      : ✗ 未找到 3.9+（可设 CLAUDELINK_PYTHON；探过：" + (py.tried || []).join(", ") + "）");
  if (r.found.length) printTable(r.found);
  console.log("Everything  : " + everythingLine(ctx.everything));
  if (missing.length) {
    console.log("未识别      : " + missing.map((m) => m.id).join(", ") +
      "（用 detect --explain <id> 看逐层原因）");
  }
  console.log("检测保证    : 保证（PATH / npm 全局 / 已知目录 / 用户声明）｜" +
    "尽力（Everything / extra_roots / 版本哈希扫描）｜" +
    "不保证（绿色版陌生路径 / 注册表外的新 Agent / sudo 安装导致 HOME 错位）");
  console.log("--");
  if (!r.ok) console.log("结论：没有可用 Agent —— 先装一个 AI Agent CLI（或检查 PATH），再跑 `chorum doctor`");
  else if (!py.cmd) console.log("结论：Agent 就绪，但缺 Python 3.9+，`start` 需要它来跑中枢");
  else console.log("结论：就绪。直接 `chorum start`");
  return 0;
}

async function cmdSetup(opts) {
  // --attach / --detach：只接 MCP 接口，**不动 config.json**（默认零副作用）
  if (opts.attach || opts.detach) {
    return runAttach({
      detach: opts.detach, yes: opts.yes, json: opts.json, config: configPath(opts), only: opts.only,
    });
  }
  const file = configPath(opts);
  const cfg = loadConfig(file, EXAMPLE);
  const found = detect(loadAgents(), { config: cfg });
  const usable = found.filter((a) => a.usable);
  if (usable.length < 1) {
    printTable(found);
    console.log("没有识别到AIAgent");
    return 1;
  }
  if (opts.dryRun) {
    const preview = patchConfig(cfg, found, { port: opts.port });
    console.log("配置：" + file);
    summary(preview);
    console.log("dry-run：未写入");
    return 0;
  }
  // 加锁 + 写前 CAS：两个 setup 同时跑时基于磁盘最新内容重算，不丢更新
  const out = writeSetupConfig(file, EXAMPLE, found, { port: opts.port });
  if (!out.ok) {
    console.error("✗ 写入失败：" + writeFailReason(out, file));
    return 1;
  }
  console.log("配置：" + file);
  summary(out.res);
  if (out.backup) console.log("备份：" + out.backup);
  console.log(out.unchanged ? "写入完成（内容无变化，未改动文件）" : "写入完成");
  await warnIfHubRunning(cfg, opts);
  return 0;
}

/**
 * 中枢运行期间它的配置是「启动时的内存快照」：Web UI 里任何一次保存都会整份回写，
 * 把刚刚 setup 的结果覆盖掉。这不是 CLI 能修掉的（hub.py 不拿我们的锁），只能提醒。
 */
async function warnIfHubRunning(cfg, opts) {
  try {
    const h = await httpJson(urlFor(hostOf(cfg), portOf(cfg, opts)) + "/api/health", 800);
    if (!h.ok) return;
    console.log("提示：中枢正在运行，本次改动已安全落盘（中枢保存时会与磁盘合并，不会覆盖它）；" +
      "但中枢内存里仍是启动快照，要让新配置在界面上生效需要 stop 再 start。");
  } catch { /* 健康检查失败不影响 setup 结果 */ }
}

/** 把「锁超时 / CAS 反复冲突」翻译成人能看懂的一句话。 */
function writeFailReason(out, file) {
  if (out && out.reason === "lock") {
    return ((out.error && out.error.message) || ("另一个进程正在写 " + file)) +
      "（可用 CLAUDELINK_NO_LOCK=1 应急绕过，但要确认没有别的实例在跑）";
  }
  if (out && out.reason === "conflict") {
    return file + " 在写入期间被其他进程反复修改（例如中枢 Web UI 正在改配置），已放弃写入，请稍后重试";
  }
  return (out && out.error && out.error.message) || "未知原因";
}

function spawnHub(py, detach, configFile, runtimeHome) {
  const hub = path.join(ROOT, "hub.py");
  // 必须把配置路径显式传给子进程：否则 hub.py 只会读包目录下的 config.json，
  // --config 指到别处时会"起了服务但端口/成员全不对"。
  // CLAUDELINK_AGENTS：把本次检测结果（真实路径）交给 hub.py，避免它再猜机器布局。
  const env = Object.assign({}, process.env, {
    CLAUDELINK_CONFIG: configFile,
    CLAUDELINK_AGENTS: agentsCachePath(),
    CLAUDELINK_HOME: runtimeHome || path.dirname(configFile),
  });
  if (detach) {
    const log = fs.openSync(path.join(runtimeHome || path.dirname(configFile), "hub.log"), "a");
    const child = cp.spawn(py.cmd[0], [...py.cmd.slice(1), hub], {
      cwd: ROOT, env, detached: true, stdio: ["ignore", log, log], windowsHide: true,
    });
    fs.closeSync(log);
    child.unref();
    return { child, logPath: path.join(runtimeHome || path.dirname(configFile), "hub.log") };
  }
  const child = cp.spawn(py.cmd[0], [...py.cmd.slice(1), hub], {
    cwd: ROOT, env, stdio: "inherit",
  });
  return { child, logPath: "" };
}

async function waitHealth(url, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const h = await httpJson(url + "/api/health", 1200);
    if (h.ok) return h;
    await new Promise((r) => setTimeout(r, 500));
  }
  return null;
}

async function cmdStart(opts) {
  let file=configPath(opts), runtimeHome=homeFor(opts);
  let cfg0=loadConfig(file,EXAMPLE);
  const port=portOf(cfg0,opts), host=hostOf(cfg0), url=urlFor(host,port);
  const current=resolveRuntime(opts);
  if(current.running){
    if(!current.ok){console.error(current.error);return 1;}
    runtimeHome=current.data_dir;file=current.config_path;
    if(current.version===PACKAGE_VERSION&&current.code_revision===CODE_REVISION){
      if(!opts.quiet)console.log("Chorum "+current.version+" 已在运行："+url);
      if(!opts.noOpen)openBrowser(url);
      return 0;
    }
    if(current.busy){console.error("运行中的服务版本不是 "+PACKAGE_VERSION+"，且有任务在执行；未中断任务或启动第二个实例。");return 1;}
    const stopped=resolveRuntime(opts,{shutdown:true,connection:current});
    if(!stopped.ok){console.error(stopped.error||"无法停止旧服务");return 1;}
    let gone=false;
    for(let i=0;i<50;i++){const h=await httpJson(url+"/api/health",300);if(!h.ok){gone=true;break;}await new Promise(r=>setTimeout(r,100));}
    if(!gone){console.error("旧服务尚未退出，未启动第二个实例");return 1;}
    cfg0=loadConfig(file,EXAMPLE);opts={...opts,noSetup:true};
    console.log("旧服务已停止，使用原数据启动 "+PACKAGE_VERSION);
  }
  fs.mkdirSync(runtimeHome,{recursive:true});
  const found=opts.noSetup?[]:detect(loadAgents(),{config:cfg0});
  const usable=found.filter(a=>a.usable);
  if(!opts.noSetup&&!usable.length&&!opts.quiet)console.log("未检测到可运行 CLI；可用 `model add` 加一个 API 模型，或在网页成员面板添加。");
  if(found.length)try{writeCacheLocked(found);}catch(err){console.error("检测缓存写入失败："+err.message);}
  // 先确认 Python 再动配置：缺解释器时不应该已经改过用户的 config
  const py = pythonForRuntime();
  if (!py || !py.cmd) {
    console.error("没有找到 Python 3.9+（中枢是 Python 写的）。");
    console.error("装一个 3.9+ 后重试，或设置 CLAUDELINK_PYTHON 指向 python 解释器。");
    if (py && py.tried && py.tried.length) console.error("已尝试：" + py.tried.join(", "));
    return 1;
  }

  if (!opts.noSetup) {
    // 与 setup 同一条加锁写路径：start 和 setup 同时跑也不会互相覆盖
    const out = writeSetupConfig(file, EXAMPLE, found, { port: opts.port });
    if (!out.ok) {
      console.error("✗ 写入配置失败：" + writeFailReason(out, file));
      return 1;
    }
    if(!opts.quiet)console.log("配置：" + file + (out.backup ? "（备份 " + path.basename(out.backup) + "）" : "）"));
    summary(out.res);
  } else if (!fs.existsSync(file)) {
    console.error("config.json 不存在，且指定了 --no-setup");
    return 1;
  } else if (opts.port) {
    // --no-setup 的语义是"别动我的配置"：只写 server.port，别重建 agents/order
    let bak = "";
    const out = updateJsonLocked(file, (cur) => {
      const base = cur || cfg0;
      base.server = base.server || {};
      base.server.port = Number(opts.port);
      return base;
    }, { onBeforeWrite: () => { if (!bak) bak = backup(file); } });
    if (!out.ok) {
      console.error("✗ 端口写入失败：" + writeFailReason(out, file));
      return 1;
    }
    console.log("端口已改为 " + opts.port + "（" + (bak ? path.basename(bak) : "无备份") + "）");
  }

  if(!opts.quiet)console.log("Python：" + py.version + "（sqlite " + py.sqlite + "）");
  const { child, logPath } = spawnHub(py, opts.detach, file, runtimeHome);
  // 子进程立刻退出时别空等 35 秒
  let exitInfo = null;
  child.on("exit", (code, signal) => { exitInfo = { code, signal }; });
  let health = null;
  const readinessDeadline = Date.now() + 35000;
  while (Date.now() < readinessDeadline) {
    health = await httpJson(url + "/api/health", 1200);
    if (health.ok) break;
    health = null;
    if (exitInfo) break;
    await new Promise(resolve => setTimeout(resolve, 150));
  }
  if (!health) {
    // 两个 start 同时跑时，抢到端口的那个会活下来、这里报 EADDRINUSE 退出：
    // 再探一次健康检查，能通就说明中枢已经由别的进程起来了，不该报错
    const raced = exitInfo ? await httpJson(url + "/api/health", 1500) : null;
    if (raced && raced.ok) {
      console.log("中枢已由另一个进程启动：" + url);
      if (!opts.noOpen) openBrowser(url);
      return 0;
    }
    console.error("中枢启动失败：" + (exitInfo ? "进程已退出（code=" + exitInfo.code + " signal=" + exitInfo.signal + "）" : "35 秒内 /api/health 未就绪"));
    if (logPath) console.error(tail(logPath));
    return 1;
  }
  if(!opts.quiet)console.log("中枢已就绪：" + url);
  if (!opts.noOpen) openBrowser(url);
  if (opts.detach) return 0;

  return await new Promise((resolve) => {
    child.on("exit", (code) => resolve(code || 0));
    process.on("SIGINT", () => { try { child.kill("SIGINT"); } catch {} });
  });
}

async function cmdTUI(opts) {
  let file=configPath(opts), home=homeFor(opts);
  const readOnly=opts._.includes("--check")||opts._.includes("--no-start");
  if(!readOnly){
    const started=await cmdStart({...opts,noOpen:true,detach:true,noSetup:fs.existsSync(file)});
    if(started)return started;
  }
  const resolved=resolveRuntime(opts);
  if(resolved.running&&resolved.ok){file=resolved.config_path;home=resolved.data_dir;}
  if(!readOnly)fs.mkdirSync(home,{recursive:true});
  const py=pythonForRuntime();
  if(!py.cmd)throw new Error("需要 Python 3.9+");
  const env={...process.env,CLAUDELINK_CONFIG:file,CLAUDELINK_HOME:home,CLAUDELINK_AGENTS:agentsCachePath()};
  const args=[...py.cmd.slice(1),path.join(ROOT,"hub_tui.py"),...opts._.slice(1)];
  return new Promise((resolve,reject)=>{
    const child=cp.spawn(py.cmd[0],args,{cwd:ROOT,env,stdio:"inherit",windowsHide:true});
    child.once("error",reject);child.once("exit",code=>resolve(code==null?1:code));
  });
}

async function cmdEnv(opts) {
  return runEnv({ show: opts.show, set: opts.set || [], unset: opts.unset || [], yes: opts.yes, json: opts.json });
}

async function cmdUI(opts) {
  const code=await cmdStart({...opts,noOpen:true,detach:true,noSetup:fs.existsSync(configPath(opts))});
  if(code)return code;
  const cfg=loadConfig(configPath(opts),EXAMPLE);
  const url=urlFor(hostOf(cfg),portOf(cfg,opts));
  if(!opts.noOpen)openBrowser(url);
  console.log("Web 工作台："+url);
  return 0;
}

async function cmdStop(opts){
  const current=resolveRuntime(opts);
  if(!current.running){console.log("中枢未运行");return 0;}
  if(!current.ok){console.error(current.error);return 1;}
  const result=resolveRuntime(opts,{shutdown:true,allowBusy:true,connection:current});
  if(!result.ok){console.error(result.error);return 1;}
  console.log("已停止中枢，数据保留");return 0;
}

/** status：不启动、不写东西，只报「现在是什么状态」。 */
async function cmdStatus(opts) {
  const current = resolveRuntime(opts);
  if (!current.running) {
    console.log("中枢未运行。启动：chorum ui（网页）或 chorum tui（终端）");
    return 0;
  }
  if (!current.ok) { console.error(current.error); return 1; }
  const cfgFile = current.config_path || configPath(opts);
  const cfg = loadConfig(cfgFile, EXAMPLE);
  const url = urlFor(hostOf(cfg), current.port || portOf(cfg, opts));
  const token = tokenFrom(current.data_dir || path.dirname(cfgFile));
  const res = await httpRequest(url + "/api/state", { headers: { "X-ClaudeLink-Token": token }, timeout: 8000 });
  const data = {
    running: true, url, version: current.version, codeRevision: current.code_revision,
    pid: current.pid, dataDir: current.data_dir, config: cfgFile, busy: !!current.busy,
    agents: res.ok ? (res.json.agents || []) : [],
    health: res.ok ? (res.json.health || {}) : {},
  };
  if (opts.json) { console.log(JSON.stringify(data, null, 2)); return 0; }
  console.log("地址        : " + url);
  console.log("版本        : " + (current.version || "?") + "（代码 " + (current.code_revision || "?") + "，pid " + (current.pid || "?") + "）");
  console.log("数据目录    : " + (current.data_dir || "?"));
  console.log("状态        : " + (current.busy ? "有任务在执行或排队" : "空闲"));
  if (!res.ok) {
    console.log("成员        : 读不到（HTTP " + res.status + "）：token 可能不匹配");
    return 1;
  }
  const agents = data.agents;
  if (!agents.length) console.log("成员        : 无");
  else {
    console.log("");
    for (const line of table(agents, [
      { title: "成员", get: (a) => a.name },
      { title: "名称", get: (a) => a.label },
      { title: "类型", get: (a) => (a.kind === "cli" ? "CLI" : "API " + a.kind) },
      { title: "状态", get: (a) => (a.enabled ? (a.running_since ? "执行中" : "启用") : "停用") },
      { title: "模型", get: (a) => a.model || a.session || "" },
      { title: "会话", get: (a) => (a.native_memory ? "原生" : "中枢") },
    ])) console.log(line);
  }
  return 0;
}

function help() {
  console.log([
    "chorum <command>",
    "",
    "  detect            识别本机已安装的 AI Agent（--json / --write 写检测缓存）",
    "                    --explain <id>  某个 Agent 找不到时：逐层「试过哪 → 命中/不存在/不可用」+ 补法",
    "  doctor            一次性体检：Agent / Python / 工作目录 / npm / 配置 / 缓存",
    "  setup             按检测结果装配 config.json（--dry-run 只预览）",
    "                    --attach  把已识别的 Agent 接到 ClaudeLink MCP（默认 dry-run，--yes 才执行）",
    "                    --detach  精确移除上面注册的 claudelink 条目",
    "  start             检测 → 装配 → 启动中枢并打开 Web UI",
    "                    --port 8787  --no-open  --no-setup  --detach",
    "  status            看当前实例：地址、版本、成员、是否忙（不启动、不写盘）",
    "  ui                打开已在运行的中枢页面",
    "  stop              让中枢退出（server.allow_shutdown=true 时）",
    "  tui               打开终端界面；首次运行自动检测并生成配置",
    "",
    "  model list        列出已配置的 API 模型",
    "  model add         --label 名称 --style openai|anthropic --base-url URL --model M",
    "                    [--key K] [--path P] [--max-tokens N] [--id 覆盖已有]",
    "  model test        --base-url URL --model M [--style …] [--key K]  只验证连通性",
    "  model rm <id>     删除某个 API 模型",
    "  ask --to <id|名称> \"问题\"",
    "                    发给某个成员并实时显示回复与它的工具调用（--timeout 300 / --no-wait / - 读 stdin）",
    "  attach / detach   同 setup --attach / setup --detach",
    "  env               用户级变量：--show / --set K=V / --unset K（--yes 才写入）",
    "",
    "环境变量：CLAUDELINK_CONFIG  CLAUDELINK_PYTHON  CLAUDELINK_AGENTS  CLAUDELINK_HOME",
    "          CLAUDELINK_REGISTRY（自定义注册表 JS）  CLAUDELINK_NPM_PREFIX",
  ].join("\n"));
}

async function main() {
  // 用户级变量（~/.claudelink/env.json）在解析参数前加载：真实环境变量优先
  try { loadManagedEnv(); } catch { /* 坏文件不影响主流程 */ }
  const argv = process.argv.slice(2);
  const opts = parseArgs(argv);
  const cmd = opts._[0] || (opts.help ? "help" : "start");
  if (cmd === "help" || opts.help) { help(); return 0; }
  if (cmd === "detect") return cmdDetect(opts);
  if (cmd === "doctor") return cmdDoctor(opts);
  if (cmd === "setup") return cmdSetup(opts);
  if (cmd === "attach") return runAttach({ yes: opts.yes, json: opts.json, config: configPath(opts), only: opts.only });
  if (cmd === "detach") return runAttach({ detach: true, yes: opts.yes, json: opts.json, config: configPath(opts), only: opts.only });
  if (cmd === "env") return cmdEnv(opts);
  if (cmd === "start") return cmdStart(opts);
  if (cmd === "tui") return cmdTUI(opts);
  if (cmd === "ui") return cmdUI(opts);
  if (cmd === "stop") return cmdStop(opts);
  if (cmd === "status") return cmdStatus(opts);
  // model / ask 走自己的参数解析：它们的选项比全局旗标多，混在一个 parser 里会互相吃掉。
  // 传「命令词之后的原始参数」，这样 `--port 9000 model add …` 这种前置写法也能用。
  if (cmd === "model" || cmd === "ask") {
    const raw = process.argv.slice(2);
    const at = raw.indexOf(cmd);
    const rest = at >= 0 ? raw.slice(at + 1) : [];
    const ctx = { ensureReady: (extra) => ensureReady({ ...opts, ...(extra || {}) }) };
    return settle(cmd === "model" ? cmdModel(rest, ctx) : cmdAsk(rest, ctx));
  }
  console.error("未知命令：" + cmd);
  help();
  return 2;
}

main().then((code) => { process.exitCode = code; }).catch((err) => {
  console.error(process.env.CLAUDELINK_DEBUG ? (err && err.stack || err) : (err && err.message || err));
  process.exitCode = 1;
});
