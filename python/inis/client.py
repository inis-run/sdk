from __future__ import annotations

import base64
import codecs
import hashlib
import json
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from enum import Enum
from typing import Any, Callable, Iterator

import httpx

from inis import interpreter_common as _ic
from inis.interpreter_common import InterpreterResult

DEFAULT_BASE_URL = "https://api.inis.run"


class _IncrementalUtf8Decoder:
    """Incrementally decodes base64-encoded byte chunks belonging to ONE
    logical output stream (e.g. every stdout frame of one exec_stream/
    stream_process_logs call) into UTF-8 text, without corrupting a
    multi-byte character whose bytes land on either side of a chunk
    boundary.

    Chunk boundaries here are wherever the guest's io.Copy (32KB buffer) or
    the SSE line reader happened to split a write — not UTF-8 character
    boundaries. Decoding each base64 chunk standalone with
    ``base64.b64decode(...).decode("utf-8", errors="replace")`` (the
    previous approach) replaces a character split across chunk N/N+1 with
    TWO U+FFFD replacement characters (a dangling lead byte at the end of
    chunk N, a dangling continuation byte at the start of chunk N+1) instead
    of joining them into the one character they were always meant to be.
    ``codecs.getincrementaldecoder`` holds back any trailing incomplete
    sequence and prepends it to the next ``decode()`` call — which only
    works if the SAME decoder instance is reused across every chunk of that
    one stream. Never share one decoder between independent streams (e.g.
    stdout and stderr) — their byte sequences are unrelated. Call ``finish()``
    once at the true end of the stream to flush (and, per errors="replace",
    surface as a replacement character — a genuine decoding error at that
    point, not a boundary artifact) any bytes still held back.
    """

    def __init__(self) -> None:
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")

    def push(self, b64: str) -> str:
        return self._decoder.decode(base64.b64decode(b64), final=False)

    def finish(self) -> str:
        return self._decoder.decode(b"", final=True)



# Codes an SDK caller can reasonably expect to succeed by simply
# retrying the identical request after backing off, as opposed to a failure
# that needs the caller (bad input, expired credential) or an operator
# (fleet capacity, cold-tier config) to change something first. Mirrors
# internal/errcode.Retryable in the Go codebase (the single source of truth
# there) by hand -- this SDK can't import a Go package -- and
# sdk/typescript/src/client.ts's RETRYABLE_CODES mirrors it a third time.
# Keep all three in sync when this list changes.
#
# Explicitly NOT retryable despite sharing an HTTP status class with a
# retryable code: session_ended (410, definitively over), archive_unavailable
# (503, needs a node/cold-tier config change), size_unavailable (503, a
# fleet-capacity fact not a transient dip), payload_too_large (400,
# deterministic in the request content), method_not_allowed (405, client
# misuse -- retrying the identical wrong-method request fails identically).
# Any code not listed here defaults to non-retryable -- the safe choice for a
# code this SDK has never seen.
_RETRYABLE_CODES = frozenset(
    {
        "session_not_live",
        "session_unavailable",
        "session_node_unavailable",
        "rate_limited",
        "unavailable",
        "template_version_unavailable",
        "bad_gateway",
        # Legacy, status-derived codes that predate the errcode taxonomy but
        # mean the same retryable 429/503/502 thing.
        "concurrency_limit_exceeded",
        "rate_limit_exceeded",
        "service_unavailable",
    }
)

_REQUEST_ID_HEADER = "X-Inis-Request-Id"


def _parse_retry_after(value: str) -> float | None:
    """Parse an HTTP Retry-After header: an integer delta-seconds (what
    every writer in this codebase actually sends -- internal/api/server.go's
    writeRetryableError et al. always emit a bare integer) or, for
    robustness, an RFC 9110 HTTP-date."""
    value = value.strip()
    try:
        return float(value)
    except ValueError:
        pass
    try:
        dt = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    delta = (dt - datetime.now(timezone.utc)).total_seconds()
    return max(delta, 0.0)


class InisError(Exception):
    """Raised when the inis.run API returns an error.

    Carries the full error contract rather than just a message: a
    stable machine-readable ``code`` (falls back to ``"error"`` if the
    response carried none -- never derived from the message text), the HTTP
    ``status``, whether the failure is ``retryable``, ``retry_after``
    seconds if the server sent a Retry-After header, and ``request_id`` if
    the server sent X-Inis-Request-Id. ``response`` holds the raw decoded
    JSON error body (or ``None`` for a non-JSON/empty body), so nothing the
    server actually sent is lost -- including a ``code`` value this SDK
    version has never seen, which is passed through completely raw rather
    than collapsed into a message-only error.
    """

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
        super().__init__(message)
        self.code = code
        self.status = status
        self.retryable = retryable
        self.retry_after = retry_after
        self.request_id = request_id
        self.response = response

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return (
            f"InisError({str(self)!r}, code={self.code!r}, status={self.status!r}, "
            f"retryable={self.retryable!r}, retry_after={self.retry_after!r}, "
            f"request_id={self.request_id!r})"
        )


def _raise_for_status(resp: httpx.Response) -> None:
    """Raise a fully-populated InisError for a non-2xx httpx.Response,
    preserving the server's whole error contract instead of just its
    message string. The single choke point every request path in this
    module funnels through -- do not hand-roll another
    ``resp.json().get("error", ...)`` block; add a call to this instead.
    """
    if resp.status_code < 400:
        return
    body: dict[str, Any] | None = None
    try:
        parsed = resp.json()
        if isinstance(parsed, dict):
            body = parsed
    except Exception:
        body = None

    message = (body or {}).get("error") or resp.text or f"HTTP {resp.status_code}"
    code = (body or {}).get("code") or "error"
    retryable = code in _RETRYABLE_CODES

    retry_after: float | None = None
    ra_header = resp.headers.get("retry-after")
    if ra_header:
        retry_after = _parse_retry_after(ra_header)
        if retry_after is not None:
            # A Retry-After header is itself a stronger, independent
            # retryability signal than the static code table above: the
            # server sending one is a direct statement "retry is fine,
            # here's when" -- honour it even for a code not in
            # _RETRYABLE_CODES yet (e.g. a future code this SDK predates).
            retryable = True

    # httpx.Headers keys are case-insensitive, so this matches the header
    # however the server (or an intervening proxy) capitalised it.
    request_id = resp.headers.get(_REQUEST_ID_HEADER)

    raise InisError(
        f"{resp.status_code}: {message}",
        code=code,
        status=resp.status_code,
        retryable=retryable,
        retry_after=retry_after,
        request_id=request_id,
        response=body,
    )


class OnPtyDetachPolicy(str, Enum):
    """What the server does when the last PTY client detaches."""

    KEEP_LIVE = "keep_live"
    PAUSE = "pause"
    DESTROY = "destroy"


class DestroyReason(str, Enum):
    """Caller-supplied reason recorded as end_reason on explicit destroy."""

    CLIENT_DESTROY = "client_destroy"
    SHELL_EXIT = "shell_exit"


def _egress_payload(
    default: str | None, allow: list[str] | None
) -> dict[str, Any] | None:
    """Build an egress request body, or None when no policy was requested."""
    if not default and not allow:
        return None
    body: dict[str, Any] = {"mode": default or "allow"}
    if allow:
        body["allow"] = list(allow)
    return body


# ── Supporting data models ────────────────────────────────────────────────────


@dataclass
class EgressPolicy:
    """Per-session outbound allowlist."""

    mode: str  # "allow" | "deny"
    allow: list[str] | None = None


@dataclass
class ArchiveStatus:
    """Durable (cold) object-store copy of a paused session."""

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


# ── Primary data models ───────────────────────────────────────────────────────


@dataclass
class ExecResult:
    """Result of a command run inside a session."""

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
    actually produced -- the guest's own per-stream capture cap cut a large
    stream before it ever grew large enough to hit this SDK's own size
    limits. Only the buffered exec()/run_code() calls can set this; exec_stream()
    never truncates (see ExecStreamEvent)."""
    stdout_encoding: str | None = None
    stderr_encoding: str | None = None
    """None (the default) means stdout/stderr are plain text, as always.
    "base64" means that stream's output was not valid UTF-8, so the API
    returned it base64-encoded rather than risk silently corrupting it --
    decode with base64.b64decode(result.stdout) to get the exact bytes the
    command produced. Only the buffered exec()/run_code() calls can set
    this."""


@dataclass
class SessionInfo:
    """Snapshot of a session returned by create, get, pause, resume, etc."""

    session_id: str
    state: str
    status_detail: str | None = None
    """Coarse provisioning phase while state=="creating": "waiting_for_capacity",
    "preparing_template", or "starting". Empty once the session is live or
    failed."""
    status_reason: str | None = None
    """Set only when state=="failed": a short, user-safe reason provisioning
    didn't complete."""
    volume_id: str | None = None
    created_at: str | None = None
    name: str | None = None
    labels: dict[str, str] | None = None
    last_active_at: str | None = None
    idle_timeout_ms: int | None = None
    max_lifetime_ms: int | None = None
    exposed_ports: list[int] | None = None
    size: str | None = None
    vcpus: int | None = None
    mem_mb: int | None = None
    template: str | None = None
    no_sudo: bool = False
    on_pty_detach: str | None = None  # "keep_live" | "pause" | "destroy"
    paused_ttl_ms: int | None = None
    ended_at: str | None = None  # state=ended rows only
    end_reason: str | None = None  # state=ended rows only
    exposed_previews: list[ExposedPreview] | None = None
    egress: EgressPolicy | None = None
    archive: ArchiveStatus | None = None
    node_id: str | None = None
    mcp_url: str | None = None
    active_since: str | None = None
    """When the current continuous-live stretch began (RFC 3339). Set only
    while live with a max-active-window armed; the session auto-pauses at
    active_since + max_active_window_ms."""
    max_active_window_ms: int | None = None
    """The continuous-active window (ms) the current live stretch was armed
    with. Set only while live with the window armed."""
    snapshot_tier: str | None = None
    """Resume-readiness of a PAUSED session: "hot" (instant), "warm" (quick
    local decompress), or "cold" (needs a download). None for non-paused
    sessions."""
    env: dict[str, str] | None = None
    """Plain (non-sensitive) environment variables set on this session."""
    secret_names: list[str] | None = None
    """Names of the secrets configured on this session — never their values."""
    connector_names: list[str] | None = None
    """Egress connectors this session has opted into, by name — never any secret material."""


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
class CapacityLimits:
    running: int
    warm: int


@dataclass
class Capacity:
    running: int
    warm: int
    limits: CapacityLimits | None = None


@dataclass
class ForkResult:
    parent_session_id: str
    children: list[str]


@dataclass
class ExecStreamEvent:
    """One event from a live-streamed exec (see Session.exec_stream)."""

    stream: str  # "stdout" | "stderr" | "exit" | "error"
    data: str  # decoded text for stdout/stderr/error; empty for exit
    exit_code: int | None = None  # set only when stream=="exit"
    timed_out: bool | None = None  # set only when stream=="exit"
    duration_ms: int | None = None  # set only when stream=="exit"


@dataclass
class ProcessInfo:
    """A named background process, running or exited, in a session."""

    name: str
    state: str  # "running" | "exited"
    pid: int | None = None
    command: list[str] | None = None
    exit_code: int | None = None
    keep_alive: bool = False
    started_at: str | None = None
    ended_at: str | None = None


def _map_process_info(resp: dict[str, Any]) -> ProcessInfo:
    return ProcessInfo(
        name=resp.get("name", ""),
        state=resp.get("state", ""),
        pid=resp.get("pid"),
        command=resp.get("command"),
        exit_code=resp.get("exit_code"),
        keep_alive=resp.get("keep_alive", False),
        started_at=resp.get("started_at"),
        ended_at=resp.get("ended_at"),
    )


@dataclass
class ProcessLogs:
    """Buffered stdout/stderr captured so far for a named process."""

    name: str
    stdout: str
    stderr: str
    truncated: bool = False
    """True when stdout/stderr are an incomplete view of the process's
    actual output so far -- the guest's own per-read tail cap cut it."""
    stdout_encoding: str | None = None
    stderr_encoding: str | None = None
    """None (the default) means plain text. "base64" means that stream's
    output was not valid UTF-8 -- decode with base64.b64decode(...) to get
    the exact bytes. See ExecResult.stdout_encoding for the full rationale."""


@dataclass
class ProcessLogEvent:
    """One event from a live-followed process log stream (see Session.stream_process_logs)."""

    event: str  # "stdout" | "stderr" | "eof" | "error"
    data: str  # decoded text for stdout/stderr/error; empty for eof


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


def _map_checkpoint(resp: dict[str, Any]) -> CheckpointInfo:
    return CheckpointInfo(
        checkpoint_id=resp["checkpoint_id"],
        session_id=resp.get("session_id"),
        parent_session_id=resp.get("parent_session_id"),
        name=resp.get("name"),
        labels=resp.get("labels"),
        size_bytes=resp.get("size_bytes", 0),
        created_at=resp.get("created_at"),
        node_id=resp.get("node_id"),
    )


@dataclass
class TemplateInfo:
    """A base environment a session can be created from."""

    name: str
    kind: str | None = None  # "official" | "user"
    description: str | None = None
    size: str | None = None
    created_at: str | None = None
    version: str | None = None  # promoted current version
    versions: list[str] | None = None  # available pinnable versions
    # BYO-image import (a template imported from an OCI ref): "queued" behind the
    # node's serial build queue, then "building" while the image is flattened,
    # then "ready" or "failed". "ready" for every official and promote-a-session
    # template. queue_position is the 1-based place in the build queue, set only
    # while status == "queued".
    status: str | None = None
    source_image: str | None = None  # OCI ref imported from, if any
    build_error: str | None = None  # reason when status == "failed"
    queue_position: int | None = None  # 1-based, only while status == "queued"
    built_at: str | None = None  # official templates only
    base_image_ref: str | None = None  # e.g. docker.io/library/python:3.12-slim; official only
    base_image_digest: str | None = None  # sha256:...; official only
    content_hash: str | None = None  # tamper-evident bundle hash; official only
    cadence: str | None = None  # "weekly" | "monthly"; official only
    category: str | None = None  # "language" | "use-case"; official only


def _map_template(resp: dict[str, Any]) -> TemplateInfo:
    return TemplateInfo(
        name=resp["name"],
        kind=resp.get("kind"),
        description=resp.get("description"),
        size=resp.get("size"),
        created_at=resp.get("created_at"),
        version=resp.get("version"),
        versions=resp.get("versions"),
        status=resp.get("status"),
        source_image=resp.get("source_image"),
        build_error=resp.get("build_error"),
        queue_position=resp.get("queue_position"),
        built_at=resp.get("built_at"),
        base_image_ref=resp.get("base_image_ref"),
        base_image_digest=resp.get("base_image_digest"),
        content_hash=resp.get("content_hash"),
        cadence=resp.get("cadence"),
        category=resp.get("category"),
    )


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


def _map_registry_credential(resp: dict[str, Any]) -> RegistryCredentialInfo:
    return RegistryCredentialInfo(
        id=resp["id"],
        name=resp["name"],
        registry_host=resp["registry_host"],
        username=resp["username"],
        secret_last4=resp.get("secret_last4"),
        created_at=resp.get("created_at"),
        updated_at=resp.get("updated_at"),
    )


@dataclass
class ConnectorInfo:
    """A stored egress connector, redacted -- the secret is never included,
    on create, list, or any other response."""

    id: str
    name: str
    target_base_url: str
    auth_shape: str
    header_name: str | None = None
    secret_last4: str | None = None
    created_at: str | None = None
    updated_at: str | None = None


def _map_connector(resp: dict[str, Any]) -> ConnectorInfo:
    return ConnectorInfo(
        id=resp["id"],
        name=resp["name"],
        target_base_url=resp["target_base_url"],
        auth_shape=resp["auth_shape"],
        header_name=resp.get("header_name"),
        secret_last4=resp.get("secret_last4"),
        created_at=resp.get("created_at"),
        updated_at=resp.get("updated_at"),
    )


@dataclass
class WebhookEndpointInfo:
    """A registered webhook subscription. ``secret`` is populated ONLY
    on the response from :meth:`_WebhooksAPI.add` -- store it immediately,
    it is never returned again (``secret_last4`` is what every later
    ``list()`` shows instead)."""

    id: str
    url: str
    event_types: list[str]
    enabled: bool
    created_at: str | None = None
    disabled_at: str | None = None
    secret: str | None = None
    secret_last4: str | None = None


def _map_webhook(resp: dict[str, Any]) -> WebhookEndpointInfo:
    return WebhookEndpointInfo(
        id=resp["id"],
        url=resp["url"],
        event_types=resp.get("event_types") or [],
        enabled=resp.get("enabled", True),
        created_at=resp.get("created_at"),
        disabled_at=resp.get("disabled_at"),
        secret=resp.get("secret"),
        secret_last4=resp.get("secret_last4"),
    )


@dataclass
class WebhookDeliveryInfo:
    """One logged delivery attempt series for a webhook subscription -- the
    per-subscription delivery log entry."""

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


def _map_webhook_delivery(resp: dict[str, Any]) -> WebhookDeliveryInfo:
    return WebhookDeliveryInfo(
        id=resp["id"],
        event_type=resp["event_type"],
        status=resp["status"],
        attempt_count=resp.get("attempt_count", 0),
        max_attempts=resp.get("max_attempts", 0),
        session_id=resp.get("session_id"),
        attempts=resp.get("attempts"),
        created_at=resp.get("created_at"),
        delivered_at=resp.get("delivered_at"),
        next_attempt_at=resp.get("next_attempt_at"),
    )


@dataclass
class DomainInfo:
    """A registered custom domain: CNAME a customer-owned hostname to
    the fleet's ingress, verify DNS ownership, then route it to an exposed
    session port for auto-provisioned TLS in place of the default preview
    URL. ``verify_txt_name``/``verify_txt_value`` are the TXT record to
    create before calling :meth:`_DomainsAPI.verify` (a CNAME to the fleet's
    ingress also satisfies verification)."""

    id: str
    domain: str
    verified: bool
    verify_txt_name: str
    verify_txt_value: str
    created_at: str | None = None
    verified_at: str | None = None
    session_id: str | None = None
    port: int | None = None
    ingress_base_domain: str | None = None
    """The hostname to CNAME this domain to so requests reach the fleet.
    TXT verification only proves ownership -- traffic still needs this
    record. ``None`` when the deployment has no ingress domain set."""


def _map_domain(resp: dict[str, Any]) -> DomainInfo:
    return DomainInfo(
        id=resp["id"],
        domain=resp["domain"],
        verified=resp.get("verified", False),
        verify_txt_name=resp.get("verify_txt_name", ""),
        verify_txt_value=resp.get("verify_txt_value", ""),
        created_at=resp.get("created_at"),
        verified_at=resp.get("verified_at"),
        session_id=resp.get("session_id"),
        port=resp.get("port"),
        ingress_base_domain=resp.get("ingress_base_domain"),
    )


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


def _map_artifact(resp: dict[str, Any]) -> ArtifactInfo:
    files = [
        ArtifactFile(
            path=f.get("path", ""),
            url=f.get("url"),
            size_bytes=f.get("size_bytes", 0),
            content_type=f.get("content_type"),
        )
        for f in (resp.get("files") or [])
    ]
    return ArtifactInfo(
        id=resp["id"],
        status=resp.get("status", ""),
        session_id=resp.get("session_id"),
        captured_at=resp.get("captured_at"),
        expires_at=resp.get("expires_at"),
        files=files,
        error=resp.get("error"),
    )


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
    """None (the default) means stdout/stderr are plain text, as always.
    "base64" means that session's output on this stream was not valid UTF-8,
    so the API returned it base64-encoded rather than risk silently
    corrupting it -- decode with base64.b64decode(result.stdout) to get the
    exact bytes. See ExecResult.stdout_encoding for the full rationale."""


# ── Helpers ───────────────────────────────────────────────────────────────────


def _map_egress(resp: dict[str, Any] | None) -> EgressPolicy | None:
    if resp is None:
        return None
    # Support both old "default" field and new "mode" field.
    mode = resp.get("mode") or resp.get("default") or "allow"
    return EgressPolicy(mode=mode, allow=resp.get("allow"))


def _map_archive(resp: dict[str, Any] | None) -> ArchiveStatus | None:
    if resp is None:
        return None
    return ArchiveStatus(
        status=resp.get("status", ""),
        cold_uri=resp.get("cold_uri"),
        uploaded_at=resp.get("uploaded_at"),
    )


def _map_session_info(resp: dict[str, Any]) -> SessionInfo:
    raw_previews = resp.get("exposed_previews")
    exposed_previews = None
    if raw_previews is not None:
        exposed_previews = [
            ExposedPreview(
                port=p.get("port", 0),
                preview_url=p.get("preview_url", ""),
                visibility=p.get("visibility"),
                auth=p.get("auth"),
            )
            for p in raw_previews
        ]
    return SessionInfo(
        session_id=resp["session_id"],
        state=resp["state"],
        status_detail=resp.get("status_detail"),
        status_reason=resp.get("status_reason"),
        volume_id=resp.get("volume_id") or None,
        created_at=resp.get("created_at"),
        name=resp.get("name"),
        labels=resp.get("labels"),
        last_active_at=resp.get("last_active_at"),
        idle_timeout_ms=resp.get("idle_timeout_ms"),
        max_lifetime_ms=resp.get("max_lifetime_ms"),
        exposed_ports=resp.get("exposed_ports"),
        size=resp.get("size"),
        vcpus=resp.get("vcpus"),
        mem_mb=resp.get("mem_mb"),
        template=resp.get("template"),
        no_sudo=resp.get("no_sudo", False),
        on_pty_detach=resp.get("on_pty_detach"),
        paused_ttl_ms=resp.get("paused_ttl_ms"),
        ended_at=resp.get("ended_at"),
        end_reason=resp.get("end_reason"),
        exposed_previews=exposed_previews,
        egress=_map_egress(resp.get("egress")),
        archive=_map_archive(resp.get("archive")),
        node_id=resp.get("node_id"),
        mcp_url=resp.get("mcp_url"),
        active_since=resp.get("active_since"),
        max_active_window_ms=resp.get("max_active_window_ms"),
        snapshot_tier=resp.get("snapshot_tier"),
        env=resp.get("env"),
        secret_names=resp.get("secret_names"),
        connector_names=resp.get("connector_names"),
    )


def _map_batch_results(data: dict[str, Any]) -> list[BatchExecResult]:
    out: list[BatchExecResult] = []
    for item in data.get("results", []):
        if item.get("error"):
            out.append(BatchExecResult(session_id=item["session_id"], error=item["error"]))
        else:
            out.append(
                BatchExecResult(
                    session_id=item["session_id"],
                    stdout=item.get("stdout", ""),
                    stderr=item.get("stderr", ""),
                    exit_code=item.get("exit_code", 0),
                    duration_ms=item.get("duration_ms", 0),
                    timed_out=item.get("timed_out", False),
                    stdout_encoding=item.get("stdout_encoding"),
                    stderr_encoding=item.get("stderr_encoding"),
                )
            )
    return out


class FilesAPI:
    """Backward-compatible namespace for session file operations.

    Delegates to the Session's direct read_file / list_files / write_file methods.
    """

    def __init__(self, session: "Session") -> None:
        self._session = session

    def read(self, path: str, *, timeout_ms: int | None = None) -> str:
        return self._session.read_file(path)

    def write(self, path: str, content: str, *, timeout_ms: int | None = None) -> None:
        self._session.write_file(path, content)

    def list(self, path: str = "/workspace", *, timeout_ms: int | None = None) -> list[str]:
        return self._session.list_files(path)


class Session:
    """Context manager for a live sandbox session."""

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
        egress_default: str | None = None,
        egress_allow: list[str] | None = None,
        from_checkpoint: str | None = None,
        template: str | None = None,
        size: str | None = None,
        no_sudo: bool = False,
        on_pty_detach: str | OnPtyDetachPolicy | None = None,
        paused_ttl_ms: int | None = None,
        command_capture: str | None = None,
        env: dict[str, str] | None = None,
        secrets: dict[str, str] | None = None,
        connectors: list[str] | None = None,
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
        self._from_checkpoint = from_checkpoint
        self._template = template
        self._size = size
        self._no_sudo = no_sudo
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
        self._timeout = timeout
        self._client = httpx.Client(timeout=timeout)
        self.session_id: str | None = session_id
        self.files = FilesAPI(self)
        # on_status/wait: POST /v1/sessions acknowledges immediately
        # with the session in "creating" state instead of blocking until the
        # sandbox is ready — a cold template pull can take tens of seconds.
        # on_status, if given, is called with each coarse status_detail phase
        # ("preparing_template", "starting", ...) observed while __enter__
        # polls GET to keep this class's historical blocking contract (the
        # "with" block only starts once the session is actually live).
        # wait=False skips that poll entirely, returning as soon as the
        # create is acknowledged, for a caller that wants the async shape
        # and will poll get() itself.
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
    ) -> "Session":
        """Bind to an existing session without creating a new one.

        base_url and token default to the same env resolution as Client
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

    def __enter__(self) -> "Session":
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
        if self._from_checkpoint:
            payload["from_checkpoint"] = self._from_checkpoint
        if self._template:
            payload["template"] = self._template
        if self._size:
            payload["size"] = self._size
        if self._no_sudo:
            payload["no_sudo"] = True
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
        egress = _egress_payload(self._egress_default, self._egress_allow)
        if egress is not None:
            payload["egress"] = egress
        resp = self._request("POST", "/v1/sessions", json=payload)
        self.session_id = resp["session_id"]
        if self._wait:
            self._wait_until_ready(resp)
        return self

    # sessionCreatePollTimeout's Python equivalent: generously above the
    # worst documented cold-template-pull cost (~35s for the largest
    # official bundle) plus room for a scale-out wait, without
    # blocking forever against a genuinely wedged create.
    _CREATE_POLL_TIMEOUT_S = 300.0
    _CREATE_POLL_INTERVAL_S = 0.3

    def _wait_until_ready(self, resp: dict[str, Any]) -> None:
        """Poll GET until this session leaves state="creating".

        Keeps Session's historical blocking contract (the sandbox is ready
        to use by the time __enter__/create() returns) now that POST
        acknowledges immediately. A no-op when resp is already something
        other than "creating" (the common already-warm case: no template,
        or a template already materialized on the placed node).
        """
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
            time.sleep(self._CREATE_POLL_INTERVAL_S)
            info = self._request("GET", f"/v1/sessions/{self.session_id}")
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

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if self.session_id:
            try:
                self.destroy()
            finally:
                pass
        self._client.close()

    def close(self) -> None:
        """Close this object's local HTTP transport without destroying the
        session server-side.

        Use this for a session you attached to but do not own (`attach()` /
        `client.sessions.attach(...)`) — `__exit__` always destroys, which is
        correct for an owned session's `with` block but wrong for one you are
        only borrowing: attached sessions must not be destroyed. Safe to
        call more than once.
        """
        self._client.close()

    # ── Session state ─────────────────────────────────────────────────────────

    def get(self) -> SessionInfo:
        """Fetch the current state of this session."""
        resp = self._request("GET", f"/v1/sessions/{self.session_id}")
        return _map_session_info(resp)

    def info(self) -> SessionInfo:
        """Fetch the current state of this session. Alias for get()."""
        return self.get()

    def pause(self) -> SessionInfo:
        """Pause this session."""
        resp = self._request("POST", f"/v1/sessions/{self.session_id}/pause")
        return _map_session_info(resp)

    def resume(self) -> SessionInfo:
        """Resume this paused session."""
        resp = self._request("POST", f"/v1/sessions/{self.session_id}/resume")
        return _map_session_info(resp)

    def destroy(self, *, reason: str | DestroyReason | None = None) -> None:
        """Destroy this session and free all its resources."""
        if not self.session_id:
            return
        req_params: dict[str, Any] | None = None
        if reason is not None:
            req_params = {
                "reason": reason.value if isinstance(reason, DestroyReason) else reason
            }
        self._request("DELETE", f"/v1/sessions/{self.session_id}", params=req_params)
        self.session_id = None

    # ── Exec ──────────────────────────────────────────────────────────────────

    def exec(
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
        resp = self._request("POST", f"/v1/sessions/{self.session_id}/exec", json=payload)
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
        with self._client.stream(
            "POST", url, headers=headers, params={"stream": "true"}, json=payload
        ) as resp:
            if resp.status_code >= 400:
                resp.read()
                _raise_for_status(resp)
            # Separate incremental decoders for stdout/stderr: they're
            # independent byte streams, so a character split at the end of
            # one must never be joined with bytes from the other.
            stdout_decoder = _IncrementalUtf8Decoder()
            stderr_decoder = _IncrementalUtf8Decoder()
            event_type: str | None = None
            for line in resp.iter_lines():
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
                        # Flush any bytes still held back for a not-yet-
                        # complete trailing multi-byte character — real
                        # content, not a boundary artifact, this being the
                        # true end of the stream.
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

    def run_code(
        self, code: str, *, timeout_ms: int | None = None
    ) -> list[InterpreterResult]:
        """Run code in this session's persistent Python interpreter context.

        Unlike exec(), variables/imports/functions/classes survive across
        calls — the "context" — until restart_context() or the session
        ends. Returns a list of typed results: "text" (stdout/stderr/the
        last expression's repr), "image" (a captured matplotlib figure,
        .data holding decoded PNG bytes), "table" (a pandas DataFrame/
        Series last expression, capped at 1000 rows), "json" (a dict/list/
        tuple last expression), or "error" (an uncaught exception with its
        traceback — the context is untouched and stays usable next call).

        Built entirely on exec/write_file/read_file/start_process: no
        guest-agent or template changes required. A small stdlib-only
        Python script (matplotlib/pandas used opportunistically if already
        installed) is uploaded on first use and run as a background
        process. If that process has died — OOM, a manual
        kill_process("inis-kernel"), code that segfaulted the interpreter —
        the next run_code() call transparently restarts it with a fresh,
        empty context and runs normally; it does not raise.
        """
        timeout_ms = timeout_ms or _ic.DEFAULT_TIMEOUT_MS
        return self._run_code_attempt(code, timeout_ms, allow_retry=True)

    def restart_context(self) -> None:
        """Discard this session's interpreter context: kills the running
        kernel process (if any) and starts a fresh one. The next run_code()
        call begins from empty globals — no prior variables, imports, or
        definitions."""
        try:
            self.kill_process(_ic.KERNEL_NAME)
        except InisError:
            pass
        self._kernel_verified = False
        self._ensure_kernel()

    def _ensure_kernel(self) -> None:
        if getattr(self, "_kernel_verified", False):
            return
        running = False
        try:
            info = self.get_process(_ic.KERNEL_NAME)
            running = info.state == "running"
        except InisError:
            running = False
        version_ok = False
        if running:
            try:
                installed = self.read_file(_ic.VERSION_PATH)
                version_ok = installed.strip() == _ic.KERNEL_VERSION
            except InisError:
                version_ok = False
        if running and version_ok:
            self._kernel_verified = True
            return

        self.exec(_ic.mkdirs_command())
        self.write_file(_ic.KERNEL_PATH, _ic.KERNEL_SOURCE)
        self.write_file(_ic.VERSION_PATH, _ic.KERNEL_VERSION)
        if running:
            try:
                self.kill_process(_ic.KERNEL_NAME)
            except InisError:
                pass
        self.start_process(
            _ic.KERNEL_NAME, ["python3", _ic.KERNEL_PATH, _ic.ROOT_DIR]
        )
        self._kernel_verified = True

    def _run_code_attempt(
        self, code: str, timeout_ms: int, *, allow_retry: bool
    ) -> list[InterpreterResult]:
        self._ensure_kernel()
        req_id = _ic.new_request_id()
        tmp_path, final_path = _ic.request_paths(req_id)
        self.write_file(tmp_path, _ic.encode_request(req_id, code))
        wait_cmd = _ic.wait_command(req_id, tmp_path, final_path, timeout_ms)
        result = self.exec(wait_cmd, timeout_ms=_ic.exec_timeout_ms(timeout_ms))
        raw_results = _ic.parse_envelope(result.stdout, req_id)
        if raw_results is None:
            if allow_retry:
                try:
                    info = self.get_process(_ic.KERNEL_NAME)
                    kernel_alive = info.state == "running"
                except InisError:
                    kernel_alive = False
                if not kernel_alive:
                    self._kernel_verified = False
                    return self._run_code_attempt(code, timeout_ms, allow_retry=False)
            return [_ic.timeout_result(timeout_ms)]
        return [self._materialize_result(r) for r in raw_results]

    def _materialize_result(self, raw: dict[str, Any]) -> InterpreterResult:
        result = _ic.raw_to_result(raw)
        if result.type == "image" and result.path:
            try:
                data = self.read_file(result.path, encoding="base64")
                result.data = data if isinstance(data, bytes) else data.encode("utf-8")
            except InisError:
                pass
        return result

    # ── PTY (interactive terminal) ──────────────────────────────────────────────

    def pty(
        self,
        *,
        cols: int = 80,
        rows: int = 24,
        command: list[str] | None = None,
        cwd: str | None = None,
        term: str = "xterm-256color",
        colorterm: str = "truecolor",
        open_timeout: float = 30.0,
    ) -> "PTY":
        """Open an interactive pseudo-terminal in this session.

        Returns a live PTY over the same WebSocket endpoint and binary frame
        protocol the console's web terminal uses. command defaults to the
        guest's login shell; pass e.g. ["python3", "-i"] to open a REPL
        directly. Use as a context manager to close it automatically::

            with session.pty(command=["python3", "-i"]) as term:
                for chunk in term:
                    ...
        """
        from inis.pty import PTY as _PTY

        return _PTY._open(
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

    def start_process(
        self,
        name: str,
        command: str | list[str],
        *,
        cwd: str | None = None,
        keep_alive: bool = False,
    ) -> ProcessInfo:
        """Start a named, detached background process in this session.

        Unlike exec(), the process keeps running after this call returns —
        independent of this client disconnecting. Starting a second process
        under a name still in use by a running one is rejected; a name whose
        process has already exited may be reused.

        keep_alive=True suppresses the session's idle timer for as long as
        this process is running, the same mechanism an attached PTY or a
        pinned ingress connection uses.
        """
        if isinstance(command, str):
            command = ["bash", "-lc", command]
        body: dict[str, Any] = {"name": name, "command": command, "keep_alive": keep_alive}
        if cwd:
            body["cwd"] = cwd
        resp = self._request("POST", f"/v1/sessions/{self.session_id}/processes", json=body)
        return _map_process_info(resp)

    def list_processes(self) -> list[ProcessInfo]:
        """List every named background process in this session (running and exited)."""
        resp = self._request("GET", f"/v1/sessions/{self.session_id}/processes")
        return [_map_process_info(p) for p in resp.get("processes", [])]

    def get_process(self, name: str) -> ProcessInfo:
        """Get one named background process's current state."""
        resp = self._request("GET", f"/v1/sessions/{self.session_id}/processes/{name}")
        return _map_process_info(resp)

    def kill_process(self, name: str) -> ProcessInfo:
        """Kill a named background process: SIGTERM, then SIGKILL after a grace
        period if it hasn't exited. A no-op (just reports state) if it has
        already exited."""
        resp = self._request("DELETE", f"/v1/sessions/{self.session_id}/processes/{name}")
        return _map_process_info(resp)

    def get_process_logs(self, name: str) -> ProcessLogs:
        """Read a named process's captured stdout/stderr so far, buffered (one read)."""
        resp = self._request("GET", f"/v1/sessions/{self.session_id}/processes/{name}/logs")
        return ProcessLogs(
            name=resp.get("name", name),
            stdout=resp.get("stdout", ""),
            stderr=resp.get("stderr", ""),
            truncated=resp.get("truncated", False),
            stdout_encoding=resp.get("stdout_encoding"),
            stderr_encoding=resp.get("stderr_encoding"),
        )

    def stream_process_logs(self, name: str) -> Iterator[ProcessLogEvent]:
        """Live-follow a named process's stdout/stderr via Server-Sent Events.

        Yields a ProcessLogEvent per chunk as the guest produces it, ending
        with event="eof" once the process exits and its output is fully
        drained, or event="error" on a guest-side failure. Both terminal
        events end iteration and close the underlying HTTP stream.
        """
        headers = {"Authorization": f"Bearer {self._token}"}
        url = f"{self._base_url}/v1/sessions/{self.session_id}/processes/{name}/logs"
        with self._client.stream("GET", url, headers=headers, params={"follow": "true"}) as resp:
            if resp.status_code >= 400:
                resp.read()
                _raise_for_status(resp)
            stdout_decoder = _IncrementalUtf8Decoder()
            stderr_decoder = _IncrementalUtf8Decoder()
            event_type: str | None = None
            for line in resp.iter_lines():
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
                        # Flush any bytes still held back for a not-yet-
                        # complete trailing multi-byte character — this is
                        # the true end of the stream, so what's buffered is
                        # real content, not a boundary artifact waiting on a
                        # next chunk that will never arrive.
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

    def read_file(self, path: str, *, encoding: str = "text") -> str | bytes:
        """Read a file from the session filesystem.

        GET /v1/sessions/{id}/files?path=<path>&op=read[&encoding=base64]

        encoding="text" (default) returns the file as a str. encoding="base64"
        reads it as binary and returns the decoded raw bytes.
        """
        params: dict[str, Any] = {"path": path, "op": "read"}
        if encoding != "text":
            params["encoding"] = encoding
        resp = self._request(
            "GET",
            f"/v1/sessions/{self.session_id}/files",
            params=params,
        )
        content = resp.get("content", "")
        if encoding == "base64":
            return base64.b64decode(content)
        return content

    def list_files(self, path: str = "/workspace") -> list[str]:
        """List entries under path in the session filesystem.

        GET /v1/sessions/{id}/files?path=<path>&op=list
        """
        resp = self._request(
            "GET",
            f"/v1/sessions/{self.session_id}/files",
            params={"path": path, "op": "list"},
        )
        return resp.get("entries", [])

    def write_file(self, path: str, content: str | bytes, encoding: str = "text") -> None:
        """Write content to a file in the session filesystem.

        PUT /v1/sessions/{id}/files

        Pass bytes to write binary content — it is base64-encoded on the wire
        automatically. Passing encoding="base64" with a str is also honored
        (the str is assumed already base64-encoded).
        """
        if isinstance(content, bytes):
            content = base64.b64encode(content).decode("ascii")
            encoding = "base64"
        self._request(
            "PUT",
            f"/v1/sessions/{self.session_id}/files",
            json={"path": path, "content": content, "encoding": encoding},
        )

    def write_files(self, files: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Write multiple small files under /workspace in one call.

        PUT /v1/sessions/{id}/files/batch. Capped at 64 files / 8 MiB total
        content — for bulk import use artifacts capture (export) or exec/SSH
        instead. Not fail-fast: returns one {"path", "ok", "error"?} result per
        file, so one bad path doesn't sink the rest of the batch.

        Each entry in files is {"path": str, "content": str, "encoding"?: "text"|"base64"}.
        """
        resp = self._request(
            "PUT",
            f"/v1/sessions/{self.session_id}/files/batch",
            json={"files": files},
        )
        return resp.get("results", [])

    def find_files(
        self,
        pattern: str,
        *,
        path: str = "/workspace",
        max_results: int | None = None,
    ) -> dict[str, Any]:
        """Find file paths under path matching a glob.

        GET /v1/sessions/{id}/files?op=find

        pattern supports an exact path, "*"/"?"/"[...]" shallow globs, or a
        trailing "/**" for recursive. Returns {"paths": [...], "truncated": bool}
        — truncated is set (not an error) when max_results caps the result set.
        """
        params: dict[str, Any] = {"path": path, "op": "find", "pattern": pattern}
        if max_results:
            params["max_results"] = max_results
        resp = self._request(
            "GET",
            f"/v1/sessions/{self.session_id}/files",
            params=params,
        )
        return {
            "paths": resp.get("paths", []),
            "truncated": resp.get("truncated", False),
        }

    def grep_files(
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
        """Search file contents under path with a regex (Go RE2 syntax — no
        PCRE lookaround/backrefs).

        GET /v1/sessions/{id}/files?op=grep

        Returns {"matches": [{"path", "line_number", "line", "before"?, "after"?}, ...],
        "files_searched": int, "truncated": bool}. max_results caps the number
        of matching FILES (not match lines); truncated is set rather than
        raising when a cap is hit. Binary files are skipped automatically.
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
        resp = self._request(
            "GET",
            f"/v1/sessions/{self.session_id}/files",
            params=params,
        )
        return {
            "matches": resp.get("matches", []),
            "files_searched": resp.get("files_searched", 0),
            "truncated": resp.get("truncated", False),
        }

    def mkdir(self, path: str) -> None:
        """Create a directory (with parents) in the session filesystem.

        POST /v1/sessions/{id}/files/mkdir

        Always `mkdir -p` semantics: intermediate components are created as
        needed, and mkdir-ing an already-existing directory is not an error.
        """
        self._request(
            "POST",
            f"/v1/sessions/{self.session_id}/files/mkdir",
            json={"path": path},
        )

    def remove(self, path: str, *, recursive: bool = False) -> None:
        """Remove a file, or a directory (with recursive=True).

        DELETE /v1/sessions/{id}/files?path=<path>&recursive=<bool>

        Without recursive, a directory target must already be empty (rmdir
        semantics — a distinct "directory not empty" error rather than
        silently recursing); a file target is removed unconditionally either
        way. The workspace root itself can never be removed.
        """
        params: dict[str, Any] = {"path": path}
        if recursive:
            params["recursive"] = "true"
        self._request(
            "DELETE",
            f"/v1/sessions/{self.session_id}/files",
            params=params,
        )

    def rename(self, path: str, dest_path: str) -> None:
        """Rename or move a file or directory.

        POST /v1/sessions/{id}/files/rename

        Both path and dest_path must resolve under /workspace; the
        destination's parent directories are created as needed.
        """
        self._request(
            "POST",
            f"/v1/sessions/{self.session_id}/files/rename",
            json={"path": path, "dest_path": dest_path},
        )

    def upload_file(self, local_path: str | os.PathLike, remote_path: str) -> dict[str, Any]:
        """Stream a local file into the session filesystem — NOT subject to
        write_files' 64-file/8 MiB batch cap. Use this (not write_file) for a
        repo tarball, dataset, or any large/binary file.

        PUT /v1/sessions/{id}/files/stream

        Streams the file straight from disk (httpx reads it in bounded
        chunks as it sends) rather than loading the whole thing into memory
        first. Returns {"path", "bytes", "sha256", "duration_ms"} — sha256 is
        the server's digest of the received content, for round-trip
        verification against the local file's own hash.
        """
        headers = {"Authorization": f"Bearer {self._token}"}
        url = f"{self._base_url}/v1/sessions/{self.session_id}/files/stream"
        with open(local_path, "rb") as f:
            resp = self._client.request(
                "PUT", url, headers=headers, params={"path": remote_path}, content=f
            )
        if resp.status_code >= 400:
            _raise_for_status(resp)
        return resp.json()

    def download_file(self, remote_path: str, local_path: str | os.PathLike) -> dict[str, Any]:
        """Stream a file from the session filesystem to local disk — NOT
        subject to read_file's 4 MiB whole-file cap. Use this (not
        read_file) for large results/datasets.

        GET /v1/sessions/{id}/files/stream

        Streams the response body straight to disk via httpx's streaming
        request, computing a running sha256 as it writes, instead of
        buffering the whole file in memory. Returns {"path", "local_path",
        "bytes", "sha256"}.
        """
        headers = {"Authorization": f"Bearer {self._token}"}
        url = f"{self._base_url}/v1/sessions/{self.session_id}/files/stream"
        hasher = hashlib.sha256()
        total = 0
        with self._client.stream(
            "GET", url, headers=headers, params={"path": remote_path}
        ) as resp:
            if resp.status_code >= 400:
                resp.read()
                _raise_for_status(resp)
            with open(local_path, "wb") as f:
                for chunk in resp.iter_bytes():
                    f.write(chunk)
                    hasher.update(chunk)
                    total += len(chunk)
        return {
            "path": remote_path,
            "local_path": str(local_path),
            "bytes": total,
            "sha256": hasher.hexdigest(),
        }

    def _files(
        self,
        op: str,
        path: str,
        *,
        content: str | None = None,
        timeout_ms: int | None = None,
    ) -> dict[str, Any]:
        """Internal helper retained for backward compatibility."""
        if op == "write":
            payload: dict[str, Any] = {"path": path, "content": content or ""}
            return self._request("PUT", f"/v1/sessions/{self.session_id}/files", json=payload)
        else:
            return self._request(
                "GET",
                f"/v1/sessions/{self.session_id}/files",
                params={"path": path, "op": op},
            )

    # ── Fork / checkpoint ─────────────────────────────────────────────────────

    def fork(self, count: int = 1) -> ForkResult:
        """Fork this session into count independent children."""
        resp = self._request(
            "POST",
            f"/v1/sessions/{self.session_id}/fork",
            json={"count": count},
        )
        return ForkResult(
            parent_session_id=resp.get("parent_session_id", ""),
            children=list(resp.get("children", [])),
        )

    def checkpoint(
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
        resp = self._request(
            "POST",
            f"/v1/sessions/{self.session_id}/checkpoints",
            json=body,
        )
        return _map_checkpoint(resp)

    def checkpoints(self) -> list[CheckpointInfo]:
        """List all checkpoints captured from this session."""
        resp = self._request("GET", f"/v1/sessions/{self.session_id}/checkpoints")
        return [_map_checkpoint(c) for c in resp.get("checkpoints", [])]

    def restore(self, checkpoint_id: str) -> SessionInfo:
        """Roll this session back to a checkpoint in place."""
        resp = self._request(
            "POST",
            f"/v1/sessions/{self.session_id}/restore",
            json={"checkpoint_id": checkpoint_id},
        )
        return _map_session_info(resp)

    def save_as_template(
        self,
        name: str,
        *,
        description: str | None = None,
    ) -> TemplateInfo:
        """Promote this live session into a named, reusable template."""
        body: dict[str, Any] = {"name": name}
        if description:
            body["description"] = description
        resp = self._request(
            "POST",
            f"/v1/sessions/{self.session_id}/templates",
            json=body,
        )
        return _map_template(resp)

    # ── Archive ───────────────────────────────────────────────────────────────

    def archive_retry(self) -> ArchiveStatus:
        """Re-drive a stuck or failed durable upload for this paused session."""
        resp = self._request("POST", f"/v1/sessions/{self.session_id}/archive/retry")
        return ArchiveStatus(
            status=resp.get("status", ""),
            cold_uri=resp.get("cold_uri"),
            uploaded_at=resp.get("uploaded_at"),
        )

    # ── Ports ─────────────────────────────────────────────────────────────────

    def expose(
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
        resp = self._request(
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

    def unexpose(self, port: int) -> bool:
        """Remove a previously exposed port."""
        resp = self._request(
            "POST",
            f"/v1/sessions/{self.session_id}/unexpose",
            json={"port": port},
        )
        return bool(resp.get("ok", False))

    # ── Egress ────────────────────────────────────────────────────────────────

    def get_egress(self) -> dict[str, Any]:
        """Read the active egress policy for this session."""
        return self._request("GET", f"/v1/sessions/{self.session_id}/egress")

    def set_egress(
        self,
        *,
        mode: str = "deny",
        allow: list[str] | None = None,
    ) -> dict[str, Any]:
        """Replace the egress policy on this live session."""
        body: dict[str, Any] = {"mode": mode}
        if allow:
            body["allow"] = allow
        return self._request("POST", f"/v1/sessions/{self.session_id}/egress", json=body)

    # ── Artifacts ─────────────────────────────────────────────────────────────

    def capture_artifacts(
        self,
        paths: list[str],
        *,
        destination: dict[str, Any] | None = None,
    ) -> ArtifactInfo:
        """Capture output files from this session to durable storage."""
        body: dict[str, Any] = {"paths": paths}
        if destination:
            body["destination"] = destination
        resp = self._request(
            "POST",
            f"/v1/sessions/{self.session_id}/artifacts",
            json=body,
        )
        return _map_artifact(resp)

    def artifacts(self) -> list[ArtifactInfo]:
        """List artifact captures for this session."""
        resp = self._request("GET", f"/v1/sessions/{self.session_id}/artifacts")
        return [_map_artifact(a) for a in resp.get("artifacts", [])]

    # ── Batch (static) ────────────────────────────────────────────────────────

    @staticmethod
    def batch_exec(
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
        with httpx.Client(timeout=timeout) as client:
            headers = {"Authorization": f"Bearer {token}"}
            resp = client.post(
                f"{base_url.rstrip('/')}/v1/sessions/batch/exec",
                headers=headers,
                json=payload,
            )
            if resp.status_code >= 400:
                _raise_for_status(resp)
            data = resp.json()
        return _map_batch_results(data)

    # ── Internal ──────────────────────────────────────────────────────────────

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {self._token}"}
        resp = self._client.request(
            method,
            f"{self._base_url}{path}",
            headers=headers,
            params=params,
            **kwargs,
        )
        if resp.status_code >= 400:
            _raise_for_status(resp)
        if resp.status_code == 204:
            return {}
        return resp.json()

    def _map_session_info(self, resp: dict[str, Any]) -> SessionInfo:
        """Convenience wrapper retained for backward compatibility."""
        return _map_session_info(resp)


class InisClient:
    """One-shot operations backed by a throwaway session.

    Each call creates a session, runs the command, and tears it down. There is
    no separate stateless surface — a session is the only primitive.
    """

    def __init__(self, *, base_url: str, token: str, timeout: float = 120.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._client = httpx.Client(timeout=timeout)

    def exec(
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
        session = self._post("/v1/sessions", create)
        session_id = session["session_id"]
        try:
            payload: dict[str, Any] = {"command": command}
            if cwd:
                payload["cwd"] = cwd
            if timeout_ms:
                payload["timeout_ms"] = timeout_ms
            resp = self._post(f"/v1/sessions/{session_id}/exec", payload)
        finally:
            try:
                self._client.request(
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

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {self._token}"}
        resp = self._client.post(f"{self._base_url}{path}", headers=headers, json=payload)
        if resp.status_code >= 400:
            _raise_for_status(resp)
        return resp.json()

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "InisClient":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()


def _resolve_token(token: str | None) -> str:
    if token:
        return token
    env = os.environ.get("INIS_API_KEY")
    if not env:
        raise InisError("INIS_API_KEY is required")
    return env


def _resolve_base_url(base_url: str | None) -> str:
    return (base_url or os.environ.get("INIS_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")


class _SessionsAPI:
    def __init__(self, client: "Client") -> None:
        self._client = client

    def create(
        self,
        *,
        volume_id: str | None = None,
        max_lifetime_ms: int | None = None,
        idle_timeout_ms: int | None = None,
        name: str | None = None,
        labels: dict[str, str] | None = None,
        egress_default: str | None = None,
        egress_allow: list[str] | None = None,
        from_checkpoint: str | None = None,
        template: str | None = None,
        size: str | None = None,
        no_sudo: bool = False,
        on_pty_detach: str | OnPtyDetachPolicy | None = None,
        paused_ttl_ms: int | None = None,
        command_capture: str | None = None,
        env: dict[str, str] | None = None,
        secrets: dict[str, str] | None = None,
        connectors: list[str] | None = None,
        on_status: Callable[[str], None] | None = None,
        wait: bool = True,
    ) -> Session:
        """Create and start a new session.

        env sets plain, non-sensitive environment variables (config/flags)
        for every process this session's exec/PTY spawns. secrets sets
        sensitive values: encrypted at rest server-side, never logged, never
        echoed back (session.get().secret_names shows only the configured
        names, never values).

        connectors opts this session into stored egress connectors by name
        (see client.connectors) -- explicit, off by default. From
        inside the guest, call
        ``http://<its own default gateway>:18080/<name>/<path>`` and the
        host-side proxy injects the real credential; the guest never holds
        or sees it. A name not listed here gets a distinct "not enabled"
        error, even if the account has a connector by that name.

        Creation is asynchronous on the server: this call still
        blocks until the session is live by default (its historical
        contract), polling under the hood — a cold template pull can take
        tens of seconds. Pass on_status to observe the phase
        ("preparing_template", "starting", ...) as it progresses, or
        wait=False to get the handle back immediately (state may still be
        "creating") and poll session.get() yourself.
        """
        session = self._client.session(
            volume_id=volume_id,
            max_lifetime_ms=max_lifetime_ms,
            idle_timeout_ms=idle_timeout_ms,
            name=name,
            labels=labels,
            egress_default=egress_default,
            egress_allow=egress_allow,
            from_checkpoint=from_checkpoint,
            template=template,
            size=size,
            no_sudo=no_sudo,
            on_pty_detach=on_pty_detach,
            paused_ttl_ms=paused_ttl_ms,
            command_capture=command_capture,
            env=env,
            secrets=secrets,
            connectors=connectors,
            on_status=on_status,
            wait=wait,
        )
        try:
            session.__enter__()
        except Exception:
            session._client.close()
            raise
        return session

    def get(self, session_id: str) -> SessionInfo:
        """Fetch state for a session by ID without attaching to it."""
        resp = self._client._get(f"/v1/sessions/{session_id}")
        return _map_session_info(resp)

    def attach(self, session_id: str) -> Session:
        """Bind to an existing session using this client's base_url/token."""
        return Session.attach(
            session_id,
            base_url=self._client._base_url,
            token=self._client._token,
            timeout=self._client._timeout,
        )

    def list(
        self,
        *,
        state: str | None = None,
        limit: int | None = None,
        cursor: str | None = None,
    ) -> tuple[list[SessionInfo], str | None]:
        """List sessions, optionally filtered by state."""
        params: dict[str, Any] = {}
        if state:
            params["state"] = state
        if limit:
            params["limit"] = limit
        if cursor:
            params["cursor"] = cursor
        resp = self._client._get("/v1/sessions", params=params)
        sessions = [_map_session_info(item) for item in resp.get("sessions", [])]
        return sessions, resp.get("next_cursor")

    def read_file(self, session_id: str, path: str) -> str:
        """Read a file from a session filesystem by session ID."""
        resp = self._client._get(
            f"/v1/sessions/{session_id}/files",
            params={"path": path, "op": "read"},
        )
        return resp["content"]

    def list_files(self, session_id: str, path: str) -> list[str]:
        """List entries under path in a session filesystem by session ID."""
        resp = self._client._get(
            f"/v1/sessions/{session_id}/files",
            params={"path": path, "op": "list"},
        )
        return resp.get("entries", [])

    def write_file(
        self, session_id: str, path: str, content: str, encoding: str = "text"
    ) -> None:
        """Write content to a file in a session filesystem by session ID."""
        self._client._put(
            f"/v1/sessions/{session_id}/files",
            payload={"path": path, "content": content, "encoding": encoding},
        )

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
        if isinstance(command, str):
            command = ["bash", "-lc", command]
        payload: dict[str, Any] = {"session_ids": session_ids, "command": command}
        if cwd:
            payload["cwd"] = cwd
        if timeout_ms:
            payload["timeout_ms"] = timeout_ms
        resp = self._client._post("/v1/sessions/batch/exec", payload)
        return _map_batch_results(resp)


class _CheckpointsAPI:
    def __init__(self, client: "Client") -> None:
        self._client = client

    def get(self, checkpoint_id: str) -> CheckpointInfo:
        resp = self._client._get(f"/v1/checkpoints/{checkpoint_id}")
        return _map_checkpoint(resp)

    def delete(self, checkpoint_id: str) -> None:
        self._client._delete(f"/v1/checkpoints/{checkpoint_id}")

    def create_session(
        self,
        checkpoint_id: str,
        *,
        name: str | None = None,
        labels: dict[str, str] | None = None,
        max_lifetime_ms: int | None = None,
        idle_timeout_ms: int | None = None,
    ) -> Session:
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
        resp = self._client._post(f"/v1/checkpoints/{checkpoint_id}/sessions", body)
        return Session.attach(
            resp["session_id"],
            base_url=self._client.base_url,
            token=self._client.token,
            timeout=self._client.timeout,
        )


class _ArtifactsAPI:
    def __init__(self, client: "Client") -> None:
        self._client = client

    def get(self, artifact_id: str) -> ArtifactInfo:
        """Get an artifact manifest (status + pre-signed download URLs)."""
        resp = self._client._get(f"/v1/artifacts/{artifact_id}")
        return _map_artifact(resp)

    def extend(self, artifact_id: str, ttl_days: int) -> ArtifactInfo:
        """Push an artifact's expiry forward (capped at the retention limit)."""
        resp = self._client._post(
            f"/v1/artifacts/{artifact_id}/extend", {"ttl_days": ttl_days}
        )
        return _map_artifact(resp)

    def delete(self, artifact_id: str) -> None:
        self._client._delete(f"/v1/artifacts/{artifact_id}")


class _TemplatesAPI:
    def __init__(self, client: "Client") -> None:
        self._client = client

    def list(self) -> list[TemplateInfo]:
        """List the templates available to this account (official and user)."""
        resp = self._client._get("/v1/templates")
        return [_map_template(t) for t in resp.get("templates", [])]

    def import_image(
        self,
        from_image: str,
        name: str,
        description: str | None = None,
    ) -> TemplateInfo:
        """Import a PUBLIC OCI image as a custom template.

        The image (e.g. ``docker.io/library/alpine:3.20`` or
        ``python:3.12-slim``) is pulled, flattened into the VM rootfs, and given
        the inis guest agent. The build runs asynchronously: the returned
        template starts in ``status="building"`` and becomes usable once it
        flips to ``"ready"`` (poll :meth:`list`). Public registries only.
        """
        payload: dict[str, Any] = {"from_image": from_image, "name": name}
        if description is not None:
            payload["description"] = description
        resp = self._client._post("/v1/templates", payload)
        return _map_template(resp)

    def delete(self, name: str) -> None:
        """Delete a user template. Official templates cannot be deleted."""
        self._client._delete(f"/v1/templates/{name}")


class _RegistriesAPI:
    """Private-registry pull credentials for template import.

    Storing a credential here does not change how ``templates.import_image``
    is called: the import matches a stored credential to the pull by the
    image ref's registry host automatically.
    """

    def __init__(self, client: "Client") -> None:
        self._client = client

    def add(
        self,
        name: str,
        *,
        registry_host: str,
        username: str,
        secret: str,
    ) -> RegistryCredentialInfo:
        """Store a private-registry pull credential under ``name``.

        Covers Docker Hub, GHCR, and Google Artifact Registry (token/
        username-password auth). ``secret`` is encrypted at rest and never
        returned by any read — :meth:`list` only shows a last-4 fragment.
        Rotation is delete-then-create for v1: reusing a name already in use
        raises ``InisError`` (409).
        """
        payload = {
            "name": name,
            "registry_host": registry_host,
            "username": username,
            "secret": secret,
        }
        resp = self._client._post("/v1/registry-credentials", payload)
        return _map_registry_credential(resp)

    def list(self) -> list[RegistryCredentialInfo]:
        """List your stored registry credentials (redacted, never the secret)."""
        resp = self._client._get("/v1/registry-credentials")
        return [_map_registry_credential(c) for c in resp.get("credentials", [])]

    def delete(self, name: str) -> None:
        """Delete a stored registry credential. Idempotent."""
        self._client._delete(f"/v1/registry-credentials/{name}")


class _ConnectorsAPI:
    """Egress connectors: credential injection without the sandbox
    ever seeing the secret.

    Register a connector here, then opt a session into it by name at create
    time (``client.sessions.create(connectors=["stripe"])``). From inside
    that session's guest, call
    ``http://<its own default gateway>:18080/<name>/<path>`` -- the host-side
    proxy forwards to the connector's target with the credential injected.
    A session that did not list the name gets a distinct "not enabled"
    error; a connector belongs to your account only and is never reachable
    by a different account's session, even if it opts into the same name.
    """

    def __init__(self, client: "Client") -> None:
        self._client = client

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

        auth_shape is "bearer" (``Authorization: Bearer <secret>``) or
        "header" (a custom header named ``header_name`` set to the raw
        secret). ``secret`` is encrypted at rest and never returned by any
        read -- :meth:`list` only shows a last-4 fragment. Rotation is
        delete-then-create for v1: reusing a name already in use raises
        ``InisError`` (409).
        """
        payload: dict[str, Any] = {
            "name": name,
            "target_base_url": target_base_url,
            "auth_shape": auth_shape,
            "secret": secret,
        }
        if header_name is not None:
            payload["header_name"] = header_name
        resp = self._client._post("/v1/connectors", payload)
        return _map_connector(resp)

    def list(self) -> list[ConnectorInfo]:
        """List your stored connectors (redacted, never the secret)."""
        resp = self._client._get("/v1/connectors")
        return [_map_connector(c) for c in resp.get("connectors", [])]

    def delete(self, name: str) -> None:
        """Delete a stored connector. Idempotent."""
        self._client._delete(f"/v1/connectors/{name}")


class _WebhooksAPI:
    """Webhook subscriptions for session events.

    A subscription receives the same event envelope the account-scoped
    event stream (``GET /v1/events``) sends, signed with HMAC-SHA256 (see
    ``Inis-Signature: t=<ts>,v1=<hex>`` -- verify with
    ``hmac.new(secret, f"{t}.{raw_body}".encode(), "sha256").hexdigest()``)
    and retried with bounded exponential backoff on non-2xx/failure.
    """

    def __init__(self, client: "Client") -> None:
        self._client = client

    def add(self, url: str, *, event_types: list[str] | None = None) -> WebhookEndpointInfo:
        """Register ``url`` to receive signed event deliveries.

        ``event_types`` is an allowlist (e.g. ``["session.created"]``); omit
        or pass ``None``/``[]`` to receive every event type the stream
        carries. The returned object's ``secret`` is the ONLY time the raw
        signing secret is available.
        """
        payload: dict[str, Any] = {"url": url}
        if event_types:
            payload["event_types"] = event_types
        resp = self._client._post("/v1/org/webhooks", payload)
        return _map_webhook(resp)

    def list(self) -> list[WebhookEndpointInfo]:
        """List this org's webhook subscriptions (secret never included)."""
        resp = self._client._get("/v1/org/webhooks")
        return [_map_webhook(e) for e in resp.get("endpoints", [])]

    def delete(self, endpoint_id: str) -> None:
        """Delete a webhook subscription."""
        self._client._delete(f"/v1/org/webhooks/{endpoint_id}")

    def test(self, endpoint_id: str) -> WebhookDeliveryInfo:
        """Fire one synthetic ``webhook.test`` delivery immediately (bypassing
        the retry queue) and return the delivered/dead result synchronously.
        """
        resp = self._client._post(f"/v1/org/webhooks/{endpoint_id}/test", {})
        return _map_webhook_delivery(resp)

    def deliveries(self, endpoint_id: str, *, limit: int = 50) -> list[WebhookDeliveryInfo]:
        """The most recent deliveries for a subscription, newest first --
        the per-subscription delivery log."""
        resp = self._client._get(f"/v1/org/webhooks/{endpoint_id}/deliveries", params={"limit": limit})
        return [_map_webhook_delivery(d) for d in resp.get("deliveries", [])]


class _DomainsAPI:
    """Custom domains with auto-TLS for exposed session ports.

    v1 binds a domain directly to a (session_id, port) pair -- the same
    ephemeral unit preview URLs already use -- rather than a longer-lived
    project route; rebinding after a session is destroyed is a follow-up
    :meth:`route` call with the new session id.
    """

    def __init__(self, client: "Client") -> None:
        self._client = client

    def add(self, domain: str) -> DomainInfo:
        """Register ``domain`` as a pending (unverified) custom domain.
        The returned ``verify_txt_name``/``verify_txt_value`` are the TXT
        record to create before calling :meth:`verify` (or CNAME the domain
        to the fleet's ingress instead)."""
        resp = self._client._post("/v1/org/domains", {"domain": domain})
        return _map_domain(resp)

    def list(self) -> list[DomainInfo]:
        """List this org's registered domains, verified and unverified."""
        resp = self._client._get("/v1/org/domains")
        return [_map_domain(d) for d in resp.get("domains", [])]

    def delete(self, domain_id: str) -> None:
        """Delete a registered custom domain."""
        self._client._delete(f"/v1/org/domains/{domain_id}")

    def verify(self, domain_id: str) -> DomainInfo:
        """Run a live DNS check (TXT record, or CNAME to the fleet's
        ingress) and mark the domain verified on success. Safe to call
        repeatedly. Raises :class:`InisError` (409) if verification fails."""
        resp = self._client._post(f"/v1/org/domains/{domain_id}/verify", {})
        return _map_domain(resp)

    def route(self, domain_id: str, session_id: str, port: int) -> DomainInfo:
        """Bind a verified domain to ``session_id``'s exposed ``port``.
        Raises :class:`InisError` (409) if the domain isn't verified yet."""
        resp = self._client._put(f"/v1/org/domains/{domain_id}/route", {"session_id": session_id, "port": port})
        return _map_domain(resp)

    def unroute(self, domain_id: str) -> DomainInfo:
        """Clear a domain's session/port binding without deleting the
        registration -- verified state is preserved for a later rebind."""
        resp = self._client._delete_json(f"/v1/org/domains/{domain_id}/route")
        return _map_domain(resp)


class Client:
    """Primary SDK entry point for sessions and stateless execution."""

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
        self._http = httpx.Client(timeout=timeout)
        self.sessions = _SessionsAPI(self)
        self.checkpoints = _CheckpointsAPI(self)
        self.artifacts = _ArtifactsAPI(self)
        self.templates = _TemplatesAPI(self)
        self.registries = _RegistriesAPI(self)
        self.connectors = _ConnectorsAPI(self)
        self.webhooks = _WebhooksAPI(self)
        self.domains = _DomainsAPI(self)

    def close(self) -> None:
        """Close the underlying HTTP connection pool."""
        self._http.close()

    def __enter__(self) -> "Client":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()

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
        egress_default: str | None = None,
        egress_allow: list[str] | None = None,
        from_checkpoint: str | None = None,
        template: str | None = None,
        size: str | None = None,
        no_sudo: bool = False,
        on_pty_detach: str | OnPtyDetachPolicy | None = None,
        paused_ttl_ms: int | None = None,
        command_capture: str | None = None,
        env: dict[str, str] | None = None,
        secrets: dict[str, str] | None = None,
        connectors: list[str] | None = None,
        on_status: Callable[[str], None] | None = None,
        wait: bool = True,
    ) -> Session:
        """Return a Session configured for use as a context manager.

        on_status, if given, is called with each coarse provisioning phase
        ("preparing_template", "starting", ...) observed while waiting for
        the session to come up — most useful for a cold template
        pull, which can take tens of seconds. wait=False returns as soon as
        the create is acknowledged (session_id set, state left as whatever
        the server returned) instead of blocking until live; the caller is
        then responsible for polling get() itself.
        """
        return Session(
            base_url=self._base_url,
            token=self._token,
            volume_id=volume_id,
            max_lifetime_ms=max_lifetime_ms,
            idle_timeout_ms=idle_timeout_ms,
            name=name,
            labels=labels,
            egress_default=egress_default,
            egress_allow=egress_allow,
            from_checkpoint=from_checkpoint,
            template=template,
            size=size,
            no_sudo=no_sudo,
            on_pty_detach=on_pty_detach,
            paused_ttl_ms=paused_ttl_ms,
            command_capture=command_capture,
            env=env,
            secrets=secrets,
            connectors=connectors,
            timeout=self._timeout,
            on_status=on_status,
            wait=wait,
        )

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
        resp = self._post("/v1/sessions/exec", payload)
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

    def capacity(self) -> Capacity:
        """Return aggregate session capacity and limits for the account."""
        resp = self._get("/v1/org")
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

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {self._token}"}
        resp = self._http.post(
            f"{self._base_url}{path}",
            headers=headers,
            json=payload,
        )
        if resp.status_code >= 400:
            _raise_for_status(resp)
        return resp.json()

    def _put(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {self._token}"}
        resp = self._http.put(
            f"{self._base_url}{path}",
            headers=headers,
            json=payload,
        )
        if resp.status_code >= 400:
            _raise_for_status(resp)
        if resp.status_code == 204:
            return {}
        return resp.json()

    def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {self._token}"}
        resp = self._http.get(
            f"{self._base_url}{path}",
            headers=headers,
            params=params,
        )
        if resp.status_code >= 400:
            _raise_for_status(resp)
        return resp.json()

    def _delete(self, path: str) -> None:
        headers = {"Authorization": f"Bearer {self._token}"}
        resp = self._http.request("DELETE", f"{self._base_url}{path}", headers=headers)
        if resp.status_code >= 400:
            _raise_for_status(resp)

    def _delete_json(self, path: str) -> dict[str, Any]:
        """Like :meth:`_delete`, but for the (uncommon) DELETE endpoints that
        return a 200 + JSON body instead of 204 -- e.g. clearing a domain's
        route returns the updated domain, not an empty response."""
        headers = {"Authorization": f"Bearer {self._token}"}
        resp = self._http.request("DELETE", f"{self._base_url}{path}", headers=headers)
        if resp.status_code >= 400:
            _raise_for_status(resp)
        if resp.status_code == 204:
            return {}
        return resp.json()
