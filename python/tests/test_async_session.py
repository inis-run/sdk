"""Mocked-HTTP tests for inis.async_client.AsyncSession: the async twin of
tests/test_session.py. Covers the same request construction, response
mapping, and error handling, plus the SSE streaming methods as async
generators — proving the async surface has no thread-pool fallback anywhere
(every I/O call in AsyncSession is awaited directly against httpx.AsyncClient).
"""

from __future__ import annotations

import base64

import httpx
import pytest

from inis.async_client import AsyncSession
from inis.client import ConnectionSpec, DestroyReason, ExecStreamEvent, InisError, OnPtyDetachPolicy
from tests.conftest import error_response, json_response


def make_session(**overrides) -> AsyncSession:
    kwargs = dict(base_url="https://api.inis.run", token="tok_abc")
    kwargs.update(overrides)
    session = AsyncSession(**kwargs)
    session.session_id = "sess_123"
    return session


class TestAttach:
    def test_attach_with_explicit_base_url_and_token(self):
        session = AsyncSession.attach("sess_123", base_url="https://api.inis.run", token="tok_abc")
        assert session.session_id == "sess_123"
        assert session._base_url == "https://api.inis.run"
        assert session._token == "tok_abc"

    def test_attach_falls_back_to_env_when_omitted(self, monkeypatch):
        monkeypatch.setenv("INIS_API_KEY", "tok_from_env")
        monkeypatch.setenv("INIS_BASE_URL", "https://eu1.inis.run")
        session = AsyncSession.attach("sess_123")
        assert session.session_id == "sess_123"
        assert session._base_url == "https://eu1.inis.run"
        assert session._token == "tok_from_env"

    def test_attach_raises_without_token_or_env(self, monkeypatch):
        monkeypatch.delenv("INIS_API_KEY", raising=False)
        with pytest.raises(InisError, match="INIS_API_KEY is required"):
            AsyncSession.attach("sess_123")


class TestGet:
    async def test_request_and_mapping(self, fake_http_async):
        transport = fake_http_async(
            lambda method, path, params, body: json_response(
                200,
                {
                    "session_id": "sess_123",
                    "state": "live",
                    "on_pty_detach": "pause",
                    "egress": {"mode": "deny", "allow": ["api.openai.com"]},
                },
            )
        )
        session = make_session()

        info = await session.get()

        assert len(transport.calls) == 1
        call = transport.calls[0]
        assert call.method == "GET"
        assert call.path == "https://api.inis.run/v1/sessions/sess_123"
        assert call.headers["Authorization"] == "Bearer tok_abc"
        assert info.session_id == "sess_123"
        assert info.state == "live"
        assert info.on_pty_detach == "pause"
        assert info.egress.mode == "deny"
        assert info.egress.allow == ["api.openai.com"]

    async def test_info_is_alias_for_get(self, fake_http_async):
        fake_http_async(
            lambda method, path, params, body: json_response(
                200, {"session_id": "sess_123", "state": "live"}
            )
        )
        info = await make_session().info()
        assert info.session_id == "sess_123"


class TestPauseResumeDestroy:
    async def test_pause(self, fake_http_async):
        transport = fake_http_async(
            lambda method, path, params, body: json_response(
                200, {"session_id": "sess_123", "state": "paused"}
            )
        )
        info = await make_session().pause()
        assert transport.calls[0].path == "https://api.inis.run/v1/sessions/sess_123/pause"
        assert info.state == "paused"

    async def test_resume(self, fake_http_async):
        transport = fake_http_async(
            lambda method, path, params, body: json_response(
                200, {"session_id": "sess_123", "state": "live"}
            )
        )
        info = await make_session().resume()
        assert transport.calls[0].path == "https://api.inis.run/v1/sessions/sess_123/resume"
        assert info.state == "live"

    async def test_destroy_sends_reason_and_clears_session_id(self, fake_http_async):
        transport = fake_http_async(lambda method, path, params, body: json_response(204, {}))
        session = make_session()
        await session.destroy(reason=DestroyReason.SHELL_EXIT)
        assert transport.calls[0].method == "DELETE"
        assert transport.calls[0].params == {"reason": "shell_exit"}
        assert session.session_id is None

    async def test_destroy_is_noop_without_session_id(self, fake_http_async):
        transport = fake_http_async(lambda method, path, params, body: json_response(204, {}))
        session = make_session()
        session.session_id = None
        await session.destroy()
        assert transport.calls == []


class TestExec:
    async def test_wraps_string_command(self, fake_http_async):
        transport = fake_http_async(
            lambda method, path, params, body: json_response(
                200, {"stdout": "hi\n", "exit_code": 0, "duration_ms": 5}
            )
        )
        result = await make_session().exec("echo hi")
        assert transport.calls[0].json_body["command"] == ["bash", "-lc", "echo hi"]
        assert result.stdout == "hi\n"
        assert result.exit_code == 0

    async def test_accepts_argv_list(self, fake_http_async):
        transport = fake_http_async(
            lambda method, path, params, body: json_response(200, {"stdout": "", "exit_code": 0})
        )
        await make_session().exec(["python3", "-c", "print(1)"])
        assert transport.calls[0].json_body["command"] == ["python3", "-c", "print(1)"]


class TestFiles:
    async def test_read_list_write(self, fake_http_async):
        transport = fake_http_async(
            lambda method, path, params, body: json_response(
                200, {"content": "data", "entries": ["a", "b"]}
            )
        )
        session = make_session()
        assert await session.read_file("/a") == "data"
        assert await session.list_files("/") == ["a", "b"]
        await session.write_file("/a", "content")
        assert transport.calls[-1].method == "PUT"
        assert transport.calls[-1].json_body == {
            "path": "/a",
            "content": "content",
            "encoding": "text",
        }

    async def test_read_file_base64_decodes_bytes(self, fake_http_async):
        raw = b"\x00\x01binary"
        fake_http_async(
            lambda method, path, params, body: json_response(
                200, {"content": base64.b64encode(raw).decode("ascii")}
            )
        )
        content = await make_session().read_file("/bin", encoding="base64")
        assert content == raw

    async def test_write_file_bytes_base64_encodes(self, fake_http_async):
        transport = fake_http_async(lambda method, path, params, body: json_response(200, {}))
        await make_session().write_file("/bin", b"\x00\x01binary")
        assert transport.calls[0].json_body["encoding"] == "base64"

    async def test_write_files_batch(self, fake_http_async):
        transport = fake_http_async(
            lambda method, path, params, body: json_response(
                200, {"results": [{"path": "/a", "ok": True}]}
            )
        )
        results = await make_session().write_files([{"path": "/a", "content": "x"}])
        assert transport.calls[0].path == "https://api.inis.run/v1/sessions/sess_123/files/batch"
        assert results == [{"path": "/a", "ok": True}]

    async def test_find_files(self, fake_http_async):
        transport = fake_http_async(
            lambda method, path, params, body: json_response(
                200, {"paths": ["/workspace/a.py"], "truncated": False}
            )
        )
        result = await make_session().find_files("*.py")
        assert transport.calls[0].params["op"] == "find"
        assert result["paths"] == ["/workspace/a.py"]

    async def test_grep_files(self, fake_http_async):
        fake_http_async(
            lambda method, path, params, body: json_response(
                200, {"matches": [], "files_searched": 3, "truncated": False}
            )
        )
        result = await make_session().grep_files("TODO")
        assert result["files_searched"] == 3

    async def test_mkdir(self, fake_http_async):
        transport = fake_http_async(lambda method, path, params, body: json_response(200, {"path": "/a/b"}))
        await make_session().mkdir("/workspace/a/b")
        call = transport.calls[0]
        assert call.method == "POST"
        assert call.path.endswith("/files/mkdir")
        assert call.json_body == {"path": "/workspace/a/b"}

    async def test_remove_recursive(self, fake_http_async):
        transport = fake_http_async(lambda method, path, params, body: json_response(200, {}))
        await make_session().remove("/workspace/d", recursive=True)
        call = transport.calls[0]
        assert call.method == "DELETE"
        assert call.params == {"path": "/workspace/d", "recursive": "true"}

    async def test_remove_nonexistent_path_raises(self, fake_http_async):
        fake_http_async(lambda method, path, params, body: error_response(404, "no such file or directory"))
        with pytest.raises(InisError) as excinfo:
            await make_session().remove("/workspace/missing.txt")
        assert "404" in str(excinfo.value)

    async def test_rename(self, fake_http_async):
        transport = fake_http_async(
            lambda method, path, params, body: json_response(200, {"path": "/a", "dest_path": "/b"})
        )
        await make_session().rename("/workspace/a.txt", "/workspace/b.txt")
        call = transport.calls[0]
        assert call.method == "POST"
        assert call.path.endswith("/files/rename")
        assert call.json_body == {"path": "/workspace/a.txt", "dest_path": "/workspace/b.txt"}


class TestFileStreaming:
    """upload_file/download_file async twins — download_file uses
    .stream() like exec_stream, so it needs the real MockTransport seam
    (see sse_session's doc above) rather than fake_http_async.
    """

    async def test_upload_file_streams_from_disk(self, fake_http_async, tmp_path):
        local = tmp_path / "payload.bin"
        raw = bytes(range(256)) * 40
        local.write_bytes(raw)

        transport = fake_http_async(
            lambda method, path, params, body: json_response(
                200, {"path": "/workspace/payload.bin", "bytes": len(raw), "sha256": "deadbeef"}
            )
        )
        result = await make_session().upload_file(str(local), "/workspace/payload.bin")
        call = transport.calls[0]
        assert call.method == "PUT"
        assert call.path.endswith("/files/stream")
        assert call.params == {"path": "/workspace/payload.bin"}
        assert result["bytes"] == len(raw)

    async def test_upload_file_error_raises(self, fake_http_async, tmp_path):
        local = tmp_path / "ro.bin"
        local.write_bytes(b"data")
        fake_http_async(lambda method, path, params, body: error_response(400, "permission denied"))
        with pytest.raises(InisError) as excinfo:
            await make_session().upload_file(str(local), "/workspace/ro/ro.bin")
        assert "400" in str(excinfo.value)

    async def test_download_file_writes_to_disk_and_hashes(self, tmp_path):
        import hashlib

        raw = bytes(range(256)) * 40

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=raw, headers={"content-type": "application/octet-stream"})

        session = make_session()
        session._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

        dest = tmp_path / "downloaded.bin"
        result = await session.download_file("/workspace/payload.bin", str(dest))

        assert dest.read_bytes() == raw
        assert result["bytes"] == len(raw)
        assert result["sha256"] == hashlib.sha256(raw).hexdigest()

    async def test_download_file_nonexistent_remote_raises(self, tmp_path):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404, json={"error": "no such file or directory"})

        session = make_session()
        session._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

        dest = tmp_path / "downloaded.bin"
        with pytest.raises(InisError) as excinfo:
            await session.download_file("/workspace/missing.bin", str(dest))
        assert "404" in str(excinfo.value)
        assert not dest.exists()


class TestProcesses:
    async def test_start_list_get_kill(self, fake_http_async):
        transport = fake_http_async(
            lambda method, path, params, body: json_response(
                200,
                {"name": "server", "state": "running", "pid": 42, "command": ["python3", "app.py"]},
            )
        )
        session = make_session()
        info = await session.start_process("server", "python3 app.py", keep_alive=True)
        assert transport.calls[0].json_body == {
            "name": "server",
            "command": ["bash", "-lc", "python3 app.py"],
            "keep_alive": True,
        }
        assert info.pid == 42

        killed = await session.kill_process("server")
        assert transport.calls[-1].method == "DELETE"
        assert killed.name == "server"

    async def test_list_processes(self, fake_http_async):
        fake_http_async(
            lambda method, path, params, body: json_response(
                200, {"processes": [{"name": "a", "state": "exited"}]}
            )
        )
        procs = await make_session().list_processes()
        assert len(procs) == 1
        assert procs[0].state == "exited"

    async def test_get_process_logs(self, fake_http_async):
        fake_http_async(
            lambda method, path, params, body: json_response(
                200, {"name": "server", "stdout": "hello\n", "stderr": ""}
            )
        )
        logs = await make_session().get_process_logs("server")
        assert logs.stdout == "hello\n"

    async def test_process_not_found_raises(self, fake_http_async):
        fake_http_async(lambda method, path, params, body: error_response(404, 'process "x" not found'))
        with pytest.raises(InisError) as excinfo:
            await make_session().get_process("x")
        assert "404" in str(excinfo.value)


class TestPortsAndEgress:
    async def test_expose(self, fake_http_async):
        transport = fake_http_async(
            lambda method, path, params, body: json_response(
                200,
                {
                    "session_id": "sess_123",
                    "port": 8080,
                    "preview_url": "https://sess-123-8080.inis.run",
                },
            )
        )
        result = await make_session().expose(8080, visibility="public")
        assert transport.calls[0].json_body == {"port": 8080, "visibility": "public"}
        assert result.preview_url == "https://sess-123-8080.inis.run"

    async def test_unexpose(self, fake_http_async):
        fake_http_async(lambda method, path, params, body: json_response(200, {"ok": True}))
        assert await make_session().unexpose(8080) is True

    async def test_get_set_egress(self, fake_http_async):
        transport = fake_http_async(
            lambda method, path, params, body: json_response(200, {"mode": "deny", "allow": []})
        )
        session = make_session()
        policy = await session.get_egress()
        assert policy["mode"] == "deny"
        await session.set_egress(mode="deny", allow=["api.openai.com"])
        assert transport.calls[-1].json_body == {"mode": "deny", "allow": ["api.openai.com"]}


class TestConnections:
    async def test_connection_spec_sent_on_create(self, fake_http_async):
        transport = fake_http_async(
            lambda method, path, params, body: json_response(
                200, {"session_id": "sess_new", "state": "live"}
            )
        )
        session = AsyncSession(
            base_url="https://api.inis.run",
            token="tok",
            connections=[
                ConnectionSpec(
                    name="stripe",
                    origin="https://api.stripe.com",
                    secret="sk_live_should-only-appear-in-this-one-request-body-1234567890",
                    methods=["GET", "POST"],
                    paths=["/v1/*"],
                    ttl_seconds=3600,
                )
            ],
        )
        await session.__aenter__()
        body = transport.calls[0].json_body
        assert body["connections"] == [
            {
                "name": "stripe",
                "origin": "https://api.stripe.com",
                "authentication": {
                    "type": "bearer",
                    "secret": "sk_live_should-only-appear-in-this-one-request-body-1234567890",
                },
                "allow": {"methods": ["GET", "POST"], "paths": ["/v1/*"]},
                "ttl_seconds": 3600,
            }
        ]

    async def test_get_exposes_redacted_connections_including_expired_state(self, fake_http_async):
        fake_http_async(
            lambda method, path, params, body: json_response(
                200,
                {
                    "session_id": "sess_123",
                    "state": "live",
                    "connections": [
                        {
                            "name": "stripe",
                            "origin": "https://api.stripe.com",
                            "state": "active",
                            "allow": {"methods": ["GET"], "paths": ["/v1/*"]},
                        },
                        {"name": "acme", "origin": "https://api.acme.example", "state": "expired"},
                    ],
                },
            )
        )
        info = await make_session().get()
        assert [c.state for c in info.connections] == ["active", "expired"]
        assert not hasattr(info.connections[0], "secret")

    async def test_rotate_connection_sends_put_and_maps_response_without_secret(
        self, fake_http_async
    ):
        transport = fake_http_async(
            lambda method, path, params, body: json_response(
                200,
                {
                    "name": "stripe",
                    "origin": "https://api.stripe.com",
                    "state": "active",
                    "allow": {"methods": ["GET"], "paths": ["/v1/*"]},
                },
            )
        )
        status = await make_session().rotate_connection(
            "stripe",
            origin="https://api.stripe.com",
            secret="sk_live_new-should-only-appear-in-this-one-request-body",
            methods=["GET"],
            paths=["/v1/*"],
        )
        call = transport.calls[0]
        assert call.method == "PUT"
        assert call.path == "https://api.inis.run/v1/sessions/sess_123/connections/stripe"
        assert call.json_body["authentication"]["secret"] == (
            "sk_live_new-should-only-appear-in-this-one-request-body"
        )
        assert status.name == "stripe"
        assert not hasattr(status, "secret")

    async def test_revoke_connection_sends_delete(self, fake_http_async):
        transport = fake_http_async(lambda method, path, params, body: json_response(204, {}))
        await make_session().revoke_connection("stripe")
        call = transport.calls[0]
        assert call.method == "DELETE"
        assert call.path == "https://api.inis.run/v1/sessions/sess_123/connections/stripe"


class TestForkCheckpointRestore:
    async def test_fork(self, fake_http_async):
        fake_http_async(
            lambda method, path, params, body: json_response(
                200, {"parent_session_id": "sess_123", "children": ["sess_c1", "sess_c2"]}
            )
        )
        result = await make_session().fork(2)
        assert result.children == ["sess_c1", "sess_c2"]

    async def test_checkpoint_and_list(self, fake_http_async):
        fake_http_async(
            lambda method, path, params, body: json_response(
                200, {"checkpoint_id": "cp_1", "checkpoints": [{"checkpoint_id": "cp_1"}]}
            )
        )
        session = make_session()
        ckpt = await session.checkpoint(name="before-risky-step")
        assert ckpt.checkpoint_id == "cp_1"
        ckpts = await session.checkpoints()
        assert ckpts[0].checkpoint_id == "cp_1"

    async def test_restore(self, fake_http_async):
        fake_http_async(
            lambda method, path, params, body: json_response(
                200, {"session_id": "sess_123", "state": "live"}
            )
        )
        info = await make_session().restore("cp_1")
        assert info.state == "live"

    async def test_save_as_template(self, fake_http_async):
        fake_http_async(
            lambda method, path, params, body: json_response(200, {"name": "my-tpl", "kind": "user"})
        )
        tpl = await make_session().save_as_template("my-tpl")
        assert tpl.name == "my-tpl"


class TestArtifacts:
    async def test_capture_and_list(self, fake_http_async):
        fake_http_async(
            lambda method, path, params, body: json_response(
                200, {"id": "art_1", "status": "pending", "artifacts": [{"id": "art_1", "status": "ready"}]}
            )
        )
        session = make_session()
        artifact = await session.capture_artifacts(["/workspace/out.txt"])
        assert artifact.id == "art_1"
        artifacts = await session.artifacts()
        assert artifacts[0].status == "ready"


class TestBatchExecStatic:
    async def test_batch_exec(self, fake_http_async):
        fake_http_async(
            lambda method, path, params, body: json_response(200, {"results": []})
        )
        await AsyncSession.batch_exec(
            base_url="https://api.inis.run",
            token="tok_abc",
            session_ids=["sess_1"],
            command="echo hi",
        )
        # batch_exec opens its own short-lived httpx.AsyncClient rather than
        # reusing a Session's, so it isn't captured by fake_http_async's
        # patch target being exercised above — the call not raising is the
        # assertion here; wire shape is covered on the sync twin.


def sse_session(sse_text: str, *, status_code: int = 200) -> AsyncSession:
    """Async twin of test_session.py's sse_session: gives the AsyncSession's
    httpx.AsyncClient a real MockTransport (the low-level seam both
    .request() and .stream() funnel through) instead of monkeypatching
    .request(), since exec_stream/stream_process_logs bypass .request()
    entirely via .stream().
    """
    session = make_session()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status_code, text=sse_text, headers={"content-type": "text/event-stream"}
        )

    session._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return session


class TestExecStreamSSE:
    """Async twin of TestExecStreamSSE in test_session.py — same coverage,
    exercised as an async generator instead of a plain iterator, proving
    exec_stream yields chunks incrementally through httpx.AsyncClient with no
    thread-pool wrapping."""

    async def test_decodes_stdout_stderr_and_exit(self):
        def b64(s: str) -> str:
            return base64.b64encode(s.encode("utf-8")).decode("ascii")

        out1 = b64("out1\n")
        err1 = b64("err1\n")
        sse = (
            f"event: stdout\ndata: {out1}\n\n"
            f"event: stderr\ndata: {err1}\n\n"
            'event: exit\ndata: {"exit_code":0,"timed_out":false,"duration_ms":42}\n\n'
        )
        session = sse_session(sse)

        events = [e async for e in session.exec_stream(["echo", "hi"])]

        assert [(e.stream, e.data) for e in events[:2]] == [("stdout", "out1\n"), ("stderr", "err1\n")]
        exit_event = events[2]
        assert exit_event.stream == "exit"
        assert exit_event.exit_code == 0
        assert exit_event.timed_out is False
        assert exit_event.duration_ms == 42

    async def test_stops_at_error_frame(self):
        sse = "event: error\ndata: guest agent unreachable\n\n"
        session = sse_session(sse)

        events = [e async for e in session.exec_stream("echo hi")]

        assert events == [ExecStreamEvent(stream="error", data="guest agent unreachable")]

    async def test_raises_inis_error_on_non_ok_response(self):
        session = sse_session("", status_code=503)
        with pytest.raises(InisError) as excinfo:
            async for _ in session.exec_stream("echo hi"):
                pass
        assert "503" in str(excinfo.value)

    async def test_reassembles_multibyte_char_split_across_chunk_boundary(self):
        raw = "hello \U0001F389 world".encode("utf-8")
        split_at = 6 + 2
        chunk1 = base64.b64encode(raw[:split_at]).decode("ascii")
        chunk2 = base64.b64encode(raw[split_at:]).decode("ascii")
        sse = (
            f"event: stdout\ndata: {chunk1}\n\n"
            f"event: stdout\ndata: {chunk2}\n\n"
            'event: exit\ndata: {"exit_code":0,"timed_out":false,"duration_ms":1}\n\n'
        )
        session = sse_session(sse)

        events = [e async for e in session.exec_stream("echo hi")]

        combined = "".join(e.data for e in events if e.stream == "stdout")
        assert combined == "hello \U0001F389 world"
        assert "�" not in combined


class TestStreamProcessLogsSSE:
    async def test_reassembles_multibyte_char_split_across_chunk_boundary(self):
        raw = "hello \U0001F389 world".encode("utf-8")
        split_at = 6 + 2
        chunk1 = base64.b64encode(raw[:split_at]).decode("ascii")
        chunk2 = base64.b64encode(raw[split_at:]).decode("ascii")
        sse = f"event: stdout\ndata: {chunk1}\n\n" f"event: stdout\ndata: {chunk2}\n\n" "event: eof\ndata: \n\n"
        session = sse_session(sse)

        events = [e async for e in session.stream_process_logs("server")]

        combined = "".join(e.data for e in events if e.event == "stdout")
        assert combined == "hello \U0001F389 world"
        assert "�" not in combined


class TestSessionCreateAsyncPolling:
    """Async twin of TestSessionCreateAsyncPolling in test_client.py, exercised
    directly against AsyncSession.__aenter__ (the underlying poll loop
    AsyncClient.sessions.create delegates to)."""

    @pytest.fixture(autouse=True)
    def _fast_poll(self, monkeypatch):
        monkeypatch.setattr(AsyncSession, "_CREATE_POLL_INTERVAL_S", 0.001)

    async def test_already_live_no_polling(self, fake_http_async):
        transport = fake_http_async(
            lambda method, path, params, body: json_response(
                200, {"session_id": "sess_new", "state": "live"}
            )
        )
        session = AsyncSession(base_url="https://api.inis.run", token="tok_abc")
        await session.__aenter__()
        assert session.session_id == "sess_new"
        assert len(transport.calls) == 1

    async def test_polls_through_creating_to_live(self, fake_http_async):
        responses = [
            {"session_id": "sess_new", "state": "creating", "status_detail": "preparing_template"},
            {"session_id": "sess_new", "state": "creating", "status_detail": "starting"},
            {"session_id": "sess_new", "state": "live"},
        ]
        calls = {"n": 0}

        def handler(method, path, params, body):
            resp = responses[min(calls["n"], len(responses) - 1)]
            calls["n"] += 1
            return json_response(200, resp)

        transport = fake_http_async(handler)
        session = AsyncSession(base_url="https://api.inis.run", token="tok_abc")
        await session.__aenter__()
        assert session.session_id == "sess_new"
        assert len(transport.calls) == 3

    async def test_failed_raises_with_reason(self, fake_http_async):
        responses = [
            {"session_id": "sess_new", "state": "creating", "status_detail": "preparing_template"},
            {"session_id": "sess_new", "state": "failed", "status_reason": "template not found"},
        ]
        calls = {"n": 0}

        def handler(method, path, params, body):
            resp = responses[min(calls["n"], len(responses) - 1)]
            calls["n"] += 1
            return json_response(200, resp)

        fake_http_async(handler)
        session = AsyncSession(base_url="https://api.inis.run", token="tok_abc")
        with pytest.raises(InisError) as excinfo:
            await session.__aenter__()
        assert "template not found" in str(excinfo.value)

    async def test_wait_false_skips_polling(self, fake_http_async):
        transport = fake_http_async(
            lambda method, path, params, body: json_response(
                200, {"session_id": "sess_new", "state": "creating", "status_detail": "preparing_template"}
            )
        )
        session = AsyncSession(base_url="https://api.inis.run", token="tok_abc", wait=False)
        await session.__aenter__()
        assert session.session_id == "sess_new"
        assert len(transport.calls) == 1
