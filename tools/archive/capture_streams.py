"""抓两条 CLI 链的原始事件流，用来确定 usage 字段的真实位置。

不猜字段名 —— 直接落盘原始 JSONL，再按事实改 hub.py。
输出：.verify/raw-codebuddy.jsonl / .verify/raw-codex.jsonl
"""
import json
import os
import queue
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
HUB = os.path.dirname(HERE)
NO_WINDOW = 0x08000000 if os.name == "nt" else 0
PROMPT = "Reply with exactly: OK"
CODEX = r"C:\Users\Administrator\AppData\Local\OpenAI\Codex\bin\13995fba801849b0\codex.exe"
CB_NODE = "node"
CB_SCRIPT = r"D:\Workbuddy\resources\app.asar.unpacked\cli\bin\codebuddy"


def dump(name, lines):
    path = os.path.join(HERE, name)
    with open(path, "w", encoding="utf-8") as fh:
        for ln in lines:
            fh.write(ln.rstrip("\n") + "\n")
    print(f"[dump] {name}: {len(lines)} lines")


def capture_codebuddy():
    argv = [CB_NODE, CB_SCRIPT, "-p", "--output-format", "stream-json",
            "--include-partial-messages", "-y"]
    print("[codebuddy] spawn", argv)
    proc = subprocess.Popen(argv, cwd=HUB, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                            errors="replace", bufsize=1, creationflags=NO_WINDOW)
    proc.stdin.write(PROMPT)
    proc.stdin.close()
    lines = []
    deadline = time.time() + 120
    for line in proc.stdout:
        lines.append(line)
        if time.time() > deadline:
            break
    try:
        proc.kill()
    except Exception:
        pass
    dump("raw-codebuddy.jsonl", lines)
    kinds = {}
    for ln in lines:
        s = ln.strip()
        if not s.startswith("{"):
            continue
        try:
            o = json.loads(s)
        except json.JSONDecodeError:
            continue
        t = o.get("type")
        if t == "stream_event":
            ev = (o.get("event") or {}).get("type")
            kinds[f"stream_event/{ev}"] = kinds.get(f"stream_event/{ev}", 0) + 1
            if ev in ("message_start", "message_delta"):
                print(f"  [{ev}] usage=", json.dumps((o['event'].get('usage') or {}),
                                                    ensure_ascii=False)[:300],
                      "| message.usage=", json.dumps(
                          ((o['event'].get('message') or {}).get('usage') or {}),
                          ensure_ascii=False)[:300])
        else:
            kinds[t] = kinds.get(t, 0) + 1
    print("[codebuddy] kinds:", json.dumps(kinds, ensure_ascii=False))


def capture_codex():
    print("[codex] spawn app-server")
    proc = subprocess.Popen([CODEX, "app-server"], cwd=HUB, stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                            encoding="utf-8", errors="replace", bufsize=1,
                            creationflags=NO_WINDOW)
    inbox = queue.Queue()
    lines = []

    def reader():
        for line in proc.stdout:
            lines.append(line)
            inbox.put(line)
        inbox.put(None)

    threading.Thread(target=reader, daemon=True).start()

    def send(obj):
        proc.stdin.write(json.dumps(obj) + "\n")
        proc.stdin.flush()

    def next_obj(timeout):
        try:
            ln = inbox.get(timeout=timeout)
        except queue.Empty:
            return None
        if ln is None:
            raise RuntimeError("exited")
        ln = ln.strip()
        if not ln.startswith("{"):
            return {}
        try:
            return json.loads(ln)
        except json.JSONDecodeError:
            return {}

    def wait(rid, timeout=90):
        end = time.time() + timeout
        while time.time() < end:
            o = next_obj(0.5)
            if not o:
                continue
            if o.get("id") == rid:
                if o.get("error"):
                    raise RuntimeError(str(o["error"])[:200])
                return o.get("result") or {}
        raise RuntimeError("no response")

    methods = {}
    try:
        send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
              "params": {"clientInfo": {"name": "capture", "version": "1.0"}}})
        wait(1)
        send({"jsonrpc": "2.0", "id": 2, "method": "thread/start",
              "params": {"cwd": HUB, "approvalPolicy": "never",
                         "sandboxPolicy": {"type": "dangerFullAccess"}}})
        res = wait(2, 90)
        tid = (res.get("thread") or {}).get("id") or res.get("threadId")
        print("[codex] thread", tid)
        send({"jsonrpc": "2.0", "id": 3, "method": "turn/start",
              "params": {"threadId": tid, "input": [{"type": "text", "text": PROMPT}],
                         "cwd": HUB, "approvalPolicy": "never",
                         "sandboxPolicy": {"type": "dangerFullAccess"}}})
        deadline = time.time() + 120
        while time.time() < deadline:
            o = next_obj(0.5)
            if not o:
                if proc.poll() is not None:
                    break
                continue
            m = o.get("method")
            if m:
                methods[m] = methods.get(m, 0) + 1
                if m in ("turn/completed", "turn/failed"):
                    print(f"  [{m}] params=",
                          json.dumps(o.get("params"), ensure_ascii=False)[:600])
                if m == "turn/completed":
                    break
    except Exception as exc:
        print("[codex] err:", type(exc).__name__, exc)
    finally:
        try:
            proc.kill()
        except Exception:
            pass
    dump("raw-codex.jsonl", lines)
    print("[codex] methods:", json.dumps(methods, ensure_ascii=False))


if __name__ == "__main__":
    which = sys.argv[1] if len(sys.argv) > 1 else "both"
    if which in ("both", "codebuddy"):
        capture_codebuddy()
    if which in ("both", "codex"):
        capture_codex()
