# Structured errors — full reference

Source of truth: `https://inis.run/docs/api/errors` (generated from the
OpenAPI spec — this file is a snapshot for offline reading; if the two
disagree, the docs site wins). Every response body is
`{"error": "<message>", "code": "<stable string>"}`. Branch on `code`.
Never match on `error` text — it can be reworded without notice. A `code`
you don't recognize should be treated as a generic failure of the class
implied by the HTTP status, not assumed to mean anything more specific.

| Code | Also seen as | HTTP | Retryable | Action |
|---|---|---|---|---|
| `validation` | `bad_request` | 400 | No | Fix the request body/params; retrying unmodified fails identically. |
| `payload_too_large` | | 400 | No | Shrink the payload; it exceeds a transport limit. |
| `unauthenticated` | `unauthorized` | 401 | No | Refresh the credential, then retry. |
| `payment_required` | | 402 | No | Prepaid balance exhausted or a spend cap hit — top up / raise the cap. |
| `forbidden` | | 403 | No | Valid credential, not allowed to do this. |
| `not_found` | | 404 | No | Missing, or missing for this caller — a destroyed session looks identical to one that never existed. |
| `method_not_allowed` | | 405 | No | Wrong HTTP method — a well-behaved client should never hit this. |
| `conflict` | | 409 | No | Re-read the resource's current state before retrying. |
| `session_not_live` | | 409 | Yes | Live-only op against a paused/ending/not-yet-ready session — refetch state, retry once live. |
| `session_ended` | | 410 | No | Ended past recall (idle timeout, max lifetime, or expired-paused-TTL) — start a new session. |
| `rate_limited` | `concurrency_limit_exceeded`, `rate_limit_exceeded` | 429 | Yes | Back off and retry; honor `Retry-After`/`.retry_after` if set. |
| `bad_gateway` | | 502 | Yes | Upstream node unreachable — retry with backoff. |
| `session_unavailable` | | 503 | Yes | Session's VM is wedged/unreachable — retry. |
| `session_node_unavailable` | | 503 | Yes | Session's home node unreachable — retry; response usually sets `Retry-After`. |
| `template_version_unavailable` | | 502/503 | Yes | Pinned template version couldn't be materialized on this node — retry (never silently falls back to a different version). |
| `archive_unavailable` | | 503 | No | No working cold tier to pause into — needs a placement/config change, not a retry; the session itself was left running. |
| `size_unavailable` | | 503 | No | Requested size doesn't fit anywhere in the fleet today — pick a smaller size. |
| `unavailable` | `service_unavailable` | 502/503 | Yes | A dependency is temporarily unreachable — retry. |
| `internal` | `internal_error` | 500 | No | Unexpected server-side failure — generic by design; include the request ID if escalating. |
| `error` | | any | No | Fallback used only when nothing more specific applies. |

## The one destructive mistake to avoid

Don't invent a code that "sounds right" and isn't in this table — an
earlier draft of adapter guidance did exactly that (cited a
concurrency-limit code that exists nowhere in this API) and it shipped
publicly before being caught. If you need a code for a scenario that isn't
here, go read `api/openapi.yaml`'s error responses or
`https://inis.run/docs/api/errors` directly — don't guess from a
plausible-sounding name.

## Retry pattern

Retry only when `.retryable` is true (or the code appears in the
Retryable=Yes rows above), using `.retry_after`/`retryAfter` as the delay
when the server provides one, otherwise an increasing backoff with a
attempt cap. See `error_recovery_and_limits` in
[`examples/agent-harness`](https://github.com/inis-run/sdk/tree/main/examples/agent-harness)
for a real retry helper exercised against a scripted error sequence in
both Python and TypeScript.
