// Deterministic framework-level tests: a REAL generateText() call through
// the real ai-sdk tool-loop (inisTools()/inisLifecycleTools()), driven by a
// FAKE model (ai/test's MockLanguageModelV2) — not a direct
// tool.execute(...) call, which would skip the real tool-call routing and
// input-schema validation the framework does — a deterministic
// framework-level test uses the real adapter path and a fake model.
//
// Session I/O is mocked at the inis SDK boundary (Session.prototype.exec /
// .files.read / .files.write / .listFiles), not at the HTTP layer — no
// network, no live fleet.

import { generateText, stepCountIs } from "ai";
import { MockLanguageModelV2 } from "ai/test";
import { Client, ExecResult, InisError, Session } from "@inis-run/sdk";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { createOwnedInisTools, inisTools } from "./tools.js";

function execResult(overrides: Partial<ExecResult> = {}): ExecResult {
  return {
    stdout: "",
    stderr: "",
    exitCode: 0,
    durationMs: 1,
    timedOut: false,
    ...overrides,
  };
}

/** One tool-call step, then a final text step — the minimal deterministic
 * driver for a real generateText() run. */
function oneShotModel(toolName: string, input: Record<string, unknown>, finalText: string) {
  let step = 0;
  return new MockLanguageModelV2({
    doGenerate: async () => {
      step += 1;
      if (step === 1) {
        return {
          finishReason: "tool-calls" as const,
          usage: { inputTokens: 1, outputTokens: 1, totalTokens: 2 },
          content: [
            {
              type: "tool-call" as const,
              toolCallId: "call_1",
              toolName,
              input: JSON.stringify(input),
            },
          ],
          warnings: [],
        };
      }
      return {
        finishReason: "stop" as const,
        usage: { inputTokens: 1, outputTokens: 1, totalTokens: 2 },
        content: [{ type: "text" as const, text: finalText }],
        warnings: [],
      };
    },
  });
}

beforeEach(() => {
  vi.restoreAllMocks();
});

describe("inisTools", () => {
  it("requires sessionId — never silently no-ops", () => {
    // @ts-expect-error - deliberately omitting the required field
    expect(() => inisTools({})).toThrow(InisError);
    // @ts-expect-error - deliberately omitting the required field
    expect(() => inisTools({})).toThrow(/sessionId/);
  });

  it("exposes only the bounded execution/file surface by default", () => {
    const tools = inisTools({ sessionId: "ses_test123", apiKey: "tok_test" });
    expect(Object.keys(tools).sort()).toEqual([
      "execute_python",
      "execute_shell",
      "list_files",
      "read_file",
      "write_file",
    ]);
  });

  it("runs execute_python through a real generateText() tool loop", async () => {
    const execSpy = vi
      .spyOn(Session.prototype, "exec")
      .mockResolvedValue(execResult({ stdout: "hi\n" }));

    const tools = inisTools({ sessionId: "ses_test123", apiKey: "tok_test" });
    const model = oneShotModel("execute_python", { code: "print('hi')" }, "done");

    const result = await generateText({ model, tools, stopWhen: stepCountIs(2), prompt: "run it" });

    expect(result.text).toBe("done");
    expect(execSpy).toHaveBeenCalledWith(["python3", "-c", "print('hi')"]);
  });

  it("execute_python throws on non-zero exit instead of reporting success", async () => {
    vi.spyOn(Session.prototype, "exec").mockResolvedValue(
      execResult({ stderr: "boom", exitCode: 1 }),
    );
    const tools = inisTools({ sessionId: "ses_test123", apiKey: "tok_test" });

    await expect(
      tools.execute_python.execute!({ code: "raise SystemExit(1)" }, {
        toolCallId: "x",
        messages: [],
      }),
    ).rejects.toThrow(/exited 1/);
  });

  it("execute_shell raises on non-zero exit", async () => {
    vi.spyOn(Session.prototype, "exec").mockResolvedValue(
      execResult({ stderr: "boom", exitCode: 1 }),
    );
    const tools = inisTools({ sessionId: "ses_test123", apiKey: "tok_test" });

    await expect(
      tools.execute_shell.execute!({ command: "false" }, { toolCallId: "x", messages: [] }),
    ).rejects.toThrow(/exit 1/);
  });

  it("read_file/write_file round-trip through the real tool loop", async () => {
    const store = new Map<string, string>();
    vi.spyOn(Session.prototype, "writeFile").mockImplementation(async (path, content) => {
      store.set(path, content as string);
    });
    vi.spyOn(Session.prototype, "readFile").mockImplementation(
      async (path) => store.get(path) ?? "",
    );

    const tools = inisTools({ sessionId: "ses_test123", apiKey: "tok_test" });
    await tools.write_file.execute!(
      { path: "/workspace/a.txt", content: "hello" },
      { toolCallId: "x", messages: [] },
    );
    const read = await tools.read_file.execute!(
      { path: "/workspace/a.txt" },
      { toolCallId: "x", messages: [] },
    );
    expect(read).toBe("hello");
  });
});

describe("createOwnedInisTools", () => {
  it("creates a session, returns its id, and cleanup() destroys exactly that session", async () => {
    const createSpy = vi
      .spyOn(Session, "create")
      .mockResolvedValue(Session.attach({ baseUrl: "https://api.inis.run", token: "t" } as Client, "ses_owned"));
    const destroySpy = vi.spyOn(Session.prototype, "destroy").mockResolvedValue(undefined);

    const owned = await createOwnedInisTools({ apiKey: "tok_test" });

    expect(owned.sessionId).toBe("ses_owned");
    expect(Object.keys(owned.tools)).toContain("execute_python");
    expect(createSpy).toHaveBeenCalledTimes(1);

    await owned.cleanup();
    expect(destroySpy).toHaveBeenCalledTimes(1);
  });
});

describe("two concurrent conversations stay isolated", () => {
  it("never lets one conversation's calls land on the other's session", async () => {
    const calls: Record<string, string[]> = { ses_a: [], ses_b: [] };

    vi.spyOn(Session.prototype, "exec").mockImplementation(async function (
      this: Session,
      command: string | string[],
    ) {
      // Force interleaving: ses_a's call resolves after a delay, ses_b's
      // resolves immediately, so ses_b would win any shared-state race.
      await new Promise((resolve) =>
        setTimeout(resolve, this.sessionId === "ses_a" ? 20 : 0),
      );
      const cmd = Array.isArray(command) ? command[command.length - 1] : command;
      calls[this.sessionId].push(cmd);
      return execResult({ stdout: `${this.sessionId}-out\n` });
    });

    function run(sessionId: "ses_a" | "ses_b") {
      const tools = inisTools({ sessionId, apiKey: "tok_test" });
      const model = oneShotModel(
        "execute_shell",
        { command: `echo ${sessionId}` },
        `done-${sessionId}`,
      );
      return generateText({ model, tools, stopWhen: stepCountIs(2), prompt: "go" });
    }

    const [resultA, resultB] = await Promise.all([run("ses_a"), run("ses_b")]);

    expect(resultA.text).toBe("done-ses_a");
    expect(resultB.text).toBe("done-ses_b");
    expect(calls).toEqual({ ses_a: ["echo ses_a"], ses_b: ["echo ses_b"] });
  });
});
