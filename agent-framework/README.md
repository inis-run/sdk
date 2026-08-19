# inis-agent-framework

Native inis.run CodeAct provider for [Microsoft Agent Framework](https://github.com/microsoft/agent-framework) (`agent_framework.ContextProvider`). Gives an `Agent` a real, isolated `execute_code` tool backed by inis.run's persistent Python interpreter.

[![PyPI](https://img.shields.io/pypi/v/inis-agent-framework.svg)](https://pypi.org/project/inis-agent-framework/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](https://github.com/inis-run/sdk/blob/main/LICENSE)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://pypi.org/project/inis-agent-framework/)

**CodeAct providers are a young, beta convention upstream.** `agent-framework-core` itself is stable; Microsoft's own first-party CodeAct backends (`agent-framework-monty`, `agent-framework-hyperlight`) are each beta. Verified against `agent-framework-core==1.13.0`.

## Install

```bash
pip install inis-agent-framework
```

## Quickstart: attached (recommended)

One inis.run session for the whole conversation, owned by your app:

```python
from inis import AsyncClient
from agent_framework import Agent
from inis_agent_framework import InisCodeActProvider

client = AsyncClient()
session = await client.sessions.create(egress_default="deny")
codeact = InisCodeActProvider.wrap(session)

agent = Agent(client=chat_client, name="assistant", context_providers=[codeact])
await agent.run("Use execute_code to compute 6 * 7.")
await session.destroy()  # this app owns the session; the provider never will
```

Reconnect a later process to the same persistent interpreter context by
session ID alone — no snapshot, no workspace round-trip:

```python
codeact = InisCodeActProvider.attach("9hmquuxiz7qyqdqiwfvep")
agent = Agent(client=chat_client, context_providers=[codeact])
await agent.run("What was x again?")  # sees state from the earlier process
```

## Owned: the provider creates and destroys the session for you

```python
from inis_agent_framework import InisCodeActProvider

codeact = await InisCodeActProvider.create(egress_default="deny")
try:
    agent = Agent(client=chat_client, context_providers=[codeact])
    await agent.run("...")
finally:
    await codeact.aclose()  # destroys the session
```

Either way, only `execute_code` is model-visible.

## Persistent interpreter state, not a fresh one per call

`InisExecuteCodeTool` runs through `AsyncSession.run_code()`, backed by a
real persistent Python process inside the session: a variable set in one
`execute_code` call is still there in the next one, across separate tool
calls *and* separate `agent.run()` invocations against the same session,
until `restart_context()` is called or the session is torn down.

## What it does

- `execute_code` (persistent Python interpreter) — text/image/table/json/error results mapped to `agent_framework.Content`
- Interpreter state across calls and `agent.run()` invocations — `.attach()` resumes the same context
- `restart_context()` — discards the interpreter context without touching the session's filesystem or lifecycle
- Owned/attached lifecycle split — `.create()` owns and destroys; `.wrap()`/`.attach()` never do

## Notes

`call_tool` bridging to host-registered tools is not implemented, deliberately: inis.run's `execute_code` runs in a real remote VM with no channel for the guest to call an arbitrary host function mid-cell — that would let sandboxed code call any tool the agent has, unbounded. `workspace_root`/`file_mounts` (host-directory binding) is not implemented either: an inis.run session runs on separate hardware — there's no host directory to bind. Use `session.read_file()`/`write_file()` directly instead. .NET is out of scope: this package is Python-only, matching `agent-framework-core`'s Python package. True remote cancellation of an in-flight `execute_code` call is not implemented — `run_code()` is timeout-only; a `timeout_ms` still ends the call and the interpreter context stays usable afterward.

## Development

```bash
pip install -e ".[dev]"
pip install -e ../python           # inis core SDK, editable
pytest
```

## Links

- Docs: [docs.inis.run](https://docs.inis.run)
- Source: [github.com/inis-run/sdk/tree/main/agent-framework](https://github.com/inis-run/sdk/tree/main/agent-framework)
- Homepage: [inis.run](https://inis.run)

Built and run in the EU. Your code and data never leave Europe.

## License

MIT — see [LICENSE](https://github.com/inis-run/sdk/blob/main/LICENSE).
