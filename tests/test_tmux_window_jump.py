"""Behavior tests for the tmux-window-jump CLI."""

from __future__ import annotations

import os
import pty
import signal
import subprocess
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
JUMP_BIN = ROOT / "bin" / "tmux-window-jump"


class TmuxWindowJumpTests(unittest.TestCase):
    def setUp(self):
        self.socket = f"window-jump-{os.getpid()}-{id(self)}"
        self.session = "jump"
        self.tmux(
            "-f",
            "/dev/null",
            "new-session",
            "-d",
            "-s",
            self.session,
            "-n",
            "zero",
            "sleep 30",
        )
        self.tmux("set-option", "-g", "base-index", "0")
        self.tmux("set-option", "-g", "renumber-windows", "on")
        for index in range(1, 12):
            self.tmux(
                "new-window",
                "-d",
                "-t",
                f"{self.session}:{index}",
                "-n",
                str(index),
                "sleep 30",
            )

        self.session_id = self.tmux_output(
            "display-message", "-p", "-t", self.session, "#{session_id}"
        )
        socket_path = self.tmux_output(
            "display-message", "-p", "-t", self.session, "#{socket_path}"
        )
        server_pid = self.tmux_output(
            "display-message", "-p", "-t", self.session, "#{pid}"
        )
        pane_id = self.tmux_output(
            "display-message", "-p", "-t", self.session, "#{pane_id}"
        )
        self.environment = os.environ.copy()
        self.environment["TMUX"] = (
            f"{socket_path},{server_pid},{pane_id.lstrip('%')}"
        )

    def tearDown(self):
        self.tmux("kill-server", check=False)

    def tmux(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["tmux", "-L", self.socket, *args],
            check=check,
            capture_output=True,
            text=True,
        )

    def tmux_output(self, *args: str) -> str:
        return self.tmux(*args).stdout.strip()

    def run_jump(
        self,
        digit: int,
        client_key: str = "4242",
        session_id: str | None = None,
    ) -> subprocess.CompletedProcess:
        target_session_id = self.session_id if session_id is None else session_id
        return subprocess.run(
            [
                str(JUMP_BIN),
                "select",
                str(digit),
                client_key,
                target_session_id,
            ],
            check=False,
            capture_output=True,
            text=True,
            env=self.environment,
        )

    def run_end(self) -> subprocess.CompletedProcess:
        return subprocess.run(
            [str(JUMP_BIN), "end", "4242", self.session_id],
            check=False,
            capture_output=True,
            text=True,
            env=self.environment,
        )

    def current_window(self) -> int:
        return int(
            self.tmux_output(
                "display-message",
                "-p",
                "-t",
                self.session,
                "#{window_index}",
            )
        )

    def add_windows_through(self, last_index: int) -> None:
        existing = {
            int(index)
            for index in self.tmux_output(
                "list-windows", "-t", self.session, "-F", "#{window_index}"
            ).splitlines()
        }
        for index in range(last_index + 1):
            if index in existing:
                continue
            self.tmux(
                "new-window",
                "-d",
                "-t",
                f"{self.session}:{index}",
                "-n",
                str(index),
                "sleep 30",
            )

    def test_single_press_selects_exact_base_window_index(self):
        self.tmux("select-window", "-t", f"{self.session}:5")

        result = self.run_jump(9)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.current_window(), 9)

    def test_second_same_digit_press_selects_next_decade(self):
        first = self.run_jump(1)
        second = self.run_jump(1)

        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(self.current_window(), 11)

    def test_zero_presses_start_at_zero_and_advance_by_ten(self):
        self.tmux("set-option", "-g", "base-index", "1")
        self.add_windows_through(20)
        self.tmux("select-window", "-t", f"{self.session}:5")

        for expected_index in (0, 10, 20):
            with self.subTest(expected_index=expected_index):
                result = self.run_jump(0)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.current_window(), expected_index)

    def test_zero_without_window_zero_starts_at_ten(self):
        self.add_windows_through(30)
        self.tmux("set-option", "-g", "base-index", "1")
        self.tmux("kill-window", "-t", f"{self.session}:0")
        self.tmux("select-window", "-t", f"{self.session}:5")

        for expected_index in (10, 20, 30):
            with self.subTest(expected_index=expected_index):
                result = self.run_jump(0)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.current_window(), expected_index)

    def test_zero_appearing_during_a_chain_does_not_repeat_a_target(self):
        self.add_windows_through(20)
        self.tmux("set-option", "-g", "base-index", "1")
        self.tmux("kill-window", "-t", f"{self.session}:0")

        first = self.run_jump(0)
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(self.current_window(), 10)
        self.tmux(
            "new-window", "-d", "-t", f"{self.session}:0", "sleep 30"
        )
        second = self.run_jump(0)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(self.current_window(), 20)

        self.run_end()
        restarted = self.run_jump(0)
        self.assertEqual(restarted.returncode, 0, restarted.stderr)
        self.assertEqual(self.current_window(), 0)

    def test_zero_disappearing_during_a_chain_does_not_skip_a_target(self):
        self.add_windows_through(20)
        self.tmux("set-option", "-g", "base-index", "1")

        for expected_index in (0, 10):
            result = self.run_jump(0)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(self.current_window(), expected_index)
        self.tmux("kill-window", "-t", f"{self.session}:0")
        result = self.run_jump(0)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.current_window(), 20)

    def test_zero_without_window_ten_does_not_skip_to_twenty(self):
        self.add_windows_through(20)
        self.tmux("set-option", "-g", "renumber-windows", "off")
        self.tmux("kill-window", "-t", f"{self.session}:0")
        self.tmux("kill-window", "-t", f"{self.session}:10")
        self.tmux("select-window", "-t", f"{self.session}:5")

        first = self.run_jump(0)
        repeated = self.run_jump(0)

        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(repeated.returncode, 0, repeated.stderr)
        self.assertEqual(self.current_window(), 5)

    def test_zero_missing_target_stays_put_and_blocks_until_silence(self):
        self.tmux(
            "set-option", "-g", "@tmux-window-jump-timeout-ms", "2000"
        )

        # setUp has windows 0-11, so 0 and 10 exist but 20 does not yet.
        first = self.run_jump(0)
        second = self.run_jump(0)
        missing = self.run_jump(0)
        self.assertEqual(self.current_window(), 10)
        self.add_windows_through(20)
        self.tmux(
            "set-option", "-g", "@tmux-window-jump-timeout-ms", "80"
        )
        blocked = self.run_jump(0)
        self.assertEqual(self.current_window(), 10)
        time.sleep(0.15)
        restarted = self.run_jump(0)
        self.assertEqual(self.current_window(), 0)
        advanced = self.run_jump(0)
        self.assertEqual(self.current_window(), 10)
        advanced_again = self.run_jump(0)

        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(missing.returncode, 0, missing.stderr)
        self.assertEqual(blocked.returncode, 0, blocked.stderr)
        self.assertEqual(restarted.returncode, 0, restarted.stderr)
        self.assertEqual(advanced.returncode, 0, advanced.stderr)
        self.assertEqual(advanced_again.returncode, 0, advanced_again.stderr)
        self.assertEqual(self.current_window(), 20)

    def test_press_after_timeout_starts_again_at_base_index(self):
        self.tmux(
            "set-option", "-g", "@tmux-window-jump-timeout-ms", "80"
        )

        first = self.run_jump(1)
        time.sleep(0.15)
        second = self.run_jump(1)

        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(self.current_window(), 1)

    def test_invalid_timeout_values_fall_back_to_700ms(self):
        for invalid_value in ("", "0", "invalid"):
            with self.subTest(value=invalid_value):
                self.run_end()
                self.tmux(
                    "set-option",
                    "-g",
                    "@tmux-window-jump-timeout-ms",
                    invalid_value,
                )

                first = self.run_jump(1)
                time.sleep(0.1)
                second = self.run_jump(1)

                self.assertEqual(first.returncode, 0, first.stderr)
                self.assertEqual(second.returncode, 0, second.stderr)
                self.assertEqual(self.current_window(), 11)

    def test_each_adjacent_press_extends_timeout_and_advances_a_decade(self):
        self.add_windows_through(29)
        self.tmux(
            "set-option", "-g", "@tmux-window-jump-timeout-ms", "300"
        )

        first = self.run_jump(9)
        time.sleep(0.18)
        second = self.run_jump(9)
        time.sleep(0.18)
        third = self.run_jump(9)

        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(third.returncode, 0, third.stderr)
        self.assertEqual(self.current_window(), 29)

    def test_different_digit_starts_a_new_chain(self):
        self.add_windows_through(12)

        one = self.run_jump(1)
        two = self.run_jump(2)
        twelve = self.run_jump(2)

        self.assertEqual(one.returncode, 0, one.stderr)
        self.assertEqual(two.returncode, 0, two.stderr)
        self.assertEqual(twelve.returncode, 0, twelve.stderr)
        self.assertEqual(self.current_window(), 12)

    def test_plain_digit_input_neither_counts_nor_resets_the_chain(self):
        self.add_windows_through(19)

        first = self.run_jump(9)
        self.tmux("send-keys", "-t", self.session, "9")
        repeated_shortcut = self.run_jump(9)

        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(
            repeated_shortcut.returncode, 0, repeated_shortcut.stderr
        )
        self.assertEqual(self.current_window(), 19)

    def test_external_window_change_starts_a_new_chain(self):
        first = self.run_jump(1)
        second = self.run_jump(1)
        self.tmux("select-window", "-t", f"{self.session}:5")
        restarted = self.run_jump(1)

        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(restarted.returncode, 0, restarted.stderr)
        self.assertEqual(self.current_window(), 1)

    def test_digit_chain_is_isolated_by_tmux_client(self):
        first_client = self.run_jump(1, client_key="4242")
        first_client_again = self.run_jump(1, client_key="4242")
        second_client = self.run_jump(1, client_key="4343")

        self.assertEqual(first_client.returncode, 0, first_client.stderr)
        self.assertEqual(
            first_client_again.returncode, 0, first_client_again.stderr
        )
        self.assertEqual(second_client.returncode, 0, second_client.stderr)
        self.assertEqual(self.current_window(), 1)

    def test_digit_chain_is_isolated_by_tmux_session(self):
        other_session = "other"
        self.tmux(
            "new-session",
            "-d",
            "-s",
            other_session,
            "-n",
            "zero",
            "sleep 30",
        )
        for index in range(1, 12):
            self.tmux(
                "new-window",
                "-d",
                "-t",
                f"{other_session}:{index}",
                "-n",
                str(index),
                "sleep 30",
            )
        other_session_id = self.tmux_output(
            "display-message",
            "-p",
            "-t",
            other_session,
            "#{session_id}",
        )

        first = self.run_jump(1)
        second = self.run_jump(1)
        other = self.run_jump(1, session_id=other_session_id)

        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(other.returncode, 0, other.stderr)
        other_index = self.tmux_output(
            "display-message",
            "-p",
            "-t",
            other_session,
            "#{window_index}",
        )
        self.assertEqual(other_index, "1")

    def test_missing_target_stays_put_and_blocks_same_digit_until_silence(self):
        self.tmux(
            "set-option", "-g", "@tmux-window-jump-timeout-ms", "2000"
        )

        first = self.run_jump(9)
        missing = self.run_jump(9)
        self.add_windows_through(19)
        blocked = self.run_jump(9)

        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(missing.returncode, 0, missing.stderr)
        self.assertEqual(blocked.returncode, 0, blocked.stderr)
        self.assertEqual(self.current_window(), 9)

    def test_missing_target_unlocks_after_timeout_silence(self):
        self.tmux(
            "set-option", "-g", "@tmux-window-jump-timeout-ms", "80"
        )

        first = self.run_jump(9)
        missing = self.run_jump(9)
        self.add_windows_through(19)
        time.sleep(0.15)
        restarted = self.run_jump(9)
        advanced = self.run_jump(9)

        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(missing.returncode, 0, missing.stderr)
        self.assertEqual(restarted.returncode, 0, restarted.stderr)
        self.assertEqual(advanced.returncode, 0, advanced.stderr)
        self.assertEqual(self.current_window(), 19)

    def test_missing_target_displays_one_message_while_blocked(self):
        client_environment = os.environ.copy()
        client_environment.pop("TMUX", None)
        client_environment.pop("TMUX_PANE", None)
        client_environment["TERM"] = "xterm-256color"
        child_pid, master_fd = pty.fork()
        if child_pid == 0:
            os.execvpe(
                "tmux",
                [
                    "tmux",
                    "-L",
                    self.socket,
                    "attach-session",
                    "-t",
                    self.session,
                ],
                client_environment,
            )
        try:
            client_key = ""
            for _ in range(50):
                client_key = self.tmux_output(
                    "list-clients", "-F", "#{client_pid}"
                )
                if client_key:
                    break
                time.sleep(0.02)
            self.assertTrue(client_key, "tmux client did not attach")
            self.tmux(
                "set-option",
                "-g",
                "@tmux-window-jump-timeout-ms",
                "2000",
            )

            first = self.run_jump(9, client_key=client_key)
            missing = self.run_jump(9, client_key=client_key)
            blocked = self.run_jump(9, client_key=client_key)

            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertEqual(missing.returncode, 0, missing.stderr)
            self.assertEqual(blocked.returncode, 0, blocked.stderr)
            messages = self.tmux_output(
                "show-messages", "-t", client_key
            )
            self.assertEqual(messages.count("Window 19 does not exist"), 1)
        finally:
            # Close the PTY before waiting: terminal teardown can otherwise
            # block in macOS while the unread master is still open.
            os.close(master_fd)
            try:
                os.kill(child_pid, signal.SIGHUP)
            except ProcessLookupError:
                pass
            os.waitpid(child_pid, 0)

    def test_end_selects_last_window_and_resets_digit_chain(self):
        first = self.run_jump(1)
        end = self.run_end()
        end_window = self.current_window()
        restarted = self.run_jump(1)

        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(end.returncode, 0, end.stderr)
        self.assertEqual(end_window, 11)
        self.assertEqual(restarted.returncode, 0, restarted.stderr)
        self.assertEqual(self.current_window(), 1)


if __name__ == "__main__":
    unittest.main()
