# inis-pydantic-ai

[Pydantic AI](https://ai.pydantic.dev) toolset and capability for [inis.run](https://inis.run) sandboxes.

[![PyPI](https://img.shields.io/pypi/v/inis-pydantic-ai.svg)](https://pypi.org/project/inis-pydantic-ai/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](https://github.com/inis-run/sdk/blob/main/LICENSE)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://pypi.org/project/inis-pydantic-ai/)

**Beta.** Supported [Pydantic AI](https://ai.pydantic.dev) range: `>=1.71,<3` (1.71 is the first release with the `capabilities` API).

## Install

```bash
pip install inis-pydantic-ai
```

## Quickstart: `inis_toolset()`

Your app creates (or attaches to) the session and owns `INIS_API_KEY`;
`inis_toolset()` just hands the model a bounded execution/file surface for it.

```python
import asyncio

from inis import AsyncClient
from inis_pydantic_ai import inis_toolset
from pydantic_ai import Agent

async def main() -> None:
    client = AsyncClient()
    session = await client.sessions.create(egress_default="deny")
    toolset = inis_toolset(session_id=session.session_id)

    agent = Agent(
        "openai:gpt-4o",
        toolsets=[toolset],
        system_prompt="Run Python in the sandbox instead of guessing.",
    )

    try:
        result = await agent.run("What is 19 * 23? Use Python.")
        print(result.output)
    finally:
        await toolset.aclose()  # releases the toolset's local HTTP transport
        await session.destroy()  # this app owns the session; clean it up when done

asyncio.run(main())
```

`session_id` is required and `inis_toolset()` never creates, destroys, or
discovers a session on its own — one agent thread maps to one session. To
attach to a session another part of your app already owns instead of
creating one, pass its ID the same way; this call never destroys it.

### What it provides

| Tool | Description |
|------|-------------|
| `execute_python` | Run Python in the sandbox. Raises `InisError` on non-zero exit. |
| `execute_shell` | Run a shell command in the persistent session. Raises `InisError` on non-zero exit. |
| `read_file` / `write_file` | File I/O under `/workspace`. |
| `list_files` | List a directory (defaults to `/workspace`). |

## Opt-in: session administration and one-shot sandboxes

Import from `inis_pydantic_ai.lifecycle` (not exported at the top level)
only if you want the model itself to control these:

```python
from inis_pydantic_ai.lifecycle import inis_ephemeral_toolset, inis_lifecycle_toolset

agent = Agent(
    "openai:gpt-4o",
    toolsets=[
        inis_toolset(session_id=session.session_id),
        inis_lifecycle_toolset(session_id=session.session_id),  # pause/resume/checkpoint/restore/expose/capture
    ],
)
```

| Toolset | Tools | Hands the model |
|---|---|---|
| `inis_lifecycle_toolset(session_id=...)` | `pause`, `resume`, `checkpoint`, `restore`, `expose_port`, `capture_artifacts` | Session administration |
| `inis_ephemeral_toolset()` | `execute_once` | Unbounded throwaway one-shot sandboxes, no `session_id` needed |

## `InisSandbox` capability

A capability (`Agent(capabilities=[...])`) following the
[pydantic-ai-harness](https://pypi.org/project/pydantic-ai-harness/)
sandbox contract — `run_command`, `read_file`, `write_file`,
`list_directory`. Binds to one session for the life of the `Agent`
instance rather than creating a fresh sandbox per run.

```python
from inis import AsyncClient
from inis_pydantic_ai.sandbox import InisSandbox
from pydantic_ai import Agent

client = AsyncClient()
session = await client.sessions.create(egress_default="deny")

sandbox = InisSandbox(session=session)
agent = Agent("anthropic:claude-sonnet-5", capabilities=[sandbox])

try:
    result = await agent.run("Write a script that prints the first 10 primes and run it.")
    print(result.output)
finally:
    await sandbox.aclose()  # no-op here — `session` is host-owned
    await session.destroy()
```

## Configuration

```python
inis_toolset(
    session_id="9hmquuxiz7qyqdqiwfvep",  # required
    api_key="inis_...",           # or $INIS_API_KEY
    base_url="https://api.inis.run",
)
```

## Links

- Docs: [docs.inis.run](https://docs.inis.run) · [Pydantic AI integration guide](https://docs.inis.run/docs/integrations/pydantic-ai)
- Source: [github.com/inis-run/sdk/tree/main/pydantic-ai](https://github.com/inis-run/sdk/tree/main/pydantic-ai)
- Homepage: [inis.run](https://inis.run)

Built and run in the EU. Your code and data never leave Europe.

## License

MIT — see [LICENSE](https://github.com/inis-run/sdk/blob/main/LICENSE).
