# aipane

用于日常 AI CLI 工作流的 zsh 工具集：隔离账号配置启动 Claude Code、通过 tmux 窗格编排多个 AI 工具，以及清理相关进程。

[English README](README.md)

## 功能

| 命令 | 说明 |
|---|---|
| `cc [email] [args...]` | 使用账号隔离的配置目录启动 Claude Code；省略 `email` 时交互选择账号 |
| `ccd [args...]` | 使用常规配置目录并带 `--dangerously-skip-permissions` 启动 Claude Code |
| `ai [--new\|-n] [--layout\|-l <layout>] <tools_string> [tool_args...]` | 直接启动一个 AI CLI，或在 tmux 窗格中编排多个 CLI |
| `codexx [args...]` | 启动 `codex --yolo` |
| `killcc [options]` | 清理已分离的 AI CLI 进程树和孤立的 AI 子进程 |
| `killmcp [options]` | 清理过期、已分离或孤立的 MCP 辅助进程 |
| `killrod [options]` | 强制清理匹配的 Rod/Leakless 浏览器进程和 Playwright `chrome-headless-shell` 进程 |

便捷别名：

- `geminii` → `gemini --yolo`
- `oc` → `opencode`

> [!WARNING]
> `ccd`、`codexx` 和部分 `ai` 启动器会关闭对应工具的审批检查；清理命令会终止进程。请先确认命令定义，不确定时先用 `--dry-run` 检查清理范围。

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
- Claude Code：供 `cc`、`ccd` 以及 `ai` 的 `c` 工具使用
- tmux：供多工具 `ai` 调用，以及使用 `--new` 或 `--layout` 时使用
- 其他 `ai` 工具对应的可选 CLI：Codex、Droid、Grok、OpenCode、Cursor CLI、Qoder CLI 或 Oh My Pi（`omp`）

`ai x` 这类单工具调用默认在当前 shell 中运行；未使用 `--new` 或 `--layout` 时不依赖 tmux。

## 可选配置

在加载 `init.zsh` 之前设置覆盖值：

```bash
export AIPANE_CLAUDE_CMD="claude"                       # 例如 claude-guard
export AIPANE_ACCOUNTS_BASE="$HOME/.claude-accounts"
export AIPANE_SHARED_DIR="$AIPANE_ACCOUNTS_BASE/_shared"
export AIPANE_CACHE_DIR="${XDG_CACHE_HOME:-$HOME/.cache}"

export AIPANE_CODEX_LAUNCH_CMD="codex --yolo"
export AIPANE_DROID_LAUNCH_CMD="droid"
export AIPANE_GROK_LAUNCH_CMD="grok --always-approve"
export AIPANE_OPENCODE_LAUNCH_CMD="opencode"
export AIPANE_CURSOR_LAUNCH_CMD="cursor-agent --force"
export AIPANE_QODER_LAUNCH_CMD="qodercli"
export AIPANE_OMP_LAUNCH_CMD="omp --approval-mode=yolo"
```

加载这些覆盖值后，`ai --help` 会显示实际生效的启动命令。

## Claude 账号模型

只有 `cc` 使用账号专属的 `CLAUDE_CONFIG_DIR`。省略邮箱，或者第一个参数以 `-` 开头时，`cc` 会从 `AIPANE_ACCOUNTS_BASE` 下的目录中交互选择账号。

```bash
cc alice@example.com
cc alice@example.com --resume <session-id>
cc --continue                         # 先选择账号，再透传 --continue
```

`ccd` 特意使用 Claude Code 的常规配置目录，不接受账号选择参数：

```bash
ccd
ccd --resume <session-id>
```

`cc` 创建的账号目录结构如下：

```text
~/.claude-accounts/
├── alice@example.com/
│   ├── .claude.json
│   ├── rules -> ~/.claude/rules
│   ├── settings.json -> ~/.claude/settings.json
│   ├── settings.local.json -> ~/.claude/settings.local.json
│   ├── projects -> ~/.claude-accounts/_shared/projects
│   └── history.jsonl -> ~/.claude-accounts/_shared/history.jsonl
├── bob@example.com/
└── _shared/
    ├── projects/
    └── history.jsonl
```

账号目录中已有的普通文件会被保留；缺少的链接和共享路径会按需创建。

## `ai` 启动器

工具键及其默认命令：

| 键 | 工具 | 默认命令 |
|---|---|---|
| `c` | Claude Code | `ccd` |
| `x` | Codex | `codex --yolo` |
| `d` | Droid | `droid` |
| `g` | Grok | `grok --always-approve` |
| `o` | OpenCode | `opencode` |
| `r` | Cursor | `cursor-agent --force` |
| `q` | Qoder | `qodercli` |
| `p` | Oh My Pi | `omp --approval-mode=yolo` |

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
├── .gitignore
├── LICENSE
├── README.md
├── README_CN.md
├── init.zsh                 # zsh 加载入口
├── aipane.zsh               # 兼容旧版的入口
├── lib/
│   └── core.zsh             # 公共配置、账号和布局辅助函数
├── cmd/
│   ├── aliases.zsh
│   ├── cc.zsh
│   ├── codex.zsh
│   ├── kill.zsh
│   ├── killmcp.zsh
│   ├── killrod.zsh
│   └── pane.zsh
├── bin/
│   ├── aipane-cleanup
│   └── rod-cleanup
├── docs/
│   └── plans/
│       └── 2026-03-05-rod-cleanup-design.md
└── tests/
    └── aipane-cleanup-ai-protection.sh
```

## 验证

```bash
zsh -n init.zsh aipane.zsh cmd/*.zsh lib/*.zsh
sh -n bin/aipane-cleanup bin/rod-cleanup tests/*.sh

zsh -fc '
  source ./init.zsh
  type cc ccd ai codexx killcc killmcp killrod geminii oc
  ai --help >/dev/null
'

./tests/aipane-cleanup-ai-protection.sh
```

## 许可证

MIT
