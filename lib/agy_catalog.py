"""Antigravity model catalog, Family x Effort factorization, SWR cache, and quota reader."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import fcntl
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
from typing import Any


EFFORT_ORDER = ("low", "medium", "high", "xhigh", "max")
EFFORT_RANK = {name: idx for idx, name in enumerate(EFFORT_ORDER)}
EFFORT_SHORT = {
    "low": "low",
    "medium": "med",
    "high": "high",
    "xhigh": "xh",
    "max": "max",
}
EFFORT_ALIASES = {
    "low": "low",
    "med": "medium",
    "medium": "medium",
    "high": "high",
    "xh": "xhigh",
    "xhigh": "xhigh",
    "extra": "xhigh",
    "max": "max",
}
PAREN_SUFFIX_RE = re.compile(r"^(?P<family>.+?)\s*\((?P<tag>[^()]+)\)\s*$")
DEFAULT_CACHE_TTL_SECONDS = 1800.0
QUOTA_STALE_SECONDS = 7200.0


@dataclass(frozen=True)
class ModelVariant:
    slug: str
    display_name: str
    family: str
    effort: str  # canonical effort ("low".."max") or "" for non-effort variants
    variant_label: str  # display/step label (e.g. "xhigh", "neon", "fixed")
    order: int


@dataclass(frozen=True)
class ModelFamily:
    name: str
    variants: tuple[ModelVariant, ...]
    adjustable: bool
    has_effort_axis: bool
    default_variant_slug: str
    quota_group: str


@dataclass(frozen=True)
class Catalog:
    route: str
    fetched_at: float
    binary_signature: str
    variants: tuple[ModelVariant, ...]
    families: tuple[ModelFamily, ...]
    source: str  # "cache", "live", "stale-cache", "fallback"

    def by_slug(self) -> dict[str, ModelVariant]:
        return {item.slug: item for item in self.variants}

    def by_family(self) -> dict[str, ModelFamily]:
        return {item.name: item for item in self.families}

    def resolve_identifier(self, value: str | None) -> ModelVariant | None:
        """Resolve a slug or display name (case-insensitive) to a catalog variant."""
        if not value or not isinstance(value, str):
            return None
        needle = value.strip()
        if not needle:
            return None
        lowered = needle.casefold()
        for item in self.variants:
            if item.slug.casefold() == lowered or item.display_name.casefold() == lowered:
                return item
        return None

    def resolve_family_and_effort(self, family_name: str, effort: str | None = None) -> ModelVariant | None:
        family = self.by_family().get(family_name)
        if family is None:
            lowered = family_name.strip().casefold()
            family = next((f for f in self.families if f.name.casefold() == lowered), None)
        if family is None:
            return None
        if effort:
            canonical = EFFORT_ALIASES.get(effort.strip().casefold(), effort.strip().casefold())
            for variant in family.variants:
                if variant.effort == canonical or variant.variant_label.casefold() == canonical:
                    return variant
        return next((v for v in family.variants if v.slug == family.default_variant_slug), family.variants[0])


def state_dir(environment: dict[str, str] | None = None) -> Path:
    env = os.environ if environment is None else environment
    data = Path(env.get("XDG_DATA_HOME") or Path(env.get("HOME", str(Path.home()))) / ".local/share")
    return Path(env.get("AIPANE_STATE_DIR") or data / "aipane") / "agy"


def binary_signature(binary: Path) -> str:
    try:
        info = binary.resolve().stat()
        return f"{info.st_mtime_ns}:{info.st_size}"
    except OSError:
        return ""


def parse_models_output(output: str) -> list[tuple[str, str]]:
    """Parse `agy models` stdout (TSV when piped, multi-space fallback)."""
    rows: list[tuple[str, str]] = []
    seen: set[str] = set()
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if "\t" in line:
            slug, display = line.split("\t", 1)
        else:
            parts = re.split(r"\s{2,}", line, maxsplit=1)
            if len(parts) == 2:
                slug, display = parts
            else:
                continue
        slug = slug.strip()
        display = display.strip()
        if not slug or not display or slug in seen:
            continue
        # Reject error/spinner lines if stderr leaked into stdout.
        if " " in slug or slug.startswith("error:"):
            continue
        seen.add(slug)
        rows.append((slug, display))
    return rows


def quota_group_for_model(name: str) -> str:
    lowered = name.casefold()
    if "argon" in lowered:
        return "argon"
    if "claude" in lowered or "gpt" in lowered:
        return "3p"
    return "gemini"


def _split_family_and_tag(display_name: str) -> tuple[str, str, str]:
    """Return (family_name, canonical_effort, variant_label)."""
    match = PAREN_SUFFIX_RE.match(display_name)
    if not match:
        return display_name.strip(), "", "fixed"
    family = match.group("family").strip()
    tag = match.group("tag").strip()
    effort = EFFORT_ALIASES.get(tag.casefold(), "")
    if effort:
        return family, effort, effort
    return family, "", tag.lower()


def build_catalog(
    raw_rows: list[tuple[str, str]],
    *,
    route: str = "plan",
    fetched_at: float = 0.0,
    binary_sig: str = "",
    source: str = "live",
) -> Catalog:
    variants: list[ModelVariant] = []
    grouped: dict[str, list[ModelVariant]] = {}
    family_order: list[str] = []

    for index, (slug, display_name) in enumerate(raw_rows):
        family_name, effort, variant_label = _split_family_and_tag(display_name)
        variant = ModelVariant(
            slug=slug,
            display_name=display_name,
            family=family_name,
            effort=effort,
            variant_label=variant_label,
            order=index,
        )
        variants.append(variant)
        if family_name not in grouped:
            grouped[family_name] = []
            family_order.append(family_name)
        grouped[family_name].append(variant)

    families: list[ModelFamily] = []
    for family_name in family_order:
        items = grouped[family_name]
        # Keep the provider's first-listed variant as the family's baseline default
        # when the user has not yet recorded a family-specific preference.
        default_slug = items[0].slug
        has_effort_axis = all(bool(item.effort) for item in items)
        if has_effort_axis:
            ordered = tuple(sorted(items, key=lambda item: (EFFORT_RANK.get(item.effort, 99), item.order)))
        else:
            ordered = tuple(items)
        adjustable = len(ordered) > 1
        families.append(
            ModelFamily(
                name=family_name,
                variants=ordered,
                adjustable=adjustable,
                has_effort_axis=has_effort_axis,
                default_variant_slug=default_slug,
                quota_group=quota_group_for_model(family_name),
            )
        )

    return Catalog(
        route=route,
        fetched_at=fetched_at,
        binary_signature=binary_sig,
        variants=tuple(variants),
        families=tuple(families),
        source=source,
    )


def catalog_cache_path(route: str, environment: dict[str, str] | None = None) -> Path:
    safe_route = re.sub(r"[^a-zA-Z0-9_-]", "_", route) or "plan"
    return state_dir(environment) / f"catalog-{safe_route}.json"


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = None
    try:
        fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        temporary = Path(name)
        os.chmod(temporary, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, separators=(",", ":"))
            stream.write("\n")
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def load_cached_catalog(
    route: str,
    *,
    environment: dict[str, str] | None = None,
) -> Catalog | None:
    path = catalog_cache_path(route, environment)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("version") != 1:
        return None
    items = data.get("items")
    if not isinstance(items, list) or not items:
        return None
    rows: list[tuple[str, str]] = []
    for item in items:
        if (
            isinstance(item, dict)
            and isinstance(item.get("slug"), str)
            and isinstance(item.get("display_name"), str)
        ):
            rows.append((item["slug"], item["display_name"]))
    if not rows:
        return None
    fetched_at = float(data.get("fetched_at") or 0.0)
    sig = str(data.get("binary_signature") or "")
    return build_catalog(rows, route=route, fetched_at=fetched_at, binary_sig=sig, source="cache")


def save_cached_catalog(
    catalog: Catalog,
    *,
    environment: dict[str, str] | None = None,
) -> None:
    path = catalog_cache_path(catalog.route, environment)
    payload = {
        "version": 1,
        "route": catalog.route,
        "fetched_at": catalog.fetched_at,
        "binary_signature": catalog.binary_signature,
        "items": [
            {"slug": item.slug, "display_name": item.display_name}
            for item in catalog.variants
        ],
    }
    atomic_write_json(path, payload)


def is_cache_stale(
    catalog: Catalog,
    current_binary_sig: str,
    *,
    now: float | None = None,
    ttl: float = DEFAULT_CACHE_TTL_SECONDS,
) -> bool:
    current_time = time.time() if now is None else now
    if current_binary_sig and catalog.binary_signature != current_binary_sig:
        return True
    return (current_time - catalog.fetched_at) >= ttl


def fetch_live_catalog(
    binary: Path,
    *,
    route: str,
    extra_args: list[str] | None = None,
    env: dict[str, str] | None = None,
    timeout: float = 12.0,
    environment_for_cache: dict[str, str] | None = None,
    persist: bool = True,
) -> Catalog:
    """Invoke `<agy> [extra_args...] models` under a single-flight lock and cache."""
    lock_path = catalog_cache_path(route, environment_for_cache).with_suffix(".lock")
    lock_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with lock_path.open("a", encoding="utf-8") as lock_stream:
        try:
            os.chmod(lock_path, 0o600)
        except OSError:
            pass
        fcntl.flock(lock_stream, fcntl.LOCK_EX)
        command = [str(binary), *(extra_args or []), "models"]
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
            check=False,
        )
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "").strip().splitlines()
            reason = detail[-1] if detail else f"exit {completed.returncode}"
            raise ValueError(f"agy models 失败（{reason}）")
        rows = parse_models_output(completed.stdout)
        if not rows:
            raise ValueError("agy models 返回了空模型列表")
        catalog = build_catalog(
            rows,
            route=route,
            fetched_at=time.time(),
            binary_sig=binary_signature(binary),
            source="live",
        )
        if persist:
            save_cached_catalog(catalog, environment=environment_for_cache)
        return catalog


def try_background_refresh(
    launcher_script: Path,
    *,
    route: str,
    environment: dict[str, str] | None = None,
) -> bool:
    """Spawn a detached single-flight refresh process if no refresh is running."""
    env = dict(os.environ if environment is None else environment)
    lock_path = catalog_cache_path(route, env).with_suffix(".lock")
    lock_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        with lock_path.open("a", encoding="utf-8") as lock_stream:
            try:
                fcntl.flock(lock_stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return False
            fcntl.flock(lock_stream, fcntl.LOCK_UN)
        subprocess.Popen(
            [sys.executable, str(launcher_script), "--_refresh-catalog", route],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
            start_new_session=True,
        )
        return True
    except OSError:
        return False


def fallback_catalog(
    route: str,
    *,
    settings_model: str = "",
    remembered_slug: str = "",
    binary_sig: str = "",
) -> Catalog:
    """Minimal offline fallback when no cache exists and network fetch failed."""
    candidates: list[tuple[str, str]] = []
    for raw in (remembered_slug, settings_model):
        value = raw.strip()
        if not value:
            continue
        slug = re.sub(r"[^a-z0-9.+-]+", "-", value.lower()).strip("-")
        if slug and not any(item[0] == slug for item in candidates):
            candidates.append((slug, value))
    if not candidates:
        candidates.append(("gemini-4-neon-high", "Gemini 4 Neon (High)"))
    return build_catalog(
        candidates,
        route=route,
        fetched_at=0.0,
        binary_sig=binary_sig,
        source="fallback",
    )


def quota_snapshot_path(environment: dict[str, str] | None = None) -> Path:
    return state_dir(environment) / "quota-snapshot.json"


def _numeric(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def normalize_quota_payload(payload: dict[str, Any], *, now: float | None = None) -> dict[str, Any] | None:
    """Extract only bucket fractions/amounts and absolute UTC reset timestamps."""
    raw_quota = payload.get("quota")
    if not isinstance(raw_quota, dict) or not raw_quota:
        return None
    observed_at = time.time() if now is None else now
    buckets: dict[str, dict[str, Any]] = {}
    for key, bucket in raw_quota.items():
        if not isinstance(key, str) or not isinstance(bucket, dict) or bucket.get("disabled") is True:
            continue
        entry: dict[str, Any] = {}
        if _numeric(bucket.get("remaining_fraction")):
            entry["remaining_fraction"] = max(0.0, min(1.0, float(bucket["remaining_fraction"])))
        elif _numeric(bucket.get("remaining_amount")):
            entry["remaining_amount"] = max(0.0, float(bucket["remaining_amount"]))
        else:
            continue
        reset_at: float | None = None
        if _numeric(bucket.get("reset_in_seconds")):
            reset_at = observed_at + max(0.0, float(bucket["reset_in_seconds"]))
        elif isinstance(bucket.get("reset_time"), str):
            try:
                moment = datetime.fromisoformat(bucket["reset_time"].replace("Z", "+00:00"))
                reset_at = moment.timestamp()
            except ValueError:
                reset_at = None
        if reset_at is not None:
            entry["reset_at"] = reset_at
        buckets[key] = entry
    if not buckets:
        return None
    result: dict[str, Any] = {
        "version": 1,
        "observed_at": observed_at,
        "buckets": buckets,
    }
    plan_tier = payload.get("plan_tier")
    if isinstance(plan_tier, str) and plan_tier.strip():
        result["plan_tier"] = plan_tier.strip()
    return result


def load_quota_snapshot(environment: dict[str, str] | None = None) -> dict[str, Any] | None:
    path = quota_snapshot_path(environment)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("buckets"), dict):
        return None
    return data


def _format_duration(seconds: float) -> str:
    seconds = max(1.0, seconds)
    for unit, suffix in ((86400, "d"), (3600, "h"), (60, "m"), (1, "s")):
        if seconds >= unit or unit == 1:
            return f"{max(1, int(seconds // unit))}{suffix}"
    return "1s"


def _format_age(seconds: float) -> str:
    if seconds < 60:
        return "刚刚"
    return f"{_format_duration(seconds)}前"


def format_quota_for_group(
    group: str,
    snapshot: dict[str, Any] | None,
    *,
    now: float | None = None,
) -> tuple[str, str]:
    """Return (compact_column_text, detailed_text) honestly reflecting staleness.

    Never synthesizes 100% after reset_at because another parallel session may
    already have consumed quota since the reset boundary.
    """
    if not snapshot:
        return "", ""
    current_time = time.time() if now is None else now
    observed_at = float(snapshot.get("observed_at") or 0.0)
    if observed_at <= 0:
        return "", ""
    age = max(0.0, current_time - observed_at)
    buckets = snapshot.get("buckets") or {}

    if group == "argon":
        keys = [("gemini-argon-wtu-weekly", "周")]
        label = "Argon"
    elif group == "3p":
        keys = [("3p-5h", "5h"), ("3p-weekly", "周")]
        label = "3P"
    else:
        keys = [("gemini-5h", "5h"), ("gemini-weekly", "周")]
        label = "Gemini"

    present = [(k, period, buckets[k]) for k, period in keys if isinstance(buckets.get(k), dict)]
    if not present:
        return "", ""

    if age > QUOTA_STALE_SECONDS:
        return f"{label} 待刷新", f"{label} 配额快照已过期（{_format_age(age)}，启动后刷新）"

    compact_parts: list[str] = []
    detail_parts: list[str] = []
    for _, period, entry in present:
        reset_at = entry.get("reset_at")
        if _numeric(reset_at) and current_time >= float(reset_at):
            compact_parts.append(f"{period}待刷新")
            detail_parts.append(f"{period}已过重置时间，待刷新")
            continue
        if _numeric(entry.get("remaining_fraction")):
            pct = f"{round(float(entry['remaining_fraction']) * 100)}%"
        elif _numeric(entry.get("remaining_amount")):
            pct = f"{float(entry['remaining_amount']):g}"
        else:
            continue
        compact_parts.append(f"{period}{pct}" if period == "周" and len(present) == 1 else pct)
        detail = f"{period}剩 {pct}"
        if _numeric(reset_at) and float(reset_at) > current_time:
            detail += f" (↻{_format_duration(float(reset_at) - current_time)})"
        detail_parts.append(detail)

    if not compact_parts:
        return "", ""
    compact = f"{label} " + "/".join(compact_parts)
    detail_text = f"{label} " + " · ".join(detail_parts) + f"（{_format_age(age)}）"
    return compact, detail_text
