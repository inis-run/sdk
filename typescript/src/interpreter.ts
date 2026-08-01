// Code-interpreter protocol helpers: path/protocol constants and the
// request/result envelope logic shared by Session.runCode / restartContext
// in client.ts. See sdk/python/inis/_kernel_script.py for the guest-side
// half of this protocol and the full design rationale (no guest-agent or
// template changes; a small stdlib-only script uploaded and run as a
// background process, driven entirely through exec/writeFile/readFile/
// startProcess).

import { KERNEL_SCRIPT, KERNEL_SCRIPT_VERSION } from "./kernel_script.js";

export { KERNEL_SCRIPT, KERNEL_SCRIPT_VERSION };

export const KERNEL_NAME = "inis-kernel";
export const ROOT_DIR = "/workspace/.inis_kernel";
export const KERNEL_PATH = `${ROOT_DIR}/kernel.py`;
export const VERSION_PATH = `${ROOT_DIR}/version`;
export const REQUESTS_DIR = `${ROOT_DIR}/requests`;
export const RESULTS_DIR = `${ROOT_DIR}/results`;
export const ARTIFACTS_DIR = `${ROOT_DIR}/artifacts`;

export const DEFAULT_TIMEOUT_MS = 30_000;
export const POLL_INTERVAL_S = 0.05;
const EXEC_TIMEOUT_BUFFER_MS = 10_000;
export const TIMEOUT_MARKER = "__inis_timeout__";

/**
 * One typed piece of runCode() output.
 *
 * type is one of:
 *   "text"  — stdout/stderr captured during the cell, or the repr() of its
 *             last expression (stream distinguishes which: "stdout",
 *             "stderr", or "result").
 *   "image" — a matplotlib figure captured on show/implicit display.
 *             format is "png"; data holds the decoded image bytes.
 *   "table" — a pandas DataFrame/Series last expression, as columns + rows
 *             (capped at 1000 rows — see rowCount/truncated for the
 *             untruncated count).
 *   "json"  — a dict/list/tuple last expression that isn't a DataFrame.
 *   "error" — an uncaught exception: ename/evalue/traceback. The
 *             interpreter context is untouched and stays usable.
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
  /** Guest-side artifact path (type=="image"). */
  path?: string;
}

export function newRequestId(): string {
  // crypto.randomUUID is available in every Node/browser runtime this SDK
  // targets (Node >=18.0 per package.json engines — randomUUID has been on
  // globalThis.crypto since 19.0/webcrypto backport, and on node:crypto
  // since 14.17). Use node:crypto directly so it also works on 18.x, where
  // globalThis.crypto isn't guaranteed.
  return randomUUIDHex();
}

function randomUUIDHex(): string {
  const bytes = new Uint8Array(16);
  if (typeof globalThis.crypto?.getRandomValues === "function") {
    globalThis.crypto.getRandomValues(bytes);
  } else {
    for (let i = 0; i < bytes.length; i++) bytes[i] = Math.floor(Math.random() * 256);
  }
  return Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
}

export function mkdirsCommand(): string {
  return `mkdir -p '${REQUESTS_DIR}' '${RESULTS_DIR}' '${ARTIFACTS_DIR}'`;
}

export function requestPaths(reqId: string): [string, string] {
  return [`${REQUESTS_DIR}/.${reqId}.tmp`, `${REQUESTS_DIR}/${reqId}.json`];
}

export function resultPath(reqId: string): string {
  return `${RESULTS_DIR}/${reqId}.json`;
}

export function encodeRequest(reqId: string, code: string): string {
  return JSON.stringify({ id: reqId, code });
}

/** Timeout to pass to exec() itself: comfortably above the in-guest wait
 * loop's own timeout so that loop — not the HTTP transport — is what fires
 * first and returns the graceful TIMEOUT_MARKER. */
export function execTimeoutMs(timeoutMs: number): number {
  return timeoutMs + EXEC_TIMEOUT_BUFFER_MS;
}

/** Shell command run via exec(): atomically renames the request file into
 * place, then polls in-guest (no extra HTTP round trips) for the matching
 * result file, printing it once it appears. Gives up after timeoutMs and
 * prints TIMEOUT_MARKER instead of hanging forever. */
export function waitCommand(
  reqId: string,
  tmpPath: string,
  finalPath: string,
  timeoutMs: number,
): string {
  const resPath = resultPath(reqId);
  const iterations = Math.max(1, Math.floor(timeoutMs / (POLL_INTERVAL_S * 1000)) + 1);
  return (
    `mv '${tmpPath}' '${finalPath}' && ` +
    `i=0; while [ ! -f '${resPath}' ]; do ` +
    `i=$((i+1)); if [ $i -ge ${iterations} ]; then break; fi; ` +
    `sleep ${POLL_INTERVAL_S}; done; ` +
    `if [ -f '${resPath}' ]; then cat '${resPath}'; rm -f '${resPath}'; ` +
    `else echo '{"${TIMEOUT_MARKER}": true}'; fi`
  );
}

/** Parse the wait command's stdout. Returns the raw result-item objects on
 * success, or null if the kernel didn't answer in time (TIMEOUT_MARKER) or
 * the output wasn't a well-formed envelope for this request — both mean
 * "go check whether the kernel is still alive". */
export function parseEnvelope(stdout: string, reqId: string): Record<string, unknown>[] | null {
  const trimmed = (stdout ?? "").trim();
  if (!trimmed) return null;
  let payload: unknown;
  try {
    payload = JSON.parse(trimmed);
  } catch {
    return null;
  }
  if (typeof payload !== "object" || payload === null) return null;
  const obj = payload as Record<string, unknown>;
  if (obj[TIMEOUT_MARKER]) return null;
  if (obj.id !== reqId) return null;
  if (!Array.isArray(obj.results)) return null;
  return obj.results as Record<string, unknown>[];
}

export function rawToResult(raw: Record<string, unknown>): InterpreterResult {
  return {
    type: (raw.type as InterpreterResult["type"]) ?? "text",
    text: raw.text as string | undefined,
    stream: raw.stream as InterpreterResult["stream"],
    format: raw.format as string | undefined,
    size: raw.size as number | undefined,
    columns: raw.columns as string[] | undefined,
    rows: raw.rows as Record<string, unknown>[] | undefined,
    rowCount: raw.row_count as number | undefined,
    truncated: raw.truncated as boolean | undefined,
    json: raw.json,
    ename: raw.ename as string | undefined,
    evalue: raw.evalue as string | undefined,
    traceback: raw.traceback as string | undefined,
    path: raw.path as string | undefined,
  };
}

export function timeoutResult(timeoutMs: number): InterpreterResult {
  return {
    type: "error",
    ename: "TimeoutError",
    evalue: `runCode timed out after ${timeoutMs}ms (context left intact)`,
  };
}
