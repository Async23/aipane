"""Exact live dsh identity and native durable resume validation."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))
from dsh_sessions import current_session


class DshLiveSessionTests(unittest.TestCase):
    def test_live_identity_rejects_stale_process_and_wrong_server(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            records = home / "aipane/sessions"
            records.mkdir(parents=True)
            started = subprocess.check_output(["ps", "-p", str(os.getpid()), "-o", "lstart="], text=True).strip()
            record = dict(version=1, pid=os.getpid(), process_started=started,
                          updated_at=time.time() * 1000, pane_id="%8", socket="/tmp/test-dsh",
                          server_pid="321", session_id="session-current", cwd=str(home))
            path = records / f"{os.getpid()}.json"
            def read(**changes):
                path.write_text(json.dumps({**record, **changes}))
                return current_session(pane_id="%8", socket_path="/tmp/test-dsh",
                                       server_pid="321", environment={"DSH_HOME": directory})
            self.assertEqual(read()["session_id"], "session-current")
            self.assertIsNone(read(server_pid="999"))
            self.assertIsNone(read(process_started="previous process"))
            self.assertIsNone(read(updated_at=time.time() * 1000 - 11000))
            self.assertIsNone(read(session_id=""))


@unittest.skipUnless(shutil.which("dsh") and shutil.which("node"), "installed dsh native persistence required")
class DshDurableSessionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name).resolve()
        self.state = self.home / "state"
        self.state.mkdir()
        self.env = {**os.environ, "AIPANE_STATE_DIR": str(self.state),
                    "DSH_TUI_SESSION_ROOT": str(self.home / "sessions")}
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
for (const id of ['session-launch', 'session-selected']) {
 const handle = await backend.create({version:SESSION_FORMAT_VERSION,id,createdAt:Date.now(),cwd:process.cwd(),isSeeded:false,delegationDepth:0});
 await handle.flush(); await handle.close();
}
'''
        subprocess.run(["node", "--input-type=module", "-e", script, shutil.which("dsh")],
                       cwd=self.home, env=self.env, check=True, capture_output=True, text=True)

    def plan(self, sid, full="node /Users/test/.local/bin/dsh-tui --resume session-launch", **env):
        (self.state / "coords-last.json").write_text(json.dumps({"0:1.0": {"sid": sid, "tool": "d"}} if sid is not None else {}))
        dump = self.home / "dump"
        dump.write_text(f"pane\t0\t1\t:window\t1\t0\t:title\t:{self.home}\t1\tnode\t:{full}\n")
        result = subprocess.run([str(ROOT / "bin/ai-restore"), "--plan-json", "--dump", str(dump)],
                                env={**self.env, **env}, text=True, capture_output=True, check=True)
        return json.loads(result.stdout) if result.stdout.strip() else None

    def test_current_selected_binding_supersedes_stale_launch_and_resumes_opaque_id(self):
        plan = self.plan("session-selected")
        self.assertEqual(plan["command"], "dsh-tui --resume session-selected")
        self.assertEqual(plan["sid"], "session-selected")
        self.assertEqual(plan["kind"], "resume")
        self.assertTrue(plan["restorable"])

    def test_resurrect_outer_node_launchers_and_native_worker_are_detected(self):
        for full in ("node /Users/test/.local/bin/dsh-tui",
                     "node /Users/test/.local/bin/dsh --profile dsh-tui",
                     "node /installed/@deepseek-ai/dsh/lib/worker.js"):
            with self.subTest(command=full):
                self.assertEqual(self.plan("session-selected", full)["kind"], "resume")

    def test_unrelated_node_arguments_cannot_impersonate_dsh_script(self):
        for full in ("node /tmp/other.js /Users/test/.local/bin/dsh-tui",
                     "node /tmp/other.js /installed/@deepseek-ai/dsh/lib/worker.js",
                     "node -e dsh", "node --eval dsh", "node -r dsh other.js"):
            with self.subTest(command=full):
                self.assertIsNone(self.plan("session-selected", full))

    def test_unpersisted_current_session_never_falls_back_to_saved_launch(self):
        plan = self.plan("session-unsaved")
        self.assertEqual(plan["sid"], "session-unsaved")
        self.assertEqual(plan["kind"], "invalid")
        self.assertFalse(plan["restorable"])

    def test_exact_resume_argv_works_but_global_continue_does_not(self):
        self.assertEqual(self.plan(None, "dsh-tui --resume=session-selected")["kind"], "resume")
        plan = self.plan(None, "dsh-tui --continue")
        self.assertEqual(plan["kind"], "fresh")
        self.assertFalse(plan["restorable"])

    def test_unavailable_current_binding_blocks_stale_resume_argv(self):
        plan = self.plan("")
        self.assertEqual(plan["kind"], "invalid")
        self.assertFalse(plan["restorable"])

    def test_native_validator_rejects_wrong_cwd_and_corrupt_highest_generation(self):
        command = [str(ROOT / "bin/aipane-dsh-session"), "session-selected"]
        self.assertEqual(subprocess.run([*command, str(ROOT)], env=self.env).returncode, 1)
        files = list((self.home / "sessions").glob("*/session-selected/session.v*.jsonl.zstd"))
        self.assertEqual(len(files), 1)
        files[0].write_bytes(b"corrupt generation")
        self.assertEqual(subprocess.run([*command, str(self.home)], env=self.env).returncode, 1)


if __name__ == "__main__":
    unittest.main()
