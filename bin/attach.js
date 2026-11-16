#!/usr/bin/env node
"use strict";
/**
 * 把「已识别的 Agent CLI」接到 ClaudeLink 的 MCP 接口。
 *
 * 三条底线（甲方硬性约束）：
 *   1) 默认零副作用：没有 --yes / CLAUDELINK_AUTO_ATTACH=1 时只打印将要执行的命令。
 *   2) 不猜命令：只消费注册表里的 mcp 元数据（{list,add,remove}）；没有就打印「未支持自动接入」。
 *   3) 只用 detect 解析出的路径调用（codex 这类不在 PATH 的必须靠它），每条 10 秒超时；
 *      超时/失败 → 状态 unknown，绝不把它当成「未接入」去重复写入。
 *
 * 账本：<数据目录>/attach-state.json（{agent, cmd, at}），只记我们做过什么，不记任何密钥。
 */
const fs = require("fs");
const os = require("os");
const path = require("path");
const { detect } = require("./detect");
const { loadAgents } = require("./registry");
const { loadConfig } = require("./setup");
const { dataDir, run, findOnPath, readJson, expand, updateJsonLocked, withFileLock } = require("./util");

const PKG_ROOT = path.resolve(__dirname, "..");
const SERVER_DEFAULT = "claudelink-server";
const NAME = "claudelink";
const PAD = 12;

/** mcp 命令超时（默认 10s；测试可用 CLAUDELINK_ATTACH_TIMEOUT 调小）。 */
function timeoutMs() {
  const v = Number(process.env.CLAUDELINK_ATTACH_TIMEOUT);
  return Number.isFinite(v) && v > 0 ? v : 10000;
}

/** 要注册的服务命令（默认 claudelink-server，可用 CLAUDELINK_MCP_SERVER 换自定义部署）。 */
function serverCommand() {
  return process.env.CLAUDELINK_MCP_SERVER || SERVER_DEFAULT;
}

// ── 纯函数（selftest 直接测这些，不需要真的装 CLI）─────────────────────────

/** 注册表模板 → argv（{server} 占位替换）。 */
function tmplArgs(tmpl, server) {
  return (Array.isArray(tmpl) ? tmpl : []).map((a) => String(a).replace(/\{server\}/g, server || SERVER_DEFAULT));
}

/** 取某 Agent 的 mcp 元数据；null / 缺字段 一律视为「不支持自动接入」。 */
function mcpMeta(hit) {
  const m = (hit && hit.mcp) || (hit && hit.agent && hit.agent.mcp);
  if (!m || typeof m !== "object") return null;
  if (!Array.isArray(m.list) || !m.list.length) return null;
  if (!Array.isArray(m.add) || !m.add.length) return null;
  return m;
}

/**
 * 解析 mcp list 的三种真实输出（2026-10 实测）：
 *   claude     claudelink: claudelink-server  - √ Connected
 *   codebuddy  claudelink: <node.exe> <...\claudelink\bin\server.js> - ✓ Connected
 *   codex      表格：Name   Command   Args   Env   Cwd   Status   Auth
 * 只认「名字恰好是 claudelink」的条目，避免把 claudelink-server / claudelink-xxx 认错。
 */
const ENTRY_RE = new RegExp("^\\s*" + NAME + "\\s*(?::|\\s{2,}|$)");
function parseList(out) {
  const lines = String(out || "").split(/\r?\n/);
  for (const line of lines) {
    if (!ENTRY_RE.test(line)) continue;
    const rest = line.replace(ENTRY_RE, "").replace(/\s+-\s+[√✓✗✘×].*$/, "").trim();
    // 已接入 = 有条目 且 命令指向 ClaudeLink（健康检查的 √/✗ 只作展示，不参与判定）
    const attached = /claudelink-server|server\.js/i.test(rest);
    return { found: true, attached, command: rest.slice(0, 300), line: line.trim().slice(0, 300) };
  }
  return { found: false, attached: false, command: "", line: "" };
}

/** 拒绝条件：sudo / root 家目录（会把配置写到错的地方）。返回空串 = 通过。 */
function refusalReason(info, env) {
  const u = info || {};
  const e = env || {};
  if (String(u.username || "") !== "root") return "";
  if (!e.SUDO_USER && e.HOME !== "/root") return "";
  return "检测到以 root 身份运行（" + (e.SUDO_USER ? "SUDO_USER=" + e.SUDO_USER : "HOME=/root") + "）：" +
    "这样会把接入写进 root 的家目录，和你日常用的 CLI 配置对不上。请换回普通用户运行" +
    "（或 sudo -u <你的用户> claudelink-hub setup --attach）。";
}

// ── 执行层 ────────────────────────────────────────────────────────────────

function quote(s) {
  const v = String(s == null ? "" : s);
  return /\s/.test(v) ? '"' + v + '"' : v;
}
function displayArgv(argv) {
  return argv.map(quote).join(" ");
}
function fmtStatus(s) {
  return (s === null || s === undefined) ? "超时/无退出码" : String(s);
}
function lastLines(text, n) {
  return String(text || "").split(/\r?\n/).map((s) => s.trim()).filter(Boolean).slice(-n).join(" | ").slice(0, 400);
}

/**
 * 该用哪个可执行文件跑 mcp 子命令。
 * .js / 无扩展名的 node 入口 → node <file>（和 detect 的 versionFor 同一套判定）；
 * 其余（.exe/.cmd/bat）直接跑 —— .cmd 由 util.run 走 shell。
 */
function launcherFor(hit) {
  const file = String((hit && hit.path) || "");
  const isJs = /\.(js|mjs|cjs)$/i.test(file);
  const bareNode = !isJs && !!(hit && hit.agent && hit.agent.node) && !/\.(exe|cmd|bat)$/i.test(file);
  if (isJs || bareNode) {
    const node = (hit.node && fs.existsSync(hit.node)) ? hit.node : (findOnPath("node") || process.execPath);
    return { exe: node, pre: [file] };
  }
  return { exe: file, pre: [] };
}

function argvFor(hit, args) {
  const l = launcherFor(hit);
  return [l.exe, ...l.pre, ...args];
}

/** 跑一条命令：继承用户环境（尊重 CLAUDE_CONFIG_DIR / CODEX_HOME），带超时。 */
function exec(argv) {
  const res = run(argv[0], argv.slice(1), { timeout: timeoutMs() });
  return {
    ok: !!res.ok,
    status: res.status,
    timedOut: !!(res.error && (res.error.code === "ETIMEDOUT" || /timed?\s?out/i.test(String(res.error.message || "")))),
    out: res.out || "",
    argv,
    display: displayArgv(argv),
  };
}

// ── 账本 ──────────────────────────────────────────────────────────────────

function ledgerPath() {
  return path.join(dataDir(PKG_ROOT), "attach-state.json");
}

function normalizeLedger(d) {
  const entries = (d && typeof d === "object" && d.entries && typeof d.entries === "object") ? d.entries : {};
  return { schema: 1, updatedAt: (d && d.updatedAt) || "", entries };
}

function readLedger() {
  return normalizeLedger(readJson(ledgerPath(), null));
}

/**
 * 账本落盘：锁内「读最新 → 合并本次增删 → 写」。
 * 两个 attach 进程同时跑时各自只合并自己的条目，不会互相覆盖。
 */
function commitLedger(added, removed) {
  const out = updateJsonLocked(ledgerPath(), (cur) => {
    const l = normalizeLedger(cur);
    for (const [id, entry] of added) l.entries[id] = entry;
    for (const id of removed) delete l.entries[id];
    l.updatedAt = new Date().toISOString();
    return l;
  }, {});
  return Object.assign({ path: ledgerPath() }, out);
}

// ── 主流程 ────────────────────────────────────────────────────────────────

function configFileOf(opts) {
  if (opts && opts.config) return path.resolve(expand(opts.config));
  if (process.env.CLAUDELINK_CONFIG) return path.resolve(expand(process.env.CLAUDELINK_CONFIG));
  return path.join(dataDir(PKG_ROOT), "config.json");
}

function parseOnly(v) {
  if (!v) return null;
  const set = new Set(String(v).split(",").map((s) => s.trim()).filter(Boolean));
  return set.size ? set : null;
}

function countStates(rows) {
  const c = {};
  for (const r of rows) c[r.state] = (c[r.state] || 0) + 1;
  return c;
}

function printHuman(result, rows, missing, apply, detach) {
  console.log("== claudelink-hub " + result.mode + " ==");
  console.log("数据目录    : " + result.dataDir);
  console.log("账本        : " + result.ledger);
  console.log("服务命令    : " + result.server);
  // 让「继承用户环境」这件事可见：错的家目录是这类工具最常见的坑
  const envLine = ["CLAUDE_CONFIG_DIR", "CODEX_HOME"]
    .filter((k) => process.env[k])
    .map((k) => k + "=" + process.env[k]);
  if (envLine.length) console.log("用户环境    : " + envLine.join("  "));
  console.log("--");
  const nameWidth = Math.max(PAD, ...rows.map((r) => r.id.length + 2));
  for (const r of rows) {
    if (r.state === "missing") continue;
    const id = r.id.padEnd(nameWidth);
    let text;
    switch (r.state) {
      case "pending": text = r.note + "： " + r.display; break;
      case "added": text = "已注册 ✓ " + r.display; break;
      case "detached": text = "已移除 ✓ " + r.display; break;
      case "failed": text = r.note + (r.error ? "（" + r.error + "）" : ""); break;
      case "unsupported": text = "未支持：" + r.note; break;
      default: text = r.note;
    }
    console.log(id + text);
  }
  if (missing.length) console.log("未安装（不执行）：" + missing.join(", "));
  console.log("--");
  const c = countStates(rows);
  console.log("结果：已接入 " + ((c.added || 0) + (c.attached || 0)) +
    " / " + (detach ? "已移除 " : "待执行 ") + (detach ? (c.detached || 0) : (c.pending || 0)) +
    " / 跳过 " + ((c.skipped || 0) + (c["no-mcp"] || 0) + (c.unsupported || 0)) +
    " / 未知 " + (c.unknown || 0) + " / 失败 " + (c.failed || 0) +
    (missing.length ? " / 未安装 " + missing.length : ""));
  if (!apply) {
    console.log(detach
      ? "dry-run：未执行任何命令。加 --yes 才会真正移除。"
      : "dry-run：未执行任何命令。加 --yes（或 CLAUDELINK_AUTO_ATTACH=1）才会真正接入。");
  }
}

/**
 * attach / detach 主流程。
 * opts: { detach, yes, json, quiet, config, only }
 * 返回退出码：0 成功（含 dry-run）｜1 有失败项｜2 环境不允许（sudo/root）
 */
/** 接入事务锁：attach / detach 全程互斥，避免两个进程同时判「未接入」后各写一条第三方配置。 */
function runLockPath() {
  return path.join(dataDir(PKG_ROOT), "attach-run.json");
}

/**
 * attach / detach 入口。
 * 真正执行（--yes / CLAUDELINK_AUTO_ATTACH=1）时用一整把锁串起「探测 → 写第三方 → 记账」，
 * 因为这里的写发生在别人的配置文件里，重复写 = 重复条目，事后很难收拾。
 * dry-run 只读，不占锁。
 */
function runAttach(opts) {
  const o = opts || {};
  const apply = !!(o.yes || process.env.CLAUDELINK_AUTO_ATTACH === "1");
  if (!apply) return runAttachInner(o);
  try {
    return withFileLock(runLockPath(), () => runAttachInner(o), { timeoutMs: 60000, staleMs: 180000, attempts: 1 });
  } catch (err) {
    if (err && err.code === "LOCK_TIMEOUT") {
      const msg = "另一个 claudelink-hub 正在接入（attach/detach 不允许并发，否则会在第三方 CLI 配置里写出重复条目）；请等它结束后重试";
      if (o.json) {
        console.log(JSON.stringify({
          schema: 1, mode: o.detach ? "detach" : "attach", applied: false,
          error: msg, agents: [], failures: 1,
        }, null, 2));
      } else {
        console.error("✗ " + msg);
      }
      return 1;
    }
    throw err;
  }
}

function runAttachInner(opts) {
  const o = opts || {};
  const detach = !!o.detach;
  const mode = detach ? "detach" : "attach";
  const apply = !!(o.yes || process.env.CLAUDELINK_AUTO_ATTACH === "1");
  const json = !!o.json;
  const quiet = !!o.quiet;

  const result = {
    schema: 1, mode, applied: apply, server: serverCommand(),
    dataDir: dataDir(PKG_ROOT), ledger: ledgerPath(), agents: [], error: "", failures: 0,
  };

  const block = refusalReason(os.userInfo(), process.env);
  if (block) {
    result.error = block;
    if (json) console.log(JSON.stringify(result, null, 2));
    else console.error("✗ " + block);
    return 2;
  }

  const registry = loadAgents();
  const only = parseOnly(o.only);
  if (only) {
    const known = new Set(registry.map((a) => a.id));
    const bad = Array.from(only).filter((id) => !known.has(id));
    if (bad.length) {
      const msg = "注册表里没有这些 Agent id：" + bad.join(", ") + "（用 claudelink-hub detect 查看已识别的成员）";
      result.error = msg;
      if (json) console.log(JSON.stringify(result, null, 2));
      else console.error("✗ " + msg);
      return 2;
    }
  }
  const cfg = loadConfig(configFileOf(o), path.join(PKG_ROOT, "config.example.json"));
  const found = detect(registry, { config: cfg });
  const byId = new Map(found.map((a) => [a.id, a]));
  const server = serverCommand();
  const added = new Map();
  const removed = new Set();
  let failures = 0;

  const rows = [];
  const missing = [];
  for (const agent of registry) {
    if (only && !only.has(agent.id)) continue;
    const hit = byId.get(agent.id);
    const row = {
      id: agent.id, label: agent.label, state: "", cmd: null, display: "",
      resolved: hit ? hit.path : "", note: "", verified: null,
    };
    rows.push(row);

    if (!hit) { row.state = "missing"; row.note = "未安装 / 未识别：不执行任何命令"; missing.push(agent.id); continue; }
    if (!hit.usable) { row.state = "unsupported"; row.note = hit.note || "不在支持列表里：不执行任何命令"; continue; }

    const meta = mcpMeta(hit);
    if (!meta) {
      row.state = "no-mcp";
      row.note = (hit.note ? hit.note + "；" : "") + "没有 mcp 元数据：未支持自动接入，只给手动步骤";
      continue;
    }
    if (detach && (!Array.isArray(meta.remove) || !meta.remove.length)) {
      row.state = "no-mcp";
      row.note = "没有 mcp remove 元数据：不能自动移除，只给手动步骤";
      continue;
    }

    // 1) 先问 list：既做「是否已接入」判定，也是唯一允许的读取操作
    const listed = exec(argvFor(hit, tmplArgs(meta.list, server)));
    row.listCommand = listed.display;
    if (!listed.ok) {
      row.state = "unknown";
      row.note = "mcp list " + (listed.timedOut ? "超时" : "失败") + "（" + fmtStatus(listed.status) +
        "）：状态未知，未做任何改动（别把它当「未接入」重复注册）";
      continue;
    }
    const p = parseList(listed.out);
    row.listed = p;

    const actionArgs = detach ? tmplArgs(meta.remove, server) : tmplArgs(meta.add, server);
    row.cmd = argvFor(hit, actionArgs);
    row.display = displayArgv(row.cmd);

    if (!detach) {
      if (p.found && p.attached) { row.state = "attached"; row.note = "已接入（跳过，幂等）"; continue; }
      if (p.found) {
        row.state = "stale";
        row.note = "已存在同名条目但命令不是 ClaudeLink（" + p.command + "）：不敢改，先 --detach 或手工修正";
        continue;
      }
      row.state = "pending";
      row.note = apply ? "" : "将执行（dry-run）";
    } else {
      if (!p.found) { row.state = "skipped"; row.note = "未接入（跳过）"; continue; }
      if (!p.attached) {
        row.state = "stale";
        row.note = "同名条目命令不是 ClaudeLink（" + p.command + "）：不敢删，请手工处理";
        continue;
      }
      row.state = "pending";
      row.note = apply ? "" : "将执行（dry-run）";
    }

    // 2) 真正执行（只有 --yes / CLAUDELINK_AUTO_ATTACH=1 会走到这里）
    if (!apply) continue;
    const res = exec(row.cmd);
    if (!res.ok) {
      row.state = "failed";
      row.note = "失败（" + fmtStatus(res.status) + (res.timedOut ? "，超时" : "") + "）：" + res.display;
      row.error = lastLines(res.out, 3);
      failures++;
      continue;
    }
    row.state = detach ? "detached" : "added";
    if (detach) { removed.add(row.id); added.delete(row.id); }
    else { added.set(row.id, { agent: row.id, cmd: res.display, argv: row.cmd, at: new Date().toISOString(), server }); removed.delete(row.id); }

    // 3) 事后复核（list 一次）：把「命令说成功但实际没写进去」抓出来
    const after = exec(argvFor(hit, tmplArgs(meta.list, server)));
    row.verified = after.ok ? parseList(after.out).attached : null;
    if (row.verified === false) {
      row.note = "命令返回成功，但复核没看到 claudelink 条目（可能被 CLI 静默忽略）";
      failures++;
    }
  }

  if (added.size || removed.size) {
    try {
      const c = commitLedger(added, removed);
      if (c.ok) result.ledger = c.path;
      else {
        result.ledgerError = c.reason === "lock"
          ? ((c.error && c.error.message) || "账本被另一个进程占用")
          : "账本在写入期间被反复修改，已放弃写入";
        failures++;
      }
    } catch (err) { result.ledgerError = err.message; failures++; }
  }

  result.failures = failures;
  result.counts = countStates(rows);
  result.agents = rows;

  if (json) {
    console.log(JSON.stringify(result, null, 2));
    return failures ? 1 : 0;
  }
  if (!quiet) printHuman(result, rows, missing, apply, detach);
  return failures ? 1 : 0;
}

module.exports = {
  runAttach, parseList, mcpMeta, tmplArgs, launcherFor, argvFor, refusalReason,
  ledgerPath, readLedger, commitLedger, serverCommand, ENTRY_RE,
};

// ── 独立运行：node bin/attach.js [--detach] [--yes] [--json] [--only a,b] ──
if (require.main === module) {
  const argv = process.argv.slice(2);
  const opts = { _: [] };
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a === "--yes" || a === "-y") opts.yes = true;
    else if (a === "--detach") opts.detach = true;
    else if (a === "--json") opts.json = true;
    else if (a === "--quiet") opts.quiet = true;
    else if (a === "--dry-run") opts.yes = false;
    else if (a === "--only") opts.only = argv[++i];
    else if (a === "--config") opts.config = argv[++i];
    else opts._.push(a);
  }
  try {
    process.exitCode = runAttach(opts);
  } catch (err) {
    console.error(err && err.stack || err);
    process.exitCode = 1;
  }
}
