from __future__ import annotations

from typing import Any

import httpx

from search_meta_api.errors import ErrorClass, classify_status
from search_meta_api.models import EffectiveRequest, SearchResult
from search_meta_api.providers.base import (
    Adapter, Capabilities, build_results, domain_operators,
)

ENDPOINT = "https://www.googleapis.com/customsearch/v1"

DATE_RESTRICT = {"day": "d1", "week": "w1", "month": "m1", "year": "y1"}
QUOTA_REASONS = frozenset({"dailyLimitExceeded", "rateLimitExceeded",
                           "userRateLimitExceeded", "quotaExceeded"})


class GoogleCseAdapter(Adapter):
    """Programmable Search Engine, not Google web search.

    The configured engine (`cx`) must be set to search the entire web, and the
    vendor caps a single call at 10 results.
    """

    type_name = "google_cse"
    capabilities = Capabilities(
        supported_filters=frozenset({
            "include_domains", "exclude_domains", "freshness",
            "country", "language", "safe_search",
        }),
        max_results_cap=10,
        passthrough_params={
            "searchType": str,
            "fileType": str,
            "rights": str,
            "sort": str,
            "start": int,
            "exactTerms": str,
            "excludeTerms": str,
        },
    )

    async def search(
        self, req: EffectiveRequest, passthrough: dict[str, Any]
    ) -> list[SearchResult]:
        params: dict[str, Any] = {
            "key": self.options["api_key"],
            "cx": self.options["cx"],
            # siteSearch takes a single domain, so operators do the work instead.
            "q": domain_operators(req.query, req.include_domains, req.exclude_domains),
            "num": req.max_results,
        }
        if req.freshness:
            params["dateRestrict"] = DATE_RESTRICT[req.freshness]
        if req.country:
            params["gl"] = req.country
        if req.language:
            params["lr"] = f"lang_{req.language}"
        if req.safe_search:
            params["safe"] = "off" if req.safe_search == "off" else "active"
        params.update(passthrough)

        body = await self.request_json("GET", ENDPOINT, params=params)
        return self._parse(body, req.max_results)

    def classify(self, response: httpx.Response) -> ErrorClass:
        """A 403 is either a bad key or an exhausted quota — the body decides."""
        if response.status_code != 403:
            return classify_status(response.status_code)
        try:
            errors = (response.json().get("error") or {}).get("errors") or []
        except ValueError:
            return ErrorClass.AUTH
        if any(entry.get("reason") in QUOTA_REASONS for entry in errors):
            return ErrorClass.QUOTA
        return ErrorClass.AUTH

    def _parse(self, body: dict[str, Any], limit: int) -> list[SearchResult]:
        rows = body.get("items") or []
        # Google names the URL field `link`, so the default keep predicate
        # (which looks for `url`) would discard every row.
        return build_results(
            rows,
            limit,
            lambda row, rank: SearchResult(
                title=row.get("title") or "",
                url=row["link"],
                snippet=row.get("snippet") or "",
                rank=rank,
            ),
            keep=lambda row: bool(row.get("link")),
        )
