"""Versioned, digest-protected execution plans for destructive recovery."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import shlex
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any


PLAN_VERSION = 2
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


TOOL_TAGS = {
    "pi": "p",
    "grok": "g",
    "claude": "c",
    "cursor": "r",
    "codex": "x",
    "opencode": "o",
    "kimi": "k",
    "dsh": "d",
    "qwen": "q",
}

class RecoveryPlanError(RuntimeError):
    pass


@dataclass(frozen=True)
class SessionIdentity:
    tool: str
    store: str
    sid: str


def session_identity(item: dict[str, Any], environment: dict[str, str] | None = None) -> SessionIdentity:
    environment = os.environ if environment is None else environment
    home = Path(environment.get("HOME", str(Path.home())))
    tool = item["tool"]
    if tool == "codex":
        storage = item.get("codex_home") or environment.get("CODEX_HOME") or home / ".codex"
    elif tool == "dsh":
        storage = item.get("session_root") or Path(
            item.get("dsh_home") or environment.get("DSH_HOME") or home / ".dsh"
        ) / "sessions"
    elif tool == "qwen":
        storage = item.get("qwen_home") or environment.get("QWEN_HOME") or home / ".qwen"
    else:
        storage = item.get("cwd", "")
    return SessionIdentity(tool, str(Path(os.path.expandvars(str(storage))).expanduser().resolve()), str(item.get("sid", "")))


def ensure_unique_sessions(items: list[dict[str, Any]]) -> None:
    """A Codex conversation has one writer per data directory, across panes."""
    claims: dict[SessionIdentity, str] = {}
    for item in items:
        if item.get("restorable") is not True or item.get("tool") != "codex" or not item.get("sid"):
            continue
        identity = session_identity(item)
        prior = claims.get(identity)
        if prior is not None:
            raise RecoveryPlanError(
                f"session conflict: Codex conversation {identity.sid} is requested by "
                f"both {prior} and {item['target']} in {identity.store}; select one pane before restarting"
            )
        claims[identity] = item["target"]


def normalize_plan(
    raw_items: list[dict[str, Any]],
    *,
    source: str,
    require_guard: bool = False,
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    targets: set[str] = set()
    for number, raw in enumerate(raw_items, start=1):
        required = ("target", "tool", "kind", "cwd", "command", "restorable")
        if not isinstance(raw, dict) or any(key not in raw for key in required):
            raise RecoveryPlanError(
                f"{source} item {number} is missing required fields"
            )
        if any(not isinstance(raw[key], str) for key in required if key != "restorable") or type(raw["restorable"]) is not bool:
            raise RecoveryPlanError(f"{source} item {number} has an invalid field type")
        if not isinstance(raw.get("sid", ""), str) or raw["kind"] not in {"resume", "fresh", "recreated", "invalid"}:
            raise RecoveryPlanError(f"{source} item {number} has an invalid session identity")
        item = {
            "target": str(raw["target"]),
            "tool": str(raw["tool"]),
            "kind": str(raw["kind"]),
            "restorable": raw["restorable"] is True,
            "cwd": str(raw["cwd"]),
            "command": str(raw["command"]),
            "sid": str(raw.get("sid", "")),
        }
        if item["tool"] == "codex":
            try:
                arguments = shlex.split(item["command"])
            except ValueError as error:
                raise RecoveryPlanError(f"invalid Codex launch command: {error}") from error
            command_home = ""
            for argument in arguments[1:] if arguments and arguments[0] == "env" else arguments:
                if "=" not in argument:
                    break
                key, value = argument.split("=", 1)
                if key == "CODEX_HOME":
                    command_home = value
            item["codex_home"] = session_identity({**item, "codex_home": raw.get("codex_home") or command_home}).store
            if command_home and session_identity({**item, "codex_home": command_home}).store != item["codex_home"]:
                raise RecoveryPlanError("Codex command namespace does not match its selected session")
        if item["tool"] == "dsh":
            for key in ("dsh_home", "session_root"):
                item[key] = str(raw.get(key, ""))
        if item["tool"] == "qwen":
            for key in ("qwen_home", "qwen_runtime_dir"):
                item[key] = str(raw.get(key, ""))
        if (
            not item["target"]
            or not item["tool"]
            or not item["cwd"]
            or not item["command"]
        ):
            raise RecoveryPlanError(
                f"{source} item {number} contains an empty identity field"
            )
        if item["tool"] not in TOOL_TAGS:
            raise RecoveryPlanError(
                f"{source} item {number} has unknown tool {item['tool']!r}"
            )
        if item["target"] in targets:
            raise RecoveryPlanError(
                f"{source} contains duplicate target {item['target']}"
            )
        targets.add(item["target"])
        if require_guard:
            guard = raw.get("guard")
            if not isinstance(guard, dict) or any(
                field_name not in guard for field_name in GUARD_FIELDS
            ):
                raise RecoveryPlanError(
                    f"{source} item {number} has an incomplete pane guard"
                )
            if any(not isinstance(guard[field], str) for field in GUARD_FIELDS) or any(not guard[field] for field in GUARD_FIELDS[:5]):
                raise RecoveryPlanError(f"{source} item {number} has an invalid pane guard")
            item["guard"] = dict(guard)
        result.append(item)
    return result


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
    result = normalize_plan(raw_items, source="sealed recovery plan", require_guard=True)
    if any(item["restorable"] is not True for item in result):
        raise RecoveryPlanError("sealed recovery plan contains a non-restorable item")
    guards = [(item["guard"]["socket_path"], item["guard"]["server_pid"], item["guard"]["pane_id"]) for item in result]
    if len(set(guards)) != len(guards):
        raise RecoveryPlanError("sealed recovery plan contains duplicate stable pane identity")
    ensure_unique_sessions(result)
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
        "dump_sha256": hashlib.sha256(resolved_dump.read_bytes()).hexdigest(),
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
    if payload.get("dump_sha256") != hashlib.sha256(resolved_dump.read_bytes()).hexdigest():
        raise RecoveryPlanError("sealed recovery dump changed after confirmation")
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
