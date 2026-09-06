"""Tests for the PTY handle against a real local WebSocket server.

The sync (websockets.sync.client) and async (websockets.asyncio.client)
transports open actual TCP sockets, so the httpx.MockTransport seam the rest
of this SDK's tests use (see conftest.py's fake_http/fake_http_async) doesn't
apply here — these tests run a tiny real `websockets` server on localhost and
exercise the SDK's PTY handle against it, mirroring the exec-node's binary
frame protocol (internal/api/pty.go, internal/protocol PTYFrame*).
"""

from __future__ import annotations

import asyncio
import json
import struct
import threading
from typing import Any, Callable

import pytest
import websockets
from websockets.asyncio.server import serve

from inis.async_client import AsyncSession
from inis.client import InisError, Session

_DATA = 0x01
_RESIZE = 0x02
_SIGNAL = 0x03
_EXIT = 0x05
_ERROR = 0x06


class MockPTYServer:
    """A real `websockets` server on its own background event loop / thread,
    so it never shares a loop with the SDK's async client under test (running
    that server on the *same* loop as a blocking `t.join()`-style wait wedges
    both — the accept() never gets a turn).

    process_request, if given, lets a test reject the handshake outright
    (e.g. to emulate the exec node's 404 for a nonexistent session) instead of
    accepting the WebSocket upgrade.
    """

    def __init__(
        self,
        handler: Callable[[Any], "asyncio.Future"],
        process_request: Callable | None = None,
    ) -> None:
        self._handler = handler
        self._process_request = process_request
        self._loop: asyncio.AbstractEventLoop | None = None
        self._server = None
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self.received: list[tuple[int, bytes]] = []

    def _run(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)

        async def main() -> None:
            kwargs: dict[str, Any] = {}
            if self._process_request:
                kwargs["process_request"] = self._process_request
            self._server = await serve(self._handler, "localhost", 0, **kwargs)
            self._ready.set()
            await self._server.wait_closed()

        self._loop.run_until_complete(main())

    def start(self) -> int:
        self._thread.start()
        if not self._ready.wait(timeout=5):
            raise RuntimeError("mock pty server did not start in time")
        return self._server.sockets[0].getsockname()[1]

    def stop(self) -> None:
        if self._loop and self._server:
            self._loop.call_soon_threadsafe(self._server.close)
        self._thread.join(timeout=5)


@pytest.fixture
def mock_pty_server():
    servers: list[MockPTYServer] = []

    def _start(handler, process_request=None) -> tuple[int, MockPTYServer]:
        srv = MockPTYServer(handler, process_request=process_request)
        port = srv.start()
        servers.append(srv)
        return port, srv

    yield _start
    for srv in servers:
        srv.stop()


def _base_url(port: int) -> str:
    return f"http://localhost:{port}"


async def _echo_resize_exit_handler(ws) -> None:
    """DATA -> echoes "echo:"+payload; RESIZE -> emits a DATA frame
    describing the new size; SIGNAL -> exits with the signal number as the
    exit code (so kill()/send_signal() are observable); otherwise ignored.
    """
    async for msg in ws:
        typ, payload = msg[0], msg[1:]
        if typ == _DATA:
            await ws.send(bytes([_DATA]) + b"echo:" + payload)
        elif typ == _RESIZE:
            rows, cols = struct.unpack(">HH", payload[:4])
            await ws.send(bytes([_DATA]) + f"resized:{rows}x{cols}".encode())
        elif typ == _SIGNAL:
            await ws.send(bytes([_EXIT]) + struct.pack(">i", payload[0]))
            return


async def _immediate_error_handler(ws) -> None:
    await ws.send(bytes([_ERROR]) + b"guest agent unreachable")


async def _reject_handshake(connection, request):
    body = json.dumps({"error": "session not found", "code": "not_found"}) + "\n"
    return connection.respond(404, body)


# ── sync (Session.pty) ──────────────────────────────────────────────────────


def test_sync_pty_write_and_iterate(mock_pty_server):
    port, _srv = mock_pty_server(_echo_resize_exit_handler)
    session = Session(base_url=_base_url(port), token="tok_abc", session_id="sess_1")

    with session.pty(cols=80, rows=24) as term:
        term.write("hi")
        chunks = []
        for chunk in term:
            chunks.append(chunk)
            break
    assert chunks == [b"echo:hi"]


def test_sync_pty_resize_sends_be_rows_cols(mock_pty_server):
    port, _srv = mock_pty_server(_echo_resize_exit_handler)
    session = Session(base_url=_base_url(port), token="tok_abc", session_id="sess_1")

    with session.pty() as term:
        term.resize(cols=120, rows=40)
        chunks = []
        for chunk in term:
            chunks.append(chunk)
            break
    assert chunks == [b"resized:40x120"]


def test_sync_pty_kill_sends_signal_and_records_exit_code(mock_pty_server):
    port, _srv = mock_pty_server(_echo_resize_exit_handler)
    session = Session(base_url=_base_url(port), token="tok_abc", session_id="sess_1")

    with session.pty() as term:
        term.kill()
        # kill() only sends the signal — read the resulting PTY_EXIT frame
        # (the handler echoes the signal number back as the exit code).
        for _chunk in term:
            pass
    assert term.exit_code == 9


def test_sync_pty_error_frame_raises_inis_error(mock_pty_server):
    port, _srv = mock_pty_server(_immediate_error_handler)
    session = Session(base_url=_base_url(port), token="tok_abc", session_id="sess_1")

    with session.pty() as term:
        with pytest.raises(InisError, match="guest agent unreachable"):
            for _chunk in term:
                pass


def test_sync_pty_open_on_missing_session_raises_inis_error(mock_pty_server):
    async def unused_handler(ws) -> None:
        pass

    port, _srv = mock_pty_server(unused_handler, process_request=_reject_handshake)
    session = Session(base_url=_base_url(port), token="tok_abc", session_id="does-not-exist")

    with pytest.raises(InisError, match="404"):
        session.pty()


def test_sync_pty_open_sends_command_and_cwd_query_params(mock_pty_server):
    captured: dict[str, Any] = {}

    async def capturing_process_request(connection, request):
        captured["path"] = request.path
        return None  # None = proceed with the normal handshake/handler

    port, _srv = mock_pty_server(
        _echo_resize_exit_handler, process_request=capturing_process_request
    )
    session = Session(base_url=_base_url(port), token="tok_abc", session_id="sess_1")
    with session.pty(command=["python3", "-i"], cwd="/workspace") as term:
        term.close()

    assert "cmd=python3" in captured["path"]
    assert "cmd=-i" in captured["path"]
    assert "cwd=%2Fworkspace" in captured["path"] or "cwd=/workspace" in captured["path"]


# ── async (AsyncSession.pty) ─────────────────────────────────────────────────


async def test_async_pty_write_and_iterate(mock_pty_server):
    port, _srv = mock_pty_server(_echo_resize_exit_handler)
    session = AsyncSession(base_url=_base_url(port), token="tok_abc", session_id="sess_1")

    term = await session.pty(cols=80, rows=24)
    async with term:
        await term.write("hi")
        chunks = []
        async for chunk in term:
            chunks.append(chunk)
            break
    assert chunks == [b"echo:hi"]


async def test_async_pty_resize_sends_be_rows_cols(mock_pty_server):
    port, _srv = mock_pty_server(_echo_resize_exit_handler)
    session = AsyncSession(base_url=_base_url(port), token="tok_abc", session_id="sess_1")

    term = await session.pty()
    async with term:
        await term.resize(cols=120, rows=40)
        chunks = []
        async for chunk in term:
            chunks.append(chunk)
            break
    assert chunks == [b"resized:40x120"]


async def test_async_pty_kill_sends_signal_and_records_exit_code(mock_pty_server):
    port, _srv = mock_pty_server(_echo_resize_exit_handler)
    session = AsyncSession(base_url=_base_url(port), token="tok_abc", session_id="sess_1")

    term = await session.pty()
    async with term:
        await term.kill()
        async for _chunk in term:
            pass
    assert term.exit_code == 9


async def test_async_pty_error_frame_raises_inis_error(mock_pty_server):
    port, _srv = mock_pty_server(_immediate_error_handler)
    session = AsyncSession(base_url=_base_url(port), token="tok_abc", session_id="sess_1")

    term = await session.pty()
    async with term:
        with pytest.raises(InisError, match="guest agent unreachable"):
            async for _chunk in term:
                pass


async def test_async_pty_open_on_missing_session_raises_inis_error(mock_pty_server):
    async def unused_handler(ws) -> None:
        pass

    port, _srv = mock_pty_server(unused_handler, process_request=_reject_handshake)
    session = AsyncSession(base_url=_base_url(port), token="tok_abc", session_id="does-not-exist")

    with pytest.raises(InisError, match="404"):
        await session.pty()
