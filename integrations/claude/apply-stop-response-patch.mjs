import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { spawnSync } from 'node:child_process';
import { parseArgs } from 'node:util';
import { pathToFileURL } from 'node:url';
import { PATCH_MARKER, patchStopResponse } from './stop-response-patch.mjs';

const { values } = parseArgs({ options: {
  binary: { type: 'string', default: path.join(os.homedir(), '.local/bin/claude') },
  check: { type: 'boolean' },
  apply: { type: 'boolean' },
  output: { type: 'string' },
  restore: { type: 'string' },
} });
if ([values.check, values.apply, values.output, values.restore].filter(Boolean).length !== 1) {
  throw new Error('Choose --check, --output PATH, --apply, or --restore MANIFEST; optional --binary PATH');
}

const digest = file => createHash('sha256').update(fs.readFileSync(file)).digest('hex');
function command(file, args) {
  const result = spawnSync(file, args, { encoding: 'utf8', timeout: 30000, cwd: os.tmpdir() });
  if (result.error || result.status !== 0) {
    throw new Error(`${file} failed: ${result.error?.message || result.stderr}`);
  }
  return result.stdout.trim();
}

function replaceAtomically(source, target, expectedDigest) {
  const directory = fs.mkdtempSync(path.join(path.dirname(target), '.claude-stop-'));
  try {
    const staged = path.join(directory, 'claude');
    fs.copyFileSync(source, staged, fs.constants.COPYFILE_EXCL);
    fs.chmodSync(staged, fs.statSync(target).mode & 0o777);
    if (digest(target) !== expectedDigest) throw new Error('Installed Claude changed; refusing to overwrite it');
    fs.renameSync(staged, target);
  } finally {
    fs.rmSync(directory, { recursive: true, force: true });
  }
}

async function main() {
  if (values.restore) {
    const manifest = JSON.parse(fs.readFileSync(values.restore, 'utf8'));
    assert.equal(digest(manifest.backup), manifest.originalSha256, 'Backup digest mismatch');
    command('/usr/bin/codesign', ['--verify', '--strict', manifest.backup]);
    replaceAtomically(manifest.backup, manifest.binary, manifest.patchedSha256);
    console.log(JSON.stringify({ status: 'restored', binary: manifest.binary }));
    return;
  }

  const binary = fs.realpathSync(values.binary);
  const tweakccRoot = path.resolve(process.env.TWEAKCC_ROOT || path.join(os.homedir(), 'code/oss/tweakcc'));
  const native = await import(pathToFileURL(path.join(tweakccRoot, 'src/nativeInstallation.ts')));
  const extract = native.extractClaudeJsModulesFromNativeInstallation;
  const original = extract(binary);
  if (!original) throw new Error('Cannot extract native Claude module graph');
  const candidates = original.modules.filter(module => module.isJavaScript && (
    module.contents.includes(Buffer.from('hook_event_name:"Stop",stop_hook_active:')) ||
    module.contents.includes(Buffer.from(`/*${PATCH_MARKER}*/`))
  ));
  assert.equal(candidates.length, 1, 'Expected exactly one Stop module');
  const target = candidates[0];
  const source = target.contents.toString('utf8');
  const patchedSource = patchStopResponse(source);
  const version = command(binary, ['--version']);
  if (patchedSource === source || values.check) {
    console.log(JSON.stringify({ status: patchedSource === source ? 'patched' : 'unpatched', version, binary, sha256: original.sourceSha256, module: target.name }));
    return;
  }

  let directory;
  let output;
  let backup;
  if (values.apply) {
    const backups = path.join(os.homedir(), 'Library/Application Support/aipane/claude-stop-patches.noindex');
    fs.mkdirSync(backups, { recursive: true, mode: 0o700 });
    directory = fs.mkdtempSync(path.join(backups, `${new Date().toISOString().replaceAll(':', '-')}-`));
    backup = path.join(directory, 'claude.original');
    fs.copyFileSync(binary, backup, fs.constants.COPYFILE_EXCL);
    assert.equal(digest(backup), original.sourceSha256, 'Claude changed during backup');
    output = path.join(directory, 'claude.patched');
  } else {
    output = path.resolve(values.output);
    if (fs.existsSync(output)) throw new Error('Output already exists');
    fs.mkdirSync(path.dirname(output), { recursive: true });
  }

  const contents = Buffer.from(patchedSource);
  native.repackNativeInstallationModules(binary, {
    sourceSha256: original.sourceSha256,
    modules: [{ index: target.index, name: target.name, contents }],
  }, output);
  fs.chmodSync(output, fs.statSync(binary).mode & 0o777);
  const after = extract(output);
  assert.ok(after, 'Cannot re-read patched module graph');
  assert.equal(after.modules.length, original.modules.length, 'Module count changed');
  for (const before of original.modules) {
    const current = after.modules[before.index];
    assert.equal(current.name, before.name, 'Module identity changed');
    assert.ok(current.contents.equals(before.index === target.index ? contents : before.contents), `Unexpected change in ${before.name}`);
  }
  command('/usr/bin/codesign', ['--verify', '--strict', output]);
  assert.equal(command(output, ['--version']), version, 'Patched binary cannot report the original version');
  const manifest = {
    patch: PATCH_MARKER, version, binary, output, backup,
    originalSha256: original.sourceSha256, patchedSha256: after.sourceSha256,
    module: target.name, moduleIndex: target.index, moduleCount: original.modules.length,
    tweakccRevision: command('git', ['-C', tweakccRoot, 'rev-parse', 'HEAD']),
  };
  const manifestPath = directory ? path.join(directory, 'manifest.json') : `${output}.json`;
  fs.writeFileSync(manifestPath, JSON.stringify(manifest, null, 2) + '\n', { flag: 'wx', mode: 0o600 });
  if (values.apply) replaceAtomically(output, binary, original.sourceSha256);
  console.log(JSON.stringify({ status: values.apply ? 'installed' : 'staged', version, binary, output, manifest: manifestPath, module: target.name, verifiedModules: original.modules.length }));
}

await main();
