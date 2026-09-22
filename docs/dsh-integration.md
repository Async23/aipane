# dsh integrations

`ai d` launches dsh-TUI. The profile plugins in `integrations/dsh/` connect
native lifecycle events to aipane without changing the installed dsh package.

| Feature | Owner |
|---|---|
| Pane/window close confirmation | `bin/tmux-window-wrap` process-tree detection |
| Busy/idle projection and repair | `aipane-activity.mjs`, `lib/dsh_activity.py`, `lib/agent_activity.py` |
| Foreground TUI session binding | `aipane-session.mjs`, `lib/dsh_sessions.py` |
| Durable session validation | `bin/aipane-dsh-session`, using the installed native parser |
| Snapshot, restore, and restart | `aipane-snapshot`, `ai-restore`, `aipane-restore-executor` |
| Copy a reply to the clipboard | `aipane-copy.mjs` (`/copy`) |
| Desktop notifications | [dsh-notifications.md](dsh-notifications.md) |
| Installation audit | `bin/aipane-doctor` |

## Register the profiles

Merge [`integrations/dsh/cordis.patch.yml`](../integrations/dsh/cordis.patch.yml)
into each installed profile's `cordis.patch.yml` under
`${DSH_HOME:-~/.dsh}/profiles/`, replacing the example paths with this checkout.
The desktop sender setup is documented separately in
[dsh-notifications.md](dsh-notifications.md).

Install the session validator alongside the existing activity, binding and
notification helpers:

```sh
ln -s "$PWD/bin/aipane-dsh-session" "$HOME/.local/bin/aipane-dsh-session"
```

For the **web** profile, set `config: { mode: web }` on the
`aipane-dsh-session` entry. Web can own several independent root sessions;
it does not supply the single foreground TUI binding used by `ai-restart`.
Its activity observer aggregates every running agent in the host, while
notifications apply to each human-facing root turn. Activity requires the Web
host to run inside tmux. Notification delivery also works outside tmux.

Profiles with `patchReload: live` reload changes to their patch file. When
upgrading a plugin already imported by a live process, use its `file:` URL
with a new content revision query (for example `?v=<source-hash>`) to load the
new module, or restart dsh. The URL still references the same tracked source.
The doctor resolves these URLs to their canonical files.

## Copy command

`aipane-copy.mjs` registers `/copy [n]` on dsh's human-command registry, so
dsh-TUI merges it into the slash menu and dispatches it against the foreground
agent; the plugin owns no UI. `n` counts assistant replies back from the newest
and defaults to `1`. The handler reads the receiving agent's live session log
the way the TUI's own `/export` does — `assistant/message` text blocks only, so
reasoning and tool results never reach the clipboard — and reports which path
settled the write.

Clipboard paths, in the order the TUI itself tries them for a selection: a
native utility first (`pbcopy`, `clip`, `wl-copy`/`xclip`/`xsel`), then the tmux
paste buffer with `-w` so the outer terminal follows, then OSC 52 — wrapped in a
tmux DCS passthrough so an SSH client's terminal can consume it. A native write
is skipped when `SSH_CONNECTION` is set, where it would target the wrong
machine. In the Web profile the row is still registered so profile parity holds,
but the plugin stays inert: the browser owns its own command surface, and a
write from the host process would target the host.

```sh
node --test tests/test_dsh_copy.mjs
```

## Session recovery

The TUI channel's selected Agent is authoritative. Creating a background root
does not steal the binding; switching workspace, `/new`, and resume update it.
Snapshots preserve the selected session ID, workspace, `DSH_HOME`, and session
storage root. A restore validates the exact durable session with dsh's native
format reader before launching `dsh-tui --resume ID`. Verification requires
the restarted channel to select that same session and workspace.
The selected session and storage namespace are checked again after confirmation
and immediately before restart; switching sessions invalidates the old plan.

Live session records expire after ten seconds without a heartbeat. Dead
processes, reused PIDs, another socket/pane, multiple matching hosts, and
invalid session artifacts do not authorize a guessed resume. See
[session-restore-design.md](session-restore-design.md) for the shared recovery
contract.

## Activity recovery

The observer atomically writes aggregate state independently of the tmux
report helper, refreshes it every two seconds, and retries failed reports.
The resolver checks the exact process generation, plugin instance, pane,
socket, server and record freshness. Two stable observations are required
before repairing a missed busy or idle projection. A newer running turn
cancels a pending idle repair. Missing or stale evidence resolves to unknown.

Runtime metadata contains identities and states, not conversation text, and
lives in private `${DSH_HOME}/aipane/activity/` and `aipane/sessions/` files.
Old plugin teardown cannot remove the replacement's records.

## Audit and cleanup boundaries

`aipane-doctor` reads native bundle/profile/home patch composition without
starting dsh or evaluating configuration expressions. It checks both installed
TUI and Web profiles, canonical source and helper paths, and the notification
App and icon. Broken or disabled registrations fail the audit.

MCP cleanup recognizes live dsh ancestors, including hosts without a TTY.
Its explicit expired-session and orphan rules still apply. The cleanup engine
only manages Rod browsers and MCP helpers.

The machine-local `my-local-token-stats` skill owns optional dsh Token
accounting separately from this repository. It reads persisted session usage,
excludes inherited fork usage and migration copies, and reports its coverage
limits for auxiliary requests that do not persist usage.
