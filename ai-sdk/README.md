# @inis-run/ai-sdk

Pre-built [Vercel AI SDK](https://sdk.vercel.ai) tools for [inis.run](https://inis.run) sandboxes.

[![npm](https://img.shields.io/npm/v/%40inis-run%2Fai-sdk.svg)](https://www.npmjs.com/package/@inis-run/ai-sdk)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](https://github.com/inis-run/sdk/blob/main/LICENSE)
[![Node](https://img.shields.io/badge/node-%3E%3D18-blue.svg)](https://www.npmjs.com/package/@inis-run/ai-sdk)

**Beta.** Built and tested against `ai@^5.0.0`. Newer AI SDK majors exist and are not yet validated.

## Install

```bash
npm install @inis-run/ai-sdk @inis-run/sdk ai zod
```

## Quickstart: `inisTools()`

Your app creates (or attaches to) the session and owns `INIS_API_KEY`;
`inisTools()` just hands the model a bounded execution/file surface for it.

```typescript
import { anthropic } from "@ai-sdk/anthropic";
import { generateText, stepCountIs } from "ai";
import { Client } from "@inis-run/sdk";
import { inisTools } from "@inis-run/ai-sdk";

const client = new Client({ token: process.env.INIS_API_KEY });
const session = await client.sessions.create({ egress: { mode: "deny" } });

try {
  const result = await generateText({
    model: anthropic("claude-sonnet-5"),
    system: "Run Python in the sandbox instead of guessing.",
    messages,
    tools: inisTools({ sessionId: session.sessionId }),
    stopWhen: stepCountIs(5),
  });
  console.log(result.text);
} finally {
  await session.destroy(); // this app owns the session; inisTools() never will
}
```

`sessionId` is required and `inisTools()` never creates, destroys, or
discovers a session on its own — one agent conversation maps to one
session. To attach to a session another part of your app already owns
instead of creating one, pass its ID the same way; nothing here ever
destroys it:

```typescript
const tools = inisTools({ sessionId: existingSessionId });
```

### What it provides

| Tool | Description |
|------|-------------|
| `execute_python` | Run Python in the sandbox. Throws on non-zero exit. |
| `execute_shell` | Run an arbitrary shell command. Throws on non-zero exit. |
| `read_file` / `write_file` | File I/O under `/workspace` |
| `list_files` | List a directory (defaults to `/workspace`) |

That's the whole bundle by design — credentials and session lifecycle
administration stay host-controlled unless you opt in below.

## Owned sessions: `createOwnedInisTools()`

Let the package create the session for you instead:

```typescript
import { createOwnedInisTools } from "@inis-run/ai-sdk";

const { tools, sessionId, cleanup } = await createOwnedInisTools({ egress: "deny" });
try {
  const result = await generateText({ model, messages, tools, stopWhen: stepCountIs(5) });
  console.log(result.text);
} finally {
  await cleanup(); // destroys the session this call created
}
```

One call = one owned session for one conversation — construct a new one
per conversation, never share `tools`/`sessionId` across concurrent ones.

Pass `connections` to bind the session to external API origins with
session-scoped credential leases (see `ConnectionCreate` in `@inis-run/sdk`):

```typescript
const { tools, cleanup } = await createOwnedInisTools({
  egress: "deny",
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

## Opt-in: session administration and one-shot sandboxes

Import from `@inis-run/ai-sdk/lifecycle` (a separate entry point) only if
you want the model itself to control these:

```typescript
import { inisLifecycleTools } from "@inis-run/ai-sdk/lifecycle";

const tools = {
  ...inisTools({ sessionId: session.sessionId }),
  ...inisLifecycleTools({ sessionId: session.sessionId }), // pause/resume/checkpoint/restore/expose/capture
};
```

| Export | Tools | Hands the model |
|---|---|---|
| `inisLifecycleTools({ sessionId })` | `pause`, `resume`, `checkpoint`, `restore`, `expose_port`, `capture_artifacts` | Session administration |
| `inisEphemeralTools()` | `execute_once` | Unbounded throwaway one-shot sandboxes, no `sessionId` needed |

## MCP alternative

Already on remote MCP? Connect to `https://mcp.inis.run/sse` via the AI
SDK MCP client instead — no tool definitions in your app code.

## Links

- Docs: [docs.inis.run](https://docs.inis.run) · [AI SDK integration guide](https://docs.inis.run/docs/integrations/ai-sdk)
- Source: [github.com/inis-run/sdk/tree/main/ai-sdk](https://github.com/inis-run/sdk/tree/main/ai-sdk)
- Homepage: [inis.run](https://inis.run)

Built and run in the EU. Your code and data never leave Europe.

## License

MIT — see [LICENSE](https://github.com/inis-run/sdk/blob/main/LICENSE).
