from __future__ import annotations

from typing import Any

import pycountry

from search_meta_api.errors import ErrorClass, ProviderError
from search_meta_api.models import EffectiveRequest, SearchResult
from search_meta_api.providers.base import Adapter, Capabilities, build_results

ENDPOINT = "https://api.tavily.com/search"


def country_name(alpha_2: str) -> str | None:
    """Tavily takes country names, not ISO codes."""
    entry = pycountry.countries.get(alpha_2=alpha_2.upper())
    return entry.name.lower() if entry else None


class TavilyAdapter(Adapter):
    type_name = "tavily"
    capabilities = Capabilities(
        supported_filters=frozenset({
            "include_domains", "exclude_domains", "freshness",
            "start_date", "end_date", "country", "topic",
        }),
        max_results_cap=20,
        passthrough_params={
            "search_depth": str,
            "chunks_per_source": int,
            "include_answer": object,
            "include_raw_content": object,
            "include_images": bool,
            "include_image_descriptions": bool,
            "include_favicon": bool,
            "auto_parameters": bool,
            "exact_match": bool,
            "safe_search": bool,
            "include_usage": bool,
        },
    )

    async def search(
        self, req: EffectiveRequest, passthrough: dict[str, Any]
    ) -> list[SearchResult]:
        payload: dict[str, Any] = {
            "query": req.query,
            "max_results": req.max_results,
            "topic": req.topic,
        }
        if req.include_domains:
            payload["include_domains"] = req.include_domains
        if req.exclude_domains:
            payload["exclude_domains"] = req.exclude_domains
        if req.freshness:
            payload["time_range"] = req.freshness
        if req.start_date:
            payload["start_date"] = req.start_date.isoformat()
        if req.end_date:
            payload["end_date"] = req.end_date.isoformat()
        if req.country and (name := country_name(req.country)):
            payload["country"] = name
        payload.update(passthrough)

        body = await self.request_json(
            "POST",
            ENDPOINT,
            json=payload,
            headers={"Authorization": f"Bearer {self.options['api_key']}"},
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
            snippet=row.get("content") or "",
            rank=rank,
            score=row.get("score"),
            favicon=row.get("favicon"),
        ))
