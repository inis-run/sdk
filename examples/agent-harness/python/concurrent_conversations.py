#!/usr/bin/env python3
"""Concurrent customer conversations (#1237, epic #1229) — run at least two
conversations truly simultaneously and PROVE their files, background
processes, and cleanup can't cross, rather than asserting it.

No module-global session variable anywhere: `run_conversation` below is a
plain function that creates its OWN session locally and returns everything
the caller needs to verify isolation — nothing about one conversation's
session is visible to another except by literally reading the other's
return value, which this script does on purpose (from the host, after both
finish) specifically to prove neither conversation could see it FROM
INSIDE its own sandbox.

Concurrency is real, not simulated: both conversations run from separate
threads against separate sessions (separate VMs), each thread using its
own `Client`. Wall-clock proof follows the same shape as
branch_and_evaluate.py's fork-candidate timing check.

Cleanup: each conversation owns exactly one session and destroys it in its
own `finally`, independent of whether the OTHER conversation succeeded or
failed — one conversation's crash must never leak or double-destroy the
other's session.

Run:
    export INIS_API_KEY=...
    pip install inis
    python concurrent_conversations.py
"""

from __future__ import annotations

import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass

from inis import Client, InisError

WORKSPACE_DIR = "/workspace"


@dataclass
class ConversationProof:
    convo_id: str
    session_id: str
    secret_path: str
    secret_value: str
    process_name: str
    visible_files: list[str]
    visible_process_names: list[str]
    duration_s: float


def run_conversation(convo_id: str) -> ConversationProof:
    """Everything a customer conversation needs, entirely local to this
    call: its own Client, its own session, its own secret file, its own
    named background process. Nothing here is a module-level or otherwise
    shared mutable session reference — call this twice concurrently from
    separate threads and the two invocations share nothing but the Python
    import, not any state."""
    start = time.monotonic()
    client = Client()
    secret_path = f"{WORKSPACE_DIR}/secret-{convo_id}.txt"
    secret_value = f"only-{convo_id}-should-see-this"
    process_name = f"proc-{convo_id}"

    with client.session(
        idle_timeout_ms=60_000,
        max_lifetime_ms=5 * 60_000,
        egress_default="deny",
    ) as session:
        session_id = session.session_id  # captured now: destroy() clears session.session_id on __exit__
        session.write_file(secret_path, secret_value)
        # A uniquely-named background process, distinguishable from the
        # other conversation's, so list_processes() isolation is provable
        # too, not just files.
        session.start_process(process_name, "sleep 30", keep_alive=True)
        # Deliberate work to make concurrency visible in wall-clock time,
        # same technique as branch_and_evaluate.py.
        session.exec(["sleep", "2"])

        visible_files = session.list_files(WORKSPACE_DIR)
        visible_process_names = [p.name for p in session.list_processes()]

        session.kill_process(process_name)

    client.close()
    return ConversationProof(
        convo_id=convo_id,
        session_id=session_id or "",
        secret_path=secret_path,
        secret_value=secret_value,
        process_name=process_name,
        visible_files=visible_files,
        visible_process_names=visible_process_names,
        duration_s=time.monotonic() - start,
    )


def main() -> int:
    if not os.environ.get("INIS_API_KEY"):
        print(
            "INIS_API_KEY is not set. Export it before running this example — "
            "never put it in code, a file, or a session/guest command.",
            file=sys.stderr,
        )
        return 1

    convo_ids = ["alice", "bob"]

    try:
        start = time.monotonic()
        results: dict[str, ConversationProof] = {}
        with ThreadPoolExecutor(max_workers=len(convo_ids)) as pool:
            futures = {pool.submit(run_conversation, cid): cid for cid in convo_ids}
            for future in as_completed(futures):
                proof = future.result()
                results[proof.convo_id] = proof
        elapsed = time.monotonic() - start

        print(f"ran {len(convo_ids)} conversations concurrently in {elapsed:.1f}s wall-clock:")
        for cid in convo_ids:
            p = results[cid]
            print(
                f"  {cid}: session={p.session_id} own_files={p.visible_files} "
                f"own_processes={p.visible_process_names} ({p.duration_s:.1f}s)"
            )

        # Proof #1: genuinely concurrent, not sequential. Each does a 2s
        # exec plus setup/teardown overhead; if this ran sequentially the
        # combined wall time would be roughly double any one conversation's
        # own duration.
        max_single = max(p.duration_s for p in results.values())
        if elapsed >= max_single + min(p.duration_s for p in results.values()) * 0.8:
            raise RuntimeError(
                f"expected overlap: total {elapsed:.1f}s should be close to the "
                f"slowest single conversation's {max_single:.1f}s, not their sum"
            )

        # Proof #2: cross-contamination check, from the HOST's view (not
        # from inside either sandbox — that's the point: this is what an
        # operator can observe from outside, and it should be the same
        # answer either way). Each conversation's own file/process listing
        # must show only its own secret file and its own process name.
        for cid in convo_ids:
            other = next(c for c in convo_ids if c != cid)
            mine = results[cid]
            theirs = results[other]
            mine_basename = os.path.basename(mine.secret_path)
            theirs_basename = os.path.basename(theirs.secret_path)
            if mine_basename not in mine.visible_files:
                raise RuntimeError(f"{cid}'s own secret file should be visible to it, got {mine.visible_files}")
            if theirs_basename in mine.visible_files:
                raise RuntimeError(
                    f"ISOLATION VIOLATION: {cid}'s session saw {other}'s secret file {theirs_basename!r}"
                )
            if theirs.process_name in mine.visible_process_names:
                raise RuntimeError(
                    f"ISOLATION VIOLATION: {cid}'s session saw {other}'s process {theirs.process_name!r}"
                )
            if mine.session_id == theirs.session_id:
                raise RuntimeError("both conversations ended up on the SAME session — not isolated at all")

        print(
            "isolation confirmed: each conversation saw only its own file and process; "
            "sessions were distinct throughout"
        )
        return 0
    except InisError as e:
        print(f"inis.run API error: {e} (code={e.code})", file=sys.stderr)
        return 1
    except RuntimeError as e:
        print(f"scenario failed: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
