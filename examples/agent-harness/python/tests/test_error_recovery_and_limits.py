"""Mock-backed tests for error_recovery_and_limits.py.

demo_retry_on_capacity_limit needs no mocking at all — it exercises
call_with_recovery against a scripted InisError sequence, entirely local
Python. The other demos go through fake_backend to prove the documented,
source-verified error codes (not_found for a destroyed/bogus session,
bad_request for an over-hard-cap fork) without a live session."""

from __future__ import annotations

import pytest
from inis import Client, InisError

import error_recovery_and_limits as harness


def test_call_with_recovery_retries_then_succeeds():
    attempts = {"n": 0}

    def flaky():
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise InisError("x", code="rate_limited", status=429, retryable=True, retry_after=0.01)
        return "ok"

    assert harness.call_with_recovery(flaky, max_attempts=5) == "ok"
    assert attempts["n"] == 3


def test_call_with_recovery_does_not_retry_non_retryable_errors():
    def always_fails():
        raise InisError("x", code="bad_request", status=400, retryable=False)

    with pytest.raises(InisError):
        harness.call_with_recovery(always_fails, max_attempts=5)


def test_demo_retry_on_capacity_limit_is_fully_self_contained():
    """No fake_backend fixture used here on purpose — this demo makes no
    API call at all, which is the whole point (see the file's docstring
    on why a real 429 isn't forced)."""
    harness.demo_retry_on_capacity_limit()


def test_demo_gone_session_reports_not_found_for_destroyed_and_bogus_ids(fake_backend, capsys):
    client = Client()
    harness.demo_gone_session(client)
    out = capsys.readouterr().out
    assert out.count("code='not_found'") == 2


def test_demo_request_level_limit_hits_the_hard_fork_cap(fake_backend):
    client = Client()
    session_id = harness.demo_request_level_limit(client)
    assert session_id in fake_backend.by_id
    assert not fake_backend.by_id[session_id].destroyed  # cleanup is main()'s job, not this demo's


def test_full_scenario_cleans_up_even_though_some_demos_are_not_live(fake_backend):
    exit_code = harness.main()
    assert exit_code == 0
    assert fake_backend.by_id
    assert all(rec.destroyed for rec in fake_backend.by_id.values())
