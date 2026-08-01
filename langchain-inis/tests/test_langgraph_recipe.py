"""Tests for `examples/langgraph_recipe.py` against the fake local-subprocess
inis.run backend — no production secret, no network, no third-party model.

`_FakeInisClient` is a minimal stand-in for `inis.Client` that supports
exactly the two calls the recipe needs (`sessions.create()` /
`sessions.attach(id)`), each backed by its own `LocalProcessInisBackend` +
mocked-transport `Session` (same technique `fake_backend.attach_fake_session`
uses for a single session) — it exists only so this test file can exercise
`get_or_create_session`/`end_thread`/`make_sandbox_tools`/`build_graph`
against something that behaves like the real multi-session `Client` without
a live server.
"""

from __future__ import annotations

import sys
import time
import uuid
from pathlib import Path

from inis import Session

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples"))

from langchain_core.messages import HumanMessage, ToolMessage  # noqa: E402

from langgraph_recipe import (  # noqa: E402
    ThreadSessionStore,
    build_graph,
    end_thread,
    get_or_create_session,
    make_sandbox_tools,
    sweep_idle_threads,
)

from .fake_backend import LocalProcessInisBackend, attach_fake_session, attach_session_to_backend  # noqa: E402
from .fake_model import final_message, scripted_model, tool_call_message  # noqa: E402


def _session_id() -> str:
    return f"ses_test_{uuid.uuid4().hex[:12]}"


class _FakeSessionsAPI:
    def __init__(self, backends: dict[str, LocalProcessInisBackend]) -> None:
        self._backends = backends

    def create(self, **_kwargs: object) -> Session:
        session_id = _session_id()
        session, backend = attach_fake_session(session_id)
        self._backends[session_id] = backend
        return session

    def attach(self, session_id: str) -> Session:
        if session_id not in self._backends:
            # Reattaching to a session this fake process hadn't seen before
            # (e.g. after a simulated restart) — create its backing state
            # lazily, same as the real API would already have it running.
            _, backend = attach_fake_session(session_id)
            self._backends[session_id] = backend
        # Reuse the EXISTING backend's state — a second, unrelated
        # `attach_fake_session()` call here would wire this Session to a
        # brand-new (empty) fake backend instead of the one this session id
        # already has data in.
        return attach_session_to_backend(session_id, self._backends[session_id])


class _FakeInisClient:
    """Stands in for `inis.Client` — supports only `.sessions.create()` /
    `.sessions.attach(id)`, the two calls `langgraph_recipe` uses."""

    def __init__(self) -> None:
        self.backends: dict[str, LocalProcessInisBackend] = {}
        self.sessions = _FakeSessionsAPI(self.backends)

    def cleanup(self) -> None:
        for backend in self.backends.values():
            backend.cleanup()


def _tool_messages(result: dict) -> list[ToolMessage]:
    return [m for m in result["messages"] if isinstance(m, ToolMessage)]


# -- ThreadSessionStore: pure logic, no I/O mocking needed -------------------


def test_store_persists_across_separate_instances(tmp_path: Path) -> None:
    """Two separate `ThreadSessionStore` instances pointed at the same file
    resolve the same thread id to the same session id — the "host-process
    boundary" this recipe demonstrates, using two independent Python
    objects (as close as a same-process test can get to two separate
    process runs)."""
    store_path = tmp_path / "store.json"
    store_a = ThreadSessionStore(store_path)
    store_a.touch("thread-1", "ses_abc123")

    store_b = ThreadSessionStore(store_path)  # a fresh instance, own memory
    assert store_b.get_session_id("thread-1") == "ses_abc123"


def test_store_idle_thread_ids(tmp_path: Path) -> None:
    store = ThreadSessionStore(tmp_path / "store.json")
    store.touch("fresh-thread", "ses_fresh")
    store.touch("stale-thread", "ses_stale")
    # Force the "stale" row's last_used_at into the past without waiting.
    data = store._read()
    data["stale-thread"]["last_used_at"] = time.time() - 10_000
    store._write(data)

    idle = store.idle_thread_ids(idle_seconds=60)
    assert idle == ["stale-thread"]


# -- get_or_create_session / end_thread lifecycle ----------------------------


def test_get_or_create_session_reuses_existing_mapping(tmp_path: Path) -> None:
    client = _FakeInisClient()
    try:
        store = ThreadSessionStore(tmp_path / "store.json")
        first = get_or_create_session(store, "thread-1", client)  # type: ignore[arg-type]
        second = get_or_create_session(store, "thread-1", client)  # type: ignore[arg-type]
        assert first.session_id == second.session_id
        assert store.get_session_id("thread-1") == first.session_id
    finally:
        client.cleanup()


def test_two_threads_get_two_different_sessions(tmp_path: Path) -> None:
    client = _FakeInisClient()
    try:
        store = ThreadSessionStore(tmp_path / "store.json")
        session_1 = get_or_create_session(store, "thread-1", client)  # type: ignore[arg-type]
        session_2 = get_or_create_session(store, "thread-2", client)  # type: ignore[arg-type]
        assert session_1.session_id != session_2.session_id
    finally:
        client.cleanup()


def test_end_thread_destroys_owned_session_and_clears_store(tmp_path: Path) -> None:
    client = _FakeInisClient()
    try:
        store = ThreadSessionStore(tmp_path / "store.json")
        session = get_or_create_session(store, "thread-1", client)  # type: ignore[arg-type]
        backend = client.backends[session.session_id]  # type: ignore[arg-type]

        end_thread(store, "thread-1", client, destroy=True)  # type: ignore[arg-type]

        assert backend.destroyed is True
        assert store.get_session_id("thread-1") is None
    finally:
        client.cleanup()


def test_end_thread_on_failure_path_still_destroys(tmp_path: Path) -> None:
    """Owned cleanup must run on the exception path too, not just the
    happy path, exercised here at the recipe level."""
    import pytest

    client = _FakeInisClient()
    try:
        store = ThreadSessionStore(tmp_path / "store.json")
        session = get_or_create_session(store, "thread-1", client)  # type: ignore[arg-type]
        backend = client.backends[session.session_id]  # type: ignore[arg-type]

        with pytest.raises(RuntimeError):
            try:
                raise RuntimeError("simulated failure mid-conversation")
            finally:
                end_thread(store, "thread-1", client, destroy=True)  # type: ignore[arg-type]

        assert backend.destroyed is True
    finally:
        client.cleanup()


def test_sweep_idle_threads_cleans_up_abandoned_sessions(tmp_path: Path) -> None:
    client = _FakeInisClient()
    try:
        store = ThreadSessionStore(tmp_path / "store.json")
        session = get_or_create_session(store, "abandoned-thread", client)  # type: ignore[arg-type]
        backend = client.backends[session.session_id]  # type: ignore[arg-type]
        data = store._read()
        data["abandoned-thread"]["last_used_at"] = time.time() - 10_000
        store._write(data)

        swept = sweep_idle_threads(store, client, idle_seconds=60)  # type: ignore[arg-type]

        assert swept == ["abandoned-thread"]
        assert backend.destroyed is True
        assert store.get_session_id("abandoned-thread") is None
    finally:
        client.cleanup()


# -- full graph: @tool / ToolNode / tools_condition / StateGraph(MessagesState) --


def test_graph_execute_tool_hits_real_fake_backend(tmp_path: Path) -> None:
    client = _FakeInisClient()
    try:
        store = ThreadSessionStore(tmp_path / "store.json")
        tools = make_sandbox_tools(store, client)  # type: ignore[arg-type]
        model = scripted_model(
            [
                tool_call_message("execute", {"command": "echo hello-from-langgraph-recipe"}, call_id="c1"),
                final_message("done"),
            ]
        )
        graph = build_graph(model, tools)

        result = graph.invoke(
            {"messages": [HumanMessage(content="run something")]},
            config={"configurable": {"thread_id": "thread-exec"}},
        )

        tool_messages = _tool_messages(result)
        assert len(tool_messages) == 1
        assert "hello-from-langgraph-recipe" in tool_messages[0].content
    finally:
        client.cleanup()


def test_graph_two_threads_do_not_share_sandbox_state(tmp_path: Path) -> None:
    client = _FakeInisClient()
    try:
        store = ThreadSessionStore(tmp_path / "store.json")
        tools = make_sandbox_tools(store, client)  # type: ignore[arg-type]

        # Pre-provision each thread's session (as `get_or_create_session`
        # would lazily do on the tool's first call) so the test can build
        # each scripted model's file path under that fake session's own
        # real workspace_root — see `fake_backend.LocalProcessInisBackend`'s
        # docstring for why this fake can't use the literal string
        # "/workspace" as a shared root. The graph's own tool call below
        # then finds the same store mapping and attaches to it (the
        # existing-mapping branch), rather than creating a second session.
        session_a = get_or_create_session(store, "thread-A", client)  # type: ignore[arg-type]
        session_b = get_or_create_session(store, "thread-B", client)  # type: ignore[arg-type]
        path_a = f"{client.backends[session_a.session_id].workspace_root}/who.txt"  # type: ignore[arg-type]
        path_b = f"{client.backends[session_b.session_id].workspace_root}/who.txt"  # type: ignore[arg-type]

        model_a = scripted_model(
            [
                tool_call_message("write_file", {"path": path_a, "content": "thread-A"}, call_id="a1"),
                tool_call_message("read_file", {"path": path_a}, call_id="a2"),
                final_message("done A"),
            ]
        )
        model_b = scripted_model(
            [
                tool_call_message("write_file", {"path": path_b, "content": "thread-B"}, call_id="b1"),
                tool_call_message("read_file", {"path": path_b}, call_id="b2"),
                final_message("done B"),
            ]
        )

        result_a = build_graph(model_a, tools).invoke(
            {"messages": [HumanMessage(content="go")]},
            config={"configurable": {"thread_id": "thread-A"}},
        )
        result_b = build_graph(model_b, tools).invoke(
            {"messages": [HumanMessage(content="go")]},
            config={"configurable": {"thread_id": "thread-B"}},
        )

        read_a = [m for m in _tool_messages(result_a) if m.name == "read_file"][0]
        read_b = [m for m in _tool_messages(result_b) if m.name == "read_file"][0]
        assert "thread-A" in read_a.content
        assert "thread-B" in read_b.content
        assert "thread-B" not in read_a.content
        assert "thread-A" not in read_b.content
        assert store.get_session_id("thread-A") != store.get_session_id("thread-B")
    finally:
        client.cleanup()
