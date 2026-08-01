# inis-google-adk

Native inis.run environment provider for [Google Agent Development Kit (ADK)](https://google.github.io/adk-docs/)'s `BaseEnvironment` / `EnvironmentToolset` surface. Gives ADK's `Execute`/`ReadFile`/`EditFile`/`WriteFile` tools a real, isolated inis.run session instead of a local subprocess.

[![PyPI](https://img.shields.io/pypi/v/inis-google-adk.svg)](https://pypi.org/project/inis-google-adk/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](https://github.com/inis-run/sdk/blob/main/LICENSE)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://pypi.org/project/inis-google-adk/)

**This is ADK's own experimental surface, not this package's caution.** `BaseEnvironment`/`EnvironmentToolset` are decorated `@experimental` in ADK's own source (Google's words, not ours) — ADK ships two other in-tree providers against the same contract. Verified against `google-adk==2.6.0`. Neither class is re-exported from the `google.adk` top level; import from `google.adk.environment` / `google.adk.tools.environment` directly.

## Install

```bash
pip install inis-google-adk
```

## Quickstart: attached (recommended)

One inis.run session for the whole conversation, owned by your app:

```python
from inis import AsyncClient
from google.adk.agents import Agent
from google.adk.tools.environment import EnvironmentToolset
from inis_google_adk import InisEnvironment

client = AsyncClient()
session = await client.sessions.create(egress_default="deny")
environment = InisEnvironment.wrap(session)

agent = Agent(model="...", tools=[EnvironmentToolset(environment=environment)])
...
await session.destroy()  # this app owns the session; the environment never will
```

Reconnect a later process to the same session (and its persistent
`/workspace` filesystem) by session ID alone:

```python
environment = InisEnvironment.attach("ses_abc123")
```

## Owned: the environment creates and destroys the session for you

```python
from inis_google_adk import InisEnvironment

environment = InisEnvironment(egress_default="deny")
toolset = EnvironmentToolset(environment=environment)
try:
    agent = Agent(model="...", tools=[toolset])
    ...
finally:
    await toolset.close()  # calls environment.close(), which destroys the session
```

## Explicit fallback: plain FunctionTools

If you'd rather not depend on ADK's experimental surface at all,
`inis_adk_tools()` returns ordinary `FunctionTool`s wrapping the same
session directly:

```python
from google.adk.agents import Agent
from inis_google_adk import inis_adk_tools

agent = Agent(model="...", tools=inis_adk_tools(session))
```

The smaller, stable surface: `execute`, `read_file`, `write_file`,
`list_files` — nothing that creates, destroys, or administers the
session.

## What it does

- `execute` (shell) — real stdout/stderr/exit_code/timed_out
- `read_file` / `write_file` — a missing file raises `FileNotFoundError`
- `EditFile` — built entirely on `read_file`/`write_file`
- Owned/attached lifecycle split — `InisEnvironment()` owns; `.wrap()`/`.attach()` never do
- `close()` idempotency — safe to call more than once in either mode
- Non-experimental fallback (`inis_adk_tools()`)

## Notes

TypeScript, Java, and Go support isn't claimed: this package wraps the Python `inis` SDK and Python `google-adk` only. True remote cancellation of an in-flight `execute()` call is not implemented — `BaseEnvironment.execute(command, *, timeout)` is timeout-only; a timeout still ends the call.

## Truncated output

Neither `ExecutionResult` nor `inis_adk_tools()`'s `execute` has a field
for "this output was cut short." When inis.run's own per-stream capture
limit truncates output, both paths append the same marker instead of
silently dropping the fact — to `stderr` for `InisEnvironment.execute()`,
inline in the returned string for `inis_adk_tools()`:

```
[inis: output truncated by the sandbox's own capture limit (nonce=<16 hex chars>)]
```

## Development

```bash
pip install -e ".[dev]"
pip install -e ../python           # inis core SDK, editable
pytest
```

## Links

- Docs: [docs.inis.run](https://docs.inis.run)
- Source: [github.com/inis-run/sdk/tree/main/google-adk](https://github.com/inis-run/sdk/tree/main/google-adk)
- Homepage: [inis.run](https://inis.run)

Built and run in the EU. Your code and data never leave Europe.

## License

MIT — see [LICENSE](https://github.com/inis-run/sdk/blob/main/LICENSE).
