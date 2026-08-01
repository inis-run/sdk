/**
 * Native inis.run sandbox provider for Strands Agents' `@strands-agents/sdk`
 * `Sandbox` surface.
 *
 * Verified against `@strands-agents/sdk==1.11.2` installed source, not docs
 * or blog posts. Two things worth recording explicitly:
 *
 * 1. Unlike the OpenAI Agents SDK's beta, internal-ABC `agents.sandbox`
 *    surface (see `inis-openai-agents`'s module docstring for that story),
 *    `@strands-agents/sdk`'s `Sandbox`/`PosixShellSandbox` classes are a
 *    stable, public, documented extension point re-exported straight from
 *    the package root -- `import { Sandbox, PosixShellSandbox } from
 *    "@strands-agents/sdk"` -- and `DockerSandbox`/`SshSandbox` (also in the
 *    same package) are built exactly the way this module is: subclass
 *    `PosixShellSandbox`, implement `executeStreaming`, override
 *    `getTools()` to vend `sandbox_bash`/`sandbox_file_editor` bound to the
 *    instance. This is the *identical* shape to the Python
 *    `inis-strands-agents` package (same upstream project, matching Python
 *    and TypeScript SDKs) -- see that package's `sandbox.py` module
 *    docstring for the fuller account of what's shared between the two.
 *
 * 2. `@strands-agents/sdk`'s public exports have no per-call `env` at all in
 *    either language: the wire protocol (`internal/protocol.ExecRequest` in
 *    the Go source) carries only `command`/`cwd`/`timeout_ms`. inis.run's
 *    own `env` is fixed at *session creation* and applied to every exec by
 *    the guest agent automatically. This module closes that gap the same
 *    way `SshSandbox` closes the identical gap for OpenSSH: a validated
 *    `export KEY=VALUE && ` shell prefix ahead of the command, since every
 *    command already runs through `bash -lc` on the guest.
 *
 * Also worth recording, found while writing this adapter's own tests, not
 * assumed: `Session.execStream` has no `AbortSignal`/cancellation-token
 * parameter of its own -- it awaits its underlying stream reader directly.
 * `ExecuteOptions.signal` is honored here by racing each pulled chunk
 * against the signal and calling the inner generator's `.return()` when it
 * fires, which does close the connection and kill the remote command (the
 * server treats an abandoned stream as a disconnect) -- **but only at a
 * chunk boundary**. Calling `.return()` on a JS async generator that is
 * already suspended mid-`await` (blocked on a real pending read, not paused
 * at a `yield`) does not preempt that specific wait -- per the language's
 * own async-generator semantics, the return request is queued and only
 * takes effect once that step naturally resolves. So a command that is
 * silent when the signal fires (no output yet, not exited) keeps running
 * until it next produces a chunk or exits on its own; abort takes effect
 * immediately once it does. This is a real, verified difference from the
 * Python `inis-strands-agents` package, where `asyncio` task cancellation
 * preempts an in-flight await at the event-loop level regardless of
 * silence -- see that package's own module docstring.
 *
 * Two ways to put an inis.run session behind a Strands `Agent`
 * -------------------------------------------------------------------------
 *
 * 1. Attached -- one inis.run session for the whole conversation
 *    (recommended). This adapter never creates or destroys the underlying
 *    session:
 *
 *        import { Client } from "@inis-run/sdk";
 *        import { Agent } from "@strands-agents/sdk";
 *        import { InisSandbox } from "@inis-run/strands-agents";
 *
 *        const client = new Client();
 *        const session = await client.sessions.create({ egress: { mode: "deny" } });
 *        const sandbox = InisSandbox.wrap(session);
 *
 *        const agent = new Agent({ model, sandbox });
 *        await agent.invoke("list files in /workspace");
 *        await session.destroy(); // this app owns the session; the adapter never will
 *
 * 2. Owned -- the adapter creates and destroys the session for you, for a
 *    single bounded task:
 *
 *        const sandbox = await InisSandbox.create(undefined, { egress: { mode: "deny" } });
 *        try {
 *          const agent = new Agent({ model, sandbox });
 *          await agent.invoke("...");
 *        } finally {
 *          await sandbox.close();
 *        }
 *
 * Create/destroy, egress, pause/resume, and fork stay host-controlled either
 * way: nothing here vends a tool that can create, destroy, or reconfigure a
 * session -- only `sandbox_bash`/`sandbox_file_editor`, matching every other
 * `Sandbox` backend (Docker/SSH never let the model create or destroy the
 * container/host either).
 *
 * Explicitly not implemented (declared, not faked)
 * -------------------------------------------------------------------------
 * - PTY: inis.run's PTY primitive is a separate product surface with no
 *   equivalent in the `Sandbox` shell/file contract.
 * - `ExecutionResult.outputFiles`: always `[]`, same as every other
 *   shell-based `Sandbox` -- there is no channel here for "this run produced
 *   these binary artifacts" distinct from stdout/stderr.
 */

import {
  type ExecuteOptions,
  type ExecutionResult,
  type FileInfo,
  PosixShellSandbox,
  SandboxAbortError,
  SandboxPathNotFoundError,
  SandboxTimeoutError,
  type StreamChunk,
  type Tool,
} from "@strands-agents/sdk";
import { makeFileEditor, DEFAULT_FILE_EDITOR_DESCRIPTION } from "@strands-agents/sdk/vended-tools/file-editor";
import { makeBash, SANDBOX_BASH_DESCRIPTION } from "@strands-agents/sdk/vended-tools/bash";
import { Client, type ExecStreamEvent, InisError, Session } from "@inis-run/sdk";

// `CreateSessionOptions` is a real exported interface in
// sdk/typescript/src/client.ts but isn't re-exported from its curated
// top-level index.ts (same situation `sdk/mastra/src/fake-session.ts`
// documents for `FileBatchItem`), so it's derived structurally here too.
type CreateSessionOptions = NonNullable<Parameters<Client["sessions"]["create"]>[0]>;

export const DEFAULT_WORKSPACE_ROOT = "/workspace";

// Same POSIX env-var name rule the core `inis.sandbox` machinery would use --
// kept local rather than importing `@strands-agents/sdk`'s own equivalent
// helpers (`validateEnvKeys`/`buildShellEnvPrefix` in `posix-shell.js`, not
// reachable through any subpath package.json actually exports) so this
// adapter never depends on a non-exported internal.
const ENV_KEY_PATTERN = /^[a-zA-Z_][a-zA-Z0-9_]*$/;

// Guest-side error text for a missing path, read directly from
// `guest/agent/files.go` -- Go's `os`/`syscall` errors stringify with these
// phrases verbatim, and the API layer forwards them unmodified with a
// generic `code="bad_request"` (inis.run's file-op HTTP responses do not
// carry a distinct machine-readable "not found" code today). Matched
// case-insensitively against whatever `InisError` surfaces.
const NOT_FOUND_MARKERS = ["no such file or directory", "not a directory"];

function shellQuote(value: string): string {
  return `'${value.replace(/'/g, `'\\''`)}'`;
}

function validateEnvKeys(env: Record<string, string>): void {
  for (const key of Object.keys(env)) {
    if (!ENV_KEY_PATTERN.test(key)) {
      throw new Error(`Invalid environment variable name: ${key}`);
    }
  }
}

/** Build a shell `export KEY=VALUE && ...` prefix, or `""` when empty.
 * inis.run's exec API has no per-call `env` field (see module docstring,
 * point 2) -- every command already runs as `bash -lc <command>` on the
 * guest, so exporting into that same shell invocation genuinely sets the
 * variables for this one command without touching the session's own
 * persistent env. */
function buildEnvPrefix(env?: Record<string, string>): string {
  if (!env || Object.keys(env).length === 0) return "";
  validateEnvKeys(env);
  const assignments = Object.entries(env)
    .map(([key, value]) => `${key}=${shellQuote(value)}`)
    .join(" ");
  return `export ${assignments} && `;
}

function looksLikeMissingPath(e: InisError): boolean {
  const text = e.message.toLowerCase();
  return NOT_FOUND_MARKERS.some((marker) => text.includes(marker));
}

export interface InisSandboxCreateOptions extends CreateSessionOptions {
  workspaceRoot?: string;
}

/**
 * `PosixShellSandbox` backed by one `inis.Session`.
 *
 * Construct via `InisSandbox.wrap()` (attach mode) or `InisSandbox.create()`
 * (owned mode) -- not directly. `executeStreaming` is the only shell
 * primitive implemented from scratch (via `Session.execStream`);
 * `executeCodeStreaming` is inherited from `PosixShellSandbox` unchanged (it
 * pipes base64-encoded source through `executeStreaming`, so it gets this
 * sandbox's env/timeout/cwd/signal handling for free). `readFile`/
 * `writeFile`/`removeFile`/`listFiles` are overridden to use inis.run's
 * native binary-safe `/files` endpoints instead of the default's
 * `base64`-over-shell fallback.
 */
export class InisSandbox extends PosixShellSandbox {
  readonly workspaceRoot: string;
  private readonly session: Session;
  private readonly owned: boolean;

  private constructor(session: Session, owned: boolean, workspaceRoot: string) {
    super();
    this.session = session;
    this.owned = owned;
    this.workspaceRoot = workspaceRoot;
  }

  /**
   * Wrap an inis.run session the host already created or attached to. This
   * wrapper never creates or destroys the underlying session -- call
   * `await session.destroy()` (the `inis.Session`, not this wrapper)
   * yourself when the conversation is over. `close()` on a wrapped sandbox
   * is a no-op.
   */
  static wrap(session: Session, opts?: { workspaceRoot?: string }): InisSandbox {
    if (!session.sessionId) {
      throw new Error("InisSandbox.wrap() requires an already-created/attached session (sessionId is unset)");
    }
    return new InisSandbox(session, false, opts?.workspaceRoot ?? DEFAULT_WORKSPACE_ROOT);
  }

  /**
   * Create a new inis.run session and own its lifecycle. `opts` is
   * forwarded to `client.sessions.create(...)` (`template`, `size`,
   * `egress`, `name`, `labels`, `idleTimeoutMs`, `maxLifetimeMs`, ... -- see
   * the core `inis` SDK for the full set), plus an optional `workspaceRoot`.
   * `client` defaults to a fresh `Client()` (reading
   * `INIS_API_KEY`/`INIS_BASE_URL`).
   *
   * `close()` destroys the session -- call it when the conversation is over.
   */
  static async create(client?: Client, opts?: InisSandboxCreateOptions): Promise<InisSandbox> {
    const { workspaceRoot, ...sessionOpts } = opts ?? {};
    const actualClient = client ?? new Client();
    const session = await actualClient.sessions.create(sessionOpts);
    return new InisSandbox(session, true, workspaceRoot ?? DEFAULT_WORKSPACE_ROOT);
  }

  /** Destroy the session (owned mode only). A no-op for a wrapped (attached)
   * sandbox. */
  async close(): Promise<void> {
    if (!this.owned) return;
    try {
      await this.session.destroy();
    } catch (e) {
      if (!(e instanceof InisError)) throw e; // best-effort: already gone is fine
    }
  }

  async [Symbol.asyncDispose](): Promise<void> {
    await this.close();
  }

  // ── Command execution ─────────────────────────────────────────────────

  async *executeStreaming(
    command: string,
    options?: ExecuteOptions,
  ): AsyncGenerator<StreamChunk | ExecutionResult, void, undefined> {
    const prefixed = buildEnvPrefix(options?.env) + command;
    const timeoutMs = options?.timeout ? Math.floor(options.timeout * 1000) : undefined;
    const signal = options?.signal;

    const inner = this.session.execStream(prefixed, { cwd: options?.cwd, timeoutMs });

    // Races each `inner.next()` against the abort signal so a signal that
    // fires mid-wait preempts an in-flight step, not just the next loop
    // iteration. `execStream` has no cancellation parameter of its own;
    // calling `.return()` on the generator is what closes the underlying
    // connection and (per Session.execStream's own documented behavior)
    // kills the remote command.
    let abortPromise: Promise<"aborted"> | undefined;
    let removeAbortListener: (() => void) | undefined;
    if (signal) {
      abortPromise = new Promise<"aborted">((resolve) => {
        if (signal.aborted) {
          resolve("aborted");
          return;
        }
        const onAbort = () => resolve("aborted");
        signal.addEventListener("abort", onAbort, { once: true });
        removeAbortListener = () => signal.removeEventListener("abort", onAbort);
      });
    }

    let outParts = "";
    let errParts = "";
    try {
      while (true) {
        const nextResult = inner.next();
        const winner = abortPromise ? await Promise.race([nextResult, abortPromise]) : await nextResult;
        if (winner === "aborted") {
          await inner.return(undefined);
          throw new SandboxAbortError();
        }
        const { value: event, done } = winner as IteratorResult<ExecStreamEvent>;
        if (done) return;

        if (event.stream === "stdout") {
          outParts += event.data;
          yield { type: "streamChunk", data: event.data, streamType: "stdout" };
        } else if (event.stream === "stderr") {
          errParts += event.data;
          yield { type: "streamChunk", data: event.data, streamType: "stderr" };
        } else if (event.stream === "exit") {
          if (event.timedOut) {
            throw new SandboxTimeoutError((timeoutMs ?? 0) / 1000);
          }
          yield {
            type: "executionResult",
            exitCode: event.exitCode ?? 0,
            stdout: outParts,
            stderr: errParts,
            outputFiles: [],
          };
          return;
        } else if (event.stream === "error") {
          // A guest-side failure distinct from a nonzero exit (e.g. the
          // guest agent itself became unreachable mid-command) -- throw
          // rather than yield a result that would misrepresent this as
          // "the command ran and produced no output."
          throw new Error(`inis.run guest error during execution: ${event.data}`);
        }
      }
    } finally {
      removeAbortListener?.();
    }
  }

  // ── Files ──────────────────────────────────────────────────────────────

  async readFile(path: string): Promise<Uint8Array> {
    // InisError already extends Error, satisfying the base contract's
    // "@throws Error if the file does not exist" -- matching
    // PosixShellSandbox's own default readFile, which throws for any
    // nonzero exit code from its `base64 < path` shell command too, not
    // just a genuinely missing file. No need to re-wrap.
    return await this.session.readFile(path, "base64");
  }

  async writeFile(path: string, content: Uint8Array): Promise<void> {
    // Session.writeFile base64-encodes a Uint8Array payload itself; the
    // guest creates parent directories automatically (secureMkdirAll),
    // matching writeFile's documented contract.
    await this.session.writeFile(path, content);
  }

  async removeFile(path: string): Promise<void> {
    // Same reasoning as readFile: InisError already satisfies "@throws
    // Error", and PosixShellSandbox's own default removeFile is equally
    // unconditional about a nonzero exit meaning "not found".
    await this.session.remove(path);
  }

  async listFiles(path: string): Promise<FileInfo[]> {
    let entries: string[];
    try {
      entries = await this.session.listFiles(path);
    } catch (e) {
      if (e instanceof InisError && looksLikeMissingPath(e)) {
        throw new SandboxPathNotFoundError(path);
      }
      throw e;
    }
    // guest/agent/files.go's listDirOp appends a trailing "/" to every
    // directory entry name -- verified in the guest source, not guessed.
    // `size` stays undefined: this listing op does not report it.
    return entries.map((entry) => {
      const isDir = entry.endsWith("/");
      return { name: isDir ? entry.slice(0, -1) : entry, isDir };
    });
  }

  // ── Tool vending ─────────────────────────────────────────────────────────

  /** Vend the standard `sandbox_bash`/`sandbox_file_editor` tools bound to
   * this session -- the same pattern `DockerSandbox`/`SshSandbox` use. No
   * separate create/destroy/pause/resume/fork tool is vended: those stay
   * host-controlled (see module docstring). */
  getTools(): Tool[] {
    const sessionDesc = `inis.run session "${this.session.sessionId}"`;
    return [
      makeFileEditor(this, {
        name: "sandbox_file_editor",
        description: `${DEFAULT_FILE_EDITOR_DESCRIPTION} Files are in ${sessionDesc}.`,
      }) as unknown as Tool,
      makeBash(this, {
        name: "sandbox_bash",
        description: `${SANDBOX_BASH_DESCRIPTION} Runs in ${sessionDesc}. Working directory: ${this.workspaceRoot}.`,
      }) as unknown as Tool,
    ];
  }
}
