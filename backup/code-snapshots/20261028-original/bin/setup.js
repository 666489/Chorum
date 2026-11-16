"use strict";
const fs = require("fs");
const path = require("path");
const { loadAgents } = require("./registry");
const { expand, updateJsonLocked } = require("./util");

const SKELETON = {
  server: { host: "127.0.0.1", port: 8787, allow_shutdown: true },
  workspace: { cwd: "" },
  agents: {},
  relay: { enabled: true, max_depth: 2 },
  dispatch: { mode: "sequential", order: [], planner: "" },
  limits: { turn_timeout_sec: 420, max_output_chars: 0 },
  context: { history_limit: 40 },
  streaming: { min_interval_ms: 80, min_chars: 8, live_max_chars: 0 },
  trace: { enabled: true, max_thinking_chars: 0, file_roots: [] },
  claudelink: { mirror: true },
};

function readJson(file, fallback) {
  try {
    return JSON.parse(fs.readFileSync(file, "utf8"));
  } catch {
    return fallback;
  }
}

function loadConfig(configPath, examplePath) {
  if (fs.existsSync(configPath)) return readJson(configPath, SKELETON);
  if (examplePath && fs.existsSync(examplePath)) return readJson(examplePath, SKELETON);
  return JSON.parse(JSON.stringify(SKELETON));
}

/**
 * 把注册表里的 generic「声明式模板」翻译成 hub.py 的 run_generic_cli 认的 schema：
 *   模板  promptVia / promptFlag / modelFlag / sessionArgs / resumeArgs / streamArgs / jsonLines
 *   → hub  args / stdin / model_args / create_args / resume_args / output / text_field / list_field / session_field
 * 这样「用户按文档写模板」和「hub.py 实际执行」就是同一套语义。
 */
function translateGeneric(spec) {
  const s = spec || {};
  const out = {};
  const via = String(s.promptVia || "arg").toLowerCase();
  const flag = Array.isArray(s.promptFlag) ? s.promptFlag : (s.promptFlag ? [s.promptFlag] : ["-p"]);

  const args = [];
  if (Array.isArray(s.streamArgs)) args.push(...s.streamArgs);
  if (via === "arg") args.push(...flag, "{prompt}");
  out.args = args;
  // hub 的 generic runner 没有「env 传 prompt」，用 stdin 等价表达（最通用）
  if (via === "stdin" || via === "env") out.stdin = true;

  if (s.modelFlag) out.model_args = [s.modelFlag, "{model}"];

  const sess = Array.isArray(s.sessionArgs) ? s.sessionArgs : (s.sessionArgs ? [s.sessionArgs] : []);
  if (sess.length) {
    out.create_args = sess;
    out.resume_args = Array.isArray(s.resumeArgs) ? s.resumeArgs : sess;
  }
  out.output = s.jsonLines ? "jsonl" : (s.output || "text");
  for (const k of ["text_field", "list_field", "session_field"]) if (s[k]) out[k] = s[k];
  return out;
}

function defaultsFor(agent) {
  return Object.assign({ enabled: true, relay: true, stream: true }, agent.defaults || {}, {
    label: (agent.defaults && agent.defaults.label) || agent.label,
  });
}

function patchConfig(config, detected, opts = {}) {
  config.agents = config.agents || {};
  const byId = new Map(detected.map((a) => [a.id, a]));
  const enabled = [];
  const disabled = [];
  const unsupported = [];
  // 用「加载后的注册表」（内置 + 用户自定义），这样用户加的 Agent 也能被装配
  const registry = opts.registry || loadAgents();

  for (const agent of registry) {
    const found = byId.get(agent.id);
    const cur = config.agents[agent.id];
    const supported = agent.support === "builtin" || (agent.support === "generic" && !!agent.generic);
    if (found && supported) {
      // 区分「用户 pin」与「setup 自动写入」：
      //   - 用户手写、存在、且没有 exe_auto 标记 → 保留（不覆盖）
      //   - **占位符形态（%VAR% / ~）一律视为用户意图**：保留原样，不能被改写成绝对路径
      //   - setup 自己写过的（exe_auto:true）→ 允许跟随检测更新（Codex 版本哈希目录升级就是这种）
      const curExe = cur && typeof cur.exe === "string" ? cur.exe.trim() : "";
      const hasPlaceholder = /%[^%]+%|^~[\\/]/.test(curExe);
      const expandedExe = curExe && !hasPlaceholder ? expand(curExe) : "";
      const userPinned = !!curExe && !(cur && cur.exe_auto) &&
        (hasPlaceholder || (!!expandedExe && fs.existsSync(expandedExe)));
      const next = Object.assign({}, defaultsFor(agent), cur || {}, {
        enabled: true,
        exe: userPinned ? curExe : found.path,
        exe_auto: !userPinned,
      });
      if (agent.support === "generic") {
        const spec = translateGeneric(agent.generic);
        next.runner = "generic";
        next.generic = spec;
        next.supports_native_memory = !!(
          (spec.resume_args && spec.resume_args.length) ||
          (spec.create_args && spec.create_args.length) ||
          spec.session_field
        );
      }
      if (agent.node && !next.node) {
        next.node = found.node || process.execPath;
      }
      config.agents[agent.id] = next;
      enabled.push(agent.id);
    } else if (cur) {
      if (cur.enabled !== false) {
        cur.enabled = false;
        disabled.push(agent.id);
      }
    }
    if (found && !supported) unsupported.push({ id: agent.id, reason: agent.note || "未支持" });
  }

  // 保留用户既有的执行顺序（只丢掉已不存在的成员），新成员按注册表顺序追加
  const prevOrder = (config.dispatch && Array.isArray(config.dispatch.order)) ? config.dispatch.order : [];
  const order = prevOrder.filter((n) => config.agents[n]);
  for (const agent of registry) if (config.agents[agent.id] && !order.includes(agent.id)) order.push(agent.id);
  for (const name of Object.keys(config.agents)) if (!order.includes(name)) order.push(name);
  config.dispatch = config.dispatch || {};
  config.dispatch.order = order;
  if (!config.dispatch.mode) config.dispatch.mode = "sequential";

  if (opts.port) {
    config.server = config.server || {};
    config.server.port = Number(opts.port);
  }
  return { config, enabled, disabled, unsupported };
}

function timestamp() {
  const d = new Date();
  const p = (n) => String(n).padStart(2, "0");
  return d.getFullYear() + p(d.getMonth() + 1) + p(d.getDate()) + "-" +
    p(d.getHours()) + p(d.getMinutes()) + p(d.getSeconds()) + "-" +
    String(d.getMilliseconds()).padStart(3, "0");
}

function backup(configPath) {
  if (!fs.existsSync(configPath)) return "";
  const dst = configPath + ".bak-detect-" + timestamp();
  fs.copyFileSync(configPath, dst);
  return dst;
}

function writeConfig(configPath, config) {
  const tmp = configPath + ".tmp-" + process.pid;   // 并发 setup 不互相覆盖
  fs.writeFileSync(tmp, JSON.stringify(config, null, 2) + "\n", "utf8");
  fs.renameSync(tmp, configPath);
}

/**
 * 装配并**安全写入** config.json：文件锁 + 写前 CAS 重试。
 * 两个 claudelink-hub 进程同时 setup / start 时，后跑的那个会基于磁盘最新内容重算，
 * 而不是拿旧快照覆盖（旧的 writeConfig 是「读一次 → 直接写」）。
 * 返回 { ok, attempts, unchanged?, reason?, res, backup }
 */
function writeSetupConfig(file, examplePath, found, opts = {}) {
  let res = null;
  let bak = "";
  const out = updateJsonLocked(file, (cur) => {
    // 每轮重试都用「刚读到的内容」重新装配：patchConfig 是幂等的
    const base = cur || loadConfig(file, examplePath);
    res = patchConfig(base, found, opts);
    return res.config;
  }, {
    attempts: opts.attempts || 3,
    timeoutMs: opts.timeoutMs,
    onBeforeWrite: () => { if (!bak) bak = backup(file); },
  });
  return Object.assign({}, out, { res, backup: bak });
}

module.exports = { SKELETON, loadConfig, patchConfig, backup, writeConfig, writeSetupConfig, defaultsFor, translateGeneric };
