"""Read-only evidence for local Codex conversations and their loaded state."""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path


def rollout_matches_session(path: Path, sid: str) -> bool:
    try:
        with path.open(encoding="utf-8") as rollout:
            metadata = json.loads(rollout.readline(65536))
        return (
            isinstance(metadata, dict)
            and metadata.get("type") == "session_meta"
            and isinstance(metadata.get("payload"), dict)
            and metadata["payload"].get("id") == sid
        )
    except (OSError, ValueError):
        return False


def session_exists(sid: str, codex_home: Path) -> bool:
    """Find a durable rollout, including files with an extra rollout ID.

    A filename can end in either <session-id>.jsonl or
    <session-id>_<rollout-id>.jsonl. The header identifies the conversation;
    the extra filename ID must never become the resume ID.
    """
    if not sid:
        return False
    for root_name in ("sessions", "archived_sessions"):
        root = codex_home / root_name
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
                with path.open(encoding="utf-8") as rollout:
                    metadata = json.loads(rollout.readline(65536))
                payload = metadata.get("payload", {})
                if metadata.get("type") != "session_meta" or not isinstance(payload, dict) or not payload.get("id"):
                    continue
                sessions.setdefault(pid, []).append({"sid": payload["id"], "cwd": payload.get("cwd", ""),
                                                     "codex_home": str(storage), "rollout": str(path)})
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
