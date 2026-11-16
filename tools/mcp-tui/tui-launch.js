"use strict";
/**
 * Chorum 终端版启动器（零依赖）。
 *
 * 职责：解析「中枢目录 / Python 解释器 / hub_tui.py」，然后在新控制台窗口里启动。
 * 直接运行会打印解析结果与逐项尝试记录，便于诊断：
 *   node dist/tui-launch.js            # 只解析，不开窗口
 *   node dist/tui-launch.js --open     # 解析并开窗口
 *
 * 解析原则：**不写死任何机器路径**。
 * 以前这里把 `D:\AI-GroupChat\claudelink-hub` 和 `D:\Anaconda3\envs\py11\python.exe`
 * 当成兜底常量，项目一搬迁（D: → F:）就表现为「终端版打不开」，而错误信息只说找不到
 * hub_tui.py，看不出是路径过期。现在按「环境变量 → 注册表 → 同级安装包 → npm 全局根 →
 * 当前目录上溯」逐层解析，并把每一层试过什么都打印出来。
 */
const fs = require("fs");
const os = require("os");
const path = require("path");
const { spawn, spawnSync } = require("child_process");

// 中枢的包名候选（改名前后都认，避免升级期互相看不见）：npm 包名 = chorum
const HUB_PKG_NAMES = ["chorum", "chorum-hub", "claudelink-hub"];

function exists(p) {
  try { return !!p && fs.existsSync(p); } catch { return false; }
}

function isHubDir(dir) {
  return exists(dir) && exists(path.join(dir, "hub.py")) && exists(path.join(dir, "hub_tui.py"));
}

function packageRoot() {
  return path.resolve(__dirname, "..");
}

/** 同级安装包：全局装的时候 claudelink 与中枢是 node_modules 里的兄弟目录。 */
function siblingCandidates() {
  const parent = path.dirname(packageRoot());
  return HUB_PKG_NAMES.map((name) => path.join(parent, name));
}

let _npmRootCache;
function npmGlobalRoot() {
  if (_npmRootCache !== undefined) return _npmRootCache;
  const env = process.env.CLAUDELINK_NPM_ROOT;
  if (env && exists(env)) return (_npmRootCache = env);
  try {
    const res = spawnSync("npm", ["root", "-g"], {
      encoding: "utf8", timeout: 8000, windowsHide: true,
      shell: process.platform === "win32",
    });
    const line = String(res.stdout || "").split(/\r?\n/).map((s) => s.trim()).filter(Boolean).pop();
    _npmRootCache = line && exists(line) ? line : "";
  } catch {
    _npmRootCache = "";
  }
  return _npmRootCache;
}

function npmRootCandidates() {
  const root = npmGlobalRoot();
  return root ? HUB_PKG_NAMES.map((name) => path.join(root, name)) : [];
}

/** 当前目录及其上溯：从源码目录启动时（node tools/mcp-tui/tui-launch.js）用得上。 */
function cwdCandidates() {
  const out = [];
  let dir = process.cwd();
  for (let i = 0; i < 6; i++) {
    out.push(dir);
    const next = path.dirname(dir);
    if (next === dir) break;
    dir = next;
  }
  return out;
}

/** 启动器自身所在目录的上溯：源码树里运行时，中枢就是本仓库根目录。 */
function launcherCandidates() {
  const out = [];
  let dir = path.resolve(__dirname);
  for (let i = 0; i < 4; i++) {
    out.push(dir);
    const next = path.dirname(dir);
    if (next === dir) break;
    dir = next;
  }
  return out;
}

function readRegistryConfig() {
  const file = path.join(os.homedir(), ".claudelink", "hub.json");
  try {
    return JSON.parse(fs.readFileSync(file, "utf8")) || {};
  } catch {
    return {};
  }
}

function hubDir(trace) {
  const tried = [];
  const sink = trace || [];
  const first = (label, cands) => {
    for (const c of cands) {
      if (!c) continue;
      const dir = path.resolve(c);
      const ok = isHubDir(dir);
      tried.push({ layer: label, path: dir, ok });
      if (ok) return dir;
    }
    return null;
  };

  const layers = [
    ["env CLAUDELINK_HUB_DIR", [process.env.CLAUDELINK_HUB_DIR]],
    ["~/.claudelink/hub.json", [readRegistryConfig().dir]],
    ["启动器所在目录上溯", launcherCandidates()],
    ["同级安装包", siblingCandidates()],
    ["npm 全局根", npmRootCandidates()],
    ["当前目录上溯", cwdCandidates()],
  ];
  for (const [label, cands] of layers) {
    const before = tried.length;
    const hit = first(label, cands);
    sink.push(...tried.slice(before));
    if (hit) return hit;
  }
  return null;
}

function pythonWorks(cmd) {
  try {
    const res = spawnSync(cmd, ["-c", "import sys,sqlite3;print(sys.version_info[:2]>=(3,9))"], {
      encoding: "utf8", timeout: 4000, windowsHide: true,
    });
    return res.status === 0 && String(res.stdout || "").trim().endsWith("True");
  } catch {
    return false;
  }
}

function pythonExe(hub) {
  const env = process.env.CLAUDELINK_PYTHON;
  if (exists(env)) return env;
  const local = [
    hub ? path.join(hub, ".venv", "Scripts", "python.exe") : null,
    hub ? path.join(hub, ".venv", "bin", "python") : null,
    hub ? path.join(hub, "venv", "Scripts", "python.exe") : null,
    hub ? path.join(hub, "venv", "bin", "python") : null,
  ].filter(Boolean);
  for (const p of local) if (exists(p) && pythonWorks(p)) return p;
  // PATH 上的解释器要真的探一次：Windows Store 的 python3 是个会弹商店的存根。
  for (const p of (process.platform === "win32" ? ["python", "python3"] : ["python3", "python"])) {
    if (pythonWorks(p)) return p;
  }
  return process.platform === "win32" ? "python" : "python3";
}

function tuiScript(hub) {
  const env = process.env.CLAUDELINK_TUI;
  if (exists(env)) return env;
  if (hub && exists(path.join(hub, "hub_tui.py"))) return path.join(hub, "hub_tui.py");
  const bundled = path.join(packageRoot(), "tui", "hub_tui.py");
  if (exists(bundled)) return bundled;
  return null;
}

function resolve(trace) {
  const sink = [];
  const hub = hubDir(sink);
  const script = tuiScript(hub);
  if (trace) trace.push(...sink);
  if (!script) {
    const lines = sink.map((t) => "  " + (t.ok ? "✓" : "✗") + " [" + t.layer + "] " + t.path);
    throw new Error(
      "找不到 hub_tui.py。中枢目录按下列顺序解析均未命中：\n" + (lines.join("\n") || "  （无候选）") +
      "\n任选一种方式修正：\n" +
      "  1) 设 CLAUDELINK_HUB_DIR 指向含 hub.py 的中枢目录；\n" +
      "  2) 写 ~/.claudelink/hub.json：{\"dir\": \"<中枢目录>\"}；\n" +
      "  3) 或设 CLAUDELINK_TUI 直接指向 hub_tui.py。"
    );
  }
  return { hub: hub || path.dirname(script), python: pythonExe(hub), script };
}

function openTerminal() {
  const info = resolve();
  const title = "Chorum TUI";
  if (process.platform === "win32") {
    const child = spawn(process.env.ComSpec || "cmd.exe",
      ["/c", "start", title, info.python, info.script],
      { detached: true, stdio: "ignore", cwd: info.hub, windowsHide: false });
    child.unref();
  } else if (process.platform === "darwin") {
    const cmd = '"' + info.python + '" "' + info.script + '"';
    const child = spawn("osascript",
      ["-e", 'tell application "Terminal" to do script ' + JSON.stringify(cmd)],
      { detached: true, stdio: "ignore", cwd: info.hub });
    child.unref();
  } else {
    const child = spawn("x-terminal-emulator", ["-e", info.python, info.script],
      { detached: true, stdio: "ignore", cwd: info.hub });
    child.unref();
  }
  return { ok: true, hub: info.hub, python: info.python, script: info.script, title };
}

module.exports = { openTerminal, resolve, hubDir, pythonExe, tuiScript, HUB_PKG_NAMES };

if (require.main === module) {
  try {
    const trace = [];
    const info = resolve(trace);
    const payload = { ...info, trace };
    if (process.argv.includes("--open")) payload.opened = openTerminal();
    console.log(JSON.stringify(payload, null, 2));
  } catch (err) {
    console.error(String((err && err.message) || err));
    process.exit(1);
  }
}
