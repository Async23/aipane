/** Patch Claude's Stop payload construction, before any notification hook runs. */
export const PATCH_MARKER = 'aipane-stop-response-v1';

function lastAssistantWithText(messages, textOf) {
  if (!Array.isArray(messages)) return;
  for (let index = messages.length - 1; index >= 0; index--) {
    const message = messages[index];
    // Tool results are also user messages: never reuse pre-tool commentary as
    // a final answer when the final model response contains no text.
    if (message.type === 'user' ||
        (message.type === 'system' && message.subtype === 'compact_boundary')) return;
    if (message.type !== 'assistant') continue;
    const content = message.message?.content;
    if (Array.isArray(content) && textOf(content, '\n').trim()) return message;
  }
}

export function patchStopResponse(source) {
  // Match the payload-local selection, never the shared getLastAssistantMessage
  // helper used by unrelated UI, API error, and conversation paths.
  const pattern = /let (?<last>[$\w]+)=(?<messages>[$\w]+)\?(?<select>[$\w]+)\(\k<messages>\):void 0,(?<text>[$\w]+)=\k<last>\?(?<extract>[$\w]+)\(\k<last>\.message\.content,\x60\n\x60\)\.trim\(\)\|\|void 0:void 0/g;
  const sites = [...source.matchAll(pattern)].filter(match => {
    const following = source.slice(match.index + match[0].length, match.index + match[0].length + 1500);
    return following.includes('hook_event_name:"Stop",stop_hook_active:') &&
      following.includes('hook_event_name:"SubagentStop",stop_hook_active:') &&
      following.includes(`last_assistant_message:${match.groups.text}`);
  });
  const markers = source.split(`/*${PATCH_MARKER}*/`).length - 1;
  if (markers === 1 && sites.length === 0) return source;
  if (sites.length !== 1 || markers !== 0) {
    throw new Error(`Expected one unpatched Stop payload selection; found ${sites.length} sites and ${markers} markers`);
  }
  const site = sites[0];
  const { last, messages, select, extract } = site.groups;
  const oldSelection = `${messages}?${select}(${messages}):void 0`;
  const newSelection = `(/*${PATCH_MARKER}*/${lastAssistantWithText.toString()})(${messages},${extract})`;
  const replacement = site[0].replace(`let ${last}=${oldSelection}`, `let ${last}=${newSelection}`);
  return source.slice(0, site.index) + replacement + source.slice(site.index + site[0].length);
}
