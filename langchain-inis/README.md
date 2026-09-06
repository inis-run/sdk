# langchain-inis

Native inis.run sandbox backend for [LangChain Deep Agents](https://docs.langchain.com/oss/python/deepagents/overview) (`deepagents.backends.sandbox.BaseSandbox`), plus a lower-level LangGraph recipe for teams not using Deep Agents.

[![PyPI](https://img.shields.io/pypi/v/langchain-inis.svg)](https://pypi.org/project/langchain-inis/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](https://github.com/inis-run/sdk/blob/main/LICENSE)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://pypi.org/project/langchain-inis/)

**Beta.** Verified against `deepagents==0.7.1` — pin `langchain-inis` and re-check before assuming it tracks a later deepagents major without changes.

## Install

```bash
pip install langchain-inis deepagents
```

## Quickstart: native Deep Agents backend

```python
from inis import Client
from deepagents import create_deep_agent
from langchain_inis import InisSandboxBackend

backend = InisSandboxBackend.create()  # owned: this app destroys it
try:
    agent = create_deep_agent(model="anthropic:claude-sonnet-5", backend=backend)
    result = agent.invoke({"messages": [{"role": "user", "content": "List /workspace, then write hello.txt"}]})
    print(result["messages"][-1].content)
finally:
    backend.destroy()
```

No custom lifecycle tools in the model prompt — passing `backend=backend`
is the whole integration. Deep Agents auto-registers
`ls`/`read_file`/`write_file`/`edit_file`/`glob`/`grep` and `execute`
against it, all bounded.

### Owned vs. attached

- **`InisSandboxBackend.create(...)`** — a brand-new session this app
  owns and must `backend.destroy()` when done; `SandboxBackendProtocol`
  has no lifecycle hooks of its own.
- **`InisSandboxBackend.attach(session_id, ...)`** — binds to a session
  this app does not own. Never destroys it; call `backend.close()` when
  done, which only releases this backend's local HTTP transport.

```python
backend = InisSandboxBackend.attach(existing_session_id)
try:
    agent = create_deep_agent(model=..., backend=backend)
    ...
finally:
    backend.close()  # local transport only — the session is not this app's to destroy
```

One agent thread/conversation should map to one session — resolve
`session_id` per thread using whatever key your app already tracks
conversations by. See [`examples/langgraph_recipe.py`](https://github.com/inis-run/sdk/blob/main/langchain-inis/examples/langgraph_recipe.py)'s `ThreadSessionStore` for a worked example.

## What it does

- `execute`/`aexecute` (shell)
- `upload_files`/`download_files` (+ async) — base64 for binary
- Every derived file op (`ls`/`read`/`write`/`edit`/`grep`/`glob`/`delete`) — `BaseSandbox`'s own defaults, unmodified
- Owned/attached lifetime split — see above
- Structured error mapping — `InisError.code` mapped onto `FileOperationError` literals where it matches

`enable_capture_offload` is `False`, forced — see below, this is the most important thing to read here.

## Notes

True remote cancellation of an in-flight `execute()` is not implemented: `BaseSandbox.execute(command, timeout=...)` is timeout-only, with no abort hook. A `timeout` still ends the call. Streaming output is not implemented either — `ExecuteResponse` has no streaming shape.

### Large `execute()` output is not capped on this backend

deepagents' automatic eviction of oversized tool results
(`/large_tool_results`) requires writing to a directory hardcoded off the
filesystem root — which fails `Permission denied` on inis.run's
non-root, `/workspace`-confined session, and deepagents silently keeps
the full-size message rather than raising. `enable_capture_offload=False`
reflects that this eviction path is a no-op here, not a preference.

The practical ceiling is instead inis.run's own guest-side capture cap
(up to 4 MiB per stream), well past what Deep Agents' ~80 KB budget
assumes. Nothing is silently dropped — the risk is an unexpectedly large
chunk of context from one verbose command. Bound noisy commands yourself
(`head -c`/`tail -c`, or write to a file and `read_file` back a slice)
until deepagents supports a configurable offload root for a plain
`BaseSandbox`.

`ExecuteResponse.truncated` is set directly from inis.run's own
per-stream truncation signal, so a caller reading it gets an honest
answer.

## LangGraph recipe (lower-level, secondary path)

[`examples/langgraph_recipe.py`](https://github.com/inis-run/sdk/blob/main/langchain-inis/examples/langgraph_recipe.py) is a complete, runnable example using
plain LangGraph primitives (`@tool`, `ToolNode`, `StateGraph`) for teams
not using Deep Agents. One session per graph thread ID, a bounded tool
surface, and a small file-backed `ThreadSessionStore` demonstrating a
session ID surviving a process restart.

```bash
python examples/langgraph_recipe.py           # creates (or reattaches to) demo-thread-1
python examples/langgraph_recipe.py --end      # ... and cleans it up
```

## Development

```bash
pip install -e ".[dev]"
pip install -e ../python   # inis core SDK, editable
pytest
```

Tests subclass the upstream `SandboxIntegrationTests` conformance suite
(`langchain-tests`) against a fake local-subprocess inis.run HTTP
backend — the network transport is faked, `execute()`/file ops run for
real. Run on Linux (or Linux CI); several conformance tests assume GNU
`grep` semantics that a default macOS shell doesn't have.

## Links

- Docs: [docs.inis.run](https://docs.inis.run) · [LangChain integration guide](https://docs.inis.run/docs/integrations/langchain)
- Source: [github.com/inis-run/sdk/tree/main/langchain-inis](https://github.com/inis-run/sdk/tree/main/langchain-inis)
- Homepage: [inis.run](https://inis.run)

Built and run in the EU. Your code and data never leave Europe.

## License

MIT — see [LICENSE](https://github.com/inis-run/sdk/blob/main/LICENSE).
