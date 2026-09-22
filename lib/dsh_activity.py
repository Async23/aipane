"""Read a live dsh host's aggregate activity without replaying conversation data."""

from __future__ import annotations

import json
import os
import stat
import time
from pathlib import Path
from typing import Mapping


def read_runtime(
    reference: object,
    *,
    pane_id: str,
    socket_path: str,
    server_pid: str,
    process_id: object,
    now_ms: int | None = None,
) -> dict[str, object] | None:
    """Require the caller's independently verified process and exact pane identity."""
    if not isinstance(reference, Mapping):
        return None
    pid = reference.get("pid")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0 or pid != process_id:
        return None
    instance = reference.get("instance")
    path_value = reference.get("path")
    if not isinstance(instance, str) or not instance or not isinstance(path_value, str):
        return None
    path = Path(path_value)
    if not path.is_absolute() or path.name != f"{pid}.json":
        return None
    try:
        metadata = path.lstat()
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid()
                or metadata.st_mode & 0o022 or metadata.st_size > 256 * 1024):
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(value, dict):
        return None
    sequence = value.get("sequence")
    minimum_sequence = reference.get("sequence", 0)
    updated = value.get("updated_at")
    now = now_ms if now_ms is not None else time.time_ns() // 1_000_000
    if (value.get("version") != 1 or value.get("pid") != pid
            or value.get("instance") != instance
            or value.get("pane_id") != pane_id
            or value.get("socket") != socket_path
            or str(value.get("server_pid", "")) != server_pid
            or not isinstance(sequence, int) or isinstance(sequence, bool)
            or not isinstance(minimum_sequence, int) or isinstance(minimum_sequence, bool)
            or sequence < minimum_sequence or sequence < 0
            or not isinstance(updated, int) or isinstance(updated, bool)
            or not -1000 <= now - updated <= 10_000):
        return None
    agents = value.get("agents")
    if not isinstance(agents, list) or any(
        not isinstance(agent, dict) or agent.get("status") not in {"running", "idle"}
        for agent in agents
    ):
        return None
    expected = "busy" if any(agent["status"] == "running" for agent in agents) else "idle"
    if value.get("state") != expected:
        return None
    return value
