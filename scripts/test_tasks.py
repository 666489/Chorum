#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""任务模型（ORC-01）+ 运行表（UX-06）自测。

跑法：python scripts/test_tasks.py
"""
import json
import os
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
TMP = tempfile.mkdtemp(prefix="clh-task-")
os.environ["CLAUDELINK_HOME"] = TMP
os.environ["CLAUDELINK_DB"] = os.path.join(TMP, "hub.db")
CFG = os.path.join(TMP, "config.json")
os.environ["CLAUDELINK_CONFIG"] = CFG
with open(CFG, "w", encoding="utf-8") as fh:
    json.dump({"server": {"port": 8798, "auth": False}, "agents": {},
               "workspace": {}, "detect": {}, "dispatch": {}, "relay": {},
               "queue": {"max_parallel": 2}, "context": {"history_limit": 10}},
              fh, ensure_ascii=False)

import hub  # noqa: E402

FAILED = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (("  " + str(extra)) if extra and not cond else ""))
    if not cond:
        FAILED.append(name)


def wait_drain(store, timeout=6.0):
    end = time.time() + timeout
    while time.time() < end:
        d = store.queue_depth()
        if not d["queued"] and not d["running"]:
            return True
        time.sleep(0.05)
    return False


def main():
    H = hub.HUB
    S = H.store

    # 1) 任务基本生命周期
    t = S.task_create(1, "claude", "修一下登录", created_by="user", depth=0)
    check("创建后是 submitted", t and t["status"] == "submitted", t)
    S.task_update(t["id"], status="working")
    check("可以转 working", S.task_get(t["id"])["status"] == "working")
    done = S.task_close(t["id"], "completed", "改完并自测通过")
    check("关闭写终态与理由", done["status"] == "completed" and done["closure_reason"] == "改完并自测通过")
    try:
        S.task_close(t["id"], "whatever")
        check("非法终态应报错", False)
    except ValueError:
        check("非法终态应报错", True)

    # 2) 开放任务列表只含未关闭的
    open_t = S.task_create(1, "codex", "补测试")
    names = [x["id"] for x in S.task_list(1, status="open")]
    check("开放列表包含新任务", open_t["id"] in names, names)
    check("开放列表不含已关闭任务", t["id"] not in names, names)

    # 3) 消息挂任务
    msg = H.post("user", "claude", "user", "帮我改登录", conv_id=1)
    S.link_message_task(msg["id"], task_id=open_t["id"], expects_reply=True)
    row = S.con.execute("SELECT task_id, expects_reply FROM messages WHERE id=?",
                        (msg["id"],)).fetchone()
    check("消息挂上了 task_id", row and row[0] == open_t["id"], row)
    check("expects_reply 落库", row and row[1] == 1, row)

    # 4) 走调度器：入队即建任务，跑完自动闭合
    H._run_agent = lambda *a, **k: "ok"
    qid = H.job_submit("claude", "跑一轮", "user", "claude", 0)
    wait_drain(S)
    task = S.con.execute(
        "SELECT t.status, t.owner, t.closure_reason FROM tasks t"
        " JOIN turn_queue q ON q.id=? WHERE t.message_id IS NULL OR 1=1"
        " ORDER BY t.id DESC LIMIT 1", (qid,)).fetchone()
    check("入队自动建任务", bool(task), task)
    check("任务跑完是 completed", task and task[0] == "completed", task)
    check("owner 是执行成员", task and task[1] == "claude", task)

    # 5) 接力子任务带 parent_task_id
    parent = S.task_create(1, "claude", "父任务")
    H.job_submit("codex", "接力", "claude", "codex", 1, parent_task_id=parent["id"])
    wait_drain(S)
    child = S.con.execute(
        "SELECT parent_task_id FROM tasks WHERE owner='codex' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    check("子任务记录了父任务", child and child[0] == parent["id"], child)

    # 6) 运行表：轮次左连任务
    S.con.execute(
        "INSERT INTO turns(id, trace_id, agent, trigger_kind, start_ts, status,"
        " conv_id, reply_msg_id, in_tokens, out_tokens)"
        " VALUES('turn-1','tr', 'claude','user',?, 'ok', 1, ?, 100, 20)",
        (int(time.time() * 1000), msg["id"]))
    S.con.commit()
    S.task_update(open_t["id"], reply_msg_id=msg["id"])
    runs = S.runs(1)
    hit = [r for r in runs if r["turn_id"] == "turn-1"]
    check("运行表能取到该轮", bool(hit), len(runs))
    check("运行表带上任务", hit and hit[0]["task_id"] == open_t["id"], hit)
    check("运行表统计 token", hit and hit[0]["tokens"] == 120, hit)

    # 7) 产物
    aid = S.artifact_add(open_t["id"], "file", "a.py", "deadbeef", 128, msg["id"])
    arts = S.artifact_list(open_t["id"])
    check("产物可写入可回读", arts and arts[0]["id"] == aid and arts[0]["bytes"] == 128)

    print()
    if FAILED:
        print("失败 %d 项：%s" % (len(FAILED), "、".join(FAILED)))
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
