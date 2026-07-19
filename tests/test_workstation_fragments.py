"""Structural tests for public Ghostty/tmux workstation fragments.

Drives the shipped conf files under conf/ (not re-implemented copies).
"""

from __future__ import annotations

import re
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GHOSTTY_CONF = ROOT / "conf" / "ghostty-tmux.conf"
TMUX_WS = ROOT / "conf" / "tmux-workstation.conf"
TMUX_WRAP = ROOT / "conf" / "tmux-window-wrap.conf"
WRAP_BIN = ROOT / "bin" / "tmux-window-wrap"
CHEATSHEET = ROOT / "docs" / "cheatsheet.md"
WS_DOC = ROOT / "docs" / "ghostty-tmux-workstation.md"


class GhosttyFragmentTests(unittest.TestCase):
    def test_ghostty_fragment_defines_one_shot_cmd_bridge_and_shift_enter(self):
        text = GHOSTTY_CONF.read_text(encoding="utf-8")
        self.assertIn("keybind = cmd+s=unbind", text)
        self.assertNotIn("keybind = cmd+s=text:\\x00", text)
        self.assertIn("keybind = super+alt+p=text:\\x00Q", text)
        self.assertIn("keybind = cmd+t=text:\\x00c", text)
        self.assertIn("keybind = cmd+d=text:\\x00|", text)
        self.assertIn("keybind = shift+enter=text:\\x0a", text)
        self.assertIn("macos-option-as-alt = true", text)
        # public fragment must not pin personal fonts
        self.assertNotIn("font-family", text)


class TmuxWorkstationFragmentTests(unittest.TestCase):
    def test_workstation_sets_c_space_prefix_and_broadcast(self):
        text = TMUX_WS.read_text(encoding="utf-8")
        self.assertIn("set -g prefix C-Space", text)
        self.assertIn("synchronize-panes", text)
        self.assertIn("window_zoomed_flag", text)
        self.assertIn("bind v copy-mode", text)
        self.assertIn("bind Q display-popup", text)
        self.assertIn("tmux-shot-capture", text)
        # stay a fragment: no TPM / no personal home paths
        self.assertNotIn("@plugin", text)
        self.assertNotIn("/Users/", text)

    def test_window_wrap_fragment_and_binary_exist(self):
        self.assertTrue(TMUX_WRAP.is_file())
        self.assertTrue(WRAP_BIN.is_file())
        wrap = TMUX_WRAP.read_text(encoding="utf-8")
        self.assertIn("tmux-window-wrap render", wrap)
        self.assertIn("status-format[0]", wrap)

    def test_workstation_sources_cleanly_in_tmux(self):
        socket = f"ws-frag-{Path(__file__).stat().st_mtime_ns}"
        try:
            subprocess.run(
                [
                    "tmux",
                    "-L",
                    socket,
                    "-f",
                    "/dev/null",
                    "new-session",
                    "-d",
                    "-s",
                    "t",
                    "sleep 5",
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            subprocess.run(
                ["tmux", "-L", socket, "source-file", str(TMUX_WS)],
                check=True,
                capture_output=True,
                text=True,
            )
            subprocess.run(
                ["tmux", "-L", socket, "source-file", str(TMUX_WRAP)],
                check=True,
                capture_output=True,
                text=True,
            )
            prefix = subprocess.run(
                ["tmux", "-L", socket, "display-message", "-p", "#{prefix}"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            self.assertEqual(prefix, "C-Space")
            keys = subprocess.run(
                ["tmux", "-L", socket, "list-keys", "-T", "prefix"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout
            self.assertRegex(keys, re.compile(r"prefix\s+B\s+.*synchronize-panes"))
            self.assertRegex(keys, re.compile(r"prefix\s+v\s+.*copy-mode"))
            self.assertRegex(keys, re.compile(r"prefix\s+Q\s+.*display-popup"))
            fmt0 = subprocess.run(
                [
                    "tmux",
                    "-L",
                    socket,
                    "show-options",
                    "-g",
                    "-v",
                    "status-format[0]",
                ],
                check=True,
                capture_output=True,
                text=True,
            ).stdout
            self.assertIn("tmux-window-wrap", fmt0)
        finally:
            subprocess.run(
                ["tmux", "-L", socket, "kill-server"],
                check=False,
                capture_output=True,
            )


class DocsPointerTests(unittest.TestCase):
    def test_docs_exist_and_name_public_confs(self):
        self.assertTrue(CHEATSHEET.is_file())
        self.assertTrue(WS_DOC.is_file())
        doc = WS_DOC.read_text(encoding="utf-8")
        self.assertIn("conf/ghostty-tmux.conf", doc)
        self.assertIn("conf/tmux-workstation.conf", doc)
        self.assertIn("conf/tmux-window-wrap.conf", doc)
        self.assertIn("`Cmd+Opt+P`", doc)

        cheatsheet = CHEATSHEET.read_text(encoding="utf-8")
        self.assertIn("| `Cmd+Opt+P` | popup pane ID list |", cheatsheet)
        self.assertNotIn("| `Cmd+S` | prefix only |", cheatsheet)


if __name__ == "__main__":
    unittest.main()
