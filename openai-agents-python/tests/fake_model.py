"""A minimal, scripted `agents.models.interface.Model` for deterministic
SandboxAgent tests — no third-party model API involved, a deterministic
fake/test model for framework-adapter canaries."""

from __future__ import annotations

import json

from agents.items import ModelResponse
from agents.models.interface import Model
from agents.usage import Usage
from openai.types.responses import (
    ResponseFunctionToolCall,
    ResponseOutputMessage,
    ResponseOutputText,
)


class ScriptedModel(Model):
    """Replays a fixed list of `ModelResponse`s, one per `get_response()` call."""

    def __init__(self, script: list[ModelResponse]) -> None:
        self._script = list(script)

    async def get_response(
        self,
        system_instructions,
        input,
        model_settings,
        tools,
        output_schema,
        handoffs,
        tracing,
        *,
        previous_response_id,
        conversation_id,
        prompt,
    ) -> ModelResponse:
        if not self._script:
            raise AssertionError("ScriptedModel ran out of scripted responses")
        return self._script.pop(0)

    async def stream_response(self, *args, **kwargs):  # pragma: no cover - unused here
        raise NotImplementedError("ScriptedModel does not support streaming")


def tool_call(call_id: str, name: str, arguments: dict) -> ModelResponse:
    return ModelResponse(
        output=[
            ResponseFunctionToolCall(
                id=call_id,
                call_id=call_id,
                name=name,
                arguments=json.dumps(arguments),
                type="function_call",
            )
        ],
        usage=Usage(),
        response_id=None,
    )


def final_message(text: str) -> ModelResponse:
    return ModelResponse(
        output=[
            ResponseOutputMessage(
                id="msg_1",
                role="assistant",
                status="completed",
                type="message",
                content=[ResponseOutputText(text=text, type="output_text", annotations=[])],
            )
        ],
        usage=Usage(),
        response_id=None,
    )
