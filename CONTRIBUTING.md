# 贡献指南

## 这个仓库的形态

中枢是 **Node 启动器 + Python 服务** 的组合，两边都只用标准库（`bin/` 只用 Node 内置模块，`*.py` 只用 Python 标准库）。产品运行不需要 `npm install`。

因此**运行时脚本刻意放在仓库根目录**：

- `hub.py` 用 `__file__` 解析 `web/`、数据目录与配置文件；
- `bin/cli.js` 用 `__dirname/..` 作为包根（`ROOT`），据此定位 `hub.py` 与 `config.example.json`；
- `package.json` 的 `files` 白名单按同样的相对位置列举。

把 `hub.py` 或 `bin/` 挪进子目录，会连带改 `ROOT`、`WEB_DIR`、`DATA_DIR`、白名单和所有文档链接。除非有充分理由，不要动这个结构。

## 环境

| 依赖 | 版本 | 说明 |
| --- | --- | --- |
| Node.js | ≥ 18 | 启动器、检测、打包测试 |
| Python | ≥ 3.9，含 `sqlite3` | 中枢本体；Windows 上避免使用应用商店的 `python3` 存根 |

`CLAUDELINK_PYTHON` 可显式指定解释器。

## 改动前必读

- **不要提交运行数据**：`config.json`（含端点与密钥）、`hub.db`（含全部会话）、`memory.db`、`hub.token`、`uploads/`、`roundtable.json`（含议题原文）。`.gitignore` 已覆盖，但提交前请看一眼 `git status`。
- **协议标识符不要改名**：`X-ClaudeLink-Token`、`CLAUDELINK_*` 环境变量、`~/.claudelink/` 数据目录。改名会连带迁移已安装的包与已有数据，收益为零。产品名是 Chorum，不代表标识符要跟着改。
- **工具路径校验只校验授权根之下**：见 `runtime.py: ToolRuntime._path`。不要退回「校验绝对路径每个分量」的写法，那会让工作区在点目录里的所有文件工具失效。

## 测试

```bash
npm run check        # 密钥 / 用户目录 / 私有词守门（打包前必跑）
npm test             # Node 检测用例 + Python 回归（17 项）
npm run selftest     # 注册表、检测优先级、并发锁
npm run smoke        # 临时目录里跑一次真实安装启动
npm run pkg-e2e      # npm pack → 临时 prefix 安装 → doctor
npm run e2e:model    # mock provider ⇄ 中枢 ⇄ CLI，双协议带工具调用
npm run ops          # check + selftest + smoke + pkg-e2e
```

`npm run e2e:model` 里的 mock provider **必须是独立进程**：测试用 `spawnSync` 调 CLI，同进程的 HTTP server 会被事件循环阻塞，表现为「provider 收到 0 次请求」。

改了 `runtime.py` 的工具循环或 `hub.py` 的 API 模型路径，至少跑 `npm run e2e:model` 与 `npm test`。

## 提交

- 提交信息用中文或英文均可，但要说清**问题**而非动作：「修复工作目录失效后静默创建空目录」，不是「修改 hub.py」。
- 一次提交只做一件事。格式化、改名、修 bug 分开。
- 新增行为请同时更新 `CHANGELOG.md`；接口或配置变更同时更新 `docs/` 下对应文件与 `README.md`。

## 文档

- `README.md` 是入口，控制在两百行内，只讲「是什么、怎么用、去哪看细节」。
- 完整手册在 `docs/manual.md`；打包与便携化在 `docs/packaging.md`；Agent 接入在 `docs/agent-integration.md`；圆桌设计依据在 `docs/roundtable.md`。
- 文档里的路径优先用相对链接，不要写死机器路径（`npm run check` 会拦 `Anaconda` 这类安装路径）。
