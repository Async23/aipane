"""Antigravity lifecycle, session isolation and conservative recovery contracts."""

import json
from contextlib import closing
import os
from pathlib import Path
import shlex
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))
import agy_sessions as agy
from agent_activity import AgentActivity, InMemoryActivityAdapter, PaneActivity
from agent_notifications import AgentNotifications, InMemoryNotificationAdapter
from recovery_plan import normalize_plan, session_identity, ensure_unique_sessions, RecoveryPlanError
from recovery_snapshot import capture_snapshot

SID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
OTHER = "11111111-2222-4333-8444-555555555555"


class AntigravityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.cwd = self.root / "a project"
        self.cwd.mkdir()
        self.env = {**os.environ, "HOME": str(self.root), "AIPANE_STATE_DIR": str(self.root / "state"),
                    "TMUX": "/tmp/agy-test,111,0", "TMUX_PANE": "%1"}
        self.process = {"pid": 999, "ppid": 1, "started_at": 100.0, "foreground": True,
                        "tty": "ttys999", "executable": "agy"}
        self.integration = agy.AgyIntegration(self.env, reader=lambda pid: self.process,
                                               producer=lambda: self.process, pane_matches=lambda process: True)
        self.payload = {"cwd": str(self.cwd), "conversation_id": SID, "agent_state": "idle",
                        "model": {"id": "Gemini 4 Hydrogen", "effort": "high"}, "cycle_mode": "auto"}

    def status(self, **changes):
        return self.integration.status({**self.payload, **changes})

    def hook(self, event, **changes):
        return self.integration.hook(event, {"conversationId": SID,
                                            "transcriptPath": str(agy.cli_home(self.env) / "brain" / SID / "logs/transcript.jsonl"),
                                            **changes})

    def current(self, **changes):
        return agy.current_session(pane_id="%1", socket_path="/tmp/agy-test", server_pid="111",
                                   pane_tty="/dev/ttys999", environment=self.env,
                                   reader=lambda pid: {**self.process, **changes})

    def durable_session(self, sid=SID, **summary_changes):
        home = agy.cli_home(self.env)
        (home / "cache").mkdir(parents=True, exist_ok=True)
        summary = {"WorkspaceURIs": [self.cwd.as_uri()], "AppDataDir": "antigravity-cli", **summary_changes}
        (home / "cache/conversation_metadata.json").write_text(json.dumps({"conversations": {
            sid: {"summary": summary, "is_internal": False}}}))
        database = home / "conversations" / f"{sid}.db"
        database.parent.mkdir(exist_ok=True)
        with closing(sqlite3.connect(database)) as connection:
            connection.execute("CREATE TABLE IF NOT EXISTS steps(idx INTEGER PRIMARY KEY)")
            connection.execute("INSERT OR IGNORE INTO steps VALUES(1)")
            connection.execute("CREATE TABLE IF NOT EXISTS trajectory_meta(cascade_id TEXT)")
            connection.execute("INSERT INTO trajectory_meta VALUES(?)", (sid,))
            connection.commit()
        return home

    def test_initial_idle_and_resumed_history_do_not_notify(self):
        record, events = self.status()
        self.assertEqual(record["state"], "idle")
        self.assertEqual(events, [])
        self.assertEqual(self.hook("Stop", fullyIdle=True)[1], [])

    def test_background_command_outlives_turn_but_still_defers_notification(self):
        self.status(agent_state="working")
        self.assertEqual(self.hook("Stop", fullyIdle=True, continuationActive=False)[1], [])
        record, events = self.status(agent_state="idle", task_count=1)
        self.assertEqual(record["state"], "idle")
        self.assertEqual(self.current()["state"], "idle")
        self.assertEqual(events, [])
        record, events = self.status()
        self.assertEqual(events[0]["event"], "completed")

    def test_stop_after_idle_with_background_command_waits_to_notify(self):
        self.status(agent_state="working", task_count=1)
        record, events = self.status(task_count=1)
        self.assertEqual(record["state"], "idle")
        self.assertEqual(events, [])
        self.assertEqual(self.hook("Stop", fullyIdle=True, terminationReason="error", error="broken")[1], [])
        self.assertEqual(self.status()[1][0]["event"], "failed")

    def test_background_command_starting_before_delivery_defers_queued_completion(self):
        self.status(agent_state="working")
        self.hook("Stop", fullyIdle=True)
        event = self.status()[1][0]
        record, events = self.status(task_count=1)
        self.assertEqual(record["state"], "idle")
        self.assertEqual(events, [])
        with patch.object(agy, "process_info", return_value=self.process):
            self.assertFalse(agy.notification_current(event, self.env))
            retry = self.status()[1][0]
            self.assertEqual(retry["token"], event["token"])
            self.assertTrue(agy.notification_current(retry, self.env))

    def test_next_turn_has_a_distinct_notification_identity(self):
        self.status(agent_state="working")
        self.hook("Stop", fullyIdle=True)
        first = self.status()[1][0]
        self.hook("PreInvocation")
        self.hook("Stop", fullyIdle=True)
        second = self.status()[1][0]
        self.assertNotEqual(first["token"], second["token"])

    def test_first_turn_hooks_can_precede_the_first_status_with_a_session_id(self):
        self.status(conversation_id="")
        self.hook("PreInvocation", workspacePaths=[str(self.cwd)])
        self.hook("Stop", workspacePaths=[str(self.cwd)], terminationReason="NO_TOOL_CALL", fullyIdle=True)
        record, events = self.status()
        self.assertEqual(record["session_id"], SID)
        self.assertEqual(events[0]["event"], "completed")

    def test_live_binding_and_database_work_before_picker_cache_is_refreshed(self):
        home = self.durable_session()
        self.status()
        (home / "cache/conversation_metadata.json").unlink()
        with patch.dict(os.environ, self.env):
            self.assertTrue(agy.session_exists(SID, str(self.cwd), home))
            self.assertFalse(agy.session_exists(SID, str(self.root), home))

    def test_running_subagent_prevents_completion(self):
        self.status(agent_state="working")
        self.hook("Stop", fullyIdle=True)
        state, events = self.status(subagents=[{"status": "running"}])
        self.assertEqual(state["state"], "busy")
        self.assertEqual(events, [])

    def test_goal_continuation_is_busy_and_does_not_notify_a_false_completion(self):
        self.status(agent_state="working")
        self.hook("Stop", fullyIdle=True, continuationActive=True)
        record, events = self.status()
        self.assertEqual(record["state"], "busy")
        self.assertEqual(events, [])
        self.hook("PreInvocation")
        self.hook("Stop", fullyIdle=True, continuationActive=False)
        self.assertEqual(self.status()[1][0]["event"], "completed")

    def test_subagent_or_stale_session_hook_cannot_replace_root(self):
        self.status(agent_state="working")
        self.assertEqual(self.hook("Stop", conversationId=OTHER, fullyIdle=True), (None, []))
        self.assertEqual(self.current()["state"], "busy")

    def test_same_directory_panes_and_in_process_switch_are_independent(self):
        self.status()
        second_env = {**self.env, "TMUX_PANE": "%2"}
        second = agy.AgyIntegration(second_env, reader=lambda pid: self.process, producer=lambda: self.process, pane_matches=lambda process: True)
        second.status({**self.payload, "conversation_id": OTHER})
        self.assertEqual(self.current()["session_id"], SID)
        self.status(conversation_id=OTHER)
        self.assertEqual(self.current()["session_id"], OTHER)
        self.assertEqual(self.hook("Stop", fullyIdle=True), (None, []))

    def test_pid_reuse_wrong_tty_and_background_process_fail_closed(self):
        self.status()
        for changes in ({"started_at": 101}, {"tty": "ttys123"}, {"foreground": False}, {"executable": "zsh"}):
            with self.subTest(changes=changes):
                self.assertIsNone(self.current(**changes))

    def test_approval_notifies_once_per_dialog_and_questions_use_step_ids(self):
        self.status()
        self.assertEqual(self.status(tool_confirmation_pending=True)[1][0]["event"], "approval")
        self.assertEqual(self.status(tool_confirmation_pending=True)[1], [])
        self.status()
        self.assertEqual(self.status(tool_confirmation_pending=True)[1][0]["event"], "approval")
        event = self.hook("PreToolUse", stepId="step1", toolCall={"name": "ask_question", "args": {
            "questions": [{"question": "Which one?"}]}})[1][0]
        self.assertEqual(event["question"], "Which one?")
        self.assertEqual(event["token"], "question:step1")

    def test_runtime_rejects_untrusted_file_permissions(self):
        self.status()
        self.integration.path.chmod(0o666)
        self.assertIsNone(self.current())

    def test_nested_cli_on_another_tty_cannot_publish_to_parent_pane(self):
        self.integration.pane_matches = lambda process: False
        self.assertEqual(self.status(agent_state="working"), (None, []))

    def test_desktop_hook_cannot_finish_the_cli_even_if_conversation_id_matches(self):
        self.status(agent_state="working")
        self.assertEqual(self.hook("Stop", transcriptPath=str(self.root / ".gemini/antigravity/brain" / SID / "transcript.jsonl")), (None, []))

    def test_two_panes_cannot_automatically_restore_the_same_conversation(self):
        item = dict(target="s:1.1", tool="agy", kind="resume", restorable=True,
                    sid=SID, cwd=str(self.cwd), command="agy --conversation " + SID)
        with self.assertRaises(RecoveryPlanError):
            ensure_unique_sessions([item, {**item, "target": "s:1.2"}])

    def test_new_turn_invalidates_queued_completion(self):
        self.status(agent_state="working")
        self.hook("Stop", fullyIdle=True)
        event = self.status()[1][0]
        with patch.object(agy, "process_info", return_value=self.process):
            self.assertTrue(agy.notification_current(event, self.env))
            self.hook("PreInvocation")
            self.assertFalse(agy.notification_current(event, self.env))

    def test_legacy_model_variant_restores_effort_without_an_unsupported_flag(self):
        home = self.durable_session()
        command = agy.resume_command(SID, str(self.cwd), {"agy_home": str(home),
                                      "agy_model": "Gemini 3.1 Pro (Low)", "agy_effort": "low"})
        self.assertNotIn("--effort", shlex.split(command))

    def test_notification_uses_final_response_and_unwraps_user_request(self):
        transcript = self.root / "transcript.jsonl"
        transcript.write_text("\n".join(json.dumps(row) for row in [
            {"type": "USER_INPUT", "source": "USER_EXPLICIT", "content": "<USER_REQUEST>\nFix it\n</USER_REQUEST>"},
            {"type": "PLANNER_RESPONSE", "content": "Working on it"},
            {"type": "PLANNER_RESPONSE", "content": "Done"},
        ]))
        notifications = AgentNotifications(adapter=InMemoryNotificationAdapter(), environment=self.env)
        notice = notifications.handle("agy", {"event": "completed", "session_id": SID,
                    "token": "turn:1", "transcript_path": str(transcript)}, preview=True).notification
        self.assertEqual(notice.body, "Done")
        self.assertEqual(notice.subtitle, "Fix it")

    def test_empty_or_malformed_state_is_unknown(self):
        self.assertEqual(self.status(agent_state="authenticating")[0]["state"], "unknown")
        self.assertEqual(self.status(task_count="zero")[0]["state"], "unknown")

    def test_recovery_requires_local_cli_database_and_exact_workspace(self):
        home = self.durable_session()
        self.assertTrue(agy.session_exists(SID, str(self.cwd), home))
        self.assertFalse(agy.session_exists(SID, str(self.root), home))
        self.durable_session(AppDataDir="antigravity")
        self.assertFalse(agy.session_exists(SID, str(self.cwd), home))
        self.durable_session(Internal=True)
        self.assertFalse(agy.session_exists(SID, str(self.cwd), home))
        self.durable_session()
        (home / "conversations" / f"{SID}.db").unlink()
        self.assertFalse(agy.session_exists(SID, str(self.cwd), home))

    def test_resume_preserves_settings_and_quotes_arguments(self):
        home = self.durable_session()
        metadata = {"agy_home": str(home), "agy_model": "Gemini 4 Hydrogen", "agy_effort": "high", "agy_mode": "plan"}
        command = agy.resume_command(SID, str(self.cwd), metadata)
        self.assertEqual(shlex.split(command), ["agy", "--effort", "high", "--mode", "plan", "--model",
                                               "Gemini 4 Hydrogen", "--conversation", SID])
        with self.assertRaises(ValueError):
            agy.resume_command(SID, str(self.cwd), {**metadata, "agy_mode": "unknown"})
        plan = normalize_plan([dict(target="s:1.1", tool="agy", kind="resume", restorable=True,
                                    cwd=str(self.cwd), sid=SID, command=command, **metadata)], source="test")[0]
        self.assertEqual(plan["agy_model"], metadata["agy_model"])
        self.assertEqual(session_identity(plan).store, str(home))

    def test_snapshot_plan_resumes_exact_id_without_global_recent_cache(self):
        home = self.durable_session()
        dump = self.root / "dump"
        dump.write_text(f"pane\ts\t1\t0\t1\t1\tname\t:{self.cwd}\t1\tagy\t:agy\n")
        capture_snapshot(dump, {"s:1.1": {"sid": SID, "tool": "a", "cwd": str(self.cwd),
                                           "agy_home": str(home), "agy_model": "Gemini 4 Hydrogen"}}, ("/tmp/old", "100"))
        result = subprocess.run(["zsh", str(ROOT / "bin/ai-restore"), "--plan-json", "--dump", str(dump)],
                                env=self.env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        plan = json.loads(result.stdout)
        self.assertEqual(plan["sid"], SID)
        self.assertIn("--conversation", plan["command"])
        self.assertNotIn("--continue", plan["command"])

    def test_notification_preview_does_not_claim_or_deliver_and_duplicate_is_suppressed(self):
        adapter = InMemoryNotificationAdapter()
        notifications = AgentNotifications(adapter=adapter, environment=self.env)
        payload = {"event": "question", "session_id": SID, "token": "question:1", "question": "Pick a model"}
        self.assertEqual(notifications.handle("agy", payload, preview=True).outcome, "previewed")
        self.assertEqual(adapter.deliveries, [])
        self.assertFalse((agy.state_dir(self.env) / "notifications").exists())
        self.assertEqual(notifications.handle("agy", payload).outcome, "delivered")
        self.assertEqual(notifications.handle("agy", payload).outcome, "deduplicated")
        self.assertEqual(len(adapter.deliveries), 1)
        self.assertEqual(notifications.handle("agy", {**payload, "token": "question:2"}).outcome, "delivered")

    def test_launcher_passes_model_arguments_without_preallocating_a_session(self):
        output = self.root / "arguments.json"
        fixture = self.root / "fake-agy"
        fixture.write_text("#!/usr/bin/env python3\nimport json,sys\nfrom pathlib import Path\n"
                           f"Path({str(output)!r}).write_text(json.dumps(sys.argv[1:]))\n")
        fixture.chmod(0o755)
        command = f"source {shlex.quote(str(ROOT / 'init.zsh'))}; ai a --model 'Gemini 4 Hydrogen' --effort high"
        result = subprocess.run(["zsh", "-c", command], env={**self.env, "AIPANE_AGY_LAUNCH_CMD": str(fixture)},
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(output.read_text()), ["--model", "Gemini 4 Hydrogen", "--effort", "high"])

    def test_question_observer_never_denies_tools_even_with_bad_input(self):
        for raw in ("{}", "{", "[]"):
            result = subprocess.run([str(ROOT / "bin/aipane-agy"), "PreToolUse"], input=raw,
                                    env={**self.env, "TMUX": "", "TMUX_PANE": ""},
                                    text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout), {"decision": "allow"})

    def test_native_tool_call_id_deduplicates_question_callbacks(self):
        self.status(agent_state="working")
        payload = {"toolCall": {"id": "tool-uuid", "name": "ask_question", "args": {
            "questions": [{"question": "Which one?"}]}}, "stepIdx": 0}
        first = self.hook("PreToolUse", **payload)[1][0]
        second = self.hook("PreToolUse", **payload)[1][0]
        self.assertEqual(first["token"], second["token"])


if __name__ == "__main__":
    unittest.main()
