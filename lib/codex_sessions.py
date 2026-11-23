"""Read-only evidence and canonical storage adapter for local Codex conversations."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sqlite3
import subprocess
import time
from contextlib import closing
from pathlib import Path
from typing import Any

CODEX_SESSION_ROOT_NAMES = ("sessions", "archived_sessions")
CODEX_ROLLOUT_SUFFIXES = (".jsonl", ".jsonl.zst")
ROLLOUT_META_MAX_BYTES = 4 * 1024 * 1024
_STATE_DB_RE = re.compile(r"^state_(\d+)\.sqlite$")
_FILENAME_TS_RE = re.compile(
    r"^rollout-(\d{4})-(\d{2})-(\d{2})T(\d{2})-(\d{2})-(\d{2})(?:-[0-9a-fA-F]{8}-|$)"
)
UUID_RE = re.compile(
    r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})"
)


def zstd_binary() -> str:
    return shutil.which("zstd") or "/opt/homebrew/bin/zstd"


def is_compressed_rollout(path: Path) -> bool:
    name = path.name.lower()
    return name.endswith(".jsonl.zst") or name.endswith(".jsonl.zstd")


def is_rollout_file_name(name: str) -> bool:
    low = name.lower()
    return low.endswith(".jsonl") or low.endswith(".jsonl.zst") or low.endswith(".jsonl.zstd")


def rollout_plain_stem(path: Path) -> str:
    """Return filename stem with `.jsonl`, `.jsonl.zst`, or `.jsonl.zstd` stripped."""
    name = path.name
    low = name.lower()
    for suffix in (".jsonl.zstd", ".jsonl.zst", ".jsonl"):
        if low.endswith(suffix):
            return name[: -len(suffix)]
    return path.stem


def rollout_canonical_key(path: Path) -> Path:
    """Key unifying plain `.jsonl` and compressed `.jsonl.zst` of the same rollout."""
    try:
        parent = path.parent.resolve()
    except OSError:
        parent = path.parent
    return parent / rollout_plain_stem(path)


def extract_filename_ts(path: Path) -> str:
    """Extract normalized sortable timestamp from `rollout-YYYY-MM-DDTHH-MM-SS-...`."""
    m = _FILENAME_TS_RE.match(rollout_plain_stem(path))
    if not m:
        return ""
    y, mo, d, h, mi, s = m.groups()
    return f"{y}-{mo}-{d}T{h}:{mi}:{s}"


def extract_filename_ids(path: Path) -> tuple[str | None, str | None]:
    """Return `(filename_session_id, filename_rollout_id)` parsed from rollout name.

    For `<ts>-<sid>.jsonl[.zst]` -> `(sid, sid)`.
    For `<ts>-<sid>_<rid>.jsonl[.zst]` -> `(sid, rid)`.
    """
    uuids = UUID_RE.findall(rollout_plain_stem(path))
    if len(uuids) >= 2:
        return uuids[0], uuids[-1]
    if len(uuids) == 1:
        return uuids[0], uuids[0]
    return None, None


def codex_session_roots(codex_home: Path, *, existing_only: bool = False) -> list[Path]:
    roots = [codex_home / name for name in CODEX_SESSION_ROOT_NAMES]
    if existing_only:
        return [root for root in roots if root.is_dir()]
    return roots


def extract_parent_session_id(payload: dict[str, Any]) -> str | None:
    own_id = payload.get("id")
    for key in ("parent_thread_id", "forked_from_id", "parent_session_id"):
        val = payload.get(key)
        if isinstance(val, str) and val and val != own_id:
            return val
    source = payload.get("source")
    if isinstance(source, dict):
        subagent = source.get("subagent") or source.get("sub_agent")
        if isinstance(subagent, dict):
            thread_spawn = subagent.get("thread_spawn") or subagent.get("threadSpawn")
            if isinstance(thread_spawn, dict):
                val = thread_spawn.get("parent_thread_id") or thread_spawn.get("parentThreadId")
                if isinstance(val, str) and val and val != own_id:
                    return val
            val = subagent.get("parent_thread_id") or subagent.get("parentThreadId")
            if isinstance(val, str) and val and val != own_id:
                return val
    return None


def extract_subagent_parent_id(payload: dict[str, Any]) -> str | None:
    """Return parent thread ID specifically when `source.subagent` spawns this thread."""
    own_id = payload.get("id")
    source = payload.get("source")
    if not isinstance(source, dict):
        return None
    subagent = source.get("subagent") or source.get("sub_agent")
    if not isinstance(subagent, dict):
        return None
    spawn = subagent.get("thread_spawn") or subagent.get("threadSpawn")
    if isinstance(spawn, dict):
        val = spawn.get("parent_thread_id") or spawn.get("parentThreadId")
        if isinstance(val, str) and val and val != own_id:
            return val
    val = subagent.get("parent_thread_id") or subagent.get("parentThreadId")
    if isinstance(val, str) and val and val != own_id:
        return val
    return None


def _extract_history_base(payload: dict[str, Any]) -> dict[str, Any] | None:
    hb = payload.get("history_base")
    if not isinstance(hb, dict):
        return None
    base_rid = hb.get("thread_id")
    if not isinstance(base_rid, str) or not base_rid:
        return None
    end_ord = hb.get("end_ordinal_exclusive")
    if not isinstance(end_ord, int) or isinstance(end_ord, bool):
        end_ord = None
    end_byte = hb.get("end_byte_offset")
    if not isinstance(end_byte, int) or isinstance(end_byte, bool):
        end_byte = None
    return {
        "rollout_id": base_rid,
        "end_ordinal_exclusive": end_ord,
        "end_byte_offset": end_byte,
    }


def infer_codex_home(path: Path) -> str:
    """Infer the owning `CODEX_HOME` for a rollout path, including future layouts."""
    try:
        resolved = path.resolve()
    except OSError:
        resolved = path
    directory = next(
        (ancestor for ancestor in resolved.parents if ancestor.name in CODEX_SESSION_ROOT_NAMES),
        None,
    )
    if directory is not None:
        return str(directory.parent)
    # Fallback for novel subdirectories (e.g. ~/.codex/future_threads/...):
    # look up a few levels for state_*.sqlite, config.toml, or a `.codex` root.
    for idx, ancestor in enumerate(resolved.parents):
        if idx > 5:
            break
        if ancestor.name == ".codex":
            return str(ancestor)
        try:
            if (ancestor / "config.toml").is_file() or (ancestor / "sessions").is_dir():
                return str(ancestor)
            if any(ancestor.glob("state_*.sqlite")):
                return str(ancestor)
        except OSError:
            continue
    return ""


def _read_first_lines_raw(
    path: Path,
    *,
    max_lines: int = 1,
    max_bytes: int = ROLLOUT_META_MAX_BYTES,
) -> tuple[list[bytes], str | None]:
    """Read up to `max_lines` raw lines from plain or zstd-compressed rollout."""
    if is_compressed_rollout(path):
        try:
            proc = subprocess.Popen(
                [zstd_binary(), "-dcq", str(path)],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
        except OSError:
            return [], "io_error"
        assert proc.stdout is not None
        lines: list[bytes] = []
        err: str | None = None
        try:
            for _ in range(max(1, max_lines)):
                raw = proc.stdout.readline(max_bytes + 1)
                if not raw:
                    break
                if len(raw) > max_bytes:
                    err = "header_too_large"
                    break
                lines.append(raw)
        except OSError:
            err = "io_error"
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
        return lines, err

    try:
        lines = []
        with path.open("rb") as rollout:
            for _ in range(max(1, max_lines)):
                raw = rollout.readline(max_bytes + 1)
                if not raw:
                    break
                if len(raw) > max_bytes:
                    return lines, "header_too_large"
                lines.append(raw)
        return lines, None
    except OSError:
        return [], "io_error"


def read_rollout_meta_detailed(
    path: Path,
    *,
    max_lines: int = 1,
    max_bytes: int = ROLLOUT_META_MAX_BYTES,
) -> tuple[dict[str, Any] | None, str | None]:
    """Parse `session_meta` header and return `(meta, error_code)`.

    `error_code` is None on success, or one of:
    `io_error`, `header_too_large`, `invalid_json`, `missing_meta`.
    """
    raw_lines, read_err = _read_first_lines_raw(path, max_lines=max_lines, max_bytes=max_bytes)
    if read_err is not None and not raw_lines:
        return None, read_err

    saw_invalid_json = False
    for raw in raw_lines:
        stripped = raw.strip()
        if not stripped:
            continue
        try:
            metadata = json.loads(stripped.decode("utf-8", errors="replace"))
        except ValueError:
            saw_invalid_json = True
            continue
        if not isinstance(metadata, dict):
            continue
        if metadata.get("type") != "session_meta":
            continue
        payload = metadata.get("payload")
        if not isinstance(payload, dict):
            return None, "missing_meta"
        sid = payload.get("id")
        if not isinstance(sid, str) or not sid:
            return None, "missing_meta"
        ordinal = metadata.get("ordinal")
        if not isinstance(ordinal, int) or isinstance(ordinal, bool):
            ordinal = None
        ts = payload.get("timestamp") or metadata.get("timestamp") or ""
        fn_sid, fn_rid = extract_filename_ids(path)
        rollout_id = fn_rid or sid
        return (
            {
                "sid": sid,
                "rollout_id": rollout_id,
                "filename_sid": fn_sid,
                "cwd": str(payload.get("cwd") or ""),
                "timestamp": str(ts) if isinstance(ts, str) else "",
                "filename_ts": extract_filename_ts(path),
                "ordinal": ordinal,
                "parent_id": extract_parent_session_id(payload),
                "subagent_parent_id": extract_subagent_parent_id(payload),
                "history_base": _extract_history_base(payload),
                "history_mode": str(payload.get("history_mode") or ""),
                "codex_home": infer_codex_home(path),
                "rollout": str(path),
            },
            None,
        )

    if read_err is not None:
        return None, read_err
    if saw_invalid_json:
        return None, "invalid_json"
    return None, "missing_meta"


def read_rollout_meta(
    path: Path,
    *,
    max_lines: int = 1,
    max_bytes: int = ROLLOUT_META_MAX_BYTES,
) -> dict[str, Any] | None:
    meta, _ = read_rollout_meta_detailed(path, max_lines=max_lines, max_bytes=max_bytes)
    return meta


def rollout_matches_session(path: Path, sid: str) -> bool:
    meta = read_rollout_meta(path, max_lines=1)
    return bool(meta and meta["sid"] == sid)


def _effective_timestamp(meta: dict[str, Any] | None, path: Path) -> str:
    if meta:
        ts = meta.get("timestamp")
        if isinstance(ts, str) and ts:
            return ts
        fn_ts = meta.get("filename_ts")
        if isinstance(fn_ts, str) and fn_ts:
            return fn_ts
    return extract_filename_ts(path)


def sort_rollout_entries(
    entries: list[tuple[dict[str, Any] | None, Path]],
) -> list[tuple[dict[str, Any] | None, Path]]:
    """Order rollout segments for the same session from earliest to latest creation.

    Uses `session_meta.timestamp` (falling back per-entry to canonical filename
    timestamp `rollout-YYYY-MM-DDTHH-MM-SS`) so consecutive rollbacks to an
    earlier ordinal still preserve true causal creation order even when a
    segment omits `session_meta.timestamp`.
    """
    effective_ts = [_effective_timestamp(meta, path) for meta, path in entries]
    if entries and all(effective_ts):
        return [
            entry
            for _, entry in sorted(
                zip(effective_ts, entries),
                key=lambda pair: (
                    pair[0],
                    int(pair[1][0]["ordinal"])
                    if (pair[1][0] and isinstance(pair[1][0].get("ordinal"), int))
                    else 0,
                    str(pair[1][1]),
                ),
            )
        ]
    all_have_ordinal = bool(entries) and all(
        meta is not None and isinstance(meta.get("ordinal"), int)
        for meta, _ in entries
    )
    if all_have_ordinal:
        return sorted(
            entries,
            key=lambda item: (
                int(item[0]["ordinal"]),  # type: ignore[index]
                str(item[1]),
            ),
        )
    return sorted(
        entries,
        key=lambda item: (
            "" if _effective_timestamp(item[0], item[1]) else "~",
            _effective_timestamp(item[0], item[1]),
            str(item[1]),
        ),
    )


def deduplicate_rollout_paths(paths: list[Path]) -> list[Path]:
    """Deduplicate physical files (symlinks + plain/compressed sibling pairs).

    Prefers plain `.jsonl` over `.jsonl.zst` when both exist for the same
    rollout, and prefers `sessions/` over `archived_sessions/` for symlinks.
    """
    by_inode: dict[tuple[int, int], Path] = {}
    by_canonical: dict[Path, Path] = {}

    def _priority(p: Path) -> tuple[int, int, int, str]:
        is_comp = 1 if is_compressed_rollout(p) else 0
        is_arch = 1 if "archived_sessions" in p.parts else 0
        is_sym = 1 if p.is_symlink() else 0
        return (is_comp, is_arch, is_sym, str(p))

    for path in paths:
        try:
            st = path.stat()
        except OSError:
            continue
        if not path.is_file():
            continue
        inode_key = (st.st_dev, st.st_ino)
        prev = by_inode.get(inode_key)
        if prev is not None:
            if _priority(path) < _priority(prev):
                by_inode[inode_key] = path
            continue
        by_inode[inode_key] = path

    for path in by_inode.values():
        ckey = rollout_canonical_key(path)
        prev = by_canonical.get(ckey)
        if prev is None or _priority(path) < _priority(prev):
            by_canonical[ckey] = path

    return list(by_canonical.values())


def iter_codex_rollout_files(codex_home: Path) -> list[Path]:
    """Return deduplicated `.jsonl` and `.jsonl.zst` rollouts under `codex_home`."""
    raw: list[Path] = []
    for root in codex_session_roots(codex_home, existing_only=True):
        for pattern in ("*.jsonl", "*.jsonl.zst"):
            try:
                for path in root.rglob(pattern):
                    raw.append(path)
            except OSError:
                continue
    return deduplicate_rollout_paths(raw)


def find_session_rollouts(
    sid: str,
    codex_home: Path,
    *,
    verify_header: bool = True,
) -> list[Path]:
    """Return all durable rollouts belonging to `sid`, ordered earliest to latest.

    Matches plain `.jsonl` and compressed `.jsonl.zst`, both unsuffixed and
    suffixed (`<sid>_<rollout_id>`), across `sessions/` and `archived_sessions/`.
    """
    if not sid:
        return []
    raw_candidates: list[Path] = []
    for root in codex_session_roots(codex_home, existing_only=True):
        for suffix in CODEX_ROLLOUT_SUFFIXES:
            for pattern in (f"*-{sid}{suffix}", f"*-{sid}_*{suffix}"):
                try:
                    raw_candidates.extend(root.rglob(pattern))
                except OSError:
                    continue
    deduped = deduplicate_rollout_paths(raw_candidates)
    matched: list[tuple[dict[str, Any] | None, Path]] = []
    for path in deduped:
        meta = read_rollout_meta(path, max_lines=1)
        if verify_header:
            if not meta or meta["sid"] != sid:
                continue
        elif meta is not None and meta["sid"] != sid:
            continue
        matched.append((meta, path))
    return [path for _, path in sort_rollout_entries(matched)]


def find_rollout_by_rollout_id(
    rollout_id: str,
    codex_home: Path,
    *,
    rollout_index: dict[str, Path] | None = None,
) -> Path | None:
    """Locate a physical rollout file by its physical `rollout_id`.

    A physical rollout ID is either the sole UUID in `rollout-...-<rid>.jsonl[.zst]`
    or the trailing UUID in `rollout-...-<sid>_<rid>.jsonl[.zst]`.
    """
    if not rollout_id:
        return None
    if rollout_index is not None and rollout_id in rollout_index:
        return rollout_index[rollout_id]
    raw_candidates: list[Path] = []
    for root in codex_session_roots(codex_home, existing_only=True):
        for suffix in CODEX_ROLLOUT_SUFFIXES:
            for pattern in (f"*-{rollout_id}{suffix}", f"*_{rollout_id}{suffix}"):
                try:
                    raw_candidates.extend(root.rglob(pattern))
                except OSError:
                    continue
    deduped = deduplicate_rollout_paths(raw_candidates)
    for path in deduped:
        _, fn_rid = extract_filename_ids(path)
        if fn_rid == rollout_id:
            if rollout_index is not None:
                rollout_index[rollout_id] = path
            return path
    return None


def resolve_alternate_rollout_path(path: Path, codex_home: Path | None = None) -> Path | None:
    """Find an existing plain/compressed/archived counterpart for a missing rollout path."""
    if path.is_file():
        return path
    # 1. Check plain <-> .zst sibling in the same directory
    if is_compressed_rollout(path):
        plain = path.with_name(rollout_plain_stem(path) + ".jsonl")
        if plain.is_file():
            return plain
    elif path.name.lower().endswith(".jsonl"):
        comp = path.with_name(path.name + ".zst")
        if comp.is_file():
            return comp
    # 2. Check across sessions/ <-> archived_sessions/ by physical rollout_id or filename
    home = codex_home
    if home is None:
        inferred = infer_codex_home(path)
        home = Path(inferred) if inferred else None
    if home is not None:
        _, fn_rid = extract_filename_ids(path)
        if fn_rid:
            found = find_rollout_by_rollout_id(fn_rid, home)
            if found is not None:
                return found
        stem = rollout_plain_stem(path)
        for root in codex_session_roots(home, existing_only=True):
            for suffix in CODEX_ROLLOUT_SUFFIXES:
                for cand in root.rglob(f"{stem}{suffix}"):
                    if cand.is_file():
                        return cand
    return None


def resolve_effective_lineage(
    session_id: str,
    candidates: list[Path],
    *,
    active_path: Path | None = None,
    sqlite_path: Path | None = None,
    codex_home: Path | None = None,
    meta_cache: dict[Path, dict[str, Any] | None] | None = None,
    rollout_index: dict[str, Path] | None = None,
) -> tuple[list[dict[str, Any]], list[Path], list[str]]:
    """Resolve the active effective segment chain for `session_id`.

    Returns `(effective_segments, unlinked_segments, warnings)` where each entry
    in `effective_segments` has:
      - `path`: Path
      - `meta`: dict | None
      - `role`: "effective" | "inherited_prefix"
      - `end_ordinal_exclusive`: int | None
      - `end_byte_offset`: int | None
      - `segment_sid`: str (header sid; may differ from `session_id` for forked prefix)

    Follows Codex's `history_base` pointer chain from the authoritative head
    (verified `active_path` > verified `sqlite_path` > newest candidate) so
    uncommitted rollback files written before SQLite switches never truncate
    live history, and forked threads inherit their parent's bounded prefix.
    """
    cache = meta_cache if meta_cache is not None else {}

    def _meta(p: Path) -> dict[str, Any] | None:
        try:
            key = p.resolve()
        except OSError:
            key = p
        if key not in cache:
            cache[key] = read_rollout_meta(p, max_lines=1)
        return cache[key]

    deduped = deduplicate_rollout_paths(candidates)
    same_sid_entries: list[tuple[dict[str, Any] | None, Path]] = []
    for p in deduped:
        m = _meta(p)
        if m is not None and m["sid"] != session_id:
            continue
        same_sid_entries.append((m, p))

    ordered_entries = sort_rollout_entries(same_sid_entries)
    if not ordered_entries:
        return [], [], []

    # Build local rollout_id -> Path map for fast lineage traversal
    local_rindex: dict[str, Path] = dict(rollout_index) if rollout_index is not None else {}
    for m, p in ordered_entries:
        rid = (m or {}).get("rollout_id") or extract_filename_ids(p)[1]
        if isinstance(rid, str) and rid and rid not in local_rindex:
            local_rindex[rid] = p

    def _match_anchor(anchor: Path | None) -> tuple[dict[str, Any] | None, Path] | None:
        if anchor is None:
            return None
        resolved_anchor = resolve_alternate_rollout_path(anchor, codex_home)
        if resolved_anchor is None:
            return None
        m = _meta(resolved_anchor)
        if not m or m["sid"] != session_id:
            return None
        return m, resolved_anchor

    committed_head = _match_anchor(active_path) or _match_anchor(sqlite_path)
    head_meta, head_path = committed_head if committed_head is not None else ordered_entries[-1]

    warnings: list[str] = []

    # Path A: Head participates in Codex's explicit `history_base` lineage chain
    if head_meta is not None and head_meta.get("history_base") is not None:
        chain: list[dict[str, Any]] = []
        seen_rids: set[str] = set()
        seen_resolved: set[Path] = set()
        cur_meta: dict[str, Any] | None = head_meta
        cur_path: Path | None = head_path
        cur_end_ord: int | None = None
        cur_end_byte: int | None = None

        while cur_path is not None:
            try:
                cur_res = cur_path.resolve()
            except OSError:
                cur_res = cur_path
            cur_rid = (cur_meta or {}).get("rollout_id") or extract_filename_ids(cur_path)[1] or str(cur_res)
            if cur_rid in seen_rids or cur_res in seen_resolved:
                warnings.append(f"lineage_cycle_detected: cycle at rollout {cur_rid}")
                break
            seen_rids.add(cur_rid)
            seen_resolved.add(cur_res)
            seg_sid = str((cur_meta or {}).get("sid") or session_id)
            role = "effective" if seg_sid == session_id else "inherited_prefix"
            chain.append(
                {
                    "path": cur_path,
                    "meta": cur_meta,
                    "role": role,
                    "end_ordinal_exclusive": cur_end_ord,
                    "end_byte_offset": cur_end_byte,
                    "segment_sid": seg_sid,
                }
            )
            hb = cur_meta.get("history_base") if cur_meta else None
            if not hb:
                break
            base_rid = hb["rollout_id"]
            base_path = local_rindex.get(base_rid)
            if base_path is None and codex_home is not None:
                base_path = find_rollout_by_rollout_id(base_rid, codex_home, rollout_index=local_rindex)
            if base_path is None or not base_path.is_file():
                warnings.append(
                    f"missing_history_base_rollout: segment {cur_path} references missing base rollout {base_rid}"
                )
                break
            cur_path = base_path
            cur_meta = _meta(base_path)
            cur_end_ord = hb.get("end_ordinal_exclusive")
            cur_end_byte = hb.get("end_byte_offset")

        chain.reverse()
        used_resolved = set()
        for seg in chain:
            try:
                used_resolved.add(seg["path"].resolve())
            except OSError:
                used_resolved.add(seg["path"])
        unlinked: list[Path] = []
        for _, p in ordered_entries:
            try:
                pres = p.resolve()
            except OSError:
                pres = p
            if pres not in used_resolved:
                unlinked.append(p)
        return chain, unlinked, warnings

    # Path B: Head has no `history_base` (either single/initial segment or legacy/synthetic multi-segment).
    # If we have a committed head (active_path or sqlite_path), any candidate created strictly
    # after it is an uncommitted/orphaned rollback segment and must NOT truncate the committed head.
    try:
        head_res = head_path.resolve()
    except OSError:
        head_res = head_path

    if committed_head is not None:
        head_idx = next(
            (
                i
                for i, (_, p) in enumerate(ordered_entries)
                if (p.resolve() if p.exists() else p) == head_res
            ),
            None,
        )
        if head_idx is not None:
            active_entries = ordered_entries[: head_idx + 1]
            unlinked_paths = [p for _, p in ordered_entries[head_idx + 1 :]]
        else:
            active_entries = [(head_meta, head_path)]
            unlinked_paths = [p for _, p in ordered_entries if (p.resolve() if p.exists() else p) != head_res]
    else:
        active_entries = ordered_entries
        unlinked_paths = []

    # If the committed head itself starts at ordinal 0 and has no history_base,
    # it is a fresh/initial segment (or full reset); check if earlier same-sid segments exist.
    first_ords = [
        m.get("ordinal") if (m and isinstance(m.get("ordinal"), int)) else None
        for m, _ in active_entries
    ]
    suffix_min_bounds: list[int | None] = [None] * len(active_entries)
    suffix_min: int | None = None
    for idx in range(len(active_entries) - 1, -1, -1):
        suffix_min_bounds[idx] = suffix_min
        cur_ord = first_ords[idx]
        if cur_ord is not None:
            suffix_min = cur_ord if suffix_min is None else min(suffix_min, cur_ord)

    effective: list[dict[str, Any]] = []
    for idx, (m, p) in enumerate(active_entries):
        bound_ord = suffix_min_bounds[idx]
        # Detect legacy/ordinal-less predecessor superseded by a later segment with ordinal == 0
        if bound_ord is not None and first_ords[idx] is None:
            warnings.append(
                f"ambiguous_rollback_boundary: legacy segment {p} has no ordinal while later segment restarts at ordinal {bound_ord}"
            )
        effective.append(
            {
                "path": p,
                "meta": m,
                "role": "effective",
                "end_ordinal_exclusive": bound_ord,
                "end_byte_offset": None,
                "segment_sid": str((m or {}).get("sid") or session_id),
            }
        )
    return effective, unlinked_paths, warnings


def find_state_dbs(codex_home: Path) -> list[Path]:
    """Return existing Codex `state_<N>.sqlite` files across root and `sqlite/` subdir.

    Ordered by descending schema version, preferring root over `sqlite/` on tie.
    """
    candidates: list[tuple[int, int, Path]] = []
    search_dirs = ((0, codex_home), (1, codex_home / "sqlite"))
    seen: set[Path] = set()
    for dir_rank, directory in search_dirs:
        if not directory.is_dir():
            continue
        try:
            for candidate in directory.glob("state_*.sqlite"):
                if not candidate.is_file():
                    continue
                m = _STATE_DB_RE.match(candidate.name)
                if not m:
                    continue
                try:
                    resolved = candidate.resolve()
                except OSError:
                    resolved = candidate
                if resolved in seen:
                    continue
                seen.add(resolved)
                candidates.append((-int(m.group(1)), dir_rank, candidate))
        except OSError:
            continue
    candidates.sort(key=lambda item: (item[0], item[1], str(item[2])))
    return [path for _, _, path in candidates]


def _latest_state_db(codex_home: Path) -> Path | None:
    dbs = find_state_dbs(codex_home)
    return dbs[0] if dbs else None


def query_sqlite_rollout_path_detailed(
    sid: str,
    codex_home: Path,
) -> tuple[Path | None, str | None]:
    """Read-only lookup of Codex's recorded rollout_path for `sid`.

    Returns `(path, error_message)` so callers can distinguish absent DB/row
    from schema/read errors.
    """
    if not sid:
        return None, None
    dbs = find_state_dbs(codex_home)
    if not dbs:
        return None, None
    last_error: str | None = None
    for db_path in dbs:
        try:
            uri = f"{db_path.resolve().as_uri()}?mode=ro"
            with closing(sqlite3.connect(uri, uri=True, timeout=0.15)) as conn:
                row = conn.execute(
                    "SELECT rollout_path FROM threads WHERE id = ? LIMIT 1",
                    (sid,),
                ).fetchone()
                if row and isinstance(row[0], str) and row[0].strip():
                    return Path(row[0].strip()).expanduser(), None
        except (OSError, sqlite3.Error, ValueError) as exc:
            last_error = f"{db_path}: {exc}"
            continue
    return None, last_error


def query_sqlite_rollout_path(sid: str, codex_home: Path) -> Path | None:
    """Read-only lookup of Codex's recorded latest rollout_path for `sid`."""
    path, _ = query_sqlite_rollout_path_detailed(sid, codex_home)
    return path


def query_recent_sqlite_rollouts_detailed(
    codex_home: Path,
    *,
    days: int = 14,
    now: float | None = None,
) -> tuple[list[tuple[str, Path]], list[str]]:
    """Read-only list of recent `(thread_id, rollout_path)` plus any DB query errors."""
    dbs = find_state_dbs(codex_home)
    if not dbs:
        return [], []
    cutoff_sec = int((now if now is not None else time.time()) - max(0, days) * 86400)
    out: list[tuple[str, Path]] = []
    errors: list[str] = []
    db_path = dbs[0]
    try:
        uri = f"{db_path.resolve().as_uri()}?mode=ro"
        with closing(sqlite3.connect(uri, uri=True, timeout=0.25)) as conn:
            if days > 0:
                rows = conn.execute(
                    "SELECT id, rollout_path FROM threads WHERE updated_at >= ? AND rollout_path IS NOT NULL AND rollout_path != ''",
                    (cutoff_sec,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT id, rollout_path FROM threads WHERE rollout_path IS NOT NULL AND rollout_path != ''",
                ).fetchall()
            for tid, rpath in rows:
                if isinstance(tid, str) and tid and isinstance(rpath, str) and rpath.strip():
                    out.append((tid, Path(rpath.strip()).expanduser()))
    except (OSError, sqlite3.Error, ValueError) as exc:
        errors.append(f"{db_path}: {exc}")
    return out, errors


def query_recent_sqlite_rollouts(
    codex_home: Path,
    *,
    days: int = 14,
    now: float | None = None,
) -> list[tuple[str, Path]]:
    rows, _ = query_recent_sqlite_rollouts_detailed(codex_home, days=days, now=now)
    return rows


def session_exists(sid: str, codex_home: Path) -> bool:
    """Find a durable rollout, including suffixed and compressed files.

    A filename can end in `<session-id>.jsonl[.zst]` or
    `<session-id>_<rollout-id>.jsonl[.zst]`. The header identifies the
    conversation; the extra filename ID must never become the resume ID.
    """
    if not sid:
        return False
    for root in codex_session_roots(codex_home, existing_only=True):
        for suffix in CODEX_ROLLOUT_SUFFIXES:
            for pattern in (f"*-{sid}{suffix}", f"*-{sid}_*{suffix}"):
                for path in root.rglob(pattern):
                    if rollout_matches_session(path, sid):
                        return True
    return False


def _open_sessions(arguments: list[str], codex_home: Path | None) -> dict[int, list[dict]]:
    try:
        result = subprocess.run(
            ["lsof", "-nP", *arguments, "-F", "pfan"],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {}
    # lsof returns 1 when one requested name has no matches, even if another
    # name produced valid descriptors. Parse the descriptors it did return.
    if not result.stdout or result.returncode not in (0, 1):
        return {}
    roots = (
        [(codex_home / name).resolve() for name in CODEX_SESSION_ROOT_NAMES]
        if codex_home
        else []
    )
    sessions: dict[int, list[dict]] = {}
    pid, writable = 0, False
    for line in result.stdout.splitlines():
        if line.startswith("p"):
            pid = int(line[1:]) if line[1:].isdigit() else 0
            writable = False
        elif line.startswith("f"):
            writable = False
        elif line.startswith("a"):
            writable = line[1:] in {"w", "u"}
        elif line.startswith("n") and pid and writable:
            path = Path(line[1:])
            try:
                path = path.resolve()
                if path.suffix != ".jsonl":
                    continue
                if codex_home is not None:
                    storage = codex_home.resolve()
                    if not path.is_relative_to(storage):
                        continue
                else:
                    inferred = infer_codex_home(path)
                    if not inferred:
                        continue
                    storage = Path(inferred)
                meta = read_rollout_meta(path, max_lines=1)
                if not meta:
                    continue
                sessions.setdefault(pid, []).append(
                    {
                        "sid": meta["sid"],
                        "cwd": meta["cwd"],
                        "codex_home": str(storage),
                        "rollout": str(path),
                    }
                )
            except (OSError, ValueError, AttributeError):
                continue
    return sessions


def loaded_sessions(pid: int, codex_home: Path | None = None) -> list[dict]:
    return _open_sessions(["-a", "-p", str(pid)], codex_home).get(pid, []) if pid > 0 else []


def session_writers(codex_home: Path) -> dict[int, list[dict]]:
    """Read current writers, including instances outside the selected tmux panes."""
    return _open_sessions(["-c", "codex", "-c", "Codex"], codex_home)


def session_loaded(pid: int, sid: str, codex_home: Path) -> bool:
    """A startup preview/argv is insufficient: require an exact writable rollout."""
    return bool(sid) and any(record["sid"] == sid for record in loaded_sessions(pid, codex_home))


def current_session(tty: str, preferred_sid: str = "") -> dict | None:
    """Find a pane's writable conversation independently of activity hooks."""
    if not tty:
        return None
    try:
        rows = subprocess.run(
            ["ps", "-t", tty.removeprefix("/dev/"), "-o", "pid=,stat=,comm="],
            text=True,
            capture_output=True,
            check=False,
            timeout=2,
        ).stdout.splitlines()
    except (OSError, subprocess.TimeoutExpired):
        return None
    records = {}
    for line in rows:
        fields = line.split(maxsplit=2)
        if len(fields) != 3 or not fields[0].isdigit() or "+" not in fields[1]:
            continue
        name = Path(fields[2]).name.lower()
        if name not in {"codex", "codexx"} and not name.startswith("codex-"):
            continue
        for record in loaded_sessions(int(fields[0])):
            records[(record["codex_home"], record["sid"])] = record
    if len(records) == 1:
        return next(iter(records.values()))
    preferred = [record for record in records.values() if record["sid"] == preferred_sid]
    return preferred[0] if len(preferred) == 1 else None


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Check for a saved Codex conversation")
    parser.add_argument("sid")
    parser.add_argument("codex_home", type=Path)
    arguments = parser.parse_args()
    raise SystemExit(0 if session_exists(arguments.sid, arguments.codex_home.expanduser()) else 1)
