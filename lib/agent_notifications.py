"""Normalize and deliver AI Tool notifications through one deep module."""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shlex
import shutil
import sqlite3
import subprocess
import time
import unicodedata
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Protocol


__all__ = (
    "AgentNotifications",
    "InMemoryNotificationAdapter",
    "MacOSNotificationAdapter",
    "Notification",
    "NotificationResult",
)


_MARKDOWN_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]+\)")
_CURSOR_TRANSCRIPT_TAIL_BYTES = 8 * 1024 * 1024
_GROK_TRANSCRIPT_TAIL_BYTES = 4 * 1024 * 1024
COMPLETION_SOUNDS = ("Glass", "Ping", "Pop", "Purr", "Submarine", "Tink")
_CODEX_TITLE_SPINNER_FRAMES = frozenset(
    ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")
)
_CODEX_TITLE_ACTION_PREFIXES = (
    "[ ! ] Action Required",
    "[ . ] Action Required",
)
_FILE_TOKEN_RE = re.compile(
    r"(?<![\w.-])(?:[\w@+.-]+/)*[\w@+.-]+\.[A-Za-z][A-Za-z0-9-]*"
)
_GOAL_CONTEXT_RE = re.compile(
    r"<codex_internal_context\s+source=[\"']goal[\"']"
)
_GOAL_NOTIFICATION_TITLES = {
    "complete": "Goal 完成",
    "blocked": "Goal 需要你处理",
    "paused": "Goal 已暂停",
    "usage_limited": "Goal 用量受限",
    "budget_limited": "Goal 预算已用完",
}
_KIMI_SAFE_SESSION_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,159}")
_KIMI_WIRE_TAIL_BYTES = 1024 * 1024


@dataclass(frozen=True)
class Notification:
    title: str
    subtitle: str
    body: str
    sound: str
    event: str
    group: str
    action: str = ""
    category: str = "main"
    goal_status: str = ""
    pane_id: str = ""

    def as_dict(self) -> dict[str, str]:
        return {
            "title": self.title,
            "subtitle": self.subtitle,
            "body": self.body,
            "sound": self.sound,
            "event": self.event,
            "group": self.group,
            "action": self.action,
            "category": self.category,
        }


@dataclass(frozen=True)
class NotificationResult:
    outcome: str
    notification: Notification | None = None
    reason: str = ""
    channel: str = ""


class NotificationAdapter(Protocol):
    def deliver(self, agent: str, notification: Notification) -> str: ...


class InMemoryNotificationAdapter:
    """Record notifications without crossing the macOS delivery seam."""

    def __init__(self) -> None:
        self.deliveries: list[tuple[str, Notification]] = []

    def deliver(self, agent: str, notification: Notification) -> str:
        self.deliveries.append((agent, notification))
        return "memory"


class MacOSNotificationAdapter:
    """Deliver normalized notifications through agent-specific macOS apps."""

    def __init__(
        self,
        *,
        environment: Mapping[str, str] | None = None,
        run_command: Callable[[list[str], float], bool] | None = None,
    ) -> None:
        self.environment = dict(environment or os.environ)
        self.home = Path(
            self.environment.get("HOME", str(Path.home()))
        ).expanduser()
        self.run_command = run_command

    def deliver(self, agent: str, notification: Notification) -> str:
        if agent == "cursor":
            return self._deliver_cursor(notification)
        if agent == "claude":
            return self._deliver_claude(notification)
        if agent == "grok":
            return self._deliver_grok(notification)
        if agent == "codex":
            return self._deliver_codex(notification)
        if agent == "kimi":
            return self._deliver_kimi(notification)
        raise ValueError(f"unsupported notification agent: {agent}")

    def _deliver_cursor(self, notification: Notification) -> str:
        app = self.home / "Applications" / "Cursor Agent Notifier.app"
        notifier = app / "Contents" / "MacOS" / "terminal-notifier"
        icon = app / "Contents" / "Resources" / "CursorAgent.icns"
        args = [
            "-title",
            notification.title,
            "-message",
            notification.body,
            "-sound",
            notification.sound,
            "-group",
            notification.group,
            "-ignoreDnD",
        ]
        if notification.subtitle:
            args.extend(["-subtitle", notification.subtitle])
        if icon.is_file():
            args.extend(["-appIcon", icon.resolve().as_uri()])
        if notification.action:
            args.extend(["-execute", notification.action])
        if _executable(notifier) and self._run([str(notifier), *args], timeout=5):
            return "custom-app"

        fallback = Path("/opt/homebrew/bin/terminal-notifier")
        if _executable(fallback) and self._run(
            [
                str(fallback),
                *args,
                "-sender",
                "com.alfheim.cursor-cli-notifier",
            ],
            timeout=5,
        ):
            return "terminal-notifier"
        return "failed"

    def _deliver_claude(self, notification: Notification) -> str:
        notifier = (
            self.home
            / "Applications"
            / "Claude Code Notifier.app"
            / "Contents"
            / "MacOS"
            / "terminal-notifier"
        )
        if not _executable(notifier):
            discovered = shutil.which(
                "terminal-notifier",
                path=self.environment.get("PATH"),
            )
            notifier = Path(discovered) if discovered else notifier
        args = ["-title", notification.title]
        if notification.subtitle:
            args.extend(["-subtitle", notification.subtitle])
        args.extend(
            [
                "-message",
                notification.body,
                "-group",
                notification.group,
                "-appIcon",
                f"file://{self.home}/.claude/hooks/claude-code-icon.png",
                "-sound",
                notification.sound,
            ]
        )
        if notification.action:
            args.extend(["-execute", notification.action])
        if _executable(notifier) and self._run([str(notifier), *args], timeout=5):
            return "custom-app"

        script = (
            'display notification "{}" with title "{}" sound name "{}"'.format(
                _apple_escape(notification.body),
                _apple_escape(notification.title),
                _apple_escape(notification.sound),
            )
        )
        osascript = Path("/usr/bin/osascript")
        command = str(osascript) if osascript.is_file() else "osascript"
        if self._run([command, "-e", script], timeout=5):
            return "osascript"
        return "failed"

    def _deliver_grok(self, notification: Notification) -> str:
        notifier = (
            self.home
            / "Applications"
            / "Grok Notifier.app"
            / "Contents"
            / "MacOS"
            / "terminal-notifier"
        )
        args = [
            "-title",
            notification.title,
            "-subtitle",
            notification.subtitle,
            "-message",
            notification.body,
            "-sound",
            notification.sound,
            "-group",
            notification.group,
        ]
        if notification.action:
            args.extend(["-execute", notification.action])
        if _executable(notifier) and self._run([str(notifier), *args], timeout=7):
            return "custom-app"

        ghostty_script = (
            'tell application id "com.mitchellh.ghostty" to display notification '
            '"{}" with title "{}" subtitle "{}" sound name "{}"'.format(
                _apple_escape(notification.body),
                _apple_escape(notification.title),
                _apple_escape(notification.subtitle),
                _apple_escape(notification.sound),
            )
        )
        osascript = Path("/usr/bin/osascript")
        command = str(osascript) if osascript.is_file() else "osascript"
        if self._run([command, "-e", ghostty_script], timeout=7):
            return "ghostty-osascript"

        script = (
            'display notification "{}" with title "{}" subtitle "{}" '
            'sound name "{}"'.format(
                _apple_escape(notification.body),
                _apple_escape(notification.title),
                _apple_escape(notification.subtitle),
                _apple_escape(notification.sound),
            )
        )
        if self._run([command, "-e", script], timeout=7):
            return "osascript"
        return "failed"

    def _deliver_codex(self, notification: Notification) -> str:
        app = self.home / "Applications" / "Codex Notifier.app"
        icon = app / "Contents" / "Resources" / "CodexGhostty.icns"
        args = [
            "-title",
            notification.title,
            "-subtitle",
            notification.subtitle,
            "-message",
            notification.body,
            "-sound",
            notification.sound,
            "-group",
            notification.group,
            "-appIcon",
            icon.resolve().as_uri(),
        ]
        if notification.action:
            args.extend(["-execute", notification.action])
        if app.exists() and self._run(
            ["/usr/bin/open", "-gj", str(app), "--args", *args],
            timeout=5,
        ):
            return "custom-app"

        fallback = Path("/opt/homebrew/bin/terminal-notifier")
        fallback_args = [
            str(fallback),
            "-title",
            notification.title,
            "-subtitle",
            notification.subtitle,
            "-message",
            notification.body,
            "-appIcon",
            "file:///Applications/Ghostty.app/Contents/Resources/Ghostty.icns",
            "-sound",
            notification.sound,
            "-group",
            notification.group,
        ]
        if _executable(fallback) and self._run(fallback_args, timeout=5):
            return "terminal-notifier"

        ghostty_script = (
            'tell application id "com.mitchellh.ghostty" to display notification '
            '"{}" with title "{}" subtitle "{}" sound name "{}"'.format(
                _apple_escape(notification.body),
                _apple_escape(notification.title),
                _apple_escape(notification.subtitle),
                _apple_escape(notification.sound),
            )
        )
        osascript = Path("/usr/bin/osascript")
        command = str(osascript) if osascript.is_file() else "osascript"
        if self._run([command, "-e", ghostty_script], timeout=5):
            return "ghostty-osascript"
        script = (
            'display notification "{}" with title "{}" subtitle "{}" '
            'sound name "{}"'.format(
                _apple_escape(notification.body),
                _apple_escape(notification.title),
                _apple_escape(notification.subtitle),
                _apple_escape(notification.sound),
            )
        )
        if self._run([command, "-e", script], timeout=5):
            return "osascript"
        return "failed"

    def _deliver_kimi(self, notification: Notification) -> str:
        app = Path(
            self.environment.get("AIPANE_KIMI_NOTIFIER_APP")
            or self.home / "Applications" / "Kimi Code Notifier.app"
        ).expanduser()
        notifier = app / "Contents" / "MacOS" / "kimi-notifier"
        if not _executable(notifier):
            return "sender-missing"
        arguments = [
            str(notifier),
            "-title",
            notification.title,
            "-subtitle",
            notification.subtitle,
            "-message",
            notification.body,
            "-group",
            notification.group,
        ]
        if notification.sound:
            arguments.extend(["-sound", notification.sound])
        if notification.action:
            arguments.extend(["-execute", notification.action])
        if self.run_command is not None:
            return "sent" if self._run(arguments, timeout=6) else "sender-error"
        try:
            result = subprocess.run(
                arguments,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=6,
                env=self.environment,
            )
        except subprocess.TimeoutExpired:
            return "sender-timeout"
        except OSError:
            return "sender-error"
        return "sent" if result.returncode == 0 else f"sender-exit-{result.returncode}"

    def _run(self, command: list[str], *, timeout: float) -> bool:
        if self.run_command is not None:
            try:
                return self.run_command(command, timeout)
            except Exception:
                return False
        try:
            subprocess.run(
                command,
                check=True,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=timeout,
                env=self.environment,
            )
            return True
        except (OSError, subprocess.SubprocessError):
            return False


class AgentNotifications:
    """Own notification adaptation and delivery for supported AI Tools."""

    def __init__(
        self,
        *,
        adapter: NotificationAdapter,
        environment: Mapping[str, str] | None = None,
        choose_sound: Callable[[tuple[str, ...]], str] | None = None,
    ) -> None:
        self.adapter = adapter
        self.environment = dict(environment or os.environ)
        self.choose_sound = choose_sound or secrets.choice

    def handle(
        self,
        agent: str,
        payload: Mapping[str, object] | None,
        *,
        preview: bool = False,
        phase: str = "ready",
    ) -> NotificationResult:
        normalized = dict(payload) if isinstance(payload, Mapping) else {}
        if agent == "cursor":
            notification = self._cursor_notification(normalized)
        elif agent == "claude":
            if any(
                self.environment.get(name)
                for name in (
                    "GROK_HOOK_EVENT",
                    "CURSOR_PROJECT_DIR",
                    "CURSOR_VERSION",
                )
            ):
                return NotificationResult(
                    outcome="ignored",
                    reason="compatibility_hook",
                )
            notification = self._claude_notification(normalized)
        elif agent == "grok":
            event = _compact(
                _pick(
                    normalized,
                    "event",
                    "hookEventName",
                    "hook_event_name",
                    "type",
                )
            ).lower()
            if event == "stop":
                reason = _compact(_pick(normalized, "reason")).lower()
                if reason != "end_turn":
                    return NotificationResult(
                        outcome="ignored",
                        reason="observation_only_stop",
                    )
            if phase == "initial" and event in {
                "stop",
                "turn_complete",
                "task_complete",
            }:
                return NotificationResult(
                    outcome="deferred",
                    reason="awaiting_transcript",
                )
            if phase == "deferred":
                self._wait_for_grok_transcript(normalized)
            notification = self._grok_notification(normalized)
        elif agent == "codex":
            notification, suppression_reason = self._codex_notification(normalized)
            if suppression_reason:
                if not preview:
                    self._write_codex_log(
                        normalized,
                        notification,
                        suppressed=True,
                        reason=suppression_reason,
                    )
                return NotificationResult(
                    outcome="suppressed",
                    notification=notification,
                    reason=suppression_reason,
                )
        elif agent == "kimi":
            notification = self._kimi_notification(normalized)
            if notification is None:
                return NotificationResult(
                    outcome="ignored",
                    reason="unsupported_event",
                )
        else:
            raise ValueError(f"unsupported notification agent: {agent}")
        if preview:
            return NotificationResult(
                outcome="previewed",
                notification=notification,
            )
        if agent == "grok" and self._grok_is_duplicate(normalized, notification):
            self._write_grok_log(normalized, notification, skipped=True)
            return NotificationResult(
                outcome="deduplicated",
                notification=notification,
                reason="duplicate_completion",
            )
        if agent == "kimi" and not self._claim_kimi(notification.group):
            self._write_kimi_log(notification, "deduplicated")
            return NotificationResult(
                outcome="deduplicated",
                notification=notification,
                reason="duplicate_event",
            )
        if agent == "grok":
            self._write_grok_log(normalized, notification, skipped=False)
        elif agent == "codex":
            self._write_codex_log(
                normalized,
                notification,
                suppressed=False,
                reason="",
            )
        channel = self.adapter.deliver(agent, notification)
        if agent == "cursor":
            self._write_cursor_log(normalized, notification, channel)
        elif agent == "kimi":
            self._write_kimi_log(notification, channel)
        return NotificationResult(
            outcome="failed" if _delivery_failed(channel) else "delivered",
            notification=notification,
            channel=channel,
        )

    def _cursor_notification(self, payload: dict[str, object]) -> Notification:
        status = _compact(payload.get("status") or "completed").lower()
        transcript_path = (
            payload.get("transcript_path")
            or self.environment.get("CURSOR_TRANSCRIPT_PATH")
            or ""
        )
        prompt, response = _cursor_transcript_messages(transcript_path)
        project_dir = _cursor_project_directory(payload, self.environment)
        title = _project_title(project_dir, "Cursor Agent")
        subtitle = _short_text(_first_plain_line(prompt), 80, "…")
        if status == "completed":
            body = (
                _short_text(_first_plain_line(response), 140, "…")
                or "当前任务已执行完成。"
            )
        elif status == "aborted":
            body = "任务已中止。"
        else:
            body = (
                _short_text(_first_plain_line(response), 140, "…")
                or "任务执行出错。"
            )
        conversation_id = _compact(
            payload.get("conversation_id")
            or payload.get("session_id")
            or "cursor"
        )
        digest = hashlib.sha1(conversation_id.encode("utf-8")).hexdigest()[:12]
        action = ""
        cursor_home = Path(
            self.environment.get("CURSOR_CONFIG_DIR")
            or Path(self.environment.get("HOME", str(Path.home()))) / ".cursor"
        ).expanduser()
        focus_script = cursor_home / "hooks" / "cursor-focus-tmux.sh"
        if focus_script.is_file():
            target = _tmux_focus_target(self.environment)
            if any(target):
                action = shlex.join([str(focus_script), *target])
        return Notification(
            title=title,
            subtitle=subtitle,
            body=body,
            sound=self.environment.get("CURSOR_NOTIFY_SOUND", "").strip()
            or self.choose_sound(COMPLETION_SOUNDS),
            event=str(payload.get("hook_event_name") or "stop"),
            group=f"cursor-agent-turn-{digest}",
            action=action,
        )

    def _claim_kimi(self, group: str) -> bool:
        home = Path(
            self.environment.get("HOME", str(Path.home()))
        ).expanduser()
        kimi_home = Path(
            self.environment.get("KIMI_CODE_HOME") or home / ".kimi-code"
        ).expanduser()
        state_dir = Path(
            self.environment.get("AIPANE_KIMI_NOTIFY_STATE_DIR")
            or kimi_home / "notifications"
        ).expanduser()
        directory = state_dir / "dedupe"
        _ensure_private_directory(directory)
        marker = directory / group
        now = time.time()
        try:
            descriptor = os.open(
                marker,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                0o600,
            )
        except FileExistsError:
            try:
                if now - marker.stat().st_mtime < 3.0:
                    return False
                os.utime(marker, (now, now))
                return True
            except OSError:
                return True
        except OSError:
            return True
        else:
            os.close(descriptor)
            return True

    def _write_kimi_log(
        self,
        notification: Notification,
        status: str,
    ) -> None:
        home = Path(
            self.environment.get("HOME", str(Path.home()))
        ).expanduser()
        kimi_home = Path(
            self.environment.get("KIMI_CODE_HOME") or home / ".kimi-code"
        ).expanduser()
        path = kimi_home / "logs" / "kimi-notify.log"
        try:
            _ensure_private_directory(path.parent)
            line = json.dumps(
                {
                    "time": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                    "event": notification.event,
                    "group": notification.group,
                    "pane": notification.pane_id,
                    "status": status,
                },
                separators=(",", ":"),
            )
            descriptor = os.open(
                path,
                os.O_CREAT | os.O_APPEND | os.O_WRONLY,
                0o600,
            )
            with os.fdopen(descriptor, "a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        except OSError:
            pass

    def _write_cursor_log(
        self,
        payload: Mapping[str, object],
        notification: Notification,
        channel: str,
    ) -> None:
        cursor_home = Path(
            self.environment.get("CURSOR_CONFIG_DIR")
            or Path(self.environment.get("HOME", str(Path.home()))) / ".cursor"
        ).expanduser()
        path = cursor_home / "hooks" / "cursor-notify.log"
        record = {
            "ts": time.time(),
            "event": payload.get("hook_event_name") or "stop",
            "status": _compact(payload.get("status") or "completed").lower(),
            "conversation_id": payload.get("conversation_id")
            or payload.get("session_id"),
            "project_dir": _cursor_project_directory(payload, self.environment),
            "channel": channel,
        }
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError:
            pass

    def _claude_notification(self, payload: dict[str, object]) -> Notification:
        cwd = str(payload.get("cwd") or self.environment.get("PWD") or "")
        title = self.environment.get("CLAUDE_NOTIFY_TITLE", "Claude Code")
        if cwd:
            title = _project_title(cwd, title)

        subtitle = ""
        session_id = _compact(payload.get("session_id"))
        if session_id:
            safe_session_id = re.sub(r"[^A-Za-z0-9_.-]", "_", session_id)
            home = Path(
                self.environment.get("HOME", str(Path.home()))
            ).expanduser()
            state_path = home / ".claude" / "hooks" / "state" / f"{safe_session_id}.json"
            try:
                state = json.loads(state_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                state = {}
            if isinstance(state, dict):
                subtitle = _short_text(
                    _first_compact_line(state.get("prompt")),
                    80,
                    "...",
                )

        assistant = _short_text(
            _first_compact_line(payload.get("last_assistant_message")),
            140,
            "...",
        )
        fallback = str(
            payload.get("_fallback_message")
            or self.environment.get("CLAUDE_NOTIFY_MESSAGE")
            or "任务已完成"
        )
        action = ""
        home = Path(
            self.environment.get("HOME", str(Path.home()))
        ).expanduser()
        focus_script = home / ".claude" / "hooks" / "focus-tmux.sh"
        if _executable(focus_script):
            target = _tmux_focus_target(self.environment)
            if any(target):
                action = shlex.join([str(focus_script), *target])
        return Notification(
            title=title,
            subtitle=subtitle,
            body=assistant or fallback,
            sound=self.environment.get("CLAUDE_NOTIFY_SOUND", "").strip()
            or self.choose_sound(COMPLETION_SOUNDS),
            event=str(payload.get("hook_event_name") or "stop"),
            group="claude-code-task-complete",
            action=action,
        )

    def _grok_notification(self, payload: dict[str, object]) -> Notification:
        event = _compact(
            _pick(payload, "event", "hookEventName", "hook_event_name", "type")
        ).lower()
        session_id = _compact(
            _pick(payload, "sessionId", "session_id")
            or self.environment.get("GROK_SESSION_ID")
        )
        recorded = _grok_recorded_prompt(session_id, self.environment)
        cwd = str(
            _pick(payload, "cwd", "workspaceRoot", "workspace_root", "workspace")
            or recorded.get("cwd")
            or self.environment.get("GROK_WORKSPACE_ROOT")
            or self.environment.get("CLAUDE_PROJECT_DIR")
            or self.environment.get("PWD")
            or os.getcwd()
        )
        project = _project_title(cwd, "Grok")
        coordinate = _tmux_coordinate(self.environment)
        title = f"{coordinate} · {project}" if coordinate else project

        transcript_prompt, transcript_message = _grok_transcript_messages(
            payload,
            self.environment,
        )
        prompt = transcript_prompt or _grok_user_prompt(payload, recorded)
        subtitle = _notification_subtitle(_first_compact_line(prompt))
        if not subtitle:
            subtitle = _notification_subtitle(_grok_event_subtitle(event))

        body_source = _grok_agent_message(payload) or transcript_message
        if not body_source and payload.get("source") == "env":
            body_source = str(payload.get("message") or "")
        if event in {"approval_required", "notification"} and not _first_compact_line(
            body_source
        ):
            body = _legacy_short_text(
                str(payload.get("message") or "需要你的确认"),
                140,
            )
        elif event in {"agent_error", "stopfailure", "stop_failure"}:
            body = (
                _legacy_short_text(_first_compact_line(body_source), 140)
                or "任务执行出错。"
            )
        else:
            body = (
                _legacy_short_text(_first_compact_line(body_source), 140)
                or "当前任务已执行完成。"
            )

        action = ""
        home = Path(
            self.environment.get("HOME", str(Path.home()))
        ).expanduser()
        focus_script = home / ".grok" / "bin" / "grok-focus-tmux.sh"
        if focus_script.is_file():
            target = _tmux_focus_target(self.environment)
            if any(target):
                action = shlex.join([str(focus_script), *target])
        return Notification(
            title=title,
            subtitle=subtitle,
            body=body,
            sound=self.environment.get("GROK_NOTIFY_SOUND", "").strip()
            or self.choose_sound(COMPLETION_SOUNDS),
            event=event,
            group="grok-agent-turn-complete",
            action=action,
        )

    def _wait_for_grok_transcript(
        self,
        payload: Mapping[str, object],
    ) -> None:
        if _grok_agent_message(payload):
            return
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            _, assistant = _grok_transcript_messages(
                payload,
                self.environment,
            )
            if assistant:
                return
            time.sleep(0.05)

    def _codex_notification(
        self,
        payload: dict[str, object],
    ) -> tuple[Notification, str]:
        cwd = str(payload.get("cwd") or payload.get("workspace") or "")
        thread_id = _compact(payload.get("thread-id") or payload.get("thread_id"))
        pane_title = _short_display_text(
            _codex_tmux_thread_title(self.environment),
            48,
        )
        database_title = ""
        if not pane_title:
            database_title = _short_display_text(
                _codex_root_thread_title(thread_id, self.environment),
                48,
            )
        original_title = pane_title or database_title or _project_title(cwd, "Codex")
        coordinate = _tmux_coordinate(self.environment)
        base_title = (
            f"{coordinate} · {original_title}" if coordinate else original_title
        )
        last_message = str(
            payload.get("last_agent_message")
            or payload.get("last-assistant-message")
            or payload.get("message")
            or payload.get("summary")
            or payload.get("raw")
            or ""
        )
        metadata = _codex_thread_metadata(payload, self.environment)
        if metadata.get("is_subagent"):
            depth = metadata.get("depth")
            level = f" · L{depth}" if isinstance(depth, int) else ""
            title = f"{base_title} · Subagent{level}"
            model_label = "/".join(
                part
                for part in (
                    str(metadata.get("model") or ""),
                    str(metadata.get("reasoning_effort") or ""),
                )
                if part
            )
            agent_label = " · ".join(
                part
                for part in (
                    str(metadata.get("task_name") or ""),
                    str(metadata.get("nickname") or ""),
                )
                if part
            )
            subtitle = _notification_subtitle(
                " · ".join(part for part in (model_label, agent_label) if part)
            )
            body = (
                _legacy_short_text(_result_summary(last_message), 140)
                or "当前子任务已执行完成。"
            )
            category = "subagent"
            goal_status = ""
            suppression_reason = ""
        else:
            title = base_title
            turn_id = _compact(payload.get("turn-id") or payload.get("turn_id"))
            goal = _codex_query_goal(thread_id, self.environment)
            if goal and _codex_turn_has_goal_context(
                thread_id,
                turn_id,
                self.environment,
                str(metadata.get("rollout_path") or ""),
            ):
                status = str(goal.get("status") or "")
                status_title = (
                    "Goal 执行中"
                    if status == "active"
                    else _GOAL_NOTIFICATION_TITLES.get(status, "Goal 状态未知")
                )
                title = f"{base_title} · {status_title}"
                subtitle = _notification_subtitle(goal.get("objective") or "")
                body = (
                    _legacy_short_text(_result_summary(last_message), 140)
                    or "当前 Goal 回合已结束。"
                )
                category = "goal"
                goal_status = status
                suppression_reason = "goal_active" if status == "active" else ""
            else:
                subtitle = _notification_subtitle(
                    _first_compact_line(_codex_latest_user_prompt(payload))
                )
                body = (
                    _legacy_short_text(_first_compact_line(last_message), 140)
                    or "当前任务已执行完成。"
                )
                category = "main"
                goal_status = ""
                suppression_reason = ""
        action = ""
        home = Path(
            self.environment.get("HOME", str(Path.home()))
        ).expanduser()
        codex_home = Path(
            self.environment.get("CODEX_HOME") or home / ".codex"
        ).expanduser()
        focus_script = codex_home / "bin" / "codex-focus-tmux.sh"
        if focus_script.exists():
            target = _tmux_focus_target(self.environment)
            if any(target):
                action = shlex.join([str(focus_script), *target])
        return (
            Notification(
                title=title,
                subtitle=subtitle,
                body=body,
                sound=self.environment.get("CODEX_NOTIFY_SOUND", "").strip()
                or self.choose_sound(COMPLETION_SOUNDS),
                event=str(payload.get("type") or "agent-turn-complete"),
                group="codex-agent-turn-complete-ghostty-clean",
                action=action,
                category=category,
                goal_status=goal_status,
            ),
            suppression_reason,
        )

    def _kimi_notification(
        self,
        payload: dict[str, object],
    ) -> Notification | None:
        event = _kimi_compact(
            payload.get("hook_event_name") or payload.get("event"),
            64,
        )
        tool_name = _kimi_compact(payload.get("tool_name"), 80)
        if event == "PermissionRequest" and tool_name == "AskUserQuestion":
            return None
        thread_title, prompt, answer = _kimi_session_content(
            payload,
            self.environment,
        )
        target = _kimi_tmux_target(self.environment)
        project = _kimi_project_name(payload.get("cwd"))
        base_title = thread_title or project
        numeric_session_id = target.get("session_id", "").removeprefix("$")
        if (
            numeric_session_id.isdigit()
            and target.get("window_index", "").isdigit()
            and target.get("pane_index", "").isdigit()
        ):
            coordinate = (
                f"{numeric_session_id}:{target['window_index']}:{target['pane_index']}"
            )
            base_title = f"{coordinate} · {base_title}"
        if event == "Stop":
            title = base_title
            subtitle = prompt or "任务完成"
            body = answer or "当前任务已执行完成。"
            sound = self.choose_sound(COMPLETION_SOUNDS)
            event_key = "complete"
        elif event == "StopFailure":
            title = f"{base_title} · 任务失败"
            subtitle = prompt or project
            error_type = _kimi_compact(payload.get("error_type"), 80)
            error_message = _kimi_compact(payload.get("error_message"))
            body = _kimi_compact(
                ": ".join(
                    part for part in (error_type, error_message) if part
                )
            ) or "任务执行失败。"
            sound = "default"
            event_key = "failure"
        elif event == "PermissionRequest":
            title = f"{base_title} · 等待确认"
            subtitle = prompt or project
            body = (
                _kimi_compact(payload.get("action"))
                or tool_name
                or "Kimi Code 需要你的确认。"
            )
            sound = "default"
            event_key = (
                "approval:"
                + (
                    _kimi_compact(payload.get("tool_call_id"), 120)
                    or tool_name
                )
            )
        elif event == "PreToolUse" and tool_name == "AskUserQuestion":
            title = f"{base_title} · 等待输入"
            subtitle = prompt or project
            body = _kimi_first_question(payload.get("tool_input"))
            sound = "default"
            event_key = (
                "question:"
                + (
                    _kimi_compact(payload.get("tool_call_id"), 120)
                    or "pending"
                )
            )
        else:
            return None
        session_id = _kimi_compact(payload.get("session_id"), 160) or "unknown"
        group_seed = f"{session_id}:{event_key}"
        action = ""
        home = Path(
            self.environment.get("HOME", str(Path.home()))
        ).expanduser()
        focus_script = Path(
            self.environment.get("AIPANE_KIMI_FOCUS")
            or home / ".local" / "bin" / "aipane-kimi-focus"
        ).expanduser()
        if focus_script.is_file() and target:
            action = shlex.join(
                [
                    str(focus_script),
                    target.get("session", ""),
                    target.get("window_id", ""),
                    target.get("pane_id", ""),
                    target.get("window_index", ""),
                ]
            )
        return Notification(
            title=title,
            subtitle=subtitle,
            body=body,
            sound=sound,
            event=event,
            group="kimi-"
            + hashlib.sha256(group_seed.encode("utf-8")).hexdigest()[:24],
            action=action,
            pane_id=target.get("pane_id", ""),
        )

    def _grok_is_duplicate(
        self,
        payload: Mapping[str, object],
        notification: Notification,
    ) -> bool:
        session_id = _compact(
            _pick(payload, "sessionId", "session_id")
            or self.environment.get("GROK_SESSION_ID")
        )
        event_class = (notification.event or "turn").replace("-", "_")
        if event_class in {"stop", "turn_complete", ""}:
            event_class = "turn_complete"
        elif event_class in {"stopfailure", "stop_failure", "agent_error"}:
            event_class = "agent_error"
        material = f"{session_id or notification.title}|{event_class}"
        digest = hashlib.sha1(material.encode("utf-8")).hexdigest()[:16]
        key = _safe_id(session_id or "nosession") + "-" + digest
        home = Path(
            self.environment.get("HOME", str(Path.home()))
        ).expanduser()
        directory = home / ".grok" / "logs" / "notify-dedup"
        path = directory / f"{key}.stamp"
        now = time.time()
        try:
            directory.mkdir(parents=True, exist_ok=True)
            if path.is_file() and now - path.stat().st_mtime < 8:
                return True
            path.write_text(
                json.dumps(
                    {
                        "ts": now,
                        "title": notification.title,
                        "subtitle": notification.subtitle,
                        "body": notification.body,
                        "event": notification.event,
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
        except OSError:
            return False
        try:
            for stamp in directory.glob("*.stamp"):
                if now - stamp.stat().st_mtime > 60:
                    stamp.unlink(missing_ok=True)
        except OSError:
            pass
        return False

    def _write_codex_log(
        self,
        payload: Mapping[str, object],
        notification: Notification,
        *,
        suppressed: bool,
        reason: str,
    ) -> None:
        path = _codex_home(self.environment) / "log" / "codex-notify.log"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(
                        {
                            "payload": dict(payload),
                            "title": notification.title,
                            "subtitle": notification.subtitle,
                            "body": notification.body,
                            "suppressed": suppressed,
                            "reason": reason,
                            "category": notification.category,
                            "goal_status": notification.goal_status,
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
        except OSError:
            pass

    def _write_grok_log(
        self,
        payload: Mapping[str, object],
        notification: Notification,
        *,
        skipped: bool,
    ) -> None:
        home = Path(
            self.environment.get("HOME", str(Path.home()))
        ).expanduser()
        path = home / ".grok" / "logs" / "grok-notify.log"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(
                        {
                            "payload": dict(payload),
                            "title": notification.title,
                            "subtitle": notification.subtitle,
                            "body": notification.body,
                            "skipped_dedup": skipped,
                            "ts": time.time(),
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
        except OSError:
            pass


def _compact(value: object) -> str:
    return " ".join(str(value or "").split())


def _kimi_compact(value: object, limit: int = 140) -> str:
    text = _compact(value)
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _kimi_project_name(value: object) -> str:
    directory = _kimi_compact(value, 240)
    if not directory:
        return "Kimi Code"
    return Path(directory).name or "Kimi Code"


def _kimi_first_question(tool_input: object) -> str:
    fallback = "Kimi Code 正在等待你的回答。"
    if not isinstance(tool_input, dict):
        return fallback
    questions = tool_input.get("questions")
    if not isinstance(questions, list) or not questions:
        return fallback
    first = questions[0]
    if not isinstance(first, dict):
        return fallback
    return _kimi_compact(first.get("question")) or fallback


def _kimi_first_line(value: object, limit: int) -> str:
    for line in str(value or "").splitlines():
        text = line.strip()
        text = re.sub(r"^#{1,6}\s+", "", text)
        text = re.sub(r"^[-*•]\s+", "", text)
        text = _MARKDOWN_LINK_RE.sub(r"\1", text)
        text = _kimi_compact(text, limit)
        if text:
            return text
    return ""


def _kimi_session_directory(
    session_id: object,
    environment: Mapping[str, str],
) -> Path | None:
    value = _kimi_compact(session_id, 160)
    if not _KIMI_SAFE_SESSION_ID_RE.fullmatch(value):
        return None
    home = Path(environment.get("HOME", str(Path.home()))).expanduser()
    kimi_home = Path(
        environment.get("KIMI_CODE_HOME") or home / ".kimi-code"
    ).expanduser()
    sessions_root = kimi_home / "sessions"
    try:
        resolved_root = sessions_root.resolve(strict=True)
        workspace_dirs = [sessions_root, *sessions_root.iterdir()]
    except OSError:
        return None
    for workspace_dir in workspace_dirs:
        candidate = workspace_dir / value
        try:
            resolved = candidate.resolve(strict=True)
        except OSError:
            continue
        if resolved.is_dir() and resolved.is_relative_to(resolved_root):
            return resolved
    return None


def _kimi_session_state(session_dir: Path | None) -> dict[str, object]:
    if session_dir is None:
        return {}
    try:
        value = json.loads((session_dir / "state.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _tail_lines(path: Path, byte_limit: int) -> list[str]:
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            start = max(0, size - byte_limit)
            handle.seek(start)
            data = handle.read()
    except OSError:
        return []
    if start:
        newline = data.find(b"\n")
        if newline < 0:
            return []
        data = data[newline + 1 :]
    return data.decode("utf-8", errors="replace").splitlines()


def _kimi_message_text(message: object) -> str:
    if isinstance(message, str):
        return message
    if isinstance(message, list):
        parts: list[str] = []
        for item in message:
            if not isinstance(item, dict) or item.get("type") != "text":
                continue
            text = item.get("text")
            if isinstance(text, str):
                parts.append(text)
        return "\n".join(parts)
    if not isinstance(message, dict):
        return ""
    if message.get("type") == "text" and isinstance(message.get("text"), str):
        return str(message["text"])
    return _kimi_message_text(message.get("content"))


def _kimi_latest_turn_content(session_dir: Path | None) -> tuple[str, str]:
    if session_dir is None:
        return "", ""
    paths = (
        session_dir / "agents" / "main" / "wire.jsonl",
        session_dir / "wire.jsonl",
    )
    wire_path = next((path for path in paths if path.is_file()), None)
    if wire_path is None:
        return "", ""
    prompt = ""
    answer = ""
    message_fallback = ""
    for raw_line in reversed(_tail_lines(wire_path, _KIMI_WIRE_TAIL_BYTES)):
        try:
            record = json.loads(raw_line)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict):
            continue
        record_type = record.get("type")
        if not prompt and record_type == "turn.prompt":
            prompt = _kimi_first_line(
                _kimi_message_text(record.get("input")),
                80,
            )
        if not answer and record_type == "context.append_loop_event":
            event = record.get("event")
            if isinstance(event, dict) and event.get("type") == "content.part":
                part = event.get("part")
                if isinstance(part, dict) and part.get("type") == "text":
                    answer = _kimi_first_line(part.get("text"), 140)
        message = (
            record.get("message")
            if record_type == "context.append_message"
            else record
        )
        if isinstance(message, dict) and message.get("role") == "assistant" and not answer:
            answer = _kimi_first_line(_kimi_message_text(message), 140)
        if isinstance(message, dict) and message.get("role") == "user" and not message_fallback:
            fallback = _kimi_first_line(_kimi_message_text(message), 80)
            if not fallback.startswith(("<system-reminder>", "<hook_result")):
                message_fallback = fallback
        if prompt and answer:
            break
    return prompt or message_fallback, answer


def _kimi_session_content(
    payload: Mapping[str, object],
    environment: Mapping[str, str],
) -> tuple[str, str, str]:
    directory = _kimi_session_directory(payload.get("session_id"), environment)
    state = _kimi_session_state(directory)
    title = _kimi_compact(
        payload.get("session_title") or state.get("title"),
        48,
    )
    wire_prompt, answer = _kimi_latest_turn_content(directory)
    prompt = _kimi_first_line(state.get("lastPrompt"), 80)
    return title, prompt or wire_prompt, answer


def _kimi_tmux_target(environment: Mapping[str, str]) -> dict[str, str]:
    pane = environment.get("TMUX_PANE", "")
    if not pane:
        return {}
    command = shlex.split(environment.get("AIPANE_TMUX", "tmux"))
    if not command:
        return {}
    try:
        result = subprocess.run(
            [
                *command,
                "display-message",
                "-p",
                "-t",
                pane,
                "#{session_id}\t#{session_name}\t#{window_id}\t#{pane_id}\t"
                "#{window_index}\t#{pane_index}",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=1.5,
            env=dict(environment),
        )
    except (OSError, subprocess.SubprocessError):
        return {"pane_id": pane}
    parts = result.stdout.strip().split("\t")
    if len(parts) != 6:
        return {"pane_id": pane}
    return dict(
        zip(
            (
                "session_id",
                "session",
                "window_id",
                "pane_id",
                "window_index",
                "pane_index",
            ),
            parts,
            strict=True,
        )
    )


def _executable(path: Path) -> bool:
    return path.is_file() and os.access(path, os.X_OK)


def _delivery_failed(channel: str) -> bool:
    return channel == "failed" or channel in {
        "sender-missing",
        "sender-timeout",
        "sender-error",
        "worker-error",
    } or channel.startswith("sender-exit-")


def _ensure_private_directory(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        directory.chmod(0o700)
    except OSError:
        pass


def _safe_id(value: object) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", str(value or "")) or "unknown"


def _apple_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _plain_text(value: object) -> str:
    text = _MARKDOWN_LINK_RE.sub(r"\1", str(value or ""))
    text = re.sub(r"^\s{0,3}#{1,6}\s*", "", text)
    text = re.sub(r"^\s*[-*+]\s+", "", text)
    return _compact(text.replace("`", "").replace("**", "").replace("__", ""))


def _first_plain_line(value: object) -> str:
    for line in str(value or "").splitlines():
        cleaned = _plain_text(line)
        if cleaned:
            return cleaned
    return ""


def _first_compact_line(value: object) -> str:
    for line in str(value or "").splitlines():
        text = _compact(line)
        if text:
            return text
    return ""


def _short_text(value: object, limit: int, suffix: str) -> str:
    text = _compact(value)
    if len(text) <= limit:
        return text
    return text[: limit - len(suffix)] + suffix


def _legacy_short_text(value: object, limit: int) -> str:
    text = _compact(value)
    if not text or len(text) <= limit:
        return text
    return text[: limit - 1] + "..."


def _display_width(value: object) -> int:
    width = 0
    for char in str(value or ""):
        if unicodedata.combining(char) or unicodedata.category(char) in {
            "Mn",
            "Me",
            "Cf",
        }:
            continue
        width += 2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1
    return width


def _short_display_text(value: object, limit: int) -> str:
    text = _compact(value)
    if not text or limit <= 0:
        return ""
    if _display_width(text) <= limit:
        return text
    suffix = "..."
    budget = max(0, limit - _display_width(suffix))
    result: list[str] = []
    width = 0
    for char in text:
        char_width = _display_width(char)
        if width + char_width > budget:
            break
        result.append(char)
        width += char_width
    return "".join(result) + suffix


def _notification_subtitle(value: object) -> str:
    image_marker = re.compile(r"\[\s*Image\s+#\d+\s*\]", re.IGNORECASE)
    text = _compact(value)
    had_image = bool(image_marker.search(text))
    text = _compact(image_marker.sub("", text))
    if not text and had_image:
        text = "图片请求"
    return _short_display_text(text, 48)


def _pick(payload: Mapping[str, object], *keys: str) -> object | None:
    for key in keys:
        if key in payload and payload[key] not in (None, ""):
            return payload[key]
    return None


def _grok_recorded_prompt(
    session_id: str,
    environment: Mapping[str, str],
) -> dict[str, object]:
    if not session_id:
        return {}
    safe_id = re.sub(r"[^A-Za-z0-9_.-]", "_", session_id) or "unknown"
    home = Path(environment.get("HOME", str(Path.home()))).expanduser()
    path = home / ".grok" / "hooks" / "state" / f"{safe_id}.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _reverse_jsonl(path: Path, byte_limit: int):
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            end = handle.tell()
            start = max(0, end - byte_limit)
            handle.seek(start)
            data = handle.read()
    except OSError:
        return
    if start:
        newline = data.find(b"\n")
        if newline < 0:
            return
        data = data[newline + 1 :]
    for raw_line in reversed(data.splitlines()):
        try:
            item = json.loads(raw_line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if isinstance(item, dict):
            yield item


def _grok_transcript_messages(
    payload: Mapping[str, object],
    environment: Mapping[str, str],
) -> tuple[str, str]:
    value = _pick(payload, "transcriptPath", "transcript_path", "transcript")
    transcripts: list[Path] = []
    if isinstance(value, str) and value.strip():
        transcripts.append(Path(value).expanduser())
    else:
        session_id = _compact(_pick(payload, "sessionId", "session_id"))
        home = Path(environment.get("HOME", str(Path.home()))).expanduser()
        sessions_dir = home / ".grok" / "sessions"
        if session_id and sessions_dir.is_dir():
            safe_session_id = re.sub(r"[^A-Za-z0-9_.-]", "_", session_id)
            try:
                transcripts.extend(
                    sorted(
                        sessions_dir.glob(f"*/{safe_session_id}/updates.jsonl"),
                        key=lambda path: path.stat().st_mtime,
                        reverse=True,
                    )
                )
            except OSError:
                pass

    candidates: list[Path] = []
    for transcript in transcripts:
        if transcript not in candidates:
            candidates.append(transcript)
        history = transcript.with_name("chat_history.jsonl")
        if history not in candidates:
            candidates.append(history)

    best_assistant = ""
    for path in candidates:
        if not path.is_file():
            continue
        assistant = ""
        for item in _reverse_jsonl(path, _GROK_TRANSCRIPT_TAIL_BYTES):
            params = item.get("params")
            update = params.get("update") if isinstance(params, dict) else None
            if isinstance(update, dict) and update:
                kind = str(update.get("sessionUpdate") or "")
                if kind == "agent_message_chunk" and not assistant:
                    assistant = _content_text(update.get("content")).strip()
                elif kind == "user_message_chunk":
                    user = _clean_user_prompt(_content_text(update.get("content")))
                    if user:
                        return user, assistant or best_assistant
                    break

            role = str(item.get("role") or item.get("type") or "").lower()
            if role in {"assistant", "agent"} and not assistant:
                assistant = _content_text(item.get("content")).strip()
            elif role in {"user", "human"}:
                user = _clean_user_prompt(_content_text(item.get("content")))
                if user:
                    return user, assistant or best_assistant
                break
        if assistant and not best_assistant:
            best_assistant = assistant
    return "", best_assistant


def _grok_user_prompt(
    payload: Mapping[str, object],
    recorded: Mapping[str, object],
) -> str:
    direct = _pick(
        payload,
        "prompt",
        "userPrompt",
        "user_prompt",
        "last_user_message",
        "lastUserMessage",
    )
    if isinstance(direct, str) and direct.strip():
        return _clean_user_prompt(direct)
    messages = (
        _pick(payload, "input-messages", "input_messages", "userMessages", "messages")
        or []
    )
    if isinstance(messages, list):
        for message in reversed(messages):
            if isinstance(message, str) and message.strip():
                return _clean_user_prompt(message)
            if not isinstance(message, dict):
                continue
            role = str(message.get("role") or message.get("type") or "").lower()
            if role not in {"", "user", "human"}:
                continue
            content = (
                message.get("content")
                or message.get("message")
                or message.get("text")
                or message.get("prompt")
            )
            text = _content_text(content)
            if text.strip():
                return _clean_user_prompt(text)
    recorded_prompt = recorded.get("prompt")
    return _clean_user_prompt(recorded_prompt) if isinstance(recorded_prompt, str) else ""


def _grok_agent_message(payload: Mapping[str, object]) -> str:
    value = _pick(
        payload,
        "last_agent_message",
        "last-assistant-message",
        "last_assistant_message",
        "lastAssistantMessage",
        "assistant_message",
        "assistantMessage",
        "message",
        "summary",
        "text",
        "raw",
    )
    return _content_text(value)


def _grok_event_subtitle(event: str) -> str:
    mapping = {
        "turn_complete": "turn complete",
        "stop": "turn complete",
        "approval_required": "需要你的确认",
        "agent_error": "agent error",
        "stopfailure": "agent error",
        "stop_failure": "agent error",
        "task_complete": "task complete",
        "session_ready": "session ready",
        "notification": "notification",
    }
    return mapping.get(event.replace("-", "_"), event or "")


def _codex_latest_user_prompt(payload: Mapping[str, object]) -> str:
    messages = payload.get("input-messages") or payload.get("input_messages") or []
    if not isinstance(messages, list):
        return ""
    for message in reversed(messages):
        if isinstance(message, str):
            return message
        if isinstance(message, dict):
            content = (
                message.get("content")
                or message.get("message")
                or message.get("text")
            )
            if isinstance(content, str):
                return content
    return ""


def _codex_home(environment: Mapping[str, str]) -> Path:
    home = Path(environment.get("HOME", str(Path.home()))).expanduser()
    return Path(environment.get("CODEX_HOME") or home / ".codex").expanduser().resolve()


def _parse_json_object(value: object) -> dict[str, object]:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str) or not value:
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _codex_normalize_thread_metadata(row: Mapping[str, object]) -> dict[str, object]:
    source = _parse_json_object(row.get("source"))
    subagent_value = source.get("subagent") or source.get("sub_agent") or {}
    subagent = subagent_value if isinstance(subagent_value, dict) else {}
    spawn_value = subagent.get("thread_spawn") or subagent.get("threadSpawn") or {}
    thread_spawn = spawn_value if isinstance(spawn_value, dict) else {}
    agent_path = str(row.get("agent_path") or thread_spawn.get("agent_path") or "")
    nickname = str(
        row.get("agent_nickname") or thread_spawn.get("agent_nickname") or ""
    )
    role = str(row.get("agent_role") or thread_spawn.get("agent_role") or "")
    parent_thread_id = str(
        row.get("parent_thread_id")
        or thread_spawn.get("parent_thread_id")
        or ""
    )
    depth = thread_spawn.get("depth")
    if not isinstance(depth, int) or isinstance(depth, bool):
        parts = [part for part in agent_path.split("/") if part]
        depth = max(0, len(parts) - 1) if parts and parts[0] == "root" else None
    thread_source = str(row.get("thread_source") or "")
    is_subagent = bool(
        thread_source == "subagent"
        or parent_thread_id
        or agent_path
        or thread_spawn
    )
    task_name = agent_path.rstrip("/").rsplit("/", 1)[-1] if agent_path else ""
    return {
        "is_subagent": is_subagent,
        "thread_source": thread_source,
        "parent_thread_id": parent_thread_id,
        "depth": depth,
        "agent_path": agent_path,
        "task_name": task_name,
        "nickname": nickname,
        "role": role,
        "model": str(row.get("model") or ""),
        "reasoning_effort": str(row.get("reasoning_effort") or ""),
        "rollout_path": str(row.get("rollout_path") or ""),
    }


def _codex_query_thread_metadata(
    thread_id: str,
    environment: Mapping[str, str],
) -> dict[str, object]:
    if not thread_id:
        return {}
    codex_home = _codex_home(environment)
    for path in (codex_home / "state_5.sqlite", codex_home / "sqlite" / "state_5.sqlite"):
        if not path.is_file():
            continue
        try:
            uri = path.resolve().as_uri() + "?mode=ro"
            with closing(sqlite3.connect(uri, uri=True, timeout=0.25)) as connection:
                connection.row_factory = sqlite3.Row
                try:
                    row = connection.execute(
                        """
                        SELECT
                            t.thread_source,
                            t.source,
                            t.agent_path,
                            t.agent_nickname,
                            t.agent_role,
                            t.model,
                            t.reasoning_effort,
                            t.rollout_path,
                            e.parent_thread_id
                        FROM threads AS t
                        LEFT JOIN thread_spawn_edges AS e
                          ON e.child_thread_id = t.id
                        WHERE t.id = ?
                        """,
                        (thread_id,),
                    ).fetchone()
                except sqlite3.OperationalError:
                    row = connection.execute(
                        """
                        SELECT
                            t.thread_source,
                            t.source,
                            t.agent_path,
                            t.agent_nickname,
                            t.agent_role,
                            t.model,
                            t.reasoning_effort,
                            t.rollout_path,
                            NULL AS parent_thread_id
                        FROM threads AS t
                        WHERE t.id = ?
                        """,
                        (thread_id,),
                    ).fetchone()
                if row is not None:
                    return _codex_normalize_thread_metadata(dict(row))
        except (OSError, sqlite3.Error):
            continue
    return {}


def _codex_root_thread_title(
    thread_id: str,
    environment: Mapping[str, str],
) -> str:
    if not thread_id:
        return ""
    codex_home = _codex_home(environment)
    for path in (codex_home / "state_5.sqlite", codex_home / "sqlite" / "state_5.sqlite"):
        if not path.is_file():
            continue
        try:
            uri = path.resolve().as_uri() + "?mode=ro"
            with closing(sqlite3.connect(uri, uri=True, timeout=0.25)) as connection:
                connection.row_factory = sqlite3.Row
                try:
                    row = connection.execute(
                        """
                        WITH RECURSIVE ancestry(id, title, depth) AS (
                            SELECT id, title, 0
                            FROM threads
                            WHERE id = ?
                            UNION ALL
                            SELECT parent.id, parent.title, ancestry.depth + 1
                            FROM ancestry
                            JOIN thread_spawn_edges AS edge
                              ON edge.child_thread_id = ancestry.id
                            JOIN threads AS parent
                              ON parent.id = edge.parent_thread_id
                            WHERE ancestry.depth < 32
                        )
                        SELECT title
                        FROM ancestry
                        ORDER BY depth DESC
                        LIMIT 1
                        """,
                        (thread_id,),
                    ).fetchone()
                except sqlite3.OperationalError:
                    row = connection.execute(
                        "SELECT title FROM threads WHERE id = ?",
                        (thread_id,),
                    ).fetchone()
                if row is not None:
                    return _compact(row["title"] or "")
        except (OSError, sqlite3.Error):
            continue
    return ""


def _codex_rollout_candidates(
    thread_id: str,
    environment: Mapping[str, str],
    rollout_path: str = "",
) -> list[Path]:
    candidates: list[Path] = []
    if rollout_path:
        candidates.append(Path(rollout_path))
    if thread_id:
        codex_home = _codex_home(environment)
        for root_name in ("sessions", "archived_sessions"):
            root = codex_home / root_name
            if root.is_dir():
                candidates.extend(root.rglob(f"*-{thread_id}.jsonl"))
    return candidates


def _codex_query_goal(
    thread_id: str,
    environment: Mapping[str, str],
) -> dict[str, object]:
    if not thread_id:
        return {}
    codex_home = _codex_home(environment)
    for path in (
        codex_home / "goals_1.sqlite",
        codex_home / "sqlite" / "goals_1.sqlite",
    ):
        if not path.is_file():
            continue
        try:
            uri = path.resolve().as_uri() + "?mode=ro"
            with closing(sqlite3.connect(uri, uri=True, timeout=0.25)) as connection:
                connection.row_factory = sqlite3.Row
                row = connection.execute(
                    """
                    SELECT goal_id, objective, status
                    FROM thread_goals
                    WHERE thread_id = ?
                    """,
                    (thread_id,),
                ).fetchone()
                if row is not None:
                    return dict(row)
        except (OSError, sqlite3.Error):
            continue
    return {}


def _iter_jsonl_reverse(path: Path, chunk_size: int = 64 * 1024):
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            position = handle.tell()
            pending = b""
            while position > 0:
                size = min(chunk_size, position)
                position -= size
                handle.seek(position)
                lines = (handle.read(size) + pending).split(b"\n")
                pending = lines[0]
                for raw_line in reversed(lines[1:]):
                    if not raw_line.strip():
                        continue
                    try:
                        yield json.loads(raw_line.decode("utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        continue
            if pending.strip():
                try:
                    yield json.loads(pending.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    pass
    except OSError:
        return


def _codex_response_item_text(payload: Mapping[str, object]) -> str:
    content = payload.get("content") or []
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    texts: list[str] = []
    for item in content:
        if isinstance(item, str):
            texts.append(item)
        elif isinstance(item, dict) and isinstance(item.get("text"), str):
            texts.append(str(item["text"]))
    return "\n".join(texts)


def _codex_turn_has_goal_context(
    thread_id: str,
    turn_id: str,
    environment: Mapping[str, str],
    rollout_path: str = "",
) -> bool:
    if not thread_id or not turn_id:
        return False
    seen: set[Path] = set()
    for candidate in _codex_rollout_candidates(
        thread_id,
        environment,
        rollout_path,
    ):
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if resolved in seen or not resolved.is_file():
            continue
        seen.add(resolved)
        for record in _iter_jsonl_reverse(resolved):
            if not isinstance(record, dict):
                continue
            payload = record.get("payload") or {}
            if not isinstance(payload, dict):
                continue
            if record.get("type") == "response_item" and payload.get("role") == "user":
                metadata = payload.get("internal_chat_message_metadata_passthrough") or {}
                if not isinstance(metadata, dict) or metadata.get("turn_id") != turn_id:
                    continue
                text = _codex_response_item_text(payload)
                if _GOAL_CONTEXT_RE.search(text) or "<goal_context>" in text:
                    return True
            if (
                record.get("type") == "event_msg"
                and payload.get("type") == "task_started"
                and payload.get("turn_id") == turn_id
            ):
                break
    return False


def _codex_rollout_thread_metadata(
    thread_id: str,
    environment: Mapping[str, str],
    rollout_path: str = "",
) -> dict[str, object]:
    seen: set[Path] = set()
    for candidate in _codex_rollout_candidates(
        thread_id,
        environment,
        rollout_path,
    ):
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if resolved in seen or not resolved.is_file():
            continue
        seen.add(resolved)
        try:
            with resolved.open("r", encoding="utf-8") as handle:
                for _ in range(12):
                    line = handle.readline()
                    if not line:
                        break
                    record = json.loads(line)
                    if record.get("type") != "session_meta":
                        continue
                    payload = record.get("payload") or {}
                    if not isinstance(payload, dict):
                        break
                    metadata = dict(payload)
                    metadata["rollout_path"] = str(resolved)
                    return _codex_normalize_thread_metadata(metadata)
        except (OSError, json.JSONDecodeError):
            continue
    return {}


def _codex_thread_metadata(
    payload: Mapping[str, object],
    environment: Mapping[str, str],
) -> dict[str, object]:
    thread_id = _compact(payload.get("thread-id") or payload.get("thread_id"))
    metadata = _codex_query_thread_metadata(thread_id, environment)
    if metadata.get("is_subagent"):
        return metadata
    rollout = _codex_rollout_thread_metadata(
        thread_id,
        environment,
        str(metadata.get("rollout_path") or ""),
    )
    return rollout if rollout.get("is_subagent") else metadata


def _codex_clean_tmux_thread_title(value: object) -> str:
    text = _compact(value)
    for prefix in _CODEX_TITLE_ACTION_PREFIXES:
        if text == prefix:
            return ""
        marker = f"{prefix} | "
        if text.startswith(marker):
            return _compact(text[len(marker) :])
    if text in _CODEX_TITLE_SPINNER_FRAMES:
        return ""
    for spinner in _CODEX_TITLE_SPINNER_FRAMES:
        marker = f"{spinner} "
        if text.startswith(marker):
            return _compact(text[len(marker) :])
    return text


def _codex_tmux_thread_title(environment: Mapping[str, str]) -> str:
    pane_id = environment.get("TMUX_PANE", "")
    if not pane_id:
        return ""
    configured = environment.get("AIPANE_TMUX") or environment.get("TMUX_BIN")
    command = shlex.split(configured) if configured else ["tmux"]
    if not command:
        return ""
    try:
        result = subprocess.run(
            [*command, "display-message", "-p", "-t", pane_id, "#{pane_title}"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=2,
            env=dict(environment),
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return _codex_clean_tmux_thread_title(result.stdout)


def _plain_notification_text(value: object) -> str:
    text = _MARKDOWN_LINK_RE.sub(r"\1", str(value or ""))
    text = re.sub(r"^\s{0,3}#{1,6}\s*", "", text)
    text = re.sub(r"^\s*[-*+]\s+", "", text)
    return _compact(text.replace("`", "").replace("**", "").replace("__", ""))


def _result_summary(value: object) -> str:
    lines = [line for line in str(value or "").splitlines() if line.strip()]
    pairs = [
        (line, _plain_notification_text(line))
        for line in lines
        if _plain_notification_text(line)
    ]
    if not pairs:
        return ""
    while len(pairs) > 1 and pairs[0][1] in {
        "结论",
        "完成内容",
        "验证结果",
        "结果",
    }:
        pairs.pop(0)
    first = pairs[0][1]
    modified = re.match(r"^已完成[，,]\s*仅修改\s*(.*?)[。.]?$", first)
    if modified:
        target = modified.group(1).strip().rstrip("：:")
        if not target:
            targets: list[str] = []
            for raw_line, cleaned_line in pairs[1:]:
                if not re.match(r"^\s*[-*+]\s+", raw_line):
                    break
                for file_name in _FILE_TOKEN_RE.findall(cleaned_line):
                    if file_name not in targets:
                        targets.append(file_name)
            if targets:
                suffix = "等" if len(targets) > 3 else ""
                target = "、".join(targets[:3]) + suffix
        if target:
            return f"已完成 {target} 改造"
    return first


def _content_text(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = [_content_text(item) for item in value]
        return "\n".join(part for part in parts if part)
    if not isinstance(value, dict):
        return ""
    if value.get("type") == "text" and isinstance(value.get("text"), str):
        return str(value["text"])
    for key in ("text", "content", "message"):
        nested = value.get(key)
        if isinstance(nested, (str, list, dict)):
            text = _content_text(nested)
            if text:
                return text
    return ""


def _clean_user_prompt(value: object) -> str:
    text = str(value or "").strip()
    matches = re.findall(
        r"<user_query(?:\s[^>]*)?>\s*(.*?)\s*</user_query>",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    return (matches[-1] if matches else text).strip()


def _cursor_transcript_messages(path_value: object) -> tuple[str, str]:
    if not isinstance(path_value, str) or not path_value.strip():
        return "", ""
    path = Path(path_value).expanduser()
    if not path.is_file():
        return "", ""
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            end = handle.tell()
            start = max(0, end - _CURSOR_TRANSCRIPT_TAIL_BYTES)
            handle.seek(start)
            data = handle.read()
    except OSError:
        return "", ""
    if start:
        newline = data.find(b"\n")
        if newline < 0:
            return "", ""
        data = data[newline + 1 :]
    user_message = ""
    assistant_message = ""
    for raw_line in reversed(data.splitlines()):
        try:
            item = json.loads(raw_line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or item.get("type") or "").lower()
        text = _content_text(item.get("message", item)).strip()
        if not text:
            continue
        if role in {"assistant", "agent"} and not assistant_message:
            assistant_message = text
        elif role in {"user", "human"} and not user_message:
            user_message = _clean_user_prompt(text)
        if user_message and assistant_message:
            break
    return user_message, assistant_message


def _cursor_project_directory(
    payload: Mapping[str, object],
    environment: Mapping[str, str],
) -> str:
    for value in (
        environment.get("CURSOR_PROJECT_DIR"),
        environment.get("CLAUDE_PROJECT_DIR"),
        payload.get("cwd"),
        payload.get("workspace"),
    ):
        if isinstance(value, str) and value.strip():
            return str(Path(value).expanduser())
    roots = payload.get("workspace_roots")
    if isinstance(roots, list):
        for root in roots:
            if isinstance(root, str) and root.strip():
                return str(Path(root).expanduser())
    return ""


def _project_title(directory: str, fallback: str) -> str:
    if not directory:
        return fallback
    try:
        result = subprocess.run(
            ["git", "-C", directory, "rev-parse", "--show-toplevel"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=2,
        )
        root = result.stdout.strip()
        if root:
            return Path(root).name
    except Exception:
        pass
    return Path(directory).name or fallback


def _tmux_focus_target(
    environment: Mapping[str, str],
) -> tuple[str, str, str, str]:
    pane_id = environment.get("TMUX_PANE", "")
    if not pane_id:
        return "", "", "", ""
    configured = environment.get("AIPANE_TMUX") or environment.get("TMUX_BIN")
    if configured:
        command = shlex.split(configured)
    elif Path("/opt/homebrew/bin/tmux").exists():
        command = ["/opt/homebrew/bin/tmux"]
    else:
        command = ["tmux"]
    if not command:
        return "", "", pane_id, ""
    try:
        result = subprocess.run(
            [
                *command,
                "display-message",
                "-p",
                "-t",
                pane_id,
                "#{session_name}\t#{window_id}\t#{pane_id}\t#{window_index}",
            ],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=2,
            env=dict(environment),
        )
    except (OSError, subprocess.SubprocessError):
        return "", "", pane_id, ""
    parts = result.stdout.strip().split("\t")
    if len(parts) != 4:
        return "", "", pane_id, ""
    return parts[0], parts[1], parts[2], parts[3]


def _tmux_coordinate(environment: Mapping[str, str]) -> str:
    pane_id = environment.get("TMUX_PANE", "")
    if not pane_id:
        return ""
    configured = environment.get("AIPANE_TMUX") or environment.get("TMUX_BIN")
    command = shlex.split(configured) if configured else ["tmux"]
    if not command:
        return ""
    try:
        result = subprocess.run(
            [
                *command,
                "display-message",
                "-p",
                "-t",
                pane_id,
                "#{session_id}\t#{window_index}\t#{pane_index}",
            ],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=2,
            env=dict(environment),
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    parts = result.stdout.strip().split("\t")
    if len(parts) != 3:
        return ""
    session_id = parts[0].removeprefix("$")
    window_index, pane_index = parts[1:]
    if not all(part.isdecimal() for part in (session_id, window_index, pane_index)):
        return ""
    return f"{session_id}:{window_index}:{pane_index}"
