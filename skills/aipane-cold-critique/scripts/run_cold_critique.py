#!/usr/bin/env python3
"""
零前情双边对抗冷审执行脚本（零第三方依赖，仅使用 Python 标准库）。

支持并发调用以下独立、零上下文后端（CLI 均在每次新建的独立空临时目录中运行）：
  - codex:  本机 `codex exec`（-s read-only --ephemeral + 提示词禁工具）
  - claude: 本机 `claude -p`（硬禁工具 --tools "" + --strict-mcp-config + --no-session-persistence）
  - agy:    本机 `agy --print`（--disable-slash-commands + 提示词禁工具）
  - openai: 任意 OpenAI 兼容 Chat Completions API（硬禁工具，无 tools 字段）

可通过 COLD_CRITIQUE_CHANNELS 环境变量配置默认通道列表（逗号或分号分隔）。
"""

from __future__ import annotations

import argparse
import concurrent.futures
import dataclasses
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from typing import Any

CALIBRATED_PROMPT_PREFIX = (
    "仅基于下方这段文字做纯文本分析（不要调用任何工具、不要查阅本机文件或历史记录）。\n\n"
    "找找这段总结里，哪几个概念合并是硬凑的、哪些因果链条经不起推敲，给出最尖锐的反驳；"
    "如果某部分经得起推敲就直接说明成立，不要为了挑刺而强行编造缺陷。\n\n"
    "---\n\n"
)

SUPPORTED_BACKENDS = ("codex", "claude", "agy", "openai")
DEFAULT_OPENAI_BASE_URL = "https://api.openai.com/v1"

_TRAILING_SECTION_RE = re.compile(
    r"\n##\s+(?:参考资料|参考文献|发布前检查|References|Further Reading|Checklist|Pre-publish Checklist)\b",
    re.IGNORECASE,
)
_MD_LINK_RE = re.compile(r"(?<!!)\[([^\]]+)\]\(https?://[^)]+\)")
_MD_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\(https?://[^)]+\)")
_HOME_PATH_RE = re.compile(
    r"(?:/Users/[^/\s`'\"<>]+|/home/[^/\s`'\"<>]+|[A-Za-z]:\\Users\\[^\\\s`'\"<>]+)"
)
_ENV_PREFIX_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
_EFFORT_RE = re.compile(r"^[a-zA-Z0-9_-]+$")


@dataclasses.dataclass(frozen=True)
class ChannelSpec:
    raw: str
    backend: str
    model: str | None = None
    effort: str | None = None
    env_prefix: str | None = None

    @property
    def label(self) -> str:
        parts = [self.backend]
        if self.model:
            parts.append(f":{self.model}")
        if self.effort:
            parts.append(f"@{self.effort}")
        if self.env_prefix:
            parts.append(f"#{self.env_prefix}")
        return "".join(parts)


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
        left, candidate = spec.rsplit("#", 1)
        if not _ENV_PREFIX_RE.match(candidate):
            raise ValueError(f"无效的环境变量前缀 '{candidate}'（需大写字母、数字或下划线）。")
        spec = left
        env_prefix = candidate

    if "@" in spec:
        left, candidate = spec.rsplit("@", 1)
        if not _EFFORT_RE.match(candidate):
            raise ValueError(f"无效的推理强度标记 '@{candidate}'。")
        spec = left
        effort = candidate

    if ":" in spec:
        backend, model = spec.split(":", 1)
        model = model.strip() or None
    else:
        backend, model = spec, None

    backend = backend.strip().lower()
    if backend not in SUPPORTED_BACKENDS:
        allowed = ", ".join(SUPPORTED_BACKENDS)
        raise ValueError(f"不支持的后端 '{backend}'，可选：{allowed}。")

    if backend != "openai" and env_prefix is not None:
        raise ValueError(f"只有 openai 后端支持 '#{env_prefix}' 环境变量前缀。")

    return ChannelSpec(
        raw=raw,
        backend=backend,
        model=model,
        effort=effort,
        env_prefix=env_prefix,
    )


def resolve_openai_env(spec: ChannelSpec, env: dict[str, str] | None = None) -> tuple[str, str, str]:
    """解析 OpenAI 兼容后端所需的 (base_url, api_key, model)。"""
    source = os.environ if env is None else env
    if spec.env_prefix:
        prefix = spec.env_prefix
        base_url = source.get(f"{prefix}_BASE_URL", "").strip() or DEFAULT_OPENAI_BASE_URL
        api_key = source.get(f"{prefix}_API_KEY", "").strip()
        model = spec.model or source.get(f"{prefix}_MODEL", "").strip()
        key_hint = f"{prefix}_API_KEY"
        model_hint = f"openai:<model>#{prefix} 或设置 {prefix}_MODEL"
    else:
        base_url = (
            source.get("COLD_CRITIQUE_BASE_URL", "").strip()
            or source.get("OPENAI_BASE_URL", "").strip()
            or DEFAULT_OPENAI_BASE_URL
        )
        api_key = (
            source.get("COLD_CRITIQUE_API_KEY", "").strip()
            or source.get("OPENAI_API_KEY", "").strip()
        )
        model = (
            spec.model
            or source.get("COLD_CRITIQUE_OPENAI_MODEL", "").strip()
            or source.get("OPENAI_MODEL", "").strip()
        )
        key_hint = "COLD_CRITIQUE_API_KEY / OPENAI_API_KEY"
        model_hint = "openai:<model> 或设置 COLD_CRITIQUE_OPENAI_MODEL / OPENAI_MODEL"

    if not api_key:
        raise ValueError(f"缺少 API 密钥，请设置环境变量 {key_hint}。")
    if not model:
        raise ValueError(f"缺少模型名称，请指定 {model_hint}。")

    return base_url.rstrip("/"), api_key, model


def detect_available_backends(env: dict[str, str] | None = None) -> list[dict[str, Any]]:
    """探测当前环境可用的后端通道（纯本地检查，不发起网络请求）。"""
    source = os.environ if env is None else env
    results: list[dict[str, Any]] = []

    for cli in ("codex", "claude", "agy"):
        path = shutil.which(cli)
        results.append(
            {
                "backend": cli,
                "available": bool(path),
                "detail": path or "未在 PATH 中找到",
            }
        )

    has_key = bool(source.get("COLD_CRITIQUE_API_KEY") or source.get("OPENAI_API_KEY"))
    model = (
        source.get("COLD_CRITIQUE_OPENAI_MODEL", "").strip()
        or source.get("OPENAI_MODEL", "").strip()
    )
    base_url = (
        source.get("COLD_CRITIQUE_BASE_URL", "").strip()
        or source.get("OPENAI_BASE_URL", "").strip()
        or DEFAULT_OPENAI_BASE_URL
    )
    openai_ready = has_key and bool(model)
    if openai_ready:
        detail = f"已配置模型 {model}（{base_url}）"
    elif has_key:
        detail = f"已检测到 API Key，显式传 -c openai:<model> 即可使用（{base_url}）"
    else:
        detail = "未设置 OPENAI_API_KEY / COLD_CRITIQUE_API_KEY"

    results.append(
        {
            "backend": "openai",
            "available": openai_ready,
            "detail": detail,
            "default_spec": f"openai:{model}" if openai_ready else None,
        }
    )
    return results


def parse_env_channels(env: dict[str, str] | None = None) -> list[ChannelSpec]:
    """从 COLD_CRITIQUE_CHANNELS 环境变量解析默认通道列表（逗号或分号分隔）。"""
    source = os.environ if env is None else env
    raw = source.get("COLD_CRITIQUE_CHANNELS", "").strip()
    if not raw:
        return []
    parts = [item.strip() for item in re.split(r"[,;]", raw) if item.strip()]
    return [parse_channel_spec(part) for part in parts]


def auto_select_channels(env: dict[str, str] | None = None) -> list[ChannelSpec]:
    """未指定 --channel 时，自动选择最多两个不同后端以构成异构双通道。"""
    detected = detect_available_backends(env=env)
    chosen: list[ChannelSpec] = []
    for item in detected:
        if not item["available"]:
            continue
        raw = item.get("default_spec") or item["backend"]
        chosen.append(parse_channel_spec(raw))
        if len(chosen) >= 2:
            break
    return chosen


def build_codex_command(spec: ChannelSpec, out_path: str) -> list[str]:
    cmd = [
        "codex",
        "exec",
        "--skip-git-repo-check",
        "-s",
        "read-only",
        "--ephemeral",
    ]
    if spec.model:
        cmd.extend(["-m", spec.model])
    if spec.effort:
        cmd.extend(["-c", f'model_reasoning_effort="{spec.effort}"'])
    cmd.extend(["-o", out_path, "-"])
    return cmd


def build_claude_command(spec: ChannelSpec) -> list[str]:
    cmd = [
        "claude",
        "-p",
        "--tools",
        "",
        "--strict-mcp-config",
        "--disable-slash-commands",
        "--no-session-persistence",
        "--output-format",
        "json",
    ]
    if spec.model:
        cmd.extend(["--model", spec.model])
    if spec.effort:
        cmd.extend(["--effort", spec.effort])
    return cmd


def build_agy_command(spec: ChannelSpec, full_prompt: str) -> list[str]:
    cmd = [
        "agy",
        "--disable-slash-commands",
        "--output-format",
        "json",
    ]
    if spec.model:
        cmd.extend(["--model", spec.model])
    if spec.effort:
        cmd.extend(["--effort", spec.effort])
    cmd.append(f"--print={full_prompt}")
    return cmd


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


def _run_codex(spec: ChannelSpec, full_prompt: str, timeout_s: int) -> dict[str, Any]:
    t0 = time.time()
    with tempfile.NamedTemporaryFile(prefix="cold_critique_codex_", suffix=".txt", delete=False) as tf:
        out_path = tf.name

    cmd = build_codex_command(spec, out_path)
    try:
        with tempfile.TemporaryDirectory(prefix="cold_critique_work_") as workdir:
            proc = subprocess.run(
                cmd,
                input=full_prompt,
                cwd=workdir,
                capture_output=True,
                text=True,
                timeout=timeout_s,
            )
        elapsed = round(time.time() - t0, 2)
        resp = ""
        if os.path.exists(out_path):
            with open(out_path, "r", encoding="utf-8") as f:
                resp = f.read().strip()
        if proc.returncode == 0 and resp:
            return {
                "channel": spec.label,
                "backend": "codex",
                "model": spec.model or "default",
                "ok": True,
                "duration_seconds": elapsed,
                "chars": len(resp),
                "response": resp,
            }
        return {
            "channel": spec.label,
            "backend": "codex",
            "model": spec.model or "default",
            "ok": False,
            "duration_seconds": elapsed,
            "error": f"rc={proc.returncode} stderr={proc.stderr.strip()[:400]} stdout={proc.stdout.strip()[:400]}",
        }
    except Exception as exc:
        return {
            "channel": spec.label,
            "backend": "codex",
            "model": spec.model or "default",
            "ok": False,
            "duration_seconds": round(time.time() - t0, 2),
            "error": str(exc),
        }
    finally:
        if os.path.exists(out_path):
            try:
                os.remove(out_path)
            except OSError:
                pass


def _run_claude(spec: ChannelSpec, full_prompt: str, timeout_s: int) -> dict[str, Any]:
    t0 = time.time()
    cmd = build_claude_command(spec)
    try:
        with tempfile.TemporaryDirectory(prefix="cold_critique_work_") as workdir:
            proc = subprocess.run(
                cmd,
                input=full_prompt,
                cwd=workdir,
                capture_output=True,
                text=True,
                timeout=timeout_s,
            )
        elapsed = round(time.time() - t0, 2)
        raw_out = proc.stdout.strip()
        if proc.returncode == 0 and raw_out:
            parsed = _parse_last_json_object(raw_out)
            if parsed is not None:
                resp = str(parsed.get("result") or parsed.get("response") or "").strip()
                is_error = bool(parsed.get("is_error"))
                if resp and not is_error:
                    result: dict[str, Any] = {
                        "channel": spec.label,
                        "backend": "claude",
                        "model": spec.model or "default",
                        "ok": True,
                        "duration_seconds": elapsed,
                        "chars": len(resp),
                        "response": resp,
                    }
                    usage = parsed.get("usage")
                    if isinstance(usage, dict):
                        result["usage"] = usage
                    return result
            else:
                return {
                    "channel": spec.label,
                    "backend": "claude",
                    "model": spec.model or "default",
                    "ok": True,
                    "duration_seconds": elapsed,
                    "chars": len(raw_out),
                    "response": raw_out,
                }
        return {
            "channel": spec.label,
            "backend": "claude",
            "model": spec.model or "default",
            "ok": False,
            "duration_seconds": elapsed,
            "error": f"rc={proc.returncode} stderr={proc.stderr.strip()[:400]} stdout={raw_out[:400]}",
        }
    except Exception as exc:
        return {
            "channel": spec.label,
            "backend": "claude",
            "model": spec.model or "default",
            "ok": False,
            "duration_seconds": round(time.time() - t0, 2),
            "error": str(exc),
        }


def _run_agy(spec: ChannelSpec, full_prompt: str, timeout_s: int) -> dict[str, Any]:
    t0 = time.time()
    cmd = build_agy_command(spec, full_prompt)
    try:
        with tempfile.TemporaryDirectory(prefix="cold_critique_work_") as workdir:
            proc = subprocess.run(
                cmd,
                cwd=workdir,
                capture_output=True,
                text=True,
                timeout=timeout_s,
            )
        elapsed = round(time.time() - t0, 2)
        raw_out = proc.stdout.strip()
        if proc.returncode == 0 and raw_out:
            parsed = _parse_last_json_object(raw_out)
            if parsed and parsed.get("status") == "SUCCESS":
                resp = str(parsed.get("response") or "").strip()
                if resp:
                    result: dict[str, Any] = {
                        "channel": spec.label,
                        "backend": "agy",
                        "model": spec.model or "default",
                        "ok": True,
                        "duration_seconds": round(float(parsed.get("duration_seconds", elapsed)), 2),
                        "chars": len(resp),
                        "response": resp,
                    }
                    usage = parsed.get("usage")
                    if isinstance(usage, dict):
                        result["usage"] = usage
                    return result
        return {
            "channel": spec.label,
            "backend": "agy",
            "model": spec.model or "default",
            "ok": False,
            "duration_seconds": elapsed,
            "error": f"rc={proc.returncode} stderr={proc.stderr.strip()[:400]} stdout={raw_out[:400]}",
        }
    except Exception as exc:
        return {
            "channel": spec.label,
            "backend": "agy",
            "model": spec.model or "default",
            "ok": False,
            "duration_seconds": round(time.time() - t0, 2),
            "error": str(exc),
        }


def build_openai_payload(model: str, full_prompt: str, effort: str | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": model,
        "messages": [{"role": "user", "content": full_prompt}],
    }
    if effort:
        payload["reasoning_effort"] = effort
    return payload


def extract_openai_text(data: dict[str, Any]) -> str:
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ValueError("API 响应中缺少 choices 字段。")
    message = choices[0].get("message") or {}
    content = message.get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = [
            item.get("text", "")
            for item in content
            if isinstance(item, dict) and item.get("type") in ("text", "output_text")
        ]
        return "".join(parts).strip()
    raise ValueError("无法从 choices[0].message.content 中提取文本。")


def _run_openai(spec: ChannelSpec, full_prompt: str, timeout_s: int) -> dict[str, Any]:
    t0 = time.time()
    try:
        base_url, api_key, model = resolve_openai_env(spec)
    except ValueError as exc:
        return {
            "channel": spec.label,
            "backend": "openai",
            "model": spec.model or "unset",
            "ok": False,
            "duration_seconds": 0.0,
            "error": str(exc),
        }

    url = f"{base_url}/chat/completions"
    payload = build_openai_payload(model=model, full_prompt=full_prompt, effort=spec.effort)
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp_obj:
            raw_body = resp_obj.read().decode("utf-8", errors="replace")
        elapsed = round(time.time() - t0, 2)
        parsed = json.loads(raw_body)
        text = extract_openai_text(parsed)
        if not text:
            raise ValueError("模型返回了空正文。")
        result: dict[str, Any] = {
            "channel": spec.label,
            "backend": "openai",
            "model": str(parsed.get("model") or model),
            "ok": True,
            "duration_seconds": elapsed,
            "chars": len(text),
            "response": text,
        }
        usage = parsed.get("usage")
        if isinstance(usage, dict):
            result["usage"] = usage
        return result
    except urllib.error.HTTPError as exc:
        elapsed = round(time.time() - t0, 2)
        err_body = ""
        try:
            err_body = exc.read().decode("utf-8", errors="replace")[:400]
        except Exception:
            pass
        return {
            "channel": spec.label,
            "backend": "openai",
            "model": model,
            "ok": False,
            "duration_seconds": elapsed,
            "error": f"HTTP {exc.code}: {err_body}",
        }
    except Exception as exc:
        return {
            "channel": spec.label,
            "backend": "openai",
            "model": model,
            "ok": False,
            "duration_seconds": round(time.time() - t0, 2),
            "error": str(exc),
        }


def run_channel(spec: ChannelSpec, full_prompt: str, timeout_s: int) -> dict[str, Any]:
    if spec.backend == "codex":
        return _run_codex(spec, full_prompt, timeout_s)
    if spec.backend == "claude":
        return _run_claude(spec, full_prompt, timeout_s)
    if spec.backend == "agy":
        return _run_agy(spec, full_prompt, timeout_s)
    if spec.backend == "openai":
        return _run_openai(spec, full_prompt, timeout_s)
    raise ValueError(f"未知后端：{spec.backend}")


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="零前情、禁工具的多通道对抗冷审执行器（支持 codex / claude / agy / openai 兼容 API）。"
    )
    parser.add_argument("--file", "-f", help="待审 Markdown/文本文件路径（省略时从 stdin 读取）")
    parser.add_argument(
        "--channel",
        "-c",
        action="append",
        dest="channels",
        help=(
            "指定评审通道，可重复传入多个以并发执行。"
            "格式：<backend>[:<model>][@<effort>][#<ENV_PREFIX>]，"
            "例如 codex@high、claude:opus@high、openai:gpt-4.1、openai:deepseek-reasoner#DEEPSEEK。"
            "省略时优先读取 COLD_CRITIQUE_CHANNELS 环境变量，否则自动探测最多两个可用后端。"
        ),
    )
    parser.add_argument("--prompt-file", help="可选自定义评审前缀提示词文件路径（默认使用内置双边校准提示词）")
    parser.add_argument("--raw", action="store_true", help="跳过剥壳，保留原始 frontmatter、参考资料与外链")
    parser.add_argument("--redact-paths", action="store_true", help="将文本中的本机用户目录路径替换为 <HOME>")
    parser.add_argument("--timeout", type=int, default=300, help="单通道超时时间（秒，默认 300）")
    parser.add_argument("--json", action="store_true", help="以 JSON 格式输出结果")
    parser.add_argument("--dry-run", action="store_true", help="仅打印剥壳统计与已选通道，不实际调用模型")
    parser.add_argument("--list-backends", action="store_true", help="检查当前环境可用的后端通道并退出")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    if args.list_backends:
        detected = detect_available_backends()
        env_raw = os.environ.get("COLD_CRITIQUE_CHANNELS", "").strip()
        if args.json:
            info_out: dict[str, Any] = {"backends": detected}
            if env_raw:
                info_out["env_channels"] = env_raw
            print(json.dumps(info_out, ensure_ascii=False, indent=2))
        else:
            for item in detected:
                mark = "可用" if item["available"] else "未就绪"
                print(f"- {item['backend']:<7} [{mark}] {item['detail']}")
            if env_raw:
                print(f"- 默认通道 (COLD_CRITIQUE_CHANNELS): {env_raw}")
        return 0

    if args.channels:
        try:
            specs = [parse_channel_spec(c) for c in args.channels]
        except ValueError as exc:
            parser.error(str(exc))
    else:
        try:
            specs = parse_env_channels()
        except ValueError as exc:
            parser.error(f"COLD_CRITIQUE_CHANNELS 配置无效：{exc}")
        if not specs:
            specs = auto_select_channels()
        if not specs:
            parser.error(
                "未自动探测到可用后端。请安装 codex/claude/agy CLI，配置 COLD_CRITIQUE_CHANNELS，或配置 OPENAI_API_KEY 后显式传入 -c openai:<model>。"
            )

    if args.file:
        with open(args.file, "r", encoding="utf-8") as f:
            raw_text = f.read()
    else:
        if sys.stdin.isatty():
            parser.error("请通过 --file <路径> 指定待审文件，或通过标准输入传入文本。")
        raw_text = sys.stdin.read()

    if args.prompt_file:
        with open(args.prompt_file, "r", encoding="utf-8") as f:
            prompt_prefix = f.read().rstrip() + "\n\n---\n\n"
    else:
        prompt_prefix = CALIBRATED_PROMPT_PREFIX

    if args.raw:
        clean_text = _HOME_PATH_RE.sub("<HOME>", raw_text.strip()) if args.redact_paths else raw_text.strip()
    else:
        clean_text = strip_markdown_shell(raw_text, redact_paths=args.redact_paths)

    if not clean_text:
        parser.error("剥壳后的待审正文为空。")

    full_prompt = prompt_prefix + clean_text

    if args.dry_run:
        info = {
            "raw_chars": len(raw_text),
            "clean_chars": len(clean_text),
            "prompt_chars": len(full_prompt),
            "channels": [s.label for s in specs],
            "single_channel_warning": len(specs) == 1,
            "preview_head": full_prompt[:280],
            "preview_tail": full_prompt[-200:],
        }
        print(json.dumps(info, ensure_ascii=False, indent=2))
        return 0

    with concurrent.futures.ThreadPoolExecutor(max_workers=len(specs)) as ex:
        futures = [ex.submit(run_channel, spec, full_prompt, args.timeout) for spec in specs]
        results = [f.result() for f in futures]

    any_ok = any(r.get("ok") for r in results)

    if args.json:
        print(
            json.dumps(
                {
                    "input_chars": len(clean_text),
                    "channels_count": len(specs),
                    "results": results,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0 if any_ok else 1

    if len(specs) == 1:
        print("提示：当前仅运行了单通道冷审；若需异构交叉校验，请再追加一个不同模型通道（-c）。\n")

    for res in results:
        ch = res["channel"]
        model = res["model"]
        print("=" * 70)
        if res["ok"]:
            print(f"[{ch}] model={model} | duration={res['duration_seconds']}s | chars={res['chars']}")
            print("=" * 70)
            print(res["response"])
            print()
        else:
            print(f"[{ch}] FAILED ({res['duration_seconds']}s)")
            print("=" * 70)
            print(res.get("error"))
            print()

    return 0 if any_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
