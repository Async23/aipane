#!/usr/bin/env zsh
# Command: codexx
# Codex multi-profile launcher with interactive selection and caching.

codexx() {
  local config="$HOME/.codex/config.toml"
  local -a entries
  local cache_dir="${AIPANE_CACHE_DIR:-${XDG_CACHE_HOME:-$HOME/.cache}}"
  local cache_file="$cache_dir/codexx_last_profile"
  local default_choice=0
  local cached_name
  local idx
  local entry
  local name rest model provider
  local i choice
  local selected profile_name

  if (( $# > 0 )); then
    codex --yolo "$@"
    return
  fi

  if [[ ! -f "$config" ]]; then
    codex --yolo
    return
  fi

  entries=("${(@f)$(awk '
    BEGIN { current = "" }
    /^\[/ { current = $0; gsub(/[\[\] \t\r]/, "", current); next }
    current == "" && /^model[[:space:]]*=/ {
      val = $0; sub(/^[^"]*"/, "", val); sub(/".*/, "", val)
      default_model = val
    }
    current ~ /^profiles\./ && /^model[[:space:]]*=/ {
      p = current; sub(/^profiles\./, "", p)
      val = $0; sub(/^[^"]*"/, "", val); sub(/".*/, "", val)
      prof_model[p] = val; prof_order[++prof_count] = p
    }
    current ~ /^profiles\./ && /^model_provider[[:space:]]*=/ {
      p = current; sub(/^profiles\./, "", p)
      val = $0; sub(/^[^"]*"/, "", val); sub(/".*/, "", val)
      prof_prov[p] = val
    }
    current ~ /^model_providers\./ && /^name[[:space:]]*=/ {
      mp = current; sub(/^model_providers\./, "", mp)
      val = $0; sub(/^[^"]*"/, "", val); sub(/".*/, "", val)
      prov_name[mp] = val
    }
    END {
      printf "default\t%s\tOpenAI\n", default_model
      for (i = 1; i <= prof_count; i++) {
        p = prof_order[i]
        m = prof_model[p]; if (m == "") m = default_model
        mp = prof_prov[p]
        n = prov_name[mp]; if (n == "") n = mp
        printf "%s\t%s\t%s\n", p, m, n
      }
    }
  ' "$config")}")

  if (( ${#entries[@]} == 0 )); then
    codex --yolo
    return
  fi

  if [[ -r "$cache_file" ]]; then
    IFS= read -r cached_name < "$cache_file"
    if [[ -n "$cached_name" ]]; then
      idx=0
      for entry in "${entries[@]}"; do
        name="${entry%%	*}"
        if [[ "$name" == "$cached_name" ]]; then
          default_choice="$idx"
          break
        fi
        (( idx++ ))
      done
    fi
  fi

  print "Codex profile:"
  i=0
  for entry in "${entries[@]}"; do
    name="${entry%%	*}"
    rest="${entry#*	}"
    model="${rest%%	*}"
    provider="${rest#*	}"
    printf "  [%d] %-10s (%s, %s)\n" "$i" "$name" "$provider" "$model"
    (( i++ ))
  done

  while true; do
    read -r "choice?Choose [${default_choice}]: " < /dev/tty
    [[ -z "$choice" ]] && choice="$default_choice"
    if [[ "$choice" == <-> ]] && (( choice >= 0 && choice < ${#entries[@]} )); then
      break
    fi
    print "Invalid selection: $choice"
  done

  print

  selected="${entries[$((choice + 1))]}"
  profile_name="${selected%%	*}"
  mkdir -p "$cache_dir" && print -r -- "$profile_name" >| "$cache_file"

  if (( choice == 0 )); then
    codex --yolo
  else
    codex --yolo --profile "$profile_name"
  fi
}
