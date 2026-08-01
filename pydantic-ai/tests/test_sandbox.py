"""Tests for InisSandbox: the pydantic-ai-harness-shaped capability. Runs a
REAL pydantic-ai Agent (Agent(capabilities=[InisSandbox(...)])) through a
deterministic FunctionModel, against a mocked inis.AsyncSession — the same
"real adapter path, fake model" bar as test_toolset.py."""

from __future__ import annotations

import asyncio

import pytest
from inis import ExecResult, InisError
from inis.async_client import AsyncSession
from inis_pydantic_ai.sandbox import InisSandbox
from pydantic_ai import Agent
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import FunctionModel

SANDBOX_TOOLS = {"run_command", "read_file", "write_file", "list_directory"}


def _one_shot_model(tool_name: str, args: dict, final_text: str) -> FunctionModel:
    step = {"n": 0}

    async def fake_model(messages, info):
        step["n"] += 1
        if step["n"] == 1:
            return ModelResponse(parts=[ToolCallPart(tool_name=tool_name, args=args)])
        return ModelResponse(parts=[TextPart(content=final_text)])

    return FunctionModel(fake_model)


def test_requires_exactly_one_of_session_id_or_session():
    with pytest.raises(ValueError, match="exactly one"):
        InisSandbox()
    with pytest.raises(ValueError, match="exactly one"):
        InisSandbox(
            session_id="ses_a",
            session=AsyncSession.attach(
                "ses_b", base_url="https://api.inis.run", token="tok"
            ),
        )


def test_api_key_rejected_with_injected_session():
    session = AsyncSession.attach("ses_a", base_url="https://api.inis.run", token="tok")
    with pytest.raises(ValueError, match="api_key"):
        InisSandbox(session=session, api_key="tok2")


def test_get_toolset_exposes_only_the_bounded_surface():
    sandbox = InisSandbox(
        session_id="ses_test123", api_key="tok_test", base_url="https://api.inis.run"
    )
    toolset = sandbox.get_toolset()
    assert set(toolset.tools) == SANDBOX_TOOLS


async def test_run_command_through_a_real_agent(monkeypatch):
    calls: list[list[str]] = []

    async def fake_exec(self, command, *, cwd=None, timeout_ms=None):
        calls.append(command)
        return ExecResult(
            stdout="hi\n", stderr="", exit_code=0, duration_ms=1, timed_out=False
        )

    monkeypatch.setattr(AsyncSession, "exec", fake_exec)

    sandbox = InisSandbox(
        session_id="ses_test123", api_key="tok_test", base_url="https://api.inis.run"
    )
    model = _one_shot_model("run_command", {"command": "echo hi"}, "done")
    agent = Agent(model, capabilities=[sandbox])

    result = await agent.run("run it")

    assert result.output == "done"
    assert calls == [["bash", "-lc", "echo hi"]]
    await sandbox.aclose()


async def test_run_command_nonzero_exit_is_reported_not_raised(monkeypatch):
    """Divergence from inis_toolset()'s execute_shell: this contract (matching
    pydantic-ai-harness's ModalSandbox) reports a non-zero exit in the
    returned text so the model can react, rather than raising."""

    async def fake_exec(self, command, *, cwd=None, timeout_ms=None):
        return ExecResult(
            stdout="partial\n", stderr="boom", exit_code=1, duration_ms=1, timed_out=False
        )

    monkeypatch.setattr(AsyncSession, "exec", fake_exec)

    sandbox = InisSandbox(
        session_id="ses_test123", api_key="tok_test", base_url="https://api.inis.run"
    )
    toolset = sandbox.get_toolset()
    output = await toolset.tools["run_command"].function(command="false")

    assert "[stdout]\npartial" in output
    assert "[stderr]\nboom" in output
    assert "[exit code: 1]" in output
    await sandbox.aclose()


async def test_retryable_error_becomes_model_retry_not_a_crash(monkeypatch):
    from pydantic_ai.exceptions import ModelRetry

    async def fake_exec(self, command, *, cwd=None, timeout_ms=None):
        raise InisError("session busy", code="session_not_live", retryable=True)

    monkeypatch.setattr(AsyncSession, "exec", fake_exec)

    sandbox = InisSandbox(
        session_id="ses_test123", api_key="tok_test", base_url="https://api.inis.run"
    )
    toolset = sandbox.get_toolset()
    with pytest.raises(ModelRetry):
        await toolset.tools["run_command"].function(command="echo hi")
    await sandbox.aclose()


async def test_terminal_error_propagates(monkeypatch):
    async def fake_exec(self, command, *, cwd=None, timeout_ms=None):
        raise InisError("session gone", code="session_ended", retryable=False)

    monkeypatch.setattr(AsyncSession, "exec", fake_exec)

    sandbox = InisSandbox(
        session_id="ses_test123", api_key="tok_test", base_url="https://api.inis.run"
    )
    toolset = sandbox.get_toolset()
    with pytest.raises(InisError, match="session gone"):
        await toolset.tools["run_command"].function(command="echo hi")
    await sandbox.aclose()


async def test_injected_session_is_never_attached_or_closed_by_aclose():
    """`session=` mode: the capability must not open its own attach, and
    aclose() is a no-op — the host owns that object outright."""
    session = AsyncSession.attach(
        "ses_injected", base_url="https://api.inis.run", token="tok"
    )
    sandbox = InisSandbox(session=session)
    assert sandbox._resolve_session() is session
    await sandbox.aclose()  # must not raise, must not touch `session`


async def test_two_concurrent_conversations_stay_isolated(monkeypatch):
    """Two concurrent conversations must prove isolation. Two
    InisSandbox instances, each bound to its own session, run concurrently
    with interleaved awaits — no call should land against the wrong
    session."""
    calls: dict[str, list[str]] = {"ses_a": [], "ses_b": []}

    async def fake_exec(self, command, *, cwd=None, timeout_ms=None):
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

    def make_agent(session_id: str):
        sandbox = InisSandbox(
            session_id=session_id, api_key="tok_test", base_url="https://api.inis.run"
        )
        model = _one_shot_model(
            "run_command", {"command": f"echo {session_id}"}, f"done-{session_id}"
        )
        return Agent(model, capabilities=[sandbox]), sandbox

    agent_a, sandbox_a = make_agent("ses_a")
    agent_b, sandbox_b = make_agent("ses_b")

    result_a, result_b = await asyncio.gather(agent_a.run("go"), agent_b.run("go"))

    assert result_a.output == "done-ses_a"
    assert result_b.output == "done-ses_b"
    assert calls == {"ses_a": ["echo ses_a"], "ses_b": ["echo ses_b"]}

    await sandbox_a.aclose()
    await sandbox_b.aclose()
