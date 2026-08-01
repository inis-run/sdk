"""Native inis.run sandbox provider for Strands Agents (`strands.sandbox`).

Verified against `strands-agents==1.50.2` installed source, not docs or blog
posts. Two things worth recording explicitly because they were not obvious
going in:

1. Unlike the OpenAI Agents SDK's beta, internal-ABC `agents.sandbox`
   surface (see `inis_openai_agents.sandbox`'s module docstring for that
   story), Strands' `strands.sandbox` is a **stable, public, documented
   extension point shipped in the core package**: `strands.sandbox.Sandbox`
   is the abstract base, `strands.sandbox.PosixShellSandbox` is a concrete
   abstract subclass with default shell-based file/code operations, and
   `strands.sandbox.docker.DockerSandbox` / `strands.sandbox.ssh.SshSandbox`
   are real first-party backends built the exact same way this module is:
   subclass `PosixShellSandbox`, implement `execute_streaming`, override
   `get_tools()` to vend `sandbox_bash`/`sandbox_file_editor` bound to the
   sandbox instance. `PosixShellSandbox`'s own docstring explicitly invites
   this: "Subclasses may override any method with a native implementation
   for better performance ... or native API calls for cloud backends" —
   this module does exactly that for `read_file`/`write_file`/`remove_file`/
   `list_files`, using inis.run's native binary-safe `/files` endpoints
   instead of the default's `base64`-over-shell fallback.

2. `strands.sandbox`'s public HTTP surface has no per-call `env` parameter
   at all: `internal/protocol.ExecRequest` (the wire contract; verified by
   reading the Go source, not just the Python/TS client wrappers) carries
   only `command`/`cwd`/`timeout_ms`. inis.run's own `env` is fixed at
   *session creation* and applied to every exec by the guest agent
   automatically — there is no way to override it for one call through the
   public API. This module closes that gap the same way `SshSandbox` closes
   the identical gap for OpenSSH (which also has no native per-call env):
   a validated `export KEY=VALUE && ` shell prefix ahead of the command,
   since every command already runs through `bash -lc` on the guest. This
   is a real, working per-call override, not a fake — it just uses the
   shell it already has to reach it.

Two ways to put an inis.run session behind a Strands `Agent`
-------------------------------------------------------------------------

**1. Attached — one inis.run session for the whole conversation
(recommended).** Create or attach to a session yourself and wrap it. This
adapter never creates or destroys the underlying session:

    from inis import AsyncClient
    from strands import Agent
    from inis_strands_agents import InisSandbox

    client = AsyncClient()
    session = await client.sessions.create(egress_default="deny")
    sandbox = InisSandbox.wrap(session)

    agent = Agent(model="...", sandbox=sandbox)
    await agent.invoke_async("list files in /workspace")
    await agent.invoke_async("now write hello.txt")
    await session.destroy()  # this app owns the session; the adapter never will

**2. Owned — the adapter creates and destroys the session for you**, for a
single bounded task:

    from inis_strands_agents import InisSandbox

    async with await InisSandbox.create(egress_default="deny") as sandbox:
        agent = Agent(model="...", sandbox=sandbox)
        await agent.invoke_async("...")
    # session destroyed on exit

Create/destroy, egress, pause/resume, and fork stay host-controlled either
way: nothing here vends a tool that can create, destroy, or reconfigure a
session — only `sandbox_bash`/`sandbox_file_editor`, matching every other
`strands.sandbox` backend (Docker/SSH never let the model create or destroy
the container/host either).

Explicitly not implemented (declared, not faked)
-------------------------------------------------------------------------
- **Genuine mid-command cancellation of an already-dispatched exec.**
  Cancelling the `asyncio` task consuming `execute_streaming` (the
  documented Strands cancellation model — see `strands.sandbox.base`'s
  module docstring) closes inis.run's underlying SSE connection, which the
  server takes as a disconnect and kills the remote command (this is
  `AsyncSession.exec_stream`'s own documented behavior, not something this
  adapter adds) — so cancellation genuinely stops the remote process, not
  just the local iterator. What is *not* available is cancelling one
  command while leaving the sandbox otherwise idle-ready faster than a
  fresh `execute_streaming` call would be; there is no separate
  cancel-in-place primitive to call.
- **PTY.** inis.run's PTY primitive (`AsyncSession.pty`) is a separate
  product surface not wired through `strands.sandbox`'s shell/file
  contract, which has no PTY concept to fill.
- **`OutputFile` artifacts.** `ExecutionResult.output_files` is always `[]`
  here, same as every other shell-based `Sandbox` (Docker/SSH) — inis.run's
  shell exec has no channel for the sandbox to declare "this run produced
  these binary artifacts" distinct from stdout/stderr; a caller wanting a
  produced file reads it with `read_file`/`read_text`.
"""

from __future__ import annotations

import re
import shlex
from collections.abc import AsyncGenerator
from typing import Any

from strands.sandbox import PosixShellSandbox
from strands.sandbox.errors import SandboxPathNotFoundError, SandboxTimeoutError
from strands.sandbox.types import ExecutionResult, FileInfo, StreamChunk
from strands.types.tools import AgentTool
from strands.vended_tools import make_bash, make_file_editor
from strands.vended_tools.bash.types import SANDBOX_BASH_DESCRIPTION
from strands.vended_tools.file_editor.file_editor import DEFAULT_FILE_EDITOR_DESCRIPTION

from inis import AsyncClient, AsyncSession, InisError

DEFAULT_WORKSPACE_ROOT = "/workspace"

# Same POSIX env-var name rule `strands.sandbox.constants.ENV_KEY_PATTERN`
# validates against, kept local rather than importing that non-exported
# module attribute (`strands.sandbox.posix_shell`/`.constants` are not part
# of `strands.sandbox.__all__`, unlike `Sandbox`/`PosixShellSandbox`
# themselves) so this adapter does not depend on an internal symbol that
# could move without notice.
_ENV_KEY_PATTERN = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")

# Guest-side error text for a missing path, read directly from
# `guest/agent/files.go` (`openBeneathWorkspace`/`os.Lstat` surface Go's own
# `os.ErrNotExist`/`syscall.ENOENT` string verbatim, and the API layer
# forwards it unmodified as the `error` field with a generic
# `code="bad_request"` -- inis.run's file-op HTTP responses do not carry a
# distinct machine-readable "not found" code today, so the message text is
# the only reliable signal). Matched case-insensitively against whatever
# `InisError` surfaces.
_NOT_FOUND_MARKERS = ("no such file or directory", "not a directory")


def _validate_env_keys(env: dict[str, str]) -> None:
    for key in env:
        if not _ENV_KEY_PATTERN.fullmatch(key):
            raise ValueError(f"Invalid environment variable name: {key}")


def _build_env_prefix(env: dict[str, str] | None) -> str:
    """Build a shell `export KEY=VALUE && ...` prefix, or `""` when empty.

    inis.run's exec API has no per-call `env` field (see module docstring,
    point 2) -- every command already runs as `bash -lc <command>` on the
    guest, so exporting into that same shell invocation genuinely sets the
    variables for this one command without touching the session's own
    persistent env.
    """
    if not env:
        return ""
    _validate_env_keys(env)
    assignments = " ".join(f"{key}={shlex.quote(value)}" for key, value in env.items())
    return f"export {assignments} && "


def _looks_like_missing_path(e: InisError) -> bool:
    text = str(e).lower()
    return any(marker in text for marker in _NOT_FOUND_MARKERS)


class InisSandbox(PosixShellSandbox):
    """`strands.sandbox.PosixShellSandbox` backed by one `inis.AsyncSession`.

    Construct via `InisSandbox.wrap()` (attach mode) or `InisSandbox.create()`
    (owned mode) -- not directly. `execute_streaming` is the only shell
    primitive implemented from scratch (via `AsyncSession.exec_stream`);
    `execute_code_streaming` is inherited from `PosixShellSandbox` unchanged
    (it pipes base64-encoded source through `execute_streaming`, so it gets
    this sandbox's env/timeout/cwd handling for free). `read_file`/
    `write_file`/`remove_file`/`list_files` are overridden to use
    inis.run's native binary-safe `/files` endpoints instead of the
    default's `base64`-over-shell fallback.
    """

    def __init__(
        self,
        *,
        session: AsyncSession,
        owned: bool,
        workspace_root: str = DEFAULT_WORKSPACE_ROOT,
        owned_client: AsyncClient | None = None,
    ) -> None:
        self._session = session
        self._owned = owned
        self.workspace_root = workspace_root
        # Only set for create()'s owned mode, and only when create() also
        # created the AsyncClient itself -- see aclose().
        self._owned_client = owned_client

    @classmethod
    def wrap(cls, session: AsyncSession, *, workspace_root: str = DEFAULT_WORKSPACE_ROOT) -> InisSandbox:
        """Wrap an inis.run session the host already created or attached to.

        This wrapper never creates or destroys the underlying session --
        call `await session.destroy()` (the `AsyncSession`, not this
        wrapper) yourself when the conversation is over. `aclose()` on a
        wrapped sandbox is a no-op.
        """
        if not session.session_id:
            raise ValueError("InisSandbox.wrap() requires an already-created/attached session (session_id is unset)")
        return cls(session=session, owned=False, workspace_root=workspace_root)

    @classmethod
    async def create(
        cls,
        client: AsyncClient | None = None,
        *,
        workspace_root: str = DEFAULT_WORKSPACE_ROOT,
        **session_kwargs: Any,
    ) -> InisSandbox:
        """Create a new inis.run session and own its lifecycle.

        `session_kwargs` is forwarded to `AsyncClient.sessions.create(...)`
        (`template`, `size`, `egress_default`, `egress_allow`, `name`,
        `labels`, `idle_timeout_ms`, `max_lifetime_ms`, ... -- see the core
        `inis` SDK for the full set). `client` defaults to a fresh
        `AsyncClient()` (reading `INIS_API_KEY`/`INIS_BASE_URL`); this
        adapter closes it in `aclose()` only if it created it itself.

        `aclose()` (or the `async with` context manager) destroys the
        session -- call it when the conversation is over.
        """
        owns_client = client is None
        client = client or AsyncClient()
        try:
            session = await client.sessions.create(**session_kwargs)
        except Exception:
            if owns_client:
                await client.close()
            raise
        return cls(
            session=session,
            owned=True,
            workspace_root=workspace_root,
            owned_client=client if owns_client else None,
        )

    async def aclose(self) -> None:
        """Destroy the session (owned mode only) and close any client this
        adapter created for itself. A no-op for a wrapped (attached) sandbox."""
        if not self._owned:
            return
        try:
            await self._session.destroy()
        except InisError:
            pass  # best-effort: already gone is fine
        if self._owned_client is not None:
            await self._owned_client.close()

    async def __aenter__(self) -> InisSandbox:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    # ── Command execution ───────────────────────────────────────────────────

    async def execute_streaming(
        self,
        command: str,
        *,
        timeout: float | None = None,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        **kwargs: Any,
    ) -> AsyncGenerator[StreamChunk | ExecutionResult, None]:
        prefixed = _build_env_prefix(env) + command
        timeout_ms = int(timeout * 1000) if timeout else None
        out_parts: list[str] = []
        err_parts: list[str] = []

        async for event in self._session.exec_stream(prefixed, cwd=cwd, timeout_ms=timeout_ms):
            if event.stream == "stdout":
                out_parts.append(event.data)
                yield StreamChunk(data=event.data, stream_type="stdout")
            elif event.stream == "stderr":
                err_parts.append(event.data)
                yield StreamChunk(data=event.data, stream_type="stderr")
            elif event.stream == "exit":
                if event.timed_out:
                    raise SandboxTimeoutError(timeout)
                yield ExecutionResult(
                    exit_code=event.exit_code or 0,
                    stdout="".join(out_parts),
                    stderr="".join(err_parts),
                )
                return
            elif event.stream == "error":
                # A guest-side failure distinct from a nonzero exit (e.g. the
                # guest agent itself became unreachable mid-command) -- raise
                # rather than yield a result that would misrepresent this as
                # "the command ran and produced no output."
                raise RuntimeError(f"inis.run guest error during execution: {event.data}")

    # ── Files ────────────────────────────────────────────────────────────────

    async def read_file(self, path: str, **kwargs: Any) -> bytes:
        try:
            data = await self._session.read_file(path, encoding="base64")
        except InisError as e:
            # Matches PosixShellSandbox's own default read_file, which raises
            # FileNotFoundError for any nonzero exit code from its `base64 <
            # path` shell command -- not just a genuinely missing file. See
            # Sandbox.read_file's docstring: "FileNotFoundError: If the file
            # does not exist or cannot be read."
            raise FileNotFoundError(str(e)) from e
        return data if isinstance(data, bytes) else data.encode("utf-8")

    async def write_file(self, path: str, content: bytes, **kwargs: Any) -> None:
        try:
            # AsyncSession.write_file auto-switches to base64 transport for a
            # `bytes` payload; the guest creates parent directories itself
            # (`secureMkdirAll`), matching write_file's documented contract.
            await self._session.write_file(path, content)
        except InisError as e:
            raise OSError(str(e)) from e

    async def remove_file(self, path: str, **kwargs: Any) -> None:
        try:
            await self._session.remove(path)
        except InisError as e:
            # Matches PosixShellSandbox's own default remove_file, which
            # raises FileNotFoundError for any nonzero exit from `rm`, not
            # just a genuinely missing file -- see Sandbox.remove_file's
            # docstring.
            raise FileNotFoundError(str(e)) from e

    async def list_files(self, path: str, **kwargs: Any) -> list[FileInfo]:
        try:
            entries = await self._session.list_files(path)
        except InisError as e:
            if _looks_like_missing_path(e):
                raise SandboxPathNotFoundError(path) from e
            raise OSError(str(e)) from e
        # guest/agent/files.go's listDirOp appends a trailing "/" to every
        # directory entry name -- verified in the guest source, not guessed.
        # `size` stays None: this listing op does not report it.
        result: list[FileInfo] = []
        for entry in entries:
            is_dir = entry.endswith("/")
            result.append(FileInfo(name=entry[:-1] if is_dir else entry, is_dir=is_dir))
        return result

    # ── Tool vending ─────────────────────────────────────────────────────────

    def get_tools(self) -> list[AgentTool]:
        """Vend the standard `sandbox_bash`/`sandbox_file_editor` tools bound
        to this session -- the same pattern `DockerSandbox`/`SshSandbox` use.
        No separate create/destroy/pause/resume/fork tool is vended: those
        stay host-controlled (see module docstring)."""
        session_desc = f'inis.run session "{self._session.session_id}"'
        return [
            make_file_editor(
                sandbox=self,
                name="sandbox_file_editor",
                description=f"{DEFAULT_FILE_EDITOR_DESCRIPTION} Files are in {session_desc}.",
            ),
            make_bash(
                sandbox=self,
                name="sandbox_bash",
                description=(
                    f"{SANDBOX_BASH_DESCRIPTION} Runs in {session_desc}. "
                    f"Working directory: {self.workspace_root}."
                ),
            ),
        ]
