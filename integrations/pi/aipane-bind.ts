/**
 * Keep aipane's pane-to-session binding aligned with Pi's active session.
 *
 * Pi can replace a session in-process via /new, /resume, /fork, or an extension.
 * The launch argv does not change, so launch-time session ids become stale. Pi
 * emits session_start after every replacement; record the current id there.
 * Outside tmux, or when aipane-bind is unavailable, this Adapter is a silent
 * no-op and never blocks Pi.
 */
import type {
  ExtensionAPI,
  ExtensionContext,
} from "@earendil-works/pi-coding-agent";
import { homedir } from "node:os";
import { join } from "node:path";

export type Environment = Readonly<Record<string, string | undefined>>;

const BIND_TIMEOUT_MS = 5_000;

export function defaultBindBinary(): string {
  return join(homedir(), ".local", "bin", "aipane-bind");
}

export function bindBinary(environment: Environment): string {
  return environment.PI_AIPANE_BIND_BIN?.trim() || defaultBindBinary();
}

export async function reportSessionBinding(
  pi: ExtensionAPI,
  ctx: ExtensionContext,
  environment: Environment = process.env,
): Promise<boolean> {
  const paneId = environment.TMUX_PANE?.trim();
  if (!paneId) return false;

  try {
    const sessionId = ctx.sessionManager.getSessionId()?.trim();
    if (!sessionId) return false;
    const result = await pi.exec(
      bindBinary(environment),
      [
        "--tool",
        "p",
        "--sid",
        sessionId,
        "--pane",
        paneId,
        "--cmd",
        `pi --session-id ${sessionId}`,
      ],
      { timeout: BIND_TIMEOUT_MS },
    );
    return result.code === 0;
  } catch {
    return false;
  }
}

export function registerPiSessionBinding(
  pi: ExtensionAPI,
  environment: Environment = process.env,
): void {
  pi.on("session_start", async (_event, ctx) => {
    await reportSessionBinding(pi, ctx, environment);
  });
}

export default function piSessionBindingExtension(pi: ExtensionAPI): void {
  registerPiSessionBinding(pi);
}
