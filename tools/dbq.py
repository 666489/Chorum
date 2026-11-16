#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""hub.db 只读查询小工具（默认只读；exec 子命令才会写）。

用法（解释器固定用 py11：D:\\Anaconda3\\envs\\py11\\python.exe）：
  python tools/dbq.py kinds                 # 各类消息条数
  python tools/dbq.py errors                # 最近 12 条失败消息
  python tools/dbq.py last [N]              # 最近 N 条消息
  python tools/dbq.py turns                 # 最近 15 个轮次
  python tools/dbq.py steps                 # 各类步骤条数与空 detail 数
  python tools/dbq.py perms                 # 权限申请（含状态）
  python tools/dbq.py grants                # 当前授权表
  python tools/dbq.py state                 # state 表（公告 / relay / 开关 / grants_sig）
  python tools/dbq.py sql "<SELECT ...>"    # 任意只读 SQL
  python tools/dbq.py exec "<UPDATE ...>"   # 写操作（会 commit，谨慎）

连的是 live 库；写操作前请先按 §诊断日志 的做法复制 hub.db(+wal+shm) 备份。
"""
import os
import sqlite3
import sys

HUB = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(HUB, "hub.db")


def show(sql, args=(), width=200):
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    print("SQL:", sql)
    try:
        for row in con.execute(sql, args):
            print({k: (row[k][:width].replace("\n", " ⏎ ") if isinstance(row[k], str) else row[k])
                   for k in row.keys()})
    finally:
        con.close()
    print("-" * 60)


def main():
    what = sys.argv[1] if len(sys.argv) > 1 else "kinds"
    if what == "kinds":
        show("SELECT kind, COUNT(*) n FROM messages GROUP BY kind ORDER BY n DESC")
    elif what == "errors":
        show("SELECT id, ts, sender, target, ref, text FROM messages "
             "WHERE kind='error' ORDER BY id DESC LIMIT 12")
    elif what == "last":
        limit = int(sys.argv[2]) if len(sys.argv) > 2 else 20
        show(f"SELECT id, ts, sender, target, kind, ref, text FROM messages "
             f"ORDER BY id DESC LIMIT {limit}")
    elif what == "turns":
        show("SELECT id, agent, status, start_ts, end_ts, in_tokens, out_tokens, "
             "reply_msg_id FROM turns ORDER BY start_ts DESC LIMIT 15")
    elif what == "steps":
        show("SELECT kind, COUNT(*) n, SUM(detail IS NULL) nulls, MAX(length(detail)) maxlen "
             "FROM steps GROUP BY kind")
        show("SELECT id, turn_id, kind, name, status, length(detail) dlen FROM steps "
             "ORDER BY id DESC LIMIT 12")
    elif what == "perms":
        show("SELECT id, agent, path, mode, status, reason, turn_id FROM perm_requests "
             "ORDER BY id DESC LIMIT 20")
    elif what == "grants":
        show("SELECT * FROM grants ORDER BY agent, path")
    elif what == "state":
        show("SELECT * FROM state ORDER BY k")
    elif what == "sql":
        show(sys.argv[2])
    elif what == "exec":
        con = sqlite3.connect(DB)
        con.execute(sys.argv[2])
        con.commit()
        con.close()
        print("committed:", sys.argv[2])
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
