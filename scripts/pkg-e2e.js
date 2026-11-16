#!/usr/bin/env node
"use strict";
/**
 * 打包端到端（npm run pkg-e2e）：
 *   npm pack → 装到临时 prefix（模拟 npm i -g）→ 断言文件齐全 → 跑安装后的 doctor --json
 *   → 断言 postinstall 写了检测缓存。
 *
 * 全程只碰系统临时目录；不写你的 ~/.claudelink（用 CLAUDELINK_HOME/CLAUDELINK_AGENTS 重定向）。
 */
const fs = require("fs");
const os = require("os");
const path = require("path");
const { run, IS_WIN } = require(path.join(__dirname, "..", "bin", "util"));

const ROOT = path.resolve(__dirname, "..");
const pkg = require(path.join(ROOT, "package.json"));
const TMP = fs.mkdtempSync(path.join(os.tmpdir(), "clh-pkge2e-"));
const PACK = path.join(TMP, "pack");
const PREFIX = path.join(TMP, "prefix");
const HOME = path.join(TMP, "home");
for (const d of [PACK, PREFIX, HOME]) fs.mkdirSync(d, { recursive: true });

let failures = 0;
const check = (name, cond, detail) => {
  if (cond) console.log("  ✓ " + name);
  else { failures++; console.log("  ✗ " + name + (detail ? "  → " + detail : "")); }
};

function cleanupTmp() {
  try { fs.rmSync(TMP, { recursive: true, force: true, maxRetries: 6, retryDelay: 250 }); } catch { /* ignore */ }
}
process.on("exit", cleanupTmp);

function done(code) {
  cleanupTmp();
  process.exitCode = code;
}

console.log("== 1) npm pack ==");
const packed = run("npm", ["pack", "--pack-destination", PACK], { timeout: 180000, cwd: ROOT });
if (!packed.ok) {
  console.error((packed.out || "").split(/\r?\n/).slice(-10).join("\n"));
  check("npm pack 成功", false);
  return done(1);
}
const tgz = fs.readdirSync(PACK).find((f) => f.endsWith(".tgz"));
check("生成 tgz", !!tgz, tgz);
if (!tgz) return done(1);

console.log("== 2) 安装到临时 prefix（等同 npm i -g）==");
const env = Object.assign({}, process.env, {
  CLAUDELINK_HOME: HOME,
  CLAUDELINK_AGENTS: path.join(HOME, "agents.detected.json"),
});
const install = run("npm", ["i", "-g", "--prefix", PREFIX, path.join(PACK, tgz)], { timeout: 300000, env });
if (!install.ok) {
  console.error((install.out || "").split(/\r?\n/).slice(-15).join("\n"));
  check("安装成功", false);
  return done(1);
}
// 安装后**先**查 postinstall 的产物（doctor 自己也会写缓存，不能拿它当证据）
const cacheFile = path.join(HOME, "agents.detected.json");
console.log("== 2b) postinstall 的独立产物（此时还没跑 doctor）==");
check("检测缓存由 postinstall 写出", fs.existsSync(cacheFile), cacheFile);
if (fs.existsSync(cacheFile)) {
  try {
    const cache = JSON.parse(fs.readFileSync(cacheFile, "utf8"));
    check("缓存里有 Agent 条目", cache && cache.byId && Object.keys(cache.byId).length > 0);
  } catch (err) {
    check("缓存可解析", false, err.message);
  }
}

console.log("== 2c) 默认不自动接入（T7）==");
const stateFile = path.join(HOME, "attach-state.json");
check("安装后没有 attach-state.json（默认只提示，不动第三方配置）", !fs.existsSync(stateFile), stateFile);

console.log("== 3) 安装后的关键文件 ==");
// POSIX 上全局包在 <prefix>/lib/node_modules；Windows 在 <prefix>/node_modules
const modDir = path.join(PREFIX, IS_WIN ? "node_modules" : path.join("lib", "node_modules"), pkg.name);
check("安装目录存在", fs.existsSync(modDir), modDir);
for (const f of ["hub.py", "hub_pid.py", "hub_tui.py", "tui_workspace.py", "hub_connection.py", "extract.py", "runtime.py", "codex_cli.py", "workflows.py", "docs/upgrade-0.3.md", "web/index.html", "web/workbench.js", "web/workbench.css",
                 "config.example.json", "bin/cli.js", "bin/postinstall.js", "bin/registry.js",
                 "bin/attach.js", "bin/env.js"]) {
  check(f, fs.existsSync(path.join(modDir, f)));
}
check("包里不含 config.json", !fs.existsSync(path.join(modDir, "config.json")));
check("包里不含 hub.db", !fs.existsSync(path.join(modDir, "hub.db")));
check("包里不含 scripts/（守门工具不发布）", !fs.existsSync(path.join(modDir, "scripts")));

console.log("== 4) 从安装位置跑 doctor --json ==");
// bin 名从 package.json 取：改名（claudelink-hub → chorum）时不该再改一次测试
const primaryBin = Object.keys(pkg.bin || {})[0] || "chorum";
const binName = IS_WIN ? primaryBin + ".cmd" : path.join("bin", primaryBin);
const binPath = path.join(PREFIX, binName);
check("bin 存在（" + primaryBin + "）", fs.existsSync(binPath), binPath);
const doc = run(binPath, ["doctor", "--json"], { timeout: 120000, env, cwd: TMP });
let data = null;
try { data = JSON.parse(doc.out); } catch { /* 下面统一报 */ }
check("doctor --json 输出可解析", !!data, (doc.out || "").slice(0, 160));
if (data) {
  check("doctor.ok 为 true 或至少识别到 Agent",
    data.ok === true || (Array.isArray(data.agents) && data.agents.length >= 1),
    JSON.stringify({ ok: data.ok, agents: (data.agents || []).length, unsupported: data.unsupported }));
  check("数据目录已切到 CLAUDELINK_HOME", path.resolve(data.dataDir || "") === path.resolve(HOME), data.dataDir);
  check("doctor 报告里有 Python", !!data.python, JSON.stringify(data.python));
}

console.log("== 5) 缓存仍完好（doctor 之后）==");
check("缓存文件仍在", fs.existsSync(cacheFile), cacheFile);

console.log("\n" + (failures ? "✗ pkg-e2e 失败 " + failures + " 项" : "✅ pkg-e2e 通过"));
done(failures ? 1 : 0);
