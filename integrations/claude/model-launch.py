#!/usr/bin/env python3
"""Start Claude Code with a context window matched to the chosen model."""

from __future__ import annotations

import copy
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
from pathlib import Path

from model_picker import select_model


HOME = Path.home()
CONFIG = Path(os.environ.get("CLAUDE_CONFIG_DIR", HOME / ".claude")).expanduser()
NATIVE_CLAUDE = HOME / ".local/bin/claude"
MODEL_METADATA = CONFIG / "model-routing.json"
MANAGEMENT_COMMANDS = {
    "agents", "attach", "auth", "doctor", "install", "logs", "mcp", "plugin",
    "remote-control", "rm", "setup-token", "stop", "update", "upgrade",
}
ALIASES = {
    "opus": "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "sonnet": "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "haiku": "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    "fable": "ANTHROPIC_DEFAULT_FABLE_MODEL",
}


def read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError) as error:
        raise ValueError(f"无法读取 {path}: {error}") from error
    if not isinstance(data, dict):
        raise ValueError(f"{path} 必须是 JSON 对象")
    return data


def remembered_model(options: list[dict], models: dict) -> str | None:
    """Only offer a remembered choice that is still in the current menu."""
    try:
        model = read_json(CONFIG / "model-picker-state.json").get("last_model")
    except ValueError:
        return None
    if (isinstance(model, str) and model in models
            and any(option.get("model") == model for option in options)):
        return model
    return None


def remember_model(model: str) -> None:
    """Atomically replace the last confirmed choice, independently of settings."""
    temporary = None
    try:
        fd, filename = tempfile.mkstemp(prefix=".model-picker-", suffix=".json", dir=CONFIG)
        temporary = Path(filename)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump({"last_model": model}, stream)
            stream.write("\n")
        temporary.replace(CONFIG / "model-picker-state.json")
    except OSError as error:
        print(f"claude-window: 无法记住本次选择（{error}）；仍使用 {model} 启动", file=sys.stderr)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def canonical_model(value: str, settings: dict) -> str:
    model = value.strip().lower()
    if model.endswith("[1m]"):
        model = model[:-4]
    model = settings.get("env", {}).get(ALIASES.get(model, ""), model)
    if model.startswith("claude-fable-5-dd-"):
        model = model.removeprefix("claude-fable-5-dd-")[::-1]
    return {"grok-4.7-build": "grok-4.7", "deepseek-v4-flash": "deepseek-flash"}.get(model, model)


def take_option(args: list[str], option: str) -> tuple[list[str], str | None]:
    result = []
    value = None
    iterator = iter(args)
    for arg in iterator:
        if arg == option:
            try:
                value = next(iterator)
            except StopIteration as error:
                raise ValueError(f"{option} 后面缺少值") from error
        elif arg.startswith(option + "="):
            value = arg.split("=", 1)[1]
        else:
            result.append(arg)
    return result, value


def load_extra_settings(value: str | None) -> dict:
    if value is None:
        return {}
    stripped = value.lstrip()
    if stripped.startswith("{"):
        try:
            data = json.loads(value)
        except ValueError as error:
            raise ValueError(f"--settings 不是有效的 JSON: {error}") from error
        if not isinstance(data, dict):
            raise ValueError("--settings 必须是 JSON 对象")
        return data
    return read_json(Path(value).expanduser())


def resume_model(args: list[str], settings: dict) -> str | None:
    """Recover the last requested model without exposing transcript contents."""
    projects = CONFIG / "projects"
    path = None
    for index, arg in enumerate(args):
        if arg in ("--continue", "-c"):
            project = re.sub(r"[^A-Za-z0-9-]", "-", str(Path.cwd().resolve()))
            candidates = list((projects / project).glob("*.jsonl"))
            if candidates:
                path = max(candidates, key=lambda item: item.stat().st_mtime_ns)
            break
        if arg in ("--resume", "-r"):
            identifier = args[index + 1] if index + 1 < len(args) else ""
        elif arg.startswith("--resume="):
            identifier = arg.split("=", 1)[1]
        else:
            continue
        if re.fullmatch(r"[0-9a-fA-F-]{8,36}", identifier):
            candidates = list(projects.glob("*/" + identifier + "*.jsonl"))
            if len(candidates) == 1:
                path = candidates[0]
        break
    if path is None:
        return None
    try:
        with path.open("rb") as stream:
            stream.seek(0, os.SEEK_END)
            size = stream.tell()
            stream.seek(max(0, size - 32 * 1024 * 1024))
            lines = stream.read().splitlines()
        for line in reversed(lines):
            try:
                item = json.loads(line)
            except (ValueError, UnicodeDecodeError):
                continue
            if not isinstance(item, dict):
                continue
            if item.get("type") != "assistant":
                continue
            message = item.get("message") or {}
            candidate = item.get("requestedModel") or (message.get("model") if isinstance(message, dict) else None)
            if isinstance(candidate, str) and candidate not in ("<synthetic>", "unknown"):
                return canonical_model(candidate, settings)
    except OSError:
        return None
    return None


def make_overlay(settings: dict, metadata: dict, extra: dict, model: str) -> dict:
    models = metadata["models"]
    window = models[model]["context_tokens"]
    compatible = {name for name, row in models.items() if row.get("context_tokens") == window}
    overlay = copy.deepcopy(extra)
    env = overlay.setdefault("env", {})
    if not isinstance(env, dict):
        raise ValueError("--settings 中的 env 必须是对象")
    env["ANTHROPIC_MODEL"] = model
    env["CLAUDE_CODE_MAX_CONTEXT_TOKENS"] = str(window)
    # Keep the existing GPT subagent where it has the same window. Otherwise
    # use the selected model so every subagent uses the session's window.
    subagent = "gpt-6.1-sol" if "gpt-6.1-sol" in compatible else model
    env["CLAUDE_CODE_SUBAGENT_MODEL"] = subagent
    env["CLAUDE_CODE_SUBAGENT_MODEL_FORCE"] = "1"
    for family in ALIASES.values():
        requested = canonical_model(str(env.get(family, settings.get("env", {}).get(family, model))), settings)
        if requested not in compatible:
            env[family] = model
    overlay["model"] = model
    picker = settings.get("modelPicker", {})
    options = [copy.deepcopy(item) for item in picker.get("options", []) if item.get("model") in compatible]
    overlay["modelPicker"] = {"options": options, "replaceBuiltInOptions": True}
    return overlay


def run_with_private_settings(argv: list[str], overlay: dict) -> int:
    # An incoming settings file can contain credentials. Keep it out of argv.
    fd, filename = tempfile.mkstemp(prefix="claude-window-", suffix=".json")
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(overlay, stream, ensure_ascii=False)
        command = [str(NATIVE_CLAUDE), *argv, "--settings", filename]
        previous = signal.signal(signal.SIGINT, signal.SIG_IGN)
        try:
            def reset_signals():
                signal.signal(signal.SIGINT, signal.SIG_DFL)
            return subprocess.call(command, preexec_fn=reset_signals)
        finally:
            signal.signal(signal.SIGINT, previous)
    finally:
        Path(filename).unlink(missing_ok=True)


def main() -> int:
    args = sys.argv[1:]
    # This marker belongs only to the recovery launcher, not to Claude or its
    # child processes. Ordinary interactive resumes still show the picker.
    automatic_recovery = os.environ.pop("AIPANE_AUTO_RESUME", "") == "1"
    if any(arg in ("--help", "-h", "--version", "-v") for arg in args) or (args and args[0] in MANAGEMENT_COMMANDS):
        os.execv(NATIVE_CLAUDE, ["claude", *args])
    if CONFIG != HOME / ".claude" and not MODEL_METADATA.exists():
        # Other Claude profiles own their own model configuration.
        os.execv(NATIVE_CLAUDE, ["claude", *args])
    settings = read_json(CONFIG / "settings.json")
    metadata = read_json(MODEL_METADATA)
    args, requested = take_option(args, "--model")
    args, extra_arg = take_option(args, "--settings")
    extra = load_extra_settings(extra_arg)
    explicit_default = (extra.get("env", {}).get("ANTHROPIC_MODEL")
                        if isinstance(extra.get("env", {}), dict) else None)
    explicit_default = explicit_default or os.environ.get("ANTHROPIC_MODEL") or extra.get("model")
    resumed = resume_model(args, settings)
    interactive = (sys.stdin.isatty() and sys.stdout.isatty()
                   and not any(arg in ("-p", "--print", "--bg", "--background") for arg in args))
    is_resume = any(arg in ("-c", "--continue", "-r", "--resume") or arg.startswith("--resume=") for arg in args)
    automatic_resume = automatic_recovery and is_resume
    if is_resume and not interactive and not (requested or explicit_default or resumed):
        raise ValueError("无法确定恢复会话使用的模型，请通过 --model 指定")
    options = settings.get("modelPicker", {}).get("options", [])
    picked = requested is None and interactive and not automatic_resume
    remembered = remembered_model(options, metadata["models"]) if picked else None
    default_model = explicit_default or resumed or remembered or settings.get("model", "")
    default = canonical_model(str(default_model), settings)
    model = canonical_model(requested, settings) if requested else default
    if automatic_resume and requested is None:
        if not resumed:
            raise ValueError("无法确定自动恢复会话的原模型，请通过 --model 指定")
        model = resumed
    elif picked:
        model = select_model(options, metadata["models"], default)
    if model not in metadata.get("models", {}):
        raise ValueError(f"模型 {model!r} 没有配置窗口，请先在 {MODEL_METADATA} 中配置")
    overlay = make_overlay(settings, metadata, extra, model)
    if picked:
        remember_model(model)
    argv = [*args, "--model", model]
    if extra_arg is not None:
        return run_with_private_settings(argv, overlay)
    os.execv(NATIVE_CLAUDE, ["claude", *argv, "--settings", json.dumps(overlay, ensure_ascii=False)])
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError) as error:
        print(f"claude-window: {error}", file=sys.stderr)
        raise SystemExit(1)
    except KeyboardInterrupt:
        print("\n已取消启动。", file=sys.stderr)
        raise SystemExit(130)
