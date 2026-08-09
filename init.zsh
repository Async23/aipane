#!/usr/bin/env zsh
# aipane entrypoint

if [[ "${_AIPANE_INIT_VERSION:-}" == "3" ]]; then
  return 0
fi
typeset -g _AIPANE_INIT_LOADED=1
typeset -g _AIPANE_INIT_VERSION=3

# Remove only the public functions owned by the retired multi-account module.
# A user-defined function with the same short name must remain untouched.
if (( $+functions[cc] )) &&
   [[ "${functions[cc]}" == *'_aipane_cc_invoke "normal" "$@"'* ]]; then
  unfunction cc
fi
if (( $+functions[ccd] )) &&
   [[ "${functions[ccd]}" == *'_aipane_cc_invoke "danger" --no-account-dir "$@"'* ]]; then
  unfunction ccd
fi

# Remove only the retired launcher shortcuts that were defined by aipane.
# User-owned functions and aliases with the same names must remain untouched.
if (( $+functions[codexx] )) &&
   [[ "${functions[codexx]}" == *'codex --yolo --disable plugins "$@"'* ]]; then
  unfunction codexx
fi
if (( $+aliases[geminii] )) && [[ "${aliases[geminii]}" == 'gemini --yolo' ]]; then
  unalias geminii
fi
if (( $+aliases[oc] )) && [[ "${aliases[oc]}" == 'opencode' ]]; then
  unalias oc
fi

for _aipane_legacy_f in \
  _aipane_cc_invoke \
  _aipane_link_if_missing \
  _aipane_account_dir \
  _aipane_list_accounts \
  _aipane_cached_name_index \
  _aipane_store_cached_name \
  _aipane_select_account \
  _aipane_shell_join \
  _aipane_cc_command_string \
  _aipane_ccd_command_string \
  _aipane_project_dir_key; do
  (( $+functions[$_aipane_legacy_f] )) && unfunction "$_aipane_legacy_f"
done
unset _aipane_legacy_f

typeset -g AIPANE_ROOT="${${(%):-%N}:A:h}"

source "$AIPANE_ROOT/lib/core.zsh"
source "$AIPANE_ROOT/lib/session.zsh"

for _aipane_f in "$AIPANE_ROOT"/cmd/*.zsh(N); do
  source "$_aipane_f"
done
unset _aipane_f
