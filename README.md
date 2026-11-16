# Chorum

**把本机已装的 AI Agent CLI，和任意 OpenAI / Anthropic 兼容的 API 模型，收进同一条群聊。**

一条时间线 · @ 点名接力 · 圆桌 · 持续执行 · 工具调用留痕。全部在本机运行，只监听回环地址，不上传对话与凭据，没有账号。

> 原名 ClaudeLink Hub（`claudelink-hub`），0.5.0 起更名。命令 `chorum`，别名 `chorum-hub` / `claudelink-hub` / `claude-hub`。
> 当前版本 **0.5.0**，变化见 [CHANGELOG](CHANGELOG.md)。

---

## 为什么

同时用几个编码 Agent 时，卡住的地方不是模型能力，而是它们之间：

| 断层 | 表现 |
| --- | --- |
| 终端割裂 | 每个 CLI 一个窗口，谁说了什么靠人肉搬运 |
| 上下文不共享 | 每个 Agent 只看见自己窗口的历史，材料要复述 N 次 |
| 成本不可见 | 一轮几十次调用，token 与工具次数散落在各家日志里 |

Chorum 补的是这一层：发言、接力、预算、证据放在同一条时间线上，可回看、可停止、可计量。它不替代任何 Agent，每个成员仍然用它自己的模型与配置。

## 特性

- **成员自动发现**：九层探测本机已装的 Agent（环境变量 → 配置 → PATH → 已知目录 → npm 全局根 → 搜索根 → 用户声明 → 版本目录 → Everything）。找不到时 `detect --explain` 逐层说明原因并给补救命令。
- **直连 API 模型**：只填 Base URL、模型名、Key 就能开工，支持 OpenAI 与 Anthropic 两种协议；同样具备六个只读工具（网页搜索、网页读取、读文件、列目录、检索记忆、保存记忆），工具结果回灌给模型、可多步。
- **一条时间线**：发给全体或只问某人；回复里 @ 谁就触发谁，接力有深度上限。
- **圆桌**：按你指定的顺序逐个发言，每个实例独立角色与系统提示词，固定轮数，随时暂停/继续。
- **持续执行**：主代理只规划一次，执行代理按依赖推进，每步存检查点，中断可恢复。
- **可回查**：每次工具调用的名称、入参、输出、状态都保留；token 分输入/输出/缓存读/缓存写四个互不重叠的桶；失败也算一次已结算的轮次，带原因入库。

## 快速开始

需要 **Node.js ≥ 18** 与 **Python ≥ 3.9**（含 `sqlite3`）。产品运行不需要 `npm install`。

```bash
# 用本地打包的安装包（尚未发布到 npm 公共仓库）
npm i -g ./chorum-0.5.0.tgz

chorum doctor      # 体检：Agent / Python / 工作目录 / npm / 配置 / 缓存
chorum start       # 按检测结果装配 config.json 并启动，自动打开 http://127.0.0.1:8787
```

从源码运行：

```bash
node bin/cli.js doctor
node bin/cli.js start        # 或 npm start / npm run tui / npm run ui
```

不想开浏览器就用 `chorum tui`：零依赖终端版，直连同一个中枢，SSE 实时、不轮询。

### 没有 CLI，只有 API Key？

```bash
chorum model add --label DeepSeek --style openai \
  --base-url https://api.deepseek.com/v1 --model deepseek-chat --key sk-...

chorum ask --to DeepSeek "列出工作区里的 TODO，按风险排序"
```

`ask` 会实时打出这次调用的工具步骤，回复走 stdout、进度走 stderr，可以直接管道。

## 命令

| 命令 | 作用 |
| --- | --- |
| `chorum doctor` | 一次性体检 |
| `chorum detect [--json\|--write]` | 只识别本机 Agent，不启动 |
| `chorum detect --explain <id>` | 某个 Agent 找不到时，逐层看「试过哪 / 命中或不存在 / 原因」 |
| `chorum setup [--dry-run]` | 只装配 `config.json` |
| `chorum start [--port N] [--no-open] [--detach]` | 装配并启动 |
| `chorum tui` / `chorum ui` | 终端版 / 只开网页 |
| `chorum status [--json]` | 看当前实例，不启动、不写盘 |
| `chorum stop` | 停中枢，数据保留 |
| `chorum model list\|add\|test\|rm` | 管理 API 模型 |
| `chorum ask --to <id\|名称> "…"` | 发消息并实时看回复与工具调用（`--timeout` / `--no-wait` / 结尾 `-` 读 stdin） |
| `chorum setup --attach\|--detach` | 把 Agent 接到 ClaudeLink MCP / 精确移除 |

完整说明：`chorum help`。

## 配置

`config.json` 首次 `start` 时按 [`config.example.json`](config.example.json) 生成。常用项：

| 键 | 说明 |
| --- | --- |
| `server.port` / `server.host` | 默认 `127.0.0.1:8787`；只监听回环 |
| `workspace.cwd` | 成员的工作目录与工具授权根。指向不存在的路径时会回退到数据目录并告警 |
| `tools.enabled` / `tools.max_steps` | 直连模型的工具开关与单轮最大步数 |
| `permissions.grants.<成员>` | 每个成员可额外访问的目录 |
| `context.history_limit` | 进 prompt 的历史条数 |
| `relay.max_depth` | @ 接力深度上限，防 A@B、B@A 对喊 |

数据（`hub.db`、`memory.db`、`hub.token`、日志、上传）默认落在包目录；系统级只读安装会落到 `~/.claudelink-hub`，可用 `CLAUDELINK_HOME` 指定。

## 架构

```
 CLI 成员                      Chorum 中枢                     API 模型
 claude    --+                                            +--  openai 协议
 codex       |--->  :8787  群聊 / 接力 / 圆桌 / 任务 / 记账  <--|
 codebuddy   |                 |          |                +--  anthropic 协议
 dsh       --+              hub.db     memory.db
                               |
                         web/ 与 hub_tui.py（两个前端）
```

- `bin/cli.js`（Node）负责检测、装配、启动、控制，不承载业务；
- `hub.py`（Python，仅标准库）提供 HTTP/SSE 接口、成员调度、工具循环与记账；
- 两个前端（`web/` 与 `hub_tui.py`）连的是同一个本机实例。

## 目录

```
bin/            Node 启动器：cli / detect / setup / attach / env / model / util
web/            网页工作台（index.html、workbench.*、theme/）
hub.py          中枢本体：HTTP 接口、调度、工具循环、记账
hub_tui.py      终端版前端            tui_workspace.py  终端布局
runtime.py      工具运行时与记忆        workflows.py      持续执行
hub_connection.py  本机实例发现与鉴权   hub_pid.py        进程/端口工具
extract.py      附件文本提取           codex_cli.py      Codex exec 适配
docs/           文档（见 docs/README.md）
scripts/        守门与测试（不随包发布）
test/           Node 单元测试
tools/          开发工具，如 MCP 终端版安装器（不随包发布）
```

`*.py` 与 `bin/` 刻意留在根目录：`hub.py` 用 `__file__` 定位 `web/` 与数据目录，`bin/cli.js` 用 `__dirname/..` 作为包根。挪动它们要同步改 `ROOT`、`WEB_DIR`、`files` 白名单与所有文档链接。

## 开发

```bash
npm run check        # 密钥 / 用户目录 / 私有词守门，打包前必跑
npm test             # Node 检测用例 + Python 回归（17 项）
npm run selftest     # 注册表、检测优先级、并发锁
npm run smoke        # 临时目录里跑一次真实安装启动
npm run pkg-e2e      # npm pack → 临时 prefix 安装 → doctor
npm run e2e:model    # mock provider ⇄ 中枢 ⇄ CLI，双协议带工具调用
npm run ops          # check + selftest + smoke + pkg-e2e
```

细节与「不要提交什么」见 [CONTRIBUTING](CONTRIBUTING.md)，安全问题见 [SECURITY](SECURITY.md)。

## 已知边界

- 直连 API 模型**带工具**走非流式请求：能看到每步正文与工具行，但没有逐字打字机效果。
- 界面与调度链路的自动化测试用确定性本地 mock，证明「可操作、可发起并完成调用」，**不证明真实模型的语义质量或话题不漂移**。
- 真实模型的长会话成本、并发压力、以及不同中转站对 `tools` 字段的兼容性，不在自动化通过结论里。
- 未发布到 npm 公共仓库，目前通过本地 `.tgz` 分发。

## 文档

[文档索引](docs/README.md) · [完整手册](docs/manual.md) · [运维](docs/ops.md) · [打包与便携化](docs/packaging.md) · [Agent 接入](docs/agent-integration.md) · [圆桌设计依据](docs/roundtable.md)

## 许可

[MIT](LICENSE)。

终端版通过 MCP 挂载依赖上游开源项目 [`claudelink`](https://www.npmjs.com/package/claudelink)（MIT，作者 Jay Siddiqi）：中枢复用它的 MCP 服务端，只额外注入一个 `tui` prompt 与 `open_terminal` 工具。它与本项目的更名历史无关，是独立依赖。
