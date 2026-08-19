#!/usr/bin/env python3
"""Branch and evaluate — establish shared state once, fork it into
independent children, run candidate work on each child at the same time,
and grade the results OUTSIDE the sandbox to pick a winner.

## Getting the real API right (this is the part the issue flagged as wrong)

The illustrative `session.fork(n=10)` returning an iterable `ForkResult` you
loop over never existed. The real surface (verified against
sdk/python/inis/client.py) is:

    session.fork(count: int = 1) -> ForkResult
    # ForkResult(parent_session_id: str, children: list[str])

`children` is a list of plain session ID strings, NOT live `Session`
handles and not richer child objects — each one is a fully independent
session (its own VM, its own lifecycle) that inherited the parent's
filesystem at fork time and diverges from there. To actually run anything
on a child you attach to its ID, exactly like reattaching to any other
session: `client.sessions.attach(child_id)`.

## Is running candidates on the children actually concurrent?

Yes — but not because of a "parallel fork helper". There IS a
`Session.batch_exec` / `POST /v1/sessions/batch/exec` endpoint that takes
multiple session IDs and one command, and it is tempting to reach for it
here. Checked against the server's handler for it: it loops over
`session_ids` and calls `Exec` on each **one at a time, sequentially** —
it is a fan-out convenience for running the *same* command on many
sessions, not a concurrency primitive, and this example does not use it
(nor call it "parallel" anywhere, unlike an earlier illustrative version
of this scenario would have).

The real concurrency here comes from the client, not the server: each
forked child is an independently addressable session, so issuing one
`exec()` HTTP call per child from separate threads genuinely overlaps in
wall-clock time — proven below by having each candidate sleep, then
checking that the *total* elapsed time is close to one sleep, not the sum
of all of them.

## Fork's cost story

Fork clones a live session's filesystem to seed N children cheaply, which
is what makes "start N candidates from the same expensive setup" viable at
all — the economics of that (vs. re-running setup N times, or keeping N
sessions live from the start) are out of scope here: this example proves
the mechanics, not the pricing.

Egress/TTL/cleanup: same conventions as examples/quickstart — deny-by-default
egress set explicitly, short TTLs for this disposable demo. Every session
this script creates (the parent AND every fork child — forking clones into
INDEPENDENT sessions, each billed and cleaned up like any other) is
destroyed in the `finally` block, success or failure.

Run:
    export INIS_API_KEY=...
    pip install inis
    python branch_and_evaluate.py
"""

from __future__ import annotations

import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass

from inis import Client, ForkResult, InisError, Session

# The server's default per-request fork cap is 4 (a raised account limit
# can go up to a hard cap of 16); this example forks 2 to stay comfortably
# under the default even on an account with no capacity headroom to spare.
# This file was NOT run against production (see the PR/issue for exactly
# what was and wasn't executed) — fork_with_retry below exists because a
# busy shared fleet can genuinely return a retryable 503 while reserving
# slots for several children at once, not because that was observed here.
FORK_COUNT = 2

# Candidate "solutions" to a toy task: given N, print the sum 1..N. One is
# deliberately wrong, to prove the grader actually checks correctness
# rather than just "did it exit 0".
CANDIDATES = {
    "iterative": "n=int(open('/workspace/n.txt').read()); print(sum(range(1, n + 1)))",
    "off_by_one_bug": "n=int(open('/workspace/n.txt').read()); print(sum(range(1, n)))",  # wrong on purpose
}
N = 1000
EXPECTED = sum(range(1, N + 1))


def fork_with_retry(session: Session, count: int, *, max_attempts: int = 5) -> ForkResult:
    """Fork, retrying on the server's own retryable signal (InisError.retryable
    — driven by the error `code`, e.g. `service_unavailable`/`unavailable` for
    a transient capacity shortfall) rather than on a hardcoded guess of which
    codes are transient. This is the same "recover from a capacity-limited
    request" pattern the task-oriented docs describe: reserving restore
    slots for several fork children at once can genuinely return a
    retryable 503 on a busy fleet when slots are scarce — that is not a bug
    to hide, it's the exact case this retry loop exists for. (Not exercised
    against production in this change — see the PR/issue for what was.)"""
    last_error: InisError | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            return session.fork(count=count)
        except InisError as e:
            if not e.retryable or attempt == max_attempts:
                raise
            last_error = e
            delay = e.retry_after or min(2.0 * attempt, 10.0)
            print(
                f"  fork attempt {attempt}/{max_attempts} hit a retryable error "
                f"(code={e.code}): {e} — retrying in {delay:.1f}s"
            )
            time.sleep(delay)
    assert last_error is not None  # unreachable: loop always returns or raises
    raise last_error


@dataclass
class CandidateResult:
    name: str
    stdout: str
    exit_code: int
    duration_ms: int


def run_candidate(client: Client, child_session_id: str, name: str, code: str) -> CandidateResult:
    """Attach to one forked child and run its candidate. `attach()` opens
    its own HTTP connection (it does not share the passed-in `client`'s
    pool), so each call here is fully independent across threads — closed
    again once this candidate's run finishes."""
    session = client.sessions.attach(child_session_id)
    try:
        session.write_file(f"/workspace/{name}.py", code)
        # A deliberate sleep makes the overlap in wall-clock time obvious in
        # the printed output below — real candidate work wouldn't need this.
        result = session.exec(["bash", "-lc", f"sleep 2; python3 /workspace/{name}.py"])
        return CandidateResult(
            name=name, stdout=result.stdout.strip(), exit_code=result.exit_code, duration_ms=result.duration_ms
        )
    finally:
        session.close()


def grade(results: list[CandidateResult]) -> CandidateResult | None:
    """The grader runs OUTSIDE the sandbox, on the host, over the returned
    stdout/exit_code — never inside the workspace, and never with access to
    what a candidate itself could tamper with beyond its own printed
    output. Picks the first candidate that exited 0 and printed the exact
    expected sum."""
    for r in results:
        if r.exit_code == 0 and r.stdout == str(EXPECTED):
            return r
    return None


def main() -> int:
    if not os.environ.get("INIS_API_KEY"):
        print(
            "INIS_API_KEY is not set. Export it before running this example — "
            "never put it in code, a file, or a session/guest command.",
            file=sys.stderr,
        )
        return 1

    client = Client()
    parent_id: str | None = None
    child_ids: list[str] = []

    try:
        # 1. Establish shared state ONCE: one parent session, with the input
        #    every candidate needs already written to its filesystem.
        with client.session(
            idle_timeout_ms=60_000,
            max_lifetime_ms=5 * 60_000,
            egress_default="deny",
        ) as parent:
            parent_id = parent.session_id
            print(f"parent session {parent_id}: writing shared input (n={N})")
            parent.write_file("/workspace/n.txt", str(N))

            # 2. Fork into independent children — the REAL API: count=, not
            #    n=; returns parent_session_id + a list of child session ID
            #    strings, not an iterable of ForkResult objects.
            fork_result = fork_with_retry(parent, FORK_COUNT)
            child_ids = list(fork_result.children)
            print(f"forked {len(child_ids)} children from {fork_result.parent_session_id}: {child_ids}")
            if len(child_ids) != FORK_COUNT:
                raise RuntimeError(f"expected {FORK_COUNT} children, got {len(child_ids)}")

            # 3. Run each candidate CONCURRENTLY, one per child, from the
            #    client side — proven below by wall-clock time, not by
            #    trusting a docstring.
            start = time.monotonic()
            results: list[CandidateResult] = []
            with ThreadPoolExecutor(max_workers=len(child_ids)) as pool:
                futures = {
                    pool.submit(run_candidate, client, child_id, name, code): name
                    for child_id, (name, code) in zip(child_ids, CANDIDATES.items())
                }
                for future in as_completed(futures):
                    results.append(future.result())
            elapsed = time.monotonic() - start

            print(f"ran {len(results)} candidates in {elapsed:.1f}s wall-clock (each sleeps 2s):")
            for r in sorted(results, key=lambda r: r.name):
                print(f"  {r.name}: stdout={r.stdout!r} exit_code={r.exit_code} duration_ms={r.duration_ms}")

            # If this were truly sequential (like batch_exec's server-side
            # loop), 3 candidates each sleeping 2s would take >=6s. Running
            # them from separate threads against separate sessions instead
            # takes close to one sleep's worth of wall time — the actual
            # concurrency claim, proven, not asserted.
            if elapsed >= len(CANDIDATES) * 2.0:
                raise RuntimeError(
                    f"expected concurrent overlap (~2s), took {elapsed:.1f}s — looks sequential"
                )

            # 4. Grade OUTSIDE the sandbox and select a winner.
            winner = grade(results)
            if winner is None:
                raise RuntimeError("no candidate produced the expected result")
            print(f"winner: {winner.name!r} (expected {EXPECTED}, got {winner.stdout})")

        # parent's `with` block destroyed the parent above; still need to
        # destroy every forked child — forking does NOT make them
        # children-of-a-lifecycle that the parent's destroy cascades to.
        return 0
    except InisError as e:
        print(f"inis.run API error: {e} (code={e.code})", file=sys.stderr)
        return 1
    except RuntimeError as e:
        print(f"scenario failed: {e}", file=sys.stderr)
        return 1
    finally:
        # Every forked child is a fully independent session — destroy each
        # one explicitly. (The parent is already destroyed by its own
        # `with` block above; this only cleans up the children.)
        for child_id in child_ids:
            attached = client.sessions.attach(child_id)
            try:
                attached.destroy()
                print(f"child session {child_id} destroyed")
            except InisError as e:
                print(f"warning: failed to destroy child {child_id}: {e}", file=sys.stderr)
            finally:
                attached.close()
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
