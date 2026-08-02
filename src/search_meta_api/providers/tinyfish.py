from __future__ import annotations

from typing import Any

from search_meta_api.errors import ErrorClass, ProviderError
from search_meta_api.models import EffectiveRequest, SearchResult
from search_meta_api.providers.base import Adapter, Capabilities, build_results

ENDPOINT = "https://api.search.tinyfish.ai"

RECENCY_MINUTES = {"day": 1440, "week": 10_080, "month": 43_200, "year": 525_600}


class TinyFishAdapter(Adapter):
    type_name = "tinyfish"
    capabilities = Capabilities(
        supported_filters=frozenset({"freshness", "start_date", "end_date",
                                     "language", "topic"}),
        max_results_cap=None,
        passthrough_params={
            "location": str,
            "purpose": str,
            "page": int,
            "pub_year_min": int,
            "pub_year_max": int,
            "domain_type": str,
        },
    )

    async def search(
        self, req: EffectiveRequest, passthrough: dict[str, Any]
    ) -> list[SearchResult]:
        params: dict[str, Any] = {
            "query": req.query,
            "domain_type": "news" if req.topic == "news" else "web",
        }
        if req.freshness:
            params["recency_minutes"] = RECENCY_MINUTES[req.freshness]
        if req.start_date:
            params["after_date"] = req.start_date.isoformat()
        if req.end_date:
            params["before_date"] = req.end_date.isoformat()
        if req.language:
            params["language"] = req.language
        params.update(passthrough)

        body = await self.request_json(
            "GET", ENDPOINT, params=params,
            headers={"X-API-Key": self.options["api_key"]},
        )
        return self._parse(body, req.max_results)

    def _parse(self, body: dict[str, Any], limit: int) -> list[SearchResult]:
        rows = body.get("results")
        if not isinstance(rows, list):
            raise ProviderError(self.name, ErrorClass.MALFORMED, status=200,
                                message="missing 'results' array")
        return build_results(rows, limit, lambda row, rank: SearchResult(
            title=row.get("title") or "",
            url=row["url"],
            snippet=row.get("snippet") or "",
            rank=rank,
        ))
