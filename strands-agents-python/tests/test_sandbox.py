"""Tests for InisSandbox.

Uses a real subprocess + real temp directory behind an httpx.MockTransport
(fake_backend.LocalProcessInisBackend) for exec/file behavior, and canned SSE
text (fake_backend.canned_sse_transport) for testing this adapter's own
event-mapping logic (timeout, error frames) in isolation -- see
fake_backend.py's module docstring for why the split.
"""

from __future__ import annotations

import asyncio
import base64
import json

import httpx
import pytest
from fake_backend import RecordingTransport, attach_fake_session, canned_sse_transport
from fake_model import ScriptedToolCallModel
from strands import Agent
from strands.sandbox import ExecutionResult, FileInfo, StreamChunk
from strands.sandbox.errors import SandboxPathNotFoundError, SandboxTimeoutError

from inis import AsyncSession, ConnectionSpec
from inis_strands_agents import InisSandbox


async def _collect(gen):
    return [item async for item in gen]


class TestWrap:
    async def test_wrap_requires_session_id(self):
        unattached = AsyncSession(base_url="https://fake.inis.test", token="t")
        with pytest.raises(ValueError):
            InisSandbox.wrap(unattached)

    async def test_wrap_aclose_is_a_noop(self):
        session, backend = attach_fake_session()
        sandbox = InisSandbox.wrap(session)
        await sandbox.aclose()
        assert backend.destroyed is False
        await session.aclose()
        backend.cleanup()


class TestExecuteStreaming:
    async def test_stdout_stderr_order_and_final_result(self):
        session, backend = attach_fake_session()
        sandbox = InisSandbox.wrap(session)
        try:
            events = await _collect(
                sandbox.execute_streaming("echo out1; echo err1 1>&2; exit 3")
            )
            chunks = [e for e in events if isinstance(e, StreamChunk)]
            result = events[-1]
            assert isinstance(result, ExecutionResult)
            assert result.exit_code == 3
            assert "out1" in result.stdout
            assert "err1" in result.stderr
            assert any(c.stream_type == "stdout" and "out1" in c.data for c in chunks)
            assert any(c.stream_type == "stderr" and "err1" in c.data for c in chunks)
        finally:
            await session.aclose()
            backend.cleanup()

    async def test_env_is_applied_via_export_prefix(self):
        session, backend = attach_fake_session()
        recording = RecordingTransport(backend.handler)
        session._client = httpx.AsyncClient(transport=recording)
        sandbox = InisSandbox.wrap(session)
        try:
            events = await _collect(
                sandbox.execute_streaming("echo $GREETING", env={"GREETING": "hello world"})
            )
            result = events[-1]
            assert isinstance(result, ExecutionResult)
            assert result.exit_code == 0
            assert "hello world" in result.stdout

            exec_requests = [r for r in recording.requests if r.url.path.endswith("/exec")]
            body = json.loads(exec_requests[0].content)
            sent_command = body["command"][-1]
            assert sent_command.startswith("export GREETING='hello world' && ")
        finally:
            await session.aclose()
            backend.cleanup()

    async def test_invalid_env_key_rejected_before_any_request(self):
        session, backend = attach_fake_session()
        sandbox = InisSandbox.wrap(session)
        try:
            with pytest.raises(ValueError):
                await _collect(sandbox.execute_streaming("echo hi", env={"1BAD": "x"}))
        finally:
            await session.aclose()
            backend.cleanup()

    async def test_timeout_raises_sandbox_timeout_error(self):
        sse = 'event: exit\ndata: {"exit_code":-1,"timed_out":true,"duration_ms":5000}\n\n'
        session = AsyncSession.attach("sess-timeout", base_url="https://fake.inis.test", token="t")
        session._client = httpx.AsyncClient(transport=canned_sse_transport(sse))
        sandbox = InisSandbox.wrap(session)
        try:
            with pytest.raises(SandboxTimeoutError):
                await _collect(sandbox.execute_streaming("sleep 999", timeout=5))
        finally:
            await session.aclose()

    async def test_guest_error_frame_raises_runtime_error(self):
        sse = "event: error\ndata: guest agent unreachable\n\n"
        session = AsyncSession.attach("sess-error", base_url="https://fake.inis.test", token="t")
        session._client = httpx.AsyncClient(transport=canned_sse_transport(sse))
        sandbox = InisSandbox.wrap(session)
        try:
            with pytest.raises(RuntimeError, match="guest agent unreachable"):
                await _collect(sandbox.execute_streaming("echo hi"))
        finally:
            await session.aclose()

    async def test_cancellation_kills_the_remote_process(self):
        """Proves cancelling the consuming task genuinely kills the remote
        command -- not just abandons the local async generator."""
        session, backend = attach_fake_session()
        sandbox = InisSandbox.wrap(session)
        try:
            marker = backend._tmp / "workspace" / "marker"
            command = f"sleep 5 && touch {marker}"

            async def consume_one_then_hang():
                async for _ in sandbox.execute_streaming(command):
                    return  # stop after the first chunk, abandoning the generator

            task = asyncio.ensure_future(consume_one_then_hang())
            await asyncio.sleep(0.2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

            # Give the fake backend's cleanup a moment to actually kill the process.
            await asyncio.sleep(0.3)
            assert backend.last_proc is not None
            assert backend.last_proc.returncode is not None  # process was reaped, not left running
            assert not marker.exists()  # the sleep never completed
        finally:
            await session.aclose()
            backend.cleanup()


class TestExecuteCodeStreaming:
    async def test_runs_python_via_base64_heredoc(self):
        session, backend = attach_fake_session()
        sandbox = InisSandbox.wrap(session)
        try:
            events = await _collect(sandbox.execute_code_streaming("print(1 + 1)", "python3"))
            result = events[-1]
            assert isinstance(result, ExecutionResult)
            assert result.exit_code == 0
            assert result.stdout.strip() == "2"
        finally:
            await session.aclose()
            backend.cleanup()

    async def test_rejects_invalid_language(self):
        session, backend = attach_fake_session()
        sandbox = InisSandbox.wrap(session)
        try:
            with pytest.raises(ValueError):
                await _collect(sandbox.execute_code_streaming("1", "python3; rm -rf /"))
        finally:
            await session.aclose()
            backend.cleanup()


class TestFiles:
    async def test_write_read_roundtrip_binary(self):
        session, backend = attach_fake_session()
        sandbox = InisSandbox.wrap(session)
        try:
            raw = bytes(range(256))
            await sandbox.write_file("/workspace/bin.dat", raw)
            back = await sandbox.read_file("/workspace/bin.dat")
            assert back == raw
        finally:
            await session.aclose()
            backend.cleanup()

    async def test_read_text_write_text_convenience(self):
        session, backend = attach_fake_session()
        sandbox = InisSandbox.wrap(session)
        try:
            await sandbox.write_text("/workspace/hello.txt", "hi there")
            assert await sandbox.read_text("/workspace/hello.txt") == "hi there"
        finally:
            await session.aclose()
            backend.cleanup()

    async def test_read_missing_file_raises_file_not_found(self):
        session, backend = attach_fake_session()
        sandbox = InisSandbox.wrap(session)
        try:
            with pytest.raises(FileNotFoundError):
                await sandbox.read_file("/workspace/nope.txt")
        finally:
            await session.aclose()
            backend.cleanup()

    async def test_remove_missing_file_raises_file_not_found(self):
        session, backend = attach_fake_session()
        sandbox = InisSandbox.wrap(session)
        try:
            with pytest.raises(FileNotFoundError):
                await sandbox.remove_file("/workspace/nope.txt")
        finally:
            await session.aclose()
            backend.cleanup()

    async def test_list_files_marks_directories(self):
        session, backend = attach_fake_session()
        sandbox = InisSandbox.wrap(session)
        try:
            await sandbox.write_text("/workspace/a.txt", "a")
            (backend._tmp / "workspace" / "subdir").mkdir()
            entries = await sandbox.list_files("/workspace")
            by_name = {e.name: e for e in entries}
            assert by_name["a.txt"] == FileInfo(name="a.txt", is_dir=False)
            assert by_name["subdir"].is_dir is True
        finally:
            await session.aclose()
            backend.cleanup()

    async def test_list_files_missing_dir_raises_sandbox_path_not_found(self):
        session, backend = attach_fake_session()
        sandbox = InisSandbox.wrap(session)
        try:
            with pytest.raises(SandboxPathNotFoundError):
                await sandbox.list_files("/workspace/does-not-exist")
        finally:
            await session.aclose()
            backend.cleanup()

    async def test_remove_file_actually_removes_it(self):
        session, backend = attach_fake_session()
        sandbox = InisSandbox.wrap(session)
        try:
            await sandbox.write_text("/workspace/temp.txt", "x")
            await sandbox.remove_file("/workspace/temp.txt")
            with pytest.raises(FileNotFoundError):
                await sandbox.read_file("/workspace/temp.txt")
        finally:
            await session.aclose()
            backend.cleanup()


class TestGetTools:
    async def test_vends_bash_and_file_editor_only(self):
        session, backend = attach_fake_session()
        sandbox = InisSandbox.wrap(session)
        try:
            tools = sandbox.get_tools()
            names = sorted(t.tool_name for t in tools)
            assert names == ["sandbox_bash", "sandbox_file_editor"]
        finally:
            await session.aclose()
            backend.cleanup()


class TestOwnedLifecycle:
    async def test_create_destroys_on_aclose(self):
        session, backend = attach_fake_session("sess-owned-1")

        class _FakeSessions:
            async def create(self, **kwargs):
                return session

        class _FakeClient:
            sessions = _FakeSessions()

            async def close(self):
                pass

        sandbox = await InisSandbox.create(client=_FakeClient())
        assert sandbox._owned is True
        await sandbox.aclose()
        assert backend.destroyed is True
        backend.cleanup()

    async def test_create_forwards_connections_kwarg_to_sessions_create(self):
        # InisSandbox.create()'s **session_kwargs passthrough must carry
        # `connections` to AsyncClient.sessions.create(...) the same way it
        # already carries egress_default/egress_allow/labels/... -- no
        # adapter-side handling needed, just proof the forwarding reaches it.
        session, backend = attach_fake_session("sess-owned-conn")
        captured: dict[str, object] = {}

        class _FakeSessions:
            async def create(self, **kwargs):
                captured.update(kwargs)
                return session

        class _FakeClient:
            sessions = _FakeSessions()

            async def close(self):
                pass

        spec = ConnectionSpec(
            name="stripe",
            origin="https://api.stripe.com",
            secret="sk_test_123",
            methods=["GET"],
            paths=["/v1/"],
        )
        sandbox = await InisSandbox.create(client=_FakeClient(), connections=[spec])
        try:
            assert captured["connections"] == [spec]
        finally:
            await sandbox.aclose()
            backend.cleanup()


class TestAgentIntegration:
    """Deterministic Agent-level tests: a real Agent, a real InisSandbox, a
    real (fake-backed) inis session, and a scripted Model that calls the
    framework-vended tool exactly once."""

    async def test_agent_runs_sandbox_bash_tool(self):
        session, backend = attach_fake_session()
        sandbox = InisSandbox.wrap(session)
        try:
            model = ScriptedToolCallModel(
                tool_name="sandbox_bash",
                tool_input={"command": "echo hello-from-inis"},
                final_text="ran it",
            )
            agent = Agent(model=model, sandbox=sandbox)
            result = await agent.invoke_async("run a command")

            assert "ran it" in str(result)
            # The tool result actually round-tripped the real subprocess's stdout.
            tool_result_texts = [
                item["text"]
                for message in agent.messages
                for block in message.get("content", [])
                if "toolResult" in block
                for item in block["toolResult"]["content"]
                if "text" in item
            ]
            assert any("hello-from-inis" in t for t in tool_result_texts)
        finally:
            await session.aclose()
            backend.cleanup()

    async def test_agent_runs_sandbox_file_editor_tool(self):
        session, backend = attach_fake_session()
        sandbox = InisSandbox.wrap(session)
        try:
            model = ScriptedToolCallModel(
                tool_name="sandbox_file_editor",
                tool_input={"command": "create", "path": "/workspace/note.txt", "file_text": "hello"},
                final_text="created it",
            )
            agent = Agent(model=model, sandbox=sandbox)
            result = await agent.invoke_async("create a file")

            assert "created it" in str(result)
            assert (backend._tmp / "workspace" / "note.txt").read_text() == "hello"
        finally:
            await session.aclose()
            backend.cleanup()
