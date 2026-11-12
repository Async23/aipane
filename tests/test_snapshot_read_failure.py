"""A failed tmux read must not replace recoverable identities with an empty map."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SNAPSHOT = Path(__file__).resolve().parents[1] / "bin/aipane-snapshot"


class SnapshotReadFailureTests(unittest.TestCase):
    def test_failed_or_incomplete_tmux_reads_preserve_snapshot_and_projection(self):
        for failure in ("server", "list-error", "list-empty", "list-malformed"):
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
