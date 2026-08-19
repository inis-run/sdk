/**
 * Native inis.run sandbox provider for Mastra's `Workspace` (the
 * `WorkspaceSandbox` contract from `@mastra/core/workspace`).
 *
 * Verified against `@mastra/core==1.55.0` by installing the real package and
 * reading its shipped `.d.ts` directly (`workspace/sandbox/sandbox.ts`,
 * `mastra-sandbox.ts`, `process-manager/*`) — not the marketing docs. Two
 * things worth recording:
 *
 * 1. `WorkspaceSandbox` is a genuine, documented third-party extension
 *    point: `mastra-sandbox.d.ts`'s own docstring says "External providers
 *    can extend this class to get logger support, or implement the
 *    WorkspaceSandbox interface directly if they don't need logging." This
 *    isn't a guess from fetched docs — it's the shipped type declaration.
 * 2. Every provider actually published today (`@mastra/e2b`, `@mastra/
 *    daytona`, `@mastra/modal`, `@mastra/blaxel`, `@mastra/docker`) lives
 *    `@mastra/*`-scoped inside the `mastra-ai/mastra` monorepo itself — npm
 *    scopes are access-controlled, so nothing outside that org can publish
 *    under `@mastra/*`. There is no known precedent of a vendor shipping a
 *    `WorkspaceSandbox` provider under its own npm scope; per a deliberate
 *    product decision, this ships as its own `@inis-run/mastra` package
 *    regardless — a working, tested, real provider is the strongest possible
 *    opening for an eventual upstream listing, which stays upside, not a gate.
 *
 * ## Owned vs. attached
 *
 * One `InisSandbox` wraps exactly one inis.run session, in one of two modes:
 *
 * - **Attached** (`{ session }`) — wrap a session the host already created
 *   or attached to (an `inis.Session`, or its session id). This sandbox
 *   never destroys it: `destroy()` is a no-op on the remote session, and
 *   `start()`/`stop()` never pause or resume it either. The host owns that
 *   session's whole lifecycle; this adapter only ever reads/execs against it.
 * - **Owned** (no `session` option) — `start()` creates a fresh session
 *   (`client.sessions.create`), `stop()` pauses it, a subsequent `start()` resumes
 *   it, and `destroy()` really destroys it. This is the mode `clone()`
 *   fleets and one-off/ephemeral workspaces want.
 *
 * Credentials, session create/delete, egress, pause/resume and fork stay
 * host-controlled either way — reach them through the `session` accessor
 * below (a plain getter, never surfaced as a model tool), not through any
 * inference this class makes on its own.
 *
 * ## What's real vs. declared unsupported
 *
 * `executeCommand` is intentionally left undefined here so
 * `MastraSandbox`'s own default (`processes.spawn()` + `.wait()`) applies —
 * see `process-manager.ts`. That default already gives genuine remote
 * cancellation (an `abortSignal` on `executeCommand`/`spawn` reaches a real
 * `killProcess()` SIGTERM-then-SIGKILL) and genuine live output streaming
 * (`onStdout`/`onStderr` are fed from a real Server-Sent-Events log follow),
 * because inis.run's named-background-process API backs both. The one gap
 * is `ProcessHandle.sendStdin()` — no stdin-write endpoint exists for a
 * named process — declared unsupported there, not emulated.
 *
 * `mount`/`unmount`: inis.run has no FUSE-style cloud-filesystem mount
 * primitive. `MountNotSupportedError` is NOT thrown automatically just
 * because these methods are absent — verified against the actual installed
 * `@mastra/core` package: `MountManager` is only constructed, and
 * `Workspace`'s own `mounts` config only wired up, `if` the sandbox
 * implements `mount()` in the first place, and a config-driven mount that
 * throws inside `MountManager.processPending()` is caught there and
 * recorded as a per-entry error state, not rethrown. So mount/unmount ARE
 * implemented below — as the one-line throw the `WorkspaceSandbox`
 * interface's own doc comment says a non-supporting sandbox should raise
 * (`@throws {MountNotSupportedError} if sandbox doesn't support
 * mounting`) — specifically so a caller invoking `sandbox.mount()`/
 * `unmount()` directly gets a loud, immediate error instead of `mount?.()`
 * silently resolving to `undefined`.
 */
import { randomUUID } from "node:crypto";
import type { RequestContext } from "@mastra/core/di";
import {
  MastraSandbox,
  MountNotSupportedError,
  SandboxNotReadyError,
  type InstructionsOption,
  type MastraSandboxOptions,
  type MountResult,
  type ProviderStatus,
  type SandboxCloneOptions,
  type SandboxFileInput,
  type SandboxInfo,
  type SandboxNetworking,
  type WorkspaceFilesystem,
} from "@mastra/core/workspace";
import {
  Client,
  InisError,
  Session,
  type ClientOptions,
  type ConnectionCreate,
  type SessionSize,
} from "@inis-run/sdk";
import { InisProcessManager } from "./process-manager.js";

// `CreateSessionOptions`/`FileBatchItem` are real exported interfaces in
// sdk/typescript/src/client.ts but aren't re-exported from its curated
// top-level `index.ts` (see that file's export list) — derived structurally
// here rather than widening the core SDK's public surface just for this
// provider package.
type CreateSessionOptions = NonNullable<Parameters<Client["sessions"]["create"]>[0]>;
type FileBatchItem = Parameters<Session["writeFiles"]>[0][number];

function decodeInisError(e: unknown): Error {
  if (e instanceof InisError) {
    return new Error(`inis error [${e.code}] (status=${e.status}, retryable=${e.retryable}): ${e.message}`);
  }
  return e instanceof Error ? e : new Error(String(e));
}

/** Configuration for `InisSandbox`. */
export interface InisSandboxOptions extends Omit<MastraSandboxOptions, "processes"> {
  /** Unique identifier for this sandbox instance. Defaults to a fresh id. */
  id?: string;

  /**
   * Attach mode: an already-created/attached `inis.Session`, or its session
   * id (reattached via `client.sessions.attach` — no network call happens until
   * `start()`). When set, this sandbox never creates, pauses, resumes, or
   * destroys the underlying session — see the module docstring.
   */
  session?: Session | string;

  /** Reuse an existing `inis.Client` instead of constructing one from `apiKey`/`baseUrl`. */
  client?: Client;
  /** Passed through to `new Client()` if `client` isn't given. Falls back to `INIS_API_KEY`. */
  apiKey?: string;
  /** Passed through to `new Client()` if `client` isn't given. Falls back to `INIS_BASE_URL`. */
  baseUrl?: string;

  // Owned-mode session-creation options (ignored in attach mode).
  /** Human-readable name for the created session (not this sandbox's `name`/`provider` fields). */
  sessionName?: string;
  template?: string;
  size?: SessionSize;
  egressDefault?: "allow" | "deny";
  egressAllow?: string[];
  /**
   * Inline Connections to create with the owned session: session-scoped
   * credential leases bound to specific external API origins. See
   * `ConnectionCreate` in `@inis-run/sdk` for the full shape. Ignored in
   * attach mode — attach to a session that already has its Connections
   * configured instead.
   */
  connections?: ConnectionCreate[];
  labels?: Record<string, string>;
  /** Plain (non-sensitive) env vars for every command this session's exec/processes spawn. */
  env?: Record<string, string>;
  idleTimeoutMs?: number;
  maxLifetimeMs?: number;

  /**
   * Custom instructions overriding `getInstructions()`'s default. A string
   * fully replaces the default; a function receives it for extension.
   */
  instructions?: InstructionsOption;
}

/**
 * inis.run sandbox provider for Mastra `Workspace`.
 *
 * @example Attached — one inis.run session for a whole conversation (recommended)
 * ```typescript
 * import { Client } from "@inis-run/sdk";
 * import { Workspace } from "@mastra/core/workspace";
 * import { InisSandbox } from "@inis-run/mastra";
 *
 * const client = new Client();
 * const session = await client.sessions.create({ egress: { mode: "deny" } });
 * const workspace = new Workspace({ sandbox: new InisSandbox({ session }) });
 * try {
 *   await workspace.sandbox?.executeCommand?.("ls /workspace");
 * } finally {
 *   await session.destroy(); // this app owns the session; InisSandbox never will
 * }
 * ```
 *
 * @example Owned — the sandbox creates and destroys its own session
 * ```typescript
 * import { Workspace } from "@mastra/core/workspace";
 * import { InisSandbox } from "@inis-run/mastra";
 *
 * const workspace = new Workspace({
 *   sandbox: new InisSandbox({ egressDefault: "deny" }),
 * });
 * await workspace.sandbox?.executeCommand?.("ls /workspace"); // creates the session lazily
 * await workspace.destroy(); // destroys it
 * ```
 */
export class InisSandbox extends MastraSandbox {
  readonly id: string;
  readonly name = "InisSandbox";
  readonly provider = "inis";
  status: ProviderStatus = "pending";
  declare readonly processes: InisProcessManager;
  declare readonly networking: SandboxNetworking;

  private readonly _owned: boolean;
  private readonly _attachArg?: Session | string;
  private readonly _explicitClient?: Client;
  private readonly _clientOpts: ClientOptions;
  private _lazyClient?: Client;
  private readonly _createOpts: CreateSessionOptions;
  private readonly _instructionsOverride?: InstructionsOption;
  private readonly _constructorOptions: InisSandboxOptions;
  private _session?: Session;
  private _createdAt?: Date;

  constructor(options: InisSandboxOptions = {}) {
    super({
      name: "InisSandbox",
      onStart: options.onStart,
      onStop: options.onStop,
      onDestroy: options.onDestroy,
      processes: new InisProcessManager({ env: options.env }),
    });
    this.id = options.id ?? `inis-${randomUUID()}`;
    this._constructorOptions = options;
    this._instructionsOverride = options.instructions;
    this._owned = options.session === undefined;
    this._attachArg = options.session;

    this._explicitClient = options.client;
    this._clientOpts = {};
    if (options.apiKey) this._clientOpts.token = options.apiKey;
    if (options.baseUrl) this._clientOpts.baseUrl = options.baseUrl;

    this._createOpts = {};
    if (options.sessionName) this._createOpts.name = options.sessionName;
    if (options.labels) this._createOpts.labels = options.labels;
    if (options.template) this._createOpts.template = options.template;
    if (options.size) this._createOpts.size = options.size;
    if (options.egressDefault) {
      this._createOpts.egress = { mode: options.egressDefault, allow: options.egressAllow };
    }
    if (options.connections) this._createOpts.connections = options.connections;
    if (options.env) this._createOpts.env = options.env;
    if (options.idleTimeoutMs) this._createOpts.idleTimeoutMs = options.idleTimeoutMs;
    if (options.maxLifetimeMs) this._createOpts.maxLifetimeMs = options.maxLifetimeMs;

    this.networking = {
      getPortUrl: (port: number) => this._getPortUrl(port),
    };
  }

  /**
   * Construct a sibling `InisSandbox` that inherits this sandbox's owned-mode
   * configuration (template, size, egress, env, credentials) with
   * per-instance overrides. Performs no I/O — the clone provisions (or
   * attaches) on its own `start()`.
   *
   * `options.sandboxId`, if given, makes the sibling an *attached* wrap of
   * that existing session id — same as passing `session: sandboxId` to the
   * constructor: the sibling's `destroy()`/`stop()` never touch it. Without
   * `sandboxId` the sibling is *owned*: a fresh session, created on its own
   * `start()`, inheriting this instance's owned-mode options regardless of
   * whether THIS instance itself is owned or attached (attach mode has no
   * meaningful clone target of its own — there is exactly one attached
   * session, owned by the host that created it).
   * `options.idleTimeoutMinutes` maps to `idleTimeoutMs`.
   */
  clone(options?: SandboxCloneOptions): InisSandbox {
    const base = this._constructorOptions;
    return new InisSandbox({
      ...base,
      id: options?.id ?? undefined,
      session: options?.sandboxId,
      env: { ...base.env, ...options?.env },
      idleTimeoutMs: options?.idleTimeoutMinutes != null ? options.idleTimeoutMinutes * 60_000 : base.idleTimeoutMs,
    });
  }

  /**
   * The underlying `inis.Session` for host-side use — reach pause/resume/
   * fork/checkpoint/expose/file APIs not surfaced through `WorkspaceSandbox`
   * here. Deliberately a plain getter, not a model tool: nothing in this
   * package registers it with Mastra's tool system.
   *
   * @throws {SandboxNotReadyError} if the sandbox hasn't been started yet.
   */
  get session(): Session {
    if (!this._session) throw new SandboxNotReadyError(this.status);
    return this._session;
  }

  /**
   * Lazily constructs the `inis.Client` used for attach-by-id and owned-mode
   * `create()` calls — never in the constructor. Attach-by-instance (the
   * common case, `{ session: existingSession }`) needs no `Client` at all,
   * so building one eagerly would force every caller to have `INIS_API_KEY`
   * set (or pass an explicit `client`) even when they never need one.
   */
  private _getClient(): Client {
    if (!this._lazyClient) this._lazyClient = this._explicitClient ?? new Client(this._clientOpts);
    return this._lazyClient;
  }

  async start(): Promise<void> {
    if (!this._owned) {
      if (!this._session) {
        this._session =
          typeof this._attachArg === "string" ? this._getClient().sessions.attach(this._attachArg) : this._attachArg!;
      }
      try {
        await this._session.get(); // verify reachable; throws if the id is bad or the session is gone
      } catch (e) {
        throw decodeInisError(e);
      }
      this._createdAt ??= new Date();
      return;
    }

    if (this._session) {
      // Re-start after our own stop() (pause) — resume, don't recreate.
      try {
        const info = await this._session.get();
        if (info.state === "paused") await this._session.resume();
      } catch (e) {
        throw decodeInisError(e);
      }
      return;
    }

    try {
      this._session = await this._getClient().sessions.create(this._createOpts);
    } catch (e) {
      throw decodeInisError(e);
    }
    this._createdAt = new Date();
  }

  /**
   * Pause the session — real work only in owned mode. Attached mode never
   * pauses a session the host may still be using elsewhere; pause/resume
   * stay host-controlled for a session this sandbox didn't create (reach
   * `sandbox.session.pause()` directly if that's genuinely what's wanted).
   */
  async stop(): Promise<void> {
    if (!this._owned || !this._session) return;
    try {
      await this._session.pause();
    } catch (e) {
      throw decodeInisError(e);
    }
  }

  /**
   * Destroy the session — real work only in owned mode. Attached mode NEVER
   * destroys the underlying session: the host that created/attached it owns
   * that decision. This is the one invariant the epic calls out explicitly
   * and it's enforced here unconditionally, not left to caller discipline.
   */
  async destroy(): Promise<void> {
    if (!this._owned || !this._session) return;
    try {
      await this._session.destroy();
    } catch (e) {
      throw decodeInisError(e);
    }
  }

  async getInfo(): Promise<SandboxInfo> {
    const info = this._session
      ? await this._session.get().catch(() => undefined)
      : undefined;
    return {
      id: this.id,
      name: this.name,
      provider: this.provider,
      status: this.status,
      createdAt: this._createdAt ?? new Date(),
      lastUsedAt: info?.lastActiveAt ? new Date(info.lastActiveAt) : undefined,
      resources: info ? { memoryMB: info.memMb, cpuCores: info.vcpus } : undefined,
      metadata: info
        ? { inisSessionId: info.sessionId, state: info.state, owned: this._owned, connections: info.connections }
        : undefined,
    };
  }

  /**
   * Bulk-write files via inis.run's real batch-write endpoint
   * (`session.writeFiles`) — a genuine fast path, not `executeCommand` in a
   * loop. Not fail-fast per-file: if any file fails, every failure is
   * collected into one error rather than surfacing only the first.
   */
  async writeFiles(files: SandboxFileInput[]): Promise<void> {
    const items: FileBatchItem[] = files.map((f) =>
      typeof f.content === "string"
        ? { path: f.path, content: f.content, encoding: "text" }
        : { path: f.path, content: Buffer.from(f.content).toString("base64"), encoding: "base64" },
    );
    let results;
    try {
      results = await this.session.writeFiles(items);
    } catch (e) {
      throw decodeInisError(e);
    }
    const failed = results.filter((r) => !r.ok);
    if (failed.length > 0) {
      throw new Error(
        `@inis-run/mastra: writeFiles() failed for ${failed.length}/${results.length} file(s): ` +
          failed.map((f) => `${f.path}: ${f.error ?? "unknown error"}`).join("; "),
      );
    }
  }

  /**
   * inis.run has no FUSE-style cloud-filesystem mount primitive. Implemented
   * (rather than left undefined) specifically so this fails loudly: leaving
   * `mount`/`unmount` off this class entirely means `MountManager` is never
   * constructed and `Workspace`'s own `mounts` config wiring never engages
   * (both gate on `if (this.mount)`), so a caller configuring mounts would
   * get no mounts AND no error — a silent no-op. Throwing here instead
   * matches the `WorkspaceSandbox` interface's own documented contract for
   * an unsupporting sandbox (`@throws {MountNotSupportedError}`), so a
   * direct `sandbox.mount(...)` call surfaces the failure immediately. A
   * mount requested via `new Workspace({ mounts: {...} })` config still
   * surfaces this same error, but as a per-path entry state
   * (`workspace.filesystem.mounts.get(path).error`) rather than a thrown
   * exception — `MountManager.processPending()` deliberately catches and
   * records per-mount failures rather than letting one bad mount crash
   * startup, and that catching happens in `@mastra/core`, outside this
   * adapter's control.
   */
  async mount(_filesystem: WorkspaceFilesystem, _mountPath: string): Promise<MountResult> {
    throw new MountNotSupportedError(this.provider);
  }

  /** See mount()'s doc comment — same reasoning applies to unmount. */
  async unmount(_mountPath: string): Promise<void> {
    throw new MountNotSupportedError(this.provider);
  }

  getInstructions(opts?: { requestContext?: RequestContext }): string {
    const defaultInstructions = this._getDefaultInstructions();
    if (this._instructionsOverride === undefined) return defaultInstructions;
    return typeof this._instructionsOverride === "string"
      ? this._instructionsOverride
      : this._instructionsOverride({ defaultInstructions, requestContext: opts?.requestContext });
  }

  private _getDefaultInstructions(): string {
    return (
      "Commands run inside a persistent, isolated sandbox, not on this host. " +
      "The default working directory is /workspace. Background processes started " +
      "with background: true keep running and streaming output after the tool call " +
      "returns; kill them explicitly when done."
    );
  }

  /**
   * Real, via `session.expose()` — not a guess at a URL pattern. Exposes the
   * port publicly (no separate ingress token needed) so the URL this returns
   * is directly fetchable, matching what `SandboxNetworking.getPortUrl`
   * promises. Returns null if the sandbox has no session yet rather than
   * throwing, per the interface's own contract ("or the sandbox is not
   * running").
   */
  private async _getPortUrl(port: number): Promise<string | null> {
    if (!this._session) return null;
    try {
      const result = await this._session.expose(port, { visibility: "public" });
      return result.previewUrl || null;
    } catch (e) {
      throw decodeInisError(e);
    }
  }
}
