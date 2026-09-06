import { generateText, stepCountIs } from "ai";
import { MockLanguageModelV2 } from "ai/test";
import { Client, InisError, Session } from "@inis-run/sdk";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { inisEphemeralTools, inisLifecycleTools } from "./lifecycle.js";

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
            { type: "tool-call" as const, toolCallId: "call_1", toolName, input: JSON.stringify(input) },
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

describe("inisLifecycleTools", () => {
  it("requires sessionId", () => {
    // @ts-expect-error - deliberately omitting the required field
    expect(() => inisLifecycleTools({})).toThrow(InisError);
  });

  it("is disjoint from the default execution/file tools — not exported from the main entry point", () => {
    const tools = inisLifecycleTools({ sessionId: "ses_test123", apiKey: "tok_test" });
    expect(Object.keys(tools).sort()).toEqual([
      "capture_artifacts",
      "checkpoint",
      "expose_port",
      "pause",
      "restore",
      "resume",
    ]);
  });

  it("pause then resume run through a real generateText() tool loop", async () => {
    const pauseSpy = vi
      .spyOn(Session.prototype, "pause")
      .mockResolvedValue({ sessionId: "ses_test123", state: "paused" });
    const resumeSpy = vi
      .spyOn(Session.prototype, "resume")
      .mockResolvedValue({ sessionId: "ses_test123", state: "live" });

    const tools = inisLifecycleTools({ sessionId: "ses_test123", apiKey: "tok_test" });

    let step = 0;
    const model = new MockLanguageModelV2({
      doGenerate: async () => {
        step += 1;
        if (step === 1) {
          return {
            finishReason: "tool-calls" as const,
            usage: { inputTokens: 1, outputTokens: 1, totalTokens: 2 },
            content: [{ type: "tool-call" as const, toolCallId: "c1", toolName: "pause", input: "{}" }],
            warnings: [],
          };
        }
        if (step === 2) {
          return {
            finishReason: "tool-calls" as const,
            usage: { inputTokens: 1, outputTokens: 1, totalTokens: 2 },
            content: [{ type: "tool-call" as const, toolCallId: "c2", toolName: "resume", input: "{}" }],
            warnings: [],
          };
        }
        return {
          finishReason: "stop" as const,
          usage: { inputTokens: 1, outputTokens: 1, totalTokens: 2 },
          content: [{ type: "text" as const, text: "done" }],
          warnings: [],
        };
      },
    });

    const result = await generateText({ model, tools, stopWhen: stepCountIs(3), prompt: "pause then resume" });

    expect(result.text).toBe("done");
    expect(pauseSpy).toHaveBeenCalledTimes(1);
    expect(resumeSpy).toHaveBeenCalledTimes(1);
  });

  it("checkpoint returns the checkpoint id through the real tool loop", async () => {
    vi.spyOn(Session.prototype, "checkpoint").mockResolvedValue({
      checkpointId: "chk_abc123",
      sessionId: "ses_test123",
      sizeBytes: 0,
    });

    const tools = inisLifecycleTools({ sessionId: "ses_test123", apiKey: "tok_test" });
    const model = oneShotModel("checkpoint", { name: "before-risky-op" }, "done");

    const result = await generateText({ model, tools, stopWhen: stepCountIs(2), prompt: "checkpoint first" });
    expect(result.text).toBe("done");
    expect(result.steps[0].toolResults[0].output).toBe("chk_abc123");
  });
});

describe("inisEphemeralTools", () => {
  it("execute_once throws on non-zero exit instead of reporting success", async () => {
    vi.spyOn(Client.prototype, "execute").mockResolvedValue({
      stdout: "",
      stderr: "boom",
      exitCode: 1,
      durationMs: 1,
      timedOut: false,
    });

    const tools = inisEphemeralTools({ apiKey: "tok_test" });
    await expect(
      tools.execute_once.execute!(
        { code: "raise SystemExit(1)", language: "python" },
        { toolCallId: "x", messages: [] },
      ),
    ).rejects.toThrow(/exited 1/);
  });
});
