"""Mock-backed tests for concurrent_conversations.py — proves two
conversations run concurrently (via real OS threads against the fake
backend) never share a session, that each conversation's own cleanup runs
independent of the other, and that the isolation check itself is
load-bearing (it actually fails when sessions collide, not just when
nothing is wrong)."""

from __future__ import annotations

import dataclasses

import concurrent_conversations as harness


def test_run_conversation_creates_its_own_session_and_cleans_up(fake_backend):
    proof = harness.run_conversation("alice")
    assert proof.session_id in fake_backend.by_id
    assert fake_backend.by_id[proof.session_id].destroyed, "run_conversation must destroy its own session"
    assert f"secret-{proof.convo_id}.txt" in proof.visible_files
    # The background process was killed before the session was destroyed,
    # but list_processes() was captured BEFORE the kill — it should still
    # show up in what this conversation itself observed.
    assert proof.process_name in proof.visible_process_names


def test_two_conversations_run_concurrently_and_never_share_a_session(fake_backend):
    exit_code = harness.main()
    assert exit_code == 0
    # Every session created across the whole run must have been destroyed
    # by its own owner's cleanup.
    assert fake_backend.by_id
    assert all(rec.destroyed for rec in fake_backend.by_id.values())


def test_isolation_check_actually_fires_on_a_forced_collision(fake_backend, monkeypatch):
    """The isolation assertions in main() must be load-bearing: force both
    conversations' proofs onto the SAME session ID and confirm main()
    reports failure, rather than the check being a no-op that would pass
    regardless of what run_conversation returns."""
    real_run_conversation = harness.run_conversation

    def colliding(convo_id: str) -> harness.ConversationProof:
        proof = real_run_conversation(convo_id)
        return dataclasses.replace(proof, session_id="ses_forced_collision")

    monkeypatch.setattr(harness, "run_conversation", colliding)
    exit_code = harness.main()
    assert exit_code == 1, "main() must detect and fail on a forced session collision"
