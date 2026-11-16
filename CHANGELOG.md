# Changelog

本文件记录对外可见的变化。每个版本的完整「问题 → 根因 → 修复」表在 `docs/upgrade-*.md`。
格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循语义化版本。

## [0.5.0] — 2026-11-15

### 更名

- 产品名 **ClaudeLink Hub → Chorum**，npm 包名 `claudelink-hub` → `chorum`。
- 注册四个 bin：`chorum`（主）、`chorum-hub`、`claudelink-hub`、`claude-hub`。
- **协议与环境变量不变**：`X-ClaudeLink-Token`、`CLAUDELINK_*`、`~/.claudelink/` 保持原样，避免已装的包与数据目录失效。

### 新增

- `chorum model list|add|test|rm`：命令行直接登记与验证 API 模型，不必开网页。
- `chorum ask --to <id|名称>`：发一条消息并实时显示回复与它发起的工具调用。回复走 stdout、进度走 stderr，可管道。
- `chorum status`：查看当前实例的地址、版本、成员与忙碌状态，不启动、不写盘。
- `npm run e2e:model`：本地 mock provider ⇄ 中枢 ⇄ CLI 的双协议端到端测试（不花钱）。

### 修复

- 工作目录 `workspace.cwd` 指向已不存在的路径时，曾静默 `makedirs` 出一个空气目录，成员与工具根都落在那里。现在告警并回退到数据目录，`doctor` 增加「工作目录」一行。
- `ToolRuntime._path` 校验绝对路径的每个分量，工作区含点目录（`.workbuddy`、`~/.config`）时所有文件工具 100% 失败。改为只校验授权根之下。
- 工具循环末步用 `tool_choice:"none"` 收口，该形状仅 OpenAI 接受，Anthropic 返回 400 结束整轮。改为末步不提供 `tools`。
- 工具调用的中间说明文字被丢弃，多步工具期间界面无输出。现在逐步实时发出。
- 工具参数非法 JSON 时整轮抛异常，改为回灌错误结果继续。
- 一次响应中工具调用超过 16 个时，先执行 16 个再报错，改为先判定再执行。
- npm 包版本与 `hub.py` 的 `APP_VERSION` 各写一遍，升级后每次命令都重启中枢并打断在途轮次。`APP_VERSION` 改为读取 `package.json`。
- 终端版启动器写死机器路径与某个 conda 解释器，项目搬迁后「打不开」。改为六层解析并打印逐层尝试记录。
- 浏览器端 `safe()` 不转义引号，属性值被截断。新增 `attr()` 用于属性上下文。

详见 [docs/upgrade-0.5.md](docs/upgrade-0.5.md)。

## [0.4.0] — 2026-10-28

- Web 工作台补齐工具、推理、持续执行、记忆四个视图；终端新增成员侧栏与 F1–F7 操作菜单。
- 修复 token 占位符重复加引号导致整页脚本报错、所有按钮失效。
- 启动器统一解析运行实例、数据目录、版本与代码摘要；有在途任务时拒绝自动升级。
- 修复工具完成事件覆盖入参、缓存未报告显示为 0%、发送失败丢失草稿等。

详见 [docs/upgrade-0.4.md](docs/upgrade-0.4.md)。

## [0.3.x] — 2026-10

- 圆桌模式（固定顺序、固定轮数、每实例独立角色与会话）、分层记忆、`/model` `/effort` `/tools` `/view thinking`。
- 0.3.1 修复 TUI 连接已有实例时的 401：按实例登记发现真实数据目录并验证鉴权。

详见 [docs/upgrade-0.3.md](docs/upgrade-0.3.md)。
