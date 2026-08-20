# ADR 0003: Local capability credential transport

- Status: Accepted
- Date: 2026-08-20
- Scope: Phase 3 local dual-interface workbench only

## Decision

The launcher owns two independent, randomly generated capability credentials:
`operator-console` and `adk-developer`. It injects them into child-process
environments. They are never command-line arguments, URLs, static assets,
Agent instructions, tool parameters, model context, tool results, logs, or
persisted run provenance.

The ADK control-plane client sends its credential only as
`Authorization: Bearer <credential>`. The credential also authenticates an
HMAC confirmation binding over the stable idempotency key and canonical start
request. The idempotency key is generated inside the ADK tool before the ADK
confirmation interrupt and is reused when the confirmed invocation resumes.
It is carried as the opaque `Idempotency-Key` header; the confirmation binding
is carried separately and neither is a model-visible tool argument.

The browser never receives the operator capability credential or launcher
bootstrap. A launcher-owned, single-use bootstrap has a short expiry. Anonymous
Console GET requests only return the static shell and never issue a session.
The shell generates a private random claim in browser memory, submits it in a
same-origin JSON body, and displays the returned short-lived handoff ID. The
operator pastes that ID into the launcher's terminal prompt. The launcher sends
the ID and its bootstrap to the control plane in a loopback JSON body with the
exact configured Origin. The ADK child receives neither the bootstrap nor the
operator credential, and cannot obtain the browser's private claim. The browser
polls with the ID and private claim; only an exact approved claim receives an
opaque server-side session cookie
with `HttpOnly`, `SameSite=Strict`, and `Path=/`. The response returns a separate
CSRF token. Browser mutations require that token plus exact configured `Host`
and `Origin`; the bootstrap secret is not accepted in a URL, cookie, header,
Web Storage, static HTML, Referer, or log output. Replay and expiry fail without
issuing a session.

Credential comparison uses constant-time digest comparison. Rotation advances
the credential generation and immediately invalidates the old bearer and all
sessions derived from it. Rotation does not mutate already accepted business
runs.

## Authorization semantics

Every registered application route and HTTP method is present in one
declarative capability matrix. Health and the static console shell are the only
anonymous reads. Catalog, run, artifact, projection, review, and execution
surfaces authenticate a trusted principal. Missing or invalid authentication is
`401`; an authenticated principal lacking the route action is `403`; a real
object outside the principal's persisted workspace/source scope is the same
bounded `404` as an absent object. Caller-provided identity, workspace, source,
headers, cookies, and tool metadata can narrow a list but never widen scope.

FastAPI documentation, ReDoc, and OpenAPI routes are disabled. Application
startup validates the live route registry against the matrix, so a newly added
or framework-generated method cannot silently inherit authority.

## Rejected alternatives

- Putting either capability credential in browser JavaScript, a URL, or Web
  Storage exposes it to page and navigation surfaces.
- Treating `operator_id`, `workspace_id`, `source`, an allowlist, or a model
  confirmation boolean as authority makes those caller-controlled values an
  escalation path.
- Cookies for ADK or bearer credentials for browser JavaScript blur the two
  threat boundaries and complicate CSRF handling.
- CORS, non-loopback binding, SSO/RBAC, and production secret management remain
  explicit non-goals for this local development phase.
