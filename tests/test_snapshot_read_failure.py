"""Distinguish unavailable pane directories from incomplete tmux reads."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SNAPSHOT = Path(__file__).resolve().parents[1] / "bin/aipane-snapshot"


class SnapshotReadFailureTests(unittest.TestCase):
    def test_unavailable_pane_directory_does_not_block_other_session_bindings(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            tmux = root / "tmux"
            tmux.write_text('''#!/usr/bin/env python3
import sys

if sys.argv[1] == 'list-panes':
    print('%7\\t0:6.4\\t')
    print('%3\\t0:1.1\\t/tmp')
elif sys.argv[1] == 'display-message':
    if 'pane_current_command' in sys.argv[-1]:
        pane = sys.argv[sys.argv.index('-t') + 1]
        command = 'yazi' if pane == '%7' else 'claude'
        print('\\x1f'.join([pane, command, '0:@1.' + pane,
                             '/dev/missing-snapshot', '/tmp/snapshot-test',
                             '999', '123', '', '', '', '']))
    else:
        print('/tmp/snapshot-test\\t999')
else:
    raise SystemExit(99)
''')
            tmux.chmod(0o755)
            dump = root / "snapshot.txt"
            dump.write_text("pane\t0\t1\t1\t:*\t1\tClaude\t:/tmp\t1\tclaude\t:claude\n")
            state = root / "state"
            state.mkdir()
            registry = state / "registry.jsonl"
            registry.write_text(json.dumps({"sock": "/tmp/snapshot-test", "srv": "999",
                                            "pane": "%3", "sid": "known", "tool": "c"}) + "\n")
            result = subprocess.run(
                [sys.executable, str(SNAPSHOT), str(dump)], text=True, capture_output=True,
                env={**os.environ, "AIPANE_TMUX": str(tmux),
                     "AIPANE_STATE_DIR": str(state), "AIPANE_REGISTRY": str(registry)},
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertNotIn("incomplete tmux pane list", result.stderr)
            expected = {"0:1.1": {"sid": "known", "tool": "c", "cwd": "/tmp"}}
            self.assertEqual(json.loads((state / "coords-last.json").read_text()), expected)
            metadata = next(line for line in dump.read_text().splitlines()
                            if line.startswith("aipane_sessions\t"))
            self.assertEqual(json.loads(metadata.split("\t", 1)[1])["bindings"], expected)

    def test_failed_or_incomplete_tmux_reads_preserve_snapshot_and_projection(self):
        for failure in ("server", "list-error", "list-empty", "list-malformed",
                        "list-no-id", "list-no-coordinate", "list-extra-field"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                tmux = root / "tmux"
                tmux.write_text('''#!/bin/sh
case "$1" in
  display-message)
    [ "$READ_FAILURE" = server ] && exit 1
    printf '/tmp/snapshot-test\\t999\\n'
    ;;
  list-panes)
    case "$READ_FAILURE" in
      list-error) exit 1 ;;
      list-empty) exit 0 ;;
      list-malformed) printf '%%3\\t0:1.1\\n' ;;
      list-no-id) printf '\\t0:1.1\\t/tmp\\n' ;;
      list-no-coordinate) printf '%%3\\t\\t/tmp\\n' ;;
      list-extra-field) printf '%%3\\t0:1.1\\t/tmp\\textra\\n' ;;
    esac
    ;;
  *) exit 99 ;;
esac
''')
                tmux.chmod(0o755)
                dump = root / "snapshot.txt"
                dump.write_text("pane\t0\t1\t1\t:*\t1\tCodex\t:/tmp\t1\tcodex\t:codex\n")
                state = root / "state"
                state.mkdir()
                projection = state / "coords-last.json"
                projection.write_text(json.dumps({"0:1.1": {"sid": "known", "tool": "x"}}))
                original_dump, original_projection = dump.read_bytes(), projection.read_bytes()
                result = subprocess.run(
                    [sys.executable, str(SNAPSHOT), str(dump)], text=True, capture_output=True,
                    env={**os.environ, "READ_FAILURE": failure, "AIPANE_TMUX": str(tmux),
                         "AIPANE_STATE_DIR": str(state), "AIPANE_REGISTRY": str(state / "missing")},
                )
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn("aipane-snapshot:", result.stderr)
                self.assertIn("aipane-snapshot:", (state / "snapshot-errors.log").read_text())
                self.assertEqual(dump.read_bytes(), original_dump)
                self.assertEqual(projection.read_bytes(), original_projection)


if __name__ == "__main__":
    unittest.main()
