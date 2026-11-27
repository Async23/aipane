"""Live tmux regression tests for in-place spatial multi-pane session copy pick."""

from __future__ import annotations

import json
import os
import pty
import shutil
import signal
import subprocess
import tempfile
import termios
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TMUX_WS = ROOT / "conf" / "tmux-workstation.conf"
SESSION_PICK_BIN = ROOT / "bin" / "tmux-session-pick"
PANE_SYNC_BIN = ROOT / "bin" / "tmux-pane-sync"
SESSIONS_BIN = ROOT / "bin" / "aipane-sessions"
WINDOW_WRAP_BIN = ROOT / "bin" / "tmux-window-wrap"


class TmuxSessionPickTests(unittest.TestCase):
    def setUp(self) -> None:
        self.node_bin = shutil.which("node")
        if not self.node_bin:
            self.skipTest("node is required for live AI pane session-pick tests")

        self.socket = f"session-pick-{os.getpid()}-{id(self)}-{time.time_ns()}"
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.home = Path(self.temp_dir.name)
        local_bin = self.home / ".local" / "bin"
        local_bin.mkdir(parents=True)
        (local_bin / "tmux-session-pick").symlink_to(SESSION_PICK_BIN)
        (local_bin / "tmux-pane-sync").symlink_to(PANE_SYNC_BIN)
        (local_bin / "aipane-sessions").symlink_to(SESSIONS_BIN)
        (local_bin / "tmux-window-wrap").symlink_to(WINDOW_WRAP_BIN)

        self.env = os.environ.copy()
        self.env.pop("TMUX", None)
        self.env.pop("TMUX_PANE", None)
        self.env["HOME"] = str(self.home)
        self.env["TERM"] = "xterm-256color"
        # Prevent tests from clobbering the developer's macOS clipboard (tmux buffer still receives copies).
        self.env["SSH_CONNECTION"] = "127.0.0.1 1 127.0.0.1 22"
        self.env["AIPANE_TMUX"] = f"tmux -L {self.socket}"
        self.env["AIPANE_STATE_DIR"] = str(self.home / ".local/share/aipane")

        self.clients: list[tuple[int, int]] = []
        self.output = bytearray()

    def tearDown(self) -> None:
        for child_pid, master_fd in self.clients:
            try:
                os.close(master_fd)
            except OSError:
                pass
            try:
                os.kill(child_pid, signal.SIGHUP)
            except ProcessLookupError:
                pass
            for _ in range(50):
                try:
                    if os.waitpid(child_pid, os.WNOHANG)[0]:
                        break
                except ChildProcessError:
                    break
                time.sleep(0.02)
            else:
                try:
                    os.kill(child_pid, signal.SIGKILL)
                    os.waitpid(child_pid, 0)
                except OSError:
                    pass
        subprocess.run(
            ["tmux", "-L", self.socket, "kill-server"],
            check=False,
            capture_output=True,
            env=self.env,
        )
        self.temp_dir.cleanup()

    def tmux(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["tmux", "-L", self.socket, *args],
            check=check,
            capture_output=True,
            text=True,
            env=self.env,
        )

    def attach_client(self, session: str = "pick") -> int:
        child_pid, master_fd = pty.fork()
        if child_pid == 0:
            termios.tcsetwinsize(0, (36, 120))
            os.execvpe(
                "tmux",
                ["tmux", "-L", self.socket, "attach-session", "-t", session],
                self.env,
            )
        os.set_blocking(master_fd, False)
        self.clients.append((child_pid, master_fd))
        expected_count = len(self.clients)
        self.assertTrue(
            self.wait_until(
                lambda: len([l for l in self.tmux("list-clients", "-F", "#{client_name}").stdout.splitlines() if l.strip()]) >= expected_count
            )
        )
        return master_fd

    def write_dsh_session(self, pane_id: str, sid: str) -> Path:
        pane_pid = int(self.tmux("display-message", "-p", "-t", pane_id, "#{pane_pid}").stdout.strip())
        sock_path = self.tmux("display-message", "-p", "#{socket_path}").stdout.strip()
        srv_pid = self.tmux("display-message", "-p", "#{pid}").stdout.strip()
        rt = self.home / ".dsh/aipane/sessions" / f"{pane_pid}.json"
        rt.parent.mkdir(parents=True, exist_ok=True)
        rt.write_text(
            json.dumps(
                {
                    "version": 1,
                    "pid": pane_pid,
                    "pane_id": pane_id,
                    "socket": sock_path,
                    "server_pid": srv_pid,
                    "session_id": sid,
                    "cwd": str(self.home),
                    "updated_at": int(time.time() * 1000),
                }
            ),
            encoding="utf-8",
        )
        return rt

    def start_mixed_window(self) -> tuple[str, list[str], dict[str, str], int]:
        """Create 4 panes: #1 dsh, #2 cat (non-AI), #3 dsh (active origin), #4 dsh."""
        assert self.node_bin is not None
        node_cmd = f"{self.node_bin} -e 'setTimeout(() => {{}}, 120000)'"
        first = self.tmux(
            "-f", "/dev/null", "new-session", "-d", "-P", "-F", "#{pane_id}",
            "-s", "pick", "-x", "120", "-y", "36", node_cmd,
        ).stdout.strip()
        self.tmux("source-file", str(TMUX_WS))
        for k in ("HOME", "SSH_CONNECTION", "AIPANE_TMUX", "AIPANE_STATE_DIR"):
            self.tmux("set-environment", "-g", k, self.env[k])
        wid = self.tmux("display-message", "-p", "-t", first, "#{window_id}").stdout.strip()
        p2 = self.tmux("split-window", "-d", "-P", "-F", "#{pane_id}", "-t", first, "cat").stdout.strip()
        p3 = self.tmux("split-window", "-d", "-P", "-F", "#{pane_id}", "-t", p2, node_cmd).stdout.strip()
        p4 = self.tmux("split-window", "-d", "-P", "-F", "#{pane_id}", "-t", p3, node_cmd).stdout.strip()
        self.tmux("select-layout", "-t", wid, "tiled")
        panes = [first, p2, p3, p4]
        sids = {
            first: "dsh-11111111-1111-4111-8111",
            p3: "dsh-33333333-3333-4333-8333",
            p4: "dsh-44444444-4444-4444-8444",
        }
        for pid, sid in sids.items():
            self.write_dsh_session(pid, sid)
        self.tmux("select-pane", "-t", p3)
        fd = self.attach_client("pick")
        return wid, panes, sids, fd

    def drain(self) -> None:
        for _, master_fd in self.clients:
            while True:
                try:
                    chunk = os.read(master_fd, 65536)
                    if not chunk:
                        break
                    self.output.extend(chunk)
                except (BlockingIOError, OSError):
                    break

    def wait_until(self, predicate, timeout: float = 4.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.drain()
            if predicate():
                return True
            time.sleep(0.02)
        self.drain()
        return bool(predicate())

    def send(self, fd: int, data: bytes) -> None:
        os.write(fd, data)

    def key_tables(self) -> list[str]:
        return [
            line.strip()
            for line in self.tmux("list-clients", "-F", "#{client_key_table}").stdout.splitlines()
            if line.strip()
        ]

    def key_table(self) -> str:
        tables = self.key_tables()
        return tables[0] if tables else ""

    def window_opt(self, wid: str, name: str) -> str:
        return self.tmux("show-options", "-wqv", "-t", wid, name).stdout.strip()

    def pane_opt(self, pid: str, name: str) -> str:
        return self.tmux("show-options", "-pqv", "-t", pid, name).stdout.strip()

    def is_zoomed(self, wid: str) -> bool:
        return self.tmux("display-message", "-p", "-t", wid, "#{window_zoomed_flag}").stdout.strip() == "1"

    def active_pane(self, wid: str) -> str:
        return self.tmux("display-message", "-p", "-t", wid, "#{pane_id}").stdout.strip()

    def paste_buffer(self) -> str:
        res = self.tmux("show-buffer", check=False)
        return res.stdout if res.returncode == 0 else ""

    def test_spatial_pick_cyan_badges_non_ai_rejection_others_all_clear_and_commit(self) -> None:
        wid, panes, sids, fd = self.start_mixed_window()
        p1, p2, p3, p4 = panes

        # Enter session-pick via Ghostty Cmd+Opt+Shift+Y (\x00Y).
        self.send(fd, b"\x00Y")
        self.assertTrue(self.wait_until(lambda: self.key_table() == "session-pick" and self.window_opt(wid, "@aipane-session-valid-count") == "3"))
        self.assertEqual(self.window_opt(wid, "@aipane-session-draft-count"), "0")

        # Cyan read-only chrome; sync-picking stays strictly false.
        self.assertEqual(self.tmux("display-message", "-p", "-t", p3, "#{E:@aipane-sync-picking}").stdout.strip(), "0")
        self.assertEqual(self.tmux("display-message", "-p", "-t", p3, "#{E:@aipane-session-picking}").stdout.strip(), "1")
        self.assertEqual(self.tmux("display-message", "-p", "-t", p3, "#{E:pane-border-style}").stdout.strip(), "fg=cyan")
        self.assertEqual(self.tmux("display-message", "-p", "-t", p3, "#{E:pane-active-border-style}").stdout.strip(), "fg=cyan")
        self.assertIn("COPY 0/3", self.tmux("display-message", "-p", "-t", p3, "#{E:@aipane-session-status}").stdout)
        self.assertIn("[1 dsh]", self.tmux("display-message", "-p", "-t", p1, "#{E:pane-border-format}").stdout)
        self.assertIn("[2 —]", self.tmux("display-message", "-p", "-t", p2, "#{E:pane-border-format}").stdout)

        # Pressing non-AI pane 2 must be ignored.
        self.send(fd, b"2")
        time.sleep(0.15)
        self.assertEqual(self.window_opt(wid, "@aipane-session-draft-count"), "0")

        # Toggle pane 1 -> cyan SELECTED COPY banner.
        self.send(fd, b"1")
        self.assertTrue(self.wait_until(lambda: self.window_opt(wid, "@aipane-session-draft-count") == "1"))
        self.assertIn("★ [1 ✓ dsh] COPY", self.tmux("display-message", "-p", "-t", p1, "#{E:pane-border-format}").stdout)

        # 'o' replaces draft with valid siblings {1, 4} (excluding origin p3 and non-AI p2).
        self.send(fd, b"o")
        self.assertTrue(self.wait_until(lambda: self.window_opt(wid, "@aipane-session-draft-count") == "2"))
        draft_o = self.window_opt(wid, "@aipane-session-draft")
        self.assertIn(p1, draft_o)
        self.assertIn(p4, draft_o)
        self.assertNotIn(p3, draft_o)
        self.assertNotIn(p2, draft_o)

        # 'a' selects all 3 valid AI panes; 'c' clears to 0.
        self.send(fd, b"a")
        self.assertTrue(self.wait_until(lambda: self.window_opt(wid, "@aipane-session-draft-count") == "3"))
        self.send(fd, b"c")
        self.assertTrue(self.wait_until(lambda: self.window_opt(wid, "@aipane-session-draft-count") == "0"))

        # Pick 1 and 4 and commit with Enter -> newline-separated refs in buffer + full tag cleanup.
        self.send(fd, b"1")
        self.assertTrue(self.wait_until(lambda: self.window_opt(wid, "@aipane-session-draft-count") == "1"))
        self.send(fd, b"4")
        self.assertTrue(self.wait_until(lambda: self.window_opt(wid, "@aipane-session-draft-count") == "2"))
        self.send(fd, b"\r")
        self.assertTrue(self.wait_until(lambda: self.key_table() == "root" and self.window_opt(wid, "@aipane-session-draft") == ""))
        self.assertEqual(self.paste_buffer(), f"dsh:{sids[p1]}\ndsh:{sids[p4]}")
        for pid in panes:
            self.assertEqual(self.pane_opt(pid, "@aipane-session-tag"), "")

    def test_empty_enter_preserves_clipboard_repress_cancels_and_zoom_restores(self) -> None:
        wid, panes, _, fd = self.start_mixed_window()
        p3 = panes[2]
        self.tmux("set-buffer", "PRESERVED_CLIP")
        self.tmux("resize-pane", "-Z", "-t", wid)
        self.assertTrue(self.is_zoomed(wid))

        # Enter from zoomed state -> auto-unzooms; unbound 'x' does not leak; empty Enter exits without touching clipboard.
        self.send(fd, b"\x00Y")
        self.assertTrue(self.wait_until(lambda: self.key_table() == "session-pick" and self.window_opt(wid, "@aipane-session-valid-count") == "3" and not self.is_zoomed(wid)))
        self.send(fd, b"x")
        time.sleep(0.1)
        self.assertEqual(self.key_table(), "session-pick")
        self.send(fd, b"\r")
        self.assertTrue(self.wait_until(lambda: self.key_table() == "root" and self.is_zoomed(wid)))
        self.assertEqual(self.paste_buffer(), "PRESERVED_CLIP")
        self.assertEqual(self.active_pane(wid), p3)
        for pid in panes:
            self.assertNotIn("x", self.tmux("capture-pane", "-p", "-t", pid).stdout)

        # Re-entering, toggling pane 1, and re-pressing \x00Y cancels cleanly without modifying clipboard.
        self.send(fd, b"\x00Y")
        self.assertTrue(self.wait_until(lambda: self.key_table() == "session-pick" and self.window_opt(wid, "@aipane-session-valid-count") == "3" and not self.is_zoomed(wid)))
        self.send(fd, b"1")
        self.assertTrue(self.wait_until(lambda: self.window_opt(wid, "@aipane-session-draft-count") == "1"))
        self.send(fd, b"\x00Y")
        self.assertTrue(self.wait_until(lambda: self.key_table() == "root" and self.is_zoomed(wid)))
        self.assertEqual(self.paste_buffer(), "PRESERVED_CLIP")
        for pid in panes:
            self.assertEqual(self.pane_opt(pid, "@aipane-session-tag"), "")

    def test_commit_aborts_without_clobbering_clipboard_when_selected_session_changes(self) -> None:
        wid, panes, _, fd = self.start_mixed_window()
        p1 = panes[0]
        self.tmux("set-buffer", "KEEP_ME")

        self.send(fd, b"\x00Y")
        self.assertTrue(self.wait_until(lambda: self.key_table() == "session-pick" and self.window_opt(wid, "@aipane-session-valid-count") == "3"))
        self.send(fd, b"1")
        self.assertTrue(self.wait_until(lambda: self.window_opt(wid, "@aipane-session-draft-count") == "1"))

        # Simulate pane 1 switching to a brand-new session before Enter commit.
        self.write_dsh_session(p1, "dsh-99999999-9999-4999-8999")
        self.send(fd, b"\r")
        self.assertTrue(self.wait_until(lambda: self.key_table() == "root" and self.window_opt(wid, "@aipane-session-draft") == ""))
        self.assertEqual(self.paste_buffer(), "KEEP_ME")
        for pid in panes:
            self.assertEqual(self.pane_opt(pid, "@aipane-session-tag"), "")

    def test_live_broadcast_preserved_mouse_click_toggles_and_double_click_blocked(self) -> None:
        wid, panes, _, fd = self.start_mixed_window()
        p1, _, p3, p4 = panes
        self.tmux("set-buffer", "UNTOUCHED")

        # Enable managed broadcast on p1 and p4 first.
        self.tmux("set-option", "-w", "-t", wid, "synchronize-panes", "off")
        self.tmux("set-option", "-p", "-t", p1, "synchronize-panes", "on")
        self.tmux("set-option", "-p", "-t", p4, "synchronize-panes", "on")
        self.tmux("set-option", "-w", "-t", wid, "@aipane-sync-managed", "1")

        self.send(fd, b"\x00Y")
        self.assertTrue(self.wait_until(lambda: self.key_table() == "session-pick" and self.window_opt(wid, "@aipane-session-valid-count") == "3"))
        # Status keeps BOTH cyan COPY and yellow SYNC visible simultaneously.
        self.assertIn("COPY 0/3", self.tmux("display-message", "-p", "-t", p3, "#{E:@aipane-session-status}").stdout)
        self.assertIn("SYNC", self.tmux("display-message", "-p", "-t", p3, "#{E:@aipane-sync-status}").stdout)

        # Mouse click inside pane 1 toggles pane 1 in draft.
        p1_left = int(self.tmux("display-message", "-p", "-t", p1, "#{pane_left}").stdout.strip())
        p1_top = int(self.tmux("display-message", "-p", "-t", p1, "#{pane_top}").stdout.strip())
        # SGR 1-based coordinates: pane top bar is row (p1_top + 1), interior starts at (p1_top + 2).
        self.send(fd, f"\x1b[<0;{p1_left + 3};{p1_top + 2}M\x1b[<0;{p1_left + 3};{p1_top + 2}m".encode())
        self.assertTrue(self.wait_until(lambda: self.window_opt(wid, "@aipane-session-draft-count") == "1"))

        # Double-clicking or right-clicking p4's top border inside session-pick must NOT
        # fall through to root's zoom or border menu!
        p4_left = int(self.tmux("display-message", "-p", "-t", p4, "#{pane_left}").stdout.strip())
        p4_top = int(self.tmux("display-message", "-p", "-t", p4, "#{pane_top}").stdout.strip())
        for _ in range(2):
            self.send(fd, f"\x1b[<0;{p4_left + 4};{p4_top + 1}M\x1b[<0;{p4_left + 4};{p4_top + 1}m".encode())
            time.sleep(0.04)
        time.sleep(0.4)
        self.assertFalse(self.is_zoomed(wid))
        self.assertEqual(self.key_table(), "session-pick")
        self.send(fd, f"\x1b[<2;{p4_left + 4};{p4_top + 1}M\x1b[<2;{p4_left + 4};{p4_top + 1}m".encode())
        time.sleep(0.15)
        self.assertEqual(self.key_table(), "session-pick")
        self.assertEqual(self.paste_buffer(), "UNTOUCHED")

        # Cancel with q -> live synchronize-panes broadcast remains untouched.
        self.send(fd, b"q")
        self.assertTrue(self.wait_until(lambda: self.key_table() == "root"))
        sync_out = self.tmux("list-panes", "-t", wid, "-F", "#{pane_id} #{pane_synchronized}").stdout
        sync_map = {line.split()[0]: int(line.split()[1]) for line in sync_out.splitlines() if line.strip()}
        self.assertEqual(sync_map[p1], 1)
        self.assertEqual(sync_map[p4], 1)

    def test_single_owner_split_no_rezoom_kill_cleanup_and_non_ai_window(self) -> None:
        wid, panes, _, fd1 = self.start_mixed_window()
        fd2 = self.attach_client("pick")

        # Client 1 enters session-pick; Client 2 trying to enter the same window is rejected without disturbing Client 1.
        self.send(fd1, b"\x00Y")
        self.assertTrue(self.wait_until(lambda: "session-pick" in self.key_tables() and self.window_opt(wid, "@aipane-session-valid-count") == "3"))
        self.send(fd1, b"1")
        self.assertTrue(self.wait_until(lambda: self.window_opt(wid, "@aipane-session-draft-count") == "1"))

        self.send(fd2, b"\x00Y")
        time.sleep(0.2)
        tables = self.key_tables()
        self.assertEqual(tables.count("session-pick"), 1)
        self.assertEqual(tables.count("root"), 1)
        self.assertEqual(self.window_opt(wid, "@aipane-session-draft-count"), "1")
        self.assertEqual(self.pane_opt(panes[0], "@aipane-session-tag"), "dsh")

        # Cancel client 1, zoom, enter again, then split-window -> must clean up WITHOUT re-zooming over the new split.
        self.send(fd1, b"q")
        self.assertTrue(self.wait_until(lambda: all(t == "root" for t in self.key_tables())))
        self.tmux("resize-pane", "-Z", "-t", wid)
        self.assertTrue(self.is_zoomed(wid))
        self.send(fd1, b"\x00Y")
        self.assertTrue(self.wait_until(lambda: "session-pick" in self.key_tables() and self.window_opt(wid, "@aipane-session-valid-count") == "3" and not self.is_zoomed(wid)))
        self.send(fd1, b"\x00|")
        self.assertTrue(
            self.wait_until(
                lambda: all(t == "root" for t in self.key_tables()) and self.window_opt(wid, "@aipane-session-draft") == ""
            )
        )
        self.assertFalse(self.is_zoomed(wid))

        # Kill a pane mid-pick -> pane-exited / after-kill-pane cleans up draft and returns client to root.
        self.send(fd1, b"\x00Y")
        self.assertTrue(self.wait_until(lambda: "session-pick" in self.key_tables() and self.window_opt(wid, "@aipane-session-valid-count") == "3"))
        self.tmux("kill-pane", "-t", panes[1])
        self.assertTrue(
            self.wait_until(
                lambda: all(t == "root" for t in self.key_tables()) and self.window_opt(wid, "@aipane-session-draft") == ""
            )
        )

        # Window with zero AI panes exits immediately back to root.
        shell_win = self.tmux("new-window", "-P", "-F", "#{window_id}", "-t", "pick", "cat").stdout.strip()
        self.send(fd1, b"\x00Y")
        time.sleep(0.2)
        self.assertTrue(all(t == "root" for t in self.key_tables()))
        self.assertEqual(self.window_opt(shell_win, "@aipane-session-draft"), "")

    def test_format_toggle_magenta_raw_status_off_space_origin_and_stale_reset(self) -> None:
        wid, panes, sids, fd = self.start_mixed_window()
        p1, p2, p3, _ = panes

        # Simulate stale leftover raw option from a prior crashed pick + hidden status bar (Cmd+B).
        self.tmux("set-option", "-w", "-t", wid, "@aipane-session-fmt", "raw")
        self.tmux("set-option", "-g", "status", "off")

        # Entry must unconditionally reset format back to ref (cyan) and mark origin pane with ●.
        self.send(fd, b"\x00Y")
        self.assertTrue(self.wait_until(lambda: self.key_table() == "session-pick" and self.window_opt(wid, "@aipane-session-valid-count") == "3"))
        self.assertEqual(self.window_opt(wid, "@aipane-session-fmt"), "ref")
        self.assertEqual(self.tmux("display-message", "-p", "-t", p3, "#{E:pane-border-style}").stdout.strip(), "fg=cyan")
        self.assertIn("● [3 dsh]", self.tmux("display-message", "-p", "-t", p3, "#{E:pane-border-format}").stdout)

        # With 0 panes selected and status off, pressing 'f' switches borders to magenta and prefixes RAW on top bars.
        self.send(fd, b"f")
        self.assertTrue(self.wait_until(lambda: self.window_opt(wid, "@aipane-session-fmt") == "raw"))
        self.assertEqual(self.key_table(), "session-pick")
        self.assertEqual(self.tmux("display-message", "-p", "-t", p3, "#{E:pane-border-style}").stdout.strip(), "fg=magenta")
        self.assertEqual(self.tmux("display-message", "-p", "-t", p3, "#{E:pane-active-border-style}").stdout.strip(), "fg=magenta")
        self.assertIn("RAW [1 dsh]", self.tmux("display-message", "-p", "-t", p1, "#{E:pane-border-format}").stdout)
        self.assertIn("● RAW [3 dsh]", self.tmux("display-message", "-p", "-t", p3, "#{E:pane-border-format}").stdout)
        self.assertIn("COPY RAW 0/3", self.tmux("display-message", "-p", "-t", p3, "#{E:@aipane-session-status}").stdout)

        # Pressing 'f' twice keeps key-table armed and toggles cleanly back to ref (cyan).
        self.send(fd, b"f")
        self.assertTrue(self.wait_until(lambda: self.window_opt(wid, "@aipane-session-fmt") == "ref"))
        self.assertEqual(self.key_table(), "session-pick")
        self.assertEqual(self.tmux("display-message", "-p", "-t", p3, "#{E:pane-border-style}").stdout.strip(), "fg=cyan")

        # Toggle back to raw, select origin pane with Space + pane 1, commit -> bare sids without agent prefix.
        self.send(fd, b"f")
        self.assertTrue(self.wait_until(lambda: self.window_opt(wid, "@aipane-session-fmt") == "raw"))
        self.send(fd, b" ")
        self.assertTrue(self.wait_until(lambda: self.window_opt(wid, "@aipane-session-draft-count") == "1"))
        self.assertIn("★ RAW [3 ✓ dsh]", self.tmux("display-message", "-p", "-t", p3, "#{E:pane-border-format}").stdout)
        self.send(fd, b"1")
        self.assertTrue(self.wait_until(lambda: self.window_opt(wid, "@aipane-session-draft-count") == "2"))
        self.send(fd, b"\r")
        self.assertTrue(self.wait_until(lambda: self.key_table() == "root" and self.window_opt(wid, "@aipane-session-draft") == ""))
        self.assertEqual(self.paste_buffer(), f"{sids[p1]}\n{sids[p3]}")
        self.assertEqual(self.window_opt(wid, "@aipane-session-fmt"), "")
        self.assertEqual(self.window_opt(wid, "@aipane-session-origin"), "")

        # Next entry resets automatically to ref; focusing non-AI pane p2 shows ● RAW on f and rejects Space.
        self.tmux("select-pane", "-t", p2)
        self.send(fd, b"\x00Y")
        self.assertTrue(self.wait_until(lambda: self.key_table() == "session-pick" and self.window_opt(wid, "@aipane-session-fmt") == "ref"))
        self.assertIn("● [2 —]", self.tmux("display-message", "-p", "-t", p2, "#{E:pane-border-format}").stdout)
        self.send(fd, b"f")
        self.assertTrue(self.wait_until(lambda: self.window_opt(wid, "@aipane-session-fmt") == "raw"))
        self.assertIn("● RAW [2 —]", self.tmux("display-message", "-p", "-t", p2, "#{E:pane-border-format}").stdout)
        self.send(fd, b" ")
        time.sleep(0.15)
        self.assertEqual(self.window_opt(wid, "@aipane-session-draft-count"), "0")
        self.send(fd, b"q")
        self.assertTrue(self.wait_until(lambda: self.key_table() == "root"))


if __name__ == "__main__":
    unittest.main()
