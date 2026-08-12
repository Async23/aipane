# AI 会话恢复设计备忘（session-restore）

> 目标：tmux server 因**升级重启**或**意外崩溃**退出后，尽可能完美地恢复工作区，
> 尤其是各 pane 里的 **AI agent CLI 会话**（以及普通 shell / lazygit / yazi 等）。
> 本文记录已敲定的事实、决策与设计不变量，供后续在 aipane 中落地。
>
> 状态：核心恢复链路与原地批量重启均已实现；后文保留历史决策和验证记录。

## 当前使用方式

- tmux-resurrect 保存结构后，`ai-restore` 负责在原 pane 续接 AI 会话。
- `ai-restart --dry-run` 刷新快照并预览本次可恢复的 pane。
- `ai-restart` 确认后批量重生这些 pane，再调用 `ai-restore`。
- `prefix A`（默认是 `Ctrl-Space` 后按 `A`）可在 popup 中执行，不需要手动逐个退出。
- 默认检测到任一 `busy` Agent Activity 就整体中止；旧进程显示 `unknown` 时，popup
  要求明确确认所有任务已空闲。非交互覆盖必须使用 `--force`。
- Qoder 与 Droid 暂不在恢复范围内。

## 0. 一个绕不开的前提

tmux pane 里的进程都是 tmux server 的子进程；server 一退出（`kill-server` 或崩溃），
这些进程全部收到 SIGHUP 死亡。因此必须把目标拆成两个**互相独立**的问题：

| 序号 | 问题 | 内容 | 谁负责 |
|---|---|---|---|
| 1 | 结构 | session/window/pane、布局、cwd、屏幕文字 | tmux-resurrect + tmux-continuum |
| 2 | 活状态 | AI 对话上下文、shell 环境、TUI 内部状态 | 进程一死即失；只能靠“应用层落盘 + resume”重建 |

本方案聚焦问题 2 中最有价值的部分：**把 AI 会话按正确的 pane 重新 resume 回来**。
（已明确**不采用 abduco/dtach** 保活方案——用户决定。）

## 1. 各 AI 工具的 session id 生成时机（实测）

方法：全新空目录中“只启动、答掉信任提示、不发任何消息”，观察各自 store。

| 序号 | 工具 (aipane key) | id 生成时机 | store 路径 / id 形态 |
|---|---|---|---|
| 1 | cursor-agent (`r`) | **启动即生成（急切）** | `~/.cursor/chats/<工程hash>/<chat-uuid>/meta.json` |
| 2 | pi (`p`) | **启动即生成（急切）** | `~/.pi/agent/sessions/<编码cwd>/` |
| 3 | qodercli (`q`) | **启动即生成（急切）** | `~/.qoder/projects/<编码cwd>/<uuid>/state.json` |
| 4 | codex (`x`) | **首条消息才生成（懒惰）** | `~/.codex/sessions/YYYY/MM/DD/rollout-<ISO>-<uuidv7>.jsonl` |
| 5 | grok (`g`) | **首条消息才生成（懒惰）** | `~/.grok/sessions/<urlencode cwd>/<uuidv7>/` |
| 6 | claude (`c`) | **首条消息才生成（懒惰）** | `~/.claude/projects/<dash编码cwd>/<uuidv4>.jsonl` |
| 7 | opencode (`o`) | **首条消息才生成（懒惰）** | `~/.local/share/opencode/`（sqlite + storage/） |
| 8 | kimi (`k`) | **首条消息才生成（懒惰）** | 启动仅建全局 search-index；单会话 store 位置未定 |
| 9 | droid (`d`) | 未安装，未实测 | — |

要点：懒惰派“没发消息就崩溃”时**根本没有会话被创建**，也就没有东西可丢/可恢复。

## 2. 各 AI 工具的钩子/插件能力（实测 `--help`）

| 序号 | 工具 (key) | 钩子/插件 | 说明 |
|---|---|---|---|
| 1 | claude (`c`) | **原生 hooks + plugins** | `--include-hook-events`、`--bare` 跳过 hooks/LSP/plugin |
| 2 | codex (`x`) | **原生 hooks + plugins** | 带信任模型（`--dangerously-bypass-hook-trust`） |
| 3 | qodercli (`q`) | **原生 hooks + plugins** | `hooks|hook`、`plugins|plugin` 子命令 |
| 4 | opencode (`o`) | **plugins（JS 事件插件）** | `opencode plugin`、`--pure` 关闭外部插件；**已用 `chat.message` 插件落地，见 §14** |
| 5 | grok (`g`) | **plugins + 市场**（未见显式 hooks） | 需查插件能否订阅会话生命周期 |
| 6 | cursor-agent (`r`) | **plugins**（未见显式 hooks） | 急切派，无需钩子 |
| 7 | pi (`p`) | 无 | 急切派，无需钩子 |
| 8 | kimi (`k`) | ~~无（仅 MCP）~~ → **有原生 `[[hooks]]`（13 事件，stdin JSON 带 `session_id`）** | **订正**：非无钩子；已用 `SessionStart` 钩子落地，见 §14 |

## 3. 核心决策

### 3.1 钩子取舍：只给“需要且能用钩子”的懒惰派上钩子

原则：**钩子的唯一不可替代价值 = “在 id 被创建那一刻把它告诉你”。**
急切派 id 启动即存在，用钩子换不到正确性、只增成本 → **急切派一律不用钩子**。

> **§12 已核实（2026-08-08）：核心 4 家里 pi / grok / claude 都支持 `--session-id` 预指定，
> cursor 用 `create-chat` 预取 id → 这 4 家全部由 aipane 掌控 id、无需钩子/探测；
> 只有 codex 无预指定旗标、仍需钩子。急切/懒惰之分对 gold-case 已无意义。**

| 序号 | 工具 | 机制决策 |
|---|---|---|
| 1 | claude (`c`) | **gold-case**（`--session-id <uuid>`）→ aipane 预指定 id，不用 hook/探测（见 §12） |
| 2 | codex (`x`) | **核心工具中唯一非 gold-case**：懒惰 + 无预指定旗标 → **用 hook**（见 §12） |
| 3 | opencode (`o`) | 懒惰 + 事件插件 → **用插件**（未纳入本轮核实） |
| 4 | grok (`g`) | **gold-case**（`-s/--session-id <uuid>`，仅新建）→ aipane 预指定 id（见 §12） |
| 5 | kimi (`k`) | 懒惰 + 无钩子 → **回落**（cwd+时间解析，未纳入本轮核实） |
| 6 | cursor (`r`) | **半 gold-case**：无旗标，但 `create-chat` 回吐 uuid → 先建后 `--resume`（见 §12） |
| 7 | pi (`p`) | **gold-case**（`--session-id <uuid>`，不存在则建、幂等，见 §11/§12） |
| 8 | qoder (`q`) | 急切（虽有 hook）→ **store 探测，不用钩子**（未纳入本轮核实） |

`ai x resume <id>` 这类 **resume 启动是最省事的一档**：id 在命令里，启动即可直接绑定，
无需 store 探测/钩子。gold-case（§12）把这一档从「resume 时」提前到「首次启动时」。

### 3.2 绑定机制：aipane 注入关联令牌 + 统一接收脚本

aipane 是所有 AI 启动的**唯一咽喉**。启动时给命令注入环境变量：

```zsh
AIPANE_LAUNCH=<uuid> AIPANE_PANE=<pane> AIPANE_TOOL=x  codex --yolo
```

- 钩子/插件进程由 AI CLI 拉起，**继承该 pane 的环境**（含 `$TMUX_PANE` 与上面的令牌）。
- 钩子只需回报：`aipane-bind "$AIPANE_LAUNCH" "<该工具的 session_id>"`。
- 每个工具唯一的差异 = “session_id 字段叫什么”；令牌/pane/写注册表全由共享脚本 `aipane-bind` 完成。
- 急切派无钩子：aipane 启动后按 `编码cwd + 启动时间` 直接从 store 读出刚建的会话 id。

## 4. 两条设计不变量（铁律）

### 铁律一：动态绑定（last-writer-wins）

一个 pane 一生可顺序住过多个会话（codex→退出→grok→退出→`ai x resume C`……）。
因此绑定语义是「**pane → 此刻在跑的会话**」，非「一次绑死」：

- 每次 `ai` 启动都对该 pane 条目 **upsert 覆盖**。
- 注册表键用 **`session:window.pane`**（稳定；`%id` 重启会变）。
- 附记 `tool`、`启动时间`（同 cwd 顺序多会话消歧用）。

### 铁律二：恢复时与 resurrect 快照对账

**绝不盲信注册表**，以 resurrect 记录的“该 pane 实际在跑什么命令”（`pane_current_command`
+ 完整命令行）为准：

| 序号 | pane 历史 | 快照时实际在跑 | 对账结果 |
|---|---|---|---|
| 1 | codex(A) → 退出，停在 shell | `zsh` | 无 AI → 不 resume（A 视为过期） |
| 2 | codex(A) → 退出 → `ai x resume C` | codex | resume **C** |
| 3 | codex(A) → 退出 → `ai g` | grok | resume grok 会话（工具以快照为准） |
| 4 | 绕过 aipane 手敲 `codex` | codex | 工具一致但 id 可能对不上 → best-effort |

即：**“是否恢复 / 恢复成哪个工具”以快照命令为准；注册表只负责补 `session_id`。**
过期绑定（退出变 shell / 中途换 AI）由此自动作废——**故急切派无需退出钩子**。

## 5. 残留边界（诚实记录）

- **绕过 aipane 启动**、或同 pane 同工具快速换新会话且不经 aipane：注册表 id 可能过期。
  对策：按 `cwd + 晚于上次启动时间的最新会话` 重解析；实在对不上就开新的。
- **整机重启/注销**：一切进程皆亡，只能走 id-resume 重建（这正是本方案主线）。
- **CLI 自身崩溃**：与 tmux 无关，本方案不处理。
- 加分项（非必需）：有 SessionEnd/退出钩子的懒惰派（claude/codex）可在退出时清除该 pane 绑定，
  作为卫生优化；正确性已由铁律二覆盖。

## 6. 第 1 层：resurrect/continuum 配置（结构安全网，先行且低风险）

个人 `~/.tmux.conf` 目前把自动保存关了（`@continuum-save-interval '0'`）、文字捕获也关了，
导致最新存档停在手动存的那次。建议：

> **实现修正（见 §13）**：本机 resurrect **有** `@resurrect-hook-post-restore-all` 钩子，
> 故不必重绑 C-r；且 resurrect 会为**每个** pane 存下完整命令行，gold-case 注入的 id
> 天然落入存档 → `ai-restore` 直接解析存档即可。gold-case 4 家**不需要坐标快照**；
> `aipane-snapshot` 仅为 **codex**（id 不在命令行）保留。下面是**已实际落地**的 `~/.tmux.conf`：

```tmux
set -g @resurrect-capture-pane-contents 'on'   # 保留每个 pane 的文字（含 chat 标题/含 id 的命令行）
set -g @continuum-save-interval '15'           # 每 15 分钟自动存（原为 0 = 关闭 → 存档陈旧）
set -g @continuum-restore 'off'                # 结构恢复保持手动（prefix+Ctrl-r）；ai-restore 自动跟上
# 非 AI、命名唯一的程序：交给 resurrect 白名单直接重开；切勿加入 node（见 §9-D1 坑）
set -g @resurrect-processes 'lazygit yazi ssh top htop'
# 结构恢复完成后自动 resume 各 pane 的 AI 会话（解析存档命令行，仅恢复有确切 id 的）
set -g @resurrect-hook-post-restore-all '$HOME/.local/bin/ai-restore'
```

`history-limit` 已由 `conf/tmux-workstation.conf` 设为 50000（够大），本方案不再改动。
存档目录：`~/.local/share/tmux/resurrect/`（`last` 软链指向最新一份）。

## 7. 待验证项（实现前必须确认）

| 序号 | 事项 |
|---|---|
| 1 | claude / codex / opencode 钩子的**具体事件名**、回调载荷是否含 `session_id`、能否执行任意命令 → opencode **已实证**（`chat.message` 带 `sessionID`，插件可 spawn 命令），见 §14 |
| 2 | grok 插件能否订阅“会话开始/首条消息”并拿到 session_id |
| 3 | 各工具在本机当前版本下的**真实 resume 语法**（codex 用 `resume <id>` 子命令；grok `--resume <id>`；cursor `resume <chat-id>`；claude `--resume <uuid>` / `--continue`；pi 已实测=见 §11；各不相同且随版本变，且需保留原始 flag 如 `--yolo`/`--always-approve`） |
| 4 | ~~kimi 单会话 store 的真实位置与 id 形态~~ → **已实证**：`~/.kimi-code/sessions/wd_<name>_<hash>/`（**按 cwd 分目录**，故 `--continue` 可靠）；id 经 `SessionStart` 钩子 stdin JSON 拿到，见 §14 |
| 5 | ~~核实 codex / grok / claude / cursor 是否有类似 pi `--session-id` 的旗标~~ → **已核实，见 §12**：grok/claude 有 `--session-id`（仅新建），cursor 有 `create-chat`（回吐 id），codex 没有 |

## 8. 建议的落地顺序（§12 定案后修订：gold-case 主线先行、codex 走钩子支路）

1. 先做第 6 节的 resurrect/continuum 配置（马上见效、近零风险）。
2. **gold-case 主线**：aipane 为 pi/grok/claude 各写「生成 uuid → 注入 `--session-id` 启动 →
   记录 `(%N → uuid)` → resume 命令映射」；cursor 走「`create-chat` 预取 id → `--resume` 启动」。
   这 4 家**无需钩子、无需 store 探测、无竞态**。
3. 实现铁律二「恢复对账」+ `ai-restore`：读 resurrect 快照的命令行 → argv 解析认工具 →
   按注册表补 id → `send-keys` 跑各家 resume 命令。
4. ~~codex 支路~~ **已落地**（见 §13「codex 支路」）：codex `notify` 包装器记 `thread-id` +
   `aipane-snapshot` 坐标快照 + ai-restore 三级 id 优先级。
5. ~~余下非核心工具按 §3.1 补齐：opencode（插件）/ qoder（探测）/ kimi（回落）~~
   → **opencode / kimi 已落地，见 §14**（均走钩子支路，非回落）；qoder 待补。

## 9. 决策定案（2026-08-08）

### A. 恢复编排
| 序号 | 决策 |
|---|---|
| A1 | ~~更正（resurrect 无 post-restore 钩子）：重绑 `prefix+Ctrl-r`~~ → **再修正见 §13**：resurrect **有** `post-restore-all` 钩子，已改用它挂 `ai-restore`，**不再重绑 C-r**；`ai-restore` 仍可独立手动运行 |
| A2 | resume **自动执行**，不逐个确认（已接受随之而来的 token / `--yolo` 成本） |
| A3 | **不做**分批 / 限流（降低实现复杂度） |
| A4 | 因无 post-restore 钩子，`ai-restore` 经恢复键链式触发，可能在 restore 尚未 settle 时启动 → `ai-restore` 需轮询 / 短延迟等各 pane 稳定后再 `send-keys` |

### B. resurrect / continuum
| 序号 | 决策 |
|---|---|
| B1 | 结构恢复 **手动**（`@continuum-restore 'off'`，`prefix+Ctrl-r`）；AI resume 由 post-restore 钩子自动跟上 |
| B2 | `@continuum-save-interval '15'`（暂不深究当年 CPU 根因，一次只干一件事） |
| B3 | 「间隔」即 B2（15min）；另定 `history-limit 10000`（scrollback 行数，决定被捕获文字量与每 pane 内存） |

### C. 注册表
| 序号 | 决策 |
|---|---|
| C1 | 位置 `~/.local/share/aipane/registry.jsonl`（落盘，扛崩溃） |
| C2 | 并发：append-only JSONL + `flock` 原子追加；读取按键取最后一条（last-writer-wins） |
| C3 | GC：`ai-restore` 运行时顺带压缩——只保留仍存活 pane 的每键最新条目，失效条目丢弃 |
| C4 | **定案见 §10**：两层身份（server 存活期用 `%N`；跨重启用“存盘同刻的坐标快照”）+ resume 启动的命令行自带 id |

### D. 非 AI 内容（目标：尽量收集、无感恢复）
| 序号 | 决策 |
|---|---|
| D1 | 分两路收集：① 非 node、命名唯一者（lazygit/yazi/ssh/top…）→ resurrect `@resurrect-processes` 白名单直接重开；② **坑**：cursor-agent / pi / vite 都是 `node`，无法靠进程名区分 → **绝不把 `node` 加入白名单**；所有 node 型 pane 交给 `ai-restore`：argv 解析后 AI→resume、非 AI node（如 vite）→原样重跑 |
| D2 | 普通 shell 只恢复 cwd；venv / 环境变量 / 后台任务不恢复。**默认不做**「每 pane 重初始化」（过脆弱、收益低）。可反悔 |
| D3 | **定案见 §11**：pi 是一等 AI，且为唯一 gold-case（支持 `--session-id` 预指定 id）；aipane 启动注入 `--session-id`，resume 亦用之 |

### E. 边界 / 收尾
| 序号 | 决策 |
|---|---|
| E1 | 记录的 id 已失效 → 回落为「用 aipane 开新会话」并提示 |
| E2 | 单账号；忽略 claude 多账号 |
| E3 | （说明，非决策）判断 pane 在跑什么时读 resurrect 存的命令行，node 型解析 argv 认工具，与 store 探测复用同一逻辑 |
| E4 | 直接在真实环境测试（现状已清空，正好整治） |
| E5 | **不做** `tmux-upgrade` 脚本；升级前手动 `prefix+Ctrl-s` 即可 |

### 待验证补充
- ~~确认 post-restore 钩子名~~ → **已确认 resurrect 无 post-restore 钩子**；A1 已改为重绑恢复键链式触发。
- 确认 `@resurrect-hook-post-save-layout` 传入的存档文件路径参数格式与执行环境。

## 10. C4 定案：注册表键的健壮性

> **v1 修正见 §13-修正二**：resurrect 存档已含每个 pane 的完整命令行（gold-case id 天然在内），
> `ai-restore` 直接按存档坐标恢复即可，v1 **不实现** `aipane-snapshot` 坐标快照。以下坐标快照方案
> 留作 codex 无 id 等未来增强的备用设计。

背景（本机实测）：`renumber-windows on`（关窗即重编号）+ `automatic-rename on`（窗口名随程序变）
→ 绑定时记的 `session:window.pane` 会频繁漂移，naive 键确实脆。
且 resurrect **只有 pre-restore 钩子、无 post-restore**，但**有 `post-save` / `post-save-layout`**
（后者把刚写的存档文件路径作为参数传入）——这是解法的钥匙。

### 两层身份
| 序号 | 层 | 键 | 为何稳 |
|---|---|---|---|
| 1 | server 存活期 | 注册表用 **pane 唯一 id `%N`**（`$TMUX_PANE`） | `%N` 随 pane 对象终身不变；重命名/重编号/换位皆不影响；aipane 与钩子都能拿到 |
| 2 | 跨重启 | **存盘同刻的坐标快照** `session:win.pane → session_id`，经 `post-save-layout` 与该 dump 配对 | 见下 |

### 为何抗漂移（关键）
resurrect 恢复的是**上一次存档**；坐标快照取自**同一次存档的同一刻**。存档之后的任何重编号/改名，
resurrect 自己也看不到（它只恢复存档时刻）。故快照里的坐标 = resurrect 会重建的坐标，**天然一致**，
`renumber-windows on` 变得无关紧要——从不指望坐标跨 churn 存活，而是每次存盘都锁定到 resurrect 的 dump 重记一次。

### 恢复时匹配优先级
| 序号 | 优先级 | 机制 |
|---|---|---|
| 1 | 最稳 | resume 启动的命令行自带 id（`grok --resume <id>`），resurrect 原样存下 → 不依赖坐标/快照 |
| 2 | 主路 | 按恢复出的 `session:win.pane` 查配对快照 → session_id → resume |
| 3 | 回落 | 快照缺失（churn / 手敲）→ 内容匹配（cwd + tool + 时间窗） |
| 4 | 兜底 | 仍无 → 用 aipane 开新会话并提示 |

### 残留
上次存盘 ~ 崩溃之间新开/改动的 pane，落到优先级 3/4。这是 15 分钟存档模型的固有损失，resurrect 本身亦然。

### aipane-snapshot 要点
- 由 `@resurrect-hook-post-save-layout` 触发（拿到存档文件路径用于配对命名）。
- 遍历现存 pane：`%N` → 现坐标 + cwd + 当前命令；用注册表（`%N → session_id`，含钩子已解析出的 id）补 `session_id`。
- 懒惰派若尚未发首条消息（无 id）→ 跳过（本就无会话可恢复）。
- 产出一份与 resurrect `last` dump 配对的坐标文件（如 `~/.local/share/aipane/coords-<dump时间戳>.json`）。

## 11. D3 定案：pi 的处理

pi = **AI coding assistant**（完整会话模型），按一等 AI 处理，且是所有工具中**唯一的 gold-case**。

pi 会话相关 flag（实测 `pi --help`）：

| 序号 | flag | 含义 |
|---|---|---|
| 1 | `--session-id <id>` | **用指定的项目会话 id，不存在则创建** ← 可由 aipane 预指定 |
| 2 | `--continue, -c` | 继续最近会话 |
| 3 | `--resume, -r` | 交互选择一个会话恢复 |
| 4 | `--session <path\|id>` | 用指定会话文件 / 部分 UUID |
| 5 | `--fork <path\|id>` | 复刻为新会话 |
| 6 | `--name, -n <name>` | 设置会话显示名 |

存储：`~/.pi/agent/sessions/<编码cwd>/<ts>_<uuidv7>.jsonl`（cwd 归档，id 在文件名，启动即建）。

### 决策
- aipane 启动 pi 改为注入 **`pi --session-id <aipane 生成的 uuid>`**（可选 `--name`）。
- id 由 aipane 完全掌控、写在 argv → 命中 **C4 优先级 1（重启无关、最稳）**，**无需 store 探测、无需钩子**。
- 恢复：`pi --session-id <同一 uuid>`（不存在则创建，天然降级）。
- **坑**：aipane 现有 `ai p resume …` 会把 `resume` 当参数传给 pi（`pi resume` 不合法）；pi 续接用 `--continue`/`--resume`/`--session`/`--session-id`，与 codex/grok 的 `resume` 子命令不同 → aipane 需为 pi 单独映射。
- **已确认（2026-08-08，用户同意）**：`ai p` 默认启动改为注入 `--session-id`。

### 高价值线索（超出 D3，见 §7-5）
pi 证明「预指定会话 id」能力存在。应回头核实 codex/grok/claude/cursor 是否也有类似旗标；
若有，它们同样升级为 gold-case，整套钩子 / store 探测机制可大幅简化。
→ **已核实，见 §12。**

## 12. §7-5 核实结果：预指定 session id 的能力（2026-08-08 实测 `--help`）

方法：抓各工具完整 `--help`（含子命令），检索会话/id 相关旗标；对 cursor 另跑一次
`cursor-agent create-chat` 验证返回值。原始 help 存于 `/tmp/help_*.txt`。

### 结论一览

| 序号 | 工具 | 能否让 aipane 在启动时预定 id | 依据（原文实测） | 定级 |
|---|---|---|---|---|
| 1 | pi (`p`) | ✅ 直接旗标 | `--session-id <id>`：用指定项目会话 id，**不存在则创建**（幂等） | **gold-case** |
| 2 | grok (`g`) | ✅ 直接旗标 | `-s, --session-id <SESSION_ID>`：为**新**会话指定 UUID（须合法且**尚不存在**）；resume 用 `-r` | **gold-case** |
| 3 | claude (`c`) | ✅ 直接旗标 | `--session-id <uuid>`：为该会话指定 id（须合法 UUID） | **gold-case** |
| 4 | cursor (`r`) | ✅ 间接（预建） | 无 `--session-id`；但 `create-chat` **返回纯 UUID**，再 `--resume <chatId>` 进入 | **半 gold-case** |
| 5 | codex (`x`) | ❌ 无 | 无预指定旗标；`-c/--config` 只覆盖 `config.toml`，session id 内部生成；`resume <SESSION_ID>` 只接既有会话 | 非 gold-case → **仍走 hook** |

cursor 实测：`cursor-agent create-chat` → 输出 `2905e7b7-016d-472c-a468-4dd51371a97b`（exit 0，纯 id）。

### 关键点：gold-case 让「急切/懒惰」之分作废
id 是 **aipane 自己生成并写进 argv** 的，无论工具何时真正落盘会话文件，aipane 从启动第一刻就已知该 id。
→ pi/grok/claude/cursor **无需 store 探测、无需钩子、无竞态**。§1 的生成时机表对这 4 家仅剩存档意义。
唯一还需要钩子的是 codex（懒惰 + 无预指定）。

### 启动 / 恢复 语法矩阵（gold-case 部分）

| 序号 | 工具 | 首次启动（aipane 预指定 id=X） | 恢复（同一 X） | 备注 |
|---|---|---|---|---|
| 1 | pi | `pi --session-id X` | `pi --session-id X` | 幂等，一条命令通吃，最干净 |
| 2 | grok | `grok --session-id X` | `grok --resume X` | `--session-id` **仅新建**（已存在会报错）→ 恢复必须换 `-r` |
| 3 | claude | `claude --session-id X` | `claude --resume X`（`-r`） | 同上，`--session-id` 用于新建 |
| 4 | cursor | `X=$(cursor-agent create-chat)` 后 `cursor-agent --resume X` | `cursor-agent --resume X` | 启动多一次预建调用；两端都用 `--resume` |
| 5 | codex | 裸启动（无法预指定）+ **hook** 回报 id | `codex resume X` | 唯一保留钩子/探测的核心工具 |

（以上须保留各自原始 flag，如 `--yolo` / `--always-approve` / `--force` 等。）

### 对整体设计的影响
- **§3.2 关联令牌 + 钩子链路**：核心工具从「4 懒惰派需钩子」缩到**仅 codex 一家**；令牌注入仍保留，但主要服务 codex。
- **C4（§10）优先级 1（命令行自带 id 最稳）**：pi/grok/claude/cursor **从首次启动就命中**，不再只在 resume 时命中 → 注册表与坐标快照对这 4 家近乎冗余（仍留作对账与 codex 用）。
- aipane 落地：为 pi/grok/claude/cursor 各写一个「生成 uuid → 注入启动 → 记录 (%N→X) → 定义 resume 命令」的适配；codex 单独保留 hook 支路。

### 已确认（2026-08-08，用户同意）
**aipane 默认启动**（`ai p` / `ai g` / `ai c` / `ai r`）改为「aipane 生成 uuid 并预指定」
（cursor 走 `create-chat` 预取）。换来这 4 家**零竞态、零钩子**的确定性绑定。
落地顺序据此改为「gold-case 主线先行、codex 走钩子支路」，见 §8。

## 13. v1 实现纪要（2026-08-08 落地；含对早期设计的两处修正）

实现中读了本机 resurrect 源码与存档，推翻了 §9-A1 / §10 的两个前提假设，方案因此大幅简化。

### 修正一：resurrect **有** `post-restore-all` 钩子
`~/.tmux/plugins/tmux-resurrect/scripts/restore.sh:382` 在全部恢复完成后 `execute_hook "post-restore-all"`。
→ 不必重绑 `C-r`；直接 `set -g @resurrect-hook-post-restore-all '$HOME/.local/bin/ai-restore'` 即可。
（`post-save-layout` 也确实把存档文件路径作为 `$1` 传入，见 `save.sh:246`。）

### 修正二：存档已含**每个** pane 的完整命令行 → gold-case 无需坐标快照
resurrect 的 save 会为所有 pane（不止白名单）写下 `session\twin\t..\tpane\ttitle\t:cwd\t..\tcur_cmd\t:full_cmd`。
gold-case 注入的 `--session-id X`、以及任何 `resume` 式启动的 id，都**天然落在 full_cmd 字段**里。
→ `ai-restore` 直接解析 `last` 存档，按 `session:win.pane` 坐标把 resume 命令送回对应 pane。
gold-case 4 家**无需 `aipane-snapshot`、无需注册表**（「铁律二：以存档命令行为准」的最强形态）。
坐标快照**只为 codex**保留（其 id 不在命令行，见下「codex 支路」）。

### codex 支路（实测已落地）——唯一非 gold-case 核心工具
codex 无启动期 `--session-id`，其会话 id（`thread-id`）在**运行后**才知道。链路：
1. **信道 = codex `notify`**（实证其载荷含 `thread-id`，且进程继承 `$TMUX_PANE`；`type=agent-turn-complete`，每回合触发）。
2. `config.toml` 的 `notify` 改指向 **`bin/aipane-codex-notify`** 包装器：后台 fire-and-forget 调 `aipane-bind` 记 `(%N→thread-id)`，再 `exec` 原 `codex-notify.py`（**用户脚本零改动**）。
3. 存盘时 **`bin/aipane-snapshot`**（`post-save-layout` 钩子）把 `%N→sid` 解析成 `坐标→sid` 写 `coords-last.json`（同刻锁定坐标，抗 renumber，理由同 §10）。
4. `ai-restore` 对 codex 的 id 优先级：**argv > 坐标快照(coords-last.json) > pane 标题 uuid**。

### 已落地清单
| 序号 | 位置 | 内容 |
|---|---|---|
| 1 | `~/.tmux.conf` | capture=on、save-interval=15、restore=off、processes 白名单、post-restore-all + post-save-layout 两钩子 |
| 2 | `lib/session.zsh`（新） | uuid、JSONL 注册表原子追加、`_aipane_prepare_launch` gold-case 注入、cursor 预建 |
| 3 | `init.zsh` | core 之后 source `lib/session.zsh` |
| 4 | `cmd/pane.zsh` | 单/多 pane 启动接注入 + 记 `(%N→tool,sid)`；build 暴露 `_aipane_result_panes` |
| 5 | `bin/ai-restore`（新） | 解析存档 + 坐标快照 → 逐 pane 重建 resume |
| 6 | `bin/aipane-bind`（新） | 复用 `_aipane_registry_record` 写注册表（codex 钩子/手动） |
| 7 | `bin/aipane-codex-notify`（新） | codex notify 包装器：记 `thread-id` 后委托原 notifier |
| 8 | `bin/aipane-snapshot`（新，py） | 存盘同刻写 `coords-last.json`（坐标→sid，主为 codex） |
| 9 | `~/.codex/config.toml` | `notify` 指向 `aipane-codex-notify`（已备份 `config.toml.bak-aipane-*`） |
| 10 | `~/.local/bin` | symlink：ai-restore / aipane-bind / aipane-codex-notify / aipane-snapshot |

### ai-restore 的关键策略（实测已验证）
- **仅恢复有确切 id 的 pane（resume）**；无可恢复 id 的（fresh）**默认跳过**，不空起新 AI（省 token、避免惊吓）。`AI_RESTORE_FRESH=1` 可开启空起。
- **幂等 & 安全**：只对「当前停在 idle shell」的 pane 下手 → 不覆盖 resurrect 已重开的白名单进程；重复运行无害。
- **per-tool resume 重建**：pi `--session-id X`（幂等）；grok/claude `--resume X`（其 `--session-id` 仅新建）；cursor `--resume X`；codex `resume X`（argv→坐标快照→标题）。
- `--dry-run` 可对任意存档预演；日志在 `~/.local/share/aipane/ai-restore.log`。

### 已知残留 / 待办
- **懒惰派空会话**：`ai g` / `ai c` 注入了 id 但重启前**未发消息** → 会话未落盘，`--resume X` 会报「找不到」。属 §1 早已承认的「没发消息＝没东西可恢复」；可后续加 store 存在性探测改判 resume/fresh。
- **cursor 预建 ~3s**：`create-chat` 网络往返约 2.7–3s，每次裸 `ai r` 会等这一下。`AIPANE_CURSOR_PRECREATE=0` 可单独关闭（则 cursor pane 重启后为 fresh）。
- **codex 首回合前重启**：`thread-id` 经 `notify`（回合完成）才拿到；若一条消息都没发过就重启，无 id 可恢复（同懒惰派空会话，合理）。标题恰为 uuid 时仍可兜底。
- **codex config 生效范围**：`notify` 改动只对**新启动**的 codex 生效；运行中的旧进程仍用旧 notifier。
- ai-restore 的 resume base 命令使用 aipane 默认值或直接命令覆盖；从裸 hook 环境取不到时回退为纯二进制。

## 14. opencode / kimi 支路（2026-08-08 落地；订正 §3.1/§4/§7 旧结论）

用户追加需求：opencode（`o`）与 kimi（`k`）也要恢复。实测推翻了早期两处判断：

- **订正一**：kimi **有钩子系统**（§4 表曾记「无（仅 MCP）」）。`~/.kimi-code/config.toml` 支持
  Claude-Code 式 `[[hooks]]`，13 种事件；官方文档确认**钩子命令经 stdin 收 JSON**，base 字段含
  `hook_event_name` / `session_id` / `session_title` / `cwd`，且 `SessionStart`（`source=startup|resume`）
  一开始就带 `session_id`。钩子进程继承 `$TMUX_PANE`（现有 `tmux-window-wrap` 钩子已证）。
- **订正二**：opencode 与 kimi 均**非 gold-case**——`opencode -s/--session <id>`、`kimi -S/--session [id]`
  都只能**续接已存在**的会话，无「启动即预指定新 id」的旗标。二者 id 均**懒惰**（首条消息才生成，
  opencode 形如 `ses_...`、非 UUID）。故二者归入 **codex 同类**：走「钩子 → `aipane-bind` → 坐标快照 →
  `ai-restore` 按坐标兑底、按 id resume」，复用已建链路，`aipane-bind`/`aipane-snapshot` 与工具无关、零改动。

### 信道（各接一条，均 fail-open、绝不阻断工具）
| 序号 | 工具 | 信道 | 取 id 时机 | 记录动作 |
|---|---|---|---|---|
| 1 | opencode | **插件** `~/.config/opencode/plugins/aipane-bind.js`（`chat.message` 钩子，带 `sessionID`） | 首条消息 | 插件在 opencode 进程内 spawn `aipane-bind --tool o --sid <id> --pane $TMUX_PANE` |
| 2 | kimi | **`SessionStart` 钩子** → `bin/aipane-kimi-hook`（读 stdin JSON 取 `session_id`） | 会话开始（startup/resume） | `aipane-bind --tool k --sid <id> --pane $TMUX_PANE` |

### 恢复语法（ai-restore；id 只来自坐标快照，不在 argv）
| 序号 | 工具 | 有 id（坐标快照命中） | 无 id 兜底 |
|---|---|---|---|
| 1 | opencode | `opencode --session <id>`（resume） | **fresh 跳过**：`opencode --continue` 是全局「上一个」、跨 pane 有歧义，不可信 |
| 2 | kimi | `kimi --yolo --session <id>`（resume） | `kimi --yolo --continue`（**按 cwd 续接**，pane 的 cwd 已被 resurrect 恢复 → 仍是真 resume） |

### 已落地清单（本轮新增/改动）
| 序号 | 位置 | 内容 |
|---|---|---|
| 1 | `bin/aipane-kimi-hook`（新，sh） | kimi `SessionStart` 钩子包装器：stdin JSON 取 `session_id` → 后台 `aipane-bind --tool k`，恒 exit 0 |
| 2 | `~/.config/opencode/plugins/aipane-bind.js`（新，js） | opencode 插件：`chat.message` 首条即 spawn `aipane-bind --tool o`；每会话去重；`AIPANE_OPENCODE_DEBUG=1` 记日志 |
| 3 | `bin/ai-restore` | `detect_tool` 认 `opencode`/`kimi`（均 Mach-O，argv0 直判）；`build_resume` 增两工具的 resume/兜底；新增 `OPENCODE_BASE`/`KIMI_BASE` |
| 4 | `~/.kimi-code/config.toml` | 新增一条 `SessionStart → aipane-kimi-hook`（不动既有 8 条钩子；已备份 `config.toml.bak.aipane-session-*`） |
| 5 | `~/.local/bin` | symlink：`aipane-kimi-hook` |

### 实测（均通过）
- kimi 钩子：喂伪 `SessionStart` JSON → 注册表得 `{"tool":"k","sid":"sess_kimi_clean_001",...}`；`kimi doctor` 判 config 有效。
- opencode 插件：真跑 `opencode run`（`AIPANE_OPENCODE_DEBUG=1`）→ 日志 `plugin loaded` + `bind ... sid=ses_...`，注册表得 `tool:o` 条目。**关键**：`chat.message` 在模型调用**之前**触发，故即使模型鉴权失败也已抓到 id。
- `ai-restore --dry-run`（合成 dump + 坐标快照）：opencode 有 id→`--session`、无 id→skip；kimi 有 id→`--session`、无 id→`--continue`；codex/cursor 无回归。

### 已知残留
- 二者都属懒惰派：**重启前从未发消息**（opencode）/ **从未开过会话**（kimi 该 cwd）时无 id 可恢复——opencode 落 fresh、kimi `--continue` 若无历史则自然新开。属 §1 早已承认的边界。
- opencode 插件加载依赖该版本的 `{id, server}` 默认导出形态（本机实测通过）；若将来 opencode 换加载器需回看。
- kimi 同一事件挂两条 `[[hooks]]`（既有 `tmux-window-wrap` + 新 `aipane-kimi-hook`）依赖 kimi「同事件多钩子皆执行」；`doctor` 已验证 config 合法，实跑时如只执行其一需改为串接。
