"""A minimal scripted `agent_framework.BaseChatClient` for deterministic
`Agent.run()` tests: drives one `execute_code` call per top-level `run()`,
from a caller-supplied list of code snippets consumed one at a time, then a
final text turn once it sees the matching `function_result`. Not a
general-purpose test double for Agent Framework itself -- just enough of the
`BaseChatClient` contract to drive one real tool call per run through a real
`Agent` + `InisCodeActProvider`.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from agent_framework import ChatResponse, Content, Message
from agent_framework._clients import BaseChatClient
from agent_framework._tools import FunctionInvocationLayer


class ScriptedCodeActClient(FunctionInvocationLayer, BaseChatClient):
    """Emits `execute_code(code=codes[i])` on the i-th `run()` call, then a
    final text response once the corresponding `function_result` shows up in
    the conversation.

    Composes `FunctionInvocationLayer` (a cooperative mixin, per its own
    docstring: "Layer for chat clients to apply function invocation around
    get_response") ahead of `BaseChatClient` in the MRO -- `Agent.__init__`
    only drives the real tool-call loop when
    `isinstance(client, FunctionInvocationLayer)` (see `agent_framework.
    _agents`), and otherwise just warns and returns the raw
    `function_call` content unexecuted.
    """

    def __init__(self, codes: Sequence[str]) -> None:
        super().__init__()
        self._codes = list(codes)
        self._next_index = 0
        self._pending_call_id: str | None = None
        self.raw_responses: list[list[Content]] = []

    def _has_pending_result(self, messages: Sequence[Message]) -> bool:
        if self._pending_call_id is None:
            return False
        for message in messages:
            for content in message.contents:
                if content.type == "function_result" and content.call_id == self._pending_call_id:
                    return True
        return False

    async def _inner_get_response(
        self,
        *,
        messages: Sequence[Message],
        stream: bool,
        options: Mapping[str, Any],
        **kwargs: Any,
    ) -> Any:
        await self._validate_options(options)
        if stream:
            raise NotImplementedError("ScriptedCodeActClient does not support streaming")

        if self._has_pending_result(messages):
            self._pending_call_id = None
            contents = [Content.from_text(f"done (step {self._next_index - 1})")]
            self.raw_responses.append(contents)
            return ChatResponse(messages=[Message(role="assistant", contents=contents)])

        if self._next_index >= len(self._codes):
            contents = [Content.from_text("nothing left to run")]
            self.raw_responses.append(contents)
            return ChatResponse(messages=[Message(role="assistant", contents=contents)])

        code = self._codes[self._next_index]
        call_id = f"call_{self._next_index}"
        self._pending_call_id = call_id
        self._next_index += 1
        contents = [Content.from_function_call(call_id, "execute_code", arguments={"code": code})]
        self.raw_responses.append(contents)
        return ChatResponse(messages=[Message(role="assistant", contents=contents)])
