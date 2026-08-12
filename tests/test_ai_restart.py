"""Live-tmux regression tests for safe in-place AI Tool restarts."""

from __future__ import annotations

import json
import os
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


if __name__ == "__main__":
    unittest.main()
