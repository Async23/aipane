"""A missed dsh report must recover from its live, independently written state."""

import json
import io
import runpy
import sys
import tempfile
import time
import unittest
from unittest import mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
from agent_activity import AgentActivity, InMemoryActivityAdapter, PaneActivity


class DshRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "54321.json"
        self.reference = {"pid": 54321, "instance": "live-host", "path": str(self.path)}
        self.sequence = 0
        self.pane = PaneActivity(pane_id="%7", current_command="node", pane_tty="/dev/ttys7",
                                 socket_path="/tmp/dsh.sock", server_pid="42", pane_pid="123")
        self.adapter = InMemoryActivityAdapter([self.pane])
        self.activity = AgentActivity(
            adapter=self.adapter,
            process_identity=lambda pane: {"pid": 54321, "started_at": "same-process"},
            process_matches=lambda process, pane: process.get("pid") == 54321,
        )

    def runtime(self, state, **overrides):
        self.sequence += 1
        value = {"version": 1, "instance": "live-host", "pid": 54321,
                 "pane_id": "%7", "socket": "/tmp/dsh.sock", "server_pid": "42",
                 "sequence": self.sequence, "updated_at": time.time_ns() // 1_000_000,
                 "state": state, "agents": [{"id": "root", "status": "running" if state == "busy" else "idle"}],
                 **overrides}
        self.path.write_text(json.dumps(value))
        return value

    def report(self, state):
        self.runtime(state)
        return self.activity.report("%7", state, {"dsh_runtime": self.reference})

    def test_missed_idle_is_read_only_until_two_stable_repair_probes(self):
        self.report("busy")
        self.runtime("idle")
        inspection = self.activity.inspect("%7")
        self.assertEqual(inspection.state, "idle")
        self.assertTrue(inspection.repairable)
        self.assertEqual(self.adapter.read("%7").marker, "node")
        self.assertFalse(self.activity.reconcile().changed)
        self.assertTrue(self.activity.reconcile().changed)
        self.assertEqual(self.adapter.read("%7").marker, "")

    def test_missed_busy_is_also_repaired(self):
        self.report("idle")
        self.runtime("busy")
        self.assertEqual(self.activity.inspect("%7").state, "busy")
        self.assertFalse(self.activity.reconcile().changed)
        self.assertTrue(self.activity.reconcile().changed)
        self.assertEqual(self.adapter.read("%7").marker, "node")

    def test_stale_or_foreign_evidence_is_unknown_and_never_clears_busy(self):
        cases = [
            {"instance": "previous-plugin"}, {"pid": 12}, {"pane_id": "%8"},
            {"socket": "/tmp/other.sock"}, {"server_pid": "99"},
            {"updated_at": 1}, {"sequence": 0},
            {"agents": [{"status": "running"}]},
        ]
        for changes in cases:
            with self.subTest(changes=changes):
                self.report("busy")
                self.runtime("idle", **changes)
                self.assertEqual(self.activity.inspect("%7").state, "unknown")
                self.activity.reconcile()
                self.assertFalse(self.activity.reconcile().changed)
                self.assertEqual(self.adapter.read("%7").marker, "node")

    def test_newer_running_turn_cancels_pending_idle_repair(self):
        self.report("busy")
        self.runtime("idle")
        self.assertFalse(self.activity.reconcile().changed)
        self.runtime("busy")
        self.assertFalse(self.activity.reconcile().changed)
        self.assertEqual(self.activity.inspect("%7").state, "busy")
        self.assertEqual(self.adapter.read("%7").marker, "node")

    def test_child_running_prevents_idle_and_retired_host_is_unknown(self):
        self.report("busy")
        self.runtime("busy", agents=[{"id": "root", "status": "idle"},
                                     {"id": "child", "status": "running"}])
        self.assertEqual(self.activity.inspect("%7").state, "busy")
        self.path.unlink()
        self.assertEqual(self.activity.inspect("%7").state, "unknown")

    def test_payload_cannot_bind_another_process_runtime(self):
        self.runtime("busy")
        result = self.activity.report("%7", "busy", {"dsh_runtime": {**self.reference, "pid": 12}})
        self.assertFalse(result.accepted)
        self.assertEqual(self.adapter.read("%7").marker, "")

    def test_malformed_reference_does_not_crash_inspection(self):
        self.report("busy")
        record = json.loads(self.adapter.read("%7").record)
        record["dsh_runtime"] = []
        self.adapter.set_option("%7", "@tmux-window-wrap-activity-record", json.dumps(record))
        self.assertEqual(self.activity.inspect("%7").state, "unknown")

    def test_cli_acknowledges_rejected_report_as_retryable_failure(self):
        module = runpy.run_path(str(Path(__file__).resolve().parents[1] / "bin/aipane-activity"))
        main = module["main"]
        main.__globals__["controller"] = lambda socket: self.activity
        self.runtime("busy")
        for reference, expected in (({**self.reference, "pid": 12}, 3), (self.reference, 0)):
            with mock.patch.object(sys, "argv", ["aipane-activity", "report", "busy", "--pane", "%7"]), \
                 mock.patch.object(sys, "stdin", io.StringIO(json.dumps({"dsh_runtime": reference}))):
                self.assertEqual(main(), expected)


if __name__ == "__main__":
    unittest.main()
