"""Zero-index local session discovery, search, and lossless export for 7 AI CLIs.

Supported agents (aligned with aipane/integrations):
  claude, codex, dsh, grok, pi, kimi, antigravity (alias: agy)

Primitives:
  list  - Metadata / recency listing pruned by agent + mtime window
  find  - Content search via rg -z on pruned file sets (snippet-windowed)
  read  - Self-contained exact session lookup, child traversal, timeline & export
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Mapping

_LIB_DIR = str(Path(__file__).resolve().parent)
if _LIB_DIR not in sys.path:
    sys.path.insert(0, _LIB_DIR)

import codex_sessions  # noqa: E402

ALL_AGENTS = ("claude", "codex", "dsh", "grok", "pi", "kimi", "antigravity")
AGENT_ALIASES = {
    "agy": "antigravity",
    "antigravity-cli": "antigravity",
    "antigravity-ide": "antigravity",
    "claude-code": "claude",
    "claude_code": "claude",
    "dsh-tui": "dsh",
    "kimi-code": "kimi",
    "pi_agent": "pi",
    "pi-agent": "pi",
}
UUID_RE = re.compile(
    r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})"
)
DSH_GEN_RE = re.compile(r"^session(?:\.v(\d+))?\.jsonl(?:\.zstd)?$")
DATA_IMAGE_RE = re.compile(r"^data:(image/[a-zA-Z0-9.+-]+);base64,(.+)$", re.S)
ATTACHMENT_HINT_TOKENS = (
    "data:image/",
    '"base64"',
    '"mimeType"',
    '"mime_type"',
    '"media_type"',
    '"inlineData"',
    '"inline_data"',
    '"media"',
)
MIME_EXT_MAP = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/webp": "webp",
    "image/gif": "gif",
    "image/svg+xml": "svg",
    "image/avif": "avif",
    "image/heic": "heic",
    "image/bmp": "bmp",
}


@dataclass
class SessionFile:
    agent: str
    session_id: str
    path: Path
    mtime: float
    size_bytes: int
    is_subagent: bool = False
    parent_id: str | None = None


@dataclass
class LogicalSession:
    """A logical conversation aggregated across one or more physical storage files."""

    agent: str
    session_id: str
    segments: list[SessionFile]
    is_subagent: bool = False
    parent_id: str | None = None

    @property
    def mtime(self) -> float:
        return max(s.mtime for s in self.segments)

    @property
    def size_bytes(self) -> int:
        return sum(s.size_bytes for s in self.segments)

    @property
    def first_segment(self) -> SessionFile:
        return self.segments[0]

    @property
    def latest_segment(self) -> SessionFile:
        return self.segments[-1]


@dataclass
class _SourcePlan:
    sf: SessionFile
    role: str = "effective"  # "effective" | "inherited_prefix" | "superseded"
    end_ordinal_exclusive: int | None = None
    end_byte_offset: int | None = None
    inherited_ranges: list[dict[str, Any]] = field(default_factory=list)


def _path_cache_key(path: Path) -> Path:
    try:
        return path.resolve()
    except OSError:
        return path


def _meta_cache_key(path: Path) -> tuple[Path, int, int]:
    return codex_sessions.path_identity_key(path)


def _cached_codex_meta(
    path: Path,
    meta_cache: dict[Any, dict[str, Any] | None] | None,
) -> dict[str, Any] | None:
    if meta_cache is None:
        return codex_sessions.read_rollout_meta(path, max_lines=1)
    key = _meta_cache_key(path)
    if key not in meta_cache:
        meta_cache[key] = codex_sessions.read_rollout_meta(path, max_lines=1)
    return meta_cache[key]


def _dedup_session_files(files: list[SessionFile]) -> list[SessionFile]:
    """Deduplicate physical files by (dev, inode) so symlinks never double-count."""
    by_inode: dict[tuple[int, int], SessionFile] = {}

    def _priority(sf: SessionFile) -> tuple[int, int, int, str]:
        p = sf.path
        is_comp = 1 if codex_sessions.is_compressed_rollout(p) else 0
        is_sym = 1 if p.is_symlink() else 0
        is_arch = 1 if "archived_sessions" in p.parts else 0
        return (is_comp, is_sym, is_arch, str(p))

    for sf in files:
        try:
            st = sf.path.stat()
        except OSError:
            continue
        ikey = (st.st_dev, st.st_ino)
        prev = by_inode.get(ikey)
        if prev is None or _priority(sf) < _priority(prev):
            by_inode[ikey] = sf
    return list(by_inode.values())


def group_logical_sessions(
    files: list[SessionFile],
    *,
    sort_segments: bool = True,
    meta_cache: dict[Path, dict[str, Any] | None] | None = None,
) -> list[LogicalSession]:
    """Group physical storage files by logical (agent, session_id, is_subagent)."""
    deduped = _dedup_session_files(files)
    buckets: dict[tuple[str, str, bool], list[SessionFile]] = {}
    for sf in deduped:
        key = (sf.agent, sf.session_id, sf.is_subagent)
        buckets.setdefault(key, []).append(sf)

    sessions: list[LogicalSession] = []
    for (agent, sid, is_sub), segs in buckets.items():
        if len(segs) > 1:
            if sort_segments and agent == "codex":
                meta_pairs = [(_cached_codex_meta(sf.path, meta_cache), sf.path) for sf in segs]
                by_path = {sf.path: sf for sf in segs}
                segs = [by_path[p] for _, p in codex_sessions.sort_rollout_entries(meta_pairs)]
            else:
                segs = sorted(segs, key=lambda s: (s.mtime, str(s.path)))
        parent_id = next((s.parent_id for s in segs if s.parent_id), None)
        sessions.append(
            LogicalSession(
                agent=agent,
                session_id=sid,
                segments=segs,
                is_subagent=is_sub,
                parent_id=parent_id,
            )
        )
    return sessions


def normalize_agents(raw: list[str] | None) -> list[str]:
    if not raw:
        return list(ALL_AGENTS)
    out: list[str] = []
    for item in raw:
        for part in item.split(","):
            key = part.strip().lower()
            if not key:
                continue
            key = AGENT_ALIASES.get(key, key)
            if key not in ALL_AGENTS:
                raise ValueError(f"unsupported agent: {part!r}")
            if key not in out:
                out.append(key)
    return out


def iso_local(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts).astimezone().isoformat(timespec="seconds")


def _codex_home(
    home: Path | None = None,
    *,
    codex_home: Path | None = None,
    environment: Mapping[str, str] | None = None,
) -> Path:
    if codex_home is not None:
        return codex_home.expanduser()
    env = os.environ if environment is None else environment
    if env.get("CODEX_HOME"):
        return Path(env["CODEX_HOME"]).expanduser()
    h = home or Path(env.get("HOME") or str(Path.home()))
    return (h / ".codex").expanduser()


def agent_roots(
    agent: str,
    home: Path | None = None,
    *,
    codex_home: Path | None = None,
    environment: Mapping[str, str] | None = None,
) -> list[tuple[Path, dict[str, Any]]]:
    env = os.environ if environment is None else environment
    h = home or Path(env.get("HOME") or str(Path.home()))
    if agent == "codex":
        return [
            (root, {})
            for root in codex_sessions.codex_session_roots(
                _codex_home(home, codex_home=codex_home, environment=environment)
            )
        ]
    if agent == "claude":
        base = Path(env.get("CLAUDE_CONFIG_DIR") or (h / ".claude"))
        return [(base / "projects", {})]
    if agent == "antigravity":
        base = Path(env.get("GEMINI_HOME") or (h / ".gemini"))
        return [
            (base / "antigravity-cli" / "brain", {"is_ide": False}),
            (base / "antigravity" / "brain", {"is_ide": True}),
        ]
    if agent == "dsh":
        base = Path(env.get("DSH_HOME") or (h / ".dsh"))
        return [(base / "sessions", {})]
    if agent == "grok":
        base = Path(env.get("GROK_HOME") or (h / ".grok"))
        return [(base / "sessions", {})]
    if agent == "pi":
        base = Path(env.get("PI_CODING_AGENT_DIR") or (h / ".pi" / "agent"))
        return [(base / "sessions", {})]
    if agent == "kimi":
        roots: list[tuple[Path, dict[str, Any]]] = []
        seen: set[Path] = set()
        for env_key in ("KIMI_CODE_HOME", "KIMI_HOME"):
            val = env.get(env_key)
            if val:
                p = Path(val).expanduser() / "sessions"
                if p not in seen:
                    seen.add(p)
                    roots.append((p, {}))
        if not roots:
            for rel in (".kimi-code", ".kimi"):
                p = h / rel / "sessions"
                if p not in seen:
                    seen.add(p)
                    roots.append((p, {}))
        return roots
    return []


def _is_dsh_session_header(hdr: dict[str, Any] | None) -> bool:
    return bool(
        isinstance(hdr, dict)
        and hdr.get("type") == "session"
        and "createdAt" in hdr
        and "timestamp" not in hdr
    )


def discover_agent_files(
    agent: str,
    home: Path | None = None,
    *,
    codex_home: Path | None = None,
    environment: Mapping[str, str] | None = None,
    verify_headers: bool = True,
    meta_cache: dict[Path, dict[str, Any] | None] | None = None,
) -> list[SessionFile]:
    found: list[SessionFile] = []

    if agent == "codex":
        chome = _codex_home(home, codex_home=codex_home, environment=environment)
        for p in codex_sessions.iter_codex_rollout_files(chome):
            try:
                st = p.stat()
            except OSError:
                continue
            fn_sid, fn_rid = codex_sessions.extract_filename_ids(p)
            plain_stem = codex_sessions.rollout_plain_stem(p)
            if not verify_headers:
                sid = fn_sid or plain_stem
            elif fn_sid and fn_sid == fn_rid and plain_stem.startswith("rollout-"):
                sid = fn_sid
            else:
                meta = _cached_codex_meta(p, meta_cache)
                sid = meta["sid"] if meta else (fn_sid or plain_stem)
            found.append(SessionFile("codex", sid, p, st.st_mtime, st.st_size))

    elif agent == "claude":
        for root, _ in agent_roots("claude", home, environment=environment):
            if not root.is_dir():
                continue
            for p in root.rglob("*.jsonl"):
                try:
                    if not p.is_file():
                        continue
                    st = p.stat()
                except OSError:
                    continue
                is_sub = "subagents" in p.parts
                if is_sub:
                    idx = p.parts.index("subagents")
                    parent_sid = p.parts[idx - 1] if idx > 0 else None
                    sid = p.stem
                else:
                    parent_sid = None
                    sid = p.stem
                found.append(
                    SessionFile("claude", sid, p, st.st_mtime, st.st_size, is_sub, parent_sid)
                )

    elif agent == "antigravity":
        for root, meta in agent_roots("antigravity", home, environment=environment):
            if not root.is_dir():
                continue
            is_ide = bool(meta.get("is_ide"))
            try:
                entries = list(root.iterdir())
            except OSError:
                continue
            for entry in entries:
                if not entry.is_dir():
                    continue
                log = entry / ".system_generated" / "logs" / "transcript_full.jsonl"
                try:
                    if not log.is_file():
                        continue
                    st = log.stat()
                except OSError:
                    continue
                sid = f"ide/{entry.name}" if is_ide else entry.name
                found.append(
                    SessionFile("antigravity", sid, log, st.st_mtime, st.st_size)
                )

    elif agent == "dsh":
        for root, _ in agent_roots("dsh", home, environment=environment):
            if not root.is_dir():
                continue
            by_dir: dict[Path, list[tuple[int, int, Path]]] = {}
            for p in root.rglob("session*.jsonl*"):
                try:
                    if not p.is_file():
                        continue
                    st = p.stat()
                except OSError:
                    continue
                m = DSH_GEN_RE.match(p.name)
                if not m:
                    continue
                gen = int(m.group(1)) if m.group(1) else 0
                by_dir.setdefault(p.parent, []).append((gen, int(st.st_mtime), p))
            for sdir, candidates in by_dir.items():
                candidates.sort(key=lambda x: (x[0], x[1]), reverse=True)
                best = candidates[0][2]
                try:
                    st = best.stat()
                except OSError:
                    continue
                found.append(
                    SessionFile("dsh", sdir.name, best, st.st_mtime, st.st_size)
                )

    elif agent == "grok":
        for root, _ in agent_roots("grok", home, environment=environment):
            if not root.is_dir():
                continue
            for p in root.rglob("updates.jsonl"):
                try:
                    if not p.is_file():
                        continue
                    st = p.stat()
                except OSError:
                    continue
                sid = p.parent.name
                found.append(SessionFile("grok", sid, p, st.st_mtime, st.st_size))

    elif agent == "pi":
        for root, _ in agent_roots("pi", home, environment=environment):
            if not root.is_dir():
                continue
            for p in root.rglob("*.jsonl"):
                if DSH_GEN_RE.match(p.name):
                    continue
                try:
                    if not p.is_file():
                        continue
                    st = p.stat()
                except OSError:
                    continue
                m = UUID_RE.findall(p.stem)
                if not verify_headers:
                    sid = m[0] if m else p.stem
                elif len(m) == 1:
                    sid = m[0]
                else:
                    hdr = read_first_json(p)
                    if _is_dsh_session_header(hdr):
                        continue
                    hdr_id = hdr.get("id") if (hdr and hdr.get("type") == "session") else None
                    sid = hdr_id if isinstance(hdr_id, str) and hdr_id else (m[0] if m else p.stem)
                found.append(SessionFile("pi", sid, p, st.st_mtime, st.st_size))

    elif agent == "kimi":
        for root, _ in agent_roots("kimi", home, environment=environment):
            if not root.is_dir():
                continue
            for p in root.rglob("wire.jsonl"):
                try:
                    if not p.is_file():
                        continue
                    st = p.stat()
                except OSError:
                    continue
                is_sub = False
                parent_sid: str | None = None
                sid = p.parent.name
                # Kimi Code 2.x layout: .../session_<uuid>/agents/<agent>/wire.jsonl
                if len(p.parts) >= 4 and p.parents[1].name == "agents":
                    agent_slot = p.parent.name
                    session_dir_name = p.parents[2].name
                    if agent_slot == "main":
                        sid = session_dir_name
                    else:
                        is_sub = True
                        parent_sid = session_dir_name
                        sid = f"{session_dir_name}/{agent_slot}"
                else:
                    for part in reversed(p.parts):
                        if part.startswith("session_"):
                            sid = part
                            break
                        m = UUID_RE.search(part)
                        if m:
                            sid = m.group(1)
                            break
                found.append(
                    SessionFile("kimi", sid, p, st.st_mtime, st.st_size, is_sub, parent_sid)
                )

    return _dedup_session_files(found)


def prune_by_time(
    files: list[Any], days: int, scan_all: bool, now: float | None = None
) -> tuple[list[Any], int]:
    files.sort(key=lambda s: s.mtime, reverse=True)
    if scan_all or days <= 0:
        return files, 0
    cutoff = (now if now is not None else time.time()) - days * 86400
    hot = [s for s in files if s.mtime >= cutoff]
    skipped = len(files) - len(hot)
    return hot, skipped


def extract_text_from_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        t = content.get("text") or content.get("content")
        return t if isinstance(t, str) else ""
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                t = item.get("text") or item.get("content")
                if isinstance(t, str):
                    parts.append(t)
        return "\n".join(parts)
    return ""


def clean_preview(text: str, limit: int = 160) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > limit:
        return text[: limit - 1] + "…"
    return text


def is_noise_user_text(text: str) -> bool:
    s = text.lstrip()
    if not s:
        return True
    prefixes = (
        "# AGENTS.md",
        "<INSTRUCTIONS>",
        "<system-reminder>",
        "Current runtime context.",
        "<environment_context>",
        "<permissions instructions>",
    )
    return s.startswith(prefixes)


def zstd_binary() -> str:
    return shutil.which("zstd") or "/opt/homebrew/bin/zstd"


def _is_zstd_path(path: Path) -> bool:
    return codex_sessions.is_compressed_rollout(path)


def iter_head_lines(
    path: Path, max_lines: int = 80, max_bytes: int = 8 * 1024 * 1024
) -> Iterator[str]:
    """Stream up to `max_lines` / `max_bytes` from the head of a session log.

    Streaming line-by-line avoids over-decompressing `.zstd`/`.zst` logs or
    buffering full chunks on normal sessions while still tolerating opening
    turns that embed multi-hundred-KB base64 screenshots.
    """
    if _is_zstd_path(path):
        cap = min(max_bytes, 4 * 1024 * 1024)
        try:
            proc = subprocess.Popen(
                [zstd_binary(), "-dcqf", str(path)],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
        except Exception:
            return
        assert proc.stdout is not None
        read_bytes = 0
        try:
            for _ in range(max_lines):
                remaining = cap - read_bytes
                if remaining <= 0:
                    break
                raw = proc.stdout.readline(remaining + 1)
                if not raw:
                    break
                read_bytes += len(raw)
                if len(raw) > remaining:
                    break
                yield raw.decode("utf-8", errors="replace")
        except Exception:
            return
        finally:
            try:
                proc.stdout.close()
            except OSError:
                pass
            proc.terminate()
            try:
                proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        return
    try:
        read_bytes = 0
        with path.open("rb") as f:
            for _ in range(max_lines):
                remaining = max_bytes - read_bytes
                if remaining <= 0:
                    break
                raw = f.readline(remaining + 1)
                if not raw:
                    break
                read_bytes += len(raw)
                if len(raw) > remaining:
                    # Line exceeded remaining byte budget; stop to avoid truncated JSON.
                    break
                yield raw.decode("utf-8", errors="replace")
    except Exception:
        return


def read_head_lines(path: Path, max_lines: int = 80, max_bytes: int = 262144) -> list[str]:
    return [line.rstrip("\r\n") for line in iter_head_lines(path, max_lines=max_lines, max_bytes=max_bytes)]


def iter_raw_bytes_lines(path: Path) -> Iterator[bytes]:
    """Yield raw line bytes (preserving exact CRLF/LF line endings) for plain or zstd files."""
    if _is_zstd_path(path):
        proc = subprocess.Popen(
            [zstd_binary(), "-dcqf", str(path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        assert proc.stdout is not None
        completed = False
        try:
            for raw in proc.stdout:
                yield raw
            completed = True
        finally:
            try:
                proc.stdout.close()
            except OSError:
                pass
            if not completed:
                proc.terminate()
                try:
                    proc.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
                if proc.stderr:
                    try:
                        proc.stderr.close()
                    except OSError:
                        pass
            else:
                rc = proc.wait()
                stderr_bytes = proc.stderr.read() if proc.stderr else b""
                if proc.stderr:
                    proc.stderr.close()
                if rc != 0:
                    stderr = stderr_bytes.decode("utf-8", errors="replace").strip()
                    raise RuntimeError(f"zstd decode failed for {path}: {stderr}")
    else:
        with path.open("rb") as f:
            for raw in f:
                yield raw


def iter_decoded_lines(path: Path) -> Iterator[str]:
    for raw in iter_raw_bytes_lines(path):
        yield raw.decode("utf-8", errors="replace")


def extract_preview(
    sf: SessionFile,
    *,
    max_ordinal_exclusive: int | None = None,
    max_byte_offset_exclusive: int | None = None,
) -> str:
    if sf.agent == "grok":
        summary = sf.path.parent / "summary.json"
        if summary.is_file():
            try:
                d = json.loads(summary.read_text(errors="replace"))
                title = d.get("title") or d.get("first_user_message") or d.get("summary")
                if isinstance(title, str) and title.strip():
                    return clean_preview(title)
            except Exception:
                pass
    elif sf.agent == "kimi":
        sdir = (
            sf.path.parents[2]
            if len(sf.path.parts) >= 4 and sf.path.parents[1].name == "agents"
            else sf.path.parent
        )
        state = sdir / "state.json"
        if state.is_file():
            try:
                d = json.loads(state.read_text(errors="replace"))
                title = d.get("title") or d.get("lastPrompt")
                if isinstance(title, str) and title.strip():
                    return clean_preview(title)
            except Exception:
                pass

    byte_offset = 0
    for line in iter_head_lines(sf.path):
        line_bytes = len(line.encode("utf-8", errors="replace"))
        if max_byte_offset_exclusive is not None and byte_offset >= max_byte_offset_exclusive:
            break
        byte_offset += line_bytes
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except Exception:
            continue
        if not isinstance(rec, dict):
            continue
        if (
            sf.agent == "codex"
            and max_ordinal_exclusive is not None
            and isinstance(rec.get("ordinal"), int)
            and not isinstance(rec.get("ordinal"), bool)
            and rec["ordinal"] >= max_ordinal_exclusive
        ):
            continue

        text = ""
        if sf.agent == "codex":
            if rec.get("type") == "response_item":
                p = rec.get("payload")
                if isinstance(p, dict) and p.get("type") == "message" and p.get("role") == "user":
                    text = extract_text_from_content(p.get("content"))
            elif rec.get("type") == "event_msg":
                p = rec.get("payload")
                if isinstance(p, dict) and p.get("type") == "user_message":
                    text = str(p.get("message", ""))
        elif sf.agent == "antigravity":
            if rec.get("type") == "USER_INPUT":
                text = str(rec.get("content", ""))
                m = re.search(r"<USER_REQUEST>\s*(.*?)\s*</USER_REQUEST>", text, re.S)
                if m:
                    text = m.group(1)
        elif sf.agent == "claude":
            if rec.get("type") == "user" and not rec.get("isMeta"):
                msg = rec.get("message")
                if isinstance(msg, dict):
                    text = extract_text_from_content(msg.get("content"))
        elif sf.agent == "pi":
            if rec.get("type") == "message":
                msg = rec.get("message")
                if isinstance(msg, dict) and msg.get("role") == "user":
                    text = extract_text_from_content(msg.get("content"))
        elif sf.agent == "dsh":
            if rec.get("type") == "user/message":
                data = rec.get("data")
                if isinstance(data, dict):
                    src = data.get("source")
                    if isinstance(src, dict) and src.get("kind") == "user":
                        text = extract_text_from_content(data.get("content"))
        elif sf.agent == "grok":
            params = rec.get("params")
            upd = params.get("update") if isinstance(params, dict) else None
            if isinstance(upd, dict) and upd.get("sessionUpdate") == "user_message_chunk":
                text = extract_text_from_content(upd.get("content"))
        elif sf.agent == "kimi":
            if rec.get("type") == "context.append_message":
                msg = rec.get("message")
                if isinstance(msg, dict) and msg.get("role") == "user":
                    text = extract_text_from_content(msg.get("content"))
            elif rec.get("role") == "user":
                text = extract_text_from_content(rec.get("content"))

        if text and not is_noise_user_text(text):
            return clean_preview(text)
    return ""


def read_first_json(path: Path, max_bytes: int = 4 * 1024 * 1024) -> dict[str, Any] | None:
    for line in iter_head_lines(path, max_lines=3, max_bytes=max_bytes):
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
            if isinstance(d, dict):
                return d
        except Exception:
            return None
    return None


def parse_session_ref(
    raw_ref: str,
    explicit_agent: str | None = None,
) -> tuple[str, str | None]:
    """Parse '<agent>:<session_id>' or bare '<session_id>'.

    Recognizes canonical agent names and aliases (e.g. 'agy:ide/<id>',
    'codex:<uuid>', 'kimi:session_<uuid>') without mangling bare IDs.
    """
    ref = raw_ref.strip()
    if ":" in ref:
        prefix, rest = ref.split(":", 1)
        key = prefix.strip().lower()
        canonical = AGENT_ALIASES.get(key, key)
        if canonical in ALL_AGENTS and rest.strip():
            if explicit_agent:
                norm_explicit = normalize_agents([explicit_agent])[0]
                if norm_explicit != canonical:
                    raise ValueError(
                        f"conflicting agent specifier: prefix {prefix!r} vs --agent {explicit_agent!r}"
                    )
            return rest.strip(), canonical
    return ref, explicit_agent


def _kimi_id_matches(file_sid: str, query_sid: str) -> bool:
    if file_sid == query_sid:
        return True
    f_bare = file_sid[len("session_") :] if file_sid.startswith("session_") else file_sid
    q_bare = query_sid[len("session_") :] if query_sid.startswith("session_") else query_sid
    return bool(f_bare) and f_bare == q_bare


def _populate_rollout_index_from_files(
    codex_home: Path,
    files: list[SessionFile],
    rollout_index: dict[str, Path],
) -> None:
    sentinel = f"__scanned__:{codex_home}"
    for sf in files:
        _, rid = codex_sessions.extract_filename_ids(sf.path)
        if rid and rid not in rollout_index:
            rollout_index[rid] = sf.path
        stem_key = f"__stem__:{codex_sessions.rollout_plain_stem(sf.path)}"
        if stem_key not in rollout_index:
            rollout_index[stem_key] = sf.path
    rollout_index[sentinel] = codex_home


def _collect_codex_children_by_sid(
    seed_sids: list[str],
    all_codex_files: list[SessionFile],
    seen_keys: set[tuple[Path, int, int]],
    meta_cache: dict[Any, dict[str, Any] | None],
) -> list[SessionFile]:
    """Discover child Codex SIDs via BFS and collect ALL segments for each child SID."""
    files_by_sid: dict[str, list[tuple[dict[str, Any] | None, SessionFile]]] = {}
    child_sids_by_parent: dict[str, list[str]] = {}
    parent_for_child: dict[str, str] = {}

    for sf in all_codex_files:
        skey = _meta_cache_key(sf.path)
        if skey in seen_keys:
            continue
        meta = _cached_codex_meta(sf.path, meta_cache)
        if not meta:
            continue
        actual_sid = meta["sid"]
        files_by_sid.setdefault(actual_sid, []).append((meta, sf))
        pid = meta.get("parent_id")
        if isinstance(pid, str) and pid and pid != actual_sid:
            bucket = child_sids_by_parent.setdefault(pid, [])
            if actual_sid not in bucket:
                bucket.append(actual_sid)
            parent_for_child.setdefault(actual_sid, pid)

    discovered: list[SessionFile] = []
    queue = list(dict.fromkeys(seed_sids))
    visited_sids = set(queue)
    while queue:
        cur = queue.pop(0)
        child_sids = child_sids_by_parent.get(cur, [])
        if not child_sids:
            continue
        # Order child SIDs by their earliest segment creation time
        child_order_entries: list[tuple[dict[str, Any] | None, Path, str]] = []
        for csid in child_sids:
            segs = files_by_sid.get(csid, [])
            if not segs:
                continue
            ordered_segs = codex_sessions.sort_rollout_entries([(m, sf.path) for m, sf in segs])
            first_meta, first_path = ordered_segs[0]
            child_order_entries.append((first_meta, first_path, csid))
        ordered_firsts = codex_sessions.sort_rollout_entries([(m, p) for m, p, _ in child_order_entries])
        first_path_to_csid = {p: csid for _, p, csid in child_order_entries}
        for _, first_path in ordered_firsts:
            csid = first_path_to_csid[first_path]
            if csid in visited_sids:
                continue
            visited_sids.add(csid)
            queue.append(csid)
            segs = files_by_sid.get(csid, [])
            by_path = {sf.path: sf for _, sf in segs}
            pid = parent_for_child.get(csid, cur)
            for _, seg_path in codex_sessions.sort_rollout_entries([(m, sf.path) for m, sf in segs]):
                orig_sf = by_path[seg_path]
                skey = _meta_cache_key(orig_sf.path)
                if skey in seen_keys:
                    continue
                seen_keys.add(skey)
                discovered.append(
                    SessionFile("codex", csid, orig_sf.path, orig_sf.mtime, orig_sf.size_bytes, True, pid)
                )
    return discovered


def resolve_session_sources(
    session_id: str,
    agent: str | None = None,
    include_children: bool = True,
    home: Path | None = None,
    *,
    codex_home: Path | None = None,
    environment: Mapping[str, str] | None = None,
    meta_cache: dict[Any, dict[str, Any] | None] | None = None,
    rollout_index: dict[str, Path] | None = None,
) -> list[SessionFile]:
    session_id, agent = parse_session_ref(session_id, agent)
    agents = normalize_agents([agent]) if agent else list(ALL_AGENTS)
    matches: list[SessionFile] = []
    by_agent: dict[str, list[SessionFile]] = {}
    cache = meta_cache if meta_cache is not None else {}

    for ag in agents:
        if ag == "codex" and not include_children:
            # Fast targeted lookup avoids parsing headers of unrelated cold Codex files.
            chome = _codex_home(home, codex_home=codex_home, environment=environment)
            raw_files = discover_agent_files(
                "codex",
                home=home,
                codex_home=chome,
                environment=environment,
                verify_headers=False,
            )
            by_agent[ag] = raw_files
            if rollout_index is not None:
                _populate_rollout_index_from_files(chome, raw_files, rollout_index)
            nonstandard: list[SessionFile] = []
            double_uuid_rest: list[SessionFile] = []
            for sf in raw_files:
                fn_sid, fn_rid = codex_sessions.extract_filename_ids(sf.path)
                plain_stem = codex_sessions.rollout_plain_stem(sf.path)
                if sf.session_id == session_id or f"-{session_id}" in sf.path.name:
                    matches.append(sf)
                elif fn_sid is None or not plain_stem.startswith("rollout-"):
                    nonstandard.append(sf)
                elif fn_sid != fn_rid:
                    double_uuid_rest.append(sf)
            # Non-standard filenames have no reliable filename SID: always inspect their headers
            # so `--no-children` matches the same primary files as default `read`.
            for sf in nonstandard:
                meta = _cached_codex_meta(sf.path, cache)
                if meta and meta["sid"] == session_id:
                    matches.append(sf)
            # Check whether verified matches are missing a predecessor (e.g. cold misnamed
            # double-UUID file when only a continuation segment with ordinal > 0 matched).
            need_misnamed_fallback = not matches
            if not need_misnamed_fallback and double_uuid_rest:
                matched_metas = [_cached_codex_meta(m.path, cache) for m in matches]
                ords = [m["ordinal"] for m in matched_metas if m and isinstance(m.get("ordinal"), int)]
                has_resolved_hb = any(
                    m
                    and isinstance(m.get("history_base"), dict)
                    and rollout_index is not None
                    and m["history_base"]["rollout_id"] in rollout_index
                    for m in matched_metas
                )
                if ords and min(ords) > 0 and not has_resolved_hb:
                    need_misnamed_fallback = True
            if need_misnamed_fallback and double_uuid_rest:
                for sf in double_uuid_rest:
                    meta = _cached_codex_meta(sf.path, cache)
                    if meta and meta["sid"] == session_id:
                        matches.append(sf)
            continue
        files = discover_agent_files(
            ag,
            home=home,
            codex_home=codex_home,
            environment=environment,
            verify_headers=True,
            meta_cache=cache,
        )
        by_agent[ag] = files
        if ag == "codex" and rollout_index is not None:
            chome = _codex_home(home, codex_home=codex_home, environment=environment)
            _populate_rollout_index_from_files(chome, files, rollout_index)
        for sf in files:
            if sf.session_id == session_id or (
                ag == "kimi" and not sf.is_subagent and _kimi_id_matches(sf.session_id, session_id)
            ):
                matches.append(sf)

    if not matches:
        raise FileNotFoundError(f"session not found: {session_id}")

    matched_agents = sorted({m.agent for m in matches})
    if len(matched_agents) > 1:
        raise ValueError(
            f"ambiguous session_id {session_id!r} matched multiple agents: {', '.join(matched_agents)}; pass --agent"
        )

    target_agent = matched_agents[0]
    if target_agent == "codex":
        verified_pairs: list[tuple[dict[str, Any] | None, SessionFile]] = []
        seen_keys: set[tuple[Path, int, int]] = set()
        for m in matches:
            mkey = _meta_cache_key(m.path)
            if mkey in seen_keys:
                continue
            meta = _cached_codex_meta(m.path, cache)
            if meta is not None and meta["sid"] != session_id:
                continue
            seen_keys.add(mkey)
            verified_pairs.append(
                (
                    meta,
                    SessionFile(
                        "codex",
                        session_id,
                        m.path,
                        m.mtime,
                        m.size_bytes,
                        m.is_subagent,
                        m.parent_id,
                    ),
                )
            )
        if not verified_pairs:
            raise FileNotFoundError(f"session not found: {session_id}")
        path_to_sf = {sf.path: sf for _, sf in verified_pairs}
        ordered = codex_sessions.sort_rollout_entries([(meta, sf.path) for meta, sf in verified_pairs])
        matches = [path_to_sf[p] for _, p in ordered]
    else:
        matches.sort(key=lambda s: (s.is_subagent, str(s.path)))
    result: list[SessionFile] = list(matches)
    seen_paths: set[Path] = set()
    seen_inode_keys: set[tuple[Path, int, int]] = set()
    for m in result:
        seen_paths.add(m.path)
        seen_paths.add(_path_cache_key(m.path))
        seen_inode_keys.add(_meta_cache_key(m.path))

    if not include_children:
        return result

    all_agent_files = by_agent[target_agent]

    if target_agent == "claude":
        for sf in all_agent_files:
            if sf.is_subagent and sf.parent_id == session_id and sf.path not in seen_paths:
                result.append(sf)
                seen_paths.add(sf.path)

    elif target_agent == "dsh":
        parent_map: dict[str, list[SessionFile]] = {}
        for sf in all_agent_files:
            if sf.path in seen_paths:
                continue
            hdr = read_first_json(sf.path)
            if hdr and hdr.get("type") == "session":
                pid = hdr.get("parentSession")
                if isinstance(pid, str) and pid:
                    sf.parent_id = pid
                    parent_map.setdefault(pid, []).append(sf)
        queue = [m.session_id for m in matches]
        while queue:
            cur = queue.pop(0)
            for child in parent_map.get(cur, []):
                if child.path not in seen_paths:
                    result.append(child)
                    seen_paths.add(child.path)
                    queue.append(child.session_id)

    elif target_agent == "codex":
        result.extend(
            _collect_codex_children_by_sid(
                [m.session_id for m in matches],
                all_agent_files,
                seen_inode_keys,
                cache,
            )
        )

    elif target_agent == "pi":
        candidate_dirs = {m.path.parent for m in matches}
        queue_paths = [str(m.path) for m in matches]
        dir_files = [
            sf for sf in all_agent_files if sf.path.parent in candidate_dirs and sf.path not in seen_paths
        ]
        headers = []
        for sf in dir_files:
            hdr = read_first_json(sf.path)
            if hdr and hdr.get("type") == "session":
                p_ref = hdr.get("parentSession")
                if isinstance(p_ref, str) and p_ref:
                    headers.append((sf, p_ref))
        while queue_paths:
            cur_p = queue_paths.pop(0)
            for sf, p_ref in headers:
                if p_ref == cur_p and sf.path not in seen_paths:
                    sf.parent_id = session_id
                    result.append(sf)
                    seen_paths.add(sf.path)
                    queue_paths.append(str(sf.path))

    elif target_agent == "kimi":
        parent_ids = {m.session_id for m in matches}
        for sf in all_agent_files:
            if sf.is_subagent and sf.parent_id and any(
                _kimi_id_matches(sf.parent_id, pid) for pid in parent_ids
            ) and sf.path not in seen_paths:
                result.append(sf)
                seen_paths.add(sf.path)

    return result


def _ext_for_mime(mime: str) -> str:
    clean = mime.split(";", 1)[0].strip().lower()
    if clean in MIME_EXT_MAP:
        return MIME_EXT_MAP[clean]
    if "/" in clean:
        sub = re.sub(r"[^a-z0-9]", "", clean.split("/", 1)[1].split("+", 1)[0])
        if sub:
            return sub
    return "bin"


def _decode_base64_bytes(b64: str) -> bytes | None:
    cleaned = re.sub(r"\s+", "", b64)
    if not cleaned:
        return None
    pad = (-len(cleaned)) % 4
    padded = cleaned + ("=" * pad)
    try:
        return base64.b64decode(padded, validate=True)
    except Exception:
        return None


def _resolve_local_media_path(uri: str) -> Path | None:
    if uri.startswith("file://"):
        parsed = urllib.parse.urlparse(uri)
        if parsed.netloc not in ("", "localhost"):
            return None
        path_str = urllib.parse.unquote(parsed.path)
        if not path_str:
            return None
        return Path(path_str)
    if uri.startswith("/"):
        return Path(uri)
    return None


def _iter_record_attachments(
    obj: Any,
) -> Iterator[tuple[str, bytes | None, Path | None, str | None]]:
    """Yield (mime_type, raw_bytes, local_path, source_uri) for embedded/linked media."""
    if isinstance(obj, list):
        for item in obj:
            yield from _iter_record_attachments(item)
        return
    if not isinstance(obj, dict):
        return

    btype = obj.get("type")

    # 1. Codex / OpenAI style: {"type": "input_image"|"image_url", "image_url": "data:image/..."}
    if btype in ("input_image", "image_url", "image"):
        img_url = obj.get("image_url")
        url_str = img_url.get("url") if isinstance(img_url, dict) else img_url
        if isinstance(url_str, str):
            m = DATA_IMAGE_RE.match(url_str)
            if m:
                raw = _decode_base64_bytes(m.group(2))
                if raw is not None:
                    yield (m.group(1).lower(), raw, None, None)
                    return

    # 2. Claude / Anthropic style: {"type": "image", "source": {"type": "base64", "media_type": ..., "data": ...}}
    if btype == "image" and isinstance(obj.get("source"), dict):
        src = obj["source"]
        if src.get("type") == "base64" and isinstance(src.get("data"), str):
            mime = src.get("media_type") or src.get("mime_type") or src.get("mimeType")
            if isinstance(mime, str) and mime.lower().startswith("image/"):
                raw = _decode_base64_bytes(src["data"])
                if raw is not None:
                    yield (mime.lower(), raw, None, None)
                    return

    # 3. Pi / DSH / generic inline image block: {"type": "image", "data": "...", "mimeType": "image/..."}
    if btype == "image" and isinstance(obj.get("data"), str):
        mime = obj.get("mimeType") or obj.get("mime_type") or obj.get("media_type")
        if isinstance(mime, str) and mime.lower().startswith("image/"):
            raw = _decode_base64_bytes(obj["data"])
            if raw is not None:
                yield (mime.lower(), raw, None, None)
                return

    # 4. Gemini inlineData / inline_data block
    for inline_key in ("inlineData", "inline_data"):
        inline = obj.get(inline_key)
        if isinstance(inline, dict) and isinstance(inline.get("data"), str):
            mime = inline.get("mimeType") or inline.get("mime_type") or inline.get("media_type")
            if isinstance(mime, str) and mime.lower().startswith("image/"):
                raw = _decode_base64_bytes(inline["data"])
                if raw is not None:
                    yield (mime.lower(), raw, None, None)
                    return

    # 5. Antigravity media array: [{"mime_type": "image/jpeg", "uri": "file:///..."}]
    handled_media = False
    media_list = obj.get("media")
    if isinstance(media_list, list):
        handled_media = True
        for item in media_list:
            if not isinstance(item, dict):
                continue
            mime = item.get("mime_type") or item.get("mimeType") or item.get("media_type")
            uri = item.get("uri")
            if isinstance(mime, str) and mime.lower().startswith("image/") and isinstance(uri, str):
                m = DATA_IMAGE_RE.match(uri)
                if m:
                    raw = _decode_base64_bytes(m.group(2))
                    if raw is not None:
                        yield (m.group(1).lower(), raw, None, None)
                        continue
                local_path = _resolve_local_media_path(uri)
                if local_path is not None and local_path.is_file():
                    yield (mime.lower(), None, local_path, uri)

    for k, v in obj.items():
        if handled_media and k == "media":
            continue
        if isinstance(v, (dict, list)):
            yield from _iter_record_attachments(v)


def _extract_line_attachments(
    line: str,
    source_idx: int,
    line_no: int,
    attachments_dir: Path,
) -> list[dict[str, Any]]:
    if not any(tok in line for tok in ATTACHMENT_HINT_TOKENS):
        return []
    try:
        rec = json.loads(line)
    except Exception:
        return []

    extracted: list[dict[str, Any]] = []
    att_seq = 0
    for mime, raw_bytes, src_path, source_uri in _iter_record_attachments(rec):
        data: bytes | None = raw_bytes
        if data is None and src_path is not None:
            try:
                data = src_path.read_bytes()
            except Exception:
                continue
        if data is None:
            continue

        att_seq += 1
        attachments_dir.mkdir(parents=True, exist_ok=True)
        ext = _ext_for_mime(mime)
        att_path = attachments_dir / f"{source_idx:03d}-L{line_no}-{att_seq:02d}.{ext}"
        att_path.write_bytes(data)
        entry: dict[str, Any] = {
            "line": line_no,
            "mime_type": mime,
            "bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
            "path": str(att_path.resolve()),
        }
        if source_uri:
            entry["source_uri"] = source_uri
        extracted.append(entry)
    return extracted


def _validate_anchor_identity(
    path: Path,
    agent: str,
    expected_sid: str,
    *,
    meta_cache: dict[Any, dict[str, Any] | None] | None = None,
) -> tuple[bool, str | None]:
    """Verify that a runtime/SQLite anchor file actually belongs to `(agent, expected_sid)`."""
    if agent == "codex":
        key = _meta_cache_key(path)
        if meta_cache is not None and key in meta_cache and meta_cache[key] is not None:
            meta = meta_cache[key]
        else:
            meta, err = codex_sessions.read_rollout_meta_detailed(path, max_lines=1)
            if err is not None or meta is None:
                return False, f"unverifiable_header ({err or 'missing_meta'})"
            if meta_cache is not None:
                meta_cache[key] = meta
        if meta["sid"] != expected_sid:
            return False, f"sid_conflict (expected {expected_sid}, header declares {meta['sid']})"
        return True, None
    if agent == "pi":
        hdr = read_first_json(path)
        if not hdr or hdr.get("type") != "session" or _is_dsh_session_header(hdr):
            return False, "unverifiable_header (not a Pi session header)"
        if hdr.get("id") != expected_sid:
            return False, f"sid_conflict (expected {expected_sid}, header declares {hdr.get('id')})"
        return True, None
    return True, None


def _plan_codex_sid_sources(
    sid: str,
    static_files: list[SessionFile],
    *,
    codex_home: Path | None,
    expected_active_path: Path | None,
    is_subagent: bool,
    parent_id: str | None,
    include_superseded: bool,
    meta_cache: dict[Any, dict[str, Any] | None],
    rollout_index: dict[str, Path],
    covered_plan_by_key: dict[tuple[Path, int, int], _SourcePlan],
) -> tuple[list[_SourcePlan], list[str]]:
    """Verify anchors & resolve the authoritative Codex lineage for one SID."""
    warnings: list[str] = []
    known_keys: set[tuple[Path, int, int]] = {_meta_cache_key(sf.path) for sf in static_files}
    candidate_paths: list[Path] = [sf.path for sf in static_files]
    sf_by_key: dict[tuple[Path, int, int], SessionFile] = {_meta_cache_key(sf.path): sf for sf in static_files}

    def _check_anchor(
        raw_path: Path | None,
        *,
        missing_code: str,
        missed_code: str,
        conflict_code: str,
        label: str,
    ) -> Path | None:
        if raw_path is None:
            return None
        resolved_cand = codex_sessions.resolve_alternate_rollout_path(
            raw_path, codex_home, rollout_index=rollout_index
        )
        if resolved_cand is None:
            warnings.append(f"{missing_code}: {label} path {raw_path} does not exist")
            return None
        ok, err_reason = _validate_anchor_identity(
            resolved_cand, "codex", sid, meta_cache=meta_cache
        )
        if not ok:
            warnings.append(
                f"{conflict_code}: {label} path {resolved_cand} failed identity check ({err_reason})"
            )
            return None
        cand_key = _meta_cache_key(resolved_cand)
        if cand_key not in known_keys:
            warnings.append(
                f"{missed_code}: {label} path {resolved_cand} was not covered by static discovery"
            )
            known_keys.add(cand_key)
            candidate_paths.append(resolved_cand)
            try:
                st = resolved_cand.stat()
                sf_by_key[cand_key] = SessionFile(
                    "codex", sid, resolved_cand, st.st_mtime, st.st_size, is_subagent, parent_id
                )
            except OSError as exc:
                warnings.append(f"unreadable_source: {resolved_cand} ({exc})")
                return None
        return resolved_cand

    verified_active = _check_anchor(
        expected_active_path,
        missing_code="active_file_missing",
        missed_code="active_file_missed_by_discovery",
        conflict_code="active_file_sid_conflict",
        label="live pane active",
    )

    verified_sqlite: Path | None = None
    if codex_home is not None:
        sqlite_raw, sqlite_err = codex_sessions.query_sqlite_rollout_path_detailed(sid, codex_home)
        if sqlite_err:
            warnings.append(f"codex_sqlite_query_failed: {sqlite_err}")
        verified_sqlite = _check_anchor(
            sqlite_raw,
            missing_code="sqlite_rollout_missing",
            missed_code="sqlite_rollout_missed_by_discovery",
            conflict_code="sqlite_rollout_sid_conflict",
            label="state sqlite rollout",
        )

    if not candidate_paths:
        return [], warnings

    effective, unlinked, lineage_warnings = codex_sessions.resolve_effective_lineage(
        sid,
        candidate_paths,
        active_path=verified_active,
        sqlite_path=verified_sqlite,
        codex_home=codex_home,
        meta_cache=meta_cache,
        rollout_index=rollout_index,
    )
    warnings.extend(lineage_warnings)

    plans: list[_SourcePlan] = []
    for seg in effective:
        p = seg["path"]
        pkey = _meta_cache_key(p)
        role = str(seg.get("role") or "effective")
        if seg.get("meta") is None:
            _, hdr_err = codex_sessions.read_rollout_meta_detailed(p, max_lines=1)
            warnings.append(
                f"codex_header_{hdr_err or 'missing_meta'}: unable to verify session_meta header in {p}"
            )
        # If an inherited prefix is ALREADY covered by an ancestor SID in this read
        # (e.g., reading a parent together with its forked children), record the
        # inherited range on the ancestor plan instead of duplicating the file.
        if role == "inherited_prefix" and pkey in covered_plan_by_key:
            existing = covered_plan_by_key[pkey]
            existing.inherited_ranges.append(
                {
                    "inherited_by": sid,
                    "end_ordinal_exclusive": seg.get("end_ordinal_exclusive"),
                    "end_byte_offset": seg.get("end_byte_offset"),
                }
            )
            continue
        sf = sf_by_key.get(pkey)
        if sf is None:
            try:
                st = p.stat()
            except OSError as exc:
                warnings.append(f"unreadable_source: {p} ({exc})")
                continue
            seg_sid = str(seg.get("segment_sid") or sid)
            if role == "inherited_prefix":
                seg_parent = (seg.get("meta") or {}).get("parent_id")
                if seg_parent == seg_sid:
                    seg_parent = None
                sf = SessionFile("codex", seg_sid, p, st.st_mtime, st.st_size, False, seg_parent)
            else:
                sf = SessionFile("codex", seg_sid, p, st.st_mtime, st.st_size, is_subagent, parent_id)
        plan = _SourcePlan(
            sf=sf,
            role=role,
            end_ordinal_exclusive=seg.get("end_ordinal_exclusive"),
            end_byte_offset=seg.get("end_byte_offset"),
        )
        plans.append(plan)
        covered_plan_by_key.setdefault(pkey, plan)
    if include_superseded:
        for up in unlinked:
            ukey = _meta_cache_key(up)
            if ukey in covered_plan_by_key:
                continue
            sf = sf_by_key.get(ukey)
            if sf is None:
                try:
                    st = up.stat()
                except OSError:
                    continue
                sf = SessionFile("codex", sid, up, st.st_mtime, st.st_size, is_subagent, parent_id)
            plan = _SourcePlan(sf=sf, role="superseded")
            plans.append(plan)
            covered_plan_by_key.setdefault(ukey, plan)
    return plans, warnings


def _plan_session_sources(
    session_id: str,
    target_agent: str,
    sources: list[SessionFile],
    *,
    home: Path | None = None,
    codex_home: Path | None = None,
    unknown_codex_home: bool = False,
    environment: Mapping[str, str] | None = None,
    expected_active_path: Path | None = None,
    include_superseded: bool = False,
    meta_cache: dict[Any, dict[str, Any] | None] | None = None,
    rollout_index: dict[str, Path] | None = None,
) -> tuple[bool, list[str], list[_SourcePlan]]:
    """Build verified, lineage-ordered source plans for `read_session`."""
    warnings: list[str] = []
    cache = meta_cache if meta_cache is not None else {}

    if target_agent != "codex":
        plans = [_SourcePlan(sf=sf) for sf in sources]
        if expected_active_path is not None:
            known = {_meta_cache_key(sf.path) for sf in sources}
            if not expected_active_path.is_file():
                warnings.append(
                    f"active_file_missing: live pane active path {expected_active_path} does not exist"
                )
            else:
                ok, err_reason = _validate_anchor_identity(
                    expected_active_path, target_agent, session_id, meta_cache=cache
                )
                if not ok:
                    warnings.append(
                        f"active_file_sid_conflict: live pane active path {expected_active_path} failed identity check ({err_reason})"
                    )
                elif _meta_cache_key(expected_active_path) not in known:
                    warnings.append(
                        f"active_file_missed_by_discovery: live pane active path {expected_active_path} was not covered by static discovery"
                    )
                    try:
                        st = expected_active_path.stat()
                        plans.append(
                            _SourcePlan(
                                sf=SessionFile(
                                    target_agent, session_id, expected_active_path, st.st_mtime, st.st_size
                                )
                            )
                        )
                    except OSError as exc:
                        warnings.append(f"unreadable_source: {expected_active_path} ({exc})")
        return (len(warnings) == 0), warnings, plans

    chome: Path | None = (
        None if unknown_codex_home else _codex_home(home, codex_home=codex_home, environment=environment)
    )
    if unknown_codex_home:
        warnings.append(
            f"unknown_pane_storage_root: could not confirm CODEX_HOME for live pane active path {expected_active_path}"
        )

    sid_order: list[str] = []
    sid_files: dict[str, list[SessionFile]] = {}
    sid_meta: dict[str, tuple[bool, str | None]] = {}
    for sf in sources:
        if sf.session_id not in sid_files:
            sid_order.append(sf.session_id)
            sid_files[sf.session_id] = []
            sid_meta[sf.session_id] = (sf.is_subagent, sf.parent_id)
        sid_files[sf.session_id].append(sf)
    if session_id not in sid_files:
        sid_order.insert(0, session_id)
        sid_files[session_id] = []
        sid_meta[session_id] = (False, None)

    rindex: dict[str, Path] = rollout_index if rollout_index is not None else {}
    for sf in sources:
        _, rid = codex_sessions.extract_filename_ids(sf.path)
        if rid and rid not in rindex:
            rindex[rid] = sf.path

    all_plans: list[_SourcePlan] = []
    covered_plan_by_key: dict[tuple[Path, int, int], _SourcePlan] = {}
    for sid in sid_order:
        is_sub, pid = sid_meta[sid]
        sid_plans, sid_warnings = _plan_codex_sid_sources(
            sid,
            sid_files[sid],
            codex_home=chome,
            expected_active_path=expected_active_path if sid == session_id else None,
            is_subagent=is_sub,
            parent_id=pid,
            include_superseded=include_superseded,
            meta_cache=cache,
            rollout_index=rindex,
            covered_plan_by_key=covered_plan_by_key,
        )
        warnings.extend(sid_warnings)
        all_plans.extend(sid_plans)

    return (len(warnings) == 0), warnings, all_plans


def _check_stream_header_sid(
    agent: str,
    rec: dict[str, Any],
    expected_sid: str | None,
) -> str | None:
    """Return an error reason if an open stream's header SID conflicts with `expected_sid`."""
    if not expected_sid:
        return None
    if agent == "codex" and rec.get("type") == "session_meta":
        payload = rec.get("payload")
        actual = payload.get("id") if isinstance(payload, dict) else None
        if isinstance(actual, str) and actual and actual != expected_sid:
            return f"expected {expected_sid}, stream header declares {actual}"
    elif agent == "pi" and rec.get("type") == "session" and not _is_dsh_session_header(rec):
        actual = rec.get("id")
        if isinstance(actual, str) and actual and actual != expected_sid:
            return f"expected {expected_sid}, stream header declares {actual}"
    return None


def export_sources(
    session_id: str,
    sources: list[SessionFile],
    output_dir: Path,
    include_children: bool,
    *,
    complete: bool = True,
    warnings: list[str] | None = None,
    source_plans: list[_SourcePlan] | None = None,
) -> dict[str, Any]:
    if output_dir.exists():
        raise FileExistsError(f"output directory already exists: {output_dir}")

    plans = source_plans if source_plans is not None else [_SourcePlan(sf=sf) for sf in sources]
    out_warnings = list(warnings or [])
    out_complete = bool(complete)
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    tmp_dir = output_dir.parent / f".tmp-{output_dir.name}-{uuid.uuid4().hex[:8]}"
    tmp_dir.mkdir(parents=True, exist_ok=False)

    final_dir_resolved = output_dir.resolve()
    tmp_attachments_dir = tmp_dir / "attachments"
    final_attachments_dir = final_dir_resolved / "attachments"

    try:
        manifest_sources: list[dict[str, Any]] = []
        for idx, plan in enumerate(plans, 1):
            sf = plan.sf
            out_name = f"{idx:03d}-{sf.agent}.jsonl"
            tmp_out_path = tmp_dir / out_name
            final_out_path = final_dir_resolved / out_name
            hasher = hashlib.sha256()
            byte_count = 0
            record_count = 0
            malformed_lines = 0
            attachments_meta: list[dict[str, Any]] = []
            ordinal_start: int | None = None
            ordinal_end: int | None = None
            sid_conflict_reason: str | None = None
            misaligned_byte_boundary = False
            checked_header = False

            with tmp_out_path.open("wb") as dst:
                for line_no, raw_bytes in enumerate(iter_raw_bytes_lines(sf.path), 1):
                    line_start = byte_count
                    line_end = line_start + len(raw_bytes)
                    line = raw_bytes.decode("utf-8", errors="replace")
                    stripped = line.strip()
                    rec_obj: dict[str, Any] | None = None
                    if stripped:
                        try:
                            parsed = json.loads(stripped)
                            if isinstance(parsed, dict):
                                rec_obj = parsed
                            else:
                                malformed_lines += 1
                        except Exception:
                            malformed_lines += 1

                    # Open-stream SID check prevents TOCTOU `os.replace` from exporting foreign content.
                    if not checked_header and rec_obj is not None:
                        checked_header = True
                        conflict = _check_stream_header_sid(sf.agent, rec_obj, sf.session_id)
                        if conflict is not None:
                            sid_conflict_reason = conflict
                            break

                    dst.write(raw_bytes)
                    hasher.update(raw_bytes)
                    byte_count = line_end

                    if (
                        plan.end_byte_offset is not None
                        and line_start < plan.end_byte_offset < line_end
                    ):
                        misaligned_byte_boundary = True

                    if stripped:
                        record_count += 1
                        ord_v = (
                            rec_obj.get("ordinal")
                            if (sf.agent == "codex" and isinstance(rec_obj, dict))
                            else None
                        )
                        if isinstance(ord_v, int) and not isinstance(ord_v, bool):
                            if ordinal_start is None or ord_v < ordinal_start:
                                ordinal_start = ord_v
                            if ordinal_end is None or ord_v > ordinal_end:
                                ordinal_end = ord_v

                        within_range = True
                        if plan.end_byte_offset is not None and line_start >= plan.end_byte_offset:
                            within_range = False
                        if (
                            plan.end_byte_offset is not None
                            and line_start < plan.end_byte_offset < line_end
                        ):
                            within_range = False
                        if (
                            sf.agent == "codex"
                            and plan.end_ordinal_exclusive is not None
                            and isinstance(ord_v, int)
                            and not isinstance(ord_v, bool)
                            and ord_v >= plan.end_ordinal_exclusive
                        ):
                            within_range = False

                        extracted = _extract_line_attachments(line, idx, line_no, tmp_attachments_dir)
                        for entry in extracted:
                            rel_name = Path(entry["path"]).name
                            entry["path"] = str(final_attachments_dir / rel_name)
                            if plan.end_ordinal_exclusive is not None or plan.end_byte_offset is not None:
                                entry["within_effective_range"] = within_range
                        attachments_meta.extend(extracted)

            if sid_conflict_reason is not None:
                tmp_out_path.unlink(missing_ok=True)
                out_complete = False
                out_warnings.append(
                    f"source_sid_changed_during_read: skipped {sf.path} ({sid_conflict_reason})"
                )
                continue

            if malformed_lines > 0:
                out_complete = False
                out_warnings.append(
                    f"malformed_record_lines: found {malformed_lines} malformed/truncated line(s) in {sf.path}"
                )
            if misaligned_byte_boundary:
                out_complete = False
                out_warnings.append(
                    f"history_base_byte_boundary_misaligned: end_byte_offset={plan.end_byte_offset} "
                    f"falls inside a JSONL line in {sf.path}"
                )
            if plan.end_byte_offset is not None and byte_count < plan.end_byte_offset:
                out_complete = False
                out_warnings.append(
                    f"history_base_parent_truncated: expected at least {plan.end_byte_offset} bytes "
                    f"in {sf.path}, got {byte_count}"
                )

            source_entry: dict[str, Any] = {
                "source": {
                    "agent": sf.agent,
                    "session_id": sf.session_id,
                    "path": str(sf.path),
                    "parent": sf.parent_id,
                    "role": plan.role,
                },
                "exported": str(final_out_path),
                "bytes": byte_count,
                "records": record_count,
                "sha256": hasher.hexdigest(),
                "attachments": attachments_meta,
            }
            if ordinal_start is not None:
                source_entry["ordinal_start"] = ordinal_start
                source_entry["ordinal_end"] = ordinal_end
            if plan.end_ordinal_exclusive is not None:
                source_entry["superseded_from_ordinal"] = plan.end_ordinal_exclusive
            if plan.end_byte_offset is not None:
                source_entry["end_byte_offset"] = plan.end_byte_offset
            if plan.inherited_ranges:
                source_entry["inherited_ranges"] = plan.inherited_ranges
            manifest_sources.append(source_entry)

        manifest = {
            "version": 1,
            "session_id": session_id,
            "include_children": include_children,
            "complete": out_complete,
            "warnings": out_warnings,
            "sources": manifest_sources,
        }
        (tmp_dir / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        if output_dir.exists():
            raise FileExistsError(f"output directory already exists: {output_dir}")
        tmp_dir.replace(output_dir)
        return manifest
    except Exception:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise


def _parse_timeline_events_with_stats(
    agent: str,
    path: Path,
    full: bool = False,
    *,
    max_ordinal_exclusive: int | None = None,
    max_byte_offset_exclusive: int | None = None,
    expected_sid: str | None = None,
) -> tuple[list[dict[str, Any]], int, int, int, list[str]]:
    events: list[dict[str, Any]] = []
    total_records = 0
    superseded_records = 0
    malformed_lines = 0
    parse_warnings: list[str] = []

    def fmt(text: str) -> str:
        return text.strip() if full else clean_preview(text, 300)

    byte_offset = 0
    checked_header = False
    misaligned_warned = False
    for idx, raw_bytes in enumerate(iter_raw_bytes_lines(path), 1):
        line_start = byte_offset
        byte_offset += len(raw_bytes)
        line = raw_bytes.decode("utf-8", errors="replace").strip()
        if not line:
            continue
        total_records += 1

        if (
            max_byte_offset_exclusive is not None
            and line_start < max_byte_offset_exclusive < byte_offset
        ):
            if not misaligned_warned:
                misaligned_warned = True
                parse_warnings.append(
                    f"history_base_byte_boundary_misaligned: end_byte_offset={max_byte_offset_exclusive} "
                    f"falls inside a JSONL line in {path}"
                )
            superseded_records += 1
            continue

        if max_byte_offset_exclusive is not None and line_start >= max_byte_offset_exclusive:
            superseded_records += 1
            continue
        try:
            rec = json.loads(line)
        except Exception:
            malformed_lines += 1
            continue
        if not isinstance(rec, dict):
            malformed_lines += 1
            continue

        if not checked_header:
            checked_header = True
            conflict = _check_stream_header_sid(agent, rec, expected_sid)
            if conflict is not None:
                parse_warnings.append(
                    f"source_sid_changed_during_read: skipped {path} ({conflict})"
                )
                return [], total_records, superseded_records, malformed_lines, parse_warnings

        if (
            agent == "codex"
            and max_ordinal_exclusive is not None
            and isinstance(rec.get("ordinal"), int)
            and not isinstance(rec.get("ordinal"), bool)
            and rec["ordinal"] >= max_ordinal_exclusive
        ):
            superseded_records += 1
            continue

        ts = rec.get("timestamp") or rec.get("created_at") or rec.get("time") or ""
        if isinstance(ts, (int, float)):
            sec = ts / 1000.0 if ts > 1e12 else float(ts)
            ts = iso_local(sec)

        if agent == "codex":
            if rec.get("type") == "response_item":
                p = rec.get("payload")
                if not isinstance(p, dict):
                    continue
                pt = p.get("type")
                if pt == "message" and p.get("role") in ("user", "assistant"):
                    txt = extract_text_from_content(p.get("content"))
                    if p.get("role") == "user" and is_noise_user_text(txt):
                        continue
                    if txt.strip():
                        events.append(
                            {
                                "line": idx,
                                "ts": ts,
                                "kind": p["role"],
                                "text": fmt(txt),
                            }
                        )
                elif pt in ("function_call", "custom_tool_call"):
                    events.append(
                        {
                            "line": idx,
                            "ts": ts,
                            "kind": "tool_call",
                            "tool": p.get("name"),
                        }
                    )

        elif agent == "antigravity":
            t = rec.get("type")
            if t == "USER_INPUT":
                txt = str(rec.get("content", ""))
                m = re.search(r"<USER_REQUEST>\s*(.*?)\s*</USER_REQUEST>", txt, re.S)
                if m:
                    txt = m.group(1)
                if txt.strip():
                    events.append(
                        {
                            "line": idx,
                            "ts": ts,
                            "kind": "user",
                            "text": fmt(txt),
                        }
                    )
            elif t == "PLANNER_RESPONSE":
                txt = str(rec.get("content", ""))
                tcs = rec.get("tool_calls")
                if txt.strip():
                    events.append(
                        {
                            "line": idx,
                            "ts": ts,
                            "kind": "assistant",
                            "text": fmt(txt),
                        }
                    )
                if isinstance(tcs, list):
                    for tc in tcs:
                        if isinstance(tc, dict):
                            events.append(
                                {
                                    "line": idx,
                                    "ts": ts,
                                    "kind": "tool_call",
                                    "tool": tc.get("name") or tc.get("tool_name"),
                                }
                            )

        elif agent == "claude":
            t = rec.get("type")
            if t == "user" and not rec.get("isMeta"):
                msg = rec.get("message")
                if not isinstance(msg, dict):
                    continue
                txt = extract_text_from_content(msg.get("content"))
                if txt.strip() and not is_noise_user_text(txt):
                    events.append(
                        {
                            "line": idx,
                            "ts": ts,
                            "kind": "user",
                            "text": fmt(txt),
                        }
                    )
            elif t == "assistant":
                msg = rec.get("message")
                if not isinstance(msg, dict):
                    continue
                content = msg.get("content") or []
                txt = extract_text_from_content(content)
                if txt.strip():
                    events.append(
                        {
                            "line": idx,
                            "ts": ts,
                            "kind": "assistant",
                            "text": fmt(txt),
                        }
                    )
                if isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "tool_use":
                            events.append(
                                {
                                    "line": idx,
                                    "ts": ts,
                                    "kind": "tool_call",
                                    "tool": block.get("name"),
                                }
                            )

        elif agent == "pi":
            if rec.get("type") == "message":
                msg = rec.get("message")
                if not isinstance(msg, dict):
                    continue
                role = msg.get("role")
                content = msg.get("content")
                if role == "user":
                    txt = extract_text_from_content(content)
                    if txt.strip() and not is_noise_user_text(txt):
                        events.append(
                            {
                                "line": idx,
                                "ts": ts,
                                "kind": "user",
                                "text": fmt(txt),
                            }
                        )
                elif role == "assistant":
                    txt = extract_text_from_content(content)
                    if txt.strip():
                        events.append(
                            {
                                "line": idx,
                                "ts": ts,
                                "kind": "assistant",
                                "text": fmt(txt),
                            }
                        )
                    if isinstance(content, list):
                        for block in content:
                            if isinstance(block, dict) and block.get("type") == "toolCall":
                                events.append(
                                    {
                                        "line": idx,
                                        "ts": ts,
                                        "kind": "tool_call",
                                        "tool": block.get("name"),
                                    }
                                )

        elif agent == "dsh":
            t = rec.get("type")
            data = rec.get("data")
            if not isinstance(data, dict):
                continue
            if t == "user/message":
                src = data.get("source")
                if isinstance(src, dict) and src.get("kind") == "user":
                    txt = extract_text_from_content(data.get("content"))
                    if txt.strip() and not is_noise_user_text(txt):
                        events.append(
                            {
                                "line": idx,
                                "ts": ts,
                                "kind": "user",
                                "text": fmt(txt),
                            }
                        )
            elif t == "assistant/message":
                raw_msg = data.get("message")
                msg = raw_msg if isinstance(raw_msg, dict) else data
                txt = extract_text_from_content(msg.get("content"))
                if txt.strip():
                    events.append(
                        {
                            "line": idx,
                            "ts": ts,
                            "kind": "assistant",
                            "text": fmt(txt),
                        }
                    )
            elif t in ("tool/call", "tool/start"):
                events.append(
                    {
                        "line": idx,
                        "ts": ts,
                        "kind": "tool_call",
                        "tool": data.get("name") or data.get("tool"),
                    }
                )

        elif agent == "grok":
            params = rec.get("params")
            upd = params.get("update") if isinstance(params, dict) else None
            if not isinstance(upd, dict):
                continue
            su = upd.get("sessionUpdate")
            if su in ("user_message_chunk", "agent_message_chunk"):
                txt = extract_text_from_content(upd.get("content"))
                kind = "user" if su == "user_message_chunk" else "assistant"
                if full:
                    if events and events[-1]["kind"] == kind:
                        events[-1]["text"] += txt
                    elif txt.strip():
                        events.append(
                            {
                                "line": idx,
                                "ts": ts,
                                "kind": kind,
                                "text": txt,
                            }
                        )
                elif txt.strip():
                    if events and events[-1]["kind"] == kind:
                        if len(events[-1]["text"]) < 300:
                            events[-1]["text"] = clean_preview(
                                events[-1]["text"] + txt, 300
                            )
                    else:
                        events.append(
                            {
                                "line": idx,
                                "ts": ts,
                                "kind": kind,
                                "text": clean_preview(txt, 300),
                            }
                        )
            elif su == "tool_call":
                events.append(
                    {
                        "line": idx,
                        "ts": ts,
                        "kind": "tool_call",
                        "tool": upd.get("title") or upd.get("toolName"),
                    }
                )

        elif agent == "kimi":
            t = rec.get("type")
            if t == "context.append_message":
                msg = rec.get("message")
                if isinstance(msg, dict):
                    role = msg.get("role")
                    if role in ("user", "assistant"):
                        txt = extract_text_from_content(msg.get("content"))
                        if txt.strip() and (role != "user" or not is_noise_user_text(txt)):
                            events.append(
                                {
                                    "line": idx,
                                    "ts": ts,
                                    "kind": role,
                                    "text": fmt(txt),
                                }
                            )
            elif t == "context.append_loop_event":
                ev = rec.get("event")
                if isinstance(ev, dict):
                    et = ev.get("type")
                    if et == "content.part":
                        part = ev.get("part")
                        if isinstance(part, dict) and part.get("type") == "text":
                            txt = str(part.get("text") or "")
                            if txt.strip():
                                events.append(
                                    {
                                        "line": idx,
                                        "ts": ts,
                                        "kind": "assistant",
                                        "text": fmt(txt),
                                    }
                                )
                    elif et == "tool.call":
                        events.append(
                            {
                                "line": idx,
                                "ts": ts,
                                "kind": "tool_call",
                                "tool": ev.get("name"),
                            }
                        )
            else:
                role = rec.get("role")
                if role in ("user", "assistant"):
                    txt = extract_text_from_content(rec.get("content"))
                    if txt.strip():
                        events.append(
                            {
                                "line": idx,
                                "ts": ts,
                                "kind": role,
                                "text": fmt(txt),
                            }
                        )

    if agent == "grok" and full:
        for ev in events:
            if "text" in ev:
                ev["text"] = ev["text"].strip()

    if max_byte_offset_exclusive is not None and byte_offset < max_byte_offset_exclusive:
        parse_warnings.append(
            f"history_base_parent_truncated: expected at least {max_byte_offset_exclusive} bytes "
            f"in {path}, got {byte_offset}"
        )

    return events, total_records, superseded_records, malformed_lines, parse_warnings


def parse_timeline_events(
    agent: str,
    path: Path,
    full: bool = False,
    *,
    max_ordinal_exclusive: int | None = None,
    max_byte_offset_exclusive: int | None = None,
) -> tuple[list[dict[str, Any]], int]:
    events, total_records, _, _, _ = _parse_timeline_events_with_stats(
        agent,
        path,
        full=full,
        max_ordinal_exclusive=max_ordinal_exclusive,
        max_byte_offset_exclusive=max_byte_offset_exclusive,
    )
    return events, total_records


def window_snippet(line: str, start: int, end: int, radius: int = 120) -> str:
    s = max(0, start - radius)
    e = min(len(line), end + radius)
    frag = line[s:e]
    frag = re.sub(r"\s+", " ", frag).strip()
    if s > 0:
        frag = "…" + frag
    if e < len(line):
        frag = frag + "…"
    return frag


def _verify_session_file_header(
    sf: SessionFile,
    meta_cache: dict[Any, dict[str, Any] | None],
) -> SessionFile | None:
    """Verify authoritative header SID for non-canonical/multi-UUID Codex or Pi files."""
    if sf.agent == "codex":
        fn_sid, fn_rid = codex_sessions.extract_filename_ids(sf.path)
        plain_stem = codex_sessions.rollout_plain_stem(sf.path)
        if not (fn_sid and fn_sid == fn_rid and plain_stem.startswith("rollout-")):
            meta = _cached_codex_meta(sf.path, meta_cache)
            if meta and meta["sid"] != sf.session_id:
                return SessionFile(
                    "codex", meta["sid"], sf.path, sf.mtime, sf.size_bytes, sf.is_subagent, sf.parent_id
                )
    elif sf.agent == "pi":
        m = UUID_RE.findall(sf.path.stem)
        if len(m) != 1:
            hdr = read_first_json(sf.path)
            if _is_dsh_session_header(hdr):
                return None
            hdr_id = hdr.get("id") if (hdr and hdr.get("type") == "session") else None
            if isinstance(hdr_id, str) and hdr_id and hdr_id != sf.session_id:
                return SessionFile(
                    "pi", hdr_id, sf.path, sf.mtime, sf.size_bytes, sf.is_subagent, sf.parent_id
                )
    return sf


def _pull_misnamed_cold_predecessors(
    target_sessions: list[LogicalSession],
    cold_rough: list[LogicalSession],
    meta_cache: dict[Any, dict[str, Any] | None],
) -> list[LogicalSession]:
    """If a hot Codex session only has continuation segments (`min_ord > 0`), check cold
    double-UUID/non-standard files once so misnamed cold predecessors are not dropped."""
    if not cold_rough or not target_sessions:
        return target_sessions
    orphan_sids: set[str] = set()
    for ls in target_sessions:
        if ls.agent != "codex":
            continue
        ords: list[int] = []
        for sf in ls.segments:
            meta = _cached_codex_meta(sf.path, meta_cache)
            if meta and isinstance(meta.get("ordinal"), int):
                ords.append(meta["ordinal"])
        if ords and min(ords) > 0:
            orphan_sids.add(ls.session_id)
    if not orphan_sids:
        return target_sessions

    extra_by_sid: dict[str, list[SessionFile]] = {}
    for cls in cold_rough:
        if cls.agent != "codex":
            continue
        for sf in cls.segments:
            fn_sid, fn_rid = codex_sessions.extract_filename_ids(sf.path)
            plain_stem = codex_sessions.rollout_plain_stem(sf.path)
            if fn_sid and fn_sid == fn_rid and plain_stem.startswith("rollout-"):
                continue
            meta = _cached_codex_meta(sf.path, meta_cache)
            if meta and meta["sid"] in orphan_sids:
                extra_by_sid.setdefault(meta["sid"], []).append(
                    SessionFile("codex", meta["sid"], sf.path, sf.mtime, sf.size_bytes, sf.is_subagent, sf.parent_id)
                )
    if not extra_by_sid:
        return target_sessions

    merged: list[LogicalSession] = []
    for ls in target_sessions:
        extras = extra_by_sid.get(ls.session_id) if ls.agent == "codex" else None
        if extras:
            segs = _dedup_session_files([*ls.segments, *extras])
            merged.append(
                LogicalSession(ls.agent, ls.session_id, segs, ls.is_subagent, ls.parent_id)
            )
        else:
            merged.append(ls)
    return merged


def _verify_hot_logical_sessions(
    hot_rough: list[LogicalSession],
    *,
    meta_cache: dict[Any, dict[str, Any] | None],
    cold_rough: list[LogicalSession] | None = None,
    lazy_limit: int | None = None,
) -> tuple[list[LogicalSession], int]:
    """Verify headers for hot sessions, stopping early when `lazy_limit` is set."""
    if lazy_limit is not None and lazy_limit > 0 and len(hot_rough) > lazy_limit:
        # Process rough sessions in descending mtime order and stop once we have
        # at least `lazy_limit` verified top sessions whose mtimes beat all remaining
        # unverified sessions (and merge any same-SID collisions among seen files).
        verified_by_key: dict[tuple[str, str, bool], list[SessionFile]] = {}
        processed = 0
        for idx, ls in enumerate(hot_rough):
            for sf in ls.segments:
                vsf = _verify_session_file_header(sf, meta_cache)
                if vsf is not None:
                    verified_by_key.setdefault((vsf.agent, vsf.session_id, vsf.is_subagent), []).append(vsf)
            processed = idx + 1
            if len(verified_by_key) >= lazy_limit:
                next_mtime = hot_rough[idx + 1].mtime if idx + 1 < len(hot_rough) else -1.0
                top_mtimes = sorted(
                    (max(s.mtime for s in segs) for segs in verified_by_key.values()),
                    reverse=True,
                )
                if len(top_mtimes) >= lazy_limit and top_mtimes[lazy_limit - 1] > next_mtime:
                    break
        # Pull any additional rough segments whose rough SID matches a selected verified SID
        selected_sids = {sid for _, sid, _ in verified_by_key}
        for ls in hot_rough[processed:]:
            if ls.session_id in selected_sids:
                for sf in ls.segments:
                    vsf = _verify_session_file_header(sf, meta_cache)
                    if vsf is not None and vsf.session_id in selected_sids:
                        verified_by_key.setdefault((vsf.agent, vsf.session_id, vsf.is_subagent), []).append(vsf)
        all_verified = [sf for segs in verified_by_key.values() for sf in segs]
        sessions = group_logical_sessions(all_verified, sort_segments=False, meta_cache=meta_cache)
        sessions.sort(key=lambda s: s.mtime, reverse=True)
        selected = sessions[:lazy_limit]
        if cold_rough:
            selected = _pull_misnamed_cold_predecessors(selected, cold_rough, meta_cache)
        estimated_total = len(sessions) + max(0, len(hot_rough) - processed)
        return selected, estimated_total

    verified_files: list[SessionFile] = []
    for ls in hot_rough:
        for sf in ls.segments:
            vsf = _verify_session_file_header(sf, meta_cache)
            if vsf is not None:
                verified_files.append(vsf)
    sessions = group_logical_sessions(verified_files, sort_segments=False, meta_cache=meta_cache)
    sessions.sort(key=lambda s: s.mtime, reverse=True)
    if cold_rough:
        sessions = _pull_misnamed_cold_predecessors(sessions, cold_rough, meta_cache)
    return sessions, len(sessions)


def _extract_logical_preview(
    ls: LogicalSession,
    *,
    home: Path | None = None,
    meta_cache: dict[Any, dict[str, Any] | None] | None = None,
) -> str:
    if ls.agent == "codex" and len(ls.segments) > 1:
        chome = _codex_home(home)
        sqlite_path = codex_sessions.query_sqlite_rollout_path(ls.session_id, chome)
        effective, _, _ = codex_sessions.resolve_effective_lineage(
            ls.session_id,
            [s.path for s in ls.segments],
            sqlite_path=sqlite_path,
            codex_home=chome,
            meta_cache=meta_cache,
        )
        by_key = {_meta_cache_key(s.path): s for s in ls.segments}
        for seg in effective:
            p = seg["path"]
            sf = by_key.get(_meta_cache_key(p))
            if sf is None:
                try:
                    st = p.stat()
                    sf = SessionFile("codex", ls.session_id, p, st.st_mtime, st.st_size)
                except OSError:
                    continue
            preview = extract_preview(
                sf,
                max_ordinal_exclusive=seg.get("end_ordinal_exclusive"),
                max_byte_offset_exclusive=seg.get("end_byte_offset"),
            )
            if preview:
                return preview
        # Effective lineage is authoritative: if all opening prompts were rolled
        # back, return "" instead of falling back to superseded segments.
        return ""
    for seg in ls.segments:
        preview = extract_preview(seg)
        if preview:
            return preview
    return ""


def list_sessions(
    agents: list[str] | None = None,
    days: int = 14,
    scan_all: bool = False,
    limit: int = 20,
    home: Path | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    t0 = time.monotonic()
    norm_agents = normalize_agents(agents)
    meta_cache: dict[Any, dict[str, Any] | None] = {}
    all_files: list[SessionFile] = []
    for ag in norm_agents:
        for sf in discover_agent_files(ag, home=home, verify_headers=False):
            if not sf.is_subagent:
                all_files.append(sf)

    # First prune by logical-session mtime with zero header reads on cold files.
    rough_logical = group_logical_sessions(all_files, sort_segments=False)
    hot_rough, skipped = prune_by_time(rough_logical, days, scan_all, now=now)
    cold_rough = rough_logical[len(hot_rough) :] if skipped > 0 else []
    selected, total_hot = _verify_hot_logical_sessions(
        hot_rough, meta_cache=meta_cache, cold_rough=cold_rough, lazy_limit=limit
    )
    selected = selected[:limit]
    items: list[dict[str, Any]] = []
    for ls in selected:
        if len(ls.segments) > 1 and ls.agent == "codex":
            meta_pairs = [(_cached_codex_meta(sf.path, meta_cache), sf.path) for sf in ls.segments]
            by_path = {sf.path: sf for sf in ls.segments}
            ls = LogicalSession(
                agent=ls.agent,
                session_id=ls.session_id,
                segments=[by_path[p] for _, p in codex_sessions.sort_rollout_entries(meta_pairs)],
                is_subagent=ls.is_subagent,
                parent_id=ls.parent_id,
            )
        item: dict[str, Any] = {
            "agent": ls.agent,
            "session_id": ls.session_id,
            "mtime": iso_local(ls.mtime),
            "size_bytes": ls.size_bytes,
            "preview": _extract_logical_preview(ls, home=home, meta_cache=meta_cache),
            "path": str(ls.latest_segment.path),
            "segment_count": len(ls.segments),
        }
        if len(ls.segments) > 1:
            item["paths"] = [str(seg.path) for seg in ls.segments]
        items.append(item)
    return {
        "command": "list",
        "count": len(items),
        "truncated": total_hot > len(items),
        "scanned_scope": {
            "agents": norm_agents,
            "days": None if scan_all else days,
            "all": scan_all,
            "matched_sessions_in_window": total_hot,
            "skipped_cold_sessions": skipped,
            "elapsed_ms": int((time.monotonic() - t0) * 1000),
        },
        "sessions": items,
    }


def find_sessions(
    pattern: str,
    agents: list[str] | None = None,
    days: int = 14,
    scan_all: bool = False,
    limit: int = 20,
    fixed: bool = False,
    case_sensitive: bool = False,
    all_matches_per_session: bool = False,
    home: Path | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    t0 = time.monotonic()
    norm_agents = normalize_agents(agents)
    meta_cache: dict[Any, dict[str, Any] | None] = {}
    all_files: list[SessionFile] = []
    for ag in norm_agents:
        all_files.extend(discover_agent_files(ag, home=home, verify_headers=False))

    # Prune at logical-session granularity without opening cold files.
    rough_logical = group_logical_sessions(all_files, sort_segments=False)
    hot_rough, skipped_sessions = prune_by_time(rough_logical, days, scan_all, now=now)
    cold_rough = rough_logical[len(hot_rough) :] if skipped_sessions > 0 else []
    hot_logical, _ = _verify_hot_logical_sessions(
        hot_rough, meta_cache=meta_cache, cold_rough=cold_rough
    )
    logical_mtime_by_path: dict[str, float] = {}
    hot: list[SessionFile] = []
    for ls in hot_logical:
        lm = ls.mtime
        for sf in ls.segments:
            hot.append(sf)
            logical_mtime_by_path[str(sf.path)] = lm
    # Order search batches by logical-session recency so active sessions with hits
    # in older segments still rank ahead of stale sessions.
    hot.sort(key=lambda s: (logical_mtime_by_path.get(str(s.path), s.mtime), s.mtime), reverse=True)
    skipped = len(all_files) - len(hot)
    by_path: dict[str, SessionFile] = {str(sf.path): sf for sf in hot}

    hits: list[dict[str, Any]] = []
    seen_sessions: set[tuple[str, str]] = set()
    rg_bin = shutil.which("rg") or "/opt/homebrew/bin/rg"

    if hot:
        batch_size = 200
        for i in range(0, len(hot), batch_size):
            if len(hits) >= limit:
                break
            batch = [str(sf.path) for sf in hot[i : i + batch_size]]
            cmd = [rg_bin, "--json", "-z", "--no-heading", "--max-count", "3"]
            if fixed:
                cmd.append("-F")
            if not case_sensitive:
                cmd.append("-i")
            cmd.extend(["--", pattern, *batch])

            proc = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                errors="replace",
            )
            batch_hits: list[tuple[tuple[float, float], dict[str, Any]]] = []
            for raw in proc.stdout.splitlines():
                try:
                    ev = json.loads(raw)
                except Exception:
                    continue
                if ev.get("type") != "match":
                    continue
                data = ev.get("data") or {}
                path_str = (data.get("path") or {}).get("text")
                if not path_str or path_str not in by_path:
                    continue
                sf = by_path[path_str]
                line_num = data.get("line_number")
                line_text = (data.get("lines") or {}).get("text", "")
                subs = data.get("submatches") or []
                if subs:
                    b_start = subs[0].get("start", 0)
                    b_end = subs[0].get("end", b_start)
                    b_line = line_text.encode("utf-8", errors="replace")
                    c_start = len(b_line[:b_start].decode("utf-8", errors="replace"))
                    c_end = len(b_line[:b_end].decode("utf-8", errors="replace"))
                    snippet = window_snippet(line_text, c_start, c_end)
                else:
                    snippet = clean_preview(line_text, 240)

                rank_key = (logical_mtime_by_path.get(path_str, sf.mtime), sf.mtime)
                batch_hits.append(
                    (
                        rank_key,
                        {
                            "agent": sf.agent,
                            "session_id": sf.session_id,
                            "mtime": iso_local(sf.mtime),
                            "line": line_num,
                            "snippet": snippet,
                            "path": path_str,
                        },
                    )
                )
            batch_hits.sort(key=lambda x: x[0], reverse=True)
            for _, h in batch_hits:
                key = (h["agent"], h["session_id"])
                if not all_matches_per_session and key in seen_sessions:
                    continue
                seen_sessions.add(key)
                hits.append(h)
                if len(hits) >= limit:
                    break

    return {
        "command": "find",
        "pattern": pattern,
        "count": len(hits),
        "truncated": len(hits) >= limit,
        "scanned_scope": {
            "agents": norm_agents,
            "days": None if scan_all else days,
            "all": scan_all,
            "scanned_files": len(hot),
            "skipped_cold_files": skipped,
            "elapsed_ms": int((time.monotonic() - t0) * 1000),
        },
        "hits": hits,
    }


def read_session(
    session_id: str | None = None,
    agent: str | None = None,
    view: str = "timeline",
    include_children: bool = True,
    output_dir: str | None = None,
    max_events: int | None = None,
    full: bool = False,
    home: Path | None = None,
    pane: str | None = None,
    environment: dict[str, str] | None = None,
    include_superseded: bool = False,
) -> dict[str, Any]:
    resolved_pane_info: dict[str, Any] | None = None
    expected_active_path: Path | None = None
    pane_codex_home: Path | None = None
    unknown_pane_codex_home = False
    env = dict(os.environ if environment is None else environment)

    if pane:
        import live_sessions

        panes = live_sessions.list_live_panes(environment=env, home=home)
        target_pane = live_sessions.resolve_pane_spec(
            pane, panes, current_pane_id=env.get("TMUX_PANE", "")
        )
        if not target_pane.agent:
            raise ValueError(
                f"pane {target_pane.coord} ({target_pane.current_command}) is not a supported AI agent"
            )
        if not target_pane.session_id:
            raise FileNotFoundError(
                f"{target_pane.agent_alias or target_pane.agent} in {target_pane.coord} has no active session yet"
            )
        if agent:
            norm_explicit = normalize_agents([agent])[0]
            if norm_explicit != target_pane.agent:
                raise ValueError(
                    f"conflicting agent specifier: pane {target_pane.coord} runs {target_pane.agent!r}, got --agent {agent!r}"
                )
        session_id = target_pane.session_id
        agent = target_pane.agent
        if target_pane.active_path:
            expected_active_path = Path(target_pane.active_path).expanduser()
        if target_pane.agent == "codex":
            if target_pane.storage_root:
                pane_codex_home = Path(target_pane.storage_root).expanduser()
            elif expected_active_path is not None:
                inferred = codex_sessions.infer_codex_home(expected_active_path)
                if inferred:
                    pane_codex_home = Path(inferred)
                else:
                    # Do NOT fall back to default ~/.codex and risk mixing another
                    # CODEX_HOME's same-SID history with this pane's active file.
                    unknown_pane_codex_home = True
        resolved_pane_info = target_pane.as_dict()

    if not session_id:
        raise ValueError("session_id or --pane is required")

    session_id, agent = parse_session_ref(session_id, agent)
    meta_cache: dict[Any, dict[str, Any] | None] = {}
    rollout_index: dict[str, Path] = {}

    if unknown_pane_codex_home:
        sources: list[SessionFile] = []
        target_agent = "codex"
    else:
        try:
            sources = resolve_session_sources(
                session_id=session_id,
                agent=agent,
                include_children=include_children,
                home=home,
                codex_home=pane_codex_home,
                environment=env,
                meta_cache=meta_cache,
                rollout_index=rollout_index,
            )
        except FileNotFoundError:
            fallback_agent = normalize_agents([agent])[0] if agent else None
            if expected_active_path is not None and fallback_agent:
                sources = []
                target_agent = fallback_agent
            elif fallback_agent in (None, "codex"):
                chome = _codex_home(home, codex_home=pane_codex_home, environment=env)
                sqlite_cand = codex_sessions.query_sqlite_rollout_path(session_id, chome)
                alt = (
                    codex_sessions.resolve_alternate_rollout_path(
                        sqlite_cand, chome, rollout_index=rollout_index
                    )
                    if sqlite_cand is not None
                    else None
                )
                if alt is not None:
                    sources = []
                    target_agent = "codex"
                else:
                    raise
            else:
                raise
            # Even when primary was only recovered via SQLite/live anchor, still
            # discover standard-directory children when include_children=True.
            if target_agent == "codex" and include_children:
                chome = _codex_home(home, codex_home=pane_codex_home, environment=env)
                all_codex = discover_agent_files(
                    "codex", home=home, codex_home=chome, environment=env, verify_headers=False
                )
                _populate_rollout_index_from_files(chome, all_codex, rollout_index)
                sources.extend(
                    _collect_codex_children_by_sid([session_id], all_codex, set(), meta_cache)
                )
        else:
            target_agent = sources[0].agent

    complete, warnings, plans = _plan_session_sources(
        session_id,
        target_agent,
        sources,
        home=home,
        codex_home=pane_codex_home,
        unknown_codex_home=unknown_pane_codex_home,
        environment=env,
        expected_active_path=expected_active_path,
        include_superseded=(include_superseded or view == "export"),
        meta_cache=meta_cache,
        rollout_index=rollout_index,
    )
    if not plans:
        detail = f" ({'; '.join(warnings)})" if warnings else ""
        raise FileNotFoundError(f"session not found: {session_id}{detail}")

    planned_sources = [p.sf for p in plans]

    if view == "export":
        if output_dir:
            out_dir = Path(output_dir)
        else:
            stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
            safe_id = session_id.replace("/", "_")
            out_dir = Path(f"/tmp/aipane-sessions-{safe_id}-{stamp}")
        manifest = export_sources(
            session_id,
            planned_sources,
            out_dir,
            include_children,
            complete=complete,
            warnings=warnings,
            source_plans=plans,
        )
        payload: dict[str, Any] = {
            "command": "read",
            "view": "export",
            "output_dir": str(out_dir.resolve()),
            "manifest_path": str((out_dir / "manifest.json").resolve()),
            "complete": manifest["complete"],
            "warnings": manifest["warnings"],
            "manifest": manifest,
        }
        if resolved_pane_info is not None:
            payload["resolved_pane"] = resolved_pane_info
        return payload

    effective_max = max_events if max_events is not None else (None if full else 120)
    all_sources = []
    for plan in plans:
        sf = plan.sf
        # Never clear fork cutoffs on inherited parent prefixes even under --include-superseded:
        # parent messages written after the fork never belonged to the forked session.
        preserve_bounds = plan.role == "inherited_prefix"
        bound_ord = plan.end_ordinal_exclusive if (preserve_bounds or not include_superseded) else None
        bound_byte = plan.end_byte_offset if (preserve_bounds or not include_superseded) else None
        try:
            evs, records, superseded_count, malformed_count, parse_warnings = _parse_timeline_events_with_stats(
                sf.agent,
                sf.path,
                full=full,
                max_ordinal_exclusive=bound_ord,
                max_byte_offset_exclusive=bound_byte,
                expected_sid=sf.session_id,
            )
        except (OSError, RuntimeError) as exc:
            complete = False
            warnings.append(f"unreadable_source: {sf.path} ({exc})")
            continue
        if parse_warnings:
            complete = False
            warnings.extend(parse_warnings)
            if any("source_sid_changed_during_read" in w for w in parse_warnings):
                continue
        if malformed_count > 0:
            complete = False
            warnings.append(
                f"malformed_record_lines: skipped {malformed_count} malformed/truncated line(s) in {sf.path}"
            )
        if effective_max is not None and effective_max > 0:
            truncated = len(evs) > effective_max
            out_events = evs[:effective_max]
        else:
            truncated = False
            out_events = evs
        src_entry: dict[str, Any] = {
            "agent": sf.agent,
            "session_id": sf.session_id,
            "parent": sf.parent_id,
            "source_path": str(sf.path),
            "records": records,
            "total_timeline_events": len(evs),
            "truncated": truncated,
            "events": out_events,
        }
        if plan.role != "effective":
            src_entry["role"] = plan.role
        if bound_ord is not None:
            src_entry["superseded_from_ordinal"] = bound_ord
            src_entry["superseded_records"] = superseded_count
        if bound_byte is not None:
            src_entry["end_byte_offset"] = bound_byte
        if plan.inherited_ranges:
            src_entry["inherited_ranges"] = plan.inherited_ranges
        all_sources.append(src_entry)
    result: dict[str, Any] = {
        "command": "read",
        "view": "timeline",
        "full": full,
        "session_id": session_id,
        "complete": complete,
        "warnings": warnings,
        "sources": all_sources,
    }
    if resolved_pane_info is not None:
        result["resolved_pane"] = resolved_pane_info
    return result


def list_live_pane_sessions(
    *,
    scope: str = "window",
    pane: str | None = None,
    include_non_agents: bool = False,
    home: Path | None = None,
    environment: dict[str, str] | None = None,
) -> dict[str, Any]:
    import live_sessions

    t0 = time.monotonic()
    env = dict(os.environ if environment is None else environment)
    current_pane_id = env.get("TMUX_PANE", "")
    all_panes = live_sessions.list_live_panes(environment=env, home=home)

    current = (
        next((p for p in all_panes if p.pane_id == current_pane_id), None)
        if current_pane_id
        else next((p for p in all_panes if p.window_active and p.pane_active), None)
    )

    if pane:
        selected = [live_sessions.resolve_pane_spec(pane, all_panes, current_pane_id=current_pane_id)]
        effective_scope = "pane"
    elif scope == "all" or current is None:
        selected = list(all_panes)
        effective_scope = "all"
    elif scope == "siblings":
        selected = [
            p
            for p in all_panes
            if p.session_name == current.session_name
            and p.window_id == current.window_id
            and p.pane_id != current.pane_id
        ]
        effective_scope = "siblings"
    else:
        selected = [
            p
            for p in all_panes
            if p.session_name == current.session_name and p.window_id == current.window_id
        ]
        effective_scope = "window"

    if not include_non_agents and not pane:
        selected = [p for p in selected if p.agent]

    return {
        "command": "panes",
        "scope": effective_scope,
        "current_pane": current.as_dict() if current else None,
        "count": len(selected),
        "elapsed_ms": int((time.monotonic() - t0) * 1000),
        "panes": [p.as_dict() for p in selected],
    }


def _tmux_display_message(message: str, environment: dict[str, str], duration_ms: int = 2500) -> None:
    import live_sessions

    cmd = [*live_sessions._tmux_command(environment), "display-message", "-d", str(duration_ms), "--", message]
    try:
        subprocess.run(cmd, env=environment, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=2, check=False)
    except (OSError, subprocess.SubprocessError):
        pass


def yank_pane_session(
    target: str | None = None,
    *,
    fmt: str = "ref",
    display: bool = True,
    home: Path | None = None,
    environment: dict[str, str] | None = None,
) -> tuple[dict[str, Any], int]:
    import live_sessions

    env = dict(os.environ if environment is None else environment)
    lookup_target = target or env.get("TMUX_PANE") or None
    if lookup_target and (lookup_target.startswith("%") or ":" in lookup_target):
        panes = live_sessions.list_live_panes(target=lookup_target, environment=env, home=home)
        item = panes[0] if panes else None
    else:
        panes = live_sessions.list_live_panes(environment=env, home=home)
        if lookup_target:
            item = live_sessions.resolve_pane_spec(
                lookup_target, panes, current_pane_id=env.get("TMUX_PANE", "")
            )
        else:
            item = next((p for p in panes if p.window_active and p.pane_active), None)

    if item is None:
        msg = f"No live tmux pane found for {target or 'current pane'}"
        if display:
            _tmux_display_message(msg, env)
        return {"command": "yank", "ok": False, "error": msg}, 1

    if not item.agent:
        msg = f"No AI agent in {item.coord} ({item.current_command})"
        if display:
            _tmux_display_message(msg, env)
        return {"command": "yank", "ok": False, "pane": item.as_dict(), "error": msg}, 1

    if not item.session_id:
        tag = item.agent_alias or item.agent
        msg = f"{tag} in {item.coord} has no active session yet"
        if display:
            _tmux_display_message(msg, env)
        return {"command": "yank", "ok": False, "pane": item.as_dict(), "error": msg}, 1

    tag = item.agent_alias or item.agent
    payload_text = (
        f"{tag}:{item.session_id}" if (fmt == "ref" and tag) else item.session_id
    )
    sinks = live_sessions.copy_text_to_clipboard(payload_text, environment=env)
    if not sinks:
        msg = "Failed to copy session ID to clipboard or tmux buffer"
        if display:
            _tmux_display_message(msg, env)
        return {"command": "yank", "ok": False, "pane": item.as_dict(), "error": msg}, 1

    banner = live_sessions.format_yank_banner(item)
    if display:
        _tmux_display_message(banner, env)
    return {
        "command": "yank",
        "ok": True,
        "copied": payload_text,
        "sinks": sinks,
        "banner": banner,
        "pane": item.as_dict(),
    }, 0


def _extract_turn_from_record(agent: str, rec: dict[str, Any], line_no: int) -> dict[str, Any] | None:
    """Extract a single compact user/assistant preview turn from a decoded JSONL record."""
    if agent == "codex":
        if rec.get("type") == "response_item":
            p = rec.get("payload")
            if isinstance(p, dict) and p.get("type") == "message" and p.get("role") in ("user", "assistant"):
                txt = extract_text_from_content(p.get("content"))
                if p.get("role") == "user" and is_noise_user_text(txt):
                    return None
                if txt.strip():
                    return {"line": line_no, "kind": p["role"], "text": clean_preview(txt, 300)}
    elif agent == "antigravity":
        t = rec.get("type")
        if t == "USER_INPUT":
            txt = str(rec.get("content", ""))
            m = re.search(r"<USER_REQUEST>\s*(.*?)\s*</USER_REQUEST>", txt, re.S)
            if m:
                txt = m.group(1)
            if txt.strip():
                return {"line": line_no, "kind": "user", "text": clean_preview(txt, 300)}
        elif t == "PLANNER_RESPONSE":
            txt = str(rec.get("content", ""))
            if txt.strip():
                return {"line": line_no, "kind": "assistant", "text": clean_preview(txt, 300)}
    elif agent == "claude":
        t = rec.get("type")
        msg = rec.get("message")
        if isinstance(msg, dict):
            if t == "user" and not rec.get("isMeta"):
                txt = extract_text_from_content(msg.get("content"))
                if txt.strip() and not is_noise_user_text(txt):
                    return {"line": line_no, "kind": "user", "text": clean_preview(txt, 300)}
            elif t == "assistant":
                txt = extract_text_from_content(msg.get("content") or [])
                if txt.strip():
                    return {"line": line_no, "kind": "assistant", "text": clean_preview(txt, 300)}
    elif agent == "pi":
        if rec.get("type") == "message":
            msg = rec.get("message")
            if isinstance(msg, dict) and msg.get("role") in ("user", "assistant"):
                txt = extract_text_from_content(msg.get("content"))
                if msg.get("role") == "user" and is_noise_user_text(txt):
                    return None
                if txt.strip():
                    return {"line": line_no, "kind": msg["role"], "text": clean_preview(txt, 300)}
    elif agent == "dsh":
        t = rec.get("type")
        data = rec.get("data")
        if isinstance(data, dict):
            if t == "user/message":
                src = data.get("source")
                if isinstance(src, dict) and src.get("kind") == "user":
                    txt = extract_text_from_content(data.get("content"))
                    if txt.strip() and not is_noise_user_text(txt):
                        return {"line": line_no, "kind": "user", "text": clean_preview(txt, 300)}
            elif t == "assistant/message":
                raw_msg = data.get("message")
                msg = raw_msg if isinstance(raw_msg, dict) else data
                txt = extract_text_from_content(msg.get("content"))
                if txt.strip():
                    return {"line": line_no, "kind": "assistant", "text": clean_preview(txt, 300)}
    elif agent == "kimi":
        t = rec.get("type")
        if t == "context.append_message":
            msg = rec.get("message")
            if isinstance(msg, dict) and msg.get("role") in ("user", "assistant"):
                txt = extract_text_from_content(msg.get("content"))
                if txt.strip() and (msg.get("role") != "user" or not is_noise_user_text(txt)):
                    return {"line": line_no, "kind": msg["role"], "text": clean_preview(txt, 300)}
        elif t == "context.append_loop_event":
            ev = rec.get("event")
            if isinstance(ev, dict) and ev.get("type") == "content.part":
                part = ev.get("part")
                if isinstance(part, dict) and part.get("type") == "text":
                    txt = str(part.get("text") or "")
                    if txt.strip():
                        return {"line": line_no, "kind": "assistant", "text": clean_preview(txt, 300)}
        else:
            role = rec.get("role")
            if role in ("user", "assistant"):
                txt = extract_text_from_content(rec.get("content"))
                if txt.strip() and (role != "user" or not is_noise_user_text(txt)):
                    return {"line": line_no, "kind": role, "text": clean_preview(txt, 300)}
    return None


def _read_bounded_head_turns(
    plan: _SourcePlan,
    limit: int,
    *,
    max_lines: int = 4000,
) -> tuple[list[dict[str, Any]], bool]:
    """Read up to `limit` opening turns, returning `(turns, hit_eof)`."""
    sf = plan.sf
    if sf.agent == "grok":
        evs, _, _, _, _ = _parse_timeline_events_with_stats(sf.agent, sf.path, full=False)
        turns = [e for e in evs if e.get("kind") in ("user", "assistant")]
        return turns[:limit], len(turns) <= limit

    turns: list[dict[str, Any]] = []
    byte_offset = 0
    hit_eof = True
    for idx, raw in enumerate(iter_raw_bytes_lines(sf.path), 1):
        line_start = byte_offset
        byte_offset += len(raw)
        if plan.end_byte_offset is not None and line_start >= plan.end_byte_offset:
            break
        if plan.end_byte_offset is not None and line_start < plan.end_byte_offset < byte_offset:
            break
        if idx > max_lines:
            hit_eof = False
            break
        line = raw.decode("utf-8", errors="replace").strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except Exception:
            continue
        if not isinstance(rec, dict):
            continue
        if (
            sf.agent == "codex"
            and plan.end_ordinal_exclusive is not None
            and isinstance(rec.get("ordinal"), int)
            and not isinstance(rec.get("ordinal"), bool)
            and rec["ordinal"] >= plan.end_ordinal_exclusive
        ):
            continue
        ev = _extract_turn_from_record(sf.agent, rec, idx)
        if ev is not None:
            turns.append(ev)
            if len(turns) >= limit:
                hit_eof = False
                break
    return turns, hit_eof


def _read_bounded_tail_turns(
    plan: _SourcePlan,
    limit: int,
    *,
    chunk_bytes: int = 512 * 1024,
    max_scan_bytes: int = 16 * 1024 * 1024,
) -> tuple[list[dict[str, Any]], bool]:
    """Read up to `limit` latest effective turns, returning `(turns, truncated_by_budget)`."""
    sf = plan.sf
    if limit <= 0:
        return [], False

    if sf.agent == "grok":
        evs, _, _, _, _ = _parse_timeline_events_with_stats(
            sf.agent,
            sf.path,
            full=False,
            max_ordinal_exclusive=plan.end_ordinal_exclusive,
            max_byte_offset_exclusive=plan.end_byte_offset,
        )
        turns = [e for e in evs if e.get("kind") in ("user", "assistant")]
        return turns[-limit:], False

    if _is_zstd_path(sf.path):
        # Keep a fixed-size deque of trailing raw lines so compressed logs do not
        # JSON-decode tens of thousands of earlier records just to show the last 5 turns.
        ring: deque[tuple[int, bytes]] = deque(maxlen=max(limit * 32, 256))
        byte_offset = 0
        try:
            for idx, raw in enumerate(iter_raw_bytes_lines(sf.path), 1):
                line_start = byte_offset
                byte_offset += len(raw)
                if plan.end_byte_offset is not None and line_start >= plan.end_byte_offset:
                    break
                if plan.end_byte_offset is not None and line_start < plan.end_byte_offset < byte_offset:
                    break
                if raw.strip():
                    ring.append((idx, raw))
        except (OSError, RuntimeError):
            return [], False
        rev_turns: list[dict[str, Any]] = []
        for idx, raw in reversed(ring):
            line = raw.decode("utf-8", errors="replace").strip()
            try:
                rec = json.loads(line)
            except Exception:
                continue
            if not isinstance(rec, dict):
                continue
            if (
                sf.agent == "codex"
                and plan.end_ordinal_exclusive is not None
                and isinstance(rec.get("ordinal"), int)
                and not isinstance(rec.get("ordinal"), bool)
                and rec["ordinal"] >= plan.end_ordinal_exclusive
            ):
                continue
            ev = _extract_turn_from_record(sf.agent, rec, idx)
            if ev is not None:
                rev_turns.append(ev)
                if len(rev_turns) >= limit:
                    break
        rev_turns.reverse()
        return rev_turns, False

    try:
        file_size = sf.path.stat().st_size
    except OSError:
        return [], False
    end_pos = min(file_size, plan.end_byte_offset) if plan.end_byte_offset is not None else file_size
    if end_pos <= 0:
        return [], False

    # Step backwards in bounded chunks so a huge trailing metadata/screenshot line
    # (e.g. 5 MiB token_count) is stepped cleanly without parsing the entire file.
    rev_turns = []
    scanned = 0
    pos = end_pos
    pending = b""
    truncated_by_budget = False
    try:
        with sf.path.open("rb") as fh:
            while pos > 0 and len(rev_turns) < limit:
                if scanned >= max_scan_bytes:
                    truncated_by_budget = True
                    break
                step = min(chunk_bytes, pos, max_scan_bytes - scanned)
                pos -= step
                scanned += step
                fh.seek(pos)
                data = fh.read(step) + pending
                parts = data.split(b"\n")
                pending = parts[0]
                for raw in reversed(parts[1:]):
                    stripped_b = raw.strip()
                    if not stripped_b:
                        continue
                    try:
                        rec = json.loads(stripped_b.decode("utf-8", errors="replace"))
                    except Exception:
                        continue
                    if not isinstance(rec, dict):
                        continue
                    if (
                        sf.agent == "codex"
                        and plan.end_ordinal_exclusive is not None
                        and isinstance(rec.get("ordinal"), int)
                        and not isinstance(rec.get("ordinal"), bool)
                        and rec["ordinal"] >= plan.end_ordinal_exclusive
                    ):
                        continue
                    ev = _extract_turn_from_record(sf.agent, rec, 0)
                    if ev is not None:
                        rev_turns.append(ev)
                        if len(rev_turns) >= limit:
                            break
            if pos == 0 and len(rev_turns) < limit and pending.strip():
                try:
                    rec = json.loads(pending.decode("utf-8", errors="replace").strip())
                    if isinstance(rec, dict) and not (
                        sf.agent == "codex"
                        and plan.end_ordinal_exclusive is not None
                        and isinstance(rec.get("ordinal"), int)
                        and not isinstance(rec.get("ordinal"), bool)
                        and rec["ordinal"] >= plan.end_ordinal_exclusive
                    ):
                        ev = _extract_turn_from_record(sf.agent, rec, 1)
                        if ev is not None:
                            rev_turns.append(ev)
                except Exception:
                    pass
    except OSError:
        return [], False

    rev_turns.reverse()
    return rev_turns[-limit:], (truncated_by_budget and len(rev_turns) < limit)


def render_preview(
    *,
    kind: str,
    pane_id: str = "",
    agent: str = "",
    session_id: str = "",
    lines: int = 38,
    home: Path | None = None,
    environment: dict[str, str] | None = None,
) -> str:
    """Render ANSI-friendly human preview text for the fzf session picker."""
    import live_sessions

    env = dict(os.environ if environment is None else environment)
    dim = "\033[2m"
    bold = "\033[1m"
    cyan = "\033[36m"
    green = "\033[32m"
    yellow = "\033[33m"
    reset = "\033[0m"

    header_parts: list[str] = []
    if kind == "live" and pane_id and pane_id != "-":
        panes = live_sessions.list_live_panes(target=pane_id, environment=env, home=home)
        p = panes[0] if panes else None
        if p:
            tag = p.agent_alias or p.agent or p.current_command
            header_parts.append(f"{bold}{cyan}{tag}{reset}  {bold}{p.coord}{reset} [{p.window_name}]")
            if p.title:
                header_parts.append(f"{bold}Title:{reset} {p.title}")
            header_parts.append(f"{dim}SID:   {p.session_id or '(none)'}  ({p.authority or 'n/a'}){reset}")
            header_parts.append(f"{dim}CWD:   {p.cwd}{reset}")
        else:
            header_parts.append(f"{bold}{cyan}{agent}{reset}  {pane_id}")
            if session_id:
                header_parts.append(f"{dim}SID:   {session_id}{reset}")
        header_parts.append(f"{dim}{'─' * 56}{reset}")

        cap_cmd = [*live_sessions._tmux_command(env), "capture-pane", "-ep", "-t", pane_id]
        try:
            proc = subprocess.run(cap_cmd, capture_output=True, text=True, env=env, timeout=2, check=False)
            if proc.returncode == 0:
                raw_lines = proc.stdout.splitlines()
                while raw_lines and not re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", raw_lines[-1]).strip():
                    raw_lines.pop()
                body = raw_lines[-lines:] if raw_lines else [f"{dim}(empty screen){reset}"]
            else:
                body = [f"{dim}(pane capture unavailable){reset}"]
        except (OSError, subprocess.SubprocessError):
            body = [f"{dim}(pane capture timed out){reset}"]
        return "\n".join([*header_parts, *body])

    # Disk / IDE session preview
    sid_clean, norm_agent = parse_session_ref(session_id, agent or None)
    label = f"{agent or norm_agent or 'session'}  {sid_clean}"
    header_parts.append(f"{bold}{cyan}{label}{reset}")
    header_parts.append(f"{dim}{'─' * 56}{reset}")
    try:
        meta_cache: dict[Any, dict[str, Any] | None] = {}
        rollout_index: dict[str, Path] = {}
        try:
            primary_sources = resolve_session_sources(
                session_id=sid_clean,
                agent=norm_agent,
                include_children=False,
                home=home,
                environment=env,
                meta_cache=meta_cache,
                rollout_index=rollout_index,
            )
            target_agent = primary_sources[0].agent
        except FileNotFoundError:
            fallback_agent = normalize_agents([norm_agent])[0] if norm_agent else None
            if fallback_agent in (None, "codex"):
                chome = _codex_home(home, environment=env)
                sqlite_cand = codex_sessions.query_sqlite_rollout_path(sid_clean, chome)
                alt = (
                    codex_sessions.resolve_alternate_rollout_path(sqlite_cand, chome, rollout_index=rollout_index)
                    if sqlite_cand is not None
                    else None
                )
                if alt is not None:
                    primary_sources = []
                    target_agent = "codex"
                else:
                    raise
            else:
                raise
        _, _, plans = _plan_session_sources(
            sid_clean,
            target_agent,
            primary_sources,
            home=home,
            environment=env,
            include_superseded=False,
            meta_cache=meta_cache,
            rollout_index=rollout_index,
        )
        body_lines: list[str] = []
        shown: list[dict[str, Any]] = []
        tail_truncated = False
        if len(plans) == 1:
            head_turns, hit_eof = _read_bounded_head_turns(plans[0], 11)
            if hit_eof:
                shown = head_turns
                if len(shown) > 10:
                    shown = [*shown[:3], {"kind": "gap", "text": f"… {len(shown) - 8} turns omitted …"}, *shown[-5:]]
            else:
                tail_turns, tail_truncated = _read_bounded_tail_turns(plans[0], 5)
                gap_msg = "… tail preview truncated (large non-text records) …" if tail_truncated else "… middle turns omitted …"
                shown = [*head_turns[:3], {"kind": "gap", "text": gap_msg}, *tail_turns]
        elif plans:
            head_turns = []
            hit_eof_all = True
            for plan in plans:
                need = 11 - len(head_turns)
                if need <= 0:
                    hit_eof_all = False
                    break
                seg_head, seg_eof = _read_bounded_head_turns(plan, need)
                head_turns.extend(seg_head)
                if not seg_eof:
                    hit_eof_all = False
                    break

            if hit_eof_all:
                if len(head_turns) <= 10:
                    shown = head_turns
                else:
                    shown = [
                        *head_turns[:3],
                        {"kind": "gap", "text": f"… {len(head_turns) - 8} turns omitted …"},
                        *head_turns[-5:],
                    ]
            else:
                tail_turns = []
                for plan in reversed(plans):
                    need = 5 - len(tail_turns)
                    if need <= 0:
                        break
                    seg_tail, seg_trunc = _read_bounded_tail_turns(plan, need)
                    if seg_trunc:
                        tail_truncated = True
                    tail_turns = [*seg_tail, *tail_turns]
                gap_text = (
                    f"… across {len(plans)} segments (tail preview truncated) …"
                    if tail_truncated
                    else f"… across {len(plans)} segments …"
                )
                shown = [*head_turns[:3], {"kind": "gap", "text": gap_text}, *tail_turns[-5:]]

        for ev in shown:
            ek = ev.get("kind")
            txt = str(ev.get("text") or "").strip()
            if ek == "user":
                body_lines.append(f"{bold}{green}USER{reset}  {txt}")
            elif ek == "assistant":
                body_lines.append(f"{bold}{yellow}ASST{reset}  {txt}")
            elif ek == "gap":
                body_lines.append(f"{dim}{txt}{reset}")
        if not body_lines:
            body_lines.append(f"{dim}(no text messages recorded yet){reset}")
    except Exception as exc:
        body_lines = [f"{dim}(preview unavailable: {exc}){reset}"]
    return "\n".join([*header_parts, *body_lines])


def audit_session_storage_drift(
    *,
    home: Path | None = None,
    days: int = 14,
    environment: dict[str, str] | None = None,
    check_live_panes: bool = False,
    now: float | None = None,
) -> dict[str, Any]:
    """Read-only storage-layout drift check for aipane-doctor.

    Independently compares filename-derived IDs against authoritative headers,
    flags unreadable/oversized/malformed headers and SQLite query failures,
    verifies Codex state_*.sqlite rollout coverage (including compressed or
    archived counterparts and missing recent rows), validates Pi headers, and
    optionally checks live tmux panes against their own storage_root.
    """
    env = dict(os.environ if environment is None else environment)
    resolved_home = (home or Path(env.get("HOME", str(Path.home())))).expanduser()
    issues: list[dict[str, str]] = []
    meta_cache: dict[Any, dict[str, Any] | None] = {}

    codex_home = _codex_home(resolved_home, environment=env)
    codex_files = discover_agent_files(
        "codex",
        home=resolved_home,
        codex_home=codex_home,
        environment=env,
        verify_headers=False,
    )
    hot_codex, _ = prune_by_time(list(codex_files), days, False, now=now)

    codex_by_resolved: dict[Path, SessionFile] = {}
    codex_by_canonical: dict[Path, SessionFile] = {}
    rollout_index: dict[str, Path] = {}
    _populate_rollout_index_from_files(codex_home, codex_files, rollout_index)
    for sf in codex_files:
        codex_by_resolved[_path_cache_key(sf.path)] = sf
        codex_by_canonical[codex_sessions.rollout_canonical_key(sf.path, codex_home=codex_home)] = sf

    for sf in hot_codex:
        fn_sid, _ = codex_sessions.extract_filename_ids(sf.path)
        meta, err = codex_sessions.read_rollout_meta_detailed(sf.path, max_lines=1)
        if err is not None or meta is None:
            issues.append(
                {
                    "severity": "error",
                    "code": f"codex_header_{err or 'missing_meta'}",
                    "message": f"{sf.path}: failed to verify Codex session_meta header ({err or 'missing_meta'})",
                }
            )
            continue
        meta_cache[_meta_cache_key(sf.path)] = meta
        if fn_sid is not None and meta["sid"] != fn_sid:
            issues.append(
                {
                    "severity": "error",
                    "code": "codex_sid_drift",
                    "message": (
                        f"{sf.path}: filename session_id={fn_sid} "
                        f"differs from session_meta.payload.id={meta['sid']}"
                    ),
                }
            )

    recent_sqlite, sqlite_errors = codex_sessions.query_recent_sqlite_rollouts_detailed(
        codex_home, days=days, now=now
    )
    for err_msg in sqlite_errors:
        issues.append(
            {
                "severity": "error",
                "code": "codex_sqlite_query_failed",
                "message": f"Codex state sqlite query failed: {err_msg}",
            }
        )

    checked_sqlite = 0
    for tid, rpath in recent_sqlite:
        checked_sqlite += 1
        actual = codex_sessions.resolve_alternate_rollout_path(
            rpath, codex_home, rollout_index=rollout_index
        )
        if actual is None:
            issues.append(
                {
                    "severity": "error",
                    "code": "codex_sqlite_rollout_missing",
                    "message": (
                        f"Codex state sqlite thread {tid} points to missing rollout {rpath} "
                        "(no plain, compressed, or archived counterpart found)"
                    ),
                }
            )
            continue
        r_resolved = _path_cache_key(actual)
        r_canonical = codex_sessions.rollout_canonical_key(actual, codex_home=codex_home)
        matched = codex_by_resolved.get(r_resolved) or codex_by_canonical.get(r_canonical)
        if matched is None:
            issues.append(
                {
                    "severity": "error",
                    "code": "codex_sqlite_rollout_uncovered",
                    "message": (
                        f"Codex state sqlite thread {tid} points to existing file {actual} "
                        "not discovered by aipane-sessions"
                    ),
                }
            )
            continue
        meta = _cached_codex_meta(matched.path, meta_cache)
        actual_sid = meta["sid"] if meta else matched.session_id
        if actual_sid != tid:
            issues.append(
                {
                    "severity": "error",
                    "code": "codex_sqlite_sid_mismatch",
                    "message": (
                        f"Codex state sqlite thread {tid} rollout {actual} "
                        f"has authoritative session_id={actual_sid}"
                    ),
                }
            )

    pi_files = discover_agent_files("pi", home=resolved_home, environment=env, verify_headers=False)
    hot_pi, _ = prune_by_time(list(pi_files), days, False, now=now)
    for sf in hot_pi:
        fn_uuids = UUID_RE.findall(sf.path.stem)
        fn_sid = fn_uuids[0] if fn_uuids else None
        hdr = read_first_json(sf.path)
        if _is_dsh_session_header(hdr):
            continue
        if not hdr or hdr.get("type") != "session" or not isinstance(hdr.get("id"), str) or not hdr.get("id"):
            issues.append(
                {
                    "severity": "error",
                    "code": "pi_header_invalid",
                    "message": f"{sf.path}: missing or invalid Pi session header",
                }
            )
            continue
        hdr_id = hdr["id"]
        if fn_sid and hdr_id != fn_sid:
            issues.append(
                {
                    "severity": "error",
                    "code": "pi_sid_drift",
                    "message": (
                        f"{sf.path}: filename session_id={fn_sid} "
                        f"differs from session header id={hdr_id}"
                    ),
                }
            )

    checked_live = 0
    if check_live_panes:
        import live_sessions

        panes = live_sessions.list_live_panes(
            only_agents=True, environment=env, home=resolved_home
        )
        codex_root_maps: dict[Path, dict[Path, SessionFile]] = {
            _path_cache_key(codex_home): codex_by_resolved
        }
        other_agent_maps: dict[str, dict[Path, SessionFile]] = {}

        for pane in panes:
            if not pane.active_path:
                continue
            ap = Path(pane.active_path).expanduser()
            if not ap.is_file():
                continue
            checked_live += 1
            ap_res = _path_cache_key(ap)
            if pane.agent == "codex":
                pane_chome = (
                    Path(pane.storage_root).expanduser()
                    if pane.storage_root
                    else codex_home
                )
                chome_key = _path_cache_key(pane_chome)
                if chome_key not in codex_root_maps:
                    root_map: dict[Path, SessionFile] = {}
                    for sf in discover_agent_files(
                        "codex",
                        home=resolved_home,
                        codex_home=pane_chome,
                        environment=env,
                        verify_headers=False,
                    ):
                        root_map[_path_cache_key(sf.path)] = sf
                    codex_root_maps[chome_key] = root_map
                matched = codex_root_maps[chome_key].get(ap_res)
                if matched is not None:
                    meta = _cached_codex_meta(matched.path, meta_cache)
                    matched_sid = meta["sid"] if meta else matched.session_id
                else:
                    matched_sid = ""
            else:
                if pane.agent not in other_agent_maps:
                    agent_map: dict[Path, SessionFile] = {}
                    for sf in discover_agent_files(
                        pane.agent, home=resolved_home, environment=env, verify_headers=True
                    ):
                        agent_map[_path_cache_key(sf.path)] = sf
                    other_agent_maps[pane.agent] = agent_map
                matched = other_agent_maps[pane.agent].get(ap_res)
                matched_sid = matched.session_id if matched is not None else ""

            if matched is None:
                issues.append(
                    {
                        "severity": "error",
                        "code": "live_pane_active_path_uncovered",
                        "message": (
                            f"Live pane {pane.coord} ({pane.agent}:{pane.session_id}) writes {ap} "
                            "which is not covered by static discovery"
                        ),
                    }
                )
            elif pane.session_id and matched_sid != pane.session_id:
                issues.append(
                    {
                        "severity": "error",
                        "code": "live_pane_sid_mismatch",
                        "message": (
                            f"Live pane {pane.coord} active file {ap} resolved to {pane.session_id} "
                            f"via runtime but {matched_sid} via static discovery"
                        ),
                    }
                )

    return {
        "checked_codex_files": len(hot_codex),
        "checked_sqlite_rollouts": checked_sqlite,
        "checked_pi_files": len(hot_pi),
        "checked_live_panes": checked_live,
        "issues": issues,
    }


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="aipane-sessions",
        description="Zero-index local session discovery, live tmux topology, search, and export across 7 AI CLIs.",
    )
    sub = p.add_subparsers(dest="subcmd", required=True)

    p_panes = sub.add_parser("panes", help="List live tmux panes with resolved AI session IDs")
    scope_group = p_panes.add_mutually_exclusive_group()
    scope_group.add_argument("--window", action="store_true", help="List panes in the current tmux window (default)")
    scope_group.add_argument("--siblings", action="store_true", help="List other AI panes in the current tmux window")
    scope_group.add_argument("--all", action="store_true", help="List all live AI panes across the tmux server")
    p_panes.add_argument("--pane", help="Resolve one pane specifier ('.', '1', '4.1', '0:4.1', '%%510')")
    p_panes.add_argument("--include-non-agents", action="store_true", help="Include non-AI panes (shell, yazi, etc.)")

    p_list = sub.add_parser("list", help="List recent sessions by mtime")
    p_list.add_argument("--agent", action="append", help="Filter by agent (repeatable or comma-separated)")
    p_list.add_argument("--days", type=int, default=14, help="Lookback window in days (default: 14)")
    p_list.add_argument("--all", action="store_true", help="Include all historical sessions")
    p_list.add_argument("--limit", type=int, default=20, help="Max sessions to return (default: 20)")

    p_find = sub.add_parser("find", help="Search session content with time-window pruning")
    p_find.add_argument("pattern", help="Regex pattern (or literal with --fixed)")
    p_find.add_argument("--agent", action="append", help="Filter by agent (repeatable or comma-separated)")
    p_find.add_argument("--days", type=int, default=14, help="Lookback window in days (default: 14)")
    p_find.add_argument("--all", action="store_true", help="Scan full cold history")
    p_find.add_argument("--limit", type=int, default=20, help="Max hits to return (default: 20)")
    p_find.add_argument("--fixed", action="store_true", help="Treat pattern as literal string (-F)")
    p_find.add_argument("--case-sensitive", action="store_true", help="Case-sensitive match")
    p_find.add_argument("--all-matches-per-session", action="store_true", help="Return multiple hits per session")

    p_read = sub.add_parser("read", help="Read timeline or export full logs for a session ID or live pane")
    p_read.add_argument("session_id", nargs="?", help="Exact session ID, '<agent>:<id>', or omit with --pane")
    p_read.add_argument("--pane", help="Resolve live tmux pane ('.', '1', '4.1', '0:4.1', '%%510') instead of passing session_id")
    p_read.add_argument("--agent", help="Optional agent disambiguator")
    p_read.add_argument("--view", choices=("timeline", "export"), default="timeline", help="Output mode (default: timeline)")
    p_read.add_argument("--full", action="store_true", help="Include full untruncated timeline text and all events")
    p_read.add_argument("--no-children", action="store_true", help="Do not follow child sessions")
    p_read.add_argument(
        "--include-superseded",
        action="store_true",
        help="Include rolled-back Codex records superseded by a later rollout segment",
    )
    p_read.add_argument("--output-dir", help="Target export directory (must not exist)")
    p_read.add_argument(
        "--max-events",
        type=int,
        default=None,
        help="Max timeline events per source (default: 120; unlimited with --full)",
    )

    p_yank = sub.add_parser("yank", help="Copy a live tmux pane's current AI session ref ('<agent>:<id>') to the clipboard")
    p_yank.add_argument("target", nargs="?", help="Optional pane ID or coordinate (defaults to current pane)")
    p_yank.add_argument("--format", choices=("ref", "raw"), default="ref", help="Copy '<agent>:<id>' or raw ID (default: ref)")
    p_yank.add_argument("--no-display", action="store_true", help="Suppress tmux status-line confirmation message")

    p_preview = sub.add_parser("preview", help=argparse.SUPPRESS)
    p_preview.add_argument("--kind", choices=("live", "disk"), default="live")
    p_preview.add_argument("--pane", default="")
    p_preview.add_argument("--agent", default="")
    p_preview.add_argument("--sid", default="")
    p_preview.add_argument("--lines", type=int, default=38)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.subcmd == "panes":
            scope = "all" if args.all else ("siblings" if args.siblings else "window")
            result = list_live_pane_sessions(
                scope=scope,
                pane=args.pane,
                include_non_agents=args.include_non_agents,
            )
        elif args.subcmd == "list":
            result = list_sessions(
                agents=args.agent, days=args.days, scan_all=args.all, limit=args.limit
            )
        elif args.subcmd == "find":
            result = find_sessions(
                pattern=args.pattern,
                agents=args.agent,
                days=args.days,
                scan_all=args.all,
                limit=args.limit,
                fixed=args.fixed,
                case_sensitive=args.case_sensitive,
                all_matches_per_session=args.all_matches_per_session,
            )
        elif args.subcmd == "yank":
            result, code = yank_pane_session(
                target=args.target,
                fmt=args.format,
                display=not args.no_display,
            )
            print(json.dumps(result, ensure_ascii=False))
            return code
        elif args.subcmd == "preview":
            print(
                render_preview(
                    kind=args.kind,
                    pane_id=args.pane,
                    agent=args.agent,
                    session_id=args.sid,
                    lines=args.lines,
                )
            )
            return 0
        else:
            result = read_session(
                session_id=args.session_id,
                agent=args.agent,
                view=args.view,
                include_children=not args.no_children,
                output_dir=args.output_dir,
                max_events=args.max_events,
                full=args.full,
                pane=args.pane,
                include_superseded=args.include_superseded,
            )
    except (FileNotFoundError, FileExistsError, ValueError, RuntimeError, OSError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        return 1
    except Exception as exc:
        print(json.dumps({"error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
