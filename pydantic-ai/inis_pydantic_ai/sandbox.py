"""`InisSandbox`: a pydantic-ai *capability* (as opposed to `inis_toolset()`'s
plain toolset) shaped to match the current pydantic-ai-harness sandbox
contract — see e.g. `pydantic_ai_harness.modal_sandbox.ModalSandbox`. Same
idea: a dataclass capability whose `get_toolset()` returns command execution
plus bounded file read/write/list tools, registered via
`Agent(capabilities=[...])`.

One deliberate divergence from `ModalSandbox`: it defaults to a fresh
sandbox *per run*, because Modal sandboxes are not inis.run's persistence
primitive. inis.run sessions are — one agent thread/conversation maps to
one session, with no global mutable session state, and recreating one per
run would throw away the exact feature this product sells. So
`InisSandbox` always binds to ONE session for the life of the `Agent`
instance it's attached to (i.e. for the whole conversation, however many
`agent.run()` calls that takes), and never creates or destroys that session
itself:

- `session_id=...` attaches to a session the host already created (or is
  itself attached to). Never destroyed by this capability.
- `session=...` injects an `AsyncSession` object the host already opened
  (e.g. via `await client.sessions.create(...)`), for when the host wants to
  read `session.session_id` or otherwise manage it directly. Never touched
  by this capability beyond running tools against it.

Exactly one of the two is required — this capability creates nothing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from inis import AsyncSession, InisError
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.exceptions import ModelRetry
from pydantic_ai.tools import AgentDepsT
from pydantic_ai.toolsets import AgentToolset, FunctionToolset

_DEFAULT_COMMAND_TIMEOUT_MS = 60_000
_DEFAULT_MAX_OUTPUT_CHARS = 20_000

_INSTRUCTIONS = (
    "You have a persistent inis.run sandbox: an isolated Linux VM that stays "
    "running for the rest of this conversation. Use `run_command` to run shell "
    "commands in it, and `read_file` / `write_file` / `list_directory` to manage "
    "files (defaults to /workspace). Files and any background state persist "
    "between calls. A non-zero exit is reported in the output, not raised, so "
    "you can react to it."
)


@dataclass(kw_only=True)
class InisSandbox(AbstractCapability[AgentDepsT]):
    """Gives the agent a persistent inis.run sandbox: `run_command` plus
    bounded file read/write/list, following the pydantic-ai-harness sandbox
    capability shape.

    ```python
    from inis import AsyncClient
    from pydantic_ai import Agent
    from inis_pydantic_ai.sandbox import InisSandbox

    client = AsyncClient()
    session = await client.sessions.create(egress_default="deny")

    agent = Agent(
        "anthropic:claude-sonnet-4-6",
        capabilities=[InisSandbox(session=session)],
    )
    try:
        result = await agent.run("Write a script that prints the first 10 primes and run it.")
    finally:
        await session.destroy()  # this app owns the session
    ```
    """

    session_id: str | None = None
    """Attach to an existing session by ID. Never destroyed by this capability."""

    session: AsyncSession | None = None
    """Use a session object the host already opened. Never destroyed by this
    capability — the host owns it outright, same as `session_id`'s attach
    contract, just without a second attach round-trip."""

    api_key: str | None = None
    """API key used only when attaching via `session_id`; defaults to
    $INIS_API_KEY. Not used with `session` — that object already carries its
    own credentials."""

    base_url: str | None = None
    """API base URL used only when attaching via `session_id`; defaults to
    $INIS_BASE_URL or https://api.inis.run."""

    default_command_timeout_ms: int = _DEFAULT_COMMAND_TIMEOUT_MS
    """Default timeout for one `run_command` call when the model omits one."""

    max_output_chars: int = _DEFAULT_MAX_OUTPUT_CHARS
    """Maximum characters retained per stdout/stderr stream or file read,
    applied independently to each so a large stderr cannot crowd stdout out
    of a shared budget."""

    instructions: str | None = None
    """Instructions describing the sandbox, added to the system prompt.
    Leave as None for the default; pass "" for none."""

    _attached_session: AsyncSession | None = field(
        default=None, init=False, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        if (self.session_id is None) == (self.session is None):
            raise ValueError(
                "InisSandbox requires exactly one of `session_id` (attach by ID) "
                "or `session` (an AsyncSession the host already opened)."
            )
        if self.session is not None and self.api_key is not None:
            raise ValueError(
                "api_key only applies when attaching via session_id; `session` "
                "already carries its own credentials."
            )

    def get_instructions(self) -> str | None:
        if self.instructions is not None:
            return self.instructions or None
        return _INSTRUCTIONS

    def _resolve_session(self) -> AsyncSession:
        if self.session is not None:
            return self.session
        if self._attached_session is None:
            self._attached_session = AsyncSession.attach(
                self.session_id, base_url=self.base_url, token=self.api_key
            )
        return self._attached_session

    async def aclose(self) -> None:
        """Close the local HTTP transport opened for `session_id` attach mode.

        A no-op when constructed with `session` — the host opened that
        object and owns closing it. Never destroys the session either way;
        call `session.destroy()` yourself if your app also owns that.
        """
        if self._attached_session is not None:
            await self._attached_session.aclose()
            self._attached_session = None

    def get_toolset(self) -> AgentToolset[AgentDepsT]:
        return InisSandboxToolset[AgentDepsT](
            session=self._resolve_session(),
            default_command_timeout_ms=self.default_command_timeout_ms,
            max_output_chars=self.max_output_chars,
        )


class InisSandboxToolset(FunctionToolset[AgentDepsT]):
    """The tool surface behind `InisSandbox`. Binds directly to a session the
    capability resolved (attached or injected) — this toolset never opens or
    closes that session; see `InisSandbox.aclose()`."""

    def __init__(
        self,
        *,
        session: AsyncSession,
        default_command_timeout_ms: int,
        max_output_chars: int,
    ) -> None:
        super().__init__()
        self._session = session
        self._default_timeout_ms = default_command_timeout_ms
        self._max_chars = max_output_chars
        self.add_function(self.run_command, name="run_command")
        self.add_function(self.read_file, name="read_file")
        self.add_function(self.write_file, name="write_file")
        self.add_function(self.list_directory, name="list_directory")

    def _truncate(self, text: str) -> str:
        if len(text) <= self._max_chars:
            return text
        return text[-self._max_chars :] + f"\n[truncated to last {self._max_chars} chars]"

    async def run_command(self, command: str, *, timeout_ms: int | None = None) -> str:
        """Run a shell command in the sandbox and return its output.

        Runs through `bash -lc`, so pipes, redirection, `&&`, and globs work.
        A non-zero exit is reported in the returned text, not raised, so you
        can read it and react.

        Args:
            command: The shell command to run.
            timeout_ms: Maximum milliseconds to wait (default: the configured timeout).
        """
        try:
            result = await self._session.exec(
                ["bash", "-lc", command],
                timeout_ms=timeout_ms or self._default_timeout_ms,
            )
        except InisError as e:
            if e.retryable:
                raise ModelRetry(str(e)) from e
            raise
        parts: list[str] = []
        if result.stdout:
            parts.append(f"[stdout]\n{self._truncate(result.stdout)}")
        if result.stderr:
            parts.append(f"[stderr]\n{self._truncate(result.stderr)}")
        output = "\n".join(parts) if parts else "(no output)"
        if result.timed_out:
            return f"{output}\n[timed out]"
        if result.exit_code:
            return f"{output}\n[exit code: {result.exit_code}]"
        return output

    async def read_file(self, path: str) -> str:
        """Read a text file from the sandbox (defaults resolve under /workspace).

        Args:
            path: Path to the file inside the sandbox.
        """
        try:
            data = await self._session.read_file(path)
        except InisError as e:
            if e.retryable:
                raise ModelRetry(f"could not read {path!r}: {e}") from e
            raise
        text = data if isinstance(data, str) else data.decode("utf-8", errors="replace")
        return self._truncate(text)

    async def write_file(self, path: str, content: str) -> str:
        """Write text to a file in the sandbox.

        Args:
            path: Path to the file inside the sandbox.
            content: The text to write.
        """
        try:
            await self._session.write_file(path, content)
        except InisError as e:
            if e.retryable:
                raise ModelRetry(f"could not write {path!r}: {e}") from e
            raise
        return f"wrote {len(content)} bytes to {path}"

    async def list_directory(self, path: str = "/workspace") -> str:
        """List the entries in a sandbox directory (defaults to /workspace).

        Args:
            path: Directory to list inside the sandbox.
        """
        try:
            entries = await self._session.list_files(path)
        except InisError as e:
            if e.retryable:
                raise ModelRetry(f"could not list {path!r}: {e}") from e
            raise
        return "\n".join(entries) if entries else "(empty)"
