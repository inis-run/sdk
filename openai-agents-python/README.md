# inis-openai-agents

Native inis.run sandbox provider for the [OpenAI Agents SDK](https://openai.github.io/openai-agents-python/) `SandboxAgent` path (`agents.sandbox`).

[![PyPI](https://img.shields.io/pypi/v/inis-openai-agents.svg)](https://pypi.org/project/inis-openai-agents/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](https://github.com/inis-run/sdk/blob/main/LICENSE)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://pypi.org/project/inis-openai-agents/)

**Beta, upstream and here.** The OpenAI Agents SDK documents its own sandbox surface as beta. Verified against `openai-agents==0.19.1`.

## Install

```bash
pip install inis-openai-agents
```

## Quickstart: attached (recommended)

One inis.run session for the whole conversation, owned by your app:

```python
from inis import AsyncClient
from agents import Runner, RunConfig
from agents.run_config import SandboxRunConfig
from agents.sandbox import SandboxAgent
from inis_openai_agents import InisSandboxSession

client = AsyncClient()
inis_session = await client.sessions.create(egress_default="deny")
sandbox_session = InisSandboxSession.wrap(inis_session)

agent = SandboxAgent(name="assistant", model="gpt-5.1")
run_config = RunConfig(sandbox=SandboxRunConfig(session=sandbox_session))
try:
    result = await Runner.run(agent, "list files in /workspace", run_config=run_config)
    result2 = await Runner.run(agent, "now write hello.txt", run_config=run_config)
finally:
    await inis_session.destroy()  # this app owns the session; the SDK never will
```

The Agents SDK's own sandbox runtime never destroys a session passed via
`RunConfig.sandbox.session` — that lifecycle stays entirely
host-controlled: one persistent session across a whole conversation, not
one sandbox per model turn.

## Owned: one `Runner.run()` call at a time

```python
from agents.run_config import SandboxRunConfig
from inis_openai_agents import InisSandboxClient, InisSandboxClientOptions

run_config = RunConfig(
    sandbox=SandboxRunConfig(
        client=InisSandboxClient(),
        options=InisSandboxClientOptions(egress_default="deny"),
    )
)
result = await Runner.run(agent, "...", run_config=run_config)
```

The SDK creates a fresh session for that call and destroys it
unconditionally when the call ends. To resume a conversation in a later
process, pass `result._sandbox_resume_state` back in — the SDK creates a
*new* session and restores the workspace from a tar snapshot; it does not
reattach to the original session ID. Prefer the attached path above for a
real multi-turn agent.

## What it does

- `exec_command` (shell), with a real interactive PTY (`tty=True`) and `write_stdin` for a running session
- File read/write, symlink-safe path validation
- Exposed ports, via `session.expose()`
- Owned/attached lifetime split — see above
- Snapshot persist/hydrate (owned path's resume) — tar+base64 over `exec()`

## Notes

Docker-volume-mount manifests are not implemented: `supports_docker_volume_mounts()` stays `False`. True remote cancellation of an in-flight `exec()` is not implemented — the sandbox ABC's `exec()` is timeout-only, with no abort handle. A timeout still ends the call.

## Truncated output

`InisSandboxSession.exec()` returns `InisExecResult`, a plain subclass of
upstream's `agents.sandbox.types.ExecResult` adding one field:

```python
result = await sandbox_session.exec("some-command-that-produces-a-lot-of-output")
result.truncated  # bool — set when inis.run's own per-stream capture limit cut a stream
```

`stdout`/`stderr` are returned exactly as inis.run produced them — no
marker text, nothing to parse out. `truncated` is copied straight from
inis.run's own trusted signal (never derived from stream content), so it
can't be spoofed by anything the sandboxed command prints. An earlier
version of this adapter appended a marker with a random per-call nonce to
`stderr` instead, implying a caller could tell a real marker from a guest
printing a fake one by checking the nonce; nothing ever checked it, so
that implication was false.

## Fallback: plain function tools

Not using `SandboxAgent`? Use an ordinary `Agent` with function tools
against `inis` directly — see the core `inis` SDK's own examples.

## Development

```bash
pip install -e ".[dev]"
pip install -e ../python           # inis core SDK, editable
pytest
```

## Links

- Docs: [docs.inis.run](https://docs.inis.run)
- Source: [github.com/inis-run/sdk/tree/main/openai-agents-python](https://github.com/inis-run/sdk/tree/main/openai-agents-python)
- Homepage: [inis.run](https://inis.run)

Built and run in the EU. Your code and data never leave Europe.

## License

MIT — see [LICENSE](https://github.com/inis-run/sdk/blob/main/LICENSE).
