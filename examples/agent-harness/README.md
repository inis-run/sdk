# inis.run agent-harness examples

Five executable scenarios organised around the jobs an agent builder is
actually trying to do, not around individual API endpoints. Read
[`../quickstart`](../quickstart) first — every example here reuses its
create/attach/destroy shape and its owned-vs-attached rule; this directory
is the follow-on set the quickstart itself points to.

| Example | Proves | Python | TypeScript |
|---|---|---|---|
| Persistent tool loop | One session per agent thread, reconnected by a host-persisted ID across turns, model restricted to a bounded exec/file tool surface | [`python/persistent_tool_loop.py`](python/persistent_tool_loop.py) | [`typescript/persistent-tool-loop.ts`](typescript/persistent-tool-loop.ts) |
| Branch and evaluate | Real `fork(count=…)` API, genuine client-side concurrency across forked children, grading outside the sandbox | [`python/branch_and_evaluate.py`](python/branch_and_evaluate.py) | [`typescript/branch-and-evaluate.ts`](typescript/branch-and-evaluate.ts) |
| Pause/resume across turns | Explicit pause at end of turn, resume from a **genuinely separate OS process**, plus the paused-TTL cleanup backstop | [`python/pause_resume_across_turns.py`](python/pause_resume_across_turns.py) | [`typescript/pause-resume-across-turns.ts`](typescript/pause-resume-across-turns.ts) |
| Concurrent conversations | Two conversations running at the same time on independent sessions, with file/process isolation proven from the host's view, not asserted | [`python/concurrent_conversations.py`](python/concurrent_conversations.py) | [`typescript/concurrent-conversations.ts`](typescript/concurrent-conversations.ts) |
| Error recovery & limits | `InisError.code`/`.retryable` branching against the documented error contract, plus `timeout_ms` and `kill_process` for cancelling work | [`python/error_recovery_and_limits.py`](python/error_recovery_and_limits.py) | [`typescript/error-recovery-and-limits.ts`](typescript/error-recovery-and-limits.ts) |

The first four are the epic's required scenarios. Error recovery & limits
is a fifth, supporting example backing the "cancel or time out work" and
"recover from a paused/expired/failed/capacity-limited session"
task-oriented docs (see `docs-site/content/docs/agent-recipes.mdx`) — there
was no existing executable material for either, unlike egress control
(`docs-site/content/docs/networking.mdx`) and artifact transfer
(`docs-site/content/docs/artifacts.mdx`), which already have real, working
code samples this set links to rather than duplicates.

## What was actually run, and what wasn't

**None of these ten scripts have been executed against production inis.run
as part of this change** — this environment has no credentials for it and
is not permitted to create a live session (see the PR for the exact
constraint). What was verified instead:

- Every Python file imports cleanly against the real `inis` package, passes
  `mypy`, and fails closed with a clear message (exit 1) when
  `INIS_API_KEY` is unset — checked directly, not assumed.
- Every TypeScript file passes `tsc --noEmit` against the real built SDK
  and fails closed the same way.
- A local, mock-backed test suite (`python/tests/`, `typescript/tests/`)
  exercises every example's own functions — session-per-thread reuse,
  fork's real return shape, retry/backoff logic, cleanup on both the
  success and the failure path, and cross-conversation isolation — against
  an in-memory fake of the session backend, not a live one. See each
  test file's header for exactly what it proves and what it deliberately
  doesn't (i.e. it proves this repo's own harness logic, not the SDK's
  HTTP layer, which `sdk/python/tests` and `sdk/typescript/src/client.test.ts`
  already cover).
- Every specific `InisError.code` cited in these examples (`not_found`,
  `bad_request`, `rate_limited`/`concurrency_limit_exceeded`,
  `session_ended`) was confirmed by reading the request-handling source,
  not by observing a live response. Each file's own docstring says so
  explicitly at the point it matters.

Running any of these ten scripts for real, with a production
`INIS_API_KEY`, is unverified by this change and should be treated as such
until someone does.

## Getting the real API right

The issue that tracked this work flagged its own illustrative code as
wrong: `session.fork(n=10)` returning an iterable `ForkResult` never
existed. Every call in these examples was checked against
`sdk/python/inis/client.py` / `sdk/typescript/src/client.ts` directly, not
copied from docs prose. What was actually wrong, and what these examples do
instead:

- **Fork's real signature** is `fork(count: int = 1) -> ForkResult` (Python)
  / `fork(count: number): Promise<ForkResult>` (TypeScript), where
  `ForkResult.children` is a **list of plain session ID strings** — not
  live session handles, not a richer object. Attach to each child by ID to
  actually run anything on it. See `branch_and_evaluate.py`/`.ts`.
- **`batch_exec` is not a concurrency primitive.** Its server-side handler
  loops over session IDs and execs each one sequentially — confirmed by
  reading the handler. `branch_and_evaluate` does not use it and does not
  describe it as parallel anywhere; the real concurrency comes from
  issuing one `exec()` per forked child from the client (a thread pool in
  Python, `Promise.all` in TypeScript).
- **Fork's cost/economics story is deliberately out of scope here.** Each
  forked child is its own independently billed session (see "Owned vs.
  attached" below) — this example proves the mechanics, not the pricing.
- **Fork has two independent limits, easy to conflate.** The hard,
  account-independent cap on a single fork call is `count<=16`
  (`HardMaxForkCount`); a lower default per-request cap of `count<=4`
  (`DefaultMaxForkCount`) applies unless an account has a raised limit —
  either one rejects with a non-retryable `400 bad_request` before any
  capacity check runs. Separately, an account's live-session *concurrency*
  cap (independent of fork's own count limits) can reject with a retryable
  `429` (`rate_limited`, also seen as `concurrency_limit_exceeded`).
  `branch_and_evaluate` forks 2 to stay comfortably under the default cap
  and includes a retry-with-backoff helper for the concurrency case;
  `error_recovery_and_limits` triggers the hard-cap case deterministically
  (`count=17`) and demonstrates the retry/backoff pattern for the
  concurrency case against a scripted error sequence rather than trying to
  force a real account over its limit.
- **Destroying a session and then accessing it returns `404 not_found`**,
  confirmed by reading the session lookup path: a destroyed session is
  dropped from the live lookup table entirely. `session_ended` (410,
  "ended past recall" per `docs-site/content/docs/api/errors.mdx`) is a
  *different* outcome, reserved for a session that reached history through
  a lifecycle event (idle timeout, `max_lifetime_ms`, or a paused session
  past its `paused_ttl_ms`) rather than one explicitly deleted — not
  independently confirmed live in this change, and called out as such in
  `error_recovery_and_limits`.

## Owned vs. attached (same rule as the quickstart)

Every example destroys exactly the sessions it created (owned) and never
destroys a session it only attached to by ID. Fork is the one place this
gets subtle: forking clones a live session into fully independent child
sessions — each one is its own billed, independently-destroyable session,
not a lifecycle child that the parent's destroy cascades to. Both
`branch_and_evaluate` and `error_recovery_and_limits` destroy every child
they create.

## No module-global session state

None of these examples store a live session object at module scope. Where
a session needs to be found again later (persistent tool loop,
pause/resume), only its **ID** is persisted — in a small instance-scoped
store standing in for a real database row — exactly the pattern
`examples/quickstart` establishes for its own reconnect step.

## Running these yourself

```bash
export INIS_API_KEY=...

# Python
pip install inis
python examples/agent-harness/python/persistent_tool_loop.py
python examples/agent-harness/python/branch_and_evaluate.py
python examples/agent-harness/python/pause_resume_across_turns.py
python examples/agent-harness/python/concurrent_conversations.py
python examples/agent-harness/python/error_recovery_and_limits.py

# TypeScript
cd examples/agent-harness/typescript && npm install
npx tsx persistent-tool-loop.ts
npx tsx branch-and-evaluate.ts
npx tsx pause-resume-across-turns.ts
npx tsx concurrent-conversations.ts
npx tsx error-recovery-and-limits.ts
```

Each script exits 0 and prints its own observed proof on success, or exits
1 with a clear stderr message if `INIS_API_KEY` is unset. Every session a
script creates is destroyed before it exits, on both the success and the
failure path — see "What was actually run, and what wasn't" above for how
that claim was checked in this change (mock-backed tests, not a live run).

## Running the mock-backed test suites

```bash
# Python — no INIS_API_KEY needed, no network call made
pip install -e sdk/python
cd examples/agent-harness/python && pip install pytest && pytest

# TypeScript — no INIS_API_KEY needed, no network call made
cd sdk/typescript && npm ci && npm run build
cd examples/agent-harness/typescript
npm install --no-save ../../../sdk/typescript
npm install --no-save vitest tsx typescript @types/node
npx vitest run
```

## What's deliberately out of scope here

- **No new product capability.** This directory does not implement fork,
  pause/resume, streaming, credential-brokering, or egress — it exercises
  what already exists and is honest about what doesn't (see
  `error_recovery_and_limits`'s note on remote cancellation).
- **Egress control and artifact transfer** are not re-demonstrated as
  separate examples here: `docs-site/content/docs/networking.mdx` and
  `docs-site/content/docs/artifacts.mdx` already carry real, checked code
  samples for both, and `docs-site/content/docs/agent-recipes.mdx` links to
  them directly instead of forking a third copy of the same shape.
- **Framework adapters** (Pydantic AI, Vercel AI SDK) are not used here on
  purpose — these examples build the owned/attached, bounded-tool-surface
  pattern by hand, framework-free, so it reads as a property of the
  *pattern* every adapter is required to follow ("Definition of a
  supported integration"), not something specific to one adapter.
