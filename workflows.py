"""One planner invocation, bounded persistent workers, resumable checkpoints."""
import concurrent.futures
import json
import re
import threading
import time
import uuid


def json_object(text):
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", str(text).strip())
    value = json.loads(text)
    if not isinstance(value, dict):
        raise ValueError("expected a JSON object")
    return value


def validate_plan(plan, agents):
    tasks = plan.get("tasks")
    if not isinstance(tasks, list) or not 1 <= len(tasks) <= 24:
        raise ValueError("planner must return 1..24 tasks")
    known, checked = set(), []
    for t in tasks:
        tid = str(t.get("id") or "")
        deps = t.get("depends_on") or []
        if not tid or tid in known or t.get("agent") not in agents or not str(t.get("instruction") or "").strip():
            raise ValueError("invalid task id, agent or instruction")
        if not isinstance(deps, list) or any(d not in known for d in deps):
            raise ValueError("dependencies must refer to earlier tasks; cycles are forbidden")
        known.add(tid)
        checked.append({"id": tid, "agent": t["agent"], "instruction": str(t["instruction"])[:6000],
                        "depends_on": deps, "status": "pending", "steps": 0, "summary": "", "error": ""})
    return checked


class Workflows:
    def __init__(self, hub):
        self.hub = hub
        self.lock = threading.RLock()
        self.stops = {}
        self.active = set()
        self.tickets = {}
        # Crash recovery changes only in-flight work; completed checkpoints stay completed.
        for item in self.list():
            if item["status"] in ("planning", "running", "stopping"):
                item["status"] = "paused"
                item["error"] = "进程重启；保留已完成任务，恢复后继续"
                for task in item.get("tasks", []):
                    if task["status"] == "running":
                        task["status"] = "pending"
                self.save(item)

    def list(self):
        raw = self.hub.store.state_get("workflows", "[]")
        return json.loads(raw or "[]")

    def save(self, item):
        with self.lock:
            items = [r for r in self.list() if r["id"] != item["id"]]
            items.append(item)
            # Keep every active workflow and the 40 most recent terminal records.
            active = [r for r in items if r["status"] not in ("done", "error", "cancelled")]
            terminal = [r for r in items if r["status"] in ("done", "error", "cancelled")][-40:]
            self.hub.store.state_set("workflows", json.dumps(active + terminal, ensure_ascii=False))
        self.hub.broadcast({"type": "workflow", "workflow": item})

    def start(self, topic, planner, agents, max_steps=4):
        if not str(topic).strip():
            raise ValueError("任务主题不能为空")
        available = {d["name"] for d in self.hub.agent_defs() if d.get("enabled")}
        if planner not in available or not agents or any(a not in available for a in agents):
            raise ValueError("主代理和执行代理必须是启用的成员")
        conv = self.hub.store.active_conv()
        with self.lock:
            if any(r["conversation"] == conv and r["status"] in ("planning", "running", "stopping") for r in self.list()):
                raise ValueError("当前会话已有执行中的任务")
            item = {"id": uuid.uuid4().hex[:12], "conversation": conv, "topic": str(topic)[:16000],
                    "planner": planner, "agents": list(dict.fromkeys(agents)), "tasks": [],
                    "max_steps": max(1, min(20, int(max_steps))), "status": "planning", "error": "", "created": time.time()}
            self.save(item)
            self.launch(item, planning=True)
        return item

    def launch(self, item, planning=False):
        with self.lock:
            if item["id"] in self.active:
                raise ValueError("该任务仍在收尾")
            stop = threading.Event()
            self.stops[item["id"]] = stop
            self.active.add(item["id"])
        threading.Thread(target=self.run, args=(item, stop, planning), daemon=True).start()

    def stop(self, key=None):
        with self.lock:
            for wid, flag in self.stops.items():
                if key is None or wid == key:
                    flag.set()
                    for ticket in list(self.tickets.get(wid, [])):
                        self.hub.cancel_job(ticket["qid"])

    def resume(self, key):
        with self.lock:
            item = next((r for r in self.list() if r["id"] == key), None)
            if not item or item["status"] not in ("paused", "error"):
                raise ValueError("只能恢复暂停或失败的任务")
            if not item["tasks"]:
                raise ValueError("调度计划尚未持久化；需要新建任务，避免隐式重复调度")
            for task in item["tasks"]:
                if task["status"] in ("running", "error", "blocked", "exhausted"):
                    task["status"], task["steps"], task["error"] = "pending", 0, ""
            item["status"], item["error"] = "running", ""
            self.launch(item)
            self.save(item)
        return item

    def call(self, item, agent, text, kind, stop):
        if stop.is_set():
            return None
        if self.hub.store.active_conv() != item["conversation"]:
            stop.set()
            return None
        allowed, reason = self.hub.admit(agent, item["conversation"])
        if not allowed:
            raise RuntimeError(reason)
        ticket = self.hub.job_submit(agent, text, "任务调度", agent, 0,
                                    conv_id=item["conversation"], trigger_kind=kind, wait=True)
        with self.lock:
            self.tickets.setdefault(item["id"], []).append(ticket)
            if stop.is_set():
                self.hub.cancel_job(ticket["qid"])
        try:
            return self.hub.job_wait(ticket)
        finally:
            with self.lock:
                self.tickets[item["id"]].remove(ticket)

    def worker(self, item, task, stop):
        try:
            for _ in range(item["max_steps"] - task["steps"]):
                if stop.is_set():
                    task["status"] = "pending"
                    return
                deps = [{"id": t["id"], "summary": t["summary"]} for t in item["tasks"] if t["id"] in task["depends_on"]]
                prompt = ("[持续执行任务]\n原始目标：" + item["topic"] + "\n你的任务：" + task["instruction"] +
                          "\n依赖结果：" + json.dumps(deps, ensure_ascii=False) + "\n你的检查点：" + task["summary"] +
                          "\n执行一个可验证步骤。只输出 JSON：{\"status\":\"done|continue|blocked\",\"summary\":\"结果和依据\",\"next\":\"下一步\"}。"
                          "完成必须说明实际结果；缺少材料就 blocked，禁止把计划当完成。")
                raw = self.call(item, task["agent"], prompt, "worker", stop)
                if raw is None:
                    task["status"] = "pending" if stop.is_set() else "error"
                    task["error"] = "执行被中断或成员调用失败"
                    return
                result = json_object(raw)
                status = result.get("status")
                if status not in ("done", "continue", "blocked") or not str(result.get("summary") or "").strip():
                    raise ValueError("worker must return status and nonempty summary")
                task["steps"] += 1
                task["summary"] = str(result["summary"])[:3000] + ("\n下一步：" + str(result["next"])[:1000] if result.get("next") else "")
                task["status"] = "running" if status == "continue" else status
                self.save(item)
                self.hub.memory.put(self.hub.project_key(), item["conversation"], "short", task["id"] + ": " + task["summary"], "workflow:" + item["id"] + ";worker-reported")
                if status != "continue":
                    return
            task["status"] = "exhausted"
            task["error"] = "达到执行步数上限，尚未完成"
        except Exception as exc:
            task["status"], task["error"] = "error", str(exc)[:2000]
        finally:
            self.save(item)

    def run(self, item, stop, planning=False):
        try:
            if planning:
                prompt = ("原始目标：" + item["topic"] + "\n只调度一次，不执行任务。把工作拆成可独立完成的任务，按依赖拓扑顺序排列。"
                          "可用执行代理：" + ",".join(item["agents"]) +
                          '\n只输出 JSON：{"tasks":[{"id":"t1","agent":"成员id","instruction":"任务与验收结果","depends_on":[]}]}')
                raw = self.call(item, item["planner"], prompt, "planner", stop)
                if raw is None:
                    raise RuntimeError("调度未完成，未启动执行代理")
                item["tasks"] = validate_plan(json_object(raw), item["agents"])
            item["status"] = "running"
            self.save(item)
            with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
                futures = {}
                while not stop.is_set():
                    busy = {t["agent"] for t in futures.values()}
                    done_ids = {t["id"] for t in item["tasks"] if t["status"] == "done"}
                    for task in item["tasks"]:
                        if task["status"] == "pending" and task["agent"] not in busy and set(task["depends_on"]) <= done_ids:
                            task["status"] = "running"
                            busy.add(task["agent"])
                            futures[pool.submit(self.worker, item, task, stop)] = task
                    self.save(item)
                    if not futures:
                        break
                    finished, _ = concurrent.futures.wait(futures, timeout=0.3, return_when=concurrent.futures.FIRST_COMPLETED)
                    for future in finished:
                        future.result()
                        del futures[future]
            item["status"] = "paused" if stop.is_set() else ("done" if all(t["status"] == "done" for t in item["tasks"]) else "error")
            if item["status"] == "error":
                item["error"] = "存在失败、阻塞或达到步数上限的子任务；未判定完成"
        except Exception as exc:
            item["status"] = "paused" if stop.is_set() else "error"
            item["error"] = str(exc)[:2000]
        finally:
            self.save(item)
            with self.lock:
                self.active.discard(item["id"])
                self.stops.pop(item["id"], None)
                self.tickets.pop(item["id"], None)
