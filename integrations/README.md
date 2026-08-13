# Agent Activity integrations

`tmux-window-wrap activity busy|idle` is the Agent Activity Interface. Each
AI Tool integration in this directory is an Adapter from that tool's lifecycle
events to this Interface.

Agent Activity is turn-level: a submitted turn is `busy` until it completes,
fails, is interrupted, or its session ends. The command inherits `TMUX_PANE`
from the AI Tool process, so hook configuration does not need a pane id.

Claude Code activity is translated by `bin/aipane-claude-activity`. Regular
turns start at `UserPromptSubmit`; agent-team teammates instead start with a
`SessionStart` payload containing `agent_type`, and reused teammates may first
become observable at `PreToolUse`. `TeammateIdle` clears their pane when the
delegated turn finishes. An in-turn `SessionStart(source=compact)` preserves
the existing marker.

If a hard Claude failure returns to the prompt without delivering its terminal
hook, `tmux-window-wrap` reconciles the marker with Claude Code's live session
registry. The registry target, process, version, status, and status timestamp
must all agree, so this fallback does not infer activity from terminal titles
or process CPU usage.

Codex completion is handled by `bin/aipane-codex-notify`, not its `Stop` hook.
Another Codex `Stop` hook can ask the current turn to continue, so `Stop` is not
a reliable completion boundary. `UserPromptSubmit` starts normal turns;
`PreToolUse` is a fallback for internal `/goal` turns that do not pass through a
normal user-message event. The wrapper changes pane activity only for the root
agent: subagent completions inherit the same `TMUX_PANE`, but they neither clear
the parent's busy state nor replace its root session binding. A root completion
clears activity only when no active goal will continue the work. `SessionStart`
only clears on `startup|resume|clear`; the `compact` source occurs inside an
already-running turn and must preserve busy.

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
