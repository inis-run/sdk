from __future__ import annotations

import pytest

from fake_backend import fake_backend  # noqa: F401 - re-exported as a pytest fixture


@pytest.fixture(autouse=True)
def fake_inis_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every example's Client() resolves INIS_API_KEY from the host env.
    This is an obviously-fake value — fake_backend never makes a real HTTP
    call, so it is never sent anywhere; it exists purely so Client()
    doesn't raise "INIS_API_KEY is required" before a test even starts."""
    monkeypatch.setenv("INIS_API_KEY", "test-key-not-real-no-network-calls-made")
    monkeypatch.setenv("INIS_BASE_URL", "https://inis.invalid.test")
