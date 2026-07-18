import fcntl
import json
import os
import pty
import select
import signal
import struct
import subprocess
import termios
import tempfile
import time
import unittest
import uuid
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "bin/tmux-window-wrap"
CONFIG = ROOT / "conf/tmux-window-wrap.conf"


class WindowWrapCliTests(unittest.TestCase):
    def run_cli(self, arguments, payload):
        completed = subprocess.run(
            [SCRIPT, *arguments],
            input=json.dumps(payload),
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return completed.stdout

    def run_plan(self, payload):
        output = self.run_cli(["plan"], payload)
        return json.loads(output)

    def run_render(self, payload, line):
        return self.run_cli(["render", "--line", str(line)], payload).rstrip("\n")

    def test_windows_stay_on_one_line_when_they_fit(self):
        result = self.run_plan(
            {
                "width": 30,
                "left_width": 4,
                "right_width": 5,
                "active": "@2",
                "windows": [
                    {"id": "@1", "label": " 1:a "},
                    {"id": "@2", "label": " 2:bb "},
                ],
            }
        )

        self.assertEqual(
            result,
            {"line_count": 1, "lines": [["@1", "@2"]]},
        )

    def test_overflow_continues_on_the_second_line(self):
        result = self.run_plan(
            {
                "width": 20,
                "left_width": 4,
                "right_width": 5,
                "active": "@2",
                "windows": [
                    {"id": "@1", "label": " 1:a "},
                    {"id": "@2", "label": " 2:bb "},
                    {"id": "@3", "label": " 3:c "},
                ],
            }
        )

        self.assertEqual(
            result,
            {"line_count": 2, "lines": [["@1", "@2"], ["@3"]]},
        )

    def test_cjk_characters_use_terminal_column_width(self):
        result = self.run_plan(
            {
                "width": 18,
                "left_width": 2,
                "right_width": 4,
                "active": "@1",
                "windows": [
                    {"id": "@1", "label": " 1:手册 "},
                    {"id": "@2", "label": " 2:a "},
                ],
            }
        )

        self.assertEqual(
            result,
            {"line_count": 2, "lines": [["@1"], ["@2"]]},
        )

    def test_zero_width_unicode_marks_match_tmux_column_width(self):
        result = self.run_plan(
            {
                "width": 10,
                "left_width": 0,
                "right_width": 0,
                "active": "@1",
                "windows": [
                    {"id": "@1", "label": " 1:a\u200b "},
                    {"id": "@2", "label": " 2:1\ufe0f\u20e3 "},
                ],
            }
        )

        self.assertEqual(
            result,
            {"line_count": 1, "lines": [["@1", "@2"]]},
        )

    def test_active_window_stays_visible_when_three_lines_are_full(self):
        result = self.run_plan(
            {
                "width": 20,
                "left_width": 4,
                "right_width": 5,
                "active": "@7",
                "windows": [
                    {"id": f"@{index}", "label": f" {index}:a "}
                    for index in range(1, 9)
                ],
            }
        )

        self.assertEqual(
            result,
            {
                "line_count": 3,
                "lines": [
                    ["@1", "@2"],
                    ["@3", "@4", "@5"],
                    ["@6", "@7", "@8"],
                ],
            },
        )

    def test_render_keeps_window_ranges_and_indents_the_second_line(self):
        payload = {
            "width": 20,
            "left_width": 4,
            "right_width": 5,
            "active": "@2",
            "windows": [
                {"id": "@1", "index": "1", "name": "a", "label": " 1:a "},
                {"id": "@2", "index": "2", "name": "bb", "label": " 2:bb "},
                {"id": "@3", "index": "3", "name": "c", "label": " 3:c "},
            ],
        }

        self.assertEqual(
            self.run_render(payload, 0),
            "#[range=window|1] #[fg=colour7]1:a #[norange]"
            "#[range=window|2]#[fg=blue]#[bg=blue]#[fg=colour15]#[bold]"
            " 2:bb #[bg=default]#[fg=blue]#[default]#[norange]",
        )
        self.assertEqual(
            self.run_render(payload, 1),
            "    #[range=window|3] #[fg=colour7]3:c #[norange]",
        )

    def test_render_indents_the_third_line(self):
        payload = {
            "width": 20,
            "left_width": 4,
            "right_width": 5,
            "active": "@7",
            "windows": [
                {
                    "id": f"@{index}",
                    "index": str(index),
                    "name": "a",
                    "label": f" {index}:a ",
                }
                for index in range(1, 8)
            ],
        }

        third_line = self.run_render(payload, 2)

        self.assertTrue(third_line.startswith("    "))
        self.assertIn("#[range=window|6]", third_line)
        self.assertIn("#[range=window|7]", third_line)
        self.assertIn(" 7:a ", third_line)

    def test_extreme_width_reduces_indent_to_keep_active_window_visible(self):
        payload = {
            "width": 9,
            "left_width": 9,
            "right_width": 0,
            "active": "@1",
            "windows": [
                {"id": "@1", "index": "1", "name": "a", "label": " 1:a "},
            ],
        }

        self.assertEqual(
            self.run_plan(payload),
            {"line_count": 2, "lines": [[], ["@1"]]},
        )
        self.assertEqual(
            self.run_render(payload, 1),
            "    #[range=window|1]#[fg=blue]#[bg=blue]#[fg=colour15]#[bold]"
            " 1:a #[bg=default]#[fg=blue]#[default]#[norange]",
        )

    def test_oversized_overflow_label_still_makes_progress_across_lines(self):
        result = self.run_plan(
            {
                "width": 20,
                "left_width": 4,
                "right_width": 5,
                "active": "@1",
                "windows": [
                    {"id": "@1", "label": " 1:a "},
                    {"id": "@2", "label": " 2:abcdefghijklmnopqrst "},
                    {"id": "@3", "label": " 3:b "},
                ],
            }
        )

        self.assertEqual(
            result,
            {"line_count": 3, "lines": [["@1"], ["@2"], ["@3"]]},
        )

    def test_window_name_cannot_inject_tmux_status_styles(self):
        payload = {
            "width": 80,
            "left_width": 4,
            "right_width": 5,
            "active": "@1",
            "windows": [
                {
                    "id": "@1",
                    "index": "1",
                    "name": "x#[bg=red]y",
                    "label": " 1:x#[bg=red]y ",
                },
            ],
        }

        self.assertEqual(
            self.run_render(payload, 0),
            "#[range=window|1]#[fg=blue]#[bg=blue]#[fg=colour15]#[bold]"
            " 1:x##[bg=red]y #[bg=default]#[fg=blue]#[default]#[norange]",
        )


class WindowWrapTmuxIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.attached_clients = []
        self.socket_name = f"window-wrap-test-{uuid.uuid4().hex}"
        self.tmux(
            "-f",
            "/dev/null",
            "new-session",
            "-d",
            "-s",
            "wrap",
            "-x",
            "80",
            "-y",
            "24",
            "-n",
            "a",
            "sleep 120",
        )
        self.tmux("new-window", "-d", "-t", "wrap", "-n", "手册", "sleep 120")
        self.tmux("new-window", "-d", "-t", "wrap", "-n", "c", "sleep 120")
        self.tmux("select-window", "-t", "wrap:1")
        self.session_id = self.tmux(
            "display-message", "-p", "-t", "wrap", "#{session_id}"
        ).stdout.strip()
        self.socket_path = Path(
            self.tmux(
                "display-message", "-p", "-t", "wrap", "#{socket_path}"
            ).stdout.strip()
        )

    def tearDown(self):
        self.tmux("kill-server", check=False)
        for process, master_fd in self.attached_clients:
            try:
                os.close(master_fd)
            except OSError:
                pass
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=1)
        self.socket_path.unlink(missing_ok=True)

    def tmux(self, *arguments, check=True):
        return subprocess.run(
            ["tmux", "-L", self.socket_name, *arguments],
            text=True,
            capture_output=True,
            check=check,
        )

    def render_runtime(self, line, width):
        return subprocess.run(
            [
                SCRIPT,
                "render",
                "--line",
                str(line),
                "--session-id",
                self.session_id,
                "--width",
                str(width),
                "--left-width",
                "4",
                "--right-width",
                "5",
                "--socket-name",
                self.socket_name,
            ],
            text=True,
            capture_output=True,
            check=True,
        ).stdout.rstrip("\n")

    def attach_client(self, width, height=24):
        master_fd, slave_fd = pty.openpty()
        fcntl.ioctl(
            slave_fd,
            termios.TIOCSWINSZ,
            struct.pack("HHHH", height, width, 0, 0),
        )
        environment = os.environ.copy()
        environment["TERM"] = "xterm-256color"
        process = subprocess.Popen(
            ["tmux", "-L", self.socket_name, "attach-session", "-t", "wrap"],
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            close_fds=True,
            env=environment,
        )
        os.close(slave_fd)
        os.set_blocking(master_fd, False)
        self.attached_clients.append((process, master_fd))
        return process, master_fd

    def resize_client(self, process, master_fd, width, height=24):
        fcntl.ioctl(
            master_fd,
            termios.TIOCSWINSZ,
            struct.pack("HHHH", height, width, 0, 0),
        )
        process.send_signal(signal.SIGWINCH)

    def drain_client_output(self, master_fd):
        while True:
            try:
                if not os.read(master_fd, 65536):
                    return
            except (BlockingIOError, OSError):
                return

    def wait_for_active_label(self, master_fd, label, timeout):
        deadline = time.monotonic() + timeout
        label_bytes = f" {label} ".encode()
        output = b""
        while time.monotonic() < deadline:
            readable, _, _ = select.select([master_fd], [], [], 0.02)
            if not readable:
                continue
            try:
                output = (output + os.read(master_fd, 65536))[-16384:]
            except BlockingIOError:
                continue

            offset = 0
            while True:
                position = output.find(label_bytes, offset)
                if position == -1:
                    break
                style_prefix = output[max(0, position - 100) : position]
                if b"[44m" in style_prefix and b"[1m" in style_prefix:
                    return time.monotonic()
                offset = position + 1
        self.fail(f"active status style for {label!r} was not rendered in time")

    def wait_for_status_text(self, master_fd, text, timeout):
        deadline = time.monotonic() + timeout
        needle = text.encode()
        output = b""
        while time.monotonic() < deadline:
            readable, _, _ = select.select([master_fd], [], [], 0.02)
            if not readable:
                continue
            try:
                output = (output + os.read(master_fd, 65536))[-16384:]
            except BlockingIOError:
                continue
            if needle in output:
                return time.monotonic()
        self.fail(
            f"status text {text!r} was not rendered in time; "
            f"output tail={output[-1000:]!r}"
        )

    def wait_for_status_label_arrow(self, master_fd, label, timeout):
        deadline = time.monotonic() + timeout
        label_bytes = label.encode()
        arrow = "▶".encode()
        output = b""
        while time.monotonic() < deadline:
            readable, _, _ = select.select([master_fd], [], [], 0.02)
            if not readable:
                continue
            try:
                output = (output + os.read(master_fd, 65536))[-16384:]
            except BlockingIOError:
                continue
            offset = 0
            while True:
                position = output.find(label_bytes, offset)
                if position == -1:
                    break
                if arrow in output[position : position + 100]:
                    return time.monotonic()
                offset = position + 1
        self.fail(
            f"status label {label!r} with sync arrow was not rendered in time; "
            f"output tail={output[-1000:]!r}"
        )

    def wait_for_client_count(self, expected):
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            completed = self.tmux(
                "list-clients", "-t", self.session_id, "-F", "#{client_width}"
            )
            widths = [int(value) for value in completed.stdout.splitlines()]
            if len(widths) == expected:
                return widths
            time.sleep(0.02)
        self.fail(f"expected {expected} attached clients")

    def wait_for_client_widths(self, expected):
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            completed = self.tmux(
                "list-clients", "-t", self.session_id, "-F", "#{client_width}"
            )
            widths = sorted(int(value) for value in completed.stdout.splitlines())
            if widths == sorted(expected):
                return widths
            time.sleep(0.02)
        self.fail(f"client widths did not become {expected}")

    def source_window_wrap_config(self):
        config_text = CONFIG.read_text().replace(
            "$HOME/.local/bin/tmux-window-wrap",
            str(SCRIPT),
        )
        # Fixture chrome that real users keep in personal ~/.tmux.conf — not in
        # the published conf fragment. Matches the integration assumptions used
        # before window-wrap was extracted from the full home config.
        preamble = (
            "set -g mouse on\n"
            "set -g base-index 1\n"
            "setw -g pane-base-index 1\n"
            "set -g renumber-windows on\n"
            'set -g status-left "#[fg=blue]#[bold] #S "\n'
            'set -g status-right "'
            "#{?pane_synchronized,#[bg=yellow]#[fg=black]#[bold] SYNC #[default],}"
            '#[fg=colour7] %m-%d %H:%M "\n'
            "set -g status-left-length 30\n"
            "set -g status-right-length 200\n"
            "bind -n M-< swap-window -d -t :-1\n"
            "bind -n M-> swap-window -d -t :+1\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "tmux.conf"
            config_path.write_text(preamble + config_text)
            self.tmux("source-file", str(config_path))

    def wait_for_status(self, expected, timeout=3):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            status = self.tmux(
                "display-message", "-p", "-t", self.session_id, "#{status}"
            ).stdout.strip()
            if status == expected:
                return time.monotonic()
            time.sleep(0.02)
        self.fail(f"status did not become {expected}")

    def test_runtime_switches_between_one_two_and_three_status_lines(self):
        three_line_first = self.render_runtime(line=0, width=12)
        three_line_second = self.render_runtime(line=1, width=12)
        three_line_third = self.render_runtime(line=2, width=12)

        self.assertNotIn("0:a", three_line_first)
        self.assertTrue(three_line_second.startswith("    "))
        self.assertIn("0:a", three_line_second)
        self.assertTrue(three_line_third.startswith("    "))
        self.assertIn("1:手册", three_line_third)
        self.assertEqual(
            self.tmux("show-options", "-t", self.session_id, "-v", "status")
            .stdout.strip(),
            "3",
        )

        first_line = self.render_runtime(line=0, width=20)
        second_line = self.render_runtime(line=1, width=20)

        self.assertIn("0:a", first_line)
        self.assertNotIn("1:手册", first_line)
        self.assertTrue(second_line.startswith("    "))
        self.assertIn("1:手册", second_line)
        self.assertIn("2:c", second_line)
        self.assertEqual(
            self.tmux("show-options", "-t", self.session_id, "-v", "status")
            .stdout.strip(),
            "2",
        )

        wide_line = self.render_runtime(line=0, width=80)
        self.assertIn("0:a", wide_line)
        self.assertIn("1:手册", wide_line)
        self.assertIn("2:c", wide_line)
        self.assertEqual(
            self.tmux("show-options", "-t", self.session_id, "-v", "status")
            .stdout.strip(),
            "on",
        )

    def test_runtime_accepts_unicode_line_separators_in_window_names(self):
        for separator in ("\u0085", "\u2028", "\u2029"):
            with self.subTest(separator=f"U+{ord(separator):04X}"):
                name = f"a{separator}b"
                self.tmux("rename-window", "-t", "wrap:2", name)
                rendered = self.render_runtime(line=0, width=80)
                self.assertIn(f"2:{name}", rendered)

        self.source_window_wrap_config()
        _, master_fd = self.attach_client(width=80)
        self.wait_for_client_count(1)
        self.wait_for_status_text(master_fd, f"2:{name}", timeout=3)

    def test_session_uses_the_narrowest_attached_client_width(self):
        narrow_process, _ = self.attach_client(width=20)
        self.attach_client(width=80)
        self.assertEqual(sorted(self.wait_for_client_count(2)), [20, 80])

        self.render_runtime(line=0, width=80)

        self.assertEqual(
            self.tmux("show-options", "-t", self.session_id, "-v", "status")
            .stdout.strip(),
            "2",
        )

        narrow_process.terminate()
        narrow_process.wait(timeout=1)
        self.assertEqual(self.wait_for_client_count(1), [80])
        self.render_runtime(line=0, width=80)
        self.assertEqual(
            self.tmux("show-options", "-t", self.session_id, "-v", "status")
            .stdout.strip(),
            "on",
        )

    def test_detaching_narrow_client_restores_one_line_without_cached_delay(self):
        self.source_window_wrap_config()
        self.attach_client(width=80)
        self.wait_for_client_count(1)
        self.wait_for_status("on")

        narrow_process, _ = self.attach_client(width=20)
        self.assertEqual(sorted(self.wait_for_client_count(2)), [20, 80])
        self.wait_for_status("3")

        narrow_process.terminate()
        narrow_process.wait(timeout=1)
        self.assertEqual(self.wait_for_client_count(1), [80])
        started = time.monotonic()
        restored = self.wait_for_status("on", timeout=0.5)
        self.assertLess(restored - started, 0.5)

    def test_resizing_narrow_client_refreshes_all_clients_without_cached_delay(self):
        self.source_window_wrap_config()
        _, wide_master = self.attach_client(width=80)
        self.wait_for_client_count(1)
        narrow_process, narrow_master = self.attach_client(width=20)
        self.assertIsNone(narrow_process.poll())
        self.assertEqual(self.wait_for_client_widths([20, 80]), [20, 80])
        self.wait_for_status("3")
        self.drain_client_output(wide_master)

        started = time.monotonic()
        self.resize_client(narrow_process, narrow_master, width=80)
        self.assertEqual(self.wait_for_client_widths([80, 80]), [80, 80])
        restored = self.wait_for_status("on", timeout=0.5)
        rendered = self.wait_for_status_text(wide_master, "2:c", timeout=0.5)
        self.assertLess(max(restored, rendered) - started, 0.5)

        self.drain_client_output(wide_master)
        started = time.monotonic()
        self.resize_client(narrow_process, narrow_master, width=20)
        self.assertEqual(self.wait_for_client_widths([20, 80]), [20, 80])
        wrapped = self.wait_for_status("3", timeout=0.5)
        rendered = self.wait_for_status_text(wide_master, "1:手册", timeout=0.5)
        self.assertLess(max(wrapped, rendered) - started, 0.5)

    def test_cache_invalidation_preserves_explicitly_hidden_status(self):
        self.source_window_wrap_config()
        self.attach_client(width=80)
        self.wait_for_client_count(1)
        self.wait_for_status("on")
        self.tmux("set-option", "-t", "wrap", "status", "off")

        subprocess.run(
            [
                SCRIPT,
                "invalidate",
                "--socket-name",
                self.socket_name,
            ],
            text=True,
            capture_output=True,
            check=True,
        )

        self.assertEqual(
            self.tmux("show-options", "-t", "wrap", "-v", "status")
            .stdout.strip(),
            "off",
        )

    def test_attached_client_executes_status_format_and_clicks_third_row(self):
        self.source_window_wrap_config()
        _, master_fd = self.attach_client(width=20, height=24)
        self.wait_for_client_count(1)
        self.wait_for_status("3")

        deadline = time.monotonic() + 3
        click = b"\x1b[<0;8;24M\x1b[<0;8;24m"
        started = time.monotonic()
        while time.monotonic() < deadline:
            self.drain_client_output(master_fd)
            os.write(master_fd, click)
            active_index = self.tmux(
                "display-message", "-p", "-t", "wrap", "#{window_index}"
            ).stdout.strip()
            if active_index == "2":
                break
            time.sleep(0.05)
        else:
            self.fail("clicking the third status row did not select window 2")

        rendered = self.wait_for_active_label(master_fd, "2:c", timeout=0.5)
        self.assertLess(rendered - started, 0.5)

    def test_window_switch_refreshes_active_style_without_cached_delay(self):
        self.source_window_wrap_config()
        _, master_fd = self.attach_client(width=80, height=24)
        self.wait_for_client_count(1)
        self.wait_for_active_label(master_fd, "1:手册", timeout=3)

        targets = (
            ("2", "2:c"),
            ("1", "1:手册"),
            ("2", "2:c"),
            ("1", "1:手册"),
        )
        for target_index, target_label in targets:
            with self.subTest(target=target_label):
                self.drain_client_output(master_fd)
                started = time.monotonic()
                self.tmux("select-window", "-t", f"wrap:{target_index}")
                rendered = self.wait_for_active_label(
                    master_fd,
                    target_label,
                    timeout=0.5,
                )
                self.assertLess(rendered - started, 0.5)

    def test_alt_shift_arrows_refresh_reordered_window_without_cached_delay(self):
        self.source_window_wrap_config()
        _, master_fd = self.attach_client(width=80, height=24)
        self.wait_for_client_count(1)
        self.wait_for_active_label(master_fd, "1:手册", timeout=3)
        self.drain_client_output(master_fd)

        started = time.monotonic()
        os.write(master_fd, b"\x1b>")
        deadline = time.monotonic() + 0.5
        while time.monotonic() < deadline:
            active_index = self.tmux(
                "display-message", "-p", "-t", "wrap", "#{window_index}"
            ).stdout.strip()
            if active_index == "2":
                break
            time.sleep(0.01)
        else:
            self.fail("M-> did not move the active window to index 2")

        rendered = self.wait_for_active_label(master_fd, "2:手册", timeout=0.5)
        self.assertLess(rendered - started, 0.5)

        self.drain_client_output(master_fd)
        started = time.monotonic()
        os.write(master_fd, b"\x1b<")
        deadline = time.monotonic() + 0.5
        while time.monotonic() < deadline:
            active_index = self.tmux(
                "display-message", "-p", "-t", "wrap", "#{window_index}"
            ).stdout.strip()
            if active_index == "1":
                break
            time.sleep(0.01)
        else:
            self.fail("M-< did not move the active window back to index 1")

        rendered = self.wait_for_active_label(master_fd, "1:手册", timeout=0.5)
        self.assertLess(rendered - started, 0.5)

    def test_closing_background_window_updates_label_before_it_can_be_clicked(self):
        self.source_window_wrap_config()
        self.tmux("move-window", "-r", "-t", "wrap")
        self.tmux("select-window", "-t", "wrap:2")
        self.tmux("new-window", "-d", "-t", "wrap", "-n", "d", "sleep 120")
        _, master_fd = self.attach_client(width=80, height=24)
        self.wait_for_client_count(1)
        self.wait_for_status_text(master_fd, "4:d", timeout=3)
        self.drain_client_output(master_fd)

        started = time.monotonic()
        self.tmux("kill-window", "-t", "wrap:3")
        rendered = self.wait_for_status_text(master_fd, "3:d", timeout=0.5)
        self.assertLess(rendered - started, 0.5)

        os.write(master_fd, b"\x1b[<0;22;24M\x1b[<0;22;24m")
        deadline = time.monotonic() + 0.5
        while time.monotonic() < deadline:
            active = self.tmux(
                "display-message",
                "-p",
                "-t",
                "wrap",
                "#{window_index}:#{window_name}",
            ).stdout.strip()
            if active == "3:d":
                break
            time.sleep(0.02)
        else:
            self.fail(f"click selected {active!r} instead of the visible 3:d label")

    def test_creating_background_window_refreshes_without_cached_delay(self):
        self.source_window_wrap_config()
        _, master_fd = self.attach_client(width=80, height=24)
        self.wait_for_client_count(1)
        self.wait_for_status_text(master_fd, "2:c", timeout=3)
        self.drain_client_output(master_fd)

        started = time.monotonic()
        self.tmux("new-window", "-d", "-t", "wrap", "-n", "d", "sleep 120")
        rendered = self.wait_for_status_text(master_fd, "3:d", timeout=0.5)
        self.assertLess(rendered - started, 0.5)

    def test_swapping_background_windows_refreshes_without_cached_delay(self):
        self.source_window_wrap_config()
        _, master_fd = self.attach_client(width=80, height=24)
        self.wait_for_client_count(1)
        self.wait_for_status_text(master_fd, "2:c", timeout=3)
        self.drain_client_output(master_fd)

        started = time.monotonic()
        self.tmux("swap-window", "-s", "wrap:0", "-t", "wrap:2")
        rendered = self.wait_for_status_text(master_fd, "0:c", timeout=0.5)
        self.assertLess(rendered - started, 0.5)

    def test_renaming_background_window_refreshes_without_cached_delay(self):
        self.source_window_wrap_config()
        _, master_fd = self.attach_client(width=80, height=24)
        self.wait_for_client_count(1)
        self.wait_for_status_text(master_fd, "2:c", timeout=3)
        self.drain_client_output(master_fd)

        started = time.monotonic()
        name = "quoted ' $() |"
        self.tmux("rename-window", "-t", "wrap:2", name)
        rendered = self.wait_for_status_text(master_fd, f"2:{name}", timeout=0.5)
        self.assertLess(rendered - started, 0.5)

    def test_renaming_window_round_trip_restores_status_height_immediately(self):
        self.source_window_wrap_config()
        self.attach_client(width=40, height=24)
        self.wait_for_client_count(1)
        self.wait_for_status("on")

        self.tmux(
            "rename-window",
            "-t",
            "wrap:2",
            "abcdefghijklmnopqrst",
        )
        self.wait_for_status("2", timeout=0.5)

        started = time.monotonic()
        self.tmux("rename-window", "-t", "wrap:2", "c")
        restored = self.wait_for_status("on", timeout=0.5)
        self.assertLess(restored - started, 0.5)

    def test_synchronizing_background_window_refreshes_without_cached_delay(self):
        self.tmux("rename-window", "-t", "wrap:2", "sync-target")
        self.source_window_wrap_config()
        _, master_fd = self.attach_client(width=80, height=24)
        self.wait_for_client_count(1)
        self.wait_for_status_text(master_fd, "2:sync-target", timeout=3)
        self.drain_client_output(master_fd)

        started = time.monotonic()
        self.tmux(
            "set-window-option",
            "-t",
            "wrap:2",
            "synchronize-panes",
            "on",
        )
        rendered = self.wait_for_status_label_arrow(
            master_fd,
            "2:sync-target",
            timeout=0.5,
        )
        self.assertLess(rendered - started, 0.5)

    def test_sourcing_config_preserves_external_indexed_hooks(self):
        external_hooks = {
            "after-select-window": (
                "set-environment -g AIPANE_EXTERNAL_AFTER_SELECT preserved"
            ),
            "window-linked": (
                "set-environment -g AIPANE_EXTERNAL_WINDOW_LINKED preserved"
            ),
        }
        for hook, command in external_hooks.items():
            self.tmux("set-hook", "-g", f"{hook}[99]", command)

        self.source_window_wrap_config()

        for hook, command in external_hooks.items():
            configured = self.tmux("show-hooks", "-g", hook).stdout
            self.assertIn(command, configured)

    def test_window_wrap_config_connects_all_status_rows_to_complete_cache_keys(self):
        self.source_window_wrap_config()

        first_row = self.tmux(
            "show-options", "-g", "-v", "status-format[0]"
        ).stdout.strip()
        second_row = self.tmux(
            "show-options", "-g", "-v", "status-format[1]"
        ).stdout.strip()
        third_row = self.tmux(
            "show-options", "-g", "-v", "status-format[2]"
        ).stdout.strip()

        self.assertIn("#{T;=/#{status-left-length}:status-left}", first_row)
        self.assertIn("tmux-window-wrap render --line 0", first_row)
        self.assertIn("#{T;=/#{status-right-length}:status-right}", first_row)
        self.assertIn("tmux-window-wrap render --line 1", second_row)
        self.assertIn("tmux-window-wrap render --line 2", third_row)
        cache_keys = (
            "TMUX_WINDOW_WRAP_GENERATION=#{TMUX_WINDOW_WRAP_GENERATION}",
            "TMUX_WINDOW_WRAP_WINDOWS=#{W:",
            "#{q:window_name}_#{pane_synchronized}",
            "TMUX_WINDOW_WRAP_CLIENTS=#{L:",
            "#{q:client_name}_#{client_width}",
            "TMUX_WINDOW_WRAP_ACTIVE=#{q:window_id}:#{window_index}",
        )
        for cache_key in cache_keys:
            self.assertIn(cache_key, first_row)
            self.assertIn(cache_key, second_row)
            self.assertIn(cache_key, third_row)
        self.assertNotIn("#[align=right", second_row)
        self.assertNotIn("#[align=right", third_row)

        for hook in (
            "client-attached",
            "client-detached",
            "client-resized",
            "client-session-changed",
            "window-linked",
            "window-unlinked",
            "window-renamed",
            "session-renamed",
        ):
            configured = self.tmux("show-hooks", "-g", hook).stdout
            self.assertIn("tmux-window-wrap invalidate", configured)

        # conf must stay a public fragment: no personal prefix/plugins.
        conf_text = CONFIG.read_text()
        self.assertNotIn("set -g prefix", conf_text)
        self.assertNotIn("@plugin", conf_text)
        self.assertNotIn("tpm", conf_text.lower())


if __name__ == "__main__":
    unittest.main()
