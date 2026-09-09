"""A fake inis.run HTTP backend for deterministic tests.

Backs `httpx.MockTransport` with a real local subprocess and a real temp
directory instead of hand-simulated shell semantics, mirroring
`inis_openai_agents`'s own `LocalProcessInisBackend` (see that package's
tests/fake_backend.py). The `exec` endpoint streams real Server-Sent Events
framed exactly like the real wire protocol (`event: stdout|stderr|exit\\ndata:
<base64 or JSON>\\n\\n` -- verified against `AsyncSession.exec_stream` and
`sdk/python/tests/test_async_session.py`'s own `TestExecStreamSSE`), backed by
a real `asyncio.subprocess` so cancellation tests can prove the subprocess is
genuinely killed, not just that the local iterator stopped.

Timeout behavior is intentionally NOT exercised through this real-subprocess
path (racing a real timeout deterministically is its own can of worms); the
`SandboxTimeoutError` mapping is tested instead against a canned SSE payload
(`canned_sse_transport`, same technique `test_async_session.py` uses for its
own SSE unit tests) since only the *mapping* of an already-timed-out event is
this adapter's own logic, not inis.run's timeout enforcement itself.
"""

from __future__ import annotations

import asyncio
import base64
import json
import shutil
import tempfile
from pathlib import Path
from typing import Any

import httpx


def canned_sse_transport(sse_text: str, *, status_code: int = 200) -> httpx.MockTransport:
    """A MockTransport that always answers with the given canned SSE body,
    regardless of the request -- for testing this adapter's own event-mapping
    logic (timeout, error frames) in isolation from real process execution."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, text=sse_text, headers={"content-type": "text/event-stream"})

    return httpx.MockTransport(handler)


class RecordingTransport(httpx.MockTransport):
    """Wraps another handler, recording every request's JSON body (when any)
    alongside its method/path, so tests can assert on exactly what this
    adapter sent (e.g. the `export FOO=bar && ...` env prefix)."""

    def __init__(self, handler: Any) -> None:
        self.requests: list[httpx.Request] = []

        async def recording_handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            result = handler(request)
            if asyncio.iscoroutine(result):
                result = await result
            return result

        super().__init__(recording_handler)


class LocalProcessInisBackend:
    """Real subprocess + real temp directory standing in for one inis.run
    session. Everything upstream of this (the real `inis.AsyncSession`/
    `AsyncClient` HTTP client, and the real `InisSandbox` adapter code) runs
    for real; only "the session lives on this machine instead of inis.run's
    infrastructure" is faked.
    """

    def __init__(self, session_id: str, *, workspace_root: str = "/workspace") -> None:
        self.session_id = session_id
        self.destroyed = False
        self._tmp = Path(tempfile.mkdtemp(prefix="inis-strands-test-"))
        self.workspace_root = workspace_root
        (self._tmp / workspace_root.lstrip("/")).mkdir(parents=True, exist_ok=True)
        self.last_proc: asyncio.subprocess.Process | None = None

    def cleanup(self) -> None:
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _under_workspace_root(self, sandbox_path: str) -> bool:
        root_prefix = self.workspace_root.rstrip("/") + "/"
        return sandbox_path == self.workspace_root or sandbox_path.startswith(root_prefix)

    def _real_path(self, sandbox_path: str) -> Path:
        if self._under_workspace_root(sandbox_path):
            return self._tmp / sandbox_path.lstrip("/")
        return Path(sandbox_path)

    async def _sse_exec_body(self, command: list[str], cwd: str | None) -> Any:
        """Async generator of raw SSE bytes, backed by a real subprocess.
        Killed in `finally` if this generator is abandoned early (GeneratorExit
        from the consumer cancelling/closing) -- the same real-cancellation
        proof `LocalProcessInisBackend` in the OpenAI adapter's tests do not
        need (that adapter never streams) but this one does.
        """
        real_cwd = self._real_path(cwd) if cwd else (self._tmp / self.workspace_root.lstrip("/"))
        proc = await asyncio.create_subprocess_exec(
            *command,
            cwd=str(real_cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self.last_proc = proc
        queue: asyncio.Queue[bytes | None] = asyncio.Queue()

        async def pump(stream: asyncio.StreamReader, event_name: str) -> None:
            while chunk := await stream.read(4096):
                data = base64.b64encode(chunk).decode("ascii")
                await queue.put(f"event: {event_name}\ndata: {data}\n\n".encode())
            await queue.put(None)

        assert proc.stdout is not None and proc.stderr is not None
        pumps = asyncio.gather(pump(proc.stdout, "stdout"), pump(proc.stderr, "stderr"))

        async def reader() -> None:
            await pumps
            await proc.wait()
            await queue.put(None)

        reader_task = asyncio.ensure_future(reader())
        try:
            done = 0
            while done < 3:  # two stream-EOF sentinels + the reader's own final sentinel
                item = await queue.get()
                if item is None:
                    done += 1
                    continue
                yield item
            exit_payload = json.dumps({"exit_code": proc.returncode or 0, "timed_out": False, "duration_ms": 1})
            yield f"event: exit\ndata: {exit_payload}\n\n".encode()
        finally:
            if proc.returncode is None:
                proc.kill()
            pumps.cancel()
            reader_task.cancel()
            await asyncio.gather(pumps, reader_task, return_exceptions=True)

    async def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        method = request.method
        prefix = f"/v1/sessions/{self.session_id}"

        if path == prefix and method == "DELETE":
            self.destroyed = True
            return httpx.Response(204)

        if path == f"{prefix}/exec" and method == "POST" and request.url.params.get("stream") == "true":
            body = json.loads(request.content)
            command = body["command"]
            cwd = body.get("cwd")
            return httpx.Response(
                200,
                content=self._sse_exec_body(command, cwd),
                headers={"content-type": "text/event-stream"},
            )

        if path == f"{prefix}/files" and method == "GET":
            sandbox_path = request.url.params["path"]
            op = request.url.params.get("op", "read")
            if not self._under_workspace_root(sandbox_path):
                return httpx.Response(400, json={"error": "path must be under /workspace", "code": "bad_request"})
            real = self._real_path(sandbox_path)
            if op == "list":
                if not real.is_dir():
                    return httpx.Response(
                        404, json={"error": f"open {sandbox_path}: no such file or directory", "code": "bad_request"}
                    )
                names = []
                for p in sorted(real.iterdir()):
                    names.append(p.name + "/" if p.is_dir() else p.name)
                return httpx.Response(200, json={"entries": names})
            if not real.is_file():
                return httpx.Response(
                    404, json={"error": f"open {sandbox_path}: no such file or directory", "code": "bad_request"}
                )
            data = real.read_bytes()
            if request.url.params.get("encoding") == "base64":
                return httpx.Response(200, json={"content": base64.b64encode(data).decode("ascii")})
            return httpx.Response(200, json={"content": data.decode("utf-8", errors="replace")})

        if path == f"{prefix}/files" and method == "PUT":
            body = json.loads(request.content)
            if not self._under_workspace_root(body["path"]):
                return httpx.Response(400, json={"error": "path must be under /workspace", "code": "bad_request"})
            real = self._real_path(body["path"])
            real.parent.mkdir(parents=True, exist_ok=True)
            content = body["content"]
            if body.get("encoding") == "base64":
                real.write_bytes(base64.b64decode(content))
            else:
                real.write_text(content)
            return httpx.Response(200, json={})

        if path == f"{prefix}/files" and method == "DELETE":
            sandbox_path = request.url.params["path"]
            real = self._real_path(sandbox_path)
            if not real.exists():
                return httpx.Response(
                    404, json={"error": f"remove {sandbox_path}: no such file or directory", "code": "bad_request"}
                )
            if real.is_dir():
                shutil.rmtree(real, ignore_errors=True)
            else:
                real.unlink()
            return httpx.Response(204)

        return httpx.Response(500, json={"error": f"unhandled {method} {path}"})


def attach_fake_session(session_id: str = "sess-test-1", *, workspace_root: str = "/workspace"):
    """Returns (AsyncSession, LocalProcessInisBackend) wired together."""
    from inis import AsyncSession

    backend = LocalProcessInisBackend(session_id, workspace_root=workspace_root)
    session = AsyncSession.attach(session_id, base_url="https://fake.inis.test", token="test-token")
    session._client = httpx.AsyncClient(transport=httpx.MockTransport(backend.handler))
    return session, backend
