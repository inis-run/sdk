"""Mock-backed tests for branch_and_evaluate.py — proves fork's real
return shape is used correctly, that fork_with_retry actually retries on
a retryable error, and that grade() picks the correct candidate purely
from observed exit_code/stdout."""

from __future__ import annotations

import pytest
from inis import Client, ForkResult, InisError

import branch_and_evaluate as harness


def test_fork_returns_a_list_of_child_ids_not_an_iterable_of_sessions(fake_backend):
    """The exact regression the issue called out: children is a list of
    plain session ID strings, not an iterable of ready-to-use sessions."""
    client = Client()
    parent = client.sessions.create(idle_timeout_ms=1000)
    result = parent.fork(count=2)
    assert isinstance(result, ForkResult)
    assert result.parent_session_id == parent.session_id
    assert isinstance(result.children, list)
    assert all(isinstance(c, str) for c in result.children)
    assert len(result.children) == 2


def test_grade_picks_the_correct_candidate_only():
    correct = harness.CandidateResult(name="a", stdout=str(harness.EXPECTED), exit_code=0, duration_ms=1)
    wrong = harness.CandidateResult(name="b", stdout="not the right answer", exit_code=0, duration_ms=1)
    crashed = harness.CandidateResult(name="c", stdout="", exit_code=1, duration_ms=1)

    assert harness.grade([wrong, crashed]) is None
    assert harness.grade([wrong, correct, crashed]) is correct


def test_fork_with_retry_backs_off_on_retryable_then_succeeds(fake_backend, monkeypatch):
    client = Client()
    session = client.sessions.create(idle_timeout_ms=1000)

    attempts = {"n": 0}
    real_fork = type(session).fork

    def flaky_fork(self, count=1):
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise InisError("503: capacity", code="service_unavailable", status=503, retryable=True, retry_after=0.01)
        return real_fork(self, count=count)

    monkeypatch.setattr(type(session), "fork", flaky_fork)
    result = harness.fork_with_retry(session, 2, max_attempts=5)
    assert attempts["n"] == 3
    assert len(result.children) == 2


def test_fork_with_retry_does_not_retry_a_non_retryable_error(fake_backend, monkeypatch):
    client = Client()
    session = client.sessions.create(idle_timeout_ms=1000)

    def always_bad_request(self, count=1):
        raise InisError("400: count exceeds hard cap of 16", code="bad_request", status=400, retryable=False)

    monkeypatch.setattr(type(session), "fork", always_bad_request)
    with pytest.raises(InisError) as excinfo:
        harness.fork_with_retry(session, 99, max_attempts=5)
    assert excinfo.value.code == "bad_request"


def test_branch_and_evaluate_end_to_end_cleans_up_parent_and_children(fake_backend):
    exit_code = harness.main()
    assert exit_code == 0
    # Every session this run touched (parent + every forked child) must
    # have been destroyed by cleanup, success or failure.
    assert fake_backend.by_id
    assert all(rec.destroyed for rec in fake_backend.by_id.values())


def test_cleanup_runs_even_when_grading_finds_no_winner(fake_backend, monkeypatch):
    monkeypatch.setattr(harness, "grade", lambda results: None)
    exit_code = harness.main()
    assert exit_code == 1
    assert fake_backend.by_id
    assert all(rec.destroyed for rec in fake_backend.by_id.values()), "cleanup must run even when no candidate wins"
