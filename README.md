# search-meta-api

One web-search HTTP API in front of many third-party search vendors. A caller
sends a query; the service walks an ordered list of configured provider
instances and returns the **first non-empty result set**. If every provider
comes back empty, the caller gets a `200` with `results: []` — that's a
legitimate answer, not a failure. If every provider actually *errors* (bad
key, quota exhausted, timeout, malformed response), the caller gets a `503`.
Either way, the response's `attempts` array is the full story of what
happened on the way to that answer: which providers were tried, in what
order, with what outcome, and how long each took.

The intended consumer is an **LLM agent via tool-calling** — the request
schema is a flat, typed shape (named parameters an agent can fill in
directly), and the response is small and predictable enough to drop straight
into a context window.

## Quick start

```bash
cp config.example.yaml config.yaml
# edit config.yaml: fill in real API keys for whichever providers you have
docker compose up -d
```

`config.yaml` is bind-mounted into the container (see `docker-compose.yml`),
not baked into the image, so rotating a key is just an edit + restart, no
rebuild. It's gitignored — never commit it.

**Keep `config.yaml` readable by uid 10001.** The container runs as an
unprivileged `appuser` (uid/gid 10001), and a bind mount carries the host's
permission bits straight through. `chmod 0600` — the natural instinct for a
file full of API keys — makes it unreadable inside the container and the
service fails to boot with `config file not found`, since the host owner
almost certainly isn't uid 10001. Use `0644`, or `0640` with the file's
group set to gid 10001. Protect the keys with the *directory's* permissions
instead; the file itself has to stay readable.

Once it's up:

```bash
curl -s https://search.local.alban.me/health | python -m json.tool
curl -s -X POST https://search.local.alban.me/search \
  -H 'Authorization: Bearer <your token>' \
  -H 'Content-Type: application/json' \
  -d '{"query":"claude opus 5 pricing","max_results":3}' | python -m json.tool
```

(`<your token>` is whatever you set as `server.auth_token` in `config.yaml`;
if you left it unset, drop the `Authorization` header entirely — auth is
disabled.) These deploy/verify commands assume the service is wired up on
the operator's home server per the `exposing-service-on-local-alban-me`
skill (Caddy route + `edge` network + landing-page card); wiring that up is
a deploy-time step, not something this repo does for you.

Running locally without Docker: `uvicorn search_meta_api.app:build --factory`
(reads `CONFIG_PATH`, defaulting to `config.yaml` in the working directory).

## Request examples

Minimal request — just a query, everything else defaults:

```json
{"query": "claude opus 5 pricing"}
```

A request that exercises all three `params` scopes at once (see below for
what scopes mean):

```json
{
  "query": "claude opus 5 pricing",
  "max_results": 5,
  "params": {
    "*/freshness": "week",
    "tavily/search_depth": "advanced",
    "tavily-b/max_results": 20
  }
}
```

Response shape (abbreviated):

```json
{
  "query": "claude opus 5 pricing",
  "provider": "tavily-b",
  "results": [
    {"title": "Pricing — Anthropic", "url": "https://www.anthropic.com/pricing",
     "snippet": "…", "rank": 1, "published_date": null, "score": null, "favicon": null}
  ],
  "degraded": ["language"],
  "attempts": [
    {"provider": "tavily-a", "outcome": "error", "error_class": "QUOTA", "status": 432, "ms": 210},
    {"provider": "tinyfish-free", "outcome": "empty", "status": 200, "ms": 480},
    {"provider": "tavily-b", "outcome": "success", "status": 200, "ms": 640, "degraded": ["language"]}
  ],
  "elapsed_ms": 1452,
  "truncated": false
}
```

`truncated` is `true` when the chain stopped early instead of being walked
to the end — today that means only one thing, the request deadline fired.
**Check it before concluding a query has no answers.** Without it, a
deadline stop after an early empty answer is byte-identical to "every
provider was asked and nobody had results": the same `200`, the same
`provider: null`, the same `results: []`. It stays a `200` either way,
because an empty answer is a legitimate outcome; `truncated` is the flag
that separates "asked everyone" from "ran out of time".

The `503` body — every provider errored — is **nested under `detail`**,
because it goes out through FastAPI's `HTTPException`:

```json
{
  "detail": {
    "error": "all_providers_failed",
    "query": "claude opus 5 pricing",
    "attempts": [
      {"provider": "tavily-a", "outcome": "error", "error_class": "QUOTA", "status": 432, "ms": 210},
      {"provider": "brave-web", "outcome": "error", "error_class": "AUTH", "status": 401, "ms": 90}
    ],
    "elapsed_ms": 8210
  }
}
```

Reading `response["attempts"]` on a `503` is a `KeyError` — it's
`response["detail"]["attempts"]`. Note that `truncated` is a `200`-response
field only; on a `503` the equivalent signal is a final `attempts` entry
with `reason: "deadline-exceeded"`.

`GET /providers` lists the configured chain, its order, each instance's
capabilities, and current breaker state. `GET /docs` is FastAPI's generated
OpenAPI page — the tool schema an agent can call directly.

**Auth covers `/search`, `/providers` and `/mcp` only.** When
`server.auth_token` is set, those three require `Authorization: Bearer
<token>` (see "MCP server" below for what the token guards there). `/health`,
`/docs`, `/openapi.json` and `/redoc` are always unauthenticated. That is
deliberate for `/health` (a liveness probe shouldn't need a credential) and
accepted for the schema endpoints: they publish the request/response shape,
which is the point of an agent-facing tool schema, and they contain no keys,
no provider names and no breaker state. If your deployment can't accept
that, put the service behind an authenticating reverse proxy — this app
does not gate them.

## The `params` scoping rules

Every unified filter (`include_domains`, `freshness`, `country`, …) can be
set two ways: as a flat top-level field, or namespaced inside `params` as
`<scope>/<name>`. A flat field and its `*/` twin are **the same slot**, not
two precedence levels — sending both `"country": "fr"` and
`"params": {"*/country": "de"}` in one request is a `422`, not a silent
override.

**Scopes, in increasing precedence:**

| Scope | Matches | Example |
|---|---|---|
| `*` | every configured instance | `"*/freshness": "week"` |
| `<type>` | every instance of that provider type | `"tavily/search_depth": "advanced"` |
| `<instance name>` | exactly one configured instance | `"tavily-b/max_results": 20` |

A more specific scope wins over a less specific one for the same field.

**Two kinds of parameter behave differently — and it's the NAME that
decides which, not the scope:**

- A **unified field name** (`freshness`, `max_results`, `country`, …) goes
  through the unified→vendor translation table below, in *every* scope. This
  is **best-effort**: it is validated against the unified type, clamped where
  the provider caps it, dropped at the wire level by any provider with no
  equivalent, and that drop is reported in the response's `degraded` list.
- A name in that adapter's declared `passthrough_params` is **literal
  passthrough** straight to the vendor's API. Only a `<type>` or
  `<instance name>` scope can produce one — a `*/` key never becomes
  passthrough. This is the escape hatch to vendor-specific power features —
  Exa's `type`, Tavily's `search_depth`, Firecrawl's `scrapeOptions` —
  without any of them polluting the request shape every other provider has
  to tolerate.

So a scoped key is **not** automatically raw. `"tavily-b/max_results": 20`
does not bypass the unified schema: it is the unified `max_results`,
translated, clamped and degrade-reported like any other, just retargeted at
one instance. The scope narrows *who* it applies to; it never changes *how*
it is handled.

That has a consequence worth stating outright: an adapter can never expose a
`passthrough_params` entry whose name collides with a unified field, because
the unified branch is matched first and the passthrough branch is
unreachable. Tavily's `safe_search` was such an entry (see footnote ² below)
and has been removed.

Each adapter declares its `passthrough_params` as a name → type map.
Validation happens **at the edge**: an unknown parameter name, a wrong
value type, or a scope naming no configured provider or type is a `422`
before any provider is called — a typo never becomes a vendor 400 halfway
down the failover chain.

**A note on collisions.** A scoped passthrough parameter can share its name
with the *vendor-side* field an adapter writes while translating a unified
filter — and if it does, the passthrough value silently wins, with no
`degraded` signal. This is by design (passthrough is meant to override,
unconditionally), but it's easy to trip over. Every current case is the
unified `topic` field, which three adapters translate into a parameter that
is also declared as passthrough:

| Set both of these… | …and the passthrough value wins |
|---|---|
| `topic` and `tinyfish/domain_type` | `domain_type` (`topic` → `"news"`/`"web"`) |
| `topic` and `exa/category` | `category` (`topic: "news"` → `"news"`) |
| `topic` and `searxng/categories` | `categories` (`topic` → `"news"`/`"general"`) |

In all three the passthrough value replaces whatever `topic` produced, and
you won't see it flagged anywhere in the response. (Firecrawl's `categories`
passthrough is *not* one of these: Firecrawl translates `topic` into
`sources`, so the two never touch.)

## Capability table

Which unified field each vendor has a real equivalent for. A `—` means the
adapter drops that filter for that vendor and reports it in `degraded`.

| Unified | tavily | brave | exa | linkup | firecrawl | tinyfish | google_cse | searxng |
|---|---|---|---|---|---|---|---|---|
| `query` | `query` | `q` | `query` | `q` | `query` | `query` | `q` | `q` |
| `max_results` | `max_results` ≤20 | `count` ≤20 | `numResults` ≤100 | `maxResults` | `limit` ≤100 | — | `num` ≤10 | — |
| `include_domains` | `include_domains` | `site:` ops | `includeDomains` | `includeDomains` | `includeDomains` | — | `site:` ops | `site:` ops |
| `exclude_domains` | `exclude_domains` | `-site:` ops | `excludeDomains` | `excludeDomains` | `-site:` ops | — | `-site:` ops | `-site:` ops |
| `freshness` | `time_range` | `freshness` | — | — | `tbs=qdr:*` | `recency_minutes` | `dateRestrict` | `time_range` |
| `start/end_date` | `start_date`/`end_date` | `freshness` range | `startPublishedDate`/… | `fromDate`/`toDate` | — | `after_date`/`before_date` | — | — |
| `country` | `country` | `country` | `userLocation` | — | `country` | —¹ | `gl` | — |
| `language` | — | `search_lang` | — | — | — | `language` | `lr` | `language` |
| `safe_search` | —² | `safesearch` | `moderation` | — | — | — | `safe` | `safesearch` |
| `topic` | `topic` | — | `category` | — | `sources` | `domain_type` | — | `categories` |

¹ TinyFish's `location` passthrough parameter takes region identifiers, not
ISO country codes — guessing a mapping from `country` would mislead callers,
so it isn't translated. Reachable only as `tinyfish/location` passthrough;
the shared name is coincidence, not a translation.

² Tavily gates `safe_search` to enterprise plans; sending it on a standard
key risks a 400 on every request, so it isn't translated automatically.
**It is not reachable through this API at all**, by any spelling.
`safe_search` is a unified field name, and a `params` key naming a unified
field is always treated as a scoped unified override, never as passthrough
— so `tavily/safe_search` is translated (and, since Tavily doesn't support
the filter, dropped and reported in `degraded`) rather than forwarded. If
you have an enterprise key and need it, call Tavily directly.

`include_domains`/`exclude_domains` for google_cse and searxng are
genuinely honoured, not dropped — both fold domains into the query as
`site:`/`-site:` search operators (the same mechanism Brave uses) rather
than a dedicated API parameter, since Google's `siteSearch` only accepts a
single domain. Firecrawl treats the two directions differently:
`include_domains` goes through as a real `includeDomains` parameter, but
`exclude_domains` is folded into the query as `-site:` operators instead,
because the vendor's `includeDomains` and `excludeDomains` parameters are
mutually exclusive.

Anything a provider can't honour — whether because it's structurally absent
(this table) or value-dependent — is reported by name in the response's
top-level `degraded` list, hoisted from the winning attempt so a caller
never has to scan `attempts` to learn the answer was loosely filtered.
When there is no winner — every provider answered emptily, so `provider`
is `null` — the top-level list is instead the sorted union of every
attempt's, since an empty answer is exactly when knowing a filter was
dropped matters most.

The value-dependent cases, all reported the same way:

- **searxng `freshness`** — it has day/month/year windows but no `week`.
- **brave `freshness`** — Brave has one `freshness` parameter for both a
  relative window and a date range, and the range is more specific, so it
  wins; sending `freshness` alongside `start_date`/`end_date` drops the
  window.
- **tavily `country`** — Tavily takes country *names*, so an ISO code that
  doesn't resolve to one has nothing to send.

## Failover semantics

```
for instance in configured_order:
    if deadline_exceeded():              → record skipped (reason
                                           "deadline-exceeded"), set
                                           truncated, stop walking
    if breaker.is_open(instance):        → record skipped, continue
    try:
        results = await instance.search(request)
    except ProviderError as e:           → record error, breaker.fault(e), continue
    breaker.success(instance)            # BEFORE the empty check: an empty
                                         # answer is a healthy response and
                                         # actively heals the breaker
    if len(results) == 0:                → record empty, continue
    return results
return 200 with results: [] if any provider answered emptily,
       else 503 if every provider errored or was skipped
```

**Success** means a well-formed response with **at least one result**. An
empty result set is not a fault — the provider is healthy, it just found
nothing — so the chain continues without penalising that instance's
breaker. A `503` is returned only when no provider produced a well-formed
response at all.

**Error classes:**

| Class | Triggers | Breaker | Cooldown |
|---|---|---|---|
| `TRANSIENT` | 5xx, 408, connect/read timeout | fault | standard |
| `AUTH` | 401, 403 | fault | standard, logged at ERROR — a dead key needs a human |
| `QUOTA` | 402, 429, 432, 433, Google `dailyLimitExceeded`/`rateLimitExceeded` | fault — **opens the breaker immediately**, bypassing `fault_threshold` | **extended** — guaranteed to fail again |
| `MALFORMED` | 200 but unparseable; Firecrawl `success: false`; unexpected 400 | fault | standard |

Those four are the whole of `ErrorClass`. **An empty result set is not an
error class.** A provider that returns 200 with zero results produces an
attempt with `outcome: "empty"`, `error_class: null`, and no breaker fault
— in fact it *heals* the breaker, exactly like a non-empty answer, because
the provider is demonstrably healthy. `"EMPTY"` never appears in
`error_class`; don't match on it.

**Circuit breaker**, per provider *instance* (`tavily-a` and `tavily-b` are
independent despite sharing an adapter):

- `closed` → 3 consecutive faults (configurable via `breaker.fault_threshold`)
  → `open` — except a single `QUOTA` fault opens it immediately, bypassing
  `fault_threshold` entirely: a quota error is guaranteed to recur, so
  there's nothing to learn from two more attempts
- `open` → skipped entirely; after the cooldown → `half-open`
- `half-open` → admits exactly one probe; success → `closed`, fault → `open`
- Cooldown: 60s standard / 300s for `QUOTA` (both configurable), or the
  vendor's `Retry-After` header when present and longer

Breaker state lives in process memory only — a restart re-probes every
provider once, which is cheap and correct, so nothing is persisted to disk.

**Timeouts:** a per-provider timeout (`defaults.timeout_ms`, overridable per
instance) and a whole-request deadline (`server.request_deadline_ms`). The
deadline is checked before each new attempt starts, not enforced mid-flight,
so a request can overshoot it by up to one adapter's own timeout. When it
fires, the remaining providers are left unasked, the last `attempts` entry
records `reason: "deadline-exceeded"`, and the response's top-level
`truncated` is `true` — the status is whatever the answer so far deserves
(`200` if any provider answered, `503` if none did), never a `503` purely
on account of the deadline.

## MCP server

The same service is reachable over the Model Context Protocol at
`POST /mcp` — same container, same bearer token, same provider chain. The
tool calls the router in-process, through the same `run_search` and
`describe_providers` functions the HTTP routes call; there is no second
service to deploy and no separate code path that could drift out of sync
with the one described above.

Register it with any client that speaks remote MCP over streamable HTTP:

```bash
claude mcp add --transport http search https://search.local.alban.me/mcp \
  --header "Authorization: Bearer <your token>"
```

Drop the `--header` if `server.auth_token` is unset — `/mcp` is then open,
exactly as `/search` and `/providers` are. It is worth one extra thought
here, though. The MCP transport's DNS-rebinding protection is switched off,
because its default allowlist is localhost-only and would reject every
request arriving through the reverse proxy by hostname; the bearer token is
what stands in for it, since no browser tricked into posting at the endpoint
carries one. With no token configured, nothing stands in for it — `/mcp` is
then protected by the network boundary in front of it and nothing else, so
leave the token unset only on a network you would be happy to expose the
provider chain to.

**The URL has no trailing slash.** `/mcp` is the endpoint; `/mcp/` is not.
That's deliberate, not an oversight: the endpoint is built as a route
rather than mounted as a sub-app, because mounting would put it at `/mcp/`
and answer `/mcp` itself with a redirect — and the MCP SDK's own client
never follows redirects, so a client configured with the natural URL would
fail outright instead of being bounced to the right place.

Two tools:

- **`search`** — the same flat, typed parameters as `POST /search`,
  including the scoped `params` map, returning the same response body.
  `degraded` and `truncated` mean exactly what they mean over HTTP, and
  `truncated` is worth the same attention here as there: an empty
  `results` with `truncated: true` is not the same answer as an empty
  `results` with `truncated: false` (see "Request examples" above for why
  that distinction exists).
- **`list_providers`** — the same payload as `GET /providers`, built by the
  same function. An agent needs it before it can write a scoped param like
  `tavily/search_depth`, and to understand why a search reported a filter
  as `degraded`.

Two differences from the HTTP surface, both in how failures arrive rather
than what counts as one:

- What would be a `422` (bad `params`) or a `503` (every provider errored)
  on the HTTP surface instead becomes an MCP tool error on this one — MCP
  has no status code to carry the distinction, only a single error
  channel, so both collapse onto it. The `503` case serialises the same
  `all_providers_failed` payload the HTTP body nests under `detail` into
  the error text rather than dropping it, so the `attempts` array survives
  for the agent to read.
- Everything else is unchanged. In particular, **an empty result set stays
  a success on both surfaces** — it comes back as a normal, non-error tool
  result, because it's a legitimate answer, not a failure. Only a
  validation failure and an all-providers-errored chain become MCP tool
  errors.

## Adding a provider

1. Write an adapter in `src/search_meta_api/providers/<name>.py` subclassing
   `Adapter` (`providers/base.py`): declare `type_name` and a `Capabilities`
   (`supported_filters`, optional `max_results_cap`, `passthrough_params`),
   implement `async def search(self, req, passthrough) -> list[SearchResult]`
   to translate the request, call the vendor, and translate the response
   back — raising a classified `ProviderError` on any failure. See any
   existing adapter (`tavily.py` is a compact example) for the shape.
2. Register it in `src/search_meta_api/providers/registry.py`: add it to the
   `ADAPTERS` dict and add its required config fields to `REQUIRED_OPTIONS`.
3. Add a line to `config.example.yaml` under `providers:` (and read the
   comment right above that block — the instance `name` must never equal a
   provider `type` name, or the config loader refuses to boot).
4. Write fixture tests in `tests/providers/test_<name>.py`: a recorded
   success response, an empty result set, and each documented error status,
   replayed through `respx` with no real network access — see
   `tests/providers/test_tavily.py` for the pattern.

## Why Bing is excluded

Bing's web search API was retired by Microsoft on 2025-08-11. Its
successor, *Grounding with Bing Search*, is an Azure AI Agents tool — it's
invoked as part of an agent orchestration pipeline, not a callable SERP
endpoint you can adapt the same way as the other eight vendors here. There
is currently no drop-in replacement to add back.

## Live smoke script

`scripts/smoke.py` calls every configured provider once, for real — it
spends credits and touches the network, so it is **not** part of the test
suite and must never run in CI. Run it manually to sanity-check that keys
are still valid and a vendor hasn't silently changed its response shape:

```bash
.venv/bin/python scripts/smoke.py "your query here"
```

It prints one line per provider (`ok`/`FAIL`) and a healthy-count summary,
then exits non-zero if anything failed.
