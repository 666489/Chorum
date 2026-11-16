"""Codex exec JSONL adapter; uses installed CLI/auth/config, no RPC handshake."""
import json
import time


def run(h, agent, prompt, session_id, on_text, cancel=None, on_proc=None,
        on_event=None, on_think=None, system_extra=None):
    cfg = h.cfg_agent(agent)
    sandbox, cwd = h.HUB.codex_turn_policy(agent)
    policy = {"readOnly": "read-only", "workspaceWrite": "workspace-write", "dangerFullAccess": "danger-full-access"}[sandbox["type"]]
    cmd = [h.resolve_command(h.cli_exe("codex")), "-a", "never",
           "-c", "sandbox_mode=" + json.dumps(policy)]
    if sandbox.get("networkAccess") is not None and policy == "workspace-write":
        cmd += ["-c", "sandbox_workspace_write.network_access=" + str(sandbox["networkAccess"]).lower()]
    roots = sandbox.get("writableRoots") or []
    if roots:
        cmd += ["-c", "sandbox_workspace_write.writable_roots=" + json.dumps(roots)]
    if cfg.get("model"):
        cmd += ["-m", str(cfg["model"])]
    if cfg.get("model_provider"):
        cmd += ["-c", "model_provider=" + json.dumps(cfg["model_provider"])]
    effort = cfg.get("effort")
    if effort:
        cmd += ["-c", "model_reasoning_effort=" + json.dumps(effort)]
    if cfg.get("web_search"):
        cmd += ["-c", 'web_search="live"']
    cmd += ["exec"]
    if session_id:
        cmd += ["resume", session_id]
    cmd += ["--json", "--skip-git-repo-check"]
    for image in h.HUB.current_images.get(agent) or []:
        cmd += ["--image", image]
    cmd += ["-"]
    state = {"session": session_id, "text": "", "completed": False, "error": None}
    diagnostics = []

    def emit(kind, data):
        if on_event:
            on_event(kind, data)

    def event(line):
        try:
            obj = json.loads(line)
        except (ValueError, TypeError):
            if line.strip():
                diagnostics.append(h.scrub_secrets(line)[:600])
                del diagnostics[:-8]
            return
        kind = obj.get("type")
        item = obj.get("item") or {}
        itype = item.get("type")
        if kind == "thread.started":
            state["session"] = obj.get("thread_id")
        elif kind == "turn.started":
            emit("llm_start", {"model": cfg.get("model")})
        elif kind in ("item.started", "item.updated", "item.completed"):
            if itype == "agent_message" and kind == "item.completed":
                # Commentary may precede final response. The last completed agent message wins.
                state["text"] = item.get("text") or ""
                if h.HUB.streaming_on(agent):
                    on_text(state["text"] + "\n")
            elif itype == "reasoning" and kind == "item.completed":
                if on_think:
                    on_think(item.get("text") or "")
            elif itype not in ("reasoning", "agent_message"):
                data = {"id": item.get("id"), "name": itype or "tool", "input": item}
                if kind == "item.started":
                    emit("tool_start", data)
                elif kind == "item.completed":
                    data.update({"output": item.get("aggregated_output") or json.dumps(item, ensure_ascii=False),
                                 "error": item.get("status") == "failed" or bool(item.get("exit_code"))})
                    emit("tool_end", data)
        elif kind == "turn.completed":
            u = obj.get("usage") or {}
            usage = {}
            if u.get("input_tokens") is not None:
                total = max(0, int(u["input_tokens"]))
                hit = u.get("cached_input_tokens")
                if hit is not None:
                    usage["cache_read_tokens"] = max(0, min(total, int(hit)))
                usage["in_tokens"] = total - usage.get("cache_read_tokens", 0)
            if u.get("output_tokens") is not None:
                usage["out_tokens"] = max(0, int(u["output_tokens"]))
            emit("llm_end", usage)
            state["completed"] = True
        elif kind in ("turn.failed", "error"):
            state["error"] = obj.get("error") or obj.get("message") or obj

    code, lines = h.stream_command(h.wrap_command(cmd), cwd, h.turn_timeout(agent), event,
                                  cancel, on_proc, input_text=prompt)
    if cancel is not None and cancel.is_set():
        raise h.Cancelled()
    if "[timeout]" in lines:
        raise RuntimeError("Codex CLI 执行超时；未自动重试，避免重复执行工具")
    if code or not state["completed"] or not state["text"].strip():
        detail = state["error"] or "\n".join(diagnostics) or "CLI 未返回 turn.completed 或最终正文"
        raise RuntimeError("Codex CLI 失败：" + h.scrub_secrets(str(detail))[:2000])
    return {"session_id": state["session"], "text": state["text"].strip()}
