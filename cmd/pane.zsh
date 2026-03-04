#!/usr/bin/env zsh
# Command: ai

_aipane_pane_help() {
  cat <<'EOH'
Usage:
  ai [--new|-n] <tools_string>

Tools:
  c  Claude Code   (ccd <email>)
  x  Codex         (codex --yolo)
  d  Droid         (droid)
  g  Gemini        (gemini --yolo)
  o  Opencode      (opencode)

Examples:
  ai cxdg
  ai --new cxdg
  ai cc
  ai g
EOH
}

_aipane_pane_build_and_run_tmux() {
  local mode="$1"
  local pane_count="$2"
  local cols="$3"
  local rows="$4"
  local -a rows_per_col commands col_tops pane_ids
  local new_info window_id root_pane expected v
  local c needed_rows anchor new_pane
  local i
  shift 4

  rows_per_col=("${@:1:$cols}")
  shift "$cols"
  commands=("$@")

  if (( ${#commands[@]} != pane_count )); then
    print -u2 "ai: internal error: expected ${pane_count} commands, got ${#commands[@]}"
    return 1
  fi

  expected=0
  for v in "${rows_per_col[@]}"; do
    (( expected += v ))
  done
  if (( expected != pane_count )); then
    print -u2 "ai: internal error: layout mismatch (expected ${pane_count} panes, got ${expected})"
    return 1
  fi

  if [[ "$mode" == "here" ]]; then
    local existing_panes
    existing_panes="$(tmux display-message -p '#{window_panes}')" || return 1
    if (( existing_panes != 1 )); then
      print -u2 "ai: current window has ${existing_panes} panes; use --new to create a new window"
      return 1
    fi
    new_info="$(tmux display-message -p '#{window_id} #{pane_id}')" || return 1
  elif [[ "$mode" == "window" ]]; then
    new_info="$(tmux new-window -c "$PWD" -P -F '#{window_id} #{pane_id}')" || return 1
  elif [[ "$mode" == "session" ]]; then
    typeset -g _aipane_session="ai-$$"
    new_info="$(tmux new-session -d -s "$_aipane_session" -c "$PWD" -P -F '#{window_id} #{pane_id}')" || return 1
  else
    print -u2 "ai: internal error: unknown tmux mode '${mode}'"
    return 1
  fi

  window_id="${new_info%% *}"
  root_pane="${new_info#* }"

  # Build columns, then rows per column.
  col_tops=("$root_pane")
  for (( c = 2; c <= cols; c++ )); do
    new_pane="$(tmux split-window -h -d -P -F '#{pane_id}' -t "${col_tops[c-1]}")" || return 1
    col_tops+=("$new_pane")
    sleep 0.12
  done

  for (( c = 1; c <= cols; c++ )); do
    needed_rows="${rows_per_col[c]}"
    anchor="${col_tops[c]}"
    for (( r = 2; r <= needed_rows; r++ )); do
      new_pane="$(tmux split-window -v -d -P -F '#{pane_id}' -t "$anchor")" || return 1
      anchor="$new_pane"
      sleep 0.12
    done
  done

  # Reorder panes row-major (top->bottom, left->right) to match the original AppleScript logic.
  pane_ids=("${(@f)$(tmux list-panes -t "$window_id" -F '#{pane_id} #{pane_top} #{pane_left}' | sort -n -k2,2 -k3,3 | cut -d' ' -f1)}")
  if (( ${#pane_ids[@]} != pane_count )); then
    print -u2 "ai: internal error: expected ${pane_count} panes, found ${#pane_ids[@]}"
    return 1
  fi

  # Send commands to pane 2..N first, then pane 1 (so focus ends on the first pane).
  for (( i = 2; i <= pane_count; i++ )); do
    tmux send-keys -t "${pane_ids[i]}" "${commands[i]}" Enter
  done
  tmux send-keys -t "${pane_ids[1]}" "${commands[1]}" Enter
  tmux select-pane -t "${pane_ids[1]}"

  (( rows > 0 )) || true # keep rows in the signature for parity with the old backend
}

ai() {
  local _new_window=0
  local tools_str=""
  local -a tools commands rows_per_col grid_spec_parts
  local pane_count cols rows c_index i
  local ch

  while (( $# > 0 )); do
    case "$1" in
      --new|-n)
        _new_window=1
        ;;
      --help|-h)
        _aipane_pane_help
        return 0
        ;;
      -*)
        print -u2 "ai: unknown flag '$1'"
        _aipane_pane_help
        return 1
        ;;
      *)
        if [[ -n "$tools_str" ]]; then
          print -u2 "ai: expected one tools string"
          _aipane_pane_help
          return 1
        fi
        tools_str="$1"
        ;;
    esac
    shift
  done

  if [[ -z "$tools_str" ]]; then
    _aipane_pane_help
    return 0
  fi

  for (( i = 1; i <= ${#tools_str}; i++ )); do
    ch="${tools_str[i]}"
    case "$ch" in
      c|x|d|g|o) tools+=("$ch") ;;
      *)
        print -u2 "ai: unknown tool '${ch}' (valid: c/x/d/g/o)"
        return 1
        ;;
    esac
  done

  _aipane_ensure_tmux || return 1
  local _tmux_mode="$REPLY"
  if [[ "$_tmux_mode" == "window" && "$_new_window" -eq 0 ]]; then
    _tmux_mode="here"
  fi

  c_index=0
  for ch in "${tools[@]}"; do
    case "$ch" in
      c)
        (( c_index++ ))
        _aipane_select_account "$c_index" || return 1
        _aipane_ccd_command_string "$REPLY"
        commands+=("$REPLY")
        ;;
      x)
        commands+=("$AIPANE_CODEX_LAUNCH_CMD")
        ;;
      d)
        commands+=("$AIPANE_DROID_LAUNCH_CMD")
        ;;
      g)
        commands+=("$AIPANE_GEMINI_LAUNCH_CMD")
        ;;
      o)
        commands+=("$AIPANE_OPENCODE_LAUNCH_CMD")
        ;;
    esac
  done

  pane_count="${#tools[@]}"

  _aipane_grid_for_count "$pane_count" || return 1
  grid_spec_parts=("${(s: :)REPLY}")
  cols="${grid_spec_parts[1]}"
  rows="${grid_spec_parts[2]}"
  rows_per_col=("${grid_spec_parts[@]:2}")

  _aipane_pane_build_and_run_tmux \
    "$_tmux_mode" "$pane_count" "$cols" "$rows" \
    "${rows_per_col[@]}" \
    "${commands[@]}" || return 1

  if [[ "$_tmux_mode" == "session" ]]; then
    tmux attach -t "$_aipane_session"
  fi
}
