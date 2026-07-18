#!/usr/bin/env zsh
# Commands: cc / ccd

_aipane_cc_invoke() {
  local mode="$1"
  shift

  local email
  local account_dir
  local use_account_dir=1
  local -a args cmd

  if [[ "$1" == "--no-account-dir" ]]; then
    use_account_dir=0
    shift
  fi

  if (( use_account_dir )); then
    if (( $# == 0 )) || [[ "$1" == -* ]]; then
      _aipane_select_account "1" "ccd_last_account" || return 1
      email="$REPLY"
    else
      email="$1"
      shift
    fi
  fi

  args=("$@")

  if (( use_account_dir )); then
    _aipane_account_dir "$email" || return 1
    account_dir="$REPLY"
  fi

  cmd=("$AIPANE_CLAUDE_CMD")
  if [[ "$mode" == "danger" ]]; then
    cmd+=(--dangerously-skip-permissions)
  fi

#  if [[ "$mode" == "danger" ]]; then
#    cmd+=(--model "claude-opus-4-6[1m]")
#  fi

  cmd+=("${args[@]}")

  if (( use_account_dir )); then
    CLAUDE_CONFIG_DIR="$account_dir" "${cmd[@]}"
  else
    "${cmd[@]}"
  fi
}

cc() {
  _aipane_cc_invoke "normal" "$@"
}

ccd() {
  _aipane_cc_invoke "danger" --no-account-dir "$@"
}
