# aipane

Unified zsh toolkit for daily AI CLI workflows: Claude Code multi-account launch, tmux pane orchestration, usage batch query, session switching, and process cleanup.

## Features

| Command | Description |
|---|---|
| `cc [email] [args...]` | Launch Claude Code with account-isolated config directory |
| `ccd [email] [args...]` | Launch Claude Code with `--dangerously-skip-permissions` |
| `ai [--layout <layout>] <tools_string> [tool_args...]` | Launch AI CLIs in tmux panes (`c/x/d/g/o/r/q/p`) |
| `cc-status` | Show login/config state for all Claude accounts |
| `cc-usage [cmd] [--timeout N] [--yes\|-y]` | Open all Claude accounts in panes and send command (default `/usage`) |
| `cc-switch [email] [session-id]` | Resume the latest/current project session with another account |
| `killcc` | Kill detached/zombie Claude-related processes (`TTY=??`) |

**Aliases:** `ccstatus` → `cc-status`, `ccusage` → `cc-usage`

## Project Structure

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
└── aipane.zsh  # compatibility wrapper (legacy entry)
```

## Install

```bash
git clone https://github.com/Async23/aipane.git ~/.aipane
echo 'source ~/.aipane/init.zsh' >> ~/.zshrc
source ~/.zshrc
```

## Optional Configuration

Set these before `source ~/.aipane/init.zsh`:

```bash
export AIPANE_CLAUDE_CMD="claude"                       # e.g. claude-guard
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

## Dependencies

- macOS + zsh
- tmux (`ai`, `cc-usage`) (outside tmux, these commands auto-create a session and attach)
- `jq` (`cc-status`)
- Claude Code (`cc`, `ccd`, `cc-usage`, `cc-switch`)
- Optional: Codex, Droid, Grok, Opencode, Cursor CLI, Qoder CLI, Oh My Pi (`omp`) (for `ai`)

## Account Layout

`aipane` uses account-isolated config under `~/.claude-accounts/` and shared data in `_shared`:

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

## Examples

```bash
cc alice@example.com
ccd bob@example.com --resume 9d47f4f1-xxxx-xxxx-xxxx-xxxxxxxxxxxx

ai cxdg
ai cxr                                   # 3 tools prompt for layout first
ai cxr --layout main-right               # skip the layout prompt
ai -l columns cxr
ai x resume 019e680c-d2bd-71a2-9a9a-8cf78a2d8da1
ai --new x resume 019e680c-d2bd-71a2-9a9a-8cf78a2d8da1
ai x -- --help                           # pass an ai-looking flag to the tool
ai q
ai xq
ai p
ai cc

cc-status
cc-usage
cc-usage "/cost this month" --timeout 30
cc-usage -y                                # skip interactive selection, auto layout

cc-switch alice@example.com
killcc
```

For 3 panes, `ai` supports `main-left`, `main-right`, `columns`, and `rows`; custom column specs such as `--layout 1,2` or `--layout 2,1` are also accepted. Use `--layout auto` to skip the prompt and keep the automatic layout.

Tool arguments are forwarded only when the tools string contains a single tool, for example `ai x resume <session-id>` or `ai c --resume <session-id>`. Multi-tool launches such as `ai xg resume <session-id>` are rejected to avoid sending incompatible arguments to multiple CLIs. If a tool argument has the same spelling as an `ai` option, put it after `--`, for example `ai x -- --help`.

## Quick Verification

```bash
source ./init.zsh
type cc ccd ai cc-status cc-usage cc-switch killcc
```

---

[中文文档 / Chinese README](README_CN.md)

## License

MIT
