#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把「Chorum 终端版」装进全局 claudelink MCP。

做两件事：
  1. 把 tui-launch.js 放到 <包>/dist/ ；
  2. 给 <包>/dist/index.js 加 MCP prompt（tui）与工具（open_terminal）。

幂等：已打过补丁就跳过第 2 步。
npm 升级会覆盖 dist/，升级后重跑本脚本即可：

    D:\Anaconda3\envs\py11\python.exe tools\mcp-tui\install.py
"""
import argparse
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
MARK = "open_terminal"


def global_root():
    if os.environ.get("CLAUDELINK_PKG"):
        return os.environ["CLAUDELINK_PKG"]
    try:
        out = subprocess.run(["npm", "root", "-g"], capture_output=True, text=True,
                             timeout=20, shell=True)
        root = (out.stdout or "").strip().splitlines()
        if root:
            return os.path.join(root[-1], "claudelink")
    except Exception:
        pass
    for cand in (r"D:\Node.js\node_global\node_modules\claudelink",
                 os.path.join(os.environ.get("APPDATA", ""), "npm", "node_modules", "claudelink")):
        if os.path.isdir(cand):
            return cand
    return ""


def patch_index(path):
    with open(path, encoding="utf-8") as fh:
        src = fh.read()
    if MARK in src:
        return "already"

    # 产品已更名 Chorum：连上游包原生的显示名（serverInfo.name、启动日志）一起改掉
    src = src.replace("ClaudeLink", "Chorum")

    def one(old, new, label):
        nonlocal src
        n = src.count(old)
        if n != 1:
            raise SystemExit("锚点 %s 命中 %d 次，版本可能变了；未修改" % (label, n))
        src = src.replace(old, new, 1)

    one('{ capabilities: { tools: {} } }',
        '{ capabilities: { tools: {}, prompts: {} } }', "capabilities")
    one('const ui_launcher_js_1 = require("./ui-launcher.js");',
        'const ui_launcher_js_1 = require("./ui-launcher.js");\n'
        'const tui_launch_js_1 = require("./tui-launch.js");', "require")

    block = '''        {
            name: "open_terminal",
            description: "Open the Chorum terminal client (TUI) in a new console window. Use when the user asks for the terminal version, a TUI, or a non-browser view of the group chat.",
            inputSchema: {
                type: "object",
                properties: {},
            },
        },
    ],
}));
// --- Prompts: an MCP prompt becomes a slash command in clients such as CodeBuddy and Claude Code ---
server.setRequestHandler(types_js_1.ListPromptsRequestSchema, async () => ({
    prompts: [
        {
            name: "tui",
            title: "Chorum 终端版",
            description: "在新控制台窗口打开 Chorum 终端版（直连中枢，SSE 实时，不占当前会话）",
            arguments: [],
        },
    ],
}));
server.setRequestHandler(types_js_1.GetPromptRequestSchema, async (request) => {
    if (request.params.name !== "tui") {
        throw new Error("Unknown prompt: " + request.params.name);
    }
    return {
        description: "打开 Chorum 终端版",
        messages: [
            {
                role: "user",
                content: {
                    type: "text",
                    text: "打开 Chorum 终端版：调用 open_terminal 工具（全名可能是 mcp__claudelink__open_terminal，或当前客户端里的等价名字）。它会在新的控制台窗口启动 TUI，不要在当前终端前台运行 hub_tui.py。成功后用一句话回报：窗口已打开，Enter 发送、鼠标滚轮上下查看、Ctrl+C 退出。",
                },
            },
        ],
    };
});
// --- Call Tool ---'''
    one('    ],\n}));\n// --- Call Tool ---', block, "tool-list-end")

    case = '''            case "open_terminal": {
                const info = tui_launch_js_1.openTerminal();
                return {
                    content: [{ type: "text", text: "Opened Chorum TUI in a new window: " + info.hub + " (python " + info.python + ")" }],
                };
            }
            default:'''
    one('            default:', case, "default-case")

    bak = path + ".bak-pre-tui"
    if not os.path.exists(bak):
        shutil.copy2(path, bak)
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(src)
    return "patched"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--package", default="", help="claudelink 包目录")
    args = ap.parse_args()
    pkg = args.package or global_root()
    if not pkg or not os.path.isdir(pkg):
        print("找不到全局 claudelink 包；用 --package 指定目录", file=sys.stderr)
        return 2
    dist = os.path.join(pkg, "dist")
    if not os.path.isdir(dist):
        print("包里没有 dist/：" + pkg, file=sys.stderr)
        return 2

    src_launcher = os.path.join(HERE, "tui-launch.js")
    dst_launcher = os.path.join(dist, "tui-launch.js")
    shutil.copy2(src_launcher, dst_launcher)

    status = patch_index(os.path.join(dist, "index.js"))
    print("package:", pkg)
    print("launcher:", dst_launcher)
    print("index:", status)
    return 0


if __name__ == "__main__":
    sys.exit(main())
