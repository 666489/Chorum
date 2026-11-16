/* 抓 /api/stream 的 SSE 事件若干秒，统计 status 事件里的 live / think 长度。

   用法：node tools/browser_stream.js [url] [输出文件] [毫秒]
   例（先起抓取，再投消息，就能看到流式过程）：
     node tools/browser_stream.js http://127.0.0.1:8787 sse.txt 45000
     (另一个终端) curl.exe -s -X POST http://127.0.0.1:8787/api/send ^
        -H "Content-Type: application/json" -d "{\"text\":\"只回复两个字：在线\",\"target\":\"codex\"}"

   用途：改过 set_status / 流式通道之后，先看这里有没有 think，再去看界面。
   注意：Windows 上别用 Start-Process 把 curl 的输出重定向进来（会被策略拦），
   直接用 node 这份脚本落盘更省事。
*/
const http = require("http");
const fs = require("fs");

const url = process.argv[2] || "http://127.0.0.1:8787/api/stream";
const out = process.argv[3] || "sse-out.txt";
const ms = parseInt(process.argv[4] || "45000", 10);

const chunks = [];
const req = http.get(url, res => {
  res.setEncoding("utf8");
  res.on("data", d => chunks.push(d));
});

setTimeout(() => {
  try { req.destroy(); } catch (e) {}
  const text = chunks.join("");
  fs.writeFileSync(out, text, "utf8");
  const lines = text.split("\n").filter(l => l.startsWith("data:"));
  const events = [];
  for (const l of lines) { try { events.push(JSON.parse(l.slice(5))); } catch (e) {} }
  const status = events.filter(e => e.type === "status");
  const withThink = status.filter(e => (e.think || "").length);
  const maxThink = withThink.reduce((a, e) => Math.max(a, (e.think || "").length), 0);
  const maxLive = status.reduce((a, e) => Math.max(a, (e.live || "").length), 0);
  console.log("总字节", text.length, "| 事件", events.length, "| status", status.length);
  console.log("带非空 think 的 status:", withThink.length, "| think 最大长度", maxThink, "| live 最大长度", maxLive);
  console.log("thinking 步骤事件:", events.filter(e => e.type === "step" &&
    e.step && e.step.kind === "thinking").length);
  console.log("落盘:", out);
}, ms);
