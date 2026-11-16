#!/usr/bin/env node
"use strict";
/**
 * 发布前守门（npm run check / prepublishOnly）。
 *
 * 扫描集合 = `package.json` 的 files 白名单 **+ package.json 自身**
 *            （npm 会把 package.json 一并发布，但它不在 files 里）。
 *
 * A. 硬拦截：密钥形态 / 用户目录绝对路径 / 非默认盘安装路径 / 邮箱 / `.leak-denylist` 私有词
 *    → 命中即打码打印并以非 0 退出。
 * B. 软提醒：目录里存在现场文件（hub.db、uploads/…）时只提示"别 git add / 别整目录外发"。
 *
 * 本文件**不含任何作者专属路径字面量**：私有项目名/机器名请写进仓库根的
 * `.leak-denylist`（一行一个，已 gitignore）。
 */
const fs = require("fs");
const path = require("path");

const ROOT = path.resolve(__dirname, "..");
const pkg = JSON.parse(fs.readFileSync(path.join(ROOT, "package.json"), "utf8"));

const RULES = [
  // 常见 Key 形态：sk-xxx / sk_xxx / ak_xxx / ark-xxx
  { name: "API Key（sk/ak/ark）", re: /\b(?:sk|ak|ark)[-_][A-Za-z0-9_-]{16,}/g },
  { name: "GitHub Token", re: /\bgh[pousr]_[A-Za-z0-9]{20,}/g },
  { name: "Google API Key", re: /\bAIza[A-Za-z0-9_-]{20,}/g },
  { name: "Slack Token", re: /\bxox[baprs]-[A-Za-z0-9-]{10,}/g },
  { name: "私钥", re: /-----BEGIN [A-Z ]*PRIVATE KEY-----/g },
  { name: "JWT", re: /\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\./g },
  { name: "Bearer 字面量", re: /Bearer\s+[A-Za-z0-9._-]{20,}/g },
  // 真实用户主目录：Windows 正/反斜杠、macOS /Users、Linux /home（排除公共目录）
  { name: "用户目录绝对路径", re: /[A-Za-z]:[\\/]{1,2}Users[\\/]{1,2}(?!(?:Public|Default|All Users)[\\/])[A-Za-z0-9._-]+/g },
  { name: "POSIX 用户目录", re: /(?:^|[\s"'(])\/(?:home|Users)\/(?![A-Za-z0-9._-]*\b(?:Public|Shared)\b)[A-Za-z0-9._-]+/g },
  // 常见"装在非默认盘"的工具目录（通用工具名，不含作者项目名）
  { name: "非默认盘安装路径", re: /[A-Za-z]:[\\/]{1,2}(?:Anaconda\d?|miniconda\d?|ProgramData[\\/]Anaconda\d?)\b/gi },
  // 邮箱；排除 git@github.com 这类 VCS SSH 形式（不是个人信息）
  { name: "邮箱", re: /\b[A-Za-z0-9._%+-]+@(?!(?:github\.com|gitlab\.com|bitbucket\.org)\b)[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b/g },
];

// 作者私有词（一行一个，已 gitignore）
const DENYLIST_FILE = path.join(ROOT, ".leak-denylist");
let denyWords = [];
try {
  denyWords = fs.readFileSync(DENYLIST_FILE, "utf8")
    .split(/\r?\n/).map((s) => s.trim()).filter((s) => s && !s.startsWith("#"));
} catch { /* 没有就算了 */ }
if (denyWords.length) {
  const escaped = denyWords.map((w) => w.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("|");
  // 不用 \b（对中文无效），用前后不能是字母数字的弱边界
  RULES.push({ name: "私有词（.leak-denylist）", re: new RegExp("(?<![A-Za-z0-9])(?:" + escaped + ")(?![A-Za-z0-9])", "gi") });
}

const MAX_BYTES = 8 * 1024 * 1024;

function walk(entry, out) {
  const p = path.join(ROOT, entry);
  let st;
  try { st = fs.statSync(p); } catch { return; }
  if (st.isDirectory()) {
    for (const name of fs.readdirSync(p)) {
      if (name === "node_modules" || name === "__pycache__" || name.startsWith(".")) continue;
      walk(path.join(entry, name), out);
    }
  } else if (st.isFile()) {
    out.push(entry);
  }
}

function mask(s) {
  const t = String(s);
  return t.length <= 12 ? t.slice(0, 3) + "***" : t.slice(0, 6) + "***" + t.slice(-4);
}

// ── 扫描集合 ──
const files = [];
for (const entry of pkg.files || []) walk(entry, files);
if (!files.includes("package.json")) files.push("package.json");

let hits = 0;
let scanned = 0;
const skipped = [];
for (const rel of files) {
  const abs = path.join(ROOT, rel);
  let text;
  try {
    const size = fs.statSync(abs).size;
    if (size > MAX_BYTES) { skipped.push(rel + "（>8MB）"); continue; }
    text = fs.readFileSync(abs, "utf8");
  } catch (err) {
    skipped.push(rel + "（" + err.code + "）");
    continue;
  }
  scanned++;
  const lines = text.split(/\r?\n/);
  for (let i = 0; i < lines.length; i++) {
    for (const rule of RULES) {
      rule.re.lastIndex = 0;
      const m = rule.re.exec(lines[i]);
      if (!m) continue;
      hits++;
      console.error("  ✗ " + rel + ":" + (i + 1) + "  [" + rule.name + "]  " + mask(m[0]));
    }
  }
}

console.log("[check-secrets] 扫描 " + scanned + " 个文件（" + (pkg.files || []).length + " 个白名单 + package.json），规则 " + RULES.length + " 条");
if (skipped.length) console.log("[check-secrets] 跳过 " + skipped.length + " 个：" + skipped.slice(0, 5).join("、"));
if (scanned === 0) {
  console.error("[check-secrets] ✗ 一个文件都没扫到 —— files 白名单可能写错了，视为失败。");
  process.exit(1);
}
if (hits) {
  console.error("[check-secrets] 命中 " + hits + " 处 —— 已中止发布。清理后再发，或把该文件移出 files 白名单。");
  process.exit(1);
}
console.log("[check-secrets] 未发现密钥/作者隐私 ✅");

// ── 软提醒：不该提交/外发的现场文件 ──
const RISKY = ["hub.db", "hub.db-wal", "config.json", "uploads", "hub.log", ".verify", "backup",
               "memory.db", "docs/history", "docs/reports"];
const present = RISKY.filter((f) => { try { fs.statSync(path.join(ROOT, f)); return true; } catch { return false; } });
if (present.length) {
  console.log("[check-secrets] 提醒：以下现场文件存在，且**不会**被打包 —— 但请勿 `git add` / 整目录外发：");
  console.log("              " + present.join("、"));
}
