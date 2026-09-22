import assert from "node:assert/strict"
import { execFile } from "node:child_process"
import { mkdtemp, mkdir, writeFile, rm } from "node:fs/promises"
import { tmpdir } from "node:os"
import { join } from "node:path"
import { promisify } from "node:util"
import test from "node:test"

test("selected Channel follows switches, ignores background roots, and survives plugin handoff", async () => {
  const home = await mkdtemp(join(tmpdir(), "aipane-dsh-session-"))
  try {
    const registry = join(home, ".dsh/profiles/dsh-tui/node_modules/@deepseek-harness-tui/dsh-tui/lib/types/adapter/channel")
    await mkdir(registry, { recursive: true })
    await writeFile(join(registry, "package.json"), '{"type":"module"}')
    await writeFile(join(registry, "host-registry.js"), `
      export const getRegisteredTuiChannel = ctx => ctx.channel;
      export const onTuiChannelRegistered = (ctx, listener) => () => {};
    `)
    const script = `
      import assert from 'node:assert/strict';
      import {readFileSync,existsSync} from 'node:fs';
      import {apply} from ${JSON.stringify(new URL("../integrations/dsh/aipane-session.mjs", import.meta.url).href)};
      const listeners=new Set(), disposers=[];
      const roots=['old','new','background'].map(id=>({session:{id,header:{cwd:process.env.HOME}}}));
      const channel={agentId:'old',subscribe:fn=>{listeners.add(fn);return()=>listeners.delete(fn)}};
      const ctx={channel,agents:{get:id=>roots.find(a=>a.session.id===id),roots:()=>roots},
        get:()=>undefined,effect:setup=>disposers.push(setup())};
      const path=process.env.DSH_HOME+'/aipane/sessions/'+process.pid+'.json';
      const record=()=>JSON.parse(readFileSync(path,'utf8'));
      const emit=()=>{for(const fn of listeners)fn()};
      await apply(ctx);
      assert.equal(record().session_id,'old');
      assert.equal(record().socket,'/tmp/socket,with,commas');
      emit(); assert.equal(record().session_id,'old');
      channel.agentId='new';emit();assert.equal(record().session_id,'new');
      channel.agentId='old';emit();assert.equal(record().session_id,'old');
      const first=record().instance;
      await apply(ctx); const second=record().instance;
      assert.notEqual(first,second);
      emit();assert.equal(record().instance,second);
      await disposers[0]();assert.equal(record().instance,second);
      roots[0].session.header.cwd='';emit();assert.equal(existsSync(path),false);
      roots[0].session.header.cwd=process.env.HOME;emit();assert.equal(record().instance,second);
      await disposers[1]();assert.equal(existsSync(path),false);
    `
    // Missing aipane-bind exercises asynchronous spawn failure without taking
    // down the host or suppressing its authoritative selected-session record.
    await promisify(execFile)(process.execPath, ["--input-type=module", "-e", script], {
      env: { ...process.env, HOME: home, DSH_HOME: join(home, ".dsh"),
        TMUX: "/tmp/socket,with,commas,123,0", TMUX_PANE: "%9" },
      timeout: 10000,
    })
  } finally { await rm(home, { recursive: true, force: true }) }
})
