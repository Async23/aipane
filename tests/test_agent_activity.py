"""Behavior tests for the pane-local Agent Activity resolver."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

from agent_activity import AgentActivity, PaneActivity, codex_goal_state  # noqa: E402


def make_codex_history(
    codex_home: Path,
    *,
    session_id: str,
    lifecycles: list[tuple[str, str, int, int | None]],
) -> Path:
    transcript = (
        codex_home
        / "sessions"
        / "2026"
        / "08"
        / "14"
        / f"rollout-2026-08-14T19-00-41-{session_id}.jsonl"
    )
    transcript.parent.mkdir(parents=True)
    records = []
    for turn_id, status, started_at, completed_at in lifecycles:
        records.append(
            {
                "type": "event_msg",
                "payload": {
                    "type": "task_started",
                    "turn_id": turn_id,
                    "started_at": started_at,
                },
            }
        )
        if status != "inProgress":
            records.append(
                {
                    "type": "event_msg",
                    "payload": {
                        "type": (
                            "turn_aborted"
                            if status == "interrupted"
                            else "task_complete"
                        ),
                        "turn_id": turn_id,
                        "completed_at": completed_at,
                        **(
                            {"error": {"message": "turn failed"}}
                            if status == "failed"
                            else {}
                        ),
                    },
                }
            )
    transcript.write_text(
        "".join(json.dumps(record) + "\n" for record in records),
        encoding="utf-8",
    )

    history = sqlite3.connect(codex_home / "thread_history_1.sqlite")
    try:
        history.executescript(
            """
            CREATE TABLE thread_turns (
                thread_id TEXT NOT NULL,
                turn_id TEXT NOT NULL,
                rollout_ordinal INTEGER NOT NULL,
                status TEXT NOT NULL,
                started_at INTEGER,
                completed_at INTEGER,
                rollout_end_byte_offset INTEGER,
                PRIMARY KEY (thread_id, turn_id)
            );
            CREATE TABLE thread_history_projection_state (
                thread_id TEXT PRIMARY KEY,
                next_rollout_byte_offset INTEGER NOT NULL,
                next_rollout_ordinal INTEGER NOT NULL
            );
            """
        )
        for ordinal, (turn_id, status, started_at, completed_at) in enumerate(
            lifecycles, start=1
        ):
            history.execute(
                "INSERT INTO thread_turns VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    session_id,
                    turn_id,
                    ordinal,
                    status,
                    started_at,
                    completed_at,
                    transcript.stat().st_size if status != "inProgress" else None,
                ),
            )
        history.execute(
            "INSERT INTO thread_history_projection_state VALUES (?, ?, ?)",
            (session_id, transcript.stat().st_size, len(lifecycles) + 1),
        )
        history.commit()
    finally:
        history.close()
    return transcript


def make_kimi_session(
    kimi_home: Path,
    *,
    session_id: str,
    wire_records: list[dict[str, object]],
    last_turn_reason: str,
    updated_at: int,
) -> Path:
    session_dir = kimi_home / "sessions" / "wd_removed_1234" / session_id
    wire = session_dir / "agents" / "main" / "wire.jsonl"
    wire.parent.mkdir(parents=True)
    wire.write_text(
        "".join(json.dumps(record) + "\n" for record in wire_records),
        encoding="utf-8",
    )
    (session_dir / "state.json").write_text(
        json.dumps(
            {
                "id": session_id,
                "updatedAt": updated_at,
                "lastTurnReason": last_turn_reason,
            }
        ),
        encoding="utf-8",
    )
    (kimi_home / "session_index.jsonl").write_text(
        json.dumps(
            {
                "sessionId": session_id,
                "sessionDir": str(session_dir),
                # The regression: Kimi's original cwd was renamed mid-turn.
                "workDir": str(kimi_home.parent / "removed-workspace"),
            }
        )
        + "\n",
        encoding="utf-8",
    )
    return session_dir


def make_kimi_busy_pane(
    kimi_home: Path,
    session_id: str,
    *,
    reported_at: int = 1_788_328_946_526,
) -> PaneActivity:
    return PaneActivity(
        pane_id="%7",
        current_command="kimi",
        pane_tty="/dev/ttys023",
        socket_path="/private/tmp/tmux/default",
        server_pid="30402",
        marker="kimi",
        reporter="kimi",
        marker_updated_at=str(reported_at),
        record=json.dumps(
            {
                "version": 1,
                "revision": "revision-kimi",
                "generation": "generation-kimi",
                "owner": "kimi",
                "reported": "busy",
                "updated_at": reported_at,
                "pane": {
                    "id": "%7",
                    "tty": "/dev/ttys023",
                    "socket": "/private/tmp/tmux/default",
                    "server_pid": "30402",
                },
                "process": {"pid": 123, "started_at": "process-1"},
                "session": {
                    "id": session_id,
                    "kimi_home": str(kimi_home),
                },
            }
        ),
    )


class AgentActivityTests(unittest.TestCase):
    def test_unrecognized_codex_goal_status_fails_closed(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            codex_home = Path(raw_tmp)
            connection = sqlite3.connect(codex_home / "goals_1.sqlite")
            try:
                connection.execute(
                    "CREATE TABLE thread_goals (thread_id TEXT PRIMARY KEY, status TEXT)"
                )
                connection.execute(
                    "INSERT INTO thread_goals VALUES (?, ?)",
                    ("thread-unknown-goal", "future_status"),
                )
                connection.commit()
            finally:
                connection.close()

            self.assertEqual(
                codex_goal_state(codex_home, "thread-unknown-goal"),
                "unknown",
            )

    def legacy_resolver_and_pane(
        self,
        home: Path,
        session_id: str,
    ) -> tuple[AgentActivity, PaneActivity]:
        registry = home / "registry.jsonl"
        registry.write_text(
            json.dumps(
                {
                    "ts": 1_786_705_287,
                    "sock": "/private/tmp/tmux/default",
                    "srv": "30402",
                    "pane": "%12",
                    "tool": "x",
                    "sid": session_id,
                }
            )
            + "\n",
            encoding="utf-8",
        )
        pane = PaneActivity(
            pane_id="%12",
            current_command="codex",
            pane_tty="/dev/ttys031",
            socket_path="/private/tmp/tmux/default",
            server_pid="30402",
            marker="codex",
            reporter="codex",
            marker_updated_at="1786706587976",
        )
        resolver = AgentActivity(
            environment={
                "HOME": str(home),
                "CODEX_HOME": str(home / ".codex"),
                "AIPANE_REGISTRY": str(registry),
            },
            process_started_at=lambda _pane: 1_786_705_241_000,
        )
        return resolver, pane

    def test_failed_codex_turn_supersedes_its_stale_busy_report(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            codex_home = Path(raw_tmp) / ".codex"
            transcript = (
                codex_home
                / "sessions"
                / "2026"
                / "08"
                / "14"
                / "rollout-2026-08-14T19-00-41-thread-1.jsonl"
            )
            transcript.parent.mkdir(parents=True)
            transcript.write_text(
                "\n".join(
                    (
                        json.dumps(
                            {
                                "type": "event_msg",
                                "payload": {
                                    "type": "task_started",
                                    "turn_id": "turn-1",
                                    "started_at": 1_786_705_339,
                                },
                            }
                        ),
                        json.dumps(
                            {
                                "type": "event_msg",
                                "payload": {
                                    "type": "task_complete",
                                    "turn_id": "turn-1",
                                    "error": {"message": "remote compact failed"},
                                    "completed_at": 1_786_707_036,
                                },
                            }
                        ),
                    )
                )
                + "\n",
                encoding="utf-8",
            )
            history = sqlite3.connect(codex_home / "thread_history_1.sqlite")
            try:
                history.executescript(
                    """
                    CREATE TABLE thread_turns (
                        thread_id TEXT NOT NULL,
                        turn_id TEXT NOT NULL,
                        rollout_ordinal INTEGER NOT NULL,
                        status TEXT NOT NULL,
                        started_at INTEGER,
                        completed_at INTEGER,
                        rollout_end_byte_offset INTEGER,
                        PRIMARY KEY (thread_id, turn_id)
                    );
                    CREATE TABLE thread_history_projection_state (
                        thread_id TEXT PRIMARY KEY,
                        next_rollout_byte_offset INTEGER NOT NULL,
                        next_rollout_ordinal INTEGER NOT NULL
                    );
                    """
                )
                history.execute(
                    """
                    INSERT INTO thread_turns VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "thread-1",
                        "turn-1",
                        1,
                        "failed",
                        1_786_705_339,
                        1_786_707_036,
                        transcript.stat().st_size,
                    ),
                )
                history.execute(
                    "INSERT INTO thread_history_projection_state VALUES (?, ?, ?)",
                    ("thread-1", transcript.stat().st_size, 2),
                )
                history.commit()
            finally:
                history.close()

            record = json.dumps(
                {
                    "version": 1,
                    "revision": "revision-1",
                    "generation": "generation-1",
                    "owner": "codex",
                    "reported": "busy",
                    "updated_at": 1_786_706_587_976,
                    "pane": {
                        "id": "%12",
                        "tty": "/dev/ttys031",
                        "socket": "/private/tmp/tmux/default",
                        "server_pid": "30402",
                    },
                    "process": {"pid": 6322, "started_at": "process-1"},
                    "root": {
                        "session_id": "thread-1",
                        "turn_id": "turn-1",
                        "transcript_path": str(transcript),
                        "codex_home": str(codex_home),
                    },
                },
                separators=(",", ":"),
            )
            pane = PaneActivity(
                pane_id="%12",
                current_command="codex",
                pane_tty="/dev/ttys031",
                socket_path="/private/tmp/tmux/default",
                server_pid="30402",
                marker="codex",
                reporter="codex",
                marker_updated_at="1786706587976",
                record=record,
            )

            view = AgentActivity(
                process_matches=lambda _process, _pane: True,
            ).resolve(pane)

            self.assertEqual(view.state, "idle")
            self.assertTrue(view.repairable)
            self.assertEqual(view.evidence_turn_id, "turn-1")

    def test_legacy_codex_marker_uses_matching_registry_binding(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            home = Path(raw_tmp)
            codex_home = home / ".codex"
            make_codex_history(
                codex_home,
                session_id="thread-legacy",
                lifecycles=[
                    ("turn-failed", "failed", 1_786_705_339, 1_786_707_036)
                ],
            )
            registry = home / "registry.jsonl"
            registry.write_text(
                json.dumps(
                    {
                        "ts": 1_786_705_287,
                        "sock": "/private/tmp/tmux/default",
                        "srv": "30402",
                        "pane": "%12",
                        "tool": "x",
                        "sid": "thread-legacy",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            pane = PaneActivity(
                pane_id="%12",
                current_command="codex",
                pane_tty="/dev/ttys031",
                socket_path="/private/tmp/tmux/default",
                server_pid="30402",
                marker="codex",
                reporter="codex",
                marker_updated_at="1786706587976",
            )

            resolver = AgentActivity(
                environment={
                    "HOME": str(home),
                    "CODEX_HOME": str(codex_home),
                    "AIPANE_REGISTRY": str(registry),
                },
                process_started_at=lambda _pane: 1_786_705_241_000,
            )
            view = resolver.resolve(pane)

            self.assertEqual(view.state, "idle")
            self.assertTrue(view.repairable)
            self.assertEqual(view.evidence_turn_id, "turn-failed")

            repaired = resolver.resolve(
                PaneActivity(
                    **{
                        **pane.__dict__,
                        "marker": "",
                        "marker_updated_at": "",
                        "record": view.repair_record,
                    }
                )
            )
            self.assertEqual(repaired.state, "idle")
            self.assertEqual(repaired.reason, "codex_turn_failed")

    def test_later_codex_turn_prevents_old_terminal_from_clearing_busy(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            home = Path(raw_tmp)
            make_codex_history(
                home / ".codex",
                session_id="thread-continuing",
                lifecycles=[
                    ("turn-old", "failed", 1_786_705_339, 1_786_705_500),
                    ("turn-new", "inProgress", 1_786_706_700, None),
                ],
            )
            resolver, pane = self.legacy_resolver_and_pane(
                home,
                "thread-continuing",
            )

            view = resolver.resolve(pane)

            self.assertEqual(view.state, "busy")
            self.assertFalse(view.repairable)
            self.assertEqual(view.evidence_turn_id, "turn-new")

    def test_stale_codex_projection_falls_back_to_canonical_rollout(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            home = Path(raw_tmp)
            codex_home = home / ".codex"
            make_codex_history(
                codex_home,
                session_id="thread-rollout",
                lifecycles=[
                    ("turn-rollout", "failed", 1_786_705_339, 1_786_707_036)
                ],
            )
            history = sqlite3.connect(codex_home / "thread_history_1.sqlite")
            try:
                history.execute(
                    """
                    UPDATE thread_history_projection_state
                    SET next_rollout_byte_offset = 0
                    WHERE thread_id = ?
                    """,
                    ("thread-rollout",),
                )
                history.commit()
            finally:
                history.close()
            resolver, pane = self.legacy_resolver_and_pane(
                home,
                "thread-rollout",
            )

            view = resolver.resolve(pane)

            self.assertEqual(view.state, "idle")
            self.assertEqual(view.reason, "codex_rollout_turn_failed")

    def test_new_codex_session_with_no_turn_remains_reported_idle(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            codex_home = Path(raw_tmp) / ".codex"
            transcript = make_codex_history(
                codex_home,
                session_id="thread-new-session",
                lifecycles=[],
            )
            record = json.dumps(
                {
                    "version": 1,
                    "revision": "revision-session",
                    "generation": "generation-session",
                    "owner": "codex",
                    "reported": "idle",
                    "updated_at": 1_786_706_587_976,
                    "pane": {
                        "id": "%12",
                        "tty": "/dev/ttys031",
                        "socket": "/private/tmp/tmux/default",
                        "server_pid": "30402",
                    },
                    "process": {"pid": 6322, "started_at": "process-1"},
                    "root": {
                        "session_id": "thread-new-session",
                        "turn_id": "",
                        "transcript_path": str(transcript),
                        "codex_home": str(codex_home),
                    },
                },
                separators=(",", ":"),
            )
            pane = PaneActivity(
                pane_id="%12",
                current_command="codex",
                pane_tty="/dev/ttys031",
                socket_path="/private/tmp/tmux/default",
                server_pid="30402",
                reporter="codex",
                record=record,
            )

            view = AgentActivity(
                process_matches=lambda _process, _pane: True,
            ).resolve(pane)

            self.assertEqual(view.state, "idle")
            self.assertEqual(view.reason, "codex_session_idle")

    def test_codex_prompt_report_captures_exact_turn_reference(self):
        pane = PaneActivity(
            pane_id="%12",
            current_command="codex",
            pane_tty="/dev/ttys031",
            socket_path="/private/tmp/tmux/default",
            server_pid="30402",
        )
        payload = {
            "hook_event_name": "UserPromptSubmit",
            "session_id": "thread-prompt",
            "turn_id": "turn-prompt",
            "transcript_path": "/tmp/codex/sessions/prompt.jsonl",
        }

        report = AgentActivity(
            environment={"CODEX_HOME": "/tmp/codex"},
            process_identity=lambda _pane: {
                "pid": 6322,
                "started_at": "Fri Aug 14 19:00:41 2026",
            },
        ).report(pane, "busy", payload, now_ms=1_786_706_587_976)

        record = json.loads(report.record)
        self.assertEqual(report.state, "busy")
        self.assertEqual(
            record["root"],
            {
                "session_id": "thread-prompt",
                "turn_id": "turn-prompt",
                "transcript_path": "/tmp/codex/sessions/prompt.jsonl",
                "codex_home": "/tmp/codex",
            },
        )
        self.assertEqual(record["pane"]["id"], "%12")
        self.assertEqual(record["process"]["pid"], 6322)

    def test_old_codex_completion_cannot_clear_a_newer_turn(self):
        process = {
            "pid": 6322,
            "started_at": "Fri Aug 14 19:00:41 2026",
        }
        activity = AgentActivity(
            environment={"CODEX_HOME": "/tmp/codex"},
            process_identity=lambda _pane: process,
        )
        pane = PaneActivity(
            pane_id="%12",
            current_command="codex",
            pane_tty="/dev/ttys031",
            socket_path="/private/tmp/tmux/default",
            server_pid="30402",
        )
        busy = activity.report(
            pane,
            "busy",
            {
                "hook_event_name": "UserPromptSubmit",
                "session_id": "thread-race",
                "turn_id": "turn-new",
                "transcript_path": "/tmp/codex/sessions/race.jsonl",
            },
            now_ms=2_000,
        )
        busy_pane = PaneActivity(
            **{
                **pane.__dict__,
                "marker": "codex",
                "reporter": "codex",
                "marker_updated_at": "2000",
                "record": busy.record,
            }
        )

        late = activity.report(
            busy_pane,
            "idle",
            {
                "type": "agent-turn-complete",
                "thread-id": "thread-race",
                "turn-id": "turn-old",
            },
            now_ms=2_001,
        )

        self.assertFalse(late.accepted)
        self.assertEqual(late.state, "busy")
        self.assertEqual(late.record, busy.record)

    def test_matching_completion_preserves_turn_start_for_later_resolution(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            codex_home = Path(raw_tmp) / ".codex"
            transcript = make_codex_history(
                codex_home,
                session_id="thread-complete",
                lifecycles=[
                    (
                        "turn-complete",
                        "completed",
                        1_786_705_339,
                        1_786_707_036,
                    )
                ],
            )
            process = {"pid": 6322, "started_at": "process-1"}
            activity = AgentActivity(
                environment={"CODEX_HOME": str(codex_home)},
                process_identity=lambda _pane: process,
                process_matches=lambda _process, _pane: True,
            )
            pane = PaneActivity(
                pane_id="%12",
                current_command="codex",
                pane_tty="/dev/ttys031",
                socket_path="/private/tmp/tmux/default",
                server_pid="30402",
            )
            busy = activity.report(
                pane,
                "busy",
                {
                    "hook_event_name": "UserPromptSubmit",
                    "session_id": "thread-complete",
                    "turn_id": "turn-complete",
                    "transcript_path": str(transcript),
                },
                now_ms=1_786_706_587_976,
            )
            busy_pane = PaneActivity(
                **{
                    **pane.__dict__,
                    "marker": "codex",
                    "reporter": "codex",
                    "marker_updated_at": "1786706587976",
                    "record": busy.record,
                }
            )

            completed = activity.report(
                busy_pane,
                "idle",
                {
                    "type": "agent-turn-complete",
                    "thread-id": "thread-complete",
                    "turn-id": "turn-complete",
                },
                now_ms=1_786_707_037_000,
            )
            completed_record = json.loads(completed.record)
            resolved = activity.resolve(
                PaneActivity(
                    **{
                        **busy_pane.__dict__,
                        "marker": "",
                        "marker_updated_at": "",
                        "record": completed.record,
                    }
                )
            )

            self.assertEqual(
                completed_record["updated_at"],
                1_786_706_587_976,
            )
            self.assertEqual(resolved.state, "idle")
            self.assertEqual(resolved.reason, "codex_turn_completed")

    def test_codex_completion_without_exact_record_cannot_clear_legacy_busy(self):
        pane = PaneActivity(
            pane_id="%12",
            current_command="codex",
            marker="codex",
            reporter="codex",
            marker_updated_at="2000",
        )

        completion = AgentActivity().report(
            pane,
            "idle",
            {
                "type": "agent-turn-complete",
                "thread-id": "thread-legacy",
                "turn-id": "turn-legacy",
            },
            now_ms=2_001,
        )

        self.assertFalse(completion.accepted)
        self.assertEqual(completion.state, "busy")
        self.assertEqual(completion.record, "")

    def test_codex_subagent_progress_does_not_replace_root_turn(self):
        process = {"pid": 6322, "started_at": "process-1"}
        activity = AgentActivity(
            environment={"CODEX_HOME": "/tmp/codex"},
            process_identity=lambda _pane: process,
        )
        pane = PaneActivity(
            pane_id="%12",
            current_command="codex",
            pane_tty="/dev/ttys031",
            socket_path="/private/tmp/tmux/default",
            server_pid="30402",
        )
        root = activity.report(
            pane,
            "busy",
            {
                "hook_event_name": "UserPromptSubmit",
                "session_id": "thread-root",
                "turn_id": "turn-root",
                "transcript_path": "/tmp/codex/sessions/root.jsonl",
            },
            now_ms=2_000,
        )
        busy_pane = PaneActivity(
            **{
                **pane.__dict__,
                "marker": "codex",
                "reporter": "codex",
                "marker_updated_at": "2000",
                "record": root.record,
            }
        )

        progress = activity.report(
            busy_pane,
            "busy",
            {
                "hook_event_name": "PreToolUse",
                "session_id": "thread-child",
                "turn_id": "turn-child",
                "transcript_path": "/tmp/codex/sessions/child.jsonl",
                "agent_id": "researcher",
            },
            now_ms=2_100,
        )

        self.assertTrue(progress.accepted)
        self.assertFalse(progress.wake)
        self.assertEqual(progress.record, root.record)

    def test_root_pre_tool_use_rebinds_when_a_new_turn_was_missed(self):
        process = {"pid": 6322, "started_at": "process-1"}
        activity = AgentActivity(
            environment={"CODEX_HOME": "/tmp/codex"},
            process_identity=lambda _pane: process,
        )
        pane = PaneActivity(
            pane_id="%12",
            current_command="codex",
            pane_tty="/dev/ttys031",
            socket_path="/private/tmp/tmux/default",
            server_pid="30402",
        )
        old_turn = activity.report(
            pane,
            "busy",
            {
                "hook_event_name": "UserPromptSubmit",
                "session_id": "thread-root",
                "turn_id": "turn-old",
                "transcript_path": "/tmp/codex/sessions/root.jsonl",
            },
            now_ms=2_000,
        )
        busy_pane = PaneActivity(
            **{
                **pane.__dict__,
                "marker": "codex",
                "reporter": "codex",
                "marker_updated_at": "2000",
                "record": old_turn.record,
            }
        )

        new_turn = activity.report(
            busy_pane,
            "busy",
            {
                "hook_event_name": "PreToolUse",
                "session_id": "thread-root",
                "turn_id": "turn-new",
                "transcript_path": "/tmp/codex/sessions/root.jsonl",
            },
            now_ms=2_100,
        )

        before = json.loads(old_turn.record)
        after = json.loads(new_turn.record)
        self.assertEqual(after["root"]["turn_id"], "turn-new")
        self.assertNotEqual(after["generation"], before["generation"])
        self.assertTrue(new_turn.wake)

    def test_reused_process_id_does_not_validate_a_stale_record(self):
        pane = PaneActivity(
            pane_id="%7",
            current_command="python3",
            socket_path="/private/tmp/tmux/default",
            server_pid="30402",
            marker="python3",
            reporter="python3",
            record=json.dumps(
                {
                    "version": 1,
                    "revision": "revision-stale-pid",
                    "generation": "generation-stale-pid",
                    "owner": "python3",
                    "reported": "busy",
                    "updated_at": 2_000,
                    "pane": {
                        "id": "%7",
                        "tty": "",
                        "socket": "/private/tmp/tmux/default",
                        "server_pid": "30402",
                    },
                    "process": {
                        "pid": os.getpid(),
                        "started_at": "Mon Jan 01 00:00:00 2001",
                    },
                }
            ),
        )

        view = AgentActivity().resolve(pane)

        self.assertEqual(view.state, "unknown")
        self.assertEqual(view.reason, "activity_identity_mismatch")

    def test_old_codex_session_end_cannot_clear_new_session(self):
        process = {"pid": 6322, "started_at": "process-1"}
        activity = AgentActivity(
            environment={"CODEX_HOME": "/tmp/codex"},
            process_identity=lambda _pane: process,
        )
        pane = PaneActivity(
            pane_id="%12",
            current_command="codex",
            pane_tty="/dev/ttys031",
            socket_path="/private/tmp/tmux/default",
            server_pid="30402",
        )
        busy = activity.report(
            pane,
            "busy",
            {
                "hook_event_name": "UserPromptSubmit",
                "session_id": "thread-new",
                "turn_id": "turn-new",
                "transcript_path": "/tmp/codex/sessions/new.jsonl",
            },
            now_ms=2_000,
        )
        busy_pane = PaneActivity(
            **{
                **pane.__dict__,
                "marker": "codex",
                "reporter": "codex",
                "marker_updated_at": "2000",
                "record": busy.record,
            }
        )

        ended = activity.report(
            busy_pane,
            "idle",
            {
                "hook_event_name": "SessionEnd",
                "session_id": "thread-old",
            },
            now_ms=2_100,
        )

        self.assertFalse(ended.accepted)
        self.assertEqual(ended.state, "busy")
        self.assertEqual(ended.record, busy.record)

    def test_claude_idle_registry_resolves_stale_busy_through_same_interface(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            config = Path(raw_tmp) / ".claude"
            sessions = config / "sessions"
            sessions.mkdir(parents=True)
            (sessions / "123.json").write_text(
                json.dumps(
                    {
                        "pid": 123,
                        "status": "idle",
                        "statusUpdatedAt": 2_000,
                        "tmux": "work:@8.%7",
                        "version": "2.1.229",
                    }
                ),
                encoding="utf-8",
            )
            pane = PaneActivity(
                pane_id="%7",
                target="work:@8.%7",
                current_command="2.1.229",
                pane_tty="/dev/ttys007",
                socket_path="/private/tmp/tmux/default",
                server_pid="30402",
                marker="2.1.229",
                reporter="2.1.229",
                marker_updated_at="1000",
                record=json.dumps(
                    {
                        "version": 1,
                        "revision": "revision-claude",
                        "generation": "generation-claude",
                        "owner": "2.1.229",
                        "reported": "busy",
                        "updated_at": 1_000,
                        "pane": {
                            "id": "%7",
                            "tty": "/dev/ttys007",
                            "socket": "/private/tmp/tmux/default",
                            "server_pid": "30402",
                        },
                        "process": {"pid": 123, "started_at": "process-1"},
                    }
                ),
            )

            view = AgentActivity(
                environment={"CLAUDE_CONFIG_DIR": str(config)},
                process_matches=lambda _process, _pane: True,
                process_exists=lambda _pid: True,
                process_tty=lambda _pid: "ttys007",
            ).resolve(pane)

            self.assertEqual(view.state, "idle")
            self.assertEqual(view.reason, "claude_registry_idle")

    def test_grok_completion_resolves_stale_busy_through_same_interface(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            grok_home = Path(raw_tmp) / ".grok"
            workspace = Path(raw_tmp) / "workspace"
            session_id = "e841e6b4-e97e-4ef1-b0b8-a321c7e9f7ce"
            session_dir = (
                grok_home
                / "sessions"
                / urllib.parse.quote(str(workspace), safe="")
                / session_id
            )
            session_dir.mkdir(parents=True)
            (grok_home / "active_sessions.json").write_text(
                json.dumps(
                    [
                        {
                            "session_id": session_id,
                            "pid": 123,
                            "cwd": str(workspace),
                        }
                    ]
                ),
                encoding="utf-8",
            )
            (session_dir / "updates.jsonl").write_text(
                json.dumps(
                    {
                        "params": {
                            "update": {"sessionUpdate": "turn_completed"},
                            "_meta": {"agentTimestampMs": 2_000},
                        }
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            pane = PaneActivity(
                pane_id="%7",
                current_command="grok-1.0.3-maco",
                pane_tty="/dev/ttys023",
                marker="grok-1.0.3-maco",
                reporter="grok-1.0.3-maco",
                marker_updated_at="1000",
            )

            view = AgentActivity(
                environment={"GROK_HOME": str(grok_home)},
                process_exists=lambda _pid: True,
                process_tty=lambda _pid: "ttys023",
            ).resolve(pane)

            self.assertEqual(view.state, "idle")
            self.assertEqual(view.reason, "grok_update_idle")

    def test_kimi_turn_end_resolves_busy_when_original_cwd_was_renamed(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            kimi_home = Path(raw_tmp) / ".kimi-code"
            session_id = "session_22d9202e-989b-459f-8ba3-2fafab56df19"
            make_kimi_session(
                kimi_home,
                session_id=session_id,
                wire_records=[
                    {
                        "type": "turn.prompt",
                        "agentId": "main",
                        "time": 1_788_328_946_527,
                    },
                    {
                        "type": "turn.ended",
                        "agentId": "main",
                        "time": 1_788_329_146_727,
                        "turnId": 0,
                        "reason": "completed",
                    },
                ],
                last_turn_reason="completed",
                updated_at=1_788_329_146_728,
            )
            pane = make_kimi_busy_pane(kimi_home, session_id)

            view = AgentActivity(
                environment={"KIMI_CODE_HOME": str(kimi_home)},
                process_matches=lambda _process, _pane: True,
            ).resolve(pane)

            self.assertEqual(view.state, "idle")
            self.assertEqual(view.reason, "kimi_turn_ended")
            self.assertEqual(view.evidence_turn_id, "0")
            self.assertTrue(view.repairable)

    def test_newer_kimi_prompt_prevents_previous_end_from_clearing_busy(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            kimi_home = Path(raw_tmp) / ".kimi-code"
            session_id = "session_22d9202e-989b-459f-8ba3-2fafab56df19"
            make_kimi_session(
                kimi_home,
                session_id=session_id,
                wire_records=[
                    {
                        "type": "turn.prompt",
                        "agentId": "main",
                        "time": 1_788_328_900_000,
                    },
                    {
                        "type": "turn.ended",
                        "agentId": "main",
                        "turnId": 0,
                        "reason": "completed",
                        "time": 1_788_328_910_000,
                    },
                    {
                        "type": "turn.prompt",
                        "agentId": "main",
                        "time": 1_788_328_946_527,
                    },
                ],
                # Simulate reading while state.json still lags the wire append.
                last_turn_reason="completed",
                updated_at=1_788_328_910_001,
            )
            pane = make_kimi_busy_pane(kimi_home, session_id)

            view = AgentActivity(
                process_matches=lambda _process, _pane: True,
            ).resolve(pane)

            self.assertEqual(view.state, "busy")
            self.assertEqual(view.reason, "reported_activity")
            self.assertFalse(view.repairable)

    def test_kimi_end_older_than_busy_report_cannot_clear_new_turn(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            kimi_home = Path(raw_tmp) / ".kimi-code"
            session_id = "session_22d9202e-989b-459f-8ba3-2fafab56df19"
            make_kimi_session(
                kimi_home,
                session_id=session_id,
                wire_records=[
                    {
                        "type": "turn.prompt",
                        "agentId": "main",
                        "time": 1_788_328_900_000,
                    },
                    {
                        "type": "turn.ended",
                        "agentId": "main",
                        "turnId": 0,
                        "reason": "completed",
                        "time": 1_788_328_910_000,
                    },
                ],
                last_turn_reason="completed",
                updated_at=1_788_328_910_001,
            )
            pane = make_kimi_busy_pane(kimi_home, session_id)

            view = AgentActivity(
                process_matches=lambda _process, _pane: True,
            ).resolve(pane)

            self.assertEqual(view.state, "busy")
            self.assertFalse(view.repairable)

    def test_kimi_busy_report_records_session_and_kimi_code_process(self):
        pane = PaneActivity(
            pane_id="%7",
            current_command="kimi",
            pane_tty="/dev/ttys023",
            socket_path="/private/tmp/tmux/default",
            server_pid="30402",
        )
        started_at = "Tue Sep  2 14:00:00 2026"
        process_line = f"1234 {started_at} /opt/kimi-code\n"
        with (
            mock.patch("agent_activity.os.getppid", return_value=6322),
            mock.patch(
                "agent_activity.subprocess.run",
                return_value=subprocess.CompletedProcess(
                    ["ps"],
                    0,
                    stdout=process_line,
                    stderr="",
                ),
            ),
        ):
            report = AgentActivity(
                environment={"KIMI_CODE_HOME": "/tmp/kimi-home"},
            ).report(
                pane,
                "busy",
                {
                    "hook_event_name": "TurnStarted",
                    "session_id": (
                        "session_22d9202e-989b-459f-8ba3-2fafab56df19"
                    ),
                },
                now_ms=1_788_328_946_526,
            )

        record = json.loads(report.record)
        self.assertEqual(
            record["session"],
            {
                "id": "session_22d9202e-989b-459f-8ba3-2fafab56df19",
                "kimi_home": "/tmp/kimi-home",
            },
        )
        self.assertEqual(
            record["process"],
            {"pid": 6322, "started_at": started_at},
        )

    def test_subagent_can_promote_idle_without_replacing_root_binding(self):
        process = {"pid": 6322, "started_at": "process-1"}
        activity = AgentActivity(
            environment={"CODEX_HOME": "/tmp/codex"},
            process_identity=lambda _pane: process,
        )
        pane = PaneActivity(
            pane_id="%12",
            current_command="codex",
            pane_tty="/dev/ttys031",
            socket_path="/private/tmp/tmux/default",
            server_pid="30402",
        )
        session = activity.report(
            pane,
            "idle",
            {
                "hook_event_name": "SessionStart",
                "source": "startup",
                "session_id": "thread-root",
                "transcript_path": "/tmp/codex/sessions/root.jsonl",
            },
            now_ms=2_000,
        )
        idle_pane = PaneActivity(
            **{
                **pane.__dict__,
                "reporter": "codex",
                "record": session.record,
            }
        )

        progress = activity.report(
            idle_pane,
            "busy",
            {
                "hook_event_name": "PreToolUse",
                "session_id": "thread-child",
                "turn_id": "turn-child",
                "transcript_path": "/tmp/codex/sessions/child.jsonl",
                "agent_id": "researcher",
            },
            now_ms=2_100,
        )

        before = json.loads(session.record)
        after = json.loads(progress.record)
        self.assertEqual(progress.state, "busy")
        self.assertEqual(after["root"], before["root"])
        self.assertEqual(after["generation"], before["generation"])


if __name__ == "__main__":
    unittest.main()
