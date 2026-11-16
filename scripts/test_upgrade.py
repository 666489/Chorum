"""Behavior regressions for CLI transport, tools, memory, HTTP routes and execution."""
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
import faulthandler
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
TMP = tempfile.mkdtemp(prefix="clh-upgrade-")
os.environ.update(CLAUDELINK_HOME=TMP, CLAUDELINK_DB=str(Path(TMP) / "hub.db"), CLAUDELINK_CONFIG=str(Path(TMP) / "config.json"))
Path(os.environ["CLAUDELINK_CONFIG"]).write_text(json.dumps({
    "server": {"host": "127.0.0.1", "auth": False}, "workspace": {"cwd": TMP},
    "agents": {"codex": {"enabled": True}, "claude": {"enabled": True}},
    "permissions": {"default_mode": "read-only"}, "relay": {"enabled": False},
    "context": {"history_limit": 6, "max_history_chars": 500},
    "queue": {"max_parallel": 2}, "claudelink": {"mirror": False},
}), encoding="utf-8")
import hub
faulthandler.dump_traceback_later(18, exit=True)
import codex_cli
import hub_tui
from runtime import Memory, ToolRuntime, api_tool_loop, bounded_history
from workflows import validate_plan, Workflows


class UpgradeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = hub.HubHTTPServer(("127.0.0.1", 0), hub.Handler)
        cls.port = cls.server.server_address[1]
        hub.CONFIG["server"]["port"] = cls.port
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def http(self, path, body=None):
        req = urllib.request.Request("http://127.0.0.1:" + str(self.port) + path,
              data=json.dumps(body).encode() if body is not None else None,
              headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=6) as response:
            return json.load(response)

    def test_restored_routes_and_effort(self):
        self.assertTrue(self.http("/api/dispatch", {"mode": "parallel"})["ok"])
        self.assertTrue(self.http("/api/agents/model", {"agent": "codex", "model": "test-model"})["ok"])
        self.assertTrue(self.http("/api/agents/effort", {"agent": "codex", "effort": "low"})["ok"])
        self.assertEqual(hub.cfg_agent("codex")["effort"], "low")
        self.assertTrue(self.http("/api/context", {"text": "test context"})["ok"])
        self.assertTrue(self.http("/api/stop", {"disable_relay": False})["ok"])
        self.assertTrue(self.http("/api/activity")["ok"])
        stored = self.http("/api/memory", {"kind":"short", "text":"http memory", "source":"test"})
        self.assertTrue(stored["ok"])
        self.assertTrue(self.http("/api/memory?q=http")["memories"])
        self.assertTrue(self.http("/api/memory", {"action":"delete", "id":stored["id"]})["ok"])
        result = self.http("/mcp", {"jsonrpc":"2.0", "id":1, "method":"tools/call", "params":{"name":"read_inbox", "arguments":{"agent":"codex"}}})
        self.assertIn("result", result)
        with self.assertRaises(urllib.error.HTTPError) as err:
            self.http("/api/no-such-route", {})
        self.assertEqual(err.exception.code, 404)

    def test_memory_scope_dedup_and_restart(self):
        path = str(Path(TMP) / "memory-test.db")
        mem = Memory(path)
        key = mem.put("p", 1, "long", "Codex uses CLI", "user-confirmed")
        self.assertEqual(key, mem.put("p", 1, "long", "Codex uses CLI", "user-confirmed"))
        mem.put("p", 1, "short", "working note", "task:1")
        self.assertEqual(len(mem.search("p", 2)), 1)
        self.assertEqual(len(mem.search("q", 1)), 0)
        self.assertEqual(len(Memory(path).search("p", 1)), 2)
        self.assertTrue(mem.remove("p", key))
        self.assertFalse(mem.remove("q", key))
        mem.clear_short("p", 1)
        self.assertEqual(mem.search("p", 1), [])

    def test_tool_path_grants_and_sensitive_files(self):
        root = Path(TMP) / "tools"
        root.mkdir(exist_ok=True)
        (root / "paper.md").write_text("actual material", encoding="utf-8")
        (root / ".env").write_text("secret", encoding="utf-8")
        rt = ToolRuntime(hub.HUB.memory, "p", 1, [str(root)])
        self.assertEqual(rt.call("read_file", {"path": "paper.md"})["text"], "actual material")
        for value in (".env", "../config.json", "../../outside.txt"):
            with self.assertRaises(ValueError):
                rt.call("read_file", {"path": value})
        with self.assertRaises(ValueError):
            rt.call("web_fetch", {"url": "http://127.0.0.1/private"})

    def test_openai_real_tool_roundtrip_and_effort(self):
        payloads, events, output = [], [], []
        rt = ToolRuntime(hub.HUB.memory, "p", 1, [TMP])
        replies = [
            {"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [{"id":"c1", "type":"function", "function":{"name":"memory_search", "arguments":'{"query":"test"}'}}]}}], "_usage":{"in_tokens":10,"out_tokens":3}},
            {"choices": [{"message": {"role": "assistant", "content": "verified final"}}], "_usage":{"in_tokens":14,"out_tokens":4}},
        ]
        def transport(payload):
            payloads.append(payload)
            return replies.pop(0)
        common = {"style":"openai","model":"test","messages":[{"role":"user","content":"x"}],"system":"s","max_tokens":100}
        text = api_tool_loop(common, transport, rt, lambda k,p: events.append((k,p)), output.append, None, None, effort="low")
        self.assertEqual(text, "verified final")
        self.assertEqual(payloads[0]["reasoning_effort"], "low")
        self.assertEqual(payloads[1]["messages"][-1]["role"], "tool")
        self.assertEqual([k for k,p in events].count("tool_start"), 1)
        self.assertEqual([k for k,p in events].count("llm_end"), 2)

    def test_anthropic_tool_errors_are_visible(self):
        rt = ToolRuntime(hub.HUB.memory, "p", 1, [TMP])
        payloads, events = [], []
        replies = [{"content":[{"type":"tool_use","id":"c","name":"read_file","input":{"path":"../outside.txt"}}]}, {"content":[{"type":"text","text":"cannot read"}]}]
        def transport(payload):
            payloads.append(payload)
            return replies.pop(0)
        common = {"style":"anthropic","model":"test","messages":[],"max_tokens":100}
        api_tool_loop(common, transport, rt, lambda k,p: events.append((k,p)), lambda _: None, None, None)
        self.assertTrue(payloads[1]["messages"][-1]["content"][0]["is_error"])
        self.assertTrue(next(p for k,p in events if k == "tool_end")["error"])

    def test_codex_cli_events_and_cache_truth(self):
        events, commands, text = [], [], []
        def stream(cmd, cwd, timeout, callback, *args, **kwargs):
            commands.append((cmd, kwargs))
            for event in [
                {"type":"thread.started", "thread_id":"sid"}, {"type":"turn.started"},
                {"type":"item.started", "item":{"id":"c","type":"command_execution","command":"echo ok"}},
                {"type":"item.completed", "item":{"id":"c","type":"command_execution","aggregated_output":"ok","exit_code":0}},
                {"type":"item.completed", "item":{"type":"reasoning","text":"visible summary"}},
                {"type":"item.completed", "item":{"type":"agent_message","text":"done"}},
                {"type":"turn.completed", "usage":{"input_tokens":100,"cached_input_tokens":40,"output_tokens":9}},
            ]:
                callback(json.dumps(event))
            return 0, []
        with patch.object(hub, "stream_command", stream), patch.object(hub, "wrap_command", lambda x:x):
            result = hub.run_codex("codex", "中文 long prompt", "old-sid", text.append, on_event=lambda k,p: events.append((k,p)))
        self.assertEqual(result["text"], "done")
        self.assertIn("resume", commands[0][0])
        self.assertNotIn("中文 long prompt", commands[0][0])
        usage = next(p for k,p in events if k == "llm_end")
        self.assertEqual(usage, {"in_tokens":60,"cache_read_tokens":40,"out_tokens":9})
        def incomplete(cmd, cwd, timeout, callback, *args, **kw):
            callback(json.dumps({"type":"item.completed","item":{"type":"agent_message","text":"partial"}}))
            return 0, []
        with patch.object(hub, "stream_command", incomplete):
            with self.assertRaises(RuntimeError):
                hub.run_codex("codex", "x", None, text.append)

    @unittest.skipUnless(os.name == "nt", "Windows shim regression")
    def test_codex_shim_never_launches_claude(self):
        root = Path(TMP) / "shim-test"
        claude = root / "node_modules/@anthropic-ai/claude-code/bin/claude.exe"
        claude.parent.mkdir(parents=True, exist_ok=True)
        claude.write_bytes(b"not executable; path selection only")
        shim = root / "codex.cmd"
        shim.write_text("@echo off", encoding="utf-8")
        wrapped = hub.wrap_command([str(shim), "--version"])
        self.assertNotIn(str(claude), wrapped)
        codex = root / "node_modules/@openai/codex/bin/codex.js"
        codex.parent.mkdir(parents=True, exist_ok=True)
        codex.write_text("", encoding="utf-8")
        wrapped = hub.wrap_command([str(shim), "--version"])
        self.assertEqual(wrapped[1], str(codex))

    def test_unknown_cache_is_not_displayed_as_zero_hit_rate(self):
        H = hub.HUB
        conv = H.store.create_conversation("unknown cache")["id"]
        sink = hub.TraceSink(H, "Codex", conv_id=conv)
        sink.on_event("llm_start", {"model":"test"})
        sink.on_event("llm_end", {"in_tokens":100, "out_tokens":10})
        sink.close("ok", end_ts=hub.now_ms(), in_chars=20, out_chars=10)
        self.assertIsNone(H.store.cache_observation(conv)["cache_hit_rate"])
        sink = hub.TraceSink(H, "Codex", conv_id=conv)
        sink.on_event("llm_start", {"model":"test"})
        sink.on_event("llm_end", {"in_tokens":60, "cache_read_tokens":40, "out_tokens":10})
        sink.close("ok", end_ts=hub.now_ms(), in_chars=20, out_chars=10)
        report = H.store.cache_observation(conv)
        self.assertEqual(report["cache_hit_rate"], .4)
        self.assertEqual(report["cache_unreported_calls"], 1)

    def test_tool_completion_keeps_input_and_does_not_close_another_call(self):
        H=hub.HUB
        sink=hub.TraceSink(H,"Codex")
        sink.on_event("llm_start",{"model":"test"})
        llm=sink.open_steps["llm"]
        sink.on_event("tool_start",{"id":"t","name":"read_file","input":{"path":"paper.md"}})
        tid=sink.open_steps["tool:t"]
        sink.on_event("tool_end",{"id":"t","name":"read_file","output":"material"})
        detail=json.loads(H.store.get_step(tid)["detail"])
        self.assertEqual(detail["path"],"paper.md")
        self.assertEqual(detail["output"],"material")
        sink.on_event("tool_end",{"id":"unknown","name":"search","output":"result"})
        self.assertEqual(sink.open_steps["llm"],llm)
        sink.on_event("llm_end",{"in_tokens":5,"out_tokens":2})
        self.assertEqual(H.store.get_step(llm)["in_tokens"],5)
        sink.close("ok",end_ts=hub.now_ms(),in_chars=0,out_chars=0)

    def test_activity_does_not_leak_other_conversations(self):
        H = hub.HUB
        conv = H.store.create_conversation("private activity")["id"]
        sink = hub.TraceSink(H, "Codex", conv_id=conv)
        sink.on_event("thinking", {"text":"private-other-conversation"})
        sink.close("ok", end_ts=hub.now_ms(), in_chars=0, out_chars=0)
        data = self.http("/api/activity")
        self.assertNotIn(sink.id, {t["id"] for t in data["turns"]})

    def test_stream_cancellation_can_interrupt_blocked_stdin(self):
        cancel = threading.Event()
        def ignore_stdin():
            time.sleep(.2)
            cancel.set()
        threading.Thread(target=ignore_stdin, daemon=True).start()
        started = time.monotonic()
        code, lines = hub.stream_command([sys.executable, "-c", "import time; time.sleep(20)"], TMP, 3,
                                         lambda _:None, cancel, input_text="x" * 1000000)
        self.assertIn("[cancelled]", lines)
        self.assertLess(time.monotonic() - started, 5)

    def test_context_bounded_and_current_message_not_duplicated(self):
        rows = [{"text":"a" * 300}, {"text":"b" * 300}]
        self.assertEqual(len(bounded_history(rows, 500)), 1)
        conv = hub.HUB.store.create_conversation("context")["id"]
        hub.HUB.post("user", "all", "user", "original objective", conv_id=conv)
        hub.HUB.frozen_prompt("codex", conv, "Codex", [], "", "user", "original objective")
        hub.HUB.post("user", "all", "user", "latest request", conv_id=conv)
        prompt = hub.HUB.frozen_prompt("codex", conv, "Codex", [], "", "user", "latest request")
        self.assertNotIn("latest request", prompt)
        self.assertIn("original objective", prompt)

    def test_tui_full_thinking_and_model_commands(self):
        class Client:
            def __init__(self): self.posts = []; self.host = "127.0.0.1"; self.port = 1
            def post(self, path, body): self.posts.append((path, body)); return {"ok":True}
        client = Client()
        app = hub_tui.App(client)
        app.agent_order = ["codex"]
        app.labels = {"codex":"Codex"}
        app.agents = {"codex":{"enabled":True, "model":"m"}}
        detail = "BEGIN\n" + "reasoning line\n" * 600 + "END"
        app.apply_event({"type":"step", "step":{"id":"s", "turn_id":"t", "kind":"thinking", "status":"ok", "detail":json.dumps({"text":detail})}})
        app.command("/view thinking")
        flat = "\n".join(t for t,c in app.flat_lines(90))
        self.assertIn("BEGIN", flat)
        self.assertIn("END", flat)
        app.command("/model 1 custom-model")
        app.command("/effort 1 high")
        self.assertEqual(client.posts[-1], ("/api/agents/effort", {"agent":"codex", "effort":"high"}))
        frame = hub_tui.strip_ansi(hub_tui.render_frame(app, 90, 24))
        self.assertIn("END", frame)
        app.scroll = 600
        frame = hub_tui.strip_ansi(hub_tui.render_frame(app, 90, 24))
        self.assertNotIn("END", frame)

    def test_workflow_one_plan_dependencies_and_continuation(self):
        H = hub.HUB
        conv = H.store.create_conversation("workflow")["id"]
        H.store.set_active_conv(conv)
        calls = []
        def runner(agent, text, sender, target, depth, attachments=None, **kw):
            calls.append((agent, kw.get("trigger_kind")))
            if kw.get("trigger_kind") == "planner":
                return json.dumps({"tasks":[{"id":"t1","agent":"codex","instruction":"first","depends_on":[]},{"id":"t2","agent":"claude","instruction":"second","depends_on":["t1"]}]})
            count = sum(1 for a,k in calls if a == agent and k == "worker")
            return json.dumps({"status":"continue" if agent == "codex" and count == 1 else "done", "summary":"actual checkpoint", "next":"next"})
        with patch.object(H, "_run_agent", runner):
            item = H.workflows.start("original objective", "claude", ["codex","claude"])
            end = time.time() + 8
            while item["id"] in H.workflows.active and time.time() < end:
                time.sleep(0.05)
        saved = next(w for w in H.workflows.list() if w["id"] == item["id"])
        self.assertEqual(saved["status"], "done", saved)
        self.assertEqual(sum(k == "planner" for a,k in calls), 1)
        self.assertEqual(calls[-1], ("claude", "worker"))
        self.assertEqual(saved["tasks"][0]["steps"], 2)
        recovered = Workflows(H)
        self.assertEqual(next(w for w in recovered.list() if w["id"] == item["id"])["status"], "done")
        with self.assertRaises(ValueError):
            validate_plan({"tasks":[{"id":"x","agent":"codex","instruction":"x","depends_on":["x"]}]}, ["codex"])

    def test_native_session_receives_only_new_external_messages(self):
        H = hub.HUB
        conv = H.store.create_conversation("native delta")["id"]
        H.store.set_active_conv(conv)
        H.store.state_set("native_memory:codex", "1")
        prompts = []
        def runner(agent, prompt, session, on_line, *args, **kwargs):
            prompts.append(prompt)
            if len(prompts) == 1:
                H.post("Other", "all", "reply", "message_during_execution", conv_id=conv)
            return {"session_id":"native-session", "text":"own_reply_already_in_native_context"}
        try:
            with patch.object(H, "_runner_for", return_value=(runner, None)), patch.object(H, "_maybe_relay"):
                H.post("user", "codex", "user", "first_unique_request", conv_id=conv)
                H._run_agent("codex", "first_unique_request", "user", "codex", 0, conv_id=conv)
                H.post("user", "codex", "user", "second_unique_request", conv_id=conv)
                H._run_agent("codex", "second_unique_request", "user", "codex", 0, conv_id=conv)
            self.assertIn("message_during_execution", prompts[1])
            self.assertNotIn("first_unique_request", prompts[1])
            self.assertNotIn("own_reply_already_in_native_context", prompts[1])
            self.assertEqual(prompts[1].count("second_unique_request"), 1)
        finally:
            H.store.state_set("native_memory:codex", "0")

    def test_blocked_workflow_resumes_without_replanning(self):
        H = hub.HUB
        conv = H.store.create_conversation("blocked workflow")["id"]
        H.store.set_active_conv(conv)
        planned, worked = [], []
        def runner(agent, text, sender, target, depth, attachments=None, **kw):
            if kw.get("trigger_kind") == "planner":
                planned.append(True)
                return json.dumps({"tasks":[{"id":"t","agent":"codex","instruction":"verify","depends_on":[]}]})
            worked.append(True)
            return json.dumps({"status":"blocked" if len(worked) == 1 else "done", "summary":"source unavailable" if len(worked) == 1 else "result available"})
        def wait(item):
            end = time.time() + 5
            while item["id"] in H.workflows.active and time.time() < end:
                time.sleep(.03)
            self.assertNotIn(item["id"], H.workflows.active)
        with patch.object(H, "_run_agent", runner):
            item = H.workflows.start("goal", "claude", ["codex"])
            wait(item)
            self.assertEqual(item["status"], "error")
            resumed = H.workflows.resume(item["id"])
            wait(resumed)
        self.assertEqual(resumed["status"], "done")
        self.assertEqual(len(planned), 1)

    def test_queue_refusal_and_cancel_release_waiters(self):
        H = hub.HUB
        with patch.object(H, "admit", return_value=(False, "budget exhausted")):
            ticket = H.job_submit("codex", "no budget", "user", "codex", 0, wait=True)
            self.assertTrue(ticket["event"].wait(4))
            self.assertIn("budget", ticket["box"]["error"])
        ticket = {"qid":-123, "event":threading.Event(), "box":{}}
        H.sched_waiters[-123] = ticket
        with patch.object(H.store, "queue_cancel", return_value="cancelled"):
            H.cancel_job(ticket["qid"])
        self.assertTrue(ticket["event"].wait(4))


if __name__ == "__main__":
    unittest.main(verbosity=2)
