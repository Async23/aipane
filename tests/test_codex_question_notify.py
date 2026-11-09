"""Codex question hooks, one-shot delivery, and side-effect-free previews."""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

from agent_notifications import AgentNotifications, InMemoryNotificationAdapter


class CodexQuestionNotificationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.environment = {
            "HOME": str(self.home),
            "CODEX_HOME": str(self.home / ".codex"),
            "PATH": os.environ["PATH"],
            "TMUX_PANE": "",
            "CODEX_NOTIFY_SOUND": "Ping",
        }
        self.delivery = InMemoryNotificationAdapter()
        self.notifications = AgentNotifications(adapter=self.delivery, environment=self.environment)

    def payload(self, **fields):
        return {
            "hook_event_name": "PreToolUse",
            "tool_name": "request_user_input_async",
            "session_id": "thread-one",
            "turn_id": "turn-one",
            "tool_use_id": "call-one",
            "cwd": "/projects/notification-task",
            "tool_input": {"questions": [{"title": "只重试 **失败工单** 可以吗？"}]},
            **fields,
        }

    def test_async_and_blocking_question_shapes_show_question_text(self):
        for tool, question in (
            ("request_user_input_async", {"title": "只重试 **失败工单** 可以吗？"}),
            ("request_user_input", {"header": "重试", "id": "retry",
                                    "question": "只重试 **失败工单** 可以吗？"}),
        ):
            with self.subTest(tool=tool):
                result = self.notifications.handle("codex", self.payload(
                    tool_name=tool, tool_input={"questions": [question]},
                ), preview=True)
                self.assertEqual(result.outcome, "previewed")
                self.assertEqual(result.notification.title, "notification-task · 等待回答")
                self.assertEqual(result.notification.body, "只重试 失败工单 可以吗？")
                self.assertEqual(result.notification.sound, "Ping")
                self.assertEqual(result.notification.category, "question")
        self.assertEqual(self.delivery.deliveries, [])
        self.assertFalse((self.home / ".codex").exists())

    def test_duplicate_question_across_instances_and_turns_is_silent(self):
        payload = self.payload()
        first = self.notifications.handle("codex", payload)
        again = AgentNotifications(adapter=self.delivery, environment=self.environment)
        self.assertEqual(again.handle("codex", payload).outcome, "deduplicated")
        self.assertEqual(again.handle("codex", self.payload(turn_id="turn-two")).outcome,
                         "deduplicated")
        second = again.handle("codex", self.payload(tool_use_id="call-two"))
        other_session = again.handle("codex", self.payload(session_id="thread-two"))
        self.assertEqual(len(self.delivery.deliveries), 3)
        self.assertEqual(len({n.notification.group for n in (first, second, other_session)}), 3)
        log = self.home / ".codex/log/codex-questions.log"
        records = [json.loads(line) for line in log.read_text().splitlines()]
        self.assertEqual(len(records), 3)
        self.assertTrue(all(r["outcome"] == "delivered" for r in records))
        self.assertNotIn("失败工单", log.read_text())
        self.assertEqual(log.stat().st_mode & 0o777, 0o600)

    def test_concurrent_duplicate_hook_delivers_once(self):
        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(
                lambda _: self.notifications.handle("codex", self.payload()), range(16)
            ))
        self.assertEqual([r.outcome for r in results].count("delivered"), 1)
        self.assertEqual(len(self.delivery.deliveries), 1)

    def test_preview_does_not_consume_delivery(self):
        self.notifications.handle("codex", self.payload(), preview=True)
        self.assertFalse((self.home / ".codex").exists())
        self.assertEqual(self.notifications.handle("codex", self.payload()).outcome, "delivered")

    def test_failed_delivery_can_retry_and_sender_exception_does_not_escape(self):
        for failure in ("failed", OSError("sender unavailable")):
            payload = self.payload(tool_use_id=str(failure))
            if isinstance(failure, Exception):
                mock = patch.object(self.delivery, "deliver", side_effect=failure)
            else:
                mock = patch.object(self.delivery, "deliver", return_value=failure)
            with mock:
                self.assertEqual(self.notifications.handle("codex", payload).outcome, "failed")
            self.assertEqual(self.notifications.handle("codex", payload).outcome, "delivered")

    def test_other_tools_subagents_and_invalid_questions_do_not_notify(self):
        for fields in (
            {"hook_event_name": "PostToolUse"}, {"hook_event_name": ""},
            {"tool_name": "exec_command"}, {"tool_name": []},
            {"agent_id": "child-thread"}, {"session_id": ""}, {"tool_use_id": ""},
            {"tool_input": {}}, {"tool_input": {"questions": []}},
            {"tool_input": {"questions": ["invalid"]}},
            {"tool_input": {"questions": [{"title": " "}]}},
            {"tool_input": {"questions": [{"title": 42}]}},
        ):
            with self.subTest(fields=fields):
                self.assertEqual(self.notifications.handle("codex", self.payload(**fields)).outcome,
                                 "ignored")
        self.assertFalse((self.home / ".codex").exists())
        self.assertEqual(self.delivery.deliveries, [])

    def test_multiple_questions_are_counted_and_body_is_bounded(self):
        result = self.notifications.handle("codex", self.payload(tool_input={"questions": [
            {"title": "问" * 200}, {"title": "第二个问题？"},
        ]}), preview=True)
        self.assertEqual(result.notification.subtitle, "2 个问题等待回答")
        self.assertLessEqual(len(result.notification.body), 140)

    def test_question_does_not_use_goal_completion_suppression(self):
        with patch("agent_notifications._codex_query_goal", side_effect=AssertionError):
            result = self.notifications.handle("codex", self.payload())
        self.assertEqual(result.outcome, "delivered")

    def test_title_and_click_action_keep_original_pane(self):
        focus = self.home / ".codex/bin/codex-focus-tmux.sh"
        focus.parent.mkdir(parents=True)
        focus.touch()
        self.notifications.environment["TMUX_PANE"] = "%9"
        with patch("agent_notifications._codex_tmux_thread_title", return_value="工单重试"), \
             patch("agent_notifications._tmux_coordinate", return_value="0:1:2"), \
             patch("agent_notifications._tmux_focus_target", return_value=("main", "@4", "%9", "1")):
            result = self.notifications.handle("codex", self.payload(), preview=True)
        self.assertEqual(result.notification.title, "0:1:2 · 工单重试 · 等待回答")
        self.assertEqual(shlex.split(result.notification.action), [str(focus.resolve()), "main", "@4", "%9", "1"])

    def test_registered_hook_reaches_real_wrapper_through_stdin(self):
        hooks = json.loads((ROOT / "integrations/codex/hooks.json").read_text())["hooks"]
        for tool in ("request_user_input", "request_user_input_async"):
            matching = [handler for group in hooks["PreToolUse"]
                        if re.search(group.get("matcher", ".*"), tool)
                        for handler in group["hooks"] if "aipane-codex-notify" in handler["command"]]
            self.assertEqual(len(matching), 1)
            self.assertTrue(matching[0]["async"])
        local_bin = self.home / ".local/bin"
        local_bin.mkdir(parents=True)
        (local_bin / "aipane-codex-notify").symlink_to(ROOT / "bin/aipane-codex-notify")
        result = subprocess.run(["/bin/sh", "-c", matching[0]["command"]],
                                input=json.dumps(self.payload()), text=True, capture_output=True,
                                env={**self.environment, "CODEX_NOTIFY_DRY_RUN": "1"}, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["category"], "question")
        self.assertFalse((self.home / ".codex").exists())

    def test_invalid_json_never_sends_completion(self):
        for raw in ("not JSON", "[]", "null"):
            result = subprocess.run([str(ROOT / "bin/aipane-codex-desktop-notify")], input=raw,
                                    text=True, capture_output=True,
                                    env={**self.environment, "CODEX_NOTIFY_DRY_RUN": "1"}, timeout=10)
            self.assertEqual((result.returncode, result.stdout), (0, ""))
        self.assertFalse((self.home / ".codex").exists())

    def test_manual_tracer_is_unique_and_uses_question_delivery(self):
        groups = set()
        for _ in range(2):
            result = subprocess.run([str(ROOT / "bin/aipane-codex-notify"), "--test-question"],
                                    text=True, capture_output=True,
                                    env={**self.environment, "CODEX_NOTIFY_DRY_RUN": "1"}, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            preview = json.loads(result.stdout)
            self.assertEqual(preview["category"], "question")
            self.assertIn("aipane-question-", preview["body"])
            groups.add(preview["group"])
        self.assertEqual(len(groups), 2)
        self.assertFalse((self.home / ".codex").exists())


if __name__ == "__main__":
    unittest.main()
