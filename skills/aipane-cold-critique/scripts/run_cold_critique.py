#!/usr/bin/env python3
"""
零前情双边对抗冷审执行脚本（零第三方依赖，仅使用 Python 3.9+ 标准库）。

支持并发调用与 aipane 对齐的 7 个本地编程 Agent CLI 及任意 OpenAI 兼容 API
（所有 CLI 均在每次新建的独立空临时目录中运行）：
  - claude / grok / pi / dsh（别名 dsh-tui） / openai: 硬禁工具
  - codex / agy（别名 antigravity） / kimi: 沙箱/配置隔离 + 提示词约束

可通过 COLD_CRITIQUE_CHANNELS 环境变量配置默认通道列表（逗号或分号分隔）。
"""

from __future__ import annotations

import argparse
import concurrent.futures
import contextlib
import dataclasses
import hashlib
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from typing import Any, Callable

try:
    import tomllib  # Python 3.11+
except ImportError:  # pragma: no cover - Python 3.9/3.10 fallback
    tomllib = None  # type: ignore[assignment]

PROMPT_PROFILES: dict[str, str] = {
    "summary": (
        "仅基于下方这段文字做纯文本分析（不要调用任何工具、不要查阅本机文件或历史记录）。\n\n"
        "找找这段总结里，哪几个概念合并是硬凑的、哪些因果链条经不起推敲，给出最尖锐的反驳；"
        "如果某部分经得起推敲就直接说明成立，不要为了挑刺而强行编造缺陷。\n\n"
        "---\n\n"
    ),
    "article": (
        "仅基于下方这篇文章做纯文本分析（不要调用任何工具、不要查阅本机文件或历史记录）。\n\n"
        "找找这篇文章里，哪几个概念合并是硬凑的、哪些因果链条经不起推敲、哪些地方遗漏了关键隐含前提、"
        "哪些跨学科概念借用或类比越过了原理论的适用边界，以及标题或开头结论是否说得比正文证据更满，给出最尖锐的反驳；"
        "如果某部分经得起推敲就直接说明成立，不要为了挑刺而强行编造缺陷。\n\n"
        "---\n\n"
    ),
    "code": (
        "仅基于下方提供的代码、设计或对照规范做纯文本分析（不要调用任何工具、不要查阅本机文件或历史记录）。\n\n"
        "找找这套实现或方案里，哪些抽象或状态边界耦合过紧、哪些故障路径与并发/数据一致性前提经不起推敲、"
        "哪些重构建议属于过度设计或脱离当前规模边界，给出最尖锐的反驳；"
        "如果某部分设计经得起推敲或已匹配当前规模就直接说明成立，不要为了挑刺而强行编造缺陷。\n\n"
        "---\n\n"
    ),
}
CALIBRATED_PROMPT_PREFIX = PROMPT_PROFILES["summary"]

DEFAULT_OPENAI_BASE_URL = "https://api.openai.com/v1"
MODEL_CACHE_TTL_SECONDS = 24 * 3600
MODEL_PROBE_TIMEOUT_SECONDS = 8
MODEL_SUMMARY_LIMIT = 8
ARGV_WARN_BYTES_WINDOWS = 7500
ARGV_WARN_BYTES_POSIX = 100_000
STD_EFFORTS = ("low", "medium", "high", "xhigh", "max")
DEFAULT_TIMEOUT_SECONDS = 300
EFFORT_DEFAULT_TIMEOUTS: dict[str, int] = {
    "xhigh": 600,
    "max": 900,
}

CODEX_DISABLED_FEATURES: tuple[str, ...] = (
    "hooks",
    "plugins",
    "apps",
    "multi_agent",
    "shell_tool",
    "skill_search",
)
CODEX_ISOLATION_OVERRIDES: tuple[str, ...] = (
    'developer_instructions=""',
    'model_verbosity="medium"',
    "notify=[]",
    "check_for_update_on_startup=false",
)
CLAUDE_DISABLE_HOOKS_SETTINGS = json.dumps({"disableAllHooks": True}, separators=(",", ":"))


@dataclasses.dataclass(frozen=True)
class BackendInfo:
    name: str
    kind: str  # "cli" | "http"
    binary: str | None
    isolation_level: str  # "hard" | "sandbox"
    isolation_summary: str
    supports_effort: bool
    effort_values: tuple[str, ...]
    input_mode: str  # "stdin" | "prompt_file" | "argv" | "http_json"
    aliases: tuple[str, ...] = ()


BACKEND_REGISTRY: tuple[BackendInfo, ...] = (
    BackendInfo(
        "codex", "cli", "codex", "sandbox",
        "沙箱/配置隔离（-s read-only --ephemeral + 禁 shell_tool/hooks/plugins/apps/MCP + 清空个人指令）",
        True, STD_EFFORTS, "stdin",
    ),
    BackendInfo(
        "agy", "cli", "agy", "sandbox",
        "沙箱/提示词约束（--sandbox --disable-slash-commands + stream-json stdin + 空目录）",
        True, STD_EFFORTS, "stdin", ("antigravity",),
    ),
    BackendInfo(
        "claude", "cli", "claude", "hard",
        '硬禁工具（--tools "" + --strict-mcp-config + --no-session-persistence + disableAllHooks）',
        True, STD_EFFORTS, "stdin",
    ),
    BackendInfo(
        "grok", "cli", "grok", "hard",
        '硬禁工具（--tools "" + --no-memory --no-subagents --disable-web-search）',
        True, STD_EFFORTS, "prompt_file",
    ),
    BackendInfo(
        "dsh", "cli", "dsh", "hard",
        "硬禁工具（dsh --profile headless + --permission-mode read-only + 临时 --patch 禁用工具插件）",
        False, (), "stdin", ("dsh-tui",),
    ),
    BackendInfo(
        "pi", "cli", "pi", "hard",
        "硬禁工具（--no-tools --no-mcp --no-extensions --no-skills --no-context-files --no-session）",
        True, ("off", "minimal") + STD_EFFORTS, "stdin",
    ),
    BackendInfo(
        "kimi", "cli", "kimi", "sandbox",
        "沙箱/提示词约束（空目录 + 空 --skills-dir + 不传 --auto/--yolo）",
        False, (), "argv",
    ),
    BackendInfo(
        "openai", "http", None, "hard",
        "硬禁工具（纯文本 Chat Completions API，无 tools 字段）",
        True, STD_EFFORTS[:4], "http_json",
    ),
)

BACKEND_MAP: dict[str, BackendInfo] = {b.name: b for b in BACKEND_REGISTRY}
SUPPORTED_BACKENDS: tuple[str, ...] = tuple(b.name for b in BACKEND_REGISTRY)
BACKEND_ALIASES: dict[str, str] = {a: b.name for b in BACKEND_REGISTRY for a in b.aliases}

DSH_DISABLED_PLUGIN_IDS: tuple[str, ...] = (
    "tool-bash", "tool-pwsh", "tool-jobs", "tool-fs", "tool-fs-search",
    "tool-skill", "tool-subagent", "tool-subagent-fork", "tool-subagent-control",
    "tool-workflow", "tool-working-directory", "tool-todo", "tool-goal",
    "tool-web", "mcp-resources", "agent-instructions", "skill-filesystem",
)

_TRAILING_SECTION_RE = re.compile(
    r"\n##\s+(?:参考资料|参考文献|发布前检查|References|Further Reading|Checklist|Pre-publish Checklist)\b",
    re.IGNORECASE,
)
_MD_LINK_RE = re.compile(r"(?<!!)\[([^\]]+)\]\(https?://[^)]+\)")
_MD_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\(https?://[^)]+\)")
_HOME_PATH_RE = re.compile(r"(?:/Users/[^/\s`'\"<>]+|/home/[^/\s`'\"<>]+|[A-Za-z]:\\Users\\[^\\\s`'\"<>]+)")
_ENV_PREFIX_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
_EFFORT_RE = re.compile(r"^[a-zA-Z0-9_-]+$")
_TOML_STRING_KV_RE = re.compile(r'^\s*([A-Za-z0-9_.-]+)\s*=\s*"([^"]*)"')
_TOML_BOOL_KV_RE = re.compile(r"^\s*([A-Za-z0-9_.-]+)\s*=\s*(true|false)\b", re.IGNORECASE)
_TOML_SECTION_RE = re.compile(r"^\s*\[([^\]]+)\]\s*$")
_SAFE_TOML_KEY_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_SAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


@dataclasses.dataclass(frozen=True)
class ChannelSpec:
    raw: str
    backend: str
    model: str | None = None
    effort: str | None = None
    env_prefix: str | None = None

    @property
    def label(self) -> str:
        return (
            self.backend
            + (f":{self.model}" if self.model else "")
            + (f"@{self.effort}" if self.effort else "")
            + (f"#{self.env_prefix}" if self.env_prefix else "")
        )


def normalize_backend_name(name: str) -> str:
    cleaned = name.strip().lower()
    return BACKEND_ALIASES.get(cleaned, cleaned)


def strip_markdown_shell(text: str, redact_paths: bool = False) -> str:
    """剥离 YAML frontmatter、文末参考/检查章节、Markdown 外链 URL，并可选脱敏本机主目录路径。"""
    normalized = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if normalized.startswith("---\n"):
        end_idx = normalized.find("\n---\n", 4)
        if end_idx != -1:
            normalized = normalized[end_idx + 5 :].strip()
        elif normalized.endswith("\n---"):
            normalized = ""
    normalized = _TRAILING_SECTION_RE.split(normalized, maxsplit=1)[0].strip()
    normalized = _MD_IMAGE_RE.sub(lambda m: f"[图片: {m.group(1)}]" if m.group(1) else "[图片]", normalized)
    normalized = _MD_LINK_RE.sub(r"\1", normalized)
    if redact_paths:
        normalized = _HOME_PATH_RE.sub("<HOME>", normalized)
    return normalized.strip()


def parse_channel_spec(raw: str) -> ChannelSpec:
    """解析 `<backend>[:<model>][@<effort>][#<ENV_PREFIX>]` 格式的通道声明。"""
    spec = raw.strip()
    if not spec:
        raise ValueError("通道不能为空。")
    env_prefix: str | None = None
    effort: str | None = None

    if "#" in spec:
        spec, candidate = spec.rsplit("#", 1)
        if not _ENV_PREFIX_RE.match(candidate):
            raise ValueError(f"无效的环境变量前缀 '{candidate}'（需大写字母、数字或下划线）。")
        env_prefix = candidate
    if "@" in spec:
        spec, candidate = spec.rsplit("@", 1)
        if not _EFFORT_RE.match(candidate):
            raise ValueError(f"无效的推理强度标记 '@{candidate}'。")
        effort = candidate

    backend_raw, model = (spec.split(":", 1) if ":" in spec else (spec, None))
    model = model.strip() or None if model is not None else None
    backend = normalize_backend_name(backend_raw)
    if backend not in BACKEND_MAP:
        allowed = ", ".join(SUPPORTED_BACKENDS)
        raise ValueError(f"不支持的后端 '{backend_raw.strip()}'，可选：{allowed}（别名：dsh-tui→dsh，antigravity→agy）。")
    if backend != "openai" and env_prefix is not None:
        raise ValueError(f"只有 openai 后端支持 '#{env_prefix}' 环境变量前缀。")
    if effort is not None and not BACKEND_MAP[backend].supports_effort:
        raise ValueError(
            f"后端 '{backend}' 不支持命令行推理强度参数 '@{effort}'，请去掉 '@{effort}' 后重试（或在该工具自身配置中调整）。"
        )
    return ChannelSpec(raw=raw, backend=backend, model=model, effort=effort, env_prefix=env_prefix)


def resolve_channel_timeout(spec: ChannelSpec, explicit_timeout: int | None = None) -> int:
    """按显式参数或推理强度决定单通道超时（秒）。"""
    if explicit_timeout is not None:
        return explicit_timeout
    effort = (spec.effort or "").strip().lower()
    if effort in EFFORT_DEFAULT_TIMEOUTS:
        return EFFORT_DEFAULT_TIMEOUTS[effort]
    model_lower = (spec.model or "").strip().lower()
    for suffix, sec in EFFORT_DEFAULT_TIMEOUTS.items():
        if model_lower.endswith(f"-{suffix}"):
            return sec
    return DEFAULT_TIMEOUT_SECONDS


def resolve_model_shorthand(spec: ChannelSpec, candidates: list[str]) -> ChannelSpec:
    """基于本地候选模型列表做唯一简写解析；多命中报错，零命中原样放行以兼容新上线模型。"""
    if not spec.model or spec.backend == "openai" or not candidates:
        return spec
    query = spec.model.strip()
    if query in candidates:
        return spec
    lower_map = {c.lower(): c for c in candidates}
    if query.lower() in lower_map:
        return dataclasses.replace(spec, model=lower_map[query.lower()])

    # 1. If effort is present, prefer candidates ending with <query>-<effort> (e.g., short@high -> vendor-short-high)
    if spec.effort:
        eff_q = f"{query}-{spec.effort}".lower()
        eff_pat = re.compile(rf"(?:^|[/\-_.]){re.escape(eff_q)}$", re.IGNORECASE)
        eff_hits = [c for c in candidates if eff_pat.search(c)]
        if len(eff_hits) == 1:
            return dataclasses.replace(spec, model=eff_hits[0])

    # 2. Boundary-aligned suffix match (e.g., pro -> vendor-pro, v2 -> org/model-v2)
    suffix_pat = re.compile(rf"(?:^|[/\-_.]){re.escape(query)}$", re.IGNORECASE)
    suffix_hits = [c for c in candidates if suffix_pat.search(c)]
    if len(suffix_hits) == 1:
        return dataclasses.replace(spec, model=suffix_hits[0])

    # 3. General case-insensitive substring match, optionally narrowed by -<effort>
    sub_hits = [c for c in candidates if query.lower() in c.lower()]
    if len(sub_hits) == 1:
        return dataclasses.replace(spec, model=sub_hits[0])
    if len(sub_hits) > 1 and spec.effort:
        eff_narrow = [c for c in sub_hits if c.lower().endswith(f"-{spec.effort.lower()}")]
        if len(eff_narrow) == 1:
            return dataclasses.replace(spec, model=eff_narrow[0])

    ambiguous = suffix_hits if len(suffix_hits) > 1 else sub_hits
    if len(ambiguous) > 1:
        preview = ", ".join(ambiguous[:8])
        more = f" 等共 {len(ambiguous)} 个" if len(ambiguous) > 8 else ""
        raise ValueError(
            f"通道 '{spec.raw}' 中的模型简写 '{query}' 在后端 '{spec.backend}' 匹配到多个候选模型："
            f"{preview}{more}。请写更精确的模型 ID（或用 --list-models {spec.backend} 查看）。"
        )
    return spec


def resolve_channel_specs(
    specs: list[ChannelSpec],
    *,
    env: dict[str, str] | None = None,
    home: pathlib.Path | None = None,
    allow_live_probe: bool = False,
) -> list[ChannelSpec]:
    """批量对指定了 model 的通道执行本地唯一简写解析。"""
    cache: dict[str, list[str]] = {}
    resolved: list[ChannelSpec] = []
    for spec in specs:
        if not spec.model or spec.backend == "openai":
            resolved.append(spec)
            continue
        if spec.backend not in cache:
            info = BACKEND_MAP[spec.backend]
            exe = shutil.which(info.binary) if info.binary else None
            meta = probe_backend_models(
                spec.backend, executable=exe, env=env, home=home, allow_live_probe=allow_live_probe
            )
            cache[spec.backend] = list(meta.get("models") or [])
        resolved.append(resolve_model_shorthand(spec, cache[spec.backend]))
    return resolved


def resolve_openai_env(spec: ChannelSpec, env: dict[str, str] | None = None) -> tuple[str, str, str]:
    """解析 OpenAI 兼容后端所需的 (base_url, api_key, model)。"""
    src = os.environ if env is None else env
    if spec.env_prefix:
        p = spec.env_prefix
        base_url = src.get(f"{p}_BASE_URL", "").strip() or DEFAULT_OPENAI_BASE_URL
        api_key = src.get(f"{p}_API_KEY", "").strip()
        model = spec.model or src.get(f"{p}_MODEL", "").strip()
        key_hint, model_hint = f"{p}_API_KEY", f"openai:<model>#{p} 或设置 {p}_MODEL"
    else:
        base_url = src.get("COLD_CRITIQUE_BASE_URL", "").strip() or src.get("OPENAI_BASE_URL", "").strip() or DEFAULT_OPENAI_BASE_URL
        api_key = src.get("COLD_CRITIQUE_API_KEY", "").strip() or src.get("OPENAI_API_KEY", "").strip()
        model = spec.model or src.get("COLD_CRITIQUE_OPENAI_MODEL", "").strip() or src.get("OPENAI_MODEL", "").strip()
        key_hint = "COLD_CRITIQUE_API_KEY / OPENAI_API_KEY"
        model_hint = "openai:<model> 或设置 COLD_CRITIQUE_OPENAI_MODEL / OPENAI_MODEL"
    if not api_key:
        raise ValueError(f"缺少 API 密钥，请设置环境变量 {key_hint}。")
    if not model:
        raise ValueError(f"缺少模型名称，请指定 {model_hint}。")
    return base_url.rstrip("/"), api_key, model


def resolve_cli_executable(binary: str, resolved: str | None = None) -> str:
    return resolved or shutil.which(binary) or binary


def check_argv_prompt_warnings(specs: list[ChannelSpec], full_prompt: str) -> list[str]:
    """对通过命令行参数传递正文的后端做跨平台长度预警。"""
    prompt_bytes = len(full_prompt.encode("utf-8"))
    limit = ARGV_WARN_BYTES_WINDOWS if os.name == "nt" else ARGV_WARN_BYTES_POSIX
    if prompt_bytes <= limit:
        return []
    seen: set[str] = set()
    warnings: list[str] = []
    for spec in specs:
        if BACKEND_MAP[spec.backend].input_mode == "argv" and spec.backend not in seen:
            seen.add(spec.backend)
            warnings.append(
                f"通道 '{spec.backend}' 通过命令行参数传递正文，当前长度 {prompt_bytes} 字节已超过安全阈值 {limit} 字节，"
                "可能触发操作系统参数长度限制；超长文本建议改用支持 stdin/文件的后端（codex/agy/claude/grok/dsh/pi/openai）。"
            )
    return warnings


# ---------------------------------------------------------------------------
# 本地配置探测与混合 24h TTL 模型缓存（只读公开配置，不读取凭证/密钥）
# ---------------------------------------------------------------------------


def _clean_str(val: Any) -> str | None:
    return val.strip() or None if isinstance(val, str) else None


def _dedupe(items: list[str | None]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        s = (item or "").strip()
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out


def _normalize_slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.strip().lower()).strip("-")


def _probe_meta(
    default_model: str | None = None,
    default_effort: str | None = None,
    models: list[str | None] | None = None,
    model_source: str = "none",
) -> dict[str, Any]:
    ordered = ([default_model] if default_model else []) + list(models or [])
    return {
        "default_model": default_model,
        "default_effort": default_effort,
        "models": _dedupe(ordered),
        "model_source": model_source,
    }


def get_cache_file_path(env: dict[str, str] | None = None, home: pathlib.Path | None = None) -> pathlib.Path:
    src = os.environ if env is None else env
    if custom := src.get("COLD_CRITIQUE_CACHE_DIR", "").strip():
        return pathlib.Path(custom) / "models_cache.json"
    home_dir = home if home is not None else pathlib.Path.home()
    if os.name == "nt" and (win_base := src.get("LOCALAPPDATA") or src.get("APPDATA")):
        return pathlib.Path(win_base) / "aipane-cold-critique" / "models_cache.json"
    if xdg := src.get("XDG_CACHE_HOME", "").strip():
        return pathlib.Path(xdg) / "aipane-cold-critique" / "models_cache.json"
    return home_dir / ".cache" / "aipane-cold-critique" / "models_cache.json"


def _read_json(path: pathlib.Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="replace")) if path.is_file() else None
    except Exception:
        return None


def _atomic_write_json(path: pathlib.Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _parse_toml(path: pathlib.Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    text = path.read_text(encoding="utf-8", errors="replace")
    if tomllib is not None:
        try:
            parsed = tomllib.loads(text)
            return parsed if isinstance(parsed, dict) else None
        except Exception:
            return None
    result: dict[str, Any] = {}
    section: str | None = None
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if m := _TOML_SECTION_RE.match(s):
            section = m.group(1).strip()
            result.setdefault(section, {})
        elif m := _TOML_STRING_KV_RE.match(s):
            target = result if section is None else result.setdefault(section, {})
            if isinstance(target, dict):
                target[m.group(1)] = m.group(2)
        elif m := _TOML_BOOL_KV_RE.match(s):
            target = result if section is None else result.setdefault(section, {})
            if isinstance(target, dict):
                target[m.group(1)] = m.group(2).lower() == "true"
    return result


def list_codex_enabled_mcp_servers(home: pathlib.Path | None = None) -> list[str]:
    """读取 ~/.codex/config.toml 中未显式禁用的 MCP server 名称，用于在冷审时逐项关闭。"""
    home_dir = home if home is not None else pathlib.Path.home()
    cfg = _parse_toml(home_dir / ".codex" / "config.toml")
    if not isinstance(cfg, dict):
        return []
    names: list[str] = []
    mcp_table = cfg.get("mcp_servers")
    if isinstance(mcp_table, dict):
        for name, entry in mcp_table.items():
            if isinstance(name, str) and isinstance(entry, dict) and entry.get("enabled") is not False:
                names.append(name)
    else:
        for sec_key, entry in cfg.items():
            if isinstance(sec_key, str) and sec_key.startswith("mcp_servers."):
                parts = sec_key.split(".", 2)
                if len(parts) == 2 and isinstance(entry, dict) and entry.get("enabled") is not False:
                    names.append(parts[1].strip('"').strip("'"))
    return _dedupe(names)


def _load_ttl_cache(path: pathlib.Path) -> dict[str, Any]:
    data = _read_json(path)
    return data if isinstance(data, dict) and isinstance(data.get("backends"), dict) else {"version": 1, "backends": {}}


def _save_ttl_cache(path: pathlib.Path, data: dict[str, Any]) -> None:
    with contextlib.suppress(Exception):
        _atomic_write_json(path, data)


def _probe_codex_local(home: pathlib.Path) -> dict[str, Any]:
    cfg = _parse_toml(home / ".codex" / "config.toml") or {}
    default_model, default_effort = _clean_str(cfg.get("model")), _clean_str(cfg.get("model_reasoning_effort"))
    cache = _read_json(home / ".codex" / "models_cache.json")
    models = [
        _clean_str(e.get("slug") or e.get("id"))
        for e in (cache.get("models") if isinstance(cache, dict) and isinstance(cache.get("models"), list) else [])
        if isinstance(e, dict)
    ]
    source = "local_cache" if cache else ("local_config" if default_model else "none")
    return _probe_meta(default_model, default_effort, models, source)


def _probe_claude_local(home: pathlib.Path) -> dict[str, Any]:
    cfg = _read_json(home / ".claude" / "settings.json")
    cfg_dict = cfg if isinstance(cfg, dict) else {}
    env_map = cfg_dict.get("env") if isinstance(cfg_dict.get("env"), dict) else {}

    picker_models: list[str | None] = []
    picker = cfg_dict.get("modelPicker")
    if isinstance(picker, dict) and isinstance(picker.get("options"), list):
        picker_models = [_clean_str(opt.get("model")) for opt in picker["options"] if isinstance(opt, dict)]

    settings_models: list[str | None] = []
    if isinstance(cfg_dict.get("modelSettings"), dict):
        settings_models = [_clean_str(k) for k in cfg_dict["modelSettings"].keys()]

    env_models: list[str | None] = [
        _clean_str(env_map.get(k))
        for k in ("ANTHROPIC_DEFAULT_OPUS_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL", "ANTHROPIC_DEFAULT_HAIKU_MODEL", "ANTHROPIC_DEFAULT_FABLE_MODEL")
    ]
    fallback_avail: list[str | None] = []
    if not any(picker_models) and not any(settings_models) and isinstance(cfg_dict.get("availableModels"), list):
        fallback_avail = [_clean_str(m) for m in cfg_dict["availableModels"]]

    models: list[str | None] = picker_models + settings_models + env_models + fallback_avail + ["opus", "sonnet", "haiku"]
    return _probe_meta(
        _clean_str(cfg_dict.get("model")),
        _clean_str(cfg_dict.get("effortLevel")),
        models,
        "local_config" if cfg else "builtin_aliases",
    )


def _probe_grok_local(home: pathlib.Path) -> dict[str, Any]:
    cfg = _parse_toml(home / ".grok" / "config.toml") or {}
    models_sec = cfg.get("models") if isinstance(cfg.get("models"), dict) else {}
    default_model = _clean_str(models_sec.get("default"))
    default_effort = _clean_str(models_sec.get("default_reasoning_effort"))

    aliases: list[str | None] = []
    if isinstance(cfg.get("model"), dict):
        aliases.extend(_clean_str(k) for k in cfg["model"].keys())
    else:
        aliases.extend(_clean_str(k.split(".", 1)[1]) for k in cfg if isinstance(k, str) and k.startswith("model."))

    cache = _read_json(home / ".grok" / "models_cache.json")
    cached: list[str | None] = []
    if isinstance(cache, dict):
        raw = cache.get("models")
        if isinstance(raw, list):
            for e in raw:
                if isinstance(e, dict):
                    mid = _clean_str(e.get("id") or e.get("name") or e.get("model"))
                    cached.append(mid)
                    if e.get("is_default") and not default_model:
                        default_model = mid
                elif isinstance(e, str):
                    cached.append(_clean_str(e))
        elif isinstance(raw, dict):
            cached.extend(_clean_str(k) for k in raw.keys())
        default_model = default_model or _clean_str(cache.get("default_model"))

    merged = _dedupe(([default_model] if default_model else []) + cached + aliases)
    default_model = default_model or (merged[0] if merged else None)
    source = "local_cache" if any(cached) else ("local_config" if merged else "none")
    return _probe_meta(default_model, default_effort, merged, source)


def _probe_pi_local(home: pathlib.Path) -> dict[str, Any]:
    settings = _read_json(home / ".pi" / "agent" / "settings.json")
    s_dict = settings if isinstance(settings, dict) else {}
    prov, mod = _clean_str(s_dict.get("defaultProvider")), _clean_str(s_dict.get("defaultModel"))
    default_model = (f"{prov}/{mod}" if prov and "/" not in mod else mod) if mod else None
    default_effort = _clean_str(s_dict.get("defaultThinkingLevel") or s_dict.get("thinkingLevel"))

    doc = _read_json(home / ".pi" / "agent" / "models.json")
    providers = doc.get("providers") if isinstance(doc, dict) and isinstance(doc.get("providers"), dict) else {}
    multi = len(providers) > 1
    models: list[str | None] = []
    for p_name, p_cfg in providers.items():
        if isinstance(p_cfg, dict) and isinstance(p_cfg.get("models"), list):
            for e in p_cfg["models"]:
                if isinstance(e, dict) and (mid := _clean_str(e.get("id"))):
                    models.append(f"{p_name}/{mid}" if multi else mid)
    return _probe_meta(default_model, default_effort, models, "local_config" if (models or default_model) else "none")


def _probe_kimi_local(home: pathlib.Path) -> dict[str, Any]:
    cfg_path = home / ".kimi-code" / "config.toml"
    cfg = _parse_toml(cfg_path if cfg_path.is_file() else home / ".kimi" / "config.toml") or {}
    default_model = _clean_str(cfg.get("default_model"))
    thinking = cfg.get("thinking") if isinstance(cfg.get("thinking"), dict) else {}
    default_effort = _clean_str(thinking.get("effort"))
    if isinstance(cfg.get("models"), dict):
        models: list[str | None] = [_clean_str(k) for k in cfg["models"].keys()]
    else:
        models = [
            _clean_str(k.split(".", 1)[1].strip().strip('"').strip("'"))
            for k in cfg
            if isinstance(k, str) and k.startswith("models.")
        ]
    return _probe_meta(default_model, default_effort, models, "local_config" if (models or default_model) else "none")


def _probe_dsh_local(home: pathlib.Path) -> dict[str, Any]:
    provider, model = "deepseek-official", "deepseek-flash"
    patch_path = home / ".dsh" / "profiles" / "headless" / "cordis.patch.yml"
    if patch_path.is_file():
        text = patch_path.read_text(encoding="utf-8", errors="replace")
        if m_block := re.search(r"id:\s*agent-default-model(.*?)(?=\n-\s+id:|\Z)", text, re.DOTALL):
            if pm := re.search(r"^\s*provider:\s*['\"]?([^\s'\"#]+)", m_block.group(1), re.MULTILINE):
                provider = pm.group(1).strip()
            if mm := re.search(r"^\s*model:\s*['\"]?([^\s'\"#]+)", m_block.group(1), re.MULTILINE):
                model = mm.group(1).strip()
    full_default = f"{provider}/{model}" if "/" not in model else model
    return _probe_meta(full_default, None, [full_default, "deepseek-chat", "deepseek-reasoner"], "local_config")


def _probe_agy_local(home: pathlib.Path) -> dict[str, Any]:
    settings = _read_json(home / ".gemini" / "antigravity-cli" / "settings.json")
    default_model = _clean_str(settings.get("model")) if isinstance(settings, dict) else None
    return _probe_meta(default_model, None, [], "local_config" if default_model else "none")


_LOCAL_PROBES: dict[str, Callable[[pathlib.Path], dict[str, Any]]] = {
    "codex": _probe_codex_local,
    "claude": _probe_claude_local,
    "grok": _probe_grok_local,
    "pi": _probe_pi_local,
    "kimi": _probe_kimi_local,
    "dsh": _probe_dsh_local,
    "agy": _probe_agy_local,
}


def _parse_cli_models(backend: str, stdout: str) -> list[str]:
    models: list[str | None] = []
    for idx, line in enumerate(stdout.splitlines()):
        s = line.strip()
        if not s:
            continue
        if backend == "agy":
            if s.lower().startswith(("fetching ", "available ")):
                continue
            col = s.split("\t", 1)[0].split()[0].strip()
            if re.match(r"^[a-zA-Z0-9][a-zA-Z0-9._:/-]*$", col):
                models.append(col)
        elif backend == "grok":
            if m := re.match(r"^\s*[*+-]\s+([a-zA-Z0-9._:/-]+)", line):
                models.append(m.group(1))
        elif backend == "pi":
            cols = s.split()
            if not (idx == 0 and len(cols) >= 2 and cols[0].lower() == "provider" and cols[1].lower() == "model") and len(cols) >= 2:
                models.append(cols[1])
    return _dedupe(models)


def _run_cli_model_probe(backend: str, executable: str) -> list[str]:
    args_map = {"agy": ["models"], "grok": ["models"], "pi": ["--list-models"]}
    if backend not in args_map:
        return []
    try:
        proc = subprocess.run(
            [executable, *args_map[backend]],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=MODEL_PROBE_TIMEOUT_SECONDS,
        )
        return _parse_cli_models(backend, proc.stdout) if (proc.returncode == 0 and proc.stdout) else []
    except Exception:
        return []


def _resolve_ttl_models(
    backend: str,
    executable: str | None,
    cache_path: pathlib.Path,
    *,
    refresh_models: bool,
    allow_live_probe: bool,
    ttl_seconds: int,
) -> tuple[list[str], str | None]:
    cache_doc = _load_ttl_cache(cache_path)
    entry = cache_doc.get("backends", {}).get(backend)
    now = time.time()
    cached = _dedupe([str(m) for m in entry["models"]]) if isinstance(entry, dict) and isinstance(entry.get("models"), list) else []
    if cached and not refresh_models and (now - float(entry.get("fetched_at") or 0.0)) < ttl_seconds:
        return cached, "ttl_cache"
    if allow_live_probe and executable:
        if live := _run_cli_model_probe(backend, executable):
            cache_doc.setdefault("backends", {})[backend] = {"fetched_at": now, "models": live}
            _save_ttl_cache(cache_path, cache_doc)
            return live, "live_probe"
    if cached:
        return cached, "stale_ttl_cache"
    return [], None


def _normalize_agy_default_model(default_model: str | None, models: list[str]) -> str | None:
    """将 agy settings.json 中的显示名（如 'Gemini 4 Neon (XHigh)'）映射为规范 slug。"""
    if not default_model or not models:
        return default_model
    if default_model in models:
        return default_model
    norm = _normalize_slug(default_model)
    for m in models:
        if _normalize_slug(m) == norm:
            return m
    return default_model


def probe_backend_models(
    backend: str,
    executable: str | None = None,
    env: dict[str, str] | None = None,
    home: pathlib.Path | None = None,
    refresh_models: bool = False,
    allow_live_probe: bool = True,
    ttl_seconds: int = MODEL_CACHE_TTL_SECONDS,
) -> dict[str, Any]:
    """混合探测单个后端的默认模型与候选模型列表。"""
    home_dir = home if home is not None else pathlib.Path.home()
    probe_fn = _LOCAL_PROBES.get(backend)
    if probe_fn is None:
        return _probe_meta()
    local = probe_fn(home_dir)
    if backend not in ("agy", "grok", "pi"):
        return local
    if backend != "agy" and local["models"] and not refresh_models:
        return local

    ttl_models, ttl_source = _resolve_ttl_models(
        backend,
        executable,
        get_cache_file_path(env=env, home=home_dir),
        refresh_models=refresh_models,
        allow_live_probe=allow_live_probe,
        ttl_seconds=ttl_seconds,
    )
    if ttl_models and ttl_source:
        if backend == "agy":
            norm_default = _normalize_agy_default_model(local["default_model"], ttl_models)
            return {**local, "default_model": norm_default, "models": ttl_models, "model_source": ttl_source}
        merged = _dedupe(
            ([local["default_model"]] if local["default_model"] else []) + ttl_models + local["models"]
        )
        return {**local, "models": merged, "model_source": ttl_source}
    return local


def detect_available_backends(
    env: dict[str, str] | None = None,
    home: pathlib.Path | None = None,
    include_models: bool = True,
    refresh_models: bool = False,
    allow_live_probe: bool = True,
) -> list[dict[str, Any]]:
    """探测当前环境可用的全部后端、隔离级别、effort 能力及模型摘要。"""
    src = os.environ if env is None else env
    results: list[dict[str, Any]] = []
    for info in BACKEND_REGISTRY:
        base = {
            "backend": info.name,
            "aliases": list(info.aliases),
            "isolation_level": info.isolation_level,
            "isolation_summary": info.isolation_summary,
            "supports_effort": info.supports_effort,
            "effort_values": list(info.effort_values),
            "input_mode": info.input_mode,
        }
        if info.kind == "cli":
            assert info.binary is not None
            path = shutil.which(info.binary)
            meta = (
                probe_backend_models(
                    info.name, executable=path, env=src, home=home,
                    refresh_models=refresh_models, allow_live_probe=allow_live_probe,
                )
                if (path and include_models)
                else _probe_meta()
            )
            results.append({**base, "available": bool(path), "detail": path or "未在 PATH 中找到", **meta})
        else:
            has_key = bool(src.get("COLD_CRITIQUE_API_KEY") or src.get("OPENAI_API_KEY"))
            model = src.get("COLD_CRITIQUE_OPENAI_MODEL", "").strip() or src.get("OPENAI_MODEL", "").strip()
            base_url = src.get("COLD_CRITIQUE_BASE_URL", "").strip() or src.get("OPENAI_BASE_URL", "").strip() or DEFAULT_OPENAI_BASE_URL
            ready = has_key and bool(model)
            detail = (
                f"已配置模型 {model}（{base_url}）"
                if ready
                else (f"已检测到 API Key，显式传 -c openai:<model> 即可使用（{base_url}）" if has_key else "未设置 OPENAI_API_KEY / COLD_CRITIQUE_API_KEY")
            )
            results.append(
                {
                    **base,
                    "available": ready,
                    "detail": detail,
                    "default_spec": f"openai:{model}" if ready else None,
                    **_probe_meta(model or None, None, [model] if model else [], "env" if model else "none"),
                }
            )
    return results


def _infer_model_family(backend: str, default_model: str | None) -> str:
    """推断后端当前默认模型所属模型家族，用于生成真正异构的推荐预设。"""
    m = (default_model or "").strip().lower()
    if m:
        if "gemini" in m:
            return "gemini"
        if any(k in m for k in ("gpt-", "gpt5", "gpt6", "codex")) or re.search(r"(?:^|[/-])o[134](?:$|[/-])", m):
            return "openai"
        if any(k in m for k in ("claude", "opus", "sonnet", "haiku")):
            return "anthropic"
        if "grok" in m:
            return "xai"
        if "deepseek" in m:
            return "deepseek"
        if "kimi" in m or re.search(r"(?:^|[/-])k[23](?:\.|$|[/-])", m):
            return "moonshot"
        if "qwen" in m:
            return "qwen"
    fallback = {
        "codex": "openai",
        "agy": "gemini",
        "claude": "anthropic",
        "grok": "xai",
        "dsh": "deepseek",
        "kimi": "moonshot",
    }
    return fallback.get(backend, backend)


def _select_heterogeneous_backends(avail: list[dict[str, Any]], count: int) -> list[dict[str, Any]]:
    """优先挑选不同模型家族的可用后端，避免多 CLI 实际指向同一模型家族的“伪异构”。"""
    chosen: list[dict[str, Any]] = []
    seen_families: set[str] = set()
    for item in avail:
        fam = _infer_model_family(str(item["backend"]), _clean_str(item.get("default_model")))
        if fam not in seen_families:
            seen_families.add(fam)
            chosen.append(item)
            if len(chosen) >= count:
                return chosen
    for item in avail:
        if item not in chosen:
            chosen.append(item)
            if len(chosen) >= count:
                break
    return chosen


def _preset_channel(item: dict[str, Any], effort: str | None = None) -> str:
    base = str(item.get("default_spec") or item["backend"])
    return f"{base}@{effort}" if (effort and item.get("supports_effort")) else base


def build_recommended_presets(
    detected: list[dict[str, Any]],
    env: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """基于本机实际可用后端动态生成 2–3 个不写死个人模型名的真异构推荐组合预设。"""
    src = os.environ if env is None else env
    avail = [item for item in detected if item.get("available")]
    presets: list[dict[str, Any]] = []

    try:
        env_specs = parse_env_channels(env=src)
    except ValueError:
        env_specs = []

    def add_preset(name: str, channels: list[str], note: str) -> None:
        if channels and not any(p["channels"] == channels for p in presets):
            presets.append({"id": len(presets) + 1, "name": name, "channels": channels, "note": note})

    duo = _select_heterogeneous_backends(avail, 2)
    trio = _select_heterogeneous_backends(avail, 3)

    if env_specs:
        add_preset("当前默认配置（COLD_CRITIQUE_CHANNELS）", [s.label for s in env_specs], "沿用环境变量 COLD_CRITIQUE_CHANNELS 设定的通道组合")
    elif len(duo) >= 2:
        add_preset("标准异构双通道（推荐）", [_preset_channel(i, "high") for i in duo], "从本机可用后端自动挑选 2 个不同模型家族 Agent，均衡速度与交叉校验质量")
    elif len(avail) == 1:
        add_preset("单通道冷审（当前仅检测到 1 个可用后端）", [_preset_channel(avail[0], "high")], "仅检测到 1 个可用后端；建议安装更多 CLI 或配置 OPENAI_API_KEY 以启用异构双审")

    if len(trio) >= 3:
        add_preset("异构三通道深度盲审", [_preset_channel(i, "high") for i in trio], "同时拉起 3 个优先跨模型家族的 Agent 并发对抗，适合关键决策与定稿前深审")
    if len(duo) >= 2:
        add_preset("轻量快速双审", [_preset_channel(i, "low") for i in duo], "支持 effort 的通道使用 low 强度，耗时更短，适合初稿快速试探")
    return presets[:3]


def parse_env_channels(env: dict[str, str] | None = None) -> list[ChannelSpec]:
    raw = (os.environ if env is None else env).get("COLD_CRITIQUE_CHANNELS", "").strip()
    if not raw:
        return []
    return [parse_channel_spec(p.strip()) for p in re.split(r"[,;]", raw) if p.strip()]


def auto_select_channels(env: dict[str, str] | None = None) -> list[ChannelSpec]:
    detected = detect_available_backends(env=env, include_models=True, allow_live_probe=False)
    avail = _select_heterogeneous_backends([item for item in detected if item["available"]], 2)
    return [parse_channel_spec(str(item.get("default_spec") or item["backend"])) for item in avail]


# ---------------------------------------------------------------------------
# 各后端命令构造与统一执行器
# ---------------------------------------------------------------------------


def _format_codex_mcp_disable(server_name: str) -> str:
    key = server_name if _SAFE_TOML_KEY_RE.match(server_name) else json.dumps(server_name)
    return f"mcp_servers.{key}.enabled=false"


def build_codex_command(
    spec: ChannelSpec,
    out_path: str,
    executable: str | None = None,
    disabled_mcp_servers: list[str] | tuple[str, ...] | None = None,
) -> list[str]:
    cmd = [executable or "codex", "exec", "--skip-git-repo-check", "-s", "read-only", "--ephemeral"]
    for feat in CODEX_DISABLED_FEATURES:
        cmd.extend(["--disable", feat])
    for override in CODEX_ISOLATION_OVERRIDES:
        cmd.extend(["-c", override])
    for srv in disabled_mcp_servers or ():
        cmd.extend(["-c", _format_codex_mcp_disable(srv)])
    if spec.model:
        cmd.extend(["-m", spec.model])
    if spec.effort:
        cmd.extend(["-c", f'model_reasoning_effort="{spec.effort}"'])
    return [*cmd, "-o", out_path, "-"]


def build_claude_command(spec: ChannelSpec, executable: str | None = None) -> list[str]:
    cmd = [
        executable or "claude", "-p",
        "--tools", "",
        "--strict-mcp-config",
        "--disable-slash-commands",
        "--no-session-persistence",
        "--settings", CLAUDE_DISABLE_HOOKS_SETTINGS,
        "--output-format", "json",
    ]
    if spec.model:
        cmd.extend(["--model", spec.model])
    if spec.effort:
        cmd.extend(["--effort", spec.effort])
    return cmd


def build_agy_stream_input(full_prompt: str) -> str:
    return json.dumps({"event": "user", "message": {"role": "user", "content": full_prompt}}, ensure_ascii=False) + "\n"


def build_agy_command(spec: ChannelSpec, executable: str | None = None) -> list[str]:
    cmd = [
        executable or "agy",
        "--disable-slash-commands",
        "--sandbox",
        "--input-format", "stream-json",
        "--output-format", "stream-json",
    ]
    if spec.model:
        cmd.extend(["--model", spec.model])
    if spec.effort:
        cmd.extend(["--effort", spec.effort])
    return cmd


def build_grok_command(spec: ChannelSpec, prompt_file: str, executable: str | None = None) -> list[str]:
    cmd = [executable or "grok", "--prompt-file", prompt_file, "--tools", "", "--no-memory", "--no-subagents", "--disable-web-search", "--output-format", "json"]
    if spec.model:
        cmd.extend(["--model", spec.model])
    if spec.effort:
        cmd.extend(["--reasoning-effort", spec.effort])
    return cmd


def build_pi_command(spec: ChannelSpec, executable: str | None = None) -> list[str]:
    cmd = [executable or "pi", "-p", "--no-tools", "--no-mcp", "--no-extensions", "--no-skills", "--no-context-files", "--no-session", "--mode", "text"]
    if spec.model:
        cmd.extend(["--model", spec.model])
    if spec.effort:
        cmd.extend(["--thinking", spec.effort])
    return cmd


def build_kimi_command(spec: ChannelSpec, full_prompt: str, skills_dir: str, executable: str | None = None) -> list[str]:
    cmd = [executable or "kimi", "--skills-dir", skills_dir, "--output-format", "stream-json"]
    if spec.model:
        cmd.extend(["--model", spec.model])
    return [*cmd, "-p", full_prompt]


def build_dsh_patch_yaml(model: str | None = None) -> str:
    """生成用于 dsh --profile headless 的临时 patch：启用本地凭证、硬禁工具，并可选覆写默认模型。"""
    entries = ["- id: credentials\n  disabled: false"] + [f"- id: {pid}\n  disabled: true" for pid in DSH_DISABLED_PLUGIN_IDS]
    if model:
        if "/" in model:
            provider, model_id = model.split("/", 1)
            cfg = f"    provider: {json.dumps(provider, ensure_ascii=False)}\n    model: {json.dumps(model_id, ensure_ascii=False)}"
        else:
            cfg = f"    model: {json.dumps(model, ensure_ascii=False)}"
        entries.append(f"- id: agent-default-model\n  config:\n{cfg}")
    return "\n".join(entries) + "\n"


def build_dsh_command(spec: ChannelSpec, patch_path: str, executable: str | None = None) -> list[str]:
    return [executable or "dsh", "--profile", "headless", "--patch", patch_path, "--permission-mode", "read-only", "--approval-policy", "never", "-"]


def build_openai_payload(model: str, full_prompt: str, effort: str | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"model": model, "messages": [{"role": "user", "content": full_prompt}]}
    if effort:
        payload["reasoning_effort"] = effort
    return payload


def extract_openai_text(data: dict[str, Any]) -> str:
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ValueError("API 响应中缺少 choices 字段。")
    content = (choices[0].get("message") or {}).get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        return "".join(i.get("text", "") for i in content if isinstance(i, dict) and i.get("type") in ("text", "output_text")).strip()
    raise ValueError("无法从 choices[0].message.content 中提取文本。")


def _parse_last_json_object(raw_out: str) -> dict[str, Any] | None:
    stripped = raw_out.strip()
    if not stripped:
        return None
    try:
        parsed = json.loads(stripped)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        for line in reversed(stripped.splitlines()):
            line = line.strip()
            if line.startswith("{") and line.endswith("}"):
                try:
                    parsed = json.loads(line)
                    if isinstance(parsed, dict):
                        return parsed
                except json.JSONDecodeError:
                    continue
    return None


def _extract_agy_stream_json(raw_out: str) -> dict[str, Any] | None:
    """解析 agy --output-format stream-json（兼容器老式单对象 JSON）输出。"""
    init_model: str | None = None
    result_obj: dict[str, Any] | None = None
    for line in raw_out.splitlines():
        s = line.strip()
        if not s or not s.startswith("{"):
            continue
        try:
            obj = json.loads(s)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        ev = obj.get("event")
        if ev == "init" and isinstance(obj.get("init"), dict):
            init_model = _clean_str(obj["init"].get("model")) or init_model
        elif ev == "result" and isinstance(obj.get("result"), dict):
            result_obj = obj["result"]
        elif ev is None and "status" in obj and "response" in obj:
            result_obj = obj
    if result_obj is None:
        return None
    out = dict(result_obj)
    if init_model and not out.get("model"):
        out["model"] = init_model
    return out


def _extract_kimi_stream_json(raw_out: str) -> str:
    parts: list[str] = []
    parsed_any = False
    for line in raw_out.splitlines():
        s = line.strip()
        if not s:
            continue
        try:
            obj = json.loads(s)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        parsed_any = True
        if obj.get("role") == "assistant":
            content = obj.get("content")
            if isinstance(content, str) and content.strip():
                parts.append(content.strip())
            elif isinstance(content, list):
                joined = "".join(
                    str(i.get("text") or "") for i in content if isinstance(i, dict) and i.get("type") in ("text", "output_text")
                ).strip()
                if joined:
                    parts.append(joined)
    if parts:
        return "\n\n".join(parts).strip()
    if not parsed_any:
        cleaned = raw_out.strip()
        return cleaned[2:].strip() if cleaned.startswith("• ") else cleaned
    return ""


def _ok_result(
    spec: ChannelSpec,
    response: str,
    elapsed: float,
    *,
    model: str | None = None,
    usage: Any = None,
) -> dict[str, Any]:
    res: dict[str, Any] = {
        "channel": spec.label,
        "backend": spec.backend,
        "model": model or spec.model or "default",
        "ok": True,
        "duration_seconds": round(elapsed, 2),
        "chars": len(response),
        "response": response,
    }
    if isinstance(usage, dict):
        res["usage"] = usage
    return res


def _err_result(spec: ChannelSpec, error: str, elapsed: float, *, model: str | None = None) -> dict[str, Any]:
    return {
        "channel": spec.label,
        "backend": spec.backend,
        "model": model or spec.model or "default",
        "ok": False,
        "duration_seconds": round(elapsed, 2),
        "error": error,
    }


def _format_exec_error(exc: Exception, backend: str) -> str:
    if isinstance(exc, OSError) and getattr(exc, "errno", None) in (7, 206):
        return (
            f"命令行参数超出系统长度上限（{exc}）。"
            f"通道 '{backend}' 通过参数传正文，超长文本请改用 codex/agy/claude/grok/dsh/pi/openai。"
        )
    return str(exc)


@contextlib.contextmanager
def _temp_file(prefix: str, suffix: str, content: str | None = None):
    with tempfile.NamedTemporaryFile(prefix=prefix, suffix=suffix, mode="w", encoding="utf-8", delete=False) as tf:
        if content is not None:
            tf.write(content)
        path = tf.name
    try:
        yield path
    finally:
        if os.path.exists(path):
            with contextlib.suppress(OSError):
                os.remove(path)


def _run_cli_channel(spec: ChannelSpec, full_prompt: str, timeout_s: int) -> dict[str, Any]:
    t0 = time.time()
    exe = resolve_cli_executable(BACKEND_MAP[spec.backend].binary or spec.backend)
    try:
        with contextlib.ExitStack() as stack:
            workdir = stack.enter_context(tempfile.TemporaryDirectory(prefix="cold_critique_work_"))
            stdin_text: str | None = None
            out_path: str | None = None

            if spec.backend == "codex":
                out_path = stack.enter_context(_temp_file("cold_critique_codex_", ".txt"))
                mcp_servers = list_codex_enabled_mcp_servers()
                cmd = build_codex_command(spec, out_path, executable=exe, disabled_mcp_servers=mcp_servers)
                stdin_text = full_prompt
            elif spec.backend == "claude":
                cmd, stdin_text = build_claude_command(spec, executable=exe), full_prompt
            elif spec.backend == "agy":
                cmd, stdin_text = build_agy_command(spec, executable=exe), build_agy_stream_input(full_prompt)
            elif spec.backend == "grok":
                pf = stack.enter_context(_temp_file("cold_critique_grok_", ".txt", full_prompt))
                cmd = build_grok_command(spec, pf, executable=exe)
            elif spec.backend == "pi":
                cmd, stdin_text = build_pi_command(spec, executable=exe), full_prompt
            elif spec.backend == "kimi":
                sk = stack.enter_context(tempfile.TemporaryDirectory(prefix="cold_critique_kimi_skills_"))
                cmd = build_kimi_command(spec, full_prompt, skills_dir=sk, executable=exe)
            elif spec.backend == "dsh":
                patch = stack.enter_context(_temp_file("cold_critique_dsh_patch_", ".yml", build_dsh_patch_yaml(spec.model)))
                cmd, stdin_text = build_dsh_command(spec, patch, executable=exe), full_prompt
            else:
                raise ValueError(f"未知 CLI 后端：{spec.backend}")

            kwargs: dict[str, Any] = {
                "cwd": workdir,
                "capture_output": True,
                "text": True,
                "encoding": "utf-8",
                "errors": "replace",
                "timeout": timeout_s,
            }
            if stdin_text is not None:
                kwargs["input"] = stdin_text
            else:
                kwargs["stdin"] = subprocess.DEVNULL
            proc = subprocess.run(cmd, **kwargs)

            elapsed = time.time() - t0
            raw_out = proc.stdout.strip()
            if proc.returncode == 0:
                if spec.backend == "codex" and out_path and os.path.exists(out_path):
                    resp = pathlib.Path(out_path).read_text(encoding="utf-8", errors="replace").strip()
                    if resp:
                        return _ok_result(spec, resp, elapsed)
                elif spec.backend in ("pi", "dsh") and raw_out:
                    return _ok_result(spec, raw_out, elapsed)
                elif spec.backend == "kimi" and raw_out:
                    if resp := _extract_kimi_stream_json(raw_out):
                        return _ok_result(spec, resp, elapsed)
                elif spec.backend == "agy" and raw_out:
                    parsed = _extract_agy_stream_json(raw_out)
                    if parsed and parsed.get("status") == "SUCCESS":
                        if resp := str(parsed.get("response") or "").strip():
                            return _ok_result(
                                spec,
                                resp,
                                float(parsed.get("duration_seconds", elapsed)),
                                model=_clean_str(parsed.get("model")) or spec.model,
                                usage=parsed.get("usage"),
                            )
                elif spec.backend in ("claude", "grok") and raw_out:
                    parsed = _parse_last_json_object(raw_out)
                    if parsed is None:
                        return _ok_result(spec, raw_out, elapsed)
                    resp = str(parsed.get("text" if spec.backend == "grok" else "result") or parsed.get("result") or parsed.get("response") or "").strip()
                    if resp and not (spec.backend == "claude" and parsed.get("is_error")):
                        model = spec.model
                        if spec.backend == "grok" and not model and isinstance(parsed.get("modelUsage"), dict) and parsed["modelUsage"]:
                            model = next(iter(parsed["modelUsage"].keys()))
                        return _ok_result(spec, resp, elapsed, model=model, usage=parsed.get("usage"))

            err = f"rc={proc.returncode} stderr={proc.stderr.strip()[:400]} stdout={raw_out[:400]}"
            return _err_result(spec, err, elapsed)
    except Exception as exc:
        return _err_result(spec, _format_exec_error(exc, spec.backend), time.time() - t0)


def _run_openai(spec: ChannelSpec, full_prompt: str, timeout_s: int) -> dict[str, Any]:
    t0 = time.time()
    try:
        base_url, api_key, model = resolve_openai_env(spec)
    except ValueError as exc:
        return _err_result(spec, str(exc), 0.0, model=spec.model or "unset")

    body = json.dumps(build_openai_payload(model, full_prompt, spec.effort), ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        f"{base_url}/chat/completions",
        data=body,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp_obj:
            parsed = json.loads(resp_obj.read().decode("utf-8", errors="replace"))
        text = extract_openai_text(parsed)
        if not text:
            raise ValueError("模型返回了空正文。")
        return _ok_result(spec, text, time.time() - t0, model=str(parsed.get("model") or model), usage=parsed.get("usage"))
    except urllib.error.HTTPError as exc:
        err_body = ""
        with contextlib.suppress(Exception):
            err_body = exc.read().decode("utf-8", errors="replace")[:400]
        return _err_result(spec, f"HTTP {exc.code}: {err_body}", time.time() - t0, model=model)
    except Exception as exc:
        return _err_result(spec, str(exc), time.time() - t0, model=model)


def run_channel(spec: ChannelSpec, full_prompt: str, timeout_s: int) -> dict[str, Any]:
    if spec.backend == "openai":
        return _run_openai(spec, full_prompt, timeout_s)
    if spec.backend in BACKEND_MAP:
        return _run_cli_channel(spec, full_prompt, timeout_s)
    raise ValueError(f"未知后端：{spec.backend}")


# ---------------------------------------------------------------------------
# 逐路原子落盘与失败通道续跑（--out-dir / --resume）
# ---------------------------------------------------------------------------


def _channel_result_filename(label: str, occurrence: int = 1) -> str:
    safe = _SAFE_FILENAME_RE.sub("_", label).strip("._") or "channel"
    suffix = f"_{occurrence}" if occurrence > 1 else ""
    return f"{safe}{suffix}.json"


def build_channel_result_paths(specs: list[ChannelSpec], out_dir: pathlib.Path) -> list[pathlib.Path]:
    counts: dict[str, int] = {}
    paths: list[pathlib.Path] = []
    for spec in specs:
        counts[spec.label] = counts.get(spec.label, 0) + 1
        paths.append(out_dir / _channel_result_filename(spec.label, counts[spec.label]))
    return paths


def _load_resumable_result(path: pathlib.Path, spec: ChannelSpec, prompt_sha256: str) -> dict[str, Any] | None:
    data = _read_json(path)
    if not isinstance(data, dict):
        return None
    if (
        data.get("ok") is True
        and bool(str(data.get("response") or "").strip())
        and data.get("channel") == spec.label
        and data.get("prompt_sha256") == prompt_sha256
    ):
        reused = dict(data)
        reused["resumed"] = True
        return reused
    return None


def execute_channels(
    specs: list[ChannelSpec],
    full_prompt: str,
    *,
    explicit_timeout: int | None = None,
    out_dir: pathlib.Path | None = None,
    resume: bool = False,
    clean_text: str | None = None,
    prefix: str | None = None,
) -> tuple[list[dict[str, Any]], str]:
    """并发执行全部通道；指定 out_dir 时每路完成即原子落盘，支持 --resume 仅重跑未成功通道。"""
    prompt_sha256 = hashlib.sha256(full_prompt.encode("utf-8")).hexdigest()
    result_paths: list[pathlib.Path | None] = [None] * len(specs)
    if out_dir is not None:
        out_dir.mkdir(parents=True, exist_ok=True)
        if clean_text is not None:
            (out_dir / "input.md").write_text(clean_text, encoding="utf-8")
        if prefix is not None:
            (out_dir / "prompt.txt").write_text(prefix, encoding="utf-8")
        result_paths = list(build_channel_result_paths(specs, out_dir))

    def _run_one(idx: int) -> dict[str, Any]:
        spec = specs[idx]
        rpath = result_paths[idx]
        if resume and rpath is not None:
            if cached := _load_resumable_result(rpath, spec, prompt_sha256):
                cached["result_file"] = str(rpath)
                return cached
        timeout_s = resolve_channel_timeout(spec, explicit_timeout)
        res = run_channel(spec, full_prompt, timeout_s)
        res["timeout_seconds"] = timeout_s
        if rpath is not None:
            persisted = {**res, "prompt_sha256": prompt_sha256}
            _atomic_write_json(rpath, persisted)
            res["result_file"] = str(rpath)
        return res

    with concurrent.futures.ThreadPoolExecutor(max_workers=len(specs)) as ex:
        results = list(ex.map(_run_one, range(len(specs))))
    return results, prompt_sha256


# ---------------------------------------------------------------------------
# CLI 参数与入口
# ---------------------------------------------------------------------------


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="零前情、禁工具的多通道对抗冷审执行器（对齐 aipane 7 个 CLI：claude/codex/dsh/grok/pi/kimi/agy，并保留 openai 兼容 API）。"
    )
    parser.add_argument("--file", "-f", help="待审 Markdown/文本文件路径（省略时从 stdin 读取）")
    parser.add_argument(
        "--channel", "-c", action="append", dest="channels",
        help="指定评审通道，可重复传入并发执行（支持同后端多开与本地唯一模型简写）。格式：<backend>[:<model>][@<effort>][#<ENV_PREFIX>]。",
    )
    parser.add_argument(
        "--profile", choices=tuple(PROMPT_PROFILES.keys()), default="summary",
        help="内置评审前缀模板：summary（默认对话/方案总结）、article（公开长文）、code（代码/架构对照）",
    )
    parser.add_argument("--prompt-file", help="可选自定义评审前缀提示词文件路径（优先级高于 --profile）")
    parser.add_argument("--raw", action="store_true", help="跳过剥壳，保留原始 frontmatter、参考资料与外链")
    parser.add_argument("--redact-paths", action="store_true", help="将文本中的本机用户目录路径替换为 <HOME>")
    parser.add_argument(
        "--timeout", type=int, default=None,
        help="单通道超时时间（秒；省略时按 effort 自动设值：常规 300s、xhigh 600s、max 900s）",
    )
    parser.add_argument("--out-dir", help="结果落盘目录：保存剥壳输入、每路完成后立即落盘单通道 JSON 及 summary.json")
    parser.add_argument("--resume", action="store_true", help="配合 --out-dir 使用：复用已成功且输入未变的通道结果，仅补跑失败/缺失通道")
    parser.add_argument("--json", action="store_true", help="以 JSON 格式输出结果")
    parser.add_argument("--dry-run", action="store_true", help="仅打印剥壳统计、长度预警与已选通道，不实际调用模型")
    parser.add_argument("--list-backends", action="store_true", help="检查本机可用后端、隔离级别、模型摘要与动态推荐预设并退出")
    parser.add_argument("--list-models", metavar="BACKEND", help="列出指定后端的完整候选模型列表并退出")
    parser.add_argument("--refresh-models", action="store_true", help="忽略本地 24h TTL 模型快照缓存，强制重新探测")
    return parser


def _print_backends_human(detected: list[dict[str, Any]], presets: list[dict[str, Any]], env_raw: str) -> None:
    print("可用后端速查：")
    for item in detected:
        mark = "可用" if item["available"] else "未就绪"
        iso = "硬禁工具" if item["isolation_level"] == "hard" else "沙箱/配置隔离"
        eff = f"支持({'/'.join(item['effort_values'])})" if item["supports_effort"] else "不支持"
        alias = f"（别名: {', '.join(item['aliases'])}）" if item.get("aliases") else ""
        print(f"- {item['backend']}{alias} [{mark}] | 隔离: {iso} | effort: {eff} | 默认模型: {item.get('default_model') or '默认'}")
        if item["available"]:
            models = item.get("models") or []
            if models:
                summary = ", ".join(models[:MODEL_SUMMARY_LIMIT])
                if len(models) > MODEL_SUMMARY_LIMIT:
                    summary += f" …（共 {len(models)} 个，用 --list-models {item['backend']} 查看全部）"
                print(f"    候选模型 ({item.get('model_source')}): {summary}")
        else:
            print(f"    说明: {item['detail']}")
    if env_raw:
        print(f"\n环境变量默认通道 (COLD_CRITIQUE_CHANNELS): {env_raw}")
    if presets:
        print("\n推荐组合预设：")
        for p in presets:
            print(f"  {p['id']}. {p['name']}: {', '.join(p['channels'])}\n     {p['note']}")


def _handle_list_models(parser: argparse.ArgumentParser, args: argparse.Namespace) -> int:
    backend = normalize_backend_name(args.list_models)
    if backend not in BACKEND_MAP:
        parser.error(f"不支持的后端 '{args.list_models}'，可选：{', '.join(SUPPORTED_BACKENDS)}。")
    info = BACKEND_MAP[backend]
    exe = shutil.which(info.binary) if info.binary else None
    if info.kind == "cli" and not exe:
        parser.error(f"未在 PATH 中找到 '{info.binary}'，无法列出模型。")
    meta = (
        next((d for d in detect_available_backends(include_models=True) if d["backend"] == "openai"), _probe_meta())
        if info.kind == "http"
        else probe_backend_models(backend, executable=exe, refresh_models=args.refresh_models, allow_live_probe=True)
    )
    out = {
        "backend": backend,
        "supports_effort": info.supports_effort,
        "effort_values": list(info.effort_values),
        "default_model": meta.get("default_model"),
        "default_effort": meta.get("default_effort"),
        "model_source": meta.get("model_source"),
        "models": meta.get("models") or [],
    }
    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        eff = "/".join(out["effort_values"]) if out["supports_effort"] else "不支持"
        print(f"后端: {backend} | 默认模型: {out['default_model'] or '未检测到'} | effort: {eff} | 来源: {out['model_source']}")
        print("\n".join(f"  - {m}" for m in out["models"]) or "  （未发现本地候选模型列表，可留空走默认或手动指定模型 ID）")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    if args.list_models:
        return _handle_list_models(parser, args)

    if args.list_backends:
        detected = detect_available_backends(include_models=True, refresh_models=args.refresh_models, allow_live_probe=True)
        presets = build_recommended_presets(detected)
        env_raw = os.environ.get("COLD_CRITIQUE_CHANNELS", "").strip()
        if args.json:
            payload: dict[str, Any] = {"backends": detected, "presets": presets}
            if env_raw:
                payload["env_channels"] = env_raw
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            _print_backends_human(detected, presets, env_raw)
        return 0

    if args.resume and not args.out_dir:
        parser.error("--resume 需要同时指定 --out-dir。")

    try:
        raw_specs = [parse_channel_spec(c) for c in args.channels] if args.channels else (parse_env_channels() or auto_select_channels())
        specs = resolve_channel_specs(raw_specs, allow_live_probe=False)
    except ValueError as exc:
        parser.error(str(exc))
    if not specs:
        parser.error(
            "未自动探测到可用后端。请安装任一受支持 CLI（claude/codex/dsh/grok/pi/kimi/agy），"
            "配置 COLD_CRITIQUE_CHANNELS，或配置 OPENAI_API_KEY 后显式传入 -c openai:<model>。"
        )

    if args.file:
        raw_text = pathlib.Path(args.file).read_text(encoding="utf-8")
    else:
        if sys.stdin.isatty():
            parser.error("请通过 --file <路径> 指定待审文件，或通过标准输入传入文本。")
        raw_text = sys.stdin.read()

    prefix = (
        pathlib.Path(args.prompt_file).read_text(encoding="utf-8").rstrip() + "\n\n---\n\n"
        if args.prompt_file
        else PROMPT_PROFILES[args.profile]
    )
    clean_text = (_HOME_PATH_RE.sub("<HOME>", raw_text.strip()) if args.redact_paths else raw_text.strip()) if args.raw else strip_markdown_shell(raw_text, redact_paths=args.redact_paths)
    if not clean_text:
        parser.error("剥壳后的待审正文为空。")

    full_prompt = prefix + clean_text
    argv_warnings = check_argv_prompt_warnings(specs, full_prompt)
    timeouts = {s.label: resolve_channel_timeout(s, args.timeout) for s in specs}

    if args.dry_run:
        print(json.dumps({
            "profile": "custom" if args.prompt_file else args.profile,
            "raw_chars": len(raw_text),
            "clean_chars": len(clean_text),
            "prompt_chars": len(full_prompt),
            "prompt_bytes": len(full_prompt.encode("utf-8")),
            "channels": [s.label for s in specs],
            "timeouts": timeouts,
            "single_channel_warning": len(specs) == 1,
            "warnings": argv_warnings,
            "preview_head": full_prompt[:280],
            "preview_tail": full_prompt[-200:],
        }, ensure_ascii=False, indent=2))
        return 0

    out_dir_path = pathlib.Path(args.out_dir) if args.out_dir else None
    results, prompt_sha256 = execute_channels(
        specs,
        full_prompt,
        explicit_timeout=args.timeout,
        out_dir=out_dir_path,
        resume=args.resume,
        clean_text=clean_text,
        prefix=prefix,
    )
    any_ok = any(r.get("ok") for r in results)

    summary_payload: dict[str, Any] = {
        "profile": "custom" if args.prompt_file else args.profile,
        "input_chars": len(clean_text),
        "channels_count": len(specs),
        "prompt_sha256": prompt_sha256,
        "results": results,
    }
    if out_dir_path is not None:
        summary_payload["out_dir"] = str(out_dir_path)
        summary_payload["input_file"] = str(out_dir_path / "input.md")
        _atomic_write_json(out_dir_path / "summary.json", summary_payload)
    if argv_warnings:
        summary_payload["warnings"] = argv_warnings

    if args.json:
        print(json.dumps(summary_payload, ensure_ascii=False, indent=2))
        return 0 if any_ok else 1

    for warn in argv_warnings:
        print(f"警告：{warn}\n", file=sys.stderr)
    if len(specs) == 1:
        print("提示：当前仅运行了单通道冷审；若需异构交叉校验，请再追加一个不同模型通道（-c）。\n")
    for res in results:
        print("=" * 70)
        tag = " (resumed)" if res.get("resumed") else ""
        if res["ok"]:
            print(f"[{res['channel']}]{tag} model={res['model']} | duration={res['duration_seconds']}s | chars={res['chars']}")
            print("=" * 70)
            print(res["response"], end="\n\n")
        else:
            print(f"[{res['channel']}] FAILED ({res['duration_seconds']}s)")
            print("=" * 70)
            print(res.get("error"), end="\n\n")
    return 0 if any_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
