/**
 * inis.run canonical quickstart (TypeScript) — the source of truth for
 * every first-run snippet (console, marketing, docs, adapter READMEs). If
 * you're fixing a quickstart snippet somewhere else in this repo, fix it
 * here first, then point the other surface at this file or match it — do
 * not hand-edit a divergent copy. See ../README.md for the full contract
 * and scripts/check-quickstart-surfaces.py for the CI drift check that
 * enforces this.
 *
 * Endpoint:  https://api.inis.run (INIS_BASE_URL env var to override)
 * Package:   @inis-run/sdk — npm install @inis-run/sdk (see sdk/typescript/package.json for
 *            the supported Node range and dependency pins)
 * Timeout:   120_000ms per HTTP call by default (new Client({ timeoutMs })
 *            to change) — separate from this session's own
 *            maxLifetimeMs/idleTimeoutMs below, which bound the *session*,
 *            not one HTTP request.
 * Egress:    deny-by-default — egress: { mode: "deny" } means the session
 *            cannot reach the network at all unless explicitly allowed
 *            (see EgressPolicy in sdk/typescript/src/client.ts).
 * TTL:       maxLifetimeMs hard-caps how long the session may stay live;
 *            idleTimeoutMs auto-pauses it after inactivity. Both are short
 *            here because this is a disposable demo — not production
 *            guidance.
 * Cleanup:   the session this script OWNS is destroyed in a finally block
 *            on BOTH the success and the failure path. The reconnect step
 *            only ATTACHES to that same session by ID; attaching never
 *            destroys it — only the owner's finally does.
 *
 * Run:
 *   export INIS_API_KEY=...    # never hardcode this, never put it in a
 *                               # guest command/file/fixture
 *   npm install @inis-run/sdk
 *   npx tsx quickstart.ts       # or: tsc && node quickstart.js
 */

import { randomUUID } from "node:crypto";
import { Client, InisError, type Session } from "@inis-run/sdk";

const NONCE_PATH = "/workspace/quickstart-nonce.txt";

/** Run a command that reads the nonce file back, proving the session's
 * filesystem persisted since it was written — the core thing this
 * quickstart demonstrates, twice (once same-handle, once reattached). */
async function readBackNonce(session: Session, nonce: string, label: string): Promise<void> {
  const result = await session.exec(["cat", NONCE_PATH]);
  if (result.exitCode !== 0 || !result.stdout.includes(nonce)) {
    throw new Error(
      `${label}: nonce round-trip failed ` +
        `(exitCode=${result.exitCode} stdout=${JSON.stringify(result.stdout)} stderr=${JSON.stringify(result.stderr)})`,
    );
  }
  console.log(`${label}: read back ${JSON.stringify(result.stdout.trim())} — matches the nonce we wrote`);
}

async function main(): Promise<number> {
  if (!process.env.INIS_API_KEY) {
    console.error(
      "INIS_API_KEY is not set. Export it before running this example — " +
        "never put it in code, a file, or a session/guest command.",
    );
    return 1;
  }

  const client = new Client(); // resolves INIS_API_KEY / INIS_BASE_URL from the host env
  const nonce = randomUUID();

  // 1. Create a short-lived session with deny-by-default egress. This
  //    process OWNS this session: it is responsible for destroying it in
  //    the finally block below, on both the success and the failure path.
  const session = await client.sessions.create({
    idleTimeoutMs: 60_000,
    maxLifetimeMs: 5 * 60_000,
    egress: { mode: "deny" },
  });
  const sessionId = session.sessionId;
  console.log(`created session ${sessionId} (egress: deny-by-default)`);

  try {
    // 2. Write a small nonce to the session's filesystem.
    await session.writeFile(NONCE_PATH, nonce);

    // 3. Run a SECOND command that reads it back — proves state persisted
    //    across exec() calls, not just within one call.
    await readBackNonce(session, nonce, "same-handle read-back");

    // 4. Persist the session ID somewhere the next process/request can
    //    read it from (host-side only — a DB row, a cache key; logging it
    //    here stands in for that).
    console.log(`persisting session id for later reconnect: ${sessionId}`);

    // 5. Simulate a later process reconnecting: attach to the SAME session
    //    purely by ID (not the original in-process handle), and prove it
    //    is still the same filesystem. This is the "owned-session" flow
    //    attaching to itself; a genuinely separate process would call
    //    `client.sessions.attach(sessionId)` exactly the same way.
    const reattached = client.sessions.attach(sessionId);
    await readBackNonce(reattached, nonce, "post-reconnect read-back");
    // attach() never destroys the session it binds to — only the owner's
    // finally block below does, once this scope exits.

    return 0;
  } finally {
    // 6. Destroy the OWNED session on both the success and the failure
    //    path. An attached session (step 5) is never destroyed by the
    //    attacher — only by whichever caller owns it, here.
    await session.destroy();
    console.log(`session ${sessionId} destroyed (owner cleanup ran in finally)`);
  }
}

main()
  .then((code) => process.exit(code))
  .catch((err) => {
    if (err instanceof InisError) {
      console.error(`inis.run API error: ${err.message}`);
    } else {
      console.error(`quickstart scenario failed: ${err instanceof Error ? err.message : String(err)}`);
    }
    process.exit(1);
  });
