/**
 * Pause/resume across turns — persist the session ID, pause at the end of
 * one host process, and resume from a GENUINELY
 * SEPARATE OS process (not just a second in-process `Client`, unlike the
 * reconnect step in examples/quickstart, which documents why it stayed
 * in-process for that simpler scenario). This script re-execs itself via
 * `node`/`tsx` as a child process to play the part of "the next turn,
 * handled later by a different process".
 *
 * ## What each turn does
 *
 * - **Turn 1** (this process): creates a session, writes a nonce,
 *   explicitly pauses it (`session.pause()`) rather than waiting for idle
 *   timeout, and hands the session ID to a freshly spawned child process
 *   via argv.
 * - **Turn 2** (the child process, `--turn2 <sessionId> <nonce>`): a
 *   separate OS process, no shared globals, no shared `Client`. It
 *   attaches by ID, calls `session.resume()` EXPLICITLY once (the
 *   documented call, even though docs-site/content/docs/session-lifecycle.mdx
 *   is clear that any op wakes a paused session on its own), reads the
 *   nonce back, appends a second value, and exits.
 * - **Turn 3** (back in this parent process): confirms the child's write
 *   is visible, then demonstrates "cover expiry and cleanup when the next
 *   turn never arrives" using a SECOND, throwaway session with a short
 *   `pausedTtlMs`. A real TTL wait is too slow for a quick example, so
 *   this destroys that session itself and says so, rather than pretending
 *   to have observed a real expiry.
 *
 * Cleanup: the turn-1/2/3 session is destroyed by the parent process at
 * the end, success or failure. The short-TTL throwaway session in turn 3
 * is destroyed immediately after demonstrating the knob.
 *
 * Run:
 *   export INIS_API_KEY=...
 *   npm install @inis-run/sdk
 *   npx tsx pause-resume-across-turns.ts
 */

import { spawnSync } from "node:child_process";
import { randomUUID } from "node:crypto";
import { fileURLToPath } from "node:url";
import { Client, InisError } from "@inis-run/sdk";

const NONCE_PATH = "/workspace/pause-resume-nonce.txt";
const THIS_FILE = fileURLToPath(import.meta.url);

export async function runTurn1(client: Client): Promise<{ sessionId: string; nonce: string }> {
  const session = await client.sessions.create({
    idleTimeoutMs: 120_000,
    maxLifetimeMs: 10 * 60_000,
    egress: { mode: "deny" },
  });
  const nonce = randomUUID();
  await session.writeFile(NONCE_PATH, nonce);
  console.log(`turn 1 (parent process, pid=${process.pid}): created ${session.sessionId}, wrote nonce ${nonce}`);
  const info = await session.pause();
  console.log(`  paused explicitly at end of turn 1 (state=${info.state})`);
  return { sessionId: session.sessionId, nonce };
}

interface Turn2Result {
  nonce: string;
  secondValue: string;
}

/** Spawn an actual separate OS process — via `npx tsx`, the same way a
 * user runs this file themselves — with no shared interpreter state with
 * the parent whatsoever, and have IT resume and extend the session. */
function runTurn2InChildProcess(sessionId: string, nonce: string): Turn2Result {
  console.log(`turn 2: spawning a separate OS process to resume ${sessionId}`);
  const result = spawnSync("npx", ["tsx", THIS_FILE, "--turn2", sessionId, nonce], {
    encoding: "utf8",
    env: process.env,
  });
  if (result.stderr) process.stderr.write(result.stderr);
  if (result.status !== 0) {
    throw new Error(`turn 2 child process exited ${result.status}`);
  }
  const lastLine = result.stdout.trim().split("\n").pop() ?? "";
  return JSON.parse(lastLine) as Turn2Result;
}

/** Entry point when this file is re-invoked as the turn-2 child process
 * (see --turn2 handling in main() below). Prints exactly one JSON line to
 * stdout as its result contract with the parent; everything else goes to
 * stderr. */
export async function turn2ChildMain(sessionId: string, expectedNonce: string): Promise<number> {
  console.error(`turn 2 (child process, pid=${process.pid}): attaching to ${sessionId}`);
  const client = new Client();
  try {
    const session = client.sessions.attach(sessionId);
    // Explicit resume — documented, even though wake-on-op would do this
    // for us on the very next call below regardless.
    const info = await session.resume();
    console.error(`  resumed explicitly (state=${info.state})`);
    const gotNonceRaw = await session.readFile(NONCE_PATH);
    const gotNonce = typeof gotNonceRaw === "string" ? gotNonceRaw : new TextDecoder().decode(gotNonceRaw);
    if (gotNonce !== expectedNonce) {
      throw new Error(`nonce mismatch: expected ${expectedNonce}, got ${gotNonce}`);
    }
    const secondValue = randomUUID();
    await session.writeFile(NONCE_PATH, `${gotNonce}:${secondValue}`);
    console.error(`  read back ${gotNonce}, appended ${secondValue}`);
    console.log(JSON.stringify({ nonce: gotNonce, secondValue }));
    return 0;
  } catch (err) {
    if (err instanceof InisError) {
      console.error(`turn 2 API error: ${err.message} (code=${err.code})`);
    } else {
      console.error(`turn 2 failed: ${err instanceof Error ? err.message : String(err)}`);
    }
    return 1;
  }
}

/** The other half of "pause/resume across turns": what happens if the
 * next turn NEVER arrives. `pausedTtlMs` is the auto-destroy backstop —
 * set here short to show the knob, but a real TTL wait is too slow for a
 * quick example, so this session is destroyed explicitly right after, not
 * left to actually expire. */
export async function demonstratePausedTtlCleanup(client: Client): Promise<void> {
  const session = await client.sessions.create({
    idleTimeoutMs: 60_000,
    maxLifetimeMs: 5 * 60_000,
    pausedTtlMs: 60_000, // would auto-destroy 60s after pausing
    egress: { mode: "deny" },
  });
  const sessionId = session.sessionId;
  console.log(`turn 3: created ${sessionId} with pausedTtlMs=60000 (auto-destroy backstop if no next turn)`);
  await session.pause();
  console.log("  paused it — in production this is where you'd stop if the conversation ended here.");
  console.log("  NOT waiting out the real 60s TTL in this example; destroying it explicitly instead:");
  await session.destroy();
  console.log(`  session ${sessionId} destroyed (this example's own cleanup, standing in for the TTL firing)`);
}

export async function main(): Promise<number> {
  if (!process.env.INIS_API_KEY) {
    console.error(
      "INIS_API_KEY is not set. Export it before running this example — " +
        "never put it in code, a file, or a session/guest command.",
    );
    return 1;
  }

  const client = new Client();
  let ownedSessionId: string | undefined;
  try {
    const { sessionId, nonce } = await runTurn1(client);
    ownedSessionId = sessionId;

    const childResult = runTurn2InChildProcess(sessionId, nonce);
    console.log(`turn 2 result (from the child process's stdout): ${JSON.stringify(childResult)}`);

    console.log(`turn 3 (parent process, pid=${process.pid}): reattaching to ${sessionId}`);
    const session = client.sessions.attach(sessionId);
    const finalRaw = await session.readFile(NONCE_PATH);
    const finalValue = typeof finalRaw === "string" ? finalRaw : new TextDecoder().decode(finalRaw);
    const expected = `${childResult.nonce}:${childResult.secondValue}`;
    if (finalValue !== expected) {
      throw new Error(`expected ${expected}, got ${finalValue}`);
    }
    console.log(`  confirmed: ${finalValue} — turn 2's write survived a real process boundary and a pause`);

    await demonstratePausedTtlCleanup(client);
    return 0;
  } finally {
    if (ownedSessionId !== undefined) {
      await client.sessions.attach(ownedSessionId).destroy();
      console.log(`session ${ownedSessionId} destroyed (owner cleanup)`);
    }
  }
}

if (process.argv[1] !== undefined && fileURLToPath(import.meta.url) === process.argv[1]) {
  const turn2Idx = process.argv.indexOf("--turn2");
  if (turn2Idx !== -1) {
    const sessionId = process.argv[turn2Idx + 1];
    const nonce = process.argv[turn2Idx + 2];
    if (!sessionId || !nonce) {
      console.error("--turn2 requires <sessionId> <nonce>");
      process.exit(1);
    }
    turn2ChildMain(sessionId, nonce).then((code) => process.exit(code));
  } else {
    main()
      .then((code) => process.exit(code))
      .catch((err) => {
        if (err instanceof InisError) {
          console.error(`inis.run API error: ${err.message} (code=${err.code})`);
        } else {
          console.error(`scenario failed: ${err instanceof Error ? err.message : String(err)}`);
        }
        process.exit(1);
      });
  }
}
