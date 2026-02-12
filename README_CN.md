# aipane

统一的 zsh 工具集，用于日常 AI CLI 工作流：Claude Code 多账号启动、iTerm2 窗格编排、用量批量查询、会话切换和进程清理。

## 功能

| 命令 | 说明 |
|---|---|
| `cc [email] [args...]` | 以账号隔离的配置目录启动 Claude Code |
| `ccd [email] [args...]` | 以 `--dangerously-skip-permissions` 模式启动 Claude Code |
| `ai <tools_string>` | 在 iTerm2 窗格中启动多个 AI CLI（`c/x/d/g`） |
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
export AIPANE_GEMINI_LAUNCH_CMD="gemini --yolo"
```

## 依赖

- macOS + zsh
- iTerm2（`ai`、`cc-usage`）
- `jq`（`cc-status`）
- Claude Code（`cc`、`ccd`、`cc-usage`、`cc-switch`）
- 可选：Codex、Droid、Gemini CLI（用于 `ai`）

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
ai cc

cc-status
cc-usage
cc-usage "/cost this month" --timeout 30
cc-usage -y                                # 跳过交互式选择，自动布局

cc-switch alice@example.com
killcc
```

## 快速验证

```bash
source ./init.zsh
type cc ccd ai cc-status cc-usage cc-switch killcc
```

---

[English README](README.md)

## 许可证

MIT
