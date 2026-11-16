#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""hub.py 侧自测（被 scripts/selftest.js 调用，也可单独跑）。

做法：用 AST 从 hub.py 里**只抽出**要测的函数/常量来 exec —— 不 import hub.py
（import 会连配置、DB、HTTP 一起拉起来）。覆盖：

  H5  env_or_cfg 跳过"路径形态但不存在"的值
  C2  检测缓存是数组/坏形态时不崩
  H1  wrap_command 对含空格 .cmd 路径 + 带空格参数的处理
  M10 resolve_command 不接受目录

退出码：0 全过；1 有失败。
"""
import ast
import atexit
import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
HUB = os.path.join(HERE, "..", "hub.py")

WANT_FUNCS = {
    "_quote_win_arg", "_win_short_path", "wrap_command", "_looks_like_path",
    "env_or_cfg", "detected_agents", "_detected_by_id", "detected_exe", "detected_node",
    "resolve_command", "candidate_bin_dirs", "_npm_prefix", "cli_exe",
}
WANT_ASSIGN = {
    "_BIN_GLOBS", "_BIN_DIRS", "CLI_ENV", "CLI_DEFAULTS", "CLI_NODE_DEFAULT",
    "_NPM_PREFIX_CACHE", "_DETECTED_CACHE", "CLAUDE_SETTINGS_DEFAULT",
}

failures = []


def check(name, cond, detail=""):
    if cond:
        print("  ✓ " + name)
    else:
        failures.append(name)
        print("  ✗ " + name + ("  → " + str(detail) if detail else ""))


def load_ns():
    src = open(HUB, encoding="utf-8").read()
    tree = ast.parse(src)
    parts = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in WANT_FUNCS:
            parts.append(ast.get_source_segment(src, node))
        elif isinstance(node, ast.Assign):
            if any(isinstance(t, ast.Name) and t.id in WANT_ASSIGN for t in node.targets):
                parts.append(ast.get_source_segment(src, node))
    ns = {
        "os": os, "sys": sys, "json": json, "glob": __import__("glob"),
        "shutil": __import__("shutil"), "subprocess": subprocess,
        "CONFIG": {"agents": {}},
    }
    exec("\n\n".join(parts), ns)
    return ns


def main():
    ns = load_ns()

    print("== H5: env_or_cfg 跳过失效路径 ==")
    got = ns["env_or_cfg"]("__CLH_NOPE__", r"C:\does\not\exist\x.exe", "claude")
    check("失效绝对路径被跳过", got == "claude", got)
    got2 = ns["env_or_cfg"]("__CLH_NOPE__", "", r"C:\does\not\exist\x.exe")
    check("default 原样返回（不校验存在性）", got2.endswith("x.exe"), got2)

    print("== C2: 检测缓存形态防御 ==")
    d = tempfile.mkdtemp(prefix="clh-py-")
    cache = os.path.join(d, "agents.json")
    open(cache, "w", encoding="utf-8").write('[{"id":"myagent"}]')
    old = os.environ.get("CLAUDELINK_AGENTS")
    os.environ["CLAUDELINK_AGENTS"] = cache
    ns["_DETECTED_CACHE"].update({"mtime": None, "data": {}})
    try:
        check("数组缓存 → detected_agents 返回 {}", ns["detected_agents"]() == {})
        check("detected_exe 不抛异常", ns["detected_exe"]("claude") == "")
        exe = ns["cli_exe"]("claude")
        check("cli_exe 回落到裸名/缓存", isinstance(exe, str) and exe != "", exe)
    except Exception as exc:                                    # noqa: BLE001
        check("缓存防御", False, "%s: %s" % (type(exc).__name__, exc))
    finally:
        if old is None:
            os.environ.pop("CLAUDELINK_AGENTS", None)
        else:
            os.environ["CLAUDELINK_AGENTS"] = old

    print("== M10: resolve_command 只接受普通文件（同名时跳过目录）==")
    base = tempfile.mkdtemp(prefix="clh-res-")
    atexit.register(shutil.rmtree, base, True)
    dir_dir = os.path.join(base, "A")
    file_dir = os.path.join(base, "B")
    os.makedirs(os.path.join(dir_dir, "mytool"))          # A/mytool 是目录
    os.makedirs(file_dir)
    tool = os.path.join(file_dir, "mytool")               # B/mytool 是文件
    open(tool, "w").close()
    if os.name != "nt":
        os.chmod(tool, 0o755)
    old_path = os.environ.get("PATH", "")
    os.environ["PATH"] = dir_dir + os.pathsep + file_dir + os.pathsep + old_path
    try:
        got3 = ns["resolve_command"]("mytool")
        check("同名时选文件而不是目录", os.path.abspath(got3) == os.path.abspath(tool), got3)
    finally:
        os.environ["PATH"] = old_path

    if os.name == "nt":
        print("== H1: wrap_command（含空格 .cmd 路径）==")
        d3 = tempfile.mkdtemp(prefix="clh test ")
        out = os.path.join(d3, "out.txt")
        cmd = os.path.join(d3, "e.cmd")
        with open(cmd, "w", encoding="utf-8", newline="\r\n") as fh:
            fh.write("@echo off\r\nchcp 65001 >nul\r\n(for %%a in (%*) do echo [%%~a]) > \"%~dp0out.txt\"\r\n")
        wrapped = ns["wrap_command"]([cmd, "--x", "hello world", "中文 提示"])
        subprocess.run(wrapped, capture_output=True, timeout=30)
        text = ""
        if os.path.exists(out):
            for enc in ("utf-8", "gbk"):
                try:
                    text = open(out, encoding=enc).read(); break
                except Exception:                              # noqa: BLE001
                    continue
        check("--x 送达", "--x" in text, text.strip()[:80])
        check("含空格参数送达", "hello world" in text, text.strip()[:80])
        check("中文参数送达", "中文 提示" in text, text.strip()[:80])
    else:
        print("== H1: 跳过（仅 Windows 需要 cmd 包装）==")

    print()
    if failures:
        print("✗ Python 自测失败 %d 项：%s" % (len(failures), ", ".join(failures)))
        return 1
    print("✅ Python 自测全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
