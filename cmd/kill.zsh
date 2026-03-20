#!/usr/bin/env zsh
# Command: killcc
# Compatibility wrapper around the unified cleanup engine.

killcc() {
  local script="$AIPANE_ROOT/bin/aipane-cleanup"
  command "$script" ai --verbose "$@"
}
