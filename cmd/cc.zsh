#!/usr/bin/env zsh
# Commands: cc / ccd

_aipane_cc_invoke() {
  local mode="$1"
  shift

  local email
  local account_dir
  local -a args cmd

  if (( $# == 0 )) || [[ "$1" == -* ]]; then
    _aipane_select_account "1" || return 1
    email="$REPLY"
  else
    email="$1"
    shift
  fi

  args=("$@")

  _aipane_account_dir "$email" || return 1
  account_dir="$REPLY"

  cmd=("$AIPANE_CLAUDE_CMD")
  if [[ "$mode" == "danger" ]]; then
    cmd+=(--dangerously-skip-permissions)
  fi
  cmd+=("${args[@]}")

  CLAUDE_CONFIG_DIR="$account_dir" "${cmd[@]}"
}

cc() {
  _aipane_cc_invoke "normal" "$@"
}

ccd() {
  _aipane_cc_invoke "danger" "$@"
}
