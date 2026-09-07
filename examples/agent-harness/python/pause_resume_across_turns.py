#!/usr/bin/env python3
"""Pause/resume across turns — persist the session ID, pause at the end of
one host process, and resume from a GENUINELY SEPARATE
OS process (not just a second in-process `Client`, unlike the reconnect
step in examples/quickstart, which documents why it stayed in-process for
that simpler scenario). This script re-execs itself as a child process to
play the part of "the next turn, handled later by a different process".

## What each turn does

- **Turn 1** (this process): creates a session, writes a nonce, explicitly
  pauses it (`session.pause()`) rather than waiting for idle timeout —
  the "end of my host process/turn" moment — and hands the session ID to a
  freshly spawned child process via argv (never via env, a file, or
  anything that would land the ID somewhere logged as a secret would be;
  a session ID isn't a credential, but the API key genuinely never leaves
  this parent process either way).
- **Turn 2** (the child process, `--turn2 SESSION_ID`): a separate `python3`
  process with its own interpreter, no shared globals, no shared `Client`.
  It attaches by ID, calls `session.resume()` EXPLICITLY once (the
  documented call, even though docs-site/content/docs/session-lifecycle.mdx
  is clear that any op — exec, files, expose — wakes a paused session on
  its own; this shows both: the explicit call succeeds, and a subsequent
  op would have worked either way), reads the nonce back, appends a second
  value, and exits.
- **Turn 3** (back in this parent process): confirms the child's write is
  visible, then demonstrates the OTHER half of the pause/resume contract —
  "cover expiry and cleanup when the next turn never arrives" — using a
  SECOND, throwaway session with a short `paused_ttl_ms` (auto-destroy
  after sitting paused that long). We can't actually wait out a real TTL in
  a short-running example, so this explicitly destroys that session itself
  and says so, rather than pretending to have observed a real expiry.

Cleanup: the turn-1/2/3 session is destroyed by the parent process at the
end (it's the owner — it created the session), success or failure. The
short-TTL throwaway session in turn 3 is destroyed immediately after
demonstrating the knob, for the reason above.

Run:
    export INIS_API_KEY=...
    pip install inis
    python pause_resume_across_turns.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid

from inis import Client, InisError

NONCE_PATH = "/workspace/pause-resume-nonce.txt"


def run_turn1(client: Client) -> tuple[str, str]:
    """Create a session, write a nonce, and pause it explicitly — the end
    of this host process's turn. Returns (session_id, nonce)."""
    session = client.sessions.create(
        idle_timeout_ms=120_000,
        max_lifetime_ms=10 * 60_000,
        egress_default="deny",
    )
    try:
        nonce = uuid.uuid4().hex
        session.write_file(NONCE_PATH, nonce)
        print(f"turn 1 (parent process, pid={os.getpid()}): created {session.session_id}, wrote nonce {nonce!r}")
        info = session.pause()
        print(f"  paused explicitly at end of turn 1 (state={info.state})")
        return session.session_id, nonce
    finally:
        session.close()  # local transport only; the session stays paused server-side


def run_turn2_in_child_process(session_id: str, nonce: str) -> dict:
    """Spawn an actual second `python3` process — no shared interpreter
    state with the parent whatsoever — and have IT resume and extend the
    session. Returns the child's reported observations, parsed from its
    stdout."""
    print(f"turn 2: spawning a separate OS process to resume {session_id}")
    result = subprocess.run(
        [sys.executable, __file__, "--turn2", session_id, nonce],
        capture_output=True,
        text=True,
        timeout=60,
    )
    sys.stderr.write(result.stderr)
    if result.returncode != 0:
        raise RuntimeError(f"turn 2 child process exited {result.returncode}")
    return json.loads(result.stdout.strip().splitlines()[-1])


def turn2_child_main(session_id: str, expected_nonce: str) -> int:
    """Entry point when THIS file is re-invoked as the turn-2 child process
    (see --turn2 handling in __main__ below). Prints exactly one JSON line
    to stdout as its result contract with the parent; everything else goes
    to stderr so the parent's json.loads of the last stdout line stays
    simple."""
    print(f"turn 2 (child process, pid={os.getpid()}): attaching to {session_id}", file=sys.stderr)
    client = Client()
    session = client.sessions.attach(session_id)
    try:
        # Explicit resume — documented, even though wake-on-op would do
        # this for us on the very next call below regardless.
        info = session.resume()
        print(f"  resumed explicitly (state={info.state})", file=sys.stderr)
        got_nonce = session.read_file(NONCE_PATH)
        if isinstance(got_nonce, bytes):
            got_nonce = got_nonce.decode()
        if got_nonce != expected_nonce:
            raise RuntimeError(f"nonce mismatch: expected {expected_nonce!r}, got {got_nonce!r}")
        second_value = uuid.uuid4().hex
        session.write_file(NONCE_PATH, f"{got_nonce}:{second_value}")
        print(f"  read back {got_nonce!r}, appended {second_value!r}", file=sys.stderr)
        print(json.dumps({"nonce": got_nonce, "second_value": second_value}))
        return 0
    except InisError as e:
        print(f"turn 2 API error: {e} (code={e.code})", file=sys.stderr)
        return 1
    except RuntimeError as e:
        print(f"turn 2 scenario failed: {e}", file=sys.stderr)
        return 1
    finally:
        session.close()
        client.close()


def demonstrate_paused_ttl_cleanup(client: Client) -> None:
    """The other half of "pause/resume across turns": what happens if the
    next turn NEVER arrives. `paused_ttl_ms` is the auto-destroy backstop —
    set here short to show the knob, but a real TTL wait is too slow for a
    quick example, so this session is destroyed explicitly right after,
    not left to actually expire."""
    session = client.sessions.create(
        idle_timeout_ms=60_000,
        max_lifetime_ms=5 * 60_000,
        paused_ttl_ms=60_000,  # would auto-destroy 60s after pausing
        egress_default="deny",
    )
    session_id = session.session_id
    try:
        print(f"turn 3: created {session_id} with paused_ttl_ms=60000 (auto-destroy backstop if no next turn)")
        session.pause()
        print("  paused it — in production this is where you'd stop if the conversation ended here.")
        print("  NOT waiting out the real 60s TTL in this example; destroying it explicitly instead:")
        session.destroy()  # note: destroy() clears session.session_id, hence the local above
        print(f"  session {session_id} destroyed (this example's own cleanup, standing in for the TTL firing)")
    finally:
        session.close()


def main() -> int:
    if not os.environ.get("INIS_API_KEY"):
        print(
            "INIS_API_KEY is not set. Export it before running this example — "
            "never put it in code, a file, or a session/guest command.",
            file=sys.stderr,
        )
        return 1

    client = Client()
    owned_session_id: str | None = None
    try:
        owned_session_id, nonce = run_turn1(client)

        child_result = run_turn2_in_child_process(owned_session_id, nonce)
        print(f"turn 2 result (from the child process's stdout): {child_result}")

        # turn 3, back in the parent: confirm the child process's write
        # survived the process boundary. The session is still live from
        # turn 2's resume at this point (idle_timeout_ms=120_000 hasn't
        # elapsed) — reattaching here works regardless of whether it finds
        # the session live or paused, which is the point of wake-on-op.
        print(f"turn 3 (parent process, pid={os.getpid()}): reattaching to {owned_session_id}")
        session = client.sessions.attach(owned_session_id)
        try:
            final_value = session.read_file(NONCE_PATH)
            if isinstance(final_value, bytes):
                final_value = final_value.decode()
            expected = f"{child_result['nonce']}:{child_result['second_value']}"
            if final_value != expected:
                raise RuntimeError(f"expected {expected!r}, got {final_value!r}")
            print(f"  confirmed: {final_value!r} — turn 2's write survived a real process boundary and a pause")
        finally:
            session.close()

        demonstrate_paused_ttl_cleanup(client)
        return 0
    except InisError as e:
        print(f"inis.run API error: {e} (code={e.code})", file=sys.stderr)
        return 1
    except RuntimeError as e:
        print(f"scenario failed: {e}", file=sys.stderr)
        return 1
    finally:
        if owned_session_id is not None:
            attached = client.sessions.attach(owned_session_id)
            try:
                attached.destroy()
                print(f"session {owned_session_id} destroyed (owner cleanup)")
            finally:
                attached.close()
        client.close()


if __name__ == "__main__":
    if len(sys.argv) >= 4 and sys.argv[1] == "--turn2":
        raise SystemExit(turn2_child_main(sys.argv[2], sys.argv[3]))
    raise SystemExit(main())
