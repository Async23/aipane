"""Live-tmux regression tests for safe in-place AI Tool restarts."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import tempfile
import textwrap
import unittest
import uuid
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RESTART = ROOT / "bin" / "ai-restart"


class AiRestartTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.tmp = Path(self.temp_dir.name)
        self.socket = f"ai-restart-test-{uuid.uuid4().hex}"
        self.tmux(
            "-f",
            "/dev/null",
            "new-session",
            "-d",
            "-s",
            "restart",
            "-c",
            str(self.tmp),
            "sleep 120",
        )
        self.tmux("set-option", "-g", "default-shell", "/bin/sh")
        self.target = self.tmux(
            "display-message",
            "-p",
            "-t",
            "restart",
            "#{session_name}:#{window_index}.#{pane_index}",
        ).stdout.strip()
        self.pane_id = self.tmux(
            "display-message",
            "-p",
            "-t",
            self.target,
            "#{pane_id}",
        ).stdout.strip()
        self.restore_log = self.tmp / "restore.log"
        self.save_log = self.tmp / "save.log"
        self.snapshot_log = self.tmp / "snapshot.log"
        self.resurrect_dir = self.tmp / "tmux" / "resurrect"
        self.make_fixtures()

    def tearDown(self):
        self.tmux("kill-server", check=False)
        self.temp_dir.cleanup()

    def tmux(self, *arguments, check=True):
        return subprocess.run(
            ["tmux", "-L", self.socket, *arguments],
            check=check,
            capture_output=True,
            text=True,
        )

    def make_fixtures(self):
        self.fake_save = self.tmp / "save.sh"
        self.fake_save.write_text(
            textwrap.dedent(
                """\
                #!/bin/sh
                mkdir -p "$TEST_RESURRECT_DIR"
                : > "$TEST_RESURRECT_DIR/dump.txt"
                ln -sf dump.txt "$TEST_RESURRECT_DIR/last"
                printf 'saved\\n' >> "$TEST_SAVE_LOG"
                """
            ),
            encoding="utf-8",
        )
        self.fake_save.chmod(0o755)

        self.fake_snapshot = self.tmp / "snapshot.sh"
        self.fake_snapshot.write_text(
            "#!/bin/sh\nprintf 'snapshot\\n' >> \"$TEST_SNAPSHOT_LOG\"\n",
            encoding="utf-8",
        )
        self.fake_snapshot.chmod(0o755)

        self.fake_restore = self.tmp / "restore.py"
        self.fake_restore.write_text(
            textwrap.dedent(
                """\
                #!/usr/bin/env python3
                import json
                import os
                import shlex
                import subprocess
                import sys

                if "--plan-json" in sys.argv:
                    print(json.dumps({
                        "target": os.environ["TEST_TARGET"],
                        "tool": "claude",
                        "kind": (
                            "invalid"
                            if os.environ.get("TEST_PLAN_INVALID") == "1"
                            else "resume"
                        ),
                        "restorable": os.environ.get("TEST_PLAN_INVALID") != "1",
                        "cwd": os.environ["TEST_CWD"],
                        "command": "claude --resume test-session",
                    }))
                else:
                    with open(os.environ["TEST_RESTORE_LOG"], "a", encoding="utf-8") as log:
                        log.write(" ".join(sys.argv[1:]) + "\\n")
                    if os.environ.get("TEST_RESTORE_LEAVES_SHELL") != "1":
                        subprocess.run([
                            *shlex.split(os.environ["AIPANE_TMUX"]),
                            "send-keys", "-t", os.environ["TEST_TARGET"],
                            "sleep 120", "Enter",
                        ], check=True)
                """
            ),
            encoding="utf-8",
        )
        self.fake_restore.chmod(0o755)

    def environment(self):
        environment = os.environ.copy()
        environment.update(
            {
                "AIPANE_TMUX": f"tmux -L {self.socket}",
                "AIPANE_RESURRECT_SAVE_SCRIPT": str(self.fake_save),
                "AIPANE_SNAPSHOT_COMMAND": str(self.fake_snapshot),
                "AIPANE_AI_RESTORE_COMMAND": str(self.fake_restore),
                "TEST_TARGET": self.target,
                "TEST_CWD": str(self.tmp),
                "TEST_RESURRECT_DIR": str(self.resurrect_dir),
                "TEST_RESTORE_LOG": str(self.restore_log),
                "TEST_SAVE_LOG": str(self.save_log),
                "TEST_SNAPSHOT_LOG": str(self.snapshot_log),
                "AIPANE_RESTART_VERIFY_TIMEOUT": "0.35",
                "AIPANE_RESTART_VERIFY_STABILITY": "0.1",
                "XDG_DATA_HOME": str(self.tmp),
            }
        )
        if getattr(self, "restore_leaves_shell", False):
            environment["TEST_RESTORE_LEAVES_SHELL"] = "1"
        if getattr(self, "plan_invalid", False):
            environment["TEST_PLAN_INVALID"] = "1"
        environment.update(getattr(self, "extra_environment", {}))
        return environment

    def run_restart(self, *arguments):
        return subprocess.run(
            [str(RESTART), *arguments],
            check=False,
            capture_output=True,
            text=True,
            env=self.environment(),
        )

    def current_command(self):
        return self.tmux(
            "display-message",
            "-p",
            "-t",
            self.target,
            "#{pane_current_command}",
        ).stdout.strip()

    def test_dry_run_refreshes_snapshot_without_restarting_pane(self):
        result = self.run_restart("--dry-run")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("claude", result.stdout)
        self.assertIn("Dry run", result.stdout)
        self.assertEqual(self.current_command(), "sleep")
        self.assertEqual(self.save_log.read_text(), "saved\n")
        self.assertEqual(self.snapshot_log.read_text(), "snapshot\n")
        self.assertFalse(self.restore_log.exists())

    def test_busy_pane_aborts_the_whole_restart(self):
        self.tmux(
            "set-option",
            "-p",
            "-t",
            self.target,
            "@tmux-window-wrap-activity",
            "sleep",
        )

        result = self.run_restart("--yes")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("0.0 (BUSY)", result.stderr)
        self.assertEqual(self.current_command(), "sleep")
        self.assertFalse(self.restore_log.exists())

    def test_unreported_pane_cannot_be_accepted_by_yes_alone(self):
        result = self.run_restart("--yes")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("cannot be accepted by --yes alone", result.stderr)
        self.assertEqual(self.current_command(), "sleep")
        self.assertFalse(self.restore_log.exists())

    def test_reported_idle_pane_restarts_without_force(self):
        self.tmux(
            "set-option",
            "-p",
            "-t",
            self.target,
            "@tmux-window-wrap-activity-reporter",
            "sleep",
        )

        result = self.run_restart("--yes")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("verified 1 AI pane(s) resumed", result.stdout)
        self.assertIn("--dump", self.restore_log.read_text())

    def test_restore_command_that_leaves_a_shell_is_reported_failed(self):
        self.restore_leaves_shell = True
        self.tmux(
            "set-option",
            "-p",
            "-t",
            self.target,
            "@tmux-window-wrap-activity-reporter",
            "sleep",
        )

        result = self.run_restart("--yes")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn(f"failed to resume {self.target} (claude)", result.stderr)
        self.assertIn("verified 0 resumed; 1 failed", result.stdout)

    def test_invalid_session_is_reported_and_left_untouched(self):
        self.plan_invalid = True

        result = self.run_restart("--dry-run")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Non-restorable AI panes left untouched", result.stdout)
        self.assertIn("no saved conversation", result.stdout)
        self.assertIn("no live, restorable AI panes found", result.stdout)
        self.assertEqual(self.current_command(), "sleep")
        self.assertFalse(self.restore_log.exists())

    def test_force_respawns_same_pane_then_invokes_ai_restore(self):
        self.tmux(
            "set-option",
            "-p",
            "-t",
            self.target,
            "@tmux-window-wrap-activity",
            "sleep",
        )

        result = self.run_restart("--yes", "--force")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("verified 1 AI pane(s) resumed", result.stdout)
        self.assertEqual(self.current_command(), "sleep")
        current_pane_id = self.tmux(
            "display-message",
            "-p",
            "-t",
            self.target,
            "#{pane_id}",
        ).stdout.strip()
        self.assertEqual(current_pane_id, self.pane_id)
        restored_cwd = self.tmux(
            "display-message",
            "-p",
            "-t",
            self.target,
            "#{pane_current_path}",
        ).stdout.strip()
        self.assertEqual(Path(restored_cwd).resolve(), self.tmp.resolve())
        self.assertIn("--dump", self.restore_log.read_text())

    def test_dry_run_resolves_terminal_codex_without_repairing_marker(self):
        fake_codex = self.tmp / "codex"
        fake_codex_source = self.tmp / "codex.c"
        fake_codex_source.write_text(
            "#include <unistd.h>\nint main(void) { sleep(120); return 0; }\n",
            encoding="utf-8",
        )
        subprocess.run(
            ["cc", str(fake_codex_source), "-o", str(fake_codex)],
            check=True,
            capture_output=True,
            text=True,
        )
        self.tmux(
            "respawn-pane",
            "-k",
            "-t",
            self.target,
            str(fake_codex),
            "120",
        )
        self.assertEqual(self.current_command(), "codex")

        codex_home = self.tmp / ".codex"
        transcript = (
            codex_home
            / "sessions"
            / "2026"
            / "08"
            / "14"
            / "rollout-2026-08-14T19-00-41-thread-restart.jsonl"
        )
        transcript.parent.mkdir(parents=True)
        transcript.write_text(
            json.dumps(
                {
                    "type": "event_msg",
                    "payload": {
                        "type": "task_complete",
                        "turn_id": "turn-restart",
                        "error": {"message": "remote compact failed"},
                        "completed_at": 1_786_707_036,
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )
        history = sqlite3.connect(codex_home / "thread_history_1.sqlite")
        try:
            history.executescript(
                """
                CREATE TABLE thread_turns (
                    thread_id TEXT NOT NULL,
                    turn_id TEXT NOT NULL,
                    rollout_ordinal INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    completed_at INTEGER,
                    PRIMARY KEY (thread_id, turn_id)
                );
                CREATE TABLE thread_history_projection_state (
                    thread_id TEXT PRIMARY KEY,
                    next_rollout_byte_offset INTEGER NOT NULL,
                    next_rollout_ordinal INTEGER NOT NULL
                );
                """
            )
            history.execute(
                "INSERT INTO thread_turns VALUES (?, ?, ?, ?, ?)",
                (
                    "thread-restart",
                    "turn-restart",
                    1,
                    "failed",
                    1_786_707_036,
                ),
            )
            history.execute(
                "INSERT INTO thread_history_projection_state VALUES (?, ?, ?)",
                ("thread-restart", transcript.stat().st_size, 2),
            )
            history.commit()
        finally:
            history.close()
        identity = self.tmux(
            "display-message",
            "-p",
            "-t",
            self.target,
            "#{pane_id}\t#{pane_tty}\t#{socket_path}\t#{pid}\t#{pane_pid}",
        ).stdout.strip().split("\t")
        pane_id, pane_tty, socket_path, server_pid, process_pid = identity
        process_started_at = subprocess.run(
            ["ps", "-p", process_pid, "-o", "lstart="],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        record = json.dumps(
            {
                "version": 1,
                "revision": "revision-restart",
                "generation": "generation-restart",
                "owner": "codex",
                "reported": "busy",
                "updated_at": 1_786_706_587_976,
                "pane": {
                    "id": pane_id,
                    "tty": pane_tty,
                    "socket": socket_path,
                    "server_pid": server_pid,
                },
                "process": {
                    "pid": int(process_pid),
                    "started_at": process_started_at,
                },
                "root": {
                    "session_id": "thread-restart",
                    "turn_id": "turn-restart",
                    "transcript_path": str(transcript),
                    "codex_home": str(codex_home),
                },
            },
            separators=(",", ":"),
        )
        for option, value in (
            ("@tmux-window-wrap-activity", "codex"),
            ("@tmux-window-wrap-activity-reporter", "codex"),
            ("@tmux-window-wrap-activity-updated-at", "1786706587976"),
            ("@tmux-window-wrap-activity-record", record),
        ):
            self.tmux("set-option", "-p", "-t", self.target, option, value)
        self.extra_environment = {"CODEX_HOME": str(codex_home)}

        result = self.run_restart("--dry-run")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("idle", result.stdout)
        self.assertNotIn("BUSY", result.stdout)
        marker = self.tmux(
            "show-options",
            "-pqv",
            "-t",
            self.target,
            "@tmux-window-wrap-activity",
        ).stdout.strip()
        self.assertEqual(marker, "codex")


if __name__ == "__main__":
    unittest.main()
