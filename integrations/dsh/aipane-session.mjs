// The TUI channel owns the foreground session; background roots and detached
// forks must never replace its binding. Keep the installed-package seam here.
import { spawn, execFileSync } from "node:child_process"
import { mkdirSync, writeFileSync, renameSync, rmSync } from "node:fs"
import { join, resolve } from "node:path"
import { pathToFileURL } from "node:url"
import { randomUUID } from "node:crypto"
import { readFileSync } from "node:fs"

export const name = "aipane-dsh-session"
export const inject = ["agents"]

export async function apply(ctx, config = {}) {
  if (config.mode === "web") return
  const { HOME: home, TMUX_PANE: pane, TMUX: tmux } = process.env
  if (!home || !pane || !tmux) return
  const dshHome = resolve(process.env.DSH_HOME || join(home, ".dsh"))
  const registryPath = join(dshHome, "profiles/dsh-tui/node_modules/@deepseek-harness-tui/dsh-tui/lib/types/adapter/channel/host-registry.js")
  let registry
  try { registry = await import(pathToFileURL(registryPath).href) }
  catch {
    ctx.logger?.warn("aipane: dsh TUI session registry unavailable; exact session binding disabled")
    return
  }
  const directory = join(dshHome, "aipane/sessions")
  const path = join(directory, `${process.pid}.json`)
  const tmuxParts = tmux.split(",")
  const serverPid = tmuxParts.at(-2), socket = tmuxParts.slice(0, -2).join(",")
  const instance = randomUUID()
  let processStarted
  try {
    processStarted = execFileSync("ps", ["-p", String(process.pid), "-o", "lstart="], {
      encoding: "utf8", timeout: 2000, env: { ...process.env, LC_ALL: "C" },
    }).trim()
  } catch { return }
  let channel, unsubscribe, disposed = false, bound = "", pending = Promise.resolve()
  let claimed = false, superseded = false

  function removeOwned() {
    try {
      if (JSON.parse(readFileSync(path, "utf8")).instance === instance) {
        rmSync(path, { force: true })
        claimed = false
      }
    } catch {}
  }

  function publish() {
    if (disposed || superseded) return
    if (claimed) {
      try {
        if (JSON.parse(readFileSync(path, "utf8")).instance !== instance) {
          superseded = true
          return
        }
      } catch { return }
    }
    const agent = channel && ctx.agents.get(channel.agentId)
    if (!agent || !ctx.agents.roots().includes(agent)) {
      bound = ""
      removeOwned()
      return
    }
    const sid = String(agent.session.id)
    const cwd = agent.session.header.cwd
    if (!cwd) { bound = ""; removeOwned(); return }
    try {
      mkdirSync(directory, { recursive: true, mode: 0o700 })
      writeFileSync(`${path}.${instance}.tmp`, JSON.stringify({
        version: 1, instance, pid: process.pid, process_started: processStarted,
        updated_at: Date.now(), pane_id: pane, socket, server_pid: serverPid,
        session_id: sid, cwd,
        dsh_home: dshHome,
        session_root: resolve(ctx.get("sessionPersistence")?.config?.root || process.env.DSH_TUI_SESSION_ROOT || join(dshHome, "sessions")),
      }), { mode: 0o600 })
      renameSync(`${path}.${instance}.tmp`, path)
      claimed = true
    } catch { return }
    if (bound === sid) return
    bound = sid
    pending = pending.then(() => new Promise(resolve => {
      let child
      try {
        child = spawn(join(home, ".local/bin/aipane-bind"),
          ["--tool", "d", "--sid", sid, "--pane", pane], { cwd, stdio: "ignore" })
      } catch { if (bound === sid) bound = ""; resolve(); return }
      const timeout = setTimeout(() => child.kill("SIGKILL"), 3000)
      timeout.unref()
      const done = (failed) => {
        clearTimeout(timeout)
        if (failed && bound === sid) bound = ""
        resolve()
      }
      child.once("error", () => done(true))
      child.once("close", code => done(code !== 0))
    }))
  }
  function attach(next) {
    unsubscribe?.()
    channel = next
    unsubscribe = channel?.subscribe(publish)
    publish()
  }
  const unregister = registry.onTuiChannelRegistered(ctx, attach)
  attach(registry.getRegisteredTuiChannel(ctx))
  const heartbeat = setInterval(publish, 2000)
  heartbeat.unref()
  ctx.effect(() => async () => {
    disposed = true
    clearInterval(heartbeat)
    unregister()
    unsubscribe?.()
    removeOwned()
    await pending
  })
}
