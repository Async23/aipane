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

    def test_p1_1_anchor_sid_conflict_and_pane_agent_conflict_rejected(self) -> None:
        from contextlib import closing
        import sqlite3
        from unittest import mock
        import live_sessions

        sid_a = "11111111-aaaa-7111-8111-111111111111"
        sid_b = "22222222-bbbb-7222-8222-222222222222"
        seg_a = self.home / ".codex/sessions/2026/11/20" / f"rollout-2026-11-20T10-00-00-{sid_a}.jsonl"
        seg_b = self.home / ".codex/sessions/2026/11/21" / f"rollout-2026-11-21T10-00-00-{sid_b}.jsonl"
        write_jsonl(
            seg_a,
            [
                {"ordinal": 0, "type": "session_meta", "timestamp": "2026-11-20T02:00:00Z", "payload": {"id": sid_a}},
                {"ordinal": 1, "type": "response_item", "timestamp": "2026-11-20T02:00:01Z", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Message from A"}]}},
            ],
        )
        write_jsonl(
            seg_b,
            [
                {"ordinal": 0, "type": "session_meta", "timestamp": "2026-11-21T02:00:00Z", "payload": {"id": sid_b}},
                {"ordinal": 1, "type": "response_item", "timestamp": "2026-11-21T02:00:01Z", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Foreign message from B"}]}},
            ],
        )

        # Corrupt SQLite row for A pointing at B (ordinal 0)
        db = self.home / ".codex/state_5.sqlite"
        with closing(sqlite3.connect(db)) as conn, conn:
            conn.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, rollout_path TEXT, updated_at INTEGER)")
            conn.execute("INSERT INTO threads VALUES (?, ?, ?)", (sid_a, str(seg_b), int(self.now)))

        res = mod.read_session(f"codex:{sid_a}", view="timeline", full=True, home=self.home)
        self.assertFalse(res["complete"])
        self.assertTrue(any("sqlite_rollout_sid_conflict" in w for w in res["warnings"]))
        texts = [ev["text"] for s in res["sources"] for ev in s["events"]]
        self.assertEqual(texts, ["Message from A"])
        self.assertNotIn("Foreign message from B", texts)

        # Export must also refuse to export B under A
        exp_dir = self.home / "export-conflict"
        exp = mod.read_session(f"codex:{sid_a}", view="export", output_dir=str(exp_dir), home=self.home)
        self.assertFalse(exp["complete"])
        self.assertEqual([Path(s["source"]["path"]) for s in exp["manifest"]["sources"]], [seg_a])

        # Conflicting --pane (codex) + --agent pi must raise ValueError instead of exporting Codex as Pi
        fake_pane = live_sessions.LivePaneSession(
            pane_id="%50",
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
            pane_tty="/dev/ttys050",
            pane_title="Codex",
            agent="codex",
            agent_alias="codex",
            session_id=sid_a,
            title="Codex",
            authority="codex_lsof",
            active_path=str(seg_a),
        )
        with (
            mock.patch.object(live_sessions, "list_live_panes", return_value=[fake_pane]),
            mock.patch.object(live_sessions, "resolve_pane_spec", return_value=fake_pane),
        ):
            with self.assertRaisesRegex(ValueError, "conflicting agent specifier"):
                mod.read_session(pane="1", agent="pi", view="export", output_dir=str(self.home / "exp-pi"), home=self.home)

    def test_p1_2_uncommitted_rollback_does_not_truncate_and_fork_inherits_history_base(self) -> None:
        from contextlib import closing
        import sqlite3

        # Part A: uncommitted rollback file B written before SQLite switches away from A
        sid = "33333333-aaaa-7333-8333-333333333333"
        rid_b = "44444444-bbbb-7444-8444-444444444444"
        seg_a = self.home / ".codex/sessions/2026/11/20" / f"rollout-2026-11-20T10-00-00-{sid}.jsonl"
        seg_b = self.home / ".codex/sessions/2026/11/20" / f"rollout-2026-11-20T10-05-00-{sid}_{rid_b}.jsonl"
        write_jsonl(
            seg_a,
            [
                {"ordinal": 0, "type": "session_meta", "timestamp": "2026-11-20T02:00:00Z", "payload": {"id": sid}},
                {"ordinal": 1, "type": "response_item", "timestamp": "2026-11-20T02:00:01Z", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Turn 1"}]}},
                {"ordinal": 5, "type": "response_item", "timestamp": "2026-11-20T02:00:05Z", "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Committed tail turn 5"}]}},
            ],
        )
        # Uncommitted rollback B (only header, ordinal 2) sits on disk while SQLite still points to A
        write_jsonl(
            seg_b,
            [
                {
                    "ordinal": 2,
                    "type": "session_meta",
                    "timestamp": "2026-11-20T02:05:00Z",
                    "payload": {"id": sid, "history_base": {"thread_id": sid, "end_ordinal_exclusive": 2}},
                },
            ],
        )
        db = self.home / ".codex/state_5.sqlite"
        with closing(sqlite3.connect(db)) as conn, conn:
            conn.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, rollout_path TEXT, updated_at INTEGER)")
            conn.execute("INSERT INTO threads VALUES (?, ?, ?)", (sid, str(seg_a), int(self.now)))

        res = mod.read_session(f"codex:{sid}", view="timeline", full=True, include_children=False, home=self.home)
        self.assertTrue(res["complete"])
        self.assertEqual([Path(s["source_path"]) for s in res["sources"]], [seg_a])
        self.assertEqual([ev["text"] for ev in res["sources"][0]["events"]], ["Turn 1", "Committed tail turn 5"])

        # Opt-in --include-superseded still surfaces the unlinked file
        raw_res = mod.read_session(f"codex:{sid}", view="timeline", full=True, include_children=False, include_superseded=True, home=self.home)
        self.assertEqual([Path(s["source_path"]) for s in raw_res["sources"]], [seg_a, seg_b])
        self.assertEqual(raw_res["sources"][1]["role"], "superseded")

        # Part B: forked thread inherits parent's bounded prefix via history_base
        parent_sid = "55555555-aaaa-7555-8555-555555555555"
        fork_sid = "66666666-bbbb-7666-8666-666666666666"
        parent_seg = self.home / ".codex/sessions/2026/11/19" / f"rollout-2026-11-19T10-00-00-{parent_sid}.jsonl"
        fork_seg = self.home / ".codex/sessions/2026/11/20" / f"rollout-2026-11-20T11-00-00-{fork_sid}.jsonl"
        p_lines = [
            json.dumps({"ordinal": 0, "type": "session_meta", "timestamp": "2026-11-19T02:00:00Z", "payload": {"id": parent_sid}}) + "\n",
            json.dumps({"ordinal": 1, "type": "response_item", "timestamp": "2026-11-19T02:00:01Z", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Shared parent prefix"}]}}) + "\n",
            json.dumps({"ordinal": 5, "type": "response_item", "timestamp": "2026-11-19T02:00:05Z", "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Parent post-fork turn"}]}}) + "\n",
        ]
        parent_seg.parent.mkdir(parents=True, exist_ok=True)
        parent_seg.write_text("".join(p_lines), encoding="utf-8")
        end_byte = len((p_lines[0] + p_lines[1]).encode("utf-8"))
        write_jsonl(
            fork_seg,
            [
                {
                    "ordinal": 5,
                    "type": "session_meta",
                    "timestamp": "2026-11-20T03:00:00Z",
                    "payload": {
                        "id": fork_sid,
                        "forked_from_id": parent_sid,
                        "history_base": {
                            "thread_id": parent_sid,
                            "end_ordinal_exclusive": 5,
                            "end_byte_offset": end_byte,
                        },
                    },
                },
                {"ordinal": 6, "type": "response_item", "timestamp": "2026-11-20T03:00:01Z", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Forked branch turn"}]}},
            ],
        )

        fork_res = mod.read_session(f"codex:{fork_sid}", view="timeline", full=True, include_children=False, home=self.home)
        self.assertTrue(fork_res["complete"])
        self.assertEqual([Path(s["source_path"]) for s in fork_res["sources"]], [parent_seg, fork_seg])
        self.assertEqual(fork_res["sources"][0]["role"], "inherited_prefix")
        self.assertEqual(fork_res["sources"][0]["end_byte_offset"], end_byte)
        self.assertEqual([ev["text"] for s in fork_res["sources"] for ev in s["events"]], ["Shared parent prefix", "Forked branch turn"])

    def test_p1_3_sqlite_only_recovery_missing_rollout_warning_and_child_sqlite_check(self) -> None:
        from contextlib import closing
        import sqlite3

        # Case 1: only log lives in unrecognized directory, SQLite recovers it
        sid1 = "77777777-aaaa-7777-8777-777777777777"
        novel = self.home / ".codex/future_threads" / f"rollout-2026-11-23T09-00-00-{sid1}.jsonl"
        write_jsonl(
            novel,
            [
                {"ordinal": 0, "type": "session_meta", "timestamp": "2026-11-23T01:00:00Z", "payload": {"id": sid1}},
                {"ordinal": 1, "type": "response_item", "timestamp": "2026-11-23T01:00:01Z", "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Recovered sole log"}]}},
            ],
        )

        # Case 2: SQLite points to missing newer rollout while older segment exists
        sid2 = "88888888-aaaa-7888-8888-888888888888"
        old2 = self.home / ".codex/sessions/2026/11/10" / f"rollout-2026-11-10T10-00-00-{sid2}.jsonl"
        write_jsonl(
            old2,
            [
                {"ordinal": 0, "type": "session_meta", "timestamp": "2026-11-10T02:00:00Z", "payload": {"id": sid2}},
                {"ordinal": 1, "type": "response_item", "timestamp": "2026-11-10T02:00:01Z", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Old segment 2"}]}},
            ],
        )
        missing_latest = self.home / ".codex/sessions/2026/11/23" / f"rollout-2026-11-23T10-00-00-{sid2}_99999999-9999-7999-8999-999999999999.jsonl"

        # Case 3: child session's newer segment is only reachable via SQLite when reading parent
        parent_sid = "99999999-aaaa-7999-8999-999999999999"
        child_sid = "aaaaaaaa-bbbb-7aaa-8aaa-aaaaaaaaaaaa"
        child_rid2 = "bbbbbbbb-cccc-7bbb-8bbb-bbbbbbbbbbbb"
        p_seg = self.home / ".codex/sessions/2026/11/20" / f"rollout-2026-11-20T10-00-00-{parent_sid}.jsonl"
        c_seg1 = self.home / ".codex/sessions/2026/11/20" / f"rollout-2026-11-20T10-01-00-{child_sid}.jsonl"
        c_seg2 = self.home / ".codex/future_threads" / f"rollout-2026-11-20T10-05-00-{child_sid}_{child_rid2}.jsonl"
        write_jsonl(p_seg, [{"ordinal": 0, "type": "session_meta", "timestamp": "2026-11-20T02:00:00Z", "payload": {"id": parent_sid}}])
        write_jsonl(
            c_seg1,
            [
                {"ordinal": 0, "type": "session_meta", "timestamp": "2026-11-20T02:01:00Z", "payload": {"id": child_sid, "source": {"subagent": {"thread_spawn": {"parent_thread_id": parent_sid}}}}},
                {"ordinal": 1, "type": "response_item", "timestamp": "2026-11-20T02:01:01Z", "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Child old turn"}]}},
            ],
        )
        write_jsonl(
            c_seg2,
            [
                {"ordinal": 5, "type": "session_meta", "timestamp": "2026-11-20T02:05:00Z", "payload": {"id": child_sid, "history_base": {"thread_id": child_sid, "end_ordinal_exclusive": 5}}},
                {"ordinal": 6, "type": "response_item", "timestamp": "2026-11-20T02:05:01Z", "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Child SQLite-recovered turn"}]}},
            ],
        )

        db = self.home / ".codex/state_5.sqlite"
        with closing(sqlite3.connect(db)) as conn, conn:
            conn.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, rollout_path TEXT, updated_at INTEGER)")
            conn.executemany(
                "INSERT INTO threads VALUES (?, ?, ?)",
                [
                    (sid1, str(novel), int(self.now)),
                    (sid2, str(missing_latest), int(self.now)),
                    (parent_sid, str(p_seg), int(self.now)),
                    (child_sid, str(c_seg2), int(self.now)),
                ],
            )

        res1 = mod.read_session(f"codex:{sid1}", view="timeline", full=True, home=self.home)
        self.assertFalse(res1["complete"])
        self.assertTrue(any("sqlite_rollout_missed_by_discovery" in w for w in res1["warnings"]))
        self.assertEqual(res1["sources"][0]["events"][0]["text"], "Recovered sole log")

        res2 = mod.read_session(f"codex:{sid2}", view="timeline", full=True, home=self.home)
        self.assertFalse(res2["complete"])
        self.assertTrue(any("sqlite_rollout_missing" in w for w in res2["warnings"]))

        res3 = mod.read_session(f"codex:{parent_sid}", view="timeline", full=True, include_children=True, home=self.home)
        self.assertFalse(res3["complete"])
        self.assertTrue(any("sqlite_rollout_missed_by_discovery" in w for w in res3["warnings"]))
        child_texts = [ev["text"] for s in res3["sources"] if s["session_id"] == child_sid for ev in s["events"]]
        self.assertEqual(child_texts, ["Child old turn", "Child SQLite-recovered turn"])

    def test_p1_5_codex_zst_compressed_prefix_discovered_deduped_and_audited(self) -> None:
        from contextlib import closing
        import sqlite3

        if not shutil.which("zstd"):
            self.skipTest("zstd is required")

        sid = "12121212-aaaa-7212-8212-121212121212"
        rid2 = "34343434-bbbb-7434-8434-343434343434"
        seg1_zst = self.home / ".codex/sessions/2026/11/10" / f"rollout-2026-11-10T10-00-00-{sid}.jsonl.zst"
        seg2 = self.home / ".codex/sessions/2026/11/23" / f"rollout-2026-11-23T10-00-00-{sid}_{rid2}.jsonl"
        seg1_records = [
            {"ordinal": 0, "type": "session_meta", "timestamp": "2026-11-10T02:00:00Z", "payload": {"id": sid}},
            {"ordinal": 1, "type": "response_item", "timestamp": "2026-11-10T02:00:01Z", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Compressed prefix turn"}]}},
        ]
        write_zstd_jsonl(seg1_zst, seg1_records)
        write_jsonl(
            seg2,
            [
                {"ordinal": 10, "type": "session_meta", "timestamp": "2026-11-23T02:00:00Z", "payload": {"id": sid, "history_base": {"thread_id": sid, "end_ordinal_exclusive": 10}}},
                {"ordinal": 11, "type": "response_item", "timestamp": "2026-11-23T02:00:01Z", "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Uncompressed tail turn"}]}},
            ],
        )
        db = self.home / ".codex/state_5.sqlite"
        with closing(sqlite3.connect(db)) as conn, conn:
            conn.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, rollout_path TEXT, updated_at INTEGER)")
            conn.execute("INSERT INTO threads VALUES (?, ?, ?)", (sid, str(seg2), int(self.now)))

        res = mod.read_session(f"codex:{sid}", view="timeline", full=True, include_children=False, home=self.home)
        self.assertTrue(res["complete"])
        self.assertEqual([ev["text"] for s in res["sources"] for ev in s["events"]], ["Compressed prefix turn", "Uncompressed tail turn"])

        # Doctor audit covers .jsonl.zst cleanly
        audit = mod.audit_session_storage_drift(home=self.home, days=14, now=self.now)
        self.assertEqual(audit["issues"], [])
        self.assertEqual(audit["checked_codex_files"], 2)

        # If both plain .jsonl and .jsonl.zst exist for the same rollout, deduplicate preferring plain .jsonl
        seg1_plain = seg1_zst.with_name(f"rollout-2026-11-10T10-00-00-{sid}.jsonl")
        write_jsonl(seg1_plain, seg1_records)
        dedup_res = mod.read_session(f"codex:{sid}", view="timeline", full=True, include_children=False, home=self.home)
        self.assertEqual([Path(s["source_path"]) for s in dedup_res["sources"]], [seg1_plain, seg2])

    def test_p2_6_missing_timestamp_uses_filename_ts_and_legacy_full_reset_warns(self) -> None:
        # Part A: 0 -> 95 -> 90 where final segment omits session_meta.timestamp
        sid = "56565656-aaaa-7656-8656-565656565656"
        r2 = "67676767-bbbb-7767-8767-676767676767"
        r3 = "78787878-cccc-7878-8878-787878787878"
        s1 = self.home / ".codex/sessions/2026/11/20" / f"rollout-2026-11-20T10-00-00-{sid}.jsonl"
        s2 = self.home / ".codex/sessions/2026/11/20" / f"rollout-2026-11-20T10-05-00-{sid}_{r2}.jsonl"
        s3 = self.home / ".codex/sessions/2026/11/20" / f"rollout-2026-11-20T10-06-00-{sid}_{r3}.jsonl"
        write_jsonl(s1, [{"ordinal": 0, "type": "session_meta", "timestamp": "2026-11-20T02:00:00Z", "payload": {"id": sid}}, {"ordinal": 10, "type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Base"}]}}])
        write_jsonl(s2, [{"ordinal": 95, "type": "session_meta", "timestamp": "2026-11-20T02:05:00Z", "payload": {"id": sid}}, {"ordinal": 96, "type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Superseded 95"}]}}])
        # Final segment omits timestamp completely — filename timestamp 10-06-00 must preserve causal order!
        write_jsonl(s3, [{"ordinal": 90, "type": "session_meta", "payload": {"id": sid}}, {"ordinal": 91, "type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Surviving 90"}]}}])

        res = mod.read_session(f"codex:{sid}", view="timeline", full=True, include_children=False, home=self.home)
        self.assertEqual([Path(s["source_path"]) for s in res["sources"]], [s1, s2, s3])
        self.assertEqual([ev["text"] for s in res["sources"] for ev in s["events"]], ["Base", "Surviving 90"])

        # Part B: legacy ordinal-less segment + later segment restarting at ordinal 0 emits ambiguity warning
        sid_leg = "89898989-aaaa-7989-8989-898989898989"
        r_reset = "90909090-bbbb-7090-8090-909090909090"
        leg1 = self.home / ".codex/sessions/2026/11/10" / f"rollout-2026-11-10T10-00-00-{sid_leg}.jsonl"
        leg2 = self.home / ".codex/sessions/2026/11/20" / f"rollout-2026-11-20T10-00-00-{sid_leg}_{r_reset}.jsonl"
        write_jsonl(leg1, [{"type": "session_meta", "timestamp": "2026-11-10T02:00:00Z", "payload": {"id": sid_leg}}, {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Legacy turn"}]}}])
        write_jsonl(leg2, [{"ordinal": 0, "type": "session_meta", "timestamp": "2026-11-20T02:00:00Z", "payload": {"id": sid_leg}}, {"ordinal": 1, "type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Reset turn"}]}}])

        leg_res = mod.read_session(f"codex:{sid_leg}", view="timeline", full=True, include_children=False, home=self.home)
        self.assertFalse(leg_res["complete"])
        self.assertTrue(any("ambiguous_rollback_boundary" in w for w in leg_res["warnings"]))
        self.assertEqual([ev["text"] for s in leg_res["sources"] for ev in s["events"]], ["Legacy turn", "Reset turn"])

    def test_p2_7_cross_day_child_in_earlier_date_dir_not_pruned(self) -> None:
        parent_sid = "13131313-aaaa-7313-8313-131313131313"
        child_sid = "14141414-bbbb-7414-8414-141414141414"
        parent = self.home / ".codex/sessions/2026/11/23" / f"rollout-2026-11-23T00-05-00-{parent_sid}.jsonl"
        # Child stored under earlier local date directory 2026/11/22
        child = self.home / ".codex/sessions/2026/11/22" / f"rollout-2026-11-22T23-59-00-{child_sid}.jsonl"
        write_jsonl(parent, [{"ordinal": 0, "type": "session_meta", "timestamp": "2026-11-22T16:05:00Z", "payload": {"id": parent_sid}}])
        write_jsonl(
            child,
            [
                {"ordinal": 0, "type": "session_meta", "timestamp": "2026-11-22T16:06:00Z", "payload": {"id": child_sid, "source": {"subagent": {"thread_spawn": {"parent_thread_id": parent_sid}}}}},
                {"ordinal": 1, "type": "response_item", "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Child in earlier date dir"}]}},
            ],
        )
        res = mod.read_session(f"codex:{parent_sid}", view="timeline", full=True, include_children=True, home=self.home)
        self.assertEqual([s["session_id"] for s in res["sources"]], [parent_sid, child_sid])

    def test_p2_8_doctor_independent_checks_large_header_malformed_lines_and_cli_json(self) -> None:
        import io
        from contextlib import redirect_stdout
        import codex_sessions

        # 1. Valid 540KB header works across read, session_exists, and doctor
        sid_ok = "15151515-aaaa-7515-8515-151515151515"
        ok_path = self.home / ".codex/sessions/2026/11/23" / f"rollout-2026-11-23T10-00-00-{sid_ok}.jsonl"
        write_jsonl(
            ok_path,
            [
                {"ordinal": 0, "type": "session_meta", "timestamp": "2026-11-23T02:00:00Z", "payload": {"id": sid_ok, "base_instructions": "B" * 540_000}},
                {"ordinal": 1, "type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "After 540KB header"}]}},
            ],
        )
        self.assertTrue(codex_sessions.session_exists(sid_ok, self.home / ".codex"))
        res_ok = mod.read_session(f"codex:{sid_ok}", view="timeline", full=True, include_children=False, home=self.home)
        self.assertTrue(res_ok["complete"])
        self.assertEqual(res_ok["sources"][0]["events"][0]["text"], "After 540KB header")
        self.assertEqual(mod.audit_session_storage_drift(home=self.home, days=14, now=self.now)["issues"], [])

        # 2. Truncated trailing JSONL line + non-dict response_item.payload
        sid_bad = "16161616-aaaa-7616-8616-161616161616"
        bad_path = self.home / ".codex/sessions/2026/11/23" / f"rollout-2026-11-23T10-02-00-{sid_bad}.jsonl"
        bad_path.parent.mkdir(parents=True, exist_ok=True)
        bad_path.write_text(
            json.dumps({"ordinal": 0, "type": "session_meta", "timestamp": "2026-11-23T02:02:00Z", "payload": {"id": sid_bad}})
            + "\n"
            + json.dumps({"ordinal": 1, "type": "response_item", "payload": "unexpected-string-payload"})
            + "\n"
            + json.dumps({"ordinal": 2, "type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Valid message"}]}})
            + "\n"
            + '{"ordinal": 3, "type": "response_item", "payload": {"truncated": ',
            encoding="utf-8",
        )
        listed = mod.list_sessions(agents=["codex"], days=14, home=self.home, now=self.now)
        self.assertTrue(any(s["session_id"] == sid_bad and s["preview"] == "Valid message" for s in listed["sessions"]))
        res_bad = mod.read_session(f"codex:{sid_bad}", view="timeline", full=True, include_children=False, home=self.home)
        self.assertFalse(res_bad["complete"])
        self.assertTrue(any("malformed_record_lines" in w for w in res_bad["warnings"]))
        self.assertEqual(res_bad["sources"][0]["events"][0]["text"], "Valid message")

        # 3. Oversized >4MB header marks read incomplete AND fails doctor
        sid_huge = "17171717-aaaa-7717-8717-171717171717"
        huge_path = self.home / ".codex/sessions/2026/11/23" / f"rollout-2026-11-23T10-03-00-{sid_huge}.jsonl"
        huge_path.write_text(
            json.dumps({"ordinal": 0, "type": "session_meta", "payload": {"id": sid_huge, "blob": "X" * (4 * 1024 * 1024 + 512)}}) + "\n",
            encoding="utf-8",
        )
        res_huge = mod.read_session(f"codex:{sid_huge}", view="timeline", full=True, include_children=False, home=self.home)
        self.assertFalse(res_huge["complete"])
        self.assertTrue(any("codex_header_header_too_large" in w for w in res_huge["warnings"]))
        audit = mod.audit_session_storage_drift(home=self.home, days=14, now=self.now)
        self.assertIn("codex_header_header_too_large", {i["code"] for i in audit["issues"]})

        # 4. CLI main() returns valid JSON error payload even on unexpected exceptions
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = mod.main(["read", "codex:00000000-0000-0000-0000-000000000000"])
        self.assertEqual(code, 1)
        err_obj = json.loads(buf.getvalue())
        self.assertIn("error", err_obj)

    def test_p2_10_pi_discovery_excludes_uncompressed_dsh_in_overlapping_dir(self) -> None:
        sid = "18181818-aaaa-7818-8818-181818181818"
        shared = self.home / "shared-agent-root"
        dsh_log = shared / "sessions" / "--tmp--" / sid / "session.v4.jsonl"
        write_jsonl(
            dsh_log,
            [
                {"type": "session", "version": 4, "id": sid, "createdAt": 1791446825469},
                {"type": "user/message", "time": 1791446826000, "data": {"source": {"kind": "user"}, "content": [{"type": "text", "text": "Uncompressed DSH turn"}]}},
            ],
        )
        env = {"HOME": str(self.home), "DSH_HOME": str(shared), "PI_CODING_AGENT_DIR": str(shared)}
        self.assertEqual(mod.discover_agent_files("pi", home=self.home, environment=env), [])
        res = mod.read_session(sid, view="timeline", home=self.home, environment=env)
        self.assertEqual(res["sources"][0]["agent"], "dsh")
        self.assertEqual(res["sources"][0]["events"][0]["text"], "Uncompressed DSH turn")

    def test_p2_11_symlink_dedup_and_concurrent_unlink_resilience(self) -> None:
        from unittest import mock

        if not shutil.which("rg"):
            self.skipTest("rg is required")

        sid = "19191919-aaaa-7919-8919-191919191919"
        other_sid = "20202020-bbbb-7020-8020-202020202020"
        seg = self.home / ".codex/sessions/2026/11/23" / f"rollout-2026-11-23T10-00-00-{sid}.jsonl"
        write_jsonl(
            seg,
            [
                {"ordinal": 0, "type": "session_meta", "timestamp": "2026-11-23T02:00:00Z", "payload": {"id": sid}},
                {"ordinal": 1, "type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "SYMLINK_DEDUP_TOKEN"}]}},
            ],
        )
        arch_dir = self.home / ".codex/archived_sessions"
        arch_dir.mkdir(parents=True, exist_ok=True)
        (arch_dir / seg.name).symlink_to(seg)

        listed = mod.list_sessions(agents=["codex"], days=14, home=self.home, now=self.now)
        self.assertEqual(listed["count"], 1)
        self.assertEqual(listed["sessions"][0]["segment_count"], 1)
        self.assertEqual(listed["sessions"][0]["size_bytes"], seg.stat().st_size)

        found = mod.find_sessions("SYMLINK_DEDUP_TOKEN", agents=["codex"], days=14, all_matches_per_session=True, home=self.home, now=self.now)
        self.assertEqual(found["count"], 1)

        # Unrelated file vanishing between enumeration and stat() must not fail reading target SID
        vanishing = self.home / ".codex/sessions/2026/11/23" / f"rollout-2026-11-23T09-00-00-{other_sid}.jsonl"
        write_jsonl(vanishing, [{"type": "session_meta", "payload": {"id": other_sid}}])
        real_stat = Path.stat

        def flaky_stat(path_self: Path, *args, **kwargs):
            if path_self.name == vanishing.name:
                raise FileNotFoundError(str(path_self))
            return real_stat(path_self, *args, **kwargs)

        with mock.patch.object(Path, "stat", flaky_stat):
            res = mod.read_session(f"codex:{sid}", view="timeline", full=True, include_children=False, home=self.home)
        self.assertTrue(res["complete"])
        self.assertEqual(res["sources"][0]["events"][0]["text"], "SYMLINK_DEDUP_TOKEN")

    def test_p2_12_export_preserves_crlf_bytes_and_cleans_up_on_failure(self) -> None:
        from unittest import mock

        sid = "21212121-aaaa-7121-8121-212121212121"
        rid2 = "22222222-cccc-7222-8222-222222222222"
        seg1 = self.home / ".codex/sessions/2026/11/20" / f"rollout-2026-11-20T10-00-00-{sid}.jsonl"
        seg2 = self.home / ".codex/sessions/2026/11/21" / f"rollout-2026-11-21T10-00-00-{sid}_{rid2}.jsonl"
        seg1.parent.mkdir(parents=True, exist_ok=True)
        seg2.parent.mkdir(parents=True, exist_ok=True)
        crlf_bytes = (
            json.dumps({"ordinal": 0, "type": "session_meta", "timestamp": "2026-11-20T02:00:00Z", "payload": {"id": sid}})
            + "\r\n"
            + json.dumps({"ordinal": 1, "type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "CRLF line"}]}})
            + "\r\n"
        ).encode("utf-8")
        seg1.write_bytes(crlf_bytes)
        write_jsonl(seg2, [{"ordinal": 10, "type": "session_meta", "timestamp": "2026-11-21T02:00:00Z", "payload": {"id": sid}}])

        out_dir = self.home / "export-crlf"
        # Simulate mid-export failure on seg2 -> output_dir must not remain and retry must succeed
        real_iter = mod.iter_raw_bytes_lines

        def fail_on_seg2(p: Path):
            if p == seg2:
                raise OSError("simulated disappear during export")
            return real_iter(p)

        with mock.patch.object(mod, "iter_raw_bytes_lines", side_effect=fail_on_seg2):
            with self.assertRaises(OSError):
                mod.read_session(f"codex:{sid}", view="export", output_dir=str(out_dir), home=self.home)
        self.assertFalse(out_dir.exists())
        self.assertEqual(list(self.home.glob(".tmp-export-crlf-*")), [])

        # Retry with the exact same output_dir succeeds and preserves exact CRLF bytes + sha256
        exp = mod.read_session(f"codex:{sid}", view="export", output_dir=str(out_dir), home=self.home)
        src1 = exp["manifest"]["sources"][0]
        exported_bytes = Path(src1["exported"]).read_bytes()
        self.assertEqual(exported_bytes, crlf_bytes)
        self.assertEqual(src1["bytes"], len(crlf_bytes))
        self.assertEqual(src1["sha256"], hashlib.sha256(crlf_bytes).hexdigest())

    def test_p2_13_cold_header_skip_and_single_read_per_segment(self) -> None:
        from unittest import mock
        import codex_sessions

        # Seed 20 cold double-UUID Codex sessions (each 2 segments) + 1 hot session (2 segments)
        for i in range(20):
            csid = f"300000{i:02d}-aaaa-7000-8000-3000000000{i:02d}"
            crid = f"400000{i:02d}-bbbb-7000-8000-4000000000{i:02d}"
            p1 = self.home / ".codex/sessions/2026/10/01" / f"rollout-2026-10-01T10-00-{i:02d}-{csid}.jsonl"
            p2 = self.home / ".codex/sessions/2026/10/02" / f"rollout-2026-10-02T10-00-{i:02d}-{csid}_{crid}.jsonl"
            write_jsonl(p1, [{"ordinal": 0, "type": "session_meta", "timestamp": "2026-10-01T02:00:00Z", "payload": {"id": csid}}])
            write_jsonl(p2, [{"ordinal": 5, "type": "session_meta", "timestamp": "2026-10-02T02:00:00Z", "payload": {"id": csid}}])
            cold_ts = self.now - 30 * 86400
            os.utime(p1, (cold_ts, cold_ts))
            os.utime(p2, (cold_ts, cold_ts))

        hot_sid = "50000001-aaaa-7000-8000-500000000001"
        hot_rid = "60000001-bbbb-7000-8000-600000000001"
        h1 = self.home / ".codex/sessions/2026/11/23" / f"rollout-2026-11-23T09-00-00-{hot_sid}.jsonl"
        h2 = self.home / ".codex/sessions/2026/11/23" / f"rollout-2026-11-23T10-00-00-{hot_sid}_{hot_rid}.jsonl"
        write_jsonl(h1, [{"ordinal": 0, "type": "session_meta", "timestamp": "2026-11-23T01:00:00Z", "payload": {"id": hot_sid}}, {"ordinal": 1, "type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Hot prompt"}]}}])
        write_jsonl(h2, [{"ordinal": 5, "type": "session_meta", "timestamp": "2026-11-23T02:00:00Z", "payload": {"id": hot_sid, "history_base": {"thread_id": hot_sid, "end_ordinal_exclusive": 5}}}])

        read_paths: list[Path] = []
        real_read = codex_sessions.read_rollout_meta_detailed

        def spy_read(path: Path, *args, **kwargs):
            read_paths.append(path.resolve())
            return real_read(path, *args, **kwargs)

        with mock.patch.object(codex_sessions, "read_rollout_meta_detailed", side_effect=spy_read):
            listed = mod.list_sessions(agents=["codex"], days=1, limit=1, home=self.home, now=self.now)
        self.assertEqual(listed["count"], 1)
        self.assertEqual(listed["sessions"][0]["session_id"], hot_sid)
        # ZERO cold files may be opened
        self.assertTrue(all(p in {h1.resolve(), h2.resolve()} for p in read_paths), f"opened cold files: {read_paths}")

        # read --no-children reads each segment header at most once
        read_paths.clear()
        with mock.patch.object(codex_sessions, "read_rollout_meta_detailed", side_effect=spy_read):
            res = mod.read_session(f"codex:{hot_sid}", view="timeline", include_children=False, home=self.home)
        self.assertTrue(res["complete"])
        self.assertEqual(read_paths.count(h1.resolve()), 1)
        self.assertEqual(read_paths.count(h2.resolve()), 1)

    def test_p2_14_rollback_preview_metadata_only_tail_large_file_and_find_ranking(self) -> None:
        from unittest import mock

        if not shutil.which("rg"):
            self.skipTest("rg is required")

        # 1. Full rollback updates list.preview to post-rollback prompt
        rb_sid = "71717171-aaaa-7171-8171-717171717171"
        rb_rid = "72727272-bbbb-7272-8272-727272727272"
        rb1 = self.home / ".codex/sessions/2026/11/20" / f"rollout-2026-11-20T10-00-00-{rb_sid}.jsonl"
        rb2 = self.home / ".codex/sessions/2026/11/20" / f"rollout-2026-11-20T10-05-00-{rb_sid}_{rb_rid}.jsonl"
        write_jsonl(rb1, [{"ordinal": 0, "type": "session_meta", "timestamp": "2026-11-20T02:00:00Z", "payload": {"id": rb_sid}}, {"ordinal": 1, "type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "RETRACTED_OPENING_PROMPT"}]}}])
        write_jsonl(rb2, [{"ordinal": 1, "type": "session_meta", "timestamp": "2026-11-20T02:05:00Z", "payload": {"id": rb_sid, "history_base": {"thread_id": rb_sid, "end_ordinal_exclusive": 1}}}, {"ordinal": 2, "type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "SURVIVING_OPENING_PROMPT"}]}}])
        listed = mod.list_sessions(agents=["codex"], days=14, home=self.home, now=self.now)
        rb_item = next(s for s in listed["sessions"] if s["session_id"] == rb_sid)
        self.assertEqual(rb_item["preview"], "SURVIVING_OPENING_PROMPT")

        # 2. 3-segment session where trailing 2 segments have only metadata: preview still shows all 6 turns from seg1
        meta_sid = "73737373-aaaa-7373-8373-737373737373"
        m_r2 = "74747474-bbbb-7474-8474-747474747474"
        m_r3 = "75757575-cccc-7575-8575-757575757575"
        m1 = self.home / ".codex/sessions/2026/11/20" / f"rollout-2026-11-20T10-00-00-{meta_sid}.jsonl"
        m2 = self.home / ".codex/sessions/2026/11/21" / f"rollout-2026-11-21T10-00-00-{meta_sid}_{m_r2}.jsonl"
        m3 = self.home / ".codex/sessions/2026/11/22" / f"rollout-2026-11-22T10-00-00-{meta_sid}_{m_r3}.jsonl"
        m1_recs = [{"ordinal": 0, "type": "session_meta", "timestamp": "2026-11-20T02:00:00Z", "payload": {"id": meta_sid}}]
        for i in range(1, 7):
            m1_recs.append({"ordinal": i, "type": "response_item", "payload": {"type": "message", "role": "user" if i % 2 else "assistant", "content": [{"type": "input_text" if i % 2 else "output_text", "text": f"SEG1_TURN_{i}"}]}})
        write_jsonl(m1, m1_recs)
        write_jsonl(m2, [{"ordinal": 10, "type": "session_meta", "timestamp": "2026-11-21T02:00:00Z", "payload": {"id": meta_sid, "history_base": {"thread_id": meta_sid, "end_ordinal_exclusive": 10}}}])
        write_jsonl(m3, [{"ordinal": 20, "type": "session_meta", "timestamp": "2026-11-22T02:00:00Z", "payload": {"id": meta_sid, "history_base": {"thread_id": m_r2, "end_ordinal_exclusive": 20}}}])
        prev_meta = mod.render_preview(kind="disk", agent="codex", session_id=meta_sid, home=self.home)
        for i in range(1, 7):
            self.assertIn(f"SEG1_TURN_{i}", prev_meta)

        # 3. Single huge file (12,000 middle tool-call lines) uses bounded head/tail without full timeline parse
        big_sid = "76767676-aaaa-7676-8676-767676767676"
        big_p = self.home / ".codex/sessions/2026/11/23" / f"rollout-2026-11-23T10-00-00-{big_sid}.jsonl"
        big_p.parent.mkdir(parents=True, exist_ok=True)
        with big_p.open("w", encoding="utf-8") as fh:
            fh.write(json.dumps({"ordinal": 0, "type": "session_meta", "timestamp": "2026-11-23T02:00:00Z", "payload": {"id": big_sid}}) + "\n")
            for i in range(1, 4):
                fh.write(json.dumps({"ordinal": i, "type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": f"BIG_HEAD_{i}"}]}}) + "\n")
            filler = json.dumps({"ordinal": 10, "type": "response_item", "payload": {"type": "function_call", "name": "noop", "arguments": "X" * 80}}) + "\n"
            for _ in range(12_000):
                fh.write(filler)
            for i in range(1, 6):
                fh.write(json.dumps({"ordinal": 20000 + i, "type": "response_item", "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": f"BIG_TAIL_{i}"}]}}) + "\n")

        with mock.patch.object(mod, "_parse_timeline_events_with_stats", side_effect=AssertionError("must not full-scan large file")):
            prev_big = mod.render_preview(kind="disk", agent="codex", session_id=big_sid, home=self.home)
        self.assertIn("BIG_HEAD_1", prev_big)
        self.assertIn("BIG_TAIL_5", prev_big)

        # 4. find --limit 1 ranks active session A (hit in old seg1) ahead of stale session B
        a_sid = "77777777-cccc-7777-8777-777777777777"
        a_r2 = "78787878-dddd-7878-8878-787878787878"
        b_sid = "79797979-eeee-7979-8979-797979797979"
        a1 = self.home / ".codex/sessions/2026/11/10" / f"rollout-2026-11-10T10-00-00-{a_sid}.jsonl"
        a2 = self.home / ".codex/sessions/2026/11/23" / f"rollout-2026-11-23T11-00-00-{a_sid}_{a_r2}.jsonl"
        b1 = self.home / ".codex/sessions/2026/11/20" / f"rollout-2026-11-20T10-00-00-{b_sid}.jsonl"
        write_jsonl(a1, [{"ordinal": 0, "type": "session_meta", "timestamp": "2026-11-10T02:00:00Z", "payload": {"id": a_sid}}, {"ordinal": 1, "type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "RANK_NEEDLE_999"}]}}])
        write_jsonl(a2, [{"ordinal": 5, "type": "session_meta", "timestamp": "2026-11-23T03:00:00Z", "payload": {"id": a_sid}}])
        write_jsonl(b1, [{"ordinal": 0, "type": "session_meta", "timestamp": "2026-11-20T02:00:00Z", "payload": {"id": b_sid}}, {"ordinal": 1, "type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "RANK_NEEDLE_999"}]}}])
        os.utime(a1, (self.now - 10 * 86400, self.now - 10 * 86400))
        os.utime(a2, (self.now - 30, self.now - 30))
        os.utime(b1, (self.now - 3 * 86400, self.now - 3 * 86400))

        found_top = mod.find_sessions("RANK_NEEDLE_999", agents=["codex"], days=14, limit=1, home=self.home, now=self.now)
        self.assertEqual(found_top["count"], 1)
        self.assertEqual(found_top["hits"][0]["session_id"], a_sid)
        self.assertEqual(found_top["hits"][0]["path"], str(a1))

    def test_r2_p1_3_os_replace_same_path_caught_before_sqlite_and_during_open_stream(self) -> None:
        from contextlib import closing
        import sqlite3
        from unittest import mock
        import codex_sessions

        sid_a = "81818181-aaaa-7181-8181-818181818181"
        sid_b = "82828282-bbbb-7282-8282-828282828282"
        p = self.home / ".codex/sessions/2026/11/23" / f"rollout-2026-11-23T10-00-00-{sid_a}.jsonl"
        wrong = self.home / "staging" / f"rollout-2026-11-23T10-00-00-{sid_b}.jsonl"
        write_jsonl(p, [{"ordinal": 0, "type": "session_meta", "timestamp": "2026-11-23T02:00:00Z", "payload": {"id": sid_a}}, {"ordinal": 1, "type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Original A"}]}}])
        write_jsonl(wrong, [{"ordinal": 0, "type": "session_meta", "timestamp": "2026-11-23T02:00:00Z", "payload": {"id": sid_b}}, {"ordinal": 1, "type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "FOREIGN_B_CONTENT"}]}}])
        with closing(sqlite3.connect(self.home / ".codex/state_5.sqlite")) as db, db:
            db.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, rollout_path TEXT, updated_at INTEGER)")
            db.execute("INSERT INTO threads VALUES (?, ?, ?)", (sid_a, str(p), int(self.now)))

        # 1. Atomic os.replace between static discovery and SQLite verification must NOT return B
        orig_query = codex_sessions.query_sqlite_rollout_path_detailed

        def swap_before_sqlite(*args, **kwargs):
            os.replace(wrong, p)
            return orig_query(*args, **kwargs)

        with mock.patch.object(codex_sessions, "query_sqlite_rollout_path_detailed", side_effect=swap_before_sqlite):
            with self.assertRaisesRegex(FileNotFoundError, "sqlite_rollout_sid_conflict"):
                mod.read_session(f"codex:{sid_a}", view="timeline", include_children=False, home=self.home)

        # 2. Atomic os.replace right before open-stream parse/export must ALSO reject B
        write_jsonl(p, [{"ordinal": 0, "type": "session_meta", "timestamp": "2026-11-23T02:00:00Z", "payload": {"id": sid_a}}, {"ordinal": 1, "type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Original A"}]}}])
        write_jsonl(wrong, [{"ordinal": 0, "type": "session_meta", "timestamp": "2026-11-23T02:00:00Z", "payload": {"id": sid_b}}, {"ordinal": 1, "type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "FOREIGN_B_CONTENT"}]}}])
        real_iter = mod.iter_raw_bytes_lines

        def swap_before_open(path: Path):
            if path == p and wrong.exists():
                os.replace(wrong, p)
            return real_iter(path)

        with mock.patch.object(mod, "iter_raw_bytes_lines", side_effect=swap_before_open):
            tl = mod.read_session(f"codex:{sid_a}", view="timeline", include_children=False, home=self.home)
        self.assertFalse(tl["complete"])
        self.assertTrue(any("source_sid_changed_during_read" in w for w in tl["warnings"]))
        self.assertEqual(tl["sources"], [])

    def test_r2_p1_4_fork_cutoff_truncation_midline_and_include_superseded_isolation(self) -> None:
        parent_sid = "83838383-aaaa-7383-8383-838383838383"
        fork_sid = "84848484-bbbb-7484-8484-848484848484"
        pp = self.home / ".codex/sessions/2026/11/20" / f"rollout-2026-11-20T10-00-00-{parent_sid}.jsonl"
        fc = self.home / ".codex/sessions/2026/11/21" / f"rollout-2026-11-21T10-00-00-{fork_sid}.jsonl"
        lines = [
            json.dumps({"ordinal": 0, "type": "session_meta", "timestamp": "2026-11-20T02:00:00Z", "payload": {"id": parent_sid}}) + "\n",
            json.dumps({"ordinal": 1, "type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "SHARED_PREFIX"}]}}) + "\n",
            json.dumps({"ordinal": 2, "type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "PARENT_AFTER_FORK"}]}}) + "\n",
        ]
        pp.parent.mkdir(parents=True, exist_ok=True)
        pp.write_text("".join(lines), encoding="utf-8")
        exact_cutoff = len((lines[0] + lines[1]).encode("utf-8"))
        write_jsonl(
            fc,
            [
                {"ordinal": 2, "type": "session_meta", "timestamp": "2026-11-21T02:00:00Z", "payload": {"id": fork_sid, "forked_from_id": parent_sid, "history_base": {"thread_id": parent_sid, "end_ordinal_exclusive": 2, "end_byte_offset": exact_cutoff}}},
                {"ordinal": 3, "type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "CHILD_TURN"}]}},
            ],
        )

        # 1. --include-superseded on fork MUST NOT leak PARENT_AFTER_FORK
        sup = mod.read_session(f"codex:{fork_sid}", view="timeline", full=True, include_children=False, include_superseded=True, home=self.home)
        self.assertTrue(sup["complete"])
        self.assertEqual([ev["text"] for s in sup["sources"] for ev in s["events"]], ["SHARED_PREFIX", "CHILD_TURN"])

        # 2. Mid-line byte offset excludes straddling line and marks complete=False
        mid_cutoff = len(lines[0].encode("utf-8")) + 10
        write_jsonl(
            fc,
            [
                {"ordinal": 2, "type": "session_meta", "timestamp": "2026-11-21T02:00:00Z", "payload": {"id": fork_sid, "forked_from_id": parent_sid, "history_base": {"thread_id": parent_sid, "end_ordinal_exclusive": 2, "end_byte_offset": mid_cutoff}}},
                {"ordinal": 3, "type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "CHILD_TURN"}]}},
            ],
        )
        mid = mod.read_session(f"codex:{fork_sid}", view="timeline", full=True, include_children=False, home=self.home)
        self.assertFalse(mid["complete"])
        self.assertTrue(any("history_base_byte_boundary_misaligned" in w for w in mid["warnings"]))
        self.assertEqual([ev["text"] for s in mid["sources"] for ev in s["events"]], ["CHILD_TURN"])

        # 3. Parent truncated at complete JSONL boundary shorter than exact_cutoff marks complete=False in timeline & export
        write_jsonl(
            fc,
            [
                {"ordinal": 2, "type": "session_meta", "timestamp": "2026-11-21T02:00:00Z", "payload": {"id": fork_sid, "forked_from_id": parent_sid, "history_base": {"thread_id": parent_sid, "end_ordinal_exclusive": 2, "end_byte_offset": exact_cutoff}}},
                {"ordinal": 3, "type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "CHILD_TURN"}]}},
            ],
        )
        pp.write_text(lines[0], encoding="utf-8")
        trunc = mod.read_session(f"codex:{fork_sid}", view="timeline", full=True, include_children=False, home=self.home)
        self.assertFalse(trunc["complete"])
        self.assertTrue(any("history_base_parent_truncated" in w for w in trunc["warnings"]))
        exp = mod.read_session(f"codex:{fork_sid}", view="export", include_children=False, output_dir=str(self.home / "exp-trunc"), home=self.home)
        self.assertFalse(exp["complete"])
        self.assertFalse(exp["manifest"]["complete"])

    def test_r2_p1_5_child_continuations_sqlite_parent_children_and_3level_fork_dedup(self) -> None:
        from contextlib import closing
        import sqlite3

        # 1. Parent with child whose seg2 omits parent_thread_id + 3-level fork A -> B -> C dedup
        sid_a = "85858585-aaaa-7585-8585-858585858585"
        sid_b = "86868686-bbbb-7686-8686-868686868686"
        rid_b2 = "87878787-cccc-7787-8787-878787878787"
        sid_c = "88888888-dddd-7888-8888-888888888888"

        pa = self.home / ".codex/sessions/2026/11/20" / f"rollout-2026-11-20T10-00-00-{sid_a}.jsonl"
        pb1 = self.home / ".codex/sessions/2026/11/21" / f"rollout-2026-11-21T10-00-00-{sid_b}.jsonl"
        pb2 = self.home / ".codex/sessions/2026/11/22" / f"rollout-2026-11-22T10-00-00-{sid_b}_{rid_b2}.jsonl"
        pc = self.home / ".codex/sessions/2026/11/23" / f"rollout-2026-11-23T10-00-00-{sid_c}.jsonl"
        write_jsonl(pa, [{"ordinal": 0, "type": "session_meta", "timestamp": "2026-11-20T02:00:00Z", "payload": {"id": sid_a}}, {"ordinal": 1, "type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "A_TURN"}]}}])
        write_jsonl(pb1, [{"ordinal": 2, "type": "session_meta", "timestamp": "2026-11-21T02:00:00Z", "payload": {"id": sid_b, "forked_from_id": sid_a, "history_base": {"thread_id": sid_a, "end_ordinal_exclusive": 2, "end_byte_offset": pa.stat().st_size}}}, {"ordinal": 3, "type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "B1_TURN"}]}}])
        write_jsonl(pb2, [{"ordinal": 4, "type": "session_meta", "timestamp": "2026-11-22T02:00:00Z", "payload": {"id": sid_b, "history_base": {"thread_id": sid_b, "end_ordinal_exclusive": 4, "end_byte_offset": pb1.stat().st_size}}}, {"ordinal": 5, "type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "B2_CONTINUATION"}]}}])
        write_jsonl(pc, [{"ordinal": 6, "type": "session_meta", "timestamp": "2026-11-23T02:00:00Z", "payload": {"id": sid_c, "forked_from_id": sid_b, "history_base": {"thread_id": rid_b2, "end_ordinal_exclusive": 6, "end_byte_offset": pb2.stat().st_size}}}, {"ordinal": 7, "type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "C_GRANDCHILD"}]}}])

        res = mod.read_session(f"codex:{sid_a}", view="timeline", full=True, include_children=True, home=self.home)
        self.assertTrue(res["complete"])
        self.assertEqual([ev["text"] for s in res["sources"] for ev in s["events"]], ["A_TURN", "B1_TURN", "B2_CONTINUATION", "C_GRANDCHILD"])
        # No source may declare itself as its own parent, and physical files are not duplicated
        self.assertEqual(len(res["sources"]), 4)
        for s in res["sources"]:
            self.assertNotEqual(s["session_id"], s["parent"])

        exp = mod.read_session(f"codex:{sid_a}", view="export", include_children=True, output_dir=str(self.home / "exp-3level"), home=self.home)
        self.assertEqual(len(exp["manifest"]["sources"]), 4)
        self.assertTrue(exp["manifest"]["sources"][0].get("inherited_ranges"))

        # 2. SQLite-only parent in future_threads/ still discovers child in sessions/
        p_only = "89898989-aaaa-7989-8989-898989898989"
        c_std = "90909090-bbbb-7090-8090-909090909090"
        pf = self.home / ".codex/future_threads" / f"rollout-2026-11-23T10-00-00-{p_only}.jsonl"
        cs = self.home / ".codex/sessions/2026/11/23" / f"rollout-2026-11-23T10-05-00-{c_std}.jsonl"
        write_jsonl(pf, [{"ordinal": 0, "type": "session_meta", "timestamp": "2026-11-23T02:00:00Z", "payload": {"id": p_only}}, {"ordinal": 1, "type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "SQLITE_PARENT"}]}}])
        write_jsonl(cs, [{"ordinal": 0, "type": "session_meta", "timestamp": "2026-11-23T02:05:00Z", "payload": {"id": c_std, "source": {"subagent": {"thread_spawn": {"parent_thread_id": p_only}}}}}, {"ordinal": 1, "type": "response_item", "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "STD_CHILD"}]}}])
        with closing(sqlite3.connect(self.home / ".codex/state_5.sqlite")) as db, db:
            db.execute("CREATE TABLE IF NOT EXISTS threads (id TEXT PRIMARY KEY, rollout_path TEXT, updated_at INTEGER)")
            db.execute("INSERT OR REPLACE INTO threads VALUES (?, ?, ?)", (p_only, str(pf), int(self.now)))
        res_sq = mod.read_session(f"codex:{p_only}", view="timeline", full=True, include_children=True, home=self.home)
        self.assertEqual([ev["text"] for s in res_sq["sources"] for ev in s["events"]], ["SQLITE_PARENT", "STD_CHILD"])

    def test_r2_p1_6_mixed_local_filename_and_utc_meta_timestamps_order_correctly(self) -> None:
        sid = "91919191-aaaa-7191-8191-919191919191"
        r2 = "92929292-bbbb-7292-8292-929292929292"
        r3 = "93939393-cccc-7393-8393-939393939393"
        # Local UTC+8 filenames 10:00, 10:05, 10:06; UTC headers 02:00Z and 02:06Z; middle omits header timestamp
        s1 = self.home / ".codex/sessions/2026/11/23" / f"rollout-2026-11-23T10-00-00-{sid}.jsonl"
        s2 = self.home / ".codex/sessions/2026/11/23" / f"rollout-2026-11-23T10-05-00-{sid}_{r2}.jsonl"
        s3 = self.home / ".codex/sessions/2026/11/23" / f"rollout-2026-11-23T10-06-00-{sid}_{r3}.jsonl"
        write_jsonl(s1, [{"ordinal": 0, "type": "session_meta", "timestamp": "2026-11-23T02:00:00Z", "payload": {"id": sid}}, {"ordinal": 1, "type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "base"}]}}])
        write_jsonl(s2, [{"ordinal": 95, "type": "session_meta", "payload": {"id": sid}}, {"ordinal": 96, "type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "obsolete95"}]}}])
        write_jsonl(s3, [{"ordinal": 90, "type": "session_meta", "timestamp": "2026-11-23T02:06:00Z", "payload": {"id": sid}}, {"ordinal": 91, "type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "current90"}]}}])

        res = mod.read_session(f"codex:{sid}", view="timeline", full=True, include_children=False, home=self.home)
        self.assertTrue(res["complete"])
        self.assertEqual([ev["text"] for s in res["sources"] for ev in s["events"]], ["base", "current90"])

    def test_r2_p1_7_and_p2_6_plain_over_sqlite_zst_archived_dedup_and_jsonl_symlink_to_zst(self) -> None:
        from contextlib import closing
        import sqlite3

        if not shutil.which("zstd"):
            self.skipTest("zstd is required")

        sid = "94949494-aaaa-7494-8494-949494949494"
        plain = self.home / ".codex/sessions/2026/11/23" / f"rollout-2026-11-23T10-00-00-{sid}.jsonl"
        zst = Path(str(plain) + ".zst")
        old_recs = [
            {"ordinal": 0, "type": "session_meta", "timestamp": "2026-11-23T02:00:00Z", "payload": {"id": sid}},
            {"ordinal": 1, "type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "OLD_TURN"}]}},
        ]
        write_zstd_jsonl(zst, old_recs)
        write_jsonl(plain, [*old_recs, {"ordinal": 2, "type": "response_item", "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "PLAIN_NEWER_TURN"}]}}])

        # SQLite points to existing .zst while newer plain .jsonl exists -> plain .jsonl MUST win
        with closing(sqlite3.connect(self.home / ".codex/state_5.sqlite")) as db, db:
            db.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, rollout_path TEXT, updated_at INTEGER)")
            db.execute("INSERT INTO threads VALUES (?, ?, ?)", (sid, str(zst), int(self.now)))

        res = mod.read_session(f"codex:{sid}", view="timeline", full=True, include_children=False, home=self.home)
        self.assertTrue(res["complete"])
        self.assertFalse(res["sources"][0]["source_path"].endswith(".zst"))
        self.assertEqual([ev["text"] for s in res["sources"] for ev in s["events"]], ["OLD_TURN", "PLAIN_NEWER_TURN"])

        # Cross-directory plain (sessions/) vs archived .zst (archived_sessions/) dedupes to 1 segment
        arch_zst = self.home / ".codex/archived_sessions" / zst.name
        write_zstd_jsonl(arch_zst, old_recs)
        zst.unlink()
        listed = mod.list_sessions(agents=["codex"], scan_all=True, home=self.home)
        self.assertEqual(listed["sessions"][0]["segment_count"], 1)

        # Symlink named .jsonl pointing to .jsonl.zst is detected as zstd and readable
        sid_sym = "95959595-bbbb-7595-8595-959595959595"
        real_zst = self.home / ".codex/archived_sessions" / f"rollout-2026-11-23T11-00-00-{sid_sym}.jsonl.zst"
        write_zstd_jsonl(
            real_zst,
            [
                {"ordinal": 0, "type": "session_meta", "timestamp": "2026-11-23T03:00:00Z", "payload": {"id": sid_sym}},
                {"ordinal": 1, "type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "SYMLINK_ZST_TURN"}]}},
            ],
        )
        compat_link = self.home / ".codex/sessions/2026/11/23" / f"rollout-2026-11-23T11-00-00-{sid_sym}.jsonl"
        compat_link.symlink_to(real_zst)
        res_sym = mod.read_session(f"codex:{sid_sym}", view="timeline", full=True, include_children=False, home=self.home)
        self.assertTrue(res_sym["complete"])
        self.assertEqual([ev["text"] for s in res_sym["sources"] for ev in s["events"]], ["SYMLINK_ZST_TURN"])

        # Also test a standalone .jsonl symlink pointing to an out-of-tree .jsonl.zst (no archived_sessions copy)
        sid_sym2 = "95959595-cccc-7595-8595-959595959595"
        ext_zst = self.home / "external_store" / f"rollout-2026-11-23T11-05-00-{sid_sym2}.jsonl.zst"
        write_zstd_jsonl(
            ext_zst,
            [
                {"ordinal": 0, "type": "session_meta", "timestamp": "2026-11-23T03:05:00Z", "payload": {"id": sid_sym2}},
                {"ordinal": 1, "type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "STANDALONE_SYMLINK_ZST"}]}},
            ],
        )
        solo_link = self.home / ".codex/sessions/2026/11/23" / f"rollout-2026-11-23T11-05-00-{sid_sym2}.jsonl"
        solo_link.symlink_to(ext_zst)
        res_solo = mod.read_session(f"codex:{sid_sym2}", view="timeline", full=True, include_children=False, home=self.home)
        self.assertTrue(res_solo["complete"])
        self.assertEqual([ev["text"] for s in res_solo["sources"] for ev in s["events"]], ["STANDALONE_SYMLINK_ZST"])

    def test_r2_p2_2_to_p2_5_export_malformed_query_consistency_preview_and_scan_cost(self) -> None:
        from collections import Counter
        import io
        from contextlib import redirect_stderr
        from unittest import mock
        import codex_sessions

        # 1. Export flags malformed trailing line consistently with timeline
        sid_m = "96969696-aaaa-7696-8696-969696969696"
        pm = self.home / ".codex/sessions/2026/11/23" / f"rollout-2026-11-23T10-00-00-{sid_m}.jsonl"
        pm.parent.mkdir(parents=True, exist_ok=True)
        pm.write_text(
            json.dumps({"ordinal": 0, "type": "session_meta", "timestamp": "2026-11-23T02:00:00Z", "payload": {"id": sid_m}}) + "\n"
            + json.dumps({"ordinal": 1, "type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "GOOD"}]}}) + "\n"
            + '{"unfinished":',
            encoding="utf-8",
        )
        exp_m = mod.read_session(f"codex:{sid_m}", view="export", include_children=False, output_dir=str(self.home / "exp-bad-tail"), home=self.home)
        self.assertFalse(exp_m["complete"])
        self.assertFalse(exp_m["manifest"]["complete"])
        self.assertTrue(any("malformed_record_lines" in w for w in exp_m["manifest"]["warnings"]))

        # 2. Non-standard filename works identically with and without --no-children
        sid_ns = "97979797-aaaa-7797-8797-979797979797"
        pns = self.home / ".codex/sessions/2026/11/23/custom_log_name.jsonl"
        write_jsonl(pns, [{"ordinal": 0, "type": "session_meta", "timestamp": "2026-11-23T02:00:00Z", "payload": {"id": sid_ns}}, {"ordinal": 1, "type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "NONSTANDARD_TURN"}]}}])
        r_with = mod.read_session(f"codex:{sid_ns}", view="timeline", include_children=True, home=self.home)
        r_no = mod.read_session(f"codex:{sid_ns}", view="timeline", include_children=False, home=self.home)
        self.assertEqual(r_with["sources"][0]["events"][0]["text"], "NONSTANDARD_TURN")
        self.assertEqual(r_no["sources"][0]["events"][0]["text"], "NONSTANDARD_TURN")

        # 3. Rolled-back sole prompt -> list.preview is empty (not retracted prompt)
        sid_rb = "98989898-aaaa-7898-8898-989898989898"
        rb_rid = "99999999-bbbb-7999-8999-999999999999"
        rb1 = self.home / ".codex/sessions/2026/11/20" / f"rollout-2026-11-20T10-00-00-{sid_rb}.jsonl"
        rb2 = self.home / ".codex/sessions/2026/11/21" / f"rollout-2026-11-21T10-00-00-{sid_rb}_{rb_rid}.jsonl"
        write_jsonl(rb1, [{"ordinal": 0, "type": "session_meta", "timestamp": "2026-11-20T02:00:00Z", "payload": {"id": sid_rb}}, {"ordinal": 1, "type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "RETRACTED_SOLE"}]}}])
        write_jsonl(rb2, [{"ordinal": 0, "type": "session_meta", "timestamp": "2026-11-21T02:00:00Z", "payload": {"id": sid_rb}}])
        listed_rb = mod.list_sessions(agents=["codex"], scan_all=True, home=self.home)
        self.assertEqual(next(s for s in listed_rb["sessions"] if s["session_id"] == sid_rb)["preview"], "")

        # 4. 5 MiB trailing metadata line -> render_preview still recovers last 5 turns
        sid_big = "a1a1a1a1-cccc-71a1-81a1-a1a1a1a1a1a1"
        pbig = self.home / ".codex/sessions/2026/11/23" / f"rollout-2026-11-23T12-00-00-{sid_big}.jsonl"
        with pbig.open("w", encoding="utf-8") as fh:
            fh.write(json.dumps({"ordinal": 0, "type": "session_meta", "timestamp": "2026-11-23T04:00:00Z", "payload": {"id": sid_big}}) + "\n")
            for i in range(1, 21):
                fh.write(json.dumps({"ordinal": i, "type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": f"MT_TURN_{i:02d}"}]}}) + "\n")
            fh.write(json.dumps({"type": "event_msg", "payload": {"type": "token_count", "blob": "x" * (5 * 1024 * 1024)}}) + "\n")
        prev_big = mod.render_preview(kind="disk", agent="codex", session_id=sid_big, home=self.home)
        self.assertIn("MT_TURN_01", prev_big)
        self.assertIn("MT_TURN_20", prev_big)

        # 5. Legacy Kimi bare role record works in render_preview
        kimi_sid = "session_a2a2a2a2-aaaa-42a2-82a2-a2a2a2a2a2a2"
        kp = self.home / ".kimi-code/sessions" / kimi_sid / "wire.jsonl"
        write_jsonl(kp, [{"role": "user", "content": "KIMI_LEGACY_PREVIEW"}])
        prev_k = mod.render_preview(kind="disk", agent="kimi", session_id=kimi_sid, home=self.home)
        self.assertIn("KIMI_LEGACY_PREVIEW", prev_k)

        # 6. 10-stage fork does not trigger per-hop rglob storms, and lazy list --limit 1 opens <=2 headers
        fork_home = self.home / "scan-cost-home"
        ids = [f"b00000{i:02d}-aaaa-7000-8000-b000000000{i:02d}" for i in range(10)]
        prev_p = fork_home / ".codex/sessions/2026/11/01" / f"rollout-2026-11-01T10-00-00-{ids[0]}.jsonl"
        write_jsonl(prev_p, [{"ordinal": 0, "type": "session_meta", "timestamp": "2026-11-01T02:00:00Z", "payload": {"id": ids[0]}}, {"ordinal": 1, "type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "ROOT"}]}}])
        for j in range(1, len(ids)):
            cur_p = fork_home / f".codex/sessions/2026/11/{j+1:02d}" / f"rollout-2026-11-{j+1:02d}T10-00-00-{ids[j]}.jsonl"
            write_jsonl(cur_p, [{"ordinal": j * 2, "type": "session_meta", "timestamp": f"2026-11-{j+1:02d}T02:00:00Z", "payload": {"id": ids[j], "history_base": {"thread_id": ids[j - 1], "end_ordinal_exclusive": j * 2, "end_byte_offset": prev_p.stat().st_size}}}, {"ordinal": j * 2 + 1, "type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": f"HOP_{j}"}]}}])
            prev_p = cur_p
        rglob_calls = Counter()
        real_rglob = Path.rglob

        def spy_rglob(path_self: Path, pat: str, *args, **kwargs):
            rglob_calls[str(path_self)] += 1
            return real_rglob(path_self, pat, *args, **kwargs)

        with mock.patch.object(Path, "rglob", spy_rglob):
            fork10 = mod.read_session(f"codex:{ids[-1]}", view="timeline", include_children=False, home=fork_home)
        self.assertTrue(fork10["complete"])
        self.assertEqual(len(fork10["sources"]), 10)
        self.assertLessEqual(rglob_calls[str(fork_home / ".codex/sessions")], 2)

        # 7. Compressed DSH preview uses bounded JSON parsing and produces zero stderr warnings
        if shutil.which("zstd"):
            dsh_sid = "c3c3c3c3-aaaa-43c3-83c3-c3c3c3c3c3c3"
            dz = self.home / ".dsh/sessions" / dsh_sid / "session.v4.jsonl.zstd"
            dsh_rows = [{"type": "session", "id": dsh_sid, "version": 4, "createdAt": 100}]
            dsh_rows.extend({"type": "assistant/message", "data": {"content": f"DSH_T_{i}"}} for i in range(2000))
            write_zstd_jsonl(dz, dsh_rows)
            err_buf = io.StringIO()
            with redirect_stderr(err_buf), mock.patch.object(mod.json, "loads", wraps=mod.json.loads) as jloads:
                prev_dsh = mod.render_preview(kind="disk", agent="dsh", session_id=dsh_sid, home=self.home)
            self.assertIn("DSH_T_1999", prev_dsh)
            self.assertLess(jloads.call_count, 300)
            self.assertNotIn("zstd decode failed", err_buf.getvalue())


if __name__ == "__main__":
    unittest.main()
