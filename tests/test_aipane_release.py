"""A failed or edited development tree cannot replace the installed runtime."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
RELEASE = ROOT / "bin/aipane-release"


class AipaneReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.source = self.base / "source"
        self.source.mkdir()
        self.runtime = self.base / "runtime"
        self.bin = self.base / "bin"
        self.git("init", "-q")
        (self.source / "bin").mkdir()
        (self.source / "tests").mkdir()
        shutil.copy2(RELEASE, self.source / "bin/aipane-release")
        for name in ("ai-restart", "ai-restore", "aipane-bind", "aipane-snapshot", "aipane-restore-executor"):
            script = self.source / "bin" / name
            script.write_text("#!/bin/sh\nprintf 'one\\n'\n")
            script.chmod(0o755)
        self.validator(True)
        self.commit()

    def git(self, *args):
        return subprocess.check_output(["git", "-c", "user.name=Aipane Tests", "-c", "user.email=test@localhost",
                                        "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false", *args],
                                       cwd=self.source, stderr=subprocess.PIPE, text=True)

    def commit(self):
        self.git("add", "--all")
        self.git("commit", "-qm", "fixture")

    def validator(self, passed):
        (self.source / "tests/test_smoke.py").write_text(
            f"import unittest\nclass Smoke(unittest.TestCase):\n def test_ready(self): self.assertTrue({passed})\n")

    def run_release(self, action):
        return subprocess.run([sys.executable, str(RELEASE), action, "--source", str(self.source),
                               "--root", str(self.runtime), "--bin-dir", str(self.bin)],
                              env={**os.environ, "AIPANE_STATE_DIR": str(self.base / "state")},
                              text=True, capture_output=True)

    def installed_output(self):
        return subprocess.check_output([str(self.bin / "ai-restart")], text=True).strip()

    def test_development_edits_do_not_change_runtime_or_pass_installation(self):
        result = self.run_release("install")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        (self.source / "bin/ai-restart").write_text("#!/bin/sh\nprintf 'development\\n'\n")
        self.assertEqual(self.installed_output(), "one")
        self.assertNotEqual(self.run_release("install").returncode, 0)
        self.assertEqual(self.installed_output(), "one")

    def test_failed_validation_keeps_previous_runtime(self):
        self.assertEqual(self.run_release("install").returncode, 0)
        before = (self.runtime / "current").resolve()
        self.validator(False)
        self.commit()
        result = self.run_release("install")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("validation failed", result.stderr)
        self.assertEqual((self.runtime / "current").resolve(), before)
        self.assertEqual(self.installed_output(), "one")

    def test_activation_and_rollback_switch_all_entry_points_together(self):
        self.assertEqual(self.run_release("install").returncode, 0)
        first = (self.runtime / "current").resolve()
        (self.source / "bin/ai-restart").write_text("#!/bin/sh\nprintf 'two\\n'\n")
        self.commit()
        self.assertEqual(self.run_release("install").returncode, 0)
        self.assertEqual(self.installed_output(), "two")
        self.assertEqual((self.runtime / "previous").resolve(), first)
        self.assertEqual(self.run_release("rollback").returncode, 0)
        self.assertEqual(self.installed_output(), "one")
        self.assertTrue((self.bin / "aipane-release").is_file())


if __name__ == "__main__":
    unittest.main()
