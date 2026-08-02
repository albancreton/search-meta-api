from __future__ import annotations

from typing import Any

from search_meta_api.errors import ErrorClass, ProviderError
from search_meta_api.models import EffectiveRequest, SearchResult
from search_meta_api.providers.base import Adapter, Capabilities, build_results

ENDPOINT = "https://api.linkup.so/v1/search"


class LinkupAdapter(Adapter):
    type_name = "linkup"
    capabilities = Capabilities(
        supported_filters=frozenset({
            "include_domains", "exclude_domains", "start_date", "end_date",
        }),
        max_results_cap=None,
        passthrough_params={
            "depth": str,
            "outputType": str,
            "structuredOutputSchema": str,
            "includeImages": bool,
            "includeInlineCitations": bool,
            "includeSources": bool,
        },
    )

    async def search(
        self, req: EffectiveRequest, passthrough: dict[str, Any]
    ) -> list[SearchResult]:
        payload: dict[str, Any] = {
            "q": req.query,
            "depth": "standard",          # required by the vendor
            "outputType": "searchResults",
            "maxResults": req.max_results,
        }
        if req.include_domains:
            payload["includeDomains"] = req.include_domains
        if req.exclude_domains:
            payload["excludeDomains"] = req.exclude_domains
        if req.start_date:
            payload["fromDate"] = req.start_date.isoformat()
        if req.end_date:
            payload["toDate"] = req.end_date.isoformat()
        payload.update(passthrough)

        body = await self.request_json(
            "POST", ENDPOINT, json=payload,
            headers={"Authorization": f"Bearer {self.options['api_key']}"},
        )
        return self._parse(body, req.max_results)

    def _parse(self, body: dict[str, Any], limit: int) -> list[SearchResult]:
        rows = body.get("results")
        if not isinstance(rows, list):
            raise ProviderError(self.name, ErrorClass.MALFORMED, status=200,
                                message="missing 'results' array")
        # Linkup returns a mixed text/image list; images are not search results.
        return build_results(
            rows,
            limit,
            lambda row, rank: SearchResult(
                title=row.get("name") or "",
                url=row["url"],
                snippet=row.get("content") or "",
                rank=rank,
                favicon=row.get("favicon"),
            ),
            keep=lambda row: row.get("type") == "text" and bool(row.get("url")),
        )
