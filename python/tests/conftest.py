"""Shared fixtures for mocked-HTTP tests of the Python SDK.

All HTTP calls in inis.client funnel through httpx.Client's get/post/put/
request methods, which in turn all call httpx.Client.request(...) under the
hood. Patching that single method lets us intercept every call the SDK makes
without touching source or reaching the network.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

import httpx
import pytest


@dataclass
class RecordedRequest:
    method: str
    url: str
    params: dict[str, Any] | None
    json_body: Any
    headers: dict[str, str] | None

    @property
    def path(self) -> str:
        """URL path with any query string stripped."""
        return str(self.url).split("?", 1)[0]


@dataclass
class FakeTransport:
    """Records every call and dispatches to a caller-supplied handler.

    `handler(method, path, params, json_body) -> httpx.Response`
    """

    handler: Callable[[str, str, dict[str, Any] | None, Any], httpx.Response]
    calls: list[RecordedRequest] = field(default_factory=list)

    def __call__(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        params = kwargs.get("params")
        json_body = kwargs.get("json")
        headers = kwargs.get("headers")
        record = RecordedRequest(
            method=method, url=str(url), params=params, json_body=json_body, headers=headers
        )
        self.calls.append(record)
        return self.handler(method, record.path, params, json_body)


@pytest.fixture
def fake_http(monkeypatch: pytest.MonkeyPatch) -> Callable[[Callable], FakeTransport]:
    """Install a FakeTransport as the target for every httpx.Client.request call.

    Usage:
        transport = fake_http(lambda method, path, params, body: httpx.Response(200, json={...}))
        ... exercise SDK code ...
        assert transport.calls[0].method == "POST"
    """

    def _install(
        handler: Callable[[str, str, dict[str, Any] | None, Any], httpx.Response],
    ) -> FakeTransport:
        transport = FakeTransport(handler=handler)

        def fake_request(self: httpx.Client, method: str, url: Any, **kwargs: Any) -> httpx.Response:
            return transport(method, url, **kwargs)

        monkeypatch.setattr(httpx.Client, "request", fake_request)
        return transport

    return _install


@pytest.fixture
def fake_http_async(monkeypatch: pytest.MonkeyPatch) -> Callable[[Callable], FakeTransport]:
    """Async twin of fake_http: patches httpx.AsyncClient.request instead of
    httpx.Client.request, for exercising inis.async_client (AsyncClient /
    AsyncSession). Same FakeTransport, same handler signature — only the
    monkeypatched method differs.
    """

    def _install(
        handler: Callable[[str, str, dict[str, Any] | None, Any], httpx.Response],
    ) -> FakeTransport:
        transport = FakeTransport(handler=handler)

        async def fake_request(
            self: httpx.AsyncClient, method: str, url: Any, **kwargs: Any
        ) -> httpx.Response:
            return transport(method, url, **kwargs)

        monkeypatch.setattr(httpx.AsyncClient, "request", fake_request)
        return transport

    return _install


def json_response(status_code: int, body: Any) -> httpx.Response:
    return httpx.Response(status_code, json=body)


def error_response(
    status_code: int,
    error: str,
    *,
    code: str | None = None,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    """Build an error httpx.Response. `code` and `headers` are optional so
    every pre-existing call site -- which only ever cared about
    the message -- keeps working unchanged."""
    body: dict[str, Any] = {"error": error}
    if code is not None:
        body["code"] = code
    return httpx.Response(status_code, json=body, headers=headers)
