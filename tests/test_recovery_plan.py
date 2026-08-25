"""Tests for immutable, digest-protected restart plans."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LIB = ROOT / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

from recovery_plan import RecoveryPlanError, load_sealed_plan, seal_plan


class RecoveryPlanTests(unittest.TestCase):
    def test_plan_pins_resolved_dump_and_round_trips_exact_items(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dump = root / "dump.txt"
            dump.write_text("snapshot\n", encoding="utf-8")
            latest = root / "last"
            latest.symlink_to(dump.name)
            plan_path = root / "restart-plan.json"
            items = [
                {
                    "target": "0:1.0",
                    "tool": "codex",
                    "kind": "resume",
                    "restorable": True,
                    "cwd": str(root),
                    "command": "codex resume thread-1",
                    "sid": "thread-1",
                    "guard": {
                        "pane_id": "%4",
                        "socket_path": "/tmp/tmux-test",
                        "server_pid": "42",
                        "pane_pid": "84",
                        "current_command": "codex",
                        "activity_marker": "codex",
                        "activity_reporter": "codex",
                        "activity_updated_at": "123",
                        "activity_record": '{"version":1}',
                    },
                }
            ]

            sealed = seal_plan(plan_path, latest, items, plan_id="fixed-plan")
            loaded = load_sealed_plan(plan_path, expected_dump=dump)

            self.assertEqual(sealed, loaded)
            self.assertEqual(loaded["dump"], str(dump.resolve()))
            self.assertEqual(loaded["items"], items)
            self.assertEqual(loaded["plan_id"], "fixed-plan")

    def test_plan_digest_rejects_post_confirmation_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dump = root / "dump.txt"
            dump.touch()
            plan_path = root / "restart-plan.json"
            seal_plan(
                plan_path,
                dump,
                [
                    {
                        "target": "0:1.0",
                        "tool": "claude",
                        "kind": "resume",
                        "restorable": True,
                        "cwd": str(root),
                        "command": "claude --resume original",
                        "sid": "original",
                        "guard": {
                            "pane_id": "%4",
                            "socket_path": "/tmp/tmux-test",
                            "server_pid": "42",
                            "pane_pid": "84",
                            "current_command": "claude",
                            "activity_marker": "",
                            "activity_reporter": "",
                            "activity_updated_at": "",
                            "activity_record": "",
                        },
                    }
                ],
            )
            payload = json.loads(plan_path.read_text(encoding="utf-8"))
            payload["items"][0]["command"] = "claude --resume replacement"
            plan_path.write_text(json.dumps(payload), encoding="utf-8")

            with self.assertRaisesRegex(RecoveryPlanError, "digest"):
                load_sealed_plan(plan_path)


if __name__ == "__main__":
    unittest.main()
