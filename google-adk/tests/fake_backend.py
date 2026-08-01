"""A fake inis.run HTTP backend for deterministic tests.

Backs a real subprocess + a real temp directory instead of hand-simulated
shell semantics, mirroring `inis_strands_agents`/`inis_openai_agents`'s own
`tests/fake_backend.py`: everything upstream of this (the real
`inis.AsyncSession`/`AsyncClient`, and the real `InisEnvironment` adapter
code) runs for real; only "the session lives on this machine instead of
inis.run's infrastructure" is faked.

Session create/destroy is wired through one monkeypatched `httpx.AsyncClient`
inside `inis.async_client`, same technique (and same reason -- each
`AsyncSession`/`AsyncClient` opens its own private `httpx.AsyncClient`) as
`inis_openai_agents`' `tests/fake_client_backend.py`.
"""

from __future__ import annotations

import asyncio
import base64
import itertools
import json
import shutil
import tempfile
from pathlib import Path
from typing import Any

import httpx

import inis.async_client as inis_async_client_module

_counter = itertools.count(1)


class LocalProcessInisBackend:
    """Real subprocess + real temp directory standing in for one inis.run
    session's shell exec and file I/O."""

    def __init__(self, session_id: str, *, workspace_root: str = "/workspace") -> None:
        self.session_id = session_id
        self.destroyed = False
        self.workspace_root = workspace_root
        self._tmp = Path(tempfile.mkdtemp(prefix="inis-adk-test-"))
        (self._tmp / workspace_root.lstrip("/")).mkdir(parents=True, exist_ok=True)
        self.exec_log: list[list[str]] = []
        # When True, the next exec() response is marked truncated=True --
        # for testing this adapter's own truncation-marker logic in
        # isolation from inis.run's real guest-side capture cap.
        self.force_truncated_once = False

    def cleanup(self) -> None:
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _under_workspace_root(self, sandbox_path: str) -> bool:
        root_prefix = self.workspace_root.rstrip("/") + "/"
        return sandbox_path == self.workspace_root or sandbox_path.startswith(root_prefix)

    def _real_path(self, sandbox_path: str) -> Path:
        if self._under_workspace_root(sandbox_path):
            return self._tmp / sandbox_path.lstrip("/")
        return Path(sandbox_path)

    async def _run_exec(self, command: list[str], timeout_ms: int | None) -> dict[str, Any]:
        self.exec_log.append(command)
        cwd = self._tmp / self.workspace_root.lstrip("/")
        proc = await asyncio.create_subprocess_exec(
            *command,
            cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=(timeout_ms / 1000 if timeout_ms else None)
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return {"stdout": "", "stderr": "", "exit_code": -1, "timed_out": True, "duration_ms": timeout_ms or 0}
        truncated = self.force_truncated_once
        if truncated:
            self.force_truncated_once = False
        return {
            "stdout": stdout.decode("utf-8", errors="replace"),
            "stderr": stderr.decode("utf-8", errors="replace"),
            "exit_code": proc.returncode or 0,
            "timed_out": False,
            "duration_ms": 1,
            "truncated": truncated,
        }

    async def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        method = request.method
        prefix = f"/v1/sessions/{self.session_id}"

        if path == prefix and method == "DELETE":
            self.destroyed = True
            return httpx.Response(204)
        if path == prefix and method == "GET":
            return httpx.Response(200, json={"session_id": self.session_id, "state": "live"})

        if path == f"{prefix}/exec" and method == "POST":
            body = json.loads(request.content)
            command = body["command"]
            if isinstance(command, str):
                command = ["bash", "-lc", command]
            result = await self._run_exec(command, body.get("timeout_ms"))
            return httpx.Response(200, json=result)

        if path == f"{prefix}/files" and method == "GET":
            sandbox_path = request.url.params["path"]
            if not self._under_workspace_root(sandbox_path):
                return httpx.Response(400, json={"error": "path must be under /workspace", "code": "bad_request"})
            real = self._real_path(sandbox_path)
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
    """Returns (AsyncSession, LocalProcessInisBackend) wired together --
    for `.wrap()`/`.attach()`-style tests of an already-existing session."""
    from inis import AsyncSession

    backend = LocalProcessInisBackend(session_id, workspace_root=workspace_root)
    session = AsyncSession.attach(session_id, base_url="https://fake.inis.test", token="test-token")
    session._client = httpx.AsyncClient(transport=httpx.MockTransport(backend.handler))
    return session, backend


def attach_fake_client(monkeypatch) -> tuple[Any, dict[str, LocalProcessInisBackend]]:
    """Monkeypatch every `httpx.AsyncClient` `inis.async_client` constructs to
    route to one shared in-memory registry of `LocalProcessInisBackend`s --
    for `InisEnvironment()`'s owned mode (`initialize()`/`close()` calling
    `AsyncClient.sessions.create()`/`session.destroy()`)."""
    registry: dict[str, LocalProcessInisBackend] = {}

    async def dispatch(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/sessions" and request.method == "POST":
            session_id = f"sess_fake_{next(_counter)}"
            registry[session_id] = LocalProcessInisBackend(session_id)
            return httpx.Response(200, json={"session_id": session_id, "state": "live"})
        for session_id, backend in registry.items():
            if request.url.path.startswith(f"/v1/sessions/{session_id}"):
                return await backend.handler(request)
        return httpx.Response(404, json={"error": "unknown session", "code": "not_found"})

    class _FakeAsyncHttpxClient(httpx.AsyncClient):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            kwargs.pop("transport", None)
            super().__init__(*args, transport=httpx.MockTransport(dispatch), **kwargs)

    monkeypatch.setattr(inis_async_client_module.httpx, "AsyncClient", _FakeAsyncHttpxClient)

    client = inis_async_client_module.AsyncClient(token="test-token", base_url="https://fake.inis.test")
    return client, registry
