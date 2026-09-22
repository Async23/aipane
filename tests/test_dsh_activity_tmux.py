"""Exercise the dsh Adapter through the real activity CLI and tmux format."""

import json
import os
import shlex
import shutil
import subprocess
import tempfile
import time
import unittest
import uuid
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which("node") and shutil.which("tmux"), "node and tmux required")
class DshActivityTmuxTests(unittest.TestCase):
    def test_dsh_lifecycle_drives_the_statusline_projection(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            local_bin = home / ".local" / "bin"
            local_bin.mkdir(parents=True)
            (local_bin / "aipane-activity").symlink_to(ROOT / "bin/aipane-activity")
            adapter_url = (ROOT / "integrations/dsh/aipane-activity.mjs").as_uri()
            host = home / "host.mjs"
            host.write_text(
                "import { createInterface } from 'node:readline'\n"
                f"import {{ apply }} from {json.dumps(adapter_url)}\n"
                "const handlers = new Map()\n"
                "const agents = [{ status: 'idle' }, { status: 'idle' }]\n"
                "let dispose\n"
                "apply({\n"
                "  agents: { list: () => agents },\n"
                "  on: (name, handler) => handlers.set(name, handler),\n"
                "  effect: (setup) => { dispose = setup() },\n"
                "})\n"
                "createInterface({ input: process.stdin }).on('line', async (line) => {\n"
                "  if (line === 'dispose') { await dispose(); return }\n"
                "  const [index, status] = line.split(' ')\n"
                "  const agent = agents[Number(index)]\n"
                "  agent.status = status\n"
                "  handlers.get('agent/status')({ agent, status })\n"
                "})\n",
                encoding="utf-8",
            )
            socket_name = f"dsh-activity-{uuid.uuid4().hex}"
            environment = {
                key: value for key, value in os.environ.items()
                if key not in {"TMUX", "TMUX_PANE", "AIPANE_TMUX"}
            }
            environment["HOME"] = str(home)
            tmux = ["tmux", "-L", socket_name]

            def run(*args):
                return subprocess.run(
                    [*tmux, *args], env=environment, check=True,
                    capture_output=True, text=True,
                ).stdout.strip()

            def send(line):
                run("send-keys", "-t", "%0", "-l", line)
                run("send-keys", "-t", "%0", "Enter")

            def wait_for(state):
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    result = subprocess.run(
                        [str(ROOT / "bin/aipane-activity"), "inspect", "--json",
                         "--pane", "%0", "--socket-name", socket_name],
                        env=environment, capture_output=True, text=True, check=True,
                    )
                    activity = json.loads(result.stdout)
                    if activity["state"] == state:
                        self.assertTrue(activity["reported"])
                        return
                    time.sleep(0.025)
                self.fail(f"Expected {state}; last activity: {activity}")

            def breathing():
                # The exact scalar condition used by conf/tmux-window-wrap.conf.
                return run(
                    "display-message", "-p", "-t", "%0",
                    "#{&&:#{m/r:^.+$,#{@tmux-window-wrap-activity}},"
                    "#{==:#{@tmux-window-wrap-activity},#{pane_current_command}}}",
                )

            try:
                run("-f", "/dev/null", "new-session", "-d",
                    shlex.join([shutil.which("node"), str(host)]))
                wait_for("idle")
                self.assertEqual(breathing(), "0")
                send("0 running")
                wait_for("busy")
                self.assertEqual(breathing(), "1")
                send("1 running")
                send("0 idle")
                send("1 idle")
                wait_for("idle")
                self.assertEqual(breathing(), "0")
                send("1 running")
                wait_for("busy")
                send("dispose")
                wait_for("idle")
                self.assertEqual(breathing(), "0")
            finally:
                subprocess.run([*tmux, "kill-server"], env=environment,
                               capture_output=True, check=False)


if __name__ == "__main__":
    unittest.main()
