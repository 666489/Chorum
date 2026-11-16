#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""队列 / 预算 / 压缩账本的自测（不需要起服务、不碰正式 hub.db）。

跑法：python scripts/test_queue.py
"""
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
TMP = tempfile.mkdtemp(prefix="clh-queue-")
os.environ["CLAUDELINK_HOME"] = TMP
os.environ["CLAUDELINK_DB"] = os.path.join(TMP, "hub.db")
os.environ["CLAUDELINK_CONFIG"] = os.path.join(TMP, "config.json")

import hub  # noqa: E402

FAILED = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (("  " + str(extra)) if extra and not cond else ""))
    if not cond:
        FAILED.append(name)


def main():
    store = hub.Store(os.environ["CLAUDELINK_DB"])

    # 1) 入队 → 认领：同一成员不会被同时认领两次（单飞）
    a = store.queue_add("claude", 1, {"text": "one"})
    b = store.queue_add("claude", 1, {"text": "two"})
    first = store.queue_claim([])
    check("认领拿到第一条", first and first["id"] == a, first)
    second = store.queue_claim(["claude"])
    check("同成员忙时不再认领", second is None, second)
    third = store.queue_claim([])
    check("成员空闲后可认领第二条", third and third["id"] == b, third)

    # 2) 深度/排队信息
    depth = store.queue_depth()
    check("队列深度 running=2", depth["running"] == 2 and depth["queued"] == 0, depth)

    # 3) 取消排队中的任务
    c = store.queue_add("codex", 1, {"text": "three"})
    check("排队中取消返回 cancelled", store.queue_cancel(c) == "cancelled")
    check("取消后不会再被认领", store.queue_claim([]) is None)

    # 4) 取消运行中的任务 → 标记 requsted，任务行保留
    check("运行中取消返回 requested", store.queue_cancel(a) == "requested")
    check("运行中任务仍在队列里", any(r["id"] == a for r in store.queue_open()))

    # 5) 结束与对账
    store.queue_finish(a, "done")
    store.queue_finish(b, "failed", "boom")
    left = store.queue_add("codex", 1, {"text": "left"})
    store.queue_claim([])
    dead = store.queue_recover()
    check("重启对账把遗留任务判死", dead >= 1, dead)
    check("对账后队列为空", store.queue_depth() == {"queued": 0, "running": 0})

    # 6) 停止键清队
    store.queue_add("claude", 1, {"text": "x"})
    store.queue_add("claude", 1, {"text": "y"})
    n = store.queue_clear()
    check("停止键清空排队任务", n == 2 and store.queue_depth()["queued"] == 0, n)

    # 7) 压缩账本
    cid = store.compaction_add(1, "claude", "anchor", 1200, 80, 400, 30, [1, 2, 3], "摘要")
    rows = store.compaction_list(1)
    check("压缩账本可写入可回读", rows and rows[0]["id"] == cid and rows[0]["tokens_before"] == 400)

    # 8) token 估算与预算闸门（纯函数）
    t = hub.est_tokens("你好世界")
    check("CJK 估算约等于字数", 3 <= t <= 6, t)
    check("空串估算为 0", hub.est_tokens("") == 0)

    # 9) 排队优先级：priority 高的先认领
    store.queue_add("claude", 1, {"text": "low"}, priority=0)
    hi = store.queue_add("claude", 1, {"text": "high"}, priority=5)
    got = store.queue_claim([])
    check("高优先级先出队", got and got["id"] == hi, got)

    print()
    if FAILED:
        print("失败 %d 项：%s" % (len(FAILED), "、".join(FAILED)))
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
