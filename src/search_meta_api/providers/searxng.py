from __future__ import annotations

from typing import Any

from search_meta_api.errors import ErrorClass, ProviderError
from search_meta_api.models import EffectiveRequest, SearchResult
from search_meta_api.providers.base import (
    Adapter, Capabilities, build_results, domain_operators,
)

# SearXNG offers day/month/year windows. There is no week.
TIME_RANGES = {"day": "day", "month": "month", "year": "year"}
SAFESEARCH_LEVELS = {"off": 0, "moderate": 1, "strict": 2}


class SearxngAdapter(Adapter):
    type_name = "searxng"
    capabilities = Capabilities(
        supported_filters=frozenset({
            "include_domains", "exclude_domains", "freshness",
            "language", "safe_search", "topic",
        }),
        max_results_cap=None,
        passthrough_params={"pageno": int, "engines": str, "categories": str},
    )

    def extra_degraded(self, req: EffectiveRequest) -> list[str]:
        if req.freshness and req.freshness not in TIME_RANGES:
            return ["freshness"]
        return []

    async def search(
        self, req: EffectiveRequest, passthrough: dict[str, Any]
    ) -> list[SearchResult]:
        params: dict[str, Any] = {
            "q": domain_operators(req.query, req.include_domains, req.exclude_domains),
            "format": "json",
            "categories": "news" if req.topic == "news" else "general",
        }
        if req.freshness in TIME_RANGES:
            params["time_range"] = TIME_RANGES[req.freshness]
        if req.language:
            params["language"] = req.language
        if req.safe_search:
            params["safesearch"] = SAFESEARCH_LEVELS[req.safe_search]
        params.update(passthrough)

        base = self.options["base_url"].rstrip("/")
        body = await self.request_json("GET", f"{base}/search", params=params)
        return self._parse(body, req.max_results)

    def _parse(self, body: dict[str, Any], limit: int) -> list[SearchResult]:
        rows = body.get("results")
        if not isinstance(rows, list):
            raise ProviderError(self.name, ErrorClass.MALFORMED, status=200,
                                message="missing 'results' array")
        return build_results(rows, limit, lambda row, rank: SearchResult(
            title=row.get("title") or "",
            url=row["url"],
            snippet=row.get("content") or "",
            rank=rank,
            score=row.get("score"),
            published_date=row.get("publishedDate"),
        ))
