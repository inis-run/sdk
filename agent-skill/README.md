# inis.run coding-agent skill

A portable [Agent Skill](https://github.com/inis-run/sdk/tree/main/agent-skill/inis-run) that teaches a coding agent (Claude Code today; portable to any tool that reads the same `SKILL.md` format) how to add, test, and clean up an inis.run integration in *your* project — picking the right package, handling `INIS_API_KEY` safely, session lifecycle, and verifying the result actually works. Not published to PyPI or npm; installed by cloning the mirror.

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](https://github.com/inis-run/sdk/blob/main/LICENSE)

## Install (Claude Code)

```bash
mkdir -p .claude/skills
git clone --depth 1 --branch agent-skill-v1 \
  https://github.com/inis-run/sdk /tmp/inis-sdk
cp -r /tmp/inis-sdk/agent-skill/inis-run .claude/skills/inis-run
rm -rf /tmp/inis-sdk
```

Claude Code discovers a skill by the directory it lives in
(`.claude/skills/<name>/SKILL.md`), so keep the destination directory
named `inis-run`. Pin `--branch` to a specific `agent-skill-vX` tag to
track an exact version instead of always pulling `main` — see the
[mirror's tags](https://github.com/inis-run/sdk/tags).

## What it does

Read `SKILL.md` for the authoritative content — it routes an agent to the
same [quickstart](https://docs.inis.run/docs/quickstart), example
scenarios, and framework adapters documented on `docs.inis.run` rather
than duplicating them. Covers, in order: picking the right package for
the project's existing framework, keeping `INIS_API_KEY` host-side only,
one session per agent thread, giving the model a bounded tool surface,
branching on structured error codes, and verifying the integration
actually executes, persists, and cleans up.

## Upgrade

Re-run the install command with a newer `agent-skill-vX` tag — it only
overwrites `.claude/skills/inis-run/`, a self-contained directory. It
never touches your project's own `AGENTS.md`/`CLAUDE.md` or any other
skill installed alongside it.

## Remove

```bash
rm -rf .claude/skills/inis-run
```

## Other coding agents

`SKILL.md`'s frontmatter and body are plain Markdown with no Claude
Code-specific syntax — a different tool that reads a similarly-shaped
instruction file can point at the same directory.

## Links

- Docs: [docs.inis.run/docs/integrations/agent-skill](https://docs.inis.run/docs/integrations/agent-skill)
- Source: [github.com/inis-run/sdk/tree/main/agent-skill](https://github.com/inis-run/sdk/tree/main/agent-skill)
- Homepage: [inis.run](https://inis.run)

Built and run in the EU. Your code and data never leave Europe.

## License

MIT — see [LICENSE](https://github.com/inis-run/sdk/blob/main/LICENSE).
