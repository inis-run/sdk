"""Deterministic tests for `inis_openai_agents.sandbox` using the REAL
adapter path (`InisSandboxSession`/`InisSandboxClient`, the real
`inis.AsyncSession`/`AsyncClient`, and a real `agents.sandbox.SandboxAgent`
+ `agents.Runner`) with a fake model and a fake inis.run HTTP backend
(`tests/fake_backend.py` — a real local subprocess + temp directory, not a
hand-simulated shell). Only the model and the network transport are faked;
none of `inis_openai_agents` itself is stubbed out.
"""

from __future__ import annotations

import asyncio
import io
from pathlib import Path

import pytest
from agents import Runner
from agents.run_config import RunConfig, SandboxRunConfig
from agents.sandbox import SandboxAgent

from inis_openai_agents.sandbox import InisSandboxClient, InisSandboxClientOptions, InisSandboxSession

from .fake_backend import LocalProcessInisBackend, attach_fake_session
from .fake_model import ScriptedModel, final_message, tool_call


async def _run_one_exec_turn(session_id: str, echo_word: str):
    inis_session, backend = attach_fake_session(session_id)
    sandbox_session = InisSandboxSession.wrap(inis_session)
    model = ScriptedModel(
        [
            tool_call("call_1", "exec_command", {"cmd": f"echo {echo_word}"}),
            final_message(f"done-{echo_word}"),
        ]
    )
    agent = SandboxAgent(name="tester", model=model)
    run_config = RunConfig(sandbox=SandboxRunConfig(session=sandbox_session))
    result = await Runner.run(agent, "run a command", run_config=run_config)
    return result, backend, inis_session


class TestExecViaRealAdapterPath:
    async def test_tool_call_reaches_real_session_exec(self):
        result, backend, inis_session = await _run_one_exec_turn("sess_exec_1", "hello-world")
        try:
            assert result.final_output == "done-hello-world"
            assert backend.exec_log == [["sh", "-lc", "echo hello-world"]]
            # SandboxRunConfig(session=...) is the attach/never-destroy path
            # (see runtime_session_manager._create_resources: owns_session
            # is False whenever RunConfig.sandbox.session is set) — the
            # runtime must not have destroyed our backend.
            assert backend.destroyed is False
        finally:
            await inis_session.aclose()
            backend.cleanup()

    async def test_exec_timeout_raises_not_hangs(self):
        # The sandbox ABC's exec() carries no abort handle, so a timeout is
        # the one way this adapter ever ends an in-flight command early.
        # (inis.run itself does have real cancellation, but only for named
        # background processes, which this contract has no way to reach.)
        # Confirm it actually does: the real fake backend
        # subprocess is killed by `_run`'s own `asyncio.wait_for`, and
        # `_exec_internal` turns `timed_out=True` into `ExecTimeoutError`
        # rather than returning a result or hanging.
        from agents.sandbox.errors import ExecTimeoutError

        inis_session, backend = attach_fake_session("sess_exec_timeout")
        sandbox_session = InisSandboxSession.wrap(inis_session)
        try:
            with pytest.raises(ExecTimeoutError):
                await sandbox_session.exec("sleep", "5", timeout=0.2)
        finally:
            await inis_session.aclose()
            backend.cleanup()

    async def test_truncated_stream_is_flagged_not_silently_dropped(self):
        # `agents.sandbox.types.ExecResult` has no `truncated` field at all
        # (just stdout/stderr/exit_code) -- see sandbox.py's `_exec_internal`
        # comment. Confirms the marker this adapter appends to stderr when
        # inis.run's own `ExecResult.truncated` fires actually reaches the
        # caller, instead of that signal just being dropped on the floor.
        inis_session, backend = attach_fake_session("sess_exec_truncated")
        sandbox_session = InisSandboxSession.wrap(inis_session)
        backend.force_truncated = True
        try:
            result = await sandbox_session.exec("echo", "hi")
            assert b"truncated" in result.stderr
        finally:
            await inis_session.aclose()
            backend.cleanup()

    async def test_truncation_marker_carries_a_fresh_nonce_each_call(self):
        # The marker is a static, publicly-readable string otherwise -- a
        # guest could just copy it out of this package's own source to fake
        # or mask a truncation signal. Confirm two truncated calls in the
        # same session produce two DIFFERENT marker strings, i.e. the nonce
        # is not itself a second static constant hiding behind the first.
        inis_session, backend = attach_fake_session("sess_exec_truncated_nonce")
        sandbox_session = InisSandboxSession.wrap(inis_session)
        backend.force_truncated = True
        try:
            # Same command both times so the only thing that can make the
            # two stderrs differ is the nonce, not the echoed content.
            first = await sandbox_session.exec("echo", "hi")
            second = await sandbox_session.exec("echo", "hi")
            assert first.stderr != second.stderr
        finally:
            await inis_session.aclose()
            backend.cleanup()

    async def test_nonzero_exit_is_reported_not_raised(self):
        inis_session, backend = attach_fake_session("sess_exec_nonzero")
        sandbox_session = InisSandboxSession.wrap(inis_session)
        model = ScriptedModel(
            [
                tool_call("call_1", "exec_command", {"cmd": "exit 7"}),
                final_message("saw-the-exit-code"),
            ]
        )
        agent = SandboxAgent(name="tester", model=model)
        run_config = RunConfig(sandbox=SandboxRunConfig(session=sandbox_session))
        try:
            result = await Runner.run(agent, "run a failing command", run_config=run_config)
            assert result.final_output == "saw-the-exit-code"
            # The tool result text the model saw must retain the real exit
            # code (`exec_command`'s formatted response includes
            # "Process exited with code N") — preserved documented meaning,
            # not swallowed.
            assert backend.exec_log == [["sh", "-lc", "exit 7"]]
        finally:
            await inis_session.aclose()
            backend.cleanup()


class TestFileReadWrite:
    async def test_write_then_read_round_trip(self):
        inis_session, backend = attach_fake_session("sess_files_1")
        sandbox_session = InisSandboxSession.wrap(inis_session)
        try:
            await sandbox_session.write(Path("/workspace/hello.txt"), io.BytesIO(b"hi there"))
            data = await sandbox_session.read(Path("/workspace/hello.txt"))
            assert data.read() == b"hi there"
        finally:
            await inis_session.aclose()
            backend.cleanup()

    async def test_read_missing_file_raises_not_found(self):
        from agents.sandbox.errors import WorkspaceReadNotFoundError

        inis_session, backend = attach_fake_session("sess_files_missing")
        sandbox_session = InisSandboxSession.wrap(inis_session)
        try:
            with pytest.raises(WorkspaceReadNotFoundError):
                await sandbox_session.read(Path("/workspace/does-not-exist.txt"))
        finally:
            await inis_session.aclose()
            backend.cleanup()


class TestPersistHydrate:
    async def test_persist_then_hydrate_into_a_new_session(self):
        src_session, src_backend = attach_fake_session("sess_persist_src")
        dst_session, dst_backend = attach_fake_session("sess_persist_dst")
        src = InisSandboxSession.wrap(src_session)
        dst = InisSandboxSession.wrap(dst_session)
        try:
            await src.write(Path("/workspace/data.txt"), io.BytesIO(b"payload-xyz"))
            archive = await src.persist_workspace()
            await dst.hydrate_workspace(archive)
            data = await dst.read(Path("/workspace/data.txt"))
            assert data.read() == b"payload-xyz"
        finally:
            await src_session.aclose()
            await dst_session.aclose()
            src_backend.cleanup()
            dst_backend.cleanup()


class TestOwnedLifecycle:
    async def test_create_then_delete_destroys_the_session(self, monkeypatch):
        # InisSandboxClient.create()/delete() go through a real
        # `inis.AsyncClient`; fake only its HTTP transport (session create +
        # the per-session backend), same real-adapter-path principle as
        # above.
        from .fake_client_backend import attach_fake_client

        client, backend_registry = attach_fake_client(monkeypatch)
        sandbox_client = InisSandboxClient(api_key="test-token")
        sandbox_client._client = client
        session = await sandbox_client.create(options=InisSandboxClientOptions())
        assert session.state.inis_session_id in backend_registry
        backend = backend_registry[session.state.inis_session_id]
        assert backend.destroyed is False
        await sandbox_client.delete(session)
        assert backend.destroyed is True
        backend.cleanup()


class TestConcurrentIsolation:
    """Runs two SandboxAgent conversations concurrently against two
    independent inis.run sessions and proves neither's tool calls or
    filesystem state cross over — two independent conversations must never
    share session state."""

    async def test_two_conversations_stay_isolated(self):
        (result_a, backend_a, session_a), (result_b, backend_b, session_b) = await asyncio.gather(
            _run_one_exec_turn("sess_iso_a", "marker-A"),
            _run_one_exec_turn("sess_iso_b", "marker-B"),
        )
        try:
            assert result_a.final_output == "done-marker-A"
            assert result_b.final_output == "done-marker-B"
            assert backend_a.exec_log == [["sh", "-lc", "echo marker-A"]]
            assert backend_b.exec_log == [["sh", "-lc", "echo marker-B"]]
            # Each backend must have seen exactly its own conversation's
            # command, never the other's.
            assert backend_a.session_id != backend_b.session_id
            assert all("marker-B" not in " ".join(cmd) for cmd in backend_a.exec_log)
            assert all("marker-A" not in " ".join(cmd) for cmd in backend_b.exec_log)
        finally:
            await session_a.aclose()
            await session_b.aclose()
            backend_a.cleanup()
            backend_b.cleanup()
