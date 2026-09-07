"""Mock-backed tests for persistent_tool_loop.py — proves the harness
invariants (one session per thread, reattach-by-ID across a fresh Client,
bounded tool surface) against fake_backend, not a live inis.run session."""

from __future__ import annotations

from inis import Client

import persistent_tool_loop as harness


def test_get_or_create_session_reuses_the_same_session_for_one_thread(fake_backend):
    client = Client()
    store = harness.ThreadSessionStore()

    first = client.sessions.create(idle_timeout_ms=1000)
    store.put("thread-1", first.session_id)

    # A later call for the SAME thread must attach to the same session,
    # not create a second one — the core "one session per thread" rule.
    second = harness.get_or_create_session(client, store, "thread-1")
    assert second.session_id == first.session_id

    # A DIFFERENT thread gets its own, distinct session.
    third = harness.get_or_create_session(client, store, "thread-2")
    assert third.session_id != first.session_id


def test_bounded_tool_surface_only_exposes_exec_and_files(fake_backend):
    client = Client()
    session = client.sessions.create(idle_timeout_ms=1000)
    tools = harness.BoundedToolSurface(session)

    # The model-facing surface never exposes lifecycle methods.
    for forbidden in ("pause", "resume", "destroy", "fork"):
        assert not hasattr(tools, forbidden)

    tools.write_file(harness.STATE_PATH, '{"counter": 1}')
    assert harness.read_counter(tools) == 1


def test_persistent_tool_loop_end_to_end(fake_backend):
    """Runs the real main() against the fake backend end to end: three
    turns, the middle one through a second Client, proving filesystem AND
    background-process state survive both a reconnect and (per the fake
    backend) a second in-process Client standing in for a second host
    process."""
    exit_code = harness.main()
    assert exit_code == 0

    # The one session this run owned must have been destroyed by cleanup.
    assert fake_backend.by_id
    destroyed_states = [rec.destroyed for rec in fake_backend.by_id.values()]
    assert all(destroyed_states), "owned session was not destroyed by cleanup"


def test_cleanup_runs_even_when_a_turn_raises(fake_backend, monkeypatch):
    """A failure mid-conversation must still destroy the one session this
    script owns — proving the finally block, not just the happy path."""

    def boom(*args, **kwargs):
        raise RuntimeError("simulated turn failure")

    monkeypatch.setattr(harness, "read_counter", boom)
    exit_code = harness.main()
    assert exit_code == 1
    assert fake_backend.by_id, "expected a session to have been created before it failed"
    assert all(rec.destroyed for rec in fake_backend.by_id.values()), "cleanup must run on the error path too"
