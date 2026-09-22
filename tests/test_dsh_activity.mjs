import assert from "node:assert/strict";
import { execFile } from "node:child_process";
import { mkdtemp, mkdir, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { promisify } from "node:util";
import test from "node:test";

const execFileAsync = promisify(execFile);
const pluginUrl = new URL("../integrations/dsh/aipane-activity.mjs", import.meta.url).href;

// Run the real plugin in a child so HOME and TMUX_PANE cannot affect the workstation.
const exercisePlugin = `
  import assert from "node:assert/strict";
  const plugin = await import(process.env.AIPANE_TEST_PLUGIN);
  assert.equal(plugin.name, "aipane-activity");
  assert.deepEqual(plugin.inject, ["agents"]);
  const scenario = JSON.parse(process.env.AIPANE_TEST_SCENARIO);
  const agents = new Map(scenario.agents.map(({ key, ...agent }) => [key, agent]));
  const handlers = new Map();
  const disposers = [];
  const ctx = {
    agents: { list: () => scenario.initial.map(key => agents.get(key)) },
    on(event, handler) {
      const listeners = handlers.get(event) ?? [];
      listeners.push(handler);
      handlers.set(event, listeners);
      return () => {};
    },
    effect(setup) {
      const dispose = setup();
      disposers.push(dispose);
      return dispose;
    },
  };
  plugin.apply(ctx);
  // Cordis emits lifecycle events synchronously; do not wait between transitions.
  for (const { event, key, status } of scenario.events) {
    const agent = agents.get(key);
    if (event === "agent/status") agent.status = status;
    for (const handler of handlers.get(event) ?? []) handler({ agent, status });
  }
  for (const dispose of disposers.reverse()) await dispose();
`;

async function reportsFor({
  agents = [{ key: "main", id: "main", status: "idle" }],
  initial = ["main"],
  events = [],
  inTmux = true,
  helperExists = true,
} = {}) {
  const directory = await mkdtemp(join(tmpdir(), "aipane-dsh-test-"));
  try {
    const bin = join(directory, ".local", "bin");
    const log = join(directory, "reports.jsonl");
    await mkdir(bin, { recursive: true });
    if (helperExists) {
      await writeFile(join(bin, "aipane-activity"), `#!${process.execPath}
        const { appendFileSync } = require("node:fs");
        const args = process.argv.slice(2);
        // Concurrent writes would finish idle before busy and fail ordering checks.
        setTimeout(() => appendFileSync(process.env.AIPANE_TEST_LOG,
          JSON.stringify({ args, pane: process.env.TMUX_PANE }) + "\\n"
        ), args[1] === "busy" ? 70 : 0);
      `, { mode: 0o755 });
    }
    const env = {
      ...process.env,
      HOME: directory,
      AIPANE_TEST_PLUGIN: pluginUrl,
      AIPANE_TEST_LOG: log,
      AIPANE_TEST_SCENARIO: JSON.stringify({ agents, initial, events }),
    };
    if (inTmux) env.TMUX_PANE = "%42";
    else delete env.TMUX_PANE;
    const { stdout, stderr } = await execFileAsync(process.execPath,
      ["--input-type=module", "--eval", exercisePlugin], { env, timeout: 10_000 });
    assert.equal(stdout, "");
    assert.equal(stderr, "");
    const contents = await readFile(log, "utf8").catch(error => {
      if (error.code === "ENOENT") return "";
      throw error;
    });
    return contents.trim() ? contents.trim().split("\n").map(line => JSON.parse(line)) : [];
  } finally {
    await rm(directory, { recursive: true, force: true });
  }
}

function expectStates(reports, states) {
  assert.deepEqual(reports, states.map(state => ({
    args: ["report", state, "--pane", "%42"],
    pane: "%42",
  })));
}

test("rapid running / idle / running transitions finish in order and disposal clears busy", async () => {
  const reports = await reportsFor({ events: [
    { event: "agent/status", key: "main", status: "running" },
    { event: "agent/status", key: "main", status: "idle" },
    { event: "agent/status", key: "main", status: "running" },
  ] });
  expectStates(reports, ["idle", "busy", "idle", "busy", "idle"]);
});

test("all concurrent agents must stop before the pane becomes idle", async () => {
  const reports = await reportsFor({
    agents: [
      { key: "main", id: "main", status: "idle" },
      { key: "child", id: "child", status: "idle" },
      { key: "unused", id: "unused", status: "idle" },
    ],
    events: [
      { event: "agent/status", key: "main", status: "running" },
      { event: "agent/created", key: "child" },
      { event: "agent/status", key: "child", status: "running" },
      { event: "agent/status", key: "main", status: "idle" },
      { event: "agent/created", key: "unused" },
      { event: "agent/disposed", key: "child" },
    ],
  });
  expectStates(reports, ["idle", "busy", "idle"]);
});

test("disposing an older agent with a reused id cannot clear the new agent's busy state", async () => {
  const reports = await reportsFor({
    agents: [
      { key: "old", id: "same-session", status: "running" },
      { key: "replacement", id: "same-session", status: "running" },
      { key: "other", id: "other", status: "idle" },
    ],
    initial: ["old"],
    events: [
      { event: "agent/created", key: "replacement" },
      { event: "agent/disposed", key: "old" },
      { event: "agent/status", key: "other", status: "running" },
      { event: "agent/status", key: "other", status: "idle" },
      { event: "agent/status", key: "replacement", status: "idle" },
    ],
  });
  expectStates(reports, ["busy", "idle"]);
});

test("hot loading while an existing agent runs reports busy immediately", async () => {
  const reports = await reportsFor({
    agents: [{ key: "main", id: "main", status: "running" }],
  });
  expectStates(reports, ["busy", "idle"]);
});

test("loading with idle agents clears a stale marker without redundant idle reports", async () => {
  expectStates(await reportsFor(), ["idle"]);
});

test("outside tmux there are no activity writes", async () => {
  assert.deepEqual(await reportsFor({ inTmux: false, events: [
    { event: "agent/status", key: "main", status: "running" },
  ] }), []);
});

test("a missing activity command does not reject events or plugin disposal", async () => {
  assert.deepEqual(await reportsFor({ helperExists: false, events: [
    { event: "agent/status", key: "main", status: "running" },
    { event: "agent/status", key: "main", status: "idle" },
  ] }), []);
});
