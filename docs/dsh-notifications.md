# dsh desktop notifications

dsh uses a dedicated macOS sender with the selected **04 / 鲸鱼描线** icon.
The tracked [SVG](../native/icons/dsh.svg) is the icon source; the
[design sheet](design/dsh-notification-icons.html) is a visual reference.

## Wiring

| Layer | Source |
|---|---|
| Native dsh lifecycle | `integrations/dsh/aipane-notify.mjs` |
| JSON input adapter | `bin/aipane-dsh-notify` |
| Content, deduplication and delivery | `lib/agent_notifications.py` |
| Modern macOS sender | `native/agent-notifier.m` |
| App build | `bin/build-dsh-notifier` |
| Click action | `bin/aipane-dsh-focus` |

The App is `~/Applications/dsh Notifier.app`, with stable Bundle ID
`com.alfheim.dsh-notifier`. It uses `UNUserNotificationCenter` and its bundle
icon. It does not impersonate another Agent or use a legacy notifier fallback.
macOS supplies the surrounding notification presentation.

The plugin observes live root Agent turns. Completed turns, failures, pending
approvals and pending user questions produce notifications. User cancellation,
historical transcript replay, initial plugin loading and background subagent
completion do not. Automatically resolved approval/question requests do not
produce waiting notifications. The independent
[`aipane-activity.mjs`](../integrations/dsh/aipane-activity.mjs) plugin continues
to own the tmux breathing indicator.

Completion content follows the other local Agents: tmux coordinate and task
title, user prompt, and visible answer. Completion sounds use the existing
random pool; failures and waiting events use the default sound. Stable event
identities deduplicate repeat delivery. Logs record IDs and outcomes rather
than conversation or tool-input text.

Clicks activate Ghostty and return to the original tmux socket and pane.
Missing panes fall back to their window and session. Missing tmux targets
leave Ghostty active.

## Install

From the checkout, on macOS:

```sh
bin/build-dsh-notifier
ln -s "$PWD/bin/aipane-dsh-notify" "$HOME/.local/bin/aipane-dsh-notify"
ln -s "$PWD/bin/aipane-dsh-focus" "$HOME/.local/bin/aipane-dsh-focus"
```

The builder refuses to overwrite an existing App. For an upgrade, build into
a staging path using `--output`, increment `--version` / `--build`, verify the
bundle, then back up and replace the installed App.

Register the installed App and request its first notification authorization:

```sh
/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister \
  -f "$HOME/Applications/dsh Notifier.app"
"$HOME/Applications/dsh Notifier.app/Contents/MacOS/dsh-notifier" -authorize
```

Register the plugins in each installed TUI and Web profile as described in
[dsh integrations](dsh-integration.md#register-the-profiles), using this
checkout's source paths. That guide also covers upgrading a plugin already
loaded by a live host. Both profiles use the same notification sender.

## Verify

Preview a payload without sending a notification or changing deduplication
state:

```sh
printf '%s\n' '{"event":"complete","session_id":"preview","turn_id":"1","event_id":"end-1","cwd":"/tmp/demo","session_title":"通知预览","prompt":"测试 dsh 通知","answer":"验证完成。"}' \
  | bin/aipane-dsh-notify --dry-run
"$HOME/Applications/dsh Notifier.app/Contents/MacOS/dsh-notifier" -status
```

Delivery diagnostics are under `${DSH_HOME:-~/.dsh}/logs/`. A successful sender
exit confirms submission; verify a new tracer in `usernoted` and the
notification database to confirm delivery. Verify the click separately while
the originating tmux pane still exists.

```sh
node --test tests/test_dsh_notifications.mjs
python3 tests/test_dsh_notify.py
python3 tests/test_dsh_focus.py
python3 tests/test_agent_notifications.py
python3 tests/test_agent_notification_sounds.py
```
