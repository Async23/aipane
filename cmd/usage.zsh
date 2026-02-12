#!/usr/bin/env zsh
# Command: cc-usage

_aipane_usage_help() {
  cat <<'EOH'
Usage:
  cc-usage [command] [--timeout N] [--yes|-y]

Options:
  --timeout N   Wait seconds for Claude Code to start (default: 25)
  --yes, -y     Skip interactive selection, use all accounts + auto layout

Examples:
  cc-usage
  cc-usage /usage --timeout 30
  cc-usage "/cost this month" -y
EOH
}

_aipane_usage_select_accounts() {
  local -a accounts selected chosen tokens
  local input token i mark

  accounts=("${reply[@]}")
  if (( ${#accounts[@]} == 0 )); then
    reply=()
    return 1
  fi

  for (( i = 1; i <= ${#accounts[@]}; i++ )); do
    selected[i]=1
  done

  while true; do
    print "Accounts:"
    for (( i = 1; i <= ${#accounts[@]}; i++ )); do
      if (( selected[i] )); then
        mark="x"
      else
        mark=" "
      fi
      print "  [${mark}] ${i}) ${accounts[i]}"
    done
    print

    read -r "input?Toggle by number (e.g. \"2 3\"), Enter to continue: " < /dev/tty
    print

    if [[ -z "$input" ]]; then
      chosen=()
      for (( i = 1; i <= ${#accounts[@]}; i++ )); do
        if (( selected[i] )); then
          chosen+=("${accounts[i]}")
        fi
      done

      if (( ${#chosen[@]} == 0 )); then
        print -u2 "cc-usage: select at least one account"
        print
        continue
      fi

      reply=("${chosen[@]}")
      return 0
    fi

    input="${input//,/ }"
    tokens=(${=input})
    for token in "${tokens[@]}"; do
      if [[ "$token" == <-> ]] && (( token >= 1 && token <= ${#accounts[@]} )); then
        if (( selected[token] )); then
          selected[token]=0
        else
          selected[token]=1
        fi
      else
        print -u2 "Invalid selection: ${token}"
      fi
    done
    print
  done
}

_aipane_usage_layout_presets() {
  local pane_count="$1"

  case "$pane_count" in
    2)
      reply=(
        "2 columns|2|1 1"
        "2 rows|1|2"
      )
      ;;
    3)
      reply=(
        "1+2 columns|2|1 2"
        "2+1 columns|2|2 1"
        "3 rows|1|3"
        "3 columns|3|1 1 1"
      )
      ;;
    4)
      reply=(
        "2x2 grid|2|2 2"
        "4 columns|4|1 1 1 1"
        "4 rows|1|4"
      )
      ;;
    5)
      reply=(
        "3-col auto|3|2 2 1"
        "5 columns|5|1 1 1 1 1"
        "5 rows|1|5"
      )
      ;;
    6)
      reply=(
        "3x2 grid|3|2 2 2"
        "2x3 grid|2|3 3"
        "6 columns|6|1 1 1 1 1 1"
      )
      ;;
    *)
      reply=()
      return 1
      ;;
  esac

  return 0
}

_aipane_usage_parse_preset() {
  local preset="$1"
  local label rest cols rpc_str v rows=0
  local -a rpc_parts

  if [[ "$preset" != *"|"* ]]; then
    return 1
  fi

  label="${preset%%|*}"
  rest="${preset#*|}"
  cols="${rest%%|*}"
  rpc_str="${rest#*|}"

  if [[ -z "$label" || "$cols" != <-> || -z "$rpc_str" ]]; then
    return 1
  fi

  rpc_parts=(${=rpc_str})
  if (( ${#rpc_parts[@]} != cols )); then
    return 1
  fi

  for v in "${rpc_parts[@]}"; do
    if [[ "$v" != <-> ]] || (( v <= 0 )); then
      return 1
    fi
    (( v > rows )) && rows="$v"
  done

  REPLY="$cols $rows ${(j: :)rpc_parts}"
}

_aipane_usage_render_grid() {
  local cols="$1"
  shift

  local -a rows_per_col accounts lines
  local -A pane_owner pane_top pane_bottom pane_center pane_name
  local max_rows=0 pane_count idx c r i top bottom
  local inner_width=7 display_rows dr
  local raw_name short_name
  local blank hline line segment border_char
  local pane_id text
  local text_len pad_left pad_right
  local left_h right_h

  rows_per_col=("${@:1:$cols}")
  shift "$cols"
  accounts=("$@")
  pane_count="${#accounts[@]}"

  if (( cols <= 0 || ${#rows_per_col[@]} != cols || pane_count <= 0 )); then
    reply=()
    return 1
  fi

  for r in "${rows_per_col[@]}"; do
    if [[ "$r" != <-> ]] || (( r <= 0 )); then
      reply=()
      return 1
    fi
    (( r > max_rows )) && max_rows="$r"
  done

  for raw_name in "${accounts[@]}"; do
    short_name="${raw_name%%@*}"
    [[ -z "$short_name" ]] && short_name="$raw_name"
    (( ${#short_name} > inner_width )) && inner_width="${#short_name}"
  done

  (( inner_width < 7 )) && inner_width=7
  (( inner_width > 18 )) && inner_width=18

  idx=1
  for (( r = 1; r <= max_rows; r++ )); do
    for (( c = 1; c <= cols; c++ )); do
      if (( r <= rows_per_col[c] )); then
        pane_owner["$r,$c"]="$idx"
        pane_top["$idx"]="$r"
        if (( r == rows_per_col[c] )); then
          pane_bottom["$idx"]="$max_rows"
        else
          pane_bottom["$idx"]="$r"
        fi
        (( idx++ ))
      else
        pane_owner["$r,$c"]="${pane_owner["$((r - 1)),$c"]}"
      fi
    done
  done

  if (( idx - 1 != pane_count )); then
    reply=()
    return 1
  fi

  for (( i = 1; i <= pane_count; i++ )); do
    raw_name="${accounts[i]}"
    short_name="${raw_name%%@*}"
    [[ -z "$short_name" ]] && short_name="$raw_name"
    if (( ${#short_name} > inner_width )); then
      short_name="${short_name[1,$((inner_width - 1))]}~"
    fi
    pane_name["$i"]="$short_name"

    top="${pane_top["$i"]}"
    bottom="${pane_bottom["$i"]}"
    pane_center["$i"]="$(( ((2 * top - 1) + (2 * bottom - 1)) / 2 ))"
  done

  printf -v blank "%${inner_width}s" ""
  hline="${blank// /─}"

  line="┌"
  for (( c = 1; c <= cols; c++ )); do
    line+="$hline"
    if (( c < cols )); then
      line+="┬"
    else
      line+="┐"
    fi
  done
  lines+=("$line")

  display_rows=$((2 * max_rows - 1))
  for (( dr = 1; dr <= display_rows; dr++ )); do
    if (( dr % 2 )); then
      r=$(( (dr + 1) / 2 ))
      line="│"
    else
      r=$(( dr / 2 ))
      if (( r < rows_per_col[1] )); then
        line="├"
      else
        line="│"
      fi
    fi

    for (( c = 1; c <= cols; c++ )); do
      segment="$blank"

      if (( !(dr % 2) && r < rows_per_col[c] )); then
        segment="$hline"
      else
        pane_id="${pane_owner["$r,$c"]}"
        if [[ -n "$pane_id" && "${pane_center["$pane_id"]}" == "$dr" ]]; then
          text="${pane_name["$pane_id"]}"
          text_len="${#text}"
          pad_left=$(( (inner_width - text_len) / 2 ))
          pad_right=$(( inner_width - text_len - pad_left ))
          printf -v segment "%${pad_left}s%s%${pad_right}s" "" "$text" ""
        fi
      fi

      line+="$segment"

      if (( dr % 2 )); then
        border_char="│"
      elif (( c == cols )); then
        if (( r < rows_per_col[c] )); then
          border_char="┤"
        else
          border_char="│"
        fi
      else
        left_h=0
        right_h=0
        (( r < rows_per_col[c] )) && left_h=1
        (( r < rows_per_col[c + 1] )) && right_h=1

        if (( left_h && right_h )); then
          border_char="┼"
        elif (( left_h )); then
          border_char="┤"
        elif (( right_h )); then
          border_char="├"
        else
          border_char="│"
        fi
      fi

      line+="$border_char"
    done

    lines+=("$line")
  done

  line="└"
  for (( c = 1; c <= cols; c++ )); do
    line+="$hline"
    if (( c < cols )); then
      line+="┴"
    else
      line+="┘"
    fi
  done
  lines+=("$line")

  reply=("${lines[@]}")
}

_aipane_usage_select_layout() {
  local pane_count="$1"
  shift

  local -a accounts presets preview_lines spec_parts rows_per_col
  local -A block_line
  local -a block_width block_height
  local i j max_height=0 width
  local preset title choice line segment cols

  accounts=("$@")
  _aipane_usage_layout_presets "$pane_count" || return 1
  presets=("${reply[@]}")

  print
  print "Select layout (${pane_count} panes):"
  print

  for (( i = 1; i <= ${#presets[@]}; i++ )); do
    preset="${presets[i]}"
    title="[${i}] ${preset%%|*}"

    _aipane_usage_parse_preset "$preset" || return 1
    spec_parts=("${(s: :)REPLY}")
    cols="${spec_parts[1]}"
    rows_per_col=("${spec_parts[@]:2}")

    _aipane_usage_render_grid "$cols" "${rows_per_col[@]}" "${accounts[@]}" || return 1
    preview_lines=("${reply[@]}")

    block_line["$i,1"]="$title"
    width="${#title}"
    for (( j = 1; j <= ${#preview_lines[@]}; j++ )); do
      block_line["$i,$((j + 1))"]="${preview_lines[j]}"
      (( ${#preview_lines[j]} > width )) && width="${#preview_lines[j]}"
    done

    block_width[i]="$width"
    block_height[i]=$(( ${#preview_lines[@]} + 1 ))
    (( block_height[i] > max_height )) && max_height="${block_height[i]}"
  done

  for (( j = 1; j <= max_height; j++ )); do
    line=""
    for (( i = 1; i <= ${#presets[@]}; i++ )); do
      segment="${block_line["$i,$j"]}"
      [[ -z "$segment" ]] && segment=""
      printf -v segment "%-${block_width[i]}s" "$segment"
      line+="$segment"
      if (( i < ${#presets[@]} )); then
        line+="    "
      fi
    done
    print "$line"
  done

  print
  while true; do
    read -r "choice?Choose [1]: " < /dev/tty
    [[ -z "$choice" ]] && choice=1

    if [[ "$choice" == <-> ]] && (( choice >= 1 && choice <= ${#presets[@]} )); then
      _aipane_usage_parse_preset "${presets[choice]}" || return 1
      print
      return 0
    fi

    print "Invalid selection: ${choice}"
  done
}

_aipane_usage_build_and_run_applescript() {
  local pane_count="$1"
  local cols="$2"
  local rows="$3"
  local timeout_seconds="$4"
  local followup_cmd="$5"
  local -a rows_per_col start_cmds osa_args
  shift 5

  rows_per_col=("${@:1:$cols}")
  shift "$cols"
  start_cmds=("$@")

  osa_args=("$pane_count" "$cols" "$rows" "$timeout_seconds" "$followup_cmd")
  osa_args+=("${rows_per_col[@]}")
  osa_args+=("${start_cmds[@]}")

  osascript - "${osa_args[@]}" <<'APPLESCRIPT' >/dev/null 2>&1 &
on run argv
  set paneCount to (item 1 of argv) as integer
  set colCount to (item 2 of argv) as integer
  set rowCount to (item 3 of argv) as integer
  set timeoutSeconds to (item 4 of argv) as integer
  set followupCmd to item 5 of argv

  set argIndex to 6
  set rowSpec to {}
  repeat with c from 1 to colCount
    copy ((item argIndex of argv) as integer) to end of rowSpec
    set argIndex to argIndex + 1
  end repeat

  set startCmds to {}
  repeat with i from 1 to paneCount
    copy (item argIndex of argv) to end of startCmds
    set argIndex to argIndex + 1
  end repeat

  tell application "iTerm2"
    activate
    if not (exists current window) then
      create window with default profile
    end if

    tell current window
      tell current tab
        set colTops to {session 1}
        if colCount > 1 then
          repeat with c from 2 to colCount
            set refSession to item (c - 1) of colTops
            tell refSession
              set newTop to (split vertically with default profile)
            end tell
            copy newTop to end of colTops
          end repeat
        end if

        set columnsSessions to {}
        repeat with c from 1 to colCount
          set topSession to item c of colTops
          set neededRows to item c of rowSpec
          set oneColumn to {topSession}

          if neededRows > 1 then
            set anchorSession to topSession
            repeat with r from 2 to neededRows
              tell anchorSession
                set newRow to (split horizontally with default profile)
              end tell
              copy newRow to end of oneColumn
              set anchorSession to newRow
            end repeat
          end if

          copy oneColumn to end of columnsSessions
        end repeat

        set orderedSessions to {}
        repeat with r from 1 to rowCount
          repeat with c from 1 to colCount
            set oneColumn to item c of columnsSessions
            if r <= (count of oneColumn) then
              copy (item r of oneColumn) to end of orderedSessions
            end if
          end repeat
        end repeat

        repeat with i from 1 to paneCount
          tell (item i of orderedSessions)
            write text (item i of startCmds)
          end tell
        end repeat

        set paneReady to {}
        repeat with i from 1 to paneCount
          set end of paneReady to false
        end repeat

        set readyCount to 0
        set elapsed to 0
        repeat while readyCount < paneCount and elapsed < timeoutSeconds
          repeat with i from 1 to paneCount
            if item i of paneReady is false then
              tell (item i of orderedSessions)
                set paneText to contents
              end tell
              if paneText contains "claude code" or paneText contains "Claude Code" then
                set item i of paneReady to true
                set readyCount to readyCount + 1
                tell (item i of orderedSessions)
                  write text followupCmd newline NO
                  delay 0.3
                  write text return newline NO
                end tell
              end if
            end if
          end repeat
          if readyCount < paneCount then
            delay 0.5
            set elapsed to elapsed + 0.5
          end if
        end repeat
      end tell
    end tell
  end tell
end run
APPLESCRIPT
  disown
}

cc-usage() {
  local timeout_seconds=25
  local skip_interactive=0
  local -a command_parts accounts rows_per_col start_cmds grid_spec_parts
  local arg followup_cmd pane_count cols rows

  if (( $# > 0 )) && [[ "$1" == "--help" || "$1" == "-h" ]]; then
    _aipane_usage_help
    return 0
  fi

  while (( $# > 0 )); do
    arg="$1"
    case "$arg" in
      --timeout)
        shift
        if (( $# == 0 )) || [[ "$1" != <-> ]]; then
          print -u2 "cc-usage: --timeout requires an integer value"
          return 1
        fi
        timeout_seconds="$1"
        ;;
      --yes|-y)
        skip_interactive=1
        ;;
      *)
        command_parts+=("$arg")
        ;;
    esac
    shift
  done

  if (( ${#command_parts[@]} == 0 )); then
    followup_cmd="/usage"
  else
    followup_cmd="${(j: :)command_parts}"
  fi

  _aipane_list_accounts || {
    print -u2 "cc-usage: no Claude accounts found in $AIPANE_ACCOUNTS_BASE"
    return 1
  }

  _aipane_require_iterm || {
    print -u2 "cc-usage: requires iTerm2"
    return 1
  }

  if (( !skip_interactive && ${#reply[@]} > 1 )); then
    _aipane_usage_select_accounts || return 1
  fi

  accounts=("${reply[@]}")
  pane_count="${#accounts[@]}"
  if (( pane_count <= 0 )); then
    print -u2 "cc-usage: no accounts selected"
    return 1
  fi

  if (( pane_count >= 2 && pane_count <= 6 )); then
    if (( skip_interactive )); then
      # Keep -y default aligned with interactive default by using first preset.
      _aipane_usage_layout_presets "$pane_count" || return 1
      _aipane_usage_parse_preset "${reply[1]}" || return 1
    else
      _aipane_usage_select_layout "$pane_count" "${accounts[@]}" || return 1
    fi
  else
    _aipane_grid_for_count "$pane_count" || return 1
  fi

  grid_spec_parts=("${(s: :)REPLY}")
  cols="${grid_spec_parts[1]}"
  rows="${grid_spec_parts[2]}"
  rows_per_col=("${grid_spec_parts[@]:2}")

  for arg in "${accounts[@]}"; do
    _aipane_ccd_command_string "$arg"
    start_cmds+=("$REPLY")
  done

  print "Launching ${pane_count} panes..."

  _aipane_usage_build_and_run_applescript \
    "$pane_count" "$cols" "$rows" "$timeout_seconds" "$followup_cmd" \
    "${rows_per_col[@]}" \
    "${start_cmds[@]}"
}

ccusage() {
  cc-usage "$@"
}
