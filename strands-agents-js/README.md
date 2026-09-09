# @inis-run/strands-agents

[inis.run](https://inis.run) sandbox provider for [Strands Agents](https://strandsagents.com)' `Sandbox` surface (`@strands-agents/sdk`). Gives Strands its standard `sandbox_bash` and `sandbox_file_editor` tools backed by a real, isolated inis.run session instead of the local host.

[![npm](https://img.shields.io/npm/v/%40inis-run%2Fstrands-agents.svg)](https://www.npmjs.com/package/@inis-run/strands-agents)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](https://github.com/inis-run/sdk/blob/main/LICENSE)
[![Node](https://img.shields.io/badge/node-%3E%3D18-blue.svg)](https://www.npmjs.com/package/@inis-run/strands-agents)

Verified against `@strands-agents/sdk==1.11.2`. `AbortSignal` cancellation here can only preempt at a chunk boundary (`Session.execStream` has no cancellation parameter of its own) — the companion Python package doesn't share this limitation, because `asyncio` task cancellation preempts regardless.

## Install

```bash
npm install @inis-run/strands-agents @strands-agents/sdk @inis-run/sdk
```

## Quickstart: attached (recommended)

One inis.run session for the whole conversation, owned by your app:

```typescript
import { Client } from "@inis-run/sdk";
import { Agent } from "@strands-agents/sdk";
import { InisSandbox } from "@inis-run/strands-agents";

const client = new Client();
const session = await client.sessions.create({ egress: { mode: "deny" } });
const sandbox = InisSandbox.wrap(session);

const agent = new Agent({ model, sandbox });
await agent.invoke("list files in /workspace");
await agent.invoke("now write hello.txt");
await session.destroy(); // this app owns the session; the adapter never will
```

## Owned: the adapter creates and destroys the session for you

```typescript
import { Agent } from "@strands-agents/sdk";
import { InisSandbox } from "@inis-run/strands-agents";

const sandbox = await InisSandbox.create(undefined, { egress: { mode: "deny" } });
try {
  const agent = new Agent({ model, sandbox });
  await agent.invoke("...");
} finally {
  await sandbox.close();
}
```

`InisSandbox.create()`'s `opts` is `CreateSessionOptions` from the core
`@inis-run/sdk` (plus an optional `workspaceRoot`), so `connections` —
inline Connections, scoped credential leases bound to a specific external
API origin — works the same way:

```typescript
const sandbox = await InisSandbox.create(undefined, {
  connections: [
    {
      name: "stripe",
      origin: "https://api.stripe.com",
      authentication: { type: "bearer", secret: process.env.STRIPE_KEY! },
      allow: { methods: ["GET"], paths: ["/v1/customers"] },
    },
  ],
});
```

Either way, `new Agent({ sandbox })` registers `sandbox_bash` and
`sandbox_file_editor` automatically — no separate tool wiring needed.
Session create/destroy, egress, connections, pause/resume, and fork stay
host-controlled.

## What it does

- `executeStreaming` (shell) — distinct stdout/stderr chunks, then a final `ExecutionResult`
- `executeCodeStreaming` — inherited from `PosixShellSandbox`
- Binary-safe `readFile`/`writeFile`/`removeFile`/`listFiles`, routed to inis.run's native `/files` endpoints
- Per-call `timeout` — genuinely kills the remote process on expiry
- Per-call `env` — via a validated shell prefix (inis.run's exec API fixes env at session creation)
- `AbortSignal` cancellation — real, but only at a chunk boundary, see above

## Notes

PTY isn't implemented: no equivalent in the `Sandbox` shell/file contract. `ExecutionResult.outputFiles` is always empty, same as every other shell-based `Sandbox` — read a produced file with `readFile` instead.

## Development

```bash
npm install
npm run build   # core @inis-run/sdk must be built first: (cd ../typescript && npm ci && npm run build)
npx tsc --noEmit
npm test
```

## Links

- Docs: [docs.inis.run](https://docs.inis.run)
- Source: [github.com/inis-run/sdk/tree/main/strands-agents-js](https://github.com/inis-run/sdk/tree/main/strands-agents-js)
- Homepage: [inis.run](https://inis.run)

Built and run in the EU. Your code and data never leave Europe.

## License

MIT — see [LICENSE](https://github.com/inis-run/sdk/blob/main/LICENSE).
