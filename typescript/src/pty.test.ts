// Tests for the PTY handle against a real local `ws` server: the
// client opens actual TCP WebSocket connections (needed for the bearer-token
// handshake header), so client.test.ts's mocked-fetch pattern doesn't apply
// here. Mirrors the exec-node's binary frame protocol (internal/api/pty.go,
// internal/protocol PTYFrame*).

import { createServer } from "node:http";
import type { AddressInfo } from "node:net";
import { afterEach, describe, expect, it } from "vitest";
import { WebSocketServer, type WebSocket as WSWebSocket } from "ws";
import { Client, InisError } from "./client.js";

const DATA = 0x01;
const RESIZE = 0x02;
const SIGNAL = 0x03;
const EXIT = 0x05;
const ERROR = 0x06;

function frame(type: number, payload: Buffer | Uint8Array): Buffer {
  return Buffer.concat([Buffer.from([type]), Buffer.from(payload)]);
}

/** Echoes DATA, acks RESIZE with a descriptive DATA frame, and replies to a
 * SIGNAL frame with an EXIT carrying the signal number as the exit code (so
 * kill()/sendSignal() are observable in a test). */
function echoResizeExitHandler(ws: WSWebSocket): void {
  ws.on("message", (msg: Buffer) => {
    const type = msg[0];
    const payload = msg.subarray(1);
    if (type === DATA) {
      ws.send(frame(DATA, Buffer.concat([Buffer.from("echo:"), payload])));
    } else if (type === RESIZE) {
      const rows = payload.readUInt16BE(0);
      const cols = payload.readUInt16BE(2);
      ws.send(frame(DATA, Buffer.from(`resized:${rows}x${cols}`)));
    } else if (type === SIGNAL) {
      const buf = Buffer.alloc(4);
      buf.writeInt32BE(payload[0]);
      ws.send(frame(EXIT, buf));
    }
  });
}

function immediateErrorHandler(ws: WSWebSocket): void {
  ws.send(frame(ERROR, Buffer.from("guest agent unreachable")));
}

interface TestServer {
  port: number;
  requestPaths: string[];
  close: () => Promise<void>;
}

/** Starts a real HTTP+WebSocketServer pair on localhost. rejectWith, if set,
 * makes every upgrade request fail the handshake with that status/body
 * instead of accepting it — emulating the exec node's 404 for a nonexistent
 * session. */
function startServer(
  handler: (ws: WSWebSocket) => void,
  opts?: { rejectWith?: { status: number; body: unknown } },
): Promise<TestServer> {
  const requestPaths: string[] = [];
  const httpServer = createServer((_req, res) => {
    res.writeHead(404).end();
  });
  const wss = new WebSocketServer({ noServer: true });
  wss.on("connection", handler);

  httpServer.on("upgrade", (req, socket, head) => {
    requestPaths.push(req.url ?? "");
    if (opts?.rejectWith) {
      const body = JSON.stringify(opts.rejectWith.body);
      socket.write(
        `HTTP/1.1 ${opts.rejectWith.status} Rejected\r\n` +
          `Content-Type: application/json\r\n` +
          `Content-Length: ${Buffer.byteLength(body)}\r\n` +
          `Connection: close\r\n\r\n${body}`,
      );
      socket.destroy();
      return;
    }
    wss.handleUpgrade(req, socket, head, (ws) => {
      wss.emit("connection", ws, req);
    });
  });

  return new Promise((resolve) => {
    httpServer.listen(0, "localhost", () => {
      const port = (httpServer.address() as AddressInfo).port;
      resolve({
        port,
        requestPaths,
        close: () =>
          new Promise((res) => {
            wss.close(() => httpServer.close(() => res()));
          }),
      });
    });
  });
}

function baseUrl(port: number): string {
  return `http://localhost:${port}`;
}

describe("Session.pty()", () => {
  let server: TestServer | undefined;

  afterEach(async () => {
    await server?.close();
    server = undefined;
  });

  it("write() sends a DATA frame and iteration yields the echoed output", async () => {
    server = await startServer(echoResizeExitHandler);
    const client = new Client({ baseUrl: baseUrl(server.port), token: "tok_abc" });
    const session = client.sessions.attach("sess_1");
    const term = await session.pty({ cols: 80, rows: 24 });
    await term.write("hi");
    const chunks: Uint8Array[] = [];
    for await (const chunk of term) {
      chunks.push(chunk);
      break;
    }
    await term.close();
    expect(Buffer.from(chunks[0]).toString()).toBe("echo:hi");
  });

  it("resize() sends rows/cols big-endian", async () => {
    server = await startServer(echoResizeExitHandler);
    const client = new Client({ baseUrl: baseUrl(server.port), token: "tok_abc" });
    const session = client.sessions.attach("sess_1");
    const term = await session.pty();
    await term.resize(120, 40);
    const chunks: Uint8Array[] = [];
    for await (const chunk of term) {
      chunks.push(chunk);
      break;
    }
    await term.close();
    expect(Buffer.from(chunks[0]).toString()).toBe("resized:40x120");
  });

  it("kill() sends SIGKILL(9); reading the resulting EXIT frame sets exitCode", async () => {
    server = await startServer(echoResizeExitHandler);
    const client = new Client({ baseUrl: baseUrl(server.port), token: "tok_abc" });
    const session = client.sessions.attach("sess_1");
    const term = await session.pty();
    await term.kill();
    for await (const _chunk of term) {
      // drain until EXIT
    }
    expect(term.exitCode).toBe(9);
  });

  it("an ERROR frame raises InisError from the iterator", async () => {
    server = await startServer(immediateErrorHandler);
    const client = new Client({ baseUrl: baseUrl(server.port), token: "tok_abc" });
    const session = client.sessions.attach("sess_1");
    const term = await session.pty();
    await expect(async () => {
      for await (const _chunk of term) {
        // should throw before yielding
      }
    }).rejects.toThrow(/guest agent unreachable/);
  });

  it("a rejected handshake (nonexistent session) raises InisError with the status", async () => {
    server = await startServer(() => {}, {
      rejectWith: { status: 404, body: { error: "session not found", code: "not_found" } },
    });
    const client = new Client({ baseUrl: baseUrl(server.port), token: "tok_abc" });
    const session = client.sessions.attach("does-not-exist");
    await expect(session.pty()).rejects.toThrow(InisError);
    await expect(session.pty()).rejects.toThrow(/404/);
  });

  it("command/cwd are sent as repeated cmd= / cwd= query params", async () => {
    server = await startServer(echoResizeExitHandler);
    const client = new Client({ baseUrl: baseUrl(server.port), token: "tok_abc" });
    const session = client.sessions.attach("sess_1");
    const term = await session.pty({ command: ["python3", "-i"], cwd: "/workspace" });
    await term.close();
    const path = server.requestPaths[0];
    expect(path).toContain("cmd=python3");
    expect(path).toContain("cmd=-i");
    expect(path).toMatch(/cwd=(%2F|\/)workspace/);
  });
});
