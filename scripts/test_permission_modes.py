#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""权限模式解析测试：legacy 映射、成员覆盖、Codex sandboxPolicy。"""
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
HOME = tempfile.mkdtemp(prefix="clh-perm-")
os.environ["CLAUDELINK_HOME"] = HOME
os.environ.setdefault("CLAUDELINK_CONFIG", os.path.join(ROOT, "config.example.json"))
sys.path.insert(0, ROOT)

import hub  # noqa: E402

try:
    cfg = hub.CONFIG
    cfg["agents"] = {
        "claude": {"label": "Claude", "enabled": True},
        "codex": {"label": "Codex", "enabled": True},
        "codebuddy": {"label": "CodeBuddy", "enabled": True},
        "dsh": {"label": "DeepSeek Harness", "enabled": True},
    }
    cfg["permissions"] = {"default": "unrestricted"}
    assert hub.HUB.permission_mode("claude") == "danger"
    cfg["permissions"] = {"mode": "read-only"}
    assert hub.HUB.permission_mode("claude") == "read-only"
    cfg["agents"]["claude"]["permissions"] = {"mode": "workspace-write"}
    assert hub.HUB.permission_mode("claude") == "workspace-write"
    cfg["permissions"] = {}
    cfg["agents"]["codex"]["sandbox"] = "read-only"
    assert hub.HUB.permission_mode("codex") == "read-only"

    cfg["permissions"] = {"mode": "read-only"}
    assert hub.HUB.codex_turn_policy("codex")[0]["type"] == "readOnly"
    cfg["permissions"] = {"mode": "workspace-write"}
    policy, cwd = hub.HUB.codex_turn_policy("codex")
    assert policy["type"] == "workspaceWrite" and policy["networkAccess"] is True
    cfg["permissions"] = {"mode": "danger"}
    assert hub.HUB.codex_turn_policy("codex")[0]["type"] == "dangerFullAccess"

    # 只有 ro grant 时，workspace-write 不能回落 danger
    cfg["permissions"] = {"mode": "workspace-write"}
    hub.HUB.store.set_grant("codex", os.path.join(HOME, "ro"), "ro")
    assert hub.HUB.codex_turn_policy("codex")[0]["type"] == "workspaceWrite"
    print("PERMISSION_MODES_OK")
finally:
    try:
        hub.HUB.store.close()
    except Exception:
        pass
    shutil.rmtree(HOME, ignore_errors=True)
