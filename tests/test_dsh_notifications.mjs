import assert from "node:assert/strict";
import { execFile } from "node:child_process";
import { mkdtemp, mkdir, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { promisify } from "node:util";
import test from "node:test";

const execFileAsync = promisify(execFile);
const pluginUrl = new URL("../integrations/dsh/aipane-notify.mjs", import.meta.url).href;

// Run the real plugin and a fake executable in an isolated HOME. Never invoke
// the workstation's sender, notification center, tmux or a model.
const exercise = `
  const plugin = await import(process.env.AIPANE_TEST_PLUGIN);
  const scenario = JSON.parse(process.env.AIPANE_TEST_SCENARIO);
  const agents = new Map(scenario.agents.map(({ key, ...spec }) => {
    const session = { id: spec.id ?? key, header: spec.header ?? { cwd: "/tmp/project" },
      firstLiveSeq: spec.firstLiveSeq ?? 0 };
    return [key, { id: session.id, session, root: spec.root ?? true }];
  }));
  const handlers = new Map();
  const disposers = [];
  const diagnostics = [];
  const questions = new Map();
  const results = [];
  const ctx = {
    agents: {
      get: id => [...agents.values()].find(agent => agent.id === id && !agent.removed),
      roots: () => [...agents.values()].filter(agent => agent.root && !agent.removed),
    },
    logger: { warn: message => diagnostics.push(JSON.parse(message)) },
    get: name => name === "sessionTitle" ? { get: () => ({ title: scenario.title ?? "" }) } : undefined,
    on: (event, handler) => handlers.set(event, handler),
    effect: setup => disposers.push(setup()),
  };
  plugin.apply(ctx);
  for (const action of scenario.actions) {
    const agent = agents.get(action.key ?? "main");
    if (action.type === "pause") await new Promise(resolve => setTimeout(resolve, action.ms));
    else if (action.type === "root") agent.root = action.value;
    else if (action.type === "remove") {
      agent.removed = true;
      handlers.get("agent/disposed")?.({ agent });
    } else if (action.type === "question") {
      const abort = new AbortController();
      if (action.aborted) abort.abort();
      const deferred = Promise.withResolvers();
      const request = { agent, signal: abort.signal,
        questions: [{ id: "choice", question: "Which option?" }] };
      const promise = handlers.get("user-questions/request")(request, () => deferred.promise)
        .then(value => results.push({ value }), error => results.push({ error: error.message }));
      questions.set(action.id, { ...deferred, abort, promise });
    } else if (action.type === "answer") {
      const question = questions.get(action.id);
      if (action.error) question.reject(new Error(action.error));
      else question.resolve({ answers: [{ id: "choice", selected: ["A"] }] });
      await question.promise;
    } else if (action.type === "abort") questions.get(action.id).abort.abort();
    else if (action.type === "bus") handlers.get(action.event)?.({ agent, turn: 1 });
    else handlers.get("session/event")(agent.session, action.event);
  }
  for (const dispose of disposers.reverse()) await dispose();
  process.stdout.write(JSON.stringify({ diagnostics, results }));
`;

async function run(actions, options = {}) {
  const directory = await mkdtemp(join(tmpdir(), "aipane-dsh-notify-test-"));
  try {
    const bin = join(directory, ".local", "bin");
    const log = join(directory, "notifications.jsonl");
    await mkdir(bin, { recursive: true });
    if (options.helper !== "missing") {
      await writeFile(join(bin, "aipane-dsh-notify"), `#!${process.execPath}
        const fs = require("node:fs");
        let source = "";
        process.stdin.on("data", data => source += data);
        process.stdin.on("end", () => {
          const payload = JSON.parse(source);
          if (process.env.AIPANE_TEST_HELPER === "failure") process.exit(9);
          if (process.env.AIPANE_TEST_HELPER === "hang") return setInterval(() => {}, 1000);
          setTimeout(() => fs.appendFileSync(process.env.AIPANE_TEST_LOG,
            JSON.stringify(payload) + "\\n"), Number(process.env.AIPANE_TEST_DELAY));
        });
      `, { mode: 0o755 });
    }
    const { stdout, stderr } = await execFileAsync(process.execPath,
      ["--input-type=module", "--eval", exercise], {
        timeout: 15_000,
        env: { ...process.env, HOME: directory,
          AIPANE_TEST_PLUGIN: pluginUrl, AIPANE_TEST_LOG: log,
          AIPANE_TEST_SCENARIO: JSON.stringify({ actions, agents: options.agents ?? [{ key: "main" }], title: options.title }),
          AIPANE_TEST_HELPER: options.helper ?? "success",
          AIPANE_TEST_DELAY: String(options.delay ?? 0),
        },
      });
    assert.equal(stderr, "");
    const source = await readFile(log, "utf8").catch(error => {
      if (error.code === "ENOENT") return "";
      throw error;
    });
    return { ...JSON.parse(stdout), notifications: source.trim().split("\n").filter(Boolean).map(JSON.parse) };
  } finally {
    await rm(directory, { recursive: true, force: true });
  }
}

const event = (seq, type, data, key = "main") => ({ key, event: { seq, type, data } });
const start = (seq = 0, turn = 1, key = "main") => event(seq, "turn/start", { turn }, key);
const step = (seq = 1, turn = 1, key = "main") => event(seq, "step/start", { turn, step: 1 }, key);
const end = (seq = 2, kind = "completed", turn = 1, key = "main", extra = {}) =>
  event(seq, "turn/end", { turn, reason: { kind, ...extra } }, key);
const pause = ms => ({ type: "pause", ms });

test("complete fires once at canonical turn/end with exact text and session title", async () => {
  const closing = end(8);
  const { notifications, diagnostics } = await run([
    start(), step(),
    event(2, "session/title", { title: "Fix notification icons" }),
    event(3, "user/message", { source: { kind: "user" }, content: [{ type: "text", text: "Use icon 04" }] }),
    event(4, "user/message", { source: { kind: "plugin" }, content: [{ type: "text", text: "HIDDEN CONTEXT" }] }),
    event(5, "assistant/message", { turn: 1, message: { content: [
      { type: "reasoning", text: "PRIVATE REASONING" }, { type: "text", text: "Done." },
    ] } }),
    { type: "bus", event: "agent/turn-stopping" },
    closing, closing,
  ]);
  assert.deepEqual(diagnostics, []);
  assert.deepEqual(notifications, [{ event: "complete", session_id: "main", turn_id: "1", event_id: "seq:8",
    cwd: "/tmp/project", session_title: "Fix notification icons", prompt: "Use icon 04", answer: "Done.", message: "" }]);
});

test("startup, replay, interrupted history, an empty turn, and Stop do not notify", async () => {
  const { notifications } = await run([
    start(0), step(1), end(2),
    end(10), // Turn already running when the plugin was installed.
    start(11), end(12, "interrupted"),
    start(13, 2), end(14, "completed", 2),
    start(15, 3), step(16, 3), { type: "bus", event: "agent/turn-stopping" },
  ], { agents: [{ key: "main", firstLiveSeq: 10 }] });
  assert.deepEqual(notifications, []);
});

test("live roots notify; owned subagents do not; resumed lineage can be a root", async () => {
  const { notifications } = await run([
    start(0, 1, "child"), step(1, 1, "child"), end(2, "completed", 1, "child"),
    start(0, 1, "resumed"), step(1, 1, "resumed"), end(2, "completed", 1, "resumed"),
    start(), step(), end(),
  ], { agents: [{ key: "main" }, { key: "child", root: false },
    { key: "resumed", header: { cwd: "/tmp/resumed", parentSession: "parent", origin: "subagent" } }] });
  assert.deepEqual(notifications.map(item => item.session_id), ["resumed", "main"]);
});

test("failures notify with safe descriptions while user/parent/disposal cancels are silent", async () => {
  const actions = [];
  const reasons = ["error", "blocked", "max-tokens", "aborted", "aborted", "aborted", "aborted"];
  const extras = [{ error: { message: "TOKEN=SECRET", code: "UNKNOWN" } }, {}, {},
    { reason: { kind: "user" } }, { reason: { kind: "parent" } },
    { reason: { kind: "disposed" } }, { reason: { kind: "hook", reason: "TOKEN=SECRET" } }];
  reasons.forEach((kind, index) => actions.push(start(index * 3, index + 1),
    step(index * 3 + 1, index + 1), end(index * 3 + 2, kind, index + 1, "main", extras[index])));
  const { notifications, diagnostics } = await run(actions);
  assert.deepEqual(notifications.map(item => item.event), Array(4).fill("failure"));
  assert.equal(JSON.stringify({ notifications, diagnostics }).includes("SECRET"), false);
});

test("approval only notifies when actually pending and never copies raw tool input", async () => {
  const { notifications } = await run([
    start(), step(),
    event(2, "tool/call", { name: "shell", arguments: "SECRET" }),
    event(3, "approval/asked", { id: "automatic", reason: "SECRET" }),
    event(4, "approval/decided", { id: "automatic", outcome: "allowed-once" }),
    event(5, "approval/asked", { id: "human", reason: "SECRET" }),
    pause(450),
    event(6, "approval/decided", { id: "human", outcome: "allowed-once" }), end(7),
  ]);
  assert.deepEqual(notifications.map(item => item.event), ["approval", "complete"]);
  assert.equal(notifications[0].event_id, "seq:5");
  assert.equal(JSON.stringify(notifications).includes("SECRET"), false);
});

test("questions preserve answer/rejection and notify only while unanswered", async () => {
  const { notifications, results } = await run([
    start(), step(),
    { type: "question", id: "fast" }, { type: "answer", id: "fast" },
    { type: "question", id: "failed" }, { type: "answer", id: "failed", error: "NO_PROVIDER" },
    { type: "question", id: "pending" }, pause(450), { type: "answer", id: "pending" },
    end(),
  ]);
  assert.deepEqual(notifications.map(item => item.event), ["question", "complete"]);
  assert.equal(notifications[0].message, "Which option?");
  assert.equal(results.length, 3);
  assert.equal(results[1].error, "NO_PROVIDER");
  assert.deepEqual(results[2].value, { answers: [{ id: "choice", selected: ["A"] }] });
});

test("aborted and delegated questions do not notify or consume the answer", async () => {
  const { notifications, results } = await run([
    start(), step(), { type: "question", id: "cancelled" },
    { type: "abort", id: "cancelled" }, pause(350), { type: "answer", id: "cancelled" },
    { type: "question", id: "already", aborted: true }, pause(350), { type: "answer", id: "already" },
    start(0, 1, "child"), step(1, 1, "child"),
    { type: "question", id: "delegated", key: "child" }, pause(350), { type: "answer", id: "delegated" },
  ], { agents: [{ key: "main" }, { key: "child", root: false }] });
  assert.deepEqual(notifications, []);
  assert.equal(results.length, 3);
});

test("serialized helpers preserve rapid turn order and skip waits resolved while queued", async () => {
  const { notifications } = await run([
    start(), step(), end(), start(3, 2), step(4, 2),
    event(5, "approval/asked", { id: "already-answered" }), pause(350),
    event(6, "approval/decided", { id: "already-answered", outcome: "allowed-once" }), end(7, "completed", 2),
  ], { delay: 450 });
  assert.deepEqual(notifications.map(item => [item.event, item.turn_id]), [["complete", "1"], ["complete", "2"]]);
});

test("disposed agents and plugin teardown cancel pending waits", async () => {
  const { notifications } = await run([
    start(), step(), event(2, "approval/asked", { id: "removed" }),
    { type: "remove" }, pause(350),
  ]);
  assert.deepEqual(notifications, []);
  const tornDown = await run([start(), step(), event(2, "approval/asked", { id: "teardown" })]);
  assert.deepEqual(tornDown.notifications, []);
});

test("folded title is read without replaying history", async () => {
  const { notifications } = await run([start(), step(), end()], { title: "Previously named session" });
  assert.equal(notifications[0].session_title, "Previously named session");
});

for (const helper of ["missing", "failure", "hang"]) {
  test(`a ${helper} helper cannot reject dsh events and logs safe diagnostics`, async () => {
    const { notifications, diagnostics } = await run([
      start(), step(), event(2, "user/message", { source: { kind: "user" },
        content: [{ type: "text", text: "DO NOT LOG THIS SECRET" }] }), end(3),
    ], { helper });
    assert.deepEqual(notifications, []);
    assert.equal(diagnostics.length, 1);
    assert.equal(diagnostics[0].stage, "helper");
    assert.equal(diagnostics[0].event, "complete");
    assert.equal(JSON.stringify(diagnostics).includes("SECRET"), false);
  });
}
