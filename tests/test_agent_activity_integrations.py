import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import tomllib
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INTEGRATIONS = ROOT / "integrations"
WINDOW_WRAP = ROOT / "bin" / "tmux-window-wrap"
CLAUDE_ACTIVITY = ROOT / "bin" / "aipane-claude-activity"
CODEX_NOTIFY = ROOT / "bin" / "aipane-codex-notify"
TMUX_CONFIG = ROOT / "conf" / "tmux-window-wrap.conf"
BUSY_COMMAND = "$HOME/.local/bin/tmux-window-wrap activity busy"
IDLE_COMMAND = "$HOME/.local/bin/tmux-window-wrap activity idle"
CLAUDE_ACTIVITY_COMMAND = "$HOME/.local/bin/aipane-claude-activity"


def nested_commands(hook_groups):
    return [
        hook["command"]
        for group in hook_groups
        for hook in group.get("hooks", [])
    ]


class AgentActivityIntegrationTests(unittest.TestCase):
    def load_json_hooks(self, tool):
        path = INTEGRATIONS / tool / "hooks.json"
        return json.loads(path.read_text(encoding="utf-8"))["hooks"]

    def assert_nested_event(self, hooks, event, command):
        self.assertIn(command, nested_commands(hooks[event]))

    def test_claude_hook_fragment_implements_turn_level_contract(self):
        hooks = self.load_json_hooks("claude")

        for event in (
            "SessionStart",
            "UserPromptSubmit",
            "PreToolUse",
            "Stop",
            "StopFailure",
            "TeammateIdle",
            "SessionEnd",
        ):
            self.assert_nested_event(hooks, event, CLAUDE_ACTIVITY_COMMAND)
        self.assertNotIn("PermissionRequest", hooks)

    def test_claude_activity_adapter_tracks_teammate_lifecycle(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            home = Path(raw_tmp)
            local_bin = home / ".local" / "bin"
            local_bin.mkdir(parents=True)
            activity_log = home / "activity.log"
            (local_bin / "tmux-window-wrap").write_text(
                "#!/bin/sh\n"
                f"printf '%s|' \"$*\" >> {activity_log}\n"
                f"cat >> {activity_log}\n",
                encoding="utf-8",
            )
            (local_bin / "tmux-window-wrap").chmod(0o755)

            environment = os.environ.copy()
            environment.update(
                {
                    "HOME": str(home),
                    "AIPANE_PYTHON": sys.executable,
                    "TMUX_PANE": "%7",
                }
            )

            def report(payload):
                return subprocess.run(
                    [str(CLAUDE_ACTIVITY)],
                    input=json.dumps(payload),
                    check=False,
                    capture_output=True,
                    text=True,
                    env=environment,
                )

            main_start = {
                "hook_event_name": "SessionStart",
                "source": "startup",
            }
            teammate_start = {
                "hook_event_name": "SessionStart",
                "source": "startup",
                "agent_type": "general-purpose",
            }
            pre_tool = {
                "hook_event_name": "PreToolUse",
                "agent_id": "sh-logo-search-a",
                "agent_type": "general-purpose",
                "tool_name": "WebSearch",
            }
            teammate_idle = {
                "hook_event_name": "TeammateIdle",
                "teammate_name": "sh-logo-search-a",
                "team_name": "session-05ca097e",
            }

            for payload in (
                main_start,
                teammate_start,
                pre_tool,
                teammate_idle,
                {"hook_event_name": "SessionStart", "source": "compact"},
            ):
                result = report(payload)
                self.assertEqual(result.returncode, 0, result.stderr)

            self.assertEqual(
                activity_log.read_text(),
                "".join(
                    (
                        "activity idle|" + json.dumps(main_start),
                        "activity busy|" + json.dumps(teammate_start),
                        "activity busy|" + json.dumps(pre_tool),
                        "activity idle|" + json.dumps(teammate_idle),
                    )
                ),
            )

    def test_standard_hook_fragments_implement_turn_level_contract(self):
        for tool in ("grok", "qoder"):
            with self.subTest(tool=tool):
                hooks = self.load_json_hooks(tool)
                self.assert_nested_event(hooks, "UserPromptSubmit", BUSY_COMMAND)
                for event in ("SessionStart", "Stop", "SessionEnd"):
                    self.assert_nested_event(hooks, event, IDLE_COMMAND)

        for tool in ("grok", "qoder"):
            with self.subTest(tool=tool, event="StopFailure"):
                hooks = self.load_json_hooks(tool)
                self.assert_nested_event(hooks, "StopFailure", IDLE_COMMAND)

    def test_codex_does_not_report_idle_from_the_blockable_stop_hook(self):
        hooks = self.load_json_hooks("codex")

        self.assert_nested_event(hooks, "UserPromptSubmit", BUSY_COMMAND)
        self.assert_nested_event(hooks, "PreToolUse", BUSY_COMMAND)
        self.assertNotIn(IDLE_COMMAND, nested_commands(hooks.get("Stop", [])))
        for event in ("SessionStart", "SessionEnd"):
            self.assert_nested_event(hooks, event, IDLE_COMMAND)
        self.assertEqual(
            hooks["SessionStart"][0]["matcher"],
            "^(startup|resume|clear)$",
        )
        self.assertNotIn("StopFailure", hooks)

    def test_codex_notify_reports_idle_after_true_turn_completion(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            home = Path(raw_tmp)
            local_bin = home / ".local" / "bin"
            sessions = home / ".codex" / "sessions" / "2026" / "08" / "12"
            local_bin.mkdir(parents=True)
            sessions.mkdir(parents=True)
            activity_log = home / "activity.log"
            bind_log = home / "bind.log"
            notify_log = home / "notify.log"
            thread_id = "019ff4e2-e2b8-72e2-bbd9-4e9069fd8c48"
            session_meta = {
                "type": "session_meta",
                "payload": {
                    "id": thread_id,
                    "source": "cli",
                },
            }
            (sessions / f"rollout-2026-08-12T15-32-17-{thread_id}.jsonl").write_text(
                json.dumps(session_meta) + "\n",
                encoding="utf-8",
            )

            (local_bin / "tmux-window-wrap").write_text(
                "#!/bin/sh\n"
                f"printf '%s|' \"$*\" >> {activity_log}\n"
                f"cat >> {activity_log}\n",
                encoding="utf-8",
            )
            (local_bin / "aipane-bind").write_text(
                f"#!/bin/sh\nprintf '%s\\n' \"$*\" >> {bind_log}\n",
                encoding="utf-8",
            )
            notifier = home / "notify"
            notifier.write_text(
                f"#!/bin/sh\nprintf '%s\\n' \"$1\" >> {notify_log}\n",
                encoding="utf-8",
            )
            for command in (*local_bin.iterdir(), notifier):
                command.chmod(0o755)

            payload = json.dumps(
                {
                    "type": "agent-turn-complete",
                    "thread-id": thread_id,
                    "turn-id": "turn-root",
                }
            )
            environment = os.environ.copy()
            environment.update(
                {
                    "HOME": str(home),
                    "AIPANE_PYTHON": sys.executable,
                    "AIPANE_CODEX_REAL_NOTIFY": str(notifier),
                    "TMUX_PANE": "%7",
                }
            )
            result = subprocess.run(
                [str(CODEX_NOTIFY), payload],
                check=False,
                capture_output=True,
                text=True,
                env=environment,
            )
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline and not bind_log.exists():
                time.sleep(0.01)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                activity_log.read_text(),
                f"activity idle|{payload}",
            )
            self.assertIn(f"--tool x --sid {thread_id}", bind_log.read_text())
            self.assertEqual(notify_log.read_text(), payload + "\n")

    def test_codex_subagent_completion_keeps_parent_busy_and_binds_parent(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            home = Path(raw_tmp)
            local_bin = home / ".local" / "bin"
            local_bin.mkdir(parents=True)
            sessions = home / ".codex" / "sessions" / "2026" / "08" / "12"
            sessions.mkdir(parents=True)
            activity_log = home / "activity.log"
            bind_log = home / "bind.log"
            notify_log = home / "notify.log"
            parent_id = "019ff626-3d48-7be3-9106-ed7cd1f2b1f8"
            child_id = "019ff628-1d69-7f01-a75a-028abb9ad39b"
            session_meta = {
                "type": "session_meta",
                "payload": {
                    "id": child_id,
                    "source": {
                        "subagent": {
                            "thread_spawn": {"parent_thread_id": parent_id}
                        }
                    },
                },
            }
            (sessions / f"rollout-2026-08-12T21-27-31-{child_id}.jsonl").write_text(
                json.dumps(session_meta) + "\n",
                encoding="utf-8",
            )

            (local_bin / "tmux-window-wrap").write_text(
                "#!/bin/sh\n"
                f"printf '%s|' \"$*\" >> {activity_log}\n"
                f"cat >> {activity_log}\n",
                encoding="utf-8",
            )
            (local_bin / "aipane-bind").write_text(
                f"#!/bin/sh\nprintf '%s\\n' \"$*\" >> {bind_log}\n",
                encoding="utf-8",
            )
            notifier = home / "notify"
            notifier.write_text(
                f"#!/bin/sh\nprintf '%s\\n' \"$1\" >> {notify_log}\n",
                encoding="utf-8",
            )
            for command in (*local_bin.iterdir(), notifier):
                command.chmod(0o755)

            payload = json.dumps(
                {"type": "agent-turn-complete", "thread-id": child_id}
            )
            environment = os.environ.copy()
            environment.update(
                {
                    "HOME": str(home),
                    "AIPANE_PYTHON": sys.executable,
                    "AIPANE_CODEX_REAL_NOTIFY": str(notifier),
                    "TMUX_PANE": "%7",
                }
            )
            result = subprocess.run(
                [str(CODEX_NOTIFY), payload],
                check=False,
                capture_output=True,
                text=True,
                env=environment,
            )
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline and not bind_log.exists():
                time.sleep(0.01)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(activity_log.exists())
            self.assertIn(f"--tool x --sid {parent_id}", bind_log.read_text())
            self.assertNotIn(child_id, bind_log.read_text())
            self.assertEqual(notify_log.read_text(), payload + "\n")

    def test_codex_active_goal_completion_keeps_parent_busy(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            home = Path(raw_tmp)
            local_bin = home / ".local" / "bin"
            codex_home = home / ".codex"
            local_bin.mkdir(parents=True)
            codex_home.mkdir()
            activity_log = home / "activity.log"
            bind_log = home / "bind.log"
            notify_log = home / "notify.log"
            thread_id = "019ff626-3d48-7be3-9106-ed7cd1f2b1f8"
            sessions = codex_home / "sessions" / "2026" / "08" / "12"
            sessions.mkdir(parents=True)
            (sessions / f"rollout-2026-08-12T21-27-31-{thread_id}.jsonl").write_text(
                json.dumps(
                    {
                        "type": "session_meta",
                        "payload": {"id": thread_id, "source": "cli"},
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            connection = sqlite3.connect(codex_home / "goals_1.sqlite")
            try:
                connection.execute(
                    "CREATE TABLE thread_goals (thread_id TEXT PRIMARY KEY, status TEXT)"
                )
                connection.execute(
                    "INSERT INTO thread_goals(thread_id, status) VALUES (?, 'active')",
                    (thread_id,),
                )
                connection.commit()
            finally:
                connection.close()

            (local_bin / "tmux-window-wrap").write_text(
                "#!/bin/sh\n"
                f"printf '%s|' \"$*\" >> {activity_log}\n"
                f"cat >> {activity_log}\n",
                encoding="utf-8",
            )
            (local_bin / "aipane-bind").write_text(
                f"#!/bin/sh\nprintf '%s\\n' \"$*\" >> {bind_log}\n",
                encoding="utf-8",
            )
            notifier = home / "notify"
            notifier.write_text(
                f"#!/bin/sh\nprintf '%s\\n' \"$1\" >> {notify_log}\n",
                encoding="utf-8",
            )
            for command in (*local_bin.iterdir(), notifier):
                command.chmod(0o755)

            payload = json.dumps(
                {
                    "type": "agent-turn-complete",
                    "thread-id": thread_id,
                    "turn-id": "turn-goal",
                }
            )
            environment = os.environ.copy()
            environment.update(
                {
                    "HOME": str(home),
                    "AIPANE_PYTHON": sys.executable,
                    "AIPANE_CODEX_REAL_NOTIFY": str(notifier),
                    "TMUX_PANE": "%7",
                }
            )
            result = subprocess.run(
                [str(CODEX_NOTIFY), payload],
                check=False,
                capture_output=True,
                text=True,
                env=environment,
            )
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline and not bind_log.exists():
                time.sleep(0.01)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                activity_log.read_text(),
                f"activity busy|{payload}",
            )
            self.assertIn(f"--tool x --sid {thread_id}", bind_log.read_text())
            self.assertEqual(notify_log.read_text(), payload + "\n")

    def test_codex_goal_store_failure_keeps_parent_busy(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            home = Path(raw_tmp)
            local_bin = home / ".local" / "bin"
            codex_home = home / ".codex"
            sessions = codex_home / "sessions" / "2026" / "08" / "12"
            local_bin.mkdir(parents=True)
            sessions.mkdir(parents=True)
            activity_log = home / "activity.log"
            bind_log = home / "bind.log"
            notify_log = home / "notify.log"
            thread_id = "019ff626-3d48-7be3-9106-ed7cd1f2b1f8"
            (sessions / f"rollout-2026-08-12T21-27-31-{thread_id}.jsonl").write_text(
                json.dumps(
                    {
                        "type": "session_meta",
                        "payload": {"id": thread_id, "source": "cli"},
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            (codex_home / "goals_1.sqlite").write_text(
                "not a sqlite database",
                encoding="utf-8",
            )
            (local_bin / "tmux-window-wrap").write_text(
                "#!/bin/sh\n"
                f"printf '%s|' \"$*\" >> {activity_log}\n"
                f"cat >> {activity_log}\n",
                encoding="utf-8",
            )
            (local_bin / "aipane-bind").write_text(
                f"#!/bin/sh\nprintf '%s\\n' \"$*\" >> {bind_log}\n",
                encoding="utf-8",
            )
            notifier = home / "notify"
            notifier.write_text(
                f"#!/bin/sh\nprintf '%s\\n' \"$1\" >> {notify_log}\n",
                encoding="utf-8",
            )
            for command in (*local_bin.iterdir(), notifier):
                command.chmod(0o755)
            payload = json.dumps(
                {
                    "type": "agent-turn-complete",
                    "thread-id": thread_id,
                    "turn-id": "turn-goal-unknown",
                }
            )
            environment = os.environ.copy()
            environment.update(
                {
                    "HOME": str(home),
                    "AIPANE_PYTHON": sys.executable,
                    "AIPANE_CODEX_REAL_NOTIFY": str(notifier),
                    "TMUX_PANE": "%7",
                }
            )

            result = subprocess.run(
                [str(CODEX_NOTIFY), payload],
                check=False,
                capture_output=True,
                text=True,
                env=environment,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                activity_log.read_text(),
                f"activity busy|{payload}",
            )

    def test_codex_unknown_completion_cannot_clear_or_rebind_pane(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            home = Path(raw_tmp)
            local_bin = home / ".local" / "bin"
            local_bin.mkdir(parents=True)
            activity_log = home / "activity.log"
            bind_log = home / "bind.log"
            notify_log = home / "notify.log"
            (local_bin / "tmux-window-wrap").write_text(
                "#!/bin/sh\n"
                f"printf '%s|' \"$*\" >> {activity_log}\n"
                f"cat >> {activity_log}\n",
                encoding="utf-8",
            )
            (local_bin / "aipane-bind").write_text(
                f"#!/bin/sh\nprintf '%s\\n' \"$*\" >> {bind_log}\n",
                encoding="utf-8",
            )
            notifier = home / "notify"
            notifier.write_text(
                f"#!/bin/sh\nprintf '%s\\n' \"$1\" >> {notify_log}\n",
                encoding="utf-8",
            )
            for command in (*local_bin.iterdir(), notifier):
                command.chmod(0o755)
            payload = json.dumps(
                {
                    "type": "agent-turn-complete",
                    "thread-id": "thread-without-rollout",
                    "turn-id": "turn-unknown",
                }
            )
            environment = os.environ.copy()
            environment.update(
                {
                    "HOME": str(home),
                    "AIPANE_PYTHON": sys.executable,
                    "AIPANE_CODEX_REAL_NOTIFY": str(notifier),
                    "TMUX_PANE": "%7",
                }
            )

            result = subprocess.run(
                [str(CODEX_NOTIFY), payload],
                check=False,
                capture_output=True,
                text=True,
                env=environment,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(activity_log.exists())
            self.assertFalse(bind_log.exists())
            self.assertEqual(notify_log.read_text(), payload + "\n")

    def test_codex_session_end_respects_three_second_timeout_limit(self):
        hooks = self.load_json_hooks("codex")

        session_end_hook = hooks["SessionEnd"][0]["hooks"][0]
        self.assertEqual(session_end_hook["timeout"], 3)

    def test_cursor_hook_fragment_implements_turn_level_contract(self):
        hooks = self.load_json_hooks("cursor")

        self.assertEqual(hooks["sessionStart"][0]["command"], IDLE_COMMAND)
        self.assertEqual(hooks["beforeSubmitPrompt"][0]["command"], BUSY_COMMAND)
        self.assertEqual(hooks["stop"][0]["command"], IDLE_COMMAND)
        self.assertEqual(hooks["sessionEnd"][0]["command"], IDLE_COMMAND)

    def test_kimi_turn_started_hook_covers_non_user_turn_origins(self):
        path = INTEGRATIONS / "kimi" / "hooks.toml"
        hooks = tomllib.loads(path.read_text(encoding="utf-8"))["hooks"]
        commands = {hook["event"]: hook["command"] for hook in hooks}

        self.assertEqual(commands["TurnStarted"], BUSY_COMMAND)
        self.assertNotIn("UserPromptSubmit", commands)
        for event in ("SessionStart", "Stop", "StopFailure", "Interrupt", "SessionEnd"):
            self.assertEqual(commands[event], IDLE_COMMAND)
        self.assertNotIn("PermissionRequest", commands)
        self.assertNotIn("PermissionResult", commands)

    def test_opencode_adapter_owns_binding_and_activity_translation(self):
        path = INTEGRATIONS / "opencode" / "aipane-bind.js"
        source = path.read_text(encoding="utf-8")

        self.assertIn('["activity", state, "--pane", pane]', source)
        self.assertIn('"chat.message"', source)
        self.assertIn('event.type === "session.status"', source)
        self.assertIn('event.type === "session.idle"', source)
        self.assertIn('event.type === "session.error"', source)

    def test_renderer_has_no_terminal_title_activity_input(self):
        script = WINDOW_WRAP.read_text(encoding="utf-8")
        config = TMUX_CONFIG.read_text(encoding="utf-8")

        self.assertNotIn("pane_title", script)
        self.assertNotIn("pane_title", config)
        self.assertNotIn("CODEX_WORKING_SPINNER", script)


if __name__ == "__main__":
    unittest.main()
