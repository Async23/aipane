"""Reusable modeless terminal picker built on fzf with plain-text fallback.

This module owns terminal layout, Unicode cell width alignment, fzf action
wiring, responsive resizing, and readable non-fzf fallback. It intentionally
knows nothing about Claude, Antigravity, quotas, or model routing.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
from typing import Callable
import unicodedata


WIDE_THRESHOLD = 78
MIN_FZF_COLUMNS = 44
MIN_FZF_LINES = 12


@dataclass(frozen=True)
class PickerRow:
    key: str
    wide_summary: str
    narrow_summary: str
    details: str
    search_text: str = ""
    plain_label: str = ""


@dataclass(frozen=True)
class PickerPage:
    header_wide: str
    header_narrow: str
    rows: tuple[PickerRow, ...]
    default_index: int = 1
    border_label: str = " 选择模型 "
    subtitle: str = "本次启动使用所选模型"
    ghost: str = "输入名称筛选"


@dataclass(frozen=True)
class DynamicBindings:
    """Optional stateful callbacks invoked by fzf on navigation or view shifts."""

    helper_command: tuple[str, ...]
    state_file: Path
    auto_flat_flag: Path
    manual_flat_flag: Path
    family_label_file: Path
    flat_label_file: Path
    next_pos_file: Path
    allow_step: bool = True
    allow_view_toggle: bool = True
    allow_route_toggle: bool = False
    allow_refresh: bool = True
    empty_query_hint_wide: str = (
        "输入名称/档位筛选 · ↑/↓ 选择 · ←/→ 调档 · Enter 启动 · Esc 取消"
    )
    empty_query_hint_narrow: tuple[str, str] = (
        "输入筛选 · ↑/↓ 选择 · ←/→ 调档",
        "Enter 启动 · Esc 取消",
    )
    active_query_hint_wide: str = (
        "↑/↓ 选择 · Opt+,/. 调档 · Enter 启动 · Esc 清空 · Ctrl+C 取消"
    )
    active_query_hint_narrow: tuple[str, str] = (
        "↑/↓ 选择 · Opt+,/. 调档 · Enter 启动",
        "Esc 清空 · Ctrl+C 取消",
    )


def display_text(value: object) -> str:
    return " ".join("".join(c if c.isprintable() else " " for c in str(value)).split())


def cell_width(text: str) -> int:
    width = 0
    in_escape = False
    for ch in text:
        if in_escape:
            if ch == "m":
                in_escape = False
            continue
        if ch == "\x1b":
            in_escape = True
            continue
        if unicodedata.combining(ch):
            continue
        width += 2 if unicodedata.east_asian_width(ch) in "WF" else 1
    return width


def cell(text: str, width: int) -> str:
    if width <= 0:
        return ""
    if cell_width(text) > width:
        result = ""
        used = 0
        for character in text:
            step = 0 if unicodedata.combining(character) else (
                2 if unicodedata.east_asian_width(character) in "WF" else 1
            )
            if used + step > max(0, width - 1):
                break
            result += character
            used += step
        text = result + "…"
    return text + " " * max(0, width - cell_width(text))


def format_records(page: PickerPage) -> str:
    """Encode a PickerPage into NUL-delimited, tab-separated fzf records."""
    records = [f"\t{page.header_wide}\t{page.header_narrow}\t\t"]
    for row in page.rows:
        search = display_text(row.search_text)
        records.append(
            f"{row.key}\t{row.wide_summary}\t{row.narrow_summary}\t{row.details}\t{search}"
        )
    return "\0".join(records) + "\0"


def _sh_quote(value: object) -> str:
    return shlex.quote(str(value))


def _preview_script(bindings: DynamicBindings | None) -> str:
    if bindings is not None:
        active_w = bindings.active_query_hint_wide
        active_n1, active_n2 = bindings.active_query_hint_narrow
        empty_w = bindings.empty_query_hint_wide
        empty_n1, empty_n2 = bindings.empty_query_hint_narrow
    else:
        active_w = "↑/↓ 或 Ctrl+J/K 选择 · 回车启动 · Esc 清空筛选 · Ctrl+C 取消"
        active_n1, active_n2 = ("↑/↓ 或 Ctrl+J/K 选择 · 回车启动", "Esc 清空筛选 · Ctrl+C 取消")
        empty_w = "输入名称筛选 · ↑/↓ 或 Ctrl+J/K 选择 · 回车启动 · Esc 取消"
        empty_n1, empty_n2 = ("输入名称筛选 · ↑/↓ 或 Ctrl+J/K 选择", "回车启动 · Esc 取消")

    return (
        "if [ -n {1} ]; then printf '%s\\n' {4}; "
        "else printf '%s\\n' '没有匹配的模型' '按 Esc 或 Ctrl+U 清空筛选。' ''; fi; "
        'if [ -n "$FZF_QUERY" ]; then '
        'if [ "$FZF_PREVIEW_COLUMNS" -ge 72 ]; then '
        f"printf '\\n%s' {_sh_quote(active_w)}; "
        f"else printf '\\n%s\\n' {_sh_quote(active_n1)} {_sh_quote(active_n2)}; fi; "
        "else "
        'if [ "$FZF_PREVIEW_COLUMNS" -ge 72 ]; then '
        f"printf '\\n%s' {_sh_quote(empty_w)}; "
        f"else printf '\\n%s\\n' {_sh_quote(empty_n1)} {_sh_quote(empty_n2)}; fi; fi; "
        ": {q}"
    )


def _fzf_height(row_count: int, lines: int, *, dynamic: bool = False) -> tuple[int, int]:
    """Compute responsive (total_height, preview_lines) that fit split panes."""
    if lines >= 24:
        preview = 5
    elif lines >= 18:
        preview = 4
    else:
        preview = 3
    chrome = preview + 7
    desired_rows = max(row_count, 13 if dynamic else 5)
    visible_rows = min(desired_rows, max(3, min(14, lines - chrome)))
    total = min(lines, visible_rows + chrome)
    return max(MIN_FZF_LINES, total), preview


def _build_dynamic_binds(bindings: DynamicBindings, subtitle: str) -> list[str]:
    helper = " ".join(_sh_quote(part) for part in bindings.helper_command)
    state = _sh_quote(bindings.state_file)
    auto_flag = _sh_quote(bindings.auto_flat_flag)
    manual_flag = _sh_quote(bindings.manual_flat_flag)
    fam_label = _sh_quote(bindings.family_label_file)
    flat_label = _sh_quote(bindings.flat_label_file)
    binds: list[str] = []

    if bindings.allow_step:
        # Empty query: ←/→ step effort in place (matching native /effort).
        # Non-empty query: preserve normal text cursor editing inside query box.
        binds.append(
            '--bind=left:transform:'
            'if [ -n "$FZF_QUERY" ]; then printf "%s" "backward-char"; '
            f'else {helper} step --state {state} --key {{1}} --delta -1 --pos "$FZF_POS"; fi'
        )
        binds.append(
            '--bind=right:transform:'
            'if [ -n "$FZF_QUERY" ]; then printf "%s" "forward-char"; '
            f'else {helper} step --state {state} --key {{1}} --delta 1 --pos "$FZF_POS"; fi'
        )
        # Option+, / Option+. adjust effort regardless of query text.
        binds.append(
            f'--bind=alt-,:transform:{helper} step --state {state} --key {{1}} --delta -1 --pos "$FZF_POS"'
        )
        binds.append(
            f'--bind=alt-.:transform:{helper} step --state {state} --key {{1}} --delta 1 --pos "$FZF_POS"'
        )

    if bindings.allow_view_toggle:
        binds.append(
            f'--bind=ctrl-t:transform:{helper} toggle-view --state {state} --key {{1}} --pos "$FZF_POS" --query "$FZF_QUERY"'
        )
        # Fast-path pure-/bin/sh check on query change: zero Python forks when
        # typing single-word family queries like "neon" or "opus"; expands once
        # to all variants on multi-word queries ("neon xh", "opus low") or
        # standalone effort keywords ("xhigh", "max"), and collapses back on clear.
        change_script = (
            f'if [ -f {manual_flag} ]; then exit 0; fi; '
            'q=$(printf "%s" "$FZF_QUERY" | tr "[:upper:]" "[:lower:]"); '
            'q="${q#"${q%%[![:space:]]*}"}"; '
            'q="${q%"${q##*[![:space:]]}"}"; '
            'want_flat=0; '
            'case "$q" in '
            '*" "*) want_flat=1 ;; '
            'low|med|medium|high|xh|xhigh|extra|max) want_flat=1 ;; '
            'esac; '
            'if [ "$want_flat" = "1" ]; then '
            f'if [ ! -f {auto_flag} ]; then '
            f': > {auto_flag}; '
            f'lbl=$(cat {flat_label} 2>/dev/null || printf " 选择模型 "); '
            f'printf "reload-sync(%s render --state %s --view flat)+change-border-label(%s)" '
            f'{_sh_quote(helper)} {_sh_quote(state)} "$lbl"; '
            'fi; '
            'else '
            f'if [ -f {auto_flag} ]; then '
            f'rm -f {auto_flag}; '
            f'lbl=$(cat {fam_label} 2>/dev/null || printf " 选择模型 "); '
            f'printf "reload-sync(%s render --state %s --view family)+change-border-label(%s)" '
            f'{_sh_quote(helper)} {_sh_quote(state)} "$lbl"; '
            'fi; '
            'fi'
        )
        binds.append(f"--bind=change:transform:{change_script}")

    if bindings.allow_route_toggle:
        binds.append(
            f'--bind=ctrl-g:transform:{helper} toggle-route --state {state} --query "$FZF_QUERY"'
        )

    if bindings.allow_refresh:
        binds.append(
            f"--bind=ctrl-r:change-header(正在同步最新模型列表…)"
            f"+reload-sync({helper} refresh --state {state})"
            f"+change-header({subtitle})"
        )

    return binds


def select_with_fzf(
    page: PickerPage,
    *,
    executable: str,
    bindings: DynamicBindings | None = None,
) -> str:
    columns, lines = shutil.get_terminal_size()
    default_index = max(1, min(page.default_index, len(page.rows)))
    height, preview_lines = _fzf_height(len(page.rows), lines, dynamic=bindings is not None)
    height_flag = f"--height={height}" if bindings is not None else f"--height=~{height}"
    if bindings is not None:
        next_pos_q = _sh_quote(bindings.next_pos_file)
        load_bind = (
            f"--bind=load:transform:if [ -f {next_pos_q} ]; then "
            f'p=$(cat {next_pos_q}); rm -f {next_pos_q}; '
            f'if [ -n "$p" ]; then printf "pos(%s)" "$p"; fi; fi'
        )
    else:
        load_bind = f"--bind=load:pos({default_index})+unbind(load)"
    command = [
        executable,
        "--read0",
        "--print0",
        "--delimiter=\t",
        "--with-nth=" + ("2" if columns >= WIDE_THRESHOLD else "3"),
        "--accept-nth=1",
        "--header-lines=1",
        "--no-multi-line",
        "--no-multi",
        "--layout=reverse",
        height_flag,
        "--border=sharp",
        f"--border-label={page.border_label}",
        "--border-label-pos=3",
        "--padding=0,1",
        "--header-first",
        f"--header={page.subtitle}",
        "--prompt=搜索 › ",
        f"--ghost={page.ghost}",
        "--info=inline-right",
        "--no-sort",
        "--cycle",
        "--no-hscroll",
        "--highlight-line",
        "--pointer=›",
        "--gutter= ",
        "--preview=" + _preview_script(bindings),
        f"--preview-window=down,{preview_lines},wrap,border-top",
        "--sync",
        "--no-tty-default",
        "--with-shell=/bin/sh -c",
        load_bind,
        "--bind=enter:accept-non-empty,double-click:accept-non-empty,ctrl-c:abort,"
        "ctrl-j:down,ctrl-k:up,ctrl-n:down,ctrl-p:up,tab:down,shift-tab:up,ctrl-u:clear-query",
        '--bind=esc:transform:if [ -n "$FZF_QUERY" ]; then '
        "printf '%s' 'clear-query'; else printf '%s' 'abort'; fi",
        f'--bind=resize:transform:if [ "$FZF_COLUMNS" -ge {WIDE_THRESHOLD} ]; then '
        "printf '%s' 'change-with-nth(2)'; else printf '%s' 'change-with-nth(3)'; fi",
    ]
    if bindings is not None:
        command.extend(_build_dynamic_binds(bindings, page.subtitle))
    if "NO_COLOR" in os.environ:
        command.append("--color=bw")
    else:
        command.append(
            "--color=16,fg:-1,bg:-1,fg+:-1:reverse,bg+:-1,"
            "hl:4:bold,hl+:4:reverse:bold,pointer:4:bold,prompt:4:bold,"
            "header:-1:dim,footer:-1,info:-1:dim,border:4,label:4:bold,gutter:-1,"
            "preview-fg:-1,preview-bg:-1"
        )
    env = {key: value for key, value in os.environ.items() if not key.startswith("FZF_")}
    result = subprocess.run(
        command,
        input=format_records(page),
        stdout=subprocess.PIPE,
        text=True,
        env=env,
    )
    if result.returncode in (1, 130, -2):
        raise KeyboardInterrupt
    if result.returncode != 0:
        raise ValueError(f"模型选择器退出（状态 {result.returncode}）；可通过 --model 指定模型")
    selected = result.stdout.rstrip("\0\n")
    if not selected:
        raise ValueError("模型选择器没有返回有效选项")
    return selected


def select_with_text(
    page: PickerPage,
    *,
    text_resolver: Callable[[str], str | PickerPage | None] | None = None,
    extra_prompt: str = "",
) -> str:
    """Readable fallback for a small/dumb terminal or a missing fzf."""
    current = page
    while True:
        default_index = max(1, min(current.default_index, len(current.rows)))
        default_row = current.rows[default_index - 1]
        default_label = display_text(default_row.plain_label or default_row.narrow_summary)
        print(f"\n{display_text(current.border_label)}", file=sys.stderr)
        for index, row in enumerate(current.rows, 1):
            label = display_text(row.plain_label or row.narrow_summary)
            badge = " · 默认" if index == default_index else ""
            print(f"  {index:>2}. {label}{badge}", file=sys.stderr)
        while True:
            suffix = f" · {extra_prompt}" if extra_prompt else ""
            print(
                f"序号或名称 · 回车选 {default_label}{suffix} · q 取消: ",
                end="",
                file=sys.stderr,
                flush=True,
            )
            raw = sys.stdin.readline()
            if not raw:
                raise KeyboardInterrupt
            value = raw.strip()
            if value.casefold() in ("q", "quit", "exit") or value == "\x1b":
                raise KeyboardInterrupt
            if not value:
                return default_row.key
            if value.isdecimal() and 1 <= int(value) <= len(current.rows):
                return current.rows[int(value) - 1].key
            if text_resolver is not None:
                resolved = text_resolver(value)
                if isinstance(resolved, PickerPage):
                    current = resolved
                    break
                if isinstance(resolved, str) and resolved:
                    return resolved
            lowered = value.casefold()
            tokens = lowered.split()
            matches = [
                row for row in current.rows
                if all(token in f"{row.key} {row.wide_summary} {row.search_text}".casefold()
                       for token in tokens)
            ]
            if len(matches) == 1:
                return matches[0].key
            print(
                "匹配到多个模型，请输入更完整的名称或序号。" if matches
                else "没有匹配的模型，请重新输入。",
                file=sys.stderr,
            )


def select(
    page: PickerPage,
    *,
    bindings: DynamicBindings | None = None,
    text_resolver: Callable[[str], str | PickerPage | None] | None = None,
    extra_text_prompt: str = "",
) -> str:
    if not page.rows:
        raise ValueError("没有找到可选择的模型")
    executable = shutil.which("fzf")
    columns, lines = shutil.get_terminal_size()
    if (
        executable
        and sys.stdin.isatty()
        and sys.stderr.isatty()
        and os.environ.get("TERM", "dumb") != "dumb"
        and columns >= MIN_FZF_COLUMNS
        and lines >= MIN_FZF_LINES
    ):
        return select_with_fzf(page, executable=executable, bindings=bindings)
    return select_with_text(
        page,
        text_resolver=text_resolver,
        extra_prompt=extra_text_prompt,
    )
