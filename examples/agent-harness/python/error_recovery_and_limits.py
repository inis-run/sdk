#!/usr/bin/env python3
"""Recover from a paused, expired, failed, or capacity-limited session, and
cancel/time-bound work (#1237) — a generic retry helper plus scenarios
against the real, documented error contract.

## The recovery contract

Branch on `InisError.code`, never on the message string (it can be
reworded — see docs-site/content/docs/api/errors.mdx). `InisError.retryable`
and `.retry_after` come straight from the server's own signal (a code in
its known-retryable set, or an explicit `Retry-After` header) — this file's
`call_with_recovery` trusts that field instead of hand-maintaining a second
copy of which codes are "probably transient".

## What each scenario below actually does, and what it doesn't claim

This file was NOT run against production as part of this change — see the
PR/issue for exactly what was executed (import/type checks and a local,
mock-backed test suite only). Every specific error `code` cited below was
confirmed by reading the request-handling source, not by observing a live
response, and that distinction is kept explicit throughout rather than
written as if it had been observed:

- **Gone (404 `not_found`)** — attaching to a session you already
  destroyed, or to a made-up ID. Deterministic and account-independent: a
  destroyed session is dropped from the live lookup table and any further
  operation on its ID gets a plain 404 `not_found`, confirmed against the
  session lookup path. `session_ended` (410) is a *different* outcome —
  reserved for a session that reached history through a lifecycle event
  (idle timeout, `max_lifetime_ms`, or a paused session past its
  `paused_ttl_ms`), not one explicitly deleted — both are gone-for-good and
  handled the same way by `call_with_recovery`'s caller: don't retry the
  same ID, start fresh instead.
- **Over the request-level fork limit (400 `bad_request`)** — `fork(count=17)`
  exceeds the hard cap of 16 that applies before any account-specific limit
  or capacity check runs, so this triggers the same way regardless of
  which account runs it. Not retryable: the request itself is invalid.
- **Capacity-limited (429, `rate_limited`/`concurrency_limit_exceeded`)** —
  covered by `demo_retry_on_capacity_limit` below WITHOUT calling the real
  API: actually exhausting an account's live-session concurrency cap
  depends on account-specific state this script doesn't know and
  shouldn't guess at (forking enough children to hit an assumed number
  would either no-op on a roomy account or interfere with a small one).
  Instead, this demonstrates `call_with_recovery`'s actual retry/backoff
  behavior against a scripted `InisError` sequence — real recovery code,
  a fake failure sequence, deterministic either way.
- **A session stuck in `state="failed"`** — not triggered here (forcing a
  real provisioning failure on demand, e.g. a bad template, risks side
  effects unrelated to what this example needs to prove). The pattern
  (poll `session.get()`, branch on `state`, read `status_reason`) is
  documented, not exercised live, in `demo_failed_state_pattern` below.
- **Cancel / time-bound work** — `exec(..., timeout_ms=...)` and
  `kill_process()` on a background process. There is no separate "cancel
  this in-flight request from a different process" primitive in the
  current API for a plain buffered `exec()` call — only `exec_stream()`
  cancels an in-flight *foreground* command, by disconnecting from the
  SAME calling process; a *named* background process can be killed at any
  time by ID from anywhere via `kill_process()`. True remote cancellation
  of an arbitrary in-flight request from an unrelated process is a real
  product gap, not a docs gap — see #1044/#1046.

Cleanup: every session this script creates is destroyed in `finally`,
success or failure.

Run:
    export INIS_API_KEY=...
    pip install inis
    python error_recovery_and_limits.py
"""

from __future__ import annotations

import os
import sys
import time
from typing import Callable, TypeVar

from inis import Client, InisError

T = TypeVar("T")


def call_with_recovery(fn: Callable[[], T], *, max_attempts: int = 4) -> T:
    """Generic retry-with-backoff over InisError.retryable/.retry_after —
    the same shape whether the underlying cause is a transient node blip,
    an account concurrency limit, or anything else the server marks
    retryable. A non-retryable error (validation, not_found, ...) is
    re-raised on the first attempt; there is nothing to wait out."""
    last_error: InisError | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            return fn()
        except InisError as e:
            if not e.retryable or attempt == max_attempts:
                raise
            last_error = e
            delay = e.retry_after if e.retry_after is not None else min(1.5 * attempt, 8.0)
            print(f"    retryable error (code={e.code}), attempt {attempt}/{max_attempts}: backing off {delay:.1f}s")
            time.sleep(delay)
    assert last_error is not None  # unreachable
    raise last_error


def demo_gone_session(client: Client) -> None:
    print("1. gone session (destroyed, then accessed again):")
    session = client.sessions.create(idle_timeout_ms=60_000, max_lifetime_ms=300_000, egress_default="deny")
    session_id = session.session_id
    session.destroy()
    attached = client.sessions.attach(session_id)
    try:
        attached.exec(["echo", "should not run"])
        raise RuntimeError("expected an error accessing a destroyed session")
    except InisError as e:
        print(f"   got code={e.code!r} status={e.status} retryable={e.retryable} -> {e}")
        print("   recovery: not retryable, this session is gone for good — create a fresh replacement.")
    finally:
        attached.close()

    print("2. bogus session ID (never existed):")
    bogus = client.sessions.attach("ses_this_id_was_never_created")
    try:
        bogus.exec(["echo", "should not run"])
        raise RuntimeError("expected an error accessing a nonexistent session")
    except InisError as e:
        print(f"   got code={e.code!r} status={e.status} retryable={e.retryable} -> {e}")
    finally:
        bogus.close()


def demo_request_level_limit(client: Client) -> str:
    print("3. over the hard fork-count cap (400, not retryable):")
    session = client.sessions.create(idle_timeout_ms=60_000, max_lifetime_ms=300_000, egress_default="deny")
    try:
        session.fork(count=17)  # hard cap is 16, checked before any account-specific limit
        raise RuntimeError("expected count=17 to exceed the hard fork-count cap")
    except InisError as e:
        print(f"   got code={e.code!r} status={e.status} retryable={e.retryable} -> {e}")
        print("   recovery: NOT retryable as-is — the request itself is invalid. Retrying identically")
        print("   would fail again forever; the fix is reading the error and lowering count, not backoff.")
    return session.session_id


def demo_retry_on_capacity_limit() -> None:
    """Exercise `call_with_recovery`'s actual retry/backoff behavior
    against a SCRIPTED sequence of errors — real recovery code, a fake
    failure sequence — rather than trying to force a genuine 429 out of an
    account whose concurrency headroom this script doesn't know. This is
    exactly the shape a real 429 `rate_limited`/`concurrency_limit_exceeded`
    would drive `call_with_recovery` through."""
    print("4. retry/backoff over a capacity-limited (429) error — scripted, not live:")
    attempts = {"n": 0}

    def flaky_call() -> str:
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise InisError(
                "429: concurrency limit exceeded",
                code="rate_limited",
                status=429,
                retryable=True,
                retry_after=0.05,  # kept tiny so this demo stays fast
            )
        return "ok"

    result = call_with_recovery(flaky_call, max_attempts=5)
    if result != "ok" or attempts["n"] != 3:
        raise RuntimeError(f"expected recovery after 3 attempts, got result={result!r} attempts={attempts['n']}")
    print(f"   recovered after {attempts['n']} attempts by backing off on retryable=True")


def demo_failed_state_pattern(session_id: str, client: Client) -> None:
    """Documents the shape for a session stuck in state="failed" WITHOUT
    forcing one: poll get(), branch on state, read status_reason. Not
    exercised live here — forcing a real provisioning failure on demand
    risks side effects unrelated to what this example needs to prove."""
    print("5. recovering from state=\"failed\" (pattern only, not triggered here):")
    info = client.sessions.attach(session_id).get()
    print(f"   this session's actual state is {info.state!r} (healthy) — the failed-state branch below never fires")
    if info.state == "failed":
        print(f"   would recover by reading status_reason ({info.status_reason!r}) and creating a fresh session")
    else:
        print("   the real pattern: if session.get().state == \"failed\", read status_reason and start over —")
        print("   provisioning failures are not retryable against the same session ID.")


def demo_cancel_and_timeout(client: Client, session_id: str) -> None:
    print("6. cancel / time-bound work:")
    session = client.sessions.attach(session_id)
    try:
        # Bounded execution: the command would run 5s, but timeout_ms caps it.
        result = session.exec(["sleep", "5"], timeout_ms=1_000)
        print(f"   exec(sleep 5, timeout_ms=1000): timed_out={result.timed_out}, duration_ms={result.duration_ms}")
        if not result.timed_out:
            raise RuntimeError("expected the 5s sleep to be cut off by the 1s timeout")

        # Cancelling a named background process on demand, from anywhere
        # that knows its name — not the same call that started it.
        session.start_process("long-job", "sleep 30", keep_alive=True)
        before = session.get_process("long-job")
        killed = session.kill_process("long-job")
        print(f"   background process: state before kill={before.state!r}, after kill={killed.state!r}")
        if killed.state == "running":
            raise RuntimeError("expected kill_process to have stopped the background job")
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
    session_id: str | None = None
    try:
        demo_gone_session(client)
        session_id = demo_request_level_limit(client)
        demo_retry_on_capacity_limit()
        demo_failed_state_pattern(session_id, client)
        demo_cancel_and_timeout(client, session_id)
        return 0
    except InisError as e:
        print(f"unhandled inis.run API error: {e} (code={e.code})", file=sys.stderr)
        return 1
    except RuntimeError as e:
        print(f"scenario failed: {e}", file=sys.stderr)
        return 1
    finally:
        if session_id is not None:
            attached = client.sessions.attach(session_id)
            try:
                attached.destroy()
                print(f"session {session_id} destroyed")
            finally:
                attached.close()
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
