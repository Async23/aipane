# tmux-window-wrap

Optional multi-line tmux status window list (up to 3 rows).

## Pane count annotations

Window labels show the pane total as subscript digits when a window owns more
than one pane:

```text
1:single       # one pane; the implicit count stays hidden
2₄:grid         # four panes
3₁₂:large-grid  # twelve panes; multi-digit totals stay exact
```

The annotation follows the window index, inherits its style, remains visible
while a pane is zoomed, and participates in the renderer's width and wrapping
calculations. Busy activity cells remain flush with the combined index, for
example `▓▓▓2₄:grid` means three busy agent panes in a four-pane window.

The public default is `subscript`. Set the option before sourcing
`conf/tmux-window-wrap.conf` to select a compatibility style or disable the
annotation:

```tmux
set -g @tmux-window-wrap-pane-count-style 'plain'  # 2[4]:grid
set -g @tmux-window-wrap-pane-count-style 'off'    # 2:grid
```

Accepted values are `subscript`, `plain`, and `off`.

## Agent activity indicators

Each busy agent pane adds one fixed dark-shade cell before the window index:

```text
18:theme       # no busy agent pane
▓18:theme      # one busy agent pane
▓▓18:theme     # two busy agent panes
▓▓▓18:theme    # three busy agent panes
```

The glyph never changes; only its foreground colour moves through thirteen
perceptual contrast levels. The 24-frame level sequence is
`0 1 ... 11 12 11 ... 2 1`. Every busy pane in the session participates in
one global phase distribution, including panes in different windows. Two
indicators use phases `0/12`, three use `0/8/16`, and four use `0/6/12/18`.
Therefore two running panes start at the widest contrast and follow
`0/12 → 1/11 → ... → 11/1 → 12/0 → 11/1 → ... → 1/11`.
This keeps multiple agent panes distinguishable without synchronizing their
breathing animation.
The animator advances exactly one adjacent level per rendered frame. If a tmux
or theme probe is briefly slow, the current frame lasts slightly longer rather
than skipping levels to catch up, which avoids visible jumps.

AI Tool integrations report state with `tmux-window-wrap activity busy` and
`tmux-window-wrap activity idle`. The command stores the pane's current command
in `@tmux-window-wrap-activity`; a marker is honored only while that command
still owns the pane, so a marker left by an abnormal exit is ignored.
The safety source is a versioned JSON record in
`@tmux-window-wrap-activity-record`. It binds the report to the tmux pane,
server, TTY, and exact Agent process; Codex records also carry the root session,
turn, transcript, and per-pane `CODEX_HOME`. The older scalar options remain
display/compatibility projections and are written only after the record.
Every call also records the current command in
`@tmux-window-wrap-activity-reporter`. This lets destructive maintenance tools
distinguish a reported `idle` state from an older Agent process whose state is
unknown; the reporter option does not affect statusline rendering.
Busy reports also store their wall-clock time in
`@tmux-window-wrap-activity-updated-at`. The timestamp prevents an older idle
record from clearing a newly submitted turn.

### Agent Activity Interface

Agent Activity is turn-level: `busy` starts when the user submits a prompt and
ends when the AI Tool completes or fails the turn. New AI Tool integrations
should report this state explicitly through `tmux-window-wrap activity
busy|idle`. Hooks and plugins inherit `TMUX_PANE`, so no pane id needs to appear
in model messages.

Claude Code agent-team teammates do not receive a user-submitted prompt, even
when each teammate owns a separate tmux pane. The Claude Adapter therefore
marks a teammate busy from `SessionStart(agent_type=...)`, refreshes busy at
`PreToolUse` for later delegated turns, and clears it at `TeammateIdle`.
`SessionStart(source=compact)` is ignored because compaction happens inside an
already-running turn. This preserves one activity cell per actually busy
teammate pane without reading terminal titles.

Codex also reports `busy` from `PreToolUse`. This is a deliberate fallback for
`/goal`: goal work can start as an internal `thread_goal_updated -> task_started`
sequence without an ordinary user-message event, so `UserPromptSubmit` does not
cover that entry path.

Codex is a deliberate exception to the usual `Stop -> idle` mapping. A Codex
`Stop` hook may add feedback and make the same turn continue, so reporting idle
there would hide an Agent that is still working. Its existing `notify` wrapper
reports idle only when `agent-turn-complete` matches the record's exact root
session and turn; a late completion from an older turn cannot clear newer work.
`SessionEnd` remains the cleanup fallback. `SessionStart` is restricted to
`startup|resume|clear`, because Codex
also emits it after an in-turn context compaction. The activity command ignores
both an inherited `Stop` idle report and a `SessionStart(source=compact)` idle
report, so already-running Codex processes that loaded older hook configuration
do not regress before their next restart. The notify wrapper resolves each
completing Codex thread through its rollout metadata: subagent completions keep
the parent pane busy and bind the root thread id, while a root completion stays
busy when its goal is still active.

Codex can terminate a failed turn before its legacy notifier runs (for example,
a remote-compaction stream failure). Once per activity probe—not in the 20 FPS
render path—the shared resolver checks the exact turn in Codex's read-only
thread-history projection. It uses that projection only when its byte offset has
caught up with the canonical rollout; otherwise it reads a bounded rollout tail.
`completed`, `failed`, and `interrupted` are terminal, while a newer
`inProgress` turn or active goal remains busy. A repair is materialized only
after two identical observations and an identity/revision recheck under the
pane lock. Missing, malformed, locked, mismatched, or unfamiliar evidence is
`unknown` and never clears a busy marker.

`ai-restart` calls the same resolver before showing its plan and again after
confirmation. Resolution is read-only, so `--dry-run` does not repair tmux
options; `unknown` continues to require interactive acknowledgement or
`--force`. This also protects detached panes that have no attached statusline
animator.

Terminal titles are presentation owned by each AI Tool and are not activity
inputs. Versioned hook fragments and plugin source live under
`integrations/`; user-owned Agent configuration only registers those Adapters.

Claude Code also maintains a live session record under
`${CLAUDE_CONFIG_DIR:-~/.claude}/sessions/` with its exact tmux target and
`busy` / `idle` status. The animation probe reconciles a matching Claude
marker against that record. This is a narrow fallback for hard failure paths
such as a context-limit rejection that can return Claude to its prompt without
delivering the expected completion hook. A live record must match the pane's
tmux target, TTY, and Claude version, and its idle timestamp must be at least as
new as the busy marker; stale records therefore cannot clear a later turn.

Grok skips both `Stop` and `StopFailure` when a turn is interrupted, refused,
or stopped by its turn limit. The animation probe therefore reconciles a
marked Grok pane with `${GROK_HOME:-~/.grok}/active_sessions.json` and the
session's authoritative `updates.jsonl` stream. The live process, TTY, session
id, and Grok command must agree, and only a `turn_completed` timestamp at least
as new as the busy marker clears it. A previous turn's completion therefore
cannot clear a newly submitted turn. Other AI Tools remain hook/plugin-driven.

### Kimi Code hooks

Kimi Code can drive the pane-local marker through lifecycle hooks in
`~/.kimi-code/config.toml`. Merge
[`integrations/kimi/hooks.toml`](../integrations/kimi/hooks.toml) into that
file. The fragment uses `TurnStarted`, available since Kimi Code 0.32.0, so
slash skills, plugin commands, goal continuations, and other non-user turn
origins are covered. `UserPromptSubmit` is not a complete activity boundary
because Kimi only emits it for turns whose origin is `user`. The turn stays
busy while Kimi waits for permission; permission events do not create a
tool-specific interpretation of Agent Activity.

The helper uses `TMUX_PANE`, which Kimi inherits from tmux. Run `/reload` in an
idle Kimi session or start a new Kimi process after changing the hook
configuration. Outside tmux, the helper is a silent no-op.

### Pi extension

Pi can report its lifecycle through the installable
[`@async23/pi-tmux-window-wrap`](https://github.com/Async23/pi-packages/tree/main/packages/tmux-window-wrap)
package:

```bash
pi install npm:@async23/pi-tmux-window-wrap
```

Restart Pi after installation, or run `/reload` in an idle session. The
extension marks the pane busy on `agent_start` and clears it on
`agent_settled`, after retries, compaction, and queued follow-ups have
finished. It also clears stale state on session startup and shutdown.

To pulse the current window indicator for 1.2 seconds:

```text
/tmux-window-wrap-test
```

Outside tmux, or when `~/.local/bin/tmux-window-wrap` is unavailable, the
extension is a silent no-op.

For any window with an activity indicator, the normal left padding is omitted
so the indicator begins flush with the preceding window label and index. The
first indicator replaces that omitted padding; additional indicators add one
column each.

Agent Activity depends on each AI Tool's lifecycle hooks or plugin Adapter. The
indicator width is included in wrapping calculations, and a
lightweight driver advances a tmux animation option every approximately
`50ms` (`20 FPS`) while any busy agent pane exists. One full loop takes
`1.2s`. Window layout stays cached, so each frame is expanded by tmux without
rerunning the Python layout renderer. The cached 24-frame colour lookup uses a
balanced format tree, keeping per-frame expansion depth logarithmic without
changing the frame sequence. The driver advances exactly one palette level per
rendered frame; a slow background probe stretches that frame instead of
skipping a level, which keeps the breathing transition continuous. Only the
current light or dark palette is cached;
a system appearance change updates the cache key and reruns the renderer once.
The fragment does not lower tmux's global `status-interval`.

The animator records uncaught exceptions in
`~/.local/state/aipane/tmux-window-wrap-animate.log`. A logged crash ends the
animator cleanly so tmux does not replace the active pane with its background
job error view. Normal ownership handoff between animator processes is not
logged.

Four thirteen-colour palettes cover active/inactive windows in light/dark
terminal themes:

```tmux
set -g @tmux-window-wrap-activity-light-inactive 'RRGGBB,...'
set -g @tmux-window-wrap-activity-light-active 'RRGGBB,...'
set -g @tmux-window-wrap-activity-dark-inactive 'RRGGBB,...'
set -g @tmux-window-wrap-activity-dark-active 'RRGGBB,...'
```

Set personal palettes before sourcing `conf/tmux-window-wrap.conf`. The public
fragment supplies neutral fallbacks. On macOS the animation driver checks the
system appearance and updates `@tmux-window-wrap-color-scheme` automatically.
After each indicator, the renderer restores the normal window-name foreground
colour.

## Files

| Path | Role |
|------|------|
| `bin/tmux-window-wrap` | `plan` / `render` / `animate` / `invalidate` CLI |
| `lib/agent_activity.py` | shared report/resolution policy and AI Tool evidence adapters |
| `conf/tmux-window-wrap.conf` | Public fragment: `status-format` + lifecycle hooks |
| `tests/test_tmux_window_wrap.py` | Unit + live tmux tests |

## Install

`AIPANE_ROOT` is wherever you cloned this repo (default install path: `~/.aipane`).

```bash
AIPANE_ROOT="${AIPANE_ROOT:-$HOME/.aipane}"
ln -sf "$AIPANE_ROOT/bin/tmux-window-wrap" ~/.local/bin/tmux-window-wrap
```

In personal `~/.tmux.conf` (do not inline the fragment):

```tmux
source-file ~/.aipane/conf/tmux-window-wrap.conf
```

Not loaded by `init.zsh`. Symlink the binary; do not copy the script body into `~/.local/bin`.

## Ownership (for humans and agents)

**Canonical source is this repo.** Machine wiring:

1. Symlink binary → `~/.local/bin/tmux-window-wrap`
2. Personal `~/.tmux.conf` only `source-file`s the conf — no inlined `status-format` / wrap hooks

Do **not** commit a full personal `~/.tmux.conf` (prefix, theme, TPM, keybinds).

`conf/tmux-window-wrap.conf` must stay public: no `set -g prefix`, no `@plugin` / tpm.

### When a change belongs here

If the edit touches wrap render/plan/invalidate, `TMUX_WINDOW_WRAP_*` keys, wrap `status-format`, hooks, or tests → update **`bin/` + `conf/` + `tests/` here**.

Personal-only (prefix, splits, colors, plugins, non-wrap status text) → only `~/.tmux.conf`.

### After changes

```bash
python3 tests/test_tmux_window_wrap.py
```

Personal `synchronize-panes` bindings should also run:

```tmux
run-shell -b "$HOME/.local/bin/tmux-window-wrap invalidate"
```
