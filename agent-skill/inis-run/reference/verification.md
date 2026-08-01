# Verifying an integration actually works

"Tests pass" and "it should work" are not verification. Prove these four
things happened, with real output, before calling an integration done —
each maps to a specific canonical example rather than a new script you'd
have to write and maintain yourself:

| Property | How to prove it | Canonical example |
|---|---|---|
| **Execution** | Run a command in the session, check its real exit code / stdout. | `examples/quickstart` |
| **Persistence** | Write a value in one `exec()`/`write_file` call, read it back in a *separate* call — and again after re-attaching by session ID only (no in-memory handle carried over). | `examples/quickstart`, `persistent_tool_loop` |
| **Isolation** | Two independent sessions cannot see each other's filesystem or processes — checked from the host's own observation, not asserted. | `concurrent_conversations` |
| **Cleanup** | The session your code owns is destroyed on both the success path and a forced failure path; a subsequent access to its ID returns `not_found` (404). An attached-only session is never destroyed. | every example in `examples/agent-harness` — see each one's `finally`/`try`/`with` block |

All five scripts live in
[`examples/agent-harness`](https://github.com/inis-run/sdk/tree/main/examples/agent-harness)
(Python and TypeScript), plus
[`examples/quickstart`](https://github.com/inis-run/sdk/tree/main/examples/quickstart)
for the base case. Run the one(s) that match what you just built, with the
project's own `INIS_API_KEY` exported, and report the actual exit code and
printed output — not a paraphrase of what you expect it to say.

## What "done" looks like in your own report

- Which package/adapter you installed and why (table in `SKILL.md` §1).
- The exact command you ran to verify, and its real output (truncated is
  fine; fabricated is not).
- Confirmation the session(s) your code created were destroyed — quote the
  destroy confirmation line the example itself prints, or a follow-up
  `not_found` on the same ID.
- If verification could not be run (no key available, no sandbox account
  provisioned yet), say so explicitly instead of asserting success.
