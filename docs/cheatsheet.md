# Ghostty + tmux cheatsheet (aipane workstation)

Prefix is **Ctrl-Space**. In Ghostty, **Cmd+S** sends the general tmux prefix;
**Cmd+Opt+P** opens the pane ID popup in one shot. Other mapped Cmd shortcuts
also send complete prefix+key chords.

## Ghostty Cmd bridge

| Key | Action |
|-----|--------|
| `Cmd+S` | prefix only |
| `Cmd+Opt+P` | popup pane ID list |
| `Cmd+T` | new window |
| `Cmd+W` | kill pane |
| `Cmd+D` / `Cmd+Shift+D` | split L-R / T-B |
| `Cmd+[` / `Cmd+]` | prev / next pane |
| `Cmd+Shift+[` / `]` | prev / next window |
| `Cmd+I` | centered popup rename window |
| `Cmd+Shift+I` | rename session |
| `Cmd+Shift+Enter` | zoom pane |
| `Cmd+1…9` / `Cmd+0` | select window (0 = last) |
| `Cmd+Opt+I` | broadcast on/off (window) |
| `Cmd+Opt+Shift+I` | this pane join/leave broadcast |
| `Shift+Enter` | newline for AI CLIs (→ Ctrl+J) |

## Pane navigation (zoom-aware)

`prefix` then `h/j/k/l` or `[` / `]`. While zoomed, focus change keeps zoom.

```
         k
         ↑
   h ←   ·   → l
         ↓
         j
```

`-r` on hjkl: hold prefix once, tap directions repeatedly.

## Windows

| Key | Action |
|-----|--------|
| `prefix 1-9` | select window |
| `prefix 0` / `Cmd+0` | last window |
| `prefix p` / `n` | prev / next window |
| `prefix ,` / `Cmd+I` | centered popup rename current window |
| `Option+,` / `Option+.` (`M-</>`) | reorder window left / right |
| `prefix w` | window list (tmux default) |

## Sessions

| Key | Action |
|-----|--------|
| `prefix N` | new session |
| `prefix W` | kill session (confirm) |
| `prefix s` | session tree (tmux default) |
| `prefix d` | detach |

## Move window / pane

| Key | Action |
|-----|--------|
| `prefix m` | move current window to another session |
| `prefix S` / `V` | pull a pane here (vertical / horizontal join) |
| `prefix !` | break pane to its own window |
| `prefix >` / `<` | swap pane with next / previous |

## Splits & layout

| Key | Action |
|-----|--------|
| `prefix \|` or `\` | split left-right |
| `prefix -` | split top-bottom |
| `prefix Space` | cycle layouts (tmux default) |

## Resize (no prefix)

| Key | Action |
|-----|--------|
| `Option+Shift+H/J/K/L` | resize by 2 cells |

Requires Ghostty `macos-option-as-alt = true` (in `ghostty-tmux.conf`).

## Broadcast

| Key | Action |
|-----|--------|
| `prefix B` / `Cmd+Opt+I` | sync input to all panes in window |
| `prefix e` / `Cmd+Opt+Shift+I` | mute / unmute this pane only |

Status: yellow `SYNC`; pane bar / window label show `▶` when synced.

## Copy mode

```
prefix+v → move → v (or C-v rect) → y (clipboard) or Y (tmux-shot)
```

| Key | Action |
|-----|--------|
| `prefix v` | copy-mode |
| `y` | copy to macOS clipboard |
| `Y` | screenshot selection via `tmux-shot-capture` (if installed) |
| `prefix b` | paste tmux buffer |

## Other

| Key | Action |
|-----|--------|
| `prefix P` | indexed colour palette (`0–255`) |
| `prefix Q` / `Cmd+Opt+P` | popup pane ID list |
| double-click status window label | zoom that window's pane |
| multi-line window list | `tmux-window-wrap` when labels overflow |
