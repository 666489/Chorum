"""TUI authentication regressions. Real local HTTP; no model or existing data."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import hub_tui as ui
import hub_connection as discovery


class AuthTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="clh-tui-auth-")
        self.root = Path(self.tmp.name)
        self.a, self.b = self.root / "client", self.root / "server with space"
        self.a.mkdir(); self.b.mkdir()
        self.runtime = self.root / "registry"
        self.env = patch.dict(os.environ, {"CLAUDELINK_RUNTIME_DIR":str(self.runtime), "CLAUDELINK_AUTO_DISCOVER":"1"})
        self.env.start()
        self.old_env_token = os.environ.pop("CLAUDELINK_TOKEN", None)
        self.token = "server-test-token"
        self.rotate = False
        self.effects = 0
        self.state_reads = 0
        parent = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_POST(self): self.do_GET()
            def do_GET(self):
                if self.command == "POST":
                    self.rfile.read(int(self.headers.get("Content-Length") or 0))
                if self.path == "/api/health":
                    return self.reply(200, {"running":[], "messages":0})
                if parent.rotate:
                    parent.rotate = False
                    parent.token = "rotated-test-token"
                    (parent.b / "hub.token").write_text(parent.token)
                if self.headers.get("X-ClaudeLink-Token") != parent.token:
                    return self.reply(401, {"error":"unauthorized"})
                if self.command == "POST":
                    parent.effects += 1
                if self.path == "/api/state":
                    parent.state_reads += 1
                    return self.reply(200, {"messages":[], "agents":[], "conversation":1, "health":{}, "perms":[]})
                return self.reply(200, {"ok":True, "context":"", "turns":[], "steps":{}, "workflows":[]})
            def reply(self, status, data):
                body = json.dumps(data).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        for directory in (self.a,self.b):
            (directory / "config.json").write_text(json.dumps({"server":{"host":"127.0.0.1","port":self.port}}))
        (self.a / "hub.token").write_text("wrong-test-token")
        (self.b / "hub.token").write_text(self.token)
        self.globals = patch.multiple(ui, DATA_DIR=str(self.a), CONFIG_PATH=str(self.a / "config.json"),
                                      TOKEN_PATH=str(self.a / "hub.token"), ROUNDTABLE_PATH=str(self.a / "roundtable.json"))
        self.globals.start()

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(timeout=2)
        self.globals.stop()
        if self.old_env_token is not None:
            os.environ["CLAUDELINK_TOKEN"] = self.old_env_token
        self.env.stop()
        self.tmp.cleanup()

    def client(self, directory=None):
        client = ui.Client("127.0.0.1", self.port)
        if directory:
            client.token_path = str(directory / "hub.token")
            client.token = ui.load_token(client.token_path)
        return client

    def test_read_token_again_after_rotation(self):
        client = self.client(self.b)
        self.token = "new-test-token"
        (self.b / "hub.token").write_text(self.token)
        self.assertTrue(client.get("/api/context")["ok"])

    def test_retry_rejected_post_once_without_duplicate_action(self):
        client = self.client(self.b)
        self.rotate = True
        self.assertTrue(client.post("/api/send", {"text":"test"})["ok"])
        self.assertEqual(self.effects, 1)

    def test_attach_uses_running_instances_directory_not_client_directory(self):
        discovery.register_instance("127.0.0.1", self.port, str(self.b), str(self.b / "config.json"))
        client = self.client()
        with patch.object(discovery, "_process_commands", return_value=[]):
            self.assertTrue(ui.ensure_hub(client, auto_start=False))
        self.assertEqual(Path(client.token_path), self.b / "hub.token")
        self.assertEqual(Path(ui.ROUNDTABLE_PATH), self.b / "roundtable.json")
        self.assertEqual((self.a / "hub.token").read_text(), "wrong-test-token")
        with self.assertRaises(urllib.error.HTTPError) as error:
            urllib.request.urlopen("http://127.0.0.1:" + str(self.port) + "/api/state")
        self.assertEqual(error.exception.code, 401)

    def test_attach_to_legacy_source_server_using_listener_argv(self):
        script = self.b / "hub.py"
        script.write_text("")
        command = '"' + sys.executable + '" "' + str(script) + '"'
        client = self.client()
        with patch.object(discovery, "_process_commands", return_value=[command]):
            self.assertTrue(ui.ensure_hub(client, auto_start=False))
        self.assertEqual(Path(client.token_path), self.b / "hub.token")

    def test_online_health_alone_is_not_success_and_does_not_start_another_server(self):
        client = self.client()
        with patch.object(discovery, "connection_candidates", return_value=[]), patch.object(ui,"start_hub") as start:
            with self.assertRaisesRegex(RuntimeError, "鉴权失败"):
                ui.ensure_hub(client)
        start.assert_not_called()

    def test_missing_token_before_auto_start_is_loaded_after_start(self):
        (self.b / "hub.token").unlink()
        client = self.client(self.b)
        with patch.object(client,"health",side_effect=[None, {}]), patch.object(ui,"start_hub",side_effect=lambda:(self.b / "hub.token").write_text(self.token)), patch.object(ui.time,"sleep"):
            self.assertTrue(ui.ensure_hub(client))

    def test_explicit_environment_token_is_not_silently_replaced(self):
        discovery.register_instance("127.0.0.1", self.port, str(self.b), str(self.b / "config.json"))
        with patch.dict(os.environ,{"CLAUDELINK_TOKEN":"explicit-wrong-test-token"}):
            with self.assertRaisesRegex(RuntimeError,"鉴权失败"):
                ui.ensure_hub(self.client(),auto_start=False)

    def test_render_once_reproduces_the_full_user_startup_path(self):
        discovery.register_instance("127.0.0.1", self.port, str(self.b), str(self.b / "config.json"))
        env = dict(os.environ, CLAUDELINK_HOME=str(self.a), CLAUDELINK_CONFIG=str(self.a / "config.json"))
        result = subprocess.run([sys.executable,str(ROOT / "hub_tui.py"),"--render-once","--no-start","--width","90","--height","24"],
                                capture_output=True,text=True,encoding="utf-8",timeout=20,env=env)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertIn("ClaudeLink",result.stdout)
        self.assertGreater(self.state_reads,0)
        self.assertNotIn(self.token,result.stdout + result.stderr)

    def test_unresolved_401_produces_error_without_traceback(self):
        env = dict(os.environ, CLAUDELINK_HOME=str(self.a), CLAUDELINK_CONFIG=str(self.a / "config.json"), CLAUDELINK_AUTO_DISCOVER="0")
        result = subprocess.run([sys.executable,str(ROOT / "hub_tui.py"),"--render-once","--no-start"],
                                capture_output=True,text=True,encoding="utf-8",timeout=10,env=env)
        self.assertEqual(result.returncode,2)
        self.assertIn("鉴权失败",result.stderr)
        self.assertNotIn("Traceback",result.stderr)
        checked = subprocess.run([sys.executable,str(ROOT / "hub_tui.py"),"--check"],capture_output=True,text=True,encoding="utf-8",timeout=10,env=env)
        self.assertNotEqual(checked.returncode,0)
        self.assertFalse(json.loads(checked.stdout)["authenticated"])

    def test_registry_cleanup_cannot_remove_replacement_instance(self):
        old = discovery.register_instance("127.0.0.1",self.port,str(self.a),str(self.a / "config.json"))
        new = discovery.register_instance("127.0.0.1",self.port,str(self.b),str(self.b / "config.json"))
        discovery.unregister_instance(old)
        self.assertTrue((self.runtime / (str(self.port) + ".json")).exists())
        discovery.unregister_instance(new)
        self.assertFalse((self.runtime / (str(self.port) + ".json")).exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
