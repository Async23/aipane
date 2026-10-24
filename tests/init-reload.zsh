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
codexx() {
  codex --yolo --disable plugins "$@"
}
killcc() {
  local script="$AIPANE_ROOT/bin/aipane-cleanup"
  command "$script" ai --verbose "$@"
}
alias geminii='gemini --yolo'
alias oc='opencode'
typeset -g _AIPANE_INIT_LOADED=1
typeset -g _AIPANE_INIT_VERSION=4
typeset -g _AIPANE_CORE_LOADED=1
unset AIPANE_CLAUDE_LAUNCH_CMD

source "$AIPANE_TEST_ROOT/init.zsh"

if (( $+functions[cc] || $+functions[ccd] )); then
  print -u2 "legacy aipane cc/ccd functions survived a version reload"
  exit 1
fi
if (( $+functions[codexx] || $+aliases[geminii] || $+aliases[oc] )); then
  print -u2 "retired AI launcher shortcuts survived a version reload"
  exit 1
fi
if (( $+functions[killcc] )); then
  print -u2 "retired cleanup wrapper survived a version reload"
  exit 1
fi
source "$AIPANE_TEST_ROOT/init.zsh"
if (( $+functions[killcc] )); then
  print -u2 "retired cleanup wrapper returned after a second source"
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
codexx() {
  print "user-owned-codexx"
}
killcc() {
  print "user-owned-killcc"
}
alias geminii='user-gemini-command'
alias oc='user-opencode-command'
typeset -g _AIPANE_INIT_LOADED=1
typeset -g _AIPANE_CORE_LOADED=1

source "$AIPANE_TEST_ROOT/init.zsh"

if (( ! $+functions[cc] )) || [[ "${functions[cc]}" != *"user-owned-cc"* ]]; then
  print -u2 "reload removed a user-owned cc function"
  exit 1
fi
if (( ! $+functions[codexx] )) || [[ "${functions[codexx]}" != *"user-owned-codexx"* ]]; then
  print -u2 "reload removed a user-owned codexx function"
  exit 1
fi
if (( ! $+functions[killcc] )) || [[ "${functions[killcc]}" != *"user-owned-killcc"* ]]; then
  print -u2 "reload removed a user-owned cleanup function"
  exit 1
fi
if [[ "${aliases[geminii]:-}" != "user-gemini-command" ||
      "${aliases[oc]:-}" != "user-opencode-command" ]]; then
  print -u2 "reload removed a user-owned launcher alias"
  exit 1
fi
ZSH

AIPANE_TEST_ROOT="$ROOT_DIR" zsh -df <<'ZSH'
set -eu
typeset -g _AIPANE_INIT_VERSION=3
typeset -g _AIPANE_CORE_VERSION=2
typeset -g AIPANE_DROID_LAUNCH_CMD=droid
unset AIPANE_DSH_LAUNCH_CMD TMUX_PANE
typeset -ga received=()
ai() { droid "$@"; }
droid() { return 99; }
dsh-tui() { received=("$@"); }

source "$AIPANE_TEST_ROOT/init.zsh"
ai d -- --version "two words"

if (( ${#received[@]} != 2 )) ||
   [[ "${received[1]}" != --version || "${received[2]}" != "two words" ]]; then
  print -u2 "reloaded ai d did not launch dsh-TUI with intact arguments"
  exit 1
fi
ZSH

AIPANE_TEST_ROOT="$ROOT_DIR" zsh -df <<'ZSH'
set -eu
typeset -g _AIPANE_INIT_VERSION=5
typeset -g _AIPANE_CORE_VERSION=3
typeset -g AIPANE_CODEX_LAUNCH_CMD="codex --yolo"
unset TMUX_PANE
typeset -ga received=()
codex() { received=("$@"); }

source "$AIPANE_TEST_ROOT/init.zsh"
ai x resume "session with spaces"
if [[ "${(j:|:)received}" != \
      '--yolo|-c|check_for_update_on_startup=false|resume|session with spaces' ]]; then
  print -u2 "reload did not migrate the Codex default with intact resume arguments"
  exit 1
fi
ZSH

AIPANE_TEST_ROOT="$ROOT_DIR" zsh -df <<'ZSH'
set -eu
typeset -g _AIPANE_INIT_VERSION=5
typeset -g _AIPANE_CORE_VERSION=3
typeset -g AIPANE_CODEX_LAUNCH_CMD="custom-codex --custom"
unset TMUX_PANE
typeset -ga received=()
custom-codex() { received=("$@"); }

source "$AIPANE_TEST_ROOT/init.zsh"
ai x "two words"
if [[ "${(j:|:)received}" != '--custom|two words' ]]; then
  print -u2 "reload changed the user-owned Codex launcher"
  exit 1
fi
ZSH

print "init reload tests passed"
