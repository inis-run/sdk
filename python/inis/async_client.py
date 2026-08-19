"""Async twin of inis.client, built on httpx.AsyncClient.

Every request/response dataclass, enum, and pure-mapping helper is imported
from inis.client rather than redefined here — one source of truth for the
wire format, no copy-paste model drift between the sync and async surfaces.
Only the I/O-bound pieces (Session, Client, and their sub-APIs) are
reimplemented against httpx.AsyncClient, using ``async def`` methods and
``async for`` generators in place of blocking calls and iterators.

Usage mirrors the sync client one-for-one::

    async with AsyncClient() as client:
        async with client.session(template="python") as s:
            result = await s.exec("python --version")
            async for event in s.exec_stream("python -u long_job.py"):
                ...
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import time
from typing import Any, AsyncIterator, Callable

import httpx

from inis.client import (
    DEFAULT_BASE_URL,
    ArchiveStatus,
    ArtifactInfo,
    BatchExecResult,
    Capacity,
    CapacityLimits,
    CheckpointInfo,
    ConnectionSpec,
    ConnectionStatus,
    ConnectorInfo,
    DestroyReason,
    DomainInfo,
    ExecResult,
    ExecStreamEvent,
    ExposeResult,
    ForkResult,
    InisError,
    OnPtyDetachPolicy,
    ProcessInfo,
    ProcessLogEvent,
    ProcessLogs,
    RegistryCredentialInfo,
    SessionInfo,
    TemplateInfo,
    VolumeInfo,
    WebhookDeliveryInfo,
    WebhookEndpointInfo,
    _connection_spec_payload,
    _egress_payload,
    _IncrementalUtf8Decoder,
    _raise_for_status,
    _map_artifact,
    _map_batch_results,
    _map_checkpoint,
    _map_domain,
    _map_connection_status,
    _map_connector,
    _map_process_info,
    _map_registry_credential,
    _map_session_info,
    _map_template,
    _map_volume,
    _map_webhook,
    _map_webhook_delivery,
    _resolve_base_url,
    _resolve_token,
)
from inis import interpreter_common as _ic
from inis.interpreter_common import InterpreterResult

__all__ = [
    "AsyncClient",
    "AsyncFilesAPI",
    "AsyncInisClient",
    "AsyncSession",
]

# Mirrors protocol.MaxStreamChunkBytes: the bounded chunk size
# AsyncSession.upload_file reads the local file in, so it never holds more
# than one chunk in memory regardless of file size.
_UPLOAD_CHUNK_BYTES = 4 << 20


async def _async_file_chunks(path: str | os.PathLike, chunk_size: int = _UPLOAD_CHUNK_BYTES) -> AsyncIterator[bytes]:
    """Async generator yielding path's contents in bounded chunks — the
    request body for AsyncSession.upload_file's streamed PUT. Each chunk is a
    plain blocking os-level read (no async file API in the stdlib), but only
    one chunk is ever held in memory at a time.
    """
    with open(path, "rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            yield chunk


class AsyncFilesAPI:
    """Backward-compatible namespace for session file operations (async).

    Delegates to the AsyncSession's direct read_file / list_files / write_file
    methods, mirroring inis.client.FilesAPI.
    """

    def __init__(self, session: "AsyncSession") -> None:
        self._session = session

    async def read(self, path: str, *, timeout_ms: int | None = None) -> str:
        return await self._session.read_file(path)

    async def write(self, path: str, content: str, *, timeout_ms: int | None = None) -> None:
        await self._session.write_file(path, content)

    async def list(self, path: str = "/workspace", *, timeout_ms: int | None = None) -> list[str]:
        return await self._session.list_files(path)


class AsyncSession:
    """Async context manager for a live sandbox session.

    Full async twin of inis.client.Session: same fields, same request
    payloads, same response mapping — only the transport is httpx.AsyncClient
    and every I/O method is a coroutine (or, for the SSE endpoints, an async
    generator).
    """

    def __init__(
        self,
        *,
        base_url: str,
        token: str,
        volume_id: str | None = None,
        max_lifetime_ms: int | None = None,
        idle_timeout_ms: int | None = None,
        name: str | None = None,
        labels: dict[str, str] | None = None,
        external_id: str | None = None,
        idempotency_key: str | None = None,
        egress_default: str | None = None,
        egress_allow: list[str] | None = None,
        from_checkpoint: str | None = None,
        template: str | None = None,
        size: str | None = None,
        no_sudo: bool = False,
        wake_on_http: bool = True,
        on_pty_detach: str | OnPtyDetachPolicy | None = None,
        paused_ttl_ms: int | None = None,
        command_capture: str | None = None,
        env: dict[str, str] | None = None,
        secrets: dict[str, str] | None = None,
        connectors: list[str] | None = None,
        connections: list[ConnectionSpec | dict[str, Any]] | None = None,
        timeout: float = 120.0,
        session_id: str | None = None,
        on_status: Callable[[str], None] | None = None,
        wait: bool = True,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._volume_id = volume_id
        self._egress_default = egress_default
        self._egress_allow = egress_allow
        self._max_lifetime_ms = max_lifetime_ms
        self._idle_timeout_ms = idle_timeout_ms
        self._name = name
        self._labels = labels
        self._external_id = external_id
        self._idempotency_key = idempotency_key
        self._from_checkpoint = from_checkpoint
        self._template = template
        self._size = size
        self._no_sudo = no_sudo
        self._wake_on_http = wake_on_http
        self._on_pty_detach = (
            on_pty_detach.value
            if isinstance(on_pty_detach, OnPtyDetachPolicy)
            else on_pty_detach
        )
        self._paused_ttl_ms = paused_ttl_ms
        self._command_capture = command_capture
        self._env = env
        self._secrets = secrets
        self._connectors = connectors
        self._connections = connections
        self._timeout = timeout
        self._client = httpx.AsyncClient(timeout=timeout)
        self.session_id: str | None = session_id
        self.files = AsyncFilesAPI(self)
        self._on_status = on_status
        self._wait = wait

    @classmethod
    def attach(
        cls,
        session_id: str,
        *,
        base_url: str | None = None,
        token: str | None = None,
        timeout: float = 120.0,
    ) -> "AsyncSession":
        """Bind to an existing session without creating a new one.

        base_url and token default to the same env resolution as AsyncClient
        (INIS_BASE_URL / INIS_API_KEY) when omitted. Prefer
        client.sessions.attach(session_id) when you already have a Client.
        """
        return cls(
            base_url=_resolve_base_url(base_url),
            token=_resolve_token(token),
            timeout=timeout,
            session_id=session_id,
        )

    # ── Context manager ───────────────────────────────────────────────────────

    async def __aenter__(self) -> "AsyncSession":
        if self.session_id:
            return self
        payload: dict[str, Any] = {}
        if self._volume_id:
            payload["volume_id"] = self._volume_id
        if self._max_lifetime_ms:
            payload["max_lifetime_ms"] = self._max_lifetime_ms
        if self._idle_timeout_ms:
            payload["idle_timeout_ms"] = self._idle_timeout_ms
        if self._name:
            payload["name"] = self._name
        if self._labels:
            payload["labels"] = self._labels
        if self._external_id:
            payload["external_id"] = self._external_id
        if self._from_checkpoint:
            payload["from_checkpoint"] = self._from_checkpoint
        if self._template:
            payload["template"] = self._template
        if self._size:
            payload["size"] = self._size
        if self._no_sudo:
            payload["no_sudo"] = True
        if not self._wake_on_http:
            payload["wake_on_http"] = False
        if self._on_pty_detach:
            payload["on_pty_detach"] = self._on_pty_detach
        if self._paused_ttl_ms is not None:
            payload["paused_ttl_ms"] = self._paused_ttl_ms
        if self._command_capture:
            payload["command_capture"] = self._command_capture
        if self._env:
            payload["env"] = self._env
        if self._secrets:
            payload["secrets"] = self._secrets
        if self._connectors:
            payload["connectors"] = self._connectors
        if self._connections:
            payload["connections"] = [_connection_spec_payload(c) for c in self._connections]
        egress = _egress_payload(self._egress_default, self._egress_allow)
        if egress is not None:
            payload["egress"] = egress
        create_headers = None
        if self._idempotency_key:
            create_headers = {"Idempotency-Key": self._idempotency_key}
        resp = await self._request("POST", "/v1/sessions", json=payload, headers=create_headers)
        self.session_id = resp["session_id"]
        if self._wait:
            await self._wait_until_ready(resp)
        return self

    # Same budget as the sync Session.
    _CREATE_POLL_TIMEOUT_S = 300.0
    _CREATE_POLL_INTERVAL_S = 0.3

    async def _wait_until_ready(self, resp: dict[str, Any]) -> None:
        """Poll GET until this session leaves state="creating", async twin
        of Session._wait_until_ready — see there for the full rationale."""
        state = resp.get("state")
        detail = resp.get("status_detail")
        if state != "creating":
            return
        if detail and self._on_status:
            self._on_status(detail)
        deadline = time.monotonic() + self._CREATE_POLL_TIMEOUT_S
        while True:
            if time.monotonic() > deadline:
                raise InisError(
                    f"session {self.session_id} did not become ready within "
                    f"{self._CREATE_POLL_TIMEOUT_S:.0f}s"
                )
            await asyncio.sleep(self._CREATE_POLL_INTERVAL_S)
            info = await self._request("GET", f"/v1/sessions/{self.session_id}")
            state = info.get("state")
            if state == "creating":
                detail = info.get("status_detail")
                if detail and self._on_status:
                    self._on_status(detail)
                continue
            if state == "failed":
                reason = info.get("status_reason") or "provisioning failed"
                raise InisError(f"session {self.session_id} failed to start: {reason}")
            return

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if self.session_id:
            try:
                await self.destroy()
            finally:
                pass
        await self._client.aclose()

    async def aclose(self) -> None:
        """Close this object's local HTTP transport without destroying the
        session server-side.

        Use this for a session you attached to but do not own (`attach()` /
        `client.sessions.attach(...)`) — `__aexit__` always destroys, which is
        correct for an owned session's `async with` block but wrong for one
        you are only borrowing: attached sessions must not be destroyed.
        Safe to call more than once. A session created via
        `async with AsyncSession(...)` and torn down through `__aexit__`
        does not need this — that path already closes the transport.
        """
        await self._client.aclose()

    # ── Session state ─────────────────────────────────────────────────────────

    async def get(self) -> SessionInfo:
        """Fetch the current state of this session."""
        resp = await self._request("GET", f"/v1/sessions/{self.session_id}")
        return _map_session_info(resp)

    async def info(self) -> SessionInfo:
        """Fetch the current state of this session. Alias for get()."""
        return await self.get()

    async def pause(self) -> SessionInfo:
        """Pause this session."""
        resp = await self._request("POST", f"/v1/sessions/{self.session_id}/pause")
        return _map_session_info(resp)

    async def resume(self) -> SessionInfo:
        """Resume this paused session."""
        resp = await self._request("POST", f"/v1/sessions/{self.session_id}/resume")
        return _map_session_info(resp)

    async def destroy(self, *, reason: str | DestroyReason | None = None) -> None:
        """Destroy this session and free all its resources."""
        if not self.session_id:
            return
        req_params: dict[str, Any] | None = None
        if reason is not None:
            req_params = {
                "reason": reason.value if isinstance(reason, DestroyReason) else reason
            }
        await self._request("DELETE", f"/v1/sessions/{self.session_id}", params=req_params)
        self.session_id = None

    # ── Exec ──────────────────────────────────────────────────────────────────

    async def exec(
        self,
        command: str | list[str],
        *,
        cwd: str | None = None,
        timeout_ms: int | None = None,
    ) -> ExecResult:
        """Run a command inside this session."""
        if isinstance(command, str):
            command = ["bash", "-lc", command]
        payload: dict[str, Any] = {"command": command}
        if cwd:
            payload["cwd"] = cwd
        if timeout_ms:
            payload["timeout_ms"] = timeout_ms
        resp = await self._request("POST", f"/v1/sessions/{self.session_id}/exec", json=payload)
        return ExecResult(
            stdout=resp.get("stdout", ""),
            stderr=resp.get("stderr", ""),
            exit_code=resp.get("exit_code", 0),
            duration_ms=resp.get("duration_ms", 0),
            timed_out=resp.get("timed_out", False),
            restore_ms=resp.get("restore_ms"),
            truncated=resp.get("truncated", False),
            stdout_encoding=resp.get("stdout_encoding"),
            stderr_encoding=resp.get("stderr_encoding"),
        )

    async def exec_stream(
        self,
        command: str | list[str],
        *,
        cwd: str | None = None,
        timeout_ms: int | None = None,
    ) -> AsyncIterator[ExecStreamEvent]:
        """Run a command inside this session, streaming stdout/stderr live via
        Server-Sent Events instead of waiting for it to finish.

        Async twin of Session.exec_stream — an async generator yielding an
        ExecStreamEvent per chunk as the guest produces it, ending with
        exactly one terminal event: stream="exit" (exit_code/timed_out/
        duration_ms populated) once the command completes or its timeout
        fires, or stream="error" on a guest-side failure. Breaking out of the
        loop early (or letting the generator be garbage collected) closes the
        underlying HTTP connection, which the server takes as a disconnect
        and kills the command — unlike start_process(), it does not keep
        running in the background.
        """
        if isinstance(command, str):
            command = ["bash", "-lc", command]
        payload: dict[str, Any] = {"command": command}
        if cwd:
            payload["cwd"] = cwd
        if timeout_ms:
            payload["timeout_ms"] = timeout_ms
        headers = {"Authorization": f"Bearer {self._token}"}
        url = f"{self._base_url}/v1/sessions/{self.session_id}/exec"
        async with self._client.stream(
            "POST", url, headers=headers, params={"stream": "true"}, json=payload
        ) as resp:
            if resp.status_code >= 400:
                await resp.aread()
                _raise_for_status(resp)
            stdout_decoder = _IncrementalUtf8Decoder()
            stderr_decoder = _IncrementalUtf8Decoder()
            event_type: str | None = None
            async for line in resp.aiter_lines():
                if not line:
                    continue
                if line.startswith("event:"):
                    event_type = line[len("event:") :].strip()
                elif line.startswith("data:"):
                    raw = line[len("data:") :].strip()
                    if event_type == "stdout" and raw:
                        yield ExecStreamEvent(stream="stdout", data=stdout_decoder.push(raw))
                    elif event_type == "stderr" and raw:
                        yield ExecStreamEvent(stream="stderr", data=stderr_decoder.push(raw))
                    elif event_type == "exit":
                        trailing_stdout = stdout_decoder.finish()
                        if trailing_stdout:
                            yield ExecStreamEvent(stream="stdout", data=trailing_stdout)
                        trailing_stderr = stderr_decoder.finish()
                        if trailing_stderr:
                            yield ExecStreamEvent(stream="stderr", data=trailing_stderr)
                        try:
                            exit_data: dict[str, Any] = json.loads(raw) if raw else {}
                        except ValueError:
                            exit_data = {}
                        yield ExecStreamEvent(
                            stream="exit",
                            data="",
                            exit_code=exit_data.get("exit_code"),
                            timed_out=exit_data.get("timed_out"),
                            duration_ms=exit_data.get("duration_ms"),
                        )
                        return
                    else:
                        yield ExecStreamEvent(stream=event_type or "", data=raw)
                        if event_type == "error":
                            return

    # ── Code interpreter ──────────────────────────────────────────────────────

    async def run_code(
        self, code: str, *, timeout_ms: int | None = None
    ) -> list[InterpreterResult]:
        """Run code in this session's persistent Python interpreter context.

        Async twin of Session.run_code() — same protocol (built on exec/
        write_file/read_file/start_process, no guest-agent or template
        changes), same typed result list ("text"/"image"/"table"/"json"/
        "error"), same transparent recovery if the kernel process has died.
        """
        timeout_ms = timeout_ms or _ic.DEFAULT_TIMEOUT_MS
        return await self._run_code_attempt(code, timeout_ms, allow_retry=True)

    async def restart_context(self) -> None:
        """Discard this session's interpreter context: kills the running
        kernel process (if any) and starts a fresh one."""
        try:
            await self.kill_process(_ic.KERNEL_NAME)
        except InisError:
            pass
        self._kernel_verified = False
        await self._ensure_kernel()

    async def _ensure_kernel(self) -> None:
        if getattr(self, "_kernel_verified", False):
            return
        running = False
        try:
            info = await self.get_process(_ic.KERNEL_NAME)
            running = info.state == "running"
        except InisError:
            running = False
        version_ok = False
        if running:
            try:
                installed = await self.read_file(_ic.VERSION_PATH)
                version_ok = installed.strip() == _ic.KERNEL_VERSION
            except InisError:
                version_ok = False
        if running and version_ok:
            self._kernel_verified = True
            return

        await self.exec(_ic.mkdirs_command())
        await self.write_file(_ic.KERNEL_PATH, _ic.KERNEL_SOURCE)
        await self.write_file(_ic.VERSION_PATH, _ic.KERNEL_VERSION)
        if running:
            try:
                await self.kill_process(_ic.KERNEL_NAME)
            except InisError:
                pass
        await self.start_process(
            _ic.KERNEL_NAME, ["python3", _ic.KERNEL_PATH, _ic.ROOT_DIR]
        )
        self._kernel_verified = True

    async def _run_code_attempt(
        self, code: str, timeout_ms: int, *, allow_retry: bool
    ) -> list[InterpreterResult]:
        await self._ensure_kernel()
        req_id = _ic.new_request_id()
        tmp_path, final_path = _ic.request_paths(req_id)
        await self.write_file(tmp_path, _ic.encode_request(req_id, code))
        wait_cmd = _ic.wait_command(req_id, tmp_path, final_path, timeout_ms)
        result = await self.exec(wait_cmd, timeout_ms=_ic.exec_timeout_ms(timeout_ms))
        raw_results = _ic.parse_envelope(result.stdout, req_id)
        if raw_results is None:
            if allow_retry:
                try:
                    info = await self.get_process(_ic.KERNEL_NAME)
                    kernel_alive = info.state == "running"
                except InisError:
                    kernel_alive = False
                if not kernel_alive:
                    self._kernel_verified = False
                    return await self._run_code_attempt(code, timeout_ms, allow_retry=False)
            return [_ic.timeout_result(timeout_ms)]
        return [await self._materialize_result(r) for r in raw_results]

    async def _materialize_result(self, raw: dict[str, Any]) -> InterpreterResult:
        result = _ic.raw_to_result(raw)
        if result.type == "image" and result.path:
            try:
                data = await self.read_file(result.path, encoding="base64")
                result.data = data if isinstance(data, bytes) else data.encode("utf-8")
            except InisError:
                pass
        return result

    # ── PTY (interactive terminal) ──────────────────────────────────────────────

    async def pty(
        self,
        *,
        cols: int = 80,
        rows: int = 24,
        command: list[str] | None = None,
        cwd: str | None = None,
        term: str = "xterm-256color",
        colorterm: str = "truecolor",
        open_timeout: float = 30.0,
    ) -> "AsyncPTY":
        """Open an interactive pseudo-terminal in this session.

        Async twin of Session.pty() — same protocol, coroutine methods and an
        async iterator for output::

            async with await session.pty(command=["python3", "-i"]) as term:
                async for chunk in term:
                    ...
        """
        from inis.pty import AsyncPTY as _AsyncPTY

        return await _AsyncPTY._open(
            base_url=self._base_url,
            token=self._token,
            session_id=self.session_id,
            rows=rows,
            cols=cols,
            command=command,
            cwd=cwd,
            term=term,
            colorterm=colorterm,
            open_timeout=open_timeout,
        )

    # ── Processes ─────────────────────────────────────────────────────────────

    async def start_process(
        self,
        name: str,
        command: str | list[str],
        *,
        cwd: str | None = None,
        keep_alive: bool = False,
    ) -> ProcessInfo:
        """Start a named, detached background process in this session.

        See Session.start_process for the full semantics — identical here,
        just awaited.
        """
        if isinstance(command, str):
            command = ["bash", "-lc", command]
        body: dict[str, Any] = {"name": name, "command": command, "keep_alive": keep_alive}
        if cwd:
            body["cwd"] = cwd
        resp = await self._request(
            "POST", f"/v1/sessions/{self.session_id}/processes", json=body
        )
        return _map_process_info(resp)

    async def list_processes(self) -> list[ProcessInfo]:
        """List every named background process in this session (running and exited)."""
        resp = await self._request("GET", f"/v1/sessions/{self.session_id}/processes")
        return [_map_process_info(p) for p in resp.get("processes", [])]

    async def get_process(self, name: str) -> ProcessInfo:
        """Get one named background process's current state."""
        resp = await self._request(
            "GET", f"/v1/sessions/{self.session_id}/processes/{name}"
        )
        return _map_process_info(resp)

    async def kill_process(self, name: str) -> ProcessInfo:
        """Kill a named background process: SIGTERM, then SIGKILL after a grace
        period if it hasn't exited. A no-op (just reports state) if it has
        already exited."""
        resp = await self._request(
            "DELETE", f"/v1/sessions/{self.session_id}/processes/{name}"
        )
        return _map_process_info(resp)

    async def get_process_logs(self, name: str) -> ProcessLogs:
        """Read a named process's captured stdout/stderr so far, buffered (one read)."""
        resp = await self._request(
            "GET", f"/v1/sessions/{self.session_id}/processes/{name}/logs"
        )
        return ProcessLogs(
            name=resp.get("name", name),
            stdout=resp.get("stdout", ""),
            stderr=resp.get("stderr", ""),
            truncated=resp.get("truncated", False),
            stdout_encoding=resp.get("stdout_encoding"),
            stderr_encoding=resp.get("stderr_encoding"),
        )

    async def stream_process_logs(self, name: str) -> AsyncIterator[ProcessLogEvent]:
        """Live-follow a named process's stdout/stderr via Server-Sent Events.

        Async generator twin of Session.stream_process_logs. Yields a
        ProcessLogEvent per chunk as the guest produces it, ending with
        event="eof" once the process exits and its output is fully drained,
        or event="error" on a guest-side failure. Both terminal events end
        iteration and close the underlying HTTP stream.
        """
        headers = {"Authorization": f"Bearer {self._token}"}
        url = f"{self._base_url}/v1/sessions/{self.session_id}/processes/{name}/logs"
        async with self._client.stream(
            "GET", url, headers=headers, params={"follow": "true"}
        ) as resp:
            if resp.status_code >= 400:
                await resp.aread()
                _raise_for_status(resp)
            stdout_decoder = _IncrementalUtf8Decoder()
            stderr_decoder = _IncrementalUtf8Decoder()
            event_type: str | None = None
            async for line in resp.aiter_lines():
                if not line:
                    continue
                if line.startswith("event:"):
                    event_type = line[len("event:") :].strip()
                elif line.startswith("data:"):
                    raw = line[len("data:") :].strip()
                    if event_type == "stdout" and raw:
                        yield ProcessLogEvent(event="stdout", data=stdout_decoder.push(raw))
                        continue
                    if event_type == "stderr" and raw:
                        yield ProcessLogEvent(event="stderr", data=stderr_decoder.push(raw))
                        continue
                    if event_type in ("eof", "error"):
                        trailing_stdout = stdout_decoder.finish()
                        if trailing_stdout:
                            yield ProcessLogEvent(event="stdout", data=trailing_stdout)
                        trailing_stderr = stderr_decoder.finish()
                        if trailing_stderr:
                            yield ProcessLogEvent(event="stderr", data=trailing_stderr)
                    yield ProcessLogEvent(event=event_type or "", data=raw)
                    if event_type in ("eof", "error"):
                        return

    # ── Files ─────────────────────────────────────────────────────────────────

    async def read_file(self, path: str, *, encoding: str = "text") -> str | bytes:
        """Read a file from the session filesystem.

        GET /v1/sessions/{id}/files?path=<path>&op=read[&encoding=base64]
        """
        params: dict[str, Any] = {"path": path, "op": "read"}
        if encoding != "text":
            params["encoding"] = encoding
        resp = await self._request(
            "GET",
            f"/v1/sessions/{self.session_id}/files",
            params=params,
        )
        content = resp.get("content", "")
        if encoding == "base64":
            return base64.b64decode(content)
        return content

    async def list_files(self, path: str = "/workspace") -> list[str]:
        """List entries under path in the session filesystem.

        GET /v1/sessions/{id}/files?path=<path>&op=list
        """
        resp = await self._request(
            "GET",
            f"/v1/sessions/{self.session_id}/files",
            params={"path": path, "op": "list"},
        )
        return resp.get("entries", [])

    async def write_file(self, path: str, content: str | bytes, encoding: str = "text") -> None:
        """Write content to a file in the session filesystem.

        PUT /v1/sessions/{id}/files
        """
        if isinstance(content, bytes):
            content = base64.b64encode(content).decode("ascii")
            encoding = "base64"
        await self._request(
            "PUT",
            f"/v1/sessions/{self.session_id}/files",
            json={"path": path, "content": content, "encoding": encoding},
        )

    async def write_files(self, files: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Write multiple small files under /workspace in one call.

        PUT /v1/sessions/{id}/files/batch. See Session.write_files for limits
        and the per-file result contract.
        """
        resp = await self._request(
            "PUT",
            f"/v1/sessions/{self.session_id}/files/batch",
            json={"files": files},
        )
        return resp.get("results", [])

    async def find_files(
        self,
        pattern: str,
        *,
        path: str = "/workspace",
        max_results: int | None = None,
    ) -> dict[str, Any]:
        """Find file paths under path matching a glob.

        GET /v1/sessions/{id}/files?op=find
        """
        params: dict[str, Any] = {"path": path, "op": "find", "pattern": pattern}
        if max_results:
            params["max_results"] = max_results
        resp = await self._request(
            "GET",
            f"/v1/sessions/{self.session_id}/files",
            params=params,
        )
        return {
            "paths": resp.get("paths", []),
            "truncated": resp.get("truncated", False),
        }

    async def grep_files(
        self,
        pattern: str,
        *,
        path: str = "/workspace",
        file_pattern: str | None = None,
        case_sensitive: bool = False,
        context_lines: int = 0,
        max_results: int | None = None,
        exclude_dirs: list[str] | None = None,
    ) -> dict[str, Any]:
        """Search file contents under path with a regex (Go RE2 syntax).

        GET /v1/sessions/{id}/files?op=grep
        """
        params: dict[str, Any] = {"path": path, "op": "grep", "pattern": pattern}
        if file_pattern:
            params["file_pattern"] = file_pattern
        if case_sensitive:
            params["case_sensitive"] = "true"
        if context_lines:
            params["context_lines"] = context_lines
        if max_results:
            params["max_results"] = max_results
        if exclude_dirs:
            params["exclude_dirs"] = ",".join(exclude_dirs)
        resp = await self._request(
            "GET",
            f"/v1/sessions/{self.session_id}/files",
            params=params,
        )
        return {
            "matches": resp.get("matches", []),
            "files_searched": resp.get("files_searched", 0),
            "truncated": resp.get("truncated", False),
        }

    async def mkdir(self, path: str) -> None:
        """Create a directory (with parents) in the session filesystem.

        POST /v1/sessions/{id}/files/mkdir. Async twin of Session.mkdir —
        always `mkdir -p` semantics.
        """
        await self._request(
            "POST",
            f"/v1/sessions/{self.session_id}/files/mkdir",
            json={"path": path},
        )

    async def remove(self, path: str, *, recursive: bool = False) -> None:
        """Remove a file, or a directory (with recursive=True).

        DELETE /v1/sessions/{id}/files?path=<path>&recursive=<bool>. Async
        twin of Session.remove.
        """
        params: dict[str, Any] = {"path": path}
        if recursive:
            params["recursive"] = "true"
        await self._request(
            "DELETE",
            f"/v1/sessions/{self.session_id}/files",
            params=params,
        )

    async def rename(self, path: str, dest_path: str) -> None:
        """Rename or move a file or directory.

        POST /v1/sessions/{id}/files/rename. Async twin of Session.rename.
        """
        await self._request(
            "POST",
            f"/v1/sessions/{self.session_id}/files/rename",
            json={"path": path, "dest_path": dest_path},
        )

    async def upload_file(self, local_path: str | os.PathLike, remote_path: str) -> dict[str, Any]:
        """Stream a local file into the session filesystem — NOT subject to
        write_files' 64-file/8 MiB batch cap. Async twin of
        Session.upload_file: the request body is an async generator reading
        the local file in bounded chunks (plain blocking os-level reads per
        chunk — there is no async file API in the stdlib and this package
        adds no extra dependency for one — but never more than one chunk
        held in memory at a time, matching the sync client's memory
        profile).
        """
        headers = {"Authorization": f"Bearer {self._token}"}
        url = f"{self._base_url}/v1/sessions/{self.session_id}/files/stream"
        resp = await self._client.request(
            "PUT", url, headers=headers, params={"path": remote_path}, content=_async_file_chunks(local_path)
        )
        if resp.status_code >= 400:
            _raise_for_status(resp)
        return resp.json()

    async def download_file(self, remote_path: str, local_path: str | os.PathLike) -> dict[str, Any]:
        """Stream a file from the session filesystem to local disk — NOT
        subject to read_file's 4 MiB whole-file cap. Async twin of
        Session.download_file: streams the response body via httpx's async
        streaming request instead of buffering the whole file in memory,
        computing a running sha256 as it writes.
        """
        headers = {"Authorization": f"Bearer {self._token}"}
        url = f"{self._base_url}/v1/sessions/{self.session_id}/files/stream"
        hasher = hashlib.sha256()
        total = 0
        async with self._client.stream(
            "GET", url, headers=headers, params={"path": remote_path}
        ) as resp:
            if resp.status_code >= 400:
                await resp.aread()
                _raise_for_status(resp)
            with open(local_path, "wb") as f:
                async for chunk in resp.aiter_bytes():
                    f.write(chunk)
                    hasher.update(chunk)
                    total += len(chunk)
        return {
            "path": remote_path,
            "local_path": str(local_path),
            "bytes": total,
            "sha256": hasher.hexdigest(),
        }

    # ── Fork / checkpoint ─────────────────────────────────────────────────────

    async def fork(self, count: int = 1) -> ForkResult:
        """Fork this session into count independent children."""
        resp = await self._request(
            "POST",
            f"/v1/sessions/{self.session_id}/fork",
            json={"count": count},
        )
        return ForkResult(
            parent_session_id=resp.get("parent_session_id", ""),
            children=list(resp.get("children", [])),
        )

    async def checkpoint(
        self,
        *,
        name: str | None = None,
        labels: dict[str, str] | None = None,
    ) -> CheckpointInfo:
        """Capture a named, retained checkpoint of this live session."""
        body: dict[str, Any] = {}
        if name:
            body["name"] = name
        if labels:
            body["labels"] = labels
        resp = await self._request(
            "POST",
            f"/v1/sessions/{self.session_id}/checkpoints",
            json=body,
        )
        return _map_checkpoint(resp)

    async def checkpoints(self) -> list[CheckpointInfo]:
        """List all checkpoints captured from this session."""
        resp = await self._request("GET", f"/v1/sessions/{self.session_id}/checkpoints")
        return [_map_checkpoint(c) for c in resp.get("checkpoints", [])]

    async def restore(self, checkpoint_id: str) -> SessionInfo:
        """Roll this session back to a checkpoint in place."""
        resp = await self._request(
            "POST",
            f"/v1/sessions/{self.session_id}/restore",
            json={"checkpoint_id": checkpoint_id},
        )
        return _map_session_info(resp)

    async def save_as_template(
        self,
        name: str,
        *,
        description: str | None = None,
    ) -> TemplateInfo:
        """Promote this live session into a named, reusable template."""
        body: dict[str, Any] = {"name": name}
        if description:
            body["description"] = description
        resp = await self._request(
            "POST",
            f"/v1/sessions/{self.session_id}/templates",
            json=body,
        )
        return _map_template(resp)

    # ── Archive ───────────────────────────────────────────────────────────────

    async def archive_retry(self) -> ArchiveStatus:
        """Re-drive a stuck or failed durable upload for this paused session."""
        resp = await self._request("POST", f"/v1/sessions/{self.session_id}/archive/retry")
        return ArchiveStatus(
            status=resp.get("status", ""),
            cold_uri=resp.get("cold_uri"),
            uploaded_at=resp.get("uploaded_at"),
        )

    # ── Ports ─────────────────────────────────────────────────────────────────

    async def expose(
        self,
        port: int,
        *,
        visibility: str | None = None,
        auth: str | None = None,
    ) -> ExposeResult:
        """Expose a guest port as a Caddy-proxied preview URL."""
        body: dict[str, Any] = {"port": port}
        if visibility is not None:
            body["visibility"] = visibility
        if auth is not None:
            body["auth"] = auth
        resp = await self._request(
            "POST",
            f"/v1/sessions/{self.session_id}/expose",
            json=body,
        )
        return ExposeResult(
            session_id=resp["session_id"],
            port=resp["port"],
            preview_url=resp["preview_url"],
            ingress_token=resp.get("ingress_token"),
            guest_ip=resp.get("guest_ip"),
            visibility=resp.get("visibility"),
            auth=resp.get("auth"),
            auth_token=resp.get("auth_token"),
        )

    async def unexpose(self, port: int) -> bool:
        """Remove a previously exposed port."""
        resp = await self._request(
            "POST",
            f"/v1/sessions/{self.session_id}/unexpose",
            json={"port": port},
        )
        return bool(resp.get("ok", False))

    # ── Egress ────────────────────────────────────────────────────────────────

    async def get_egress(self) -> dict[str, Any]:
        """Read the active egress policy for this session."""
        return await self._request("GET", f"/v1/sessions/{self.session_id}/egress")

    async def set_egress(
        self,
        *,
        mode: str = "deny",
        allow: list[str] | None = None,
    ) -> dict[str, Any]:
        """Replace the egress policy on this live session."""
        body: dict[str, Any] = {"mode": mode}
        if allow:
            body["allow"] = allow
        return await self._request(
            "POST", f"/v1/sessions/{self.session_id}/egress", json=body
        )

    # ── Connections ───────────────────────────────────────────────────────────
    # TODO: list_connections() / add_connection() for a Connection added to an
    # already-live session (GET and POST .../connections) land here once the
    # server verbs ship.

    async def rotate_connection(
        self,
        name: str,
        *,
        origin: str,
        secret: str,
        methods: list[str],
        paths: list[str],
        auth_type: str = "bearer",
        header_name: str | None = None,
        ttl_seconds: int | None = None,
    ) -> ConnectionStatus:
        """Replace Connection `name` on this live or paused session.

        See Session.rotate_connection for the full contract, identical
        here -- origin/secret/methods/paths/ttl_seconds fully replace
        whatever Connection `name` currently has.
        """
        spec = ConnectionSpec(
            name=name,
            origin=origin,
            secret=secret,
            methods=methods,
            paths=paths,
            auth_type=auth_type,
            header_name=header_name,
            ttl_seconds=ttl_seconds,
        )
        resp = await self._request(
            "PUT",
            f"/v1/sessions/{self.session_id}/connections/{name}",
            json=_connection_spec_payload(spec),
        )
        return _map_connection_status(resp)

    async def revoke_connection(self, name: str) -> None:
        """Revoke Connection `name` immediately. See
        Session.revoke_connection -- fails closed, idempotent."""
        await self._request("DELETE", f"/v1/sessions/{self.session_id}/connections/{name}")

    # ── Artifacts ─────────────────────────────────────────────────────────────

    async def capture_artifacts(
        self,
        paths: list[str],
        *,
        destination: dict[str, Any] | None = None,
    ) -> ArtifactInfo:
        """Capture output files from this session to durable storage."""
        body: dict[str, Any] = {"paths": paths}
        if destination:
            body["destination"] = destination
        resp = await self._request(
            "POST",
            f"/v1/sessions/{self.session_id}/artifacts",
            json=body,
        )
        return _map_artifact(resp)

    async def artifacts(self) -> list[ArtifactInfo]:
        """List artifact captures for this session."""
        resp = await self._request("GET", f"/v1/sessions/{self.session_id}/artifacts")
        return [_map_artifact(a) for a in resp.get("artifacts", [])]

    # ── Batch (static) ────────────────────────────────────────────────────────

    @staticmethod
    async def batch_exec(
        *,
        base_url: str,
        token: str,
        session_ids: list[str],
        command: str | list[str],
        cwd: str | None = None,
        timeout_ms: int | None = None,
        timeout: float = 120.0,
    ) -> list[BatchExecResult]:
        """Fan the same command across multiple sessions in parallel."""
        if isinstance(command, str):
            command = ["bash", "-lc", command]
        payload: dict[str, Any] = {"session_ids": session_ids, "command": command}
        if cwd:
            payload["cwd"] = cwd
        if timeout_ms:
            payload["timeout_ms"] = timeout_ms
        async with httpx.AsyncClient(timeout=timeout) as client:
            headers = {"Authorization": f"Bearer {token}"}
            resp = await client.post(
                f"{base_url.rstrip('/')}/v1/sessions/batch/exec",
                headers=headers,
                json=payload,
            )
            if resp.status_code >= 400:
                _raise_for_status(resp)
            data = resp.json()
        return _map_batch_results(data)

    # ── Internal ──────────────────────────────────────────────────────────────

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        req_headers = {"Authorization": f"Bearer {self._token}"}
        if headers:
            req_headers.update(headers)
        resp = await self._client.request(
            method,
            f"{self._base_url}{path}",
            headers=req_headers,
            params=params,
            **kwargs,
        )
        if resp.status_code >= 400:
            _raise_for_status(resp)
        if resp.status_code == 204:
            return {}
        return resp.json()


class AsyncInisClient:
    """One-shot operations backed by a throwaway session (async twin of
    inis.client.InisClient).

    Each call creates a session, runs the command, and tears it down. There
    is no separate stateless surface — a session is the only primitive.
    """

    def __init__(self, *, base_url: str, token: str, timeout: float = 120.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._client = httpx.AsyncClient(timeout=timeout)

    async def exec(
        self,
        command: str | list[str],
        *,
        cwd: str | None = None,
        volume_id: str | None = None,
        timeout_ms: int | None = None,
        egress_default: str | None = None,
        egress_allow: list[str] | None = None,
        size: str | None = None,
    ) -> ExecResult:
        if isinstance(command, str):
            command = ["bash", "-lc", command]
        create: dict[str, Any] = {"destroy_on_completion": True}
        if volume_id:
            create["volume_id"] = volume_id
        if size:
            create["size"] = size
        egress = _egress_payload(egress_default, egress_allow)
        if egress is not None:
            create["egress"] = egress
        session = await self._post("/v1/sessions", create)
        session_id = session["session_id"]
        try:
            payload: dict[str, Any] = {"command": command}
            if cwd:
                payload["cwd"] = cwd
            if timeout_ms:
                payload["timeout_ms"] = timeout_ms
            resp = await self._post(f"/v1/sessions/{session_id}/exec", payload)
        finally:
            try:
                await self._client.request(
                    "DELETE",
                    f"{self._base_url}/v1/sessions/{session_id}",
                    headers={"Authorization": f"Bearer {self._token}"},
                )
            except Exception:
                pass
        return ExecResult(
            stdout=resp.get("stdout", ""),
            stderr=resp.get("stderr", ""),
            exit_code=resp.get("exit_code", 0),
            duration_ms=resp.get("duration_ms", 0),
            timed_out=resp.get("timed_out", False),
            restore_ms=resp.get("restore_ms"),
            truncated=resp.get("truncated", False),
            stdout_encoding=resp.get("stdout_encoding"),
            stderr_encoding=resp.get("stderr_encoding"),
        )

    async def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {self._token}"}
        resp = await self._client.post(f"{self._base_url}{path}", headers=headers, json=payload)
        if resp.status_code >= 400:
            _raise_for_status(resp)
        return resp.json()

    async def close(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "AsyncInisClient":
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        await self.close()


class _AsyncSessionsAPI:
    def __init__(self, client: "AsyncClient") -> None:
        self._client = client

    async def create(
        self,
        *,
        volume_id: str | None = None,
        max_lifetime_ms: int | None = None,
        idle_timeout_ms: int | None = None,
        name: str | None = None,
        labels: dict[str, str] | None = None,
        external_id: str | None = None,
        idempotency_key: str | None = None,
        egress_default: str | None = None,
        egress_allow: list[str] | None = None,
        from_checkpoint: str | None = None,
        template: str | None = None,
        size: str | None = None,
        no_sudo: bool = False,
        wake_on_http: bool = True,
        on_pty_detach: str | OnPtyDetachPolicy | None = None,
        paused_ttl_ms: int | None = None,
        command_capture: str | None = None,
        env: dict[str, str] | None = None,
        secrets: dict[str, str] | None = None,
        connectors: list[str] | None = None,
        connections: list[ConnectionSpec | dict[str, Any]] | None = None,
        on_status: Callable[[str], None] | None = None,
        wait: bool = True,
    ) -> AsyncSession:
        """Create and start a new session. See Client.sessions.create for the
        full semantics (env/secrets, external_id, idempotency_key,
        connectors/connections, on_status/wait) — identical here."""
        session = self._client.session(
            volume_id=volume_id,
            max_lifetime_ms=max_lifetime_ms,
            idle_timeout_ms=idle_timeout_ms,
            name=name,
            labels=labels,
            external_id=external_id,
            idempotency_key=idempotency_key,
            egress_default=egress_default,
            egress_allow=egress_allow,
            from_checkpoint=from_checkpoint,
            template=template,
            size=size,
            no_sudo=no_sudo,
            wake_on_http=wake_on_http,
            on_pty_detach=on_pty_detach,
            paused_ttl_ms=paused_ttl_ms,
            command_capture=command_capture,
            env=env,
            secrets=secrets,
            connectors=connectors,
            connections=connections,
            on_status=on_status,
            wait=wait,
        )
        try:
            await session.__aenter__()
        except Exception:
            await session._client.aclose()
            raise
        return session

    async def get(self, session_id: str) -> SessionInfo:
        """Fetch state for a session by ID without attaching to it."""
        resp = await self._client._get(f"/v1/sessions/{session_id}")
        return _map_session_info(resp)

    async def attach(self, session_id: str) -> AsyncSession:
        """Bind to an existing session using this client's base_url/token."""
        return AsyncSession.attach(
            session_id,
            base_url=self._client._base_url,
            token=self._client._token,
            timeout=self._client._timeout,
        )

    async def list(
        self,
        *,
        state: str | None = None,
        limit: int | None = None,
        cursor: str | None = None,
        external_id: str | None = None,
    ) -> tuple[list[SessionInfo], str | None]:
        """List sessions, optionally filtered by state and/or external_id.

        external_id is not unique -- it may match more than one session.
        """
        params: dict[str, Any] = {}
        if state:
            params["state"] = state
        if limit:
            params["limit"] = limit
        if cursor:
            params["cursor"] = cursor
        if external_id:
            params["external_id"] = external_id
        resp = await self._client._get("/v1/sessions", params=params)
        sessions = [_map_session_info(item) for item in resp.get("sessions", [])]
        return sessions, resp.get("next_cursor")

    async def read_file(self, session_id: str, path: str) -> str:
        """Read a file from a session filesystem by session ID."""
        resp = await self._client._get(
            f"/v1/sessions/{session_id}/files",
            params={"path": path, "op": "read"},
        )
        return resp["content"]

    async def list_files(self, session_id: str, path: str) -> list[str]:
        """List entries under path in a session filesystem by session ID."""
        resp = await self._client._get(
            f"/v1/sessions/{session_id}/files",
            params={"path": path, "op": "list"},
        )
        return resp.get("entries", [])

    async def write_file(
        self, session_id: str, path: str, content: str, encoding: str = "text"
    ) -> None:
        """Write content to a file in a session filesystem by session ID."""
        await self._client._put(
            f"/v1/sessions/{session_id}/files",
            payload={"path": path, "content": content, "encoding": encoding},
        )

    async def batch_exec(
        self,
        session_ids: list[str],
        command: str | list[str],
        *,
        cwd: str | None = None,
        timeout_ms: int | None = None,
        timeout: float = 120.0,
    ) -> list[BatchExecResult]:
        """Fan the same command across multiple sessions in parallel."""
        if isinstance(command, str):
            command = ["bash", "-lc", command]
        payload: dict[str, Any] = {"session_ids": session_ids, "command": command}
        if cwd:
            payload["cwd"] = cwd
        if timeout_ms:
            payload["timeout_ms"] = timeout_ms
        resp = await self._client._post("/v1/sessions/batch/exec", payload)
        return _map_batch_results(resp)


class _AsyncCheckpointsAPI:
    def __init__(self, client: "AsyncClient") -> None:
        self._client = client

    async def get(self, checkpoint_id: str) -> CheckpointInfo:
        resp = await self._client._get(f"/v1/checkpoints/{checkpoint_id}")
        return _map_checkpoint(resp)

    async def delete(self, checkpoint_id: str) -> None:
        await self._client._delete(f"/v1/checkpoints/{checkpoint_id}")

    async def create_session(
        self,
        checkpoint_id: str,
        *,
        name: str | None = None,
        labels: dict[str, str] | None = None,
        max_lifetime_ms: int | None = None,
        idle_timeout_ms: int | None = None,
    ) -> AsyncSession:
        """Create a new, independent session from a checkpoint (template path)."""
        body: dict[str, Any] = {}
        if name:
            body["name"] = name
        if labels:
            body["labels"] = labels
        if max_lifetime_ms:
            body["max_lifetime_ms"] = max_lifetime_ms
        if idle_timeout_ms:
            body["idle_timeout_ms"] = idle_timeout_ms
        resp = await self._client._post(f"/v1/checkpoints/{checkpoint_id}/sessions", body)
        return AsyncSession.attach(
            resp["session_id"],
            base_url=self._client.base_url,
            token=self._client.token,
            timeout=self._client.timeout,
        )


class _AsyncArtifactsAPI:
    def __init__(self, client: "AsyncClient") -> None:
        self._client = client

    async def get(self, artifact_id: str) -> ArtifactInfo:
        """Get an artifact manifest (status + pre-signed download URLs)."""
        resp = await self._client._get(f"/v1/artifacts/{artifact_id}")
        return _map_artifact(resp)

    async def extend(self, artifact_id: str, ttl_days: int) -> ArtifactInfo:
        """Push an artifact's expiry forward (capped at the retention limit)."""
        resp = await self._client._post(
            f"/v1/artifacts/{artifact_id}/extend", {"ttl_days": ttl_days}
        )
        return _map_artifact(resp)

    async def delete(self, artifact_id: str) -> None:
        await self._client._delete(f"/v1/artifacts/{artifact_id}")


class _AsyncTemplatesAPI:
    def __init__(self, client: "AsyncClient") -> None:
        self._client = client

    async def list(self) -> list[TemplateInfo]:
        """List the templates available to this account (official and user)."""
        resp = await self._client._get("/v1/templates")
        return [_map_template(t) for t in resp.get("templates", [])]

    async def import_image(
        self,
        from_image: str,
        name: str,
        description: str | None = None,
    ) -> TemplateInfo:
        """Import a PUBLIC OCI image as a custom template.

        See Client.templates.import_image for the full contract — identical
        here, just awaited.
        """
        payload: dict[str, Any] = {"from_image": from_image, "name": name}
        if description is not None:
            payload["description"] = description
        resp = await self._client._post("/v1/templates", payload)
        return _map_template(resp)

    async def delete(self, name: str) -> None:
        """Delete a user template. Official templates cannot be deleted."""
        await self._client._delete(f"/v1/templates/{name}")


class _AsyncRegistriesAPI:
    """Private-registry pull credentials for template import — see
    inis.client._RegistriesAPI for the full contract, identical here."""

    def __init__(self, client: "AsyncClient") -> None:
        self._client = client

    async def add(
        self,
        name: str,
        *,
        registry_host: str,
        username: str,
        secret: str,
    ) -> RegistryCredentialInfo:
        payload = {
            "name": name,
            "registry_host": registry_host,
            "username": username,
            "secret": secret,
        }
        resp = await self._client._post("/v1/registry-credentials", payload)
        return _map_registry_credential(resp)

    async def list(self) -> list[RegistryCredentialInfo]:
        resp = await self._client._get("/v1/registry-credentials")
        return [_map_registry_credential(c) for c in resp.get("credentials", [])]

    async def delete(self, name: str) -> None:
        await self._client._delete(f"/v1/registry-credentials/{name}")


class _AsyncConnectorsAPI:
    """Egress connectors — see inis.client._ConnectorsAPI for the
    full contract, identical here."""

    def __init__(self, client: "AsyncClient") -> None:
        self._client = client

    async def add(
        self,
        name: str,
        *,
        target_base_url: str,
        auth_shape: str = "bearer",
        header_name: str | None = None,
        secret: str,
    ) -> ConnectorInfo:
        payload: dict = {
            "name": name,
            "target_base_url": target_base_url,
            "auth_shape": auth_shape,
            "secret": secret,
        }
        if header_name is not None:
            payload["header_name"] = header_name
        resp = await self._client._post("/v1/connectors", payload)
        return _map_connector(resp)

    async def list(self) -> list[ConnectorInfo]:
        resp = await self._client._get("/v1/connectors")
        return [_map_connector(c) for c in resp.get("connectors", [])]

    async def delete(self, name: str) -> None:
        await self._client._delete(f"/v1/connectors/{name}")


class _AsyncWebhooksAPI:
    """Webhook subscriptions for session events — see
    inis.client._WebhooksAPI for the full contract, identical here."""

    def __init__(self, client: "AsyncClient") -> None:
        self._client = client

    async def add(self, url: str, *, event_types: list[str] | None = None) -> WebhookEndpointInfo:
        payload: dict[str, Any] = {"url": url}
        if event_types:
            payload["event_types"] = event_types
        resp = await self._client._post("/v1/org/webhooks", payload)
        return _map_webhook(resp)

    async def list(self) -> list[WebhookEndpointInfo]:
        resp = await self._client._get("/v1/org/webhooks")
        return [_map_webhook(e) for e in resp.get("endpoints", [])]

    async def delete(self, endpoint_id: str) -> None:
        await self._client._delete(f"/v1/org/webhooks/{endpoint_id}")

    async def test(self, endpoint_id: str) -> WebhookDeliveryInfo:
        resp = await self._client._post(f"/v1/org/webhooks/{endpoint_id}/test", {})
        return _map_webhook_delivery(resp)

    async def deliveries(self, endpoint_id: str, *, limit: int = 50) -> list[WebhookDeliveryInfo]:
        resp = await self._client._get(f"/v1/org/webhooks/{endpoint_id}/deliveries", params={"limit": limit})
        return [_map_webhook_delivery(d) for d in resp.get("deliveries", [])]


class _AsyncDomainsAPI:
    """Custom domains with auto-TLS for exposed session ports — see
    inis.client._DomainsAPI for the full contract, identical here."""

    def __init__(self, client: "AsyncClient") -> None:
        self._client = client

    async def add(self, domain: str) -> DomainInfo:
        resp = await self._client._post("/v1/org/domains", {"domain": domain})
        return _map_domain(resp)

    async def list(self) -> list[DomainInfo]:
        resp = await self._client._get("/v1/org/domains")
        return [_map_domain(d) for d in resp.get("domains", [])]

    async def delete(self, domain_id: str) -> None:
        await self._client._delete(f"/v1/org/domains/{domain_id}")

    async def verify(self, domain_id: str) -> DomainInfo:
        resp = await self._client._post(f"/v1/org/domains/{domain_id}/verify", {})
        return _map_domain(resp)

    async def route(self, domain_id: str, session_id: str, port: int) -> DomainInfo:
        resp = await self._client._put(f"/v1/org/domains/{domain_id}/route", {"session_id": session_id, "port": port})
        return _map_domain(resp)

    async def unroute(self, domain_id: str) -> DomainInfo:
        resp = await self._client._delete_json(f"/v1/org/domains/{domain_id}/route")
        return _map_domain(resp)


class _AsyncVolumesAPI:
    """Persistent volumes -- see inis.client._VolumesAPI for the full
    contract, identical here."""

    def __init__(self, client: "AsyncClient") -> None:
        self._client = client

    async def create(self, *, size_gb: int | None = None) -> VolumeInfo:
        payload: dict[str, Any] = {}
        if size_gb is not None:
            payload["size_gb"] = size_gb
        resp = await self._client._post("/v1/volumes", payload)
        return _map_volume(resp)

    async def list(self) -> list[VolumeInfo]:
        resp = await self._client._get("/v1/volumes")
        return [_map_volume(v) for v in resp.get("volumes", [])]

    async def get(self, volume_id: str) -> VolumeInfo:
        resp = await self._client._get(f"/v1/volumes/{volume_id}")
        return _map_volume(resp)

    async def resize(self, volume_id: str, size_gb: int) -> VolumeInfo:
        resp = await self._client._patch(f"/v1/volumes/{volume_id}", {"size_gb": size_gb})
        return _map_volume(resp)

    async def delete(self, volume_id: str) -> None:
        await self._client._delete(f"/v1/volumes/{volume_id}")


class AsyncClient:
    """Async twin of inis.client.Client, backed by httpx.AsyncClient.

    Usage::

        async with AsyncClient() as client:
            async with client.session(template="python") as s:
                result = await s.exec("python --version")
    """

    def __init__(
        self,
        *,
        token: str | None = None,
        base_url: str | None = None,
        timeout: float = 120.0,
    ) -> None:
        self._token = _resolve_token(token)
        self._base_url = _resolve_base_url(base_url)
        self._timeout = timeout
        self._http = httpx.AsyncClient(timeout=timeout)
        self.sessions = _AsyncSessionsAPI(self)
        self.checkpoints = _AsyncCheckpointsAPI(self)
        self.artifacts = _AsyncArtifactsAPI(self)
        self.templates = _AsyncTemplatesAPI(self)
        self.registries = _AsyncRegistriesAPI(self)
        self.connectors = _AsyncConnectorsAPI(self)
        self.webhooks = _AsyncWebhooksAPI(self)
        self.domains = _AsyncDomainsAPI(self)
        self.volumes = _AsyncVolumesAPI(self)

    async def close(self) -> None:
        """Close the underlying HTTP connection pool."""
        await self._http.aclose()

    async def __aenter__(self) -> "AsyncClient":
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        await self.close()

    @property
    def base_url(self) -> str:
        return self._base_url

    @property
    def token(self) -> str:
        return self._token

    @property
    def timeout(self) -> float:
        return self._timeout

    def session(
        self,
        *,
        volume_id: str | None = None,
        max_lifetime_ms: int | None = None,
        idle_timeout_ms: int | None = None,
        name: str | None = None,
        labels: dict[str, str] | None = None,
        external_id: str | None = None,
        idempotency_key: str | None = None,
        egress_default: str | None = None,
        egress_allow: list[str] | None = None,
        from_checkpoint: str | None = None,
        template: str | None = None,
        size: str | None = None,
        no_sudo: bool = False,
        wake_on_http: bool = True,
        on_pty_detach: str | OnPtyDetachPolicy | None = None,
        paused_ttl_ms: int | None = None,
        command_capture: str | None = None,
        env: dict[str, str] | None = None,
        secrets: dict[str, str] | None = None,
        connectors: list[str] | None = None,
        connections: list[ConnectionSpec | dict[str, Any]] | None = None,
        on_status: Callable[[str], None] | None = None,
        wait: bool = True,
    ) -> AsyncSession:
        """Return an AsyncSession configured for use as an async context manager.

        The session is not created until ``__aenter__`` runs (i.e. entering
        an ``async with`` block). See Client.session for on_status/wait,
        external_id, idempotency_key, and the connectors/connections
        contract.
        """
        return AsyncSession(
            base_url=self._base_url,
            token=self._token,
            volume_id=volume_id,
            max_lifetime_ms=max_lifetime_ms,
            idle_timeout_ms=idle_timeout_ms,
            name=name,
            labels=labels,
            external_id=external_id,
            idempotency_key=idempotency_key,
            egress_default=egress_default,
            egress_allow=egress_allow,
            from_checkpoint=from_checkpoint,
            template=template,
            size=size,
            no_sudo=no_sudo,
            wake_on_http=wake_on_http,
            on_pty_detach=on_pty_detach,
            paused_ttl_ms=paused_ttl_ms,
            command_capture=command_capture,
            env=env,
            secrets=secrets,
            connectors=connectors,
            connections=connections,
            timeout=self._timeout,
            on_status=on_status,
            wait=wait,
        )

    async def execute(
        self,
        *,
        language: str,
        code: str,
        dependencies: list[str] | None = None,
        volume_id: str | None = None,
        timeout_ms: int | None = None,
        size: str | None = None,
        template: str | None = None,
        no_sudo: bool = False,
        command_capture: str | None = None,
    ) -> ExecResult:
        """One-shot language execution in a throwaway session."""
        payload: dict[str, Any] = {"language": language, "code": code}
        if dependencies:
            payload["dependencies"] = dependencies
        if volume_id:
            payload["volume_id"] = volume_id
        if timeout_ms:
            payload["timeout_ms"] = timeout_ms
        if size:
            payload["size"] = size
        if template:
            payload["template"] = template
        if no_sudo:
            payload["no_sudo"] = True
        if command_capture:
            payload["command_capture"] = command_capture
        resp = await self._post("/v1/sessions/exec", payload)
        return ExecResult(
            stdout=resp.get("stdout", ""),
            stderr=resp.get("stderr", ""),
            exit_code=resp.get("exit_code", 0),
            duration_ms=resp.get("duration_ms", 0),
            timed_out=resp.get("timed_out", False),
            restore_ms=resp.get("restore_ms"),
            install_ms=resp.get("install_ms"),
            phase=resp.get("phase"),
            truncated=resp.get("truncated", False),
            stdout_encoding=resp.get("stdout_encoding"),
            stderr_encoding=resp.get("stderr_encoding"),
        )

    async def capacity(self) -> Capacity:
        """Return aggregate session capacity and limits for the account."""
        resp = await self._get("/v1/org")
        cap = resp.get("capacity", {})
        limits = None
        if cap.get("limits") is not None:
            limits = CapacityLimits(
                running=cap["limits"].get("running", 0),
                warm=cap["limits"].get("warm", 0),
            )
        return Capacity(
            running=cap.get("running", 0),
            warm=cap.get("warm", 0),
            limits=limits,
        )

    async def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {self._token}"}
        resp = await self._http.post(
            f"{self._base_url}{path}",
            headers=headers,
            json=payload,
        )
        if resp.status_code >= 400:
            _raise_for_status(resp)
        return resp.json()

    async def _put(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {self._token}"}
        resp = await self._http.put(
            f"{self._base_url}{path}",
            headers=headers,
            json=payload,
        )
        if resp.status_code >= 400:
            _raise_for_status(resp)
        if resp.status_code == 204:
            return {}
        return resp.json()

    async def _patch(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {self._token}"}
        resp = await self._http.patch(
            f"{self._base_url}{path}",
            headers=headers,
            json=payload,
        )
        if resp.status_code >= 400:
            _raise_for_status(resp)
        return resp.json()

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {self._token}"}
        resp = await self._http.get(
            f"{self._base_url}{path}",
            headers=headers,
            params=params,
        )
        if resp.status_code >= 400:
            _raise_for_status(resp)
        return resp.json()

    async def _delete(self, path: str) -> None:
        headers = {"Authorization": f"Bearer {self._token}"}
        resp = await self._http.request("DELETE", f"{self._base_url}{path}", headers=headers)
        if resp.status_code >= 400:
            _raise_for_status(resp)

    async def _delete_json(self, path: str) -> dict[str, Any]:
        """Async twin of inis.client.Client._delete_json."""
        headers = {"Authorization": f"Bearer {self._token}"}
        resp = await self._http.request("DELETE", f"{self._base_url}{path}", headers=headers)
        if resp.status_code >= 400:
            _raise_for_status(resp)
        if resp.status_code == 204:
            return {}
        return resp.json()
