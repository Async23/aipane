# aipane

A zsh toolkit for daily AI CLI workflows: tmux pane orchestration for multiple AI tools, process cleanup, plus an optional **Ghostty + tmux workstation** (Cmd key bridge, broadcast, multi-line window list).

[中文文档 / Chinese README](README_CN.md)

## Features

| Command / surface | Description |
|---|---|
| `ai [--new\|-n] [--layout\|-l <layout>] <tools_string> [tool_args...]` | Launch one AI CLI directly or orchestrate multiple CLIs in tmux panes |
| `killcc [options]` | Clean up detached AI CLI trees and orphaned AI child processes |
| `killmcp [options]` | Clean up stale detached/orphaned MCP helper processes |
| `killrod [options]` | Force-clean matching Rod/Leakless browser processes and Playwright `chrome-headless-shell` processes |
| Optional Ghostty + tmux workstation | Cmd↔tmux key bridge, broadcast, multi-line window list (not loaded by `init.zsh`) |

> [!WARNING]
> Several `ai` launchers (including `ai c` for Claude Code) disable their tools' approval checks. The cleanup commands terminate processes. Review the command definitions and use `--dry-run` before cleanup when in doubt.

## Install

```bash
git clone https://github.com/Async23/aipane.git ~/.aipane
grep -qxF 'source ~/.aipane/init.zsh' ~/.zshrc 2>/dev/null ||
  printf '\nsource ~/.aipane/init.zsh\n' >> ~/.zshrc
source ~/.zshrc
```

To update an existing installation:

```bash
git -C ~/.aipane pull --ff-only
```

## Dependencies

- macOS and zsh
- Claude Code for the `c` tool in `ai` (optional if you never use `c`)
- tmux for multi-tool `ai` launches, or whenever `--new`/`--layout` is used
- The corresponding optional CLI for each additional `ai` tool: Codex, Droid, Grok, OpenCode, Cursor CLI, Qoder CLI, Pi (`pi`), or Kimi Code (`kimi`)

Single-tool calls such as `ai x` run in the current shell and do not require tmux unless `--new` or `--layout` is supplied.

## Optional: Ghostty + tmux workstation

Terminal shell tuned for many AI CLI panes. **Not** loaded by `init.zsh`. Personal font/theme/TPM stay in your home configs; public fragments are only **included**.

| Piece | What it does |
|-------|----------------|
| Ghostty Cmd bridge | `Cmd+S` prefix; one-shot `Cmd+T/W/D/[]/…`; `Cmd+I` renames in a centered popup; `Cmd+Opt+P` shows pane IDs |
| Shift+Enter | Newline for Codex/other CLIs under tmux (`→` Ctrl+J) |
| tmux workstation | repeated-digit window jump, zoom-aware nav, broadcast + per-pane mute, pane top bar, 256-colour palette, vi copy-mode |
| window-wrap | status window labels wrap up to 3 lines |
| `Y` in copy-mode | optional long screenshot via [tmux-shot](https://github.com/Async23/tmux-shot) |

**Dark theme**

![tmux indexed colour palette popup in dark theme](docs/assets/tmux-colour-palette-dark.png)

**Light theme**

![tmux indexed colour palette popup in light theme](docs/assets/tmux-colour-palette-light.png)

```bash
AIPANE_ROOT="${AIPANE_ROOT:-$HOME/.aipane}"
mkdir -p ~/.local/bin
ln -sf "$AIPANE_ROOT/bin/aipane-doctor" ~/.local/bin/aipane-doctor
ln -sf "$AIPANE_ROOT/bin/tmux-rename-window-popup" ~/.local/bin/tmux-rename-window-popup
ln -sf "$AIPANE_ROOT/bin/tmux-colour-palette" ~/.local/bin/tmux-colour-palette
ln -sf "$AIPANE_ROOT/bin/tmux-window-jump" ~/.local/bin/tmux-window-jump
ln -sf "$AIPANE_ROOT/bin/aipane-activity" ~/.local/bin/aipane-activity
ln -sf "$AIPANE_ROOT/bin/tmux-window-wrap" ~/.local/bin/tmux-window-wrap
ln -sf "$AIPANE_ROOT/bin/ai-restart" ~/.local/bin/ai-restart
```

The rename popup requires `fzf` on `PATH`.

**Ghostty** (`~/.config/ghostty/config`) — personal look + include:

```ini
config-file = /path/to/aipane/conf/ghostty-tmux.conf
```

**tmux** (`~/.tmux.conf`):

```tmux
source-file ~/.aipane/conf/tmux-workstation.conf
source-file ~/.aipane/conf/tmux-window-wrap.conf
```

| Path | Role |
|------|------|
| `conf/ghostty-tmux.conf` | Cmd bridge + Shift+Enter |
| `conf/tmux-workstation.conf` | prefix, binds, broadcast, status chrome |
| `conf/tmux-window-wrap.conf` | multi-line window list |
| `bin/aipane-doctor` | read-only installation and Agent Activity wiring audit |
| `bin/tmux-rename-window-popup` | centered stable-target window rename UI |
| `bin/tmux-colour-palette` | indexed terminal colour palette (`0–255`) |
| `bin/tmux-window-jump` | repeated-digit exact-index window selector |
| `bin/tmux-window-wrap` | renderer CLI |
| `bin/aipane-activity` | Agent Activity CLI |
| `bin/ai-restart` | confirm and seal an exact in-place AI pane restart plan |
| `bin/aipane-restore-executor` | exclusively execute, verify, retry, and persist recovery |
| `lib/recovery_plan.py` | versioned, digest-protected restart plan contract |
| [`integrations/`](integrations/README.md) | Agent lifecycle hook fragments and OpenCode Adapter |
| `tests/test_agent_activity.py` | Agent Activity behavior tests |
| `tests/test_agent_activity_integrations.py` | Agent Activity contract tests |
| `tests/test_restore_executor.py` | verified recovery, retry, and pending-intent tests |
| `tests/test_recovery_plan.py` | sealed recovery plan contract tests |
| `tests/test_tmux_window_jump.py` | window-jump behavior + live tmux tests |
| `tests/test_tmux_window_wrap.py` | window-wrap unit + live tmux tests |
| `tests/test_workstation_fragments.py` | structural tests for conf fragments |
| [`docs/ghostty-tmux-workstation.md`](docs/ghostty-tmux-workstation.md) | install + ownership |
| [`docs/cheatsheet.md`](docs/cheatsheet.md) | key map |
| [`docs/tmux-window-wrap.md`](docs/tmux-window-wrap.md) | wrapping and Agent Activity notes |

Run `aipane-doctor` after installation or after pulling workstation changes.

After editing workstation conf or the wrap renderer:

```bash
python3 "$AIPANE_ROOT/tests/test_aipane_doctor.py"
python3 "$AIPANE_ROOT/tests/test_agent_activity.py"
python3 "$AIPANE_ROOT/tests/test_agent_activity_integrations.py"
python3 "$AIPANE_ROOT/tests/test_tmux_window_jump.py"
python3 "$AIPANE_ROOT/tests/test_workstation_fragments.py"
python3 "$AIPANE_ROOT/tests/test_tmux_window_wrap.py"
```

If Ghostty is already running, reload config with **Cmd+Shift+,** (`reload_config`) so the include is picked up.

## Optional Configuration

Set overrides before sourcing `init.zsh`:

```bash
export AIPANE_CLAUDE_LAUNCH_CMD="claude --dangerously-skip-permissions"
export AIPANE_CODEX_LAUNCH_CMD="codex --yolo"
export AIPANE_DROID_LAUNCH_CMD="droid"
export AIPANE_GROK_LAUNCH_CMD="grok --always-approve"
export AIPANE_OPENCODE_LAUNCH_CMD="opencode"
export AIPANE_CURSOR_LAUNCH_CMD="cursor-agent --force"
export AIPANE_QODER_LAUNCH_CMD="qodercli"
export AIPANE_PI_LAUNCH_CMD="pi"
export AIPANE_KIMI_LAUNCH_CMD="kimi --auto"
```

`ai --help` displays the effective launcher commands after these overrides are loaded.

## `ai` Launcher

Tool keys and default commands:

| Key | Tool | Default command |
|---|---|---|
| `c` | Claude Code | `claude --dangerously-skip-permissions` |
| `x` | Codex | `codex --yolo` |
| `d` | Droid | `droid` |
| `g` | Grok | `grok --always-approve` |
| `o` | OpenCode | `opencode` |
| `r` | Cursor | `cursor-agent --force` |
| `q` | Qoder | `qodercli` |
| `p` | Pi | `pi` |
| `k` | Kimi Code | `kimi --auto` |

Examples:

```bash
ai x                                      # run one tool in the current shell
ai x resume <session-id>                  # forward arguments to one tool
ai x -- --help                            # pass an ai-looking option to the tool
ai --new x resume <session-id>            # run one tool in a new tmux window/session

ai cxdg                                   # one pane per character
ai cxr                                    # interactively choose a 3-pane layout
ai cxr --layout main-right                # skip the 3-pane prompt
ai -l columns cxr
ai --new cxdg
ai cc                                     # duplicate keys launch duplicate tools
```

Available layouts:

- `auto`: automatic grid; skips the 3-pane prompt
- `main-left`, `main-right`: asymmetric layouts that require exactly three panes
- `columns`, `rows`: one column or row per tool
- Custom column counts such as `1,2` or `2,1`; the sum must equal the number of tools

Tool arguments are supported only when the tools string contains one tool. Multi-tool calls with extra arguments are rejected so incompatible arguments are not sent to multiple CLIs.

Inside tmux, a pane layout reuses the current window only when it has one pane; use `--new` to create a new window. Outside tmux, pane launches create a temporary session and attach to it.

## Restart AI Panes

`ai-restart` refreshes the tmux-resurrect snapshot, finds AI panes with a
recoverable session, checks Agent Activity, and confirms one exact target set.
After confirmation it rechecks the pane/process/activity guard and seals a
versioned, digest-protected execution plan. `aipane-restore-executor` is the
single owner of pane replacement and recovery side effects under one lock.
Pane IDs, layout, and working directories stay unchanged. Recovery commands are
cleared of stale terminal input before launch, and SYNC is disabled on affected
tmux windows so distinct pane commands cannot be broadcast across the window.
Transient failures are retried, and a pane is rebound only after verification.
Grok must report the requested `session loaded`; other Agents must remain alive
for a stability window. Unfinished intent is kept in
`~/.local/share/aipane/restore-pending.json`, so a later continuum save cannot
erase a failed recovery. Qoder and Droid are ignored because their session
restore is not implemented.

The full recovery invariants and failure model live in
[`docs/session-restore-design.md`](docs/session-restore-design.md).

The macOS completion-notification adapters and their sound overrides are indexed in
[`docs/agent-notifications.md`](docs/agent-notifications.md).

```bash
ai-restart --dry-run  # refresh snapshot and preview; does not restart panes
ai-restart            # preview, confirm, then restart and resume
ai-restart --force    # allow panes currently marked busy
```

Pi can change sessions without restarting its process. Install aipane's Pi
session-binding Adapter so `/new`, `/resume`, and `/fork` remain restorable:

```bash
AIPANE_ROOT="${AIPANE_ROOT:-$HOME/.aipane}"
mkdir -p ~/.pi/agent/extensions ~/.local/bin
ln -sf "$AIPANE_ROOT/integrations/pi/aipane-bind.ts" \
  ~/.pi/agent/extensions/aipane-bind.ts
ln -sf "$AIPANE_ROOT/bin/aipane-bind" ~/.local/bin/aipane-bind
```

Run `/reload` in Pi processes that were already open.

The default command aborts the whole operation if any selected Agent Activity
is effectively busy. It resolves Codex turns from exact lifecycle evidence
before the plan and again after confirmation; `--dry-run` remains read-only.
Missing, stale, or inconsistent evidence is shown as `unknown` and requires an
explicit interactive confirmation that all tasks are idle. `--yes` alone never
accepts `unknown`; `--force` is the non-interactive override. Run the command
from a separate terminal outside tmux so it can safely replace every selected
AI pane. This requires
tmux-resurrect and the session-restore hooks described in
[`docs/session-restore-design.md`](docs/session-restore-design.md).

Pi and Claude can own a preassigned `--session-id` without having created a
durable conversation yet. An empty Pi session is reopened as an empty TUI and
reported as `recreated`, never as a resumed conversation. A Pi ID that exists
under another project (or has a damaged record) remains `invalid`. Claude still
requires a durable transcript and is left untouched when none exists.

## Process Cleanup

The wrappers map to the unified cleanup engine as follows:

```text
killcc  → aipane-cleanup ai  --verbose
killmcp → aipane-cleanup mcp --verbose --session-age 18000
killrod → aipane-cleanup rod --force --verbose
```

Start with a dry run:

```bash
killcc --dry-run
killmcp --dry-run
killrod --dry-run
```

Direct usage:

```bash
./bin/aipane-cleanup [all|ai|rod|mcp] \
  [--all|--force] [--max-age SECONDS] [--orphan-age SECONDS] \
  [--session-age SECONDS] [--dry-run] [--verbose|-v] [--help|-h]
```

The `ai` mode recognizes detached Claude, Codex, Droid, Gemini, Grok, OpenCode, and `agent-browser` daemon processes, follows their process trees, and also checks known orphaned AI child processes. `--session-age` additionally enables cleanup of old detached tmux Claude session trees.

In `rod` mode, `--force` selects all matching Rod/Leakless Chromium processes and every matching `ms-playwright/.../chrome-headless-shell` process, regardless of age.

Cleanup actions are logged to `~/logs/aipane-cleanup.log`. Age defaults can be overridden with:

- `AIPANE_AI_ORPHAN_MAX_AGE` (default `900`)
- `AIPANE_AI_SESSION_MAX_AGE` (disabled when unset)
- `AIPANE_ROD_MAX_AGE` (default `300`)
- `AIPANE_MCP_MAX_AGE` (default `21600`)
- `AIPANE_MCP_ORPHAN_MAX_AGE` (default `900`)
- `AIPANE_MCP_SESSION_MAX_AGE` (disabled when unset)
- `AIPANE_TMUX_BIN` (optional explicit tmux path)

## Project Structure

```text
.
├── init.zsh                 # zsh entrypoint
├── aipane.zsh               # compatibility entrypoint
├── lib/core.zsh
├── lib/agent_activity.py    # shared Agent Activity policy + evidence adapters
├── lib/agent_notifications.py # shared notification adaptation + delivery policy
├── lib/recovery_plan.py     # immutable restart plan contract
├── cmd/                     # ai/pane and kill* commands
├── bin/
│   ├── aipane-cleanup
│   ├── aipane-doctor
│   ├── aipane-activity
│   ├── aipane-claude-activity
│   ├── ai-restart
│   ├── ai-restore
│   ├── aipane-restore-executor
│   ├── rod-cleanup
│   ├── tmux-rename-window-popup
│   ├── tmux-colour-palette
│   ├── tmux-window-jump
│   └── tmux-window-wrap     # optional status-bar renderer
├── conf/                    # optional; not sourced by init.zsh
│   ├── ghostty-tmux.conf
│   ├── tmux-workstation.conf
│   └── tmux-window-wrap.conf
├── integrations/            # AI Tool Agent Activity Adapters
│   ├── claude/
│   ├── codex/
│   ├── cursor/
│   ├── grok/
│   ├── kimi/
│   ├── opencode/
│   ├── pi/
│   └── qoder/
├── docs/
│   ├── agent-notifications.md
│   ├── cheatsheet.md
│   ├── ghostty-tmux-workstation.md
│   └── tmux-window-wrap.md
└── tests/
    ├── ai-contracts.zsh
    ├── ai-p-launches-pi.zsh
    ├── aipane-cleanup-ai-protection.sh
    ├── aipane-cleanup-contracts.sh
    ├── init-reload.zsh
    ├── test_ai_restart.py
    ├── test_aipane_doctor.py
    ├── test_agent_activity.py
    ├── test_agent_activity_integrations.py
    ├── test_agent_notification_sounds.py
    ├── test_agent_notifications.py
    ├── test_kimi_notify.py
    ├── test_recovery_plan.py
    ├── test_restore_executor.py
    ├── test_session_restore.py
    ├── test_tmux_window_jump.py
    ├── test_tmux_window_wrap.py
    └── test_workstation_fragments.py
```

## Verification

```bash
zsh -n init.zsh aipane.zsh cmd/*.zsh lib/*.zsh tests/*.zsh
sh -n bin/aipane-cleanup bin/aipane-claude-activity bin/rod-cleanup bin/tmux-colour-palette bin/tmux-rename-window-popup bin/tmux-window-jump tests/*.sh

zsh -fc '
  source ./init.zsh
  type ai killcc killmcp killrod
  for retired in codexx geminii oc; do
    (( $+functions[$retired] || $+aliases[$retired] )) && exit 1
  done
  ai --help >/dev/null
'

./tests/init-reload.zsh
./tests/ai-p-launches-pi.zsh
./tests/ai-contracts.zsh
./tests/aipane-cleanup-ai-protection.sh
./tests/aipane-cleanup-contracts.sh

# optional workstation / window-wrap
python3 tests/test_ai_restart.py
python3 tests/test_aipane_doctor.py
python3 tests/test_agent_activity.py
python3 tests/test_agent_activity_integrations.py
python3 tests/test_agent_notification_sounds.py
python3 tests/test_agent_notifications.py
python3 tests/test_kimi_notify.py
python3 tests/test_recovery_plan.py
python3 tests/test_session_restore.py
python3 tests/test_restore_executor.py
node --experimental-strip-types --test tests/pi-session-binding.test.mjs
python3 tests/test_tmux_window_jump.py
python3 tests/test_workstation_fragments.py
python3 tests/test_tmux_window_wrap.py
```

## License

MIT
