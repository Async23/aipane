import assert from "node:assert/strict";
import test from "node:test";

const {
  default: registerExtension,
  registerPiSessionBinding,
  reportSessionBinding,
} = await import("../integrations/pi/aipane-bind.ts");

function createHarness({
  environment = {
    HOME: "/Users/tester",
    TMUX_PANE: "%42",
    PI_AIPANE_BIND_BIN: "/opt/test/aipane-bind",
  },
  execResult = { code: 0, stdout: "", stderr: "", killed: false },
  execError,
} = {}) {
  const calls = [];
  const handlers = new Map();
  const pi = {
    on(name, handler) {
      handlers.set(name, handler);
    },
    async exec(command, args, options) {
      calls.push({ command, args, options });
      if (execError) throw execError;
      return execResult;
    },
  };

  registerPiSessionBinding(pi, environment);
  return { calls, environment, handlers, pi };
}

function context(sessionId) {
  return {
    sessionManager: {
      getSessionId() {
        return sessionId;
      },
    },
  };
}

test("default export registers the Pi session_start Adapter", () => {
  const handlers = new Map();
  registerExtension({
    on(name, handler) {
      handlers.set(name, handler);
    },
  });

  assert.deepEqual([...handlers.keys()], ["session_start"]);
});

test("session_start always binds the currently active Pi session", async () => {
  const { calls, handlers } = createHarness();
  const onSessionStart = handlers.get("session_start");

  await onSessionStart({ type: "session_start", reason: "startup" }, context("session-start"));
  await onSessionStart({ type: "session_start", reason: "resume" }, context("session-resumed"));
  await onSessionStart({ type: "session_start", reason: "fork" }, context("session-forked"));

  assert.deepEqual(
    calls.map(({ args }) => args),
    ["session-start", "session-resumed", "session-forked"].map((sid) => [
      "--tool",
      "p",
      "--sid",
      sid,
      "--pane",
      "%42",
      "--cmd",
      `pi --session-id ${sid}`,
    ]),
  );
  assert.ok(calls.every(({ command }) => command === "/opt/test/aipane-bind"));
  assert.ok(calls.every(({ options }) => options.timeout === 5_000));
});

test("binding is fail-open outside tmux or when the helper fails", async () => {
  const outside = createHarness({ environment: {} });
  await outside.handlers.get("session_start")(
    { type: "session_start", reason: "startup" },
    context("session-outside"),
  );
  assert.equal(outside.calls.length, 0);

  const failed = createHarness({ execError: new Error("missing helper") });
  assert.equal(
    await reportSessionBinding(failed.pi, context("session-failed"), failed.environment),
    false,
  );
});
