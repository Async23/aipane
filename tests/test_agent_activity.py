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

from agent_activity import (  # noqa: E402
    ACTIVITY_REPORTER_OPTION,
    ActivityReadError,
    ActivityWriteError,
    AgentActivity,
    InMemoryActivityAdapter,
    ListedPaneActivity,
    PaneActivity,
    codex_goal_state,
)


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
    def activity_for(self, pane: PaneActivity, **kwargs):
        environment = kwargs.get("environment")
        adapter = InMemoryActivityAdapter(
            [pane],
            environment=environment,
        )
        return AgentActivity(adapter=adapter, **kwargs), adapter

    def test_projection_read_failure_is_reported_as_unknown(self):
        class UnavailableAdapter(InMemoryActivityAdapter):
            def read(self, target):
                raise ActivityReadError(f"{target} disappeared")

            def list_panes(self):
                raise ActivityReadError("tmux unavailable")

        activity = AgentActivity(adapter=UnavailableAdapter())
        result = activity.inspect("%9")

        self.assertFalse(result.available)
        self.assertEqual(result.state, "unknown")
        self.assertEqual(result.reason, "activity_projection_unavailable")
        report = activity.report("%9", "busy")
        self.assertEqual(report.state, "unknown")
        self.assertFalse(report.accepted)
        reconcile = activity.reconcile()
        self.assertFalse(reconcile.available)
        self.assertTrue(reconcile.errors)

    def test_failed_projection_change_rolls_back_prior_writes(self):
        pane = PaneActivity(
            pane_id="%7",
            current_command="codex",
            marker="codex",
            reporter="codex",
            marker_updated_at="1000",
            record='{"version":1}',
        )

        class FailingAdapter(InMemoryActivityAdapter):
            def unset_option(self, target, option):
                if option == ACTIVITY_REPORTER_OPTION:
                    raise ActivityWriteError("synthetic failure")
                super().unset_option(target, option)

        adapter = FailingAdapter([pane])
        activity = AgentActivity(adapter=adapter)

        with self.assertRaisesRegex(ActivityWriteError, "synthetic failure"):
            activity.clear(pane.pane_id)

        self.assertEqual(adapter.read(pane.pane_id), pane)

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
        resolver, _adapter = self.activity_for(
            pane,
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

            resolver, adapter = self.activity_for(
                pane,
                process_matches=lambda _process, _pane: True,
            )
            view = resolver.inspect(pane.pane_id)

            self.assertEqual(view.state, "idle")
            self.assertTrue(view.repairable)
            self.assertEqual(view.evidence, "turn-1")

            self.assertTrue(resolver.reconcile().busy)
            stale = adapter.read(pane.pane_id)
            adapter.list_panes = lambda: (
                "owner",
                [ListedPaneActivity(True, stale)],
            )
            adapter.add(
                PaneActivity(
                    **{**stale.__dict__, "pane_tty": "/dev/ttys099"}
                )
            )
            write_count = len(adapter.writes)
            raced = resolver.reconcile()
            self.assertFalse(raced.changed)
            self.assertTrue(raced.busy)
            self.assertEqual(len(adapter.writes), write_count)

    def test_codex_early_idle_report_is_ignored_unless_compatibility_is_enabled(self):
        pane = PaneActivity(
            pane_id="%12",
            current_command="codex",
            marker="codex",
            reporter="codex",
        )
        activity, adapter = self.activity_for(pane)

        ignored = activity.report(
            pane.pane_id,
            "idle",
            {"hook_event_name": "Stop"},
            now_ms=2_000,
        )

        self.assertFalse(ignored.accepted)
        self.assertEqual(adapter.read(pane.pane_id).marker, "codex")

        compatible, compatible_adapter = self.activity_for(
            pane,
            environment={"TMUX_WINDOW_WRAP_ALLOW_CODEX_STOP_IDLE": "1"},
        )
        accepted = compatible.report(
            pane.pane_id,
            "idle",
            {"hook_event_name": "Stop"},
            now_ms=2_000,
        )
        self.assertTrue(accepted.accepted)
        self.assertEqual(compatible_adapter.read(pane.pane_id).marker, "")

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

            resolver, adapter = self.activity_for(
                pane,
                environment={
                    "HOME": str(home),
                    "CODEX_HOME": str(codex_home),
                    "AIPANE_REGISTRY": str(registry),
                },
                process_started_at=lambda _pane: 1_786_705_241_000,
            )
            view = resolver.inspect(pane.pane_id)

            self.assertEqual(view.state, "idle")
            self.assertTrue(view.repairable)
            self.assertEqual(view.evidence, "turn-failed")

            self.assertTrue(resolver.reconcile().busy)
            self.assertTrue(resolver.reconcile().changed)
            repaired = resolver.inspect(pane.pane_id)
            self.assertEqual(repaired.state, "idle")
            self.assertEqual(repaired.reason, "codex_turn_failed")
            self.assertTrue(adapter.read(pane.pane_id).record)

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

            view = resolver.inspect(pane.pane_id)

            self.assertEqual(view.state, "busy")
            self.assertFalse(view.repairable)
            self.assertEqual(view.evidence, "turn-new")

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

            view = resolver.inspect(pane.pane_id)

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

            resolver, _adapter = self.activity_for(
                pane,
                process_matches=lambda _process, _pane: True,
            )
            view = resolver.inspect(pane.pane_id)

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

        activity, adapter = self.activity_for(
            pane,
            environment={"CODEX_HOME": "/tmp/codex"},
            process_identity=lambda _pane: {
                "pid": 6322,
                "started_at": "Fri Aug 14 19:00:41 2026",
            },
        )
        report = activity.report(
            pane.pane_id,
            "busy",
            payload,
            now_ms=1_786_706_587_976,
        )

        record = json.loads(adapter.read(pane.pane_id).record)
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
        activity, adapter = self.activity_for(
            PaneActivity(
                pane_id="%12",
                current_command="codex",
                pane_tty="/dev/ttys031",
                socket_path="/private/tmp/tmux/default",
                server_pid="30402",
            ),
            environment={"CODEX_HOME": "/tmp/codex"},
            process_identity=lambda _pane: process,
        )
        pane = adapter.read("%12")
        busy = activity.report(
            pane.pane_id,
            "busy",
            {
                "hook_event_name": "UserPromptSubmit",
                "session_id": "thread-race",
                "turn_id": "turn-new",
                "transcript_path": "/tmp/codex/sessions/race.jsonl",
            },
            now_ms=2_000,
        )
        busy_record = adapter.read(pane.pane_id).record

        late = activity.report(
            pane.pane_id,
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
        self.assertEqual(adapter.read(pane.pane_id).record, busy_record)

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
            pane = PaneActivity(
                pane_id="%12",
                current_command="codex",
                pane_tty="/dev/ttys031",
                socket_path="/private/tmp/tmux/default",
                server_pid="30402",
            )
            activity, adapter = self.activity_for(
                pane,
                environment={"CODEX_HOME": str(codex_home)},
                process_identity=lambda _pane: process,
                process_matches=lambda _process, _pane: True,
            )
            busy = activity.report(
                pane.pane_id,
                "busy",
                {
                    "hook_event_name": "UserPromptSubmit",
                    "session_id": "thread-complete",
                    "turn_id": "turn-complete",
                    "transcript_path": str(transcript),
                },
                now_ms=1_786_706_587_976,
            )
            busy_record = adapter.read(pane.pane_id).record

            completed = activity.report(
                pane.pane_id,
                "idle",
                {
                    "type": "agent-turn-complete",
                    "thread-id": "thread-complete",
                    "turn-id": "turn-complete",
                },
                now_ms=1_786_707_037_000,
            )
            completed_record = json.loads(adapter.read(pane.pane_id).record)
            resolved = activity.inspect(pane.pane_id)

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

        activity, adapter = self.activity_for(pane)
        completion = activity.report(
            pane.pane_id,
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
        self.assertEqual(adapter.read(pane.pane_id).record, "")

    def test_codex_subagent_progress_does_not_replace_root_turn(self):
        process = {"pid": 6322, "started_at": "process-1"}
        pane = PaneActivity(
            pane_id="%12",
            current_command="codex",
            pane_tty="/dev/ttys031",
            socket_path="/private/tmp/tmux/default",
            server_pid="30402",
        )
        activity, adapter = self.activity_for(
            pane,
            environment={"CODEX_HOME": "/tmp/codex"},
            process_identity=lambda _pane: process,
        )
        root = activity.report(
            pane.pane_id,
            "busy",
            {
                "hook_event_name": "UserPromptSubmit",
                "session_id": "thread-root",
                "turn_id": "turn-root",
                "transcript_path": "/tmp/codex/sessions/root.jsonl",
            },
            now_ms=2_000,
        )
        root_record = adapter.read(pane.pane_id).record
        wake_count = adapter.wake_count

        progress = activity.report(
            pane.pane_id,
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
        self.assertEqual(adapter.wake_count, wake_count)
        self.assertEqual(adapter.read(pane.pane_id).record, root_record)

    def test_root_pre_tool_use_rebinds_when_a_new_turn_was_missed(self):
        process = {"pid": 6322, "started_at": "process-1"}
        pane = PaneActivity(
            pane_id="%12",
            current_command="codex",
            pane_tty="/dev/ttys031",
            socket_path="/private/tmp/tmux/default",
            server_pid="30402",
        )
        activity, adapter = self.activity_for(
            pane,
            environment={"CODEX_HOME": "/tmp/codex"},
            process_identity=lambda _pane: process,
        )
        old_turn = activity.report(
            pane.pane_id,
            "busy",
            {
                "hook_event_name": "UserPromptSubmit",
                "session_id": "thread-root",
                "turn_id": "turn-old",
                "transcript_path": "/tmp/codex/sessions/root.jsonl",
            },
            now_ms=2_000,
        )
        old_record = adapter.read(pane.pane_id).record

        new_turn = activity.report(
            pane.pane_id,
            "busy",
            {
                "hook_event_name": "PreToolUse",
                "session_id": "thread-root",
                "turn_id": "turn-new",
                "transcript_path": "/tmp/codex/sessions/root.jsonl",
            },
            now_ms=2_100,
        )

        before = json.loads(old_record)
        after = json.loads(adapter.read(pane.pane_id).record)
        self.assertEqual(after["root"]["turn_id"], "turn-new")
        self.assertNotEqual(after["generation"], before["generation"])

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

        activity, _adapter = self.activity_for(pane)
        view = activity.inspect(pane.pane_id)

        self.assertEqual(view.state, "unknown")
        self.assertEqual(view.reason, "activity_identity_mismatch")

    def test_old_codex_session_end_cannot_clear_new_session(self):
        process = {"pid": 6322, "started_at": "process-1"}
        pane = PaneActivity(
            pane_id="%12",
            current_command="codex",
            pane_tty="/dev/ttys031",
            socket_path="/private/tmp/tmux/default",
            server_pid="30402",
        )
        activity, adapter = self.activity_for(
            pane,
            environment={"CODEX_HOME": "/tmp/codex"},
            process_identity=lambda _pane: process,
        )
        busy = activity.report(
            pane.pane_id,
            "busy",
            {
                "hook_event_name": "UserPromptSubmit",
                "session_id": "thread-new",
                "turn_id": "turn-new",
                "transcript_path": "/tmp/codex/sessions/new.jsonl",
            },
            now_ms=2_000,
        )
        busy_record = adapter.read(pane.pane_id).record

        ended = activity.report(
            pane.pane_id,
            "idle",
            {
                "hook_event_name": "SessionEnd",
                "session_id": "thread-old",
            },
            now_ms=2_100,
        )

        self.assertFalse(ended.accepted)
        self.assertEqual(ended.state, "busy")
        self.assertEqual(adapter.read(pane.pane_id).record, busy_record)

    def test_report_owner_accepts_claude_native_names_and_rejects_foreign_panes(self):
        for command in ("claude", "2.1.317", "codex", "dsh-tui", "node", "kimi"):
            for state in ("busy", "idle"):
                with self.subTest(command=command, state=state):
                    activity, adapter = self.activity_for(
                        PaneActivity(pane_id="%7", current_command=command),
                        process_identity=lambda _pane: {"pid": 6322, "started_at": "process-1"},
                        process_matches=lambda _process, _pane: True,
                    )
                    activity.report("%7", "idle" if state == "busy" else "busy")
                    original = adapter.read("%7")
                    writes = len(adapter.writes)
                    report = activity.report(
                        "%7", state, {"hook_event_name": "UserPromptSubmit"},
                        owner="claude",
                    )
                    if command in {"claude", "2.1.317"}:
                        self.assertTrue(report.accepted)
                        self.assertEqual(report.state, state)
                        self.assertEqual(
                            adapter.read("%7").marker, command if state == "busy" else "",
                        )
                    else:
                        self.assertFalse(report.accepted)
                        self.assertEqual(adapter.read("%7"), original)
                        self.assertEqual(len(adapter.writes), writes)

    def test_claude_native_name_binds_reports_to_the_live_process(self):
        started_at = "Fri Oct 16 09:15:30 2026"
        for pane_command, process_command, matches in (
            ("2.1.301", "claude", True),
            ("claude", "2.1.301", True),
            ("2.1.301", "node", False),
            ("2.1.301", "2.1.300", False),
            ("codex", "claude", False),
            ("2.1", "claude", False),
        ):
            with self.subTest(pane=pane_command, process=process_command):
                pane = PaneActivity(
                    pane_id="%7",
                    current_command=pane_command,
                    pane_tty="/dev/ttys003",
                    socket_path="/private/tmp/tmux/default",
                    server_pid="30402",
                )

                def ps_result(arguments, **_kwargs):
                    columns = arguments[-1]
                    if columns == "ppid=,lstart=,comm=":
                        output = f"1 {started_at} /opt/bin/{process_command}\n"
                    elif columns == "lstart=,tty=":
                        output = f"{started_at} ttys003\n"
                    else:
                        output = ""
                    return subprocess.CompletedProcess(
                        arguments, 0, stdout=output, stderr="",
                    )

                with (
                    mock.patch("agent_activity.os.getppid", return_value=6322),
                    mock.patch("agent_activity.os.kill"),
                    mock.patch("agent_activity.subprocess.run", side_effect=ps_result),
                ):
                    activity, adapter = self.activity_for(pane)
                    activity.report(
                        pane.pane_id, "busy",
                        {"hook_event_name": "UserPromptSubmit"}, now_ms=1_000,
                    )
                    record = json.loads(adapter.read(pane.pane_id).record)
                    self.assertEqual(
                        record["process"],
                        {"pid": 6322, "started_at": started_at} if matches else {},
                    )
                    self.assertEqual(
                        activity.inspect(pane.pane_id).state,
                        "busy" if matches else "unknown",
                    )
                    if matches:
                        activity.report(
                            pane.pane_id, "idle",
                            {"hook_event_name": "Stop"}, now_ms=2_000,
                        )
                        self.assertEqual(activity.inspect(pane.pane_id).state, "idle")
                        self.assertEqual(adapter.read(pane.pane_id).marker, "")

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

            activity, adapter = self.activity_for(
                pane,
                environment={"CLAUDE_CONFIG_DIR": str(config)},
                process_matches=lambda _process, _pane: True,
                process_exists=lambda _pid: True,
                process_tty=lambda _pid: "ttys007",
            )
            view = activity.inspect(pane.pane_id)

            self.assertEqual(view.state, "idle")
            self.assertEqual(view.reason, "claude_registry_idle")

            adapter.add(
                PaneActivity(
                    **{**pane.__dict__, "marker_updated_at": "2001"}
                )
            )
            newer = activity.inspect(pane.pane_id)
            self.assertEqual(newer.state, "busy")

    def test_grok_current_session_is_available_before_any_completed_turn(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            grok_home = Path(raw_tmp)
            session_id = "e841e6b4-e97e-4ef1-b0b8-a321c7e9f7ce"
            active = grok_home / "active_sessions.json"
            active.write_text(json.dumps([{
                "session_id": session_id, "pid": 123, "cwd": raw_tmp,
            }]), encoding="utf-8")
            pane = PaneActivity(
                pane_id="%7", current_command="grok-1.0.13-mac",
                pane_tty="/dev/ttys023",
            )
            activity, adapter = self.activity_for(
                pane, environment={"GROK_HOME": str(grok_home)},
                process_exists=lambda pid: pid == 123,
                process_tty=lambda _pid: "ttys023",
            )
            view = activity.inspect(pane.pane_id)
            self.assertIsNotNone(view.session)
            self.assertEqual(view.session.session_id, session_id)
            self.assertEqual(view.session.tool_key, "g")
            self.assertEqual(view.state, "unknown")
            self.assertEqual(adapter.writes, [])

    def test_grok_current_session_rejects_dead_foreign_and_ambiguous_records(self):
        sid = "e841e6b4-e97e-4ef1-b0b8-a321c7e9f7ce"
        for scenario in ("dead", "foreign_tty", "ambiguous", "invalid", "shell"):
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as raw_tmp:
                grok_home = Path(raw_tmp)
                records = [{"session_id": sid, "pid": 123, "cwd": raw_tmp}]
                if scenario == "ambiguous":
                    records.append({
                        "session_id": "11111111-2222-4333-8444-555555555555",
                        "pid": 124, "cwd": raw_tmp,
                    })
                if scenario == "invalid":
                    records[0]["session_id"] = "not-a-session-id"
                (grok_home / "active_sessions.json").write_text(
                    json.dumps(records), encoding="utf-8",
                )
                pane = PaneActivity(
                    pane_id="%7", current_command="zsh" if scenario == "shell" else "grok",
                    pane_tty="/dev/ttys023",
                )
                activity, _adapter = self.activity_for(
                    pane, environment={"GROK_HOME": str(grok_home)},
                    process_exists=lambda _pid: scenario != "dead",
                    process_tty=lambda _pid: "ttys099" if scenario == "foreign_tty" else "ttys023",
                )
                self.assertIsNone(activity.inspect(pane.pane_id).session)

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

            activity, adapter = self.activity_for(
                pane,
                environment={"GROK_HOME": str(grok_home)},
                process_exists=lambda _pid: True,
                process_tty=lambda _pid: "ttys023",
            )
            view = activity.inspect(pane.pane_id)

            self.assertEqual(view.state, "idle")
            self.assertEqual(view.reason, "grok_update_idle")

            adapter.add(
                PaneActivity(
                    **{**pane.__dict__, "marker_updated_at": "2000001"}
                )
            )
            newer = activity.inspect(pane.pane_id)
            self.assertEqual(newer.state, "busy")

    def test_grok_hook_report_reconciles_ctrl_c_and_preserves_next_turn(self):
        # tmux reports the versioned executable while ps reports the launch
        # symlink. Exercise the actual identity reader, not an injected identity.
        for command in ("grok", "grok-1.0.30-mac"):
            with self.subTest(command=command), tempfile.TemporaryDirectory() as raw_tmp:
                grok_home = Path(raw_tmp)
                session_id = "e841e6b4-e97e-4ef1-b0b8-a321c7e9f7ce"
                session_dir = (
                    grok_home / "sessions"
                    / urllib.parse.quote(raw_tmp, safe="") / session_id
                )
                session_dir.mkdir(parents=True)
                (grok_home / "active_sessions.json").write_text(
                    json.dumps([{
                        "session_id": session_id, "pid": 123, "cwd": raw_tmp,
                    }]), encoding="utf-8",
                )
                pane = PaneActivity(
                    pane_id="%7", current_command=command,
                    pane_tty="/dev/ttys018",
                )
                activity, adapter = self.activity_for(
                    pane, environment={"GROK_HOME": raw_tmp},
                    process_exists=lambda pid: pid == 123,
                    process_tty=lambda _pid: "ttys018",
                )
                started_at = "Mon Sep 14 11:02:30 2026"
                live_start, live_tty = started_at, "ttys018"

                def fake_ps(arguments, **_kwargs):
                    self.assertEqual(arguments[0], "ps")
                    output = {
                        "ppid=,lstart=,comm=": f"1 {started_at} grok\n",
                        "lstart=,tty=": f"{live_start} {live_tty}\n",
                        "lstart=,comm=": f"{started_at} grok\n",
                    }[arguments[-1]]
                    return subprocess.CompletedProcess(arguments, 0, output, "")

                with (
                    mock.patch("agent_activity.os.getppid", return_value=123),
                    mock.patch("agent_activity.os.kill"),
                    mock.patch("agent_activity.subprocess.run", side_effect=fake_ps),
                ):
                    activity.report(
                        pane.pane_id, "busy",
                        {"hook_event_name": "UserPromptSubmit"},
                        now_ms=1_789_354_965_249,
                    )
                    self.assertEqual(activity.inspect(pane.pane_id).state, "busy")
                    (session_dir / "updates.jsonl").write_text(
                        json.dumps({
                            "timestamp": 1_789_355_332,
                            "method": "_x.ai/session/update",
                            "params": {
                                "sessionId": session_id,
                                "update": {
                                    "sessionUpdate": "turn_completed",
                                    "stop_reason": "cancelled",
                                },
                                "_meta": {
                                    "agentTimestampMs": 1_789_355_332_237,
                                    "cancelTrigger": "ctrl_c",
                                    "cancellationCategory": "MidTurnAbort",
                                },
                            },
                        }) + "\n", encoding="utf-8",
                    )
                    # Completion evidence must not bypass process identity.
                    for live_start, live_tty in (
                        ("Mon Sep 14 11:10:00 2026", "ttys018"),
                        (started_at, "ttys099"),
                    ):
                        self.assertEqual(activity.inspect(pane.pane_id).state, "unknown")
                        for _ in range(2):
                            self.assertFalse(activity.reconcile().changed)
                        self.assertEqual(adapter.read(pane.pane_id).marker, command)
                    live_start, live_tty = started_at, "ttys018"
                    view = activity.inspect(pane.pane_id)
                    self.assertEqual(view.state, "idle")
                    self.assertEqual(view.reason, "grok_update_idle")
                    self.assertTrue(view.repairable)

                    first = activity.reconcile()
                    self.assertTrue(first.busy)
                    self.assertFalse(first.changed)
                    second = activity.reconcile()
                    self.assertFalse(second.busy)
                    self.assertTrue(second.changed)
                    repaired = adapter.read(pane.pane_id)
                    self.assertEqual(repaired.marker, "")
                    self.assertEqual(json.loads(repaired.record)["reported"], "idle")
                    self.assertEqual(activity.inspect(pane.pane_id).state, "idle")
                    self.assertFalse(activity.reconcile().busy)

                    # A new report between the two observations invalidates
                    # the pending repair even when it is the same session.
                    activity.report(pane.pane_id, "busy", now_ms=1_789_354_965_249)
                    self.assertFalse(activity.reconcile().changed)
                    activity.report(
                        pane.pane_id, "busy",
                        {"hook_event_name": "UserPromptSubmit"},
                        now_ms=1_789_355_333_000,
                    )
                    self.assertEqual(activity.inspect(pane.pane_id).state, "busy")
                    for _ in range(2):
                        result = activity.reconcile()
                        self.assertTrue(result.busy)
                        self.assertFalse(result.changed)
                    self.assertEqual(adapter.read(pane.pane_id).marker, command)

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

            activity, _adapter = self.activity_for(
                pane,
                environment={"KIMI_CODE_HOME": str(kimi_home)},
                process_matches=lambda _process, _pane: True,
            )
            view = activity.inspect(pane.pane_id)

            self.assertEqual(view.state, "idle")
            self.assertEqual(view.reason, "kimi_turn_ended")
            self.assertEqual(view.evidence, "0")
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

            activity, _adapter = self.activity_for(
                pane,
                process_matches=lambda _process, _pane: True,
            )
            view = activity.inspect(pane.pane_id)

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

            activity, _adapter = self.activity_for(
                pane,
                process_matches=lambda _process, _pane: True,
            )
            view = activity.inspect(pane.pane_id)

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
            activity, adapter = self.activity_for(
                pane,
                environment={"KIMI_CODE_HOME": "/tmp/kimi-home"},
            )
            report = activity.report(
                pane.pane_id,
                "busy",
                {
                    "hook_event_name": "TurnStarted",
                    "session_id": (
                        "session_22d9202e-989b-459f-8ba3-2fafab56df19"
                    ),
                },
                now_ms=1_788_328_946_526,
            )

        record = json.loads(adapter.read(pane.pane_id).record)
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
        pane = PaneActivity(
            pane_id="%12",
            current_command="codex",
            pane_tty="/dev/ttys031",
            socket_path="/private/tmp/tmux/default",
            server_pid="30402",
        )
        activity, adapter = self.activity_for(
            pane,
            environment={"CODEX_HOME": "/tmp/codex"},
            process_identity=lambda _pane: process,
        )
        session = activity.report(
            pane.pane_id,
            "idle",
            {
                "hook_event_name": "SessionStart",
                "source": "startup",
                "session_id": "thread-root",
                "transcript_path": "/tmp/codex/sessions/root.jsonl",
            },
            now_ms=2_000,
        )
        session_record = adapter.read(pane.pane_id).record

        progress = activity.report(
            pane.pane_id,
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

        before = json.loads(session_record)
        after = json.loads(adapter.read(pane.pane_id).record)
        self.assertEqual(progress.state, "busy")
        self.assertEqual(after["root"], before["root"])
        self.assertEqual(after["generation"], before["generation"])

    def test_repeated_agy_status_line_and_pre_tool_use_skip_tmux_option_writes(self):
        process = {"pid": 7711, "started_at": "Mon Nov 23 09:53:36 2026"}
        runtime_state = "busy"
        pane = PaneActivity(
            pane_id="%6",
            current_command="agy",
            pane_tty="/dev/ttys016",
            socket_path="/private/tmp/tmux/default",
            server_pid="37160",
        )
        process_alive = True
        activity, adapter = self.activity_for(
            pane,
            process_identity=lambda _pane: process,
            process_matches=lambda recorded, _pane: process_alive and recorded == process,
        )

        with mock.patch(
            "agy_sessions.current_session",
            side_effect=lambda **_kw: {"state": runtime_state},
        ):
            first_busy = activity.report(
                pane.pane_id,
                "busy",
                {"session_id": "sid-1"},
                owner="agy",
                hook_event_name="StatusLine",
                now_ms=10_000,
            )
            self.assertTrue(first_busy.accepted)
            self.assertTrue(first_busy.changed)
            self.assertEqual(adapter.wake_count, 1)
            busy_snapshot = adapter.read(pane.pane_id)
            self.assertEqual(busy_snapshot.marker, "agy")
            self.assertEqual(busy_snapshot.marker_updated_at, "10000")

            adapter.writes.clear()
            repeated_busy = activity.report(
                pane.pane_id,
                "busy",
                {"session_id": "sid-1"},
                owner="agy",
                hook_event_name="StatusLine",
                now_ms=10_500,
            )
            self.assertTrue(repeated_busy.accepted)
            self.assertFalse(repeated_busy.changed)
            self.assertEqual(adapter.writes, [])
            self.assertEqual(adapter.wake_count, 1)
            self.assertEqual(adapter.read(pane.pane_id), busy_snapshot)

            pre_tool = activity.report(
                pane.pane_id,
                "busy",
                {"session_id": "sid-1"},
                owner="agy",
                hook_event_name="PreToolUse",
                now_ms=10_700,
            )
            self.assertTrue(pre_tool.accepted)
            self.assertFalse(pre_tool.changed)
            self.assertEqual(adapter.writes, [])
            self.assertEqual(adapter.wake_count, 1)

            # PreInvocation must still refresh and wake on an already-busy pane.
            pre_inv = activity.report(
                pane.pane_id,
                "busy",
                {"session_id": "sid-1"},
                owner="agy",
                hook_event_name="PreInvocation",
                now_ms=10_900,
            )
            self.assertTrue(pre_inv.accepted)
            self.assertTrue(pre_inv.changed)
            self.assertTrue(adapter.writes)
            self.assertEqual(adapter.wake_count, 2)

            # Transition busy -> idle via StatusLine must still write and wake.
            runtime_state = "idle"
            adapter.writes.clear()
            to_idle = activity.report(
                pane.pane_id,
                "idle",
                {"session_id": "sid-1"},
                owner="agy",
                hook_event_name="StatusLine",
                now_ms=11_000,
            )
            self.assertTrue(to_idle.accepted)
            self.assertTrue(to_idle.changed)
            self.assertEqual(adapter.wake_count, 3)
            idle_snapshot = adapter.read(pane.pane_id)
            self.assertEqual(idle_snapshot.marker, "")
            self.assertEqual(idle_snapshot.marker_updated_at, "")

            # Repeated idle StatusLine polls must perform zero option writes.
            adapter.writes.clear()
            repeated_idle = activity.report(
                pane.pane_id,
                "idle",
                {"session_id": "sid-1"},
                owner="agy",
                hook_event_name="StatusLine",
                now_ms=11_500,
            )
            self.assertTrue(repeated_idle.accepted)
            self.assertFalse(repeated_idle.changed)
            self.assertEqual(adapter.writes, [])
            self.assertEqual(adapter.wake_count, 3)
            self.assertEqual(adapter.read(pane.pane_id), idle_snapshot)

            # If the agy process restarted in the same pane, StatusLine must refresh.
            process = {"pid": 8822, "started_at": "Mon Nov 23 10:00:00 2026"}
            adapter.writes.clear()
            restarted = activity.report(
                pane.pane_id,
                "idle",
                {"session_id": "sid-1"},
                owner="agy",
                hook_event_name="StatusLine",
                now_ms=12_000,
            )
            self.assertTrue(restarted.accepted)
            self.assertTrue(restarted.changed)
            self.assertTrue(adapter.writes)
            self.assertEqual(
                json.loads(adapter.read(pane.pane_id).record)["process"],
                process,
            )

    def test_codex_same_turn_and_subagent_pre_tool_use_skip_option_writes(self):
        process = {"pid": 6322, "started_at": "process-1"}
        pane = PaneActivity(
            pane_id="%12",
            current_command="codex",
            pane_tty="/dev/ttys031",
            socket_path="/private/tmp/tmux/default",
            server_pid="30402",
        )
        activity, adapter = self.activity_for(
            pane,
            environment={"CODEX_HOME": "/tmp/codex"},
            process_identity=lambda _pane: process,
        )
        activity.report(
            pane.pane_id,
            "busy",
            {
                "hook_event_name": "UserPromptSubmit",
                "session_id": "thread-root",
                "turn_id": "turn-root",
                "transcript_path": "/tmp/codex/sessions/root.jsonl",
            },
            now_ms=2_000,
        )
        snapshot = adapter.read(pane.pane_id)
        wake_count = adapter.wake_count
        adapter.writes.clear()

        same_turn = activity.report(
            pane.pane_id,
            "busy",
            {
                "hook_event_name": "PreToolUse",
                "session_id": "thread-root",
                "turn_id": "turn-root",
                "transcript_path": "/tmp/codex/sessions/root.jsonl",
            },
            now_ms=2_100,
        )
        subagent = activity.report(
            pane.pane_id,
            "busy",
            {
                "hook_event_name": "PreToolUse",
                "session_id": "thread-child",
                "turn_id": "turn-child",
                "transcript_path": "/tmp/codex/sessions/child.jsonl",
                "agent_id": "researcher",
            },
            now_ms=2_200,
        )

        self.assertTrue(same_turn.accepted)
        self.assertFalse(same_turn.changed)
        self.assertTrue(subagent.accepted)
        self.assertFalse(subagent.changed)
        self.assertEqual(adapter.writes, [])
        self.assertEqual(adapter.wake_count, wake_count)
        self.assertEqual(adapter.read(pane.pane_id), snapshot)

    def test_codex_thread_identity_handles_suffixed_archived_and_zst_rollouts(self):
        import shutil
        from agent_activity import CodexThreadIdentity, codex_thread_identity
        import codex_sessions

        with tempfile.TemporaryDirectory() as raw_tmp:
            codex_home = Path(raw_tmp) / ".codex"
            root_sid = "11111111-1111-7111-8111-111111111111"
            root_rid = "22222222-2222-7222-8222-222222222222"
            child_sid = "33333333-3333-7333-8333-333333333333"
            child_rid = "44444444-4444-7444-8444-444444444444"
            zst_sid = "55555555-5555-7555-8555-555555555555"

            # Root session exists only as a suffixed rollout
            root_path = codex_home / "sessions/2026/11/23" / f"rollout-2026-11-23T10-00-00-{root_sid}_{root_rid}.jsonl"
            root_path.parent.mkdir(parents=True, exist_ok=True)
            root_path.write_text(
                json.dumps({"type": "session_meta", "payload": {"id": root_sid, "source": "cli"}}) + "\n",
                encoding="utf-8",
            )
            # Subagent session exists only as an archived suffixed rollout
            child_path = codex_home / "archived_sessions" / f"rollout-2026-11-23T10-05-00-{child_sid}_{child_rid}.jsonl"
            child_path.parent.mkdir(parents=True, exist_ok=True)
            child_path.write_text(
                json.dumps(
                    {
                        "type": "session_meta",
                        "payload": {
                            "id": child_sid,
                            "source": {"subagent": {"thread_spawn": {"parent_thread_id": root_sid}}},
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            self.assertEqual(codex_thread_identity(codex_home, root_sid), CodexThreadIdentity(root_sid, "main"))
            self.assertEqual(codex_thread_identity(codex_home, child_sid), CodexThreadIdentity(root_sid, "subagent"))

            if shutil.which("zstd"):
                zst_path = codex_home / "archived_sessions" / f"rollout-2026-11-23T10-06-00-{zst_sid}.jsonl.zst"
                raw = (
                    json.dumps(
                        {
                            "type": "session_meta",
                            "payload": {
                                "id": zst_sid,
                                "source": {"subagent": {"thread_spawn": {"parent_thread_id": root_sid}}},
                            },
                        }
                    )
                    + "\n"
                ).encode("utf-8")
                subprocess.run(
                    [codex_sessions.zstd_binary(), "-q", "-o", str(zst_path)],
                    input=raw,
                    check=True,
                )
                self.assertEqual(codex_thread_identity(codex_home, zst_sid), CodexThreadIdentity(root_sid, "subagent"))

            # SQLite-only rollout in future_threads/ is also resolved by codex_thread_identity
            from contextlib import closing
            sqlite_sid = "66666666-6666-7666-8666-666666666666"
            future_p = codex_home / "future_threads" / f"rollout-2026-11-23T10-07-00-{sqlite_sid}.jsonl"
            future_p.parent.mkdir(parents=True, exist_ok=True)
            future_p.write_text(json.dumps({"type": "session_meta", "payload": {"id": sqlite_sid, "source": "cli"}}) + "\n", encoding="utf-8")
            with closing(sqlite3.connect(codex_home / "state_5.sqlite")) as conn, conn:
                conn.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, rollout_path TEXT, updated_at INTEGER)")
                conn.execute("INSERT INTO threads VALUES (?, ?, ?)", (sqlite_sid, str(future_p), 1_795_400_000))
            self.assertEqual(codex_thread_identity(codex_home, sqlite_sid), CodexThreadIdentity(sqlite_sid, "main"))

    def test_r3_p1_legacy_codex_activity_respects_committed_sqlite_head_and_zstd(self) -> None:
        from contextlib import closing
        import shutil
        import sqlite3
        import codex_sessions

        sid = "88888888-8888-7888-8888-888888888888"
        rid = "99999999-9999-7999-8999-999999999999"
        with tempfile.TemporaryDirectory() as raw_tmp:
            home = Path(raw_tmp)
            codex_home = home / ".codex"
            sdir = codex_home / "sessions/2026/11/23"
            sdir.mkdir(parents=True, exist_ok=True)
            committed = sdir / f"rollout-2026-11-23T00-00-01-{sid}.jsonl"
            committed.write_text(
                json.dumps({"ordinal": 0, "type": "session_meta", "payload": {"id": sid, "cwd": str(home), "source": "cli"}}) + "\n"
                + json.dumps({"ordinal": 1, "type": "event_msg", "payload": {"type": "task_started", "turn_id": "turn-busy-1"}}) + "\n",
                encoding="utf-8",
            )
            with closing(sqlite3.connect(codex_home / "state_8.sqlite")) as conn, conn:
                conn.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, rollout_path TEXT, updated_at INTEGER)")
                conn.execute("INSERT INTO threads VALUES (?, ?, ?)", (sid, str(committed), 1_795_400_000))

            # Uncommitted header-only rollback segment must NOT flip committed busy pane to idle.
            uncommitted = sdir / f"rollout-2026-11-23T00-00-02-{sid}_{rid}.jsonl"
            uncommitted.write_text(
                json.dumps({"ordinal": 0, "type": "session_meta", "payload": {"id": sid, "cwd": str(home), "source": "cli"}}) + "\n",
                encoding="utf-8",
            )
            resolver, pane = self.legacy_resolver_and_pane(home, sid)
            view = resolver.inspect(pane.pane_id)
            self.assertEqual(view.state, "busy")
            self.assertEqual(view.evidence, "turn-busy-1")

            # Compressing & archiving the committed rollout must still resolve as busy, not unknown.
            if shutil.which("zstd"):
                arch = codex_home / "archived_sessions"
                arch.mkdir(parents=True, exist_ok=True)
                zst = arch / f"{committed.name}.zst"
                subprocess.run(
                    [codex_sessions.zstd_binary(), "-q", "-o", str(zst)],
                    input=committed.read_bytes(),
                    check=True,
                )
                committed.unlink()
                uncommitted.unlink()
                view_zst = resolver.inspect(pane.pane_id)
                self.assertEqual(view_zst.state, "busy")
                self.assertEqual(view_zst.evidence, "turn-busy-1")

    def test_r4_p1_modern_and_legacy_codex_activity_guard_committed_head_multi_db_and_pane_root(self) -> None:
        from contextlib import closing
        import shutil
        import sqlite3
        import codex_sessions

        sid = "89898989-1111-7898-8898-898989898989"
        foreign_sid = "90909090-2222-7909-8909-909090909090"
        rid_old = "aaaaaaaa-3333-7aaa-8aaa-aaaaaaaaaaaa"
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp).resolve()
            home = root / "home"
            codex_home = home / ".codex"
            sdir = codex_home / "sessions/2026/11/23"
            sdir.mkdir(parents=True, exist_ok=True)

            # Active committed rollout running a new turn
            committed = sdir / f"rollout-2026-11-23T00-00-01-{sid}.jsonl"
            committed.write_text(
                json.dumps({"ordinal": 0, "type": "session_meta", "payload": {"id": sid, "cwd": str(home), "source": "cli"}}) + "\n"
                + json.dumps({"ordinal": 1, "type": "event_msg", "payload": {"type": "task_started", "turn_id": "active-new-turn"}}) + "\n",
                encoding="utf-8",
            )
            with closing(sqlite3.connect(codex_home / "state_5.sqlite")) as conn, conn:
                conn.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, rollout_path TEXT, updated_at INTEGER)")
                conn.execute("INSERT INTO threads VALUES (?, ?, ?)", (sid, str(committed), 1_795_400_000))

            # 1. Structured hook pointed to an older rollout whose turn finished & was archived to .zst,
            #    while SQLite committed head is running `active-new-turn`. Must stay BUSY (never IDLE).
            past = sdir / f"rollout-2026-11-22T22-00-00-{sid}_{rid_old}.jsonl"
            past.write_text(
                json.dumps({"ordinal": 0, "type": "session_meta", "payload": {"id": sid, "cwd": str(home), "source": "cli"}}) + "\n"
                + json.dumps({"ordinal": 1, "type": "event_msg", "payload": {"type": "task_started", "turn_id": "previous-turn"}}) + "\n"
                + json.dumps({"ordinal": 2, "type": "event_msg", "payload": {"type": "task_complete", "turn_id": "previous-turn", "completed_at": 1_795_400_100_000}}) + "\n",
                encoding="utf-8",
            )
            if shutil.which("zstd"):
                zpast = Path(f"{past}.zst")
                subprocess.run([codex_sessions.zstd_binary(), "-q", "-o", str(zpast)], input=past.read_bytes(), check=True)
                past.unlink()
            pane_mod = PaneActivity(
                pane_id="%9",
                current_command="codex",
                pane_tty="/dev/ttys099",
                socket_path="/private/tmp/tmux-test",
                server_pid="999",
                pane_pid="9999",
            )
            mod_activity, _ = self.activity_for(
                pane_mod,
                environment={"HOME": str(home), "CODEX_HOME": str(codex_home)},
            )
            state, reason, ev_turn = mod_activity._resolve_codex(
                {
                    "session_id": sid,
                    "turn_id": "previous-turn",
                    "transcript_path": str(past),
                    "codex_home": str(codex_home),
                },
                1_795_400_000_000,
            )
            self.assertEqual(state, "busy")
            self.assertEqual(reason, "codex_rollout_turn_in_progress")
            self.assertEqual(ev_turn, "active-new-turn")

            # 2. Legacy multi-DB: higher state_10.sqlite points to foreign SID, missing file, or corrupt DB;
            #    must still recover valid busy head from state_5.sqlite instead of flipping idle.
            resolver, pane = self.legacy_resolver_and_pane(home, sid)
            foreign_file = codex_home / "future/opaque.jsonl"
            foreign_file.parent.mkdir(parents=True, exist_ok=True)
            foreign_file.write_text(
                json.dumps({"ordinal": 0, "type": "session_meta", "payload": {"id": foreign_sid}}) + "\n",
                encoding="utf-8",
            )
            db10 = codex_home / "state_10.sqlite"
            for bad_target in (foreign_file, foreign_file.parent / "absent.jsonl"):
                db10.unlink(missing_ok=True)
                with closing(sqlite3.connect(db10)) as conn, conn:
                    conn.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, rollout_path TEXT, updated_at INTEGER)")
                    conn.execute("INSERT INTO threads VALUES (?, ?, ?)", (sid, str(bad_target), 1_795_400_500))
                view = resolver.inspect(pane.pane_id)
                self.assertEqual(view.state, "busy", bad_target)
                self.assertEqual(view.evidence, "active-new-turn")
            db10.write_bytes(b"not a sqlite database")
            self.assertEqual(resolver.inspect(pane.pane_id).state, "busy")
            db10.unlink()

            # 3. Caller's CODEX_HOME has an idle copy of `sid`, while pane binding points to busy `codex_home`.
            caller_home = root / "reader profile"
            caller_idle = caller_home / "sessions/2026/11/23" / f"rollout-2026-11-23T00-00-01-{sid}.jsonl"
            caller_idle.parent.mkdir(parents=True, exist_ok=True)
            caller_idle.write_text(
                json.dumps({"ordinal": 0, "type": "session_meta", "payload": {"id": sid, "cwd": str(home), "source": "cli"}}) + "\n",
                encoding="utf-8",
            )
            with closing(sqlite3.connect(caller_home / "state_5.sqlite")) as conn, conn:
                conn.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, rollout_path TEXT, updated_at INTEGER)")
                conn.execute("INSERT INTO threads VALUES (?, ?, ?)", (sid, str(caller_idle), 1_795_400_000))
            reg_path = Path(resolver.environment["AIPANE_REGISTRY"])
            reg_rec = json.loads(reg_path.read_text(encoding="utf-8").strip())
            reg_rec["codex_home"] = str(codex_home)
            reg_path.write_text(json.dumps(reg_rec) + "\n", encoding="utf-8")
            resolver.environment["CODEX_HOME"] = str(caller_home)
            self.assertEqual(resolver.inspect(pane.pane_id).state, "busy")

    def test_r5_p1_higher_db_missing_header_and_no_db_active_idle_guard(self) -> None:
        from contextlib import closing
        import sqlite3

        sid = "89898989-4444-7898-8898-898989898989"
        foreign_sid = "90909090-5555-7909-8909-909090909090"
        rid_extra = "bbbbbbbb-6666-7bbb-8bbb-bbbbbbbbbbbb"
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp).resolve()
            home = root / "home"
            codex_home = home / ".codex"
            sdir = codex_home / "sessions/2026/11/23"
            sdir.mkdir(parents=True, exist_ok=True)

            # 1. Higher state_10.sqlite points to same-SID filename whose line 1 is token_count
            #    (missing_meta, line 2 foreign SID); lower state_5.sqlite points to busy head -> BUSY.
            busy_p = sdir / f"rollout-2026-11-23T00-00-01-{sid}.jsonl"
            busy_p.write_text(
                json.dumps({"ordinal": 0, "type": "session_meta", "payload": {"id": sid, "cwd": str(home), "source": "cli"}}) + "\n"
                + json.dumps({"ordinal": 1, "type": "event_msg", "payload": {"type": "task_started", "turn_id": "turn-live"}}) + "\n",
                encoding="utf-8",
            )
            bad_p = sdir / f"rollout-2026-11-23T01-00-00-{sid}_{rid_extra}.jsonl"
            bad_p.write_text(
                json.dumps({"type": "event_msg", "payload": {"type": "token_count"}}) + "\n"
                + json.dumps({"type": "session_meta", "payload": {"id": foreign_sid}}) + "\n",
                encoding="utf-8",
            )
            with closing(sqlite3.connect(codex_home / "state_5.sqlite")) as conn, conn:
                conn.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, rollout_path TEXT, updated_at INTEGER)")
                conn.execute("INSERT INTO threads VALUES (?, ?, ?)", (sid, str(busy_p), 1_795_400_000))
            with closing(sqlite3.connect(codex_home / "state_10.sqlite")) as conn, conn:
                conn.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, rollout_path TEXT, updated_at INTEGER)")
                conn.execute("INSERT INTO threads VALUES (?, ?, ?)", (sid, str(bad_p), 1_795_400_500))
            resolver, pane = self.legacy_resolver_and_pane(home, sid)
            view = resolver.inspect(pane.pane_id)
            self.assertEqual(view.state, "busy")
            self.assertEqual(view.evidence, "turn-live")

            # 2. Structured hook completed on active transcript with NO SQLite DB; unrelated newer
            #    sibling carrying open task_started must NOT override verified idle state.
            (codex_home / "state_5.sqlite").unlink()
            (codex_home / "state_10.sqlite").unlink()
            done_p = busy_p
            done_p.write_text(
                json.dumps({"ordinal": 0, "type": "session_meta", "payload": {"id": sid, "cwd": str(home), "source": "cli"}}) + "\n"
                + json.dumps({"ordinal": 1, "type": "event_msg", "payload": {"type": "task_started", "turn_id": "turn-done"}}) + "\n"
                + json.dumps({"ordinal": 2, "type": "event_msg", "payload": {"type": "task_complete", "turn_id": "turn-done", "completed_at": 1_795_400_200_000}}) + "\n",
                encoding="utf-8",
            )
            bad_p.write_text(
                json.dumps({"ordinal": 0, "type": "session_meta", "payload": {"id": sid, "cwd": str(home), "source": "cli"}}) + "\n"
                + json.dumps({"ordinal": 1, "type": "event_msg", "payload": {"type": "task_started", "turn_id": "stale-uncommitted"}}) + "\n",
                encoding="utf-8",
            )
            mod_activity, _ = self.activity_for(
                PaneActivity(pane_id="%9", current_command="codex", pane_tty="/dev/ttys099", socket_path="/private/tmp/tmux-test", server_pid="999", pane_pid="9999"),
                environment={"HOME": str(home), "CODEX_HOME": str(codex_home)},
            )
            st, reason, _ = mod_activity._resolve_codex(
                {"session_id": sid, "turn_id": "turn-done", "transcript_path": str(done_p), "codex_home": str(codex_home)},
                1_795_400_000_000,
            )
            self.assertEqual(st, "idle")
            self.assertEqual(reason, "codex_rollout_turn_completed")

    def test_r6_p1_locked_sqlite_foreign_zst_plain_twin_and_relative_hook_root(self) -> None:
        from contextlib import closing
        import shutil
        import sqlite3
        import subprocess
        from unittest import mock

        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp).resolve()
            home = root / "home"
            codex_home = home / ".codex"
            day = codex_home / "sessions/2026/11/23"
            day.mkdir(parents=True, exist_ok=True)
            sid = "71717171-7171-4717-8717-717171717171"
            foreign_sid = "82828282-8282-4828-8828-828282828282"
            rid2 = "93939393-9393-4939-8939-939393939393"

            done_p = day / f"rollout-2026-11-23T00-00-01-{sid}.jsonl"
            done_p.write_text(
                json.dumps({"ordinal": 0, "type": "session_meta", "payload": {"id": sid, "cwd": str(home), "source": "cli"}}) + "\n"
                + json.dumps({"ordinal": 1, "type": "event_msg", "payload": {"type": "task_started", "turn_id": "t-done"}}) + "\n"
                + json.dumps({"ordinal": 2, "type": "event_msg", "payload": {"type": "task_complete", "turn_id": "t-done", "completed_at": 1_795_400_200_000}}) + "\n",
                encoding="utf-8",
            )
            busy_q = day / f"rollout-2026-11-23T00-00-02-{sid}_{rid2}.jsonl"
            busy_q.write_text(
                json.dumps({"ordinal": 0, "type": "session_meta", "payload": {"id": sid, "cwd": str(home), "source": "cli"}}) + "\n"
                + json.dumps({"ordinal": 1, "type": "event_msg", "payload": {"type": "task_started", "turn_id": "t-busy"}}) + "\n",
                encoding="utf-8",
            )
            db8 = codex_home / "state_8.sqlite"
            with closing(sqlite3.connect(db8)) as conn, conn:
                conn.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, rollout_path TEXT, updated_at INTEGER)")
                conn.execute("INSERT INTO threads VALUES (?, ?, ?)", (sid, str(busy_q), 1_795_400_100))

            mod_activity, _ = self.activity_for(
                PaneActivity(pane_id="%9", current_command="codex", pane_tty="/dev/ttys099", socket_path="/private/tmp/tmux-test", server_pid="999", pane_pid="9999"),
                environment={"HOME": str(home), "CODEX_HOME": str(codex_home)},
            )

            # 1. Exclusive SQLite lock must fail closed (`unknown`), never silently allow `idle`.
            locker = sqlite3.connect(db8)
            try:
                locker.execute("BEGIN EXCLUSIVE")
                st_lock, reason_lock, _ = mod_activity._resolve_codex(
                    {"session_id": sid, "turn_id": "t-done", "transcript_path": str(done_p), "codex_home": str(codex_home)},
                    1_795_400_000_000,
                )
                self.assertEqual(st_lock, "unknown")
                self.assertEqual(reason_lock, "codex_sqlite_conflict")
            finally:
                locker.rollback()
                locker.close()

            # 2. Higher DB pointing to foreign-SID .jsonl.zst with completed same-SID plain twin
            #    must NOT bypass compressed target verification; falls back to lower busy DB.
            if shutil.which("zstd"):
                foreign_zst = done_p.with_name(done_p.name + ".zst")
                foreign_bytes = done_p.read_bytes().replace(sid.encode(), foreign_sid.encode())
                foreign_zst.write_bytes(subprocess.check_output(["zstd", "-cq"], input=foreign_bytes))
                db10 = codex_home / "state_10.sqlite"
                with closing(sqlite3.connect(db10)) as conn, conn:
                    conn.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, rollout_path TEXT, updated_at INTEGER)")
                    conn.execute("INSERT INTO threads VALUES (?, ?, ?)", (sid, str(foreign_zst), 1_795_400_200))
                st_twin, reason_twin, _ = mod_activity._resolve_codex(
                    {"session_id": sid, "turn_id": "t-done", "transcript_path": str(done_p), "codex_home": str(codex_home)},
                    1_795_400_000_000,
                )
                self.assertEqual(st_twin, "busy")
                self.assertEqual(reason_twin, "codex_rollout_turn_in_progress")

            # 3. Relative CODEX_HOME reported by a hook running in another cwd resolves via target process cwd / transcript.
            ws = root / "workspace"
            target_profile = ws / "profile"
            target_log = target_profile / "sessions/2026/11/23" / f"rollout-2026-11-23T00-00-00-{sid}.jsonl"
            target_log.parent.mkdir(parents=True, exist_ok=True)
            target_log.write_text(done_p.read_text(encoding="utf-8"), encoding="utf-8")
            pane_rel = PaneActivity(
                pane_id="%12",
                current_command="codex",
                pane_tty="/dev/ttys012",
                socket_path="/private/tmp/tmux-test",
                server_pid="999",
                pane_pid="42424",
            )
            act_rel, adapter_rel = self.activity_for(
                pane_rel,
                environment={"HOME": str(home), "CODEX_HOME": "profile"},
            )
            prev_cwd = os.getcwd()
            os.chdir(root)
            try:
                with mock.patch("codex_sessions.process_cwd", return_value=str(ws)):
                    rep = act_rel.report(
                        pane_rel.pane_id,
                        "busy",
                        owner="codex",
                        payload={"session_id": sid, "turn_id": "t-1", "transcript_path": str(target_log)},
                    )
            finally:
                os.chdir(prev_cwd)
            self.assertTrue(rep.accepted)
            saved_rec = json.loads(adapter_rel.read(pane_rel.pane_id).record)
            self.assertEqual(saved_rec["root"]["codex_home"], str(target_profile.resolve()))

    def test_process_snapshot_parses_numeric_tdev_and_single_digit_day_lstart(self):
        import stat as stat_mod
        from agent_activity import parse_process_snapshot

        output = "\n".join(
            (
                "  100     1 Fri Aug  4 19:00:41 2026    16/75 S+   /usr/local/bin/agy --mode auto",
                "  200     1 Tue Nov 24 01:03:36 2026    16/76 S    codex --yolo",
                "  300     1 Tue Nov 24 01:03:36 2026       ?? Ss   /sbin/launchd",
                "  400   200 Tue Nov 24 01:04:00 2026    16/76 Z    (defunct)",
            )
        )
        fake_stats = {
            "/dev/ttys075": mock.Mock(
                st_mode=stat_mod.S_IFCHR | 0o620,
                st_rdev=os.makedev(16, 75),
            ),
            "/dev/ttys076": mock.Mock(
                st_mode=stat_mod.S_IFCHR | 0o620,
                st_rdev=os.makedev(16, 76),
            ),
            "/dev/notatty": mock.Mock(
                st_mode=stat_mod.S_IFREG | 0o644,
                st_rdev=os.makedev(16, 75),
            ),
        }

        def fake_stat(path):
            if path not in fake_stats:
                raise FileNotFoundError(path)
            return fake_stats[path]

        snap = parse_process_snapshot(output, stat_fn=fake_stat)
        pane_75 = PaneActivity(pane_id="%1", current_command="agy", pane_tty="/dev/ttys075")
        pane_76 = PaneActivity(pane_id="%2", current_command="codex", pane_tty="/dev/ttys076")
        pane_bad = PaneActivity(pane_id="%3", current_command="agy", pane_tty="/dev/notatty")

        # Double-space single-digit day preserved verbatim for exact 24-char match.
        self.assertTrue(
            snap.matches_process(
                {"pid": 100, "started_at": "Fri Aug  4 19:00:41 2026"},
                pane_75,
            )
        )
        self.assertFalse(
            snap.matches_process(
                {"pid": 100, "started_at": "Fri Aug 4 19:00:41 2026"},
                pane_75,
            )
        )
        self.assertFalse(
            snap.matches_process(
                {"pid": 100, "started_at": "Fri Aug  4 19:00:41 2026"},
                pane_76,
            )
        )
        self.assertFalse(
            snap.matches_process(
                {"pid": 100, "started_at": "Fri Aug  4 19:00:41 2026"},
                pane_bad,
            )
        )

        agy_info = snap.agy_process_info(100, "/dev/ttys075")
        self.assertIsNotNone(agy_info)
        self.assertEqual(agy_info["executable"], "agy")
        self.assertEqual(agy_info["tty"], "ttys075")
        self.assertTrue(agy_info["foreground"])

        # Mismatched tty must not match pane_tty.
        agy_wrong_tty = snap.agy_process_info(100, "/dev/ttys076")
        self.assertIsNotNone(agy_wrong_tty)
        self.assertNotEqual(agy_wrong_tty["tty"], "ttys076")

        # No-tty process maps to empty tty_for_pid; zombies excluded from presence_table.
        self.assertEqual(snap.tty_for_pid(300, "/dev/ttys075"), "")
        self.assertEqual(snap.tty_for_pid(200, "/dev/ttys076"), "ttys076")
        presence = snap.presence_table()
        self.assertIn(100, presence)
        self.assertIn(200, presence)
        self.assertNotIn(400, presence)

    def test_reconcile_shares_single_snapshot_and_revalidates_live_before_repair(self):
        import stat as stat_mod
        from datetime import datetime
        from agent_activity import parse_process_snapshot

        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            codex_home = root / ".codex"
            agy_state = root / "aipane-state"
            sid = "019d0000-0000-7000-8000-000000000001"
            rollout = make_codex_history(
                codex_home,
                session_id=sid,
                lifecycles=[("turn-1", "completed", 1_795_400_000, 1_795_400_005)],
            )

            started_at = "Tue Nov 24 01:03:36 2026"
            started_ts = datetime.strptime(started_at, "%a %b %d %H:%M:%S %Y").timestamp()
            codex_record = json.dumps(
                {
                    "version": 1,
                    "revision": "rev-1",
                    "owner": "codex",
                    "reported": "busy",
                    "updated_at": 1_795_400_000_000,
                    "pane": {
                        "id": "%1",
                        "tty": "/dev/ttys075",
                        "socket": "/tmp/tmux-test",
                        "server_pid": "999",
                    },
                    "process": {"pid": 100, "started_at": started_at},
                    "root": {
                        "session_id": sid,
                        "turn_id": "turn-1",
                        "transcript_path": str(rollout),
                        "codex_home": str(codex_home),
                    },
                }
            )
            pane_codex = PaneActivity(
                pane_id="%1",
                current_command="codex",
                pane_tty="/dev/ttys075",
                socket_path="/tmp/tmux-test",
                server_pid="999",
                pane_pid="100",
                marker="codex",
                reporter="codex",
                marker_updated_at="1795400000000",
                record=codex_record,
            )
            pane_agy = PaneActivity(
                pane_id="%2",
                current_command="agy",
                pane_tty="/dev/ttys076",
                socket_path="/tmp/tmux-test",
                server_pid="999",
                pane_pid="200",
                reporter="agy",
                record="{}",
            )
            env = {
                "CODEX_HOME": str(codex_home),
                "AIPANE_STATE_DIR": str(agy_state),
                "HOME": str(root),
            }
            import agy_sessions
            rt_path = agy_sessions.runtime_path("%2", "/tmp/tmux-test", "999", env)
            rt_path.parent.mkdir(parents=True, exist_ok=True)
            rt_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "pid": 200,
                        "started_at": started_ts,
                        "tty": "ttys076",
                        "pane": "%2",
                        "socket": "/tmp/tmux-test",
                        "server": "999",
                        "state": "idle",
                        "cwd": str(root),
                        "session_id": sid,
                    }
                ),
                encoding="utf-8",
            )
            os.chmod(rt_path, 0o600)

            ps_output = "\n".join(
                (
                    f"  100     1 {started_at}    16/75 S+   codex --yolo",
                    f"  200     1 {started_at}    16/76 S+   agy --mode auto",
                )
            )
            fake_stats = {
                "/dev/ttys075": mock.Mock(
                    st_mode=stat_mod.S_IFCHR | 0o620,
                    st_rdev=os.makedev(16, 75),
                ),
                "/dev/ttys076": mock.Mock(
                    st_mode=stat_mod.S_IFCHR | 0o620,
                    st_rdev=os.makedev(16, 76),
                ),
            }
            snapshot = parse_process_snapshot(ps_output, stat_fn=fake_stats.__getitem__)
            adapter = InMemoryActivityAdapter([pane_codex, pane_agy], environment=env)
            activity = AgentActivity(adapter=adapter, environment=env)

            live_checks = []
            live_matches_result = [False]

            def fake_live_ps(arguments, **_kwargs):
                live_checks.append(arguments)
                lstart = started_at if live_matches_result[0] else "Tue Nov 24 02:00:00 2026"
                return subprocess.CompletedProcess(
                    arguments, 0, f"{lstart} ttys075\n", "",
                )

            with (
                mock.patch("agent_activity.capture_process_snapshot", return_value=snapshot) as cap_mock,
                mock.patch("agent_activity.os.kill"),
                mock.patch("agent_activity.subprocess.run", side_effect=fake_live_ps),
                mock.patch("agy_sessions.process_info", side_effect=AssertionError("must use shared snapshot for agy scan")),
            ):
                # Round 1: read-only scan uses shared snapshot once; zero live ps calls.
                first = activity.reconcile()
                self.assertEqual(cap_mock.call_count, 1)
                self.assertEqual(live_checks, [])
                self.assertTrue(first.busy)
                self.assertFalse(first.changed)

                # Round 2: triggers _repair(), which MUST run live identity check under lock.
                # Because live_matches_result is False (PID reused), repair aborts safely.
                second = activity.reconcile()
                self.assertEqual(cap_mock.call_count, 2)
                self.assertEqual(len(live_checks), 1)
                self.assertFalse(second.changed)
                self.assertEqual(adapter.read("%1").marker, "codex")

                # Round 3: live process now matches -> _repair() succeeds and clears marker.
                live_matches_result[0] = True
                third = activity.reconcile()
                self.assertEqual(len(live_checks), 2)
                self.assertTrue(third.changed)
                self.assertFalse(third.busy)
                self.assertEqual(adapter.read("%1").marker, "")

    @unittest.skipUnless(sys.platform == "darwin", "macOS libproc only")
    def test_darwin_libproc_snapshot_runs_without_ps_fork_and_scopes_presence(self):
        from agent_activity import capture_process_snapshot

        with mock.patch(
            "agent_activity.subprocess.run",
            side_effect=AssertionError("macOS capture_process_snapshot must not fork ps"),
        ):
            snap = capture_process_snapshot()

        self.assertIsNotNone(snap)
        me = snap.entries.get(os.getpid())
        self.assertIsNotNone(me)
        self.assertEqual(me.ppid, os.getppid())
        self.assertEqual(len(me.lstart), 24)
        self.assertTrue(me.command)

        resolved_pids = []
        orig_resolver = snap._command_resolver

        def spy_resolver(pid):
            resolved_pids.append(pid)
            return orig_resolver(pid)

        snap._command_resolver = spy_resolver
        scoped = snap.presence_table(root_pids=[os.getpid()])
        self.assertIn(os.getpid(), scoped)
        # Only this process (and any direct descendants) should be resolved via sysctl.
        self.assertLessEqual(len(resolved_pids), 10)
        self.assertLess(len(scoped), len(snap.entries))

    def test_reconcile_caches_unchanged_idle_codex_and_agy_panes_by_stat(self):
        import stat as stat_mod
        from datetime import datetime
        from agent_activity import parse_process_snapshot

        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            codex_home = root / ".codex"
            agy_state = root / "aipane-state"
            sid = "019d0000-0000-7000-8000-000000000002"
            rollout = make_codex_history(
                codex_home,
                session_id=sid,
                lifecycles=[("turn-1", "completed", 1_795_400_000, 1_795_400_005)],
            )
            started_at = "Tue Nov 24 01:03:36 2026"
            started_ts = datetime.strptime(started_at, "%a %b %d %H:%M:%S %Y").timestamp()
            codex_record = json.dumps(
                {
                    "version": 1,
                    "revision": "rev-idle",
                    "owner": "codex",
                    "reported": "idle",
                    "updated_at": 1_795_400_000_000,
                    "pane": {
                        "id": "%1",
                        "tty": "/dev/ttys075",
                        "socket": "/tmp/tmux-test",
                        "server_pid": "999",
                    },
                    "process": {"pid": 100, "started_at": started_at},
                    "root": {
                        "session_id": sid,
                        "turn_id": "turn-1",
                        "transcript_path": str(rollout),
                        "codex_home": str(codex_home),
                    },
                }
            )
            pane_codex = PaneActivity(
                pane_id="%1",
                current_command="codex",
                pane_tty="/dev/ttys075",
                socket_path="/tmp/tmux-test",
                server_pid="999",
                pane_pid="100",
                marker="",
                reporter="codex",
                record=codex_record,
            )
            pane_agy = PaneActivity(
                pane_id="%2",
                current_command="agy",
                pane_tty="/dev/ttys076",
                socket_path="/tmp/tmux-test",
                server_pid="999",
                pane_pid="200",
                reporter="agy",
                record="{}",
            )
            env = {
                "CODEX_HOME": str(codex_home),
                "AIPANE_STATE_DIR": str(agy_state),
                "HOME": str(root),
            }
            import agy_sessions
            rt_path = agy_sessions.runtime_path("%2", "/tmp/tmux-test", "999", env)
            rt_path.parent.mkdir(parents=True, exist_ok=True)
            rt_payload = {
                "version": 1,
                "pid": 200,
                "started_at": started_ts,
                "tty": "ttys076",
                "pane": "%2",
                "socket": "/tmp/tmux-test",
                "server": "999",
                "state": "idle",
                "cwd": str(root),
                "session_id": sid,
            }
            rt_path.write_text(json.dumps(rt_payload), encoding="utf-8")
            os.chmod(rt_path, 0o600)

            ps_output = "\n".join(
                (
                    f"  100     1 {started_at}    16/75 S+   codex --yolo",
                    f"  200     1 {started_at}    16/76 S+   agy --mode auto",
                )
            )
            fake_stats = {
                "/dev/ttys075": mock.Mock(
                    st_mode=stat_mod.S_IFCHR | 0o620,
                    st_rdev=os.makedev(16, 75),
                ),
                "/dev/ttys076": mock.Mock(
                    st_mode=stat_mod.S_IFCHR | 0o620,
                    st_rdev=os.makedev(16, 76),
                ),
            }
            snapshot = parse_process_snapshot(ps_output, stat_fn=fake_stats.__getitem__)
            adapter = InMemoryActivityAdapter([pane_codex, pane_agy], environment=env)
            activity = AgentActivity(adapter=adapter, environment=env)

            with (
                mock.patch("agent_activity.capture_process_snapshot", return_value=snapshot),
                mock.patch.object(activity, "_resolve", wraps=activity._resolve) as resolve_spy,
            ):
                r1 = activity.reconcile()
                self.assertFalse(r1.busy)
                self.assertEqual(resolve_spy.call_count, 2)

                # Round 2: unchanged stat fingerprints -> both idle panes hit cache (0 _resolve calls).
                r2 = activity.reconcile()
                self.assertFalse(r2.busy)
                self.assertEqual(resolve_spy.call_count, 2)

                # Mutate agy runtime file to busy -> immediate cache miss and busy=True!
                rt_payload["state"] = "busy"
                rt_path.write_text(json.dumps(rt_payload), encoding="utf-8")
                r3 = activity.reconcile()
                self.assertTrue(r3.busy)
                self.assertEqual(resolve_spy.call_count, 3)


if __name__ == "__main__":
    unittest.main()
