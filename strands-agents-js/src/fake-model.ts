/**
 * A minimal scripted `Model` for deterministic Agent-level tests: emits one
 * tool-use turn (calling a given tool with a given input), then a final text
 * turn once it sees the corresponding `ToolResultBlock` in the conversation.
 * Not a general-purpose test double for Strands itself -- just enough of the
 * `Model` contract (verified against the installed
 * `@strands-agents/sdk` source's actual event/message shapes, not guessed)
 * to drive one real tool call through a real `Agent`.
 */

import { type BaseModelConfig, Message, Model, type ModelStreamEvent, ToolResultBlock } from "@strands-agents/sdk";

export class ScriptedToolCallModel extends Model {
  readonly calls: Message[][] = [];

  constructor(
    private readonly toolName: string,
    private readonly toolInput: Record<string, unknown>,
    private readonly finalText = "done",
  ) {
    super();
  }

  updateConfig(): void {
    // no-op: this test double has no configuration to update
  }

  getConfig(): BaseModelConfig {
    return {};
  }

  async *stream(messages: Message[]): AsyncIterable<ModelStreamEvent> {
    this.calls.push(messages);
    const alreadyCalled = messages.some((m) => m.content.some((block) => block instanceof ToolResultBlock));

    yield { type: "modelMessageStartEvent", role: "assistant" };
    if (!alreadyCalled) {
      yield {
        type: "modelContentBlockStartEvent",
        start: { type: "toolUseStart", name: this.toolName, toolUseId: "tool_call_1" },
      };
      yield {
        type: "modelContentBlockDeltaEvent",
        delta: { type: "toolUseInputDelta", input: JSON.stringify(this.toolInput) },
      };
      yield { type: "modelContentBlockStopEvent" };
      // Strands TS uses camelCase StopReason values ('toolUse'/'endTurn'),
      // unlike the Python SDK's snake_case ('tool_use'/'end_turn') -- verified
      // against agent.js's own `stopReason !== 'toolUse'` check, not guessed.
      yield { type: "modelMessageStopEvent", stopReason: "toolUse" };
    } else {
      yield { type: "modelContentBlockDeltaEvent", delta: { type: "textDelta", text: this.finalText } };
      yield { type: "modelContentBlockStopEvent" };
      yield { type: "modelMessageStopEvent", stopReason: "endTurn" };
    }
  }
}
