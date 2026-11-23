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
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

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


def group_logical_sessions(files: list[SessionFile]) -> list[LogicalSession]:
    """Group physical storage files by logical (agent, session_id, is_subagent)."""
    buckets: dict[tuple[str, str, bool], list[SessionFile]] = {}
    for sf in files:
        key = (sf.agent, sf.session_id, sf.is_subagent)
        buckets.setdefault(key, []).append(sf)

    sessions: list[LogicalSession] = []
    for (agent, sid, is_sub), segs in buckets.items():
        if len(segs) > 1:
            if agent == "codex":
                meta_pairs = [
                    (codex_sessions.read_rollout_meta(sf.path, max_lines=1), sf.path)
                    for sf in segs
                ]
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


def agent_roots(agent: str, home: Path | None = None) -> list[tuple[Path, dict[str, Any]]]:
    h = home or Path.home()
    if agent == "codex":
        base = Path(os.environ.get("CODEX_HOME") or (h / ".codex"))
        return [(root, {}) for root in codex_sessions.codex_session_roots(base)]
    if agent == "claude":
        base = Path(os.environ.get("CLAUDE_CONFIG_DIR") or (h / ".claude"))
        return [(base / "projects", {})]
    if agent == "antigravity":
        base = Path(os.environ.get("GEMINI_HOME") or (h / ".gemini"))
        return [
            (base / "antigravity-cli" / "brain", {"is_ide": False}),
            (base / "antigravity" / "brain", {"is_ide": True}),
        ]
    if agent == "dsh":
        base = Path(os.environ.get("DSH_HOME") or (h / ".dsh"))
        return [(base / "sessions", {})]
    if agent == "grok":
        base = Path(os.environ.get("GROK_HOME") or (h / ".grok"))
        return [(base / "sessions", {})]
    if agent == "pi":
        base = Path(os.environ.get("PI_CODING_AGENT_DIR") or (h / ".pi" / "agent"))
        return [(base / "sessions", {})]
    if agent == "kimi":
        roots: list[tuple[Path, dict[str, Any]]] = []
        seen: set[Path] = set()
        for env_key in ("KIMI_CODE_HOME", "KIMI_HOME"):
            val = os.environ.get(env_key)
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


def discover_agent_files(agent: str, home: Path | None = None) -> list[SessionFile]:
    found: list[SessionFile] = []

    if agent == "codex":
        for root, _ in agent_roots("codex", home):
            if not root.is_dir():
                continue
            for p in root.rglob("*.jsonl"):
                if not p.is_file():
                    continue
                m = UUID_RE.findall(p.stem)
                if len(m) == 1 and p.stem.startswith("rollout-"):
                    sid = m[0]
                else:
                    meta = codex_sessions.read_rollout_meta(p, max_lines=1)
                    sid = meta["sid"] if meta else (m[0] if m else p.stem)
                st = p.stat()
                found.append(SessionFile("codex", sid, p, st.st_mtime, st.st_size))

    elif agent == "claude":
        for root, _ in agent_roots("claude", home):
            if not root.is_dir():
                continue
            for p in root.rglob("*.jsonl"):
                if not p.is_file():
                    continue
                is_sub = "subagents" in p.parts
                if is_sub:
                    idx = p.parts.index("subagents")
                    parent_sid = p.parts[idx - 1] if idx > 0 else None
                    sid = p.stem
                else:
                    parent_sid = None
                    sid = p.stem
                st = p.stat()
                found.append(
                    SessionFile("claude", sid, p, st.st_mtime, st.st_size, is_sub, parent_sid)
                )

    elif agent == "antigravity":
        for root, meta in agent_roots("antigravity", home):
            if not root.is_dir():
                continue
            is_ide = bool(meta.get("is_ide"))
            for entry in root.iterdir():
                if not entry.is_dir():
                    continue
                log = entry / ".system_generated" / "logs" / "transcript_full.jsonl"
                if log.is_file():
                    sid = f"ide/{entry.name}" if is_ide else entry.name
                    st = log.stat()
                    found.append(
                        SessionFile("antigravity", sid, log, st.st_mtime, st.st_size)
                    )

    elif agent == "dsh":
        for root, _ in agent_roots("dsh", home):
            if not root.is_dir():
                continue
            by_dir: dict[Path, list[tuple[int, int, Path]]] = {}
            for p in root.rglob("session*.jsonl*"):
                if not p.is_file():
                    continue
                m = DSH_GEN_RE.match(p.name)
                if not m:
                    continue
                gen = int(m.group(1)) if m.group(1) else 0
                by_dir.setdefault(p.parent, []).append((gen, int(p.stat().st_mtime), p))
            for sdir, candidates in by_dir.items():
                candidates.sort(key=lambda x: (x[0], x[1]), reverse=True)
                best = candidates[0][2]
                st = best.stat()
                found.append(
                    SessionFile("dsh", sdir.name, best, st.st_mtime, st.st_size)
                )

    elif agent == "grok":
        for root, _ in agent_roots("grok", home):
            if not root.is_dir():
                continue
            for p in root.rglob("updates.jsonl"):
                if not p.is_file():
                    continue
                sid = p.parent.name
                st = p.stat()
                found.append(SessionFile("grok", sid, p, st.st_mtime, st.st_size))

    elif agent == "pi":
        for root, _ in agent_roots("pi", home):
            if not root.is_dir():
                continue
            for p in root.rglob("*.jsonl"):
                if not p.is_file():
                    continue
                m = UUID_RE.findall(p.stem)
                if len(m) == 1:
                    sid = m[0]
                else:
                    hdr = read_first_json(p)
                    hdr_id = hdr.get("id") if (hdr and hdr.get("type") == "session") else None
                    sid = hdr_id if isinstance(hdr_id, str) and hdr_id else (m[0] if m else p.stem)
                st = p.stat()
                found.append(SessionFile("pi", sid, p, st.st_mtime, st.st_size))

    elif agent == "kimi":
        for root, _ in agent_roots("kimi", home):
            if not root.is_dir():
                continue
            for p in root.rglob("wire.jsonl"):
                if not p.is_file():
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
                st = p.stat()
                found.append(
                    SessionFile("kimi", sid, p, st.st_mtime, st.st_size, is_sub, parent_sid)
                )

    return found


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


def iter_head_lines(
    path: Path, max_lines: int = 80, max_bytes: int = 8 * 1024 * 1024
) -> Iterator[str]:
    """Stream up to `max_lines` / `max_bytes` from the head of a session log.

    Streaming line-by-line avoids reading full buffers on normal sessions while
    still tolerating opening turns that embed multi-hundred-KB base64 screenshots.
    """
    if path.suffix == ".zstd":
        for line in read_head_lines(path, max_lines=max_lines, max_bytes=min(max_bytes, 2 * 1024 * 1024)):
            yield line
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
    if path.suffix == ".zstd":
        try:
            proc = subprocess.Popen(
                [zstd_binary(), "-dcq", str(path)],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
            assert proc.stdout is not None
            try:
                data = proc.stdout.read(max_bytes)
            finally:
                proc.stdout.close()
            proc.terminate()
            try:
                proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                proc.kill()
            return data.decode("utf-8", errors="replace").splitlines()[:max_lines]
        except Exception:
            return []
    try:
        with path.open("rb") as f:
            data = f.read(max_bytes)
        return data.decode("utf-8", errors="replace").splitlines()[:max_lines]
    except Exception:
        return []


def iter_decoded_lines(path: Path) -> Iterator[str]:
    if path.suffix == ".zstd":
        proc = subprocess.Popen(
            [zstd_binary(), "-dcq", str(path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        assert proc.stdout is not None
        try:
            for line in proc.stdout:
                yield line
        finally:
            proc.stdout.close()
            rc = proc.wait()
            stderr = proc.stderr.read().strip() if proc.stderr else ""
            if proc.stderr:
                proc.stderr.close()
            if rc != 0:
                raise RuntimeError(f"zstd decode failed for {path}: {stderr}")
    else:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                yield line


def extract_preview(sf: SessionFile) -> str:
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

    for line in iter_head_lines(sf.path):
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except Exception:
            continue
        if not isinstance(rec, dict):
            continue

        text = ""
        if sf.agent == "codex":
            if rec.get("type") == "response_item":
                p = rec.get("payload") or {}
                if p.get("type") == "message" and p.get("role") == "user":
                    text = extract_text_from_content(p.get("content"))
            elif rec.get("type") == "event_msg":
                p = rec.get("payload") or {}
                if p.get("type") == "user_message":
                    text = str(p.get("message", ""))
        elif sf.agent == "antigravity":
            if rec.get("type") == "USER_INPUT":
                text = str(rec.get("content", ""))
                m = re.search(r"<USER_REQUEST>\s*(.*?)\s*</USER_REQUEST>", text, re.S)
                if m:
                    text = m.group(1)
        elif sf.agent == "claude":
            if rec.get("type") == "user" and not rec.get("isMeta"):
                msg = rec.get("message") or {}
                text = extract_text_from_content(msg.get("content"))
        elif sf.agent == "pi":
            if rec.get("type") == "message":
                msg = rec.get("message") or {}
                if msg.get("role") == "user":
                    text = extract_text_from_content(msg.get("content"))
        elif sf.agent == "dsh":
            if rec.get("type") == "user/message":
                data = rec.get("data") or {}
                src = data.get("source") or {}
                if isinstance(src, dict) and src.get("kind") == "user":
                    text = extract_text_from_content(data.get("content"))
        elif sf.agent == "grok":
            params = rec.get("params") or {}
            upd = params.get("update") or {}
            if upd.get("sessionUpdate") == "user_message_chunk":
                text = extract_text_from_content(upd.get("content"))
        elif sf.agent == "kimi":
            if rec.get("type") == "context.append_message":
                msg = rec.get("message") or {}
                if isinstance(msg, dict) and msg.get("role") == "user":
                    text = extract_text_from_content(msg.get("content"))
            elif rec.get("role") == "user":
                text = extract_text_from_content(rec.get("content"))

        if text and not is_noise_user_text(text):
            return clean_preview(text)
    return ""


def read_first_json(path: Path, max_bytes: int = 524288) -> dict[str, Any] | None:
    for line in read_head_lines(path, max_lines=3, max_bytes=max_bytes):
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


def _codex_date_dir_key(path: Path) -> tuple[int, int, int] | None:
    """Extract (YYYY, MM, DD) from `.../sessions/YYYY/MM/DD/file.jsonl` when present."""
    parts = path.parts
    if len(parts) >= 5 and parts[-5] == "sessions":
        y, m, d = parts[-4], parts[-3], parts[-2]
        if len(y) == 4 and len(m) == 2 and len(d) == 2 and y.isdigit() and m.isdigit() and d.isdigit():
            return (int(y), int(m), int(d))
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


def resolve_session_sources(
    session_id: str,
    agent: str | None = None,
    include_children: bool = True,
    home: Path | None = None,
) -> list[SessionFile]:
    session_id, agent = parse_session_ref(session_id, agent)
    agents = normalize_agents([agent]) if agent else list(ALL_AGENTS)
    matches: list[SessionFile] = []
    by_agent: dict[str, list[SessionFile]] = {}

    for ag in agents:
        files = discover_agent_files(ag, home=home)
        by_agent[ag] = files
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
        # Verify authoritative header session_id and sort segments earliest -> latest.
        verified_pairs: list[tuple[dict[str, Any] | None, SessionFile]] = []
        seen_resolved: set[Path] = set()
        for m in matches:
            try:
                resolved = m.path.resolve()
            except OSError:
                resolved = m.path
            if resolved in seen_resolved:
                continue
            meta = codex_sessions.read_rollout_meta(m.path, max_lines=1)
            if meta is not None and meta["sid"] != session_id:
                continue
            seen_resolved.add(resolved)
            verified_pairs.append((meta, m))
        # Ensure we didn't miss any segment whose filename did not match fast-path heuristics.
        h = home or Path.home()
        codex_home = Path(os.environ.get("CODEX_HOME") or (h / ".codex"))
        for extra_path in codex_sessions.find_session_rollouts(session_id, codex_home):
            try:
                resolved = extra_path.resolve()
            except OSError:
                resolved = extra_path
            if resolved in seen_resolved:
                continue
            seen_resolved.add(resolved)
            st = extra_path.stat()
            meta = codex_sessions.read_rollout_meta(extra_path, max_lines=1)
            verified_pairs.append(
                (meta, SessionFile("codex", session_id, extra_path, st.st_mtime, st.st_size))
            )
        if not verified_pairs:
            raise FileNotFoundError(f"session not found: {session_id}")
        path_to_sf = {sf.path: sf for _, sf in verified_pairs}
        ordered = codex_sessions.sort_rollout_entries([(meta, sf.path) for meta, sf in verified_pairs])
        matches = [path_to_sf[p] for _, p in ordered]
    else:
        # Primary sources first (non-subagent first, then path order)
        matches.sort(key=lambda s: (s.is_subagent, str(s.path)))
    result: list[SessionFile] = list(matches)
    seen_paths: set[Path] = set()
    for m in result:
        seen_paths.add(m.path)
        try:
            seen_paths.add(m.path.resolve())
        except OSError:
            pass

    if not include_children:
        return result

    all_agent_files = by_agent[target_agent]

    if target_agent == "claude":
        for sf in all_agent_files:
            if sf.is_subagent and sf.parent_id == session_id and sf.path not in seen_paths:
                result.append(sf)
                seen_paths.add(sf.path)

    elif target_agent == "dsh":
        # BFS over parentSession header field
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
        # Traverse child/forked sessions across day directories and archived_sessions.
        # Only prune sessions/YYYY/MM/DD entries strictly older than the earliest parent date.
        parent_dates = [_codex_date_dir_key(m.path) for m in matches]
        min_parent_date = min(parent_dates) if parent_dates and all(d is not None for d in parent_dates) else None
        queue = list(dict.fromkeys(m.session_id for m in matches))
        visited_ids = set(queue)
        children_by_parent: dict[str, list[tuple[dict[str, Any] | None, SessionFile]]] = {}
        for sf in all_agent_files:
            try:
                sf_resolved = sf.path.resolve()
            except OSError:
                sf_resolved = sf.path
            if sf.path in seen_paths or sf_resolved in seen_paths:
                continue
            if min_parent_date is not None:
                cdate = _codex_date_dir_key(sf.path)
                if cdate is not None and cdate < min_parent_date:
                    continue
            meta = codex_sessions.read_rollout_meta(sf.path, max_lines=1)
            if not meta:
                continue
            pid = meta.get("parent_id")
            if isinstance(pid, str) and pid:
                # Trust authoritative header sid on child segments too.
                sf = SessionFile("codex", meta["sid"], sf.path, sf.mtime, sf.size_bytes, True, pid)
                children_by_parent.setdefault(pid, []).append((meta, sf))
        while queue:
            cur = queue.pop(0)
            child_entries = children_by_parent.get(cur, [])
            if not child_entries:
                continue
            path_to_child = {sf.path: sf for _, sf in child_entries}
            for _, p in codex_sessions.sort_rollout_entries([(meta, sf.path) for meta, sf in child_entries]):
                sf = path_to_child[p]
                try:
                    sf_resolved = sf.path.resolve()
                except OSError:
                    sf_resolved = sf.path
                if sf.path in seen_paths or sf_resolved in seen_paths:
                    continue
                result.append(sf)
                seen_paths.add(sf.path)
                seen_paths.add(sf_resolved)
                if sf.session_id not in visited_ids:
                    visited_ids.add(sf.session_id)
                    queue.append(sf.session_id)

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


def _compute_codex_segment_bounds(sources: list[SessionFile]) -> dict[Path, int | None]:
    """Compute exclusive `superseded_from_ordinal` upper bounds per Codex segment.

    When Codex rolls back/rewinds a turn and opens a suffixed rollout segment,
    the new segment starts at an earlier ordinal than the previous segment's tail.
    For each segment `i` within the same logical Codex session, any record with
    `ordinal >= min(first_ordinal of later segments j > i)` has been superseded.
    """
    bounds: dict[Path, int | None] = {}
    groups: dict[str, list[SessionFile]] = {}
    for sf in sources:
        if sf.agent == "codex":
            groups.setdefault(sf.session_id, []).append(sf)

    for segs in groups.values():
        if len(segs) <= 1:
            continue
        first_ordinals: list[int | None] = []
        for sf in segs:
            meta = codex_sessions.read_rollout_meta(sf.path, max_lines=1)
            ord_val = meta.get("ordinal") if meta else None
            first_ordinals.append(ord_val if isinstance(ord_val, int) else None)
        # Suffix minimum of later segments' starting ordinals
        suffix_min: int | None = None
        for idx in range(len(segs) - 1, -1, -1):
            bounds[segs[idx].path] = suffix_min
            cur_ord = first_ordinals[idx]
            if cur_ord is not None:
                suffix_min = cur_ord if suffix_min is None else min(suffix_min, cur_ord)
    return bounds


def export_sources(
    session_id: str,
    sources: list[SessionFile],
    output_dir: Path,
    include_children: bool,
) -> dict[str, Any]:
    if output_dir.exists():
        raise FileExistsError(f"output directory already exists: {output_dir}")
    output_dir.mkdir(parents=True, exist_ok=False)

    codex_bounds = _compute_codex_segment_bounds(sources)
    attachments_dir = output_dir / "attachments"
    manifest_sources: list[dict[str, Any]] = []
    for idx, sf in enumerate(sources, 1):
        out_name = f"{idx:03d}-{sf.agent}.jsonl"
        out_path = output_dir / out_name
        hasher = hashlib.sha256()
        byte_count = 0
        record_count = 0
        attachments_meta: list[dict[str, Any]] = []
        ordinal_start: int | None = None
        ordinal_end: int | None = None
        with out_path.open("wb") as dst:
            for line_no, line in enumerate(iter_decoded_lines(sf.path), 1):
                encoded = line.encode("utf-8", errors="replace")
                dst.write(encoded)
                hasher.update(encoded)
                byte_count += len(encoded)
                if line.strip():
                    record_count += 1
                    if sf.agent == "codex" and '"ordinal"' in line:
                        try:
                            rec_obj = json.loads(line)
                            ord_v = rec_obj.get("ordinal") if isinstance(rec_obj, dict) else None
                            if isinstance(ord_v, int) and not isinstance(ord_v, bool):
                                if ordinal_start is None or ord_v < ordinal_start:
                                    ordinal_start = ord_v
                                if ordinal_end is None or ord_v > ordinal_end:
                                    ordinal_end = ord_v
                        except Exception:
                            pass
                    attachments_meta.extend(
                        _extract_line_attachments(line, idx, line_no, attachments_dir)
                    )
        source_entry: dict[str, Any] = {
            "source": {
                "agent": sf.agent,
                "session_id": sf.session_id,
                "path": str(sf.path),
                "parent": sf.parent_id,
            },
            "exported": str(out_path.resolve()),
            "bytes": byte_count,
            "records": record_count,
            "sha256": hasher.hexdigest(),
            "attachments": attachments_meta,
        }
        if ordinal_start is not None:
            source_entry["ordinal_start"] = ordinal_start
            source_entry["ordinal_end"] = ordinal_end
        superseded_from = codex_bounds.get(sf.path)
        if superseded_from is not None:
            source_entry["superseded_from_ordinal"] = superseded_from
        manifest_sources.append(source_entry)

    manifest = {
        "version": 1,
        "session_id": session_id,
        "include_children": include_children,
        "complete": True,
        "sources": manifest_sources,
    }
    manifest_path = output_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return manifest


def _parse_timeline_events_with_stats(
    agent: str,
    path: Path,
    full: bool = False,
    *,
    max_ordinal_exclusive: int | None = None,
) -> tuple[list[dict[str, Any]], int, int]:
    events: list[dict[str, Any]] = []
    total_records = 0
    superseded_records = 0

    def fmt(text: str) -> str:
        return text.strip() if full else clean_preview(text, 300)

    for idx, line in enumerate(iter_decoded_lines(path), 1):
        line = line.strip()
        if not line:
            continue
        total_records += 1
        try:
            rec = json.loads(line)
        except Exception:
            continue
        if not isinstance(rec, dict):
            continue

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
                p = rec.get("payload") or {}
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
                tcs = rec.get("tool_calls") or []
                if txt.strip():
                    events.append(
                        {
                            "line": idx,
                            "ts": ts,
                            "kind": "assistant",
                            "text": fmt(txt),
                        }
                    )
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
                msg = rec.get("message") or {}
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
                msg = rec.get("message") or {}
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
                msg = rec.get("message") or {}
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
            data = rec.get("data") or {}
            if t == "user/message":
                src = data.get("source") or {}
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
                msg = data.get("message") or data
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
            upd = (rec.get("params") or {}).get("update") or {}
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
                msg = rec.get("message") or {}
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
                ev = rec.get("event") or {}
                if isinstance(ev, dict):
                    et = ev.get("type")
                    if et == "content.part":
                        part = ev.get("part") or {}
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

    return events, total_records, superseded_records


def parse_timeline_events(
    agent: str,
    path: Path,
    full: bool = False,
    *,
    max_ordinal_exclusive: int | None = None,
) -> tuple[list[dict[str, Any]], int]:
    events, total_records, _ = _parse_timeline_events_with_stats(
        agent, path, full=full, max_ordinal_exclusive=max_ordinal_exclusive
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


def _extract_logical_preview(ls: LogicalSession) -> str:
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
    all_files: list[SessionFile] = []
    for ag in norm_agents:
        for sf in discover_agent_files(ag, home=home):
            if not sf.is_subagent:
                all_files.append(sf)

    logical = group_logical_sessions(all_files)
    hot, skipped = prune_by_time(logical, days, scan_all, now=now)
    selected: list[LogicalSession] = hot[:limit]
    items: list[dict[str, Any]] = []
    for ls in selected:
        item: dict[str, Any] = {
            "agent": ls.agent,
            "session_id": ls.session_id,
            "mtime": iso_local(ls.mtime),
            "size_bytes": ls.size_bytes,
            "preview": _extract_logical_preview(ls),
            "path": str(ls.latest_segment.path),
            "segment_count": len(ls.segments),
        }
        if len(ls.segments) > 1:
            item["paths"] = [str(seg.path) for seg in ls.segments]
        items.append(item)
    return {
        "command": "list",
        "count": len(items),
        "truncated": len(hot) > len(items),
        "scanned_scope": {
            "agents": norm_agents,
            "days": None if scan_all else days,
            "all": scan_all,
            "matched_sessions_in_window": len(hot),
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
    all_files: list[SessionFile] = []
    for ag in norm_agents:
        all_files.extend(discover_agent_files(ag, home=home))

    # Prune at logical-session granularity so older segments of an active
    # multi-segment session remain searchable within the hot window.
    logical = group_logical_sessions(all_files)
    hot_logical, _ = prune_by_time(logical, days, scan_all, now=now)
    hot: list[SessionFile] = []
    for ls in hot_logical:
        hot.extend(ls.segments)
    hot.sort(key=lambda s: s.mtime, reverse=True)
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
            batch_hits: list[tuple[float, dict[str, Any]]] = []
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

                batch_hits.append(
                    (
                        sf.mtime,
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
    if pane:
        import live_sessions

        env = dict(os.environ if environment is None else environment)
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
        session_id = target_pane.session_id
        agent = agent or target_pane.agent
        resolved_pane_info = target_pane.as_dict()

    if not session_id:
        raise ValueError("session_id or --pane is required")

    session_id, agent = parse_session_ref(session_id, agent)
    sources = resolve_session_sources(
        session_id=session_id,
        agent=agent,
        include_children=include_children,
        home=home,
    )

    if view == "export":
        if output_dir:
            out_dir = Path(output_dir)
        else:
            stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
            safe_id = session_id.replace("/", "_")
            out_dir = Path(f"/tmp/aipane-sessions-{safe_id}-{stamp}")
        manifest = export_sources(session_id, sources, out_dir, include_children)
        payload: dict[str, Any] = {
            "command": "read",
            "view": "export",
            "output_dir": str(out_dir.resolve()),
            "manifest_path": str((out_dir / "manifest.json").resolve()),
            "manifest": manifest,
        }
        if resolved_pane_info is not None:
            payload["resolved_pane"] = resolved_pane_info
        return payload

    effective_max = max_events if max_events is not None else (None if full else 120)
    codex_bounds = {} if include_superseded else _compute_codex_segment_bounds(sources)
    all_sources = []
    for sf in sources:
        superseded_from = codex_bounds.get(sf.path)
        evs, records, superseded_count = _parse_timeline_events_with_stats(
            sf.agent,
            sf.path,
            full=full,
            max_ordinal_exclusive=superseded_from,
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
        if superseded_from is not None:
            src_entry["superseded_from_ordinal"] = superseded_from
            src_entry["superseded_records"] = superseded_count
        all_sources.append(src_entry)
    result: dict[str, Any] = {
        "command": "read",
        "view": "timeline",
        "full": full,
        "session_id": session_id,
        "complete": True,
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
        primary_sources = resolve_session_sources(
            session_id=sid_clean,
            agent=norm_agent,
            include_children=False,
            home=home,
        )
        codex_bounds = _compute_codex_segment_bounds(primary_sources)
        body_lines: list[str] = []
        shown: list[dict[str, Any]] = []
        if len(primary_sources) <= 2:
            for sf in primary_sources:
                evs, _, _ = _parse_timeline_events_with_stats(
                    sf.agent,
                    sf.path,
                    full=False,
                    max_ordinal_exclusive=codex_bounds.get(sf.path),
                )
                shown.extend(e for e in evs if e.get("kind") in ("user", "assistant"))
            if len(shown) > 10:
                shown = [*shown[:3], {"kind": "gap", "text": f"… {len(shown) - 8} turns omitted …"}, *shown[-5:]]
        else:
            head_turns: list[dict[str, Any]] = []
            head_last_idx = 0
            for idx, sf in enumerate(primary_sources):
                head_last_idx = idx
                evs, _, _ = _parse_timeline_events_with_stats(
                    sf.agent,
                    sf.path,
                    full=False,
                    max_ordinal_exclusive=codex_bounds.get(sf.path),
                )
                head_turns.extend(e for e in evs if e.get("kind") in ("user", "assistant"))
                if len(head_turns) >= 3:
                    break
            remaining = primary_sources[head_last_idx + 1 :]
            if not remaining:
                shown = head_turns
                if len(shown) > 10:
                    shown = [*shown[:3], {"kind": "gap", "text": f"… {len(shown) - 8} turns omitted …"}, *shown[-5:]]
            else:
                tail_turns: list[dict[str, Any]] = []
                tail_segs_read = 0
                for sf in reversed(remaining):
                    evs, _, _ = _parse_timeline_events_with_stats(
                        sf.agent,
                        sf.path,
                        full=False,
                        max_ordinal_exclusive=codex_bounds.get(sf.path),
                    )
                    seg_turns = [e for e in evs if e.get("kind") in ("user", "assistant")]
                    tail_turns = [*seg_turns, *tail_turns]
                    tail_segs_read += 1
                    if len(tail_turns) >= 5:
                        break
                omitted_segs = max(0, len(remaining) - tail_segs_read)
                gap_text = (
                    f"… {omitted_segs} middle segment(s) omitted ({len(primary_sources)} segments total) …"
                    if omitted_segs
                    else f"… across {len(primary_sources)} segments …"
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
    except (FileNotFoundError, FileExistsError, ValueError, RuntimeError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
