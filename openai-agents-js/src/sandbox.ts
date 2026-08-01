/**
 * Native inis.run sandbox provider for the OpenAI Agents SDK's `SandboxAgent`
 * path (`@openai/agents-core/sandbox`).
 *
 * Verified against `@openai/agents-core==0.14.1`/`@openai/agents==0.14.1`.
 * Two things worth recording explicitly:
 *
 * 1. The TypeScript SDK documents its own sandbox surface as upstream BETA
 *    (same wording as the Python package —
 *    https://openai.github.io/openai-agents-python/sandbox/clients/ — the
 *    JS docs site mirrors the same status).
 * 2. Unlike the Python package's heavyweight internal `BaseSandboxSession`
 *    ABC, the TypeScript `SandboxSession`/`SandboxClient` types
 *    (`@openai/agents-core/sandbox`) are plain **structural** interfaces —
 *    every member is optional, and the SDK ships only `local`/`docker`
 *    providers in-tree (no E2B/Modal/etc TS equivalents to mirror). This
 *    genuinely matches the issue's "structural contract" framing for the
 *    TS side, unlike Python's — see the `inis-openai-agents` (Python
 *    package) module docstring for that discrepancy.
 *
 * Two ways to put an inis.run session behind a `SandboxAgent`:
 *
 * 1. **Attached — one inis.run session for the whole conversation
 *    (recommended).** Create or attach to a session yourself and pass it
 *    through `RunConfig({ sandbox: { session } })` for every `run()` call
 *    in the conversation. The runtime never destroys a session provided
 *    this way — call `session.destroy()` yourself when the conversation is
 *    over.
 * 2. **Owned, one `run()` call at a time**, via `InisSandboxClient` +
 *    `RunConfig({ sandbox: { client: new InisSandboxClient(), options } })`.
 *    The SDK creates a fresh inis.run session for that call and destroys it
 *    when the call ends.
 *
 * Explicitly not implemented (declared, not faked): PTY (`supportsPty()`
 * stays `false`), Docker-volume-mount manifests, nested `Dir` entry
 * children and non-`file`/`dir` manifest entry types (`local_file`,
 * `local_dir`, `git_repo` — these throw a clear error rather than silently
 * doing nothing), and true remote cancellation of an in-flight command --
 * the last of these because the upstream contract carries no abort handle,
 * not because inis.run lacks a cancellation primitive.
 */

import {
  Manifest,
  normalizeSandboxClientCreateArgs,
  type ExecCommandArgs,
  type ExposedPortEndpoint,
  type ListDirectoryArgs,
  type MaterializeEntryArgs,
  type ReadFileArgs,
  type SandboxClient,
  type SandboxClientCreateArgs,
  type SandboxClientOptions,
  type SandboxClientResumeOptions,
  type SandboxDirectoryEntry,
  type SandboxExecResult,
  type SandboxSession,
  type SandboxSessionLifecycleOptions,
  type SandboxSessionState,
  type WorkspaceArchiveData,
  type WorkspaceArchiveOptions,
  type WriteStdinArgs,
} from "@openai/agents-core/sandbox";
import { Client, InisError, Session } from "@inis-run/sdk";
import { randomBytes } from "node:crypto";

const TAR_MAGIC = "INIS_SANDBOX_TAR_V1\n";
const DEFAULT_WORKSPACE_ROOT = "/workspace";

export interface InisSandboxClientOptions extends SandboxClientOptions {
  template?: string;
  size?: string;
  egressDefault?: "allow" | "deny";
  egressAllow?: string[];
  name?: string;
  labels?: Record<string, string>;
  idleTimeoutMs?: number;
  maxLifetimeMs?: number;
}

export interface InisSandboxSessionState extends SandboxSessionState {
  type: "inis";
  /** The inis.run session id this state was captured from. Informational
   * only for `resume()` — see the module docstring: the SDK destroys owned
   * sessions at the end of every owned run, so by the time `resume()` runs
   * this id is very likely already gone. */
  inisSessionId: string;
  workspaceRoot: string;
}

function shellCommand(args: ExecCommandArgs): string[] {
  const shellBin = args.shell ?? "sh";
  const flag = args.login === false ? "-c" : "-lc";
  return [shellBin, flag, args.cmd];
}

function decodeInisError(e: unknown): Error {
  if (e instanceof InisError) {
    return new Error(`inis error [${e.code}] (status=${e.status}, retryable=${e.retryable}): ${e.message}`);
  }
  return e instanceof Error ? e : new Error(String(e));
}

/** `SandboxSession` backed by one `inis.Session`. Construct via
 * `InisSandboxSession.wrap()` (attach mode) or `InisSandboxClient`
 * (owned mode) — not directly. */
export class InisSandboxSession implements SandboxSession<InisSandboxSessionState> {
  state: InisSandboxSessionState;
  private readonly session: Session;

  constructor(state: InisSandboxSessionState, session: Session) {
    this.state = state;
    this.session = session;
  }

  /** Wrap an inis.run session the host already created or attached to.
   * Pass the result via `RunConfig({ sandbox: { session } })`. This wrapper
   * never creates or destroys the underlying session — call
   * `await session.destroy()` (the `inis.Session`, not this wrapper)
   * yourself when the conversation is over. */
  static wrap(session: Session, opts?: { workspaceRoot?: string }): InisSandboxSession {
    if (!session.sessionId) {
      throw new Error(
        "InisSandboxSession.wrap() requires an already-created/attached session",
      );
    }
    const workspaceRoot = opts?.workspaceRoot ?? DEFAULT_WORKSPACE_ROOT;
    const state: InisSandboxSessionState = {
      type: "inis",
      inisSessionId: session.sessionId,
      workspaceRoot,
      manifest: new Manifest({ root: workspaceRoot }),
      workspaceReady: true,
    };
    return new InisSandboxSession(state, session);
  }

  supportsPty(): boolean {
    return false;
  }

  async running(): Promise<boolean> {
    try {
      const info = await this.session.get();
      return info.state === "live" || info.state === "paused";
    } catch {
      return false;
    }
  }

  async exec(args: ExecCommandArgs): Promise<SandboxExecResult> {
    const command = shellCommand(args);
    const start = Date.now();
    try {
      const result = await this.session.exec(command, {
        cwd: args.workdir,
        timeoutMs: args.yieldTimeMs,
      });
      // `SandboxExecResult` has no `truncated` slot -- just output/stdout/
      // stderr/exitCode -- so there is nowhere typed to carry inis.run's
      // own truncation signal (the guest's per-stream capture cap cutting
      // a stream before it ever reached this adapter). Rather than
      // silently dropping that, append a visible marker to stderr.
      //
      // This adapter's source is public and this product runs untrusted/
      // AI-generated code, so a static marker string could simply be read
      // out of this file and printed verbatim by the guest to fake (or
      // mask) a truncation signal. A random per-call nonce, generated
      // host-side after the command has already run, can't be predicted by
      // guest code no matter how thoroughly it has read this source -- it
      // closes off blind copy-paste spoofing, though a guest with full
      // control of its own output can still print *some* text shaped like
      // the marker with the wrong nonce.
      const stderr = result.truncated
        ? `${result.stderr}\n[inis: output truncated by the sandbox's own capture limit (nonce=${randomBytes(8).toString("hex")})]`
        : result.stderr;
      const output =
        result.stdout && stderr ? `${result.stdout}\n${stderr}` : result.stdout || stderr;
      return {
        output,
        stdout: result.stdout,
        stderr,
        wallTimeSeconds: (Date.now() - start) / 1000,
        exitCode: result.timedOut ? undefined : result.exitCode,
      };
    } catch (e) {
      throw decodeInisError(e);
    }
  }

  async execCommand(args: ExecCommandArgs): Promise<string> {
    const result = await this.exec(args);
    if (result.exitCode == null) {
      return `${result.output}\n[timed out]`;
    }
    if (result.exitCode !== 0) {
      return `${result.output}\n[exit code: ${result.exitCode}]`;
    }
    return result.output;
  }

  async writeStdin(_args: WriteStdinArgs): Promise<string> {
    throw new Error(
      "InisSandboxSession does not support writeStdin — PTY is not implemented in this beta " +
        "cut (inis.run's PTY primitive is a separate product surface)",
    );
  }

  async readFile(args: ReadFileArgs): Promise<string | Uint8Array> {
    try {
      const data = await this.session.readFile(args.path, "base64");
      return data;
    } catch (e) {
      throw decodeInisError(e);
    }
  }

  async listDir(args: ListDirectoryArgs): Promise<SandboxDirectoryEntry[]> {
    // `list_files` only returns names, not entry types, so we resolve type
    // (file/dir) with one extra `find -maxdepth 1` exec rather than
    // guessing or reporting every entry as "other".
    const script =
      `find ${JSON.stringify(args.path)} -mindepth 1 -maxdepth 1 ` +
      `-printf '%y %f\\n' 2>/dev/null`;
    const result = await this.session.exec(["sh", "-c", script]);
    const entries: SandboxDirectoryEntry[] = [];
    for (const line of result.stdout.split("\n")) {
      if (!line.trim()) continue;
      const [kind, ...rest] = line.split(" ");
      const name = rest.join(" ");
      const type: SandboxDirectoryEntry["type"] = kind === "d" ? "dir" : kind === "f" ? "file" : "other";
      entries.push({ name, path: `${args.path.replace(/\/$/, "")}/${name}`, type });
    }
    return entries;
  }

  async materializeEntry(args: MaterializeEntryArgs): Promise<void> {
    const entry = args.entry as { type: string; content?: string | Uint8Array };
    if (entry.type === "file") {
      const content = entry.content ?? "";
      const text = typeof content === "string" ? content : Buffer.from(content).toString("base64");
      const encoding = typeof content === "string" ? "text" : "base64";
      await this.session.writeFile(args.path, text, encoding as "text" | "base64");
      return;
    }
    if (entry.type === "dir") {
      await this.session.mkdir(args.path);
      return;
    }
    throw new Error(
      `InisSandboxSession.materializeEntry(): entry type ${JSON.stringify(entry.type)} is not ` +
        "implemented in this beta cut (only plain 'file' and 'dir' entries are; " +
        "'local_file'/'local_dir'/'git_repo' and nested Dir children are declared unsupported, " +
        "not faked)",
    );
  }

  async persistWorkspace(): Promise<Uint8Array> {
    const root = this.state.workspaceRoot;
    const script = `tar -C ${JSON.stringify(root)} -cf - . | base64 -w0`;
    const result = await this.session.exec(["bash", "-lc", script]);
    if (result.exitCode !== 0) {
      throw new Error(
        `@inis-run/openai-agents: persistWorkspace() failed (exit ${result.exitCode}): ${result.stderr}`,
      );
    }
    const raw = Buffer.from(result.stdout, "base64");
    return Buffer.concat([Buffer.from(TAR_MAGIC, "utf-8"), raw]);
  }

  async hydrateWorkspace(data: WorkspaceArchiveData, _options?: WorkspaceArchiveOptions): Promise<void> {
    let raw = Buffer.isBuffer(data)
      ? data
      : typeof data === "string"
        ? Buffer.from(data, "utf-8")
        : Buffer.from(data instanceof ArrayBuffer ? new Uint8Array(data) : data);
    if (!raw.length) return;
    const magic = Buffer.from(TAR_MAGIC, "utf-8");
    if (raw.subarray(0, magic.length).equals(magic)) {
      raw = raw.subarray(magic.length);
    }
    const root = this.state.workspaceRoot;
    // Must live under /workspace, not /tmp: confirmed live against
    // api.inis.run that the real `writeFile` endpoint (unlike a bare
    // exec()) enforces "path must be under /workspace" server-side. The
    // Python package's local test fake didn't reproduce that restriction
    // either, so this shipped broken against the real API despite passing
    // every unit test -- caught only by an actual live run. Cleaned up in
    // `finally` below regardless.
    const tarPath = `${root.replace(/\/$/, "")}/.inis-sandbox-hydrate-${this.state.inisSessionId}.tar.b64`;
    const encoded = raw.toString("base64");
    try {
      await this.session.writeFile(tarPath, encoded, "text");
      // `< file` (stdin redirection), not a positional file argument: GNU
      // base64 accepts either, BSD/macOS base64 only decodes from stdin.
      const script = `base64 -d < ${JSON.stringify(tarPath)} | tar -C ${JSON.stringify(root)} -xf -`;
      const result = await this.session.exec(["bash", "-lc", script]);
      if (result.exitCode !== 0) {
        throw new Error(
          `@inis-run/openai-agents: hydrateWorkspace() failed (exit ${result.exitCode}): ${result.stderr}`,
        );
      }
    } finally {
      try {
        await this.session.remove(tarPath);
      } catch {
        // best-effort
      }
    }
  }

  async resolveExposedPort(port: number): Promise<ExposedPortEndpoint> {
    const preview = await this.session.expose(port);
    const host = preview.previewUrl.split("://")[1]?.split("/")[0] ?? preview.previewUrl;
    return { host, port: 443, tls: true, url: preview.previewUrl };
  }

  /** Best-effort local cleanup only — never destroys the inis.run session.
   * For an attached session (`wrap()`), the SDK runtime never even calls
   * this; for an owned session, the real `destroy()` happens in
   * `InisSandboxClient.delete()`. */
  async shutdown(_options?: SandboxSessionLifecycleOptions): Promise<void> {
    return;
  }

  async close(): Promise<void> {
    return;
  }
}

function optionsToSessionArgs(options: InisSandboxClientOptions | undefined) {
  const opts: Record<string, unknown> = {};
  if (!options) return opts;
  if (options.template != null) opts.template = options.template;
  if (options.size != null) opts.size = options.size;
  if (options.egressDefault != null) {
    opts.egress = { mode: options.egressDefault, allow: options.egressAllow };
  }
  if (options.name != null) opts.name = options.name;
  if (options.labels != null) opts.labels = options.labels;
  if (options.idleTimeoutMs != null) opts.idleTimeoutMs = options.idleTimeoutMs;
  if (options.maxLifetimeMs != null) opts.maxLifetimeMs = options.maxLifetimeMs;
  return opts;
}

/** `SandboxClient` that creates/resumes/destroys inis.run sessions owned by
 * a single `run()` call. See the module docstring — for a real multi-turn
 * conversation, use `InisSandboxSession.wrap()` with
 * `RunConfig({ sandbox: { session } })` instead. */
export class InisSandboxClient
  implements SandboxClient<InisSandboxClientOptions, InisSandboxSessionState>
{
  backendId = "inis";
  supportsDefaultOptions = true;
  private readonly client: Client;

  constructor(opts?: { apiKey?: string; baseUrl?: string }) {
    this.client = new Client({ token: opts?.apiKey, baseUrl: opts?.baseUrl });
  }

  create = (async (
    argsOrManifest?: SandboxClientCreateArgs<InisSandboxClientOptions> | Manifest,
    manifestOptions?: InisSandboxClientOptions,
  ): Promise<InisSandboxSession> => {
    const normalized = normalizeSandboxClientCreateArgs<InisSandboxClientOptions>(
      argsOrManifest,
      manifestOptions,
    );
    const sessionArgs = optionsToSessionArgs(normalized.options);
    const session = await Session.create(this.client, sessionArgs as never);
    if (!session.sessionId) {
      throw new Error("@inis-run/openai-agents: session create() did not return a session id");
    }
    const workspaceRoot = normalized.manifest.root || DEFAULT_WORKSPACE_ROOT;
    const state: InisSandboxSessionState = {
      type: "inis",
      inisSessionId: session.sessionId,
      workspaceRoot,
      manifest: normalized.manifest,
      workspaceReady: true,
    };
    return new InisSandboxSession(state, session);
  }) as SandboxClient<InisSandboxClientOptions, InisSandboxSessionState>["create"];

  async delete(state: InisSandboxSessionState): Promise<void> {
    try {
      const session = Session.attach(this.client, state.inisSessionId);
      await session.destroy();
    } catch {
      // best-effort: already gone is fine
    }
  }

  async serializeSessionState(state: InisSandboxSessionState): Promise<Record<string, unknown>> {
    // Plain JSON round-trip — never carries an API key or token. Note this
    // spreads `Manifest`'s own fields but does not reconstruct a live
    // `Manifest` instance on the way back in (see deserializeSessionState);
    // fine for this adapter's own `resume()`, which discards the manifest
    // and starts fresh, but a caller reaching into `state.manifest` after
    // deserializing should not expect its instance methods.
    return { ...state };
  }

  async deserializeSessionState(payload: Record<string, unknown>): Promise<InisSandboxSessionState> {
    return payload as unknown as InisSandboxSessionState;
  }

  async resume(
    state: InisSandboxSessionState,
    _options?: SandboxClientResumeOptions,
  ): Promise<InisSandboxSession> {
    // The owned session this state was captured from is very likely already
    // destroyed (the SDK calls `delete()` unconditionally at the end of
    // every owned run — see module docstring), so this always creates a
    // *new* inis.run session and restores the workspace from
    // `hydrateWorkspace()` rather than reattaching to `state.inisSessionId`.
    const session = await Session.create(this.client);
    if (!session.sessionId) {
      throw new Error("@inis-run/openai-agents: session create() did not return a session id");
    }
    const newState: InisSandboxSessionState = { ...state, inisSessionId: session.sessionId };
    const wrapped = new InisSandboxSession(newState, session);
    return wrapped;
  }
}
