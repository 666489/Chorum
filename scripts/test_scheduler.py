#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""调度器行为自测：单飞、并发上限、取消、预算闸门、熔断。

跑法：python scripts/test_scheduler.py
"""
import json
import os
import sys
import tempfile
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
TMP = tempfile.mkdtemp(prefix="clh-sched-")
os.environ["CLAUDELINK_HOME"] = TMP
os.environ["CLAUDELINK_DB"] = os.path.join(TMP, "hub.db")
CFG = os.path.join(TMP, "config.json")
os.environ["CLAUDELINK_CONFIG"] = CFG

CFG_BODY = {
    "server": {"host": "127.0.0.1", "port": 8799, "auth": False},
    "agents": {}, "workspace": {}, "detect": {}, "dispatch": {}, "relay": {},
    "queue": {"max_parallel": 2, "fail_streak": 2, "cooldown_sec": 30},
    "budget": {"max_tokens": 0, "max_turns": 0},
    "context": {"history_limit": 10},
}
with open(CFG, "w", encoding="utf-8") as fh:
    json.dump(CFG_BODY, fh, ensure_ascii=False)

import hub  # noqa: E402

FAILED = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (("  " + str(extra)) if extra and not cond else ""))
    if not cond:
        FAILED.append(name)


class Recorder:
    def __init__(self, sleep=0.35, boom=False):
        self.lock = threading.Lock()
        self.calls = []
        self.live = 0
        self.peak = 0
        self.sleep = sleep
        self.boom = boom

    def __call__(self, agent, text, sender, target, depth, attachments=None, **kw):
        with self.lock:
            self.live += 1
            self.peak = max(self.peak, self.live)
            self.calls.append((agent, text, time.time()))
        time.sleep(self.sleep)
        with self.lock:
            self.live -= 1
        if self.boom:
            raise RuntimeError("boom")
        return "ok:" + text


def wait_drain(store, timeout=8.0):
    end = time.time() + timeout
    while time.time() < end:
        d = store.queue_depth()
        if not d["queued"] and not d["running"]:
            return True
        time.sleep(0.05)
    return False


def main():
    H = hub.HUB

    # 1) 单飞：同一成员同时只跑一轮
    rec = Recorder()
    H._run_agent = rec
    for i in range(4):
        H.job_submit("claude", "m%d" % i, "user", "claude", 0)
    check("同成员任务全部跑完", wait_drain(H.store), H.store.queue_depth())
    check("同成员没有并发", rec.peak == 1, rec.peak)
    check("四次调用都发生", len(rec.calls) == 4, len(rec.calls))

    # 2) 并发上限：不同成员最多 max_parallel 个同时跑
    rec2 = Recorder(sleep=0.4)
    H._run_agent = rec2
    for name in ("a1", "a2", "a3", "a4"):
        H.job_submit(name, "x", "user", "all", 0)
    check("跨成员任务跑完", wait_drain(H.store))
    check("并发不超过 max_parallel", rec2.peak <= 2, rec2.peak)
    check("确实并行过（>1）", rec2.peak == 2, rec2.peak)

    # 3) 取消排队中的任务：永远不执行
    rec3 = Recorder(sleep=0.5)
    H._run_agent = rec3
    q1 = H.job_submit("b1", "keep", "user", "all", 0)
    time.sleep(0.15)                        # 让第一条先跑起来
    q2 = H.job_submit("b2", "drop", "user", "all", 0)
    state = H.cancel_job(q2)
    wait_drain(H.store)
    check("取消排队任务返回 cancelled", state == "cancelled", state)
    check("被取消的任务没有执行", all(t != "drop" for _, t, _ in rec3.calls), rec3.calls)

    # 4) 预算闸门：轮次预算用尽后新任务被拦住
    with open(CFG, "w", encoding="utf-8") as fh:
        json.dump({**CFG_BODY, "budget": {"max_turns": 1}}, fh, ensure_ascii=False)
    hub.reload_config()
    H.store.con.execute(
        "INSERT INTO turns(id, trace_id, agent, trigger_kind, start_ts, status, conv_id)"
        " VALUES('t-x','tr',1,'user',?, 'ok', 1)", (int(time.time() * 1000),))
    H.store.con.commit()
    rec4 = Recorder(sleep=0.05)
    H._run_agent = rec4
    H.job_submit("c1", "blocked", "user", "all", 0)
    wait_drain(H.store)
    check("超预算的任务没有执行", not rec4.calls, rec4.calls)
    rows = H.store.con.execute(
        "SELECT status, error FROM turn_queue ORDER BY id DESC LIMIT 1").fetchone()
    check("超预算任务标记为 blocked", rows and rows[0] == "blocked", rows)
    check("拦截原因写进错误列", rows and "预算" in (rows[1] or ""), rows)

    # 5) 熔断：连续失败到阈值后进入冷却
    with open(CFG, "w", encoding="utf-8") as fh:
        json.dump(CFG_BODY, fh, ensure_ascii=False)
    hub.reload_config()
    H.breaker.clear()
    H.store.queue_clear()
    H.store.con.execute("DELETE FROM turns")
    H.store.con.commit()
    rec5 = Recorder(sleep=0.01, boom=True)
    H._run_agent = rec5
    for _ in range(2):
        H.job_submit("d1", "fail", "user", "all", 0)
        wait_drain(H.store)
    ok, why = H.admit("d1", 1)
    check("连续失败后进入冷却", not ok and "冷却" in (why or ""), why)
    ok2, why2 = H.admit("other", 1)
    check("冷却只影响失败的成员", ok2, why2)

    print()
    if FAILED:
        print("失败 %d 项：%s" % (len(FAILED), "、".join(FAILED)))
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
