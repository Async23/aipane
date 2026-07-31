# Ghostty + tmux workstation (aipane)

Optional terminal shell for multi-agent CLI work. **Not** loaded by `init.zsh`.

## What you get

| Layer | Feature |
|-------|---------|
| Ghostty | Cmd chords → tmux prefix (`C-Space`); Shift+Enter → newline for AI CLIs |
| tmux | centered window rename popup, 256-colour palette, zoom-aware pane nav, broadcast + per-pane mute, pane top bar, copy-mode `v`/`Y` |
| window-wrap | multi-line status window list (separate conf) |
| tmux-shot | optional; `Y` in copy-mode via [tmux-shot](https://github.com/Async23/tmux-shot) |

## Files

| Path | Role |
|------|------|
| `conf/ghostty-tmux.conf` | Ghostty keybind bridge + Shift+Enter |
| `conf/tmux-workstation.conf` | tmux prefix, binds, broadcast, status chrome |
| `conf/tmux-window-wrap.conf` | multi-line window list |
| `bin/tmux-rename-window-popup` | stable-target window rename UI (requires `fzf`) |
| `bin/tmux-colour-palette` | indexed terminal colour palette (`0–255`) |
| `bin/tmux-window-wrap` | renderer (symlink to `~/.local/bin`) |
| `docs/cheatsheet.md` | key map |

## Install

```bash
AIPANE_ROOT="${AIPANE_ROOT:-$HOME/.aipane}"

ln -sf "$AIPANE_ROOT/bin/tmux-rename-window-popup" ~/.local/bin/tmux-rename-window-popup
ln -sf "$AIPANE_ROOT/bin/tmux-colour-palette" ~/.local/bin/tmux-colour-palette
ln -sf "$AIPANE_ROOT/bin/tmux-window-wrap" ~/.local/bin/tmux-window-wrap
```

The rename popup requires `fzf` on `PATH`.

### Ghostty (`~/.config/ghostty/config`)

Keep font/theme personal; include the public fragment:

```ini
# personal font/theme...
config-file = /Users/YOU/.aipane/conf/ghostty-tmux.conf
```

Use an absolute path (or a path Ghostty can resolve). Reload Ghostty config after edits.

`Cmd+S` remains the prefix-only starter for general tmux commands. Pane ID
lookup does not require the two-step sequence: use `Cmd+Opt+P` for its one-shot
popup. `Cmd+I` opens a centered window rename popup prefilled with the current
name; the binding expands and captures `window_id` before the popup opens, so
index renumbering cannot change the target. `Ctrl-Space` also provides direct
access to the tmux prefix. Use `prefix P` to open the complete indexed terminal
colour palette; scroll with the arrow keys and press `q` to close it.

### tmux (`~/.tmux.conf`)

```tmux
# optional personal: reload, TPM, etc.
bind r source-file ~/.tmux.conf \; display "Config reloaded!"

source-file ~/.aipane/conf/tmux-workstation.conf
source-file ~/.aipane/conf/tmux-window-wrap.conf

# optional personal plugins last
# run '~/.tmux/plugins/tpm/tpm'
```

Then: `tmux source-file ~/.tmux.conf`

## Ownership

Canonical sources live in this repo. Personal configs only **include/source** them.

Do not paste large keybind or `status-format` blocks back into home copies.

Not in this package: `claude-guard`, `pane-col.sh`, company skills, full agent home dirs.

## Related

- [tmux-window-wrap.md](./tmux-window-wrap.md)
- [cheatsheet.md](./cheatsheet.md)
