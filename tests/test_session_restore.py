"""Regression tests for tmux server-scoped AI session restoration."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT = ROOT / "bin" / "aipane-snapshot"
RESTORE = ROOT / "bin" / "ai-restore"

VALID_CODEX_SID = "019fdad7-16c3-7a13-89cf-ec8c2184e5f7"
FOREIGN_SID = "1fa8ab6a-cba1-4189-abcd-64a29ffb2fc3"
VALID_PI_SID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"


class SessionRestoreTests(unittest.TestCase):
    def test_snapshot_uses_codex_activity_record_before_first_notify(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            state_dir = tmp / "state"
            state_dir.mkdir()
            record = json.dumps(
                {
                    "version": 1,
                    "owner": "codex",
                    "pane": {
                        "id": "%3",
                        "socket": "/private/tmp/tmux-main",
                        "server_pid": "999",
                    },
                    "root": {
                        "session_id": VALID_CODEX_SID,
                        "turn_id": "",
                    },
                },
                separators=(",", ":"),
            )
            fake_tmux = tmp / "tmux"
            fake_tmux.write_text(
                textwrap.dedent(
                    f"""\
                    #!/bin/sh
                    case "$1" in
                      display-message)
                        case "$*" in
                          *pane_current_command*)
                            printf '%%3\\037codex\\0370:@1.%%3\\037\\037/private/tmp/tmux-main\\037999\\037123\\037\\037\\037\\037%s\\n' '{record}'
                            ;;
                          *)
                            printf '/private/tmp/tmux-main\\t999\\n'
                            ;;
                        esac
                        ;;
                      list-panes)
                        printf '%%3\\t0:4.1\\t/Users/test\\n'
                        ;;
                      *)
                        exit 99
                        ;;
                    esac
                    """
                ),
                encoding="utf-8",
            )
            fake_tmux.chmod(0o755)
            env = os.environ.copy()
            env.update(
                {
                    "AIPANE_STATE_DIR": str(state_dir),
                    "AIPANE_REGISTRY": str(state_dir / "missing.jsonl"),
                    "AIPANE_TMUX": str(fake_tmux),
                }
            )

            subprocess.run(
                [sys.executable, str(SNAPSHOT)],
                check=True,
                capture_output=True,
                text=True,
                env=env,
            )

            coords = json.loads((state_dir / "coords-last.json").read_text())
            self.assertEqual(
                coords["0:4.1"],
                {"sid": VALID_CODEX_SID, "tool": "x", "cwd": "/Users/test"},
            )

    def test_snapshot_rejects_activity_record_from_another_tmux_server(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            state_dir = tmp / "state"
            state_dir.mkdir()
            record = json.dumps(
                {
                    "version": 1,
                    "owner": "codex",
                    "pane": {
                        "id": "%3",
                        "socket": "/private/tmp/tmux-old",
                        "server_pid": "111",
                    },
                    "root": {"session_id": FOREIGN_SID, "turn_id": ""},
                },
                separators=(",", ":"),
            )
            fake_tmux = tmp / "tmux"
            fake_tmux.write_text(
                textwrap.dedent(
                    f"""\
                    #!/bin/sh
                    case "$1" in
                      display-message)
                        case "$*" in
                          *pane_current_command*)
                            printf '%%3\\037codex\\0370:@1.%%3\\037\\037/private/tmp/tmux-main\\037999\\037123\\037\\037\\037\\037%s\\n' '{record}'
                            ;;
                          *)
                            printf '/private/tmp/tmux-main\\t999\\n'
                            ;;
                        esac
                        ;;
                      list-panes)
                        printf '%%3\\t0:4.1\\t/Users/test\\n'
                        ;;
                      *)
                        exit 99
                        ;;
                    esac
                    """
                ),
                encoding="utf-8",
            )
            fake_tmux.chmod(0o755)
            env = os.environ.copy()
            env.update(
                {
                    "AIPANE_STATE_DIR": str(state_dir),
                    "AIPANE_REGISTRY": str(state_dir / "missing.jsonl"),
                    "AIPANE_TMUX": str(fake_tmux),
                }
            )

            subprocess.run(
                [sys.executable, str(SNAPSHOT)],
                check=True,
                capture_output=True,
                text=True,
                env=env,
            )

            self.assertEqual(
                json.loads((state_dir / "coords-last.json").read_text()),
                {},
            )

    def test_snapshot_ignores_newer_binding_from_another_tmux_server(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            state_dir = tmp / "state"
            state_dir.mkdir()
            registry = state_dir / "registry.jsonl"
            records = [
                {
                    "ts": 1,
                    "sock": "/private/tmp/tmux-main",
                    "srv": "999",
                    "pane": "%3",
                    "tool": "x",
                    "sid": VALID_CODEX_SID,
                },
                {
                    "ts": 2,
                    "sock": "aipane-test",
                    "srv": "aipane-test",
                    "pane": "%3",
                    "tool": "g",
                    "sid": FOREIGN_SID,
                },
            ]
            registry.write_text(
                "".join(json.dumps(record) + "\n" for record in records),
                encoding="utf-8",
            )

            fake_tmux = tmp / "tmux"
            fake_tmux.write_text(
                textwrap.dedent(
                    """\
                    #!/bin/sh
                    case "$1" in
                      display-message)
                        printf '/private/tmp/tmux-main\\t999\\n'
                        ;;
                      list-panes)
                        printf '%%3\\t0:4.1\\t/Users/test\\n'
                        ;;
                      *)
                        exit 99
                        ;;
                    esac
                    """
                ),
                encoding="utf-8",
            )
            fake_tmux.chmod(0o755)

            env = os.environ.copy()
            env.update(
                {
                    "AIPANE_STATE_DIR": str(state_dir),
                    "AIPANE_REGISTRY": str(registry),
                    "AIPANE_TMUX": str(fake_tmux),
                }
            )
            subprocess.run(
                [sys.executable, str(SNAPSHOT)],
                check=True,
                capture_output=True,
                text=True,
                env=env,
            )

            coords = json.loads((state_dir / "coords-last.json").read_text())
            self.assertEqual(
                coords["0:4.1"]["sid"],
                VALID_CODEX_SID,
                "a newer record from another tmux server must not win by pane id",
            )
            self.assertEqual(coords["0:4.1"]["tool"], "x")

    def test_restore_rejects_coord_binding_for_a_different_tool(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            result = self.run_restore(
                tmp,
                coord={"sid": FOREIGN_SID, "tool": "g"},
                title=VALID_CODEX_SID,
                saved_codex_ids={VALID_CODEX_SID},
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(
                "codex --yolo --disable plugins "
                f"-c check_for_update_on_startup=false resume {VALID_CODEX_SID}",
                result.stdout,
            )
            self.assertNotIn(FOREIGN_SID, result.stdout)

    def test_restore_migrates_retired_codex_override(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            result = self.run_restore(
                tmp,
                coord={"sid": VALID_CODEX_SID, "tool": "x"},
                title="custom-title",
                saved_codex_ids={VALID_CODEX_SID},
                extra_env={"AIPANE_CODEX_LAUNCH_CMD": "codexx"},
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(
                "codex --yolo --disable plugins "
                f"-c check_for_update_on_startup=false resume {VALID_CODEX_SID}",
                result.stdout,
            )
            self.assertNotIn("codexx resume", result.stdout)

    def test_restore_marks_missing_codex_session_invalid(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            result = self.run_restore(
                tmp,
                coord={"sid": FOREIGN_SID, "tool": "x"},
                title="custom-title",
                saved_codex_ids=set(),
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            matching_line = next(
                line for line in result.stdout.splitlines() if "0:4.1" in line
            )
            self.assertIn("invalid", matching_line)

    def test_restore_marks_claude_id_without_a_conversation_invalid(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            claude_config = self.claude_config(tmp, conversation_exists=False)
            result = self.run_restore(
                tmp,
                coord={"sid": FOREIGN_SID, "tool": "c"},
                title="custom-title",
                saved_codex_ids=set(),
                plan_json=True,
                current_command="claude",
                full_command=(
                    "claude --dangerously-skip-permissions "
                    f"--session-id {FOREIGN_SID}"
                ),
                extra_env={"CLAUDE_CONFIG_DIR": str(claude_config)},
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            plan = json.loads(result.stdout)
            self.assertEqual(plan["tool"], "claude")
            self.assertEqual(plan["kind"], "invalid")
            self.assertFalse(plan["restorable"])

    def test_restore_keeps_claude_id_with_a_project_transcript(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            claude_config = self.claude_config(tmp, conversation_exists=True)
            result = self.run_restore(
                tmp,
                coord={"sid": FOREIGN_SID, "tool": "c"},
                title="custom-title",
                saved_codex_ids=set(),
                plan_json=True,
                current_command="claude",
                full_command=(
                    "claude --dangerously-skip-permissions "
                    f"--session-id {FOREIGN_SID}"
                ),
                extra_env={"CLAUDE_CONFIG_DIR": str(claude_config)},
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            plan = json.loads(result.stdout)
            self.assertEqual(plan["kind"], "resume")
            self.assertTrue(plan["restorable"])

    def test_restore_recreates_empty_pi_session_without_claiming_resume(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            pi_agent_dir = tmp / "pi-agent"
            pi_agent_dir.mkdir()
            result = self.run_restore(
                tmp,
                coord={"sid": VALID_PI_SID, "tool": "p"},
                title="pi",
                saved_codex_ids=set(),
                plan_json=True,
                current_command="node",
                full_command="pi",
                extra_env={"PI_CODING_AGENT_DIR": str(pi_agent_dir)},
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            plan = json.loads(result.stdout)
            self.assertEqual(plan["tool"], "pi")
            self.assertEqual(plan["kind"], "recreated")
            self.assertTrue(plan["restorable"])
            self.assertEqual(plan["command"], f"pi --session-id {VALID_PI_SID}")

    def test_restore_prefers_current_pi_binding_over_stale_launch_id(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            pi_agent_dir = tmp / "pi-agent"
            self.write_pi_session(pi_agent_dir, FOREIGN_SID, tmp)
            self.write_pi_session(pi_agent_dir, VALID_PI_SID, tmp)
            result = self.run_restore(
                tmp,
                coord={"sid": VALID_PI_SID, "tool": "p"},
                title="pi",
                saved_codex_ids=set(),
                plan_json=True,
                current_command="node",
                full_command=f"pi --session-id {FOREIGN_SID}",
                extra_env={"PI_CODING_AGENT_DIR": str(pi_agent_dir)},
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            plan = json.loads(result.stdout)
            self.assertEqual(plan["kind"], "resume")
            self.assertTrue(plan["restorable"])
            self.assertEqual(plan["command"], f"pi --session-id {VALID_PI_SID}")

    def test_restore_recreates_current_empty_pi_binding_instead_of_stale_argv(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            pi_agent_dir = tmp / "pi-agent"
            self.write_pi_session(pi_agent_dir, FOREIGN_SID, tmp)
            result = self.run_restore(
                tmp,
                coord={"sid": VALID_PI_SID, "tool": "p"},
                title="pi",
                saved_codex_ids=set(),
                plan_json=True,
                current_command="node",
                full_command=f"pi --session-id {FOREIGN_SID}",
                extra_env={"PI_CODING_AGENT_DIR": str(pi_agent_dir)},
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            plan = json.loads(result.stdout)
            self.assertEqual(plan["kind"], "recreated")
            self.assertTrue(plan["restorable"])
            self.assertEqual(plan["command"], f"pi --session-id {VALID_PI_SID}")

    def test_restore_rejects_pi_session_from_a_different_project(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            pi_agent_dir = tmp / "pi-agent"
            self.write_pi_session(pi_agent_dir, VALID_PI_SID, tmp / "other-project")
            result = self.run_restore(
                tmp,
                coord={"sid": VALID_PI_SID, "tool": "p"},
                title="pi",
                saved_codex_ids=set(),
                plan_json=True,
                current_command="node",
                full_command="pi",
                extra_env={"PI_CODING_AGENT_DIR": str(pi_agent_dir)},
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            plan = json.loads(result.stdout)
            self.assertEqual(plan["kind"], "invalid")
            self.assertFalse(plan["restorable"])

    def test_restore_prefers_current_codex_binding_over_stale_resume_argv(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            result = self.run_restore(
                tmp,
                coord={"sid": VALID_CODEX_SID, "tool": "x"},
                title=VALID_CODEX_SID,
                saved_codex_ids={VALID_CODEX_SID, FOREIGN_SID},
                plan_json=True,
                full_command=(
                    "codex --yolo --disable plugins "
                    f"resume {FOREIGN_SID}"
                ),
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            plan = json.loads(result.stdout)
            self.assertEqual(
                plan["command"],
                "codex --yolo --disable plugins "
                f"-c check_for_update_on_startup=false resume {VALID_CODEX_SID}",
            )

    def test_restore_falls_back_to_codex_argv_when_binding_is_not_durable(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            result = self.run_restore(
                tmp,
                coord={"sid": FOREIGN_SID, "tool": "x"},
                title="custom-title",
                saved_codex_ids={VALID_CODEX_SID},
                plan_json=True,
                full_command=f"codex --yolo resume {VALID_CODEX_SID}",
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            plan = json.loads(result.stdout)
            self.assertEqual(
                plan["command"],
                "codex --yolo --disable plugins "
                f"-c check_for_update_on_startup=false resume {VALID_CODEX_SID}",
            )

    def test_restore_exposes_machine_readable_recovery_plan(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            result = self.run_restore(
                tmp,
                coord={"sid": VALID_CODEX_SID, "tool": "x"},
                title="custom-title",
                saved_codex_ids={VALID_CODEX_SID},
                plan_json=True,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            plan = json.loads(result.stdout)
            self.assertEqual(plan["target"], "0:4.1")
            self.assertEqual(plan["tool"], "codex")
            self.assertEqual(plan["kind"], "resume")
            self.assertTrue(plan["restorable"])
            self.assertEqual(plan["cwd"], str(tmp))
            self.assertEqual(
                plan["command"],
                "codex --yolo --disable plugins "
                f"-c check_for_update_on_startup=false resume {VALID_CODEX_SID}",
            )

    def test_successful_restore_rebinds_session_to_the_new_pane_id(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            home = tmp / "home"
            local_bin = home / ".local" / "bin"
            local_bin.mkdir(parents=True)
            tmux_log = tmp / "tmux.log"
            bind_log = tmp / "bind.log"
            pane_command = tmp / "pane-command"
            pane_command.write_text("zsh\n", encoding="utf-8")

            fake_tmux = local_bin / "tmux"
            fake_tmux.write_text(
                textwrap.dedent(
                    """\
                    #!/bin/sh
                    case "$1" in
                      has-session)
                        exit 0
                        ;;
                      display-message)
                        case "$*" in
                          *pane_current_command*) cat "$PANE_COMMAND" ;;
                          *pane_id*socket_path*pid*) printf '%%42\\t/private/tmp/tmux-test\\t4242\\n' ;;
                          *pane_id*) printf '%%42\\n' ;;
                          *socket_path*) printf '/private/tmp/tmux-test\\n' ;;
                          *"#{pid}"*) printf '4242\\n' ;;
                        esac
                        ;;
                      send-keys)
                        printf '%s\\n' "$*" >> "$TMUX_LOG"
                        case "$*" in
                          *Enter*) printf 'codex\\n' > "$PANE_COMMAND" ;;
                        esac
                        ;;
                      *)
                        exit 99
                        ;;
                    esac
                    """
                ),
                encoding="utf-8",
            )
            fake_tmux.chmod(0o755)

            fake_bind = local_bin / "aipane-bind"
            fake_bind.write_text(
                "#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$BIND_LOG\"\n",
                encoding="utf-8",
            )
            fake_bind.chmod(0o755)

            result = self.run_restore(
                tmp,
                coord={"sid": VALID_CODEX_SID, "tool": "x"},
                title="custom-title",
                saved_codex_ids={VALID_CODEX_SID},
                dry_run=False,
                extra_env={
                    "HOME": str(home),
                    "AIPANE_TMUX": str(fake_tmux),
                    "TMUX_LOG": str(tmux_log),
                    "BIND_LOG": str(bind_log),
                    "PANE_COMMAND": str(pane_command),
                    "AIPANE_BIND_COMMAND": str(fake_bind),
                    "AI_RESTORE_SETTLE_DELAY": "0",
                    "AI_RESTORE_MAX_ATTEMPTS": "1",
                    "AI_RESTORE_VERIFY_TIMEOUT": "0.3",
                    "AI_RESTORE_VERIFY_STABILITY": "0.03",
                    "AI_RESTORE_POLL_INTERVAL": "0.01",
                },
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(
                "send-keys -l -t 0:4.1 codex --yolo --disable plugins "
                f"-c check_for_update_on_startup=false resume {VALID_CODEX_SID}",
                tmux_log.read_text(),
            )
            binding = bind_log.read_text()
            self.assertIn(f"--tool x --sid {VALID_CODEX_SID}", binding)
            self.assertIn("--pane %42", binding)

    def run_restore(
        self,
        tmp: Path,
        *,
        coord: dict[str, str],
        title: str,
        saved_codex_ids: set[str],
        dry_run: bool = True,
        plan_json: bool = False,
        extra_env: dict[str, str] | None = None,
        current_command: str = "codex",
        full_command: str = "codex --yolo",
    ) -> subprocess.CompletedProcess[str]:
        state_dir = tmp / "state"
        state_dir.mkdir()
        (state_dir / "coords-last.json").write_text(
            json.dumps({"0:4.1": coord}),
            encoding="utf-8",
        )

        codex_home = tmp / "codex"
        sessions_dir = codex_home / "sessions" / "2026" / "08" / "09"
        sessions_dir.mkdir(parents=True)
        for sid in saved_codex_ids:
            (sessions_dir / f"rollout-2026-08-09T00-00-00-{sid}.jsonl").touch()

        dump = tmp / "resurrect.txt"
        dump.write_text(
            "\t".join(
                [
                    "pane",
                    "0",
                    "4",
                    "0",
                    ":-",
                    "1",
                    title,
                    f":{tmp}",
                    "1",
                    current_command,
                    f":{full_command}",
                ]
            )
            + "\n",
            encoding="utf-8",
        )

        env = os.environ.copy()
        env.update(
            {
                "AIPANE_STATE_DIR": str(state_dir),
                "AIPANE_CODEX_LAUNCH_CMD": "codex --yolo --disable plugins",
                "CODEX_HOME": str(codex_home),
            }
        )
        if extra_env:
            env.update(extra_env)
        args = ["zsh", str(RESTORE)]
        if plan_json:
            args.append("--plan-json")
        elif dry_run:
            args.append("--dry-run")
        args.extend(["--dump", str(dump)])
        return subprocess.run(
            args,
            check=False,
            capture_output=True,
            text=True,
            env=env,
        )

    def claude_config(self, tmp: Path, *, conversation_exists: bool) -> Path:
        config = tmp / "claude"
        project = config / "projects" / "-Users-test-project"
        project.mkdir(parents=True)
        if conversation_exists:
            (project / f"{FOREIGN_SID}.jsonl").touch()
        return config

    def write_pi_session(self, agent_dir: Path, sid: str, cwd: Path) -> Path:
        project = agent_dir / "sessions" / "--fixture-project--"
        project.mkdir(parents=True, exist_ok=True)
        session = project / f"2026-08-16T09-01-27-117Z_{sid}.jsonl"
        session.write_text(
            json.dumps(
                {
                    "type": "session",
                    "version": 3,
                    "id": sid,
                    "timestamp": "2026-08-16T09:01:27.117Z",
                    "cwd": str(cwd),
                }
            )
            + "\n",
            encoding="utf-8",
        )
        return session


if __name__ == "__main__":
    unittest.main()
