// OpenCode Adapter for aipane session binding and Agent Activity.
//
// OpenCode creates its session id lazily, so this plugin records the
// pane-to-session binding on the first message. It also translates OpenCode
// session lifecycle events to the Agent Activity Interface.
// All integration work is best-effort and never blocks OpenCode.

import { spawn } from "node:child_process"
import { appendFileSync } from "node:fs"

const HOME = process.env.HOME || ""
const BIND = HOME + "/.local/bin/aipane-bind"
const ACTIVITY = HOME + "/.local/bin/aipane-activity"

function debug(msg) {
  if (!process.env.AIPANE_OPENCODE_DEBUG) return
  try {
    appendFileSync(
      HOME + "/.local/share/aipane/opencode-plugin.log",
      new Date().toISOString() + " " + msg + "\n",
    )
  } catch {}
}

function spawnDetached(command, args) {
  try {
    const child = spawn(command, args, { stdio: "ignore", detached: true })
    child.on("error", () => {})
    child.unref()
  } catch {}
}

function reportActivity(state) {
  const pane = process.env.TMUX_PANE
  if (!pane) return
  debug("activity pane=" + pane + " state=" + state)
  spawnDetached(ACTIVITY, ["report", state, "--pane", pane])
}

function createActivityTracker(report) {
  const trackedSessions = new Set()
  const busySessions = new Set()
  let reportedState

  const sync = () => {
    const state = busySessions.size > 0 ? "busy" : "idle"
    if (state === reportedState) return
    reportedState = state
    report(state)
  }

  return {
    track(sessionID) {
      if (!sessionID) return
      const sid = String(sessionID)
      trackedSessions.add(sid)
      busySessions.add(sid)
      sync()
    },
    update(sessionID, status) {
      if (!sessionID) return
      const sid = String(sessionID)
      if (!trackedSessions.has(sid)) return
      if (status === "idle") busySessions.delete(sid)
      else busySessions.add(sid)
      sync()
    },
    finish(sessionID) {
      if (sessionID) busySessions.delete(String(sessionID))
      else busySessions.clear()
      sync()
    },
    forget(sessionID) {
      if (!sessionID) return
      const sid = String(sessionID)
      trackedSessions.delete(sid)
      busySessions.delete(sid)
      sync()
    },
    clear() {
      busySessions.clear()
      sync()
    },
  }
}

export default {
  id: "aipane-bind",
  server: async () => {
    const seen = new Set()
    const activity = createActivityTracker(reportActivity)
    debug("plugin loaded (pane=" + (process.env.TMUX_PANE || "") + ")")
    activity.clear()

    const bind = (sid) => {
      const pane = process.env.TMUX_PANE
      if (!pane || !sid || seen.has(sid)) return
      seen.add(sid)
      debug("bind pane=" + pane + " sid=" + sid)
      spawnDetached(BIND, [
        "--tool",
        "o",
        "--sid",
        String(sid),
        "--pane",
        pane,
        "--cmd",
        "opencode",
      ])
    }

    return {
      "chat.message": async (input) => {
        const sessionID = input && input.sessionID
        bind(sessionID)
        activity.track(sessionID)
      },
      event: async ({ event }) => {
        if (!event) return
        const properties = event.properties || {}
        if (event.type === "session.status") {
          activity.update(properties.sessionID, properties.status && properties.status.type)
        } else if (event.type === "session.idle") {
          activity.finish(properties.sessionID)
        } else if (event.type === "session.error") {
          activity.finish(properties.sessionID)
        } else if (event.type === "session.deleted") {
          activity.forget(properties.info && properties.info.id)
        }
      },
      dispose: async () => {
        activity.clear()
      },
    }
  },
}
