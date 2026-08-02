# search-meta-api — design

**Date:** 2026-08-01
**Status:** approved (pending spec review)
**Research input:** [`docs/research/2026-08-01-provider-api-survey.md`](../../research/2026-08-01-provider-api-survey.md)

## 1. Purpose

One web-search API in front of many third-party search vendors. A caller sends a
query; the service walks an ordered list of configured providers and returns the
first positive response. If every provider fails, it returns an error.

The consumer is **LLM agents via tool-calling**. That drives two choices: the
request schema is flat and typed (agents call named parameters, not opaque
maps), and the response is a small predictable shape that is cheap to put in a
context window.

## 2. Scope

**In scope (v1):** a `POST /search` endpoint returning ranked snippet results,
backed by eight provider adapter types — nine configured instances, since Tavily
appears twice with different keys — with ordered failover, per-instance circuit
breaking, and namespaced per-provider parameter control.

**Out of scope (v1):**

- **Content extraction.** URL → page text stays with the existing external
  extraction service. It will fold in later as a second capability
  (`POST /extract`) with its own ordered provider list, which is why the router
  is built capability-agnostic from day one.
- **Generated answers, images, structured output.** Fewer than half the
  providers support each, and every one changes the per-call cost model.
- **Bing.** Retired by Microsoft on 2025-08-11. Its successor, *Grounding with
  Bing Search*, is an Azure AI Agents tool, not a callable SERP endpoint.
- Caching, result merging/deduplication across providers, and parallel or
  hedged requests. Failover is strictly sequential.

## 3. Non-goals

- Not a result aggregator. Exactly one provider answers any given request.
- Not a scraper. Providers are search APIs, not SERP-scraping proxies.
- Not multi-tenant. Single deployment, single shared config, home-server scale.

## 4. Architecture

```
app.py            FastAPI — POST /search, GET /health, GET /providers
config.py         YAML load → validated pydantic settings
models.py         SearchRequest / SearchResult / SearchResponse / Attempt
router.py         the failover loop — capability-agnostic
breaker.py        per-instance circuit breaker state
errors.py         ProviderError hierarchy + HTTP status classification
providers/
  base.py         Adapter ABC
  registry.py     type name → adapter class
  tavily.py  brave.py  exa.py  firecrawl.py
  linkup.py  tinyfish.py  google_cse.py  searxng.py
```

**Stack:** Python 3.12, FastAPI, httpx (async), pydantic v2. Docker, deployed on
the home server.

### Boundaries

An **adapter** does exactly four things: declare its capabilities, translate a
unified request into its vendor's dialect, call the vendor, and translate the
response back into unified results. It raises a classified `ProviderError` on
failure. It has no knowledge that other providers exist.

The **router** walks an ordered list of adapter instances and applies failover
policy. It has no knowledge of what a search is — it takes a list of instances,
a callable, and a success predicate. This is the seam that lets `/extract` reuse
it unchanged.

Nothing else holds state except `breaker.py`.

## 5. Provider inventory

Order below is the default config order; it is data, not code.

| # | Instance | Type | Auth | Notes |
|---|---|---|---|---|
| 1 | `tavily-a` | tavily | `Authorization: Bearer tvly-…` | POST `https://api.tavily.com/search` |
| 2 | `tavily-b` | tavily | same, different key | second key, same adapter |
| 3 | `tinyfish` | tinyfish | `X-API-Key` | GET `https://api.search.tinyfish.ai`; free, no credits |
| 4 | `brave` | brave | `X-Subscription-Token` | GET `…/res/v1/web/search`; `count` ≤ 20 |
| 5 | `exa` | exa | `x-api-key` | POST `https://api.exa.ai/search` |
| 6 | `linkup` | linkup | `Authorization: Bearer` | POST `https://api.linkup.so/v1/search` |
| 7 | `firecrawl` | firecrawl | `Authorization: Bearer` | POST `https://api.firecrawl.dev/v2/search` |
| 8 | `google` | google_cse | `key=` + `cx=` params | `num` ≤ 10, 100 results total max |
| 9 | `searxng` | searxng | none | self-hosted; needs `format: json` enabled |

## 6. Request schema

```jsonc
{
  "query": "claude opus 5 pricing",       // required
  "max_results": 10,                      // default 10, clamped per provider
  "include_domains": ["anthropic.com"],
  "exclude_domains": [],
  "freshness": "week",                    // day | week | month | year
  "start_date": "2026-01-01",             // YYYY-MM-DD
  "end_date": null,
  "country": "fr",                        // ISO 3166-1 alpha-2
  "language": "fr",                       // ISO 639-1
  "safe_search": "moderate",              // off | moderate | strict
  "topic": "general",                     // general | news
  "params": { }                           // namespaced overrides, §7
}
```

Every flat field except `query` is optional. Flat fields are exactly equivalent
to their `*/` namespaced form; `country: "fr"` and `params: {"*/country": "fr"}`
are the same instruction.

### Translation

Each adapter maps the flat fields onto its vendor's dialect. Summary (full
per-vendor detail in the research doc):

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

Where a cell is `—`, the adapter drops that filter and reports it (§9).

¹ TinyFish's `location` passthrough parameter takes region identifiers, not
ISO country codes — guessing a mapping from `country` would mislead callers,
so it is not translated. Reachable only as `tinyfish/location` passthrough;
the shared name is coincidence, not a translation.

² Tavily gates `safe_search` to enterprise plans; sending it on a standard
key risks a 400 on every request, so it is not translated automatically.
**It is not reachable through this API at all**, by any spelling.
`safe_search` is a unified field name, and §7's precedence rules match
unified names before the passthrough branch, so `tavily/safe_search` is a
scoped unified override — translated, then dropped and reported in
`degraded` because Tavily's `supported_filters` excludes it. There is no
workaround short of calling Tavily directly. An adapter must therefore
never declare a `passthrough_params` entry whose name is a unified field:
it would be unreachable, and `GET /providers` would advertise it anyway.

`include_domains`/`exclude_domains` for google_cse and searxng are genuinely
honoured, not dropped — both fold domains into the query as `site:`/`-site:`
search operators (`domain_operators`), the same mechanism Brave uses, rather
than a dedicated API parameter: Google's `siteSearch` only accepts a single
domain, so operators do the work instead and support any number of domains.
Firecrawl treats the two directions differently: `include_domains` goes
through as a real `includeDomains` parameter, but `exclude_domains` is
folded into the query as `-site:` operators instead, because the vendor's
`includeDomains` and `excludeDomains` parameters are mutually exclusive.

## 7. Namespaced parameters

`params` is a flat map whose keys are `<scope>/<name>`.

```jsonc
"params": {
  "*/freshness":          "week",       // everyone, translated, best-effort
  "tavily/search_depth":  "advanced",   // both tavily instances, raw
  "tavily-b/max_results": 20,           // that one instance only
  "exa/type":             "fast",
  "brave/extra_snippets": true,
  "searxng/engines":      "google,ddg"
}
```

**Scopes**, in increasing precedence: `*` → `<type>` → `<instance name>`.

A flat top-level field and its `*/` twin are the **same slot**, not two
precedence levels. Supplying both `country: "fr"` and `params: {"*/country":
"de"}` in one request is a 422, rather than one silently shadowing the other.

**The two scope kinds behave differently, and that is the point.**

**What decides the handling is the parameter's NAME, not its scope.**

- A parameter whose name is a **unified field** goes through the translation
  table, in *every* scope including `<type>` and `<instance name>`. It is
  **best-effort**: validated against the unified type, clamped where the
  provider caps it, dropped for any provider with no equivalent, and that drop
  reported in the response's `degraded`. `tavily-b/max_results` is not raw — it
  is the unified `max_results` retargeted at one instance.
- A parameter whose name is in that adapter's `passthrough_params` is
  **literal passthrough** to the vendor's API. Only a `<type>` or
  `<instance name>` scope can produce one; a `*/` key never becomes
  passthrough. It is the escape hatch to every vendor's power features — Exa's
  `type`, Tavily's `search_depth`, Firecrawl's `scrapeOptions` — without any of
  them polluting the unified schema.

The unified branch is matched **first**, so the two sets must stay disjoint: a
`passthrough_params` entry whose name is a unified field is unreachable by
construction, while `GET /providers` would still advertise it. (An earlier
draft of this section claimed the "can't honour it" case cannot arise for a
scoped parameter. That was wrong for exactly this reason, and Tavily's
`safe_search` was the case that proved it — see §6 footnote ².)

**Validation is at the edge.** Each adapter declares `passthrough_params` as a
name → type map. An unknown parameter name, a wrong type, or a scope naming no
configured provider or type is a **422 before any provider is called**. A typo
never becomes a vendor 400 halfway down the chain.

## 8. Failover semantics

```
for instance in configured_order:
    if deadline_exceeded():              → record skipped (reason
                                           "deadline-exceeded"), set
                                           truncated, stop walking
    if breaker.is_open(instance):        → record skipped, continue
    try:
        results = await instance.search(request)
    except ProviderError as e:           → record error, breaker.fault(e), continue
    breaker.success(instance)            # BEFORE the empty check, deliberately:
                                         # an empty answer is a well-formed
                                         # response from a healthy provider, so
                                         # it heals the breaker like any other
    if len(results) == 0:                → record empty, continue
    return results
return 200 with results: [] if any provider answered emptily,
       else 503 if every provider errored or was skipped
```

The deadline check comes first in the loop body, before the breaker check, and
that ordering is load-bearing: `breaker.is_open()` is side-effecting — for a
HALF_OPEN instance it consumes the single probe token — so checking it and then
breaking on the deadline would abandon the token with nothing left to release
it, blackholing a recovering provider permanently. For the same reason the
router releases the token explicitly if the request is cancelled mid-attempt.

**Success** is a well-formed response with **at least one result**. An empty
result set is not a fault — the provider is healthy, it just found nothing — so
the chain continues without penalising the breaker. If every provider returns
empty, that is a legitimate `200` with `results: []`, not an error. A `503` is
returned only when no provider produced a well-formed response at all.

### Error classification

Because request validation happens at the edge, every failure reaching the
router is the provider's problem, not the caller's. There is therefore no
"fail-fast, don't try the others" class.

| Class | Triggers | Breaker | Cooldown |
|---|---|---|---|
| `TRANSIENT` | 5xx, 408, connect/read timeout | fault | standard |
| `AUTH` | 401, 403 | fault | standard, logged at ERROR — a dead key needs a human |
| `QUOTA` | 402, 429, 432, 433, Google `dailyLimitExceeded`/`rateLimitExceeded` | fault — **opens the breaker immediately**, bypassing `fault_threshold` | **extended** — guaranteed to fail again |
| `MALFORMED` | 200 but unparseable; Firecrawl `success: false`; unexpected 400 | fault | standard |

Those four are the entire `ErrorClass` enum. An empty result set is **not** an
error class: a 200 with zero results produces an attempt with
`outcome: "empty"` and `error_class: null`, and calls `breaker.record_success`
— it heals the breaker rather than merely not faulting it, because a
well-formed empty answer is proof the provider is healthy.

Tavily's 432 (plan limit) and 433 (pay-as-you-go cap) are distinct from 429 and
all three classify as `QUOTA`. SearXNG's 403 (`format: json` not enabled in
`settings.yml`) classifies as `AUTH` and is logged loudly — it is a config
error, not a transient one.

### Circuit breaker

Per provider **instance** — `tavily-a` and `tavily-b` are independent despite
sharing an adapter.

- `closed` → 3 consecutive faults → `open` — except a single `QUOTA` fault
  opens it immediately, bypassing `fault_threshold` entirely: a quota error
  is guaranteed to recur, so there is nothing to learn from two more tries
- `open` → skipped entirely; after the cooldown → `half-open`
- `half-open` → admits exactly one probe; success → `closed`, fault → `open`
- Cooldown: 60s standard, 300s for `QUOTA`, or the vendor's `Retry-After` when
  present and longer.

State is in-process memory. It is intentionally not persisted: a restart
re-probing every provider once is cheap and correct.

### Timeouts

Per-provider timeout (default 10 000 ms, overridable per instance) and a
whole-request deadline (default 30 000 ms).

The deadline is **best-effort**, not a hard cut-off. It is checked before each
new attempt starts and never interrupts an attempt already in flight, so a
request can overshoot it by up to one adapter's own timeout. What bounds a
request in practice is the per-provider timeout; the deadline bounds how many
more providers are tried.

Exceeding it mid-chain stops the walk, records a final `skipped` attempt with
`reason: "deadline-exceeded"`, and sets the response's top-level `truncated`
flag. It does **not** by itself change the status code: if some provider
already answered, that is still a `200` (with `results: []` if the answer was
empty) — the `truncated` flag, not the status, is what tells a caller the chain
was cut short. A `503` still means only that no provider produced a well-formed
response at all.

## 9. Response schema

```jsonc
{
  "query": "claude opus 5 pricing",
  "provider": "tavily-b",
  "results": [
    {
      "title": "Pricing — Anthropic",
      "url": "https://www.anthropic.com/pricing",
      "snippet": "…",
      "rank": 1,
      "published_date": null,     // nullable — most providers omit
      "score": null,              // nullable — tavily / exa / searxng only
      "favicon": null             // nullable
    }
  ],
  "degraded": ["language"],       // filters this provider could not honour
  "attempts": [
    {"provider":"tavily-a","outcome":"error","error_class":"QUOTA","status":432,"ms":210},
    {"provider":"tinyfish","outcome":"empty","status":200,"ms":480},
    {"provider":"brave","outcome":"skipped","reason":"breaker-open"},
    {"provider":"tavily-b","outcome":"success","status":200,"ms":640,
     "degraded":["language"]}
  ],
  "elapsed_ms": 1452,
  "truncated": false            // chain stopped early rather than fully walked
}
```

Only `title`, `url` and `snippet` are universal across all nine instances;
everything else is nullable by construction. `rank` is assigned by the router
from result order, since only TinyFish returns an explicit `position`.

Top-level `degraded` is the winning attempt's `degraded` list, hoisted so a
caller never has to scan `attempts` to learn the answer was loosely filtered. It
is always present, `[]` when nothing was dropped. When there is no winner —
every provider answered emptily, so `provider` is `null` — it is the sorted,
deduplicated union of every attempt's list instead: an empty answer is exactly
when a caller most needs to know a filter was dropped, since the drop may be why
the answer was empty.

`truncated` is `true` when the chain stopped early instead of being walked to
the end — today, only because the request deadline fired. It exists because
without it a deadline stop after an early empty answer is indistinguishable
from a genuine no-results answer: same `200`, same `provider: null`, same
`results: []`, with the truth buried in `attempts[].reason`. An agent reading
`results` and `provider` would conclude the query has no answers and stop, when
most of the chain was never asked. The status stays `200` — an empty answer is
a legitimate outcome — and this flag is what separates "asked everyone, no
results" from "ran out of time".

`attempts` is the entire observability story. No external tracing is needed to
answer "why was that slow", "which key is dead", or "why did filtering look
loose".

Error response (503, every provider failed). It is raised as a FastAPI
`HTTPException`, so the payload is **nested under `detail`** — a caller reading
`response["attempts"]` on a 503 gets a `KeyError`:

```jsonc
{
  "detail": {
    "error": "all_providers_failed",
    "query": "…",
    "attempts": [ … ],
    "elapsed_ms": 8210
  }
}
```

`truncated` is a field of the 200 response only. On a 503 the equivalent signal
is a final `attempts` entry with `reason: "deadline-exceeded"`.

## 10. Configuration

Single YAML file, API keys inline. Order of the `providers:` list *is* the
failover order.

```yaml
server:
  auth_token: "changeme"          # null or omitted → auth disabled
  request_deadline_ms: 30000

defaults:
  timeout_ms: 10000
  max_results: 10

breaker:
  fault_threshold: 3
  cooldown_ms: 60000
  quota_cooldown_ms: 300000

providers:
  # An instance `name` must never equal a provider `type` name — see below.
  # These match config.example.yaml, which is the authoritative template.
  - {name: tavily-a,           type: tavily,     api_key: "tvly-AAAAA"}
  - {name: tavily-b,           type: tavily,     api_key: "tvly-BBBBB", timeout_ms: 15000}
  - {name: tinyfish-free,      type: tinyfish,   api_key: "XXXXX"}
  - {name: brave-web,          type: brave,      api_key: "BSA…"}
  - {name: exa-web,            type: exa,        api_key: "…"}
  - {name: linkup-web,         type: linkup,     api_key: "…"}
  - {name: firecrawl-search,   type: firecrawl,  api_key: "fc-…"}
  - {name: google,             type: google_cse, api_key: "…", cx: "…"}
  - {name: searxng-selfhosted, type: searxng,    base_url: "https://xng.xmpl.me"}
```

`config.yaml` is gitignored; `config.example.yaml` ships with placeholders. The
loader validates at startup and **refuses to boot** on an unknown provider type,
a duplicate instance name, or a missing required field — a misconfigured
provider must not become a silent runtime failure.

Instance names must not collide with type names, so that a scope prefix is
never ambiguous. The loader enforces this and refuses to boot on a collision —
which is why the example above cannot use bare `tinyfish`, `brave`, `exa`,
`linkup`, `firecrawl` or `searxng` as instance names.

`auth_token`, if set, must be ASCII-only: it is compared byte-for-byte against
an `Authorization` header, and HTTP headers cannot carry anything else, so a
non-ASCII token could never be matched by any client. The loader rejects one
rather than booting a service where every request fails.

## 11. API surface

| Method | Path | Purpose |
|---|---|---|
| POST | `/search` | the search request (§6) |
| GET | `/health` | liveness + per-instance breaker state |
| GET | `/providers` | configured instances, order, capabilities, breaker state |
| GET | `/docs`, `/redoc`, `/openapi.json` | FastAPI OpenAPI — the agent-facing tool schema |

**Auth:** optional static bearer token from `server.auth_token`. When set,
`/search` and `/providers` require `Authorization: Bearer <token>`. When unset,
auth is disabled entirely. Keeping it optional means the service can run
unauthenticated on the internal network and be locked down when exposed.

**`/health` and the three schema endpoints are never authenticated**, whatever
`auth_token` is. `/health` is a liveness probe and must work without a
credential. `/docs`, `/redoc` and `/openapi.json` are FastAPI's built-ins,
mounted before any dependency this app declares; they publish the request and
response *shape* — which is the entire point of an agent-facing tool schema —
and contain no keys, no provider names and no breaker state. Gating them would
mean disabling FastAPI's built-in routes and re-registering all three by hand,
which buys nothing here. A deployment that needs them closed should sit behind
an authenticating reverse proxy.

The token is compared as **bytes**, not as a `str`: `secrets.compare_digest`
raises `TypeError` on a non-ASCII `str`, and uvicorn decodes headers as
latin-1, so a `str` comparison turned `Authorization: Bearer sécret` from any
unauthenticated caller into a 500.

## 12. Deployment

Docker service on the home server, on the `edge` network, exposed at
`https://search.local.alban.me` with wildcard TLS via the existing Caddy proxy,
with a landing-page card. Wiring follows the `exposing-service-on-local-alban-me`
skill. `config.yaml` is bind-mounted, not baked into the image, so keys rotate
without a rebuild.

No scheduled jobs, so the systemd-timer guidance for this host does not apply.

## 13. Testing

**Adapter contract tests.** One fixture set per vendor — a recorded success, an
empty result set, and each documented error status — replayed through `respx`.
These pin request translation and response parsing without spending credits, and
catch vendor response-shape drift.

**Router tests.** Fake adapters, no network. Cover: first-provider success; skip
to second on error; cascade through all to the last; every provider erroring →
503; every provider empty → 200 with `results: []`; empty response does not open
a breaker; breaker opens after threshold, skips while open, probes when
half-open; `QUOTA` gets the extended cooldown; deadline exceeded mid-chain.

**Validation tests.** Unknown `params` key, wrong type, and unknown scope each
return 422 with no provider called.

**Live smoke script.** Opt-in, real keys, run manually. Not part of CI.

Development is test-first per the project's TDD practice.

## 14. Deferred

- `POST /extract` — URL → content, second capability on the same router, when
  the existing extraction service gets consolidated in. That service already
  runs on the home server:
  - API: `https://scrape-api.local.alban.me`
  - Docs: `https://scrape.local.alban.me/docs.md`

  Folding it in means adding an `extract` capability registry alongside
  `search` and writing adapters for it — the router itself needs no change,
  which is the whole reason it was built capability-agnostic. Candidate
  providers for that registry, beyond the existing service: Tavily `/extract`,
  Exa `/contents`, Firecrawl `/scrape`, TinyFish Web Fetch, Jina Reader.
- Response caching keyed on the normalised request.
- Per-provider cost accounting (Exa reports `costDollars`, Firecrawl
  `creditsUsed`, Tavily `usage.credits`; all three are currently discarded).
- Runtime provider reordering without a restart.
