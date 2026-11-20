"""Exercise the real fzf UI in a PTY without starting Claude or calling an API."""

import fcntl
import io
import json
import os
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
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "integrations/claude"
sys.path.insert(0, str(SCRIPTS))
import model_picker

OPTIONS = [
    {"model": "gpt-sol", "label": "GPT Sol"},
    {"model": "kimi-pro", "label": "Kimi Pro"},
    {"model": "grok-fast", "label": "Grok Fast"},
    {"model": "j-k-special", "label": "J/K Special"},
]
MODELS = {
    "gpt-sol": {"context_tokens": 500_000, "billing_source": "OpenAI Codex 订阅"},
    "kimi-pro": {"context_tokens": 1_048_576, "billing_source": "Kimi Code 额度"},
    "grok-fast": {"context_tokens": 500_000, "billing_source": "Grok Build 额度"},
    "j-k-special": {"context_tokens": 500_000, "billing_source": "测试"},
}
HARNESS = """
import json, sys
sys.path.insert(0, sys.argv[1])
from model_picker import select_model
try:
    selected = select_model(json.loads(sys.argv[2]), json.loads(sys.argv[3]), sys.argv[4])
    print('SELECTED=' + selected, flush=True)
except KeyboardInterrupt:
    print('CANCELLED', flush=True)
    raise SystemExit(130)
"""


class PickerPTY:
    def __init__(self, columns=100, lines=30, default="grok-fast", extra_env=None):
        self.default = default
        self.master, self.slave = pty.openpty()
        self.resize(columns, lines)
        self.before = termios.tcgetattr(self.slave)
        env = dict(os.environ, TERM="xterm-256color")
        for key in ("LINES", "COLUMNS"):
            env.pop(key, None)
        env.update(extra_env or {})
        self.proc = subprocess.Popen(
            [sys.executable, "-c", HARNESS, str(SCRIPTS), json.dumps(OPTIONS),
             json.dumps(MODELS), default],
            stdin=self.slave, stdout=self.slave, stderr=self.slave,
            env=env, start_new_session=True,
        )
        self.output = b""
        self.control_tail = b""

    def resize(self, columns, lines):
        fcntl.ioctl(self.slave, termios.TIOCSWINSZ, struct.pack("HHHH", lines, columns, 0, 0))
        if hasattr(self, "proc"):
            os.killpg(self.proc.pid, signal.SIGWINCH)

    def wait_for(self, text, timeout=5):
        target = text.encode()
        deadline = time.monotonic() + timeout
        while target not in self.output:
            if time.monotonic() >= deadline:
                raise AssertionError(f"UI did not show {text!r}: {self.output[-600:]!r}")
            if select.select([self.master], [], [], .05)[0]:
                chunk = os.read(self.master, 65536)
                # Inline fzf asks the terminal for its cursor position. A PTY
                # alone is not a terminal emulator, so reply as the emulator.
                pending = self.control_tail + chunk
                for _ in range(pending.count(b"\x1b[6n")):
                    os.write(self.master, b"\x1b[1;1R")
                self.control_tail = pending[-3:]
                self.output += chunk
            elif self.proc.poll() is not None:
                raise AssertionError(f"UI exited early: {self.output[-600:]!r}")

    def send(self, keys):
        self.output = b""
        os.write(self.master, keys)

    def ready(self):
        self.wait_for(self.default)

    def finish(self, expected, code=0):
        self.wait_for(expected)
        if self.proc.wait(timeout=3) != code:
            raise AssertionError("Unexpected exit code")
        if termios.tcgetattr(self.slave) != self.before:
            raise AssertionError("Terminal settings were not restored")

    def close(self):
        if self.proc.poll() is None:
            os.killpg(self.proc.pid, signal.SIGTERM)
            self.proc.wait(timeout=3)
        os.close(self.master)
        os.close(self.slave)


@unittest.skipUnless(shutil.which("fzf"), "fzf is not installed")
class InteractivePickerTests(unittest.TestCase):
    def picker(self, **kwargs):
        picker = PickerPTY(**kwargs)
        self.addCleanup(picker.close)
        picker.ready()
        return picker

    def test_enter_launches_highlighted_default_immediately_and_restores_terminal(self):
        picker = self.picker()
        picker.send(b"\r")
        picker.finish("SELECTED=grok-fast")

    def test_arrow_changes_selection_and_enter_launches(self):
        picker = self.picker()
        picker.send(b"\x1b[A")
        picker.wait_for("kimi-pro")
        picker.send(b"\r")
        picker.finish("SELECTED=kimi-pro")

    def test_ctrl_j_and_ctrl_k_move_selection(self):
        picker = self.picker()
        picker.send(b"\x0b")
        picker.wait_for("kimi-pro")
        picker.send(b"\x0a")
        picker.wait_for("grok-fast")
        picker.send(b"\r")
        picker.finish("SELECTED=grok-fast")

    def test_navigation_wraps_at_both_ends(self):
        for key, default, expected in (
            (b"\x0a", "j-k-special", "gpt-sol"),
            (b"\x0b", "gpt-sol", "j-k-special"),
            (b"\x1b[B", "j-k-special", "gpt-sol"),
            (b"\x1b[A", "gpt-sol", "j-k-special"),
        ):
            with self.subTest(key=key):
                picker = self.picker(default=default)
                picker.send(key + b"\r")
                picker.finish("SELECTED=" + expected)

    def test_navigation_wraps_within_filtered_results(self):
        for key, default, expected in (
            (b"\x0a", "j-k-special", "kimi-pro"),
            (b"\x0b", "kimi-pro", "j-k-special"),
            (b"\x1b[B", "j-k-special", "kimi-pro"),
            (b"\x1b[A", "kimi-pro", "j-k-special"),
        ):
            with self.subTest(key=key):
                picker = self.picker(default=default)
                picker.send(b"!sol")
                picker.wait_for("3/4")
                # Search updates can move the cursor; page to the boundary first.
                picker.send(b"\x1b[6~" if default == "j-k-special" else b"\x1b[5~")
                picker.wait_for(default)
                picker.send(key + b"\r")
                picker.finish("SELECTED=" + expected)

    def test_search_accepts_j_k_and_slash_as_text_and_launches_on_single_enter(self):
        picker = self.picker()
        picker.send(b"j/k")
        picker.wait_for("j-k-special")
        picker.send(b"\r")
        picker.finish("SELECTED=j-k-special")

    def test_escape_clears_query_first_then_aborts_when_empty(self):
        picker = self.picker()
        picker.send(b"pro")
        picker.wait_for("kimi-pro")
        picker.send(b"\x1b")
        picker.wait_for("4/4")
        self.assertIsNone(picker.proc.poll())
        picker.send(b"\x1b")
        picker.finish("CANCELLED", 130)

    def test_escape_clears_query_and_allows_immediate_re_search(self):
        picker = self.picker()
        picker.send(b"pro")
        picker.wait_for("kimi-pro")
        picker.send(b"\x1b")
        picker.wait_for("4/4")
        picker.send(b"j/k")
        picker.wait_for("j-k-special")
        picker.send(b"\r")
        picker.finish("SELECTED=j-k-special")

    def test_ctrl_u_clears_query_and_keeps_picker_open(self):
        picker = self.picker()
        picker.send(b"pro")
        picker.wait_for("kimi-pro")
        picker.send(b"\x15")
        picker.wait_for("4/4")
        picker.send(b"j/k")
        picker.wait_for("j-k-special")
        picker.send(b"\r")
        picker.finish("SELECTED=j-k-special")

    def test_ctrl_u_on_empty_query_is_safe_noop(self):
        picker = self.picker()
        picker.send(b"\x15\x0b")
        picker.wait_for("kimi-pro")
        picker.send(b"\r")
        picker.finish("SELECTED=kimi-pro")

    def test_escape_clears_no_results_and_recovers(self):
        picker = self.picker()
        picker.send(b"zzzzzz")
        picker.wait_for("没有匹配的模型")
        picker.send(b"\r")
        time.sleep(.1)
        self.assertIsNone(picker.proc.poll())
        picker.send(b"\x1b")
        picker.wait_for("4/4")
        picker.send(b"sol")
        picker.wait_for("gpt-sol")
        picker.send(b"\r")
        picker.finish("SELECTED=gpt-sol")

    def test_search_and_bracketed_paste_launches_on_single_enter(self):
        picker = self.picker()
        picker.send(b"\x1b[200~Kimi Pro\x1b[201~")
        picker.wait_for("kimi-pro")
        picker.send(b"\r")
        picker.finish("SELECTED=kimi-pro")

    def test_mouse_selects_the_clicked_row(self):
        picker = self.picker()
        # With header-first + persistent search prompt + column header, Kimi Pro is on row 7.
        picker.send(b"\x1b[<0;12;7M\x1b[<0;12;7m")
        picker.wait_for("kimi-pro")
        picker.send(b"\r")
        picker.finish("SELECTED=kimi-pro")

    def test_escape_on_empty_query_and_ctrl_c_cancel_and_restore_terminal(self):
        for key in (b"\x1b", b"\x03"):
            with self.subTest(key=key):
                picker = self.picker()
                picker.send(key)
                picker.finish("CANCELLED", 130)

    def test_ctrl_c_cancels_even_with_active_query(self):
        picker = self.picker()
        picker.send(b"kimi")
        picker.wait_for("kimi-pro")
        picker.send(b"\x03")
        picker.finish("CANCELLED", 130)

    def test_narrow_terminal_and_resize(self):
        picker = self.picker(columns=48, lines=18)
        picker.resize(100, 30)
        picker.send(b"kimi")
        picker.wait_for("kimi-pro")
        picker.resize(48, 18)
        picker.send(b"\r")
        picker.finish("SELECTED=kimi-pro")

    def test_global_fzf_options_cannot_auto_accept_or_replace_choices(self):
        picker = self.picker(extra_env={"FZF_DEFAULT_OPTS": "--filter=wrong --bind=start:accept",
                                       "FZF_DEFAULT_OPTS_FILE": "/does/not/exist"})
        picker.send(b"\r")
        picker.finish("SELECTED=grok-fast")


class TextPickerTests(unittest.TestCase):
    def pick(self, text, default="grok-fast"):
        with patch("sys.stdin", io.StringIO(text)), patch("sys.stderr", io.StringIO()):
            return model_picker.select_with_text(OPTIONS, MODELS, default)

    def test_default_number_and_name(self):
        self.assertEqual(self.pick("\n"), "grok-fast")
        self.assertEqual(self.pick("2\n"), "kimi-pro")
        self.assertEqual(self.pick("kimi\n"), "kimi-pro")

    def test_eof_and_q_cancel(self):
        for value in ("", "q\n"):
            with self.assertRaises(KeyboardInterrupt):
                self.pick(value)

    def test_invalid_input_retries(self):
        self.assertEqual(self.pick("unknown\n2\n"), "kimi-pro")

    def test_no_choices_cannot_launch(self):
        with self.assertRaisesRegex(ValueError, "没有找到"):
            model_picker.select_model([], MODELS, "grok-fast")

    def test_labels_cannot_inject_terminal_controls_or_split_records(self):
        options = [{"model": "gpt-sol", "label": "模型\x1b[2J\t\x00\n$(touch /tmp/never)"}]
        records = model_picker.picker_records(options, MODELS, "gpt-sol")
        self.assertEqual(records.count("\0"), 2)
        self.assertNotIn("\x1b", records)
        self.assertEqual(records.split("\0")[1].count("\t"), 3)


if __name__ == "__main__":
    unittest.main()
