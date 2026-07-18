#!/bin/sh

set -eu

ROOT_DIR=$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)
TEST_DIR=$(mktemp -d "${TMPDIR:-/tmp}/aipane-cleanup-contracts.XXXXXX")
FAKE_BIN="$TEST_DIR/bin"
TEST_HOME="$TEST_DIR/home"
ROD_ENTRY=""
ROD_USER_DATA=""

cleanup() {
  rm -rf "$TEST_DIR"
  if [ -n "$ROD_ENTRY" ]; then
    rmdir "$ROD_ENTRY" 2>/dev/null || true
    rmdir "$ROD_USER_DATA" 2>/dev/null || true
    rmdir "${ROD_USER_DATA%/user-data}" 2>/dev/null || true
  fi
}
trap cleanup EXIT HUP INT TERM

mkdir -p "$FAKE_BIN" "$TEST_HOME"

cat > "$FAKE_BIN/_fake" <<'EOF'
#!/bin/sh

name=${0##*/}

case "$name" in
  ps)
    case "${AIPANE_TEST_PS_MODE:-}" in
      ai_false_positive)
        case "$*" in
          '-axo pid=,ppid=,tty=,command=')
            printf '%s\n' '999991 1 ?? /usr/bin/python3 worker.py --label codex'
            ;;
          '-axo pid=,ppid=,pgid=,tty=,command=')
            printf '%s\n' '999991 1 999991 ?? /usr/bin/python3 worker.py --label codex'
            ;;
        esac
        ;;
      ai_direct)
        case "$*" in
          '-axo pid=,ppid=,tty=,command=')
            printf '%s\n' '999991 1 ?? /opt/homebrew/bin/codex --yolo'
            ;;
          '-axo pid=,ppid=,pgid=,tty=,command=')
            printf '%s\n' '999991 1 999991 ?? /opt/homebrew/bin/codex --yolo'
            ;;
        esac
        ;;
      ai_node_script)
        case "$*" in
          '-axo pid=,ppid=,tty=,command=')
            printf '%s\n' '999991 1 ?? /opt/homebrew/bin/node /tmp/node_modules/@openai/codex/bin/codex.js'
            ;;
          '-axo pid=,ppid=,pgid=,tty=,command=')
            printf '%s\n' '999991 1 999991 ?? /opt/homebrew/bin/node /tmp/node_modules/@openai/codex/bin/codex.js'
            ;;
        esac
        ;;
      mcp_false_positive)
        case "$*" in
          '-axo pid=,ppid=,tty=,etime=,command=')
            printf '%s\n' '999991 1 ?? 20:00 /usr/bin/python3 worker.py --label chrome-devtools-mcp'
            ;;
        esac
        ;;
      mcp_direct)
        case "$*" in
          '-axo pid=,ppid=,tty=,etime=,command=')
            printf '%s\n' '999991 1 ?? 20:00 /opt/homebrew/bin/chrome-devtools-mcp --stdio'
            ;;
        esac
        ;;
      mcp_npm_exec)
        case "$*" in
          '-axo pid=,ppid=,tty=,etime=,command=')
            printf '%s\n' '999991 1 ?? 20:00 /opt/homebrew/bin/npm exec @upstash/context7-mcp'
            ;;
        esac
        ;;
      mcp_node_script)
        case "$*" in
          '-axo pid=,ppid=,tty=,etime=,command=')
            printf '%s\n' '999991 1 ?? 20:00 /opt/homebrew/bin/node /tmp/node_modules/chrome-devtools-mcp/build/src/index.js'
            ;;
        esac
        ;;
      rod_false_positive)
        case "$*" in
          '-axo pid=,command=')
            printf '%s\n' '999991 /usr/bin/python3 worker.py --note leakless-marker --kind rod-marker'
            ;;
        esac
        ;;
      rod_direct)
        case "$*" in
          '-axo pid=,command=')
            printf '%s\n' '999991 /private/var/folders/example/T/go-rod/leakless --rod-dir /tmp/rod'
            ;;
        esac
        ;;
      rod_chromium)
        case "$*" in
          '-axo pid=,command=')
            printf '%s\n' '999991 /Users/test/.cache/rod/browser/chromium-123/Chromium.app/Contents/MacOS/Chromium'
            ;;
        esac
        ;;
      rod_playwright)
        case "$*" in
          '-axo pid=,command=')
            printf '%s\n' '999991 /Users/test/Library/Caches/ms-playwright/chromium-123/chrome-headless-shell'
            ;;
        esac
        ;;
    esac
    ;;
  tmux)
    exit 0
    ;;
  pgrep|lsof)
    exit 1
    ;;
  kill|killall|pkill|launchctl)
    exit 0
    ;;
esac

exit 0
EOF
chmod +x "$FAKE_BIN/_fake"
for name in ps tmux pgrep lsof kill killall pkill launchctl; do
  ln -s _fake "$FAKE_BIN/$name"
done

run_cleanup() {
  mode="$1"
  shift
  HOME="$TEST_HOME" \
  PATH="$FAKE_BIN:/usr/bin:/bin:/usr/sbin:/sbin" \
  AIPANE_TMUX_BIN="$FAKE_BIN/tmux" \
  AIPANE_TEST_PS_MODE="$mode" \
    "$ROOT_DIR/bin/aipane-cleanup" "$@"
}

assert_eq() {
  expected="$1"
  actual="$2"
  label="$3"
  if [ "$actual" != "$expected" ]; then
    echo "$label" >&2
    echo "expected: $expected" >&2
    echo "actual:   $actual" >&2
    exit 1
  fi
}

output=$(run_cleanup ai_false_positive ai --force --dry-run --verbose)
assert_eq \
  'killcc: no detached AI CLI or old detached tmux Claude session processes found' \
  "$output" \
  'AI cleanup must ignore tool names that occur only in unrelated arguments'

output=$(run_cleanup ai_direct ai --force --dry-run --verbose)
assert_eq \
  'killcc: ai: would kill 1 detached AI CLI tree process(es): 999991' \
  "$output" \
  'AI cleanup must still match a directly executed AI CLI'

output=$(run_cleanup ai_node_script ai --force --dry-run --verbose)
assert_eq \
  'killcc: ai: would kill 1 detached AI CLI tree process(es): 999991' \
  "$output" \
  'AI cleanup must still match an AI CLI launched through its Node package script'

output=$(run_cleanup mcp_false_positive mcp --force --dry-run --verbose)
assert_eq \
  'killmcp: no stale MCP helper processes found' \
  "$output" \
  'MCP cleanup must ignore helper names that occur only in unrelated arguments'

output=$(run_cleanup mcp_direct mcp --force --dry-run --verbose)
assert_eq \
  'killmcp: mcp: would kill 1 stale MCP helper process(es): 999991' \
  "$output" \
  'MCP cleanup must still match a directly executed MCP helper'

output=$(run_cleanup mcp_npm_exec mcp --force --dry-run --verbose)
assert_eq \
  'killmcp: mcp: would kill 1 stale MCP helper process(es): 999991' \
  "$output" \
  'MCP cleanup must still match an MCP helper launched through npm exec'

output=$(run_cleanup mcp_node_script mcp --force --dry-run --verbose)
assert_eq \
  'killmcp: mcp: would kill 1 stale MCP helper process(es): 999991' \
  "$output" \
  'MCP cleanup must still match an MCP helper launched through its Node package script'

output=$(run_cleanup rod_false_positive rod --force --dry-run --verbose)
assert_eq \
  'killrod: no rod processes found' \
  "$output" \
  'Rod cleanup must ignore leakless and rod markers in unrelated arguments'

output=$(run_cleanup rod_direct rod --force --dry-run --verbose)
assert_eq \
  'killrod: rod: would kill 1 rod process(es): 999991' \
  "$output" \
  'Rod cleanup must still match a directly executed leakless helper'

output=$(run_cleanup rod_chromium rod --force --dry-run --verbose)
assert_eq \
  'killrod: rod: would kill 1 rod process(es): 999991' \
  "$output" \
  'Rod cleanup must still match Rod-managed Chromium executables'

output=$(run_cleanup rod_playwright rod --force --dry-run --verbose)
assert_eq \
  'killrod: rod: would kill 1 rod process(es): 999991' \
  "$output" \
  'Rod cleanup must still match Playwright Chromium helper executables'

case "${TMPDIR:-}" in
  /var/folders/*/*/T/)
    ROD_USER_DATA="${TMPDIR%/}/rod/user-data"
    ROD_ENTRY="$ROD_USER_DATA/aipane-contract-$$"
    mkdir -p "$ROD_ENTRY"
    output=$(run_cleanup empty rod --dry-run --verbose)
    assert_eq \
      'rod-cleanup: no stale rod processes found' \
      "$output" \
      'Rod cleanup must not clean profiles when no stale Rod process exists'
    ;;
esac

echo "aipane cleanup contract tests passed"
