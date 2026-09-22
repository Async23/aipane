// Read-only projection of dsh's native profile composition. Never call
// loadProfile / prepareProfile: those initialize or rewrite profile files.
import { realpathSync } from "node:fs"
import { createRequire } from "node:module"
import { dirname, join } from "node:path"
import { fileURLToPath, pathToFileURL } from "node:url"

try {
  const [executable, profileDir, home, template] = process.argv.slice(2)
  const require = createRequire(realpathSync(executable))
  const boot = await import(pathToFileURL(require.resolve("@deepseek-ai/dsh-app-boot")))
  const installAnchor = require.resolve("@deepseek-ai/dsh/package.json")
  const profile = boot.loadProfileDirectory("aipane-doctor", profileDir, installAnchor)
  const layers = [
    ...profile.layers.map(layer => layer.patches),
    profile.patches,
    boot.loadOptionalPatches("aipane-doctor", join(home, "cordis.patch.yml")) ?? [],
  ]
  const warnings = []
  const entries = boot.composeEntries(layers, message => warnings.push(message))
  const expected = boot.composeEntries([
    boot.loadOptionalPatches("aipane-doctor", template) ?? [],
  ])
  const project = (rows, parentDisabled = false) => rows.flatMap(row => {
    const disabled = parentDisabled || row.disabled || false
    const own = typeof row.id === "string" && row.id.startsWith("aipane-")
      || typeof row.name === "string" && row.name.includes("/aipane-")
      ? [{ id: row.id, name: typeof row.name === "string" && row.name.startsWith("file:")
          ? fileURLToPath(row.name) : row.name, disabled }] : []
    return row.group && Array.isArray(row.config)
      ? [...own, ...project(row.config, disabled)] : own
  })
  process.stdout.write(JSON.stringify({
    entries: project(entries), expected: project(expected), warnings,
    base: dirname(profile.patchPath),
  }))
} catch (error) {
  // Config parsing errors can contain secret source snippets. Emit only the
  // error class; the doctor names the profile and the readonly diagnostic.
  process.stderr.write(`dsh profile projection failed (${error?.name ?? "Error"})\n`)
  process.exitCode = 1
}
