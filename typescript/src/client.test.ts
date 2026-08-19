// Mocked-fetch tests for the TypeScript SDK: request construction, response
// mapping, and error handling for the client-surface redesign (Session.get,
// pause, resume, fork, archiveRetry, readFile, listFiles, writeFile, destroy;
// Client.sessions.batchExec/get/pause/resume/fork).

import { createHash } from "node:crypto";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { Client, InisError, Session } from "./client.js";

interface RecordedCall {
  method: string;
  url: string;
  path: string;
  headers: Record<string, string>;
  body: unknown;
}

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

function errorResponse(
  status: number,
  error: string,
  opts?: { code?: string; headers?: Record<string, string> },
): Response {
  const body: Record<string, unknown> = { error };
  if (opts?.code !== undefined) body.code = opts.code;
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json", ...opts?.headers },
  });
}

function installFetch(
  handler: (call: RecordedCall) => Response,
): { calls: RecordedCall[] } {
  const calls: RecordedCall[] = [];
  const fetchMock = vi.fn(async (input: string | URL, init?: RequestInit) => {
    const url = String(input);
    const [path] = url.split("?");
    const headers = (init?.headers ?? {}) as Record<string, string>;
    const body = init?.body ? JSON.parse(String(init.body)) : undefined;
    const call: RecordedCall = { method: init?.method ?? "GET", url, path, headers, body };
    calls.push(call);
    return handler(call);
  });
  vi.stubGlobal("fetch", fetchMock);
  return { calls };
}

function makeClient(): Client {
  return new Client({ token: "tok_abc", baseUrl: "https://api.inis.run" });
}

beforeEach(() => {
  vi.unstubAllGlobals();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("Session lifecycle", () => {
  it("get() maps fields including on_pty_detach and egress.mode", async () => {
    const { calls } = installFetch(() =>
      jsonResponse(200, {
        session_id: "sess_123",
        state: "live",
        on_pty_detach: "pause",
        egress: { mode: "deny", allow: ["api.openai.com"] },
      }),
    );
    const session = Session.attach(makeClient(), "sess_123");

    const info = await session.get();

    expect(calls[0].method).toBe("GET");
    expect(calls[0].path).toBe("https://api.inis.run/v1/sessions/sess_123");
    expect(calls[0].headers.Authorization).toBe("Bearer tok_abc");
    expect(info.onPtyDetach).toBe("pause");
    expect(info.egress).toEqual({ mode: "deny", allow: ["api.openai.com"] });
  });

  it("egress 'default' field is still accepted for backward compat", async () => {
    installFetch(() =>
      jsonResponse(200, { session_id: "sess_123", state: "live", egress: { default: "allow" } }),
    );
    const info = await Session.attach(makeClient(), "sess_123").get();
    expect(info.egress?.mode).toBe("allow");
  });

  it("get() maps env in full and secretNames as names only, never values", async () => {
    // The response fixture never includes a secret VALUE — the real API
    // never sends one; this pins the client-side mapping to that contract.
    installFetch(() =>
      jsonResponse(200, {
        session_id: "sess_123",
        state: "live",
        env: { MODE: "prod" },
        secret_names: ["API_KEY", "DB_PASSWORD"],
      }),
    );
    const info = await Session.attach(makeClient(), "sess_123").get();
    expect(info.env).toEqual({ MODE: "prod" });
    expect(info.secretNames).toEqual(["API_KEY", "DB_PASSWORD"]);
  });

  it("pause() and resume() hit the right endpoints", async () => {
    const { calls } = installFetch((call) =>
      jsonResponse(200, {
        session_id: "sess_123",
        state: call.path.endsWith("/pause") ? "paused" : "live",
      }),
    );
    const session = Session.attach(makeClient(), "sess_123");

    const paused = await session.pause();
    expect(calls[0].path).toBe("https://api.inis.run/v1/sessions/sess_123/pause");
    expect(paused.state).toBe("paused");

    const resumed = await session.resume();
    expect(calls[1].path).toBe("https://api.inis.run/v1/sessions/sess_123/resume");
    expect(resumed.state).toBe("live");
  });

  it("fork() sends count and maps children", async () => {
    const { calls } = installFetch(() =>
      jsonResponse(200, { parent_session_id: "sess_123", children: ["sess_a", "sess_b"] }),
    );
    const result = await Session.attach(makeClient(), "sess_123").fork(2);
    expect(calls[0].body).toEqual({ count: 2 });
    expect(result).toEqual({ parentSessionId: "sess_123", children: ["sess_a", "sess_b"] });
  });

  it("archiveRetry() maps archive status and posts to /archive/retry", async () => {
    const { calls } = installFetch(() =>
      jsonResponse(200, { status: "complete", cold_uri: "s3://x", uploaded_at: "2026-07-14T00:00:00Z" }),
    );
    const status = await Session.attach(makeClient(), "sess_123").archiveRetry();
    expect(calls[0].method).toBe("POST");
    expect(calls[0].url).toBe("https://api.inis.run/v1/sessions/sess_123/archive/retry");
    expect(status).toEqual({
      status: "complete",
      coldUri: "s3://x",
      uploadedAt: "2026-07-14T00:00:00Z",
    });
  });

  it("destroy() appends reason as a query param when given", async () => {
    const { calls } = installFetch(() => new Response(null, { status: 204 }));
    await Session.attach(makeClient(), "sess_123").destroy({ reason: "shell_exit" });
    expect(calls[0].method).toBe("DELETE");
    expect(calls[0].url).toBe("https://api.inis.run/v1/sessions/sess_123?reason=shell_exit");
  });

  it("destroy() without reason omits the query param", async () => {
    const { calls } = installFetch(() => new Response(null, { status: 204 }));
    await Session.attach(makeClient(), "sess_123").destroy();
    expect(calls[0].url).toBe("https://api.inis.run/v1/sessions/sess_123");
  });

  it("[Symbol.asyncDispose]() destroys the session, enabling `await using`", async () => {
    const { calls } = installFetch(() => new Response(null, { status: 204 }));
    const session = Session.attach(makeClient(), "sess_123");
    await session[Symbol.asyncDispose]();
    expect(calls[0].method).toBe("DELETE");
    expect(calls[0].url).toBe("https://api.inis.run/v1/sessions/sess_123");
  });
});

// `truncated` was missing from api/openapi.yaml entirely and so never reached
// ExecResult -- these pin the field through Session.exec()'s response
// mapping now that it's fixed, both when the guest sets it and the (much
// more common) absent-key case a complete response actually sends.
describe("Session.exec()", () => {
  it("maps truncated when the guest sets it", async () => {
    installFetch(() =>
      jsonResponse(200, {
        stdout: "partial output",
        stderr: "",
        exit_code: 0,
        duration_ms: 12,
        timed_out: false,
        truncated: true,
      }),
    );
    const result = await Session.attach(makeClient(), "sess_123").exec("cat bigfile");
    expect(result.truncated).toBe(true);
  });

  it("truncated is false when the field is absent from the response", async () => {
    installFetch(() =>
      jsonResponse(200, {
        stdout: "ok",
        stderr: "",
        exit_code: 0,
        duration_ms: 5,
        timed_out: false,
      }),
    );
    const result = await Session.attach(makeClient(), "sess_123").exec("echo ok");
    expect(result.truncated).toBe(false);
  });

  // stdout_encoding/stderr_encoding are set by the API when a
  // stream's output was not valid UTF-8 and had to be base64-encoded rather
  // than risk json.Marshal silently corrupting it. These pin the field
  // through Session.exec()'s response mapping, both when the API sets it
  // and the (much more common) absent-key case a plain-text response sends.
  it("maps stdoutEncoding when the API flags a non-UTF-8 stream", async () => {
    installFetch(() =>
      jsonResponse(200, {
        stdout: "QkVGT1JFLf8tQUZURVI=",
        stderr: "",
        exit_code: 0,
        duration_ms: 5,
        timed_out: false,
        stdout_encoding: "base64",
      }),
    );
    const result = await Session.attach(makeClient(), "sess_123").exec("cat binfile");
    expect(result.stdoutEncoding).toBe("base64");
    expect(result.stderrEncoding).toBeUndefined();
    expect(result.stdout).toBe("QkVGT1JFLf8tQUZURVI=");
  });

  it("stdoutEncoding/stderrEncoding are undefined for a plain-text response", async () => {
    installFetch(() =>
      jsonResponse(200, {
        stdout: "ok",
        stderr: "",
        exit_code: 0,
        duration_ms: 5,
        timed_out: false,
      }),
    );
    const result = await Session.attach(makeClient(), "sess_123").exec("echo ok");
    expect(result.stdoutEncoding).toBeUndefined();
    expect(result.stderrEncoding).toBeUndefined();
  });
});

describe("Session file operations", () => {
  it("readFile / listFiles / writeFile use path-based GET/PUT", async () => {
    const { calls } = installFetch((call) => {
      if (call.method === "GET") return jsonResponse(200, { content: "hello", entries: ["a", "b"] });
      return new Response(null, { status: 204 });
    });
    const session = Session.attach(makeClient(), "sess_123");

    expect(await session.readFile("/workspace/x.txt")).toBe("hello");
    expect(calls[0].url).toContain("op=read");
    expect(calls[0].url).toContain(encodeURIComponent("/workspace/x.txt"));

    expect(await session.listFiles("/workspace")).toEqual(["a", "b"]);
    expect(calls[1].url).toContain("op=list");

    await session.writeFile("/workspace/x.txt", "content", "base64");
    expect(calls[2].method).toBe("PUT");
    expect(calls[2].body).toEqual({ path: "/workspace/x.txt", content: "content", encoding: "base64" });
  });

  it("legacy files.* namespace delegates to POST-based file ops", async () => {
    const { calls } = installFetch(() => jsonResponse(200, { content: "hi", entries: [] }));
    const session = Session.attach(makeClient(), "sess_123");
    await session.files.read("/a");
    expect(calls[0].method).toBe("POST");
    expect(calls[0].body).toEqual({ op: "read", path: "/a" });
  });

  it("readFile/writeFile round-trip real binary bytes via base64", async () => {
    const raw = new Uint8Array([0, 1, 2, 255, 254, 137, 80, 78, 71]);
    let writtenBody: unknown;
    const { calls } = installFetch((call) => {
      if (call.method === "PUT") {
        writtenBody = call.body;
        return new Response(null, { status: 204 });
      }
      const b64 = (writtenBody as { content: string }).content;
      return jsonResponse(200, { content: b64, encoding: "base64" });
    });
    const session = Session.attach(makeClient(), "sess_123");

    await session.writeFile("/workspace/bin.dat", raw);
    expect(calls[0].body).toMatchObject({ path: "/workspace/bin.dat", encoding: "base64" });

    const got = await session.readFile("/workspace/bin.dat", "base64");
    expect(calls[1].url).toContain("encoding=base64");
    expect(Array.from(got)).toEqual(Array.from(raw));
  });

  it("writeFiles posts the batch and maps per-file results", async () => {
    const { calls } = installFetch(() =>
      jsonResponse(200, {
        results: [
          { path: "/workspace/a.txt", ok: true },
          { path: "/workspace/b.txt", ok: false, error: "boom" },
        ],
      }),
    );
    const session = Session.attach(makeClient(), "sess_123");
    const results = await session.writeFiles([
      { path: "/workspace/a.txt", content: "aaa" },
      { path: "/workspace/b.txt", content: "bbb" },
    ]);
    expect(calls[0].method).toBe("PUT");
    expect(calls[0].path).toContain("/files/batch");
    expect(results).toEqual([
      { path: "/workspace/a.txt", ok: true, error: undefined },
      { path: "/workspace/b.txt", ok: false, error: "boom" },
    ]);
  });

  it("findFiles sends pattern/max_results and maps truncated", async () => {
    const { calls } = installFetch(() =>
      jsonResponse(200, { paths: ["/workspace/a.go"], truncated: true }),
    );
    const session = Session.attach(makeClient(), "sess_123");
    const result = await session.findFiles("*.go", { maxResults: 10 });
    expect(calls[0].url).toContain("op=find");
    expect(calls[0].url).toContain("pattern=*.go");
    expect(calls[0].url).toContain("max_results=10");
    expect(result).toEqual({ paths: ["/workspace/a.go"], truncated: true });
  });

  it("grepFiles sends all query params and maps matches", async () => {
    const { calls } = installFetch(() =>
      jsonResponse(200, {
        matches: [{ path: "/workspace/a.go", line_number: 3, line: "needle" }],
        files_searched: 1,
        truncated: false,
      }),
    );
    const session = Session.attach(makeClient(), "sess_123");
    const result = await session.grepFiles("needle", {
      filePattern: "*.go",
      caseSensitive: true,
      contextLines: 2,
      maxResults: 5,
      excludeDirs: ["node_modules", ".git"],
    });
    expect(calls[0].url).toContain("op=grep");
    expect(calls[0].url).toContain("case_sensitive=true");
    expect(calls[0].url).toContain("context_lines=2");
    expect(calls[0].url).toContain(encodeURIComponent("node_modules,.git"));
    expect(result.filesSearched).toBe(1);
    expect(result.matches[0]).toEqual({
      path: "/workspace/a.go",
      lineNumber: 3,
      line: "needle",
      before: undefined,
      after: undefined,
    });
  });

  it("mkdir posts the path", async () => {
    const { calls } = installFetch(() => jsonResponse(200, { path: "/workspace/a/b" }));
    const session = Session.attach(makeClient(), "sess_123");
    await session.mkdir("/workspace/a/b");
    expect(calls[0].method).toBe("POST");
    expect(calls[0].path).toContain("/files/mkdir");
    expect(calls[0].body).toEqual({ path: "/workspace/a/b" });
  });

  it("remove sends recursive as a query param", async () => {
    const { calls } = installFetch(() => jsonResponse(200, {}));
    const session = Session.attach(makeClient(), "sess_123");
    await session.remove("/workspace/d", { recursive: true });
    expect(calls[0].method).toBe("DELETE");
    expect(calls[0].url).toContain("recursive=true");
  });

  it("remove on a nonexistent path raises InisError", async () => {
    installFetch(() => errorResponse(404, "no such file or directory"));
    const session = Session.attach(makeClient(), "sess_123");
    await expect(session.remove("/workspace/missing.txt")).rejects.toThrow(/404/);
  });

  it("rename posts path and dest_path", async () => {
    const { calls } = installFetch(() => jsonResponse(200, { path: "/a", dest_path: "/b" }));
    const session = Session.attach(makeClient(), "sess_123");
    await session.rename("/workspace/a.txt", "/workspace/b.txt");
    expect(calls[0].method).toBe("POST");
    expect(calls[0].path).toContain("/files/rename");
    expect(calls[0].body).toEqual({ path: "/workspace/a.txt", dest_path: "/workspace/b.txt" });
  });
});

describe("Session streamed file transfer", () => {
  it("uploadFile streams the local file as the request body", async () => {
    const raw = new Uint8Array(10_000);
    for (let i = 0; i < raw.length; i++) raw[i] = i % 256;
    const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "inis-ts-"));
    const localPath = path.join(tmpDir, "payload.bin");
    fs.writeFileSync(localPath, raw);

    let capturedMethod = "";
    let capturedUrl = "";
    let capturedBytes: Uint8Array | undefined;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: string | URL, init?: RequestInit) => {
        capturedMethod = init?.method ?? "GET";
        capturedUrl = String(input);
        if (init?.body) {
          const buf = await new Response(init.body as BodyInit).arrayBuffer();
          capturedBytes = new Uint8Array(buf);
        }
        return jsonResponse(200, {
          path: "/workspace/payload.bin",
          bytes: raw.length,
          sha256: "deadbeef",
          duration_ms: 5,
        });
      }),
    );

    const session = Session.attach(makeClient(), "sess_123");
    const result = await session.uploadFile(localPath, "/workspace/payload.bin");

    expect(capturedMethod).toBe("PUT");
    expect(capturedUrl).toContain("/files/stream?path=");
    expect(capturedBytes).toEqual(raw);
    expect(result.bytes).toBe(raw.length);
    expect(result.sha256).toBe("deadbeef");

    fs.rmSync(tmpDir, { recursive: true, force: true });
  });

  it("uploadFile raises InisError on a guest write failure (not a silent success)", async () => {
    const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "inis-ts-"));
    const localPath = path.join(tmpDir, "ro.bin");
    fs.writeFileSync(localPath, "data");
    vi.stubGlobal("fetch", vi.fn(async () => errorResponse(400, "permission denied")));

    const session = Session.attach(makeClient(), "sess_123");
    await expect(session.uploadFile(localPath, "/workspace/ro/ro.bin")).rejects.toThrow(/400/);

    fs.rmSync(tmpDir, { recursive: true, force: true });
  });

  it("downloadFile writes the response body to disk and hashes it", async () => {
    const raw = new Uint8Array(10_000);
    for (let i = 0; i < raw.length; i++) raw[i] = (i * 7) % 256;
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response(raw, { status: 200, headers: { "content-type": "application/octet-stream" } })),
    );

    const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "inis-ts-"));
    const localPath = path.join(tmpDir, "out.bin");
    const session = Session.attach(makeClient(), "sess_123");
    const result = await session.downloadFile("/workspace/payload.bin", localPath);

    const onDisk = fs.readFileSync(localPath);
    expect(new Uint8Array(onDisk)).toEqual(raw);
    expect(result.bytes).toBe(raw.length);
    expect(result.sha256).toBe(createHash("sha256").update(raw).digest("hex"));

    fs.rmSync(tmpDir, { recursive: true, force: true });
  });

  it("downloadFile raises InisError for a nonexistent remote file, before writing any local file", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => errorResponse(404, "no such file or directory")));

    const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "inis-ts-"));
    const localPath = path.join(tmpDir, "out.bin");
    const session = Session.attach(makeClient(), "sess_123");
    await expect(session.downloadFile("/workspace/missing.bin", localPath)).rejects.toThrow(/404/);
    expect(fs.existsSync(localPath)).toBe(false);

    fs.rmSync(tmpDir, { recursive: true, force: true });
  });
});

describe("Session checkpoint / template flow", () => {
  it("checkpoint(), checkpoints(), restore() round-trip", async () => {
    installFetch((call) => {
      if (call.method === "POST" && call.path.endsWith("/checkpoints")) {
        return jsonResponse(200, { checkpoint_id: "cp_1", session_id: "sess_123", size_bytes: 42 });
      }
      if (call.method === "GET" && call.path.endsWith("/checkpoints")) {
        return jsonResponse(200, { checkpoints: [{ checkpoint_id: "cp_1", size_bytes: 42 }] });
      }
      if (call.path.endsWith("/restore")) {
        return jsonResponse(200, { session_id: "sess_123", state: "live" });
      }
      throw new Error(`unexpected call: ${call.method} ${call.path}`);
    });
    const session = Session.attach(makeClient(), "sess_123");

    const cp = await session.checkpoint({ name: "golden" });
    expect(cp.checkpointId).toBe("cp_1");
    expect(cp.sizeBytes).toBe(42);

    const cps = await session.checkpoints();
    expect(cps).toHaveLength(1);

    const info = await session.restore("cp_1");
    expect(info.state).toBe("live");
  });

  it("saveAsTemplate() posts name and description", async () => {
    const { calls } = installFetch(() =>
      jsonResponse(200, { name: "my-template", kind: "user" }),
    );
    const template = await Session.attach(makeClient(), "sess_123").saveAsTemplate("my-template", {
      description: "golden env",
    });
    expect(calls[0].body).toEqual({ name: "my-template", description: "golden env" });
    expect(template.kind).toBe("user");
  });
});

describe("Session.create()", () => {
  it("forwards onPtyDetach, pausedTtlMs, noSudo, template, egress.mode", async () => {
    const { calls } = installFetch(() => jsonResponse(200, { session_id: "sess_new", state: "live" }));
    const session = await Session.create(makeClient(), {
      onPtyDetach: "destroy",
      pausedTtlMs: 60_000,
      noSudo: true,
      template: "python-3.12",
      egress: { mode: "deny", allow: ["*.openai.com"] },
    });
    expect(calls[0].body).toMatchObject({
      on_pty_detach: "destroy",
      paused_ttl_ms: 60_000,
      no_sudo: true,
      template: "python-3.12",
      egress: { mode: "deny", allow: ["*.openai.com"] },
    });
    expect(session.sessionId).toBe("sess_new");
  });

  it("forwards env and secrets", async () => {
    const { calls } = installFetch(() => jsonResponse(200, { session_id: "sess_new", state: "live" }));
    await Session.create(makeClient(), {
      env: { MODE: "prod" },
      secrets: { API_KEY: "sk-canary-value" },
    });
    expect(calls[0].body).toMatchObject({
      env: { MODE: "prod" },
      secrets: { API_KEY: "sk-canary-value" },
    });
  });

  it("forwards connectors opt-in list", async () => {
    const { calls } = installFetch(() => jsonResponse(200, { session_id: "sess_new", state: "live" }));
    await Session.create(makeClient(), {
      connectors: ["stripe", "acme"],
    });
    expect(calls[0].body).toMatchObject({
      connectors: ["stripe", "acme"],
    });
  });

  it("omits env/secrets from the request body when unset", async () => {
    const { calls } = installFetch(() => jsonResponse(200, { session_id: "sess_new", state: "live" }));
    await Session.create(makeClient(), {});
    expect(calls[0].body).not.toHaveProperty("env");
    expect(calls[0].body).not.toHaveProperty("secrets");
  });

  it("forwards externalId in the request body and maps it back on the response", async () => {
    const { calls } = installFetch(() =>
      jsonResponse(200, { session_id: "sess_new", state: "live", external_id: "workflow-42" }),
    );
    const session = await Session.create(makeClient(), { externalId: "workflow-42" });
    expect(calls[0].body).toMatchObject({ external_id: "workflow-42" });
    expect(session.sessionId).toBe("sess_new");
  });

  it("omits external_id from the request body when unset", async () => {
    const { calls } = installFetch(() => jsonResponse(200, { session_id: "sess_new", state: "live" }));
    await Session.create(makeClient(), {});
    expect(calls[0].body).not.toHaveProperty("external_id");
  });

  it("sends idempotencyKey as the Idempotency-Key header, never in the body", async () => {
    const { calls } = installFetch(() => jsonResponse(200, { session_id: "sess_new", state: "live" }));
    await Session.create(makeClient(), { idempotencyKey: "retry-key-1" });
    expect(calls[0].headers["Idempotency-Key"]).toBe("retry-key-1");
    expect(calls[0].body).not.toHaveProperty("idempotency_key");
  });

  it("omits the Idempotency-Key header when unset", async () => {
    const { calls } = installFetch(() => jsonResponse(200, { session_id: "sess_new", state: "live" }));
    await Session.create(makeClient(), {});
    expect(calls[0].headers["Idempotency-Key"]).toBeUndefined();
  });

  it("forwards inline connections, serializing camelCase to the wire shape", async () => {
    const { calls } = installFetch(() => jsonResponse(200, { session_id: "sess_new", state: "live" }));
    await Session.create(makeClient(), {
      connections: [
        {
          name: "stripe",
          origin: "https://api.stripe.com",
          authentication: { type: "bearer", secret: "sk_live_canary" },
          allow: { methods: ["GET", "POST"], paths: ["/v1/*"] },
          ttlSeconds: 3600,
        },
        {
          name: "acme",
          origin: "https://api.acme.example",
          authentication: { type: "header", secret: "acme-secret", headerName: "X-Api-Key" },
          allow: { methods: ["GET"], paths: ["/widgets"] },
        },
      ],
    });
    expect(calls[0].body).toMatchObject({
      connections: [
        {
          name: "stripe",
          origin: "https://api.stripe.com",
          authentication: { type: "bearer", secret: "sk_live_canary" },
          allow: { methods: ["GET", "POST"], paths: ["/v1/*"] },
          ttl_seconds: 3600,
        },
        {
          name: "acme",
          origin: "https://api.acme.example",
          authentication: { type: "header", secret: "acme-secret", header_name: "X-Api-Key" },
          allow: { methods: ["GET"], paths: ["/widgets"] },
        },
      ],
    });
    // bearer's authentication never grows a header_name on the wire.
    const sentAuth = (calls[0].body as { connections: Array<{ authentication: Record<string, unknown> }> })
      .connections[0].authentication;
    expect(sentAuth).not.toHaveProperty("header_name");
  });

  it("omits connections from the request body when unset", async () => {
    const { calls } = installFetch(() => jsonResponse(200, { session_id: "sess_new", state: "live" }));
    await Session.create(makeClient(), {});
    expect(calls[0].body).not.toHaveProperty("connections");
  });
});

describe("Session Connections", () => {
  it("get() maps SessionInfo.connections to redacted ConnectionStatus entries, never a secret", async () => {
    installFetch(() =>
      jsonResponse(200, {
        session_id: "sess_123",
        state: "live",
        connections: [
          {
            name: "stripe",
            origin: "https://api.stripe.com",
            state: "active",
            header_name: "Authorization",
            allow: { methods: ["GET", "POST"], paths: ["/v1/*"] },
            expires_at: "2026-08-05T12:00:00Z",
            created_at: "2026-08-05T11:00:00Z",
            // A server bug or future field leaking a secret must never
            // surface through the mapper -- assert on this below.
            secret: "sk_live_should_never_appear",
          },
        ],
      }),
    );
    const info = await Session.attach(makeClient(), "sess_123").get();
    expect(info.connections).toEqual([
      {
        name: "stripe",
        origin: "https://api.stripe.com",
        state: "active",
        headerName: "Authorization",
        allow: { methods: ["GET", "POST"], paths: ["/v1/*"] },
        expiresAt: "2026-08-05T12:00:00Z",
        createdAt: "2026-08-05T11:00:00Z",
      },
    ]);
    expect(JSON.stringify(info.connections)).not.toContain("sk_live_should_never_appear");
  });

  it("get() maps state: \"expired\" (a lease past its TTL, flipped lazily on read)", async () => {
    installFetch(() =>
      jsonResponse(200, {
        session_id: "sess_123",
        state: "live",
        connections: [
          {
            name: "stripe",
            origin: "https://api.stripe.com",
            state: "expired",
            allow: { methods: ["GET"], paths: ["/v1/*"] },
          },
        ],
      }),
    );
    const info = await Session.attach(makeClient(), "sess_123").get();
    expect(info.connections?.[0].state).toBe("expired");
  });

  it("get() leaves connections undefined when the session has none", async () => {
    installFetch(() => jsonResponse(200, { session_id: "sess_123", state: "live" }));
    const info = await Session.attach(makeClient(), "sess_123").get();
    expect(info.connections).toBeUndefined();
  });

  it("rotateConnection() PUTs to the per-name endpoint, serializes the body, and maps the redacted response", async () => {
    const { calls } = installFetch(() =>
      jsonResponse(200, {
        name: "stripe",
        origin: "https://api.stripe.com",
        state: "active",
        header_name: "Authorization",
        allow: { methods: ["GET"], paths: ["/v1/*"] },
        expires_at: "2026-08-05T13:00:00Z",
      }),
    );
    const status = await Session.attach(makeClient(), "sess_123").rotateConnection("stripe", {
      origin: "https://api.stripe.com",
      authentication: { type: "bearer", secret: "sk_live_rotated" },
      allow: { methods: ["GET"], paths: ["/v1/*"] },
      ttlSeconds: 1800,
    });
    expect(calls[0].method).toBe("PUT");
    expect(calls[0].path).toBe("https://api.inis.run/v1/sessions/sess_123/connections/stripe");
    expect(calls[0].body).toMatchObject({
      origin: "https://api.stripe.com",
      authentication: { type: "bearer", secret: "sk_live_rotated" },
      allow: { methods: ["GET"], paths: ["/v1/*"] },
      ttl_seconds: 1800,
    });
    // The rotate body has no `name` field -- it's fixed by the URL, not the body.
    expect(calls[0].body).not.toHaveProperty("name");
    expect(status).toEqual({
      name: "stripe",
      origin: "https://api.stripe.com",
      state: "active",
      headerName: "Authorization",
      allow: { methods: ["GET"], paths: ["/v1/*"] },
      expiresAt: "2026-08-05T13:00:00Z",
      createdAt: undefined,
    });
  });

  it("rotateConnection() URL-encodes the connection name", async () => {
    const { calls } = installFetch(() =>
      jsonResponse(200, { name: "a b", origin: "https://api.example.com", state: "active", allow: {} }),
    );
    await Session.attach(makeClient(), "sess_123").rotateConnection("a b", {
      origin: "https://api.example.com",
      authentication: { type: "bearer", secret: "sk" },
      allow: { methods: ["GET"], paths: ["/"] },
    });
    expect(calls[0].path).toBe("https://api.inis.run/v1/sessions/sess_123/connections/a%20b");
  });

  it("rotateConnection() surfaces a 400 origin mismatch as an InisError", async () => {
    installFetch(() => errorResponse(400, "Connection origin cannot be changed; revoke it and create a new session"));
    await expect(
      Session.attach(makeClient(), "sess_123").rotateConnection("stripe", {
        origin: "https://different.example.com",
        authentication: { type: "bearer", secret: "sk" },
        allow: { methods: ["GET"], paths: ["/"] },
      }),
    ).rejects.toThrow(/400/);
  });

  it("revokeConnection() DELETEs the per-name endpoint", async () => {
    const { calls } = installFetch(() => new Response(null, { status: 204 }));
    await Session.attach(makeClient(), "sess_123").revokeConnection("stripe");
    expect(calls[0].method).toBe("DELETE");
    expect(calls[0].path).toBe("https://api.inis.run/v1/sessions/sess_123/connections/stripe");
    expect(calls[0].body).toBeUndefined();
  });

  it("revokeConnection() surfaces a 404 for an unknown name as an InisError", async () => {
    installFetch(() => errorResponse(404, "Connection not found"));
    await expect(Session.attach(makeClient(), "sess_123").revokeConnection("missing")).rejects.toThrow(/404/);
  });
});

// POST /v1/sessions acknowledges immediately with state="creating" instead
// of blocking until the sandbox is ready. Session.create() keeps its
// historical blocking contract by polling GET internally — these cover that
// poll loop directly (the already-live fast path, progressing through
// phases to live, surfacing a failure, the onStatus callback, and the
// wait:false escape hatch). Fake timers stand in for the real ~300ms poll
// cadence so these run instantly.
describe("Session.create() async provisioning", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it("returns immediately with no GET polling when already live", async () => {
    const { calls } = installFetch(() => jsonResponse(200, { session_id: "sess_new", state: "live" }));
    const session = await Session.create(makeClient(), {});
    expect(session.sessionId).toBe("sess_new");
    expect(calls).toHaveLength(1); // just the POST
  });

  it("polls through creating phases to live", async () => {
    let n = 0;
    const responses = [
      () => jsonResponse(200, { session_id: "sess_new", state: "creating", status_detail: "preparing_template" }),
      () => jsonResponse(200, { session_id: "sess_new", state: "creating", status_detail: "starting" }),
      () => jsonResponse(200, { session_id: "sess_new", state: "live" }),
    ];
    const { calls } = installFetch(() => responses[Math.min(n++, responses.length - 1)]());

    const createPromise = Session.create(makeClient(), {});
    // Two 300ms poll ticks are needed to walk through the two "creating"
    // responses before the loop observes the third (live) one.
    await vi.advanceTimersByTimeAsync(1000);
    const session = await createPromise;

    expect(session.sessionId).toBe("sess_new");
    expect(calls).toHaveLength(3); // 1 POST + 2 GET polls
    expect(calls[0].method).toBe("POST");
    expect(calls[1].method).toBe("GET");
    expect(calls[2].method).toBe("GET");
  });

  it("rejects with the status_reason when provisioning fails", async () => {
    let n = 0;
    const responses = [
      () => jsonResponse(200, { session_id: "sess_new", state: "creating", status_detail: "preparing_template" }),
      () => jsonResponse(200, { session_id: "sess_new", state: "failed", status_reason: "template not found" }),
    ];
    installFetch(() => responses[Math.min(n++, responses.length - 1)]());

    const createPromise = Session.create(makeClient(), {});
    const assertion = expect(createPromise).rejects.toThrow(/template not found/);
    await vi.advanceTimersByTimeAsync(1000);
    await assertion;
  });

  it("invokes onStatus with each observed phase", async () => {
    let n = 0;
    const responses = [
      () => jsonResponse(200, { session_id: "sess_new", state: "creating", status_detail: "preparing_template" }),
      () => jsonResponse(200, { session_id: "sess_new", state: "creating", status_detail: "starting" }),
      () => jsonResponse(200, { session_id: "sess_new", state: "live" }),
    ];
    installFetch(() => responses[Math.min(n++, responses.length - 1)]());

    const seen: string[] = [];
    const createPromise = Session.create(makeClient(), { onStatus: (d) => seen.push(d) });
    await vi.advanceTimersByTimeAsync(1000);
    await createPromise;

    expect(seen).toEqual(["preparing_template", "starting"]);
  });

  it("wait:false returns immediately without polling", async () => {
    const { calls } = installFetch(() =>
      jsonResponse(200, { session_id: "sess_new", state: "creating", status_detail: "preparing_template" }),
    );
    const session = await Session.create(makeClient(), { wait: false });
    expect(session.sessionId).toBe("sess_new");
    expect(calls).toHaveLength(1); // no GET polling with wait:false
  });
});

describe("Client.sessions", () => {
  it("get() fetches a session by id without attaching", async () => {
    const { calls } = installFetch(() => jsonResponse(200, { session_id: "sess_1", state: "paused" }));
    const info = await makeClient().sessions.get("sess_1");
    expect(calls[0].path).toBe("https://api.inis.run/v1/sessions/sess_1");
    expect(info.state).toBe("paused");
  });

  it("pause()/resume()/fork()/archiveRetry() operate by session id", async () => {
    const { calls } = installFetch((call) => {
      if (call.path.endsWith("/pause")) return jsonResponse(200, { session_id: "sess_1", state: "paused" });
      if (call.path.endsWith("/resume")) return jsonResponse(200, { session_id: "sess_1", state: "live" });
      if (call.path.endsWith("/fork")) return jsonResponse(200, { parent_session_id: "sess_1", children: ["sess_2"] });
      if (call.path.endsWith("/archive/retry")) return jsonResponse(200, { status: "pending" });
      throw new Error("unexpected");
    });
    const sessions = makeClient().sessions;
    expect((await sessions.pause("sess_1")).state).toBe("paused");
    expect((await sessions.resume("sess_1")).state).toBe("live");
    expect((await sessions.fork("sess_1", 1)).children).toEqual(["sess_2"]);
    expect((await sessions.archiveRetry("sess_1")).status).toBe("pending");
    expect(calls).toHaveLength(4);
  });

  it("batchExec() wraps a string command in bash -lc and maps per-session results", async () => {
    const { calls } = installFetch(() =>
      jsonResponse(200, {
        results: [
          { session_id: "sess_1", stdout: "ok", exit_code: 0 },
          { session_id: "sess_2", error: "timed out" },
        ],
      }),
    );
    const results = await makeClient().sessions.batchExec({
      sessionIds: ["sess_1", "sess_2"],
      command: "echo hi",
    });
    expect(calls[0].path).toBe("https://api.inis.run/v1/sessions/batch/exec");
    expect(calls[0].body).toMatchObject({ command: ["bash", "-lc", "echo hi"] });
    expect(results[0]).toMatchObject({ sessionId: "sess_1", stdout: "ok" });
    expect(results[1]).toMatchObject({ sessionId: "sess_2", error: "timed out" });
  });

  it("list() forwards state/limit/cursor as query params", async () => {
    const { calls } = installFetch(() =>
      jsonResponse(200, { sessions: [{ session_id: "sess_1", state: "live" }], next_cursor: "cur_2" }),
    );
    const { sessions, nextCursor } = await makeClient().sessions.list({
      state: "live",
      limit: 10,
      cursor: "cur_1",
    });
    const url = new URL(calls[0].url);
    expect(url.searchParams.get("state")).toBe("live");
    expect(url.searchParams.get("limit")).toBe("10");
    expect(url.searchParams.get("cursor")).toBe("cur_1");
    expect(sessions).toHaveLength(1);
    expect(nextCursor).toBe("cur_2");
  });

  it("list() forwards externalId as the external_id query param", async () => {
    const { calls } = installFetch(() =>
      jsonResponse(200, { sessions: [{ session_id: "sess_1", state: "live", external_id: "workflow-42" }] }),
    );
    const { sessions } = await makeClient().sessions.list({ externalId: "workflow-42" });
    const url = new URL(calls[0].url);
    expect(url.searchParams.get("external_id")).toBe("workflow-42");
    expect(sessions[0].externalId).toBe("workflow-42");
  });

  it("readFile/listFiles/writeFile by session id delegate through Session.attach", async () => {
    const { calls } = installFetch((call) => {
      if (call.method === "GET") return jsonResponse(200, { content: "data", entries: ["a"] });
      return new Response(null, { status: 204 });
    });
    const sessions = makeClient().sessions;
    expect(await sessions.readFile("sess_1", "/a")).toBe("data");
    expect(await sessions.listFiles("sess_1", "/")).toEqual(["a"]);
    await sessions.writeFile("sess_1", "/a", "content");
    expect(calls[2].method).toBe("PUT");
  });
});

describe("Client.checkpoints", () => {
  it("get()/delete()/createSession() operate on /v1/checkpoints", async () => {
    const { calls } = installFetch((call) => {
      if (call.method === "GET") return jsonResponse(200, { checkpoint_id: "cp_1", size_bytes: 1 });
      if (call.method === "DELETE") return new Response(null, { status: 204 });
      return jsonResponse(200, { session_id: "sess_branch" });
    });
    const checkpoints = makeClient().checkpoints;

    const cp = await checkpoints.get("cp_1");
    expect(cp.checkpointId).toBe("cp_1");

    await checkpoints.delete("cp_1");
    expect(calls[1].path).toBe("https://api.inis.run/v1/checkpoints/cp_1");

    const session = await checkpoints.createSession("cp_1", { name: "branch-a" });
    expect(calls[2].path).toBe("https://api.inis.run/v1/checkpoints/cp_1/sessions");
    expect(session.sessionId).toBe("sess_branch");
  });
});

describe("Client.templates and Client.artifacts", () => {
  it("templates.list() and templates.delete()", async () => {
    const { calls } = installFetch((call) => {
      if (call.method === "GET") return jsonResponse(200, { templates: [{ name: "python-3.12", kind: "official" }] });
      return new Response(null, { status: 204 });
    });
    const templates = await makeClient().templates.list();
    expect(templates[0]).toMatchObject({ name: "python-3.12", kind: "official" });
    await makeClient().templates.delete("my-template");
    expect(calls[1].path).toBe("https://api.inis.run/v1/templates/my-template");
  });

  it("artifacts.get()/extend()/delete()", async () => {
    const { calls } = installFetch((call) => {
      if (call.method === "GET") return jsonResponse(200, { id: "art_1", status: "ready", files: [] });
      if (call.method === "DELETE") return new Response(null, { status: 204 });
      return jsonResponse(200, { id: "art_1", status: "ready" });
    });
    const artifacts = makeClient().artifacts;
    expect((await artifacts.get("art_1")).status).toBe("ready");
    await artifacts.extend("art_1", 7);
    expect(calls[1].body).toEqual({ ttl_days: 7 });
    await artifacts.delete("art_1");
    expect(calls[2].method).toBe("DELETE");
  });
});

describe("Client.registries", () => {
  it("registries.add() sends the fields and never leaks the secret in the return value", async () => {
    const { calls } = installFetch(() =>
      jsonResponse(201, { id: "cred_1", name: "my-ghcr", registry_host: "ghcr.io", username: "user", secret_last4: "7890" }),
    );
    const cred = await makeClient().registries.add("my-ghcr", {
      registryHost: "ghcr.io",
      username: "user",
      secret: "ghp_super-secret-pat",
    });
    expect(calls[0].method).toBe("POST");
    expect(calls[0].path).toBe("https://api.inis.run/v1/registry-credentials");
    expect(calls[0].body).toEqual({ name: "my-ghcr", registry_host: "ghcr.io", username: "user", secret: "ghp_super-secret-pat" });
    expect(cred).toMatchObject({ name: "my-ghcr", registryHost: "ghcr.io", username: "user", secretLast4: "7890" });
    expect((cred as Record<string, unknown>).secret).toBeUndefined();
  });

  it("registries.list() and registries.delete()", async () => {
    const { calls } = installFetch((call) => {
      if (call.method === "GET") {
        return jsonResponse(200, { credentials: [{ id: "cred_1", name: "my-ghcr", registry_host: "ghcr.io", username: "user" }] });
      }
      return new Response(null, { status: 204 });
    });
    const creds = await makeClient().registries.list();
    expect(creds[0]).toMatchObject({ name: "my-ghcr", registryHost: "ghcr.io" });
    await makeClient().registries.delete("my-ghcr");
    expect(calls[1].path).toBe("https://api.inis.run/v1/registry-credentials/my-ghcr");
    expect(calls[1].method).toBe("DELETE");
  });
});

describe("Client.connectors", () => {
  it("connectors.add() sends the fields and never leaks the secret in the return value", async () => {
    const { calls } = installFetch(() =>
      jsonResponse(201, { id: "conn_1", name: "stripe", target_base_url: "https://api.stripe.com", auth_shape: "bearer", secret_last4: "7890" }),
    );
    const conn = await makeClient().connectors.add("stripe", {
      targetBaseUrl: "https://api.stripe.com",
      secret: "sk_live_super_secret",
    });
    expect(calls[0].method).toBe("POST");
    expect(calls[0].path).toBe("https://api.inis.run/v1/connectors");
    expect(calls[0].body).toEqual({ name: "stripe", target_base_url: "https://api.stripe.com", auth_shape: "bearer", secret: "sk_live_super_secret" });
    expect(conn).toMatchObject({ name: "stripe", targetBaseUrl: "https://api.stripe.com", authShape: "bearer", secretLast4: "7890" });
    expect((conn as Record<string, unknown>).secret).toBeUndefined();
  });

  it("connectors.add() with header shape sends header_name", async () => {
    const { calls } = installFetch(() =>
      jsonResponse(201, { id: "conn_2", name: "acme", target_base_url: "https://api.acme.example", auth_shape: "header", header_name: "X-Api-Key" }),
    );
    await makeClient().connectors.add("acme", {
      targetBaseUrl: "https://api.acme.example",
      authShape: "header",
      headerName: "X-Api-Key",
      secret: "acme-secret",
    });
    expect(calls[0].body).toMatchObject({ auth_shape: "header", header_name: "X-Api-Key" });
  });

  it("connectors.list() and connectors.delete()", async () => {
    const { calls } = installFetch((call) => {
      if (call.method === "GET") {
        return jsonResponse(200, { connectors: [{ id: "conn_1", name: "stripe", target_base_url: "https://api.stripe.com", auth_shape: "bearer" }] });
      }
      return new Response(null, { status: 204 });
    });
    const conns = await makeClient().connectors.list();
    expect(conns[0]).toMatchObject({ name: "stripe", targetBaseUrl: "https://api.stripe.com" });
    await makeClient().connectors.delete("stripe");
    expect(calls[1].path).toBe("https://api.inis.run/v1/connectors/stripe");
    expect(calls[1].method).toBe("DELETE");
  });
});

describe("Client.domains", () => {
  it("domains.add() sends the domain and maps verify instructions", async () => {
    const { calls } = installFetch(() =>
      jsonResponse(201, {
        id: "dom_1",
        domain: "app.customer.com",
        verified: false,
        verify_txt_name: "_inis-verify.app.customer.com",
        verify_txt_value: "tok_abc123",
      }),
    );
    const d = await makeClient().domains.add("app.customer.com");
    expect(calls[0].method).toBe("POST");
    expect(calls[0].path).toBe("https://api.inis.run/v1/org/domains");
    expect(calls[0].body).toEqual({ domain: "app.customer.com" });
    expect(d).toMatchObject({
      id: "dom_1",
      verified: false,
      verifyTxtName: "_inis-verify.app.customer.com",
      verifyTxtValue: "tok_abc123",
    });
  });

  it("domains.list() and domains.delete()", async () => {
    const { calls } = installFetch((call) => {
      if (call.method === "GET") {
        return jsonResponse(200, {
          domains: [{ id: "dom_1", domain: "app.customer.com", verified: true, session_id: "ses_1", port: 8000 }],
        });
      }
      return new Response(null, { status: 204 });
    });
    const domains = await makeClient().domains.list();
    expect(domains[0]).toMatchObject({ domain: "app.customer.com", verified: true, sessionId: "ses_1", port: 8000 });
    await makeClient().domains.delete("dom_1");
    expect(calls[1].path).toBe("https://api.inis.run/v1/org/domains/dom_1");
    expect(calls[1].method).toBe("DELETE");
  });

  it("domains.verify() marks verified, throws InisError on failure", async () => {
    const { calls } = installFetch(() => jsonResponse(200, { id: "dom_1", domain: "app.customer.com", verified: true }));
    const d = await makeClient().domains.verify("dom_1");
    expect(calls[0].method).toBe("POST");
    expect(calls[0].path).toBe("https://api.inis.run/v1/org/domains/dom_1/verify");
    expect(d.verified).toBe(true);

    installFetch(() => errorResponse(409, "verification failed"));
    await expect(makeClient().domains.verify("dom_1")).rejects.toThrow(InisError);
  });

  it("domains.route() sends sessionId/port, domains.unroute() clears them", async () => {
    const { calls } = installFetch((call) => {
      if (call.method === "PUT") {
        return jsonResponse(200, { id: "dom_1", domain: "app.customer.com", verified: true, session_id: "ses_1", port: 8000 });
      }
      return jsonResponse(200, { id: "dom_1", domain: "app.customer.com", verified: true });
    });
    const routed = await makeClient().domains.route("dom_1", "ses_1", 8000);
    expect(calls[0].method).toBe("PUT");
    expect(calls[0].path).toBe("https://api.inis.run/v1/org/domains/dom_1/route");
    expect(calls[0].body).toEqual({ session_id: "ses_1", port: 8000 });
    expect(routed).toMatchObject({ sessionId: "ses_1", port: 8000 });

    const unrouted = await makeClient().domains.unroute("dom_1");
    expect(calls[1].method).toBe("DELETE");
    expect(calls[1].path).toBe("https://api.inis.run/v1/org/domains/dom_1/route");
    expect(unrouted.sessionId).toBeUndefined();
  });

  it("domains.route() before verification throws InisError", async () => {
    installFetch(() => errorResponse(409, "domain must be verified"));
    await expect(makeClient().domains.route("dom_1", "ses_1", 8000)).rejects.toThrow(InisError);
  });
});

// Standalone persistent-volume CRUD. Lazy creation is retired -- a
// volume must be created explicitly before its id can be attached
// anywhere, and an unknown/cross-tenant id (get/delete) or an over-cap
// size (create) must surface as a clean InisError, not a raw HTTP failure.
describe("Client.volumes", () => {
  it("volumes.create() with no size omits size_gb", async () => {
    const { calls } = installFetch(() =>
      jsonResponse(201, { id: "vol_1", size_gb: 1, created_at: "2026-08-07T00:00:00Z", attached: false }),
    );
    const v = await makeClient().volumes.create();
    expect(calls[0].method).toBe("POST");
    expect(calls[0].path).toBe("https://api.inis.run/v1/volumes");
    expect(calls[0].body).toEqual({});
    expect(v).toMatchObject({ id: "vol_1", sizeGb: 1, attached: false });
    expect(v.sessionId).toBeUndefined();
  });

  it("volumes.create() with an explicit size sends size_gb", async () => {
    const { calls } = installFetch(() =>
      jsonResponse(201, { id: "vol_2", size_gb: 5, created_at: "2026-08-07T00:00:00Z", attached: false }),
    );
    const v = await makeClient().volumes.create({ sizeGb: 5 });
    expect(calls[0].body).toEqual({ size_gb: 5 });
    expect(v.sizeGb).toBe(5);
  });

  it("volumes.create() over the cap throws InisError", async () => {
    installFetch(() => errorResponse(400, "size_gb exceeds maximum (10 GB)", { code: "bad_request" }));
    await expect(makeClient().volumes.create({ sizeGb: 999 })).rejects.toThrow(InisError);
    installFetch(() => errorResponse(400, "size_gb exceeds maximum (10 GB)", { code: "bad_request" }));
    try {
      await makeClient().volumes.create({ sizeGb: 999 });
      throw new Error("expected volumes.create() to throw");
    } catch (err) {
      expect(err).toBeInstanceOf(InisError);
      expect((err as InisError).code).toBe("bad_request");
      expect((err as InisError).status).toBe(400);
    }
  });

  it("volumes.list() returns an empty array when there are none", async () => {
    installFetch(() => jsonResponse(200, { volumes: [] }));
    expect(await makeClient().volumes.list()).toEqual([]);
  });

  it("volumes.list() maps a populated list, including the attached session id", async () => {
    const { calls } = installFetch(() =>
      jsonResponse(200, {
        volumes: [
          { id: "vol_1", size_gb: 1, created_at: "2026-08-01T00:00:00Z", attached: false },
          { id: "vol_2", size_gb: 5, created_at: "2026-08-02T00:00:00Z", attached: true, session_id: "ses_xyz" },
        ],
      }),
    );
    const volumes = await makeClient().volumes.list();
    expect(calls[0].method).toBe("GET");
    expect(calls[0].path).toBe("https://api.inis.run/v1/volumes");
    expect(volumes.map((v) => v.id)).toEqual(["vol_1", "vol_2"]);
    expect(volumes[1]).toMatchObject({ attached: true, sessionId: "ses_xyz" });
  });

  it("volumes.get() returns the volume", async () => {
    const { calls } = installFetch(() =>
      jsonResponse(200, { id: "vol_1", size_gb: 2, created_at: "2026-08-01T00:00:00Z", attached: false }),
    );
    const v = await makeClient().volumes.get("vol_1");
    expect(calls[0].path).toBe("https://api.inis.run/v1/volumes/vol_1");
    expect(v).toMatchObject({ id: "vol_1", sizeGb: 2 });
  });

  // A volume that has never completed a push must map to
  // lastSavedAt=undefined -- never a zeroed/synthesized timestamp. That
  // absence is the one signal distinguishing "no durable copy yet" from
  // "saved a long time ago".
  it("volumes.get() of a never-saved volume has no lastSavedAt", async () => {
    installFetch(() =>
      jsonResponse(200, { id: "vol_1", size_gb: 2, created_at: "2026-08-01T00:00:00Z", attached: false }),
    );
    const v = await makeClient().volumes.get("vol_1");
    expect(v.lastSavedAt).toBeUndefined();
  });

  it("volumes.get() of a saved volume returns lastSavedAt", async () => {
    installFetch(() =>
      jsonResponse(200, {
        id: "vol_1",
        size_gb: 2,
        created_at: "2026-08-01T00:00:00Z",
        attached: false,
        last_saved_at: "2026-08-07T10:00:00Z",
      }),
    );
    const v = await makeClient().volumes.get("vol_1");
    expect(v.lastSavedAt).toBe("2026-08-07T10:00:00Z");
  });

  it("volumes.get() of an unknown id throws a clean InisError, not a raw HTTP failure", async () => {
    installFetch(() => errorResponse(404, "volume not found", { code: "not_found" }));
    try {
      await makeClient().volumes.get("vol_nonexistent");
      throw new Error("expected volumes.get() to throw");
    } catch (err) {
      expect(err).toBeInstanceOf(InisError);
      expect((err as InisError).code).toBe("not_found");
      expect((err as InisError).status).toBe(404);
    }
  });

  it("volumes.delete() of an unattached volume succeeds", async () => {
    const { calls } = installFetch(() => new Response(null, { status: 204 }));
    await makeClient().volumes.delete("vol_1");
    expect(calls[0].method).toBe("DELETE");
    expect(calls[0].path).toBe("https://api.inis.run/v1/volumes/vol_1");
  });

  it("volumes.delete() of an unknown id throws a clean not-found InisError", async () => {
    installFetch(() => errorResponse(404, "volume not found", { code: "not_found" }));
    try {
      await makeClient().volumes.delete("vol_nonexistent");
      throw new Error("expected volumes.delete() to throw");
    } catch (err) {
      expect(err).toBeInstanceOf(InisError);
      expect((err as InisError).code).toBe("not_found");
    }
  });

  it("volumes.delete() while attached throws a distinguishable conflict InisError", async () => {
    installFetch(() => errorResponse(409, "volume is attached to a live or paused session", { code: "conflict" }));
    try {
      await makeClient().volumes.delete("vol_attached");
      throw new Error("expected volumes.delete() to throw");
    } catch (err) {
      expect(err).toBeInstanceOf(InisError);
      expect((err as InisError).code).toBe("conflict");
      expect((err as InisError).status).toBe(409);
    }
  });

  it("volumes.resize() grows and returns the new size", async () => {
    const { calls } = installFetch(() =>
      jsonResponse(200, { id: "vol_1", size_gb: 5, created_at: "2026-08-01T00:00:00Z", attached: false }),
    );
    const v = await makeClient().volumes.resize("vol_1", 5);
    expect(calls[0].method).toBe("PATCH");
    expect(calls[0].path).toBe("https://api.inis.run/v1/volumes/vol_1");
    expect(calls[0].body).toEqual({ size_gb: 5 });
    expect(v.sizeGb).toBe(5);
  });

  // Grow-only: a same-size or smaller request is refused outright, not
  // silently accepted as a no-op or attempted as a shrink.
  it("volumes.resize() shrink throws a bad_request InisError", async () => {
    installFetch(() =>
      errorResponse(
        400,
        "size_gb must be greater than the current size (5 GB) — volumes only grow, they never shrink",
        { code: "bad_request" },
      ),
    );
    try {
      await makeClient().volumes.resize("vol_1", 3);
      throw new Error("expected volumes.resize() to throw");
    } catch (err) {
      expect(err).toBeInstanceOf(InisError);
      expect((err as InisError).code).toBe("bad_request");
      expect((err as InisError).status).toBe(400);
    }
  });

  it("volumes.resize() while attached throws a distinguishable conflict InisError", async () => {
    installFetch(() =>
      errorResponse(409, "volume is attached to session ses_abc; detach it before resizing", { code: "conflict" }),
    );
    try {
      await makeClient().volumes.resize("vol_attached", 5);
      throw new Error("expected volumes.resize() to throw");
    } catch (err) {
      expect(err).toBeInstanceOf(InisError);
      expect((err as InisError).code).toBe("conflict");
      expect((err as InisError).status).toBe(409);
    }
  });

  it("volumes.resize() over the account quota throws a distinguishable conflict InisError", async () => {
    installFetch(() =>
      errorResponse(409, "volume storage quota exceeded: 8 GB used, 4 GB more requested, cap is 10 GB", {
        code: "conflict",
      }),
    );
    try {
      await makeClient().volumes.resize("vol_1", 10);
      throw new Error("expected volumes.resize() to throw");
    } catch (err) {
      expect(err).toBeInstanceOf(InisError);
      expect((err as InisError).code).toBe("conflict");
    }
  });
});

describe("error handling", () => {
  it("throws InisError with status and message from the error field", async () => {
    installFetch(() => errorResponse(404, "session not found"));
    await expect(Session.attach(makeClient(), "sess_1").get()).rejects.toThrow(InisError);
    await expect(Session.attach(makeClient(), "sess_1").get()).rejects.toThrow(/404.*session not found/);
  });

  it("falls back to raw text when the error body isn't JSON", async () => {
    installFetch(() => new Response("bad gateway", { status: 502 }));
    await expect(Session.attach(makeClient(), "sess_1").resume()).rejects.toThrow(/bad gateway/);
  });

  it("resolveToken throws InisError when no token is available", () => {
    const original = process.env.INIS_API_KEY;
    delete process.env.INIS_API_KEY;
    try {
      expect(() => new Client({ baseUrl: "https://api.inis.run" })).toThrow(InisError);
    } finally {
      if (original !== undefined) process.env.INIS_API_KEY = original;
    }
  });

  // InisError carries the server's full error contract, not just a message
  // string.
  it("carries code and status from the response body", async () => {
    installFetch(() => errorResponse(409, "session is paused", { code: "session_not_live" }));
    try {
      await Session.attach(makeClient(), "sess_1").get();
      expect.unreachable("expected InisError");
    } catch (err) {
      expect(err).toBeInstanceOf(InisError);
      const ie = err as InisError;
      expect(ie.code).toBe("session_not_live");
      expect(ie.status).toBe(409);
      expect(ie.message).toMatch(/session is paused/);
      expect(ie.response).toEqual({ error: "session is paused", code: "session_not_live" });
    }
  });

  it("falls back to the generic \"error\" code when the response carries none", async () => {
    installFetch(() => errorResponse(500, "internal error"));
    try {
      await Session.attach(makeClient(), "sess_1").get();
      expect.unreachable("expected InisError");
    } catch (err) {
      expect((err as InisError).code).toBe("error");
    }
  });

  // A code this SDK version has never seen must still reach InisError.code
  // (and .response) with its exact raw value —
  // never collapsed into a message-only error just because RETRYABLE_CODES
  // doesn't recognise it.
  it("preserves an unrecognised future code raw, and treats it as non-retryable by default", async () => {
    installFetch(() =>
      errorResponse(422, "the future happened", { code: "some_brand_new_code_from_2027" }),
    );
    try {
      await Session.attach(makeClient(), "sess_1").get();
      expect.unreachable("expected InisError");
    } catch (err) {
      const ie = err as InisError;
      expect(ie.code).toBe("some_brand_new_code_from_2027");
      expect(ie.response?.code).toBe("some_brand_new_code_from_2027");
      expect(ie.retryable).toBe(false);
    }
  });

  it.each([
    "session_not_live",
    "session_unavailable",
    "session_node_unavailable",
    "rate_limited",
    "unavailable",
    "template_version_unavailable",
    "concurrency_limit_exceeded",
    "rate_limit_exceeded",
    "service_unavailable",
  ])("marks known-retryable code %s as retryable", async (code) => {
    installFetch(() => errorResponse(503, "try again", { code }));
    try {
      await Session.attach(makeClient(), "sess_1").get();
      expect.unreachable("expected InisError");
    } catch (err) {
      expect((err as InisError).retryable).toBe(true);
    }
  });

  it.each([
    "validation",
    "unauthenticated",
    "forbidden",
    "not_found",
    "conflict",
    "session_ended",
    "session_node_lost",
    "archive_unavailable",
    "size_unavailable",
    "payload_too_large",
    "internal",
    "payment_required",
  ])("marks known-non-retryable code %s as not retryable", async (code) => {
    installFetch(() => errorResponse(400, "nope", { code }));
    try {
      await Session.attach(makeClient(), "sess_1").get();
      expect.unreachable("expected InisError");
    } catch (err) {
      expect((err as InisError).retryable).toBe(false);
    }
  });

  // Retry-After must reach the SDK caller.
  it("Retry-After header reaches the caller as retryAfter (seconds) and marks it retryable", async () => {
    installFetch(() => errorResponse(503, "please retry", { headers: { "Retry-After": "5" } }));
    try {
      await Session.attach(makeClient(), "sess_1").get();
      expect.unreachable("expected InisError");
    } catch (err) {
      const ie = err as InisError;
      expect(ie.retryAfter).toBe(5);
      expect(ie.retryable).toBe(true);
    }
  });

  // RFC 9110 §10.2.3 allows Retry-After to be an HTTP-date instead of
  // integer delta-seconds; parseRetryAfter accepts both, but until this
  // test only the delta-seconds form had coverage.
  it("Retry-After as an HTTP-date reaches the caller as retryAfter (seconds)", async () => {
    const future = new Date(Date.now() + 30_000).toUTCString();
    installFetch(() => errorResponse(503, "please retry", { headers: { "Retry-After": future } }));
    try {
      await Session.attach(makeClient(), "sess_1").get();
      expect.unreachable("expected InisError");
    } catch (err) {
      const ie = err as InisError;
      expect(ie.retryAfter).toBeGreaterThan(0);
      expect(ie.retryAfter).toBeLessThanOrEqual(31);
      expect(ie.retryable).toBe(true);
    }
  });

  it("X-Inis-Request-Id header reaches the caller as requestId", async () => {
    installFetch(() =>
      errorResponse(500, "internal error", { headers: { "X-Inis-Request-Id": "req_abc123" } }),
    );
    try {
      await Session.attach(makeClient(), "sess_1").get();
      expect.unreachable("expected InisError");
    } catch (err) {
      expect((err as InisError).requestId).toBe("req_abc123");
    }
  });

  it("a non-JSON error body still produces a structured InisError with the generic code", async () => {
    installFetch(() => new Response("<html>Bad Gateway</html>", { status: 502 }));
    try {
      await Session.attach(makeClient(), "sess_1").get();
      expect.unreachable("expected InisError");
    } catch (err) {
      const ie = err as InisError;
      expect(ie.code).toBe("error");
      expect(ie.status).toBe(502);
      expect(ie.response).toBeUndefined();
    }
  });
});

describe("named background processes", () => {
  it("startProcess() sends name/command/keep_alive and maps the response", async () => {
    const { calls } = installFetch(() =>
      jsonResponse(200, {
        name: "server",
        state: "running",
        pid: 4242,
        command: ["python3", "-m", "http.server", "8000"],
        keep_alive: true,
      }),
    );
    const session = Session.attach(makeClient(), "sess_123");

    const info = await session.startProcess("server", ["python3", "-m", "http.server", "8000"], {
      keepAlive: true,
    });

    expect(calls[0].method).toBe("POST");
    expect(calls[0].path).toBe("https://api.inis.run/v1/sessions/sess_123/processes");
    expect(calls[0].body).toEqual({
      name: "server",
      command: ["python3", "-m", "http.server", "8000"],
      keep_alive: true,
    });
    expect(info.name).toBe("server");
    expect(info.state).toBe("running");
    expect(info.pid).toBe(4242);
    expect(info.keepAlive).toBe(true);
  });

  it("startProcess() wraps a string command in bash -lc", async () => {
    const { calls } = installFetch(() => jsonResponse(200, { name: "greet", state: "running" }));
    const session = Session.attach(makeClient(), "sess_123");

    await session.startProcess("greet", "echo hi");

    expect(calls[0].body).toEqual({ name: "greet", command: ["bash", "-lc", "echo hi"], keep_alive: false });
  });

  it("listProcesses() maps the process list", async () => {
    installFetch(() =>
      jsonResponse(200, {
        processes: [
          { name: "server", state: "running" },
          { name: "done", state: "exited", exit_code: 0 },
        ],
      }),
    );
    const session = Session.attach(makeClient(), "sess_123");

    const procs = await session.listProcesses();

    expect(procs.map((p) => p.name)).toEqual(["server", "done"]);
    expect(procs[1].state).toBe("exited");
    expect(procs[1].exitCode).toBe(0);
  });

  it("getProcess() and killProcess() hit the expected path/method", async () => {
    const { calls } = installFetch((call) =>
      jsonResponse(200, { name: "server", state: call.method === "DELETE" ? "exited" : "running" }),
    );
    const session = Session.attach(makeClient(), "sess_123");

    const info = await session.getProcess("server");
    expect(calls[0].method).toBe("GET");
    expect(calls[0].path).toBe("https://api.inis.run/v1/sessions/sess_123/processes/server");
    expect(info.state).toBe("running");

    const killed = await session.killProcess("server");
    expect(calls[1].method).toBe("DELETE");
    expect(calls[1].path).toBe("https://api.inis.run/v1/sessions/sess_123/processes/server");
    expect(killed.state).toBe("exited");
  });

  it("getProcessLogs() returns buffered stdout/stderr", async () => {
    const { calls } = installFetch(() =>
      jsonResponse(200, { name: "server", stdout: "hello\n", stderr: "" }),
    );
    const session = Session.attach(makeClient(), "sess_123");

    const logs = await session.getProcessLogs("server");

    expect(calls[0].method).toBe("GET");
    expect(calls[0].path).toBe("https://api.inis.run/v1/sessions/sess_123/processes/server/logs");
    expect(logs.stdout).toBe("hello\n");
  });

  it("streamProcessLogs() decodes base64 SSE frames and stops at eof", async () => {
    const b64 = (s: string) => Buffer.from(s, "utf-8").toString("base64");
    const sse =
      `event: stdout\ndata: ${b64("line1\n")}\n\n` +
      `event: stderr\ndata: ${b64("err1\n")}\n\n` +
      `event: eof\ndata: \n\n`;
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response(sse, { status: 200, headers: { "content-type": "text/event-stream" } })),
    );
    const session = Session.attach(makeClient(), "sess_123");

    const events = [];
    for await (const ev of session.streamProcessLogs("server")) {
      events.push(ev);
    }

    expect(events).toEqual([
      { event: "stdout", data: "line1\n" },
      { event: "stderr", data: "err1\n" },
      { event: "eof", data: "" },
    ]);
  });

  // Same multi-byte UTF-8 reassembly fix applied to this sibling SSE
  // consumer — see the identical execStream test above for the full
  // rationale.
  it("streamProcessLogs() reassembles a multi-byte character split across a chunk boundary", async () => {
    const raw = Buffer.from("hello \u{1F389} world", "utf-8");
    const splitAt = 6 + 2;
    const chunk1 = raw.subarray(0, splitAt).toString("base64");
    const chunk2 = raw.subarray(splitAt).toString("base64");
    const sse =
      `event: stdout\ndata: ${chunk1}\n\n` +
      `event: stdout\ndata: ${chunk2}\n\n` +
      `event: eof\ndata: \n\n`;
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response(sse, { status: 200, headers: { "content-type": "text/event-stream" } })),
    );
    const session = Session.attach(makeClient(), "sess_123");

    const events = [];
    for await (const ev of session.streamProcessLogs("server")) {
      events.push(ev);
    }

    const stdoutEvents = events.filter((e) => e.event === "stdout");
    const combined = stdoutEvents.map((e) => e.data).join("");
    expect(combined).toBe("hello \u{1F389} world");
    expect(combined).not.toContain("�");
  });

  it("streamProcessLogs() throws InisError on a non-OK response", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => errorResponse(404, 'process "x" not found')),
    );
    const session = Session.attach(makeClient(), "sess_123");

    await expect(async () => {
      for await (const _ev of session.streamProcessLogs("x")) {
        // drain
      }
    }).rejects.toThrow(/404/);
  });
});

describe("streaming exec", () => {
  it("execStream() posts to ?stream=true and decodes stdout/stderr/exit frames", async () => {
    const b64 = (s: string) => Buffer.from(s, "utf-8").toString("base64");
    const sse =
      `event: stdout\ndata: ${b64("out1\n")}\n\n` +
      `event: stderr\ndata: ${b64("err1\n")}\n\n` +
      `event: exit\ndata: {"exit_code":0,"timed_out":false,"duration_ms":42}\n\n`;
    let capturedUrl = "";
    let capturedInit: RequestInit | undefined;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: string | URL, init?: RequestInit) => {
        capturedUrl = String(input);
        capturedInit = init;
        return new Response(sse, { status: 200, headers: { "content-type": "text/event-stream" } });
      }),
    );
    const session = Session.attach(makeClient(), "sess_123");

    const events = [];
    for await (const ev of session.execStream(["echo", "hi"], { cwd: "/workspace" })) {
      events.push(ev);
    }

    expect(capturedUrl).toBe("https://api.inis.run/v1/sessions/sess_123/exec?stream=true");
    expect(capturedInit?.method).toBe("POST");
    expect(JSON.parse(String(capturedInit?.body))).toEqual({ command: ["echo", "hi"], cwd: "/workspace" });
    expect(events).toEqual([
      { stream: "stdout", data: "out1\n" },
      { stream: "stderr", data: "err1\n" },
      { stream: "exit", data: "", exitCode: 0, timedOut: false, durationMs: 42 },
    ]);
  });

  it("execStream() stops at an error frame", async () => {
    const sse = `event: error\ndata: guest agent unreachable\n\n`;
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response(sse, { status: 200, headers: { "content-type": "text/event-stream" } })),
    );
    const session = Session.attach(makeClient(), "sess_123");

    const events = [];
    for await (const ev of session.execStream("echo hi")) {
      events.push(ev);
    }

    expect(events).toEqual([{ stream: "error", data: "guest agent unreachable" }]);
  });

  it("execStream() throws InisError on a non-OK response", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => errorResponse(503, "session unavailable")),
    );
    const session = Session.attach(makeClient(), "sess_123");

    await expect(async () => {
      for await (const _ev of session.execStream("echo hi")) {
        // drain
      }
    }).rejects.toThrow(/503/);
  });

  // A multi-byte UTF-8 character split across two separate stdout SSE
  // frames (mirroring io.Copy's 32KB read boundary
  // landing mid-character on the guest side) must reassemble correctly, not
  // decode as two U+FFFD replacement characters. "🎉" is U+1F389, a 4-byte
  // UTF-8 sequence (F0 9F 8E 89); split it 2/2 across chunk boundaries.
  it("execStream() reassembles a multi-byte character split across a chunk boundary", async () => {
    const raw = Buffer.from("hello \u{1F389} world", "utf-8"); // "hello 🎉 world"
    const splitAt = 6 + 2; // "hello " (6 bytes) + first 2 bytes of the emoji
    const chunk1 = raw.subarray(0, splitAt).toString("base64");
    const chunk2 = raw.subarray(splitAt).toString("base64");
    const sse =
      `event: stdout\ndata: ${chunk1}\n\n` +
      `event: stdout\ndata: ${chunk2}\n\n` +
      `event: exit\ndata: {"exit_code":0,"timed_out":false,"duration_ms":1}\n\n`;
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response(sse, { status: 200, headers: { "content-type": "text/event-stream" } })),
    );
    const session = Session.attach(makeClient(), "sess_123");

    const events = [];
    for await (const ev of session.execStream("echo hi")) {
      events.push(ev);
    }

    const stdoutEvents = events.filter((e) => e.stream === "stdout");
    const combined = stdoutEvents.map((e) => e.data).join("");
    expect(combined).toBe("hello \u{1F389} world");
    expect(combined).not.toContain("�");
  });
});
