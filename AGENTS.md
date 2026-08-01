# inis.run — public integration guide for coding agents

This file is for a coding agent (Claude Code, Cursor, Codex, Copilot, or
similar) adding an inis.run integration to someone else's project. It is
generated (scripts/gen/docs_manifest.py) and published in two places: this
repository's public docs (`https://inis.run/docs/AGENTS.md`) and the root of the public SDK
mirror (`github.com/inis-run/sdk`).

**This is not the same file as this repository's root `AGENTS.md`.** That
file is internal contributor/fleet-operations guidance for people working
on inis.run itself (deploy procedures, node names, issue-tracking
conventions) and is unsafe and irrelevant as customer integration guidance.
If you have reached this file by way of the private monorepo, use this one
for integration work, not the root one.

## What inis.run is

A fast, isolated code-execution sandbox: each execution or session gets its
own VM, starts in under a second, tears down clean. Persistent sessions
with pause/resume and fork, full Linux, preview URLs, CLI, Python/TypeScript
SDKs, and a remote MCP server. Built and run in the EU (Ireland) — your
code and data stay in Europe.

## Install

```bash
pip install inis          # Python
npm install @inis-run/sdk  # TypeScript / Node
```

Set `INIS_API_KEY` in the host environment. Never hardcode it, and never
put it inside a session/guest command, file, fixture, or transcript you
generate — the host application owns the key, not the sandboxed code.

## Pick the right surface

These are distinct choices, not layers of the same thing:

- **Core SDK** (`inis` for Python, `@inis-run/sdk` for TypeScript) — direct session/exec/files
  control. Use this when you're writing the integration yourself rather
  than gluing into an existing agent framework.
- **Native framework adapter** — a package that wraps the core SDK for a
  specific agent framework: `inis-pydantic-ai` (Pydantic AI) and
  `@inis-run/ai-sdk` (Vercel AI SDK). Both are currently **beta** — check
  https://inis.run/docs/support-matrix.json for the exact supported framework version range
  before pinning.
- **CLI** (`https://inis.run/install.sh`) — scripts, CI pipelines, manual runs.
  No SDK required.
- **MCP** (`https://mcp.inis.run/sse`) — point an MCP-capable client at the hosted
  endpoint with an API key; sandbox tools appear automatically. No local
  process to run.

Full matrix with exact supported ranges: https://inis.run/docs/support-matrix.json

## Canonical example

Don't invent a snippet from memory. `https://github.com/inis-run/sdk/tree/main/examples/quickstart` is the one
executable scenario every first-run surface in this product reuses:
create a short-lived session with deny-by-default egress, write a value,
read it back across two `exec()` calls, reattach to the same session by ID
only, then destroy the session on both the success and the failure path.

## The one semantic rule that matters most

- **Owned session** — your code created it
  (`client.session()` / `client.sessions.create()`). Your code destroys it,
  on both the success and the failure path.
- **Attached session** — your code only bound to an existing session ID
  (`client.sessions.attach(id)`). Never destroy a session you only
  attached to — that deletes state another caller may still be using.

An integration that gets this backwards — destroying an attached session,
or leaking an owned one — does not meet this product's bar for a supported
integration, regardless of how correct the happy path looks.

## Further reading

- Docs index: https://inis.run/docs
- Session lifecycle (pause/resume, hot/warm/cold resume, TTL knobs):
  https://inis.run/docs/session-lifecycle
- Networking & egress (deny-by-default, allowlists): https://inis.run/docs/networking
- Errors (machine-readable codes, retryability): https://inis.run/docs/api/errors
- Limits: https://inis.run/docs/api/org
- OpenAPI spec: https://inis.run/docs/openapi.yaml

## What this file deliberately excludes

Internal architecture, fleet operations, deploy/ops runbooks, credentials,
node names, and GitHub issue references. If a coding agent needs any of
that to integrate inis.run, that's a bug in this file or the docs it
links — file it at https://github.com/inis-run/sdk/issues, not by reading
further into the private monorepo.
