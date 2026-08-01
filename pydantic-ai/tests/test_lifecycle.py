"""Tests for the opt-in inis_pydantic_ai.lifecycle toolsets: construction,
non-default status (never returned by inis_toolset()), and one real
Agent.run() through each against a FunctionModel."""

from __future__ import annotations

from inis import CheckpointInfo, ExecResult, SessionInfo
from inis.async_client import AsyncSession
from inis_pydantic_ai.lifecycle import inis_ephemeral_toolset, inis_lifecycle_toolset
from inis_pydantic_ai.toolset import inis_toolset
from pydantic_ai import Agent
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import FunctionModel

LIFECYCLE_TOOLS = {
    "expose_port",
    "pause",
    "resume",
    "checkpoint",
    "restore",
    "capture_artifacts",
}


def test_lifecycle_tools_are_not_in_the_default_toolset():
    """The model-facing default exposes only the bounded execution/file
    surface it needs; lifecycle administration remains host-controlled or
    explicitly opt-in. Assert the two toolsets don't overlap."""
    default_tools = set(
        inis_toolset(
            session_id="ses_test123", api_key="tok_test", base_url="https://api.inis.run"
        ).tools
    )
    assert default_tools.isdisjoint(LIFECYCLE_TOOLS)
    assert "execute_once" not in default_tools


def test_lifecycle_toolset_constructs_and_generates_schemas():
    toolset = inis_lifecycle_toolset(
        session_id="ses_test123", api_key="tok_test", base_url="https://api.inis.run"
    )
    assert set(toolset.tools) == LIFECYCLE_TOOLS
    for tool in toolset.tools.values():
        assert tool.function_schema.json_schema is not None


def test_ephemeral_toolset_constructs_and_generates_schemas():
    toolset = inis_ephemeral_toolset(api_key="tok_test", base_url="https://api.inis.run")
    assert set(toolset.tools) == {"execute_once"}
    assert toolset.tools["execute_once"].function_schema.json_schema is not None


async def test_pause_then_resume_through_a_real_agent(monkeypatch):
    calls: list[str] = []

    async def fake_pause(self):
        calls.append("pause")
        return SessionInfo(session_id=self.session_id, state="paused")

    async def fake_resume(self):
        calls.append("resume")
        return SessionInfo(session_id=self.session_id, state="live")

    monkeypatch.setattr(AsyncSession, "pause", fake_pause)
    monkeypatch.setattr(AsyncSession, "resume", fake_resume)

    toolset = inis_lifecycle_toolset(
        session_id="ses_test123", api_key="tok_test", base_url="https://api.inis.run"
    )

    step = {"n": 0}

    async def fake_model(messages, info):
        step["n"] += 1
        if step["n"] == 1:
            return ModelResponse(parts=[ToolCallPart(tool_name="pause", args={})])
        if step["n"] == 2:
            return ModelResponse(parts=[ToolCallPart(tool_name="resume", args={})])
        return ModelResponse(parts=[TextPart(content="done")])

    agent = Agent(FunctionModel(fake_model), toolsets=[toolset])
    result = await agent.run("pause then resume")

    assert result.output == "done"
    assert calls == ["pause", "resume"]
    await toolset.aclose()


async def test_checkpoint_through_a_real_agent(monkeypatch):
    async def fake_checkpoint(self, *, name=None, labels=None):
        return CheckpointInfo(checkpoint_id="chk_abc123", session_id=self.session_id, name=name)

    monkeypatch.setattr(AsyncSession, "checkpoint", fake_checkpoint)

    toolset = inis_lifecycle_toolset(
        session_id="ses_test123", api_key="tok_test", base_url="https://api.inis.run"
    )
    model_calls: list[dict] = []

    step = {"n": 0}

    async def fake_model(messages, info):
        step["n"] += 1
        if step["n"] == 1:
            return ModelResponse(
                parts=[ToolCallPart(tool_name="checkpoint", args={"name": "before-risky-op"})]
            )
        # Record the tool result the model actually saw before answering.
        model_calls.append(messages[-1].parts[-1].content)
        return ModelResponse(parts=[TextPart(content="done")])

    agent = Agent(FunctionModel(fake_model), toolsets=[toolset])
    result = await agent.run("checkpoint first")

    assert result.output == "done"
    assert model_calls == ["chk_abc123"]
    await toolset.aclose()


async def test_execute_once_raises_on_nonzero_exit(monkeypatch):
    """Same fix as execute_python: a non-zero exit_code must not silently
    look like a successful call."""
    from inis.async_client import AsyncClient

    async def fake_execute(self, *, language, code, **kwargs):
        return ExecResult(
            stdout="", stderr="boom", exit_code=1, duration_ms=1, timed_out=False
        )

    monkeypatch.setattr(AsyncClient, "execute", fake_execute)

    toolset = inis_ephemeral_toolset(api_key="tok_test", base_url="https://api.inis.run")
    tool = toolset.tools["execute_once"]

    from inis import InisError
    import pytest

    with pytest.raises(InisError, match="boom"):
        await tool.function(code="raise SystemExit(1)")
    await toolset.aclose()
