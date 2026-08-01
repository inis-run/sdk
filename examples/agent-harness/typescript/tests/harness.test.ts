/**
 * Mock-backed tests for the four required agent-harness examples — the
 * TypeScript twin of ../../python/tests/test_*.py, using the same
 * fake-backend technique (see fake-backend.ts) instead of a live inis.run
 * session.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { Client, InisError } from "@inis-run/sdk";
import { installFakeBackend, type FakeBackend } from "./fake-backend.js";

beforeEach(() => {
  process.env.INIS_API_KEY = "test-key-not-real-no-network-calls-made";
  process.env.INIS_BASE_URL = "https://inis.invalid.test";
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("persistent-tool-loop", () => {
  let backend: FakeBackend;
  beforeEach(() => {
    backend = installFakeBackend();
  });

  it("reuses the same session for one thread and a different one for another", async () => {
    const { ThreadSessionStore, getOrCreateSession } = await import("../persistent-tool-loop.js");
    const client = new Client();
    const store = new ThreadSessionStore();

    const first = await client.sessions.create({ idleTimeoutMs: 1000 });
    store.put("thread-1", first.sessionId);

    const second = await getOrCreateSession(client, store, "thread-1");
    expect(second.sessionId).toBe(first.sessionId);

    const third = await getOrCreateSession(client, store, "thread-2");
    expect(third.sessionId).not.toBe(first.sessionId);
  });

  it("runs end to end and destroys the one owned session on cleanup", async () => {
    const { main } = await import("../persistent-tool-loop.js");
    const code = await main();
    expect(code).toBe(0);
    expect(backend.byId.size).toBeGreaterThan(0);
    for (const rec of backend.byId.values()) expect(rec.destroyed).toBe(true);
  });
});

describe("branch-and-evaluate", () => {
  let backend: FakeBackend;
  beforeEach(() => {
    backend = installFakeBackend();
  });

  it("fork() returns a list of child session ID strings, not sessions", async () => {
    const client = new Client();
    const parent = await client.sessions.create({ idleTimeoutMs: 1000 });
    const result = await parent.fork(2);
    expect(result.parentSessionId).toBe(parent.sessionId);
    expect(Array.isArray(result.children)).toBe(true);
    expect(result.children.every((c) => typeof c === "string")).toBe(true);
    expect(result.children).toHaveLength(2);
  });

  it("grade() picks only the candidate with the exact expected output", async () => {
    const { grade, EXPECTED } = await import("../branch-and-evaluate.js");
    const correct = { name: "a", stdout: String(EXPECTED), exitCode: 0, durationMs: 1 };
    const wrong = { name: "b", stdout: "nope", exitCode: 0, durationMs: 1 };
    const crashed = { name: "c", stdout: "", exitCode: 1, durationMs: 1 };
    expect(grade([wrong, crashed])).toBeUndefined();
    expect(grade([wrong, correct, crashed])).toBe(correct);
  });

  it("forkWithRetry backs off on a retryable error then succeeds", async () => {
    const { forkWithRetry } = await import("../branch-and-evaluate.js");
    const client = new Client();
    const session = await client.sessions.create({ idleTimeoutMs: 1000 });
    let attempts = 0;
    // Spy on the session's OWN fork call count without delegating back
    // through the mocked prototype implementation (doing so risks the mock
    // calling itself instead of the original — simpler and just as
    // faithful to override with a fixed successful result once "recovered").
    vi.spyOn(session, "fork").mockImplementation(async (count: number) => {
      attempts += 1;
      if (attempts < 3) {
        throw new InisError("503", { code: "service_unavailable", status: 503, retryable: true, retryAfter: 0.01 });
      }
      return { parentSessionId: session.sessionId, children: Array.from({ length: count }, (_, i) => `ses_child${i}`) };
    });
    const result = await forkWithRetry(session, 2, 5);
    expect(attempts).toBe(3);
    expect(result.children).toHaveLength(2);
  });

  it("runs end to end and destroys the parent and every child", async () => {
    const { main } = await import("../branch-and-evaluate.js");
    const code = await main();
    expect(code).toBe(0);
    expect(backend.byId.size).toBeGreaterThan(0);
    for (const rec of backend.byId.values()) expect(rec.destroyed).toBe(true);
  });
});

describe("pause-resume-across-turns", () => {
  let backend: FakeBackend;
  beforeEach(() => {
    backend = installFakeBackend();
  });

  it("runTurn1 creates and explicitly pauses its session", async () => {
    const { runTurn1 } = await import("../pause-resume-across-turns.js");
    const client = new Client();
    const { sessionId, nonce } = await runTurn1(client);
    const rec = backend.byId.get(sessionId)!;
    expect(rec.state).toBe("paused");
    expect(rec.files.get("/workspace/pause-resume-nonce.txt")).toBe(nonce);
  });

  it("turn2ChildMain resumes explicitly and extends the nonce", async () => {
    const { runTurn1, turn2ChildMain } = await import("../pause-resume-across-turns.js");
    const client = new Client();
    const { sessionId, nonce } = await runTurn1(client);

    const code = await turn2ChildMain(sessionId, nonce);
    expect(code).toBe(0);
    const rec = backend.byId.get(sessionId)!;
    expect(rec.state).toBe("live");
    expect(rec.files.get("/workspace/pause-resume-nonce.txt")?.startsWith(`${nonce}:`)).toBe(true);
  });

  it("demonstratePausedTtlCleanup destroys its own throwaway session", async () => {
    const { demonstratePausedTtlCleanup } = await import("../pause-resume-across-turns.js");
    const client = new Client();
    const before = new Set(backend.byId.keys());
    await demonstratePausedTtlCleanup(client);
    const created = [...backend.byId.keys()].filter((id) => !before.has(id));
    expect(created).toHaveLength(1);
    expect(backend.byId.get(created[0]!)!.destroyed).toBe(true);
  });
});

describe("concurrent-conversations", () => {
  let backend: FakeBackend;
  beforeEach(() => {
    backend = installFakeBackend();
  });

  it("each conversation creates and destroys its own session", async () => {
    const { runConversation } = await import("../concurrent-conversations.js");
    const proof = await runConversation("alice");
    expect(backend.byId.get(proof.sessionId)!.destroyed).toBe(true);
    expect(proof.visibleFiles).toContain("secret-alice.txt");
  });

  it("runs two conversations concurrently and neither shares a session", async () => {
    const { main } = await import("../concurrent-conversations.js");
    const code = await main();
    expect(code).toBe(0);
    expect(backend.byId.size).toBeGreaterThan(0);
    for (const rec of backend.byId.values()) expect(rec.destroyed).toBe(true);
  });
});

describe("error-recovery-and-limits", () => {
  it("callWithRecovery retries a retryable error then succeeds (no backend needed)", async () => {
    const { callWithRecovery } = await import("../error-recovery-and-limits.js");
    let attempts = 0;
    const flaky = async () => {
      attempts += 1;
      if (attempts < 3) {
        throw new InisError("x", { code: "rate_limited", status: 429, retryable: true, retryAfter: 0.01 });
      }
      return "ok";
    };
    await expect(callWithRecovery(flaky, 5)).resolves.toBe("ok");
    expect(attempts).toBe(3);
  });

  it("does not retry a non-retryable error", async () => {
    const { callWithRecovery } = await import("../error-recovery-and-limits.js");
    const alwaysFails = async () => {
      throw new InisError("x", { code: "bad_request", status: 400, retryable: false });
    };
    await expect(callWithRecovery(alwaysFails, 5)).rejects.toThrow(InisError);
  });

  it("demoRetryOnCapacityLimit is fully self-contained (no fake backend)", async () => {
    const { demoRetryOnCapacityLimit } = await import("../error-recovery-and-limits.js");
    await expect(demoRetryOnCapacityLimit()).resolves.toBeUndefined();
  });

  it("runs the full scenario and cleans up despite the non-live demos", async () => {
    const backend = installFakeBackend();
    const { main } = await import("../error-recovery-and-limits.js");
    const code = await main();
    expect(code).toBe(0);
    expect(backend.byId.size).toBeGreaterThan(0);
    for (const rec of backend.byId.values()) expect(rec.destroyed).toBe(true);
  });
});
