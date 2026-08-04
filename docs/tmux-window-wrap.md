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

Busy state has two inputs:

- Codex is detected automatically when a pane title starts with its working
  spinner. Idle panes and panes showing `[ ! ] Action Required` are not counted.
- Other agents can call `tmux-window-wrap activity busy` and
  `tmux-window-wrap activity idle`. The command stores the pane's current
  command in `@tmux-window-wrap-activity`; a marker is honored only while that
  command still owns the pane, so a marker left by an abnormal exit is ignored.

### Kimi Code hooks

Kimi Code can drive the pane-local marker through lifecycle hooks in
`~/.kimi-code/config.toml`:

```toml
[[hooks]]
event = "SessionStart"
command = "$HOME/.local/bin/tmux-window-wrap activity idle"
timeout = 5

[[hooks]]
event = "UserPromptSubmit"
command = "$HOME/.local/bin/tmux-window-wrap activity busy"
timeout = 5

[[hooks]]
event = "PermissionRequest"
command = "$HOME/.local/bin/tmux-window-wrap activity idle"
timeout = 5

[[hooks]]
event = "PermissionResult"
command = "$HOME/.local/bin/tmux-window-wrap activity busy"
timeout = 5

[[hooks]]
event = "Stop"
command = "$HOME/.local/bin/tmux-window-wrap activity idle"
timeout = 5

[[hooks]]
event = "StopFailure"
command = "$HOME/.local/bin/tmux-window-wrap activity idle"
timeout = 5

[[hooks]]
event = "Interrupt"
command = "$HOME/.local/bin/tmux-window-wrap activity idle"
timeout = 5

[[hooks]]
event = "SessionEnd"
command = "$HOME/.local/bin/tmux-window-wrap activity idle"
timeout = 5
```

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

Automatic Codex detection depends on the terminal-title `activity` item and
animations being enabled. External-agent detection depends on its lifecycle
hooks. The indicator width is included in wrapping calculations, and a
lightweight driver advances a tmux animation option every approximately
`50ms` (`20 FPS`) while any busy agent pane exists. One full loop takes
`1.2s`. Window layout stays cached, so each frame is expanded by tmux without
rerunning the Python layout renderer. The cached 24-frame colour lookup uses a
balanced format tree, keeping per-frame expansion depth logarithmic without
changing the frame sequence. Only the current light or dark palette is cached;
a system appearance change updates the cache key and reruns the renderer once.
The fragment does not lower tmux's global `status-interval`.

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
