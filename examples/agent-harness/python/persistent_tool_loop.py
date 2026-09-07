#!/usr/bin/env python3
"""Persistent tool loop — one inis.run session per agent thread, reconnected
across turns by a HOST-persisted session ID, with the model restricted to a
bounded exec/file tool surface.

This is the shape a real agent framework integration takes: the framework
(not the model) creates the session, remembers its ID against the
conversation/thread, and destroys it when the conversation ends. The model
only ever sees `BoundedToolSurface` below — it never sees the inis.run API
key, never sees the session ID as something it could pass to a different
operation, and has no pause/resume/fork/destroy tool at all. That split is
the same one real framework adapters enforce (lifecycle admin is
opt-in, off by default) — this example builds the same shape by hand,
framework-free, so it's clear that's a property of the *pattern*, not of
one specific adapter.

Read examples/quickstart/python/quickstart.py first — this reuses its
create/attach/destroy shape. What's new here: THREE separate turns, the
middle one served by a second `Client` with no in-process state shared with
the first (standing in for a different host request/process), each turn
running multiple tool calls that must observe the same filesystem AND the
same running background process — proving persistence isn't just "the
files happened to survive" but "the whole session, process state included,
is still the same VM".

Endpoint/package/timeout/egress/TTL: same as examples/quickstart — see that
file's header for the full reasoning. Egress is session-level deny-by-default
here (this demo sets it explicitly; the platform's own out-of-the-box
default is full egress unless a project/org default says otherwise — see
docs-site/content/docs/networking.mdx).

Cleanup: the ONE session this script owns (created in turn 1) is destroyed
in the `finally` block at the bottom, on both the success and the failure
path — regardless of which turn is currently in flight when something goes
wrong. Nothing here ever destroys a session it merely attached to.

Run:
    export INIS_API_KEY=...
    pip install inis
    python persistent_tool_loop.py
"""

from __future__ import annotations

import json
import os
import sys
import time
import uuid
from typing import Any

from inis import Client, InisError, Session

STATE_PATH = "/workspace/agent/state.json"
TICKER_PATH = "/workspace/agent/ticker.txt"
TICKER_NAME = "ticker"


class ThreadSessionStore:
    """Host-side mapping from an agent thread ID to a session ID — nothing
    more. This is deliberately NOT a place that stores a live `Session`
    object shared across turns (that would be exactly the module-global
    session variable this pattern rules out): it stores only the string a
    real app would keep in a database row or cache key, instantiated once
    per run of this script, not at module scope.
    """

    def __init__(self) -> None:
        self._by_thread: dict[str, str] = {}

    def get(self, thread_id: str) -> str | None:
        return self._by_thread.get(thread_id)

    def put(self, thread_id: str, session_id: str) -> None:
        self._by_thread[thread_id] = session_id


class BoundedToolSurface:
    """What the model is actually allowed to call. No API key, no session
    lifecycle (pause/resume/fork/destroy), no way to target a different
    session than the one the host attached it to."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def exec(self, command: list[str]) -> dict[str, Any]:
        result = self._session.exec(command)
        return {"stdout": result.stdout, "stderr": result.stderr, "exit_code": result.exit_code}

    def read_file(self, path: str) -> str:
        content = self._session.read_file(path)
        return content if isinstance(content, str) else content.decode()

    def write_file(self, path: str, content: str) -> None:
        self._session.write_file(path, content)


def get_or_create_session(client: Client, store: ThreadSessionStore, thread_id: str) -> Session:
    """The one piece of lifecycle logic the HOST owns: look up whether this
    thread already has a session; attach if so, create (and record) a new
    one if not. The model never makes this decision."""
    existing_id = store.get(thread_id)
    if existing_id is not None:
        print(f"  host: thread {thread_id!r} already has session {existing_id} — attaching")
        return client.sessions.attach(existing_id)

    session = client.sessions.create(
        idle_timeout_ms=120_000,
        max_lifetime_ms=10 * 60_000,
        egress_default="deny",
    )
    store.put(thread_id, session.session_id)
    print(f"  host: created session {session.session_id} for thread {thread_id!r} (egress: deny-by-default)")
    return session


def read_counter(tools: BoundedToolSurface) -> int:
    return json.loads(tools.read_file(STATE_PATH))["counter"]


def main() -> int:
    if not os.environ.get("INIS_API_KEY"):
        print(
            "INIS_API_KEY is not set. Export it before running this example — "
            "never put it in code, a file, or a session/guest command.",
            file=sys.stderr,
        )
        return 1

    thread_id = f"thread-{uuid.uuid4().hex[:8]}"
    store = ThreadSessionStore()
    client = Client()
    owned_session_id: str | None = None

    try:
        # ── Turn 1: the first tool call for this thread. Fresh session,
        #    fresh counter file, a background ticker process started with
        #    keep_alive so it survives between turns without pinning the
        #    session live via idle-timeout alone.
        print("turn 1 (host process A, brand-new session):")
        session = get_or_create_session(client, store, thread_id)
        owned_session_id = session.session_id
        try:
            tools = BoundedToolSurface(session)
            tools.exec(["mkdir", "-p", "/workspace/agent"])
            tools.write_file(STATE_PATH, json.dumps({"counter": 1}))
            session.start_process(
                TICKER_NAME,
                f"i=0; while true; do i=$((i+1)); echo $i > {TICKER_PATH}; sleep 1; done",
                keep_alive=True,
            )
            counter_1 = read_counter(tools)
            print(f"  wrote counter={counter_1}, started background process {TICKER_NAME!r}")
        finally:
            session.close()  # local transport only; the session and its ticker stay alive server-side

        # Let the ticker tick at least once before the next turn reads it.
        time.sleep(2)

        # ── Turn 2: a DIFFERENT host process picks up the same thread.
        #    New Client, new BoundedToolSurface — nothing here is shared
        #    in-process with turn 1 except the thread ID -> session ID
        #    mapping, exactly what a real framework would persist to a DB.
        print("turn 2 (host process B, reconnects by thread ID only):")
        client_b = Client()
        session_b = get_or_create_session(client_b, store, thread_id)
        try:
            tools_b = BoundedToolSurface(session_b)
            counter_2 = read_counter(tools_b)
            if counter_2 != counter_1:
                raise RuntimeError(f"expected counter to still be {counter_1}, got {counter_2}")
            tools_b.write_file(STATE_PATH, json.dumps({"counter": counter_2 + 1}))
            processes = session_b.list_processes()
            ticker = next((p for p in processes if p.name == TICKER_NAME), None)
            if ticker is None or ticker.state != "running":
                raise RuntimeError(
                    f"expected {TICKER_NAME!r} still running from turn 1, got {ticker!r}"
                )
            tick_at_turn2 = int(tools_b.read_file(TICKER_PATH).strip())
            print(
                f"  same filesystem: counter {counter_1} -> {counter_2 + 1}; "
                f"same process: {TICKER_NAME!r} still running, ticked to {tick_at_turn2}"
            )
        finally:
            session_b.close()
            client_b.close()

        time.sleep(2)

        # ── Turn 3: back to the original process/client. Proves turn 2's
        #    write and the still-running ticker both persisted again.
        print("turn 3 (host process A again):")
        session_c = get_or_create_session(client, store, thread_id)
        try:
            tools_c = BoundedToolSurface(session_c)
            counter_3 = read_counter(tools_c)
            if counter_3 != counter_1 + 1:
                raise RuntimeError(f"expected counter to be {counter_1 + 1}, got {counter_3}")
            tick_at_turn3 = int(tools_c.read_file(TICKER_PATH).strip())
            if tick_at_turn3 < tick_at_turn2:
                raise RuntimeError("ticker went backwards — process did not persist")
            print(f"  counter is {counter_3} (turn 1 -> turn 2 -> turn 3), ticker now at {tick_at_turn3}")

            # Conversation is over: stop the background process cleanly
            # before tearing the session down (a graceful "cancel" of
            # in-flight work, not just letting destroy() SIGKILL everything).
            session_c.kill_process(TICKER_NAME)
            print(f"  stopped {TICKER_NAME!r} before ending the conversation")
        finally:
            session_c.close()

        return 0
    except InisError as e:
        print(f"inis.run API error: {e} (code={e.code})", file=sys.stderr)
        return 1
    except RuntimeError as e:
        print(f"scenario failed: {e}", file=sys.stderr)
        return 1
    finally:
        # The host owns exactly one session for this whole thread — created
        # in turn 1, destroyed here once the conversation ends, regardless
        # of which turn raised. Never destroyed by an intermediate attach.
        if owned_session_id is not None:
            attached = client.sessions.attach(owned_session_id)
            try:
                attached.destroy()
                print(f"session {owned_session_id} destroyed (thread {thread_id!r} conversation ended)")
            finally:
                attached.close()
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
