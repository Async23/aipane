"""Durable recovery intent, keyed by server incarnation and stable pane ID.

Coordinates are display data. Every removed intent receives an explicit terminal
resolution in the same atomic write; unrelated servers are preserved verbatim.
Mutations run under the recovery engine's file lock.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import time
from typing import Any

from recovery_plan import RecoveryPlanError, TOOL_TAGS, session_identity
from recovery_snapshot import atomic_json


class RecoveryStateError(RecoveryPlanError):
    pass


def pane_key(item: dict) -> tuple[str, str, str]:
    return (str(item.get("socket_path", "")), str(item.get("server_pid", "")),
            str(item.get("pane_id") or "coordinate:" + item["target"]))


def recovery_id(item: dict) -> str:
    identity = session_identity(item)
    value = (*pane_key(item), identity.tool, identity.store, identity.sid or item["command"])
    return hashlib.sha256(json.dumps(value).encode()).hexdigest()[:16]


class PendingStore:
    def __init__(self, path: Path, max_age: float, server: tuple[str, str] | None):
        self.path, self.max_age, self.server = path, max_age, server

    def _read(self) -> dict:
        if not self.path.exists():
            return {"version": 1, "items": [], "history": []}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise RecoveryStateError(f"cannot read pending recovery {self.path}: {error}") from error
        if not isinstance(payload, dict) or payload.get("version") not in (1, 2):
            raise RecoveryStateError(f"unsupported pending recovery format: {self.path}")
        if not isinstance(payload.get("items"), list) or not isinstance(payload.get("history", []), list):
            raise RecoveryStateError(f"invalid pending recovery items: {self.path}")
        for raw in payload["items"]:
            try:
                valid = (isinstance(raw, dict) and float(raw.get("created_at", 0)) > 0
                         and raw.get("restorable") is True and raw.get("tool") in TOOL_TAGS
                         and all(isinstance(raw.get(key), str) and raw[key]
                                 for key in ("target", "tool", "command", "cwd", "kind")))
            except (TypeError, ValueError):
                valid = False
            if not valid:
                raise RecoveryStateError(f"invalid pending recovery identity: {self.path}")
        return payload

    def read_all(self) -> list[dict[str, Any]]:
        return self._read()["items"]

    def owns(self, item: dict) -> bool:
        return self.server is not None and (item.get("socket_path"), item.get("server_pid")) == self.server

    def _record(self, item: dict) -> dict:
        record = dict(item)
        if record["tool"] == "codex":
            record["codex_home"] = session_identity(record).store
        return {**record, "recovery_id": record.get("recovery_id") or recovery_id(record)}

    def load(self, now: float) -> list[dict]:
        result = [self._record(item) for item in self.read_all() if self.owns(item)
                  and (not self.max_age or now - float(item["created_at"]) <= self.max_age)]
        keys = [pane_key(item) for item in result]
        if len(set(keys)) != len(keys):
            raise RecoveryStateError("pending recovery contains multiple intents for the same stable pane")
        return result

    def foreign_items(self) -> list[dict]:
        return [item for item in self.read_all() if not self.owns(item)]

    def merge(self, current: list[dict], *, dump: str, now: float,
              preserve_identity: bool = True) -> list[dict]:
        if self.server is None:
            raise RecoveryStateError("cannot identify the current tmux server")
        previous = {pane_key(item): item for item in self.load(now)}
        merged = dict(previous)
        for item in current:
            if item["restorable"] is not True:
                # A failed/newer snapshot is not authority to erase saved intent.
                continue
            record = {**item, "socket_path": self.server[0], "server_pid": self.server[1]}
            record.pop("guard", None)
            key = pane_key(record)
            prior = previous.get(key)
            if preserve_identity and prior is None and any(
                old["target"] == record["target"] for old in previous.values()
            ):
                # A coordinate reused by a new pane cannot take over old intent.
                # Only a newly confirmed sealed plan may authorize replacement.
                continue
            same = prior is not None and session_identity(prior) == session_identity(record)
            if same and not record.get("sid"):
                same = prior["command"] == record["command"]
            record.update(created_at=prior["created_at"] if same else now,
                          updated_at=now, attempts=prior.get("attempts", 0) if same else 0,
                          last_error=prior.get("last_error", "") if same else "", source_dump=dump)
            record["recovery_id"] = prior["recovery_id"] if same else recovery_id(record)
            merged[key] = record
        return sorted(merged.values(), key=lambda row: row["target"])

    def display_plan(self, current: list[dict], *, now: float) -> list[dict]:
        pending = {pane_key(item): item for item in self.load(now)}
        result = []
        for item in current:
            key = pane_key(item)
            prior = pending.pop(key, None)
            if prior is None:
                prior = next((old for old in pending.values() if old["target"] == item["target"]), None)
                if prior is not None:
                    pending.pop(pane_key(prior))
                    result.append({**prior, "pending": True})
                    continue
            if item["restorable"] is not True and item["kind"] == "fresh" and prior and prior["tool"] == item["tool"]:
                result.append({**prior, "pending": True})
            else:
                same = prior is not None and session_identity(prior) == session_identity(item)
                result.append({**item, "pending": same})
        result.extend({**item, "pending": True} for item in pending.values())
        return result

    def save(self, items: list[dict], *, resolutions: dict[str, tuple[str, str]] | None = None) -> None:
        if self.server is None or any(not self.owns(item) for item in items):
            raise RecoveryStateError("cannot save recovery intent outside the current tmux server")
        payload = self._read()
        own = [self._record(item) for item in items]
        ids = {item["recovery_id"] for item in own}
        if len(ids) != len(own) or len({pane_key(item) for item in own}) != len(own):
            raise RecoveryStateError("duplicate recovery identity")
        history = list(payload.get("history", []))
        own_keys = {pane_key(item) for item in own}
        for previous in payload["items"]:
            if not self.owns(previous):
                continue
            record = self._record(previous)
            if record["recovery_id"] in ids:
                continue
            if record["recovery_id"] in (resolutions or {}):
                status, reason = resolutions[record["recovery_id"]]
            elif pane_key(record) in own_keys:
                status, reason = "superseded", "replaced by a newly selected recovery intent"
            elif self.max_age and time.time() - record["created_at"] > self.max_age:
                status, reason = "expired", "configured pending retention elapsed"
            else:
                status, reason = "verified", "requested session was verified and rebound"
            history.append({**record, "resolution": status, "resolution_reason": reason,
                            "resolved_at": time.time()})
        # Additive fields preserve compatibility with the previous runtime.
        atomic_json(self.path, {"version": 1, "identity_version": 2, "updated_at": time.time(), "history": history,
                               "items": [*[item for item in payload["items"] if not self.owns(item)], *own]})

    def discard(self, identifier: str) -> dict:
        items = self.load(time.time())
        matches = [item for item in items if item["recovery_id"] == identifier]
        if len(matches) != 1:
            raise RecoveryStateError(f"no pending recovery with ID {identifier} on this tmux server")
        self.save([item for item in items if item is not matches[0]],
                  resolutions={identifier: ("discarded", "explicitly discarded by the user; no pane was changed")})
        return matches[0]
