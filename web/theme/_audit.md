# 深色主题一致性审计 · 20-chat / 30-workbench

审计对象：`theme/20-chat.css`、`theme/30-workbench.css`（可改）
参照：`theme/00-tokens.css`（只读）、`theme/10-shell.css`（只写建议，不改）
实测方式：把页面真实渲染进 headless Edge，用注入探针把每个**有直接文本的节点**的
`color`、有效背景（逐层取 `backgroundColor` 并做 alpha 合成）、祖先 `opacity` 连乘取出来，
按 WCAG 2.1 算比值。共采集 6 个状态（conv / agents / share / filter / dialog / wb，
1154 个文本节点）；mock 渲染不到的组件（`.msg.perm` 内的权限卡、`.dialog .perm`、
`.wb-dialog` 全套、`.tstep .detail` 等）另做了一份合成画布 `_preview/_synth.html` 量。
探针脚本：`_preview/mkprobe.py`（可复跑）。截图：`_preview/shots/audit_*.png`。

> 时点声明：报告里的行号/像素/色值以四份 CSS 的 **20:00 落盘版**为准（身份条 3px 已计入）。
> 你正在做的手工微调如果继续改这些值，请以文件为准，本报告只作为语义与对比度的基线。

---

## 1. 语义映射总表

### 1.1 表面阶梯（elevation）

| token | 语义 | 实际用在 |
|---|---|---|
| `--nc-canvas` #0b0e13 | 画布（最底） | `body`、`main`、`#log`、`#workbenchPanel`、`#activity`、`#status`、`.wb-task pre` |
| `--nc-sunken` #0e131a | 下沉（凹槽 / 附属块） | `#text`、`.edit`、`.grid`/`.mp-row`/`.wb-dialog` 输入、`.thinkbox`、`.tstep .detail`、`.att-item`、`.wb-card pre`、`.wb-task`、`.msg .perm` |
| `--nc-surface` #131821 | 常规面板 | `header`、`#sidebar`、`#workbenchBar`、`footer#composer` |
| `--nc-raised` #19202b | 抬起（卡片 / 气泡） | `.msg`、`.box`、`.wb-card`、`.att-chip`、`.morebtn`、`#cvNew`、侧栏 / 面板次按钮 |
| `--nc-overlay` #202835 | 浮层 | `.dialog`、`.wb-dialog`、`#selBar`、`#hlBar`、`.cv-menu` |
| `--nc-overlay-2` #26303f | 浮层内再抬 | `.dialog button`、`.wb-dialog button`、`.cv-menu button:hover` |

### 1.2 文本四级

| token | 语义 | 用在哪 |
|---|---|---|
| `--nc-text` | 正文 / 标题 | `.msg .body`、`.who`、卡片标题、`h2`、`#send` 反白字池 |
| `--nc-text-2` | 次要 / 附属说明 | `.hd`、`.tstep`、`.wb-meta`、`.note`、`.thinkbody`、`.grid label`、`.perm .r` |
| `--nc-text-3` | 三级 / 旁白 | `.chip`、`.sys`、`.thklbl`、`.dur`、`.diffstat`、`.hintbar`、`.wb-card small`、`.wb-empty`、placeholder |
| `--nc-text-4` | 仅禁用 / 装饰 | 现在只剩 `#shutdown:disabled` 一处在生效（`.dot` 空闲与 `#status .st.off` 已改 `text-3`，但旧规则还留在 `10:120` / `10:272`，靠文件顺序覆盖——见 F-04） |

### 1.3 语义色（口径已定）

| 颜色 | 语义 | 全站落点 |
|---|---|---|
| `--nc-ice` #7cc4e8 | **能点**：按钮/链接/焦点/选中 | `.msg .hd button:hover`、`.morebtn:hover`、`.copybtn:hover`、`.filelink`、`#pick:hover`、`.thtoggle:hover`、`.wb-* button:hover`、`#addModel` 描边字、`:focus-visible` |
| `--nc-ice-hi` | 能点之「已选中 / 已激活」 | `#targets button.on`、`#workbenchBar nav button.on`、`#send:hover`、`.editbar/.dialog/.wb-* primary:hover` |
| `--nc-ice-wash/line` | 选中膜 / 描边 | `#targets .on`、nav `.on`、`#activity .act.on`、`.agent.active`、`.cv-item.on`、`#text:focus` 环 |
| `--nc-ice-ink` | 冰蓝实底上的字 | `#send`、`.primary`、`.dz-icon`、`#stop.live` |
| `--nc-gold` #e9c46a | **没做完 / 需要你看一眼** | 置顶星、`.dot.run`、`.tstep.running`、`.tstat.warn`（超时/中断）、`.chip.typing`、`.msg.perm` 左条、`.msg.hl` 内描边 + 左条、`.hlseg`、`#coldBanner`、`.cold`、`.grant .m`、`.wb-task` 左条、`wb-task` running/paused/blocked、`.perm .t` |
| `--nc-ok` #5bd6a0 | **完成 / 在线** | `.tstat.ok`、`.dot.on`、`.wb-task [done]`、`.perm button.ok`、diffadd |
| `--nc-danger` #f07c7c | **真失败** | `.msg.error`、`.tstat.err`、`.tstep.error`、`.wb-error`、`#banner`、`#wbError`、`#shutdown`、diffdel |
| `--nc-danger-hi` | 正文级错误字 / 危险 hover | `.errline`、`.wb-error`、`#banner` 字、`#wbError` 字、`.cv-menu .danger:hover`、`.att-item button:hover`、`.wb-participant button:hover` |
| `--nc-warn` #f0a85c | **只允许命令行走廊** | 仅 `.tstep .detail .cmdline`（改后全站 1 处） |
| `--id-*` | 谁在说话 | `.msg.<role>` 3px 左条 + `.who`；`.msg.user` 14% 身份色底 |

---

## 2. 发现的问题表

严重度：**P0** = 破坏既定口径或明显缺陷（用户一眼能看出）；**P1** = 同语义两色/两层错位；
**P2** = 打磨项。

处置只有两档：
- **已改** —— 已落在对应区域文件（`20-chat.css` / `30-workbench.css`），规则上方有 `审计：<问题> → <做法>` 注释；
- **建议（未改，理由）** —— 只给改法，我没动一行。绝大多数的原因是它在 `10-shell.css`（按约定归你改），
  其余是需要 JS 钩子、或属于清理而非一致性缺陷。

**P0 共 3 条，已全部闭环**（A-01 / C-01 由我改，B-01 由你改）；截至落盘时**未处置的 P0 = 0**。
下面「已改（由 team-lead 落地）」的行是我复核你 20:00 那版落盘后补的状态，用于对齐，不是我改的。

| # | 级 | 位置 | 现象 | 证据 | 处置 |
|---|---|---|---|---|---|
| A-01 | **P0** | `20-chat.css` `.tstep.error` | 步骤失败用**橙** `--nc-warn`，与 `.msg.error`/`.tstat.err`/`.wb-error` 的红不是一族，并把「橙只准出现在命令行走廊」的口径打破 | 改前 20:253 `.tstep.error{color:var(--nc-warn)}`；同一屏里 `.tstat.err` 是红 | **已改** → `--nc-danger`（橙全站只剩 `.cmdline` 1 处） |
| C-01 | **P0** | `30-workbench.css` `.wb-dialog input::placeholder` 等 | 占位符用 `text-4`，在 sunken 上只有 **3.40:1**；而这里的占位符是「需要完成什么，交付哪些实际结果」这类指令性文字 | 计算：`#5e6a7c` on `#0e131a` = 3.40；聊天区所有 placeholder 是 `text-3` | **已改** → `--nc-text-3`（6.78:1），同时消掉「placeholder 两色」 |
| B-01 | **P0** | `10-shell.css`（原缺规则） | `#btnShare`「保存公告」**没有任何主题规则命中**，渲染成原生 UA 按钮：`#6b6b6b` 底 + 纯白字 + 2px 纯白边 + 0 圆角，是整块侧栏里唯一的系统灰方块 | 实测 `getComputedStyle(#btnShare)` → `bg rgb(107,107,107) / color rgb(255,255,255) / border 2px rgb(255,255,255) / radius 0px` | **已改**（由 team-lead 落地，非我改）：`10-shell.css:299` 补了 `#btnShare` 与 `#btnShare:hover` |
| A-02 | P1 | `20-chat.css` `.perm` ↔ `.msg.perm` | 类名交叉污染：`msg.kind==='perm'` 的气泡是 `class="msg perm"`，里面又套一层 `class="perm"` 卡片，`.perm` 的 `border-radius`/`border-color` 因此染到**气泡本身** | 改前实测量到 `.msg.perm`：`radius 9px`（同级 `.msg` 是 12px）、四边被刷成 `gold-line` | **已改** → `.perm:not(.msg)`，气泡回到 `r-lg` + 发丝线（复测 12px） |
| A-03 | P1 | `20-chat.css` `.att-chip` | 附件标签是**不可点**的纯文本，却用 `ice-wash` 底 + `ice-hi` 字，读起来像按钮，违反「冰蓝=能点」 | `index.html:743` 生成的是 `<span class="att-chip">📎 name</span>`，无任何交互 | **已改** → 与 `.att-item` 同一套中性材质（raised + line-2 + 正文色），靠 📎 区分 |
| A-04 | P1 | `20-chat.css` `#hlBar button` | 「清除本条高亮」是**可点**按钮，字却是金色 → 金同时表示「高亮对象」和「能点」 | `#hlBar button{color:var(--nc-gold)}`，hover 金↑ | **已改** → 字回正文色、hover 冰蓝；金只留在浮条边框（框住的是高亮对象） |
| A-05 | P1 | `30-workbench.css` `.wb-card small` / `.wb-meta` vs `.wb-task span` | **同一串状态字两种颜色**：卡片头 / meta 里的 `running` 是灰（text-3/text-2），同一张卡里任务行 `running` 是金 | mock 复现截图 `shots/audit_wb.png`：右侧 `wf_8f21 · running` 灰、任务行 `running · 2 步` 金 | **已改**（由 team-lead 于 20:00 落地，非我改）：`workbench.js:34` 给 meta 状态套了 `<span data-st>`，`30-workbench.css:379-386` 与任务行共用同一套四桶映射。残留：工具记录/推理摘要视图的 `<summary><small>` 仍是无钩子的灰字（P2） |
| A-06 | P1 | `20-chat.css` 输入家族 | 同一个「sunken 输入框」两种描边：`#text`/`.grid`/`.mp-row` 用 `line`(6.5%)，侧栏与工作台用 `line-2`(10%) | 实测 `#text` border `rgba(255,255,255,.067)` vs `.wb-dialog input` `rgba(255,255,255,.10)` | **已改** → 统一 `line-2` |
| B-02 | P1 | `10-shell.css` `.agent button` / `.box button` | 次按钮底色 `raised`，而它所在的卡（`.box`/`.agent`）也是 `raised` → **按钮和卡片同色**，只靠一根发丝线分开 | 实测 `.agent button` bg `rgb(25,32,43)` == `.agent` bg；截图里「单发/设主代理/移除」糊在卡里 | **建议（未改，10-shell 归你）**：`label.chk` 已按此改到 `--nc-sunken`（`10:312`），但 `.agent/.box/.tt button` 仍是 `raised`。三处一起改才齐 |
| B-03 | P1 | `10-shell.css` `.box` | 同层不同边：`.msg`/`.wb-card` 用 `line`，`.box` 用 `line-2` | 实测 `.box` border `rgba(255,255,255,.10)`，`.wb-card` `.067` | **已改**（由 team-lead 落地）：`10:320` 追加 `.box { border-color: var(--nc-line) }` |
| C-02 | P1 | `10-shell.css` `#status .st.off` | 「跟随 关 / 接力 关」是**可点**开关，却用 `text-4` + `opacity .85`，合成后 **2.86:1**，读不出来也点不准 | 计算：`#5e6a7c`×.85 on `#0b0e13`；`index.html:662,665` 这两项带 `data-go`，点了会切状态 | **已改**（由 team-lead 落地）：`10:319` 改 `--nc-text-3` 并撤掉 `opacity`（7.03:1） |
| C-03 | P1 | `10-shell.css` `.dot` | 空闲点 `text-4` 在 raised 上 **2.98:1**，低于非文本控件 3:1 的线；而它承载「空闲」这个状态 | 计算 `#5e6a7c` on `#19202b` = 2.98 | **已改**（由 team-lead 落地）：`10:322` 改 `--nc-text-3`（6.47:1） |
| E-01 | P1 | `20-chat.css` `#targets button:hover` | 未选中胶囊 hover 只换字色+边色，底色不动 → 「点了没反馈」 | 改前 hover 只 `color/border-color` | **已改** → 叠一层中性 `--nc-line`（不用冰蓝 wash，否则与 `.on` 分不开；`.on` 特异度相同且写在后面，选中态不受影响） |
| E-02 | P1 | `20-chat.css` `.tstep.hasdetail` | 整行 `cursor:pointer` 但**没有任何 hover 态**，只有 `›` 常亮 | 改前 `.tstep.hasdetail .lb::after` 只有静态色 | **已改** → 未展开时 hover 把 `›` 提一档（不动已展开的，避免和「展开=冰蓝」打架） |
| E-03 | P2 | `20-chat.css` `.tstep .detail .filelink` | hover **比默认更暗**（默认 `ice-hi` → hover `ice`），反馈方向反了 | 改前 `filelink{color:ice-hi}` / `:hover{color:ice}` | **已改** → 默认 `ice`、hover `ice-hi` |
| A-07 | P2 | `20-chat.css` `#pick:hover` / `.thtoggle:hover` | 文本型 hover 目标色三档并存（`ice` / `ice-hi` / `text`） | `.msg .hd button:hover`=`ice`、`.morebtn:hover`=`ice`、而 `.thtoggle:hover`/`#pick:hover`=`ice-hi` | **已改** → hover 一律 `ice`，`ice-hi` 只留给选中/激活 |
| A-08 | P2 | `30-workbench.css` `#workbenchBar nav button.on` | 选中态字色 `ice`，而 `#targets .on` 是 `ice-hi` —— 同一个「选中」两种亮度 | 实测两处 `getComputedStyle().color` | **已改** → `ice-hi` |
| A-09 | P2 | `20-chat.css` `.att-item button` | 「移除附件」静态就刷满红，红被摊薄到看不出轻重（口径：红=真失败） | 改前 `color: var(--nc-danger)` | **已改** → 静态 `text-3`，hover 才 `danger-hi`（与 `.wb-participant button:hover` 同套） |
| A-10 | P2 | `20-chat.css` `.msg.hl` / `.msg.hl.user` | 重写 `box-shadow` 时丢掉了 `.msg` 的顶缘微光 `inner-hi`，高亮气泡比同级气泡少一层材质 | 改前 `.msg` 是 `shadow-1 + inner-hi`，`.msg.hl` 只有 `shadow-1` | **已改** → 补 `inner-hi` |
| B-04 | P2 | `30-workbench.css` `.wb-card` | 同级 raised 里面只有卡片没有 `inner-hi` | `.msg`/`.box` 都有 | **已改** → `shadow-1 + inner-hi` |
| A-11 | P2 | `30-workbench.css` `.wb-error` | 卡片内错误段用 `danger`(6.14:1)，聊天区同一「正文级错误字」`.errline` 用 `danger-hi`(8.23:1) | 同一语义两种深浅 | **已改** → `danger-hi` |
| B-05 | P2 | `20-chat.css` 圆角 | 裸写半径与 token 并存：`#text` 10px、`.edit` 8px、`.grid input` 8px、`#send` 10px，而侧栏/工作台同类控件是 `r-sm`/`r-md` | grep 出裸 px | **已改** → `#text`/`.edit`/`#send` → `r-md`，`.grid`/`.mp-row` → `r-sm`（与 `.wb-dialog` 对齐） |
| F-01 | P2 | `30-workbench.css` `.wb-actions .primary` | 这条规则**永远不生效**：`.wb-actions .primary`(0,2,0) 打不过 `#workbenchPanel button`(1,0,1)。实际生效的是 `#workbenchPanel button.primary` / `#workbenchBar button.primary` / `.wb-dialog button.primary` | 三个 ID 版都在，行为正确但多一条死规则 | **建议（未改，理由：删规则属清理不属一致性，且易与作者后续改动撞车）**：删掉这半条，或改成 `#… button.primary` 已覆盖 |
| F-02 | P2 | `30-workbench.css` `.wb-dialog button` vs `.wb-actions button` | 同特异度 (0,1,1)，靠「`.wb-dialog button` 写在 323 行、`.wb-actions button` 写在 106 行」才拿到 `overlay-2`；谁被挪动谁坏 | 实测 `.wb-dialog .wb-actions button` = `rgb(38,48,63)` ✓ | **已改**（仅注释，无规则变更）：在该规则上写明顺序依赖，防止后人重排 |
| D-01 | P2 | `00-tokens.css` | 有 token 写完没人用：`--nc-gold-lo`、`--nc-gold-ink`（12 个兼容别名 `--bg/--panel/…` 由 `index.html` 内联样式在用，`--nc-font` 由 00 自己用，都不算死） | 三文件全文检索 0 命中 | **建议（未改，理由：按约定 token 只报告不删）**：仅登记 |
| D-02 | P2 | `00/10/20/30` 遮罩 | 字面色值三处：`.mask`、`.wb-dialog::backdrop` 都是 `rgba(6,9,14,.62)`，`#drop` 是 `rgba(0,0,0,.66)` | grep | **已改**（由 team-lead 落地）：`00:83-84` 新增 `--nc-scrim` / `--nc-scrim-2`，`10:277` `20:475` `30:269` 三处已全部改为引用 token |
| D-03 | P2 | `20-chat.css` `#targets .chip` | 死规则：`#targets` 里从来没有 `.chip`（targets 是纯 `<button>`） | `mock.js` / `index.html:392` 的 `.chip` 在 header | **建议（未改，理由：删掉会动到别人的排版意图，先留）**：仅登记 |
| F-03 | P2 | `30-workbench.css` `.wb-task` | 失败任务块的**左竖线仍是金**（金=待处理），只有右侧文字是红 | `.wb-task` 无状态钩子，状态只在子 `<span>` 上 | **已改**（由 team-lead 落地，非我改）：`30:389` `.wb-task:has(span[data-st="error"]) { border-left-color: var(--nc-danger) }` |
| F-04 | P2 | `10-shell.css` `.dot` / `#status .st.off` | 修复是**追加**在文件末尾（`10:319/322`），旧规则 `10:120`、`10:272` 原封不动留在前面，现在靠「同特异度、后写者赢」生效。日后若有人重排、合并或跑 CSS 压缩，很容易又变回 `text-4`（2.98:1 / 2.86:1） | `grep -n "nc-text-4" 10-shell.css` → 120、272 仍在 | **建议（未改，10-shell 归你）**：把两处旧声明就地改掉、删掉末尾的两条补丁，别留顺序依赖 |

### 2.0 复核后撤销的疑点（记录在案，避免以后重复讨论）

| 疑点 | 结论 |
|---|---|
| `.cv-item .pin` 是硬编码 `#f5c26b` | **不成立**。那个 hex 在 `index.html` 的内联样式里；`10-shell.css:89` 有 `.cv-item .pin{color:var(--nc-gold)}`，加载更晚、同特异度，实际生效的是金 token |
| `.cv-item:hover` 与 `.cv-item.on` 分不开 | **不成立**。hover = 中性 `--nc-raised` 提亮 + 正文色；`.on` = 冰蓝膜 + `inset 0 0 0 1px var(--nc-ice-line)` 内描边。一个是提亮，一个是「结膜 + 描边」，缩到 30% 也是两回事 |

### 2.1 抽出来没改、但必须由你拍板的两处口径

1. **`.perm button.ok/no` 的绿/红**（`20:587`）。严格按口径，红=真失败、绿=完成，「同意/拒绝」不属于这两类。
   但「是/否」是权限卡里唯一的二元动作，去掉红绿就只剩两个同形按钮。我**保留**了，并在报告里登记为口径外的最小延伸。
   如果你要统一到冰蓝，一处改动即可（`.perm button.ok/no` 只留 `--nc-ice-*`）。
2. **`.msg.hl` 的左条转金**（这条是 20 里另一位作者 19:52 加的「艺术指导补」，不是我加的）。
   它让「高亮」压过「说话人身份」，与 00-tokens 注释里「身份色只做索引」有张力，但和 `.hlseg` 一致，我没动。

---

## 3. 对比度实测表

口径：正文 ≥4.5:1；≥18.74px/700 或 ≥24px 可放宽到 3:1；装饰性文字才允许更低。
下表全部为**真实渲染值**（不是算 token 理论值），四舍五入两位。

| 组合 | 用途 | 比值 | 判定 |
|---|---|---|---|
| `text` / canvas | 正文 | **16.44** | ✓ |
| `text` / raised | 气泡正文 | **13.93** | ✓ |
| `text-2` / raised | 消息元信息、`.wb-meta`、`.tstep` | **7.72** | ✓ |
| `text-2` / sunken | 思维链正文、diff 输出 | **8.79** | ✓ |
| `text-3` / raised | `.chip`、`.dur`、`.ico`、`.wb-card small` | **5.96** | ✓ |
| `text-3` / sunken | `.diffstat`、`.rawjson`、`.copybtn` | **6.78** | ✓ |
| `text-3` / overlay | `.wb-dialog legend`、关闭 × | **5.40** | ✓（最紧的一档，仍达标） |
| `text-3` / sunken（placeholder） | 输入占位符 | 改前 `text-4` **3.40** → 改后 **6.78** | ✓（C-01） |
| `text-4` / raised | `.dot` 空闲（非文本控件，3:1 线） | 改前 **2.98** → 改后 `text-3` **6.47** | 已闭环（C-03） |
| `text-4`×.85 / canvas | `#status .st.off`（**可点**） | 改前 **2.86** → 改后 `text-3` **7.03** | 已闭环（C-02） |
| `ice` / raised | 链接、hover 字 | **8.51** | ✓ |
| `ice-ink` / ice | `#send`、`.primary` | **9.43** | ✓ |
| `gold` / sunken | `.wb-task` running/paused/blocked | **11.16** | ✓ |
| `gold-hi` / raised | `.msg.perm .who` | **11.65** | ✓ |
| `ok` / raised | `.tstat.ok` | **9.02** | ✓ |
| `danger` / raised | `.msg.error .who`、`.tstep.error`（改后） | **6.14** | ✓ |
| `danger-hi` / raised | `.errline`、`.wb-error`（改后） | **8.23** | ✓ |
| `danger-hi` / canvas+banner | `.banner` / `#wbError` 字 | **8.23** | ✓ |
| `warn` / sunken | `.cmdline` | **9.28** | ✓ |
| 身份色 / raised（最弱的一支） | `.msg.claudelink .who`（id-mirror，再乘 `.92` 透明度） | **4.75** | ✓（压线） |
| 身份色 / raised（用户气泡） | `.msg.user .who`（id-user） | **6.27** | ✓ |

结论：**改后 6 个状态、1154 个文本节点里，没有一条承载信息的文字低于 4.5:1**。
`10-shell.css` 里原本两个不达标项（可点开关 2.86、状态点 2.98）现在也都提到了 7.03 / 6.47，
全站最低的一档回到「`text-3` 在 overlay-2 上 = 4.85」，也就是 token 层给自己留的死线。

---

## 4. 未处置项与理由

### 4.1 `10-shell.css` 的六条（我一行没动，状态以你 20:00 的落盘为准）

| 条目 | 状态 |
|---|---|
| `#btnShare` 裸奔（P0） | **已闭环**（`10:299`） |
| `.box` 边线 `line-2` → `line`（P1） | **已闭环**（`10:320`） |
| `#status .st.off` → `text-3`、撤 `opacity`（P1） | **已闭环**（`10:319`） |
| `.dot` 空闲 → `text-3`（P1） | **已闭环**（`10:322`） |
| `.agent button / .box button` 与卡片同色（P1） | **仅完成一半**：`label.chk` 已落 `--nc-sunken`（`10:312`），但 `.agent button` / `.box button` 仍是 `raised`。它们的父级是 `.box`（raised），所以还是「同色按钮靠一根线分」。`.tt button` 不用动——它在 surface 上，raised 正好是一档抬起 |
| `#sidebar .chip` 死规则 | 建议顺手删（侧栏里没有 `.chip`，`index.html:392` 的 `.chip` 在 header） |

### 4.2 已由你补上 JS 钩子的（原计划交你，现记录结果）

- **`.wb-meta` 里的状态字**（原 P1）→ **已闭环**。`workbench.js:34` 现在输出
  `<span data-st="${stSlug(w.status)}">`，`30-workbench.css:379-386` 用与 `.wb-task span`
  同一套四桶（绿=做完 / 金=没做完 / 红=真失败 / 灰=未收录）。
- **`.wb-task` 整块左竖线**（原 P2）→ **已闭环**。既然 `:has()` 可用（项目已依赖 `color-mix`，
  说明 111+），用 `30:389` 的 `:has(span[data-st="error"])` 比再加一个 JS 类更省。
- **残留（P2）**：`workbench.js:46` 工具记录 / 推理摘要视图的 `<summary><small>${status}</small>`
  仍是裸状态文本（灰、无 `data-st`）。那里状态值来自上游原始字段，取值比 workflow 更杂，
  要不要统一，等你定。

### 4.3 我判断「不改更好」的

- `.perm button.ok/no` 的绿/红：见 2.1，属于已定口径之外的最小延伸，改了会让权限卡失去二元辨识。
- `.wb-actions .primary` 死规则：删除属于清理，不算一致性缺陷；留着不影响渲染，
  贸然删可能与工作台作者后续改动撞车。
- 12 个兼容别名 token：`index.html` 内联样式还在用（`--bg/--panel/--panel2/--line/--bar/--text/--muted/--user/--claude/--codex/--codebuddy/--dsh/--mirror`），
  一个都不能删；真正没人用的是 `--nc-gold-lo` 和 `--nc-gold-ink`（只报告，未删）。

---

## 5. 改动清单（只动颜色类属性，无 `!important`，无布局属性）

`20-chat.css`（16 条 `审计：` 注释 / 覆盖 20 条规则，一个注释管多条选择器时合并写）

| 选择器 | 改了什么 |
|---|---|
| `.tstep.error` / `.tstep.error .ico` | `warn` → `danger` |
| `.tstep.hasdetail:not(.open):hover .lb::after` | 新增 hover 态 |
| `.tstep .detail .filelink` / `:hover` | 默认 `ice`、hover `ice-hi`（原来是反的） |
| `.thtoggle:hover` | `ice-hi` → `ice` |
| `#targets button:hover` | 新增 `background: --nc-line` |
| `#pick:hover` | `ice-hi` → `ice` |
| `#text` | `border-color` `line`→`line-2`；`border-radius` 10px→`r-md` |
| `.edit` | `border-radius` 8px→`r-md` |
| `#send` | `border-radius` 10px→`r-md` |
| `.grid input/select/textarea` | `border-color` `line`→`line-2`；`radius` 8px→`r-sm` |
| `#mpModelRow/.mp-row` 控件 | `border-color` → `line-2`；补 `r-sm` |
| `.perm` | → `.perm:not(.msg)`（阻止污染气泡） |
| `.msg .perm` | 新增：气泡内权限卡落 `sunken` |
| `.att-chip` | ice wash/字 → raised + line-2 + 正文色 |
| `.att-item button` / `:hover` | 静态 `danger` → `text-3`，hover `danger-hi` |
| `.msg.hl` / `.msg.hl.user` | `box-shadow` 补 `inner-hi` |
| `#hlBar button` / `:hover` | 字 `gold` → `text`，hover 冰蓝 |

`30-workbench.css`（我改的 4 条；另 1 条是纯注释的层叠锁定）。**本节只列我改的**，20:00 之后你加的「验收修正」段（`data-st` 映射 + `:has`）不在本表内。

| 选择器 | 改了什么 |
|---|---|
| `#workbenchBar nav button.on` | `ice` → `ice-hi` |
| `.wb-card` | `box-shadow` 补 `inner-hi` |
| `.wb-error` | `danger` → `danger-hi` |
| `.wb-dialog input/textarea::placeholder` | `text-4` → `text-3` |
| `.wb-dialog button` | 只加注释，锁定与 `.wb-actions button` 的顺序依赖 |

自检：两文件括号平衡（157/57），无空规则；6 状态重跑探针 → 0 条 <4.5；
截图复核 `audit_conv / audit_agents / audit_dialog / audit_wb`，四屏表面阶梯与选中态均正常。

---

## 5. 验收处置（team lead，20:05 之后）

审计交回的待办逐条处置如下。全部写在 `10-shell.css` 末尾「验收修正」段或 `30-workbench.css` 末尾，**不改原作者结构**，同特异度靠加载顺序生效。

| 编号 | 处置 | 说明 |
|---|---|---|
| §4.1-1 `#btnShare` 裸奔（P0） | **已改** | 归入次级按钮族（sunken + line-2 + r-sm），hover 提字色与边线 |
| §4.1-2 `.agent/.box button` raised 叠 raised | **已改** | 下沉到 sunken，按钮与卡片分开 |
| §4.1-3 `.box` 边线 | **已改** | `line-2` → `line`，与 `.msg`/`.wb-card` 同级 |
| §4.1-4 `#status .st.off` 2.86:1 | **已改** | `text-4` → `text-3` 且撤掉 `opacity:.85`（它是可点开关，不是禁用） |
| §4.1-5 `.dot` 空闲 2.98:1 | **已改** | → `text-3` |
| §4.1-6 `#sidebar .chip` 死规则 | **保留** | 无害，删了会动到别人排版意图 |
| A-05 卡片 meta 状态与任务行不同色 | **已改（含 JS）** | `workbench.js` 的 `wb-meta` 状态包进 `<span data-st>`，`30` 层复用同一套映射 |
| F-03 失败任务左竖线仍金 | **已改（并扩大）** | 不止 error：done/running/paused/blocked/other 全部按状态上色，竖线与右侧状态字同源 |
| D-02 三处字面 scrim | **已改** | 新增 `--nc-scrim` / `--nc-scrim-2`，`.mask`、`.wb-dialog::backdrop`、`#drop` 全部换 token |
| F-01 `.wb-actions .primary` 死规则 | **保留** | 三个 ID 版覆盖了行为，删规则属清理，风险大于收益 |
| D-01 未引用 token（`gold-lo`/`gold-ink`） | **保留** | 作为色阶留白，不删 |
| §2.1-1 `.perm button.ok/no` 红绿 | **保留** | 「同意/拒绝」是权限卡的二元动作，红绿在此表达「放行/拒绝」而非「完成/失败」，登记为口径外最小延伸 |
| §2.1-2 `.msg.hl` 左条转金 | **保留** | 有意为之：标注是人加的最高优先级，压过说话人身份 |

改完复核：`10-shell.css` / `30-workbench.css` 括号平衡，`workbench.js` 语法通过（`new Function` 编译），四态截图（agents / conv / dialog / wb）表面阶梯与状态色一致。
