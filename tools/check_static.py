#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""静态体检：hub.py 与 web/index.html 的重复定义、悬空引用、未声明函数。

用法：python tools/check_static.py
输出里 "== 全部通过 ==" 表示五项检查都没有发现问题。

这套检查是 2026-09-26 那轮排查的产物：真 bug 里有两个是「同名定义被后一份覆盖」
（前端 fileLink、前端 diffHtml+renderStepDetail），肉眼看不出来，只能这么扫。
"""
import ast
import collections
import os
import re

HUB = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = os.path.join(HUB, "hub.py")
HTML = os.path.join(HUB, "web", "index.html")

bad = 0


def headline(text):
    global bad
    print(text)


# 1) hub.py：同一作用域内重复定义（模块级 / 同名 class 内 / 同名函数内）
src = open(PY, encoding="utf-8").read()
tree = ast.parse(src)
dup_py = []


def scan(nodes, where):
    counter = collections.Counter(
        n.name for n in nodes if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    )
    for name, count in counter.items():
        if count > 1:
            dup_py.append(f"{where}.{name} x{count}")
    for n in nodes:
        if isinstance(n, ast.ClassDef):
            scan(n.body, f"class {n.name}")
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            scan(n.body, f"def {n.name}")


scan(tree.body, "<module>")
headline("== hub.py 作用域内重复定义 ==")
print("  ", dup_py or "(无)")
bad += len(dup_py)

html = open(HTML, encoding="utf-8").read()
m = re.search(r"<script>(.*)</script>", html, re.S)
js = m.group(1) if m else ""
html_only = re.sub(r"<script>.*?</script>", "", html, flags=re.S)   # 只统计真正的 HTML，别把 JS 模板串里的 id 算进来

# 2) index.html：JS 重复函数名
dup_fn = [k for k, v in collections.Counter(
    re.findall(r"\bfunction\s+([A-Za-z_$][\w$]*)", js)).items() if v > 1]
headline("== index.html JS 重复函数名 ==")
print("  ", dup_fn or "(无)")
bad += len(dup_fn)

# 3) index.html：HTML 重复 id，以及「JS 动态创建的同名 id 与静态 id 撞名」
dup_id = [k for k, v in collections.Counter(
    re.findall(r'\sid="([^"]+)"', html_only)).items() if v > 1]
headline("== index.html HTML 重复 id ==")
print("  ", dup_id or "(无)")
bad += len(dup_id)
js_ids = set(re.findall(r'id="([^"]+)"', js))          # JS 里动态创建的 id（template / insertAdjacentHTML）
clash = sorted(js_ids & set(re.findall(r'\sid="([^"]+)"', html_only)))
headline("== JS 动态 id 与静态 id 撞名 ==")
print("  ", clash or "(无)")
bad += len(clash)

# 4) JS 引用了但 HTML / JS 里都不会出现的元素 id
html_ids = set(re.findall(r'id="([^"]+)"', html_only)) | js_ids
used_ids = set(re.findall(r"getElementById\(\s*['\"]([^'\"]+)['\"]", js))
missing_ids = sorted(used_ids - html_ids)
headline("== JS 引用但 HTML 里不存在的 id ==")
print("  ", missing_ids or "(无)")
bad += len(missing_ids)

# 5) 被调用但从未声明的函数名（漏定义最典型的炸点）
declared = set(re.findall(r"\bfunction\s+([A-Za-z_$][\w$]*)", js))
declared |= set(re.findall(r"\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=", js))
for grp in re.findall(r"\(([^()]*)\)\s*(?:=>|\{)", js) + re.findall(r"\bfunction[^(]*\(([^()]*)\)", js):
    for part in grp.split(","):
        name = part.strip().split("=")[0].strip()
        if re.fullmatch(r"[A-Za-z_$][\w$]*", name):
            declared.add(name)
declared |= set(re.findall(r"(?<![\w$.])([A-Za-z_$][\w$]*)\s*=>", js))   # 单参数箭头函数 (resolve => ...)
for mm in re.finditer(r"\{([^{}]*)\}\s*=", js):
    for part in mm.group(1).split(","):
        name = part.split(":")[-1].strip()
        if re.fullmatch(r"[A-Za-z_$][\w$]*", name):
            declared.add(name)
declared |= {"if", "for", "while", "switch", "return", "typeof", "new", "function", "catch",
             "else", "do", "in", "of", "instanceof", "void", "delete", "await", "yield", "case",
             "throw", "class", "try", "super", "this", "async"}
builtins = {"JSON", "Object", "Array", "String", "Number", "Boolean", "Map", "Set", "Date",
            "Math", "Promise", "RegExp", "Error", "EventSource", "parseInt", "parseFloat",
            "isNaN", "encodeURIComponent", "decodeURIComponent", "fetch", "alert", "confirm",
            "setTimeout", "clearTimeout", "setInterval", "structuredClone", "rgba"}
declared |= {"requestAnimationFrame", "cancelAnimationFrame", "prompt", "history", "navigator"}   # 浏览器全局
called = set(re.findall(r"(?<![.\w$])([A-Za-z_$][\w$]*)\s*\(", js))
missing_fn = sorted(called - declared - builtins)
headline("== 被调用但没声明的函数（含少量误报，需人工确认）==")
print("  ", missing_fn or "(无)")
bad += len(missing_fn)

print("\n== 全部通过 ==" if bad == 0 else f"\n== 有 {bad} 处需要看 ==")
