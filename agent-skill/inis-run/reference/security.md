# Handling INIS_API_KEY safely

`INIS_API_KEY` authenticates every request to inis.run on behalf of the
account that owns it. It is a host-side secret: the code your agent
generates and the model running inside the sandbox must never see it,
carry it, or be able to derive it. This is the single most important rule
in this skill — get everything else wrong before this.

## Do

- Read it once from the host process environment
  (`os.environ["INIS_API_KEY"]` / `process.env.INIS_API_KEY`) and let the
  SDK client pick it up automatically (`Client()` / `new Client()` resolve
  it from the environment with no argument needed).
- Keep it out of any string you build for the model: system prompt, tool
  descriptions, tool call results, error messages you construct yourself.
- Keep it out of anything that leaves the host process as data: a file
  written into the session's filesystem, a `session.exec([...])` argument
  list, a log line, a test fixture, a committed file, a screenshot, a
  transcript.
- If you must confirm the key is present, check truthiness only
  (`bool(os.environ.get("INIS_API_KEY"))`) — never print the value, not
  even truncated or partially masked.
- Scope credentials per project/environment the way you already would for
  any other API key (a secrets manager, an untracked `.env` file, CI
  secrets) — inis.run doesn't require anything different here.

## Don't

- Don't pass the key as a tool argument the model can set, read, or echo
  back.
- Don't have the model write code that reads `INIS_API_KEY` from its own
  environment inside the sandbox — the sandbox's guest environment should
  not have this variable at all; the host process is the only thing that
  authenticates to the inis.run API.
- Don't put a real key in a fixture, example, or test — use a placeholder
  (`sk_...` shaped string is fine as a *pattern* to show format; never a
  live value) or point at a restricted test key already provisioned
  out-of-band for that purpose.
- Don't treat "it's on `PyPI`/`npm`" or any registry badge as evidence a
  key or the integration works. A live, working call is evidence; a
  package listing is not, and claiming otherwise is exactly the kind of
  unverified claim this skill's own verification step (`SKILL.md` §6)
  exists to avoid.

## If a key leak is suspected

Treat it like any other leaked API credential: rotate it immediately
through the account's key management surface, then re-run whatever step
depends on it with the new key. Don't wait for confirmation that it was
actually misused before rotating.
