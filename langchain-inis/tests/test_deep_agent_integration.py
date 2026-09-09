"""Framework-level tests: `create_deep_agent(backend=InisSandboxBackend(...))`
against a deterministic scripted model (no third-party model API, no
network) — proves Deep Agents automatically receives and uses the expected
execution/filesystem tools with the real adapter path, and that two
concurrent conversations never share sandbox state.
"""

from __future__ import annotations

import asyncio
import uuid

from deepagents import create_deep_agent
from langchain_core.messages import HumanMessage, ToolMessage

from .fake_backend import make_fake_inis_sandbox_backend
from .fake_model import final_message, scripted_model, tool_call_message


def _session_id() -> str:
    return f"ses_test_{uuid.uuid4().hex[:12]}"


def _tool_messages(result: dict) -> list[ToolMessage]:
    return [m for m in result["messages"] if isinstance(m, ToolMessage)]


def test_deep_agent_auto_registers_execute_tool_against_real_backend() -> None:
    """A stock Deep Agents flow switches to the inis.run backend with no
    custom lifecycle tools in the model prompt — just `backend=...`."""
    backend, fake = make_fake_inis_sandbox_backend(_session_id())
    try:
        model = scripted_model(
            [
                tool_call_message("execute", {"command": "echo hello-from-inis-sandbox"}, call_id="call_1"),
                final_message("Ran it."),
            ]
        )
        agent = create_deep_agent(model=model, backend=backend)

        result = agent.invoke({"messages": [HumanMessage(content="run a command in the sandbox")]})

        tool_messages = _tool_messages(result)
        assert len(tool_messages) == 1
        assert tool_messages[0].name == "execute"
        assert "hello-from-inis-sandbox" in tool_messages[0].content
        # Confirms the fake HTTP backend really executed it (not a stub the
        # graph short-circuited) — the real /exec endpoint recorded the call.
        assert any(cmd[:2] == ["bash", "-lc"] for cmd in fake.exec_log)
    finally:
        backend.close()
        fake.cleanup()


def test_deep_agent_write_then_read_persists_within_one_backend() -> None:
    """File state persists across tool calls within one backend/session —
    the same real `write_file`/`read_file` tools deepagents registers for a
    `SandboxBackendProtocol` backend."""
    backend, fake = make_fake_inis_sandbox_backend(_session_id())
    try:
        write_path = f"{fake.workspace_root}/nonce.txt"
        model = scripted_model(
            [
                tool_call_message("write_file", {"file_path": write_path, "content": "the-nonce-value"}, call_id="c1"),
                tool_call_message("read_file", {"file_path": write_path}, call_id="c2"),
                final_message("Wrote and read it back."),
            ]
        )
        agent = create_deep_agent(model=model, backend=backend)

        result = agent.invoke({"messages": [HumanMessage(content="write then read a file")]})

        tool_messages = _tool_messages(result)
        assert any(m.name == "write_file" for m in tool_messages)
        read_messages = [m for m in tool_messages if m.name == "read_file"]
        assert read_messages, "expected a read_file ToolMessage"
        assert "the-nonce-value" in read_messages[0].content
    finally:
        backend.close()
        fake.cleanup()


def test_two_deep_agent_conversations_do_not_share_sandbox_state() -> None:
    """Two separate `create_deep_agent()` graphs, each with its OWN
    `InisSandboxBackend` bound to a different inis.run session, must never
    observe each other's sandbox state — the concrete failure mode a
    module-global session (rather than one backend per agent instance)
    would produce."""
    backend_a, fake_a = make_fake_inis_sandbox_backend(_session_id())
    backend_b, fake_b = make_fake_inis_sandbox_backend(_session_id())
    try:
        path_a = f"{fake_a.workspace_root}/who.txt"
        path_b = f"{fake_b.workspace_root}/who.txt"

        model_a = scripted_model(
            [
                tool_call_message("write_file", {"file_path": path_a, "content": "conversation-A"}, call_id="a1"),
                tool_call_message("read_file", {"file_path": path_a}, call_id="a2"),
                final_message("done A"),
            ]
        )
        model_b = scripted_model(
            [
                tool_call_message("write_file", {"file_path": path_b, "content": "conversation-B"}, call_id="b1"),
                tool_call_message("read_file", {"file_path": path_b}, call_id="b2"),
                final_message("done B"),
            ]
        )
        agent_a = create_deep_agent(model=model_a, backend=backend_a)
        agent_b = create_deep_agent(model=model_b, backend=backend_b)

        result_a = agent_a.invoke({"messages": [HumanMessage(content="go")]})
        result_b = agent_b.invoke({"messages": [HumanMessage(content="go")]})

        read_a = [m for m in _tool_messages(result_a) if m.name == "read_file"][0]
        read_b = [m for m in _tool_messages(result_b) if m.name == "read_file"][0]
        assert "conversation-A" in read_a.content
        assert "conversation-B" in read_b.content
        assert "conversation-B" not in read_a.content
        assert "conversation-A" not in read_b.content
    finally:
        backend_a.close()
        backend_b.close()
        fake_a.cleanup()
        fake_b.cleanup()


def test_two_deep_agent_conversations_run_concurrently_without_crossing() -> None:
    """Async twin, run genuinely concurrently — the shape two real LangGraph
    threads served by the same process would take."""

    async def run() -> None:
        backend_a, fake_a = make_fake_inis_sandbox_backend(_session_id())
        backend_b, fake_b = make_fake_inis_sandbox_backend(_session_id())
        try:
            path_a = f"{fake_a.workspace_root}/who.txt"
            path_b = f"{fake_b.workspace_root}/who.txt"
            model_a = scripted_model(
                [
                    tool_call_message("execute", {"command": f"echo A > {path_a} && cat {path_a}"}, call_id="a1"),
                    final_message("done A"),
                ]
            )
            model_b = scripted_model(
                [
                    tool_call_message("execute", {"command": f"echo B > {path_b} && cat {path_b}"}, call_id="b1"),
                    final_message("done B"),
                ]
            )
            agent_a = create_deep_agent(model=model_a, backend=backend_a)
            agent_b = create_deep_agent(model=model_b, backend=backend_b)

            result_a, result_b = await asyncio.gather(
                agent_a.ainvoke({"messages": [HumanMessage(content="go")]}),
                agent_b.ainvoke({"messages": [HumanMessage(content="go")]}),
            )

            exec_a = [m for m in _tool_messages(result_a) if m.name == "execute"][0]
            exec_b = [m for m in _tool_messages(result_b) if m.name == "execute"][0]
            assert "A" in exec_a.content and "B" not in exec_a.content
            assert "B" in exec_b.content and "A" not in exec_b.content
        finally:
            backend_a.close()
            backend_b.close()
            fake_a.cleanup()
            fake_b.cleanup()

    asyncio.run(run())
