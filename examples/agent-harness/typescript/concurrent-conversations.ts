/**
 * Concurrent customer conversations — run at least two conversations truly
 * simultaneously and PROVE their files, background
 * processes, and cleanup can't cross, rather than asserting it.
 *
 * No module-global session variable anywhere: `runConversation` below is a
 * plain function that creates its OWN session locally and returns
 * everything the caller needs to verify isolation — nothing about one
 * conversation's session is visible to another except by literally
 * reading the other's return value, which this script does on purpose
 * (from the host, after both finish) specifically to prove neither
 * conversation could see it FROM INSIDE its own sandbox.
 *
 * Concurrency is real, not simulated: both conversations run via
 * `Promise.all` against separate sessions (separate VMs). Wall-clock proof
 * follows the same shape as branch-and-evaluate.ts's fork-candidate timing
 * check.
 *
 * Cleanup: each conversation owns exactly one session and destroys it in
 * its own `finally`, independent of whether the OTHER conversation
 * succeeded or failed.
 *
 * Run:
 *   export INIS_API_KEY=...
 *   npm install @inis-run/sdk
 *   npx tsx concurrent-conversations.ts
 */

import { fileURLToPath } from "node:url";
import { Client, InisError } from "@inis-run/sdk";

const WORKSPACE_DIR = "/workspace";

export interface ConversationProof {
  convoId: string;
  sessionId: string;
  secretPath: string;
  processName: string;
  visibleFiles: string[];
  visibleProcessNames: string[];
  durationS: number;
}

/** Everything a customer conversation needs, entirely local to this call:
 * its own Client, its own session, its own secret file, its own named
 * background process. Nothing here is module-level or otherwise shared
 * mutable state — call this twice concurrently and the two invocations
 * share nothing but the imported class. */
export async function runConversation(convoId: string): Promise<ConversationProof> {
  const start = performance.now();
  const client = new Client();
  const secretPath = `${WORKSPACE_DIR}/secret-${convoId}.txt`;
  const secretValue = `only-${convoId}-should-see-this`;
  const processName = `proc-${convoId}`;

  const session = await client.sessions.create({
    idleTimeoutMs: 60_000,
    maxLifetimeMs: 5 * 60_000,
    egress: { mode: "deny" },
  });
  try {
    await session.writeFile(secretPath, secretValue);
    // A uniquely-named background process, distinguishable from the other
    // conversation's, so listProcesses() isolation is provable too.
    await session.startProcess(processName, "sleep 30", { keepAlive: true });
    await session.exec(["sleep", "2"]);

    const visibleFiles = await session.listFiles(WORKSPACE_DIR);
    const visibleProcessNames = (await session.listProcesses()).map((p) => p.name);

    await session.killProcess(processName);

    return {
      convoId,
      sessionId: session.sessionId,
      secretPath,
      processName,
      visibleFiles,
      visibleProcessNames,
      durationS: (performance.now() - start) / 1000,
    };
  } finally {
    await session.destroy();
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

  const convoIds = ["alice", "bob"];

  const start = performance.now();
  const resultsArr = await Promise.all(convoIds.map((id) => runConversation(id)));
  const elapsedS = (performance.now() - start) / 1000;
  const results = new Map(resultsArr.map((r) => [r.convoId, r]));

  console.log(`ran ${convoIds.length} conversations concurrently in ${elapsedS.toFixed(1)}s wall-clock:`);
  for (const id of convoIds) {
    const p = results.get(id)!;
    console.log(
      `  ${id}: session=${p.sessionId} own_files=${JSON.stringify(p.visibleFiles)} ` +
        `own_processes=${JSON.stringify(p.visibleProcessNames)} (${p.durationS.toFixed(1)}s)`,
    );
  }

  // Proof #1: genuinely concurrent, not sequential.
  const durations = resultsArr.map((r) => r.durationS);
  const maxSingle = Math.max(...durations);
  const minSingle = Math.min(...durations);
  if (elapsedS >= maxSingle + minSingle * 0.8) {
    throw new Error(
      `expected overlap: total ${elapsedS.toFixed(1)}s should be close to the slowest single ` +
        `conversation's ${maxSingle.toFixed(1)}s, not their sum`,
    );
  }

  // Proof #2: cross-contamination check, from the HOST's view.
  for (const id of convoIds) {
    const other = convoIds.find((c) => c !== id)!;
    const mine = results.get(id)!;
    const theirs = results.get(other)!;
    const mineBasename = mine.secretPath.split("/").pop()!;
    const theirsBasename = theirs.secretPath.split("/").pop()!;
    if (!mine.visibleFiles.includes(mineBasename)) {
      throw new Error(`${id}'s own secret file should be visible to it, got ${JSON.stringify(mine.visibleFiles)}`);
    }
    if (mine.visibleFiles.includes(theirsBasename)) {
      throw new Error(`ISOLATION VIOLATION: ${id}'s session saw ${other}'s secret file ${theirsBasename}`);
    }
    if (mine.visibleProcessNames.includes(theirs.processName)) {
      throw new Error(`ISOLATION VIOLATION: ${id}'s session saw ${other}'s process ${theirs.processName}`);
    }
    if (mine.sessionId === theirs.sessionId) {
      throw new Error("both conversations ended up on the SAME session — not isolated at all");
    }
  }

  console.log("isolation confirmed: each conversation saw only its own file and process; sessions were distinct throughout");
  return 0;
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
