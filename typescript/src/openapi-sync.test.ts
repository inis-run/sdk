// Guards against api/openapi.yaml growing a new response field that the
// hand-written TypeScript SDK never picks up.
//
// Unlike the Go client (internal/inisapi/client.gen.go), this SDK is not
// generated from the spec -- scripts/codegen/generate.sh's TypeScript
// codegen target (src/generated/, gitignored) is unused dead code that
// nothing here imports (see the NOTE in that script). So "regenerate and
// diff" cannot catch this SDK falling behind the spec; this test is the
// actual drift check for it: extract each response interface's field names
// from client.ts, convert to snake_case, and fail if the corresponding
// OpenAPI schema has grown a property the interface never exposes.
//
// Critically, the *set of schemas to check* is derived from api/openapi.yaml
// itself (every schema reachable from a 2xx JSON response body, transitively
// through nested $refs) rather than hand-maintained -- a newly added
// response schema shows up here automatically, with no interface mapped to
// it, and fails until it's either mapped (added to CASES) or deliberately
// excused (added to NO_SDK_REPRESENTATION with a reason).

import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { parse as parseYaml } from "yaml";
import { describe, expect, it } from "vitest";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = path.resolve(__dirname, "../../..");
const SPEC_PATH = path.resolve(REPO_ROOT, "api/openapi.yaml");
const CLIENT_SRC = readFileSync(path.resolve(__dirname, "client.ts"), "utf8");

interface OpenApiSchema {
  $ref?: string;
  properties?: Record<string, OpenApiSchema>;
  items?: OpenApiSchema;
  allOf?: OpenApiSchema[];
  oneOf?: OpenApiSchema[];
  anyOf?: OpenApiSchema[];
}

interface OpenApiOperation {
  responses?: Record<string, { content?: Record<string, { schema?: OpenApiSchema }> }>;
}

interface OpenApiSpec {
  paths: Record<string, Record<string, OpenApiOperation>>;
  components: { schemas: Record<string, OpenApiSchema> };
}

const SPEC = parseYaml(readFileSync(SPEC_PATH, "utf8")) as OpenApiSpec;
const SCHEMAS = SPEC.components.schemas;

const HTTP_METHODS = new Set(["get", "post", "put", "patch", "delete"]);

// All component schema names $ref'd anywhere inside `node` (recursing
// through properties/items/allOf/oneOf/anyOf, but not descending past a
// $ref itself -- that name gets expanded separately by the caller).
// Walks the node generically rather than checking a fixed set of keys
// (properties/items/allOf/oneOf/anyOf). An enumerated key list is the same
// failure mode this whole file exists to remove: it silently misses any
// construct nobody thought to add -- `additionalProperties: {$ref: ...}` was
// exactly such a miss, and it made this suite weaker than the Python one,
// whose walker was already generic. Anything holding a $ref, at any depth,
// is now reachable.
function collectRefs(node: unknown): Set<string> {
  const refs = new Set<string>();
  const visit = (n: unknown): void => {
    if (!n || typeof n !== "object") return;
    if (Array.isArray(n)) {
      for (const item of n) visit(item);
      return;
    }
    const obj = n as Record<string, unknown>;
    if (typeof obj.$ref === "string") {
      refs.add(obj.$ref.split("/").pop() as string);
      return;
    }
    for (const v of Object.values(obj)) visit(v);
  };
  visit(node);
  return refs;
}

// Every schema in api/openapi.yaml reachable from a 2xx JSON response body,
// transitively (so a schema only ever nested inside another response
// schema -- e.g. EgressPolicy inside SessionResponse -- is included too).
function responseSchemaNames(): Set<string> {
  const seed = new Set<string>();
  for (const methods of Object.values(SPEC.paths)) {
    for (const [method, op] of Object.entries(methods)) {
      if (!HTTP_METHODS.has(method)) continue;
      for (const [code, resp] of Object.entries(op.responses ?? {})) {
        if (!code.startsWith("2")) continue;
        for (const body of Object.values(resp.content ?? {})) {
          for (const r of collectRefs(body.schema)) seed.add(r);
        }
      }
    }
  }

  const closure = new Set<string>();
  const frontier = [...seed];
  while (frontier.length > 0) {
    const name = frontier.pop() as string;
    if (closure.has(name)) continue;
    closure.add(name);
    const schema = SCHEMAS[name];
    if (!schema) continue;
    for (const r of collectRefs(schema)) if (!closure.has(r)) frontier.push(r);
  }
  return closure;
}

// Every schema in api/openapi.yaml reachable from a request body, transitively
// (so a schema only ever nested inside a request schema -- e.g. ConnectionAuth
// inside ConnectionCreate -- is included too). Mirrors responseSchemaNames()
// but seeds from op.requestBody instead of op.responses.
function requestSchemaNames(): Set<string> {
  const seed = new Set<string>();
  for (const methods of Object.values(SPEC.paths)) {
    for (const [method, op] of Object.entries(methods) as [string, OpenApiOperation & {
      requestBody?: { content?: Record<string, { schema?: OpenApiSchema }> };
    }][]) {
      if (!HTTP_METHODS.has(method)) continue;
      for (const body of Object.values(op.requestBody?.content ?? {})) {
        for (const r of collectRefs(body.schema)) seed.add(r);
      }
    }
  }

  const closure = new Set<string>();
  const frontier = [...seed];
  while (frontier.length > 0) {
    const name = frontier.pop() as string;
    if (closure.has(name)) continue;
    closure.add(name);
    const schema = SCHEMAS[name];
    if (!schema) continue;
    for (const r of collectRefs(schema)) if (!closure.has(r)) frontier.push(r);
  }
  return closure;
}

function schemaProperties(schemaName: string): Set<string> {
  const schema = SCHEMAS[schemaName];
  if (!schema) throw new Error(`schema ${schemaName} not found in api/openapi.yaml`);
  return new Set(Object.keys(schema.properties ?? {}));
}

// Textually extracts `fieldName?:` / `fieldName:` declarations from one
// `export interface <name> { ... }` block in client.ts. Good enough for this
// file's consistent style (one field per line, no destructuring) without
// pulling in a TS compiler/AST dependency just for a test.
function interfaceFieldNames(interfaceName: string): Set<string> {
  const re = new RegExp(`export interface ${interfaceName} \\{([\\s\\S]*?)\\n\\}`, "m");
  const match = CLIENT_SRC.match(re);
  if (!match) throw new Error(`interface ${interfaceName} not found in client.ts`);
  const body = match[1];
  const fieldRe = /^\s*([A-Za-z_][A-Za-z0-9_]*)\??:\s*/gm;
  const fields = new Set<string>();
  for (const m of body.matchAll(fieldRe)) fields.add(m[1]);
  return fields;
}

// client.ts fields are camelCase; api/openapi.yaml properties are
// snake_case. Digits are left attached (secretLast4 -> secret_last4),
// matching this codebase's naming (no camelCase acronym runs to worry
// about).
function camelToSnake(field: string): string {
  return field.replace(/[A-Z]/g, (c) => `_${c.toLowerCase()}`);
}

// (TS interface name, OpenAPI schema name, schema properties this interface
// deliberately omits -- keep this empty unless there's a documented reason).
const CASES: [string, string, string[]][] = [
  ["SessionInfo", "SessionResponse", []],
  ["TemplateInfo", "TemplateResponse", []],
  ["DomainInfo", "DomainResponse", []],
  ["WebhookDeliveryInfo", "WebhookDelivery", []],
  ["WebhookEndpointInfo", "WebhookResponse", []],
  ["RequestEndpointInfo", "RequestEndpoint", []],
  ["ConnectorInfo", "ConnectorResponse", []],
  ["RegistryCredentialInfo", "RegistryCredentialResponse", []],
  ["CheckpointInfo", "CheckpointResponse", []],
  ["ArtifactInfo", "ArtifactResponse", []],
  ["ArtifactFile", "ArtifactFile", []],
  ["ProcessInfo", "ProcessInfo", []],
  ["EgressPolicy", "EgressPolicy", []],
  ["ArchiveStatus", "ArchiveStatus", []],
  ["ExposeResult", "SessionExposeResponse", []],
  ["ForkResult", "ForkResponse", []],
  ["Capacity", "CapacityCounts", []],
  ["CapacityLimits", "CapacityLimits", []],
  ["BatchExecResult", "BatchExecResult", []],
  ["GrepMatch", "GrepMatch", []],
  // findFiles()/grepFiles() drop the request's own echoed `path` and the
  // server's internal `duration_ms` -- everything else the schema returns
  // is exposed.
  ["FindFilesResult", "FileFindResponse", ["path", "duration_ms"]],
  ["GrepFilesResult", "FileGrepResponse", ["path", "duration_ms"]],
  // writeFiles() maps each result item into FileBatchResult; the wrapper
  // (FileBatchWriteResponse) itself is excused below.
  ["FileBatchResult", "FileBatchResult", []],
  // ExecResult backs both exec() (ExecResponse) and runCode() (ExecuteResponse)
  // -- `truncated` has drifted out of api/openapi.yaml entirely for both
  // before, so both schemas get checked against the one interface here.
  ["ExecResult", "ExecResponse", []],
  ["ExecResult", "ExecuteResponse", []],
  // ProcessLogsResponse grew truncated/stdout_encoding/stderr_encoding
  // alongside ExecResponse/ExecuteResponse; covering it here too so it
  // doesn't silently drift the way other fields have for others.
  ["ProcessLogs", "ProcessLogsResponse", []],
  // Connections: rotateConnection()/revokeConnection() and
  // SessionInfo.connections already exist and are checked above via
  // SessionResponse; these are the two nested schemas they return.
  ["ConnectionStatus", "ConnectionStatus", []],
  ["ConnectionAllow", "ConnectionAllow", []],
  ["VolumeInfo", "VolumeResponse", []],
  ["UsageSummary", "UsageSummaryResponse", []],
  ["SessionUsage", "SessionUsageResponse", []],
];

const MAPPED_SCHEMAS = new Set(CASES.map(([, schemaName]) => schemaName));

// Response schemas with no per-field interface check, because the SDK
// deliberately doesn't expose them field-by-field. Each entry needs a real
// reason -- this is an excuse list, not a dumping ground; anything added
// here should be something a human decided, not something that fell out
// because nobody got around to a mapping.
const NO_SDK_REPRESENTATION: Record<string, string> = {
  // List-wrapper responses: `{"<items>": [...], ...}`. The SDK's list
  // methods return a bare array built from the already-covered item
  // schema; the wrapper itself carries no extra business field worth a
  // dedicated type.
  SessionListResponse:
    "listSessions() returns SessionInfo[]; item schema SessionResponse is checked separately",
  TemplateListResponse:
    "listTemplates() returns TemplateInfo[]; item schema TemplateResponse is checked separately",
  ArtifactListResponse:
    "artifacts() returns ArtifactInfo[]; item schema ArtifactResponse is checked separately",
  CheckpointListResponse:
    "checkpoints() returns CheckpointInfo[]; item schema CheckpointResponse is checked separately",
  ConnectorListResponse:
    "listConnectors() returns ConnectorInfo[]; item schema ConnectorResponse is checked separately",
  DomainListResponse:
    "listDomains() returns DomainInfo[]; item schema DomainResponse is checked separately",
  ProcessListResponse:
    "listProcesses() returns ProcessInfo[]; item schema ProcessInfo is checked separately",
  RegistryCredentialListResponse:
    "listRegistryCredentials() returns RegistryCredentialInfo[]; item schema RegistryCredentialResponse is checked separately",
  WebhookListResponse:
    "listWebhooks() returns WebhookEndpointInfo[]; item schema WebhookResponse is checked separately",
  WebhookDeliveryListResponse:
    "webhookDeliveries() returns WebhookDeliveryInfo[]; item schema WebhookDelivery is checked separately",
  // File ops deliberately flatten to primitives (string / string[] /
  // boolean) rather than exposing the raw response shape.
  FileReadResponse: "readFile() returns the decoded content as a string, not a response object",
  FileWriteResponse: "writeFile() returns void; the response is fire-and-forget",
  FileListResponse: "listFiles() returns string[]",
  FileRenameResponse: "renameFile() returns void; the response is fire-and-forget",
  FileOpResponse: "mkdir()/single-file ops return void; the response is fire-and-forget",
  FileBatchWriteResponse:
    "writeFiles() returns FileBatchResult[] built from the response's per-file results (item schema FileBatchResult is checked separately); duration_ms is dropped",
  FileStreamWriteResponse: "streamed file writes return void; the response is fire-and-forget",
  // Endpoints this SDK doesn't wrap at all yet (feature gap, not field
  // drift -- see the module comments' "intentionally not exhaustive").
  HealthResponse: "GET /v1/healthz is not wrapped by this SDK",
  // The session-token mint/list/revoke endpoints are deliberately not an SDK
  // surface in this release: callers that need to delegate access still proxy
  // it themselves rather than minting tokens, so these have no SDK consumer
  // yet. Wrap them once minting is a caller-facing operation.
  SessionTokenMintResponse: "POST /v1/sessions/{id}/tokens is not wrapped by this SDK yet",
  SessionTokenStatus: "the session-token endpoints are not wrapped by this SDK yet",
  OTLPExportResponse: "the OTLP export config endpoints are not wrapped by this SDK",
  // OrgResponse only ever contributes its nested `capacity` object, which
  // IS checked (as CapacityCounts, via Capacity above); the envelope
  // itself has no other field the SDK surfaces.
  OrgResponse:
    "capacity() only reads data.capacity (checked as CapacityCounts); no other OrgResponse field is surfaced",
  // getEgress()/setEgress() intentionally return EgressPolicy directly --
  // this entry is for the enclosing session-egress response envelope name
  // as it appears in the spec's session-scoped egress endpoints, which
  // carries no extra field beyond what EgressPolicy already checks.
  SessionEgressResponse:
    "getEgress()/setEgress() map straight to EgressPolicy (checked separately); session_id is not surfaced",
  // SessionInfo.selfApi is the raw response object (like getEgress() above
  // returns EgressPolicy's raw shape), not a mapped interface -- there is
  // no dedicated SDK wrapper for the guest events channel yet; the self
  // API is a separate HTTP surface inside the VM
  // (http://169.254.169.254), not something this SDK calls.
  SelfAPIStatus:
    "SessionInfo.selfApi exposes the raw response object; no dedicated SDK wrapper for the self API's guest events channel yet",
  // mapWebhookDelivery() DOES map all 4 fields (attempt, at,
  // status_code->statusCode, error) -- but into an inline object-literal
  // type on WebhookDeliveryInfo.attempts, not a standalone `export
  // interface`, so interfaceFieldNames() (which only parses named
  // interfaces out of client.ts) can't check it as a CASES entry.
  WebhookDeliveryAttempt:
    "attempts is mapped inline in WebhookDeliveryInfo.attempts (all 4 fields covered), not as a standalone named interface, so it can't be listed in CASES",
  // (GrepMatch has its own interface and is checked in CASES above, not excused here.)
  // BatchExecResponse is just `{"results": [BatchExecResult, ...]}`;
  // batchExec() returns BatchExecResult[] directly (checked above).
  BatchExecResponse:
    "batchExec() returns BatchExecResult[] directly; item schema BatchExecResult is checked separately",
  // GET /v1/sessions/{id}/connections has no TypeScript SDK method yet --
  // client.ts's rotateConnection() docstring notes "Not implemented on
  // this client yet: adding a brand-new Connection to an already-live
  // session, and listing a session's Connections independently of
  // Session.get()'s redacted connections field -- both are landing
  // separately." (the server verbs ship ahead of the SDK wiring, which is
  // follow-on work.) The item schema, ConnectionStatus, is already
  // checked above via SessionInfo.connections.
  ConnectionListResponse:
    "listConnections() is not implemented yet -- see the 'Not implemented on this client yet' note on Session.rotateConnection() in client.ts (the server verb ships ahead of the SDK wiring, which is follow-on work)",
  // List-wrapper response: `{"volumes": [...]}`. volumes.list() returns
  // VolumeInfo[] directly; item schema VolumeResponse is checked separately.
  VolumeListResponse:
    "volumes.list() returns VolumeInfo[]; item schema VolumeResponse is checked separately",
};

describe("client.ts stays in sync with api/openapi.yaml", () => {
  for (const [ifaceName, schemaName, ignored] of CASES) {
    it(`${ifaceName} exposes every ${schemaName} property`, () => {
      const schemaProps = schemaProperties(schemaName);
      const ifaceSnakeFields = new Set(
        [...interfaceFieldNames(ifaceName)].map(camelToSnake),
      );
      const missing = [...schemaProps].filter(
        (p) => !ignored.includes(p) && !ifaceSnakeFields.has(p),
      );
      expect(
        missing,
        `${schemaName} has field(s) ${JSON.stringify(missing)} that ${ifaceName} ` +
          `does not expose (the spec changed but the hand-written SDK wasn't ` +
          `updated to match). Add the field to the ${ifaceName} interface and its ` +
          `map*() function in src/client.ts (and the mirrored interface in ` +
          `client.stub.ts).`,
      ).toEqual([]);
    });
  }

  it("every response schema is mapped to an interface or explicitly excused", () => {
    // The inverse of the per-field checks above: catches a response schema
    // api/openapi.yaml grows that nobody wired an interface check up for at
    // all, rather than only catching missing fields on schemas someone
    // remembered to list in CASES. A newly added response schema must be
    // added to either CASES (mapped to an interface) or
    // NO_SDK_REPRESENTATION (with a real reason) -- this fails on it either
    // way until someone makes that call.
    const schemas = responseSchemaNames();
    const excused = new Set(Object.keys(NO_SDK_REPRESENTATION));
    const uncovered = [...schemas].filter((s) => !MAPPED_SCHEMAS.has(s) && !excused.has(s));
    expect(
      uncovered.sort(),
      `api/openapi.yaml has response schema(s) ${JSON.stringify(uncovered)} that are ` +
        `neither checked against an interface (add to CASES) nor explicitly excused ` +
        `(add to NO_SDK_REPRESENTATION with a reason).`,
    ).toEqual([]);
  });

  it("every NO_SDK_REPRESENTATION entry has a non-empty reason", () => {
    // Keeps NO_SDK_REPRESENTATION an excuse list, not a silent dumping
    // ground -- every entry has to justify itself.
    const empty = Object.entries(NO_SDK_REPRESENTATION)
      .filter(([, reason]) => !reason.trim())
      .map(([name]) => name);
    expect(empty).toEqual([]);
  });

  it("collectRefs reaches a $ref through any construct, not a fixed key list", () => {
    // The first version of this walker checked only properties/items/allOf/
    // oneOf/anyOf, so a schema reachable solely via additionalProperties was
    // invisible here while the Python suite caught it -- the two suites
    // disagreed, and TS was the weaker one. These cases pin the generic walk;
    // additionalProperties is the one that actually regressed, the rest guard
    // the shape of the fix rather than any specific key.
    expect([...collectRefs({ additionalProperties: { $ref: "#/components/schemas/Deep" } })]).toEqual(
      ["Deep"],
    );
    expect([...collectRefs({ properties: { a: { $ref: "#/components/schemas/Deep" } } })]).toEqual([
      "Deep",
    ]);
    expect([...collectRefs({ items: { $ref: "#/components/schemas/Deep" } })]).toEqual(["Deep"]);
    expect([...collectRefs({ allOf: [{ $ref: "#/components/schemas/Deep" }] })]).toEqual(["Deep"]);
    // Arbitrarily nested, through a key nobody enumerated.
    expect([
      ...collectRefs({ a: { b: [{ c: { $ref: "#/components/schemas/Deep" } }] } }),
    ]).toEqual(["Deep"]);
    expect([...collectRefs({ type: "string" })]).toEqual([]);
  });
});

// Guards against api/openapi.yaml growing a new REQUEST field that the
// hand-written SDK never picks up -- the mirror image of this file's
// response-side checks above, which only ever guarded response schemas. A
// request field can be added to the spec, wired into the Go client and the
// CLI, and quietly never reach a hand-written SDK's options type; CI stays
// green, and a caller simply has no way to send that field until someone
// notices by hand.
//
// Same architecture as the response-side checks: the set of schemas to
// check is derived from api/openapi.yaml itself (every schema reachable
// from a request body, transitively), and every one of them must be either
// mapped to the SDK surface that sets its fields (REQUEST_CASES) or
// explicitly excused (NO_REQUEST_SDK_REPRESENTATION) with a reason.
//
// Deliberately NOT covered: query and path parameters (e.g. ListSessionsOptions'
// state/limit/cursor/externalId, or a path id). A silently-dropped OPTIONAL
// field in a request BODY is invisible at the call site -- exactly the
// failure mode this section exists for, and exactly what this file's
// response-side checks already guard for return values. An optional query
// parameter dropped from an SDK method is exactly as invisible at the call
// site -- this is a scope decision, not a claim that query/path parameters
// carry no such risk. A *required* path/query parameter is different: missing
// it breaks the method outright and is obvious immediately. It's scoped out
// here because this file's machinery (schemaProperties(), the interface/
// options-literal field extractors) is built around request BODY schemas,
// and there is no equivalent "check params against the spec" mechanism to
// extend into for query/path parameters -- that would be new coverage, not a
// gap in what this section already claims to guard. Scope this to request
// BODIES, matching the mechanism this file already has.
describe("client.ts request bodies stay in sync with api/openapi.yaml", () => {
  // (SDK locator -- either a `export interface` name interfaceFieldNames()
  // can parse, or "execute()" for the one inline-typed exception handled
  // separately below --, OpenAPI request schema name, schema properties
  // this locator deliberately or currently doesn't expose). A non-empty
  // ignored list needs a real reason in the comment above its entry --
  // either "positional argument, not part of the options bag" (can't be
  // silently dropped -- the method would be unusable without it, and it's
  // visible in the call site) or a genuine known gap, tracked separately,
  // in what the SDK currently exposes.
  const REQUEST_CASES: [string, string, string[]][] = [
    // Known gap, tracked separately (docs/sdk-known-gaps.md): neither
    // exists as a settable field on CreateSessionOptions today, even though
    // the API accepts them at create time. Remove each field from this
    // ignored list as it's added to CreateSessionOptions/create() and the
    // request payload.
    [
      "CreateSessionOptions",
      "SessionCreateRequest",
      ["idle_busy_cpu_threshold_pct", "idle_busy_network_bytes_per_sec", "artifacts", "project_id"],
    ],
    // Known gap, tracked separately (docs/sdk-known-gaps.md) -- same
    // fields, missing from the checkpoint-restore path's options too.
    [
      "CheckpointSessionOptions",
      "CheckpointSessionRequest",
      [
        "idle_mode",
        "idle_busy_cpu_threshold_pct",
        "idle_busy_network_bytes_per_sec",
        "wake_on_http",
        "project_id",
      ],
    ],
    ["CheckpointOptions", "CheckpointCreateRequest", []],
    // name/command are Session.startProcess(name, command, opts?)'s
    // positional arguments, not part of StartProcessOptions.
    ["StartProcessOptions", "ProcessStartRequest", ["name", "command"]],
    // port is Session.expose(port, options)'s positional argument.
    ["ExposeOptions", "SessionExposeRequest", ["port"]],
    ["BatchExecOptions", "BatchExecRequest", []],
    // name is Session.saveAsTemplate(name, opts?)'s positional argument.
    ["SaveAsTemplateOptions", "TemplateCreateRequest", ["name"]],
    // fromImage/name are templates.import(fromImage, name, opts?)'s
    // positional arguments.
    ["ImportTemplateOptions", "TemplateImportRequest", ["from_image", "name"]],
    // name is registryCredentials.add(name, opts)'s positional argument.
    ["AddRegistryCredentialOptions", "RegistryCredentialCreateRequest", ["name"]],
    // name is connectors.add(name, opts)'s positional argument.
    ["AddConnectorOptions", "ConnectorCreateRequest", ["name"]],
    // url is webhooks.add(url, opts?)'s positional argument.
    ["AddWebhookOptions", "WebhookCreateRequest", ["url"]],
    ["CreateRequestEndpointOptions", "RequestEndpointCreateRequest", []],
    // paths is Session.captureArtifacts(paths, opts?)'s positional argument.
    ["CaptureArtifactsOptions", "ArtifactCreateRequest", ["paths"]],
    // Deliberately asymmetric with the Python SDK, which excuses this same
    // schema in NO_REQUEST_SDK_REPRESENTATION: this SDK's
    // CaptureArtifactsOptions.destination is typed as the named
    // `ArtifactDestination` interface below (a closed literal with 4 known
    // fields), so a new schema property really would be an invisible,
    // silently-unsendable gap here -- the field check earns its keep.
    // Python's capture_artifacts(destination=...) takes an untyped dict
    // instead, so a caller there can already send any key; there is no
    // equivalent gap for a field check to catch.
    ["ArtifactDestination", "ArtifactDestination", []],
    ["ConnectionCreate", "ConnectionCreate", []],
    ["ConnectionAuth", "ConnectionAuth", []],
    ["ConnectionAllow", "ConnectionAllow", []],
    // Also used as SessionFileBatchPutRequest.files' item shape (see excuse
    // below) and by Session.writeFiles(files).
    ["FileBatchItem", "FileBatchItem", []],
  ];

  const REQUEST_MAPPED_SCHEMAS = new Set([
    ...REQUEST_CASES.map(([, schemaName]) => schemaName),
    // Checked below via inlineOptionsFieldNames(), not the generic
    // interfaceFieldNames() path -- each of these three methods' options is
    // an anonymous inline type, not a named `export interface`.
    "ExecuteRequest",
    "SessionExecRequest",
    "VolumeCreateRequest",
  ]);

  const NO_REQUEST_SDK_REPRESENTATION: Record<string, string> = {
    // Nested inside SessionCreateRequest.artifacts, which Session.create()
    // doesn't accept at all yet -- same known gap as the ignored
    // `artifacts` entry on CreateSessionOptions above.
    ArtifactDeclaration:
      "reachable only via SessionCreateRequest.artifacts, which Session.create() doesn't accept yet (known gap, tracked separately in docs/sdk-known-gaps.md -- same as CreateSessionOptions' ignored `artifacts`)",
    // Schemas whose only properties are required and already positional
    // arguments the corresponding method can't be called without -- unlike
    // an optional field silently missing from an options bag, a dropped
    // required positional argument breaks the method outright and is
    // visible at every call site, so this isn't the failure mode this check is
    // about.
    ArtifactExtendRequest: "artifacts.extend(artifactId, ttlDays) -- ttl_days is the sole, required, positional argument",
    DomainCreateRequest: "domains.add(domain) -- domain is the sole, required, positional argument",
    DomainRouteRequest:
      "domains.route(domainId, sessionId, port) -- session_id/port are both required, positional arguments",
    SessionFileMkdirRequest: "Session.mkdir(path) -- path is the sole, required, positional argument",
    SessionFileRenameRequest:
      "Session.rename(path, destPath) -- both required, positional arguments",
    SessionForkRequest: "Session.fork(count) -- count is the sole, required, positional argument",
    SessionRestoreRequest:
      "checkpoints.get(id).createSession()/Session.restore(checkpointId) -- checkpoint_id is the sole, required, positional argument",
    SessionUnexposeRequest: "Session.unexpose(port) -- port is the sole property, and the sole, positional argument",
    VolumeResizeRequest: "volumes.resize(volumeId, sizeGb) -- size_gb is the sole, required, positional argument",
    // Already checked, byte-for-byte the same component, via this file's
    // response-side CASES (EgressPolicy is used identically for both
    // getEgress()'s return value and create()/setEgress()'s input).
    EgressPolicy: "identical schema/interface already checked via this file's response-side CASES above",
    // Endpoints this SDK doesn't wrap at all -- matches this file's
    // response-side excuses for the same endpoints.
    OTLPExportSetRequest: "the OTLP export config endpoints are not wrapped by this SDK (see OTLPExportResponse's excuse above)",
    SessionTokenMintRequest:
      "POST /v1/sessions/{id}/tokens is not wrapped by this SDK yet (see SessionTokenMintResponse's excuse above)",
    // files is the sole, required property -- an array positional argument
    // (Session.writeFiles(files)). Its item shape, FileBatchItem, is
    // checked separately above.
    SessionFileBatchPutRequest:
      "Session.writeFiles(files) -- files is the sole, required, positional argument; item schema FileBatchItem is checked separately above",
    // path/content/encoding are all plain positional parameters of
    // Session.writeFile(path, content, encoding?) -- no options bag to
    // silently omit a field from; encoding's presence is visible in the
    // 3-argument signature.
    SessionFilePutRequest: "Session.writeFile(path, content, encoding?) -- all 3 properties are plain positional parameters, not an options bag",
  };

  // Textually extracts field declarations from an anonymous inline object
  // type literal in client.ts, given a regex whose first (and only) capture
  // group is the literal's body (the text between its `{` and `}`). Splits
  // the body on `;` rather than reusing interfaceFieldNames()'s
  // one-field-per-line extraction, because these inline literals aren't
  // guaranteed one field per line the way a named `export interface` is in
  // this file's style -- Session.exec()'s options
  // (`{ cwd?: string; timeoutMs?: number }`) packs two fields onto a single
  // line, which a line-anchored (`^`) field regex can only ever see the
  // first of. Every field in a TS object type literal is `;`-terminated
  // (including the last, in this codebase's style), so splitting on `;`
  // handles both the one-field-per-line case (Client.execute()'s options)
  // and the packed-single-line case (Session.exec()'s, volumes.create()'s)
  // uniformly.
  function inlineOptionsFieldNames(re: RegExp, notFoundMessage: string): Set<string> {
    const match = CLIENT_SRC.match(re);
    if (!match) throw new Error(notFoundMessage);
    const fieldRe = /^\s*([A-Za-z_][A-Za-z0-9_]*)\??:/;
    const fields = new Set<string>();
    for (const part of match[1].split(";")) {
      const m = part.match(fieldRe);
      if (m) fields.add(m[1]);
    }
    return fields;
  }

  // `Client.execute(opts: { ... }): Promise<ExecResult>` -- ExecuteRequest
  // has a real, known coverage gap here (project_id/connections, ignored
  // below) worth enforcing going forward even though this options type
  // isn't a named `export interface`.
  function executeOptionsFieldNames(): Set<string> {
    return inlineOptionsFieldNames(
      /async execute\(opts: \{([\s\S]*?)\n {2}\}\): Promise<ExecResult>/,
      "Client.execute(opts: {...}): Promise<ExecResult> signature not found in client.ts",
    );
  }

  // `Session.exec(command, opts?: { cwd?: string; timeoutMs?: number }):
  // Promise<ExecResult>` -- previously excused in NO_REQUEST_SDK_REPRESENTATION
  // as "not extractable, verified present by inspection" because this
  // options type is packed onto one line; inlineOptionsFieldNames()'s `;`
  // split handles that, so this is a real field check now, matching the
  // Python suite's REQUEST_FUNCTION_CASES entry for Session.exec (which has
  // no such extraction limitation -- it walks the AST).
  function execOptionsFieldNames(): Set<string> {
    return inlineOptionsFieldNames(
      /async exec\(\s*command: string \| string\[\],\s*opts\?: \{([\s\S]*?)\},\s*\): Promise<ExecResult>/,
      "Session.exec(command, opts?: {...}): Promise<ExecResult> signature not found in client.ts",
    );
  }

  // `private async _createVolume(opts?: { sizeGb?: number }): Promise<VolumeInfo>`
  // -- the function volumes.create() actually delegates to. Previously
  // excused in NO_REQUEST_SDK_REPRESENTATION as "not a named interface to
  // check against"; inlineOptionsFieldNames() handles the anonymous type the
  // same way it does for execute()/exec() above, so this is a real field
  // check now, matching the Python suite's REQUEST_FUNCTION_CASES entry for
  // _VolumesAPI.create.
  function volumeCreateOptionsFieldNames(): Set<string> {
    return inlineOptionsFieldNames(
      /private async _createVolume\(opts\?: \{([\s\S]*?)\}\): Promise<VolumeInfo>/,
      "_createVolume(opts?: {...}): Promise<VolumeInfo> signature not found in client.ts",
    );
  }

  for (const [ifaceName, schemaName, ignored] of REQUEST_CASES) {
    it(`${ifaceName} exposes every ${schemaName} request property`, () => {
      const schemaProps = schemaProperties(schemaName);
      const ifaceSnakeFields = new Set(
        [...interfaceFieldNames(ifaceName)].map(camelToSnake),
      );
      const missing = [...schemaProps].filter(
        (p) => !ignored.includes(p) && !ifaceSnakeFields.has(p),
      );
      expect(
        missing,
        `${schemaName} has request field(s) ${JSON.stringify(missing)} that ${ifaceName} ` +
          `does not expose (the spec changed but the hand-written SDK wasn't updated to ` +
          `match, so a caller has no way to send this field). Add the field to the ` +
          `${ifaceName} interface and wire it into the request payload in src/client.ts ` +
          `(and the mirrored interface in client.stub.ts).`,
      ).toEqual([]);
    });
  }

  it("Client.execute() opts exposes every ExecuteRequest property", () => {
    const schemaProps = schemaProperties("ExecuteRequest");
    // Known gap, tracked separately (docs/sdk-known-gaps.md). Remove as
    // project_id/connections are added.
    const ignored = ["project_id", "connections"];
    const fields = new Set([...executeOptionsFieldNames()].map(camelToSnake));
    const missing = [...schemaProps].filter((p) => !ignored.includes(p) && !fields.has(p));
    expect(
      missing,
      `ExecuteRequest has request field(s) ${JSON.stringify(missing)} that Client.execute()'s ` +
        `opts does not expose. Add the field to execute()'s options type and its payload in ` +
        `src/client.ts.`,
    ).toEqual([]);
  });

  it("Session.exec() opts exposes every SessionExecRequest property", () => {
    const schemaProps = schemaProperties("SessionExecRequest");
    // command is Session.exec(command, opts?)'s positional argument.
    const ignored = ["command"];
    const fields = new Set([...execOptionsFieldNames()].map(camelToSnake));
    const missing = [...schemaProps].filter((p) => !ignored.includes(p) && !fields.has(p));
    expect(
      missing,
      `SessionExecRequest has request field(s) ${JSON.stringify(missing)} that Session.exec()'s ` +
        `opts does not expose (a caller has no way to send this field). Add the field to ` +
        `exec()'s (and execStream()'s) options type and its payload in src/client.ts.`,
    ).toEqual([]);
  });

  it("volumes.create() opts exposes every VolumeCreateRequest property", () => {
    const schemaProps = schemaProperties("VolumeCreateRequest");
    const fields = new Set([...volumeCreateOptionsFieldNames()].map(camelToSnake));
    const missing = [...schemaProps].filter((p) => !fields.has(p));
    expect(
      missing,
      `VolumeCreateRequest has request field(s) ${JSON.stringify(missing)} that ` +
        `volumes.create()'s opts does not expose (a caller has no way to send this field). ` +
        `Add the field to create()'s options type and its payload in src/client.ts.`,
    ).toEqual([]);
  });

  it("every request schema is mapped to an SDK surface or explicitly excused", () => {
    const schemas = requestSchemaNames();
    const excused = new Set(Object.keys(NO_REQUEST_SDK_REPRESENTATION));
    const uncovered = [...schemas].filter(
      (s) => !REQUEST_MAPPED_SCHEMAS.has(s) && !excused.has(s),
    );
    expect(
      uncovered.sort(),
      `api/openapi.yaml has request schema(s) ${JSON.stringify(uncovered)} that are neither ` +
        `checked against an SDK surface (add to REQUEST_CASES) nor explicitly excused (add to ` +
        `NO_REQUEST_SDK_REPRESENTATION with a reason).`,
    ).toEqual([]);
  });

  it("every NO_REQUEST_SDK_REPRESENTATION entry has a non-empty reason", () => {
    const empty = Object.entries(NO_REQUEST_SDK_REPRESENTATION)
      .filter(([, reason]) => !reason.trim())
      .map(([name]) => name);
    expect(empty).toEqual([]);
  });
});
