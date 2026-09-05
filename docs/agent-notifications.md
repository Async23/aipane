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

For adapters with an override, leave it unset for random selection. Set it to a
macOS sound name when one stable sound is preferred.

The adapters expect the corresponding dedicated notifier apps and focus scripts
to remain in each agent's configured home paths. Install them through symlinks so
the tracked adapter remains the single source of truth.

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
python3 tests/test_agent_notification_sounds.py
python3 tests/test_kimi_notify.py
```
