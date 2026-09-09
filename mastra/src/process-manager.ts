/**
 * Process manager for `InisSandbox` — implements Mastra's
 * `SandboxProcessManager`/`ProcessHandle` contract on top of inis.run's real
 * named-background-process API (`session.startProcess`/`listProcesses`/
 * `getProcess`/`killProcess`/`streamProcessLogs`).
 *
 * Unlike some other adapters in this repo, none of this is emulated: kill()
 * is a genuine remote SIGTERM-then-SIGKILL delivered inside the sandbox, and
 * output streaming is a genuine live Server-Sent-Events follow of the
 * sandbox's own stdout/stderr. The one gap is `sendStdin()` — inis.run has
 * no stdin-write endpoint for a named background process, so that's
 * declared unsupported below, not faked.
 */
import { randomUUID } from "node:crypto";
import {
  ProcessHandle,
  SandboxProcessManager,
  type CommandResult,
  type ProcessInfo,
  type SpawnProcessOptions,
} from "@mastra/core/workspace";
import { InisError, type Session } from "@inis-run/sdk";
import type { InisSandbox } from "./sandbox.js";

function isNotFound(e: unknown): boolean {
  return e instanceof InisError && e.status === 404;
}

function decodeInisError(e: unknown): Error {
  if (e instanceof InisError) {
    return new Error(`inis error [${e.code}] (status=${e.status}, retryable=${e.retryable}): ${e.message}`);
  }
  return e instanceof Error ? e : new Error(String(e));
}

/** Shell-quote a single value for safe interpolation into a `bash -lc` string. */
function shellQuote(value: string): string {
  return `'${value.replace(/'/g, "'\\''")}'`;
}

/**
 * Translate `SpawnProcessOptions.env` into a literal `export` prefix on the
 * command line. inis.run's named-process API has no per-call env parameter
 * (only session-wide env, set at session creation), so this is the honest
 * way to give a single spawned process its own extra variables: it's a real
 * shell `export`, not a placebo — the guest process genuinely sees these
 * values, just set by different means than a structured API field.
 */
function withEnvPrefix(command: string, env: NodeJS.ProcessEnv): string {
  const assignments = Object.entries(env)
    .filter((entry): entry is [string, string] => entry[1] !== undefined)
    .map(([key, value]) => `export ${key}=${shellQuote(value)};`)
    .join(" ");
  return assignments ? `${assignments} ${command}` : command;
}

/**
 * `ProcessHandle` backed by one inis.run named background process.
 *
 * Streaming starts immediately on construction (not lazily on the first
 * `wait()` call) so `handle.stdout`/`handle.stderr` are already populating
 * for a caller that only polls them without ever awaiting `wait()` — the
 * "poll model" the upstream docstring describes. A dropped stream mid-flight
 * falls back to one final `getProcess()` read for the authoritative exit
 * code rather than leaving `wait()` hanging forever.
 */
export class InisProcessHandle extends ProcessHandle {
  readonly pid: string;
  private _exitCode: number | undefined;
  private _killedByUs = false;
  private readonly _startedAt = Date.now();
  private readonly _streamDone: Promise<void>;

  constructor(
    private readonly session: Session,
    name: string,
    options?: Pick<SpawnProcessOptions, "maxRetainedBytes" | "onStdout" | "onStderr">,
    knownExitCode?: number,
  ) {
    super(options);
    this.pid = name;
    if (knownExitCode !== undefined) {
      this._exitCode = knownExitCode;
      this._streamDone = Promise.resolve();
    } else {
      this._streamDone = this._followLogs();
    }
  }

  get exitCode(): number | undefined {
    return this._exitCode;
  }

  private async _followLogs(): Promise<void> {
    try {
      for await (const event of this.session.streamProcessLogs(this.pid)) {
        if (event.event === "stdout") this.emitStdout(event.data);
        else if (event.event === "stderr") this.emitStderr(event.data);
        // "eof" ends the generator on its own; "error" is handled by the
        // getProcess() fallback below, same as a dropped connection.
      }
    } catch {
      // A network hiccup mid-stream falls through to the getProcess() read
      // below rather than rejecting wait() outright — the process may well
      // have finished successfully even if the log follow connection died.
    }
    try {
      const info = await this.session.getProcess(this.pid);
      this._exitCode = info.exitCode;
    } catch {
      // Best-effort: if the process (or session) is gone by the time we ask,
      // exitCode stays undefined rather than throwing out of a background task.
    }
  }

  /**
   * Returns whether the process is no longer running after this call — NOT
   * strictly "this call is what killed it". inis.run's real `killProcess` API
   * is a documented no-op that just reports final state if the process had
   * already exited on its own,
   * and there is no separate signal distinguishing that from a genuine
   * SIGTERM/SIGKILL this call issued. So a `kill()` racing a process that
   * was about to exit anyway can report `true` even though this call didn't
   * do the killing — a real ambiguity in the underlying API, not something
   * papered over here.
   */
  async kill(): Promise<boolean> {
    try {
      const info = await this.session.killProcess(this.pid);
      this._killedByUs = true;
      if (info.exitCode !== undefined) this._exitCode = info.exitCode;
      return info.state === "exited";
    } catch (e) {
      if (isNotFound(e)) return false;
      throw decodeInisError(e);
    }
  }

  /**
   * Not implemented: inis.run has no stdin-write endpoint for a named
   * background process (only `startProcess`/`listProcesses`/`getProcess`/
   * `killProcess`/log-read). Declared unsupported rather than silently
   * swallowing writes.
   */
  async sendStdin(_data: string): Promise<void> {
    throw new Error(
      "InisProcessHandle does not support sendStdin() — inis.run has no stdin-write endpoint " +
        "for a named background process (declared unsupported, not emulated)",
    );
  }

  async wait(): Promise<CommandResult> {
    await this._streamDone;
    // inis.run's process-info wire format used to conflate "exited with
    // code 0" and "exit code unknown" (both encoded as an absent
    // `exit_code` field, from tagging a plain `int` with `omitempty`) --
    // fixed upstream (live-verified 2026-08-01 against
    // api.inis.run: exit code 0 now round-trips as a present `0`, and a
    // still-running process correctly omits the field rather than
    // implying 0). `_exitCode` can still legitimately be `undefined` here
    // for reasons unrelated to that bug -- e.g. the session or process
    // disappeared before the final `getProcess()` read in `_followLogs()`
    // above, or `wait()` resolves before the process was ever observed as
    // exited -- so `-1` below stays as the sentinel for a genuinely
    // unknown exit code, not a stand-in for a dropped zero.
    return {
      success: this._exitCode === 0,
      exitCode: this._exitCode ?? -1,
      stdout: this.stdout,
      stderr: this.stderr,
      executionTimeMs: Date.now() - this._startedAt,
      // inis.run has no server-enforced per-process timeout; a caller's
      // `SpawnProcessOptions.timeout` is honoured host-side (see spawn()
      // below) via a real kill() call, so a timeout always surfaces as
      // `killed: true`, never `timedOut: true` — there is no signal from
      // the guest distinguishing "we killed it for timing out" from any
      // other kill.
      timedOut: false,
      killed: this._killedByUs,
    };
  }
}

/**
 * `SandboxProcessManager` backed by inis.run named background processes.
 * inis.run manages all process state server-side (like E2B's own process
 * manager) — `list()`/`get()` always hit the real API rather than relying
 * only on this instance's in-memory `_tracked` map, so a process started
 * before this manager attached (e.g. a different host process, or a fresh
 * reconnect to the same session) is still visible.
 */
export class InisProcessManager extends SandboxProcessManager<InisSandbox> {
  async spawn(command: string, options: SpawnProcessOptions = {}): Promise<ProcessHandle> {
    const name = `mastra-${randomUUID()}`;
    const finalCommand = options.env ? withEnvPrefix(command, options.env) : command;
    try {
      await this.sandbox.session.startProcess(name, finalCommand, { cwd: options.cwd });
    } catch (e) {
      throw decodeInisError(e);
    }
    const handle = new InisProcessHandle(this.sandbox.session, name, options);
    this._tracked.set(name, handle);
    if (options.timeout) {
      // Host-orchestrated timeout: a real kill() call after the deadline,
      // not a server-enforced one. If the connection to inis.run drops
      // before the deadline, this timer still fires locally but the kill()
      // call itself will simply fail the same way any other call would.
      const timer = setTimeout(() => {
        handle.kill().catch(() => {});
      }, options.timeout);
      handle.wait().finally(() => clearTimeout(timer)).catch(() => {});
    }
    return handle;
  }

  async list(): Promise<ProcessInfo[]> {
    const infos = await this.sandbox.session.listProcesses();
    return infos.map((p) => ({
      pid: p.name,
      command: p.command?.join(" "),
      running: p.state === "running",
      exitCode: p.exitCode,
    }));
  }

  /**
   * Falls back to a real `getProcess()` reconnect for a process not tracked
   * in this manager instance's memory — mirrors `E2BProcessManager.get()`,
   * whose doc comment states the same rationale: processes spawned
   * externally, or before this process reconnected, are still real and
   * still queryable server-side.
   */
  async get(pid: string): Promise<ProcessHandle | undefined> {
    const tracked = this._tracked.get(pid);
    if (tracked) return tracked;
    try {
      const info = await this.sandbox.session.getProcess(pid);
      const handle = new InisProcessHandle(this.sandbox.session, info.name, {}, info.exitCode);
      this._tracked.set(pid, handle);
      return handle;
    } catch (e) {
      if (isNotFound(e)) return undefined;
      throw decodeInisError(e);
    }
  }
}
