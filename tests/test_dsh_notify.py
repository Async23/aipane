"""Contract tests for dsh lifecycle notification adaptation and delivery."""

from __future__ import annotations

import json
import os
import shlex
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
NOTIFY = ROOT / "bin" / "aipane-dsh-notify"
sys.path.insert(0, str(ROOT / "lib"))

from agent_notifications import (  # noqa: E402
    AgentNotifications,
    InMemoryNotificationAdapter,
    MacOSNotificationAdapter,
)


class DshNotifyTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.home = Path(self.temp_dir.name)
        self.environment = {
            **os.environ,
            "HOME": str(self.home),
            "TMUX_PANE": "",
            "TMUX": "",
        }
        self.environment.pop("DSH_NOTIFY_DRY_RUN", None)
        self.environment.pop("AIPANE_DSH_NOTIFIER_APP", None)
        self.environment.pop("DSH_HOME", None)
        self.adapter = InMemoryNotificationAdapter()
        self.notifications = AgentNotifications(
            adapter=self.adapter,
            environment=self.environment,
            choose_sound=lambda pool: "Glass",
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def payload(self, event="complete", **updates):
        return {
            "event": event,
            "session_id": "session-test",
            "turn_id": "1",
            "event_id": "seq:12",
            "cwd": "/tmp/example-project",
            "session_title": "Connect dsh notifications",
            "prompt": "Use icon 04",
            "answer": "## Added the dsh notification integration.\nMore detail.",
            **updates,
        }

    def invoke(self, payload, *, dry_run=True, flag=False):
        environment = self.environment.copy()
        if dry_run and not flag:
            environment["DSH_NOTIFY_DRY_RUN"] = "1"
        command = [sys.executable, str(NOTIFY)]
        if flag:
            command.append("--dry-run")
        result = subprocess.run(
            command,
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            env=environment,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout) if result.stdout.strip() else None

    def fake_sender(self):
        notifier = (
            self.home / "Applications" / "dsh Notifier.app"
            / "Contents" / "MacOS" / "dsh-notifier"
        )
        notifier.parent.mkdir(parents=True)
        notifier.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        notifier.chmod(0o755)
        return notifier

    def test_completion_content_matches_existing_notification_contract(self):
        result = self.notifications.handle("dsh", self.payload(), preview=True)

        self.assertEqual(result.notification.title, "Connect dsh notifications")
        self.assertEqual(result.notification.subtitle, "Use icon 04")
        self.assertEqual(result.notification.body, "Added the dsh notification integration.")
        self.assertEqual(result.notification.sound, "Glass")
        self.assertEqual(result.notification.event, "complete")

    def test_tmux_coordinate_and_focus_preserve_original_socket(self):
        fake_tmux = self.home / "fake-tmux"
        fake_tmux.write_text("#!/bin/sh\nprintf '$7\\tmain session\\t@4\\t%%9\\t12\\t3\\n'\n")
        fake_tmux.chmod(0o755)
        focus = self.home / ".local" / "bin" / "aipane-dsh-focus"
        focus.parent.mkdir(parents=True)
        focus.write_text("#!/bin/sh\nexit 0\n")
        focus.chmod(0o755)
        self.environment.update({
            "TMUX_PANE": "%9",
            "TMUX": "/tmp/dsh,custom socket,42,0",
            "AIPANE_TMUX": str(fake_tmux),
        })

        notification = self.invoke(self.payload())

        self.assertEqual(notification["title"], "7:12:3 · Connect dsh notifications")
        self.assertEqual(shlex.split(notification["action"]), [
            str(focus), "main session", "@4", "%9", "12", "/tmp/dsh,custom socket",
        ])
        self.assertEqual(notification["pane_id"], "%9")

    def test_failure_and_waits_use_safe_message_and_default_sound(self):
        for event, label in (
            ("failure", "任务失败"), ("approval", "等待确认"), ("question", "等待输入"),
        ):
            with self.subTest(event=event):
                notification = self.invoke(self.payload(
                    event,
                    message="Visible description",
                    tool_input={"command": "hidden-command"},
                    error={"api_key": "hidden-credential"},
                ))
                self.assertEqual(notification["title"], f"Connect dsh notifications · {label}")
                self.assertEqual(notification["subtitle"], "Use icon 04")
                self.assertEqual(notification["body"], "Visible description")
                self.assertEqual(notification["sound"], "default")
                self.assertNotIn("hidden-", json.dumps(notification))

    def test_content_is_bounded_plain_text_and_controls_are_removed(self):
        notification = self.invoke(self.payload(
            session_title="X" * 100,
            prompt="## Prompt\x1b\x00\nDetails",
            answer="**" + "A" * 300 + "**",
        ))

        self.assertLessEqual(len(notification["title"]), 48)
        self.assertEqual(notification["subtitle"], "Prompt")
        self.assertLessEqual(len(notification["body"]), 140)
        self.assertNotIn("**", notification["body"])

    def test_project_and_generic_fallbacks(self):
        notification = self.invoke(self.payload(session_title="", prompt="", answer=""))
        self.assertEqual(notification["title"], "example-project")
        self.assertEqual(notification["subtitle"], "任务完成")
        self.assertEqual(notification["body"], "当前任务已执行完成。")
        self.assertEqual(self.invoke(self.payload(session_title="", cwd=""))["title"], "dsh")

    def test_subagents_unknown_events_and_missing_identity_are_ignored(self):
        for payload in (
            self.payload(is_subagent=True),
            self.payload("tool_complete"),
            self.payload([]),
            self.payload(session_id=""),
            self.payload(turn_id=None, event_id=""),
            ["unexpected"],
        ):
            with self.subTest(payload=payload):
                self.assertIsNone(self.invoke(payload))
        self.assertEqual(list(self.home.iterdir()), [])

    def test_both_preview_modes_are_read_only(self):
        self.assertIsNotNone(self.invoke(self.payload()))
        self.assertIsNotNone(self.invoke(self.payload(), flag=True))
        self.assertEqual(list(self.home.iterdir()), [])
        self.assertEqual(self.adapter.deliveries, [])

    def test_dedup_survives_new_adapter_instance_without_payload_content(self):
        first = self.notifications.handle("dsh", self.payload())
        second = AgentNotifications(adapter=self.adapter, environment=self.environment).handle(
            "dsh", self.payload(answer="Different rendering of the same event"),
        )
        self.assertEqual(first.outcome, "delivered")
        self.assertEqual(second.outcome, "deduplicated")
        self.assertEqual(len(self.adapter.deliveries), 1)

    def test_distinct_turns_events_sessions_and_request_ids_are_not_suppressed(self):
        payloads = [
            self.payload(),
            self.payload(turn_id="2"),
            self.payload("approval"),
            self.payload(session_id="another-session"),
            self.payload(event_id="seq:13"),
            self.payload(turn_id=0),
        ]
        results = [self.notifications.handle("dsh", payload) for payload in payloads]
        self.assertEqual([result.outcome for result in results], ["delivered"] * len(payloads))
        self.assertEqual(len({result.notification.group for result in results}), len(payloads))

    def test_logs_and_dedup_markers_are_private_and_exclude_conversation(self):
        self.notifications.handle("dsh", self.payload(
            session_id="secret-session-id",
            prompt="secret-prompt",
            answer="secret-answer",
            credential="secret-credential",
        ))
        log = self.home / ".dsh" / "logs" / "dsh-notify.log"
        record = json.loads(log.read_text())
        self.assertEqual(set(record), {"time", "event", "group", "pane", "status"})
        self.assertEqual(record["status"], "memory")
        self.assertNotIn("secret-", log.read_text())
        self.assertEqual(stat.S_IMODE(log.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(log.parent.stat().st_mode), 0o700)
        marker, = (self.home / ".dsh" / "notifications" / "dedupe").iterdir()
        self.assertEqual(marker.read_text(), "")
        self.assertEqual(stat.S_IMODE(marker.stat().st_mode), 0o600)

    def test_missing_sender_records_failure_without_using_another_identity(self):
        adapter = MacOSNotificationAdapter(environment=self.environment, run_command=lambda *_: self.fail("fallback"))
        result = AgentNotifications(adapter=adapter, environment=self.environment).handle("dsh", self.payload())
        self.assertEqual(result.outcome, "failed")
        self.assertEqual(result.channel, "sender-missing")
        self.assertEqual(json.loads((self.home / ".dsh/logs/dsh-notify.log").read_text())["status"], "sender-missing")

    def test_dsh_home_isolates_state_and_log_from_default_home(self):
        dsh_home = self.home / "custom-dsh"
        notifications = AgentNotifications(
            adapter=self.adapter,
            environment={**self.environment, "DSH_HOME": str(dsh_home)},
        )
        preview = notifications.handle("dsh", self.payload(), preview=True)
        self.assertEqual(preview.outcome, "previewed")
        self.assertFalse(dsh_home.exists())

        first = notifications.handle("dsh", self.payload())
        duplicate = notifications.handle("dsh", self.payload())
        default_home = self.notifications.handle("dsh", self.payload())

        self.assertEqual(first.outcome, "delivered")
        self.assertEqual(duplicate.outcome, "deduplicated")
        self.assertEqual(default_home.outcome, "delivered")
        self.assertTrue((dsh_home / "notifications/dedupe" / first.notification.group).is_file())
        records = [json.loads(line) for line in (dsh_home / "logs/dsh-notify.log").read_text().splitlines()]
        self.assertEqual([record["status"] for record in records], ["memory", "deduplicated"])
        self.assertEqual(len((self.home / ".dsh/logs/dsh-notify.log").read_text().splitlines()), 1)

    def test_sender_uses_only_bundle_icon_and_fixed_delivery_timeout(self):
        notifier = self.fake_sender()
        calls = []
        adapter = MacOSNotificationAdapter(
            environment=self.environment,
            run_command=lambda command, timeout: calls.append((command, timeout)) or True,
        )
        result = AgentNotifications(adapter=adapter, environment=self.environment).handle("dsh", self.payload())
        self.assertEqual(result.channel, "sent")
        command, timeout = calls[0]
        self.assertEqual(command[0], str(notifier))
        self.assertEqual(timeout, 8)
        self.assertNotIn("-appIcon", command)
        self.assertNotIn("-sender", command)
        self.assertIn("-message", command)

    def test_sender_override_is_the_only_delivery_path(self):
        notifier = self.fake_sender()
        override = self.home / "alternate.app"
        notifier.parents[2].rename(override)
        calls = []
        adapter = MacOSNotificationAdapter(
            environment={**self.environment, "AIPANE_DSH_NOTIFIER_APP": str(override)},
            run_command=lambda command, timeout: calls.append(command) or True,
        )
        notification = self.notifications.handle("dsh", self.payload(), preview=True).notification
        self.assertEqual(adapter.deliver("dsh", notification), "sent")
        self.assertEqual(calls[0][0], str(override / "Contents/MacOS/dsh-notifier"))

    def test_timeout_and_exit_failure_remain_observable(self):
        self.fake_sender()
        adapter = MacOSNotificationAdapter(environment=self.environment)
        notification = self.notifications.handle("dsh", self.payload(), preview=True).notification
        with patch("agent_notifications.subprocess.run", side_effect=subprocess.TimeoutExpired("sender", 8)):
            self.assertEqual(adapter.deliver("dsh", notification), "sender-timeout")
        with patch("agent_notifications.subprocess.run", return_value=subprocess.CompletedProcess([], 7)):
            self.assertEqual(adapter.deliver("dsh", notification), "sender-exit-7")

    def test_cli_missing_sender_never_aborts_agent(self):
        self.assertIsNone(self.invoke(self.payload(), dry_run=False))
        log = self.home / ".dsh/logs/dsh-notify.log"
        self.assertEqual(json.loads(log.read_text())["status"], "sender-missing")


if __name__ == "__main__":
    unittest.main()
