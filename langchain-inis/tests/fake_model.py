"""A minimal, scripted chat model for deterministic `create_deep_agent()`
tests — no third-party model API involved.

`langchain_core.language_models.fake_chat_models.FakeMessagesListChatModel`
(the framework's own test double) cycles through a fixed list of
`BaseMessage`s but its `bind_tools()` raises `NotImplementedError` — every
real chat model overrides it, deepagents' `create_agent()` calls it while
assembling the graph, so a script-only fake needs a trivial override that
just returns itself (the tool schema deepagents passes in is not needed:
the script below already encodes which tool call to emit at each step).
"""

from __future__ import annotations

from typing import Any

from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, BaseMessage


class ScriptedToolCallingModel(FakeMessagesListChatModel):
    """Replays a fixed list of `BaseMessage`s, one per model turn, and
    accepts (and ignores) `bind_tools()` so deepagents' graph assembly
    doesn't hit `FakeMessagesListChatModel`'s `NotImplementedError` default."""

    def bind_tools(self, tools: Any, **kwargs: Any) -> "ScriptedToolCallingModel":
        return self


def tool_call_message(name: str, args: dict[str, Any], *, call_id: str) -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": call_id}])


def final_message(text: str) -> AIMessage:
    return AIMessage(content=text)


def scripted_model(script: list[BaseMessage]) -> ScriptedToolCallingModel:
    return ScriptedToolCallingModel(responses=script)
