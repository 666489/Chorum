"""Deterministic provider for real-browser/terminal acceptance. Never shipped."""
import json
import os
from pathlib import Path
import sys
import time
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import hub


def fixture_runner(agent,prompt,session,on_text,cancel=None,on_proc=None,on_event=None,on_think=None,system_extra=None):
    if on_event:on_event("llm_start",{"model":"fixture-small"})
    if on_think:on_think("先核对原始目标，再读取验证材料。这是测试服务返回的可见推理摘要。")
    material=Path(hub.DATA_DIR)/"fixture-result.txt"
    material.write_text("本地测试工件已生成。",encoding="utf-8")
    if on_event:on_event("tool_start",{"id":"fixture-read","name":"read_file","input":{"path":str(material)}})
    if on_event:on_event("tool_end",{"id":"fixture-read","name":"read_file","output":material.read_text(encoding="utf-8"),"error":False})
    time.sleep(.15)
    if "只调度一次，不执行任务" in prompt:
        text=json.dumps({"tasks":[{"id":"t1","agent":"codex","instruction":"读取本地验证工件","depends_on":[]}]},ensure_ascii=False)
    elif "[持续执行任务]" in prompt:
        text=json.dumps({"status":"done","summary":"fixture-result.txt 已生成并读取。","next":""},ensure_ascii=False)
    else:
        text="独立结论：讨论应持续锚定原始目标，并用材料与反例推进。当前消息是确定性界面验证，不代表真实模型推理。"
    on_text(text)
    if on_event:on_event("llm_end",{"in_tokens":100,"out_tokens":20})
    return {"session_id":"fixture-session","text":text}

hub.RUNNERS["codex"]=fixture_runner
if os.environ.get("CLAUDELINK_FIXTURE_VERSION"):
    hub.APP_VERSION=os.environ["CLAUDELINK_FIXTURE_VERSION"]
if os.environ.get("CLAUDELINK_FIXTURE_REVISION"):
    hub.CODE_REVISION=os.environ["CLAUDELINK_FIXTURE_REVISION"]
if os.environ.get("CLAUDELINK_FIXTURE_BUSY"):
    original_health=hub.HUB.health
    def busy_health():
        result=original_health()
        result["running"]=[{"agent":"codex","started":time.time(),"label":"fixture active"}]
        return result
    hub.HUB.health=busy_health
hub.HUB.model_choices=lambda agent:{"current":{"model":hub.cfg_agent(agent).get("model") or "fixture-small"},"choices":[{"value":"fixture-small","label":"Fixture Small"},{"value":"fixture-large","label":"Fixture Large"}]}
hub.main()
