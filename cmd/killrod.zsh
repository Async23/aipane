#!/usr/bin/env zsh
# Command: killrod
# Kill orphaned rod/Chromium processes (PPID=1) left behind by xiaohongshu-mcp

killrod() {
  local -a pids survivors
  local pid

  pids=("${(@f)$(
    ps -axo pid=,ppid=,command= | awk '
      $2 == 1 && $0 ~ /\.cache\/rod\/browser\/.*Chromium/ {
        print $1
      }
    '
  )}")

  # filter empty entries
  pids=("${(@)pids:#}")

  if (( ${#pids[@]} == 0 )); then
    print "killrod: no orphaned rod Chromium processes found"
    return 0
  fi

  print "killrod: terminating ${#pids[@]} process(es): ${pids[*]}"
  kill "${pids[@]}" 2>/dev/null

  sleep 1

  for pid in "${pids[@]}"; do
    if kill -0 "$pid" 2>/dev/null; then
      survivors+=("$pid")
    fi
  done

  if (( ${#survivors[@]} > 0 )); then
    print "killrod: forcing ${#survivors[@]} remaining process(es): ${survivors[*]}"
    kill -9 "${survivors[@]}" 2>/dev/null
  fi

  # clean up stale user-data dirs
  local rod_tmp="/var/folders/np/mhwdd58141xflc0rkw1_rc0h0000gn/T/rod/user-data"
  if [[ -d "$rod_tmp" ]]; then
    local count=$(ls -1 "$rod_tmp" 2>/dev/null | wc -l | tr -d ' ')
    if (( count > 0 )); then
      rm -rf "$rod_tmp"
      print "killrod: cleaned up ${count} stale user-data dir(s)"
    fi
  fi
}
