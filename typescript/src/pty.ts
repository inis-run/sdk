/**
 * Interactive PTY handle over the `GET /v1/sessions/{id}/pty` WebSocket
 * endpoint — the same endpoint and binary frame protocol the console
 * web terminal uses (web/console/src/routes/_app/sessions/$id.tsx) and the
 * exec-node handler implements (internal/api/pty.go, internal/protocol
 * PTYFrame*).
 *
 * Node-only (this SDK ships no browser bundle — see client.ts's header
 * comment): uses the `ws` package rather than the platform WebSocket global,
 * because the handshake needs a custom Authorization header, which the
 * WHATWG WebSocket API (and Node's built-in global implementing it) has no
 * way to set — the console's browser client instead rides the session
 * cookie, which isn't available to an SDK caller authenticating with a
 * bearer token.
 *
 * Frame wire format (both directions): one binary WebSocket message per
 * frame, `[1 byte type][payload]`. Types below must match internal/protocol's
 * PTYFrame* constants exactly:
 *   - DATA (0x01): raw bytes. Host->guest: stdin. Guest->host: output.
 *   - RESIZE (0x02): host->guest only. 4 bytes: rows uint16 BE, cols uint16 BE.
 *   - SIGNAL (0x03): host->guest only. 1 byte: POSIX signal number (e.g. 9 =
 *     SIGKILL, to force-kill an unresponsive foreground process).
 *   - EXIT (0x05): guest->host only. 4 bytes: exit code, int32 BE. The server
 *     closes the socket immediately after, so this is always the last frame.
 *   - ERROR (0x06): guest->host only. UTF-8 message describing a
 *     protocol-level failure (as opposed to the shell/command's own exit code).
 */

import WebSocket from "ws";
import { InisError } from "./client.js";

const DATA = 0x01;
const RESIZE = 0x02;
const SIGNAL = 0x03;
const EXIT = 0x05;
const ERROR = 0x06;

// Mirrors internal/protocol.MaxPTYFrameBytes (1<<20) plus the 1-byte type
// prefix and a little headroom for the WebSocket framing overhead itself.
const MAX_FRAME_BYTES = (1 << 20) + 64;

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

function wsUrl(
  baseUrl: string,
  sessionId: string,
  opts: Required<Pick<PTYOptions, "cols" | "rows" | "term" | "colorterm">> &
    Pick<PTYOptions, "command" | "cwd">,
): string {
  let scheme: string;
  let host: string;
  if (baseUrl.startsWith("https://")) {
    scheme = "wss";
    host = baseUrl.slice("https://".length);
  } else if (baseUrl.startsWith("http://")) {
    scheme = "ws";
    host = baseUrl.slice("http://".length);
  } else {
    const idx = baseUrl.indexOf("://");
    scheme = baseUrl.slice(0, idx);
    host = baseUrl.slice(idx + 3);
  }
  const params = new URLSearchParams();
  params.set("rows", String(opts.rows));
  params.set("cols", String(opts.cols));
  if (opts.term) params.set("term", opts.term);
  if (opts.colorterm) params.set("colorterm", opts.colorterm);
  for (const part of opts.command ?? []) params.append("cmd", part);
  if (opts.cwd) params.set("cwd", opts.cwd);
  return `${scheme}://${host.replace(/\/+$/, "")}/v1/sessions/${sessionId}/pty?${params.toString()}`;
}

/** Terminal state of a PTY once its shell/command has exited. */
export interface PTYExitInfo {
  exitCode: number;
}

/**
 * A live interactive pseudo-terminal. Returned by `Session.pty()`; async
 * iterate for output, call write()/resize() to send input, and read
 * .exitCode once iteration ends.
 *
 * Usage:
 * ```typescript
 * const term = await session.pty({ cols: 80, rows: 24 });
 * try {
 *   await term.write("echo hi\n");
 *   for await (const chunk of term) {
 *     process.stdout.write(chunk);
 *   }
 * } finally {
 *   await term.close();
 * }
 * ```
 */
export class PTY {
  private _exitCode: number | null = null;
  private _closed = false;
  private _frames: Array<{ type: number; payload: Buffer }> = [];
  private _wake: (() => void) | null = null;
  private _peerClosed = false;

  private constructor(private readonly _ws: WebSocket) {
    // Registered synchronously, right when the WebSocket is constructed (see
    // _open below) — NOT after the "open" promise resolves. The server can
    // send its first frame the instant the handshake completes, which lands
    // in the same synchronous burst of socket-data processing as the "open"
    // event itself; deferring these listeners until after `await`-ing open
    // loses that race and drops the frame (observed live: an immediate
    // PTY_ERROR from the mock server never reached the iterator).
    _ws.on("message", (data: Buffer) => {
      if (data.length === 0) return;
      this._frames.push({ type: data[0], payload: data.subarray(1) });
      this._wake?.();
    });
    _ws.on("close", () => {
      this._peerClosed = true;
      this._wake?.();
    });
    // A no-op listener is required: without one, "error" events on an
    // EventEmitter with no listener throw and crash the process. The open()
    // promise/iteration surface exceptions to the caller instead.
    _ws.on("error", () => {
      this._peerClosed = true;
      this._wake?.();
    });
  }

  /** @internal use Session.pty() */
  static async _open(
    baseUrl: string,
    token: string,
    sessionId: string,
    opts?: PTYOptions,
  ): Promise<PTY> {
    const url = wsUrl(baseUrl, sessionId, {
      cols: opts?.cols ?? 80,
      rows: opts?.rows ?? 24,
      term: opts?.term ?? "xterm-256color",
      colorterm: opts?.colorterm ?? "truecolor",
      command: opts?.command,
      cwd: opts?.cwd,
    });
    const ws = new WebSocket(url, {
      headers: { Authorization: `Bearer ${token}` },
      maxPayload: MAX_FRAME_BYTES,
      handshakeTimeout: opts?.openTimeoutMs ?? 30_000,
    });
    const pty = new PTY(ws);
    await new Promise<void>((resolve, reject) => {
      const onOpen = () => {
        cleanup();
        resolve();
      };
      const onError = (err: Error) => {
        cleanup();
        reject(new InisError(`pty connection failed: ${err.message}`));
      };
      const onUnexpectedResponse = (_req: unknown, res: NodeJS.ReadableStream & { statusCode?: number }) => {
        cleanup();
        const chunks: Buffer[] = [];
        res.on("data", (c: Buffer) => chunks.push(c));
        res.on("end", () => {
          const body = Buffer.concat(chunks).toString("utf-8");
          let detail = body;
          try {
            const parsed = JSON.parse(body);
            if (parsed && typeof parsed.error === "string") detail = parsed.error;
          } catch {
            /* body wasn't JSON — use it verbatim */
          }
          reject(new InisError(`${res.statusCode ?? "?"}: ${detail || "pty handshake rejected"}`));
        });
      };
      function cleanup() {
        ws.off("open", onOpen);
        ws.off("error", onError);
        ws.off("unexpected-response", onUnexpectedResponse);
      }
      ws.on("open", onOpen);
      ws.on("error", onError);
      ws.on("unexpected-response", onUnexpectedResponse);
    });
    return pty;
  }

  private _send(type: number, payload: Uint8Array): Promise<void> {
    if (this._closed || this._ws.readyState !== WebSocket.OPEN) {
      return Promise.reject(new InisError("pty is closed"));
    }
    const msg = new Uint8Array(payload.length + 1);
    msg[0] = type;
    msg.set(payload, 1);
    return new Promise((resolve, reject) => {
      this._ws.send(msg, (err) => (err ? reject(new InisError(err.message)) : resolve()));
    });
  }

  /** Send input, as if typed at the keyboard. */
  async write(data: string | Uint8Array): Promise<void> {
    const bytes = typeof data === "string" ? new TextEncoder().encode(data) : data;
    await this._send(DATA, bytes);
  }

  /** Tell the guest shell the terminal viewport changed size. */
  async resize(cols: number, rows: number): Promise<void> {
    const b = new Uint8Array(4);
    new DataView(b.buffer).setUint16(0, rows);
    new DataView(b.buffer).setUint16(2, cols);
    await this._send(RESIZE, b);
  }

  /**
   * Send a POSIX signal to the foreground process (e.g. 9 = SIGKILL, for a
   * hung command a plain close() won't interrupt).
   */
  async sendSignal(sig: number): Promise<void> {
    await this._send(SIGNAL, new Uint8Array([sig & 0xff]));
  }

  /**
   * Force-kill the foreground process (SIGKILL).
   *
   * Does not itself close the connection — the guest still sends a PTY_EXIT
   * frame in response, which iterating (or a caller already mid-iteration)
   * needs to see to learn .exitCode. Call close() once you're done reading.
   */
  async kill(): Promise<void> {
    await this.sendSignal(9);
  }

  /** The command's exit code, or null until PTY_EXIT has arrived. */
  get exitCode(): number | null {
    return this._exitCode;
  }

  /**
   * Async-iterate output chunks as they arrive. Stops when the command exits
   * (check .exitCode afterward) or the connection drops.
   *
   * Breaking out of a `for await` loop early (e.g. once you've seen a
   * prompt you were waiting for) does NOT close the connection — you're
   * expected to keep writing/iterating later. Only a natural end (PTY_EXIT,
   * a PTY_ERROR frame, or the peer dropping the connection) closes it.
   * Deliberately no try/finally here: `break`ing a `for await` calls
   * `.return()` on this generator, which resumes it as a `return` at its
   * current suspension point — a `finally` would run on THAT early return
   * too, closing the socket out from under a caller who only meant to pause
   * reading (observed live: draining to a shell prompt, then writing a
   * follow-up command, hit "pty is closed").
   */
  async *[Symbol.asyncIterator](): AsyncIterator<Uint8Array> {
    for (;;) {
      if (this._frames.length === 0) {
        if (this._peerClosed) {
          await this.close();
          return;
        }
        await new Promise<void>((resolve) => {
          this._wake = resolve;
        });
        this._wake = null;
        continue;
      }
      const frame = this._frames.shift()!;
      if (frame.type === DATA) {
        yield new Uint8Array(frame.payload);
      } else if (frame.type === EXIT) {
        this._exitCode = frame.payload.length >= 4 ? frame.payload.readInt32BE(0) : 0;
        await this.close();
        return;
      } else if (frame.type === ERROR) {
        const message = frame.payload.toString("utf-8");
        await this.close();
        throw new InisError(message);
      }
    }
  }

  async close(): Promise<void> {
    if (this._closed) return;
    this._closed = true;
    if (this._ws.readyState === WebSocket.OPEN || this._ws.readyState === WebSocket.CONNECTING) {
      this._ws.close();
    }
  }

  async [Symbol.asyncDispose](): Promise<void> {
    await this.close();
  }
}
