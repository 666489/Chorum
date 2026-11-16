#!/usr/bin/env node
"use strict";
/**
 * 安装时自动识别 AI Agent（npm postinstall）。
 * 原则：**尽力而为、永不阻断安装** —— 任何异常都只打印提示，退出码恒为 0。
 *
 * 接入策略（T7）：
 *   - 默认**只提示**：不执行任何会改动第三方 CLI 配置的命令（上一轮"第三方覆盖"把官方 app 改坏过）。
 *   - CLAUDELINK_AUTO_ATTACH=1 才真正执行 attach（等价 setup --attach --yes）。
 */
try {
  const path = require("path");
  const { detect, writeCache } = require("./detect");
  const { loadAgents } = require("./registry");
  const { withFileLock, agentsCachePath } = require("./util");
  const { readJson, expand, dataDir, table } = require("./util");
  // 安装时也用上用户已有配置（detect.extra_roots / detect.everything）；没有配置就退回默认 —— 默认不扫全盘。
  let cfg = null;
  try {
    const file = process.env.CLAUDELINK_CONFIG
      ? path.resolve(expand(process.env.CLAUDELINK_CONFIG))
      : path.join(dataDir(path.resolve(__dirname, "..")), "config.json");
    cfg = readJson(file, null);
  } catch { /* 读不到 / 坏了 → 当没有配置 */ }
  const found = detect(loadAgents(), cfg ? { config: cfg } : {});
  if (found.length) {
    // 与 CLI 的 detect --write / doctor / start 共用一把锁：安装时别把别人刚写的缓存换掉
    const cache = withFileLock(agentsCachePath(), () => writeCache(found), { timeoutMs: 3000 });
    console.log("[chorum] 识别到 " + found.length + " 个 AI Agent → " + cache);
    const lines = table(found, [
      { title: "成员", get: (a) => a.id },
      { title: "版本", get: (a) => a.version || "?" },
      { title: "路径", get: (a) => a.path },
    ], { header: false });
    for (const line of lines) console.log("  " + line);
    if (!found.some((a) => a.usable)) {
      console.log("[chorum] 识别到的都还不在支持列表里；可运行 chorum doctor 查看");
    }
  } else {
    console.log("[chorum] 暂未识别到 AI Agent。装好任何一个后运行：chorum detect --write");
  }

  if (process.env.CLAUDELINK_AUTO_ATTACH === "1") {
    const { runAttach } = require("./attach");
    console.log("[chorum] CLAUDELINK_AUTO_ATTACH=1 → 自动接入 ClaudeLink MCP");
    const code = runAttach({ yes: true });
    console.log("[chorum] 自动接入结束（退出码 " + code + "）；明细见 attach-state.json");
  } else if (found.length) {
    console.log("[chorum] 检测到 " + found.length + " 个 Agent，运行 chorum setup --attach 可自动接入" +
      "（默认只 dry-run，不改任何第三方配置）");
  }
} catch (err) {
  console.log("[chorum] 安装时检测/接入已跳过：" + (err && err.message ? err.message : err));
}
process.exitCode = 0;
