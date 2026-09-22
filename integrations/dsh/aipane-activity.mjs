// Native dsh Agent Activity adapter, mounted in the host profile.
// The driver's running/idle boundary includes retries, permission waits,
// cancellation and failure. A blockable Stop hook is not a terminal boundary.

import { spawn } from "node:child_process"
import { join } from "node:path"

export const name = "aipane-activity"
export const inject = ["agents"]

export function apply(ctx) {
  const pane = process.env.TMUX_PANE
  const home = process.env.HOME
  if (!pane || !home) return

  const command = join(home, ".local/bin/aipane-activity")
  const running = new Set()
  let pending = Promise.resolve()
  let reportedState
  let disposed = false

  function report(state) {
    if (state === reportedState) return
    reportedState = state
    // Keep rapid idle -> busy transitions ordered. Detached concurrent
    // commands could finish in reverse order and hide the new turn.
    pending = pending.then(() => new Promise((resolve) => {
      try {
        const child = spawn(command, ["report", state, "--pane", pane], {
          stdio: "ignore",
        })
        const timeout = setTimeout(() => child.kill("SIGKILL"), 3000)
        timeout.unref()
        const finish = () => {
          clearTimeout(timeout)
          resolve()
        }
        child.once("error", finish)
        child.once("close", finish)
      } catch {
        resolve()
      }
    }))
  }

  function publish() {
    report(running.size > 0 ? "busy" : "idle")
  }

  function update(agent, status = agent.status) {
    if (disposed) return
    if (status === "running") running.add(agent)
    else if (status === "idle") running.delete(agent)
    publish()
  }

  ctx.on("agent/status", ({ agent, status }) => update(agent, status))
  ctx.on("agent/created", ({ agent }) => update(agent))
  ctx.on("agent/disposed", ({ agent }) => {
    if (disposed) return
    running.delete(agent)
    publish()
  })

  // Hydrate after subscribing so installing through a live profile patch
  // also handles turns already in flight. Track objects, not reusable ids.
  for (const agent of ctx.agents.list()) {
    if (agent.status === "running") running.add(agent)
  }
  publish()

  ctx.effect(() => async () => {
    disposed = true
    running.clear()
    publish()
    await pending
  })
}
