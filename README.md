# aipane

A zsh toolkit for daily AI CLI workflows: account-isolated Claude Code launches, tmux pane orchestration for multiple AI tools, and process cleanup.

[中文文档 / Chinese README](README_CN.md)

## Features

| Command | Description |
|---|---|
| `cc [email] [args...]` | Launch Claude Code with an account-isolated config directory; selects a configured account when `email` is omitted |
| `ccd [args...]` | Launch Claude Code with the normal config directory and `--dangerously-skip-permissions` |
| `ai [--new\|-n] [--layout\|-l <layout>] <tools_string> [tool_args...]` | Launch one AI CLI directly or orchestrate multiple CLIs in tmux panes |
| `codexx [args...]` | Launch `codex --yolo` |
| `killcc [options]` | Clean up detached AI CLI trees and orphaned AI child processes |
| `killmcp [options]` | Clean up stale detached/orphaned MCP helper processes |
| `killrod [options]` | Force-clean matching Rod/Leakless browser processes and Playwright `chrome-headless-shell` processes |

Convenience aliases:

- `geminii` → `gemini --yolo`
- `oc` → `opencode`

> [!WARNING]
> `ccd`, `codexx`, and several `ai` launchers disable their tools' approval checks. The cleanup commands terminate processes. Review the command definitions and use `--dry-run` before cleanup when in doubt.

## Install

```bash
git clone https://github.com/Async23/aipane.git ~/.aipane
grep -qxF 'source ~/.aipane/init.zsh' ~/.zshrc 2>/dev/null ||
  printf '\nsource ~/.aipane/init.zsh\n' >> ~/.zshrc
source ~/.zshrc
```

To update an existing installation:

```bash
git -C ~/.aipane pull --ff-only
```

## Dependencies

- macOS and zsh
- Claude Code for `cc`, `ccd`, and the `c` tool in `ai`
- tmux for multi-tool `ai` launches, or whenever `--new`/`--layout` is used
- The corresponding optional CLI for each additional `ai` tool: Codex, Droid, Grok, OpenCode, Cursor CLI, Qoder CLI, or Oh My Pi (`omp`)

Single-tool calls such as `ai x` run in the current shell and do not require tmux unless `--new` or `--layout` is supplied.

## Optional Configuration

Set overrides before sourcing `init.zsh`:

```bash
export AIPANE_CLAUDE_CMD="claude"                       # e.g. claude-guard
export AIPANE_ACCOUNTS_BASE="$HOME/.claude-accounts"
export AIPANE_SHARED_DIR="$AIPANE_ACCOUNTS_BASE/_shared"
export AIPANE_CACHE_DIR="${XDG_CACHE_HOME:-$HOME/.cache}"

export AIPANE_CODEX_LAUNCH_CMD="codex --yolo"
export AIPANE_DROID_LAUNCH_CMD="droid"
export AIPANE_GROK_LAUNCH_CMD="grok --always-approve"
export AIPANE_OPENCODE_LAUNCH_CMD="opencode"
export AIPANE_CURSOR_LAUNCH_CMD="cursor-agent --force"
export AIPANE_QODER_LAUNCH_CMD="qodercli"
export AIPANE_OMP_LAUNCH_CMD="omp --approval-mode=yolo"
```

`ai --help` displays the effective launcher commands after these overrides are loaded.

## Claude Account Model

Only `cc` uses an account-specific `CLAUDE_CONFIG_DIR`. If the email is omitted—or the first argument starts with `-`—`cc` uses the only directory under `AIPANE_ACCOUNTS_BASE` automatically, or prompts when multiple account directories exist. If none exist, pass an email explicitly to create the first account directory.

```bash
cc alice@example.com
cc alice@example.com --resume <session-id>
cc --continue                         # select an account, then forward --continue
```

`ccd` deliberately uses Claude Code's normal config directory. It does not accept an account selector:

```bash
ccd
ccd --resume <session-id>
```

The account layout used by `cc` and Claude Code is:

```text
~/.claude-accounts/
├── alice@example.com/
│   ├── .claude.json                                      # managed by Claude Code
│   ├── rules -> ~/.claude/rules                          # optional
│   ├── settings.json -> ~/.claude/settings.json          # optional
│   ├── settings.local.json -> ~/.claude/settings.local.json  # optional
│   ├── projects -> ~/.claude-accounts/_shared/projects
│   └── history.jsonl -> ~/.claude-accounts/_shared/history.jsonl
├── bob@example.com/
└── _shared/
    ├── projects/
    └── history.jsonl
```

`cc` creates the account directory, shared `projects`/`history.jsonl` paths, and their links. Claude Code creates and manages `.claude.json`. Links for `rules`, `settings.json`, and `settings.local.json` are created only when the corresponding source exists under `~/.claude/`. Existing regular files in an account directory are preserved.

## `ai` Launcher

Tool keys and default commands:

| Key | Tool | Default command |
|---|---|---|
| `c` | Claude Code | `ccd` |
| `x` | Codex | `codex --yolo` |
| `d` | Droid | `droid` |
| `g` | Grok | `grok --always-approve` |
| `o` | OpenCode | `opencode` |
| `r` | Cursor | `cursor-agent --force` |
| `q` | Qoder | `qodercli` |
| `p` | Oh My Pi | `omp --approval-mode=yolo` |

Examples:

```bash
ai x                                      # run one tool in the current shell
ai x resume <session-id>                  # forward arguments to one tool
ai x -- --help                            # pass an ai-looking option to the tool
ai --new x resume <session-id>            # run one tool in a new tmux window/session

ai cxdg                                   # one pane per character
ai cxr                                    # interactively choose a 3-pane layout
ai cxr --layout main-right                # skip the 3-pane prompt
ai -l columns cxr
ai --new cxdg
ai cc                                     # duplicate keys launch duplicate tools
```

Available layouts:

- `auto`: automatic grid; skips the 3-pane prompt
- `main-left`, `main-right`: asymmetric layouts that require exactly three panes
- `columns`, `rows`: one column or row per tool
- Custom column counts such as `1,2` or `2,1`; the sum must equal the number of tools

Tool arguments are supported only when the tools string contains one tool. Multi-tool calls with extra arguments are rejected so incompatible arguments are not sent to multiple CLIs.

Inside tmux, a pane layout reuses the current window only when it has one pane; use `--new` to create a new window. Outside tmux, pane launches create a temporary session and attach to it.

## Process Cleanup

The wrappers map to the unified cleanup engine as follows:

```text
killcc  → aipane-cleanup ai  --verbose
killmcp → aipane-cleanup mcp --verbose --session-age 18000
killrod → aipane-cleanup rod --force --verbose
```

Start with a dry run:

```bash
killcc --dry-run
killmcp --dry-run
killrod --dry-run
```

Direct usage:

```bash
./bin/aipane-cleanup [all|ai|rod|mcp] \
  [--all|--force] [--max-age SECONDS] [--orphan-age SECONDS] \
  [--session-age SECONDS] [--dry-run] [--verbose|-v] [--help|-h]
```

The `ai` mode recognizes detached Claude, Codex, Droid, Gemini, Grok, OpenCode, and `agent-browser` daemon processes, follows their process trees, and also checks known orphaned AI child processes. `--session-age` additionally enables cleanup of old detached tmux Claude session trees.

In `rod` mode, `--force` selects all matching Rod/Leakless Chromium processes and every matching `ms-playwright/.../chrome-headless-shell` process, regardless of age.

Cleanup actions are logged to `~/logs/aipane-cleanup.log`. Age defaults can be overridden with:

- `AIPANE_AI_ORPHAN_MAX_AGE` (default `900`)
- `AIPANE_AI_SESSION_MAX_AGE` (disabled when unset)
- `AIPANE_ROD_MAX_AGE` (default `300`)
- `AIPANE_MCP_MAX_AGE` (default `21600`)
- `AIPANE_MCP_ORPHAN_MAX_AGE` (default `900`)
- `AIPANE_MCP_SESSION_MAX_AGE` (disabled when unset)
- `AIPANE_TMUX_BIN` (optional explicit tmux path)

## Project Structure

```text
.
├── .gitignore
├── LICENSE
├── README.md
├── README_CN.md
├── init.zsh                 # entrypoint sourced by zsh
├── aipane.zsh               # compatibility entrypoint
├── lib/
│   └── core.zsh             # shared config, account, and layout helpers
├── cmd/
│   ├── aliases.zsh
│   ├── cc.zsh
│   ├── codex.zsh
│   ├── kill.zsh
│   ├── killmcp.zsh
│   ├── killrod.zsh
│   └── pane.zsh
├── bin/
│   ├── aipane-cleanup
│   └── rod-cleanup
├── docs/
│   └── plans/
│       └── 2026-03-05-rod-cleanup-design.md
└── tests/
    └── aipane-cleanup-ai-protection.sh
```

## Verification

```bash
zsh -n init.zsh aipane.zsh cmd/*.zsh lib/*.zsh
sh -n bin/aipane-cleanup bin/rod-cleanup tests/*.sh

zsh -fc '
  source ./init.zsh
  type cc ccd ai codexx killcc killmcp killrod geminii oc
  ai --help >/dev/null
'

./tests/aipane-cleanup-ai-protection.sh
```

## License

MIT
