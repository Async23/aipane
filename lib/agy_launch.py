"""Unified Antigravity CLI launcher: catalog, picker, theme, skills, routes, recovery."""

from __future__ import annotations

import copy
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import shlex
import socket
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any

from agy_catalog import (
    Catalog,
    EFFORT_ALIASES,
    EFFORT_SHORT,
    ModelFamily,
    ModelVariant,
    atomic_write_json,
    binary_signature,
    fallback_catalog,
    fetch_live_catalog,
    format_quota_for_group,
    is_cache_stale,
    load_cached_catalog,
    load_quota_snapshot,
    quota_group_for_model,
    quota_snapshot_path,
    state_dir,
    try_background_refresh,
)
import agy_sessions
from tui_picker import (
    DynamicBindings,
    PickerPage,
    PickerRow,
    cell,
    display_text,
    format_records,
    select,
)


SUBCOMMANDS = frozenset({
    "acp",
    "agent",
    "agents",
    "changelog",
    "help",
    "install",
    "mcp",
    "mic-serve",
    "models",
    "plugin",
    "plugins",
    "remote-control",
    "update",
})
VALUE_OPTIONS = frozenset({
    "-p",
    "-i",
    "--add-dir",
    "--agent",
    "--conversation",
    "--effort",
    "--input-format",
    "--json-schema",
    "--log-file",
    "--mode",
    "--model",
    "--output-format",
    "--print",
    "--print-timeout",
    "--project",
    "--prompt",
    "--prompt-interactive",
    "--settings",
    "--route",
    "--aipane-route",
})
PLAN_ENV_UNSET = ("AGY_ADC_AUTH",)
GCP_ENV_UNSET = (
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "GOOGLE_GEMINI_BASE_URL",
    "CLOUD_CODE_URL",
)


@dataclass(frozen=True)
class RouteConfig:
    name: str
    label: str
    project: str = ""
    location: str = "global"
    credentials: str = ""
    https_proxy: str = ""
    no_proxy: str = "localhost,127.0.0.1"


@dataclass
class ParsedArgs:
    passthrough: bool
    subcommand: str
    remaining: list[str]
    model: str | None
    effort: str | None
    settings_values: list[str]
    route: str | None
    conversation_id: str | None
    continue_session: bool
    non_interactive_flag: bool
    add_dirs: list[str]


def cli_home(environment: dict[str, str] | None = None) -> Path:
    env = os.environ if environment is None else environment
    return Path(env.get("HOME", str(Path.home()))) / ".gemini/antigravity-cli"


def native_agy_binary(environment: dict[str, str] | None = None) -> Path:
    env = os.environ if environment is None else environment
    override = env.get("AIPANE_AGY_BIN") or env.get("AGY_BIN")
    if override:
        return Path(override).expanduser()
    home_bin = Path(env.get("HOME", str(Path.home()))) / ".local/bin/agy"
    if home_bin.exists():
        return home_bin
    raise FileNotFoundError(f"找不到原生 agy 可执行文件: {home_bin}")


def launch_config_path(environment: dict[str, str] | None = None) -> Path:
    env = os.environ if environment is None else environment
    override = env.get("AIPANE_AGY_LAUNCH_CONFIG")
    if override:
        return Path(override).expanduser()
    xdg = Path(env.get("XDG_CONFIG_HOME") or Path(env.get("HOME", str(Path.home()))) / ".config")
    return xdg / "aipane/agy-launch.json"


def load_routes(environment: dict[str, str] | None = None) -> dict[str, RouteConfig]:
    routes: dict[str, RouteConfig] = {
        "plan": RouteConfig(name="plan", label="订阅配额"),
    }
    path = launch_config_path(environment)
    if not path.is_file():
        return routes
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return routes
    raw_routes = data.get("routes") if isinstance(data, dict) else None
    if not isinstance(raw_routes, dict):
        return routes
    gcp = raw_routes.get("gcp")
    if isinstance(gcp, dict) and gcp.get("project") and gcp.get("disabled") is not True:
        routes["gcp"] = RouteConfig(
            name="gcp",
            label=display_text(gcp.get("label") or "GCP 按量"),
            project=str(gcp["project"]).strip(),
            location=str(gcp.get("location") or "global").strip(),
            credentials=str(Path(str(gcp.get("credentials") or "")).expanduser()) if gcp.get("credentials") else "",
            https_proxy=str(gcp.get("https_proxy") or "").strip(),
            no_proxy=str(gcp.get("no_proxy") or "localhost,127.0.0.1").strip(),
        )
    return routes


def parse_args(argv: list[str]) -> ParsedArgs:
    """Single source of truth for Antigravity CLI option & subcommand parsing."""
    remaining: list[str] = []
    model: str | None = None
    effort: str | None = None
    settings_values: list[str] = []
    route: str | None = None
    conversation_id: str | None = None
    continue_session = False
    non_interactive_flag = False
    add_dirs: list[str] = []
    passthrough = False
    subcommand = ""

    index = 0
    while index < len(argv):
        arg = argv[index]
        if arg == "--":
            remaining.extend(argv[index:])
            break
        option = arg.split("=", 1)[0]
        if option in ("-h", "--help", "--version"):
            passthrough = True
            remaining.extend(argv[index:])
            break
        if option in VALUE_OPTIONS:
            if "=" in arg:
                value = arg.split("=", 1)[1]
                consumed = [arg]
            else:
                if index + 1 >= len(argv):
                    raise ValueError(f"{option} 后面缺少值")
                value = argv[index + 1]
                consumed = [arg, value]
                index += 1
            if option == "--model":
                model = value
            elif option == "--effort":
                effort = value
            elif option == "--settings":
                settings_values.append(value)
            elif option in ("--route", "--aipane-route"):
                route = value.strip().lower()
            elif option == "--conversation":
                conversation_id = value
                remaining.extend(consumed)
            elif option == "--add-dir":
                add_dirs.append(value)
                remaining.extend(consumed)
            else:
                if option in ("-p", "--print", "--prompt"):
                    non_interactive_flag = True
                remaining.extend(consumed)
        elif arg.startswith("-"):
            if arg in ("-c", "--continue"):
                continue_session = True
            remaining.append(arg)
        else:
            # First positional argument selects an agy subcommand.
            passthrough = True
            subcommand = arg
            remaining.extend(argv[index:])
            break
        index += 1

    return ParsedArgs(
        passthrough=passthrough,
        subcommand=subcommand,
        remaining=remaining,
        model=model,
        effort=effort,
        settings_values=settings_values,
        route=route,
        conversation_id=conversation_id,
        continue_session=continue_session,
        non_interactive_flag=non_interactive_flag,
        add_dirs=add_dirs,
    )


def detect_color_scheme(environment: dict[str, str] | None = None) -> str:
    env = os.environ if environment is None else environment
    light = env.get("AGY_LIGHT_SCHEME", "solarized light")
    dark = env.get("AGY_DARK_SCHEME", "solarized dark")
    appearance = env.get("AGY_APPEARANCE_OVERRIDE")
    if appearance is None:
        try:
            completed = subprocess.run(
                ["defaults", "read", "-g", "AppleInterfaceStyle"],
                capture_output=True,
                text=True,
                timeout=1.5,
                check=False,
            )
            appearance = completed.stdout.strip() if completed.returncode == 0 else ""
        except (OSError, subprocess.SubprocessError):
            appearance = ""
    return dark if appearance.strip().lower() == "dark" else light


def load_settings_arg(raw: str) -> dict[str, Any]:
    stripped = raw.lstrip()
    if stripped.startswith("{"):
        try:
            data = json.loads(raw)
        except ValueError as error:
            raise ValueError(f"--settings 不是有效的 JSON: {error}") from error
    else:
        path = Path(raw).expanduser()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise ValueError(f"无法读取 --settings 文件 {path}: {error}") from error
    if not isinstance(data, dict):
        raise ValueError("--settings 必须是 JSON 对象")
    return data


def deep_merge_dict(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge_dict(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def compose_settings(
    *,
    scheme: str,
    route_cfg: RouteConfig,
    user_settings_list: list[dict[str, Any]],
) -> dict[str, Any]:
    """Deep-merge monoid: Theme (+) Route (+) UserSettings."""
    result: dict[str, Any] = {"colorScheme": scheme}
    if route_cfg.name == "gcp" and route_cfg.project:
        result = deep_merge_dict(
            result,
            {
                "modelProvider": "",
                "gcp": {
                    "project": route_cfg.project,
                    "location": route_cfg.location or "global",
                },
            },
        )
    for layer in user_settings_list:
        result = deep_merge_dict(result, layer)
    return result


def build_route_environment(
    route_cfg: RouteConfig,
    base_env: dict[str, str],
) -> tuple[dict[str, str], list[str]]:
    """Return (env_for_agy, extra_agy_args_for_models_query)."""
    env = dict(base_env)
    env["AIPANE_AGY_ROUTE"] = route_cfg.name
    if route_cfg.name == "gcp":
        for key in GCP_ENV_UNSET:
            env.pop(key, None)
        env["AGY_ADC_AUTH"] = "true"
        if route_cfg.credentials:
            env["GOOGLE_APPLICATION_CREDENTIALS"] = route_cfg.credentials
        if route_cfg.project:
            env["GOOGLE_CLOUD_PROJECT"] = route_cfg.project
            env["GOOGLE_CLOUD_QUOTA_PROJECT"] = route_cfg.project
        if route_cfg.location:
            env["GOOGLE_CLOUD_LOCATION"] = route_cfg.location
        if route_cfg.https_proxy:
            env.setdefault("HTTPS_PROXY", route_cfg.https_proxy)
            env.setdefault("HTTP_PROXY", env["HTTPS_PROXY"])
        if route_cfg.no_proxy:
            env.setdefault("NO_PROXY", route_cfg.no_proxy)
        settings = json.dumps(
            {
                "modelProvider": "",
                "gcp": {"project": route_cfg.project, "location": route_cfg.location or "global"},
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return env, ["--settings", settings]

    for key in PLAN_ENV_UNSET:
        env.pop(key, None)
    return env, []


def inherit_skill_dirs(
    cwd: Path,
    home: Path,
    explicit_add_dirs: list[str],
) -> list[str]:
    """Return missing parent directories (nearest first, up to HOME) with .agents/skills."""
    resolved_home = home.resolve()
    resolved_cwd = cwd.resolve()
    existing = set()
    for raw in explicit_add_dirs:
        if not raw:
            continue
        candidate = Path(raw).expanduser()
        if not candidate.is_absolute():
            candidate = resolved_cwd / candidate
        try:
            existing.add(candidate.resolve())
        except OSError:
            existing.add(candidate)

    inherited: list[str] = []
    current = resolved_cwd
    while current != resolved_home and current.is_relative_to(resolved_home):
        current = current.parent
        if (current / ".agents" / "skills").is_dir() and current not in existing:
            inherited.extend(["--add-dir", str(current)])
            existing.add(current)
    return inherited


def read_agy_settings(environment: dict[str, str] | None = None) -> dict[str, Any]:
    path = cli_home(environment) / "settings.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def picker_state_path(environment: dict[str, str] | None = None) -> Path:
    return state_dir(environment) / "picker-state.json"


def load_picker_state(environment: dict[str, str] | None = None) -> dict[str, Any]:
    path = picker_state_path(environment)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and data.get("version") == 1:
            return data
    except (OSError, ValueError):
        pass
    return {
        "version": 1,
        "last_route": "plan",
        "last_model_by_route": {},
        "effort_by_family": {},
        "last_seen_settings_model": "",
    }


def save_picker_state(
    *,
    state: dict[str, Any],
    route: str,
    variant: ModelVariant,
    settings_model: str,
    environment: dict[str, str] | None = None,
) -> None:
    updated = copy.deepcopy(state)
    updated["version"] = 1
    updated["last_route"] = route
    by_route = updated.setdefault("last_model_by_route", {})
    if not isinstance(by_route, dict):
        by_route = {}
        updated["last_model_by_route"] = by_route
    by_route[route] = variant.slug
    by_family = updated.setdefault("effort_by_family", {})
    if not isinstance(by_family, dict):
        by_family = {}
        updated["effort_by_family"] = by_family
    if variant.variant_label and variant.variant_label != "fixed":
        by_family[f"{route}:{variant.family}"] = variant.variant_label
    updated["last_seen_settings_model"] = settings_model
    updated["updated_at"] = time.time()
    try:
        atomic_write_json(picker_state_path(environment), updated)
    except OSError as error:
        print(
            f"aipane-agy-launch: 无法记住本次选择（{error}）；仍使用 {variant.slug} 启动",
            file=sys.stderr,
        )


def resolve_continue_conversation_id(cwd: Path, environment: dict[str, str] | None = None) -> str | None:
    path = cli_home(environment) / "cache/last_conversations.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    target = str(cwd.resolve())
    for key, value in data.items():
        if isinstance(key, str) and isinstance(value, str) and agy_sessions.SID.fullmatch(value):
            try:
                if str(Path(key).resolve()) == target:
                    return value
            except OSError:
                if key == target:
                    return value
    return None


def resolve_session_model(
    sid: str,
    environment: dict[str, str] | None = None,
) -> tuple[str | None, str | None]:
    """Return (model_identifier, route) for a saved conversation if available."""
    if not sid or not agy_sessions.SID.fullmatch(sid):
        return None, None
    home = cli_home(environment)
    binding = agy_sessions._read(agy_sessions.session_path(sid, home, environment))
    if isinstance(binding, dict):
        model_id = binding.get("model") or ""
        route = binding.get("route") or None
        if isinstance(model_id, str) and model_id.strip():
            return model_id.strip(), route if isinstance(route, str) and route else None
    transcript = home / "brain" / sid / ".system_generated/logs/transcript.jsonl"
    try:
        with transcript.open("rb") as stream:
            size = stream.seek(0, os.SEEK_END)
            limit = 4 * 1024 * 1024
            stream.seek(max(0, size - limit))
            if size > limit:
                stream.readline()
            lines = stream.read().splitlines()
        for raw_line in reversed(lines):
            try:
                step = json.loads(raw_line)
            except (ValueError, UnicodeDecodeError):
                continue
            if not isinstance(step, dict) or step.get("type") != "PLANNER_RESPONSE":
                continue
            candidate = step.get("model")
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip(), None
    except OSError:
        return None, None
    return None, None


def get_catalog_for_route(
    binary: Path,
    route_cfg: RouteConfig,
    *,
    base_env: dict[str, str],
    settings_model: str = "",
    remembered_slug: str = "",
    force_refresh: bool = False,
    allow_background_refresh: bool = True,
    launcher_script: Path | None = None,
) -> Catalog:
    sig = binary_signature(binary)
    cached = load_cached_catalog(route_cfg.name, environment=base_env)
    route_env, extra_args = build_route_environment(route_cfg, base_env)

    if not force_refresh and cached is not None:
        if allow_background_refresh and launcher_script is not None and is_cache_stale(cached, sig):
            try_background_refresh(launcher_script, route=route_cfg.name, environment=base_env)
        return cached

    if not force_refresh and sys.stderr.isatty():
        print(f"正在拉取 Antigravity 模型列表（{route_cfg.label}）…", file=sys.stderr, flush=True)
    try:
        return fetch_live_catalog(
            binary,
            route=route_cfg.name,
            extra_args=extra_args,
            env=route_env,
            environment_for_cache=base_env,
            persist=True,
        )
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        if cached is not None:
            print(
                f"aipane-agy-launch: 在线刷新失败（{error}），继续使用本地缓存",
                file=sys.stderr,
            )
            return Catalog(
                route=cached.route,
                fetched_at=cached.fetched_at,
                binary_signature=cached.binary_signature,
                variants=cached.variants,
                families=cached.families,
                source="stale-cache",
            )
        print(
            f"aipane-agy-launch: 无法获取模型列表（{error}），使用保底目录",
            file=sys.stderr,
        )
        return fallback_catalog(
            route_cfg.name,
            settings_model=settings_model,
            remembered_slug=remembered_slug,
            binary_sig=sig,
        )


def _format_effort_bar(family: ModelFamily, active_label: str, *, wide: bool) -> str:
    if not family.adjustable:
        if family.variants[0].effort:
            return f"固定 ({EFFORT_SHORT.get(family.variants[0].effort, family.variants[0].effort)})"
        return "固定"
    if not wide:
        token = EFFORT_SHORT.get(active_label, active_label)
        return f"[{token.upper()}]"
    parts: list[str] = []
    for variant in family.variants:
        label = variant.variant_label
        short = EFFORT_SHORT.get(label, label)
        if label == active_label:
            parts.append(f"[{short.upper()}]")
        else:
            parts.append(short)
    return " · ".join(parts)


def _catalog_freshness_note(catalog: Catalog, now: float) -> str:
    if catalog.source == "fallback":
        return "离线保底目录（按 Ctrl+R 同步）"
    if catalog.source == "stale-cache":
        return "离线缓存（按 Ctrl+R 重试同步）"
    if catalog.fetched_at <= 0:
        return ""
    age = max(0.0, now - catalog.fetched_at)
    if age < 60:
        return "目录刚同步"
    if age < 3600:
        return f"目录 {int(age // 60)}m 前同步"
    return f"目录 {int(age // 3600)}h 前同步"


def build_picker_page(
    catalog: Catalog,
    *,
    route_cfg: RouteConfig,
    view: str,
    efforts: dict[str, str],
    default_slug: str,
    quota_snapshot: dict[str, Any] | None,
    has_multiple_routes: bool,
    scheme: str,
    effort_lock: str | None = None,
) -> PickerPage:
    now = time.time()
    freshness = _catalog_freshness_note(catalog, now)
    view_tag = "全部变体" if view == "flat" else "模型家族"
    border_label = f" Antigravity · {route_cfg.label} · {view_tag} "
    subtitle_parts = ["本次启动使用所选模型"]
    if has_multiple_routes:
        subtitle_parts.append("Ctrl+G 切路由")
    subtitle_parts.append("Ctrl+T 切视图")
    subtitle_parts.append("Ctrl+R 同步")
    if effort_lock:
        subtitle_parts.append(f"预置档位 {effort_lock}")
    subtitle = " · ".join(subtitle_parts)

    rows: list[PickerRow] = []
    default_index = 1

    if view == "flat":
        name_width = min(30, max(18, *(len(v.display_name) for v in catalog.variants)))
        header_wide = f"{cell('模型变体', name_width)}  {cell('配额余量', 20)}  标记"
        header_narrow = f"{cell('模型变体', 26)}  标记"
        for idx, variant in enumerate(catalog.variants, 1):
            group = quota_group_for_model(variant.display_name)
            q_compact, q_detail = (
                format_quota_for_group(group, quota_snapshot, now=now)
                if route_cfg.name == "plan"
                else (route_cfg.label, f"按量计费 · GCP {route_cfg.project} ({route_cfg.location})")
            )
            badge = "默认" if variant.slug == default_slug else ""
            if badge:
                default_index = idx
            wide = f"{cell(variant.display_name, name_width)}  {cell(q_compact or '—', 20)}  {badge}".rstrip()
            narrow = f"{cell(variant.display_name, 26)}  {badge}".rstrip()
            detail_lines = [
                f"{variant.slug}  ({variant.display_name})",
                q_detail or f"路由：{route_cfg.label}",
                f"主题：{scheme}" + (f" · {freshness}" if freshness else ""),
            ]
            short_effort = EFFORT_SHORT.get(variant.variant_label, variant.variant_label)
            search_tokens = f"{variant.slug} {variant.display_name} {variant.family} {variant.variant_label} {short_effort}"
            rows.append(
                PickerRow(
                    key=f"slug:{variant.slug}",
                    wide_summary=wide,
                    narrow_summary=narrow,
                    details="\n".join(detail_lines),
                    search_text=search_tokens,
                    plain_label=variant.display_name,
                )
            )
    else:
        name_width = min(22, max(16, *(len(f.name) for f in catalog.families)))
        effort_width = 26
        header_wide = f"{cell('模型家族', name_width)}  {cell('推理档位 (Ctrl+H/L)', effort_width)}  {cell('配额余量', 18)}"
        header_narrow = f"{cell('模型家族', 20)}  {cell('档位', 10)}"
        default_variant = catalog.resolve_identifier(default_slug)
        default_family = default_variant.family if default_variant else catalog.families[0].name
        for idx, family in enumerate(catalog.families, 1):
            fam_key = f"{route_cfg.name}:{family.name}"
            preferred_label = efforts.get(fam_key, "")
            active_variant = family.variants[0]
            if preferred_label:
                for candidate in family.variants:
                    if candidate.variant_label == preferred_label or candidate.effort == preferred_label:
                        active_variant = candidate
                        break
            else:
                active_variant = next(
                    (v for v in family.variants if v.slug == family.default_variant_slug),
                    family.variants[0],
                )
            q_group = quota_group_for_model(active_variant.display_name)
            q_compact, q_detail = (
                format_quota_for_group(q_group, quota_snapshot, now=now)
                if route_cfg.name == "plan"
                else (route_cfg.label, f"按量计费 · GCP {route_cfg.project} ({route_cfg.location})")
            )
            badge = "默认" if family.name == default_family else ""
            if badge:
                default_index = idx
            bar_wide = _format_effort_bar(family, active_variant.variant_label, wide=True)
            bar_narrow = _format_effort_bar(family, active_variant.variant_label, wide=False)
            wide = (
                f"{cell(family.name, name_width)}  {cell(bar_wide, effort_width)}  "
                f"{cell(q_compact or '—', 18)}" + (f"  {badge}" if badge else "")
            ).rstrip()
            narrow = (
                f"{cell(family.name, 20)}  {cell(bar_narrow, 10)}" + (f"  {badge}" if badge else "")
            ).rstrip()
            avail = " / ".join(v.variant_label for v in family.variants)
            detail_lines = [
                f"{active_variant.slug}  ({active_variant.display_name})",
                (f"可选档位：{avail}（Ctrl+H/L 调节）" if family.adjustable else "固定配置（不可调档）"),
                (q_detail or f"路由：{route_cfg.label}")
                + f" · 主题 {scheme}"
                + (f" · {freshness}" if freshness else ""),
            ]
            search_tokens = " ".join([
                family.name,
                active_variant.slug,
                *(v.slug for v in family.variants),
                *(v.variant_label for v in family.variants),
                *(EFFORT_SHORT.get(v.variant_label, v.variant_label) for v in family.variants),
            ])
            rows.append(
                PickerRow(
                    key=f"family:{family.name}",
                    wide_summary=wide,
                    narrow_summary=narrow,
                    details="\n".join(detail_lines),
                    search_text=search_tokens,
                    plain_label=active_variant.display_name,
                )
            )

    return PickerPage(
        header_wide=header_wide,
        header_narrow=header_narrow,
        rows=tuple(rows),
        default_index=default_index,
        border_label=border_label,
        subtitle=subtitle,
        ghost="输入名称或档位，如 neon xh / opus low",
    )


def _write_label_files(ui_state: dict[str, Any], route_cfg: RouteConfig) -> None:
    Path(ui_state["family_label_file"]).write_text(
        f" Antigravity · {route_cfg.label} · 模型家族 ",
        encoding="utf-8",
    )
    Path(ui_state["flat_label_file"]).write_text(
        f" Antigravity · {route_cfg.label} · 全部变体 ",
        encoding="utf-8",
    )


def _load_ui_state(state_file: Path) -> dict[str, Any]:
    return json.loads(state_file.read_text(encoding="utf-8"))


def _save_ui_state(state_file: Path, ui_state: dict[str, Any]) -> None:
    atomic_write_json(state_file, ui_state)


def _socket_client_binary(env: dict[str, str] | None = None) -> str | None:
    environ = os.environ if env is None else env
    if environ.get("AIPANE_PICKER_NO_SOCKET") == "1":
        return None
    if sys.platform == "darwin" and os.access("/usr/bin/nc", os.X_OK):
        return "/usr/bin/nc"
    return None


class _PickerSession:
    """Shared state machine for both the warm socket server and subprocess helper."""

    def __init__(
        self,
        *,
        state_file: Path,
        ui_state: dict[str, Any],
        routes: dict[str, RouteConfig],
        env: dict[str, str],
        catalogs: dict[str, Catalog] | None = None,
        quota_snapshot: dict[str, Any] | None = None,
        quota_mtime_ns: int | None = None,
    ) -> None:
        self.state_file = state_file
        self.ui_state = ui_state
        self.routes = routes
        self.env = env
        self.catalogs: dict[str, Catalog] = dict(catalogs or {})
        self._quota_mtime_ns = quota_mtime_ns
        self._quota_snapshot = quota_snapshot
        self._quota_loaded = quota_snapshot is not None or quota_mtime_ns is not None

    def _route_cfg(self) -> RouteConfig:
        return self.routes.get(self.ui_state["route"]) or self.routes["plan"]

    def _current_quota(self) -> dict[str, Any] | None:
        qpath = quota_snapshot_path(self.env)
        try:
            mtime_ns: int | None = qpath.stat().st_mtime_ns
        except OSError:
            mtime_ns = None
        if not self._quota_loaded or mtime_ns != self._quota_mtime_ns:
            self._quota_snapshot = load_quota_snapshot(self.env) if mtime_ns is not None else None
            self._quota_mtime_ns = mtime_ns
            self._quota_loaded = True
        return self._quota_snapshot

    def _catalog(self, route_cfg: RouteConfig, *, force_refresh: bool = False) -> Catalog:
        if not force_refresh and route_cfg.name in self.catalogs:
            return self.catalogs[route_cfg.name]
        catalog = get_catalog_for_route(
            Path(self.ui_state["binary"]),
            route_cfg,
            base_env=self.env,
            settings_model=self.ui_state.get("settings_model", ""),
            remembered_slug=self.ui_state.get("default_by_route", {}).get(route_cfg.name, ""),
            force_refresh=force_refresh,
            allow_background_refresh=False,
        )
        self.catalogs[route_cfg.name] = catalog
        return catalog

    def _build_records(self, *, route_cfg: RouteConfig, catalog: Catalog, view: str) -> tuple[PickerPage, str]:
        page = build_picker_page(
            catalog,
            route_cfg=route_cfg,
            view=view,
            efforts=self.ui_state["efforts"],
            default_slug=self.ui_state["default_by_route"].get(route_cfg.name, ""),
            quota_snapshot=self._current_quota(),
            has_multiple_routes=len(self.routes) > 1,
            scheme=self.ui_state["scheme"],
            effort_lock=self.ui_state.get("effort_lock"),
        )
        return page, format_records(page)

    def _reload_action(self, *, view: str, records: str) -> str:
        raw_records_file = self.ui_state.get("records_file")
        if raw_records_file:
            target = Path(raw_records_file)
            tmp = target.with_suffix(".tmp")
            tmp.write_text(records, encoding="utf-8")
            os.replace(tmp, target)
            return f"reload-sync(cat {shlex.quote(str(target))})"
        helper_cmd = " ".join(shlex.quote(p) for p in self.ui_state["helper_command"])
        state_q = shlex.quote(str(self.state_file))
        return f"reload-sync({helper_cmd} render --state {state_q} --view {view})"

    def render(self, view: str | None = None) -> str:
        route_cfg = self._route_cfg()
        target_view = view or self.ui_state["view"]
        catalog = self._catalog(route_cfg, force_refresh=False)
        _, records = self._build_records(route_cfg=route_cfg, catalog=catalog, view=target_view)
        return records

    def refresh(self) -> str:
        route_cfg = self._route_cfg()
        catalog = self._catalog(route_cfg, force_refresh=True)
        view = "flat" if (
            Path(self.ui_state["manual_flat_flag"]).exists()
            or Path(self.ui_state["auto_flat_flag"]).exists()
        ) else "family"
        _, records = self._build_records(route_cfg=route_cfg, catalog=catalog, view=view)
        return records

    def step(self, key: str, delta: int, pos: str) -> str:
        if not key.startswith("family:"):
            return ""
        if (
            Path(self.ui_state["manual_flat_flag"]).exists()
            or Path(self.ui_state["auto_flat_flag"]).exists()
        ):
            return ""
        route_cfg = self._route_cfg()
        family_name = key.removeprefix("family:")
        catalog = self._catalog(route_cfg, force_refresh=False)
        family = catalog.by_family().get(family_name)
        if family is None or not family.adjustable:
            return ""
        fam_key = f"{route_cfg.name}:{family.name}"
        current_label = self.ui_state["efforts"].get(fam_key, "")
        labels = [v.variant_label for v in family.variants]
        if current_label in labels:
            idx = labels.index(current_label)
        else:
            default_v = next(
                (v for v in family.variants if v.slug == family.default_variant_slug),
                family.variants[0],
            )
            idx = labels.index(default_v.variant_label)
        next_label = labels[(idx + delta) % len(labels)]
        self.ui_state["efforts"][fam_key] = next_label
        _save_ui_state(self.state_file, self.ui_state)
        Path(self.ui_state["next_pos_file"]).write_text(f"{pos}\n", encoding="utf-8")
        _, records = self._build_records(route_cfg=route_cfg, catalog=catalog, view="family")
        return self._reload_action(view="family", records=records)

    def toggle_view(self, key: str, pos: str, query: str) -> str:
        del pos
        route_cfg = self._route_cfg()
        manual_flag = Path(self.ui_state["manual_flat_flag"])
        auto_flag = Path(self.ui_state["auto_flat_flag"])
        next_pos_file = Path(self.ui_state["next_pos_file"])
        currently_flat = manual_flag.exists() or auto_flag.exists()
        catalog = self._catalog(route_cfg, force_refresh=False)
        if currently_flat:
            manual_flag.unlink(missing_ok=True)
            auto_flag.unlink(missing_ok=True)
            self.ui_state["view"] = "family"
            target_pos = 1
            if key.startswith("slug:"):
                v = catalog.resolve_identifier(key.removeprefix("slug:"))
                if v is not None:
                    self.ui_state["efforts"][f"{route_cfg.name}:{v.family}"] = v.variant_label
                    for idx, fam in enumerate(catalog.families, 1):
                        if fam.name == v.family:
                            target_pos = idx
                            break
            _save_ui_state(self.state_file, self.ui_state)
            if not query.strip():
                next_pos_file.write_text(f"{target_pos}\n", encoding="utf-8")
            else:
                next_pos_file.unlink(missing_ok=True)
            label = Path(self.ui_state["family_label_file"]).read_text(encoding="utf-8")
            _, records = self._build_records(route_cfg=route_cfg, catalog=catalog, view="family")
            return self._reload_action(view="family", records=records) + f"+change-border-label({label})"

        manual_flag.write_text("1", encoding="utf-8")
        auto_flag.unlink(missing_ok=True)
        self.ui_state["view"] = "flat"
        target_pos = 1
        if key.startswith("family:"):
            fam_name = key.removeprefix("family:")
            pref = self.ui_state["efforts"].get(f"{route_cfg.name}:{fam_name}", "")
            v = catalog.resolve_family_and_effort(fam_name, pref)
            if v is not None:
                for idx, item in enumerate(catalog.variants, 1):
                    if item.slug == v.slug:
                        target_pos = idx
                        break
        _save_ui_state(self.state_file, self.ui_state)
        if not query.strip():
            next_pos_file.write_text(f"{target_pos}\n", encoding="utf-8")
        else:
            next_pos_file.unlink(missing_ok=True)
        label = Path(self.ui_state["flat_label_file"]).read_text(encoding="utf-8")
        _, records = self._build_records(route_cfg=route_cfg, catalog=catalog, view="flat")
        return self._reload_action(view="flat", records=records) + f"+change-border-label({label})"

    def toggle_route(self, query: str) -> str:
        route_cfg = self._route_cfg()
        names = list(self.routes.keys())
        if len(names) <= 1:
            return ""
        next_route = names[(names.index(route_cfg.name) + 1) % len(names)]
        next_cfg = self.routes[next_route]
        self.ui_state["route"] = next_route
        _write_label_files(self.ui_state, next_cfg)
        view = "flat" if (
            Path(self.ui_state["manual_flat_flag"]).exists()
            or Path(self.ui_state["auto_flat_flag"]).exists()
        ) else "family"
        self.ui_state["view"] = view
        _save_ui_state(self.state_file, self.ui_state)
        catalog = self._catalog(next_cfg, force_refresh=False)
        page, records = self._build_records(route_cfg=next_cfg, catalog=catalog, view=view)
        next_pos_file = Path(self.ui_state["next_pos_file"])
        if not query.strip():
            next_pos_file.write_text(f"{page.default_index}\n", encoding="utf-8")
        else:
            next_pos_file.unlink(missing_ok=True)
        label = (
            Path(self.ui_state["flat_label_file"]).read_text(encoding="utf-8")
            if view == "flat"
            else Path(self.ui_state["family_label_file"]).read_text(encoding="utf-8")
        )
        return self._reload_action(view=view, records=records) + f"+change-border-label({label})"

    def dispatch(self, raw_line: str) -> str:
        parts = raw_line.rstrip("\r\n").split("\x1f")
        if not parts or not parts[0]:
            return ""
        action = parts[0]
        if action == "step":
            key = parts[1] if len(parts) > 1 else ""
            delta = int(parts[2]) if len(parts) > 2 and parts[2] else 1
            pos = parts[3] if len(parts) > 3 and parts[3] else "1"
            return self.step(key, delta, pos)
        if action == "render":
            view = parts[1] if len(parts) > 1 and parts[1] else None
            return self.render(view)
        if action == "toggle-view":
            key = parts[1] if len(parts) > 1 else ""
            pos = parts[2] if len(parts) > 2 else "1"
            query = parts[3] if len(parts) > 3 else ""
            return self.toggle_view(key, pos, query)
        if action == "toggle-route":
            query = parts[1] if len(parts) > 1 else ""
            return self.toggle_route(query)
        if action == "refresh":
            return self.refresh()
        return ""


class _PickerServer:
    """In-process AF_UNIX callback server eliminating per-keystroke Python cold starts."""

    def __init__(self, session: _PickerSession, sock_path: Path, *, env: dict[str, str]) -> None:
        self._session = session
        self._requested_path = sock_path
        self._env = env
        self.socket_path: Path | None = None
        self.nc_executable: str | None = None
        self._sock: socket.socket | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def __enter__(self) -> _PickerServer:
        nc = _socket_client_binary(self._env)
        if not nc or len(str(self._requested_path).encode("utf-8")) >= 100:
            return self
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            srv.bind(str(self._requested_path))
            os.chmod(self._requested_path, 0o600)
            srv.listen(16)
            srv.settimeout(0.2)
        except OSError:
            srv.close()
            self._requested_path.unlink(missing_ok=True)
            return self
        self._sock = srv
        self.socket_path = self._requested_path
        self.nc_executable = nc
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()
        return self

    def _serve(self) -> None:
        assert self._sock is not None
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            with conn:
                conn.settimeout(1.0)
                chunks: list[bytes] = []
                total = 0
                try:
                    while total < 16384:
                        chunk = conn.recv(4096)
                        if not chunk:
                            break
                        chunks.append(chunk)
                        total += len(chunk)
                        if b"\n" in chunk:
                            break
                except OSError:
                    continue
                if not chunks:
                    continue
                request = b"".join(chunks).decode("utf-8", errors="replace")
                try:
                    response = self._session.dispatch(request)
                except Exception:
                    response = ""
                if response:
                    try:
                        conn.sendall(response.encode("utf-8"))
                    except OSError:
                        pass

    def __exit__(self, exc_type, exc, tb) -> None:
        self._stop.set()
        if self._sock is not None:
            if self.socket_path is not None:
                try:
                    wakeup = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    wakeup.settimeout(0.1)
                    wakeup.connect(str(self.socket_path))
                    wakeup.close()
                except OSError:
                    pass
            try:
                self._sock.close()
            except OSError:
                pass
        if self._thread is not None:
            self._thread.join(timeout=1.0)
        if self.socket_path is not None:
            self.socket_path.unlink(missing_ok=True)


def handle_helper_command(argv: list[str]) -> int:
    """Internal fzf action callbacks (`--_picker-helper <action> ...`)."""
    action = argv[0]
    state_file = Path(argv[argv.index("--state") + 1])
    ui_state = _load_ui_state(state_file)
    env = dict(os.environ)
    session = _PickerSession(
        state_file=state_file,
        ui_state=ui_state,
        routes=load_routes(env),
        env=env,
    )

    if action == "render":
        view = argv[argv.index("--view") + 1] if "--view" in argv else ui_state["view"]
        sys.stdout.write(session.render(view))
        return 0
    if action == "refresh":
        sys.stdout.write(session.refresh())
        return 0
    if action == "step":
        key = argv[argv.index("--key") + 1] if "--key" in argv else ""
        delta = int(argv[argv.index("--delta") + 1]) if "--delta" in argv else 1
        pos = argv[argv.index("--pos") + 1] if "--pos" in argv else "1"
        sys.stdout.write(session.step(key, delta, pos))
        return 0
    if action == "toggle-view":
        key = argv[argv.index("--key") + 1] if "--key" in argv else ""
        pos = argv[argv.index("--pos") + 1] if "--pos" in argv else "1"
        query = argv[argv.index("--query") + 1] if "--query" in argv else ""
        sys.stdout.write(session.toggle_view(key, pos, query))
        return 0
    if action == "toggle-route":
        query = argv[argv.index("--query") + 1] if "--query" in argv else ""
        sys.stdout.write(session.toggle_route(query))
        return 0

    raise ValueError(f"未知选择器动作: {action}")


def resolve_default_selection(
    *,
    routes: dict[str, RouteConfig],
    parsed: ParsedArgs,
    merged_user_settings: dict[str, Any],
    agy_settings: dict[str, Any],
    picker_state: dict[str, Any],
    session_model: str | None,
    session_route: str | None,
    env: dict[str, str],
) -> tuple[str, str]:
    """Resolve (initial_route, initial_model_identifier) via precedence lattice + 3-way reconciliation."""
    settings_model = str(agy_settings.get("model") or "").strip()
    last_seen = str(picker_state.get("last_seen_settings_model") or "").strip()

    # 1. Determine route priority: explicit flag > explicit env > session route > settings drift > remembered.
    env_route = (env.get("AIPANE_AGY_ROUTE") or "").strip().lower()
    settings_changed = bool(settings_model and last_seen and settings_model != last_seen)

    if parsed.route and parsed.route in routes:
        route = parsed.route
    elif env_route and env_route in routes and env.get("AIPANE_AUTO_RESUME") == "1":
        route = env_route
    elif session_route and session_route in routes:
        route = session_route
    elif settings_changed:
        route = "plan"
    else:
        remembered_route = str(picker_state.get("last_route") or "plan")
        route = remembered_route if remembered_route in routes else "plan"

    # 2. Determine model priority: --settings#model > AIPANE_AGY_DEFAULT_MODEL > session model > 3-way reconciled memory > settings.json.
    explicit_default = str(merged_user_settings.get("model") or "").strip()
    if not explicit_default:
        explicit_default = str(env.get("AIPANE_AGY_DEFAULT_MODEL") or "").strip()
    if explicit_default:
        return route, explicit_default
    if session_model:
        return route, session_model

    by_route = picker_state.get("last_model_by_route")
    remembered = str(by_route.get(route) or "").strip() if isinstance(by_route, dict) else ""

    if route == "plan" and settings_changed:
        return route, settings_model
    if remembered:
        return route, remembered
    if settings_model:
        return route, settings_model
    return route, ""


def run_interactive_picker(
    *,
    binary: Path,
    launcher_script: Path,
    routes: dict[str, RouteConfig],
    initial_route: str,
    initial_model_id: str,
    picker_state: dict[str, Any],
    settings_model: str,
    scheme: str,
    effort_lock: str | None,
    base_env: dict[str, str],
) -> tuple[RouteConfig, ModelVariant]:
    route_cfg = routes[initial_route]
    by_route = picker_state.get("last_model_by_route")
    remembered_for_route = by_route.get(route_cfg.name, "") if isinstance(by_route, dict) else ""
    catalog = get_catalog_for_route(
        binary,
        route_cfg,
        base_env=base_env,
        settings_model=settings_model,
        remembered_slug=remembered_for_route,
        launcher_script=launcher_script,
    )
    efforts: dict[str, str] = {}
    raw_efforts = picker_state.get("effort_by_family")
    if isinstance(raw_efforts, dict):
        efforts.update({str(k): str(v) for k, v in raw_efforts.items()})

    # Apply reconciled initial model's effort to its family for this session.
    resolved_initial = catalog.resolve_identifier(initial_model_id)
    if resolved_initial is None:
        resolved_initial = catalog.variants[0]
    if resolved_initial.variant_label and resolved_initial.variant_label != "fixed":
        efforts[f"{route_cfg.name}:{resolved_initial.family}"] = resolved_initial.variant_label

    # If caller passed --effort without --model, pre-position every supporting family to that effort.
    if effort_lock:
        canonical_lock = EFFORT_ALIASES.get(effort_lock.strip().casefold(), effort_lock.strip().casefold())
        for family in catalog.families:
            for v in family.variants:
                if v.effort == canonical_lock:
                    efforts[f"{route_cfg.name}:{family.name}"] = v.variant_label
                    break
        locked_initial = catalog.resolve_family_and_effort(resolved_initial.family, canonical_lock)
        if locked_initial is not None:
            resolved_initial = locked_initial

    default_by_route: dict[str, str] = {route_cfg.name: resolved_initial.slug}
    for name in routes:
        if name != route_cfg.name and isinstance(by_route, dict) and isinstance(by_route.get(name), str):
            default_by_route[name] = by_route[name]

    quota = load_quota_snapshot(base_env)
    page = build_picker_page(
        catalog,
        route_cfg=route_cfg,
        view="family",
        efforts=efforts,
        default_slug=resolved_initial.slug,
        quota_snapshot=quota,
        has_multiple_routes=len(routes) > 1,
        scheme=scheme,
        effort_lock=effort_lock,
    )

    with tempfile.TemporaryDirectory(prefix="agyp-") as tmp:
        os.chmod(tmp, 0o700)
        tmp_dir = Path(tmp)
        state_file = tmp_dir / "ui-state.json"
        auto_flag = tmp_dir / "auto_flat.flag"
        manual_flag = tmp_dir / "manual_flat.flag"
        family_label_file = tmp_dir / "label_family.txt"
        flat_label_file = tmp_dir / "label_flat.txt"
        next_pos_file = tmp_dir / "next_pos.txt"
        records_file = tmp_dir / "r.dat"
        sock_path = tmp_dir / "s"
        next_pos_file.write_text(f"{page.default_index}\n", encoding="utf-8")
        helper_command = (sys.executable, str(launcher_script), "--_picker-helper")
        ui_state: dict[str, Any] = {
            "route": route_cfg.name,
            "view": "family",
            "efforts": efforts,
            "default_by_route": default_by_route,
            "binary": str(binary),
            "settings_model": settings_model,
            "scheme": scheme,
            "effort_lock": effort_lock,
            "helper_command": list(helper_command),
            "auto_flat_flag": str(auto_flag),
            "manual_flat_flag": str(manual_flag),
            "family_label_file": str(family_label_file),
            "flat_label_file": str(flat_label_file),
            "next_pos_file": str(next_pos_file),
            "records_file": str(records_file),
        }
        _write_label_files(ui_state, route_cfg)
        _save_ui_state(state_file, ui_state)

        try:
            initial_quota_mtime: int | None = quota_snapshot_path(base_env).stat().st_mtime_ns
        except OSError:
            initial_quota_mtime = None
        session = _PickerSession(
            state_file=state_file,
            ui_state=ui_state,
            routes=routes,
            env=base_env,
            catalogs={route_cfg.name: catalog},
            quota_snapshot=quota,
            quota_mtime_ns=initial_quota_mtime,
        )

        # Plain-text fallback resolver: supports direct "neon xh" / "opus low", "g" route switch, "r" refresh.
        text_state = {"route": route_cfg.name, "catalog": catalog}

        def text_resolver(query: str) -> str | PickerPage | None:
            lowered = query.casefold()
            if lowered == "g" and len(routes) > 1:
                names = list(routes.keys())
                nxt = names[(names.index(text_state["route"]) + 1) % len(names)]
                text_state["route"] = nxt
                ui_state["route"] = nxt
                _save_ui_state(state_file, ui_state)
                nxt_cfg = routes[nxt]
                nxt_cat = get_catalog_for_route(
                    binary,
                    nxt_cfg,
                    base_env=base_env,
                    settings_model=settings_model,
                    remembered_slug=default_by_route.get(nxt, ""),
                    allow_background_refresh=False,
                )
                text_state["catalog"] = nxt_cat
                return build_picker_page(
                    nxt_cat,
                    route_cfg=nxt_cfg,
                    view="family",
                    efforts=ui_state["efforts"],
                    default_slug=default_by_route.get(nxt, ""),
                    quota_snapshot=quota,
                    has_multiple_routes=len(routes) > 1,
                    scheme=scheme,
                    effort_lock=effort_lock,
                )
            if lowered == "r":
                cur_cfg = routes[text_state["route"]]
                refreshed = get_catalog_for_route(
                    binary,
                    cur_cfg,
                    base_env=base_env,
                    settings_model=settings_model,
                    force_refresh=True,
                    allow_background_refresh=False,
                )
                text_state["catalog"] = refreshed
                return build_picker_page(
                    refreshed,
                    route_cfg=cur_cfg,
                    view="family",
                    efforts=ui_state["efforts"],
                    default_slug=default_by_route.get(cur_cfg.name, ""),
                    quota_snapshot=quota,
                    has_multiple_routes=len(routes) > 1,
                    scheme=scheme,
                    effort_lock=effort_lock,
                )
            tokens = lowered.split()
            if len(tokens) >= 2 or (len(tokens) == 1 and tokens[0] in EFFORT_ALIASES):
                cur_cat: Catalog = text_state["catalog"]
                matches = [
                    v for v in cur_cat.variants
                    if all(
                        tok in f"{v.slug} {v.display_name} {EFFORT_SHORT.get(v.variant_label, v.variant_label)}".casefold()
                        for tok in tokens
                    )
                ]
                if len(matches) == 1:
                    return f"slug:{matches[0].slug}"
            return None

        extra_text = "g 切路由 · r 同步" if len(routes) > 1 else "r 同步"
        with _PickerServer(session, sock_path, env=base_env) as server:
            bindings = DynamicBindings(
                helper_command=helper_command,
                state_file=state_file,
                auto_flat_flag=auto_flag,
                manual_flat_flag=manual_flag,
                family_label_file=family_label_file,
                flat_label_file=flat_label_file,
                next_pos_file=next_pos_file,
                socket_path=server.socket_path,
                nc_executable=server.nc_executable,
                allow_step=True,
                allow_view_toggle=True,
                allow_route_toggle=len(routes) > 1,
                allow_refresh=True,
            )
            selected_key = select(
                page,
                bindings=bindings,
                text_resolver=text_resolver,
                extra_text_prompt=extra_text,
            )
        final_ui = _load_ui_state(state_file)

    final_route_cfg = routes.get(final_ui["route"]) or route_cfg
    final_catalog = (
        catalog
        if final_route_cfg.name == route_cfg.name and load_cached_catalog(route_cfg.name, environment=base_env) is None
        else get_catalog_for_route(
            binary,
            final_route_cfg,
            base_env=base_env,
            settings_model=settings_model,
            allow_background_refresh=False,
        )
    )
    if selected_key.startswith("slug:"):
        chosen = final_catalog.resolve_identifier(selected_key.removeprefix("slug:"))
        if chosen is None:
            raise ValueError(f"所选模型无效: {selected_key}")
    elif selected_key.startswith("family:"):
        fam_name = selected_key.removeprefix("family:")
        pref = final_ui["efforts"].get(f"{final_route_cfg.name}:{fam_name}", "")
        chosen = final_catalog.resolve_family_and_effort(fam_name, pref)
        if chosen is None:
            raise ValueError(f"所选模型家族无效: {fam_name}")
    else:
        chosen = final_catalog.resolve_identifier(selected_key)
        if chosen is None:
            raise ValueError(f"无法识别所选模型: {selected_key}")
    return final_route_cfg, chosen


def canonicalize_explicit_model_args(
    model: str | None,
    effort: str | None,
) -> list[str]:
    """Emit legal agy --model/--effort args respecting v1.5.13 validation rules.

    Verified empirically: passing --effort alongside a display name that already
    encodes effort (e.g. 'Gemini 4 Neon (XHigh)') fails even when effort matches.
    """
    args: list[str] = []
    if model:
        args.extend(["--model", model])
        if effort and not agy_sessions.FIXED_VARIANT_SUFFIX.search(model):
            # Also avoid redundant --effort when model is already a variant slug ending in -<effort>.
            if not re.search(r"-(low|medium|high|xhigh|max)(-thinking)?$", model, re.I):
                args.extend(["--effort", effort])
    elif effort:
        args.extend(["--effort", effort])
    return args


def launch(argv: list[str], *, launcher_script: Path, env: dict[str, str] | None = None) -> int:
    base_env = dict(os.environ if env is None else env)
    if argv and argv[0] == "--_picker-helper":
        return handle_helper_command(argv[1:])
    if argv and argv[0] == "--_refresh-catalog":
        route_name = argv[1] if len(argv) > 1 else "plan"
        routes = load_routes(base_env)
        route_cfg = routes.get(route_name) or routes["plan"]
        binary = native_agy_binary(base_env)
        route_env, extra_args = build_route_environment(route_cfg, base_env)
        try:
            fetch_live_catalog(
                binary,
                route=route_cfg.name,
                extra_args=extra_args,
                env=route_env,
                environment_for_cache=base_env,
                persist=True,
            )
        except (OSError, ValueError, subprocess.SubprocessError):
            return 1
        return 0

    # Consume automatic-recovery marker before handing control to native agy.
    automatic_recovery = base_env.pop("AIPANE_AUTO_RESUME", "") == "1"
    parsed = parse_args(argv)
    binary = native_agy_binary(base_env)
    routes = load_routes(base_env)

    if parsed.route and parsed.route not in routes:
        available = ", ".join(routes.keys())
        raise ValueError(f"未知路由 {parsed.route!r}（当前可用: {available}）")

    # 1. Subcommands & meta flags (--help/--version/models/mcp/acp/update/...): passthrough.
    if parsed.passthrough:
        exec_env = base_env
        extra_args: list[str] = []
        if parsed.route:
            exec_env, extra_args = build_route_environment(routes[parsed.route], base_env)
        os.execve(str(binary), ["agy", *extra_args, *parsed.remaining], exec_env)
        return 0

    user_settings_list = [load_settings_arg(raw) for raw in parsed.settings_values]
    merged_user_settings: dict[str, Any] = {}
    for layer in user_settings_list:
        merged_user_settings = deep_merge_dict(merged_user_settings, layer)

    agy_settings = read_agy_settings(base_env)
    settings_model = str(agy_settings.get("model") or "").strip()
    scheme = detect_color_scheme(base_env)
    cwd = Path.cwd()
    home = Path(base_env.get("HOME", str(Path.home())))

    interactive = (
        sys.stdin.isatty()
        and sys.stdout.isatty()
        and not parsed.non_interactive_flag
    )

    session_id = parsed.conversation_id
    if not session_id and parsed.continue_session:
        session_id = resolve_continue_conversation_id(cwd, base_env)
    session_model, session_route = resolve_session_model(session_id or "", base_env)

    inherited_dirs = inherit_skill_dirs(cwd, home, parsed.add_dirs)

    # 2. Automatic recovery or explicit --model or non-interactive mode: skip picker.
    if automatic_recovery or parsed.model is not None or not interactive:
        env_route = (base_env.get("AIPANE_AGY_ROUTE") or "").strip().lower()
        if parsed.route:
            route_name = parsed.route
        elif automatic_recovery and env_route in routes:
            route_name = env_route
        elif session_route in routes:
            route_name = session_route
        elif env_route in routes and base_env.get("AGY_ADC_AUTH") == "true":
            route_name = env_route
        else:
            route_name = "plan"
        route_cfg = routes[route_name]
        exec_env, _ = build_route_environment(route_cfg, base_env)
        composed = compose_settings(
            scheme=scheme,
            route_cfg=route_cfg,
            user_settings_list=user_settings_list,
        )
        chosen_model = parsed.model
        chosen_effort = parsed.effort
        if automatic_recovery and chosen_model is None and session_model:
            chosen_model = session_model
        model_args = canonicalize_explicit_model_args(chosen_model, chosen_effort)
        final_argv = [
            "agy",
            *inherited_dirs,
            "--settings",
            json.dumps(composed, ensure_ascii=False, separators=(",", ":")),
            *model_args,
            *parsed.remaining,
        ]
        os.execve(str(binary), final_argv, exec_env)
        return 0

    # 3. Interactive pick path.
    picker_state = load_picker_state(base_env)
    initial_route, initial_model_id = resolve_default_selection(
        routes=routes,
        parsed=parsed,
        merged_user_settings=merged_user_settings,
        agy_settings=agy_settings,
        picker_state=picker_state,
        session_model=session_model,
        session_route=session_route,
        env=base_env,
    )
    route_cfg, chosen = run_interactive_picker(
        binary=binary,
        launcher_script=launcher_script,
        routes=routes,
        initial_route=initial_route,
        initial_model_id=initial_model_id,
        picker_state=picker_state,
        settings_model=settings_model,
        scheme=scheme,
        effort_lock=parsed.effort,
        base_env=base_env,
    )
    save_picker_state(
        state=picker_state,
        route=route_cfg.name,
        variant=chosen,
        settings_model=settings_model,
        environment=base_env,
    )
    print(
        f"启动 {chosen.display_name} · {route_cfg.label} · {scheme}",
        file=sys.stderr,
    )
    exec_env, _ = build_route_environment(route_cfg, base_env)
    composed = compose_settings(
        scheme=scheme,
        route_cfg=route_cfg,
        user_settings_list=user_settings_list,
    )
    # Variant slug uniquely encodes both family and effort; omit --effort to
    # avoid native agy fixed-variant conflict errors.
    final_argv = [
        "agy",
        *inherited_dirs,
        "--settings",
        json.dumps(composed, ensure_ascii=False, separators=(",", ":")),
        "--model",
        chosen.slug,
        *parsed.remaining,
    ]
    os.execve(str(binary), final_argv, exec_env)
    return 0
