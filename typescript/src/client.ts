// node:fs / node:crypto are used only by uploadFile/downloadFile —
// this SDK targets Node (>=18 per package.json engines), not the browser, so
// local-disk streaming is in scope; every other method in this file uses
// only ambient fetch and needs no Node-specific import.
import { createReadStream, createWriteStream } from "node:fs";
import { createHash } from "node:crypto";
import { Readable } from "node:stream";
// Type-only: erased at compile time, so this never creates a runtime import
// cycle with pty.ts (which imports the real, value-level InisError from this
// file). Session.pty() below loads the PTY class itself via a dynamic
// import() at call time.
import type { PTY, PTYOptions } from "./pty.js";
import * as interp from "./interpreter.js";
import type { InterpreterResult } from "./interpreter.js";

export type { InterpreterResult } from "./interpreter.js";

/** Structured detail attached to an {@link InisError}. */
export interface InisErrorDetail {
  /** Stable machine-readable code from the server; "error" if the response carried none at all. */
  code: string;
  /** HTTP status, undefined if the failure was below the HTTP layer (e.g. a network error). */
  status?: number;
  /** Whether a caller can reasonably expect success from simply retrying the identical request after backing off. */
  retryable: boolean;
  /** Seconds to wait before retrying, from a Retry-After header; undefined if the server sent none. */
  retryAfter?: number;
  /** X-Inis-Request-Id, if the server sent one — safe to quote to support. */
  requestId?: string;
  /** Raw decoded JSON error body, or undefined for a non-JSON/empty body. Nothing the server sent is lost here, including a code value this SDK version has never seen. */
  response?: Record<string, unknown>;
}

export class InisError extends Error {
  /** Stable machine-readable code; "error" if the response carried none. Passed through raw even for a code this SDK predates — never collapsed to message-only. */
  readonly code: string;
  readonly status?: number;
  readonly retryable: boolean;
  readonly retryAfter?: number;
  readonly requestId?: string;
  readonly response?: Record<string, unknown>;

  constructor(message: string, detail?: Partial<InisErrorDetail>) {
    super(message);
    this.name = "InisError";
    this.code = detail?.code ?? "error";
    this.status = detail?.status;
    this.retryable = detail?.retryable ?? false;
    this.retryAfter = detail?.retryAfter;
    this.requestId = detail?.requestId;
    this.response = detail?.response;
  }
}

// Codes an SDK caller can reasonably expect to succeed by simply
// retrying the identical request after backing off, as opposed to a failure
// that needs the caller (bad input, expired credential) or an operator
// (fleet capacity, cold-tier config) to change something first. Mirrors
// internal/errcode.Retryable in the Go codebase (the single source of truth
// there) by hand — this SDK can't import a Go package — and
// sdk/python/inis/client.py's _RETRYABLE_CODES mirrors it a third time. Keep
// all three in sync when this list changes.
//
// Explicitly NOT retryable despite sharing an HTTP status class with a
// retryable code: session_ended (410, definitively over), archive_unavailable
// (503, needs a node/cold-tier config change), size_unavailable (503, a
// fleet-capacity fact not a transient dip), payload_too_large (400,
// deterministic in the request content), method_not_allowed (405, client
// misuse — retrying the identical wrong-method request fails identically).
// Any code not listed here defaults to non-retryable — the safe choice for a
// code this SDK has never seen.
const RETRYABLE_CODES = new Set([
  "session_not_live",
  "session_unavailable",
  "session_node_unavailable",
  "rate_limited",
  "unavailable",
  "template_version_unavailable",
  "bad_gateway",
  // Legacy, status-derived codes that predate the errcode taxonomy but mean
  // the same retryable 429/503/502 thing.
  "concurrency_limit_exceeded",
  "rate_limit_exceeded",
  "service_unavailable",
]);

const REQUEST_ID_HEADER = "x-inis-request-id";

/**
 * Parse an HTTP Retry-After header: an integer delta-seconds (what every
 * writer in this codebase actually sends — internal/api/server.go's
 * writeRetryableError et al. always emit a bare integer) or, for
 * robustness, an RFC 9110 HTTP-date.
 */
function parseRetryAfter(value: string): number | undefined {
  const trimmed = value.trim();
  const asNumber = Number(trimmed);
  if (!Number.isNaN(asNumber) && trimmed !== "") return asNumber;
  const asDate = new Date(trimmed);
  if (Number.isNaN(asDate.getTime())) return undefined;
  return Math.max((asDate.getTime() - Date.now()) / 1000, 0);
}

/**
 * Build a fully-populated InisError from a non-ok fetch Response and its
 * already-read body text (fetch response bodies can only be read once, so
 * every call site reads .text() itself and hands it here rather than this
 * function re-reading it). The single choke point every request path in
 * this file funnels through — do not hand-roll another
 * `JSON.parse(text).error ?? text` block; call this instead.
 */
function buildInisError(resp: Response, text: string): InisError {
  let body: Record<string, unknown> | undefined;
  try {
    const parsed: unknown = text ? JSON.parse(text) : undefined;
    if (parsed && typeof parsed === "object") body = parsed as Record<string, unknown>;
  } catch {
    body = undefined;
  }
  const message = (typeof body?.error === "string" ? body.error : undefined) || text || `HTTP ${resp.status}`;
  const code = (typeof body?.code === "string" ? body.code : undefined) || "error";

  let retryable = RETRYABLE_CODES.has(code);
  let retryAfter: number | undefined;
  const ra = resp.headers.get("retry-after");
  if (ra) {
    const parsedRA = parseRetryAfter(ra);
    if (parsedRA !== undefined) {
      retryAfter = parsedRA;
      // A Retry-After header is itself a stronger, independent
      // retryability signal than the static code table above: the server
      // sending one is a direct statement "retry is fine, here's when" —
      // honour it even for a code not in RETRYABLE_CODES yet (e.g. a
      // future code this SDK predates).
      retryable = true;
    }
  }
  const requestId = resp.headers.get(REQUEST_ID_HEADER) ?? undefined;

  return new InisError(`${resp.status}: ${message}`, {
    code,
    status: resp.status,
    retryable,
    retryAfter,
    requestId,
    response: body,
  });
}

/** Sandbox tier: small (1 vCPU/1 GB), medium (2/4 GB), large (4/8 GB). */
export type SessionSize = "small" | "medium" | "large";

/** Lifecycle states a session can be in. */
export type SessionState = "live" | "paused" | "creating" | "failed" | "ended";

/**
 * Coarse, user-safe provisioning phase, present only while state=="creating".
 * "preparing_template" (materializing the starting template — the
 * slow case, a cold bundle pull) and "starting" (restore + guest warm-up)
 * are set by inisd today; "waiting_for_capacity" is reserved for a future
 * router/autoscaler-level wait.
 */
export type SessionStatusDetail = "waiting_for_capacity" | "preparing_template" | "starting";

/** What the server does when the last PTY client disconnects. */
export type OnPtyDetach = "keep_live" | "pause" | "destroy";

/** Why a session ended. Present only on state="ended" history rows. */
export type EndReason =
  | "client_destroy"
  | "shell_exit"
  | "on_detach_destroy"
  | "max_lifetime"
  | "paused_ttl"
  | "error";

export interface ArchiveStatus {
  status: "pending" | "complete" | "failed";
  coldUri?: string;
  uploadedAt?: string;
}

export interface EgressPolicy {
  /** "allow" (default) keeps full public egress; "deny" restricts to the allow list. */
  mode?: "allow" | "deny";
  /** Domains reachable in deny mode: exact ("api.openai.com") or "*.wildcard". */
  allow?: string[];
}

export interface ExposedPreview {
  port: number;
  previewUrl: string;
  visibility: "token" | "public";
  auth?: "none" | "bearer";
}

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
  truncated: boolean;
}

/** One line hit from grepFiles. */
export interface GrepMatch {
  path: string;
  lineNumber: number;
  line: string;
  before?: string[];
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
  filesSearched: number;
  truncated: boolean;
}

export interface SessionInfo {
  sessionId: string;
  state: SessionState;
  /** Set only while state=="creating": the current provisioning phase. */
  statusDetail?: SessionStatusDetail;
  /** Set only when state=="failed": a short, user-safe reason. */
  statusReason?: string;

  // Identity
  name?: string;
  labels?: Record<string, string>;

  // Lifecycle timestamps
  createdAt?: string;
  lastActiveAt?: string;
  endedAt?: string;
  endReason?: EndReason;

  // Behaviour
  idleTimeoutMs?: number;
  maxLifetimeMs?: number;
  onPtyDetach?: OnPtyDetach;
  pausedTtlMs?: number;
  noSudo?: boolean;

  // Resources
  size?: SessionSize;
  vcpus?: number;
  memMb?: number;
  nodeId?: string;
  volumeId?: string;
  template?: string;

  // Network
  exposedPorts?: number[];
  exposedPreviews?: ExposedPreview[];
  egress?: EgressPolicy;

  // Durable archive
  archive?: ArchiveStatus;
  mcpUrl?: string;

  /** When the current continuous-live stretch began (RFC 3339). Set only
   * while live with a max-active-window armed; the session auto-pauses at
   * activeSince + maxActiveWindowMs. */
  activeSince?: string;
  /** The continuous-active window (ms) the current live stretch was armed
   * with. Set only while live with the window armed. */
  maxActiveWindowMs?: number;
  /** Resume-readiness of a PAUSED session: "hot" (instant), "warm" (quick
   * local decompress), or "cold" (needs a download). Undefined for
   * non-paused sessions. */
  snapshotTier?: "hot" | "warm" | "cold";

  /** Plain (non-sensitive) environment variables set on this session. */
  env?: Record<string, string>;
  /**
   * Names of the secrets configured on this session — NEVER their values.
   * The API never returns secret values once set.
   */
  secretNames?: string[];
  /**
   * Egress connectors this session has opted into, by name — never any
   * secret material.
   */
  connectorNames?: string[];
}

export interface ExecResult {
  stdout: string;
  stderr: string;
  exitCode: number;
  durationMs: number;
  timedOut: boolean;
  restoreMs?: number;
  installMs?: number;
  phase?: string;
  /**
   * True when stdout/stderr are an incomplete view of what the command
   * actually produced -- the guest's own per-stream capture cap cut a large
   * stream before it hit any size limit of this SDK's own. Only exec()/
   * runCode() can set this; execStream() never truncates (see ExecStreamEvent).
   */
  truncated?: boolean;
  /**
   * undefined (the default) means stdout is plain text, as always. "base64"
   * means that stream's output was not valid UTF-8, so the API returned it
   * base64-encoded rather than risk silently corrupting it -- decode with
   * `Buffer.from(result.stdout, "base64")` (Node) or an equivalent to get
   * the exact bytes the command produced. Only exec()/runCode() can set
   * this.
   */
  stdoutEncoding?: "base64";
  /** Same as stdoutEncoding, for stderr. */
  stderrEncoding?: "base64";
}

export interface ForkResult {
  parentSessionId: string;
  children: string[];
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

/** A named background process, running or exited, in a session. */
export interface ProcessInfo {
  name: string;
  state: "running" | "exited";
  pid?: number;
  command?: string[];
  exitCode?: number;
  keepAlive?: boolean;
  startedAt?: string;
  endedAt?: string;
}

export interface StartProcessOptions {
  cwd?: string;
  /** Suppresses the session's idle timer for as long as this process is running. */
  keepAlive?: boolean;
}

/** Buffered stdout/stderr captured so far for a named process. */
export interface ProcessLogs {
  name: string;
  stdout: string;
  stderr: string;
  /**
   * True when stdout/stderr are an incomplete view of the process's actual
   * output so far -- the guest's own per-read tail cap cut it.
   */
  truncated?: boolean;
  /** See ExecResult.stdoutEncoding for the full rationale. */
  stdoutEncoding?: "base64";
  /** Same as stdoutEncoding, for stderr. */
  stderrEncoding?: "base64";
}

/** One event from a live-followed process log stream (see Session.streamProcessLogs). */
export interface ProcessLogEvent {
  event: "stdout" | "stderr" | "eof" | "error";
  /** Decoded text for stdout/stderr/error; empty for eof. */
  data: string;
}

export interface BatchExecResult {
  sessionId: string;
  stdout: string;
  stderr: string;
  exitCode: number;
  durationMs: number;
  timedOut: boolean;
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

export interface DestroyOptions {
  reason?: "client_destroy" | "shell_exit";
}

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

export interface TemplateInfo {
  name: string;
  kind?: "official" | "user";
  description?: string;
  size?: SessionSize;
  version?: string;
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
  /** 1-based place in the node's serial build queue (1 == next to build).
   * Present only while status is "queued". */
  queuePosition?: number;
  /** When the current version's bundle was built (RFC3339 UTC). Official
   * templates only. */
  builtAt?: string;
  /** Upstream base image the bundle was built from, e.g.
   * docker.io/library/python:3.12-slim. Official templates only. */
  baseImageRef?: string;
  /** Content digest of the base image (sha256:...). Official templates only. */
  baseImageDigest?: string;
  /** Content hash of the built bundle — the tamper-evident identifier
   * recorded at publish and re-checked on restore. Official templates only. */
  contentHash?: string;
  /** Rebuild cadence for this template. Official templates only. */
  cadence?: "weekly" | "monthly";
  /** Grouping for the official set: "language" tracks an upstream base image,
   * "use-case" is a composed flavor built from a Dockerfile. Official
   * templates only. */
  category?: "language" | "use-case";
}

export interface SaveAsTemplateOptions {
  description?: string;
}

export interface ImportTemplateOptions {
  description?: string;
}

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
  /** Last 4 characters of the stored secret — a display aid, never enough
   * to reconstruct it. */
  secretLast4?: string;
  createdAt?: string;
  updatedAt?: string;
}

export interface AddConnectorOptions {
  targetBaseUrl: string;
  /** "bearer" (default, sets `Authorization: Bearer <secret>`) or "header"
   * (sets a custom header named `headerName` to the raw secret value). */
  authShape?: "bearer" | "header";
  /** Required when authShape is "header". */
  headerName?: string;
  secret: string;
}

/** A registered webhook subscription. `secret` is populated ONLY on
 * the response from `webhooks.add()` — store it immediately, it is never
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

/** A registered custom domain: CNAME a customer-owned hostname to the
 * fleet's ingress, verify DNS ownership, then route it to an exposed session
 * port for auto-provisioned TLS in place of the default preview URL.
 * `verifyTxtName`/`verifyTxtValue` are the TXT record to create before
 * calling `domains.verify()` (a CNAME to the fleet's ingress also satisfies
 * verification). v1 binds directly to a (sessionId, port) pair — the same
 * ephemeral unit preview URLs use — rather than a longer-lived project
 * route. */
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
  /** The hostname to CNAME this domain to so requests reach the fleet. TXT
   * verification only proves ownership — traffic still needs this record.
   * Undefined when the deployment has no ingress domain set. */
  ingressBaseDomain?: string;
}

export interface ArtifactDestination {
  type?: "hosted" | "s3";
  bucket?: string;
  prefix?: string;
  region?: string;
}

export interface ArtifactFile {
  path: string;
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

export interface ExposeResult {
  sessionId: string;
  port: number;
  previewUrl: string;
  ingressToken?: string;
  guestIp?: string;
  auth?: "none" | "bearer";
  authToken?: string;
}

export interface ExposeOptions {
  visibility?: "token" | "public";
  auth?: "none" | "bearer";
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

export interface ClientOptions {
  token?: string;
  baseUrl?: string;
  timeoutMs?: number;
}

export interface CreateSessionOptions {
  name?: string;
  labels?: Record<string, string>;
  volumeId?: string;
  maxLifetimeMs?: number;
  idleTimeoutMs?: number;
  egress?: EgressPolicy;
  template?: string;
  size?: SessionSize;
  noSudo?: boolean;
  onPtyDetach?: OnPtyDetach;
  pausedTtlMs?: number;
  destroyOnCompletion?: boolean;
  commandCapture?: "hash" | "executable" | "full";
  /**
   * Plain, non-sensitive environment variables (config/flags) set for every
   * process this session's exec/PTY spawns. Echoed back on the session
   * response. Use `secrets` for anything sensitive.
   */
  env?: Record<string, string>;
  /**
   * Sensitive environment variables. Encrypted at rest server-side
   * immediately on create, never logged, and decrypted only in memory at the
   * moment a guest request is sent. Never echoed back — `session.secretNames`
   * on the response shows only the configured names, never values.
   */
  secrets?: Record<string, string>;
  /**
   * Opts this session into stored egress connectors by name (see
   * `client.connectors`) — explicit, off by default. From inside the
   * guest, call `http://<its own default gateway>:18080/<name>/<path>` and
   * the host-side proxy injects the real credential; the guest never holds
   * or sees it. A name not listed here gets a distinct "not enabled" error,
   * even if the account has a connector by that name.
   */
  connectors?: string[];
  /**
   * Called with each coarse provisioning phase ("preparing_template",
   * "starting", ...) observed while Session.create waits for the session to
   * come up — most useful for a cold template pull, which can take tens of
   * seconds.
   */
  onStatus?: (detail: SessionStatusDetail) => void;
  /**
   * Creation is asynchronous on the server: Session.create still
   * blocks until the session is live by default (its historical contract),
   * polling under the hood. Pass `wait: false` to get the handle back as
   * soon as the create is acknowledged (state may still be "creating")
   * instead, and poll `session.get()` yourself.
   */
  wait?: boolean;
}

export interface ListSessionsOptions {
  state?: SessionState;
  limit?: number;
  cursor?: string;
}

const DEFAULT_BASE_URL = "https://api.inis.run";

function resolveToken(token?: string): string {
  const value = token ?? process.env.INIS_API_KEY;
  if (!value) throw new InisError("INIS_API_KEY is required");
  return value;
}

function resolveBaseUrl(baseUrl?: string): string {
  return (baseUrl ?? process.env.INIS_BASE_URL ?? DEFAULT_BASE_URL).replace(/\/$/, "");
}

function mapExecResult(data: Record<string, unknown>): ExecResult {
  return {
    stdout: String(data.stdout ?? ""),
    stderr: String(data.stderr ?? ""),
    exitCode: Number(data.exit_code ?? 0),
    durationMs: Number(data.duration_ms ?? 0),
    timedOut: Boolean(data.timed_out),
    restoreMs: data.restore_ms != null ? Number(data.restore_ms) : undefined,
    installMs: data.install_ms != null ? Number(data.install_ms) : undefined,
    phase: data.phase ? String(data.phase) : undefined,
    truncated: Boolean(data.truncated),
    // Undefined unless the API actually flagged that stream as base64 (its
    // output wasn't valid UTF-8) -- never coerce a missing
    // field to a truthy/falsy default the way the boolean fields above do,
    // since the only valid values here are undefined or exactly "base64".
    stdoutEncoding: data.stdout_encoding === "base64" ? "base64" : undefined,
    stderrEncoding: data.stderr_encoding === "base64" ? "base64" : undefined,
  };
}

function mapSessionInfo(data: Record<string, unknown>): SessionInfo {
  const egress = data.egress as Record<string, unknown> | undefined;
  const archive = data.archive as Record<string, unknown> | undefined;
  const previews = data.exposed_previews as Record<string, unknown>[] | undefined;
  return {
    sessionId: String(data.session_id ?? ""),
    state: String(data.state ?? "") as SessionState,
    statusDetail: data.status_detail ? (String(data.status_detail) as SessionStatusDetail) : undefined,
    statusReason: data.status_reason ? String(data.status_reason) : undefined,
    name: data.name ? String(data.name) : undefined,
    labels: data.labels ? (data.labels as Record<string, string>) : undefined,
    createdAt: data.created_at ? String(data.created_at) : undefined,
    lastActiveAt: data.last_active_at ? String(data.last_active_at) : undefined,
    endedAt: data.ended_at ? String(data.ended_at) : undefined,
    endReason: data.end_reason ? (String(data.end_reason) as EndReason) : undefined,
    idleTimeoutMs: data.idle_timeout_ms != null ? Number(data.idle_timeout_ms) : undefined,
    maxLifetimeMs: data.max_lifetime_ms != null ? Number(data.max_lifetime_ms) : undefined,
    onPtyDetach: data.on_pty_detach ? (String(data.on_pty_detach) as OnPtyDetach) : undefined,
    pausedTtlMs: data.paused_ttl_ms != null ? Number(data.paused_ttl_ms) : undefined,
    noSudo: data.no_sudo ? Boolean(data.no_sudo) : undefined,
    size: data.size ? (String(data.size) as SessionSize) : undefined,
    vcpus: data.vcpus != null ? Number(data.vcpus) : undefined,
    memMb: data.mem_mb != null ? Number(data.mem_mb) : undefined,
    nodeId: data.node_id ? String(data.node_id) : undefined,
    volumeId: data.volume_id ? String(data.volume_id) : undefined,
    template: data.template ? String(data.template) : undefined,
    mcpUrl: data.mcp_url ? String(data.mcp_url) : undefined,
    exposedPorts: data.exposed_ports ? (data.exposed_ports as number[]) : undefined,
    exposedPreviews: previews
      ? previews.map((p) => ({
          port: Number(p.port ?? 0),
          previewUrl: String(p.preview_url ?? ""),
          visibility: String(p.visibility ?? "token") as "token" | "public",
          auth: p.auth ? (String(p.auth) as "none" | "bearer") : undefined,
        }))
      : undefined,
    egress: egress
      ? {
          mode: egress.mode ? (String(egress.mode) as "allow" | "deny") : egress.default ? (String(egress.default) as "allow" | "deny") : undefined,
          allow: egress.allow ? (egress.allow as string[]) : undefined,
        }
      : undefined,
    archive: archive
      ? {
          status: String(archive.status ?? "pending") as "pending" | "complete" | "failed",
          coldUri: archive.cold_uri ? String(archive.cold_uri) : undefined,
          uploadedAt: archive.uploaded_at ? String(archive.uploaded_at) : undefined,
        }
      : undefined,
    activeSince: data.active_since ? String(data.active_since) : undefined,
    maxActiveWindowMs: data.max_active_window_ms != null ? Number(data.max_active_window_ms) : undefined,
    snapshotTier: data.snapshot_tier ? (String(data.snapshot_tier) as "hot" | "warm" | "cold") : undefined,
    env: data.env ? (data.env as Record<string, string>) : undefined,
    secretNames: data.secret_names ? (data.secret_names as string[]) : undefined,
    connectorNames: data.connector_names ? (data.connector_names as string[]) : undefined,
  };
}

function mapCheckpoint(data: Record<string, unknown>): CheckpointInfo {
  return {
    checkpointId: String(data.checkpoint_id ?? ""),
    sessionId: data.session_id ? String(data.session_id) : undefined,
    parentSessionId: data.parent_session_id ? String(data.parent_session_id) : undefined,
    name: data.name ? String(data.name) : undefined,
    labels: data.labels ? (data.labels as Record<string, string>) : undefined,
    sizeBytes: Number(data.size_bytes ?? 0),
    createdAt: data.created_at ? String(data.created_at) : undefined,
    nodeId: data.node_id ? String(data.node_id) : undefined,
  };
}

function mapTemplate(data: Record<string, unknown>): TemplateInfo {
  return {
    name: String(data.name ?? ""),
    kind: data.kind ? (String(data.kind) as "official" | "user") : undefined,
    description: data.description ? String(data.description) : undefined,
    size: data.size ? (String(data.size) as SessionSize) : undefined,
    version: data.version ? String(data.version) : undefined,
    versions: data.versions ? (data.versions as string[]) : undefined,
    createdAt: data.created_at ? String(data.created_at) : undefined,
    status: data.status
      ? (String(data.status) as "queued" | "building" | "ready" | "failed")
      : undefined,
    sourceImage: data.source_image ? String(data.source_image) : undefined,
    buildError: data.build_error ? String(data.build_error) : undefined,
    queuePosition:
      typeof data.queue_position === "number" ? data.queue_position : undefined,
    builtAt: data.built_at ? String(data.built_at) : undefined,
    baseImageRef: data.base_image_ref ? String(data.base_image_ref) : undefined,
    baseImageDigest: data.base_image_digest ? String(data.base_image_digest) : undefined,
    contentHash: data.content_hash ? String(data.content_hash) : undefined,
    cadence: data.cadence ? (String(data.cadence) as "weekly" | "monthly") : undefined,
    category: data.category ? (String(data.category) as "language" | "use-case") : undefined,
  };
}

function mapRegistryCredential(data: Record<string, unknown>): RegistryCredentialInfo {
  return {
    id: String(data.id ?? ""),
    name: String(data.name ?? ""),
    registryHost: String(data.registry_host ?? ""),
    username: String(data.username ?? ""),
    secretLast4: data.secret_last4 ? String(data.secret_last4) : undefined,
    createdAt: data.created_at ? String(data.created_at) : undefined,
    updatedAt: data.updated_at ? String(data.updated_at) : undefined,
  };
}

function mapConnector(data: Record<string, unknown>): ConnectorInfo {
  return {
    id: String(data.id ?? ""),
    name: String(data.name ?? ""),
    targetBaseUrl: String(data.target_base_url ?? ""),
    authShape: String(data.auth_shape ?? ""),
    headerName: data.header_name ? String(data.header_name) : undefined,
    secretLast4: data.secret_last4 ? String(data.secret_last4) : undefined,
    createdAt: data.created_at ? String(data.created_at) : undefined,
    updatedAt: data.updated_at ? String(data.updated_at) : undefined,
  };
}

function mapWebhook(data: Record<string, unknown>): WebhookEndpointInfo {
  return {
    id: String(data.id ?? ""),
    url: String(data.url ?? ""),
    eventTypes: Array.isArray(data.event_types) ? (data.event_types as string[]) : [],
    enabled: Boolean(data.enabled),
    createdAt: data.created_at ? String(data.created_at) : undefined,
    disabledAt: data.disabled_at ? String(data.disabled_at) : undefined,
    secret: data.secret ? String(data.secret) : undefined,
    secretLast4: data.secret_last4 ? String(data.secret_last4) : undefined,
  };
}

function mapWebhookDelivery(data: Record<string, unknown>): WebhookDeliveryInfo {
  return {
    id: typeof data.id === "number" ? data.id : Number(data.id ?? 0),
    eventType: String(data.event_type ?? ""),
    status: (data.status as WebhookDeliveryInfo["status"]) ?? "pending",
    attemptCount: typeof data.attempt_count === "number" ? data.attempt_count : 0,
    maxAttempts: typeof data.max_attempts === "number" ? data.max_attempts : 0,
    sessionId: data.session_id ? String(data.session_id) : undefined,
    attempts: Array.isArray(data.attempts)
      ? (data.attempts as Record<string, unknown>[]).map((a) => ({
          attempt: typeof a.attempt === "number" ? a.attempt : 0,
          at: String(a.at ?? ""),
          statusCode: typeof a.status_code === "number" ? a.status_code : undefined,
          error: a.error ? String(a.error) : undefined,
        }))
      : undefined,
    createdAt: data.created_at ? String(data.created_at) : undefined,
    deliveredAt: data.delivered_at ? String(data.delivered_at) : undefined,
    nextAttemptAt: data.next_attempt_at ? String(data.next_attempt_at) : undefined,
  };
}

function mapDomain(data: Record<string, unknown>): DomainInfo {
  return {
    id: String(data.id ?? ""),
    domain: String(data.domain ?? ""),
    verified: Boolean(data.verified),
    verifyTxtName: String(data.verify_txt_name ?? ""),
    verifyTxtValue: String(data.verify_txt_value ?? ""),
    createdAt: data.created_at ? String(data.created_at) : undefined,
    verifiedAt: data.verified_at ? String(data.verified_at) : undefined,
    sessionId: data.session_id ? String(data.session_id) : undefined,
    port: typeof data.port === "number" ? data.port : undefined,
    ingressBaseDomain: data.ingress_base_domain ? String(data.ingress_base_domain) : undefined,
  };
}

function mapArtifact(data: Record<string, unknown>): ArtifactInfo {
  const files = ((data.files as Record<string, unknown>[]) ?? []).map((f) => ({
    path: String(f.path ?? ""),
    url: f.url ? String(f.url) : undefined,
    sizeBytes: Number(f.size_bytes ?? 0),
    contentType: f.content_type ? String(f.content_type) : undefined,
  }));
  return {
    id: String(data.id ?? ""),
    status: String(data.status ?? "pending") as "pending" | "ready" | "failed",
    sessionId: data.session_id ? String(data.session_id) : undefined,
    capturedAt: data.captured_at ? String(data.captured_at) : undefined,
    expiresAt: data.expires_at ? String(data.expires_at) : undefined,
    files,
    error: data.error ? String(data.error) : undefined,
  };
}

/**
 * Decode a base64 string to raw bytes. Uses the Node/Bun global Buffer when
 * available (this SDK targets Node >=18), falling back to atob for browser
 * bundlers that shim fetch but not Buffer.
 */
function base64ToBytes(b64: string): Uint8Array {
  if (typeof Buffer !== "undefined") return new Uint8Array(Buffer.from(b64, "base64"));
  const bin = atob(b64);
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out;
}

/** Encode raw bytes to a base64 string. See base64ToBytes for the Buffer/atob split. */
function bytesToBase64(bytes: Uint8Array): string {
  if (typeof Buffer !== "undefined") return Buffer.from(bytes).toString("base64");
  let bin = "";
  for (const b of bytes) bin += String.fromCharCode(b);
  return btoa(bin);
}

/**
 * Incrementally decodes a sequence of base64-encoded byte chunks belonging to
 * ONE logical output stream (e.g. every stdout frame of one execStream/
 * streamProcessLogs call) into UTF-8 text, without corrupting a multi-byte
 * character whose bytes land on either side of a chunk boundary.
 *
 * Chunk boundaries here are wherever the guest's io.Copy (32KB buffer) or the
 * host's own read loop happened to split a write — not UTF-8 character
 * boundaries. Decoding each base64 chunk with a FRESH TextDecoder (the
 * previous approach) treats every chunk as if it stood alone: a multi-byte
 * character split across chunk N/N+1 becomes a dangling lead byte at the end
 * of chunk N (replaced with U+FFFD) and a dangling continuation byte at the
 * start of chunk N+1 (also replaced with U+FFFD) — silently corrupting real
 * output. `{ stream: true }` tells TextDecoder to hold back any trailing
 * incomplete sequence and prepend it to the next decode() call instead, which
 * only works if the SAME decoder instance (with its internal buffer) is
 * reused across every chunk of that one stream — a decoder must never be
 * shared between independent streams (e.g. stdout and stderr), since their
 * byte sequences are unrelated. Call finish() once at the true end of the
 * stream to flush (and surface, as replacement characters — a genuine
 * decoding error at that point, not a boundary artifact) any bytes still
 * held back.
 */
class IncrementalUtf8Decoder {
  private decoder = new TextDecoder("utf-8");

  /** Decode one base64-encoded chunk, continuing from any held-back bytes. */
  push(b64: string): string {
    return this.decoder.decode(base64ToBytes(b64), { stream: true });
  }

  /** Flush any bytes still buffered from an incomplete trailing sequence. */
  finish(): string {
    return this.decoder.decode();
  }
}

function mapProcessInfo(data: Record<string, unknown>): ProcessInfo {
  return {
    name: String(data.name ?? ""),
    state: String(data.state ?? "") as "running" | "exited",
    pid: data.pid !== undefined ? Number(data.pid) : undefined,
    command: (data.command as string[]) ?? undefined,
    exitCode: data.exit_code !== undefined ? Number(data.exit_code) : undefined,
    keepAlive: data.keep_alive !== undefined ? Boolean(data.keep_alive) : undefined,
    startedAt: data.started_at ? String(data.started_at) : undefined,
    endedAt: data.ended_at ? String(data.ended_at) : undefined,
  };
}

async function request<T>(
  baseUrl: string,
  token: string,
  method: string,
  path: string,
  body?: unknown,
  timeoutMs = 120_000,
): Promise<T> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const resp = await fetch(`${baseUrl}${path}`, {
      method,
      headers: {
        Authorization: `Bearer ${token}`,
        ...(body !== undefined ? { "Content-Type": "application/json" } : {}),
      },
      body: body !== undefined ? JSON.stringify(body) : undefined,
      signal: controller.signal,
    });
    if (resp.status === 204) return undefined as T;
    const text = await resp.text();
    if (!resp.ok) {
      // Build the structured error from the raw text before attempting our
      // own JSON.parse below — an error body isn't guaranteed to be JSON
      // (a fronting proxy's HTML error page, e.g.), and buildInisError
      // already handles that gracefully instead of throwing a SyntaxError
      // that would mask the real failure.
      throw buildInisError(resp, text);
    }
    const data = text ? JSON.parse(text) : {};
    return data as T;
  } finally {
    clearTimeout(timer);
  }
}

// Symbol.asyncDispose landed as a native well-known symbol only in newer
// runtimes (V8 11.4+ / Node 20.4+); the package still supports Node >=18.
// Guard-define it so `[Symbol.asyncDispose]` below never evaluates to the
// property key "undefined" on older runtimes, and so callers on those
// runtimes can still invoke `session[Symbol.asyncDispose]()` directly even
// without native `await using` syntax support.
if (typeof (Symbol as { asyncDispose?: symbol }).asyncDispose !== "symbol") {
  (Symbol as unknown as { asyncDispose: symbol }).asyncDispose = Symbol.for("Symbol.asyncDispose");
}

export class Session {
  /** Legacy files namespace for backward compatibility. */
  readonly files = {
    read: (path: string, opts?: { timeoutMs?: number }): Promise<string> =>
      this._fileOp("read", path, undefined, opts?.timeoutMs) as Promise<string>,
    write: (path: string, content: string, opts?: { timeoutMs?: number }): Promise<void> =>
      (this._fileOp("write", path, content, opts?.timeoutMs) as Promise<string>).then(() => undefined),
    list: (path = "/workspace", opts?: { timeoutMs?: number }): Promise<string[]> =>
      this._fileOp("list", path, undefined, opts?.timeoutMs) as Promise<string[]>,
  };

  /** Whether the code-interpreter kernel has been verified installed
   * and running under the current KERNEL_SCRIPT_VERSION this call. Reset to
   * false to force _ensureKernel to re-check (a suspected death, or an
   * explicit restartContext()). */
  private _kernelVerified = false;

  constructor(
    private readonly _baseUrl: string,
    private readonly _token: string,
    private readonly _timeoutMs: number,
    public readonly sessionId: string,
  ) {}

  static async create(client: Client, opts?: CreateSessionOptions): Promise<Session> {
    const payload: Record<string, unknown> = {};
    if (opts?.name) payload.name = opts.name;
    if (opts?.labels) payload.labels = opts.labels;
    if (opts?.volumeId) payload.volume_id = opts.volumeId;
    if (opts?.maxLifetimeMs) payload.max_lifetime_ms = opts.maxLifetimeMs;
    if (opts?.idleTimeoutMs) payload.idle_timeout_ms = opts.idleTimeoutMs;
    if (opts?.egress) {
      payload.egress = { mode: opts.egress.mode ?? "allow", allow: opts.egress.allow };
    }
    if (opts?.template) payload.template = opts.template;
    if (opts?.size) payload.size = opts.size;
    if (opts?.noSudo) payload.no_sudo = true;
    if (opts?.onPtyDetach) payload.on_pty_detach = opts.onPtyDetach;
    if (opts?.pausedTtlMs != null) payload.paused_ttl_ms = opts.pausedTtlMs;
    if (opts?.destroyOnCompletion) payload.destroy_on_completion = true;
    if (opts?.commandCapture) payload.command_capture = opts.commandCapture;
    if (opts?.env) payload.env = opts.env;
    if (opts?.secrets) payload.secrets = opts.secrets;
    if (opts?.connectors) payload.connectors = opts.connectors;
    const data = await request<{
      session_id: string;
      state?: string;
      status_detail?: string;
    }>(
      client.baseUrl, client.token, "POST", "/v1/sessions", payload, client.timeoutMs,
    );
    const session = new Session(client.baseUrl, client.token, client.timeoutMs, data.session_id);
    if (opts?.wait !== false) {
      await session._waitUntilReady(data.state, data.status_detail, opts?.onStatus);
    }
    return session;
  }

  /**
   * Poll GET until this session leaves state=="creating". Keeps
   * Session.create's historical blocking contract (the sandbox is ready to
   * use by the time create() resolves) now that POST acknowledges
   * immediately. A no-op when initialState is already something other than
   * "creating" (the common already-warm case: no template, or a template
   * already materialized on the placed node).
   */
  private async _waitUntilReady(
    initialState: string | undefined,
    initialDetail: string | undefined,
    onStatus?: (detail: SessionStatusDetail) => void,
  ): Promise<void> {
    if (initialState !== "creating") return;
    if (initialDetail) onStatus?.(initialDetail as SessionStatusDetail);
    // Generously above the worst documented cold-template-pull cost (~35s
    // for the largest official bundle) plus room for a scale-out wait,
    // without blocking forever against a genuinely wedged create.
    const deadline = Date.now() + 5 * 60 * 1000;
    for (;;) {
      if (Date.now() > deadline) {
        throw new InisError(`session ${this.sessionId} did not become ready in time`);
      }
      await new Promise((resolve) => setTimeout(resolve, 300));
      const info = await this.get();
      if (info.state === "creating") {
        if (info.statusDetail) onStatus?.(info.statusDetail);
        continue;
      }
      if (info.state === "failed") {
        throw new InisError(
          `session ${this.sessionId} failed to start: ${info.statusReason ?? "provisioning failed"}`,
        );
      }
      return;
    }
  }

  static attach(client: Client, sessionId: string): Session {
    return new Session(client.baseUrl, client.token, client.timeoutMs, sessionId);
  }

  async exec(
    command: string | string[],
    opts?: { cwd?: string; timeoutMs?: number },
  ): Promise<ExecResult> {
    const argv = typeof command === "string" ? ["bash", "-lc", command] : command;
    const payload: Record<string, unknown> = { command: argv };
    if (opts?.cwd) payload.cwd = opts.cwd;
    if (opts?.timeoutMs) payload.timeout_ms = opts.timeoutMs;
    const data = await request<Record<string, unknown>>(
      this._baseUrl, this._token, "POST",
      `/v1/sessions/${this.sessionId}/exec`, payload, this._timeoutMs,
    );
    return mapExecResult(data);
  }

  /**
   * Run a command inside this session, streaming stdout/stderr live via
   * Server-Sent Events instead of waiting for it to finish.
   *
   * Yields an ExecStreamEvent per chunk as the guest produces it, ending
   * with exactly one terminal event: stream="exit" (exitCode/timedOut/
   * durationMs populated) once the command completes or its timeout fires,
   * or stream="error" on a guest-side failure. Breaking out of the loop
   * early (or otherwise abandoning the generator) closes the underlying
   * connection, which the server takes as a disconnect and kills the
   * command — unlike startProcess(), it does not keep running in the
   * background.
   */
  async *execStream(
    command: string | string[],
    opts?: { cwd?: string; timeoutMs?: number },
  ): AsyncGenerator<ExecStreamEvent> {
    const argv = typeof command === "string" ? ["bash", "-lc", command] : command;
    const payload: Record<string, unknown> = { command: argv };
    if (opts?.cwd) payload.cwd = opts.cwd;
    if (opts?.timeoutMs) payload.timeout_ms = opts.timeoutMs;

    const url = `${this._baseUrl}/v1/sessions/${this.sessionId}/exec?stream=true`;
    const resp = await fetch(url, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${this._token}`,
        "Content-Type": "application/json",
      },
      body: JSON.stringify(payload),
    });
    if (!resp.ok || !resp.body) {
      const text = await resp.text();
      throw buildInisError(resp, text);
    }

    const reader = resp.body.getReader();
    // Decodes the SSE FRAMING itself (the "event: .../data: ..." protocol
    // text) — always pure ASCII (base64 alphabet + line syntax), so a fresh
    // decoder per read() with {stream: true} is fine here; this is NOT the
    // decoder susceptible to the chunk-boundary bug (see
    // IncrementalUtf8Decoder's doc) — that one decodes the base64 PAYLOAD.
    const lineDecoder = new TextDecoder();
    // Separate incremental decoders for stdout/stderr: they're independent
    // byte streams, so a character split at the end of one must never be
    // joined with bytes from the other.
    const stdoutDecoder = new IncrementalUtf8Decoder();
    const stderrDecoder = new IncrementalUtf8Decoder();
    let buf = "";
    let eventType = "";
    try {
      while (true) {
        const { done, value } = await reader.read();
        if (done) return;
        buf += lineDecoder.decode(value, { stream: true });
        let idx: number;
        // eslint-disable-next-line no-cond-assign
        while ((idx = buf.indexOf("\n")) >= 0) {
          const line = buf.slice(0, idx).replace(/\r$/, "");
          buf = buf.slice(idx + 1);
          if (line === "") continue;
          if (line.startsWith("event:")) {
            eventType = line.slice("event:".length).trim();
          } else if (line.startsWith("data:")) {
            const raw = line.slice("data:".length).trim();
            if (eventType === "stdout" && raw) {
              yield { stream: "stdout", data: stdoutDecoder.push(raw) };
            } else if (eventType === "stderr" && raw) {
              yield { stream: "stderr", data: stderrDecoder.push(raw) };
            } else if (eventType === "exit") {
              // Flush any bytes the incremental decoders were still holding
              // back for a not-yet-complete trailing multi-byte character —
              // real content, not a boundary artifact, this being the true
              // end of the stream.
              const trailingStdout = stdoutDecoder.finish();
              if (trailingStdout) yield { stream: "stdout", data: trailingStdout };
              const trailingStderr = stderrDecoder.finish();
              if (trailingStderr) yield { stream: "stderr", data: trailingStderr };
              let parsed: Record<string, unknown> = {};
              try {
                parsed = raw ? JSON.parse(raw) : {};
              } catch {
                parsed = {};
              }
              yield {
                stream: "exit",
                data: "",
                exitCode: typeof parsed.exit_code === "number" ? parsed.exit_code : undefined,
                timedOut: typeof parsed.timed_out === "boolean" ? parsed.timed_out : undefined,
                durationMs: typeof parsed.duration_ms === "number" ? parsed.duration_ms : undefined,
              };
              return;
            } else {
              const stream = eventType as ExecStreamEvent["stream"];
              yield { stream, data: raw };
              if (stream === "error") return;
            }
          }
        }
      }
    } finally {
      reader.releaseLock();
    }
  }

  // ── Code interpreter ────────────────────────────────────────────────────

  /**
   * Run code in this session's persistent Python interpreter context.
   *
   * Unlike exec(), variables/imports/functions/classes survive across
   * calls — the "context" — until restartContext() or the session ends.
   * Returns a list of typed results: "text" (stdout/stderr/the last
   * expression's repr), "image" (a captured matplotlib figure, .data
   * holding decoded PNG bytes), "table" (a pandas DataFrame/Series last
   * expression, capped at 1000 rows), "json" (a dict/list/tuple last
   * expression), or "error" (an uncaught exception with its traceback —
   * the context is untouched and stays usable next call).
   *
   * Built entirely on exec/writeFile/readFile/startProcess: no guest-agent
   * or template changes required. A small stdlib-only Python script
   * (matplotlib/pandas used opportunistically if already installed) is
   * uploaded on first use and run as a background process. If that process
   * has died — OOM, a manual killProcess("inis-kernel"), code that
   * segfaulted the interpreter — the next runCode() call transparently
   * restarts it with a fresh, empty context and runs normally; it does not
   * throw.
   */
  async runCode(code: string, opts?: { timeoutMs?: number }): Promise<InterpreterResult[]> {
    const timeoutMs = opts?.timeoutMs ?? interp.DEFAULT_TIMEOUT_MS;
    return this._runCodeAttempt(code, timeoutMs, /* allowRetry */ true);
  }

  /**
   * Discard this session's interpreter context: kills the running kernel
   * process (if any) and starts a fresh one. The next runCode() call
   * begins from empty globals — no prior variables, imports, or
   * definitions.
   */
  async restartContext(): Promise<void> {
    try {
      await this.killProcess(interp.KERNEL_NAME);
    } catch {
      // no kernel running yet — nothing to kill
    }
    this._kernelVerified = false;
    await this._ensureKernel();
  }

  private async _ensureKernel(): Promise<void> {
    if (this._kernelVerified) return;

    let running = false;
    try {
      const info = await this.getProcess(interp.KERNEL_NAME);
      running = info.state === "running";
    } catch {
      running = false;
    }
    let versionOk = false;
    if (running) {
      try {
        const installed = await this.readFile(interp.VERSION_PATH);
        versionOk = installed.trim() === interp.KERNEL_SCRIPT_VERSION;
      } catch {
        versionOk = false;
      }
    }
    if (running && versionOk) {
      this._kernelVerified = true;
      return;
    }

    await this.exec(interp.mkdirsCommand());
    await this.writeFile(interp.KERNEL_PATH, interp.KERNEL_SCRIPT);
    await this.writeFile(interp.VERSION_PATH, interp.KERNEL_SCRIPT_VERSION);
    if (running) {
      try {
        await this.killProcess(interp.KERNEL_NAME);
      } catch {
        // already gone
      }
    }
    await this.startProcess(interp.KERNEL_NAME, ["python3", interp.KERNEL_PATH, interp.ROOT_DIR]);
    this._kernelVerified = true;
  }

  private async _runCodeAttempt(
    code: string,
    timeoutMs: number,
    allowRetry: boolean,
  ): Promise<InterpreterResult[]> {
    await this._ensureKernel();
    const reqId = interp.newRequestId();
    const [tmpPath, finalPath] = interp.requestPaths(reqId);
    await this.writeFile(tmpPath, interp.encodeRequest(reqId, code));
    const waitCmd = interp.waitCommand(reqId, tmpPath, finalPath, timeoutMs);
    const result = await this.exec(waitCmd, { timeoutMs: interp.execTimeoutMs(timeoutMs) });
    const rawResults = interp.parseEnvelope(result.stdout, reqId);
    if (rawResults === null) {
      if (allowRetry) {
        let kernelAlive: boolean;
        try {
          const info = await this.getProcess(interp.KERNEL_NAME);
          kernelAlive = info.state === "running";
        } catch {
          kernelAlive = false;
        }
        if (!kernelAlive) {
          this._kernelVerified = false;
          return this._runCodeAttempt(code, timeoutMs, false);
        }
      }
      return [interp.timeoutResult(timeoutMs)];
    }
    return Promise.all(rawResults.map((r) => this._materializeResult(r)));
  }

  private async _materializeResult(raw: Record<string, unknown>): Promise<InterpreterResult> {
    const result = interp.rawToResult(raw);
    if (result.type === "image" && result.path) {
      try {
        result.data = await this.readFile(result.path, "base64");
      } catch {
        // best-effort: leave data unset rather than fail the whole call
      }
    }
    return result;
  }

  /**
   * Open an interactive pseudo-terminal in this session.
   *
   * Returns a live PTY over the same WebSocket endpoint and binary frame
   * protocol the console's web terminal uses. command defaults to the
   * guest's login shell; pass e.g. ["python3", "-i"] to open a REPL
   * directly.
   *
   * Loaded via a dynamic import so client.ts (this file) and pty.ts don't
   * form a static import cycle at the module-graph level — pty.ts imports
   * InisError from here; only its type is imported above.
   */
  async pty(opts?: PTYOptions): Promise<PTY> {
    const { PTY: PTYImpl } = await import("./pty.js");
    return PTYImpl._open(this._baseUrl, this._token, this.sessionId, opts);
  }

  async get(): Promise<SessionInfo> {
    const data = await request<Record<string, unknown>>(
      this._baseUrl, this._token, "GET",
      `/v1/sessions/${this.sessionId}`, undefined, this._timeoutMs,
    );
    return mapSessionInfo(data);
  }

  async pause(): Promise<SessionInfo> {
    const data = await request<Record<string, unknown>>(
      this._baseUrl, this._token, "POST",
      `/v1/sessions/${this.sessionId}/pause`, {}, this._timeoutMs,
    );
    return mapSessionInfo(data);
  }

  async resume(): Promise<SessionInfo> {
    const data = await request<Record<string, unknown>>(
      this._baseUrl, this._token, "POST",
      `/v1/sessions/${this.sessionId}/resume`, {}, this._timeoutMs,
    );
    return mapSessionInfo(data);
  }

  async fork(count: number): Promise<ForkResult> {
    const data = await request<{ parent_session_id?: string; children?: string[] }>(
      this._baseUrl, this._token, "POST",
      `/v1/sessions/${this.sessionId}/fork`, { count }, this._timeoutMs,
    );
    return {
      parentSessionId: String(data.parent_session_id ?? this.sessionId),
      children: data.children ?? [],
    };
  }

  async archiveRetry(): Promise<ArchiveStatus> {
    const data = await request<{ status?: string; cold_uri?: string; uploaded_at?: string }>(
      this._baseUrl, this._token, "POST",
      `/v1/sessions/${this.sessionId}/archive/retry`, {}, this._timeoutMs,
    );
    return {
      status: String(data.status ?? "pending") as "pending" | "complete" | "failed",
      coldUri: data.cold_uri,
      uploadedAt: data.uploaded_at,
    };
  }

  async destroy(opts?: DestroyOptions): Promise<void> {
    let path = `/v1/sessions/${this.sessionId}`;
    if (opts?.reason) path += `?reason=${encodeURIComponent(opts.reason)}`;
    await request<void>(this._baseUrl, this._token, "DELETE", path, undefined, this._timeoutMs);
  }

  /**
   * Explicit resource management (TC39): enables `await using session = await
   * client.sessions.create()` to auto-destroy the session when it goes out
   * of scope, mirroring the Python SDK's `with client.session() as s:`.
   */
  async [Symbol.asyncDispose](): Promise<void> {
    await this.destroy();
  }

  async checkpoint(opts?: CheckpointOptions): Promise<CheckpointInfo> {
    const body: Record<string, unknown> = {};
    if (opts?.name) body.name = opts.name;
    if (opts?.labels) body.labels = opts.labels;
    const data = await request<Record<string, unknown>>(
      this._baseUrl, this._token, "POST",
      `/v1/sessions/${this.sessionId}/checkpoints`, body, this._timeoutMs,
    );
    return mapCheckpoint(data);
  }

  async checkpoints(): Promise<CheckpointInfo[]> {
    const data = await request<{ checkpoints?: Record<string, unknown>[] }>(
      this._baseUrl, this._token, "GET",
      `/v1/sessions/${this.sessionId}/checkpoints`, undefined, this._timeoutMs,
    );
    return (data.checkpoints ?? []).map(mapCheckpoint);
  }

  async restore(checkpointId: string): Promise<SessionInfo> {
    const data = await request<Record<string, unknown>>(
      this._baseUrl, this._token, "POST",
      `/v1/sessions/${this.sessionId}/restore`, { checkpoint_id: checkpointId }, this._timeoutMs,
    );
    return mapSessionInfo(data);
  }

  async saveAsTemplate(name: string, opts?: SaveAsTemplateOptions): Promise<TemplateInfo> {
    const body: Record<string, unknown> = { name };
    if (opts?.description) body.description = opts.description;
    const data = await request<Record<string, unknown>>(
      this._baseUrl, this._token, "POST",
      `/v1/sessions/${this.sessionId}/templates`, body, this._timeoutMs,
    );
    return mapTemplate(data);
  }

  async captureArtifacts(paths: string[], opts?: CaptureArtifactsOptions): Promise<ArtifactInfo> {
    const body: Record<string, unknown> = { paths };
    if (opts?.destination) body.destination = opts.destination;
    const data = await request<Record<string, unknown>>(
      this._baseUrl, this._token, "POST",
      `/v1/sessions/${this.sessionId}/artifacts`, body, this._timeoutMs,
    );
    return mapArtifact(data);
  }

  async artifacts(): Promise<ArtifactInfo[]> {
    const data = await request<{ artifacts?: Record<string, unknown>[] }>(
      this._baseUrl, this._token, "GET",
      `/v1/sessions/${this.sessionId}/artifacts`, undefined, this._timeoutMs,
    );
    return (data.artifacts ?? []).map(mapArtifact);
  }

  async expose(
    port: number,
    options: ExposeOptions | "token" | "public" = {},
  ): Promise<ExposeResult> {
    const opts: ExposeOptions =
      typeof options === "string" ? { visibility: options } : options;
    const body: Record<string, unknown> = {
      port,
      visibility: opts.visibility ?? "token",
    };
    if (opts.auth) body.auth = opts.auth;
    const data = await request<Record<string, unknown>>(
      this._baseUrl, this._token, "POST",
      `/v1/sessions/${this.sessionId}/expose`, body, this._timeoutMs,
    );
    return {
      sessionId: String(data.session_id ?? this.sessionId),
      port: Number(data.port ?? port),
      previewUrl: String(data.preview_url ?? ""),
      ingressToken: data.ingress_token ? String(data.ingress_token) : undefined,
      guestIp: data.guest_ip ? String(data.guest_ip) : undefined,
      auth: data.auth ? (String(data.auth) as "none" | "bearer") : undefined,
      authToken: data.auth_token ? String(data.auth_token) : undefined,
    };
  }

  async unexpose(port: number): Promise<boolean> {
    const data = await request<{ ok?: boolean }>(
      this._baseUrl, this._token, "POST",
      `/v1/sessions/${this.sessionId}/unexpose`, { port }, this._timeoutMs,
    );
    return !!data.ok;
  }

  async getEgress(): Promise<EgressPolicy> {
    const data = await request<Record<string, unknown>>(
      this._baseUrl, this._token, "GET",
      `/v1/sessions/${this.sessionId}/egress`, undefined, this._timeoutMs,
    );
    return {
      mode: (data.mode ?? data.default) as "allow" | "deny" | undefined,
      allow: (data.allow as string[]) ?? undefined,
    };
  }

  async setEgress(policy: EgressPolicy): Promise<EgressPolicy> {
    const data = await request<Record<string, unknown>>(
      this._baseUrl, this._token, "POST",
      `/v1/sessions/${this.sessionId}/egress`,
      { mode: policy.mode ?? "deny", allow: policy.allow ?? [] },
      this._timeoutMs,
    );
    return {
      mode: (data.mode ?? data.default) as "allow" | "deny" | undefined,
      allow: (data.allow as string[]) ?? undefined,
    };
  }

  async readFile(path: string, encoding?: "text"): Promise<string>;
  async readFile(path: string, encoding: "base64"): Promise<Uint8Array>;
  async readFile(path: string, encoding?: "text" | "base64"): Promise<string | Uint8Array> {
    const params = new URLSearchParams({ path, op: "read" });
    if (encoding === "base64") params.set("encoding", "base64");
    const data = await request<{ content?: string }>(
      this._baseUrl, this._token, "GET",
      `/v1/sessions/${this.sessionId}/files?${params}`, undefined, this._timeoutMs,
    );
    const content = String(data.content ?? "");
    if (encoding === "base64") return base64ToBytes(content);
    return content;
  }

  async listFiles(path: string): Promise<string[]> {
    const params = new URLSearchParams({ path, op: "list" });
    const data = await request<{ entries?: string[] }>(
      this._baseUrl, this._token, "GET",
      `/v1/sessions/${this.sessionId}/files?${params}`, undefined, this._timeoutMs,
    );
    return data.entries ?? [];
  }

  async writeFile(
    path: string,
    content: string | Uint8Array,
    encoding?: "text" | "base64",
  ): Promise<void> {
    const body: Record<string, unknown> = { path, content };
    if (content instanceof Uint8Array) {
      body.content = bytesToBase64(content);
      body.encoding = "base64";
    } else if (encoding) {
      body.encoding = encoding;
    }
    await request<void>(
      this._baseUrl, this._token, "PUT",
      `/v1/sessions/${this.sessionId}/files`, body, this._timeoutMs,
    );
  }

  /**
   * Write multiple small files under /workspace in one call.
   * PUT /v1/sessions/{id}/files/batch. Capped at 64 files / 8 MiB total content —
   * for bulk import use artifact capture (export) or exec/SSH instead. Not
   * fail-fast: one bad path never sinks the rest of the batch.
   */
  async writeFiles(files: FileBatchItem[]): Promise<FileBatchResult[]> {
    const data = await request<{ results?: Array<{ path?: string; ok?: boolean; error?: string }> }>(
      this._baseUrl, this._token, "PUT",
      `/v1/sessions/${this.sessionId}/files/batch`, { files }, this._timeoutMs,
    );
    return (data.results ?? []).map((r) => ({
      path: String(r.path ?? ""),
      ok: Boolean(r.ok),
      error: r.error,
    }));
  }

  /**
   * Find file paths under the workspace matching a glob (exact path,
   * "*"/"?"/"[...]", or a trailing "/**" for recursive).
   * GET /v1/sessions/{id}/files?op=find
   */
  async findFiles(pattern: string, opts?: FindFilesOptions): Promise<FindFilesResult> {
    const params = new URLSearchParams({ path: opts?.path ?? "/workspace", op: "find", pattern });
    if (opts?.maxResults) params.set("max_results", String(opts.maxResults));
    const data = await request<{ paths?: string[]; truncated?: boolean }>(
      this._baseUrl, this._token, "GET",
      `/v1/sessions/${this.sessionId}/files?${params}`, undefined, this._timeoutMs,
    );
    return { paths: data.paths ?? [], truncated: Boolean(data.truncated) };
  }

  /**
   * Search file contents under the workspace with a regex (Go RE2 syntax —
   * no PCRE lookaround/backrefs). Binary files are skipped automatically.
   * GET /v1/sessions/{id}/files?op=grep
   */
  async grepFiles(pattern: string, opts?: GrepFilesOptions): Promise<GrepFilesResult> {
    const params = new URLSearchParams({ path: opts?.path ?? "/workspace", op: "grep", pattern });
    if (opts?.filePattern) params.set("file_pattern", opts.filePattern);
    if (opts?.caseSensitive) params.set("case_sensitive", "true");
    if (opts?.contextLines) params.set("context_lines", String(opts.contextLines));
    if (opts?.maxResults) params.set("max_results", String(opts.maxResults));
    if (opts?.excludeDirs?.length) params.set("exclude_dirs", opts.excludeDirs.join(","));
    const data = await request<{
      matches?: Array<{ path?: string; line_number?: number; line?: string; before?: string[]; after?: string[] }>;
      files_searched?: number;
      truncated?: boolean;
    }>(
      this._baseUrl, this._token, "GET",
      `/v1/sessions/${this.sessionId}/files?${params}`, undefined, this._timeoutMs,
    );
    return {
      matches: (data.matches ?? []).map((m) => ({
        path: String(m.path ?? ""),
        lineNumber: Number(m.line_number ?? 0),
        line: String(m.line ?? ""),
        before: m.before,
        after: m.after,
      })),
      filesSearched: Number(data.files_searched ?? 0),
      truncated: Boolean(data.truncated),
    };
  }

  /**
   * Create a directory (with parents) in the session filesystem.
   * POST /v1/sessions/{id}/files/mkdir
   *
   * Always `mkdir -p` semantics: intermediate components are created as
   * needed, and mkdir-ing an already-existing directory is not an error.
   */
  async mkdir(path: string): Promise<void> {
    await request<void>(
      this._baseUrl, this._token, "POST",
      `/v1/sessions/${this.sessionId}/files/mkdir`, { path }, this._timeoutMs,
    );
  }

  /**
   * Remove a file, or a directory (with recursive=true).
   * DELETE /v1/sessions/{id}/files?path=<path>&recursive=<bool>
   *
   * Without recursive, a directory target must already be empty (rmdir
   * semantics — a distinct "directory not empty" error rather than silently
   * recursing); a file target is removed unconditionally either way. The
   * workspace root itself can never be removed.
   */
  async remove(path: string, opts?: { recursive?: boolean }): Promise<void> {
    const params = new URLSearchParams({ path });
    if (opts?.recursive) params.set("recursive", "true");
    await request<void>(
      this._baseUrl, this._token, "DELETE",
      `/v1/sessions/${this.sessionId}/files?${params}`, undefined, this._timeoutMs,
    );
  }

  /**
   * Rename or move a file or directory.
   * POST /v1/sessions/{id}/files/rename
   *
   * Both path and destPath must resolve under /workspace; the destination's
   * parent directories are created as needed.
   */
  async rename(path: string, destPath: string): Promise<void> {
    await request<void>(
      this._baseUrl, this._token, "POST",
      `/v1/sessions/${this.sessionId}/files/rename`, { path, dest_path: destPath }, this._timeoutMs,
    );
  }

  /**
   * Stream a local file into the session filesystem — NOT subject to
   * writeFiles' 64-file/8 MiB batch cap. Use this (not writeFile) for a repo
   * tarball, dataset, or any large/binary file.
   * PUT /v1/sessions/{id}/files/stream
   *
   * Streams localPath from disk as the request body (a Node ReadStream)
   * rather than reading it fully into memory first. Returns the server's
   * sha256 digest of the received content, for round-trip verification
   * against the local file's own hash.
   */
  async uploadFile(
    localPath: string,
    remotePath: string,
  ): Promise<{ path: string; bytes: number; sha256: string; durationMs: number }> {
    const params = new URLSearchParams({ path: remotePath });
    const body = Readable.toWeb(createReadStream(localPath)) as ReadableStream<Uint8Array>;
    const resp = await fetch(`${this._baseUrl}/v1/sessions/${this.sessionId}/files/stream?${params}`, {
      method: "PUT",
      headers: { Authorization: `Bearer ${this._token}` },
      body,
      // Node's fetch requires an explicit opt-in to stream a request body
      // rather than buffering it; not yet in the bundled DOM/node fetch
      // typings, hence the cast.
      duplex: "half",
    } as RequestInit & { duplex: "half" });
    if (!resp.ok) {
      const text = await resp.text();
      throw buildInisError(resp, text);
    }
    const data = (await resp.json()) as Record<string, unknown>;
    return {
      path: String(data.path ?? remotePath),
      bytes: Number(data.bytes ?? 0),
      sha256: String(data.sha256 ?? ""),
      durationMs: Number(data.duration_ms ?? 0),
    };
  }

  /**
   * Stream a file from the session filesystem to local disk — NOT subject
   * to readFile's 4 MiB whole-file cap. Use this (not readFile) for large
   * results/datasets.
   * GET /v1/sessions/{id}/files/stream
   *
   * Writes the response body straight to localPath as it arrives (never
   * buffering the whole file in memory) and computes a running sha256 as it
   * writes.
   */
  async downloadFile(
    remotePath: string,
    localPath: string,
  ): Promise<{ path: string; localPath: string; bytes: number; sha256: string }> {
    const params = new URLSearchParams({ path: remotePath });
    const resp = await fetch(`${this._baseUrl}/v1/sessions/${this.sessionId}/files/stream?${params}`, {
      headers: { Authorization: `Bearer ${this._token}` },
    });
    if (!resp.ok || !resp.body) {
      const text = await resp.text();
      throw buildInisError(resp, text);
    }

    const hash = createHash("sha256");
    let total = 0;
    const out = createWriteStream(localPath);
    const reader = resp.body.getReader();
    try {
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        if (value && value.length > 0) {
          hash.update(value);
          total += value.length;
          await new Promise<void>((resolve, reject) => {
            out.write(value, (err) => (err ? reject(err) : resolve()));
          });
        }
      }
    } finally {
      await new Promise<void>((resolve, reject) => {
        out.end((err: unknown) => (err ? reject(err) : resolve()));
      });
    }
    return { path: remotePath, localPath, bytes: total, sha256: hash.digest("hex") };
  }

  /**
   * Start a named, detached background process in this session.
   *
   * Unlike exec(), the process keeps running after this call returns —
   * independent of the caller disconnecting. Starting a second process under
   * a name still in use by a running one is rejected; a name whose process
   * has already exited may be reused.
   *
   * opts.keepAlive suppresses the session's idle timer for as long as this
   * process is running, the same mechanism an attached PTY or a pinned
   * ingress connection uses.
   */
  async startProcess(
    name: string,
    command: string | string[],
    opts?: StartProcessOptions,
  ): Promise<ProcessInfo> {
    const argv = typeof command === "string" ? ["bash", "-lc", command] : command;
    const body: Record<string, unknown> = { name, command: argv, keep_alive: !!opts?.keepAlive };
    if (opts?.cwd) body.cwd = opts.cwd;
    const data = await request<Record<string, unknown>>(
      this._baseUrl, this._token, "POST",
      `/v1/sessions/${this.sessionId}/processes`, body, this._timeoutMs,
    );
    return mapProcessInfo(data);
  }

  /** List every named background process in this session (running and exited). */
  async listProcesses(): Promise<ProcessInfo[]> {
    const data = await request<{ processes?: Record<string, unknown>[] }>(
      this._baseUrl, this._token, "GET",
      `/v1/sessions/${this.sessionId}/processes`, undefined, this._timeoutMs,
    );
    return (data.processes ?? []).map(mapProcessInfo);
  }

  /** Get one named background process's current state. */
  async getProcess(name: string): Promise<ProcessInfo> {
    const data = await request<Record<string, unknown>>(
      this._baseUrl, this._token, "GET",
      `/v1/sessions/${this.sessionId}/processes/${encodeURIComponent(name)}`, undefined, this._timeoutMs,
    );
    return mapProcessInfo(data);
  }

  /**
   * Kill a named background process: SIGTERM, then SIGKILL after a grace
   * period if it hasn't exited. A no-op (just reports state) if it has
   * already exited.
   */
  async killProcess(name: string): Promise<ProcessInfo> {
    const data = await request<Record<string, unknown>>(
      this._baseUrl, this._token, "DELETE",
      `/v1/sessions/${this.sessionId}/processes/${encodeURIComponent(name)}`, undefined, this._timeoutMs,
    );
    return mapProcessInfo(data);
  }

  /** Read a named process's captured stdout/stderr so far, buffered (one read). */
  async getProcessLogs(name: string): Promise<ProcessLogs> {
    const data = await request<Record<string, unknown>>(
      this._baseUrl, this._token, "GET",
      `/v1/sessions/${this.sessionId}/processes/${encodeURIComponent(name)}/logs`, undefined, this._timeoutMs,
    );
    return {
      name: String(data.name ?? name),
      stdout: String(data.stdout ?? ""),
      stderr: String(data.stderr ?? ""),
      truncated: Boolean(data.truncated),
      stdoutEncoding: data.stdout_encoding === "base64" ? "base64" : undefined,
      stderrEncoding: data.stderr_encoding === "base64" ? "base64" : undefined,
    };
  }

  /**
   * Live-follow a named process's stdout/stderr via Server-Sent Events.
   *
   * Yields a ProcessLogEvent per chunk as the guest produces it, ending with
   * event="eof" once the process exits and its output is fully drained, or
   * event="error" on a guest-side failure. Both terminal events end
   * iteration.
   */
  async *streamProcessLogs(name: string): AsyncGenerator<ProcessLogEvent> {
    const url = `${this._baseUrl}/v1/sessions/${this.sessionId}/processes/${encodeURIComponent(name)}/logs?follow=true`;
    const resp = await fetch(url, {
      headers: { Authorization: `Bearer ${this._token}` },
    });
    if (!resp.ok || !resp.body) {
      const text = await resp.text();
      throw buildInisError(resp, text);
    }

    const reader = resp.body.getReader();
    // See execStream's identical split for why this is two decoders: one for
    // the (always-ASCII) SSE framing text, and one PER independent output
    // stream for the base64 payload — a fresh TextDecoder per chunk (the
    // previous atobUtf8-per-frame approach) silently corrupts any multi-byte
    // character whose bytes straddle a chunk boundary.
    const lineDecoder = new TextDecoder();
    const stdoutDecoder = new IncrementalUtf8Decoder();
    const stderrDecoder = new IncrementalUtf8Decoder();
    let buf = "";
    let eventType = "";
    try {
      while (true) {
        const { done, value } = await reader.read();
        if (done) return;
        buf += lineDecoder.decode(value, { stream: true });
        let idx: number;
        // eslint-disable-next-line no-cond-assign
        while ((idx = buf.indexOf("\n")) >= 0) {
          const line = buf.slice(0, idx).replace(/\r$/, "");
          buf = buf.slice(idx + 1);
          if (line === "") continue;
          if (line.startsWith("event:")) {
            eventType = line.slice("event:".length).trim();
          } else if (line.startsWith("data:")) {
            const raw = line.slice("data:".length).trim();
            if (eventType === "stdout" && raw) {
              yield { event: "stdout", data: stdoutDecoder.push(raw) };
              continue;
            }
            if (eventType === "stderr" && raw) {
              yield { event: "stderr", data: stderrDecoder.push(raw) };
              continue;
            }
            if (eventType === "eof" || eventType === "error") {
              // Flush any bytes still held back for a not-yet-complete
              // trailing multi-byte character — this is the true end of the
              // stream, so what's buffered is real content, not a boundary
              // artifact waiting on a next chunk that will never arrive.
              const trailingStdout = stdoutDecoder.finish();
              if (trailingStdout) yield { event: "stdout", data: trailingStdout };
              const trailingStderr = stderrDecoder.finish();
              if (trailingStderr) yield { event: "stderr", data: trailingStderr };
            }
            const event = eventType as ProcessLogEvent["event"];
            yield { event, data: raw };
            if (event === "eof" || event === "error") return;
          }
        }
      }
    } finally {
      reader.releaseLock();
    }
  }

  /** @internal Legacy POST-based file operation for backward compat. */
  private async _fileOp(
    op: "read" | "write" | "list",
    path: string,
    content?: string,
    timeoutMs?: number,
  ): Promise<string | string[]> {
    const payload: Record<string, unknown> = { op, path };
    if (content !== undefined) payload.content = content;
    if (timeoutMs) payload.timeout_ms = timeoutMs;
    const data = await request<Record<string, unknown>>(
      this._baseUrl, this._token, "POST",
      `/v1/sessions/${this.sessionId}/files`, payload, this._timeoutMs,
    );
    if (op === "read") return String(data.content ?? "");
    if (op === "list") return (data.entries as string[]) ?? [];
    return path;
  }
}

export class Client {
  readonly baseUrl: string;
  readonly token: string;
  readonly timeoutMs: number;

  readonly sessions: {
    create(opts?: CreateSessionOptions): Promise<Session>;
    attach(sessionId: string): Session;
    list(opts?: ListSessionsOptions): Promise<{ sessions: SessionInfo[]; nextCursor?: string }>;
    get(sessionId: string): Promise<SessionInfo>;
    pause(sessionId: string): Promise<SessionInfo>;
    resume(sessionId: string): Promise<SessionInfo>;
    fork(sessionId: string, count: number): Promise<ForkResult>;
    archiveRetry(sessionId: string): Promise<ArchiveStatus>;
    batchExec(opts: BatchExecOptions): Promise<BatchExecResult[]>;
    readFile(sessionId: string, path: string, encoding?: "text" | "base64"): Promise<string | Uint8Array>;
    listFiles(sessionId: string, path: string): Promise<string[]>;
    writeFile(
      sessionId: string,
      path: string,
      content: string | Uint8Array,
      encoding?: "text" | "base64",
    ): Promise<void>;
    writeFiles(sessionId: string, files: FileBatchItem[]): Promise<FileBatchResult[]>;
    findFiles(sessionId: string, pattern: string, opts?: FindFilesOptions): Promise<FindFilesResult>;
    grepFiles(sessionId: string, pattern: string, opts?: GrepFilesOptions): Promise<GrepFilesResult>;
  };

  readonly checkpoints: {
    get(checkpointId: string): Promise<CheckpointInfo>;
    delete(checkpointId: string): Promise<void>;
    createSession(checkpointId: string, opts?: CheckpointSessionOptions): Promise<Session>;
  };

  readonly templates: {
    list(): Promise<TemplateInfo[]>;
    /** Import a PUBLIC OCI image (Docker Hub / GHCR anonymous pull) as a custom
     * template. Builds are serialized: the returned template starts "queued"
     * (with a queuePosition), moves to "building", then becomes usable at
     * "ready". Over the build-queue depth cap the call rejects with 429. */
    import(fromImage: string, name: string, opts?: ImportTemplateOptions): Promise<TemplateInfo>;
    delete(name: string): Promise<void>;
  };

  readonly artifacts: {
    get(artifactId: string): Promise<ArtifactInfo>;
    extend(artifactId: string, ttlDays: number): Promise<ArtifactInfo>;
    delete(artifactId: string): Promise<void>;
  };

  /** Private-registry pull credentials for template import — storing
   * one here doesn't change how templates.import is called; the import
   * matches a stored credential to the pull by the image ref's registry
   * host automatically. */
  readonly registries: {
    list(): Promise<RegistryCredentialInfo[]>;
    /** Store a credential under `name`. Covers Docker Hub, GHCR, and Google
     * Artifact Registry (token/username-password auth). `secret` is
     * encrypted at rest and never returned by any read. Rotation is
     * delete-then-create for v1 — reusing a name already in use rejects
     * with a 409. */
    add(name: string, opts: AddRegistryCredentialOptions): Promise<RegistryCredentialInfo>;
    delete(name: string): Promise<void>;
  };

  /** Egress connectors: credential injection without the sandbox
   * ever seeing the secret. Register one here, then opt a session into it
   * by name (`sessions.create({ connectors: ["stripe"] })`). */
  readonly connectors: {
    list(): Promise<ConnectorInfo[]>;
    /** Register a connector under `name`. `secret` is encrypted at rest and
     * never returned by any read. Rotation is delete-then-create for v1 —
     * reusing a name already in use rejects with a 409. */
    add(name: string, opts: AddConnectorOptions): Promise<ConnectorInfo>;
    delete(name: string): Promise<void>;
  };

  /** Webhook subscriptions for session events: a subscription
   * receives the same event envelope the account-scoped event stream
   * (`GET /v1/events`) sends, signed with HMAC-SHA256 (`Inis-Signature:
   * t=<ts>,v1=<hex>`) and retried with bounded exponential backoff. */
  readonly webhooks: {
    list(): Promise<WebhookEndpointInfo[]>;
    /** Register `url`. The returned object's `secret` is the ONLY time the
     * raw signing secret is available — store it now. */
    add(url: string, opts?: AddWebhookOptions): Promise<WebhookEndpointInfo>;
    delete(endpointId: string): Promise<void>;
    /** Fire one synthetic test delivery immediately (bypassing the retry
     * queue) and return the delivered/dead result synchronously. */
    test(endpointId: string): Promise<WebhookDeliveryInfo>;
    /** The most recent deliveries for a subscription, newest first — the
     * per-subscription delivery log. */
    deliveries(endpointId: string, limit?: number): Promise<WebhookDeliveryInfo[]>;
  };

  /** Custom domains with auto-TLS for exposed session ports: CNAME a
   * customer-owned hostname to the fleet's ingress, verify DNS ownership,
   * then route it to a session's exposed port in place of the default
   * preview URL. v1 binds directly to a (sessionId, port) pair rather than
   * a longer-lived project route. */
  readonly domains: {
    /** List this org's registered domains, verified and unverified. */
    list(): Promise<DomainInfo[]>;
    /** Register `domain` as a pending (unverified) custom domain. The
     * response's `verifyTxtName`/`verifyTxtValue` are the TXT record to
     * create before calling `verify()` (a CNAME to the fleet's ingress
     * also satisfies verification). */
    add(domain: string): Promise<DomainInfo>;
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

  constructor(opts: ClientOptions = {}) {
    this.token = resolveToken(opts.token);
    this.baseUrl = resolveBaseUrl(opts.baseUrl);
    this.timeoutMs = opts.timeoutMs ?? 120_000;

    this.sessions = {
      create: (opts?: CreateSessionOptions) => Session.create(this, opts),
      attach: (sessionId: string) => Session.attach(this, sessionId),
      list: (opts?: ListSessionsOptions) => this._listSessions(opts),
      get: (sessionId: string) => this._getSession(sessionId),
      pause: (sessionId: string) => this._pauseSession(sessionId),
      resume: (sessionId: string) => this._resumeSession(sessionId),
      fork: (sessionId: string, count: number) => this._forkSession(sessionId, count),
      archiveRetry: (sessionId: string) => this._archiveRetry(sessionId),
      batchExec: (opts: BatchExecOptions) => this._batchExec(opts),
      readFile: (sessionId: string, path: string, encoding?: "text" | "base64") =>
        encoding === "base64"
          ? Session.attach(this, sessionId).readFile(path, "base64")
          : Session.attach(this, sessionId).readFile(path),
      listFiles: (sessionId: string, path: string) =>
        Session.attach(this, sessionId).listFiles(path),
      writeFile: (
        sessionId: string,
        path: string,
        content: string | Uint8Array,
        encoding?: "text" | "base64",
      ) => Session.attach(this, sessionId).writeFile(path, content, encoding),
      writeFiles: (sessionId: string, files: FileBatchItem[]) =>
        Session.attach(this, sessionId).writeFiles(files),
      findFiles: (sessionId: string, pattern: string, opts?: FindFilesOptions) =>
        Session.attach(this, sessionId).findFiles(pattern, opts),
      grepFiles: (sessionId: string, pattern: string, opts?: GrepFilesOptions) =>
        Session.attach(this, sessionId).grepFiles(pattern, opts),
    };

    this.checkpoints = {
      get: (checkpointId: string) => this._getCheckpoint(checkpointId),
      delete: (checkpointId: string) => this._deleteCheckpoint(checkpointId),
      createSession: (checkpointId: string, opts?: CheckpointSessionOptions) =>
        this._createSessionFromCheckpoint(checkpointId, opts),
    };

    this.templates = {
      list: () => this._listTemplates(),
      import: (fromImage: string, name: string, opts?: ImportTemplateOptions) =>
        this._importTemplate(fromImage, name, opts),
      delete: (name: string) => this._deleteTemplate(name),
    };

    this.artifacts = {
      get: (artifactId: string) => this._getArtifact(artifactId),
      extend: (artifactId: string, ttlDays: number) => this._extendArtifact(artifactId, ttlDays),
      delete: (artifactId: string) => this._deleteArtifact(artifactId),
    };

    this.registries = {
      list: () => this._listRegistryCredentials(),
      add: (name: string, opts: AddRegistryCredentialOptions) => this._addRegistryCredential(name, opts),
      delete: (name: string) => this._deleteRegistryCredential(name),
    };

    this.connectors = {
      list: () => this._listConnectors(),
      add: (name: string, opts: AddConnectorOptions) => this._addConnector(name, opts),
      delete: (name: string) => this._deleteConnector(name),
    };

    this.webhooks = {
      list: () => this._listWebhooks(),
      add: (url: string, opts?: AddWebhookOptions) => this._addWebhook(url, opts),
      delete: (endpointId: string) => this._deleteWebhook(endpointId),
      test: (endpointId: string) => this._testWebhook(endpointId),
      deliveries: (endpointId: string, limit?: number) => this._webhookDeliveries(endpointId, limit),
    };

    this.domains = {
      list: () => this._listDomains(),
      add: (domain: string) => this._addDomain(domain),
      delete: (domainId: string) => this._deleteDomain(domainId),
      verify: (domainId: string) => this._verifyDomain(domainId),
      route: (domainId: string, sessionId: string, port: number) => this._setDomainRoute(domainId, sessionId, port),
      unroute: (domainId: string) => this._clearDomainRoute(domainId),
    };
  }

  private async _listSessions(opts?: ListSessionsOptions): Promise<{ sessions: SessionInfo[]; nextCursor?: string }> {
    const params = new URLSearchParams();
    if (opts?.state) params.set("state", opts.state);
    if (opts?.limit) params.set("limit", String(opts.limit));
    if (opts?.cursor) params.set("cursor", opts.cursor);
    const query = params.toString() ? `?${params.toString()}` : "";
    const data = await request<{ sessions?: Record<string, unknown>[]; next_cursor?: string }>(
      this.baseUrl, this.token, "GET", `/v1/sessions${query}`, undefined, this.timeoutMs,
    );
    return {
      sessions: (data.sessions ?? []).map(mapSessionInfo),
      nextCursor: data.next_cursor,
    };
  }

  private async _getSession(sessionId: string): Promise<SessionInfo> {
    const data = await request<Record<string, unknown>>(
      this.baseUrl, this.token, "GET", `/v1/sessions/${sessionId}`, undefined, this.timeoutMs,
    );
    return mapSessionInfo(data);
  }

  private async _pauseSession(sessionId: string): Promise<SessionInfo> {
    const data = await request<Record<string, unknown>>(
      this.baseUrl, this.token, "POST", `/v1/sessions/${sessionId}/pause`, {}, this.timeoutMs,
    );
    return mapSessionInfo(data);
  }

  private async _resumeSession(sessionId: string): Promise<SessionInfo> {
    const data = await request<Record<string, unknown>>(
      this.baseUrl, this.token, "POST", `/v1/sessions/${sessionId}/resume`, {}, this.timeoutMs,
    );
    return mapSessionInfo(data);
  }

  private async _forkSession(sessionId: string, count: number): Promise<ForkResult> {
    const data = await request<{ parent_session_id?: string; children?: string[] }>(
      this.baseUrl, this.token, "POST", `/v1/sessions/${sessionId}/fork`, { count }, this.timeoutMs,
    );
    return {
      parentSessionId: String(data.parent_session_id ?? sessionId),
      children: data.children ?? [],
    };
  }

  private async _archiveRetry(sessionId: string): Promise<ArchiveStatus> {
    const data = await request<{ status?: string; cold_uri?: string; uploaded_at?: string }>(
      this.baseUrl, this.token, "POST", `/v1/sessions/${sessionId}/archive/retry`, {}, this.timeoutMs,
    );
    return {
      status: String(data.status ?? "pending") as "pending" | "complete" | "failed",
      coldUri: data.cold_uri,
      uploadedAt: data.uploaded_at,
    };
  }

  private async _batchExec(opts: BatchExecOptions): Promise<BatchExecResult[]> {
    const command = typeof opts.command === "string" ? ["bash", "-lc", opts.command] : opts.command;
    const payload: Record<string, unknown> = {
      session_ids: opts.sessionIds,
      command,
    };
    if (opts.cwd) payload.cwd = opts.cwd;
    if (opts.timeoutMs) payload.timeout_ms = opts.timeoutMs;
    const data = await request<{ results?: Record<string, unknown>[] }>(
      this.baseUrl, this.token, "POST", "/v1/sessions/batch/exec", payload, this.timeoutMs,
    );
    return (data.results ?? []).map((r) => ({
      sessionId: String(r.session_id ?? ""),
      stdout: String(r.stdout ?? ""),
      stderr: String(r.stderr ?? ""),
      exitCode: Number(r.exit_code ?? 0),
      durationMs: Number(r.duration_ms ?? 0),
      timedOut: Boolean(r.timed_out),
      error: r.error ? String(r.error) : undefined,
      stdoutEncoding: r.stdout_encoding === "base64" ? "base64" : undefined,
      stderrEncoding: r.stderr_encoding === "base64" ? "base64" : undefined,
    }));
  }

  private async _getCheckpoint(checkpointId: string): Promise<CheckpointInfo> {
    const data = await request<Record<string, unknown>>(
      this.baseUrl, this.token, "GET", `/v1/checkpoints/${checkpointId}`, undefined, this.timeoutMs,
    );
    return mapCheckpoint(data);
  }

  private async _deleteCheckpoint(checkpointId: string): Promise<void> {
    await request<void>(
      this.baseUrl, this.token, "DELETE", `/v1/checkpoints/${checkpointId}`, undefined, this.timeoutMs,
    );
  }

  private async _createSessionFromCheckpoint(
    checkpointId: string,
    opts?: CheckpointSessionOptions,
  ): Promise<Session> {
    const body: Record<string, unknown> = {};
    if (opts?.name) body.name = opts.name;
    if (opts?.labels) body.labels = opts.labels;
    if (opts?.maxLifetimeMs) body.max_lifetime_ms = opts.maxLifetimeMs;
    if (opts?.idleTimeoutMs) body.idle_timeout_ms = opts.idleTimeoutMs;
    const data = await request<{ session_id: string }>(
      this.baseUrl, this.token, "POST", `/v1/checkpoints/${checkpointId}/sessions`, body, this.timeoutMs,
    );
    return Session.attach(this, data.session_id);
  }

  private async _listTemplates(): Promise<TemplateInfo[]> {
    const data = await request<{ templates?: Record<string, unknown>[] }>(
      this.baseUrl, this.token, "GET", "/v1/templates", undefined, this.timeoutMs,
    );
    return (data.templates ?? []).map(mapTemplate);
  }

  private async _importTemplate(
    fromImage: string,
    name: string,
    opts?: ImportTemplateOptions,
  ): Promise<TemplateInfo> {
    const body: Record<string, unknown> = { from_image: fromImage, name };
    if (opts?.description !== undefined) body.description = opts.description;
    const data = await request<Record<string, unknown>>(
      this.baseUrl, this.token, "POST", "/v1/templates", body, this.timeoutMs,
    );
    return mapTemplate(data);
  }

  private async _deleteTemplate(name: string): Promise<void> {
    await request<void>(
      this.baseUrl, this.token, "DELETE", `/v1/templates/${encodeURIComponent(name)}`, undefined, this.timeoutMs,
    );
  }

  private async _listRegistryCredentials(): Promise<RegistryCredentialInfo[]> {
    const data = await request<{ credentials?: Record<string, unknown>[] }>(
      this.baseUrl, this.token, "GET", "/v1/registry-credentials", undefined, this.timeoutMs,
    );
    return (data.credentials ?? []).map(mapRegistryCredential);
  }

  private async _addRegistryCredential(
    name: string,
    opts: AddRegistryCredentialOptions,
  ): Promise<RegistryCredentialInfo> {
    const body = {
      name,
      registry_host: opts.registryHost,
      username: opts.username,
      secret: opts.secret,
    };
    const data = await request<Record<string, unknown>>(
      this.baseUrl, this.token, "POST", "/v1/registry-credentials", body, this.timeoutMs,
    );
    return mapRegistryCredential(data);
  }

  private async _deleteRegistryCredential(name: string): Promise<void> {
    await request<void>(
      this.baseUrl, this.token, "DELETE", `/v1/registry-credentials/${encodeURIComponent(name)}`, undefined, this.timeoutMs,
    );
  }

  private async _listConnectors(): Promise<ConnectorInfo[]> {
    const data = await request<{ connectors?: Record<string, unknown>[] }>(
      this.baseUrl, this.token, "GET", "/v1/connectors", undefined, this.timeoutMs,
    );
    return (data.connectors ?? []).map(mapConnector);
  }

  private async _addConnector(
    name: string,
    opts: AddConnectorOptions,
  ): Promise<ConnectorInfo> {
    const body: Record<string, unknown> = {
      name,
      target_base_url: opts.targetBaseUrl,
      auth_shape: opts.authShape ?? "bearer",
      secret: opts.secret,
    };
    if (opts.headerName) body.header_name = opts.headerName;
    const data = await request<Record<string, unknown>>(
      this.baseUrl, this.token, "POST", "/v1/connectors", body, this.timeoutMs,
    );
    return mapConnector(data);
  }

  private async _deleteConnector(name: string): Promise<void> {
    await request<void>(
      this.baseUrl, this.token, "DELETE", `/v1/connectors/${encodeURIComponent(name)}`, undefined, this.timeoutMs,
    );
  }

  private async _listWebhooks(): Promise<WebhookEndpointInfo[]> {
    const data = await request<{ endpoints?: Record<string, unknown>[] }>(
      this.baseUrl, this.token, "GET", "/v1/org/webhooks", undefined, this.timeoutMs,
    );
    return (data.endpoints ?? []).map(mapWebhook);
  }

  private async _addWebhook(url: string, opts?: AddWebhookOptions): Promise<WebhookEndpointInfo> {
    const body: Record<string, unknown> = { url };
    if (opts?.eventTypes && opts.eventTypes.length > 0) {
      body.event_types = opts.eventTypes;
    }
    const data = await request<Record<string, unknown>>(
      this.baseUrl, this.token, "POST", "/v1/org/webhooks", body, this.timeoutMs,
    );
    return mapWebhook(data);
  }

  private async _deleteWebhook(endpointId: string): Promise<void> {
    await request<void>(
      this.baseUrl, this.token, "DELETE", `/v1/org/webhooks/${encodeURIComponent(endpointId)}`, undefined, this.timeoutMs,
    );
  }

  private async _testWebhook(endpointId: string): Promise<WebhookDeliveryInfo> {
    const data = await request<Record<string, unknown>>(
      this.baseUrl, this.token, "POST", `/v1/org/webhooks/${encodeURIComponent(endpointId)}/test`, undefined, this.timeoutMs,
    );
    return mapWebhookDelivery(data);
  }

  private async _webhookDeliveries(endpointId: string, limit?: number): Promise<WebhookDeliveryInfo[]> {
    const params = new URLSearchParams();
    if (limit !== undefined) params.set("limit", String(limit));
    const qs = params.toString();
    const path = `/v1/org/webhooks/${encodeURIComponent(endpointId)}/deliveries${qs ? `?${qs}` : ""}`;
    const data = await request<{ deliveries?: Record<string, unknown>[] }>(
      this.baseUrl, this.token, "GET", path, undefined, this.timeoutMs,
    );
    return (data.deliveries ?? []).map(mapWebhookDelivery);
  }

  private async _listDomains(): Promise<DomainInfo[]> {
    const data = await request<{ domains?: Record<string, unknown>[] }>(
      this.baseUrl, this.token, "GET", "/v1/org/domains", undefined, this.timeoutMs,
    );
    return (data.domains ?? []).map(mapDomain);
  }

  private async _addDomain(domain: string): Promise<DomainInfo> {
    const data = await request<Record<string, unknown>>(
      this.baseUrl, this.token, "POST", "/v1/org/domains", { domain }, this.timeoutMs,
    );
    return mapDomain(data);
  }

  private async _deleteDomain(domainId: string): Promise<void> {
    await request<void>(
      this.baseUrl, this.token, "DELETE", `/v1/org/domains/${encodeURIComponent(domainId)}`, undefined, this.timeoutMs,
    );
  }

  private async _verifyDomain(domainId: string): Promise<DomainInfo> {
    const data = await request<Record<string, unknown>>(
      this.baseUrl, this.token, "POST", `/v1/org/domains/${encodeURIComponent(domainId)}/verify`, undefined, this.timeoutMs,
    );
    return mapDomain(data);
  }

  private async _setDomainRoute(domainId: string, sessionId: string, port: number): Promise<DomainInfo> {
    const data = await request<Record<string, unknown>>(
      this.baseUrl, this.token, "PUT", `/v1/org/domains/${encodeURIComponent(domainId)}/route`,
      { session_id: sessionId, port }, this.timeoutMs,
    );
    return mapDomain(data);
  }

  private async _clearDomainRoute(domainId: string): Promise<DomainInfo> {
    const data = await request<Record<string, unknown>>(
      this.baseUrl, this.token, "DELETE", `/v1/org/domains/${encodeURIComponent(domainId)}/route`, undefined, this.timeoutMs,
    );
    return mapDomain(data);
  }

  private async _getArtifact(artifactId: string): Promise<ArtifactInfo> {
    const data = await request<Record<string, unknown>>(
      this.baseUrl, this.token, "GET", `/v1/artifacts/${artifactId}`, undefined, this.timeoutMs,
    );
    return mapArtifact(data);
  }

  private async _extendArtifact(artifactId: string, ttlDays: number): Promise<ArtifactInfo> {
    const data = await request<Record<string, unknown>>(
      this.baseUrl, this.token, "POST", `/v1/artifacts/${artifactId}/extend`, { ttl_days: ttlDays }, this.timeoutMs,
    );
    return mapArtifact(data);
  }

  private async _deleteArtifact(artifactId: string): Promise<void> {
    await request<void>(
      this.baseUrl, this.token, "DELETE", `/v1/artifacts/${artifactId}`, undefined, this.timeoutMs,
    );
  }

  async capacity(): Promise<Capacity> {
    const data = await request<Record<string, unknown>>(
      this.baseUrl, this.token, "GET", "/v1/org", undefined, this.timeoutMs,
    );
    const cap = (data.capacity ?? {}) as Record<string, unknown>;
    const limitsRaw = cap.limits as Record<string, unknown> | undefined;
    return {
      running: Number(cap.running ?? 0),
      warm: Number(cap.warm ?? 0),
      limits: limitsRaw
        ? { running: Number(limitsRaw.running ?? 0), warm: Number(limitsRaw.warm ?? 0) }
        : undefined,
    };
  }

  async execute(opts: {
    language: "python" | "node" | "bun";
    code: string;
    dependencies?: string[];
    volumeId?: string;
    timeoutMs?: number;
    size?: SessionSize;
    template?: string;
    noSudo?: boolean;
    commandCapture?: "hash" | "executable" | "full";
  }): Promise<ExecResult> {
    const payload: Record<string, unknown> = {
      language: opts.language,
      code: opts.code,
    };
    if (opts.dependencies) payload.dependencies = opts.dependencies;
    if (opts.volumeId) payload.volume_id = opts.volumeId;
    if (opts.timeoutMs) payload.timeout_ms = opts.timeoutMs;
    if (opts.size) payload.size = opts.size;
    if (opts.template) payload.template = opts.template;
    if (opts.noSudo) payload.no_sudo = true;
    if (opts.commandCapture) payload.command_capture = opts.commandCapture;
    const data = await request<Record<string, unknown>>(
      this.baseUrl, this.token, "POST", "/v1/sessions/exec", payload, this.timeoutMs,
    );
    return mapExecResult(data);
  }
}
