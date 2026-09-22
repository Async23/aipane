#!/bin/sh

set -eu

ROOT_DIR=$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)
TEST_DIR=$(mktemp -d "${TMPDIR:-/tmp}/aipane-cleanup-modes.XXXXXX")
trap 'rm -rf "$TEST_DIR"' EXIT HUP INT TERM

FAKE_BIN="$TEST_DIR/bin"
TEST_HOME="$TEST_DIR/home"
mkdir -p "$FAKE_BIN" "$TEST_HOME"

# Every invocation is dry-run and every process query uses this fixture.
# Include detached agents so reintroducing AI selection breaks the contract.
cat > "$FAKE_BIN/ps" <<'EOF'
#!/bin/sh
printf '%s\n' "$*" >> "$AIPANE_TEST_PS_CALLS"
case "$*" in
  '-axo pid=,ppid=,tty=,etime=,command=')
    printf '%s\n' \
      '999990 1 ?? 30:00 /opt/homebrew/bin/codex --yolo' \
      '999991 1 ?? 30:00 /opt/homebrew/bin/claude' \
      '999992 1 ?? 30:00 /opt/homebrew/bin/chrome-devtools-mcp --stdio' \
      '999993 1 ?? 30:00 /tmp/go-rod/leakless --rod-dir /tmp/rod' \
      '999994 999993 ?? 30:00 /Users/test/.cache/rod/browser/chromium-123/Chromium'
    ;;
  '-axo pid=,etime=,command=')
    printf '%s\n' \
      '999990 30:00 /opt/homebrew/bin/codex --yolo' \
      '999991 30:00 /opt/homebrew/bin/claude' \
      '999993 30:00 /tmp/go-rod/leakless --rod-dir /tmp/rod'
    ;;
  '-axo pid=,ppid=,command=')
    printf '%s\n' \
      '999990 1 /opt/homebrew/bin/codex --yolo' \
      '999991 1 /opt/homebrew/bin/claude' \
      '999994 999993 /Users/test/.cache/rod/browser/chromium-123/Chromium'
    ;;
  '-axo pid=,command=')
    printf '%s\n' \
      '999990 /opt/homebrew/bin/codex --yolo' \
      '999991 /opt/homebrew/bin/claude' \
      '999993 /tmp/go-rod/leakless --rod-dir /tmp/rod' \
      '999994 /Users/test/.cache/rod/browser/chromium-123/Chromium'
    ;;
  '-axo pid=,ppid=,tty=,command=')
    printf '%s\n' \
      '999990 1 ?? /opt/homebrew/bin/codex --yolo' \
      '999991 1 ?? /opt/homebrew/bin/claude'
    ;;
  '-axo pid=,ppid=,pgid=,tty=,command=')
    printf '%s\n' \
      '999990 1 999990 ?? /opt/homebrew/bin/codex --yolo' \
      '999991 1 999991 ?? /opt/homebrew/bin/claude'
    ;;
  *)
    echo "unexpected ps arguments: $*" >&2
    exit 1
    ;;
esac
EOF
chmod +x "$FAKE_BIN/ps"

run_cleanup() {
  HOME="$TEST_HOME" \
  PATH="$FAKE_BIN:/usr/bin:/bin" \
  AIPANE_TMUX_BIN=/usr/bin/false \
  AIPANE_TEST_PS_CALLS="$TEST_DIR/ps-calls" \
    "$ROOT_DIR/bin/aipane-cleanup" "$@" --dry-run --verbose
}

if run_cleanup ai --force > "$TEST_DIR/rejected" 2>&1; then
  echo 'retired ai mode must be rejected' >&2
  exit 1
fi
if ! grep -q '^aipane-cleanup: unknown argument: ai$' "$TEST_DIR/rejected"; then
  cat "$TEST_DIR/rejected" >&2
  exit 1
fi
if [ -e "$TEST_DIR/ps-calls" ] || [ -e "$TEST_HOME/logs" ]; then
  echo 'retired ai mode must fail before process scans or cleanup initialization' >&2
  exit 1
fi

assert_candidates() {
  expected="$1"
  shift
  output=$(run_cleanup "$@")
  candidates=$(printf '%s\n' "$output" | grep 'would kill')
  if [ "$candidates" != "$expected" ]; then
    echo 'cleanup must select only Rod browsers and MCP helpers' >&2
    printf 'expected:\n%s\nactual:\n%s\n' "$expected" "$output" >&2
    exit 1
  fi
}

expected='aipane-cleanup: rod: would kill 1 stale rod leakless process(es): 999993
aipane-cleanup: rod: would kill 1 orphaned rod Chromium child process(es): 999994
aipane-cleanup: mcp: would kill 1 stale MCP helper process(es): 999992'
assert_candidates "$expected"
assert_candidates "$expected" all
assert_candidates 'aipane-cleanup: rod: would kill 2 rod process(es): 999993 999994
aipane-cleanup: mcp: would kill 1 stale MCP helper process(es): 999992' all --force

echo 'aipane cleanup mode tests passed'
