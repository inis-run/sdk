import { existsSync, mkdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import {
  Agent,
  JsonBlock,
  SandboxPathNotFoundError,
  SandboxTimeoutError,
  TextBlock,
  ToolResultBlock,
} from "@strands-agents/sdk";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { ScriptedToolCallModel } from "./fake-model.js";
import { asSession, FakeSession } from "./fake-session.js";
import { InisSandbox } from "./sandbox.js";

async function collect<T>(gen: AsyncGenerator<T, void, undefined>): Promise<T[]> {
  const out: T[] = [];
  for await (const item of gen) out.push(item);
  return out;
}

describe("InisSandbox.wrap", () => {
  it("requires an attached session", () => {
    const unattached = { sessionId: undefined } as never;
    expect(() => InisSandbox.wrap(unattached)).toThrow();
  });

  it("aclose is a no-op for a wrapped sandbox", async () => {
    const fake = new FakeSession("sess-wrap-1");
    const sandbox = InisSandbox.wrap(asSession(fake));
    await sandbox.close();
    expect(fake.destroyed).toBe(false);
    fake.cleanup();
  });
});

describe("InisSandbox.executeStreaming", () => {
  let fake: FakeSession;
  let sandbox: InisSandbox;

  beforeEach(() => {
    fake = new FakeSession("sess-exec-1");
    sandbox = InisSandbox.wrap(asSession(fake));
  });

  afterEach(() => fake.cleanup());

  it("yields distinct stdout/stderr chunks then a final ExecutionResult", async () => {
    const events = await collect(sandbox.executeStreaming("echo out1; echo err1 1>&2; exit 3"));
    const chunks = events.filter((e) => e.type === "streamChunk");
    const result = events[events.length - 1];
    expect(result.type).toBe("executionResult");
    if (result.type !== "executionResult") throw new Error("unreachable");
    expect(result.exitCode).toBe(3);
    expect(result.stdout).toContain("out1");
    expect(result.stderr).toContain("err1");
    expect(chunks.some((c) => c.type === "streamChunk" && c.streamType === "stdout" && c.data.includes("out1"))).toBe(
      true,
    );
    expect(chunks.some((c) => c.type === "streamChunk" && c.streamType === "stderr" && c.data.includes("err1"))).toBe(
      true,
    );
  });

  it("applies env via an export prefix", async () => {
    const events = await collect(sandbox.executeStreaming("echo $GREETING", { env: { GREETING: "hello world" } }));
    const result = events[events.length - 1];
    expect(result.type).toBe("executionResult");
    if (result.type !== "executionResult") throw new Error("unreachable");
    expect(result.exitCode).toBe(0);
    expect(result.stdout).toContain("hello world");
  });

  it("rejects an invalid env key before making any request", async () => {
    await expect(collect(sandbox.executeStreaming("echo hi", { env: { "1BAD": "x" } }))).rejects.toThrow();
  });

  it("cancellation via AbortSignal kills the remote process", async () => {
    // A command that keeps producing output (rather than one that runs
    // silently to completion) so a chunk arrives and the abort race can act
    // on it: `Session.execStream` has no cancellation parameter of its own
    // (verified against its source -- see sandbox.ts's module docstring), so
    // an abort can only preempt at the next stdout/stderr chunk boundary or
    // process exit, not an already-pending read while the command is
    // silent. A `for await (const chunk of gen)` loop that `break`s after
    // its first chunk hits this same boundary naturally, so this is
    // representative real-world usage, not a special case.
    const marker = join(fake.tmpRoot, "workspace", "marker");
    const controller = new AbortController();
    const gen = sandbox.executeStreaming(
      `for i in $(seq 1 50); do echo tick; sleep 0.1; done; touch ${marker}`,
      { signal: controller.signal },
    );

    const first = await gen.next();
    expect(first.done).toBe(false);
    controller.abort();

    await expect(gen.next()).rejects.toThrow();

    await new Promise((resolve) => setTimeout(resolve, 300));
    expect(fake.lastChild?.killed || fake.lastChild?.exitCode !== null).toBe(true);
    expect(existsSync(marker)).toBe(false);
  });

  it("raises SandboxTimeoutError when the command exceeds timeout", async () => {
    await expect(collect(sandbox.executeStreaming("sleep 5", { timeout: 0.2 }))).rejects.toBeInstanceOf(
      SandboxTimeoutError,
    );
  });
});

describe("InisSandbox.executeCodeStreaming", () => {
  it("runs python via the inherited base64 heredoc", async () => {
    const fake = new FakeSession("sess-code-1");
    const sandbox = InisSandbox.wrap(asSession(fake));
    try {
      const events = await collect(sandbox.executeCodeStreaming("print(1 + 1)", "python3"));
      const result = events[events.length - 1];
      expect(result.type).toBe("executionResult");
      if (result.type !== "executionResult") throw new Error("unreachable");
      expect(result.exitCode).toBe(0);
      expect(result.stdout.trim()).toBe("2");
    } finally {
      fake.cleanup();
    }
  });

  it("rejects an invalid language", async () => {
    const fake = new FakeSession("sess-code-2");
    const sandbox = InisSandbox.wrap(asSession(fake));
    try {
      await expect(collect(sandbox.executeCodeStreaming("1", "python3; rm -rf /"))).rejects.toThrow();
    } finally {
      fake.cleanup();
    }
  });
});

describe("InisSandbox files", () => {
  let fake: FakeSession;
  let sandbox: InisSandbox;

  beforeEach(() => {
    fake = new FakeSession("sess-files-1");
    sandbox = InisSandbox.wrap(asSession(fake));
  });

  afterEach(() => fake.cleanup());

  it("round-trips binary content", async () => {
    const raw = new Uint8Array(256).map((_, i) => i);
    await sandbox.writeFile("/workspace/bin.dat", raw);
    const back = await sandbox.readFile("/workspace/bin.dat");
    expect(Array.from(back)).toEqual(Array.from(raw));
  });

  it("throws for a missing file", async () => {
    await expect(sandbox.readFile("/workspace/nope.txt")).rejects.toThrow();
  });

  it("marks directories in listFiles", async () => {
    await sandbox.writeFile("/workspace/a.txt", new TextEncoder().encode("a"));
    mkdirSync(join(fake.tmpRoot, "workspace", "subdir"));
    const entries = await sandbox.listFiles("/workspace");
    const byName = Object.fromEntries(entries.map((e) => [e.name, e]));
    expect(byName["a.txt"]?.isDir).toBe(false);
    expect(byName["subdir"]?.isDir).toBe(true);
  });

  it("throws SandboxPathNotFoundError for a missing directory", async () => {
    await expect(sandbox.listFiles("/workspace/does-not-exist")).rejects.toBeInstanceOf(SandboxPathNotFoundError);
  });

  it("removeFile actually removes the file", async () => {
    await sandbox.writeFile("/workspace/temp.txt", new TextEncoder().encode("x"));
    await sandbox.removeFile("/workspace/temp.txt");
    await expect(sandbox.readFile("/workspace/temp.txt")).rejects.toThrow();
  });
});

describe("InisSandbox.getTools", () => {
  it("vends sandbox_bash and sandbox_file_editor only", () => {
    const fake = new FakeSession("sess-tools-1");
    const sandbox = InisSandbox.wrap(asSession(fake));
    try {
      const names = sandbox.getTools().map((t) => (t as { name: string }).name).sort();
      expect(names).toEqual(["sandbox_bash", "sandbox_file_editor"]);
    } finally {
      fake.cleanup();
    }
  });
});

describe("InisSandbox owned lifecycle", () => {
  it("create() destroys the session on close()", async () => {
    const fake = new FakeSession("sess-owned-1");
    const fakeClient = {
      sessions: {
        create: async () => asSession(fake),
      },
    } as never;

    const sandbox = await InisSandbox.create(fakeClient);
    await sandbox.close();
    expect(fake.destroyed).toBe(true);
    fake.cleanup();
  });
});

describe("Agent integration", () => {
  it("runs the sandbox_bash tool through a real Agent", async () => {
    const fake = new FakeSession("sess-agent-bash-1");
    const sandbox = InisSandbox.wrap(asSession(fake));
    try {
      const model = new ScriptedToolCallModel("sandbox_bash", { command: "echo hello-from-inis" }, "ran it");
      const agent = new Agent({ model, sandbox });
      const result = await agent.invoke("run a command");

      const finalText = result.lastMessage.content.find((b): b is TextBlock => b instanceof TextBlock)?.text;
      expect(finalText).toContain("ran it");

      // sandbox_bash returns a BashOutput object ({ output, error }), so the
      // framework wraps its tool result as a JsonBlock, not a TextBlock.
      const toolResultJson = agent.messages
        .flatMap((m) => m.content)
        .filter((b): b is ToolResultBlock => b instanceof ToolResultBlock)
        .flatMap((b) => b.content)
        .filter((c): c is JsonBlock => c instanceof JsonBlock)
        .map((c) => c.json as { output?: string; error?: string });
      expect(toolResultJson.some((j) => (j.output ?? "").includes("hello-from-inis"))).toBe(true);
    } finally {
      fake.cleanup();
    }
  });

  it("runs the sandbox_file_editor tool through a real Agent", async () => {
    const fake = new FakeSession("sess-agent-editor-1");
    const sandbox = InisSandbox.wrap(asSession(fake));
    try {
      const model = new ScriptedToolCallModel(
        "sandbox_file_editor",
        { command: "create", path: "/workspace/note.txt", file_text: "hello" },
        "created it",
      );
      const agent = new Agent({ model, sandbox });
      const result = await agent.invoke("create a file");

      const finalText = result.lastMessage.content.find((b): b is TextBlock => b instanceof TextBlock)?.text;
      expect(finalText).toContain("created it");
      expect(readFileSync(join(fake.tmpRoot, "workspace", "note.txt"), "utf-8")).toBe("hello");
    } finally {
      fake.cleanup();
    }
  });
});
