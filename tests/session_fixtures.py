"""Shared realistic session fixtures for cross-module session tests."""

from __future__ import annotations

from contextlib import closing
import json
import os
import sqlite3
from pathlib import Path
from typing import Any


def write_jsonl(path: Path, records: list[dict[str, Any]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return path


def seed_codex_multi_segment(
    home: Path,
    sid: str,
    *,
    rollout_ids: tuple[str, ...] = ("32323232-3232-7232-8232-323232323232",),
    now: float | None = None,
    include_sqlite: bool = False,
    untracked_sqlite_rollout: Path | None = None,
) -> list[Path]:
    """Create a realistic multi-segment Codex conversation with optional state_5.sqlite."""
    codex_home = home / ".codex"
    paths: list[Path] = []

    seg1 = codex_home / "sessions/2026/11/10" / f"rollout-2026-11-10T10-00-00-{sid}.jsonl"
    write_jsonl(
        seg1,
        [
            {"ordinal": 0, "type": "session_meta", "timestamp": "2026-11-10T02:00:00Z", "payload": {"id": sid}},
            {
                "ordinal": 1,
                "type": "response_item",
                "timestamp": "2026-11-10T02:00:01Z",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "Opening turn"}],
                },
            },
        ],
    )
    paths.append(seg1)

    for idx, rid in enumerate(rollout_ids, start=2):
        day = 10 + idx * 3
        seg = (
            codex_home
            / f"sessions/2026/11/{day:02d}"
            / f"rollout-2026-11-{day:02d}T12-00-00-{sid}_{rid}.jsonl"
        )
        base_ord = idx * 10
        write_jsonl(
            seg,
            [
                {
                    "ordinal": base_ord,
                    "type": "session_meta",
                    "timestamp": f"2026-11-{day:02d}T04:00:00Z",
                    "payload": {"id": sid},
                },
                {
                    "ordinal": base_ord + 1,
                    "type": "response_item",
                    "timestamp": f"2026-11-{day:02d}T04:00:01Z",
                    "payload": {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": f"Segment {idx} reply"}],
                    },
                },
            ],
        )
        paths.append(seg)

    if now is not None:
        for i, p in enumerate(paths):
            age = max(0, (len(paths) - 1 - i) * 3)
            ts = now - age * 86400
            os.utime(p, (ts, ts))

    if include_sqlite or untracked_sqlite_rollout is not None:
        latest = untracked_sqlite_rollout or paths[-1]
        db_path = codex_home / "state_5.sqlite"
        updated_at = int(now) if now is not None else int(latest.stat().st_mtime)
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS threads (id TEXT PRIMARY KEY, rollout_path TEXT, updated_at INTEGER)"
            )
            conn.execute(
                "INSERT OR REPLACE INTO threads VALUES (?, ?, ?)",
                (sid, str(latest), updated_at),
            )
    return paths
