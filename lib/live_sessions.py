"""Fast, read-only live tmux pane to authoritative AI session resolution.

Shared narrow waist consumed by aipane-sessions (panes, read --pane, yank,
preview) and bin/tmux-session-picker. Avoids spawning macOS `ps` on hot paths
by combining a single `tmux list-panes` query with batched `lsof` and native
per-agent runtime files.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import stat
import subprocess
import time
import urllib.parse
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

import codex_sessions
from agent_activity_contract import ACTIVITY_RECORD_OPTION

FIELD_SEP = "\x1f"
UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
CLAUDE_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$")
GENERIC_TITLES = frozenset(
    {"", "agy", "antigravity", "codex", "codexx", "claude", "kimi", "dsh", "dsh-tui", "grok", "pi", "node"}
)

CANONICAL_AGENT = {
    "a": "antigravity",
    "agy": "antigravity",
    "antigravity": "antigravity",
    "c": "claude",
    "claude": "claude",
    "x": "codex",
    "codex": "codex",
    "d": "dsh",
    "dsh": "dsh",
    "g": "grok",
    "grok": "grok",
    "k": "kimi",
    "kimi": "kimi",
    "p": "pi",
    "pi": "pi",
}

DISPLAY_AGENT = {
    "antigravity": "agy",
    "claude": "claude",
    "codex": "codex",
    "dsh": "dsh",
    "grok": "grok",
    "kimi": "kimi",
    "pi": "pi",
}


@dataclass(frozen=True)
class RawPane:
    pane_id: str
    session_name: str
    window_id: str
    window_index: str
    window_name: str
    pane_index: str
    pane_active: bool
    window_active: bool
    current_command: str
    cwd: str
    pane_tty: str
    pane_title: str
    socket_path: str
    server_pid: str
    pane_pid: str
    activity_record: str
    pane_in_mode: bool = False

    @property
    def coord(self) -> str:
        return f"{self.session_name}:{self.window_index}.{self.pane_index}"

    @property
    def claude_target(self) -> str:
        return f"{self.session_name}:{self.window_id}.{self.pane_id}"

    @property
    def short_tty(self) -> str:
        return self.pane_tty.removeprefix("/dev/")


@dataclass(frozen=True)
class LivePaneSession:
    pane_id: str
    coord: str
    session_name: str
    window_id: str
    window_index: str
    window_name: str
    pane_index: str
    pane_active: bool
    window_active: bool
    current_command: str
    cwd: str
    pane_tty: str
    pane_title: str
    agent: str
    agent_alias: str
    session_id: str
    title: str
    authority: str
    is_self: bool = False
    pane_in_mode: bool = False
    active_path: str = ""
    storage_root: str = ""

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "pane_id": self.pane_id,
            "coord": self.coord,
            "session_name": self.session_name,
            "window_id": self.window_id,
            "window_index": self.window_index,
            "window_name": self.window_name,
            "pane_index": self.pane_index,
            "pane_active": self.pane_active,
            "window_active": self.window_active,
            "is_self": self.is_self,
            "current_command": self.current_command,
            "agent": self.agent,
            "agent_alias": self.agent_alias,
            "session_id": self.session_id,
            "ref": f"{self.agent_alias}:{self.session_id}" if (self.agent_alias and self.session_id) else "",
            "title": self.title,
            "cwd": self.cwd,
            "authority": self.authority,
        }
        if self.active_path:
            out["active_path"] = self.active_path
        if self.storage_root:
            out["storage_root"] = self.storage_root
        return out


def _default_process_exists(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _read_safe_json(path: Path, max_bytes: int = 131072) -> dict[str, Any] | None:
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_size > max_bytes:
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else None
    except (OSError, ValueError):
        return None


def _is_codex_cmd(cmd: str) -> bool:
    low = cmd.lower()
    return low in {"codex", "codexx"} or low.startswith("codex-")


def _is_claude_cmd(cmd: str) -> bool:
    low = cmd.lower()
    return low in {"claude", "claude-guard"} or bool(CLAUDE_VERSION_RE.match(cmd))


def _is_grok_cmd(cmd: str) -> bool:
    low = cmd.lower()
    return low == "grok" or low.startswith("grok-")


def _is_kimi_cmd(cmd: str) -> bool:
    low = cmd.lower()
    return low == "kimi" or low.startswith("kimi-")


def _is_pi_cmd(cmd: str) -> bool:
    low = cmd.lower()
    return low == "pi" or low.startswith("pi-")


def _is_dsh_cmd(cmd: str) -> bool:
    low = cmd.lower()
    return low in {"dsh", "dsh-tui", "dst"}


def _clean_title(raw: str) -> str:
    text = " ".join((raw or "").split()).strip()
    low = text.lower()
    if low in GENERIC_TITLES or CLAUDE_VERSION_RE.match(text):
        return ""
    # Strip leading Claude busy/idle glyph noise only for emptiness check; keep readable text.
    return text


def _tmux_command(environment: Mapping[str, str]) -> list[str]:
    cmd = shlex.split(environment.get("AIPANE_TMUX", "tmux"))
    return cmd or ["tmux"]


def _pane_format() -> str:
    return FIELD_SEP.join(
        (
            "#{pane_id}",
            "#{session_name}",
            "#{window_id}",
            "#{window_index}",
            "#{window_name}",
            "#{pane_index}",
            "#{pane_active}",
            "#{window_active}",
            "#{pane_current_command}",
            "#{pane_current_path}",
            "#{pane_tty}",
            "#{pane_title}",
            "#{socket_path}",
            "#{pid}",
            "#{pane_pid}",
            f"#{{{ACTIVITY_RECORD_OPTION}}}",
            "#{pane_in_mode}",
        )
    )


def _parse_raw_pane(line: str) -> RawPane | None:
    fields = line.split(FIELD_SEP, 16)
    if len(fields) != 17 or not fields[0]:
        return None
    return RawPane(
        pane_id=fields[0],
        session_name=fields[1],
        window_id=fields[2],
        window_index=fields[3],
        window_name=fields[4],
        pane_index=fields[5],
        pane_active=fields[6] == "1",
        window_active=fields[7] == "1",
        current_command=fields[8],
        cwd=fields[9],
        pane_tty=fields[10],
        pane_title=fields[11],
        socket_path=fields[12],
        server_pid=fields[13],
        pane_pid=fields[14],
        activity_record=fields[15],
        pane_in_mode=fields[16] == "1",
    )


def list_raw_panes(
    *,
    target: str | None = None,
    environment: Mapping[str, str] | None = None,
    tmux_runner: Callable[[list[str]], subprocess.CompletedProcess[str]] | None = None,
) -> list[RawPane]:
    env = dict(os.environ if environment is None else environment)
    cmd = _tmux_command(env)
    if target:
        args = [*cmd, "display-message", "-p", "-t", target, _pane_format()]
    else:
        args = [*cmd, "list-panes", "-a", "-F", _pane_format()]
    try:
        if tmux_runner is not None:
            proc = tmux_runner(args)
        else:
            proc = subprocess.run(args, capture_output=True, text=True, env=env, timeout=5, check=False)
    except (OSError, subprocess.SubprocessError):
        return []
    if proc.returncode != 0:
        return []
    out: list[RawPane] = []
    for line in proc.stdout.splitlines():
        parsed = _parse_raw_pane(line)
        if parsed is not None:
            out.append(parsed)
    return out


def _state_dir(environment: Mapping[str, str], home: Path) -> Path:
    explicit = environment.get("AIPANE_STATE_DIR")
    if explicit:
        return Path(explicit).expanduser()
    xdg = environment.get("XDG_DATA_HOME")
    base = Path(xdg).expanduser() if xdg else (home / ".local/share")
    return base / "aipane"


def _load_registry_latest(
    socket_path: str,
    server_pid: str,
    environment: Mapping[str, str],
    home: Path,
) -> dict[str, dict[str, str]]:
    registry_path = Path(
        environment.get("AIPANE_REGISTRY") or (_state_dir(environment, home) / "registry.jsonl")
    ).expanduser()
    if not registry_path.is_file():
        return {}
    result: dict[str, dict[str, str]] = {}
    srv_needle = f'"{server_pid}"'.encode("utf-8")
    try:
        with registry_path.open("rb") as fh:
            for raw in fh:
                if srv_needle not in raw:
                    continue
                try:
                    rec = json.loads(raw)
                except ValueError:
                    continue
                if not isinstance(rec, dict):
                    continue
                if rec.get("sock") != socket_path or str(rec.get("srv", "")) != str(server_pid):
                    continue
                pane = rec.get("pane")
                sid = rec.get("sid")
                tool = rec.get("tool")
                if isinstance(pane, str) and pane and isinstance(sid, str) and sid:
                    entry_rec = {
                        "sid": sid,
                        "tool": str(tool or ""),
                        "cwd": str(rec.get("cwd", "")),
                    }
                    ch = rec.get("codex_home")
                    if isinstance(ch, str) and ch:
                        entry_rec["codex_home"] = ch
                    result[pane] = entry_rec
    except OSError:
        return {}
    return result


def _parse_activity_record(raw: str) -> dict[str, Any] | None:
    if not raw:
        return None
    try:
        obj = json.loads(raw)
        return obj if isinstance(obj, dict) else None
    except ValueError:
        return None


def _run_lsof_batch(
    pids: list[int],
    *,
    lsof_runner: Callable[[list[str]], str] | None = None,
) -> dict[int, dict[str, Any]]:
    """Run one batched `lsof` over PIDs, returning {pid: {'ttys': set, 'writable_jsonl': list, 'paths': list}}."""
    if not pids:
        return {}
    pid_arg = ",".join(str(p) for p in sorted(set(pids)))
    args = ["lsof", "-nP", "-a", "-p", pid_arg, "-F", "pfan"]
    try:
        if lsof_runner is not None:
            stdout = lsof_runner(args)
        else:
            res = subprocess.run(args, capture_output=True, text=True, timeout=3, check=False)
            if not res.stdout or res.returncode not in (0, 1):
                return {}
            stdout = res.stdout
    except (OSError, subprocess.SubprocessError):
        return {}

    info: dict[int, dict[str, Any]] = {}
    cur_pid = 0
    writable = False
    for line in stdout.splitlines():
        if line.startswith("p"):
            cur_pid = int(line[1:]) if line[1:].isdigit() else 0
            writable = False
            if cur_pid:
                info.setdefault(cur_pid, {"ttys": set(), "writable_jsonl": [], "paths": []})
        elif line.startswith("f"):
            writable = False
        elif line.startswith("a"):
            writable = line[1:] in {"w", "u"}
        elif line.startswith("n") and cur_pid:
            name = line[1:]
            entry = info[cur_pid]
            if name.startswith("/dev/ttys") or name.startswith("/dev/pts/"):
                entry["ttys"].add(name.removeprefix("/dev/"))
            elif writable and name.endswith(".jsonl"):
                entry["writable_jsonl"].append(name)
            if "pi-coding-agent" in name or "/.pi/" in name or "/pi/" in name:
                entry["paths"].append(name)
    return info


def _find_pids_by_name(
    names: tuple[str, ...],
    *,
    pgrep_runner: Callable[[list[str]], list[int]] | None = None,
) -> list[int]:
    pids: list[int] = []
    for name in names:
        args = ["pgrep", "-x", name]
        try:
            if pgrep_runner is not None:
                pids.extend(pgrep_runner(args))
            else:
                res = subprocess.run(args, capture_output=True, text=True, timeout=2, check=False)
                if res.returncode == 0:
                    for tok in res.stdout.split():
                        if tok.isdigit():
                            pids.append(int(tok))
        except (OSError, subprocess.SubprocessError):
            continue
    return sorted(set(pids))


def _codex_rollout_meta(path_str: str) -> dict[str, str] | None:
    """Validate a writable JSONL opened by a confirmed Codex PID via its header.

    Does NOT pre-filter on `/sessions/` or `/archived_sessions/` path segments
    so future storage directories (e.g. `future_threads/`) are still detected
    and propagated as `active_path` for completeness verification and doctor.
    """
    path = Path(path_str)
    meta = codex_sessions.read_rollout_meta(path, max_lines=1)
    if not meta:
        return None
    return {
        "sid": str(meta["sid"]),
        "cwd": str(meta["cwd"]),
        "codex_home": str(meta.get("codex_home") or ""),
        "rollout": path_str,
    }


def _extract_env_var_from_ps(ps_line: str, var_name: str) -> str:
    """Extract an env var value from `ps eww -o command=` without splitting on spaces."""
    if not ps_line or not var_name:
        return ""
    m = re.search(
        rf"(?:^|\s){re.escape(var_name)}=(.*?)(?=\s+[A-Za-z_][A-Za-z0-9_]*=|$)",
        ps_line.rstrip("\r\n"),
    )
    if not m:
        return ""
    return m.group(1).strip()


def _read_process_ps_eww(pid: int) -> str:
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
    return res.stdout.rstrip("\r\n")


def _process_env_var(pid: int, var_name: str) -> str:
    """Best-effort read of an environment variable from a live process via `ps eww`."""
    return _extract_env_var_from_ps(_read_process_ps_eww(pid), var_name)


def _process_codex_home(pid: int) -> str:
    """Resolve verified CODEX_HOME for a live Codex process via `ps eww`.

    Returns resolved `CODEX_HOME` if set, else `<HOME>/.codex` when `HOME` is
    visible in the process environment (confirming `CODEX_HOME` is unset), else `""`.
    """
    ps_line = _read_process_ps_eww(pid)
    if not ps_line:
        return ""
    chome = _extract_env_var_from_ps(ps_line, "CODEX_HOME")
    if chome:
        return str(Path(chome).expanduser().resolve())
    home_val = _extract_env_var_from_ps(ps_line, "HOME")
    if home_val:
        return str((Path(home_val).expanduser() / ".codex").resolve())
    return ""


def _pick_preferred_rollout_rec(
    prev: dict[str, str],
    curr: dict[str, str],
) -> dict[str, str]:
    """Pick the authoritative open rollout when one PID holds multiple writable FDs.

    If state_*.sqlite commits one of the open files, prefer that committed file so
    an uncommitted replacement/rollback FD opened before SQLite switches never
    overrides committed history.
    """
    chome_str = curr.get("codex_home") or prev.get("codex_home") or ""
    sid = curr.get("sid") or prev.get("sid") or ""
    if chome_str and sid:
        chome = Path(chome_str)
        sqlite_raw = codex_sessions.query_sqlite_rollout_path(sid, chome)
        if sqlite_raw is not None:
            sqlite_res = codex_sessions.resolve_alternate_rollout_path(sqlite_raw, chome)
            if sqlite_res is not None:
                skey = codex_sessions.path_identity_key(sqlite_res)
                if codex_sessions.path_identity_key(Path(prev["rollout"])) == skey:
                    return prev
                if codex_sessions.path_identity_key(Path(curr["rollout"])) == skey:
                    return curr
    try:
        prev_p = Path(prev["rollout"])
        curr_p = Path(curr["rollout"])
        prev_m = codex_sessions.read_rollout_meta(prev_p, max_lines=1)
        curr_m = codex_sessions.read_rollout_meta(curr_p, max_lines=1)
        ordered = codex_sessions.sort_rollout_entries([(prev_m, prev_p), (curr_m, curr_p)])
        return curr if ordered[-1][1] == curr_p else prev
    except OSError:
        return curr


def _collect_codex_tty_state(
    codex_panes: list[RawPane],
    *,
    pgrep_runner: Callable[[list[str]], list[int]] | None = None,
    lsof_runner: Callable[[list[str]], str] | None = None,
) -> tuple[dict[str, list[dict[str, str]]], dict[str, list[int]]]:
    if not codex_panes:
        return {}, {}
    names = tuple(sorted({p.current_command for p in codex_panes if p.current_command} | {"codex"}))
    pids = _find_pids_by_name(names, pgrep_runner=pgrep_runner)
    if not pids:
        return {}, {}
    lsof_data = _run_lsof_batch(pids, lsof_runner=lsof_runner)
    tty_map: dict[str, list[dict[str, str]]] = {}
    tty_pids: dict[str, list[int]] = {}
    for pid, entry in lsof_data.items():
        if not entry["ttys"]:
            continue
        for tty in entry["ttys"]:
            pid_bucket = tty_pids.setdefault(tty, [])
            if pid not in pid_bucket:
                pid_bucket.append(pid)
        if not entry["writable_jsonl"]:
            continue
        pid_codex_home: str | None = None
        records: dict[tuple[str, str], dict[str, str]] = {}
        for jpath in entry["writable_jsonl"]:
            meta = _codex_rollout_meta(jpath)
            if meta is not None:
                if not meta["codex_home"]:
                    if pid_codex_home is None:
                        pid_codex_home = _process_env_var(pid, "CODEX_HOME")
                    if pid_codex_home:
                        meta["codex_home"] = str(Path(pid_codex_home).expanduser().resolve())
                key = (meta["codex_home"], meta["sid"])
                prev = records.get(key)
                records[key] = meta if prev is None else _pick_preferred_rollout_rec(prev, meta)
        if not records:
            continue
        for tty in entry["ttys"]:
            bucket = tty_map.setdefault(tty, [])
            for rec in records.values():
                if rec not in bucket:
                    bucket.append(rec)
    return tty_map, tty_pids


def _build_codex_tty_map(
    codex_panes: list[RawPane],
    *,
    pgrep_runner: Callable[[list[str]], list[int]] | None = None,
    lsof_runner: Callable[[list[str]], str] | None = None,
) -> dict[str, list[dict[str, str]]]:
    tty_map, _ = _collect_codex_tty_state(
        codex_panes, pgrep_runner=pgrep_runner, lsof_runner=lsof_runner
    )
    return tty_map


def _load_claude_sessions(
    environment: Mapping[str, str],
    home: Path,
    process_exists: Callable[[int], bool],
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    """Return (by_tmux_target, by_pane_id) from ~/.claude/sessions/*.json."""
    config_dir = Path(environment.get("CLAUDE_CONFIG_DIR") or (home / ".claude")).expanduser()
    sessions_dir = config_dir / "sessions"
    by_target: dict[str, dict[str, Any]] = {}
    by_pane: dict[str, dict[str, Any]] = {}
    if not sessions_dir.is_dir():
        return by_target, by_pane
    try:
        entries = list(sessions_dir.glob("*.json"))
    except OSError:
        return by_target, by_pane
    for path in entries:
        rec = _read_safe_json(path)
        if not rec:
            continue
        pid = rec.get("pid")
        sid = rec.get("sessionId")
        tmux_target = rec.get("tmux")
        updated = rec.get("updatedAt") or rec.get("statusUpdatedAt") or rec.get("startedAt") or 0
        if (
            type(pid) is not int
            or pid <= 0
            or not isinstance(sid, str)
            or not sid
            or not isinstance(tmux_target, str)
            or not tmux_target
            or not process_exists(pid)
        ):
            continue
        item = {"pid": pid, "session_id": sid, "cwd": str(rec.get("cwd", "")), "updated_at": int(updated)}
        prev = by_target.get(tmux_target)
        if prev is None or item["updated_at"] >= prev["updated_at"]:
            by_target[tmux_target] = item
        if "." in tmux_target:
            pane_part = tmux_target.rsplit(".", 1)[1]
            if pane_part.startswith("%"):
                prev_p = by_pane.get(pane_part)
                if prev_p is None or item["updated_at"] >= prev_p["updated_at"]:
                    by_pane[pane_part] = item
    return by_target, by_pane


def _load_agy_runtime(
    pane: RawPane,
    environment: Mapping[str, str],
    home: Path,
    process_exists: Callable[[int], bool],
) -> dict[str, Any] | None:
    key = hashlib.sha256(f"{pane.socket_path}\0{pane.server_pid}\0{pane.pane_id}".encode()).hexdigest()
    path = _state_dir(environment, home) / "agy" / "runtime" / f"{key}.json"
    rec = _read_safe_json(path)
    if not rec or rec.get("version") != 1:
        return None
    pid = rec.get("pid")
    sid = rec.get("session_id")
    if (
        type(pid) is not int
        or pid <= 0
        or not process_exists(pid)
        or rec.get("pane") != pane.pane_id
        or rec.get("socket") != pane.socket_path
        or str(rec.get("server", "")) != pane.server_pid
        or rec.get("tty") != pane.short_tty
        or not isinstance(sid, str)
        or (sid and not UUID_RE.match(sid))
    ):
        return None
    return rec


def _load_dsh_sessions(
    panes: list[RawPane],
    environment: Mapping[str, str],
    home: Path,
    process_exists: Callable[[int], bool],
) -> dict[str, dict[str, Any]]:
    """Map pane_id -> validated dsh runtime session record."""
    homes: set[Path] = {Path(environment.get("DSH_HOME") or (home / ".dsh")).expanduser()}
    for pane in panes:
        rec = _parse_activity_record(pane.activity_record)
        if rec and isinstance(rec.get("dsh_runtime"), dict):
            ref_path = rec["dsh_runtime"].get("path")
            if isinstance(ref_path, str):
                p = Path(ref_path)
                if p.is_absolute() and p.parent.name == "activity" and p.parent.parent.name == "aipane":
                    homes.add(p.parents[2])

    by_pane: dict[str, list[dict[str, Any]]] = {}
    now_ms = time.time() * 1000 + 60_000
    for dsh_home in homes:
        sdir = dsh_home / "aipane" / "sessions"
        if not sdir.is_dir():
            continue
        try:
            entries = os.scandir(sdir)
        except OSError:
            continue
        with entries:
            for entry in entries:
                if not entry.name.endswith(".json"):
                    continue
                stem = entry.name[:-5]
                if not stem.isdigit():
                    continue
                pid_from_name = int(stem)
                if not process_exists(pid_from_name):
                    continue
                rec = _read_safe_json(Path(entry.path), max_bytes=16384)
                if not rec or rec.get("version") != 1:
                    continue
                pid = rec.get("pid")
                pane_id = rec.get("pane_id")
                sid = rec.get("session_id")
                updated = rec.get("updated_at")
                if (
                    type(pid) is not int
                    or pid != pid_from_name
                    or not isinstance(pane_id, str)
                    or not pane_id
                    or not isinstance(sid, str)
                    or not sid
                    or type(updated) not in (int, float)
                    or not 0 < updated <= now_ms
                ):
                    continue
                by_pane.setdefault(pane_id, []).append(rec)

    resolved: dict[str, dict[str, Any]] = {}
    pane_lookup = {p.pane_id: p for p in panes}
    for pane_id, candidates in by_pane.items():
        pane = pane_lookup.get(pane_id)
        if pane is None:
            continue
        matched = [
            c
            for c in candidates
            if c.get("socket") == pane.socket_path and str(c.get("server_pid", "")) == pane.server_pid
        ]
        if len(matched) == 1:
            resolved[pane_id] = matched[0]
    return resolved


def _load_grok_tty_map(
    grok_panes: list[RawPane],
    environment: Mapping[str, str],
    home: Path,
    process_exists: Callable[[int], bool],
    lsof_runner: Callable[[list[str]], str] | None = None,
) -> dict[str, list[dict[str, str]]]:
    if not grok_panes:
        return {}
    grok_home = Path(environment.get("GROK_HOME") or (home / ".grok")).expanduser()
    active_file = grok_home / "active_sessions.json"
    try:
        records = json.loads(active_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(records, list):
        return {}
    live_recs: list[dict[str, Any]] = []
    for rec in records:
        if not isinstance(rec, dict):
            continue
        pid = rec.get("pid")
        sid = rec.get("session_id")
        cwd = rec.get("cwd")
        if (
            type(pid) is not int
            or pid <= 0
            or not isinstance(sid, str)
            or not sid
            or not isinstance(cwd, str)
            or not os.path.isabs(cwd)
            or not process_exists(pid)
        ):
            continue
        try:
            uuid.UUID(sid)
        except ValueError:
            continue
        live_recs.append(rec)
    if not live_recs:
        return {}
    lsof_data = _run_lsof_batch([r["pid"] for r in live_recs], lsof_runner=lsof_runner)
    tty_map: dict[str, list[dict[str, str]]] = {}
    for rec in live_recs:
        ttys = lsof_data.get(rec["pid"], {}).get("ttys", set())
        for tty in ttys:
            tty_map.setdefault(tty, []).append({"sid": rec["session_id"], "cwd": rec["cwd"]})
    return tty_map


def _kimi_state_title(
    sid: str,
    environment: Mapping[str, str],
    home: Path,
) -> str:
    if not sid:
        return ""
    roots: list[Path] = []
    for env_key in ("KIMI_CODE_HOME", "KIMI_HOME"):
        val = environment.get(env_key)
        if val:
            roots.append(Path(val).expanduser())
    if not roots:
        roots = [home / ".kimi-code", home / ".kimi"]
    for root in roots:
        index_path = root / "session_index.jsonl"
        if not index_path.is_file():
            continue
        try:
            lines = index_path.read_text(encoding="utf-8", errors="ignore").splitlines()
        except OSError:
            continue
        for raw in reversed(lines):
            if sid not in raw:
                continue
            try:
                entry = json.loads(raw)
            except ValueError:
                continue
            if entry.get("sessionId") != sid or entry.get("deleted") is True:
                continue
            sdir = entry.get("sessionDir")
            if isinstance(sdir, str) and sdir:
                state = _read_safe_json(Path(sdir).expanduser() / "state.json")
                if state and isinstance(state.get("title"), str):
                    return _clean_title(state["title"])
            break
    return ""


def _resolve_pi_node_pids(
    node_panes: list[RawPane],
    registry: Mapping[str, dict[str, str]],
    *,
    pgrep_runner: Callable[[list[str]], list[int]] | None = None,
    lsof_runner: Callable[[list[str]], str] | None = None,
) -> set[str]:
    """Return short TTYs where a live node process is confirmed to be Pi."""
    candidates = [
        p
        for p in node_panes
        if p.current_command.lower() == "node"
        and (registry.get(p.pane_id) or {}).get("tool") in {"p", "pi"}
    ]
    if not candidates:
        return set()
    pids = _find_pids_by_name(("node",), pgrep_runner=pgrep_runner)
    if not pids:
        return set()
    lsof_data = _run_lsof_batch(pids, lsof_runner=lsof_runner)
    confirmed_ttys: set[str] = set()
    for entry in lsof_data.values():
        if entry["ttys"] and entry["paths"]:
            confirmed_ttys.update(entry["ttys"])
    return confirmed_ttys


def _resolve_single_raw_pane(
    pane: RawPane,
    *,
    current_pane_id: str,
    registry: Mapping[str, dict[str, str]],
    codex_tty_map: Mapping[str, list[dict[str, str]]],
    codex_tty_pids: Mapping[str, list[int]],
    claude_by_target: Mapping[str, dict[str, Any]],
    claude_by_pane: Mapping[str, dict[str, Any]],
    dsh_by_pane: Mapping[str, dict[str, Any]],
    grok_tty_map: Mapping[str, list[dict[str, str]]],
    pi_node_ttys: set[str],
    environment: Mapping[str, str],
    home: Path,
    process_exists: Callable[[int], bool],
) -> LivePaneSession:
    cmd = pane.current_command
    act_rec = _parse_activity_record(pane.activity_record)
    reg_rec = registry.get(pane.pane_id) or {}

    agent = ""
    sid = ""
    authority = ""
    title = ""
    cwd = pane.cwd
    active_path = ""
    storage_root = ""

    # 1. Antigravity CLI
    if cmd == "agy":
        agent = "antigravity"
        rt = _load_agy_runtime(pane, environment, home, process_exists)
        if rt:
            sid = str(rt.get("session_id") or "")
            title = _clean_title(str(rt.get("title") or ""))
            if rt.get("cwd"):
                cwd = str(rt["cwd"])
            if sid:
                authority = "agy_runtime"

    # 2. Codex
    elif _is_codex_cmd(cmd):
        agent = "codex"
        preferred = ""
        if act_rec and isinstance(act_rec.get("root"), dict):
            preferred = str(act_rec["root"].get("session_id") or "")
        if not preferred and reg_rec.get("tool") in {"x", "codex"}:
            preferred = reg_rec.get("sid", "")
        cands = codex_tty_map.get(pane.short_tty, [])
        if len(cands) == 1:
            sid = cands[0]["sid"]
            active_path = cands[0].get("rollout", "")
            storage_root = cands[0].get("codex_home", "")
            authority = "codex_lsof"
        elif len(cands) > 1 and preferred:
            matched = [c for c in cands if c["sid"] == preferred]
            if len(matched) == 1:
                sid = matched[0]["sid"]
                active_path = matched[0].get("rollout", "")
                storage_root = matched[0].get("codex_home", "")
                authority = "codex_lsof"
        if (
            authority == "codex_lsof"
            and not storage_root
            and act_rec
            and isinstance(act_rec.get("root"), dict)
            and act_rec["root"].get("codex_home")
        ):
            root_info = act_rec["root"]
            if root_info.get("session_id") == sid or (
                active_path and str(root_info.get("transcript_path") or "") == active_path
            ):
                storage_root = str(root_info.get("codex_home") or "")
        if not sid and preferred:
            if act_rec and isinstance(act_rec.get("root"), dict) and act_rec["root"].get("session_id") == preferred:
                sid = preferred
                active_path = str(act_rec["root"].get("transcript_path") or "")
                storage_root = str(act_rec["root"].get("codex_home") or "")
                authority = "codex_activity"
            elif reg_rec.get("tool") in {"x", "codex"} and reg_rec.get("sid") == preferred:
                sid = preferred
                authority = "registry"
                reg_chome = str(reg_rec.get("codex_home") or "")
                if reg_chome:
                    storage_root = str(Path(reg_chome).expanduser().resolve())
            if sid and not storage_root and not active_path:
                tty_pids = codex_tty_pids.get(pane.short_tty, [])
                if len(tty_pids) == 1:
                    storage_root = _process_codex_home(tty_pids[0])

    # 3. Claude Code
    elif _is_claude_cmd(cmd):
        agent = "claude"
        cl = claude_by_target.get(pane.claude_target) or claude_by_pane.get(pane.pane_id)
        if cl and cl.get("session_id"):
            sid = cl["session_id"]
            authority = "claude_sessions"
            if cl.get("cwd"):
                cwd = cl["cwd"]
        elif reg_rec.get("tool") in {"c", "claude"} and reg_rec.get("sid"):
            sid = reg_rec["sid"]
            authority = "registry"

    # 4. Kimi Code
    elif _is_kimi_cmd(cmd):
        agent = "kimi"
        if act_rec and isinstance(act_rec.get("session"), dict) and act_rec["session"].get("id"):
            sid = str(act_rec["session"]["id"])
            authority = "kimi_activity"
        elif reg_rec.get("tool") in {"k", "kimi"} and reg_rec.get("sid"):
            sid = reg_rec["sid"]
            authority = "registry"
        if sid:
            title = _kimi_state_title(sid, environment, home)

    # 5. Grok
    elif _is_grok_cmd(cmd):
        agent = "grok"
        cands = grok_tty_map.get(pane.short_tty, [])
        if len(cands) == 1:
            sid = cands[0]["sid"]
            authority = "grok_active"
            if cands[0].get("cwd"):
                cwd = cands[0]["cwd"]

    # 6. dsh-TUI (may appear as node or dsh/dsh-tui)
    elif _is_dsh_cmd(cmd) or pane.pane_id in dsh_by_pane:
        agent = "dsh"
        dsh_rec = dsh_by_pane.get(pane.pane_id)
        if dsh_rec and dsh_rec.get("session_id"):
            sid = str(dsh_rec["session_id"])
            authority = "dsh_channel"
            if dsh_rec.get("cwd"):
                cwd = str(dsh_rec["cwd"])

    # 7. Pi (explicit pi binary or verified node running pi)
    elif _is_pi_cmd(cmd) or (cmd.lower() == "node" and pane.short_tty in pi_node_ttys):
        agent = "pi"
        if reg_rec.get("tool") in {"p", "pi"} and reg_rec.get("sid"):
            sid = reg_rec["sid"]
            authority = "registry"

    if not title:
        title = _clean_title(pane.pane_title)

    return LivePaneSession(
        pane_id=pane.pane_id,
        coord=pane.coord,
        session_name=pane.session_name,
        window_id=pane.window_id,
        window_index=pane.window_index,
        window_name=pane.window_name,
        pane_index=pane.pane_index,
        pane_active=pane.pane_active,
        window_active=pane.window_active,
        current_command=cmd,
        cwd=cwd,
        pane_tty=pane.pane_tty,
        pane_title=pane.pane_title,
        agent=agent,
        agent_alias=DISPLAY_AGENT.get(agent, ""),
        session_id=sid,
        title=title,
        authority=authority,
        is_self=(bool(current_pane_id) and pane.pane_id == current_pane_id),
        pane_in_mode=pane.pane_in_mode,
        active_path=active_path,
        storage_root=storage_root,
    )


def list_live_panes(
    *,
    target: str | None = None,
    only_agents: bool = False,
    environment: Mapping[str, str] | None = None,
    home: Path | None = None,
    tmux_runner: Callable[[list[str]], subprocess.CompletedProcess[str]] | None = None,
    pgrep_runner: Callable[[list[str]], list[int]] | None = None,
    lsof_runner: Callable[[list[str]], str] | None = None,
    process_exists: Callable[[int], bool] | None = None,
) -> list[LivePaneSession]:
    """Resolve live tmux panes to their current authoritative AI session IDs."""
    env = dict(os.environ if environment is None else environment)
    resolved_home = (home or Path(env.get("HOME", str(Path.home())))).expanduser()
    exists_fn = process_exists or _default_process_exists
    current_pane_id = env.get("TMUX_PANE", "")

    raw_panes = list_raw_panes(target=target, environment=env, tmux_runner=tmux_runner)
    if not raw_panes:
        return []

    socket_path = raw_panes[0].socket_path
    server_pid = raw_panes[0].server_pid

    codex_panes = [p for p in raw_panes if _is_codex_cmd(p.current_command)]
    grok_panes = [p for p in raw_panes if _is_grok_cmd(p.current_command)]
    claude_panes = [p for p in raw_panes if _is_claude_cmd(p.current_command)]
    kimi_panes = [p for p in raw_panes if _is_kimi_cmd(p.current_command)]
    pi_explicit = [p for p in raw_panes if _is_pi_cmd(p.current_command)]
    node_or_dsh = [p for p in raw_panes if _is_dsh_cmd(p.current_command) or p.current_command.lower() == "node"]

    codex_tty_map, codex_tty_pids = _collect_codex_tty_state(
        codex_panes, pgrep_runner=pgrep_runner, lsof_runner=lsof_runner
    )
    claude_by_target, claude_by_pane = (
        _load_claude_sessions(env, resolved_home, exists_fn) if claude_panes else ({}, {})
    )
    dsh_by_pane = (
        _load_dsh_sessions(node_or_dsh, env, resolved_home, exists_fn) if node_or_dsh else {}
    )
    grok_tty_map = (
        _load_grok_tty_map(grok_panes, env, resolved_home, exists_fn, lsof_runner=lsof_runner)
        if grok_panes
        else {}
    )
    non_dsh_nodes = [p for p in node_or_dsh if p.pane_id not in dsh_by_pane]

    # Only read registry.jsonl when at least one pane may actually consult it.
    needs_registry = bool(pi_explicit or non_dsh_nodes) or any(
        len(codex_tty_map.get(p.short_tty, [])) != 1 for p in codex_panes
    ) or any(
        p.claude_target not in claude_by_target and p.pane_id not in claude_by_pane
        for p in claude_panes
    ) or any(
        not ((_parse_activity_record(p.activity_record) or {}).get("session") or {}).get("id")
        for p in kimi_panes
    )
    registry = (
        _load_registry_latest(socket_path, server_pid, env, resolved_home)
        if needs_registry
        else {}
    )
    pi_node_ttys = _resolve_pi_node_pids(
        non_dsh_nodes, registry, pgrep_runner=pgrep_runner, lsof_runner=lsof_runner
    )

    resolved = [
        _resolve_single_raw_pane(
            pane,
            current_pane_id=current_pane_id,
            registry=registry,
            codex_tty_map=codex_tty_map,
            codex_tty_pids=codex_tty_pids,
            claude_by_target=claude_by_target,
            claude_by_pane=claude_by_pane,
            dsh_by_pane=dsh_by_pane,
            grok_tty_map=grok_tty_map,
            pi_node_ttys=pi_node_ttys,
            environment=env,
            home=resolved_home,
            process_exists=exists_fn,
        )
        for pane in raw_panes
    ]
    if only_agents:
        return [item for item in resolved if item.agent]
    return resolved


def resolve_pane_spec(
    spec: str,
    panes: list[LivePaneSession],
    *,
    current_pane_id: str = "",
) -> LivePaneSession:
    """Resolve a human/machine pane specifier against live panes.

    Supported forms:
      - '.'                 current pane
      - '%510'              exact tmux pane ID
      - '2'                 pane index in the current window
      - '4.1'               window.pane in the current session
      - '0:4.1'             full session:window.pane coordinate
    """
    raw = spec.strip()
    if not raw:
        raise ValueError("empty pane specifier")

    current = next((p for p in panes if p.pane_id == current_pane_id), None) if current_pane_id else None
    if current is None:
        current = next((p for p in panes if p.window_active and p.pane_active), None)

    if raw == ".":
        if current is None:
            raise ValueError("cannot determine current tmux pane; pass an explicit coordinate or pane ID")
        return current

    if raw.startswith("%"):
        match = next((p for p in panes if p.pane_id == raw), None)
        if match is None:
            raise FileNotFoundError(f"live tmux pane not found: {raw}")
        return match

    if raw.isdigit():
        if current is None:
            raise ValueError(f"pane index {raw!r} requires a current tmux window context")
        matches = [
            p
            for p in panes
            if p.session_name == current.session_name
            and p.window_id == current.window_id
            and p.pane_index == raw
        ]
        if len(matches) == 1:
            return matches[0]
        raise FileNotFoundError(f"pane index {raw} not found in current window {current.session_name}:{current.window_index}")

    if ":" not in raw and "." in raw:
        if current is None:
            raise ValueError(f"relative coordinate {raw!r} requires a current tmux session context")
        full_coord = f"{current.session_name}:{raw}"
        match = next((p for p in panes if p.coord == full_coord), None)
        if match is not None:
            return match
        raise FileNotFoundError(f"live tmux pane not found: {full_coord}")

    match = next((p for p in panes if p.coord == raw), None)
    if match is not None:
        return match
    raise FileNotFoundError(f"live tmux pane not found: {raw}")


def format_yank_banner(item: LivePaneSession) -> str:
    sid = item.session_id
    short_sid = f"{sid[:8]}…{sid[-4:]}" if len(sid) > 15 else sid
    tag = item.agent_alias or item.agent
    label = f"{tag} · {item.coord}"
    if item.title:
        clipped = item.title if len(item.title) <= 36 else item.title[:35] + "…"
        label += f' "{clipped}"'
    return f"Copied [{label}] {short_sid}"


def copy_text_to_clipboard(
    text: str,
    *,
    environment: Mapping[str, str] | None = None,
    tmux_runner: Callable[[list[str], str], bool] | None = None,
    pbcopy_runner: Callable[[str], bool] | None = None,
) -> list[str]:
    """Write text to macOS clipboard (when local) and tmux paste buffer."""
    env = dict(os.environ if environment is None else environment)
    written: list[str] = []

    if not env.get("SSH_CONNECTION"):
        if pbcopy_runner is not None:
            if pbcopy_runner(text):
                written.append("pbcopy")
        else:
            try:
                proc = subprocess.run(["pbcopy"], input=text, text=True, timeout=2, check=False)
                if proc.returncode == 0:
                    written.append("pbcopy")
            except (OSError, subprocess.SubprocessError):
                pass

    cmd = [*_tmux_command(env), "load-buffer", "-"]
    if tmux_runner is not None:
        if tmux_runner(cmd, text):
            written.append("tmux")
    else:
        try:
            proc = subprocess.run(cmd, input=text, text=True, env=env, timeout=2, check=False)
            if proc.returncode == 0:
                written.append("tmux")
        except (OSError, subprocess.SubprocessError):
            pass

    return written
