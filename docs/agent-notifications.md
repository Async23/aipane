# macOS Agent Notifications

`lib/agent_notifications.py` is the deep Agent Notifications module. Its
`AgentNotifications.handle(agent, payload)` Interface owns payload adaptation,
session/transcript evidence, title and message normalization, sound selection,
deduplication, focus actions, delivery results, and diagnostic logging.

The executables in `bin/` are thin input/output Adapters. They preserve each AI
Tool's hook invocation and dry-run output while delegating notification decisions
to the shared module. `MacOSNotificationAdapter` preserves the dedicated app and
fallback identities; `InMemoryNotificationAdapter` exercises the same Interface
without sending a desktop notification.

Preview mode is read-only: it does not claim a deduplication key, write a log, or
cross the delivery seam. Grok transcript settling and Kimi background delivery
remain Adapter scheduling details; the shared module still owns the final
decision and result.

Each delivered completion chooses one sound from `Glass`, `Ping`, `Pop`, `Purr`,
`Submarine`, and `Tink`. A logical notification chooses once, so its primary and
fallback delivery paths use the same sound.

| No. | Agent | Adapter | Fixed-sound override |
| ---: | --- | --- | --- |
| 1 | Codex | `bin/aipane-codex-notify` delegates to `bin/aipane-codex-desktop-notify` | `CODEX_NOTIFY_SOUND` |
| 2 | Grok Build | `bin/aipane-grok-notify` | `GROK_NOTIFY_SOUND` |
| 3 | Claude Code | `bin/aipane-claude-notify` | `CLAUDE_NOTIFY_SOUND` |
| 4 | Cursor Agent | `bin/aipane-cursor-notify` | `CURSOR_NOTIFY_SOUND` |
| 5 | Kimi Code | `bin/aipane-kimi-notify` | — |
| 6 | dsh-TUI / dsh Web | [`bin/aipane-dsh-notify`](../bin/aipane-dsh-notify) ([installation](dsh-notifications.md)) | — |

Kimi Code's selected notification icon is **04 / 雾蓝**. Its tracked
[SVG](../native/icons/kimi-code.svg) keeps the official Kimi geometry and adds
the `#E9F0FA` background; [`bin/build-kimi-notifier`](../bin/build-kimi-notifier)
builds the App from this local source. The [design sheet](design/kimi-code-notification-icons.html)
retains the other background options. The active App is
`~/Applications/Kimi Code Notifier.app` with Bundle ID
`com.alfheim.kimi-code-notifier.v2`.

For adapters with an override, leave it unset for random selection. Set it to a
macOS sound name when one stable sound is preferred.

The adapters expect the corresponding dedicated notifier apps and focus scripts
to remain in each agent's configured home paths. Install them through symlinks so
the tracked adapter remains the single source of truth.

## Claude Code

Merge [`integrations/claude/hooks.json`](../integrations/claude/hooks.json) into
`~/.claude/settings.json`, preserving unrelated settings and hooks. The fragment
includes both Agent Activity and desktop notifications. Install its notification
entrypoint alongside `aipane-claude-activity`:

```bash
ln -s /path/to/aipane/bin/aipane-claude-notify ~/.local/bin/aipane-claude-notify
```

| No. | Trigger | Notification |
| ---: | --- | --- |
| 1 | `Stop` | Turn completed; first line of the final response |
| 2 | `StopFailure` | API failure; available error details |
| 3 | `PermissionRequest`, except `AskUserQuestion` | Tool approval requested |
| 4 | `PreToolUse` for `AskUserQuestion` | First question, before the tool waits |
| 5 | `Notification`: `elicitation_dialog`, `elicitation_url_dialog`, `agent_needs_input` | MCP input or teammate input requested |

Each event has one notification trigger. Delayed `permission_prompt` and
`idle_prompt` notifications are omitted because approval and completion already
have direct hooks. Set `preferredNotifChannel` to `notifications_disabled` in the
local Claude settings to use only these custom notifications; this does not
disable `Notification` hooks. See the [Claude hooks reference](https://code.claude.com/docs/en/hooks#notification).

Notifications run asynchronously, including when the terminal is focused.
They retain `Claude Code Notifier.app` and the existing focus script at
`~/.claude/hooks/focus-tmux.sh`; clicking targets the original session/window/pane
and activates Ghostty. Each session has separate notification groups. Imported
Cursor/Grok hooks, Claude Desktop sessions, and events identified as subagents
are ignored.
The sender uses `UNUserNotificationCenter` with Bundle ID
`com.alfheim.claude-code-notifier.v2` and the original Claude bundle icon in
[`native/icons/claude-code.icns`](../native/icons/claude-code.icns). The adapter
directly executes `Contents/MacOS/claude-notifier`, without `-appIcon` or a
Homebrew sender impersonating its identity. The legacy sender could exit before
delivery when a long-lived tmux server outlived the desktop login: its
`NSWorkspace` check could no longer see the running Notification Center. The
modern sender talks to the notification service directly.

Build with [`bin/build-claude-notifier`](../bin/build-claude-notifier) into a new
hidden `.noindex` staging directory, keeping the final App filename:

```bash
bin/build-claude-notifier --output '/path/to/.stage.noindex/Claude Code Notifier.app'
```

Before installing, back up the existing App, adapter, notification preferences,
and notification database. Unregister and archive the old App and any indexed
backup copies, then install and register the staged App at
`~/Applications/Claude Code Notifier.app`. Do not reuse the legacy Bundle ID
with the modern API. Authorize the new identity before testing hooks:

```bash
"$HOME/Applications/Claude Code Notifier.app/Contents/MacOS/claude-notifier" -authorize
"$HOME/Applications/Claude Code Notifier.app/Contents/MacOS/claude-notifier" -status
```

The sender has a 10-second timeout; the AppleScript fallback has a 5-second
timeout, both inside the 20-second hook budget. Primary failures emit a
`notify_sender_failed` JSON diagnostic without notification text; the fallback
has the AppleScript identity/icon and cannot run the tmux focus action.

Claude notifications use `<session>:<window>:<pane> · <session title>` as the
title, the current user prompt as the subtitle, and the first plain-text line of
the response as the body. Failure, approval, and input notifications append their
status to the title. Title and prompt text use the same display-width limits as
Codex; common Markdown headings, links, emphasis, and code markers are removed.

The hook's `transcript_path` supplies the session title and current user prompt.
Explicitly renamed sessions take precedence over generated `ai-title` records.
The reader scans at most the last 8 MiB, filters by session and available prompt
IDs, and skips tool results, subagent messages, and injected context. It does not
depend on the old `record-prompt.sh` cache. Missing transcript content falls back
to the project name and event label; outside tmux, the coordinate is omitted.

The old `~/.claude/hooks/notify-complete.sh` and `notify-waiting-input.sh` paths can
both symlink to `bin/aipane-claude-notify` for compatibility. Register only the
canonical `~/.local/bin/aipane-claude-notify` command in settings.

`~/.claude/hooks/notify.jsonl` records event/session identifiers and the delivery
channel, without conversation text. A `custom-app` result records successful
sender execution; actual macOS delivery still requires `usernoted` or notification
database evidence. `CLAUDE_NOTIFY_DRY_RUN=1` prints the normalized notification,
including its focus action, without sending or writing logs:

```bash
printf '%s\n' '{"hook_event_name":"Stop","session_id":"preview","last_assistant_message":"Preview only"}' |
  CLAUDE_NOTIFY_DRY_RUN=1 ~/.local/bin/aipane-claude-notify
python3 tests/test_claude_notify.py
```

## Codex

Merge [`integrations/codex/hooks.json`](../integrations/codex/hooks.json) into
`~/.codex/hooks.json`, preserving unrelated hooks. The `PreToolUse` matcher for
`request_user_input` and `request_user_input_async` calls the existing entrypoint:

```bash
ln -s /path/to/aipane/bin/aipane-codex-notify ~/.local/bin/aipane-codex-notify
```

Append the new matcher group when merging, so existing hook indexes and trust
records stay stable. Codex 0.166 also requires a trusted hash for the new hook;
review and trust this command in `/hooks` before use. Registering `hooks.json`
alone leaves an untrusted hook inactive.

This hook runs asynchronously with a 30-second timeout. It sends a question
notification when Codex asks, including queued questions while Codex continues
working and questions in an active Goal. It runs even when the terminal is
focused. The title ends in `等待回答`; the body contains the first question,
and the subtitle shows the count for a batch. It retains the Codex notifier App,
sound selection, and click action targeting the original tmux pane.

The canonical trigger is `PreToolUse`; do not add a second notification hook to
`PostToolUse`. Each session/tool-call ID is claimed atomically, so duplicate
callbacks, subsequent turns, and answers do not repeat the alert. Distinct calls
with identical wording still notify. Subagent hooks and malformed questions are
ignored. Delivery failures release the claim for a subsequent retry. No reminder
timer or historical question replay is installed. Keep the existing `notify`
program for completion events; `tui.notifications` can remain disabled.

Question diagnostics in `$CODEX_HOME/log/codex-questions.log` contain identifiers,
time, pane, outcome, and sender channel, without question text. Claims live in
`$CODEX_HOME/notifications/questions/`. Sender acceptance is not proof that macOS
displayed a banner. Existing Codex processes may need their hooks reloaded or the
conversation resumed in a new process before the added hook takes effect.

Preview the actual wrapper without sending a notification or writing state:

```bash
printf '%s\n' '{"hook_event_name":"PreToolUse","tool_name":"request_user_input_async","session_id":"preview","tool_use_id":"question-1","tool_input":{"questions":[{"title":"只重试失败工单可以吗？"}]}}' |
  CODEX_NOTIFY_DRY_RUN=1 ~/.local/bin/aipane-codex-notify
python3 -m unittest discover -s tests -p 'test_codex_question_notify.py'
```

For manual macOS banner and click verification, run:

```bash
~/.local/bin/aipane-codex-notify --test-question
```

Each invocation prints a unique tracer and sends one question notification.
Confirm that the matching banner appears and clicking returns to the invoking
pane. `CODEX_NOTIFY_DRY_RUN=1` also works with this command for a silent preview.

Codex TUI's temporary task-title and catch-up recap requests inherit the legacy
`notify` program. They can finish after the visible task has ended, each with a
new thread/turn ID. The module suppresses these internal completions by matching
the `codex-tui` client and the single generated prompt's instruction/context
signature. Legacy payloads omit `thread_source`, and ephemeral threads have no
persisted metadata, so this compatibility filter needs checking if Codex changes
those templates. Normal user completions, subagent notifications, and goal
handling keep their existing behavior. Suppressed internal events log only IDs
and the reason (`internal_title` or `internal_recap`), without conversation text.

Codex subagent titles use `<tmux coordinate> · L<depth> · <main task title>`.
Missing coordinates or depth are omitted.

Run the Interface and compatibility contracts with:

```bash
python3 tests/test_agent_notifications.py
python3 tests/test_codex_question_notify.py
python3 tests/test_claude_notify.py
python3 tests/test_agent_notification_sounds.py
python3 tests/test_kimi_notify.py
```
