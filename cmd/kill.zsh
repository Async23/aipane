#!/usr/bin/env zsh
# Command: killcc
# Kill detached/zombie AI CLI processes (TTY=??) for:
#   Claude Code, Codex, Factory Droid, Gemini CLI, Opencode

killcc() {
  local -a pids survivors
  local pid

  pids=("${(@f)$(
    ps -axo pid=,tty=,command= | awk '
      $2 == "??" {
        if ($0 ~ /claude/ ||
            $0 ~ /@anthropic-ai\/claude-code/ ||
            $0 ~ /claude-code/ ||
            $0 ~ /claude-guard/ ||
            $0 ~ /codex/ ||
            $0 ~ /\/droid\>/ ||
            $0 ~ /factory-droid/ ||
            $0 ~ /gemini/ ||
            $0 ~ /opencode/)
          print $1
      }
    '
  )}")

  # filter empty entries
  pids=("${(@)pids:#}")

  if (( ${#pids[@]} == 0 )); then
    print "killcc: no detached AI CLI processes found"
    return 0
  fi

  print "killcc: terminating ${#pids[@]} process(es): ${pids[*]}"
  kill "${pids[@]}" 2>/dev/null

  sleep 1

  for pid in "${pids[@]}"; do
    if kill -0 "$pid" 2>/dev/null; then
      survivors+=("$pid")
    fi
  done

  if (( ${#survivors[@]} > 0 )); then
    print "killcc: forcing ${#survivors[@]} remaining process(es): ${survivors[*]}"
    kill -9 "${survivors[@]}" 2>/dev/null
  fi
}
