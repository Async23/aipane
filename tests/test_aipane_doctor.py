import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DOCTOR = ROOT / "bin" / "aipane-doctor"


class AipaneDoctorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        temporary_root = Path(self.temporary.name)
        self.root = temporary_root / "aipane"
        self.home = temporary_root / "home"
        self.local_bin = self.home / ".local" / "bin"
        self.local_bin.mkdir(parents=True)
        (self.root / "bin").mkdir(parents=True)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def executable(self, name: str) -> Path:
        path = self.root / "bin" / name
        path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        path.chmod(0o755)
        return path

    def link(self, name: str) -> None:
        (self.local_bin / name).symlink_to(self.executable(name))

    def configure_core(self) -> None:
        for name in ("aipane-doctor", "aipane-activity", "tmux-window-wrap"):
            self.link(name)

    def run_doctor(self) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                str(DOCTOR),
                "--root",
                str(self.root),
                "--home",
                str(self.home),
            ],
            check=False,
            capture_output=True,
            text=True,
            env={**os.environ, "NO_COLOR": "1"},
        )

    def test_healthy_core_installation_passes(self) -> None:
        self.configure_core()
        hooks = self.home / ".codex" / "hooks.json"
        hooks.parent.mkdir(parents=True)
        hooks.write_text(
            '{"command":"$HOME/.local/bin/aipane-activity report busy"}',
            encoding="utf-8",
        )

        result = self.run_doctor()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("PASS aipane doctor", result.stdout)
        self.assertIn("canonical activity registrations: 1", result.stdout)

    def test_missing_canonical_entrypoint_fails(self) -> None:
        self.configure_core()
        (self.local_bin / "aipane-activity").unlink()

        result = self.run_doctor()

        self.assertEqual(result.returncode, 1)
        self.assertIn("FAIL ~/.local/bin/aipane-activity: missing", result.stdout)

    def test_wrong_symlink_target_fails(self) -> None:
        self.configure_core()
        wrong = self.home / "wrong-aipane-activity"
        wrong.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        wrong.chmod(0o755)
        destination = self.local_bin / "aipane-activity"
        destination.unlink()
        destination.symlink_to(wrong)

        result = self.run_doctor()

        self.assertEqual(result.returncode, 1)
        self.assertIn("does not point to this checkout", result.stdout)

    def test_wrong_registration_symlink_target_fails(self) -> None:
        self.configure_core()
        expected = self.root / "integrations" / "grok" / "hooks.json"
        expected.parent.mkdir(parents=True)
        expected.write_text("{}", encoding="utf-8")
        wrong = self.home / "wrong-grok-hooks.json"
        wrong.write_text("{}", encoding="utf-8")
        registration = self.home / ".grok" / "hooks" / "aipane-activity.json"
        registration.parent.mkdir(parents=True)
        registration.symlink_to(wrong)

        result = self.run_doctor()

        self.assertEqual(result.returncode, 1)
        self.assertIn(
            "registration does not point to this checkout",
            result.stdout,
        )

    def test_referenced_wrapper_must_be_installed_from_this_checkout(self) -> None:
        self.configure_core()
        settings = self.home / ".claude" / "settings.json"
        settings.parent.mkdir(parents=True)
        settings.write_text(
            '{"command":"$HOME/.local/bin/aipane-claude-activity"}',
            encoding="utf-8",
        )
        self.executable("aipane-claude-activity")

        result = self.run_doctor()

        self.assertEqual(result.returncode, 1)
        self.assertIn(
            "FAIL ~/.local/bin/aipane-claude-activity: missing",
            result.stdout,
        )

    def test_escaped_local_bin_reference_is_discovered(self) -> None:
        self.configure_core()
        config = self.home / ".codex" / "config.toml"
        config.parent.mkdir(parents=True)
        config.write_text(
            r'notify = ["[\"\\/tmp\\/.local\\/bin\\/aipane-codex-notify\"]"]',
            encoding="utf-8",
        )
        self.executable("aipane-codex-notify")

        result = self.run_doctor()

        self.assertEqual(result.returncode, 1)
        self.assertIn(
            "FAIL ~/.local/bin/aipane-codex-notify: missing",
            result.stdout,
        )

    def test_legacy_activity_command_is_an_explicit_warning(self) -> None:
        self.configure_core()
        hooks = self.home / ".cursor" / "hooks.json"
        hooks.parent.mkdir(parents=True)
        hooks.write_text(
            '{"command":"$HOME/.local/bin/tmux-window-wrap activity busy"}',
            encoding="utf-8",
        )

        result = self.run_doctor()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("WARN legacy activity command", result.stdout)
        self.assertIn("compatibility references: 1", result.stdout)


if __name__ == "__main__":
    unittest.main()
