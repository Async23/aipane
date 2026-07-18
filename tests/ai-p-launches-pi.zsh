#!/usr/bin/env zsh

set -eu

ROOT_DIR="${0:A:h:h}"
TEST_DIR="$(mktemp -d "${TMPDIR:-/tmp}/aipane-ai-p-test.XXXXXX")"
trap 'rm -rf "$TEST_DIR"' EXIT HUP INT TERM

FAKE_BIN="$TEST_DIR/bin"
CAPTURE_FILE="$TEST_DIR/pi-args"
mkdir -p "$FAKE_BIN"

cat > "$FAKE_BIN/pi" <<'EOF'
#!/bin/sh

{
  printf 'command=pi\n'
  printf 'argc=%s\n' "$#"
  for arg in "$@"; do
    printf 'arg=%s\n' "$arg"
  done
} > "$AIPANE_TEST_CAPTURE"
EOF

cat > "$FAKE_BIN/omp" <<'EOF'
#!/bin/sh

echo "ai p must not launch omp" >&2
exit 99
EOF

chmod +x "$FAKE_BIN/pi" "$FAKE_BIN/omp"

export PATH="$FAKE_BIN:/usr/bin:/bin"
export AIPANE_TEST_CAPTURE="$CAPTURE_FILE"
unset AIPANE_PI_LAUNCH_CMD

source "$ROOT_DIR/init.zsh"

ai p -- --version "two words"

EXPECTED_OUTPUT=$'command=pi\nargc=2\narg=--version\narg=two words'
ACTUAL_OUTPUT="$(< "$CAPTURE_FILE")"

if [[ "$ACTUAL_OUTPUT" != "$EXPECTED_OUTPUT" ]]; then
  print -u2 "unexpected ai p invocation"
  print -u2 "expected:"
  print -u2 -r -- "$EXPECTED_OUTPUT"
  print -u2 "actual:"
  print -u2 -r -- "$ACTUAL_OUTPUT"
  exit 1
fi

HELP_OUTPUT="$(ai --help)"
if [[ "$HELP_OUTPUT" != *"p  Pi             (pi)"* ]]; then
  print -u2 "ai --help does not advertise Pi as the p launcher"
  exit 1
fi

print "ai p Pi launcher test passed"
