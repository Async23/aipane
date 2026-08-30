"""Contract tests for the shared random notification-sound pool."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
COMPLETION_SOUNDS = {"Glass", "Ping", "Pop", "Purr", "Submarine", "Tink"}
NOTIFIERS = (
    (
        ROOT / "bin" / "aipane-codex-desktop-notify",
        "CODEX_NOTIFY_DRY_RUN",
        "CODEX_NOTIFY_SOUND",
    ),
    (
        ROOT / "bin" / "aipane-grok-notify",
        "GROK_NOTIFY_DRY_RUN",
        "GROK_NOTIFY_SOUND",
    ),
    (
        ROOT / "bin" / "aipane-claude-notify",
        "CLAUDE_NOTIFY_DRY_RUN",
        "CLAUDE_NOTIFY_SOUND",
    ),
    (
        ROOT / "bin" / "aipane-cursor-notify",
        "CURSOR_NOTIFY_DRY_RUN",
        "CURSOR_NOTIFY_SOUND",
    ),
)


class AgentNotificationSoundTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.home = Path(self.temp_dir.name)

    def tearDown(self):
        self.temp_dir.cleanup()

    def run_notifier(
        self,
        notifier: Path,
        dry_run_variable: str,
        sound_variable: str,
        fixed_sound: str | None = None,
    ) -> dict:
        environment = os.environ.copy()
        environment.update(
            {
                "HOME": str(self.home),
                "CODEX_HOME": str(self.home / ".codex"),
                "CURSOR_CONFIG_DIR": str(self.home / ".cursor"),
                "TMUX_PANE": "",
                dry_run_variable: "1",
            }
        )
        environment.pop(sound_variable, None)
        if fixed_sound is not None:
            environment[sound_variable] = fixed_sound
        result = subprocess.run(
            [str(notifier)],
            input="{}",
            text=True,
            capture_output=True,
            check=False,
            env=environment,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_each_notifier_uses_the_shared_random_pool(self):
        for notifier, dry_run_variable, sound_variable in NOTIFIERS:
            with self.subTest(notifier=notifier.name):
                observed = {
                    self.run_notifier(
                        notifier,
                        dry_run_variable,
                        sound_variable,
                    )["sound"]
                    for _ in range(12)
                }
                self.assertLessEqual(observed, COMPLETION_SOUNDS)
                self.assertGreater(len(observed), 1)

    def test_each_notifier_supports_a_fixed_sound_override(self):
        for notifier, dry_run_variable, sound_variable in NOTIFIERS:
            with self.subTest(notifier=notifier.name):
                notification = self.run_notifier(
                    notifier,
                    dry_run_variable,
                    sound_variable,
                    fixed_sound="Ping",
                )
                self.assertEqual(notification["sound"], "Ping")


if __name__ == "__main__":
    unittest.main()
