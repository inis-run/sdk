/**
 * Persistent tool loop (#1237, epic #1229) — one inis.run session per agent
 * thread, reconnected across turns by a HOST-persisted session ID, with the
 * model restricted to a bounded exec/file tool surface.
 *
 * This is the shape a real agent framework integration takes: the
 * framework (not the model) creates the session, remembers its ID against
 * the conversation/thread, and destroys it when the conversation ends. The
 * model only ever sees `BoundedToolSurface` below — it never sees the
 * inis.run API key, never sees the session ID as something it could pass
 * to a different operation, and has no pause/resume/fork/destroy tool at
 * all. That split is the same one #1241 enforces in the real adapters
 * (lifecycle admin is opt-in, off by default) — this example builds the
 * same shape by hand, framework-free, so it's clear that's a property of
 * the *pattern*, not of one specific adapter.
 *
 * Read examples/quickstart/typescript/quickstart.ts first — this reuses
 * its create/attach/destroy shape. What's new here: THREE separate turns,
 * the middle one served by a second `Client` sharing no in-process state
 * with the first (standing in for a different host request/process), each
 * turn running multiple tool calls that must observe the same filesystem
 * AND the same running background process.
 *
 * Egress is session-level deny-by-default here (set explicitly; the
 * platform's own out-of-the-box default is full egress — see
 * docs-site/content/docs/networking.mdx).
 *
 * Cleanup: the ONE session this script owns (created in turn 1) is
 * destroyed in the `finally` block at the bottom, on both the success and
 * the failure path.
 *
 * Run:
 *   export INIS_API_KEY=...
 *   npm install @inis-run/sdk
 *   npx tsx persistent-tool-loop.ts
 */

import { fileURLToPath } from "node:url";
import { Client, InisError, type Session } from "@inis-run/sdk";

const STATE_PATH = "/workspace/agent/state.json";
const TICKER_PATH = "/workspace/agent/ticker.txt";
const TICKER_NAME = "ticker";

/** Host-side mapping from an agent thread ID to a session ID — nothing
 * more. NOT a place that stores a live Session object shared across turns
 * (that would be the "module-global session variable" #1229/#1237 rule
 * out): instantiated once per run of this script, not at module scope. */
export class ThreadSessionStore {
  private byThread = new Map<string, string>();
  get(threadId: string): string | undefined {
    return this.byThread.get(threadId);
  }
  put(threadId: string, sessionId: string): void {
    this.byThread.set(threadId, sessionId);
  }
}

/** What the model is actually allowed to call. No API key, no session
 * lifecycle (pause/resume/fork/destroy), no way to target a different
 * session than the one the host attached it to. */
export class BoundedToolSurface {
  constructor(private session: Session) {}
  async exec(command: string[]): Promise<{ stdout: string; stderr: string; exitCode: number }> {
    const result = await this.session.exec(command);
    return { stdout: result.stdout, stderr: result.stderr, exitCode: result.exitCode };
  }
  async readFile(path: string): Promise<string> {
    const content = await this.session.readFile(path);
    return typeof content === "string" ? content : new TextDecoder().decode(content);
  }
  async writeFile(path: string, content: string): Promise<void> {
    await this.session.writeFile(path, content);
  }
}

/** The one piece of lifecycle logic the HOST owns: look up whether this
 * thread already has a session; attach if so, create (and record) a new
 * one if not. The model never makes this decision. */
export async function getOrCreateSession(client: Client, store: ThreadSessionStore, threadId: string): Promise<Session> {
  const existingId = store.get(threadId);
  if (existingId !== undefined) {
    console.log(`  host: thread ${threadId} already has session ${existingId} — attaching`);
    return client.sessions.attach(existingId);
  }
  const session = await client.sessions.create({
    idleTimeoutMs: 120_000,
    maxLifetimeMs: 10 * 60_000,
    egress: { mode: "deny" },
  });
  store.put(threadId, session.sessionId);
  console.log(`  host: created session ${session.sessionId} for thread ${threadId} (egress: deny-by-default)`);
  return session;
}

export async function readCounter(tools: BoundedToolSurface): Promise<number> {
  return (JSON.parse(await tools.readFile(STATE_PATH)) as { counter: number }).counter;
}

export async function main(): Promise<number> {
  if (!process.env.INIS_API_KEY) {
    console.error(
      "INIS_API_KEY is not set. Export it before running this example — " +
        "never put it in code, a file, or a session/guest command.",
    );
    return 1;
  }

  const threadId = `thread-${Math.random().toString(16).slice(2, 10)}`;
  const store = new ThreadSessionStore();
  const client = new Client();
  let ownedSessionId: string | undefined;

  try {
    // ── Turn 1: brand-new session, background ticker process started
    //    with keepAlive so it survives between turns.
    console.log("turn 1 (host process A, brand-new session):");
    const session = await getOrCreateSession(client, store, threadId);
    ownedSessionId = session.sessionId;
    const tools = new BoundedToolSurface(session);
    await tools.exec(["mkdir", "-p", "/workspace/agent"]);
    await tools.writeFile(STATE_PATH, JSON.stringify({ counter: 1 }));
    await session.startProcess(
      TICKER_NAME,
      `i=0; while true; do i=$((i+1)); echo $i > ${TICKER_PATH}; sleep 1; done`,
      { keepAlive: true },
    );
    const counter1 = await readCounter(tools);
    console.log(`  wrote counter=${counter1}, started background process ${TICKER_NAME}`);

    await new Promise((r) => setTimeout(r, 2000));

    // ── Turn 2: a DIFFERENT host process picks up the same thread. New
    //    Client, new BoundedToolSurface — nothing shared in-process with
    //    turn 1 except the thread ID -> session ID mapping.
    console.log("turn 2 (host process B, reconnects by thread ID only):");
    const clientB = new Client();
    let tickAtTurn2: number;
    try {
      const sessionB = await getOrCreateSession(clientB, store, threadId);
      const toolsB = new BoundedToolSurface(sessionB);
      const counter2 = await readCounter(toolsB);
      if (counter2 !== counter1) {
        throw new Error(`expected counter to still be ${counter1}, got ${counter2}`);
      }
      await toolsB.writeFile(STATE_PATH, JSON.stringify({ counter: counter2 + 1 }));
      const processes = await sessionB.listProcesses();
      const ticker = processes.find((p) => p.name === TICKER_NAME);
      if (!ticker || ticker.state !== "running") {
        throw new Error(`expected ${TICKER_NAME} still running from turn 1, got ${JSON.stringify(ticker)}`);
      }
      tickAtTurn2 = parseInt((await toolsB.readFile(TICKER_PATH)).trim(), 10);
      console.log(
        `  same filesystem: counter ${counter1} -> ${counter2 + 1}; ` +
          `same process: ${TICKER_NAME} still running, ticked to ${tickAtTurn2}`,
      );
    } finally {
      // clientB has no `close()` — the TS client uses ambient fetch, not a
      // pooled connection object to release (unlike the Python httpx.Client).
    }

    await new Promise((r) => setTimeout(r, 2000));

    // ── Turn 3: back to the original process/client.
    console.log("turn 3 (host process A again):");
    const sessionC = await getOrCreateSession(client, store, threadId);
    const toolsC = new BoundedToolSurface(sessionC);
    const counter3 = await readCounter(toolsC);
    if (counter3 !== counter1 + 1) {
      throw new Error(`expected counter to be ${counter1 + 1}, got ${counter3}`);
    }
    const tickAtTurn3 = parseInt((await toolsC.readFile(TICKER_PATH)).trim(), 10);
    if (tickAtTurn3 < tickAtTurn2) {
      throw new Error("ticker went backwards — process did not persist");
    }
    console.log(`  counter is ${counter3} (turn 1 -> turn 2 -> turn 3), ticker now at ${tickAtTurn3}`);

    await sessionC.killProcess(TICKER_NAME);
    console.log(`  stopped ${TICKER_NAME} before ending the conversation`);

    return 0;
  } finally {
    if (ownedSessionId !== undefined) {
      await client.sessions.attach(ownedSessionId).destroy();
      console.log(`session ${ownedSessionId} destroyed (thread ${threadId} conversation ended)`);
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
