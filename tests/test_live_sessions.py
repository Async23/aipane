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
        self.assertEqual(by_id["%2"].active_path, str(codex_rollout))
        self.assertEqual(by_id["%2"].as_dict()["active_path"], str(codex_rollout))
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

    def test_codex_pane_propagates_custom_codex_home_and_detects_future_threads_end_to_end(self) -> None:
        from unittest import mock

        sid = "a4a4a4a4-a4a4-74a4-84a4-a4a4a4a4a4a4"
        r2 = "b5b5b5b5-b5b5-75b5-85b5-b5b5b5b5b5b5"

        # Default CODEX_HOME A has a different conversation using the same SID
        default_home = self.home / ".codex"
        default_seg = default_home / "sessions/2026/11/10" / f"rollout-2026-11-10T10-00-00-{sid}.jsonl"
        default_seg.parent.mkdir(parents=True, exist_ok=True)
        default_seg.write_text(
            "\n".join(
                [
                    json.dumps({"ordinal": 0, "type": "session_meta", "timestamp": "2026-11-10T02:00:00Z", "payload": {"id": sid, "cwd": "/tmp/a"}}),
                    json.dumps({"ordinal": 1, "type": "response_item", "timestamp": "2026-11-10T02:00:01Z", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "WRONG_HOME_A_OPENING"}]}}),
                ]
            )
            + "\n",
            encoding="utf-8",
        )

        # Custom CODEX_HOME B holds the real pane's 2 segments
        custom_home = self.home / "custom-codex-b"
        b_seg1 = custom_home / "sessions/2026/11/15" / f"rollout-2026-11-15T10-00-00-{sid}.jsonl"
        b_seg2 = custom_home / "sessions/2026/11/23" / f"rollout-2026-11-23T10-00-00-{sid}_{r2}.jsonl"
        b_seg1.parent.mkdir(parents=True, exist_ok=True)
        b_seg2.parent.mkdir(parents=True, exist_ok=True)
        b_seg1.write_text(
            "\n".join(
                [
                    json.dumps({"ordinal": 0, "type": "session_meta", "timestamp": "2026-11-15T02:00:00Z", "payload": {"id": sid, "cwd": "/tmp/b"}}),
                    json.dumps({"ordinal": 1, "type": "response_item", "timestamp": "2026-11-15T02:00:01Z", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "CUSTOM_HOME_B_OPENING"}]}}),
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        b_seg2.write_text(
            "\n".join(
                [
                    json.dumps({"ordinal": 2, "type": "session_meta", "timestamp": "2026-11-23T02:00:00Z", "payload": {"id": sid, "cwd": "/tmp/b", "history_base": {"thread_id": sid, "end_ordinal_exclusive": 2}}}),
                    json.dumps({"ordinal": 3, "type": "response_item", "timestamp": "2026-11-23T02:00:01Z", "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "CUSTOM_HOME_B_TAIL"}]}}),
                ]
            )
            + "\n",
            encoding="utf-8",
        )

        # Second pane writes to an unrecognized future_threads/ directory under default_home
        future_sid = "c6c6c6c6-c6c6-76c6-86c6-c6c6c6c6c6c6"
        future_file = default_home / "future_threads/2026" / f"rollout-2026-11-23T11-00-00-{future_sid}.jsonl"
        future_file.parent.mkdir(parents=True, exist_ok=True)
        future_file.write_text(
            "\n".join(
                [
                    json.dumps({"ordinal": 0, "type": "session_meta", "timestamp": "2026-11-23T03:00:00Z", "payload": {"id": future_sid, "cwd": "/tmp/future"}}),
                    json.dumps({"ordinal": 1, "type": "response_item", "timestamp": "2026-11-23T03:00:01Z", "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "FUTURE_DIR_TURN"}]}}),
                ]
            )
            + "\n",
            encoding="utf-8",
        )

        tmux_stdout = "\n".join(
            [
                _make_raw_line("%10", pane_index="1", cmd="codex", tty="/dev/ttys010", title="Codex B", sock=self.sock, srv=self.srv),
                _make_raw_line("%11", pane_index="2", cmd="codex", tty="/dev/ttys011", title="Codex Future", sock=self.sock, srv=self.srv),
            ]
        )

        def fake_tmux(args: list[str]) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(args, 0, stdout=tmux_stdout + "\n", stderr="")

        def fake_pgrep(args: list[str]) -> list[int]:
            return [3010, 3011] if args[-1] == "codex" else []

        def fake_lsof(args: list[str]) -> str:
            pid_arg = args[args.index("-p") + 1]
            out: list[str] = []
            if "3010" in pid_arg:
                out.extend(["p3010", "f0", "au", "n/dev/ttys010", "f9", "aw", f"n{b_seg2}"])
            if "3011" in pid_arg:
                out.extend(["p3011", "f0", "au", "n/dev/ttys011", "f9", "aw", f"n{future_file}"])
            return "\n".join(out) + "\n"

        env = {"HOME": str(self.home), "AIPANE_STATE_DIR": str(self.state_dir), "TMUX_PANE": "%10"}
        real_list_live_panes = live_mod.list_live_panes

        def patched_list_live_panes(**kwargs):
            kwargs.setdefault("tmux_runner", fake_tmux)
            kwargs.setdefault("pgrep_runner", fake_pgrep)
            kwargs.setdefault("lsof_runner", fake_lsof)
            kwargs.setdefault("process_exists", lambda pid: pid in {3010, 3011})
            return real_list_live_panes(**kwargs)

        with mock.patch.object(live_mod, "list_live_panes", side_effect=patched_list_live_panes):
            panes = live_mod.list_live_panes(environment=env, home=self.home)
            by_id = {p.pane_id: p for p in panes}
            self.assertEqual(by_id["%10"].storage_root, str(custom_home.resolve()))
            self.assertEqual(by_id["%11"].session_id, future_sid)
            self.assertEqual(by_id["%11"].active_path, str(future_file))
            self.assertEqual(by_id["%11"].storage_root, str(default_home.resolve()))

            # 1. read --pane %10 must use custom_home B and NEVER mix default_home A
            res_b = sessions_mod.read_session(pane="%10", view="timeline", full=True, include_children=False, home=self.home, environment=env)
            self.assertTrue(res_b["complete"])
            texts_b = [ev["text"] for s in res_b["sources"] for ev in s["events"]]
            self.assertEqual(texts_b, ["CUSTOM_HOME_B_OPENING", "CUSTOM_HOME_B_TAIL"])
            self.assertNotIn("WRONG_HOME_A_OPENING", texts_b)

            # 2. read --pane %11 must recover future_threads/ file and mark complete=False
            res_future = sessions_mod.read_session(pane="%11", view="timeline", full=True, include_children=False, home=self.home, environment=env)
            self.assertFalse(res_future["complete"])
            self.assertTrue(any("active_file_missed_by_discovery" in w for w in res_future["warnings"]))
            self.assertEqual(res_future["sources"][0]["events"][0]["text"], "FUTURE_DIR_TURN")

            # 3. doctor drift audit must check BOTH live panes and flag ONLY %11 as uncovered
            audit = sessions_mod.audit_session_storage_drift(home=self.home, days=14, environment=env, check_live_panes=True)
            self.assertEqual(audit["checked_live_panes"], 2)
            uncovered = [i for i in audit["issues"] if i["code"] == "live_pane_active_path_uncovered"]
            self.assertEqual(len(uncovered), 1)
            self.assertIn(str(future_file), uncovered[0]["message"])

    def test_r2_p1_1_and_p1_2_uncommitted_pane_rollback_and_custom_archived_sqlite_root(self) -> None:
        from contextlib import closing
        import sqlite3
        from unittest import mock

        # Part A (P1-1): pane vs SID consistency when uncommitted rollback segment exists
        sid = "77777777-aaaa-4777-8777-777777777777"
        rid = "88888888-aaaa-4888-8888-888888888888"
        chome = self.home / ".codex"
        logs = chome / "sessions/2026/11/23"
        logs.mkdir(parents=True, exist_ok=True)
        p = logs / f"rollout-2026-11-23T07-00-00-{sid}.jsonl"
        q = logs / f"rollout-2026-11-23T08-00-00-{sid}_{rid}.jsonl"
        hdr_p = json.dumps({"ordinal": 0, "type": "session_meta", "timestamp": "2026-11-23T07:00:00Z", "payload": {"id": sid, "cwd": str(self.home)}}) + "\n"
        p.write_text(
            hdr_p
            + json.dumps({"ordinal": 1, "type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "COMMITTED_OPENING"}]}}) + "\n"
            + json.dumps({"ordinal": 2, "type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "COMMITTED_TAIL"}]}}) + "\n",
            encoding="utf-8",
        )
        q.write_text(
            json.dumps({"ordinal": 1, "type": "session_meta", "timestamp": "2026-11-23T08:00:00Z", "payload": {"id": sid, "cwd": str(self.home), "history_base": {"thread_id": sid, "end_ordinal_exclusive": 1, "end_byte_offset": len(hdr_p.encode())}}}) + "\n",
            encoding="utf-8",
        )
        with closing(sqlite3.connect(chome / "state_7.sqlite")) as db, db:
            db.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, rollout_path TEXT, updated_at INTEGER)")
            db.execute("INSERT INTO threads VALUES (?, ?, ?)", (sid, str(p), int(time.time())))

        env = {"HOME": str(self.home), "AIPANE_STATE_DIR": str(self.state_dir), "TMUX_PANE": "%20"}
        sid_res = sessions_mod.read_session(f"codex:{sid}", view="timeline", full=True, include_children=False, home=self.home, environment=env)
        sid_texts = [ev["text"] for s in sid_res["sources"] for ev in s["events"]]
        self.assertEqual(sid_texts, ["COMMITTED_OPENING", "COMMITTED_TAIL"])

        for mode, open_files, expect_complete in (("both", [p, q], True), ("replacement_only", [q], False)):
            tmux_out = _make_raw_line("%20", pane_index="1", cmd="codex", tty="/dev/ttys020", title="Codex", sock=self.sock, srv=self.srv) + "\n"
            real_list = live_mod.list_live_panes

            def fake_lsof(args: list[str], files=open_files) -> str:
                out = ["p4020", "f0", "au", "n/dev/ttys020"]
                for idx, fp in enumerate(files, start=9):
                    out.extend([f"f{idx}", "aw", f"n{fp}"])
                return "\n".join(out) + "\n"

            def patched_list(**kwargs):
                kwargs.setdefault("tmux_runner", lambda a: subprocess.CompletedProcess(a, 0, stdout=tmux_out, stderr=""))
                kwargs.setdefault("pgrep_runner", lambda a: [4020] if a[-1] == "codex" else [])
                kwargs.setdefault("lsof_runner", fake_lsof)
                kwargs.setdefault("process_exists", lambda pid: pid == 4020)
                return real_list(**kwargs)

            with mock.patch.object(live_mod, "list_live_panes", side_effect=patched_list):
                pane_res = sessions_mod.read_session(pane="%20", view="timeline", full=True, include_children=False, home=self.home, environment=env)
            pane_texts = [ev["text"] for s in pane_res["sources"] for ev in s["events"]]
            self.assertEqual(pane_texts, sid_texts, f"pane diverged from SID in mode={mode}")
            self.assertEqual(pane_res["complete"], expect_complete)
            if not expect_complete:
                self.assertTrue(any("uncommitted_active_rollout" in w for w in pane_res["warnings"]))

        # Part B (P1-2): custom CODEX_HOME with only archived_sessions/, sqlite/state_12.sqlite, and future_threads/
        custom_root = self.home / "custom-nested-home"
        (custom_root / "archived_sessions").mkdir(parents=True, exist_ok=True)
        (custom_root / "sqlite").mkdir(parents=True, exist_ok=True)
        custom_future = custom_root / "future_threads" / f"rollout-2026-11-23T09-00-00-{sid}.jsonl"
        custom_future.parent.mkdir(parents=True, exist_ok=True)
        custom_future.write_text(
            json.dumps({"ordinal": 0, "type": "session_meta", "timestamp": "2026-11-23T09:00:00Z", "payload": {"id": sid}}) + "\n"
            + json.dumps({"ordinal": 1, "type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "CUSTOM_ONLY_TURN"}]}}) + "\n",
            encoding="utf-8",
        )
        with closing(sqlite3.connect(custom_root / "sqlite/state_12.sqlite")) as db, db:
            db.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, rollout_path TEXT, updated_at INTEGER)")
            db.execute("INSERT INTO threads VALUES (?, ?, ?)", (sid, str(custom_future), int(time.time())))

        tmux_custom = _make_raw_line("%21", pane_index="2", cmd="codex", tty="/dev/ttys021", title="Codex", sock=self.sock, srv=self.srv) + "\n"
        real_list = live_mod.list_live_panes

        def patched_custom(**kwargs):
            kwargs.setdefault("tmux_runner", lambda a: subprocess.CompletedProcess(a, 0, stdout=tmux_custom, stderr=""))
            kwargs.setdefault("pgrep_runner", lambda a: [4021] if a[-1] == "codex" else [])
            kwargs.setdefault("lsof_runner", lambda a: f"p4021\nf0\nau\nn/dev/ttys021\nf9\naw\nn{custom_future}\n")
            kwargs.setdefault("process_exists", lambda pid: pid == 4021)
            return real_list(**kwargs)

        with mock.patch.object(live_mod, "list_live_panes", side_effect=patched_custom):
            custom_res = sessions_mod.read_session(pane="%21", view="timeline", full=True, include_children=False, home=self.home, environment=env)
        custom_texts = [ev["text"] for s in custom_res["sources"] for ev in s["events"]]
        self.assertEqual(custom_texts, ["CUSTOM_ONLY_TURN"])
        self.assertNotIn("COMMITTED_OPENING", custom_texts)

        # Bare unknown root (no markers, no CODEX_HOME env) must NOT mix default ~/.codex
        orphan_file = self.home / "orphan_dir" / f"rollout-2026-11-23T09-30-00-{sid}.jsonl"
        orphan_file.parent.mkdir(parents=True, exist_ok=True)
        orphan_file.write_text(
            json.dumps({"ordinal": 0, "type": "session_meta", "timestamp": "2026-11-23T09:30:00Z", "payload": {"id": sid}}) + "\n"
            + json.dumps({"ordinal": 1, "type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "ORPHAN_ONLY_TURN"}]}}) + "\n",
            encoding="utf-8",
        )

        def patched_orphan(**kwargs):
            kwargs.setdefault("tmux_runner", lambda a: subprocess.CompletedProcess(a, 0, stdout=tmux_custom, stderr=""))
            kwargs.setdefault("pgrep_runner", lambda a: [4022] if a[-1] == "codex" else [])
            kwargs.setdefault("lsof_runner", lambda a: f"p4022\nf0\nau\nn/dev/ttys021\nf9\naw\nn{orphan_file}\n")
            kwargs.setdefault("process_exists", lambda pid: pid == 4022)
            return real_list(**kwargs)

        with (
            mock.patch.object(live_mod, "list_live_panes", side_effect=patched_orphan),
            mock.patch.object(live_mod, "_process_env_var", return_value=""),
        ):
            orphan_res = sessions_mod.read_session(pane="%21", view="timeline", full=True, include_children=False, home=self.home, environment=env)
        self.assertFalse(orphan_res["complete"])
        self.assertTrue(any("unknown_pane_storage_root" in w for w in orphan_res["warnings"]))
        orphan_texts = [ev["text"] for s in orphan_res["sources"] for ev in s["events"]]
        self.assertEqual(orphan_texts, ["ORPHAN_ONLY_TURN"])
        self.assertNotIn("COMMITTED_OPENING", orphan_texts)

    def test_r3_p1_codex_home_with_spaces_and_registry_only_root_verification(self) -> None:
        from unittest import mock

        # 1. Kernel envp reader preserves spaces in CODEX_HOME and ignores spoofed argv text
        spaced_probe = self.home / "Profile Work"
        spaced_probe.mkdir(parents=True, exist_ok=True)
        child_env = {**os.environ, "CODEX_HOME": str(spaced_probe), "HOME": str(self.home)}
        proc = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import time; time.sleep(15)",
                "look up CODEX_HOME=/foreign-profile in prompt",
            ],
            env=child_env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            self.assertEqual(
                live_mod._process_env_var(proc.pid, "CODEX_HOME"),
                str(spaced_probe),
            )
            self.assertEqual(
                live_mod._process_codex_home(proc.pid),
                str(spaced_probe.resolve()),
            )
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()

        # 2. Registry-only Codex pane with unconfirmed storage_root must NOT fall back to default ~/.codex
        sid = "91919191-aaaa-4919-8919-919191919191"
        default_file = self.home / ".codex/sessions/2026/11/23" / f"rollout-2026-11-23T10-00-00-{sid}.jsonl"
        default_file.parent.mkdir(parents=True, exist_ok=True)
        default_file.write_text(
            json.dumps({"ordinal": 0, "type": "session_meta", "timestamp": "2026-11-23T02:00:00Z", "payload": {"id": sid}}) + "\n"
            + json.dumps({"ordinal": 1, "type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "WRONG_DEFAULT"}]}}) + "\n",
            encoding="utf-8",
        )
        reg_path = self.state_dir / "registry.jsonl"
        reg_path.parent.mkdir(parents=True, exist_ok=True)
        reg_path.write_text(
            json.dumps({"sock": self.sock, "srv": self.srv, "pane": "%31", "tool": "x", "sid": sid, "ts": int(time.time())}) + "\n",
            encoding="utf-8",
        )
        tmux_out = _make_raw_line("%31", pane_index="1", cmd="codex", tty="/dev/ttys031", title="Codex", sock=self.sock, srv=self.srv) + "\n"
        env = {"HOME": str(self.home), "AIPANE_STATE_DIR": str(self.state_dir), "TMUX_PANE": "%31"}
        real_list = live_mod.list_live_panes

        def patched_reg(**kwargs):
            kwargs.setdefault("tmux_runner", lambda a: subprocess.CompletedProcess(a, 0, stdout=tmux_out, stderr=""))
            kwargs.setdefault("pgrep_runner", lambda a: [])
            kwargs.setdefault("lsof_runner", lambda a: "")
            return real_list(**kwargs)

        with mock.patch.object(live_mod, "list_live_panes", side_effect=patched_reg):
            with self.assertRaisesRegex(FileNotFoundError, "unknown_pane_storage_root"):
                sessions_mod.read_session(pane="%31", view="timeline", full=True, include_children=False, home=self.home, environment=env)

        # 3. When live Codex PID exposes CODEX_HOME (with spaces!), registry-only pane resolves custom root
        spaced_root = self.home / "Profile Work"
        spaced_file = spaced_root / "sessions/2026/11/23" / f"rollout-2026-11-23T10-00-00-{sid}.jsonl"
        spaced_file.parent.mkdir(parents=True, exist_ok=True)
        spaced_file.write_text(
            json.dumps({"ordinal": 0, "type": "session_meta", "timestamp": "2026-11-23T02:00:00Z", "payload": {"id": sid}}) + "\n"
            + json.dumps({"ordinal": 1, "type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "RIGHT_SPACED_CUSTOM"}]}}) + "\n",
            encoding="utf-8",
        )

        def patched_reg_with_pid(**kwargs):
            kwargs.setdefault("tmux_runner", lambda a: subprocess.CompletedProcess(a, 0, stdout=tmux_out, stderr=""))
            kwargs.setdefault("pgrep_runner", lambda a: [4031] if a[-1] == "codex" else [])
            kwargs.setdefault("lsof_runner", lambda a: "p4031\nf0\nau\nn/dev/ttys031\n")
            return real_list(**kwargs)

        with (
            mock.patch.object(live_mod, "list_live_panes", side_effect=patched_reg_with_pid),
            mock.patch.object(live_mod, "_process_codex_home", return_value=str(spaced_root.resolve())),
        ):
            ok_res = sessions_mod.read_session(pane="%31", view="timeline", full=True, include_children=False, home=self.home, environment=env)
        self.assertTrue(ok_res["complete"])
        self.assertEqual([ev["text"] for s in ok_res["sources"] for ev in s["events"]], ["RIGHT_SPACED_CUSTOM"])

        # 4. Live Codex PID env beats stale registry codex_home when they disagree
        stale_root = self.home / "stale-profile"
        stale_file = stale_root / "sessions/2026/11/23" / f"rollout-2026-11-23T10-00-00-{sid}.jsonl"
        stale_file.parent.mkdir(parents=True, exist_ok=True)
        stale_file.write_text(
            json.dumps({"ordinal": 0, "type": "session_meta", "timestamp": "2026-11-23T02:00:00Z", "payload": {"id": sid}}) + "\n"
            + json.dumps({"ordinal": 1, "type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "STALE_REGISTRY_ROOT"}]}}) + "\n",
            encoding="utf-8",
        )
        reg_path.write_text(
            json.dumps({"sock": self.sock, "srv": self.srv, "pane": "%31", "tool": "x", "sid": sid, "codex_home": str(stale_root), "ts": int(time.time())}) + "\n",
            encoding="utf-8",
        )
        with (
            mock.patch.object(live_mod, "list_live_panes", side_effect=patched_reg_with_pid),
            mock.patch.object(live_mod, "_process_codex_home", return_value=str(spaced_root.resolve())),
        ):
            fresh_res = sessions_mod.read_session(pane="%31", view="timeline", full=True, include_children=False, home=self.home, environment=env)
        self.assertTrue(fresh_res["complete"])
        self.assertEqual([ev["text"] for s in fresh_res["sources"] for ev in s["events"]], ["RIGHT_SPACED_CUSTOM"])

        # 5. Relative CODEX_HOME in live process env resolves against target process cwd, never reader cwd
        workspace = self.home / "workspace"
        target_profile = workspace / "profile"
        reader_shadow = self.home / "profile"
        target_profile.mkdir(parents=True, exist_ok=True)
        reader_shadow.mkdir(parents=True, exist_ok=True)
        rel_proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(15)"],
            cwd=workspace,
            env={**os.environ, "CODEX_HOME": "profile", "HOME": str(self.home)},
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            prev_cwd = os.getcwd()
            os.chdir(self.home)
            try:
                resolved_root = live_mod._process_codex_home(rel_proc.pid)
            finally:
                os.chdir(prev_cwd)
            self.assertEqual(resolved_root, str(target_profile.resolve()))
            self.assertNotEqual(resolved_root, str(reader_shadow.resolve()))
        finally:
            rel_proc.terminate()
            try:
                rel_proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                rel_proc.kill()
                rel_proc.wait()

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
        self.assertEqual(picker.format_copy_payload(single), "agy:1111-aaaa")
        self.assertEqual(
            picker.format_copy_payload(multi),
            "agy:1111-aaaa\ncodex:2222-bbbb\nagy:ide/3333-cccc",
        )

    def test_picker_enter_injects_without_clobbering_default_buffer_and_ctrl_y_copies_ref(self) -> None:
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

        # Fake fzf #2: Ctrl+Y copies '<agent>:<sid>' ref to clipboard + tmux buffer.
        fake_fzf_yank = self.home / "fzf-yank"
        fake_fzf_yank.write_text("#!/bin/sh\nline=$(head -n 1)\nprintf 'ctrl-y\\n%s\\n' \"$line\"\n", encoding="utf-8")
        fake_fzf_yank.chmod(0o755)
        run_env["AIPANE_FZF_BIN"] = str(fake_fzf_yank)
        res2 = subprocess.run([str(PICKER_BIN)], env=run_env, capture_output=True, text=True, check=False)
        self.assertEqual(res2.returncode, 0, res2.stderr)
        self.assertEqual(self.tmux("show-buffer").stdout, f"dsh:{dsh_sid}")

        # Direct yank defaults to '<agent>:<sid>' ref and supports explicit --format raw.
        out_ref, code_ref = sessions_mod.yank_pane_session(p2, display=False, home=self.home, environment=self.env)
        self.assertEqual(code_ref, 0)
        self.assertEqual(out_ref["copied"], f"dsh:{dsh_sid}")
        self.assertEqual(self.tmux("show-buffer").stdout, f"dsh:{dsh_sid}")

        out_raw, code_raw = sessions_mod.yank_pane_session(p2, fmt="raw", display=False, home=self.home, environment=self.env)
        self.assertEqual(code_raw, 0)
        self.assertEqual(out_raw["copied"], dsh_sid)
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

            expected_ref = f"dsh:{dsh_sid}"
            deadline = time.monotonic() + 3.0
            copied = ""
            while time.monotonic() < deadline:
                copied = self.tmux("show-buffer", check=False).stdout.strip()
                if copied == expected_ref:
                    break
                time.sleep(0.05)
            self.assertEqual(copied, expected_ref)
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
                if copied_y == expected_ref:
                    break
                time.sleep(0.05)
            self.assertEqual(copied_y, expected_ref)
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
