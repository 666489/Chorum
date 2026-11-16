#!/usr/bin/env node
"use strict";
/**
 * 自测（npm run selftest）：验证便携化改造里最容易回归的逻辑。
 *
 * 原则：**只在系统临时目录里跑**，所有路径类环境变量都被重定向，
 * 绝不改动仓库内的 config.json / hub.db / ~/.claudelink。
 *
 * 覆盖：
 *   1. 注册表合并（内置 + 用户注册表；缓存形态的文件被忽略）
 *   2. 写检测缓存**不会覆盖用户注册表**（C1）
 *   3. generic 模板 → hub schema 的翻译（C3）
 *   4. exe_auto：自动识别的跟随升级 / 用户手写的保留（M7）
 *   5. dispatch.order 保留（M6）
 *   6. 绝对路径通配展开（H6）
 *   7. detect 的优先级：env > config（M4）
 *   8. 交给 Python 侧：env_or_cfg 跳过失效路径、缓存形态防御、含空格 .cmd（H1/H5/C2）
 */
const fs = require("fs");
const os = require("os");
const path = require("path");

const ROOT = path.resolve(__dirname, "..");
const TMP = fs.mkdtempSync(path.join(os.tmpdir(), "clh-selftest-"));

// ── 重定向所有会写文件的路径（必须在 require 之前设置）──
process.env.CLAUDELINK_USER_AGENTS = path.join(TMP, "agents.json");
process.env.CLAUDELINK_AGENTS = path.join(TMP, "agents.detected.json");

const { loadAgents } = require(path.join(ROOT, "bin/registry"));
const { detect, writeCache, expandPattern } = require(path.join(ROOT, "bin/detect"));
const { translateGeneric, patchConfig } = require(path.join(ROOT, "bin/setup"));
const { writeJsonAtomic, findPython } = require(path.join(ROOT, "bin/util"));

let failures = 0;
let skips = 0;
// 无论成功/抛错都清理临时目录（覆盖中途异常）
process.on("exit", () => { try { fs.rmSync(TMP, { recursive: true, force: true }); } catch { /* ignore */ } });

function check(name, cond, detail) {
  if (cond) {
    console.log("  ✓ " + name);
  } else {
    failures++;
    console.log("  ✗ " + name + (detail ? "  → " + detail : ""));
  }
}
function section(title) { console.log("\n== " + title + " =="); }

// ── 1/2. 注册表与缓存隔离 ────────────────────────────────────────────────
section("注册表 / 检测缓存隔离（C1）");
const userFile = process.env.CLAUDELINK_USER_AGENTS;
writeJsonAtomic(userFile, [{ id: "myagent", label: "My Agent", bin: ["myagent"], support: "detect" }]);
{
  const agents = loadAgents();
  check("用户注册表被合并", agents.some((a) => a.id === "myagent"));
  const found = detect(agents);
  const cachePath = writeCache(found);
  const after = JSON.parse(fs.readFileSync(userFile, "utf8"));
  check("写缓存后用户文件仍是数组", Array.isArray(after), JSON.stringify(after).slice(0, 80));
  check("写缓存后自定义 Agent 仍在", loadAgents().some((a) => a.id === "myagent"));
  check("缓存写到独立文件", path.resolve(cachePath) === path.resolve(process.env.CLAUDELINK_AGENTS));
}
{
  // 缓存形态的文件放在用户注册表位置 → 必须被忽略（不能当成用户条目）
  writeJsonAtomic(userFile, { schema: 1, byId: { ghost: { path: "/nope" } } });
  check("缓存形态的用户文件被忽略", !loadAgents().some((a) => a.id === "ghost"));
  writeJsonAtomic(userFile, []);
}

// ── 3. generic 模板翻译（C3）──────────────────────────────────────────────
section("generic 模板 → hub schema（C3）");
{
  const spec = translateGeneric({
    promptVia: "arg", promptFlag: ["-p"], modelFlag: "--model",
    sessionArgs: ["--session-id", "{session}"], streamArgs: ["--output-format", "stream-json"],
    jsonLines: true, text_field: "result", session_field: "sid",
  });
  check("args 含 stream + prompt", JSON.stringify(spec.args) === JSON.stringify(["--output-format", "stream-json", "-p", "{prompt}"]), JSON.stringify(spec.args));
  check("model_args 生成", JSON.stringify(spec.model_args) === JSON.stringify(["--model", "{model}"]));
  check("create/resume args 都有", (spec.create_args || []).length > 0 && (spec.resume_args || []).length > 0);
  check("output=jsonl", spec.output === "jsonl");
  check("字段透传", spec.text_field === "result" && spec.session_field === "sid");
  const stdinSpec = translateGeneric({ promptVia: "stdin" });
  check("promptVia=stdin → stdin:true", stdinSpec.stdin === true && (stdinSpec.args || []).length === 0);
}

// ── 4/5. exe_auto 与 order（M7/M6）────────────────────────────────────────
section("exe_auto / order（M6/M7）");
{
  const which = process.execPath;
  const cfg = {
    server: { port: 1 },
    agents: {
      claude: { enabled: true, exe: which, label: "Claude" },                                  // 用户钉死
      codebuddy: { enabled: true, exe: "C:/old/path/does-not-exist", exe_auto: true, node: "" }, // 自动写入
      zzz: { enabled: true, exe: which, label: "zzz" },                                          // 不在注册表里的自定义
    },
    dispatch: { order: ["codebuddy", "claude"] },
  };
  const found = detect(loadAgents());
  const res = patchConfig(cfg, found, {});
  const a = res.config.agents;
  check("用户手写 exe 被保留", a.claude.exe === which && !a.claude.exe_auto);
  const cbDetected = (found.find((f) => f.id === "codebuddy") || {}).path;
  check("exe_auto 的成员跟随检测更新", cbDetected ? a.codebuddy.exe === cbDetected : true, a.codebuddy.exe);
  check("旧 order 被保留在前", JSON.stringify(res.config.dispatch.order.slice(0, 2)) === JSON.stringify(["codebuddy", "claude"]), JSON.stringify(res.config.dispatch.order));
  check("自定义成员仍在 order 里", res.config.dispatch.order.includes("zzz"));
}

// ── 6. 通配（H6）──────────────────────────────────────────────────────────
section("通配展开（H6）");
{
  const base = path.join(TMP, "glob");
  fs.mkdirSync(path.join(base, "v1", "bin"), { recursive: true });
  fs.writeFileSync(path.join(base, "v1", "bin", "tool.exe"), "");
  fs.mkdirSync(path.join(base, "v2", "bin"), { recursive: true });
  const dirs = expandPattern(path.join(base, "*", "bin"));
  check("绝对路径通配能展开", dirs.length === 2, JSON.stringify(dirs));
  const files = expandPattern(path.join(base, "*", "bin", "*"));
  check("末段可匹配文件", files.some((f) => f.endsWith("tool.exe")), JSON.stringify(files));
}

// ── 7. detect 优先级（M4：env > config）──────────────────────────────────
section("detect 优先级（M4：env > config）");
{
  const agents = loadAgents();
  const target = agents.find((a) => a.env) || agents[0];
  const envFile = path.join(TMP, "env-pick.bin");
  const cfgFile = path.join(TMP, "cfg-pick.bin");
  fs.writeFileSync(envFile, ""); fs.writeFileSync(cfgFile, "");
  const oldEnv = process.env[target.env];
  try {
    const cfg = { agents: {} };
    cfg.agents[target.id] = { exe: cfgFile };

    process.env[target.env] = envFile;
    const gotEnv = detect(agents, { config: cfg }).find((a) => a.id === target.id);
    check("env 压过 config", !!gotEnv && path.resolve(gotEnv.path) === path.resolve(envFile),
      gotEnv && (gotEnv.source + " " + gotEnv.path));

    delete process.env[target.env];
    const gotCfg = detect(agents, { config: cfg }).find((a) => a.id === target.id);
    check("env 未设时 config 生效", !!gotCfg && path.resolve(gotCfg.path) === path.resolve(cfgFile),
      gotCfg && (gotCfg.source + " " + gotCfg.path));
  } finally {
    if (oldEnv === undefined) delete process.env[target.env]; else process.env[target.env] = oldEnv;
  }
}

// ── 8. 交给 Python 侧 ─────────────────────────────────────────────────────
section("Python 侧（H1/H5/C2）");
{
  const py = findPython();
  if (!py || !py.cmd) {
    skips++;
    console.log("  … SKIP：没找到 Python 3.9+，跳过 Python 自测（只跑 JS 部分）");
  } else {
    console.log("  使用 Python：" + py.version + " -> " + py.exe);
    const r = require("child_process").spawnSync(py.cmd[0], [...py.cmd.slice(1), path.join(__dirname, "selftest.py")], { stdio: "inherit" });
    if (r.status !== 0) failures++;
  }
}

// ── 9. Everything 可选加速层（T1）──────────────────────────────────────────
section("Everything 可选层（T1）");
{
  const { probe } = require(path.join(ROOT, "bin/everything"));
  const { exploreFor, extraRootsFor } = require(path.join(ROOT, "bin/detect"));
  const { IS_WIN } = require(path.join(ROOT, "bin/util"));
  const agents = loadAgents();
  const sh = path.join(TMP, "esbin");
  fs.mkdirSync(sh, { recursive: true });
  const oldEs = process.env.CLAUDELINK_ES;
  const writeStub = (file, content) => {
    fs.writeFileSync(file, content, "utf8");
    if (!IS_WIN) fs.chmodSync(file, 0o755);
    return file;
  };
  const echoStub = (lines, sleepSec) => {
    if (IS_WIN) {
      return ["@echo off"].concat(
        sleepSec ? ["ping -n " + (sleepSec + 1) + " 127.0.0.1 >nul"] : [],
        lines.map((l) => "echo " + l)
      ).join("\r\n") + "\r\n";
    }
    return ["#!/bin/sh"].concat(sleepSec ? ["sleep " + sleepSec] : [], lines.map((l) => "echo " + JSON.stringify(l))).join("\n") + "\n";
  };

  // Everything 只对 Windows 有意义：其它平台按设计返回 unsupported，不做断言也不计失败
  if (IS_WIN) {
  // 9.1 es 缺失 → 静默回退，detect 结果与不启用时完全一致
  try {
    process.env.CLAUDELINK_ES = path.join(TMP, "no-such-es.exe");
    const cfgMiss = { detect: { everything: { enabled: true, roots: [TMP] } } };
    const pMiss = probe(agents, cfgMiss);
    check("es 缺失时静默回退（status=no-es）", pMiss.status === "no-es" && pMiss.count === 0, pMiss.status + " " + pMiss.reason);
    const withEs = detect(agents, { config: cfgMiss });
    const without = detect(agents);
    check("es 缺失不改变 detect 结果", JSON.stringify(withEs.map((a) => [a.id, a.path])) === JSON.stringify(without.map((a) => [a.id, a.path])));
  } finally {
    if (oldEs === undefined) delete process.env.CLAUDELINK_ES; else process.env.CLAUDELINK_ES = oldEs;
  }

  // 9.2 假 es 返回噪声 → 只保留白名单 + 普通文件
  {
    const found = path.join(TMP, "everything-dir");
    fs.mkdirSync(found, { recursive: true });
    const good = path.join(found, "claude.cmd");
    fs.writeFileSync(good, "@echo off\r\necho 9.9.9\r\n", "utf8");
    const noise = [
      path.join(found, "evil.exe"),           // 文件名不在白名单
      path.join(found, "claude-helper.exe"),  // 前缀相似但不是精确名
      "not-a-path-at-all",                    // 噪声行
      path.join(found, "codebuddy.exe"),      // 这是**目录**，不是普通文件
    ];
    fs.mkdirSync(path.join(found, "codebuddy.exe"), { recursive: true });
    const stub = writeStub(path.join(sh, IS_WIN ? "es.cmd" : "es.sh"), echoStub([good, ...noise]));
    const cfg = { detect: { everything: { enabled: true, exe: stub, roots: [found] } } };
    const p = probe(agents, cfg);
    check("假 es 被识别（status=ok）", p.status === "ok", p.status + " " + p.reason);
    check("只保留白名单命中", p.count === 1 && (p.hits.claude || []).length === 1, JSON.stringify(p.results));
    check("目录/相似名/噪声行被丢弃", !p.results.some((x) => /evil|helper|not-a-path/.test(x)));
    const res = exploreFor(agents.find((a) => a.id === "claude"), {
      config: cfg, everything: p, extraRoots: [], npmRoot: "", searchRoots: [], collectTrace: true,
    });
    check("everything 命中标 source=everything", res.cands.some((c) => c.source === "everything"), JSON.stringify(res.cands.map((c) => c.source)));
    const ev = res.trace.find((t) => t.layer === "everything");
    check("everything 层进 trace 且为命中", !!ev && ev.status === "hit", ev && ev.status);
  }

  // 9.3 超时不阻塞（预算 300ms，stub 睡 3s）
  {
    const slow = writeStub(path.join(sh, IS_WIN ? "es-slow.cmd" : "es-slow.sh"), echoStub([path.join(TMP, "claude.cmd")], 3));
    const cfg = { detect: { everything: { enabled: true, exe: slow, roots: [TMP], timeout_ms: 300 } } };
    const t0 = Date.now();
    const p = probe(agents, cfg);
    const ms = Date.now() - t0;
    check("超时按预算结束（<2.5s）", ms < 2500, ms + "ms");
    check("超时不产生命中且不抛", p.status === "timeout" && p.count === 0, p.status + " " + p.reason);
  }

  // 9.4 默认不启用全盘：roots 为空且未 allow_full_scan
  {
    const anyEs = writeStub(path.join(sh, IS_WIN ? "es-any.cmd" : "es-any.sh"), echoStub([]));
    const p = probe(agents, { detect: { everything: { enabled: true, exe: anyEs } } });
    check("roots 为空时不查全盘（status=no-roots）", p.status === "no-roots" && p.count === 0, p.status);
    const p2 = probe(agents, { detect: { everything: { enabled: false, roots: [TMP] } } });
    check("enabled=false 可整体关闭", p2.status === "disabled", p2.status);
  }

  } else {
    console.log("  … 仅 Windows 的功能（Everything 加速层）：在 " + process.platform + " 上按设计不可用，跳过 4 组断言（不计失败）");
  }

  // 9.5 detect.extra_roots（T11）：用户声明目录能命中，系统目录被跳过
  {
    const extra = path.join(TMP, "extra-root");
    fs.mkdirSync(extra, { recursive: true });
    fs.writeFileSync(path.join(extra, "gemini.cmd"), "@echo off\r\necho 9.9.9\r\n", "utf8");
    const cfg = { detect: { extra_roots: [extra, path.resolve(path.sep)] } };
    check("extra_roots 过掉系统目录根", !extraRootsFor(cfg).some((p) => path.resolve(p) === path.resolve(path.sep)), JSON.stringify(extraRootsFor(cfg)));
    const res = exploreFor(agents.find((a) => a.id === "gemini"), {
      config: cfg, everything: { status: "disabled", hits: {} }, npmRoot: "", searchRoots: [], collectTrace: true,
    });
    check("extra_root 命中", res.cands.length > 0 && res.cands[0].source === "extra-root", JSON.stringify(res.cands.map((c) => c.source)));
  }
}

// ── 9. 接入层：attach 判定 / 参数模板（T5/T17）────────────────────────────
section("接入层 attach 判定（T5/T17）");
{
  const attach = require(path.join(ROOT, "bin/attach"));

  // 三种真实 mcp list 输出的判定（fixtures 用中性路径，不含本机信息）
  const claudeOut = "Checking MCP server health…\n\nclaudelink: claudelink-server  - √ Connected\n";
  const p1 = attach.parseList(claudeOut);
  check("claude 输出识别为已接入", p1.found && p1.attached, JSON.stringify(p1));

  const cbOut = "Checking MCP server health...\nclaudelink: X:/tools/node/node.exe X:/tools/claudelink/bin/server.js - ✓ Connected\n";
  const p2 = attach.parseList(cbOut);
  check("codebuddy 输出（server.js）识别为已接入", p2.found && p2.attached, JSON.stringify(p2));

  const codexOut = "Name       Command                    Args  Env  Cwd  Status\n" +
    "claudelink   X:/tools/claudelink-server   -     -    -    enabled\n";
  const p3 = attach.parseList(codexOut);
  check("codex 表格输出识别为已接入", p3.found && p3.attached, JSON.stringify(p3));

  const empty = attach.parseList("No MCP servers configured.\n");
  check("空列表 → 未接入", !empty.found && !empty.attached, JSON.stringify(empty));

  const stale = attach.parseList("claudelink: some-other-server  - √ Connected\n");
  check("同名但命令不是 ClaudeLink → 不算已接入", stale.found && !stale.attached, JSON.stringify(stale));

  const decoy = attach.parseList("claudelink-server: X:/bin/foo  - √ Connected\n");
  check("claudelink-server 条目不会被误认成 claudelink", !decoy.found, JSON.stringify(decoy));

  // mcp 元数据：null / 缺 add 都算「未支持自动接入」
  check("dsh 式 mcp:null → 不支持", attach.mcpMeta({ agent: { mcp: null } }) === null);
  check("缺 add 字段 → 不支持", attach.mcpMeta({ agent: { mcp: { list: ["mcp", "list"] } } }) === null);
  const meta = { list: ["mcp", "list"], add: ["mcp", "add", "--", "{server}"], remove: ["mcp", "remove", "claudelink"] };
  check("完整元数据被接受", !!attach.mcpMeta({ agent: { mcp: meta } }));

  // {server} 占位替换（裸名回退 + 自定义服务命令）
  check("{server} 替换成默认服务命令",
    JSON.stringify(attach.tmplArgs(meta.add, "")) === JSON.stringify(["mcp", "add", "--", "claudelink-server"]),
    JSON.stringify(attach.tmplArgs(meta.add, "")));
  check("{server} 可换成自定义部署",
    attach.tmplArgs(meta.add, "my-server")[3] === "my-server");

  // sudo / root 拒绝（T16）
  check("root + SUDO_USER → 拒绝", !!attach.refusalReason({ username: "root" }, { SUDO_USER: "alice" }));
  check("root + HOME=/root → 拒绝", !!attach.refusalReason({ username: "root" }, { HOME: "/root" }));
  check("普通用户 → 放行", attach.refusalReason({ username: "alice" }, { HOME: "/home/alice" }) === "");

  // .cmd / .js / 无扩展名 node 入口的调用方式（T14）
  const cmdHit = attach.launcherFor({ path: "X:/tools/claude.cmd", agent: {} });
  check(".cmd 直接执行", cmdHit.exe === "X:/tools/claude.cmd" && cmdHit.pre.length === 0);
  const jsHit = attach.launcherFor({ path: path.join(TMP, "bin.js"), agent: { node: true }, node: process.execPath });
  check(".js 用 node 执行", path.resolve(jsHit.exe) === path.resolve(process.execPath) && jsHit.pre.length === 1);
  const bareHit = attach.launcherFor({ path: path.join(TMP, "codebuddy"), agent: { node: true }, node: process.execPath });
  check("无扩展名 + node:true 用 node 执行", bareHit.pre.length === 1);
}

// ── 10. 用户级变量：只允许 CLAUDELINK_*（T6）──────────────────────────────
section("用户级变量（T6）");
{
  const env = require(path.join(ROOT, "bin/env"));
  const envFile = path.join(TMP, "user-env.json");
  const oldFile = process.env.CLAUDELINK_ENV_FILE;
  process.env.CLAUDELINK_ENV_FILE = envFile;
  const log = console.log;
  try {
    check("允许 CLAUDELINK_*", env.isAllowed("CLAUDELINK_PYTHON") && env.isAllowed("CLAUDELINK_NPM_PREFIX"));
    check("拒绝第三方 KEY", !env.isAllowed("OPENAI_API_KEY") && !env.isAllowed("CLAUDELINK") && !env.isAllowed("DSH_TASK"));
    const bad = env.computeChanges(["OPENAI_API_KEY=sk-x"], []);
    check("--set 第三方 KEY 被拒", !!bad.error, bad.error);

    const quiet = (fn) => { console.log = () => {}; try { return fn(); } finally { console.log = log; } };

    quiet(() => env.runEnv({ set: ["CLAUDELINK_PYTHON=X:/py/python.exe"], yes: false }));
    check("没有 --yes 不落盘", !fs.existsSync(envFile));

    quiet(() => env.runEnv({ set: ["CLAUDELINK_PYTHON=X:/py/python.exe"], yes: true }));
    check("--yes 才写 env.json", fs.existsSync(envFile) && env.readEnvFile().CLAUDELINK_PYTHON === "X:/py/python.exe",
      JSON.stringify(env.readEnvFile()));

    quiet(() => env.runEnv({ unset: ["CLAUDELINK_PYTHON"], yes: true }));
    check("--unset 生效", !env.readEnvFile().CLAUDELINK_PYTHON, JSON.stringify(env.readEnvFile()));
  } finally {
    console.log = log;
    if (oldFile === undefined) delete process.env.CLAUDELINK_ENV_FILE; else process.env.CLAUDELINK_ENV_FILE = oldFile;
  }
}

// ── 11. 并发保护：文件锁 / CAS / 跨进程不丢更新 ──────────────────────────
section("并发保护（锁 / CAS / 跨进程）");
{
  const util = require(path.join(ROOT, "bin/util"));

  // (a) 锁被活进程占着 → 超时报错，且绝不写文件
  const locked = path.join(TMP, "locked.json");
  const lockDir = util.lockDirFor(locked);
  fs.mkdirSync(path.dirname(lockDir), { recursive: true });
  fs.mkdirSync(lockDir);
  fs.writeFileSync(path.join(lockDir, "owner.json"),
    JSON.stringify({ pid: process.pid, host: "test", at: new Date().toISOString() }));
  const t0 = Date.now();
  const r1 = util.updateJsonLocked(locked, () => ({ a: 1 }), { timeoutMs: 250 });
  check("锁被占 → 超时失败（LOCK_TIMEOUT）", r1.ok === false && r1.reason === "lock" && /超时/.test(r1.error.message),
    JSON.stringify({ ok: r1.ok, reason: r1.reason }));
  check("没拿到锁就不写文件", !fs.existsSync(locked));
  check("确实等待过（>=200ms）", Date.now() - t0 >= 200, String(Date.now() - t0));

  // (b) 上一个持有者崩了（pid 不存在）→ 残留锁自动回收
  fs.writeFileSync(path.join(lockDir, "owner.json"),
    JSON.stringify({ pid: 999999, host: "test", at: new Date().toISOString() }));
  const r2 = util.updateJsonLocked(locked, () => ({ a: 2 }), { timeoutMs: 3000 });
  check("死进程的残留锁被回收", r2.ok === true && !fs.existsSync(lockDir), JSON.stringify({ ok: r2.ok }));
  check("回收后正常写入", JSON.parse(fs.readFileSync(locked, "utf8")).a === 2);

  // (c) CAS：读到旧内容后，外部（不拿我们锁的写者）改了文件 → 重读重算，不覆盖
  const casFile = path.join(TMP, "cas.json");
  fs.writeFileSync(casFile, JSON.stringify({ n: 0 }, null, 2) + "\n");
  let first = true;
  const r3 = util.updateJsonLocked(casFile, (cur) => {
    if (first) {
      first = false;
      // 模拟中枢 UI 在我「读完、未写」之间插了一笔
      fs.writeFileSync(casFile, JSON.stringify({ n: 0, ui: true }, null, 2) + "\n");
    }
    return Object.assign({}, cur || {}, { n: ((cur && cur.n) || 0) + 1 });
  }, { attempts: 5 });
  const casData = JSON.parse(fs.readFileSync(casFile, "utf8"));
  check("CAS 发现外部改动并重试", r3.ok === true && r3.attempts === 1, JSON.stringify(r3));
  check("外部写入的字段没被覆盖", casData.ui === true, JSON.stringify(casData));
  check("计数既没丢也没重复", casData.n === 1, JSON.stringify(casData));

  // (d) 内容本来就一致 → 不写盘（无竞争时行为不变）
  const sameFile = path.join(TMP, "same.json");
  fs.writeFileSync(sameFile, JSON.stringify({ a: 1 }, null, 2) + "\n");
  const r4 = util.updateJsonLocked(sameFile, () => ({ a: 1 }), {});
  check("内容相同则不写盘", r4.ok === true && r4.unchanged === true, JSON.stringify(r4));

  // (e) 真·跨进程：4 个进程各 +15，最终必须正好 60（丢更新就一定小于 60）
  const helper = path.join(TMP, "counter.js");
  fs.writeFileSync(helper, [
    'const { updateJsonLocked } = require(process.argv[2] + "/bin/util");',
    "const fs = require('fs');",
    "const file = process.argv[3];",
    "const times = Number(process.argv[4] || 15);",
    "for (let i = 0; i < times; i++) {",
    "  const out = updateJsonLocked(file, (cur) => {",
    "    const c = (cur && typeof cur === 'object') ? cur : {};",
    "    c.n = (c.n || 0) + 1;",
    "    return c;",
    "  }, { attempts: 20, timeoutMs: 20000 });",
    "  if (!out.ok) { console.error('FAIL ' + out.reason); process.exit(2); }",
    "}",
    "fs.writeFileSync(process.argv[5], 'ok');",
  ].join("\n"));

  const counterFile = path.join(TMP, "counter.json");
  fs.writeFileSync(counterFile, JSON.stringify({ n: 0 }, null, 2) + "\n");
  const KIDS = 4, EACH = 15;
  const procs = [];
  for (let i = 0; i < KIDS; i++) {
    const done = path.join(TMP, "done-" + i + ".txt");
    const child = require("child_process").spawn(process.execPath,
      [helper, ROOT, counterFile, String(EACH), done],
      { stdio: ["ignore", "ignore", "pipe"], windowsHide: true });
    let err = "";
    child.stderr.on("data", (c) => { err += String(c); });
    procs.push({ child, done, getErr: () => err });
  }
  const deadline = Date.now() + 60000;
  while (Date.now() < deadline && !procs.every((p) => fs.existsSync(p.done))) util.sleepSync(50);
  const finished = procs.filter((p) => fs.existsSync(p.done)).length;
  check("4 个子进程全部跑完", finished === KIDS, finished + "/" + KIDS);
  const total = JSON.parse(fs.readFileSync(counterFile, "utf8")).n;
  check("跨进程无丢更新（" + KIDS + "×" + EACH + "=" + KIDS * EACH + "）", total === KIDS * EACH, "实际 " + total);
  for (const p of procs) {
    if (!fs.existsSync(p.done)) { try { p.child.kill(); } catch { /* ignore */ } }
  }
  check("无残留锁目录", !fs.existsSync(util.lockDirFor(counterFile)));
}

// ── 收尾 ─────────────────────────────────────────────────────────────────
// CLH_STRICT=1 → 把"跳过"也视为失败（CI 用；本机没有 Python 时不至于误报绿）
const strict = process.env.CLH_STRICT === "1";
if (skips) console.log("\n… 跳过 " + skips + " 项" + (strict ? "（CLH_STRICT=1 → 视为失败）" : ""));
if (strict && skips) failures += skips;
console.log("\n" + (failures ? "✗ 失败 " + failures + " 项" : "✅ 全部通过"));
process.exitCode = failures ? 1 : 0;
