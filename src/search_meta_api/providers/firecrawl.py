from __future__ import annotations

from typing import Any

from search_meta_api.errors import ErrorClass, ProviderError
from search_meta_api.models import EffectiveRequest, SearchResult
from search_meta_api.providers.base import (
    Adapter, Capabilities, build_results, domain_operators,
)

ENDPOINT = "https://api.firecrawl.dev/v2/search"

TBS = {"day": "qdr:d", "week": "qdr:w", "month": "qdr:m", "year": "qdr:y"}


class FirecrawlAdapter(Adapter):
    type_name = "firecrawl"
    capabilities = Capabilities(
        supported_filters=frozenset({
            "include_domains", "exclude_domains", "freshness", "country", "topic",
        }),
        max_results_cap=100,
        passthrough_params={
            "scrapeOptions": dict,
            "categories": list,
            "location": str,
            "highlights": bool,
            "ignoreInvalidURLs": bool,
            "timeout": int,
        },
    )

    async def search(
        self, req: EffectiveRequest, passthrough: dict[str, Any]
    ) -> list[SearchResult]:
        source = "news" if req.topic == "news" else "web"
        payload: dict[str, Any] = {
            # excludeDomains cannot coexist with includeDomains, so exclusions
            # ride along as query operators instead of being dropped.
            "query": domain_operators(req.query, [], req.exclude_domains),
            "limit": req.max_results,
            "sources": [{"type": source}],
        }
        if req.include_domains:
            payload["includeDomains"] = req.include_domains
        if req.freshness:
            payload["tbs"] = TBS[req.freshness]
        if req.country:
            payload["country"] = req.country
        payload.update(passthrough)

        body = await self.request_json(
            "POST", ENDPOINT, json=payload,
            headers={"Authorization": f"Bearer {self.options['api_key']}"},
        )
        if body.get("success") is False:
            raise ProviderError(self.name, ErrorClass.MALFORMED, status=200,
                                message=str(body.get("error"))[:200])
        return self._parse(body, source, req.max_results)

    def _parse(self, body: dict[str, Any], source: str, limit: int) -> list[SearchResult]:
        rows = (body.get("data") or {}).get(source) or []
        return build_results(rows, limit, lambda row, rank: SearchResult(
            title=row.get("title") or "",
            url=row["url"],
            snippet=row.get("description") or row.get("snippet") or "",
            rank=rank,
            published_date=row.get("date"),
        ))
