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
  "elapsed_ms": 1452
}
```

`GET /providers` lists the configured chain, its order, each instance's
capabilities, and current breaker state. `GET /docs` is FastAPI's generated
OpenAPI page — the tool schema an agent can call directly.

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

**The two scope kinds behave differently, and that's the point:**

- `*` (and flat top-level fields) go through the unified→vendor translation
  table below. This is **best-effort**: any provider with no equivalent for
  that filter drops it silently at the wire level and reports the drop in
  the response's `degraded` list. A typo or an unsupported filter here never
  reaches a vendor.
- `<type>` and `<instance name>` scopes are **literal passthrough** straight
  to that vendor's API, bypassing the unified schema entirely. This is the
  escape hatch to vendor-specific power features — Exa's `type`, Tavily's
  `search_depth`, Firecrawl's `scrapeOptions` — without any of them
  polluting the request shape every other provider has to tolerate.

Each adapter declares its `passthrough_params` as a name → type map.
Validation happens **at the edge**: an unknown parameter name, a wrong
value type, or a scope naming no configured provider or type is a `422`
before any provider is called — a typo never becomes a vendor 400 halfway
down the failover chain.

**A note on collisions.** A scoped passthrough parameter can share its name
with a field the adapter itself sets internally while translating a unified
filter — and if it does, the passthrough value silently wins, with no
`degraded` signal. This is by design (passthrough is meant to override,
unconditionally), but it's easy to trip over. The concrete case in this
repo: TinyFish translates the unified `topic` field into its own
`domain_type` parameter (`"news"` or `"web"`) *before* passthrough is
applied — so `"params": {"tinyfish/domain_type": "…"}` will quietly replace
whatever `topic` produced. If you set both, `domain_type` from `params`
wins and you won't see it flagged anywhere in the response.

## Capability table

Which unified field each vendor has a real equivalent for. A `—` means the
adapter drops that filter for that vendor and reports it in `degraded`.

| Unified | tavily | brave | exa | linkup | firecrawl | tinyfish | google_cse | searxng |
|---|---|---|---|---|---|---|---|---|
| `query` | `query` | `q` | `query` | `q` | `query` | `query` | `q` | `q` |
| `max_results` | `max_results` ≤20 | `count` ≤20 | `numResults` ≤100 | `maxResults` | `limit` ≤100 | — | `num` ≤10 | — |
| `include_domains` | `include_domains` | `site:` ops | `includeDomains` | `includeDomains` | `includeDomains` | — | `siteSearch`+`i` (1 only) | — |
| `exclude_domains` | `exclude_domains` | `-site:` ops | `excludeDomains` | `excludeDomains` | `excludeDomains` | — | `siteSearch`+`e` (1 only) | — |
| `freshness` | `time_range` | `freshness` | — | — | `tbs=qdr:*` | `recency_minutes` | `dateRestrict` | `time_range` |
| `start/end_date` | `start_date`/`end_date` | `freshness` range | `startPublishedDate`/… | `fromDate`/`toDate` | — | `after_date`/`before_date` | — | — |
| `country` | `country` | `country` | `userLocation` | — | `country` | `location` | `gl` | — |
| `language` | — | `search_lang` | — | — | — | `language` | `lr` | `language` |
| `safe_search` | `safe_search` | `safesearch` | `moderation` | — | — | — | `safe` | `safesearch` |
| `topic` | `topic` | — | `category` | — | `sources` | `domain_type` | — | `categories` |

Anything a provider can't honour — whether because it's structurally absent
(this table) or value-dependent (e.g. SearXNG supports `freshness` in
general but has no `week` bucket) — is reported by name in the response's
top-level `degraded` list, hoisted from the winning attempt so a caller
never has to scan `attempts` to learn the answer was loosely filtered.

## Failover semantics

```
for instance in configured_order:
    if deadline_exceeded():              → stop, return 503
    if breaker.is_open(instance):        → record skipped, continue
    try:
        results = await instance.search(request)
    except ProviderError as e:           → record error, breaker.fault(e), continue
    if len(results) == 0:                → record empty, no breaker fault, continue
    breaker.success(instance)
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
| `QUOTA` | 402, 429, 432, 433, Google `dailyLimitExceeded`/`rateLimitExceeded` | fault | **extended** — guaranteed to fail again |
| `MALFORMED` | 200 but unparseable; Firecrawl `success: false`; unexpected 400 | fault | standard |
| `EMPTY` | 200, zero results | **no fault** | — |

**Circuit breaker**, per provider *instance* (`tavily-a` and `tavily-b` are
independent despite sharing an adapter):

- `closed` → 3 consecutive faults (configurable via `breaker.fault_threshold`)
  → `open`
- `open` → skipped entirely; after the cooldown → `half-open`
- `half-open` → admits exactly one probe; success → `closed`, fault → `open`
- Cooldown: 60s standard / 300s for `QUOTA` (both configurable), or the
  vendor's `Retry-After` header when present and longer

Breaker state lives in process memory only — a restart re-probes every
provider once, which is cheap and correct, so nothing is persisted to disk.

**Timeouts:** a per-provider timeout (`defaults.timeout_ms`, overridable per
instance) and a whole-request deadline (`server.request_deadline_ms`). The
deadline is checked before each new attempt starts, not enforced mid-flight,
so a request can overshoot it by up to one adapter's own timeout.

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
