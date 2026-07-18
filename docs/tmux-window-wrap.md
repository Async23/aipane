# tmux-window-wrap

Optional multi-line tmux status window list (up to 3 rows).

## Files

| Path | Role |
|------|------|
| `bin/tmux-window-wrap` | `plan` / `render` / `invalidate` CLI |
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
