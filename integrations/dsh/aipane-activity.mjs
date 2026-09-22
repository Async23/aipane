// Native dsh Agent Activity adapter, mounted in the host profile.
// The driver's running/idle boundary includes retries, permission waits,
// cancellation and failure. A blockable Stop hook is not a terminal boundary.

import { spawn } from "node:child_process"
import { randomUUID } from "node:crypto"
import { mkdirSync, readFileSync, renameSync, unlinkSync, writeFileSync } from "node:fs"
import { join, resolve } from "node:path"

export const name = "aipane-activity"
export const inject = ["agents"]

export function apply(ctx) {
  const pane = process.env.TMUX_PANE
  const home = process.env.HOME
  if (!pane || !home) return

  const command = join(home, ".local/bin/aipane-activity")
  const running = new Set()
  const agents = new Map()
  const instance = randomUUID()
  const runtimeDirectory = join(resolve(process.env.DSH_HOME || join(home, ".dsh")), "aipane/activity")
  const runtimePath = join(runtimeDirectory, `${process.pid}.json`)
  const tmux = (process.env.TMUX || "").split(",")
  const serverPid = tmux.at(-2) || ""
  const socket = tmux.slice(0, -2).join(",")
  const reference = { path: runtimePath, pid: process.pid, instance }
  let pending = Promise.resolve()
  let queued = 0
  let queuedState
  let confirmedState
  let sequence = 0
  let writeFailed = false
  let claimed = false
  let superseded = false
  let disposed = false

  function diagnose(stage) {
    try { ctx.logger?.warn(JSON.stringify({ adapter: name, stage })) } catch {}
  }

  function writeRuntime() {
    if (superseded) return false
    try {
      if (claimed) {
        try {
          const previous = JSON.parse(readFileSync(runtimePath, "utf8"))
          if (previous.instance !== instance) { superseded = true; return false }
        } catch (error) {
          if (error.code !== "ENOENT") throw error
        }
      }
      const roots = new Set(ctx.agents.roots?.() || [])
      const entries = [...agents]
      const snapshot = entries.map(([agent, status]) => ({
        id: agent.session?.id || agent.id || "", status,
        ...(roots.has(agent) ? { cwd: agent.session?.header?.cwd || "" } : {}),
      }))
      const value = {
        version: 1, instance, pid: process.pid, sequence, updated_at: Date.now(),
        pane_id: pane, socket, server_pid: serverPid,
        state: running.size > 0 ? "busy" : "idle",
        agents: snapshot,
        roots: snapshot.filter((_, index) => roots.has(entries[index][0])),
      }
      mkdirSync(runtimeDirectory, { recursive: true, mode: 0o700 })
      const temporary = `${runtimePath}.${instance}.tmp`
      writeFileSync(temporary, JSON.stringify(value), { mode: 0o600 })
      renameSync(temporary, runtimePath)
      claimed = true
      writeFailed = false
      return true
    } catch {
      if (!writeFailed) diagnose("runtime-write-failed")
      writeFailed = true
      return false
    }
  }

  function report(state) {
    if (state === queuedState) return
    queuedState = state
    queued++
    // Keep rapid idle -> busy transitions ordered. Detached concurrent
    // commands could finish in reverse order and hide the new turn.
    pending = pending.then(() => new Promise((resolve) => {
      let finished = false
      let timeout
      const finish = (success) => {
        if (finished) return
        finished = true
        clearTimeout(timeout)
        if (success) confirmedState = state
        else diagnose("report-failed")
        queued--
        if (!queued) queuedState = confirmedState
        resolve()
      }
      try {
        const child = spawn(command, ["report", state, "--pane", pane], {
          stdio: ["pipe", "ignore", "ignore"],
        })
        timeout = setTimeout(() => { child.kill("SIGKILL"); finish(false) }, 3000)
        timeout.unref()
        child.once("error", () => finish(false))
        child.once("close", code => finish(code === 0))
        child.stdin.on("error", () => {})
        child.stdin.end(JSON.stringify({ dsh_runtime: reference }))
      } catch {
        finish(false)
      }
    }))
  }

  function publish() {
    sequence++
    if (writeRuntime()) report(running.size > 0 ? "busy" : "idle")
  }

  function update(agent, status = agent.status) {
    if (disposed) return
    if (status !== "running" && status !== "idle") return
    agents.set(agent, status)
    if (status === "running") running.add(agent)
    else if (status === "idle") running.delete(agent)
    publish()
  }

  ctx.on("agent/status", ({ agent, status }) => update(agent, status))
  ctx.on("agent/created", ({ agent }) => update(agent))
  ctx.on("agent/disposed", ({ agent }) => {
    if (disposed) return
    running.delete(agent)
    agents.delete(agent)
    publish()
  })

  // Hydrate after subscribing so installing through a live profile patch
  // also handles turns already in flight. Track objects, not reusable ids.
  for (const agent of ctx.agents.list()) {
    if (agent.status === "running" || agent.status === "idle") agents.set(agent, agent.status)
    if (agent.status === "running") running.add(agent)
  }
  publish()

  // Refresh native evidence independently of the helper. Retry a failed final
  // report without waiting for another user turn; never grow an in-flight queue.
  const heartbeat = setInterval(() => {
    if (!disposed && writeRuntime() && queued === 0) {
      report(running.size > 0 ? "busy" : "idle")
    }
  }, 2000)
  heartbeat.unref()

  ctx.effect(() => async () => {
    disposed = true
    clearInterval(heartbeat)
    running.clear()
    agents.clear()
    publish()
    await pending
    // An old plugin teardown must not remove a hot-loaded replacement's file.
    try {
      if (JSON.parse(readFileSync(runtimePath, "utf8")).instance === instance) unlinkSync(runtimePath)
    } catch {}
  })
}
