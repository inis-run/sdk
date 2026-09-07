// Mocked-fetch tests for Session.runCode / Session.restartContext:
// request construction (kernel install/version-check, write-then-rename
// request files, the wait-command's exec timeout buffer), envelope parsing,
// image-result materialization, and kernel-death auto-recovery.
//
// FakeGuest below is a deliberately tiny stand-in for the real guest-side
// kernel: it tracks a "running"/"exited" process and a handful of fake
// variables (assignment + "name + N" / bare-name lookup) rather than
// actually running Python. The real cell-execution/rich-output logic (the
// kernel script itself) is Python, shared by both SDKs, and is exhaustively
// unit-tested directly in sdk/python/tests/test_kernel_script.py; these
// tests exist to prove the TypeScript side of the protocol — not to
// re-verify Python semantics.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { Client, Session } from "./client.js";

interface RecordedCall {
  method: string;
  url: string;
  path: string;
  body: unknown;
}

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

function errorResponse(status: number, error: string): Response {
  return jsonResponse(status, { error });
}

class FakeGuest {
  processState: "running" | "exited" | null = null;
  /** path -> base64-encoded content — a single canonical storage form so
   * binary (image) and text (request/version) files round-trip correctly
   * regardless of which encoding a given write/read call used. */
  files = new Map<string, string>();
  vars = new Map<string, string>();
  readonly base = "https://api.inis.run/v1/sessions/sess_123";
  calls: RecordedCall[] = [];

  install(): void {
    const fetchMock = vi.fn(async (input: string | URL, init?: RequestInit) => {
      const url = String(input);
      const [path] = url.split("?");
      const body = init?.body ? JSON.parse(String(init.body)) : undefined;
      const call: RecordedCall = { method: init?.method ?? "GET", url, path, body };
      this.calls.push(call);
      return this.handle(call);
    });
    vi.stubGlobal("fetch", fetchMock);
  }

  private handle(call: RecordedCall): Response {
    if (!call.path.startsWith(this.base)) throw new Error(`unexpected path ${call.path}`);
    const suffix = call.path.slice(this.base.length);
    if (call.method === "POST" && suffix === "/exec") return this.execHandler(call);
    if (call.method === "PUT" && suffix === "/files") return this.writeHandler(call);
    if (call.method === "GET" && suffix === "/files") return this.readHandler(call);
    if (call.method === "POST" && suffix === "/processes") {
      this.processState = "running";
      this.vars.clear();
      const body = call.body as { name: string };
      return jsonResponse(200, { name: body.name, state: "running", pid: 111 });
    }
    if (call.method === "GET" && suffix === "/processes/inis-kernel") {
      if (this.processState === null) return errorResponse(404, "process not found");
      return jsonResponse(200, { name: "inis-kernel", state: this.processState });
    }
    if (call.method === "DELETE" && suffix === "/processes/inis-kernel") {
      this.processState = "exited";
      return jsonResponse(200, { name: "inis-kernel", state: "exited", exit_code: 143 });
    }
    throw new Error(`unhandled request: ${call.method} ${suffix}`);
  }

  private writeHandler(call: RecordedCall): Response {
    const body = call.body as { path: string; content: string; encoding?: string };
    const base64 =
      body.encoding === "base64" ? body.content : Buffer.from(body.content, "utf-8").toString("base64");
    this.files.set(body.path, base64);
    return jsonResponse(200, {});
  }

  private readHandler(call: RecordedCall): Response {
    const url = new URL(call.url);
    const path = url.searchParams.get("path")!;
    const base64 = this.files.get(path);
    if (base64 === undefined) return errorResponse(404, "not found");
    if (url.searchParams.get("encoding") === "base64") {
      return jsonResponse(200, { content: base64 });
    }
    return jsonResponse(200, { content: Buffer.from(base64, "base64").toString("utf-8") });
  }

  private execHandler(call: RecordedCall): Response {
    const body = call.body as { command: string[] };
    const script = body.command[body.command.length - 1];
    if (script.startsWith("mkdir -p")) {
      return jsonResponse(200, { stdout: "", stderr: "", exit_code: 0, duration_ms: 1, timed_out: false });
    }
    const timeoutMarker = JSON.stringify({ __inis_timeout__: true });
    const m = script.match(/mv '([^']+)' '([^']+)'/);
    if (!m) throw new Error(`exec script wasn't mkdir or a runCode wait command: ${script}`);
    const tmpPath = m[1];
    const rawBase64 = this.files.get(tmpPath);
    this.files.delete(tmpPath);
    if (rawBase64 === undefined || this.processState !== "running") {
      return jsonResponse(200, { stdout: timeoutMarker, stderr: "", exit_code: 0, duration_ms: 1, timed_out: false });
    }
    const raw = Buffer.from(rawBase64, "base64").toString("utf-8");
    const req = JSON.parse(raw) as { id: string; code: string };
    const results = this.evaluate(req.code);
    const stdout = JSON.stringify({ id: req.id, results });
    return jsonResponse(200, { stdout, stderr: "", exit_code: 0, duration_ms: 5, timed_out: false });
  }

  /** Minimal fake "interpreter": assignment (`name = value`), `name + N`
   * arithmetic, bare-name lookup (NameError if unset), and a canned
   * exception trigger — just enough surface for these protocol tests. */
  private evaluate(code: string): Record<string, unknown>[] {
    const assign = code.match(/^(\w+) = (\d+)$/);
    if (assign) {
      this.vars.set(assign[1], assign[2]);
      return [];
    }
    if (code === "raise ValueError('boom')") {
      return [
        {
          type: "error",
          ename: "ValueError",
          evalue: "boom",
          traceback: "Traceback (most recent call last):\n...\nValueError: boom\n",
        },
      ];
    }
    const plus = code.match(/^(\w+) \+ (\d+)$/);
    if (plus) {
      const base = this.vars.get(plus[1]);
      if (base === undefined) {
        return [{ type: "error", ename: "NameError", evalue: `name '${plus[1]}' is not defined` }];
      }
      return [{ type: "text", stream: "result", text: String(Number(base) + Number(plus[2])) }];
    }
    const bare = code.match(/^(\w+)$/);
    if (bare) {
      const value = this.vars.get(bare[1]);
      if (value === undefined) {
        return [{ type: "error", ename: "NameError", evalue: `name '${bare[1]}' is not defined` }];
      }
      return [{ type: "text", stream: "result", text: value }];
    }
    if (code.includes("plt.plot")) {
      const path = "/workspace/.inis_kernel/artifacts/fake.png";
      const png = Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a, 1, 2, 3]);
      this.files.set(path, png.toString("base64"));
      return [{ type: "image", format: "png", path, size: png.length }];
    }
    throw new Error(`FakeGuest.evaluate: no canned result for code ${JSON.stringify(code)}`);
  }
}

function makeSession(): Session {
  const client = new Client({ token: "tok_abc", baseUrl: "https://api.inis.run" });
  return Session.attach(client, "sess_123");
}

beforeEach(() => {
  vi.unstubAllGlobals();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("runCode: context persistence", () => {
  it("keeps variables across calls", async () => {
    const guest = new FakeGuest();
    guest.install();
    const session = makeSession();

    const first = await session.runCode("x = 41");
    const second = await session.runCode("x + 1");

    expect(first).toEqual([]);
    expect(second).toEqual([{ type: "text", stream: "result", text: "42" }]);
  });

  it("installs and starts the kernel exactly once across calls", async () => {
    const guest = new FakeGuest();
    guest.install();
    const session = makeSession();

    await session.runCode("a = 1");
    await session.runCode("a + 1");

    const startCalls = guest.calls.filter((c) => c.path.endsWith("/processes"));
    expect(startCalls).toHaveLength(1);
  });
});

describe("runCode: error path", () => {
  it("returns a structured error and leaves the context usable", async () => {
    const guest = new FakeGuest();
    guest.install();
    const session = makeSession();

    await session.runCode("x = 41");
    const errResult = await session.runCode("raise ValueError('boom')");

    expect(errResult).toHaveLength(1);
    expect(errResult[0].type).toBe("error");
    expect(errResult[0].ename).toBe("ValueError");
    expect(errResult[0].evalue).toBe("boom");
    expect(errResult[0].traceback).toContain("ValueError: boom");

    const after = await session.runCode("x + 1");
    expect(after[0].text).toBe("42");
  });
});

describe("runCode: kernel death recovery", () => {
  it("auto-restarts with a fresh context after killProcess", async () => {
    const guest = new FakeGuest();
    guest.install();
    const session = makeSession();

    await session.runCode("x = 41");
    await session.killProcess("inis-kernel");
    expect(guest.processState).toBe("exited");

    const results = await session.runCode("x");
    expect(results[0].type).toBe("error");
    expect(results[0].ename).toBe("NameError");

    await session.runCode("x = 5");
    const again = await session.runCode("x");
    expect(again[0].text).toBe("5");
  });
});

describe("restartContext", () => {
  it("clears the interpreter globals", async () => {
    const guest = new FakeGuest();
    guest.install();
    const session = makeSession();

    await session.runCode("x = 41");
    await session.restartContext();
    const results = await session.runCode("x");

    expect(results[0].type).toBe("error");
    expect(results[0].ename).toBe("NameError");
  });
});

describe("runCode: image results", () => {
  it("materializes image bytes via readFile", async () => {
    const guest = new FakeGuest();
    guest.install();
    const session = makeSession();

    const results = await session.runCode("import matplotlib.pyplot as plt\nplt.plot([1,2,3])");

    expect(results).toHaveLength(1);
    expect(results[0].type).toBe("image");
    expect(results[0].format).toBe("png");
    expect(results[0].data).toBeInstanceOf(Uint8Array);
    expect(Array.from(results[0].data!.slice(0, 4))).toEqual([0x89, 0x50, 0x4e, 0x47]);
  });
});
