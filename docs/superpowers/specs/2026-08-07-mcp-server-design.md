# MCP server on top of search-meta-api — design

**Date:** 2026-08-07
**Status:** approved (pending spec review)
**Builds on:** [`2026-08-01-search-meta-api-design.md`](2026-08-01-search-meta-api-design.md)

## 1. Purpose

Expose the existing meta-search service over the Model Context Protocol, so an
MCP-capable client (Claude Code, Claude Desktop connectors) can call it as a
tool without anyone writing a bespoke HTTP wrapper first.

The service was already built for this consumer — the README states the
intended caller is "an LLM agent via tool-calling", and the flat typed request
schema exists for that reason. This adds the protocol that consumer actually
speaks. It does not change what the service does.

## 2. Scope

**In scope:** a streamable-HTTP MCP endpoint at `POST /mcp`, served in-process
by the same FastAPI app and the same container, behind the same bearer token,
exposing two tools — `search` and `list_providers`.

**Out of scope:**

- **A separate MCP container or subdomain.** One deploy, one process; the tool
  calls the router in-process with no extra network hop.
- **stdio transport.** Nothing needs a locally-spawned server today; the
  tool implementations are transport-agnostic if that changes.
- **OAuth 2.1.** Correct for a public multi-user server, heavy for a
  single-operator home-network service that already has a bearer token.
- **MCP resources and prompts.** Neither has a use here; tools are the whole
  surface.
- **A second response shape.** The MCP tool returns the `/search` response
  verbatim. See §6.

## 3. Non-goals

- Not a new capability. Every behaviour — failover order, circuit breaking,
  `degraded`, `truncated`, scoped `params` — is the existing behaviour reached
  through a second door.
- Not a divergent contract. If the two surfaces can disagree about anything,
  that is a bug in this design, not a feature.

## 4. Architecture

```
app.py             + POST /mcp route, + session-manager lifespan
mcp_server.py      MCPServer, the two tool definitions, the auth ASGI wrapper
service.py         run_search() / describe_providers() — shared by both surfaces
```

`mcp_server.py` builds the `MCPServer`, registers the tools, and returns a
Starlette route for `app.py` to append. `app.py` keeps ownership of routing,
config, and lifespan.

**Dependency:** `mcp>=2.0` in `pyproject.toml`. Note the 2.0 layout: the server
class is `mcp.server.mcpserver.MCPServer` (the pre-2.0 name was
`mcp.server.fastmcp.FastMCP`, which no longer exists).

### 4.1 Transport configuration

Built with `stateless_http=True, json_response=True`: one POST in, one JSON
body out, no session state and no SSE. Every search is independent, so there is
no session worth keeping, and statelessness means nothing breaks if the
container restarts mid-conversation.

### 4.2 Mounting at exactly `/mcp`

The obvious wiring — `app.mount("/mcp", mcp.streamable_http_app())` — makes the
real endpoint `/mcp/` and answers `/mcp` with a 307 redirect. **That breaks
real clients:** the MCP SDK's own HTTP client never sets `follow_redirects`, so
httpx's default of `False` applies and a client configured with the natural
`https://search.local.alban.me/mcp` receives a bare 307 and fails. Publishing a
URL whose trailing slash is load-bearing is a trap.

Instead:

1. Call `mcp.streamable_http_app(...)` once for its side effect of constructing
   the session manager, and discard the returned Starlette app.
2. Append `Route("/mcp", endpoint=StreamableHTTPASGIApp(mcp.session_manager),
   methods=["POST", "GET", "DELETE"])` to the FastAPI router.

`GET` and `DELETE` are the streamable-HTTP transport's SSE-stream and
session-termination verbs. Stateless mode serves neither, but routing them
anyway lets the MCP layer answer a client that tries with a protocol-shaped
error, instead of FastAPI's bare 405 from a path that only accepts POST.

`MCPServer.session_manager` is a public property documented for exactly this
(it raises a `RuntimeError` if read before `streamable_http_app()`, which is
why step 1 comes first). This yields an exact `/mcp` with no redirect, and
leaves FastAPI's own 404 handling intact for every other path — which mounting
a catch-all sub-app at `/` would not.

### 4.3 Lifespan

The session manager runs as an async context manager. FastAPI does not
propagate lifespan into mounted sub-apps, so `app.py`'s existing `lifespan`
enters `mcp.session_manager.run()` around its `yield`, alongside the existing
provider-instance setup. Without this, every MCP request fails with
`RuntimeError: Task group is not initialized`.

### 4.4 Transport security

The SDK auto-enables DNS-rebinding protection when the host looks local,
allowing only `localhost` / `127.0.0.1` / `[::1]` hosts and origins. Behind
Caddy the `Host` header is `search.local.alban.me`, so the default would reject
every real request. Pass
`TransportSecuritySettings(enable_dns_rebinding_protection=False)`.

That protection defends a localhost-bound dev server against requests forged by
a web page in the user's browser. This deployment is not that: it is reached
over TLS through a reverse proxy, and access is gated by a bearer token the
browser does not have. Disabling it here removes a check that would only ever
produce false rejections; §5 is the control that actually matters.

## 5. Auth

A thin ASGI wrapper in front of the MCP app applies the same rule as
`require_auth`: compare `Authorization` against `Bearer <server.auth_token>`
with `secrets.compare_digest` on **bytes**, encoding the header with
`surrogateescape`. This is not incidental — a non-ASCII header would otherwise
raise `TypeError` inside `compare_digest` and turn an unauthenticated request
into a 500. The MCP path is as reachable by an anonymous caller as `/search`
is, so it needs the same handling.

Token unset in config → the endpoint is open, exactly as `/search` is today.
No new config keys, no second secret to rotate.

A rejected request gets a plain HTTP `401` before the MCP layer sees it, rather
than a JSON-RPC error — an unauthenticated caller is not yet in a protocol
conversation.

## 6. The two tools

### `search`

Flat, typed parameters mirroring `UnifiedFilters`: `query`, `max_results`,
`include_domains`, `exclude_domains`, `freshness`, `start_date`, `end_date`,
`country`, `language`, `safe_search`, `topic` — plus the `params` map for
scoped per-provider overrides.

Flat rather than one nested object, for the reason the README already gives:
the caller is an agent filling named parameters. The tool description and
per-parameter descriptions carry the semantics an agent needs and cannot infer
— in particular that `truncated` must be checked before concluding a query has
no answers, and what the three `params` scopes mean.

### `list_providers`

Returns the `/providers` payload: instance name, adapter type, breaker state,
supported filters, `max_results_cap`, passthrough params.

This is what makes `params` usable. An agent cannot write
`tavily/search_depth` without discovering that a `tavily`-typed instance exists
and accepts that key. Two tools, both earning their context cost.

### Return shape

The full `SearchResponse` as JSON, `attempts` and `elapsed_ms` included —
byte-identical to what `POST /search` returns for the same request. The
per-attempt data is operator-facing rather than agent-facing, but keeping one
shape means there is no second contract to document, test, or let drift.

### Error mapping

| Condition | HTTP surface | MCP surface |
|---|---|---|
| Invalid `params` scope/key/type | `422`, detail string | tool error, same string |
| Every provider errored | `503`, `detail` object | tool error, same object |
| Every provider empty | `200`, `results: []` | success, `results: []` |

The third row is the one to get right. An empty result set is a legitimate
answer, not a failure, and it stays a success on both surfaces — with
`truncated` distinguishing "asked everyone, nobody had results" from "ran out
of time". Reporting it as a tool error would tell an agent the search broke.

## 7. Shared implementation

The body of the `/search` handler moves into `service.run_search(state, req)
-> SearchResponse`, and `/providers` into `service.describe_providers(state)`.
Both routes and both tools call these. The MCP tools become thin adapters.

This is the part that keeps §6's "byte-identical" claim true over time. It also
protects a non-obvious invariant the current code carries a comment about: the
configured `max_results` default must be applied *after* `validate_params`,
because assigning to the model adds the field to `model_fields_set` and would
make an applied default look caller-supplied, falsely conflicting with a
`*/max_results` override. Duplicating that logic into a second call path is
exactly how it would get broken.

## 8. Testing

TDD, following the existing layout — a new `tests/test_mcp.py` reusing the
fake-adapter pattern from `tests/test_app.py` (no `respx`; these tests are
about the protocol surface, not vendor HTTP). Tests drive the real MCP client
over an ASGI transport against an app built by `create_app`, so they exercise
the actual protocol rather than calling the tool functions directly.

Cases:

1. `tools/list` returns both tools with the expected parameter schemas.
2. A successful search returns a payload **equal to** the `/search` response
   for the same request — the anti-drift test.
3. An empty-everywhere search is a success with `results: []`.
4. An invalid `params` key is a tool error carrying the validation message.
5. All-providers-failed is a tool error carrying the `all_providers_failed`
   object with its `attempts`.
6. `list_providers` reports names, types, filters, caps, breaker state.
7. Auth: correct token accepted; wrong/missing token `401`; non-ASCII token
   header `401` rather than 500; unset token → open.

## 9. Deployment

No compose changes: same image, same container, same port, same Caddy route,
same landing-page card. The endpoint is `https://search.local.alban.me/mcp`.

README gains an MCP section documenting the endpoint and the client
registration, e.g.

```bash
claude mcp add --transport http search https://search.local.alban.me/mcp \
  --header "Authorization: Bearer <your token>"
```

## 10. Risks

- **SDK churn.** `mcp` renamed its server class between 1.x and 2.0. The
  dependency is pinned `>=2.0` and the wiring is confined to `mcp_server.py`,
  so a future rename touches one file.
- **`StreamableHTTPASGIApp` import.** Not underscore-prefixed and stable in
  2.0, but less prominent than `streamable_http_app()`. Test 1 fails loudly if
  it moves — this is a caught-at-CI risk, not a silent one.
