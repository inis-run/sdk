/**
 * Shared in-memory `inis.Session` double for this package's tests.
 *
 * Mirrors `sdk/openai-agents-js/src/sandbox.test.ts`'s approach: fakes at
 * the `inis.Session` object boundary (not the HTTP transport underneath a
 * real client), so every line of `InisSandbox`/`InisProcessManager`'s own
 * code runs for real — only the network call inside each `Session` method
 * is stubbed. Named-process behaviour is scripted by command text:
 * - `echo <text>` exits immediately (code 0) with `<text>\n` on stdout.
 * - a command containing `long-running` never exits on its own — it only
 *   stops when `killProcess()` is called (or the test calls `finishProcess`
 *   directly) — this is what models a genuine background process for
 *   kill/timeout tests.
 * - anything else exits immediately with code 0 and empty output.
 */
import { InisError, type ExposeResult, type ProcessInfo, type Session, type SessionInfo } from "@inis-run/sdk";

// See sandbox.ts's identical comment: `FileBatchItem` is a real exported
// interface in sdk/typescript/src/client.ts but isn't re-exported from its
// curated top-level index.ts, so it's derived structurally here too.
type FileBatchItem = Parameters<Session["writeFiles"]>[0][number];

/**
 * A real `InisError` (not just an object shaped like one) — the adapter
 * code's `e instanceof InisError` checks (`isNotFound()` in
 * process-manager.ts, `decodeInisError()` in sandbox.ts) only narrow on the
 * real class, matching what a real 404 response actually throws.
 */
function notFound(message: string): InisError {
  return new InisError(message, { code: "not_found", status: 404, retryable: false });
}

interface FakeProcess {
  name: string;
  command: string[];
  state: "running" | "exited";
  exitCode?: number;
  stdout: string;
}

export class FakeSession {
  sessionId: string;
  state: "live" | "paused" | "ended" = "live";
  destroyed = false;
  pauseCalls = 0;
  resumeCalls = 0;
  destroyCalls = 0;
  private files = new Map<string, string>();
  private processes = new Map<string, FakeProcess>();
  private processSeq = 0;
  private startProcessLog: Array<{ name: string; command: string[] }> = [];

  constructor(sessionId: string) {
    this.sessionId = sessionId;
  }

  async get(): Promise<Partial<SessionInfo> & { sessionId: string; state: string }> {
    if (this.state === "ended") {
      const err = notFound(`session ${this.sessionId} not found`);
      throw err;
    }
    return { sessionId: this.sessionId, state: this.state, lastActiveAt: new Date().toISOString(), memMb: 1024, vcpus: 1 };
  }

  async pause(): Promise<void> {
    this.pauseCalls++;
    this.state = "paused";
  }

  async resume(): Promise<void> {
    this.resumeCalls++;
    this.state = "live";
  }

  async destroy(): Promise<void> {
    this.destroyCalls++;
    this.destroyed = true;
    this.state = "ended";
  }

  async expose(port: number, _opts?: unknown): Promise<ExposeResult> {
    return {
      sessionId: this.sessionId,
      port,
      previewUrl: `https://${this.sessionId}-${port}.preview.inis.test`,
    };
  }

  async writeFiles(items: FileBatchItem[]): Promise<Array<{ path: string; ok: boolean; error?: string }>> {
    return items.map((item) => {
      if (!item.path.startsWith("/workspace")) {
        return { path: item.path, ok: false, error: "path must be under /workspace" };
      }
      const content = item.encoding === "base64" ? Buffer.from(item.content, "base64").toString("utf-8") : item.content;
      this.files.set(item.path, content);
      return { path: item.path, ok: true };
    });
  }

  readFileText(path: string): string | undefined {
    return this.files.get(path);
  }

  async startProcess(name: string, command: string | string[]): Promise<ProcessInfo> {
    const argv = typeof command === "string" ? ["bash", "-lc", command] : command;
    this.startProcessLog.push({ name, command: argv });
    const scriptLine = argv[argv.length - 1] ?? "";
    const echoMatch = /^echo (.*)$/.exec(scriptLine);
    const proc: FakeProcess = { name, command: argv, state: "running", stdout: "" };
    if (scriptLine.includes("long-running")) {
      // Stays running until finishProcess()/killProcess() is called.
    } else if (echoMatch) {
      proc.state = "exited";
      proc.exitCode = 0;
      proc.stdout = `${echoMatch[1]}\n`;
    } else {
      proc.state = "exited";
      proc.exitCode = 0;
    }
    this.processes.set(name, proc);
    this.processSeq++;
    return this.toInfo(proc);
  }

  /** Test helper: mark a `long-running` process finished, as if it exited on its own. */
  finishProcess(name: string, exitCode = 0): void {
    const proc = this.processes.get(name);
    if (!proc) throw notFound(`process ${JSON.stringify(name)} not found`);
    proc.state = "exited";
    proc.exitCode = exitCode;
  }

  async listProcesses(): Promise<ProcessInfo[]> {
    return Array.from(this.processes.values()).map((p) => this.toInfo(p));
  }

  async getProcess(name: string): Promise<ProcessInfo> {
    const proc = this.processes.get(name);
    if (!proc) throw notFound(`process ${JSON.stringify(name)} not found`);
    return this.toInfo(proc);
  }

  async killProcess(name: string): Promise<ProcessInfo> {
    const proc = this.processes.get(name);
    if (!proc) throw notFound(`process ${JSON.stringify(name)} not found`);
    if (proc.state === "running") {
      proc.state = "exited";
      proc.exitCode = 143; // SIGTERM
    }
    return this.toInfo(proc);
  }

  async *streamProcessLogs(name: string): AsyncGenerator<{ event: "stdout" | "stderr" | "eof" | "error"; data: string }> {
    const proc = this.processes.get(name);
    if (!proc) throw notFound(`process ${JSON.stringify(name)} not found`);
    if (proc.stdout) yield { event: "stdout", data: proc.stdout };
    const deadline = Date.now() + 2000;
    while (proc.state === "running" && Date.now() < deadline) {
      await new Promise((resolve) => setTimeout(resolve, 5));
    }
    yield { event: "eof", data: "" };
  }

  getStartProcessLog(): Array<{ name: string; command: string[] }> {
    return this.startProcessLog;
  }

  private toInfo(p: FakeProcess): ProcessInfo {
    return { name: p.name, state: p.state, command: p.command, exitCode: p.exitCode };
  }
}

export function asSession(fake: FakeSession): Session {
  return fake as unknown as Session;
}
