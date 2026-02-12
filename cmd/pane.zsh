#!/usr/bin/env zsh
# Command: ai

_aipane_pane_help() {
  cat <<'EOH'
Usage:
  ai <tools_string>

Tools:
  c  Claude Code   (ccd <email>)
  x  Codex         (codex --yolo)
  d  Droid         (droid)
  g  Gemini        (gemini --yolo)

Examples:
  ai cxdg
  ai cc
  ai g
EOH
}

_aipane_pane_build_and_run_applescript() {
  local pane_count="$1"
  local cols="$2"
  local rows="$3"
  local -a rows_per_col commands osa_args
  shift 3

  rows_per_col=("${@:1:$cols}")
  shift "$cols"
  commands=("$@")

  osa_args=("$pane_count" "$cols" "$rows")
  osa_args+=("${rows_per_col[@]}")
  osa_args+=("${commands[@]}")

  osascript - "${osa_args[@]}" <<'APPLESCRIPT'
on run argv
  set paneCount to (item 1 of argv) as integer
  set colCount to (item 2 of argv) as integer
  set rowCount to (item 3 of argv) as integer

  set argIndex to 4
  set rowSpec to {}
  repeat with c from 1 to colCount
    copy ((item argIndex of argv) as integer) to end of rowSpec
    set argIndex to argIndex + 1
  end repeat

  set cmdList to {}
  repeat with i from 1 to paneCount
    copy (item argIndex of argv) to end of cmdList
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

        if paneCount > 1 then
          repeat with i from 2 to paneCount
            tell (item i of orderedSessions)
              write text (item i of cmdList)
            end tell
          end repeat
        end if

        tell (item 1 of orderedSessions)
          write text (item 1 of cmdList)
        end tell
      end tell
    end tell
  end tell
end run
APPLESCRIPT
}

ai() {
  local tools_str="$1"
  local -a tools commands rows_per_col grid_spec_parts
  local pane_count cols rows c_index i
  local ch

  if (( $# == 0 )) || [[ "$tools_str" == "--help" ]] || [[ "$tools_str" == "-h" ]]; then
    _aipane_pane_help
    return 0
  fi

  if (( $# != 1 )); then
    print -u2 "ai: expected one tools string"
    _aipane_pane_help
    return 1
  fi

  for (( i = 1; i <= ${#tools_str}; i++ )); do
    ch="${tools_str[i]}"
    case "$ch" in
      c|x|d|g) tools+=("$ch") ;;
      *)
        print -u2 "ai: unknown tool '${ch}' (valid: c/x/d/g)"
        return 1
        ;;
    esac
  done

  _aipane_require_iterm || {
    print -u2 "ai: requires iTerm2"
    return 1
  }

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
    esac
  done

  pane_count="${#tools[@]}"

  _aipane_grid_for_count "$pane_count" || return 1
  grid_spec_parts=("${(s: :)REPLY}")
  cols="${grid_spec_parts[1]}"
  rows="${grid_spec_parts[2]}"
  rows_per_col=("${grid_spec_parts[@]:2}")

  _aipane_pane_build_and_run_applescript \
    "$pane_count" "$cols" "$rows" \
    "${rows_per_col[@]}" \
    "${commands[@]}"
}
