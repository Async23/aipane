// Native dsh notifications. Observe canonical live session events; Stop hooks
// can continue a turn and restored history must never produce notifications.
import { spawn } from "node:child_process"
import { createHash, randomUUID } from "node:crypto"
import { join } from "node:path"

export const name = "aipane-notify"
export const inject = ["agents"]

const WAIT_DELAY_MS = 300
const HELPER_TIMEOUT_MS = 10_000 // The sender's timeout is eight seconds.
const MAX_TEXT = 4_000

function text(value) {
  return typeof value === "string" ? value.slice(0, MAX_TEXT) : ""
}

function messageText(message) {
  return (message?.content ?? [])
    .filter(block => block.type === "text")
    .map(block => text(block.text)).join("\n").slice(0, MAX_TEXT)
}

export function apply(ctx) {
  if (!process.env.HOME) return
  const command = join(process.env.HOME, ".local/bin/aipane-dsh-notify")
  const states = new WeakMap()
  const waiting = new Set()
  const questionIds = new WeakMap()
  const instance = randomUUID()
  let nextQuestion = 0
  let pending = Promise.resolve()
  let disposed = false

  function diagnose(stage, payload, code) {
    // Never log conversation text, tool input, error messages or environment.
    try {
      ctx.logger?.warn(JSON.stringify({
        adapter: name, stage, event: payload?.event ?? "unknown", code,
        id: createHash("sha256").update(JSON.stringify([
          payload?.session_id, payload?.turn_id, payload?.event_id,
        ])).digest("hex").slice(0, 12),
      }))
    } catch {}
  }

  function enqueue(payload, stillRelevant = () => true) {
    pending = pending.then(() => new Promise(resolve => {
      if (!stillRelevant()) return resolve()
      let child
      let timeout
      let finished = false
      const finish = (code) => {
        if (finished) return
        finished = true
        clearTimeout(timeout)
        if (code) diagnose("helper", payload, code)
        resolve()
      }
      try {
        child = spawn(command, [], { stdio: ["pipe", "ignore", "ignore"] })
        timeout = setTimeout(() => {
          child.kill("SIGKILL")
          finish("timeout")
        }, HELPER_TIMEOUT_MS)
        timeout.unref()
        child.once("error", () => finish("spawn-failed"))
        child.once("close", (code, signal) => finish(
          code === 0 ? undefined : signal ? "signal" : `exit-${code}`,
        ))
        // A missing/exiting helper may close stdin before end() completes.
        child.stdin.on("error", () => {})
        child.stdin.end(JSON.stringify(payload))
      } catch {
        child?.kill("SIGKILL")
        finish("spawn-failed")
      }
    })).catch(() => diagnose("helper", payload, "queue-failed"))
  }

  function rootFor(session) {
    const agent = ctx.agents.get(session.id)
    // Runtime ownership is authoritative. A previously delegated session may
    // be resumed as a human-operated root; durable parentSession is lineage.
    return agent?.session === session && ctx.agents.roots().includes(agent)
      ? agent : undefined
  }

  function stateFor(session) {
    let state = states.get(session)
    if (!state) {
      // Reading a folded title does not replay any lifecycle notifications.
      const title = text(ctx.get?.("sessionTitle")?.get(session)?.title)
      state = { title, turn: undefined, lastSeq: -1 }
      states.set(session, state)
    }
    return state
  }

  function payloadFor(session, state, event, eventId, message = "") {
    return {
      event,
      session_id: String(session.id),
      turn_id: String(state.turn.number),
      event_id: eventId,
      cwd: text(session.header?.cwd),
      session_title: state.title,
      prompt: state.turn.prompt,
      answer: state.turn.answer,
      message,
    }
  }

  function cancelWait(item) {
    item.active = false
    clearTimeout(item.timer)
    waiting.delete(item)
  }

  function clearWaits(session, requestId) {
    for (const item of waiting) {
      if (item.session === session && (requestId === undefined || item.id === requestId)) {
        cancelWait(item)
      }
    }
  }

  function scheduleWait(session, state, event, id, message, signal) {
    const item = { session, id, active: true, timer: undefined }
    const turn = state.turn
    const relevant = () => item.active && !disposed && !signal?.aborted
      && state.turn === turn && Boolean(rootFor(session))
    item.timer = setTimeout(() => {
      if (relevant()) enqueue(payloadFor(session, state, event, id, message), relevant)
    }, WAIT_DELAY_MS)
    item.timer.unref()
    waiting.add(item)
    return item
  }

  function onSessionEvent(session, event) {
    if (disposed || !rootFor(session)) return
    if (!Number.isSafeInteger(event.seq) || event.seq < (session.firstLiveSeq ?? 0)) return
    const state = stateFor(session)
    if (event.seq <= state.lastSeq) return
    state.lastSeq = event.seq
    const data = event.data
    if (event.type === "session/title") {
      state.title = text(data.title)
      return
    }
    if (event.type === "turn/start") {
      clearWaits(session)
      state.turn = { number: data.turn, prompt: "", answer: "", sawStep: false }
      return
    }
    const turn = state.turn
    if (!turn) return // Includes a hot-loaded plugin's already-running turn.
    switch (event.type) {
      case "step/start":
        if (data.turn === turn.number) turn.sawStep = true
        break
      case "user/message":
        if (data.source?.kind === "user") turn.prompt = messageText(data)
        break
      case "assistant/message":
        if (data.turn === turn.number && !data.interrupted) {
          turn.answer = messageText(data.message)
        }
        break
      case "approval/asked":
        scheduleWait(session, state, "approval", `seq:${event.seq}`, "有操作等待你的审批。")
          .id = data.id
        break
      case "approval/decided":
        clearWaits(session, data.id)
        break
      case "turn/end": {
        if (data.turn !== turn.number) break
        clearWaits(session)
        const reason = data.reason
        let notification
        let message = ""
        if (reason?.kind === "completed" && turn.sawStep) notification = "complete"
        else if (reason?.kind === "error") {
          notification = "failure"
          message = "本轮运行失败，请返回终端查看。"
        } else if (reason?.kind === "blocked" ||
          (reason?.kind === "aborted" && reason.reason?.kind === "hook")) {
          notification = "failure"
          message = "本轮运行被阻止，请返回终端查看。"
        } else if (reason?.kind === "max-tokens") {
          notification = "failure"
          message = "本轮达到输出限制，请返回终端查看。"
        }
        if (notification) enqueue(payloadFor(session, state, notification, `seq:${event.seq}`, message))
        state.turn = undefined
        break
      }
    }
  }

  ctx.on("session/event", (session, event) => {
    try {
      onSessionEvent(session, event)
    } catch {
      diagnose("event", undefined, "capture-failed")
    }
  })

  ctx.on("user-questions/request", async (request, next) => {
    let item
    try {
      const session = request.agent?.session
      const state = session && states.get(session)
      if (!disposed && state?.turn && rootFor(session) === request.agent && !request.signal?.aborted
          && !questionIds.has(request)) {
        questionIds.set(request, `${instance}:${++nextQuestion}`)
        item = scheduleWait(session, state, "question", questionIds.get(request),
          text(request.questions?.[0]?.question) || "有问题等待你的回答。", request.signal)
      }
    } catch {
      diagnose("question", undefined, "capture-failed")
    }
    // Preserve the answerer's result, rejection and cancellation semantics.
    try {
      return await next()
    } finally {
      if (item) cancelWait(item)
    }
  }, { prepend: true })

  ctx.on("agent/disposed", ({ agent }) => {
    clearWaits(agent.session)
    states.delete(agent.session)
  })
  ctx.effect(() => async () => {
    disposed = true
    for (const item of waiting) cancelWait(item)
    await pending
  })
}
