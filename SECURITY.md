# 安全说明

## 设计前提

Chorum 只在本机运行：服务监听 `127.0.0.1`，不上传对话与凭据，不联网校验授权，没有账号体系。上报的任何问题都应在这个前提下评估。

## 数据落在哪里

以下文件含敏感信息，**不应提交到版本库、不应外发**：

| 文件 | 内容 |
| --- | --- |
| `config.json` | 成员的 API 端点、可能含 API Key；工作区路径与权限授权 |
| `hub.db` | 全部会话、轮次、工具调用参数与输出 |
| `memory.db` | 短期 / 长期记忆正文 |
| `hub.token` | 本机 API 访问令牌（创建时 `0600`） |
| `roundtable.json` | 上一次圆桌的议题原文与角色提示词 |
| `uploads/` | 拖入的附件与提取出的文本 |

`.gitignore` 已覆盖上述路径。打包前 `npm run check` 会拦截密钥形态、用户目录绝对路径、私有词与作者信息。

## 访问控制

- 所有 `/api/*` 需要 `X-ClaudeLink-Token`（`/api/health` 与 `/api/stream` 例外，后者可用查询参数 token）。
- 额外校验 `Host`、`Origin`、`Sec-Fetch-Site`，阻断 DNS rebinding 与跨源请求。
- `server.auth: false` 可关闭 token，但 Host/Origin 校验仍然生效；只在完全可信的隔离环境里这样做。
- 接口只接受 `application/json`（上传除外），避免简单表单跨站提交。

## 工具调用的边界

直连 API 模型的工具（读文件、列目录、网页抓取）只在**授权根之下**工作：

- 授权根 = `workspace.cwd` + 成员面板里为该成员添加的目录；
- 拒绝点文件、`node_modules`、`__pycache__`、`.env`、`hub.token`、`config.json` 等敏感名；
- 拒绝 `.db` `.sqlite` `.key` `.pem` `.token` `.pfx` 等敏感扩展名；
- 网页抓取只允许公共 http(s) 地址，私有网段与本地地址直接拒绝。

## 报告问题

发现凭据泄露、路径穿越或权限绕过时，请**不要**开公开 issue 贴出可用利用链。通过仓库的私有安全报告渠道（GitHub Security Advisories）提交，或直接联系维护者，并附：

1. 复现步骤与受影响版本（`chorum status` 里的版本与代码摘要）；
2. 影响范围（能否读到授权根之外的文件、能否触发未授权请求）；
3. 建议的修复方向（可选）。

不含敏感细节的功能性问题，走普通 issue。
