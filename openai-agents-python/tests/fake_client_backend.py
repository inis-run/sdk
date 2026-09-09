"""Fakes an entire `inis.AsyncClient` (not just one already-attached
session) for testing `InisSandboxClient.create()`/`.delete()`/`.resume()`.

`inis.AsyncSession` opens its own private `httpx.AsyncClient` per instance
(not the parent `AsyncClient`'s transport), so faking only one session's
transport (as `fake_backend.attach_fake_session` does) can't reach the
`POST /v1/sessions` call `sessions.create()` makes before we'd get a chance
to intervene. This monkeypatches `httpx.AsyncClient` itself, inside the
`inis.async_client` module only, so every transport it constructs — the
outer client's and each session's — is wired to one shared in-memory
registry of `LocalProcessInisBackend`s instead of the network.
"""

from __future__ import annotations

import itertools

import httpx

import inis.async_client as inis_async_client_module

from .fake_backend import LocalProcessInisBackend

_counter = itertools.count(1)


def attach_fake_client(monkeypatch):
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
        def __init__(self, *args, **kwargs) -> None:
            kwargs.pop("transport", None)
            super().__init__(*args, transport=httpx.MockTransport(dispatch), **kwargs)

    monkeypatch.setattr(inis_async_client_module.httpx, "AsyncClient", _FakeAsyncHttpxClient)

    client = inis_async_client_module.AsyncClient(token="test-token", base_url="https://fake.inis.test")
    return client, registry
