"""Qwen recovery contracts using isolated tmux and a local Node fixture.

The fixture only stays alive; it never invokes Qwen, a model or the network.
Native metadata is written explicitly to exercise session swaps and failures.
"""

import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))
import qwen_sessions as qwen

SID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
OLD_SID = "11111111-2222-4333-8444-555555555555"

loader = importlib.machinery.SourceFileLoader("qwen_test_executor", str(ROOT / "bin/aipane-restore-executor"))
spec = importlib.util.spec_from_loader(loader.name, loader)
executor = importlib.util.module_from_spec(spec)
sys.modules[loader.name] = executor
loader.exec_module(executor)


class QwenCommandTests(unittest.TestCase):
    def test_script_position_is_required(self):
        for command in ("qwen --yolo", "node /usr/local/bin/qwen --model custom",
                        "node /pkg/@qwen-code/qwen-code/cli-entry.js"):
            self.assertTrue(qwen.is_qwen_command_line(command), command)
        for command in ("node app.js --prompt qwen", "node --eval qwen",
                        "bash -c qwen", "node --require qwen app.js"):
            self.assertFalse(qwen.is_qwen_command_line(command), command)


@unittest.skipUnless(shutil.which("tmux") and shutil.which("node"), "tmux and Node required")
class QwenSessionTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.home = self.root / "qwen-home"
        self.runtime = self.root / "runtime"
        self.state = self.root / "state"
        self.state.mkdir()
        self.env = {**os.environ, "HOME": str(self.root),
                    "QWEN_HOME": str(self.home), "QWEN_RUNTIME_DIR": str(self.runtime),
                    "AIPANE_STATE_DIR": str(self.state),
                    "AIPANE_REGISTRY": str(self.state / "registry.jsonl"),
                    "AI_RESTORE_VERIFY_STABILITY": "0.01",
                    "AI_RESTORE_POLL_INTERVAL": "0.01"}
        self.env.pop("TMUX", None)
        self.env.pop("TMUX_PANE", None)
        self.tmux = ["tmux", "-L", "qwen-test-" + uuid.uuid4().hex]
        self.addCleanup(lambda: subprocess.run(
            [*self.tmux, "kill-server"], env=self.env, capture_output=True,
        ))
        script = self.root / "qwen"
        script.write_text("setInterval(() => {}, 1000)\n")
        self.env["AIPANE_TMUX"] = shlex.join(self.tmux)
        self.call_tmux("-f", "/dev/null", "new-session", "-d", "-s", "test",
                       "-c", str(self.root),
                       f"exec node {shlex.quote(str(script))} --session-id {OLD_SID}")
        info = self.call_tmux("display-message", "-p",
                              "#{pane_id}\t#{pane_tty}\t#{socket_path}\t#{pid}")
        self.pane, self.tty, sock, server = info.strip().split("\t")
        # Wait only for the fixture's exec, not an arbitrary fixed delay.
        deadline = time.monotonic() + 3
        while not (processes := qwen.foreground_processes(self.tty)):
            if time.monotonic() >= deadline:
                self.fail("Node fixture did not become foreground")
            time.sleep(0.01)
        self.process = processes[0]
        self.pid = self.process["pid"]
        self.registry = self.home / "sessions" / f"{self.pid}.json"
        self.registry.parent.mkdir(parents=True)
        self.record = dict(schemaVersion=1, pid=self.pid, procStart=None, pidNs=None,
                           sessionId=SID, cwd=str(self.root), kind="tui",
                           startedAt=time.time() * 1000)
        if sys.platform == "linux":
            fields = Path(f"/proc/{self.pid}/stat").read_text().rsplit(")", 1)[1].split()
            boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
            self.record.update(procStart=f"{boot}:{fields[19]}",
                               pidNs=Path(f"/proc/{self.pid}/ns/pid").stat().st_ino)
        self.write_registry()
        self.chats = qwen.chats_dir(str(self.root), self.env)
        self.chats.mkdir(parents=True)
        self.write_transcript(SID)
        self.write_transcript(OLD_SID)
        self.sidecar = self.chats / f"{SID}.runtime.json"
        self.runtime_record = dict(schema_version=1, pid=self.pid, session_id=SID,
                                   work_dir=str(self.root), hostname=socket.gethostname(),
                                   started_at=time.time())
        self.sidecar.write_text(json.dumps(self.runtime_record))
        self.launch_registry = self.state / "registry.jsonl"
        self.launch_registry.write_text(json.dumps(
            dict(sock=sock, srv=server, pane=self.pane, sid=OLD_SID, tool="q"),
        ) + "\n")
        self.coord = self.call_tmux("display-message", "-p",
                                   "#{session_name}:#{window_index}.#{pane_index}").strip()
        self.plan = dict(target=self.coord, tool="qwen", sid=SID, cwd=str(self.root),
                         command=f"qwen --yolo --resume {SID}", kind="resume",
                         restorable=True, qwen_home=str(self.home),
                         qwen_runtime_dir=str(self.runtime))

    def call_tmux(self, *args):
        return subprocess.check_output([*self.tmux, *args], env=self.env, text=True)

    def write_registry(self, **changes):
        self.registry.write_text(json.dumps({**self.record, **changes}))
        self.registry.chmod(0o600)

    def write_transcript(self, sid):
        path = self.chats / f"{sid}.jsonl"
        path.write_text(json.dumps(dict(
            uuid="message", parentUuid=None, sessionId=sid, cwd=str(self.root),
            type="user", message={"role": "user", "parts": [{"text": "saved"}]},
        )) + "\n")
        return path

    def current(self):
        return qwen.current_session(pane_tty=self.tty, environment=self.env)

    def loaded(self):
        return qwen.session_loaded(pane_tty=self.tty, sid=SID,
                                   cwd=str(self.root), environment=self.env)

    def snapshot(self):
        subprocess.run([sys.executable, str(ROOT / "bin/aipane-snapshot")],
                       env=self.env, capture_output=True, check=True)
        return json.loads((self.state / "coords-last.json").read_text())[self.coord]

    def restore_plan(self):
        session, window_pane = self.coord.split(":")
        window, pane = window_pane.split(".")
        dump = self.root / "dump"
        dump.write_text("\t".join((
            "pane", session, window, "0", ":-", pane, "qwen", ":" + str(self.root),
            "1", "node", f":node qwen --session-id {OLD_SID}",
        )) + "\n")
        result = subprocess.check_output(
            ["zsh", str(ROOT / "bin/ai-restore"), "--plan-json", "--dump", str(dump)],
            env=self.env, text=True,
        )
        return json.loads(result)

    def test_snapshot_and_plan_use_current_session_and_namespace(self):
        saved = self.snapshot()
        self.assertEqual(saved["sid"], SID)
        self.assertEqual(saved["qwen_home"], str(self.home))
        self.assertEqual(saved["qwen_runtime_dir"], str(self.runtime))
        plan = self.restore_plan()
        self.assertTrue(plan["restorable"])
        self.assertEqual(plan["sid"], SID)
        self.assertIn(f"--resume {SID}", plan["command"])
        self.assertIn(f"QWEN_RUNTIME_DIR={self.runtime}", plan["command"])

    def test_native_session_does_not_need_a_launch_binding_or_hook(self):
        self.launch_registry.unlink()
        self.assertEqual(self.snapshot()["sid"], SID)

    def test_switched_session_without_transcript_never_resumes_old_session(self):
        (self.chats / f"{SID}.jsonl").unlink()
        self.assertEqual(self.snapshot()["sid"], SID)
        plan = self.restore_plan()
        self.assertEqual(plan["sid"], SID)
        self.assertFalse(plan["restorable"])

    def test_durable_preflight_rejects_metadata_only_and_malformed_messages(self):
        path = self.chats / f"{SID}.jsonl"
        valid = json.loads(path.read_text())
        for changes in (
            {"type": "system", "subtype": "session_sources_snapshot", "message": None},
            {"message": {"role": "user", "parts": [None]}},
            {"uuid": ""},
            {"sessionId": OLD_SID},
        ):
            with self.subTest(changes=changes):
                path.write_text(json.dumps({**valid, **changes}) + "\n")
                self.assertFalse(qwen.session_exists(SID, str(self.root), self.env))

    def test_missing_native_record_blocks_old_launch_even_without_binding(self):
        self.registry.unlink()
        for bound in (True, False):
            with self.subTest(bound=bound):
                if not bound:
                    self.launch_registry.unlink()
                self.assertEqual(self.snapshot()["binding"], "unavailable")
                self.assertFalse(self.restore_plan()["restorable"])

    def test_stale_foreign_unsafe_and_ambiguous_registry_records_are_rejected(self):
        self.assertEqual(self.current()["session_id"], SID)
        for changes in ({"pid": self.pid + 1}, {"schemaVersion": 2},
                        {"schemaVersion": True}, {"kind": "acp"},
                        {"startedAt": (self.process["started_at"] - 1) * 1000},
                        {"startedAt": float("nan")}, {"cwd": "relative"},
                        {"sessionId": "../../escape"}):
            with self.subTest(changes=changes):
                self.write_registry(**changes)
                self.assertIsNone(self.current())
        self.write_registry()
        self.registry.chmod(0o666)
        self.assertIsNone(self.current())
        self.registry.chmod(0o600)
        with patch.object(qwen, "foreground_processes", return_value=[self.process, self.process]):
            self.assertIsNone(self.current())
        self.assertIsNone(qwen.current_session(pane_tty="/dev/absent", environment=self.env))

    def test_readiness_requires_matching_runtime_sidecar(self):
        self.assertTrue(self.loaded())
        for changes in ({"pid": self.pid + 1}, {"session_id": OLD_SID},
                        {"work_dir": "/foreign"}, {"hostname": None},
                        {"hostname": ""}, {"hostname": 123},
                        {"started_at": self.process["started_at"] - 1},
                        {"schema_version": 2}):
            with self.subTest(changes=changes):
                self.sidecar.write_text(json.dumps({**self.runtime_record, **changes}))
                self.assertFalse(self.loaded())
        self.sidecar.unlink()
        self.assertFalse(self.loaded())

    def test_restart_and_recovery_accept_hostname_changes_for_same_live_session(self):
        runner = executor.RestoreExecutor(self.env)
        planned = {**self.plan, "guard": runner.tmux.recovery_guard(self.coord)}
        for saved_hostname in ("Alfheim.local", "previous-computer-name"):
            with self.subTest(saved_hostname=saved_hostname):
                self.sidecar.write_text(json.dumps({
                    **self.runtime_record, "hostname": saved_hostname,
                }))
                with patch.object(socket, "gethostname", return_value="Alfheim"):
                    runner.assert_exact_guards([planned])
                    self.assertTrue(runner.existing_tool_matches(self.plan, "node"))
                    attempt = executor.Attempt(self.plan, 0.5, None, True, False)
                    verified, failures, blocked = runner.verify_round([attempt])
                self.assertEqual(verified, [self.plan])
                self.assertEqual((failures, blocked), ({}, {}))

    def test_restart_reports_unverifiable_runtime_without_claiming_session_changed(self):
        runner = executor.RestoreExecutor(self.env)
        planned = {**self.plan, "guard": runner.tmux.recovery_guard(self.coord)}
        self.sidecar.unlink()
        with self.assertRaisesRegex(executor.RestoreExecutionError,
                                    "Qwen runtime identity could not be verified"):
            runner.assert_exact_guards([planned])

    def test_executor_verifies_current_session_but_not_old_argv(self):
        runner = executor.RestoreExecutor(self.env)
        self.assertTrue(runner.existing_tool_matches(self.plan, "node"))
        self.assertFalse(runner.existing_tool_matches({**self.plan, "sid": OLD_SID}, "node"))
        attempt = executor.Attempt(self.plan, 0.5, None, True, False)
        verified, failures, blocked = runner.verify_round([attempt])
        self.assertEqual(verified, [self.plan])
        self.assertEqual((failures, blocked), ({}, {}))
        self.write_registry(sessionId=OLD_SID)
        attempt = executor.Attempt(self.plan, 0.05, None, True, False)
        verified, failures, blocked = runner.verify_round([attempt])
        self.assertEqual(verified, [])
        self.assertIn(self.coord, blocked)

    def test_explicit_fresh_launch_still_requires_native_runtime_identity(self):
        runner = executor.RestoreExecutor(self.env)
        fresh = {**self.plan, "sid": "", "kind": "fresh"}
        self.assertTrue(runner.qwen_ready(fresh))
        self.assertFalse(runner.qwen_ready({**fresh, "kind": "resume"}))
        self.sidecar.unlink()
        self.assertFalse(runner.qwen_ready(fresh))

    def test_restart_rechecks_current_session_before_replacing_process(self):
        runner = executor.RestoreExecutor(self.env)
        with patch.object(runner.tmux, "guard_matches", return_value=True):
            runner.assert_exact_guards([{**self.plan, "guard": {}}])
            self.write_registry(sessionId=OLD_SID)
            with self.assertRaisesRegex(executor.RestoreExecutionError,
                                        "Qwen runtime identity could not be verified"):
                runner.assert_exact_guards([{**self.plan, "guard": {}}])


if __name__ == "__main__":
    unittest.main()
