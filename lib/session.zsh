#!/usr/bin/env zsh
# aipane session-restore core.
#
# Two jobs:
#   1. gold-case launch: give aipane-controlled tools a session id AT LAUNCH
#      (pi/grok/claude via --session-id; cursor via pre-created chat id) so the
#      pane→session binding is deterministic — no hooks, no store races.
#   2. registry: append (pane %N → tool, session id) bindings to a JSONL log,
#      last-writer-wins, so ai-restore / aipane-snapshot can resume the right
#      session into the right pane after a tmux server restart.
#
# See docs/session-restore-design.md (§8 landing order, §12 verification).

if [[ "${_AIPANE_SESSION_VERSION:-}" == "1" ]]; then
  return 0
fi
typeset -g _AIPANE_SESSION_VERSION=1

typeset -g AIPANE_STATE_DIR="${AIPANE_STATE_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/aipane}"
typeset -g AIPANE_REGISTRY="${AIPANE_REGISTRY:-$AIPANE_STATE_DIR/registry.jsonl}"

# Tools whose session id aipane can fix at launch time (gold-case).
#   p/g/c : accept --session-id <uuid>
#   r     : no flag, but `cursor-agent create-chat` returns a usable id
typeset -gA _AIPANE_GOLDCASE=( p 1 g 1 c 1 r 1 )

# REPLY <- lowercase UUID (tools validate lowercase).
_aipane_uuid() {
  local u
  u="$(uuidgen 2>/dev/null)" || return 1
  [[ -n "$u" ]] || return 1
  REPLY="${u:l}"
}

# REPLY <- JSON-escaped scalar (quotes, backslash, and the control chars we may hit).
_aipane_json_escape() {
  local s="$1"
  s="${s//\\/\\\\}"
  s="${s//\"/\\\"}"
  s="${s//$'\t'/\\t}"
  s="${s//$'\n'/\\n}"
  s="${s//$'\r'/\\r}"
  REPLY="$s"
}

# Append one binding record: _aipane_registry_record <pane> <tool> <sid> <cmd>
# pane is the tmux unique pane id (%N / $TMUX_PANE); empty pane → no-op (not in tmux).
_aipane_registry_record() {
  local pane="$1" tool="$2" sid="$3" cmd="$4"
  [[ -n "$pane" ]] || return 0
  mkdir -p "$AIPANE_STATE_DIR" 2>/dev/null || return 1

  local ts jpane jtool jsid jcmd jcwd sock srvpid
  ts="$(date +%s)"
  sock="${TMUX%%,*}"
  srvpid="${${TMUX#*,}%%,*}"
  _aipane_json_escape "$pane"; jpane="$REPLY"
  _aipane_json_escape "$tool"; jtool="$REPLY"
  _aipane_json_escape "$sid";  jsid="$REPLY"
  _aipane_json_escape "$cmd";  jcmd="$REPLY"
  _aipane_json_escape "$PWD";  jcwd="$REPLY"

  local line
  line="{\"ts\":$ts,\"sock\":\"$sock\",\"srv\":\"$srvpid\",\"pane\":\"$jpane\",\"tool\":\"$jtool\",\"sid\":\"$jsid\",\"cwd\":\"$jcwd\",\"cmd\":\"$jcmd\"}"

  # Short single-line append is atomic on POSIX; use flock too when available.
  if command -v flock &>/dev/null; then
    { flock 9; print -r -- "$line" >&9; } 9>>"$AIPANE_REGISTRY"
  else
    print -r -- "$line" >>"$AIPANE_REGISTRY"
  fi
}

# cursor: pre-create an empty chat, REPLY <- its uuid. Network call; may fail offline.
_aipane_cursor_precreate() {
  local id
  id="$(command cursor-agent create-chat </dev/null 2>/dev/null)" || return 1
  id="${id//[[:space:]]/}"
  [[ "$id" =~ '^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$' ]] || return 1
  REPLY="$id"
}

# _aipane_prepare_launch <tool> [user_args...]
# Sets REPLY (launch command string) and REPLY_SID (aipane-assigned id, or "").
# Injects a session id ONLY on a bare launch (no user args); `ai p resume X`,
# `ai g -r X`, `ai x resume X` etc. are passed through untouched.
_aipane_prepare_launch() {
  local tool="$1"; shift
  local -a args=("$@")
  typeset -g REPLY_SID=""

  _aipane_tool_command "$tool" "$@" || return 1
  local base="$REPLY"

  # Any user-supplied arg → respect it verbatim (likely a resume/continue).
  if (( ${#args[@]} > 0 )); then
    REPLY="$base"
    return 0
  fi

  # Gold-case only; opt-out via AIPANE_NO_SESSION_ID=1.
  if [[ "${AIPANE_NO_SESSION_ID:-0}" == "1" || -z "${_AIPANE_GOLDCASE[$tool]:-}" ]]; then
    REPLY="$base"
    return 0
  fi

  local sid
  case "$tool" in
    p|g|c)
      if _aipane_uuid; then
        sid="$REPLY"
        REPLY="$base --session-id $sid"
        REPLY_SID="$sid"
      else
        REPLY="$base"
      fi
      ;;
    r)
      # cursor pre-create costs a ~3s network round-trip; opt out with
      # AIPANE_CURSOR_PRECREATE=0 to keep `ai r` instant (cursor panes then
      # restore fresh instead of resuming).
      if [[ "${AIPANE_CURSOR_PRECREATE:-1}" == "1" ]] && _aipane_cursor_precreate; then
        sid="$REPLY"
        REPLY="$AIPANE_CURSOR_LAUNCH_CMD --resume $sid"
        REPLY_SID="$sid"
      else
        REPLY="$base"
      fi
      ;;
    *)
      REPLY="$base"
      ;;
  esac
}
