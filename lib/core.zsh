#!/usr/bin/env zsh
# Shared helpers for aipane.

if [[ "${_AIPANE_CORE_VERSION:-}" == "2" ]]; then
  return 0
fi
typeset -g _AIPANE_CORE_LOADED=1
typeset -g _AIPANE_CORE_VERSION=2

# User-configurable values (export before sourcing init.zsh to override).
typeset -g AIPANE_CLAUDE_LAUNCH_CMD="${AIPANE_CLAUDE_LAUNCH_CMD:-claude --dangerously-skip-permissions}"
typeset -g AIPANE_CODEX_LAUNCH_CMD="${AIPANE_CODEX_LAUNCH_CMD:-codex --yolo}"
typeset -g AIPANE_DROID_LAUNCH_CMD="${AIPANE_DROID_LAUNCH_CMD:-droid}"
typeset -g AIPANE_GROK_LAUNCH_CMD="${AIPANE_GROK_LAUNCH_CMD:-grok --always-approve}"
typeset -g AIPANE_OPENCODE_LAUNCH_CMD="${AIPANE_OPENCODE_LAUNCH_CMD:-opencode}"
typeset -g AIPANE_CURSOR_LAUNCH_CMD="${AIPANE_CURSOR_LAUNCH_CMD:-cursor-agent --force}"
typeset -g AIPANE_QODER_LAUNCH_CMD="${AIPANE_QODER_LAUNCH_CMD:-qodercli}"
typeset -g AIPANE_PI_LAUNCH_CMD="${AIPANE_PI_LAUNCH_CMD:-pi}"
typeset -g AIPANE_KIMI_LAUNCH_CMD="${AIPANE_KIMI_LAUNCH_CMD:-kimi --yolo}"

_aipane_ceil_sqrt() {
  local n="$1"
  local i=1

  while (( i * i < n )); do
    (( i++ ))
  done

  REPLY="$i"
}

_aipane_ensure_tmux() {
  if [[ -n "${TMUX:-}" ]]; then
    REPLY="window" # Create a new window in the current tmux session.
    return 0
  fi

  if ! command -v tmux &>/dev/null; then
    print -u2 "aipane: tmux is required. Install: brew install tmux"
    return 1
  fi

  REPLY="session" # Create a detached session, then attach.
  return 0
}

_aipane_grid_for_count() {
  local count="$1"
  local cols rows c
  local -a per_col

  if (( count <= 0 )); then
    print -u2 "aipane: pane count must be positive"
    return 1
  fi

  if (( count == 1 )); then
    cols=1
    rows=1
    per_col=(1)
  elif (( count == 2 )); then
    cols=2
    rows=1
    per_col=(1 1)
  elif (( count == 3 )); then
    cols=2
    rows=2
    per_col=(1 2)
  elif (( count == 4 )); then
    cols=2
    rows=2
    per_col=(2 2)
  else
    _aipane_ceil_sqrt "$count"
    cols="$REPLY"
    rows=$(( (count + cols - 1) / cols ))
    for (( c = 1; c <= cols; c++ )); do
      per_col+=($(( ((count - c) / cols) + 1 )))
    done
  fi

  REPLY="$cols $rows ${(j: :)per_col}"
}
