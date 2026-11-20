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
        return [(base / "sessions", {})]
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
        base = Path(os.environ.get("KIMI_HOME") or (h / ".kimi"))
        return [(base / "sessions", {})]
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
                sid = m[-1] if m else p.stem
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
                sid = m[-1] if m else p.stem
                st = p.stat()
                found.append(SessionFile("pi", sid, p, st.st_mtime, st.st_size))

    elif agent == "kimi":
        for root, _ in agent_roots("kimi", home):
            if not root.is_dir():
                continue
            for p in root.rglob("wire.jsonl"):
                if not p.is_file():
                    continue
                sid = p.parent.name
                for part in reversed(p.parts):
                    if part.startswith("session_"):
                        sid = part[len("session_") :]
                        break
                    m = UUID_RE.search(part)
                    if m:
                        sid = m.group(1)
                        break
                st = p.stat()
                found.append(SessionFile("kimi", sid, p, st.st_mtime, st.st_size))

    return found


def prune_by_time(
    files: list[SessionFile], days: int, scan_all: bool, now: float | None = None
) -> tuple[list[SessionFile], int]:
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

    for line in read_head_lines(sf.path):
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
            if rec.get("role") == "user":
                text = extract_text_from_content(rec.get("content"))

        if text and not is_noise_user_text(text):
            return clean_preview(text)
    return ""


def read_first_json(path: Path) -> dict[str, Any] | None:
    for line in read_head_lines(path, max_lines=3, max_bytes=16384):
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


def resolve_session_sources(
    session_id: str,
    agent: str | None = None,
    include_children: bool = True,
    home: Path | None = None,
) -> list[SessionFile]:
    agents = normalize_agents([agent]) if agent else list(ALL_AGENTS)
    matches: list[SessionFile] = []
    by_agent: dict[str, list[SessionFile]] = {}

    for ag in agents:
        files = discover_agent_files(ag, home=home)
        by_agent[ag] = files
        for sf in files:
            if sf.session_id == session_id:
                matches.append(sf)

    if not matches:
        raise FileNotFoundError(f"session not found: {session_id}")

    matched_agents = sorted({m.agent for m in matches})
    if len(matched_agents) > 1:
        raise ValueError(
            f"ambiguous session_id {session_id!r} matched multiple agents: {', '.join(matched_agents)}; pass --agent"
        )

    target_agent = matched_agents[0]
    # Primary sources first (non-subagent first, then path order)
    matches.sort(key=lambda s: (s.is_subagent, str(s.path)))
    result: list[SessionFile] = list(matches)
    seen_paths: set[Path] = {m.path for m in result}

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
        # Check same-day directory and immediate siblings for parent_thread_id / forked_from_id
        candidate_dirs = {m.path.parent for m in matches}
        queue = [m.session_id for m in matches]
        visited_ids = set(queue)
        dir_files = [
            sf for sf in all_agent_files if sf.path.parent in candidate_dirs and sf.path not in seen_paths
        ]
        headers: list[tuple[SessionFile, str]] = []
        for sf in dir_files:
            hdr = read_first_json(sf.path)
            if not hdr or hdr.get("type") != "session_meta":
                continue
            payload = hdr.get("payload") or {}
            pid = (
                payload.get("parent_thread_id")
                or payload.get("forked_from_id")
                or payload.get("parent_session_id")
            )
            if isinstance(pid, str) and pid:
                headers.append((sf, pid))
        while queue:
            cur = queue.pop(0)
            for sf, pid in headers:
                if pid == cur and sf.path not in seen_paths:
                    sf.parent_id = pid
                    result.append(sf)
                    seen_paths.add(sf.path)
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


def export_sources(
    session_id: str,
    sources: list[SessionFile],
    output_dir: Path,
    include_children: bool,
) -> dict[str, Any]:
    if output_dir.exists():
        raise FileExistsError(f"output directory already exists: {output_dir}")
    output_dir.mkdir(parents=True, exist_ok=False)

    attachments_dir = output_dir / "attachments"
    manifest_sources: list[dict[str, Any]] = []
    for idx, sf in enumerate(sources, 1):
        out_name = f"{idx:03d}-{sf.agent}.jsonl"
        out_path = output_dir / out_name
        hasher = hashlib.sha256()
        byte_count = 0
        record_count = 0
        attachments_meta: list[dict[str, Any]] = []
        with out_path.open("wb") as dst:
            for line_no, line in enumerate(iter_decoded_lines(sf.path), 1):
                encoded = line.encode("utf-8", errors="replace")
                dst.write(encoded)
                hasher.update(encoded)
                byte_count += len(encoded)
                if line.strip():
                    record_count += 1
                    attachments_meta.extend(
                        _extract_line_attachments(line, idx, line_no, attachments_dir)
                    )
        manifest_sources.append(
            {
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
        )

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


def parse_timeline_events(
    agent: str, path: Path, full: bool = False
) -> tuple[list[dict[str, Any]], int]:
    events: list[dict[str, Any]] = []
    total_records = 0

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

    hot, skipped = prune_by_time(all_files, days, scan_all, now=now)
    selected = hot[:limit]
    items = [
        {
            "agent": sf.agent,
            "session_id": sf.session_id,
            "mtime": iso_local(sf.mtime),
            "size_bytes": sf.size_bytes,
            "preview": extract_preview(sf),
            "path": str(sf.path),
        }
        for sf in selected
    ]
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

    hot, skipped = prune_by_time(all_files, days, scan_all, now=now)
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
    session_id: str,
    agent: str | None = None,
    view: str = "timeline",
    include_children: bool = True,
    output_dir: str | None = None,
    max_events: int | None = None,
    full: bool = False,
    home: Path | None = None,
) -> dict[str, Any]:
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
        return {
            "command": "read",
            "view": "export",
            "output_dir": str(out_dir.resolve()),
            "manifest_path": str((out_dir / "manifest.json").resolve()),
            "manifest": manifest,
        }

    effective_max = max_events if max_events is not None else (None if full else 120)
    all_sources = []
    for sf in sources:
        evs, records = parse_timeline_events(sf.agent, sf.path, full=full)
        if effective_max is not None and effective_max > 0:
            truncated = len(evs) > effective_max
            out_events = evs[:effective_max]
        else:
            truncated = False
            out_events = evs
        all_sources.append(
            {
                "agent": sf.agent,
                "session_id": sf.session_id,
                "parent": sf.parent_id,
                "source_path": str(sf.path),
                "records": records,
                "total_timeline_events": len(evs),
                "truncated": truncated,
                "events": out_events,
            }
        )
    return {
        "command": "read",
        "view": "timeline",
        "full": full,
        "session_id": session_id,
        "complete": True,
        "sources": all_sources,
    }


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="aipane-sessions",
        description="Zero-index local session discovery, search, and export across 7 AI CLIs.",
    )
    sub = p.add_subparsers(dest="subcmd", required=True)

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

    p_read = sub.add_parser("read", help="Read timeline or export full logs for a known session ID")
    p_read.add_argument("session_id", help="Exact native session ID (Antigravity IDE: ide/<id>)")
    p_read.add_argument("--agent", help="Optional agent disambiguator")
    p_read.add_argument("--view", choices=("timeline", "export"), default="timeline", help="Output mode (default: timeline)")
    p_read.add_argument("--full", action="store_true", help="Include full untruncated timeline text and all events")
    p_read.add_argument("--no-children", action="store_true", help="Do not follow child sessions")
    p_read.add_argument("--output-dir", help="Target export directory (must not exist)")
    p_read.add_argument(
        "--max-events",
        type=int,
        default=None,
        help="Max timeline events per source (default: 120; unlimited with --full)",
    )

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.subcmd == "list":
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
        else:
            result = read_session(
                session_id=args.session_id,
                agent=args.agent,
                view=args.view,
                include_children=not args.no_children,
                output_dir=args.output_dir,
                max_events=args.max_events,
                full=args.full,
            )
    except (FileNotFoundError, FileExistsError, ValueError, RuntimeError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
