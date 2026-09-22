# Ghostty + tmux workstation (aipane)

Optional terminal shell for multi-agent CLI work. **Not** loaded by `init.zsh`.

## What you get

| Layer | Feature |
|-------|---------|
| Ghostty | Cmd chords → tmux prefix (`C-Space`); Shift+Enter → newline for AI CLIs |
| tmux | repeated-digit window jump, centered rename popup, 256-colour palette, zoom-aware pane nav, broadcast + per-pane mute, pane top bar, copy-mode `v`/`Y` |
| window-wrap | multi-line status window list (separate conf) |
| tmux-shot | optional; `Y` in copy-mode via [tmux-shot](https://github.com/Async23/tmux-shot) |

## Files

| Path | Role |
|------|------|
| `conf/ghostty-tmux.conf` | Ghostty keybind bridge + Shift+Enter |
| `conf/tmux-workstation.conf` | tmux prefix, binds, broadcast, status chrome |
| `conf/tmux-window-wrap.conf` | multi-line window list |
| `bin/aipane-doctor` | read-only installation and Agent Activity wiring audit |
| `bin/tmux-rename-window-popup` | stable-target window rename UI (requires `fzf`) |
| `bin/tmux-close-window-popup` | stable-target close confirmation with mouse and keyboard input |
| `bin/tmux-colour-palette` | indexed terminal colour palette (`0–255`) |
| `bin/tmux-window-jump` | repeated-digit exact-index window selector |
| `bin/aipane-activity` | Agent Activity CLI |
| `bin/tmux-window-wrap` | renderer (symlink to `~/.local/bin`) |
| `docs/cheatsheet.md` | key map |

## Install

```bash
AIPANE_ROOT="${AIPANE_ROOT:-$HOME/.aipane}"
mkdir -p ~/.local/bin

ln -sf "$AIPANE_ROOT/bin/aipane-doctor" ~/.local/bin/aipane-doctor
ln -sf "$AIPANE_ROOT/bin/tmux-rename-window-popup" ~/.local/bin/tmux-rename-window-popup
ln -sf "$AIPANE_ROOT/bin/tmux-close-window-popup" ~/.local/bin/tmux-close-window-popup
ln -sf "$AIPANE_ROOT/bin/tmux-colour-palette" ~/.local/bin/tmux-colour-palette
ln -sf "$AIPANE_ROOT/bin/tmux-window-jump" ~/.local/bin/tmux-window-jump
ln -sf "$AIPANE_ROOT/bin/aipane-activity" ~/.local/bin/aipane-activity
ln -sf "$AIPANE_ROOT/bin/tmux-window-wrap" ~/.local/bin/tmux-window-wrap
```

The rename popup requires `fzf` on `PATH`.

Run `aipane-doctor` after installation and after pulling changes that add or
move workstation executables. It is read-only: missing or misdirected required
symlinks fail the command, while supported compatibility commands are warnings.
For installed dsh TUI and Web profiles, it also checks the effective plugin
composition, helper paths and notification App; see [dsh integrations](dsh-integration.md).

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
colour palette, grouped into ANSI colours, an RGB cube, and grayscale. Scroll
with the arrow keys and press `q` or `Esc` to close it.

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

### Agent-aware pane and window close

`prefix x` / `Cmd+W` closes ordinary panes immediately. When the selected pane
is the window's last pane, aipane checks that pane's live process tree first:
an AI Tool opens a centered confirmation popup, a verified non-Agent closes
immediately, and an unavailable or inconclusive detector opens the same popup as
the safe fallback. Press `y` or click **Close window** to close it. Press `n`
or `Esc`, or click **Cancel**, to leave it open. Arrow keys select a row and
Enter chooses it; Enter with no selected row cancels. Mouse clicks always
choose the row under the pointer, even after the keyboard selected another row.
Moving the pointer or clicking outside the popup leaves the popup open. An idle
AI Tool is still an Agent for this decision; the animated activity marker is not
required.

The popup uses Python's standard-library `curses` module through
`tmux-close-window-popup` on the tmux server's `PATH`. If the helper is missing,
the close is cancelled with an installation message. Override its path before
sourcing `tmux-workstation.conf` when needed:

```tmux
set -g @aipane-close-window-popup-command '/absolute/path/to/tmux-close-window-popup'
```

The window tab's right-click menu (including Option/right-click) uses the same
check for **Kill**. Because this action closes the entire window, it checks
every pane in that window and asks for confirmation if any pane contains an
AI Tool or cannot be classified. A window containing only verified non-Agent
panes closes immediately. The clicked window remains the target, even when it
is inactive or window indexes change before confirmation.

The default detector is `tmux-window-wrap` on the tmux server's `PATH`. Override
its executable path before sourcing `tmux-workstation.conf` if needed:

```tmux
set -g @aipane-agent-status-command '/absolute/path/to/tmux-window-wrap'
```

### Repeated-digit window jump

`Cmd+1…9` on the main number row and direct `prefix 1…9` select the exact tmux
window index on the first press. Repeating the same shortcut within 700ms adds
ten each time:
`1 → 11 → 21` and `9 → 19 → 29`. `Cmd+0` / `prefix 0` use the same chain for
decades: `10 → 20 → 30`. The timeout slides after every accepted press. A
different digit or an external window change starts a new chain.

If a target does not exist, tmux stays on the last valid window, displays one
message, and ignores repeats of that digit until 700ms of silence. If
`tmux-window-jump` is not installed, `1…9` fall back to one-shot `:=N` and `0`
falls back to one-shot `:=10`.

Override the timeout before sourcing `tmux-workstation.conf`:

```tmux
set -g @tmux-window-jump-timeout-ms 700
```

The default is `700`; an empty, zero, or nonnumeric value also falls back to
`700`.

## Ownership

Canonical sources live in this repo. Personal configs only **include/source** them.

Do not paste large keybind or `status-format` blocks back into home copies.

Not in this package: `pane-col.sh`, company skills, full agent home dirs.

## Related

- [tmux-window-wrap.md](./tmux-window-wrap.md)
- [cheatsheet.md](./cheatsheet.md)

## Verify

```bash
python3 tests/test_aipane_doctor.py
python3 tests/test_tmux_window_jump.py
python3 tests/test_workstation_fragments.py
python3 tests/test_tmux_window_wrap.py
```
