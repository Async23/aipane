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
  if (scenario.holdMs) await new Promise(resolve => setTimeout(resolve, scenario.holdMs));
  for (const dispose of disposers.reverse()) await dispose();
`;

async function reportsFor({
  agents = [{ key: "main", id: "main", status: "idle" }],
  initial = ["main"],
  events = [],
  inTmux = true,
  helperExists = true,
  failOnce = false,
  holdMs = 0,
} = {}) {
  const directory = await mkdtemp(join(tmpdir(), "aipane-dsh-test-"));
  try {
    const bin = join(directory, ".local", "bin");
    const log = join(directory, "reports.jsonl");
    await mkdir(bin, { recursive: true });
    if (helperExists) {
      await writeFile(join(bin, "aipane-activity"), `#!${process.execPath}
        const { appendFileSync, existsSync, writeFileSync } = require("node:fs");
        if (process.env.AIPANE_TEST_FAIL_ONCE === "1" && !existsSync(process.env.AIPANE_TEST_LOG + ".attempt")) {
          writeFileSync(process.env.AIPANE_TEST_LOG + ".attempt", "failed");
          process.exit(1);
        }
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
      DSH_HOME: join(directory, ".dsh"),
      AIPANE_TEST_PLUGIN: pluginUrl,
      AIPANE_TEST_LOG: log,
      AIPANE_TEST_SCENARIO: JSON.stringify({ agents, initial, events, holdMs }),
      AIPANE_TEST_FAIL_ONCE: failOnce ? "1" : "0",
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

test("a failed final report retries without another native status event", async () => {
  expectStates(await reportsFor({ failOnce: true, holdMs: 2300 }), ["idle"]);
});

test("old plugin teardown cannot overwrite or remove a busy hot-loaded replacement", async () => {
  const directory = await mkdtemp(join(tmpdir(), "aipane-dsh-reload-test-"));
  try {
    const bin = join(directory, ".local/bin");
    await mkdir(bin, { recursive: true });
    await writeFile(join(bin, "aipane-activity"), `#!${process.execPath}
      const fs = require("node:fs");
      let input = "";
      process.stdin.on("data", chunk => input += chunk);
      process.stdin.on("end", () => {
        const reference = JSON.parse(input).dsh_runtime;
        const runtime = JSON.parse(fs.readFileSync(reference.path, "utf8"));
        if (runtime.instance !== reference.instance) return process.exit(1);
        fs.appendFileSync(process.env.AIPANE_TEST_LOG,
          JSON.stringify({ instance: reference.instance, state: runtime.state }) + "\\n");
      });
    `, { mode: 0o755 });
    const exerciseReload = `
      import assert from "node:assert/strict";
      import { readFile } from "node:fs/promises";
      import { join } from "node:path";
      const { apply } = await import(process.env.AIPANE_TEST_PLUGIN);
      const path = join(process.env.DSH_HOME, "aipane/activity", process.pid + ".json");
      const readRuntime = async () => JSON.parse(await readFile(path, "utf8"));
      const readReports = async () => (await readFile(process.env.AIPANE_TEST_LOG, "utf8").catch(() => ""))
        .trim().split("\\n").filter(Boolean).map(JSON.parse);
      const waitForReport = async instance => {
        for (let attempt = 0; attempt < 100; attempt++) {
          if ((await readReports()).some(report => report.instance === instance)) return;
          await new Promise(resolve => setTimeout(resolve, 20));
        }
        assert.fail("helper never acknowledged this plugin instance");
      };
      const setup = () => {
        const agent = { id: "root", status: "running" };
        let dispose;
        apply({
          agents: { list: () => [agent], roots: () => [agent] },
          on() {}, effect(fn) { dispose = fn(); },
        });
        return () => dispose();
      };
      const oldDispose = setup();
      const oldInstance = (await readRuntime()).instance;
      await waitForReport(oldInstance);
      const newDispose = setup();
      const newInstance = (await readRuntime()).instance;
      assert.notEqual(oldInstance, newInstance);
      await waitForReport(newInstance);
      await oldDispose();
      assert.equal((await readRuntime()).instance, newInstance);
      assert.equal((await readRuntime()).state, "busy");
      assert.deepEqual(await readReports(), [
        { instance: oldInstance, state: "busy" },
        { instance: newInstance, state: "busy" },
      ]);
      await newDispose();
    `;
    const { stdout, stderr } = await execFileAsync(process.execPath,
      ["--input-type=module", "--eval", exerciseReload], {
        env: { ...process.env, HOME: directory, DSH_HOME: join(directory, ".dsh"),
          TMUX: "/tmp/dsh-reload-fixture,42,0", TMUX_PANE: "%42",
          AIPANE_TEST_LOG: join(directory, "reports.jsonl"), AIPANE_TEST_PLUGIN: pluginUrl },
        timeout: 10_000,
      });
    assert.equal(stdout, "");
    assert.equal(stderr, "");
  } finally {
    await rm(directory, { recursive: true, force: true });
  }
});
