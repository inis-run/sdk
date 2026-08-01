"""Tests for InisError's structured error contract: every request
path funnels through client._raise_for_status, so exercising it directly
(rather than one specific SDK method) covers every call site — Session,
Client, and InisClient alike all raise the same InisError shape.
"""

from __future__ import annotations

import httpx
import pytest

from inis.client import InisError, _raise_for_status
from tests.conftest import error_response


def test_ok_response_does_not_raise():
    _raise_for_status(httpx.Response(200, json={"ok": True}))  # must not raise


def test_carries_code_status_and_message():
    resp = error_response(409, "session is paused", code="session_not_live")
    with pytest.raises(InisError) as exc_info:
        _raise_for_status(resp)
    err = exc_info.value
    assert err.code == "session_not_live"
    assert err.status == 409
    assert "session is paused" in str(err)
    assert err.response == {"error": "session is paused", "code": "session_not_live"}


def test_no_code_falls_back_to_generic_error_code():
    resp = error_response(500, "internal error")
    with pytest.raises(InisError) as exc_info:
        _raise_for_status(resp)
    assert exc_info.value.code == "error"


def test_no_body_falls_back_to_bare_status():
    resp = httpx.Response(503, text="")
    with pytest.raises(InisError) as exc_info:
        _raise_for_status(resp)
    err = exc_info.value
    assert err.code == "error"
    assert err.status == 503
    assert "503" in str(err)


# A code this SDK version has never seen must still come through with its
# exact raw value on InisError.code and inside
# InisError.response, not collapse into a message-only error just because
# this SDK doesn't recognise it yet.
def test_unknown_code_survives_round_trip():
    resp = error_response(422, "the future happened", code="some_brand_new_code_from_2027")
    with pytest.raises(InisError) as exc_info:
        _raise_for_status(resp)
    err = exc_info.value
    assert err.code == "some_brand_new_code_from_2027"
    assert err.response["code"] == "some_brand_new_code_from_2027"
    # Safe default: an unrecognised code with no Retry-After is not assumed
    # retryable.
    assert err.retryable is False


@pytest.mark.parametrize(
    "code",
    [
        "session_not_live",
        "session_unavailable",
        "session_node_unavailable",
        "rate_limited",
        "unavailable",
        "template_version_unavailable",
        "concurrency_limit_exceeded",
        "rate_limit_exceeded",
        "service_unavailable",
    ],
)
def test_known_retryable_codes(code):
    resp = error_response(503, "try again", code=code)
    with pytest.raises(InisError) as exc_info:
        _raise_for_status(resp)
    assert exc_info.value.retryable is True


@pytest.mark.parametrize(
    "code",
    [
        "validation",
        "unauthenticated",
        "forbidden",
        "not_found",
        "conflict",
        "session_ended",
        "archive_unavailable",
        "size_unavailable",
        "payload_too_large",
        "internal",
        "payment_required",
    ],
)
def test_known_non_retryable_codes(code):
    resp = error_response(400, "nope", code=code)
    with pytest.raises(InisError) as exc_info:
        _raise_for_status(resp)
    assert exc_info.value.retryable is False


# Retry-After must reach the caller, not just get read server-side and
# dropped.
def test_retry_after_reaches_caller():
    resp = error_response(503, "please retry", headers={"Retry-After": "5"})
    with pytest.raises(InisError) as exc_info:
        _raise_for_status(resp)
    err = exc_info.value
    assert err.retry_after == pytest.approx(5.0)
    # Presence of Retry-After marks it retryable even without a recognised
    # code — a stronger, independent signal than the static table.
    assert err.retryable is True


def test_retry_after_http_date_form():
    from datetime import datetime, timedelta, timezone
    from email.utils import format_datetime

    future = datetime.now(timezone.utc) + timedelta(seconds=30)
    resp = error_response(503, "please retry", headers={"Retry-After": format_datetime(future, usegmt=True)})
    with pytest.raises(InisError) as exc_info:
        _raise_for_status(resp)
    # Allow generous slack: we only care that it parsed to roughly 30s out,
    # not exact timing.
    assert 0 < exc_info.value.retry_after <= 31


def test_request_id_reaches_caller():
    resp = error_response(500, "internal error", headers={"X-Inis-Request-Id": "req_abc123"})
    with pytest.raises(InisError) as exc_info:
        _raise_for_status(resp)
    assert exc_info.value.request_id == "req_abc123"


def test_no_request_id_header_leaves_it_none():
    resp = error_response(500, "internal error")
    with pytest.raises(InisError) as exc_info:
        _raise_for_status(resp)
    assert exc_info.value.request_id is None


def test_non_json_body_still_raises_with_generic_code():
    resp = httpx.Response(502, text="<html>Bad Gateway</html>")
    with pytest.raises(InisError) as exc_info:
        _raise_for_status(resp)
    err = exc_info.value
    assert err.code == "error"
    assert err.status == 502
    assert err.response is None


def test_full_request_flow_raises_structured_inis_error(fake_http):
    """End-to-end: a real Client call (not _raise_for_status directly) hits
    the same structured InisError -- proves the wiring at the request-path
    level, not just the helper in isolation."""
    from inis.client import Client

    fake_http(
        lambda method, path, params, body: error_response(
            429,
            "daily runtime quota exhausted",
            code="rate_limited",
            headers={"Retry-After": "10"},
        )
    )
    client = Client(token="tok_abc", base_url="https://api.inis.run")
    with pytest.raises(InisError) as exc_info:
        client.sessions.get("sess_1")
    err = exc_info.value
    assert err.code == "rate_limited"
    assert err.status == 429
    assert err.retryable is True
    assert err.retry_after == pytest.approx(10.0)
