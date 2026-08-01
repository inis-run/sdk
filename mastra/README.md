# @inis-run/mastra

[inis.run](https://inis.run) sandbox provider for [Mastra](https://mastra.ai) `Workspace` — implements the `WorkspaceSandbox` contract from `@mastra/core/workspace`, backed by a real, isolated inis.run session instead of a local process.

[![npm](https://img.shields.io/npm/v/%40inis-run%2Fmastra.svg)](https://www.npmjs.com/package/@inis-run/mastra)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](https://github.com/inis-run/sdk/blob/main/LICENSE)
[![Node](https://img.shields.io/badge/node-%3E%3D18-blue.svg)](https://www.npmjs.com/package/@inis-run/mastra)

**Beta.** Verified against `@mastra/core==1.55.0`.

## Install

```bash
npm install @inis-run/mastra @mastra/core @inis-run/sdk
```

## Quickstart: attached (recommended)

One inis.run session for a whole conversation, owned by your app:

```typescript
import { Client } from "@inis-run/sdk";
import { Workspace } from "@mastra/core/workspace";
import { InisSandbox } from "@inis-run/mastra";

const client = new Client();
const session = await client.sessions.create({ egress: { mode: "deny" } });

const workspace = new Workspace({ sandbox: new InisSandbox({ session }) });
try {
  const result = await workspace.sandbox?.executeCommand?.("ls /workspace");
} finally {
  await session.destroy(); // this app owns the session; InisSandbox never will
}
```

`InisSandbox` never destroys, pauses, or resumes a session passed this
way. Reattach later with the session ID:

```typescript
const sandbox = new InisSandbox({ session: existingSessionId, client });
```

## Owned: the sandbox manages its own session

```typescript
const workspace = new Workspace({
  sandbox: new InisSandbox({ egressDefault: "deny", size: "medium" }),
});

await workspace.sandbox?.executeCommand?.("ls /workspace"); // creates the session lazily
await workspace.destroy(); // destroys it
```

`stop()`/`start()` pause and resume the real session (VM, filesystem, and
background processes freeze in place and thaw exactly where they left
off) rather than recreating it. `clone()` builds an independent owned
sibling.

## What it does

- `executeCommand` (foreground)
- Background processes: `spawn`/`list`/`get`/`kill`
- Remote cancellation (`abortSignal`, timeout) — genuine `SIGTERM`→`SIGKILL` server-side
- Output streaming (`onStdout`/`onStderr`) — a live log follow, not polling
- Bulk file write (`writeFiles`)
- Exposed ports (`networking.getPortUrl`), via `session.expose()`
- `session` accessor (pause/resume/fork/checkpoint/expose/files) — host-side only, never a model tool

## Notes

`ProcessHandle.sendStdin` is not implemented — no stdin-write endpoint for a named background process. `mount`/`unmount` are not supported: both throw `MountNotSupportedError` rather than silently no-op'ing; inis.run has no FUSE-style mount primitive. Each unsupported call throws a clear error so a caller finds out immediately.

Two known platform-level rough edges: `killProcess` can report `true` for a process that was already exiting on its own, and a background process exiting with code `0` can occasionally come back with no exit code at all (a wire-format bug, tracked upstream separately) — both real, not papered over by this adapter.

## Development

```bash
npm install
npm test     # vitest
npm run build
```

## Links

- Docs: [docs.inis.run](https://docs.inis.run)
- Source: [github.com/inis-run/sdk/tree/main/mastra](https://github.com/inis-run/sdk/tree/main/mastra)
- Homepage: [inis.run](https://inis.run)

Built and run in the EU. Your code and data never leave Europe.

## License

MIT — see [LICENSE](https://github.com/inis-run/sdk/blob/main/LICENSE).
