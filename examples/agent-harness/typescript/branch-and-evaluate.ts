/**
 * Branch and evaluate (#1237, epic #1229) — establish shared state once,
 * fork it into independent children, run candidate work on each child at
 * the same time, and grade the results OUTSIDE the sandbox to pick a
 * winner.
 *
 * ## Getting the real API right (this is the part the issue flagged as wrong)
 *
 * The illustrative `session.fork(n=10)` returning an iterable `ForkResult`
 * you loop over never existed. The real surface (verified against
 * sdk/typescript/src/client.ts) is:
 *
 *     session.fork(count: number): Promise<ForkResult>
 *     // ForkResult = { parentSessionId: string; children: string[] }
 *
 * `children` is a list of plain session ID strings, NOT live `Session`
 * handles — each one is a fully independent session that inherited the
 * parent's filesystem at fork time and diverges from there. To run
 * anything on a child, attach to its ID: `client.sessions.attach(childId)`.
 *
 * ## Is running candidates on the children actually concurrent?
 *
 * Yes — but not via a "parallel fork helper". There IS a
 * `client.sessions.batchExec` / `POST /v1/sessions/batch/exec` endpoint
 * that runs one command across multiple session IDs, and it is tempting to
 * reach for it here. Checked against the server's handler for it: it loops
 * over the session IDs and execs each one **sequentially** — a fan-out
 * convenience for the same command on many sessions, not a concurrency
 * primitive. This example does not use it, and does not call it "parallel"
 * anywhere.
 *
 * The real concurrency comes from the client: each forked child is an
 * independently addressable session, so issuing one `exec()` call per
 * child concurrently (`Promise.all`) genuinely overlaps in wall-clock
 * time — proven below by checking total elapsed time stays close to one
 * candidate's sleep, not the sum of all of them.
 *
 * Fork's cost story (starting N candidates from one expensive setup vs.
 * re-running setup N times) is covered in #912, not duplicated here.
 *
 * Cleanup: the parent AND every forked child (each an independent,
 * independently billed session) are destroyed in the `finally` block,
 * success or failure.
 *
 * Run:
 *   export INIS_API_KEY=...
 *   npm install @inis-run/sdk
 *   npx tsx branch-and-evaluate.ts
 */

import { fileURLToPath } from "node:url";
import { Client, InisError, type Session } from "@inis-run/sdk";

// The server's default per-request fork cap is 4 (a raised account limit
// can go up to a hard cap of 16); this example forks 2 to stay comfortably
// under the default even on an account with no capacity headroom to
// spare. Not run against production in this change — see the PR/issue for
// exactly what was and wasn't executed.
const FORK_COUNT = 2;

const CANDIDATES: Record<string, string> = {
  iterative: "n=int(open('/workspace/n.txt').read()); print(sum(range(1, n + 1)))",
  off_by_one_bug: "n=int(open('/workspace/n.txt').read()); print(sum(range(1, n)))", // wrong on purpose
};
const N = 1000;
export const EXPECTED = (N * (N + 1)) / 2;

export interface CandidateResult {
  name: string;
  stdout: string;
  exitCode: number;
  durationMs: number;
}

/** Fork, retrying on the server's own retryable signal (InisError.retryable
 * — driven by the error `code`, e.g. `service_unavailable`/`unavailable`
 * for a transient capacity shortfall) rather than a hardcoded guess of
 * which codes are transient. This is the "recover from a capacity-limited
 * request" pattern the task-oriented docs describe: reserving restore
 * slots for several fork children at once can genuinely return a
 * retryable 503 on a busy fleet — not exercised against production in
 * this change, see the PR/issue for what was. */
export async function forkWithRetry(session: Session, count: number, maxAttempts = 5) {
  let lastError: InisError | undefined;
  for (let attempt = 1; attempt <= maxAttempts; attempt++) {
    try {
      return await session.fork(count);
    } catch (err) {
      if (!(err instanceof InisError) || !err.retryable || attempt === maxAttempts) throw err;
      lastError = err;
      const delay = err.retryAfter ?? Math.min(2 * attempt, 10);
      console.log(
        `  fork attempt ${attempt}/${maxAttempts} hit a retryable error (code=${err.code}): ` +
          `${err.message} — retrying in ${delay.toFixed(1)}s`,
      );
      await new Promise((r) => setTimeout(r, delay * 1000));
    }
  }
  throw lastError;
}

/** Attach to one forked child and run its candidate. Each call attaches
 * its OWN session handle — nothing shared across concurrent calls. */
export async function runCandidate(client: Client, childSessionId: string, name: string, code: string): Promise<CandidateResult> {
  const session = client.sessions.attach(childSessionId);
  await session.writeFile(`/workspace/${name}.py`, code);
  // A deliberate sleep makes the overlap in wall-clock time obvious below —
  // real candidate work wouldn't need this.
  const result = await session.exec(["bash", "-lc", `sleep 2; python3 /workspace/${name}.py`]);
  return { name, stdout: result.stdout.trim(), exitCode: result.exitCode, durationMs: result.durationMs };
}

/** The grader runs OUTSIDE the sandbox, on the host, over the returned
 * stdout/exitCode — picks the first candidate that exited 0 with the
 * exact expected sum. */
export function grade(results: CandidateResult[]): CandidateResult | undefined {
  return results.find((r) => r.exitCode === 0 && r.stdout === String(EXPECTED));
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
  let parentId: string | undefined;
  let childIds: string[] = [];

  try {
    // 1. Establish shared state ONCE.
    const parent = await client.sessions.create({
      idleTimeoutMs: 60_000,
      maxLifetimeMs: 5 * 60_000,
      egress: { mode: "deny" },
    });
    parentId = parent.sessionId;
    try {
      console.log(`parent session ${parentId}: writing shared input (n=${N})`);
      await parent.writeFile("/workspace/n.txt", String(N));

      // 2. Fork into independent children — count=, real return type.
      const forkResult = await forkWithRetry(parent, FORK_COUNT);
      childIds = forkResult.children;
      console.log(`forked ${childIds.length} children from ${forkResult.parentSessionId}: ${childIds}`);
      if (childIds.length !== FORK_COUNT) {
        throw new Error(`expected ${FORK_COUNT} children, got ${childIds.length}`);
      }

      // 3. Run each candidate CONCURRENTLY, one per child.
      const names = Object.entries(CANDIDATES);
      const start = performance.now();
      const results = await Promise.all(
        childIds.map((childId, i) => {
          const [name, code] = names[i]!;
          return runCandidate(client, childId, name, code);
        }),
      );
      const elapsedS = (performance.now() - start) / 1000;

      console.log(`ran ${results.length} candidates in ${elapsedS.toFixed(1)}s wall-clock (each sleeps 2s):`);
      for (const r of [...results].sort((a, b) => a.name.localeCompare(b.name))) {
        console.log(`  ${r.name}: stdout=${JSON.stringify(r.stdout)} exitCode=${r.exitCode} durationMs=${r.durationMs}`);
      }

      // If this were sequential (like batchExec's server-side loop), N
      // candidates each sleeping 2s would take >= N*2s.
      if (elapsedS >= Object.keys(CANDIDATES).length * 2.0) {
        throw new Error(`expected concurrent overlap (~2s), took ${elapsedS.toFixed(1)}s — looks sequential`);
      }

      // 4. Grade OUTSIDE the sandbox and select a winner.
      const winner = grade(results);
      if (!winner) throw new Error("no candidate produced the expected result");
      console.log(`winner: ${winner.name} (expected ${EXPECTED}, got ${winner.stdout})`);
    } finally {
      await parent.destroy();
      console.log(`parent session ${parentId} destroyed`);
    }

    return 0;
  } finally {
    for (const childId of childIds) {
      try {
        await client.sessions.attach(childId).destroy();
        console.log(`child session ${childId} destroyed`);
      } catch (err) {
        console.error(`warning: failed to destroy child ${childId}: ${err instanceof Error ? err.message : err}`);
      }
    }
  }
}

if (process.argv[1] !== undefined && fileURLToPath(import.meta.url) === process.argv[1]) {
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
