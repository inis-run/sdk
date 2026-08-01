"""Mock-backed tests for pause_resume_across_turns.py.

main() re-execs itself as a genuinely separate OS process for turn 2 (the
whole point of the real file — proving pause/resume survives an actual
host-process boundary, not just a second in-process Client). That re-exec
can't run against fake_backend: a subprocess is a fresh Python process
with its own real `inis.Client()`, untouched by this process's
monkeypatches. So these tests call the file's own functions directly
instead of shelling out — `turn2_child_main` is itself a plain function
that attaches and resumes; calling it here proves the SAME logic the real
child process runs, just without forking a real subprocess for it."""

from __future__ import annotations

from inis import Client

import pause_resume_across_turns as harness


def test_run_turn1_creates_and_explicitly_pauses(fake_backend):
    client = Client()
    session_id, nonce = harness.run_turn1(client)
    rec = fake_backend.by_id[session_id]
    assert rec.state == "paused"
    assert rec.files[harness.NONCE_PATH] == nonce


def test_turn2_child_main_resumes_and_extends_the_nonce(fake_backend, capsys):
    client = Client()
    session_id, nonce = harness.run_turn1(client)

    exit_code = harness.turn2_child_main(session_id, nonce)
    assert exit_code == 0

    rec = fake_backend.by_id[session_id]
    assert rec.state == "live", "explicit resume() must have woken the session"
    assert rec.files[harness.NONCE_PATH].startswith(f"{nonce}:")

    printed = capsys.readouterr().out.strip().splitlines()[-1]
    import json

    payload = json.loads(printed)
    assert payload["nonce"] == nonce


def test_turn2_child_main_reports_a_nonce_mismatch_as_failure(fake_backend):
    client = Client()
    session_id, nonce = harness.run_turn1(client)
    exit_code = harness.turn2_child_main(session_id, "not-the-real-nonce")
    assert exit_code == 1


def test_demonstrate_paused_ttl_cleanup_destroys_its_own_session(fake_backend):
    client = Client()
    before_ids = set(fake_backend.by_id)
    harness.demonstrate_paused_ttl_cleanup(client)
    new_ids = set(fake_backend.by_id) - before_ids
    assert len(new_ids) == 1
    (new_id,) = new_ids
    assert fake_backend.by_id[new_id].destroyed
