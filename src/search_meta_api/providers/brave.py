from __future__ import annotations

from typing import Any

from search_meta_api.models import EffectiveRequest, SearchResult
from search_meta_api.providers.base import (
    Adapter,
    Capabilities,
    build_results,
    domain_operators,
)

ENDPOINT = "https://api.search.brave.com/res/v1/web/search"

FRESHNESS_CODES = {"day": "pd", "week": "pw", "month": "pm", "year": "py"}
OPEN_START = "1970-01-01"
OPEN_END = "2999-12-31"


class BraveAdapter(Adapter):
    type_name = "brave"
    capabilities = Capabilities(
        supported_filters=frozenset({
            "include_domains", "exclude_domains", "freshness",
            "start_date", "end_date", "country", "language", "safe_search",
        }),
        max_results_cap=20,
        passthrough_params={
            "extra_snippets": bool,
            "offset": int,
            "ui_lang": str,
            "enable_rich_callback": int,
        },
    )

    async def search(
        self, req: EffectiveRequest, passthrough: dict[str, Any]
    ) -> list[SearchResult]:
        params: dict[str, Any] = {
            "q": domain_operators(req.query, req.include_domains, req.exclude_domains),
            "count": req.max_results,
        }
        if freshness := self._freshness(req):
            params["freshness"] = freshness
        if req.country:
            params["country"] = req.country.upper()
        if req.language:
            params["search_lang"] = req.language
        if req.safe_search:
            params["safesearch"] = req.safe_search
        params.update(passthrough)

        body = await self.request_json(
            "GET", ENDPOINT, params=params,
            headers={"X-Subscription-Token": self.options["api_key"],
                     "Accept": "application/json"},
        )
        return self._parse(body, req.max_results)

    @staticmethod
    def _freshness(req: EffectiveRequest) -> str | None:
        """A date range is more specific than a relative window, so it wins."""
        if req.start_date or req.end_date:
            start = req.start_date.isoformat() if req.start_date else OPEN_START
            end = req.end_date.isoformat() if req.end_date else OPEN_END
            return f"{start}to{end}"
        if req.freshness:
            return FRESHNESS_CODES[req.freshness]
        return None

    def _parse(self, body: dict[str, Any], limit: int) -> list[SearchResult]:
        rows = (body.get("web") or {}).get("results") or []
        return build_results(rows, limit, lambda row, rank: SearchResult(
            title=row.get("title") or "",
            url=row["url"],
            snippet=row.get("description") or "",
            rank=rank,
            published_date=row.get("page_age"),
        ))
