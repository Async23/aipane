# aipane

zsh toolkit for daily AI CLI workflows: multi-tool tmux pane orchestration, process cleanup, and optional workstation fragments.

## Language

**AI Tool**:
A first-class coding agent CLI that can be launched by a single character key in the `ai` tools string.
_Avoid_: model, provider, account

**Tool Key**:
The single character that selects an AI Tool inside an `ai` tools string (for example `k` for Kimi Code).
_Avoid_: shortcut, alias, flag

**Launch Command**:
The full shell command string used to start an AI Tool, overridable via `AIPANE_*_LAUNCH_CMD`.
_Avoid_: binary name alone, PATH entry

**Launch-Only Support**:
An AI Tool is wired into `ai` orchestration and a configurable Launch Command, but is not recognized by detached-process cleanup.
_Avoid_: partial support, incomplete integration (without saying what is in vs out)

**Kimi Code**:
The Moonshot Kimi Code CLI product, started by the binary `kimi`, exposed in aipane as tool key `k`.
_Avoid_: Kimi CLI (legacy product line naming), Moonshot, kimi-code home directory

**Permission Mode**:
Kimi Code's session approval policy: `manual` (prompt), `yolo` (auto-approve regular tools, may still ask), or `auto` (fully unattended).
_Avoid_: AFK (legacy/alternate naming), approval flag as a synonym for the whole mode set

**Agent Activity**:
The pane-local state of an AI Tool turn. `busy` means a submitted turn is still in flight; `idle` means no turn is in flight.
_Avoid_: process activity, terminal-title activity, spinner state
