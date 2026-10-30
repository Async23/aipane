"""Own recovery side effects, verification, retry, and durable intent.

The zsh ai-restore command owns snapshot parsing and tool-specific resume command
construction; ai-restart can additionally provide a sealed exact restart plan.
This module is the single stateful recovery interface: it persists intent,
replaces panes when explicitly authorized, launches cleanly, verifies readiness,
retries transient failures, and only then publishes the new pane binding.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any


LIB_DIR = Path(__file__).resolve().parents[1] / "lib"
if str(LIB_DIR) not in sys.path:
    sys.path.insert(0, str(LIB_DIR))

from agent_activity import (  # noqa: E402
    ActivityError,
    AgentActivity,
    TmuxActivityAdapter,
)
from recovery_plan import (GUARD_FIELDS, TOOL_TAGS, RecoveryPlanError, load_sealed_plan,
                           normalize_plan, ensure_unique_sessions, session_identity)
from recovery_state import PendingStore, recovery_id
from codex_sessions import session_loaded as codex_session_loaded, session_writers
from dsh_sessions import current_session as dsh_current_session, matches_session as dsh_matches_session, is_dsh_command_line
from qwen_sessions import current_session as qwen_current_session, session_loaded as qwen_session_loaded


SHELL_COMMANDS = {
    "zsh",
    "-zsh",
    "bash",
    "-bash",
    "sh",
    "-sh",
    "fish",
    "-fish",
    "",
}

FIELD_SEPARATOR = "\x1f"
AUTO_RESUME_PREFIX = "AIPANE_AUTO_RESUME=1 "


class RestoreExecutionError(RuntimeError):
    pass


def positive_float(environment: dict[str, str], name: str, default: float) -> float:
    try:
        value = float(environment.get(name, str(default)))
    except ValueError as error:
        raise RestoreExecutionError(f"{name} must be a number") from error
    if value <= 0:
        raise RestoreExecutionError(f"{name} must be greater than zero")
    return value


def nonnegative_float(
    environment: dict[str, str], name: str, default: float
) -> float:
    try:
        value = float(environment.get(name, str(default)))
    except ValueError as error:
        raise RestoreExecutionError(f"{name} must be a number") from error
    if value < 0:
        raise RestoreExecutionError(f"{name} must not be negative")
    return value


def positive_int(environment: dict[str, str], name: str, default: int) -> int:
    try:
        value = int(environment.get(name, str(default)))
    except ValueError as error:
        raise RestoreExecutionError(f"{name} must be an integer") from error
    if value <= 0:
        raise RestoreExecutionError(f"{name} must be greater than zero")
    return value



def read_plan(path: Path) -> list[dict[str, Any]]:
    raw_items: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise RestoreExecutionError(f"cannot read recovery plan {path}: {error}") from error
    for number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as error:
            raise RestoreExecutionError(
                f"invalid recovery plan JSON on line {number}: {error}"
            ) from error
        if not isinstance(raw, dict):
            raise RestoreExecutionError(
                f"recovery plan line {number} must be a JSON object"
            )
        raw_items.append(raw)
    return normalize_plan(raw_items, source="recovery plan")


def command_matches(
    tool: str,
    current_command: str,
    *,
    allow_ambiguous_node: bool = True,
) -> bool:
    command = current_command.lower()
    if tool == "pi":
        return (
            (allow_ambiguous_node and command == "node")
            or command == "pi"
            or command.startswith("pi-")
        )
    if tool == "grok":
        return command == "grok" or command.startswith("grok-")
    if tool == "claude":
        return command in {"claude", "claude-guard"}
    if tool == "cursor":
        return command == "cursor-agent" or (
            allow_ambiguous_node and command == "node"
        )
    if tool == "codex":
        return command in {"codex", "codexx"} or command.startswith("codex-")
    if tool == "opencode":
        return command == "opencode" or command.startswith("opencode-")
    if tool == "kimi":
        return command == "kimi" or command.startswith("kimi-")
    if tool == "dsh":
        return command in {"dsh", "dsh-tui", "dst"} or (
            allow_ambiguous_node and command == "node"
        )
    if tool == "qwen":
        return command == "qwen" or (
            allow_ambiguous_node and command == "node"
        )
    return False


class Tmux:
    def __init__(self, environment: dict[str, str]) -> None:
        self.environment = environment
        self.command = shlex.split(environment.get("AIPANE_TMUX", "tmux"))
        if not self.command:
            raise RestoreExecutionError("AIPANE_TMUX resolved to an empty command")
        self.activity = AgentActivity(
            adapter=TmuxActivityAdapter(
                command=self.command,
                environment=environment,
            )
        )

    def run(
        self,
        *arguments: str,
        check: bool = False,
        capture_output: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [*self.command, *arguments],
            check=check,
            capture_output=capture_output,
            text=True,
        )

    def current_command(self, target: str) -> str | None:
        completed = self.run(
            "display-message",
            "-p",
            "-t",
            target,
            "#{pane_current_command}",
        )
        if completed.returncode != 0:
            return None
        return completed.stdout.strip()

    def send(self, *arguments: str) -> bool:
        return self.run("send-keys", *arguments).returncode == 0

    def server_identity(self) -> tuple[str, str] | None:
        completed = self.run("display-message", "-p", "#{socket_path}\t#{pid}")
        if completed.returncode != 0:
            return None
        fields = completed.stdout.rstrip("\n").split("\t")
        if len(fields) != 2 or not all(fields):
            return None
        return fields[0], fields[1]

    def pane_identity(self, target: str) -> tuple[str, str, str] | None:
        completed = self.run(
            "display-message",
            "-p",
            "-t",
            target,
            "#{pane_id}\t#{socket_path}\t#{pid}",
        )
        if completed.returncode != 0:
            return None
        fields = completed.stdout.rstrip("\n").split("\t")
        if len(fields) != 3 or not all(fields):
            return None
        return fields[0], fields[1], fields[2]

    def recovery_guard(self, target: str) -> dict[str, str] | None:
        inspection = self.activity.inspect(target)
        if not inspection.available or inspection.guard is None:
            return None
        guard = inspection.guard.as_dict()
        if any(not guard[field_name] for field_name in GUARD_FIELDS[:5]):
            return None
        return guard

    def guard_matches(self, target: str, expected: dict[str, Any]) -> bool:
        return self.recovery_guard(target) == {
            field_name: str(expected.get(field_name, ""))
            for field_name in GUARD_FIELDS
        }

    def foreground_command(self, target: str) -> str:
        process = self.foreground_process(target)
        return process[1] if process else ""

    def foreground_process(self, target: str) -> tuple[int, str] | None:
        processes = self.foreground_processes(target)
        return processes[0] if processes else None

    def foreground_processes(self, target: str) -> list[tuple[int, str]]:
        completed = self.run(
            "display-message",
            "-p",
            "-t",
            target,
            "#{pane_tty}",
        )
        if completed.returncode != 0:
            return []
        tty = completed.stdout.strip().removeprefix("/dev/")
        if not tty:
            return []
        try:
            processes = subprocess.run(
                ["ps", "-t", tty, "-o", "pid=", "-o", "stat=", "-o", "command="],
                check=False,
                capture_output=True,
                text=True,
            )
        except OSError:
            return []
        if processes.returncode != 0:
            return []
        foreground = []
        for line in processes.stdout.splitlines():
            fields = line.strip().split(maxsplit=2)
            if len(fields) == 3 and fields[0].isdigit() and "+" in fields[1]:
                foreground.append((int(fields[0]), fields[2]))
        return foreground

    def capture_failure(self, target: str) -> str:
        completed = self.run("capture-pane", "-p", "-t", target, "-S", "-40")
        if completed.returncode != 0:
            return "pane disappeared"
        candidates = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
        for line in reversed(candidates):
            lowered = line.lower()
            if any(
                marker in lowered
                for marker in ("error", "failed", "not found", "unable", "invalid", "timed out")
            ):
                return line
        return "pane returned to its shell"

    def capture_visible(self, target: str) -> str:
        completed = self.run("capture-pane", "-p", "-t", target)
        return completed.stdout if completed.returncode == 0 else ""

    def default_shell(self) -> str:
        completed = self.run("show-option", "-gv", "default-shell")
        configured = completed.stdout.strip() if completed.returncode == 0 else ""
        return configured or self.environment.get("SHELL", "/bin/zsh")

    def synchronized_windows(
        self, items: list[dict[str, Any]]
    ) -> dict[str, str]:
        result: dict[str, str] = {}
        pane_format = f"#{{window_id}}{FIELD_SEPARATOR}#{{synchronize-panes}}"
        for item in items:
            target = item["target"]
            completed = self.run("display-message", "-p", "-t", target, pane_format)
            if completed.returncode != 0:
                reason = completed.stderr.strip() or "pane disappeared"
                raise RestoreExecutionError(
                    f"could not inspect synchronized input for {target}: {reason}"
                )
            fields = completed.stdout.rstrip("\n").split(FIELD_SEPARATOR, 1)
            if len(fields) != 2 or not fields[0]:
                raise RestoreExecutionError(
                    f"could not resolve the window containing {target}"
                )
            result[fields[0]] = fields[1]
        return result

    def disable_synchronized_windows(self, items: list[dict[str, Any]]) -> list[str]:
        states = self.synchronized_windows(items)
        disabled = [
            window_id
            for window_id, synchronized in states.items()
            if synchronized in {"1", "on"}
        ]
        for window_id in states:
            completed = self.run(
                "set-window-option",
                "-t",
                window_id,
                "synchronize-panes",
                "off",
            )
            if completed.returncode != 0:
                reason = completed.stderr.strip() or "tmux rejected the option change"
                raise RestoreExecutionError(
                    f"could not disable synchronized input for {window_id}: {reason}"
                )
            verification = self.run(
                "display-message",
                "-p",
                "-t",
                window_id,
                "#{synchronize-panes}",
            )
            if verification.returncode != 0 or verification.stdout.strip() not in {
                "0",
                "off",
            }:
                raise RestoreExecutionError(
                    f"synchronized input is still enabled for {window_id}"
                )
        return disabled

    def respawn(self, target: str, cwd: str, shell: str) -> str:
        completed = self.run(
            "respawn-pane",
            "-k",
            "-t",
            target,
            "-c",
            cwd,
            shell,
        )
        if completed.returncode == 0:
            return ""
        return completed.stderr.strip() or "tmux rejected respawn-pane"

    def clear_activity(self, target: str) -> str:
        try:
            self.activity.clear(target)
        except ActivityError as error:
            return str(error)
        return ""

    def notify(self, message: str) -> None:
        self.run("display-message", message)




@dataclass
class Attempt:
    item: dict[str, Any]
    verify_timeout: float
    grok_offset: int | None
    existing_process: bool
    sent: bool
    seen_running: bool = False
    running_since: float | None = None
    ready_since: float | None = None


@dataclass
class ExecutionReport:
    verified: int
    pending: int
    skipped: int
    deferred: int
    blocked: int = 0
    invalid: int = 0
    disabled_windows: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    retained_pending: int = 0

    def render(self) -> str:
        return (
            f"ai-restore: verified={self.verified} pending={self.pending} "
            f"skipped={self.skipped} deferred={self.deferred} "
            f"blocked={self.blocked} invalid={self.invalid}"
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "verified": self.verified,
            "pending": self.pending,
            "skipped": self.skipped,
            "deferred": self.deferred,
            "blocked": self.blocked,
            "invalid": self.invalid,
            "disabled_windows": self.disabled_windows,
            "errors": self.errors,
            "retained_pending": self.retained_pending,
        }


class RestoreExecutor:
    def __init__(self, environment: dict[str, str]) -> None:
        self.environment = environment
        self.state_dir = Path(
            environment.get("AIPANE_STATE_DIR")
            or Path(environment.get("XDG_DATA_HOME", Path.home() / ".local/share"))
            / "aipane"
        )
        pending_path = Path(
            environment.get(
                "AIPANE_RESTORE_PENDING",
                str(self.state_dir / "restore-pending.json"),
            )
        )
        pending_max_age = nonnegative_float(
            environment, "AI_RESTORE_PENDING_MAX_AGE", 0
        )
        self.tmux = Tmux(environment)
        self.store = PendingStore(pending_path, pending_max_age, None)
        self.log_path = Path(
            environment.get("AIPANE_RESTORE_LOG", str(self.state_dir / "ai-restore.log"))
        )
        # Overrides may share a pending file even with different state dirs.
        self.lock_path = pending_path.parent / "restore-executor.lock"
        self.registry_path = Path(
            environment.get(
                "AIPANE_REGISTRY",
                str(self.state_dir / "registry.jsonl"),
            )
        )
        self.bind_command = shlex.split(
            environment.get("AIPANE_BIND_COMMAND", str(Path(__file__).resolve().parents[1] / "bin/aipane-bind"))
        )
        self.grok_log = Path(
            environment.get("AIPANE_GROK_LOG", str(Path.home() / ".grok/logs/unified.jsonl"))
        )
        self.max_attempts = positive_int(environment, "AI_RESTORE_MAX_ATTEMPTS", 2)
        self.verify_timeout = positive_float(
            environment, "AI_RESTORE_VERIFY_TIMEOUT", 10
        )
        self.codex_verify_timeout = positive_float(
            environment, "AI_RESTORE_CODEX_VERIFY_TIMEOUT", 60
        )
        self.codex_home = Path(
            environment.get("CODEX_HOME") or Path.home() / ".codex"
        ).expanduser()
        self.grok_verify_timeout = positive_float(
            environment, "AI_RESTORE_GROK_VERIFY_TIMEOUT", 60
        )
        self.verify_stability = positive_float(
            environment, "AI_RESTORE_VERIFY_STABILITY", 1
        )
        self.grok_fallback_stability = positive_float(
            environment, "AI_RESTORE_GROK_FALLBACK_STABILITY", 50
        )
        self.retry_delay = nonnegative_float(
            environment, "AI_RESTORE_RETRY_DELAY", 5
        )
        self.launch_delay = nonnegative_float(
            environment, "AI_RESTORE_LAUNCH_DELAY", 0.4
        )
        self.interrupt_timeout = positive_float(
            environment, "AI_RESTORE_INTERRUPT_TIMEOUT", 3
        )
        self.poll_interval = min(
            positive_float(environment, "AI_RESTORE_POLL_INTERVAL", 0.1),
            self.verify_stability,
        )

    def scope_pending(self) -> None:
        self.store.server = self.tmux.server_identity()
        foreign_count = len(self.store.foreign_items())
        if self.store.server is None and foreign_count:
            raise RestoreExecutionError("cannot identify the current tmux server; pending recovery retained")
        if foreign_count:
            print(
                f"aipane-restore-executor: retained {foreign_count} recovery record(s) "
                "outside the current tmux server; not retried "
                f"({self.store.path})",
                file=sys.stderr,
            )

    def identify_items(self, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        result = []
        for raw in items:
            item = dict(raw)
            guard = item.get("guard")
            identity = tuple(guard[key] for key in ("pane_id", "socket_path", "server_pid")) if guard else self.tmux.pane_identity(item["target"])
            if identity is not None:
                item["pane_id"], item["socket_path"], item["server_pid"] = identity
            result.append(item)
        return result

    def pending_items(self) -> list[dict[str, Any]]:
        pending = self.store.load(time.time())
        for item in pending:
            identity = tuple(item.get(key, "") for key in ("pane_id", "socket_path", "server_pid"))
            if all(identity) and self.tmux.pane_identity(identity[0]) == identity:
                coordinate = self.tmux.run("display-message", "-p", "-t", identity[0],
                                           "#{session_name}:#{window_index}.#{pane_index}")
                if coordinate.returncode == 0 and coordinate.stdout.strip():
                    item["target"] = coordinate.stdout.strip()
        return pending

    def log(self, message: str) -> None:
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.log_path.open("a", encoding="utf-8") as log:
            log.write(f"{datetime.now().astimezone():%F %T} {message}\n")

    def grok_offset(self) -> int | None:
        try:
            return self.grok_log.stat().st_size
        except OSError:
            return None

    def grok_ready(self, sid: str, offset: int) -> bool:
        try:
            with self.grok_log.open("rb") as log:
                size = log.seek(0, os.SEEK_END)
                log.seek(offset if offset <= size else 0)
                for raw_line in log:
                    try:
                        event = json.loads(raw_line)
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        continue
                    if event.get("msg") == "session loaded" and event.get("sid") == sid:
                        return True
        except OSError:
            return False
        return False

    def wait_for_shell(self, target: str) -> bool:
        deadline = time.monotonic() + self.interrupt_timeout
        while time.monotonic() < deadline:
            command = self.tmux.current_command(target)
            if command is not None and command.lower() in SHELL_COMMANDS:
                return True
            time.sleep(self.poll_interval)
        return False

    def tool_running(self, item: dict[str, Any], current_command: str) -> bool:
        if command_matches(item["tool"], current_command):
            return True
        if item["tool"] != "claude":
            return False
        # Native Claude can expose a version as tmux's short process name.
        # An MCP child may precede it in the foreground process listing.
        for _, command in self.tmux.foreground_processes(item["target"]):
            try:
                arguments = shlex.split(command)
            except ValueError:
                continue
            if arguments and Path(arguments[0]).name in {"claude", "claude-guard"}:
                return True
        return False

    def claude_resume_matches(self, item: dict[str, Any]) -> bool:
        """Verify the native process carries the exact intended resume ID."""
        sid = item.get("sid", "")
        if not sid:
            return False
        for _, command in self.tmux.foreground_processes(item["target"]):
            try:
                args = shlex.split(command)
            except ValueError:
                continue
            if not args or Path(args[0]).name not in {"claude", "claude-guard"}:
                continue
            if any(
                (arg in {"--resume", "-r"} and index + 1 < len(args) and args[index + 1] == sid)
                or arg == f"--resume={sid}"
                for index, arg in enumerate(args)
            ):
                return True
        return False

    def existing_tool_matches(
        self,
        item: dict[str, Any],
        current_command: str,
    ) -> bool:
        if not self.tool_running(item, current_command):
            return False
        if item["tool"] == "dsh":
            return self.dsh_ready(item)
        if item["tool"] == "qwen":
            return self.qwen_ready(item)
        if item["tool"] == "claude" and self.claude_resume_matches(item):
            return True
        foreground = self.tmux.foreground_command(item["target"])
        sid = item.get("sid", "")
        if sid and sid in foreground:
            return True
        identity = self.tmux.pane_identity(item["target"])
        if not sid or identity is None or not self.registry_path.is_file():
            return False
        pane_id, socket_path, server_pid = identity
        latest: dict[str, Any] | None = None
        try:
            with self.registry_path.open(encoding="utf-8") as registry:
                for line in registry:
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if (
                        record.get("pane") == pane_id
                        and record.get("sock") == socket_path
                        and str(record.get("srv", "")) == server_pid
                    ):
                        latest = record
        except OSError:
            return False
        tag = TOOL_TAGS.get(item["tool"])
        if latest and latest.get("sid") == sid and latest.get("tool") in {
            tag,
            item["tool"],
        }:
            return True
        return False

    def dsh_ready(self, item: dict[str, Any]) -> bool:
        identity = self.tmux.pane_identity(item["target"])
        if identity is None:
            return False
        pane_id, socket_path, server_pid = identity
        tty = self.tmux.run("display-message", "-p", "-t", item["target"], "#{pane_tty}").stdout.strip()
        record = dsh_current_session(
            pane_id=pane_id, socket_path=socket_path, server_pid=server_pid,
            pane_tty=tty, environment={**self.environment,
                                      **({"DSH_HOME": item["dsh_home"]} if item.get("dsh_home") else {})},
        )
        return dsh_matches_session(
            record, session_id=item.get("sid") or (record or {}).get("session_id", ""),
            cwd=item["cwd"], dsh_home=item.get("dsh_home", ""), session_root=item.get("session_root", ""),
        )

    def qwen_ready(self, item: dict[str, Any]) -> bool:
        tty = self.tmux.run("display-message", "-p", "-t", item["target"], "#{pane_tty}")
        if tty.returncode:
            return False
        environment = self.environment.copy()
        for key, variable in (("qwen_home", "QWEN_HOME"), ("qwen_runtime_dir", "QWEN_RUNTIME_DIR")):
            if item.get(key):
                environment[variable] = item[key]
        sid = item.get("sid", "")
        if not sid and item["kind"] == "fresh":
            # AI_RESTORE_FRESH explicitly opts into opening a new session.
            # It still needs native identity, but has no saved ID to match.
            current = qwen_current_session(pane_tty=tty.stdout.strip(), environment=environment)
            sid = (current or {}).get("session_id", "")
        return qwen_session_loaded(
            pane_tty=tty.stdout.strip(), sid=sid,
            cwd=item["cwd"], environment=environment,
        )

    def pane_guard_matches(self, item: dict[str, Any]) -> bool:
        expected = (
            item.get("pane_id"),
            item.get("socket_path"),
            item.get("server_pid"),
        )
        if not all(expected):
            return False
        return self.tmux.pane_identity(item["target"]) == expected

    def launch(self, item: dict[str, Any], *, interrupt_running: bool) -> tuple[Attempt | None, str]:
        target = item["target"]
        if not all(item.get(key) for key in ("pane_id", "socket_path", "server_pid")):
            return None, "saved recovery has no stable pane identity; archive it and confirm a new restart plan"
        if not self.pane_guard_matches(item):
            return None, "pane identity changed since recovery was planned"
        current = self.tmux.current_command(target)
        if current is None:
            return None, "pane disappeared"
        matches_existing = self.existing_tool_matches(item, current)
        if interrupt_running and self.tool_running(item, current):
            if item["tool"] == "qwen":
                return None, "Qwen process left running; recovery pending"
            elif item["tool"] == "dsh" and current == "node":
                # A retry may see another Node program occupying this pane.
                # Missing readiness is not authority to interrupt arbitrary node.
                matches_existing = matches_existing or is_dsh_command_line(self.tmux.foreground_command(target))
            else:
                matches_existing = True
        if matches_existing:
            if not interrupt_running:
                return (
                    Attempt(
                        item=item,
                        verify_timeout=(
                            self.codex_verify_timeout if item["tool"] == "codex"
                            else self.verify_timeout
                        ),
                        grok_offset=None,
                        existing_process=True,
                        sent=False,
                    ),
                    "",
                )
            if not self.tmux.send("-t", target, "C-c") or not self.wait_for_shell(target):
                return None, "running Agent did not stop for retry"
            current = self.tmux.current_command(target) or ""
        if current.lower() not in SHELL_COMMANDS:
            return None, f"pane is running {current}"

        # C-c clears typed text and stale SGR mouse reports. -R resets tmux's
        # terminal state before the literal command is queued.
        if not self.tmux.send("-t", target, "C-c"):
            return None, "could not clear pane input"
        if not self.tmux.send("-R", "-t", target):
            return None, "could not reset pane terminal state"
        offset = self.grok_offset() if item["tool"] == "grok" else None
        # The shell may have changed directories while recovery was pending.
        # Fail at cd if the saved workspace vanished; never resume elsewhere.
        launch_command = item["command"]
        if item["tool"] == "codex":
            launch_command = f"CODEX_HOME={shlex.quote(str(item.get('codex_home') or self.codex_home))} {launch_command}"
        command = f"cd -- {shlex.quote(item['cwd'])} && {launch_command}"
        if not self.tmux.send("-l", "-t", target, command):
            return None, "could not send recovery command"
        if not self.tmux.send("-t", target, "Enter"):
            return None, "could not submit recovery command"

        timeout = self.grok_verify_timeout if item["tool"] == "grok" else self.verify_timeout
        if item["tool"] == "codex":
            timeout = self.codex_verify_timeout
        return (
            Attempt(
                item=item,
                verify_timeout=timeout,
                grok_offset=offset,
                existing_process=False,
                sent=True,
            ),
            "",
        )

    def verify_round(
        self, attempts: list[Attempt]
    ) -> tuple[list[dict[str, Any]], dict[str, str], dict[str, str]]:
        for attempt in attempts:
            attempt.item.setdefault("recovery_id", recovery_id(attempt.item))
        active = {attempt.item["recovery_id"]: attempt for attempt in attempts}
        # Launching the rest of a batch can exceed an earlier pane's timeout.
        # Start each verification budget when polling begins, so healthy panes
        # get their stability window before a timeout can trigger interruption.
        verification_started = time.monotonic()
        deadlines = {
            target: verification_started + attempt.verify_timeout
            for target, attempt in active.items()
        }
        verified: list[dict[str, Any]] = []
        failures: dict[str, str] = {}
        blocked: dict[str, str] = {}

        while active:
            now = time.monotonic()
            for identifier, attempt in tuple(active.items()):
                item = attempt.item
                target = item["target"]
                current = self.tmux.current_command(target)
                if current is None:
                    failures[identifier] = "pane disappeared"
                    active.pop(identifier)
                    continue
                auto_claude = (
                    attempt.sent and item["tool"] == "claude"
                    and item["command"].startswith(AUTO_RESUME_PREFIX)
                )
                matched_claude = self.claude_resume_matches(item) if auto_claude else False
                if self.tool_running(item, current) or matched_claude:
                    if auto_claude and not matched_claude:
                        attempt.seen_running = True
                        attempt.running_since = None
                        if now >= deadlines[identifier]:
                            failures[identifier] = "Claude did not start the requested session"
                            active.pop(identifier)
                        continue
                    if item["tool"] == "codex":
                        visible = " ".join(self.tmux.capture_visible(target).split()).lower()
                        process = self.tmux.foreground_process(target)
                        codex_loaded = bool(process and codex_session_loaded(
                            process[0], item.get("sid", ""), Path(item.get("codex_home") or self.codex_home),
                        ))
                        # Recheck runtime evidence after capturing the screen:
                        # loading may finish while that subprocess is running.
                        # Loaded history can also quote an old startup dialog.
                        if (
                            not codex_loaded
                            and "update available!" in visible
                            and "skip until next version" in visible
                        ):
                            blocked[identifier] = (
                                "Codex startup is blocked at its update prompt"
                            )
                            active.pop(identifier)
                            continue
                        if (
                            not codex_loaded
                            and "this conversation is open in another app" in visible
                            and "close it there and press r to continue" in visible
                            and "r retry" in visible
                        ):
                            blocked[identifier] = (
                                "Codex conversation is open in another app; close the other "
                                f"instance, then press R in pane {target} to resume. "
                                "ai-restart --retry only verifies the waiting process; "
                                "it cannot release the other instance's conversation lock"
                            )
                            active.pop(identifier)
                            continue
                    attempt.seen_running = True
                    if attempt.running_since is None:
                        attempt.running_since = now
                    stable_for = now - attempt.running_since
                    ready = False
                    if item["tool"] == "dsh":
                        ready = stable_for >= self.verify_stability and self.dsh_ready(item)
                    elif item["tool"] == "qwen":
                        if not self.qwen_ready(item):
                            attempt.ready_since = None
                        elif attempt.ready_since is None:
                            attempt.ready_since = time.monotonic()
                        ready = (
                            attempt.ready_since is not None
                            and time.monotonic() - attempt.ready_since >= self.verify_stability
                        )
                    elif item["tool"] == "codex":
                        if not codex_loaded:
                            attempt.ready_since = None
                        elif attempt.ready_since is None:
                            attempt.ready_since = time.monotonic()
                        ready = (
                            attempt.ready_since is not None
                            and time.monotonic() - attempt.ready_since >= self.verify_stability
                        )
                    elif attempt.existing_process:
                        ready = stable_for >= self.verify_stability
                    elif item["tool"] == "grok" and item.get("sid") and attempt.grok_offset is not None:
                        ready = stable_for >= self.verify_stability and (
                            self.grok_ready(item["sid"], attempt.grok_offset)
                            or stable_for >= self.grok_fallback_stability
                        )
                    elif item["tool"] == "grok":
                        ready = stable_for >= self.grok_fallback_stability
                    else:
                        ready = stable_for >= self.verify_stability
                    if ready:
                        verified.append(item)
                        active.pop(identifier)
                        continue
                elif current.lower() in SHELL_COMMANDS:
                    if attempt.seen_running:
                        failures[identifier] = self.tmux.capture_failure(target)
                        active.pop(identifier)
                        continue
                elif auto_claude:
                    # The recovery launcher may briefly be env or Python before
                    # execing Claude. Wait, but never verify it as Claude.
                    pass
                else:
                    failures[identifier] = f"pane switched to unexpected process {current}"
                    active.pop(identifier)
                    continue

                if now >= deadlines[identifier]:
                    if item["tool"] == "qwen" and attempt.seen_running:
                        blocked[identifier] = (
                            "Qwen has not confirmed the requested session in its runtime records; "
                            "left running, recovery pending"
                        )
                        active.pop(identifier)
                        continue
                    elif item["tool"] == "codex" and attempt.seen_running:
                        blocked[identifier] = (
                            "Codex has not opened the requested conversation for writing; "
                            "left running, recovery pending"
                        )
                        active.pop(identifier)
                        continue
                    elif item["tool"] == "dsh" and attempt.seen_running:
                        failures[identifier] = "dsh TUI did not select the requested session before timeout"
                    elif item["tool"] == "grok" and attempt.seen_running:
                        failures[identifier] = "Grok did not report session loaded before timeout"
                    elif current.lower() in SHELL_COMMANDS:
                        failures[identifier] = self.tmux.capture_failure(target)
                    else:
                        failures[identifier] = "Agent process did not become ready before timeout"
                    active.pop(identifier)
            if active:
                time.sleep(self.poll_interval)
        return verified, failures, blocked

    def bind(self, item: dict[str, Any]) -> bool:
        sid = item.get("sid", "")
        if not sid:
            return True
        tag = TOOL_TAGS.get(item["tool"])
        identity = self.tmux.pane_identity(item["target"])
        if not tag or identity is None or not self.bind_command:
            return False
        pane_id, socket_path, server_pid = identity
        environment = self.environment.copy()
        environment["TMUX"] = f"{socket_path},{server_pid},0"
        try:
            completed = subprocess.run(
                [
                    *self.bind_command,
                    "--tool",
                    tag,
                    "--sid",
                    sid,
                    "--pane",
                    pane_id,
                    "--cmd",
                    item["command"],
                ],
                check=False,
                capture_output=True,
                text=True,
                env=environment,
                cwd=item["cwd"],
            )
        except OSError:
            return False
        return completed.returncode == 0

    def execute_candidates(
        self,
        pending_by_id: dict[str, dict[str, Any]],
        candidates: list[dict[str, Any]],
    ) -> tuple[int, int, int, list[str]]:
        verified_count = 0
        deferred = 0
        blocked_count = 0
        final_errors: dict[str, str] = {}

        for round_index in range(self.max_attempts):
            if not candidates:
                break
            attempts: list[Attempt] = []
            immediate_failures: dict[str, str] = {}
            for item in candidates:
                attempt, error = self.launch(
                    item,
                    interrupt_running=round_index > 0,
                )
                if attempt is None:
                    immediate_failures[item["recovery_id"]] = error
                    continue
                if attempt.sent:
                    item["attempts"] = int(item.get("attempts", 0)) + 1
                    item["updated_at"] = time.time()
                    self.log(
                        f"attempt {round_index + 1} sent {item['target']} "
                        f"({item['tool']}/{item['kind']}): {item['command']}"
                    )
                attempts.append(attempt)
                if self.launch_delay and item is not candidates[-1]:
                    time.sleep(self.launch_delay)

            self.store.save(list(pending_by_id.values()))
            ready, failures, blocked = self.verify_round(attempts)
            failures.update(immediate_failures)
            retry: list[dict[str, Any]] = []

            for item in ready:
                target = item["target"]
                if self.bind(item):
                    pending_by_id.pop(item["recovery_id"], None)
                    verified_count += 1
                    self.log(
                        f"verified {target} ({item['tool']}/{item['kind']}): {item['command']}"
                    )
                else:
                    item["last_error"] = "session resumed but pane binding failed"
                    item["updated_at"] = time.time()
                    final_errors[target] = item["last_error"]
                    self.log(f"pending {target}: {item['last_error']}")

            for identifier, error in blocked.items():
                item = pending_by_id[identifier]
                target = item["target"]
                item["last_error"] = error
                item["updated_at"] = time.time()
                blocked_count += 1
                final_errors[target] = error
                self.log(f"blocked {target} ({item['tool']}): {error}")

            for item in candidates:
                target = item["target"]
                identifier = item["recovery_id"]
                if identifier in blocked:
                    continue
                error = failures.get(identifier)
                if not error:
                    continue
                item["last_error"] = error
                item["updated_at"] = time.time()
                self.log(f"attempt {round_index + 1} failed {target} ({item['tool']}): {error}")
                current_command = self.tmux.current_command(target)
                existing = any(
                    attempt.item["target"] == target and attempt.existing_process
                    for attempt in attempts
                )
                retryable = (
                    not existing and identifier not in immediate_failures and current_command is not None
                    and (current_command.lower() in SHELL_COMMANDS
                         or self.tool_running(item, current_command))
                )
                if round_index + 1 < self.max_attempts and retryable:
                    retry.append(item)
                else:
                    final_errors[target] = error
                    if not retryable:
                        deferred += 1

            self.store.save(list(pending_by_id.values()))
            candidates = retry
            if candidates and self.retry_delay:
                delay = self.retry_delay * (round_index + 1)
                self.log(f"retrying {len(candidates)} pane(s) after {delay:g}s")
                time.sleep(delay)

        return (
            verified_count,
            deferred,
            blocked_count,
            [f"{target}: {error}" for target, error in sorted(final_errors.items())],
        )

    def finish_report(self, report: ExecutionReport) -> ExecutionReport:
        self.log(report.render())
        self.tmux.notify(report.render())
        return report

    def execute(self, current: list[dict[str, Any]], dump: str) -> ExecutionReport:
        current = self.identify_items(current)
        ensure_unique_sessions(current)
        now = time.time()
        pending = self.store.merge(current, dump=dump, now=now)
        pending_by_id = {item["recovery_id"]: item for item in pending}
        ensure_unique_sessions(pending)
        self.store.save(list(pending_by_id.values()))
        skipped = sum(item["restorable"] is not True for item in current)
        invalid = [item for item in current if item["kind"] == "invalid"]
        invalid_errors = [
            f"{item['target']} ({item['tool']}): saved session identity is unavailable "
            "or its durable session is invalid; not restored"
            for item in invalid
        ]
        for error in invalid_errors:
            self.log(error)
        verified, deferred, blocked, errors = self.execute_candidates(
            pending_by_id,
            list(pending_by_id.values()),
        )

        remaining = list(pending_by_id.values())
        self.store.save(remaining)
        report = ExecutionReport(
            verified=verified,
            pending=len(remaining),
            skipped=skipped,
            deferred=deferred,
            blocked=blocked,
            invalid=len(invalid),
            errors=[*errors, *invalid_errors],
        )
        return self.finish_report(report)

    def execute_pending(self) -> ExecutionReport:
        """Retry saved intent without a new snapshot or replacing any pane."""
        pending = self.pending_items()
        ensure_unique_sessions(pending)
        self.store.save(pending)
        self.tmux.disable_synchronized_windows([
            item for item in pending if self.pane_guard_matches(item)
        ])
        return self.execute([], "pending recovery")

    def assert_exact_guards(self, items: list[dict[str, Any]]) -> None:
        self.validate_session_ownership(items)
        for item in items:
            if not self.tmux.guard_matches(item["target"], item["guard"]):
                raise RestoreExecutionError(
                    f"pane guard changed for {item['target']} after confirmation"
                )
            if item["tool"] == "dsh" and not self.dsh_ready(item):
                raise RestoreExecutionError(
                    f"dsh selected session changed for {item['target']}; refresh the restart plan"
                )
            if item["tool"] == "qwen" and not self.qwen_ready(item):
                raise RestoreExecutionError(
                    f"Qwen runtime identity could not be verified for {item['target']}; refresh the restart plan"
                )

    def validate_session_ownership(self, items: list[dict[str, Any]]) -> None:
        ensure_unique_sessions(items)
        codex = [item for item in items if item["tool"] == "codex" and item.get("sid")]
        owners: dict[str, dict[int, list[dict]]] = {}
        for item in codex:
            identity = session_identity(item, self.environment)
            if identity.store not in owners:
                owners[identity.store] = session_writers(Path(identity.store))
            writers = owners[identity.store]
            process = self.tmux.foreground_process(item["target"])
            pid = process[0] if process else None
            observed = {record["sid"] for record in writers.get(pid, [])}
            if observed and identity.sid not in observed:
                raise RestoreExecutionError(f"Codex selected session changed in {item['target']}; refresh the restart plan")
            foreign = [owner for owner, records in writers.items() if owner != pid
                       and any(record["sid"] == identity.sid for record in records)]
            if foreign:
                raise RestoreExecutionError(
                    f"session conflict: Codex conversation {identity.sid} for {item['target']} "
                    f"is already open for writing in process(es) {', '.join(map(str, foreign))}; "
                    "resolve the duplicate instance before restarting"
                )

    def execute_restart(
        self,
        exact_items: list[dict[str, Any]],
        dump: str,
    ) -> ExecutionReport:
        # The executor lock is already held by main. Validate the complete set
        # before persisting intent or touching any tmux process.
        self.assert_exact_guards(exact_items)
        exact_items = self.identify_items(exact_items)
        now = time.time()
        pending = self.store.merge(
            exact_items,
            dump=dump,
            now=now,
            preserve_identity=False,
        )
        pending_by_id = {item["recovery_id"]: item for item in pending}
        selected = {item["target"]: item for item in pending if item["pane_id"] in {p["pane_id"] for p in exact_items}}
        exact_ids = {item["recovery_id"] for item in selected.values()}
        for planned in exact_items:
            item = selected[planned["target"]]
            guard = planned["guard"]
            item["pane_id"] = guard["pane_id"]
            item["socket_path"] = guard["socket_path"]
            item["server_pid"] = guard["server_pid"]

        # Durable intent must exist before the first destructive tmux operation.
        self.store.save(list(pending_by_id.values()))
        disabled_windows = self.tmux.disable_synchronized_windows(exact_items)
        self.assert_exact_guards(exact_items)

        shell = self.tmux.default_shell()
        ready: list[dict[str, Any]] = []
        prelaunch_errors: list[str] = []
        for planned in exact_items:
            target = planned["target"]
            item = selected[target]
            if not self.tmux.guard_matches(target, planned["guard"]):
                error = "pane guard changed immediately before respawn"
            elif planned["tool"] == "dsh" and not self.dsh_ready(planned):
                error = "dsh selected session changed immediately before respawn; refresh the restart plan"
            elif planned["tool"] == "qwen" and not self.qwen_ready(planned):
                error = "Qwen runtime identity could not be verified immediately before respawn; refresh the restart plan"
            else:
                error = self.tmux.respawn(target, item["cwd"], shell)
                if error:
                    error = f"failed to respawn pane: {error}"
                elif not self.wait_for_shell(target):
                    error = "respawned shell did not become ready"
                else:
                    error = self.tmux.clear_activity(target)
                    if error:
                        error = f"failed to clear replaced Agent Activity: {error}"
            if error:
                item["last_error"] = error
                item["updated_at"] = time.time()
                prelaunch_errors.append(f"{target}: {error}")
                self.log(f"pending {target}: {error}")
                self.store.save(list(pending_by_id.values()))
                continue
            identity = self.tmux.pane_identity(target)
            if identity is None:
                error = "pane disappeared after respawn"
                item["last_error"] = error
                item["updated_at"] = time.time()
                prelaunch_errors.append(f"{target}: {error}")
                self.store.save(list(pending_by_id.values()))
                continue
            item["pane_id"], item["socket_path"], item["server_pid"] = identity
            ready.append(item)

        self.store.save(list(pending_by_id.values()))
        verified, deferred, blocked, launch_errors = self.execute_candidates(
            pending_by_id,
            ready,
        )
        remaining = list(pending_by_id.values())
        self.store.save(remaining)
        report = ExecutionReport(
            verified=verified,
            pending=sum(identifier in pending_by_id for identifier in exact_ids),
            retained_pending=sum(identifier not in exact_ids for identifier in pending_by_id),
            skipped=0,
            deferred=deferred,
            blocked=blocked,
            disabled_windows=disabled_windows,
            errors=[*prelaunch_errors, *launch_errors],
        )
        return self.finish_report(report)


def print_plan(items: list[dict[str, Any]], dump: str) -> None:
    print(f"ai-restore dry-run  (dump: {dump})")
    for item in items:
        if item["restorable"] is True:
            label = item["kind"]
        elif item["kind"] == "fresh":
            label = "skip"
        else:
            label = item["kind"]
        if item.get("pending"):
            label = f"pending/{label}"
        print(
            f"  {item['target']:<22} {item['tool']:<6} {label:<14} {item['command']}"
        )
    print(f"\nsummary: ai_panes={len(items)} (dry-run, nothing sent)")


def main() -> int:
    parser = argparse.ArgumentParser(prog="aipane-restore-executor")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--plan", type=Path)
    source.add_argument("--sealed-plan", type=Path)
    source.add_argument("--retry-pending", action="store_true")
    source.add_argument("--discard-pending", metavar="ID")
    parser.add_argument("--dump")
    parser.add_argument("--report-json", action="store_true")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--plan-json", action="store_true")
    mode.add_argument("--dry-run", action="store_true")
    arguments = parser.parse_args()

    if arguments.sealed_plan and (arguments.dump or arguments.plan_json or arguments.dry_run):
        parser.error("--sealed-plan cannot be combined with --dump or preview modes")
    if arguments.plan and not arguments.dump:
        parser.error("--dump is required with --plan")
    if arguments.retry_pending and arguments.dump:
        parser.error("--retry-pending cannot be combined with --dump")

    environment = os.environ.copy()
    executor = RestoreExecutor(environment)
    if arguments.sealed_plan:
        sealed = load_sealed_plan(arguments.sealed_plan)
        current = normalize_plan(
            sealed["items"],
            source="sealed recovery plan",
            require_guard=True,
        )
        dump = sealed["dump"]
    elif arguments.retry_pending or arguments.discard_pending:
        current = []
        dump = "pending recovery"
    else:
        current = read_plan(arguments.plan)
        dump = arguments.dump

    if arguments.plan_json:
        executor.scope_pending()
        # Machine planning has an explicit source. Pending intent must never
        # replace or extend a freshly captured restart plan by coordinate.
        display_items = ([{**item, "pending": True} for item in executor.pending_items()]
                         if arguments.retry_pending else current)
        for item in display_items:
            print(json.dumps(item, ensure_ascii=False, separators=(",", ":")))
        return 0
    if arguments.dry_run:
        executor.scope_pending()
        display_items = (executor.pending_items() if not current else
                         executor.store.display_plan(executor.identify_items(current), now=time.time()))
        print_plan(display_items, dump)
        return 0

    executor.state_dir.mkdir(parents=True, exist_ok=True)
    executor.lock_path.parent.mkdir(parents=True, exist_ok=True)
    with executor.lock_path.open("a", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(
                "aipane-restore-executor: another recovery executor is already running",
                file=sys.stderr,
            )
            return 3
        executor.scope_pending()
        if arguments.discard_pending:
            discarded = executor.store.discard(arguments.discard_pending)
            executor.log(f"discarded {discarded['recovery_id']} ({discarded['target']}) by explicit request")
            print(f"ai-restore: discarded recovery {discarded['recovery_id']} ({discarded['target']}); no pane changed")
            return 0
        if arguments.sealed_plan:
            report = executor.execute_restart(current, dump)
        elif arguments.retry_pending:
            report = executor.execute_pending()
        else:
            report = executor.execute(current, dump)
    if arguments.report_json:
        print(json.dumps(report.as_dict(), ensure_ascii=False, separators=(",", ":")))
    else:
        print(report.render())
        for error in report.errors:
            print(f"ai-restore: {error}", file=sys.stderr)
    return 0 if report.pending == 0 and report.invalid == 0 else 1
