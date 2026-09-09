"""Tests for inis_pydantic_ai.toolset: construction, and framework-level runs
through a REAL pydantic-ai Agent driven by a deterministic FAKE model
(pydantic_ai.models.function.FunctionModel) — not just calling
`tool.function(...)` directly. Driving the real `Agent.run()` path exercises
tool-call routing, JSON-schema validation of the model's args, and toolset
wiring that a direct function call bypasses entirely — a deterministic
framework-level test uses the real adapter path and a fake model.

Regression coverage: as of pydantic-ai 2.0, @toolset.tool always
sets takes_ctx=True and raises a schema-generation UserError for a plain
function ("First parameter of tools that take context must be annotated
with RunContext[...]"). pydantic-ai builds each tool's JSON schema eagerly
at registration time (FunctionToolset.tool_plain -> Tool.__init__ ->
function_schema), so simply constructing inis_toolset() below already
exercises the exact code path that broke — CI import-only smoke
(`from inis_pydantic_ai import inis_toolset`) never touched it because the
toolset is only built once a session_id is supplied.
"""

from __future__ import annotations

import asyncio

import pytest
from inis import ExecResult, InisError
from inis.async_client import AsyncSession
from inis_pydantic_ai.toolset import inis_toolset
from pydantic_ai import Agent
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import FunctionModel

# The smallest execution/file surface — the model-facing default exposes
# only the bounded execution/file surface it needs. Session/sandbox
# administration (pause, resume, checkpoint, restore, expose, one-shot
# sandboxes) lives in inis_pydantic_ai.lifecycle instead — opt-in, not here.
EXPECTED_TOOLS = {
    "execute_python",
    "execute_shell",
    "read_file",
    "write_file",
    "list_files",
}


def _one_shot_model(tool_name: str, args: dict, final_text: str) -> FunctionModel:
    """A FunctionModel that calls `tool_name(**args)` once, then answers with
    `final_text` — the minimal deterministic driver for a real Agent.run()."""
    step = {"n": 0}

    async def fake_model(messages, info):
        step["n"] += 1
        if step["n"] == 1:
            return ModelResponse(parts=[ToolCallPart(tool_name=tool_name, args=args)])
        return ModelResponse(parts=[TextPart(content=final_text)])

    return FunctionModel(fake_model)


def test_inis_toolset_constructs_and_generates_schemas():
    """Constructing the toolset must not raise, and every tool must be
    registered as context-free (none of our tools take a RunContext)."""
    toolset = inis_toolset(
        session_id="ses_test123", api_key="tok_test", base_url="https://api.inis.run"
    )
    assert set(toolset.tools) == EXPECTED_TOOLS
    for name, tool in toolset.tools.items():
        assert tool.takes_ctx is False, f"{name} unexpectedly takes RunContext"
        # tool_def / json schema must build without raising too — accessed
        # eagerly by pydantic-ai when the toolset is attached to an Agent.
        assert tool.function_schema.json_schema is not None


async def test_execute_python_runs_through_a_real_agent(monkeypatch):
    """Drive execute_python through an actual Agent.run() call against a
    FunctionModel, not a direct tool.function() invocation."""
    calls: list[list[str]] = []

    async def fake_exec(self, command, *, cwd=None, timeout_ms=None):
        calls.append(command)
        return ExecResult(
            stdout="hi\n", stderr="", exit_code=0, duration_ms=1, timed_out=False
        )

    monkeypatch.setattr(AsyncSession, "exec", fake_exec)

    toolset = inis_toolset(
        session_id="ses_test123", api_key="tok_test", base_url="https://api.inis.run"
    )
    model = _one_shot_model("execute_python", {"code": "print('hi')"}, "done")
    agent = Agent(model, toolsets=[toolset])

    result = await agent.run("run it")

    assert result.output == "done"
    assert calls == [["python3", "-c", "print('hi')"]]
    await toolset.aclose()


async def test_execute_python_nonzero_exit_is_not_reported_as_success(monkeypatch):
    """A non-zero exit must surface as a failed tool call, not a clean
    result — execute_python previously ignored exit_code entirely and
    always returned stdout."""

    async def fake_exec(self, command, *, cwd=None, timeout_ms=None):
        return ExecResult(
            stdout="", stderr="boom", exit_code=1, duration_ms=1, timed_out=False
        )

    monkeypatch.setattr(AsyncSession, "exec", fake_exec)

    toolset = inis_toolset(
        session_id="ses_test123", api_key="tok_test", base_url="https://api.inis.run"
    )
    tool = toolset.tools["execute_python"]
    with pytest.raises(InisError, match="boom"):
        await tool.function(code="raise SystemExit(1)")
    await toolset.aclose()


async def test_execute_shell_raises_inis_error_on_nonzero_exit(monkeypatch):
    async def fake_exec(self, command, *, cwd=None, timeout_ms=None):
        return ExecResult(
            stdout="", stderr="boom", exit_code=1, duration_ms=1, timed_out=False
        )

    monkeypatch.setattr(AsyncSession, "exec", fake_exec)

    toolset = inis_toolset(
        session_id="ses_test123", api_key="tok_test", base_url="https://api.inis.run"
    )
    tool = toolset.tools["execute_shell"]

    with pytest.raises(InisError, match="boom"):
        await tool.function(command="false")
    await toolset.aclose()


async def test_attached_session_is_never_destroyed(monkeypatch):
    """Attached sessions must not be destroyed. inis_toolset() only
    ever attaches — assert destroy() is never called anywhere on its path,
    and that aclose() (the documented cleanup) only closes the local
    transport."""
    destroyed = []
    closed = []

    async def fake_destroy(self, *, reason=None):
        destroyed.append(self.session_id)

    async def fake_aclose(self):
        closed.append(self.session_id)

    monkeypatch.setattr(AsyncSession, "destroy", fake_destroy)
    monkeypatch.setattr(AsyncSession, "aclose", fake_aclose)

    toolset = inis_toolset(
        session_id="ses_test123", api_key="tok_test", base_url="https://api.inis.run"
    )
    await toolset.aclose()

    assert destroyed == []
    assert closed == ["ses_test123"]


async def test_two_concurrent_conversations_stay_isolated(monkeypatch):
    """Two concurrent conversations must prove isolation. Two
    independent toolsets/agents, each bound to its own session, run
    concurrently via asyncio.gather with interleaved awaits (the fake exec
    sleeps opposite amounts per session to force interleaving) — no call
    should ever land against the wrong session."""
    calls: dict[str, list[str]] = {"ses_a": [], "ses_b": []}

    async def fake_exec(self, command, *, cwd=None, timeout_ms=None):
        # Force interleaving: ses_a's call is delayed, ses_b's is not, so
        # ses_b would resolve first if any shared/global state were racing.
        await asyncio.sleep(0.02 if self.session_id == "ses_a" else 0)
        calls[self.session_id].append(command[-1])
        return ExecResult(
            stdout=f"{self.session_id}-out\n",
            stderr="",
            exit_code=0,
            duration_ms=1,
            timed_out=False,
        )

    monkeypatch.setattr(AsyncSession, "exec", fake_exec)

    def make_agent(session_id: str) -> tuple[Agent, object]:
        toolset = inis_toolset(
            session_id=session_id, api_key="tok_test", base_url="https://api.inis.run"
        )
        model = _one_shot_model(
            "execute_shell", {"command": f"echo {session_id}"}, f"done-{session_id}"
        )
        return Agent(model, toolsets=[toolset]), toolset

    agent_a, toolset_a = make_agent("ses_a")
    agent_b, toolset_b = make_agent("ses_b")

    result_a, result_b = await asyncio.gather(
        agent_a.run("go"),
        agent_b.run("go"),
    )

    assert result_a.output == "done-ses_a"
    assert result_b.output == "done-ses_b"
    assert calls == {"ses_a": ["echo ses_a"], "ses_b": ["echo ses_b"]}

    await toolset_a.aclose()
    await toolset_b.aclose()
