/**
 * Tests for the real `InisProcessManager`/`InisProcessHandle` adapter code
 * against `FakeSession` (see fake-session.ts's header for what's faked and
 * what isn't). Every line of process-manager.ts runs for real here.
 */
import { describe, expect, it } from "vitest";
import { InisSandbox } from "./sandbox.js";
import { asSession, FakeSession } from "./fake-session.js";

describe("InisProcessManager (real adapter path, fake Session)", () => {
  it("spawn() + wait() round-trips stdout and a real exit code", async () => {
    const raw = new FakeSession("sess_spawn_1");
    const sandbox = new InisSandbox({ session: asSession(raw) });
    await sandbox.start();

    const handle = await sandbox.processes.spawn("echo hello-spawn");
    const result = await handle.wait();

    expect(result.exitCode).toBe(0);
    expect(result.stdout.trim()).toBe("hello-spawn");
    expect(result.killed).toBe(false);
  });

  it("list() and get() reflect the real session state, not just this manager's local memory", async () => {
    const raw = new FakeSession("sess_list_1");
    const sandbox = new InisSandbox({ session: asSession(raw) });
    await sandbox.start();

    const handle = await sandbox.processes.spawn("echo listed");
    await handle.wait();

    const list = await sandbox.processes.list();
    expect(list.map((p) => p.pid)).toContain(handle.pid);

    // get() on a name this exact manager instance never spawned still works
    // -- it falls back to a real getProcess() lookup, matching @mastra/e2b's
    // own "spawned externally or before reconnection" fallback rationale.
    const fresh = new InisSandbox({ session: asSession(raw) });
    await fresh.start();
    const reattached = await fresh.processes.get(handle.pid);
    expect(reattached).toBeDefined();
    expect(reattached?.exitCode).toBe(0);
  });

  it("get() of a name that was never started returns undefined, not a throw", async () => {
    const raw = new FakeSession("sess_get_missing");
    const sandbox = new InisSandbox({ session: asSession(raw) });
    await sandbox.start();
    const handle = await sandbox.processes.get("never-existed");
    expect(handle).toBeUndefined();
  });

  it("kill() on a genuinely running process really terminates it (real SIGTERM/SIGKILL server-side)", async () => {
    const raw = new FakeSession("sess_kill_1");
    const sandbox = new InisSandbox({ session: asSession(raw) });
    await sandbox.start();

    const handle = await sandbox.processes.spawn("long-running-marker");
    // Give streamProcessLogs' poll loop a tick to start before killing.
    await new Promise((resolve) => setTimeout(resolve, 10));
    const killed = await handle.kill();
    expect(killed).toBe(true);

    const result = await handle.wait();
    expect(result.killed).toBe(true);
    expect(result.exitCode).toBe(143);
  });

  it("kill() of an unknown pid returns false, not a throw", async () => {
    const raw = new FakeSession("sess_kill_missing");
    const sandbox = new InisSandbox({ session: asSession(raw) });
    await sandbox.start();
    const killed = await sandbox.processes.kill("does-not-exist");
    expect(killed).toBe(false);
  });

  it("spawn(..., { timeout }) really kills a long-running process after the deadline (host-orchestrated, not server-enforced)", async () => {
    const raw = new FakeSession("sess_timeout_1");
    const sandbox = new InisSandbox({ session: asSession(raw) });
    await sandbox.start();

    const handle = await sandbox.processes.spawn("long-running-marker", { timeout: 30 });
    const result = await handle.wait();
    expect(result.killed).toBe(true);
    // No signal distinguishes "killed for timing out" from any other kill,
    // so this surfaces as killed, never timedOut -- see wait()'s doc comment.
    expect(result.timedOut).toBe(false);
  });

  it("sendStdin() is declared unsupported, not silently swallowed", async () => {
    const raw = new FakeSession("sess_stdin_1");
    const sandbox = new InisSandbox({ session: asSession(raw) });
    await sandbox.start();
    const handle = await sandbox.processes.spawn("echo hi");
    await expect(handle.sendStdin("data\n")).rejects.toThrow(/does not support sendStdin/);
  });

  it("spawn(..., { env }) sets real per-call env vars via a shell export prefix, not a no-op", async () => {
    const raw = new FakeSession("sess_env_1");
    const sandbox = new InisSandbox({ session: asSession(raw) });
    await sandbox.start();
    await sandbox.processes.spawn("echo hi", { env: { MY_VAR: "value with spaces" } });
    const log = raw.getStartProcessLog();
    const last = log[log.length - 1];
    expect(last.command.join(" ")).toContain("export MY_VAR=");
    expect(last.command.join(" ")).toContain("value with spaces");
  });

  it("two independently-spawned processes never share output or exit state", async () => {
    const raw = new FakeSession("sess_iso_1");
    const sandbox = new InisSandbox({ session: asSession(raw) });
    await sandbox.start();

    const [a, b] = await Promise.all([
      sandbox.processes.spawn("echo marker-A"),
      sandbox.processes.spawn("echo marker-B"),
    ]);
    const [resultA, resultB] = await Promise.all([a.wait(), b.wait()]);

    expect(resultA.stdout.trim()).toBe("marker-A");
    expect(resultB.stdout.trim()).toBe("marker-B");
    expect(resultA.stdout).not.toContain("marker-B");
    expect(resultB.stdout).not.toContain("marker-A");
  });
});
