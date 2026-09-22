import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FOCUS = ROOT / "bin/aipane-dsh-focus"


@unittest.skipUnless(shutil.which("tmux"), "tmux required")
class DshFocusTests(unittest.TestCase):
    def test_original_socket_pane_and_missing_pane_fallback(self):
        with tempfile.TemporaryDirectory() as raw:
            temporary = Path(raw)
            socket = str(temporary / "tmux.sock")
            log = temporary / "focus.log"
            environment = {**os.environ, "AIPANE_DSH_SKIP_ACTIVATE": "1",
                           "AIPANE_DSH_FOCUS_LOG": str(log)}
            environment.pop("TMUX", None)

            def tmux(*args):
                return subprocess.run(["tmux", "-S", socket, *args], check=True,
                                      env=environment, capture_output=True,
                                      text=True).stdout.strip()

            def focus(pane, chosen_socket=socket):
                subprocess.run([str(FOCUS), "focus-test", "@0", pane, "0",
                                chosen_socket], env=environment, check=True,
                               capture_output=True, text=True)

            try:
                tmux("-f", "/dev/null", "new-session", "-d", "-s", "focus-test",
                     "sleep 60")
                tmux("split-window", "-t", "%0", "sleep 60")
                tmux("new-window", "-t", "focus-test", "sleep 60")
                focus("%0")
                self.assertEqual(tmux("display-message", "-p", "-t", "focus-test",
                                      "#{window_id}:#{pane_id}"), "@0:%0")
                self.assertEqual(json.loads(log.read_text().splitlines()[-1])["status"],
                                 "pane-selected")
                tmux("kill-pane", "-t", "%0")
                tmux("select-window", "-t", "@1")
                focus("%0")
                self.assertEqual(tmux("display-message", "-p", "-t", "focus-test",
                                      "#{window_id}"), "@0")
                self.assertEqual(json.loads(log.read_text().splitlines()[-1])["status"],
                                 "window-selected")
                focus("%1", str(temporary / "nonexistent.sock"))
                self.assertEqual(json.loads(log.read_text().splitlines()[-1])["status"],
                                 "ghostty-only")
            finally:
                subprocess.run(["tmux", "-S", socket, "kill-server"], env=environment,
                               capture_output=True, check=False)


if __name__ == "__main__":
    unittest.main()
