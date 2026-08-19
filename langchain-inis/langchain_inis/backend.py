"""Native inis.run sandbox backend for LangChain Deep Agents
(`deepagents.backends.sandbox.BaseSandbox` / `SandboxBackendProtocol`,
package `deepagents`).

Verified against `deepagents==0.7.1` installed source (not blog posts, not
the issue's original assumption), plus the live docs it links to. Worth
recording explicitly, because this repo's own OpenAI Agents SDK provider
(`inis_openai_agents`) found that SDK's equivalent ABC was an
*undocumented* internal contract — this one is materially different:

1. `deepagents.backends.protocol.SandboxBackendProtocol` and
   `deepagents.backends.sandbox.BaseSandbox` ARE a public, documented
   third-party extension point. `docs.langchain.com/oss/python/deepagents/
   sandboxes` lists the built-in providers (LangSmith, Daytona, E2B, Modal,
   Runloop, Vercel, AWS AgentCore, NVIDIA OpenShell — each its own
   `langchain-<provider>` package) and says plainly: "Don't see your
   provider? You can implement your own sandbox backend. See [Contributing
   a sandbox integration](/oss/python/contributing/integrations-langchain)."
   That contributing guide, in turn, points at `langchain_tests.
   integration_tests.SandboxIntegrationTests` (package `langchain-tests`,
   verified installed at `1.1.9`) as the standard conformance suite a new
   backend is expected to subclass — see `tests/test_backend.py` in this
   package, which does exactly that against a fake inis.run HTTP backend.
2. Even so, `BaseSandbox`/`SandboxBackendProtocol` are NOT re-exported from
   `deepagents`'s or even `deepagents.backends`'s top-level `__init__.py` —
   only `BackendProtocol` (the non-sandbox base) is. Import them from
   `deepagents.backends.protocol` / `deepagents.backends.sandbox` directly,
   the same way this module and `deepagents.backends.langsmith.
   LangSmithSandbox` (the one in-tree reference implementation of a real
   provider) both do.
3. The docs also note third-party sandbox integrations are only *listed* in
   that docs table when "authored and maintained by the company that
   provides the sandbox" or once a package reaches 10,000 daily PyPI
   downloads. inis.run qualifies under the first clause (this is that
   company's own package) — see README.md for what that does and doesn't
   mean for this repo's own support matrix (it doesn't grant one; that's a
   separate, ownership-gated decision).
4. The upstream ecosystem naming convention for these packages is
   `langchain-<provider>` (`langchain-e2b`, `langchain-modal`,
   `langchain-daytona`, `langchain-runloop`, `langchain-vercel-sandbox`,
   `langchain-agentcore-codeinterpreter`, `langchain-nvidia-openshell`,
   `langsmith[sandbox]`) — this package follows that convention
   (`langchain-inis`, importable as `langchain_inis`) rather than this
   repo's own `inis_<framework>` convention used for `inis_pydantic_ai` /
   `inis_openai_agents`. That is a deliberate, documented exception; see
   README.md.

`BaseSandbox` is a **synchronous** ABC: `execute()`, `upload_files()`,
`download_files()`, and the `id` property are the only `@abstractmethod`s.
Every other protocol method (`ls`, `read`, `grep`, `glob`, `write`, `edit`,
`delete`) has a default implementation derived from those four. Async
variants (`aexecute`, etc.) default to `asyncio.to_thread(self.execute,
...)` — correct but thread-pool-bound. This backend overrides `aexecute`
and the async file-transfer methods to route through `inis.AsyncSession`
directly instead, the same choice `LangSmithSandbox` makes for the same
reason: deepagents' own graph execution is normally async, and one worker
thread per concurrent tool call scales worse than a native coroutine per
call — this matters for the two-concurrent-thread isolation case this
package's own tests exercise.

What's implemented, what isn't
-------------------------------------------------------------------------
- `execute`/`aexecute`, `upload_files`/`aupload_files`,
  `download_files`/`adownload_files`, and every derived file operation
  (`ls`/`read`/`write`/`edit`/`grep`/`glob`/`delete`) — real, backed by one
  `inis.Session`/`inis.AsyncSession` pair bound to the same session id.
- `enable_capture_offload = False`, **verified live against `api.inis.run`,
  not assumed.** The obvious guess — inis.run sandbox images are a normal
  Linux userland with `bash`/`tar`/`base64`/coreutils, so this should be
  safe to enable the same way `LangSmithSandbox` does — turned out wrong
  and was caught by a live run, not code review: for a plain (non-
  `CompositeBackend`) `BaseSandbox`, deepagents' `FilesystemMiddleware`
  hardcodes its capture-at-source offload path to `/large_tool_results`
  (see `_resolve_capture` in `deepagents/middleware/filesystem.py` —
  `artifacts_root` defaults to `"/"` unless the backend is a
  `CompositeBackend`). inis.run sessions run their command as a non-root
  `sandbox` user confined to `/workspace`; a real session confirms this
  directly: `mkdir -p /large_tool_results` → `mkdir: cannot create
  directory '/large_tool_results': Permission denied` (`id` in that same
  session: `uid=999(sandbox) gid=999(sandbox)`). With capture offload
  enabled, every `execute()` call above the tool's inline-output budget
  silently failed against production (`bash: line N: /large_tool_results/
  <call_id>: No such file or directory` — the wrapper's own `mkdir -p ...
  2>/dev/null` swallows the permission error, so the *first* visible
  symptom is a later redirect into a directory that was never created).
  `enable_capture_offload=False` makes `execute_with_offload` run the
  command unwrapped instead (see `BaseSandbox.execute_with_offload`'s own
  fallback). That is the only correct choice available, but it is NOT a
  wash: deepagents' *generic* per-tool-call eviction
  (`FilesystemMiddleware.wrap_tool_call`, which runs on every tool result
  regardless of `enable_capture_offload`) also routes through the same
  hardcoded `/large_tool_results` prefix via `backend.write()`'s
  `_write_preflight` (`mkdir -p` the parent dir) — verified to fail with
  the identical permission error, at which point deepagents does not
  raise, it just keeps the original, full-size message
  (`_offload_tool_message_content` returns `None` on a write error). So
  BOTH of deepagents' size-eviction routes are inert here, not just the
  capture-at-source one: nothing caps a large `execute()` result before
  it reaches the model. See README.md's "Large `execute()` output is not
  capped on this backend" for the consequence and what a caller should do
  about it — this is not a corner case to bury in a docstring.
- **Session lifecycle is entirely host-owned.** Unlike the OpenAI Agents
  SDK's `BaseSandboxClient` (which has `create`/`resume`/`delete` hooks the
  runtime calls automatically), `SandboxBackendProtocol` has *no*
  lifecycle hooks at all — `create_deep_agent(backend=...)` never creates
  or destroys a backend itself. That makes the owned/attached split purely
  a matter of which classmethod constructs this backend: `.create()` makes
  a brand-new inis.run session your app must destroy (`finally:
  backend.destroy()`); `.attach()` binds to one your app does not own and
  never destroys it. See README.md.
- **Not implemented, declared rather than faked:** true remote cancellation
  of an in-flight `execute()`/`aexecute()` call. This is not a missing
  inis.run primitive — inis.run has a real one (`SIGTERM` then `SIGKILL`,
  confirmed in `guest/agent/process.go`'s `killProcessOp`) for named
  background processes, exposed as `DELETE
  /v1/sessions/{id}/processes/{name}`. It is unreachable through *this*
  contract: `BaseSandbox.execute(command, *, timeout: int | None)` is a
  synchronous, single-call, timeout-only contract with no abort token and
  no handle returned for later cancellation, so there is no hook here to
  route to that primitive. The Pydantic AI, Vercel AI SDK, and OpenAI
  Agents adapters have the identical framework-contract-shape gap for the
  same reason — none of those contracts carry a cancellation hook either
  (contrast Mastra's `executeCommand(cmd, { abortSignal })`, which does,
  and which the Mastra provider routes to the same primitive). A `timeout`
  still ends the call here. Also not implemented: streaming output
  (deepagents' `ExecuteResponse` has no streaming shape to fill even if
  inis.run's own `exec_stream()` were wired through here).
"""

from __future__ import annotations

import base64
import binascii
from typing import Any

from deepagents.backends.protocol import (
    ExecuteResponse,
    FileDownloadResponse,
    FileUploadResponse,
)
from deepagents.backends.sandbox import BaseSandbox

from inis import AsyncSession, ConnectionSpec, InisError, Session

# `FileOperationError` literals from `deepagents.backends.protocol`.
_FILE_NOT_FOUND = "file_not_found"
_PERMISSION_DENIED = "permission_denied"
_IS_DIRECTORY = "is_directory"
_INVALID_PATH = "invalid_path"


def _map_file_error(e: InisError) -> str:
    """Map a structured `InisError` (code/status/retryable/request_id) onto
    the closest `FileOperationError` literal the deepagents contract
    defines, falling back to a backend-specific string (explicitly allowed
    by the protocol) rather than flattening every failure onto one bucket."""
    code = e.code
    if code in ("not_found", "file_not_found"):
        return _FILE_NOT_FOUND
    if code == "invalid_path":
        return _INVALID_PATH
    if code == "permission_denied":
        return _PERMISSION_DENIED
    if code == "is_directory":
        return _IS_DIRECTORY
    return f"inis:{code}: {e}"


def _coerce_bytes(data: str | bytes) -> bytes:
    if isinstance(data, bytes):
        return data
    try:
        return base64.b64decode(data, validate=True)
    except (binascii.Error, ValueError):
        return data.encode("utf-8", errors="replace")


def _execute_response(result: Any, *, timeout: int | None) -> ExecuteResponse:
    output = result.stdout or ""
    if result.stderr:
        output = f"{output}\n{result.stderr}" if output else result.stderr
    if result.timed_out:
        suffix = f" (timed out after {timeout}s)" if timeout else " (timed out)"
        marker = f"[inis: command timed out{suffix}]"
        output = f"{output}\n{marker}" if output else marker
        return ExecuteResponse(output=output, exit_code=None, truncated=bool(result.truncated))
    return ExecuteResponse(output=output, exit_code=result.exit_code, truncated=bool(result.truncated))


class InisSandboxBackend(BaseSandbox):
    """`BaseSandbox` backed by one `inis.Session` (+ a lazily-attached
    `inis.AsyncSession` twin for real async).

    Do not construct directly in application code — use
    `InisSandboxBackend.create(...)` (owned) or
    `InisSandboxBackend.attach(session_id, ...)` (attached). Both return an
    instance ready to pass straight through
    `create_deep_agent(backend=...)`.
    """

    def __init__(self, session: Session) -> None:
        if not session.session_id:
            raise ValueError(
                "InisSandboxBackend requires an already-created/attached "
                "session (session.session_id is not set)"
            )
        # False, verified live: deepagents hardcodes its capture-at-source
        # offload path to /large_tool_results for a plain (non-Composite)
        # BaseSandbox, and inis.run sessions run as a non-root user
        # confined to /workspace — mkdir -p /large_tool_results fails with
        # Permission denied on a real session. See module docstring.
        self.enable_capture_offload = False
        self._session = session
        self._async_session: AsyncSession | None = None

    # -- construction -----------------------------------------------------

    @classmethod
    def create(
        cls,
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
        timeout: float = 120.0,
    ) -> InisSandboxBackend:
        """Create a brand-new, **owned** inis.run session and wrap it.

        This app owns the returned session: destroy it yourself when the
        conversation/thread is over —

            backend = InisSandboxBackend.create()
            try:
                agent = create_deep_agent(model=..., backend=backend)
                ...
            finally:
                backend.destroy()

        `create_deep_agent(backend=...)` never creates or destroys a
        backend itself (`SandboxBackendProtocol` has no lifecycle hooks at
        all, unlike e.g. the OpenAI Agents SDK's `BaseSandboxClient`) — the
        `finally` above is the only thing that ever cleans this up.
        Defaults to deny-by-default egress; pass `egress_default=None` for
        the account default instead. `connections` binds this session to
        one or more external API origins with a credential the platform
        injects only at request time — see `inis.ConnectionSpec`.
        """
        session = Session(
            base_url=base_url or _resolve_base_url_placeholder(),
            token=api_key or _resolve_token_placeholder(),
            template=template,
            size=size,
            egress_default=egress_default,
            egress_allow=egress_allow,
            name=name,
            labels=labels,
            idle_timeout_ms=idle_timeout_ms,
            max_lifetime_ms=max_lifetime_ms,
            connections=connections,
            timeout=timeout,
        )
        session.__enter__()
        return cls(session)

    @classmethod
    def attach(
        cls,
        session_id: str,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: float = 120.0,
    ) -> InisSandboxBackend:
        """Bind to an existing inis.run session by ID without creating or
        ever destroying it.

        Use this for a session another part of your app (or a previous
        turn/process) already owns — one agent thread/conversation maps to
        one session, resolved here by whatever key your app already uses
        to look up `session_id` (a LangGraph `thread_id`, a DB row, ...).
        This backend never stores that mapping itself; see
        `examples/langgraph_recipe.py` for a worked thread-id -> session-id
        store.
        """
        session = Session.attach(
            session_id,
            base_url=base_url,
            token=api_key,
            timeout=timeout,
        )
        return cls(session)

    # -- identity -----------------------------------------------------------

    @property
    def id(self) -> str:
        assert self._session.session_id is not None
        return self._session.session_id

    @property
    def session_id(self) -> str:
        """Alias for `id` using the inis.run SDK's own vocabulary."""
        return self.id

    # -- lifecycle: host-owned, never called by deepagents itself ----------

    def destroy(self) -> None:
        """Destroy the underlying inis.run session.

        Call this yourself, in `finally`, only for a backend your app
        created via `.create()`. Never call it on a backend from
        `.attach()` — that session belongs to someone else.
        """
        self._session.destroy()

    async def adestroy(self) -> None:
        """Async twin of `destroy()`, via the lazily-attached async
        session (created on first `aexecute()`/async file call, or here if
        this is the first async call of this backend's life)."""
        session = self._aget_async_session()
        await session.destroy()

    def close(self) -> None:
        """Close this backend's local HTTP transport(s) without destroying
        the inis.run session server-side. Safe on an attached backend;
        safe to call again after `destroy()`."""
        self._session.close()

    async def aclose(self) -> None:
        """Async twin of `close()` — also releases the async transport if
        `aexecute()`/`aupload_files()`/etc. ever created one."""
        self._session.close()
        if self._async_session is not None:
            await self._async_session.aclose()
            self._async_session = None

    def _aget_async_session(self) -> AsyncSession:
        if self._async_session is None:
            self._async_session = AsyncSession.attach(
                self._session.session_id,  # type: ignore[arg-type]
                base_url=self._session._base_url,
                token=self._session._token,
                timeout=self._session._timeout,
            )
        return self._async_session

    # -- exec -----------------------------------------------------------------

    def execute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:
        timeout_ms = int(timeout * 1000) if timeout else None
        try:
            result = self._session.exec(command, timeout_ms=timeout_ms)
        except InisError as e:
            return ExecuteResponse(
                output=f"inis.run exec failed: {_map_file_error(e)}",
                exit_code=None,
                truncated=False,
            )
        return _execute_response(result, timeout=timeout)

    async def aexecute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:  # noqa: ASYNC109
        timeout_ms = int(timeout * 1000) if timeout else None
        session = self._aget_async_session()
        try:
            result = await session.exec(command, timeout_ms=timeout_ms)
        except InisError as e:
            return ExecuteResponse(
                output=f"inis.run exec failed: {_map_file_error(e)}",
                exit_code=None,
                truncated=False,
            )
        return _execute_response(result, timeout=timeout)

    # -- files ----------------------------------------------------------------

    def upload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        responses: list[FileUploadResponse] = []
        for path, content in files:
            if not path.startswith("/"):
                responses.append(FileUploadResponse(path=path, error=_INVALID_PATH))
                continue
            try:
                self._session.write_file(path, content)
            except InisError as e:
                responses.append(FileUploadResponse(path=path, error=_map_file_error(e)))
            else:
                responses.append(FileUploadResponse(path=path, error=None))
        return responses

    async def aupload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        session = self._aget_async_session()
        responses: list[FileUploadResponse] = []
        for path, content in files:
            if not path.startswith("/"):
                responses.append(FileUploadResponse(path=path, error=_INVALID_PATH))
                continue
            try:
                await session.write_file(path, content)
            except InisError as e:
                responses.append(FileUploadResponse(path=path, error=_map_file_error(e)))
            else:
                responses.append(FileUploadResponse(path=path, error=None))
        return responses

    def download_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        responses: list[FileDownloadResponse] = []
        for path in paths:
            if not path.startswith("/"):
                responses.append(FileDownloadResponse(path=path, content=None, error=_INVALID_PATH))
                continue
            try:
                data = self._session.read_file(path, encoding="base64")
            except InisError as e:
                responses.append(FileDownloadResponse(path=path, content=None, error=_map_file_error(e)))
                continue
            responses.append(FileDownloadResponse(path=path, content=_coerce_bytes(data), error=None))
        return responses

    async def adownload_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        session = self._aget_async_session()
        responses: list[FileDownloadResponse] = []
        for path in paths:
            if not path.startswith("/"):
                responses.append(FileDownloadResponse(path=path, content=None, error=_INVALID_PATH))
                continue
            try:
                data = await session.read_file(path, encoding="base64")
            except InisError as e:
                responses.append(FileDownloadResponse(path=path, content=None, error=_map_file_error(e)))
                continue
            responses.append(FileDownloadResponse(path=path, content=_coerce_bytes(data), error=None))
        return responses


def _resolve_base_url_placeholder() -> str | None:
    # `Session.__init__` requires `base_url: str` (not optional), but
    # `Session.attach`'s own env-resolution helpers (`_resolve_base_url`)
    # are private to `inis.client`. Passing `None` through here and letting
    # `Session`'s own httpx calls fail is wrong; instead resolve the same
    # env var (`INIS_BASE_URL`) directly so `.create()` matches `.attach()`
    # and every other adapter's default resolution.
    import os

    return os.environ.get("INIS_BASE_URL") or "https://api.inis.run"


def _resolve_token_placeholder() -> str:
    import os

    token = os.environ.get("INIS_API_KEY")
    if not token:
        raise InisError(
            "INIS_API_KEY is not set. InisSandboxBackend.create() reads it "
            "from the environment, never from model output or any value "
            "visible to a SandboxAgent/deep-agent tool call.",
            code="missing_api_key",
        )
    return token
