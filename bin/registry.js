"use strict";
/**
 * Agent 注册表：**声明式**描述"怎么找到 + 怎么跑"一个 AI Agent CLI。
 *
 * ★ 铁律：本文件不得出现任何机器专属绝对路径（作者把 Node.js 装在哪个盘都行）。
 *   所有位置都用「环境变量占位符（%APPDATA% / %LOCALAPPDATA% / %USERPROFILE%）」
 *   或「PATH 里的裸命令名」表达。
 *
 * 字段说明
 *   id          唯一标识（同时是 config.agents.<id> 的键、CLAUDELINK_<大写ID> 环境变量后缀）
 *   label       展示名
 *   bin         PATH 里可能的命令名（Windows 会自动补 .exe/.cmd/.bat 变体）
 *   env         用户覆盖用的环境变量（值 = 可执行文件绝对路径）
 *   versionArgs 探测版本用参数（默认 ["--version"]）
 *   support     builtin = hub.py 已有专用 runner
 *               generic = 用下面的 generic 声明即可跑（无需改 hub.py）
 *               detect  = 只识别，不装配（参数未验证）
 *   paths       额外的**便携**位置（只允许 %VAR% / ~ 形式）
 *   scanDirs    需要浅扫（含版本子目录）的目录，便携形式
 *   node        需要 node 解释器时写 true（装配时填 node 绝对路径）
 *   mcp         把这家的 MCP 注册命令**模板化**（attach.js 消费；缺省 = 不支持自动接入）
 *               { list:[...], add:[...], remove:[...] }，数组中 "{server}" 是服务命令占位；
 *               显式写 null = 该 CLI 没有 MCP 子命令（attach 只给手动步骤，不执行任何命令）。
 *   defaults    装进 config.agents.<id> 的默认值
 *   note        备注（doctor 展示）
 */

/** 通用运行声明模板：用户加新 Agent 时照这个填，hub.py 的 generic runner 会照做。 */
const GENERIC_TEMPLATE = {
  promptVia: "arg",            // arg | stdin | env
  promptFlag: ["-p"],          // promptVia=arg 时用；占位符 {prompt}
  promptEnv: "",               // promptVia=env 时用，如 "DSH_TASK"
  modelFlag: "",               // 如 "--model"（留空 = 不传模型）
  sessionArgs: [],             // 如 ["--session-id","{session}"]
  streamArgs: [],              // 如 ["--output-format","stream-json"]
  jsonLines: false,            // 输出是否为逐行 JSON（true 时 hub 会尝试解析 text/delta 字段）
};

const AGENTS = [
  {
    id: "claude",
    label: "Claude Code",
    bin: ["claude", "claude.cmd"],
    env: "CLAUDELINK_CLAUDE",
    versionArgs: ["--version"],
    support: "builtin",
    paths: ["%APPDATA%/npm/claude.cmd", "~/.local/bin/claude"],
    mcp: {
      list: ["mcp", "list"],
      add: ["mcp", "add", "--scope", "user", "claudelink", "--", "{server}"],
      remove: ["mcp", "remove", "--scope", "user", "claudelink"],
    },
    defaults: { label: "Claude", stream: true, relay: true, settings_profile: "", model: "" },
    note: "Claude Code：hub 已有 run_claude（stream-json / --resume）",
  },
  {
    id: "codex",
    label: "Codex CLI",
    bin: ["codex", "codex.cmd"],
    env: "CLAUDELINK_CODEX",
    versionArgs: ["--version"],
    support: "builtin",
    // Codex 桌面版会把 CLI 放在版本化子目录里 → 必须扫描，不能只靠 PATH
    paths: ["%LOCALAPPDATA%/OpenAI/Codex/bin", "%APPDATA%/npm/codex.cmd"],
    scanDirs: ["%LOCALAPPDATA%/OpenAI/Codex/bin"],
    // codex 的 mcp add 没有 --scope（默认写 ~/.codex/config.toml，受 CODEX_HOME 影响）
    mcp: {
      list: ["mcp", "list"],
      add: ["mcp", "add", "claudelink", "--", "{server}"],
      remove: ["mcp", "remove", "claudelink"],
    },
    defaults: { label: "Codex", stream: true, relay: true, model: "", model_provider: "", transport: "exec", effort: "", sandbox: "workspace-write" },
    note: "Codex CLI：exec --json / exec resume；复用本机登录与配置",
  },
  {
    id: "codebuddy",
    label: "CodeBuddy",
    bin: ["codebuddy", "codebuddy-code", "codebuddy.cmd"],
    env: "CLAUDELINK_CODEBUDDY",
    versionArgs: ["--version"],
    support: "builtin",
    node: true,
    paths: ["%APPDATA%/npm/codebuddy.cmd", "%LOCALAPPDATA%/Programs/codebuddy"],
    // codebuddy 用 -s 而不是 --scope（实测），存储位置 ~/.codebuddy/.mcp.json
    mcp: {
      list: ["mcp", "list"],
      add: ["mcp", "add", "-s", "user", "claudelink", "--", "{server}"],
      remove: ["mcp", "remove", "-s", "user", "claudelink"],
    },
    defaults: { label: "CodeBuddy", stream: true, relay: true, model: "", max_turns: null, node: "" },
    note: "CodeBuddy CLI：hub 已有 run_codebuddy（stream-json / --session-id）",
  },
  {
    id: "dsh",
    label: "DeepSeek Harness",
    bin: ["dsh", "dsh.cmd"],
    env: "CLAUDELINK_DSH",
    versionArgs: ["--version"],
    support: "builtin",
    node: true,
    paths: ["%APPDATA%/npm/dsh.cmd"],
    mcp: null,   // 实测：dsh 只有 web/plugin 子命令，没有 mcp → 不支持自动接入
    defaults: { label: "DeepSeek Harness", stream: true, relay: true, profile: "headless", model: "", model_capable: false, supports_native_memory: false, node: "" },
    note: "DeepSeek Harness：headless profile，prompt 走 DSH_TASK 环境变量",
  },

  // ── 以下为「识别为主」的 Agent：装好后 doctor 会报出来，参数需实测后再切成 generic ──
  { id: "gemini", label: "Gemini CLI", bin: ["gemini", "gemini.cmd"], env: "CLAUDELINK_GEMINI", support: "detect",
    defaults: { label: "Gemini", stream: true, relay: true }, note: "待实测非交互参数与输出格式" },
  { id: "qwen", label: "Qwen Code", bin: ["qwen", "qwen.cmd"], env: "CLAUDELINK_QWEN", support: "detect",
    defaults: { label: "Qwen", stream: true, relay: true }, note: "待实测非交互参数与输出格式" },
  { id: "opencode", label: "OpenCode", bin: ["opencode", "opencode.cmd"], env: "CLAUDELINK_OPENCODE", support: "detect",
    paths: ["%LOCALAPPDATA%/opencode"], defaults: { label: "OpenCode", stream: true, relay: true }, note: "待实测 run 子命令与输出格式" },
  { id: "crush", label: "Crush", bin: ["crush", "crush.cmd"], env: "CLAUDELINK_CRUSH", support: "detect",
    defaults: { label: "Crush", stream: true, relay: true }, note: "待实测非交互参数" },
  { id: "aider", label: "Aider", bin: ["aider", "aider.exe", "aider.cmd"], env: "CLAUDELINK_AIDER", support: "detect",
    paths: ["%APPDATA%/Python/Scripts", "%LOCALAPPDATA%/Programs/Python/Python3*/Scripts"],
    defaults: { label: "Aider", stream: true, relay: true }, note: "待实测 --message 非交互模式" },
  { id: "goose", label: "Goose", bin: ["goose", "goose.exe"], env: "CLAUDELINK_GOOSE", support: "detect",
    paths: ["%LOCALAPPDATA%/Programs/Goose", "~/.local/bin"], defaults: { label: "Goose", stream: true, relay: true }, note: "待实测 run 子命令" },
  { id: "amp", label: "Amp", bin: ["amp", "amp.cmd"], env: "CLAUDELINK_AMP", support: "detect",
    defaults: { label: "Amp", stream: true, relay: true }, note: "待实测非交互参数" },
  { id: "copilot", label: "GitHub Copilot CLI", bin: ["copilot", "copilot.cmd"], env: "CLAUDELINK_COPILOT", support: "detect",
    defaults: { label: "Copilot", stream: true, relay: true }, note: "待实测 -p 非交互模式" },
  { id: "cursor-agent", label: "Cursor Agent", bin: ["cursor-agent", "cursor-agent.cmd"], env: "CLAUDELINK_CURSOR_AGENT", support: "detect",
    paths: ["~/.local/bin"], defaults: { label: "Cursor", stream: true, relay: true }, note: "待实测 --print 模式" },
  { id: "continue", label: "Continue CLI", bin: ["cn", "cn.cmd"], env: "CLAUDELINK_CONTINUE", support: "detect",
    defaults: { label: "Continue", stream: true, relay: true }, note: "待实测非交互参数" },
  { id: "ollama", label: "Ollama", bin: ["ollama", "ollama.exe"], env: "CLAUDELINK_OLLAMA", support: "detect",
    paths: ["%LOCALAPPDATA%/Programs/Ollama", "%ProgramFiles%/Ollama"],
    defaults: { label: "Ollama", stream: true, relay: true }, note: "本地模型运行器，不是 CLI Agent；更适合作为自定义 HTTP 模型接入" },
];

/**
 * 合并注册表：内置 → CLAUDELINK_REGISTRY（JS 文件，导出 {AGENTS}）→ ~/.claudelink/agents.json（JSON 数组）。
 * 后两者按 id 覆盖内置条目 —— 这就是「用户自己加 Agent 不用改代码」的入口。
 */
function loadAgents() {
  const byId = new Map();
  for (const a of AGENTS) byId.set(a.id, a);

  const merge = (list, source) => {
    if (!Array.isArray(list)) return;
    for (const a of list) {
      if (!a || !a.id) continue;
      const prev = byId.get(a.id) || {};
      const merged = Object.assign({}, prev, a, {
        support: a.support || prev.support || "detect",
        defaults: Object.assign({}, prev.defaults || {}, a.defaults || {}),
      });
      merged._source = source;
      byId.set(a.id, merged);
    }
  };

  const override = process.env.CLAUDELINK_REGISTRY;
  if (override) {
    try {
      const mod = require(override.startsWith(".") ? require("path").resolve(override) : override);
      if (mod && Array.isArray(mod.AGENTS)) merge(mod.AGENTS, "CLAUDELINK_REGISTRY");
    } catch (err) {
      console.error("CLAUDELINK_REGISTRY 加载失败：" + err.message);
    }
  }

  try {
    const fs = require("fs");
    const { userAgentsPath } = require("./util");
    // ★ 只读「用户注册表」；检测缓存是另一个文件（agents.detected.json），两者互不覆盖
    const userFile = userAgentsPath();
    if (fs.existsSync(userFile)) {
      const data = JSON.parse(fs.readFileSync(userFile, "utf8"));
      // 兼容两种形态：{agents:[...]} 或 [...]；检测缓存形态（{schema,byId}）直接忽略
      const list = Array.isArray(data) ? data : (Array.isArray(data.agents) ? data.agents : null);
      if (list) merge(list, "user-agents.json");
    }
  } catch { /* 用户文件坏了不影响内置 */ }

  return Array.from(byId.values());
}

module.exports = { AGENTS, GENERIC_TEMPLATE, loadAgents };
