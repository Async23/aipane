# tmux-window-wrap

Optional multi-line tmux status window list (up to 3 rows).

## Codex activity indicators

Each Codex pane whose terminal title starts with Codex's working spinner adds
one fixed dark-shade cell before the window index:

```text
18:theme       # no running Codex pane
▓18:theme      # one running Codex pane
▓▓18:theme     # two running Codex panes
▓▓▓18:theme    # three running Codex panes
```

The glyph never changes; only its foreground colour moves through thirteen
perceptual contrast levels. The 24-frame level sequence is
`0 1 ... 11 12 11 ... 2 1`. Every busy Codex in the session participates in
one global phase distribution, including panes in different windows. Two
indicators use phases `0/12`, three use `0/8/16`, and four use `0/6/12/18`.
Therefore two running panes start at the widest contrast and follow
`0/12 → 1/11 → ... → 11/1 → 12/0 → 11/1 → ... → 1/11`.
This keeps multiple Codex panes distinguishable without synchronizing their
breathing animation. Idle panes and panes showing `[ ! ] Action Required` are
not counted.

For any window with an activity indicator, the normal left padding is omitted
so the indicator begins flush with the preceding window label and index. The
first indicator replaces that omitted padding; additional indicators add one
column each.

Detection depends on the Codex terminal-title `activity` item and animations
being enabled. The indicator width is included in wrapping calculations, and
a lightweight driver advances a tmux animation option every approximately
`50ms` (`20 FPS`) while any busy Codex pane exists. One full loop takes
`1.2s`. Window layout stays cached, so each frame is expanded by tmux without
rerunning the Python layout renderer. The fragment does not lower tmux's
global `status-interval`.

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
