# Adapter routing — what each one actually implements

Read this before assuming two adapters are at parity. Every package listed
here is independently verified against the real upstream framework version
named in its own README — that README's own module docstring is the fuller
account of what is and is not implemented. Don't restate this file's
summary as more certain than the source it points to.

| Adapter | Framework extension point | Verified against |
|---|---|---|
| `inis-pydantic-ai` | `inis_toolset()` function-tool surface | `pydantic-ai` `>=1.71,<3` |
| `@inis-run/ai-sdk` | `inisTools()` for `generateText`/`streamText` | `ai@^5.0.0` |
| `langchain-inis` | `deepagents.backends.sandbox.BaseSandbox` | `deepagents==0.7.1` |
| `@inis-run/mastra` | `@mastra/core/workspace` `WorkspaceSandbox` | `@mastra/core==1.55.0` |
| `inis-openai-agents` | `agents.sandbox` `SandboxAgent` path | `openai-agents==0.19.1` |
| `@inis-run/openai-agents` | `@openai/agents-core/sandbox` | `@openai/agents-core==0.14.1` |
| `inis-strands-agents` | `strands.sandbox.PosixShellSandbox` | `strands-agents==1.50.2` |
| `@inis-run/strands-agents` | `Sandbox`/`PosixShellSandbox` | `@strands-agents/sdk==1.11.2` |
| `inis-agent-framework` | `agent_framework.ContextProvider` (CodeAct) | `agent-framework-core==1.13.0` |
| `inis-google-adk` | `google.adk.environment.BaseEnvironment` | `google-adk==2.6.0` |

Common gaps, called out by name so you don't rediscover them by testing:

- **True remote cancellation of an arbitrary in-flight command is not
  implemented anywhere** — inis.run itself doesn't have that primitive for
  a one-shot `exec()` (its context is rooted in the server's own top-level
  context, not tied to any client connection). What *is* real: `SIGTERM`→
  `SIGKILL` cancellation of a **named background process** via
  `kill_process(name)`, and `exec_stream()` cancellation by disconnecting
  from the same calling process. Don't write that inis.run lacks
  cancellation outright — it has this one, real primitive; naming a
  broader one that doesn't exist is the specific mistake that shipped in
  a public adapter README and had to be corrected.
- **PTY / interactive terminal** is not wired into any framework adapter
  today (OpenAI Agents, both languages).
- **Docker-volume-style mounts** are not implemented (OpenAI Agents,
  Microsoft Agent Framework) — file transfer goes through the session
  filesystem / artifacts API instead.
- **`AbortSignal` cancellation in `@inis-run/strands-agents`** can only preempt
  at a chunk boundary (stdout/stderr arriving, or process exit), not while
  the running command is silent — a real, tested limitation of
  `Session.execStream` having no cancellation parameter of its own. The
  Python sibling package doesn't share this limitation (`asyncio` task
  cancellation preempts at the event loop regardless of silence).

## No adapter matches the project's framework

Two remaining choices, in order of preference:

1. **Core SDK** (`inis` for Python / `@inis-run/sdk` for TypeScript) if you're writing the tool-calling glue
   yourself. Give the model only `exec`/`read_file`/`write_file` (or
   whatever subset the task needs) by wrapping the session in a small class
   that exposes just those methods — see `persistent_tool_loop`'s
   `BoundedToolSurface` in
   [`examples/agent-harness`](https://github.com/inis-run/sdk/tree/main/examples/agent-harness).
   Session lifecycle (create/destroy/pause/resume/fork) stays in your host
   code, not a model-callable tool, unless the task is specifically about
   managing sessions.
2. **MCP** (`https://mcp.inis.run/sse`) if the harness is itself an MCP
   client (Cursor, Claude Desktop, a generic MCP host) and you don't want
   to write any adapter code at all. See
   `https://inis.run/docs/integrations/mcp`.

## Fork's two limits (easy to conflate)

- Hard, account-independent cap: `count<=16` — `HardMaxForkCount`.
- Lower default per-request cap: `count<=4` — `DefaultMaxForkCount`,
  unless the account has a raised limit.

Either cap rejects with `validation`/`bad_request` (400, not retryable)
before any capacity check runs. Separately, an account's live-session
concurrency cap can reject a fork (or any session-creating call) with
`rate_limited` (429, retryable) — a different limit, checked at a
different point. `fork(count=...)` returns `ForkResult.children`, a list of
plain session ID strings — attach to each one to actually run anything on
it, it is not a list of live session handles. `batch_exec` loops over
session IDs sequentially server-side; it is not a concurrency primitive.
Real concurrency across forked children comes from issuing one `exec()`
per child from the client (a thread pool in Python, `Promise.all` in
TypeScript) — see `branch_and_evaluate` in
[`examples/agent-harness`](https://github.com/inis-run/sdk/tree/main/examples/agent-harness).
