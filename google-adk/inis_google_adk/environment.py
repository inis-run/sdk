"""Native inis.run environment provider for Google ADK
(`google.adk.environment.BaseEnvironment` / `google.adk.tools.environment.
EnvironmentToolset`, package `google-adk`).

Verified against `google-adk==2.6.0` installed source, not blog posts or the
issue's original assumption. Worth recording explicitly:

1. `BaseEnvironment` (`google.adk.environment._base_environment`) and
   `EnvironmentToolset` (`google.adk.tools.environment._environment_toolset`)
   are real, public, importable classes -- but **both are decorated
   `@experimental` in ADK's own source** (`google.adk.utils.feature_decorator.
   experimental` / `google.adk.features.experimental`), matching this
   package's issue almost exactly: "Google ADK exposes an experimental
   EnvironmentToolset/BaseEnvironment provider path." This is not this
   package's own caution talking -- it is Google's.
2. Neither is re-exported from the `google.adk` top-level package (verified:
   `'BaseEnvironment' in dir(google.adk)` is `False`). Import from
   `google.adk.environment` / `google.adk.tools.environment` directly, the
   same way `InisEnvironment` below does.
3. ADK already ships two in-tree reference providers against this exact
   contract: `google.adk.integrations.e2b.E2BEnvironment` and
   `google.adk.integrations.daytona.DaytonaEnvironment`, both also
   `@experimental`. `InisEnvironment` below was written by reading both
   directly (not a "how to add a provider" doc, because none exists -- ADK
   ships its own environments in-tree rather than through a documented
   third-party extension convention like LangChain's `langchain-<provider>`
   packages).
4. **Real churn, not hypothetical.** `BaseEnvironment`/`EnvironmentToolset`'s
   public class names and import paths have been stable back to at least
   `google-adk==2.0.0` (checked directly by downloading and inspecting that
   wheel), but the toolset's internal composition has not: 2.0.0 shipped one
   `_tools.py` covering everything; 2.5.0+ split it into separate
   `ExecuteTool`/`ReadFileTool`/`EditFileTool`/`WriteFileTool` classes. Pin
   `inis-google-adk` and re-verify before assuming forward compatibility of
   anything not covered by this package's own tests, per the issue's
   instruction.
5. Upstream `ExecutionResult` (the dataclass `BaseEnvironment.execute()` is
   typed to return) has no `truncated` field -- just `exit_code`/`stdout`/
   `stderr`/`timed_out`. `execute()` below returns `InisExecutionResult`
   instead, a subclass adding that one field, copied straight from
   inis.run's own trusted `ExecResult.truncated` rather than encoded into
   `stderr` as a marker string. See `InisExecutionResult`'s own docstring
   for the earlier stderr-marker approach this replaced and why.

Two ways to put an inis.run session behind ADK's `EnvironmentToolset`
-------------------------------------------------------------------------

**1. Attached -- one inis.run session for the whole conversation
(recommended).** Create or attach to a session yourself; this environment
never creates or destroys it:

    from inis import AsyncClient
    from google.adk.agents import Agent
    from google.adk.tools.environment import EnvironmentToolset
    from inis_google_adk import InisEnvironment

    client = AsyncClient()
    session = await client.sessions.create(egress_default="deny")
    environment = InisEnvironment.wrap(session)

    agent = Agent(model="...", tools=[EnvironmentToolset(environment=environment)])
    ...
    await session.destroy()  # this app owns the session; the environment never will

Reconnect a later process to the same session (and its persistent
filesystem under /workspace) by session ID alone:

    environment = InisEnvironment.attach("ses_abc123")

**2. Owned -- the environment creates and destroys the session for you**,
matching `E2BEnvironment`/`DaytonaEnvironment`'s own shape:

    from inis_google_adk import InisEnvironment

    environment = InisEnvironment(egress_default="deny")
    toolset = EnvironmentToolset(environment=environment)
    try:
        agent = Agent(model="...", tools=[toolset])
        ...
    finally:
        await toolset.close()  # calls environment.close(), which destroys the session

`EnvironmentToolset` calls `environment.initialize()` itself on first use
(lazily, from `get_tools()`/`process_llm_request()`) -- there is no need to
call it directly in either mode, matching `E2BEnvironment`/
`DaytonaEnvironment`'s own contract exactly.

Explicitly not implemented (declared, not faked)
-------------------------------------------------------------------------
- **TypeScript, Java, and Go support.** This package wraps the Python `inis`
  SDK and Python `google-adk` only. ADK itself ships a separate Java
  implementation with its own environment contract (not verified here) and
  this repo has TypeScript and Go inis.run clients, but no equivalent
  contract/client pairing has been checked for either -- claiming support
  without that verification is exactly what the issue this package
  implements rules out.
- **True remote cancellation of an in-flight `execute()` call.** Not for
  want of a cancellation primitive -- inis.run has a real one
  (`SIGTERM`->`SIGKILL`) for named background processes -- but because
  `BaseEnvironment.execute(command, *, timeout)` is a timeout-only,
  single-call contract with no abort handle for this adapter to route
  through. The same shape gap affects the OpenAI Agents SDK, Pydantic AI,
  Vercel AI SDK, and Microsoft Agent Framework adapters in this repo for
  their own `exec()`-shaped contracts. A `timeout` still ends the call.
- **`EditFile`'s surgical replacement.** `EnvironmentToolset`'s `EditFileTool`
  is built entirely on top of `BaseEnvironment.read_file()`/`write_file()`
  (verified in `google/adk/tools/environment/_edit_file_tool.py`) -- there is
  no separate abstract method for it, so nothing beyond `read_file`/
  `write_file` is needed here for `EditFile` to work.
"""

from __future__ import annotations

import dataclasses
import os
from pathlib import Path, PurePosixPath
from typing import Any

from google.adk.environment import BaseEnvironment, ExecutionResult

from inis import AsyncClient, AsyncSession, ConnectionSpec, InisError

WORKSPACE_ROOT = "/workspace"

_NOT_FOUND_MARKERS = ("no such file or directory", "not a directory")


def _looks_like_missing_path(e: InisError) -> bool:
    if e.code in ("not_found", "file_not_found"):
        return True
    text = str(e).lower()
    return any(marker in text for marker in _NOT_FOUND_MARKERS)


def _resolve_path(path: str | os.PathLike[str]) -> str:
    """Resolve a relative path against the session's working directory,
    matching `E2BEnvironment`/`DaytonaEnvironment`'s own `_resolve_path`."""
    pure = PurePosixPath(os.fspath(path))
    if pure.is_absolute():
        return str(pure)
    return str(PurePosixPath(WORKSPACE_ROOT) / pure)


@dataclasses.dataclass
class InisExecutionResult(ExecutionResult):
    """`ExecutionResult` plus inis.run's own `truncated` signal, which the
    upstream dataclass has no field for.

    `ExecutionResult` is a plain (non-frozen, no-slots) `@dataclasses.
    dataclass`, so subclassing it to add one more defaulted field is a
    strict superset -- any caller typed against `ExecutionResult` keeps
    working unchanged, and one that wants the extra signal reads
    `.truncated` directly (or `getattr(result, "truncated", False)` if it
    can't assume this package's type is in play).

    `truncated` here is *never* derived from `stdout`/`stderr` content. It
    is copied straight from `inis.AsyncSession.exec()`'s own
    `ExecResult.truncated`, which inis.run's host sets from its own
    byte-accounting when it caps a stream, and which travels to this
    adapter as a separate field on the session-API response -- a channel
    the guest process cannot write into no matter what it prints. See
    an earlier version of `InisEnvironment.execute()` appended a
    marker string (with a random nonce) into `stderr` itself so truncation
    was at least legible to something reading raw stderr, but nothing ever
    checked that nonce against anything, so a guest printing its own
    marker-shaped text was exactly as convincing as a real one. Not
    shipping anything marker-shaped in `stderr` at all closes that off
    completely: there is no longer a string for a guest to imitate,
    because nothing parses stderr for this signal any more.
    """

    truncated: bool = False


class InisEnvironment(BaseEnvironment):
    """`BaseEnvironment` backed by one `inis.AsyncSession`.

    Construct via `InisEnvironment.wrap()` (attach an existing session),
    `.attach()` (reconnect by session ID alone), or the plain constructor
    (owned mode -- creates a new session lazily on `initialize()`, matching
    `E2BEnvironment`/`DaytonaEnvironment`'s own shape). `close()` is
    idempotent in every mode, per the `BaseEnvironment` contract.
    """

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        template: str | None = None,
        size: str | None = None,
        egress_default: str | None = "deny",
        egress_allow: list[str] | None = None,
        name: str | None = None,
        labels: dict[str, str] | None = None,
        idle_timeout_ms: int | None = None,
        max_lifetime_ms: int | None = None,
        connections: list[ConnectionSpec | dict[str, Any]] | None = None,
    ) -> None:
        """Create an owned inis.run environment (session created lazily on
        `initialize()`). Defaults to deny-by-default egress; pass
        `egress_default=None` for the account default instead. `connections`
        binds this session to one or more external API origins with a
        credential the platform injects only at request time -- see
        `inis.ConnectionSpec`."""
        self._api_key = api_key
        self._base_url = base_url
        self._session_kwargs: dict[str, Any] = {
            "template": template,
            "size": size,
            "egress_default": egress_default,
            "egress_allow": egress_allow,
            "name": name,
            "labels": labels,
            "idle_timeout_ms": idle_timeout_ms,
            "max_lifetime_ms": max_lifetime_ms,
            "connections": connections,
        }
        self._session: AsyncSession | None = None
        self._client: AsyncClient | None = None
        self._owned = True

    @classmethod
    def wrap(cls, session: AsyncSession) -> InisEnvironment:
        """Wrap an inis.run session the host already created or attached to.

        This environment never creates or destroys the underlying session --
        `close()` is a no-op here; call `await session.destroy()` (the
        `AsyncSession`, not this environment) yourself when the conversation
        is over.
        """
        if not session.session_id:
            raise ValueError(
                "InisEnvironment.wrap() requires an already-created/attached "
                "session (session.session_id is not set)"
            )
        env = cls.__new__(cls)
        env._api_key = None
        env._base_url = None
        env._session_kwargs = {}
        env._session = session
        env._client = None
        env._owned = False
        env.is_initialized = True
        return env

    @classmethod
    def attach(
        cls,
        session_id: str,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
    ) -> InisEnvironment:
        """Reconnect to an existing inis.run session by ID alone -- its
        persistent /workspace filesystem is exactly as this environment (or
        an earlier process's) left it. Never destroys the session; see
        `wrap()`."""
        session = AsyncSession.attach(session_id, base_url=base_url, token=api_key)
        return cls.wrap(session)

    def _require_session(self) -> AsyncSession:
        if self._session is None:
            raise RuntimeError("Environment is not started. Call initialize() first.")
        return self._session

    @property
    def working_dir(self) -> Path:
        self._require_session()
        return Path(WORKSPACE_ROOT)

    async def initialize(self) -> None:
        if self._session is not None:
            self.is_initialized = True
            return
        self._client = AsyncClient(token=self._api_key, base_url=self._base_url)
        kwargs = {k: v for k, v in self._session_kwargs.items() if v is not None}
        try:
            self._session = await self._client.sessions.create(**kwargs)
        except Exception:
            await self._client.close()
            self._client = None
            raise
        self.is_initialized = True

    async def close(self) -> None:
        """Idempotent: destroys the session in owned mode (safe to call more
        than once -- a second call is a no-op since the session/client are
        already cleared). A no-op in wrapped/attached mode -- the caller owns
        that session's lifecycle, matching every other attach-capable
        adapter in this repo."""
        if not self._owned:
            self.is_initialized = False
            return
        if self._session is not None:
            try:
                await self._session.destroy()
            except InisError:
                pass  # best-effort: already gone is fine
            self._session = None
        if self._client is not None:
            await self._client.close()
            self._client = None
        self.is_initialized = False

    async def execute(
        self,
        command: str,
        *,
        timeout: float | None = None,
    ) -> InisExecutionResult:
        session = self._require_session()
        timeout_ms = int(timeout * 1000) if timeout is not None else None
        result = await session.exec(command, timeout_ms=timeout_ms)

        # `stdout`/`stderr` are returned byte-for-byte as inis.run produced
        # them -- no marker text is appended. See `InisExecutionResult`
        # above for where the truncation signal actually lives and why that
        # is safe even though the guest fully controls both streams'
        # content.
        return InisExecutionResult(
            exit_code=result.exit_code,
            stdout=result.stdout,
            stderr=result.stderr,
            timed_out=bool(result.timed_out),
            truncated=result.truncated,
        )

    async def read_file(self, path: Path | str) -> bytes:
        session = self._require_session()
        resolved = _resolve_path(path)
        try:
            data = await session.read_file(resolved, encoding="base64")
        except InisError as e:
            if _looks_like_missing_path(e):
                raise FileNotFoundError(resolved) from e
            raise
        return data if isinstance(data, bytes) else data.encode("utf-8", errors="replace")

    async def write_file(self, path: Path | str, content: str | bytes) -> None:
        session = self._require_session()
        resolved = _resolve_path(path)
        payload = content.encode("utf-8") if isinstance(content, str) else content
        await session.write_file(resolved, payload)
