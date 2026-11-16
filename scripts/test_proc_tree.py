#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""进程树终止测试：父进程再 spawn 子进程，terminate_tree 后两者都必须消失。"""
import os
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
HOME = tempfile.mkdtemp(prefix="clh-proc-")
os.environ["CLAUDELINK_HOME"] = HOME
os.environ.setdefault("CLAUDELINK_CONFIG", os.path.join(ROOT, "config.example.json"))
sys.path.insert(0, ROOT)

import hub  # noqa: E402

def alive(pid):
    # Windows 上 os.kill(pid, 0) 对活进程也可能抛 EINVAL，改用进程映像探测。
    if os.name == "nt":
        return bool(hub.process_image_path(pid))
    try:
        os.kill(int(pid), 0)
        return True
    except OSError:
        return False

try:
    marker = os.path.join(HOME, "child.pid")
    code = (
        "import os, subprocess, sys, time; "
        "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
        "open(os.environ['MARKER'], 'w').write(str(p.pid)); "
        "time.sleep(60)"
    )
    env = dict(os.environ, MARKER=marker)
    proc = subprocess.Popen([sys.executable, "-c", code], env=env,
                            **hub._popen_session_kwargs())
    child = None
    for _ in range(100):
        if os.path.exists(marker):
            child = int(open(marker).read().strip())
            break
        time.sleep(0.1)
    assert child, "子进程没有写 marker"
    assert proc.poll() is None and alive(child)
    hub.terminate_tree(proc, timeout=5)
    time.sleep(0.5)
    assert proc.poll() is not None, "父进程未退出"
    assert not alive(child), "子进程未退出"
    print("PROC_TREE_OK")
finally:
    try:
        hub.HUB.store.close()
    except Exception:
        pass
    shutil.rmtree(HOME, ignore_errors=True)
