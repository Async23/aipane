"""Antigravity's pane-local runtime, lifecycle and exact recovery identity.

The CLI's statusLine callback is the authoritative selected-session interface.
Hooks enrich that state; a model invocation or a Stop alone is not completion.
Only the foreground CLI process may publish status, and hooks must match its
selected conversation. Readers verify PID lifetime, TTY and tmux incarnation.
"""

from __future__ import annotations

from contextlib import closing, contextmanager
from datetime import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import stat
import sqlite3
import subprocess
import sys
import time
import uuid
from urllib.parse import unquote, urlparse

SID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
MODES = {"auto", "plan", "accept-edits"}
EFFORTS = {"low", "medium", "high", "xhigh", "max"}
ROUTES = {"plan", "gcp"}
FIXED_VARIANT_SUFFIX = re.compile(r"\((low|medium|high|xhigh|extra|max)\)$", re.I)
AGY_RECOVERY_FIELDS = ("agy_home", "agy_model", "agy_effort", "agy_mode", "agy_agent", "agy_route")


def state_dir(environment=None) -> Path:
    env = os.environ if environment is None else environment
    data = Path(env.get("XDG_DATA_HOME") or Path(env.get("HOME", str(Path.home()))) / ".local/share")
    return Path(env.get("AIPANE_STATE_DIR") or data / "aipane") / "agy"


def cli_home(environment=None) -> Path:
    env = os.environ if environment is None else environment
    return Path(env.get("HOME", str(Path.home()))) / ".gemini/antigravity-cli"


def process_info(pid: int) -> dict | None:
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "pid=", "-o", "ppid=", "-o", "lstart=",
             "-o", "tty=", "-o", "stat=", "-o", "command="],
            capture_output=True, text=True, timeout=2, check=False,
            env={**os.environ, "LC_ALL": "C"},
        )
        fields = result.stdout.strip().split(None, 9)
        if result.returncode or len(fields) != 10:
            return None
        args = shlex.split(fields[9])
        return {"pid": int(fields[0]), "ppid": int(fields[1]),
                "started_at": datetime.strptime(" ".join(fields[2:7]), "%a %b %d %H:%M:%S %Y").timestamp(),
                "tty": fields[7].removeprefix("/dev/"), "foreground": "+" in fields[8],
                "executable": Path(args[0]).name if args else ""}
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def producer_process(reader=process_info) -> dict | None:
    pid = os.getppid()
    for _ in range(12):
        current = reader(pid)
        if not current:
            return None
        if current["executable"] == "agy":
            return current
        pid = current["ppid"]
        if pid <= 1:
            break
    return None


def runtime_path(pane: str, socket: str, server: str, environment=None) -> Path:
    key = hashlib.sha256(f"{socket}\0{server}\0{pane}".encode()).hexdigest()
    return state_dir(environment) / "runtime" / f"{key}.json"


def _read(path: Path) -> dict | None:
    try:
        info = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o022 or info.st_size > 65536):
            return None
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else None
    except (OSError, ValueError):
        return None


def _write(path: Path, value: dict) -> None:
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex)
    try:
        with temporary.open("x") as stream:
            temporary.chmod(0o600)
            json.dump(value, stream, ensure_ascii=False, separators=(",", ":"))
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def runtime_lock(path: Path):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with path.with_suffix(".lock").open("a") as stream:
        os.chmod(stream.name, 0o600)
        fcntl.flock(stream, fcntl.LOCK_EX)
        yield


def status_phase(payload: dict) -> str:
    tasks = payload.get("task_count", 0)
    agents = payload.get("subagents", [])
    if (type(tasks) is not int or tasks < 0 or not isinstance(agents, list)
            or any(not isinstance(item, dict) for item in agents)):
        return "unknown"
    if any(item.get("status") == "running" for item in agents):
        return "busy"
    phase = payload.get("agent_state")
    if phase in {"working", "tool_use"} or payload.get("tool_confirmation_pending") is True:
        return "busy"
    if phase == "reviewing":
        queued = payload.get("pending_input_count", 0)
        if type(queued) is not int or queued < 0:
            return "unknown"
        return "busy" if queued else "reviewing"
    return "idle" if phase in {"idle", "error"} else "unknown"


def turn_activity(phase: str, stop: dict, *, active: bool) -> str:
    if phase == "reviewing":
        # Opening a conversation can display its artifacts without starting a turn.
        if stop:
            phase = "idle" if stop.get("fullyIdle") is True else "busy"
        else:
            phase = "busy" if active else "idle"
    if phase == "idle" and stop.get("continuationActive"):
        return "busy"
    return phase


def final_reply_predates_process(transcript: Path, started_at: float) -> bool:
    """Corroborate a passive review when upgrading an ambiguous old busy record."""
    try:
        with transcript.open("rb") as stream:
            size = stream.seek(0, os.SEEK_END)
            limit = 65536
            stream.seek(max(0, size - limit))
            if size > limit:
                stream.readline(limit)  # Discard an incomplete first line.
            lines = stream.read(limit).splitlines()
            if not lines or os.fstat(stream.fileno()).st_mtime >= started_at:
                return False
        step = json.loads(lines[-1])
    except (OSError, ValueError):
        return False
    return (isinstance(step, dict) and step.get("source") == "MODEL"
            and step.get("type") == "PLANNER_RESPONSE" and step.get("status") == "DONE"
            and isinstance(step.get("content"), str) and bool(step["content"].strip())
            and not step.get("tool_calls") and not step.get("truncated_fields"))


def completion_ready(record: dict) -> bool:
    stop = record.get("stop")
    return (record.get("state") == "idle" and not record.get("has_background_tasks")
            and bool(stop) and not stop.get("continuationActive"))


def current_session(*, pane_id: str, socket_path: str, server_pid: str,
                    pane_tty: str, environment=None, reader=process_info) -> dict | None:
    record = _read(runtime_path(pane_id, socket_path, server_pid, environment))
    if not record or record.get("version") != 1:
        return None
    process = reader(record.get("pid", 0))
    if (not process or process.get("executable") != "agy" or not process.get("foreground")
            or process.get("pid") != record.get("pid")
            or process.get("started_at") != record.get("started_at")
            or process.get("tty") != pane_tty.removeprefix("/dev/")
            or record.get("pane") != pane_id or record.get("socket") != socket_path
            or record.get("server") != server_pid
            or record.get("state") not in {"busy", "idle", "unknown"}
            or not isinstance(record.get("cwd"), str) or not os.path.isabs(record["cwd"])
            or not isinstance(record.get("session_id"), str)
            or (record["session_id"] and not SID.fullmatch(record["session_id"]))):
        return None
    return record


def presence_lock_held(sid: str, home: Path) -> bool | None:
    """Probe Antigravity's native OS flock on presence/<sid>.lock without mutating it.

    Returns False if unlocked or absent, True if held by a live process,
    or None if the lock state cannot be safely determined.
    """
    if not SID.fullmatch(sid):
        return None
    lock_path = home / "presence" / f"{sid}.lock"
    if not lock_path.exists():
        return False
    try:
        with lock_path.open("rb") as stream:
            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            try:
                return False
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
    except FileNotFoundError:
        return False
    except OSError:
        return None


def _wal_has_bytes(database: Path) -> bool:
    wal = database.with_name(database.name + "-wal")
    try:
        return wal.is_file() and wal.stat().st_size > 0
    except OSError:
        return True


def _query_sqlite_state(uri: str, sid: str) -> str:
    with closing(sqlite3.connect(uri, uri=True, timeout=0.5)) as connection:
        has_steps = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='steps' LIMIT 1"
        ).fetchone()
        if has_steps is None:
            return "incomplete_shell"
        if connection.execute("SELECT 1 FROM steps LIMIT 1").fetchone() is None:
            return "empty_shell"
        has_meta = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='trajectory_meta' LIMIT 1"
        ).fetchone()
        if (has_meta is None
                or connection.execute("SELECT 1 FROM trajectory_meta WHERE cascade_id=? LIMIT 1", (sid,)).fetchone() is None):
            return "mismatched_meta"
        return "valid"


def _inspect_sqlite_db(database: Path, sid: str) -> str:
    """Inspect a trajectory SQLite file with WAL consistency and zero side-files on clean DBs."""
    try:
        if not database.is_file():
            return "missing"
        db_size = database.stat().st_size
    except OSError:
        return "busy_or_unreadable"

    wal_nonempty = _wal_has_bytes(database)
    if db_size == 0 and not wal_nonempty:
        return "empty_shell"

    try:
        if wal_nonempty:
            return _query_sqlite_state(database.as_uri() + "?mode=ro", sid)
        # Cleanly closed WAL DBs have no non-empty -wal; immutable=1 prevents
        # SQLite from creating -shm/-wal side-files or acquiring reader locks.
        state = _query_sqlite_state(database.as_uri() + "?mode=ro&immutable=1", sid)
        if _wal_has_bytes(database):
            return _query_sqlite_state(database.as_uri() + "?mode=ro", sid)
        return state
    except (sqlite3.Error, OSError):
        return "busy_or_unreadable"


def inspect_conversation_storage(sid: str, home: Path) -> dict:
    """Classify local storage for an Antigravity CLI conversation ID."""
    conversations = home / "conversations"
    database = conversations / f"{sid}.db"
    proto = conversations / f"{sid}.pb"
    try:
        pb_size = proto.stat().st_size if proto.is_file() else 0
    except OSError:
        pb_size = 0
    db_state = _inspect_sqlite_db(database, sid)
    if db_state == "valid":
        kind = "sqlite"
    elif db_state == "missing" and pb_size > 0:
        kind = "proto"
    elif db_state in {"empty_shell", "incomplete_shell"} and pb_size > 0:
        kind = "shadowed_proto"
    elif db_state in {"empty_shell", "incomplete_shell"}:
        kind = "empty_db_only"
    elif db_state == "busy_or_unreadable":
        kind = "unreadable"
    else:
        kind = "none"
    return {
        "sid": sid,
        "kind": kind,
        "db_state": db_state,
        "db_path": database,
        "pb_path": proto,
        "pb_size": pb_size,
    }


def quarantine_shadowed_db(sid: str, home: Path, environment=None) -> dict | None:
    """Move an empty/incomplete .db shell into backups when it shadows a non-empty .pb.

    Strictly conservative: requires unlocked native presence flock, zero active
    aipane session writers, WAL-consistent empty/incomplete DB confirmation, and
    a non-empty sibling .pb. Moves the main .db first via rename and never deletes.
    """
    if not SID.fullmatch(sid):
        return None
    storage = inspect_conversation_storage(sid, home)
    if storage["kind"] != "shadowed_proto":
        return None
    if presence_lock_held(sid, home) is not False:
        return None
    homes = {str(home)}
    try:
        homes.add(str(home.resolve()))
    except OSError:
        pass
    for candidate_home in homes:
        if session_writers(sid, candidate_home, environment):
            return None
    # Re-verify storage state immediately before rename to narrow TOCTOU.
    storage = inspect_conversation_storage(sid, home)
    if storage["kind"] != "shadowed_proto":
        return None

    db_path: Path = storage["db_path"]
    backups_root = home / "backups" / "orphaned-db"
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup_dir = backups_root / f"{stamp}-{sid}"
    if backup_dir.exists():
        backup_dir = backups_root / f"{stamp}-{sid}-{uuid.uuid4().hex[:6]}"
    backups_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    backup_dir.mkdir(mode=0o700, parents=True, exist_ok=True)

    moved: list[str] = []
    try:
        # Move main .db first: it is the sole file that makes agy pick Store 1 over .pb.
        db_path.replace(backup_dir / db_path.name)
        moved.append(db_path.name)
    except FileNotFoundError:
        try:
            backup_dir.rmdir()
        except OSError:
            pass
        return None

    for suffix in ("-wal", "-shm"):
        companion = db_path.with_name(db_path.name + suffix)
        try:
            if companion.exists():
                companion.replace(backup_dir / companion.name)
                moved.append(companion.name)
        except OSError:
            pass

    transcript = home / "brain" / sid / ".system_generated/logs/transcript.jsonl"
    try:
        has_transcript = transcript.is_file() and transcript.stat().st_size > 0
    except OSError:
        has_transcript = False
    return {
        "sid": sid,
        "backup_dir": str(backup_dir),
        "moved": moved,
        "db_state": storage["db_state"],
        "pb_size": storage["pb_size"],
        "has_transcript": has_transcript,
    }


def _uses_agy_launcher(args: list[str]) -> bool:
    if not args:
        return False
    executable = args[0]
    if Path(executable).name == "aipane-agy-launch":
        return True
    candidate = Path(executable).expanduser()
    if not candidate.is_absolute():
        found = shutil.which(executable)
        if not found:
            return False
        candidate = Path(found)
    try:
        if candidate.resolve().name == "aipane-agy-launch":
            return True
        if candidate.is_file() and candidate.stat().st_size <= 4096:
            text = candidate.read_text(encoding="utf-8", errors="ignore")
            if "aipane-agy-launch" in text:
                return True
    except OSError:
        pass
    return False


def session_exists(sid: str, cwd: str, home: Path, *, allow_shadowed: bool = False) -> bool:
    if not SID.fullmatch(sid) or not os.path.isabs(cwd):
        return False
    try:
        # The picker merges desktop, cloud and CLI entries. Require local CLI
        # trajectory storage (.db or fallback .pb) plus workspace ownership.
        storage = inspect_conversation_storage(sid, home)
        allowed = {"sqlite", "proto", "shadowed_proto"} if allow_shadowed else {"sqlite", "proto"}
        if storage["kind"] not in allowed:
            return False
        # Fresh conversations do not appear in the CLI's picker cache until a
        # later cache refresh. Keep the process-verified workspace binding too.
        binding = _read(session_path(sid, home))
        if binding and binding.get("version") == 1 and binding.get("home") == str(home.resolve()):
            return str(Path(binding.get("cwd", "")).resolve()) == str(Path(cwd).resolve())
        metadata = json.loads((home / "cache/conversation_metadata.json").read_text())
        item = metadata["conversations"][sid]
        summary = item["summary"]
        paths = summary.get("WorkspaceURIs") or summary.get("workspaces") or summary.get("workspacePaths") or []
        if isinstance(paths, list):
            paths = [p.get("workspaceFolderAbsoluteUri") or p.get("workspacePath") or p.get("path")
                     if isinstance(p, dict) else p for p in paths]
        normalized = {str(Path(unquote(urlparse(p).path) if p.startswith("file://") else p).resolve()) for p in paths if isinstance(p, str)}
        if (item.get("is_internal") is True or summary.get("Internal") is True
                or summary.get("AppDataDir") != "antigravity-cli"
                or str(Path(cwd).resolve()) not in normalized):
            return False
        return True
    except (OSError, ValueError, TypeError, KeyError, sqlite3.Error):
        return False


def resume_command(sid: str, cwd: str, metadata: dict, base="agy") -> str:
    home = Path(metadata.get("agy_home") or cli_home())
    args = shlex.split(base)
    if not session_exists(sid, cwd, home, allow_shadowed=_uses_agy_launcher(args)):
        raise ValueError("Antigravity session is missing, internal, or belongs to another workspace")
    for flag, key, allowed in [("--effort", "agy_effort", EFFORTS), ("--mode", "agy_mode", MODES)]:
        value = metadata.get(key)
        if value:
            if value not in allowed:
                raise ValueError(f"invalid Antigravity {key}")
            if key == "agy_effort" and FIXED_VARIANT_SUFFIX.search(str(metadata.get("agy_model", ""))):
                # Model variants whose selector ID already encodes effort reject --effort.
                continue
            args.extend([flag, value])
    for flag, key in [("--model", "agy_model"), ("--agent", "agy_agent")]:
        value = metadata.get(key)
        if value:
            if not isinstance(value, str) or len(value) > 256 or any(ord(c) < 32 for c in value):
                raise ValueError(f"invalid Antigravity {key}")
            args.extend([flag, value])
    return shlex.join([*args, "--conversation", sid])


def audit_agy_conversations(
    home: Path,
    *,
    log_days: int = 7,
    orphan_min_age_seconds: int = 600,
    now: float | None = None,
    environment=None,
) -> dict:
    """Read-only health audit of Antigravity CLI conversation storage and recent lock logs."""
    current_time = time.time() if now is None else now
    conversations = home / "conversations"
    sids: set[str] = set()
    if conversations.is_dir():
        for suffix in ("*.db", "*.pb"):
            for path in conversations.glob(suffix):
                if SID.fullmatch(path.stem):
                    sids.add(path.stem)

    counts = {
        "sqlite": 0,
        "proto": 0,
        "shadowed": 0,
        "suspected_orphans": 0,
        "unreadable": 0,
        "total": len(sids),
    }
    shadowed: list[dict] = []
    suspected_orphans: list[dict] = []
    homes = {str(home)}
    try:
        homes.add(str(home.resolve()))
    except OSError:
        pass

    for sid in sorted(sids):
        storage = inspect_conversation_storage(sid, home)
        kind = storage["kind"]
        if kind == "sqlite":
            counts["sqlite"] += 1
        elif kind == "proto":
            counts["proto"] += 1
        elif kind == "shadowed_proto":
            counts["shadowed"] += 1
            try:
                db_size = storage["db_path"].stat().st_size
            except OSError:
                db_size = 0
            shadowed.append({
                "sid": sid,
                "db_state": storage["db_state"],
                "db_size": db_size,
                "pb_size": storage["pb_size"],
                "db_path": str(storage["db_path"]),
                "pb_path": str(storage["pb_path"]),
            })
        elif kind == "empty_db_only":
            transcript = home / "brain" / sid / ".system_generated/logs/transcript.jsonl"
            try:
                has_transcript = transcript.is_file() and transcript.stat().st_size > 0
                st = storage["db_path"].stat()
                age = max(0.0, current_time - st.st_mtime)
                db_size = st.st_size
            except OSError:
                continue
            if has_transcript or age < orphan_min_age_seconds:
                continue
            if presence_lock_held(sid, home) is not False:
                continue
            if any(session_writers(sid, candidate_home, environment) for candidate_home in homes):
                continue
            counts["suspected_orphans"] += 1
            suspected_orphans.append({
                "sid": sid,
                "db_state": storage["db_state"],
                "db_size": db_size,
                "age_seconds": int(age),
                "db_path": str(storage["db_path"]),
            })
        elif kind == "unreadable":
            counts["unreadable"] += 1

    log_dir = home / "log"
    cutoff = current_time - max(1, log_days) * 86400
    lock_events = {
        "days": max(1, log_days),
        "scanned_logs": 0,
        "database_locked": 0,
        "another_opener": 0,
        "new_conversations": 0,
    }
    if log_dir.is_dir():
        for log_file in sorted(log_dir.glob("cli-*.log")):
            try:
                if log_file.stat().st_mtime < cutoff:
                    continue
                lock_events["scanned_logs"] += 1
                with log_file.open("r", encoding="utf-8", errors="ignore") as stream:
                    for line in stream:
                        if "Creating new cascade trajectory" in line:
                            lock_events["new_conversations"] += 1
                        elif "failed to create persistent trajectory in store" in line and "database is locked" in line:
                            lock_events["database_locked"] += 1
                        elif "failed to create conversation:" in line and "its database was initialized by another opener" in line:
                            lock_events["another_opener"] += 1
            except OSError:
                continue

    return {
        "counts": counts,
        "shadowed": shadowed,
        "suspected_orphans": suspected_orphans,
        "lock_events": lock_events,
    }


def recovery_metadata(record: dict) -> dict:
    model = record.get("model") or {}
    return {"agy_home": record["agy_home"], "agy_model": model.get("id", ""),
            "agy_effort": model.get("effort", ""), "agy_mode": record.get("cycle_mode", ""),
            "agy_agent": record.get("agent_name", ""), "agy_route": record.get("route", "")}


def session_writers(sid: str, home: str, environment=None) -> set[int]:
    writers = set()
    for path in (state_dir(environment) / "runtime").glob("*.json"):
        record = _read(path)
        if not record or record.get("session_id") != sid or record.get("agy_home") != home:
            continue
        process = process_info(record.get("pid", 0))
        if (process and process.get("executable") == "agy" and process.get("foreground")
                and process.get("started_at") == record.get("started_at") and process.get("tty") == record.get("tty")):
            writers.add(process["pid"])
    return writers


def session_path(sid: str, home: Path, environment=None) -> Path:
    key = hashlib.sha256(f"{home.resolve()}\0{sid}".encode()).hexdigest()
    return state_dir(environment) / "sessions" / f"{key}.json"


class AgyIntegration:
    def __init__(self, environment=None, *, reader=process_info, producer=None, pane_matches=None):
        self.environment = dict(os.environ if environment is None else environment)
        self.reader = reader
        self.producer = producer or (lambda: producer_process(reader))
        tmux = self.environment.get("TMUX", "").rsplit(",", 2)
        self.socket, self.server = tmux[:2] if len(tmux) == 3 else ("", "")
        self.pane = self.environment.get("TMUX_PANE", "")
        self.pane_matches = pane_matches or self._owns_pane

    def _owns_pane(self, process):
        if not self.pane:
            return True
        command = shlex.split(self.environment.get("AIPANE_TMUX", "")) or ["tmux", "-S", self.socket]
        try:
            probe = subprocess.run([*command, "display-message", "-p", "-t", self.pane,
                                    "#{pane_tty}\x1f#{pane_current_command}\x1f#{socket_path}\x1f#{pid}"],
                                   env=self.environment, capture_output=True, text=True, timeout=2)
            fields = probe.stdout.strip().split("\x1f")
            return probe.returncode == 0 and fields == ["/dev/" + process["tty"], "agy", self.socket, self.server]
        except (OSError, subprocess.SubprocessError):
            return False

    def _candidate(self, record, event, token, **extra):
        return {"event": event, "session_id": record["session_id"],
                "generation": record["generation"], "token": token,
                "cwd": record["cwd"], "title": record.get("title", ""),
                "transcript_path": record.get("transcript_path", ""),
                "runtime_path": str(self.path), "pane": self.pane,
                **extra}

    def _complete(self, record):
        if not record.get("active") or not completion_ready(record):
            return []
        stop = record["stop"]
        reason = str(stop.get("terminationReason", "")).lower()
        event = "failed" if stop.get("error") or reason in {"error", "max_steps_exceeded", "max_steps"} else (
            "interrupted" if any(word in reason for word in ("cancel", "interrupt", "user_stop")) else "completed")
        token = f"turn:{record['generation']}"
        return [self._candidate(record, event, token, error=str(stop.get("error") or "")[:1000])]

    def status(self, payload: dict) -> tuple[dict | None, list[dict]]:
        process = self.producer()
        if not process or process.get("executable") != "agy" or not process.get("foreground") or not self.pane_matches(process):
            return None, []
        sid = payload.get("conversation_id") or payload.get("session_id") or ""
        cwd = payload.get("cwd")
        if not isinstance(sid, str) or (sid and not SID.fullmatch(sid)) or not isinstance(cwd, str) or not os.path.isabs(cwd):
            return None, []
        key = self.pane or f"process:{process['pid']}:{process['started_at']}"
        self.path = runtime_path(key, self.socket, self.server, self.environment)
        with runtime_lock(self.path):
            prior = _read(self.path) or {}
            if prior.get("pid") and prior["pid"] != process["pid"]:
                owner = self.reader(prior["pid"])
                if (owner and owner.get("started_at") == prior.get("started_at")
                        and owner.get("foreground") and owner.get("tty") == process.get("tty")):
                    return None, []
            same_process = prior.get("pid") == process["pid"] and prior.get("started_at") == process["started_at"]
            same = same_process and (prior.get("session_id") == sid or (
                not prior.get("session_id") and sid and (prior.get("active") or sid in prior.get("pending", {}))))
            record = dict(prior) if same else {"generation": uuid.uuid4().hex, "active": False}
            pending = record.pop("pending", {}).get(sid, {})
            if pending.get("started"):
                record["active"] = True
            if pending.get("stop"):
                record["stop"] = pending["stop"]
            before_state = record.get("state")
            # v1.5.9 can report the desktop brain path. Use the selected CLI conversation.
            transcript = (cli_home(self.environment) / "brain" / sid / ".system_generated/logs/transcript.jsonl"
                          if sid else None)
            phase = status_phase(payload)
            if phase == "reviewing" and not sid and not record.get("active"):
                phase = "unknown"
            if (record.get("activity_version", 1) == 1 and record.get("active")
                    and record.get("status_phase") == "reviewing" and phase == "reviewing"
                    and not record.get("stop") and transcript
                    and final_reply_predates_process(transcript, process["started_at"])):
                record["active"] = False
            record["status_phase"] = phase
            state = turn_activity(phase, record.get("stop") or {}, active=record.get("active", False))
            if state == "busy" and (not record.get("active") or before_state == "idle"):
                record.update(active=True, generation=uuid.uuid4().hex)
                record.pop("stop", None)
            model = payload.get("model")
            model = {k: v for k,v in model.items() if k in {"id", "effort", "display_name"} and isinstance(v, str)} if isinstance(model, dict) else {}
            env_route = str(self.environment.get("AIPANE_AGY_ROUTE") or "").strip().lower()
            if env_route in ROUTES:
                route = env_route
            elif str(self.environment.get("AGY_ADC_AUTH") or "").strip().lower() == "true":
                route = "gcp"
            else:
                route = ""
            record.update(version=1, activity_version=2, pid=process["pid"], started_at=process["started_at"],
                          tty=process["tty"], pane=self.pane, socket=self.socket, server=self.server,
                          session_id=sid, cwd=cwd, agy_home=str(cli_home(self.environment)),
                          state=state, agent_state=payload.get("agent_state"),
                          has_background_tasks=bool(payload.get("task_count")),
                          title=str(payload.get("conversation_title") or "")[:500],
                          transcript_path=str(transcript) if transcript else "",
                          model=model, route=route, cycle_mode=payload.get("cycle_mode", ""),
                          agent_name=(payload.get("agent") or {}).get("name", "") if isinstance(payload.get("agent", {}), dict) else "",
                          updated_at=time.time(), sequence=int(record.get("sequence", 0)) + 1)
            events = []
            approval = payload.get("tool_confirmation_pending") is True
            if approval and not record.get("approval"):
                events.append(self._candidate(record, "approval", "approval:" + uuid.uuid4().hex))
            record["approval"] = approval
            if payload.get("agent_state") == "error" and before_state == "busy" and not record.get("stop"):
                record["stop"] = {"terminationReason": "error"}
            events.extend(self._complete(record))
            _write(self.path, record)
            if sid:
                binding = session_path(sid, cli_home(self.environment), self.environment)
                with runtime_lock(binding):
                    _write(binding, {
                        "version": 1,
                        "session_id": sid,
                        "home": str(cli_home(self.environment).resolve()),
                        "cwd": cwd,
                        **({"model": model["id"]} if model.get("id") else {}),
                        **({"effort": model["effort"]} if model.get("effort") else {}),
                        **({"route": route} if route else {}),
                    })
            if isinstance(payload.get("quota"), dict) and payload["quota"]:
                try:
                    from agy_catalog import atomic_write_json, normalize_quota_payload, quota_snapshot_path
                    snapshot = normalize_quota_payload(payload)
                    if snapshot is not None:
                        atomic_write_json(quota_snapshot_path(self.environment), snapshot)
                except Exception:
                    pass
            return record, events

    def hook(self, event: str, payload: dict) -> tuple[dict | None, list[dict]]:
        if not self.pane:
            process = self.producer()
            if not process:
                return None, []
            key = f"process:{process['pid']}:{process['started_at']}"
        else:
            key = self.pane
        self.path = runtime_path(key, self.socket, self.server, self.environment)
        with runtime_lock(self.path):
            record = _read(self.path)
            if not record:
                return None, []
            process = self.reader(record.get("pid", 0))
            if (not process or process.get("started_at") != record.get("started_at")
                    or process.get("executable") != "agy" or process.get("tty") != record.get("tty")
                    or not process.get("foreground")):
                return None, []
            sid = payload.get("conversationId")
            transcript = payload.get("transcriptPath")
            expected = cli_home(self.environment) / "brain" / str(sid)
            if not isinstance(transcript, str) or not Path(transcript).expanduser().resolve().is_relative_to(expected.resolve()):
                return None, []
            if sid != record.get("session_id"):
                # First-turn hooks may finish before statusLine publishes the
                # newly allocated ID. Do not bind a hook's ID to the pane: hold
                # only lifecycle metadata until statusLine selects that exact ID.
                paths = payload.get("workspacePaths", [])
                if (record.get("session_id") or not isinstance(sid, str) or not SID.fullmatch(sid)
                        or not isinstance(paths, list) or record.get("cwd") not in paths):
                    return None, []
                pending = record.setdefault("pending", {})
                if len(pending) >= 8 and sid not in pending:
                    return None, []
                waiting = pending.setdefault(sid, {})
                if event == "PreInvocation":
                    waiting["started"] = True
                    record.update(active=True, state="busy")
                elif event == "Stop":
                    waiting["stop"] = {k: payload[k] for k in ("terminationReason", "error", "fullyIdle", "executionNum", "continuationActive") if k in payload}
                _write(self.path, record)
                return record, []
            events = []
            if event == "PreInvocation":
                if not record.get("active") or record.get("state") == "idle":
                    record.update(active=True, generation=uuid.uuid4().hex)
                record["state"] = "busy"
                record.pop("stop", None)
                record.pop("status_phase", None)
            elif event == "Stop":
                record["stop"] = {k: payload[k] for k in ("terminationReason", "error", "fullyIdle", "executionNum", "continuationActive") if k in payload}
                if "status_phase" in record:
                    record["state"] = turn_activity(record["status_phase"], record["stop"], active=record.get("active", False))
                events.extend(self._complete(record))
            elif event == "PreToolUse":
                tool = payload.get("toolCall") or {}
                if isinstance(tool, dict) and tool.get("name") == "ask_question":
                    arguments = tool.get("args") or {}
                    questions = arguments.get("questions", []) if isinstance(arguments, dict) else []
                    questions = questions if isinstance(questions, list) else []
                    question = questions[0].get("question", "") if questions and isinstance(questions[0], dict) else ""
                    step = tool.get("id") or payload.get("stepId")
                    if not step and type(payload.get("stepIdx")) is int:
                        step = str(payload["stepIdx"])
                    events.append(self._candidate(record, "question", "question:" + str(step or uuid.uuid4().hex), question=question))
            record.update(updated_at=time.time(), sequence=int(record.get("sequence", 0)) + 1)
            _write(self.path, record)
            return record, events


def notification_current(payload: dict, environment=None) -> bool:
    path = Path(payload.get("runtime_path", ""))
    if path.parent != state_dir(environment) / "runtime":
        return False
    record = _read(path)
    if not record or record.get("session_id") != payload.get("session_id") or record.get("generation") != payload.get("generation"):
        return False
    process = process_info(record.get("pid", 0))
    if not process or process.get("started_at") != record.get("started_at") or not process.get("foreground"):
        return False
    if payload.get("event") in {"completed", "failed", "interrupted"}:
        return completion_ready(record)
    return True


if __name__ == "__main__":
    try:
        if sys.argv[1] == "resume":
            print(resume_command(sys.argv[2], sys.argv[3], json.loads(sys.argv[4]), sys.argv[5]))
        else:
            raise ValueError("unknown operation")
    except (ValueError, IndexError) as error:
        print(f"agy session: {error}", file=sys.stderr)
        raise SystemExit(1)
