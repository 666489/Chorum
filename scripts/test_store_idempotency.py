#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""client_msg_id 幂等性测试：同一 (会话, client_msg_id) 只落一条消息。"""
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
HOME = tempfile.mkdtemp(prefix="clh-idem-")
os.environ["CLAUDELINK_HOME"] = HOME
os.environ.setdefault("CLAUDELINK_CONFIG", os.path.join(ROOT, "config.example.json"))
sys.path.insert(0, ROOT)

import hub  # noqa: E402

try:
    store = hub.HUB.store
    a = store.add_message("user", "all", "user", "hello", client_msg_id="cid-1")
    b = store.add_message("user", "all", "user", "hello", client_msg_id="cid-1")
    assert a and b and a["id"] == b["id"]
    assert a.get("_created") is True and b.get("_created") is False
    count = store.con.execute(
        "SELECT COUNT(*) FROM messages WHERE client_msg_id='cid-1'"
    ).fetchone()[0]
    assert count == 1, count
    c = store.add_message("user", "all", "user", "cross", client_msg_id="cid-1", conv_id=2)
    assert c and c["id"] != a["id"], "同一 id 在不同会话应各有一条"
    d = store.add_message("user", "all", "user", "no-id")
    e = store.add_message("user", "all", "user", "no-id")
    assert d and e and d["id"] != e["id"], "没有 client_msg_id 时不应去重"
    print("IDEMPOTENCY_OK")
finally:
    try:
        hub.HUB.store.close()
    except Exception:
        pass
    shutil.rmtree(HOME, ignore_errors=True)
