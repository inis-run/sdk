"""A minimal scripted `strands.models.model.Model` for deterministic
Agent-level tests: emits one tool-use turn (calling a given tool with a given
input), then a final text turn once it sees the corresponding `toolResult` in
the conversation. Not a general-purpose test double for Strands itself --
just enough of the `Model` contract (verified against
`strands.event_loop.streaming.process_stream`'s actual chunk handling, not
guessed) to drive one real tool call through a real `Agent`.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

from strands.models.model import Model


class ScriptedToolCallModel(Model):
    """Calls `tool_name` once with `tool_input`, then answers `final_text`."""

    def __init__(self, *, tool_name: str, tool_input: dict[str, Any], final_text: str = "done") -> None:
        self._tool_name = tool_name
        self._tool_input = tool_input
        self._final_text = final_text
        self.calls: list[Any] = []

    def update_config(self, **model_config: Any) -> None:
        pass

    def get_config(self) -> Any:
        return {}

    async def structured_output(
        self, output_model: Any, prompt: Any, system_prompt: str | None = None, **kwargs: Any
    ) -> AsyncIterator[dict[str, Any]]:
        raise NotImplementedError("ScriptedToolCallModel does not support structured_output")
        yield {}  # pragma: no cover - unreachable, satisfies the AsyncGenerator signature

    async def stream(
        self,
        messages: Any,
        tool_specs: Any = None,
        system_prompt: str | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[dict[str, Any]]:
        self.calls.append(messages)
        already_called = any("toolResult" in block for message in messages for block in message.get("content", []))

        yield {"messageStart": {"role": "assistant"}}
        if not already_called:
            yield {
                "contentBlockStart": {
                    "contentBlockIndex": 0,
                    "start": {"toolUse": {"toolUseId": "tool_call_1", "name": self._tool_name}},
                }
            }
            yield {
                "contentBlockDelta": {
                    "contentBlockIndex": 0,
                    "delta": {"toolUse": {"input": json.dumps(self._tool_input)}},
                }
            }
            yield {"contentBlockStop": {"contentBlockIndex": 0}}
            yield {"messageStop": {"stopReason": "tool_use"}}
        else:
            yield {"contentBlockDelta": {"contentBlockIndex": 0, "delta": {"text": self._final_text}}}
            yield {"contentBlockStop": {"contentBlockIndex": 0}}
            yield {"messageStop": {"stopReason": "end_turn"}}
