# aipane

Multi-AI CLI pane launcher for iTerm2. One command to split your terminal into a grid of AI coding assistants.

```bash
ai cxdg      # Claude + Codex + Droid + Gemini in a 2x2 grid
ai cc         # two Claude Code side by side
ai cx         # Claude + Codex
```

## Prerequisites

- **macOS** with [iTerm2](https://iterm2.com/) (AppleScript support required)
- **zsh**
- One or more AI CLI tools installed:

| Key | Tool | Install |
|-----|------|---------|
| `c` | [Claude Code](https://docs.anthropic.com/en/docs/claude-code) | `npm install -g @anthropic-ai/claude-code` |
| `x` | [Codex](https://github.com/openai/codex) | `npm install -g @openai/codex` |
| `d` | [Droid](https://github.com/nicepkg/droid) | `npm install -g droid-cli` |
| `g` | [Gemini CLI](https://github.com/google-gemini/gemini-cli) | `npm install -g @anthropic-ai/gemini-cli` |

## Install

```bash
git clone https://github.com/Async23/aipane.git ~/code/aipane
echo 'source ~/code/aipane/aipane.zsh' >> ~/.zshrc
source ~/.zshrc
```

## Usage

```bash
ai <tools_string>
```

Each character in `tools_string` maps to one AI tool. The panes are arranged automatically:

| Count | Layout |
|-------|--------|
| 1 | single pane |
| 2 | 2 columns |
| 3 | 3 columns |
| 4 | 2x2 grid |
| 5+ | `ceil(sqrt(N))` columns, rows filled left-to-right, top-to-bottom |

### Layout examples

```
ai cx          ai cxdg        ai cccxdg

┌─────┬─────┐  ┌─────┬─────┐  ┌───┬───┬───┐
│  c  │  x  │  │  c  │  d  │  │ c │ c │ d │
│     │     │  ├─────┼─────┤  ├───┼───┼───┤
└─────┴─────┘  │  x  │  g  │  │ c │ x │ g │
               └─────┴─────┘  └───┴───┴───┘
```

## Claude Multi-Account Support

`aipane` supports multiple Claude Code accounts via `~/.claude-accounts/`:

```
~/.claude-accounts/
├── alice@example.com/     # account 1
├── bob@example.com/       # account 2
└── _shared/               # (ignored)
```

When your `tools_string` contains multiple `c`s, you'll be prompted to pick an account for each:

```
Claude Code #1 - select account:
  [1] alice@example.com
  [2] bob@example.com
Choose [1]: 1

Claude Code #2 - select account:
  [1] alice@example.com
  [2] bob@example.com
Choose [1]: 2
```

If only one account exists, it is selected automatically.

## Error Handling

```
ai: unknown tool 'z' (valid: c/x/d/g)
ai: requires iTerm2
ai: no Claude accounts found in ~/.claude-accounts/
```

## License

[MIT](LICENSE)
