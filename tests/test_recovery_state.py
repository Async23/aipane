"""State transitions preserve intent across pane moves, retries and migration."""
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
from recovery_state import PendingStore


class RecoveryStateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.file = self.root / "pending.json"
        self.store = PendingStore(self.file, 0, ("/tmp/server", "123"))
        self.item = dict(target="0:1.1", tool="codex", sid="conversation", kind="resume",
                         restorable=True, cwd=str(self.root), command="codex resume conversation",
                         codex_home=str(self.root / "codex"), pane_id="%1",
                         socket_path="/tmp/server", server_pid="123")

    def save_intent(self, item=None, **options):
        merged = self.store.merge([item or self.item], dump="dump", now=time.time(), **options)
        self.store.save(merged)
        return self.store.load(time.time())

    def test_move_preserves_record_identity_and_attempt_history(self):
        first = self.save_intent()[0]
        first["attempts"] = 4
        self.store.save([first])
        moved = self.save_intent({**self.item, "target": "0:9.3"})[0]
        self.assertEqual(moved["recovery_id"], first["recovery_id"])
        self.assertEqual((moved["target"], moved["attempts"]), ("0:9.3", 4))
        self.assertEqual(json.loads(self.file.read_text())["history"], [])

    def test_coordinate_reuse_cannot_take_over_pending_intent(self):
        first = self.save_intent()[0]
        remaining = self.save_intent({**self.item, "pane_id": "%99"})
        self.assertEqual(remaining[0]["recovery_id"], first["recovery_id"])
        self.assertEqual([item["pane_id"] for item in remaining], ["%1"])

    def test_confirmed_replacement_archives_previous_session(self):
        first = self.save_intent()[0]
        new = self.save_intent({**self.item, "sid": "new", "command": "codex resume new"}, preserve_identity=False)[0]
        self.assertNotEqual(first["recovery_id"], new["recovery_id"])
        history = json.loads(self.file.read_text())["history"]
        self.assertEqual([(item["sid"], item["resolution"]) for item in history], [("conversation", "superseded")])

    def test_invalid_new_snapshot_does_not_erase_pending_intent(self):
        first = self.save_intent()[0]
        remaining = self.save_intent({**self.item, "sid": "", "kind": "invalid", "restorable": False})
        self.assertEqual(remaining, [first])

    def test_migration_and_discard_preserve_foreign_intent_and_audit(self):
        local = {**self.item, "created_at": time.time()}
        foreign = {**local, "server_pid": "456"}
        self.file.write_text(json.dumps({"version": 1, "items": [local, foreign]}))
        before = self.file.read_bytes()
        identifier = self.store.load(time.time())[0]["recovery_id"]
        self.assertEqual(self.file.read_bytes(), before)
        self.store.discard(identifier)
        payload = json.loads(self.file.read_text())
        self.assertEqual(payload["items"], [foreign])
        self.assertEqual(payload["identity_version"], 2)
        self.assertEqual(payload["history"][0]["resolution"], "discarded")
        self.assertEqual(payload["history"][0]["sid"], "conversation")


if __name__ == "__main__":
    unittest.main()
