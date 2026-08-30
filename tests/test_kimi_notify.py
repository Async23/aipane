"""Contract tests for Kimi Code notification hook adaptation."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
NOTIFY = ROOT / "bin" / "aipane-kimi-notify"
COMPLETION_SOUNDS = {"Glass", "Ping", "Pop", "Purr", "Submarine", "Tink"}


class KimiNotifyTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.home = Path(self.temp_dir.name)

    def tearDown(self):
        self.temp_dir.cleanup()

    def run_notify(self, payload, environment_updates=None):
        environment = os.environ.copy()
        environment.update(
            {
                "HOME": str(self.home),
                "KIMI_CODE_HOME": str(self.home / ".kimi-code"),
                "KIMI_NOTIFY_DRY_RUN": "1",
                "TMUX_PANE": "",
            }
        )
        if environment_updates:
            environment.update(environment_updates)
        result = subprocess.run(
            [str(NOTIFY)],
            input=json.dumps(payload),
            text=True,
            capture_output=True,
            check=False,
            env=environment,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout) if result.stdout.strip() else None

    def write_session(
        self,
        *,
        title="Refactor notifications",
        prompt="Update the notifier",
        wire_prompt=None,
    ):
        session_dir = (
            self.home
            / ".kimi-code"
            / "sessions"
            / "wd_test"
            / "session-test"
        )
        wire_dir = session_dir / "agents" / "main"
        wire_dir.mkdir(parents=True)
        (session_dir / "state.json").write_text(
            json.dumps(
                {
                    "id": "session-test",
                    "title": title,
                    "lastPrompt": prompt,
                }
            ),
            encoding="utf-8",
        )
        records = [
            {"type": "metadata", "protocol_version": 2},
            {
                "type": "context.append_loop_event",
                "event": {
                    "type": "content.part",
                    "turnId": "0",
                    "part": {"type": "text", "text": "Previous answer."},
                },
            },
            {
                "type": "turn.prompt",
                "origin": {"kind": "user"},
                "input": [
                    {
                        "type": "text",
                        "text": wire_prompt or prompt or "Prompt from wire",
                    }
                ],
            },
            {
                "type": "context.append_message",
                "message": {
                    "role": "user",
                    "content": [{"type": "text", "text": "<system-reminder>"}],
                },
            },
            {
                "type": "context.append_loop_event",
                "event": {
                    "type": "content.part",
                    "turnId": "1",
                    "part": {"type": "think", "think": "Private reasoning."},
                },
            },
            {
                "type": "context.append_loop_event",
                "event": {
                    "type": "content.part",
                    "turnId": "1",
                    "part": {
                        "type": "text",
                        "text": "## Implemented the notification changes.\n\nMore detail.",
                    },
                },
            },
            {"type": "turn.ended", "turnId": "1", "reason": "completed"},
        ]
        (wire_dir / "wire.jsonl").write_text(
            "\n".join(json.dumps(record) for record in records) + "\n",
            encoding="utf-8",
        )

    def base_payload(self, event):
        return {
            "hook_event_name": event,
            "session_id": "session-test",
            "cwd": "/tmp/example-project",
            "session_title": "Refactor notifications",
        }

    def test_stop_matches_shared_content_contract(self):
        self.write_session()

        notification = self.run_notify(self.base_payload("Stop"))

        self.assertEqual(notification["title"], "Refactor notifications")
        self.assertEqual(notification["subtitle"], "Update the notifier")
        self.assertEqual(notification["body"], "Implemented the notification changes.")
        self.assertIn(notification["sound"], COMPLETION_SOUNDS)

    def test_tmux_title_matches_shared_coordinate_format(self):
        self.write_session()
        fake_tmux = self.home / "fake-tmux"
        fake_tmux.write_text(
            "#!/bin/sh\nprintf '$7\\tmain\\t@4\\t%%9\\t12\\t3\\n'\n",
            encoding="utf-8",
        )
        fake_tmux.chmod(0o755)

        notification = self.run_notify(
            self.base_payload("Stop"),
            {"TMUX_PANE": "%9", "AIPANE_TMUX": str(fake_tmux)},
        )

        self.assertEqual(notification["title"], "7:12:3 · Refactor notifications")

    def test_prompt_falls_back_to_wire_for_prompt_mode_sessions(self):
        self.write_session(prompt=None, wire_prompt="Prompt mode question")

        notification = self.run_notify(self.base_payload("Stop"))

        self.assertEqual(notification["subtitle"], "Prompt mode question")

    def test_failure_has_sound_without_exposing_tool_input(self):
        self.write_session()
        payload = self.base_payload("StopFailure")
        payload.update({"error_type": "ProviderError", "error_message": "request failed"})

        notification = self.run_notify(payload)

        self.assertEqual(notification["title"], "Refactor notifications · 任务失败")
        self.assertEqual(notification["subtitle"], "Update the notifier")
        self.assertEqual(notification["body"], "ProviderError: request failed")
        self.assertEqual(notification["sound"], "default")

    def test_permission_request_uses_action(self):
        self.write_session()
        payload = self.base_payload("PermissionRequest")
        payload.update(
            {
                "tool_name": "Shell",
                "tool_call_id": "tool-1",
                "action": "Run project tests",
                "tool_input": {"command": "secret-value"},
            }
        )

        notification = self.run_notify(payload)

        self.assertEqual(notification["title"], "Refactor notifications · 等待确认")
        self.assertEqual(notification["subtitle"], "Update the notifier")
        self.assertEqual(notification["body"], "Run project tests")
        self.assertNotIn("secret-value", json.dumps(notification))

    def test_question_uses_only_first_question(self):
        self.write_session()
        payload = self.base_payload("PreToolUse")
        payload.update(
            {
                "tool_name": "AskUserQuestion",
                "tool_call_id": "tool-2",
                "tool_input": {
                    "questions": [
                        {"question": "Which database should we use?"},
                        {"question": "This should not be included"},
                    ]
                },
            }
        )

        notification = self.run_notify(payload)

        self.assertEqual(notification["title"], "Refactor notifications · 等待输入")
        self.assertEqual(notification["subtitle"], "Update the notifier")
        self.assertEqual(notification["body"], "Which database should we use?")

    def test_stop_falls_back_without_session_files(self):
        notification = self.run_notify(self.base_payload("Stop"))

        self.assertEqual(notification["title"], "Refactor notifications")
        self.assertEqual(notification["subtitle"], "任务完成")
        self.assertEqual(notification["body"], "当前任务已执行完成。")

    def test_invalid_session_id_cannot_escape_kimi_home(self):
        payload = self.base_payload("Stop")
        payload["session_id"] = "../../outside"

        notification = self.run_notify(payload)

        self.assertEqual(notification["body"], "当前任务已执行完成。")

    def test_question_permission_duplicate_is_suppressed(self):
        payload = self.base_payload("PermissionRequest")
        payload.update({"tool_name": "AskUserQuestion", "tool_call_id": "tool-2"})

        self.assertIsNone(self.run_notify(payload))

    def test_unrelated_event_is_ignored(self):
        self.assertIsNone(self.run_notify(self.base_payload("PostToolUse")))


if __name__ == "__main__":
    unittest.main()
