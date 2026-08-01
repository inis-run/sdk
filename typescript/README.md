# @inis-run/sdk

The inis.run TypeScript SDK. Run untrusted or AI-generated code in its own isolated VM.

[![npm](https://img.shields.io/npm/v/%40inis-run%2Fsdk.svg)](https://www.npmjs.com/package/@inis-run/sdk)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](https://github.com/inis-run/sdk/blob/main/LICENSE)
[![Node](https://img.shields.io/badge/node-%3E%3D18-blue.svg)](https://www.npmjs.com/package/@inis-run/sdk)

## Install

```bash
npm install @inis-run/sdk
```

## Quickstart

```typescript
import { Client } from "@inis-run/sdk";

const client = new Client(); // reads INIS_API_KEY (and INIS_BASE_URL) from the environment

const session = await client.sessions.create();
await session.exec(["echo", "hello"]);
await session.files.write("/workspace/out.txt", "hello");
console.log(await session.files.read("/workspace/out.txt"));
await session.destroy();

// Or let `await using` destroy it automatically (Node 20.4+ / TS 5.2+):
{
  await using autoSession = await client.sessions.create();
  await autoSession.exec(["echo", "hello"]);
} // destroyed here

// One-shot execution (throwaway session, no cleanup needed)
const result = await client.execute({
  language: "python",
  code: "print(42)",
});
console.log(result.stdout);

// Re-attach to a session created elsewhere, by ID
const existing = client.sessions.attach("ses_123");
```

## Timeouts

`new Client({ timeoutMs })` is **milliseconds** (JS convention). The
Python SDK's `timeout` is seconds (httpx convention) — a deliberate
per-language idiom, not drift.

## Links

- Docs: [docs.inis.run](https://docs.inis.run)
- Quickstart: [docs.inis.run/docs/quickstart](https://docs.inis.run/docs/quickstart)
- Source: [github.com/inis-run/sdk/tree/main/typescript](https://github.com/inis-run/sdk/tree/main/typescript)
- Homepage: [inis.run](https://inis.run)

Built and run in the EU. Your code and data never leave Europe.

## License

MIT — see [LICENSE](https://github.com/inis-run/sdk/blob/main/LICENSE).
