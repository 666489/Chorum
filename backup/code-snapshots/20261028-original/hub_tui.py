#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ClaudeLink 终端版（零依赖）。

直连中枢的 HTTP + SSE，不轮询、不写库：

    python hub_tui.py                 # 全屏界面
    python hub_tui.py --dump 50       # 打印最近 50 条（管道友好，不带 ANSI）
    python hub_tui.py --render-once   # 渲染一帧到 stdout（调试布局）
    python hub_tui.py --check         # 健康检查，输出一行 JSON
    python hub_tui.py --no-start      # 中枢没跑时报错退出，不自动拉起

键位：Enter 发送；←/→ 切换目标；↑/↓ 翻发送历史；Ctrl+R 搜索历史；鼠标滚轮上下查看；
      /all 回全体；/stop 停本轮；/relay 切换接力；
      /interrupt 1 打断单个成员；/retry <消息id> 重试；/think 折叠思维链；
      /round 多 CLI 角色圆桌（同一 Agent 可重复；固定顺序/轮数/系统提示；/round stop｜resume｜status｜again）；
      /help；Ctrl+C 退出。

消息正文支持 Markdown：加粗、斜体、行内代码、围栏代码块、标题、列表、引用、链接。
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.request
import uuid
from datetime import datetime

HUB_DIR = os.path.dirname(os.path.abspath(__file__))
# 与 hub.py / npm CLI 保持同一份配置：优先 CLAUDELINK_CONFIG，其次数据目录（默认包目录）
DATA_DIR = os.path.abspath(os.environ.get("CLAUDELINK_HOME") or HUB_DIR)
CONFIG_PATH = os.path.abspath(os.environ.get("CLAUDELINK_CONFIG") or os.path.join(DATA_DIR, "config.json"))
TOKEN_PATH = os.path.join(DATA_DIR, "hub.token")
ROUNDTABLE_PATH = os.path.join(DATA_DIR, "roundtable.json")
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

RESET = "\x1b[0m"
DIM = "\x1b[2m"
REV = "\x1b[7m"
C_USER = "\x1b[92m"
C_SYS = "\x1b[2;37m"
C_ERR = "\x1b[91m"
C_MIRROR = "\x1b[35m"
C_HL = "\x1b[93m"
C_TITLE = "\x1b[7m"
C_BAR = "\x1b[44;97m"
C_INPUT = "\x1b[96m"
C_OK = "\x1b[32m"
C_OFF = "\x1b[90m"
C_WARN = "\x1b[33m"
AGENT_COLORS = ["\x1b[36m", "\x1b[35m", "\x1b[33m", "\x1b[34m", "\x1b[32m", "\x1b[91m"]


# ---------------------------------------------------------------- 基础工具

def load_endpoint():
    host, port = "127.0.0.1", 8787
    try:
        with open(CONFIG_PATH, encoding="utf-8") as fh:
            cfg = json.load(fh)
        srv = cfg.get("server") or {}
        host = str(srv.get("host") or host)
        port = int(srv.get("port") or port)
    except Exception:
        pass
    if host in ("0.0.0.0", "::", ""):
        host = "127.0.0.1"
    return host, port


def load_token():
    env = (os.environ.get("CLAUDELINK_TOKEN") or "").strip()
    if env:
        return env
    try:
        with open(TOKEN_PATH, encoding="utf-8") as fh:
            return fh.read().strip()
    except Exception:
        return ""


ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


def strip_ansi(text):
    return ANSI_RE.sub("", str(text or ""))


def char_width(ch):
    if unicodedata.combining(ch):
        return 0
    if unicodedata.east_asian_width(ch) in ("W", "F"):
        return 2
    if ord(ch) < 32:
        return 0
    return 1


def disp_width(text):
    # 渲染层可能带 ANSI（Markdown 行内样式）；计算宽度时整段跳过。
    return sum(char_width(ch) for ch in strip_ansi(text))


def clip_disp(text, width):
    if width <= 0:
        return ""
    text = str(text or "")
    out, used, i = [], 0, 0
    while i < len(text):
        m = ANSI_RE.match(text, i)
        if m:
            out.append(m.group(0))
            i = m.end()
            continue
        ch = text[i]
        w = char_width(ch)
        if used + w > width:
            if used < width:
                out.append("…")
            break
        out.append(ch)
        used += w
        i += 1
    return "".join(out)


def wrap_text(text, width):
    """按显示列宽硬换行；保留显式换行，制表符展开。"""
    if width <= 0:
        return []
    out = []
    for raw in str(text or "").replace("\t", "    ").split("\n"):
        if raw == "":
            out.append("")
            continue
        line, used = "", 0
        for ch in raw:
            w = char_width(ch)
            if used + w > width and line:
                out.append(line)
                line, used = ch, w
            else:
                line += ch
                used += w
        out.append(line)
    return out


def term_size():
    try:
        size = shutil.get_terminal_size()
    except Exception:
        return 100, 30
    return max(20, size.columns), max(8, size.lines)


def enable_vt():
    """打开 VT 输出 + VT 输入。

    滚轮要能作为 SGR 序列从 stdin 读到，光开输出 VT 不够：
    Windows Terminal 需要 ENABLE_VIRTUAL_TERMINAL_INPUT (0x0200)；
    同时关掉 QuickEdit (0x0040)，否则鼠标滚轮会被“选择文本”模式吞掉。
    """
    if os.name != "nt":
        return
    try:
        import ctypes

        k32 = ctypes.windll.kernel32
        out = k32.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if k32.GetConsoleMode(out, ctypes.byref(mode)):
            k32.SetConsoleMode(out, mode.value | 0x0001 | 0x0004)
        inp = k32.GetStdHandle(-10)
        imode = ctypes.c_uint32()
        if k32.GetConsoleMode(inp, ctypes.byref(imode)):
            # ENABLE_VIRTUAL_TERMINAL_INPUT | ENABLE_EXTENDED_FLAGS, clear QuickEdit
            new_mode = (imode.value | 0x0200 | 0x0080) & ~0x0040
            k32.SetConsoleMode(inp, new_mode)
    except Exception:
        pass


def fmt_secs(sec):
    sec = max(0, int(sec))
    if sec < 60:
        return f"{sec}s"
    if sec < 3600:
        return f"{sec // 60}m{sec % 60:02d}s"
    return f"{sec // 3600}h{(sec % 3600) // 60:02d}m"


def fmt_chars(n):
    n = int(n or 0)
    if n < 1000:
        return str(n)
    if n < 10000:
        return f"{n / 1000:.1f}k"
    return f"{n // 1000}k"


# ---------------------------------------------------------------- HTTP / SSE

class Client:
    def __init__(self, host, port):
        self.host, self.port = host, port
        self.base = f"http://{host}:{port}"
        self.token = load_token()

    def _headers(self, extra=None):
        token = self.token or load_token()
        if token:
            self.token = token
        headers = dict(extra or {})
        if token:
            headers["X-ClaudeLink-Token"] = token
        return headers

    def get(self, path, timeout=5):
        req = urllib.request.Request(self.base + path, headers=self._headers())
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def post(self, path, payload=None, timeout=15):
        data = json.dumps(payload or {}, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            self.base + path, data=data,
            headers=self._headers({"Content-Type": "application/json; charset=utf-8"}),
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            # 中枢的 4xx 正文里有可读的 error（如「轮数必须在 1-20 之间」），
            # urllib 默认只给 "HTTP Error 400"，向导里看不出该改哪。
            try:
                detail = json.loads(exc.read().decode("utf-8")).get("error")
            except Exception:
                detail = ""
            raise RuntimeError(str(exc.code) + ": " + (detail or str(exc.reason))) from exc

    def health(self, timeout=1.2):
        try:
            return self.get("/api/health", timeout)
        except Exception:
            return None

    def stream(self, on_event, stopping):
        backoff = 1.0
        while not stopping():
            conn = None
            try:
                conn = http.client.HTTPConnection(self.host, self.port, timeout=90)
                conn.request("GET", "/api/stream", headers=self._headers({"Accept": "text/event-stream"}))
                resp = conn.getresponse()
                if resp.status != 200:
                    raise RuntimeError(f"stream status {resp.status}")
                on_event({"type": "__connected__"})
                backoff = 1.0
                data_lines = []
                while not stopping():
                    raw = resp.readline()
                    if not raw:
                        break
                    line = raw.decode("utf-8", "replace").rstrip("\r\n")
                    if line.startswith("data:"):
                        data_lines.append(line[5:].lstrip())
                    elif line == "":
                        if data_lines:
                            payload = "\n".join(data_lines)
                            data_lines = []
                            try:
                                on_event(json.loads(payload))
                            except Exception:
                                pass
            except Exception:
                pass
            finally:
                try:
                    if conn is not None:
                        conn.close()
                except Exception:
                    pass
            on_event({"type": "__disconnected__"})
            if stopping():
                return
            time.sleep(backoff)
            backoff = min(backoff * 1.5, 8.0)


def start_hub():
    exe = sys.executable
    env = os.environ.copy()
    # ★ 不设任何机器专属路径：hub.py 自己按
    #   环境变量 → config.agents.*.exe → ~/.claudelink/agents.json（安装时检测结果）→ PATH 解析。
    env.setdefault("CLAUDELINK_CLAUDE_SETTINGS_DIR",
                   os.path.join(os.path.expanduser("~"), ".ccm", "settings"))
    # 让 hub.py 与 TUI 读同一份配置/缓存（否则连的端口可能不是同一个）
    env["CLAUDELINK_CONFIG"] = CONFIG_PATH
    env["CLAUDELINK_HOME"] = DATA_DIR
    if os.environ.get("CLAUDELINK_AGENTS"):
        env["CLAUDELINK_AGENTS"] = os.environ["CLAUDELINK_AGENTS"]
    log_path = os.path.join(DATA_DIR, "hub.log")
    log = open(log_path, "ab", buffering=0)
    subprocess.Popen(
        [exe, os.path.join(HUB_DIR, "hub.py")],
        cwd=HUB_DIR, env=env, stdout=log, stderr=log,
        stdin=subprocess.DEVNULL, creationflags=CREATE_NO_WINDOW,
    )
    log.close()


def ensure_hub(client, auto_start=True):
    if client.health() is not None:
        return True
    if not auto_start:
        return False
    start_hub()
    for _ in range(40):
        time.sleep(0.5)
        if client.health() is not None:
            return True
    return False


# ---------------------------------------------------------------- 输入

class Editor:
    def __init__(self):
        self.buf = []
        self.cur = 0
        self.history = []
        self.hidx = None

    def text(self):
        return "".join(self.buf)

    def insert(self, text):
        for ch in text:
            if ch == "\r":
                continue
            self.buf.insert(self.cur, ch)
            self.cur += 1
        self.hidx = None

    def backspace(self):
        if self.cur > 0:
            del self.buf[self.cur - 1]
            self.cur -= 1
        self.hidx = None

    def delete(self):
        if self.cur < len(self.buf):
            del self.buf[self.cur]
        self.hidx = None

    def left(self):
        self.cur = max(0, self.cur - 1)

    def right(self):
        self.cur = min(len(self.buf), self.cur + 1)

    def home(self):
        self.cur = 0

    def end(self):
        self.cur = len(self.buf)

    def clear(self):
        self.buf, self.cur, self.hidx = [], 0, None

    def recall(self, delta, draft):
        if not self.history:
            return
        if self.hidx is None:
            self.hidx = len(self.history)
            self._draft = draft
        self.hidx = max(0, min(len(self.history), self.hidx + delta))
        if self.hidx == len(self.history):
            text = getattr(self, "_draft", "")
        else:
            text = self.history[self.hidx]
        self.buf, self.cur = list(text), len(text)

    def remember(self, text):
        text = text.strip()
        if text and (not self.history or self.history[-1] != text):
            self.history.append(text)
        self.hidx = None


def sgr_wheel(seq):
    """从 SGR 鼠标序列里取滚轮方向：CSI <b;x;yM，b=64/65。"""
    i = str(seq).find("<")
    if i < 0:
        return None
    try:
        code = int(str(seq)[i + 1:].split(";", 1)[0])
    except (TypeError, ValueError):
        return None
    if code & 64:
        low = code & 3
        if low == 0:
            return "wheel_up"
        if low == 1:
            return "wheel_down"
    return None


def x10_wheel(seq):
    """X10 鼠标：CSI M b x y（b、x、y 都是 +32 后的字节）。"""
    seq = str(seq)
    if not seq.startswith("[M") or len(seq) < 5:
        return None
    try:
        code = ord(seq[2]) - 32
    except TypeError:
        return None
    if code & 64:
        low = code & 3
        return "wheel_up" if low == 0 else ("wheel_down" if low == 1 else None)
    return None


def read_key_windows():
    import msvcrt

    ch = msvcrt.getwch()
    if ch in ("\x00", "\xe0"):
        code = msvcrt.getwch()
        return {
            "H": "up", "P": "down", "K": "left", "M": "right",
            "G": "home", "O": "end", "S": "delete",
            "s": "edit_left", "t": "edit_right",
        }.get(code, "none")
    if ch == "\r":
        return "enter"
    if ch in ("\x08", "\x7f"):
        # 0x08 = legacy Backspace；开 ENABLE_VIRTUAL_TERMINAL_INPUT 后
        # Windows Terminal 按 VT 约定把 Backspace 发成 0x7f (DEL)。
        return "backspace"
    if ch == "\x03":
        return "ctrl_c"
    if ch == "\x01":
        return "ctrl_a"
    if ch == "\x05":
        return "ctrl_e"
    if ch == "\x12":
        return "ctrl_r"
    if ch == "\x15":
        return "ctrl_u"
    if ch == "\x0c":
        return "ctrl_l"
    if ch == "\x1b":
        if not msvcrt.kbhit():
            return "esc"
        first = msvcrt.getwch()
        if first not in ("[", "O"):
            return "esc"
        seq = first
        while len(seq) < 32 and msvcrt.kbhit():
            c = msvcrt.getwch()
            seq += c
            if c in "ABCDHFMm~" or (first == "O" and len(seq) >= 2):
                break
        if seq.startswith("[M") and len(seq) < 5:
            while len(seq) < 5 and msvcrt.kbhit():
                seq += msvcrt.getwch()
        if seq.startswith("[<") or seq.startswith("[M"):
            return sgr_wheel(seq) or x10_wheel(seq) or "none"
        if seq.startswith("[200~"):
            return "paste_start"
        if seq.startswith("[201~"):
            return "paste_end"
        return {
            "[A": "up", "[B": "down", "[C": "right", "[D": "left",
            "[H": "home", "[F": "end", "[3~": "delete",
            "[1;5D": "edit_left", "[1;5C": "edit_right",
        }.get(seq, "esc")
    if ch == "\t":
        return "tab"
    if ch >= " ":
        return ("char", ch)
    return "none"


def read_keys_posix():
    import select
    import termios
    import tty

    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        data = os.read(fd, 64)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
    if not data:
        return ["ctrl_c"]
    text = data.decode("utf-8", "replace")
    keys = []
    i = 0
    while i < len(text):
        if text.startswith("\x1b[<", i):
            end = i + 3
            while end < len(text) and text[end] not in "Mm":
                end += 1
            if end < len(text):
                keys.append(sgr_wheel(text[i:end + 1]) or "none")
                i = end + 1
                continue
        if text.startswith("\x1b[M", i) and i + 6 <= len(text):
            keys.append(x10_wheel(text[i + 1:i + 6]) or "none")
            i += 6
            continue
        if text.startswith("\x1b[", i):
            seq = text[i + 2:i + 4]
            keys.append({"A": "up", "B": "down", "C": "right", "D": "left",
                         "H": "home", "F": "end", "3~": "delete"}.get(seq, "none"))
            i += 2 + len(seq) if seq.endswith("~") else 3
        elif text[i] == "\x03":
            keys.append("ctrl_c"); i += 1
        elif text[i] in ("\r", "\n"):
            keys.append("enter"); i += 1
        elif text[i] == "\x7f":
            keys.append("backspace"); i += 1
        elif text.startswith("\x1b[1;5D", i):
            keys.append("edit_left"); i += 6
        elif text.startswith("\x1b[1;5C", i):
            keys.append("edit_right"); i += 6
        elif text[i] == "\x01":
            keys.append("ctrl_a"); i += 1
        elif text[i] == "\x05":
            keys.append("ctrl_e"); i += 1
        elif text[i] == "\x12":
            keys.append("ctrl_r"); i += 1
        elif text[i] == "\x15":
            keys.append("ctrl_u"); i += 1
        elif text[i] == "\x0c":
            keys.append("ctrl_l"); i += 1
        else:
            keys.append(("char", text[i])); i += 1
    return keys


# ---------------------------------------------------------------- 渲染

def target_text(target, labels=None):
    target = str(target or "all")
    if target in ("all", ""):
        return "全体"
    labels = labels or {}
    return ",".join(labels.get(part.strip(), part.strip()) for part in target.split(",") if part.strip())


MD_CODE_STYLE = "\x1b[97;100m"


def _md_clean(text):
    text = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\t", "    ")
    # 模型输出里的 ESC/控制字符不能直接进终端（会破坏布局/注入样式）。
    return "".join(ch for ch in text if ch == "\n" or ord(ch) >= 32 and ch != "\x7f")


def _md_find(text, marker, start):
    i = start
    while True:
        i = text.find(marker, i)
        if i < 0:
            return -1
        bs = 0
        j = i - 1
        while j >= 0 and text[j] == "\\":
            bs += 1
            j -= 1
        if bs % 2 == 0:
            return i
        i += len(marker)


def md_inline(text, attrs=None, depth=0):
    """把一行 Markdown 拆成 (文本, 样式集合) 片段；不支持 HTML。"""
    attrs = set(attrs or ())
    if depth > 6:
        return [(text, attrs)]
    out, buf, i = [], [], 0

    def flush():
        if buf:
            out.append(("".join(buf), set(attrs)))
            buf.clear()

    while i < len(text):
        ch = text[i]
        if ch == "\\" and i + 1 < len(text):
            buf.append(text[i + 1])
            i += 2
            continue
        if ch == "`":
            n = 1
            while i + n < len(text) and text[i + n] == "`":
                n += 1
            end = _md_find(text, "`" * n, i + n)
            if end >= 0:
                flush()
                out.append((text[i + n:end], attrs | {"code"}))
                i = end + n
                continue
            buf.append("`" * n)
            i += n
            continue
        if text.startswith("**", i) or text.startswith("__", i):
            marker = text[i:i + 2]
            end = _md_find(text, marker, i + 2)
            if end > i + 2:
                flush()
                out.extend(md_inline(text[i + 2:end], attrs | {"bold"}, depth + 1))
                i = end + 2
                continue
            buf.append(marker)
            i += 2
            continue
        if text.startswith("~~", i):
            end = _md_find(text, "~~", i + 2)
            if end > i + 2:
                flush()
                out.extend(md_inline(text[i + 2:end], attrs | {"strike"}, depth + 1))
                i = end + 2
                continue
            buf.append("~~")
            i += 2
            continue
        if ch in "*_":
            prev = text[i - 1] if i else ""
            if ch == "_" and prev.isalnum():
                buf.append(ch)
                i += 1
                continue
            if i + 1 < len(text) and not text[i + 1].isspace():
                end = _md_find(text, ch, i + 1)
                if end > i + 1:
                    flush()
                    out.extend(md_inline(text[i + 1:end], attrs | {"italic"}, depth + 1))
                    i = end + 1
                    continue
            buf.append(ch)
            i += 1
            continue
        if ch == "!" and i + 1 < len(text) and text[i + 1] == "[":
            m = re.match(r"!\[([^\]]*)\]\(([^)\s]+)\)", text[i:])
            if m:
                flush()
                out.append(("[" + (m.group(1) or "图片") + "]", attrs | {"dim"}))
                out.append((" (" + m.group(2) + ")", attrs | {"dim"}))
                i += m.end()
                continue
        if ch == "[":
            m = re.match(r"\[([^\]]+)\]\(([^)\s]+)\)", text[i:])
            if m:
                flush()
                out.extend(md_inline(m.group(1), attrs | {"link"}, depth + 1))
                out.append((" (" + m.group(2) + ")", attrs | {"dim"}))
                i += m.end()
                continue
        buf.append(ch)
        i += 1
    flush()
    return out


def md_blocks(text):
    lines = _md_clean(text).split("\n")
    blocks, para, i = [], [], 0

    def flush_para():
        if para:
            blocks.append({"kind": "paragraph", "text": "\n".join(para)})
            para.clear()

    while i < len(lines):
        line = lines[i]
        if not line.strip():
            flush_para()
            blocks.append({"kind": "blank"})
            i += 1
            continue
        m = re.match(r"^\s{0,3}(`{3,}|~{3,})(.*)$", line)
        if m:
            flush_para()
            fence = m.group(1)
            info = m.group(2).strip()
            code = []
            close = re.compile(r"^\s{0,3}" + re.escape(fence[0]) +
                               "{" + str(len(fence)) + r",}\s*$")
            i += 1
            while i < len(lines):
                if close.match(lines[i]):
                    i += 1
                    break
                code.append(lines[i])
                i += 1
            blocks.append({"kind": "code", "lines": code,
                           "lang": info.split()[0] if info else ""})
            continue
        m = re.match(r"^\s{0,3}(#{1,6})\s+(.*)$", line)
        if m:
            flush_para()
            blocks.append({"kind": "heading", "text": m.group(2)})
            i += 1
            continue
        if re.match(r"^\s{0,3}([-*_])(?:\s*\1){2,}\s*$", line):
            flush_para()
            blocks.append({"kind": "hr"})
            i += 1
            continue
        m = re.match(r"^\s{0,3}>\s?(.*)$", line)
        if m:
            flush_para()
            blocks.append({"kind": "quote", "text": m.group(1)})
            i += 1
            continue
        m = re.match(r"^(\s*)([-+*])\s+(.*)$", line)
        if m:
            flush_para()
            level = len(m.group(1).replace("\t", "    ")) // 2
            blocks.append({"kind": "list", "marker": "• ", "text": m.group(3),
                           "level": level})
            i += 1
            continue
        m = re.match(r"^(\s*)(\d+)[.)]\s+(.*)$", line)
        if m:
            flush_para()
            level = len(m.group(1).replace("\t", "    ")) // 2
            blocks.append({"kind": "list", "marker": m.group(2) + ". ",
                           "text": m.group(3), "level": level})
            i += 1
            continue
        para.append(line)
        i += 1
    flush_para()
    return blocks


def _md_style(text, attrs, base):
    if not attrs:
        return text
    codes = []
    for key, code in (("bold", "1"), ("dim", "2"), ("italic", "3"),
                      ("underline", "4"), ("strike", "9"), ("code", "7"),
                      ("link", "4")):
        if key in attrs and code not in codes:
            codes.append(code)
    if not codes:
        return text
    # 先底色再属性：某些终端把 bold 映射成亮色，若先 1 再 36 可能被颜色覆盖。
    # 注意：Windows Terminal 默认 intenseTextStyle="bright"，会把 SGR 1 渲染成亮色而不是粗体；
    # 要真正粗体需在 profile 里设 intenseTextStyle="bold" 或 "all"。
    return base + "".join("\x1b[" + c + "m" for c in codes) + text + RESET + base


def _md_wrap(segments, width, prefix, cont_prefix, base):
    segments = [(str(t), set(a or ())) for t, a in segments if str(t)]
    if not segments:
        return [(prefix.rstrip(), base)]
    lines, cur, used, cur_prefix = [], [], 0, prefix

    def avail():
        return max(1, width - disp_width(cur_prefix))

    def flush():
        nonlocal cur, used, cur_prefix
        rendered = "".join(_md_style(t, a, base) for t, a in cur)
        lines.append((cur_prefix + rendered, base))
        cur, used = [], 0
        cur_prefix = cont_prefix

    for text, attrs in segments:
        piece = []
        for ch in text:
            if ch == "\n":
                if piece:
                    cur.append(("".join(piece), attrs))
                    piece = []
                flush()
                continue
            w = char_width(ch)
            if used + w > avail() and used > 0:
                if piece:
                    cur.append(("".join(piece), attrs))
                    piece = []
                flush()
            piece.append(ch)
            used += w
        if piece:
            cur.append(("".join(piece), attrs))
    if cur or not lines:
        flush()
    return lines


def _md_code_line(line, width, prefix):
    line = str(line or "").replace("\t", "    ")
    if line == "":
        return [(prefix.rstrip(), MD_CODE_STYLE)]
    out, chunk, used = [], [], 0
    limit = max(1, width - disp_width(prefix))
    for ch in line:
        w = char_width(ch)
        if used + w > limit and used > 0:
            out.append((prefix + "".join(chunk), MD_CODE_STYLE))
            chunk, used = [], 0
        chunk.append(ch)
        used += w
    out.append((prefix + "".join(chunk), MD_CODE_STYLE))
    return out


def render_markdown_lines(text, width, base_style, prefix="▌ "):
    """Markdown → [(含 ANSI 的文本, 行底色)]。"""
    clean = _md_clean(text)
    if not clean.strip():
        return [(prefix.rstrip(), base_style)]
    blocks = md_blocks(clean)
    lines = []
    for idx, block in enumerate(blocks):
        kind = block["kind"]
        if kind == "blank":
            if lines and lines[-1][0].strip() != prefix.strip():
                lines.append((prefix.rstrip(), base_style))
            continue
        if kind == "code":
            if lines and lines[-1][0].strip() != prefix.strip():
                lines.append((prefix.rstrip(), base_style))
            for raw in block["lines"] or [""]:
                lines.extend(_md_code_line(raw, width, prefix + "│ "))
            if idx + 1 < len(blocks):
                lines.append((prefix.rstrip(), base_style))
            continue
        if kind == "heading":
            lines.extend(_md_wrap(md_inline(block["text"], {"bold", "underline"}),
                                  width, prefix, prefix, base_style))
        elif kind == "quote":
            lines.extend(_md_wrap(md_inline(block["text"], {"italic"}),
                                  width, prefix + "│ ", prefix + "│ ", base_style))
        elif kind == "list":
            indent = "  " * max(0, int(block.get("level") or 0))
            marker = block["marker"]
            start = prefix + indent + marker
            cont = prefix + indent + " " * disp_width(marker)
            lines.extend(_md_wrap(md_inline(block["text"]), width, start, cont, base_style))
        elif kind == "hr":
            lines.append((prefix + "─" * max(1, width - disp_width(prefix)), base_style))
        else:
            lines.extend(_md_wrap(md_inline(block["text"]), width, prefix, prefix, base_style))
    return lines or [(prefix.rstrip(), base_style)]


def build_message_lines(msg, width, labels, colors):
    kind = msg.get("kind") or "event"
    ts = time.strftime("%H:%M", time.localtime(msg.get("ts") or 0))
    sender = msg.get("sender") or "?"
    label = labels.get(sender, sender)
    hl = bool(msg.get("highlight"))
    body = msg.get("text") or ""
    if kind == "user" and msg.get("display_text"):
        body = msg.get("display_text") or ""
    atts = msg.get("attachments") or []
    if atts:
        body += ("\n" if body else "") + "[附件] " + "、".join(a.get("name", "?") for a in atts[:4])
        if len(atts) > 4:
            body += " 等 %d 个" % len(atts)

    col = max(1, min(width - 2, 110))
    if kind == "event":
        style = C_HL if hl else C_SYS
        wrapped = wrap_text("· " + ts + "  " + body, col)
        out = []
        for i, part in enumerate(wrapped):
            out.append(("  " + (part if i == 0 else "  " + part), style))
        return out

    if kind == "user":
        head = ts + "  你 → " + target_text(msg.get("target"), labels)
        style = C_USER
    elif kind == "reply":
        tgt = "私聊" if str(msg.get("target")) == str(sender) else target_text(msg.get("target"), labels)
        head = ts + "  " + label + " → " + tgt
        style = colors.get(sender, C_SYS)
    elif kind == "error":
        head = ts + ("  ⚠ 错误" if sender == "system" else "  ⚠ " + label + " 出错")
        style = C_ERR
    elif kind == "claudelink":
        head = ts + "  旁听 · " + label + " → " + target_text(msg.get("target"), labels)
        style = C_MIRROR
    else:
        head = ts + "  " + label
        style = C_SYS

    if hl:
        style = C_HL
        head = "★ " + head
    # 样式不再带 SGR 1（粗体只留给 Markdown 行内加粗），头/身同色。
    head_style = style
    body_style = style

    out = []
    for part in wrap_text(head, col):
        out.append(("  " + part, head_style))
    # Markdown 行自己处理换行/前缀；col+2 = 前缀 "▌ " + 正文列宽。
    out.extend(render_markdown_lines(body, col + 2, body_style))
    return out

def build_stream_lines(status, agents, width, show_think):
    lines = []
    shown = 0
    pending = 0
    for name, st in status.items():
        if st.get("state") != "running" or name not in agents:
            continue
        if shown >= 3:
            pending += 1
            continue
        shown += 1
        agent = agents[name]
        label = st.get("label") or agent.get("label") or name
        color = AGENT_COLORS[(agent.get("index") or 0) % len(AGENT_COLORS)]
        since = st.get("since") or agent.get("running_since") or time.time()
        live_full = st.get("live") or ""
        think_full = st.get("think") or ""
        live = live_full[-4000:]
        think = think_full[-2000:]

        head = "● " + label + "  " + fmt_secs(time.time() - since)
        metrics = []
        if think_full:
            metrics.append("思考 " + fmt_chars(len(think_full)) + " 字")
        if live_full:
            metrics.append("正文 " + fmt_chars(len(live_full)) + " 字")
        if metrics:
            head += "   " + " · ".join(metrics)
        lines.append(("  " + clip_disp(head, width - 2), color))

        if not think_full and not live_full:
            lines.append(("▌ 等待输出…", C_SYS))
            continue
        col = max(1, min(width - 2, 110))
        if show_think and think:
            for part in wrap_text(think, max(1, col - 2))[-3:]:
                lines.append(("▌ ⋯ " + clip_disp(part, col - 2), C_SYS))
        for text, style in render_markdown_lines(live, col + 2, color)[-7:]:
            lines.append((clip_disp(text, col + 2), style))
    if pending:
        lines.append(("  … 还有 " + str(pending) + " 个成员在跑", C_SYS))
    return lines

def pad_cells(text, width):
    return text + " " * max(0, width - disp_width(text))


def render_header(app, width):
    conv = "#" + str(app.conv_id) if app.conv_id else "#?"
    brand = " ClaudeLink "
    meta = conv + "  " + app.client.host + ":" + str(app.client.port)
    conn = "● 已连接" if app.connected else "○ 重连中"
    conn_code = C_OK if app.connected else C_WARN
    health = app.health or {}
    err = int(health.get("errors") or 0)
    rate = health.get("cache_hit_rate")

    count = str(len(app.order)) + ("+" if len(app.order) >= 1000 else "")
    parts = [(str(len(app.agent_order)) + " 成员", C_SYS),
             (count + " 条", C_SYS)]
    rt = getattr(app, "rt", None)
    if rt and rt.get("state") in ("running", "pausing", "stopping", "paused", "stopped"):
        if rt.get("state") in ("paused", "stopped"):
            tag = "圆桌 暂停"
        else:
            tag = "圆桌 " + str(rt.get("round", 0)) + "/" + str(rt.get("rounds", 0))
            if rt.get("current_role"):
                tag += " · " + str(rt["current_role"])
        parts.insert(0, (tag, C_HL))
    if err:
        parts.append((str(err) + " 错", C_ERR))
    if getattr(app, "perms_count", 0):
        parts.append((str(app.perms_count) + " 待批", C_WARN))
    if rate is not None:
        parts.append(("命中 " + str(int(round(rate * 100))) + "%", C_SYS))

    stats_txt, stats_w = "", 0
    for i, (t, c) in enumerate(parts):
        if i:
            stats_txt += C_SYS + " · " + RESET
            stats_w += 3
        stats_txt += c + t + RESET
        stats_w += disp_width(t)

    for with_meta, with_stats in ((True, True), (True, False), (False, False)):
        left = brand + (meta if with_meta else "")
        right_w = disp_width(conn) + ((2 + stats_w) if with_stats else 0)
        gap = width - disp_width(left) - right_w
        if gap < 1:
            continue
        out = "\x1b[1;36m" + brand + RESET
        if with_meta:
            out += C_SYS + meta + RESET
        out += " " * gap
        out += conn_code + conn + RESET
        if with_stats:
            out += "  " + stats_txt
        return out
    return clip_disp(brand + conn, width)


def render_agents(app, width):
    out, used = "", 0
    for idx, name in enumerate(app.agent_order):
        agent = app.agents[name]
        st = app.status.get(name) or {}
        state = st.get("state") or ("running" if agent.get("running_since") else "idle")
        label = agent.get("label", name)
        num = str(idx + 1)
        if not agent.get("enabled", True):
            txt, code = " " + num + " " + label + " 停用", C_OFF
        elif state == "running":
            since = st.get("since") or agent.get("running_since") or time.time()
            txt, code = " " + num + " " + label + " ●" + fmt_secs(time.time() - since), C_OK
        else:
            txt, code = " " + num + " " + label + " ○", C_SYS
        if name in app.targets:
            txt = "▸" + txt[1:]
            code = "\x1b[7m" + code
        add = disp_width(txt) + (2 if used else 0)
        if used + add > width - 1:
            out += C_SYS + " …" + RESET
            break
        if used:
            out += "  "
        out += code + txt + RESET
        used += add
    return out


def render_frame(app, cols, rows):
    width = max(20, cols)
    lines = []
    show_help = rows >= 12
    show_bar = rows >= 9

    lines.append((render_header(app, width), ""))
    if show_bar:
        lines.append((render_agents(app, width), ""))
    sep = "─" * width
    lines.append((sep, C_SYS))

    fixed = (3 if show_bar else 2) + 2 + (1 if show_help else 0)
    body_h = max(3, rows - fixed)

    if app.rt_wizard is not None and app.rt_wizard.get("stage") == "setup":
        body = app.rt_setup_lines(width)
    else:
        body = app.flat_lines(width)
    if app.scroll > 0 and app._last_msgs and len(body) > app._last_msgs:
        app.scroll += len(body) - app._last_msgs
    app._last_msgs = len(body)
    stream = build_stream_lines(app.status, app.agents, width, app.show_think)
    if stream:
        body = body + [("", "")] + stream
    if not body:
        body = [("  " + part, C_SYS) for part in
                wrap_text("还没有消息 · Enter 发送 · /round 开多 CLI 角色圆桌", max(1, width - 2))]

    total = len(body)
    max_scroll = max(0, total - body_h)
    app.scroll = min(app.scroll, max_scroll)
    top_align = bool(app.rt_wizard is not None and app.rt_wizard.get("stage") == "setup")
    start = min(max(0, app.scroll), max_scroll) if top_align else max(0, total - body_h - app.scroll)
    view = body[start:start + body_h]
    if len(body) < body_h:
        pad = [("", "")] * (body_h - len(view))
        view = (view + pad) if top_align else (pad + view)
    while len(view) < body_h:
        view.append(("", ""))
    lines.extend(view)

    lines.append((sep, C_SYS))

    target = "全体" if not app.targets else ",".join(
        app.agents[n].get("label", n) for n in app.targets if n in app.agents)
    if app.rt_wizard is not None:
        prefix = app.rt_wizard_prompt()
    elif app.search is not None:
        q = app.search["q"]
        prefix = (f"(搜历史 {q}) ▸ " if q else "(搜历史) ▸ ")
    else:
        prefix = f" 你 ▸ [{target}] "
    raw = app.editor.text()
    text = raw.replace("\n", "↵")
    avail = max(4, width - disp_width(prefix) - 1)
    before = raw[: app.editor.cur].replace("\n", "↵")
    cursor_w = disp_width(before)
    if cursor_w > avail:
        before = "…" + before[-max(1, avail // 2):]
        text_view = before + text[app.editor.cur:]
        cursor_w = disp_width(before)
    else:
        text_view = text
    miss = " （无匹配）" if (app.search is not None and app.search.get("miss")) else ""
    input_line = prefix + text_view + miss
    cursor_col = 1 + disp_width(prefix) + cursor_w
    lines.append((pad_cells(clip_disp(input_line, width), width), C_INPUT))

    if show_help:
        if app.rt_wizard is not None and app.rt_wizard.get("stage") == "setup":
            hint = "↑/↓ 选择 · e 角色 · p 提示词 · a 添加实例 · d 删除 · u/j 移动 · r 轮数 · t 议题 · n 会话 · Enter 开始 · Esc 取消"
        elif app.rt_wizard is not None:
            hint = "圆桌设置：Enter 下一步 · Esc 取消 · 系统提示词可粘贴多行"
        elif app.search is not None:
            hint = "Ctrl+R 上一个匹配 · Enter 采用 · Esc 取消"
        elif app.rt and app.rt.get("state") in ("running", "pausing", "stopping"):
            hint = "圆桌进行中 · /round stop 暂停（圆桌保留） · /round status 查看 · Ctrl+C 退出"
        elif app.rt and app.rt.get("state") in ("paused", "stopped"):
            hint = "圆桌已暂停 · /round resume 继续 · /round status 查看 · /round again 载入上次配置"
        elif app.scroll > 0:
            hint = f"↑ 已上翻 {app.scroll} 行，滚轮向下回底 · Ctrl+C 退出"
        else:
            if width >= 104:
                hint = "Enter 发送 · ←/→ 选目标 · ↑ 历史 · Ctrl+R 搜索 · 滚轮上下查看 · /help"
            elif width >= 78:
                hint = "Enter 发送 · ←/→ 选目标 · ↑ 历史 · Ctrl+R 搜索 · /help"
            elif width >= 52:
                hint = "Enter 发送 · ←/→ 选目标 · ↑ 历史 · /help"
            else:
                hint = "Enter 发送 · ←/→ 选目标"
        lines.append((clip_disp(hint, width), C_SYS))

    while len(lines) < rows:
        lines.append(("", ""))

    out = ["\x1b[?25l"]
    for i, (text_line, code) in enumerate(lines[:rows]):
        out.append(f"\x1b[{i + 1};1H")
        out.append((code + text_line + RESET) if code else text_line)
        out.append("\x1b[K")
    cursor_row = rows - (1 if show_help else 0)
    out.append(f"\x1b[{cursor_row};{max(1, min(cursor_col, width))}H\x1b[?25h")
    return "".join(out)


class App:
    def __init__(self, client):
        self.client = client
        self.q = queue.Queue()
        self.messages = {}
        self.order = []
        self.agents = {}
        self.agent_order = []
        self.status = {}
        self.labels = {}
        self.colors = {}
        self.relay = False
        self.health = {}
        self.perms_count = 0
        self.conv_id = None
        self.connected = False
        self.dirty = True
        self.scroll = 0
        self.show_think = True
        self.targets = []
        self.editor = Editor()
        self.notices = []
        self._flat = None
        self._flat_width = 0
        self._cache = {}
        self._stopping = False
        self._last_msgs = 0
        self.pasting = False
        self.search = None
        self.rt = None              # 中枢返回的圆桌状态（只读快照）
        self.rt_wizard = None       # 终端本地的设置向导状态
        self._rt_seen = None        # 已提示过的 (state, round, turn)，防重复刷屏
        self._reload_pending = False
        self._had_disconnect = False
        self._lock = threading.Lock()

    # -- 状态 ---------------------------------------------------------
    def reload_state(self):
        state = self.client.get("/api/state", timeout=10)
        with self._lock:
            self.status = {}
            self.messages.clear()
            self.order = []
            for msg in state.get("messages") or []:
                self.messages[msg["id"]] = msg
                self.order.append(msg["id"])
            self.agents.clear()
            self.agent_order = []
            for idx, agent in enumerate(state.get("agents") or []):
                agent = dict(agent)
                agent["index"] = idx
                self.agents[agent["name"]] = agent
                self.agent_order.append(agent["name"])
            self.labels = {n: a.get("label", n) for n, a in self.agents.items()}
            self.labels.setdefault("system", "系统")
            self.colors = {n: AGENT_COLORS[i % len(AGENT_COLORS)]
                           for i, n in enumerate(self.agent_order)}
            self.relay = bool(state.get("relay"))
            self.targets = [n for n in self.targets
                            if n in self.agents and self.agents[n].get("enabled", True)]
            self.health = state.get("health") or {}
            self.perms_count = len(state.get("perms") or [])
            self.conv_id = state.get("conversation")
            self.rt = state.get("roundtable")
            if self.rt and self.rt.get("state") == "running":
                self._rt_seen = (self.rt.get("state"), self.rt.get("round"), self.rt.get("turn"))
            for name, agent in self.agents.items():
                if agent.get("running_since"):
                    self.status[name] = {
                        "state": "running", "since": agent["running_since"],
                        "live": agent.get("live") or "", "think": "",
                    }
            self._invalidate()
        self.dirty = True

    def _invalidate(self):
        self._flat = None
        self._cache.clear()

    def flat_lines(self, width):
        with self._lock:
            if self._flat is not None and self._flat_width == width:
                return self._flat
            if self._flat_width != width:
                self._cache.clear()
            out = []
            for mid in self.order:
                msg = self.messages.get(mid)
                if not msg:
                    continue
                key = (msg.get("text"), msg.get("display_text"), msg.get("highlight"),
                       len(msg.get("attachments") or []))
                if out and out[-1][0] != "":
                    out.append(("", ""))
                cached = self._cache.get(mid)
                if cached and cached[0] == key:
                    out.extend(cached[1])
                else:
                    block = build_message_lines(msg, width, self.labels, self.colors)
                    self._cache[mid] = (key, block)
                    out.extend(block)
            if self.notices and out and out[-1][0] != "":
                out.append(("", ""))
            for notice in self.notices[-100:]:
                out.extend(build_message_lines(notice, width, self.labels, self.colors))
            self._flat, self._flat_width = out, width
            return out

    # -- 事件 ---------------------------------------------------------
    def enqueue(self, event):
        self.q.put(event)

    def drain(self):
        while True:
            try:
                event = self.q.get_nowait()
            except queue.Empty:
                return
            self.apply_event(event)

    def apply_event(self, event):
        etype = event.get("type")
        if etype == "__connected__":
            self.connected = True
            if self._had_disconnect:
                self._had_disconnect = False
                self._reload_pending = True
            self.dirty = True
            return
        if etype == "__disconnected__":
            self.connected = False
            self._had_disconnect = True
            self.dirty = True
            return
        if etype == "message":
            msg = event.get("message") or {}
            mid = msg.get("id")
            if mid is None:
                return
            with self._lock:
                if mid not in self.messages:
                    self.order.append(mid)
                self.messages[mid] = msg
                self._invalidate()
            self.dirty = True
            return
        if etype == "message_updated":
            msg = event.get("message") or {}
            mid = msg.get("id")
            if mid in self.messages:
                with self._lock:
                    self.messages[mid] = msg
                    self._invalidate()
                self.dirty = True
            return
        if etype == "message_deleted":
            mid = event.get("id")
            with self._lock:
                self.messages.pop(mid, None)
                if mid in self.order:
                    self.order.remove(mid)
                self._invalidate()
            self.dirty = True
            return
        if etype == "message_highlighted":
            msg = event.get("message") or {}
            mid = msg.get("id")
            if mid in self.messages:
                with self._lock:
                    self.messages[mid] = msg
                    self._invalidate()
                self.dirty = True
            return
        if etype == "status":
            name = event.get("agent")
            if not name:
                return
            with self._lock:
                cur = self.status.setdefault(name, {})
                cur["state"] = event.get("state")
                if event.get("label"):
                    cur["label"] = event["label"]
                if event.get("live") is not None:
                    cur["live"] = event.get("live")
                if event.get("think") is not None:
                    cur["think"] = event.get("think")
                cur["since"] = event.get("since") or cur.get("since")
                if event.get("state") != "running":
                    cur["since"] = None
            self.dirty = True
            return
        if etype == "relay":
            self.relay = bool(event.get("relay"))
            self.dirty = True
            return
        if etype == "roundtable":
            rt = event.get("roundtable")
            with self._lock:
                self.rt = rt
            self._rt_event(rt)
            self.dirty = True
            return
        if etype == "agents":
            self._reload_pending = True
            return
        if etype == "cleared":
            with self._lock:
                self.messages.clear()
                self.order = []
                self._invalidate()
            self.scroll = 0
            self.dirty = True
            return
        if etype in ("conversation", "trace_reset"):
            if etype == "conversation":
                self.scroll = 0
            self._reload_pending = True
            return
        if etype == "dispatch":
            self.dirty = True
            return
        if etype == "perms":
            self._reload_pending = True
            return

    # -- 操作 ---------------------------------------------------------
    def notice(self, text, kind="event"):
        with self._lock:
            self.notices.append({"id": -(len(self.notices) + 1), "ts": time.time(),
                                 "sender": "system", "target": "all", "kind": kind,
                                 "text": text})
            del self.notices[:-100]
            self._invalidate()
        self.dirty = True

    # -- 圆桌模式（终端专用，独立于 relay/dispatch）-------------------
    def _rt_event(self, rt):
        if not rt:
            return
        state = rt.get("state")
        key = (state, rt.get("round"), rt.get("turn"), rt.get("current"))
        if key == self._rt_seen:
            return
        self._rt_seen = key
        rounds = rt.get("rounds") or 0
        total = rt.get("total") or 0
        labels = rt.get("labels") or {}
        roles = rt.get("roles") or {}
        if state == "running":
            current = rt.get("current")
            if current:
                label = labels.get(current, current)
                role = roles.get(current) or rt.get("current_role") or label
                suffix = "" if role and role in label else ("（" + role + "）" if role else "")
                self.notice("圆桌 " + str(rt.get("round", 0)) + "/" + str(rounds) +
                            " · " + str(rt.get("turn", 0)) + "/" + str(total) +
                            " · " + label + suffix)
            elif rt.get("round") == 0:
                self.notice("圆桌开始：" + str(rt.get("topic") or "自由讨论") + "；" +
                            str(rounds) + " 轮 × " + str(len(rt.get("order") or [])) + " 位")
        elif state in ("pausing", "stopping"):
            self.notice("圆桌正在暂停…")
        elif state in ("paused", "stopped"):
            self.notice("圆桌已暂停（第 " + str(rt.get("round", 0)) + "/" + str(rounds) +
                        " 轮 · 第 " + str(rt.get("turn", 0)) + "/" + str(total) +
                        " 位）；/round resume 继续")
        elif state == "done":
            self.notice("圆桌本轮完成：" + str(rt.get("topic") or "") +
                        "；/round resume 可再加一轮")
        elif state == "error":
            tail = rt.get("error") or ""
            self.notice("圆桌出错：" + str(rt.get("topic") or "") +
                        ("（" + tail + "）" if tail else "") + "；/round resume 可重试",
                        "error")

    def rt_begin_wizard(self):
        if self.rt and self.rt.get("state") in ("running", "pausing", "stopping"):
            self.notice("圆桌正在进行中；先 /round stop 暂停")
            return
        if self.rt and self.rt.get("state") in ("paused", "stopped"):
            self.notice("已有暂停的圆桌；/round resume 继续，继续新建会另开一桌")
        elif self.rt and self.rt.get("state") == "done":
            self.notice("上一桌已完成；/round resume 可再加一轮，继续新建会另开一桌")
        self.rt_wizard = {
            "stage": "confirm", "participants": [], "idx": 0, "sel": 0,
            "rounds": 2, "topic": "", "new_conversation": True,
        }
        self.editor.clear()
        self.scroll = 0
        self.dirty = True

    def rt_cancel(self):
        self.rt_wizard = None
        self.editor.clear()
        self.notice("已取消圆桌设置")
        self.dirty = True

    def rt_wizard_prompt(self):
        w = self.rt_wizard
        if not w:
            return " 你 ▸ "
        stage = w.get("stage")
        if stage == "confirm":
            return " 圆桌 ▸ 开启多个 CLI 角色多轮会话？(y/l/N) "
        if stage == "members":
            return " 圆桌 ▸ 参与顺序（编号/名字，逗号分隔） "
        if stage == "role":
            p = w["participants"][w["idx"]]
            return " 圆桌 ▸ " + p["label"] + " 的角色名 "
        if stage == "prompt":
            p = w["participants"][w["idx"]]
            return " 圆桌 ▸ " + p["role"] + " 的系统提示词（Enter 保存 · Esc 返回）"
        if stage == "rounds":
            return " 圆桌 ▸ 轮数（1-20，默认 2） "
        if stage == "topic":
            return " 圆桌 ▸ 议题/目标（空 = 自由讨论） "
        if stage == "setup":
            return " 圆桌设置 ▸ 命令 "
        if stage == "add_member":
            return " 圆桌设置 ▸ 添加成员（编号/名字） "
        return " 圆桌 ▸ "

    def _rt_add_participant(self, w, agent, role=None, system_prompt="", slot=None):
        """把一个实例追加到向导；同一个 agent 第二次出现会自动拿 agent#2 的身份。"""
        slot = str(slot or "").strip()
        used = {p["slot"] for p in w["participants"]}
        if slot and slot in used:
            slot = None
        if not slot:
            n = 0
            while True:
                candidate = agent if n == 0 else agent + "#" + str(n + 1)
                if candidate not in used:
                    slot = candidate
                    break
                n += 1
        label = self.labels.get(agent, agent)
        default_role = label
        w["participants"].append({
            "agent": agent, "slot": slot, "role": str(role or default_role).strip()[:40] or default_role,
            "system_prompt": str(system_prompt or ""), "label": label,
        })
        return w["participants"][-1]

    def rt_input(self, text):
        w = self.rt_wizard
        if not w:
            return
        text = (text or "").strip()
        stage = w.get("stage")
        if stage == "confirm":
            low = text.lower()
            if low in ("y", "yes"):
                self._rt_members_stage()
            elif low in ("l", "last", "r", "reuse"):
                self.rt_start_last()
            else:
                self.rt_cancel()
            return
        if stage == "members":
            names = [n for n in self.agent_order if self.agents[n].get("enabled", True)]
            if not names:
                self.notice("没有启用中的成员", "error")
                self.rt_cancel()
                return
            if not text:
                picked = names
            else:
                by_key = {}
                for n in names:
                    by_key[n.lower()] = n
                    by_key[self.labels.get(n, n).lower()] = n
                picked = []
                for token in re.split(r"[,，、\s]+", text):
                    token = token.strip()
                    if not token:
                        continue
                    name = None
                    if token.isdigit() and 1 <= int(token) <= len(names):
                        name = names[int(token) - 1]
                    else:
                        name = by_key.get(token.lower())
                    if not name:
                        self.notice("未知成员：" + token + "；用编号或名字", "error")
                        return
                    # 同一个 Agent 可以重复：每个出现都是一个独立实例（独立 CLI 会话）。
                    picked.append(name)
            if len(picked) < 2:
                self.notice("至少选 2 个成员", "error")
                return
            w["participants"] = []
            for name in picked:
                self._rt_add_participant(w, name)
            w["idx"] = 0
            w["sel"] = 0
            w["stage"] = "setup"
            self.notice("已创建 " + str(len(w["participants"])) +
                        " 个实例；e 改角色、p 改系统提示词，Enter 开始")
            return
        if stage == "role":
            p = w["participants"][w["idx"]]
            label = p["label"]
            role = text[:40] or label
            p["role"] = role
            if w.pop("chain_prompt", False):
                w["stage"] = "prompt"
                self._rt_prefill(p["system_prompt"])
                self.notice(label + " 的角色：" + role + "；接着编辑系统提示词")
            elif w.get("return_stage"):
                w["stage"] = w.pop("return_stage")
                self.notice(label + " 的角色已改为：" + role)
            else:
                w["stage"] = "prompt"
                self.notice(label + " 的角色：" + role + "；下一步设置系统提示词")
            return
        if stage == "prompt":
            p = w["participants"][w["idx"]]
            role = p["role"]
            if not text:
                text = "你是" + role + "。只代表本角色发言，观点明确，给出理由和可执行建议。"
            p["system_prompt"] = text
            if w.get("return_stage"):
                w["stage"] = w.pop("return_stage")
                self.notice(p["label"] + " 的系统提示词已保存")
                return
            w["idx"] += 1
            if w["idx"] < len(w["participants"]):
                w["stage"] = "role"
                nxt = w["participants"][w["idx"]]
                self.notice("下一位：" + nxt["label"] + " 的角色名")
            else:
                w["stage"] = "rounds"
                self.notice("角色设置完成；下一步设置轮数")
            return
        if stage == "rounds":
            try:
                rounds = int(text) if text else 2
            except ValueError:
                self.notice("轮数必须是整数", "error")
                return
            if not 1 <= rounds <= 20:
                self.notice("轮数必须在 1-20 之间", "error")
                return
            w["rounds"] = rounds
            if w.get("return_stage"):
                w["stage"] = w.pop("return_stage")
                self.notice("轮数已改为 " + str(rounds))
            else:
                w["stage"] = "conversation"
                self.notice("轮数：" + str(rounds) + "；新开专用会话可避免混入现有群聊")
            return
        if stage == "topic":
            w["topic"] = text or "自由讨论"
            w.pop("return_stage", None)
            w["stage"] = "setup"
            self.notice("设置已就绪；Enter 开始，或继续修改")
            return
        if stage == "add_member":
            if not text:
                w["stage"] = "setup"
                return
            names = [n for n in self.agent_order if self.agents[n].get("enabled", True)]
            by_key = {}
            for n in names:
                by_key[n.lower()] = n
                by_key[self.labels.get(n, n).lower()] = n
            name = None
            if text.isdigit() and 1 <= int(text) <= len(names):
                name = names[int(text) - 1]
            else:
                name = by_key.get(text.lower())
            if not name:
                self.notice("未知成员：" + text + "；用编号或名字", "error")
                return
            p = self._rt_add_participant(w, name)
            w["sel"] = len(w["participants"]) - 1
            w["stage"] = "setup"
            self.notice("已添加 " + p["label"] + "（" + p["slot"] + "）；按 e 改角色、p 改提示词")
            return
        if stage == "setup":
            if not text:
                self.rt_start()
            else:
                self.rt_setup_key(text[:1].lower())
            return

    def _rt_prefill(self, text):
        self.editor.buf = list(str(text or ""))
        self.editor.cur = len(self.editor.buf)
        self.editor.hidx = None

    def rt_setup_key(self, key):
        """设置页的单键命令（不经过输入框）。"""
        w = self.rt_wizard
        if not w or w.get("stage") != "setup":
            return
        parts = w["participants"]
        if key == "up":
            w["sel"] = max(0, w["sel"] - 1)
        elif key == "down":
            w["sel"] = min(max(0, len(parts) - 1), w["sel"] + 1)
        elif key in ("e", "p"):
            if not parts:
                return
            p = parts[w["sel"]]
            w["idx"] = w["sel"]
            w["return_stage"] = "setup"
            if key == "e":
                w["chain_prompt"] = True
                w["stage"] = "role"
                self._rt_prefill(p["role"])
                self.notice("编辑 " + p["label"] + " 的角色（Enter 保存 · Esc 返回）")
            else:
                w["stage"] = "prompt"
                self._rt_prefill(p["system_prompt"])
                self.notice("编辑 " + p["label"] + "·" + p["role"] + " 的系统提示词（Enter 保存 · Esc 返回）")
        elif key == "a":
            w["stage"] = "add_member"
            self.editor.clear()
            self.notice("输入要添加的成员编号/名字（可重复；空 = 取消）")
        elif key == "d":
            if len(parts) <= 2:
                self.notice("至少保留 2 个实例", "error")
            else:
                gone = parts.pop(w["sel"])
                w["sel"] = min(w["sel"], len(parts) - 1)
                self.notice("已删除 " + gone["label"] + "（" + gone["slot"] + "）")
        elif key in ("u", "j"):
            i = w["sel"]
            j = i - 1 if key == "u" else i + 1
            if 0 <= j < len(parts):
                parts[i], parts[j] = parts[j], parts[i]
                w["sel"] = j
        elif key == "r":
            w["return_stage"] = "setup"
            w["stage"] = "rounds"
            self._rt_prefill(str(w["rounds"]))
            self.notice("编辑轮数（Enter 保存 · Esc 返回）")
        elif key == "t":
            w["return_stage"] = "setup"
            w["stage"] = "topic"
            self._rt_prefill(w["topic"] or "自由讨论")
            self.notice("编辑议题（Enter 保存 · Esc 返回）")
        elif key == "n":
            w["new_conversation"] = not w["new_conversation"]
            self.notice("新开专用会话：" + ("是" if w["new_conversation"] else "否"))
        elif key in ("s", "enter"):
            self.rt_start()
            return
        elif key == "esc":
            self.rt_cancel()
            return
        self.dirty = True

    def rt_setup_lines(self, width):
        w = self.rt_wizard
        if not w:
            return []
        lines = [(clip_disp(" 圆桌设置 · 顺序 / 成员 / 角色 / 系统提示词", width - 2), C_TITLE),
                 ("─" * max(10, width - 2), C_SYS)]
        for i, p in enumerate(w["participants"]):
            prompt = (p.get("system_prompt") or "").replace("\n", " ")
            summary = "（默认）" if not prompt else prompt
            name = p["label"] if p["slot"] == p["agent"] else p["label"] + " #" + p["slot"].split("#")[-1]
            row = (str(i + 1).rjust(2) + "  " + pad_cells(name[:18], 18) + "  " +
                   pad_cells(p["role"][:14], 14) + "  " + summary)
            mark = "▸ " if i == w.get("sel", 0) else "  "
            lines.append((clip_disp(mark + row, max(10, width - 2)),
                          C_HL if i == w.get("sel", 0) else C_SYS))
        lines.append(("", ""))
        lines.append((clip_disp(" 轮数 " + str(w["rounds"]) + " · 议题 " + (w["topic"] or "自由讨论") +
                                " · " + ("新开会话" if w["new_conversation"] else "沿用当前会话"),
                                max(10, width - 2)), C_SYS))
        for part in wrap_text("↑/↓ 选择 · e 角色 · p 系统提示词 · a 添加实例 · d 删除 · u/j 移动 · "
                              "r 轮数 · t 议题 · n 会话 · Enter 开始 · Esc 取消",
                              max(10, width - 2)):
            lines.append((" " + part, C_SYS))
        with self._lock:
            recent = list(self.notices[-1:])
        for n in recent:
            lines.append((clip_disp(" · " + str(n.get("text") or ""), max(10, width - 2)),
                          C_ERR if n.get("kind") == "error" else C_SYS))
        return lines

    def rt_status_text(self):
        rt = self.rt
        if not rt:
            return "圆桌：未开始（/round 开始）"
        state = {"running": "进行中", "pausing": "暂停中", "stopping": "暂停中",
                 "paused": "已暂停", "stopped": "已暂停", "done": "本轮完成",
                 "error": "出错"}.get(rt.get("state"), str(rt.get("state")))
        labels = rt.get("labels") or {}
        roles = rt.get("roles") or {}
        bits = []
        for n in rt.get("order") or []:
            label = labels.get(n, n)
            role = roles.get(n) or ""
            bits.append(label if role and role in label else label + ("（" + role + "）" if role else ""))
        return ("圆桌[" + state + "]：" + str(rt.get("topic") or "") + "；" +
                str(rt.get("round", 0)) + "/" + str(rt.get("rounds", 0)) + " 轮 · " +
                str(rt.get("turn", 0)) + "/" + str(rt.get("total", 0)) + " 位；已完成 " +
                str(rt.get("completed", 0)) + "/" + str(rt.get("total", 0)) + "；" +
                " → ".join(bits))

    def _rt_members_stage(self):
        names = [n for n in self.agent_order if self.agents[n].get("enabled", True)]
        if not names:
            self.notice("没有启用中的成员", "error")
            self.rt_cancel()
            return
        w = self.rt_wizard
        w["stage"] = "members"
        self.notice("可选成员（按当前顺序）：" + "  ".join(
            str(i + 1) + "." + self.labels.get(n, n) for i, n in enumerate(names)))
        self.notice("输入编号/名字，逗号分隔；留空 = 全部，按输入顺序发言")
        self.dirty = True

    def rt_start(self):
        w = self.rt_wizard
        if not w:
            return
        if len(w["participants"]) < 2:
            self.notice("至少需要 2 个实例", "error")
            return
        payload = {
            "topic": w["topic"] or "自由讨论",
            "rounds": w["rounds"],
            "new_conversation": bool(w["new_conversation"]),
            "max_chars": 600,
            "participants": [
                {"agent": p["agent"], "slot": p["slot"], "role": p["role"],
                 "system_prompt": p["system_prompt"]}
                for p in w["participants"]
            ],
        }
        self._rt_post_start(payload)

    def _rt_post_start(self, payload):
        if self.rt and self.rt.get("state") in ("running", "stopping", "pausing"):
            self.notice("圆桌正在进行中；先 /round stop 暂停")
            return
        try:
            result = self.client.post("/api/roundtable/start", payload, timeout=20)
            if not result.get("ok"):
                raise RuntimeError(str(result.get("error") or "未知错误"))
            self.rt = result.get("roundtable")
            self.rt_wizard = None
            self.editor.clear()
            self._rt_seen = None
            self._rt_save(payload)
            self.notice("圆桌已启动；/round stop 暂停，/round status 查看")
            if payload.get("new_conversation"):
                self._reload_pending = True
        except Exception as exc:
            # 失败时保留向导，改完还能重试（不要因为一次 400 就把用户输入清掉）
            self.notice("启动失败：" + type(exc).__name__ + ": " + str(exc), "error")
        self.dirty = True

    def _rt_save(self, payload):
        try:
            with open(ROUNDTABLE_PATH, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def rt_start_last(self):
        try:
            with open(ROUNDTABLE_PATH, encoding="utf-8") as fh:
                payload = json.load(fh)
        except Exception:
            self.notice("没有上次圆桌配置；先 /round 新建一次", "error")
            return
        if not isinstance(payload, dict) or not payload.get("participants"):
            self.notice("上次圆桌配置不可用", "error")
            return
        if self.rt and self.rt.get("state") in ("running", "stopping", "pausing"):
            self.notice("圆桌正在进行中；先 /round stop 暂停")
            return
        w = {
            "stage": "setup", "participants": [], "idx": 0, "sel": 0,
            "rounds": 2, "topic": payload.get("topic") or "自由讨论",
            "new_conversation": bool(payload.get("new_conversation", True)),
        }
        try:
            w["rounds"] = max(1, min(20, int(payload.get("rounds") or 2)))
        except (TypeError, ValueError):
            w["rounds"] = 2
        for item in payload.get("participants") or []:
            if isinstance(item, str):
                agent, role, prompt, slot = item, None, "", ""
            elif isinstance(item, dict):
                agent = str(item.get("agent") or item.get("name") or "")
                role = item.get("role")
                prompt = item.get("system_prompt") or item.get("prompt") or ""
                slot = item.get("slot") or ""
            else:
                continue
            if agent not in self.agents:
                continue
            self._rt_add_participant(w, agent, role=role, system_prompt=prompt, slot=slot)
        if len(w["participants"]) < 2:
            self.notice("上次圆桌配置不足 2 个实例", "error")
            return
        self.rt_wizard = w
        self.editor.clear()
        self.scroll = 0
        self.notice("已载入上次圆桌配置；可修改后 Enter 开始")
        self.dirty = True

    def rt_command(self, args):
        sub = (args[0].lower() if args else "")
        if sub in ("stop", "pause", "end", "halt"):
            try:
                result = self.client.post("/api/roundtable/stop", {})
                self.rt = result.get("roundtable") or self.rt
                state = (self.rt or {}).get("state")
                if state == "paused":
                    self.notice("圆桌已暂停；/round resume 继续")
                elif state == "done":
                    self.notice("圆桌本轮已完成；/round resume 可再加一轮")
                else:
                    self.notice("已请求暂停：当前发言会中断，圆桌保留；/round resume 继续")
            except Exception as exc:
                self.notice("暂停失败：" + str(exc), "error")
            return
        if sub in ("resume", "continue", "go"):
            try:
                result = self.client.post("/api/roundtable/resume", {})
                self.rt = result.get("roundtable") or self.rt
                self._rt_seen = None
                self.notice("圆桌继续；/round status 查看进度")
            except Exception as exc:
                self.notice("继续失败：" + str(exc), "error")
            return
        if sub in ("status", "show", "info"):
            self.notice(self.rt_status_text())
            return
        if sub in ("again", "last", "reuse"):
            self.rt_start_last()
            return
        if sub in ("cancel", "abort"):
            self.rt_cancel()
            return
        if self.rt_wizard is not None:
            self.notice("圆桌设置进行中；Esc 取消，或继续按提示输入")
            return
        self.rt_begin_wizard()

    def send(self, text):
        text = text.strip()
        if not text:
            return
        payload = {"text": text, "attachments": [], "client_msg_id": uuid.uuid4().hex}
        if self.targets:
            names = [n for n in self.targets if n in self.agents]
            payload["targets"] = names
            payload["target"] = ",".join(names)
        else:
            payload["target"] = "all"
            payload["targets"] = []
        try:
            result = self.client.post("/api/send", payload)
            if not result.get("ok"):
                self.notice("发送失败：" + str(result.get("error")), "error")
        except Exception as exc:
            self.notice(f"发送失败：{type(exc).__name__}: {exc}", "error")

    def command(self, line):
        parts = line[1:].split()
        if not parts:
            return
        cmd, args = parts[0].lower(), parts[1:]
        if cmd in ("q", "quit", "exit"):
            self._stopping = True
            return
        if cmd in ("h", "help", "?"):
            for text in [
                "←/→ 切换目标（全体 ↔ 成员）；Ctrl+←/→ 移动光标；/all 回全体",
                "/stop 停止所有轮次（圆桌保留为暂停；不动接力开关）；/interrupt 1 打断单个",
                "/relay 切换全局接力；/retry <消息id> 重试失败轮次",
                "/queue 查看排队与运行中的任务；/cancel <队列id> 取消其中一条",
                "/tasks 进行中的任务；/runs 最近运行表；/replay <消息id> 重跑该轮",
                "/think 折叠/展开思维链；/clear 清屏（只影响本终端）",
                "/round 多 CLI 角色圆桌（同一 Agent 可重复；固定顺序/轮数/系统提示）；/round stop｜resume｜status｜again",
                "↑/↓ 翻发送历史；Ctrl+R 搜历史（Enter 采用，Esc 取消）",
                "鼠标滚轮上下查看记录；Ctrl+C 退出",
            ]:
                self.notice(text)
            return
        if cmd == "all":
            self.targets = []
            self.notice("目标：全体")
            return
        if cmd in ("tasks", "task"):
            try:
                data = self.client.get("/api/tasks?status=open")
            except Exception as exc:
                self.notice(f"读取任务失败：{exc}", "error")
                return
            items = data.get("tasks") or []
            if not items:
                self.notice("没有进行中的任务")
                return
            lines = ["进行中的任务："]
            for t in items:
                lines.append(f"#{t['id']} [{t['status']}] {t['owner']}：{(t['title'] or '')[:36]}")
            self.notice("\n".join(lines))
            return
        if cmd in ("runs", "run"):
            try:
                data = self.client.get("/api/runs?limit=20")
            except Exception as exc:
                self.notice(f"读取运行表失败：{exc}", "error")
                return
            items = data.get("runs") or []
            if not items:
                self.notice("还没有运行记录")
                return
            lines = ["最近 20 次运行（turn 用 /replay 重跑）："]
            for r in items:
                lines.append(f"{r['agent']} [{r['status']}] {r['tokens']}tok "
                             f"{r.get('trigger_kind') or ''}")
            self.notice("\n".join(lines))
            return
        if cmd == "replay":
            if not args or not args[0].isdigit():
                self.notice("用法：/replay <消息id>（回复某条消息即重跑该成员）", "error")
                return
            try:
                r = self.client.post("/api/replay", {"message_id": int(args[0])})
                self.notice("已重放：" + "、".join(r.get("dispatched") or []))
            except Exception as exc:
                self.notice(f"重放失败：{exc}", "error")
            return
        if cmd == "queue":
            try:
                data = self.client.get("/api/queue")
            except Exception as exc:
                self.notice(f"读取队列失败：{exc}", "error")
                return
            items = data.get("open") or []
            depth = data.get("depth") or {}
            head = (f"排队 {depth.get('queued', 0)}，运行 {depth.get('running', 0)}，"
                    f"并发上限 {data.get('max_parallel', '?')}")
            if not items:
                self.notice(head + "（队列为空）")
                return
            lines = [head]
            for it in items:
                mark = "运行中" if it.get("status") == "running" else "排队中"
                lines.append(f"#{it['id']} {mark} {it['agent']}："
                             f"{(it.get('preview') or '')[:40]}")
            self.notice("\n".join(lines))
            return
        if cmd == "cancel":
            if not args or not args[0].isdigit():
                self.notice("用法：/cancel <队列id>（用 /queue 查 id）", "error")
                return
            try:
                r = self.client.post("/api/queue/cancel", {"id": int(args[0])})
            except Exception as exc:
                self.notice(f"取消失败：{exc}", "error")
                return
            self.notice({"cancelled": "已取消（排队中，未执行）",
                         "requested": "已请求中断（运行中，稍后收尾）"
                         }.get(r.get("state") or "", "没有这个任务"))
            return
        if cmd == "stop":
            try:
                self.client.post("/api/stop", {"disable_relay": False})
                self.notice("已发送停止；圆桌会保留为暂停，可 /round resume 继续")
            except Exception as exc:
                self.notice(f"停止失败：{exc}", "error")
            return
        if cmd == "relay":
            try:
                result = self.client.post("/api/relay", {"enabled": not self.relay})
                self.relay = bool(result.get("relay"))
                self.notice("接力：" + ("开" if self.relay else "关"))
            except Exception as exc:
                self.notice(f"切换失败：{exc}", "error")
            return
        if cmd == "interrupt":
            if not args:
                self.notice("用法：/interrupt 1", "error")
                return
            token = args[0]
            name = None
            if token.isdigit() and 1 <= int(token) <= len(self.agent_order):
                name = self.agent_order[int(token) - 1]
            elif token in self.agents:
                name = token
            if not name:
                self.notice("未知成员", "error")
                return
            try:
                self.client.post("/api/interrupt", {"agent": name})
                self.notice(f"已打断 {self.agents[name]['label']}")
            except Exception as exc:
                self.notice(f"打断失败：{exc}", "error")
            return
        if cmd in ("retry", "again"):
            if not args or not args[0].isdigit():
                self.notice("用法：/retry <消息id>", "error")
                return
            try:
                result = self.client.post("/api/retry", {"message_id": int(args[0])})
                self.notice("已重试：" + "、".join(result.get("targets") or []))
            except Exception as exc:
                self.notice(f"重试失败：{exc}", "error")
            return
        if cmd == "think":
            self.show_think = not self.show_think
            self.notice("思维链：" + ("显示" if self.show_think else "折叠"))
            return
        if cmd == "clear":
            with self._lock:
                self.messages.clear()
                self.order = []
                self._invalidate()
            self.scroll = 0
            self.notice("已清屏（只影响本终端，不删中枢记录）")
            return
        if cmd in ("round", "rt", "roundtable"):
            self.rt_command(args)
            return
        self.notice(f"未知命令：/{cmd}，输入 /help 看可用命令", "error")

    # -- 主循环 -------------------------------------------------------
    def run(self):
        enable_vt()
        # 1000=按键/滚轮事件，1006=SGR 坐标编码；退出时必须关掉，否则终端会一直吞鼠标。
        sys.stdout.write("\x1b[?1049h\x1b[?2004h\x1b[?1000h\x1b[?1006h")
        sys.stdout.flush()
        threading.Thread(target=self.client.stream, args=(self.enqueue, lambda: self._stopping),
                         daemon=True).start()
        last_render = 0.0
        last_size = (0, 0)
        try:
            while not self._stopping:
                handled = False
                self.drain()
                if self._reload_pending:
                    self._reload_pending = False
                    try:
                        self.reload_state()
                    except Exception:
                        pass
                    handled = True
                if os.name == "nt":
                    import msvcrt

                    while msvcrt.kbhit():
                        self.handle_key(read_key_windows())
                        handled = True
                else:
                    import select

                    if select.select([sys.stdin], [], [], 0)[0]:
                        for key in read_keys_posix():
                            self.handle_key(key)
                        handled = True
                size = term_size()
                now = time.time()
                if (handled or self.dirty or size != last_size) and now - last_render > 0.04:
                    sys.stdout.write(render_frame(self, size[0], size[1]))
                    sys.stdout.flush()
                    self.dirty = False
                    last_render = now
                    last_size = size
                time.sleep(0.01)
        except KeyboardInterrupt:
            pass
        finally:
            self._stopping = True
            sys.stdout.write("\x1b[?1000l\x1b[?1006l\x1b[?2004l\x1b[?25h\x1b[?1049l")
            sys.stdout.flush()

    def cycle_target(self, step):
        names = [n for n in self.agent_order if self.agents[n].get("enabled", True)]
        options = [None] + names
        cur = self.targets[0] if len(self.targets) == 1 else None
        try:
            idx = options.index(cur)
        except ValueError:
            idx = 0
        idx = (idx + step) % len(options)
        self.targets = [] if options[idx] is None else [options[idx]]
        self.dirty = True

    def start_search(self):
        self.search = {"q": "", "orig": self.editor.text(),
                       "idx": len(self.editor.history), "miss": False}
        self.search_step()

    def search_step(self):
        s = self.search
        if s is None:
            return
        q = s["q"].lower()
        i = s["idx"] - 1
        while i >= 0:
            if not q or q in self.editor.history[i].lower():
                s["idx"] = i
                text = self.editor.history[i]
                self.editor.buf, self.editor.cur = list(text), len(text)
                s["miss"] = False
                self.dirty = True
                return
            i -= 1
        s["miss"] = bool(q)
        self.dirty = True

    def search_type(self, ch):
        s = self.search
        if s is None:
            return
        s["q"] += ch
        s["idx"] = len(self.editor.history)
        self.search_step()

    def search_backspace(self):
        s = self.search
        if s is None:
            return
        if s["q"]:
            s["q"] = s["q"][:-1]
        s["idx"] = len(self.editor.history)
        self.search_step()

    def search_cancel(self):
        s = self.search
        if s is not None:
            self.editor.buf = list(s["orig"])
            self.editor.cur = len(self.editor.buf)
        self.search = None
        self.dirty = True

    def scroll_wheel(self, up):
        """鼠标滚轮：聊天区向上=回看历史；设置页向上=回到顶部。"""
        step = 3
        setup = bool(self.rt_wizard is not None and self.rt_wizard.get("stage") == "setup")
        if setup:
            self.scroll = max(0, self.scroll - step) if up else self.scroll + step
        else:
            self.scroll = max(0, self.scroll + step) if up else max(0, self.scroll - step)
        self.dirty = True

    def handle_key(self, key):
        if key in ("wheel_up", "wheel_down"):
            self.scroll_wheel(key == "wheel_up")
            return
        if self.rt_wizard is not None:
            # 向导期间 Enter 是「下一步」，不是发送；Esc 取消，左右键移动光标而不是切目标。
            if key == "ctrl_c":
                self._stopping = True
                return
            if key == "esc":
                w = self.rt_wizard
                if w.get("return_stage"):
                    # 从设置页进来的编辑（角色/提示词/轮数/议题）：Esc 只放弃本次修改，回设置页。
                    w["stage"] = w.pop("return_stage")
                    self.notice("已返回设置页（本次修改未保存）")
                    self.dirty = True
                    return
                self.rt_cancel()
                return
            if self.rt_wizard.get("stage") == "setup":
                if key in ("up", "down"):
                    self.rt_setup_key(key)
                    return
                if isinstance(key, tuple):
                    self.rt_setup_key(key[1].lower())
                    return
                if key == "enter":
                    self.rt_setup_key("s")
                    return
                return
            if key == "enter" and not self.pasting:
                text = self.editor.text()
                self.editor.clear()
                self.rt_input(text)
                self.dirty = True
                return
            if key == "ctrl_r":
                return
            if key == "left":
                self.editor.left(); self.dirty = True
                return
            if key == "right":
                self.editor.right(); self.dirty = True
                return
        if key == "ctrl_r":
            if self.search is None:
                self.start_search()
            else:
                self.search_step()
            return
        if self.search is not None:
            if isinstance(key, tuple):
                self.search_type(key[1])
                return
            if key == "backspace":
                self.search_backspace()
                return
            if key == "enter":
                self.search = None
                self.dirty = True
                return
            if key == "esc":
                self.search_cancel()
                return
            self.search = None
        if isinstance(key, tuple):
            self.editor.insert(key[1])
            self.dirty = True
            return
        if key == "paste_start":
            self.pasting = True
            return
        if key == "paste_end":
            self.pasting = False
            return
        if key == "enter" and self.pasting:
            self.editor.insert("\n")
            self.dirty = True
            return
        if key == "ctrl_c":
            self._stopping = True
        elif key == "ctrl_l":
            sys.stdout.write("\x1b[2J")
            self.dirty = True
        elif key == "enter":
            text = self.editor.text()
            self.editor.remember(text)
            self.editor.clear()
            if text.startswith("/"):
                self.command(text)
            elif text.strip():
                self.send(text)
            self.scroll = 0
            self.dirty = True
        elif key == "backspace":
            self.editor.backspace(); self.dirty = True
        elif key == "delete":
            self.editor.delete(); self.dirty = True
        elif key == "left":
            self.cycle_target(-1)
        elif key == "right":
            self.cycle_target(1)
        elif key == "edit_left":
            self.editor.left(); self.dirty = True
        elif key == "edit_right":
            self.editor.right(); self.dirty = True
        elif key == "ctrl_a":
            self.editor.home(); self.dirty = True
        elif key == "ctrl_e":
            self.editor.end(); self.dirty = True
        elif key == "home":
            self.editor.home(); self.dirty = True
        elif key == "end":
            self.editor.end(); self.dirty = True
        elif key == "up":
            self.editor.recall(-1, self.editor.text()); self.dirty = True
        elif key == "down":
            self.editor.recall(1, self.editor.text()); self.dirty = True
        elif key == "ctrl_u":
            self.editor.clear(); self.dirty = True
        elif key == "esc":
            self.editor.clear(); self.dirty = True
        elif key == "tab":
            self.editor.insert("  "); self.dirty = True


def dump(client, limit):
    state = client.get("/api/state", timeout=10)
    msgs = state.get("messages") or []
    labels = {a["name"]: a.get("label", a["name"]) for a in state.get("agents") or []}
    for msg in msgs[-limit:]:
        ts = time.strftime("%H:%M:%S", time.localtime(msg.get("ts") or 0))
        kind = msg.get("kind")
        sender = msg.get("sender") or "?"
        if kind == "event":
            print(f"{ts} · {msg.get('text')}")
        elif kind == "user":
            print(f"{ts} 你 → {target_text(msg.get('target'))}")
            print("  " + (msg.get("display_text") or msg.get("text") or "").replace("\n", "\n  "))
        else:
            print(f"{ts} {labels.get(sender, sender)} → {target_text(msg.get('target'))}")
            print("  " + (msg.get("text") or "").replace("\n", "\n  "))


def main():
    parser = argparse.ArgumentParser(description="ClaudeLink 终端版")
    parser.add_argument("--dump", type=int, metavar="N", help="打印最近 N 条消息后退出")
    parser.add_argument("--check", action="store_true", help="健康检查，输出一行 JSON")
    parser.add_argument("--render-once", action="store_true", help="渲染一帧到 stdout")
    parser.add_argument("--no-start", action="store_true", help="中枢没跑时不自动拉起")
    parser.add_argument("--width", type=int, default=0, help="--render-once 的宽度")
    parser.add_argument("--height", type=int, default=0, help="--render-once 的高度")
    args = parser.parse_args()

    host, port = load_endpoint()
    client = Client(host, port)

    if args.check:
        health = client.health(timeout=2)
        print(json.dumps({
            "url": client.base,
            "up": health is not None,
            "messages": (health or {}).get("messages"),
            "running": [r.get("agent") for r in (health or {}).get("running") or []],
        }, ensure_ascii=False))
        return 0 if health is not None else 1

    if not ensure_hub(client, auto_start=not args.no_start):
        print(f"中枢未运行：{client.base}", file=sys.stderr)
        print("启动：`claudelink-hub start`（npm 安装）或 `python hub.py`，"
              "也可以去掉 --no-start 让本程序自动拉起", file=sys.stderr)
        return 2

    if args.dump:
        dump(client, args.dump)
        return 0

    app = App(client)
    app.reload_state()

    if args.render_once:
        width = args.width or term_size()[0]
        height = args.height or term_size()[1]
        sys.stdout.write(render_frame(app, width, height))
        sys.stdout.write("\n")
        return 0

    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        print("当前不是交互终端；用 --dump N 或 --render-once", file=sys.stderr)
        return 2

    app.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
