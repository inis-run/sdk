/**
 * A local, in-process fake of the inis.run session backend for vitest —
 * the TypeScript twin of ../../python/tests/fake_backend.py. Patches
 * `Session`'s static `create` factory and its instance methods directly
 * (the same technique sdk/typescript/src/client.test.ts uses when it
 * stubs global `fetch`, one level lower) so the example scripts' own
 * functions run for real against an in-memory registry — no network call,
 * no live inis.run session.
 */

import { vi } from "vitest";
import { Client, ForkResult, InisError, ProcessInfo, Session, SessionInfo } from "@inis-run/sdk";

const HARD_MAX_FORK_COUNT = 16; // mirrors the server's hard fork-count cap

export interface FakeSessionState {
  sessionId: string;
  state: string;
  files: Map<string, string>;
  processes: Map<string, ProcessInfo>;
  tickingPaths: Set<string>;
  labels?: Record<string, string>;
  destroyed: boolean;
}

export class FakeBackend {
  byId = new Map<string, FakeSessionState>();
  private counter = 0;

  newId(): string {
    this.counter += 1;
    return `ses_fake${String(this.counter).padStart(4, "0")}`;
  }

  create(labels?: Record<string, string>): FakeSessionState {
    const rec: FakeSessionState = {
      sessionId: this.newId(),
      state: "live",
      files: new Map(),
      processes: new Map(),
      tickingPaths: new Set(),
      labels,
      destroyed: false,
    };
    this.byId.set(rec.sessionId, rec);
    return rec;
  }

  get(sessionId: string): FakeSessionState {
    const rec = this.byId.get(sessionId);
    if (!rec || rec.destroyed) {
      throw new InisError("404: session not found", { code: "not_found", status: 404, retryable: false });
    }
    return rec;
  }
}

/** Installs fake implementations of every Session/Client method the
 * example scripts call. Returns the backend so a test can inspect it.
 * Call this from a `beforeEach` (see tests/harness.test.ts) — vitest
 * restores all spies afterward via `restoreMocks`/`vi.restoreAllMocks()`. */
export function installFakeBackend(): FakeBackend {
  const backend = new FakeBackend();

  vi.spyOn(Session, "create").mockImplementation(async (client: Client, opts?: any) => {
    const rec = backend.create(opts?.labels);
    return Session.attach(client, rec.sessionId);
  });

  vi.spyOn(Client.prototype as any, "_getSession").mockImplementation((async (sessionId: string) => {
    const rec = backend.get(sessionId);
    return { sessionId: rec.sessionId, state: rec.state, labels: rec.labels } as SessionInfo;
  }) as any);

  vi.spyOn(Session.prototype, "exec").mockImplementation(async function (this: Session, command: any, opts?: any) {
    const rec = backend.get(this.sessionId);
    const argv = Array.isArray(command) ? command : ["bash", "-lc", command];
    if (argv[0] === "cat") {
      const path = argv[1];
      if (!rec.files.has(path)) {
        return { stdout: "", stderr: `cat: ${path}: No such file or directory\n`, exitCode: 1, durationMs: 1, timedOut: false } as any;
      }
      return { stdout: rec.files.get(path)!, stderr: "", exitCode: 0, durationMs: 1, timedOut: false } as any;
    }
    if (argv[0] === "sleep" && argv.length === 2) {
      const seconds = Number(argv[1]);
      const timedOut = opts?.timeoutMs !== undefined && opts.timeoutMs < seconds * 1000;
      return { stdout: "", stderr: "", exitCode: 0, durationMs: Math.round(opts?.timeoutMs ?? seconds * 1000), timedOut } as any;
    }
    if (argv[0] === "bash" && argv[1] === "-lc") {
      // branch-and-evaluate.ts's candidates: "sleep 2; python3
      // /workspace/<name>.py", where that file is a one-line Python
      // formula reading /workspace/n.txt. This fake can't run Python, so
      // it pattern-matches the two known candidate shapes (correct sum
      // vs. the deliberately off-by-one one) and computes their real
      // arithmetic result directly, rather than faking a fixed answer.
      const script = argv[2] as string;
      const match = /python3 (\/workspace\/\S+\.py)/.exec(script);
      if (match) {
        const code = rec.files.get(match[1]!) ?? "";
        const n = Number(rec.files.get("/workspace/n.txt") ?? "0");
        const isCorrectFormula = /range\(1,\s*n\s*\+\s*1\)/.test(code);
        const total = isCorrectFormula ? (n * (n + 1)) / 2 : ((n - 1) * n) / 2;
        return { stdout: `${total}\n`, stderr: "", exitCode: 0, durationMs: 1, timedOut: false } as any;
      }
    }
    return { stdout: "", stderr: "", exitCode: 0, durationMs: 1, timedOut: false } as any;
  });

  vi.spyOn(Session.prototype, "writeFile").mockImplementation(async function (this: Session, path: string, content: any) {
    const rec = backend.get(this.sessionId);
    rec.files.set(path, typeof content === "string" ? content : new TextDecoder().decode(content));
  });

  vi.spyOn(Session.prototype, "readFile").mockImplementation(async function (this: Session, path: string) {
    const rec = backend.get(this.sessionId);
    if (!rec.files.has(path)) {
      throw new InisError(`404: ${path} not found`, { code: "not_found", status: 404, retryable: false });
    }
    if (rec.tickingPaths.has(path)) {
      rec.files.set(path, String(Number(rec.files.get(path)) + 1));
    }
    return rec.files.get(path)!;
  } as any);

  vi.spyOn(Session.prototype, "listFiles").mockImplementation(async function (this: Session, path: string = "/workspace") {
    const rec = backend.get(this.sessionId);
    const prefix = path.replace(/\/$/, "") + "/";
    const names = new Set<string>();
    for (const p of rec.files.keys()) {
      if (p.startsWith(prefix)) names.add(p.slice(prefix.length).split("/")[0]!);
    }
    return [...names].sort();
  });

  vi.spyOn(Session.prototype, "get").mockImplementation(async function (this: Session) {
    const rec = backend.get(this.sessionId);
    return { sessionId: rec.sessionId, state: rec.state, labels: rec.labels } as SessionInfo;
  });

  vi.spyOn(Session.prototype, "pause").mockImplementation(async function (this: Session) {
    const rec = backend.get(this.sessionId);
    rec.state = "paused";
    return { sessionId: rec.sessionId, state: rec.state, labels: rec.labels } as SessionInfo;
  });

  vi.spyOn(Session.prototype, "resume").mockImplementation(async function (this: Session) {
    const rec = backend.get(this.sessionId);
    rec.state = "live";
    return { sessionId: rec.sessionId, state: rec.state, labels: rec.labels } as SessionInfo;
  });

  vi.spyOn(Session.prototype, "destroy").mockImplementation(async function (this: Session) {
    const rec = backend.byId.get(this.sessionId);
    if (rec) rec.destroyed = true;
  });

  vi.spyOn(Session.prototype, "fork").mockImplementation(async function (this: Session, count: number) {
    const rec = backend.get(this.sessionId);
    if (count > HARD_MAX_FORK_COUNT) {
      throw new InisError(`400: count exceeds hard cap of ${HARD_MAX_FORK_COUNT}`, { code: "bad_request", status: 400, retryable: false });
    }
    const children: string[] = [];
    for (let i = 0; i < count; i++) {
      const child = backend.create(rec.labels);
      child.files = new Map(rec.files);
      children.push(child.sessionId);
    }
    return { parentSessionId: rec.sessionId, children } as ForkResult;
  });

  vi.spyOn(Session.prototype, "startProcess").mockImplementation(async function (this: Session, name: string, command: any, opts?: any) {
    const rec = backend.get(this.sessionId);
    const info = { name, state: "running", keepAlive: !!opts?.keepAlive } as ProcessInfo;
    rec.processes.set(name, info);
    const script = Array.isArray(command) ? command.join(" ") : command;
    const match = /-?>\s*(\S+)/.exec(script);
    if (match) {
      const path = match[1]!.replace(/;+$/, "");
      if (!rec.files.has(path)) rec.files.set(path, "0");
      rec.tickingPaths.add(path);
    }
    return info;
  });

  vi.spyOn(Session.prototype, "getProcess").mockImplementation(async function (this: Session, name: string) {
    const rec = backend.get(this.sessionId);
    const info = rec.processes.get(name);
    if (!info) throw new InisError(`404: process ${name} not found`, { code: "not_found", status: 404, retryable: false });
    return info;
  });

  vi.spyOn(Session.prototype, "killProcess").mockImplementation(async function (this: Session, name: string) {
    const rec = backend.get(this.sessionId);
    const info = rec.processes.get(name);
    if (!info) throw new InisError(`404: process ${name} not found`, { code: "not_found", status: 404, retryable: false });
    info.state = "exited";
    return info;
  });

  vi.spyOn(Session.prototype, "listProcesses").mockImplementation(async function (this: Session) {
    const rec = backend.get(this.sessionId);
    return [...rec.processes.values()];
  });

  return backend;
}
