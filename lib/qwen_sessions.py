"""Read-only Qwen Code session identity and conservative recovery checks.

Qwen 0.25.3 publishes a per-PID registry and a per-session runtime sidecar.
Neither argv nor aipane's launch binding follows an in-process session switch.
Unknown schemas, ambiguous processes and unavailable evidence fail closed.
"""

from __future__ import annotations

from datetime import datetime
import json
import math
import os
from pathlib import Path
import re
import shlex
import socket
import stat
import subprocess
import sys
import time


SID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}")


def namespace(environment=None) -> tuple[Path, Path]:
    env = os.environ if environment is None else environment
    user_home = env.get("HOME") or str(Path.home())

    def absolute(value):
        if value == "~" or value.startswith("~/"):
            value = user_home + value[1:]
        return Path(os.path.abspath(value))

    home = absolute(env.get("QWEN_HOME") or str(Path(user_home) / ".qwen"))
    runtime = absolute(env.get("QWEN_RUNTIME_DIR") or str(home))
    return home, runtime


def is_qwen_command_line(command: str) -> bool:
    try:
        args = shlex.split(command)
    except ValueError:
        return False
    if not args:
        return False
    if Path(args[0]).name == "qwen":
        return True
    if Path(args[0]).name != "node" or len(args) < 2:
        return False
    # Only the script position counts; text in --eval or a prompt is not an
    # executable. Fail closed for unrecognized Node launch options.
    script = args[1]
    return Path(script).name == "qwen" or (
        "/@qwen-code/qwen-code/" in script
        and Path(script).name in {"cli.js", "cli-entry.js"}
    )


def foreground_processes(pane_tty: str) -> list[dict]:
    tty = pane_tty.removeprefix("/dev/")
    if not tty or tty.startswith("-"):
        return []
    try:
        result = subprocess.run(
            ["ps", "-t", tty, "-o", "pid=", "-o", "lstart=", "-o", "stat=", "-o", "command="],
            capture_output=True, text=True, timeout=2, check=False,
            env={**os.environ, "LC_ALL": "C"},
        )
        if result.returncode:
            return []
        processes = []
        for line in result.stdout.splitlines():
            fields = line.split(None, 7)
            if len(fields) != 8 or "+" not in fields[6]:
                continue
            if not is_qwen_command_line(fields[7]):
                continue
            started = datetime.strptime(" ".join(fields[1:6]), "%a %b %d %H:%M:%S %Y").timestamp()
            processes.append({"pid": int(fields[0]), "started_at": started})
        return processes
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return []


def _read_record(path: Path) -> dict | None:
    try:
        info = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o022 or info.st_size > 16384):
            return None
        record = json.loads(path.read_text(encoding="utf-8"))
        return record if isinstance(record, dict) else None
    except (OSError, ValueError):
        return None


def _timestamp(value, started: float, scale=1) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and started <= value / scale <= time.time() + 5)


def _process_identity(record: dict, process: dict) -> bool:
    if not _timestamp(record.get("startedAt"), process["started_at"], 1000):
        return False
    if sys.platform != "linux":
        # Qwen's procStart is null on macOS. The registration time must be
        # within this process lifetime, excluding records left by a reused PID.
        return record.get("procStart") is None and record.get("pidNs") is None
    try:
        pid = process["pid"]
        boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        token = f"{boot}:{fields[19]}"
        return (record.get("procStart") == token
                and record.get("pidNs") == Path(f"/proc/{pid}/ns/pid").stat().st_ino)
    except (OSError, IndexError):
        return False


def current_session(*, pane_tty: str, environment=None) -> dict | None:
    """Resolve exactly one foreground Qwen process on this pane's TTY."""
    processes = foreground_processes(pane_tty)
    if len(processes) != 1:
        return None
    process = processes[0]
    home, runtime = namespace(environment)
    record = _read_record(home / "sessions" / f"{process['pid']}.json")
    if not record:
        return None
    sid, cwd = record.get("sessionId"), record.get("cwd")
    if (type(record.get("schemaVersion")) is not int or record["schemaVersion"] != 1
            or type(record.get("pid")) is not int or record["pid"] != process["pid"]
            or record.get("kind", "tui") != "tui"
            or not isinstance(sid, str) or not SID.fullmatch(sid)
            or not isinstance(cwd, str) or not os.path.isabs(cwd)
            or any(ord(c) < 32 for c in cwd)
            or not _process_identity(record, process)):
        return None
    # Return only the fields aipane needs, never IPC tokens or other metadata.
    return {**process, "session_id": sid, "cwd": cwd,
            "qwen_home": str(home), "qwen_runtime_dir": str(runtime)}


def chats_dir(cwd: str, environment=None) -> Path:
    # Qwen Storage uses the exact project spelling, not a realpath hash.
    return namespace(environment)[1] / "projects" / re.sub(r"[^a-zA-Z0-9]", "-", cwd) / "chats"


def session_exists(sid: str, cwd: str, environment=None) -> bool:
    """Require conversation content in the exact project's active transcript.

    This is a conservative preflight, not a replacement for Qwen's history
    loader. Runtime verification remains mandatory after launching --resume.
    """
    if not SID.fullmatch(sid) or not os.path.isabs(cwd):
        return False
    path = chats_dir(cwd, environment) / f"{sid}.jsonl"
    try:
        content = False
        first = True
        with path.open(encoding="utf-8") as stream:
            for line in stream:
                if not line.strip():
                    continue
                record = json.loads(line)
                if not isinstance(record, dict) or record.get("sessionId") != sid:
                    return False
                if first:
                    # Native worktree records may name a child worktree while
                    # the transcript remains owned by its original project.
                    record_cwd = record.get("cwd")
                    if not isinstance(record_cwd, str):
                        return False
                    worktree_root = record_cwd.rpartition("/.qwen/worktrees/")[0]
                    if cwd not in (record_cwd, worktree_root):
                        return False
                    first = False
                if record.get("type") in {"user", "assistant"}:
                    message = record.get("message")
                    if (not isinstance(record.get("uuid"), str) or not record["uuid"]
                            or not isinstance(message, dict)
                            or message.get("role") not in {"user", "model"}
                            or not isinstance(message.get("parts"), list)
                            or any(not isinstance(part, dict) for part in message["parts"])):
                        return False
                    content = content or bool(message["parts"])
        return content
    except (OSError, ValueError, TypeError):
        return False


def session_loaded(*, pane_tty: str, sid: str, cwd: str, environment=None) -> bool:
    """Match the live process registry AND its interactive runtime sidecar."""
    current = current_session(pane_tty=pane_tty, environment=environment)
    if not current or current["session_id"] != sid or current["cwd"] != cwd:
        return False
    record = _read_record(chats_dir(cwd, environment) / f"{sid}.runtime.json")
    return bool(
        record and type(record.get("schema_version")) is int and record["schema_version"] == 1
        and type(record.get("pid")) is int and record["pid"] == current["pid"]
        and record.get("session_id") == sid and record.get("work_dir") == cwd
        and record.get("hostname") == socket.gethostname()
        and _timestamp(record.get("started_at"), current["started_at"])
    )


if __name__ == "__main__":
    raise SystemExit(0 if len(sys.argv) == 3 and session_exists(*sys.argv[1:]) else 1)
