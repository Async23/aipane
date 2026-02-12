#!/usr/bin/env zsh
# aipane entrypoint

if [[ -n "${_AIPANE_INIT_LOADED:-}" ]]; then
  return 0
fi
typeset -g _AIPANE_INIT_LOADED=1

typeset -g AIPANE_ROOT="${${(%):-%N}:A:h}"

source "$AIPANE_ROOT/lib/core.zsh"

for _aipane_f in "$AIPANE_ROOT"/cmd/*.zsh(N); do
  source "$_aipane_f"
done
unset _aipane_f
