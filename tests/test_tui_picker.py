"""Unit and real-PTY tests for the reusable modeless fzf terminal picker."""

from __future__ import annotations

import fcntl
import io
import json
import os
from pathlib import Path
import pty
import select
import shutil
import signal
import struct
import subprocess
import sys
import termios
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))
import tui_picker


HARNESS = """
import json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import tui_picker

rows = tuple(tui_picker.PickerRow(**item) for item in json.loads(sys.argv[2]))
page = tui_picker.PickerPage(
    header_wide="Model            Meta",
    header_narrow="Model",
    rows=rows,
    default_index=int(sys.argv[3]),
    border_label=" Generic · Pick ",
    subtitle="Choose an entry",
    ghost="Filter items",
)
try:
    selected = tui_picker.select(page)
    print("SELECTED=" + selected, flush=True)
except KeyboardInterrupt:
    print("CANCELLED", flush=True)
    raise SystemExit(130)
"""


SAMPLE_ROWS = [
    {"key": "alpha", "wide_summary": "Alpha Model      Fast", "narrow_summary": "Alpha Model",
     "details": "alpha-detail", "search_text": "alpha-slug fast", "plain_label": "Alpha Model"},
    {"key": "beta", "wide_summary": "Beta Model       Deep", "narrow_summary": "Beta Model",
     "details": "beta-detail", "search_text": "beta-slug deep", "plain_label": "Beta Model"},
    {"key": "gamma", "wide_summary": "Gamma Special    Max", "narrow_summary": "Gamma Special",
     "details": "gamma-detail", "search_text": "gamma-slug max", "plain_label": "Gamma Special"},
]


class TuiPickerUnitTests(unittest.TestCase):
    def test_cell_width_handles_cjk_and_ansi(self):
        self.assertEqual(tui_picker.cell_width("abc"), 3)
        self.assertEqual(tui_picker.cell_width("模型"), 4)
        self.assertEqual(tui_picker.cell_width("\x1b[34m模型\x1b[0m"), 4)

    def test_cell_truncates_and_pads_to_exact_width(self):
        padded = tui_picker.cell("模型", 6)
        self.assertEqual(tui_picker.cell_width(padded), 6)
        clipped = tui_picker.cell("很长的中文模型名称", 7)
        self.assertLessEqual(tui_picker.cell_width(clipped), 7)
        self.assertIn("…", clipped)

    def test_responsive_fzf_height_fits_short_split_panes(self):
        total, preview = tui_picker._fzf_height(10, 14)
        self.assertLessEqual(total, 14)
        self.assertEqual(preview, 3)
        total_tall, preview_tall = tui_picker._fzf_height(10, 36)
        self.assertLessEqual(total_tall, 36)
        self.assertEqual(preview_tall, 5)

    def test_text_fallback_matches_index_and_tokens(self):
        rows = tuple(tui_picker.PickerRow(**item) for item in SAMPLE_ROWS)
        page = tui_picker.PickerPage("H", "H", rows, default_index=2, border_label=" Pick ")
        with patch.object(sys, "stdin", io.StringIO("\n")), patch.object(sys, "stderr", io.StringIO()):
            self.assertEqual(tui_picker.select_with_text(page), "beta")
        with patch.object(sys, "stdin", io.StringIO("gamma max\n")), patch.object(sys, "stderr", io.StringIO()):
            self.assertEqual(tui_picker.select_with_text(page), "gamma")
        with patch.object(sys, "stdin", io.StringIO("q\n")), patch.object(sys, "stderr", io.StringIO()):
            with self.assertRaises(KeyboardInterrupt):
                tui_picker.select_with_text(page)


class GenericPickerPTY:
    def __init__(self, *, columns=96, lines=24, default_index=2):
        self.master, self.slave = pty.openpty()
        fcntl.ioctl(self.slave, termios.TIOCSWINSZ, struct.pack("HHHH", lines, columns, 0, 0))
        self.before = termios.tcgetattr(self.slave)
        env = dict(os.environ, TERM="xterm-256color")
        for key in ("LINES", "COLUMNS"):
            env.pop(key, None)
        self.proc = subprocess.Popen(
            [sys.executable, "-c", HARNESS, str(ROOT / "lib"), json.dumps(SAMPLE_ROWS), str(default_index)],
            stdin=self.slave, stdout=self.slave, stderr=self.slave,
            env=env, start_new_session=True,
        )
        self.output = b""
        self.tail = b""

    def wait_for(self, text: str, timeout: float = 5.0):
        target = text.encode()
        deadline = time.monotonic() + timeout
        while target not in self.output:
            if time.monotonic() >= deadline:
                raise AssertionError(f"did not observe {text!r}: {self.output[-600:]!r}")
            if select.select([self.master], [], [], 0.05)[0]:
                chunk = os.read(self.master, 65536)
                pending = self.tail + chunk
                for _ in range(pending.count(b"\x1b[6n")):
                    os.write(self.master, b"\x1b[1;1R")
                self.tail = pending[-3:]
                self.output += chunk
            elif self.proc.poll() is not None:
                raise AssertionError(f"exited early: {self.output[-600:]!r}")

    def send(self, data: bytes):
        self.output = b""
        os.write(self.master, data)

    def finish(self, expected: str, code: int = 0):
        self.wait_for(expected)
        if self.proc.wait(timeout=3) != code:
            raise AssertionError(f"unexpected exit code {self.proc.returncode}")
        if termios.tcgetattr(self.slave) != self.before:
            raise AssertionError("terminal settings not restored")

    def close(self):
        if self.proc.poll() is None:
            os.killpg(self.proc.pid, signal.SIGTERM)
            self.proc.wait(timeout=3)
        os.close(self.master)
        os.close(self.slave)


@unittest.skipUnless(shutil.which("fzf"), "fzf is not installed")
class TuiPickerPTYTests(unittest.TestCase):
    def spawn(self, **kwargs):
        p = GenericPickerPTY(**kwargs)
        self.addCleanup(p.close)
        p.wait_for("beta-detail")
        return p

    def test_enter_launches_default_and_restores_tty(self):
        p = self.spawn()
        p.send(b"\r")
        p.finish("SELECTED=beta")

    def test_search_matches_visible_row_and_esc_clears_before_abort(self):
        p = self.spawn()
        p.send(b"Gamma Max")
        p.wait_for("gamma-detail")
        p.send(b"\x1b")
        p.wait_for("3/3")
        self.assertIsNone(p.proc.poll())
        p.send(b"\x1b")
        p.finish("CANCELLED", 130)

    def test_short_split_pane_runs_fzf_cleanly(self):
        p = self.spawn(columns=60, lines=14)
        p.send(b"\x1b[B\r")
        p.finish("SELECTED=gamma")

    def test_bracketed_paste_does_not_accidental_submit_without_enter(self):
        p = self.spawn()
        p.send(b"\x1b[200~Alpha Fast\x1b[201~")
        p.wait_for("alpha-detail")
        time.sleep(0.1)
        self.assertIsNone(p.proc.poll())
        p.send(b"\r")
        p.finish("SELECTED=alpha")


if __name__ == "__main__":
    unittest.main()
