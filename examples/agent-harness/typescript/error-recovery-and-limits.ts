/**
 * Recover from a paused, expired, failed, or capacity-limited session, and
 * cancel/time-bound work — a generic retry helper plus scenarios against
 * the real, documented error contract.
 *
 * ## The recovery contract
 *
 * Branch on `InisError.code`, never the message string (it can be
 * reworded — see docs-site/content/docs/api/errors.mdx). `.retryable` and
 * `.retryAfter` come straight from the server's own signal — this file's
 * `callWithRecovery` trusts that field instead of hand-maintaining a
 * second copy of which codes are "probably transient".
 *
 * ## What each scenario below actually does, and what it doesn't claim
 *
 * This file was NOT run against production as part of this change — see
 * the PR/issue for exactly what was executed (type checks and a local,
 * mock-backed test suite only). Every specific error `code` cited below
 * was confirmed by reading the request-handling source, not observed live:
 *
 * - **Gone (404 `not_found`)** — attaching to a session you already
 *   destroyed, or to a made-up ID. Deterministic and account-independent.
 *   `session_ended` (410) is a *different* outcome, reserved for a session
 *   that reached history through a lifecycle event (idle timeout,
 *   `maxLifetimeMs`, or a paused session past its `pausedTtlMs`) rather
 *   than one explicitly deleted — both are gone-for-good and handled the
 *   same way: don't retry the same ID, start fresh instead.
 * - **Over the request-level fork limit (400 `bad_request`)** —
 *   `fork(17)` exceeds the hard cap of 16 that applies before any
 *   account-specific limit or capacity check runs, so this triggers the
 *   same way regardless of which account runs it. Not retryable.
 * - **Capacity-limited (429, `rate_limited`/`concurrency_limit_exceeded`)**
 *   — covered by `demoRetryOnCapacityLimit` below WITHOUT calling the real
 *   API: actually exhausting an account's live-session concurrency cap
 *   depends on account-specific state this script doesn't know. Instead,
 *   this demonstrates `callWithRecovery`'s actual retry/backoff behavior
 *   against a scripted `InisError` sequence.
 * - **A session stuck in `state="failed"`** — not triggered here (forcing
 *   a real provisioning failure on demand, e.g. a bad template, risks side
 *   effects unrelated to what this example needs to prove). The pattern
 *   (poll `client.sessions.get()`, branch on `state`, read `statusReason`)
 *   is documented, not exercised live, in `demoFailedStatePattern` below.
 * - **Cancel / time-bound work** — `exec(..., { timeoutMs })` and
 *   `killProcess()` on a background process. There is no separate "cancel
 *   this in-flight request from a different process" primitive for a
 *   plain buffered `exec()` call in the current API — a known gap, not
 *   modelled as anything else here.
 *
 * Cleanup: every session this script creates is destroyed in a `finally`,
 * success or failure.
 *
 * Run:
 *   export INIS_API_KEY=...
 *   npm install @inis-run/sdk
 *   npx tsx error-recovery-and-limits.ts
 */

import { fileURLToPath } from "node:url";
import { Client, InisError, type Session } from "@inis-run/sdk";

/** Generic retry-with-backoff over InisError.retryable/.retryAfter — the
 * same shape whether the underlying cause is a transient node blip, an
 * account concurrency limit, or anything else the server marks retryable.
 * A non-retryable error (validation, not_found, ...) is re-thrown on the
 * first attempt. */
export async function callWithRecovery<T>(fn: () => Promise<T>, maxAttempts = 4): Promise<T> {
  let lastError: InisError | undefined;
  for (let attempt = 1; attempt <= maxAttempts; attempt++) {
    try {
      return await fn();
    } catch (err) {
      if (!(err instanceof InisError) || !err.retryable || attempt === maxAttempts) throw err;
      lastError = err;
      const delay = err.retryAfter ?? Math.min(1.5 * attempt, 8.0);
      console.log(`    retryable error (code=${err.code}), attempt ${attempt}/${maxAttempts}: backing off ${delay.toFixed(1)}s`);
      await new Promise((r) => setTimeout(r, delay * 1000));
    }
  }
  throw lastError;
}

export async function demoGoneSession(client: Client): Promise<void> {
  console.log("1. gone session (destroyed, then accessed again):");
  const session = await client.sessions.create({
    idleTimeoutMs: 60_000,
    maxLifetimeMs: 300_000,
    egress: { mode: "deny" },
  });
  const sessionId = session.sessionId;
  await session.destroy();
  try {
    await client.sessions.attach(sessionId).exec(["echo", "should not run"]);
    throw new Error("expected an error accessing a destroyed session");
  } catch (err) {
    if (!(err instanceof InisError)) throw err;
    console.log(`   got code=${err.code} status=${err.status} retryable=${err.retryable} -> ${err.message}`);
    console.log("   recovery: not retryable, this session is gone for good — create a fresh replacement.");
  }

  console.log("2. bogus session ID (never existed):");
  try {
    await client.sessions.attach("ses_this_id_was_never_created").exec(["echo", "should not run"]);
    throw new Error("expected an error accessing a nonexistent session");
  } catch (err) {
    if (!(err instanceof InisError)) throw err;
    console.log(`   got code=${err.code} status=${err.status} retryable=${err.retryable} -> ${err.message}`);
  }
}

export async function demoRequestLevelLimit(client: Client): Promise<string> {
  console.log("3. over the hard fork-count cap (400, not retryable):");
  const session = await client.sessions.create({
    idleTimeoutMs: 60_000,
    maxLifetimeMs: 300_000,
    egress: { mode: "deny" },
  });
  try {
    await session.fork(17); // hard cap is 16, checked before any account-specific limit
    throw new Error("expected count=17 to exceed the hard fork-count cap");
  } catch (err) {
    if (!(err instanceof InisError)) throw err;
    console.log(`   got code=${err.code} status=${err.status} retryable=${err.retryable} -> ${err.message}`);
    console.log("   recovery: NOT retryable as-is — the request itself is invalid. Retrying identically");
    console.log("   would fail again forever; the fix is reading the error and lowering count, not backoff.");
  }
  return session.sessionId;
}

/** Exercise callWithRecovery's actual retry/backoff behavior against a
 * SCRIPTED sequence of errors — real recovery code, a fake failure
 * sequence — rather than trying to force a genuine 429 out of an account
 * whose concurrency headroom this script doesn't know. This is exactly
 * the shape a real 429 rate_limited/concurrency_limit_exceeded would
 * drive callWithRecovery through. */
export async function demoRetryOnCapacityLimit(): Promise<void> {
  console.log("4. retry/backoff over a capacity-limited (429) error — scripted, not live:");
  let attempts = 0;

  const flakyCall = async (): Promise<string> => {
    attempts += 1;
    if (attempts < 3) {
      throw new InisError("429: concurrency limit exceeded", {
        code: "rate_limited",
        status: 429,
        retryable: true,
        retryAfter: 0.05, // kept tiny so this demo stays fast
      });
    }
    return "ok";
  };

  const result = await callWithRecovery(flakyCall, 5);
  if (result !== "ok" || attempts !== 3) {
    throw new Error(`expected recovery after 3 attempts, got result=${result} attempts=${attempts}`);
  }
  console.log(`   recovered after ${attempts} attempts by backing off on retryable=true`);
}

/** Documents the shape for a session stuck in state="failed" WITHOUT
 * forcing one: poll get(), branch on state, read statusReason. Not
 * exercised live here. */
export async function demoFailedStatePattern(sessionId: string, client: Client): Promise<void> {
  console.log('5. recovering from state="failed" (pattern only, not triggered here):');
  const info = await client.sessions.get(sessionId);
  console.log(`   this session's actual state is ${info.state} (healthy) — the failed-state branch below never fires`);
  if (info.state === "failed") {
    console.log(`   would recover by reading statusReason (${info.statusReason}) and creating a fresh session`);
  } else {
    console.log('   the real pattern: if (await client.sessions.get(id)).state === "failed", read statusReason');
    console.log("   and start over — provisioning failures are not retryable against the same session ID.");
  }
}

export async function demoCancelAndTimeout(client: Client, sessionId: string): Promise<void> {
  console.log("6. cancel / time-bound work:");
  const session: Session = client.sessions.attach(sessionId);

  const result = await session.exec(["sleep", "5"], { timeoutMs: 1_000 });
  console.log(`   exec(sleep 5, timeoutMs=1000): timedOut=${result.timedOut}, durationMs=${result.durationMs}`);
  if (!result.timedOut) {
    throw new Error("expected the 5s sleep to be cut off by the 1s timeout");
  }

  await session.startProcess("long-job", "sleep 30", { keepAlive: true });
  const before = await session.getProcess("long-job");
  const killed = await session.killProcess("long-job");
  console.log(`   background process: state before kill=${before.state}, after kill=${killed.state}`);
  if (killed.state === "running") {
    throw new Error("expected killProcess to have stopped the background job");
  }
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
  let sessionId: string | undefined;
  try {
    await demoGoneSession(client);
    sessionId = await demoRequestLevelLimit(client);
    await demoRetryOnCapacityLimit();
    await demoFailedStatePattern(sessionId, client);
    await demoCancelAndTimeout(client, sessionId);
    return 0;
  } finally {
    if (sessionId !== undefined) {
      await client.sessions.attach(sessionId).destroy();
      console.log(`session ${sessionId} destroyed`);
    }
  }
}

if (process.argv[1] !== undefined && fileURLToPath(import.meta.url) === process.argv[1]) {
  main()
    .then((code) => process.exit(code))
    .catch((err) => {
      if (err instanceof InisError) {
        console.error(`unhandled inis.run API error: ${err.message} (code=${err.code})`);
      } else {
        console.error(`scenario failed: ${err instanceof Error ? err.message : String(err)}`);
      }
      process.exit(1);
    });
}
