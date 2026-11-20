# Antigravity CLI

`ai a` starts the official `agy` CLI; `ai ax` combines Antigravity and Codex.
`AIPANE_AGY_LAUNCH_CMD` overrides the default `agy` command. Single-tool arguments
are passed through, for example `ai a --model 'Gemini 4 Hydrogen' --effort high`.
The default follows agy's own permission settings.

When you start an agy session below your home directory, aipane checks each
parent up to and including your home directory, even across Git roots. It passes
each parent containing `.agents/skills/` to agy with `--add-dir`, nearest first.
Each added directory becomes a workspace, so its rules and other workspace
customizations apply along with its skills. This also applies in multi-pane
launches such as `ai ca`. Subcommands such as `ai a models` run as usual.

## Install

Install a tested, committed aipane runtime with `bin/aipane-release install` as
described in [recovery-runtime.md](recovery-runtime.md). This installs both
`aipane-agy` and `aipane-agy-focus` from the same immutable release as recovery.

Merge [the hook fragment](../integrations/antigravity/hooks.json) into
`~/.gemini/config/hooks.json`, preserving unrelated named hooks. Hooks placed in
`~/.gemini/antigravity-cli/settings.json` are not loaded by agy. The global hook
file is shared by Antigravity surfaces; aipane accepts only the conversation
selected by a verified foreground CLI's status callback.

Merge [the statusLine fragment](../integrations/antigravity/settings.json) into
`~/.gemini/antigravity-cli/settings.json`. Keep an existing custom status command
by chaining its output after `aipane-agy status`; do not silently replace it.
`stack_with_default` preserves the CLI's built-in model and quota display.
The custom status line shows the conversation title when available, followed by
the full selected conversation ID on a separate `Session: <ID>` line. The ID
appears once a conversation starts and updates when switching conversations.
New CLI processes load the configuration. Run `aipane-doctor` to inspect wiring.

## Activity and sessions

The CLI's structured status callback provides the selected conversation ID,
model, effort, execution mode, agent state, background tasks and approval state.
The Adapter records a private runtime entry per tmux socket/server/pane. Readers
verify the foreground PID, process start time and TTY before trusting that entry.
The tmux activity indicator stays busy during work, tool use, approval, goal
continuation and running subagents. Reopening a conversation that displays an
artifact for review leaves the indicator off. Once a turn starts or input is
queued, `reviewing` stays busy until that turn's Stop confirms `fullyIdle`.
A background command such as a preview server can remain open after the
indicator clears.
Missing or inconsistent evidence is unknown.

`SessionStart`/`PreInvocation`/`Stop` may precede the first callback containing a
new conversation ID. Pending lifecycle metadata is matched only after statusLine
selects that same ID. Other conversations, including subagents, cannot replace
the root binding. Session switches are reflected by statusLine without requiring
another prompt.

Recovery saves the exact selected ID and model/effort/mode/agent. It checks the
local CLI trajectory SQLite database and its recorded workspace before using
`agy --conversation ID`. The picker cache also lists desktop/cloud sessions and
may omit a newly created CLI session, so a picker entry alone is insufficient.
The workspace-keyed `--continue` cache is not used for automatic recovery.
After relaunch, the foreground callback must confirm the expected conversation
and settings before the recovery intent is marked verified. API keys are never
copied into snapshots; API mode needs the caller's configured credentials.

## Notifications

The question observer always returns `{"decision":"allow"}` to `PreToolUse`,
including on bad input, and delegates approval to agy's existing policy. It does
not auto-approve tools. Native question tool-call IDs provide the deduplication
key; `stepIdx` is the fallback for older payloads.

| No. | Trigger | Behavior |
| ---: | --- | --- |
| 1 | Terminal Stop plus idle status, no running tasks | One completion, failure or interruption notification |
| 2 | Stop with `continuationActive` | Remains busy; no intermediate completion |
| 3 | `PreToolUse` for `ask_question` | First question, deduplicated by step ID |
| 4 | Tool confirmation becomes pending | Approval notification once per dialog |

`PostInvocation` is not a completion event. A Stop while background work remains
does not notify until the status callback confirms idle. Delivery runs in a
detached worker that rechecks the conversation and turn after settling, so a
continued or replaced conversation cannot deliver an old completion.

The dedicated sender is `~/Applications/Antigravity Notifier.app`, Bundle ID
`com.alfheim.antigravity-cli-notifier`, executable `agy-notifier`. It uses the
shared `native/agent-notifier.m` modern `UNUserNotificationCenter` sender and the
original Antigravity icon in `native/icons/antigravity.icns`. Requests do not
override the bundle icon. Completion sounds use aipane's shared random pool;
other events use the system default. A failed sender is logged as failed and
releases the deduplication claim for retry, without impersonating another App.

Titles include the tmux coordinate and conversation title when available; the
body contains the final response's first plain-text line, question or error.
Clicking activates Ghostty and selects the original pane on its original tmux
socket, falling back to the original window/session if the pane disappeared.
The focus implementation is shared with dsh via `lib/agent_focus.py`.

Build into a hidden staging directory before installation:

```bash
bin/build-agy-notifier --output '/path/to/.stage.noindex/Antigravity Notifier.app'
```

Inspect notification content without delivery or state writes:

```bash
printf '%s\n' '{"event":"question","session_id":"preview","token":"question-1","question":"Choose a model"}' |
  aipane-agy --preview
```

The user runs the tracer in the target Ghostty/tmux pane, allows notifications if
prompted, then checks the fresh banner's icon and clicks it:

```bash
aipane-agy --test-notification
```

The command prints its unique tracer and sender outcome. Only macOS delivery
evidence and the user's banner/click check establish delivery and focus success.
Runtime entries, deduplication claims and structured diagnostics live under
`$AIPANE_STATE_DIR/agy` (default `~/.local/share/aipane/agy`). Diagnostic logs do
not include prompts, responses or credentials.

## Verification

```bash
python3 tests/test_agy_sessions.py
python3 tests/test_agent_notifications.py
python3 tests/test_dsh_focus.py
zsh tests/ai-contracts.zsh
zsh tests/init-reload.zsh
python3 -m unittest discover -s tests -p 'test_*.py'
aipane-doctor
ai-restart --dry-run
```

Live acceptance uses an isolated tmux service and loopback Gemini stub: normal
completion, first-turn hook ordering, session switching, exact resume and model
restoration exercise the installed CLI without consuming online model quota.
Terminal acceptance additionally covers multiline input, bracketed paste,
broadcast and tmux copying.

When rolling back to an aipane revision from before this integration, also
restore the backed-up agy settings and named hooks. The runtime manager removes
its owned optional agy links when the selected older release lacks them.
