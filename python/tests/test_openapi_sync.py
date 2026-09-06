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

import ast
from dataclasses import fields
from pathlib import Path
from typing import Any

import pytest
import yaml

from inis import client as _client

REPO_ROOT = Path(__file__).resolve().parents[3]
SPEC_PATH = REPO_ROOT / "api" / "openapi.yaml"
CLIENT_PY_PATH = REPO_ROOT / "sdk" / "python" / "inis" / "client.py"

with open(SPEC_PATH) as _f:
    _SPEC = yaml.safe_load(_f)

with open(CLIENT_PY_PATH) as _f:
    _CLIENT_PY_SRC = _f.read()

_CLIENT_PY_AST = ast.parse(_CLIENT_PY_SRC, filename=str(CLIENT_PY_PATH))

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


def _request_schema_names() -> set[str]:
    """Every schema in api/openapi.yaml reachable from a request body,
    transitively (so a schema only ever nested inside a request schema --
    e.g. ConnectionAuth inside ConnectionCreate -- is included too). Mirrors
    _response_schema_names() but seeds from op["requestBody"] instead of
    op["responses"]."""
    seed: set[str] = set()
    for methods in _SPEC["paths"].values():
        for method, op in methods.items():
            if method not in ("get", "post", "put", "patch", "delete"):
                continue
            request_body = op.get("requestBody")
            if not request_body:
                continue
            for body in (request_body.get("content") or {}).values():
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
    (_client.RequestEndpointInfo, "RequestEndpoint", set()),
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


# ── Request-body drift ───────────────────────────────────────────────────────
#
# Guards against api/openapi.yaml growing a new REQUEST field that the
# hand-written Python SDK never picks up -- the mirror image of the checks
# above, which only ever guarded response schemas. A request field can be
# added to the spec, wired into the Go client and the CLI, and quietly never
# reach a hand-written SDK's keyword arguments; CI stays green, and a caller
# simply has no way to send that field until someone notices by hand.
#
# Same architecture as the response-side checks above: the set of schemas to
# check is derived from api/openapi.yaml itself (every schema reachable from
# a request body, transitively), and every one of them must be either mapped
# to the SDK surface that sets its fields (FUNCTION_CASES / DATACLASS_CASES)
# or explicitly excused (NO_REQUEST_SDK_REPRESENTATION) with a reason.
#
# Deliberately NOT covered: query and path parameters (e.g. Sessions.list()'s
# state/limit/cursor/external_id, or a path id). A silently-dropped OPTIONAL
# field in a request BODY is invisible at the call site -- exactly the
# failure mode this section exists for, and exactly what the response-side
# checks above already guard for return values. An optional query parameter
# dropped from an SDK method is exactly as invisible at the call site -- this
# is a scope decision, not a claim that query/path parameters carry no such
# risk. A *required* path/query parameter is different: missing it breaks the
# method outright and is obvious immediately. It's scoped out here because
# this file's machinery (_flattened_request_properties(), the AST-based
# _function_param_names()) is built around request BODY schemas, and there is
# no equivalent "check params against the spec" mechanism to extend into for
# query/path parameters -- that would be new coverage, not a gap in what this
# section already claims to guard. Scope this to request BODIES, matching the
# mechanism this file already has.


def _function_param_names(class_name: str, func_name: str) -> set[str]:
    """Positional-or-keyword and keyword-only parameter names of
    `class_name.func_name` in client.py, via the AST rather than a regex --
    request-building methods have nested blocks/braces (if-statements,
    f-strings, dict literals) that make a text-bounded body extraction (as
    interfaceFieldNames() does for the TypeScript SDK's single-expression
    interface bodies) unreliable here. `self` is dropped; `*`/`**` markers
    aren't parameters."""
    for node in ast.walk(_CLIENT_PY_AST):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            for item in node.body:
                if isinstance(item, ast.FunctionDef) and item.name == func_name:
                    args = item.args
                    names = {a.arg for a in args.args if a.arg != "self"}
                    names |= {a.arg for a in args.kwonlyargs}
                    return names
            raise ValueError(f"{func_name}() not found on class {class_name} in client.py")
    raise ValueError(f"class {class_name} not found in client.py")


def _flattened_request_properties(schema_name: str, flatten: set[str]) -> set[str]:
    """schema_name's own top-level properties, plus -- for each property
    named in `flatten` that is itself a $ref to another object schema --
    that nested schema's properties in its place (one level). Matches how
    ConnectionSpec flattens ConnectionCreate's nested authentication/allow
    objects onto itself, so the check can still be derived from the spec
    instead of a hand-copied field list."""
    schema = _SCHEMAS[schema_name]
    props = schema.get("properties") or {}
    result = set(props.keys()) - flatten
    for name in flatten:
        ref = (props.get(name) or {}).get("$ref")
        if not ref:
            continue
        nested = _SCHEMAS[ref.rsplit("/", 1)[-1]]
        result |= set((nested.get("properties") or {}).keys())
    return result


# (dataclass, OpenAPI request schema name, schema properties this dataclass
# deliberately or currently doesn't expose).
REQUEST_DATACLASS_CASES: list[tuple[type, str, set[str]]] = [
    # Also used for ConnectionStatus.allow on the response side; the request
    # and response shapes are identical here.
    (_client.ConnectionAllow, "ConnectionAllow", set()),
]

# (class name, method name, OpenAPI request schema name, schema properties
# this method deliberately or currently doesn't expose -- a non-empty set
# needs a real reason in the comment above its entry: either "positional
# argument, can't be silently dropped without breaking the call" or a
# genuine known gap, tracked separately, in what the SDK currently exposes).
REQUEST_FUNCTION_CASES: list[tuple[str, str, str, set[str]]] = [
    # Known gap, tracked separately (docs/sdk-known-gaps.md): none of these
    # exist as settable keyword arguments on create() today, even though the
    # API accepts them at create time. Remove each field as it's added to
    # create()'s keyword arguments and the request payload.
    (
        "_SessionsAPI",
        "create",
        "SessionCreateRequest",
        {
            "idle_busy_cpu_threshold_pct",
            "idle_busy_network_bytes_per_sec",
            "artifacts",
            "project_id",
            "destroy_on_completion",
            # Not a gap: create() flattens EgressPolicy onto two renamed
            # kwargs (egress_default -> mode, egress_allow -> allow) instead
            # of taking a single `egress` argument -- verified present under
            # those names, just not named "egress" for this check to find.
            "egress",
        },
    ),
    # Known gap, tracked separately (docs/sdk-known-gaps.md) -- same fields,
    # missing from the checkpoint-restore path's keyword arguments too.
    (
        "_CheckpointsAPI",
        "create_session",
        "CheckpointSessionRequest",
        {
            "idle_mode",
            "idle_busy_cpu_threshold_pct",
            "idle_busy_network_bytes_per_sec",
            "wake_on_http",
            "project_id",
        },
    ),
    # Known gap, tracked separately (docs/sdk-known-gaps.md).
    ("Client", "execute", "ExecuteRequest", {"project_id", "connections"}),
    ("Session", "checkpoint", "CheckpointCreateRequest", set()),
    # name/command are Session.start_process(name, command, ...)'s
    # positional arguments.
    ("Session", "start_process", "ProcessStartRequest", {"name", "command"}),
    # port is Session.expose(port, ...)'s positional argument.
    ("Session", "expose", "SessionExposeRequest", {"port"}),
    ("Session", "exec", "SessionExecRequest", {"command"}),
    # session_ids/command are Session.batch_exec(session_ids, command, ...)'s
    # positional arguments.
    ("Session", "batch_exec", "BatchExecRequest", {"session_ids", "command"}),
    # name is Session.save_as_template(name, ...)'s positional argument.
    ("Session", "save_as_template", "TemplateCreateRequest", {"name"}),
    # from_image/name are _TemplatesAPI.import_image(from_image, name, ...)'s
    # positional arguments.
    ("_TemplatesAPI", "import_image", "TemplateImportRequest", {"from_image", "name"}),
    # name is _RegistriesAPI.add(name, ...)'s positional argument.
    ("_RegistriesAPI", "add", "RegistryCredentialCreateRequest", {"name"}),
    # name is _ConnectorsAPI.add(name, ...)'s positional argument.
    ("_ConnectorsAPI", "add", "ConnectorCreateRequest", {"name"}),
    # url is _WebhooksAPI.add(url, ...)'s positional argument.
    ("_WebhooksAPI", "add", "WebhookCreateRequest", {"url"}),
    ("_RequestEndpointAPI", "create", "RequestEndpointCreateRequest", {"url"}),
    # paths is Session.capture_artifacts(paths, ...)'s positional argument.
    ("Session", "capture_artifacts", "ArtifactCreateRequest", {"paths"}),
    ("_VolumesAPI", "create", "VolumeCreateRequest", set()),
]

_REQUEST_MAPPED_SCHEMAS = {schema_name for _, schema_name, _ in REQUEST_DATACLASS_CASES} | {
    schema_name for _, _, schema_name, _ in REQUEST_FUNCTION_CASES
} | {
    # Checked below by the dedicated ConnectionSpec flattening test, not the
    # generic per-field loops above -- ConnectionSpec flattens
    # ConnectionCreate's nested authentication object onto itself under
    # different field names, which the generic name-equality checks can't
    # express.
    "ConnectionCreate",
    "ConnectionAuth",
}

# Request schemas with no per-field check, because the SDK deliberately
# doesn't expose them field-by-field (or doesn't wrap the endpoint yet).
# Each entry needs a real reason -- this is an excuse list, not a dumping
# ground.
NO_REQUEST_SDK_REPRESENTATION: dict[str, str] = {
    # Nested inside SessionCreateRequest.artifacts, which Sessions.create()
    # doesn't accept at all yet -- same known gap as the ignored
    # `artifacts` entry in REQUEST_FUNCTION_CASES above.
    "ArtifactDeclaration": "reachable only via SessionCreateRequest.artifacts, which Sessions.create() doesn't accept yet (known gap, tracked separately in docs/sdk-known-gaps.md -- same as create()'s ignored `artifacts`)",
    # capture_artifacts()'s destination is an untyped dict passthrough, not
    # a dataclass -- no field names to check. Deliberately asymmetric with
    # the TypeScript SDK, which DOES field-check this same schema (as
    # ArtifactDestination in REQUEST_CASES): TypeScript's
    # CaptureArtifactsOptions.destination is typed as a named
    # `ArtifactDestination` interface (a closed literal with 4 known
    # fields), so a new schema property really would be an invisible,
    # silently-unsendable gap there. Python's capture_artifacts() has no
    # such closed type -- a caller can already pass any key in the dict --
    # so there is no equivalent gap for a field check to catch here.
    "ArtifactDestination": "Session.capture_artifacts(destination=...) accepts an untyped dict passthrough, not a dataclass -- no field names to check against (unlike the TypeScript SDK, whose CaptureArtifactsOptions.destination is a closed, named interface and is field-checked in REQUEST_CASES there)",
    # Schemas whose only properties are required and already positional
    # arguments the corresponding method can't be called without -- unlike
    # an optional field silently missing from a kwargs set, a dropped
    # required positional argument breaks the method outright and is
    # visible at every call site, so this isn't the failure mode this check
    # is about.
    "ArtifactExtendRequest": "Artifacts.extend(artifact_id, ttl_days) -- ttl_days is the sole, required, positional argument",
    "DomainCreateRequest": "Domains.add(domain) -- domain is the sole, required, positional argument",
    "DomainRouteRequest": "Domains.route(domain_id, session_id, port) -- session_id/port are both required, positional arguments",
    "SessionFileMkdirRequest": "Session.mkdir(path) -- path is the sole, required, positional argument",
    "SessionFileRenameRequest": "Session.rename(path, dest_path) -- both required, positional arguments",
    "SessionForkRequest": "Session.fork(count=1) -- count is the sole property, and the sole positional argument",
    "SessionRestoreRequest": "Session.restore(checkpoint_id) -- checkpoint_id is the sole, required, positional argument",
    "SessionUnexposeRequest": "Session.unexpose(port) -- port is the sole property, and the sole, positional argument",
    "VolumeResizeRequest": "Volumes.resize(volume_id, size_gb) -- size_gb is the sole, required, positional argument",
    # files is the sole, required property -- an untyped dict-list
    # passthrough (Session.write_files(files: list[dict])), not a dataclass.
    "SessionFileBatchPutRequest": "Session.write_files(files) -- files is an untyped list[dict] passthrough, not a dataclass; no field names to check",
    "FileBatchItem": "the items inside Session.write_files(files)'s untyped list[dict] passthrough are never materialized as a dataclass -- see SessionFileBatchPutRequest's excuse",
    # path/content/encoding are all plain positional/default parameters of
    # Session.write_file(path, content, encoding="text") -- no dataclass or
    # kwargs-only set to check field names against in the same way as the
    # rest of this file, but all 3 are visibly present in the 3-argument
    # signature (checked by _function_param_names would work too, but this
    # mirrors the TypeScript SDK's equivalent excuse for symmetry between
    # the two SDKs' documented gaps/excuses).
    "SessionFilePutRequest": "Session.write_file(path, content, encoding='text') -- all 3 properties are plain positional/default parameters; verified present by inspection",
    # Already checked, byte-for-byte the same component, via this file's
    # response-side CASES (EgressPolicy is used identically for both the
    # session response's nested egress field and set_egress()'s input).
    "EgressPolicy": "identical schema/dataclass already checked via this file's response-side CASES above",
    # Endpoints this SDK doesn't wrap at all -- matches this file's
    # response-side excuses for the same endpoints.
    "OTLPExportSetRequest": "the OTLP export config endpoints are not wrapped by this SDK (see OTLPExportResponse's excuse above)",
    "SessionTokenMintRequest": "POST /v1/sessions/{id}/tokens is not wrapped by this SDK yet (see SessionTokenMintResponse's excuse above)",
}


@pytest.mark.parametrize(
    "dc,schema_name,ignored", REQUEST_DATACLASS_CASES, ids=[c[1] for c in REQUEST_DATACLASS_CASES]
)
def test_dataclass_covers_request_schema_properties(
    dc: type, schema_name: str, ignored: set[str]
) -> None:
    missing = _schema_properties(schema_name) - ignored - _dataclass_field_names(dc)
    assert not missing, (
        f"{schema_name} in api/openapi.yaml has request propert"
        f"{'y' if len(missing) == 1 else 'ies'} {sorted(missing)} that "
        f"{dc.__name__} does not expose (so a caller has no way to send "
        f"this field). Add the field to {dc.__name__} in "
        f"sdk/python/inis/client.py."
    )


@pytest.mark.parametrize(
    "class_name,func_name,schema_name,ignored",
    REQUEST_FUNCTION_CASES,
    ids=[f"{c[0]}.{c[1]}::{c[2]}" for c in REQUEST_FUNCTION_CASES],
)
def test_function_covers_request_schema_properties(
    class_name: str, func_name: str, schema_name: str, ignored: set[str]
) -> None:
    missing = _schema_properties(schema_name) - ignored - _function_param_names(class_name, func_name)
    assert not missing, (
        f"{schema_name} in api/openapi.yaml has request propert"
        f"{'y' if len(missing) == 1 else 'ies'} {sorted(missing)} that "
        f"{class_name}.{func_name}() does not expose (so a caller has no "
        f"way to send this field). Add the field to {func_name}()'s "
        f"keyword arguments and its request payload in sdk/python/inis/"
        f"client.py."
    )


def test_connection_spec_covers_connection_create_and_nested_schemas() -> None:
    """ConnectionSpec (client.py) flattens ConnectionCreate's nested
    `authentication` object into top-level fields under different names
    (`authentication.type` -> `auth_type`) -- the generic name-equality
    checks above can't express that rename, so this is checked by hand.
    Still derived from the spec (not a hand-copied field list), so a
    genuinely new field on ConnectionCreate or ConnectionAuth still fails
    here."""
    flattened = _flattened_request_properties("ConnectionCreate", {"authentication", "allow"})
    renamed = {"auth_type" if p == "type" else p for p in flattened}
    dataclass_fields = _dataclass_field_names(_client.ConnectionSpec)
    missing = renamed - dataclass_fields
    assert not missing, (
        f"ConnectionCreate/ConnectionAuth have propert{'y' if len(missing) == 1 else 'ies'} "
        f"{sorted(missing)} (after the known authentication.type -> auth_type rename) that "
        f"ConnectionSpec does not expose. Add the field to ConnectionSpec in "
        f"sdk/python/inis/client.py."
    )


def test_every_request_schema_is_covered_or_excused() -> None:
    """The inverse of the per-field checks above: catches a request schema
    api/openapi.yaml grows that nobody wired a check up for at all, rather
    than only catching missing fields on schemas someone remembered to
    list. A newly added request schema must be added to REQUEST_
    DATACLASS_CASES/REQUEST_FUNCTION_CASES (mapped) or
    NO_REQUEST_SDK_REPRESENTATION (with a real reason) -- this test fails
    on it either way until someone makes that call.
    """
    schemas = _request_schema_names()
    uncovered = schemas - _REQUEST_MAPPED_SCHEMAS - set(NO_REQUEST_SDK_REPRESENTATION)
    assert not uncovered, (
        f"api/openapi.yaml has request schema"
        f"{'' if len(uncovered) == 1 else 's'} {sorted(uncovered)} that "
        f"{'is' if len(uncovered) == 1 else 'are'} neither checked against an SDK "
        f"surface (add to REQUEST_DATACLASS_CASES/REQUEST_FUNCTION_CASES in this "
        f"file) nor explicitly excused (add to NO_REQUEST_SDK_REPRESENTATION in "
        f"this file with a reason)."
    )


def test_no_request_sdk_representation_reasons_are_non_empty() -> None:
    """Keeps NO_REQUEST_SDK_REPRESENTATION an excuse list, not a silent
    dumping ground -- every entry has to justify itself."""
    empty = [name for name, reason in NO_REQUEST_SDK_REPRESENTATION.items() if not reason.strip()]
    assert not empty, f"NO_REQUEST_SDK_REPRESENTATION entries missing a reason: {empty}"
