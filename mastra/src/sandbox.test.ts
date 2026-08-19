/**
 * Tests for the real `InisSandbox` adapter code against `FakeSession` (see
 * fake-session.ts's header). Every line of sandbox.ts runs for real here.
 */
import { describe, expect, it } from "vitest";
import {
  createWorkspaceTools,
  MountNotSupportedError,
  SandboxNotReadyError,
  Workspace,
  type WorkspaceFilesystem,
} from "@mastra/core/workspace";
import { InisSandbox } from "./sandbox.js";
import { asSession, FakeSession } from "./fake-session.js";

describe("InisSandbox — owned mode", () => {
  it("start() creates a session lazily via the client; destroy() really destroys it", async () => {
    const raw = new FakeSession("sess_owned_1");
    const sandbox = new InisSandbox({ client: { sessions: { create: async () => asSession(raw) } } as never });
    await sandbox.start();
    expect(sandbox.session.sessionId).toBe("sess_owned_1");

    await sandbox.destroy();
    expect(raw.destroyCalls).toBe(1);
    expect(raw.destroyed).toBe(true);
  });

  it("stop() pauses and a subsequent start() resumes, rather than recreating", async () => {
    const raw = new FakeSession("sess_owned_2");
    const sandbox = new InisSandbox({ client: { sessions: { create: async () => asSession(raw) } } as never });
    await sandbox.start();

    await sandbox.stop();
    expect(raw.pauseCalls).toBe(1);
    expect(raw.state).toBe("paused");

    await sandbox.start();
    expect(raw.resumeCalls).toBe(1);
    expect(raw.state).toBe("live");
    // Still the same underlying session -- resumed, not recreated.
    expect(sandbox.session.sessionId).toBe("sess_owned_2");
  });

  it("destroy() before start() is a safe no-op", async () => {
    const sandbox = new InisSandbox();
    await expect(sandbox.destroy()).resolves.toBeUndefined();
  });

  it("start() forwards connections through to client.sessions.create()", async () => {
    const raw = new FakeSession("sess_owned_connections");
    const connections = [
      {
        name: "stripe",
        origin: "https://api.stripe.com",
        authentication: { type: "bearer" as const, secret: "sk_test_x" },
        allow: { methods: ["GET"], paths: ["/v1/customers"] },
      },
    ];
    let createOpts: unknown;
    const sandbox = new InisSandbox({
      client: {
        sessions: {
          create: async (opts: unknown) => {
            createOpts = opts;
            return asSession(raw);
          },
        },
      } as never,
      connections,
    });
    await sandbox.start();
    expect(createOpts).toMatchObject({ connections });
  });
});

describe("InisSandbox — attached mode", () => {
  it("start() never creates a new session, only verifies reachability", async () => {
    const raw = new FakeSession("sess_attached_1");
    const sandbox = new InisSandbox({ session: asSession(raw) });
    await sandbox.start();
    expect(sandbox.session).toBe(asSession(raw));
  });

  it("start() by session id reattaches via client.sessions.attach(), never sessions.create()", async () => {
    const raw = new FakeSession("sess_attached_2");
    let attachCalledWith: string | undefined;
    const fakeClient = {
      sessions: {
        attach: (id: string) => {
          attachCalledWith = id;
          return asSession(raw);
        },
        create: async () => {
          throw new Error("attach mode must never call sessions.create()");
        },
      },
    } as never;
    const sandbox = new InisSandbox({ session: "sess_attached_2", client: fakeClient });
    await sandbox.start();
    expect(attachCalledWith).toBe("sess_attached_2");
    expect(sandbox.session.sessionId).toBe("sess_attached_2");
  });

  it("destroy() NEVER destroys an attached session — the core lifecycle invariant", async () => {
    const raw = new FakeSession("sess_attached_3");
    const sandbox = new InisSandbox({ session: asSession(raw) });
    await sandbox.start();

    await sandbox.destroy();

    expect(raw.destroyCalls).toBe(0);
    expect(raw.destroyed).toBe(false);
    expect(raw.state).toBe("live");
  });

  it("destroy() never destroys an attached session even on the interrupted/exception path", async () => {
    const raw = new FakeSession("sess_attached_4");
    const sandbox = new InisSandbox({ session: asSession(raw) });
    await sandbox.start();

    async function runConversation(): Promise<void> {
      try {
        await sandbox.processes.spawn("echo before-throw");
        throw new Error("simulated mid-conversation failure");
      } finally {
        // The host's own cleanup path: a real caller reaches for
        // sandbox.destroy() in a finally block just like this.
        await sandbox.destroy();
      }
    }

    await expect(runConversation()).rejects.toThrow("simulated mid-conversation failure");
    expect(raw.destroyCalls).toBe(0);
    expect(raw.destroyed).toBe(false);
  });

  it("stop() never pauses an attached session", async () => {
    const raw = new FakeSession("sess_attached_5");
    const sandbox = new InisSandbox({ session: asSession(raw) });
    await sandbox.start();
    await sandbox.stop();
    expect(raw.pauseCalls).toBe(0);
    expect(raw.state).toBe("live");
  });
});

describe("InisSandbox — session accessor", () => {
  it("throws SandboxNotReadyError before start(), not a generic error", async () => {
    const sandbox = new InisSandbox();
    expect(() => sandbox.session).toThrow(SandboxNotReadyError);
  });

  it("is never registered as a model tool -- createWorkspaceTools() never surfaces the session accessor", async () => {
    const raw = new FakeSession("sess_accessor_1");
    const sandbox = new InisSandbox({ session: asSession(raw) });
    const workspace = new Workspace({ sandbox });
    const tools = await createWorkspaceTools(workspace);
    // Real framework introspection, not an assumption: confirm the actual
    // set of tools Mastra generated for this sandbox has nothing resembling
    // a "session" tool -- the accessor is host-side only, reached via
    // `sandbox.session` directly, never through the model-facing tool layer.
    const toolIds = Object.keys(tools);
    expect(toolIds.length).toBeGreaterThan(0);
    expect(toolIds.some((id) => id.toLowerCase().includes("session"))).toBe(false);
  });
});

describe("InisSandbox — writeFiles", () => {
  it("bulk-writes real files via the batch endpoint", async () => {
    const raw = new FakeSession("sess_write_1");
    const sandbox = new InisSandbox({ session: asSession(raw) });
    await sandbox.start();
    await sandbox.writeFiles([{ path: "/workspace/a.txt", content: "hello" }]);
    expect(raw.readFileText("/workspace/a.txt")).toBe("hello");
  });

  it("a Buffer input is base64-encoded, and a failed file surfaces a real error, not a silent partial success", async () => {
    const raw = new FakeSession("sess_write_2");
    const sandbox = new InisSandbox({ session: asSession(raw) });
    await sandbox.start();
    await expect(
      sandbox.writeFiles([
        { path: "/workspace/b.bin", content: Buffer.from([1, 2, 3]) },
        { path: "/etc/passwd", content: "nope" },
      ]),
    ).rejects.toThrow(/1\/2/);
    expect(raw.readFileText("/workspace/b.bin")).toBe(Buffer.from([1, 2, 3]).toString("utf-8"));
  });
});

describe("InisSandbox — networking", () => {
  it("getPortUrl() is real, via session.expose(), not a guessed URL pattern", async () => {
    const raw = new FakeSession("sess_net_1");
    const sandbox = new InisSandbox({ session: asSession(raw) });
    await sandbox.start();
    const url = await sandbox.networking.getPortUrl(8080);
    expect(url).toBe("https://sess_net_1-8080.preview.inis.test");
  });

  it("getPortUrl() returns null (not a throw) before the sandbox has a session", async () => {
    const sandbox = new InisSandbox();
    const url = await sandbox.networking.getPortUrl(8080);
    expect(url).toBeNull();
  });
});

describe("InisSandbox — getInfo", () => {
  it("reflects real session resource fields", async () => {
    const raw = new FakeSession("sess_info_1");
    const sandbox = new InisSandbox({ session: asSession(raw) });
    await sandbox.start();
    const info = await sandbox.getInfo();
    expect(info.provider).toBe("inis");
    expect(info.resources?.memoryMB).toBe(1024);
    expect(info.resources?.cpuCores).toBe(1);
    expect(info.metadata).toMatchObject({ inisSessionId: "sess_info_1", owned: false });
  });

  it("surfaces the session's redacted connections in metadata", async () => {
    const raw = new FakeSession("sess_info_connections");
    raw.connections = [
      {
        name: "stripe",
        origin: "https://api.stripe.com",
        state: "active",
        allow: { methods: ["GET"], paths: ["/v1/customers"] },
      },
    ];
    const sandbox = new InisSandbox({ session: asSession(raw) });
    await sandbox.start();
    const info = await sandbox.getInfo();
    expect(info.metadata?.connections).toEqual(raw.connections);
  });
});

describe("InisSandbox — clone()", () => {
  it("clone() always produces an owned sibling, ignoring this instance's attached session", async () => {
    const rawOriginal = new FakeSession("sess_clone_src");
    const rawClone = new FakeSession("sess_clone_new");
    const fakeClient = {
      sessions: {
        attach: () => asSession(rawOriginal),
        create: async () => asSession(rawClone),
      },
    } as never;

    const sandbox = new InisSandbox({ session: "sess_clone_src", client: fakeClient, egressDefault: "deny" });
    await sandbox.start();

    const sibling = sandbox.clone();
    await sibling.start();

    // The clone is owned: it went through sessions.create(), producing a
    // distinct session, not sessions.attach() reusing the original's.
    expect(sibling.session.sessionId).toBe("sess_clone_new");
    expect(sibling.session).not.toBe(sandbox.session);

    // Destroying the clone (owned) really destroys its own session, but
    // never touches the original attached one -- proves clone() didn't
    // silently keep them pointed at the same underlying session.
    await sibling.destroy();
    expect(rawClone.destroyed).toBe(true);
    expect(rawOriginal.destroyed).toBe(false);
  });
});

describe("InisSandbox — two independently-attached sandboxes stay isolated", () => {
  it("spawning on one never touches the other's process list", async () => {
    const rawA = new FakeSession("sess_iso_a");
    const rawB = new FakeSession("sess_iso_b");
    const sandboxA = new InisSandbox({ session: asSession(rawA) });
    const sandboxB = new InisSandbox({ session: asSession(rawB) });
    await Promise.all([sandboxA.start(), sandboxB.start()]);

    await Promise.all([
      sandboxA.processes.spawn("echo only-on-a").then((h) => h.wait()),
      sandboxB.processes.spawn("echo only-on-b").then((h) => h.wait()),
    ]);

    const listA = await sandboxA.processes.list();
    const listB = await sandboxB.processes.list();
    expect(listA.some((p) => p.command?.includes("only-on-a"))).toBe(true);
    expect(listA.some((p) => p.command?.includes("only-on-b"))).toBe(false);
    expect(listB.some((p) => p.command?.includes("only-on-b"))).toBe(true);
    expect(listB.some((p) => p.command?.includes("only-on-a"))).toBe(false);
  });
});

describe("InisSandbox — mount/unmount fail loudly rather than silently no-op", () => {
  // The actual installed @mastra/core@1.55.0 bundle shows MountManager and
  // Workspace's own mounts wiring both gate on `if (sandbox.mount)`, and
  // MountNotSupportedError is never thrown anywhere in the framework's own
  // code -- it's meant for a sandbox's own mount() to throw. Leaving
  // mount/unmount off InisSandbox entirely meant `new Workspace({ sandbox,
  // mounts: {...} })` never threw, and `sandbox.mount?.(...)` silently
  // resolved to `undefined` -- a caller configuring mounts got no mounts
  // and no error. These tests prove that's fixed: mount()/unmount() are
  // real methods now, and they throw.

  it("mount() throws MountNotSupportedError rather than being undefined", async () => {
    const sandbox = new InisSandbox({ session: asSession(new FakeSession("sess_mount_1")) });
    await sandbox.start();

    // Before the fix this package shipped no mount() at all, so
    // `sandbox.mount` was `undefined` and `sandbox.mount?.(...)` resolved to
    // `undefined` without ever throwing. Assert the method itself exists...
    expect(typeof sandbox.mount).toBe("function");
    // ...and that calling it throws loudly, per the WorkspaceSandbox
    // interface's own documented contract for a non-supporting sandbox.
    await expect(sandbox.mount({} as WorkspaceFilesystem, "/data")).rejects.toBeInstanceOf(MountNotSupportedError);
  });

  it("unmount() throws MountNotSupportedError rather than being undefined", async () => {
    const sandbox = new InisSandbox({ session: asSession(new FakeSession("sess_mount_2")) });
    await sandbox.start();

    expect(typeof sandbox.unmount).toBe("function");
    await expect(sandbox.unmount("/data")).rejects.toBeInstanceOf(MountNotSupportedError);
  });

  it("implementing mount() makes Mastra construct a real MountManager, closing the silent-no-op gap", async () => {
    const sandbox = new InisSandbox({ session: asSession(new FakeSession("sess_mount_3")) });
    // Constructing a Workspace with a mounts config never throws by itself
    // (that part of the original PR's claim was never in dispute) -- what
    // was missing is a real MountManager to even record the attempt.
    const workspace = new Workspace({ sandbox, mounts: { "/data": {} as WorkspaceFilesystem } });
    expect(workspace.sandbox).toBe(sandbox);
    // Only constructed at all because InisSandbox now implements mount() --
    // this was `undefined` before the fix, which is exactly how a caller's
    // configured mount silently vanished with no error anywhere.
    expect(sandbox.mounts).toBeDefined();
  });
});
