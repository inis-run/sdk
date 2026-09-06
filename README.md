# inis.run SDKs

Client SDKs and toolkit integrations for [inis.run](https://inis.run) — fast,
hardware-isolated sandboxes for running untrusted or AI-generated code, built
and run in the EU.

This repo is a **read-only mirror**, synced automatically from the private
inis.run monorepo on each SDK release. It exists so the packages below have
somewhere public to point their `Repository`/`Homepage`/`Bug Tracker` links.
Please don't open PRs here — they'll be overwritten by the next sync. Open an
issue instead (here or via `support@inis.run`) and it'll get pulled into the
monorepo and released normally; the fix then shows up here on the next tag.

| Package | Language | Install |
| --- | --- | --- |
| [`python/`](./python) | Python | `pip install inis` |
| [`typescript/`](./typescript) | TypeScript | `npm install @inis-run/sdk` |
| [`ai-sdk/`](./ai-sdk) | TypeScript | `npm install @inis-run/ai-sdk` |
| [`mastra/`](./mastra) | TypeScript | `npm install @inis-run/mastra` |
| [`openai-agents-js/`](./openai-agents-js) | TypeScript | `npm install @inis-run/openai-agents` |
| [`strands-agents-js/`](./strands-agents-js) | TypeScript | `npm install @inis-run/strands-agents` |
| [`pydantic-ai/`](./pydantic-ai) | Python | `pip install inis-pydantic-ai` |
| [`langchain-inis/`](./langchain-inis) | Python | `pip install langchain-inis` |
| [`openai-agents-python/`](./openai-agents-python) | Python | `pip install inis-openai-agents` |
| [`strands-agents-python/`](./strands-agents-python) | Python | `pip install inis-strands-agents` |
| [`agent-framework/`](./agent-framework) | Python | `pip install inis-agent-framework` |
| [`google-adk/`](./google-adk) | Python | `pip install inis-google-adk` |
| [`agent-skill/`](./agent-skill) | — | Not a package — clone this directory |

See each package's own README for usage. Docs: [docs.inis.run](https://docs.inis.run).

- [`examples/quickstart/`](./examples/quickstart) — the one canonical, executable
  Python/TypeScript quickstart every first-run surface in the docs reuses or is
  checked against.
- [`AGENTS.md`](./AGENTS.md) — integration guide for a coding agent (Claude Code,
  Cursor, Codex, etc.) adding an inis.run integration to your project.

MIT licensed — see [LICENSE](./LICENSE).
