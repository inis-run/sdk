"""A minimal scripted `google.adk.models.base_llm.BaseLlm` for deterministic
`Agent`-level tests, driven through a real `google.adk.runners.InMemoryRunner`.
Calls one tool once with a fixed name/args, then answers with `final_text`
once it sees the matching `function_response` in the conversation history.
Not a general-purpose test double for ADK itself -- just enough of the
`BaseLlm` contract to drive one real tool call through a real `Agent`.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any

from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.genai import types


class ScriptedToolCallLlm(BaseLlm):
    """Calls `tool_name(**tool_args)` once, then answers `final_text`."""

    model: str = "fake"
    tool_name: str
    tool_args: dict[str, Any]
    final_text: str = "done"
    call_id: str = "call_1"

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse, None]:
        if stream:
            raise NotImplementedError("ScriptedToolCallLlm does not support streaming")

        for content in llm_request.contents:
            for part in content.parts or []:
                if part.function_response is not None and part.function_response.id == self.call_id:
                    yield LlmResponse(content=types.Content(role="model", parts=[types.Part(text=self.final_text)]))
                    return

        fc = types.FunctionCall(id=self.call_id, name=self.tool_name, args=self.tool_args)
        yield LlmResponse(content=types.Content(role="model", parts=[types.Part(function_call=fc)]))


async def run_one_turn(agent, *, user_id: str = "u1", app_name: str = "test_app", message: str = "go") -> list:
    """Drive one `InMemoryRunner.run_async()` turn against `agent` and return
    the collected events."""
    from google.adk.runners import InMemoryRunner

    runner = InMemoryRunner(agent=agent, app_name=app_name)
    session = await runner.session_service.create_session(app_name=app_name, user_id=user_id)
    content = types.Content(role="user", parts=[types.Part(text=message)])
    events = []
    async for event in runner.run_async(user_id=user_id, session_id=session.id, new_message=content):
        events.append(event)
    return events


def final_text_of(events: list) -> str:
    for event in reversed(events):
        if event.content and event.content.parts:
            for part in event.content.parts:
                if part.text:
                    return part.text
    return ""
