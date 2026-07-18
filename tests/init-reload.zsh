#!/usr/bin/env zsh

set -eu

ROOT_DIR="${0:A:h:h}"

AIPANE_TEST_ROOT="$ROOT_DIR" zsh -df <<'ZSH'
cc() {
  _aipane_cc_invoke "normal" "$@"
}
ccd() {
  _aipane_cc_invoke "danger" --no-account-dir "$@"
}
typeset -g _AIPANE_INIT_LOADED=1
typeset -g _AIPANE_CORE_LOADED=1
unset AIPANE_CLAUDE_LAUNCH_CMD

source "$AIPANE_TEST_ROOT/init.zsh"

if (( $+functions[cc] || $+functions[ccd] )); then
  print -u2 "legacy aipane cc/ccd functions survived a version reload"
  exit 1
fi
if [[ "${AIPANE_CLAUDE_LAUNCH_CMD:-}" != "claude --dangerously-skip-permissions" ]]; then
  print -u2 -r -- \
    "current Claude launcher was not loaded: ${AIPANE_CLAUDE_LAUNCH_CMD:-<unset>}"
  exit 1
fi
ZSH

AIPANE_TEST_ROOT="$ROOT_DIR" zsh -df <<'ZSH'
cc() {
  print "user-owned-cc"
}
typeset -g _AIPANE_INIT_LOADED=1
typeset -g _AIPANE_CORE_LOADED=1

source "$AIPANE_TEST_ROOT/init.zsh"

if (( ! $+functions[cc] )) || [[ "${functions[cc]}" != *"user-owned-cc"* ]]; then
  print -u2 "reload removed a user-owned cc function"
  exit 1
fi
ZSH

print "init reload tests passed"
