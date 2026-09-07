# inis.run quickstart

The one canonical scenario every first-run surface in this repo (console,
marketing site, root README, docs site, adapter READMEs) shows a version
of. If you're correcting a quickstart snippet anywhere else, treat
[`python/quickstart.py`](./python/quickstart.py) and
[`typescript/quickstart.ts`](./typescript/quickstart.ts) as the source of
truth — fix them first, then make the other surface match. CI enforces
this with [`scripts/check-quickstart-surfaces.py`](../../scripts/check-quickstart-surfaces.py),
which fails the build on drift; see that file for exactly what it checks.

## The scenario

Both examples implement the same lifecycle:

1. Install the package (`pip install inis` / `npm install inis`).
2. Read `INIS_API_KEY` from the host environment — never hardcode it, never
   put it in a session/guest command, file, fixture, transcript, or
   screenshot. The examples fail fast with a clear message if it's unset.
3. Create a short-lived session with deny-by-default egress.
4. Write a nonce to the session's filesystem.
5. Run a second command that reads the nonce back — proving state
   persisted across `exec()` calls, not just within one call.
6. Attach to the same session purely by its ID (simulating a later
   process/request reconnecting) and read the nonce back again.
7. Destroy the session the script **owns**, on both the success and the
   failure path.

## Owned vs. attached sessions

This is the one semantic distinction that matters everywhere a session ID
crosses a boundary:

- **Owned** — the caller created it (`client.session()` / `client.sessions.create()`).
  It cleans it up, success or failure. Both examples do this in a
  `with`/`finally` block around the session they created.
- **Attached** — the caller only bound to an existing session ID
  (`client.sessions.attach(id)`). It never destroys that session — deleting
  something you don't own is how you break someone else's conversation/job
  out from under them. Neither example's attach step calls `destroy()`.

A framework adapter that gets this backwards (destroys an attached session,
or leaks an owned one) fails the "Definition of a supported integration"
bar regardless of how correct its happy path looks.

## Endpoint, package, timeout, egress, TTL, cleanup

| | Python | TypeScript |
|---|---|---|
| Endpoint | `https://api.inis.run` (`INIS_BASE_URL` to override) | same |
| Package | `inis` (`pip install inis`) | `inis` (`npm install inis`) |
| Supported range | Python ≥3.10 — see `sdk/python/pyproject.toml` | Node ≥18 — see `sdk/typescript/package.json` |
| HTTP timeout | 120s default (`Client(timeout=...)`) | 120_000ms default (`new Client({ timeoutMs })`) |
| Egress | deny-by-default (`egress_default="deny"`) | deny-by-default (`egress: { mode: "deny" }`) |
| TTL | `idle_timeout_ms` / `max_lifetime_ms`, set short for this disposable demo | same, camelCase |
| Cleanup | `with client.session(...):` destroys in `__exit__`, both paths | `try/finally` destroys in `finally`, both paths |

These are the two knobs (egress, TTL) every supported integration is
expected to set explicitly rather than inherit a default — see each
example's file header for the exact reasoning.

## Design decisions

- **One script per language, not one script per surface.** A single
  runnable file per language is the source; every other surface either
  reuses it mechanically or is checked against it — see the CI job. No
  per-framework fork of the "same" quickstart.
- **The `reconnect` step is simulated in-process, not a second OS process.**
  A literal second process would add subprocess/IPC plumbing without
  changing what's being demonstrated: that `client.sessions.attach(id)`
  built from nothing but a persisted ID gets you back the same live
  filesystem. Real multi-process/multi-request use looks identical, just
  with the ID crossing a process boundary instead of a local variable.
- **CI installs the workspace build, not the registry package.** The
  scripts above `import`/`require` the package by its real published name
  (`inis`) so the example text a customer copy-pastes is exactly what they
  will run. CI substitutes a local install of `sdk/python` /
  `sdk/typescript` for that same import — see
  `.github/workflows/quickstart-drift.yml`. That substitution is a
  CI-only detail; it is not reflected in the example text itself.
- **Follow-ons stay follow-ons.** Persistent agent loops, fork, and
  pause/resume are real and documented (`docs/session-lifecycle.mdx`,
  `docs-site/content/docs/concepts.mdx`), but out of scope here on
  purpose — this quickstart is deliberately not a kitchen-sink demo.
  Follow-on executable examples for those live in
  [`../agent-harness`](../agent-harness).

## Running it yourself

```bash
export INIS_API_KEY=...

# Python
pip install inis
python examples/quickstart/python/quickstart.py

# TypeScript
npm install inis tsx
npx tsx examples/quickstart/typescript/quickstart.ts
```

Both exit 0 and print a destroy confirmation on success; both exit 1 with
a clear stderr message if `INIS_API_KEY` is unset or the scenario fails.
