"""Read-only evidence that a local Codex process has loaded a conversation."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path


def session_loaded(pid: int, sid: str, codex_home: Path) -> bool:
    """Require the requested rollout to be open for writing by this process.

    A startup preview can read history before account/read finishes. Neither
    that read handle, the resume argv, nor an old pane binding proves recovery.
    Missing lsof support or unrecognized storage fails closed, leaving pending.
    """
    if pid <= 0 or not sid:
        return False
    try:
        result = subprocess.run(
            ["lsof", "-nP", "-a", "-p", str(pid), "-F", "fan"],
            capture_output=True, text=True, timeout=2, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    if result.returncode != 0:
        return False
    roots = [(codex_home / name).resolve() for name in ("sessions", "archived_sessions")]
    writable = False
    for line in result.stdout.splitlines():
        if line.startswith("f"):
            writable = False
        elif line.startswith("a"):
            writable = line[1:] in {"w", "u"}
        elif line.startswith("n") and writable:
            path = Path(line[1:])
            try:
                path = path.resolve()
                if path.suffix != ".jsonl" or not any(path.is_relative_to(root) for root in roots):
                    continue
                with path.open(encoding="utf-8") as rollout:
                    metadata = json.loads(rollout.readline(65536))
                if (isinstance(metadata, dict) and metadata.get("type") == "session_meta"
                        and isinstance(metadata.get("payload"), dict)
                        and metadata["payload"].get("id") == sid):
                    return True
            except (OSError, ValueError):
                continue
    return False
