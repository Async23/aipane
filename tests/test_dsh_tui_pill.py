import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "bin/aipane-dsh-tui-pill"
SCREEN = Path(
    "profiles/dsh-tui/node_modules/@deepseek-harness-tui/dsh-tui"
    "/lib/types/screens/Chat.js"
)
UPSTREAM = "    const showPill = !isSticky;\n"
PATCHED = "    const showPill = false;\n"


class PillPatchScriptTest(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix="aipane-pill-"))
        self.screen = self.home / SCREEN
        self.screen.parent.mkdir(parents=True)
        self.screen.write_text(
            "const before = 1;\n" + UPSTREAM + "const after = 2;\n", encoding="utf-8"
        )

    def tearDown(self):
        shutil.rmtree(self.home, ignore_errors=True)

    def run_pill(self, *arguments):
        return subprocess.run(
            [sys.executable, str(SCRIPT), *arguments],
            env={**os.environ, "DSH_HOME": str(self.home)},
            capture_output=True, text=True, check=False,
        )

    def test_script_is_executable(self):
        self.assertTrue(os.access(SCRIPT, os.X_OK), f"{SCRIPT} is not executable")

    def test_apply_check_and_restore_round_trip(self):
        missing = self.run_pill("check")
        self.assertEqual(missing.returncode, 1, missing.stderr)

        applied = self.run_pill("apply")
        self.assertEqual(applied.returncode, 0, applied.stderr)
        text = self.screen.read_text(encoding="utf-8")
        self.assertIn(PATCHED, text)
        self.assertNotIn(UPSTREAM, text)
        # The edit replaces exactly one line and preserves its neighbours.
        self.assertIn("const before = 1;", text)
        self.assertIn("const after = 2;", text)

        self.assertEqual(self.run_pill("check").returncode, 0)
        # Re-applying is a no-op rather than a second edit.
        again = self.run_pill("apply")
        self.assertEqual(again.returncode, 0)
        self.assertIn("already", again.stdout)
        self.assertEqual(self.screen.read_text(encoding="utf-8"), text)

        restored = self.run_pill("restore")
        self.assertEqual(restored.returncode, 0, restored.stderr)
        self.assertIn(UPSTREAM, self.screen.read_text(encoding="utf-8"))
        self.assertEqual(self.run_pill("check").returncode, 1)

    def test_unknown_source_is_refused(self):
        self.screen.write_text("const showPill = computed();\n", encoding="utf-8")
        result = self.run_pill("apply")
        self.assertEqual(result.returncode, 2)
        self.assertIn("no known", result.stderr)
        self.assertEqual(self.screen.read_text(encoding="utf-8"),
                         "const showPill = computed();\n")

    def test_duplicate_source_is_refused(self):
        self.screen.write_text(UPSTREAM + UPSTREAM, encoding="utf-8")
        result = self.run_pill("apply")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(self.screen.read_text(encoding="utf-8"), UPSTREAM + UPSTREAM)

    def test_missing_screen_is_reported(self):
        shutil.rmtree(self.screen.parent)
        for action in ("apply", "check", "restore"):
            with self.subTest(action=action):
                self.assertEqual(self.run_pill(action).returncode, 2)


if __name__ == "__main__":
    unittest.main()
