"""Structural tests for public Ghostty/tmux workstation fragments.

Drives the shipped conf files under conf/ (not re-implemented copies).
"""

from __future__ import annotations

import json
import os
import pty
import re
import select
import shutil
import signal
import subprocess
import tempfile
import termios
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
README = ROOT / "README.md"
README_CN = ROOT / "README_CN.md"
GHOSTTY_CONF = ROOT / "conf" / "ghostty-tmux.conf"
TMUX_WS = ROOT / "conf" / "tmux-workstation.conf"
TMUX_WRAP = ROOT / "conf" / "tmux-window-wrap.conf"
WRAP_BIN = ROOT / "bin" / "tmux-window-wrap"
DOCTOR_BIN = ROOT / "bin" / "aipane-doctor"
WINDOW_JUMP_BIN = ROOT / "bin" / "tmux-window-jump"
LAYOUT_CYCLE_BIN = ROOT / "bin" / "tmux-layout-cycle"
PANE_SYNC_BIN = ROOT / "bin" / "tmux-pane-sync"
RENAME_BIN = ROOT / "bin" / "tmux-rename-window-popup"
PICKER_BIN = ROOT / "bin" / "tmux-session-picker"
SESSION_PICK_BIN = ROOT / "bin" / "tmux-session-pick"
CLOSE_POPUP_BIN = ROOT / "bin" / "tmux-close-window-popup"
COLOUR_PALETTE_BIN = ROOT / "bin" / "tmux-colour-palette"
CHEATSHEET = ROOT / "docs" / "cheatsheet.md"
WS_DOC = ROOT / "docs" / "ghostty-tmux-workstation.md"


class GhosttyFragmentTests(unittest.TestCase):
    def test_ghostty_fragment_defines_one_shot_cmd_bridge_and_shift_enter(self):
        text = GHOSTTY_CONF.read_text(encoding="utf-8")
        self.assertIn("keybind = cmd+s=text:\\x00", text)
        self.assertNotIn("keybind = cmd+s=unbind", text)
        self.assertIn("keybind = super+alt+p=text:\\x00Q", text)
        self.assertIn("keybind = super+alt+y=text:\\x00y", text)
        self.assertIn("keybind = super+alt+shift+y=text:\\x00Y", text)
        self.assertIn("keybind = cmd+t=text:\\x00c", text)
        self.assertIn("keybind = cmd+d=text:\\x00|", text)
        self.assertIn("keybind = cmd+b=text:\\x00T", text)
        self.assertIn("keybind = super+alt+i=text:\\x00B", text)
        self.assertNotIn("super+alt+shift+i", text)
        self.assertIn("keybind = shift+enter=text:\\x0a", text)
        self.assertIn("macos-option-as-alt = true", text)
        for digit in range(10):
            self.assertIn(
                f"keybind = cmd+digit_{digit}=text:\\x00{digit}", text
            )
        # public fragment must not pin personal fonts
        self.assertNotIn("font-family", text)


class TmuxWorkstationFragmentTests(unittest.TestCase):
    def test_copy_mode_goto_matches_absolute_line_numbers(self):
        socket = f"ws-copy-goto-{os.getpid()}-{id(self)}"
        environment = os.environ.copy()
        environment.pop("TMUX", None)
        environment.pop("TMUX_PANE", None)
        environment["TERM"] = "xterm-256color"
        child_pid = master_fd = None
        terminal_output = bytearray()

        def tmux(*args: str, check: bool = True):
            return subprocess.run(
                ["tmux", "-L", socket, *args], check=check,
                capture_output=True, text=True, env=environment,
            )

        def state(expression: str) -> str:
            return tmux("display-message", "-p", "-t", pane, expression).stdout.strip()

        def wait_until(predicate) -> bool:
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                if master_fd is not None:
                    while True:
                        try:
                            chunk = os.read(master_fd, 65536)
                            if not chunk:
                                break
                            terminal_output.extend(chunk)
                        except BlockingIOError:
                            break
                if predicate():
                    return True
                time.sleep(0.02)
            return predicate()

        def enter_line(line) -> None:
            terminal_output.clear()
            os.write(master_fd, b":")
            self.assertTrue(wait_until(lambda: b"(goto line)" in terminal_output))
            os.write(master_fd, f"{line}\r".encode())

        def jump(line: int, expected: int | None = None) -> None:
            enter_line(line)
            expected = line if expected is None else expected
            self.assertTrue(
                wait_until(lambda: state("#{e|+:#{copy_position},#{copy_cursor_y}}") == str(expected)),
                f":{line} left cursor on {state('#{copy_cursor_line}')}; "
                f"viewport starts at {state('#{copy_position}')}; "
                f"terminal output: {bytes(terminal_output[-1000:])!r}",
            )

        try:
            pane = tmux(
                "-f", "/dev/null", "new-session", "-d", "-P", "-F", "#{pane_id}",
                "-s", "copy-goto", "-x", "80", "-y", "24", "seq 1 200; sleep 120",
            ).stdout.strip()
            tmux("source-file", str(TMUX_WS))
            child_pid, master_fd = pty.fork()
            if child_pid == 0:
                termios.tcsetwinsize(0, (24, 80))
                os.execvpe(
                    "tmux", ["tmux", "-L", socket, "attach-session", "-t", "copy-goto"],
                    environment,
                )
            os.set_blocking(master_fd, False)
            self.assertTrue(wait_until(lambda: bool(tmux("list-clients").stdout)))
            self.assertTrue(wait_until(
                lambda: "200" in tmux("capture-pane", "-p", "-t", pane).stdout.splitlines()
            ))
            # Drive the physical bridge's NUL + v, then the actual : prompt.
            os.write(master_fd, b"\x00v")
            self.assertTrue(wait_until(lambda: state("#{pane_in_mode}") == "1"))
            for line in (120, 1, 200, 125, 120):
                jump(line)
                self.assertEqual(state("#{copy_cursor_line}"), str(line))
            # The trailing blank line is also part of the buffer. Clamp large
            # requests there without trying to repeat billions of movements.
            jump(4294967296, expected=int(state("#{copy_position_limit}")))
            jump(0, expected=1)
            jump(120)

            # Keep the selection anchor while jumping in either direction.
            os.write(master_fd, b"V")
            self.assertTrue(wait_until(lambda: state("#{selection_present}") == "1"))
            jump(125)
            tmux("send-keys", "-t", pane, "-X", "copy-selection-and-cancel")
            self.assertEqual(
                tmux("show-buffer").stdout.strip(),
                "\n".join(str(line) for line in range(120, 126)),
            )
            os.write(master_fd, b"\x00v")
            self.assertTrue(wait_until(lambda: state("#{pane_in_mode}") == "1"))
            jump(125)
            os.write(master_fd, b"V")
            self.assertTrue(wait_until(lambda: state("#{selection_present}") == "1"))
            jump(120)
            tmux("send-keys", "-t", pane, "-X", "copy-selection-and-cancel")
            self.assertEqual(
                tmux("show-buffer").stdout.strip(),
                "\n".join(str(line) for line in range(120, 126)),
            )
            # With legacy numbering, retain tmux's native offset semantics.
            os.write(master_fd, b"\x00v")
            self.assertTrue(wait_until(lambda: state("#{pane_in_mode}") == "1"))
            tmux("set-option", "-w", "-t", pane, "copy-mode-line-numbers", "off")
            cursor_y = state("#{copy_cursor_y}")
            enter_line(120)
            self.assertTrue(wait_until(lambda: state("#{scroll_position}") == "120"))
            self.assertEqual(state("#{copy_cursor_y}"), cursor_y)
        finally:
            tmux("kill-server", check=False)
            if master_fd is not None:
                os.close(master_fd)
            if child_pid is not None:
                try:
                    os.kill(child_pid, signal.SIGHUP)
                except ProcessLookupError:
                    pass
                os.waitpid(child_pid, 0)

    def test_status_menu_kill_guards_every_pane_in_the_clicked_window(self):
        socket = f"ws-window-menu-{os.getpid()}-{id(self)}"
        child_pid = None
        master_fd = None
        terminal_output = bytearray()
        environment = os.environ.copy()
        environment.pop("TMUX", None)
        environment.pop("TMUX_PANE", None)
        environment["TERM"] = "xterm-256color"
        environment["PATH"] = f"{ROOT / 'bin'}{os.pathsep}{environment['PATH']}"

        def tmux(*args: str, check: bool = True):
            return subprocess.run(
                ["tmux", "-L", socket, *args],
                check=check,
                capture_output=True,
                text=True,
                env=environment,
            )

        def drain_terminal() -> bytes:
            output = b""
            if master_fd is not None:
                while True:
                    try:
                        chunk = os.read(master_fd, 65536)
                        if not chunk:
                            break
                        output += chunk
                        terminal_output.extend(chunk)
                    except BlockingIOError:
                        break
            return output

        def wait_until(predicate, timeout: float = 3.0) -> bool:
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                drain_terminal()
                if predicate():
                    return True
                time.sleep(0.02)
            return predicate()

        def window_ids() -> set[str]:
            return set(
                tmux("list-windows", "-t", "window-menu", "-F", "#{window_id}")
                .stdout.splitlines()
            )

        def menu_count(centered: bool = False) -> int:
            return sum(
                (": display-popup" if centered else ": display-menu") in line
                and (not centered or "-x C -y C" in line)
                for line in tmux("show-messages", "-t", client).stdout.splitlines()
            )

        def new_target(command: str = "sleep 120") -> str:
            return tmux(
                "new-window", "-d", "-P", "-F", "#{window_id}",
                "-t", "window-menu:1", "-n", "target", command,
            ).stdout.strip()

        def invoke_menu_kill(*, option: bool = False) -> int:
            # Separate clicks so tmux does not interpret them as DoubleClick3.
            time.sleep(0.35)
            terminal_output.clear()
            menus = menu_count()
            prompts = menu_count(centered=True)
            # SGR mouse events hit the inactive window's actual status range.
            button = 10 if option else 2
            os.write(
                master_fd,
                f"\x1b[<{button};9;30M\x1b[<{button};9;30m".encode(),
            )
            self.assertTrue(
                wait_until(lambda: menu_count() > menus),
                "right-click did not open the window status menu:\n"
                + tmux("show-messages", "-t", client).stdout,
            )
            tmux("send-keys", "-K", "-c", client, "X")
            return prompts

        def assert_guarded(target: str, *, option: bool = False) -> None:
            prompts = invoke_menu_kill(option=option)
            self.assertTrue(
                wait_until(
                    lambda: menu_count(centered=True) > prompts
                    or target not in window_ids()
                ),
                "the window menu Kill action did not resolve",
            )
            self.assertIn(
                target, window_ids(),
                "window menu Kill destroyed an Agent/unknown window without "
                "confirmation",
            )
            self.assertGreater(menu_count(centered=True), prompts)
            self.assertTrue(
                wait_until(lambda: b"\x1b[?1003h" in terminal_output),
                "close popup did not enable input",
            )
            self.assertEqual(
                tmux("display-message", "-p", "-t", "window-menu", "#{window_id}")
                .stdout.strip(),
                active_window,
                "right-clicking an inactive tab changed the active window",
            )

        try:
            tmux(
                "-f", "/dev/null", "new-session", "-d", "-s", "window-menu",
                "-x", "100", "-y", "30", "sleep 120",
            )
            tmux("source-file", str(TMUX_WS))
            tmux("set-option", "-g", "@aipane-agent-status-command", str(WRAP_BIN))
            tmux("set-option", "-g", "renumber-windows", "off")
            tmux("set-option", "-g", "status-position", "bottom")
            tmux(
                "set-option", "-g", "status-format[0]",
                "#[range=window|0]ACTIVE#[norange] "
                "#[range=window|1]TARGET#[norange]",
            )
            active_window = tmux(
                "display-message", "-p", "-t", "window-menu:0", "#{window_id}",
            ).stdout.strip()
            target = new_target()
            tmux("split-window", "-d", "-t", target, "sleep 120")

            child_pid, master_fd = pty.fork()
            if child_pid == 0:
                termios.tcsetwinsize(0, (30, 100))
                os.execvpe(
                    "tmux", ["tmux", "-L", socket, "attach-session", "-t", "window-menu"],
                    environment,
                )
            os.set_blocking(master_fd, False)
            client = ""
            for _ in range(100):
                client = tmux("list-clients", "-F", "#{client_name}").stdout.strip()
                if client:
                    break
                time.sleep(0.02)
            self.assertTrue(client, "tmux client did not attach")
            initial_output = b""
            for _ in range(100):
                initial_output += drain_terminal()
                if b"TARGET" in initial_output:
                    break
                time.sleep(0.02)
            self.assertIn(b"TARGET", initial_output)

            # A plain window closes immediately, without touching the active tab.
            prompts = invoke_menu_kill()
            self.assertTrue(wait_until(lambda: target not in window_ids()))
            self.assertEqual(menu_count(centered=True), prompts)
            self.assertIn(active_window, window_ids())

            with tempfile.TemporaryDirectory() as raw_tmp:
                fake_agent = Path(raw_tmp) / "codex"
                fake_agent.symlink_to("/bin/sleep")
                # The original reported case: one last pane running an Agent.
                target = new_target(f"{fake_agent} 120")
                assert_guarded(target)
                tmux("send-keys", "-K", "-c", client, "y")
                self.assertTrue(wait_until(lambda: target not in window_ids()))
                self.assertIn(active_window, window_ids())

                target = new_target()
                agent_pane = tmux(
                    "split-window", "-d", "-P", "-F", "#{pane_id}", "-t", target,
                    f"{fake_agent} 120",
                ).stdout.strip()
                # The selected pane is ordinary; the Agent runs in another pane.
                self.assertNotEqual(
                    tmux("display-message", "-p", "-t", target, "#{pane_id}")
                    .stdout.strip(), agent_pane,
                )
                detected = subprocess.run(
                    [str(WRAP_BIN), "pane-agent-status", "--socket-name", socket,
                     "--pane", agent_pane],
                    check=True, capture_output=True, text=True, env=environment,
                ).stdout.strip()
                self.assertEqual(detected, "agent")

                assert_guarded(target)
                tmux("send-keys", "-K", "-c", client, "n")
                self.assertIn(target, window_ids())
                assert_guarded(target, option=True)
                tmux("send-keys", "-K", "-c", client, "Escape")
                self.assertIn(target, window_ids())
                assert_guarded(target)
                # A tab can move while its prompt is open. Keep the original ID.
                tmux("move-window", "-s", target, "-t", "window-menu:2")
                replacement = new_target()
                tmux("send-keys", "-K", "-c", client, "y")
                self.assertTrue(wait_until(lambda: target not in window_ids()))
                self.assertIn(active_window, window_ids())
                self.assertIn(replacement, window_ids())
                tmux("kill-window", "-t", replacement)

                # Even a reassuring stdout value is unknown if detection failed.
                detector = Path(raw_tmp) / "detector's command"
                detector.write_text("#!/bin/sh\nprintf 'none\\n'\nexit 1\n")
                detector.chmod(0o755)
                tmux("set-option", "-g", "@aipane-agent-status-command", str(detector))
                target = new_target()
                assert_guarded(target)
                tmux("send-keys", "-K", "-c", client, "y")
                self.assertTrue(wait_until(lambda: target not in window_ids()))

            # Failure to classify also needs a confirmation, including Option-click.
            tmux(
                "set-option", "-g", "@aipane-agent-status-command",
                str(ROOT / "bin" / "missing-agent-detector"),
            )
            target = new_target()
            assert_guarded(target, option=True)
            tmux("send-keys", "-K", "-c", client, "n")
            self.assertIn(target, window_ids())
            self.assertIn(active_window, window_ids())
        finally:
            tmux("kill-server", check=False)
            if master_fd is not None:
                os.close(master_fd)
            if child_pid is not None:
                try:
                    os.kill(child_pid, signal.SIGHUP)
                except ProcessLookupError:
                    pass
                for _ in range(50):
                    if os.waitpid(child_pid, os.WNOHANG)[0]:
                        break
                    time.sleep(0.02)
                else:
                    os.kill(child_pid, signal.SIGKILL)
                    os.waitpid(child_pid, 0)

    def test_last_agent_close_popup_uses_click_coordinates(self):
        socket = f"ws-last-agent-mouse-{os.getpid()}-{id(self)}"
        child_pid = None
        master_fd = None
        terminal_output = bytearray()
        environment = os.environ.copy()
        environment.pop("TMUX", None)
        environment.pop("TMUX_PANE", None)
        environment["TERM"] = "xterm-256color"
        environment["PATH"] = f"{ROOT / 'bin'}{os.pathsep}{environment['PATH']}"

        def tmux(*args: str, check: bool = True):
            return subprocess.run(
                ["tmux", "-L", socket, *args],
                check=check, capture_output=True, text=True, env=environment,
            )

        def window_ids() -> set[str]:
            return set(
                tmux(
                    "list-windows", "-t", "last-agent-mouse", "-F", "#{window_id}",
                ).stdout.splitlines()
            )

        def wait_until(predicate, timeout: float = 2.0) -> bool:
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if master_fd is not None:
                    try:
                        while chunk := os.read(master_fd, 65536):
                            terminal_output.extend(chunk)
                    except BlockingIOError:
                        pass
                if predicate():
                    return True
                time.sleep(0.02)
            return predicate()

        try:
            with tempfile.TemporaryDirectory() as raw_tmp:
                fake_agent = Path(raw_tmp) / "codex"
                fake_agent.symlink_to("/bin/sleep")
                tmux(
                    "-f", "/dev/null", "new-session", "-d", "-s",
                    "last-agent-mouse", "-x", "100", "-y", "30", "sleep 120",
                )
                tmux("source-file", str(TMUX_WS))
                tmux("set-option", "-g", "@aipane-agent-status-command", str(WRAP_BIN))
                agent_window = tmux(
                    "new-window", "-P", "-F", "#{window_id}",
                    "-t", "last-agent-mouse:", f"{fake_agent} 120",
                ).stdout.strip()
                agent_pane = tmux(
                    "display-message", "-p", "-t", agent_window, "#{pane_id}",
                ).stdout.strip()
                detected = subprocess.run(
                    [str(WRAP_BIN), "pane-agent-status", "--socket-name", socket,
                     "--pane", agent_pane],
                    check=True, capture_output=True, text=True, env=environment,
                ).stdout.strip()
                self.assertEqual(detected, "agent")

                child_pid, master_fd = pty.fork()
                if child_pid == 0:
                    termios.tcsetwinsize(0, (30, 100))
                    os.execvpe(
                        "tmux", ["tmux", "-L", socket, "attach-session", "-t", "last-agent-mouse"],
                        environment,
                    )
                os.set_blocking(master_fd, False)
                client = ""
                for _ in range(100):
                    client = tmux("list-clients", "-F", "#{client_name}").stdout.strip()
                    if client:
                        break
                    time.sleep(0.02)
                self.assertTrue(client, "tmux client did not attach")

                def open_popup() -> None:
                    terminal_output.clear()
                    tmux("switch-client", "-c", client, "-T", "prefix")
                    tmux("send-keys", "-K", "-c", client, "x")
                    self.assertTrue(
                        wait_until(lambda: b"\x1b[?1003h" in terminal_output),
                        "last Agent pane did not open the confirmation popup",
                    )
                    self.assertIn(agent_window, window_ids())

                def hover_from_outside(row: int) -> None:
                    terminal_output.clear()
                    # SGR 35 is buttonless motion. Moving outside and then
                    # inside must redraw the popup, without choosing an item.
                    os.write(
                        master_fd,
                        f"\x1b[<35;10;25M\x1b[<35;50;{row}M".encode(),
                    )
                    self.assertTrue(
                        wait_until(lambda: any(
                            label in terminal_output for label in (b"Cancel", b"Close window")
                        )),
                        "mouse motion dismissed the confirmation popup",
                    )
                    self.assertIn(
                        agent_window, window_ids(),
                        "hovering Close window confirmed without a click",
                    )

                # A 100x30 client centers Cancel at (50, 14) and Close window
                # at (50, 15). Cancellation must consume the popup, so a later
                # y goes to the pane and cannot close the Agent window.
                open_popup()
                hover_from_outside(14)
                terminal_output.clear()
                # Leave the pointer over Cancel while the keyboard selects
                # Close window. The click must use its own coordinates.
                os.write(master_fd, b"\x1b[B")
                self.assertTrue(
                    wait_until(lambda: b"Close window" in terminal_output),
                    "the Down key did not redraw the popup selection",
                )
                terminal_output.clear()
                os.write(master_fd, b"\x1b[<0;50;14M\x1b[<0;50;14m")
                self.assertTrue(
                    wait_until(lambda: b"\x1b[?1003l" in terminal_output),
                    "clicking Cancel did not dismiss the popup",
                )
                self.assertIn(
                    agent_window, window_ids(),
                    "clicking Cancel executed the keyboard-selected Close window",
                )
                tmux("send-keys", "-K", "-c", client, "y")
                self.assertIn(agent_window, window_ids())

                for key in ("n", "Escape", "Enter"):
                    open_popup()
                    terminal_output.clear()
                    tmux("send-keys", "-K", "-c", client, key)
                    self.assertTrue(
                        wait_until(lambda: b"\x1b[?1003l" in terminal_output),
                        f"{key} did not cancel the initial popup",
                    )
                    tmux("send-keys", "-K", "-c", client, "y")
                    self.assertIn(agent_window, window_ids())

                open_popup()
                hover_from_outside(15)
                # A click outside a native popup is ignored. The explicit
                # cancellation actions remain n, Escape, or the Cancel row.
                os.write(master_fd, b"\x1b[<0;10;25M\x1b[<0;10;25m")
                tmux("send-keys", "-K", "-c", client, "Up")
                terminal_output.clear()
                # The pointer is still over Close window, but the keyboard
                # now selects Cancel. Click must still choose Close window.
                os.write(master_fd, b"\x1b[<0;50;15M\x1b[<0;50;15m")
                self.assertTrue(
                    wait_until(lambda: agent_window not in window_ids()),
                    "clicking Close window used the keyboard-selected Cancel",
                )

                # No motion event is required before the first click, and
                # Enter still follows keyboard selection.
                for decision in (b"\x1b[<0;50;15M\x1b[<0;50;15m", b"\x1b[B\x1b[B\r"):
                    agent_window = tmux(
                        "new-window", "-P", "-F", "#{window_id}",
                        "-t", "last-agent-mouse:", f"{fake_agent} 120",
                    ).stdout.strip()
                    open_popup()
                    os.write(master_fd, decision)
                    self.assertTrue(
                        wait_until(lambda: agent_window not in window_ids()),
                        "an explicit click or keyboard confirmation did not close the window",
                    )
        finally:
            tmux("kill-server", check=False)
            if master_fd is not None:
                os.close(master_fd)
            if child_pid is not None:
                try:
                    os.kill(child_pid, signal.SIGHUP)
                except ProcessLookupError:
                    pass
                for _ in range(50):
                    if os.waitpid(child_pid, os.WNOHANG)[0]:
                        break
                    time.sleep(0.02)
                else:
                    os.kill(child_pid, signal.SIGKILL)
                    os.waitpid(child_pid, 0)

    def test_kill_pane_only_confirms_for_a_last_agent_pane(self):
        socket = f"ws-kill-pane-{os.getpid()}-{id(self)}"
        child_pid = None
        master_fd = None
        terminal_output = bytearray()
        client_environment = os.environ.copy()
        client_environment.pop("TMUX", None)
        client_environment.pop("TMUX_PANE", None)
        client_environment["TERM"] = "xterm-256color"
        client_environment["PATH"] = f"{ROOT / 'bin'}{os.pathsep}{client_environment['PATH']}"

        def tmux(*args: str, check: bool = True):
            return subprocess.run(
                ["tmux", "-L", socket, *args],
                check=check,
                capture_output=True,
                text=True,
                env=client_environment,
            )

        def window_ids() -> set[str]:
            return set(
                tmux(
                    "list-windows",
                    "-t",
                    "kill-pane",
                    "-F",
                    "#{window_id}",
                ).stdout.splitlines()
            )

        def wait_until(predicate, timeout: float = 2.0) -> bool:
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if master_fd is not None:
                    try:
                        while chunk := os.read(master_fd, 65536):
                            terminal_output.extend(chunk)
                    except BlockingIOError:
                        pass
                if predicate():
                    return True
                time.sleep(0.02)
            return predicate()

        try:
            tmux(
                "-f",
                "/dev/null",
                "new-session",
                "-d",
                "-s",
                "kill-pane",
                "sleep 30",
            )
            tmux(
                "set-option",
                "-g",
                "@aipane-agent-status-command",
                str(WRAP_BIN),
            )
            tmux("source-file", str(TMUX_WS))
            guarded_window = tmux(
                "display-message",
                "-p",
                "-t",
                "kill-pane",
                "#{window_id}",
            ).stdout.strip()
            tmux("new-window", "-d", "-t", "kill-pane:", "sleep 30")
            tmux("select-window", "-t", guarded_window)
            tmux("split-window", "-d", "-t", guarded_window, "sleep 30")

            child_pid, master_fd = pty.fork()
            if child_pid == 0:
                termios.tcsetwinsize(0, (30, 100))
                os.execvpe(
                    "tmux", ["tmux", "-L", socket, "attach-session", "-t", "kill-pane"],
                    client_environment,
                )
            os.set_blocking(master_fd, False)

            client = ""
            for _ in range(50):
                client = tmux(
                    "list-clients", "-F", "#{client_name}"
                ).stdout.strip()
                if client:
                    break
                time.sleep(0.02)
            self.assertTrue(client, "tmux client did not attach")

            def invoke_kill_pane_binding() -> None:
                terminal_output.clear()
                tmux("switch-client", "-c", client, "-T", "prefix")
                tmux("send-keys", "-K", "-c", client, "x")

            def centered_prompt_count() -> int:
                return tmux("show-messages", "-t", client).stdout.count(
                    "command: display-popup"
                )

            def popup_key(key: str) -> None:
                self.assertTrue(
                    wait_until(lambda: b"\x1b[?1003h" in terminal_output),
                    "close popup did not enable input",
                )
                terminal_output.clear()
                tmux("send-keys", "-K", "-c", client, key)
                self.assertTrue(
                    wait_until(lambda: b"\x1b[?1003l" in terminal_output),
                    "close popup did not finish after a decision",
                )

            # Multiple panes use the immediate cleanup path.
            invoke_kill_pane_binding()
            panes = tmux(
                "display-message",
                "-p",
                "-t",
                guarded_window,
                "#{window_panes}",
            ).stdout.strip()
            self.assertEqual(
                panes, "1", "prefix+x did not immediately close a split pane"
            )

            # A non-Agent last pane closes without entering a confirmation prompt.
            invoke_kill_pane_binding()
            self.assertTrue(
                wait_until(lambda: guarded_window not in window_ids()),
                "prefix+x prompted before closing a non-Agent window",
            )

            with tempfile.TemporaryDirectory() as raw_tmp:
                fake_agent = Path(raw_tmp) / "codex"
                fake_agent.symlink_to("/bin/sleep")
                agent_window = tmux(
                    "new-window",
                    "-d",
                    "-P",
                    "-F",
                    "#{window_id}",
                    "-t",
                    "kill-pane:",
                    f"{fake_agent} 30",
                ).stdout.strip()
                tmux("switch-client", "-c", client, "-t", agent_window)

                prompts = centered_prompt_count()
                invoke_kill_pane_binding()
                wait_until(
                    lambda: centered_prompt_count() > prompts
                    or agent_window not in window_ids()
                )
                self.assertIn(
                    agent_window,
                    window_ids(),
                    "prefix+x closed an Agent window without confirmation",
                )
                self.assertGreater(centered_prompt_count(), prompts)

                popup_key("n")
                self.assertIn(agent_window, window_ids())

                prompts = centered_prompt_count()
                invoke_kill_pane_binding()
                self.assertTrue(
                    wait_until(lambda: centered_prompt_count() > prompts),
                    "prefix+x did not show the centered popup again after cancel",
                )
                popup_key("y")
                self.assertTrue(
                    wait_until(lambda: agent_window not in window_ids()),
                    "confirming the centered prompt did not close the Agent "
                    f"window:\n{tmux('show-messages', '-t', client).stdout}",
                )

            tmux(
                "set-option",
                "-g",
                "@aipane-agent-status-command",
                str(ROOT / "bin" / "missing-agent-detector"),
            )
            unknown_window = tmux(
                "new-window",
                "-d",
                "-P",
                "-F",
                "#{window_id}",
                "-t",
                "kill-pane:",
                "sleep 30",
            ).stdout.strip()
            tmux("switch-client", "-c", client, "-t", unknown_window)

            prompts = centered_prompt_count()
            invoke_kill_pane_binding()
            wait_until(
                lambda: centered_prompt_count() > prompts
                or unknown_window not in window_ids()
            )
            self.assertIn(
                unknown_window,
                window_ids(),
                "an unavailable Agent detector allowed the window to close",
            )
            self.assertGreater(centered_prompt_count(), prompts)
            popup_key("n")
            tmux("set-option", "-g", "@aipane-close-window-popup-command", "/missing/close-popup")
            prompts = centered_prompt_count()
            invoke_kill_pane_binding()
            self.assertTrue(
                wait_until(lambda: b"Close cancelled" in terminal_output),
                "a missing confirmation program was not reported",
            )
            self.assertEqual(centered_prompt_count(), prompts)
            self.assertIn(unknown_window, window_ids())
        finally:
            tmux("kill-server", check=False)
            if master_fd is not None:
                os.close(master_fd)
            if child_pid is not None:
                try:
                    os.kill(child_pid, signal.SIGHUP)
                except ProcessLookupError:
                    pass
                for _ in range(50):
                    if os.waitpid(child_pid, os.WNOHANG)[0]:
                        break
                    time.sleep(0.02)
                else:
                    os.kill(child_pid, signal.SIGKILL)
                    os.waitpid(child_pid, 0)

    def test_workstation_sets_c_space_prefix_and_broadcast(self):
        text = TMUX_WS.read_text(encoding="utf-8")
        self.assertIn("set -g prefix C-Space", text)
        self.assertIn("synchronize-panes", text)
        self.assertIn("window_zoomed_flag", text)
        self.assertIn("bind v copy-mode", text)
        self.assertIn("bind P display-popup", text)
        self.assertIn("bind Q if-shell", text)
        self.assertIn("tmux-session-picker", text)
        self.assertIn("AIPANE_PICKER_ORIGIN_PANE=#{pane_id}", text)
        self.assertIn("bind y run-shell", text)
        self.assertIn("unbind -n DoubleClick1Status", text)
        self.assertIn("unbind -n MouseDown1Border", text)
        self.assertIn("bind -n DoubleClick1Border resize-pane -Z -t =", text)
        self.assertIn("bind -n MouseDown3Border display-menu", text)
        self.assertIn("bind -n M-MouseDown3Border display-menu", text)
        self.assertIn("unbind A", text)
        self.assertNotIn("bind A display-popup", text)
        self.assertIn("bind , run-shell -C", text)
        self.assertIn("AIPANE_RENAME_WINDOW_TARGET=#{window_id}", text)
        self.assertIn("-w 52 -h 5", text)
        self.assertIn('-T "#[align=centre] Rename window #I "', text)
        self.assertIn("tmux-rename-window-popup", text)
        self.assertIn("tmux-colour-palette", text)
        self.assertIn("tmux-shot-capture", text)
        self.assertIn("display-menu", text)
        self.assertIn("-x C -y C", text)
        self.assertNotIn('confirm-before -p "Close last pane', text)
        self.assertNotIn("#{session_id}", text)
        self.assertTrue(WINDOW_JUMP_BIN.is_file())
        self.assertTrue(os.access(WINDOW_JUMP_BIN, os.X_OK))
        self.assertTrue(PANE_SYNC_BIN.is_file())
        self.assertTrue(os.access(PANE_SYNC_BIN, os.X_OK))
        self.assertTrue(RENAME_BIN.is_file())
        self.assertTrue(os.access(RENAME_BIN, os.X_OK))
        self.assertTrue(PICKER_BIN.is_file())
        self.assertTrue(os.access(PICKER_BIN, os.X_OK))
        self.assertTrue(SESSION_PICK_BIN.is_file())
        self.assertTrue(os.access(SESSION_PICK_BIN, os.X_OK))
        self.assertTrue(COLOUR_PALETTE_BIN.is_file())
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
            base_index = subprocess.run(
                ["tmux", "-L", socket, "show-options", "-gqv", "base-index"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            self.assertEqual(base_index, "1")
            first_window = subprocess.run(
                [
                    "tmux", "-L", socket, "new-session", "-d", "-P",
                    "-F", "#{window_index}", "-s", "from-one", "sleep 5",
                ],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            self.assertEqual(first_window, "1")
            jump_timeout = subprocess.run(
                [
                    "tmux",
                    "-L",
                    socket,
                    "show-options",
                    "-g",
                    "-v",
                    "@tmux-window-jump-timeout-ms",
                ],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            self.assertEqual(jump_timeout, "700")
            keys = subprocess.run(
                ["tmux", "-L", socket, "list-keys", "-T", "prefix"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout
            self.assertRegex(keys, re.compile(r"prefix\s+B\s+.*tmux-pane-sync.*synchronize-panes"))
            self.assertRegex(keys, re.compile(r"prefix\s+e\s+.*tmux-pane-sync.*edit"))
            sync_pick_keys = subprocess.run(
                ["tmux", "-L", socket, "list-keys", "-T", "sync-pick"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout
            self.assertRegex(sync_pick_keys, re.compile(r"sync-pick\s+Enter\s+.*commit"))
            self.assertRegex(sync_pick_keys, re.compile(r"sync-pick\s+q\s+.*cancel"))
            self.assertRegex(sync_pick_keys, re.compile(r"sync-pick\s+Escape\s+.*cancel"))
            self.assertRegex(sync_pick_keys, re.compile(r"sync-pick\s+MouseDown1Border\s+.*pick-pane"))
            self.assertRegex(sync_pick_keys, re.compile(r"sync-pick\s+DoubleClick1Border\s+.*switch-client -T sync-pick"))
            self.assertRegex(sync_pick_keys, re.compile(r"sync-pick\s+MouseDown3Border\s+.*switch-client -T sync-pick"))
            self.assertRegex(sync_pick_keys, re.compile(r"sync-pick\s+M-MouseDown3Border\s+.*switch-client -T sync-pick"))
            self.assertRegex(sync_pick_keys, re.compile(r"sync-pick\s+Any\s+.*switch-client -T sync-pick"))
            self.assertRegex(keys, re.compile(r"prefix\s+Y\s+.*tmux-session-pick.*enter"))
            session_pick_keys = subprocess.run(
                ["tmux", "-L", socket, "list-keys", "-T", "session-pick"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout
            self.assertRegex(session_pick_keys, re.compile(r"session-pick\s+Enter\s+.*commit"))
            self.assertRegex(session_pick_keys, re.compile(r"session-pick\s+Space\s+.*pick-current"))
            self.assertRegex(session_pick_keys, re.compile(r"session-pick\s+o\s+.*pick-others"))
            self.assertRegex(session_pick_keys, re.compile(r"session-pick\s+a\s+.*pick-all"))
            self.assertRegex(session_pick_keys, re.compile(r"session-pick\s+c\s+.*pick-clear"))
            self.assertRegex(session_pick_keys, re.compile(r"session-pick\s+f\s+.*toggle-format"))
            self.assertRegex(session_pick_keys, re.compile(r"session-pick\s+q\s+.*cancel"))
            self.assertRegex(session_pick_keys, re.compile(r"session-pick\s+Escape\s+.*cancel"))
            self.assertRegex(session_pick_keys, re.compile(r"session-pick\s+MouseDown1Pane\s+.*pick-pane"))
            self.assertRegex(session_pick_keys, re.compile(r"session-pick\s+MouseDown1Border\s+.*pick-pane"))
            self.assertRegex(session_pick_keys, re.compile(r"session-pick\s+DoubleClick1Border\s+.*switch-client -T session-pick"))
            self.assertRegex(session_pick_keys, re.compile(r"session-pick\s+MouseDown3Border\s+.*switch-client -T session-pick"))
            self.assertRegex(session_pick_keys, re.compile(r"session-pick\s+M-MouseDown3Border\s+.*switch-client -T session-pick"))
            self.assertRegex(session_pick_keys, re.compile(r"session-pick\s+Any\s+.*switch-client -T session-pick"))
            self.assertRegex(
                keys,
                re.compile(r"prefix\s+T\s+.*status.*run-shell -b -c /.*set-option status off"),
            )
            self.assertRegex(keys, re.compile(r"prefix\s+v\s+.*copy-mode"))
            self.assertRegex(
                keys,
                re.compile(
                    r"prefix\s+P\s+.*display-popup.*tmux-colour-palette"
                ),
            )
            self.assertRegex(keys, re.compile(r"prefix\s+Q\s+.*display-popup"))
            for digit in range(10):
                self.assertRegex(
                    keys,
                    re.compile(
                        rf"prefix\s+{digit}\s+.*run-shell"
                        rf".*tmux-window-jump.*select {digit}"
                        rf".*select-window.*:={digit}"
                    ),
                )
            self.assertRegex(
                keys,
                re.compile(
                    r"prefix\s+,\s+.*run-shell -C.*display-popup"
                    r".*align=centre"
                    r".*AIPANE_RENAME_WINDOW_TARGET=#\{window_id\}"
                    r".*tmux-rename-window-popup"
                ),
            )
            root_keys = subprocess.run(
                ["tmux", "-L", socket, "list-keys", "-T", "root"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout
            for key_name, cmd_pattern in (
                (r"M-<", r"swap-window -d -t :-1"),
                (r"M->", r"swap-window -d -t :\+1"),
                (r"M-S-<", r"swap-window -d -t :-1"),
                (r"M-S->", r"swap-window -d -t :\+1"),
                (r"M-H", r"resize-pane -L 2"),
                (r"M-J", r"resize-pane -D 2"),
                (r"M-K", r"resize-pane -U 2"),
                (r"M-L", r"resize-pane -R 2"),
                (r"M-S-H", r"resize-pane -L 2"),
                (r"M-S-J", r"resize-pane -D 2"),
                (r"M-S-K", r"resize-pane -U 2"),
                (r"M-S-L", r"resize-pane -R 2"),
                (r"DoubleClick1Border", r"resize-pane -Z -t ="),
                (r"MouseDown3Border", r"display-menu.*Copy Session Ref.*Copy Raw Session ID"),
                (r"M-MouseDown3Border", r"display-menu.*Copy Session Ref.*Copy Raw Session ID"),
            ):
                self.assertRegex(
                    root_keys,
                    re.compile(rf"root\s+{key_name}\s+{cmd_pattern}"),
                )
            self.assertNotRegex(root_keys, re.compile(r"root\s+DoubleClick1Status\b"))
            self.assertNotRegex(root_keys, re.compile(r"root\s+MouseDown1Border\b"))
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

    def test_pane_border_marks_input_off_independently_of_broadcast(self):
        socket = f"ws-input-off-{os.getpid()}-{id(self)}"

        def tmux(*args: str):
            return subprocess.run(
                ["tmux", "-L", socket, *args],
                check=True,
                capture_output=True,
                text=True,
            )

        def border(target: str) -> str:
            return tmux(
                "display-message",
                "-p",
                "-t",
                target,
                "#{E:pane-border-format}",
            ).stdout.strip()

        off_badge = "#[bg=red]#[fg=colour231]#[bold] OFF #[default]"
        try:
            tmux(
                "-f",
                "/dev/null",
                "new-session",
                "-d",
                "-s",
                "input-off",
                "sleep 30",
            )
            tmux("source-file", str(TMUX_WS))
            first = tmux(
                "display-message",
                "-p",
                "-t",
                "input-off:0.0",
                "#{pane_id}",
            ).stdout.strip()
            second = tmux(
                "split-window",
                "-d",
                "-P",
                "-F",
                "#{pane_id}",
                "-t",
                "input-off:0",
                "sleep 30",
            ).stdout.strip()

            plain = border(first)
            self.assertNotIn(off_badge, plain)
            self.assertNotIn("▶ ▶ ▶", plain)

            tmux("set-option", "-w", "-t", first, "synchronize-panes", "on")
            synchronized = border(first)
            self.assertNotIn(off_badge, synchronized)
            self.assertIn("#[fg=red] ▶ ▶ ▶#[default]", synchronized)

            tmux("select-pane", "-d", "-t", first)
            active_off = border(first)
            self.assertIn(off_badge, active_off)
            self.assertNotIn("▶ ▶ ▶", active_off)

            tmux("select-pane", "-t", second)
            inactive_off = border(first)
            self.assertIn(off_badge, inactive_off)
            self.assertNotIn("▶ ▶ ▶", inactive_off)

            tmux("set-option", "-w", "-t", first, "synchronize-panes", "off")
            broadcast_off = border(first)
            self.assertIn(off_badge, broadcast_off)
            self.assertNotIn("▶ ▶ ▶", broadcast_off)

            tmux("select-pane", "-e", "-t", first)
            restored = border(first)
            self.assertNotIn(off_badge, restored)
            self.assertNotIn("▶ ▶ ▶", restored)
        finally:
            subprocess.run(
                ["tmux", "-L", socket, "kill-server"],
                check=False,
                capture_output=True,
            )

    def test_digit_binding_passes_literal_session_id_to_window_jump(self):
        socket = f"ws-jump-binding-{os.getpid()}-{id(self)}"
        child_pid = None
        master_fd = None
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_home:
            tmux_environment = os.environ.copy()
            tmux_environment.pop("TMUX", None)
            tmux_environment.pop("TMUX_PANE", None)
            tmux_environment["HOME"] = temp_home
            tmux_environment["TERM"] = "xterm-256color"

            def tmux(*args: str, check: bool = True):
                return subprocess.run(
                    ["tmux", "-L", socket, *args],
                    check=check,
                    capture_output=True,
                    text=True,
                    env=tmux_environment,
                )

            helper_dir = Path(temp_home) / ".local" / "bin"
            helper_dir.mkdir(parents=True)
            (helper_dir / "tmux-window-jump").symlink_to(WINDOW_JUMP_BIN)
            try:
                tmux(
                    "-f",
                    "/dev/null",
                    "new-session",
                    "-d",
                    "-s",
                    "jump-binding",
                    "sleep 30",
                )
                tmux("source-file", str(TMUX_WS))
                for index in range(1, 21):
                    tmux(
                        "new-window",
                        "-d",
                        "-t",
                        f"jump-binding:{index}",
                        "sleep 30",
                    )
                tmux("select-window", "-t", "jump-binding:2")

                child_pid, master_fd = pty.fork()
                if child_pid == 0:
                    os.execvpe(
                        "tmux",
                        [
                            "tmux",
                            "-L",
                            socket,
                            "attach-session",
                            "-t",
                            "jump-binding",
                        ],
                        tmux_environment,
                    )
                os.set_blocking(master_fd, False)

                client_line = ""
                for _ in range(50):
                    client_line = tmux(
                        "list-clients",
                        "-F",
                        "#{client_pid}\t#{client_name}",
                    ).stdout.strip()
                    if client_line:
                        break
                    time.sleep(0.02)
                self.assertTrue(client_line, "tmux client did not attach")
                client_pid, _, client_target = client_line.partition("\t")
                self.assertTrue(client_pid)
                self.assertTrue(client_target)

                zero_present = True
                cases = (
                    (0, True, True, 0),
                    (1, True, True, 1),
                    (0, False, True, 0),
                    (1, False, True, 1),
                    (0, False, False, 10),
                    (1, False, False, 1),
                    (0, True, False, 10),
                    (0, True, False, 20),
                )
                for digit, helper_installed, has_zero, target in cases:
                    with self.subTest(
                        digit=digit, helper=helper_installed, zero=has_zero,
                        target=target,
                    ):
                        helper = helper_dir / "tmux-window-jump"
                        if helper_installed and not helper.exists():
                            helper.symlink_to(WINDOW_JUMP_BIN)
                        elif not helper_installed and helper.exists():
                            helper.unlink()
                        if zero_present and not has_zero:
                            tmux("kill-window", "-t", "jump-binding:0")
                            zero_present = False
                        # Replay the bytes Ghostty sends for Cmd + digit.
                        os.write(master_fd, b"\x00" + str(digit).encode())
                        current_window = ""
                        for _ in range(50):
                            while True:
                                try:
                                    if not os.read(master_fd, 65536):
                                        break
                                except BlockingIOError:
                                    break
                            current_window = tmux(
                                "display-message",
                                "-p",
                                "-t",
                                "jump-binding",
                                "#{window_index}",
                            ).stdout.strip()
                            if current_window == str(target):
                                break
                            time.sleep(0.02)
                        self.assertEqual(current_window, str(target))
            finally:
                if master_fd is not None:
                    os.close(master_fd)
                if child_pid is not None:
                    try:
                        os.kill(child_pid, signal.SIGHUP)
                    except ProcessLookupError:
                        pass
                    os.waitpid(child_pid, 0)
                tmux("kill-server", check=False)

    def test_prefix_space_cycles_curated_layouts(self):
        socket = f"ws-space-layout-{os.getpid()}-{id(self)}"
        child_pid = None
        master_fd = None
        temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        temp_home = Path(temp_dir.name)
        local_bin = temp_home / ".local" / "bin"
        local_bin.mkdir(parents=True)
        (local_bin / "tmux-layout-cycle").symlink_to(LAYOUT_CYCLE_BIN)

        environment = os.environ.copy()
        environment.pop("TMUX", None)
        environment.pop("TMUX_PANE", None)
        environment["HOME"] = str(temp_home)
        environment["TERM"] = "xterm-256color"

        def tmux(*args: str, check: bool = True):
            return subprocess.run(
                ["tmux", "-L", socket, *args],
                check=check,
                capture_output=True,
                text=True,
                env=environment,
            ).stdout.strip()

        def press_prefix_space(previous_layout: str) -> str:
            os.write(master_fd, b"\x00 ")
            for _ in range(100):
                while True:
                    try:
                        if not os.read(master_fd, 65536):
                            break
                    except BlockingIOError:
                        break
                current = tmux("display-message", "-p", "-t", "layout:0", "#{window_layout}")
                if current != previous_layout:
                    return current
                time.sleep(0.02)
            return tmux("display-message", "-p", "-t", "layout:0", "#{window_layout}")

        try:
            tmux(
                "-f", "/dev/null", "new-session", "-d", "-s", "layout",
                "-x", "158", "-y", "39", "sleep 60",
            )
            tmux("source-file", str(TMUX_WS))
            self.assertEqual(tmux("show-window-options", "-gv", "main-pane-width"), "50%")
            self.assertEqual(tmux("show-window-options", "-gv", "main-pane-height"), "50%")

            child_pid, master_fd = pty.fork()
            if child_pid == 0:
                termios.tcsetwinsize(0, (39, 158))
                os.execvpe(
                    "tmux",
                    ["tmux", "-L", socket, "attach-session", "-t", "layout"],
                    environment,
                )
            os.set_blocking(master_fd, False)
            for _ in range(50):
                if tmux("list-clients"):
                    break
                time.sleep(0.02)

            def layout_kind(layout: str) -> str:
                if layout.startswith("{"):
                    root = json.loads(layout)["L"]
                    if root["t"] == "v":
                        return "tiled"
                    if root["t"] == "h":
                        children = root.get("c", [])
                        if all(child.get("t") == "p" for child in children):
                            return "even-horizontal"
                        if (
                            len(children) == 2
                            and children[0].get("t") == "p"
                            and children[1].get("t") == "v"
                        ):
                            return "main-vertical"
                        if children and children[0].get("t") == "v":
                            return "column-tiled"
                    return f"unknown:{layout}"
                if re.match(r"^[0-9a-f]+,\d+x\d+,0,0\[", layout):
                    return "tiled"
                if re.match(r"^[0-9a-f]+,\d+x\d+,0,0\{\d+x\d+,\d+,\d+,\d+,\d+x\d+,\d+,\d+\[", layout):
                    return "main-vertical"
                if re.match(r"^[0-9a-f]+,\d+x\d+,0,0\{\d+x\d+,\d+,\d+\[", layout):
                    return "column-tiled"
                if re.match(r"^[0-9a-f]+,\d+x\d+,0,0\{", layout):
                    return "even-horizontal"
                return f"unknown:{layout}"

            # 2 panes: toggle cleanly between even-horizontal and tiled.
            tmux("split-window", "-t", "layout:0", "sleep 60")
            tmux("select-layout", "-t", "layout:0", "even-horizontal")
            start_2 = tmux("display-message", "-p", "-t", "layout:0", "#{window_layout}")
            self.assertEqual(layout_kind(start_2), "even-horizontal")
            step1_2 = press_prefix_space(start_2)
            self.assertEqual(layout_kind(step1_2), "tiled")
            step2_2 = press_prefix_space(step1_2)
            self.assertEqual(step2_2, start_2)

            # 3 panes: cycle main-vertical → tiled (2 top, 1 bottom) → even-horizontal.
            tmux("split-window", "-t", "layout:0", "sleep 60")
            tmux("select-layout", "-t", "layout:0", "even-horizontal")
            even_h_3 = tmux("display-message", "-p", "-t", "layout:0", "#{window_layout}")
            self.assertEqual(layout_kind(even_h_3), "even-horizontal")
            main_v_3 = press_prefix_space(even_h_3)
            self.assertEqual(layout_kind(main_v_3), "main-vertical")
            tiled_3 = press_prefix_space(main_v_3)
            self.assertEqual(layout_kind(tiled_3), "tiled")
            back_even_h_3 = press_prefix_space(tiled_3)
            self.assertEqual(back_even_h_3, even_h_3)

            # 4 and 5 panes: cycle main-vertical → column-tiled → even-horizontal.
            for pane_count in (4, 5):
                while int(tmux("display-message", "-p", "-t", "layout:0", "#{window_panes}")) < pane_count:
                    tmux("split-window", "-t", "layout:0", "sleep 60")
                tmux("select-layout", "-t", "layout:0", "even-horizontal")
                even_h = tmux("display-message", "-p", "-t", "layout:0", "#{window_layout}")
                self.assertEqual(layout_kind(even_h), "even-horizontal")
                main_v = press_prefix_space(even_h)
                self.assertEqual(layout_kind(main_v), "main-vertical")
                col_grid = press_prefix_space(main_v)
                self.assertEqual(layout_kind(col_grid), "column-tiled")
                back_even_h = press_prefix_space(col_grid)
                self.assertEqual(back_even_h, even_h)
        finally:
            if master_fd is not None:
                os.close(master_fd)
            if child_pid is not None:
                try:
                    os.kill(child_pid, signal.SIGHUP)
                except ProcessLookupError:
                    pass
                os.waitpid(child_pid, 0)
            tmux("kill-server", check=False)
            temp_dir.cleanup()

    def test_prefix_space_falls_back_without_layout_cycle_helper(self):
        socket = f"ws-space-fallback-{os.getpid()}-{id(self)}"
        child_pid = None
        master_fd = None
        temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        environment = os.environ.copy()
        environment.pop("TMUX", None)
        environment.pop("TMUX_PANE", None)
        environment["HOME"] = temp_dir.name
        environment["TERM"] = "xterm-256color"

        def tmux(*args: str, check: bool = True):
            return subprocess.run(
                ["tmux", "-L", socket, *args],
                check=check,
                capture_output=True,
                text=True,
                env=environment,
            ).stdout.strip()

        def press_prefix_space(previous_layout: str) -> str:
            os.write(master_fd, b"\x00 ")
            for _ in range(100):
                while True:
                    try:
                        if not os.read(master_fd, 65536):
                            break
                    except BlockingIOError:
                        break
                current = tmux("display-message", "-p", "-t", "layout:0", "#{window_layout}")
                if current != previous_layout:
                    return current
                time.sleep(0.02)
            return tmux("display-message", "-p", "-t", "layout:0", "#{window_layout}")

        try:
            tmux(
                "-f", "/dev/null", "new-session", "-d", "-s", "layout",
                "-x", "158", "-y", "39", "sleep 60",
            )
            tmux("source-file", str(TMUX_WS))

            child_pid, master_fd = pty.fork()
            if child_pid == 0:
                termios.tcsetwinsize(0, (39, 158))
                os.execvpe(
                    "tmux",
                    ["tmux", "-L", socket, "attach-session", "-t", "layout"],
                    environment,
                )
            os.set_blocking(master_fd, False)
            for _ in range(50):
                if tmux("list-clients"):
                    break
                time.sleep(0.02)

            for _ in range(3):
                tmux("split-window", "-t", "layout:0", "sleep 60")
            tmux("select-layout", "-t", "layout:0", "even-horizontal")
            even_h = tmux("display-message", "-p", "-t", "layout:0", "#{window_layout}")
            main_v = press_prefix_space(even_h)
            tiled = press_prefix_space(main_v)
            back_even_h = press_prefix_space(tiled)
            self.assertNotEqual(main_v, even_h)
            self.assertNotEqual(tiled, main_v)
            self.assertEqual(back_even_h, even_h)
        finally:
            if master_fd is not None:
                os.close(master_fd)
            if child_pid is not None:
                try:
                    os.kill(child_pid, signal.SIGHUP)
                except ProcessLookupError:
                    pass
                os.waitpid(child_pid, 0)
            tmux("kill-server", check=False)
            temp_dir.cleanup()

    def test_root_meta_shift_bindings_handle_extended_keys(self):
        socket = f"ws-extkeys-{os.getpid()}-{id(self)}"
        child_pid = None
        master_fd = None
        environment = os.environ.copy()
        environment.pop("TMUX", None)
        environment.pop("TMUX_PANE", None)
        environment["TERM"] = "xterm-256color"

        def tmux(*args: str, check: bool = True):
            return subprocess.run(
                ["tmux", "-L", socket, *args],
                check=check,
                capture_output=True,
                text=True,
                env=environment,
            ).stdout.strip()

        def drain() -> None:
            while True:
                try:
                    if not os.read(master_fd, 65536):
                        break
                except BlockingIOError:
                    break

        def wait_for(reader, expected) -> object:
            last = None
            for _ in range(50):
                drain()
                last = reader()
                if last == expected:
                    return last
                time.sleep(0.02)
            return last

        try:
            tmux(
                "-f", "/dev/null", "new-session", "-d", "-s", "ext",
                "-n", "alpha", "-x", "120", "-y", "30", "sleep 60",
            )
            tmux("source-file", str(TMUX_WS))
            tmux("new-window", "-t", "ext:", "-n", "beta", "sleep 60")
            tmux("split-window", "-h", "-t", "ext:beta", "sleep 60")

            child_pid, master_fd = pty.fork()
            if child_pid == 0:
                termios.tcsetwinsize(0, (30, 120))
                os.execvpe(
                    "tmux",
                    ["tmux", "-L", socket, "attach-session", "-t", "ext"],
                    environment,
                )
            os.set_blocking(master_fd, False)
            for _ in range(50):
                if tmux("list-clients"):
                    break
                time.sleep(0.02)

            def window_order() -> list[str]:
                return tmux("list-windows", "-t", "ext", "-F", "#{window_index}:#{window_name}").splitlines()

            self.assertEqual(window_order(), ["0:alpha", "1:beta"])

            # Ghostty under tmux 3.8 extkeys sends Alt+Shift+,/. as CSI 27;4;60~ / 62~.
            os.write(master_fd, b"\x1b[27;4;60~")
            self.assertEqual(wait_for(window_order, ["0:beta", "1:alpha"]), ["0:beta", "1:alpha"])
            os.write(master_fd, b"\x1b[27;4;62~")
            self.assertEqual(wait_for(window_order, ["0:alpha", "1:beta"]), ["0:alpha", "1:beta"])

            # Ghostty under tmux 3.8 extkeys sends Alt+Shift+H/L as CSI 27;4;72~ / 76~.
            def active_width() -> int:
                return int(tmux("display-message", "-p", "-t", "ext:beta", "#{pane_width}"))

            initial_width = active_width()
            os.write(master_fd, b"\x1b[27;4;72~")
            self.assertEqual(wait_for(active_width, initial_width + 2), initial_width + 2)
            os.write(master_fd, b"\x1b[27;4;76~")
            self.assertEqual(wait_for(active_width, initial_width), initial_width)
        finally:
            if master_fd is not None:
                os.close(master_fd)
            if child_pid is not None:
                try:
                    os.kill(child_pid, signal.SIGHUP)
                except ProcessLookupError:
                    pass
                os.waitpid(child_pid, 0)
            tmux("kill-server", check=False)

    def test_prefix_T_toggles_statusline_and_restores_wrapped_height(self):
        socket = f"ws-status-toggle-{os.getpid()}-{id(self)}"
        child_pid = None
        master_fd = None
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_home:
            environment = os.environ.copy()
            environment.pop("TMUX", None)
            environment.pop("TMUX_PANE", None)
            environment["HOME"] = temp_home
            environment["TERM"] = "xterm-256color"

            helper_dir = Path(temp_home) / ".local" / "bin"
            helper_dir.mkdir(parents=True)
            (helper_dir / "tmux-window-wrap").symlink_to(WRAP_BIN)

            def tmux(*args: str, check: bool = True):
                return subprocess.run(
                    ["tmux", "-L", socket, *args],
                    check=check,
                    capture_output=True,
                    text=True,
                    env=environment,
                ).stdout.strip()

            def drain() -> None:
                if master_fd is None:
                    return
                while True:
                    try:
                        if not os.read(master_fd, 65536):
                            break
                    except BlockingIOError:
                        break

            def wait_until(predicate, timeout: float = 5.0) -> bool:
                deadline = time.monotonic() + timeout
                while time.monotonic() < deadline:
                    drain()
                    if predicate():
                        return True
                    time.sleep(0.05)
                drain()
                return bool(predicate())

            def status_of(target: str) -> str:
                return tmux("show-options", "-t", target, "-v", "status")

            def all_rows(target: str) -> str:
                return "\n".join(
                    tmux("show-options", "-t", target, "-v", f"@tmux-window-wrap-row-{i}")
                    for i in range(3)
                )

            try:
                tmux(
                    "-f", "/dev/null", "new-session", "-d", "-s", "wrapped",
                    "-x", "80", "-y", "24", "sleep 60",
                )
                tmux("source-file", str(TMUX_WS))
                tmux("set-option", "-g", "status-right", "")
                tmux("source-file", str(TMUX_WRAP))
                for index in range(25):
                    tmux("new-window", "-d", "-t", "wrapped", "-n", f"win{index:02d}", "sleep 60")
                tmux("select-window", "-t", "wrapped:1")
                tmux("new-session", "-d", "-s", "single", "-n", "solo", "sleep 60")

                child_pid, master_fd = pty.fork()
                if child_pid == 0:
                    termios.tcsetwinsize(0, (24, 80))
                    os.execvpe(
                        "tmux",
                        ["tmux", "-L", socket, "attach-session", "-t", "wrapped"],
                        environment,
                    )
                os.set_blocking(master_fd, False)

                self.assertTrue(wait_until(lambda: bool(tmux("list-clients"))))
                self.assertTrue(wait_until(lambda: status_of("wrapped") == "3"))

                # Replay Ghostty Cmd+B (\x00T) to hide the 3-row statusline.
                os.write(master_fd, b"\x00T")
                self.assertTrue(wait_until(lambda: status_of("wrapped") == "off", timeout=2.0))

                # Renaming while hidden must keep the statusline hidden.
                tmux("rename-window", "-t", "wrapped:1", "hidden_renamed")
                time.sleep(0.3)
                drain()
                self.assertEqual(status_of("wrapped"), "off")

                # Toggle back on: restore 3 rows and refresh cached rows.
                os.write(master_fd, b"\x00T")
                self.assertTrue(wait_until(lambda: status_of("wrapped") == "3", timeout=2.0))
                self.assertTrue(
                    wait_until(lambda: "hidden_renamed" in all_rows("wrapped"), timeout=3.0)
                )

                # Hide, remove overflow windows, then restore and reconcile down to 1 row.
                os.write(master_fd, b"\x00T")
                self.assertTrue(wait_until(lambda: status_of("wrapped") == "off", timeout=2.0))
                for window_id in tmux("list-windows", "-t", "wrapped", "-F", "#{window_id}").splitlines()[1:]:
                    tmux("kill-window", "-t", window_id)
                time.sleep(0.3)
                drain()
                self.assertEqual(status_of("wrapped"), "off")
                os.write(master_fd, b"\x00T")
                self.assertTrue(wait_until(lambda: status_of("wrapped") == "on", timeout=3.0))

                # Single-row session toggles cleanly on → off → on.
                tmux("switch-client", "-t", "single")
                self.assertTrue(wait_until(lambda: status_of("single") == "on", timeout=3.0))
                os.write(master_fd, b"\x00T")
                self.assertTrue(wait_until(lambda: status_of("single") == "off", timeout=2.0))
                os.write(master_fd, b"\x00T")
                self.assertTrue(wait_until(lambda: status_of("single") == "on", timeout=2.0))
            finally:
                if master_fd is not None:
                    os.close(master_fd)
                if child_pid is not None:
                    try:
                        os.kill(child_pid, signal.SIGHUP)
                    except ProcessLookupError:
                        pass
                    os.waitpid(child_pid, 0)
                tmux("kill-server", check=False)

    def test_status_tab_drag_reorders_windows_like_browser_tabs(self):
        socket = f"ws-status-drag-{os.getpid()}-{id(self)}"
        child_pid = None
        master_fd = None
        environment = os.environ.copy()
        environment.pop("TMUX", None)
        environment.pop("TMUX_PANE", None)
        environment["TERM"] = "xterm-256color"

        def tmux(*args: str, check: bool = True):
            return subprocess.run(
                ["tmux", "-L", socket, *args],
                check=check,
                capture_output=True,
                text=True,
                env=environment,
            )

        def drain() -> None:
            if master_fd is not None:
                while True:
                    try:
                        chunk = os.read(master_fd, 65536)
                        if not chunk:
                            break
                    except BlockingIOError:
                        break

        def wait_until(predicate, timeout: float = 3.0) -> bool:
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                drain()
                if predicate():
                    return True
                time.sleep(0.02)
            return predicate()

        def window_order(session: str = "drag") -> list[tuple[int, str]]:
            lines = tmux(
                "list-windows", "-t", session,
                "-F", "#{window_index}\t#{window_name}",
            ).stdout.splitlines()
            return [(int(idx), name) for idx, name in (line.split("\t", 1) for line in lines)]

        def active_name(session: str = "drag") -> str:
            return tmux(
                "display-message", "-p", "-t", session, "#{window_name}"
            ).stdout.strip()

        def global_env(name: str) -> str | None:
            out = tmux("show-environment", "-gh").stdout.splitlines()
            prefix = f"{name}="
            for line in out:
                if line.startswith(prefix):
                    return line[len(prefix):]
            return None

        def send_press(x: int, y: int, button: int = 0) -> None:
            os.write(master_fd, f"\x1b[<{button};{x};{y}M".encode())

        def send_move(x: int, y: int, button: int = 32) -> None:
            os.write(master_fd, f"\x1b[<{button};{x};{y}M".encode())

        def send_release(x: int, y: int, button: int = 0) -> None:
            os.write(master_fd, f"\x1b[<{button};{x};{y}m".encode())

        def send_drag(
            x1: int, y1: int, x2: int, y2: int,
            *, mid=None, pause: float = 0.35, release_button: int = 0,
        ) -> None:
            if pause > 0:
                time.sleep(pause)
            send_press(x1, y1)
            if mid is not None:
                send_move(mid[0], mid[1])
            send_move(x2, y2)
            send_release(x2, y2, button=release_button)

        try:
            tmux(
                "-f", "/dev/null", "new-session", "-d", "-s", "drag",
                "-n", "AAA", "-x", "100", "-y", "30", "sleep 120",
            )
            tmux("source-file", str(TMUX_WS))
            tmux("move-window", "-r", "-t", "drag")
            for name in ("BBB", "CCC", "DDD"):
                tmux("new-window", "-d", "-t", "drag", "-n", name, "sleep 120")
            tmux("select-window", "-t", "drag:1")
            tmux("set-option", "-g", "status-position", "bottom")
            tmux(
                "set-option", "-g", "status-format[0]",
                "#{W:#[range=window|#{window_index}]#{window_index}:#{window_name}#[norange] }",
            )

            child_pid, master_fd = pty.fork()
            if child_pid == 0:
                termios.tcsetwinsize(0, (30, 100))
                os.execvpe(
                    "tmux", ["tmux", "-L", socket, "attach-session", "-t", "drag"],
                    environment,
                )
            os.set_blocking(master_fd, False)
            self.assertTrue(
                wait_until(
                    lambda: bool(tmux("list-clients", "-t", "drag").stdout.strip()),
                    timeout=3.0,
                ),
                "client did not attach",
            )
            client_pid = tmux(
                "list-clients", "-t", "drag", "-F", "#{client_pid}",
            ).stdout.strip().splitlines()[0]
            drag_var = f"AIPANE_STATUS_DRAG_{client_pid}"
            time.sleep(0.15)
            drain()

            # Columns for "1:AAA 2:BBB 3:CCC 4:DDD ":
            # slot1=1..5(x=3), slot2=7..11(x=9), slot3=13..17(x=15), slot4=19..23(x=21), blank=x=40
            # 1. Drag right: 1:AAA → 3:CCC ⇒ [1:BBB, 2:CCC, 3:AAA, 4:DDD], AAA active.
            send_drag(3, 30, 15, 30, mid=(9, 30))
            self.assertTrue(
                wait_until(
                    lambda: window_order() == [(1, "BBB"), (2, "CCC"), (3, "AAA"), (4, "DDD")],
                    timeout=2.0,
                ),
                f"drag right failed, got {window_order()}",
            )
            self.assertEqual(active_name(), "AAA")
            self.assertTrue(wait_until(lambda: global_env(drag_var) is None, timeout=1.0))

            # 2. Drag left: 3:AAA → 1:BBB ⇒ [1:AAA, 2:BBB, 3:CCC, 4:DDD], AAA active.
            send_drag(15, 30, 3, 30, mid=(9, 30))
            self.assertTrue(
                wait_until(
                    lambda: window_order() == [(1, "AAA"), (2, "BBB"), (3, "CCC"), (4, "DDD")],
                    timeout=2.0,
                ),
                f"drag left failed, got {window_order()}",
            )
            self.assertEqual(active_name(), "AAA")
            self.assertTrue(wait_until(lambda: global_env(drag_var) is None, timeout=1.0))

            # 3. Split press / release checks so cancel assertions cannot pass on dropped input.
            for label, drop_x, drop_y, rel_btn, with_motion in (
                ("self", 4, 30, 0, True),
                ("blank", 40, 30, 0, True),
                ("pane", 10, 10, 0, True),
                ("ctrl-release-on-tab", 15, 30, 16, True),
                ("coalesced-blank-up", 40, 30, 0, False),
            ):
                time.sleep(0.35)
                send_press(3, 30)
                self.assertTrue(
                    wait_until(lambda: global_env(drag_var) is not None, timeout=1.5),
                    f"{label}: press did not record {drag_var}",
                )
                if with_motion:
                    send_move(drop_x, drop_y)
                send_release(drop_x, drop_y, button=rel_btn)
                self.assertTrue(
                    wait_until(lambda: global_env(drag_var) is None, timeout=1.5),
                    f"{label}: release did not clear {drag_var}",
                )
                self.assertEqual(
                    window_order(),
                    [(1, "AAA"), (2, "BBB"), (3, "CCC"), (4, "DDD")],
                    f"{label}: unexpectedly changed window order",
                )
                # Follow-up drag starting on blank status space onto tab 3 must never reorder.
                send_drag(40, 30, 15, 30)
                time.sleep(0.1)
                drain()
                self.assertEqual(
                    window_order(),
                    [(1, "AAA"), (2, "BBB"), (3, "CCC"), (4, "DDD")],
                    f"{label}: follow-up blank→tab drag reordered windows",
                )

            # 4. Drag starting inside a pane and released on a status tab does NOT reorder.
            send_drag(10, 10, 15, 30, mid=(10, 20))
            time.sleep(0.15)
            drain()
            self.assertEqual(window_order(), [(1, "AAA"), (2, "BBB"), (3, "CCC"), (4, "DDD")])
            self.assertIsNone(global_env(drag_var))

            # 5. Dragging from a status tab into a copy-mode-vi pane with an active selection
            # must NOT copy the selection, must keep copy-mode open, and must clear drag state.
            tmux("select-window", "-t", "drag:1")
            pane1 = tmux("display-message", "-p", "-t", "drag:1", "#{pane_id}").stdout.strip()
            tmux("delete-buffer", check=False)
            tmux("copy-mode", "-t", pane1)
            tmux("send-keys", "-t", pane1, "-X", "begin-selection")
            tmux("send-keys", "-t", pane1, "-X", "cursor-right")
            time.sleep(0.35)
            send_press(3, 30)
            self.assertTrue(wait_until(lambda: global_env(drag_var) is not None, timeout=1.5))
            send_move(10, 10)
            send_release(10, 10)
            self.assertTrue(wait_until(lambda: global_env(drag_var) is None, timeout=1.5))
            self.assertEqual(
                tmux("display-message", "-p", "-t", pane1, "#{pane_in_mode}").stdout.strip(),
                "1",
                "dragging status tab into copy-mode exited copy-mode",
            )
            self.assertEqual(
                tmux("list-buffers", check=False).stdout.strip(),
                "",
                "dragging status tab into copy-mode copied selection into paste buffer",
            )
            # Follow-up blank→tab drag still does nothing.
            send_drag(40, 30, 15, 30)
            time.sleep(0.1)
            drain()
            self.assertEqual(window_order(), [(1, "AAA"), (2, "BBB"), (3, "CCC"), (4, "DDD")])
            tmux("send-keys", "-t", pane1, "-X", "cancel")

            # 6. Click A then 40ms later drag B→D must NOT zoom any window, and
            # double-clicking a status tab must NOT zoom either (zoom lives on pane borders).
            for idx in (1, 2, 3, 4):
                tmux("split-window", "-d", "-t", f"drag:{idx}", "sleep 120")
            time.sleep(0.35)
            send_press(3, 30)
            send_release(3, 30)
            time.sleep(0.04)
            send_press(9, 30)
            send_move(21, 30)
            send_release(21, 30)
            self.assertTrue(
                wait_until(
                    lambda: window_order() == [(1, "AAA"), (2, "CCC"), (3, "DDD"), (4, "BBB")],
                    timeout=2.0,
                ),
                f"quick click-then-drag failed, got {window_order()}",
            )
            # Wait past tmux's 300ms click timeout; NO window should be zoomed.
            time.sleep(0.45)
            drain()
            zoomed = [
                line for line in tmux(
                    "list-windows", "-t", "drag", "-F", "#{window_name}:#{window_zoomed_flag}"
                ).stdout.splitlines()
                if line.endswith(":1")
            ]
            self.assertEqual(zoomed, [], f"click-then-drag triggered stray zoom: {zoomed}")

            # Restore order AAA BBB CCC DDD, then verify same-tab double-click does NOT zoom.
            send_drag(21, 30, 9, 30)
            self.assertTrue(
                wait_until(
                    lambda: window_order() == [(1, "AAA"), (2, "BBB"), (3, "CCC"), (4, "DDD")],
                    timeout=2.0,
                ),
            )
            time.sleep(0.35)
            send_press(3, 30)
            send_release(3, 30)
            send_press(3, 30)
            send_release(3, 30)
            time.sleep(0.45)
            drain()
            self.assertEqual(
                tmux(
                    "display-message", "-p", "-t", "drag:1", "#{window_zoomed_flag}"
                ).stdout.strip(),
                "0",
                "double-click on status tab unexpectedly zoomed pane",
            )

            # 7. Rapid 3 consecutive drags at ~45ms intervals (hits SecondClick + TripleClick).
            time.sleep(0.35)
            for expected in (
                [(1, "BBB"), (2, "CCC"), (3, "DDD"), (4, "AAA")],
                [(1, "CCC"), (2, "DDD"), (3, "AAA"), (4, "BBB")],
                [(1, "DDD"), (2, "AAA"), (3, "BBB"), (4, "CCC")],
            ):
                send_drag(3, 30, 21, 30, pause=0.045)
                self.assertTrue(
                    wait_until(lambda exp=expected: window_order() == exp, timeout=2.0),
                    f"rapid consecutive drag failed: expected {expected}, got {window_order()}",
                )
            # Restore AAA BBB CCC DDD
            send_drag(3, 30, 21, 30)
            self.assertTrue(
                wait_until(
                    lambda: window_order() == [(1, "AAA"), (2, "BBB"), (3, "CCC"), (4, "DDD")],
                    timeout=2.0,
                ),
            )

            # 8. Same window linked twice in the same session (slots 2 and 5):
            # dragging slot 2 → slot 3 must move slot 2 without miscomputing direction.
            tmux("set-option", "-g", "renumber-windows", "off")
            tmux("link-window", "-s", "drag:=2", "-t", "drag:=5")
            time.sleep(0.1)
            drain()
            # Columns now: 1:AAA(3) 2:BBB(9) 3:CCC(15) 4:DDD(21) 5:BBB(27)
            send_drag(9, 30, 15, 30)
            self.assertTrue(
                wait_until(
                    lambda: window_order() == [(1, "AAA"), (2, "CCC"), (3, "BBB"), (4, "DDD"), (5, "BBB")],
                    timeout=2.0,
                ),
                f"duplicate linked window drag failed, got {window_order()}",
            )
            tmux("unlink-window", "-t", "drag:=5")
            # Restore AAA BBB CCC DDD
            send_drag(15, 30, 9, 30)
            self.assertTrue(
                wait_until(
                    lambda: window_order() == [(1, "AAA"), (2, "BBB"), (3, "CCC"), (4, "DDD")],
                    timeout=2.0,
                ),
            )

            # 9. Non-contiguous indexes with renumber-windows off + grouped session sync.
            tmux("move-window", "-s", "drag:=4", "-t", "drag:=12")
            tmux("move-window", "-s", "drag:=3", "-t", "drag:=8")
            tmux("move-window", "-s", "drag:=2", "-t", "drag:=5")
            tmux("new-session", "-d", "-t", "drag", "-s", "grouped")
            time.sleep(0.1)
            drain()
            # Columns for "1:AAA 5:BBB 8:CCC 12:DDD ":
            # 1:AAA=1..5(3), 5:BBB=7..11(9), 8:CCC=13..17(15), 12:DDD=19..24(21)
            send_drag(3, 30, 15, 30)
            expected_gapped = [(1, "BBB"), (5, "CCC"), (8, "AAA"), (12, "DDD")]
            self.assertTrue(
                wait_until(lambda: window_order("drag") == expected_gapped, timeout=2.0),
                f"gapped drag right failed, got {window_order('drag')}",
            )
            self.assertEqual(
                window_order("grouped"), expected_gapped,
                "grouped session drifted out of sync after drag",
            )
            send_drag(15, 30, 3, 30)
            expected_restored = [(1, "AAA"), (5, "BBB"), (8, "CCC"), (12, "DDD")]
            self.assertTrue(
                wait_until(lambda: window_order("drag") == expected_restored, timeout=2.0),
                f"gapped drag left failed, got {window_order('drag')}",
            )
            self.assertEqual(window_order("grouped"), expected_restored)
        finally:
            if master_fd is not None:
                os.close(master_fd)
            if child_pid is not None:
                try:
                    os.kill(child_pid, signal.SIGHUP)
                except ProcessLookupError:
                    pass
                os.waitpid(child_pid, 0)
            tmux("kill-server", check=False)

    def test_status_tab_drag_isolates_sessions_clients_and_stale_wrap_rows(self):
        socket = f"ws-status-drag-iso-{os.getpid()}-{id(self)}"
        procs: list[tuple[int, int]] = []
        environment = os.environ.copy()
        environment.pop("TMUX", None)
        environment.pop("TMUX_PANE", None)
        environment["TERM"] = "xterm-256color"
        environment["PATH"] = f"{ROOT / 'bin'}{os.pathsep}{environment['PATH']}"

        def tmux(*args: str, check: bool = True):
            return subprocess.run(
                ["tmux", "-L", socket, *args],
                check=check,
                capture_output=True,
                text=True,
                env=environment,
            )

        def drain_all() -> None:
            for _, fd in procs:
                while True:
                    try:
                        chunk = os.read(fd, 65536)
                        if not chunk:
                            break
                    except BlockingIOError:
                        break

        def wait_until(predicate, timeout: float = 3.0) -> bool:
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                drain_all()
                if predicate():
                    return True
                time.sleep(0.02)
            return predicate()

        def attach(session: str, cols: int = 100, rows: int = 30) -> tuple[int, int]:
            pid, fd = pty.fork()
            if pid == 0:
                termios.tcsetwinsize(0, (rows, cols))
                os.execvpe(
                    "tmux", ["tmux", "-L", socket, "attach-session", "-t", session],
                    environment,
                )
            os.set_blocking(fd, False)
            procs.append((pid, fd))
            return pid, fd

        def window_order(session: str) -> list[tuple[int, str]]:
            lines = tmux(
                "list-windows", "-t", session,
                "-F", "#{window_index}\t#{window_name}",
            ).stdout.splitlines()
            return [(int(idx), name) for idx, name in (line.split("\t", 1) for line in lines)]

        def global_env(name: str) -> str | None:
            prefix = f"{name}="
            for line in tmux("show-environment", "-gh").stdout.splitlines():
                if line.startswith(prefix):
                    return line[len(prefix):]
            return None

        try:
            # --- Part A: Cross-session linked window isolation + dual-client isolation ---
            tmux(
                "-f", "/dev/null", "new-session", "-d", "-s", "drag",
                "-n", "AAA", "-x", "100", "-y", "30", "sleep 120",
            )
            tmux("source-file", str(TMUX_WS))
            tmux("move-window", "-r", "-t", "drag")
            for name in ("BBB", "CCC", "DDD"):
                tmux("new-window", "-d", "-t", "drag", "-n", name, "sleep 120")
            tmux("select-window", "-t", "drag:1")
            tmux("set-option", "-g", "status-position", "bottom")
            tmux(
                "set-option", "-g", "status-format[0]",
                "#{W:#[range=window|#{window_index}]#{window_index}:#{window_name}#[norange] }",
            )
            # Second session `other` shares window CCC via link-window.
            tmux("new-session", "-d", "-s", "other", "-n", "OOO", "sleep 120")
            tmux("link-window", "-s", "drag:=3", "-t", "other:")

            _, fd_a = attach("drag")
            _, fd_b = attach("drag")
            _, fd_other = attach("other")
            self.assertTrue(
                wait_until(lambda: len(tmux("list-clients").stdout.splitlines()) == 3, timeout=3.0),
                "all three clients did not attach",
            )
            drag_pids = tmux("list-clients", "-t", "drag", "-F", "#{client_pid}").stdout.splitlines()
            self.assertEqual(len(drag_pids), 2)
            time.sleep(0.15)
            drain_all()

            # Client A presses tab 1 (AAA); Client B clicks a pane in the SAME session;
            # meanwhile the client in `other` sends keystrokes right before Client A drops on CCC.
            os.write(fd_a, b"\x1b[<0;3;30M\x1b[<32;9;30M")
            self.assertTrue(
                wait_until(
                    lambda: any(global_env(f"AIPANE_STATUS_DRAG_{p}") is not None for p in drag_pids),
                    timeout=1.5,
                ),
                "Client A press did not record drag state",
            )
            pid_a = next(p for p in drag_pids if global_env(f"AIPANE_STATUS_DRAG_{p}") is not None)
            os.write(fd_b, b"\x1b[<0;10;10M\x1b[<0;10;10m")
            os.write(fd_other, b"q")
            time.sleep(0.1)
            drain_all()
            self.assertIsNotNone(
                global_env(f"AIPANE_STATUS_DRAG_{pid_a}"),
                "Client B clicking a pane cleared Client A's active drag",
            )
            os.write(fd_a, b"\x1b[<32;15;30M\x1b[<0;15;30m")
            self.assertTrue(
                wait_until(
                    lambda: window_order("drag") == [(1, "BBB"), (2, "CCC"), (3, "AAA"), (4, "DDD")],
                    timeout=2.0,
                ),
                f"concurrent client/session drag failed: drag={window_order('drag')} other={window_order('other')}",
            )
            self.assertEqual(
                window_order("other"), [(1, "OOO"), (2, "CCC")],
                "window leaked across sessions into `other`",
            )
            self.assertTrue(
                wait_until(lambda: global_env(f"AIPANE_STATUS_DRAG_{pid_a}") is None, timeout=1.0),
            )

            # Switching sessions mid-drag cleans up that client's env vars, and detaching does too.
            pid_b = next(p for p in drag_pids if p != pid_a)
            client_b_name = tmux(
                "list-clients", "-t", "drag",
                "-F", "#{?#{==:#{client_pid}," + pid_b + "},#{client_name},}",
            ).stdout.strip()
            os.write(fd_b, b"\x1b[<0;3;30M")
            self.assertTrue(
                wait_until(lambda: global_env(f"AIPANE_STATUS_DRAG_{pid_b}") is not None, timeout=1.5),
            )
            tmux("switch-client", "-c", client_b_name, "-t", "other")
            self.assertTrue(
                wait_until(lambda: global_env(f"AIPANE_STATUS_DRAG_{pid_b}") is None, timeout=2.0),
                "switching client session did not clean up its drag env var",
            )
            tmux("switch-client", "-c", client_b_name, "-t", "drag")
            time.sleep(0.15)
            drain_all()
            os.write(fd_b, b"\x1b[<0;3;30M")
            self.assertTrue(
                wait_until(lambda: global_env(f"AIPANE_STATUS_DRAG_{pid_b}") is not None, timeout=1.5),
            )
            tmux("detach-client", "-t", client_b_name)
            self.assertTrue(
                wait_until(lambda: global_env(f"AIPANE_STATUS_DRAG_{pid_b}") is None, timeout=2.0),
                "detaching client did not clean up its drag env var",
            )

            # --- Part B: Real 3-row tmux-window-wrap stale-screen guard + live cross-row drag ---
            fake_home = tempfile.mkdtemp(prefix="aipane-wrap-drag-")
            w_pid = None
            w_fd = None
            wt = None
            try:
                bin_dir = Path(fake_home) / ".local" / "bin"
                bin_dir.mkdir(parents=True)
                os.symlink(WRAP_BIN, bin_dir / "tmux-window-wrap")
                wrap_env = environment.copy()
                wrap_env["HOME"] = fake_home
                wrap_env["PATH"] = f"{bin_dir}{os.pathsep}{wrap_env['PATH']}"

                def wt(*args: str, check: bool = True):
                    return subprocess.run(
                        ["tmux", "-L", f"{socket}-wrap", *args],
                        check=check,
                        capture_output=True,
                        text=True,
                        env=wrap_env,
                    )

                wt(
                    "-f", "/dev/null", "new-session", "-d", "-s", "w",
                    "-n", "alpha", "-x", "28", "-y", "20", "sleep 120",
                )
                wt("source-file", str(TMUX_WS))
                wt("source-file", str(TMUX_WRAP))
                wt("set-option", "-g", "status-right", "")
                wt("move-window", "-r", "-t", "w")
                for name in ("bravo", "charlie", "delta", "echo", "foxtrot"):
                    wt("new-window", "-d", "-t", "w", "-n", name, "sleep 120")
                wt("select-window", "-t", "w:1")

                w_pid, w_fd = pty.fork()
                if w_pid == 0:
                    termios.tcsetwinsize(0, (20, 28))
                    os.execvpe(
                        "tmux", ["tmux", "-L", f"{socket}-wrap", "attach-session", "-t", "w"],
                        wrap_env,
                    )
                os.set_blocking(w_fd, False)

                def drain_w():
                    while True:
                        try:
                            if not os.read(w_fd, 65536):
                                break
                        except BlockingIOError:
                            break

                def wait_w(pred, timeout=4.0):
                    deadline = time.monotonic() + timeout
                    while time.monotonic() < deadline:
                        drain_w()
                        if pred():
                            return True
                        time.sleep(0.03)
                    return pred()

                def w_order():
                    lines = wt("list-windows", "-t", "w", "-F", "#{window_index}\t#{window_name}").stdout.splitlines()
                    return [(int(i), n) for i, n in (ln.split("\t", 1) for ln in lines)]

                def w_row(n: int) -> str:
                    return wt("show-option", "-t", "w", "-qv", f"@tmux-window-wrap-row-{n}").stdout.strip()

                def label_coords(rows_text: list[str], prefix_width: int = 3) -> dict[int, tuple[int, int]]:
                    coords: dict[int, tuple[int, int]] = {}
                    for r_idx in range(3):
                        expanded = wt(
                            "display-message", "-p", "-t", "w",
                            f"#{{E:@tmux-window-wrap-row-{r_idx}}}",
                        ).stdout.rstrip("\n")
                        col = (prefix_width + 1) if r_idx == 0 else 1
                        pos = 0
                        cur_slot = None
                        while pos < len(expanded):
                            if expanded.startswith("#[", pos):
                                end = expanded.find("]", pos)
                                if end == -1:
                                    break
                                tag = expanded[pos + 2:end]
                                if tag.startswith("range=window|"):
                                    cur_slot = int(tag.split("|", 1)[1])
                                elif tag == "norange":
                                    cur_slot = None
                                pos = end + 1
                                continue
                            if cur_slot is not None and cur_slot not in coords and expanded[pos] != " ":
                                coords[cur_slot] = (col, 18 + r_idx)
                            col += 1
                            pos += 1
                    return coords

                self.assertTrue(
                    wait_w(
                        lambda: wt("show-option", "-t", "w", "-qv", "status").stdout.strip() == "3"
                        and all(f"range=window|{i}]" in (w_row(0) + w_row(1) + w_row(2)) for i in range(1, 7)),
                        timeout=5.0,
                    ),
                    f"tmux-window-wrap did not render all 6 slots across 3 rows: status={wt('show-option', '-t', 'w', '-qv', 'status').stdout!r} rows={[w_row(i) for i in range(3)]!r}",
                )
                # Snapshot the pre-swap rendered rows so we can deterministically exercise
                # the stale-screen window where tmux order changed before wrap repaints.
                stale_rows = [w_row(0), w_row(1), w_row(2)]
                stale_coords = label_coords(stale_rows)
                self.assertIn(2, stale_coords)
                self.assertIn(4, stale_coords)
                self.assertIn(6, stale_coords)

                # Reorder alpha → echo in tmux, then pin the stale rows briefly (and point
                # render key to layout key so the background renderer stays paused for this check).
                wt("swap-window", "-d", "-s", "w:=1", "-t", "w:=2", ";",
                   "swap-window", "-d", "-s", "w:=2", "-t", "w:=3", ";",
                   "swap-window", "-d", "-s", "w:=3", "-t", "w:=4", ";",
                   "swap-window", "-d", "-s", "w:=4", "-t", "w:=5")
                for r_idx, text in enumerate(stale_rows):
                    wt("set-option", "-t", "w", f"@tmux-window-wrap-row-{r_idx}", text)
                cur_key = wt("display-message", "-p", "-t", "w", "#{E:@tmux-window-wrap-layout-key}").stdout.strip()
                wt("set-environment", "-h", "-t", "w", "TMUX_WINDOW_WRAP_RENDER_KEY", cur_key)
                wt("refresh-client", "-S")
                time.sleep(0.1)
                drain_w()
                order_before_stale = w_order()
                active_before_stale = wt("display-message", "-p", "-t", "w", "#{window_name}").stdout.strip()
                self.assertEqual(active_before_stale, "alpha")
                # Slot 2 now holds `charlie`, while the stale row still displays `2:bravo`.
                # Attempting to drag slot 2 → slot 4 must be ignored (neither move nor switch).
                x2, y2 = stale_coords[2]
                x4, y4 = stale_coords[4]
                os.write(
                    w_fd,
                    f"\x1b[<0;{x2};{y2}M\x1b[<32;{x4};{y4}M\x1b[<0;{x4};{y4}m".encode(),
                )
                time.sleep(0.2)
                drain_w()
                self.assertEqual(w_order(), order_before_stale, "stale wrap screen allowed wrong-window drag")
                self.assertEqual(
                    wt("display-message", "-p", "-t", "w", "#{window_name}").stdout.strip(),
                    "alpha",
                    "stale wrap screen switched active window to wrong slot",
                )

                # Now allow wrap to refresh to the real layout and verify live cross-row drag works!
                wt("set-environment", "-h", "-t", "w", "TMUX_WINDOW_WRAP_RENDER_KEY", "stale-trigger")
                wt("refresh-client", "-S")
                self.assertTrue(
                    wait_w(
                        lambda: "#{==:#{window_id}," + wt("display-message", "-p", "-t", "w:=6", "#{window_id}").stdout.strip() + "},#[range=window|6]" in (w_row(0) + w_row(1) + w_row(2))
                        and "#{==:#{window_id}," + wt("display-message", "-p", "-t", "w:=1", "#{window_id}").stdout.strip() + "},#[range=window|1]" in (w_row(0) + w_row(1) + w_row(2)),
                        timeout=4.0,
                    ),
                    "tmux-window-wrap did not refresh after reorder",
                )
                fresh_coords = label_coords([w_row(0), w_row(1), w_row(2)])
                x1, y1 = fresh_coords[1]
                x6, y6 = fresh_coords[6]
                self.assertNotEqual(y1, y6, f"expected slot 1 and slot 6 on different rows: {fresh_coords}")
                time.sleep(0.35)
                os.write(
                    w_fd,
                    f"\x1b[<0;{x6};{y6}M\x1b[<32;{x1};{y1}M\x1b[<0;{x1};{y1}m".encode(),
                )
                self.assertTrue(
                    wait_w(
                        lambda: w_order() == [
                            (1, "foxtrot"), (2, "bravo"), (3, "charlie"),
                            (4, "delta"), (5, "echo"), (6, "alpha"),
                        ],
                        timeout=3.0,
                    ),
                    f"real 3-row wrap cross-row drag failed, got {w_order()}",
                )
            finally:
                if w_fd is not None:
                    os.close(w_fd)
                if w_pid is not None:
                    try:
                        os.kill(w_pid, signal.SIGHUP)
                    except ProcessLookupError:
                        pass
                    os.waitpid(w_pid, 0)
                if wt is not None:
                    wt("kill-server", check=False)
                # Detached run-shell tmux-window-wrap children can still flush state
                # files into fake_home/.local/state/aipane briefly after kill-server.
                cleanup_deadline = time.monotonic() + 2.0
                while Path(fake_home).exists():
                    try:
                        shutil.rmtree(fake_home)
                        break
                    except FileNotFoundError:
                        break
                    except OSError:
                        if time.monotonic() >= cleanup_deadline:
                            shutil.rmtree(fake_home, ignore_errors=True)
                            break
                        time.sleep(0.05)
        finally:
            for pid, fd in procs:
                try:
                    os.close(fd)
                except OSError:
                    pass
                try:
                    os.kill(pid, signal.SIGHUP)
                except ProcessLookupError:
                    pass
                try:
                    os.waitpid(pid, 0)
                except ChildProcessError:
                    pass
            tmux("kill-server", check=False)


class RenameWindowPopupTests(unittest.TestCase):
    def setUp(self):
        self.socket = f"rename-popup-{os.getpid()}-{id(self)}"
        subprocess.run(
            [
                "tmux",
                "-L",
                self.socket,
                "-f",
                "/dev/null",
                "new-session",
                "-d",
                "-s",
                "rename",
                "-n",
                "original",
                "sleep 30",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        self.window_id = self.tmux(
            "display-message", "-p", "-t", "rename:0", "#{window_id}"
        ).stdout.strip()
        socket_path = self.tmux(
            "display-message", "-p", "#{socket_path}"
        ).stdout.strip()
        server_pid = self.tmux(
            "display-message", "-p", "#{pid}"
        ).stdout.strip()
        pane_id = self.tmux(
            "display-message", "-p", "-t", "rename:0", "#{pane_id}"
        ).stdout.strip()
        self.tmux_environment = f"{socket_path},{server_pid},{pane_id.lstrip('%')}"
        self.pane_id = pane_id

    def tearDown(self):
        subprocess.run(
            ["tmux", "-L", self.socket, "kill-server"],
            check=False,
            capture_output=True,
        )

    def tmux(self, *args):
        return subprocess.run(
            ["tmux", "-L", self.socket, *args],
            check=True,
            capture_output=True,
            text=True,
        )

    def run_helper(self, fake_fzf_body, new_name=None, target=None):
        with tempfile.TemporaryDirectory() as temp_dir:
            fake_fzf = Path(temp_dir) / "fzf"
            fake_fzf.write_text(
                "#!/bin/sh\n" + fake_fzf_body,
                encoding="utf-8",
            )
            fake_fzf.chmod(0o755)

            environment = os.environ.copy()
            environment.update(
                {
                    "TMUX": self.tmux_environment,
                    "TMUX_PANE": self.pane_id,
                    "AIPANE_FZF_BIN": str(fake_fzf),
                }
            )
            environment["AIPANE_RENAME_WINDOW_TARGET"] = (
                self.window_id if target is None else target
            )
            if new_name is not None:
                environment["AIPANE_TEST_NEW_NAME"] = new_name

            return subprocess.run(
                [str(RENAME_BIN)],
                check=False,
                capture_output=True,
                text=True,
                env=environment,
            )

    def test_helper_renames_stable_window_id_with_literal_name(self):
        new_name = "  -release $HOME; 'quoted' 中文  "
        result = self.run_helper(
            "printf '%s\\n' \"$AIPANE_TEST_NEW_NAME\"\n",
            new_name=new_name,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        actual_name = self.tmux(
            "display-message", "-p", "-t", self.window_id, "#{window_name}"
        ).stdout.rstrip("\n")
        self.assertEqual(actual_name, new_name)

    def test_helper_uses_query_only_fzf_mode_without_blank_candidate(self):
        new_name = "query-only"
        result = self.run_helper(
            """
byte_count=$(wc -c | tr -d ' ')
[ "$byte_count" = 0 ] || exit 90
case "$*" in
  *enter:accept-or-print-query*) ;;
  *) exit 91 ;;
esac
printf '%s\\n' "$AIPANE_TEST_NEW_NAME"
""",
            new_name=new_name,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        actual_name = self.tmux(
            "display-message", "-p", "-t", self.window_id, "#{window_name}"
        ).stdout.rstrip("\n")
        self.assertEqual(actual_name, new_name)

    def test_helper_layout_uses_short_prompt_and_english_right_aligned_footer(self):
        text = RENAME_BIN.read_text(encoding="utf-8")

        self.assertIn("--prompt='› '", text)
        self.assertIn('--footer="$footer_text"', text)
        self.assertIn("--footer-border=none", text)
        self.assertIn("footer_hint='Enter Save · Esc Cancel'", text)
        self.assertIn("footer_hint_width=23", text)
        self.assertNotIn("--header=", text)
        self.assertNotIn("新名称", text)
        self.assertNotIn("保存", text)
        self.assertNotIn("取消", text)

    def test_helper_rejects_unexpanded_window_id(self):
        result = self.run_helper(
            "exit 99\n",
            target="#{window_id}",
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("invalid window target", result.stderr)

    def test_helper_cancel_keeps_window_name(self):
        result = self.run_helper("exit 130\n")

        self.assertEqual(result.returncode, 0, result.stderr)
        actual_name = self.tmux(
            "display-message", "-p", "-t", self.window_id, "#{window_name}"
        ).stdout.rstrip("\n")
        self.assertEqual(actual_name, "original")


class ColourPalettePopupTests(unittest.TestCase):
    def test_print_mode_groups_ansi_rgb_cube_and_grayscale_colours(self):
        result = subprocess.run(
            [str(COLOUR_PALETTE_BIN), "--print"],
            check=True,
            capture_output=True,
            text=True,
        )

        colour_lines = [
            line
            for line in result.stdout.splitlines()
            if "\x1b[48;5;" in line
        ]
        colour_rows = [
            [int(value) for value in re.findall(r"\x1b\[48;5;(\d+)m", line)]
            for line in colour_lines
        ]

        self.assertIn("ANSI colours (0-15)", result.stdout)
        self.assertIn(
            "RGB cube (16-231): R blocks, G rows, B columns",
            result.stdout,
        )
        self.assertIn("Grayscale (232-255)", result.stdout)
        self.assertEqual(colour_rows[:2], [list(range(8)), list(range(8, 16))])

        expected_cube_rows = []
        for left_red in (0, 2, 4):
            right_red = left_red + 1
            for green in range(6):
                expected_cube_rows.append(
                    [
                        16 + 36 * left_red + 6 * green + blue
                        for blue in range(6)
                    ]
                    + [
                        16 + 36 * right_red + 6 * green + blue
                        for blue in range(6)
                    ]
                )
        self.assertEqual(colour_rows[2:20], expected_cube_rows)
        self.assertEqual(
            colour_rows[20:],
            [list(range(232, 244)), list(range(244, 256))],
        )

        all_colours = [colour for row in colour_rows for colour in row]
        self.assertEqual(len(all_colours), 256)
        self.assertEqual(sorted(all_colours), list(range(256)))

    def test_interactive_mode_starts_at_top_scrolls_and_quits(self):
        for exit_key in ("q", "Escape"):
            socket = f"palette-popup-{os.getpid()}-{id(self)}-{exit_key}"
            session = "palette"
            subprocess.run(
                [
                    "tmux",
                    "-L",
                    socket,
                    "-f",
                    "/dev/null",
                    "new-session",
                    "-d",
                    "-x",
                    "86",
                    "-y",
                    "20",
                    "-s",
                    session,
                    str(COLOUR_PALETTE_BIN),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            try:
                deadline = time.monotonic() + 3
                while True:
                    first_screen = subprocess.run(
                        ["tmux", "-L", socket, "capture-pane", "-p", "-t", session],
                        check=True,
                        capture_output=True,
                        text=True,
                    ).stdout
                    if "tmux 256-colour palette" in first_screen or time.monotonic() >= deadline:
                        break
                    time.sleep(0.02)
                self.assertEqual(
                    first_screen.splitlines()[0],
                    "tmux 256-colour palette",
                )

                subprocess.run(
                    ["tmux", "-L", socket, "send-keys", "-t", session, "Down"],
                    check=True,
                    capture_output=True,
                )
                time.sleep(0.1)
                self.assertEqual(
                    subprocess.run(
                        ["tmux", "-L", socket, "has-session", "-t", session],
                        check=False,
                        capture_output=True,
                    ).returncode,
                    0,
                    "Down must scroll rather than trigger the Esc binding",
                )

                subprocess.run(
                    ["tmux", "-L", socket, "send-keys", "-t", session, exit_key],
                    check=True,
                    capture_output=True,
                )
                time.sleep(0.2)
                self.assertNotEqual(
                    subprocess.run(
                        ["tmux", "-L", socket, "has-session", "-t", session],
                        check=False,
                        capture_output=True,
                    ).returncode,
                    0,
                )
            finally:
                subprocess.run(
                    ["tmux", "-L", socket, "kill-server"],
                    check=False,
                    capture_output=True,
                )

    def test_pane_border_double_click_zoom_and_right_click_menu(self):
        socket = f"ws-border-mouse-{os.getpid()}-{id(self)}"
        environment = {**os.environ, "TERM": "xterm-256color"}
        master_fd = None
        child_pid = None

        def tmux(*args: str, check: bool = True):
            return subprocess.run(
                ["tmux", "-L", socket, *args],
                check=check,
                capture_output=True,
                text=True,
                env=environment,
            )

        def drain() -> None:
            assert master_fd is not None
            while True:
                ready, _, _ = select.select([master_fd], [], [], 0.01)
                if not ready:
                    break
                try:
                    if not os.read(master_fd, 4096):
                        break
                except BlockingIOError:
                    break

        def wait_until(predicate, timeout: float = 3.0) -> bool:
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                drain()
                if predicate():
                    return True
                time.sleep(0.02)
            return predicate()

        def pane_ids() -> list[str]:
            return tmux("list-panes", "-t", "bm:1", "-F", "#{pane_id}").stdout.splitlines()

        def active_pane() -> str:
            return tmux("display-message", "-p", "-t", "bm:1", "#{pane_id}").stdout.strip()

        def is_zoomed() -> bool:
            return tmux("display-message", "-p", "-t", "bm:1", "#{window_zoomed_flag}").stdout.strip() == "1"

        def popup_count(client: str) -> int:
            return sum(
                (": display-popup" in line and "-x C -y C" in line)
                for line in tmux("show-messages", "-t", client).stdout.splitlines()
            )

        with tempfile.TemporaryDirectory() as d1, tempfile.TemporaryDirectory() as d2:
            d2_real = os.path.realpath(d2)
            try:
                tmux(
                    "-f", "/dev/null", "new-session", "-d", "-s", "bm",
                    "-c", d1, "-x", "100", "-y", "30", "sleep 120",
                )
                tmux("source-file", str(TMUX_WS))
                tmux("move-window", "-r", "-t", "bm")
                # Force detector to treat panes as agents so last-pane Close opens confirmation popup.
                fake_agent = Path(d1) / "fake-agent-detector"
                fake_agent.write_text("#!/bin/sh\necho active\n", encoding="utf-8")
                fake_agent.chmod(0o755)
                tmux("set-option", "-g", "@aipane-agent-status-command", str(fake_agent))
                tmux("set-option", "-g", "@aipane-close-window-popup-command", str(CLOSE_POPUP_BIN))

                p1 = active_pane()
                p2 = tmux(
                    "split-window", "-h", "-P", "-F", "#{pane_id}",
                    "-t", p1, "-c", d2, "sleep 120",
                ).stdout.strip()
                tmux("select-pane", "-t", p1)
                p2_left = int(tmux("display-message", "-p", "-t", p2, "#{pane_left}").stdout.strip())
                x_p2 = p2_left + 4

                child_pid, master_fd = pty.fork()
                if child_pid == 0:
                    termios.tcsetwinsize(0, (30, 100))
                    os.execvpe(
                        "tmux", ["tmux", "-L", socket, "attach-session", "-t", "bm"],
                        environment,
                    )
                os.set_blocking(master_fd, False)
                self.assertTrue(
                    wait_until(lambda: bool(tmux("list-clients", "-t", "bm").stdout.strip())),
                    "client did not attach",
                )
                client = tmux("list-clients", "-t", "bm", "-F", "#{client_name}").stdout.strip().splitlines()[0]
                time.sleep(0.15)
                drain()

                # 1. Single-click inactive p2 top bar must NOT steal focus.
                os.write(master_fd, f"\x1b[<0;{x_p2};1M\x1b[<0;{x_p2};1m".encode())
                time.sleep(0.35)
                drain()
                self.assertEqual(active_pane(), p1)
                self.assertFalse(is_zoomed())

                # 2. Double-click inactive p2 top bar activates and zooms p2; double-click again unzooms.
                for _ in range(2):
                    os.write(master_fd, f"\x1b[<0;{x_p2};1M\x1b[<0;{x_p2};1m".encode())
                    time.sleep(0.05)
                self.assertTrue(
                    wait_until(lambda: active_pane() == p2 and is_zoomed()),
                    "double-clicking inactive pane top bar did not activate and zoom it",
                )
                time.sleep(0.35)
                for _ in range(2):
                    os.write(master_fd, b"\x1b[<0;10;1M\x1b[<0;10;1m")
                    time.sleep(0.05)
                self.assertTrue(
                    wait_until(lambda: not is_zoomed()),
                    "double-clicking zoomed pane top bar did not unzoom",
                )

                # 3. Right-click inactive p2 top bar -> 'h' splits p2 in p2's cwd.
                tmux("select-pane", "-t", p1)
                time.sleep(0.35)
                os.write(master_fd, f"\x1b[<2;{x_p2};1M\x1b[<2;{x_p2};1m".encode())
                time.sleep(0.15)
                os.write(master_fd, b"h")
                self.assertTrue(
                    wait_until(lambda: len(pane_ids()) == 3),
                    "border right-click Split Right did not create a third pane",
                )
                p3 = [pid for pid in pane_ids() if pid not in (p1, p2)][0]
                p3_cwd = os.path.realpath(
                    tmux("display-message", "-p", "-t", p3, "#{pane_current_path}").stdout.strip()
                )
                self.assertEqual(p3_cwd, d2_real)

                # 4. Right-click inactive p2 top bar -> 'X' kills p2 immediately (keeps p1 and p3).
                tmux("select-pane", "-t", p1)
                p2_left_now = int(tmux("display-message", "-p", "-t", p2, "#{pane_left}").stdout.strip())
                time.sleep(0.35)
                os.write(master_fd, f"\x1b[<2;{p2_left_now + 4};1M\x1b[<2;{p2_left_now + 4};1m".encode())
                time.sleep(0.15)
                os.write(master_fd, b"X")
                self.assertTrue(
                    wait_until(lambda: p2 not in pane_ids() and p1 in pane_ids()),
                    f"border right-click Close did not kill inactive pane {p2}: {pane_ids()}",
                )

                # 5. Last pane Close via border right-click opens AI-guarded centered close popup.
                tmux("kill-pane", "-t", p3)
                self.assertEqual(pane_ids(), [p1])
                before_popups = popup_count(client)
                time.sleep(0.35)
                os.write(master_fd, b"\x1b[<2;10;1M\x1b[<2;10;1m")
                time.sleep(0.15)
                os.write(master_fd, b"X")
                self.assertTrue(
                    wait_until(lambda: popup_count(client) > before_popups),
                    "last-pane border right-click Close did not open guarded confirmation popup",
                )
                self.assertEqual(pane_ids(), [p1], "guarded last pane was killed without confirmation")
            finally:
                if master_fd is not None:
                    os.close(master_fd)
                if child_pid is not None:
                    try:
                        os.kill(child_pid, signal.SIGHUP)
                    except ProcessLookupError:
                        pass
                    os.waitpid(child_pid, 0)
                subprocess.run(
                    ["tmux", "-L", socket, "kill-server"],
                    check=False,
                    capture_output=True,
                )


class DocsPointerTests(unittest.TestCase):
    def test_docs_exist_and_name_public_confs(self):
        self.assertTrue(CHEATSHEET.is_file())
        self.assertTrue(WS_DOC.is_file())
        self.assertTrue(DOCTOR_BIN.is_file())
        self.assertTrue(os.access(DOCTOR_BIN, os.X_OK))
        doc = WS_DOC.read_text(encoding="utf-8")
        self.assertIn("conf/ghostty-tmux.conf", doc)
        self.assertIn("conf/tmux-workstation.conf", doc)
        self.assertIn("conf/tmux-window-wrap.conf", doc)
        self.assertIn("bin/tmux-rename-window-popup", doc)
        self.assertIn("bin/tmux-colour-palette", doc)
        self.assertIn("bin/aipane-doctor", doc)
        self.assertIn("aipane-doctor", README.read_text(encoding="utf-8"))
        self.assertIn("aipane-doctor", README_CN.read_text(encoding="utf-8"))
        self.assertIn("`Cmd+Opt+P`", doc)

        cheatsheet = CHEATSHEET.read_text(encoding="utf-8")
        self.assertIn("| `Cmd+S` | prefix only |", cheatsheet)
        self.assertIn("| `Cmd+Opt+P` | interactive session & pane picker popup", cheatsheet)
        self.assertIn("| `Cmd+Opt+Y` | copy current pane's AI session ref (`<agent>:<id>`) to clipboard |", cheatsheet)
        self.assertIn("| `Cmd+Opt+Shift+Y` | in-place spatial multi-pane AI session copy pick", cheatsheet)
        self.assertIn("| `Cmd+I` | centered popup rename window |", cheatsheet)
        self.assertIn("| `Cmd+B` | toggle tmux statusline |", cheatsheet)
        self.assertIn("| `prefix T` / `Cmd+B` | toggle current session statusline |", cheatsheet)
        self.assertIn("| double-click pane top bar | zoom / unzoom that pane |", cheatsheet)
        self.assertIn("| right-click pane top bar | pane menu", cheatsheet)
        self.assertNotIn("double-click status window label", cheatsheet)
        self.assertIn("red-background `OFF` badge", cheatsheet)
        self.assertIn(
            "| `prefix P` | indexed colour palette "
            "(`0–255`; `q`/`Esc` closes) |",
            cheatsheet,
        )

    def test_window_jump_install_option_and_repeat_behavior_are_documented(self):
        readme = README.read_text(encoding="utf-8")
        readme_cn = README_CN.read_text(encoding="utf-8")
        workstation = WS_DOC.read_text(encoding="utf-8")
        cheatsheet = CHEATSHEET.read_text(encoding="utf-8")

        for text in (readme, readme_cn, workstation, cheatsheet):
            self.assertIn("tmux-window-jump", text)
            self.assertIn("tmux-layout-cycle", text)
        self.assertIn("@tmux-window-jump-timeout-ms", workstation)
        self.assertIn("0 → 10 → 20", cheatsheet)
        self.assertIn("1 → 11 → 21", cheatsheet)
        self.assertIn("9 → 19 → 29", cheatsheet)


if __name__ == "__main__":
    unittest.main()
