"""Behavior tests for the Agent Notifications interface."""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LIB = ROOT / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

from agent_notifications import (  # noqa: E402
    AgentNotifications,
    InMemoryNotificationAdapter,
    MacOSNotificationAdapter,
)


# Codex TUI's temporary structured requests, with synthetic conversation text.
CODEX_TITLE_INSTRUCTIONS = (
    "Generate a concise, single-line task title of at most 36 characters and "
    "under five words where possible. Start with an imperative verb. "
    "Capitalize only the first word unless the user's language, proper nouns, "
    "acronyms, or code terms require otherwise. Preserve ticket references "
    "exactly. Write in the user's language. Do not use quotes, markdown, or "
    "trailing punctuation. Do not answer the request."
)
CODEX_RECAP_PROMPT = (
    "Write a brief catch-up for a user returning to this Codex task. "
    "In at most 40 words and one or two plain-text sentences, explain the "
    "objective, what was completed or learned, and the next step or blocker. "
    "Mention changed files, tests, approvals, or requested decisions only "
    "when relevant. Never claim changes were made or tests passed unless "
    "the conversation confirms it. If the task is complete, say so instead "
    "of inventing more work. Use the user's language; omit greetings, "
    "markdown, lists, and tool chatter.\n\nRecent conversation:\n"
    "User: Fix the bug.\nAssistant: Fixed and tested."
)


class AgentNotificationsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.home = Path(self.temp_dir.name)
        self.project = self.home / "cursor-project"
        self.project.mkdir()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_cursor_completion_is_normalized_and_delivered(self) -> None:
        transcript = self.home / "cursor-transcript.jsonl"
        transcript.write_text(
            "\n".join(
                (
                    json.dumps(
                        {
                            "role": "user",
                            "message": {
                                "content": "<user_query>Refactor notifications</user_query>"
                            },
                        }
                    ),
                    json.dumps(
                        {
                            "role": "assistant",
                            "message": {
                                "content": "## Implemented the shared module.\nMore detail."
                            },
                        }
                    ),
                )
            )
            + "\n",
            encoding="utf-8",
        )
        environment = os.environ.copy()
        environment.update(
            {
                "HOME": str(self.home),
                "CURSOR_CONFIG_DIR": str(self.home / ".cursor"),
                "CURSOR_NOTIFY_SOUND": "Ping",
                "TMUX_PANE": "",
            }
        )
        delivery = InMemoryNotificationAdapter()
        notifications = AgentNotifications(
            adapter=delivery,
            environment=environment,
        )

        result = notifications.handle(
            "cursor",
            {
                "status": "completed",
                "conversation_id": "conversation-1",
                "cwd": str(self.project),
                "transcript_path": str(transcript),
            },
        )

        self.assertEqual(result.outcome, "delivered")
        self.assertEqual(
            result.notification.as_dict(),
            {
                "title": "cursor-project",
                "subtitle": "Refactor notifications",
                "body": "Implemented the shared module.",
                "sound": "Ping",
                "event": "stop",
                "group": "cursor-agent-turn-51cf7676896a",
                "action": "",
                "category": "main",
            },
        )
        self.assertEqual(delivery.deliveries, [("cursor", result.notification)])

    def test_cursor_completion_chooses_one_shared_pool_sound(self) -> None:
        environment = os.environ.copy()
        environment.update(
            {
                "HOME": str(self.home),
                "CURSOR_CONFIG_DIR": str(self.home / ".cursor"),
                "TMUX_PANE": "",
            }
        )
        environment.pop("CURSOR_NOTIFY_SOUND", None)
        notifications = AgentNotifications(
            adapter=InMemoryNotificationAdapter(),
            environment=environment,
            choose_sound=lambda sounds: "Purr",
        )

        result = notifications.handle(
            "cursor",
            {"status": "completed", "cwd": str(self.project)},
        )

        self.assertEqual(result.notification.sound, "Purr")

    def test_preview_is_read_only(self) -> None:
        delivery = InMemoryNotificationAdapter()
        notifications = AgentNotifications(
            adapter=delivery,
            environment={
                "HOME": str(self.home),
                "CURSOR_NOTIFY_SOUND": "Ping",
                "TMUX_PANE": "",
            },
        )

        result = notifications.handle(
            "cursor",
            {"status": "aborted", "cwd": str(self.project)},
            preview=True,
        )

        self.assertEqual(result.outcome, "previewed")
        self.assertEqual(result.notification.body, "任务已中止。")
        self.assertEqual(delivery.deliveries, [])

    def test_cursor_delivery_uses_its_dedicated_notifier(self) -> None:
        notifier = (
            self.home
            / "Applications"
            / "Cursor Agent Notifier.app"
            / "Contents"
            / "MacOS"
            / "terminal-notifier"
        )
        notifier.parent.mkdir(parents=True)
        notifier.write_text(
            "#!/bin/sh\nprintf '%s\\n' \"$@\" > \"$DELIVERY_LOG\"\n",
            encoding="utf-8",
        )
        notifier.chmod(0o755)
        delivery_log = self.home / "delivery.log"
        environment = os.environ.copy()
        environment.update(
            {
                "HOME": str(self.home),
                "CURSOR_CONFIG_DIR": str(self.home / ".cursor"),
                "CURSOR_NOTIFY_SOUND": "Ping",
                "DELIVERY_LOG": str(delivery_log),
                "TMUX_PANE": "",
            }
        )
        notifications = AgentNotifications(
            adapter=MacOSNotificationAdapter(environment=environment),
            environment=environment,
        )

        result = notifications.handle(
            "cursor",
            {
                "status": "aborted",
                "conversation_id": "conversation-1",
                "cwd": str(self.project),
            },
        )

        self.assertEqual(result.outcome, "delivered")
        self.assertEqual(result.channel, "custom-app")
        self.assertEqual(
            delivery_log.read_text(encoding="utf-8").splitlines(),
            [
                "-title",
                "cursor-project",
                "-message",
                "任务已中止。",
                "-sound",
                "Ping",
                "-group",
                "cursor-agent-turn-51cf7676896a",
                "-ignoreDnD",
            ],
        )

    def test_cursor_notification_keeps_its_tmux_focus_action(self) -> None:
        cursor_home = self.home / ".cursor"
        focus = cursor_home / "hooks" / "cursor-focus-tmux.sh"
        focus.parent.mkdir(parents=True)
        focus.write_text("#!/bin/sh\n", encoding="utf-8")
        fake_tmux = self.home / "tmux"
        fake_tmux.write_text(
            "#!/bin/sh\nprintf 'main\\t@4\\t%%9\\t12\\n'\n",
            encoding="utf-8",
        )
        fake_tmux.chmod(0o755)
        environment = os.environ.copy()
        environment.update(
            {
                "HOME": str(self.home),
                "CURSOR_CONFIG_DIR": str(cursor_home),
                "CURSOR_NOTIFY_SOUND": "Ping",
                "TMUX_PANE": "%9",
                "AIPANE_TMUX": str(fake_tmux),
            }
        )
        notifications = AgentNotifications(
            adapter=InMemoryNotificationAdapter(),
            environment=environment,
        )

        result = notifications.handle("cursor", {"status": "completed"})

        self.assertEqual(
            result.notification.action,
            f"{focus} main @4 %9 12",
        )

    def test_claude_completion_is_normalized_and_delivered(self) -> None:
        environment = os.environ.copy()
        environment.update(
            {
                "HOME": str(self.home),
                "CLAUDE_NOTIFY_SOUND": "Tink",
                "TMUX_PANE": "",
            }
        )
        delivery = InMemoryNotificationAdapter()
        notifications = AgentNotifications(
            adapter=delivery,
            environment=environment,
        )

        result = notifications.handle(
            "claude",
            {
                "cwd": str(self.project),
                "last_assistant_message": "Implemented Claude notifications.\nMore detail.",
            },
        )

        self.assertEqual(result.outcome, "delivered")
        self.assertEqual(
            result.notification.as_dict(),
            {
                "title": "cursor-project",
                "subtitle": "",
                "body": "Implemented Claude notifications.",
                "sound": "Tink",
                "event": "stop",
                "group": "claude-code-task-complete",
                "action": "",
                "category": "main",
            },
        )

    def test_claude_compatibility_hooks_are_ignored(self) -> None:
        for marker in ("GROK_HOOK_EVENT", "CURSOR_PROJECT_DIR"):
            with self.subTest(marker=marker):
                environment = {
                    "HOME": str(self.home),
                    "CLAUDE_NOTIFY_SOUND": "Tink",
                    marker: "present",
                }
                delivery = InMemoryNotificationAdapter()
                result = AgentNotifications(
                    adapter=delivery,
                    environment=environment,
                ).handle("claude", {})

                self.assertEqual(result.outcome, "ignored")
                self.assertEqual(result.reason, "compatibility_hook")
                self.assertIsNone(result.notification)
                self.assertEqual(delivery.deliveries, [])

    def test_claude_delivery_uses_its_dedicated_notifier(self) -> None:
        notifier = (
            self.home
            / "Applications"
            / "Claude Code Notifier.app"
            / "Contents"
            / "MacOS"
            / "terminal-notifier"
        )
        notifier.parent.mkdir(parents=True)
        notifier.write_text(
            "#!/bin/sh\nprintf '%s\\n' \"$@\" > \"$DELIVERY_LOG\"\n",
            encoding="utf-8",
        )
        notifier.chmod(0o755)
        delivery_log = self.home / "claude-delivery.log"
        environment = os.environ.copy()
        environment.update(
            {
                "HOME": str(self.home),
                "CLAUDE_NOTIFY_SOUND": "Tink",
                "DELIVERY_LOG": str(delivery_log),
                "TMUX_PANE": "",
            }
        )
        result = AgentNotifications(
            adapter=MacOSNotificationAdapter(environment=environment),
            environment=environment,
        ).handle(
            "claude",
            {
                "cwd": str(self.project),
                "last_assistant_message": "Claude finished.",
            },
        )

        self.assertEqual(result.outcome, "delivered")
        self.assertEqual(result.channel, "custom-app")
        self.assertEqual(
            delivery_log.read_text(encoding="utf-8").splitlines(),
            [
                "-title",
                "cursor-project",
                "-message",
                "Claude finished.",
                "-group",
                "claude-code-task-complete",
                "-appIcon",
                f"file://{self.home}/.claude/hooks/claude-code-icon.png",
                "-sound",
                "Tink",
            ],
        )

    def test_grok_completion_is_normalized_and_delivered(self) -> None:
        environment = os.environ.copy()
        environment.update(
            {
                "HOME": str(self.home),
                "GROK_NOTIFY_SOUND": "Ping",
                "TMUX_PANE": "",
            }
        )
        delivery = InMemoryNotificationAdapter()
        notifications = AgentNotifications(
            adapter=delivery,
            environment=environment,
        )

        result = notifications.handle(
            "grok",
            {
                "event": "turn_complete",
                "session_id": "grok-session-1",
                "cwd": str(self.project),
                "prompt": "Refactor notifications",
                "last_agent_message": "Grok finished.\nMore detail.",
            },
        )

        self.assertEqual(result.outcome, "delivered")
        self.assertEqual(
            result.notification.as_dict(),
            {
                "title": "cursor-project",
                "subtitle": "Refactor notifications",
                "body": "Grok finished.",
                "sound": "Ping",
                "event": "turn_complete",
                "group": "grok-agent-turn-complete",
                "action": "",
                "category": "main",
            },
        )

    def test_grok_observation_only_stop_is_ignored(self) -> None:
        delivery = InMemoryNotificationAdapter()
        notifications = AgentNotifications(
            adapter=delivery,
            environment={
                "HOME": str(self.home),
                "GROK_NOTIFY_SOUND": "Ping",
                "TMUX_PANE": "",
            },
        )

        result = notifications.handle(
            "grok",
            {"event": "stop", "reason": "channel_closed"},
        )

        self.assertEqual(result.outcome, "ignored")
        self.assertEqual(result.reason, "observation_only_stop")
        self.assertIsNone(result.notification)
        self.assertEqual(delivery.deliveries, [])

    def test_grok_completion_can_be_deferred_before_transcript_settles(self) -> None:
        delivery = InMemoryNotificationAdapter()
        notifications = AgentNotifications(
            adapter=delivery,
            environment={
                "HOME": str(self.home),
                "GROK_NOTIFY_SOUND": "Ping",
                "TMUX_PANE": "",
            },
        )
        payload = {
            "event": "turn_complete",
            "session_id": "grok-session-1",
        }

        result = notifications.handle("grok", payload, phase="initial")

        self.assertEqual(result.outcome, "deferred")
        self.assertEqual(result.reason, "awaiting_transcript")
        self.assertIsNone(result.notification)
        self.assertEqual(delivery.deliveries, [])

    def test_grok_completion_reads_the_settled_transcript(self) -> None:
        transcript = self.home / "grok-updates.jsonl"
        transcript.write_text(
            "\n".join(
                (
                    json.dumps(
                        {
                            "params": {
                                "update": {
                                    "sessionUpdate": "user_message_chunk",
                                    "content": "<user_query>Use the transcript</user_query>",
                                }
                            }
                        }
                    ),
                    json.dumps(
                        {
                            "params": {
                                "update": {
                                    "sessionUpdate": "agent_message_chunk",
                                    "content": "Transcript answer.",
                                }
                            }
                        }
                    ),
                )
            )
            + "\n",
            encoding="utf-8",
        )
        notifications = AgentNotifications(
            adapter=InMemoryNotificationAdapter(),
            environment={
                "HOME": str(self.home),
                "GROK_NOTIFY_SOUND": "Ping",
                "TMUX_PANE": "",
            },
        )

        result = notifications.handle(
            "grok",
            {
                "event": "turn_complete",
                "cwd": str(self.project),
                "transcript_path": str(transcript),
            },
        )

        self.assertEqual(result.notification.subtitle, "Use the transcript")
        self.assertEqual(result.notification.body, "Transcript answer.")

    def test_grok_dual_channel_completion_is_deduplicated(self) -> None:
        delivery = InMemoryNotificationAdapter()
        notifications = AgentNotifications(
            adapter=delivery,
            environment={
                "HOME": str(self.home),
                "GROK_NOTIFY_SOUND": "Ping",
                "TMUX_PANE": "",
            },
        )
        payload = {
            "event": "turn_complete",
            "session_id": "grok-session-1",
            "cwd": str(self.project),
        }

        first = notifications.handle("grok", payload)
        second = notifications.handle("grok", payload)

        self.assertEqual(first.outcome, "delivered")
        self.assertEqual(second.outcome, "deduplicated")
        self.assertEqual(second.reason, "duplicate_completion")
        self.assertEqual(len(delivery.deliveries), 1)

    def test_grok_delivery_uses_its_dedicated_notifier(self) -> None:
        notifier = (
            self.home
            / "Applications"
            / "Grok Notifier.app"
            / "Contents"
            / "MacOS"
            / "terminal-notifier"
        )
        notifier.parent.mkdir(parents=True)
        notifier.write_text(
            "#!/bin/sh\nprintf '%s\\n' \"$@\" > \"$DELIVERY_LOG\"\n",
            encoding="utf-8",
        )
        notifier.chmod(0o755)
        delivery_log = self.home / "grok-delivery.log"
        environment = os.environ.copy()
        environment.update(
            {
                "HOME": str(self.home),
                "GROK_NOTIFY_SOUND": "Ping",
                "DELIVERY_LOG": str(delivery_log),
                "TMUX_PANE": "",
            }
        )

        result = AgentNotifications(
            adapter=MacOSNotificationAdapter(environment=environment),
            environment=environment,
        ).handle(
            "grok",
            {
                "event": "turn_complete",
                "session_id": "grok-delivery-session",
                "cwd": str(self.project),
                "prompt": "Refactor notifications",
                "last_agent_message": "Grok finished.",
            },
        )

        self.assertEqual(result.outcome, "delivered")
        self.assertEqual(result.channel, "custom-app")
        self.assertEqual(
            delivery_log.read_text(encoding="utf-8").splitlines(),
            [
                "-title",
                "cursor-project",
                "-subtitle",
                "Refactor notifications",
                "-message",
                "Grok finished.",
                "-sound",
                "Ping",
                "-group",
                "grok-agent-turn-complete",
            ],
        )

    def test_codex_main_completion_is_normalized_and_delivered(self) -> None:
        environment = os.environ.copy()
        environment.update(
            {
                "HOME": str(self.home),
                "CODEX_HOME": str(self.home / ".codex"),
                "CODEX_NOTIFY_SOUND": "Pop",
                "TMUX_PANE": "",
            }
        )
        delivery = InMemoryNotificationAdapter()
        notifications = AgentNotifications(
            adapter=delivery,
            environment=environment,
        )

        result = notifications.handle(
            "codex",
            {
                "type": "agent-turn-complete",
                "thread-id": "thread-main",
                "turn-id": "turn-1",
                "cwd": str(self.project),
                "input-messages": ["Refactor notifications"],
                "last_agent_message": "Codex finished.\nMore detail.",
            },
        )

        self.assertEqual(result.outcome, "delivered")
        self.assertEqual(
            result.notification.as_dict(),
            {
                "title": "cursor-project",
                "subtitle": "Refactor notifications",
                "body": "Codex finished.",
                "sound": "Pop",
                "event": "agent-turn-complete",
                "group": "codex-agent-turn-complete-ghostty-clean",
                "action": "",
                "category": "main",
            },
        )

    def test_codex_internal_completions_stay_silent_after_task_finishes(self) -> None:
        delivery = InMemoryNotificationAdapter()
        environment = {
            "HOME": str(self.home),
            "CODEX_HOME": str(self.home / ".codex"),
            "TMUX_PANE": "",
        }
        payload = {
            "type": "agent-turn-complete",
            "client": "codex-tui",
            "thread-id": "thread-main",
            "turn-id": "turn-main",
            "cwd": str(self.project),
            "input-messages": ["Fix the bug."],
            "last-assistant-message": "Fixed and tested.",
        }
        AgentNotifications(adapter=delivery, environment=environment).handle(
            "codex", payload
        )
        self.assertEqual(len(delivery.deliveries), 1)

        internal_requests = (
            (
                "internal_title",
                CODEX_TITLE_INSTRUCTIONS + "\n\nUser prompt:\nFix the bug.",
                '{"title":"Fix the bug"}',
            ),
            ("internal_recap", CODEX_RECAP_PROMPT, '{"recap":"Fixed and tested."}'),
            ("internal_recap", CODEX_RECAP_PROMPT, '{"recap":"The task is complete."}'),
            (
                "internal_title",
                CODEX_TITLE_INSTRUCTIONS
                + "\nPrioritize the current task and latest substantive user request."
                + "\n\nRecent conversation messages:\n"
                + '<conversation><message role="user">Fix the bug.</message></conversation>',
                '{"title":"Fix the bug"}',
            ),
            ("internal_recap", CODEX_RECAP_PROMPT, None),
        )
        for index, (reason, prompt, response) in enumerate(internal_requests):
            with self.subTest(reason=reason, index=index):
                # Each helper has a new thread/turn, so duplicate-key filtering
                # cannot fix these notifications after the visible task ends.
                result = AgentNotifications(
                    adapter=delivery, environment=environment
                ).handle(
                    "codex",
                    {
                        **payload,
                        "thread-id": f"temporary-{index}",
                        "turn-id": f"temporary-turn-{index}",
                        "input-messages": [prompt],
                        "last-assistant-message": response,
                    },
                )
                self.assertEqual(len(delivery.deliveries), 1)
                self.assertEqual(result.outcome, "suppressed")
                self.assertEqual(result.reason, reason)
                self.assertEqual(result.notification.category, "internal")

        log = self.home / ".codex" / "log" / "codex-notify.log"
        internal_records = [
            json.loads(line) for line in log.read_text().splitlines()
        ][1:]
        self.assertEqual(len(internal_records), len(internal_requests))
        for record in internal_records:
            self.assertTrue(record["suppressed"])
            self.assertNotIn("input-messages", record["payload"])
            self.assertNotIn("last-assistant-message", record["payload"])
            self.assertIn("thread-id", record["payload"])

    def test_codex_internal_preview_is_read_only(self) -> None:
        delivery = InMemoryNotificationAdapter()
        result = AgentNotifications(
            adapter=delivery,
            environment={"HOME": str(self.home), "TMUX_PANE": ""},
        ).handle(
            "codex",
            {
                "type": "agent-turn-complete",
                "client": "codex-tui",
                "input-messages": [CODEX_RECAP_PROMPT],
            },
            preview=True,
        )
        self.assertEqual(result.outcome, "suppressed")
        self.assertEqual(result.reason, "internal_recap")
        self.assertEqual(delivery.deliveries, [])
        self.assertFalse((self.home / ".codex").exists())

    def test_codex_user_title_and_recap_requests_still_notify(self) -> None:
        title_prompt = CODEX_TITLE_INSTRUCTIONS + "\n\nUser prompt:\nFix the bug."
        cases = (
            (["Generate a task title for this bug fix."], "codex-tui"),
            (["Write a brief catch-up for this task."], "codex-tui"),
            (["Explain this prompt:\n" + CODEX_RECAP_PROMPT], "codex-tui"),
            ([CODEX_TITLE_INSTRUCTIONS], "codex-tui"),
            ([title_prompt, "Now implement the fix."], "codex-tui"),
            ([title_prompt], "codex-exec"),
            ([CODEX_RECAP_PROMPT], None),
        )
        for messages, client in cases:
            with self.subTest(messages=messages, client=client):
                delivery = InMemoryNotificationAdapter()
                result = AgentNotifications(
                    adapter=delivery,
                    environment={"HOME": str(self.home), "TMUX_PANE": ""},
                ).handle(
                    "codex",
                    {
                        "type": "agent-turn-complete",
                        "client": client,
                        "cwd": str(self.project),
                        "input-messages": messages,
                        "last-assistant-message": '{"recap":"Fixed and tested."}',
                    },
                )
                self.assertEqual(result.outcome, "delivered")
                self.assertEqual(len(delivery.deliveries), 1)

    def test_codex_subagent_completion_keeps_thread_identity(self) -> None:
        codex_home = self.home / ".codex"
        codex_home.mkdir()
        with closing(sqlite3.connect(codex_home / "state_5.sqlite")) as connection, connection:
            connection.executescript(
                """
                CREATE TABLE threads (
                    id TEXT PRIMARY KEY,
                    title TEXT,
                    thread_source TEXT,
                    source TEXT,
                    agent_path TEXT,
                    agent_nickname TEXT,
                    agent_role TEXT,
                    model TEXT,
                    reasoning_effort TEXT,
                    rollout_path TEXT
                );
                CREATE TABLE thread_spawn_edges (
                    parent_thread_id TEXT,
                    child_thread_id TEXT
                );
                """
            )
            connection.execute(
                """
                INSERT INTO threads VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "thread-child",
                    "Review architecture",
                    "subagent",
                    "{}",
                    "/root/reviewer",
                    "Peirce",
                    "reviewer",
                    "gpt-5.6",
                    "high",
                    "",
                ),
            )
            connection.execute(
                "INSERT INTO threads VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "thread-parent",
                    "Parent task",
                    "cli",
                    "{}",
                    "",
                    "",
                    "",
                    "gpt-5.6",
                    "high",
                    "",
                ),
            )
            connection.execute(
                "INSERT INTO thread_spawn_edges VALUES (?, ?)",
                ("thread-parent", "thread-child"),
            )
        environment = {
            "HOME": str(self.home),
            "CODEX_HOME": str(codex_home),
            "CODEX_NOTIFY_SOUND": "Pop",
            "TMUX_PANE": "",
        }
        result = AgentNotifications(
            adapter=InMemoryNotificationAdapter(),
            environment=environment,
        ).handle(
            "codex",
            {
                "type": "agent-turn-complete",
                "thread-id": "thread-child",
                "cwd": str(self.project),
                "last_agent_message": "## 完成内容\n- Updated notification architecture.",
            },
        )

        self.assertEqual(result.notification.category, "subagent")
        self.assertEqual(
            result.notification.title,
            "L1 · Parent task",
        )
        self.assertEqual(
            result.notification.subtitle,
            "gpt-5.6/high · reviewer · Peirce",
        )
        self.assertEqual(
            result.notification.body,
            "Updated notification architecture.",
        )

    def test_codex_active_goal_completion_is_suppressed(self) -> None:
        codex_home = self.home / ".codex"
        sessions = codex_home / "sessions" / "2026" / "09" / "04"
        sessions.mkdir(parents=True)
        rollout = sessions / "rollout-thread-goal.jsonl"
        rollout.write_text(
            "\n".join(
                (
                    json.dumps(
                        {
                            "type": "event_msg",
                            "payload": {
                                "type": "task_started",
                                "turn_id": "turn-goal",
                            },
                        }
                    ),
                    json.dumps(
                        {
                            "type": "response_item",
                            "payload": {
                                "role": "user",
                                "internal_chat_message_metadata_passthrough": {
                                    "turn_id": "turn-goal"
                                },
                                "content": [
                                    {
                                        "type": "input_text",
                                        "text": '<codex_internal_context source="goal">continue</codex_internal_context>',
                                    }
                                ],
                            },
                        }
                    ),
                )
            )
            + "\n",
            encoding="utf-8",
        )
        with closing(sqlite3.connect(codex_home / "goals_1.sqlite")) as connection, connection:
            connection.execute(
                """
                CREATE TABLE thread_goals (
                    thread_id TEXT PRIMARY KEY,
                    goal_id TEXT,
                    objective TEXT,
                    status TEXT
                )
                """
            )
            connection.execute(
                "INSERT INTO thread_goals VALUES (?, ?, ?, ?)",
                (
                    "thread-goal",
                    "goal-1",
                    "Finish notification architecture",
                    "active",
                ),
            )
        delivery = InMemoryNotificationAdapter()
        result = AgentNotifications(
            adapter=delivery,
            environment={
                "HOME": str(self.home),
                "CODEX_HOME": str(codex_home),
                "CODEX_NOTIFY_SOUND": "Pop",
                "TMUX_PANE": "",
            },
        ).handle(
            "codex",
            {
                "type": "agent-turn-complete",
                "thread-id": "thread-goal",
                "turn-id": "turn-goal",
                "cwd": str(self.project),
                "last_agent_message": "Still working.",
            },
        )

        self.assertEqual(result.outcome, "suppressed")
        self.assertEqual(result.reason, "goal_active")
        self.assertEqual(result.notification.category, "goal")
        self.assertEqual(
            result.notification.title,
            "cursor-project · Goal 执行中",
        )
        self.assertEqual(
            result.notification.subtitle,
            "Finish notification architecture",
        )
        self.assertEqual(delivery.deliveries, [])

    def test_codex_delivery_uses_its_dedicated_app_identity(self) -> None:
        app = self.home / "Applications" / "Codex Notifier.app"
        app.mkdir(parents=True)
        commands: list[tuple[list[str], float]] = []

        def run_command(command: list[str], timeout: float) -> bool:
            commands.append((command, timeout))
            return True

        environment = {
            "HOME": str(self.home),
            "CODEX_HOME": str(self.home / ".codex"),
            "CODEX_NOTIFY_SOUND": "Pop",
            "TMUX_PANE": "",
        }
        result = AgentNotifications(
            adapter=MacOSNotificationAdapter(
                environment=environment,
                run_command=run_command,
            ),
            environment=environment,
        ).handle(
            "codex",
            {
                "type": "agent-turn-complete",
                "cwd": str(self.project),
                "last_agent_message": "Codex finished.",
            },
        )

        self.assertEqual(result.outcome, "delivered")
        self.assertEqual(result.channel, "custom-app")
        self.assertEqual(commands[0][0][:5], [
            "/usr/bin/open",
            "-gj",
            str(app),
            "--args",
            "-title",
        ])
        self.assertIn("codex-agent-turn-complete-ghostty-clean", commands[0][0])
        self.assertEqual(commands[0][1], 5)

    def test_kimi_completion_is_normalized_and_delivered(self) -> None:
        delivery = InMemoryNotificationAdapter()
        notifications = AgentNotifications(
            adapter=delivery,
            environment={
                "HOME": str(self.home),
                "KIMI_CODE_HOME": str(self.home / ".kimi-code"),
                "TMUX_PANE": "",
            },
            choose_sound=lambda sounds: "Glass",
        )

        result = notifications.handle(
            "kimi",
            {
                "hook_event_name": "Stop",
                "session_id": "session-test",
                "session_title": "Kimi task",
                "cwd": str(self.project),
            },
        )

        self.assertEqual(result.outcome, "delivered")
        self.assertEqual(
            result.notification.as_dict(),
            {
                "title": "Kimi task",
                "subtitle": "任务完成",
                "body": "当前任务已执行完成。",
                "sound": "Glass",
                "event": "Stop",
                "group": "kimi-f642e279dee4cc02bf17b25d",
                "action": "",
                "category": "main",
            },
        )

    def test_kimi_permission_request_exposes_action_not_tool_input(self) -> None:
        notifications = AgentNotifications(
            adapter=InMemoryNotificationAdapter(),
            environment={
                "HOME": str(self.home),
                "KIMI_CODE_HOME": str(self.home / ".kimi-code"),
                "TMUX_PANE": "",
            },
        )
        result = notifications.handle(
            "kimi",
            {
                "hook_event_name": "PermissionRequest",
                "session_id": "session-test",
                "session_title": "Kimi task",
                "cwd": str(self.project),
                "tool_name": "Shell",
                "tool_call_id": "tool-1",
                "action": "Run project tests",
                "tool_input": {"command": "secret-value"},
            },
        )

        self.assertEqual(result.outcome, "delivered")
        self.assertEqual(result.notification.title, "Kimi task · 等待确认")
        self.assertEqual(result.notification.subtitle, "cursor-project")
        self.assertEqual(result.notification.body, "Run project tests")
        self.assertEqual(result.notification.sound, "default")
        self.assertEqual(
            result.notification.group,
            "kimi-c6f663cd5ea357997e689fc6",
        )
        self.assertNotIn("secret-value", json.dumps(result.notification.as_dict()))

    def test_kimi_completion_reads_validated_session_content(self) -> None:
        session = (
            self.home
            / ".kimi-code"
            / "sessions"
            / "workspace"
            / "session-test"
        )
        wire = session / "agents" / "main" / "wire.jsonl"
        wire.parent.mkdir(parents=True)
        (session / "state.json").write_text(
            json.dumps(
                {
                    "title": "Session title",
                    "lastPrompt": "## Prompt from state",
                }
            ),
            encoding="utf-8",
        )
        wire.write_text(
            json.dumps(
                {
                    "type": "context.append_loop_event",
                    "event": {
                        "type": "content.part",
                        "part": {
                            "type": "text",
                            "text": "## Answer from wire.\nMore detail.",
                        },
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )
        result = AgentNotifications(
            adapter=InMemoryNotificationAdapter(),
            environment={
                "HOME": str(self.home),
                "KIMI_CODE_HOME": str(self.home / ".kimi-code"),
                "TMUX_PANE": "",
            },
            choose_sound=lambda sounds: "Glass",
        ).handle(
            "kimi",
            {
                "hook_event_name": "Stop",
                "session_id": "session-test",
                "cwd": str(self.project),
            },
        )

        self.assertEqual(result.notification.title, "Session title")
        self.assertEqual(result.notification.subtitle, "Prompt from state")
        self.assertEqual(result.notification.body, "Answer from wire.")

    def test_kimi_notification_keeps_coordinate_and_focus_action(self) -> None:
        fake_tmux = self.home / "kimi-tmux"
        fake_tmux.write_text(
            "#!/bin/sh\nprintf '$7\\tmain\\t@4\\t%%9\\t12\\t3\\n'\n",
            encoding="utf-8",
        )
        fake_tmux.chmod(0o755)
        focus = self.home / "aipane-kimi-focus"
        focus.write_text("#!/bin/sh\n", encoding="utf-8")
        environment = {
            "HOME": str(self.home),
            "KIMI_CODE_HOME": str(self.home / ".kimi-code"),
            "AIPANE_KIMI_FOCUS": str(focus),
            "AIPANE_TMUX": str(fake_tmux),
            "TMUX_PANE": "%9",
        }
        result = AgentNotifications(
            adapter=InMemoryNotificationAdapter(),
            environment=environment,
            choose_sound=lambda sounds: "Glass",
        ).handle(
            "kimi",
            {
                "hook_event_name": "Stop",
                "session_id": "session-test",
                "session_title": "Kimi task",
            },
        )

        self.assertEqual(result.notification.title, "7:12:3 · Kimi task")
        self.assertEqual(
            result.notification.action,
            f"{focus} main @4 %9 12",
        )

    def test_kimi_duplicate_hook_event_is_deduplicated(self) -> None:
        delivery = InMemoryNotificationAdapter()
        notifications = AgentNotifications(
            adapter=delivery,
            environment={
                "HOME": str(self.home),
                "KIMI_CODE_HOME": str(self.home / ".kimi-code"),
                "AIPANE_KIMI_NOTIFY_STATE_DIR": str(self.home / "notify-state"),
                "TMUX_PANE": "",
            },
            choose_sound=lambda sounds: "Glass",
        )
        payload = {
            "hook_event_name": "Stop",
            "session_id": "session-test",
            "session_title": "Kimi task",
        }

        first = notifications.handle("kimi", payload)
        second = notifications.handle("kimi", payload)

        self.assertEqual(first.outcome, "delivered")
        self.assertEqual(second.outcome, "deduplicated")
        self.assertEqual(second.reason, "duplicate_event")
        self.assertEqual(len(delivery.deliveries), 1)

    def test_kimi_delivery_uses_its_dedicated_notifier(self) -> None:
        app = self.home / "Applications" / "Kimi Code Notifier.app"
        notifier = app / "Contents" / "MacOS" / "kimi-notifier"
        notifier.parent.mkdir(parents=True)
        notifier.write_text(
            "#!/bin/sh\nprintf '%s\\n' \"$@\" > \"$DELIVERY_LOG\"\n",
            encoding="utf-8",
        )
        notifier.chmod(0o755)
        delivery_log = self.home / "kimi-delivery.log"
        environment = os.environ.copy()
        environment.update(
            {
                "HOME": str(self.home),
                "KIMI_CODE_HOME": str(self.home / ".kimi-code"),
                "AIPANE_KIMI_NOTIFIER_APP": str(app),
                "AIPANE_KIMI_NOTIFY_STATE_DIR": str(self.home / "notify-state"),
                "DELIVERY_LOG": str(delivery_log),
                "TMUX_PANE": "",
            }
        )

        result = AgentNotifications(
            adapter=MacOSNotificationAdapter(environment=environment),
            environment=environment,
            choose_sound=lambda sounds: "Glass",
        ).handle(
            "kimi",
            {
                "hook_event_name": "Stop",
                "session_id": "delivery-session",
                "session_title": "Kimi task",
            },
        )

        self.assertEqual(result.outcome, "delivered")
        self.assertEqual(result.channel, "sent")
        arguments = delivery_log.read_text(encoding="utf-8").splitlines()
        self.assertEqual(arguments[:6], [
            "-title",
            "Kimi task",
            "-subtitle",
            "任务完成",
            "-message",
            "当前任务已执行完成。",
        ])
        self.assertIn("Glass", arguments)

    def test_missing_kimi_notifier_is_an_explicit_delivery_failure(self) -> None:
        environment = {
            "HOME": str(self.home),
            "KIMI_CODE_HOME": str(self.home / ".kimi-code"),
            "AIPANE_KIMI_NOTIFY_STATE_DIR": str(self.home / "missing-state"),
            "TMUX_PANE": "",
        }
        result = AgentNotifications(
            adapter=MacOSNotificationAdapter(environment=environment),
            environment=environment,
            choose_sound=lambda sounds: "Glass",
        ).handle(
            "kimi",
            {
                "hook_event_name": "Stop",
                "session_id": "missing-notifier-session",
            },
        )

        self.assertEqual(result.outcome, "failed")
        self.assertEqual(result.channel, "sender-missing")


if __name__ == "__main__":
    unittest.main()
