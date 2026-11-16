"use strict";
/**
 * 直连 API 模型的命令行入口。
 *
 * 解决的问题：以前「添加 API 模型 / 试一次调用」只能开网页点。没有 CLI 成员可用时
 * （全新机器、只配了 API key），命令行这里是死路 —— `start` 只会提示「可在网页成员面板
 * 添加 API 模型」。这个模块让命令行自己就能把 API 模型配起来、发一条消息，并把模型发起的
 * 工具调用逐步打在屏幕上。
 *
 *   chorum model list
 *   chorum model add --label 名称 --style openai|anthropic --base-url URL --model M
 *                    [--key K] [--path P] [--max-tokens N] [--id 覆盖已有]
 *   chorum model test --base-url URL --model M [--style ...] [--key K]
 *   chorum model rm <id>
 *   chorum ask --to <id|label> [--timeout 300] [--no-wait] <文本，或 - 读 stdin>
 *
 * 模型调用的执行、工具循环、记忆写入都在中枢里，CLI 只负责配置、发起，并把 SSE 上的
 * 进度转成人能读的行。两处各写一套工具循环，迟早会不一致。
 */
const fs = require("fs");
const http = require("http");
const { httpRequest, table } = require("./util");

const STYLES = ["openai", "anthropic"];
const FLAG_ALIAS = {
  "base-url": "baseUrl", baseUrl: "baseUrl",
  "max-tokens": "maxTokens", maxTokens: "maxTokens",
  "api-key": "apiKey", apiKey: "apiKey", key: "apiKey",
  "no-wait": "noWait", noWait: "noWait",
};

function parseFlags(argv) {
  const flags = {};
  const rest = [];
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a === "--") { rest.push(...argv.slice(i + 1)); break; }
    if (!a.startsWith("--")) { rest.push(a); continue; }
    const key = a.slice(2);
    const name = FLAG_ALIAS[key] || key;
    const next = argv[i + 1];
    if (next === undefined || next.startsWith("--")) flags[name] = true;
    else { flags[name] = next; i++; }
  }
  return { flags, rest };
}

function fail(message) { return { code: 2, message }; }

function styleOf(value) {
  const style = String(value || "openai").trim().toLowerCase();
  if (!STYLES.includes(style)) throw new Error("style 只能是 openai 或 anthropic（收到 " + value + "）");
  return style;
}

function mask(key) {
  const v = String(key || "");
  if (!v) return "（未设置）";
  return v.length <= 8 ? "****" : v.slice(0, 4) + "…" + v.slice(-4);
}

/** HTTP 调中枢。失败时把「连不上」和「服务拒绝」分开说，别都报成 undefined。 */
async function api(ready, path, body) {
  const res = await httpRequest(ready.url + path, {
    method: body === undefined ? "GET" : "POST",
    headers: { "X-ClaudeLink-Token": ready.token },
    body,
    timeout: 60000,
  });
  if (res.ok) return res.json || {};
  if (!res.status) {
    const code = res.error && res.error.code;
    throw new Error(code === "ECONNREFUSED"
      ? "连不上中枢（" + ready.url + "）：服务没在跑，或端口不对"
      : "中枢请求失败：" + (code || (res.error && res.error.message) || "未知网络错误"));
  }
  const detail = (res.json && res.json.error) || String(res.text || "").trim().slice(0, 200) || "";
  throw new Error("中枢返回 HTTP " + res.status + (detail ? "：" + detail : ""));
}

async function withHub(ctx, fn) {
  const ready = await ctx.ensureReady();
  if (ready.error) { console.error(ready.error); return 1; }
  try { return await fn(ready); }
  catch (err) { console.error(err.message || String(err)); return 1; }
}

function pickAgent(agents, key) {
  const needle = String(key || "").trim().toLowerCase();
  if (!needle) return null;
  return agents.find((a) => String(a.name).toLowerCase() === needle)
    || agents.find((a) => String(a.label || "").toLowerCase() === needle)
    || agents.find((a) => String(a.label || "").toLowerCase().includes(needle))
    || null;
}

// ------------------------------------------------------------------ model
async function cmdModel(argv, ctx) {
  const { flags, rest } = parseFlags(argv);
  const sub = rest[0] || "list";

  if (sub === "list") {
    return withHub(ctx, async (ready) => {
      const models = (await api(ready, "/api/models")).models || [];
      if (flags.json) { console.log(JSON.stringify(models, null, 2)); return 0; }
      if (!models.length) {
        console.log("还没有 API 模型。添加一个：");
        console.log("  chorum model add --label DeepSeek --style openai \\");
        console.log("    --base-url https://api.deepseek.com/v1 --model deepseek-chat --key sk-…");
        return 0;
      }
      for (const line of table(models, [
        { title: "id", get: (m) => m.id },
        { title: "名称", get: (m) => m.label },
        { title: "协议", get: (m) => m.style },
        { title: "模型", get: (m) => m.model },
        { title: "端点", get: (m) => m.base_url },
        { title: "密钥", get: (m) => (m.api_key_set ? mask(m.api_key) : "（未设置）") },
        { title: "状态", get: (m) => (m.enabled ? "启用" : "停用") },
      ])) console.log(line);
      return 0;
    });
  }

  if (sub === "add" || sub === "update") {
    if (!flags.label || !flags.baseUrl || !flags.model) {
      return fail("缺少必填项：--label / --base-url / --model（密钥用 --key）");
    }
    return withHub(ctx, async (ready) => {
      const saved = (await api(ready, "/api/models", {
        id: flags.id || "",
        label: flags.label,
        style: styleOf(flags.style),
        base_url: flags.baseUrl,
        model: flags.model,
        api_key: flags.apiKey === true ? "" : String(flags.apiKey || ""),
        path: flags.path || "",
        max_tokens: Number(flags.maxTokens || 0) || undefined,
      })).model || {};
      console.log("已保存：" + (saved.label || flags.label) + "  id=" + (saved.id || "?"));
      console.log("试一次：chorum ask --to " + (saved.id || flags.label) + " \"你好，用一句话介绍你自己\"");
      return 0;
    });
  }

  if (sub === "rm" || sub === "delete") {
    const id = rest[1] || flags.id;
    if (!id) return fail("用法：chorum model rm <id>（先用 model list 查 id）");
    return withHub(ctx, async (ready) => {
      await api(ready, "/api/models/delete", { id });
      console.log("已删除：" + id);
      return 0;
    });
  }

  if (sub === "test") {
    if (!flags.baseUrl || !flags.model) {
      return fail("用法：chorum model test --base-url URL --model M [--style openai|anthropic] [--key K]");
    }
    return withHub(ctx, async (ready) => {
      const started = Date.now();
      const data = await api(ready, "/api/models/test", {
        style: styleOf(flags.style),
        base_url: flags.baseUrl,
        model: flags.model,
        api_key: flags.apiKey === true ? "" : String(flags.apiKey || ""),
        path: flags.path || "",
        prompt: flags.prompt || undefined,
      });
      const secs = ((Date.now() - started) / 1000).toFixed(1);
      console.log("可用（" + secs + "s）：" + String(data.reply || "").trim());
      return 0;
    });
  }

  return fail("未知子命令：" + sub + "（可用：list / add / test / rm）");
}

// -------------------------------------------------------------------- ask
/** 极简 SSE 读取：只解析 `data:` 行。返回关闭函数。 */
function openStream(url, onEvent) {
  let req = null;
  let closed = false;
  req = http.get(url, { headers: { Accept: "text/event-stream" } }, (res) => {
    if (res.statusCode !== 200) {
      onEvent({ type: "__stream_error", status: res.statusCode });
      res.resume();
      return;
    }
    res.setEncoding("utf8");
    let buffer = "";
    res.on("data", (chunk) => {
      if (closed) return;
      buffer += chunk;
      let idx;
      while ((idx = buffer.indexOf("\n\n")) >= 0) {
        const block = buffer.slice(0, idx);
        buffer = buffer.slice(idx + 2);
        for (const line of block.split("\n")) {
          if (!line.startsWith("data:")) continue;
          const payload = line.slice(5).trim();
          if (!payload) continue;
          let event;
          try { event = JSON.parse(payload); } catch { continue; }
          onEvent(event);
        }
      }
    });
  });
  req.on("error", () => onEvent({ type: "__stream_error", status: 0 }));
  return () => { closed = true; try { req.destroy(); } catch { /* 已断开 */ } };
}

async function cmdAsk(argv, ctx) {
  const { flags, rest } = parseFlags(argv);
  const timeout = Math.max(5, Number(flags.timeout || 300));
  let text = rest.join(" ").trim();
  if (text === "-" || (!text && !process.stdin.isTTY)) text = fs.readFileSync(0, "utf8").trim();
  if (!text) return fail("用法：chorum ask --to <id|label> \"要问的话\"（结尾写 - 则从 stdin 读）");

  return withHub(ctx, async (ready) => {
    const agents = (await api(ready, "/api/state")).agents || [];
    const enabled = agents.filter((a) => a.enabled);
    if (!enabled.length) {
      throw new Error("没有启用中的成员。先加一个 API 模型：\n" +
        "  chorum model add --label 名称 --style openai --base-url URL --model M --key K");
    }
    const wanted = flags.to || flags.agent;
    const target = wanted ? pickAgent(enabled, wanted) : enabled[0];
    if (!target) {
      throw new Error("找不到成员 " + wanted + "。可用：" +
        enabled.map((a) => a.name + "（" + a.label + "）").join("、"));
    }

    await api(ready, "/api/send", {
      target: target.name,
      text,
      client_msg_id: "cli-" + Date.now().toString(36) + "-" + Math.random().toString(36).slice(2, 8),
    });
    if (flags.noWait) {
      console.log("已发送给 " + target.label + "（不等待回复）");
      return 0;
    }

    const say = (s) => process.stderr.write(s + "\n");
    say("→ " + target.label + "（" + target.name + "）已收到，等待回复…（Ctrl+C 只中断等待，不会停掉中枢）");

    return await new Promise((resolve) => {
      let done = false;
      let closer = null;
      let lastChars = -1;
      let lastTool = "";
      const clearLine = () => { if (process.stderr.isTTY) process.stderr.write("\r" + " ".repeat(40) + "\r"); };
      const finish = (code) => {
        if (done) return;
        done = true;
        clearLine();
        if (closer) closer();
        clearTimeout(timer);
        resolve(code);
      };
      closer = openStream(ready.url + "/api/stream?token=" + encodeURIComponent(ready.token), (event) => {
        if (done) return;
        if (event.type === "__stream_error") {
          say("✗ 订阅回复流失败（" + (event.status ? "HTTP " + event.status : "连接错误") + "）：看不到回复，但消息已发出");
          return finish(1);
        }
        if (event.type === "status" && event.agent === target.name) {
          const chars = String(event.live || "").length + String(event.think || "").length;
          if (chars !== lastChars) {
            lastChars = chars;
            if (process.stderr.isTTY) process.stderr.write("\r  生成中… " + chars + " 字");
          }
        }
        if (event.type === "step" && event.step && event.step.kind === "tool_call") {
          const step = event.step;
          const word = { running: "开始", ok: "完成", error: "失败" }[step.status] || step.status || "";
          const mark = step.status === "error" ? "✗" : (step.status === "running" ? "·" : "✓");
          const row = "  " + mark + " 工具 " + (step.name || "?") + " " + word;
          if (row !== lastTool) {
            lastTool = row;
            clearLine();
            say(row);
          }
        }
        if (event.type === "message" && event.message) {
          const m = event.message;
          if (m.sender !== target.label && m.sender !== target.name) return;
          if (m.kind === "reply") { clearLine(); console.log(m.text || ""); return finish(0); }
          if (m.kind === "error") { clearLine(); say("✗ " + target.label + " 失败：" + (m.text || "未知错误")); return finish(1); }
        }
      });
      const timer = setTimeout(() => {
        say("… 等待 " + timeout + " 秒仍未收到回复；中枢里这一轮可能还在跑。");
        say("  用 chorum status 看状态，或到网页 / 终端版看完整回复。");
        finish(2);
      }, timeout * 1000);
    });
  });
}

module.exports = { cmdModel, cmdAsk, pickAgent, parseFlags };
