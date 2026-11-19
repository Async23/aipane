"""Unit tests for the zero-index local session lookup CLI (lib/agent_sessions.py)."""

from __future__ import annotations

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

        # Refuses to overwrite existing directory
        with self.assertRaises(FileExistsError):
            mod.read_session(dsh_id, agent="dsh", view="export", output_dir=str(out_dir), home=self.home)


if __name__ == "__main__":
    unittest.main()
