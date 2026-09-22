import assert from "node:assert/strict";
import test from "node:test";

import {
  USAGE, apply, copyToClipboard, handleCopy, oscSequence, parseCount, replyTexts,
} from "../integrations/dsh/aipane-copy.mjs";

const sessionWith = events => ({ snapshotEvents: () => events });
const assistant = text => ({ type: "assistant/message", data: { message: { content: [{ type: "text", text }] } } });

// The plugin never writes to the workstation clipboard from the test process:
// native and tmux paths are disabled through the environment, and the OSC 52
// sink is always injected.
function withoutClipboardEnvironment(run) {
  const saved = { TMUX: process.env.TMUX, LC_TERMINAL: process.env.LC_TERMINAL, SSH_CONNECTION: process.env.SSH_CONNECTION };
  delete process.env.TMUX;
  delete process.env.LC_TERMINAL;
  process.env.SSH_CONNECTION = "aipane-copy-test";
  const restore = () => {
    for (const [key, value] of Object.entries(saved)) {
      if (value === undefined) delete process.env[key];
      else process.env[key] = value;
    }
  };
  try {
    const result = run();
    // An async body must keep the environment until it settles.
    return result instanceof Promise ? result.finally(restore) : (restore(), result);
  } catch (error) {
    restore();
    throw error;
  }
}

test("parseCount defaults to the newest reply and rejects anything else", () => {
  assert.equal(parseCount(""), 1);
  assert.equal(parseCount("   "), 1);
  assert.equal(parseCount(undefined), 1);
  assert.equal(parseCount("3"), 3);
  assert.equal(parseCount(" 12 "), 12);
  for (const invalid of ["0", "-1", "1.5", "abc", "1 2", "+2", "1e2"]) {
    assert.equal(parseCount(invalid), undefined, invalid);
  }
});

test("replyTexts keeps visible assistant text in transcript order only", () => {
  const events = [
    { type: "user/message", data: { content: [{ type: "text", text: "hi" }] } },
    { type: "assistant/message", data: { message: { content: [{ type: "reasoning", text: "thinking" }] } } },
    { type: "assistant/message", data: { message: { content: [{ type: "text", text: "first" }, { type: "tool-call" }] } } },
    { type: "tool/result", data: { message: { content: [{ type: "text", text: "tool output" }] } } },
    { type: "assistant/message", data: { message: { content: [{ type: "text", text: "second" }, { type: "text", text: "more" }] } } },
  ];
  assert.deepEqual(replyTexts(sessionWith(events)), ["first", "second\n\nmore"]);
});

test("replyTexts reads the older events array and tolerates a missing session", () => {
  assert.deepEqual(replyTexts({ events: [assistant("legacy")] }), ["legacy"]);
  assert.deepEqual(replyTexts({ events: [{ type: "assistant/message", data: { content: [{ type: "text", text: "flat" }] } }] }), ["flat"]);
  assert.deepEqual(replyTexts(undefined), []);
  assert.deepEqual(replyTexts({}), []);
  assert.deepEqual(replyTexts({ snapshotEvents: () => "not an array" }), []);
});

test("oscSequence emits base64 OSC 52 and doubles ESC inside tmux", () => {
  withoutClipboardEnvironment(() => {
    const payload = Buffer.from("héllo", "utf8").toString("base64");
    assert.equal(oscSequence("héllo"), `\x1b]52;c;${payload}\x07`);
    process.env.TMUX = "/tmp/socket,1,0";
    const wrapped = oscSequence("héllo");
    // tmux forwards a DCS passthrough payload verbatim, so every inner ESC —
    // including the one opening OSC 52 — must be doubled.
    assert.equal(wrapped, `\x1bPtmux;\x1b\x1b]52;c;${payload}\x07\x1b\\`);
  });
});

test("handleCopy copies the requested reply and reports the settled path", async () => {
  const events = [1, 2, 3].map(n => assistant(`reply ${n}`));
  const copied = [];
  const copy = async text => {
    copied.push(text);
    return { ok: true, confirmed: ["pbcopy"], osc: true };
  };
  const invocation = { agent: { session: sessionWith(events) }, rawInput: "" };
  assert.deepEqual(await handleCopy(invocation, copy), {
    kind: "success", text: "Copied the newest reply — 7 chars via pbcopy.",
  });
  assert.equal(copied.at(-1), "reply 3");
  const older = await handleCopy({ ...invocation, rawInput: "3" }, copy);
  assert.equal(copied.at(-1), "reply 1");
  assert.equal(older.kind, "success");
  assert.match(older.text, /reply #3 from the end/);
});

test("handleCopy reports OSC 52-only delivery and every rejection", async () => {
  const invocation = { agent: { session: sessionWith([assistant("only")]) }, rawInput: "" };
  const oscOnly = await handleCopy(invocation, async () => ({ ok: true, confirmed: [], osc: true }));
  assert.match(oscOnly.text, /OSC 52 only/);
  assert.deepEqual(
    await handleCopy({ agent: { session: sessionWith([]) }, rawInput: "" }, async () => ({ ok: true, confirmed: [], osc: true })),
    { kind: "error", text: "No assistant reply in this session yet." },
  );
  assert.equal(
    (await handleCopy({ ...invocation, rawInput: "2" }, async () => ({ ok: true, confirmed: [], osc: true }))).text,
    "This session has 1 assistant reply; /copy 2 is out of range.",
  );
  assert.deepEqual(await handleCopy({ ...invocation, rawInput: "zero" }, async () => ({ ok: true, confirmed: [], osc: true })),
    { kind: "error", text: USAGE });
  assert.match(
    (await handleCopy(invocation, async () => ({ ok: false, confirmed: [], osc: false }))).text,
    /Clipboard unavailable/,
  );
});

test("apply registers /copy and stays out of the Web host", () => {
  const registrations = [];
  apply({ commands: { register: definition => registrations.push(definition) } });
  assert.equal(registrations.length, 1);
  assert.equal(registrations[0].name, "copy");
  assert.equal(typeof registrations[0].handler, "function");
  assert.deepEqual(registrations[0].input, { hint: "[n]" });
  assert.ok(registrations[0].description.includes("clipboard"));
  apply({ commands: { register: () => { throw new Error("the Web host must not register /copy"); } } }, { mode: "web" });
});

test("copyToClipboard still reports the OSC 52 path without a native tool", async () => {
  await withoutClipboardEnvironment(async () => {
    const written = [];
    const result = await copyToClipboard("payload", chunk => written.push(chunk));
    assert.deepEqual(result.confirmed, []);
    assert.equal(result.osc, true);
    assert.equal(result.ok, true);
    assert.equal(written.length, 1);
    assert.ok(written[0].includes(Buffer.from("payload", "utf8").toString("base64")));
  });
});
