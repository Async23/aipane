"""Recovery execution tests: verification, retry, and durable pending intent."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
EXECUTOR = ROOT / "bin" / "aipane-restore-executor"
GROK_SID = "94ea9701-d5e6-4c1f-a4ce-f876266e4629"


class RestoreExecutorTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.tmp = Path(self.temp_dir.name)
        self.state_dir = self.tmp / "state"
        self.state_dir.mkdir()
        self.runtime_state = self.tmp / "runtime-state"
        self.runtime_state.write_text("zsh\n", encoding="utf-8")
        self.launch_count = self.tmp / "launch-count"
        self.launch_count.write_text("0\n", encoding="utf-8")
        self.tmux_log = self.tmp / "tmux.log"
        self.bind_log = self.tmp / "bind.log"
        self.grok_log = self.tmp / "grok.jsonl"
        self.grok_log.touch()
        self.plan_file = self.tmp / "plan.jsonl"
        self.fake_tmux = self.tmp / "tmux"
        self.fake_bind = self.tmp / "aipane-bind"
        self.write_fake_tmux()
        self.fake_bind.write_text(
            "#!/bin/sh\nprintf 'cwd=%s %s\\n' \"$PWD\" \"$*\" >> \"$BIND_LOG\"\n",
            encoding="utf-8",
        )
        self.fake_bind.chmod(0o755)

    def tearDown(self):
        self.temp_dir.cleanup()

    def write_fake_tmux(self) -> None:
        self.fake_tmux.write_text(
            textwrap.dedent(
                """\
                #!/usr/bin/env python3
                import json
                import os
                import sys
                from pathlib import Path

                args = sys.argv[1:]
                state_path = Path(os.environ["FAKE_RUNTIME_STATE"])
                log_path = Path(os.environ["TMUX_LOG"])
                with log_path.open("a", encoding="utf-8") as log:
                    log.write(json.dumps(args) + "\\n")

                command = args[0] if args else ""
                if command == "has-session":
                    raise SystemExit(0)
                if command == "capture-pane":
                    print("Couldn't start Grok: startup timed out after 43s.")
                    raise SystemExit(0)
                if command == "display-message":
                    if args[-1].startswith("#{"):
                        state = state_path.read_text(encoding="utf-8").strip()
                        value = args[-1]
                        replacements = {
                            "#{pane_current_command}": state,
                            "#{pane_id}": os.environ["FAKE_PANE_ID"],
                            "#{socket_path}": "/private/tmp/tmux-test",
                            "#{pid}": "4242",
                        }
                        for marker, replacement in replacements.items():
                            value = value.replace(marker, replacement)
                        print(value)
                    raise SystemExit(0)
                if command == "send-keys":
                    if "Enter" in args:
                        mode = os.environ["FAKE_LAUNCH_MODE"]
                        count_path = Path(os.environ["FAKE_LAUNCH_COUNT"])
                        count = int(count_path.read_text(encoding="utf-8")) + 1
                        count_path.write_text(f"{count}\\n", encoding="utf-8")
                        if mode == "fail" or (mode == "fail-once" and count == 1):
                            state_path.write_text("zsh\\n", encoding="utf-8")
                        else:
                            state_path.write_text("grok-1.0.4-maco\\n", encoding="utf-8")
                            if mode != "running":
                                with Path(os.environ["GROK_LOG"]).open(
                                    "a", encoding="utf-8"
                                ) as grok_log:
                                    grok_log.write(json.dumps({
                                        "ts": "2026-08-17T03:04:49.935Z",
                                        "pid": 123,
                                        "lvl": "info",
                                        "sid": os.environ["GROK_SID"],
                                        "msg": "session loaded",
                                    }) + "\\n")
                    raise SystemExit(0)
                raise SystemExit(0)
                """
            ),
            encoding="utf-8",
        )
        self.fake_tmux.chmod(0o755)

    def grok_plan(self) -> dict[str, object]:
        return {
            "target": "0:4.1",
            "tool": "grok",
            "kind": "resume",
            "restorable": True,
            "cwd": str(self.tmp),
            "command": f"grok --always-approve --resume {GROK_SID}",
            "sid": GROK_SID,
        }

    def run_executor(
        self,
        plan: list[dict[str, object]],
        *,
        launch_mode: str,
        max_attempts: int = 1,
        plan_json: bool = False,
        pane_id: str = "%42",
    ) -> subprocess.CompletedProcess[str]:
        self.plan_file.write_text(
            "".join(json.dumps(item) + "\n" for item in plan),
            encoding="utf-8",
        )
        env = os.environ.copy()
        env.update(
            {
                "AIPANE_STATE_DIR": str(self.state_dir),
                "AIPANE_TMUX": str(self.fake_tmux),
                "AIPANE_BIND_COMMAND": str(self.fake_bind),
                "AIPANE_GROK_LOG": str(self.grok_log),
                "AI_RESTORE_MAX_ATTEMPTS": str(max_attempts),
                "AI_RESTORE_VERIFY_TIMEOUT": "0.15",
                "AI_RESTORE_VERIFY_STABILITY": "0.03",
                "AI_RESTORE_GROK_VERIFY_TIMEOUT": "0.15",
                "AI_RESTORE_RETRY_DELAY": "0.01",
                "AI_RESTORE_LAUNCH_DELAY": "0",
                "AI_RESTORE_POLL_INTERVAL": "0.01",
                "FAKE_RUNTIME_STATE": str(self.runtime_state),
                "FAKE_PANE_ID": pane_id,
                "FAKE_LAUNCH_COUNT": str(self.launch_count),
                "FAKE_LAUNCH_MODE": launch_mode,
                "TMUX_LOG": str(self.tmux_log),
                "BIND_LOG": str(self.bind_log),
                "GROK_LOG": str(self.grok_log),
                "GROK_SID": GROK_SID,
            }
        )
        arguments = [
            str(EXECUTOR),
            "--plan",
            str(self.plan_file),
            "--dump",
            str(self.tmp / "resurrect.txt"),
        ]
        if plan_json:
            arguments.append("--plan-json")
        return subprocess.run(
            arguments,
            check=False,
            capture_output=True,
            text=True,
            env=env,
        )

    def pending_items(self) -> list[dict[str, object]]:
        pending = self.state_dir / "restore-pending.json"
        if not pending.exists():
            return []
        return json.loads(pending.read_text(encoding="utf-8"))["items"]

    def test_running_grok_is_not_verified_before_session_loaded(self):
        result = self.run_executor([self.grok_plan()], launch_mode="running")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("pending=1", result.stdout)
        self.assertEqual(
            [item["target"] for item in self.pending_items()],
            ["0:4.1"],
        )
        self.assertFalse(self.bind_log.exists())

    def test_unproven_existing_agent_is_deferred_without_interrupting_it(self):
        self.runtime_state.write_text("grok-1.0.4-maco\n", encoding="utf-8")

        result = self.run_executor(
            [self.grok_plan()],
            launch_mode="ready",
            max_attempts=2,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("deferred=1", result.stdout)
        calls = [
            json.loads(line)
            for line in self.tmux_log.read_text(encoding="utf-8").splitlines()
        ]
        self.assertFalse(any(call[0] == "send-keys" for call in calls))

    def test_pending_intent_does_not_target_a_reused_pane_coordinate(self):
        failed = self.run_executor([self.grok_plan()], launch_mode="fail")
        self.assertNotEqual(failed.returncode, 0)
        sends_before = sum(
            json.loads(line)[0] == "send-keys"
            for line in self.tmux_log.read_text(encoding="utf-8").splitlines()
        )

        reused = self.run_executor(
            [self.grok_plan()],
            launch_mode="ready",
            max_attempts=2,
            pane_id="%99",
        )

        self.assertNotEqual(reused.returncode, 0)
        self.assertIn("deferred=1", reused.stdout)
        sends_after = sum(
            json.loads(line)[0] == "send-keys"
            for line in self.tmux_log.read_text(encoding="utf-8").splitlines()
        )
        self.assertEqual(sends_after, sends_before)
        self.assertEqual(len(self.pending_items()), 1)

    def test_pending_plan_retries_cleanly_when_new_snapshot_lost_the_pane(self):
        failed = self.run_executor([self.grok_plan()], launch_mode="fail")
        self.assertNotEqual(failed.returncode, 0)
        self.assertEqual(len(self.pending_items()), 1)

        shown = self.run_executor([], launch_mode="ready", plan_json=True)
        self.assertEqual(shown.returncode, 0, shown.stderr)
        pending_plan = json.loads(shown.stdout)
        self.assertEqual(pending_plan["target"], "0:4.1")
        self.assertTrue(pending_plan["pending"])

        shown_with_same_snapshot = self.run_executor(
            [self.grok_plan()],
            launch_mode="ready",
            plan_json=True,
        )
        self.assertEqual(shown_with_same_snapshot.returncode, 0)
        self.assertTrue(json.loads(shown_with_same_snapshot.stdout)["pending"])

        recovered = self.run_executor([], launch_mode="ready")

        self.assertEqual(recovered.returncode, 0, recovered.stderr)
        self.assertIn("verified=1", recovered.stdout)
        self.assertEqual(self.pending_items(), [])
        binding = self.bind_log.read_text(encoding="utf-8")
        self.assertIn(f"cwd={self.tmp.resolve()}", binding)
        self.assertIn(f"--tool g --sid {GROK_SID}", binding)

        tmux_calls = [
            json.loads(line)
            for line in self.tmux_log.read_text(encoding="utf-8").splitlines()
        ]
        self.assertTrue(
            any(call[0] == "send-keys" and "C-c" in call for call in tmux_calls)
        )
        self.assertTrue(
            any(call[0] == "send-keys" and "-R" in call for call in tmux_calls)
        )
        self.assertTrue(
            any(
                call[0] == "send-keys"
                and "-l" in call
                and self.grok_plan()["command"] in call
                for call in tmux_calls
            )
        )

    def test_transient_failure_is_retried_and_verified_in_the_same_run(self):
        result = self.run_executor(
            [self.grok_plan()],
            launch_mode="fail-once",
            max_attempts=2,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("verified=1", result.stdout)
        self.assertEqual(self.pending_items(), [])
        calls = [
            json.loads(line)
            for line in self.tmux_log.read_text(encoding="utf-8").splitlines()
        ]
        literal_launches = [
            call
            for call in calls
            if call[0] == "send-keys"
            and "-l" in call
            and self.grok_plan()["command"] in call
        ]
        self.assertEqual(len(literal_launches), 2)


if __name__ == "__main__":
    unittest.main()
