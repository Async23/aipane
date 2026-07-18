#!/usr/bin/env zsh

set -eu

ROOT_DIR="${0:A:h:h}"
TMUX_BIN="$(command -v tmux)"
SOCKET_NAME="aipane-ai-contracts-$$"

cleanup() {
  command "$TMUX_BIN" -L "$SOCKET_NAME" kill-server >/dev/null 2>&1 || true
}
trap cleanup EXIT HUP INT TERM

command "$TMUX_BIN" -L "$SOCKET_NAME" -f /dev/null \
  new-session -d -s contract -x 100 -y 30 -n base "sleep 120"

tmux() {
  command "$TMUX_BIN" -L "$SOCKET_NAME" "$@"
}

export TMUX="aipane-test"
export AIPANE_CLAUDE_LAUNCH_CMD="sleep 120"
export AIPANE_CODEX_LAUNCH_CMD="sleep 120"
export AIPANE_DROID_LAUNCH_CMD="sleep 120"
export AIPANE_GROK_LAUNCH_CMD="sleep 120"
export AIPANE_OPENCODE_LAUNCH_CMD="sleep 120"
export AIPANE_CURSOR_LAUNCH_CMD="sleep 120"
export AIPANE_QODER_LAUNCH_CMD="sleep 120"
export AIPANE_PI_LAUNCH_CMD="sleep 120"

source "$ROOT_DIR/init.zsh"

assert_balanced_columns() {
  local label="$1"
  local layout="$2"
  local tools="$3"
  local window_id left height
  local -A minimums maximums values

  ai --new --layout "$layout" "$tools"
  window_id="$(tmux list-windows -t contract -F '#{window_id}' | tail -1)"

  while read -r left height; do
    if [[ -z "${minimums[$left]:-}" ]]; then
      minimums[$left]="$height"
      maximums[$left]="$height"
      values[$left]="$height"
      continue
    fi
    (( height < minimums[$left] )) && minimums[$left]="$height"
    (( height > maximums[$left] )) && maximums[$left]="$height"
    values[$left]+=",$height"
  done < <(tmux list-panes -t "$window_id" -F '#{pane_left} #{pane_height}')

  for left in "${(@k)minimums}"; do
    if (( maximums[$left] - minimums[$left] > 1 )); then
      print -u2 -r -- "$label is not balanced at column $left: heights=${values[$left]}"
      exit 1
    fi
  done
}

assert_balanced_columns "rows layout" rows xdg
assert_balanced_columns "custom 1,3 layout" 1,3 xdgo
assert_balanced_columns "automatic seven-pane layout" auto cxdgorp

tmux() {
  case "$1" in
    new-window)
      print -r -- "@99 %99"
      ;;
    list-panes)
      print -r -- "%99 0 0"
      ;;
    send-keys)
      return 17
      ;;
    select-pane)
      return 0
      ;;
    *)
      print -u2 -r -- "unexpected fake tmux call: $*"
      return 99
      ;;
  esac
}

if ai --new x >/dev/null 2>&1; then
  print -u2 "ai reported success after tmux send-keys failed"
  exit 1
fi

if ai >/dev/null 2>&1; then
  print -u2 "ai reported success without the required tools string"
  exit 1
fi

print "ai contract tests passed"
