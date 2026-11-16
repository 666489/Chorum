#!/usr/bin/env node
"use strict";
/**
 * API 模型端到端（npm run e2e:model）：
 *   mock provider（独立进程） ← 中枢 ← CLI（doctor → model add → ask → status → stop）
 *
 * 验证「命令行直接接 API 模型并跑完一次工具调用」这条链路，不花真钱、不写用户 HOME：
 *   1. 独立进程起 mock provider（第 1 次请求给工具调用，第 2 次给正文）；
 *   2. 在临时数据目录里装配 config，`model add` 一个 openai 模型、一个 anthropic 模型；
 *   3. `ask` 发一条消息，断言：正文进 stdout、工具步骤进 stderr、
 *      provider 真的收到两次请求且首次带 tools（工具调用是发起并完成的，不是被吞掉）；
 *   4. `model list` / `status` 有对应输出，最后 `stop`。
 *
 * 找不到 Python 3.9+ 时以 SKIP 结束（中枢起不来，链路没法验）。
 */
const fs = require("fs");
const os = require("os");
const net = require("net");
const path = require("path");
const http = require("http");
const cp = require("child_process");
const { findPython } = require(path.join(__dirname, "..", "bin", "util"));

const ROOT = path.resolve(__dirname, "..");
const TMP = fs.mkdtempSync(path.join(os.tmpdir(), "chorum-e2e-"));
let failures = 0;
const check = (name, cond, detail) => {
  if (cond) console.log("  ✓ " + name);
  else { failures++; console.log("  ✗ " + name + (detail ? "  → " + detail : "")); }
};

let mock = null;
function cleanup() {
  try { if (mock) mock.kill(); } catch { /* 已退出 */ }
  try { fs.rmSync(TMP, { recursive: true, force: true }); } catch { /* Windows 上可能正被占用 */ }
}
process.on("exit", cleanup);

function freePort() {
  return new Promise((resolve, reject) => {
    const srv = net.createServer();
    srv.once("error", reject);
    srv.listen(0, "127.0.0.1", () => { const p = srv.address().port; srv.close(() => resolve(p)); });
  });
}

function getJson(url) {
  return new Promise((resolve) => {
    const req = http.get(url, { timeout: 3000 }, (res) => {
      let body = "";
      res.setEncoding("utf8");
      res.on("data", (c) => { body += c; });
      res.on("end", () => { try { resolve(JSON.parse(body)); } catch { resolve(null); } });
    });
    req.on("timeout", () => { req.destroy(); resolve(null); });
    req.on("error", () => resolve(null));
  });
}

async function startMock(port) {
  const child = cp.spawn(process.execPath, [path.join(__dirname, "mock-provider.js"), "--port", String(port)], {
    cwd: ROOT, stdio: "ignore", windowsHide: true,
  });
  for (let i = 0; i < 60; i++) {
    const state = await getJson("http://127.0.0.1:" + port + "/__state");
    if (state) return child;
    await new Promise((r) => setTimeout(r, 120));
  }
  throw new Error("mock provider 未就绪");
}

function cli(args, home, timeout = 120000) {
  const res = cp.spawnSync(process.execPath, [path.join(ROOT, "bin", "cli.js"), ...args], {
    encoding: "utf8",
    timeout,
    cwd: ROOT,
    env: {
      ...process.env,
      CLAUDELINK_HOME: home,
      CLAUDELINK_AGENTS: path.join(TMP, "agents.detected.json"),
      CLAUDELINK_USER_AGENTS: path.join(TMP, "agents.json"),
      CLAUDELINK_RUNTIME_DIR: path.join(TMP, "instances"),
      PYTHONIOENCODING: "utf-8",
    },
  });
  return { ok: res.status === 0, code: res.status, out: res.stdout || "", err: res.stderr || "" };
}

async function main() {
  const py = findPython();
  if (!py || !py.cmd) {
    console.log("SKIP：没有 Python 3.9+，中枢起不来");
    return 0;
  }
  const home = path.join(TMP, "home");
  fs.mkdirSync(home, { recursive: true });
  const hubPort = await freePort();
  const mockPort = await freePort();
  const cfg = JSON.parse(fs.readFileSync(path.join(ROOT, "config.example.json"), "utf8"));
  cfg.server.port = hubPort;
  for (const agent of Object.values(cfg.agents || {})) agent.enabled = false;
  fs.writeFileSync(path.join(home, "config.json"), JSON.stringify(cfg, null, 2), "utf8");

  mock = await startMock(mockPort);
  const base = "http://127.0.0.1:" + mockPort;

  console.log("== 1) model add（openai / anthropic）==");
  const add = cli(["model", "add", "--label", "MockOAI", "--style", "openai",
    "--base-url", base + "/v1", "--model", "mock-1", "--key", "k1"], home);
  check("openai 模型已保存", add.ok, (add.err || add.out).slice(-300));
  const id = (add.out.match(/id=([a-f0-9]+)/) || [])[1] || "";
  check("拿到模型 id", !!id, add.out.trim().slice(-160));

  const add2 = cli(["model", "add", "--label", "MockANT", "--style", "anthropic",
    "--base-url", base, "--model", "mock-ant", "--key", "k2"], home);
  check("anthropic 模型已保存", add2.ok, (add2.err || add2.out).slice(-300));
  const id2 = (add2.out.match(/id=([a-f0-9]+)/) || [])[1] || "";

  console.log("== 2) model list ==");
  const list = cli(["model", "list"], home);
  check("列出两个模型", list.ok && list.out.includes("MockOAI") && list.out.includes("MockANT"), list.out.slice(-300));

  console.log("== 3) ask（openai，含工具调用）==");
  await getJson(base + "/__reset");
  const ask = cli(["ask", "--to", id || "MockOAI", "--timeout", "90", "列一下目录"], home);
  check("ask 退出码 0", ask.ok, (ask.err || ask.out).slice(-400));
  check("正文进了 stdout", ask.out.includes("E2E_OPENAI_OK"), JSON.stringify(ask.out.slice(-200)));
  check("工具步骤进了 stderr", /工具\s*list_files/.test(ask.err), JSON.stringify(ask.err.slice(-300)));
  const state = await getJson(base + "/__state") || { openai: { bodies: [] } };
  const bodies = state.openai.bodies || [];
  check("provider 收到 2 次请求", bodies.length === 2, String(bodies.length));
  check("首次请求带 tools", Array.isArray(bodies[0]?.body.tools) && bodies[0].body.tools.length > 0);
  check("工具结果回灌给模型", JSON.stringify(bodies[1]?.body.messages || "").includes("entries"));

  console.log("== 4) ask（anthropic，含工具调用）==");
  await getJson(base + "/__reset");
  const ask2 = cli(["ask", "--to", id2 || "MockANT", "--timeout", "90", "读目录"], home);
  check("ask 退出码 0", ask2.ok, (ask2.err || ask2.out).slice(-400));
  check("正文进了 stdout", ask2.out.includes("E2E_ANTHROPIC_OK"), JSON.stringify(ask2.out.slice(-200)));
  const state2 = await getJson(base + "/__state") || { anthropic: { bodies: [] } };
  const abodies = state2.anthropic.bodies || [];
  check("首次请求带 input_schema", /input_schema/.test(JSON.stringify(abodies[0]?.body.tools || "")));
  check("工具结果以 tool_result 回灌", JSON.stringify(abodies[1]?.body.messages || "").includes("tool_result"));

  console.log("== 5) status / stop ==");
  const status = cli(["status"], home);
  check("status 列出成员", status.ok && status.out.includes("MockOAI"), status.out.slice(-300));
  const stop = cli(["stop"], home);
  check("stop 退出码 0", stop.ok, (stop.err || stop.out).slice(-200));

  console.log(failures ? "\n✗ " + failures + " 项失败" : "\n✅ 端到端通过");
  return failures ? 1 : 0;
}

main().then((code) => { process.exitCode = code; }).catch((err) => {
  console.error((err && err.stack) || err);
  process.exitCode = 1;
});
