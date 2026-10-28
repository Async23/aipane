"""Bind session identities to the exact tmux dump that captured them.

Coordinates only have meaning inside one snapshot. A global 'latest' binding
map is useful for inspection, but can never authorize restoration of a dump.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import uuid


class SnapshotError(RuntimeError):
    pass


def atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(payload, output, ensure_ascii=False, indent=2)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


SNAPSHOT_MARKER = b"aipane_sessions\t"


def _read_snapshot(dump: Path) -> tuple[bytes, dict | None]:
    try:
        raw = dump.read_bytes()
    except OSError as error:
        raise SnapshotError(f"cannot read recovery dump {dump}: {error}") from error
    lines = raw.splitlines(keepends=True)
    records = [line for line in lines if line.startswith(SNAPSHOT_MARKER)]
    body = b"".join(line for line in lines if not line.startswith(SNAPSHOT_MARKER))
    if not records:
        return body, None
    try:
        payload = json.loads(records[0][len(SNAPSHOT_MARKER):])
    except (ValueError, UnicodeError) as error:
        raise SnapshotError(f"cannot read paired session snapshot {dump}: {error}") from error
    if (len(records) != 1 or not isinstance(payload, dict) or payload.get("version") != 1
            or not isinstance(payload.get("bindings"), dict)
            or any(not isinstance(key, str) or not isinstance(value, dict)
                   for key, value in payload["bindings"].items())):
        raise SnapshotError(f"invalid paired session snapshot {dump}")
    if payload.get("sha256") != hashlib.sha256(body).hexdigest():
        raise SnapshotError("session snapshot does not match the requested dump")
    return body, payload


def capture_snapshot(dump: Path, bindings: dict, server: tuple[str, str]) -> None:
    """Embed identities so tmux-resurrect retains session-only changes too."""
    dump = dump.resolve(strict=True)
    if len(server) != 2 or not all(server):
        raise SnapshotError("cannot capture session identities without a tmux server identity")
    body, previous = _read_snapshot(dump)
    if body and not body.endswith(b"\n"):
        body += b"\n"
    payload = {"version": 1, "sha256": hashlib.sha256(body).hexdigest(),
               "server": list(server), "bindings": bindings}
    if previous is not None:
        if previous != payload:
            raise SnapshotError("session identities changed for an existing dump; save a new snapshot")
        return
    temporary = dump.with_name(f".{dump.name}.{uuid.uuid4().hex}.tmp")
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as output:
            output.write(body + SNAPSHOT_MARKER + json.dumps(payload, ensure_ascii=False, sort_keys=True).encode() + b"\n")
            output.flush()
            os.fsync(output.fileno())
        temporary.replace(dump)
    finally:
        temporary.unlink(missing_ok=True)


def snapshot_bindings(dump: Path) -> dict:
    _, payload = _read_snapshot(dump)
    return payload["bindings"] if payload is not None else {}


def has_snapshot_metadata(dump: Path) -> bool:
    return _read_snapshot(dump)[1] is not None


def main() -> int:
    parser = argparse.ArgumentParser(description="Read identities paired with a tmux recovery dump")
    parser.add_argument("dump", type=Path)
    args = parser.parse_args()
    try:
        bindings = snapshot_bindings(args.dump)
        fields = ("dsh_home", "session_root", "cwd", "qwen_home", "qwen_runtime_dir", "codex_home")
        rows = []
        for coordinate, record in bindings.items():
            values = [coordinate, str(record.get("sid", "")), str(record.get("tool", "")),
                      *(str(record.get(key, "")) for key in fields)]
            if any("\x1f" in value or "\n" in value or "\r" in value for value in values):
                raise SnapshotError("session identity contains a record separator")
            rows.append("\x1f".join(values))
    except SnapshotError as error:
        print(f"ai-restore: {error}", file=sys.stderr)
        return 1
    for row in rows:
        print(row)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
