import assert from 'node:assert/strict';
import { test } from 'node:test';
import { patchStopResponse } from '../integrations/claude/stop-response-patch.mjs';

// The payload construction shape in Claude Code 2.1.322, with inert dependencies.
const fixture = `
function Jh(e){return e.findLast((n)=>n.type==="assistant")}
function Ts(e,n=""){return e.filter((r)=>r.type==="text").map((r)=>r.text).join(n)}
function build(w,h){
  let ne=w?Jh(w):void 0,Te=ne?Ts(ne.message.content,\`\n\`).trim()||void 0:void 0,Ee={};
  return h?{hook_event_name:"SubagentStop",stop_hook_active:false,last_assistant_message:Te,...Ee}:{hook_event_name:"Stop",stop_hook_active:false,last_assistant_message:Te,...Ee};
}
`;

const text = value => ({ type: 'assistant', message: { content: [{ type: 'text', text: value }] } });
const thinking = { type: 'assistant', message: { content: [{ type: 'thinking', thinking: 'private' }] } };
const user = { type: 'user', message: { content: 'current request' } };
const compile = source => new Function(`${source}; return build;`)();

test('original payload reproduces text followed by thinking', () => {
  assert.equal(compile(fixture)([user, text('final answer'), thinking]).last_assistant_message, undefined);
});

for (const [label, messages, expected] of [
  ['normal final response', [user, text('final answer')], 'final answer'],
  ['trailing thinking', [user, text('final answer'), thinking], 'final answer'],
  ['multiple empty trailing messages', [user, text('final answer'), text(' \n\t'), thinking], 'final answer'],
  ['newest nonempty response', [user, text('earlier'), text('final answer'), thinking], 'final answer'],
  ['multiline and mixed blocks', [user, { type: 'assistant', message: { content: [
    { type: 'thinking', thinking: 'private' }, { type: 'text', text: 'first' }, { type: 'text', text: 'second' },
  ] } }, thinking], 'first\nsecond'],
  ['system metadata after the response', [user, text('final answer'), { type: 'system', subtype: 'turn_duration' }, thinking], 'final answer'],
  ['no text in the current turn', [user, thinking], undefined],
  ['do not reuse the previous turn', [user, text('old answer'), user, thinking], undefined],
  ['do not reuse text from before a tool result', [user, text('running tool'), {
    type: 'user', message: { content: [{ type: 'tool_result', content: 'done' }] },
  }, thinking], undefined],
  ['do not cross compaction', [user, text('old answer'), { type: 'system', subtype: 'compact_boundary' }, thinking], undefined],
  ['empty history', [], undefined],
  ['missing history', undefined, undefined],
]) {
  test(label, () => {
    const build = compile(patchStopResponse(fixture));
    for (const subagent of [false, true]) {
      const payload = build(messages, subagent);
      assert.equal(payload.hook_event_name, subagent ? 'SubagentStop' : 'Stop');
      assert.equal(payload.last_assistant_message, expected);
    }
  });
}

test('does not change the shared get-last-assistant helper', () => {
  const patched = patchStopResponse(fixture);
  assert.ok(patched.includes('function Jh(e){return e.findLast((n)=>n.type==="assistant")}'));
});

test('is idempotent', () => {
  const once = patchStopResponse(fixture);
  assert.equal(patchStopResponse(once), once);
});

test('rejects an unknown or ambiguous upstream layout', () => {
  assert.throws(() => patchStopResponse('function changed() {}'), /expected one/i);
  assert.throws(() => patchStopResponse(fixture + fixture), /expected one/i);
});

test('accepts renamed minified bindings', () => {
  const renamed = fixture.replaceAll('Jh', '$last').replaceAll('Ts', '$text').replaceAll('ne', '$message').replaceAll('Te', '$answer');
  assert.equal(compile(patchStopResponse(renamed))([user, text('answer'), thinking]).last_assistant_message, 'answer');
});
