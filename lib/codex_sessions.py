"""Read-only evidence and canonical storage adapter for local Codex conversations."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
import sqlite3
import subprocess
import time
from collections import deque
from contextlib import closing
from pathlib import Path
from typing import Any, Iterator

CODEX_SESSION_ROOT_NAMES = ("sessions", "archived_sessions")
CODEX_ROLLOUT_SUFFIXES = (".jsonl", ".jsonl.zst")
ROLLOUT_META_MAX_BYTES = 4 * 1024 * 1024
ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"
_STATE_DB_RE = re.compile(r"^state_(\d+)\.sqlite$")
_FILENAME_TS_RE = re.compile(
    r"^rollout-(\d{4})-(\d{2})-(\d{2})T(\d{2})-(\d{2})-(\d{2})(?:-[0-9a-fA-F]{8}-|$)"
)
UUID_RE = re.compile(
    r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})"
)


def zstd_binary() -> str:
    return shutil.which("zstd") or "/opt/homebrew/bin/zstd"


def path_identity_key(path: Path) -> tuple[Path, int, int]:
    """Inode-aware cache key `(resolved_path, st_dev, st_ino)` immune to `os.replace`."""
    try:
        resolved = path.resolve()
        st = resolved.stat()
        return (resolved, st.st_dev, st.st_ino)
    except OSError:
        return (path, -1, -1)


def is_compressed_rollout(path: Path) -> bool:
    """Detect zstd rollouts by name, symlink target name, or zstd frame magic bytes."""
    name = path.name.lower()
    if name.endswith(".zst") or name.endswith(".zstd"):
        return True
    try:
        if path.is_symlink():
            target_name = path.resolve().name.lower()
            if target_name.endswith(".zst") or target_name.endswith(".zstd"):
                return True
    except OSError:
        pass
    try:
        with path.open("rb") as fh:
            return fh.read(4) == ZSTD_MAGIC
    except OSError:
        return False


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


def rollout_dedup_priority(path: Path) -> tuple[int, int, int, str]:
    """Canonical preference key: plain > compressed, real file > symlink, sessions/ > archived_sessions/."""
    is_comp = 1 if is_compressed_rollout(path) else 0
    is_sym = 1 if path.is_symlink() else 0
    is_arch = 1 if "archived_sessions" in path.parts else 0
    return (is_comp, is_sym, is_arch, str(path))


def rollout_canonical_key(path: Path, codex_home: Path | None = None) -> Path:
    """Key unifying plain `.jsonl` and compressed/archived `.jsonl.zst` of the same rollout.

    Cross-directory unification (`sessions/` <-> `archived_sessions/`) applies ONLY to
    canonical `rollout-...-<uuid>` filenames. Non-canonical/opaque filenames (such as
    `session.jsonl` in different date directories) stay scoped to their own directory
    so distinct sessions are never collapsed.
    """
    stem = rollout_plain_stem(path)
    _, fn_rid = extract_filename_ids(path)
    if stem.startswith("rollout-") and fn_rid is not None:
        root_str = ""
        if codex_home is not None:
            try:
                root_str = str(codex_home.expanduser().resolve())
            except OSError:
                root_str = str(codex_home)
        else:
            root_str = infer_codex_home(path)
        if root_str:
            return Path(root_str) / stem
    try:
        parent = path.parent.resolve()
    except OSError:
        parent = path.parent
    return parent / stem


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
    raw_ord = hb.get("end_ordinal_exclusive")
    invalid_bounds = False
    if raw_ord is None:
        end_ord: int | None = None
    elif isinstance(raw_ord, int) and not isinstance(raw_ord, bool) and raw_ord >= 0:
        end_ord = raw_ord
    else:
        end_ord = None
        invalid_bounds = True

    raw_byte = hb.get("end_byte_offset")
    if raw_byte is None:
        end_byte: int | None = None
    elif isinstance(raw_byte, int) and not isinstance(raw_byte, bool) and raw_byte >= 0:
        end_byte = raw_byte
    else:
        end_byte = None
        invalid_bounds = True

    return {
        "rollout_id": base_rid,
        "end_ordinal_exclusive": end_ord,
        "end_byte_offset": end_byte,
        "invalid_bounds": invalid_bounds,
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
    # Fallback for novel subdirectories (e.g. <custom_home>/future_threads/...):
    # look up a few levels for state_*.sqlite (root or sqlite/), config.toml,
    # sessions/, archived_sessions/, or a `.codex` root.
    for idx, ancestor in enumerate(resolved.parents):
        if idx > 5:
            break
        if ancestor.name == ".codex":
            return str(ancestor)
        try:
            if (
                (ancestor / "config.toml").is_file()
                or (ancestor / "sessions").is_dir()
                or (ancestor / "archived_sessions").is_dir()
                or bool(find_state_dbs(ancestor))
            ):
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
                [zstd_binary(), "-dcqf", str(path)],
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


def iter_rollout_lines(path: Path, *, strict: bool = False) -> Iterator[str]:
    """Stream decoded text lines from a plain or zstd-compressed rollout."""
    if is_compressed_rollout(path):
        proc = subprocess.Popen(
            [zstd_binary(), "-dcqf", str(path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        assert proc.stdout is not None
        completed = False
        try:
            for raw in proc.stdout:
                yield raw.decode("utf-8", errors="replace")
            completed = True
        finally:
            try:
                proc.stdout.close()
            except OSError:
                pass
            if not completed:
                proc.terminate()
            try:
                rc = proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                proc.kill()
                rc = proc.wait()
        if strict and completed and rc != 0:
            raise OSError(f"zstd decode failed with exit code {rc} for {path}")
        return

    with path.open("rb") as fh:
        for raw in fh:
            yield raw.decode("utf-8", errors="replace")


def iter_rollout_records_reverse(
    path: Path,
    chunk_size: int = 64 * 1024,
    *,
    max_compressed_tail_bytes: int = 2 * 1024 * 1024,
) -> Iterator[dict[str, Any]]:
    """Yield parsed JSON dict records from end to start, supporting plain & zstd rollouts.

    For zstd rollouts, retains only a bounded byte tail (`max_compressed_tail_bytes`)
    in a rolling deque so reading the tail of a large compressed rollout does not
    materialize the entire decompressed log in Python memory.
    """
    if is_compressed_rollout(path):
        try:
            proc = subprocess.Popen(
                [zstd_binary(), "-dcqf", str(path)],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
        except OSError:
            return
        assert proc.stdout is not None
        tail: deque[bytes] = deque()
        buffered = 0
        try:
            for raw_b in proc.stdout:
                if not raw_b.strip():
                    continue
                tail.append(raw_b)
                buffered += len(raw_b)
                while len(tail) > 1 and buffered > max_compressed_tail_bytes:
                    buffered -= len(tail.popleft())
        except OSError:
            return
        finally:
            try:
                proc.stdout.close()
            except OSError:
                pass
            try:
                proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        for raw_b in reversed(tail):
            try:
                obj = json.loads(raw_b.decode("utf-8"))
                if isinstance(obj, dict):
                    yield obj
            except (UnicodeDecodeError, ValueError):
                continue
        return

    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            position = handle.tell()
            pending = b""
            while position > 0:
                size = min(chunk_size, position)
                position -= size
                handle.seek(position)
                lines_b = (handle.read(size) + pending).split(b"\n")
                pending = lines_b[0]
                for raw_b in reversed(lines_b[1:]):
                    if not raw_b.strip():
                        continue
                    try:
                        obj = json.loads(raw_b.decode("utf-8"))
                        if isinstance(obj, dict):
                            yield obj
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        continue
            if pending.strip():
                try:
                    obj = json.loads(pending.decode("utf-8"))
                    if isinstance(obj, dict):
                        yield obj
                except (UnicodeDecodeError, json.JSONDecodeError):
                    pass
    except OSError:
        return


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
            return None, "invalid_meta"
        sid = payload.get("id")
        if not isinstance(sid, str) or not sid:
            return None, "invalid_meta"
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


def _parse_iso_utc_epoch(ts: str) -> float | None:
    if not isinstance(ts, str) or not ts.strip():
        return None
    s = ts.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        parsed = dt.datetime.fromisoformat(s)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.timestamp()


def _parse_naive_filename_epoch(fn_ts: str) -> float | None:
    if not isinstance(fn_ts, str) or not fn_ts:
        return None
    try:
        parsed = dt.datetime.fromisoformat(fn_ts)
    except ValueError:
        return None
    return parsed.replace(tzinfo=dt.timezone.utc).timestamp()


def _local_tz_offsets_for_wall_clock(fn_ts: str) -> tuple[float, float]:
    """Return `(fold0_offset_sec, fold1_offset_sec)` in host/TZ local timezone."""
    try:
        base = dt.datetime.fromisoformat(fn_ts)
    except ValueError:
        return 0.0, 0.0
    tz_env = os.environ.get("TZ", "").strip()
    if tz_env:
        try:
            from zoneinfo import ZoneInfo

            tz_obj = ZoneInfo(tz_env.lstrip(":"))
            off0 = base.replace(tzinfo=tz_obj, fold=0).utcoffset()
            off1 = base.replace(tzinfo=tz_obj, fold=1).utcoffset()
            s0 = off0.total_seconds() if off0 is not None else 0.0
            s1 = off1.total_seconds() if off1 is not None else s0
            return s0, s1
        except Exception:
            pass
    try:
        off0 = base.replace(fold=0).astimezone().utcoffset()
        off1 = base.replace(fold=1).astimezone().utcoffset()
        s0 = off0.total_seconds() if off0 is not None else 0.0
        s1 = off1.total_seconds() if off1 is not None else s0
        return s0, s1
    except ValueError:
        return 0.0, 0.0


def _local_tz_offset_seconds(fn_ts: str) -> float:
    return _local_tz_offsets_for_wall_clock(fn_ts)[0]


def sort_rollout_entries_with_warnings(
    entries: list[tuple[dict[str, Any] | None, Path]],
) -> tuple[list[tuple[dict[str, Any] | None, Path]], list[str]]:
    """Order rollout segments earliest-to-latest and report ambiguous DST ordering.

    Normalizes UTC `session_meta.timestamp` and local-wall-clock canonical filename
    timestamps (`rollout-YYYY-MM-DDTHH-MM-SS`) onto a single UTC epoch timeline.
    Uses per-date local TZ rules when siblings match host TZ (exact across DST
    transitions), or nearest-in-time sibling offset for logs from another timezone,
    and flags `ambiguous_timestamp_order` when alternate plausible DST offsets
    could flip relative segment order.
    """
    if len(entries) <= 1:
        return list(entries), []

    meta_epochs: list[float | None] = []
    fn_strings: list[str] = []
    fn_naives: list[float | None] = []
    known_siblings: list[tuple[int, float, float, float, float]] = []

    for idx, (meta, path) in enumerate(entries):
        m_ts = str(meta.get("timestamp") or "") if meta else ""
        m_ep = _parse_iso_utc_epoch(m_ts)
        f_ts = (str(meta.get("filename_ts") or "") if meta else "") or extract_filename_ts(path)
        f_nv = _parse_naive_filename_epoch(f_ts)
        meta_epochs.append(m_ep)
        fn_strings.append(f_ts)
        fn_naives.append(f_nv)
        if m_ep is not None and f_nv is not None:
            obs_off = round((f_nv - m_ep) / 60.0) * 60.0
            loc0, loc1 = _local_tz_offsets_for_wall_clock(f_ts)
            known_siblings.append((idx, f_nv, obs_off, loc0, loc1))

    all_match_local = bool(known_siblings) and all(
        min(abs(obs - l0), abs(obs - l1)) <= 120.0
        for _, _, obs, l0, l1 in known_siblings
    )

    normalized_epochs: list[float | None] = []
    alt_epochs: list[list[float]] = []

    for m_ep, f_ts, f_nv in zip(meta_epochs, fn_strings, fn_naives):
        if m_ep is not None:
            normalized_epochs.append(m_ep)
            alt_epochs.append([m_ep])
        elif f_nv is not None:
            if all_match_local or not known_siblings:
                loc0, loc1 = _local_tz_offsets_for_wall_clock(f_ts)
                ep0 = f_nv - loc0
                normalized_epochs.append(ep0)
                if abs(loc0 - loc1) > 120.0:
                    alt_epochs.append([f_nv - loc0, f_nv - loc1])
                else:
                    alt_epochs.append([ep0])
            else:
                nearest = min(known_siblings, key=lambda s: abs(s[1] - f_nv))
                nearest_dist = abs(nearest[1] - f_nv)
                chosen_off = nearest[2]
                prev_sibs = [s for s in known_siblings if s[1] <= f_nv]
                next_sibs = [s for s in known_siblings if s[1] >= f_nv]
                prev_s = max(prev_sibs, key=lambda s: s[1]) if prev_sibs else None
                next_s = min(next_sibs, key=lambda s: s[1]) if next_sibs else None
                cand_offs = {chosen_off}
                if prev_s is not None and next_s is not None and abs(prev_s[2] - next_s[2]) > 120.0:
                    d_prev = abs(f_nv - prev_s[1])
                    d_next = abs(next_s[1] - f_nv)
                    if not (min(d_prev, d_next) <= 3600.0 and max(d_prev, d_next) > 43200.0):
                        cand_offs.add(prev_s[2])
                        cand_offs.add(next_s[2])
                elif nearest_dist > 86400.0:
                    cand_offs.add(chosen_off - 3600.0)
                    cand_offs.add(chosen_off + 3600.0)
                normalized_epochs.append(f_nv - chosen_off)
                alt_epochs.append([f_nv - off for off in sorted(cand_offs)])
        else:
            normalized_epochs.append(None)
            alt_epochs.append([])

    warnings: list[str] = []
    if all(ep is not None for ep in normalized_epochs):
        for i, alts in enumerate(alt_epochs):
            if len(alts) <= 1:
                continue
            lo, hi = min(alts), max(alts)
            flips = any(
                j != i
                and normalized_epochs[j] is not None
                and lo <= float(normalized_epochs[j]) <= hi
                for j in range(len(entries))
            )
            if flips:
                warnings.append(
                    f"ambiguous_timestamp_order: segment {entries[i][1]} lacks session_meta.timestamp "
                    "across a DST or timezone boundary"
                )
        ordered = [
            entry
            for _, entry in sorted(
                zip(normalized_epochs, entries),
                key=lambda pair: (
                    float(pair[0]),  # type: ignore[arg-type]
                    int(pair[1][0]["ordinal"])
                    if (pair[1][0] and isinstance(pair[1][0].get("ordinal"), int))
                    else 0,
                    str(pair[1][1]),
                ),
            )
        ]
        return ordered, warnings

    all_have_ordinal = all(
        meta is not None and isinstance(meta.get("ordinal"), int)
        for meta, _ in entries
    )
    if all_have_ordinal:
        ordered = sorted(
            entries,
            key=lambda item: (
                int(item[0]["ordinal"]),  # type: ignore[index]
                str(item[1]),
            ),
        )
        return ordered, warnings

    ordered = [
        entry
        for _, entry in sorted(
            zip(normalized_epochs, entries),
            key=lambda pair: (
                0 if pair[0] is not None else 1,
                float(pair[0]) if pair[0] is not None else 0.0,
                str(pair[1][1]),
            ),
        )
    ]
    return ordered, warnings


def sort_rollout_entries(
    entries: list[tuple[dict[str, Any] | None, Path]],
) -> list[tuple[dict[str, Any] | None, Path]]:
    ordered, _ = sort_rollout_entries_with_warnings(entries)
    return ordered


def deduplicate_rollout_paths(
    paths: list[Path],
    *,
    codex_home: Path | None = None,
) -> list[Path]:
    """Deduplicate physical files (symlinks + plain/compressed/archived sibling pairs).

    Prefers real plain `.jsonl` over `.jsonl.zst` (including `.jsonl` symlinks to
    `.jsonl.zst`), and prefers `sessions/` over `archived_sessions/`.
    """
    by_inode: dict[tuple[int, int], Path] = {}
    by_canonical: dict[Any, Path] = {}

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
            if rollout_dedup_priority(path) < rollout_dedup_priority(prev):
                by_inode[inode_key] = path
            continue
        by_inode[inode_key] = path

    for path in by_inode.values():
        ckey = rollout_canonical_key(path, codex_home=codex_home)
        prev = by_canonical.get(ckey)
        if prev is None:
            by_canonical[ckey] = path
            continue
        # Guard cross-directory canonical collisions by verifying header SID match.
        try:
            same_parent = prev.parent.resolve() == path.parent.resolve()
        except OSError:
            same_parent = prev.parent == path.parent
        if not same_parent:
            m_prev = read_rollout_meta(prev, max_lines=1)
            m_curr = read_rollout_meta(path, max_lines=1)
            if m_prev and m_curr and m_prev["sid"] != m_curr["sid"]:
                alt_key = (ckey, m_curr["sid"])
                alt_prev = by_canonical.get(alt_key)
                if alt_prev is None or rollout_dedup_priority(path) < rollout_dedup_priority(alt_prev):
                    by_canonical[alt_key] = path
                continue
        if rollout_dedup_priority(path) < rollout_dedup_priority(prev):
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
    return deduplicate_rollout_paths(raw, codex_home=codex_home)


def build_rollout_id_index(paths: list[Path]) -> dict[str, Path]:
    """Build `{physical_rollout_id: best_path}` preferring plain `.jsonl` over `.jsonl.zst`."""
    index: dict[str, Path] = {}
    for p in deduplicate_rollout_paths(paths):
        _, rid = extract_filename_ids(p)
        if rid and rid not in index:
            index[rid] = p
    return index


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
    deduped = deduplicate_rollout_paths(raw_candidates, codex_home=codex_home)
    matched: list[tuple[dict[str, Any] | None, Path]] = []
    for path in deduped:
        meta, err = read_rollout_meta_detailed(path, max_lines=1)
        if verify_header:
            if err is not None or not meta or meta["sid"] != sid:
                continue
        else:
            if err in ("header_too_large", "invalid_json", "invalid_meta", "io_error"):
                continue
            if meta is not None and meta["sid"] != sid:
                continue
        matched.append((meta, path))
    return [path for _, path in sort_rollout_entries(matched)]


def resolve_committed_head(
    sid: str,
    codex_home: Path,
    *,
    verify_header: bool = True,
) -> Path | None:
    """Return the authoritative committed rollout head for `sid`.

    Checks verified `state_*.sqlite` first (preferring plain `.jsonl` over
    `.jsonl.zst` via `resolve_alternate_rollout_path`) so an uncommitted
    rollback segment written to disk before SQLite switches never overrides
    committed history or busy/idle activity state. Falls back to the latest
    segment from `find_session_rollouts` when SQLite has no verified entry.
    """
    if not sid:
        return None
    sqlite_path = query_sqlite_rollout_path(sid, codex_home)
    if sqlite_path is not None:
        resolved = resolve_alternate_rollout_path(sqlite_path, codex_home)
        if resolved is not None:
            meta, err = read_rollout_meta_detailed(resolved, max_lines=1)
            if verify_header:
                if err is None and meta is not None and meta["sid"] == sid:
                    return resolved
            else:
                if err is None and meta is not None:
                    if meta["sid"] == sid:
                        return resolved
                elif err == "missing_meta" and sid in resolved.name:
                    return resolved
    transcripts = find_session_rollouts(sid, codex_home, verify_header=verify_header)
    return transcripts[-1] if transcripts else None


def _populate_rollout_index_once(
    codex_home: Path,
    rollout_index: dict[str, Path],
) -> None:
    sentinel = f"__scanned__:{codex_home}"
    if sentinel in rollout_index:
        return
    for path in iter_codex_rollout_files(codex_home):
        _, fn_rid = extract_filename_ids(path)
        if fn_rid and fn_rid not in rollout_index:
            rollout_index[fn_rid] = path
        stem_key = f"__stem__:{rollout_plain_stem(path)}"
        if stem_key not in rollout_index:
            rollout_index[stem_key] = path
    rollout_index[sentinel] = codex_home


def find_rollout_by_rollout_id(
    rollout_id: str,
    codex_home: Path,
    *,
    rollout_index: dict[str, Path] | None = None,
) -> Path | None:
    """Locate a physical rollout file by its physical `rollout_id` without repeated scans."""
    if not rollout_id:
        return None
    if rollout_index is not None:
        if rollout_id in rollout_index:
            return rollout_index[rollout_id]
        _populate_rollout_index_once(codex_home, rollout_index)
        return rollout_index.get(rollout_id)

    raw_candidates: list[Path] = []
    for root in codex_session_roots(codex_home, existing_only=True):
        for suffix in CODEX_ROLLOUT_SUFFIXES:
            for pattern in (f"*-{rollout_id}{suffix}", f"*_{rollout_id}{suffix}"):
                try:
                    raw_candidates.extend(root.rglob(pattern))
                except OSError:
                    continue
    deduped = deduplicate_rollout_paths(raw_candidates, codex_home=codex_home)
    for path in deduped:
        _, fn_rid = extract_filename_ids(path)
        if fn_rid == rollout_id:
            return path
    return None


def resolve_alternate_rollout_path(
    path: Path,
    codex_home: Path | None = None,
    *,
    rollout_index: dict[str, Path] | None = None,
) -> Path | None:
    """Resolve the authoritative on-disk representation for a rollout path.

    Always prefers an existing plain `.jsonl` over a compressed `.jsonl.zst`
    counterpart (matching upstream `compression.rs:924`), and searches across
    `sessions/` and `archived_sessions/` when a file has moved or been compressed.
    """
    home = codex_home
    if home is None:
        inferred = infer_codex_home(path)
        home = Path(inferred) if inferred else None

    stem = rollout_plain_stem(path)
    _, fn_rid = extract_filename_ids(path)
    path_exists = path.is_file()
    path_is_comp = is_compressed_rollout(path) if path_exists else path.name.lower().endswith((".zst", ".zstd"))

    # 1. If path is compressed (or points to compressed), check plain `.jsonl` counterparts FIRST!
    if path_is_comp:
        plain_sibling = path.with_name(stem + ".jsonl")
        if plain_sibling.is_file() and not is_compressed_rollout(plain_sibling):
            return plain_sibling
        if home is not None:
            if rollout_index is not None:
                stem_cand = rollout_index.get(f"__stem__:{stem}")
                if stem_cand and stem_cand.is_file() and not is_compressed_rollout(stem_cand):
                    return stem_cand
                if fn_rid:
                    rid_cand = rollout_index.get(fn_rid)
                    if rid_cand and rid_cand.is_file() and not is_compressed_rollout(rid_cand):
                        return rid_cand
            else:
                if fn_rid:
                    found = find_rollout_by_rollout_id(fn_rid, home)
                    if found is not None and found.is_file() and not is_compressed_rollout(found):
                        return found

    if path_exists:
        return path

    # 2. Path missing on disk: check compressed sibling in same directory
    if not path_is_comp and path.name.lower().endswith(".jsonl"):
        comp = path.with_name(path.name + ".zst")
        if comp.is_file():
            return comp

    # 3. Search sessions/ <-> archived_sessions/ by stem or physical rollout_id
    if home is not None:
        if rollout_index is not None:
            _populate_rollout_index_once(home, rollout_index)
            stem_cand = rollout_index.get(f"__stem__:{stem}")
            if stem_cand and stem_cand.is_file():
                return stem_cand
            if fn_rid:
                rid_cand = rollout_index.get(fn_rid)
                if rid_cand and rid_cand.is_file():
                    return rid_cand
        else:
            if fn_rid:
                found = find_rollout_by_rollout_id(fn_rid, home)
                if found is not None and found.is_file():
                    return found
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
    meta_cache: dict[Any, dict[str, Any] | None] | None = None,
    rollout_index: dict[str, Path] | None = None,
) -> tuple[list[dict[str, Any]], list[Path], list[str]]:
    """Resolve the active effective segment chain for `session_id`.

    Authoritative head priority:
      - When verified `sqlite_path` exists, it is the committed head of the
        conversation. If a live pane's `active_path` points to a DIFFERENT
        physical file (e.g., an uncommitted rollback segment written before
        SQLite switches), `sqlite_path` remains the effective head so pane reads
        and SID reads stay identical and committed history is never truncated;
        an explicit `uncommitted_active_rollout` warning is emitted.
      - When no verified `sqlite_path` exists, verified `active_path` wins over
        unanchored directory candidates.
    """
    cache = meta_cache if meta_cache is not None else {}

    def _meta(p: Path) -> dict[str, Any] | None:
        key = path_identity_key(p)
        if key not in cache:
            cache[key] = read_rollout_meta(p, max_lines=1)
        return cache[key]

    deduped = deduplicate_rollout_paths(candidates, codex_home=codex_home)
    same_sid_entries: list[tuple[dict[str, Any] | None, Path]] = []
    for p in deduped:
        m = _meta(p)
        if m is not None and m["sid"] != session_id:
            continue
        same_sid_entries.append((m, p))

    ordered_entries, sort_warnings = sort_rollout_entries_with_warnings(same_sid_entries)
    if not ordered_entries:
        return [], [], []

    local_rindex: dict[str, Path] = rollout_index if rollout_index is not None else {}
    for m, p in ordered_entries:
        rid = (m or {}).get("rollout_id") or extract_filename_ids(p)[1]
        if isinstance(rid, str) and rid and rid not in local_rindex:
            local_rindex[rid] = p

    def _match_anchor(anchor: Path | None) -> tuple[dict[str, Any] | None, Path] | None:
        if anchor is None:
            return None
        resolved_anchor = resolve_alternate_rollout_path(anchor, codex_home, rollout_index=local_rindex)
        if resolved_anchor is None:
            return None
        m = _meta(resolved_anchor)
        if not m or m["sid"] != session_id:
            return None
        return m, resolved_anchor

    warnings: list[str] = []
    sqlite_head = _match_anchor(sqlite_path)
    active_head = _match_anchor(active_path)

    if sqlite_head is not None and active_head is not None:
        if path_identity_key(sqlite_head[1]) != path_identity_key(active_head[1]):
            warnings.append(
                f"uncommitted_active_rollout: live pane writes {active_head[1]} "
                f"while state sqlite commits {sqlite_head[1]}"
            )
        committed_head: tuple[dict[str, Any] | None, Path] | None = sqlite_head
    elif sqlite_head is not None:
        committed_head = sqlite_head
    elif active_head is not None:
        committed_head = active_head
    else:
        committed_head = None

    head_meta, head_path = committed_head if committed_head is not None else ordered_entries[-1]
    if sort_warnings and (
        committed_head is None or head_meta is None or head_meta.get("history_base") is None
    ):
        warnings.extend(sort_warnings)

    # Path A: Head participates in Codex's explicit `history_base` lineage chain
    if head_meta is not None and head_meta.get("history_base") is not None:
        chain: list[dict[str, Any]] = []
        seen_rids: set[str] = set()
        seen_keys: set[tuple[Path, int, int]] = set()
        cur_meta: dict[str, Any] | None = head_meta
        cur_path: Path | None = head_path
        cur_end_ord: int | None = None
        cur_end_byte: int | None = None

        while cur_path is not None:
            cur_key = path_identity_key(cur_path)
            cur_rid = (cur_meta or {}).get("rollout_id") or extract_filename_ids(cur_path)[1] or str(cur_key[0])
            if cur_rid in seen_rids or cur_key in seen_keys:
                warnings.append(f"lineage_cycle_detected: cycle at rollout {cur_rid}")
                break
            seen_rids.add(cur_rid)
            seen_keys.add(cur_key)
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
            if hb.get("invalid_bounds"):
                warnings.append(
                    f"invalid_history_base_bounds: segment {cur_path} declared invalid history_base bounds"
                )
            elif hb.get("end_ordinal_exclusive") is None and hb.get("end_byte_offset") is None:
                warnings.append(
                    f"history_base_missing_bounds: segment {cur_path} references base rollout {hb['rollout_id']} "
                    "without exclusive ordinal or byte bounds"
                )
            base_rid = hb["rollout_id"]
            base_path = local_rindex.get(base_rid)
            if base_path is None and codex_home is not None:
                base_path = find_rollout_by_rollout_id(base_rid, codex_home, rollout_index=local_rindex)
            if base_path is not None:
                base_path = resolve_alternate_rollout_path(base_path, codex_home, rollout_index=local_rindex) or base_path
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
        used_keys = {path_identity_key(seg["path"]) for seg in chain}
        unlinked: list[Path] = [
            p for _, p in ordered_entries if path_identity_key(p) not in used_keys
        ]
        return chain, unlinked, warnings

    # Path B: Head has no `history_base` (initial segment or legacy/synthetic multi-segment).
    head_key = path_identity_key(head_path)
    if committed_head is not None:
        head_idx = next(
            (i for i, (_, p) in enumerate(ordered_entries) if path_identity_key(p) == head_key),
            None,
        )
        if head_idx is not None:
            active_entries = ordered_entries[: head_idx + 1]
            unlinked_paths = [p for _, p in ordered_entries[head_idx + 1 :]]
        else:
            active_entries = [(head_meta, head_path)]
            unlinked_paths = [p for _, p in ordered_entries if path_identity_key(p) != head_key]
    else:
        active_entries = ordered_entries
        unlinked_paths = []

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
    """Read-only list of recent `(thread_id, rollout_path)` across all state DBs."""
    dbs = find_state_dbs(codex_home)
    if not dbs:
        return [], []
    cutoff_sec = int((now if now is not None else time.time()) - max(0, days) * 86400)
    by_tid: dict[str, Path] = {}
    errors: list[str] = []
    for db_path in dbs:
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
                        # Higher-version DBs come first and win per thread_id.
                        by_tid.setdefault(tid, Path(rpath.strip()).expanduser())
        except (OSError, sqlite3.Error, ValueError) as exc:
            errors.append(f"{db_path}: {exc}")
    return list(by_tid.items()), errors


def query_recent_sqlite_rollouts(
    codex_home: Path,
    *,
    days: int = 14,
    now: float | None = None,
) -> list[tuple[str, Path]]:
    rows, _ = query_recent_sqlite_rollouts_detailed(codex_home, days=days, now=now)
    return rows


def session_exists(sid: str, codex_home: Path) -> bool:
    """Find a durable rollout via standard roots or verified `state_*.sqlite`."""
    if not sid:
        return False
    for root in codex_session_roots(codex_home, existing_only=True):
        for suffix in CODEX_ROLLOUT_SUFFIXES:
            for pattern in (f"*-{sid}{suffix}", f"*-{sid}_*{suffix}"):
                for path in root.rglob(pattern):
                    if rollout_matches_session(path, sid):
                        return True
    sqlite_path = query_sqlite_rollout_path(sid, codex_home)
    if sqlite_path is not None:
        resolved = resolve_alternate_rollout_path(sqlite_path, codex_home)
        if resolved is not None and rollout_matches_session(resolved, sid):
            return True
    return False


def session_is_known(sid: str, codex_home: Path) -> bool:
    """Return True if `sid` exists durably OR has a recorded SQLite/rollout entry.

    Used by recovery so a corrupt, missing, or non-standard-named rollout for a
    bound session `B` marks `B` invalid instead of silently falling back to a
    stale argv `A`.
    """
    if not sid:
        return False
    if session_exists(sid, codex_home):
        return True
    if query_sqlite_rollout_path(sid, codex_home) is not None:
        return True
    nonstandard_or_double: list[Path] = []
    for root in codex_session_roots(codex_home, existing_only=True):
        for suffix in CODEX_ROLLOUT_SUFFIXES:
            for pattern in (f"*-{sid}{suffix}", f"*-{sid}_*{suffix}"):
                for path in root.rglob(pattern):
                    if path.is_file():
                        return True
    for path in iter_codex_rollout_files(codex_home):
        fn_sid, fn_rid = extract_filename_ids(path)
        plain_stem = rollout_plain_stem(path)
        if not (fn_sid and fn_sid == fn_rid and plain_stem.startswith("rollout-")):
            nonstandard_or_double.append(path)
    for path in nonstandard_or_double:
        if rollout_matches_session(path, sid):
            return True
    return False


def _extract_env_var_from_ps_line(ps_line: str, var_name: str) -> str:
    if not ps_line or not var_name:
        return ""
    m = re.search(
        rf"(?:^|\s){re.escape(var_name)}=(.*?)(?=\s+[A-Za-z_][A-Za-z0-9_]*=|$)",
        ps_line.rstrip("\r\n"),
    )
    return m.group(1).strip() if m else ""


def _process_env_codex_home(pid: int) -> str:
    if pid <= 0:
        return ""
    try:
        res = subprocess.run(
            ["ps", "eww", "-o", "command=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=1,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if res.returncode != 0 or not res.stdout:
        return ""
    val = _extract_env_var_from_ps_line(res.stdout, "CODEX_HOME")
    if not val:
        return ""
    try:
        return str(Path(val).expanduser().resolve())
    except OSError:
        return val


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
    if not result.stdout or result.returncode not in (0, 1):
        return {}
    sessions: dict[int, list[dict]] = {}
    pid_env_cache: dict[int, str] = {}
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
                        if pid not in pid_env_cache:
                            pid_env_cache[pid] = _process_env_codex_home(pid)
                        inferred = pid_env_cache[pid]
                    if not inferred:
                        continue
                    storage = Path(inferred)
                    if not path.is_relative_to(storage):
                        continue
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
    parser.add_argument("--known", action="store_true", help="Exit 0 if session is durable or recorded")
    parser.add_argument("sid")
    parser.add_argument("codex_home", type=Path)
    arguments = parser.parse_args()
    check = session_is_known if arguments.known else session_exists
    raise SystemExit(0 if check(arguments.sid, arguments.codex_home.expanduser()) else 1)
