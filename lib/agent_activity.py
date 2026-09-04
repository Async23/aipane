"""Resolve pane-local Agent Activity from reports and AI Tool evidence.

The tmux marker is a fast projection.  This module owns the safety decision:
callers receive ``busy``, ``idle``, or ``unknown`` and only explicit repair
callers may materialize a newer projection.
"""

from __future__ import annotations

import json
import os
import shlex
import sqlite3
import subprocess
import tempfile
import time
import urllib.parse
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from typing import Callable, Iterable, Mapping, Protocol

import fcntl


__all__ = (
    "ActivityError",
    "ActivityInspection",
    "ActivityReadError",
    "ActivityReconcileResult",
    "ActivityReportResult",
    "ActivitySession",
    "ActivityWriteError",
    "AgentActivity",
    "InMemoryActivityAdapter",
    "TmuxActivityAdapter",
    "codex_goal_state",
    "codex_thread_identity",
)


TERMINAL_CODEX_STATUSES = frozenset({"completed", "failed", "interrupted"})
TERMINAL_KIMI_REASONS = frozenset(
    {"completed", "cancelled", "failed", "blocked"}
)
ACTIVITY_MARKER_OPTION = "@tmux-window-wrap-activity"
ACTIVITY_REPORTER_OPTION = "@tmux-window-wrap-activity-reporter"
ACTIVITY_UPDATED_AT_OPTION = "@tmux-window-wrap-activity-updated-at"
ACTIVITY_RECORD_OPTION = "@tmux-window-wrap-activity-record"
ACTIVITY_OPTIONS = (
    ACTIVITY_MARKER_OPTION,
    ACTIVITY_REPORTER_OPTION,
    ACTIVITY_UPDATED_AT_OPTION,
    ACTIVITY_RECORD_OPTION,
)
ACTIVITY_FIELD_SEPARATOR = "\x1f"


class ActivityError(RuntimeError):
    """Base error for the Agent Activity interface."""


class ActivityReadError(ActivityError):
    """The pane projection could not be read safely."""


class ActivityWriteError(ActivityError):
    """The pane projection could not be changed safely."""


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
    try:
        lock_dir = runtime_root / f"aipane-activity-locks-{os.getuid()}"
        lock_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        tmux_identity = socket_name
        if not tmux_identity:
            # TMUX is "socket_path,server_pid,client_id". Writers inherited
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
    except OSError as error:
        raise ActivityWriteError(
            f"could not lock Agent Activity for pane {pane_id}: {error}"
        ) from error


@dataclass(frozen=True)
class PaneActivity:
    pane_id: str
    current_command: str
    target: str = ""
    pane_tty: str = ""
    socket_path: str = ""
    server_pid: str = ""
    pane_pid: str = ""
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


@dataclass(frozen=True)
class ActivitySession:
    """A session binding validated against the current pane projection."""

    session_id: str
    tool_key: str


@dataclass(frozen=True)
class ActivityGuard:
    """Opaque exact pane state used to seal a destructive recovery plan."""

    pane_id: str
    socket_path: str
    server_pid: str
    pane_pid: str
    current_command: str
    marker: str
    reporter: str
    updated_at: str
    record: str

    def as_dict(self) -> dict[str, str]:
        return {
            "pane_id": self.pane_id,
            "socket_path": self.socket_path,
            "server_pid": self.server_pid,
            "pane_pid": self.pane_pid,
            "current_command": self.current_command,
            "activity_marker": self.marker,
            "activity_reporter": self.reporter,
            "activity_updated_at": self.updated_at,
            "activity_record": self.record,
        }


@dataclass(frozen=True)
class ActivityInspection:
    """Stable, read-only result returned to Agent Activity consumers."""

    state: str
    reported: bool
    reason: str
    available: bool = True
    repairable: bool = False
    evidence: str = ""
    pane_id: str = ""
    current_command: str = ""
    session: ActivitySession | None = None
    guard: ActivityGuard | None = None


@dataclass(frozen=True)
class ActivityReportResult:
    state: str
    accepted: bool
    changed: bool


@dataclass(frozen=True)
class ActivityReconcileResult:
    owner: str | None
    busy: bool
    changed: bool
    available: bool = True
    errors: tuple[str, ...] = ()


@dataclass(frozen=True)
class ListedPaneActivity:
    attached: bool
    pane: PaneActivity


class ActivityProjectionAdapter(Protocol):
    """Primitive projection I/O; AgentActivity owns all protocol rules."""

    socket_name: str | None
    lock_identity: str | None
    environment: Mapping[str, str]

    def read(self, target: str) -> PaneActivity: ...

    def list_panes(self) -> tuple[str | None, list[ListedPaneActivity]]: ...

    def set_option(self, target: str, option: str, value: str) -> None: ...

    def unset_option(self, target: str, option: str) -> None: ...

    def wake(self) -> None: ...


class TmuxActivityAdapter:
    """Read and write the Agent Activity projection in tmux."""

    def __init__(
        self,
        *,
        socket_name: str | None = None,
        environment: Mapping[str, str] | None = None,
        command: Iterable[str] | None = None,
        runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
        wake: Callable[[str | None], None] | None = None,
    ) -> None:
        self.socket_name = socket_name
        self.environment = dict(
            os.environ if environment is None else environment
        )
        configured = list(command) if command is not None else shlex.split(
            self.environment.get("AIPANE_TMUX", "tmux")
        )
        if not configured:
            raise ActivityReadError("AIPANE_TMUX resolved to an empty command")
        self.command = configured
        inherited_socket = self.environment.get("TMUX", "").split(",", 1)[0]
        self.lock_identity = (
            socket_name or inherited_socket or shlex.join(configured)
        )
        self.runner = runner
        self.wake_callback = wake

    @staticmethod
    def pane_format() -> str:
        return ACTIVITY_FIELD_SEPARATOR.join(
            (
                "#{pane_id}",
                "#{pane_current_command}",
                "#{session_name}:#{window_id}.#{pane_id}",
                "#{pane_tty}",
                "#{socket_path}",
                "#{pid}",
                "#{pane_pid}",
                f"#{{{ACTIVITY_MARKER_OPTION}}}",
                f"#{{{ACTIVITY_REPORTER_OPTION}}}",
                f"#{{{ACTIVITY_UPDATED_AT_OPTION}}}",
                f"#{{{ACTIVITY_RECORD_OPTION}}}",
            )
        )

    def _run(
        self,
        *arguments: str,
        write: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        try:
            if self.runner is not None:
                completed = self.runner(self.socket_name, *arguments)
            else:
                command = list(self.command)
                if self.socket_name:
                    command.extend(("-L", self.socket_name))
                completed = subprocess.run(
                    [*command, *arguments],
                    check=False,
                    capture_output=True,
                    text=True,
                    env=self.environment,
                )
        except (OSError, subprocess.SubprocessError) as error:
            error_type = ActivityWriteError if write else ActivityReadError
            raise error_type(str(error)) from error
        if completed.returncode != 0:
            message = completed.stderr.strip() or "tmux rejected the operation"
            error_type = ActivityWriteError if write else ActivityReadError
            raise error_type(message)
        return completed

    @staticmethod
    def _pane(fields: list[str]) -> PaneActivity:
        if len(fields) != 11:
            raise ActivityReadError("tmux returned an invalid activity projection")
        return PaneActivity(
            pane_id=fields[0],
            current_command=fields[1],
            target=fields[2],
            pane_tty=fields[3],
            socket_path=fields[4],
            server_pid=fields[5],
            pane_pid=fields[6],
            marker=fields[7],
            reporter=fields[8],
            marker_updated_at=fields[9],
            record=fields[10],
        )

    def read(self, target: str) -> PaneActivity:
        completed = self._run(
            "display-message",
            "-p",
            "-t",
            target,
            self.pane_format(),
        )
        fields = completed.stdout.rstrip("\n").split(
            ACTIVITY_FIELD_SEPARATOR,
            10,
        )
        pane = self._pane(fields)
        if not pane.pane_id or not pane.current_command:
            raise ActivityReadError(f"pane {target} has no live identity")
        return pane

    def list_panes(self) -> tuple[str | None, list[ListedPaneActivity]]:
        pane_format = ACTIVITY_FIELD_SEPARATOR.join(
            (
                "#{TMUX_WINDOW_WRAP_ANIMATOR_OWNER}",
                "#{session_attached}",
                self.pane_format(),
            )
        )
        completed = self._run("list-panes", "-a", "-F", pane_format)
        owner = None
        panes: list[ListedPaneActivity] = []
        invalid_projection = False
        for line in completed.stdout.splitlines():
            fields = line.split(ACTIVITY_FIELD_SEPARATOR, 12)
            if len(fields) != 13:
                invalid_projection = True
                continue
            if owner is None:
                owner = fields[0]
            try:
                pane = self._pane(fields[2:])
            except ActivityReadError:
                invalid_projection = True
                continue
            if not pane.pane_id or not pane.current_command:
                invalid_projection = True
                continue
            panes.append(
                ListedPaneActivity(attached=fields[1] != "0", pane=pane)
            )
        if invalid_projection:
            raise ActivityReadError(
                "tmux returned an invalid activity projection list"
            )
        return owner, panes

    def set_option(self, target: str, option: str, value: str) -> None:
        self._run(
            "set-option",
            "-p",
            "-t",
            target,
            option,
            value,
            write=True,
        )

    def unset_option(self, target: str, option: str) -> None:
        self._run(
            "set-option",
            "-p",
            "-u",
            "-t",
            target,
            option,
            write=True,
        )

    def wake(self) -> None:
        if self.wake_callback is not None:
            try:
                self.wake_callback(self.socket_name)
            except (OSError, subprocess.SubprocessError) as error:
                raise ActivityWriteError(str(error)) from error
            return
        self._run(
            "set-environment",
            "-g",
            "TMUX_WINDOW_WRAP_GENERATION",
            uuid.uuid4().hex,
            write=True,
        )
        clients = self._run("list-clients", "-F", "#{client_name}")
        for client in clients.stdout.splitlines():
            if client:
                self._run(
                    "refresh-client",
                    "-S",
                    "-t",
                    client,
                    write=True,
                )


class InMemoryActivityAdapter:
    """Deterministic projection adapter for interface-level tests."""

    _OPTION_FIELDS = {
        ACTIVITY_MARKER_OPTION: "marker",
        ACTIVITY_REPORTER_OPTION: "reporter",
        ACTIVITY_UPDATED_AT_OPTION: "marker_updated_at",
        ACTIVITY_RECORD_OPTION: "record",
    }

    def __init__(
        self,
        panes: Iterable[PaneActivity] = (),
        *,
        socket_name: str | None = "memory",
        environment: Mapping[str, str] | None = None,
        owner: str | None = "owner",
        attached: bool = True,
    ) -> None:
        self.socket_name = socket_name
        self.lock_identity = socket_name
        self.environment = dict(environment or {})
        self.owner = owner
        self.default_attached = attached
        self.panes = {pane.pane_id: pane for pane in panes}
        self.attached = {
            pane.pane_id: attached for pane in panes
        }
        self.writes: list[tuple[str, str, str | None]] = []
        self.wake_count = 0

    def add(self, pane: PaneActivity, *, attached: bool | None = None) -> None:
        self.panes[pane.pane_id] = pane
        self.attached[pane.pane_id] = (
            self.default_attached if attached is None else attached
        )

    def _pane_id(self, target: str) -> str:
        if target in self.panes:
            return target
        for pane_id, pane in self.panes.items():
            if pane.target == target:
                return pane_id
        raise ActivityReadError(f"pane {target} is unavailable")

    def read(self, target: str) -> PaneActivity:
        return self.panes[self._pane_id(target)]

    def list_panes(self) -> tuple[str | None, list[ListedPaneActivity]]:
        return self.owner, [
            ListedPaneActivity(self.attached.get(pane_id, False), pane)
            for pane_id, pane in self.panes.items()
        ]

    def set_option(self, target: str, option: str, value: str) -> None:
        pane_id = self._pane_id(target)
        field = self._OPTION_FIELDS.get(option)
        if field is None:
            raise ActivityWriteError(f"unsupported activity option: {option}")
        self.panes[pane_id] = replace(self.panes[pane_id], **{field: value})
        self.writes.append((pane_id, option, value))

    def unset_option(self, target: str, option: str) -> None:
        pane_id = self._pane_id(target)
        field = self._OPTION_FIELDS.get(option)
        if field is None:
            raise ActivityWriteError(f"unsupported activity option: {option}")
        self.panes[pane_id] = replace(self.panes[pane_id], **{field: ""})
        self.writes.append((pane_id, option, None))

    def wake(self) -> None:
        self.wake_count += 1


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
        elif _agent_commands_compatible(command, pane.current_command):
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


def is_kimi_command(current_command):
    return current_command == "kimi" or current_command.startswith("kimi-")


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
            if _agent_commands_compatible(command, pane.current_command):
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


def latest_kimi_turn_completion(
    session: Mapping[str, object],
    reported_at_ms: object,
) -> dict[str, object] | None:
    """Return a corroborated terminal event for one exact Kimi session.

    Kimi's external hook process can fail to spawn when the session's startup
    cwd is renamed.  Its session store is independent of that cwd, so use the
    append-only index to locate the session and require both state.json and the
    main agent's wire tail to agree before treating a missed Stop hook as idle.
    """

    session_id = session.get("id")
    home_value = session.get("kimi_home")
    if (
        not isinstance(session_id, str)
        or not session_id
        or Path(session_id).name != session_id
        or session_id in {".", ".."}
        or not isinstance(home_value, str)
        or not home_value
    ):
        return None

    if (
        not isinstance(reported_at_ms, int)
        or isinstance(reported_at_ms, bool)
        or reported_at_ms <= 0
    ):
        return None
    kimi_home = Path(home_value).expanduser()
    sessions_root = kimi_home / "sessions"
    session_dir = None
    for entry in reverse_json_records(
        kimi_home / "session_index.jsonl",
        max_bytes=4_194_304,
    ):
        if entry.get("sessionId") != session_id:
            continue
        if entry.get("deleted") is True:
            return None
        path_value = entry.get("sessionDir")
        if not isinstance(path_value, str) or not path_value:
            continue
        candidate = Path(path_value).expanduser()
        if (
            not candidate.is_absolute()
            or candidate.name != session_id
            or not _path_within(candidate, sessions_root)
        ):
            continue
        session_dir = candidate
        break
    if session_dir is None:
        return None

    try:
        with (session_dir / "state.json").open(encoding="utf-8") as handle:
            state = json.load(handle)
    except (OSError, ValueError):
        return None
    if not isinstance(state, dict) or state.get("id") != session_id:
        return None
    state_reason = state.get("lastTurnReason")
    state_updated_at = _milliseconds(state.get("updatedAt"))
    if (
        state_reason not in {"completed", "cancelled", "failed"}
        or state_updated_at is None
    ):
        return None

    terminal = None
    wire_path = session_dir / "agents" / "main" / "wire.jsonl"
    for event in reverse_json_records(wire_path):
        event_type = event.get("type")
        if event_type in {"prompt.accepted", "turn.prompt", "turn.started"}:
            # A newer turn has begun after any older terminal in the tail.
            return None
        if event_type != "turn.ended":
            continue
        agent_id = event.get("agentId")
        turn_id = event.get("turnId")
        reason = event.get("reason")
        ended_at = _milliseconds(event.get("time"))
        if (
            agent_id not in {None, "main"}
            or not isinstance(turn_id, int)
            or isinstance(turn_id, bool)
            or turn_id < 0
            or reason not in TERMINAL_KIMI_REASONS
            or ended_at is None
        ):
            return None
        terminal = {
            "status": "idle",
            "session_id": session_id,
            "turn_id": str(turn_id),
            "reason": reason,
            "updated_at": ended_at,
        }
        break
    if terminal is None:
        return None

    expected_state_reason = (
        "failed" if terminal["reason"] == "blocked" else terminal["reason"]
    )
    if (
        state_reason != expected_state_reason
        or state_updated_at < terminal["updated_at"]
        or terminal["updated_at"] < reported_at_ms
    ):
        return None
    return terminal


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
    """Own Agent Activity reporting, inspection, and projection repair."""

    def __init__(
        self,
        *,
        adapter: ActivityProjectionAdapter,
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
        self.adapter = adapter
        source_environment = (
            adapter.environment
            if environment is None
            else environment
        )
        self.environment = dict(source_environment)
        self.process_matches = process_matches or _default_process_matches
        self.process_started_at = (
            process_started_at or _default_process_started_at
        )
        self.process_identity = process_identity or _default_process_identity
        self.process_exists = process_exists or process_is_running
        self.process_tty = process_tty or globals()["process_tty"]
        self._repair_candidates: dict[str, tuple[str, ...]] = {}

    def report(
        self,
        target: str,
        state: str,
        payload: Mapping[str, object] | None = None,
        *,
        hook_event_name: str = "",
        hook_source: str = "",
        now_ms: int | None = None,
    ) -> ActivityReportResult:
        """Accept a lifecycle report and project it to one live pane."""

        adapter = self.adapter
        if state not in {"busy", "idle"}:
            raise ValueError(f"unsupported Agent Activity state: {state}")
        normalized = dict(payload) if isinstance(payload, Mapping) else {}
        if hook_event_name and not normalized.get("hook_event_name"):
            normalized["hook_event_name"] = hook_event_name
        if hook_source and not normalized.get("source"):
            normalized["source"] = hook_source
        event_name = normalized.get("hook_event_name", "")
        source = normalized.get("source", "")

        try:
            initial = adapter.read(target)
        except ActivityReadError:
            return ActivityReportResult(
                state="unknown",
                accepted=False,
                changed=False,
            )
        with pane_activity_lock(
            adapter.lock_identity,
            initial.pane_id,
            self.environment,
        ):
            try:
                pane = adapter.read(initial.pane_id)
            except ActivityReadError:
                return ActivityReportResult(
                    state="unknown",
                    accepted=False,
                    changed=False,
                )
            if not self._same_live_identity(initial, pane):
                return ActivityReportResult(
                    state="unknown",
                    accepted=False,
                    changed=False,
                )
            if self._ignore_early_codex_idle(
                state,
                str(event_name) if isinstance(event_name, str) else "",
                str(source) if isinstance(source, str) else "",
                pane.current_command,
            ):
                current = self._resolve(pane)
                return ActivityReportResult(
                    state=current.state,
                    accepted=False,
                    changed=False,
                )

            timestamp = int(
                now_ms if now_ms is not None else time.time_ns() // 1_000_000
            )
            decision = self._prepare_report(
                pane,
                state,
                normalized,
                now_ms=timestamp,
            )
            if not decision.accepted:
                return ActivityReportResult(
                    state=decision.state,
                    accepted=False,
                    changed=False,
                )

            desired_marker = (
                pane.current_command if decision.state == "busy" else ""
            )
            changes: list[tuple[str, str | None]] = []
            if decision.record != pane.record:
                # The record is safety truth; compatibility/display fields are
                # projected only after this write succeeds.
                changes.append((ACTIVITY_RECORD_OPTION, decision.record))
            changes.append(
                (
                    ACTIVITY_UPDATED_AT_OPTION,
                    str(timestamp) if decision.state == "busy" else None,
                )
            )
            changes.append((ACTIVITY_REPORTER_OPTION, pane.current_command))
            changes.append(
                (ACTIVITY_MARKER_OPTION, desired_marker or None)
            )
            changed = self._apply_changes(pane, changes)

            repeated_busy = (
                decision.state == "busy"
                and pane.marker == desired_marker
                and pane.reporter == pane.current_command
                and event_name != "PreToolUse"
            )
            marker_changed = pane.marker != desired_marker
            if marker_changed or repeated_busy:
                self._wake_best_effort()
            return ActivityReportResult(
                state=decision.state,
                accepted=True,
                changed=changed,
            )

    def inspect(self, target: str) -> ActivityInspection:
        """Read effective state without repairing or otherwise mutating tmux."""

        try:
            pane = self.adapter.read(target)
        except ActivityReadError:
            return ActivityInspection(
                state="unknown",
                reported=False,
                reason="activity_projection_unavailable",
                available=False,
            )
        return self._inspection(pane)

    def reconcile(self) -> ActivityReconcileResult:
        """Probe all panes and repair only stable, revalidated evidence."""

        adapter = self.adapter
        try:
            owner, listed_panes = adapter.list_panes()
        except ActivityReadError as error:
            return ActivityReconcileResult(
                owner=None,
                busy=False,
                changed=False,
                available=False,
                errors=(str(error),),
            )

        live_ids = {listed.pane.pane_id for listed in listed_panes}
        for pane_id in tuple(self._repair_candidates):
            if pane_id not in live_ids:
                self._repair_candidates.pop(pane_id, None)

        busy = False
        changed = False
        errors: list[str] = []
        for listed in listed_panes:
            pane = listed.pane
            marked_busy = bool(pane.marker) and pane.marker == pane.current_command
            if not listed.attached:
                self._repair_candidates.pop(pane.pane_id, None)
                continue
            if not (pane.marker or pane.reporter or pane.record):
                self._repair_candidates.pop(pane.pane_id, None)
                continue

            view = self._resolve(pane)
            stable_repair = (
                (
                    _is_codex_command(pane.current_command)
                    and (marked_busy or pane.record or pane.reporter == pane.current_command)
                )
                or (
                    is_kimi_command(pane.current_command)
                    and marked_busy
                    and bool(pane.record)
                )
            )
            if stable_repair and view.repairable:
                candidate = (
                    view.state,
                    pane.record,
                    pane.marker_updated_at,
                    view.evidence_turn_id,
                    view.reason,
                )
                if self._repair_candidates.get(pane.pane_id) == candidate:
                    try:
                        repaired = self._repair(pane, view)
                    except ActivityError as error:
                        repaired = False
                        errors.append(f"{pane.pane_id}: {error}")
                    if repaired:
                        self._repair_candidates.pop(pane.pane_id, None)
                        changed = True
                        if view.state == "busy":
                            busy = True
                        continue
                else:
                    self._repair_candidates[pane.pane_id] = candidate
                if view.state == "busy" or marked_busy:
                    busy = True
                continue

            self._repair_candidates.pop(pane.pane_id, None)
            legacy_idle = (
                marked_busy
                and view.state == "idle"
                and view.reason in {"claude_registry_idle", "grok_update_idle"}
            )
            if legacy_idle:
                try:
                    repaired = self._repair_legacy_marker(pane, view)
                except ActivityError as error:
                    repaired = False
                    errors.append(f"{pane.pane_id}: {error}")
                if repaired:
                    changed = True
                    continue
                busy = True
                continue
            if view.state == "busy" or (
                view.state == "unknown" and marked_busy
            ):
                busy = True

        if changed:
            try:
                adapter.wake()
            except ActivityError as error:
                errors.append(str(error))
        return ActivityReconcileResult(
            owner=owner,
            busy=busy,
            changed=changed,
            errors=tuple(errors),
        )

    def clear(self, target: str) -> bool:
        """Explicitly remove all Agent Activity projection fields."""

        adapter = self.adapter
        initial = adapter.read(target)
        with pane_activity_lock(
            adapter.lock_identity,
            initial.pane_id,
            self.environment,
        ):
            pane = adapter.read(initial.pane_id)
            if not self._same_live_identity(initial, pane):
                return False
            return self._apply_changes(
                pane,
                [(option, None) for option in ACTIVITY_OPTIONS],
            )

    def _inspection(self, pane: PaneActivity) -> ActivityInspection:
        view = self._resolve(pane)
        return ActivityInspection(
            state=view.state,
            reported=view.reported,
            reason=view.reason,
            repairable=view.repairable,
            evidence=view.evidence_turn_id,
            pane_id=pane.pane_id,
            current_command=pane.current_command,
            session=self._validated_session(pane),
            guard=ActivityGuard(
                pane_id=pane.pane_id,
                socket_path=pane.socket_path,
                server_pid=pane.server_pid,
                pane_pid=pane.pane_pid,
                current_command=pane.current_command,
                marker=pane.marker,
                reporter=pane.reporter,
                updated_at=pane.marker_updated_at,
                record=pane.record,
            ),
        )

    def _validated_session(self, pane: PaneActivity) -> ActivitySession | None:
        record = _record_object(pane.record)
        if record is None or record.get("version") != 1:
            return None
        owner = record.get("owner")
        identity = record.get("pane")
        root = record.get("root")
        session_id = root.get("session_id") if isinstance(root, dict) else None
        if (
            not isinstance(owner, str)
            or not _is_codex_command(owner)
            or owner != pane.current_command
            or not isinstance(identity, dict)
            or identity.get("id") != pane.pane_id
            or identity.get("socket") != pane.socket_path
            or str(identity.get("server_pid", "")) != pane.server_pid
            or not isinstance(session_id, str)
            or not session_id
        ):
            return None
        return ActivitySession(session_id=session_id, tool_key="x")

    def _apply_changes(
        self,
        pane: PaneActivity,
        changes: Iterable[tuple[str, str | None]],
    ) -> bool:
        adapter = self.adapter
        current = {
            ACTIVITY_MARKER_OPTION: pane.marker,
            ACTIVITY_REPORTER_OPTION: pane.reporter,
            ACTIVITY_UPDATED_AT_OPTION: pane.marker_updated_at,
            ACTIVITY_RECORD_OPTION: pane.record,
        }
        applied: list[tuple[str, str]] = []
        try:
            for option, desired in changes:
                desired_value = desired or ""
                if current[option] == desired_value:
                    continue
                previous = current[option]
                if desired_value:
                    adapter.set_option(pane.pane_id, option, desired_value)
                else:
                    adapter.unset_option(pane.pane_id, option)
                current[option] = desired_value
                applied.append((option, previous))
        except ActivityError as error:
            rollback_errors = []
            for option, previous in reversed(applied):
                try:
                    if previous:
                        adapter.set_option(pane.pane_id, option, previous)
                    else:
                        adapter.unset_option(pane.pane_id, option)
                except ActivityError as rollback_error:
                    rollback_errors.append(str(rollback_error))
            detail = f"activity projection write failed: {error}"
            if rollback_errors:
                detail += "; rollback failed: " + "; ".join(rollback_errors)
            raise ActivityWriteError(detail) from error
        return bool(applied)

    def _repair(self, pane: PaneActivity, view: ActivityView) -> bool:
        adapter = self.adapter
        with pane_activity_lock(
            adapter.lock_identity,
            pane.pane_id,
            self.environment,
        ):
            fresh = adapter.read(pane.pane_id)
            if not self._same_projection(fresh, pane):
                return False
            fresh_view = self._resolve(fresh)
            if (
                fresh_view.state != view.state
                or fresh_view.reason != view.reason
                or fresh_view.evidence_turn_id != view.evidence_turn_id
                or not fresh_view.repairable
            ):
                return False
            changes: list[tuple[str, str | None]] = [
                (ACTIVITY_RECORD_OPTION, fresh_view.repair_record)
            ]
            if fresh_view.state == "idle":
                changes.extend(
                    (
                        (ACTIVITY_MARKER_OPTION, None),
                        (ACTIVITY_UPDATED_AT_OPTION, None),
                    )
                )
            elif fresh_view.state == "busy":
                changes.extend(
                    (
                        (
                            ACTIVITY_UPDATED_AT_OPTION,
                            str(time.time_ns() // 1_000_000),
                        ),
                        (ACTIVITY_MARKER_OPTION, fresh.current_command),
                    )
                )
            return self._apply_changes(fresh, changes)

    def _repair_legacy_marker(
        self,
        pane: PaneActivity,
        view: ActivityView,
    ) -> bool:
        adapter = self.adapter
        with pane_activity_lock(
            adapter.lock_identity,
            pane.pane_id,
            self.environment,
        ):
            fresh = adapter.read(pane.pane_id)
            if not self._same_projection(fresh, pane):
                return False
            fresh_view = self._resolve(fresh)
            if (
                fresh_view.state != "idle"
                or fresh_view.reason != view.reason
            ):
                return False
            return self._apply_changes(
                fresh,
                (
                    (ACTIVITY_MARKER_OPTION, None),
                    (ACTIVITY_UPDATED_AT_OPTION, None),
                ),
            )

    @staticmethod
    def _same_projection(left: PaneActivity, right: PaneActivity) -> bool:
        # A pane id and a canonical coordinate may both address the same pane;
        # target is routing metadata, not part of the safety identity.
        return replace(left, target="") == replace(right, target="")

    @staticmethod
    def _same_live_identity(left: PaneActivity, right: PaneActivity) -> bool:
        return (
            left.pane_id,
            left.current_command,
            left.pane_tty,
            left.socket_path,
            left.server_pid,
            left.pane_pid,
        ) == (
            right.pane_id,
            right.current_command,
            right.pane_tty,
            right.socket_path,
            right.server_pid,
            right.pane_pid,
        )

    def _wake_best_effort(self) -> None:
        try:
            self.adapter.wake()
        except ActivityError:
            # Projection truth is already durable. The animator's next probe
            # will observe it even if an eager status refresh was unavailable.
            pass

    def _ignore_early_codex_idle(
        self,
        state: str,
        hook_event_name: str,
        hook_source: str,
        owner: str,
    ) -> bool:
        is_early_event = hook_event_name == "Stop" or (
            hook_event_name == "SessionStart" and hook_source == "compact"
        )
        return (
            state == "idle"
            and self.environment.get(
                "TMUX_WINDOW_WRAP_ALLOW_CODEX_STOP_IDLE"
            )
            != "1"
            and is_early_event
            and _is_codex_command(owner)
        )

    def _prepare_report(
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
        elif is_kimi_command(pane.current_command):
            kimi_home = self.environment.get("KIMI_CODE_HOME") or str(
                Path(self.environment.get("HOME", str(Path.home())))
                / ".kimi-code"
            )
            record["session"] = {
                "id": session_id if isinstance(session_id, str) else "",
                "kimi_home": str(Path(kimi_home).expanduser().absolute()),
            }
        return ActivityReport(
            state=state,
            record=json.dumps(record, separators=(",", ":")),
            accepted=True,
            wake=True,
        )

    def _resolve(self, pane: PaneActivity) -> ActivityView:
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
        if is_kimi_command(pane.current_command) and record is not None:
            session = record.get("session")
            reported_at = _milliseconds(record.get("updated_at"))
            try:
                marker_value = int(pane.marker_updated_at)
            except (TypeError, ValueError):
                marker_value = None
            marker_at = _milliseconds(marker_value)
            boundary = (
                max(reported_at, marker_at)
                if reported_at is not None and marker_at is not None
                else None
            )
            state = (
                latest_kimi_turn_completion(session, boundary)
                if isinstance(session, dict) and boundary is not None
                else None
            )
            if state is not None:
                reason = "kimi_turn_ended"
                evidence = str(state["turn_id"])
        elif is_grok_command(pane.current_command):
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


def _agent_commands_compatible(left: str, right: str) -> bool:
    return (
        left == right
        or (_is_codex_command(left) and _is_codex_command(right))
        or (is_kimi_command(left) and is_kimi_command(right))
    )
