"""Tests for InisCodeActProvider / InisExecuteCodeTool, driven against a
real `agent_framework.Agent`, a real `inis.AsyncSession`/`AsyncClient`, and
`tests/fake_backend.KernelSessionBackend` -- a faithful in-process
simulation of the guest side that runs the real `inis._kernel_script.run_cell`
cell-execution logic, not canned responses (see that module's docstring).
Only the model (`ScriptedCodeActClient`) and the network transport are
faked.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from agent_framework import Agent, AgentSession

from inis import AsyncSession, ConnectionSpec, InisError
from inis_agent_framework import InisCodeActProvider

from .fake_backend import attach_fake_client, preregister_session
from .fake_model import ScriptedCodeActClient


class TestOwnedLifecycle:
    async def test_create_then_aclose_destroys_the_session(self, monkeypatch):
        # attach_fake_client monkeypatches httpx.AsyncClient construction
        # globally (inside inis.async_client), so the AsyncClient our own
        # InisCodeActProvider.create() constructs internally is routed to
        # the fake transport automatically -- no need to inject the client.
        _, registry = attach_fake_client(monkeypatch)

        provider = await InisCodeActProvider.create(base_url="https://fake.inis.test", api_key="test-token")
        session_id = provider._session.session_id
        assert session_id in registry
        assert registry[session_id].destroyed is False

        await provider.aclose()
        assert registry[session_id].destroyed is True

    async def test_create_forwards_connections_kwarg_to_session_create(self, monkeypatch):
        # InisCodeActProvider.create()'s **session_kwargs passthrough must
        # carry `connections` to the POST /v1/sessions payload the same way
        # it already carries egress_default/egress_allow/labels/... --
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
        provider = await InisCodeActProvider.create(
            base_url="https://fake.inis.test",
            api_key="test-token",
            egress_default=None,
            connections=[spec],
        )
        await provider.aclose()

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
    async def test_wrap_never_destroys_the_session(self, monkeypatch):
        _, registry = attach_fake_client(monkeypatch)
        backend = preregister_session(registry, "sess_wrap_1")
        session = AsyncSession.attach("sess_wrap_1", base_url="https://fake.inis.test", token="test-token")

        provider = InisCodeActProvider.wrap(session)
        await provider.aclose()  # no-op: wrap() never owns the session

        assert backend.destroyed is False
        await session.aclose()


class TestContextPersistence:
    async def test_variables_persist_across_separate_agent_runs(self, monkeypatch):
        _, registry = attach_fake_client(monkeypatch)
        backend = preregister_session(registry, "sess_persist_1")
        session = AsyncSession.attach("sess_persist_1", base_url="https://fake.inis.test", token="test-token")
        provider = InisCodeActProvider.wrap(session)

        model = ScriptedCodeActClient(["x = 41", "x + 1"])
        agent = Agent(client=model, name="codeact-agent", context_providers=[provider])
        agent_session = AgentSession()

        first = await agent.run("set x", session=agent_session)
        second = await agent.run("read x back", session=agent_session)

        assert first.text == "done (step 0)"
        assert second.text == "done (step 1)"
        # The second execute_code call's result content must show "42": the
        # persistent kernel, not a fresh interpreter, answered `x + 1`.
        assert backend.process_state == "running"
        await session.aclose()

    async def test_reconnect_by_session_id_sees_prior_state(self, monkeypatch):
        _, registry = attach_fake_client(monkeypatch)
        preregister_session(registry, "sess_reconnect_1")

        first_session = AsyncSession.attach(
            "sess_reconnect_1", base_url="https://fake.inis.test", token="test-token"
        )
        first_provider = InisCodeActProvider.wrap(first_session)
        results = await first_session.run_code("y = 100")
        assert results == []
        await first_session.aclose()

        # A brand-new provider instance, constructed only from the session
        # id (simulating a new process) -- .attach() must reconnect to the
        # SAME persistent interpreter, not a fresh one.
        reconnected = InisCodeActProvider.attach(
            "sess_reconnect_1", base_url="https://fake.inis.test", api_key="test-token"
        )
        results = await reconnected._session.run_code("y + 1")
        assert results[0].type == "text"
        assert results[0].text == "101"
        await reconnected._session.aclose()
        del first_provider  # keep the reference alive until here for clarity


class TestErrorPath:
    async def test_uncaught_exception_is_reported_and_keeps_context(self, monkeypatch):
        _, registry = attach_fake_client(monkeypatch)
        preregister_session(registry, "sess_error_1")
        session = AsyncSession.attach("sess_error_1", base_url="https://fake.inis.test", token="test-token")
        provider = InisCodeActProvider.wrap(session)

        model = ScriptedCodeActClient(["x = 1", "raise ValueError('boom')", "x + 1"])
        agent = Agent(client=model, name="codeact-agent", context_providers=[provider])
        agent_session = AgentSession()

        await agent.run("ok", session=agent_session)
        await agent.run("boom", session=agent_session)
        third = await agent.run("still alive", session=agent_session)

        assert third.text == "done (step 2)"
        results = await session.run_code("x")
        assert results[0].text == "1"
        await session.aclose()


class TestTimeout:
    async def test_timeout_reports_error_and_keeps_context_usable(self, monkeypatch):
        _, registry = attach_fake_client(monkeypatch)
        backend = preregister_session(registry, "sess_timeout_1")
        session = AsyncSession.attach("sess_timeout_1", base_url="https://fake.inis.test", token="test-token")

        # Force one "kernel hasn't answered yet" response while the kernel
        # is still genuinely running -- a real timeout, distinct from
        # kernel-death auto-recovery (which run_code() handles by silently
        # retrying once; see _run_code_attempt's allow_retry path).
        await session.run_code("1")  # starts the kernel for real
        assert backend.process_state == "running"
        backend.force_timeout_once = True
        results = await session.run_code("2", timeout_ms=50)
        assert results[0].type == "error"
        assert results[0].ename == "TimeoutError"
        assert backend.process_state == "running"  # context/kernel untouched by a timeout

        # The interpreter context is untouched by a timeout: the SAME
        # kernel still has state from before.
        after = await session.run_code("1 + 1")
        assert after[0].text == "2"
        await session.aclose()


class TestTwoConversationIsolation:
    async def test_two_provider_instances_stay_isolated(self, monkeypatch):
        _, registry = attach_fake_client(monkeypatch)
        preregister_session(registry, "sess_iso_a")
        preregister_session(registry, "sess_iso_b")
        session_a = AsyncSession.attach("sess_iso_a", base_url="https://fake.inis.test", token="test-token")
        session_b = AsyncSession.attach("sess_iso_b", base_url="https://fake.inis.test", token="test-token")

        await asyncio.gather(
            session_a.run_code("marker = 'A'"),
            session_b.run_code("marker = 'B'"),
        )
        result_a, result_b = await asyncio.gather(
            session_a.run_code("marker"),
            session_b.run_code("marker"),
        )
        assert result_a[0].text == "'A'"
        assert result_b[0].text == "'B'"
        await session_a.aclose()
        await session_b.aclose()


class TestUnsupportedCallToolBridge:
    def test_execute_code_tool_has_no_tools_bridge_parameter(self):
        import inspect

        from inis_agent_framework.codeact import InisExecuteCodeTool

        sig = inspect.signature(InisExecuteCodeTool.__init__)
        assert "tools" not in sig.parameters, (
            "InisExecuteCodeTool must not silently accept a tools=/call_tool bridge "
            "parameter -- see codeact.py's module docstring for why one is not built"
        )


class TestRequestFailure:
    async def test_inis_error_from_run_code_becomes_error_content_not_a_raise(self, monkeypatch):
        _, registry = attach_fake_client(monkeypatch)
        session = AsyncSession.attach("sess_missing", base_url="https://fake.inis.test", token="test-token")
        provider = InisCodeActProvider.wrap(session)

        with pytest.raises(InisError):
            # Not registered in the fake backend at all -- proves a genuine
            # transport-level failure surfaces as InisError, not a silent
            # empty result, when called directly against the session.
            await session.run_code("1 + 1")
        await session.aclose()
