# Ghostty + tmux 工作流替代研究

核验日期：2026-11-02。结论基于本机生效配置、aipane 实现和文档、候选项目官方文档与发布记录。cmux 和 Zellij 的关键能力额外对照已发布 tag；本次没有进行候选软件的运行、压力或故障恢复测试。

**没有发现可以零改造、完整接管这套工作流的替代品。针对当前需求，优先评估 iTerm2 3.7.6：近期用普通 tmux attach 保留工作流；若长期目标是彻底移除 tmux，再验证纯 iTerm2 并迁移 aipane 的后端。** cmux 是 AI 工作区方向的第二候选。这个排序依据功能覆盖与迁移成本，不是故障率排名。

刚才的卡顿尚未定位根因。保留同一 tmux 服务端、只替换终端，有助于对照前端差异；如果阻塞实际发生在 tmux 服务端，这条路线也会受影响。

**当前实际需要替换的内容**

本机查询时：Ghostty 1.4.0（18457）、带本地图片 passthrough 修补的 tmux 3.7b；3 个会话、30 个窗口、58 个窗格，其中 33 个前台命令为 Codex，另有 Claude、Kimi、Grok、Yazi 等。iTerm2 3.7.6 已安装。窗格数量是动态快照。

| 序号 | 现有能力 | 依赖与迁移含义 |
| ---: | --- | --- |
| 1 | 终端关闭后任务继续，重新 attach | tmux 服务端托管 PTY 和子进程。新软件需要独立的进程托管机制，仅保存布局不够。 |
| 2 | 一条 ai 命令启动多个不同 Agent，指定分屏布局 | [cmd/pane.zsh](../cmd/pane.zsh) 直接调用 tmux new-window、split-window、select-layout 等命令。 |
| 3 | 原窗格精确重启或恢复 AI 会话 | [recovery-runtime.md](recovery-runtime.md) 绑定 socket、服务 PID、pane ID 和 AI session ID，带身份核验与失败重试。 |
| 4 | 跨工具准确显示 busy/idle | [tmux-window-wrap.md](tmux-window-wrap.md) 的活动状态来自各 Agent 生命周期与证据核验；不能用“终端有没有输出”直接替代。 |
| 5 | 多行窗口列表、活动动画、窗格计数 | [tmux-window-wrap.conf](../conf/tmux-window-wrap.conf) 是定制 UI，当前动画设为 20 FPS。其他软件一般需要重新设计信息展示。 |
| 6 | 实时广播和单窗格暂时不接收输入 | 当前是 synchronize-panes 加 select-pane -d/-e；“向多个 pane 发送一段命令”不是完全相同的交互。 |
| 7 | macOS 通知点击回到准确窗格 | [agent-notifications.md](agent-notifications.md) 和本机 focus-tmux.sh 使用 tmux 对象；后者还明确激活 Ghostty。换前端也需适配此处。 |
| 8 | Yazi 图片、中文输入、Shift+Enter、系统剪贴板 | 涉及终端协议、应用检测和实际输入链路，不能只对照软件功能表。 |
| 9 | 50000 行回滚、鼠标、vi 复制、窗口关闭保护与 popup | [tmux-workstation.conf](../conf/tmux-workstation.conf) 提供这些交互；原生 GUI 接管后不一定走同一条动作路径。 |

本次核对了 Ghostty 的 +show-config 和 tmux 的运行时 prefix/key table，而不只阅读配置注释。下面是已生效配置表达的动作链；未做物理按键端到端试验：

| 序号 | macOS 物理按键 | Ghostty 发送 | tmux 生效动作 |
| ---: | --- | --- | --- |
| 1 | Command + D | NUL 后接竖线 | C-Space prefix 后执行 split-window -h |
| 2 | Command + Shift + D | NUL 后接减号 | split-window -v |
| 3 | Command + Option + I | NUL 后接 B | 当前窗口切换 synchronize-panes |
| 4 | Command + Option + Shift + I | NUL 后接 e | 当前窗格 select-pane -d/-e，切换输入参与 |
| 5 | Shift + Enter | LF，即 0x0a | 继续交给前台应用；现有桥用于 AI CLI 换行 |

迁移时应按“物理键 → 候选终端动作或发送字节 → 是否经过 multiplexer → 前台应用结果”验收；避免让原生分屏快捷键和发送给 tmux 的快捷键同时占用相同组合。

**候选比较**

表内“保活”指关闭或失去 GUI 后，原进程仍由后台服务托管；不指电脑重启后还保留原进程内存。具体依据见后续各产品段落。

| 序号 | 路线 | 活进程保留 | 现有 aipane 兼容性 | 对当前工作流的判断 |
| ---: | --- | --- | --- | --- |
| 1 | iTerm2 + 普通 tmux attach | 由当前 tmux 保留 | 后端接口基本保留；键位桥、通知激活、图片需适配 | 近期优先试，改动最少，但仍有 tmux |
| 2 | 纯 iTerm2 3.7.6 | 独立 job servers；需正确设置退出行为 | 改用 iTerm2 session IDs/API，需要迁移 | 最值得验证的单软件路线 |
| 3 | iTerm2 + tmux -CC | 由 tmux 保留 | 后端保留，原生 UI 与现有 popup/关闭保护不等价 | 中间路线，不应直接替换全部日常会话 |
| 4 | cmux GUI | 默认恢复布局/会话；local-tmux 可提供保活 | surface/workspace IDs 与 tmux IDs 不同 | AI 工作区很贴合，但默认不是无损替代 |
| 5 | WezTerm + Unix mux domain | 显式配置独立 mux 后保留 | 需要 WezTerm 后端适配；广播有缺口 | 可迁移的技术底座，未必减少维护 |
| 6 | Zellij 稳定版 + 终端 | 可 detach/attach | 需要重写 tmux 适配；当前 Yazi 有已知问题 | 不是当前最合适的优先路线 |
| 7 | kitty 单独使用 | 未提供同等的独立会话后端 | 需迁移 API，保活还需其他组件 | 图片和自动化强，但缺关键保活能力 |

**为什么优先 iTerm2**

本机已装 3.7.6，官方当前稳定下载同为该版本，构建于 2026-10-15。它的能力已不限于传统 tabs/splits；3.7 系列发布记录包含 it2、Agent 状态和 Cockpit 等集成。版本存在不等于当前配置已经启用这些功能。[官方发布记录](https://iterm2.com/downloads.html)

纯 iTerm2 的 Session Restoration 把任务交给长期存活的 server，GUI 崩溃或升级后可接回。默认 Command + Q 会结束任务；需要按官方要求配置系统窗口恢复，并关闭 Advanced 中“User-initiated quit (cmd-q) of iTerm2 will kill all running jobs”。系统重启仍会结束进程。主动关闭窗格也不能等同 detach。[Session Restoration](https://iterm2.com/documentation-restoration.html)

it2 可枚举、创建、分屏、发送输入、设置状态和订阅事件，因此有接口承接 aipane 的编排。它不会自动识别或兼容 TMUX_PANE。[it2 官方说明](https://iterm2.com/documentation-utilities.html)

Session Status 可由程序、CLI 或触发器上报；Cockpit 汇总不同窗口的状态并跳转。它适合替代部分多行状态栏职责，但你的跨工具 busy/idle 语义、子 Agent 归属和迟到事件处理仍需要保留并接到这个展示层。[Session Status 与 Cockpit](https://iterm2.com/documentation-session-status.html)

iTerm2 支持实时广播，也能逐 session 切换是否接收；Python API 可指定接收者集合。这与当前“广播 + 单窗格排除”的需求接近。[广播菜单](https://iterm2.com/documentation-menu-items.html#shell-broadcast-input-)、[Broadcast API](https://iterm2.com/python-api/broadcast.html)

图片方面，当前 iTerm2 支持自有 inline image 协议和 Kitty graphics；文档还说明了适用于 tmux integration 的 multipart 传输。Yazi 的支持表仍将 iTerm2 默认适配列为 inline image 协议，并按环境选择驱动，所以需要分别验证普通 attach、-CC 和原生 pane，不能假定同一条图片路径。[iTerm2 图片](https://iterm2.com/documentation-images.html)、[Yazi 图片适配](https://yazi-rs.github.io/docs/image-preview/)

这形成三个不同选择：

1. 普通 attach：先用新前端连接既有 tmux，保留后台进程和窗格身份。主要工作是移植 Cmd 键位桥、调整通知激活目标、检查图片与剪贴板。它适合控制迁移范围并对照卡顿。
2. -CC：让 iTerm2 的原生标签和分屏映射到 tmux 对象，仍使用 tmux 保活。原生关闭动作会结束相应对象，不能假定经过当前 prefix+x 的 AI 关闭确认。自定义 display-popup、三行状态栏与鼠标交互要单独验证。[tmux integration](https://iterm2.com/documentation-tmux-integration.html)
3. 纯 iTerm2：可以去掉 tmux 渲染和控制层，但需要迁移 aipane 的编排、窗格身份、活动上报、通知路由与恢复执行器。终端 GUI 的进程保留机制不会自动代替多种 Agent 的精确会话恢复。

-CC 确有显示 tmux 状态栏内容的设置，但“显示内容”不等于已证明现有多行交互完全兼容。官方也说明，高输出下 integration 客户端有自己的暂停/追赶策略，需纳入重负载验收。[General 设置](https://iterm2.com/documentation-preferences-general.html)

**cmux 的适配度与限制**

核对的已发布版本是 v0.65.2.2（2026-10-27）。它使用 libghostty，因此更换应用组织层并不意味着完全更换了终端渲染实现；这点不能作为故障归因，只影响试验时如何解释结果。[发布 tag](https://github.com/manaflow-ai/cmux/releases/tag/v0.65.2.2)、[该版本 README](https://github.com/manaflow-ai/cmux/blob/v0.65.2.2/README.md)

cmux 的 workspace/surface CLI、侧栏状态和通知跳转与多 Agent 工作流很接近。接口可以创建分屏、发送输入、聚焦目标并上报状态。[CLI](https://cmux.com/docs/api)、[Notifications](https://cmux.com/docs/notifications)

默认 GUI restore 恢复布局、目录和尽力保存的滚动内容；受支持 Agent 根据原生 session ID 执行 resume。它不保存任意活进程。官方支持表没有列出 Kimi、dsh，这意味着它们的同等自动恢复需要另外验证或接入，不能把“支持多个 Agent”理解为覆盖你的所有工具。[Session Restore](https://cmux.com/docs/session-restore)

已发布的 local-tmux 是显式启用的持久 profile，用自己的 socket/registry 托管本机 tmux，会覆盖 GUI quit/crash/update。它仍依赖 tmux，也不会自动接管现有会话；默认新会话的 10000 行历史还需对齐当前 50000 行需求。[已发布版 local-tmux 文档](https://github.com/manaflow-ai/cmux/blob/v0.65.2.2/docs/local-tmux.md)

cmux TUI 是另一条带独立 owner 的 mux 路线，已发布 tag 的配置默认启用 detached owner；它仍需要外层终端，并使用自己的对象/API。文档开头保留了与后文及默认配置不一致的旧描述，因此本次以同 tag 的默认配置与 durable-server 段落交叉确认。[TUI 文档](https://github.com/manaflow-ai/cmux/blob/v0.65.2.2/cmux-tui/docs/getting-started.md)、[默认配置实现](https://github.com/manaflow-ai/cmux/blob/v0.65.2.2/cmux-tui/crates/cmux-tui/src/config.rs)

没有核实到 cmux GUI 与当前实时广播、逐窗格临时排除完全等价的入口。CLI 循环 send 可做批量投递，但不足以证明逐键同步。它的有限 tmux shim 也不是完整 tmux API 兼容层，因此不能承诺现有 aipane 直接运行。[CLI 文档](https://cmux.com/docs/api)、[已发布 CLI 实现](https://github.com/manaflow-ai/cmux/blob/v0.65.2.2/CLI/cmux.swift)

**WezTerm：完整架构，仍有迁移与发行取舍**

WezTerm 同时提供终端、Lua 和独立 mux。必须显式连接 Unix domain；默认 Local domain 不能当成“关 GUI 仍保活”。工作区与 tmux session/window 的映射也要重新定义。[Multiplexing](https://wezterm.org/multiplexing.html)、[Workspaces](https://wezterm.org/recipes/workspaces.html)

CLI 有 pane 定位、分屏、发送文本、读文本和聚焦；pane user vars 可以把活动状态传给 Lua 展示。这些能力足以开发适配层，但不兼容现有 tmux 调用。[CLI](https://wezterm.org/cli/cli/index.html)、[User vars](https://wezterm.org/config/lua/pane/get_user_vars.html)

实时广播需求仍在官方 issue #2658 中开放。send-text 或第三方插件可以实现部分群发；未验证它们等价于当前实时广播加输入排除。[广播需求](https://github.com/wezterm/wezterm/issues/2658)、[send-text](https://wezterm.org/cli/cli/send-text.html)

截至核查，latest 正式版仍是 20240203-110809-5046fc22，主线/nightly 仍在更新，因此应理解为正式发行间隔较大，而不是项目停止维护。[Releases](https://github.com/wezterm/wezterm/releases)、[macOS 下载](https://wezterm.org/install/macos.html)

macOS 用户报告 #8217 描述了长时间运行后 GUI 冻结、重开空白而 CLI 仍响应的相似症状。它没有稳定复现，不能代表整体故障率，但足以说明“换成 WezTerm 就能免除这类卡顿”没有依据。[具体报告](https://github.com/wezterm/wezterm/issues/8217)

**Zellij：新能力已发布，但不宜忽略 Yazi**

最新稳定版为 v0.45.1（2026-08-28）。0.45.0 已加入 Kitty graphics；“Zellij 只能 Sixel”的旧结论不适用。当前稳定版仍需要终端宿主，桌面客户端位于 main 的 Unreleased 区，不能计入稳定版替代能力。[v0.45.1](https://github.com/zellij-org/zellij/releases/tag/v0.45.1)、[0.45.0 说明](https://zellij.dev/news/nested-sessions-kitty-graphics-new-ui/)、[本次核验的 changelog](https://github.com/zellij-org/zellij/blob/65a22e070444d38488d93398d74eba52c3d316c1/CHANGELOG.md)

Zellij 可 detach/attach；Session Resurrection 重建布局与程序，不自动恢复多 Agent 的精确对话身份。它提供 pane CLI 与 WASM 插件，但意味着重写 tmux 后端和状态栏。[会话恢复](https://zellij.dev/documentation/session-resurrection.html)、[CLI actions](https://zellij.dev/documentation/cli-actions.html)、[Plugins](https://zellij.dev/documentation/plugins.html)

布局属性 exclude_from_sync 可排除 pane；尚未找到等同当前 select-pane -d/-e 的运行时单 pane 切换接口。[稳定版布局测试](https://github.com/zellij-org/zellij/blob/v0.45.1/zellij-utils/src/input/unit/layout_test.rs#L1105)

Yazi 官方当前仍提示 Zellij KGP 预览问题。#5573 在 0.45.1 复现，维护者确认修复进入 main，但核查时没有更新的稳定 release。因此“支持协议”和“你的 Yazi 路径已修好”要分开判断。[Yazi 对 Zellij 的说明](https://yazi-rs.github.io/docs/image-preview/#zellij-users)、[修复记录](https://github.com/zellij-org/zellij/issues/5573)

**kitty 和其他候选**

kitty 的图片、remote control、可筛选广播都很适合本地终端工作台。其 sessions 文件保存布局与启动信息，加载时重新运行程序；--detach 是脱离启动终端，detach_window 是移动 GUI 窗格，两者都不等于 tmux 的后台保活。[Sessions](https://sw.kovidgoyal.net/kitty/sessions/)、[CLI](https://sw.kovidgoyal.net/kitty/invocation/#cmdoption-kitty-detach)、[Broadcast](https://sw.kovidgoyal.net/kitty/kittens/broadcast/)

当前维护者资料仍把独立 daemon/persistence 作为尚未覆盖的需求。若配 tmux、zmx 等组件可以补齐保活，但那是新的组合方案，也需要迁移身份与编排。[kitty FAQ](https://sw.kovidgoyal.net/kitty/faq/#i-am-using-tmux-zellij-and-have-a-problem)、[维护者答复](https://github.com/kovidgoyal/kitty/issues/9318)、[zmx 官方定位](https://zmx.sh/)

Warp 的 Session Restoration 明确保存窗口、pane 和 Blocks 数据；这不能证明保留活进程。它的 Synchronized Inputs 以完整输入内容同步为中心，也不应直接当成现有逐键广播的替代。对已经使用多种独立 Agent CLI 的你，目前没有足够理由把它排在 iTerm2/cmux 前面。[Warp 会话恢复](https://docs.warp.dev/terminal/sessions/session-restoration)、[同步输入](https://docs.warp.dev/terminal/entry/synchronized-inputs)

Wave 的 Durable Sessions 明确面向远程 SSH，本地终端仍是随 Wave 运行的 standard session。它的图形 block、文件与网页工作区很有特色，但没有由此满足当前本机长活任务的核心条件。[Durable Sessions](https://docs.waveterm.dev/durable-sessions)、[wsh](https://docs.waveterm.dev/wsh)

Wakterm 是面向 mux 与 Agent 的 WezTerm fork；文档描述持久布局与 Claude/Codex 恢复，但尚未覆盖你全部工具。核查时 changelog 提到 v0.1.0，而 GitHub Releases 页面及 API 为空，安装交付状态需要进一步确认，适合观望。[官方定位](https://wakterm.org/)、[Agent 生命周期](https://wakterm.org/agent-lifecycle/)、[发布页](https://github.com/wakamex/wakterm/releases)

Ghostty 1.4 官方明确表示 tmux control mode 尚未接入 GUI，因此当前也不能用它直接获得 iTerm2 -CC 那种原生 tmux 界面。[1.4 release notes](https://ghostty.org/docs/install/release-notes/1-4-0)

**建议的决策和验证顺序**

近期先验证 iTerm2 普通 attach：这是保留现有进程、脚本和操作语义的最小迁移路径。它可以回答“替换前端是否改善体验”，但一次无卡顿不能证明根因已解决。

若目标是长期减少 tmux 定制维护，则用一个独立的三窗格工作区验证纯 iTerm2，另以 cmux 作对照。先迁移最小闭环：启动 Agent → 上报准确忙闲 → 通知返回正确窗格 → GUI 重开任务仍在 → 按原会话 ID 恢复。不要仅凭分屏和外观就判断完整迁移成功。

| 序号 | 验收场景 | 通过标准 |
| ---: | --- | --- |
| 1 | GUI 退出、崩溃与重新打开 | 独立测试任务的 PID 和运行进度保持；退出、关闭 pane、detach 分别测试 |
| 2 | 批量启动 Codex、Claude、Kimi、dsh 等 | 正确目录、布局、模型与会话 ID；只启动 shell 不算恢复成功 |
| 3 | 忙闲与完成事件 | 运行中、审批等待、后台子 Agent、失败及取消均不误标空闲；旧事件不覆盖新任务 |
| 4 | 通知点击 | 跨窗口和工作区后仍回到原目标；窗格重建或改名不误跳 |
| 5 | 广播和排除 | 普通文字、中文组合输入、Shift+Enter、粘贴与方向键正确；排除窗格确实不接收 |
| 6 | Yazi 与图片 | 在新环境中检查实际驱动；切图、缩放、滚动、切换 pane、重连后无残留或错位 |
| 7 | 关闭与恢复 | AI 关闭保护仍符合预期；服务重启和系统重启后的恢复单独验证，不用 GUI 重连代替 |
| 8 | 实际规模 | 在接近 30 窗口、58 窗格、50000 行回滚的输出负载下记录 CPU、内存、输入延迟，并连续使用观察 |

如果纯 iTerm2 通过这些条件，它才是可以逐步承接当前工作流的替代方向。若主要不满是偶发卡顿，而不想承担适配成本，保留现有后端、比较 iTerm2 前端更合适。

本次工作产物为本文；用于交叉核验的源码位于 /Users/alfheim/code/oss/cmux 和 /Users/alfheim/code/oss/zellij。候选软件未安装或试运行，本文没有把功能核验写成稳定性实测结论。
