#!/usr/bin/env zsh
# Command: cc-switch

_aipane_switch_help() {
  cat <<'EOH'
Usage:
  cc-switch [email] [session-id]

Behavior:
  - If email is missing, prompt for account selection.
  - If session-id is missing, use the newest local Claude session in current project.
EOH
}

_aipane_switch_find_latest_session() {
  local search_dir="$1"
  local -a session_files

  session_files=("$search_dir"/**/*.jsonl(N.om))

  if (( ${#session_files[@]} == 0 )); then
    return 1
  fi

  REPLY="${session_files[1]:t:r}"
}

_aipane_switch_detect_session() {
  local project_key project_dir

  _aipane_project_dir_key "$PWD"
  project_key="$REPLY"
  project_dir="$AIPANE_SHARED_DIR/projects/$project_key"

  if [[ -d "$project_dir" ]]; then
    _aipane_switch_find_latest_session "$project_dir" && return 0
  fi

  _aipane_switch_find_latest_session "$AIPANE_SHARED_DIR/projects"
}

cc-switch() {
  local email=""
  local session_id=""

  if (( $# > 0 )) && [[ "$1" == "--help" || "$1" == "-h" ]]; then
    _aipane_switch_help
    return 0
  fi

  if (( $# > 0 )); then
    if [[ "$1" == *"@"* ]]; then
      email="$1"
      shift
    elif (( $# > 1 )); then
      email="$1"
      shift
    else
      session_id="$1"
      shift
    fi
  fi

  if (( $# > 0 )); then
    session_id="$1"
    shift
  fi

  if (( $# > 0 )); then
    print -u2 "cc-switch: too many arguments"
    _aipane_switch_help
    return 1
  fi

  if [[ -z "$email" ]]; then
    _aipane_select_account "1" || return 1
    email="$REPLY"
  fi

  if [[ -z "$session_id" ]]; then
    _aipane_switch_detect_session || {
      print -u2 "cc-switch: unable to find a recent session under $AIPANE_SHARED_DIR/projects"
      return 1
    }
    session_id="$REPLY"
  fi

  print "Switching to account: $email"
  print "Resuming session: $session_id"

  ccd "$email" --resume "$session_id"
}
