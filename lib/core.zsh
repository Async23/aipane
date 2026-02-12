#!/usr/bin/env zsh
# Shared helpers for aipane.

if [[ -n "${_AIPANE_CORE_LOADED:-}" ]]; then
  return 0
fi
typeset -g _AIPANE_CORE_LOADED=1

# User-configurable values (export before sourcing init.zsh to override).
typeset -g AIPANE_CLAUDE_CMD="${AIPANE_CLAUDE_CMD:-claude}"
typeset -g AIPANE_ACCOUNTS_BASE="${AIPANE_ACCOUNTS_BASE:-$HOME/.claude-accounts}"
typeset -g AIPANE_SHARED_DIR="${AIPANE_SHARED_DIR:-$AIPANE_ACCOUNTS_BASE/_shared}"

typeset -g AIPANE_CODEX_LAUNCH_CMD="${AIPANE_CODEX_LAUNCH_CMD:-codex --yolo}"
typeset -g AIPANE_DROID_LAUNCH_CMD="${AIPANE_DROID_LAUNCH_CMD:-droid}"
typeset -g AIPANE_GEMINI_LAUNCH_CMD="${AIPANE_GEMINI_LAUNCH_CMD:-gemini --yolo}"

_aipane_link_if_missing() {
  local target="$1"
  local link_path="$2"

  if [[ -L "$link_path" ]]; then
    if [[ "$(readlink "$link_path")" != "$target" ]]; then
      ln -sfn "$target" "$link_path"
    fi
    return 0
  fi

  if [[ -e "$link_path" ]]; then
    return 0
  fi

  ln -s "$target" "$link_path"
}

_aipane_account_dir() {
  local email="$1"
  local account_dir

  if [[ -z "$email" ]]; then
    print -u2 "aipane: email is required"
    return 1
  fi

  account_dir="$AIPANE_ACCOUNTS_BASE/$email"

  mkdir -p "$account_dir" "$AIPANE_SHARED_DIR/projects" || return 1
  [[ -f "$AIPANE_SHARED_DIR/history.jsonl" ]] || : > "$AIPANE_SHARED_DIR/history.jsonl"

  if [[ -d "$HOME/.claude/rules" ]]; then
    _aipane_link_if_missing "$HOME/.claude/rules" "$account_dir/rules"
  fi
  if [[ -f "$HOME/.claude/settings.json" ]]; then
    _aipane_link_if_missing "$HOME/.claude/settings.json" "$account_dir/settings.json"
  fi
  if [[ -f "$HOME/.claude/settings.local.json" ]]; then
    _aipane_link_if_missing "$HOME/.claude/settings.local.json" "$account_dir/settings.local.json"
  fi

  _aipane_link_if_missing "$AIPANE_SHARED_DIR/projects" "$account_dir/projects"
  _aipane_link_if_missing "$AIPANE_SHARED_DIR/history.jsonl" "$account_dir/history.jsonl"

  REPLY="$account_dir"
}

_aipane_list_accounts() {
  local -a dirs accounts
  local name

  if [[ ! -d "$AIPANE_ACCOUNTS_BASE" ]]; then
    reply=()
    return 1
  fi

  dirs=("$AIPANE_ACCOUNTS_BASE"/*(/N:t))
  for name in "${dirs[@]}"; do
    [[ "$name" == "_shared" ]] && continue
    accounts+=("$name")
  done

  accounts=("${(@on)accounts}")
  reply=("${accounts[@]}")
  (( ${#reply[@]} > 0 ))
}

_aipane_select_account() {
  local label="$1"
  local -a accounts
  local i choice

  _aipane_list_accounts || {
    print -u2 "aipane: no Claude accounts found in $AIPANE_ACCOUNTS_BASE"
    return 1
  }

  accounts=("${reply[@]}")

  if (( ${#accounts[@]} == 1 )); then
    REPLY="${accounts[1]}"
    if [[ -n "$label" ]]; then
      print "Claude Code #${label} -> ${REPLY}"
    fi
    return 0
  fi

  if [[ -n "$label" ]]; then
    print "Claude Code #${label} - select account:"
  else
    print "Select Claude account:"
  fi

  i=1
  for choice in "${accounts[@]}"; do
    print "  [${i}] ${choice}"
    (( i++ ))
  done

  while true; do
    read -r "choice?Choose [1]: " < /dev/tty
    [[ -z "$choice" ]] && choice=1

    if [[ "$choice" == <-> ]] && (( choice >= 1 && choice <= ${#accounts[@]} )); then
      REPLY="${accounts[$choice]}"
      print
      return 0
    fi

    print "Invalid selection: ${choice}"
  done
}

_aipane_shell_join() {
  REPLY="${(j: :)${(@q)@}}"
}

_aipane_cc_command_string() {
  local email="$1"
  shift || true
  local -a cmd=(cc "$email" "$@")
  _aipane_shell_join "${cmd[@]}"
}

_aipane_ccd_command_string() {
  local email="$1"
  shift || true
  local -a cmd=(ccd "$email" "$@")
  _aipane_shell_join "${cmd[@]}"
}

_aipane_ceil_sqrt() {
  local n="$1"
  local i=1

  while (( i * i < n )); do
    (( i++ ))
  done

  REPLY="$i"
}

_aipane_require_iterm() {
  [[ "$TERM_PROGRAM" == "iTerm.app" ]]
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
    cols=3
    rows=1
    per_col=(1 1 1)
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

_aipane_project_dir_key() {
  local dir="$1"
  REPLY="${dir//\//-}"
}
