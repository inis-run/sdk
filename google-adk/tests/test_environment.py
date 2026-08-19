"""Tests for InisEnvironment / inis_adk_tools, driven against real
`google.adk` classes (`EnvironmentToolset`, `ExecuteTool`/`ReadFileTool`/
`WriteFileTool`/`EditFileTool`, `FunctionTool`, `Agent`,
`google.adk.runners.InMemoryRunner`), a real `inis.AsyncSession`/
`AsyncClient`, and `tests/fake_backend.LocalProcessInisBackend` -- a real
subprocess + real temp directory standing in for the guest VM. Only the
model (`tests/fake_model.ScriptedToolCallLlm`) and the network transport are
faked.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from google.adk.agents import Agent
from google.adk.models.llm_request import LlmRequest
from google.adk.tools.environment import EnvironmentToolset

from inis import AsyncSession, ConnectionSpec
from inis_google_adk import InisEnvironment, inis_adk_tools

from .fake_backend import attach_fake_client, attach_fake_session
from .fake_model import ScriptedToolCallLlm, final_text_of, run_one_turn


class TestOwnedLifecycle:
    async def test_initialize_then_close_destroys_the_session(self, monkeypatch):
        _, registry = attach_fake_client(monkeypatch)
        environment = InisEnvironment(base_url="https://fake.inis.test", api_key="test-token")

        await environment.initialize()
        assert environment.is_initialized is True
        session_id = environment._session.session_id
        assert session_id in registry
        assert registry[session_id].destroyed is False

        await environment.close()
        assert environment.is_initialized is False
        assert registry[session_id].destroyed is True

    async def test_close_is_idempotent(self, monkeypatch):
        attach_fake_client(monkeypatch)
        environment = InisEnvironment(base_url="https://fake.inis.test", api_key="test-token")
        await environment.initialize()
        await environment.close()
        await environment.close()  # must not raise

    async def test_initialize_is_idempotent(self, monkeypatch):
        _, registry = attach_fake_client(monkeypatch)
        environment = InisEnvironment(base_url="https://fake.inis.test", api_key="test-token")
        await environment.initialize()
        first_session_id = environment._session.session_id
        await environment.initialize()  # must not create a second session
        assert environment._session.session_id == first_session_id
        assert len(registry) == 1
        await environment.close()

    async def test_initialize_threads_connections_to_session_create(self, monkeypatch):
        # InisEnvironment(connections=...) must reach the POST /v1/sessions
        # payload the same way egress_default/egress_allow already do --
        # capture the create body directly rather than going through
        # fake_backend.attach_fake_client's dispatcher, which doesn't expose it.
        import inis.async_client as inis_async_client_module

        captured: dict[str, object] = {}

        async def dispatch(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/v1/sessions" and request.method == "POST":
                captured["json"] = json.loads(request.content)
                return httpx.Response(200, json={"session_id": "sess_conn_test", "state": "live"})
            return httpx.Response(204)

        class _FakeAsyncHttpxClient(httpx.AsyncClient):
            def __init__(self, *args, **kwargs):
                kwargs.pop("transport", None)
                super().__init__(*args, transport=httpx.MockTransport(dispatch), **kwargs)

        monkeypatch.setattr(inis_async_client_module.httpx, "AsyncClient", _FakeAsyncHttpxClient)

        spec = ConnectionSpec(
            name="stripe",
            origin="https://api.stripe.com",
            secret="sk_test_123",
            methods=["GET"],
            paths=["/v1/"],
        )
        environment = InisEnvironment(
            base_url="https://fake.inis.test",
            api_key="test-token",
            egress_default=None,
            connections=[spec],
        )
        await environment.initialize()

        assert captured["json"] == {
            "connections": [
                {
                    "name": "stripe",
                    "origin": "https://api.stripe.com",
                    "authentication": {"type": "bearer", "secret": "sk_test_123"},
                    "allow": {"methods": ["GET"], "paths": ["/v1/"]},
                }
            ]
        }


class TestAttachedLifecycle:
    async def test_wrap_never_destroys_the_session(self):
        session, backend = attach_fake_session("sess_wrap_1")
        environment = InisEnvironment.wrap(session)
        assert environment.is_initialized is True

        await environment.close()
        assert backend.destroyed is False
        await session.aclose()
        backend.cleanup()

    def test_wrap_rejects_a_session_with_no_id(self):
        session = AsyncSession(base_url="https://fake.inis.test", token="test-token")
        with pytest.raises(ValueError):
            InisEnvironment.wrap(session)


class TestExecute:
    async def test_stdout_stderr_exit_code(self):
        session, backend = attach_fake_session("sess_exec_1")
        environment = InisEnvironment.wrap(session)

        result = await environment.execute("echo out; echo err 1>&2; exit 3")
        assert result.stdout.strip() == "out"
        assert result.stderr.strip() == "err"
        assert result.exit_code == 3
        assert result.timed_out is False

        await session.aclose()
        backend.cleanup()

    async def test_timeout_reports_timed_out(self):
        session, backend = attach_fake_session("sess_exec_timeout")
        environment = InisEnvironment.wrap(session)

        result = await environment.execute("sleep 5", timeout=0.05)
        assert result.timed_out is True

        await session.aclose()
        backend.cleanup()

    async def test_truncated_flag_reaches_caller_via_result_not_stderr(self):
        # `ExecutionResult` has no `truncated` field at all, so
        # `InisExecutionResult`'s docstring in environment.py. Confirm the
        # signal still reaches the caller when inis.run's own
        # `ExecResult.truncated` fires, but now as a real attribute on the
        # returned `InisExecutionResult`, and that stderr comes back exactly
        # as the backend produced it (empty here) -- no marker appended.
        session, backend = attach_fake_session("sess_exec_trunc")
        environment = InisEnvironment.wrap(session)

        backend.force_truncated_once = True
        result = await environment.execute("echo hi")
        assert result.truncated is True
        assert result.stderr == ""

        await session.aclose()
        backend.cleanup()

    async def test_truncated_flag_false_when_backend_does_not_truncate(self):
        session, backend = attach_fake_session("sess_exec_not_trunc")
        environment = InisEnvironment.wrap(session)

        result = await environment.execute("echo hi")
        assert result.truncated is False

        await session.aclose()
        backend.cleanup()

    async def test_forged_marker_in_guest_output_is_not_believed(self):
        # The whole point here: verify the *replacement* for the
        # decorative nonce actually defends against what it claims to. Have
        # the guest (a real `echo` in the real subprocess this fake backend
        # shells out to) print text shaped exactly like the old in-stderr
        # marker -- complete with a plausible 16-hex-char nonce -- while the
        # backend's own `truncated` signal is NOT set. If truncation were
        # ever inferred from stdout/stderr content, this would come back
        # truncated=True; because the signal only ever comes from the
        # trusted, out-of-band `truncated` field on the session-API
        # response (never from stream bytes), a guest printing a forged
        # marker gets exactly nowhere.
        forged = "[inis: output truncated by the sandbox's own capture limit (nonce=deadbeefcafef00d)]"
        session, backend = attach_fake_session("sess_exec_forged_marker")
        environment = InisEnvironment.wrap(session)

        result = await environment.execute(f'echo "{forged}"')
        assert forged in result.stdout
        assert result.truncated is False

        await session.aclose()
        backend.cleanup()

    async def test_working_dir_raises_before_initialize(self):
        environment = InisEnvironment()
        with pytest.raises(RuntimeError):
            _ = environment.working_dir


class TestFiles:
    async def test_write_then_read_roundtrip(self):
        session, backend = attach_fake_session("sess_files_1")
        environment = InisEnvironment.wrap(session)

        await environment.write_file("/workspace/hello.txt", "hello world")
        data = await environment.read_file("/workspace/hello.txt")
        assert data == b"hello world"

        await session.aclose()
        backend.cleanup()

    async def test_read_missing_file_raises_file_not_found(self):
        session, backend = attach_fake_session("sess_files_2")
        environment = InisEnvironment.wrap(session)

        with pytest.raises(FileNotFoundError):
            await environment.read_file("/workspace/nope.txt")

        await session.aclose()
        backend.cleanup()

    async def test_relative_path_resolves_under_workspace(self):
        session, backend = attach_fake_session("sess_files_3")
        environment = InisEnvironment.wrap(session)

        await environment.write_file("relative.txt", "content")
        data = await environment.read_file("/workspace/relative.txt")
        assert data == b"content"

        await session.aclose()
        backend.cleanup()


class TestTwoSessionIsolation:
    async def test_two_environments_stay_isolated(self):
        session_a, backend_a = attach_fake_session("sess_iso_a")
        session_b, backend_b = attach_fake_session("sess_iso_b")
        env_a = InisEnvironment.wrap(session_a)
        env_b = InisEnvironment.wrap(session_b)

        await asyncio.gather(
            env_a.write_file("/workspace/marker.txt", "A"),
            env_b.write_file("/workspace/marker.txt", "B"),
        )
        data_a, data_b = await asyncio.gather(
            env_a.read_file("/workspace/marker.txt"),
            env_b.read_file("/workspace/marker.txt"),
        )
        assert data_a == b"A"
        assert data_b == b"B"

        await session_a.aclose()
        await session_b.aclose()
        backend_a.cleanup()
        backend_b.cleanup()


class TestEnvironmentToolset:
    async def test_get_tools_returns_the_standard_four(self):
        session, backend = attach_fake_session("sess_toolset_1")
        environment = InisEnvironment.wrap(session)
        toolset = EnvironmentToolset(environment=environment)

        tools = await toolset.get_tools()
        names = {t.name for t in tools}
        assert names == {"Execute", "ReadFile", "EditFile", "WriteFile"}

        await session.aclose()
        backend.cleanup()

    async def test_process_llm_request_injects_working_dir_instruction(self):
        session, backend = attach_fake_session("sess_toolset_2")
        environment = InisEnvironment.wrap(session)
        toolset = EnvironmentToolset(environment=environment)

        request = LlmRequest()
        await toolset.process_llm_request(tool_context=None, llm_request=request)
        assert "/workspace" in request.config.system_instruction

        await session.aclose()
        backend.cleanup()

    async def test_execute_tool_runs_a_real_command(self):
        session, backend = attach_fake_session("sess_toolset_3")
        environment = InisEnvironment.wrap(session)
        toolset = EnvironmentToolset(environment=environment)

        tools = await toolset.get_tools()
        execute_tool = next(t for t in tools if t.name == "Execute")
        result = await execute_tool.run_async(args={"command": "echo hi"}, tool_context=None)
        assert result["status"] == "ok"
        assert result["stdout"].strip() == "hi"

        await session.aclose()
        backend.cleanup()

    async def test_edit_file_tool_builds_on_read_write(self):
        session, backend = attach_fake_session("sess_toolset_4")
        environment = InisEnvironment.wrap(session)
        toolset = EnvironmentToolset(environment=environment)

        tools = await toolset.get_tools()
        write_tool = next(t for t in tools if t.name == "WriteFile")
        edit_tool = next(t for t in tools if t.name == "EditFile")
        read_tool = next(t for t in tools if t.name == "ReadFile")

        await write_tool.run_async(args={"path": "/workspace/f.txt", "content": "hello world"}, tool_context=None)
        edit_result = await edit_tool.run_async(
            args={"path": "/workspace/f.txt", "old_string": "world", "new_string": "there"}, tool_context=None
        )
        assert edit_result["status"] == "ok"
        read_result = await read_tool.run_async(args={"path": "/workspace/f.txt"}, tool_context=None)
        # ReadFileTool prefixes each line with a right-aligned line number
        # (cat -n style) -- verified in google/adk/tools/environment/
        # _read_file_tool.py, not assumed.
        assert read_result["content"].endswith("hello there")

        await session.aclose()
        backend.cleanup()

    async def test_toolset_close_is_idempotent_and_owned_only(self, monkeypatch):
        # Attached mode: toolset.close() must never destroy the session.
        session, backend = attach_fake_session("sess_toolset_close_attached")
        environment = InisEnvironment.wrap(session)
        toolset = EnvironmentToolset(environment=environment)
        await toolset.get_tools()  # triggers environment.initialize() lazily
        await toolset.close()
        await toolset.close()  # idempotent
        assert backend.destroyed is False
        await session.aclose()
        backend.cleanup()


class TestFrameworkPathViaRealAgent:
    """Drives a real google.adk.agents.Agent through google.adk.runners.
    InMemoryRunner and a scripted BaseLlm -- not just direct calls into this
    adapter's own code."""

    async def test_execute_tool_through_environment_toolset(self):
        session, backend = attach_fake_session("sess_agent_env")
        environment = InisEnvironment.wrap(session)
        toolset = EnvironmentToolset(environment=environment)

        llm = ScriptedToolCallLlm(tool_name="Execute", tool_args={"command": "echo from-agent"})
        agent = Agent(name="env_agent", model=llm, tools=[toolset])

        events = await run_one_turn(agent, message="run a command")
        assert final_text_of(events) == "done"

        await session.aclose()
        backend.cleanup()

    async def test_execute_via_fallback_function_tools(self):
        session, backend = attach_fake_session("sess_agent_fallback")
        fallback_tools = inis_adk_tools(session)

        llm = ScriptedToolCallLlm(tool_name="execute", tool_args={"command": "echo fallback"})
        agent = Agent(name="fallback_agent", model=llm, tools=fallback_tools)

        events = await run_one_turn(agent, message="run a command")
        assert final_text_of(events) == "done"

        await session.aclose()
        backend.cleanup()


class TestFallbackToolsRequireAnAttachedSession:
    def test_rejects_a_session_with_no_id(self):
        session = AsyncSession(base_url="https://fake.inis.test", token="test-token")
        with pytest.raises(ValueError):
            inis_adk_tools(session)


class TestFallbackToolsExecute:
    """inis_adk_tools()'s `execute` returns a single plain string -- the
    model-visible tool result itself, unlike `InisEnvironment.execute()`'s
    `ExecutionResult` (a structured object with a real `truncated` field as
    of). There is no separate trusted place on this path to move the
    truncation signal to, so it stays an inline marker; this change only removes
    the marker's per-call nonce, which used to imply the marker was
    cryptographically hard to spoof when nothing ever checked it (same
    honesty this path's `[inis: command timed out]`/`[inis: exited N]`
    annotations already had -- neither of those ever claimed to be
    unforgeable either). This path checked stdout/stderr/exit_code/
    timed_out but not ExecResult.truncated at all in the original PR (a
    real regression caught in review) -- these tests guard the fix, not
    the removed nonce."""

    async def test_truncated_output_gets_a_visible_marker(self):
        session, backend = attach_fake_session("sess_fallback_trunc")
        tools = inis_adk_tools(session)
        execute_tool = next(t for t in tools if t.name == "execute")

        backend.force_truncated_once = True
        result = await execute_tool.run_async(args={"command": "echo hi"}, tool_context=None)
        assert "output truncated by the sandbox's own capture limit" in result
        assert "nonce=" not in result

        await session.aclose()
        backend.cleanup()

    async def test_non_truncated_output_has_no_marker(self):
        session, backend = attach_fake_session("sess_fallback_no_trunc")
        tools = inis_adk_tools(session)
        execute_tool = next(t for t in tools if t.name == "execute")

        result = await execute_tool.run_async(args={"command": "echo hi"}, tool_context=None)
        assert "truncated" not in result

        await session.aclose()
        backend.cleanup()
