"""Exercise Codex's shared-server selection with real, local-only processes."""

from __future__ import annotations

import json
import os
from pathlib import Path
import select
import shlex
import shutil
import subprocess
import tempfile
import time
import unittest
import uuid


ROOT = Path(__file__).resolve().parents[1]
CODEX = shutil.which("codex")
TMUX = shutil.which("tmux")


def rpc(process, messages):
    """Read only the requested replies, with a deadline even on startup failure."""
    pending = {message["id"] for message in messages if "id" in message}
    for message in messages:
        process.stdin.write((json.dumps(message) + "\n").encode())
    process.stdin.flush()
    replies, buffer = {}, b""
    deadline = time.monotonic() + 15
    while pending and time.monotonic() < deadline:
        if not select.select([process.stdout], [], [], 0.1)[0]:
            continue
        chunk = os.read(process.stdout.fileno(), 65536)
        if not chunk:
            break
        buffer += chunk
        while b"\n" in buffer:
            line, buffer = buffer.split(b"\n", 1)
            value = json.loads(line)
            if value.get("id") in pending:
                pending.remove(value["id"])
                replies[value["id"]] = value
    if pending:
        raise AssertionError(f"Codex did not answer requests {pending}")
    return replies


def stop(process):
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)
    for stream in (process.stdin, process.stdout, process.stderr):
        if stream is not None:
            stream.close()


@unittest.skipUnless(CODEX and TMUX, "requires Codex CLI and tmux")
class CodexTmuxLaunchTests(unittest.TestCase):
    def test_default_launcher_reports_to_its_pane_despite_existing_daemon(self):
        # The daemon deliberately inherits a nonexistent pane. A TUI that
        # reuses it sends lifecycle hooks to that stale pane, reproducing the
        # missing activity cell after a tmux restart.
        with tempfile.TemporaryDirectory(prefix="aipane-codex-launch-") as tmp:
            directory = Path(tmp)
            codex_home = directory / "codex"
            codex_home.mkdir()
            trace = directory / "hook.json"
            hook = directory / "hook.py"
            hook.write_text(
                "import json, os, subprocess, sys\n"
                "payload = sys.stdin.buffer.read()\n"
                f"result = subprocess.run([{str(ROOT / 'bin/aipane-activity')!r}, "
                "'report', 'busy'], input=payload, capture_output=True)\n"
                f"with open({str(trace) + '.tmp'!r}, 'w') as stream:\n"
                " json.dump({'pane': os.environ.get('TMUX_PANE'), "
                "'returncode': result.returncode, "
                "'stderr': result.stderr.decode()}, stream)\n"
                f"os.replace({str(trace) + '.tmp'!r}, {str(trace)!r})\n"
                "sys.exit(2)\n",  # Stop before any model request.
                encoding="utf-8",
            )
            (codex_home / "hooks.json").write_text(
                json.dumps({"hooks": {"UserPromptSubmit": [{"hooks": [{
                    "type": "command",
                    "command": shlex.join([shutil.which("python3"), str(hook)]),
                }]}]}}),
                encoding="utf-8",
            )
            config = codex_home / "config.toml"
            config.write_text(
                'model="mock"\nmodel_provider="probe"\n'
                'check_for_update_on_startup=false\n'
                '[features]\napps=false\nplugins=false\n'
                '[model_providers.probe]\nname="probe"\n'
                'base_url="http://127.0.0.1:1"\nwire_api="responses"\n'
                'request_max_retries=0\nstream_max_retries=0\n',
                encoding="utf-8",
            )
            environment = {
                key: value for key, value in os.environ.items()
                if key not in {"TMUX", "TMUX_PANE", "CODEX_THREAD_ID",
                               "AIPANE_CODEX_LAUNCH_CMD"}
            }
            environment["CODEX_HOME"] = str(codex_home)
            initialization = [
                {"id": 1, "method": "initialize", "params": {
                    "clientInfo": {"name": "aipane-test", "version": "1"},
                    "capabilities": {"experimentalApi": True},
                }},
                {"method": "initialized"},
                {"id": 2, "method": "hooks/list", "params": {"cwds": [tmp]}},
            ]
            probe = subprocess.Popen(
                [CODEX, "app-server"], stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                cwd=tmp, env=environment,
            )
            try:
                listed = rpc(probe, initialization)[2]["result"]["data"][0]
            finally:
                stop(probe)
            self.assertEqual(len(listed["hooks"]), 1)
            configured_hook = listed["hooks"][0]
            with config.open("a", encoding="utf-8") as stream:
                stream.write(
                    f'\n[hooks.state.{json.dumps(configured_hook["key"])}]\n'
                    f'trusted_hash={json.dumps(configured_hook["currentHash"])}\n'
                )

            socket_name = "aipane-codex-test-" + uuid.uuid4().hex

            def tmux(*arguments):
                return subprocess.run(
                    [TMUX, "-L", socket_name, *arguments], env=environment,
                    capture_output=True, text=True, check=True, timeout=10,
                ).stdout.strip()

            daemon = None
            try:
                tmux("-f", "/dev/null", "new-session", "-d", "-s", "probe",
                     "-x", "120", "-y", "30", "-c", tmp, "/bin/sh")
                socket_path, server_pid, pane = tmux(
                    "display-message", "-p", "-t", "probe:0.0",
                    "#{socket_path}|#{pid}|#{pane_id}",
                ).split("|")
                daemon_environment = dict(
                    environment, TMUX=f"{socket_path},{server_pid},0",
                    TMUX_PANE="%999999",
                )
                with (directory / "daemon.log").open("wb") as log:
                    daemon = subprocess.Popen(
                        [CODEX, "app-server", "--listen", "unix://"],
                        env=daemon_environment, cwd=tmp,
                        stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                    )
                control_socket = (
                    codex_home / "app-server-control" / "app-server-control.sock"
                )
                deadline = time.monotonic() + 15
                while not control_socket.exists() and time.monotonic() < deadline:
                    self.assertIsNone(daemon.poll(), "test app-server exited")
                    time.sleep(0.05)
                self.assertTrue(control_socket.exists(), "app-server socket missing")
                launcher = subprocess.run(
                    ["zsh", "-dfc", 'source "$1/init.zsh"; '
                     '_aipane_tool_command x; print -r -- "$REPLY"',
                     "aipane-test", str(ROOT)],
                    env=environment, capture_output=True, text=True,
                    check=True, timeout=10,
                ).stdout.strip()
                tmux("respawn-pane", "-k", "-t", "probe:0.0",
                     "exec " + launcher + " 'blocked local hook test'")
                deadline = time.monotonic() + 15
                while not trace.exists() and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertTrue(trace.exists(), "Codex did not run the hook")
                event = json.loads(trace.read_text())
                self.assertEqual(event["returncode"], 0, event["stderr"])
                self.assertEqual(event["pane"], pane,
                                 "hook ran with the shared daemon's stale pane")
                marker = tmux("show-options", "-p", "-v", "-t", "probe:0.0",
                              "@tmux-window-wrap-activity")
                self.assertEqual(marker, "codex")
                record = json.loads(tmux(
                    "show-options", "-p", "-v", "-t", "probe:0.0",
                    "@tmux-window-wrap-activity-record",
                ))
                self.assertEqual(record["pane"]["id"], pane)
                self.assertNotEqual(record["process"]["pid"], daemon.pid)
            finally:
                if daemon is not None:
                    stop(daemon)
                subprocess.run([TMUX, "-L", socket_name, "kill-server"],
                               env=environment, capture_output=True, timeout=10)


if __name__ == "__main__":
    unittest.main()
