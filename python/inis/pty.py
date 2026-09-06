"""Interactive PTY handle over the ``GET /v1/sessions/{id}/pty`` WebSocket
endpoint — the same endpoint and binary frame protocol the console web
terminal uses (web/console/src/routes/_app/sessions/$id.tsx) and the exec-node
handler implements (internal/api/pty.go, internal/protocol PTYFrame*).

One module, shared by both transports:
  - inis.client.Session.pty() (sync) uses websockets.sync.client — a real
    blocking socket client, no asyncio loop required in the caller.
  - inis.async_client.AsyncSession.pty() (async) uses websockets.asyncio.client.

Frame wire format (both directions): one binary WebSocket message per frame,
``[1 byte type][payload]``. Types below must match internal/protocol's
PTYFrame* constants exactly:
  - DATA (0x01): raw bytes. Host->guest: stdin. Guest->host: output.
  - RESIZE (0x02): host->guest only. 4 bytes: rows uint16 BE, cols uint16 BE.
  - SIGNAL (0x03): host->guest only. 1 byte: POSIX signal number (e.g. 9 =
    SIGKILL, to force-kill an unresponsive foreground process).
  - EXIT (0x05): guest->host only. 4 bytes: exit code, int32 BE. The server
    closes the socket immediately after, so this is always the last frame.
  - ERROR (0x06): guest->host only. UTF-8 message describing a protocol-level
    failure (as opposed to the shell/command's own exit code).
"""

from __future__ import annotations

import json
import struct
from dataclasses import dataclass
from typing import Any, AsyncIterator, Iterator
from urllib.parse import urlencode

import websockets
import websockets.sync.client
from websockets.exceptions import ConnectionClosed, InvalidStatus

from inis.client import InisError

_DATA = 0x01
_RESIZE = 0x02
_SIGNAL = 0x03
_EXIT = 0x05
_ERROR = 0x06

# Mirrors internal/protocol.MaxPTYFrameBytes (1<<20) plus the 1-byte type
# prefix and a little headroom for the WebSocket framing overhead itself.
_MAX_FRAME_BYTES = (1 << 20) + 64


def _ws_url(
    base_url: str,
    session_id: str,
    *,
    rows: int,
    cols: int,
    command: list[str] | None,
    cwd: str | None,
    term: str,
    colorterm: str,
) -> str:
    if base_url.startswith("https://"):
        scheme, host = "wss", base_url[len("https://") :]
    elif base_url.startswith("http://"):
        scheme, host = "ws", base_url[len("http://") :]
    else:
        # Already a ws(s):// base_url (e.g. a caller pointed it there directly).
        scheme, host = base_url.split("://", 1)
    params: list[tuple[str, str]] = [("rows", str(rows)), ("cols", str(cols))]
    if term:
        params.append(("term", term))
    if colorterm:
        params.append(("colorterm", colorterm))
    for part in command or []:
        params.append(("cmd", part))
    if cwd:
        params.append(("cwd", cwd))
    return f"{scheme}://{host.rstrip('/')}/v1/sessions/{session_id}/pty?{urlencode(params)}"


def _connect_error(exc: Exception) -> InisError:
    """Map a failed WebSocket handshake to the same InisError shape the rest
    of the SDK raises for a bad HTTP response — {status}: {detail} — so a
    caller can handle a nonexistent/ended session the same way regardless of
    which endpoint told it.
    """
    response = getattr(exc, "response", None)
    if response is not None:
        status = getattr(response, "status_code", "?")
        body = bytes(getattr(response, "body", b"") or b"")
        detail = body.decode("utf-8", "replace")
        try:
            parsed = json.loads(detail)
            if isinstance(parsed, dict) and "error" in parsed:
                detail = parsed["error"]
        except (ValueError, TypeError):
            pass
        return InisError(f"{status}: {detail or exc}")
    return InisError(f"pty connection failed: {exc}")


@dataclass
class PTYExitInfo:
    """Terminal state of a PTY once its shell/command has exited."""

    exit_code: int


class _PTYCommon:
    """Frame encode/decode shared by PTY and AsyncPTY. Not instantiated
    directly — subclasses hold the actual sync/async websocket connection.
    """

    _exit_code: int | None = None
    _closed: bool = False

    def _decode_frame(self, msg: bytes | str) -> tuple[int, bytes] | None:
        if isinstance(msg, str):
            msg = msg.encode("utf-8")
        if not msg:
            return None
        return msg[0], msg[1:]

    def _handle_exit(self, payload: bytes) -> None:
        self._exit_code = struct.unpack(">i", payload[:4])[0] if len(payload) >= 4 else 0

    @property
    def exit_code(self) -> int | None:
        """The command's exit code, or None until PTY_EXIT has arrived."""
        return self._exit_code


class PTY(_PTYCommon):
    """A live, synchronous interactive PTY session. Returned by
    ``Session.pty()``; iterate it for output, call write()/resize() to send
    input, and read exit_code once iteration ends.

    Usage::

        with session.pty(cols=80, rows=24) as term:
            term.write("echo hi\\n")
            for chunk in term:
                print(chunk.decode(), end="")
                if b"$" in chunk:
                    term.write("exit\\n")
    """

    def __init__(self, ws: "websockets.sync.client.ClientConnection") -> None:
        self._ws = ws

    @classmethod
    def _open(
        cls,
        *,
        base_url: str,
        token: str,
        session_id: str,
        rows: int = 24,
        cols: int = 80,
        command: list[str] | None = None,
        cwd: str | None = None,
        term: str = "xterm-256color",
        colorterm: str = "truecolor",
        open_timeout: float = 30.0,
    ) -> "PTY":
        url = _ws_url(
            base_url,
            session_id,
            rows=rows,
            cols=cols,
            command=command,
            cwd=cwd,
            term=term,
            colorterm=colorterm,
        )
        try:
            ws = websockets.sync.client.connect(
                url,
                additional_headers={"Authorization": f"Bearer {token}"},
                max_size=_MAX_FRAME_BYTES,
                open_timeout=open_timeout,
            )
        except InvalidStatus as exc:
            raise _connect_error(exc) from exc
        except Exception as exc:  # noqa: BLE001 - surfaced as one SDK error type
            raise _connect_error(exc) from exc
        return cls(ws)

    def _send(self, typ: int, payload: bytes) -> None:
        if self._closed:
            raise InisError("pty is closed")
        try:
            self._ws.send(bytes([typ]) + payload)
        except ConnectionClosed as exc:
            self._closed = True
            raise InisError("pty connection closed") from exc

    def write(self, data: bytes | str) -> None:
        """Send input, as if typed at the keyboard."""
        if isinstance(data, str):
            data = data.encode("utf-8")
        self._send(_DATA, data)

    def resize(self, cols: int, rows: int) -> None:
        """Tell the guest shell the terminal viewport changed size."""
        self._send(_RESIZE, struct.pack(">HH", rows, cols))

    def send_signal(self, sig: int) -> None:
        """Send a POSIX signal to the foreground process (e.g. 9 = SIGKILL,
        for a hung command a plain close() won't interrupt).
        """
        self._send(_SIGNAL, bytes([sig & 0xFF]))

    def kill(self) -> None:
        """Force-kill the foreground process (SIGKILL).

        Does not itself close the connection — the guest still sends a
        PTY_EXIT frame in response, which iterating (or a caller already
        mid-iteration) needs to see to learn .exit_code. Call close() (or
        exit the `with` block) once you're done reading.
        """
        self.send_signal(9)

    def __iter__(self) -> Iterator[bytes]:
        """Yield output chunks as they arrive. Stops when the command exits
        (check .exit_code afterward) or the connection drops.

        Breaking out of this loop early (e.g. once you've seen a prompt you
        were waiting for) does NOT close the connection — you're expected to
        keep writing/iterating later. Only a natural end (PTY_EXIT, a
        PTY_ERROR frame, or the peer dropping the connection) closes it; a
        generator abandoned mid-iteration just stops silently (Python throws
        GeneratorExit into it, which propagates past the except clauses
        below untouched, running no cleanup).
        """
        try:
            for msg in self._ws:
                frame = self._decode_frame(msg)
                if frame is None:
                    continue
                typ, payload = frame
                if typ == _DATA:
                    yield payload
                elif typ == _EXIT:
                    self._handle_exit(payload)
                    self.close()
                    return
                elif typ == _ERROR:
                    self.close()
                    raise InisError(payload.decode("utf-8", "replace"))
        except ConnectionClosed:
            self.close()
            return

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._ws.close()

    def __enter__(self) -> "PTY":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()


class AsyncPTY(_PTYCommon):
    """Async twin of PTY — same protocol, `websockets.asyncio.client`
    transport, coroutine methods and an async iterator for output.
    """

    def __init__(self, ws: Any) -> None:
        self._ws = ws

    @classmethod
    async def _open(
        cls,
        *,
        base_url: str,
        token: str,
        session_id: str,
        rows: int = 24,
        cols: int = 80,
        command: list[str] | None = None,
        cwd: str | None = None,
        term: str = "xterm-256color",
        colorterm: str = "truecolor",
        open_timeout: float = 30.0,
    ) -> "AsyncPTY":
        url = _ws_url(
            base_url,
            session_id,
            rows=rows,
            cols=cols,
            command=command,
            cwd=cwd,
            term=term,
            colorterm=colorterm,
        )
        try:
            ws = await websockets.connect(
                url,
                additional_headers={"Authorization": f"Bearer {token}"},
                max_size=_MAX_FRAME_BYTES,
                open_timeout=open_timeout,
            )
        except InvalidStatus as exc:
            raise _connect_error(exc) from exc
        except Exception as exc:  # noqa: BLE001 - surfaced as one SDK error type
            raise _connect_error(exc) from exc
        return cls(ws)

    async def _send(self, typ: int, payload: bytes) -> None:
        if self._closed:
            raise InisError("pty is closed")
        try:
            await self._ws.send(bytes([typ]) + payload)
        except ConnectionClosed as exc:
            self._closed = True
            raise InisError("pty connection closed") from exc

    async def write(self, data: bytes | str) -> None:
        """Send input, as if typed at the keyboard."""
        if isinstance(data, str):
            data = data.encode("utf-8")
        await self._send(_DATA, data)

    async def resize(self, cols: int, rows: int) -> None:
        """Tell the guest shell the terminal viewport changed size."""
        await self._send(_RESIZE, struct.pack(">HH", rows, cols))

    async def send_signal(self, sig: int) -> None:
        """Send a POSIX signal to the foreground process (e.g. 9 = SIGKILL,
        for a hung command a plain close() won't interrupt).
        """
        await self._send(_SIGNAL, bytes([sig & 0xFF]))

    async def kill(self) -> None:
        """Force-kill the foreground process (SIGKILL).

        Does not itself close the connection — the guest still sends a
        PTY_EXIT frame in response, which iterating (or a caller already
        mid-iteration) needs to see to learn .exit_code. Call close() (or
        exit the `async with` block) once you're done reading.
        """
        await self.send_signal(9)

    async def __aiter__(self) -> AsyncIterator[bytes]:
        """Yield output chunks as they arrive. Stops when the command exits
        (check .exit_code afterward) or the connection drops.

        Breaking out of this loop early (e.g. once you've seen a prompt you
        were waiting for) does NOT close the connection — you're expected to
        keep writing/iterating later. Only a natural end (PTY_EXIT, a
        PTY_ERROR frame, or the peer dropping the connection) closes it; an
        async generator abandoned mid-iteration just stops silently (Python
        throws GeneratorExit into it, which propagates past the except
        clauses below untouched, running no cleanup).
        """
        try:
            async for msg in self._ws:
                frame = self._decode_frame(msg)
                if frame is None:
                    continue
                typ, payload = frame
                if typ == _DATA:
                    yield payload
                elif typ == _EXIT:
                    self._handle_exit(payload)
                    await self.close()
                    return
                elif typ == _ERROR:
                    await self.close()
                    raise InisError(payload.decode("utf-8", "replace"))
        except ConnectionClosed:
            await self.close()
            return

    async def close(self) -> None:
        if not self._closed:
            self._closed = True
            await self._ws.close()

    async def __aenter__(self) -> "AsyncPTY":
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        await self.close()
