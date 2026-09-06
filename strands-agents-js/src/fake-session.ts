/**
 * A real subprocess + real temp directory standing in for one `inis.Session`,
 * for deterministic tests of `InisSandbox`.
 *
 * Fakes at the `inis.Session` object boundary (mirroring
 * `sdk/openai-agents-js/src/sandbox.test.ts` and `sdk/mastra/src/fake-session.ts`'s
 * approach) rather than the HTTP transport underneath a real client -- every
 * line of `InisSandbox`'s own code runs for real, only "the session lives on
 * this machine instead of inis.run's infrastructure" is faked. Unlike those
 * two siblings' purely-scripted command matching, this fake actually spawns
 * `bash -lc <command>` via `node:child_process` and serves file ops from a
 * real temp directory, because `Sandbox.executeStreaming`'s stdout/stderr
 * ordering, exit codes, and cancellation behavior are exactly what this
 * adapter's own logic needs to be proven against -- a scripted "echo" match
 * would not exercise any of that.
 */

import { spawn, type ChildProcessWithoutNullStreams } from "node:child_process";
import { mkdtempSync, rmSync, mkdirSync, readdirSync, statSync, readFileSync, writeFileSync, existsSync, unlinkSync, rmdirSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, dirname } from "node:path";
import { InisError, type ExecStreamEvent, type Session } from "@inis-run/sdk";

export class FakeSession {
  sessionId: string;
  destroyed = false;
  readonly workspaceRoot: string;
  readonly tmpRoot: string;
  lastChild: ChildProcessWithoutNullStreams | undefined;

  constructor(sessionId: string, workspaceRoot = "/workspace") {
    this.sessionId = sessionId;
    this.workspaceRoot = workspaceRoot;
    this.tmpRoot = mkdtempSync(join(tmpdir(), "inis-strands-js-test-"));
    mkdirSync(this._realPath(workspaceRoot), { recursive: true });
  }

  cleanup(): void {
    rmSync(this.tmpRoot, { recursive: true, force: true });
  }

  private _underWorkspaceRoot(path: string): boolean {
    const prefix = this.workspaceRoot.replace(/\/+$/, "") + "/";
    return path === this.workspaceRoot || path.startsWith(prefix);
  }

  private _realPath(path: string): string {
    if (this._underWorkspaceRoot(path)) {
      return join(this.tmpRoot, path.replace(/^\/+/, ""));
    }
    return path;
  }

  async destroy(): Promise<void> {
    this.destroyed = true;
  }

  async *execStream(
    command: string | string[],
    opts?: { cwd?: string; timeoutMs?: number },
  ): AsyncGenerator<ExecStreamEvent> {
    const argv = typeof command === "string" ? ["bash", "-lc", command] : command;
    const cwd = opts?.cwd ? this._realPath(opts.cwd) : this._realPath(this.workspaceRoot);

    const child = spawn(argv[0]!, argv.slice(1), { cwd });
    this.lastChild = child;

    type QueueItem =
      | { stream: "stdout" | "stderr"; data: string }
      | { stream: "exit"; exitCode: number; timedOut: boolean }
      | undefined;
    const queue: QueueItem[] = [];
    const waiters: Array<(item: QueueItem) => void> = [];

    function push(item: QueueItem): void {
      const waiter = waiters.shift();
      if (waiter) waiter(item);
      else queue.push(item);
    }

    function next(): Promise<QueueItem> {
      if (queue.length > 0) return Promise.resolve(queue.shift());
      return new Promise((resolve) => waiters.push(resolve));
    }

    let timedOut = false;
    const timer = opts?.timeoutMs
      ? setTimeout(() => {
          timedOut = true;
          child.kill("SIGKILL");
        }, opts.timeoutMs)
      : undefined;

    child.stdout.on("data", (chunk: Buffer) => push({ stream: "stdout", data: chunk.toString("utf-8") }));
    child.stderr.on("data", (chunk: Buffer) => push({ stream: "stderr", data: chunk.toString("utf-8") }));
    let doneCount = 0;
    child.on("close", (code) => {
      if (timer) clearTimeout(timer);
      doneCount++;
      push({ stream: "exit", exitCode: code ?? 0, timedOut });
      push(undefined);
    });

    try {
      while (true) {
        const item = await next();
        if (item === undefined) return;
        if (item.stream === "exit") {
          yield { stream: "exit", data: "", exitCode: item.exitCode, timedOut: item.timedOut, durationMs: 1 };
        } else {
          yield { stream: item.stream, data: item.data };
        }
      }
    } finally {
      if (timer) clearTimeout(timer);
      if (doneCount === 0 && child.exitCode === null && !child.killed) {
        child.kill("SIGKILL");
      }
    }
  }

  async readFile(path: string, encoding?: "text" | "base64"): Promise<string | Uint8Array> {
    const real = this._realPath(path);
    if (!existsSync(real) || statSync(real).isDirectory()) {
      throw new InisError(`open ${path}: no such file or directory`, { code: "bad_request", status: 404 });
    }
    const data = readFileSync(real);
    if (encoding === "base64") return new Uint8Array(data);
    return data.toString("utf-8");
  }

  async writeFile(path: string, content: string | Uint8Array): Promise<void> {
    const real = this._realPath(path);
    mkdirSync(dirname(real), { recursive: true });
    if (content instanceof Uint8Array) {
      writeFileSync(real, Buffer.from(content));
    } else {
      writeFileSync(real, content, "utf-8");
    }
  }

  async remove(path: string): Promise<void> {
    const real = this._realPath(path);
    if (!existsSync(real)) {
      throw new InisError(`remove ${path}: no such file or directory`, { code: "bad_request", status: 404 });
    }
    if (statSync(real).isDirectory()) {
      rmdirSync(real, { recursive: true } as never);
    } else {
      unlinkSync(real);
    }
  }

  async listFiles(path: string): Promise<string[]> {
    const real = this._realPath(path);
    if (!existsSync(real) || !statSync(real).isDirectory()) {
      throw new InisError(`open ${path}: no such file or directory`, { code: "bad_request", status: 404 });
    }
    // Mirrors guest/agent/files.go's listDirOp: trailing "/" marks a directory.
    return readdirSync(real)
      .sort()
      .map((name) => (statSync(join(real, name)).isDirectory() ? `${name}/` : name));
  }
}

export function asSession(fake: FakeSession): Session {
  return fake as unknown as Session;
}
