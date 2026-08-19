"""Guards against api/openapi.yaml growing a new response field that the
hand-written Python SDK never picks up.

Unlike the Go client (internal/inisapi/client.gen.go), this SDK is not
generated from the spec -- scripts/codegen/generate.sh's Python codegen
target (sdk/python/inis/_generated) is unused dead code that nothing in
inis.client imports (see the NOTE in that script). So "regenerate and diff"
cannot catch this SDK falling behind the spec; this test is the actual
drift check for it: walk each response dataclass's fields against its
OpenAPI schema's properties and fail if the schema has grown a property the
dataclass never reads.

Critically, the *set of schemas to check* is derived from api/openapi.yaml
itself (every schema reachable from a 2xx JSON response body, transitively
through nested $refs) rather than hand-maintained -- a newly added response
schema shows up here automatically, with no dataclass mapped to it, and
fails until it's either mapped (added to CASES) or deliberately excused
(added to NO_SDK_REPRESENTATION with a reason).
"""

from __future__ import annotations

from dataclasses import fields
from pathlib import Path
from typing import Any

import pytest
import yaml

from inis import client as _client

REPO_ROOT = Path(__file__).resolve().parents[3]
SPEC_PATH = REPO_ROOT / "api" / "openapi.yaml"

with open(SPEC_PATH) as _f:
    _SPEC = yaml.safe_load(_f)

_SCHEMAS: dict[str, Any] = _SPEC["components"]["schemas"]


def _collect_refs(node: Any) -> set[str]:
    """All component schema names $ref'd anywhere inside `node` (recursing
    through properties/items/allOf/oneOf/anyOf, but not descending past a
    $ref itself -- that name gets expanded separately by the caller)."""
    refs: set[str] = set()
    if isinstance(node, dict):
        if "$ref" in node:
            refs.add(node["$ref"].rsplit("/", 1)[-1])
            return refs
        for value in node.values():
            refs |= _collect_refs(value)
    elif isinstance(node, list):
        for item in node:
            refs |= _collect_refs(item)
    return refs


def _response_schema_names() -> set[str]:
    """Every schema in api/openapi.yaml reachable from a 2xx JSON response
    body, transitively (so a schema only ever nested inside another
    response schema -- e.g. EgressPolicy inside SessionResponse -- is
    included too)."""
    seed: set[str] = set()
    for methods in _SPEC["paths"].values():
        for method, op in methods.items():
            if method not in ("get", "post", "put", "patch", "delete"):
                continue
            for code, resp in (op.get("responses") or {}).items():
                if not str(code).startswith("2"):
                    continue
                for body in (resp.get("content") or {}).values():
                    seed |= _collect_refs(body.get("schema") or {})

    closure: set[str] = set()
    frontier = set(seed)
    while frontier:
        name = frontier.pop()
        if name in closure:
            continue
        closure.add(name)
        schema = _SCHEMAS.get(name)
        if schema is None:
            continue
        frontier |= _collect_refs(schema) - closure
    return closure


def _schema_properties(schema_name: str) -> set[str]:
    schema = _SCHEMAS[schema_name]
    return set((schema.get("properties") or {}).keys())


def _dataclass_field_names(cls: type) -> set[str]:
    return {f.name for f in fields(cls)}


# (dataclass, OpenAPI schema name, schema properties this dataclass
# deliberately omits -- keep this empty unless there's a documented reason).
CASES: list[tuple[type, str, set[str]]] = [
    (_client.SessionInfo, "SessionResponse", set()),
    (_client.TemplateInfo, "TemplateResponse", set()),
    (_client.DomainInfo, "DomainResponse", set()),
    (_client.WebhookDeliveryInfo, "WebhookDelivery", set()),
    (_client.WebhookEndpointInfo, "WebhookResponse", set()),
    (_client.ConnectorInfo, "ConnectorResponse", set()),
    (_client.RegistryCredentialInfo, "RegistryCredentialResponse", set()),
    (_client.CheckpointInfo, "CheckpointResponse", set()),
    (_client.ArtifactInfo, "ArtifactResponse", set()),
    (_client.ArtifactFile, "ArtifactFile", set()),
    (_client.ProcessInfo, "ProcessInfo", set()),
    (_client.EgressPolicy, "EgressPolicy", set()),
    (_client.ArchiveStatus, "ArchiveStatus", set()),
    (_client.ExposeResult, "SessionExposeResponse", set()),
    (_client.ForkResult, "ForkResponse", set()),
    (_client.Capacity, "CapacityCounts", set()),
    (_client.CapacityLimits, "CapacityLimits", set()),
    (_client.BatchExecResult, "BatchExecResult", set()),
    # ExecResult backs both exec() (ExecResponse) and run_code() (ExecuteResponse)
    # -- `truncated` has drifted out of api/openapi.yaml entirely for both
    # before, so both schemas get checked against the one dataclass here.
    (_client.ExecResult, "ExecResponse", set()),
    (_client.ExecResult, "ExecuteResponse", set()),
    # ProcessLogsResponse grew truncated/stdout_encoding/stderr_encoding
    # alongside ExecResponse/ExecuteResponse; covering it here too so it
    # doesn't silently drift the way other fields have for others.
    (_client.ProcessLogs, "ProcessLogsResponse", set()),
    # Connections: rotate_connection()/revoke_connection()
    # and SessionInfo.connections already exist and are checked above via
    # SessionResponse; these are the two nested schemas they return.
    (_client.ConnectionStatus, "ConnectionStatus", set()),
    (_client.ConnectionAllow, "ConnectionAllow", set()),
    (_client.VolumeInfo, "VolumeResponse", set()),
    (_client.UsageSummary, "UsageSummaryResponse", set()),
    (_client.SessionUsage, "SessionUsageResponse", set()),
]

_MAPPED_SCHEMAS = {schema_name for _, schema_name, _ in CASES}

# Response schemas with no per-field dataclass check, because the SDK
# deliberately doesn't expose them field-by-field. Each entry needs a real
# reason -- this is an excuse list, not a dumping ground; anything added
# here should be something a human decided, not something that fell out
# because nobody got around to a mapping.
NO_SDK_REPRESENTATION: dict[str, str] = {
    # List-wrapper responses: `{"<items>": [...], ...}`. The SDK's list()
    # methods return a bare list (sessions.list() also returns the cursor,
    # as a second tuple element) built from the already-covered item
    # schema; the wrapper itself carries no extra business field worth a
    # dedicated type.
    "SessionListResponse": "list() returns list[SessionInfo] + cursor tuple; item schema SessionResponse is checked separately",
    "TemplateListResponse": "list() returns list[TemplateInfo]; item schema TemplateResponse is checked separately",
    "ArtifactListResponse": "artifacts() returns list[ArtifactInfo]; item schema ArtifactResponse is checked separately",
    "CheckpointListResponse": "checkpoints() returns list[CheckpointInfo]; item schema CheckpointResponse is checked separately",
    "ConnectorListResponse": "list() returns list[ConnectorInfo]; item schema ConnectorResponse is checked separately",
    "DomainListResponse": "list() returns list[DomainInfo]; item schema DomainResponse is checked separately",
    "ProcessListResponse": "list_processes() returns list[ProcessInfo]; item schema ProcessInfo is checked separately",
    "RegistryCredentialListResponse": "list() returns list[RegistryCredentialInfo]; item schema RegistryCredentialResponse is checked separately",
    "WebhookListResponse": "list() returns list[WebhookEndpointInfo]; item schema WebhookResponse is checked separately",
    "WebhookDeliveryListResponse": "deliveries() returns list[WebhookDeliveryInfo]; item schema WebhookDelivery is checked separately",
    # File ops deliberately flatten to primitives (str / list[str] / bool)
    # rather than exposing the raw response shape -- see FilesAPI and
    # Session.read_file/list_files/write_file(s)/find_files/grep_files.
    "FileReadResponse": "read_file() returns the decoded content as str/bytes, not a response object",
    "FileWriteResponse": "write_file() returns None; the response is fire-and-forget",
    "FileListResponse": "list_files() returns list[str]",
    "FileFindResponse": "find_files() returns FindFilesResult(paths, truncated) built ad hoc from response fields",
    "FileGrepResponse": "grep_files() returns GrepFilesResult built ad hoc from response fields",
    "FileRenameResponse": "rename_file() returns None; the response is fire-and-forget",
    "FileOpResponse": "mkdir()/single-file ops return None; the response is fire-and-forget",
    "FileBatchWriteResponse": "write_files() returns list[dict] passthrough of per-file results, not a dataclass",
    "FileStreamWriteResponse": "streamed file writes return None; the response is fire-and-forget",
    "FileBatchResult": "per-item result inside FileBatchWriteResponse's passthrough list, never materialized as a dataclass",
    "GrepMatch": "grep_files() returns matches as plain dicts inside its ad hoc result, not a dataclass",
    # Endpoints this SDK doesn't wrap at all yet (feature gap, not field
    # drift -- see the module docstrings' "intentionally not exhaustive").
    "HealthResponse": "GET /v1/healthz is not wrapped by this SDK",
    # The session-token mint/list/revoke endpoints are deliberately not an SDK
    # surface in this release: callers that need to delegate access still proxy
    # it themselves rather than minting tokens, so these have no SDK consumer
    # yet. Wrap them once minting is a caller-facing operation.
    "SessionTokenMintResponse": "POST /v1/sessions/{id}/tokens is not wrapped by this SDK yet",
    "SessionTokenStatus": "the session-token endpoints are not wrapped by this SDK yet",
    "OTLPExportResponse": "the OTLP export config endpoints are not wrapped by this SDK",
    # OrgResponse only ever contributes its nested `capacity` object, which
    # IS checked (as CapacityCounts, via Capacity above); the envelope
    # itself has no other field the SDK surfaces.
    "OrgResponse": "capacity() only reads resp['capacity'] (checked as CapacityCounts); no other OrgResponse field is surfaced",
    # get_egress() intentionally returns the raw dict rather than an
    # EgressPolicy (unlike every other place egress appears, which is
    # nested inside a SessionResponse and goes through _map_egress).
    "SessionEgressResponse": "Session.get_egress()/set_egress() return the raw dict, not a mapped dataclass",
    # SessionInfo.self_api is the raw dict (like get_egress() above), not a
    # mapped dataclass -- there is no dedicated SDK wrapper for the guest
    # events channel yet; the self API is a separate HTTP surface inside
    # the VM (http://169.254.169.254), not something this SDK calls.
    "SelfAPIStatus": "SessionInfo.self_api exposes the raw dict; no dedicated SDK wrapper for the self API's guest events channel yet",
    # WebhookDeliveryInfo.attempts is `list[dict[str, Any]]` by design --
    # attempt records are passed through unparsed rather than typed.
    "WebhookDeliveryAttempt": "WebhookDeliveryInfo.attempts keeps raw dicts by design, not a dataclass",
    # BatchExecResponse is just `{"results": [BatchExecResult, ...]}`;
    # batch_exec() returns list[BatchExecResult] directly (checked above).
    "BatchExecResponse": "batch_exec() returns list[BatchExecResult] directly; item schema BatchExecResult is checked separately",
    # GET /v1/sessions/{id}/connections has no Python SDK method yet -- see
    # the "TODO: list_connections() / add_connection() ... land here once
    # the server verbs ship" comment above Session.rotate_connection() in
    # client.py. (the server verbs ship ahead of the SDK wiring, which is
    # follow-on work.) The item schema, ConnectionStatus, is already
    # checked above via SessionInfo.connections.
    "ConnectionListResponse": "list_connections() is not implemented yet -- see the TODO above Session.rotate_connection() in client.py (the server verb ships ahead of the SDK wiring, which is follow-on work)",
    # List-wrapper response: `{"volumes": [...]}`. volumes.list() returns
    # list[VolumeInfo] directly; the item schema VolumeResponse is checked
    # separately above.
    "VolumeListResponse": "volumes.list() returns list[VolumeInfo]; item schema VolumeResponse is checked separately",
}


@pytest.mark.parametrize("dc,schema_name,ignored", CASES, ids=[c[1] for c in CASES])
def test_dataclass_covers_schema_properties(
    dc: type, schema_name: str, ignored: set[str]
) -> None:
    missing = _schema_properties(schema_name) - ignored - _dataclass_field_names(dc)
    assert not missing, (
        f"{schema_name} in api/openapi.yaml has propert"
        f"{'y' if len(missing) == 1 else 'ies'} {sorted(missing)} that "
        f"{dc.__name__} does not expose (the spec changed but the "
        f"hand-written SDK wasn't updated to match). Add the field to "
        f"{dc.__name__} and its _map_* function in sdk/python/inis/client.py "
        f"(and the mirrored dataclass in client_stub.py)."
    )


def test_every_response_schema_is_covered_or_excused() -> None:
    """The inverse of test_dataclass_covers_schema_properties: catches a
    response schema api/openapi.yaml grows that nobody wired a dataclass
    check up for at all, rather than only catching missing fields on
    schemas someone remembered to list in CASES.

    A newly added response schema must be added to either CASES (mapped to
    a dataclass) or NO_SDK_REPRESENTATION (with a real reason) -- this test
    fails on it either way until someone makes that call.
    """
    schemas = _response_schema_names()
    uncovered = schemas - _MAPPED_SCHEMAS - set(NO_SDK_REPRESENTATION)
    assert not uncovered, (
        f"api/openapi.yaml has response schema"
        f"{'' if len(uncovered) == 1 else 's'} {sorted(uncovered)} that "
        f"{'is' if len(uncovered) == 1 else 'are'} neither checked against a "
        f"dataclass (add to CASES in this file) nor explicitly excused "
        f"(add to NO_SDK_REPRESENTATION in this file with a reason)."
    )


def test_no_sdk_representation_reasons_are_non_empty() -> None:
    """Keeps NO_SDK_REPRESENTATION an excuse list, not a silent dumping
    ground -- every entry has to justify itself."""
    empty = [name for name, reason in NO_SDK_REPRESENTATION.items() if not reason.strip()]
    assert not empty, f"NO_SDK_REPRESENTATION entries missing a reason: {empty}"
