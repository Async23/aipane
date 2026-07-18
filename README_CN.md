# aipane

统一的 zsh 工具集，用于日常 AI CLI 工作流：Claude Code 多账号启动、tmux 窗格编排、用量批量查询、会话切换和进程清理。

## 功能

| 命令 | 说明 |
|---|---|
| `cc [email] [args...]` | 以账号隔离的配置目录启动 Claude Code |
| `ccd [email] [args...]` | 以 `--dangerously-skip-permissions` 模式启动 Claude Code |
| `ai [--layout <layout>] <tools_string> [tool_args...]` | 启动 AI CLI 或在 tmux 窗格中编排多个 AI CLI（`c/x/d/g/o/r/q/p`） |
| `cc-status` | 显示所有 Claude 账号的登录/配置状态 |
| `cc-usage [cmd] [--timeout N] [--yes\|-y]` | 在窗格中打开所有 Claude 账号并发送命令（默认 `/usage`） |
| `cc-switch [email] [session-id]` | 使用另一个账号恢复最新/当前项目会话 |
| `killcc` | 终止分离的/僵尸 Claude 相关进程（`TTY=??`） |

**别名：** `ccstatus` → `cc-status`、`ccusage` → `cc-usage`

## 项目结构

```text
.
├── init.zsh
├── lib/
│   └── core.zsh
├── cmd/
│   ├── cc.zsh
│   ├── pane.zsh
│   ├── status.zsh
│   ├── usage.zsh
│   ├── switch.zsh
│   └── kill.zsh
└── aipane.zsh  # 兼容性包装器（旧入口）
```

## 安装

```bash
git clone https://github.com/Async23/aipane.git ~/.aipane
echo 'source ~/.aipane/init.zsh' >> ~/.zshrc
source ~/.zshrc
```

## 可选配置

在 `source ~/.aipane/init.zsh` 之前设置以下环境变量：

```bash
export AIPANE_CLAUDE_CMD="claude"                       # 例如 claude-guard
export AIPANE_ACCOUNTS_BASE="$HOME/.claude-accounts"
export AIPANE_SHARED_DIR="$AIPANE_ACCOUNTS_BASE/_shared"

export AIPANE_CODEX_LAUNCH_CMD="codex --yolo"
export AIPANE_DROID_LAUNCH_CMD="droid"
export AIPANE_GROK_LAUNCH_CMD="grok --always-approve"
export AIPANE_OPENCODE_LAUNCH_CMD="opencode"
export AIPANE_CURSOR_LAUNCH_CMD="cursor-agent --force"
export AIPANE_QODER_LAUNCH_CMD="qodercli"
export AIPANE_OMP_LAUNCH_CMD="omp --approval-mode=yolo"
```

## 依赖

- macOS + zsh
- tmux（`ai`、`cc-usage`）（非 tmux 环境会自动创建 session 并 attach）
- `jq`（`cc-status`）
- Claude Code（`cc`、`ccd`、`cc-usage`、`cc-switch`）
- 可选：Codex、Droid、Grok、Opencode、Cursor CLI、Qoder CLI、Oh My Pi（`omp`）（用于 `ai`）

## 账号目录结构

`aipane` 在 `~/.claude-accounts/` 下使用账号隔离的配置，共享数据存放在 `_shared` 中：

```text
~/.claude-accounts/
├── alice@example.com/
│   ├── .claude.json
│   ├── rules -> ~/.claude/rules
│   ├── settings.json -> ~/.claude/settings.json
│   ├── settings.local.json -> ~/.claude/settings.local.json
│   ├── projects -> ../_shared/projects
│   └── history.jsonl -> ../_shared/history.jsonl
├── bob@example.com/
└── _shared/
    ├── projects/
    └── history.jsonl
```

## 示例

```bash
cc alice@example.com
ccd bob@example.com --resume 9d47f4f1-xxxx-xxxx-xxxx-xxxxxxxxxxxx

ai cxdg
ai cxr                                   # 3 个工具会先选择布局
ai cxr --layout main-right               # 跳过布局选择
ai -l columns cxr
ai x resume 019e680c-d2bd-71a2-9a9a-8cf78a2d8da1
ai --new x resume 019e680c-d2bd-71a2-9a9a-8cf78a2d8da1
ai x -- --help                           # 把类似 ai 选项的参数传给工具
ai q
ai xq
ai p
ai cc

cc-status
cc-usage
cc-usage "/cost this month" --timeout 30
cc-usage -y                                # 跳过交互式选择，自动布局

cc-switch alice@example.com
killcc
```

`ai` 的 3 窗格布局支持 `main-left`、`main-right`、`columns`、`rows`，也支持自定义列规格，例如 `--layout 1,2` 或 `--layout 2,1`。使用 `--layout auto` 可以跳过交互提示并沿用自动布局。

工具参数只会在工具串包含单个工具时透传，例如 `ai x resume <session-id>` 或 `ai c --resume <session-id>`。`ai xg resume <session-id>` 这类多工具带参数调用会被拒绝，避免把不兼容参数同时发给多个 CLI。如果工具参数和 `ai` 自身选项同名，放在 `--` 之后，例如 `ai x -- --help`。

## 快速验证

```bash
source ./init.zsh
type cc ccd ai cc-status cc-usage cc-switch killcc
```

---

[English README](README.md)

## 许可证

MIT
