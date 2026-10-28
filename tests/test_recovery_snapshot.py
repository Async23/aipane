"""A snapshot's session identities cannot leak across dumps or servers."""
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
from recovery_snapshot import capture_snapshot, snapshot_bindings, SnapshotError


class RecoverySnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.first = self.root / "first.dump"
        self.second = self.root / "second.dump"
        for dump in (self.first, self.second):
            dump.write_text("same tmux layout\n")

    def test_matching_layouts_from_different_servers_keep_their_own_bindings(self):
        a = {"0:1.1": {"sid": "original", "tool": "x"}}
        b = {"0:1.1": {"sid": "different", "tool": "x"}}
        capture_snapshot(self.first, a, ("/tmp/a", "1"))
        capture_snapshot(self.second, b, ("/tmp/b", "2"))
        latest = self.root / "last"
        latest.symlink_to(self.second)
        self.assertEqual(snapshot_bindings(self.first), a)
        self.assertEqual(snapshot_bindings(latest), b)

    def test_dump_rewrite_cannot_use_older_bindings(self):
        capture_snapshot(self.first, {"0:1.1": {"sid": "old"}}, ("/tmp/a", "1"))
        self.first.write_text("changed layout\n")
        self.assertEqual(snapshot_bindings(self.first), {})

    def test_session_change_alone_changes_the_resurrect_snapshot(self):
        capture_snapshot(self.first, {"0:1.1": {"sid": "old"}}, ("/tmp/a", "1"))
        capture_snapshot(self.second, {"0:1.1": {"sid": "new"}}, ("/tmp/a", "1"))
        self.assertNotEqual(self.first.read_bytes(), self.second.read_bytes())
        moved = self.root / "copied.dump"
        moved.write_bytes(self.first.read_bytes())
        self.assertEqual(snapshot_bindings(moved), {"0:1.1": {"sid": "old"}})

    def test_layout_tampering_is_rejected(self):
        capture_snapshot(self.first, {}, ("/tmp/a", "1"))
        self.first.write_bytes(self.first.read_bytes().replace(b"same tmux", b"different tmux"))
        with self.assertRaisesRegex(SnapshotError, "does not match"):
            snapshot_bindings(self.first)

    def test_recapture_cannot_replace_identities_for_a_pinned_dump(self):
        bindings = {"0:1.1": {"sid": "old"}}
        capture_snapshot(self.first, bindings, ("/tmp/a", "1"))
        with self.assertRaisesRegex(SnapshotError, "save a new snapshot"):
            capture_snapshot(self.first, {"0:1.1": {"sid": "new"}}, ("/tmp/a", "1"))
        self.assertEqual(snapshot_bindings(self.first), bindings)

    def test_corrupt_paired_data_fails_without_using_global_latest(self):
        capture_snapshot(self.first, {}, ("/tmp/a", "1"))
        (self.root / "state").mkdir()
        (self.root / "state/coords-last.json").write_text(json.dumps({"0:1.1": {"sid": "wrong"}}))
        self.first.write_text("aipane_sessions\t{\n")
        with self.assertRaises(SnapshotError):
            snapshot_bindings(self.first)


if __name__ == "__main__":
    unittest.main()
