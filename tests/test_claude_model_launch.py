"""Exercise model memory and recovery with isolated profiles and a fake Claude CLI."""

import fcntl
import json
import os
import pty
import re
import select
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import termios
import time
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "integrations/claude/model-launch.py"
SESSION = "11111111-1111-4111-8111-111111111111"
UNKNOWN_SESSION = "22222222-2222-4222-8222-222222222222"
FAKE_CLAUDE = """#!/usr/bin/env python3
import json, os, sys
print('NATIVE=' + json.dumps({'args': sys.argv[1:], 'automatic_marker': os.environ.get('AIPANE_AUTO_RESUME')}), flush=True)
"""


class ModelLauncherTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.config = self.home / ".claude"
        project = re.sub(r"[^A-Za-z0-9-]", "-", str(self.home.resolve()))
        projects = self.config / "projects" / project
        projects.mkdir(parents=True)
        (projects / (SESSION + ".jsonl")).write_text(json.dumps({
            "type": "assistant", "requestedModel": "kimi-k3", "message": {"model": "kimi-k3"},
        }) + "\n")
        choices = [{"model": "kimi-k3", "label": "Kimi K3"},
                   {"model": "grok-fast", "label": "Grok Fast"}]
        (self.config / "settings.json").write_text(json.dumps({
            "model": "grok-fast", "env": {}, "modelPicker": {"options": choices},
        }))
        (self.config / "model-routing.json").write_text(json.dumps({"models": {
            "kimi-k3": {"context_tokens": 1048576, "billing_source": "Kimi"},
            "grok-fast": {"context_tokens": 500000, "billing_source": "Grok"},
        }}))
        native = self.home / ".local/bin/claude"
        native.parent.mkdir(parents=True)
        native.write_text(FAKE_CLAUDE)
        native.chmod(0o755)

    def environment(self, *, automatic=False, inherited_model="grok-fast", term="xterm-256color", config=None):
        env = {"HOME": str(self.home), "PATH": os.environ["PATH"],
               "LANG": "en_US.UTF-8", "TERM": term,
               "CLAUDE_CONFIG_DIR": str(config or self.config), "ANTHROPIC_MODEL": inherited_model}
        if automatic:
            env["AIPANE_AUTO_RESUME"] = "1"
        return env

    def launch(self, args, *, cwd=None, **environment):
        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 30, 100, 0, 0))
        proc = subprocess.Popen([sys.executable, str(SCRIPT), *args],
                                stdin=slave, stdout=slave, stderr=slave,
                                env=self.environment(**environment), cwd=cwd or self.home,
                                start_new_session=True)
        self.addCleanup(self.close_process, proc, master, slave)
        return proc, master

    @staticmethod
    def close_process(proc, master, slave):
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGTERM)
            proc.wait(timeout=3)
        os.close(master)
        os.close(slave)

    def read_until_decided(self, proc, master, markers=None):
        output = b""
        tail = b""
        deadline = time.monotonic() + 5
        markers = markers or (b"NATIVE=", "选择模型".encode(), "无法确定".encode())
        while not any(marker in output for marker in markers):
            if time.monotonic() >= deadline:
                self.fail(f"launcher did not reach native CLI, picker, or error: {output[-300:]!r}")
            if select.select([master], [], [], .05)[0]:
                try:
                    chunk = os.read(master, 65536)
                except OSError:
                    break
                pending = tail + chunk
                for _ in range(pending.count(b"\x1b[6n")):
                    os.write(master, b"\x1b[1;1R")
                tail = pending[-3:]
                output += chunk
            elif proc.poll() is not None:
                break
        return output

    def native_data(self, output):
        self.assertIn(b"NATIVE=", output)
        return json.loads(output.split(b"NATIVE=", 1)[1].splitlines()[0])

    def pick(self, choice="\r", args=None, **environment):
        proc, master = self.launch(args or [], inherited_model="", term="dumb", **environment)
        prompt = self.read_until_decided(proc, master, ("q 取消: ".encode(),))
        os.write(master, choice.encode())
        output = self.read_until_decided(proc, master, (b"NATIVE=",))
        self.assertEqual(proc.wait(timeout=3), 0, output)
        return prompt.decode(), self.native_data(output)

    def assert_model(self, native, expected):
        args = native["args"]
        self.assertEqual(args[args.index("--model") + 1], expected)

    def state(self, config=None):
        return (config or self.config) / "model-picker-state.json"

    def remember(self, model):
        self.state().write_text(json.dumps({"last_model": model}))

    def test_choice_is_remembered_across_processes_and_directories(self):
        settings_before = (self.config / "settings.json").read_bytes()
        prompt, native = self.pick("1\r")
        self.assertIn("回车选 Grok Fast", prompt)
        self.assert_model(native, "kimi-k3")
        self.assertEqual(json.loads(self.state().read_text()), {"last_model": "kimi-k3"})
        self.assertEqual(self.state().stat().st_mode & 0o777, 0o600)

        other_project = self.home / "other-project"
        other_project.mkdir()
        prompt, native = self.pick(cwd=other_project)
        self.assertIn("回车选 Kimi K3", prompt)
        self.assert_model(native, "kimi-k3")
        self.assertEqual((self.config / "settings.json").read_bytes(), settings_before)

    def test_latest_confirmed_choice_replaces_previous_choice(self):
        self.pick("1\r")
        self.pick("2\r")
        prompt, native = self.pick()
        self.assertIn("回车选 Grok Fast", prompt)
        self.assert_model(native, "grok-fast")
        self.assertEqual(json.loads(self.state().read_text())["last_model"], "grok-fast")

    def test_cancel_does_not_create_or_replace_memory(self):
        for previous in (None, "kimi-k3"):
            with self.subTest(previous=previous):
                if previous:
                    self.remember(previous)
                before = self.state().read_bytes() if previous else None
                proc, master = self.launch([], inherited_model="", term="dumb")
                self.read_until_decided(proc, master, ("q 取消: ".encode(),))
                os.write(master, b"q\r")
                output = self.read_until_decided(proc, master, ("已取消启动".encode(),))
                self.assertEqual(proc.wait(timeout=3), 130)
                self.assertNotIn(b"NATIVE=", output)
                self.assertEqual(self.state().read_bytes() if self.state().exists() else None, before)

    def test_invalid_or_removed_memory_falls_back_to_configured_model(self):
        for content in (b"broken", b"[]", b"null", b"\xff", b'{"last_model": 1}',
                        b'{"last_model": "removed"}'):
            with self.subTest(content=content):
                self.state().write_bytes(content)
                prompt, native = self.pick()
                self.assertIn("回车选 Grok Fast", prompt)
                self.assert_model(native, "grok-fast")

    def test_model_missing_from_either_menu_or_metadata_is_ignored(self):
        for filename in ("settings.json", "model-routing.json"):
            with self.subTest(filename=filename):
                path = self.config / filename
                before = path.read_text()
                data = json.loads(before)
                if filename == "settings.json":
                    data["modelPicker"]["options"] = data["modelPicker"]["options"][1:]
                else:
                    del data["models"]["kimi-k3"]
                path.write_text(json.dumps(data))
                self.remember("kimi-k3")
                try:
                    prompt, native = self.pick()
                    self.assertIn("回车选 Grok Fast", prompt)
                    self.assert_model(native, "grok-fast")
                finally:
                    path.write_text(before)

    def test_profiles_keep_separate_memories(self):
        self.pick("1\r")
        other = self.home / "other-profile"
        other.mkdir()
        for name in ("settings.json", "model-routing.json"):
            shutil.copy2(self.config / name, other / name)
        prompt, native = self.pick(config=other)
        self.assertIn("回车选 Grok Fast", prompt)
        self.assert_model(native, "grok-fast")
        self.assertEqual(json.loads(self.state(other).read_text())["last_model"], "grok-fast")
        self.assertEqual(json.loads(self.state().read_text())["last_model"], "kimi-k3")

    def test_manual_resume_prefers_session_model_over_memory(self):
        for args in (["--resume", SESSION], ["--continue"]):
            with self.subTest(args=args):
                self.remember("grok-fast")
                prompt, native = self.pick(args=args)
                self.assertIn("回车选 Kimi K3", prompt)
                self.assert_model(native, "kimi-k3")

    def test_explicit_model_bypasses_picker_and_preserves_memory(self):
        self.remember("kimi-k3")
        before = self.state().read_bytes()
        proc, master = self.launch(["--model", "grok-fast"], inherited_model="")
        native = self.native_data(self.read_until_decided(proc, master))
        self.assert_model(native, "grok-fast")
        self.assertEqual(proc.wait(timeout=3), 0)
        self.assertEqual(self.state().read_bytes(), before)

    def test_explicit_default_takes_priority_over_memory(self):
        self.remember("kimi-k3")
        proc, master = self.launch([], term="dumb", inherited_model="grok-fast")
        prompt = self.read_until_decided(proc, master, ("q 取消: ".encode(),)).decode()
        self.assertIn("回车选 Grok Fast", prompt)
        os.write(master, b"\r")
        native = self.native_data(self.read_until_decided(proc, master))
        self.assert_model(native, "grok-fast")
        self.assertEqual(proc.wait(timeout=3), 0)

    def test_noninteractive_run_does_not_use_or_change_memory(self):
        self.remember("kimi-k3")
        before = self.state().read_bytes()
        result = subprocess.run([sys.executable, str(SCRIPT), "-p", "hello"],
                                env=self.environment(inherited_model=""), cwd=self.home,
                                text=True, capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_model(self.native_data(result.stdout.encode()), "grok-fast")
        self.assertEqual(self.state().read_bytes(), before)

    def test_failed_memory_write_does_not_prevent_launch(self):
        self.state().mkdir()
        proc, master = self.launch([], inherited_model="", term="dumb")
        self.read_until_decided(proc, master, ("q 取消: ".encode(),))
        os.write(master, b"1\r")
        output = self.read_until_decided(proc, master, (b"NATIVE=",))
        self.assertIn("无法记住本次选择".encode(), output)
        self.assert_model(self.native_data(output), "kimi-k3")
        self.assertEqual(proc.wait(timeout=3), 0)
        self.assertFalse(list(self.config.glob(".model-picker-*.json")))

    @unittest.skipUnless(shutil.which("fzf"), "fzf is not installed")
    def test_fzf_reopens_with_last_confirmed_model_highlighted(self):
        proc, master = self.launch([], inherited_model="")
        self.read_until_decided(proc, master, (b"grok-fast",))
        os.write(master, b"kimi")
        self.read_until_decided(proc, master, (b"kimi-k3",))
        os.write(master, b"\r")
        self.read_until_decided(proc, master, ("选择模式".encode(),))
        os.write(master, b"\r")
        native = self.native_data(self.read_until_decided(proc, master))
        self.assert_model(native, "kimi-k3")
        self.assertEqual(proc.wait(timeout=3), 0)

        proc, master = self.launch([], inherited_model="")
        self.read_until_decided(proc, master, (b"kimi-k3",))
        os.write(master, b"\r")
        self.read_until_decided(proc, master, ("选择模式".encode(),))
        os.write(master, b"\r")
        native = self.native_data(self.read_until_decided(proc, master))
        self.assert_model(native, "kimi-k3")
        self.assertEqual(proc.wait(timeout=3), 0)

    def test_automatic_resume_uses_session_model_without_picker(self):
        self.remember("grok-fast")
        before = self.state().read_bytes()
        proc, master = self.launch(["--resume", SESSION], automatic=True)
        output = self.read_until_decided(proc, master)
        native = self.native_data(output)
        args = native["args"]
        self.assertEqual(args[args.index("--model") + 1], "kimi-k3")
        overlay = json.loads(args[args.index("--settings") + 1])
        self.assertEqual(overlay["env"]["CLAUDE_CODE_MAX_CONTEXT_TOKENS"], "1048576")
        self.assertIsNone(native["automatic_marker"])
        self.assertEqual(proc.wait(timeout=3), 0)
        self.assertEqual(self.state().read_bytes(), before)

    def test_automatic_resume_without_known_model_does_not_guess(self):
        self.remember("kimi-k3")
        proc, master = self.launch(["--resume", UNKNOWN_SESSION], automatic=True)
        output = self.read_until_decided(proc, master)
        self.assertTrue("无法确定".encode() in output, "automatic resume did not report an unknown model")
        self.assertNotIn(b"NATIVE=", output)
        self.assertEqual(proc.wait(timeout=3), 1)

    def test_manual_resume_still_opens_picker(self):
        proc, master = self.launch(["--resume", SESSION], automatic=False)
        output = self.read_until_decided(proc, master)
        self.assertTrue("选择模型".encode() in output, "manual resume did not show the model picker")
        self.assertIsNone(proc.poll())

    def test_marker_does_not_skip_picker_for_a_new_session(self):
        proc, master = self.launch([], automatic=True)
        output = self.read_until_decided(proc, master)
        self.assertTrue("选择模型".encode() in output, "new session did not show the model picker")
        self.assertIsNone(proc.poll())

    def test_explicit_model_wins_for_automatic_resume(self):
        proc, master = self.launch(["--resume", SESSION, "--model", "grok-fast"], automatic=True)
        native = self.native_data(self.read_until_decided(proc, master))
        args = native["args"]
        self.assertEqual(args[args.index("--model") + 1], "grok-fast")
        self.assertIsNone(native["automatic_marker"])
        self.assertEqual(proc.wait(timeout=3), 0)

    def test_explicit_model_allows_recovery_without_model_history(self):
        proc, master = self.launch(["--resume", UNKNOWN_SESSION, "--model", "grok-fast"], automatic=True)
        native = self.native_data(self.read_until_decided(proc, master))
        args = native["args"]
        self.assertEqual(args[args.index("--model") + 1], "grok-fast")
        self.assertEqual(proc.wait(timeout=3), 0)

    def test_continue_uses_last_session_model_without_picker(self):
        proc, master = self.launch(["--continue"], automatic=True)
        native = self.native_data(self.read_until_decided(proc, master))
        args = native["args"]
        self.assertEqual(args[args.index("--model") + 1], "kimi-k3")
        self.assertEqual(proc.wait(timeout=3), 0)


if __name__ == "__main__":
    unittest.main()
