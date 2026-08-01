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

Explicitly not implemented (declared, not faked)
-------------------------------------------------------------------------
- **PTY.** `supports_pty()` stays the ABC default (`False`); inis.run's PTY
  primitive is a separate product surface (see the `inis` SDK's `pty()`)
  not wired through this beta cut.
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

import base64
import io
import secrets
import shlex
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
from agents.sandbox.snapshot import SnapshotBase, SnapshotSpec, resolve_snapshot
from agents.sandbox.types import ExecResult as OAIExecResult
from agents.sandbox.types import ExposedPortEndpoint, User

from inis import AsyncClient, AsyncSession, InisError

# Marker prefix so a tar-shaped persisted workspace is unambiguous even if an
# empty/noop snapshot payload round-trips through `bytes` somewhere.
_TAR_MAGIC = b"INIS_SANDBOX_TAR_V1\n"


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
    ) -> OAIExecResult:
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
        if result.truncated:
            # `OAIExecResult` (agents.sandbox.types.ExecResult) has no
            # `truncated` slot at all -- just stdout/stderr/exit_code -- so
            # there is nowhere typed to carry this. Rather than silently
            # dropping the one piece of information that says "what you're
            # about to read is an incomplete view of what the command
            # produced" (inis.run's own guest-side per-stream capture cap
            # cut it before it ever reached this adapter), append a visible
            # marker to stderr so it is at least legible to whatever reads
            # the tool result, model included.
            #
            # This runs on code the sandbox exists to run untrusted/
            # AI-generated input, and this package's source is public, so a
            # static marker string could just be read out of this file and
            # printed verbatim by the guest to fake (or mask) a truncation
            # signal. A random per-call nonce, chosen host-side after the
            # command has already run, can't be predicted by guest code no
            # matter how thoroughly it has read this source -- it closes off
            # blind copy-paste spoofing even though a guest with full
            # control of its own output can still print *some* text shaped
            # like the marker with the wrong nonce.
            nonce = secrets.token_hex(8)
            stderr += (
                f"\n[inis: output truncated by the sandbox's own capture limit (nonce={nonce})]"
            ).encode("utf-8")
        return OAIExecResult(stdout=stdout, stderr=stderr, exit_code=result.exit_code)

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
