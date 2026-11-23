"""Regression tests for tmux server-scoped AI session restoration."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
import urllib.parse
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))
from recovery_snapshot import capture_snapshot
SNAPSHOT = ROOT / "bin" / "aipane-snapshot"
RESTORE = ROOT / "bin" / "ai-restore"

VALID_CODEX_SID = "019fdad7-16c3-7a13-89cf-ec8c2184e5f7"
FOREIGN_SID = "1fa8ab6a-cba1-4189-abcd-64a29ffb2fc3"
VALID_PI_SID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
VALID_GROK_SID = "11111111-2222-4333-8444-555555555555"


class SessionRestoreTests(unittest.TestCase):
    def test_restore_ignores_stale_agent_command_in_saved_shell(self):
        # A real dump saved foreground zsh with the same Codex resume command
        # as another pane. Restoring it launched a duplicate conversation.
        for current in ("zsh", "-zsh", "bash", "-bash", "sh", "-sh", "fish", "-fish",
                        "dash", "ksh", "csh", "tcsh"):
            for bind_snapshot in (False, True):
                with self.subTest(current=current, bind_snapshot=bind_snapshot):
                    with tempfile.TemporaryDirectory() as raw_tmp:
                        result = self.run_restore(
                            Path(raw_tmp),
                            coord={"sid": VALID_CODEX_SID, "tool": "x"},
                            title="alfheim@Alfheim:~",
                            saved_codex_ids={VALID_CODEX_SID},
                            current_command=current,
                            full_command=f"codex resume {VALID_CODEX_SID}",
                            bind_snapshot=bind_snapshot,
                            plan_json=True,
                        )
                        self.assertEqual(result.returncode, 0, result.stderr)
                        self.assertEqual(result.stdout.strip(), "")

    def test_retired_tools_in_existing_dumps_are_skipped_even_with_fresh_restore(self):
        for tag, current, command in [
            ("o", "opencode", "opencode --session old"),
            ("r", "node", "node /opt/cursor-agent/index.js --resume old"),
            ("q", "node", "node /opt/@qwen-code/qwen-code/cli.js --resume old"),
        ]:
            with self.subTest(tag=tag), tempfile.TemporaryDirectory() as directory:
                result = self.run_restore(
                    Path(directory), coord={"sid": FOREIGN_SID, "tool": tag},
                    title=FOREIGN_SID, saved_codex_ids=set(), current_command=current,
                    full_command=command, plan_json=True, extra_env={"AI_RESTORE_FRESH": "1"},
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.strip(), "")

    def test_antigravity_pane_without_snapshot_metadata_is_reported_as_invalid(self):
        with tempfile.TemporaryDirectory() as directory:
            result = self.run_restore(
                Path(directory), coord={}, title="agy", saved_codex_ids=set(),
                current_command="agy", full_command="agy", bind_snapshot=False, plan_json=True,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        item = json.loads(result.stdout)
        self.assertEqual((item["tool"], item["kind"], item["restorable"]), ("agy", "invalid", False))

    def test_unbound_coordinates_cannot_override_an_older_dump(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            result = self.run_restore(
                Path(raw_tmp), coord={"sid": FOREIGN_SID, "tool": "x"},
                title="custom-title", saved_codex_ids={VALID_CODEX_SID, FOREIGN_SID},
                full_command=f"codex resume {VALID_CODEX_SID}", plan_json=True,
                bind_snapshot=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["sid"], VALID_CODEX_SID)

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

    def test_restore_keeps_codex_session_with_suffixed_rollout(self):
        for root in ("sessions/2026/08/09", "archived_sessions"):
            with self.subTest(root=root), tempfile.TemporaryDirectory() as raw_tmp:
                result = self.run_restore(
                    Path(raw_tmp),
                    coord={},
                    title="custom-title",
                    saved_codex_ids=set(),
                    saved_codex_records={
                        f"{root}/rollout-2026-08-09T00-00-00-{VALID_CODEX_SID}_{FOREIGN_SID}.jsonl":
                            json.dumps({"type": "session_meta", "payload": {"id": VALID_CODEX_SID}}),
                    },
                    plan_json=True,
                    full_command=f"codex --yolo resume {VALID_CODEX_SID}",
                )

                self.assertEqual(result.returncode, 0, result.stderr)
                plan = json.loads(result.stdout)
                self.assertTrue(plan["restorable"])
                self.assertEqual(plan["kind"], "resume")
                self.assertEqual(plan["sid"], VALID_CODEX_SID)

    def test_restore_prefers_suffixed_codex_binding_over_stale_resume_argv(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            result = self.run_restore(
                Path(raw_tmp),
                coord={"sid": VALID_CODEX_SID, "tool": "x"},
                title="custom-title",
                saved_codex_ids={FOREIGN_SID},
                saved_codex_records={
                    f"sessions/rollout-2026-08-09T00-00-00-{VALID_CODEX_SID}_{FOREIGN_SID}.jsonl":
                        json.dumps({"type": "session_meta", "payload": {"id": VALID_CODEX_SID}}),
                },
                plan_json=True,
                full_command=f"codex --yolo resume {FOREIGN_SID}",
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            plan = json.loads(result.stdout)
            self.assertTrue(plan["restorable"])
            self.assertEqual(plan["sid"], VALID_CODEX_SID)
            self.assertTrue(plan["command"].endswith(f"resume {VALID_CODEX_SID}"))

    def test_restore_rejects_codex_rollout_with_invalid_metadata(self):
        for suffix in ("", f"_{FOREIGN_SID}"):
            for content in (
                "not json",
                json.dumps({"type": "session_meta", "payload": {"id": FOREIGN_SID}}),
                json.dumps({"type": "event_msg", "payload": {"id": VALID_CODEX_SID}}),
            ):
                with self.subTest(suffix=suffix, content=content), tempfile.TemporaryDirectory() as raw_tmp:
                    result = self.run_restore(
                        Path(raw_tmp),
                        coord={},
                        title="custom-title",
                        saved_codex_ids=set(),
                        saved_codex_records={
                            f"sessions/rollout-2026-08-09T00-00-00-{VALID_CODEX_SID}{suffix}.jsonl": content,
                        },
                        plan_json=True,
                        full_command=f"codex --yolo resume {VALID_CODEX_SID}",
                    )

                    self.assertEqual(result.returncode, 0, result.stderr)
                    plan = json.loads(result.stdout)
                    self.assertEqual(plan["kind"], "invalid")
                    self.assertFalse(plan["restorable"])

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

    def test_claude_resume_plan_marks_automatic_recovery(self):
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
                full_command=f"claude --resume {FOREIGN_SID}",
                extra_env={
                    "CLAUDE_CONFIG_DIR": str(claude_config),
                    "AIPANE_CLAUDE_LAUNCH_CMD": "/tmp/claude-window --dangerously-skip-permissions",
                },
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            plan = json.loads(result.stdout)
            self.assertTrue(plan["restorable"])
            self.assertEqual(plan["command"],
                             f"AIPANE_AUTO_RESUME=1 /tmp/claude-window --dangerously-skip-permissions --resume {FOREIGN_SID}")

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

    def test_restore_prefers_current_grok_binding_over_stale_argv(self):
        for flag in ("--session-id", "--resume"):
            with self.subTest(flag=flag), tempfile.TemporaryDirectory() as raw_tmp:
                tmp = Path(raw_tmp)
                grok_home = tmp / "grok-home"
                self.write_grok_session(grok_home, FOREIGN_SID, tmp)
                self.write_grok_session(grok_home, VALID_GROK_SID, tmp)
                result = self.run_restore(
                    tmp, coord={"sid": VALID_GROK_SID, "tool": "g"},
                    title="grok", saved_codex_ids=set(), plan_json=True,
                    current_command="grok-1.0.13-mac",
                    full_command=f"grok {flag} {FOREIGN_SID}",
                    extra_env={"GROK_HOME": str(grok_home)},
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                plan = json.loads(result.stdout)
                self.assertEqual(plan["kind"], "resume")
                self.assertTrue(plan["restorable"])
                self.assertEqual(plan["sid"], VALID_GROK_SID)
                self.assertEqual(
                    plan["command"], f"grok --always-approve --resume {VALID_GROK_SID}"
                )

    def test_restore_rejects_unavailable_current_grok_session_without_using_old_id(self):
        for missing in ("session", "transcript", "cwd", "metadata"):
            with self.subTest(missing=missing), tempfile.TemporaryDirectory() as raw_tmp:
                tmp = Path(raw_tmp)
                grok_home = tmp / "grok-home"
                self.write_grok_session(grok_home, FOREIGN_SID, tmp)
                if missing != "session":
                    current = self.write_grok_session(
                        grok_home, VALID_GROK_SID,
                        tmp / "other-project" if missing == "cwd" else tmp,
                    )
                    if missing == "transcript":
                        (current / "updates.jsonl").unlink()
                    elif missing == "metadata":
                        (current / "summary.json").write_text("{}", encoding="utf-8")
                result = self.run_restore(
                    tmp, coord={"sid": VALID_GROK_SID, "tool": "g"},
                    title="grok", saved_codex_ids=set(), plan_json=True,
                    current_command="grok",
                    full_command=f"grok --session-id {FOREIGN_SID}",
                    extra_env={"GROK_HOME": str(grok_home)},
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                plan = json.loads(result.stdout)
                self.assertEqual(plan["sid"], VALID_GROK_SID)
                self.assertEqual(plan["kind"], "invalid")
                self.assertFalse(plan["restorable"])

    def test_restore_uses_durable_grok_argv_without_a_current_binding(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            grok_home = tmp / "grok-home"
            self.write_grok_session(grok_home, FOREIGN_SID, tmp)
            result = self.run_restore(
                tmp, coord={}, title="grok", saved_codex_ids=set(), plan_json=True,
                current_command="grok", full_command=f"grok --resume {FOREIGN_SID}",
                extra_env={"GROK_HOME": str(grok_home)},
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            plan = json.loads(result.stdout)
            self.assertEqual(plan["sid"], FOREIGN_SID)
            self.assertEqual(plan["kind"], "resume")

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

    def test_restore_does_not_rebind_codex_without_loaded_session_evidence(self):
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
                          *socket_path*pid*) printf '/private/tmp/tmux-test\\t4242\\n' ;;
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
                    "AI_RESTORE_CODEX_VERIFY_TIMEOUT": "0.3",
                    "AI_RESTORE_VERIFY_STABILITY": "0.03",
                    "AI_RESTORE_POLL_INTERVAL": "0.01",
                },
            )

            self.assertNotEqual(result.returncode, 0, result.stdout)
            self.assertIn(
                f"send-keys -l -t 0:4.1 cd -- {tmp} && CODEX_HOME={tmp.resolve() / 'codex'} codex --yolo --disable plugins "
                f"-c check_for_update_on_startup=false resume {VALID_CODEX_SID}",
                tmux_log.read_text(),
            )
            self.assertFalse(bind_log.exists())
            pending = json.loads((tmp / "state/restore-pending.json").read_text())["items"]
            self.assertEqual([(item["sid"], item["pane_id"]) for item in pending],
                             [(VALID_CODEX_SID, "%42")])

    def test_restore_resumes_double_uuid_pi_session_with_cwd_verification(self):
        import shlex
        import select
        import time

        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp).resolve()
            tmp_native = tmp / "native"
            tmp_renamed = tmp / "renamed"
            tmp_bad = tmp / "bad"
            tmp_corrupt = tmp / "corrupt"
            for d in (tmp_native, tmp_renamed, tmp_bad, tmp_corrupt):
                d.mkdir()
            pi_dir = tmp / "pi-agent"
            pi_sid = "87878787-8787-7787-8787-878787878787"
            extra_uuid = "98989898-9898-7898-8898-989898989898"

            # 1. Native encoded cwd dir -> `pi --session-id <sid>`
            native_dir = pi_dir / "sessions" / self._pi_encoded_dir(tmp_native)
            native_dir.mkdir(parents=True, exist_ok=True)
            native_file = native_dir / f"2026-11-23T09-01-27-117Z_{pi_sid}_{extra_uuid}.jsonl"
            native_file.write_text(
                "\n".join(
                    [
                        json.dumps({"type": "session", "version": 3, "id": pi_sid, "timestamp": "2026-11-23T09:01:27.117Z", "cwd": str(tmp_native)}),
                        json.dumps({"type": "message", "id": "aabbccdd", "parentId": None, "timestamp": "2026-11-23T09:02:27.117Z", "message": {"role": "user", "content": [{"type": "text", "text": "PI_NATIVE_MARKER"}], "timestamp": 1795400000000}}),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            ok = self.run_restore(
                tmp_native,
                coord={"sid": pi_sid, "tool": "p"},
                title="pi",
                saved_codex_ids=set(),
                plan_json=True,
                current_command="pi",
                full_command="pi",
                extra_env={"PI_CODING_AGENT_DIR": str(pi_dir)},
            )
            self.assertEqual(ok.returncode, 0, ok.stderr)
            plan = json.loads(ok.stdout.strip().splitlines()[-1])
            self.assertEqual(plan["kind"], "resume")
            self.assertEqual(plan["command"], f"pi --session-id {pi_sid}")

            # 2. Non-default/renamed project dir with matching header id & cwd -> `pi --session <path>`
            pi_sid2 = "88888888-8888-7888-8888-888888888888"
            renamed_dir = pi_dir / "sessions" / "--fixture-project--"
            renamed_dir.mkdir(parents=True, exist_ok=True)
            renamed_file = renamed_dir / f"2026-11-23T09-01-27-117Z_{pi_sid2}_{extra_uuid}.jsonl"
            renamed_file.write_text(
                "\n".join(
                    [
                        json.dumps({"type": "session", "version": 3, "id": pi_sid2, "timestamp": "2026-11-23T09:01:27.117Z", "cwd": str(tmp_renamed)}),
                        json.dumps({"type": "message", "id": "ddeeff00", "parentId": None, "timestamp": "2026-11-23T09:02:27.117Z", "message": {"role": "user", "content": [{"type": "text", "text": "PI_RENAMED_MARKER"}], "timestamp": 1795400000000}}),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            ok2 = self.run_restore(
                tmp_renamed,
                coord={"sid": pi_sid2, "tool": "p"},
                title="pi",
                saved_codex_ids=set(),
                plan_json=True,
                current_command="pi",
                full_command="pi",
                extra_env={"PI_CODING_AGENT_DIR": str(pi_dir)},
            )
            self.assertEqual(ok2.returncode, 0, ok2.stderr)
            plan2 = json.loads(ok2.stdout.strip().splitlines()[-1])
            self.assertEqual(plan2["kind"], "resume")
            self.assertIn("--session ", plan2["command"])
            self.assertIn(str(renamed_file.resolve()), plan2["command"])

            # End-to-end verification with real Pi RPC when installed: both planned resume commands load original messages
            pi_bin = Path("/opt/homebrew/bin/pi")
            if pi_bin.is_file():
                for work_cwd, planned_cmd, marker in (
                    (tmp_native, plan["command"], b"PI_NATIVE_MARKER"),
                    (tmp_renamed, plan2["command"], b"PI_RENAMED_MARKER"),
                ):
                    cmd_parts = shlex.split(planned_cmd)
                    rpc_cmd = [str(pi_bin), "--mode", "rpc", "--offline", "--no-extensions", "--no-skills", *cmd_parts[1:]]
                    rpc_env = {
                        "HOME": str(tmp),
                        "PATH": os.environ["PATH"],
                        "PI_CODING_AGENT_DIR": str(pi_dir),
                        "PI_OFFLINE": "1",
                        "PI_SKIP_VERSION_CHECK": "1",
                        "XDG_CONFIG_HOME": str(tmp / ".config"),
                        "TERM": "dumb",
                    }
                    proc = subprocess.Popen(rpc_cmd, cwd=work_cwd, env=rpc_env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                    try:
                        assert proc.stdin is not None and proc.stdout is not None
                        proc.stdin.write(b'{"type":"get_messages","id":"m"}\n')
                        proc.stdin.flush()
                        out = b""
                        deadline = time.monotonic() + 10
                        while time.monotonic() < deadline:
                            rlist, _, _ = select.select([proc.stdout], [], [], max(0.0, deadline - time.monotonic()))
                            if not rlist:
                                break
                            chunk = os.read(proc.stdout.fileno(), 65536)
                            if not chunk:
                                break
                            out += chunk
                            if b'"command":"get_messages"' in out:
                                break
                    finally:
                        proc.terminate()
                        try:
                            more, err = proc.communicate(timeout=3)
                        except subprocess.TimeoutExpired:
                            proc.kill()
                            more, err = proc.communicate()
                    self.assertIn(marker, out + more, f"planned command {planned_cmd!r} failed to load history; stderr={err.decode(errors='replace')}")

            # 3. Wrong cwd -> invalid (must NOT silently recreate over a durable session)
            bad_cwd = self.run_restore(
                tmp_bad,
                coord={"sid": pi_sid, "tool": "p"},
                title="pi",
                saved_codex_ids=set(),
                plan_json=True,
                current_command="pi",
                full_command="pi",
                extra_env={"PI_CODING_AGENT_DIR": str(pi_dir)},
            )
            self.assertEqual(bad_cwd.returncode, 0, bad_cwd.stderr)
            bad_plan = json.loads(bad_cwd.stdout.strip().splitlines()[-1])
            self.assertEqual(bad_plan["kind"], "invalid")

            # 4. Corrupt Pi file missing "id" -> invalid (must NOT classify as recreated)
            corrupt_sid = "89898989-8989-7989-8989-898989898989"
            corrupt_dir = pi_dir / "sessions" / self._pi_encoded_dir(tmp_corrupt)
            corrupt_dir.mkdir(parents=True, exist_ok=True)
            (corrupt_dir / f"2026-11-23T09-01-27-117Z_{corrupt_sid}.jsonl").write_text(
                json.dumps({"type": "session", "version": 3, "cwd": str(tmp_corrupt)}) + "\n",
                encoding="utf-8",
            )
            corrupt_res = self.run_restore(
                tmp_corrupt,
                coord={"sid": corrupt_sid, "tool": "p"},
                title="pi",
                saved_codex_ids=set(),
                plan_json=True,
                current_command="pi",
                full_command="pi",
                extra_env={"PI_CODING_AGENT_DIR": str(pi_dir)},
            )
            self.assertEqual(corrupt_res.returncode, 0, corrupt_res.stderr)
            corrupt_plan = json.loads(corrupt_res.stdout.strip().splitlines()[-1])
            self.assertEqual(corrupt_plan["kind"], "invalid")

    def test_r2_p1_8_codex_sqlite_only_binding_resumes_and_broken_binding_blocks_stale_argv(self):
        from contextlib import closing
        import sqlite3

        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp).resolve()
            # Case A: bound B lives in future_threads/ reachable only via state_5.sqlite; stale argv has durable A
            case_a = tmp / "case_a"
            case_a.mkdir()
            chome_a = case_a / "codex"
            future_b = chome_a / "future_threads" / f"rollout-2026-11-23T10-00-00-{FOREIGN_SID}.jsonl"
            future_b.parent.mkdir(parents=True, exist_ok=True)
            future_b.write_text(json.dumps({"type": "session_meta", "payload": {"id": FOREIGN_SID}}) + "\n", encoding="utf-8")
            with closing(sqlite3.connect(chome_a / "state_5.sqlite")) as conn, conn:
                conn.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, rollout_path TEXT, updated_at INTEGER)")
                conn.execute("INSERT INTO threads VALUES (?, ?, ?)", (FOREIGN_SID, str(future_b), 1_795_400_000))

            res_a = self.run_restore(
                case_a,
                coord={"sid": FOREIGN_SID, "tool": "x"},
                title="codex",
                saved_codex_ids={VALID_CODEX_SID},
                plan_json=True,
                full_command=f"codex --yolo resume {VALID_CODEX_SID}",
            )
            self.assertEqual(res_a.returncode, 0, res_a.stderr)
            plan_a = json.loads(res_a.stdout.strip().splitlines()[-1])
            self.assertEqual(plan_a["kind"], "resume")
            self.assertEqual(plan_a["sid"], FOREIGN_SID)

            # Case B: bound B is recorded in SQLite with a missing rollout -> must mark B invalid, NEVER fall back to A
            case_b = tmp / "case_b"
            case_b.mkdir()
            chome_b = case_b / "codex"
            chome_b.mkdir(parents=True, exist_ok=True)
            with closing(sqlite3.connect(chome_b / "state_5.sqlite")) as conn, conn:
                conn.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, rollout_path TEXT, updated_at INTEGER)")
                conn.execute("INSERT INTO threads VALUES (?, ?, ?)", (FOREIGN_SID, str(chome_b / "future_threads/missing.jsonl"), 1_795_400_000))

            res_b = self.run_restore(
                case_b,
                coord={"sid": FOREIGN_SID, "tool": "x"},
                title="codex",
                saved_codex_ids={VALID_CODEX_SID},
                plan_json=True,
                full_command=f"codex --yolo resume {VALID_CODEX_SID}",
            )
            self.assertEqual(res_b.returncode, 0, res_b.stderr)
            plan_b = json.loads(res_b.stdout.strip().splitlines()[-1])
            self.assertEqual(plan_b["kind"], "invalid")
            self.assertEqual(plan_b["sid"], FOREIGN_SID)

    def test_restore_accepts_codex_header_larger_than_512kb(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            big_header = json.dumps(
                {
                    "type": "session_meta",
                    "payload": {
                        "id": VALID_CODEX_SID,
                        "base_instructions": "Z" * 540_000,
                    },
                }
            )
            result = self.run_restore(
                tmp,
                coord={"sid": VALID_CODEX_SID, "tool": "x"},
                title="codex",
                saved_codex_ids=set(),
                saved_codex_records={
                    f"sessions/2026/11/23/rollout-2026-11-23T10-00-00-{VALID_CODEX_SID}.jsonl": big_header
                },
                plan_json=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            plan = json.loads(result.stdout.strip().splitlines()[-1])
            self.assertEqual(plan["kind"], "resume")
            self.assertEqual(plan["sid"], VALID_CODEX_SID)

    def run_restore(
        self,
        tmp: Path,
        *,
        coord: dict[str, str],
        title: str,
        saved_codex_ids: set[str],
        saved_codex_records: dict[str, str] | None = None,
        dry_run: bool = True,
        plan_json: bool = False,
        extra_env: dict[str, str] | None = None,
        current_command: str = "codex",
        full_command: str = "codex --yolo",
        bind_snapshot: bool = True,
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
            (sessions_dir / f"rollout-2026-08-09T00-00-00-{sid}.jsonl").write_text(
                json.dumps({"type": "session_meta", "payload": {"id": sid}}) + "\n",
                encoding="utf-8",
            )
        for relative_path, content in (saved_codex_records or {}).items():
            rollout = codex_home / relative_path
            rollout.parent.mkdir(parents=True, exist_ok=True)
            rollout.write_text(content + "\n", encoding="utf-8")

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
        if bind_snapshot:
            capture_snapshot(dump, {"0:4.1": coord}, ("/tmp/test", "123"))
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

    @staticmethod
    def _pi_encoded_dir(cwd: Path) -> str:
        resolved = os.path.realpath(str(cwd))
        return "--" + resolved.lstrip("/\\").replace("/", "-").replace("\\", "-").replace(":", "-") + "--"

    def write_pi_session(self, agent_dir: Path, sid: str, cwd: Path) -> Path:
        project = agent_dir / "sessions" / self._pi_encoded_dir(cwd)
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

    def write_grok_session(self, grok_home: Path, sid: str, cwd: Path) -> Path:
        session = grok_home / "sessions" / urllib.parse.quote(str(cwd), safe="") / sid
        session.mkdir(parents=True, exist_ok=True)
        (session / "summary.json").write_text(
            json.dumps({"info": {"id": sid, "cwd": str(cwd)}}), encoding="utf-8",
        )
        (session / "updates.jsonl").write_text(json.dumps({
            "params": {"sessionId": sid, "update": {
                "sessionUpdate": "user_message_chunk",
                "content": {"type": "text", "text": "Saved conversation"},
            }},
        }) + "\n", encoding="utf-8")
        return session

    def test_r3_p1_codex_nonstandard_filename_invalid_and_pi_explicit_session_dir_and_opaque(self):
        import select
        import shlex
        import time

        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp).resolve()
            home = root / "home"
            home.mkdir()

            # 1. Codex bound to B stored under non-standard filename must stay invalid (never fall back to argv A)
            cwd_codex = root / "codex-work"
            cwd_codex.mkdir()
            sid_a = "11111111-1111-7111-8111-111111111111"
            sid_b = "22222222-2222-7222-8222-222222222222"
            nonstd_record = (
                json.dumps({"type": "session_meta", "payload": {"id": sid_b, "cwd": str(cwd_codex)}}) + "\n"
                + json.dumps({"type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "THIS_IS_B"}]}}) + "\n"
            )
            res_c = self.run_restore(
                cwd_codex,
                coord={"sid": sid_b, "tool": "x"},
                title="codex",
                saved_codex_ids={sid_a},
                saved_codex_records={"sessions/2026/11/23/custom_log_name.jsonl": nonstd_record},
                plan_json=True,
                current_command="codex",
                full_command=f"codex resume {sid_a}",
                extra_env={"HOME": str(home)},
            )
            self.assertEqual(res_c.returncode, 0, res_c.stderr)
            plan_c = json.loads(res_c.stdout.strip().splitlines()[-1])
            self.assertEqual(plan_c["kind"], "invalid")
            self.assertFalse(plan_c["restorable"])
            self.assertEqual(plan_c["sid"], sid_b)

            # 2. Pi explicit PI_CODING_AGENT_SESSION_DIR (encoded subdir -> --session <path>; flat -> --session-id)
            #    AND opaque.jsonl in renamed folder (-> --session <path>)
            pi_sid = "89898989-8989-7898-8898-898989898989"
            pi_rid = "97979797-9797-7979-8979-979797979797"
            pi_bin = Path("/opt/homebrew/bin/pi")
            for mode in ("explicit_encoded_subdir", "flat_override", "opaque_renamed"):
                work = root / f"pi-{mode}"
                work.mkdir()
                pi_agent = root / f"agent-{mode}"
                sessions = pi_agent / "sessions"
                if mode == "explicit_encoded_subdir":
                    target_dir = sessions / self._pi_encoded_dir(work)
                    fname = f"2026-11-23T09-01-27-117Z_{pi_sid}_{pi_rid}.jsonl"
                elif mode == "flat_override":
                    target_dir = sessions
                    fname = f"2026-11-23T09-01-27-117Z_{pi_sid}_{pi_rid}.jsonl"
                else:
                    target_dir = sessions / "--renamed--"
                    fname = "opaque.jsonl"
                target_dir.mkdir(parents=True, exist_ok=True)
                fpath = target_dir / fname
                fpath.write_text(
                    json.dumps({"type": "session", "version": 3, "id": pi_sid, "cwd": str(work), "timestamp": "2026-11-23T09:01:27.117Z"}) + "\n"
                    + json.dumps({"type": "message", "id": "abcdef01", "parentId": None, "timestamp": "2026-11-23T09:02:27.117Z", "message": {"role": "user", "content": [{"type": "text", "text": f"PI_MARKER_{mode}"}], "timestamp": 1795400000000}}) + "\n",
                    encoding="utf-8",
                )
                extra = {
                    "HOME": str(home),
                    "PI_CODING_AGENT_DIR": str(pi_agent),
                    "PI_OFFLINE": "1",
                    "PI_SKIP_VERSION_CHECK": "1",
                    "XDG_CONFIG_HOME": str(home / ".config"),
                    "TERM": "dumb",
                    "AIPANE_PI_LAUNCH_CMD": "pi",
                }
                if mode in ("explicit_encoded_subdir", "flat_override"):
                    extra["PI_CODING_AGENT_SESSION_DIR"] = str(sessions)
                res_p = self.run_restore(
                    work,
                    coord={"sid": pi_sid, "tool": "p"},
                    title="pi",
                    saved_codex_ids=set(),
                    plan_json=True,
                    current_command="pi",
                    full_command="pi",
                    extra_env=extra,
                )
                self.assertEqual(res_p.returncode, 0, res_p.stderr)
                plan_p = json.loads(res_p.stdout.strip().splitlines()[-1])
                self.assertEqual(plan_p["kind"], "resume", mode)
                self.assertTrue(plan_p["restorable"], mode)
                # Explicit PI_CODING_AGENT_SESSION_DIR and opaque/renamed layouts always
                # pin `--session <verified_path>` so target shells without planner-only
                # env vars or with duplicate SIDs open the exact verified file.
                self.assertIn("--session ", plan_p["command"], mode)
                if pi_bin.is_file():
                    rpc_args = shlex.split(plan_p["command"])[1:]
                    proc = subprocess.Popen(
                        [str(pi_bin), "--mode", "rpc", "--offline", "--no-extensions", "--no-skills", *rpc_args],
                        cwd=work,
                        env={**extra, "PATH": os.environ["PATH"]},
                        stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                    )
                    try:
                        assert proc.stdin is not None and proc.stdout is not None
                        proc.stdin.write(b'{"type":"get_messages","id":"m"}\n')
                        proc.stdin.flush()
                        out = b""
                        deadline = time.monotonic() + 8.0
                        while time.monotonic() < deadline:
                            ready, _, _ = select.select([proc.stdout], [], [], max(0.0, deadline - time.monotonic()))
                            if not ready:
                                break
                            chunk = os.read(proc.stdout.fileno(), 65536)
                            if not chunk:
                                break
                            out += chunk
                            if b'"command":"get_messages"' in out:
                                break
                    finally:
                        proc.terminate()
                        try:
                            more, err = proc.communicate(timeout=3)
                        except subprocess.TimeoutExpired:
                            proc.kill()
                            more, err = proc.communicate()
                    self.assertIn(f"PI_MARKER_{mode}".encode(), out + more, f"{mode}: err={err!r}")
                    self.assertNotIn(b"creating a new session", err, mode)

    def test_r4_p1_codex_noncanonical_with_old_uuid_and_pi_duplicates_session_jsonl_settings_dir(self):
        import select
        import shlex
        import time

        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp).resolve()
            home = root / "home"
            home.mkdir()

            # 1. Codex: non-canonical names carrying old argv UUID A but header B must keep B invalid
            sid_a = "11111111-1111-7111-8111-111111111111"
            sid_b = "22222222-2222-7222-8222-222222222222"
            for idx_n, noncanon_name in enumerate((f"rollout-copy-{sid_a}.jsonl", f"rollout-{sid_a}-backup.jsonl")):
                cwd_codex = root / f"codex-work-{idx_n}"
                cwd_codex.mkdir()
                body_b = (
                    json.dumps({"type": "session_meta", "payload": {"id": sid_b, "cwd": str(cwd_codex)}}) + "\n"
                    + json.dumps({"type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "CURRENT_B"}]}}) + "\n"
                )
                res = self.run_restore(
                    cwd_codex,
                    coord={"sid": sid_b, "tool": "x"},
                    title="codex",
                    saved_codex_ids={sid_a},
                    saved_codex_records={f"sessions/d/{noncanon_name}": body_b},
                    plan_json=True,
                    current_command="codex",
                    full_command=f"codex resume {sid_a}",
                    extra_env={"HOME": str(home)},
                )
                self.assertEqual(res.returncode, 0, res.stderr)
                plan = json.loads(res.stdout.strip().splitlines()[-1])
                self.assertEqual(plan["kind"], "invalid", noncanon_name)
                self.assertEqual(plan["sid"], sid_b, noncanon_name)

            # 2. Pi: duplicate SID in default native dir, session.jsonl, settings.json sessionDir,
            #    and planner-only PI_CODING_AGENT_SESSION_DIR all pin `--session <verified_path>`.
            pi_sid = "87878787-8787-7787-8787-878787878787"
            pi_bin = Path("/opt/homebrew/bin/pi")

            def write_pi(p: Path, cwd: Path, marker: str) -> None:
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(
                    json.dumps({"type": "session", "version": 3, "id": pi_sid, "cwd": str(cwd), "timestamp": "2026-11-23T09:01:27.117Z"}) + "\n"
                    + json.dumps({"type": "message", "id": "abcdef12", "parentId": None, "timestamp": "2026-11-23T09:02:27.117Z", "message": {"role": "user", "content": [{"type": "text", "text": marker}], "timestamp": 1795400000000}}) + "\n",
                    encoding="utf-8",
                )

            for layout in ("duplicate", "opaque_session", "settings_sessionDir", "planner_only_flat"):
                case_dir = root / f"case-{layout}"
                agent = case_dir / "pi"
                work = case_dir / "work"
                wrong = case_dir / "wrong"
                for d in (agent, work, wrong):
                    d.mkdir(parents=True)
                native_dir = agent / "sessions" / self._pi_encoded_dir(work)
                extra_env = {
                    "HOME": str(home),
                    "PI_CODING_AGENT_DIR": str(agent),
                    "PI_OFFLINE": "1",
                    "PI_SKIP_VERSION_CHECK": "1",
                    "XDG_CONFIG_HOME": str(home / ".config"),
                    "TERM": "dumb",
                    "AIPANE_PI_LAUNCH_CMD": "pi",
                }
                if layout == "duplicate":
                    write_pi(native_dir / f"2026-11-18T00-00-00-000Z_{pi_sid}.jsonl", wrong, "WRONG_CWD")
                    good = native_dir / f"2026-11-23T00-00-00-000Z_{pi_sid}.jsonl"
                elif layout == "opaque_session":
                    good = agent / "sessions/--migrated--/session.jsonl"
                elif layout == "settings_sessionDir":
                    conf = case_dir / "configured"
                    conf.mkdir()
                    (agent / "settings.json").write_text(json.dumps({"sessionDir": str(conf)}), encoding="utf-8")
                    good = native_dir / f"2026-11-23T00-00-00-000Z_{pi_sid}.jsonl"
                else:
                    flat = case_dir / "flat"
                    flat.mkdir()
                    extra_env["PI_CODING_AGENT_SESSION_DIR"] = str(flat)
                    good = flat / f"2026-11-23T09-01-27-117Z_{pi_sid}.jsonl"
                write_pi(good, work, "RIGHT_CWD")

                q = self.run_restore(
                    work,
                    coord={"tool": "p", "sid": pi_sid},
                    title="pi",
                    saved_codex_ids=set(),
                    full_command="pi",
                    current_command="pi",
                    plan_json=True,
                    extra_env=extra_env,
                )
                self.assertEqual(q.returncode, 0, q.stderr)
                plan = json.loads(q.stdout.strip().splitlines()[-1])
                self.assertEqual(plan["kind"], "resume", layout)
                self.assertTrue(plan["restorable"], layout)
                self.assertIn("--session ", plan["command"], layout)
                self.assertIn(str(good.resolve()), plan["command"], layout)

                if pi_bin.is_file():
                    rpc_args = shlex.split(plan["command"])[1:]
                    # Drop planner-only PI_CODING_AGENT_SESSION_DIR to verify target shell independence
                    target_env = {k: v for k, v in extra_env.items() if k != "PI_CODING_AGENT_SESSION_DIR"}
                    target_env["PATH"] = os.environ["PATH"]
                    proc = subprocess.Popen(
                        [str(pi_bin), "--mode", "rpc", "--offline", "--no-extensions", "--no-skills", *rpc_args],
                        cwd=work,
                        env=target_env,
                        stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                    )
                    try:
                        assert proc.stdin is not None and proc.stdout is not None
                        proc.stdin.write(b'{"type":"get_messages","id":"m"}\n')
                        proc.stdin.flush()
                        out = b""
                        deadline = time.monotonic() + 8.0
                        while time.monotonic() < deadline:
                            ready, _, _ = select.select([proc.stdout], [], [], max(0.0, deadline - time.monotonic()))
                            if not ready:
                                break
                            chunk = os.read(proc.stdout.fileno(), 65536)
                            if not chunk:
                                break
                            out += chunk
                            if b'"command":"get_messages"' in out:
                                break
                    finally:
                        proc.terminate()
                        try:
                            more, err = proc.communicate(timeout=3)
                        except subprocess.TimeoutExpired:
                            proc.kill()
                            more, err = proc.communicate()
                    self.assertIn(b"RIGHT_CWD", out + more, f"{layout}: err={err!r}")
                    self.assertNotIn(b"WRONG_CWD", out + more, layout)
                    self.assertNotIn(b"creating a new session", err, layout)

    def test_r5_p1_codex_multi_db_distinct_ts_bad_date_and_pi_settings_precedence(self):
        from contextlib import closing
        import select
        import shlex
        import sqlite3
        import time

        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp).resolve()
            home = root / "home"
            home.mkdir()
            sid_a = "11111111-1111-7111-8111-111111111111"
            sid_b = "22222222-2222-7222-8222-222222222222"

            # 1. Codex: lower DB valid opaque B + higher DB missing -> native `codex resume`
            #    only reads the highest DB and fails, so ai-restore keeps bound B as `invalid`
            #    instead of claiming restorable=True or falling back to stale argv A.
            cwd1 = root / "codex-db"
            cwd1.mkdir()
            body_b = (
                json.dumps({"type": "session_meta", "ordinal": 0, "payload": {"id": sid_b, "cwd": str(cwd1)}}) + "\n"
                + json.dumps({"type": "response_item", "ordinal": 1, "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "CURRENT_B"}]}}) + "\n"
            )
            codex_home = cwd1 / "codex"
            opaque_p = codex_home / "sessions/d/opaque.jsonl"
            opaque_p.parent.mkdir(parents=True, exist_ok=True)
            opaque_p.write_text(body_b, encoding="utf-8")
            with closing(sqlite3.connect(codex_home / "state_8.sqlite")) as conn, conn:
                conn.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, rollout_path TEXT, updated_at INTEGER)")
                conn.execute("INSERT INTO threads VALUES (?, ?, ?)", (sid_b, str(opaque_p), int(time.time())))
            with closing(sqlite3.connect(codex_home / "state_9.sqlite")) as conn, conn:
                conn.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, rollout_path TEXT, updated_at INTEGER)")
                conn.execute("INSERT INTO threads VALUES (?, ?, ?)", (sid_b, str(codex_home / "sessions/d/missing.jsonl"), int(time.time())))
            res_db = self.run_restore(
                cwd1,
                coord={"sid": sid_b, "tool": "x", "codex_home": str(codex_home)},
                title="codex",
                saved_codex_ids={sid_a},
                plan_json=True,
                current_command="codex",
                full_command=f"codex resume {sid_a}",
                extra_env={"HOME": str(home)},
            )
            self.assertEqual(res_db.returncode, 0, res_db.stderr)
            plan_db = json.loads(res_db.stdout.strip().splitlines()[-1])
            self.assertEqual(plan_db["kind"], "invalid")
            self.assertFalse(plan_db["restorable"])
            self.assertEqual(plan_db["sid"], sid_b)

            # 2. Codex: distinct-timestamp filename A with header B, AND impossible date 2026-13-42T99-99-99 -> invalid B
            for idx_n, fname in enumerate(
                (f"rollout-2026-11-23T00-10-09-{sid_a}.jsonl", f"rollout-2026-13-42T99-99-99-{sid_a}.jsonl")
            ):
                cwd_c = root / f"codex-ts-{idx_n}"
                cwd_c.mkdir()
                res_c = self.run_restore(
                    cwd_c,
                    coord={"sid": sid_b, "tool": "x"},
                    title="codex",
                    saved_codex_ids={sid_a},
                    saved_codex_records={f"sessions/d/{fname}": body_b},
                    plan_json=True,
                    current_command="codex",
                    full_command=f"codex resume {sid_a}",
                    extra_env={"HOME": str(home)},
                )
                self.assertEqual(res_c.returncode, 0, res_c.stderr)
                plan_c = json.loads(res_c.stdout.strip().splitlines()[-1])
                self.assertEqual(plan_c["kind"], "invalid", fname)
                self.assertEqual(plan_c["sid"], sid_b, fname)

            # 3. Pi: project relative sessionDir > global settings > default sessions, resolved against project cwd
            pi_sid = "87878787-8787-7787-8787-878787878787"
            pi_agent = root / "pi-agent"
            project = root / "project"
            caller = root / "caller"
            global_dir = root / "global-store"
            for d in (pi_agent, project, caller, global_dir, project / "relstore", caller / "relstore"):
                d.mkdir(parents=True, exist_ok=True)
            default_dir = pi_agent / "sessions" / self._pi_encoded_dir(project)
            default_dir.mkdir(parents=True, exist_ok=True)
            fname = f"2026-11-23T09-01-27-117Z_{pi_sid}.jsonl"

            def seed_pi(folder: Path, marker: str) -> Path:
                p = folder / fname
                p.write_text(
                    json.dumps({"type": "session", "version": 3, "id": pi_sid, "cwd": str(project), "timestamp": "2026-11-23T09:01:27.117Z"}) + "\n"
                    + json.dumps({"type": "message", "id": "aabbccdd", "parentId": None, "timestamp": "2026-11-23T09:02:27.117Z", "message": {"role": "user", "content": [{"type": "text", "text": marker}], "timestamp": 1795400000000}}) + "\n",
                    encoding="utf-8",
                )
                return p

            seed_pi(default_dir, "STALE_DEFAULT")
            global_file = seed_pi(global_dir, "GLOBAL_ACTIVE")
            project_file = seed_pi(project / "relstore", "PROJECT_ACTIVE")
            seed_pi(caller / "relstore", "CALLER_SHADOW")

            (pi_agent / "settings.json").write_text(json.dumps({"sessionDir": str(global_dir)}), encoding="utf-8")
            (project / ".pi").mkdir(parents=True, exist_ok=True)
            (project / ".pi/settings.json").write_text(json.dumps({"sessionDir": "relstore"}), encoding="utf-8")

            pi_env = {
                "HOME": str(home),
                "PI_CODING_AGENT_DIR": str(pi_agent),
                "PI_OFFLINE": "1",
                "PI_SKIP_VERSION_CHECK": "1",
                "XDG_CONFIG_HOME": str(home / ".config"),
                "TERM": "dumb",
                "AIPANE_PI_LAUNCH_CMD": "pi",
            }
            # Run from `caller` cwd to prove relative `relstore` resolves against `project`, not `caller`
            prev_cwd = os.getcwd()
            os.chdir(caller)
            try:
                p_proj = self.run_restore(
                    project,
                    coord={"tool": "p", "sid": pi_sid},
                    title="pi",
                    saved_codex_ids=set(),
                    full_command="pi",
                    current_command="pi",
                    plan_json=True,
                    extra_env=pi_env,
                )
            finally:
                os.chdir(prev_cwd)
            self.assertEqual(p_proj.returncode, 0, p_proj.stderr)
            plan_proj = json.loads(p_proj.stdout.strip().splitlines()[-1])
            self.assertIn(str(project_file.resolve()), plan_proj["command"])

            # Removing project settings falls back to global settings (beating STALE_DEFAULT)
            (project / ".pi/settings.json").unlink()
            work2 = root / "work2"
            work2.mkdir()
            def2 = pi_agent / "sessions" / self._pi_encoded_dir(work2)
            def2.mkdir(parents=True, exist_ok=True)
            g2 = root / "g2"
            g2.mkdir()
            (pi_agent / "settings.json").write_text(json.dumps({"sessionDir": str(g2)}), encoding="utf-8")
            (def2 / fname).write_text(
                json.dumps({"type": "session", "version": 3, "id": pi_sid, "cwd": str(work2), "timestamp": "2026-11-23T09:01:27.117Z"}) + "\n",
                encoding="utf-8",
            )
            g2_file = g2 / fname
            g2_file.write_text(
                json.dumps({"type": "session", "version": 3, "id": pi_sid, "cwd": str(work2), "timestamp": "2026-11-23T09:01:27.117Z"}) + "\n",
                encoding="utf-8",
            )
            res_g2 = self.run_restore(
                work2,
                coord={"tool": "p", "sid": pi_sid},
                title="pi",
                saved_codex_ids=set(),
                full_command="pi",
                current_command="pi",
                plan_json=True,
                extra_env=pi_env,
            )
            self.assertEqual(res_g2.returncode, 0, res_g2.stderr)
            plan_g2 = json.loads(res_g2.stdout.strip().splitlines()[-1])
            self.assertIn(str(g2_file.resolve()), plan_g2["command"])

            pi_bin = Path("/opt/homebrew/bin/pi")
            if pi_bin.is_file():
                rpc_args = shlex.split(plan_proj["command"])[1:]
                proc = subprocess.Popen(
                    [str(pi_bin), "--mode", "rpc", "--offline", "--no-extensions", "--no-skills", *rpc_args],
                    cwd=project,
                    env={**pi_env, "PATH": os.environ["PATH"]},
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                try:
                    assert proc.stdin is not None and proc.stdout is not None
                    proc.stdin.write(b'{"type":"get_messages","id":"m"}\n')
                    proc.stdin.flush()
                    out = b""
                    deadline = time.monotonic() + 8.0
                    while time.monotonic() < deadline:
                        ready, _, _ = select.select([proc.stdout], [], [], max(0.0, deadline - time.monotonic()))
                        if not ready:
                            break
                        chunk = os.read(proc.stdout.fileno(), 65536)
                        if not chunk:
                            break
                        out += chunk
                        if b'"command":"get_messages"' in out:
                            break
                finally:
                    proc.terminate()
                    try:
                        more, _ = proc.communicate(timeout=3)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        more, _ = proc.communicate()
                self.assertIn(b"PROJECT_ACTIVE", out + more)
                self.assertNotIn(b"GLOBAL_ACTIVE", out + more)
                self.assertNotIn(b"STALE_DEFAULT", out + more)

            # 4. Pi: nested newer backup/ inside active sessionDir must NOT shadow direct active file,
            #    and an explicit verified `--session <file>` in launch argv is preserved verbatim.
            proj_flat = root / "project-flat"
            (proj_flat / ".pi").mkdir(parents=True, exist_ok=True)
            (proj_flat / "relstore/backup").mkdir(parents=True, exist_ok=True)
            (proj_flat / ".pi/settings.json").write_text(json.dumps({"sessionDir": "relstore"}), encoding="utf-8")

            def seed_pi_for(folder: Path, target_cwd: Path, marker: str) -> Path:
                p = folder / fname
                p.write_text(
                    json.dumps({"type": "session", "version": 3, "id": pi_sid, "cwd": str(target_cwd), "timestamp": "2026-11-23T09:01:27.117Z"}) + "\n"
                    + json.dumps({"type": "message", "id": "aabbccdd", "parentId": None, "timestamp": "2026-11-23T09:02:27.117Z", "message": {"role": "user", "content": [{"type": "text", "text": marker}], "timestamp": 1795400000000}}) + "\n",
                    encoding="utf-8",
                )
                return p

            direct_flat = seed_pi_for(proj_flat / "relstore", proj_flat, "DIRECT_FLAT")
            backup_file = seed_pi_for(proj_flat / "relstore/backup", proj_flat, "NESTED_BACKUP")
            future_t = time.time() + 60.0
            os.utime(backup_file, (future_t, future_t))
            p_flat = self.run_restore(
                proj_flat,
                coord={"tool": "p", "sid": pi_sid},
                title="pi",
                saved_codex_ids=set(),
                full_command="pi",
                current_command="pi",
                plan_json=True,
                extra_env=pi_env,
            )
            self.assertEqual(p_flat.returncode, 0, p_flat.stderr)
            plan_flat = json.loads(p_flat.stdout.strip().splitlines()[-1])
            self.assertIn(str(direct_flat.resolve()), plan_flat["command"])
            self.assertNotIn("backup", plan_flat["command"])

            proj_exp = root / "project-exp"
            (proj_exp / ".pi").mkdir(parents=True, exist_ok=True)
            (proj_exp / "relstore").mkdir(parents=True, exist_ok=True)
            (proj_exp / ".pi/settings.json").write_text(json.dumps({"sessionDir": "relstore"}), encoding="utf-8")
            seed_pi_for(proj_exp / "relstore", proj_exp, "OTHER_PROJECT_FILE")
            external_dir = root / "external-pi"
            external_dir.mkdir(parents=True, exist_ok=True)
            external_file = seed_pi_for(external_dir, proj_exp, "EXPLICIT_FILE")
            p_exp = self.run_restore(
                proj_exp,
                coord={"tool": "p", "sid": pi_sid},
                title="pi",
                saved_codex_ids=set(),
                full_command=f"pi --session {external_file}",
                current_command="pi",
                plan_json=True,
                extra_env=pi_env,
            )
            self.assertEqual(p_exp.returncode, 0, p_exp.stderr)
            plan_exp = json.loads(p_exp.stdout.strip().splitlines()[-1])
            self.assertIn(str(external_file.resolve()), plan_exp["command"])


if __name__ == "__main__":
    unittest.main()
