"""Read the dsh TUI's selected session without guessing from launch argv."""

from __future__ import annotations

import json
import os
import subprocess
import stat
import shlex
import time
from pathlib import Path


def is_dsh_command_line(command: str) -> bool:
    """Recognize the executable/script position, never a later prompt argument."""
    try:
        argv = shlex.split(command)
    except ValueError:
        return False
    if not argv:
        return False
    script = argv[0]
    if Path(script).name == "node":
        script = argv[1] if len(argv) > 1 else ""
        # Value-taking Node flags (-e, -r, …) make later tokens ambiguous.
        if script.startswith("-"):
            return False
    return (Path(script).name in {"dsh", "dsh-tui", "dsh-tui.js", "dst"}
            or "/@deepseek-ai/dsh/" in script
            or "/@deepseek-harness-tui/dsh-tui/" in script)


def matches_session(record: dict | None, *, session_id: str, cwd: str,
                    dsh_home: str = "", session_root: str = "") -> bool:
    """Match the selected conversation, including its storage namespace."""
    if record is None or not session_id or record.get("session_id") != session_id:
        return False
    for field, expected in (("cwd", cwd), ("dsh_home", dsh_home), ("session_root", session_root)):
        if expected and (not isinstance(record.get(field), str) or not record[field]
                         or os.path.realpath(record[field]) != os.path.realpath(expected)):
            return False
    return True


def current_session(*, pane_id: str, socket_path: str, server_pid: str,
                    pane_tty: str = "", environment=None) -> dict | None:
    environment = os.environ if environment is None else environment
    home = Path(environment.get("HOME", str(Path.home())))
    root = Path(environment.get("DSH_HOME") or home / ".dsh") / "aipane/sessions"
    matches = []
    for path in root.glob("*.json"):
        try:
            info = path.lstat()
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_mode & 0o022 or info.st_size > 16384):
                continue
            record = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(record, dict):
                continue
            pid = record.get("pid")
            if (type(pid) is not int or pid <= 0 or path.stem != str(pid)
                    or record.get("version") != 1
                    or record.get("pane_id") != pane_id
                    or record.get("socket") != socket_path
                    or str(record.get("server_pid")) != str(server_pid)
                    or type(record.get("updated_at")) not in (int, float)
                    or not 0 <= time.time() * 1000 - record["updated_at"] <= 10000
                    or not isinstance(record.get("session_id"), str)
                    or not record["session_id"]
                    or not isinstance(record.get("cwd"), str)
                    or not Path(record["cwd"]).is_absolute()):
                continue
            os.kill(pid, 0)
            process = subprocess.run(
                ["ps", "-p", str(pid), "-o", "lstart=", "-o", "tty="],
                capture_output=True, text=True, timeout=2, check=True,
                env={**os.environ, "LC_ALL": "C"},
            ).stdout.strip().rsplit(None, 1)
            if len(process) != 2 or process[0].strip() != record.get("process_started"):
                continue
            if pane_tty and process[1] != pane_tty.removeprefix("/dev/"):
                continue
            matches.append(record)
        except (OSError, ValueError, TypeError, subprocess.SubprocessError):
            continue
    # Multiple hosts in a pane (or a half-finished handoff) are ambiguous.
    return matches[0] if len(matches) == 1 else None
