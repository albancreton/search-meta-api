# Web-search provider API survey — 2026-08-01

Research input for the meta search API. Every fact below comes from the vendor's
own reference docs (fetched 2026-08-01), not from memory.

## 1. Transport & auth

| Provider | Method | Endpoint | Auth |
|---|---|---|---|
| Brave | GET | `https://api.search.brave.com/res/v1/web/search` | `X-Subscription-Token: <key>` |
| Exa | POST | `https://api.exa.ai/search` | `x-api-key: <key>` or `Authorization: Bearer <key>` |
| Firecrawl | POST | `https://api.firecrawl.dev/v2/search` | `Authorization: Bearer <key>` |
| Linkup | POST | `https://api.linkup.so/v1/search` | `Authorization: Bearer <key>` |
| Tavily | POST | `https://api.tavily.com/search` | `Authorization: Bearer tvly-<key>` |
| TinyFish | GET | `https://api.search.tinyfish.ai` | `X-API-Key: <key>` |
| Google CSE | GET | `https://www.googleapis.com/customsearch/v1` | `key=` + `cx=` query params |
| SearXNG | GET/POST | `<instance>/search` | none (self-hosted) |
| Bing | — | **retired 2025-08-11** | — |

Three shapes to support: `GET`+query-string, `POST`+JSON, and two different
header names for the key. Nothing exotic — a per-provider adapter handles it.

## 2. Bing is dead

Microsoft retired the Bing Search APIs on **2025-08-11**; existing instances were
decommissioned and no new signups are possible. The named successor, *Grounding
with Bing Search*, is a tool inside Azure AI Agents/Foundry — it grounds an LLM
response, it is not a standalone SERP endpoint you can call with an arbitrary
query and get a result list back. Reported pricing is 40–483% higher.

**Recommendation: drop Bing from the provider list.** If Bing-index coverage
actually matters, the substitute is a SERP proxy (Serper.dev, SerpAPI,
DataForSEO, Bright Data), which is a different category of vendor — scrapers,
not search APIs — with its own reliability profile.

## 3. Google CSE is a constrained special case

It is the *Programmable Search Engine* JSON API, not Google web search:

- `num` maxes at **10** results per call; `start` caps total reachable results at 100.
- Requires a pre-configured engine ID (`cx`), which must be explicitly set to
  "search the entire web".
- Free tier is 100 queries/day, then paid with a hard daily ceiling.
- No domain include/exclude *lists* — only `siteSearch` (a single site) plus
  `siteSearchFilter=i|e`.

It works as a last-resort fallback but cannot honour a `max_results > 10`
request or multi-domain filters.

## 4. Capability matrix

Legend: ✅ native · ⚠️ partial/awkward · ❌ absent

| Capability | Brave | Exa | Firecrawl | Linkup | Tavily | TinyFish | Google | SearXNG |
|---|---|---|---|---|---|---|---|---|
| Query string | `q` | `query` | `query` (≤500 ch) | `q` | `query` | `query` | `q` (≤2048 ch) | `q` |
| Result count | `count` 1–20 | `numResults` 1–100 | `limit` 1–100 | `maxResults` ≥1 | `max_results` 0–20 | ❌ | `num` 1–10 | ❌ |
| Pagination | `offset` 0–9 | ❌ | ❌ | ❌ | ❌ | `page` 0+ | `start` 1–100 | `pageno` |
| Include domains | ⚠️ `site:` op | ✅ ≤1200 | ✅ | ✅ ≤100 | ✅ ≤300 | ❌ | ⚠️ one site | ❌ |
| Exclude domains | ⚠️ `-site:` op | ✅ ≤1200 | ✅ (XOR include) | ✅ | ✅ ≤150 | ❌ | ⚠️ one site | ❌ |
| Relative freshness | `freshness` pd/pw/pm/py | ❌ | `tbs=qdr:d` | ❌ | `time_range` d/w/m/y | `recency_minutes` | `dateRestrict` | `time_range` day/month/year |
| Absolute date range | ✅ custom range | `startPublishedDate`/`end…` | ❌ | `fromDate`/`toDate` | `start_date`/`end_date` | `after_date`/`before_date` | ❌ | ❌ |
| Country / geo | `country` | `userLocation` | `country`+`location` | ❌ | `country` | `location` | `gl`/`cr` | ❌ |
| Language | `search_lang`,`ui_lang` | ❌ | ❌ | ❌ | ❌ | `language` | `lr`/`hl` | `language` |
| Safe search | off/moderate/strict | `moderation` bool | ❌ | ❌ | `safe_search` (enterprise) | ❌ | active/off | 0/1/2 |
| Topic / vertical | separate endpoints | `category` (6) | `sources` web/img/news | ❌ | `topic` general/news/finance | `domain_type` web/news/research_paper | `searchType=image` | `categories` |
| Depth / cost knob | ❌ | `type` instant→deep-reasoning | ❌ | `depth` fast/standard/deep | `search_depth` ultra-fast→advanced | ❌ | ❌ | ❌ |
| Full page content | ❌ | `contents.text` | `scrapeOptions.formats` | ❌ | `include_raw_content` | ❌ | ❌ | ❌ |
| Generated answer | ❌ | `outputSchema`/`systemPrompt` | ❌ | `outputType=sourcedAnswer` | `include_answer` | ❌ | ❌ | ❌ |
| Images | separate endpoint | `extras.imageLinks` | `sources:[images]` | `includeImages` | `include_images` | ❌ | `searchType=image` | via category |
| Relevance score | ❌ | `highlightScores` | ❌ | ❌ | `score` | ❌ | ❌ | `score` |

### Result-object field names (the normalisation problem)

| Concept | Brave | Exa | Firecrawl | Linkup | Tavily | TinyFish | Google |
|---|---|---|---|---|---|---|---|
| Title | `title` | `title` | `title` | `name` | `title` | `title` | `title` |
| URL | `url` | `url` | `url` | `url` | `url` | `url` | `link` |
| Snippet | `description` (+`extra_snippets`) | `highlights[]` / `text` | `description` | `content` | `content` | `snippet` | `snippet` |
| Published | `page_age` | `publishedDate` | `metadata` | ❌ | ❌ | ❌ | via `pagemap` |
| Score | ❌ | `highlightScores[]` | ❌ | ❌ | `score` (float) | ❌ | ❌ |
| Rank | implicit | implicit | implicit | implicit | implicit | `position` | implicit |
| Favicon | ❌ | `favicon` | ❌ | `favicon` | `favicon` (opt-in) | ❌ | ❌ |
| Cost echo | ❌ | `costDollars.total` | `creditsUsed` | ❌ | `usage.credits` | free | ❌ |
| Request id | ❌ | `requestId` | `id` | ❌ | `request_id` | ❌ | ❌ |

Only **title + url + snippet** are universal. Everything else is optional and
must be nullable in the unified response.

## 5. Failure signals (this drives the failover logic)

| Provider | Documented failure modes |
|---|---|
| Tavily | 400 bad params · 401 bad key · 429 rate limit · **432** plan limit · **433** pay-as-you-go cap · 500. Body: `{"detail":{"error":"…"}}` |
| Exa | 402 payment required (out of credits) |
| Firecrawl | 408 timeout · 500 `{"success":false,"code":"UNKNOWN_ERROR","error":"…"}` — note `success:false` can accompany a 200-ish shape |
| Linkup | 400 · 401 · 402 payment required · 429 rate limit **or** insufficient credits |
| Brave | 429 (plan-based QPS) |
| Google | 403 `dailyLimitExceeded` / `rateLimitExceeded` |
| SearXNG | 403 if `format: json` not enabled in `settings.yml` · 429 from bot-detection |
| TinyFish | undocumented |

Two things fall out of this table:

1. **Not every error should trigger failover.** A 400 caused by a malformed
   query will fail identically on all N providers — burn N calls, N× latency, and
   return a misleading "all providers failed". Errors need classifying:
   *retry-elsewhere* (401/402/403/429/432/433/5xx/timeout/connection) vs.
   *fail-fast* (400 request-level validation).
2. **HTTP 200 is not the same as success.** Firecrawl can return
   `success:false`; any provider can return `results: []`. The meta API needs an
   explicit definition of "positive response" — probably `HTTP ok AND
   provider-level ok flag AND len(results) >= 1`.

## 6. Implied unified request surface

Union of what ≥4 providers support natively, minus anything unmappable:

```
query          string, required
max_results    int, default 10          # clamp per provider (Brave 20, Google 10)
include_domains  string[]               # Brave/Google emulate via operators
exclude_domains  string[]
freshness      day|week|month|year      # map to each provider's dialect
start_date / end_date   YYYY-MM-DD      # unsupported → drop or reject
country        ISO-3166-1 alpha-2
language       ISO-639-1
safe_search    off|moderate|strict
topic          general|news             # 'finance'/'research' only some
```

Deliberately **excluded from v1**: generated answers, raw page content,
images, structured output. Only 2–3 of 8 providers support each, they change the
cost model per call, and they'd force the meta API to either degrade silently or
skip most of the fleet.

The open question they raise: when a request asks for something a provider
cannot do, does that provider get **skipped** (capability-gated) or **called
with the parameter dropped** (best-effort)? That is a design decision, not a
research finding.

## 7. Unified response sketch

```jsonc
{
  "query": "…",
  "provider": "tavily-b",          // which one actually answered
  "results": [
    {
      "title": "…",
      "url": "…",
      "snippet": "…",
      "rank": 1,
      "published_date": null,      // nullable — most providers omit
      "score": null,               // nullable — Tavily/Exa/SearXNG only
      "favicon": null
    }
  ],
  "attempts": [                    // observability: what was tried
    {"provider": "tavily-a", "outcome": "error", "status": 429, "ms": 812},
    {"provider": "tavily-b", "outcome": "success", "status": 200, "ms": 640}
  ],
  "elapsed_ms": 1452
}
```

## Sources

- [Tavily search endpoint](https://docs.tavily.com/documentation/api-reference/endpoint/search)
- [Exa search reference](https://exa.ai/docs/reference/search)
- [Brave web search query params](https://api-dashboard.search.brave.com/app/documentation/web-search/query)
- [Linkup POST /search](https://docs.linkup.so/pages/documentation/api-reference/endpoint/post-search)
- [Firecrawl /v2/search](https://docs.firecrawl.dev/api-reference/endpoint/search)
- [Google Custom Search cse.list](https://developers.google.com/custom-search/v1/reference/rest/v1/cse/list)
- [Google Custom Search REST usage](https://developers.google.com/custom-search/v1/using_rest)
- [TinyFish Search API](https://docs.tinyfish.ai/search-api)
- [SearXNG search API](https://docs.searxng.org/dev/search_api.html)
- [Bing Search API retirement notice](https://learn.microsoft.com/en-us/lifecycle/announcements/bing-search-api-retirement)
