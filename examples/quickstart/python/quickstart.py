#!/usr/bin/env python3
"""inis.run canonical quickstart (Python) — the source of truth for every
first-run snippet (console, marketing, docs, adapter READMEs). If you're
fixing a quickstart snippet somewhere else in this repo, fix it here first,
then point the other surface at this file or match it — do not hand-edit a
divergent copy. See ../README.md for the full contract and scripts/
check-quickstart-surfaces.py for the CI drift check that enforces this.

Endpoint:  https://api.inis.run (INIS_BASE_URL env var to override)
Package:   inis — pip install inis (see sdk/python/pyproject.toml for the
           supported Python range and dependency pins)
Timeout:   120s per HTTP call by default (Client(timeout=...) to change) —
           separate from this session's own max_lifetime_ms/idle_timeout_ms
           below, which bound the *session*, not one HTTP request.
Egress:    deny-by-default — egress_default="deny" means the session cannot
           reach the network at all unless explicitly allowed (see
           EgressPolicy in sdk/python/inis/client.py).
TTL:       max_lifetime_ms hard-caps how long the session may stay live;
           idle_timeout_ms auto-pauses it after inactivity. Both are short
           here because this is a disposable demo — not production guidance.
Cleanup:   the session this script OWNS is destroyed in `with` block's
           __exit__ (Session.destroy()) on BOTH the success and the failure
           path. The reconnect step only ATTACHES to that same session by
           ID; attaching never destroys it — only the owner's cleanup does.

Run:
    export INIS_API_KEY=...    # never hardcode this, never put it in a
                                # guest command/file/fixture
    pip install inis
    python quickstart.py
"""

from __future__ import annotations

import os
import sys
import uuid

from inis import Client, InisError

NONCE_PATH = "/workspace/quickstart-nonce.txt"


def read_back_nonce(session, nonce: str, label: str) -> None:
    """Run a command that reads the nonce file back, proving the session's
    filesystem persisted since it was written — the core thing this
    quickstart demonstrates, twice (once same-handle, once reattached)."""
    result = session.exec(["cat", NONCE_PATH])
    if result.exit_code != 0 or nonce not in result.stdout:
        raise RuntimeError(
            f"{label}: nonce round-trip failed "
            f"(exit={result.exit_code} stdout={result.stdout!r} stderr={result.stderr!r})"
        )
    print(f"{label}: read back {result.stdout.strip()!r} — matches the nonce we wrote")


def main() -> int:
    if not os.environ.get("INIS_API_KEY"):
        print(
            "INIS_API_KEY is not set. Export it before running this example — "
            "never put it in code, a file, or a session/guest command.",
            file=sys.stderr,
        )
        return 1

    client = Client()  # resolves INIS_API_KEY / INIS_BASE_URL from the host env
    nonce = uuid.uuid4().hex
    session_id: str | None = None

    try:
        # 1. Create a short-lived session with deny-by-default egress. This
        #    process OWNS this session: it is responsible for destroying it,
        #    which the `with` block below does in __exit__ regardless of
        #    whether the body raises.
        with client.session(
            idle_timeout_ms=60_000,
            max_lifetime_ms=5 * 60_000,
            egress_default="deny",
        ) as session:
            session_id = session.session_id
            print(f"created session {session_id} (egress: deny-by-default)")

            # 2. Write a small nonce to the session's filesystem.
            session.write_file(NONCE_PATH, nonce)

            # 3. Run a SECOND command that reads it back — proves state
            #    persisted across exec() calls, not just within one call.
            read_back_nonce(session, nonce, "same-handle read-back")

            # 4. Persist the session ID somewhere the next process/request
            #    can read it from (host-side only — a DB row, a cache key;
            #    printing it here stands in for that).
            print(f"persisting session id for later reconnect: {session_id}")

            # 5. Simulate a later process reconnecting: attach to the SAME
            #    session purely by ID (not the original in-process handle),
            #    and prove it is still the same filesystem. This is the
            #    "owned-session" flow attaching to itself; a genuinely
            #    separate process would call
            #    `client.sessions.attach(session_id)` exactly the same way.
            reattached = client.sessions.attach(session_id)
            read_back_nonce(reattached, nonce, "post-reconnect read-back")
            # attach() never destroys the session it binds to — only the
            # owner's `with` block does, below, once this scope exits.

        print(f"session {session_id} destroyed (owner cleanup ran in __exit__)")
        return 0
    except InisError as e:
        print(f"inis.run API error: {e}", file=sys.stderr)
        return 1
    except RuntimeError as e:
        print(f"quickstart scenario failed: {e}", file=sys.stderr)
        return 1
    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
