/* 用页面里真实的 filters / passFilter 源码跑一遍筛选判定（不连浏览器）。

   用法：node tools/filter_check.js
   目的：改过筛选面板（kinds 键名、只看高亮等）之后，确认「镜像」这类消息不再被
   无条件过滤掉 —— 2026-09-26 就踩过 filters.kinds.claudelink 与 .mirror 键名不一致。
*/
const fs = require("fs");
const path = require("path");

const HUB = path.dirname(__dirname);
const html = fs.readFileSync(path.join(HUB, "web", "index.html"), "utf8");
const js = html.match(/<script>([\s\S]*)<\/script>/)[1];

const passFilter = js.match(/const passFilter = m => \{[\s\S]*?\n\};/)[0];
const filtersDecl = js.match(/let filters = \{[\s\S]*?\};/)[0];

const code = `
${filtersDecl}
${passFilter}
const out = [];
const mirror = {kind:'claudelink', sender:'claudelink', target:'all', text:'x'};
const user = {kind:'user', sender:'user', target:'all', text:'x'};
out.push(['初始 kinds', JSON.stringify(filters.kinds)]);
out.push(['初始：镜像消息通过', passFilter(mirror)]);
out.push(['初始：用户消息通过', passFilter(user)]);
filters.kinds = {user:true, reply:true, error:true, event:true, claudelink:true};
out.push(['btnApply 后：镜像消息通过', passFilter(mirror)]);
filters.kinds.claudelink = false;
out.push(['取消勾选「镜像」后：镜像消息通过', passFilter(mirror)]);
filters.kinds.claudelink = true;
filters.onlyHl = true;
out.push(['只看高亮：普通用户消息通过', passFilter(user)]);
out.push(['只看高亮：带高亮的用户消息通过', passFilter({...user, highlight:true})]);
return out;
`;

for (const [k, v] of new Function(code)()) console.log(k.padEnd(34, " "), v);
