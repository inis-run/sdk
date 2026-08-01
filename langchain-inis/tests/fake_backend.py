"""A fake inis.run HTTP backend for deterministic tests.

Backs `httpx.MockTransport` with a real local subprocess + a real temp
directory instead of hand-simulated shell semantics, mirroring
`sdk/openai-agents-python/tests/fake_backend.py`'s approach (same
underlying inis.run HTTP API shape: `GET/DELETE /v1/sessions/{id}`,
`POST .../exec`, `GET/PUT/DELETE .../files`). Only the fact that the
"session" lives on this machine instead of inis.run's infrastructure is
faked — everything upstream of this (the real `inis.Session`/
`inis.AsyncSession` HTTP clients, and the real `InisSandboxBackend`
adapter code under test) runs for real.

Provides both a sync (`handler`, for `inis.Session` / `httpx.Client`) and
an async (`ahandler`, for `inis.AsyncSession` / `httpx.AsyncClient`)
request handler over the *same* backing state, since `InisSandboxBackend`
uses a sync `Session` for `execute`/`upload_files`/`download_files` and a
lazily-attached `AsyncSession` twin for `aexecute`/`aupload_files`/
`adownload_files` — both must observe the same filesystem and exec log.
"""

from __future__ import annotations

import asyncio
import base64
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import httpx


class LocalProcessInisBackend:
    """Real local subprocess + real temp directory standing in for one
    inis.run session.

    `workspace_root` is a REAL, already-writable absolute directory on this
    machine (a fresh `tempfile.mkdtemp()` result by default) — deliberately
    NOT the literal string `"/workspace"`. `deepagents.backends.sandbox.
    BaseSandbox`'s own derived operations (`ls`/`read`/`grep`/`glob`/the
    write mkdir-preflight) generate shell scripts that embed the *exact*
    absolute path a caller gave them, often base64-encoded specifically "to
    avoid shell escaping issues" (see that module's own `_GLOB_COMMAND_
    TEMPLATE`/`_GREP_PATH_GLOB_TEMPLATE`/`_WRITE_CHECK_TEMPLATE`) — so a
    fake backend cannot recover the real inis.run product's actual
    `/workspace`-only file-API restriction by textually rewriting a
    `"/workspace"` prefix inside those scripts (that trick only works when
    the path appears in the command as literal, unencoded text, which is
    true for `sdk/openai-agents-python/tests/fake_backend.py`'s own
    tar/base64 commands but not true here). Making the fake's workspace
    root a real absolute path that already exists sidesteps the problem
    entirely: every script `BaseSandbox` generates runs unmodified, because
    the path it references is genuinely real. Callers (the standard
    conformance suite's `sandbox_root_dir`, in `test_backend.py`) must
    build test paths under this same real `workspace_root` — not under
    literal `/workspace` — to match.
    """

    def __init__(self, session_id: str, *, workspace_root: str | None = None) -> None:
        self.session_id = session_id
        self.state = "live"
        self.destroyed = False
        self.exec_log: list[list[str]] = []
        self.force_truncated = False
        self.workspace_root = workspace_root or tempfile.mkdtemp(prefix="langchain-inis-test-")
        Path(self.workspace_root).mkdir(parents=True, exist_ok=True)

    def cleanup(self) -> None:
        """Remove this session's own scratch content under `workspace_root`.

        Only removes what this backend itself created (everything under
        `workspace_root`), not `workspace_root` itself — when a caller
        passes an explicit shared `workspace_root` (e.g. the conformance
        suite's `_CONFORMANCE_WORKSPACE_ROOT`), that directory may be
        reused by more than one `LocalProcessInisBackend` instance and its
        own lifecycle is the caller's to manage.
        """
        for child in Path(self.workspace_root).iterdir():
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child, ignore_errors=True)
            else:
                child.unlink(missing_ok=True)

    def _under_workspace_root(self, sandbox_path: str) -> bool:
        root_prefix = self.workspace_root.rstrip("/") + "/"
        return sandbox_path == self.workspace_root or sandbox_path.startswith(root_prefix)

    def _real_path(self, sandbox_path: str) -> Path:
        # `sandbox_path` is already a real, absolute path on this machine
        # (see class docstring) — no virtual-root remapping needed.
        return Path(sandbox_path)

    def _run_sync(self, command: list[str], timeout_ms: int | None) -> dict:
        rewritten = command
        timeout_s = (timeout_ms / 1000) if timeout_ms else 30
        try:
            proc = subprocess.run(rewritten, capture_output=True, timeout=timeout_s)
            return {
                "stdout": proc.stdout.decode("utf-8", errors="replace"),
                "stderr": proc.stderr.decode("utf-8", errors="replace"),
                "exit_code": proc.returncode,
                "timed_out": False,
            }
        except subprocess.TimeoutExpired:
            return {"stdout": "", "stderr": "", "exit_code": -1, "timed_out": True}

    async def _run_async(self, command: list[str], timeout_ms: int | None) -> dict:
        rewritten = command
        proc = await asyncio.create_subprocess_exec(
            *rewritten,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            timeout_s = (timeout_ms / 1000) if timeout_ms else 30
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
            return {
                "stdout": stdout.decode("utf-8", errors="replace"),
                "stderr": stderr.decode("utf-8", errors="replace"),
                "exit_code": proc.returncode or 0,
                "timed_out": False,
            }
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return {"stdout": "", "stderr": "", "exit_code": -1, "timed_out": True}

    # -- shared request logic, parameterized by an already-fetched body -----

    def _exec_prefix(self) -> str:
        return f"/v1/sessions/{self.session_id}"

    def _handle_get_delete_common(self, path: str, method: str) -> httpx.Response | None:
        prefix = self._exec_prefix()
        if path == prefix and method == "GET":
            return httpx.Response(200, json={"session_id": self.session_id, "state": self.state})
        if path == prefix and method == "DELETE":
            self.destroyed = True
            return httpx.Response(204)
        if path == f"{prefix}/expose" and method == "POST":
            return None  # handled by caller (needs request body)
        return None

    def _record_exec(self, command: list[str]) -> None:
        if command[:2] in (["sh", "-lc"], ["bash", "-lc"]):
            self.exec_log.append(command)

    def _files_get(self, params: httpx.QueryParams) -> httpx.Response:
        sandbox_path = params["path"]
        op = params.get("op", "read")
        if not self._under_workspace_root(sandbox_path):
            return httpx.Response(400, json={"error": "path must be under /workspace", "code": "invalid_path"})
        real = self._real_path(sandbox_path)
        if op == "list":
            if not real.is_dir():
                return httpx.Response(404, json={"error": "not a directory", "code": "not_found"})
            return httpx.Response(200, json={"entries": sorted(p.name for p in real.iterdir())})
        if not real.is_file():
            return httpx.Response(404, json={"error": "file not found", "code": "not_found"})
        data = real.read_bytes()
        if params.get("encoding") == "base64":
            return httpx.Response(200, json={"content": base64.b64encode(data).decode("ascii")})
        return httpx.Response(200, json={"content": data.decode("utf-8", errors="replace")})

    def _files_put(self, body: dict) -> httpx.Response:
        if not self._under_workspace_root(body["path"]):
            return httpx.Response(400, json={"error": "path must be under /workspace", "code": "invalid_path"})
        real = self._real_path(body["path"])
        real.parent.mkdir(parents=True, exist_ok=True)
        content = body["content"]
        if body.get("encoding") == "base64":
            real.write_bytes(base64.b64decode(content))
        else:
            real.write_text(content)
        return httpx.Response(200, json={})

    def _files_delete(self, params: httpx.QueryParams) -> httpx.Response:
        sandbox_path = params["path"]
        real = self._real_path(sandbox_path)
        if real.is_dir():
            shutil.rmtree(real, ignore_errors=True)
        else:
            real.unlink(missing_ok=True)
        return httpx.Response(204)

    def _expose(self, body: dict) -> httpx.Response:
        port = body["port"]
        return httpx.Response(
            200,
            json={
                "session_id": self.session_id,
                "port": port,
                "preview_url": f"https://{self.session_id}-{port}.preview.inis.test",
            },
        )

    # -- sync handler (inis.Session / httpx.Client) --------------------------

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        method = request.method
        prefix = self._exec_prefix()

        common = self._handle_get_delete_common(path, method)
        if common is not None:
            return common

        if path == f"{prefix}/exec" and method == "POST":
            body = json.loads(request.content)
            command = body["command"]
            self._record_exec(command)
            result = self._run_sync(command, body.get("timeout_ms"))
            result["truncated"] = self.force_truncated
            return httpx.Response(200, json=result)

        if path == f"{prefix}/files" and method == "GET":
            return self._files_get(request.url.params)
        if path == f"{prefix}/files" and method == "PUT":
            return self._files_put(json.loads(request.content))
        if path == f"{prefix}/files" and method == "DELETE":
            return self._files_delete(request.url.params)
        if path == f"{prefix}/expose" and method == "POST":
            return self._expose(json.loads(request.content))

        return httpx.Response(500, json={"error": f"unhandled {method} {path}"})

    # -- async handler (inis.AsyncSession / httpx.AsyncClient) ---------------

    async def ahandler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        method = request.method
        prefix = self._exec_prefix()

        common = self._handle_get_delete_common(path, method)
        if common is not None:
            return common

        if path == f"{prefix}/exec" and method == "POST":
            body = json.loads(request.content)
            command = body["command"]
            self._record_exec(command)
            result = await self._run_async(command, body.get("timeout_ms"))
            result["truncated"] = self.force_truncated
            return httpx.Response(200, json=result)

        if path == f"{prefix}/files" and method == "GET":
            return self._files_get(request.url.params)
        if path == f"{prefix}/files" and method == "PUT":
            return self._files_put(json.loads(request.content))
        if path == f"{prefix}/files" and method == "DELETE":
            return self._files_delete(request.url.params)
        if path == f"{prefix}/expose" and method == "POST":
            return self._expose(json.loads(request.content))

        return httpx.Response(500, json={"error": f"unhandled {method} {path}"})


def attach_fake_session(session_id: str, *, workspace_root: str | None = None):
    """Returns (Session, LocalProcessInisBackend) wired together (sync)."""
    from inis import Session

    backend = LocalProcessInisBackend(session_id, workspace_root=workspace_root)
    session = Session.attach(session_id, base_url="https://fake.inis.test", token="test-token")
    session._client = httpx.Client(transport=httpx.MockTransport(backend.handler))
    return session, backend


def attach_fake_async_session(session_id: str, backend: LocalProcessInisBackend):
    """Returns an `AsyncSession` attached to `session_id`, sharing `backend`'s state."""
    from inis import AsyncSession

    session = AsyncSession.attach(session_id, base_url="https://fake.inis.test", token="test-token")
    session._client = httpx.AsyncClient(transport=httpx.MockTransport(backend.ahandler))
    return session


def attach_session_to_backend(session_id: str, backend: LocalProcessInisBackend) -> "Session":  # noqa: F821
    """Returns a (sync) `Session` re-attached to `session_id`, sharing an
    EXISTING `backend`'s state — the multi-attach twin of
    `attach_fake_session` (which always builds a brand-new backend). Use
    this when a real `.attach(session_id)` call must reconnect to state a
    previous `.create()`/`.attach()` already established, e.g. a fake
    `Client.sessions.attach()` implementation."""
    from inis import Session

    session = Session.attach(session_id, base_url="https://fake.inis.test", token="test-token")
    session._client = httpx.Client(transport=httpx.MockTransport(backend.handler))
    return session


def make_fake_inis_sandbox_backend(session_id: str, *, workspace_root: str | None = None):
    """Returns (InisSandboxBackend, LocalProcessInisBackend) fully wired for
    both the sync `execute()`/`upload_files()`/`download_files()` path and
    the async `aexecute()`/`aupload_files()`/`adownload_files()` path — the
    async twin is pre-attached with the fake transport so
    `InisSandboxBackend._aget_async_session()` never tries a real network
    call in a test.
    """
    from langchain_inis import InisSandboxBackend

    session, fake = attach_fake_session(session_id, workspace_root=workspace_root)
    sandbox = InisSandboxBackend(session)
    sandbox._async_session = attach_fake_async_session(session_id, fake)
    return sandbox, fake
