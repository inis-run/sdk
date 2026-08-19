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
import json
from pathlib import Path

import httpx
import pytest
from agents import Runner
from agents.run_config import RunConfig, SandboxRunConfig
from agents.sandbox import SandboxAgent
from agents.sandbox.errors import PtySessionNotFoundError

from inis import ConnectionSpec
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

    async def test_truncated_flag_reaches_caller_via_result_not_stderr(self):
        # `agents.sandbox.types.ExecResult` has no `truncated` field
        # at all (just stdout/stderr/exit_code) -- see `InisExecResult`'s
        # docstring in sandbox.py. Confirm the signal still reaches the
        # caller when inis.run's own `ExecResult.truncated` fires, but now
        # as a real attribute on the returned `InisExecResult`, and that
        # stderr comes back byte-for-byte as the backend produced it (no
        # marker text appended, nothing to strip back out).
        inis_session, backend = attach_fake_session("sess_exec_truncated")
        sandbox_session = InisSandboxSession.wrap(inis_session)
        backend.force_truncated = True
        try:
            result = await sandbox_session.exec("echo", "hi")
            assert result.truncated is True
            assert result.stderr == b""
            assert b"truncated" not in result.stdout
            assert b"nonce" not in result.stdout
        finally:
            await inis_session.aclose()
            backend.cleanup()

    async def test_truncated_flag_false_when_backend_does_not_truncate(self):
        # Sibling of the above: the flag must track the backend's real
        # signal in both directions, not just default-true or sticky-true
        # across calls.
        inis_session, backend = attach_fake_session("sess_exec_not_truncated")
        sandbox_session = InisSandboxSession.wrap(inis_session)
        try:
            result = await sandbox_session.exec("echo", "hi")
            assert result.truncated is False
        finally:
            await inis_session.aclose()
            backend.cleanup()

    async def test_forged_marker_in_guest_output_is_not_believed(self):
        # The whole point here: verify the *replacement* for the
        # decorative nonce actually defends against what it claims to.
        # Have the "guest" (the fake backend's real echo subprocess) print
        # text shaped exactly like the old in-stderr marker -- the format
        # string this package used to emit, complete with a plausible
        # 16-hex-char nonce -- while the backend's own `truncated` signal
        # is NOT set. If truncation were ever inferred from stderr/stdout
        # content, this would come back truncated=True; because the signal
        # only ever comes from the trusted out-of-band `truncated` field on
        # the session-API response (never from stream bytes), a guest
        # printing a forged marker gets exactly nowhere.
        forged = "[inis: output truncated by the sandbox's own capture limit (nonce=deadbeefcafef00d)]"
        inis_session, backend = attach_fake_session("sess_exec_forged_marker")
        sandbox_session = InisSandboxSession.wrap(inis_session)
        try:
            result = await sandbox_session.exec("echo", forged)
            assert forged.encode("utf-8") in result.stdout
            assert result.truncated is False
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

    async def test_create_threads_connections_to_session_create(self, monkeypatch):
        # InisSandboxClientOptions.connections must reach the POST
        # /v1/sessions payload the same way egress_default/egress_allow
        # already do -- capture the create body directly rather than going
        # through fake_client_backend's dispatcher, which doesn't expose it.
        import inis.async_client as inis_async_client_module

        captured: dict[str, object] = {}

        async def dispatch(request):
            if request.url.path == "/v1/sessions" and request.method == "POST":
                captured["json"] = json.loads(request.content)
                return httpx.Response(200, json={"session_id": "sess_conn_test", "state": "live"})
            return httpx.Response(204)

        class _FakeAsyncHttpxClient(httpx.AsyncClient):
            def __init__(self, *args, **kwargs):
                kwargs.pop("transport", None)
                super().__init__(*args, transport=httpx.MockTransport(dispatch), **kwargs)

        monkeypatch.setattr(inis_async_client_module.httpx, "AsyncClient", _FakeAsyncHttpxClient)

        sandbox_client = InisSandboxClient(api_key="test-token", base_url="https://fake.inis.test")
        spec = ConnectionSpec(
            name="stripe",
            origin="https://api.stripe.com",
            secret="sk_test_123",
            methods=["GET"],
            paths=["/v1/"],
        )
        options = InisSandboxClientOptions(egress_default=None, connections=(spec,))
        await sandbox_client.create(options=options)

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


class FakePTY:
    """Stands in for `inis.AsyncPTY` without opening a real WebSocket --
    `fake_backend.py`'s `httpx.MockTransport` only fakes the plain HTTP
    surface, so PTY (a separate WebSocket transport) needs its own double.
    Exposes exactly the surface `InisSandboxSession`'s PTY methods use:
    `write`/`close`/async iteration/`exit_code`.
    """

    def __init__(self) -> None:
        self._queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        self._exit_code: int | None = None
        self.written: list[str] = []
        self.closed = False

    def push(self, data: bytes) -> None:
        self._queue.put_nowait(data)

    def finish(self, exit_code: int) -> None:
        self._exit_code = exit_code
        self._queue.put_nowait(None)

    @property
    def exit_code(self) -> int | None:
        return self._exit_code

    async def write(self, data: bytes | str) -> None:
        self.written.append(data.decode() if isinstance(data, bytes) else data)

    async def close(self) -> None:
        self.closed = True
        self._queue.put_nowait(None)

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        while True:
            item = await self._queue.get()
            if item is None:
                return
            yield item


class TestPty:
    """`InisSandboxSession.pty_exec_start`/`pty_write_stdin` against a fake
    `AsyncPTY` (see `FakePTY` above) -- proves the adapter wires the ABC's
    PTY contract through to the core SDK's real `session.pty()` client
    (command, stdin, output collection, process-id lifecycle), without
    needing a real WebSocket server for a unit test.
    """

    async def test_supports_pty_is_true(self):
        inis_session, backend = attach_fake_session("sess_pty_supports")
        sandbox_session = InisSandboxSession.wrap(inis_session)
        try:
            assert sandbox_session.supports_pty() is True
        finally:
            await inis_session.aclose()
            backend.cleanup()

    async def test_pty_exec_start_reaches_session_pty_and_collects_output(self):
        inis_session, backend = attach_fake_session("sess_pty_exec")
        sandbox_session = InisSandboxSession.wrap(inis_session)
        fake_pty = FakePTY()
        seen_kwargs: dict = {}

        async def fake_open(**kwargs):
            seen_kwargs.update(kwargs)
            fake_pty.push(b"hello from pty\n")
            fake_pty.finish(0)
            return fake_pty

        inis_session.pty = fake_open
        try:
            update = await sandbox_session.pty_exec_start(
                "echo hello", shell=True, yield_time_s=1
            )
            assert seen_kwargs["command"] == ["sh", "-lc", "echo hello"]
            assert update.output == b"hello from pty\n"
            assert update.exit_code == 0
            # The process already exited within the yield window, so there's
            # nothing left to write_stdin to -- the ABC contract signals that
            # by handing back process_id=None rather than a dead id.
            assert update.process_id is None
        finally:
            await inis_session.aclose()
            backend.cleanup()

    async def test_pty_write_stdin_round_trips_to_a_still_running_session(self):
        inis_session, backend = attach_fake_session("sess_pty_stdin")
        sandbox_session = InisSandboxSession.wrap(inis_session)
        fake_pty = FakePTY()

        async def fake_open(**kwargs):
            fake_pty.push(b"$ ")
            return fake_pty

        inis_session.pty = fake_open
        try:
            started = await sandbox_session.pty_exec_start("bash", yield_time_s=0.2)
            assert started.process_id is not None
            assert started.exit_code is None

            fake_pty.push(b"ls\r\nfile.txt\r\n$ ")
            update = await sandbox_session.pty_write_stdin(
                session_id=started.process_id, chars="ls\n", yield_time_s=0.2
            )
            assert fake_pty.written == ["ls\n"]
            assert b"file.txt" in update.output
            assert update.process_id == started.process_id  # still alive

            fake_pty.finish(0)
            final = await sandbox_session.pty_write_stdin(
                session_id=started.process_id, chars="", yield_time_s=0.2
            )
            assert final.exit_code == 0
            assert final.process_id is None  # evicted on exit
        finally:
            await inis_session.aclose()
            backend.cleanup()

    async def test_write_stdin_to_unknown_session_id_raises(self):
        inis_session, backend = attach_fake_session("sess_pty_unknown")
        sandbox_session = InisSandboxSession.wrap(inis_session)
        try:
            with pytest.raises(PtySessionNotFoundError):
                await sandbox_session.pty_write_stdin(session_id=999999, chars="x")
        finally:
            await inis_session.aclose()
            backend.cleanup()

    async def test_pty_terminate_all_closes_open_sessions(self):
        inis_session, backend = attach_fake_session("sess_pty_terminate")
        sandbox_session = InisSandboxSession.wrap(inis_session)
        fake_pty = FakePTY()

        async def fake_open(**kwargs):
            fake_pty.push(b"$ ")
            return fake_pty

        inis_session.pty = fake_open
        try:
            started = await sandbox_session.pty_exec_start("bash", yield_time_s=0.2)
            assert started.process_id is not None
            await sandbox_session.pty_terminate_all()
            assert fake_pty.closed is True
            with pytest.raises(PtySessionNotFoundError):
                await sandbox_session.pty_write_stdin(session_id=started.process_id, chars="x")
        finally:
            await inis_session.aclose()
            backend.cleanup()
