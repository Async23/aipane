"""Unit tests for the zero-index local session lookup CLI (lib/agent_sessions.py)."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

import agent_sessions as mod  # noqa: E402


def write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def write_zstd_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records).encode("utf-8")
    proc = subprocess.run(
        [mod.zstd_binary(), "-q", "-o", str(path)],
        input=raw,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.decode("utf-8", errors="replace"))


class AgentSessionsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="aipane-sessions-test-")
        self.home = Path(self.tmp.name)
        self.now = time.time()
        # Isolate env overrides so host environment variables do not leak into tests
        self._env_patch = {}
        for k in (
            "CODEX_HOME",
            "CLAUDE_CONFIG_DIR",
            "GEMINI_HOME",
            "DSH_HOME",
            "GROK_HOME",
            "PI_CODING_AGENT_DIR",
            "KIMI_HOME",
        ):
            self._env_patch[k] = os.environ.pop(k, None)

    def tearDown(self) -> None:
        for k, v in self._env_patch.items():
            if v is not None:
                os.environ[k] = v
        self.tmp.cleanup()

    def _seed_codex(self, sid: str, text: str, age_days: float = 0.0, parent: str | None = None) -> Path:
        p = self.home / ".codex" / "sessions" / "2026" / "11" / "19" / f"rollout-2026-11-19T10-00-00-{sid}.jsonl"
        meta_payload = {"id": sid}
        if parent:
            meta_payload["parent_thread_id"] = parent
        write_jsonl(
            p,
            [
                {"type": "session_meta", "timestamp": "2026-11-19T02:00:00Z", "payload": meta_payload},
                {
                    "type": "response_item",
                    "timestamp": "2026-11-19T02:00:01Z",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "# AGENTS.md noise"}],
                    },
                },
                {
                    "type": "response_item",
                    "timestamp": "2026-11-19T02:00:02Z",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": text}],
                    },
                },
                {
                    "type": "response_item",
                    "timestamp": "2026-11-19T02:00:03Z",
                    "payload": {"type": "function_call", "name": "exec_command"},
                },
                {
                    "type": "response_item",
                    "timestamp": "2026-11-19T02:00:04Z",
                    "payload": {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "Done inspecting."}],
                    },
                },
            ],
        )
        ts = self.now - age_days * 86400
        os.utime(p, (ts, ts))
        return p

    def _seed_antigravity(self, sid: str, request: str, is_ide: bool = False) -> Path:
        root = "antigravity" if is_ide else "antigravity-cli"
        p = self.home / ".gemini" / root / "brain" / sid / ".system_generated" / "logs" / "transcript_full.jsonl"
        write_jsonl(
            p,
            [
                {
                    "type": "USER_INPUT",
                    "created_at": "2026-11-19T01:00:00Z",
                    "content": f"<USER_REQUEST>\n{request}\n</USER_REQUEST>",
                },
                {
                    "type": "PLANNER_RESPONSE",
                    "created_at": "2026-11-19T01:00:05Z",
                    "content": "Checking file now.",
                    "tool_calls": [{"name": "read_file"}],
                },
            ],
        )
        return p

    def _seed_claude(self, sid: str, child_id: str | None = None) -> tuple[Path, Path | None]:
        proj = self.home / ".claude" / "projects" / "-tmp-proj"
        main_p = proj / f"{sid}.jsonl"
        write_jsonl(
            main_p,
            [
                {
                    "type": "user",
                    "timestamp": "2026-11-19T01:00:00Z",
                    "message": {"role": "user", "content": [{"type": "text", "text": "Fix login bug"}]},
                },
                {
                    "type": "assistant",
                    "timestamp": "2026-11-19T01:00:02Z",
                    "message": {
                        "role": "assistant",
                        "content": [
                            {"type": "text", "text": "Looking at auth.py"},
                            {"type": "tool_use", "name": "Read"},
                        ],
                    },
                },
            ],
        )
        child_p = None
        if child_id:
            child_p = proj / sid / "subagents" / f"{child_id}.jsonl"
            write_jsonl(
                child_p,
                [
                    {
                        "type": "user",
                        "timestamp": "2026-11-19T01:00:03Z",
                        "message": {"role": "user", "content": "Subagent task"},
                    }
                ],
            )
        return main_p, child_p

    def _seed_dsh(self, sid: str, text: str, parent: str | None = None) -> Path:
        p = self.home / ".dsh" / "sessions" / "--tmp-work--" / sid / "session.v4.jsonl.zstd"
        hdr = {"type": "session", "version": 4, "id": sid, "createdAt": 1791446825469}
        if parent:
            hdr["parentSession"] = parent
        write_zstd_jsonl(
            p,
            [
                hdr,
                {
                    "type": "user/message",
                    "time": 1791446826000,
                    "data": {"source": {"kind": "user"}, "content": [{"type": "text", "text": text}]},
                },
                {
                    "type": "assistant/message",
                    "time": 1791446827000,
                    "data": {"message": {"role": "assistant", "content": [{"type": "text", "text": "DSH reply"}]}},
                },
            ],
        )
        return p

    def test_list_time_pruning_and_preview_filtering(self) -> None:
        hot_id = "11111111-1111-1111-1111-111111111111"
        cold_id = "22222222-2222-2222-2222-222222222222"
        self._seed_codex(hot_id, "Inspect hot session", age_days=1)
        self._seed_codex(cold_id, "Inspect cold session", age_days=30)

        res = mod.list_sessions(days=14, home=self.home, now=self.now)
        self.assertEqual(res["count"], 1)
        self.assertEqual(res["sessions"][0]["session_id"], hot_id)
        self.assertEqual(res["sessions"][0]["preview"], "Inspect hot session")
        self.assertEqual(res["scanned_scope"]["skipped_cold_sessions"], 1)

        res_all = mod.list_sessions(scan_all=True, home=self.home, now=self.now)
        self.assertEqual(res_all["count"], 2)
        self.assertEqual(res_all["scanned_scope"]["skipped_cold_sessions"], 0)

    def test_find_plain_zstd_and_long_line_window_truncation(self) -> None:
        if not shutil.which("rg") or not shutil.which("zstd"):
            self.skipTest("rg and zstd are required")

        codex_id = "33333333-3333-3333-3333-333333333333"
        huge_text = ("A" * 2000) + "UNIQUE_NEEDLE_XYZ" + ("B" * 2000)
        self._seed_codex(codex_id, huge_text, age_days=0)

        dsh_id = "44444444-4444-4444-4444-444444444444"
        self._seed_dsh(dsh_id, "Compressed UNIQUE_NEEDLE_XYZ in zstd")

        res = mod.find_sessions("UNIQUE_NEEDLE_XYZ", home=self.home, now=self.now)
        self.assertEqual(res["count"], 2)
        by_agent = {h["agent"]: h for h in res["hits"]}
        self.assertIn("codex", by_agent)
        self.assertIn("dsh", by_agent)
        self.assertIn("UNIQUE_NEEDLE_XYZ", by_agent["codex"]["snippet"])
        self.assertLess(len(by_agent["codex"]["snippet"]), 320)

    def test_read_timeline_and_children_across_agents(self) -> None:
        if not shutil.which("zstd"):
            self.skipTest("zstd is required")

        # Claude parent + subagent
        cid = "55555555-5555-5555-5555-555555555555"
        self._seed_claude(cid, child_id="agent-sub1")
        cl = mod.read_session(cid, agent="claude", view="timeline", home=self.home)
        self.assertEqual(len(cl["sources"]), 2)
        self.assertEqual(cl["sources"][0]["events"][0]["text"], "Fix login bug")
        self.assertEqual(cl["sources"][1]["parent"], cid)

        # DSH parent + child (.zstd)
        dp = "66666666-6666-6666-6666-666666666666"
        dc = "77777777-7777-7777-7777-777777777777"
        self._seed_dsh(dp, "Parent DSH question")
        self._seed_dsh(dc, "Child DSH task", parent=dp)
        dsh_res = mod.read_session(dp, agent="dsh", view="timeline", home=self.home)
        self.assertEqual(len(dsh_res["sources"]), 2)
        self.assertEqual(dsh_res["sources"][0]["events"][1]["text"], "DSH reply")
        self.assertEqual(dsh_res["sources"][1]["session_id"], dc)

        # Antigravity IDE qualification
        aid = "88888888-8888-8888-8888-888888888888"
        self._seed_antigravity(aid, "IDE prompt", is_ide=True)
        agy_res = mod.read_session(f"ide/{aid}", agent="agy", view="timeline", home=self.home)
        self.assertEqual(agy_res["sources"][0]["events"][0]["text"], "IDE prompt")

    def test_read_export_writes_manifest_and_verifies_sha256(self) -> None:
        if not shutil.which("zstd"):
            self.skipTest("zstd is required")

        dsh_id = "99999999-9999-9999-9999-999999999999"
        self._seed_dsh(dsh_id, "Export me losslessly")
        out_dir = self.home / "export-out"

        res = mod.read_session(dsh_id, agent="dsh", view="export", output_dir=str(out_dir), home=self.home)
        manifest_path = Path(res["manifest_path"])
        self.assertTrue(manifest_path.is_file())
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertTrue(manifest["complete"])
        src = manifest["sources"][0]
        exported_bytes = Path(src["exported"]).read_bytes()
        self.assertEqual(hashlib.sha256(exported_bytes).hexdigest(), src["sha256"])
        self.assertEqual(src["records"], 3)
        self.assertEqual(src["attachments"], [])
        self.assertFalse((out_dir / "attachments").exists())

        # Refuses to overwrite existing directory
        with self.assertRaises(FileExistsError):
            mod.read_session(dsh_id, agent="dsh", view="export", output_dir=str(out_dir), home=self.home)

    def test_read_timeline_full_preserves_long_multiline_and_all_events(self) -> None:
        sid = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
        p = self.home / ".codex" / "sessions" / "2026" / "11" / "20" / f"rollout-2026-11-20T10-00-00-{sid}.jsonl"
        long_reply = "Line one\n" + ("中" * 420) + "\nFinal sentinel tail."
        records = [{"type": "session_meta", "timestamp": "2026-11-20T02:00:00Z", "payload": {"id": sid}}]
        for i in range(130):
            records.append(
                {
                    "type": "response_item",
                    "timestamp": f"2026-11-20T02:00:{i:02d}Z",
                    "payload": {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": long_reply if i == 129 else f"msg-{i}"}],
                    },
                }
            )
        write_jsonl(p, records)

        # Default timeline truncates at 120 events and 300 chars
        compact = mod.read_session(sid, agent="codex", view="timeline", home=self.home)
        self.assertFalse(compact["full"])
        c_src = compact["sources"][0]
        self.assertTrue(c_src["truncated"])
        self.assertEqual(len(c_src["events"]), 120)

        # --full returns all 130 events and full untruncated multiline text
        full_res = mod.read_session(sid, agent="codex", view="timeline", full=True, home=self.home)
        self.assertTrue(full_res["full"])
        f_src = full_res["sources"][0]
        self.assertFalse(f_src["truncated"])
        self.assertEqual(len(f_src["events"]), 130)
        self.assertEqual(f_src["events"][-1]["text"], long_reply)

        # Grok streaming chunks merge without 300-char cap under full=True
        gid = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
        gp = self.home / ".grok" / "sessions" / gid / "updates.jsonl"
        write_jsonl(
            gp,
            [
                {
                    "params": {
                        "update": {"sessionUpdate": "agent_message_chunk", "content": {"text": "Part1 " + ("X" * 250)}}
                    }
                },
                {
                    "params": {
                        "update": {"sessionUpdate": "agent_message_chunk", "content": {"text": "\nPart2 " + ("Y" * 250)}}
                    }
                },
            ],
        )
        grok_full = mod.read_session(gid, agent="grok", view="timeline", full=True, home=self.home)
        self.assertEqual(
            grok_full["sources"][0]["events"][0]["text"],
            "Part1 " + ("X" * 250) + "\nPart2 " + ("Y" * 250),
        )

    def test_read_export_unpacks_embedded_and_linked_images(self) -> None:
        png_bytes = b"\x89PNG\r\n\x1a\nfake-png-payload"
        jpg_bytes = b"\xff\xd8\xff\xe0fake-jpg-payload"
        b64_png = base64.b64encode(png_bytes).decode("ascii")
        b64_jpg = base64.b64encode(jpg_bytes).decode("ascii")

        # 1. Codex data:image/png;base64 + malformed base64 tolerance
        codex_id = "cccccccc-cccc-cccc-cccc-cccccccccccc"
        cp = self.home / ".codex" / "sessions" / "2026" / "11" / "20" / f"rollout-2026-11-20T10-00-00-{codex_id}.jsonl"
        write_jsonl(
            cp,
            [
                {"type": "session_meta", "timestamp": "2026-11-20T02:00:00Z", "payload": {"id": codex_id}},
                {
                    "type": "response_item",
                    "timestamp": "2026-11-20T02:00:01Z",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [
                            {"type": "input_image", "image_url": f"data:image/png;base64,{b64_png}"},
                            {"type": "input_image", "image_url": "data:image/png;base64,!!!not-valid!!!"},
                            {"type": "input_text", "text": "Look at this screenshot"},
                        ],
                    },
                },
            ],
        )
        codex_out = self.home / "export-codex"
        cres = mod.read_session(codex_id, agent="codex", view="export", output_dir=str(codex_out), home=self.home)
        c_src = cres["manifest"]["sources"][0]
        self.assertEqual(len(c_src["attachments"]), 1)
        att = c_src["attachments"][0]
        self.assertEqual(att["line"], 2)
        self.assertEqual(att["mime_type"], "image/png")
        self.assertTrue(att["path"].endswith("001-L2-01.png"))
        self.assertEqual(Path(att["path"]).read_bytes(), png_bytes)
        self.assertEqual(hashlib.sha256(png_bytes).hexdigest(), att["sha256"])

        # 2. Claude Anthropic base64 image block
        claude_id = "dddddddd-dddd-dddd-dddd-dddddddddddd"
        clp = self.home / ".claude" / "projects" / "-tmp-proj" / f"{claude_id}.jsonl"
        write_jsonl(
            clp,
            [
                {
                    "type": "user",
                    "timestamp": "2026-11-20T02:00:01Z",
                    "message": {
                        "role": "user",
                        "content": [
                            {
                                "type": "image",
                                "source": {"type": "base64", "media_type": "image/jpeg", "data": b64_jpg},
                            }
                        ],
                    },
                }
            ],
        )
        claude_out = self.home / "export-claude"
        clres = mod.read_session(claude_id, agent="claude", view="export", output_dir=str(claude_out), home=self.home)
        cl_att = clres["manifest"]["sources"][0]["attachments"][0]
        self.assertEqual(cl_att["mime_type"], "image/jpeg")
        self.assertTrue(cl_att["path"].endswith("001-L1-01.jpg"))
        self.assertEqual(Path(cl_att["path"]).read_bytes(), jpg_bytes)

        # 3. Pi inline base64 image block
        pi_id = "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"
        pip = self.home / ".pi" / "agent" / "sessions" / f"2026-11-20_{pi_id}.jsonl"
        write_jsonl(
            pip,
            [
                {
                    "type": "message",
                    "message": {
                        "role": "user",
                        "content": [{"type": "image", "data": b64_png, "mimeType": "image/png"}],
                    },
                }
            ],
        )
        pi_out = self.home / "export-pi"
        pires = mod.read_session(pi_id, agent="pi", view="export", output_dir=str(pi_out), home=self.home)
        self.assertEqual(Path(pires["manifest"]["sources"][0]["attachments"][0]["path"]).read_bytes(), png_bytes)

        # 4. Antigravity file:// media URI copied into attachments/
        agy_id = "ffffffff-ffff-ffff-ffff-ffffffffffff"
        media_src = self.home / ".gemini" / "antigravity-cli" / "brain" / agy_id / ".system_generated" / "logs" / "media" / "shot.jpg"
        media_src.parent.mkdir(parents=True, exist_ok=True)
        media_src.write_bytes(jpg_bytes)
        agy_log = self.home / ".gemini" / "antigravity-cli" / "brain" / agy_id / ".system_generated" / "logs" / "transcript_full.jsonl"
        write_jsonl(
            agy_log,
            [
                {
                    "type": "USER_INPUT",
                    "created_at": "2026-11-20T02:00:00Z",
                    "content": "<USER_REQUEST>Check image</USER_REQUEST>",
                    "media": [{"mime_type": "image/jpeg", "uri": media_src.as_uri()}],
                }
            ],
        )
        agy_out = self.home / "export-agy"
        ares = mod.read_session(agy_id, agent="agy", view="export", output_dir=str(agy_out), home=self.home)
        a_att = ares["manifest"]["sources"][0]["attachments"][0]
        self.assertEqual(a_att["source_uri"], media_src.as_uri())
        self.assertEqual(Path(a_att["path"]).read_bytes(), jpg_bytes)

    def test_codex_suffixed_segments_archived_cross_day_children_and_large_meta(self) -> None:
        sid = "12121212-1212-7212-8212-121212121212"
        seg2_rollout = "34343434-3434-7434-8434-343434343434"
        seg3_rollout = "56565656-5656-7656-8656-565656565656"
        child_sid = "78787878-7878-7878-8878-787878787878"

        # Segment 1: archived_sessions with >16KB session_meta header (28KB+ real-world size)
        seg1 = self.home / ".codex" / "archived_sessions" / f"rollout-2026-11-10T10-00-00-{sid}.jsonl"
        write_jsonl(
            seg1,
            [
                {
                    "ordinal": 0,
                    "type": "session_meta",
                    "timestamp": "2026-11-10T02:00:00Z",
                    "payload": {"id": sid, "base_instructions": "X" * 28000},
                },
                {
                    "ordinal": 1,
                    "type": "response_item",
                    "timestamp": "2026-11-10T02:00:01Z",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "Segment 1 user message"}],
                    },
                },
            ],
        )
        # Segment 2: suffixed rollout in sessions/2026/11/13
        seg2 = (
            self.home
            / ".codex"
            / "sessions"
            / "2026"
            / "11"
            / "13"
            / f"rollout-2026-11-13T14-51-42-{sid}_{seg2_rollout}.jsonl"
        )
        write_jsonl(
            seg2,
            [
                {
                    "ordinal": 10,
                    "type": "session_meta",
                    "timestamp": "2026-11-13T06:51:42Z",
                    "payload": {"id": sid},
                },
                {
                    "ordinal": 11,
                    "type": "response_item",
                    "timestamp": "2026-11-13T06:51:43Z",
                    "payload": {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "Segment 2 reply"}],
                    },
                },
            ],
        )
        # Segment 3: suffixed rollout on a later day
        seg3 = (
            self.home
            / ".codex"
            / "sessions"
            / "2026"
            / "11"
            / "23"
            / f"rollout-2026-11-23T09-46-00-{sid}_{seg3_rollout}.jsonl"
        )
        write_jsonl(
            seg3,
            [
                {
                    "ordinal": 20,
                    "type": "session_meta",
                    "timestamp": "2026-11-23T01:46:00Z",
                    "payload": {"id": sid},
                },
                {
                    "ordinal": 21,
                    "type": "response_item",
                    "timestamp": "2026-11-23T01:46:01Z",
                    "payload": {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "Segment 3 latest reply"}],
                    },
                },
            ],
        )
        # Cross-day child subagent with nested source.subagent.thread_spawn.parent_thread_id and >16KB header
        child = (
            self.home
            / ".codex"
            / "sessions"
            / "2026"
            / "11"
            / "24"
            / f"rollout-2026-11-24T01-00-00-{child_sid}.jsonl"
        )
        write_jsonl(
            child,
            [
                {
                    "ordinal": 0,
                    "type": "session_meta",
                    "timestamp": "2026-11-23T17:00:00Z",
                    "payload": {
                        "id": child_sid,
                        "base_instructions": "Y" * 24000,
                        "source": {"subagent": {"thread_spawn": {"parent_thread_id": sid}}},
                    },
                },
                {
                    "ordinal": 1,
                    "type": "response_item",
                    "timestamp": "2026-11-23T17:00:01Z",
                    "payload": {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "Cross-day subagent reply"}],
                    },
                },
            ],
        )

        res = mod.read_session(f"codex:{sid}", view="timeline", full=True, home=self.home)
        paths = [Path(s["source_path"]) for s in res["sources"]]
        self.assertEqual(paths, [seg1, seg2, seg3, child])
        self.assertEqual(res["sources"][2]["events"][-1]["text"], "Segment 3 latest reply")
        self.assertEqual(res["sources"][3]["session_id"], child_sid)
        self.assertEqual(res["sources"][3]["parent"], sid)

        # Suffixed rollout ID must NOT resolve as an independent session ID
        with self.assertRaises(FileNotFoundError):
            mod.read_session(f"codex:{seg3_rollout}", view="timeline", home=self.home)

    def test_logical_session_list_find_and_preview_aggregate_codex_segments(self) -> None:
        if not shutil.which("rg"):
            self.skipTest("rg is required")

        sid = "21212121-2121-7121-8121-212121212121"
        r2 = "32323232-3232-7232-8232-323232323232"
        r3 = "43434343-4343-7343-8343-434343434343"

        seg1 = self.home / ".codex/sessions/2026/10/15" / f"rollout-2026-10-15T10-00-00-{sid}.jsonl"
        seg2 = self.home / ".codex/sessions/2026/11/01" / f"rollout-2026-11-01T10-00-00-{sid}_{r2}.jsonl"
        seg3 = self.home / ".codex/sessions/2026/11/23" / f"rollout-2026-11-23T09-46-00-{sid}_{r3}.jsonl"

        # seg1 has an opening screenshot (>256KB base64 payload) so preview must still recover the prompt
        big_img = "data:image/png;base64," + ("A" * 300000)
        write_jsonl(
            seg1,
            [
                {"ordinal": 0, "type": "session_meta", "timestamp": "2026-10-15T02:00:00Z", "payload": {"id": sid}},
                {
                    "ordinal": 1,
                    "type": "response_item",
                    "timestamp": "2026-10-15T02:00:01Z",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [
                            {"type": "input_image", "image_url": big_img},
                            {"type": "input_text", "text": "Initial prompt with EARLY_NEEDLE_ABC"},
                        ],
                    },
                },
            ],
        )
        write_jsonl(
            seg2,
            [
                {"ordinal": 10, "type": "session_meta", "timestamp": "2026-11-01T02:00:00Z", "payload": {"id": sid}},
                {
                    "ordinal": 11,
                    "type": "response_item",
                    "timestamp": "2026-11-01T02:00:01Z",
                    "payload": {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "Middle progress update"}],
                    },
                },
            ],
        )
        write_jsonl(
            seg3,
            [
                {"ordinal": 20, "type": "session_meta", "timestamp": "2026-11-23T01:46:00Z", "payload": {"id": sid}},
                {
                    "ordinal": 21,
                    "type": "response_item",
                    "timestamp": "2026-11-23T01:46:01Z",
                    "payload": {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "LATEST_TAIL_REPLY from segment 3"}],
                    },
                },
            ],
        )
        # seg1 is 30 days old, seg2 is 20 days old, seg3 is active today
        os.utime(seg1, (self.now - 30 * 86400, self.now - 30 * 86400))
        os.utime(seg2, (self.now - 20 * 86400, self.now - 20 * 86400))
        os.utime(seg3, (self.now - 60, self.now - 60))

        listed = mod.list_sessions(agents=["codex"], days=14, home=self.home, now=self.now)
        self.assertEqual(listed["count"], 1)
        entry = listed["sessions"][0]
        self.assertEqual(entry["session_id"], sid)
        self.assertEqual(entry["segment_count"], 3)
        self.assertEqual(entry["path"], str(seg3))
        self.assertEqual(entry["paths"], [str(seg1), str(seg2), str(seg3)])
        self.assertEqual(entry["size_bytes"], seg1.stat().st_size + seg2.stat().st_size + seg3.stat().st_size)
        self.assertIn("Initial prompt", entry["preview"])

        # find within 14 days must still search seg1 because the logical session is hot
        found = mod.find_sessions("EARLY_NEEDLE_ABC", agents=["codex"], days=14, home=self.home, now=self.now)
        self.assertEqual(found["count"], 1)
        self.assertEqual(found["hits"][0]["session_id"], sid)
        self.assertEqual(found["hits"][0]["path"], str(seg1))

        # render_preview must show BOTH the opening turn from seg1 and latest reply from seg3
        preview = mod.render_preview(kind="disk", agent="codex", session_id=sid, home=self.home)
        self.assertIn("Initial prompt", preview)
        self.assertIn("LATEST_TAIL_REPLY", preview)

    def test_codex_ordinal_rollback_pruning_and_consecutive_deep_rollback(self) -> None:
        sid = "54545454-5454-7454-8454-545454545454"
        r2 = "65656565-6565-7565-8565-656565656565"
        r3 = "76767676-7676-7676-8676-767676767676"

        seg1 = self.home / ".codex/sessions/2026/11/20" / f"rollout-2026-11-20T10-00-00-{sid}.jsonl"
        seg2 = self.home / ".codex/sessions/2026/11/20" / f"rollout-2026-11-20T10-05-00-{sid}_{r2}.jsonl"
        seg3 = self.home / ".codex/sessions/2026/11/20" / f"rollout-2026-11-20T10-06-00-{sid}_{r3}.jsonl"

        # Seg1: ord 0..100, where ord 90..100 will be superseded by seg3's deep rollback
        write_jsonl(
            seg1,
            [
                {"ordinal": 0, "type": "session_meta", "timestamp": "2026-11-20T02:00:00Z", "payload": {"id": sid}},
                {
                    "ordinal": 10,
                    "type": "response_item",
                    "timestamp": "2026-11-20T02:00:01Z",
                    "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Keep me"}]},
                },
                {
                    "ordinal": 90,
                    "type": "response_item",
                    "timestamp": "2026-11-20T02:04:00Z",
                    "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Rolled back 1"}]},
                },
                {
                    "ordinal": 95,
                    "type": "response_item",
                    "timestamp": "2026-11-20T02:04:30Z",
                    "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Rolled back 2"}]},
                },
            ],
        )
        # Seg2: created at 02:05 starting at ord 95 (will be completely superseded by seg3 starting at ord 90)
        write_jsonl(
            seg2,
            [
                {"ordinal": 95, "type": "session_meta", "timestamp": "2026-11-20T02:05:00Z", "payload": {"id": sid}},
                {
                    "ordinal": 96,
                    "type": "response_item",
                    "timestamp": "2026-11-20T02:05:01Z",
                    "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Ghost turn in seg2"}]},
                },
            ],
        )
        # Seg3: created at 02:06 rolling back deeper to ord 90
        write_jsonl(
            seg3,
            [
                {"ordinal": 90, "type": "session_meta", "timestamp": "2026-11-20T02:06:00Z", "payload": {"id": sid}},
                {
                    "ordinal": 91,
                    "type": "response_item",
                    "timestamp": "2026-11-20T02:06:01Z",
                    "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Surviving rewritten turn"}]},
                },
            ],
        )

        # Default timeline prunes rolled-back records
        res = mod.read_session(f"codex:{sid}", view="timeline", full=True, home=self.home)
        self.assertEqual([Path(s["source_path"]) for s in res["sources"]], [seg1, seg2, seg3])
        all_texts = [ev["text"] for s in res["sources"] for ev in s["events"]]
        self.assertEqual(all_texts, ["Keep me", "Surviving rewritten turn"])
        self.assertEqual(res["sources"][0]["superseded_from_ordinal"], 90)
        self.assertEqual(res["sources"][0]["superseded_records"], 2)
        self.assertEqual(res["sources"][1]["superseded_from_ordinal"], 90)
        self.assertEqual(res["sources"][1]["events"], [])

        # Opt-in --include-superseded keeps all historical rolled-back turns
        raw_res = mod.read_session(
            f"codex:{sid}", view="timeline", full=True, include_superseded=True, home=self.home
        )
        raw_texts = [ev["text"] for s in raw_res["sources"] for ev in s["events"]]
        self.assertIn("Ghost turn in seg2", raw_texts)
        self.assertIn("Rolled back 1", raw_texts)

        # Lossless export preserves all bytes and annotates ordinal metadata in manifest
        exp_dir = self.home / "export-rollback"
        exp = mod.read_session(f"codex:{sid}", view="export", output_dir=str(exp_dir), home=self.home)
        msrcs = exp["manifest"]["sources"]
        self.assertEqual(len(msrcs), 3)
        self.assertEqual((msrcs[0]["ordinal_start"], msrcs[0]["ordinal_end"], msrcs[0]["superseded_from_ordinal"]), (0, 95, 90))
        self.assertEqual((msrcs[1]["ordinal_start"], msrcs[1]["ordinal_end"], msrcs[1]["superseded_from_ordinal"]), (95, 96, 90))
        self.assertEqual((msrcs[2]["ordinal_start"], msrcs[2]["ordinal_end"]), (90, 91))
        self.assertNotIn("superseded_from_ordinal", msrcs[2])

    def test_pi_multi_uuid_filename_uses_header_session_id(self) -> None:
        pi_sid = "87878787-8787-7787-8787-878787878787"
        extra_uuid = "98989898-9898-7898-8898-989898989898"
        pip = self.home / ".pi/agent/sessions" / f"2026-11-23_{pi_sid}_{extra_uuid}.jsonl"
        write_jsonl(
            pip,
            [
                {"type": "session", "id": pi_sid, "timestamp": "2026-11-23T01:00:00Z"},
                {"type": "message", "message": {"role": "user", "content": "Pi header identity test"}},
            ],
        )
        res = mod.read_session(f"pi:{pi_sid}", view="timeline", home=self.home)
        self.assertEqual(res["sources"][0]["events"][0]["text"], "Pi header identity test")
        with self.assertRaises(FileNotFoundError):
            mod.read_session(f"pi:{extra_uuid}", view="timeline", home=self.home)

    def test_completeness_flags_sqlite_and_live_pane_missed_files_instead_of_silent_true(self) -> None:
        import sqlite3
        from unittest import mock

        sid = "91919191-9191-7191-8191-919191919191"
        seg1 = self.home / ".codex/sessions/2026/11/10" / f"rollout-2026-11-10T10-00-00-{sid}.jsonl"
        write_jsonl(
            seg1,
            [
                {"ordinal": 0, "type": "session_meta", "timestamp": "2026-11-10T02:00:00Z", "payload": {"id": sid}},
                {
                    "ordinal": 1,
                    "type": "response_item",
                    "timestamp": "2026-11-10T02:00:01Z",
                    "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Old turn"}]},
                },
            ],
        )

        # Simulate upstream CLI writing a new segment into an unrecognized directory
        untracked = self.home / ".codex/future_threads" / f"rollout-2026-11-23T09-46-00-{sid}.jsonl"
        write_jsonl(
            untracked,
            [
                {"ordinal": 10, "type": "session_meta", "timestamp": "2026-11-23T01:46:00Z", "payload": {"id": sid}},
                {
                    "ordinal": 11,
                    "type": "response_item",
                    "timestamp": "2026-11-23T01:46:01Z",
                    "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Latest from untracked dir"}]},
                },
            ],
        )

        # 1. state_5.sqlite pointing to untracked rollout triggers complete=False + warning + recovery read
        from contextlib import closing

        db_path = self.home / ".codex/state_5.sqlite"
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, rollout_path TEXT, updated_at INTEGER)")
            conn.execute("INSERT INTO threads VALUES (?, ?, ?)", (sid, str(untracked), int(self.now)))

        res = mod.read_session(f"codex:{sid}", view="timeline", full=True, home=self.home)
        self.assertFalse(res["complete"])
        self.assertTrue(any("sqlite_rollout_missed_by_discovery" in w for w in res["warnings"]))
        self.assertEqual(res["sources"][-1]["events"][-1]["text"], "Latest from untracked dir")

        # Export manifest also reflects complete=False and warnings
        exp_dir = self.home / "export-warn"
        exp = mod.read_session(f"codex:{sid}", view="export", output_dir=str(exp_dir), home=self.home)
        self.assertFalse(exp["complete"])
        self.assertFalse(exp["manifest"]["complete"])
        self.assertTrue(exp["manifest"]["warnings"])

        # Remove sqlite so we isolate live pane active_path detection next
        db_path.unlink()

        # 2. Live pane active_path outside static discovery triggers active_file_missed_by_discovery
        import live_sessions

        fake_pane = live_sessions.LivePaneSession(
            pane_id="%99",
            coord="0:3.1",
            session_name="0",
            window_id="@2",
            window_index="3",
            window_name="work",
            pane_index="1",
            pane_active=False,
            window_active=True,
            current_command="codex",
            cwd=str(self.home),
            pane_tty="/dev/ttys001",
            pane_title="Codex",
            agent="codex",
            agent_alias="codex",
            session_id=sid,
            title="Codex",
            authority="codex_lsof",
            active_path=str(untracked),
        )
        with (
            mock.patch.object(live_sessions, "list_live_panes", return_value=[fake_pane]),
            mock.patch.object(live_sessions, "resolve_pane_spec", return_value=fake_pane),
        ):
            pane_res = mod.read_session(pane="1", view="timeline", full=True, home=self.home, environment={"HOME": str(self.home)})
        self.assertFalse(pane_res["complete"])
        self.assertTrue(any("active_file_missed_by_discovery" in w for w in pane_res["warnings"]))
        self.assertEqual(pane_res["sources"][-1]["events"][-1]["text"], "Latest from untracked dir")

    def test_audit_session_storage_drift_detects_codex_pi_and_live_mismatches(self) -> None:
        from unittest import mock

        tests_dir = str(Path(__file__).resolve().parent)
        if tests_dir not in sys.path:
            sys.path.insert(0, tests_dir)
        from session_fixtures import seed_codex_multi_segment
        import live_sessions

        sid = "a1a1a1a1-a1a1-71a1-81a1-a1a1a1a1a1a1"
        other = "b2b2b2b2-b2b2-72b2-82b2-b2b2b2b2b2b2"
        seed_codex_multi_segment(self.home, sid, now=self.now, include_sqlite=True)

        # Clean state passes with zero issues
        clean = mod.audit_session_storage_drift(home=self.home, days=14, now=self.now)
        self.assertEqual(clean["issues"], [])
        self.assertEqual(clean["checked_codex_files"], 2)
        self.assertEqual(clean["checked_sqlite_rollouts"], 1)

        # Drift 1: single-UUID Codex file whose header disagrees with filename
        bad_codex = self.home / ".codex/sessions/2026/11/23" / f"rollout-2026-11-23T10-00-00-{sid}.jsonl"
        write_jsonl(bad_codex, [{"type": "session_meta", "payload": {"id": other}}])
        os.utime(bad_codex, (self.now, self.now))

        # Drift 2: single-UUID Pi file whose header disagrees with filename
        bad_pi = self.home / ".pi/agent/sessions" / f"2026-11-23_{sid}.jsonl"
        write_jsonl(bad_pi, [{"type": "session", "id": other}])
        os.utime(bad_pi, (self.now, self.now))

        # Drift 3: live pane writing to an uncovered file
        uncovered = self.home / ".codex/other_dir/live.jsonl"
        write_jsonl(uncovered, [{"type": "session_meta", "payload": {"id": sid}}])
        fake_pane = live_sessions.LivePaneSession(
            pane_id="%1",
            coord="0:1.1",
            session_name="0",
            window_id="@1",
            window_index="1",
            window_name="w",
            pane_index="1",
            pane_active=True,
            window_active=True,
            current_command="codex",
            cwd=str(self.home),
            pane_tty="/dev/ttys001",
            pane_title="Codex",
            agent="codex",
            agent_alias="codex",
            session_id=sid,
            title="Codex",
            authority="codex_lsof",
            active_path=str(uncovered),
        )
        with mock.patch.object(live_sessions, "list_live_panes", return_value=[fake_pane]):
            dirty = mod.audit_session_storage_drift(
                home=self.home,
                days=14,
                environment={"HOME": str(self.home)},
                check_live_panes=True,
                now=self.now,
            )
        codes = {issue["code"] for issue in dirty["issues"]}
        self.assertIn("codex_sid_drift", codes)
        self.assertIn("pi_sid_drift", codes)
        self.assertIn("live_pane_active_path_uncovered", codes)


if __name__ == "__main__":
    unittest.main()
