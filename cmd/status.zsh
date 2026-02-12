#!/usr/bin/env zsh
# Command: cc-status

_aipane_status_extract_email() {
  local config_file="$1"

  jq -r '
    .oauthAccount.email? //
    .account.email? //
    .email? //
    .user.email? //
    empty
  ' "$config_file" 2>/dev/null
}

cc-status() {
  local -a accounts
  local account config_file state detail parsed_email

  _aipane_list_accounts || {
    print -u2 "cc-status: no Claude accounts found in $AIPANE_ACCOUNTS_BASE"
    return 1
  }

  accounts=("${reply[@]}")

  if ! command -v jq >/dev/null 2>&1; then
    print -u2 "cc-status: jq is required"
    return 1
  fi

  printf "%-32s %-12s %s\n" "ACCOUNT" "STATUS" "DETAIL"
  printf "%-32s %-12s %s\n" "--------------------------------" "------------" "------------------------------"

  for account in "${accounts[@]}"; do
    config_file="$AIPANE_ACCOUNTS_BASE/$account/.claude.json"

    if [[ ! -f "$config_file" ]]; then
      state="missing"
      detail=".claude.json not found"
    elif ! jq empty "$config_file" >/dev/null 2>&1; then
      state="invalid"
      detail="invalid JSON"
    else
      parsed_email="$(_aipane_status_extract_email "$config_file")"
      if [[ -n "$parsed_email" ]]; then
        state="logged-in"
        detail="$parsed_email"
      elif jq -e '
        .accessToken? != null or
        .refreshToken? != null or
        .apiKey? != null or
        .token? != null
      ' "$config_file" >/dev/null 2>&1; then
        state="configured"
        detail="token-like fields present"
      else
        state="unknown"
        detail="recognized file, unknown schema"
      fi
    fi

    printf "%-32s %-12s %s\n" "$account" "$state" "$detail"
  done
}

ccstatus() {
  cc-status "$@"
}
