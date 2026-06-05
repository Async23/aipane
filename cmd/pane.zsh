#!/usr/bin/env zsh
# Command: ai

_aipane_pane_help() {
  cat <<'EOH'
Usage:
  ai [--new|-n] [--layout|-l <layout>] <tools_string> [tool_args...]

Tools:
  c  Claude Code   (ccd)
  x  Codex         (codex --yolo)
  d  Droid         (droid)
  g  Gemini        (gemini --yolo)
  o  Opencode      (opencode)
  r  Cursor        (cursor-agent --force)
  q  Qoder         (qodercli)
  p  Oh My Pi      (omp --approval-mode=yolo)

Layouts:
  auto             Default automatic layout; skips the 3-pane prompt
  main-left        3 panes: first tool on the left, remaining tools stacked right
  main-right       3 panes: first two tools stacked left, third tool on the right
  columns          One column per tool
  rows             One row per tool
  1,2              Custom columns; each number is the pane count in that column

Notes:
  Single-tool calls run in the current pane unless --new or --layout is used.
  Tool arguments are supported only for single-tool calls.
  Use -- before tool arguments that look like ai options.

Examples:
  ai cxdg
  ai cxr
  ai cxr --layout main-right
  ai -l columns cxr
  ai x resume 019e680c-d2bd-71a2-9a9a-8cf78a2d8da1
  ai --new x resume 019e680c-d2bd-71a2-9a9a-8cf78a2d8da1
  ai x -- --help
  ai q
  ai xq
  ai --new cxdg
  ai cc
  ai p
  ai g
EOH
}

_aipane_command_with_args() {
  local base="$1"
  shift
  local -a args

  args=("$@")
  REPLY="$base"
  if (( ${#args[@]} > 0 )); then
    REPLY+=" ${(j: :)${(@q)args}}"
  fi
}

_aipane_tool_command() {
  local tool="$1"
  shift

  case "$tool" in
    c)
      _aipane_command_with_args "ccd" "$@"
      ;;
    x)
      _aipane_command_with_args "$AIPANE_CODEX_LAUNCH_CMD" "$@"
      ;;
    d)
      _aipane_command_with_args "$AIPANE_DROID_LAUNCH_CMD" "$@"
      ;;
    g)
      _aipane_command_with_args "$AIPANE_GEMINI_LAUNCH_CMD" "$@"
      ;;
    o)
      _aipane_command_with_args "$AIPANE_OPENCODE_LAUNCH_CMD" "$@"
      ;;
    r)
      _aipane_command_with_args "$AIPANE_CURSOR_LAUNCH_CMD" "$@"
      ;;
    q)
      _aipane_command_with_args "$AIPANE_QODER_LAUNCH_CMD" "$@"
      ;;
    p)
      _aipane_command_with_args "$AIPANE_OMP_LAUNCH_CMD" "$@"
      ;;
    *)
      print -u2 "ai: internal error: unknown tool '${tool}'"
      return 1
      ;;
  esac
}

_aipane_layout_spec_for_count() {
  local pane_count="$1"
  local layout="$2"
  local -a parts per_col
  local cols rows sum value

  case "$layout" in
    ""|auto)
      _aipane_grid_for_count "$pane_count" || return 1
      return 0
      ;;
    main-left|left)
      if (( pane_count != 3 )); then
        print -u2 "ai: layout '${layout}' requires exactly 3 panes"
        return 1
      fi
      REPLY="2 2 1 2"
      return 0
      ;;
    main-right|right)
      if (( pane_count != 3 )); then
        print -u2 "ai: layout '${layout}' requires exactly 3 panes"
        return 1
      fi
      REPLY="2 2 2 1"
      return 0
      ;;
    columns|cols|col)
      cols="$pane_count"
      rows=1
      per_col=()
      for (( value = 1; value <= pane_count; value++ )); do
        per_col+=(1)
      done
      REPLY="$cols $rows ${(j: :)per_col}"
      return 0
      ;;
    rows|row)
      REPLY="1 $pane_count $pane_count"
      return 0
      ;;
  esac

  if [[ "$layout" == *","* ]]; then
    parts=("${(@s:,:)layout}")
    cols="${#parts[@]}"
    rows=0
    sum=0

    for value in "${parts[@]}"; do
      if [[ "$value" != <-> ]] || (( value <= 0 )); then
        print -u2 "ai: invalid layout '${layout}' (use positive column counts, e.g. 1,2)"
        return 1
      fi
      (( sum += value ))
      (( value > rows )) && rows="$value"
    done

    if (( sum != pane_count )); then
      print -u2 "ai: layout '${layout}' has ${sum} panes, expected ${pane_count}"
      return 1
    fi

    REPLY="$cols $rows ${(j: :)parts}"
    return 0
  fi

  print -u2 "ai: unknown layout '${layout}' (valid: auto/main-left/main-right/columns/rows or custom like 1,2)"
  return 1
}

_aipane_select_three_pane_layout() {
  local -a tools
  local choice
  local first second third

  tools=("$@")
  first="${tools[1]}"
  second="${tools[2]}"
  third="${tools[3]}"

  print "Select layout for 3 panes:"
  print
  print "  [1] main-left"
  print "      +-------+-------+"
  print "      |       |   ${second}   |"
  print "      |   ${first}   +-------+"
  print "      |       |   ${third}   |"
  print "      +-------+-------+"
  print
  print "  [2] main-right"
  print "      +-------+-------+"
  print "      |   ${first}   |       |"
  print "      +-------+   ${third}   |"
  print "      |   ${second}   |       |"
  print "      +-------+-------+"
  print
  print "  [3] columns"
  print "      +-------+-------+-------+"
  print "      |   ${first}   |   ${second}   |   ${third}   |"
  print "      +-------+-------+-------+"
  print
  print "  [4] rows"
  print "      +-------+"
  print "      |   ${first}   |"
  print "      +-------+"
  print "      |   ${second}   |"
  print "      +-------+"
  print "      |   ${third}   |"
  print "      +-------+"
  print

  while true; do
    read -r "choice?Choose [1]: " < /dev/tty || return 1
    choice="${choice:-1}"
    case "$choice" in
      1) REPLY="main-left"; print; return 0 ;;
      2) REPLY="main-right"; print; return 0 ;;
      3) REPLY="columns"; print; return 0 ;;
      4) REPLY="rows"; print; return 0 ;;
      *) print "Invalid selection: ${choice}" ;;
    esac
  done
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

  if (( cols > 1 )); then
    tmux select-layout -t "$window_id" even-horizontal || return 1
    sleep 0.12
  fi

  for (( c = 1; c <= cols; c++ )); do
    needed_rows="${rows_per_col[c]}"
    anchor="${col_tops[c]}"
    for (( r = 2; r <= needed_rows; r++ )); do
      new_pane="$(tmux split-window -v -d -P -F '#{pane_id}' -t "$anchor")" || return 1
      anchor="$new_pane"
      sleep 0.12
    done
  done

  # Reorder panes column-major (left->right, then top->bottom) so the tools string maps
  # directly into columns: tool[i] fills the i-th pane in column-major order.
  pane_ids=("${(@f)$(tmux list-panes -t "$window_id" -F '#{pane_id} #{pane_top} #{pane_left}' | sort -n -k3,3 -k2,2 | cut -d' ' -f1)}")
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
  local _layout_spec=""
  local tools_str=""
  local -a tools commands rows_per_col grid_spec_parts tool_args
  local pane_count cols rows i
  local ch

  while (( $# > 0 )); do
    case "$1" in
      --)
        shift
        if [[ -z "$tools_str" ]]; then
          print -u2 "ai: -- must follow the tools string"
          _aipane_pane_help
          return 1
        fi
        tool_args+=("$@")
        break
        ;;
      --new|-n)
        _new_window=1
        ;;
      --help|-h)
        _aipane_pane_help
        return 0
        ;;
      --layout=*)
        if [[ -n "$_layout_spec" ]]; then
          print -u2 "ai: expected one layout"
          return 1
        fi
        _layout_spec="${1#*=}"
        ;;
      --layout|-l)
        if [[ -n "$_layout_spec" ]]; then
          print -u2 "ai: expected one layout"
          return 1
        fi
        shift
        if (( $# == 0 )); then
          print -u2 "ai: --layout requires a value"
          _aipane_pane_help
          return 1
        fi
        _layout_spec="$1"
        ;;
      -*)
        if [[ -n "$tools_str" ]]; then
          tool_args+=("$1")
        else
          print -u2 "ai: unknown flag '$1'"
          _aipane_pane_help
          return 1
        fi
        ;;
      *)
        if [[ -n "$tools_str" ]]; then
          tool_args+=("$1")
        else
          tools_str="$1"
        fi
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
      c|x|d|g|o|r|q|p) tools+=("$ch") ;;
      *)
        print -u2 "ai: unknown tool '${ch}' (valid: c/x/d/g/o/r/q/p)"
        return 1
        ;;
    esac
  done

  pane_count="${#tools[@]}"

  if (( ${#tool_args[@]} > 0 && pane_count != 1 )); then
    print -u2 "ai: tool arguments are only supported with a single tool"
    print -u2 "ai: use one tool like 'ai x resume ...' or omit extra arguments for multi-pane launches"
    return 1
  fi

  if (( pane_count == 1 && _new_window == 0 )) && [[ -z "$_layout_spec" ]]; then
    _aipane_tool_command "${tools[1]}" "${tool_args[@]}" || return 1
    eval "$REPLY"
    return $?
  fi

  if [[ -z "$_layout_spec" && "$pane_count" -eq 3 ]]; then
    if [[ -t 0 && -t 1 ]]; then
      _aipane_select_three_pane_layout "${tools[@]}" || return 1
      _layout_spec="$REPLY"
    else
      _layout_spec="auto"
    fi
  fi

  _aipane_layout_spec_for_count "$pane_count" "${_layout_spec:-auto}" || return 1
  grid_spec_parts=("${(s: :)REPLY}")
  cols="${grid_spec_parts[1]}"
  rows="${grid_spec_parts[2]}"
  rows_per_col=("${grid_spec_parts[@]:2}")

  _aipane_ensure_tmux || return 1
  local _tmux_mode="$REPLY"
  if [[ "$_tmux_mode" == "window" && "$_new_window" -eq 0 ]]; then
    _tmux_mode="here"
  fi

  for ch in "${tools[@]}"; do
    _aipane_tool_command "$ch" "${tool_args[@]}" || return 1
    commands+=("$REPLY")
  done

  _aipane_pane_build_and_run_tmux \
    "$_tmux_mode" "$pane_count" "$cols" "$rows" \
    "${rows_per_col[@]}" \
    "${commands[@]}" || return 1

  if [[ "$_tmux_mode" == "session" ]]; then
    tmux attach -t "$_aipane_session"
  fi
}
