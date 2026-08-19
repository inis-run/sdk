"""Native inis.run sandbox provider for the OpenAI Agents SDK `SandboxAgent`
path (`agents.sandbox`, package `openai-agents`).

Verified against `openai-agents==0.19.1` source, not blog posts or the
issue's original assumption. Two things worth recording explicitly because
they were not obvious going in:

1. `agents.sandbox` is documented upstream as **beta**, verbatim:
   "Sandbox agents are in beta. Expect details of the API, defaults, and
   supported capabilities to change before general availability, and expect
   more advanced features over time."
   (https://openai.github.io/openai-agents-python/sandbox/clients/)

2. `BaseSandboxSession`/`BaseSandboxClient` (in
   `agents.sandbox.session.{base_sandbox_session,sandbox_client}`) are NOT a
   documented third-party extension point — there is no upstream guide for
   adding a new backend. Every existing implementation (E2B, Modal, Daytona,
   Blaxel, Runloop, Cloudflare, Vercel) lives in-tree in `openai-agents`
   itself, and the ABC surface they implement is a heavyweight internal
   contract: portable-tar/snapshot fingerprinting, a workspace manifest
   materialization system, symlink-safe remote path validation, PTY
   sessions, Docker-volume-mount protocol, and OS account provisioning.
   This module was written by reading that machinery directly (chiefly
   `agents/extensions/sandbox/e2b/sandbox.py` as a worked reference) rather
   than from any public "how to add a provider" doc, because none exists.
   Only the parts inis.run actually has a primitive for are implemented for
   real; everything else falls through to the ABC's own honest defaults
   (`supports_pty()` -> False, `supports_docker_volume_mounts()` -> False,
   etc.) rather than being faked. See the module docstring section below,
   "Explicitly not implemented".

Two ways to put an inis.run session behind a `SandboxAgent`
-------------------------------------------------------------------------

**1. Attached — one inis.run session for the whole conversation (recommended).**

Create or attach to a session yourself and pass it straight through
`RunConfig(sandbox=SandboxRunConfig(session=...))` for every `Runner.run()`
call in the conversation:

    from inis import AsyncClient
    from agents import Runner, RunConfig
    from agents.run_config import SandboxRunConfig
    from agents.sandbox import SandboxAgent
    from inis_openai_agents import InisSandboxSession

    client = AsyncClient()
    inis_session = await client.sessions.create(egress_default="deny")
    sandbox_session = InisSandboxSession.wrap(inis_session)

    agent = SandboxAgent(name="assistant", model="gpt-5.1")
    run_config = RunConfig(sandbox=SandboxRunConfig(session=sandbox_session))
    try:
        result = await Runner.run(agent, "list files in /workspace", run_config=run_config)
        result2 = await Runner.run(agent, "now write hello.txt", run_config=run_config)
    finally:
        await inis_session.destroy()  # this app owns the session; the SDK never will

Read `agents/sandbox/runtime_session_manager.py::_create_resources` yourself
if you want to verify this: when `RunConfig.sandbox.session` is set, the
runtime marks `owns_session=False` and its cleanup path skips
`stop()`/`shutdown()`/`client.delete()` entirely. That is the SDK's own
built-in equivalent of the attach/never-destroy contract `inis_pydantic_ai`
and `@inis-run/ai-sdk` implement by hand, and it is the pattern that matches
inis.run's actual product: one persistent session across a whole
conversation, not one sandbox per model turn.

**2. Owned, one `Runner.run()` call at a time**, via `InisSandboxClient`:

    from agents.run_config import SandboxRunConfig
    from inis_openai_agents import InisSandboxClient, InisSandboxClientOptions

    run_config = RunConfig(
        sandbox=SandboxRunConfig(
            client=InisSandboxClient(),
            options=InisSandboxClientOptions(egress_default="deny"),
        )
    )
    result = await Runner.run(agent, "...", run_config=run_config)
    # result._sandbox_resume_state: pass this back in as
    # RunConfig(sandbox=SandboxRunConfig(session_state=...)) to continue in a
    # LATER process/turn — the SDK calls InisSandboxClient.resume(), which
    # creates a *new* inis.run session and restores the workspace from the
    # tar snapshot this adapter persisted before the previous one was
    # destroyed. It does not reattach to the original inis.run session id.

Be clear about what path 2 actually is: the upstream SDK destroys an owned
session (`client.delete()`) at the end of every single `Runner.run()` call,
unconditionally, whether or not you intend to continue the conversation. To
"resume" a later turn it round-trips a tar archive through
`persist_workspace()`/`hydrate_workspace()`, not inis.run's own persistent
session. That's the SDK's generic snapshot contract, applied honestly here
— it is not how inis.run's real pause/resume primitive works, and path 1 is
the better fit for a genuine multi-turn agent. Use path 2 for a single
bounded sandboxed task, or when you must pass through the upstream
`create`/`resume`/`delete` lifecycle exactly (e.g. for conformance testing).

PTY (`exec_command`'s `tty=True`, `write_stdin`)
-------------------------------------------------------------------------
`supports_pty()` returns `True`. `pty_exec_start()` opens a real interactive
terminal via the `inis` SDK's `AsyncSession.pty()` and `pty_write_stdin()`
writes to it; both return output collected over the caller's `yield_time_s`
window, same as every other backend's PTY tool contract. A live terminal
stays open (and holds a slot in the sandbox's PTY table) until the command
exits, `write_stdin` sees it exit, or `pty_terminate_all()` runs (the base
class already calls this on session shutdown) — closing it drops the
underlying connection, which the guest treats as a hangup and ends the
foreground process, the same disconnect-triggered cleanup inis.run's other
interactive transports rely on.

Explicitly not implemented (declared, not faked)
-------------------------------------------------------------------------
- **Docker-volume-mount manifests** (`supports_docker_volume_mounts()`
  stays `False`) and any `Mount` entry whose strategy needs more than plain
  file/directory materialization. Plain `Dir`/`LocalFile` manifest entries
  work (they resolve to ordinary `write`/`mkdir` calls).
- **True remote cancellation of an in-flight `exec()`.** Not for want of a
  cancellation primitive — inis.run has a real one (`SIGTERM`→`SIGKILL`) for
  named background processes. The gap is contract shape: the sandbox ABC's
  `exec()` is a timeout-only call with no abort handle for this adapter to
  route through, the same shape gap the Pydantic AI and Vercel AI SDK
  adapters have. A `timeout_ms` still ends the call.
- **Exposed ports** ARE implemented for real via `session.expose()` — this
  is a genuine inis.run <-> OpenAI-contract match, not a gap.
"""

from __future__ import annotations

import asyncio
import base64
import io
import shlex
import time
from collections import deque
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from agents.sandbox.errors import (
    ExecTimeoutError,
    ExecTransportError,
    ExposedPortUnavailableError,
    WorkspaceArchiveReadError,
    WorkspaceArchiveWriteError,
    WorkspaceReadNotFoundError,
    WorkspaceWriteTypeError,
)
from agents.sandbox.manifest import Manifest
from agents.sandbox.session import (
    BaseSandboxClient,
    BaseSandboxClientOptions,
    BaseSandboxSession,
    SandboxSession,
    SandboxSessionState,
)
# `pty_output`/`pty_types` are internal submodules of `agents.sandbox.session`
# (not re-exported from the package's own `__init__`), but they're the exact
# output-collection/yield-time/pruning primitives every in-tree PTY backend
# (unix_local.py, docker.py) builds on — importing them directly, the same
# way those in-tree modules do, keeps this adapter's PTY semantics identical
# to theirs instead of reinventing a slightly different one.
from agents.sandbox.session.pty_output import collect_pty_output
from agents.sandbox.session.pty_types import (
    PTY_PROCESSES_MAX,
    PtyExecUpdate,
    clamp_pty_yield_time_ms,
    process_id_to_prune_from_meta,
    resolve_pty_write_yield_time_ms,
)
from agents.sandbox.snapshot import SnapshotBase, SnapshotSpec, resolve_snapshot
from agents.sandbox.types import ExecResult as OAIExecResult
from agents.sandbox.types import ExposedPortEndpoint, User

from inis import AsyncClient, AsyncPTY, AsyncSession, ConnectionSpec, InisError

# Marker prefix so a tar-shaped persisted workspace is unambiguous even if an
# empty/noop snapshot payload round-trips through `bytes` somewhere.
_TAR_MAGIC = b"INIS_SANDBOX_TAR_V1\n"

# `pty_exec_start()`'s own process ids are just distinct ints scoped to one
# session's PTY table (the ABC has no format requirement beyond that) --
# unlike the in-tree providers we don't need process ids that double as real
# OS pids, so a plain counter is enough.
_PTY_PROCESS_ID_START = 1000


@dataclass
class _InisPtyProcessEntry:
    """One open `AsyncPTY` tracked by `pty_exec_start`/`pty_write_stdin`,
    plus the output buffer `collect_pty_output` (imported from the upstream
    package's own `pty_output` module) drains from."""

    pty: AsyncPTY
    last_used: float = field(default_factory=time.monotonic)
    output_chunks: deque[bytes] = field(default_factory=deque)
    output_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    output_notify: asyncio.Event = field(default_factory=asyncio.Event)
    output_closed: asyncio.Event = field(default_factory=asyncio.Event)
    pump_task: "asyncio.Task[None] | None" = None


def _decode_inis_error(e: InisError, *, command: tuple[str, ...]) -> Exception:
    """Map a structured `InisError` (code/status/retryable/request_id) onto
    the closest `ExecFailureError` the OpenAI contract defines, instead of
    flattening it to a message."""
    return ExecTransportError(
        command=command,
        context={
            "inis_error_code": e.code,
            "inis_status": e.status,
            "inis_request_id": e.request_id,
        },
        cause=e,
        retryable=bool(e.retryable),
    )


class InisExecResult(OAIExecResult):
    """`OAIExecResult` (`agents.sandbox.types.ExecResult`) plus inis.run's
    own `truncated` signal, which the upstream type has no slot for.

    Returned in place of a plain `OAIExecResult` by `InisSandboxSession.exec`
    -- `OAIExecResult` is not `@dataclass(frozen=True)` and declares no
    `__slots__`, so this subclass is a strict `isinstance`-compatible
    superset: any caller typed against `OAIExecResult` keeps working
    unchanged, and one that wants the extra signal can read `.truncated`
    directly (or `getattr(result, "truncated", False)` if it can't assume
    this package's type is in play).

    `truncated` here is *never* derived from `stdout`/`stderr` content. It
    is copied straight from `inis.AsyncSession.exec()`'s own
    `ExecResult.truncated`, which inis.run's host sets from its own
    byte-accounting when it caps a stream, and which travels to this
    adapter as a separate field on the session-API response -- a channel
    the guest process cannot write into no matter what it prints. See
    an earlier version of this adapter appended a marker string
    (with a random nonce) into `stderr` itself so truncation was at least
    legible to something reading raw stderr, but nothing ever checked that
    nonce against anything, so a guest printing its own marker-shaped text
    was exactly as convincing as a real one. Not shipping anything
    marker-shaped in `stderr` at all closes that off completely: there is
    no longer a string for a guest to imitate, because nothing parses
    stderr for this signal any more.
    """

    truncated: bool

    def __init__(self, *, stdout: bytes, stderr: bytes, exit_code: int, truncated: bool) -> None:
        super().__init__(stdout=stdout, stderr=stderr, exit_code=exit_code)
        self.truncated = truncated


class InisSandboxClientOptions(BaseSandboxClientOptions):
    """Options for `InisSandboxClient.create()` — maps directly onto
    `inis.AsyncClient.sessions.create(...)` kwargs. See the `inis` core SDK
    for the full meaning of each field."""

    type: Literal["inis"] = "inis"

    template: str | None = None
    size: str | None = None
    egress_default: Literal["allow", "deny"] | None = None
    egress_allow: tuple[str, ...] | None = None
    name: str | None = None
    labels: dict[str, str] | None = None
    idle_timeout_ms: int | None = None
    max_lifetime_ms: int | None = None
    api_key: str | None = None
    """Defaults to $INIS_API_KEY. Never pass a key sourced from model output
    or any value visible to SandboxAgent tool calls — this stays host-side."""

    base_url: str | None = None
    """Defaults to $INIS_BASE_URL or https://api.inis.run."""

    # Appended last on purpose: the upstream BaseSandboxClientOptions maps
    # positional constructor args onto fields in declaration order, so
    # inserting a field mid-class would silently rebind api_key/base_url for
    # anyone constructing this positionally.
    connections: tuple[ConnectionSpec | dict[str, object], ...] | None = None
    """Bind this session to one or more external API origins with a
    credential the platform injects only at request time. See
    `inis.ConnectionSpec` in the core SDK for the full field set."""


class InisSandboxSessionState(SandboxSessionState):
    """Serializable state for an owned `InisSandboxSession` (the `create()`/
    `resume()` path). Deliberately does not carry `api_key` — secrets never
    round-trip through `serialize_session_state()`/host storage."""

    type: Literal["inis"] = "inis"

    inis_session_id: str
    """The inis.run session id this state was captured from. Informational
    only for `resume()`: the SDK destroys owned sessions at the end of every
    `Runner.run()` (see module docstring), so by the time `resume()` runs
    this id is very likely already gone — `resume()` creates a *new* inis.run
    session and restores the workspace from the persisted snapshot instead of
    trying to reattach to it. Kept for debugging/telemetry, not reuse."""

    base_url: str | None = None


def _running_states(info_state: str) -> bool:
    # inis session states: "creating", "live", "paused", "failed" (see
    # sdk/python/tests/test_async_session.py). "paused" still counts as
    # running/available here: inis.run's own exec() transparently resumes a
    # paused session server-side (the documented resume-or-auto-resume
    # path), so callers of this contract's `running()` should not treat
    # "paused" as dead.
    return info_state in ("live", "paused")


class InisSandboxSession(BaseSandboxSession):
    """`BaseSandboxSession` backed by one `inis.AsyncSession`.

    Do not construct directly — use `InisSandboxSession.wrap()` (attach
    mode, path 1 in the module docstring) or go through
    `InisSandboxClient.create()`/`.resume()` (owned mode, path 2).
    """

    state: InisSandboxSessionState

    def __init__(self, *, state: InisSandboxSessionState, session: AsyncSession) -> None:
        self.state = state
        self._session = session
        self._pty_lock = asyncio.Lock()
        self._pty_processes: dict[int, _InisPtyProcessEntry] = {}
        self._next_pty_process_id = _PTY_PROCESS_ID_START

    @classmethod
    def wrap(
        cls,
        session: AsyncSession,
        *,
        manifest: Manifest | None = None,
    ) -> InisSandboxSession:
        """Wrap an inis.run session the host already created or attached to
        (via `client.sessions.create(...)`, `client.sessions.attach(...)`,
        or `AsyncSession.attach(...)`).

        Pass the result via `RunConfig(sandbox=SandboxRunConfig(session=...))`.
        This wrapper never creates or destroys the underlying session — call
        `await session.destroy()` (the `AsyncSession`, not this wrapper)
        yourself when the conversation is over.
        """
        if not session.session_id:
            raise ValueError(
                "InisSandboxSession.wrap() requires an already-created/attached "
                "session (session.session_id is not set)"
            )
        state = InisSandboxSessionState(
            snapshot=resolve_snapshot(None, session.session_id),
            manifest=manifest or Manifest(),
            inis_session_id=session.session_id,
            workspace_root_ready=True,
        )
        wrapped = cls(state=state, session=session)
        # Tell the base class this backend is already live with its
        # workspace in place: skip manifest replay / snapshot restore for a
        # session that was already running before we ever saw it.
        wrapped._set_start_state_preserved(True)
        return wrapped

    # -- exec ---------------------------------------------------------------

    async def _exec_internal(
        self,
        *command: str | Path,
        timeout: float | None = None,
    ) -> InisExecResult:
        cmd = [str(c) for c in command]
        try:
            result = await self._session.exec(
                cmd,
                timeout_ms=int(timeout * 1000) if timeout else None,
            )
        except InisError as e:
            raise _decode_inis_error(e, command=tuple(cmd)) from e

        if result.timed_out:
            raise ExecTimeoutError(command=tuple(cmd), timeout_s=timeout)

        stdout = result.stdout.encode("utf-8", errors="replace")
        stderr = result.stderr.encode("utf-8", errors="replace")
        # `stderr`/`stdout` are returned byte-for-byte as inis.run produced
        # them -- no marker text is appended. See `InisExecResult` above for
        # where the truncation signal actually lives and why that is safe
        # even though the guest fully controls both streams' content.
        return InisExecResult(
            stdout=stdout, stderr=stderr, exit_code=result.exit_code, truncated=result.truncated
        )

    # -- pty ------------------------------------------------------------------

    def supports_pty(self) -> bool:
        return True

    async def pty_exec_start(
        self,
        *command: str | Path,
        timeout: float | None = None,
        shell: bool | list[str] = True,
        user: str | User | None = None,
        tty: bool = False,
        yield_time_s: float | None = None,
        max_output_tokens: int | None = None,
    ) -> PtyExecUpdate:
        # inis.run's PTY primitive is always a real interactive terminal
        # (`AsyncSession.pty()`) -- there's no separate "plain pipes" transport
        # to switch to, so `tty` selects nothing here. `timeout` isn't applied
        # client-side either: same choice the in-tree unix_local/docker
        # backends make, since a PTY session's lifetime is governed by
        # yield_time_s / write_stdin / pty_terminate_all, not a one-shot
        # deadline.
        _ = (timeout, tty)
        sanitized_command = self._prepare_exec_command(*command, shell=shell, user=user)
        try:
            pty = await self._session.pty(command=sanitized_command)
        except InisError as e:
            raise _decode_inis_error(e, command=tuple(sanitized_command)) from e

        entry = _InisPtyProcessEntry(pty=pty)
        entry.pump_task = asyncio.create_task(self._pump_pty_output(entry))

        pruned_entry: _InisPtyProcessEntry | None = None
        async with self._pty_lock:
            process_id = self._next_pty_process_id
            self._next_pty_process_id += 1
            pruned_entry = self._prune_pty_processes_if_needed()
            self._pty_processes[process_id] = entry

        if pruned_entry is not None:
            await self._terminate_pty_entry(pruned_entry)

        yield_time_ms = 10_000 if yield_time_s is None else int(yield_time_s * 1000)
        output, original_token_count = await collect_pty_output(
            output_chunks=entry.output_chunks,
            output_lock=entry.output_lock,
            output_notify=entry.output_notify,
            is_done=entry.output_closed.is_set,
            yield_time_ms=clamp_pty_yield_time_ms(yield_time_ms),
            max_output_tokens=max_output_tokens,
        )
        return await self._finalize_pty_update(
            process_id=process_id,
            entry=entry,
            output=output,
            original_token_count=original_token_count,
        )

    async def pty_write_stdin(
        self,
        *,
        session_id: int,
        chars: str,
        yield_time_s: float | None = None,
        max_output_tokens: int | None = None,
    ) -> PtyExecUpdate:
        async with self._pty_lock:
            entry = self._resolve_pty_session_entry(
                pty_processes=self._pty_processes, session_id=session_id
            )

        if chars:
            try:
                await entry.pty.write(chars)
            except InisError as e:
                raise _decode_inis_error(e, command=("<pty-stdin>",)) from e

        yield_time_ms = 250 if yield_time_s is None else int(yield_time_s * 1000)
        output, original_token_count = await collect_pty_output(
            output_chunks=entry.output_chunks,
            output_lock=entry.output_lock,
            output_notify=entry.output_notify,
            is_done=entry.output_closed.is_set,
            yield_time_ms=resolve_pty_write_yield_time_ms(
                yield_time_ms=yield_time_ms, input_empty=chars == ""
            ),
            max_output_tokens=max_output_tokens,
        )
        entry.last_used = time.monotonic()
        return await self._finalize_pty_update(
            process_id=session_id,
            entry=entry,
            output=output,
            original_token_count=original_token_count,
        )

    async def pty_terminate_all(self) -> None:
        async with self._pty_lock:
            entries = list(self._pty_processes.values())
            self._pty_processes.clear()
        for entry in entries:
            await self._terminate_pty_entry(entry)

    async def _pump_pty_output(self, entry: _InisPtyProcessEntry) -> None:
        try:
            async for chunk in entry.pty:
                async with entry.output_lock:
                    entry.output_chunks.append(chunk)
                entry.output_notify.set()
        except InisError as e:
            async with entry.output_lock:
                entry.output_chunks.append(
                    f"\n[inis: pty error: {e}]".encode("utf-8", errors="replace")
                )
        finally:
            entry.output_closed.set()
            entry.output_notify.set()

    async def _finalize_pty_update(
        self,
        *,
        process_id: int,
        entry: _InisPtyProcessEntry,
        output: bytes,
        original_token_count: int | None,
    ) -> PtyExecUpdate:
        exit_code = entry.pty.exit_code
        live_process_id: int | None = process_id
        if entry.output_closed.is_set():
            # The connection ended (a clean PTY_EXIT frame, a PTY_ERROR frame,
            # or the peer dropping it) -- either way this process id is no
            # longer usable for a later write_stdin, so evict it now rather
            # than waiting for the caller to notice.
            async with self._pty_lock:
                removed = self._pty_processes.pop(process_id, None)
            if removed is not None:
                await self._terminate_pty_entry(removed)
            live_process_id = None
        return PtyExecUpdate(
            process_id=live_process_id,
            output=output,
            exit_code=exit_code,
            original_token_count=original_token_count,
        )

    def _prune_pty_processes_if_needed(self) -> _InisPtyProcessEntry | None:
        if len(self._pty_processes) < PTY_PROCESSES_MAX:
            return None
        meta = [
            (process_id, entry.last_used, entry.pty.exit_code is not None)
            for process_id, entry in self._pty_processes.items()
        ]
        process_id = process_id_to_prune_from_meta(meta)
        if process_id is None:
            return None
        return self._pty_processes.pop(process_id, None)

    async def _terminate_pty_entry(self, entry: _InisPtyProcessEntry) -> None:
        # Closing the PTY is enough on its own: the guest treats a dropped
        # connection as a hangup and ends the foreground process for us (the
        # same disconnect-triggered cleanup inis.run's other interactive
        # transports rely on), so there's no separate "kill" step to send
        # first.
        with suppress(InisError, OSError):
            await entry.pty.close()
        if entry.pump_task is not None:
            with suppress(Exception):
                await asyncio.wait_for(entry.pump_task, timeout=5)

    # -- paths ----------------------------------------------------------------

    async def _validate_path_access(self, path: Path | str, *, for_write: bool = False) -> Path:
        # Use the ABC's own symlink-safe remote realpath check (implemented
        # generically on top of `exec()` in base_sandbox_session.py) instead
        # of the cheap local-only default — same choice every in-tree
        # provider makes.
        return await self._validate_remote_path_access(path, for_write=for_write)

    # -- files ----------------------------------------------------------------

    async def read(self, path: Path, *, user: User | str | None = None) -> io.IOBase:
        if user is not None:
            await self._check_read_with_exec(path, user=user)
        workspace_path = await self._validate_path_access(path)
        try:
            data = await self._session.read_file(str(workspace_path), encoding="base64")
        except InisError as e:
            if e.code in ("not_found", "file_not_found"):
                raise WorkspaceReadNotFoundError(path=path, cause=e) from e
            raise WorkspaceArchiveReadError(path=path, cause=e, retryable=e.retryable) from e
        if isinstance(data, str):
            data = data.encode("utf-8", errors="replace")
        return io.BytesIO(data)

    async def write(
        self,
        path: Path,
        data: io.IOBase,
        *,
        user: User | str | None = None,
    ) -> None:
        payload = data.read()
        if isinstance(payload, str):
            payload = payload.encode("utf-8")
        if not isinstance(payload, (bytes, bytearray)):
            raise WorkspaceWriteTypeError(path=path, actual_type=type(payload).__name__)
        if user is not None:
            await self._check_write_with_exec(path, user=user)
        workspace_path = await self._validate_path_access(path, for_write=True)
        try:
            # `write_file` auto-detects a `bytes` payload and switches to
            # base64 transport itself (see `inis.AsyncSession.write_file`);
            # no `encoding=` kwarg needed here.
            await self._session.write_file(str(workspace_path), bytes(payload))
        except InisError as e:
            raise WorkspaceArchiveWriteError(path=workspace_path, cause=e, retryable=e.retryable) from e

    async def running(self) -> bool:
        try:
            info = await self._session.get()
        except InisError:
            return False
        return _running_states(info.state)

    # -- exposed ports --------------------------------------------------------

    async def _resolve_exposed_port(self, port: int) -> ExposedPortEndpoint:
        try:
            preview = await self._session.expose(port)
        except InisError as e:
            raise ExposedPortUnavailableError(
                port=port,
                exposed_ports=self.state.exposed_ports,
                reason="backend_unavailable",
                context={"backend": "inis", "inis_error_code": e.code},
                cause=e,
            ) from e
        # preview.preview_url is an https URL like https://<slug>.preview.inis.run
        url = preview.preview_url
        host = url.split("://", 1)[-1].split("/", 1)[0]
        return ExposedPortEndpoint(host=host, port=443, tls=True)

    # -- snapshot persistence (tar over exec; see module docstring "path 2") --

    def _tar_workdir(self) -> Path:
        return self._workspace_root_path()

    async def persist_workspace(self) -> io.IOBase:
        # Always produce the real tar bytes here, regardless of
        # `state.snapshot`'s type — `snapshot_lifecycle.persist_snapshot()`
        # (and any direct caller, e.g. a manual `resume()` hand-off) calls
        # this unconditionally and decides what to do with the result
        # itself; a `NoopSnapshot` already discards it for free on its own
        # `persist()`, so short-circuiting here would only be a shortcut
        # for the one caller that doesn't want it.
        root = self._tar_workdir()
        skip = self._persist_workspace_skip_relpaths()
        exclude_args = " ".join(f"--exclude={shlex.quote(p.as_posix())}" for p in skip)
        cmd = f"tar {exclude_args} -C {shlex.quote(str(root))} -cf - . | base64 -w0"
        try:
            result = await self._session.exec(["bash", "-lc", cmd])
        except InisError as e:
            raise WorkspaceArchiveReadError(path=root, cause=e, retryable=e.retryable) from e
        if result.exit_code != 0:
            raise WorkspaceArchiveReadError(
                path=root,
                context={"exit_code": result.exit_code, "stderr": result.stderr},
            )
        raw = base64.b64decode(result.stdout.encode("utf-8"))
        return io.BytesIO(_TAR_MAGIC + raw)

    async def hydrate_workspace(self, data: io.IOBase) -> None:
        raw = data.read()
        if isinstance(raw, str):
            raw = raw.encode("utf-8")
        if not raw:
            return
        if raw.startswith(_TAR_MAGIC):
            raw = raw[len(_TAR_MAGIC) :]
        root = self._tar_workdir()
        # Must live under /workspace, not /tmp: confirmed live against
        # api.inis.run that the real `write_file` endpoint (unlike a bare
        # exec()) enforces "path must be under /workspace" server-side. The
        # local test fake didn't reproduce that restriction, so this shipped
        # broken against the real API despite passing every unit test --
        # caught only by an actual live run. Cleaned up in `finally` below
        # regardless.
        tar_path = f"{str(root).rstrip('/')}/.inis-sandbox-hydrate-{self.state.session_id.hex}.tar.b64"
        encoded = base64.b64encode(raw).decode("ascii")
        try:
            await self._session.write_file(tar_path, encoded)
            result = await self._session.exec(
                [
                    "bash",
                    "-lc",
                    # `< file` (stdin redirection) rather than a positional
                    # file argument: GNU base64 accepts either, but BSD/
                    # macOS base64 only decodes from stdin.
                    f"base64 -d < {shlex.quote(tar_path)} | tar -C {shlex.quote(str(root))} -xf -",
                ]
            )
        except InisError as e:
            raise WorkspaceArchiveWriteError(path=root, cause=e, retryable=e.retryable) from e
        finally:
            try:
                await self._session.remove(tar_path)
            except InisError:
                pass
        if result.exit_code != 0:
            raise WorkspaceArchiveWriteError(
                path=root,
                context={"exit_code": result.exit_code, "stderr": result.stderr},
            )

    # -- teardown ---------------------------------------------------------------

    async def _shutdown_backend(self) -> None:
        # Deliberately a no-op, not a transport close. This never destroys
        # the inis.run session: for an attached session (`wrap()`), the SDK
        # runtime never even calls this (`owns_session=False`); for an
        # owned session (`create()`/`resume()`), the runtime calls
        # `session.stop()` -> `session.shutdown()` (this method) ->
        # `InisSandboxClient.delete()`, in that order, on the SAME session
        # object. Closing this session's local HTTP transport here would
        # leave `delete()` unable to make its own `destroy()` request right
        # after — that was a real bug caught live: it silently orphaned the
        # inis.run session because `delete()`'s failure wasn't an
        # `InisError` (it was httpx raising on a closed client), so nothing
        # surfaced except the SDK's own generic "failed to clean up sandbox
        # resources" log line. The transport is closed in
        # `InisSandboxClient.delete()` itself, after `destroy()` runs.
        return


def _client_options_to_session_kwargs(options: InisSandboxClientOptions) -> dict[str, object]:
    kwargs: dict[str, object] = {}
    if options.template is not None:
        kwargs["template"] = options.template
    if options.size is not None:
        kwargs["size"] = options.size
    if options.egress_default is not None:
        kwargs["egress_default"] = options.egress_default
    if options.egress_allow is not None:
        kwargs["egress_allow"] = list(options.egress_allow)
    if options.name is not None:
        kwargs["name"] = options.name
    if options.labels is not None:
        kwargs["labels"] = options.labels
    if options.idle_timeout_ms is not None:
        kwargs["idle_timeout_ms"] = options.idle_timeout_ms
    if options.max_lifetime_ms is not None:
        kwargs["max_lifetime_ms"] = options.max_lifetime_ms
    if options.connections is not None:
        kwargs["connections"] = list(options.connections)
    return kwargs


class InisSandboxClient(BaseSandboxClient[InisSandboxClientOptions]):
    """`BaseSandboxClient` that creates/resumes/destroys inis.run sessions
    owned by a single `Runner.run()` call. See the module docstring — for a
    real multi-turn conversation, use `InisSandboxSession.wrap()` with
    `RunConfig(sandbox=SandboxRunConfig(session=...))` instead; that path
    never goes through this client at all.
    """

    backend_id = "inis"
    supports_default_options = True  # every InisSandboxClientOptions field is optional

    def __init__(self, *, api_key: str | None = None, base_url: str | None = None) -> None:
        self._api_key = api_key
        self._base_url = base_url
        self._client = AsyncClient(token=api_key, base_url=base_url)

    async def create(
        self,
        *,
        snapshot: SnapshotSpec | SnapshotBase | None = None,
        manifest: Manifest | None = None,
        options: InisSandboxClientOptions | None = None,
    ) -> SandboxSession:
        options = options or InisSandboxClientOptions()
        session_kwargs = _client_options_to_session_kwargs(options)
        session = await self._client.sessions.create(**session_kwargs)
        assert session.session_id is not None
        state = InisSandboxSessionState(
            snapshot=resolve_snapshot(snapshot, session.session_id),
            manifest=manifest or Manifest(),
            inis_session_id=session.session_id,
            base_url=options.base_url,
            workspace_root_ready=True,
        )
        inner = InisSandboxSession(state=state, session=session)
        return self._wrap_session(inner)

    async def delete(self, session: SandboxSession) -> SandboxSession:
        inner = session._inner if isinstance(session, SandboxSession) else session
        assert isinstance(inner, InisSandboxSession)
        try:
            await inner._session.destroy()
        except InisError:
            pass  # best-effort: already gone is fine
        finally:
            # Close the local transport only now that destroy() has had its
            # chance to use it — see `_shutdown_backend`'s comment for why
            # order matters here.
            await inner._session.aclose()
        return session

    async def resume(self, state: SandboxSessionState) -> SandboxSession:
        assert isinstance(state, InisSandboxSessionState)
        # The owned session this state was captured from is very likely
        # already destroyed (the SDK calls `delete()` unconditionally at the
        # end of every owned `Runner.run()` — see module docstring), so this
        # always creates a *new* inis.run session and restores the workspace
        # from `state.snapshot` via `hydrate_workspace()` rather than trying
        # to reattach to `state.inis_session_id`.
        session = await self._client.sessions.create()
        assert session.session_id is not None
        new_state = state.model_copy(update={"inis_session_id": session.session_id})
        inner = InisSandboxSession(state=new_state, session=session)
        wrapped = self._wrap_session(inner)
        await wrapped.start()
        return wrapped

    def deserialize_session_state(self, payload: dict[str, object]) -> SandboxSessionState:
        return self._deserialize_session_state_payload(payload, InisSandboxSessionState)

    async def aclose(self) -> None:
        await self._client.close()
