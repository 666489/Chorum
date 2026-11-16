"use strict";
/**
 * Everything（voidtools）CLI 探测源 —— **可选加速层**，仅 Windows。
 *
 * 设计边界（重要）
 *   - 只是「加速/补充」：找不到 es.exe、超时、非零退出、输出乱码 → 一律**静默回退**，
 *     不打印、不改退出码、不写 hub.log，调用方看到的就是「这一层没有候选」。
 *   - 默认**不做全盘搜索**：`detect.everything.roots` 为空且 `allow_full_scan` 不为 true 时，
 *     这一层直接不查询（status=no-roots），只把结论交给 `detect --explain`。
 *   - 只接受「注册表里声明过的命令名」且必须是**普通文件**；其余结果（噪声、目录、无关程序）
 *     全部丢弃 —— 防止 Everything 索引里同名但无关的可执行文件被误判。
 *   - 不落盘：查询结果只回传给内存中的 detect()，由它按既有规则写 agents.detected.json。
 *
 * 本文件不得出现任何机器专属绝对路径：es.exe 的位置全部运行时推导（环境变量 / 配置 /
 * 注册表常见安装目录 / PATH）。
 */
const path = require("path");
const cp = require("child_process");
const { expand, unresolved, isFile, isDir, findOnPath, binVariants, IS_WIN } = require("./util");

/** 护栏默认值（可被 config 覆盖，但有上下限，防止配成「永远卡住」）。 */
const DEFAULTS = { timeout_ms: 3000, max_results: 50 };

/** Everything 的常见安装位置（便携占位符表达，不是机器路径）。 */
const KNOWN_DIRS = [
  "%ProgramFiles%/Everything/es.exe",
  "%ProgramFiles(x86)%/Everything/es.exe",
  "%LOCALAPPDATA%/Programs/Everything/es.exe",
];

function samePath(a, b) {
  const na = path.resolve(a);
  const nb = path.resolve(b);
  return IS_WIN ? na.toLowerCase() === nb.toLowerCase() : na === nb;
}

function clampNum(v, def, min, max) {
  const n = Number(v);
  if (!Number.isFinite(n)) return def;
  return Math.min(max, Math.max(min, Math.round(n)));
}

/** 归一化配置：detect.everything.{enabled,exe,roots,allow_full_scan,timeout_ms,max_results}。 */
function settings(cfg) {
  const raw = (cfg && cfg.detect && cfg.detect.everything) || {};
  const roots = [];
  for (const r of Array.isArray(raw.roots) ? raw.roots : []) {
    if (typeof r !== "string" || !r.trim()) continue;
    const v = expand(r.trim());
    if (!v || unresolved(v) || !isDir(v)) continue;
    if (!roots.some((x) => samePath(x, v))) roots.push(v);
  }
  return {
    // 三态：false=整体关闭；true=有 es 就启用；"auto"（默认）=有 es 且配了 roots 才真正查询
    enabled: raw.enabled === undefined ? "auto" : raw.enabled,
    exe: typeof raw.exe === "string" ? raw.exe : "",
    roots,
    allowFullScan: raw.allow_full_scan === true,
    timeoutMs: clampNum(raw.timeout_ms, DEFAULTS.timeout_ms, 200, 20000),
    maxResults: clampNum(raw.max_results, DEFAULTS.max_results, 1, 500),
  };
}

/**
 * 定位 es.exe：CLAUDELINK_ES → config.detect.everything.exe → 常见安装目录 → PATH 的 es。
 * 返回 { exe, how, tried[] }；tried 给 --explain 用，失败时也能说清「试过哪」。
 */
function resolveEs(cfg, env = process.env) {
  const s = settings(cfg);
  const tried = [];
  const pick = (value, how) => {
    if (!value) return "";
    const v = expand(String(value).trim(), env);
    if (!v || unresolved(v)) { tried.push({ path: String(value), how, why: "占位符无法展开" }); return ""; }
    if (!isFile(v)) { tried.push({ path: v, how, why: "不存在" }); return ""; }
    tried.push({ path: v, how, why: "" });
    return v;
  };

  let hit = pick(env.CLAUDELINK_ES, "环境变量 CLAUDELINK_ES");
  if (hit) return { exe: hit, how: "env", tried };

  hit = pick(s.exe, "配置 detect.everything.exe");
  if (hit) return { exe: hit, how: "config", tried };

  for (const p of KNOWN_DIRS) {
    hit = pick(p, "已知安装目录");
    if (hit) return { exe: hit, how: "known-dir", tried };
  }

  const onPath = findOnPath("es", env);
  if (onPath) { tried.push({ path: onPath, how: "PATH", why: "" }); return { exe: onPath, how: "path", tried }; }
  tried.push({ path: "es", how: "PATH", why: "PATH 里没有 es" });
  return { exe: "", how: "", tried };
}

/** 注册表里允许出现的文件名（小写）→ 所属 agent id 集合。 */
function allowedFiles(registry) {
  const map = new Map();
  const push = (name, id) => {
    const key = String(name).toLowerCase();
    if (!key) return;
    if (!map.has(key)) map.set(key, new Set());
    map.get(key).add(id);
  };
  for (const agent of registry || []) {
    if (!agent || !agent.id) continue;
    for (const b of agent.bin || []) for (const v of binVariants(b)) push(v, agent.id);
  }
  return map;
}

/** 文件名去掉扩展名 → 用于拼「精确文件名」正则。 */
function baseNames(fileNames) {
  const out = new Set();
  for (const n of fileNames) out.add(String(n).toLowerCase().replace(/.(exe|cmd|bat|ps1|sh)$/, ""));
  return Array.from(out).filter(Boolean).sort();
}

function escapeRe(s) {
  return String(s).replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

/** stdout/stderr 解码：先按 UTF-8 严格解，失败退 cp936（中文 Windows 常见）。 */
function decode(buf) {
  if (!buf || !buf.length) return "";
  try {
    return new TextDecoder("utf-8", { fatal: true }).decode(buf);
  } catch {
    try { return new TextDecoder("gbk").decode(buf); }
    catch { return Buffer.from(buf).toString("utf8"); }
  }
}

function toBuf(x) {
  if (!x) return Buffer.alloc(0);
  return Buffer.isBuffer(x) ? x : Buffer.from(String(x), "utf8");
}

/** 执行 es.exe。超时/非零退出都只是「这层没结果」，绝不抛。 */
function runEs(exe, args, timeoutMs) {
  const base = { timeout: timeoutMs, windowsHide: true, encoding: null, env: process.env };
  let res;
  if (IS_WIN && /\.(cmd|bat)$/i.test(exe)) {
    // .cmd/.bat 不能被直接 spawn（ENOENT），必须经 cmd
    const line = [exe, ...args].map((a) => (/[\s"&|<>^]/.test(String(a)) ? '"' + String(a).replace(/"/g, '""') + '"' : String(a))).join(" ");
    res = cp.spawnSync(line, Object.assign({}, base, { shell: true }));
  } else {
    res = cp.spawnSync(exe, args, base);
  }
  const timedOut = !!(res.error && (res.error.code === "ETIMEDOUT" || res.signal === "SIGTERM"));
  return {
    ok: res.status === 0 && !timedOut,
    status: res.status,
    timedOut,
    stdout: toBuf(res.stdout),
    stderr: toBuf(res.stderr),
  };
}

/**
 * 顶层查询：一次 es 调用覆盖所有 agent 的命令名（合并成一条精确文件名正则），
 * 结果再按白名单回填到各个 agent。
 *
 * 返回 { status, reason, exe, how, tried, query, roots, hits: {id: [path]}, count, ms }
 *   status: ok | disabled | unsupported | no-es | no-roots | timeout | error | empty
 * 任何失败路径都**不打印**，只体现在 reason 里（给 --explain / 内部排障看）。
 */
function probe(registry, cfg, opts = {}) {
  const started = Date.now();
  const s = settings(cfg);
  const env = opts.env || process.env;
  const res = {
    status: "disabled", reason: "", exe: "", how: "", tried: [], query: "",
    roots: s.roots, hits: {}, count: 0, ms: 0, results: [],
  };
  const done = () => { res.ms = Date.now() - started; return res; };

  try {
    if (!IS_WIN) { res.status = "unsupported"; res.reason = "Everything 只在 Windows 上有意义"; return done(); }
    if (s.enabled === false) { res.status = "disabled"; res.reason = "detect.everything.enabled=false（已整体关闭）"; return done(); }

    const found = resolveEs(cfg, env);
    res.exe = found.exe;
    res.how = found.how;
    res.tried = found.tried;
    if (!found.exe) {
      res.status = "no-es";
      res.reason = "没找到 es.exe（Everything 未安装、未加入 PATH，也没配 CLAUDELINK_ES）";
      return done();
    }
    if (!s.roots.length && !s.allowFullScan) {
      res.status = "no-roots";
      res.reason = "未配置 detect.everything.roots —— 默认不做全盘搜索";
      return done();
    }

    const files = allowedFiles(registry);
    const names = baseNames(files.keys());
    if (!names.length) { res.status = "empty"; res.reason = "注册表里没有声明任何命令名"; return done(); }

    const regex = "^(" + names.map(escapeRe).join("|") + ")(\\.(exe|cmd|bat))?$";
    res.query = regex;

    const deadline = started + s.timeoutMs;      // 整体预算，别让 N 个根把总时长乘出 N 倍
    const hitSets = new Map();                   // id -> Set(path)
    const seen = new Set();
    const targets = s.roots.length ? s.roots : [""];   // "" = 全盘

    for (const root of targets) {
      const left = deadline - Date.now();
      if (left <= 100) { res.status = "timeout"; res.reason = "查询超出 " + s.timeoutMs + "ms 预算，已放弃剩余搜索根"; break; }
      const args = ["-i", "-r", regex, "-n", String(s.maxResults)];
      if (root) args.push("-path", root);
      const out = runEs(found.exe, args, left);
      if (out.timedOut) { res.status = "timeout"; res.reason = "es.exe 在 " + s.timeoutMs + "ms 内没有返回"; break; }
      if (out.status !== 0) { res.status = "error"; res.reason = "es.exe 退出码 " + out.status; break; }

      const text = decode(out.stdout);
      for (const rawLine of text.split(/\r?\n/)) {
        const line = rawLine.trim();
        if (!line || !path.isAbsolute(line)) continue;      // 噪声行（提示、空行、相对路径）
        const key = IS_WIN ? line.toLowerCase() : line;
        if (seen.has(key)) continue;
        const ids = files.get(path.basename(line).toLowerCase());
        if (!ids) continue;                                  // 白名单外的一律丢弃
        if (!isFile(line)) continue;                          // 只接受普通文件
        seen.add(key);
        res.results.push(line);
        for (const id of ids) {
          if (!hitSets.has(id)) hitSets.set(id, new Set());
          hitSets.get(id).add(line);
        }
      }
    }

    for (const [id, set] of hitSets) res.hits[id] = Array.from(set).sort();
    res.count = res.results.length;
    if (!res.reason) {
      res.status = res.count ? "ok" : "empty";
      res.reason = res.count ? "" : "es.exe 没查到注册表里的命令名";
    }
  } catch (err) {
    // 绝不因为「加速层」把安装/体检搞挂
    res.status = "error";
    res.reason = "意外错误：" + (err && err.message ? err.message : String(err));
    res.hits = {};
    res.count = 0;
  }
  return done();
}

/** 命中列表 → 候选数组（给 detect 的 everything 层用）。 */
function candidatesFor(agentId, pr) {
  const list = (pr && pr.hits && pr.hits[agentId]) || [];
  return list.map((p) => ({ path: p, source: "everything" }));
}

module.exports = { DEFAULTS, settings, resolveEs, probe, candidatesFor, decode, allowedFiles, baseNames };
