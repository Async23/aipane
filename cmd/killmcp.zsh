#!/usr/bin/env zsh
# Command: killmcp
# Compatibility wrapper around the unified cleanup engine.

killmcp() {
  local script="$AIPANE_ROOT/bin/aipane-cleanup"
  command "$script" mcp --verbose --session-age 18000 "$@"
}
