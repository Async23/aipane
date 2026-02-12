#!/usr/bin/env zsh

_aipane_help() {
  cat <<'EOF'
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
EOF
}

_aipane_ceil_sqrt() {
  local n="$1"
  local i=1
  while (( i * i < n )); do
    (( i++ ))
  done
  REPLY="$i"
}

_aipane_list_accounts() {
  local base="$HOME/.claude-accounts"
  local -a dirs accounts
  local name

  if [[ ! -d "$base" ]]; then
    print -u2 "ai: no Claude accounts found in ~/.claude-accounts/"
    return 1
  fi

  dirs=("$base"/*(/N:t))
  for name in "${dirs[@]}"; do
    [[ "$name" == "_shared" ]] && continue
    accounts+=("$name")
  done
  accounts=("${(@on)accounts}")

  if (( ${#accounts[@]} == 0 )); then
    print -u2 "ai: no Claude accounts found in ~/.claude-accounts/"
    return 1
  fi

  reply=("${accounts[@]}")
}

_aipane_select_account() {
  local label="$1"
  local -a accounts
  local i choice

  _aipane_list_accounts || return 1
  accounts=("${reply[@]}")

  if (( ${#accounts[@]} == 1 )); then
    REPLY="${accounts[1]}"
    print "Claude Code #${label} -> ${REPLY}"
    return 0
  fi

  print "Claude Code #${label} - select account:"
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

_aipane_build_and_run_applescript() {
  local pane_count="$1"
  local cols="$2"
  local rows="$3"
  local -a rows_per_col commands osa_args
  shift 3
  rows_per_col=("${@:1:$cols}")
  shift "$cols"
  commands=("${@}")

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
  local -a tools commands rows_per_col
  local pane_count cols rows c_index i
  local ch

  if (( $# == 0 )) || [[ "$tools_str" == "--help" ]] || [[ "$tools_str" == "-h" ]]; then
    _aipane_help
    return 0
  fi

  if (( $# != 1 )); then
    print -u2 "ai: expected one tools string"
    _aipane_help
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

  pane_count="${#tools[@]}"

  if [[ "$TERM_PROGRAM" != "iTerm.app" ]]; then
    print -u2 "ai: requires iTerm2"
    return 1
  fi

  c_index=0
  for ch in "${tools[@]}"; do
    case "$ch" in
      c)
        (( c_index++ ))
        _aipane_select_account "$c_index" || return 1
        commands+=("ccd ${REPLY}")
        ;;
      x) commands+=("codex --yolo") ;;
      d) commands+=("droid") ;;
      g) commands+=("gemini --yolo") ;;
    esac
  done

  if (( pane_count == 1 )); then
    cols=1
    rows=1
    rows_per_col=(1)
  elif (( pane_count == 2 )); then
    cols=2
    rows=1
    rows_per_col=(1 1)
  elif (( pane_count == 3 )); then
    cols=3
    rows=1
    rows_per_col=(1 1 1)
  elif (( pane_count == 4 )); then
    cols=2
    rows=2
    rows_per_col=(2 2)
  else
    local c
    _aipane_ceil_sqrt "$pane_count"
    cols="$REPLY"
    rows=$(( (pane_count + cols - 1) / cols ))
    for (( c = 1; c <= cols; c++ )); do
      rows_per_col+=($(( ((pane_count - c) / cols) + 1 )))
    done
  fi

  if (( pane_count == 1 )); then
    print "Opening 1 pane..."
  else
    print "Opening ${pane_count} panes..."
  fi

  _aipane_build_and_run_applescript "$pane_count" "$cols" "$rows" "${rows_per_col[@]}" "${commands[@]}" || {
    print -u2 "ai: failed to control iTerm2 via AppleScript"
    return 1
  }
}
