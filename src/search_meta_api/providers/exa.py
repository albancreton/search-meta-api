from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from typing import Any

from search_meta_api.errors import ErrorClass, ProviderError
from search_meta_api.models import EffectiveRequest, SearchResult
from search_meta_api.providers.base import Adapter, Capabilities, build_results

ENDPOINT = "https://api.exa.ai/search"


def _iso_datetime(value: date) -> str:
    return f"{value.isoformat()}T00:00:00.000Z"


class ExaAdapter(Adapter):
    type_name = "exa"
    capabilities = Capabilities(
        supported_filters=frozenset({
            "include_domains", "exclude_domains", "start_date", "end_date",
            "country", "safe_search", "topic",
        }),
        max_results_cap=100,
        passthrough_params={
            "type": str,
            "category": str,
            "additionalQueries": list,
            "compliance": str,
            "contents": dict,
        },
    )

    async def search(
        self, req: EffectiveRequest, passthrough: dict[str, Any]
    ) -> list[SearchResult]:
        payload: dict[str, Any] = {
            "query": req.query,
            "numResults": req.max_results,
            # Without `contents`, Exa returns no snippet at all.
            "contents": {"highlights": {"maxCharacters": 500}},
        }
        if req.include_domains:
            payload["includeDomains"] = req.include_domains
        if req.exclude_domains:
            payload["excludeDomains"] = req.exclude_domains
        if req.start_date:
            payload["startPublishedDate"] = _iso_datetime(req.start_date)
        if req.end_date:
            payload["endPublishedDate"] = _iso_datetime(req.end_date)
        if req.country:
            payload["userLocation"] = req.country.upper()
        if req.safe_search:
            payload["moderation"] = req.safe_search != "off"
        if req.topic == "news":
            payload["category"] = "news"
        payload.update(passthrough)

        body = await self.request_json(
            "POST", ENDPOINT, json=payload,
            headers={"x-api-key": self.options["api_key"]},
        )
        return self._parse(body, req.max_results)

    def _parse(self, body: dict[str, Any], limit: int) -> list[SearchResult]:
        rows = body.get("results")
        if not isinstance(rows, list):
            raise ProviderError(self.name, ErrorClass.MALFORMED, status=200,
                                message="missing 'results' array")
        def to_result(row: Mapping[str, Any], rank: int) -> SearchResult:
            highlights = row.get("highlights") or []
            scores = row.get("highlightScores") or []
            return SearchResult(
                title=row.get("title") or "",
                url=row["url"],
                snippet=highlights[0] if highlights else "",
                rank=rank,
                published_date=row.get("publishedDate"),
                score=scores[0] if scores else None,
                favicon=row.get("favicon"),
            )

        return build_results(rows, limit, to_result)
