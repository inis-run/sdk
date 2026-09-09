---
name: inis-run
description: Use when adding, testing, or tearing down an inis.run sandbox integration in a user's own project — a hardware-isolated, sub-second-start code-execution VM for running model-written or untrusted code. Covers picking the right package (native framework adapter vs. core SDK vs. MCP), installing it, handling INIS_API_KEY safely, session lifecycle (create/attach/destroy, TTL, one session per agent thread), giving the model a bounded tool surface, interpreting structured errors, and verifying execution/persistence/isolation/cleanup actually work. Not for developing inis.run itself.
---

inis.run is a code-execution sandbox: each session is its own VM, starts in
under a second, and tears down clean. This skill routes you to the canonical
executable examples and adapters rather than re-teaching the API — read the
linked source, don't retype it from here.

## 1. Detect the environment, then pick ONE surface

Run `scripts/detect_environment.py` from the target project root. It reads
`pyproject.toml`/`requirements*.txt`/`package.json` (no network calls, no
side effects) and prints the language, package manager, any agent framework
it recognizes, and the one row below to use. If it finds nothing, read the
table yourself — don't guess from memory.

| If the project already uses… | Install | Beta? |
|---|---|---|
| Pydantic AI (`pydantic-ai`) | `pip install inis-pydantic-ai` | yes |
| Vercel AI SDK (`ai`, `zod`) | `npm install @inis-run/ai-sdk @inis-run/sdk ai zod` | yes |
| LangChain Deep Agents (`deepagents`) | `pip install langchain-inis` | yes |
| Mastra (`@mastra/core`) | `npm install @inis-run/mastra` | yes |
| OpenAI Agents SDK, Python (`openai-agents`) | `pip install inis-openai-agents` | yes |
| OpenAI Agents SDK, JS (`@openai/agents`) | `npm install @inis-run/openai-agents` | yes |
| Strands Agents, Python (`strands-agents`) | `pip install inis-strands-agents` | yes |
| Strands Agents, JS (`@strands-agents/sdk`) | `npm install @inis-run/strands-agents` | yes |
| Microsoft Agent Framework (`agent-framework`) | `pip install inis-agent-framework` | yes |
| Google ADK (`google-adk`) | `pip install inis-google-adk` | yes |
| None of the above, Python | `pip install inis` (core SDK) | no |
| None of the above, Node/TS | `npm install @inis-run/sdk` (core SDK) | no |
| Harness talks MCP directly (Cursor, Claude Desktop, a generic MCP client) | point it at `https://mcp.inis.run/sse` with an API key — no package | no |

A native adapter wraps the core SDK for that framework's own extension
point and already bounds the model's tool surface correctly — prefer it.
Falling back to the core SDK or MCP is a deliberate choice, not a
consolation: say in your own output which you picked and why (e.g. "no
adapter for CrewAI yet, using the core SDK directly"). See
`reference/routing.md` for what each adapter actually implements and does
not (every adapter README documents this explicitly; don't assume parity).
Every package above ships from a clean environment — `pip install <name>`
or `npm install <name>`, nothing else required first.

## 2. The API key never reaches the guest or the model

**This is the one rule in this skill that must never be violated.**
`INIS_API_KEY` is a host-side credential. Read it from the host process
environment (`os.environ` / `process.env`) into the SDK client constructor
only. Never:

- print it, log it, or put it in any string the model will see (system
  prompt, tool result, error message you construct yourself);
- write it into a session file, a session command (`session.exec([...,
  api_key])`), a test fixture, a transcript, or a committed file;
- pass it as a tool argument the model can set or observe.

The SDK client reads it directly from the host environment
(`Client()` / `new Client()`) — you should never see the key's value pass
through your own code as a plain string you then hand somewhere else. See
`reference/security.md` for the full checklist and why this is
non-negotiable for a security-boundary product.

## 3. One session per agent thread, no global state

Create (or attach to) exactly one session per conversation/agent thread,
keyed by a host-side thread ID → session ID mapping (a database row, a
cache entry) — never a module- or process-global session variable. Set
`idle_timeout_ms`/`max_lifetime_ms` (Python/TypeScript) short and
deliberately, and `egress_default="deny"` unless the task needs network
access. Canonical pattern:
[`persistent_tool_loop`](https://github.com/inis-run/sdk/tree/main/examples/agent-harness)
(Python and TypeScript) — reconnects a session across turns and even
across OS processes using nothing but the persisted session ID.

- **Owned** (you called `.create()`/`client.session()`) → you destroy it,
  success or failure.
- **Attached** (you called `.attach(id)` on an ID you didn't create) →
  never destroy it. Getting this backwards breaks someone else's session.

## 4. Give the model a bounded tool surface

Expose only the execution/file operations the task needs — `exec`,
`read_file`/`write_file` (or the adapter's equivalent tool names). Session
lifecycle administration (create, destroy, pause, resume, fork, expose) is
something *your* code decides, not something the model calls as a tool,
unless the task is specifically about managing sessions. Native adapters
already do this by default; if you're wiring the core SDK by hand, wrap it
the way `BoundedToolSurface` does in `persistent_tool_loop` — a thin class
exposing only the methods you intend the model to reach.

## 5. Errors: branch on `.code`, never on message text

Every non-2xx response is `{"error": "...", "code": "..."}`; the SDK raises
`InisError` with `.code`, `.status`, `.retryable`, and `.retry_after`
(`retryAfter` in TypeScript). Only take an action documented for the code
you actually received — do not invent a recovery path for a code you
don't recognize (treat it as a generic failure of that HTTP status class).
Full table: `https://inis.run/docs/api/errors`. The ones you'll hit most:

| Code | HTTP | Retryable | Action |
|---|---|---|---|
| `session_not_live` | 409 | yes | Refetch session state, retry once it's live (usually a wake race). |
| `session_ended` | 410 | no | Gone for good — create a fresh session, don't retry this one. |
| `not_found` | 404 | no | Destroyed or never existed — same as above. |
| `rate_limited` (also `concurrency_limit_exceeded`) | 429 | yes | Back off and retry; honor `.retry_after` if set. |
| `validation` (also `bad_request`) | 400 | no | Fix the request; retrying identically fails identically. |
| `payment_required` | 402 | no | Prepaid balance/spend cap — surface to the caller, don't retry silently. |

`fork(count=...)` has two independent limits easy to conflate: a hard
`count<=16` cap and a lower default `count<=4` cap, both rejecting with
`validation`/`bad_request` (not retryable) — separate from an account's
live-session concurrency cap, which is `rate_limited` (retryable). See
`error_recovery_and_limits` in
[`examples/agent-harness`](https://github.com/inis-run/sdk/tree/main/examples/agent-harness)
for real retry/backoff code against this exact distinction. `exec(...,
timeout_ms=...)` bounds one command; `kill_process(name)` stops a *named*
background process by ID from anywhere. A one-shot `exec()` call itself has
no separate remote-cancel primitive — only disconnecting `exec_stream()`
cancels a foreground command, and only from the same process. Don't claim
otherwise.

## 6. Verify before you call it done

Don't write a bespoke smoke test. Install the package, export the
project's own `INIS_API_KEY`, and run the matching canonical example
end-to-end:

```bash
# Python
python examples/quickstart/python/quickstart.py
# TypeScript
npx tsx examples/quickstart/typescript/quickstart.ts
```

(from a checkout of `https://github.com/inis-run/sdk` — clone or download
just the `examples/` directory). A clean exit proves, in one run: real
execution (`exec`), state persistence (nonce round-trip across two
`exec()` calls), a second attach-by-ID reconnect, and destroy-on-both-paths
cleanup. For isolation, two independent sessions never seeing each other's
filesystem/processes, and fork's real concurrency: run
`concurrent_conversations` and `branch_and_evaluate` from the same
`examples/agent-harness` directory. Report the real exit code and output —
don't assert success without having run it.

## Further reading

- Fork / branching: `reference/routing.md` and
  [`branch_and_evaluate`](https://github.com/inis-run/sdk/tree/main/examples/agent-harness)
- Pause/resume: `https://inis.run/docs/session-lifecycle`,
  [`pause_resume_across_turns`](https://github.com/inis-run/sdk/tree/main/examples/agent-harness)
- Egress control: `https://inis.run/docs/networking`
- File/artifact transfer: `https://inis.run/docs/artifacts`
- Limits: `https://inis.run/docs/api/org`
- Full task-oriented index: `https://inis.run/docs/agent-recipes`
