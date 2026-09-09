"""A fake inis.run HTTP backend for deterministic tests.

Mirrors the core Python SDK's own `sdk/python/tests/interpreter_fake_guest.py`
(`FakeInterpreterGuest`) technique: rather than faking the wire protocol with
canned responses, this drives the *real* `inis._kernel_script.run_cell()`
against an in-memory `globals` dict standing in for the kernel process's
memory -- so these tests exercise this adapter's real code
(`InisCodeActProvider` / `InisExecuteCodeTool`), the real
`inis.AsyncSession`/`AsyncClient`, and the real kernel cell-execution logic;
only the guest VM itself (filesystem, process lifecycle, exec dispatch) is
faked.

Session create/destroy and the per-session interpreter backend are wired
through one monkeypatched `httpx.AsyncClient` inside `inis.async_client` --
the same technique `inis_openai_agents`' own `tests/fake_client_backend.py`
uses, and for the same reason: `AsyncSession`/`AsyncClient` each open their
own private `httpx.AsyncClient`, so faking only one already-attached
session's transport can't reach a `POST /v1/sessions` call `.create()` makes
before a test gets a chance to intervene.
"""

from __future__ import annotations

import base64
import itertools
import json
import re
import tempfile
from typing import Any

import httpx

import inis.async_client as inis_async_client_module
from inis import _kernel_script as kernel

_counter = itertools.count(1)


class KernelSessionBackend:
    """One fake inis.run session: real kernel cell-execution logic (via
    `inis._kernel_script.run_cell`) against an in-memory `globals` dict,
    real in-memory files, and named-process lifecycle for the
    `inis-kernel` process specifically -- the only process this adapter
    ever starts."""

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self.destroyed = False
        self.process_state: str | None = None  # None | "running" | "exited"
        self.files: dict[str, bytes] = {}
        self.globals: dict[str, Any] = {"__name__": "__main__"}
        kernel.ART_DIR = tempfile.mkdtemp()
        # When True, the next wait_command exec() answers with the raw
        # "no result yet" marker without touching the request/result files
        # or the process's actual state -- a genuine "kernel hasn't
        # answered in time" timeout, distinct from a dead-kernel
        # auto-restart. Reset itself after firing once.
        self.force_timeout_once = False

    async def handler(self, request: httpx.Request) -> httpx.Response:
        method = request.method
        path = request.url.path
        prefix = f"/v1/sessions/{self.session_id}"

        if path == prefix and method == "DELETE":
            self.destroyed = True
            return httpx.Response(204)
        if path == prefix and method == "GET":
            return httpx.Response(200, json={"session_id": self.session_id, "state": "live"})
        if path == f"{prefix}/exec" and method == "POST":
            body = json.loads(request.content)
            command = body["command"]
            script = command if isinstance(command, str) else command[-1]
            return self._exec(script)
        if path == f"{prefix}/files" and method == "PUT":
            body = json.loads(request.content)
            content = body["content"]
            encoding = body.get("encoding", "text")
            self.files[body["path"]] = base64.b64decode(content) if encoding == "base64" else content.encode()
            return httpx.Response(200, json={})
        if path == f"{prefix}/files" and method == "GET":
            params = request.url.params
            fpath = params["path"]
            if fpath not in self.files:
                return httpx.Response(404, json={"error": "not found", "code": "not_found"})
            content = self.files[fpath]
            if params.get("encoding") == "base64":
                return httpx.Response(200, json={"content": base64.b64encode(content).decode()})
            return httpx.Response(200, json={"content": content.decode("utf-8", errors="replace")})
        if path == f"{prefix}/processes" and method == "POST":
            self.process_state = "running"
            self.globals = {"__name__": "__main__"}
            return httpx.Response(200, json={"name": "inis-kernel", "state": "running", "pid": 111})
        if path == f"{prefix}/processes/inis-kernel" and method == "GET":
            if self.process_state is None:
                return httpx.Response(404, json={"error": "process not found", "code": "not_found"})
            return httpx.Response(200, json={"name": "inis-kernel", "state": self.process_state})
        if path == f"{prefix}/processes/inis-kernel" and method == "DELETE":
            had = self.process_state
            self.process_state = "exited"
            return httpx.Response(
                200,
                json={"name": "inis-kernel", "state": "exited", "exit_code": 143 if had == "running" else None},
            )
        return httpx.Response(500, json={"error": f"unhandled {method} {path}"})

    def _exec_result(self, stdout: str) -> httpx.Response:
        return httpx.Response(
            200, json={"stdout": stdout, "stderr": "", "exit_code": 0, "duration_ms": 1, "timed_out": False}
        )

    def _exec(self, script: str) -> httpx.Response:
        if script.startswith("mkdir -p"):
            return self._exec_result("")

        m = re.search(r"mv '([^']+)' '([^']+)'", script)
        if not m:
            return self._exec_result("")
        tmp_path = m.group(1)
        timeout_marker = json.dumps({"__inis_timeout__": True})

        if self.force_timeout_once:
            self.force_timeout_once = False
            return self._exec_result(timeout_marker)

        raw = self.files.pop(tmp_path, None)
        if raw is None or self.process_state != "running":
            return self._exec_result(timeout_marker)

        req = json.loads(raw.decode())
        results = kernel.run_cell(req["code"], req["id"], self.globals)
        for r in results:
            if r.get("type") == "image":
                with open(r["path"], "rb") as f:
                    self.files[r["path"]] = f.read()
        return self._exec_result(json.dumps({"id": req["id"], "results": results}))


def attach_fake_client(monkeypatch) -> tuple[Any, dict[str, KernelSessionBackend]]:
    """Monkeypatch every `httpx.AsyncClient` `inis.async_client` constructs
    (the outer `AsyncClient`'s and each `AsyncSession`'s own) to route to
    one shared in-memory registry of `KernelSessionBackend`s.

    Returns `(AsyncClient, registry)`.
    """
    registry: dict[str, KernelSessionBackend] = {}

    async def dispatch(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/sessions" and request.method == "POST":
            session_id = f"sess_fake_{next(_counter)}"
            registry[session_id] = KernelSessionBackend(session_id)
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


def preregister_session(registry: dict[str, KernelSessionBackend], session_id: str) -> KernelSessionBackend:
    """Register a session as already existing, for `.wrap()`/`.attach()`
    tests that never call `.create()`."""
    backend = registry[session_id] = KernelSessionBackend(session_id)
    return backend
