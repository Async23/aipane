#!/bin/sh

set -eu

ROOT_DIR=$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)
TEST_DIR=$(mktemp -d "${TMPDIR:-/tmp}/aipane-cleanup-test.XXXXXX")
trap 'rm -rf "$TEST_DIR"' EXIT HUP INT TERM

FAKE_BIN="$TEST_DIR/bin"
TEST_HOME="$TEST_DIR/home"
mkdir -p "$FAKE_BIN" "$TEST_HOME"

cat > "$FAKE_BIN/ps" <<'EOF'
#!/bin/sh

case "$*" in
  '-axo pid=,ppid=,tty=,command=')
    printf '%s\n' \
      '100 1 ?? /Applications/ChatGPT.app/Contents/MacOS/ChatGPT' \
      '101 100 ?? /opt/homebrew/bin/codex -c features.code_mode_host=true app-server --analytics-default-enabled' \
      '102 101 ?? /bin/sh -c app-server-helper' \
      '103 100 ?? /opt/homebrew/bin/codex exec background-task' \
      '110 1 ?? /opt/homebrew/bin/codex app-server' \
      '200 1 ?? /opt/homebrew/bin/codex --yolo' \
      '300 1 ?? /opt/homebrew/bin/claude app-server' \
      '400 1 ?? /bin/sh -c /Applications/ChatGPT.app/Contents/MacOS/ChatGPT --diagnostic' \
      '401 400 ?? /opt/homebrew/bin/codex --yolo'
    ;;
  '-axo pid=,ppid=,pgid=,tty=,command=')
    printf '%s\n' \
      '100 1 100 ?? /Applications/ChatGPT.app/Contents/MacOS/ChatGPT' \
      '101 100 100 ?? /opt/homebrew/bin/codex -c features.code_mode_host=true app-server --analytics-default-enabled' \
      '102 101 100 ?? /bin/sh -c app-server-helper' \
      '103 100 100 ?? /opt/homebrew/bin/codex exec background-task' \
      '110 1 110 ?? /opt/homebrew/bin/codex app-server' \
      '200 1 200 ?? /opt/homebrew/bin/codex --yolo' \
      '201 200 200 ?? /bin/sh -c detached-helper' \
      '300 1 300 ?? /opt/homebrew/bin/claude app-server' \
      '400 1 400 ?? /bin/sh -c /Applications/ChatGPT.app/Contents/MacOS/ChatGPT --diagnostic' \
      '401 400 400 ?? /opt/homebrew/bin/codex --yolo'
    ;;
  '-axo pid=,ppid=,tty=,etime=,command=')
    ;;
  *)
    echo "unexpected ps arguments: $*" >&2
    exit 1
    ;;
esac
EOF
chmod +x "$FAKE_BIN/ps"

OUTPUT=$(
  HOME="$TEST_HOME" \
  PATH="$FAKE_BIN:/usr/bin:/bin" \
  AIPANE_TMUX_BIN=/usr/bin/false \
    "$ROOT_DIR/bin/aipane-cleanup" ai --dry-run --verbose
)

EXPECTED_OUTPUT='killcc: ai: would kill 4 detached AI CLI tree process(es): 200 201 300 401'
if [ "$OUTPUT" != "$EXPECTED_OUTPUT" ]; then
  echo "unexpected cleanup candidates" >&2
  echo "expected: $EXPECTED_OUTPUT" >&2
  echo "actual:   $OUTPUT" >&2
  exit 1
fi

echo "aipane-cleanup AI protection test passed"
