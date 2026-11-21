"""Tests for live pane resolution, Kimi 2.x sessions, yank, and tmux-session-picker."""

from __future__ import annotations

import hashlib
import importlib.machinery
import importlib.util
import json
import os
import pty
import shutil
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
PICKER_BIN = ROOT / "bin" / "tmux-session-picker"
TMUX_WS = ROOT / "conf" / "tmux-workstation.conf"

if str(LIB_DIR) not in sys.path:
    sys.path.insert(0, str(LIB_DIR))

import agent_sessions as sessions_mod  # noqa: E402
import live_sessions as live_mod  # noqa: E402


def _load_picker_module():
    loader = importlib.machinery.SourceFileLoader("tmux_session_picker_mod", str(PICKER_BIN))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def _make_raw_line(
    pane_id: str,
    *,
    session: str = "0",
    window_id: str = "@1",
    window_index: str = "1",
    window_name: str = "work",
    pane_index: str = "1",
    pane_active: str = "0",
    window_active: str = "1",
    cmd: str = "zsh",
    cwd: str = "/tmp/work",
    tty: str = "/dev/ttys001",
    title: str = "",
    sock: str = "/tmp/tmux-test/default",
    srv: str = "9000",
    pane_pid: str = "1001",
    activity: str = "",
    in_mode: str = "0",
) -> str:
    return live_mod.FIELD_SEP.join(
        (
            pane_id,
            session,
            window_id,
            window_index,
            window_name,
            pane_index,
            pane_active,
            window_active,
            cmd,
            cwd,
            tty,
            title,
            sock,
            srv,
            pane_pid,
            activity,
            in_mode,
        )
    )


class LiveSessionsUnitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="aipane-live-test-")
        self.home = Path(self.tmp.name)
        self.state_dir = self.home / ".local/share/aipane"
        self.sock = "/tmp/tmux-unit/default"
        self.srv = "4242"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_resolves_all_seven_agents_and_rejects_plain_node(self) -> None:
        agy_sid = "11111111-1111-4111-8111-111111111111"
        codex_sid = "22222222-2222-7222-8222-222222222222"
        claude_sid = "33333333-3333-4333-8333-333333333333"
        dsh_sid = "dsh-sess-4444"
        grok_sid = "55555555-5555-7555-8555-555555555555"
        kimi_sid = "session_66666666-6666-4666-8666-666666666666"
        pi_sid = "77777777-7777-7777-8777-777777777777"

        # 1. Antigravity runtime file
        agy_key = hashlib.sha256(f"{self.sock}\0{self.srv}\0%1".encode()).hexdigest()
        agy_rt = self.state_dir / "agy/runtime" / f"{agy_key}.json"
        agy_rt.parent.mkdir(parents=True, exist_ok=True)
        agy_rt.write_text(
            json.dumps(
                {
                    "version": 1,
                    "pid": 2001,
                    "tty": "ttys001",
                    "pane": "%1",
                    "socket": self.sock,
                    "server": self.srv,
                    "session_id": agy_sid,
                    "title": "AGY Runtime Title",
                    "cwd": "/tmp/agy",
                }
            ),
            encoding="utf-8",
        )

        # 2. Codex writable rollout
        codex_rollout = (
            self.home / ".codex/sessions/2026/11/21" / f"rollout-2026-11-21T10-00-00-{codex_sid}.jsonl"
        )
        codex_rollout.parent.mkdir(parents=True, exist_ok=True)
        codex_rollout.write_text(
            json.dumps({"type": "session_meta", "payload": {"id": codex_sid, "cwd": "/tmp/codex"}}) + "\n",
            encoding="utf-8",
        )

        # 3. Claude native session file
        claude_sess = self.home / ".claude/sessions/2003.json"
        claude_sess.parent.mkdir(parents=True, exist_ok=True)
        claude_sess.write_text(
            json.dumps(
                {
                    "pid": 2003,
                    "sessionId": claude_sid,
                    "tmux": "0:@1.%3",
                    "cwd": "/tmp/claude",
                    "updatedAt": 1795180632864,
                }
            ),
            encoding="utf-8",
        )

        # 4. dsh-TUI runtime session file
        dsh_sess = self.home / ".dsh/aipane/sessions/2004.json"
        dsh_sess.parent.mkdir(parents=True, exist_ok=True)
        dsh_sess.write_text(
            json.dumps(
                {
                    "version": 1,
                    "pid": 2004,
                    "pane_id": "%4",
                    "socket": self.sock,
                    "server_pid": self.srv,
                    "session_id": dsh_sid,
                    "cwd": "/tmp/dsh",
                    "updated_at": int(time.time() * 1000),
                }
            ),
            encoding="utf-8",
        )

        # 5. Grok active_sessions.json
        grok_active = self.home / ".grok/active_sessions.json"
        grok_active.parent.mkdir(parents=True, exist_ok=True)
        grok_active.write_text(
            json.dumps([{"pid": 2005, "session_id": grok_sid, "cwd": "/tmp/grok"}]),
            encoding="utf-8",
        )

        # 6. Kimi activity record + state.json title
        kimi_sdir = self.home / ".kimi-code/sessions/wd_demo_abc" / kimi_sid
        kimi_sdir.mkdir(parents=True, exist_ok=True)
        (kimi_sdir / "state.json").write_text(
            json.dumps({"id": kimi_sid, "title": "Kimi State Title"}),
            encoding="utf-8",
        )
        (self.home / ".kimi-code/session_index.jsonl").write_text(
            json.dumps({"sessionId": kimi_sid, "sessionDir": str(kimi_sdir)}) + "\n",
            encoding="utf-8",
        )
        kimi_act = json.dumps({"version": 1, "session": {"id": kimi_sid}})

        # 7. Pi binding + stale vite node binding (must NOT misclassify vite as Pi!)
        reg_path = self.state_dir / "registry.jsonl"
        reg_path.parent.mkdir(parents=True, exist_ok=True)
        reg_path.write_text(
            "\n".join(
                [
                    json.dumps({"sock": self.sock, "srv": self.srv, "pane": "%7", "tool": "p", "sid": pi_sid}),
                    json.dumps({"sock": self.sock, "srv": self.srv, "pane": "%8", "tool": "p", "sid": "stale-pi"}),
                ]
            )
            + "\n",
            encoding="utf-8",
        )

        tmux_stdout = "\n".join(
            [
                _make_raw_line("%1", pane_index="1", cmd="agy", tty="/dev/ttys001", title="agy", sock=self.sock, srv=self.srv),
                _make_raw_line("%2", pane_index="2", cmd="codex", tty="/dev/ttys002", title="Codex Chat", sock=self.sock, srv=self.srv),
                _make_raw_line("%3", pane_index="3", cmd="2.1.329", tty="/dev/ttys003", title="✳ Claude Task", sock=self.sock, srv=self.srv),
                _make_raw_line("%4", pane_index="4", cmd="node", tty="/dev/ttys004", title="✦ dsh Chat", sock=self.sock, srv=self.srv),
                _make_raw_line("%5", pane_index="5", cmd="grok", tty="/dev/ttys005", title="Grok Chat", sock=self.sock, srv=self.srv),
                _make_raw_line("%6", pane_index="6", cmd="kimi", tty="/dev/ttys006", title="kimi", sock=self.sock, srv=self.srv, activity=kimi_act),
                _make_raw_line("%7", pane_index="7", cmd="node", tty="/dev/ttys007", title="Pi Chat", sock=self.sock, srv=self.srv),
                _make_raw_line("%8", pane_index="8", cmd="node", tty="/dev/ttys008", title="vite dev", sock=self.sock, srv=self.srv),
                _make_raw_line("%9", pane_index="9", cmd="zsh", tty="/dev/ttys009", title="alfheim@mac", sock=self.sock, srv=self.srv),
            ]
        )

        def fake_tmux(args: list[str]) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(args, 0, stdout=tmux_stdout + "\n", stderr="")

        def fake_pgrep(args: list[str]) -> list[int]:
            name = args[-1]
            if name == "codex":
                return [2002]
            if name == "node":
                return [2007, 2008]
            return []

        def fake_lsof(args: list[str]) -> str:
            pid_arg = args[args.index("-p") + 1]
            out: list[str] = []
            if "2002" in pid_arg:
                out.extend(["p2002", "f0", "au", "n/dev/ttys002", "f9", "aw", f"n{codex_rollout}"])
            if "2005" in pid_arg:
                out.extend(["p2005", "f0", "au", "n/dev/ttys005"])
            if "2007" in pid_arg:
                out.extend(["p2007", "f0", "au", "n/dev/ttys007", "f5", "ar", "n/Users/u/node_modules/@earendil-works/pi-coding-agent/index.js"])
            if "2008" in pid_arg:
                out.extend(["p2008", "f0", "au", "n/dev/ttys008", "f5", "ar", "n/tmp/app/node_modules/vite/bin/vite.js"])
            return "\n".join(out) + "\n"

        env = {
            "HOME": str(self.home),
            "AIPANE_STATE_DIR": str(self.state_dir),
            "TMUX_PANE": "%2",
        }
        panes = live_mod.list_live_panes(
            environment=env,
            home=self.home,
            tmux_runner=fake_tmux,
            pgrep_runner=fake_pgrep,
            lsof_runner=fake_lsof,
            process_exists=lambda pid: pid in {2001, 2002, 2003, 2004, 2005, 2007, 2008},
        )
        by_id = {p.pane_id: p for p in panes}

        self.assertEqual((by_id["%1"].agent, by_id["%1"].session_id, by_id["%1"].title, by_id["%1"].authority),
                         ("antigravity", agy_sid, "AGY Runtime Title", "agy_runtime"))
        self.assertEqual((by_id["%2"].agent, by_id["%2"].session_id, by_id["%2"].is_self, by_id["%2"].authority),
                         ("codex", codex_sid, True, "codex_lsof"))
        self.assertEqual((by_id["%3"].agent, by_id["%3"].session_id, by_id["%3"].authority),
                         ("claude", claude_sid, "claude_sessions"))
        self.assertEqual((by_id["%4"].agent, by_id["%4"].session_id, by_id["%4"].authority),
                         ("dsh", dsh_sid, "dsh_channel"))
        self.assertEqual((by_id["%5"].agent, by_id["%5"].session_id, by_id["%5"].authority),
                         ("grok", grok_sid, "grok_active"))
        self.assertEqual((by_id["%6"].agent, by_id["%6"].session_id, by_id["%6"].title, by_id["%6"].authority),
                         ("kimi", kimi_sid, "Kimi State Title", "kimi_activity"))
        self.assertEqual((by_id["%7"].agent, by_id["%7"].session_id, by_id["%7"].authority),
                         ("pi", pi_sid, "registry"))
        # Plain node (vite) and zsh must NOT be treated as AI panes.
        self.assertEqual(by_id["%8"].agent, "")
        self.assertEqual(by_id["%8"].session_id, "")
        self.assertEqual(by_id["%9"].agent, "")

        # Relative topology addressing from %2 (0:1.2)
        self.assertEqual(live_mod.resolve_pane_spec(".", panes, current_pane_id="%2").pane_id, "%2")
        self.assertEqual(live_mod.resolve_pane_spec("1", panes, current_pane_id="%2").pane_id, "%1")
        self.assertEqual(live_mod.resolve_pane_spec("1.3", panes, current_pane_id="%2").pane_id, "%3")
        self.assertEqual(live_mod.resolve_pane_spec("0:1.4", panes, current_pane_id="%2").pane_id, "%4")
        self.assertEqual(live_mod.resolve_pane_spec("%6", panes, current_pane_id="%2").pane_id, "%6")

    def test_kimi_2x_discovery_preview_timeline_and_ref_prefix(self) -> None:
        raw_uuid = "aa4313fb-986d-4f74-8f8b-c5e14883633d"
        full_sid = f"session_{raw_uuid}"
        sdir = self.home / ".kimi-code/sessions/wd_demo_1234" / full_sid
        main_wire = sdir / "agents/main/wire.jsonl"
        sub_wire = sdir / "agents/sub1/wire.jsonl"
        main_wire.parent.mkdir(parents=True, exist_ok=True)
        sub_wire.parent.mkdir(parents=True, exist_ok=True)
        (sdir / "state.json").write_text(
            json.dumps({"id": full_sid, "title": "重构 Codex HTML 样式"}),
            encoding="utf-8",
        )
        main_wire.write_text(
            "\n".join(
                [
                    json.dumps({"type": "metadata", "protocol_version": "2"}),
                    json.dumps(
                        {
                            "type": "context.append_message",
                            "time": 1794882605000,
                            "message": {
                                "role": "user",
                                "content": [{"type": "text", "text": "重构下这个 HTML"}],
                            },
                        }
                    ),
                    json.dumps(
                        {
                            "type": "context.append_loop_event",
                            "time": 1794882606000,
                            "event": {"type": "tool.call", "name": "ReadFile"},
                        }
                    ),
                    json.dumps(
                        {
                            "type": "context.append_loop_event",
                            "time": 1794882607000,
                            "event": {
                                "type": "content.part",
                                "part": {"type": "text", "text": "已经完成样式重构。"},
                            },
                        }
                    ),
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        sub_wire.write_text(
            json.dumps(
                {
                    "type": "context.append_message",
                    "time": 1794882606500,
                    "message": {"role": "user", "content": [{"type": "text", "text": "子任务检查"}]},
                }
            )
            + "\n",
            encoding="utf-8",
        )

        # Ensure env overrides do not mask home/.kimi-code
        saved = {k: os.environ.pop(k, None) for k in ("KIMI_HOME", "KIMI_CODE_HOME")}
        try:
            listed = sessions_mod.list_sessions(agents=["kimi"], home=self.home)
            self.assertEqual(listed["count"], 1)
            self.assertEqual(listed["sessions"][0]["session_id"], full_sid)
            self.assertEqual(listed["sessions"][0]["preview"], "重构 Codex HTML 样式")

            # Both session_<uuid>, bare <uuid>, and kimi:<sid> work!
            for ref in (full_sid, raw_uuid, f"kimi:{full_sid}", f"kimi:{raw_uuid}"):
                data = sessions_mod.read_session(ref, view="timeline", home=self.home)
                self.assertEqual(len(data["sources"]), 2)
                kinds = [e["kind"] for e in data["sources"][0]["events"]]
                self.assertEqual(kinds, ["user", "tool_call", "assistant"])
                self.assertEqual(data["sources"][0]["events"][2]["text"], "已经完成样式重构。")
        finally:
            for k, v in saved.items():
                if v is not None:
                    os.environ[k] = v

    def test_parse_session_ref_handles_aliases_and_ide_ids(self) -> None:
        self.assertEqual(sessions_mod.parse_session_ref("agy:1111-2222"), ("1111-2222", "antigravity"))
        self.assertEqual(sessions_mod.parse_session_ref("agy:ide/abcd-ef"), ("ide/abcd-ef", "antigravity"))
        self.assertEqual(sessions_mod.parse_session_ref("codex:01a1-b2"), ("01a1-b2", "codex"))
        self.assertEqual(sessions_mod.parse_session_ref("bare-id-123"), ("bare-id-123", None))
        with self.assertRaises(ValueError):
            sessions_mod.parse_session_ref("codex:123", explicit_agent="claude")


class PickerAndTmuxIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.socket = f"aipane-pick-test-{os.getpid()}-{id(self)}"
        self.tmp = tempfile.TemporaryDirectory(prefix="aipane-pick-e2e-")
        self.home = Path(self.tmp.name)
        self.state_dir = self.home / ".local/share/aipane"
        self.env = os.environ.copy()
        self.env.pop("TMUX", None)
        self.env.pop("TMUX_PANE", None)
        self.env.update(
            {
                "HOME": str(self.home),
                "AIPANE_STATE_DIR": str(self.state_dir),
                "AIPANE_TMUX": f"tmux -L {self.socket}",
                "SSH_CONNECTION": "1",  # prevent touching host macOS pbcopy during tests
                "TERM": "xterm-256color",
            }
        )
        self.tmux("-f", "/dev/null", "new-session", "-d", "-s", "work", "-n", "main", "-x", "100", "-y", "30", "cat")
        self.p1 = self.tmux("display-message", "-p", "-t", "work:0", "#{pane_id}").stdout.strip()
        self.w1 = self.tmux("display-message", "-p", "-t", "work:0", "#{window_id}").stdout.strip()
        self.sock_path = self.tmux("display-message", "-p", "#{socket_path}").stdout.strip()
        self.srv_pid = self.tmux("display-message", "-p", "#{pid}").stdout.strip()

    def tearDown(self) -> None:
        subprocess.run(["tmux", "-L", self.socket, "kill-server"], check=False, capture_output=True)
        self.tmp.cleanup()

    def tmux(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["tmux", "-L", self.socket, *args],
            check=check,
            capture_output=True,
            text=True,
            env=self.env,
        )

    def test_picker_formatting_option_b(self) -> None:
        picker = _load_picker_module()
        single = [["live", "%1", "agy", "1111-aaaa", "0:4.1", "display"]]
        multi = [
            ["live", "%1", "agy", "1111-aaaa", "0:4.1", "display1"],
            ["live", "%2", "codex", "2222-bbbb", "0:4.3", "display2"],
            ["disk", "-", "agy", "ide/3333-cccc", "IDE", "display3"],
        ]
        self.assertEqual(picker.format_injection_payload(single), "agy:1111-aaaa")
        multi_out = picker.format_injection_payload(multi)
        self.assertNotIn("\n", multi_out)
        self.assertEqual(
            multi_out,
            "agy:1111-aaaa (0:4.1)  codex:2222-bbbb (0:4.3)  agy:ide/3333-cccc (IDE)",
        )
        self.assertEqual(picker.format_raw_copy_payload(multi), "1111-aaaa\n2222-bbbb\nide/3333-cccc")

    def test_picker_enter_injects_without_clobbering_default_buffer_and_ctrl_y_copies_raw(self) -> None:
        node_bin = shutil.which("node")
        if not node_bin:
            self.skipTest("node is required for live pane integration test")

        # Pre-seed default tmux paste buffer to verify Enter injection leaves it untouched.
        self.tmux("set-buffer", "PRESERVED_CLIPBOARD")

        # Seed a second pane running node + a valid dsh runtime session file.
        p2 = self.tmux(
            "split-window", "-d", "-P", "-F", "#{pane_id}", "-t", self.p1,
            node_bin, "-e", "setTimeout(() => {}, 60000)",
        ).stdout.strip()
        p2_pid = int(self.tmux("display-message", "-p", "-t", p2, "#{pane_pid}").stdout.strip())
        dsh_sid = "dsh-aaaaaaaa-4aaa-8aaa"
        rt = self.home / ".dsh/aipane/sessions" / f"{p2_pid}.json"
        rt.parent.mkdir(parents=True, exist_ok=True)
        rt.write_text(
            json.dumps(
                {
                    "version": 1,
                    "pid": p2_pid,
                    "pane_id": p2,
                    "socket": self.sock_path,
                    "server_pid": self.srv_pid,
                    "session_id": dsh_sid,
                    "cwd": str(self.home),
                    "updated_at": int(time.time() * 1000),
                }
            ),
            encoding="utf-8",
        )

        # Fake fzf #1: Enter selects first streamed candidate (the sibling dsh).
        fake_fzf_enter = self.home / "fzf-enter"
        fake_fzf_enter.write_text("#!/bin/sh\nline=$(head -n 1)\nprintf '\\n%s\\n' \"$line\"\n", encoding="utf-8")
        fake_fzf_enter.chmod(0o755)

        run_env = {
            **self.env,
            "AIPANE_FZF_BIN": str(fake_fzf_enter),
            "AIPANE_PICKER_ORIGIN_PANE": self.p1,
            "AIPANE_PICKER_ORIGIN_WINDOW": self.w1,
            "AIPANE_PICKER_SKIP_DISK": "1",
        }
        res = subprocess.run([str(PICKER_BIN)], env=run_env, capture_output=True, text=True, check=False)
        self.assertEqual(res.returncode, 0, res.stderr)

        # Wait briefly for cat in p1 to echo pasted text.
        deadline = time.monotonic() + 2.0
        captured = ""
        while time.monotonic() < deadline:
            captured = self.tmux("capture-pane", "-p", "-t", self.p1).stdout
            if f"dsh:{dsh_sid}" in captured:
                break
            time.sleep(0.05)
        self.assertIn(f"dsh:{dsh_sid}", captured)
        # Default tmux paste buffer must NOT have been overwritten by Enter injection.
        self.assertEqual(self.tmux("show-buffer").stdout, "PRESERVED_CLIPBOARD")
        # Transient buffer must be cleaned up.
        bufs = self.tmux("list-buffers", "-F", "#{buffer_name}", check=False).stdout
        self.assertNotIn("aipane-pick-", bufs)

        # Fake fzf #2: Ctrl+Y copies raw session ID without '<agent>:' prefix.
        fake_fzf_yank = self.home / "fzf-yank"
        fake_fzf_yank.write_text("#!/bin/sh\nline=$(head -n 1)\nprintf 'ctrl-y\\n%s\\n' \"$line\"\n", encoding="utf-8")
        fake_fzf_yank.chmod(0o755)
        run_env["AIPANE_FZF_BIN"] = str(fake_fzf_yank)
        res2 = subprocess.run([str(PICKER_BIN)], env=run_env, capture_output=True, text=True, check=False)
        self.assertEqual(res2.returncode, 0, res2.stderr)
        self.assertEqual(self.tmux("show-buffer").stdout, dsh_sid)

        # Direct yank on non-AI pane p1 (running cat) fails cleanly and preserves clipboard!
        out_fail, code_fail = sessions_mod.yank_pane_session(self.p1, display=False, home=self.home, environment=self.env)
        self.assertEqual(code_fail, 1)
        self.assertFalse(out_fail["ok"])
        self.assertEqual(self.tmux("show-buffer").stdout, dsh_sid)

    def test_double_click_border_copies_inactive_pane_without_changing_focus(self) -> None:
        node_bin = shutil.which("node")
        if not node_bin:
            self.skipTest("node is required for border double-click integration test")

        self.tmux("source-file", str(TMUX_WS))
        p2 = self.tmux(
            "split-window", "-h", "-P", "-F", "#{pane_id}", "-t", self.p1,
            node_bin, "-e", "setTimeout(() => {}, 60000)",
        ).stdout.strip()
        self.tmux("select-pane", "-t", self.p1)

        p2_pid = int(self.tmux("display-message", "-p", "-t", p2, "#{pane_pid}").stdout.strip())
        dsh_sid = "dsh-bbbbbbbb-4bbb-8bbb"
        rt = self.home / ".dsh/aipane/sessions" / f"{p2_pid}.json"
        rt.parent.mkdir(parents=True, exist_ok=True)
        rt.write_text(
            json.dumps(
                {
                    "version": 1,
                    "pid": p2_pid,
                    "pane_id": p2,
                    "socket": self.sock_path,
                    "server_pid": self.srv_pid,
                    "session_id": dsh_sid,
                    "cwd": str(self.home),
                    "updated_at": int(time.time() * 1000),
                }
            ),
            encoding="utf-8",
        )

        # Point $HOME/.local/bin/aipane-sessions to repo bin for this isolated HOME.
        local_bin = self.home / ".local/bin"
        local_bin.mkdir(parents=True, exist_ok=True)
        (local_bin / "aipane-sessions").symlink_to(ROOT / "bin/aipane-sessions")

        # Propagate test env into tmux server so run-shell hits isolated HOME/socket.
        for k in ("HOME", "AIPANE_STATE_DIR", "AIPANE_TMUX", "SSH_CONNECTION"):
            self.tmux("set-environment", "-g", k, self.env[k])

        # Locate p2's top border x coordinate (pane_left + 4, row 1 in 1-based SGR coords).
        p2_left = int(self.tmux("display-message", "-p", "-t", p2, "#{pane_left}").stdout.strip())
        click_x = p2_left + 4

        child_pid, master_fd = pty.fork()
        if child_pid == 0:
            termios.tcsetwinsize(0, (30, 100))
            os.execvpe("tmux", ["tmux", "-L", self.socket, "attach-session", "-t", "work"], self.env)
        try:
            os.set_blocking(master_fd, False)
            for _ in range(50):
                if self.tmux("list-clients").stdout.strip():
                    break
                time.sleep(0.02)
            for _ in range(2):
                os.write(master_fd, f"\x1b[<0;{click_x};1M\x1b[<0;{click_x};1m".encode())
                time.sleep(0.05)

            deadline = time.monotonic() + 3.0
            copied = ""
            while time.monotonic() < deadline:
                copied = self.tmux("show-buffer", check=False).stdout.strip()
                if copied == dsh_sid:
                    break
                time.sleep(0.05)
            self.assertEqual(copied, dsh_sid)
            # Active pane must stay p1, and neither pane may enter view-mode from run-shell stdout!
            active_now = self.tmux("display-message", "-p", "-t", self.w1, "#{pane_id}").stdout.strip()
            self.assertEqual(active_now, self.p1)
            for pid in (self.p1, p2):
                self.assertEqual(
                    self.tmux("display-message", "-p", "-t", pid, "#{pane_in_mode}").stdout.strip(),
                    "0",
                    f"pane {pid} unexpectedly entered view-mode after yank",
                )

            # Also verify prefix+y on the active AI pane p2 copies cleanly without view-mode.
            self.tmux("select-pane", "-t", p2)
            self.tmux("set-buffer", "STALE_VALUE")
            os.write(master_fd, b"\x00y")
            deadline = time.monotonic() + 3.0
            copied_y = ""
            while time.monotonic() < deadline:
                copied_y = self.tmux("show-buffer", check=False).stdout.strip()
                if copied_y == dsh_sid:
                    break
                time.sleep(0.05)
            self.assertEqual(copied_y, dsh_sid)
            self.assertEqual(
                self.tmux("display-message", "-p", "-t", p2, "#{pane_in_mode}").stdout.strip(),
                "0",
                "prefix+y unexpectedly entered view-mode",
            )
        finally:
            os.close(master_fd)
            try:
                os.kill(child_pid, signal.SIGHUP)
            except ProcessLookupError:
                pass
            os.waitpid(child_pid, 0)


if __name__ == "__main__":
    unittest.main()
