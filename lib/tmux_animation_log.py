"""Bounded animator event logs, shared safely during process handoff."""

import datetime
import fcntl
import json
import os
from pathlib import Path
import sys
import time


DEFAULT_LOG = "~/.local/state/aipane/tmux-window-wrap-animate.log"
MAX_LOG_BYTES = 1024 * 1024
LOG_BACKUPS = 3


class AnimationLog:
    def __init__(
        self, path, *, max_bytes=MAX_LOG_BYTES, backups=LOG_BACKUPS, **context
    ):
        self.path = Path(path).expanduser().absolute()
        self.max_bytes = max_bytes
        self.backups = backups
        self.context = {"pid": os.getpid(), **context}
        self._probe_errors = ()
        self._probe_errors_at = 0.0

    def emit(self, event, *, level="info", **fields):
        record = {
            "time": datetime.datetime.now().astimezone().isoformat(
                timespec="milliseconds"
            ),
            "level": level,
            **self.context,
            "event": event,
            **fields,
        }
        data = (json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # Both the outgoing and incoming animator can log during reload.
            # Lock rotation and reopen the current file for each event so neither
            # process keeps writing through a descriptor to a rotated backup.
            with self.path.with_name(self.path.name + ".lock").open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                size = self.path.stat().st_size if self.path.exists() else 0
                if size and size + len(data) > self.max_bytes:
                    for index in range(self.backups, 0, -1):
                        source = (
                            self.path if index == 1
                            else self.path.with_name(f"{self.path.name}.{index - 1}")
                        )
                        if source.exists():
                            source.replace(
                                self.path.with_name(f"{self.path.name}.{index}")
                            )
                with self.path.open("ab") as handle:
                    handle.write(data)
        except OSError as error:
            # Logging must never become another reason for the animation to die.
            try:
                sys.stderr.write(
                    f"tmux-window-wrap log_write_failed: {error}\n"
                    + data.decode("utf-8")
                )
            except OSError:
                pass

    def probe_errors(self, errors):
        errors = tuple(str(error)[:2048] for error in errors)
        now = time.monotonic()
        if errors:
            if errors != self._probe_errors or now - self._probe_errors_at >= 60:
                self.emit(
                    "activity_probe_errors", level="warning",
                    errors=list(errors[:10]), count=len(errors),
                )
                self._probe_errors_at = now
        elif self._probe_errors:
            self.emit("activity_probe_recovered")
        self._probe_errors = errors
