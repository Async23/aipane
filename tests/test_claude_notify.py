"""Claude hook routing, safe previews, and notification delivery contracts."""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

from agent_notifications import (  # noqa: E402
    AgentNotifications,
    InMemoryNotificationAdapter,
)


class ClaudeNotificationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.environment = {
            "HOME": str(self.home),
            "PATH": os.environ["PATH"],
            "TMUX_PANE": "",
            "CLAUDE_NOTIFY_SOUND": "Tink",
        }
        self.delivery = InMemoryNotificationAdapter()
        self.notifications = AgentNotifications(
            adapter=self.delivery, environment=self.environment
        )

    def payload(self, event, **fields):
        return {
            "hook_event_name": event,
            "session_id": "session-one",
            "prompt_id": "prompt-one",
            **fields,
        }

    def transcript(self, *records):
        path = self.home / "session-one.jsonl"
        path.write_text("".join(json.dumps({
            "sessionId": "session-one", **record,
        }, ensure_ascii=False) + "\n" for record in records))
        return str(path)

    def test_completion_reads_current_prompt_and_title_without_legacy_cache(self):
        transcript = self.transcript(
            {"type": "ai-title", "aiTitle": "北海道旅行"},
            {"type": "user", "promptId": "prompt-old",
             "message": {"content": "旧问题"}},
            {"type": "user", "promptId": "prompt-one",
             "message": {"content": [
                 {"type": "text", "text": "**这是什么会话？**"},
             ]}},
            {"type": "user", "message": {"content": [
                {"type": "tool_result", "content": "这是工具输出，不是问题"},
            ]}},
            {"type": "user", "isSidechain": True,
             "message": {"content": "子代理的问题"}},
            {"type": "user", "isMeta": True,
             "message": {"content": "系统注入的指令"}},
            {"type": "user", "isCompactSummary": True,
             "message": {"content": "压缩后的上下文"}},
            {"type": "user", "sessionId": "another-session",
             "message": {"content": "其他会话的问题"}},
            {"type": "user", "promptId": "next-prompt",
             "message": {"content": "下一轮问题"}},
        )
        result = self.notifications.handle("claude", self.payload(
            "Stop", transcript_path=transcript,
            last_assistant_message="## 这是 **北海道旅行** 会话。\n更多内容。",
        ), preview=True)
        self.assertEqual(result.notification.title, "北海道旅行")
        self.assertEqual(result.notification.subtitle, "这是什么会话？")
        self.assertEqual(result.notification.body, "这是 北海道旅行 会话。")
        self.assertEqual(self.delivery.deliveries, [])
        self.assertFalse((self.home / ".claude").exists())

    def test_custom_title_takes_precedence_over_generated_titles(self):
        transcript = self.transcript(
            {"type": "custom-title", "customTitle": "我的旅行计划"},
            {"type": "ai-title", "aiTitle": "自动生成的标题"},
            {"type": "user", "message": {"content": "安排路线"}},
        )
        result = self.notifications.handle("claude", self.payload(
            "Stop", transcript_path=transcript,
        ), preview=True)
        self.assertEqual(result.notification.title, "我的旅行计划")
        self.assertEqual(result.notification.subtitle, "安排路线")

    def test_transcript_prompt_replaces_stale_record_prompt_cache(self):
        state = self.home / ".claude/hooks/state/session-one.json"
        state.parent.mkdir(parents=True)
        state.write_text(json.dumps({"prompt": "过期的问题"}))
        transcript = self.transcript(
            {"type": "user", "message": {"content": "最新的问题"}},
        )
        result = self.notifications.handle("claude", self.payload(
            "Stop", transcript_path=transcript,
        ), preview=True)
        self.assertEqual(result.notification.subtitle, "最新的问题")

    def test_noncompletion_events_keep_status_visible_with_prompt_subtitles(self):
        transcript = self.transcript(
            {"type": "ai-title", "aiTitle": "发布应用"},
            {"type": "user", "message": {"content": "部署到测试环境"}},
        )
        cases = [
            ("StopFailure", {"error_details": "连接失败"}, "任务失败", "连接失败"),
            ("PermissionRequest", {"tool_name": "Bash"}, "需要批准",
             "Claude Code 请求执行 Bash"),
            ("PreToolUse", {"tool_name": "AskUserQuestion", "tool_input": {
                "questions": [{"question": "选择 **哪个环境**？"}],
            }}, "等待回答", "选择 哪个环境？"),
            ("Notification", {"notification_type": "agent_needs_input",
                              "message": "请填写版本号"}, "等待输入", "请填写版本号"),
        ]
        for event, fields, label, body in cases:
            with self.subTest(event=event):
                result = self.notifications.handle("claude", self.payload(
                    event, transcript_path=transcript, **fields,
                ), preview=True)
                self.assertEqual(result.notification.title, f"发布应用 · {label}")
                self.assertEqual(result.notification.subtitle, "部署到测试环境")
                self.assertEqual(result.notification.body, body)

    def test_image_only_prompt_does_not_reuse_previous_text_prompt(self):
        transcript = self.transcript(
            {"type": "user", "message": {"content": "之前的问题"}},
            {"type": "user", "message": {"content": [{"type": "image"}]}},
        )
        result = self.notifications.handle("claude", self.payload(
            "Stop", transcript_path=transcript,
        ), preview=True)
        self.assertEqual(result.notification.subtitle, "图片请求")

    def test_missing_or_partial_transcript_still_has_useful_content(self):
        for transcript in (
            str(self.home / "missing.jsonl"),
            self.transcript({"type": "assistant", "message": {"content": []}}),
        ):
            with self.subTest(transcript=transcript):
                result = self.notifications.handle("claude", self.payload(
                    "Stop", cwd=str(self.home / "demo"), transcript_path=transcript,
                    last_assistant_message="已更新 [配置](https://example.com) 和 `脚本`。",
                ), preview=True)
                self.assertEqual(result.notification.title, "demo")
                self.assertEqual(result.notification.subtitle, "任务完成")
                self.assertEqual(result.notification.body, "已更新 配置 和 脚本。")

    def test_completion_failure_approval_and_questions_have_distinct_content(self):
        cases = [
            (self.payload("Stop", last_assistant_message="已修复。\n测试通过。"),
             "任务完成", "已修复。", "claude-code-task-complete-"),
            (self.payload("StopFailure", error="rate_limit", error_details="额度不足"),
             "任务失败", "额度不足", "claude-code-task-failed-"),
            (self.payload("StopFailure", error="server_error"),
             "任务失败", "server_error", "claude-code-task-failed-"),
            (self.payload("PermissionRequest", tool_name="Bash"),
             "需要批准", "Claude Code 请求执行 Bash", "claude-code-waiting-input-"),
            (self.payload("PreToolUse", tool_name="AskUserQuestion",
                          tool_input={"questions": [{"question": "选哪个环境？"}]}),
             "等待回答", "选哪个环境？", "claude-code-waiting-input-"),
        ]
        for payload, subtitle, body, prefix in cases:
            with self.subTest(event=payload["hook_event_name"], body=body):
                result = self.notifications.handle("claude", payload)
                self.assertEqual(result.outcome, "delivered")
                self.assertEqual(result.notification.subtitle, subtitle)
                self.assertEqual(result.notification.body, body)
                self.assertTrue(result.notification.group.startswith(prefix))
                self.assertEqual(result.notification.sound, "Tink")

    def test_input_notifications_use_types_instead_of_english_keywords(self):
        for kind in ("elicitation_dialog", "elicitation_url_dialog", "agent_needs_input"):
            with self.subTest(kind=kind):
                result = self.notifications.handle("claude", self.payload(
                    "Notification", notification_type=kind, message="请填写项目名称"
                ))
                self.assertEqual(result.notification.body, "请填写项目名称")
                self.assertEqual(result.notification.subtitle, "等待输入")

    def test_repeated_native_alerts_and_unrelated_events_do_not_deliver(self):
        ignored = [
            self.payload("Notification", notification_type=kind, message="waiting for input")
            for kind in ("idle_prompt", "permission_prompt", "auth_success", "agent_completed")
        ] + [
            self.payload("PreToolUse", tool_name="Bash"),
            self.payload("PermissionRequest", tool_name="AskUserQuestion"),
            self.payload("SubagentStop"),
            self.payload("SessionEnd"),
            self.payload("Stop", agent_id="subagent-one"),
        ]
        for payload in ignored:
            with self.subTest(payload=payload):
                result = self.notifications.handle("claude", payload)
                self.assertEqual(result.outcome, "ignored")
        self.assertEqual(self.delivery.deliveries, [])
        self.assertFalse((self.home / ".claude").exists())

    def test_desktop_and_imported_hooks_do_not_deliver(self):
        for marker, value in (
            ("CLAUDE_CODE_ENTRYPOINT", "claude-desktop"),
            ("CLAUDE_CODE_ENTRYPOINT", "claude-desktop-3p"),
            ("GROK_HOOK_EVENT", "Stop"),
            ("CURSOR_VERSION", "1.0"),
        ):
            with self.subTest(marker=marker, value=value):
                result = AgentNotifications(
                    adapter=self.delivery,
                    environment={**self.environment, marker: value},
                ).handle("claude", self.payload("Stop"))
                self.assertEqual(result.outcome, "ignored")
        self.assertEqual(self.delivery.deliveries, [])

    def test_sessions_do_not_replace_each_others_notifications(self):
        first = self.notifications.handle("claude", self.payload("Stop"), preview=True)
        second = self.notifications.handle("claude", self.payload(
            "Stop", session_id="session-two"
        ), preview=True)
        again = self.notifications.handle("claude", self.payload("Stop"), preview=True)
        self.assertNotEqual(first.notification.group, second.notification.group)
        self.assertEqual(first.notification.group, again.notification.group)
        self.assertEqual(self.delivery.deliveries, [])
        self.assertFalse((self.home / ".claude").exists())

    def test_log_records_delivery_metadata_without_conversation_content(self):
        self.notifications.handle("claude", self.payload(
            "Stop", last_assistant_message="private answer"
        ))
        log = (self.home / ".claude/hooks/notify.jsonl").read_text()
        record = json.loads(log)
        self.assertEqual(record["session_id"], "session-one")
        self.assertEqual(record["channel"], "memory")
        self.assertEqual(record["event"], "Stop")
        self.assertNotIn("private answer", log)

    def test_empty_or_malformed_questions_have_a_useful_fallback(self):
        for tool_input in (None, [], {}, {"questions": []}, {"questions": [None]}):
            with self.subTest(tool_input=tool_input):
                result = self.notifications.handle("claude", self.payload(
                    "PreToolUse", tool_name="AskUserQuestion", tool_input=tool_input
                ), preview=True)
                self.assertEqual(result.notification.body, "Claude Code 正在等待你选择或输入")

    def test_blank_completion_message_uses_the_fallback(self):
        result = self.notifications.handle("claude", self.payload(
            "Stop", last_assistant_message=" \n\t "
        ), preview=True)
        self.assertEqual(result.notification.body, "任务已完成")

    def test_cli_preview_includes_the_exact_tmux_target_without_sending(self):
        focus = self.home / ".claude/hooks/focus-tmux.sh"
        focus.parent.mkdir(parents=True)
        focus.write_text("#!/bin/sh\nexit 0\n")
        focus.chmod(0o755)
        tmux = self.home / "fake-tmux"
        tmux.write_text(
            "#!/bin/sh\ncase \"$5\" in\n"
            "  *session_id*) printf '$0\\t2\\t1\\n' ;;\n"
            "  *) printf 'work\\t@2\\t%%3\\t2\\n' ;;\nesac\n"
        )
        tmux.chmod(0o755)
        transcript = self.transcript(
            {"type": "ai-title", "aiTitle": "北海道旅行"},
            {"type": "user", "message": {"content": "这是什么会话？"}},
        )
        result = subprocess.run(
            [str(ROOT / "bin/aipane-claude-notify")],
            input=json.dumps(self.payload(
                "Stop", transcript_path=transcript,
                last_assistant_message="简单说，这是 **北海道旅行** 会话。",
            )),
            text=True, capture_output=True, timeout=10,
            env={**self.environment, "CLAUDE_NOTIFY_DRY_RUN": "1",
                 "TMUX_PANE": "%3", "AIPANE_TMUX": str(tmux)},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        preview = json.loads(result.stdout)
        self.assertEqual(preview["outcome"], "previewed")
        self.assertEqual(preview["title"], "0:2:1 · 北海道旅行")
        self.assertEqual(preview["subtitle"], "这是什么会话？")
        self.assertEqual(preview["body"], "简单说，这是 北海道旅行 会话。")
        self.assertEqual(shlex.split(preview["action"]), [str(focus), "work", "@2", "%3", "2"])
        self.assertFalse((focus.parent / "notify.jsonl").exists())

    def test_configured_hooks_have_one_canonical_trigger_per_alert(self):
        config = json.loads((ROOT / "integrations/claude/hooks.json").read_text())["hooks"]
        cases = [
            ("Stop", "", 1), ("StopFailure", "rate_limit", 1),
            ("PermissionRequest", "Bash", 1),
            ("PermissionRequest", "AskUserQuestion", 0),
            ("PreToolUse", "AskUserQuestion", 1), ("PreToolUse", "Bash", 0),
            ("Notification", "elicitation_dialog", 1),
            ("Notification", "elicitation_url_dialog", 1),
            ("Notification", "agent_needs_input", 1),
            ("Notification", "permission_prompt", 0),
            ("Notification", "idle_prompt", 0),
            ("SubagentStop", "", 0),
        ]
        for event, value, expected in cases:
            with self.subTest(event=event, value=value):
                hooks = [hook for group in config.get(event, [])
                         if re.search(group.get("matcher", ""), value)
                         for hook in group["hooks"]
                         if "aipane-claude-notify" in hook["command"]]
                self.assertEqual(len(hooks), expected)
                for hook in hooks:
                    self.assertTrue(hook["async"])
                    self.assertGreaterEqual(hook["timeout"], 15)

    def test_registered_command_is_silent_and_uses_the_dedicated_app(self):
        sender = self.home / "Applications/Claude Code Notifier.app/Contents/MacOS/terminal-notifier"
        sender.parent.mkdir(parents=True)
        sender.write_text(
            f"#!{sys.executable}\nimport json,os,sys\n"
            "from pathlib import Path\n"
            "Path(os.environ['DELIVERY_LOG']).write_text(json.dumps(sys.argv[1:]))\n"
        )
        sender.chmod(0o755)
        entrypoint = self.home / ".local/bin/aipane-claude-notify"
        entrypoint.parent.mkdir(parents=True)
        entrypoint.symlink_to(ROOT / "bin/aipane-claude-notify")
        config = json.loads((ROOT / "integrations/claude/hooks.json").read_text())
        hook = next(hook for group in config["hooks"]["Stop"] for hook in group["hooks"]
                    if "aipane-claude-notify" in hook["command"])
        output = self.home / "delivery.json"
        result = subprocess.run(
            ["/bin/sh", "-c", hook["command"]],
            input=json.dumps(self.payload("Stop", last_assistant_message="完成")),
            env={**self.environment, "DELIVERY_LOG": str(output)},
            text=True, capture_output=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "")
        args = json.loads(output.read_text())
        self.assertEqual(args[args.index("-message") + 1], "完成")
        self.assertEqual(args[args.index("-subtitle") + 1], "任务完成")
        record = json.loads((self.home / ".claude/hooks/notify.jsonl").read_text())
        self.assertEqual(record["channel"], "custom-app")

    def test_malformed_cli_payload_does_not_turn_into_a_completion(self):
        for raw in ("invalid json", "[]", "null"):
            with self.subTest(raw=raw):
                result = subprocess.run(
                    [str(ROOT / "bin/aipane-claude-notify")], input=raw,
                    env={**self.environment, "CLAUDE_NOTIFY_DRY_RUN": "1"},
                    text=True, capture_output=True, timeout=10,
                )
                self.assertEqual(result.returncode, 0)
                self.assertEqual(result.stdout, "")
                self.assertEqual(result.stderr, "")
        self.assertFalse((self.home / ".claude").exists())


if __name__ == "__main__":
    unittest.main()
