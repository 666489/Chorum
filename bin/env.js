#!/usr/bin/env node
"use strict";
/**
 * 用户级 CLAUDELINK_* 变量管理（T6）。
 *
 * 边界（硬约束）：
 *   - 只允许 CLAUDELINK_ 前缀；第三方 KEY（OPENAI_API_KEY 等）一律拒绝，本工具不接管。
 *   - 不写系统级环境变量（不调 setx / 不写 /etc）：只写 claudelink 自己的
 *     ~/.claudelink/env.json，由 cli.js 启动时加载（真实环境变量优先，绝不被覆盖）。
 *   - 写入前先展示 diff；没有 --yes 只打印，不落盘。
 */
const fs = require("fs");
const os = require("os");
const path = require("path");
const { expand, readJson, updateJsonLocked } = require("./util");

const PREFIX = "CLAUDELINK_";
const KEY_RE = /^CLAUDELINK_[A-Z0-9_]*[A-Z0-9]$/i;

function isAllowed(key) {
  return KEY_RE.test(String(key || ""));
}

function envFilePath() {
  if (process.env.CLAUDELINK_ENV_FILE) return path.resolve(expand(process.env.CLAUDELINK_ENV_FILE));
  return path.join(os.homedir(), ".claudelink", "env.json");
}

function readEnvFile() {
  const d = readJson(envFilePath(), null);
  if (!d || typeof d !== "object" || Array.isArray(d)) return {};
  const out = {};
  for (const [k, v] of Object.entries(d)) {
    if (isAllowed(k) && typeof v === "string") out[k] = v;
  }
  return out;
}

/** 启动时调用：把 env.json 里的值补进 process.env（**真实环境变量优先**）。 */
function loadManagedEnv() {
  let file = {};
  try { file = readEnvFile(); } catch { return {}; }
  for (const [k, v] of Object.entries(file)) {
    if (v && process.env[k] === undefined) process.env[k] = v;
  }
  return file;
}

/** 当前所有 CLAUDELINK_* 变量及其来源。 */
function listVars() {
  const file = readEnvFile();
  const names = new Set([...Object.keys(file)]);
  for (const k of Object.keys(process.env)) if (isAllowed(k)) names.add(k);
  const rows = [];
  for (const name of Array.from(names).sort()) {
    const fromEnv = process.env[name] !== undefined && process.env[name] !== "";
    const fromFile = Object.prototype.hasOwnProperty.call(file, name);
    if (!fromEnv && !fromFile) continue;
    rows.push({
      name,
      value: mask(fromEnv ? process.env[name] : file[name]),
      source: fromEnv ? "环境变量" : "env.json",
    });
  }
  return rows;
}

/** 打码：变量值可能是路径，只露头尾，避免日志外泄。 */
function mask(v) {
  const s = String(v == null ? "" : v);
  if (s.length <= 12) return s;
  return s.slice(0, 6) + "…" + s.slice(-4);
}

/** 在给定内容上应用 --set/--unset，返回 {next, changes, error}。 */
function applyChanges(current, setList, unsetList) {
  const next = Object.assign({}, current || {});
  const changes = [];
  for (const item of setList || []) {
    const idx = String(item).indexOf("=");
    const key = idx < 0 ? String(item) : String(item).slice(0, idx);
    const value = idx < 0 ? "" : String(item).slice(idx + 1);
    if (!isAllowed(key)) return { error: "只允许 CLAUDELINK_* 变量（收到 " + key + "）；第三方 KEY 请在你自己的环境里配置，本工具不接管。" };
    const old = Object.prototype.hasOwnProperty.call(next, key) ? next[key] : "";
    if (!value) { delete next[key]; changes.push({ name: key, from: old, to: "" }); }
    else { next[key] = value; changes.push({ name: key, from: old, to: value }); }
  }
  for (const key of unsetList || []) {
    if (!isAllowed(key)) return { error: "只允许 CLAUDELINK_* 变量（收到 " + key + "）。" };
    const old = Object.prototype.hasOwnProperty.call(next, key) ? next[key] : "";
    changes.push({ name: key, from: old, to: "" });
    delete next[key];
  }
  return { next, changes, error: "" };
}

/** 以磁盘当前内容为基准算 diff（dry-run 展示用）。 */
function computeChanges(setList, unsetList) {
  return applyChanges(readEnvFile(), setList, unsetList);
}

/** 锁内「读最新 → 只改本次涉及的键 → 写」，避免两个 env 命令互相覆盖。 */
function applyLocked(setList, unsetList) {
  return updateJsonLocked(envFilePath(), (cur) => {
    const base = (cur && typeof cur === "object" && !Array.isArray(cur)) ? cur : {};
    const res = applyChanges(base, setList, unsetList);
    return res.next;
  }, {});
}

function printShow(json) {
  const file = envFilePath();
  const vars = listVars();
  if (json) {
    console.log(JSON.stringify({ file, exists: fs.existsSync(file), vars }, null, 2));
    return 0;
  }
  console.log("== CLAUDELINK_* 变量（用户级）==");
  console.log("文件      : " + file + (fs.existsSync(file) ? "（存在）" : "（不存在）"));
  if (!vars.length) {
    console.log("（没有任何 CLAUDELINK_* 变量）");
    return 0;
  }
  const w = Math.max(...vars.map((v) => v.name.length));
  for (const v of vars) console.log("  " + v.name.padEnd(w) + "  = " + (v.value || "(空)") + "    [" + v.source + "]");
  return 0;
}

/**
 * env 子命令入口。
 * opts: { show, set:[], unset:[], yes, json }
 */
function runEnv(opts) {
  const o = opts || {};
  const json = !!o.json;
  const setList = o.set || [];
  const unsetList = o.unset || [];
  if (!setList.length && !unsetList.length) return printShow(json);

  const { next, changes, error } = computeChanges(setList, unsetList);
  if (error) {
    if (json) console.log(JSON.stringify({ error }, null, 2));
    else console.error("✗ " + error);
    return 2;
  }
  const applied = !!o.yes && changes.length > 0;

  if (json) {
    console.log(JSON.stringify({ file: envFilePath(), changes, applied }, null, 2));
    if (applied) applyLocked(setList, unsetList);
    return 0;
  }

  console.log("== chorum env ==");
  console.log("文件      : " + envFilePath());
  console.log("--");
  if (!changes.length) {
    console.log("无变化。");
    return 0;
  }
  for (const c of changes) {
    console.log("  " + c.name + ": " + (c.from || "(未设)") + " → " + (c.to || "(删除)"));
  }
  console.log("--");
  if (!applied) {
    console.log("dry-run：未写入（加 --yes 生效）。");
    return 0;
  }
  const wrote = applyLocked(setList, unsetList);
  if (!wrote.ok) {
    console.error("✗ 写入失败：" + (wrote.reason === "lock"
      ? ((wrote.error && wrote.error.message) || "文件被另一个进程占用")
      : "env.json 在写入期间被反复修改，请稍后重试"));
    return 1;
  }
  console.log("已写入（" + changes.length + " 项）。");
  console.log("提示：本工具只写自己的 env.json（新开的 chorum 进程生效）；");
  console.log("      若要让别的程序也看到，请在你自己的 shell/profile 里设置，本工具不碰系统环境变量。");
  return 0;
}

module.exports = {
  runEnv, loadManagedEnv, envFilePath, readEnvFile, isAllowed, listVars, computeChanges, applyChanges,
};

if (require.main === module) {
  const argv = process.argv.slice(2);
  const opts = { set: [], unset: [] };
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a === "--show") opts.show = true;
    else if (a === "--yes" || a === "-y") opts.yes = true;
    else if (a === "--json") opts.json = true;
    else if (a === "--set") opts.set.push(argv[++i]);
    else if (a === "--unset") opts.unset.push(argv[++i]);
  }
  try {
    process.exitCode = runEnv(opts);
  } catch (err) {
    console.error(err && err.stack || err);
    process.exitCode = 1;
  }
}
