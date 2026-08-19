"""LangGraph recipe: bounded inis.run execute/file tools, one session per
graph thread id.

**This is the lower-level, secondary path.** Prefer the native backend —

    from inis import Client
    from deepagents import create_deep_agent
    from langchain_inis import InisSandboxBackend

    backend = InisSandboxBackend.create()
    agent = create_deep_agent(model=..., backend=backend)

— whenever a Deep Agents-shaped harness (planning, subagents, filesystem
tools, the `execute` tool, skills) fits your use case; it gets you the
whole bounded execution/file tool surface, owned/attached lifetime, and
structured-error handling for free. Reach for *this* recipe instead only
when you need LangGraph's own `StateGraph`/`ToolNode` composition directly
— a custom graph shape, a tool surface narrower or different from Deep
Agents' built-ins, or wiring inis.run sessions into an existing LangGraph
app that isn't built on Deep Agents at all.

What this recipe demonstrates, concretely:

- `@tool`, `ToolNode`, `tools_condition`, `StateGraph(MessagesState)` — the
  plain LangGraph primitives the issue asks for, not deepagents'.
- Binding only a bounded execute/file tool surface — no lifecycle control
  (create/pause/destroy) is ever exposed to the model.
- One inis.run session per graph *thread id* (`config["configurable"]
  ["thread_id"]`), resolved through `RunnableConfig` auto-injection (a
  `config: RunnableConfig` tool parameter is excluded from the model-visible
  schema by `langchain_core.tools.base` — see `_get_runnable_config_param`)
  rather than any module-global session/store state: `make_sandbox_tools()`
  is a factory that *closes over* one `ThreadSessionStore`, so nothing here
  lives at module scope.
- Persisting the session id through a host-process boundary:
  `ThreadSessionStore` is a tiny JSON-file store specifically so running
  this file's `__main__` block twice in a row demonstrates the *same*
  thread id resolving to the *same* inis.run session across two separate
  Python process runs. A real app persists this the same way it persists
  anything else about a conversation (a Postgres row, a Redis key) — the
  file-backed store here is a stand-in for that, not a recommendation to
  use flat JSON files in production.
- Cleanup on success, on failure, and for abandoned threads: `end_thread()`
  destroys an owned session and clears its store row from `finally` blocks
  on both the happy and the exception path; `sweep_idle_threads()` is the
  background-reaper shape for threads nobody ever explicitly ended.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from inis import Client, InisError, Session
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool, tool
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import MessagesState, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.prebuilt import ToolNode, tools_condition


class ThreadSessionStore:
    """Durable `thread_id -> {session_id, created_at, last_used_at}` mapping.

    See the module docstring: this JSON-file implementation exists so the
    `__main__` demo below can show a session id surviving a real process
    restart. Swap this class for your app's own persistence (same three
    methods) — the *shape* (one durable record per thread id, holding one
    inis.run session id) is the part every real app needs to reproduce.
    """

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)

    def _read(self) -> dict[str, dict[str, Any]]:
        if not self._path.exists():
            return {}
        return json.loads(self._path.read_text() or "{}")

    def _write(self, data: dict[str, dict[str, Any]]) -> None:
        self._path.write_text(json.dumps(data))

    def get_session_id(self, thread_id: str) -> str | None:
        record = self._read().get(thread_id)
        return record["session_id"] if record else None

    def touch(self, thread_id: str, session_id: str) -> None:
        data = self._read()
        now = time.time()
        record = data.get(thread_id, {"session_id": session_id, "created_at": now})
        record["session_id"] = session_id
        record["last_used_at"] = now
        data[thread_id] = record
        self._write(data)

    def delete(self, thread_id: str) -> None:
        data = self._read()
        if data.pop(thread_id, None) is not None:
            self._write(data)

    def idle_thread_ids(self, *, idle_seconds: float) -> list[str]:
        cutoff = time.time() - idle_seconds
        return [tid for tid, record in self._read().items() if record.get("last_used_at", 0) < cutoff]


def get_or_create_session(store: ThreadSessionStore, thread_id: str, client: Client) -> Session:
    """Resolve this thread's session: attach if one already exists for this
    thread id, otherwise create a brand-new owned one and record it.

    One inis.run session per graph thread id — never a session shared
    across threads, never a fresh session per tool call.
    """
    existing = store.get_session_id(thread_id)
    if existing is not None:
        session = client.sessions.attach(existing)
    else:
        session = client.sessions.create(egress_default="deny")
    store.touch(thread_id, session.session_id)  # type: ignore[arg-type]
    return session


def end_thread(store: ThreadSessionStore, thread_id: str, client: Client, *, destroy: bool) -> None:
    """Clean up a thread's sandbox session.

    Call this from a `finally` block around every `graph.invoke(...)` —
    both the success and the exception path — with `destroy=True` when this
    app owns the session's whole lifecycle (the common case for this
    recipe, since `get_or_create_session` always creates an owned session
    the first time a thread is seen). Pass `destroy=False` instead if this
    thread's session was itself attached from elsewhere and this app must
    not be the one to destroy it.
    """
    session_id = store.get_session_id(thread_id)
    if session_id is None:
        return
    if destroy:
        try:
            client.sessions.attach(session_id).destroy()
        except InisError:
            pass  # best-effort: already gone is fine
    store.delete(thread_id)


def sweep_idle_threads(store: ThreadSessionStore, client: Client, *, idle_seconds: float) -> list[str]:
    """Destroy sessions for threads nobody explicitly ended (a crashed
    process, a conversation the user simply walked away from) once they've
    been idle past `idle_seconds`. Run this on a schedule, the same shape as
    any background reaper — this function itself does not loop or sleep.

    Returns the thread ids it cleaned up.
    """
    swept: list[str] = []
    for thread_id in store.idle_thread_ids(idle_seconds=idle_seconds):
        end_thread(store, thread_id, client, destroy=True)
        swept.append(thread_id)
    return swept


def make_sandbox_tools(store: ThreadSessionStore, client: Client) -> list[BaseTool]:
    """Build the bounded execute/read/write tool surface for one
    `ThreadSessionStore` + `Client` pair.

    A factory, not module-level `@tool` functions with a module-global
    store: every tool closes over `store`/`client` from this call's scope,
    so nothing here is shared mutable state across unrelated graphs built
    from a different call to this factory. `config: RunnableConfig` is
    auto-injected by `langchain_core.tools.base` (excluded from the
    model-visible schema, per that module's `_get_runnable_config_param`)
    — the model never sees or sets `thread_id` itself.
    """

    def _session_for(config: RunnableConfig) -> Session:
        thread_id = config["configurable"]["thread_id"]
        return get_or_create_session(store, thread_id, client)

    @tool
    def execute(command: str, config: RunnableConfig) -> str:
        """Run a shell command in this conversation's sandbox and return its output."""
        session = _session_for(config)
        try:
            result = session.exec(command)
        except InisError as e:
            return f"Error: {e.code}: {e}"
        finally:
            session.close()  # local transport only — never destroys the session
        output = result.stdout
        if result.stderr:
            output = f"{output}\n{result.stderr}" if output else result.stderr
        if result.timed_out:
            output = f"{output}\n[timed out]" if output else "[timed out]"
        if result.truncated:
            output = f"{output}\n[output truncated]"
        return output

    @tool
    def read_file(path: str, config: RunnableConfig) -> str:
        """Read a text file from this conversation's sandbox /workspace."""
        session = _session_for(config)
        try:
            return session.read_file(path)
        except InisError as e:
            return f"Error: {e.code}: {e}"
        finally:
            session.close()

    @tool
    def write_file(path: str, content: str, config: RunnableConfig) -> str:
        """Write a text file to this conversation's sandbox /workspace."""
        session = _session_for(config)
        try:
            session.write_file(path, content)
            return f"wrote {path}"
        except InisError as e:
            return f"Error: {e.code}: {e}"
        finally:
            session.close()

    return [execute, read_file, write_file]


def build_graph(
    model: BaseChatModel,
    tools: list[BaseTool],
    *,
    checkpointer: BaseCheckpointSaver | None = None,
) -> CompiledStateGraph:
    """The plain `@tool`/`ToolNode`/`tools_condition`/`StateGraph(MessagesState)`
    shape the issue asks for — no deepagents involved."""
    bound_model = model.bind_tools(tools)

    def call_model(state: MessagesState) -> dict[str, list[AIMessage]]:
        return {"messages": [bound_model.invoke(state["messages"])]}

    graph = StateGraph(MessagesState)
    graph.add_node("agent", call_model)
    graph.add_node("tools", ToolNode(tools))
    graph.set_entry_point("agent")
    graph.add_conditional_edges("agent", tools_condition)
    graph.add_edge("tools", "agent")
    return graph.compile(checkpointer=checkpointer)


if __name__ == "__main__":
    # Minimal end-to-end demo: run this file twice in a row and the second
    # run attaches to the SAME inis.run session the first run created,
    # proving the thread id -> session id mapping survived a real process
    # boundary (this file's own JSON store, not memory).
    import os
    import sys

    from langchain.chat_models import init_chat_model

    store_path = Path(os.environ.get("INIS_LANGGRAPH_RECIPE_STORE", "/tmp/inis-langgraph-recipe-store.json"))
    thread_id = "demo-thread-1"

    client = Client()
    store = ThreadSessionStore(store_path)
    tools = make_sandbox_tools(store, client)
    model = init_chat_model("anthropic:claude-sonnet-4-6")
    graph = build_graph(model, tools)

    config: RunnableConfig = {"configurable": {"thread_id": thread_id}}
    try:
        result = graph.invoke(
            {"messages": [{"role": "user", "content": "Run `whoami` in the sandbox and tell me the result."}]},
            config=config,
        )
        print(result["messages"][-1].content)
    except Exception:
        end_thread(store, thread_id, client, destroy=True)
        raise
    if "--end" in sys.argv:
        end_thread(store, thread_id, client, destroy=True)
        print(f"ended thread {thread_id}")
    else:
        print(f"thread {thread_id} left open — rerun to reattach, or pass --end to clean up")
