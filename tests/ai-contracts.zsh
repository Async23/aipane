#!/usr/bin/env zsh

set -eu

ROOT_DIR="${0:A:h:h}"
TMUX_BIN="$(command -v tmux)"
SOCKET_NAME="aipane-ai-contracts-$$"
TEST_DIR="$(mktemp -d -t aipane-ai-contracts.XXXXXX)"

cleanup() {
  command "$TMUX_BIN" -L "$SOCKET_NAME" kill-server >/dev/null 2>&1 || true
  rm -rf "$TEST_DIR"
}
trap cleanup EXIT HUP INT TERM

command "$TMUX_BIN" -L "$SOCKET_NAME" -f /dev/null \
  new-session -d -s contract -x 100 -y 30 -n base "sleep 120"

tmux() {
  command "$TMUX_BIN" -L "$SOCKET_NAME" "$@"
}

export TMUX="aipane-test"
export AIPANE_STATE_DIR="$TEST_DIR/state"
export AIPANE_REGISTRY="$AIPANE_STATE_DIR/registry.jsonl"
export AIPANE_CLAUDE_LAUNCH_CMD="sleep 120"
export AIPANE_CODEX_LAUNCH_CMD="sleep 120"
export AIPANE_DSH_LAUNCH_CMD="sleep 120"
export AIPANE_GROK_LAUNCH_CMD="sleep 120"
export AIPANE_PI_LAUNCH_CMD="sleep 120"
export AIPANE_KIMI_LAUNCH_CMD="sleep 120"
export AIPANE_AGY_LAUNCH_CMD="sleep 120"
export AIPANE_NO_SESSION_ID=1

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

keys="$(ai --help | awk '/^Tools:/{tools=1;next} /^$/{tools=0} tools{printf "%s", $1}')"
[[ "$keys" == cxdgpka ]] || { print -u2 "unexpected launcher keys: $keys"; exit 1; }
for key in o r q; do
  if ai "$key" -- --version >"$TEST_DIR/error" 2>&1 || ! grep -q "unknown tool '$key'" "$TEST_DIR/error"; then
    print -u2 "unexpected launcher for '$key'"
    exit 1
  fi
done

assert_balanced_columns "rows layout" rows xdg
assert_balanced_columns "custom 1,3 layout" 1,3 xdgp
assert_balanced_columns "automatic seven-pane layout" auto cxdgpka

# Subtree in-place split in a 3-column window (portrait pane -> vertical split,
# preserving sibling panes, inheriting $PWD, and recording registry bindings).
SUBDIR="$TEST_DIR/sub workspace"
mkdir -p "$SUBDIR"
win3="$(tmux new-window -t contract -x 151 -y 43 -P -F '#{window_id}' 2>/dev/null || tmux new-window -t contract -P -F '#{window_id}')"
tmux resize-window -t "$win3" -x 151 -y 43
p1="$(tmux display-message -t "$win3" -p '#{pane_id}')"
p2="$(tmux split-window -h -d -t "$p1" -P -F '#{pane_id}')"
p3="$(tmux split-window -h -d -t "$p2" -P -F '#{pane_id}')"
tmux select-layout -t "$win3" even-horizontal
before_p1="$(tmux display-message -t "$p1" -p '#{pane_left},#{pane_top},#{pane_width},#{pane_height}')"
before_p2="$(tmux display-message -t "$p2" -p '#{pane_left},#{pane_top},#{pane_width},#{pane_height}')"
p3_left="$(tmux display-message -t "$p3" -p '#{pane_left}')"

(
  cd "$SUBDIR"
  TMUX_PANE="$p3" ai ax
)

after_p1="$(tmux display-message -t "$p1" -p '#{pane_left},#{pane_top},#{pane_width},#{pane_height}')"
after_p2="$(tmux display-message -t "$p2" -p '#{pane_left},#{pane_top},#{pane_width},#{pane_height}')"
if [[ "$before_p1" != "$after_p1" || "$before_p2" != "$after_p2" ]]; then
  print -u2 "in-place split altered sibling panes: p1=$before_p1->$after_p1 p2=$before_p2->$after_p2"
  exit 1
fi

col3_panes=("${(@f)$(tmux list-panes -t "$win3" -F '#{pane_id} #{pane_left} #{pane_height} #{pane_current_path}' | awk -v l="$p3_left" '$2 == l {print}')}")
if (( ${#col3_panes[@]} != 2 )); then
  print -u2 "expected portrait pane to split into 2 stacked rows at left=$p3_left, got: ${(j:; :)col3_panes}"
  exit 1
fi
h_top="${${(s: :)col3_panes[1]}[3]}"
h_bot="${${(s: :)col3_panes[2]}[3]}"
new_p4_id="${${(s: :)col3_panes[2]}[1]}"
new_p4_cwd="${col3_panes[2]#* * * }"
if (( h_top - h_bot > 1 || h_bot - h_top > 1 )); then
  print -u2 "stacked subtree heights are unbalanced: $h_top vs $h_bot"
  exit 1
fi
if [[ "${new_p4_cwd:A}" != "${SUBDIR:A}" ]]; then
  print -u2 "new split pane did not inherit PWD: expected ${SUBDIR:A}, got ${new_p4_cwd:A}"
  exit 1
fi
if ! grep -q "\"pane\":\"$p3\",\"tool\":\"a\"" "$AIPANE_REGISTRY" ||
   ! grep -q "\"pane\":\"$new_p4_id\",\"tool\":\"x\"" "$AIPANE_REGISTRY"; then
  print -u2 "registry did not record exact subtree pane bindings"
  exit 1
fi

# Wide sub-pane (landscape -> horizontal split with balanced widths, sibling untouched).
win_wide="$(tmux new-window -t contract -P -F '#{window_id}')"
tmux resize-window -t "$win_wide" -x 151 -y 43
top_pane="$(tmux display-message -t "$win_wide" -p '#{pane_id}')"
bot_pane="$(tmux split-window -v -d -l 20 -t "$top_pane" -P -F '#{pane_id}')"
before_top="$(tmux display-message -t "$top_pane" -p '#{pane_left},#{pane_top},#{pane_width},#{pane_height}')"
bot_top_coord="$(tmux display-message -t "$bot_pane" -p '#{pane_top}')"
TMUX_PANE="$bot_pane" ai ax
after_top="$(tmux display-message -t "$top_pane" -p '#{pane_left},#{pane_top},#{pane_width},#{pane_height}')"
[[ "$before_top" == "$after_top" ]] || { print -u2 "wide split altered top sibling"; exit 1; }
row_bot_panes=("${(@f)$(tmux list-panes -t "$win_wide" -F '#{pane_top} #{pane_width}' | awk -v t="$bot_top_coord" '$1 == t {print $2}')}")
if (( ${#row_bot_panes[@]} != 2 )) || (( row_bot_panes[1] - row_bot_panes[2] > 1 || row_bot_panes[2] - row_bot_panes[1] > 1 )); then
  print -u2 "expected wide sub-pane to split into 2 balanced columns, got widths: ${(j:,:)row_bot_panes}"
  exit 1
fi

# Zoomed narrow pane: rejected layout restores zoom and leaves pane count intact;
# auto layout unzooms and splits using true unzoomed geometry.
win_zoom="$(tmux new-window -t contract -P -F '#{window_id}')"
tmux resize-window -t "$win_zoom" -x 151 -y 43
zp1="$(tmux display-message -t "$win_zoom" -p '#{pane_id}')"
zp2="$(tmux split-window -h -d -l 113 -t "$zp1" -P -F '#{pane_id}')"
zp3="$(tmux split-window -h -d -l 75 -t "$zp2" -P -F '#{pane_id}')"
zp4="$(tmux split-window -h -d -l 37 -t "$zp3" -P -F '#{pane_id}')"
tmux resize-pane -Z -t "$zp4"
if TMUX_PANE="$zp4" ai -l cols ax >"$TEST_DIR/zoom-err" 2>&1; then
  print -u2 "expected 37-wide zoomed pane to reject 2-column split"
  exit 1
fi
if ! grep -q "too small" "$TEST_DIR/zoom-err" ||
   [[ "$(tmux display-message -t "$win_zoom" -p '#{window_zoomed_flag} #{window_panes}')" != "1 4" ]]; then
  print -u2 "failed zoomed split did not restore zoom or left stray panes"
  exit 1
fi
TMUX_PANE="$zp4" ai ax
if [[ "$(tmux display-message -t "$win_zoom" -p '#{window_zoomed_flag} #{window_panes}')" != "0 5" ]]; then
  print -u2 "zoomed auto split did not unzoom and split into stacked panes"
  exit 1
fi

# Saturated small pane: atomic pre-flight rejection with zero stray panes.
win_tiny="$(tmux new-window -t contract -P -F '#{window_id}')"
tmux resize-window -t "$win_tiny" -x 37 -y 12
tiny_pane="$(tmux display-message -t "$win_tiny" -p '#{pane_id}')"
if TMUX_PANE="$tiny_pane" ai ax >"$TEST_DIR/tiny-err" 2>&1; then
  print -u2 "expected tiny 37x12 pane to reject split"
  exit 1
fi
if ! grep -q "use --new" "$TEST_DIR/tiny-err" ||
   [[ "$(tmux display-message -t "$win_tiny" -p '#{window_panes}')" != "1" ]]; then
  print -u2 "tiny pane split did not fail atomically"
  exit 1
fi

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
