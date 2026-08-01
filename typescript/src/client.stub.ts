/**
 * client.stub.ts — full intended interface for the inis.run TypeScript SDK.
 *
 * Every method body throws "not implemented". This file is the executable
 * surface contract that client.ts should converge to. See docs/api-design/typescript-sdk.md
 * for the design rationale and usage examples.
 */

// ---------------------------------------------------------------------------
// Error
// ---------------------------------------------------------------------------

export class InisError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "InisError";
  }
}

// ---------------------------------------------------------------------------
// Primitive types
// ---------------------------------------------------------------------------

/** Sandbox tier: small (1 vCPU/1 GB), medium (2/4 GB), large (4/8 GB). */
export type SessionSize = "small" | "medium" | "large";

/** Lifecycle states a session can be in. */
export type SessionState = "live" | "paused" | "creating" | "ended";

/**
 * What the server does when the last PTY client disconnects.
 * Only fires when a PTY terminal disconnects. No-op for API-only sessions.
 */
export type OnPtyDetach = "keep_live" | "pause" | "destroy";

/** Why a session ended. Present only on state="ended" history rows. */
export type EndReason =
  | "client_destroy"
  | "shell_exit"
  | "on_detach_destroy"
  | "max_lifetime"
  | "paused_ttl"
  | "error";

// ---------------------------------------------------------------------------
// Archive, egress, previews
// ---------------------------------------------------------------------------

/**
 * Status of the durable (cold) object-store copy for a paused session.
 * Present only when archiving is enabled and an upload has been attempted.
 */
export interface ArchiveStatus {
  /** Cold upload status. */
  status: "pending" | "complete" | "failed";
  /** Object-store prefix for the bundle and manifest. */
  coldUri?: string;
  /** Timestamp the upload completed (set when status is "complete"). */
  uploadedAt?: string;
}

/** Per-session outbound network policy. */
export interface EgressPolicy {
  /** "allow" (default) keeps full public egress; "deny" restricts to the allow list. */
  mode?: "allow" | "deny";
  /** Domains reachable in deny mode: exact ("api.openai.com") or "*.wildcard". */
  allow?: string[];
}

/** An exposed port with its current preview URL and access settings. */
export interface ExposedPreview {
  port: number;
  previewUrl: string;
  visibility: "token" | "public";
  /** Inbound access mode for the preview URL. */
  auth?: "none" | "bearer";
}

// ---------------------------------------------------------------------------
// Session
// ---------------------------------------------------------------------------

export interface SessionInfo {
  sessionId: string;
  state: SessionState;

  // Identity
  name?: string;
  labels?: Record<string, string>;

  // Lifecycle timestamps
  createdAt?: string;
  lastActiveAt?: string;
  /** Set only on state="ended" history rows. */
  endedAt?: string;
  /** Set only on state="ended" history rows. */
  endReason?: EndReason;

  // Behaviour
  idleTimeoutMs?: number;
  maxLifetimeMs?: number;
  /**
   * Action taken when the last PTY client disconnects.
   * Only fires when a PTY terminal disconnects. No-op for API-only sessions.
   */
  onPtyDetach?: OnPtyDetach;
  /**
   * Per-session paused TTL in milliseconds.
   * 0 = daemon default; negative = never expire.
   */
  pausedTtlMs?: number;
  /** Whether the session runs in hardened, no-sudo mode. */
  noSudo?: boolean;

  // Resources
  size?: SessionSize;
  vcpus?: number;
  memMb?: number;
  nodeId?: string;
  volumeId?: string;
  /** Template this session was created from (omitted for default golden path). */
  template?: string;

  // Network
  exposedPorts?: number[];
  /** Exposed ports with their reachable preview URLs. */
  exposedPreviews?: ExposedPreview[];
  egress?: EgressPolicy;

  // Durable archive
  archive?: ArchiveStatus;
  mcpUrl?: string;

  /** When the current continuous-live stretch began (RFC 3339). Set only
   * while live with a max-active-window armed. */
  activeSince?: string;
  /** The continuous-active window (ms) the current live stretch was armed
   * with. Set only while live with the window armed. */
  maxActiveWindowMs?: number;
  /** Resume-readiness of a PAUSED session: "hot" | "warm" | "cold". */
  snapshotTier?: "hot" | "warm" | "cold";
}

export interface CreateSessionOptions {
  name?: string;
  labels?: Record<string, string>;
  volumeId?: string;
  maxLifetimeMs?: number;
  idleTimeoutMs?: number;
  egress?: EgressPolicy;
  /** Base environment; omit or "base" for the default golden path. */
  template?: string;
  size?: SessionSize;
  /** Harden the sandbox for untrusted code by removing passwordless sudo. */
  noSudo?: boolean;
  /**
   * What to do when the last PTY client disconnects. Default: "pause".
   * Only fires when a PTY terminal disconnects. No-op for API-only sessions.
   */
  onPtyDetach?: OnPtyDetach;
  /** Per-session paused TTL (ms). 0 = daemon default; negative = never expire. */
  pausedTtlMs?: number;
  /** Tear down the session as soon as the first command completes. */
  destroyOnCompletion?: boolean;
  /** Declare artifact capture to run automatically when the session ends. */
  artifacts?: ArtifactDeclaration;
}

export interface ListSessionsOptions {
  state?: SessionState;
  limit?: number;
  cursor?: string;
}

export interface DestroyOptions {
  /** Recorded as end_reason on the history row. Defaults to "client_destroy". */
  reason?: "client_destroy" | "shell_exit";
}

// ---------------------------------------------------------------------------
// Exec and batch
// ---------------------------------------------------------------------------

export interface ExecResult {
  stdout: string;
  stderr: string;
  exitCode: number;
  durationMs: number;
  timedOut: boolean;
  /** VM restore/boot time before the command ran. Observability; not billed. */
  restoreMs?: number;
  /** Dependency install time. One-shot execute only. */
  installMs?: number;
  /** Execution phase name. One-shot execute only. */
  phase?: string;
  /** True when stdout/stderr are an incomplete view of what the command produced. */
  truncated?: boolean;
  /**
   * undefined (the default) means stdout is plain text. "base64" means that
   * stream's output was not valid UTF-8, so the API returned it
   * base64-encoded rather than risk silently corrupting it.
   */
  stdoutEncoding?: "base64";
  /** Same as stdoutEncoding, for stderr. */
  stderrEncoding?: "base64";
}

/** One event from a live-streamed exec (see Session.execStream). */
export interface ExecStreamEvent {
  stream: "stdout" | "stderr" | "exit" | "error";
  /** Decoded text for stdout/stderr/error; empty for exit. */
  data: string;
  /** Set only when stream === "exit". */
  exitCode?: number;
  /** Set only when stream === "exit". */
  timedOut?: boolean;
  /** Set only when stream === "exit". */
  durationMs?: number;
}

/**
 * One typed piece of Session.runCode() output.
 *
 * type is "text" (stdout/stderr/last-expression repr), "image" (a captured
 * matplotlib figure — format "png", data holds decoded bytes), "table" (a
 * pandas DataFrame/Series, capped at 1000 rows), "json" (a dict/list/tuple
 * last expression), or "error" (an uncaught exception — the interpreter
 * context is untouched and stays usable).
 */
export interface InterpreterResult {
  type: "text" | "image" | "table" | "json" | "error";
  text?: string;
  stream?: "stdout" | "stderr" | "result";
  format?: string;
  data?: Uint8Array;
  size?: number;
  columns?: string[];
  rows?: Record<string, unknown>[];
  rowCount?: number;
  truncated?: boolean;
  json?: unknown;
  ename?: string;
  evalue?: string;
  traceback?: string;
  path?: string;
}

export interface ForkResult {
  parentSessionId: string;
  /** Session IDs of the newly forked children. */
  children: string[];
}

// ---------------------------------------------------------------------------
// PTY (interactive terminal)
// ---------------------------------------------------------------------------

/** Options for Session.pty(). */
export interface PTYOptions {
  cols?: number;
  rows?: number;
  /** Defaults to the guest's login shell; pass e.g. ["python3", "-i"] for a REPL. */
  command?: string[];
  cwd?: string;
  term?: string;
  colorterm?: string;
  /** Milliseconds to wait for the WebSocket handshake. Default 30000. */
  openTimeoutMs?: number;
}

/** Terminal state of a PTY once its shell/command has exited. */
export interface PTYExitInfo {
  exitCode: number;
}

/**
 * A live interactive pseudo-terminal, over the same WebSocket endpoint and
 * binary frame protocol the console's web terminal uses. Async-iterate for
 * output; write()/resize() to send input; .exitCode once iteration ends.
 */
export class PTY {
  /** Send input, as if typed at the keyboard. */
  async write(_data: string | Uint8Array): Promise<void> {
    throw new Error("not implemented");
  }

  /** Tell the guest shell the terminal viewport changed size. */
  async resize(_cols: number, _rows: number): Promise<void> {
    throw new Error("not implemented");
  }

  /** Send a POSIX signal to the foreground process (e.g. 9 = SIGKILL). */
  async sendSignal(_sig: number): Promise<void> {
    throw new Error("not implemented");
  }

  /**
   * Force-kill the foreground process (SIGKILL). Does not itself close the
   * connection — keep iterating (or call close()) to see the resulting
   * PTY_EXIT frame and .exitCode.
   */
  async kill(): Promise<void> {
    throw new Error("not implemented");
  }

  /** The command's exit code, or null until PTY_EXIT has arrived. */
  get exitCode(): number | null {
    throw new Error("not implemented");
  }

  /** Async-iterate output chunks; stops on exit (see .exitCode) or disconnect. */
  [Symbol.asyncIterator](): AsyncIterator<Uint8Array> {
    throw new Error("not implemented");
  }

  async close(): Promise<void> {
    throw new Error("not implemented");
  }
}

export interface BatchExecResult {
  sessionId: string;
  stdout: string;
  stderr: string;
  exitCode: number;
  durationMs: number;
  timedOut: boolean;
  /** Set when the fan-out itself failed for this session (distinct from command failure). */
  error?: string;
  /** See ExecResult.stdoutEncoding for the full rationale. */
  stdoutEncoding?: "base64";
  /** Same as stdoutEncoding, for stderr. */
  stderrEncoding?: "base64";
}

export interface BatchExecOptions {
  sessionIds: string[];
  command: string | string[];
  cwd?: string;
  timeoutMs?: number;
}

// ---------------------------------------------------------------------------
// Checkpoints
// ---------------------------------------------------------------------------

export interface CheckpointInfo {
  checkpointId: string;
  sessionId?: string;
  parentSessionId?: string;
  name?: string;
  labels?: Record<string, string>;
  sizeBytes: number;
  createdAt?: string;
  nodeId?: string;
}

export interface CheckpointOptions {
  name?: string;
  labels?: Record<string, string>;
}

export interface CheckpointSessionOptions {
  name?: string;
  labels?: Record<string, string>;
  maxLifetimeMs?: number;
  idleTimeoutMs?: number;
}

// ---------------------------------------------------------------------------
// Templates
// ---------------------------------------------------------------------------

export interface TemplateInfo {
  name: string;
  /** "official" = curated by inis.run, "user" = published by the caller. */
  kind?: "official" | "user";
  description?: string;
  size?: SessionSize;
  /** Current promoted version. Advisory — bare-name references resolve here. */
  version?: string;
  /** All non-deprecated versions available for pinning via "name@version". */
  versions?: string[];
  createdAt?: string;
  /** BYO-image import: "queued" behind the node's serial build queue, then
   * "building" while the image is flattened, then "ready" or "failed". "ready"
   * for every official/promote-a-session template. */
  status?: "queued" | "building" | "ready" | "failed";
  /** OCI ref this template was imported from, if any. */
  sourceImage?: string;
  /** Reason the import build failed (present when status is "failed"). */
  buildError?: string;
  /** 1-based place in the node's serial build queue. Only while "queued". */
  queuePosition?: number;
  /** When the current version's bundle was built (RFC3339 UTC). Official
   * templates only. */
  builtAt?: string;
  /** Upstream base image the bundle was built from. Official templates only. */
  baseImageRef?: string;
  /** Content digest of the base image (sha256:...). Official templates only. */
  baseImageDigest?: string;
  /** Content hash of the built bundle. Official templates only. */
  contentHash?: string;
  /** Rebuild cadence for this template. Official templates only. */
  cadence?: "weekly" | "monthly";
  /** Grouping for the official set. Official templates only. */
  category?: "language" | "use-case";
}

export interface SaveAsTemplateOptions {
  description?: string;
}

// ---------------------------------------------------------------------------
// Registry credentials
// ---------------------------------------------------------------------------

/** A stored private-registry pull credential, redacted — the secret is
 * never included, on create, list, or any other response. */
export interface RegistryCredentialInfo {
  id: string;
  name: string;
  registryHost: string;
  username: string;
  /** Last 4 characters of the stored secret — a display aid, never enough
   * to reconstruct it. */
  secretLast4?: string;
  createdAt?: string;
  updatedAt?: string;
}

export interface AddRegistryCredentialOptions {
  registryHost: string;
  username: string;
  secret: string;
}

/** A stored egress connector, redacted — the secret is never included, on
 * create, list, or any other response. */
export interface ConnectorInfo {
  id: string;
  name: string;
  targetBaseUrl: string;
  authShape: string;
  headerName?: string;
  secretLast4?: string;
  createdAt?: string;
  updatedAt?: string;
}

export interface AddConnectorOptions {
  targetBaseUrl: string;
  authShape?: "bearer" | "header";
  headerName?: string;
  secret: string;
}

// ---------------------------------------------------------------------------
// Webhooks
// ---------------------------------------------------------------------------

/** A registered webhook subscription. `secret` is populated ONLY on the
 * response from `webhooks.add()` — store it immediately, it is never
 * returned again (`secretLast4` is what `list()` shows instead). */
export interface WebhookEndpointInfo {
  id: string;
  url: string;
  eventTypes: string[];
  enabled: boolean;
  createdAt?: string;
  disabledAt?: string;
  secret?: string;
  secretLast4?: string;
}

export interface AddWebhookOptions {
  /** Allowlist of event types to receive; omit for every type. */
  eventTypes?: string[];
}

/** One logged delivery attempt series — the per-subscription delivery log
 * entry. */
export interface WebhookDeliveryInfo {
  id: number;
  eventType: string;
  status: "pending" | "delivered" | "dead";
  attemptCount: number;
  maxAttempts: number;
  sessionId?: string;
  attempts?: { attempt: number; at: string; statusCode?: number; error?: string }[];
  createdAt?: string;
  deliveredAt?: string;
  /** Set only while status is "pending". */
  nextAttemptAt?: string;
}

// ---------------------------------------------------------------------------
// Custom domains
// ---------------------------------------------------------------------------

/** A registered custom domain: CNAME a customer-owned hostname to the
 * fleet's ingress, verify DNS ownership, then route it to an exposed
 * session port for auto-provisioned TLS in place of the default preview
 * URL. `verifyTxtName`/`verifyTxtValue` are the TXT record to create before
 * calling `domains.verify()` (a CNAME to the fleet's ingress also satisfies
 * verification). v1 binds directly to a (sessionId, port) pair rather than
 * a longer-lived project route. */
export interface DomainInfo {
  id: string;
  domain: string;
  verified: boolean;
  verifyTxtName: string;
  verifyTxtValue: string;
  createdAt?: string;
  verifiedAt?: string;
  sessionId?: string;
  port?: number;
  /** The hostname to CNAME this domain to so requests reach the fleet.
   * Undefined when the deployment has no ingress domain set. */
  ingressBaseDomain?: string;
}

// ---------------------------------------------------------------------------
// Artifacts
// ---------------------------------------------------------------------------

export interface ArtifactDestination {
  /** "hosted" uses inis.run EU object store; "s3" uses your own bucket. */
  type?: "hosted" | "s3";
  bucket?: string;
  prefix?: string;
  region?: string;
}

export interface ArtifactDeclaration {
  paths: string[];
  destination?: ArtifactDestination;
}

export interface ArtifactFile {
  path: string;
  /** Pre-signed direct-download URL (no auth header required). */
  url?: string;
  sizeBytes: number;
  contentType?: string;
}

export interface ArtifactInfo {
  id: string;
  status: "pending" | "ready" | "failed";
  sessionId?: string;
  capturedAt?: string;
  expiresAt?: string;
  files?: ArtifactFile[];
  error?: string;
}

export interface CaptureArtifactsOptions {
  destination?: ArtifactDestination;
}

// ---------------------------------------------------------------------------
// Network / expose
// ---------------------------------------------------------------------------

export interface ExposeResult {
  sessionId: string;
  port: number;
  previewUrl: string;
  ingressToken?: string;
  guestIp?: string;
  auth?: "none" | "bearer";
  /** Bearer token for inbound requests. Returned once, only when auth is "bearer". */
  authToken?: string;
}

export interface ExposeOptions {
  visibility?: "token" | "public";
  auth?: "none" | "bearer";
}

// ---------------------------------------------------------------------------
// Org / capacity
// ---------------------------------------------------------------------------

// ---------------------------------------------------------------------------
// Files: batch write, find, grep
// ---------------------------------------------------------------------------

export interface FileBatchItem {
  path: string;
  content: string;
  encoding?: "text" | "base64";
}

/** One file's outcome in a batch write. Not fail-fast: one bad path never sinks the rest. */
export interface FileBatchResult {
  path: string;
  ok: boolean;
  error?: string;
}

export interface FindFilesOptions {
  /** Base directory under /workspace to search from. Defaults to /workspace. */
  path?: string;
  /** Caps the number of matching paths returned; sets `truncated` rather than erroring. */
  maxResults?: number;
}

export interface FindFilesResult {
  paths: string[];
  /** True when maxResults capped the result set — narrow the pattern or raise it for the rest. */
  truncated: boolean;
}

/** One line hit from grepFiles. */
export interface GrepMatch {
  path: string;
  lineNumber: number;
  line: string;
  /** Up to contextLines lines immediately before `line`, in file order. */
  before?: string[];
  /** Up to contextLines lines immediately after `line`, in file order. */
  after?: string[];
}

export interface GrepFilesOptions {
  /** Base directory under /workspace to search from. Defaults to /workspace. */
  path?: string;
  /** Restrict to files whose base name matches this glob, e.g. "*.go". */
  filePattern?: string;
  /** Case-sensitive matching. Defaults to case-insensitive. */
  caseSensitive?: boolean;
  /** Lines of context before/after each match (capped). */
  contextLines?: number;
  /** Caps the number of matching FILES returned (not match lines); sets `truncated` rather than erroring. */
  maxResults?: number;
  /** Directory names to skip entirely, e.g. ["node_modules", ".git"]. */
  excludeDirs?: string[];
}

export interface GrepFilesResult {
  matches: GrepMatch[];
  /** Files actually scanned (binary files are skipped automatically). */
  filesSearched: number;
  /** True when a result cap was hit — narrow the pattern or raise it for the rest. */
  truncated: boolean;
}

export interface CapacityLimits {
  running: number;
  warm: number;
}

export interface Capacity {
  running: number;
  warm: number;
  limits?: CapacityLimits;
}

// ---------------------------------------------------------------------------
// Client options
// ---------------------------------------------------------------------------

export interface ClientOptions {
  token?: string;
  baseUrl?: string;
  timeoutMs?: number;
}

// ---------------------------------------------------------------------------
// Session class
// ---------------------------------------------------------------------------

export class Session {
  readonly sessionId: string;

  constructor(
    private readonly _baseUrl: string,
    private readonly _token: string,
    private readonly _timeoutMs: number,
    sessionId: string,
  ) {
    this.sessionId = sessionId;
  }

  // ---- Factory ----

  /** Create a new session via the API. Prefer client.sessions.create(). */
  static async create(_client: Client, _opts?: CreateSessionOptions): Promise<Session> {
    throw new Error("not implemented");
  }

  /** Wrap an existing session ID in a local handle without hitting the API. */
  static attach(_client: Client, _sessionId: string): Session {
    throw new Error("not implemented");
  }

  // ---- Execution ----

  /**
   * Run a command in this session.
   * A string is wrapped as ["bash", "-lc", command]; an array is exec'd directly.
   */
  async exec(
    _command: string | string[],
    _opts?: { cwd?: string; timeoutMs?: number },
  ): Promise<ExecResult> {
    throw new Error("not implemented");
  }

  /**
   * Run a command inside this session, streaming stdout/stderr live via
   * Server-Sent Events instead of waiting for it to finish.
   *
   * Yields an ExecStreamEvent per chunk as the guest produces it, ending
   * with exactly one terminal event: stream="exit" (exitCode/timedOut/
   * durationMs populated) once the command completes or its timeout fires,
   * or stream="error" on a guest-side failure. Breaking out of the loop
   * early closes the underlying connection, which the server takes as a
   * disconnect and kills the command.
   */
  async *execStream(
    _command: string | string[],
    _opts?: { cwd?: string; timeoutMs?: number },
  ): AsyncGenerator<ExecStreamEvent> {
    throw new Error("not implemented");
  }

  /**
   * Run code in this session's persistent Python interpreter context.
   * Unlike exec(), variables/imports/functions/classes survive across
   * calls until restartContext() or the session ends. Built
   * entirely on exec/writeFile/readFile/startProcess (no guest-agent or
   * template changes) — see docs/api-design/typescript-sdk.md#code-interpreter.
   */
  async runCode(
    _code: string,
    _opts?: { timeoutMs?: number },
  ): Promise<InterpreterResult[]> {
    throw new Error("not implemented");
  }

  /**
   * Discard this session's interpreter context: kills the running kernel
   * process (if any) and starts a fresh one.
   */
  async restartContext(): Promise<void> {
    throw new Error("not implemented");
  }

  /**
   * Open an interactive pseudo-terminal in this session, over the
   * same WebSocket endpoint and binary frame protocol the console's web
   * terminal uses. command defaults to the guest's login shell; pass e.g.
   * ["python3", "-i"] to open a REPL directly.
   */
  async pty(_opts?: PTYOptions): Promise<PTY> {
    throw new Error("not implemented");
  }

  // ---- Lifecycle ----

  /** Fetch the current full state of this session from the API. */
  async get(): Promise<SessionInfo> {
    throw new Error("not implemented");
  }

  /** Suspend the session: snapshot to local disk, free the VM slot. */
  async pause(): Promise<SessionInfo> {
    throw new Error("not implemented");
  }

  /** Resume a paused session back to a live VM. */
  async resume(): Promise<SessionInfo> {
    throw new Error("not implemented");
  }

  /**
   * Fork this live session into `count` independent children.
   * Returns the child session IDs; the parent keeps running.
   */
  async fork(_count: number): Promise<ForkResult> {
    throw new Error("not implemented");
  }

  /**
   * Retry the durable (cold) upload of this paused session's snapshot after
   * a previous attempt failed or stalled. The session must be paused.
   */
  async archiveRetry(): Promise<ArchiveStatus> {
    throw new Error("not implemented");
  }

  /** Destroy the session and free all its resources. */
  async destroy(_opts?: DestroyOptions): Promise<void> {
    throw new Error("not implemented");
  }

  // ---- Checkpoints ----

  /** Capture a named, retained checkpoint of this live session. */
  async checkpoint(_opts?: CheckpointOptions): Promise<CheckpointInfo> {
    throw new Error("not implemented");
  }

  /** List checkpoints captured from this session. */
  async checkpoints(): Promise<CheckpointInfo[]> {
    throw new Error("not implemented");
  }

  /**
   * Roll this session back to a checkpoint in place.
   * The VM is stopped and restored; networking is rebound. Returns full session state.
   */
  async restore(_checkpointId: string): Promise<SessionInfo> {
    throw new Error("not implemented");
  }

  // ---- Templates ----

  /**
   * Promote the current session state into a named, reusable template.
   * The session keeps running; new sessions can start from this template by name.
   */
  async saveAsTemplate(_name: string, _opts?: SaveAsTemplateOptions): Promise<TemplateInfo> {
    throw new Error("not implemented");
  }

  // ---- Artifacts ----

  /**
   * Capture output files to durable storage asynchronously.
   * Returns a pending manifest immediately. Poll client.artifacts.get(id)
   * until status === "ready" for pre-signed download URLs.
   */
  async captureArtifacts(_paths: string[], _opts?: CaptureArtifactsOptions): Promise<ArtifactInfo> {
    throw new Error("not implemented");
  }

  /** List artifact captures recorded for this session. */
  async artifacts(): Promise<ArtifactInfo[]> {
    throw new Error("not implemented");
  }

  // ---- Network ----

  /**
   * Expose a guest port as a preview URL.
   * The bare string form ("token" | "public") is accepted for back-compat.
   */
  async expose(
    _port: number,
    _options?: ExposeOptions | "token" | "public",
  ): Promise<ExposeResult> {
    throw new Error("not implemented");
  }

  /** Remove an exposed port's preview URL. Returns true when the port was found. */
  async unexpose(_port: number): Promise<boolean> {
    throw new Error("not implemented");
  }

  /** Get the current outbound egress policy for this session. */
  async getEgress(): Promise<EgressPolicy> {
    throw new Error("not implemented");
  }

  /** Replace the outbound egress policy on this live session. */
  async setEgress(_policy: EgressPolicy): Promise<EgressPolicy> {
    throw new Error("not implemented");
  }

  // ---- Files ----

  /**
   * Read a file from the session workspace.
   * GET /v1/sessions/{id}/files?path=<path>&op=read[&encoding=base64]
   * encoding="base64" reads the file as binary and returns the decoded raw bytes.
   */
  async readFile(_path: string, _encoding?: "text"): Promise<string>;
  async readFile(_path: string, _encoding: "base64"): Promise<Uint8Array>;
  async readFile(_path: string, _encoding?: "text" | "base64"): Promise<string | Uint8Array> {
    throw new Error("not implemented");
  }

  /** List files in a directory in the session workspace. GET /v1/sessions/{id}/files?path=<path>&op=list */
  async listFiles(_path: string): Promise<string[]> {
    throw new Error("not implemented");
  }

  /**
   * Write a file to the session workspace. PUT /v1/sessions/{id}/files
   * Pass raw bytes to write binary content — base64-encoded on the wire automatically.
   */
  async writeFile(
    _path: string,
    _content: string | Uint8Array,
    _encoding?: "text" | "base64",
  ): Promise<void> {
    throw new Error("not implemented");
  }

  /**
   * Write multiple small files under /workspace in one call.
   * PUT /v1/sessions/{id}/files/batch. Capped at 64 files / 8 MiB total content —
   * for bulk import use artifact capture (export) or exec/SSH instead. Not
   * fail-fast: one bad path never sinks the rest of the batch.
   */
  async writeFiles(_files: FileBatchItem[]): Promise<FileBatchResult[]> {
    throw new Error("not implemented");
  }

  /**
   * Find file paths under the workspace matching a glob (exact path,
   * "*"/"?"/"[...]", or a trailing "/**" for recursive).
   * GET /v1/sessions/{id}/files?op=find
   */
  async findFiles(_pattern: string, _opts?: FindFilesOptions): Promise<FindFilesResult> {
    throw new Error("not implemented");
  }

  /**
   * Search file contents under the workspace with a regex (Go RE2 syntax —
   * no PCRE lookaround/backrefs). Binary files are skipped automatically.
   * GET /v1/sessions/{id}/files?op=grep
   */
  async grepFiles(_pattern: string, _opts?: GrepFilesOptions): Promise<GrepFilesResult> {
    throw new Error("not implemented");
  }

  /**
   * Create a directory (with parents) in the session workspace. Always
   * `mkdir -p` semantics. POST /v1/sessions/{id}/files/mkdir
   */
  async mkdir(_path: string): Promise<void> {
    throw new Error("not implemented");
  }

  /**
   * Remove a file, or a directory (with recursive=true).
   * DELETE /v1/sessions/{id}/files?path=<path>&recursive=<bool>
   */
  async remove(_path: string, _opts?: { recursive?: boolean }): Promise<void> {
    throw new Error("not implemented");
  }

  /**
   * Rename or move a file or directory.
   * POST /v1/sessions/{id}/files/rename
   */
  async rename(_path: string, _destPath: string): Promise<void> {
    throw new Error("not implemented");
  }

  /**
   * Stream a local file into the session workspace — not subject to
   * writeFiles' 64-file/8 MiB batch cap. PUT /v1/sessions/{id}/files/stream
   */
  async uploadFile(
    _localPath: string,
    _remotePath: string,
  ): Promise<{ path: string; bytes: number; sha256: string; durationMs: number }> {
    throw new Error("not implemented");
  }

  /**
   * Stream a file from the session workspace to local disk — not subject to
   * readFile's 4 MiB whole-file cap. GET /v1/sessions/{id}/files/stream
   */
  async downloadFile(
    _remotePath: string,
    _localPath: string,
  ): Promise<{ path: string; localPath: string; bytes: number; sha256: string }> {
    throw new Error("not implemented");
  }
}

// ---------------------------------------------------------------------------
// Client class
// ---------------------------------------------------------------------------

export class Client {
  readonly baseUrl: string;
  readonly token: string;
  readonly timeoutMs: number;

  // ---- sessions namespace ----

  readonly sessions: {
    /** Create a new session and return a Session handle. */
    create(opts?: CreateSessionOptions): Promise<Session>;

    /**
     * Wrap an existing session ID in a local Session handle.
     * Does not call the API — use session.get() to fetch current state.
     */
    attach(sessionId: string): Session;

    /** List sessions, optionally filtered by state. Paginated via cursor. */
    list(opts?: ListSessionsOptions): Promise<{ sessions: SessionInfo[]; nextCursor?: string }>;

    /** Fetch the current full state of a session by ID. */
    get(sessionId: string): Promise<SessionInfo>;

    /** Pause a session (snapshot to disk, free the VM slot). */
    pause(sessionId: string): Promise<SessionInfo>;

    /** Resume a paused session back to a live VM. */
    resume(sessionId: string): Promise<SessionInfo>;

    /**
     * Fork a session into `count` independent children.
     * Returns the child session IDs; the parent keeps running.
     */
    fork(sessionId: string, count: number): Promise<ForkResult>;

    /**
     * Retry the durable cold upload for a paused session after
     * a previous attempt failed or stalled.
     */
    archiveRetry(sessionId: string): Promise<ArchiveStatus>;

    /**
     * Fan a command across a set of sessions in parallel.
     * All sessions receive the same argv; results arrive together.
     */
    batchExec(opts: BatchExecOptions): Promise<BatchExecResult[]>;

    /**
     * Read a file from a session workspace. GET /v1/sessions/{id}/files?path=<path>&op=read
     * encoding="base64" reads the file as binary and returns the decoded raw bytes.
     */
    readFile(sessionId: string, path: string, encoding?: "text" | "base64"): Promise<string | Uint8Array>;

    /** List files in a directory in a session workspace. GET /v1/sessions/{id}/files?path=<path>&op=list */
    listFiles(sessionId: string, path: string): Promise<string[]>;

    /** Write a file to a session workspace. PUT /v1/sessions/{id}/files */
    writeFile(
      sessionId: string,
      path: string,
      content: string | Uint8Array,
      encoding?: "text" | "base64",
    ): Promise<void>;

    /** Write multiple small files under /workspace in one call (max 64 files / 8 MiB total). */
    writeFiles(sessionId: string, files: FileBatchItem[]): Promise<FileBatchResult[]>;

    /** Find file paths under a session workspace matching a glob. */
    findFiles(sessionId: string, pattern: string, opts?: FindFilesOptions): Promise<FindFilesResult>;

    /** Search file contents under a session workspace with a regex (Go RE2 syntax). */
    grepFiles(sessionId: string, pattern: string, opts?: GrepFilesOptions): Promise<GrepFilesResult>;
  };

  // ---- checkpoints namespace ----

  readonly checkpoints: {
    /** Fetch checkpoint metadata. */
    get(checkpointId: string): Promise<CheckpointInfo>;

    /** Delete a checkpoint and free its disk. Deletion is explicit. */
    delete(checkpointId: string): Promise<void>;

    /**
     * Start a new, independent session from a checkpoint.
     * The new session diverges independently — this is the template mechanism.
     */
    createSession(checkpointId: string, opts?: CheckpointSessionOptions): Promise<Session>;
  };

  // ---- templates namespace ----

  readonly templates: {
    /** List official and user templates available to this account. */
    list(): Promise<TemplateInfo[]>;

    /** Import a PUBLIC OCI image (Docker Hub / GHCR anonymous pull) as a custom
     * template. The build runs asynchronously — the returned template starts in
     * status "queued", moves to "building", then usable at "ready". */
    import(fromImage: string, name: string, opts?: { description?: string }): Promise<TemplateInfo>;

    /** Delete a user-published template. Official templates cannot be deleted. */
    delete(name: string): Promise<void>;
  };

  // ---- registries namespace ----

  /** Private-registry pull credentials for template import — storing
   * one here doesn't change how templates.import is called; the import
   * matches a stored credential to the pull by the image ref's registry
   * host automatically. */
  readonly registries: {
    /** List your stored registry credentials (redacted, never the secret). */
    list(): Promise<RegistryCredentialInfo[]>;

    /** Store a credential under `name`. Covers Docker Hub, GHCR, and Google
     * Artifact Registry. Rotation is delete-then-create for v1 — reusing a
     * name already in use rejects with a 409. */
    add(name: string, opts: AddRegistryCredentialOptions): Promise<RegistryCredentialInfo>;

    /** Delete a stored registry credential. Idempotent. */
    delete(name: string): Promise<void>;
  };

  // ---- connectors namespace ----

  /** Egress connectors: credential injection without the sandbox ever
   * seeing the secret. Register one here, then opt a session into it by
   * name (`sessions.create({ connectors: ["stripe"] })`). */
  readonly connectors: {
    /** List your stored connectors (redacted, never the secret). */
    list(): Promise<ConnectorInfo[]>;

    /** Register a connector under `name`. Rotation is delete-then-create
     * for v1 — reusing a name already in use rejects with a 409. */
    add(name: string, opts: AddConnectorOptions): Promise<ConnectorInfo>;

    /** Delete a stored connector. Idempotent. */
    delete(name: string): Promise<void>;
  };

  // ---- webhooks namespace ----

  /** Webhook subscriptions for session events: a subscription receives the
   * same event envelope the account-scoped event stream (`GET /v1/events`)
   * sends, signed with HMAC-SHA256 (`Inis-Signature: t=<ts>,v1=<hex>`) and
   * retried with bounded exponential backoff. */
  readonly webhooks: {
    /** List this org's webhook subscriptions (secret never included). */
    list(): Promise<WebhookEndpointInfo[]>;

    /** Register `url`. The returned object's `secret` is the ONLY time the
     * raw signing secret is available — store it now. */
    add(url: string, opts?: AddWebhookOptions): Promise<WebhookEndpointInfo>;

    /** Delete a webhook subscription. */
    delete(endpointId: string): Promise<void>;

    /** Fire one synthetic test delivery immediately (bypassing the retry
     * queue) and return the delivered/dead result synchronously. */
    test(endpointId: string): Promise<WebhookDeliveryInfo>;

    /** The most recent deliveries for a subscription, newest first — the
     * per-subscription delivery log. */
    deliveries(endpointId: string, limit?: number): Promise<WebhookDeliveryInfo[]>;
  };

  // ---- domains namespace ----

  /** Custom domains with auto-TLS for exposed session ports: CNAME a
   * customer-owned hostname to the fleet's ingress, verify DNS ownership,
   * then route it to a session's exposed port in place of the default
   * preview URL. */
  readonly domains: {
    /** List this org's registered domains, verified and unverified. */
    list(): Promise<DomainInfo[]>;

    /** Register `domain` as a pending (unverified) custom domain. The
     * response's `verifyTxtName`/`verifyTxtValue` are the TXT record to
     * create before calling `verify()` (a CNAME to the fleet's ingress
     * also satisfies verification). */
    add(domain: string): Promise<DomainInfo>;

    /** Delete a registered custom domain. */
    delete(domainId: string): Promise<void>;

    /** Run a live DNS check and mark the domain verified on success. Safe
     * to call repeatedly; throws if verification fails. */
    verify(domainId: string): Promise<DomainInfo>;

    /** Bind a verified domain to sessionId's exposed port. Throws if the
     * domain isn't verified yet. */
    route(domainId: string, sessionId: string, port: number): Promise<DomainInfo>;

    /** Clear a domain's session/port binding without deleting the
     * registration — verified state is preserved for a later rebind. */
    unroute(domainId: string): Promise<DomainInfo>;
  };

  // ---- artifacts namespace ----

  readonly artifacts: {
    /** Fetch an artifact manifest (status + pre-signed download URLs). */
    get(artifactId: string): Promise<ArtifactInfo>;

    /** Extend an artifact's retention (capped at the hard limit). */
    extend(artifactId: string, ttlDays: number): Promise<ArtifactInfo>;

    /** Delete an artifact's stored files and its record. */
    delete(artifactId: string): Promise<void>;
  };

  constructor(opts: ClientOptions = {}) {
    // Stub: field values are placeholders; real implementation resolves from
    // opts and environment variables (INIS_API_KEY, INIS_BASE_URL).
    void opts;
    this.token = "";
    this.baseUrl = "";
    this.timeoutMs = 120_000;

    this.sessions = {
      create: (_opts?: CreateSessionOptions): Promise<Session> => {
        throw new Error("not implemented");
      },
      attach: (_sessionId: string): Session => {
        throw new Error("not implemented");
      },
      list: (_opts?: ListSessionsOptions): Promise<{ sessions: SessionInfo[]; nextCursor?: string }> => {
        throw new Error("not implemented");
      },
      get: (_sessionId: string): Promise<SessionInfo> => {
        throw new Error("not implemented");
      },
      pause: (_sessionId: string): Promise<SessionInfo> => {
        throw new Error("not implemented");
      },
      resume: (_sessionId: string): Promise<SessionInfo> => {
        throw new Error("not implemented");
      },
      fork: (_sessionId: string, _count: number): Promise<ForkResult> => {
        throw new Error("not implemented");
      },
      archiveRetry: (_sessionId: string): Promise<ArchiveStatus> => {
        throw new Error("not implemented");
      },
      batchExec: (_opts: BatchExecOptions): Promise<BatchExecResult[]> => {
        throw new Error("not implemented");
      },
      readFile: (
        _sessionId: string,
        _path: string,
        _encoding?: "text" | "base64",
      ): Promise<string | Uint8Array> => {
        throw new Error("not implemented");
      },
      listFiles: (_sessionId: string, _path: string): Promise<string[]> => {
        throw new Error("not implemented");
      },
      writeFile: (
        _sessionId: string,
        _path: string,
        _content: string | Uint8Array,
        _encoding?: "text" | "base64",
      ): Promise<void> => {
        throw new Error("not implemented");
      },
      writeFiles: (_sessionId: string, _files: FileBatchItem[]): Promise<FileBatchResult[]> => {
        throw new Error("not implemented");
      },
      findFiles: (_sessionId: string, _pattern: string, _opts?: FindFilesOptions): Promise<FindFilesResult> => {
        throw new Error("not implemented");
      },
      grepFiles: (_sessionId: string, _pattern: string, _opts?: GrepFilesOptions): Promise<GrepFilesResult> => {
        throw new Error("not implemented");
      },
    };

    this.checkpoints = {
      get: (_checkpointId: string): Promise<CheckpointInfo> => {
        throw new Error("not implemented");
      },
      delete: (_checkpointId: string): Promise<void> => {
        throw new Error("not implemented");
      },
      createSession: (_checkpointId: string, _opts?: CheckpointSessionOptions): Promise<Session> => {
        throw new Error("not implemented");
      },
    };

    this.templates = {
      list: (): Promise<TemplateInfo[]> => {
        throw new Error("not implemented");
      },
      import: (_fromImage: string, _name: string, _opts?: { description?: string }): Promise<TemplateInfo> => {
        throw new Error("not implemented");
      },
      delete: (_name: string): Promise<void> => {
        throw new Error("not implemented");
      },
    };

    this.registries = {
      list: (): Promise<RegistryCredentialInfo[]> => {
        throw new Error("not implemented");
      },
      add: (_name: string, _opts: AddRegistryCredentialOptions): Promise<RegistryCredentialInfo> => {
        throw new Error("not implemented");
      },
      delete: (_name: string): Promise<void> => {
        throw new Error("not implemented");
      },
    };

    this.connectors = {
      list: (): Promise<ConnectorInfo[]> => {
        throw new Error("not implemented");
      },
      add: (_name: string, _opts: AddConnectorOptions): Promise<ConnectorInfo> => {
        throw new Error("not implemented");
      },
      delete: (_name: string): Promise<void> => {
        throw new Error("not implemented");
      },
    };

    this.webhooks = {
      list: (): Promise<WebhookEndpointInfo[]> => {
        throw new Error("not implemented");
      },
      add: (_url: string, _opts?: AddWebhookOptions): Promise<WebhookEndpointInfo> => {
        throw new Error("not implemented");
      },
      delete: (_endpointId: string): Promise<void> => {
        throw new Error("not implemented");
      },
      test: (_endpointId: string): Promise<WebhookDeliveryInfo> => {
        throw new Error("not implemented");
      },
      deliveries: (_endpointId: string, _limit?: number): Promise<WebhookDeliveryInfo[]> => {
        throw new Error("not implemented");
      },
    };

    this.domains = {
      list: (): Promise<DomainInfo[]> => {
        throw new Error("not implemented");
      },
      add: (_domain: string): Promise<DomainInfo> => {
        throw new Error("not implemented");
      },
      delete: (_domainId: string): Promise<void> => {
        throw new Error("not implemented");
      },
      verify: (_domainId: string): Promise<DomainInfo> => {
        throw new Error("not implemented");
      },
      route: (_domainId: string, _sessionId: string, _port: number): Promise<DomainInfo> => {
        throw new Error("not implemented");
      },
      unroute: (_domainId: string): Promise<DomainInfo> => {
        throw new Error("not implemented");
      },
    };

    this.artifacts = {
      get: (_artifactId: string): Promise<ArtifactInfo> => {
        throw new Error("not implemented");
      },
      extend: (_artifactId: string, _ttlDays: number): Promise<ArtifactInfo> => {
        throw new Error("not implemented");
      },
      delete: (_artifactId: string): Promise<void> => {
        throw new Error("not implemented");
      },
    };
  }

  // ---- top-level methods ----

  /**
   * One-shot convenience: creates a throwaway session, runs the code,
   * returns the result, and destroys the session. No session handle is exposed.
   */
  async execute(_opts: {
    language: "python" | "node" | "bun";
    code: string;
    dependencies?: string[];
    volumeId?: string;
    timeoutMs?: number;
    size?: SessionSize;
    /** Base environment (omit or "base" for default; "name@version" to pin). */
    template?: string;
    /** Harden the sandbox for untrusted code by removing passwordless sudo. */
    noSudo?: boolean;
  }): Promise<ExecResult> {
    throw new Error("not implemented");
  }

  /** Aggregate session capacity and per-account limits. */
  async capacity(): Promise<Capacity> {
    throw new Error("not implemented");
  }
}
