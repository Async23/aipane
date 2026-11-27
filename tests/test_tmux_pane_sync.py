"""Live tmux regression tests for per-pane broadcast & numeric sync-pick."""

from __future__ import annotations

import os
import pty
import signal
import subprocess
import sys
import tempfile
import termios
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LIB_DIR = ROOT / "lib"
if str(LIB_DIR) not in sys.path:
    sys.path.insert(0, str(LIB_DIR))

import recovery  # noqa: E402

TMUX_WS = ROOT / "conf" / "tmux-workstation.conf"
TMUX_WRAP = ROOT / "conf" / "tmux-window-wrap.conf"
PANE_SYNC_BIN = ROOT / "bin" / "tmux-pane-sync"
WINDOW_WRAP_BIN = ROOT / "bin" / "tmux-window-wrap"


class TmuxPaneSyncTests(unittest.TestCase):
    def setUp(self) -> None:
        self.socket = f"pane-sync-{os.getpid()}-{id(self)}-{time.time_ns()}"
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.home = Path(self.temp_dir.name)
        local_bin = self.home / ".local" / "bin"
        local_bin.mkdir(parents=True)
        (local_bin / "tmux-pane-sync").symlink_to(PANE_SYNC_BIN)
        (local_bin / "tmux-window-wrap").symlink_to(WINDOW_WRAP_BIN)

        self.env = os.environ.copy()
        self.env.pop("TMUX", None)
        self.env.pop("TMUX_PANE", None)
        self.env["HOME"] = str(self.home)
        self.env["TERM"] = "xterm-256color"

        self.child_pid: int | None = None
        self.master_fd: int | None = None
        self.output = bytearray()

    def tearDown(self) -> None:
        if self.master_fd is not None:
            try:
                os.close(self.master_fd)
            except OSError:
                pass
        if self.child_pid is not None:
            try:
                os.kill(self.child_pid, signal.SIGHUP)
            except ProcessLookupError:
                pass
            for _ in range(50):
                try:
                    if os.waitpid(self.child_pid, os.WNOHANG)[0]:
                        break
                except ChildProcessError:
                    break
                time.sleep(0.02)
            else:
                try:
                    os.kill(self.child_pid, signal.SIGKILL)
                    os.waitpid(self.child_pid, 0)
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

    def start_session(self, pane_count: int = 4, command: str = "cat") -> tuple[str, list[str]]:
        first = self.tmux(
            "-f", "/dev/null", "new-session", "-d", "-P", "-F", "#{pane_id}",
            "-s", "sync", "-x", "120", "-y", "36", command,
        ).stdout.strip()
        self.tmux("source-file", str(TMUX_WS))
        wid = self.tmux("display-message", "-p", "-t", first, "#{window_id}").stdout.strip()
        for _ in range(pane_count - 1):
            self.tmux(
                "split-window", "-d", "-t", wid, command
            )
            self.tmux("select-layout", "-t", wid, "tiled")
        panes = [
            line.strip()
            for line in self.tmux("list-panes", "-t", wid, "-F", "#{pane_id}").stdout.splitlines()
            if line.strip()
        ]
        self.tmux("select-pane", "-t", panes[0])

        self.child_pid, self.master_fd = pty.fork()
        if self.child_pid == 0:
            termios.tcsetwinsize(0, (36, 120))
            os.execvpe(
                "tmux",
                ["tmux", "-L", self.socket, "attach-session", "-t", "sync"],
                self.env,
            )
        os.set_blocking(self.master_fd, False)
        self.assertTrue(self.wait_until(lambda: bool(self.tmux("list-clients", "-F", "#{client_name}").stdout.strip())))
        return wid, panes

    def drain(self) -> None:
        if self.master_fd is None:
            return
        while True:
            try:
                chunk = os.read(self.master_fd, 65536)
                if not chunk:
                    break
                self.output.extend(chunk)
            except BlockingIOError:
                break
            except OSError:
                break

    def wait_until(self, predicate, timeout: float = 3.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.drain()
            if predicate():
                return True
            time.sleep(0.02)
        self.drain()
        return bool(predicate())

    def send_bytes(self, data: bytes) -> None:
        assert self.master_fd is not None
        os.write(self.master_fd, data)

    def pane_sync_map(self, wid: str) -> dict[str, int]:
        out = self.tmux("list-panes", "-t", wid, "-F", "#{pane_id} #{pane_synchronized}").stdout
        return {line.split()[0]: int(line.split()[1]) for line in out.splitlines() if line.strip()}

    def key_table(self) -> str:
        return self.tmux("list-clients", "-F", "#{client_key_table}").stdout.strip()

    def active_pane(self, wid: str) -> str:
        return self.tmux("display-message", "-p", "-t", wid, "#{pane_id}").stdout.strip()

    def is_zoomed(self, wid: str) -> bool:
        return self.tmux("display-message", "-p", "-t", wid, "#{window_zoomed_flag}").stdout.strip() == "1"

    def window_opt(self, wid: str, name: str) -> str:
        return self.tmux("show-options", "-wqv", "-t", wid, name).stdout.strip()

    def test_two_pane_fast_path_auto_unzooms_and_new_split_stays_unsynced(self) -> None:
        wid, panes = self.start_session(pane_count=2)
        self.tmux("resize-pane", "-Z", "-t", wid)
        self.assertTrue(self.is_zoomed(wid))

        # Cmd+Opt+I (\x00B) on 2 zoomed panes auto-unzooms and syncs both.
        self.send_bytes(b"\x00B")
        self.assertTrue(self.wait_until(lambda: sum(self.pane_sync_map(wid).values()) == 2))
        self.assertFalse(self.is_zoomed(wid))
        self.assertEqual(self.tmux("show-options", "-wqv", "-t", wid, "synchronize-panes").stdout.strip(), "off")
        self.assertEqual(self.window_opt(wid, "@aipane-sync-managed"), "1")
        self.assertEqual(self.window_opt(wid, "@aipane-sync-last"), f"{panes[0]} {panes[1]}")

        # New split must inherit window-level off and stay unsynced.
        third = self.tmux("split-window", "-d", "-P", "-F", "#{pane_id}", "-t", wid, "cat").stdout.strip()
        self.assertEqual(self.pane_sync_map(wid)[third], 0)

        # Pressing Cmd+Opt+I again clears all sync in one shot.
        self.send_bytes(b"\x00B")
        self.assertTrue(self.wait_until(lambda: sum(self.pane_sync_map(wid).values()) == 0))
        self.assertEqual(self.window_opt(wid, "@aipane-sync-managed"), "")

    def test_draft_isolation_commit_focus_shift_and_status_modes(self) -> None:
        wid, panes = self.start_session(pane_count=4)
        # Focus pane 2 (index 2), then press Cmd+Opt+I to enter sync-pick.
        self.tmux("select-pane", "-t", panes[1])
        self.send_bytes(b"\x00B")
        self.assertTrue(self.wait_until(lambda: self.key_table() == "sync-pick"))
        # Starts blank (0 pre-selected) and does not mutate live sync.
        self.assertEqual(self.window_opt(wid, "@aipane-sync-draft-count"), "0")
        self.assertEqual(sum(self.pane_sync_map(wid).values()), 0)

        # Select pane 1 and pane 3 by visible index.
        self.send_bytes(b"1")
        self.assertTrue(self.wait_until(lambda: self.window_opt(wid, "@aipane-sync-draft-count") == "1"))
        self.send_bytes(b"3")
        self.assertTrue(self.wait_until(lambda: self.window_opt(wid, "@aipane-sync-draft-count") == "2"))

        # While picking: live sync is STILL 0 everywhere (draft isolated from live state).
        self.assertEqual(sum(self.pane_sync_map(wid).values()), 0)
        p1_border = self.tmux("display-message", "-p", "-t", panes[0], "#{E:pane-border-format}").stdout
        p2_border = self.tmux("display-message", "-p", "-t", panes[1], "#{E:pane-border-format}").stdout
        status = self.tmux("display-message", "-p", "-t", panes[1], "#{E:@aipane-sync-status}").stdout
        self.assertIn("★ [1 ✓] SELECTED", p1_border)
        self.assertIn("[2]", p2_border)
        self.assertIn("PICK 2/4", status)
        self.assertNotIn("SYNC", status)
        self.assertEqual(self.tmux("display-message", "-p", "-t", panes[1], "#{E:pane-active-border-style}").stdout.strip(), "fg=yellow")
        self.assertEqual(self.tmux("display-message", "-p", "-t", panes[1], "#{E:pane-border-style}").stdout.strip(), "fg=yellow")
        # status-style stays stable so multi-row window labels never wash out.
        self.assertEqual(self.tmux("display-message", "-p", "-t", panes[1], "#{E:status-style}").stdout.strip(), "bg=colour8,fg=colour7")

        # Commit with Enter: enables {1, 3}, moves focus from pane 2 -> pane 1.
        self.send_bytes(b"\r")
        self.assertTrue(self.wait_until(lambda: self.key_table() == "root" and sum(self.pane_sync_map(wid).values()) == 2))
        self.assertEqual(self.pane_sync_map(wid), {panes[0]: 1, panes[1]: 0, panes[2]: 1, panes[3]: 0})
        self.assertEqual(self.active_pane(wid), panes[0])
        self.assertEqual(self.window_opt(wid, "@aipane-sync-draft"), "")
        self.assertEqual(self.window_opt(wid, "@aipane-sync-last"), f"{panes[0]} {panes[2]}")
        self.assertIn("SYNC 2/4", self.tmux("display-message", "-p", "-t", panes[0], "#{E:@aipane-sync-status}").stdout)
        # Running broadcast keeps original blue active border and red ▶ ▶ ▶ markers on synced panes.
        self.assertEqual(self.tmux("display-message", "-p", "-t", panes[0], "#{E:pane-active-border-style}").stdout.strip(), "fg=blue")
        self.assertIn("▶ ▶ ▶", self.tmux("display-message", "-p", "-t", panes[0], "#{E:pane-border-format}").stdout)
        self.assertIn("▶ ▶ ▶", self.tmux("display-message", "-p", "-t", panes[2], "#{E:pane-border-format}").stdout)
        self.assertNotIn("▶ ▶ ▶", self.tmux("display-message", "-p", "-t", panes[1], "#{E:pane-border-format}").stdout)

        # Switching focus to unsynced pane 2 shows SYNC(other) 2/4 and keeps pane 2 freely typeable.
        self.tmux("select-pane", "-t", panes[1])
        self.assertEqual(self.tmux("display-message", "-p", "-t", panes[1], "#{E:pane-active-border-style}").stdout.strip(), "fg=blue")
        self.assertIn("SYNC(other) 2/4", self.tmux("display-message", "-p", "-t", panes[1], "#{E:@aipane-sync-status}").stdout)
        self.assertEqual(self.tmux("display-message", "-p", "-t", panes[1], "#{pane_input_off}").stdout.strip(), "0")

    def test_cancel_keys_zoom_restore_and_prefix_frame_protection(self) -> None:
        wid, panes = self.start_session(pane_count=3)
        self.tmux("select-pane", "-t", panes[1])
        self.tmux("resize-pane", "-Z", "-t", wid)
        self.assertTrue(self.is_zoomed(wid))

        # 1) q cancels (never commits!) and restores zoom.
        self.send_bytes(b"\x00B")
        self.assertTrue(self.wait_until(lambda: self.key_table() == "sync-pick"))
        self.assertFalse(self.is_zoomed(wid))
        self.send_bytes(b"1")
        self.assertTrue(self.wait_until(lambda: self.window_opt(wid, "@aipane-sync-draft-count") == "1"))
        self.send_bytes(b"2")
        self.assertTrue(self.wait_until(lambda: self.window_opt(wid, "@aipane-sync-draft-count") == "2"))
        self.send_bytes(b"q")
        self.assertTrue(self.wait_until(lambda: self.key_table() == "root" and self.is_zoomed(wid)))
        self.assertEqual(sum(self.pane_sync_map(wid).values()), 0)
        self.assertEqual(self.active_pane(wid), panes[1])

        # 2) Unbound key stays in sync-pick without leaking, then re-pressing Cmd+Opt+I (\x00B) cancels cleanly and restores zoom.
        self.send_bytes(b"\x00B")
        self.assertTrue(self.wait_until(lambda: self.key_table() == "sync-pick"))
        self.send_bytes(b"x")
        time.sleep(0.1)
        self.assertEqual(self.key_table(), "sync-pick")
        self.send_bytes(b"\x00B")
        self.assertTrue(self.wait_until(lambda: self.key_table() == "root" and self.is_zoomed(wid)))
        self.assertEqual(self.window_opt(wid, "@aipane-sync-draft"), "")
        for pid in panes:
            captured = self.tmux("capture-pane", "-p", "-t", pid).stdout
            self.assertNotIn("x", captured)
            self.assertNotIn("B", captured)

        # 3) Switching to another window via Ghostty Cmd+<idx> cleans draft & restores origin zoom without leaking the digit.
        other_idx = self.tmux("new-window", "-d", "-P", "-F", "#{window_index}", "-t", "sync", "cat").stdout.strip()
        self.send_bytes(b"\x00B")
        self.assertTrue(self.wait_until(lambda: self.key_table() == "sync-pick"))
        self.send_bytes(f"\x00{other_idx}".encode())
        self.assertTrue(self.wait_until(lambda: self.key_table() == "root" and self.window_opt(wid, "@aipane-sync-draft") == "" and self.is_zoomed(wid)))
        for pid in panes:
            captured = self.tmux("capture-pane", "-p", "-t", pid).stdout
            self.assertNotIn(other_idx, captured)

        # 4) Raw C-Space followed by Esc exits prefix cleanly instead of hanging.
        self.tmux("select-window", "-t", wid)
        self.send_bytes(b"\x00B")
        self.assertTrue(self.wait_until(lambda: self.key_table() == "sync-pick"))
        self.send_bytes(b"\x00")
        self.assertTrue(self.wait_until(lambda: self.key_table() == "prefix"))
        self.send_bytes(b"\x1b")
        self.assertTrue(self.wait_until(lambda: self.key_table() == "root"))

    def test_mid_broadcast_edit_restore_last_and_lt2_commit_clear(self) -> None:
        wid, panes = self.start_session(pane_count=4)
        # First commit {1, 2}.
        self.send_bytes(b"\x00B")
        self.assertTrue(self.wait_until(lambda: self.key_table() == "sync-pick"))
        self.send_bytes(b"1")
        self.assertTrue(self.wait_until(lambda: self.window_opt(wid, "@aipane-sync-draft-count") == "1"))
        self.send_bytes(b"2")
        self.assertTrue(self.wait_until(lambda: self.window_opt(wid, "@aipane-sync-draft-count") == "2"))
        self.send_bytes(b"\r")
        self.assertTrue(self.wait_until(lambda: self.key_table() == "root" and sum(self.pane_sync_map(wid).values()) == 2))

        # Mid-broadcast prefix+e initializes draft with {1, 2}, NOT empty.
        self.send_bytes(b"\x00e")
        self.assertTrue(self.wait_until(lambda: self.key_table() == "sync-pick"))
        self.assertEqual(self.window_opt(wid, "@aipane-sync-draft-count"), "2")
        # Add pane 4 -> commit {1, 2, 4}.
        self.send_bytes(b"4")
        self.assertTrue(self.wait_until(lambda: self.window_opt(wid, "@aipane-sync-draft-count") == "3"))
        self.send_bytes(b"\r")
        self.assertTrue(self.wait_until(lambda: self.key_table() == "root" and sum(self.pane_sync_map(wid).values()) == 3))
        self.assertEqual(self.window_opt(wid, "@aipane-sync-last"), f"{panes[0]} {panes[1]} {panes[3]}")

        # Open prefix+e -> c (clear) -> r (restore last) must restore {1, 2, 4}, then c -> Enter clears to OFF.
        self.send_bytes(b"\x00e")
        self.assertTrue(self.wait_until(lambda: self.key_table() == "sync-pick"))
        self.send_bytes(b"c")
        self.assertTrue(self.wait_until(lambda: self.window_opt(wid, "@aipane-sync-draft-count") == "0"))
        self.send_bytes(b"r")
        self.assertTrue(self.wait_until(lambda: self.window_opt(wid, "@aipane-sync-draft-count") == "3"))
        # Clear + pick only pane 1 + Enter (< 2) -> clears broadcast OFF without overwriting @aipane-sync-last.
        self.send_bytes(b"c")
        self.assertTrue(self.wait_until(lambda: self.window_opt(wid, "@aipane-sync-draft-count") == "0"))
        self.send_bytes(b"1")
        self.assertTrue(self.wait_until(lambda: self.window_opt(wid, "@aipane-sync-draft-count") == "1"))
        self.send_bytes(b"\r")
        self.assertTrue(self.wait_until(lambda: self.key_table() == "root" and sum(self.pane_sync_map(wid).values()) == 0))
        self.assertEqual(self.window_opt(wid, "@aipane-sync-last"), f"{panes[0]} {panes[1]} {panes[3]}")

    def test_runtime_orphan_prune_on_kill_pane_preserves_last_group(self) -> None:
        wid, panes = self.start_session(pane_count=3)
        self.send_bytes(b"\x00B")
        self.assertTrue(self.wait_until(lambda: self.key_table() == "sync-pick"))
        self.send_bytes(b"1")
        self.assertTrue(self.wait_until(lambda: self.window_opt(wid, "@aipane-sync-draft-count") == "1"))
        self.send_bytes(b"2")
        self.assertTrue(self.wait_until(lambda: self.window_opt(wid, "@aipane-sync-draft-count") == "2"))
        self.send_bytes(b"\r")
        self.assertTrue(self.wait_until(lambda: self.key_table() == "root" and sum(self.pane_sync_map(wid).values()) == 2))

        # Kill pane 2 -> surviving pane 1 drops below 2 and must be pruned automatically.
        self.tmux("kill-pane", "-t", panes[1])
        self.assertTrue(
            self.wait_until(lambda: sum(self.pane_sync_map(wid).values()) == 0 and self.window_opt(wid, "@aipane-sync-managed") == ""),
            f"surviving sync map was {self.pane_sync_map(wid)}",
        )
        # History stays intact (not clobbered by a 1-pane ghost).
        self.assertEqual(self.window_opt(wid, "@aipane-sync-last"), f"{panes[0]} {panes[1]}")

        # Next Cmd+Opt+I on remaining 2 panes immediately activates 2-pane fast path (no wasted clear press!).
        self.send_bytes(b"\x00B")
        self.assertTrue(self.wait_until(lambda: sum(self.pane_sync_map(wid).values()) == 2))

    def test_recovery_disables_per_pane_sync_even_when_focused_pane_unsynced(self) -> None:
        wid, panes = self.start_session(pane_count=3)
        self.tmux("set-option", "-p", "-t", panes[0], "synchronize-panes", "on")
        self.tmux("set-option", "-p", "-t", panes[2], "synchronize-panes", "on")
        self.tmux("set-option", "-w", "-t", wid, "@aipane-sync-managed", "1")
        self.tmux("select-pane", "-t", panes[1])

        recovery_env = {**self.env, "AIPANE_TMUX": f"tmux -L {self.socket}"}
        tmux_adapter = recovery.Tmux(environment=recovery_env)
        items = [{"target": panes[1]}]
        detected = tmux_adapter.synchronized_windows(items)
        self.assertEqual(detected[wid], "1")
        disabled = tmux_adapter.disable_synchronized_windows(items)
        self.assertEqual(disabled, [wid])
        self.assertEqual(sum(self.pane_sync_map(wid).values()), 0)
        self.assertEqual(self.window_opt(wid, "@aipane-sync-managed"), "")

    def test_window_wrap_detects_partial_sync_when_active_pane_unsynced(self) -> None:
        wid, panes = self.start_session(pane_count=3)
        self.tmux("set-option", "-p", "-t", panes[0], "synchronize-panes", "on")
        self.tmux("set-option", "-p", "-t", panes[2], "synchronize-panes", "on")
        self.tmux("select-pane", "-t", panes[1])

        # Inspect via tmux-window-wrap WINDOW_FORMAT (active pane is unsynced, but 2 siblings are synced).
        completed = subprocess.run(
            [
                str(WINDOW_WRAP_BIN),
                "render",
                "--line",
                "0",
                "--socket-name",
                self.socket,
                "--session-id",
                "sync",
                "--width",
                "120",
                "--left-width",
                "8",
                "--right-width",
                "20",
            ],
            check=True,
            capture_output=True,
            text=True,
            env=self.env,
        )
        self.assertIn("▶", completed.stdout)

        # Orphan single pane in a 3-pane window must NOT render ▶.
        self.tmux("set-option", "-p", "-u", "-t", panes[2], "synchronize-panes")
        completed_orphan = subprocess.run(
            [
                str(WINDOW_WRAP_BIN),
                "render",
                "--line",
                "0",
                "--socket-name",
                self.socket,
                "--session-id",
                "sync",
                "--width",
                "120",
                "--left-width",
                "8",
                "--right-width",
                "20",
            ],
            check=True,
            capture_output=True,
            text=True,
            env=self.env,
        )
        self.assertNotIn("▶", completed_orphan.stdout)

    def test_sync_badge_off_hides_live_pills_while_keeping_pick_hint(self) -> None:
        wid, panes = self.start_session(pane_count=3)
        # Pre-setting @aipane-sync-badge off must survive re-sourcing tmux-workstation.conf.
        self.tmux("set-option", "-g", "@aipane-sync-badge", "off")
        self.tmux("source-file", str(TMUX_WS))
        self.assertEqual(self.tmux("show-options", "-gqv", "@aipane-sync-badge").stdout.strip(), "off")

        # sync-pick mode still shows the transient PICK hint even when live badge is off.
        self.send_bytes(b"\x00B")
        self.assertTrue(self.wait_until(lambda: self.key_table() == "sync-pick"))
        self.send_bytes(b"1")
        self.assertTrue(self.wait_until(lambda: self.window_opt(wid, "@aipane-sync-draft-count") == "1"))
        self.send_bytes(b"3")
        self.assertTrue(self.wait_until(lambda: self.window_opt(wid, "@aipane-sync-draft-count") == "2"))
        self.assertIn("PICK 2/3", self.tmux("display-message", "-p", "-t", panes[0], "#{E:@aipane-sync-status}").stdout)

        # Commit {1, 3}: live SYNC and SYNC(other) status-right pills are hidden; pane ▶ ▶ ▶ remains.
        self.send_bytes(b"\r")
        self.assertTrue(self.wait_until(lambda: self.key_table() == "root" and sum(self.pane_sync_map(wid).values()) == 2))
        self.assertEqual(self.tmux("display-message", "-p", "-t", panes[0], "#{E:@aipane-sync-status}").stdout.strip(), "")
        self.assertIn("▶ ▶ ▶", self.tmux("display-message", "-p", "-t", panes[0], "#{E:pane-border-format}").stdout)
        self.tmux("select-pane", "-t", panes[1])
        self.assertEqual(self.tmux("display-message", "-p", "-t", panes[1], "#{E:@aipane-sync-status}").stdout.strip(), "")

        # Toggling back to on restores live pills immediately.
        self.tmux("set-option", "-g", "@aipane-sync-badge", "on")
        self.assertIn("SYNC(other) 2/3", self.tmux("display-message", "-p", "-t", panes[1], "#{E:@aipane-sync-status}").stdout)
        self.tmux("select-pane", "-t", panes[0])
        self.assertIn("SYNC 2/3", self.tmux("display-message", "-p", "-t", panes[0], "#{E:@aipane-sync-status}").stdout)


if __name__ == "__main__":
    unittest.main()
