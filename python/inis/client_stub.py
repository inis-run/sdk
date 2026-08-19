"""inis.run Python SDK — full interface stub.

Every class, dataclass, and method in this file reflects the intended public
API documented in docs/api-design/python-sdk.md. All method bodies raise
NotImplementedError; this file exists purely as a type-annotated interface
reference and to let tooling (mypy, pyright) check call-sites against the
intended signatures.

Changes to the real implementation (client.py) should be kept in sync with
this file.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, AsyncIterator, Iterator

DEFAULT_BASE_URL = "https://api.inis.run"


# ── Exceptions ────────────────────────────────────────────────────────────────


class InisError(Exception):
    """Raised when the inis.run API returns an error.

    See client.py's InisError for the real shape: code, status,
    retryable, retry_after, request_id, response. Mirrored here signature-only
    per this file's own "kept in sync" contract above.
    """

    code: str
    status: int | None
    retryable: bool
    retry_after: float | None
    request_id: str | None
    response: dict[str, Any] | None

    def __init__(
        self,
        message: str,
        *,
        code: str = "error",
        status: int | None = None,
        retryable: bool = False,
        retry_after: float | None = None,
        request_id: str | None = None,
        response: dict[str, Any] | None = None,
    ) -> None:
        raise NotImplementedError


# ── Enums ─────────────────────────────────────────────────────────────────────


class OnPtyDetachPolicy(str, Enum):
    """What the server does when the last PTY client detaches.

    Only fires when a PTY terminal disconnects. Has no effect for API-only sessions.

    keep_live — leave the session running until it idles out.
    pause     — pause immediately so it can be resumed (default).
    destroy   — tear the session down.
    """

    KEEP_LIVE = "keep_live"
    PAUSE = "pause"
    DESTROY = "destroy"


class EndReason(str, Enum):
    """Why a session ended (present only on state=ended history rows)."""

    CLIENT_DESTROY = "client_destroy"
    SHELL_EXIT = "shell_exit"
    ON_DETACH_DESTROY = "on_detach_destroy"
    MAX_LIFETIME = "max_lifetime"
    PAUSED_TTL = "paused_ttl"
    ERROR = "error"


class SessionState(str, Enum):
    """Live states the API can return for a session."""

    LIVE = "live"
    PAUSED = "paused"
    CREATING = "creating"
    ENDED = "ended"


class DestroyReason(str, Enum):
    """Caller-supplied reason recorded as end_reason on explicit destroy."""

    CLIENT_DESTROY = "client_destroy"
    SHELL_EXIT = "shell_exit"


# ── Supporting data models ────────────────────────────────────────────────────


@dataclass
class EgressPolicy:
    """Per-session outbound allowlist.

    mode "allow" keeps full public egress; "deny" blocks everything except
    the resolved IPs of the listed domains.
    """

    mode: str  # "allow" | "deny"
    allow: list[str] | None = None


@dataclass
class ArchiveStatus:
    """Durable (cold) object-store copy of a paused session.

    Present only when session archiving is enabled and an upload has started.
    The hot local copy keeps the session resumable regardless of upload status.
    """

    status: str  # "pending" | "complete" | "failed"
    cold_uri: str | None = None
    uploaded_at: str | None = None


@dataclass
class ExposedPreview:
    """One exposed port entry as returned inside SessionInfo."""

    port: int
    preview_url: str
    visibility: str | None = None  # "token" | "public"
    auth: str | None = None  # "none" | "bearer"


@dataclass
class CapacityLimits:
    running: int
    warm: int


@dataclass
class Capacity:
    running: int
    warm: int
    limits: CapacityLimits | None = None


@dataclass
class UsageLabelCost:
    """One bucket of Client.usage(group_by="label:<key>")'s breakdown."""

    label: str
    cost_cents: int


@dataclass
class UsageSummary:
    """Aggregate compute/storage usage and spend for the account, see
    Client.usage()."""

    period_start: str | None = None
    since: str | None = None
    until: str | None = None
    executions: int = 0
    vm_seconds: float = 0.0
    vm_ms: int = 0
    paused_storage_cents: int = 0
    volume_storage_cents: int = 0
    cost_to_date_cents: int = 0
    pending_millicents: int = 0
    compute_pending_millicents: int = 0
    paused_pending_millicents: int = 0
    volume_pending_millicents: int = 0
    balance_cents: int = 0
    labels: list[UsageLabelCost] | None = None


@dataclass
class SessionUsage:
    """One session's lifetime usage/cost, see Session.usage() /
    Client.sessions.usage()."""

    session_id: str
    executions: int = 0
    vm_seconds: float = 0.0
    vm_ms: int = 0
    paused_storage_cents: int = 0
    volume_storage_cents: int = 0
    cost_to_date_cents: int = 0


# ── Primary data models ───────────────────────────────────────────────────────


@dataclass
class ExecResult:
    """Result of a command run inside a session.

    restore_ms is present on both exec and one-shot execute calls; it
    measures host-side wall time spent acquiring a pool slot and restoring (or
    cold-booting) the VM before the code ran. install_ms and phase are only
    populated by the one-shot /v1/sessions/exec endpoint.
    """

    stdout: str
    stderr: str
    exit_code: int
    duration_ms: int
    timed_out: bool
    restore_ms: int | None = None
    install_ms: int | None = None  # one-shot execute only
    phase: str | None = None  # one-shot execute only
    truncated: bool = False
    """True when stdout/stderr are an incomplete view of what the command
    actually produced (the guest's own capture cap cut a large stream)."""
    stdout_encoding: str | None = None
    stderr_encoding: str | None = None
    """None (the default) means plain text. "base64" means that stream's
    output was not valid UTF-8, so the API returned it base64-encoded
    rather than risk silently corrupting it -- decode with
    base64.b64decode(...) to get the exact bytes."""


@dataclass
class ExecStreamEvent:
    """One event from a live-streamed exec (see Session.exec_stream)."""

    stream: str  # "stdout" | "stderr" | "exit" | "error"
    data: str  # decoded text for stdout/stderr/error; empty for exit
    exit_code: int | None = None  # set only when stream=="exit"
    timed_out: bool | None = None  # set only when stream=="exit"
    duration_ms: int | None = None  # set only when stream=="exit"


@dataclass
class InterpreterResult:
    """One typed piece of Session.run_code() output.

    type is "text" (stdout/stderr/last-expression repr), "image" (a
    captured matplotlib figure — format="png", data holds decoded bytes),
    "table" (a pandas DataFrame/Series, capped at 1000 rows), "json" (a
    dict/list/tuple last expression), or "error" (an uncaught exception —
    the interpreter context is untouched and stays usable).
    """

    type: str
    text: str | None = None
    stream: str | None = None
    format: str | None = None
    data: bytes | None = None
    size: int | None = None
    columns: list[str] | None = None
    rows: list[dict[str, Any]] | None = None
    row_count: int | None = None
    truncated: bool | None = None
    json: Any | None = None
    ename: str | None = None
    evalue: str | None = None
    traceback: str | None = None
    path: str | None = None


@dataclass
class SessionInfo:
    """Snapshot of a session returned by create, get, pause, resume, etc."""

    session_id: str
    state: str
    volume_id: str | None = None
    created_at: str | None = None
    name: str | None = None
    labels: dict[str, str] | None = None
    external_id: str | None = None  # caller-owned index key set at create, if any; not unique
    last_active_at: str | None = None
    idle_timeout_ms: int | None = None
    idle_mode: str | None = None  # "ops" | "activity"
    idle_busy_cpu_threshold_pct: int | None = None
    idle_busy_network_bytes_per_sec: int | None = None
    max_lifetime_ms: int | None = None
    exposed_ports: list[int] | None = None
    size: str | None = None
    vcpus: int | None = None
    mem_mb: int | None = None
    template: str | None = None
    no_sudo: bool = False
    wake_on_http: bool = True
    on_pty_detach: str | None = None  # "keep_live" | "pause" | "destroy"
    paused_ttl_ms: int | None = None
    ended_at: str | None = None  # state=ended rows only
    end_reason: str | None = None  # state=ended rows only
    self_api: dict | None = None
    exposed_previews: list[ExposedPreview] | None = None
    egress: EgressPolicy | None = None
    archive: ArchiveStatus | None = None
    node_id: str | None = None
    mcp_url: str | None = None
    active_since: str | None = None
    max_active_window_ms: int | None = None
    snapshot_tier: str | None = None  # "hot" | "warm" | "cold"; paused sessions only
    env: dict[str, str] | None = None
    secret_names: list[str] | None = None
    connector_names: list[str] | None = None
    connections: list[ConnectionStatus] | None = None  # bound Connections, redacted


@dataclass
class ExposeResult:
    """Response from exposing a guest port as a preview URL."""

    session_id: str
    port: int
    preview_url: str
    ingress_token: str | None = None
    guest_ip: str | None = None
    visibility: str | None = None  # "token" | "public"
    auth: str | None = None  # active inbound access mode: "none" | "bearer"
    auth_token: str | None = None  # returned once when auth="bearer"


@dataclass
class ForkResult:
    parent_session_id: str
    children: list[str]


@dataclass
class CheckpointInfo:
    checkpoint_id: str
    session_id: str | None = None
    parent_session_id: str | None = None
    name: str | None = None
    labels: dict[str, str] | None = None
    size_bytes: int = 0
    created_at: str | None = None
    node_id: str | None = None


@dataclass
class TemplateInfo:
    """A base environment a session can be created from."""

    name: str
    kind: str | None = None  # "official" | "user"
    description: str | None = None
    size: str | None = None
    created_at: str | None = None
    version: str | None = None  # promoted current version
    versions: list[str] | None = None  # available pinnable versions (name@version)
    status: str | None = None  # "queued" | "building" | "ready" | "failed" (BYO-image import)
    source_image: str | None = None  # OCI ref imported from, if any
    build_error: str | None = None  # reason when status == "failed"
    queue_position: int | None = None  # 1-based build-queue place, only while "queued"
    built_at: str | None = None  # official templates only
    base_image_ref: str | None = None  # official templates only
    base_image_digest: str | None = None  # official templates only
    content_hash: str | None = None  # official templates only
    cadence: str | None = None  # "weekly" | "monthly"; official templates only
    category: str | None = None  # "language" | "use-case"; official templates only
    alias_of: str | None = None  # set only on an alias entry, e.g. "python-3.13"


@dataclass
class RegistryCredentialInfo:
    """A stored private-registry pull credential, redacted — the secret is
    never included, on create, list, or any other response."""

    id: str
    name: str
    registry_host: str
    username: str
    secret_last4: str | None = None
    created_at: str | None = None
    updated_at: str | None = None


@dataclass
class ConnectorInfo:
    """A stored egress connector, redacted — the secret is never included,
    on create, list, or any other response."""

    id: str
    name: str
    target_base_url: str
    auth_shape: str
    header_name: str | None = None
    secret_last4: str | None = None
    created_at: str | None = None
    updated_at: str | None = None


@dataclass
class ConnectionAllow:
    """HTTP methods and path prefixes a Connection's credential may be
    used for. Deny-by-default -- an empty list authorizes nothing."""

    methods: list[str]
    paths: list[str]


@dataclass
class ConnectionSpec:
    """One Connection to create with a session: binds the session to a
    single external API origin and injects `secret` into outbound
    requests that match `methods`/`paths`. Used for the `connections`
    list on session create and for Session.rotate_connection.

    The credential is never written into your code, image, or the
    session's env/secrets -- it's injected by the platform only at the
    moment a matching request leaves. This is NOT a scrubbing boundary,
    though: code running in the session can still read the credential
    back out if the destination happens to reflect request headers.
    Prefer a scoped, short-lived upstream credential so a value that
    does leak can't do more than `methods`/`paths` already allowed.
    """

    name: str
    origin: str
    secret: str
    methods: list[str]
    paths: list[str]
    auth_type: str = "bearer"  # "bearer" (Authorization: Bearer <secret>) or "header"
    header_name: str | None = None  # required (and only meaningful) when auth_type == "header"
    ttl_seconds: int | None = None  # defaults to the session's lifetime, or 1h; capped at 24h


@dataclass
class ConnectionStatus:
    """A Connection bound to a session, redacted -- the secret is never
    included, on create, get, rotate, or any other response."""

    name: str
    origin: str
    state: str  # "active" | "expired" | "revoked"
    header_name: str | None = None
    allow: ConnectionAllow | None = None
    expires_at: str | None = None
    created_at: str | None = None


@dataclass
class WebhookEndpointInfo:
    """A registered webhook subscription. ``secret`` is populated ONLY
    on the response from ``webhooks.add`` — store it immediately, it is
    never returned again (``secret_last4`` is what ``list()`` shows)."""

    id: str
    url: str
    event_types: list[str]
    enabled: bool
    created_at: str | None = None
    disabled_at: str | None = None
    secret: str | None = None
    secret_last4: str | None = None


@dataclass
class WebhookDeliveryInfo:
    """One logged delivery attempt series — the per-subscription delivery
    log entry."""

    id: int
    event_type: str
    status: str  # pending | delivered | dead
    attempt_count: int
    max_attempts: int
    session_id: str | None = None
    attempts: list[dict[str, Any]] | None = None
    created_at: str | None = None
    delivered_at: str | None = None
    next_attempt_at: str | None = None  # set only while status == "pending"


@dataclass
class DomainInfo:
    """A registered custom domain: CNAME a customer-owned hostname to
    the fleet's ingress, verify DNS ownership, then route it to an exposed
    session port for auto-provisioned TLS in place of the default preview
    URL."""

    id: str
    domain: str
    verified: bool
    verify_txt_name: str
    verify_txt_value: str
    created_at: str | None = None
    verified_at: str | None = None
    session_id: str | None = None
    port: int | None = None
    ingress_base_domain: str | None = None  # CNAME target; None when no ingress domain is set


@dataclass
class VolumeInfo:
    """A persistent volume: a durable disk that outlives whatever
    session or one-shot run mounts it. Deliberately node-agnostic -- no
    node id or placement hint anywhere in this shape."""

    id: str
    size_gb: int
    created_at: str
    attached: bool
    session_id: str | None = None  # set only when attached is True
    last_saved_at: str | None = None  # None if the volume has never completed a push


@dataclass
class ArtifactFile:
    path: str
    url: str | None = None  # pre-signed direct-download URL
    size_bytes: int = 0
    content_type: str | None = None


@dataclass
class ArtifactInfo:
    id: str
    status: str  # "pending" | "ready" | "failed"
    session_id: str | None = None
    captured_at: str | None = None
    expires_at: str | None = None
    files: list[ArtifactFile] | None = None
    error: str | None = None


@dataclass
class BatchExecResult:
    session_id: str
    stdout: str = ""
    stderr: str = ""
    exit_code: int = 0
    duration_ms: int = 0
    timed_out: bool = False
    error: str | None = None
    stdout_encoding: str | None = None
    stderr_encoding: str | None = None
    """None (the default) means plain text. "base64" means that session's
    output on this stream was not valid UTF-8, so the API returned it
    base64-encoded rather than risk silently corrupting it -- decode with
    base64.b64decode(...) to get the exact bytes."""


# ── Session ───────────────────────────────────────────────────────────────────


class Session:
    """Context manager for a live sandbox session.

    Typical usage::

        with client.session(template="python") as s:
            result = s.exec("python -c 'print(42)'")

    The session is created on ``__enter__`` and destroyed on ``__exit__``.
    Use ``Session.attach()`` to bind to an already-running session without
    creating a new one.
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
        template: str | None = None,
        size: str | None = None,
        no_sudo: bool = False,
        wake_on_http: bool = True,
        on_pty_detach: str | OnPtyDetachPolicy | None = None,
        paused_ttl_ms: int | None = None,
        env: dict[str, str] | None = None,
        secrets: dict[str, str] | None = None,
        connectors: list[str] | None = None,
        connections: list[ConnectionSpec | dict[str, Any]] | None = None,
        timeout: float = 120.0,
        session_id: str | None = None,
    ) -> None:
        raise NotImplementedError

    @classmethod
    def attach(
        cls,
        session_id: str,
        *,
        base_url: str | None = None,
        token: str | None = None,
        timeout: float = 120.0,
    ) -> "Session":
        """Bind to an existing session without creating a new one.

        base_url and token default to the same env resolution as Client
        (INIS_BASE_URL / INIS_API_KEY) when omitted. Prefer
        client.sessions.attach(session_id) when you already have a Client.
        """
        raise NotImplementedError

    # ── Context manager ───────────────────────────────────────────────────────

    def __enter__(self) -> "Session":
        raise NotImplementedError

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        raise NotImplementedError

    # ── Session state ─────────────────────────────────────────────────────────

    def get(self) -> SessionInfo:
        """Fetch the current state of this session."""
        raise NotImplementedError

    def usage(self) -> SessionUsage:
        """Fetch this session's lifetime usage/cost."""
        raise NotImplementedError

    def pause(self) -> SessionInfo:
        """Pause this session. The VM is snapshotted and its resources released."""
        raise NotImplementedError

    def resume(self) -> SessionInfo:
        """Resume this paused session. The VM is restored from its snapshot."""
        raise NotImplementedError

    def destroy(self, *, reason: str | DestroyReason | None = None) -> None:
        """Destroy this session and free all its resources.

        reason is recorded as end_reason on the retained history row.
        Defaults to client_destroy; use shell_exit for interactive terminal
        teardowns.
        """
        raise NotImplementedError

    # ── Exec ──────────────────────────────────────────────────────────────────

    def exec(
        self,
        command: str | list[str],
        *,
        cwd: str | None = None,
        timeout_ms: int | None = None,
    ) -> ExecResult:
        """Run a command inside this session.

        If command is a str it is run as ``bash -lc <command>``.
        """
        raise NotImplementedError

    def exec_stream(
        self,
        command: str | list[str],
        *,
        cwd: str | None = None,
        timeout_ms: int | None = None,
    ) -> Iterator[ExecStreamEvent]:
        """Run a command inside this session, streaming stdout/stderr live via
        Server-Sent Events instead of waiting for it to finish.

        Yields an ExecStreamEvent per chunk as the guest produces it, ending
        with exactly one terminal event: stream="exit" (exit_code/timed_out/
        duration_ms populated) once the command completes or its timeout
        fires, or stream="error" on a guest-side failure. Breaking out of the
        loop early closes the underlying HTTP connection, which the server
        takes as a disconnect and kills the command.
        """
        raise NotImplementedError

    # ── Code interpreter ────────────────────────────────────────────────────────

    def run_code(
        self, code: str, *, timeout_ms: int | None = None
    ) -> list[InterpreterResult]:
        """Run code in this session's persistent Python interpreter context.

        Unlike exec(), variables/imports/functions/classes survive across
        calls until restart_context() or the session ends. Built entirely on
        exec/write_file/read_file/start_process (no guest-agent or template
        changes) — see docs/api-design/python-sdk.md#code-interpreter.
        """
        raise NotImplementedError

    def restart_context(self) -> None:
        """Discard this session's interpreter context: kills the running
        kernel process (if any) and starts a fresh one."""
        raise NotImplementedError

    # ── Files ─────────────────────────────────────────────────────────────────

    def read_file(self, path: str) -> str:
        """Read a file from the session filesystem and return its content.

        GET /v1/sessions/{id}/files?path=<path>&op=read
        """
        raise NotImplementedError

    def list_files(self, path: str) -> list[str]:
        """List entries under path in the session filesystem.

        GET /v1/sessions/{id}/files?path=<path>&op=list
        """
        raise NotImplementedError

    def write_file(self, path: str, content: str, encoding: str = "text") -> None:
        """Write content to a file in the session filesystem.

        PUT /v1/sessions/{id}/files
        """
        raise NotImplementedError

    def mkdir(self, path: str) -> None:
        """Create a directory (with parents) in the session filesystem.

        POST /v1/sessions/{id}/files/mkdir
        """
        raise NotImplementedError

    def remove(self, path: str, *, recursive: bool = False) -> None:
        """Remove a file, or a directory (with recursive=True).

        DELETE /v1/sessions/{id}/files?path=<path>&recursive=<bool>
        """
        raise NotImplementedError

    def rename(self, path: str, dest_path: str) -> None:
        """Rename or move a file or directory.

        POST /v1/sessions/{id}/files/rename
        """
        raise NotImplementedError

    def upload_file(self, local_path: str | os.PathLike, remote_path: str) -> dict[str, Any]:
        """Stream a local file into the session filesystem — not subject to
        write_files' 64-file/8 MiB batch cap.

        PUT /v1/sessions/{id}/files/stream
        """
        raise NotImplementedError

    def download_file(self, remote_path: str, local_path: str | os.PathLike) -> dict[str, Any]:
        """Stream a file from the session filesystem to local disk — not
        subject to read_file's 4 MiB whole-file cap.

        GET /v1/sessions/{id}/files/stream
        """
        raise NotImplementedError

    # ── Fork / checkpoint ─────────────────────────────────────────────────────

    def fork(self, count: int = 1) -> ForkResult:
        """Fork this session into count independent children.

        The parent session keeps running. Each child diverges independently.
        """
        raise NotImplementedError

    def checkpoint(
        self,
        *,
        name: str | None = None,
        labels: dict[str, str] | None = None,
    ) -> CheckpointInfo:
        """Capture a named, retained checkpoint of this live session.

        The session keeps running. The checkpoint survives this session's
        destruction and can be restored in-place or used as a template.
        """
        raise NotImplementedError

    def checkpoints(self) -> list[CheckpointInfo]:
        """List all checkpoints captured from this session."""
        raise NotImplementedError

    def restore(self, checkpoint_id: str) -> SessionInfo:
        """Roll this session back to a checkpoint in place.

        Stops the current VM and restores from the checkpoint, rebinding
        networking. The "run untrusted, then roll back to clean" flow.
        """
        raise NotImplementedError

    def save_as_template(
        self,
        name: str,
        *,
        description: str | None = None,
    ) -> TemplateInfo:
        """Promote this live session into a named, reusable template.

        The session keeps running. The template becomes a starting environment
        new sessions can launch from via ``template=`` on create.
        """
        raise NotImplementedError

    # ── Archive ───────────────────────────────────────────────────────────────

    def archive_retry(self) -> ArchiveStatus:
        """Re-drive a stuck or failed durable upload for this paused session.

        The session must be paused and its hot copy still present. The upload
        runs in the background; this returns the now-pending archive status
        immediately.
        """
        raise NotImplementedError

    # ── Ports ─────────────────────────────────────────────────────────────────

    def expose(
        self,
        port: int,
        *,
        visibility: str | None = None,
        auth: str | None = None,
    ) -> ExposeResult:
        """Expose a guest port as a Caddy-proxied preview URL.

        visibility: "token" (default, token embedded in URL) or "public".
        auth: "none" (open) or "bearer" (caller must supply a Bearer token).
        """
        raise NotImplementedError

    def unexpose(self, port: int) -> bool:
        """Remove a previously exposed port."""
        raise NotImplementedError

    # ── Egress ────────────────────────────────────────────────────────────────

    def get_egress(self) -> dict[str, Any]:
        """Read the active egress policy for this session."""
        raise NotImplementedError

    def set_egress(
        self,
        *,
        mode: str = "deny",
        allow: list[str] | None = None,
    ) -> dict[str, Any]:
        """Replace the egress policy on this live session.

        mode: "allow" (full egress) or "deny" (allowlist only).
        allow: domains reachable in deny mode (exact or "*.wildcard").
        """
        raise NotImplementedError

    # ── Connections ───────────────────────────────────────────────────────────
    # TODO: list_connections() / add_connection() for a Connection added to an
    # already-live session (GET and POST .../connections) land here once the
    # server verbs ship.

    def rotate_connection(
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
        """Replace Connection `name` on this live or paused session --
        a full replacement of its origin/secret/allow rules/TTL, not a
        partial patch. See ConnectionSpec for what `secret` protects
        against (and does not).
        """
        raise NotImplementedError

    def revoke_connection(self, name: str) -> None:
        """Revoke Connection `name` immediately. Fails closed (a
        subsequent request to that origin gets a TLS-level failure, not
        an unauthenticated passthrough). Idempotent."""
        raise NotImplementedError

    # ── Artifacts ─────────────────────────────────────────────────────────────

    def capture_artifacts(
        self,
        paths: list[str],
        *,
        destination: dict[str, Any] | None = None,
    ) -> ArtifactInfo:
        """Capture output files from this session to durable storage.

        paths are scoped to /workspace (e.g. "/workspace/output/**"). Paths
        outside the workspace are rejected. Returns a pending manifest
        immediately; the upload runs asynchronously. Poll
        ``client.artifacts.get(artifact.id)`` until status is "ready".
        """
        raise NotImplementedError

    def artifacts(self) -> list[ArtifactInfo]:
        """List artifact captures for this session."""
        raise NotImplementedError


# ── _SessionsAPI ──────────────────────────────────────────────────────────────


class _SessionsAPI:
    """Session management. Accessed as ``client.sessions``."""

    def __init__(self, client: "Client") -> None:
        raise NotImplementedError

    def create(
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
        template: str | None = None,
        size: str | None = None,
        no_sudo: bool = False,
        wake_on_http: bool = True,
        on_pty_detach: str | OnPtyDetachPolicy | None = None,
        paused_ttl_ms: int | None = None,
        env: dict[str, str] | None = None,
        secrets: dict[str, str] | None = None,
        connectors: list[str] | None = None,
        connections: list[ConnectionSpec | dict[str, Any]] | None = None,
    ) -> Session:
        """Create and start a new session.

        external_id is an optional caller-owned index key (a user/workflow/
        thread id) for later lookup via list(external_id=...); not unique.

        idempotency_key makes this create retry-safe: a repeat call with the
        same idempotency_key returns the ORIGINAL session instead of
        creating a second one. Reusing a key with different create arguments
        is an error (409 conflict), not a silent replay.

        The returned session is unmanaged — the caller is responsible for
        calling ``session.destroy()`` (or using it as a context manager) to
        release the VM.
        """
        raise NotImplementedError

    def get(self, session_id: str) -> SessionInfo:
        """Fetch state for a session by ID without attaching to it."""
        raise NotImplementedError

    def usage(self, session_id: str) -> SessionUsage:
        """Fetch lifetime usage/cost for a session by ID without attaching to
        it."""
        raise NotImplementedError

    def attach(self, session_id: str) -> Session:
        """Bind to an existing session using this client's base_url/token."""
        raise NotImplementedError

    def list(
        self,
        *,
        state: str | SessionState | None = None,
        limit: int | None = None,
        cursor: str | None = None,
        external_id: str | None = None,
    ) -> tuple[list[SessionInfo], str | None]:
        """List sessions, optionally filtered by state and/or external_id.

        Use state="ended" to retrieve retained history rows for destroyed
        sessions (each carries ended_at and end_reason). external_id is not
        unique -- it may match more than one session.

        Returns a (sessions, next_cursor) tuple. next_cursor is None when
        there are no more pages.
        """
        raise NotImplementedError

    def read_file(self, session_id: str, path: str) -> str:
        """Read a file from a session filesystem by session ID.

        GET /v1/sessions/{id}/files?path=<path>&op=read
        """
        raise NotImplementedError

    def list_files(self, session_id: str, path: str) -> list[str]:
        """List entries under path in a session filesystem by session ID.

        GET /v1/sessions/{id}/files?path=<path>&op=list
        """
        raise NotImplementedError

    def write_file(
        self, session_id: str, path: str, content: str, encoding: str = "text"
    ) -> None:
        """Write content to a file in a session filesystem by session ID.

        PUT /v1/sessions/{id}/files
        """
        raise NotImplementedError

    def batch_exec(
        self,
        session_ids: list[str],
        command: str | list[str],
        *,
        cwd: str | None = None,
        timeout_ms: int | None = None,
        timeout: float = 120.0,
    ) -> list[BatchExecResult]:
        """Fan the same command across multiple sessions in parallel."""
        raise NotImplementedError


# ── _CheckpointsAPI ───────────────────────────────────────────────────────────


class _CheckpointsAPI:
    """Checkpoint management. Accessed as ``client.checkpoints``."""

    def __init__(self, client: "Client") -> None:
        raise NotImplementedError

    def get(self, checkpoint_id: str) -> CheckpointInfo:
        """Fetch checkpoint metadata."""
        raise NotImplementedError

    def delete(self, checkpoint_id: str) -> None:
        """Delete a checkpoint and free its disk. Deletion is explicit."""
        raise NotImplementedError

    def create_session(
        self,
        checkpoint_id: str,
        *,
        name: str | None = None,
        labels: dict[str, str] | None = None,
        max_lifetime_ms: int | None = None,
        idle_timeout_ms: int | None = None,
    ) -> Session:
        """Create a new, independent session from a checkpoint.

        The new session diverges independently — this is the template path.
        """
        raise NotImplementedError


# ── _ArtifactsAPI ─────────────────────────────────────────────────────────────


class _ArtifactsAPI:
    """Artifact management. Accessed as ``client.artifacts``."""

    def __init__(self, client: "Client") -> None:
        raise NotImplementedError

    def get(self, artifact_id: str) -> ArtifactInfo:
        """Get an artifact manifest (status + pre-signed download URLs)."""
        raise NotImplementedError

    def extend(self, artifact_id: str, ttl_days: int) -> ArtifactInfo:
        """Push an artifact's expiry forward (capped at the retention limit)."""
        raise NotImplementedError

    def delete(self, artifact_id: str) -> None:
        """Delete an artifact's stored files and its record."""
        raise NotImplementedError


# ── _TemplatesAPI ─────────────────────────────────────────────────────────────


class _TemplatesAPI:
    """Template management. Accessed as ``client.templates``."""

    def __init__(self, client: "Client") -> None:
        raise NotImplementedError

    def list(self) -> list[TemplateInfo]:
        """List the templates available to this account (official and user)."""
        raise NotImplementedError

    def import_image(
        self,
        from_image: str,
        name: str,
        description: str | None = None,
    ) -> TemplateInfo:
        """Import a PUBLIC OCI image as a custom template.

        The image is pulled, flattened into the VM rootfs, and given the inis
        guest agent. The build runs asynchronously: the returned template starts
        in ``status="building"`` and becomes usable once it flips to ``"ready"``
        (poll :meth:`list`). Public registries only.
        """
        raise NotImplementedError

    def delete(self, name: str) -> None:
        """Delete a user template. Official templates cannot be deleted."""
        raise NotImplementedError


# ── _RegistriesAPI ────────────────────────────────────────────────────────────


class _RegistriesAPI:
    """Private-registry pull credentials for template import. Accessed as
    ``client.registries``."""

    def __init__(self, client: "Client") -> None:
        raise NotImplementedError

    def add(
        self,
        name: str,
        *,
        registry_host: str,
        username: str,
        secret: str,
    ) -> RegistryCredentialInfo:
        """Store a private-registry pull credential under ``name``.

        Covers Docker Hub, GHCR, and Google Artifact Registry. ``secret`` is
        encrypted at rest and never returned by any read. Rotation is
        delete-then-create for v1.
        """
        raise NotImplementedError

    def list(self) -> list[RegistryCredentialInfo]:
        """List your stored registry credentials (redacted, never the secret)."""
        raise NotImplementedError

    def delete(self, name: str) -> None:
        """Delete a stored registry credential. Idempotent."""
        raise NotImplementedError


class _ConnectorsAPI:
    """Egress connectors: credential injection without the sandbox
    ever seeing the secret. Accessed as ``client.connectors``."""

    def __init__(self, client: "Client") -> None:
        raise NotImplementedError

    def add(
        self,
        name: str,
        *,
        target_base_url: str,
        auth_shape: str = "bearer",
        header_name: str | None = None,
        secret: str,
    ) -> ConnectorInfo:
        """Register a connector under ``name``.

        A session opts in by name at create time
        (``client.sessions.create(connectors=["stripe"])``); from inside
        the guest it calls
        ``http://<its own default gateway>:18080/<name>/<path>`` and the
        host-side proxy injects the credential. ``secret`` is encrypted at
        rest and never returned by any read. Rotation is delete-then-create
        for v1.
        """
        raise NotImplementedError

    def list(self) -> list[ConnectorInfo]:
        """List your stored connectors (redacted, never the secret)."""
        raise NotImplementedError

    def delete(self, name: str) -> None:
        """Delete a stored connector. Idempotent."""
        raise NotImplementedError


class _WebhooksAPI:
    """Webhook subscriptions for session events. Accessed as
    ``client.webhooks``.

    A subscription receives the same event envelope the account-scoped
    event stream (``GET /v1/events``) sends, signed with HMAC-SHA256
    (``Inis-Signature: t=<ts>,v1=<hex>``) and retried with bounded
    exponential backoff on non-2xx/failure.
    """

    def __init__(self, client: "Client") -> None:
        raise NotImplementedError

    def add(self, url: str, *, event_types: list[str] | None = None) -> WebhookEndpointInfo:
        """Register ``url`` to receive signed event deliveries.

        ``event_types`` is an allowlist; omit/empty means every type. The
        returned object's ``secret`` is the ONLY time the raw signing
        secret is available.
        """
        raise NotImplementedError

    def list(self) -> list[WebhookEndpointInfo]:
        """List this org's webhook subscriptions (secret never included)."""
        raise NotImplementedError

    def delete(self, endpoint_id: str) -> None:
        """Delete a webhook subscription."""
        raise NotImplementedError

    def test(self, endpoint_id: str) -> WebhookDeliveryInfo:
        """Fire one synthetic test delivery immediately and return the
        delivered/dead result synchronously."""
        raise NotImplementedError

    def deliveries(self, endpoint_id: str, *, limit: int = 50) -> list[WebhookDeliveryInfo]:
        """The most recent deliveries for a subscription, newest first."""
        raise NotImplementedError


class _DomainsAPI:
    """Custom domains with auto-TLS for exposed session ports.
    Accessed as ``client.domains``.

    v1 binds a domain directly to a (session_id, port) pair, the same
    ephemeral unit preview URLs already use.
    """

    def __init__(self, client: "Client") -> None:
        raise NotImplementedError

    def add(self, domain: str) -> DomainInfo:
        """Register ``domain`` as a pending (unverified) custom domain."""
        raise NotImplementedError

    def list(self) -> list[DomainInfo]:
        """List this org's registered domains, verified and unverified."""
        raise NotImplementedError

    def delete(self, domain_id: str) -> None:
        """Delete a registered custom domain."""
        raise NotImplementedError

    def verify(self, domain_id: str) -> DomainInfo:
        """Run a live DNS check (TXT or CNAME) and mark the domain verified
        on success. Safe to call repeatedly."""
        raise NotImplementedError

    def route(self, domain_id: str, session_id: str, port: int) -> DomainInfo:
        """Bind a verified domain to ``session_id``'s exposed ``port``."""
        raise NotImplementedError

    def unroute(self, domain_id: str) -> DomainInfo:
        """Clear a domain's session/port binding without deleting the
        registration."""
        raise NotImplementedError


class _VolumesAPI:
    """Persistent volumes: standalone CRUD for a durable disk that
    outlives whatever session or one-shot run mounts it. Accessed as
    ``client.volumes``. Lazy creation is retired -- create a volume here
    before attaching its id as ``volume_id`` elsewhere; an unknown or
    cross-tenant id there raises InisError (404)."""

    def __init__(self, client: "Client") -> None:
        raise NotImplementedError

    def create(self, *, size_gb: int | None = None) -> VolumeInfo:
        """Create a new, empty persistent volume. Omitted size_gb applies
        the server default; a value over the maximum raises InisError
        (400) rather than being clamped."""
        raise NotImplementedError

    def list(self) -> list[VolumeInfo]:
        """List every persistent volume the caller owns."""
        raise NotImplementedError

    def get(self, volume_id: str) -> VolumeInfo:
        """Get one volume's size, creation time, and attachment status."""
        raise NotImplementedError

    def resize(self, volume_id: str, size_gb: int) -> VolumeInfo:
        """Grow a volume's capacity (grow-only). Raises InisError (400) for
        a same-size or smaller request; (409) while attached to a live or
        paused session, or while an attach/detach is in progress."""
        raise NotImplementedError

    def delete(self, volume_id: str) -> None:
        """Permanently delete a persistent volume. Raises InisError (409)
        while attached to a live or paused session."""
        raise NotImplementedError


# ── Client ────────────────────────────────────────────────────────────────────


class Client:
    """Primary SDK entry point.

    Reads INIS_API_KEY from the environment when token is not supplied.
    Reads INIS_BASE_URL from the environment when base_url is not supplied.

    Usage::

        with inis.Client() as client:
            with client.session(template="python") as s:
                result = s.exec("python --version")
    """

    sessions: _SessionsAPI
    checkpoints: _CheckpointsAPI
    artifacts: _ArtifactsAPI
    templates: _TemplatesAPI
    registries: _RegistriesAPI
    connectors: _ConnectorsAPI
    webhooks: _WebhooksAPI
    domains: _DomainsAPI
    volumes: _VolumesAPI

    def __init__(
        self,
        *,
        token: str | None = None,
        base_url: str | None = None,
        timeout: float = 120.0,
    ) -> None:
        raise NotImplementedError

    def close(self) -> None:
        """Close the underlying HTTP connection pool."""
        raise NotImplementedError

    def __enter__(self) -> "Client":
        raise NotImplementedError

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        raise NotImplementedError

    @property
    def base_url(self) -> str:
        raise NotImplementedError

    @property
    def token(self) -> str:
        raise NotImplementedError

    @property
    def timeout(self) -> float:
        raise NotImplementedError

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
        template: str | None = None,
        size: str | None = None,
        no_sudo: bool = False,
        wake_on_http: bool = True,
        on_pty_detach: str | OnPtyDetachPolicy | None = None,
        paused_ttl_ms: int | None = None,
        env: dict[str, str] | None = None,
        secrets: dict[str, str] | None = None,
        connectors: list[str] | None = None,
        connections: list[ConnectionSpec | dict[str, Any]] | None = None,
    ) -> Session:
        """Return a Session configured for use as a context manager.

        The session is not started until ``__enter__`` is called (i.e. when
        entering a ``with`` block). For an already-started unmanaged session
        use ``client.sessions.create()``.
        """
        raise NotImplementedError

    def execute(
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
    ) -> ExecResult:
        """One-shot language execution in a throwaway session.

        Creates a session, runs the code, returns the result inline, and
        destroys the session — all in a single API call. install_ms, restore_ms,
        and phase are populated in the returned ExecResult.
        """
        raise NotImplementedError

    def capacity(self) -> Capacity:
        """Return aggregate session capacity and limits for this account."""
        raise NotImplementedError

    def usage(
        self,
        *,
        since: str | None = None,
        until: str | None = None,
        group_by: str | None = None,
    ) -> UsageSummary:
        """Aggregate compute/storage usage and spend for the account.

        since/until (RFC3339) narrow the window; both default to the current
        calendar month to now when omitted. group_by="label:<key>"
        additionally breaks cost_to_date_cents down by the value of each
        session's labels[key].
        """
        raise NotImplementedError


# ── Async surface ───────────────────────────────────────────────────────────────
#
# AsyncClient / AsyncSession mirror Client / Session one-for-one on
# httpx.AsyncClient: every I/O method becomes a coroutine, and the two SSE
# streaming methods (exec_stream, stream_process_logs) become async
# generators. They share the same request/response dataclasses and error
# types defined above them in this file — no separate async models.
#
# This section reflects the FULL surface implemented in inis/async_client.py
# (processes, write_files/find_files/grep_files, egress get/set, etc.) — a
# superset of the sync Session/Client stub above, which predates several of
# those methods landing in client.py. Reconciling that gap in the sync stub
# is tracked separately; this section is complete on its own.


class ProcessInfo:
    """A named background process, running or exited, in a session."""

    name: str
    state: str  # "running" | "exited"
    pid: int | None
    command: list[str] | None
    exit_code: int | None
    keep_alive: bool
    started_at: str | None
    ended_at: str | None


class ProcessLogs:
    """Buffered stdout/stderr captured so far for a named process."""

    name: str
    stdout: str
    stderr: str
    truncated: bool = False
    stdout_encoding: str | None = None
    stderr_encoding: str | None = None


class ProcessLogEvent:
    """One event from a live-followed process log stream."""

    event: str  # "stdout" | "stderr" | "eof" | "error"
    data: str


class AsyncSession:
    """Async context manager for a live sandbox session (httpx.AsyncClient).

    Full async twin of Session — same fields, same request payloads, same
    response mapping; every method is a coroutine (or, for exec_stream /
    stream_process_logs, an async generator) instead of a blocking call.
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
        env: dict[str, str] | None = None,
        secrets: dict[str, str] | None = None,
        connectors: list[str] | None = None,
        connections: list[ConnectionSpec | dict[str, Any]] | None = None,
        timeout: float = 120.0,
        session_id: str | None = None,
    ) -> None:
        raise NotImplementedError

    @classmethod
    def attach(
        cls,
        session_id: str,
        *,
        base_url: str | None = None,
        token: str | None = None,
        timeout: float = 120.0,
    ) -> "AsyncSession":
        """Bind to an existing session without creating a new one."""
        raise NotImplementedError

    async def __aenter__(self) -> "AsyncSession":
        raise NotImplementedError

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        raise NotImplementedError

    async def get(self) -> SessionInfo:
        raise NotImplementedError

    async def usage(self) -> SessionUsage:
        raise NotImplementedError

    async def pause(self) -> SessionInfo:
        raise NotImplementedError

    async def resume(self) -> SessionInfo:
        raise NotImplementedError

    async def destroy(self, *, reason: str | DestroyReason | None = None) -> None:
        raise NotImplementedError

    async def exec(
        self,
        command: str | list[str],
        *,
        cwd: str | None = None,
        timeout_ms: int | None = None,
    ) -> ExecResult:
        raise NotImplementedError

    def exec_stream(
        self,
        command: str | list[str],
        *,
        cwd: str | None = None,
        timeout_ms: int | None = None,
    ) -> AsyncIterator[ExecStreamEvent]:
        """Streaming twin of exec() — an async generator of ExecStreamEvent."""
        raise NotImplementedError

    # ── Code interpreter ────────────────────────────────────────────────────────

    async def run_code(
        self, code: str, *, timeout_ms: int | None = None
    ) -> list[InterpreterResult]:
        """Async twin of Session.run_code() — same protocol and result types."""
        raise NotImplementedError

    async def restart_context(self) -> None:
        """Async twin of Session.restart_context()."""
        raise NotImplementedError

    async def start_process(
        self,
        name: str,
        command: str | list[str],
        *,
        cwd: str | None = None,
        keep_alive: bool = False,
    ) -> ProcessInfo:
        raise NotImplementedError

    async def list_processes(self) -> list[ProcessInfo]:
        raise NotImplementedError

    async def get_process(self, name: str) -> ProcessInfo:
        raise NotImplementedError

    async def kill_process(self, name: str) -> ProcessInfo:
        raise NotImplementedError

    async def get_process_logs(self, name: str) -> ProcessLogs:
        raise NotImplementedError

    def stream_process_logs(self, name: str) -> AsyncIterator[ProcessLogEvent]:
        """Async generator following a named process's stdout/stderr live."""
        raise NotImplementedError

    async def read_file(self, path: str, *, encoding: str = "text") -> str | bytes:
        raise NotImplementedError

    async def list_files(self, path: str = "/workspace") -> list[str]:
        raise NotImplementedError

    async def write_file(self, path: str, content: str | bytes, encoding: str = "text") -> None:
        raise NotImplementedError

    async def write_files(self, files: list[dict[str, Any]]) -> list[dict[str, Any]]:
        raise NotImplementedError

    async def find_files(
        self,
        pattern: str,
        *,
        path: str = "/workspace",
        max_results: int | None = None,
    ) -> dict[str, Any]:
        raise NotImplementedError

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
        raise NotImplementedError

    async def mkdir(self, path: str) -> None:
        """Create a directory (with parents) in the session filesystem."""
        raise NotImplementedError

    async def remove(self, path: str, *, recursive: bool = False) -> None:
        """Remove a file, or a directory (with recursive=True)."""
        raise NotImplementedError

    async def rename(self, path: str, dest_path: str) -> None:
        """Rename or move a file or directory."""
        raise NotImplementedError

    async def upload_file(self, local_path: str | os.PathLike, remote_path: str) -> dict[str, Any]:
        """Stream a local file into the session filesystem — not subject to
        write_files' 64-file/8 MiB batch cap."""
        raise NotImplementedError

    async def download_file(self, remote_path: str, local_path: str | os.PathLike) -> dict[str, Any]:
        """Stream a file from the session filesystem to local disk — not
        subject to read_file's 4 MiB whole-file cap."""
        raise NotImplementedError

    async def fork(self, count: int = 1) -> ForkResult:
        raise NotImplementedError

    async def checkpoint(
        self,
        *,
        name: str | None = None,
        labels: dict[str, str] | None = None,
    ) -> CheckpointInfo:
        raise NotImplementedError

    async def checkpoints(self) -> list[CheckpointInfo]:
        raise NotImplementedError

    async def restore(self, checkpoint_id: str) -> SessionInfo:
        raise NotImplementedError

    async def save_as_template(
        self,
        name: str,
        *,
        description: str | None = None,
    ) -> TemplateInfo:
        raise NotImplementedError

    async def archive_retry(self) -> ArchiveStatus:
        raise NotImplementedError

    async def expose(
        self,
        port: int,
        *,
        visibility: str | None = None,
        auth: str | None = None,
    ) -> ExposeResult:
        raise NotImplementedError

    async def unexpose(self, port: int) -> bool:
        raise NotImplementedError

    async def get_egress(self) -> dict[str, Any]:
        raise NotImplementedError

    async def set_egress(
        self,
        *,
        mode: str = "deny",
        allow: list[str] | None = None,
    ) -> dict[str, Any]:
        raise NotImplementedError

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
        raise NotImplementedError

    async def revoke_connection(self, name: str) -> None:
        raise NotImplementedError

    async def capture_artifacts(
        self,
        paths: list[str],
        *,
        destination: dict[str, Any] | None = None,
    ) -> ArtifactInfo:
        raise NotImplementedError

    async def artifacts(self) -> list[ArtifactInfo]:
        raise NotImplementedError

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
        raise NotImplementedError


class _AsyncSessionsAPI:
    """Session management. Accessed as ``client.sessions``."""

    def __init__(self, client: "AsyncClient") -> None:
        raise NotImplementedError

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
        env: dict[str, str] | None = None,
        secrets: dict[str, str] | None = None,
        connectors: list[str] | None = None,
        connections: list[ConnectionSpec | dict[str, Any]] | None = None,
    ) -> AsyncSession:
        raise NotImplementedError

    async def get(self, session_id: str) -> SessionInfo:
        raise NotImplementedError

    async def usage(self, session_id: str) -> SessionUsage:
        raise NotImplementedError

    async def attach(self, session_id: str) -> AsyncSession:
        raise NotImplementedError

    async def list(
        self,
        *,
        state: str | SessionState | None = None,
        limit: int | None = None,
        cursor: str | None = None,
        external_id: str | None = None,
    ) -> tuple[list[SessionInfo], str | None]:
        raise NotImplementedError

    async def read_file(self, session_id: str, path: str) -> str:
        raise NotImplementedError

    async def list_files(self, session_id: str, path: str) -> list[str]:
        raise NotImplementedError

    async def write_file(
        self, session_id: str, path: str, content: str, encoding: str = "text"
    ) -> None:
        raise NotImplementedError

    async def batch_exec(
        self,
        session_ids: list[str],
        command: str | list[str],
        *,
        cwd: str | None = None,
        timeout_ms: int | None = None,
        timeout: float = 120.0,
    ) -> list[BatchExecResult]:
        raise NotImplementedError


class _AsyncCheckpointsAPI:
    """Checkpoint management. Accessed as ``client.checkpoints``."""

    def __init__(self, client: "AsyncClient") -> None:
        raise NotImplementedError

    async def get(self, checkpoint_id: str) -> CheckpointInfo:
        raise NotImplementedError

    async def delete(self, checkpoint_id: str) -> None:
        raise NotImplementedError

    async def create_session(
        self,
        checkpoint_id: str,
        *,
        name: str | None = None,
        labels: dict[str, str] | None = None,
        max_lifetime_ms: int | None = None,
        idle_timeout_ms: int | None = None,
    ) -> AsyncSession:
        raise NotImplementedError


class _AsyncArtifactsAPI:
    """Artifact management. Accessed as ``client.artifacts``."""

    def __init__(self, client: "AsyncClient") -> None:
        raise NotImplementedError

    async def get(self, artifact_id: str) -> ArtifactInfo:
        raise NotImplementedError

    async def extend(self, artifact_id: str, ttl_days: int) -> ArtifactInfo:
        raise NotImplementedError

    async def delete(self, artifact_id: str) -> None:
        raise NotImplementedError


class _AsyncTemplatesAPI:
    """Template management. Accessed as ``client.templates``."""

    def __init__(self, client: "AsyncClient") -> None:
        raise NotImplementedError

    async def list(self) -> list[TemplateInfo]:
        raise NotImplementedError

    async def import_image(
        self,
        from_image: str,
        name: str,
        description: str | None = None,
    ) -> TemplateInfo:
        raise NotImplementedError

    async def delete(self, name: str) -> None:
        raise NotImplementedError


class _AsyncRegistriesAPI:
    """Private-registry pull credentials. Accessed as ``client.registries``."""

    def __init__(self, client: "AsyncClient") -> None:
        raise NotImplementedError

    async def add(
        self,
        name: str,
        *,
        registry_host: str,
        username: str,
        secret: str,
    ) -> RegistryCredentialInfo:
        raise NotImplementedError

    async def list(self) -> list[RegistryCredentialInfo]:
        raise NotImplementedError

    async def delete(self, name: str) -> None:
        raise NotImplementedError


class _AsyncConnectorsAPI:
    """Egress connectors. Accessed as ``client.connectors``."""

    def __init__(self, client: "AsyncClient") -> None:
        raise NotImplementedError

    async def add(
        self,
        name: str,
        *,
        target_base_url: str,
        auth_shape: str = "bearer",
        header_name: str | None = None,
        secret: str,
    ) -> ConnectorInfo:
        raise NotImplementedError

    async def list(self) -> list[ConnectorInfo]:
        raise NotImplementedError

    async def delete(self, name: str) -> None:
        raise NotImplementedError


class _AsyncWebhooksAPI:
    """Webhook subscriptions for session events. Accessed as
    ``client.webhooks``."""

    def __init__(self, client: "AsyncClient") -> None:
        raise NotImplementedError

    async def add(self, url: str, *, event_types: list[str] | None = None) -> WebhookEndpointInfo:
        raise NotImplementedError

    async def list(self) -> list[WebhookEndpointInfo]:
        raise NotImplementedError

    async def delete(self, endpoint_id: str) -> None:
        raise NotImplementedError

    async def test(self, endpoint_id: str) -> WebhookDeliveryInfo:
        raise NotImplementedError

    async def deliveries(self, endpoint_id: str, *, limit: int = 50) -> list[WebhookDeliveryInfo]:
        raise NotImplementedError


class _AsyncDomainsAPI:
    """Custom domains with auto-TLS for exposed session ports.
    Accessed as ``client.domains``."""

    def __init__(self, client: "AsyncClient") -> None:
        raise NotImplementedError

    async def add(self, domain: str) -> DomainInfo:
        raise NotImplementedError

    async def list(self) -> list[DomainInfo]:
        raise NotImplementedError

    async def delete(self, domain_id: str) -> None:
        raise NotImplementedError

    async def verify(self, domain_id: str) -> DomainInfo:
        raise NotImplementedError

    async def route(self, domain_id: str, session_id: str, port: int) -> DomainInfo:
        raise NotImplementedError

    async def unroute(self, domain_id: str) -> DomainInfo:
        raise NotImplementedError


class AsyncClient:
    """Async twin of Client, backed by httpx.AsyncClient.

    Usage::

        async with AsyncClient() as client:
            async with client.session(template="python") as s:
                result = await s.exec("python --version")
                async for event in s.exec_stream("python -u long_job.py"):
                    ...
    """

    sessions: _AsyncSessionsAPI
    checkpoints: _AsyncCheckpointsAPI
    artifacts: _AsyncArtifactsAPI
    templates: _AsyncTemplatesAPI
    registries: _AsyncRegistriesAPI
    connectors: _AsyncConnectorsAPI
    webhooks: _AsyncWebhooksAPI
    domains: _AsyncDomainsAPI

    def __init__(
        self,
        *,
        token: str | None = None,
        base_url: str | None = None,
        timeout: float = 120.0,
    ) -> None:
        raise NotImplementedError

    async def close(self) -> None:
        raise NotImplementedError

    async def __aenter__(self) -> "AsyncClient":
        raise NotImplementedError

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        raise NotImplementedError

    @property
    def base_url(self) -> str:
        raise NotImplementedError

    @property
    def token(self) -> str:
        raise NotImplementedError

    @property
    def timeout(self) -> float:
        raise NotImplementedError

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
        env: dict[str, str] | None = None,
        secrets: dict[str, str] | None = None,
        connectors: list[str] | None = None,
        connections: list[ConnectionSpec | dict[str, Any]] | None = None,
    ) -> AsyncSession:
        """Return an AsyncSession configured for use as an async context manager."""
        raise NotImplementedError

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
    ) -> ExecResult:
        """One-shot language execution in a throwaway session."""
        raise NotImplementedError

    async def capacity(self) -> Capacity:
        """Return aggregate session capacity and limits for this account."""
        raise NotImplementedError

    async def usage(
        self,
        *,
        since: str | None = None,
        until: str | None = None,
        group_by: str | None = None,
    ) -> UsageSummary:
        """Aggregate compute/storage usage and spend for the account."""
        raise NotImplementedError


class AsyncInisClient:
    """One-shot operations backed by a throwaway session (async twin of
    InisClient)."""

    def __init__(self, *, base_url: str, token: str, timeout: float = 120.0) -> None:
        raise NotImplementedError

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
        raise NotImplementedError

    async def close(self) -> None:
        raise NotImplementedError

    async def __aenter__(self) -> "AsyncInisClient":
        raise NotImplementedError

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        raise NotImplementedError
