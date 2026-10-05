# inis-strands-agents

Native inis.run sandbox provider for [Strands Agents](https://strandsagents.com)' `strands.sandbox` surface. Gives Strands its standard `sandbox_bash` and `sandbox_file_editor` tools backed by a real, isolated inis.run session instead of the local host.

[![PyPI](https://img.shields.io/pypi/v/inis-strands-agents.svg)](https://pypi.org/project/inis-strands-agents/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](https://github.com/inis-run/sdk/blob/main/LICENSE)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://pypi.org/project/inis-strands-agents/)

Verified against `strands-agents==1.50.2`.

## Install

```bash
pip install inis-strands-agents
```

## Quickstart: attached (recommended)

One inis.run session for the whole conversation, owned by your app:

```python
from inis import AsyncClient
from strands import Agent
from inis_strands_agents import InisSandbox

client = AsyncClient()
session = await client.sessions.create(egress_default="deny")
sandbox = InisSandbox.wrap(session)

agent = Agent(model="...", sandbox=sandbox)
await agent.invoke_async("list files in /workspace")
await agent.invoke_async("now write hello.txt")
await session.destroy()  # this app owns the session; the adapter never will
```

## Owned: the adapter creates and destroys the session for you

```python
from strands import Agent
from inis_strands_agents import InisSandbox

async with await InisSandbox.create(egress_default="deny") as sandbox:
    agent = Agent(model="...", sandbox=sandbox)
    await agent.invoke_async("...")
# session destroyed on exit
```

Either way, `Agent(sandbox=...)` registers `sandbox_bash` and
`sandbox_file_editor` automatically — no separate tool wiring needed.
Session create/destroy, egress, pause/resume, and fork stay
host-controlled.

## What it does

- `execute_streaming` (shell) — distinct stdout/stderr chunks, then a final `ExecutionResult`
- `execute_code_streaming` — inherited from `PosixShellSandbox`
- Binary-safe `read_file`/`write_file`/`remove_file`/`list_files`, routed to inis.run's native `/files` endpoints
- Per-call `timeout` — genuinely kills the remote process on expiry
- Per-call `env` — via a validated shell prefix (inis.run's exec API fixes env at session creation)
- Cancellation (asyncio task cancellation) — closes the underlying stream and kills the remote command

## Notes

PTY isn't implemented: `strands.sandbox`'s shell/file contract has no equivalent. `ExecutionResult.output_files` is always empty, same as every other shell-based `Sandbox` — read a produced file with `read_file` instead.

## Development

```bash
pip install -e ".[dev]"
pip install -e ../python           # inis core SDK, editable
pytest
```

## Links

- Docs: [docs.inis.run](https://docs.inis.run)
- Source: [github.com/inis-run/sdk/tree/main/strands-agents-python](https://github.com/inis-run/sdk/tree/main/strands-agents-python)
- Homepage: [inis.run](https://inis.run)

Built and run in the EU. Your code and data never leave Europe.

## License

MIT — see [LICENSE](https://github.com/inis-run/sdk/blob/main/LICENSE).
