"""Resolve pane-local Agent Activity from reports and AI Tool evidence.

The tmux marker is a fast projection.  This module owns the safety decision:
callers receive ``busy``, ``idle``, or ``unknown`` and only explicit repair
callers may materialize a newer projection.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import tempfile
import time
import urllib.parse
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from typing import Callable, Mapping

import fcntl


TERMINAL_CODEX_STATUSES = frozenset({"completed", "failed", "interrupted"})


@contextmanager
def pane_activity_lock(
    socket_name: str | None,
    pane_id: str,
    environment: Mapping[str, str] | None = None,
):
    """Serialize all aipane writers for one tmux pane.

    ``flock`` is released by the kernel if a hook process exits unexpectedly,
    unlike a tmux ``wait-for -L`` lock that can be stranded by a crash.
    """

    if environment is None:
        environment = os.environ
    runtime_root = Path(
        environment.get("XDG_RUNTIME_DIR")
        or environment.get("TMPDIR")
        or tempfile.gettempdir()
    )
    lock_dir = runtime_root / f"aipane-activity-locks-{os.getuid()}"
    lock_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmux_identity = socket_name
    if not tmux_identity:
        # TMUX is "socket_path,server_pid,client_id".  Writers inherited
        # from different clients must still contend on the same pane lock.
        tmux_identity = environment.get("TMUX", "").split(",", 1)[0]
    identity = f"{tmux_identity}\0{pane_id}"
    lock_path = lock_dir / f"{sha256(identity.encode()).hexdigest()}.lock"
    with lock_path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@dataclass(frozen=True)
class PaneActivity:
    pane_id: str
    current_command: str
    target: str = ""
    pane_tty: str = ""
    socket_path: str = ""
    server_pid: str = ""
    marker: str = ""
    reporter: str = ""
    marker_updated_at: str = ""
    record: str = ""


@dataclass(frozen=True)
class ActivityView:
    state: str
    reported: bool
    reason: str
    revision: str = ""
    evidence_turn_id: str = ""
    repair_record: str = ""

    @property
    def repairable(self) -> bool:
        return bool(self.repair_record)


@dataclass(frozen=True)
class ActivityReport:
    state: str
    record: str
    accepted: bool
    wake: bool


@dataclass(frozen=True)
class CodexThreadIdentity:
    root_session_id: str
    scope: str


def _default_process_matches(
    process: Mapping[str, object],
    pane: PaneActivity,
) -> bool:
    pid = process.get("pid")
    started_at = process.get("started_at")
    if (
        not isinstance(pid, int)
        or isinstance(pid, bool)
        or pid <= 0
        or not isinstance(started_at, str)
        or not started_at
    ):
        return False
    try:
        os.kill(pid, 0)
        completed = subprocess.run(
            ["ps", "-p", str(pid), "-o", "lstart=,tty="],
            check=False,
            capture_output=True,
            text=True,
        )
    except (OSError, PermissionError, ProcessLookupError):
        return False
    line = completed.stdout.strip()
    if completed.returncode != 0 or len(line) < 24:
        return False
    if line[:24] != started_at:
        return False
    process_tty = line[24:].strip().removeprefix("/dev/")
    pane_tty = pane.pane_tty.removeprefix("/dev/")
    return not pane_tty or process_tty == pane_tty


def _default_process_started_at(pane: PaneActivity) -> int | None:
    tty = pane.pane_tty.removeprefix("/dev/")
    if not tty or not pane.current_command:
        return None
    try:
        completed = subprocess.run(
            ["ps", "-t", tty, "-o", "lstart=,comm="],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return None
    if completed.returncode != 0:
        return None

    exact: list[int] = []
    compatible: list[int] = []
    for line in completed.stdout.splitlines():
        if len(line) < 26:
            continue
        started_raw = line[:24]
        command = Path(line[24:].strip()).name
        try:
            started_at = int(
                datetime.strptime(
                    started_raw,
                    "%a %b %d %H:%M:%S %Y",
                ).timestamp()
                * 1000
            )
        except ValueError:
            continue
        if command == pane.current_command:
            exact.append(started_at)
        elif _is_codex_command(command) and _is_codex_command(
            pane.current_command
        ):
            compatible.append(started_at)
    candidates = exact or compatible
    return min(candidates) if candidates else None


def process_is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def process_tty(pid: int) -> str:
    try:
        completed = subprocess.run(
            ["ps", "-p", str(pid), "-o", "tty="],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return ""
    tty = completed.stdout.strip()
    if completed.returncode != 0 or tty in {"", "?", "??", "-"}:
        return ""
    return tty.removeprefix("/dev/")


def load_claude_session_states(
    targets=None,
    environment=None,
    process_exists=None,
    process_tty_lookup=None,
):
    if environment is None:
        environment = os.environ
    if process_exists is None:
        process_exists = process_is_running
    if process_tty_lookup is None:
        process_tty_lookup = process_tty
    config_dir = environment.get("CLAUDE_CONFIG_DIR")
    if not config_dir:
        home = environment.get("HOME") or os.path.expanduser("~")
        config_dir = os.path.join(home, ".claude")
    sessions_dir = os.path.join(config_dir, "sessions")
    try:
        entries = os.scandir(sessions_dir)
    except OSError:
        return {}

    states = {}
    with entries:
        for entry in entries:
            try:
                if not entry.name.endswith(".json") or not entry.is_file():
                    continue
                with open(entry.path, encoding="utf-8") as handle:
                    record = json.load(handle)
                pid = record.get("pid")
                target = record.get("tmux")
                status = record.get("status")
                updated_at = record.get("statusUpdatedAt")
                version = record.get("version")
                if (
                    not isinstance(pid, int)
                    or isinstance(pid, bool)
                    or pid <= 0
                    or not isinstance(target, str)
                    or not target
                    or not isinstance(status, str)
                    or not isinstance(updated_at, (int, float))
                    or isinstance(updated_at, bool)
                    or not isinstance(version, str)
                    or (targets is not None and target not in targets)
                    or not process_exists(pid)
                ):
                    continue
                tty = process_tty_lookup(pid)
                if not tty:
                    continue
            except (AttributeError, OSError, TypeError, ValueError):
                continue
            state = {
                "pid": pid,
                "status": status,
                "updated_at": int(updated_at),
                "version": version,
                "tty": tty,
            }
            previous = states.get(target)
            if previous is None or state["updated_at"] > previous["updated_at"]:
                states[target] = state
    return states


def claude_idle_supersedes_marker(
    state,
    current_command,
    pane_tty,
    marker_updated_at,
):
    if not state or state["status"] != "idle":
        return False
    if current_command != state["version"] and current_command != "claude":
        return False
    if pane_tty.removeprefix("/dev/") != state["tty"]:
        return False
    try:
        marker_timestamp = int(marker_updated_at)
    except (TypeError, ValueError):
        marker_timestamp = 0
    return state["updated_at"] >= marker_timestamp


def reverse_json_records(path, max_bytes=1_048_576, block_size=8192):
    try:
        handle = open(path, "rb")
    except OSError:
        return
    with handle:
        try:
            handle.seek(0, os.SEEK_END)
            position = handle.tell()
        except OSError:
            return
        pending = b""
        consumed = 0
        while position > 0 and consumed < max_bytes:
            size = min(block_size, position, max_bytes - consumed)
            position -= size
            try:
                handle.seek(position)
                chunk = handle.read(size)
            except OSError:
                return
            if not chunk:
                return
            consumed += len(chunk)
            lines = (chunk + pending).split(b"\n")
            pending = lines[0]
            for line in reversed(lines[1:]):
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue
                if isinstance(record, dict):
                    yield record
        if position == 0 and pending.strip():
            try:
                record = json.loads(pending)
            except (UnicodeDecodeError, json.JSONDecodeError):
                return
            if isinstance(record, dict):
                yield record


def grok_record_timestamp_ms(record):
    metadata = record.get("_meta")
    if not isinstance(metadata, dict):
        params = record.get("params")
        metadata = params.get("_meta") if isinstance(params, dict) else None
    value = metadata.get("agentTimestampMs") if isinstance(metadata, dict) else None
    if value is None:
        value = record.get("timestamp")
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    timestamp = int(value)
    if timestamp <= 0:
        return None
    return timestamp * 1000 if timestamp < 100_000_000_000 else timestamp


def latest_grok_turn_completion(updates_path):
    for record in reverse_json_records(updates_path):
        params = record.get("params")
        update = params.get("update") if isinstance(params, dict) else None
        if not isinstance(update, dict):
            continue
        if update.get("sessionUpdate") != "turn_completed":
            continue
        updated_at = grok_record_timestamp_ms(record)
        if updated_at is None:
            return None
        return {
            "status": "idle",
            "updated_at": updated_at,
            "stop_reason": update.get("stop_reason"),
        }
    return None


def load_grok_session_states(
    ttys=None,
    environment=None,
    process_exists=None,
    process_tty_lookup=None,
):
    if environment is None:
        environment = os.environ
    if process_exists is None:
        process_exists = process_is_running
    if process_tty_lookup is None:
        process_tty_lookup = process_tty
    grok_home = environment.get("GROK_HOME")
    if not grok_home:
        home = environment.get("HOME") or os.path.expanduser("~")
        grok_home = os.path.join(home, ".grok")
    grok_home = os.path.abspath(os.path.expanduser(grok_home))
    try:
        with open(
            os.path.join(grok_home, "active_sessions.json"),
            encoding="utf-8",
        ) as handle:
            records = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(records, list):
        return {}

    states = {}
    for record in records:
        try:
            if not isinstance(record, dict):
                continue
            pid = record.get("pid")
            session_id = record.get("session_id")
            cwd = record.get("cwd")
            if (
                not isinstance(pid, int)
                or isinstance(pid, bool)
                or pid <= 0
                or not isinstance(session_id, str)
                or not session_id
                or not isinstance(cwd, str)
                or not os.path.isabs(cwd)
                or not process_exists(pid)
            ):
                continue
            uuid.UUID(session_id)
            tty = process_tty_lookup(pid).removeprefix("/dev/")
            if not tty or (ttys is not None and tty not in ttys):
                continue
            encoded_cwd = urllib.parse.quote(cwd, safe="")
            completion = latest_grok_turn_completion(
                os.path.join(
                    grok_home,
                    "sessions",
                    encoded_cwd,
                    session_id,
                    "updates.jsonl",
                )
            )
            if completion is None:
                continue
        except (AttributeError, OSError, TypeError, ValueError):
            continue
        state = {
            **completion,
            "pid": pid,
            "session_id": session_id,
            "tty": tty,
        }
        previous = states.get(tty)
        if previous is None or state["updated_at"] > previous["updated_at"]:
            states[tty] = state
    return states


def is_grok_command(current_command):
    return current_command == "grok" or current_command.startswith("grok-")


def grok_idle_supersedes_marker(
    state,
    current_command,
    pane_tty,
    marker_updated_at,
):
    if not state or state["status"] != "idle":
        return False
    if not is_grok_command(current_command):
        return False
    if pane_tty.removeprefix("/dev/") != state["tty"]:
        return False
    try:
        marker_timestamp = int(marker_updated_at)
    except (TypeError, ValueError):
        return False
    return state["updated_at"] >= marker_timestamp


def _default_process_identity(pane: PaneActivity) -> dict[str, object]:
    pid = os.getppid()
    seen: set[int] = set()
    while pid > 1 and pid not in seen:
        seen.add(pid)
        try:
            completed = subprocess.run(
                ["ps", "-p", str(pid), "-o", "ppid=,lstart=,comm="],
                check=False,
                capture_output=True,
                text=True,
            )
        except OSError:
            break
        line = completed.stdout.strip()
        if completed.returncode != 0 or len(line) < 28:
            break
        fields = line.split(maxsplit=1)
        if len(fields) != 2:
            break
        try:
            parent_pid = int(fields[0])
        except ValueError:
            break
        identity = fields[1]
        if len(identity) >= 26:
            started_at = identity[:24]
            command = Path(identity[24:].strip()).name
            if command == pane.current_command or (
                _is_codex_command(command)
                and _is_codex_command(pane.current_command)
            ):
                return {
                    "pid": pid,
                    "started_at": started_at,
                }
        pid = parent_pid
    return {}


def _milliseconds(value: object) -> int | None:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    timestamp = int(value)
    if timestamp <= 0:
        return None
    return timestamp * 1000 if timestamp < 100_000_000_000 else timestamp


def _record_object(raw: str) -> dict[str, object] | None:
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(value, dict) or value.get("version") != 1:
        return None
    return value


def _path_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except (OSError, ValueError):
        return False
    return True


def codex_thread_identity(
    codex_home: str | os.PathLike[str],
    session_id: str,
) -> CodexThreadIdentity:
    """Resolve a legacy notify thread to its root using rollout metadata."""

    if not session_id:
        return CodexThreadIdentity("", "unknown")
    root = Path(codex_home).expanduser()
    current = session_id
    seen: set[str] = set()
    is_subagent = False
    identity_known = False
    while current and current not in seen:
        seen.add(current)
        matches = list(root.glob(f"sessions/*/*/*/*-{current}.jsonl"))
        metadata = None
        for path in matches:
            try:
                with path.open(encoding="utf-8") as handle:
                    first = json.loads(handle.readline())
            except (OSError, ValueError):
                continue
            candidate = first.get("payload") if isinstance(first, dict) else None
            if isinstance(candidate, dict) and candidate.get("id") == current:
                metadata = candidate
                identity_known = True
                break
        if metadata is None:
            break
        source = metadata.get("source")
        if not isinstance(source, dict):
            break
        subagent = source.get("subagent") or source.get("sub_agent") or {}
        if not isinstance(subagent, dict):
            break
        spawn = subagent.get("thread_spawn") or subagent.get("threadSpawn") or {}
        if not isinstance(spawn, dict):
            break
        parent = spawn.get("parent_thread_id") or spawn.get("parentThreadId")
        if not isinstance(parent, str) or not parent:
            break
        is_subagent = True
        current = parent
    if not identity_known:
        return CodexThreadIdentity(session_id, "unknown")
    return CodexThreadIdentity(
        current,
        "subagent" if is_subagent else "main",
    )


def codex_goal_state(
    codex_home: str | os.PathLike[str],
    session_id: str,
) -> str:
    root = Path(codex_home).expanduser()
    candidates = (
        root / "goals_1.sqlite",
        root / "sqlite" / "goals_1.sqlite",
    )
    goal_path = next((path for path in candidates if path.is_file()), None)
    if goal_path is None:
        return "inactive"
    try:
        uri = f"file:{goal_path.resolve()}?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=0.1)
        try:
            row = connection.execute(
                "SELECT status FROM thread_goals WHERE thread_id = ?",
                (session_id,),
            ).fetchone()
        finally:
            connection.close()
    except (OSError, sqlite3.Error):
        return "unknown"
    if row is None:
        return "inactive"
    status = row[0]
    if status == "active":
        return "active"
    if status in {
        "paused",
        "blocked",
        "usage_limited",
        "budget_limited",
        "complete",
    }:
        return "inactive"
    return "unknown"


class AgentActivity:
    """Resolve effective Agent Activity without mutating pane state."""

    def __init__(
        self,
        *,
        environment: Mapping[str, str] | None = None,
        process_matches: Callable[
            [Mapping[str, object], PaneActivity], bool
        ] | None = None,
        process_started_at: Callable[[PaneActivity], int | None] | None = None,
        process_identity: Callable[
            [PaneActivity], Mapping[str, object]
        ] | None = None,
        process_exists: Callable[[int], bool] | None = None,
        process_tty: Callable[[int], str] | None = None,
    ) -> None:
        self.environment = dict(
            os.environ if environment is None else environment
        )
        self.process_matches = process_matches or _default_process_matches
        self.process_started_at = (
            process_started_at or _default_process_started_at
        )
        self.process_identity = process_identity or _default_process_identity
        self.process_exists = process_exists or process_is_running
        self.process_tty = process_tty or globals()["process_tty"]

    def report(
        self,
        pane: PaneActivity,
        state: str,
        payload: Mapping[str, object] | None = None,
        *,
        now_ms: int | None = None,
    ) -> ActivityReport:
        if state not in {"busy", "idle"}:
            raise ValueError(f"unsupported Agent Activity state: {state}")
        payload = payload if isinstance(payload, Mapping) else {}
        now_ms = int(now_ms if now_ms is not None else time.time_ns() // 1_000_000)
        existing = _record_object(pane.record)
        event_name = payload.get("hook_event_name") or payload.get("type") or ""
        session_id = payload.get("session_id") or payload.get("thread-id")
        turn_id = payload.get("turn_id") or payload.get("turn-id")

        if (
            existing is None
            and _is_codex_command(pane.current_command)
            and event_name == "agent-turn-complete"
        ):
            busy = bool(pane.marker) and pane.marker == pane.current_command
            return ActivityReport(
                state="busy" if busy else "unknown",
                record=pane.record,
                accepted=False,
                wake=False,
            )

        if (
            existing is not None
            and _is_codex_command(pane.current_command)
            and event_name == "SessionEnd"
        ):
            root = existing.get("root")
            if (
                not isinstance(root, dict)
                or not isinstance(session_id, str)
                or not session_id
                or root.get("session_id") != session_id
            ):
                existing_state = existing.get("reported")
                return ActivityReport(
                    state=(
                        str(existing_state)
                        if existing_state in {"busy", "idle"}
                        else "unknown"
                    ),
                    record=pane.record,
                    accepted=False,
                    wake=False,
                )
            ended = dict(existing)
            ended["revision"] = uuid.uuid4().hex
            ended["reported"] = "idle"
            ended["terminal_event"] = {
                "type": "SessionEnd",
                "session_id": session_id,
                "reported_at": now_ms,
            }
            return ActivityReport(
                state="idle",
                record=json.dumps(ended, separators=(",", ":")),
                accepted=True,
                wake=existing.get("reported") != "idle",
            )

        if (
            existing is not None
            and _is_codex_command(pane.current_command)
            and state == "busy"
            and (
                bool(payload.get("agent_id"))
                or bool(payload.get("agent_type"))
            )
        ):
            existing_state = existing.get("reported")
            if existing_state == "busy":
                return ActivityReport(
                    state="busy",
                    record=pane.record,
                    accepted=True,
                    wake=False,
                )
            promoted = dict(existing)
            promoted["revision"] = uuid.uuid4().hex
            promoted["reported"] = "busy"
            promoted["updated_at"] = now_ms
            return ActivityReport(
                state="busy",
                record=json.dumps(promoted, separators=(",", ":")),
                accepted=True,
                wake=True,
            )

        if (
            existing is not None
            and _is_codex_command(pane.current_command)
            and state == "busy"
            and existing.get("reported") == "busy"
            and event_name == "PreToolUse"
        ):
            root = existing.get("root")
            exact_reference = (
                isinstance(session_id, str)
                and bool(session_id)
                and isinstance(turn_id, str)
                and bool(turn_id)
            )
            same_turn = (
                isinstance(root, dict)
                and root.get("session_id") == session_id
                and root.get("turn_id") == turn_id
            )
            if not exact_reference or same_turn:
                return ActivityReport(
                    state="busy",
                    record=pane.record,
                    accepted=True,
                    wake=False,
                )

        if (
            existing is not None
            and _is_codex_command(pane.current_command)
            and event_name == "agent-turn-complete"
        ):
            root = existing.get("root")
            matches = (
                isinstance(root, dict)
                and isinstance(session_id, str)
                and session_id
                and isinstance(turn_id, str)
                and turn_id
                and root.get("session_id") == session_id
                and root.get("turn_id") == turn_id
            )
            if not matches:
                existing_state = existing.get("reported")
                return ActivityReport(
                    state=(
                        str(existing_state)
                        if existing_state in {"busy", "idle"}
                        else "busy"
                    ),
                    record=pane.record,
                    accepted=False,
                    wake=False,
                )
            if state == existing.get("reported"):
                return ActivityReport(
                    state=state,
                    record=pane.record,
                    accepted=True,
                    wake=False,
                )
            completed = dict(existing)
            completed["revision"] = uuid.uuid4().hex
            completed["reported"] = state
            completed["terminal_event"] = {
                "type": "agent-turn-complete",
                "turn_id": turn_id,
                "reported_at": now_ms,
            }
            return ActivityReport(
                state=state,
                record=json.dumps(completed, separators=(",", ":")),
                accepted=True,
                wake=True,
            )

        record: dict[str, object] = {
            "version": 1,
            "revision": uuid.uuid4().hex,
            "generation": uuid.uuid4().hex,
            "owner": pane.current_command,
            "reported": state,
            "updated_at": now_ms,
            "pane": {
                "id": pane.pane_id,
                "tty": pane.pane_tty,
                "socket": pane.socket_path,
                "server_pid": pane.server_pid,
            },
            "process": dict(self.process_identity(pane)),
        }
        if _is_codex_command(pane.current_command):
            transcript_path = payload.get("transcript_path")
            codex_home = self.environment.get("CODEX_HOME") or str(
                Path(self.environment.get("HOME", str(Path.home()))) / ".codex"
            )
            record["root"] = {
                "session_id": session_id if isinstance(session_id, str) else "",
                "turn_id": turn_id if isinstance(turn_id, str) else "",
                "transcript_path": (
                    transcript_path if isinstance(transcript_path, str) else ""
                ),
                "codex_home": str(Path(codex_home).expanduser()),
            }
        return ActivityReport(
            state=state,
            record=json.dumps(record, separators=(",", ":")),
            accepted=True,
            wake=True,
        )

    def resolve(self, pane: PaneActivity) -> ActivityView:
        record = _record_object(pane.record)
        if record is None:
            busy = bool(pane.marker) and pane.marker == pane.current_command
            reported = busy or pane.reporter == pane.current_command
            if _is_codex_command(pane.current_command) and reported:
                return self._resolve_legacy_codex(pane)
            if busy:
                external = self._external_idle_view(pane)
                if external is not None:
                    return external
            return ActivityView(
                state="busy" if busy else ("idle" if reported else "unknown"),
                reported=reported,
                reason="legacy_marker",
            )

        revision = record.get("revision")
        owner = record.get("owner")
        reported_state = record.get("reported")
        pane_identity = record.get("pane")
        process = record.get("process")
        if (
            not isinstance(revision, str)
            or not revision
            or owner != pane.current_command
            or reported_state not in {"busy", "idle"}
            or not isinstance(pane_identity, dict)
            or pane_identity.get("id") != pane.pane_id
            or (
                pane_identity.get("tty")
                and pane_identity.get("tty") != pane.pane_tty
            )
            or (
                pane_identity.get("socket")
                and pane_identity.get("socket") != pane.socket_path
            )
            or (
                pane_identity.get("server_pid")
                and str(pane_identity.get("server_pid")) != pane.server_pid
            )
            or not isinstance(process, dict)
            or not self._same_process(process, pane)
        ):
            return ActivityView(
                state="unknown",
                reported=True,
                reason="activity_identity_mismatch",
                revision=revision if isinstance(revision, str) else "",
            )

        if not _is_codex_command(pane.current_command):
            if reported_state == "busy":
                external = self._external_idle_view(
                    pane,
                    revision=revision,
                    record=record,
                )
                if external is not None:
                    return external
            return ActivityView(
                state=str(reported_state),
                reported=True,
                reason="reported_activity",
                revision=revision,
            )

        root = record.get("root")
        if not isinstance(root, dict):
            return ActivityView(
                state="unknown",
                reported=True,
                reason="codex_root_missing",
                revision=revision,
            )
        authority = self._resolve_codex(root, record.get("updated_at"))
        if authority[0] == "unknown":
            return ActivityView(
                state="unknown",
                reported=True,
                reason=authority[1],
                revision=revision,
            )

        effective, reason, turn_id = authority
        repair_record = ""
        if effective != reported_state:
            repaired = dict(record)
            repaired["revision"] = uuid.uuid4().hex
            repaired["reported"] = effective
            repaired["observed"] = {
                "state": effective,
                "reason": reason,
                "turn_id": turn_id,
            }
            repair_record = json.dumps(repaired, separators=(",", ":"))
        return ActivityView(
            state=effective,
            reported=True,
            reason=reason,
            revision=revision,
            evidence_turn_id=turn_id,
            repair_record=repair_record,
        )

    def _external_idle_view(
        self,
        pane: PaneActivity,
        *,
        revision: str = "",
        record: Mapping[str, object] | None = None,
    ) -> ActivityView | None:
        reason = ""
        evidence = ""
        if is_grok_command(pane.current_command):
            tty = pane.pane_tty.removeprefix("/dev/")
            states = load_grok_session_states(
                {tty},
                self.environment,
                self.process_exists,
                self.process_tty,
            )
            state = states.get(tty)
            if grok_idle_supersedes_marker(
                state,
                pane.current_command,
                pane.pane_tty,
                pane.marker_updated_at,
            ):
                reason = "grok_update_idle"
                evidence = str(state.get("session_id", ""))
        elif pane.target:
            states = load_claude_session_states(
                {pane.target},
                self.environment,
                self.process_exists,
                self.process_tty,
            )
            state = states.get(pane.target)
            if claude_idle_supersedes_marker(
                state,
                pane.current_command,
                pane.pane_tty,
                pane.marker_updated_at,
            ):
                reason = "claude_registry_idle"
                evidence = str(state.get("pid", ""))
        if not reason:
            return None

        repair_record = ""
        if record is not None:
            repaired = dict(record)
            repaired["revision"] = uuid.uuid4().hex
            repaired["reported"] = "idle"
            repaired["observed"] = {
                "state": "idle",
                "reason": reason,
                "evidence": evidence,
            }
            repair_record = json.dumps(repaired, separators=(",", ":"))
        return ActivityView(
            state="idle",
            reported=True,
            reason=reason,
            revision=revision,
            evidence_turn_id=evidence,
            repair_record=repair_record,
        )

    def _same_process(
        self,
        process: Mapping[str, object],
        pane: PaneActivity,
    ) -> bool:
        if "pid" in process:
            return self.process_matches(process, pane)
        recorded_start = _milliseconds(process.get("started_at_ms"))
        current_start = self.process_started_at(pane)
        return (
            recorded_start is not None
            and current_start is not None
            and recorded_start == current_start
        )

    def _resolve_legacy_codex(self, pane: PaneActivity) -> ActivityView:
        try:
            marker_at = int(pane.marker_updated_at)
        except (TypeError, ValueError):
            return ActivityView(
                state="unknown",
                reported=True,
                reason="legacy_codex_marker_time_missing",
            )
        process_started_at = self.process_started_at(pane)
        if process_started_at is None or process_started_at > marker_at:
            return ActivityView(
                state="unknown",
                reported=True,
                reason="legacy_codex_process_mismatch",
            )

        binding = self._legacy_binding(pane)
        if binding is None or int(binding["ts"]) * 1000 < process_started_at:
            return ActivityView(
                state="unknown",
                reported=True,
                reason="legacy_codex_binding_missing",
            )
        codex_home = Path(
            self.environment.get("CODEX_HOME")
            or Path(self.environment.get("HOME", str(Path.home()))) / ".codex"
        ).expanduser()
        session_id = str(binding["sid"])
        transcripts = list(
            codex_home.glob(f"sessions/*/*/*/*-{session_id}.jsonl")
        )
        if len(transcripts) != 1:
            return ActivityView(
                state="unknown",
                reported=True,
                reason="legacy_codex_transcript_missing",
            )
        root = {
            "session_id": session_id,
            "turn_id": "",
            "transcript_path": str(transcripts[0]),
            "codex_home": str(codex_home),
        }
        state, reason, evidence_turn_id = self._resolve_codex(root, marker_at)
        if state == "unknown":
            return ActivityView(
                state="unknown",
                reported=True,
                reason=reason,
                evidence_turn_id=evidence_turn_id,
            )

        repair_record = ""
        if state != ("busy" if pane.marker else "idle"):
            revision = uuid.uuid4().hex
            migrated = {
                "version": 1,
                "revision": revision,
                "generation": uuid.uuid4().hex,
                "owner": pane.current_command,
                "reported": state,
                # Keep the original report boundary for terminal ordering.
                # A repair timestamp here would make the already-observed
                # terminal look older than the report on the next resolve.
                "updated_at": marker_at,
                "repaired_at": int(time.time_ns() // 1_000_000),
                "pane": {
                    "id": pane.pane_id,
                    "tty": pane.pane_tty,
                    "socket": pane.socket_path,
                    "server_pid": pane.server_pid,
                },
                "process": {"started_at_ms": process_started_at},
                "root": {**root, "turn_id": evidence_turn_id},
                "observed": {
                    "state": state,
                    "reason": reason,
                    "turn_id": evidence_turn_id,
                },
            }
            repair_record = json.dumps(migrated, separators=(",", ":"))
        return ActivityView(
            state=state,
            reported=True,
            reason=reason,
            evidence_turn_id=evidence_turn_id,
            repair_record=repair_record,
        )

    def _legacy_binding(
        self,
        pane: PaneActivity,
    ) -> dict[str, object] | None:
        data_home = Path(
            self.environment.get("XDG_DATA_HOME")
            or Path(self.environment.get("HOME", str(Path.home())))
            / ".local"
            / "share"
        )
        registry = Path(
            self.environment.get("AIPANE_REGISTRY")
            or data_home / "aipane" / "registry.jsonl"
        )
        latest = None
        try:
            lines = registry.read_text(encoding="utf-8").splitlines()
        except OSError:
            return None
        for line in lines:
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if (
                isinstance(record, dict)
                and record.get("sock") == pane.socket_path
                and str(record.get("srv", "")) == pane.server_pid
                and record.get("pane") == pane.pane_id
                and record.get("tool") == "x"
                and isinstance(record.get("sid"), str)
                and record.get("sid")
                and isinstance(record.get("ts"), int)
            ):
                latest = record
        return latest

    def _resolve_codex(
        self,
        root: Mapping[str, object],
        reported_at: object,
    ) -> tuple[str, str, str]:
        session_id = root.get("session_id")
        turn_id = root.get("turn_id")
        transcript_value = root.get("transcript_path")
        home_value = root.get("codex_home")
        if not all(
            isinstance(value, str) and value
            for value in (session_id, transcript_value, home_value)
        ) or not isinstance(turn_id, str):
            return "unknown", "codex_turn_reference_missing", ""

        codex_home = Path(str(home_value)).expanduser()
        transcript = Path(str(transcript_value)).expanduser()
        if not _path_within(transcript, codex_home) or not transcript.is_file():
            return "unknown", "codex_transcript_mismatch", str(turn_id)
        history_path = codex_home / "thread_history_1.sqlite"
        try:
            transcript_size = transcript.stat().st_size
        except OSError:
            return "unknown", "codex_transcript_unavailable", str(turn_id)

        exact = None
        latest = None
        projection = None
        if history_path.is_file():
            try:
                uri = f"file:{history_path.resolve()}?mode=ro"
                connection = sqlite3.connect(uri, uri=True, timeout=0.1)
                try:
                    connection.execute("BEGIN")
                    exact = (
                        connection.execute(
                            """
                            SELECT rollout_ordinal, status, completed_at
                            FROM thread_turns
                            WHERE thread_id = ? AND turn_id = ?
                            """,
                            (session_id, turn_id),
                        ).fetchone()
                        if turn_id
                        else None
                    )
                    latest = connection.execute(
                        """
                        SELECT turn_id, rollout_ordinal, status, completed_at
                        FROM thread_turns
                        WHERE thread_id = ?
                        ORDER BY rollout_ordinal DESC
                        LIMIT 1
                        """,
                        (session_id,),
                    ).fetchone()
                    projection = connection.execute(
                        """
                        SELECT next_rollout_byte_offset
                        FROM thread_history_projection_state
                        WHERE thread_id = ?
                        """,
                        (session_id,),
                    ).fetchone()
                finally:
                    connection.close()
            except (OSError, sqlite3.Error):
                exact = latest = projection = None

        if not projection or projection[0] != transcript_size:
            return self._resolve_codex_rollout(
                codex_home,
                transcript,
                str(session_id),
                str(turn_id),
                reported_at,
            )
        if latest is None and not turn_id:
            goal = self._goal_state(codex_home, str(session_id))
            if goal == "active":
                return "busy", "codex_goal_active", ""
            if goal == "unknown":
                return "unknown", "codex_goal_unknown", ""
            return "idle", "codex_session_idle", ""
        if (turn_id and exact is None) or latest is None:
            return self._resolve_codex_rollout(
                codex_home,
                transcript,
                str(session_id),
                str(turn_id),
                reported_at,
            )

        latest_turn_id, _ordinal, status, completed_at = latest
        if status == "inProgress":
            return "busy", "codex_turn_in_progress", str(latest_turn_id)
        if status not in TERMINAL_CODEX_STATUSES:
            return "unknown", "codex_turn_status_unknown", str(latest_turn_id)
        completed_at_ms = _milliseconds(completed_at)
        reported_at_ms = _milliseconds(reported_at)
        if (
            completed_at_ms is None
            or reported_at_ms is None
            or completed_at_ms < reported_at_ms
        ):
            return "unknown", "codex_terminal_precedes_report", str(latest_turn_id)

        goal = self._goal_state(codex_home, str(session_id))
        if goal == "active":
            return "busy", "codex_goal_active", str(latest_turn_id)
        if goal == "unknown":
            return "unknown", "codex_goal_unknown", str(latest_turn_id)
        return "idle", f"codex_turn_{status}", str(latest_turn_id)

    def _resolve_codex_rollout(
        self,
        codex_home: Path,
        transcript: Path,
        session_id: str,
        expected_turn_id: str,
        reported_at: object,
        *,
        max_bytes: int = 2_097_152,
    ) -> tuple[str, str, str]:
        try:
            with transcript.open("rb") as handle:
                handle.seek(0, os.SEEK_END)
                size = handle.tell()
                start = max(0, size - max_bytes)
                handle.seek(start)
                data = handle.read()
        except OSError:
            return "unknown", "codex_rollout_unavailable", expected_turn_id
        if start:
            separator = data.find(b"\n")
            if separator < 0:
                return "unknown", "codex_rollout_window_incomplete", expected_turn_id
            data = data[separator + 1 :]

        latest: tuple[str, str, int | None] | None = None
        saw_expected = not expected_turn_id
        lines = data.split(b"\n")
        if lines and not lines[-1]:
            lines.pop()
        for raw_line in lines:
            if not raw_line.strip():
                continue
            try:
                record = json.loads(raw_line)
            except (UnicodeDecodeError, ValueError):
                return "unknown", "codex_rollout_truncated", expected_turn_id
            if not isinstance(record, dict) or record.get("type") != "event_msg":
                continue
            payload = record.get("payload")
            if not isinstance(payload, dict):
                continue
            event = payload.get("type")
            turn_id = payload.get("turn_id")
            if not isinstance(turn_id, str) or not turn_id:
                continue
            if turn_id == expected_turn_id:
                saw_expected = True
            if event == "task_started":
                latest = (turn_id, "inProgress", None)
            elif event == "turn_aborted":
                latest = (
                    turn_id,
                    "interrupted",
                    _milliseconds(payload.get("completed_at")),
                )
            elif event == "task_complete":
                latest = (
                    turn_id,
                    "failed" if payload.get("error") else "completed",
                    _milliseconds(payload.get("completed_at")),
                )

        if latest is None and not expected_turn_id and start == 0:
            goal = self._goal_state(codex_home, session_id)
            if goal == "active":
                return "busy", "codex_goal_active", ""
            if goal == "unknown":
                return "unknown", "codex_goal_unknown", ""
            return "idle", "codex_session_idle", ""
        if latest is None:
            return "unknown", "codex_rollout_lifecycle_missing", expected_turn_id
        latest_turn_id, status, completed_at_ms = latest
        if expected_turn_id and not saw_expected:
            return "unknown", "codex_rollout_expected_turn_missing", latest_turn_id
        if status == "inProgress":
            return "busy", "codex_rollout_turn_in_progress", latest_turn_id
        reported_at_ms = _milliseconds(reported_at)
        if (
            completed_at_ms is None
            or reported_at_ms is None
            or completed_at_ms < reported_at_ms
        ):
            return "unknown", "codex_rollout_terminal_precedes_report", latest_turn_id
        goal = self._goal_state(codex_home, session_id)
        if goal == "active":
            return "busy", "codex_goal_active", latest_turn_id
        if goal == "unknown":
            return "unknown", "codex_goal_unknown", latest_turn_id
        return "idle", f"codex_rollout_turn_{status}", latest_turn_id

    @staticmethod
    def _goal_state(codex_home: Path, session_id: str) -> str:
        return codex_goal_state(codex_home, session_id)


def _is_codex_command(command: str) -> bool:
    return command == "codex" or command.startswith("codex-")
