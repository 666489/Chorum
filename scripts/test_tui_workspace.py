import json
from pathlib import Path
import sys
import unittest
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import hub_tui as ui


class Client:
    host="127.0.0.1";port=8787
    def __init__(self):self.posts=[]
    def get(self,path,**kw):
        if path.startswith("/api/model-choices"):return {"choices":[{"value":"existing-model","label":"Existing Model"}]}
        if path=="/api/capabilities":return {"effort":{"codex":["","low","medium","high"]}}
        if path.startswith("/api/memory"):return {"memories":[{"kind":"long","source":"user","text":"可复用经验"}]}
        return {}
    def post(self,path,body,**kw):self.posts.append((path,body));return {"ok":True}


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.client=Client();self.app=ui.App(self.client)
        self.app.agents={"codex":{"name":"codex","label":"Codex","model":"old-model","effort":"low","enabled":True,"kind":"cli"}}
        self.app.agent_order=["codex"];self.app.labels={"codex":"Codex"};self.app.server_version="0.4.0";self.app.connected=True
    def text(self):return ui.strip_ansi(ui.render_frame(self.app,120,32))
    def test_visible_workspace_contains_sidebar_and_keyboard_controls(self):
        text=self.text()
        for value in ("ClaudeLink 0.4.0","协作工作台","成员与模型","old-model","effort: low","F2 模型","F3 effort"):self.assertIn(value,text)
    def test_model_picker_accepts_free_model_without_touching_draft(self):
        self.app.editor.insert("未发送的原始目标")
        for key in ("f2","enter","enter"):
            self.app.handle_key(key)
        for c in "custom-model-42":self.app.handle_key(("char",c))
        self.app.handle_key("enter")
        self.assertEqual(self.client.posts[-1],("/api/agents/model",{"agent":"codex","model":"custom-model-42"}))
        self.assertEqual(self.app.editor.text(),"未发送的原始目标")
    def test_effort_picker_uses_supported_values(self):
        self.app.handle_key("f3");self.app.handle_key("enter")
        for _ in range(3):self.app.handle_key("down")
        self.app.handle_key("enter")
        self.assertEqual(self.client.posts[-1],("/api/agents/effort",{"agent":"codex","effort":"high"}))
    def test_work_picker_keeps_multiline_paste_until_explicit_enter(self):
        self.app.handle_key("f6");self.app.handle_key("enter");self.app.handle_key("paste_start")
        for c in "目标一":self.app.handle_key(("char",c))
        self.app.handle_key("enter")
        for c in "验收二":self.app.handle_key(("char",c))
        self.assertFalse(self.client.posts)
        self.app.handle_key("paste_end");self.app.handle_key("enter")
        self.assertEqual(self.client.posts[-1][1]["topic"],"目标一\n验收二")
    def test_memory_view_and_view_cycle_do_not_crash(self):
        self.app.handle_key("f7");self.assertIn("可复用经验",self.text())
        self.app.handle_key("ctrl_o");self.assertEqual(self.app.view,"messages")
    def test_escape_cancels_menu_without_applying_model(self):
        self.app.handle_key("f2");self.app.handle_key("esc")
        self.assertIsNone(self.app.menu);self.assertFalse(self.client.posts)
    def test_vt_function_keys_and_modified_arrows_are_not_typed_into_prompt(self):
        self.assertEqual(ui.parse_vt_keys("\x1bOQ\x1bOR\x1b[1;5D\x1b[1;5C\x1b[5~\x1b[6~\t"),["f2","f3","edit_left","edit_right","page_up","page_down","tab"])
    def test_both_narrow_and_wide_menu_are_visible(self):
        self.app.handle_key("f2")
        for cols,rows in ((60,16),(80,24),(120,32)):
            self.assertIn("选择要切换模型的成员",ui.strip_ansi(ui.render_frame(self.app,cols,rows)))

if __name__=="__main__":unittest.main(verbosity=2)
