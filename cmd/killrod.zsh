#!/usr/bin/env zsh
# Command: killrod
# Kill ALL rod-related processes (leakless + Chromium) and clean up temp dirs

killrod() {
  local -a leakless_pids chromium_pids all_pids survivors
  local pid

  # Find all leakless processes matching rod
  leakless_pids=("${(@f)$(
    ps -axo pid=,command= | awk '$0 ~ /leakless.*rod/ { print $1 }'
  )}")

  # Find all Chromium processes launched by rod
  chromium_pids=("${(@f)$(
    ps -axo pid=,command= | awk '$0 ~ /\.cache\/rod\/browser\/.*Chromium/ { print $1 }'
  )}")

  # Merge and deduplicate
  all_pids=("${(@u)leakless_pids[@]}" "${(@u)chromium_pids[@]}")
  # Filter empty entries
  all_pids=("${(@)all_pids:#}")

  if (( ${#all_pids[@]} == 0 )); then
    print "killrod: no rod processes found"
    return 0
  fi

  print "killrod: sending SIGTERM to ${#all_pids[@]} process(es): ${all_pids[*]}"
  kill "${all_pids[@]}" 2>/dev/null

  sleep 1

  for pid in "${all_pids[@]}"; do
    if kill -0 "$pid" 2>/dev/null; then
      survivors+=("$pid")
    fi
  done

  if (( ${#survivors[@]} > 0 )); then
    print "killrod: sending SIGKILL to ${#survivors[@]} survivor(s): ${survivors[*]}"
    kill -9 "${survivors[@]}" 2>/dev/null
  fi

  print "killrod: killed ${#all_pids[@]} process(es)"

  # Clean up rod user-data dirs (glob pattern, not hardcoded)
  local -a rod_dirs
  rod_dirs=(/var/folders/*/*/T/rod/user-data(N))
  for dir in "${rod_dirs[@]}"; do
    local count=$(ls -1 "$dir" 2>/dev/null | wc -l | tr -d ' ')
    rm -rf "$dir"
    print "killrod: cleaned up user-data dir ($count entries): $dir"
  done
}
