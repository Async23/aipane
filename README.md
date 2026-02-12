# aipane

Unified zsh toolkit for daily AI CLI workflows: Claude Code multi-account launch, iTerm2 pane orchestration, usage batch query, session switching, and process cleanup.

## Features

| Command | Description |
|---|---|
| `cc [email] [args...]` | Launch Claude Code with account-isolated config directory |
| `ccd [email] [args...]` | Launch Claude Code with `--dangerously-skip-permissions` |
| `ai <tools_string>` | Launch multiple AI CLIs in iTerm2 panes (`c/x/d/g`) |
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
export AIPANE_GEMINI_LAUNCH_CMD="gemini --yolo"
```

## Dependencies

- macOS + zsh
- iTerm2 (`ai`, `cc-usage`)
- `jq` (`cc-status`)
- Claude Code (`cc`, `ccd`, `cc-usage`, `cc-switch`)
- Optional: Codex, Droid, Gemini CLI (for `ai`)

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
ai cc

cc-status
cc-usage
cc-usage "/cost this month" --timeout 30
cc-usage -y                                # skip interactive selection, auto layout

cc-switch alice@example.com
killcc
```

## Quick Verification

```bash
source ./init.zsh
type cc ccd ai cc-status cc-usage cc-switch killcc
```

---

[中文文档 / Chinese README](README_CN.md)

## License

MIT
