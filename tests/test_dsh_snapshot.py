"""A stalled dsh host keeps its last selected session across snapshot/restore."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))
from agent_activity import AgentActivity, TmuxActivityAdapter
from dsh_sessions import current_session, last_reported_session


@unittest.skipUnless(shutil.which("tmux"), "tmux required for process/TTY identity")
class DshSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name).resolve()
        self.dsh_home = self.home / ".dsh"
        self.state = self.home / "state"
        self.state.mkdir()
        self.env = {**os.environ, "HOME": str(self.home),
                    "DSH_HOME": str(self.dsh_home),
                    "DSH_TUI_SESSION_ROOT": str(self.home / "sessions"),
                    "AIPANE_STATE_DIR": str(self.state),
                    "AIPANE_REGISTRY": str(self.state / "registry.jsonl")}
        self.env.pop("TMUX", None)
        self.env.pop("TMUX_PANE", None)
        self.tmux = ["tmux", "-L", f"dsh-snapshot-{uuid.uuid4().hex}"]
        self.addCleanup(lambda: subprocess.run([*self.tmux, "kill-server"], env=self.env,
                                              capture_output=True, check=False))
        self.pid = int(subprocess.check_output(
            [*self.tmux, "-f", "/dev/null", "new-session", "-d", "-P", "-F", "#{pane_pid}", "sleep 60"],
            env=self.env, text=True,
        ).strip())
        started, tty = subprocess.check_output(
            ["ps", "-p", str(self.pid), "-o", "lstart=", "-o", "tty="],
            env={**os.environ, "LC_ALL": "C"}, text=True,
        ).strip().rsplit(None, 1)
        self.record = dict(version=1, instance="selected-channel", pid=self.pid,
                           process_started=started, updated_at=time.time() * 1000,
                           pane_id="%8", socket="/tmp/test-dsh", server_pid="321",
                           session_id="session-selected", cwd=str(self.home),
                           dsh_home=str(self.dsh_home), session_root=str(self.home / "sessions"))
        self.projection = ["%8", "node", "0:@1.%8", f"/dev/{tty}",
                           "/tmp/test-dsh", "321", str(self.pid), "", "", "", ""]
        self.fake_tmux = self.home / "tmux"
        self.fake_tmux.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, sys\n"
            "from pathlib import Path\n"
            "p = json.loads(Path(os.environ['FAKE_PROJECTION']).read_text())\n"
            "if sys.argv[1] == 'list-panes':\n"
            "    print('%8\\t0:1.0\\t' + os.environ['HOME'])\n"
            "elif sys.argv[1] == 'display-message':\n"
            "    print('\\x1f'.join(p) if 'pane_current_command' in sys.argv[-1] "
            "else '/tmp/test-dsh\\t321')\n"
            "else: raise SystemExit(99)\n"
        )
        self.fake_tmux.chmod(0o755)
        self.env.update(AIPANE_TMUX=str(self.fake_tmux), FAKE_PROJECTION=str(self.home / "projection.json"))
        (self.state / "registry.jsonl").write_text(json.dumps({
            "sock": "/tmp/test-dsh", "srv": "321", "pane": "%8",
            "tool": "d", "sid": "session-launch",
        }) + "\n")

    def snapshot(self):
        records = Path(self.record["dsh_home"]) / "aipane/sessions"
        records.mkdir(parents=True, exist_ok=True)
        path = records / f"{self.pid}.json"
        path.write_text(json.dumps(self.record))
        path.chmod(0o600)
        Path(self.env["FAKE_PROJECTION"]).write_text(json.dumps(self.projection))
        subprocess.run([sys.executable, str(ROOT / "bin/aipane-snapshot")],
                       env=self.env, check=True, capture_output=True, text=True)
        return json.loads((self.state / "coords-last.json").read_text())

    def stall(self):
        self.record["updated_at"] = time.time() * 1000 - 122_000

    def test_stale_heartbeat_preserves_selected_session_without_claiming_live_state(self):
        self.assertEqual(self.snapshot()["0:1.0"]["sid"], "session-selected")
        self.stall()
        saved = self.snapshot()["0:1.0"]
        self.assertEqual(saved["sid"], "session-selected")
        self.assertEqual(saved["binding"], "last_known")
        self.assertEqual(saved["last_confirmed_at"], self.record["updated_at"])
        self.assertEqual(saved["cwd"], str(self.home))
        self.assertEqual(saved["dsh_home"], str(self.dsh_home))
        self.assertEqual(saved["session_root"], str(self.home / "sessions"))
        self.assertIsNone(current_session(pane_id="%8", socket_path="/tmp/test-dsh",
                                         server_pid="321", environment=self.env))
        activity = AgentActivity(adapter=TmuxActivityAdapter(command=[str(self.fake_tmux)],
                                                           environment=self.env), environment=self.env)
        inspection = activity.inspect("%8")
        self.assertEqual(inspection.state, "unknown")
        self.assertIsNone(inspection.session)

    def test_stale_custom_home_is_discovered_without_a_fresh_activity_heartbeat(self):
        custom = self.home / "custom-dsh"
        self.record["dsh_home"] = str(custom)
        self.projection[-1] = json.dumps({"dsh_runtime": {
            "pid": self.pid, "instance": "stalled-host",
            "path": str(custom / f"aipane/activity/{self.pid}.json"),
        }})
        self.stall()
        saved = self.snapshot()["0:1.0"]
        self.assertEqual(saved["sid"], "session-selected")
        self.assertEqual(saved["dsh_home"], str(custom))

    def test_stale_channel_record_does_not_need_the_async_launch_registry(self):
        (self.state / "registry.jsonl").unlink()
        self.stall()
        self.assertEqual(self.snapshot()["0:1.0"]["sid"], "session-selected")

    def test_latest_selected_record_replaces_old_snapshot_even_when_stale(self):
        self.snapshot()
        self.record["session_id"] = "session-new"
        self.stall()
        self.assertEqual(self.snapshot()["0:1.0"]["sid"], "session-new")

    def test_stale_record_still_requires_exact_process_and_pane_identity(self):
        original = dict(self.record)
        for changes in ({"process_started": "old process"}, {"server_pid": "999"},
                        {"pane_id": "%9"}, {"socket": "/tmp/other"},
                        {"session_id": ""}, {"updated_at": time.time() * 1000 + 60_000}):
            with self.subTest(changes=changes):
                self.record = dict(original)
                self.snapshot()
                self.stall()
                self.record.update(changes)
                saved = self.snapshot()["0:1.0"]
                self.assertEqual(saved["sid"], "")
                self.assertEqual(saved["binding"], "unavailable")

    def test_stale_record_on_another_tty_cannot_authorize_resume(self):
        self.stall()
        self.projection[3] = "/dev/not-this-tty"
        self.assertEqual(self.snapshot()["0:1.0"]["binding"], "unavailable")

    def test_dead_process_record_cannot_authorize_resume(self):
        self.stall()
        subprocess.run([*self.tmux, "kill-server"], env=self.env, check=True)
        self.assertEqual(self.snapshot()["0:1.0"]["binding"], "unavailable")

    def test_shell_does_not_inherit_stale_dsh_identity(self):
        self.stall()
        self.projection[1] = "zsh"
        self.assertEqual(self.snapshot()["0:1.0"]["binding"], "unavailable")

    def test_background_host_cannot_supply_a_stale_selection_on_the_same_tty(self):
        self.stall()
        self.snapshot()
        tty = self.projection[3].removeprefix("/dev/")
        # The process and TTY still exist after job control puts another host
        # in front. ps no longer marks this old host as foreground (+).
        process = subprocess.CompletedProcess([], 0,
            stdout=f"{self.record['process_started']} {tty} S\n")
        with mock.patch("dsh_sessions.subprocess.run", return_value=process):
            self.assertIsNone(last_reported_session(
                pane_id="%8", socket_path="/tmp/test-dsh", server_pid="321",
                pane_tty=self.projection[3], environment=self.env,
            ))

    @unittest.skipUnless(shutil.which("dsh") and shutil.which("node"), "native dsh persistence required")
    def test_stalled_snapshot_reaches_native_validated_resume_plan(self):
        script = '''
import { createRequire } from 'node:module';
import { realpathSync } from 'node:fs';
import { pathToFileURL } from 'node:url';
const require = createRequire(realpathSync(process.argv[1]));
const load = async name => import(pathToFileURL(require.resolve(name)).href);
const { Context } = await load('@deepseek-ai/cordis');
const { default: Persistence } = await load('@deepseek-ai/dsh-session-persistence-jsonl');
const { SESSION_FORMAT_VERSION } = await load('@deepseek-ai/dsh-session');
const backend = new Persistence(new Context(), { root: process.env.DSH_TUI_SESSION_ROOT });
const handle = await backend.create({version:SESSION_FORMAT_VERSION,id:'session-selected',
    createdAt:Date.now(),cwd:process.cwd(),isSeeded:false,delegationDepth:0});
await handle.flush(); await handle.close();
'''
        subprocess.run(["node", "--input-type=module", "-e", script, shutil.which("dsh")],
                       cwd=self.home, env=self.env, check=True, capture_output=True, text=True)
        self.stall()
        self.snapshot()
        dump = self.home / "dump"
        dump.write_text(f"pane\t0\t1\t:window\t1\t0\t:title\t:{self.home}\t1\tnode\t:node /test/dsh-tui --resume session-launch\n")
        result = subprocess.run([str(ROOT / "bin/ai-restore"), "--plan-json", "--dump", str(dump)],
                                env=self.env, check=True, capture_output=True, text=True)
        plan = json.loads(result.stdout)
        self.assertEqual(plan["kind"], "resume")
        self.assertTrue(plan["restorable"])
        self.assertEqual(plan["sid"], "session-selected")
        self.assertEqual(plan["dsh_home"], str(self.dsh_home))
        self.assertEqual(plan["session_root"], str(self.home / "sessions"))


if __name__ == "__main__":
    unittest.main()
