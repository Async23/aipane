#!/usr/bin/env zsh
# aipane session-restore core.
#
# Two jobs:
#   1. gold-case launch: give aipane-controlled tools a session id AT LAUNCH
#      (Pi, Grok and Claude via --session-id) so the
#      initial pane→session binding is deterministic. Pi may replace its session
#      in-process; integrations/pi/aipane-bind.ts updates that binding afterward.
#   2. registry: append (pane %N → tool, session id) bindings to a JSONL log,
#      last-writer-wins, so ai-restore / aipane-snapshot can resume the right
#      session into the right pane after a tmux server restart.
#
# See docs/session-restore-design.md (§8 landing order, §12 verification).

if [[ "${_AIPANE_SESSION_VERSION:-}" == "2" ]]; then
  return 0
fi
typeset -g _AIPANE_SESSION_VERSION=2

typeset -g AIPANE_STATE_DIR="${AIPANE_STATE_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/aipane}"
typeset -g AIPANE_REGISTRY="${AIPANE_REGISTRY:-$AIPANE_STATE_DIR/registry.jsonl}"

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

  local ts jpane jtool jsid jcmd jcwd sock srvpid tmux_server
  ts="$(date +%s)"
  # The final two fields are numeric; a socket path itself may contain commas.
  tmux_server="${TMUX%,*}"
  sock="${tmux_server%,*}"
  srvpid="${tmux_server##*,}"
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

# _aipane_prepare_launch <tool> [user_args...]
# Sets REPLY (launch command string) and REPLY_SID (aipane-assigned id, or "").
# Injects a session id ONLY on a bare launch (no user args); `ai p resume X`,
# `ai g -r X`, `ai x resume X` etc. are passed through untouched.
_aipane_prepare_launch() {
  local tool="$1"; shift
  typeset -g REPLY_SID=""

  _aipane_tool_command "$tool" "$@" || return 1
  local base="$REPLY"

  # Only bare launches of these tools accept an aipane-assigned session ID.
  if (( $# == 0 )) && [[ "${AIPANE_NO_SESSION_ID:-0}" != 1 && "$tool" == [pgc] ]] && _aipane_uuid; then
    REPLY_SID="$REPLY"
    REPLY="$base --session-id $REPLY_SID"
  else
    REPLY="$base"
  fi
}
