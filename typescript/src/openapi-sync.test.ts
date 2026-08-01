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
