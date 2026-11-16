#!/usr/bin/env node
"use strict";
/**
 * 测试用的假模型服务（OpenAI 兼容 + Anthropic 兼容，同一个进程）。
 *
 * 用途：让端到端测试真的走一遍「中枢 → provider」的 HTTP，而不花真钱。
 * 每个协议各自计数：第 1 次请求返回一个工具调用，第 2 次返回正文。
 *
 *   node scripts/mock-provider.js [--port 18999]
 *   GET  /__state    → {calls, openai:{step,bodies}, anthropic:{step,bodies}}
 *   GET  /__reset    → 计数清零
 *   POST /v1/chat/completions | /v1/messages
 *
 * 注意：它必须在**独立进程**里跑。测试用 spawnSync 调 CLI，如果 mock 和测试同进程，
 * 事件循环被阻塞，mock 永远不会响应（第一次写这个测试时就是这么挂住的）。
 */
const http = require("http");

const args = process.argv.slice(2);
const portAt = args.indexOf("--port");
const PORT = Number(portAt >= 0 ? args[portAt + 1] : 0) || 18999;

const state = {
  openai: { step: 0, bodies: [] },
  anthropic: { step: 0, bodies: [] },
};

function openAiReply(step) {
  if (step === 0) {
    return {
      choices: [{
        index: 0, finish_reason: "tool_calls",
        message: {
          role: "assistant", content: null,
          tool_calls: [{ id: "call_1", type: "function", function: { name: "list_files", arguments: JSON.stringify({ path: "." }) } }],
        },
      }],
      usage: { prompt_tokens: 100, completion_tokens: 20 },
    };
  }
  return {
    choices: [{ index: 0, finish_reason: "stop", message: { role: "assistant", content: "E2E_OPENAI_OK 目录已读取。" } }],
    usage: { prompt_tokens: 200, completion_tokens: 30 },
  };
}

function anthropicReply(step) {
  if (step === 0) {
    return {
      type: "message", role: "assistant",
      content: [
        { type: "text", text: "我先看一眼目录。" },
        { type: "tool_use", id: "toolu_1", name: "list_files", input: { path: "." } },
      ],
      stop_reason: "tool_use",
      usage: { input_tokens: 100, output_tokens: 20 },
    };
  }
  return {
    type: "message", role: "assistant",
    content: [{ type: "text", text: "E2E_ANTHROPIC_OK 目录已读取。" }],
    stop_reason: "end_turn",
    usage: { input_tokens: 200, output_tokens: 30 },
  };
}

function send(res, code, payload) {
  const text = JSON.stringify(payload);
  res.writeHead(code, { "Content-Type": "application/json", "Content-Length": Buffer.byteLength(text) });
  res.end(text);
}

const server = http.createServer((req, res) => {
  const url = new URL(req.url, "http://127.0.0.1");
  if (req.method === "GET" && url.pathname === "/__state") return send(res, 200, state);
  if (req.method === "GET" && url.pathname === "/__reset") {
    for (const k of Object.keys(state)) { state[k].step = 0; state[k].bodies = []; }
    return send(res, 200, { ok: true });
  }
  let raw = "";
  req.on("data", (c) => { raw += c; });
  req.on("end", () => {
    let body = {};
    try { body = JSON.parse(raw || "{}"); } catch { /* 保持空对象 */ }
    const anthropic = url.pathname.replace(/\/+$/, "").endsWith("/messages");
    const bucket = anthropic ? state.anthropic : state.openai;
    bucket.bodies.push({ path: url.pathname, body, at: Date.now() });
    const step = bucket.step++;
    send(res, 200, anthropic ? anthropicReply(step) : openAiReply(step));
  });
});

server.listen(PORT, "127.0.0.1", () => {
  console.log("mock-provider on http://127.0.0.1:" + server.address().port);
});
