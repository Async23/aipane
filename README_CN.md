# aipane

用于日常 AI CLI 工作流的 zsh 工具集：通过 tmux 窗格编排多个 AI 工具、清理相关进程，以及可选的 **Ghostty + tmux 工作台**（Cmd 键位桥、广播输入、窗口标签多行换行）。

[English README](README.md)

## 功能

| 命令 / 表面 | 说明 |
|---|---|
| `ai [--new\|-n] [--layout\|-l <layout>] <tools_string> [tool_args...]` | 直接启动一个 AI CLI，或在 tmux 窗格中编排多个 CLI |
| `codexx [args...]` | 启动 `codex --yolo` |
| `killcc [options]` | 清理已分离的 AI CLI 进程树和孤立的 AI 子进程 |
| `killmcp [options]` | 清理过期、已分离或孤立的 MCP 辅助进程 |
| `killrod [options]` | 强制清理匹配的 Rod/Leakless 浏览器进程和 Playwright `chrome-headless-shell` 进程 |
| 可选 Ghostty + tmux 工作台 | Cmd↔tmux 键位桥、广播、窗口标签多行（**不会**被 `init.zsh` 加载） |

便捷别名：

- `geminii` → `gemini --yolo`
- `oc` → `opencode`

> [!WARNING]
> `codexx` 和部分 `ai` 启动器（含 `ai c` 启动 Claude Code）会关闭对应工具的审批检查；清理命令会终止进程。请先确认命令定义，不确定时先用 `--dry-run` 检查清理范围。

## 安装

```bash
git clone https://github.com/Async23/aipane.git ~/.aipane
grep -qxF 'source ~/.aipane/init.zsh' ~/.zshrc 2>/dev/null ||
  printf '\nsource ~/.aipane/init.zsh\n' >> ~/.zshrc
source ~/.zshrc
```

更新已有安装：

```bash
git -C ~/.aipane pull --ff-only
```

## 依赖

- macOS 和 zsh
- Claude Code：供 `ai` 的 `c` 工具使用（不用 `c` 时可省略）
- tmux：供多工具 `ai` 调用，以及使用 `--new` 或 `--layout` 时使用
- 其他 `ai` 工具对应的可选 CLI：Codex、Droid、Grok、OpenCode、Cursor CLI、Qoder CLI 或 Pi（`pi`）

`ai x` 这类单工具调用默认在当前 shell 中运行；未使用 `--new` 或 `--layout` 时不依赖 tmux。

## 可选：Ghostty + tmux 工作台

为多 AI CLI 窗格准备的终端壳层。**不会**被 `init.zsh` 加载。个人字体/主题/TPM 仍放本机；公共片段只做 **include/source**。

| 部分 | 作用 |
|------|------|
| Ghostty Cmd 桥 | 单次 `Cmd+T/W/D/[]/…` 组合；`Cmd+Opt+P` 显示 pane ID |
| Shift+Enter | tmux 里给 Codex 等稳定换行（→ Ctrl+J） |
| tmux workstation | zoom 保持导航、广播/单格豁免、pane 顶栏、vi 复制 |
| window-wrap | 状态栏窗口标签最多 3 行 |
| copy-mode `Y` | 可选长截图，依赖 [tmux-shot](https://github.com/Async23/tmux-shot) |

```bash
AIPANE_ROOT="${AIPANE_ROOT:-$HOME/.aipane}"
ln -sf "$AIPANE_ROOT/bin/tmux-window-wrap" ~/.local/bin/tmux-window-wrap
```

**Ghostty**（`~/.config/ghostty/config`）— 个人外观 + include：

```ini
config-file = /path/to/aipane/conf/ghostty-tmux.conf
```

**tmux**（`~/.tmux.conf`）：

```tmux
source-file ~/.aipane/conf/tmux-workstation.conf
source-file ~/.aipane/conf/tmux-window-wrap.conf
```

| 路径 | 作用 |
|------|------|
| `conf/ghostty-tmux.conf` | Cmd 桥 + Shift+Enter |
| `conf/tmux-workstation.conf` | prefix、键位、广播、状态栏样式 |
| `conf/tmux-window-wrap.conf` | 窗口列表多行 |
| `bin/tmux-window-wrap` | 渲染 CLI |
| `tests/test_tmux_window_wrap.py` | window-wrap 单元 + 真 tmux 测试 |
| `tests/test_workstation_fragments.py` | conf 片段结构测试 |
| [`docs/ghostty-tmux-workstation.md`](docs/ghostty-tmux-workstation.md) | 安装与所有权 |
| [`docs/cheatsheet.md`](docs/cheatsheet.md) | 键位速查 |
| [`docs/tmux-window-wrap.md`](docs/tmux-window-wrap.md) | 仅换行说明 |

改 workstation conf 或 wrap 渲染器后：

```bash
python3 "$AIPANE_ROOT/tests/test_workstation_fragments.py"
python3 "$AIPANE_ROOT/tests/test_tmux_window_wrap.py"
```

若 Ghostty 已在运行，用 **Cmd+Shift+,**（`reload_config`）重载配置以加载 include。

## 可选配置

在加载 `init.zsh` 之前设置覆盖值：

```bash
export AIPANE_CLAUDE_LAUNCH_CMD="claude --dangerously-skip-permissions"  # 例如 claude-guard --dangerously-skip-permissions
export AIPANE_CODEX_LAUNCH_CMD="codex --yolo"
export AIPANE_DROID_LAUNCH_CMD="droid"
export AIPANE_GROK_LAUNCH_CMD="grok --always-approve"
export AIPANE_OPENCODE_LAUNCH_CMD="opencode"
export AIPANE_CURSOR_LAUNCH_CMD="cursor-agent --force"
export AIPANE_QODER_LAUNCH_CMD="qodercli"
export AIPANE_PI_LAUNCH_CMD="pi"
```

加载这些覆盖值后，`ai --help` 会显示实际生效的启动命令。

## `ai` 启动器

工具键及其默认命令：

| 键 | 工具 | 默认命令 |
|---|---|---|
| `c` | Claude Code | `claude --dangerously-skip-permissions` |
| `x` | Codex | `codex --yolo` |
| `d` | Droid | `droid` |
| `g` | Grok | `grok --always-approve` |
| `o` | OpenCode | `opencode` |
| `r` | Cursor | `cursor-agent --force` |
| `q` | Qoder | `qodercli` |
| `p` | Pi | `pi` |

示例：

```bash
ai x                                      # 在当前 shell 中运行单个工具
ai x resume <session-id>                  # 向单个工具透传参数
ai x -- --help                            # 把类似 ai 选项的参数传给工具
ai --new x resume <session-id>            # 在新的 tmux 窗口或会话中运行

ai cxdg                                   # 每个字符对应一个窗格
ai cxr                                    # 交互选择三窗格布局
ai cxr --layout main-right                # 跳过三窗格布局选择
ai -l columns cxr
ai --new cxdg
ai cc                                     # 重复的键会启动重复的工具
```

可用布局：

- `auto`：自动网格，并跳过三窗格提示
- `main-left`、`main-right`：仅适用于恰好三个窗格的非对称布局
- `columns`、`rows`：每个工具独占一列或一行
- `1,2`、`2,1` 等自定义列数量；各数字之和必须等于工具数量

只有工具串包含一个工具时才能透传参数。多工具调用带额外参数会被拒绝，避免把不兼容参数同时发给多个 CLI。

在 tmux 内，只有当前窗口恰好包含一个窗格时才会复用该窗口；使用 `--new` 可新建窗口。在 tmux 外，窗格调用会创建临时会话并自动 attach。

## 进程清理

三个包装命令与统一清理引擎的对应关系：

```text
killcc  → aipane-cleanup ai  --verbose
killmcp → aipane-cleanup mcp --verbose --session-age 18000
killrod → aipane-cleanup rod --force --verbose
```

建议先执行 dry run：

```bash
killcc --dry-run
killmcp --dry-run
killrod --dry-run
```

直接调用方式：

```bash
./bin/aipane-cleanup [all|ai|rod|mcp] \
  [--all|--force] [--max-age SECONDS] [--orphan-age SECONDS] \
  [--session-age SECONDS] [--dry-run] [--verbose|-v] [--help|-h]
```

`ai` 模式会识别已分离的 Claude、Codex、Droid、Gemini、Grok、OpenCode 和 `agent-browser` daemon 进程，沿进程树收集相关进程，并检查已知的孤立 AI 子进程。传入 `--session-age` 后，还会清理超过指定时长的已分离 tmux Claude 会话树。

在 `rod` 模式中，`--force` 会忽略进程年龄，选中所有匹配的 Rod/Leakless Chromium 进程以及所有匹配的 `ms-playwright/.../chrome-headless-shell` 进程。

清理操作记录在 `~/logs/aipane-cleanup.log`。可通过以下环境变量覆盖时间阈值：

- `AIPANE_AI_ORPHAN_MAX_AGE`（默认 `900`）
- `AIPANE_AI_SESSION_MAX_AGE`（未设置时禁用）
- `AIPANE_ROD_MAX_AGE`（默认 `300`）
- `AIPANE_MCP_MAX_AGE`（默认 `21600`）
- `AIPANE_MCP_ORPHAN_MAX_AGE`（默认 `900`）
- `AIPANE_MCP_SESSION_MAX_AGE`（未设置时禁用）
- `AIPANE_TMUX_BIN`（可选的 tmux 显式路径）

## 项目结构

```text
.
├── init.zsh                 # zsh 入口
├── aipane.zsh               # 兼容入口
├── lib/core.zsh
├── cmd/                     # ai/pane、kill*、codexx、aliases …
├── bin/
│   ├── aipane-cleanup
│   ├── rod-cleanup
│   └── tmux-window-wrap     # 可选状态栏渲染器
├── conf/                    # 可选；不会被 init.zsh 加载
│   ├── ghostty-tmux.conf
│   ├── tmux-workstation.conf
│   └── tmux-window-wrap.conf
├── docs/
│   ├── cheatsheet.md
│   ├── ghostty-tmux-workstation.md
│   └── tmux-window-wrap.md
└── tests/
    ├── ai-contracts.zsh
    ├── ai-p-launches-pi.zsh
    ├── aipane-cleanup-ai-protection.sh
    ├── aipane-cleanup-contracts.sh
    ├── init-reload.zsh
    ├── test_tmux_window_wrap.py
    └── test_workstation_fragments.py
```

## 验证

```bash
zsh -n init.zsh aipane.zsh cmd/*.zsh lib/*.zsh tests/*.zsh
sh -n bin/aipane-cleanup bin/rod-cleanup tests/*.sh

zsh -fc '
  source ./init.zsh
  type ai codexx killcc killmcp killrod geminii oc
  ai --help >/dev/null
'

./tests/init-reload.zsh
./tests/ai-p-launches-pi.zsh
./tests/ai-contracts.zsh
./tests/aipane-cleanup-ai-protection.sh
./tests/aipane-cleanup-contracts.sh

# 可选工作台 / window-wrap
python3 tests/test_workstation_fragments.py
python3 tests/test_tmux_window_wrap.py
```

## 许可证

MIT
