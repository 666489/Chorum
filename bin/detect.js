"use strict";
/**
 * Agent 检测：把「注册表声明」变成「本机实际可用的路径」。
 * 搜索顺序（对每个 agent，前面的命中优先）：
 *   1. env           环境变量 CLAUDELINK_<ID>
 *   2. config        config.agents.<id>.exe（用户显式指定，允许 %VAR%）
 *   3. path          PATH（bin 里的裸名 + 平台扩展名变体）
 *   4. known-dir     agent.paths（注册表里的便携位置，支持 * 通配）
 *   5. npm-root      npm 全局根（<root>/<bin>）
 *   6. search-root   通用搜索根 searchRoots() × bin
 *   7. extra-root    config.detect.extra_roots（用户声明的额外目录，只浅扫）★新增，默认空
 *   8. scan-dir      agent.scanDirs（含版本子目录，浅扫）
 *   9. everything    Everything 的 es.exe（可选加速层）★新增，默认不启用全盘
 *
 * 铁律：
 *   - 本文件不含任何机器专属路径。
 *   - 7/9 是**可选层**：默认零行为变化（extra_roots 默认空；everything 默认 no-roots）。
 *     关闭后 1–6、8 的优先级与语义与改造前完全一致。
 */
const fs = require("fs");
const os = require("os");
const path = require("path");
const {
  expand, unresolved, isFile, isDir, findOnPath, binVariants, uniquePaths,
  versionOf, npmGlobalRoot, searchRoots, agentsCachePath, writeJsonAtomic, IS_WIN,
} = require("./util");
const { loadAgents } = require("./registry");
const everything = require("./everything");

/** --explain 每层最多列出多少条「试过的位置」（其余只给计数，避免刷屏）。 */
const MAX_TRACE_TRIED = 60;

/** 浅扫目录找可执行文件（depth=2 覆盖 <bin>/<hash>/codex.exe 这种版本化布局）。 */
function scanDir(dir, names, depth = 2) {
  const out = [];
  if (!isDir(dir) || depth < 0) return out;
  let entries = [];
  try { entries = fs.readdirSync(dir, { withFileTypes: true }); } catch { return out; }
  for (const ent of entries) {
    const full = path.join(dir, ent.name);
    if (ent.isFile()) {
      const low = ent.name.toLowerCase();
      if (names.includes(low)) out.push(full);
    } else if (ent.isDirectory()) {
      out.push(...scanDir(full, names, depth - 1));
    }
  }
  return out;
}

/** 极简通配（只支持 *），避免正则转义踩坑。 */
function globMatch(name, pat) {
  const n = String(name).toLowerCase();
  const parts = String(pat).toLowerCase().split("*");
  if (parts.length === 1) return n === parts[0];
  let idx = 0;
  if (parts[0] && !n.startsWith(parts[0])) return false;
  idx = parts[0].length;
  for (let i = 1; i < parts.length; i++) {
    const seg = parts[i];
    if (!seg) continue;
    const at = n.indexOf(seg, idx);
    if (at < 0) return false;
    idx = at + seg.length;
  }
  const tail = parts[parts.length - 1];
  return !tail || n.endsWith(tail) || idx === n.length;
}

// 支持 %VAR%/Python3{星号}/Scripts 这种通配：逐段展开并列出匹配目录。
// 注意：注释里不能出现星号紧接斜杠（会提前闭合块注释）。
function expandPattern(p) {
  const raw = expand(p);
  if (!raw || unresolved(raw)) return [];
  if (!raw.includes("*")) return [raw];
  let dirs = [path.isAbsolute(raw) ? path.parse(raw).root : ""];
  const parts = raw.split(/[\\/]+/).filter(Boolean);
  // 绝对路径下盘符（C:）已经体现在 root 里，不能再当一段拼 → 否则得到 C:\C:\...
  if (path.isAbsolute(raw) && /^[A-Za-z]:$/.test(parts[0])) parts.shift();
  for (let i = 0; i < parts.length; i++) {
    const part = parts[i];
    const last = i === parts.length - 1;
    const next = [];
    if (!part.includes("*")) {
      for (const d of dirs) {
        const cand = path.join(d, part);
        if (last) { if (isFile(cand) || isDir(cand)) next.push(cand); }
        else if (isDir(cand)) next.push(cand);
      }
    } else {
      for (const d of dirs) {
        let ents = [];
        try { ents = fs.readdirSync(d, { withFileTypes: true }); } catch { continue; }
        for (const ent of ents) {
          if (!globMatch(ent.name, part)) continue;
          if (last) next.push(path.join(d, ent.name));          // 末段可以是文件（如 *.exe）
          else if (ent.isDirectory()) next.push(path.join(d, ent.name));
        }
      }
    }
    dirs = next;
  }
  return dirs;
}

function namesFor(agent) {
  // 用 binVariants：已经带 .exe/.cmd/.bat 的名字不再叠扩展名（否则出现 codex.cmd.exe 这种噪声）
  const names = [];
  for (const b of agent.bin || []) {
    for (const v of binVariants(b)) names.push(String(v).toLowerCase());
  }
  return Array.from(new Set(names));
}

function samePath(a, b) {
  const na = path.resolve(a);
  const nb = path.resolve(b);
  return IS_WIN ? na.toLowerCase() === nb.toLowerCase() : na === nb;
}

/** 系统目录「根」——extra_roots 指到这些位置时直接跳过（不进系统目录）。 */
function systemRoots() {
  const raw = IS_WIN
    ? ["%SystemRoot%", "%windir%", "%ProgramFiles%", "%ProgramFiles(x86)%", "%ProgramData%"]
    : ["/", "/usr", "/etc", "/var", "/bin", "/sbin", "/lib", "/lib64", "/proc", "/sys", "/dev", "/boot", "/run"];
  const out = [];
  for (const r of raw) {
    const v = expand(r);
    if (!v || unresolved(v)) continue;
    out.push(v);
  }
  return out;
}

/** 网络盘：UNC 形式一律不扫（映射盘无法在纯 Node 里可靠判别，文档里写明）。 */
function isNetworkPath(p) {
  return /^[\\/]{2}/.test(String(p || ""));
}

/**
 * config.detect.extra_roots → 实际可用的额外搜索根。
 * 规则：默认空；只接受存在的目录；跳过 UNC 网络盘与系统目录根本身。
 */
function extraRootsFor(cfg) {
  const list = cfg && cfg.detect && Array.isArray(cfg.detect.extra_roots) ? cfg.detect.extra_roots : [];
  const sys = systemRoots();
  const out = [];
  for (const r of list) {
    if (typeof r !== "string" || !r.trim()) continue;
    const v = expand(r.trim());
    if (!v || unresolved(v) || !isDir(v)) continue;
    if (isNetworkPath(v)) continue;
    // 文件系统根（/ 或 D:\）与系统目录根本身都不进
    if (path.resolve(v) === path.parse(path.resolve(v)).root) continue;
    if (sys.some((s) => samePath(s, v))) continue;
    if (!out.some((x) => samePath(x, v))) out.push(v);
  }
  return out;
}

/**
 * 单个 agent 的完整探查（带 trace，供 detect --explain / doctor --json 用）。
 * 返回 { cands: [{path, source}], trace: [{layer, status, reason, tried, triedCount, hits}] }
 */
function exploreFor(agent, opts = {}) {
  const out = [];
  const trace = [];
  let bucket = null;

  const open = (layer) => {
    bucket = { layer, status: "miss", reason: "", triedCount: 0, tried: [], hits: [] };
    trace.push(bucket);
  };
  const note = (p, status) => {
    if (!bucket) return;
    bucket.triedCount++;
    if (bucket.tried.length < MAX_TRACE_TRIED) bucket.tried.push({ path: String(p), status });
  };
  const mark = (reason) => { if (bucket) { bucket.status = "unavailable"; bucket.reason = reason; } };
  const add = (p, source) => {
    if (!p) return;
    const raw = String(p).trim();
    const v = expand(raw);
    if (!v || unresolved(v)) { note(raw, "unresolved"); return; }
    if (!isFile(v)) { note(v, "missing"); return; }
    if (bucket.hits.includes(v)) return;          // 同层重复命中（如 extra-root 的直连 + 浅扫）
    note(v, "hit");
    bucket.hits.push(v);
    out.push({ path: v, source });
  };

  // 1) 环境变量
  open("env");
  const envVal = agent.env ? process.env[agent.env] : "";
  if (!agent.env) mark("注册表里没有声明 env 变量");
  else if (!envVal) mark("未设置 " + agent.env);
  else add(envVal, "env");

  // 2) 用户配置
  open("config");
  const cfg = opts.config && opts.config.agents && opts.config.agents[agent.id];
  if (cfg && cfg.exe) add(cfg.exe, "config");
  else mark("未配置 agents." + agent.id + ".exe");

  // 3) PATH
  open("path");
  for (const b of agent.bin || []) {
    const hit = findOnPath(b);
    if (hit) add(hit, "path");
    else note(b, "not-on-path");
  }
  if (!bucket.hits.length) mark("PATH 里没有 " + (agent.bin || []).join(" / "));

  // 4) 注册表声明的位置
  open("known-dir");
  const known = agent.paths || [];
  if (!known.length) mark("注册表没声明 paths");
  for (const p of known) {
    const dirs = expandPattern(p);
    if (!dirs.length) { note(expand(p) || p, "missing"); continue; }
    for (const dir of dirs) {
      if (isFile(dir)) { add(dir, "known-dir"); continue; }
      // 声明形态本身就是文件（xxx.cmd / xxx.exe）→ 只试这个路径，别拼成 xxx.cmd\xxx
      if (/\.(exe|cmd|bat|ps1|js|mjs|cjs|py|sh)$/i.test(dir)) { add(dir, "known-dir"); continue; }
      for (const name of namesFor(agent)) add(path.join(dir, name), "known-dir");
    }
  }

  // 5) npm 全局根
  open("npm-root");
  const npmRoot = opts.npmRoot !== undefined ? opts.npmRoot : npmGlobalRoot();
  if (!npmRoot) mark("没识别到 npm 全局根（可设 CLAUDELINK_NPM_PREFIX）");
  else {
    for (const b of agent.bin || []) {
      for (const v of binVariants(b)) add(path.join(npmRoot, v), "npm-root");
      add(path.join(npmRoot, ".bin", b), "npm-root");
    }
    // Package manifests are authoritative when npm shims are absent or stale.
    const packages = {
      claude: ["@anthropic-ai/claude-code"], codex: ["@openai/codex"],
      codebuddy: ["@tencent-ai/codebuddy-code"], gemini: ["@google/gemini-cli"],
      qwen: ["@qwen-code/qwen-code"],
    };
    for (const pkg of agent.packages || packages[agent.id] || []) {
      try {
        const dir = path.join(npmRoot, pkg);
        const manifest = JSON.parse(fs.readFileSync(path.join(dir, "package.json"), "utf8"));
        const bins = typeof manifest.bin === "string" ? { [agent.id]: manifest.bin } : manifest.bin || {};
        for (const [name, target] of Object.entries(bins)) {
          if (!(agent.bin || []).includes(name) || typeof target !== "string") continue;
          const entry = path.resolve(dir, target);
          if (path.relative(dir, entry).startsWith("..")) continue;
          add(entry, "npm-package");
        }
      } catch { /* Missing or malformed package; keep the other discovery layers. */ }
    }
  }

  // 6) 通用搜索根
  open("search-root");
  const roots = opts.searchRoots || searchRoots();
  if (!roots.length) mark("没有任何可用的通用搜索根");
  for (const root of roots) {
    for (const b of agent.bin || []) {
      for (const v of binVariants(b)) {
        add(path.join(root, v), "search-root");
        add(path.join(root, b, v), "search-root");
      }
    }
  }

  // 7) 用户声明的额外搜索根（只浅扫：目录本身 + 一层子目录）
  open("extra-root");
  const extra = opts.extraRoots || extraRootsFor(opts.config);
  if (!extra.length) mark("未配置 detect.extra_roots（默认不启用）");
  const names = namesFor(agent);
  for (const dir of extra) {
    for (const name of names) add(path.join(dir, name), "extra-root");
    for (const hit of scanDir(dir, names, 1)) add(hit, "extra-root");
  }

  // 8) 注册表声明的扫描目录（含版本子目录）
  open("scan-dir");
  const scanDirs = agent.scanDirs || [];
  if (!scanDirs.length) mark("注册表没声明 scanDirs");
  for (const dir of scanDirs) {
    for (const d of expandPattern(dir)) {
      const hits = scanDir(d, names, 2);
      if (!hits.length) note(d, "no-match");
      for (const hit of hits) add(hit, "scan-dir");
    }
  }

  // 9) Everything（可选加速层）
  open("everything");
  const pr = opts.everything;
  if (!pr) mark("Everything 层未运行");
  else if (pr.status !== "ok" && pr.status !== "empty") mark(pr.reason || pr.status);
  else if (pr.status === "empty" && !(pr.hits && pr.hits[agent.id])) mark(pr.reason || "es.exe 没查到");
  else {
    const list = (pr.hits && pr.hits[agent.id]) || [];
    if (!list.length) {
      note("es.exe", "no-match");
      bucket.reason = "es.exe 没匹配到 " + (agent.bin || []).join(" / ");
    }
    for (const p of list) add(p, "everything");
  }

  // 收尾：命中优先，其次「不可用」，最后「不存在」
  for (const t of trace) {
    if (t.hits.length) t.status = "hit";
    else if (t.status !== "unavailable") t.status = "miss";
    t.triedCount = Math.max(t.triedCount, t.tried.length);
  }

  // 跨层去重：同一路径只保留**最先命中**的那一层（优先级语义与改造前一致）
  const seen = new Set();
  const uniq = [];
  for (const c of uniquePaths(out.map((x) => x.path))) {
    const key = IS_WIN ? c.toLowerCase() : c;
    if (seen.has(key)) continue;
    seen.add(key);
    const hit = out.find((x) => (IS_WIN ? x.path.toLowerCase() : x.path) === key);
    uniq.push({ path: c, source: (hit && hit.source) || "?" });
  }
  return { cands: uniq, trace };
}

/** 兼容旧签名：只要候选。 */
function candidatesFor(agent, opts = {}) {
  return exploreFor(agent, opts).cands;
}

/**
 * 组装一次检测共用的上下文（npm 根 / 搜索根 / extra_roots / Everything 探测结果）。
 * 探测结果只算一次，避免「每个 agent 都去 spawn 一遍 es.exe」。
 */
function contextFor(registry, opts = {}) {
  const cfg = opts.config || null;
  // 已经算过就别重复探测（detect() 会把 context 原样再传回来）
  const pr = opts.everything && opts.everything.status !== undefined
    ? opts.everything
    : (opts.everything === false ? null : everything.probe(registry, cfg, opts));
  return {
    config: cfg,
    npmRoot: opts.npmRoot !== undefined ? opts.npmRoot : npmGlobalRoot(),
    searchRoots: opts.searchRoots || searchRoots(),
    extraRoots: opts.extraRoots || extraRootsFor(cfg),
    everything: pr,
    collectTrace: !!opts.collectTrace,
  };
}

function versionFor(file, agent) {
  const args = agent.versionArgs || ["--version"];
  const node = findOnPath("node") || process.execPath;
  const isJs = /\.(js|mjs|cjs)$/i.test(file);
  // 无扩展名的入口：只有注册表声明了 node:true 的才按 node 脚本跑；
  // 否则可能是 python / shell 脚本（带 shebang），直接执行才对。
  const isBareNodeEntry = !isJs && !!agent.node && !/\.(exe|cmd|bat)$/i.test(file);
  if (isJs || isBareNodeEntry) return versionOf(node, [file, ...args]);
  return versionOf(file, args);
}

function isSupported(agent) {
  return agent.support === "builtin" || (agent.support === "generic" && !!agent.generic);
}

function detect(registry, opts = {}) {
  const shared = contextFor(registry, opts);
  const out = [];
  for (const agent of registry) {
    const { cands, trace } = exploreFor(agent, shared);
    if (!cands.length) continue;
    let first = cands[0], version = "";
    const probes = [];
    const configured = opts.config && opts.config.agents && opts.config.agents[agent.id];
    const pinned = first.source === "env" || (first.source === "config" && !(configured && configured.exe_auto));
    for (const candidate of cands.slice(0, pinned ? 1 : 6)) {
      const got = versionFor(candidate.path, agent) || "";
      probes.push({ path: candidate.path, ok: !!got });
      if (got) { first = candidate; version = got; break; }
    }
    const supported = isSupported(agent);
    out.push({
      id: agent.id,
      label: agent.label,
      path: first.path,
      source: first.source,
      candidates: cands,
      version: version || "?",
      support: agent.support,
      usable: supported && !!version,
      reason: !supported ? "unsupported" : version ? "ok" : "version-probe-failed",
      probes,
      node: agent.node ? (findOnPath("node") || process.execPath) : "",
      note: agent.note || "",
      // 接口契约（与 bin/attach.js）：MCP 注册命令模板，缺省 null = 不支持自动接入
      mcp: agent.mcp || null,
      ...(opts.collectTrace ? { trace } : {}),
      agent,
    });
  }
  return out;
}

/** 一次性迁移：旧版本把检测缓存写在 ~/.claudelink/agents.json（现在那里是用户注册表）。 */
function migrateLegacyCache(file) {
  // 只在「目标缓存就在同一个 ~/.claudelink 目录」时迁移。
  // 反向保护：缓存路径被 CLAUDELINK_AGENTS 指到别处（测试/自定义部署）时，
  // **绝不去动真实 home 里的 agents.json** —— 否则自检脚本会把用户的注册表搬走再删掉。
  const homeCacheDir = path.join(os.homedir(), ".claudelink");
  if (path.resolve(path.dirname(file)) !== path.resolve(homeCacheDir)) return;
  const legacy = path.join(homeCacheDir, "agents.json");
  if (path.resolve(file) === path.resolve(legacy)) return;      // 用户显式指到旧路径：不搬
  try {
    if (!fs.existsSync(legacy) || fs.existsSync(file)) return;
    const data = JSON.parse(fs.readFileSync(legacy, "utf8"));
    if (Array.isArray(data) || Array.isArray(data.agents)) return;  // 是用户注册表：绝对别动
    if (data && typeof data === "object") fs.renameSync(legacy, file);
  } catch { /* 迁移失败无所谓，下次还会再试 */ }
}

/** 写检测缓存（供 hub.py 读取）。只写「找到的」，不改用户 config。 */
function writeCache(found, file = agentsCachePath()) {
  migrateLegacyCache(file);
  const byId = {};
  for (const a of found) {
    byId[a.id] = {
      path: a.path, source: a.source, version: a.version,
      support: a.support, usable: a.usable, node: a.node || "",
    };
  }
  writeJsonAtomic(file, { schema: 1, generatedAt: new Date().toISOString(), generator: "claudelink-hub detect", byId });
  return file;
}

module.exports = {
  detect, candidatesFor, exploreFor, contextFor, writeCache, expandPattern,
  extraRootsFor, versionFor, namesFor, systemRoots,
};

if (require.main === module) {
  const args = process.argv.slice(2);
  const json = args.includes("--json");
  const write = args.includes("--write");
  const quiet = args.includes("--quiet");
  const found = detect(loadAgents());
  if (write) {
    const f = writeCache(found);
    if (!quiet) console.log("检测缓存：" + f);
  }
  if (json) {
    console.log(JSON.stringify(found.map(({ agent, ...rest }) => rest), null, 2));
  } else if (!quiet) {
    if (!found.length) console.log("没有识别到 AI Agent");
    else for (const a of found) console.log([a.id, a.version, a.source, a.path, a.usable ? "可用" : "未支持"].join("  |  "));
  }
}
