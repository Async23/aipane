# Agent Activity integrations

`tmux-window-wrap activity busy|idle` is the Agent Activity Interface. Each
AI Tool integration in this directory is an Adapter from that tool's lifecycle
events to this Interface.

Agent Activity is turn-level: a submitted turn is `busy` until it completes,
fails, is interrupted, or its session ends. The command inherits `TMUX_PANE`
from the AI Tool process, so hook configuration does not need a pane id.

The JSON and TOML files are mergeable configuration fragments; do not replace
an existing user configuration wholesale. The OpenCode Adapter is executable
plugin source and may be symlinked into OpenCode's plugin directory.

| AI Tool | Integration source | User-owned registration point |
|---|---|---|
| Claude Code | `claude/hooks.json` | `~/.claude/settings.json` |
| Codex | `codex/hooks.json` | `~/.codex/hooks.json` |
| Cursor Agent | `cursor/hooks.json` | `~/.cursor/hooks.json` |
| Grok | `grok/hooks.json` | `~/.grok/hooks/*.json` |
| Kimi Code | `kimi/hooks.toml` | `~/.kimi-code/config.toml` |
| OpenCode | `opencode/aipane-bind.js` | `~/.config/opencode/plugins/` |
| Qoder | `qoder/hooks.json` | `~/.qoder/settings.json` |

Pi uses the separately versioned `@async23/pi-tmux-window-wrap` package, so it
does not need another Adapter here.
