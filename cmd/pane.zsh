#!/usr/bin/env zsh
# Command: ai

_aipane_pane_help() {
  cat <<EOH
Usage:
  ai [--new|-n] [--layout|-l <layout>] <tools_string> [tool_args...]

Tools:
  c  Claude Code   (${AIPANE_CLAUDE_LAUNCH_CMD})
  x  Codex         (${AIPANE_CODEX_LAUNCH_CMD})
  d  dsh-TUI       (${AIPANE_DSH_LAUNCH_CMD})
  g  Grok          (${AIPANE_GROK_LAUNCH_CMD})
  p  Pi             (${AIPANE_PI_LAUNCH_CMD})
  k  Kimi Code     (${AIPANE_KIMI_LAUNCH_CMD})
  a  Antigravity   (${AIPANE_AGY_LAUNCH_CMD})

Notes:
  Single-tool calls run in the current pane unless --new or --layout is used.
  Tool arguments are supported only for single-tool calls.
  Use -- before tool arguments that look like ai options.

Examples:
  ai cxdg
  ai cxp
  ai cxp --layout main-right
  ai -l columns cxp
  ai x resume 019e680c-d2bd-71a2-9a9a-8cf78a2d8da1
  ai --new x resume 019e680c-d2bd-71a2-9a9a-8cf78a2d8da1
  ai x -- --help
  ai --new cxdg
  ai cc
  ai p
  ai g
  ai k
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

_aipane_agy_command() {
  local dir="${PWD:A}" home_dir="${HOME:A}" option value
  local -a inherited
  local -A explicit_dirs
  local i

  # A positional argument selects a subcommand. Skip values of session options
  # so that, for example, the prompt in `-p models` stays a session argument.
  for (( i = 1; i <= $#; i++ )); do
    option="${argv[i]%%=*}"
    case "$option" in
      -h|--help|--version|[^-]*)
        _aipane_command_with_args "$AIPANE_AGY_LAUNCH_CMD" "$@"
        return
        ;;
      -p|-i|--add-dir|--agent|--conversation|--effort|--input-format|--json-schema|\
      --log-file|--mode|--model|--output-format|--print|--print-timeout|--project|\
      --prompt|--prompt-interactive|--settings)
        if [[ "${argv[i]}" == *=* ]]; then
          value="${argv[i]#*=}"
        else
          (( ++i ))
          value="${argv[i]-}"
        fi
        if [[ "$option" == --add-dir && -n "$value" ]]; then
          explicit_dirs[${value:A}]=1
        fi
        ;;
    esac
  done

  while [[ "$dir" != "$home_dir" && "$dir" == "${home_dir%/}/"* ]]; do
    dir="${dir:h}"
    if [[ -d "$dir/.agents/skills" && -z "${explicit_dirs[$dir]-}" ]]; then
      inherited+=(--add-dir "$dir")
    fi
  done
  _aipane_command_with_args "$AIPANE_AGY_LAUNCH_CMD" "${inherited[@]}" "$@"
}

_aipane_tool_command() {
  local tool="$1"
  shift

  case "$tool" in
    c)
      _aipane_command_with_args "$AIPANE_CLAUDE_LAUNCH_CMD" "$@"
      ;;
    x)
      _aipane_command_with_args "$AIPANE_CODEX_LAUNCH_CMD" "$@"
      ;;
    d)
      _aipane_command_with_args "$AIPANE_DSH_LAUNCH_CMD" "$@"
      ;;
    g)
      _aipane_command_with_args "$AIPANE_GROK_LAUNCH_CMD" "$@"
      ;;
    p)
      _aipane_command_with_args "$AIPANE_PI_LAUNCH_CMD" "$@"
      ;;
    k)
      _aipane_command_with_args "$AIPANE_KIMI_LAUNCH_CMD" "$@"
      ;;
    a)
      _aipane_agy_command "$@"
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
  local pane_w="${3:-0}"
  local pane_h="${4:-0}"
  local cell_w="${5:-0}"
  local cell_h="${6:-0}"
  local -a parts per_col
  local cols rows sum value

  case "$layout" in
    ""|auto)
      _aipane_grid_for_pane "$pane_count" "$pane_w" "$pane_h" "$cell_w" "$cell_h" || return 1
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

_aipane_pane_cleanup_failed_build() {
  local mode="$1" window_id="$2" session_name="$3"
  shift 3
  local pane

  if [[ "$mode" == "window" && -n "$window_id" ]]; then
    tmux kill-window -t "$window_id" >/dev/null 2>&1 || true
  elif [[ "$mode" == "session" && -n "$session_name" ]]; then
    tmux kill-session -t "$session_name" >/dev/null 2>&1 || true
  else
    for pane in "$@"; do
      tmux kill-pane -t "$pane" >/dev/null 2>&1 || true
    done
  fi
}

_aipane_pane_build_and_run_tmux() {
  local mode="$1"
  local pane_count="$2"
  local cols="$3"
  local rows="$4"
  local -a rows_per_col commands col_tops pane_ids created_panes target_opt
  local new_info window_id root_pane expected v
  local c r needed_rows anchor new_pane
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
    [[ -n "${TMUX_PANE:-}" ]] && target_opt=(-t "$TMUX_PANE")
    new_info="$(tmux display-message "${target_opt[@]}" -p '#{window_id} #{pane_id}')" || return 1
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

  if (( pane_count == 1 )); then
    pane_ids=("$root_pane")
  else
    local root_dims pane_w pane_h
    root_dims="$(tmux display-message -t "$root_pane" -p '#{pane_width} #{pane_height}')" || {
      _aipane_pane_cleanup_failed_build "$mode" "$window_id" "${_aipane_session:-}"
      return 1
    }
    pane_w="${root_dims%% *}"
    pane_h="${root_dims#* }"

    if ! _aipane_grid_fits "$pane_w" "$pane_h" "$cols" "$rows"; then
      _aipane_pane_cleanup_failed_build "$mode" "$window_id" "${_aipane_session:-}"
      if [[ "$mode" == "here" ]]; then
        print -u2 "ai: current pane (${pane_w}x${pane_h}) is too small for ${cols}x${rows} layout (min ${AIPANE_MIN_PANE_WIDTH}x${AIPANE_MIN_PANE_HEIGHT} per pane); use --new to create a new window"
      else
        print -u2 "ai: window (${pane_w}x${pane_h}) is too small for ${cols}x${rows} layout (min ${AIPANE_MIN_PANE_WIDTH}x${AIPANE_MIN_PANE_HEIGHT} per pane)"
      fi
      return 1
    fi

    # Split columns and rows strictly inside root_pane using exact cell counts
    # so sibling panes in the same window keep their geometry untouched.
    col_tops=("$root_pane")
    local rem_w="$pane_w" rem_cols="$cols" col_w right_w
    for (( c = 2; c <= cols; c++ )); do
      col_w=$(( (rem_w - (rem_cols - 1)) / rem_cols ))
      right_w=$(( rem_w - col_w - 1 ))
      new_pane="$(tmux split-window -h -d -c "$PWD" -l "$right_w" -P -F '#{pane_id}' -t "${col_tops[c-1]}")" || {
        _aipane_pane_cleanup_failed_build "$mode" "$window_id" "${_aipane_session:-}" "${created_panes[@]}"
        return 1
      }
      col_tops+=("$new_pane")
      created_panes+=("$new_pane")
      rem_w="$right_w"
      (( rem_cols-- ))
      sleep 0.05
    done

    pane_ids=()
    local rem_h rem_rows row_h bottom_h
    for (( c = 1; c <= cols; c++ )); do
      needed_rows="${rows_per_col[c]}"
      anchor="${col_tops[c]}"
      pane_ids+=("$anchor")
      if (( needed_rows > 1 )); then
        rem_h="$pane_h"
        rem_rows="$needed_rows"
        for (( r = 2; r <= needed_rows; r++ )); do
          row_h=$(( (rem_h - (rem_rows - 1)) / rem_rows ))
          bottom_h=$(( rem_h - row_h - 1 ))
          new_pane="$(tmux split-window -v -d -c "$PWD" -l "$bottom_h" -P -F '#{pane_id}' -t "$anchor")" || {
            _aipane_pane_cleanup_failed_build "$mode" "$window_id" "${_aipane_session:-}" "${created_panes[@]}"
            return 1
          }
          pane_ids+=("$new_pane")
          created_panes+=("$new_pane")
          anchor="$new_pane"
          rem_h="$bottom_h"
          (( rem_rows-- ))
          sleep 0.05
        done
      fi
    done
  fi

  if (( ${#pane_ids[@]} != pane_count )); then
    _aipane_pane_cleanup_failed_build "$mode" "$window_id" "${_aipane_session:-}" "${created_panes[@]}"
    print -u2 "ai: internal error: expected ${pane_count} panes, found ${#pane_ids[@]}"
    return 1
  fi

  # Expose the resolved pane ids (column-major, aligned with commands[]) so the
  # caller can record pane→session bindings. See lib/session.zsh.
  typeset -ga _aipane_result_panes=("${pane_ids[@]}")

  # Send commands to pane 2..N first, then pane 1 (so focus ends on the first pane).
  for (( i = 2; i <= pane_count; i++ )); do
    tmux send-keys -t "${pane_ids[i]}" "${commands[i]}" Enter || return 1
  done
  tmux send-keys -t "${pane_ids[1]}" "${commands[1]}" Enter || return 1
  tmux select-pane -t "${pane_ids[1]}" || return 1

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
    return 1
  fi

  for (( i = 1; i <= ${#tools_str}; i++ )); do
    ch="${tools_str[i]}"
    case "$ch" in
      c|x|d|g|p|k|a) tools+=("$ch") ;;
      *)
        print -u2 "ai: unknown tool '${ch}' (valid: c/x/d/g/p/k/a)"
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
    _aipane_prepare_launch "${tools[1]}" "${tool_args[@]}" || return 1
    local _cmd="$REPLY" _sid="$REPLY_SID"
    _aipane_registry_record "${TMUX_PANE:-}" "${tools[1]}" "$_sid" "$_cmd"
    eval "$_cmd"
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

  _aipane_ensure_tmux || return 1
  local _tmux_mode="$REPLY"
  if [[ "$_tmux_mode" == "window" && "$_new_window" -eq 0 ]]; then
    _tmux_mode="here"
  fi

  local _target_w=0 _target_h=0 _cell_w=0 _cell_h=0 _was_zoomed=0 _zoom_pane=""
  if (( pane_count > 1 )); then
    if [[ "$_tmux_mode" == "here" ]]; then
      local -a _target_opt=()
      [[ -n "${TMUX_PANE:-}" ]] && _target_opt=(-t "$TMUX_PANE")
      local _here_info _zoom_flag
      _here_info="$(tmux display-message "${_target_opt[@]}" -p '#{pane_id} #{window_zoomed_flag} #{pane_width} #{pane_height} #{window_cell_width} #{window_cell_height}')" || return 1
      local -a _here_parts=("${(s: :)_here_info}")
      _zoom_pane="${_here_parts[1]}"
      _zoom_flag="${_here_parts[2]}"
      _target_w="${_here_parts[3]}"
      _target_h="${_here_parts[4]}"
      _cell_w="${_here_parts[5]:-0}"
      _cell_h="${_here_parts[6]:-0}"
      if [[ "$_zoom_flag" == "1" ]]; then
        tmux resize-pane -Z -t "$_zoom_pane" || return 1
        _was_zoomed=1
        _here_info="$(tmux display-message -t "$_zoom_pane" -p '#{pane_width} #{pane_height}')" || {
          tmux resize-pane -Z -t "$_zoom_pane" >/dev/null 2>&1 || true
          return 1
        }
        _target_w="${_here_info%% *}"
        _target_h="${_here_info#* }"
      fi
    elif [[ "$_tmux_mode" == "window" ]]; then
      local _win_info _border_pos
      if _win_info="$(tmux display-message -p '#{window_width} #{window_height} #{window_cell_width} #{window_cell_height} #{pane-border-status}' 2>/dev/null)"; then
        local -a _win_parts=("${(s: :)_win_info}")
        _target_w="${_win_parts[1]:-0}"
        _target_h="${_win_parts[2]:-0}"
        _cell_w="${_win_parts[3]:-0}"
        _cell_h="${_win_parts[4]:-0}"
        _border_pos="${_win_parts[5]:-off}"
        if [[ "$_border_pos" == "top" || "$_border_pos" == "bottom" ]] && (( _target_h > 1 )); then
          (( _target_h-- ))
        fi
      fi
    fi
  fi

  if ! _aipane_layout_spec_for_count "$pane_count" "${_layout_spec:-auto}" "$_target_w" "$_target_h" "$_cell_w" "$_cell_h"; then
    (( _was_zoomed )) && tmux resize-pane -Z -t "$_zoom_pane" >/dev/null 2>&1 || true
    return 1
  fi
  grid_spec_parts=("${(s: :)REPLY}")
  cols="${grid_spec_parts[1]}"
  rows="${grid_spec_parts[2]}"
  rows_per_col=("${grid_spec_parts[@]:2}")

  if [[ "$_tmux_mode" == "here" ]] && (( cols > 1 || rows > 1 )); then
    if ! _aipane_grid_fits "$_target_w" "$_target_h" "$cols" "$rows"; then
      (( _was_zoomed )) && tmux resize-pane -Z -t "$_zoom_pane" >/dev/null 2>&1 || true
      print -u2 "ai: current pane (${_target_w}x${_target_h}) is too small for ${cols}x${rows} layout (min ${AIPANE_MIN_PANE_WIDTH}x${AIPANE_MIN_PANE_HEIGHT} per pane); use --new to create a new window"
      return 1
    fi
  fi

  local -a sids
  for ch in "${tools[@]}"; do
    if ! _aipane_prepare_launch "$ch" "${tool_args[@]}"; then
      (( _was_zoomed )) && tmux resize-pane -Z -t "$_zoom_pane" >/dev/null 2>&1 || true
      return 1
    fi
    commands+=("$REPLY")
    sids+=("$REPLY_SID")
  done

  typeset -ga _aipane_result_panes=()
  if ! _aipane_pane_build_and_run_tmux \
    "$_tmux_mode" "$pane_count" "$cols" "$rows" \
    "${rows_per_col[@]}" \
    "${commands[@]}"; then
    (( _was_zoomed )) && tmux resize-pane -Z -t "$_zoom_pane" >/dev/null 2>&1 || true
    return 1
  fi

  # Record pane→session bindings (pane_ids are column-major, aligned with commands/tools/sids).
  if (( ${#_aipane_result_panes[@]} == pane_count )); then
    for (( i = 1; i <= pane_count; i++ )); do
      _aipane_registry_record "${_aipane_result_panes[i]}" "${tools[i]}" "${sids[i]}" "${commands[i]}"
    done
  fi

  if [[ "$_tmux_mode" == "session" ]]; then
    tmux attach -t "$_aipane_session"
  fi
}
