# macOS Agent Notifications

The notification adapters in `bin/` normalize completion notifications without
changing each agent's hook trigger or dedicated notifier app identity.

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
