#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""ClaudeLink 会话镜像：把 hub.db 里的完整会话持续导出成文件。

两个文件，职责分开：
  logs/conversation_full.md   追加式总档案（只写不读，供人查）
  logs/conversation_cache.md  缓存区（只读它；每读一次即清空，避免重复进上下文）

用法：
  python tools/conv_feed.py --pump            # 常驻，实时把新消息写进上面两个文件
  python tools/conv_feed.py --once            # 只同步一次
  python tools/conv_feed.py --drain           # 先同步，再打印缓存并清空（读缓存用这个）
  python tools/conv_feed.py --drain --max 200 # 只取最近 200 条，其余保留在缓存里
  python tools/conv_feed.py --reset           # 重置偏移并清空缓存
"""
import argparse
import json
import os
import sqlite3
import sys
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(BASE, "hub.db")
LOG_DIR = os.path.join(BASE, "logs")
FULL = os.path.join(LOG_DIR, "conversation_full.md")
CACHE = os.path.join(LOG_DIR, "conversation_cache.md")
STATE = os.path.join(LOG_DIR, ".conv_feed_state.json")
LOCKP = os.path.join(LOG_DIR, ".conv_feed.lock")

try:
    import msvcrt
except ImportError:  # 非 Windows
    msvcrt = None


class FileLock:
    """跨进程小锁，防止 pump 追加时 drain 正好清空导致丢行。"""

    def __init__(self, path, timeout=10.0):
        self.path = path
        self.timeout = timeout
        self.fh = None

    def __enter__(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self.fh = open(self.path, "a+b")
        end = time.time() + self.timeout
        while True:
            try:
                if msvcrt:
                    self.fh.seek(0)
                    msvcrt.locking(self.fh.fileno(), msvcrt.LK_NBLCK, 1)
                return self
            except OSError:
                if time.time() > end:
                    return self  # 拿不到锁也继续，宁可重读也不卡死
                time.sleep(0.05)

    def __exit__(self, *exc):
        try:
            if msvcrt:
                self.fh.seek(0)
                msvcrt.locking(self.fh.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        self.fh.close()
        return False


def read_state():
    try:
        with open(STATE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"last_id": 0, "conv_id": None}


def write_state(st):
    os.makedirs(LOG_DIR, exist_ok=True)
    tmp = STATE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False)
    os.replace(tmp, STATE)


def connect():
    conn = sqlite3.connect("file:%s?mode=ro" % DB.replace("\\", "/"), uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def fmt_time(ts):
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(ts)))
    except (TypeError, ValueError):
        return "---------- --------"


def render(rows, conv_titles, start_conv=None):
    out = []
    seen_conv = start_conv
    for r in rows:
        cid = r["conv_id"] or 1
        if cid != seen_conv:
            out.append("### 会话 #%s %s" % (cid, conv_titles.get(cid, "")))
            out.append("")
            seen_conv = cid
        sender = r["sender"] or "?"
        target = r["target"] or "all"
        kind = r["kind"] or "text"
        body = (r["display_text"] or r["text"] or "").strip()
        if kind == "event":
            out.append("【事件】%s  %s" % (fmt_time(r["ts"]), body))
        else:
            out.append("**[%s] %s -> %s**  (id=%s, %s)" % (
                fmt_time(r["ts"]), sender, target, r["id"], kind))
            out.append(body)
        out.append("")
    return "\n".join(out)


def fetch_new(last_id):
    if not os.path.exists(DB):
        return [], last_id, {}
    conn = connect()
    try:
        titles = {}
        for row in conn.execute("SELECT id, title FROM conversations"):
            titles[row["id"]] = row["title"] or ""
        rows = list(conn.execute(
            "SELECT id, ts, sender, target, kind, text, display_text, conv_id "
            "FROM messages WHERE id > ? ORDER BY id ASC", (last_id,)))
    finally:
        conn.close()
    new_last = rows[-1]["id"] if rows else last_id
    return rows, new_last, titles


def sync(verbose=False):
    """把新消息同时写进总档案和缓存。"""
    st = read_state()
    rows, new_last, titles = fetch_new(int(st.get("last_id") or 0))
    if not rows:
        return 0
    text = render(rows, titles, st.get("conv_id"))
    with FileLock(LOCKP):
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(FULL, "a", encoding="utf-8") as f:
            f.write(text)
        with open(CACHE, "a", encoding="utf-8") as f:
            f.write(text)
    st["last_id"] = new_last
    st["conv_id"] = rows[-1]["conv_id"] or 1
    write_state(st)
    if verbose:
        print("已镜像 %d 条消息" % len(rows))
    return len(rows)


def drain(max_items=None):
    sync()
    with FileLock(LOCKP):
        if not os.path.exists(CACHE):
            return "（缓存为空：没有新内容）"
        with open(CACHE, "r", encoding="utf-8", errors="replace") as f:
            data = f.read()
        if not data.strip():
            open(CACHE, "w", encoding="utf-8").close()
            return "（缓存为空：没有新内容）"
        if max_items:
            blocks = data.split("\n\n")
            keep = blocks[-max_items:]
            rest = blocks[:-max_items]
            data = "\n\n".join(keep)
            with open(CACHE, "w", encoding="utf-8") as f:
                f.write("\n\n".join(rest))
        else:
            open(CACHE, "w", encoding="utf-8").close()
    return data


def pump(interval):
    print("conv_feed 常驻中：%s -> %s" % (DB, CACHE))
    sys.stdout.flush()
    while True:
        try:
            sync()
        except Exception as e:  # 单次失败不退出
            print("同步失败：%r" % (e,))
            sys.stdout.flush()
        time.sleep(interval)


def main(argv=None):
    ap = argparse.ArgumentParser(description="ClaudeLink 会话镜像工具")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--pump", action="store_true", help="常驻同步")
    g.add_argument("--once", action="store_true", help="同步一次")
    g.add_argument("--drain", action="store_true", help="同步后打印并清空缓存")
    g.add_argument("--reset", action="store_true", help="重置偏移并清空缓存")
    ap.add_argument("--max", type=int, default=None, help="drain 时最多取最近 N 条")
    ap.add_argument("--interval", type=float, default=1.0, help="pump 轮询间隔秒")
    a = ap.parse_args(argv)

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    if a.reset:
        os.makedirs(LOG_DIR, exist_ok=True)
        write_state({"last_id": 0, "conv_id": None})
        open(CACHE, "w", encoding="utf-8").close()
        print("已重置偏移并清空缓存")
        return 0
    if a.pump:
        pump(a.interval)
        return 0
    if a.drain:
        print(drain(a.max))
        return 0
    sync(verbose=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
