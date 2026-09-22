// aipane-copy — a /copy human command for the dsh-TUI slash menu.
//
// dsh's human-command registry (dsh-commands) is the only seam that lets a
// plugin add a slash command without touching the TUI: dsh-tui merges every
// registered descriptor into its menu and dispatches through `execute`, so
// this plugin owns no UI. The handler reads the receiving agent's live session
// log exactly like the TUI's own /export does (assistant/message events with
// text blocks), then writes through the clipboard paths the TUI already uses:
// a native utility first (local sessions), the tmux paste buffer, then OSC 52
// for the terminal — including tmux's DCS passthrough so the outer terminal
// sees it over SSH.
//
// `/copy` is intentionally not a transcript renderer: selected text already
// copies on mouse-up, so this command exists for the keyboard path and for
// "copy that reply I can no longer see".
import { execFile } from "node:child_process"

export const name = "aipane-copy"
export const inject = ["commands"]

const TIMEOUT_MS = 2000
const ESC = "\x1b"
const BEL = "\x07"
const ST = `${ESC}\\`

export const USAGE = "Usage: /copy [n] — n counts assistant replies back from the newest (default 1)"

/** Live session log: alpha.4 exposes `snapshotEvents()`, older lines `events`. */
function sessionEvents(session) {
  if (session === null || typeof session !== "object") return []
  if (typeof session.snapshotEvents === "function") {
    const snapshot = session.snapshotEvents()
    return Array.isArray(snapshot) ? snapshot : []
  }
  return Array.isArray(session.events) ? session.events : []
}

function textOf(content) {
  if (!Array.isArray(content)) return ""
  return content
    .filter(block => block !== null && typeof block === "object"
      && block.type === "text" && typeof block.text === "string")
    .map(block => block.text).join("\n\n").trim()
}

/** Every assistant reply carrying visible text, oldest first. */
export function replyTexts(session) {
  const replies = []
  for (const event of sessionEvents(session)) {
    if (event?.type !== "assistant/message") continue
    const text = textOf(event.data?.message?.content ?? event.data?.content)
    if (text) replies.push(text)
  }
  return replies
}

/** Parse the optional count. Empty input means the newest reply. */
export function parseCount(rawInput) {
  const raw = typeof rawInput === "string" ? rawInput.trim() : ""
  if (raw === "") return 1
  if (!/^[0-9]+$/u.test(raw)) return undefined
  const count = Number(raw)
  return Number.isSafeInteger(count) && count >= 1 ? count : undefined
}

/** Run one helper with `input` on stdin; resolves whether it exited zero. */
function run(file, args, input) {
  return new Promise(resolve => {
    let settled = false
    const finish = (ok) => {
      if (settled) return
      settled = true
      resolve(ok)
    }
    let child
    try {
      child = execFile(file, args, { timeout: TIMEOUT_MS }, error => finish(!error))
    } catch {
      finish(false)
      return
    }
    child.once("error", () => finish(false))
    if (child.stdin) {
      child.stdin.on("error", () => {})
      child.stdin.end(input)
    }
  })
}

/**
 * Native clipboard utility. Skipped in an SSH session, where it would write
 * the remote machine's clipboard instead of the local one.
 */
export async function copyNative(text) {
  if (process.env.SSH_CONNECTION) return false
  switch (process.platform) {
    case "darwin":
      return run("pbcopy", [], text)
    case "win32":
      return run("clip", [], text)
    case "linux":
      return await run("wl-copy", [], text)
        || await run("xclip", ["-selection", "clipboard"], text)
        || await run("xsel", ["--clipboard", "--input"], text)
    default:
      return false
  }
}

/** Native tool name for the settled report. */
function nativeName() {
  return process.platform === "darwin" ? "pbcopy"
    : process.platform === "win32" ? "clip"
      : "native clipboard tool"
}

/**
 * Load the tmux paste buffer. `-w` (tmux ≥3.2) also propagates to the outer
 * terminal; it is dropped for iTerm2, whose session tmux's own OSC 52
 * emission crashes over SSH.
 */
export function tmuxLoadBuffer(text) {
  if (!process.env.TMUX) return Promise.resolve(false)
  const args = process.env.LC_TERMINAL === "iTerm2"
    ? ["load-buffer", "-"]
    : ["load-buffer", "-w", "-"]
  return run("tmux", args, text)
}

/** OSC 52 write; inside tmux it rides a DCS passthrough to the outer terminal. */
export function oscSequence(text) {
  const payload = `${ESC}]52;c;${Buffer.from(text, "utf8").toString("base64")}${BEL}`
  return process.env.TMUX
    ? `${ESC}Ptmux;${payload.replaceAll(ESC, ESC + ESC)}${ST}`
    : payload
}

/**
 * Write `text` to the clipboard through every applicable path.
 * @param text - the reply to place on the clipboard.
 * @param write - sink for the OSC 52 sequence (injectable for tests).
 * @returns the confirmed paths and whether OSC 52 was emitted.
 */
export async function copyToClipboard(text, write = chunk => { process.stdout.write(chunk) }) {
  const confirmed = []
  if (await copyNative(text)) confirmed.push(nativeName())
  if (await tmuxLoadBuffer(text)) confirmed.push("tmux buffer")
  let osc = false
  if (typeof write === "function") {
    write(oscSequence(text))
    osc = true
  }
  return { ok: confirmed.length > 0 || osc, confirmed, osc }
}

/** Command handler: resolve the reply, copy it, report what happened. */
export async function handleCopy(invocation, copy = copyToClipboard) {
  const back = parseCount(invocation?.rawInput)
  if (back === undefined) return { kind: "error", text: USAGE }
  const replies = replyTexts(invocation?.agent?.session)
  if (replies.length === 0) {
    return { kind: "error", text: "No assistant reply in this session yet." }
  }
  if (back > replies.length) {
    return {
      kind: "error",
      text: `This session has ${replies.length} assistant ${replies.length === 1 ? "reply" : "replies"}; /copy ${back} is out of range.`,
    }
  }
  const text = replies[replies.length - back]
  const result = await copy(text)
  if (!result.ok) {
    return { kind: "error", text: "Clipboard unavailable: no working pbcopy, tmux buffer or OSC 52 path." }
  }
  const what = back === 1 ? "the newest reply" : `reply #${back} from the end`
  const via = result.confirmed.length > 0
    ? result.confirmed.join(" + ")
    : "OSC 52 only (a terminal may ignore it)"
  return { kind: "success", text: `Copied ${what} — ${text.length} chars via ${via}.` }
}

export function apply(ctx, config = {}) {
  // The Web host has its own browser-side command surface, and a clipboard
  // write from the host process would target the wrong machine. The profile
  // row still exists there so the installation audit keeps profile parity.
  if (config?.mode === "web") return
  ctx.commands.register({
    name: "copy",
    description: "copy an assistant reply to the clipboard",
    input: { hint: "[n]" },
    handler: invocation => handleCopy(invocation),
  })
}
