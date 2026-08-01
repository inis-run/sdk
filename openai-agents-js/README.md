# @inis-run/openai-agents

Native inis.run sandbox provider for the [OpenAI Agents SDK](https://openai.github.io/openai-agents-js/) `SandboxAgent` path (`@openai/agents-core/sandbox`).

[![npm](https://img.shields.io/npm/v/%40inis-run%2Fopenai-agents.svg)](https://www.npmjs.com/package/@inis-run/openai-agents)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](https://github.com/inis-run/sdk/blob/main/LICENSE)
[![Node](https://img.shields.io/badge/node-%3E%3D18-blue.svg)](https://www.npmjs.com/package/@inis-run/openai-agents)

**Beta, upstream and here.** The Agents SDK documents its sandbox surface as beta. Verified against `@openai/agents-core==0.14.1`/`@openai/agents==0.14.1`.

## Install

```bash
npm install @inis-run/openai-agents
```

## Quickstart: attached (recommended)

One inis.run session for the whole conversation, owned by your app:

```ts
import { Client } from "@inis-run/sdk";
import { run } from "@openai/agents";
import { SandboxAgent } from "@openai/agents-core/sandbox";
import { InisSandboxSession } from "@inis-run/openai-agents";

const client = new Client();
const inisSession = await client.sessions.create({ egress: { mode: "deny" } });
const sandboxSession = InisSandboxSession.wrap(inisSession);

const agent = new SandboxAgent({ name: "assistant", model: "gpt-5.1" });
try {
  const result = await run(agent, "list files in /workspace", {
    sandbox: { session: sandboxSession },
  });
} finally {
  await inisSession.destroy(); // this app owns the session; the SDK never will
}
```

The Agents SDK never destroys a session passed via `sandbox.session` —
that lifecycle stays entirely host-controlled: one persistent session
across a whole conversation, not one sandbox per model turn.

## Owned: one `run()` call at a time

```ts
import { InisSandboxClient, InisSandboxClientOptions } from "@inis-run/openai-agents";

const result = await run(agent, "...", {
  sandbox: {
    client: new InisSandboxClient(),
    options: { egressDefault: "deny" } satisfies InisSandboxClientOptions,
  },
});
```

The SDK creates a fresh inis.run session for that call and destroys it
when the call ends. Prefer the attached path above for a real multi-turn
agent.

## What it does

- `exec`/`execCommand` (shell)
- `readFile`
- `materializeEntry` for plain `file`/`dir` manifest entries
- Exposed ports, via `session.expose()`
- Persist/hydrate workspace (owned `resume()` path) — tar+base64 over `exec()`

## Notes

`writeStdin` (PTY) is not implemented — it throws a clear error. Nested `Dir` children and `local_file`/`local_dir`/`git_repo` manifest entries are not implemented either — also a thrown error, not silently ignored. True remote cancellation of an in-flight command is not implemented: the upstream contract has no abort handle for this adapter to route through.

## Truncated output

When inis.run's own per-stream capture limit truncates a command's
output, `exec()` appends a marker to `stderr` rather than silently
dropping the fact:

```
[inis: output truncated by the sandbox's own capture limit (nonce=<16 hex chars>)]
```

The nonce is fresh per call, so a caller parsing `stderr` exactly can't be
fooled by sandboxed code printing a fake marker.

## Development

```bash
npm install
npm test     # vitest
npm run build
```

## Links

- Docs: [docs.inis.run](https://docs.inis.run)
- Source: [github.com/inis-run/sdk/tree/main/openai-agents-js](https://github.com/inis-run/sdk/tree/main/openai-agents-js)
- Homepage: [inis.run](https://inis.run)

Built and run in the EU. Your code and data never leave Europe.

## License

MIT — see [LICENSE](https://github.com/inis-run/sdk/blob/main/LICENSE).
