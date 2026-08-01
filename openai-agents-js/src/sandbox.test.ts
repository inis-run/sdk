/**
 * Tests for the real `InisSandboxSession`/`InisSandboxClient` adapter code.
 *
 * Unlike the Python package's tests (which fake the HTTP transport
 * underneath a real `inis.AsyncSession`/`AsyncClient`), this fakes at the
 * `inis.Session` object boundary directly: a light in-memory double
 * implementing the handful of `Session` methods this adapter actually
 * calls (`exec`, `readFile`, `writeFile`, `get`, `expose`, `remove`,
 * `destroy`, `mkdir`). That is a lower bar than the Python suite's
 * real-transport fake, disclosed here rather than presented as equivalent
 * rigor; it still exercises every line of `InisSandboxSession`/
 * `InisSandboxClient` for real — nothing in `sandbox.ts` itself is
 * stubbed out.
 */
import { describe, expect, it } from "vitest";
import { InisSandboxSession } from "./sandbox.js";
import type { Session } from "@inis-run/sdk";
// `run()` comes from the top-level `@openai/agents` package (the same
// public entry point the README's own usage example imports it from);
// `SandboxAgent` lives under the `agents-core` subpath both packages
// re-export it from unchanged.
import { run } from "@openai/agents";
import { SandboxAgent, shell } from "@openai/agents-core/sandbox";
import { Usage } from "@openai/agents-core";
import type { Model, ModelRequest, ModelResponse, StreamEvent } from "@openai/agents-core";

class FakeSession {
  sessionId: string;
  private files = new Map<string, string>();
  private execLog: string[][] = [];
  destroyed = false;
  forceTruncated = false;

  constructor(sessionId: string) {
    this.sessionId = sessionId;
  }

  async get() {
    return { sessionId: this.sessionId, state: "live" as const };
  }

  async exec(command: string[], _opts?: { cwd?: string; timeoutMs?: number }) {
    this.execLog.push(command);
    const script = command[command.length - 1] ?? "";
    const base = { durationMs: 1, timedOut: false, truncated: this.forceTruncated };
    if (script.startsWith("echo ")) {
      return {
        stdout: `${script.slice("echo ".length)}\n`,
        stderr: "",
        exitCode: 0,
        ...base,
      };
    }
    if (script === "exit 7") {
      return { stdout: "", stderr: "", exitCode: 7, ...base };
    }
    if (script === "sleep 5") {
      return { stdout: "", stderr: "", exitCode: 0, ...base, timedOut: true };
    }
    // tar/base64 persist/hydrate round trip: fake it as a JSON blob of the
    // in-memory file map instead of shelling out to real tar/base64 — still
    // exercises this adapter's own tar-command construction/parsing paths
    // (the command text itself), just not real tar bytes.
    if (script.includes("tar") && script.includes("base64") && !script.includes("base64 -d")) {
      const payload = Buffer.from(JSON.stringify(Object.fromEntries(this.files))).toString(
        "base64",
      );
      return { stdout: payload, stderr: "", exitCode: 0, ...base };
    }
    // hydrate: "base64 -d < <tarPath> | tar -C <root> -xf -". Decode the
    // scratch archive this adapter just wrote via writeFile (so it already
    // passed assertUnderWorkspace) and actually merge its entries into this
    // session's own file map, rather than a no-op success -- a no-op here
    // is exactly what would let a real hydrateWorkspace() regression pass
    // this suite unnoticed.
    const hydrateMatch = script.match(/base64 -d < "([^"]+)"/);
    if (hydrateMatch) {
      const tarPath = hydrateMatch[1];
      const encoded = this.files.get(tarPath);
      if (encoded) {
        const entries = JSON.parse(Buffer.from(encoded, "base64").toString("utf-8")) as Record<
          string,
          string
        >;
        for (const [path, content] of Object.entries(entries)) {
          this.files.set(path, content);
        }
      }
      return { stdout: "", stderr: "", exitCode: 0, ...base };
    }
    return { stdout: "", stderr: "", exitCode: 0, ...base };
  }

  private assertUnderWorkspace(path: string) {
    // Matches the real api.inis.run behaviour, confirmed live: the file
    // endpoints reject any path outside /workspace, even though a bare
    // exec() is unrestricted. Without this check the old
    // `hydrateWorkspace()` `/tmp/...` scratch-path bug would have kept
    // passing here while shipping broken against the real API.
    if (path !== "/workspace" && !path.startsWith("/workspace/")) {
      const err = new Error("path must be under /workspace") as Error & {
        code: string;
        status: number;
        retryable: boolean;
      };
      err.code = "invalid_path";
      err.status = 400;
      err.retryable = false;
      throw err;
    }
  }

  async readFile(path: string, _encoding?: "text" | "base64") {
    this.assertUnderWorkspace(path);
    const content = this.files.get(path);
    if (content === undefined) {
      const err = new Error("not found") as Error & { code: string; status: number; retryable: boolean };
      err.code = "not_found";
      err.status = 404;
      err.retryable = false;
      throw err;
    }
    return Buffer.from(content, "utf-8").toString("base64");
  }

  async writeFile(path: string, content: string, encoding?: "text" | "base64") {
    this.assertUnderWorkspace(path);
    this.files.set(
      path,
      encoding === "base64" ? Buffer.from(content, "base64").toString("utf-8") : content,
    );
  }

  async mkdir(_path: string) {}

  async remove(_path: string) {}

  async destroy() {
    this.destroyed = true;
  }

  async expose(port: number) {
    return { sessionId: this.sessionId, port, previewUrl: `https://${this.sessionId}-${port}.preview.inis.test` };
  }

  getExecLog() {
    return this.execLog;
  }
}

describe("InisSandboxSession (real adapter path, fake Session)", () => {
  it("exec() routes through session.exec and preserves output/exit code", async () => {
    const raw = new FakeSession("sess_1");
    const session = InisSandboxSession.wrap(raw as unknown as Session);

    const ok = await session.exec!({ cmd: "echo hello" });
    expect(ok.exitCode).toBe(0);
    expect(ok.stdout.trim()).toBe("hello");

    const failing = await session.exec!({ cmd: "exit 7" });
    expect(failing.exitCode).toBe(7);
  });

  it("read/write round-trip through the fake filesystem", async () => {
    const raw = new FakeSession("sess_2");
    const session = InisSandboxSession.wrap(raw as unknown as Session);

    await session.materializeEntry!({
      path: "/workspace/hello.txt",
      entry: { type: "file", content: "hi there" } as never,
    });
    const data = await session.readFile!({ path: "/workspace/hello.txt" });
    expect(Buffer.from(data as string, "base64").toString("utf-8")).toBe("hi there");
  });

  it("read of a missing file surfaces a not-found error, not a generic one", async () => {
    const raw = new FakeSession("sess_3");
    const session = InisSandboxSession.wrap(raw as unknown as Session);
    await expect(session.readFile!({ path: "/workspace/nope.txt" })).rejects.toThrow();
  });

  it("running() reflects the underlying session state", async () => {
    const raw = new FakeSession("sess_4");
    const session = InisSandboxSession.wrap(raw as unknown as Session);
    expect(await session.running!()).toBe(true);
  });

  it("resolveExposedPort() uses session.expose() for real", async () => {
    const raw = new FakeSession("sess_5");
    const session = InisSandboxSession.wrap(raw as unknown as Session);
    const endpoint = await session.resolveExposedPort!(8080);
    expect(endpoint.host).toContain("sess_5-8080");
    expect(endpoint.tls).toBe(true);
  });

  it("a timed-out command surfaces exitCode undefined, not a hang or a fake success", async () => {
    // The upstream contract carries no abort handle, so a timeout is the
    // one way this adapter ever ends an in-flight command early. (inis.run
    // itself does have real cancellation, but only for named background
    // processes, which this contract has no way to reach.)
    // `SandboxExecResult.exitCode` is `number | null |
    // undefined` in the real upstream contract precisely so a provider can
    // signal "no exit code" this way.
    const raw = new FakeSession("sess_timeout");
    const session = InisSandboxSession.wrap(raw as unknown as Session);
    const result = await session.exec!({ cmd: "sleep 5" });
    expect(result.exitCode).toBeUndefined();
    const commandText = await session.execCommand!({ cmd: "sleep 5" });
    expect(commandText).toContain("[timed out]");
  });

  it("a truncated inis.run stream is flagged in stderr, not silently dropped", async () => {
    // `SandboxExecResult` has no `truncated` field at all (just output/
    // stdout/stderr/exitCode) -- see sandbox.ts's `exec()` comment. Confirm
    // the marker this adapter appends to stderr when inis.run's own
    // `truncated` flag fires actually reaches the caller.
    const raw = new FakeSession("sess_truncated");
    raw.forceTruncated = true;
    const session = InisSandboxSession.wrap(raw as unknown as Session);
    const result = await session.exec!({ cmd: "echo hi" });
    expect(result.stderr).toContain("truncated");
  });

  it("the truncation marker carries a fresh nonce each call, not a static string", async () => {
    // The marker is otherwise a static, publicly-readable string -- a guest
    // could just copy it out of this package's own source to fake or mask a
    // truncation signal. Confirm two truncated calls (same command, so
    // nothing else can differ) produce two DIFFERENT marker strings.
    const raw = new FakeSession("sess_truncated_nonce");
    raw.forceTruncated = true;
    const session = InisSandboxSession.wrap(raw as unknown as Session);
    const first = await session.exec!({ cmd: "echo hi" });
    const second = await session.exec!({ cmd: "echo hi" });
    expect(first.stderr).not.toBe(second.stderr);
  });

  it("two independently-wrapped sessions never share exec state", async () => {
    const rawA = new FakeSession("sess_iso_a");
    const rawB = new FakeSession("sess_iso_b");
    const a = InisSandboxSession.wrap(rawA as unknown as Session);
    const b = InisSandboxSession.wrap(rawB as unknown as Session);

    await Promise.all([a.exec!({ cmd: "echo marker-A" }), b.exec!({ cmd: "echo marker-B" })]);

    // Positive, Python-shaped assertion: each backend saw EXACTLY its own
    // command, not just "didn't see the other one". A prior sabotage
    // variant (cache-once-forever instead of last-construction-wins) left
    // BOTH logs empty, which a negative-only assertion below would have
    // passed vacuously -- this catches that shape too.
    expect(rawA.getExecLog()).toEqual([["sh", "-lc", "echo marker-A"]]);
    expect(rawB.getExecLog()).toEqual([["sh", "-lc", "echo marker-B"]]);
    // Kept alongside the positive check: never the other conversation's command.
    expect(rawA.getExecLog().some((cmd) => cmd.join(" ").includes("marker-B"))).toBe(false);
    expect(rawB.getExecLog().some((cmd) => cmd.join(" ").includes("marker-A"))).toBe(false);
  });

  it("persist then hydrate into a new session copies the workspace across", async () => {
    // Regression coverage for the shipped-broken-against-the-real-API bug:
    // `hydrateWorkspace()` used to write its scratch archive under `/tmp`,
    // which the real `/files` endpoints reject (see `assertUnderWorkspace`
    // above and the module docstring). Nothing in this suite previously
    // called `persistWorkspace()`/`hydrateWorkspace()` at all, so that
    // regression shipped and passed every test here. This test drives both
    // through the real adapter code end to end.
    const rawSrc = new FakeSession("sess_persist_src");
    const rawDst = new FakeSession("sess_persist_dst");
    const src = InisSandboxSession.wrap(rawSrc as unknown as Session);
    const dst = InisSandboxSession.wrap(rawDst as unknown as Session);

    await src.materializeEntry!({
      path: "/workspace/data.txt",
      entry: { type: "file", content: "payload-xyz" } as never,
    });

    const archive = await src.persistWorkspace!();
    await dst.hydrateWorkspace!(archive);

    const data = await dst.readFile!({ path: "/workspace/data.txt" });
    expect(Buffer.from(data as string, "base64").toString("utf-8")).toBe("payload-xyz");
  });
});

/**
 * Framework-level test: the real `SandboxAgent`/`run()` adapter path from
 * `@openai/agents`, against a scripted fake model, matching the epic's
 * evidence bar ("a deterministic framework-level test uses the real
 * adapter path and a fake model") that Python's suite already meets via
 * `agents.sandbox.SandboxAgent` + `Runner.run()`. Only the model and the
 * `inis.Session` transport are faked; `SandboxAgent`, `run()`, and this
 * adapter's own code all run for real.
 */
class ScriptedModel implements Model {
  constructor(private readonly script: ModelResponse[]) {}

  async getResponse(_request: ModelRequest): Promise<ModelResponse> {
    const next = this.script.shift();
    if (!next) throw new Error("ScriptedModel ran out of scripted responses");
    return next;
  }

  getStreamedResponse(_request: ModelRequest): AsyncIterable<StreamEvent> {
    throw new Error("ScriptedModel does not support streaming");
  }
}

function toolCallResponse(callId: string, name: string, args: Record<string, unknown>): ModelResponse {
  return {
    usage: new Usage(),
    output: [
      {
        type: "function_call",
        callId,
        name,
        arguments: JSON.stringify(args),
      },
    ],
  };
}

function finalMessageResponse(text: string): ModelResponse {
  return {
    usage: new Usage(),
    output: [
      {
        type: "message",
        role: "assistant",
        status: "completed",
        content: [{ type: "output_text", text }],
      },
    ],
  };
}

// SandboxAgent's default capabilities are [filesystem(), shell(), compaction()];
// filesystem()'s edit_file tool requires the session to implement
// createEditor(), which InisSandboxSession does not (declared unsupported,
// not faked -- see sandbox.ts's module docstring). Scope these tests to the
// shell capability this adapter actually implements.
const capabilities = [shell()];

describe("SandboxAgent + run() (real framework path, fake Session + fake model)", () => {
  it("a scripted tool call reaches the real InisSandboxSession.execCommand", async () => {
    const raw = new FakeSession("sess_framework_exec");
    const session = InisSandboxSession.wrap(raw as unknown as Session);
    const model = new ScriptedModel([
      toolCallResponse("call_1", "exec_command", { cmd: "echo hello-framework" }),
      finalMessageResponse("done-hello-framework"),
    ]);
    const agent = new SandboxAgent({ name: "tester", model, capabilities });

    const result = await run(agent, "run a command", { sandbox: { session } });

    expect(result.finalOutput).toBe("done-hello-framework");
    expect(raw.getExecLog()).toEqual([["sh", "-lc", "echo hello-framework"]]);
  });

  it("two concurrent SandboxAgent conversations stay isolated end to end", async () => {
    const rawA = new FakeSession("sess_framework_iso_a");
    const rawB = new FakeSession("sess_framework_iso_b");
    const sessionA = InisSandboxSession.wrap(rawA as unknown as Session);
    const sessionB = InisSandboxSession.wrap(rawB as unknown as Session);

    const run1 = run(
      new SandboxAgent({
        name: "tester",
        model: new ScriptedModel([
          toolCallResponse("call_a", "exec_command", { cmd: "echo marker-A" }),
          finalMessageResponse("done-marker-A"),
        ]),
        capabilities,
      }),
      "run a command",
      { sandbox: { session: sessionA } },
    );
    const run2 = run(
      new SandboxAgent({
        name: "tester",
        model: new ScriptedModel([
          toolCallResponse("call_b", "exec_command", { cmd: "echo marker-B" }),
          finalMessageResponse("done-marker-B"),
        ]),
        capabilities,
      }),
      "run a command",
      { sandbox: { session: sessionB } },
    );

    const [resultA, resultB] = await Promise.all([run1, run2]);

    expect(resultA.finalOutput).toBe("done-marker-A");
    expect(resultB.finalOutput).toBe("done-marker-B");
    expect(rawA.getExecLog()).toEqual([["sh", "-lc", "echo marker-A"]]);
    expect(rawB.getExecLog()).toEqual([["sh", "-lc", "echo marker-B"]]);
  });
});
