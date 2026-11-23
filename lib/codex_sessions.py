"""Read-only evidence and canonical storage adapter for local Codex conversations."""

from __future__ import annotations

import argparse
import ctypes
import datetime as dt
import functools
import json
import os
import re
import shutil
import sqlite3
import struct
import subprocess
import sys
import time
from collections import deque
from contextlib import closing
from pathlib import Path
from typing import Any, Iterable, Iterator

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
_CANONICAL_SINGLE_UUID_RE = re.compile(
    r"^rollout-\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}-"
    r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})$"
)
_CANONICAL_ROLLOUT_STEM_RE = re.compile(
    r"^rollout-\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}-"
    r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})"
    r"(?:_([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}))?$"
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


def _has_valid_canonical_ts(stem: str) -> bool:
    m = _FILENAME_TS_RE.match(stem)
    if not m:
        return False
    try:
        dt.datetime(
            int(m.group(1)),
            int(m.group(2)),
            int(m.group(3)),
            int(m.group(4)),
            int(m.group(5)),
            int(m.group(6)),
        )
    except ValueError:
        return False
    return True


def is_canonical_single_uuid_rollout(path: Path) -> bool:
    """True only for Codex's exact single-UUID filename `rollout-YYYY-MM-DDTHH-MM-SS-<uuid>.jsonl[.zst]` with a valid calendar timestamp."""
    stem = rollout_plain_stem(path)
    return bool(_CANONICAL_SINGLE_UUID_RE.match(stem)) and _has_valid_canonical_ts(stem)


def is_canonical_rollout_name(path: Path) -> bool:
    """True only for Codex's exact canonical filename `rollout-YYYY-MM-DDTHH-MM-SS-<uuid>[_<uuid>].jsonl[.zst]` with a valid calendar timestamp."""
    stem = rollout_plain_stem(path)
    return bool(_CANONICAL_ROLLOUT_STEM_RE.match(stem)) and _has_valid_canonical_ts(stem)


def rollout_dedup_priority(path: Path) -> tuple[int, int, int, str]:
    """Canonical preference key: plain > compressed, real file > symlink, sessions/ > archived_sessions/."""
    is_comp = 1 if is_compressed_rollout(path) else 0
    is_sym = 1 if path.is_symlink() else 0
    is_arch = 1 if "archived_sessions" in path.parts else 0
    return (is_comp, is_sym, is_arch, str(path))


def rollout_canonical_key(path: Path, codex_home: Path | None = None) -> Path:
    """Key unifying plain `.jsonl` and compressed/archived `.jsonl.zst` of the same rollout.

    Cross-directory unification (`sessions/` <-> `archived_sessions/`) applies ONLY to
    exact canonical `rollout-YYYY-MM-DDTHH-MM-SS-<uuid>[_<uuid>]` filenames.
    Non-canonical/opaque filenames stay scoped to their own directory so distinct
    sessions are never collapsed.
    """
    stem = rollout_plain_stem(path)
    if is_canonical_rollout_name(path):
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
            with path.open("rb") as fh:
                proc = subprocess.Popen(
                    [zstd_binary(), "-dcq"],
                    stdin=fh,
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


def iter_rollout_bytes_lines(path: Path, *, strict: bool = False) -> Iterator[bytes]:
    """Stream raw byte lines (preserving exact line lengths) from a plain or zstd rollout."""
    if is_compressed_rollout(path):
        with path.open("rb") as fh:
            proc = subprocess.Popen(
                [zstd_binary(), "-dcq"],
                stdin=fh,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
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
                rc = proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                proc.kill()
                rc = proc.wait()
        if strict and completed and rc != 0:
            raise OSError(f"zstd decode failed with exit code {rc} for {path}")
        return

    with path.open("rb") as fh:
        for raw in fh:
            yield raw


def iter_rollout_lines(path: Path, *, strict: bool = False) -> Iterator[str]:
    """Stream decoded text lines from a plain or zstd-compressed rollout."""
    for raw in iter_rollout_bytes_lines(path, strict=strict):
        yield raw.decode("utf-8", errors="replace")


def iter_rollout_records_reverse(
    path: Path,
    chunk_size: int = 64 * 1024,
    *,
    max_compressed_tail_bytes: int = 2 * 1024 * 1024,
    max_bytes: int | None = None,
) -> Iterator[dict[str, Any]]:
    """Yield parsed JSON dict records from end to start, supporting plain & zstd rollouts.

    For zstd rollouts, retains only a bounded byte tail (`max_compressed_tail_bytes`)
    in a rolling deque so reading the tail of a large compressed rollout does not
    materialize the entire decompressed log in Python memory. Note that `json.loads`
    still requires decoding each individual line whole, so peak memory for a single
    oversized record (e.g. an inline base64 image line) scales with that single line's
    size. When `max_bytes` is set, only records ending at or before `max_bytes` are
    yielded.
    """
    if is_compressed_rollout(path):
        try:
            with path.open("rb") as fh:
                proc = subprocess.Popen(
                    [zstd_binary(), "-dcq"],
                    stdin=fh,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                )
        except OSError:
            return
        assert proc.stdout is not None
        tail: deque[bytes] = deque()
        buffered = 0
        byte_pos = 0
        try:
            for raw_b in proc.stdout:
                line_start = byte_pos
                byte_pos += len(raw_b)
                if max_bytes is not None and (
                    line_start >= max_bytes or (line_start < max_bytes < byte_pos)
                ):
                    break
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
            if max_bytes is not None:
                proc.terminate()
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
            file_size = handle.tell()
            position = min(file_size, max_bytes) if max_bytes is not None else file_size
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


@functools.lru_cache(maxsize=16)
def _iana_year_fold_table(year: int) -> dict[tuple[int, int, int, int], tuple[float, ...]]:
    """Build a once-per-year lookup table of all IANA DST fall-back fold hours.

    Enumerates `zoneinfo.available_timezones()` at most ONCE per calendar year,
    then answers every `(month, day, hour, observed_offset_sec)` query in O(1).
    """
    try:
        from zoneinfo import ZoneInfo, available_timezones
    except Exception:
        return {}
    probes = [
        dt.datetime(year, m, 1, 12, 0, tzinfo=dt.timezone.utc) for m in range(1, 13)
    ] + [dt.datetime(year, 12, 31, 23, 0, tzinfo=dt.timezone.utc)]
    raw_table: dict[tuple[int, int, int, int], set[float]] = {}
    for name in available_timezones():
        if name.startswith(("Etc/", "SystemV/")):
            continue
        try:
            tz = ZoneInfo(name)
            offs: list[float] = []
            for p in probes:
                uo = p.astimezone(tz).utcoffset()
                if uo is None:
                    raise ValueError("missing offset")
                offs.append(uo.total_seconds())
        except Exception:
            continue
        if len(set(offs)) <= 1:
            continue
        for k in range(len(probes) - 1):
            if offs[k + 1] < offs[k]:
                lo = int(probes[k].timestamp())
                hi = int(probes[k + 1].timestamp())
                off_before = offs[k]
                off_after = offs[k + 1]
                while hi - lo > 60:
                    mid = (lo + hi) // 2
                    mo = dt.datetime.fromtimestamp(mid, tz=dt.timezone.utc).astimezone(tz).utcoffset()
                    if mo is not None and mo.total_seconds() == off_before:
                        lo = mid
                    else:
                        hi = mid
                drop = int(round(off_before - off_after))
                if drop <= 120:
                    continue
                for step in range(0, drop, 1800):
                    loc = dt.datetime.fromtimestamp(hi + step, tz=dt.timezone.utc).astimezone(tz)
                    b_sec = int(round(off_before / 60.0) * 60)
                    a_sec = int(round(off_after / 60.0) * 60)
                    raw_table.setdefault((loc.month, loc.day, loc.hour, b_sec), set()).add(off_after)
                    raw_table.setdefault((loc.month, loc.day, loc.hour, a_sec), set()).add(off_before)
    return {k: tuple(sorted(v)) for k, v in raw_table.items()}


def _iana_dst_fold_offsets(year: int, month: int, day: int, hour: int, observed_off_sec: int) -> tuple[float, ...]:
    """Return any alternate DST fold UTC offsets observed by IANA zones matching `observed_off_sec` at this local wall clock."""
    return _iana_year_fold_table(year).get((month, day, hour, observed_off_sec), ())


def _foreign_dst_fold_offsets(fn_ts: str, chosen_off: float) -> tuple[float, ...]:
    try:
        parsed = dt.datetime.fromisoformat(fn_ts)
    except ValueError:
        return ()
    return _iana_dst_fold_offsets(
        parsed.year,
        parsed.month,
        parsed.day,
        parsed.hour,
        int(round(chosen_off / 60.0) * 60),
    )


def sort_rollout_entries_with_warnings(
    entries: list[tuple[dict[str, Any] | None, Path]],
) -> tuple[list[tuple[dict[str, Any] | None, Path]], list[str]]:
    """Order rollout segments earliest-to-latest and report ambiguous DST ordering.

    Normalizes UTC `session_meta.timestamp` and local-wall-clock canonical filename
    timestamps (`rollout-YYYY-MM-DDTHH-MM-SS`) onto a single UTC epoch timeline.
    Uses per-date local TZ rules when siblings match host TZ (exact across DST
    transitions), or nearest-in-time sibling offset + IANA DST-fold awareness for
    logs from another timezone, and flags `ambiguous_timestamp_order` independently
    for each missing-timestamp segment whenever alternate plausible DST offsets
    could flip relative segment order — even if another segment in the group has
    no timestamp.
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

    # Identify any DST offset transition spans observed across all known siblings.
    sibs_by_time = sorted(known_siblings, key=lambda s: s[1])
    transition_windows: list[tuple[float, float, float, float]] = []
    for k in range(len(sibs_by_time) - 1):
        s_a, s_b = sibs_by_time[k], sibs_by_time[k + 1]
        if abs(s_a[2] - s_b[2]) > 120.0:
            transition_windows.append((s_a[1], s_b[1], s_a[2], s_b[2]))

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
                cand_offs = {chosen_off}
                # Check if wall clock itself sits inside a real IANA DST fall-back fold
                # for a timezone matching `chosen_off` (so NY fold logs warn even on Shanghai host).
                for fold_alt in _foreign_dst_fold_offsets(f_ts, chosen_off):
                    cand_offs.add(fold_alt)
                # Check transition spans across ALL siblings (not just immediate prev/next).
                for t_a, t_b, off_a, off_b in transition_windows:
                    if (t_a - 7200.0) <= f_nv <= (t_b + 7200.0):
                        d_a = abs(f_nv - t_a)
                        d_b = abs(t_b - f_nv)
                        if not (min(d_a, d_b) <= 3600.0 and max(d_a, d_b) > 43200.0):
                            cand_offs.add(off_a)
                            cand_offs.add(off_b)
                if nearest_dist > 86400.0:
                    cand_offs.add(chosen_off - 3600.0)
                    cand_offs.add(chosen_off + 3600.0)
                normalized_epochs.append(f_nv - chosen_off)
                alt_epochs.append([f_nv - off for off in sorted(cand_offs)])
        else:
            normalized_epochs.append(None)
            alt_epochs.append([])

    warnings: list[str] = []
    # Evaluate per-entry DST ambiguity UNCONDITIONALLY using full pairwise interval
    # overlap (`alts_i` vs `alts_j`), so two missing-UTC segments across a fall-back
    # fold are flagged identically regardless of host TZ or single-point heuristic.
    for i, alts_i in enumerate(alt_epochs):
        if len(alts_i) <= 1:
            continue
        lo_i, hi_i = min(alts_i), max(alts_i)
        flips = False
        for j, alts_j in enumerate(alt_epochs):
            if j == i or not alts_j:
                continue
            lo_j, hi_j = min(alts_j), max(alts_j)
            if lo_i <= hi_j and hi_i >= lo_j:
                flips = True
                break
        if flips:
            warnings.append(
                f"ambiguous_timestamp_order: segment {entries[i][1]} lacks session_meta.timestamp "
                "across a DST or timezone boundary"
            )

    if all(ep is not None for ep in normalized_epochs):
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
    verify_cross_dir_collisions: bool = True,
    collision_sids: dict[Path, str] | None = None,
) -> list[Path]:
    """Deduplicate physical files (symlinks + plain/compressed/archived sibling pairs).

    Prefers real plain `.jsonl` over `.jsonl.zst` (including `.jsonl` symlinks to
    `.jsonl.zst`), and prefers `sessions/` over `archived_sessions/`.

    When `verify_cross_dir_collisions=False`, defers cross-directory canonical merging
    so cold plain/archive mirrors never require header reads before time pruning.
    When `verify_cross_dir_collisions=True` and two different directories share a
    canonical filename with different header SIDs, both are kept and their verified
    SIDs are recorded in `collision_sids`.
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

    if not verify_cross_dir_collisions:
        # Defer both same-dir plain/zst and cross-dir stem merging until after time
        # pruning so cold mirrors never trigger header reads or lose distinct SIDs.
        return list(by_inode.values())

    for path in by_inode.values():
        ckey: Any = rollout_canonical_key(path, codex_home=codex_home)
        prev = by_canonical.get(ckey)
        if prev is None:
            by_canonical[ckey] = path
            continue
        m_prev = read_rollout_meta(prev, max_lines=1)
        m_curr = read_rollout_meta(path, max_lines=1)
        if m_prev and m_curr and m_prev["sid"] != m_curr["sid"]:
            if collision_sids is not None:
                collision_sids[prev] = m_prev["sid"]
                collision_sids[path] = m_curr["sid"]
            alt_key = (ckey, m_curr["sid"])
            alt_prev = by_canonical.get(alt_key)
            if alt_prev is None or rollout_dedup_priority(path) < rollout_dedup_priority(alt_prev):
                by_canonical[alt_key] = path
            continue
        if (m_prev is None) != (m_curr is None):
            # Never drop a header-verified file in favor of an unverifiable/corrupt twin.
            if m_curr is not None:
                if collision_sids is not None:
                    collision_sids[path] = m_curr["sid"]
                by_canonical[(ckey, m_curr["sid"])] = path
            else:
                if collision_sids is not None and m_prev is not None:
                    collision_sids[prev] = m_prev["sid"]
                by_canonical[(ckey, "__unverified__")] = path
            continue
        if rollout_dedup_priority(path) < rollout_dedup_priority(prev):
            by_canonical[ckey] = path

    result = list(by_canonical.values())
    if collision_sids is not None:
        # Codex creates at most one single-UUID canonical file per session SID.
        # If multiple canonical single-UUID files share the same filename SID across
        # different timestamps, verify their headers to expose misnamed SID drift.
        single_by_fn_sid: dict[str, list[Path]] = {}
        for p in result:
            if is_canonical_single_uuid_rollout(p):
                fn_sid, _ = extract_filename_ids(p)
                if fn_sid:
                    single_by_fn_sid.setdefault(fn_sid, []).append(p)
        for group in single_by_fn_sid.values():
            if len(group) > 1:
                for p in group:
                    if p not in collision_sids:
                        m = read_rollout_meta(p, max_lines=1)
                        if m:
                            collision_sids[p] = m["sid"]
    return result


def iter_codex_rollout_files(
    codex_home: Path,
    *,
    verify_cross_dir_collisions: bool = True,
    collision_sids: dict[Path, str] | None = None,
) -> list[Path]:
    """Return deduplicated `.jsonl` and `.jsonl.zst` rollouts under `codex_home`."""
    raw: list[Path] = []
    for root in codex_session_roots(codex_home, existing_only=True):
        for pattern in ("*.jsonl", "*.jsonl.zst"):
            try:
                for path in root.rglob(pattern):
                    raw.append(path)
            except OSError:
                continue
    return deduplicate_rollout_paths(
        raw,
        codex_home=codex_home,
        verify_cross_dir_collisions=verify_cross_dir_collisions,
        collision_sids=collision_sids,
    )


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


def resolve_committed_head_detailed(
    sid: str,
    codex_home: Path,
    *,
    verify_header: bool = True,
    rollout_index: dict[str, Path] | None = None,
    meta_cache: dict[Any, dict[str, Any] | None] | None = None,
    sqlite_only: bool = False,
) -> tuple[Path | None, bool]:
    """Return `(committed_head, sqlite_conflict)` for `sid`.

    Checks all `state_*.sqlite` databases from highest to lowest schema version.
    If a higher-version DB points to a missing or foreign-SID rollout, continues
    checking lower-version DBs for a valid committed head. If SQLite had rows for
    `sid` and NONE resolved cleanly, returns `(None, True)` so callers fail closed
    instead of falling back to an uncommitted rollback file.
    """
    if not sid:
        return None, False
    rows, _ = query_sqlite_rollout_paths_detailed(sid, codex_home)
    saw_sqlite_row = False
    for _, sqlite_path in rows:
        saw_sqlite_row = True
        resolved = resolve_alternate_rollout_path(
            sqlite_path, codex_home, rollout_index=rollout_index, expected_sid=sid
        )
        if resolved is None:
            continue
        ckey = path_identity_key(resolved)
        if meta_cache is not None and ckey in meta_cache and meta_cache[ckey] is not None:
            meta, err = meta_cache[ckey], None
        else:
            meta, err = read_rollout_meta_detailed(resolved, max_lines=1)
            if meta_cache is not None and err is None and meta is not None:
                meta_cache[ckey] = meta
        # Always require an authentic line-1 session_meta header for SQLite targets
        # so a headerless or foreign-SID upper-DB row never masks a valid lower-DB head.
        if err is None and meta is not None and meta["sid"] == sid:
            return resolved, False
    if saw_sqlite_row:
        return None, True
    if sqlite_only:
        return None, False
    transcripts = find_session_rollouts(sid, codex_home, verify_header=verify_header)
    return (transcripts[-1] if transcripts else None), False


def resolve_committed_head(
    sid: str,
    codex_home: Path,
    *,
    verify_header: bool = True,
) -> Path | None:
    """Return the authoritative committed rollout head for `sid`."""
    head, _ = resolve_committed_head_detailed(sid, codex_home, verify_header=verify_header)
    return head


def _add_rollout_index_candidate(rollout_index: dict[str, Any], rid: str, path: Path) -> None:
    if not rid:
        return
    prev = rollout_index.get(rid)
    if prev is None or (isinstance(prev, Path) and rollout_dedup_priority(path) < rollout_dedup_priority(prev)):
        rollout_index[rid] = path
    bucket_key = f"__rids__:{rid}"
    bucket = rollout_index.get(bucket_key)
    if not isinstance(bucket, list):
        bucket = []
        rollout_index[bucket_key] = bucket
    pkey = path_identity_key(path)
    if not any(path_identity_key(existing) == pkey for existing in bucket):
        bucket.append(path)
        bucket.sort(key=rollout_dedup_priority)


def _populate_rollout_index_once(
    codex_home: Path,
    rollout_index: dict[str, Any],
) -> None:
    sentinel = f"__scanned__:{codex_home}"
    if sentinel in rollout_index:
        return
    for path in iter_codex_rollout_files(codex_home):
        if is_canonical_rollout_name(path):
            _, fn_rid = extract_filename_ids(path)
            if fn_rid:
                _add_rollout_index_candidate(rollout_index, fn_rid, path)
            stem_key = f"__stem__:{rollout_plain_stem(path)}"
            prev_stem = rollout_index.get(stem_key)
            if prev_stem is None or (
                isinstance(prev_stem, Path) and rollout_dedup_priority(path) < rollout_dedup_priority(prev_stem)
            ):
                rollout_index[stem_key] = path
    rollout_index[sentinel] = codex_home


def find_rollouts_by_rollout_id(
    rollout_id: str,
    codex_home: Path | None,
    *,
    rollout_index: dict[str, Any] | None = None,
) -> list[Path]:
    """Return ALL distinct physical rollout files matching `rollout_id`, ordered by priority."""
    if not rollout_id:
        return []
    if rollout_index is not None:
        bucket = rollout_index.get(f"__rids__:{rollout_id}")
        if isinstance(bucket, list) and bucket:
            return list(bucket)
        single = rollout_index.get(rollout_id)
        if isinstance(single, Path) and (codex_home is None or f"__scanned__:{codex_home}" in rollout_index):
            return [single]
        if codex_home is not None:
            _populate_rollout_index_once(codex_home, rollout_index)
            bucket = rollout_index.get(f"__rids__:{rollout_id}")
            if isinstance(bucket, list) and bucket:
                return list(bucket)
            single = rollout_index.get(rollout_id)
            return [single] if isinstance(single, Path) else []
        return [single] if isinstance(single, Path) else []

    if codex_home is None:
        return []
    raw_candidates: list[Path] = []
    for root in codex_session_roots(codex_home, existing_only=True):
        for suffix in CODEX_ROLLOUT_SUFFIXES:
            for pattern in (f"*-{rollout_id}{suffix}", f"*_{rollout_id}{suffix}"):
                try:
                    raw_candidates.extend(root.rglob(pattern))
                except OSError:
                    continue
    deduped = deduplicate_rollout_paths(raw_candidates, codex_home=codex_home)
    matched: list[Path] = []
    for path in deduped:
        if not is_canonical_rollout_name(path):
            continue
        _, fn_rid = extract_filename_ids(path)
        if fn_rid == rollout_id:
            matched.append(path)
    matched.sort(key=rollout_dedup_priority)
    return matched


def find_rollout_by_rollout_id(
    rollout_id: str,
    codex_home: Path,
    *,
    rollout_index: dict[str, Any] | None = None,
) -> Path | None:
    """Locate a physical rollout file by its physical `rollout_id` without repeated scans."""
    cands = find_rollouts_by_rollout_id(rollout_id, codex_home, rollout_index=rollout_index)
    return cands[0] if cands else None


def resolve_alternate_rollout_path(
    path: Path,
    codex_home: Path | None = None,
    *,
    rollout_index: dict[str, Any] | None = None,
    expected_sid: str | None = None,
) -> Path | None:
    """Resolve the authoritative on-disk representation for a rollout path.

    - For non-canonical / opaque names (e.g. `session.jsonl`), ONLY resolves
      plain `.jsonl` <-> compressed `.jsonl.zst` counterparts in the EXACT same
      directory so another directory's unrelated `session.jsonl` can never
      silently replace a missing or compressed committed anchor.
    - For canonical `rollout-YYYY-MM-DDTHH-MM-SS-<uuid>[_<uuid>]` names, prefers
      an existing plain `.jsonl` over `.jsonl.zst` (matching upstream
      `compression.rs:924`) ONLY when the plain file verifies with the same SID,
      and searches `sessions/` <-> `archived_sessions/` by physical `rollout_id`.
    """
    home = codex_home
    if home is None:
        inferred = infer_codex_home(path)
        home = Path(inferred) if inferred else None

    stem = rollout_plain_stem(path)
    canonical = is_canonical_rollout_name(path)
    _, fn_rid = extract_filename_ids(path) if canonical else (None, None)
    path_exists = path.is_file()
    path_is_comp = is_compressed_rollout(path) if path_exists else path.name.lower().endswith((".zst", ".zstd"))

    # 1. If path is compressed (or points to compressed), check plain `.jsonl` counterparts FIRST.
    if path_is_comp:
        target_sid = expected_sid
        if not target_sid and path_exists:
            orig_meta = read_rollout_meta(path, max_lines=1)
            target_sid = orig_meta["sid"] if orig_meta else None

        def _plain_matches_existing(cand: Path) -> bool:
            if not cand.is_file() or is_compressed_rollout(cand):
                return False
            if not path_exists and not target_sid:
                return True
            m_cand = read_rollout_meta(cand, max_lines=1)
            if target_sid:
                # Never replace a verified compressed rollout (or expected SID) with an
                # unverifiable/headerless or foreign-SID plain file.
                return bool(m_cand and m_cand["sid"] == target_sid)
            return True

        plain_sibling = path.with_name(stem + ".jsonl")
        if _plain_matches_existing(plain_sibling):
            return plain_sibling
        if canonical and home is not None:
            if rollout_index is not None:
                stem_cand = rollout_index.get(f"__stem__:{stem}")
                if isinstance(stem_cand, Path) and _plain_matches_existing(stem_cand):
                    return stem_cand
                if fn_rid:
                    for rid_cand in find_rollouts_by_rollout_id(fn_rid, home, rollout_index=rollout_index):
                        if _plain_matches_existing(rid_cand):
                            return rid_cand
            elif fn_rid:
                for found in find_rollouts_by_rollout_id(fn_rid, home):
                    if _plain_matches_existing(found):
                        return found

    if path_exists:
        return path

    # 2. Path missing on disk: check compressed sibling in the EXACT same directory.
    if not path_is_comp and path.name.lower().endswith(".jsonl"):
        comp = path.with_name(path.name + ".zst")
        if comp.is_file():
            return comp

    # 3. Cross-directory lookup ONLY for canonical filenames carrying a physical rollout_id.
    if canonical and home is not None:
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

    local_rindex: dict[str, Any] = rollout_index if rollout_index is not None else {}
    for m, p in ordered_entries:
        rid = (m or {}).get("rollout_id") or extract_filename_ids(p)[1]
        if isinstance(rid, str) and rid:
            _add_rollout_index_candidate(local_rindex, rid, p)

    def _match_anchor(anchor: Path | None) -> tuple[dict[str, Any] | None, Path] | None:
        if anchor is None:
            return None
        resolved_anchor = resolve_alternate_rollout_path(
            anchor, codex_home, rollout_index=local_rindex, expected_sid=session_id
        )
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
        def _is_verified_ancestor_of(
            target_sid: str,
            target_rid: str,
            target_eo: int | None,
            target_eb: int | None,
            logical_parent_sid: str,
            visited_parents: set[str] | None = None,
            depth: int = 0,
        ) -> bool:
            if not logical_parent_sid or depth > 16:
                return False
            seen_p = set(visited_parents or ())
            if logical_parent_sid in seen_p:
                return False
            seen_p.add(logical_parent_sid)

            p_head: Path | None = None
            if codex_home is not None:
                p_head, _ = resolve_committed_head_detailed(
                    logical_parent_sid,
                    codex_home,
                    verify_header=True,
                    rollout_index=local_rindex,
                    meta_cache=cache,
                    sqlite_only=True,
                )
            if p_head is None:
                for cand in find_rollouts_by_rollout_id(
                    logical_parent_sid, codex_home, rollout_index=local_rindex
                ):
                    cm = _meta(cand)
                    if cm is not None and cm["sid"] == logical_parent_sid:
                        p_head = cand
                        break
            if p_head is None and codex_home is not None:
                rlist = find_session_rollouts(logical_parent_sid, codex_home, verify_header=True)
                if rlist:
                    p_head = rlist[-1]
            if p_head is None:
                return False

            p_meta = _meta(p_head)
            if p_meta is None or p_meta["sid"] != logical_parent_sid:
                return False

            walk_seen: set[tuple[Path, int, int]] = set()
            w_path: Path | None = p_head
            w_meta: dict[str, Any] | None = p_meta
            max_eo: int | None = None
            max_eb: int | None = None
            for _ in range(16):
                if w_path is None or w_meta is None:
                    break
                wkey = path_identity_key(w_path)
                if wkey in walk_seen:
                    break
                walk_seen.add(wkey)
                w_hb = w_meta.get("history_base")
                if not isinstance(w_hb, dict):
                    break
                w_rid = str(w_hb.get("rollout_id") or "")
                hop_eo = w_hb.get("end_ordinal_exclusive")
                hop_eb = w_hb.get("end_byte_offset")
                eff_eo = hop_eo if max_eo is None else (min(max_eo, hop_eo) if hop_eo is not None else max_eo)
                eff_eb = hop_eb if max_eb is None else (min(max_eb, hop_eb) if hop_eb is not None else max_eb)
                w_cands = find_rollouts_by_rollout_id(w_rid, codex_home, rollout_index=local_rindex)
                next_p: Path | None = None
                next_m: dict[str, Any] | None = None
                w_parent = w_meta.get("parent_id")
                for wc in w_cands:
                    wc_res = resolve_alternate_rollout_path(wc, codex_home, rollout_index=local_rindex) or wc
                    wm = _meta(wc_res)
                    if wm is None:
                        continue
                    if wm["sid"] == target_sid and (w_rid == target_rid or wm.get("rollout_id") == target_rid):
                        eo_ok = target_eo is None or eff_eo is None or target_eo <= eff_eo
                        eb_ok = target_eb is None or eff_eb is None or target_eb <= eff_eb
                        if eo_ok and eb_ok:
                            return True
                    if next_p is None and wm["sid"] in {w_meta["sid"], w_parent}:
                        next_p, next_m = wc_res, wm
                if next_p is None and len(w_cands) == 1:
                    wc_res = resolve_alternate_rollout_path(w_cands[0], codex_home, rollout_index=local_rindex) or w_cands[0]
                    next_p, next_m = wc_res, _meta(wc_res)
                if next_m is not None and next_m["sid"] == target_sid:
                    eo_ok = target_eo is None or eff_eo is None or target_eo <= eff_eo
                    eb_ok = target_eb is None or eff_eb is None or target_eb <= eff_eb
                    if eo_ok and eb_ok:
                        return True
                w_path, w_meta = next_p, next_m
                max_eo, max_eb = eff_eo, eff_eb

            next_parent = p_meta.get("parent_id")
            if isinstance(next_parent, str) and next_parent and next_parent != logical_parent_sid:
                return _is_verified_ancestor_of(
                    target_sid, target_rid, target_eo, target_eb, next_parent, seen_p, depth + 1
                )
            return False

        chain: list[dict[str, Any]] = []
        seen_rids: set[str] = set()
        seen_keys: set[tuple[Path, int, int]] = set()
        cur_meta: dict[str, Any] | None = head_meta
        cur_path: Path | None = head_path
        cur_end_ord: int | None = None
        cur_end_byte: int | None = None
        cur_explicit_hb = False

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
                    "explicit_history_base": cur_explicit_hb,
                    "segment_sid": seg_sid,
                }
            )
            hb = cur_meta.get("history_base") if cur_meta else None
            if not hb:
                if (
                    cur_meta is not None
                    and cur_meta.get("history_mode") == "paginated"
                    and isinstance(cur_meta.get("ordinal"), int)
                    and cur_meta["ordinal"] > 0
                ):
                    warnings.append(
                        f"paginated_root_ordinal_nonzero: paginated root segment {cur_path} "
                        f"starts at ordinal {cur_meta['ordinal']} without history_base"
                    )
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
            hb_end_ord = hb.get("end_ordinal_exclusive")
            if (
                cur_meta is not None
                and isinstance(cur_meta.get("ordinal"), int)
                and isinstance(hb_end_ord, int)
                and cur_meta["ordinal"] != hb_end_ord
            ):
                warnings.append(
                    f"history_base_start_ordinal_mismatch: segment {cur_path} starts at ordinal "
                    f"{cur_meta['ordinal']} while history_base.end_ordinal_exclusive is {hb_end_ord}"
                )

            base_rid = hb["rollout_id"]
            raw_cands = find_rollouts_by_rollout_id(base_rid, codex_home, rollout_index=local_rindex)
            for em, ep in ordered_entries:
                if (em or {}).get("rollout_id") == base_rid:
                    raw_cands.append(ep)
            resolved_cands: list[tuple[Path, dict[str, Any] | None]] = []
            seen_cand_keys: set[tuple[Path, int, int]] = set()
            for cp in raw_cands:
                rp = resolve_alternate_rollout_path(cp, codex_home, rollout_index=local_rindex) or cp
                if not rp.is_file():
                    continue
                ck = path_identity_key(rp)
                if ck in seen_cand_keys:
                    continue
                seen_cand_keys.add(ck)
                resolved_cands.append((rp, _meta(rp)))

            if not resolved_cands:
                warnings.append(
                    f"missing_history_base_rollout: segment {cur_path} references missing base rollout {base_rid}"
                )
                break

            declared_parent = cur_meta.get("parent_id") if cur_meta else None
            declared_parent_str = (
                declared_parent if (isinstance(declared_parent, str) and declared_parent) else ""
            )
            same_sid_cands = [(p, m) for p, m in resolved_cands if m is not None and m["sid"] == seg_sid]
            direct_parent_cands = (
                [(p, m) for p, m in resolved_cands if m is not None and m["sid"] == declared_parent_str]
                if declared_parent_str
                else []
            )
            ancestor_cands: list[tuple[Path, dict[str, Any] | None]] = []
            if declared_parent_str and not same_sid_cands and not direct_parent_cands:
                for p, m in resolved_cands:
                    if m is not None and _is_verified_ancestor_of(
                        m["sid"],
                        base_rid,
                        hb_end_ord,
                        hb.get("end_byte_offset"),
                        declared_parent_str,
                    ):
                        ancestor_cands.append((p, m))

            chosen: tuple[Path, dict[str, Any] | None] | None = None
            if len(same_sid_cands) == 1:
                chosen = same_sid_cands[0]
            elif len(same_sid_cands) > 1:
                warnings.append(
                    f"ambiguous_history_base_rollout: multiple rollouts match base_rid={base_rid} for session {seg_sid}"
                )
                break
            elif len(direct_parent_cands) == 1:
                chosen = direct_parent_cands[0]
            elif len(direct_parent_cands) > 1:
                warnings.append(
                    f"ambiguous_history_base_rollout: multiple rollouts match base_rid={base_rid} for parent {declared_parent_str}"
                )
                break
            elif len(ancestor_cands) == 1:
                chosen = ancestor_cands[0]
            elif len(ancestor_cands) > 1:
                warnings.append(
                    f"ambiguous_history_base_rollout: multiple ancestor rollouts match base_rid={base_rid} for parent {declared_parent_str}"
                )
                break
            elif declared_parent_str:
                foreign_hit = next((item for item in resolved_cands if item[1] is not None), None)
                if foreign_hit is not None:
                    warnings.append(
                        f"history_base_parent_sid_mismatch: segment {cur_path} declares parent {declared_parent_str} "
                        f"but base rollout {foreign_hit[0]} declares session_id={foreign_hit[1]['sid']}"  # type: ignore[index]
                    )
                    break
                chosen = resolved_cands[0]
            else:
                if len(resolved_cands) == 1:
                    chosen = resolved_cands[0]
                else:
                    warnings.append(
                        f"ambiguous_history_base_rollout: multiple rollouts match base_rid={base_rid} "
                        f"and none matches session {seg_sid}"
                    )
                    break

            base_path, base_meta = chosen
            cur_path = base_path
            cur_meta = base_meta
            cur_end_ord = hb_end_ord
            cur_end_byte = hb.get("end_byte_offset")
            cur_explicit_hb = True

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


def query_sqlite_rollout_paths_detailed(
    sid: str,
    codex_home: Path,
) -> tuple[list[tuple[Path, Path]], list[str]]:
    """Read-only lookup of all recorded `(db_path, rollout_path)` for `sid` across all state DBs."""
    if not sid:
        return [], []
    dbs = find_state_dbs(codex_home)
    if not dbs:
        return [], []
    results: list[tuple[Path, Path]] = []
    errors: list[str] = []
    for db_path in dbs:
        try:
            uri = f"{db_path.resolve().as_uri()}?mode=ro"
            with closing(sqlite3.connect(uri, uri=True, timeout=0.15)) as conn:
                row = conn.execute(
                    "SELECT rollout_path FROM threads WHERE id = ? LIMIT 1",
                    (sid,),
                ).fetchone()
                if row and isinstance(row[0], str) and row[0].strip():
                    results.append((db_path, Path(row[0].strip()).expanduser()))
        except (OSError, sqlite3.Error, ValueError) as exc:
            errors.append(f"{db_path}: {exc}")
            continue
    return results, errors


def query_sqlite_rollout_paths_batch_all(
    sids: Iterable[str],
    codex_home: Path,
) -> dict[str, list[Path]]:
    """Batch read-only lookup `{sid: [rollout_paths highest-to-lowest]}` opening each DB once."""
    unique_sids = list(dict.fromkeys(s for s in sids if isinstance(s, str) and s))
    if not unique_sids:
        return {}
    dbs = find_state_dbs(codex_home)
    if not dbs:
        return {}
    found: dict[str, list[Path]] = {}
    for db_path in dbs:
        try:
            uri = f"{db_path.resolve().as_uri()}?mode=ro"
            with closing(sqlite3.connect(uri, uri=True, timeout=0.25)) as conn:
                for start in range(0, len(unique_sids), 400):
                    chunk = unique_sids[start : start + 400]
                    placeholders = ",".join("?" for _ in chunk)
                    rows = conn.execute(
                        f"SELECT id, rollout_path FROM threads WHERE id IN ({placeholders}) "
                        "AND rollout_path IS NOT NULL AND rollout_path != ''",
                        chunk,
                    ).fetchall()
                    for tid, rpath in rows:
                        if isinstance(tid, str) and tid and isinstance(rpath, str) and rpath.strip():
                            p = Path(rpath.strip()).expanduser()
                            bucket = found.setdefault(tid, [])
                            if p not in bucket:
                                bucket.append(p)
        except (OSError, sqlite3.Error, ValueError):
            continue
    return found


def query_sqlite_rollout_paths_batch(
    sids: Iterable[str],
    codex_home: Path,
) -> dict[str, Path]:
    """Batch read-only lookup `{sid: highest_db_rollout_path}` opening each DB once."""
    return {
        tid: paths[0]
        for tid, paths in query_sqlite_rollout_paths_batch_all(sids, codex_home).items()
        if paths
    }


def query_sqlite_rollout_path_detailed(
    sid: str,
    codex_home: Path,
) -> tuple[Path | None, str | None]:
    """Read-only lookup of Codex's recorded rollout_path for `sid`.

    Returns `(path, error_message)` so callers can distinguish absent DB/row
    from schema/read errors.
    """
    rows, errors = query_sqlite_rollout_paths_detailed(sid, codex_home)
    if rows:
        return rows[0][1], None
    return None, (errors[-1] if errors else None)


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
    """Find a durable rollout via standard roots or verified `state_*.sqlite` across all DB versions."""
    if not sid:
        return False
    for root in codex_session_roots(codex_home, existing_only=True):
        for suffix in CODEX_ROLLOUT_SUFFIXES:
            for pattern in (f"*-{sid}{suffix}", f"*-{sid}_*{suffix}"):
                for path in root.rglob(pattern):
                    if rollout_matches_session(path, sid):
                        return True
    rows, _ = query_sqlite_rollout_paths_detailed(sid, codex_home)
    for _, sqlite_path in rows:
        resolved = resolve_alternate_rollout_path(sqlite_path, codex_home, expected_sid=sid)
        if resolved is not None and rollout_matches_session(resolved, sid):
            return True
    return False


def session_is_known(
    sid: str,
    codex_home: Path,
    *,
    conflict_sid: str = "",
) -> bool:
    """Return True if `sid` exists durably OR has a recorded SQLite/rollout entry.

    Used by recovery so a corrupt, missing, or misnamed rollout for a bound
    session `B` marks `B` invalid instead of silently falling back to a stale
    argv `A`. When `conflict_sid` (`A`) is supplied, also verifies the headers
    of candidate files carrying `conflict_sid` in their filename.
    """
    if not sid:
        return False
    if session_exists(sid, codex_home):
        return True
    rows, _ = query_sqlite_rollout_paths_detailed(sid, codex_home)
    if rows:
        return True
    for root in codex_session_roots(codex_home, existing_only=True):
        for suffix in CODEX_ROLLOUT_SUFFIXES:
            for pattern in (f"*-{sid}{suffix}", f"*-{sid}_*{suffix}"):
                for path in root.rglob(pattern):
                    if path.is_file():
                        return True
            if conflict_sid and conflict_sid != sid:
                for pattern in (f"*-{conflict_sid}{suffix}", f"*-{conflict_sid}_*{suffix}"):
                    for path in root.rglob(pattern):
                        if path.is_file() and rollout_matches_session(path, sid):
                            return True
    collision_sids: dict[Path, str] = {}
    nonstandard_or_double: list[Path] = []
    for path in iter_codex_rollout_files(codex_home, collision_sids=collision_sids):
        if collision_sids.get(path) == sid:
            return True
        if not is_canonical_single_uuid_rollout(path):
            nonstandard_or_double.append(path)
    for path in nonstandard_or_double:
        if rollout_matches_session(path, sid):
            return True
    return False


_LIBC_DARWIN: Any = None


def _read_process_exec_and_env(pid: int) -> tuple[str, dict[str, str]] | None:
    """Read `(exec_path, envp)` for `pid` from kernel memory (strictly separate from argv).

    On macOS uses `sysctl(CTL_KERN, KERN_PROCARGS2, pid)` which carries exact `argc`
    and NUL-terminated boundaries between `exec_path`, `argv[0..argc-1]`, and `envp`,
    so prompt text like `CODEX_HOME=/foreign` in `argv` can never spoof process env
    and paths containing spaces (e.g. `Profile Work`) are preserved verbatim.
    """
    global _LIBC_DARWIN
    if pid <= 0:
        return None
    if sys.platform == "darwin":
        try:
            if _LIBC_DARWIN is None:
                _LIBC_DARWIN = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
            mib = (ctypes.c_int * 3)(1, 49, pid)  # CTL_KERN=1, KERN_PROCARGS2=49
            size = ctypes.c_size_t(0)
            if _LIBC_DARWIN.sysctl(mib, 3, None, ctypes.byref(size), None, 0) != 0 or size.value < 4:
                return None
            buf = ctypes.create_string_buffer(size.value)
            if _LIBC_DARWIN.sysctl(mib, 3, buf, ctypes.byref(size), None, 0) != 0:
                return None
            raw = buf.raw[: size.value]
        except OSError:
            return None
        if len(raw) < 4:
            return None
        argc = struct.unpack_from("i", raw, 0)[0]
        if argc < 0 or argc > 100_000:
            return None
        pos = 4
        n = len(raw)
        nul = raw.find(b"\x00", pos)
        if nul < 0:
            return None
        exec_path = raw[pos:nul].decode("utf-8", errors="replace")
        pos = nul
        while pos < n and raw[pos] == 0:
            pos += 1
        for _ in range(argc):
            if pos >= n:
                return None
            nul = raw.find(b"\x00", pos)
            if nul < 0:
                return None
            pos = nul + 1
        env: dict[str, str] = {}
        while pos < n:
            if raw[pos] == 0:
                pos += 1
                continue
            nul = raw.find(b"\x00", pos)
            if nul < 0:
                entry = raw[pos:]
                pos = n
            else:
                entry = raw[pos:nul]
                pos = nul + 1
            if b"=" not in entry:
                break
            k, v = entry.split(b"=", 1)
            env[k.decode("utf-8", errors="replace")] = v.decode("utf-8", errors="replace")
        return exec_path, env

    # Linux fallback via /proc/<pid>/environ (NUL-separated, never includes argv)
    try:
        raw_env = Path(f"/proc/{pid}/environ").read_bytes()
    except OSError:
        return None
    try:
        exec_path = os.readlink(f"/proc/{pid}/exe")
    except OSError:
        exec_path = ""
    env = {}
    for entry in raw_env.split(b"\x00"):
        if b"=" in entry:
            k, v = entry.split(b"=", 1)
            env[k.decode("utf-8", errors="replace")] = v.decode("utf-8", errors="replace")
    return exec_path, env


def read_process_env(pid: int) -> dict[str, str] | None:
    info = _read_process_exec_and_env(pid)
    return info[1] if info is not None else None


def process_is_codex(pid: int) -> bool:
    info = _read_process_exec_and_env(pid)
    if info is None:
        return False
    name = Path(info[0]).name.lower()
    return name in {"codex", "codexx"} or name.startswith("codex-")


def process_env_var(pid: int, var_name: str) -> str:
    if pid <= 0 or not var_name:
        return ""
    env = read_process_env(pid)
    if not env:
        return ""
    return env.get(var_name, "").strip()


def process_cwd(pid: int) -> str:
    """Return the target process's current working directory without spawning subprocesses."""
    global _LIBC_DARWIN
    if pid <= 0:
        return ""
    if sys.platform == "darwin":
        try:
            if _LIBC_DARWIN is None:
                _LIBC_DARWIN = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
            # struct proc_vnodepathinfo (2352 bytes): pvi_cdir.vip_path at offset 152 (1024 bytes)
            buf = ctypes.create_string_buffer(2352)
            ret = _LIBC_DARWIN.proc_pidinfo(pid, 9, 0, buf, 2352)
            if ret >= 152 + 1:
                raw_path = buf.raw[152 : 152 + 1024].split(b"\x00", 1)[0]
                if raw_path:
                    decoded = raw_path.decode("utf-8", errors="replace")
                    if os.path.isabs(decoded):
                        return decoded
        except OSError:
            return ""
        return ""
    try:
        resolved = os.readlink(f"/proc/{pid}/cwd")
        return resolved if os.path.isabs(resolved) else ""
    except OSError:
        return ""


def _resolve_process_path(raw: str, pid: int, *, cwd: str | Path | None = None, env_pwd: str = "") -> str:
    p = Path(raw).expanduser()
    if p.is_absolute():
        try:
            return str(p.resolve())
        except OSError:
            return str(p)
    # Relative paths MUST be resolved against the target process's cwd, never the reader's cwd.
    for cand_base in (process_cwd(pid), str(cwd) if cwd else "", env_pwd):
        if cand_base and os.path.isabs(cand_base):
            try:
                return str((Path(cand_base) / p).resolve())
            except OSError:
                return str(Path(cand_base) / p)
    return ""


def process_codex_home(pid: int, *, cwd: str | Path | None = None) -> str:
    """Resolve verified CODEX_HOME for `pid` from kernel envp (never argv)."""
    env = read_process_env(pid)
    if not env:
        return ""
    env_pwd = env.get("PWD", "").strip()
    chome = env.get("CODEX_HOME", "").strip()
    if chome:
        return _resolve_process_path(chome, pid, cwd=cwd, env_pwd=env_pwd)
    home_val = env.get("HOME", "").strip()
    if home_val:
        resolved_home = _resolve_process_path(home_val, pid, cwd=cwd, env_pwd=env_pwd)
        if resolved_home:
            try:
                return str((Path(resolved_home) / ".codex").resolve())
            except OSError:
                return str(Path(resolved_home) / ".codex")
    return ""


def _process_env_codex_home(pid: int, *, cwd: str | Path | None = None) -> str:
    env = read_process_env(pid)
    if not env:
        return ""
    val = env.get("CODEX_HOME", "").strip()
    if not val:
        return ""
    return _resolve_process_path(val, pid, cwd=cwd, env_pwd=env.get("PWD", "").strip())


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
    parser.add_argument("--conflict-sid", default="", help="Optional stale argv SID whose candidate files should also be header-checked")
    parser.add_argument("sid")
    parser.add_argument("codex_home", type=Path)
    arguments = parser.parse_args()
    if arguments.known:
        ok = session_is_known(
            arguments.sid,
            arguments.codex_home.expanduser(),
            conflict_sid=arguments.conflict_sid,
        )
    else:
        ok = session_exists(arguments.sid, arguments.codex_home.expanduser())
    raise SystemExit(0 if ok else 1)
