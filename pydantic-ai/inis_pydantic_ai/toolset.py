from __future__ import annotations

from typing import Any

from inis import AsyncClient, AsyncSession, InisError
from pydantic_ai.toolsets import FunctionToolset


class _SessionHolder:
    """Attaches to a pre-created session. Never creates or destroys one —
    the host app owns INIS_API_KEY and explicitly owns or attaches to the
    session."""

    def __init__(self, client: AsyncClient, session_id: str) -> None:
        self._client = client
        self._session = AsyncSession.attach(
            session_id,
            base_url=client.base_url,
            token=client.token,
            timeout=client.timeout,
        )

    def get(self) -> AsyncSession:
        return self._session

    @property
    def session_id(self) -> str:
        return self._session.session_id


class InisToolset(FunctionToolset[Any]):
    """`FunctionToolset` returned by `inis_toolset()`, with one addition:
    `aclose()` to release the local HTTP transport opened for the attached
    session.

    Nothing in pydantic-ai calls `aclose()` automatically — this toolset only
    ever attaches (never creates or destroys the session), so there is no
    lifecycle hook pydantic-ai could safely call it from without risking a
    reused toolset going cold mid-conversation. Call it yourself once the
    conversation this toolset belongs to is over, alongside — not instead of
    — `session.destroy()` if your app also owns destroying the session:

        session = await client.sessions.create(...)
        toolset = inis_toolset(session_id=session.session_id)
        try:
            ...
        finally:
            await toolset.aclose()
            await session.destroy()
    """

    def __init__(self, holder: _SessionHolder) -> None:
        super().__init__()
        self._holder = holder

    async def aclose(self) -> None:
        await self._holder.get().aclose()


def inis_toolset(
    *,
    session_id: str,
    api_key: str | None = None,
    base_url: str | None = None,
) -> InisToolset:
    """Return a Pydantic AI toolset backed by a persistent inis.run session.

    This is the attach-only, lightweight function-tool surface: it never
    creates or destroys a session — the host app must create (or attach to)
    one itself and pass its ID here: one agent thread/conversation maps to
    one session, with no global mutable session state. The bundle is
    deliberately the smallest execution/file surface a model needs —
    `execute_python`, `execute_shell`, `read_file`, `write_file`,
    `list_files` — and nothing that creates, destroys, pauses, exposes, or
    otherwise administers sessions. For that, see `inis_lifecycle_toolset()`
    and `inis_ephemeral_toolset()`, both explicit opt-ins the host adds only
    if it deliberately wants the model to have that control.

    For a capability-based alternative that follows the current
    pydantic-ai-harness sandbox contract (`run_command` / `read_file` /
    `write_file` / `list_directory`, non-zero exit reported not raised), see
    `InisSandbox` in `inis_pydantic_ai.sandbox`.

    Every tool below is a coroutine that awaits directly against
    inis.AsyncClient / inis.AsyncSession (httpx.AsyncClient under the hood) —
    no run_in_executor thread-pool wrapping. pydantic-ai's agent loop is
    async-first, so this is the native fit: a sync tool would otherwise force
    pydantic-ai to schedule it on a worker thread for every single call.

    Registered with @toolset.tool_plain (not @toolset.tool): none of these
    functions take a RunContext first argument. Since pydantic-ai 2.0,
    @toolset.tool always sets takes_ctx=True and raises a schema-generation
    UserError ("First parameter of tools that take context must be annotated
    with RunContext[...]") for a plain function like these — @toolset.tool is
    now reserved for context-aware tools specifically.
    """
    client = AsyncClient(token=api_key, base_url=base_url)
    holder = _SessionHolder(client, session_id)
    toolset = InisToolset(holder)

    @toolset.tool_plain
    async def execute_python(code: str) -> str:
        """Run Python code in the sandbox and return stdout.

        Raises InisError if the process exits non-zero.
        """
        result = await holder.get().exec(["python3", "-c", code])
        if result.exit_code != 0:
            raise InisError(
                f"python exited {result.exit_code}: {result.stderr or result.stdout}"
            )
        if result.stderr:
            return f"{result.stdout}\n[stderr]\n{result.stderr}".strip()
        return result.stdout

    @toolset.tool_plain
    async def execute_shell(command: str) -> str:
        """Run a shell command in the persistent session and return stdout.

        The session is retained between calls. Raises InisError if the
        command exits non-zero.
        """
        result = await holder.get().exec(["bash", "-lc", command])
        if result.exit_code != 0:
            raise InisError(
                f"command failed (exit {result.exit_code}): {result.stderr or result.stdout}"
            )
        return result.stdout

    @toolset.tool_plain
    async def read_file(path: str) -> str:
        """Read a file from /workspace in the sandbox."""
        return await holder.get().read_file(path)

    @toolset.tool_plain
    async def write_file(path: str, content: str) -> str:
        """Write a file under /workspace in the sandbox."""
        await holder.get().write_file(path, content)
        return f"wrote {len(content)} bytes to {path}"

    @toolset.tool_plain
    async def list_files(path: str = "/workspace") -> str:
        """List files in a directory in the sandbox (defaults to /workspace).

        Returns a newline-separated list of entry names.

        Args:
            path: Absolute directory path to list inside the sandbox.
        """
        entries = await holder.get().list_files(path)
        return "\n".join(entries)

    return toolset
