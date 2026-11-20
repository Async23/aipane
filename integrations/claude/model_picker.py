"""Terminal model selection for the Claude context-window launcher."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import unicodedata


def display_text(value: object) -> str:
    return " ".join("".join(c if c.isprintable() else " " for c in str(value)).split())


def cell_width(text: str) -> int:
    return sum(0 if unicodedata.combining(c) else
               2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in text)


def cell(text: str, width: int) -> str:
    if cell_width(text) > width:
        result = ""
        for character in text:
            if cell_width(result + character) > width - 1:
                break
            result += character
        text = result + "…"
    return text + " " * max(0, width - cell_width(text))


def compact_tokens(value: int) -> str:
    # The locally configured 1M tier uses 1,048,576 tokens. Show its exact
    # capacity in the details, while keeping the list easy to scan.
    if value == 1_048_576:
        return "1M"
    if value >= 1_000_000:
        return f"{value / 1_000_000:g}M"
    if value >= 1_000:
        return f"{value / 1_000:g}K"
    return str(value)


def picker_records(choices: list[dict], models: dict, default: str) -> str:
    labels = [display_text(option.get("label") or option["model"]) for option in choices]
    name_width = min(30, max(12, *(cell_width(label) for label in labels)))

    def row(label: str, context: str, source: str = "", badge: str = "", *, wide: bool) -> str:
        result = f"{cell(label, name_width)}  {cell(context, 6)}"
        if wide:
            result += f"  {cell(source, 18)}"
        return result + (f"  {badge}" if badge else "")

    records = ["\t" + row("模型", "上下文", "用量来源", wide=True) + "\t" +
               row("模型", "上下文", wide=False) + "\t"]
    for index, (option, label) in enumerate(zip(choices, labels), 1):
        model = option["model"]
        metadata = models[model]
        context = metadata["context_tokens"]
        source = display_text(metadata.get("billing_source") or "未配置用量来源")
        badge = "默认" if model == default else ""
        summary = row(label, compact_tokens(context), source, badge, wide=True)
        short_summary = row(label, compact_tokens(context), source, badge, wide=False)
        details = [
            display_text(model),
            f"容量 {context:,} tokens · {source}",
            "更换上下文容量需重新启动。",
        ]
        records.append(f"{index}\t{summary}\t{short_summary}\t" + "\n".join(details))
    return "\0".join(records) + "\0"


def select_with_fzf(choices: list[dict], models: dict, default: str, executable: str) -> str:
    columns, _ = shutil.get_terminal_size()
    default_index = next((i for i, option in enumerate(choices, 1)
                          if option["model"] == default), 1)
    preview = (
        "if [ -n {1} ]; then printf '%s\\n' {4}; "
        "else printf '%s\\n' '没有匹配的模型' '按 Esc 或 Ctrl+U 清空筛选。' ''; fi; "
        'if [ -n "$FZF_QUERY" ]; then '
        'if [ "$FZF_PREVIEW_COLUMNS" -ge 72 ]; then '
        "printf '\\n%s' '↑/↓ 或 Ctrl+J/K 选择 · 回车启动 · Esc 清空筛选 · Ctrl+C 取消'; "
        "else printf '\\n%s\\n' '↑/↓ 或 Ctrl+J/K 选择 · 回车启动' 'Esc 清空筛选 · Ctrl+C 取消'; fi; "
        "else "
        'if [ "$FZF_PREVIEW_COLUMNS" -ge 72 ]; then '
        "printf '\\n%s' '输入名称筛选 · ↑/↓ 或 Ctrl+J/K 选择 · 回车启动 · Esc 取消'; "
        "else printf '\\n%s\\n' '输入名称筛选 · ↑/↓ 或 Ctrl+J/K 选择' '回车启动 · Esc 取消'; fi; fi; "
        ": {q}"
    )
    # Keep the launcher modeless: the query input stays visible so filter state
    # is never hidden, Enter launches immediately, and Esc pops one level at a
    # time (clear query first, then abort when already empty).
    command = [
        executable, "--read0", "--print0", "--delimiter=\t",
        "--with-nth=" + ("2" if columns >= 78 else "3"),
        "--accept-nth=1", "--header-lines=1", "--no-multi-line", "--no-multi",
        "--layout=reverse", "--height=~" + str(min(len(choices), 10) + 15),
        "--border=sharp", "--border-label= Claude Code · 选择模型 ",
        "--border-label-pos=3", "--padding=0,1", "--header-first",
        "--header=本次启动使用所选模型", "--prompt=搜索 › ",
        "--ghost=输入名称筛选，如 opus / argon / sol", "--info=inline-right",
        "--no-sort", "--cycle", "--no-hscroll", "--highlight-line", "--pointer=›", "--gutter= ",
        "--preview=" + preview, "--preview-window=down,6,wrap,border-top",
        "--sync", "--no-tty-default",
        "--with-shell=/bin/sh -c",
        "--bind=load:pos(" + str(default_index) + "),"
        "enter:accept-non-empty,double-click:accept-non-empty,ctrl-c:abort,"
        "ctrl-j:down,ctrl-k:up,ctrl-n:down,ctrl-p:up,tab:down,shift-tab:up,ctrl-u:clear-query",
        '--bind=esc:transform:if [ -n "$FZF_QUERY" ]; then '
        "printf '%s' 'clear-query'; else printf '%s' 'abort'; fi",
        '--bind=resize:transform:if [ "$FZF_COLUMNS" -ge 78 ]; then '
        "printf '%s' 'change-with-nth(2)'; else printf '%s' 'change-with-nth(3)'; fi",
    ]
    if "NO_COLOR" in os.environ:
        command.append("--color=bw")
    else:
        # Use the terminal's own palette/default background in both light and
        # dark themes. Reverse video keeps the selection visible in either.
        command.append("--color=16,fg:-1,bg:-1,fg+:-1:reverse,bg+:-1,"
                       "hl:4:bold,hl+:4:reverse:bold,pointer:4:bold,prompt:4:bold,"
                       "header:-1:dim,footer:-1,info:-1:dim,border:4,label:4:bold,gutter:-1,"
                       "preview-fg:-1,preview-bg:-1")
    env = {key: value for key, value in os.environ.items() if not key.startswith("FZF_")}
    result = subprocess.run(command, input=picker_records(choices, models, default),
                            stdout=subprocess.PIPE, text=True, env=env)
    if result.returncode in (1, 130, -2):
        raise KeyboardInterrupt
    if result.returncode != 0:
        raise ValueError(f"模型选择器退出（状态 {result.returncode}）；可通过 --model 指定模型")
    selected = result.stdout.rstrip("\0\n")
    if not selected.isdecimal() or not 1 <= int(selected) <= len(choices):
        raise ValueError("模型选择器没有返回有效选项")
    return choices[int(selected) - 1]["model"]


def select_with_text(choices: list[dict], models: dict, default: str) -> str:
    """Readable fallback for a small/dumb terminal or a missing fzf."""
    default_index = next((i for i, option in enumerate(choices, 1)
                          if option["model"] == default), 1)
    label = display_text(choices[default_index - 1].get("label") or default)
    print("\nClaude Code · 选择模型", file=sys.stderr)
    for index, option in enumerate(choices, 1):
        model = option["model"]
        name = display_text(option.get("label") or model)
        badge = " · 默认" if index == default_index else ""
        print(f"  {index:>2}. {name} · {compact_tokens(models[model]['context_tokens'])}{badge}",
              file=sys.stderr)
    while True:
        print(f"序号或名称 · 回车选 {label} · q 取消: ", end="", file=sys.stderr, flush=True)
        value = sys.stdin.readline()
        if not value:
            raise KeyboardInterrupt
        value = value.strip()
        if value.casefold() in ("q", "quit", "exit") or value == "\x1b":
            raise KeyboardInterrupt
        if not value:
            return choices[default_index - 1]["model"]
        if value.isdecimal() and 1 <= int(value) <= len(choices):
            return choices[int(value) - 1]["model"]
        matches = [option for option in choices if value.casefold() in
                   (option["model"] + " " + str(option.get("label", ""))).casefold()]
        if len(matches) == 1:
            return matches[0]["model"]
        print("匹配到多个模型，请输入更完整的名称或序号。" if matches else
              "没有匹配的模型，请重新输入。", file=sys.stderr)


def select_model(options: list[dict], models: dict, default: str) -> str:
    choices = [option for option in options if option.get("model") in models]
    if not choices:
        raise ValueError("没有找到可选择的模型")
    if default not in {option["model"] for option in choices}:
        default = choices[0]["model"]
    executable = shutil.which("fzf")
    columns, lines = shutil.get_terminal_size()
    if (executable and sys.stdin.isatty() and sys.stderr.isatty()
            and os.environ.get("TERM", "dumb") != "dumb" and columns >= 48 and lines >= 16):
        model = select_with_fzf(choices, models, default, executable)
    else:
        model = select_with_text(choices, models, default)
    option = next(option for option in choices if option["model"] == model)
    label = display_text(option.get("label") or model)
    print(f"启动 {label} · 上下文 {compact_tokens(models[model]['context_tokens'])}", file=sys.stderr)
    return model
