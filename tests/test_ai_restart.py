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
        self.plan_log = self.tmp / "plan.log"
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
        fake_agent_source = self.tmp / "claude.c"
        fake_agent_source.write_text(
            "#include <unistd.h>\nint main(void) { sleep(120); return 0; }\n",
            encoding="utf-8",
        )
        self.fake_agent = self.tmp / "claude"
        subprocess.run(
            ["cc", str(fake_agent_source), "-o", str(self.fake_agent)],
            check=True,
            capture_output=True,
            text=True,
        )

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
                import sys

                targets = [
                    (os.environ["TEST_TARGET"], os.environ["TEST_AGENT_COMMAND"]),
                ]
                if second_target := os.environ.get("TEST_SECOND_TARGET"):
                    targets.append((second_target, os.environ["TEST_AGENT_COMMAND"]))

                if "--plan-json" in sys.argv:
                    with open(os.environ["TEST_PLAN_LOG"], "a", encoding="utf-8") as log:
                        log.write("plan\\n")
                    for target, command in targets:
                        print(json.dumps({
                            "target": target,
                            "tool": "claude",
                            "kind": (
                                "invalid"
                                if os.environ.get("TEST_PLAN_INVALID") == "1"
                                else "resume"
                            ),
                            "restorable": os.environ.get("TEST_PLAN_INVALID") != "1",
                            "cwd": os.environ["TEST_CWD"],
                            "command": (
                                "/usr/bin/true"
                                if os.environ.get("TEST_RESTORE_LEAVES_SHELL") == "1"
                                else command
                            ),
                            "sid": "",
                        }))
                else:
                    with open(os.environ["TEST_RESTORE_LOG"], "a", encoding="utf-8") as log:
                        log.write("unexpected action invocation\\n")
                    raise SystemExit(9)
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
                "TEST_AGENT_COMMAND": str(self.fake_agent),
                "TEST_RESURRECT_DIR": str(self.resurrect_dir),
                "TEST_RESTORE_LOG": str(self.restore_log),
                "TEST_PLAN_LOG": str(self.plan_log),
                "TEST_SAVE_LOG": str(self.save_log),
                "TEST_SNAPSHOT_LOG": str(self.snapshot_log),
                "AIPANE_STATE_DIR": str(self.tmp / "aipane-state"),
                "AI_RESTORE_MAX_ATTEMPTS": "1",
                "AI_RESTORE_VERIFY_TIMEOUT": "0.35",
                "AI_RESTORE_VERIFY_STABILITY": "0.1",
                "AI_RESTORE_LAUNCH_DELAY": "0",
                "AI_RESTORE_POLL_INTERVAL": "0.01",
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

    def test_dry_run_preserves_sync(self):
        self.tmux(
            "set-window-option",
            "-t",
            self.target,
            "synchronize-panes",
            "on",
        )

        result = self.run_restart("--dry-run")

        self.assertEqual(result.returncode, 0, result.stderr)
        synchronized = self.tmux(
            "display-message",
            "-p",
            "-t",
            self.target,
            "#{synchronize-panes}",
        ).stdout.strip()
        self.assertEqual(synchronized, "1")

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
        self.assertEqual(self.current_command(), "claude")
        self.assertEqual(self.plan_log.read_text(), "plan\n")
        self.assertFalse(self.restore_log.exists())

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
        self.assertIn(self.target, result.stderr)
        self.assertIn("verified 0 resumed; 1 pending", result.stdout)
        self.assertFalse(self.restore_log.exists())

    def test_invalid_session_is_reported_and_left_untouched(self):
        self.plan_invalid = True

        result = self.run_restart("--dry-run")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Non-restorable AI panes left untouched", result.stdout)
        self.assertIn("no saved conversation", result.stdout)
        self.assertIn("no live, restorable AI panes found", result.stdout)
        self.assertEqual(self.current_command(), "sleep")
        self.assertFalse(self.restore_log.exists())

    def test_force_respawns_same_pane_via_the_recovery_executor(self):
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
        self.assertEqual(self.current_command(), "claude")
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
        self.assertEqual(self.plan_log.read_text(), "plan\n")
        self.assertFalse(self.restore_log.exists())

    def test_restart_disables_sync_before_restoring_distinct_panes(self):
        second_target = self.tmux(
            "split-window",
            "-h",
            "-P",
            "-F",
            "#{session_name}:#{window_index}.#{pane_index}",
            "-t",
            self.target,
            "-c",
            str(self.tmp),
            "sleep 120",
        ).stdout.strip()
        self.extra_environment = {"TEST_SECOND_TARGET": second_target}
        self.tmux(
            "set-window-option",
            "-t",
            self.target,
            "synchronize-panes",
            "on",
        )

        result = self.run_restart("--yes", "--force")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("disabled SYNC for", result.stdout)
        synchronized = self.tmux(
            "display-message",
            "-p",
            "-t",
            self.target,
            "#{synchronize-panes}",
        ).stdout.strip()
        self.assertEqual(synchronized, "0")
        self.assertEqual(self.current_command(), "claude")
        second_command = self.tmux(
            "display-message",
            "-p",
            "-t",
            second_target,
            "#{pane_current_command}",
        ).stdout.strip()
        self.assertEqual(second_command, "claude")
        self.assertEqual(self.plan_log.read_text(), "plan\n")
        self.assertFalse(self.restore_log.exists())

    def test_restart_clears_activity_metadata_from_replaced_process(self):
        for option, value in (
            ("@tmux-window-wrap-activity", "sleep"),
            ("@tmux-window-wrap-activity-reporter", "sleep"),
            ("@tmux-window-wrap-activity-updated-at", "1234"),
            ("@tmux-window-wrap-activity-record", '{"version":1}'),
        ):
            self.tmux("set-option", "-p", "-t", self.target, option, value)

        result = self.run_restart("--yes", "--force")

        self.assertEqual(result.returncode, 0, result.stderr)
        for option in (
            "@tmux-window-wrap-activity",
            "@tmux-window-wrap-activity-reporter",
            "@tmux-window-wrap-activity-updated-at",
            "@tmux-window-wrap-activity-record",
        ):
            value = self.tmux(
                "show-options",
                "-pqv",
                "-t",
                self.target,
                option,
            ).stdout.strip()
            self.assertEqual(value, "", option)

    def test_force_cannot_bypass_a_post_confirmation_guard_change(self):
        wrapper = self.tmp / "change-guard-then-execute.py"
        wrapper.write_text(
            textwrap.dedent(
                f"""\
                #!/usr/bin/env python3
                import os
                import shlex
                import subprocess
                import sys

                subprocess.run([
                    *shlex.split(os.environ["AIPANE_TMUX"]),
                    "set-option", "-p", "-t", os.environ["TEST_TARGET"],
                    "@tmux-window-wrap-activity", "changed-after-confirmation",
                ], check=True)
                os.execv({str((ROOT / 'bin' / 'aipane-restore-executor'))!r}, [
                    {str((ROOT / 'bin' / 'aipane-restore-executor'))!r},
                    *sys.argv[1:],
                ])
                """
            ),
            encoding="utf-8",
        )
        wrapper.chmod(0o755)
        self.extra_environment = {
            "AIPANE_RESTART_EXECUTOR_COMMAND": str(wrapper),
        }

        result = self.run_restart("--yes", "--force")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("pane guard changed", result.stderr)
        self.assertEqual(self.current_command(), "sleep")
        self.assertFalse(self.restore_log.exists())

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
