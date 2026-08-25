"""Versioned, digest-protected execution plans for destructive recovery."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
import uuid
from pathlib import Path
from typing import Any


PLAN_VERSION = 1
PLAN_MODE = "restart"
GUARD_FIELDS = (
    "pane_id",
    "socket_path",
    "server_pid",
    "pane_pid",
    "current_command",
    "activity_marker",
    "activity_reporter",
    "activity_updated_at",
    "activity_record",
)


class RecoveryPlanError(RuntimeError):
    pass


def _canonical(payload: dict[str, Any]) -> bytes:
    try:
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as error:
        raise RecoveryPlanError(f"recovery plan is not JSON-serializable: {error}") from error
    return encoded.encode("utf-8")


def _digest(payload: dict[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(_canonical(payload)).hexdigest()


def _validate_items(raw_items: object) -> list[dict[str, Any]]:
    if not isinstance(raw_items, list) or not raw_items:
        raise RecoveryPlanError("recovery plan items must be a non-empty list")
    required = ("target", "tool", "kind", "restorable", "cwd", "command", "sid")
    result: list[dict[str, Any]] = []
    targets: set[str] = set()
    for number, raw in enumerate(raw_items, start=1):
        if not isinstance(raw, dict) or any(field not in raw for field in required):
            raise RecoveryPlanError(f"recovery plan item {number} is missing required fields")
        if raw.get("restorable") is not True:
            raise RecoveryPlanError(f"recovery plan item {number} is not restorable")
        if any(not isinstance(raw.get(field), str) for field in required if field != "restorable"):
            raise RecoveryPlanError(f"recovery plan item {number} has a non-string field")
        if any(not raw.get(field) for field in ("target", "tool", "kind", "cwd", "command")):
            raise RecoveryPlanError(f"recovery plan item {number} has an empty identity field")
        target = raw["target"]
        if target in targets:
            raise RecoveryPlanError(f"recovery plan contains duplicate target {target}")
        targets.add(target)
        guard = raw.get("guard")
        if not isinstance(guard, dict) or any(field not in guard for field in GUARD_FIELDS):
            raise RecoveryPlanError(f"recovery plan item {number} has an incomplete pane guard")
        if any(not isinstance(guard[field], str) for field in GUARD_FIELDS):
            raise RecoveryPlanError(f"recovery plan item {number} has a non-string pane guard")
        if any(not guard[field] for field in GUARD_FIELDS[:5]):
            raise RecoveryPlanError(f"recovery plan item {number} has an empty identity guard")
        result.append(json.loads(_canonical(raw)))
    return result


def seal_plan(
    path: Path,
    dump: Path,
    items: list[dict[str, Any]],
    *,
    plan_id: str | None = None,
    created_at: float | None = None,
) -> dict[str, Any]:
    """Atomically write a pinned restart plan and return its complete payload."""

    try:
        resolved_dump = dump.resolve(strict=True)
    except OSError as error:
        raise RecoveryPlanError(f"cannot pin recovery dump {dump}: {error}") from error
    validated_items = _validate_items(items)
    payload: dict[str, Any] = {
        "version": PLAN_VERSION,
        "mode": PLAN_MODE,
        "plan_id": plan_id or uuid.uuid4().hex,
        "created_at": time.time() if created_at is None else created_at,
        "dump": str(resolved_dump),
        "items": validated_items,
    }
    if not isinstance(payload["plan_id"], str) or not payload["plan_id"]:
        raise RecoveryPlanError("recovery plan id must be a non-empty string")
    payload["digest"] = _digest(payload)

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp.{os.getpid()}.{uuid.uuid4().hex}")
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temporary.chmod(0o600)
        temporary.replace(path)
    except OSError as error:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise RecoveryPlanError(f"cannot write recovery plan {path}: {error}") from error
    return payload


def load_sealed_plan(
    path: Path,
    *,
    expected_dump: Path | None = None,
) -> dict[str, Any]:
    """Validate and load a sealed plan without changing any of its fields."""

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RecoveryPlanError(f"cannot read sealed recovery plan {path}: {error}") from error
    if not isinstance(payload, dict):
        raise RecoveryPlanError("sealed recovery plan must be a JSON object")
    supplied_digest = payload.get("digest")
    unsigned = {key: value for key, value in payload.items() if key != "digest"}
    if not isinstance(supplied_digest, str) or not hmac.compare_digest(
        supplied_digest,
        _digest(unsigned),
    ):
        raise RecoveryPlanError("sealed recovery plan digest does not match its contents")
    if payload.get("version") != PLAN_VERSION or payload.get("mode") != PLAN_MODE:
        raise RecoveryPlanError("sealed recovery plan version or mode is unsupported")
    if not isinstance(payload.get("plan_id"), str) or not payload["plan_id"]:
        raise RecoveryPlanError("sealed recovery plan has no plan id")
    if not isinstance(payload.get("created_at"), (int, float)):
        raise RecoveryPlanError("sealed recovery plan has an invalid creation time")
    dump_value = payload.get("dump")
    if not isinstance(dump_value, str) or not Path(dump_value).is_absolute():
        raise RecoveryPlanError("sealed recovery plan has an invalid dump path")
    try:
        resolved_dump = Path(dump_value).resolve(strict=True)
    except OSError as error:
        raise RecoveryPlanError(f"sealed recovery dump is unavailable: {error}") from error
    if str(resolved_dump) != dump_value:
        raise RecoveryPlanError("sealed recovery plan dump path is not pinned")
    if expected_dump is not None:
        try:
            resolved_expected = expected_dump.resolve(strict=True)
        except OSError as error:
            raise RecoveryPlanError(f"expected recovery dump is unavailable: {error}") from error
        if resolved_dump != resolved_expected:
            raise RecoveryPlanError("sealed recovery plan references a different dump")
    payload["items"] = _validate_items(payload.get("items"))
    return payload
