"""Read-only evidence for local Codex conversations and their loaded state."""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import subprocess
import time
from contextlib import closing
from pathlib import Path
from typing import Any

CODEX_SESSION_ROOT_NAMES = ("sessions", "archived_sessions")
ROLLOUT_META_MAX_BYTES = 524288
_STATE_DB_RE = re.compile(r"^state_(\d+)\.sqlite$")
UUID_RE = re.compile(
    r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})"
)


def codex_session_roots(codex_home: Path, *, existing_only: bool = False) -> list[Path]:
    roots = [codex_home / name for name in CODEX_SESSION_ROOT_NAMES]
    if existing_only:
        return [root for root in roots if root.is_dir()]
    return roots


def extract_parent_session_id(payload: dict[str, Any]) -> str | None:
    own_id = payload.get("id")
    for key in ("parent_thread_id", "forked_from_id", "parent_session_id"):
        val = payload.get(key)
        if isinstance(val, str) and val and val != own_id:
            return val
    source = payload.get("source")
    if isinstance(source, dict):
        subagent = source.get("subagent")
        if isinstance(subagent, dict):
            thread_spawn = subagent.get("thread_spawn")
            if isinstance(thread_spawn, dict):
                val = thread_spawn.get("parent_thread_id")
                if isinstance(val, str) and val and val != own_id:
                    return val
            val = subagent.get("parent_thread_id")
            if isinstance(val, str) and val and val != own_id:
                return val
    return None


def _infer_codex_home(path: Path) -> str:
    directory = next(
        (ancestor for ancestor in path.parents if ancestor.name in CODEX_SESSION_ROOT_NAMES),
        None,
    )
    return str(directory.parent) if directory is not None else ""


def read_rollout_meta(
    path: Path,
    *,
    max_lines: int = 1,
    max_bytes: int = ROLLOUT_META_MAX_BYTES,
) -> dict[str, Any] | None:
    try:
        with path.open(encoding="utf-8", errors="replace") as rollout:
            for _ in range(max(1, max_lines)):
                raw = rollout.readline(max_bytes)
                if not raw:
                    break
                if len(raw) == max_bytes and not raw.endswith("\n"):
                    # Line exceeded safety cap; skip to avoid parsing truncated JSON.
                    return None
                metadata = json.loads(raw)
                if not isinstance(metadata, dict):
                    continue
                if metadata.get("type") != "session_meta":
                    continue
                payload = metadata.get("payload")
                if not isinstance(payload, dict):
                    return None
                sid = payload.get("id")
                if not isinstance(sid, str) or not sid:
                    return None
                ordinal = metadata.get("ordinal")
                if not isinstance(ordinal, int) or isinstance(ordinal, bool):
                    ordinal = None
                ts = payload.get("timestamp") or metadata.get("timestamp") or ""
                return {
                    "sid": sid,
                    "cwd": str(payload.get("cwd") or ""),
                    "timestamp": str(ts) if isinstance(ts, str) else "",
                    "ordinal": ordinal,
                    "parent_id": extract_parent_session_id(payload),
                    "codex_home": _infer_codex_home(path),
                    "rollout": str(path),
                }
        return None
    except (OSError, ValueError):
        return None


def rollout_matches_session(path: Path, sid: str) -> bool:
    meta = read_rollout_meta(path, max_lines=1)
    return bool(meta and meta["sid"] == sid)


def sort_rollout_entries(entries: list[tuple[dict[str, Any] | None, Path]]) -> list[tuple[dict[str, Any] | None, Path]]:
    """Order rollout segments for the same session from earliest to latest.

    Uses `ordinal` when every segment provides it; otherwise falls back to
    session_meta timestamp and path order so mixed legacy/new rollouts never
    sort legacy (ordinal=None) segments after newer ones.
    """
    all_have_ordinal = bool(entries) and all(
        meta is not None and isinstance(meta.get("ordinal"), int)
        for meta, _ in entries
    )
    if all_have_ordinal:
        return sorted(
            entries,
            key=lambda item: (
                int(item[0]["ordinal"]),  # type: ignore[index]
                str(item[0].get("timestamp") or ""),  # type: ignore[union-attr]
                str(item[1]),
            ),
        )
    return sorted(
        entries,
        key=lambda item: (
            "" if (item[0] and item[0].get("timestamp")) else "~",
            str((item[0] or {}).get("timestamp") or ""),
            str(item[1]),
        ),
    )


def find_session_rollouts(
    sid: str,
    codex_home: Path,
    *,
    verify_header: bool = True,
) -> list[Path]:
    """Return all durable rollouts belonging to `sid`, ordered earliest to latest.

    When `verify_header` is True (default), each rollout must contain a valid
    `session_meta` header matching `sid`. When False, headerless files matching
    the filename pattern are still accepted, while files whose header declares
    a different session ID are always rejected.
    """
    if not sid:
        return []
    seen: set[Path] = set()
    matched: list[tuple[dict[str, Any] | None, Path]] = []
    for root in codex_session_roots(codex_home, existing_only=True):
        for pattern in (f"*-{sid}.jsonl", f"*-{sid}_*.jsonl"):
            for path in root.rglob(pattern):
                if not path.is_file():
                    continue
                try:
                    resolved = path.resolve()
                except OSError:
                    continue
                if resolved in seen:
                    continue
                meta = read_rollout_meta(path, max_lines=1)
                if verify_header:
                    if not meta or meta["sid"] != sid:
                        continue
                elif meta is not None and meta["sid"] != sid:
                    continue
                seen.add(resolved)
                matched.append((meta, path))
    return [path for _, path in sort_rollout_entries(matched)]


def _latest_state_db(codex_home: Path) -> Path | None:
    best: tuple[int, Path] | None = None
    try:
        for candidate in codex_home.glob("state_*.sqlite"):
            if not candidate.is_file():
                continue
            m = _STATE_DB_RE.match(candidate.name)
            if not m:
                continue
            ver = int(m.group(1))
            if best is None or ver > best[0]:
                best = (ver, candidate)
    except OSError:
        return None
    return best[1] if best is not None else None


def query_sqlite_rollout_path(sid: str, codex_home: Path) -> Path | None:
    """Read-only lookup of Codex's recorded latest rollout_path for `sid`."""
    if not sid:
        return None
    db_path = _latest_state_db(codex_home)
    if db_path is None:
        return None
    try:
        uri = f"{db_path.resolve().as_uri()}?mode=ro"
        with closing(sqlite3.connect(uri, uri=True, timeout=0.15)) as conn:
            row = conn.execute(
                "SELECT rollout_path FROM threads WHERE id = ? LIMIT 1",
                (sid,),
            ).fetchone()
            if row and isinstance(row[0], str) and row[0].strip():
                return Path(row[0].strip()).expanduser()
    except (OSError, sqlite3.Error, ValueError):
        return None
    return None


def query_recent_sqlite_rollouts(
    codex_home: Path,
    *,
    days: int = 14,
    now: float | None = None,
) -> list[tuple[str, Path]]:
    """Read-only list of recent (thread_id, rollout_path) pairs from state_*.sqlite."""
    db_path = _latest_state_db(codex_home)
    if db_path is None:
        return []
    cutoff_sec = int((now if now is not None else time.time()) - max(0, days) * 86400)
    out: list[tuple[str, Path]] = []
    try:
        uri = f"{db_path.resolve().as_uri()}?mode=ro"
        with closing(sqlite3.connect(uri, uri=True, timeout=0.25)) as conn:
            if days > 0:
                rows = conn.execute(
                    "SELECT id, rollout_path FROM threads WHERE updated_at >= ? AND rollout_path IS NOT NULL AND rollout_path != ''",
                    (cutoff_sec,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT id, rollout_path FROM threads WHERE rollout_path IS NOT NULL AND rollout_path != ''",
                ).fetchall()
            for tid, rpath in rows:
                if isinstance(tid, str) and tid and isinstance(rpath, str) and rpath.strip():
                    out.append((tid, Path(rpath.strip()).expanduser()))
    except (OSError, sqlite3.Error, ValueError):
        return []
    return out


def session_exists(sid: str, codex_home: Path) -> bool:
    """Find a durable rollout, including files with an extra rollout ID.

    A filename can end in either <session-id>.jsonl or
    <session-id>_<rollout-id>.jsonl. The header identifies the conversation;
    the extra filename ID must never become the resume ID.
    """
    if not sid:
        return False
    for root in codex_session_roots(codex_home, existing_only=True):
        for pattern in (f"*-{sid}.jsonl", f"*-{sid}_*.jsonl"):
            for path in root.rglob(pattern):
                if rollout_matches_session(path, sid):
                    return True
    return False


def _open_sessions(arguments: list[str], codex_home: Path | None) -> dict[int, list[dict]]:
    try:
        result = subprocess.run(["lsof", "-nP", *arguments, "-F", "pfan"],
                                capture_output=True, text=True, timeout=3, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return {}
    # lsof returns 1 when one requested name has no matches, even if another
    # name produced valid descriptors. Parse the descriptors it did return.
    if not result.stdout or result.returncode not in (0, 1):
        return {}
    roots = [(codex_home / name).resolve() for name in ("sessions", "archived_sessions")] if codex_home else []
    sessions: dict[int, list[dict]] = {}
    pid, writable = 0, False
    for line in result.stdout.splitlines():
        if line.startswith("p"):
            pid = int(line[1:]) if line[1:].isdigit() else 0
            writable = False
        elif line.startswith("f"):
            writable = False
        elif line.startswith("a"):
            writable = line[1:] in {"w", "u"}
        elif line.startswith("n") and pid and writable:
            path = Path(line[1:])
            try:
                path = path.resolve()
                if path.suffix != ".jsonl":
                    continue
                if codex_home is not None:
                    if not any(path.is_relative_to(root) for root in roots):
                        continue
                    storage = codex_home.resolve()
                else:
                    directory = next((ancestor for ancestor in path.parents if ancestor.name in {"sessions", "archived_sessions"}), None)
                    if directory is None:
                        continue
                    storage = directory.parent
                meta = read_rollout_meta(path, max_lines=1)
                if not meta:
                    continue
                sessions.setdefault(pid, []).append(
                    {
                        "sid": meta["sid"],
                        "cwd": meta["cwd"],
                        "codex_home": str(storage),
                        "rollout": str(path),
                    }
                )
            except (OSError, ValueError, AttributeError):
                continue
    return sessions


def loaded_sessions(pid: int, codex_home: Path | None = None) -> list[dict]:
    return _open_sessions(["-a", "-p", str(pid)], codex_home).get(pid, []) if pid > 0 else []


def session_writers(codex_home: Path) -> dict[int, list[dict]]:
    """Read current writers, including instances outside the selected tmux panes."""
    return _open_sessions(["-c", "codex", "-c", "Codex"], codex_home)


def session_loaded(pid: int, sid: str, codex_home: Path) -> bool:
    """A startup preview/argv is insufficient: require an exact writable rollout."""
    return bool(sid) and any(record["sid"] == sid for record in loaded_sessions(pid, codex_home))


def current_session(tty: str, preferred_sid: str = "") -> dict | None:
    """Find a pane's writable conversation independently of activity hooks."""
    if not tty:
        return None
    try:
        rows = subprocess.run(["ps", "-t", tty.removeprefix("/dev/"), "-o", "pid=,stat=,comm="],
                              text=True, capture_output=True, check=False, timeout=2).stdout.splitlines()
    except (OSError, subprocess.TimeoutExpired):
        return None
    records = {}
    for line in rows:
        fields = line.split(maxsplit=2)
        if len(fields) != 3 or not fields[0].isdigit() or "+" not in fields[1]:
            continue
        name = Path(fields[2]).name.lower()
        if name not in {"codex", "codexx"} and not name.startswith("codex-"):
            continue
        for record in loaded_sessions(int(fields[0])):
            records[(record["codex_home"], record["sid"])] = record
    if len(records) == 1:
        return next(iter(records.values()))
    preferred = [record for record in records.values() if record["sid"] == preferred_sid]
    return preferred[0] if len(preferred) == 1 else None


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Check for a saved Codex conversation")
    parser.add_argument("sid")
    parser.add_argument("codex_home", type=Path)
    arguments = parser.parse_args()
    raise SystemExit(0 if session_exists(arguments.sid, arguments.codex_home.expanduser()) else 1)
