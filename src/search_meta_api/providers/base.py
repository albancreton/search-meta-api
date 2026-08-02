from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable, ClassVar, Mapping, Sequence

import httpx

from search_meta_api.errors import ErrorClass, ProviderError, classify_status
from search_meta_api.models import (
    FILTER_FIELDS, UNIFIED_FIELDS, EffectiveRequest, SearchRequest, SearchResult, UnifiedFilters,
)

UNIVERSAL_SCOPE = "*"


@dataclass(frozen=True)
class Capabilities:
    """What a vendor can actually honour.

    `supported_filters` names unified fields this vendor has an equivalent for.
    Anything requested but absent here is dropped and reported as degraded.
    """

    supported_filters: frozenset[str]
    max_results_cap: int | None = None
    passthrough_params: Mapping[str, type] = field(default_factory=dict)


def _field_default(name: str) -> Any:
    return UnifiedFilters.model_fields[name].get_default(call_default_factory=True)


def _split(key: str) -> tuple[str, str]:
    scope, _, name = key.partition("/")
    return scope, name


def resolve_request(
    req: SearchRequest,
    instance_name: str,
    type_name: str,
    capabilities: Capabilities,
) -> tuple[EffectiveRequest, dict[str, Any], list[str]]:
    """Bind a request to one provider instance.

    Returns the effective unified request, the raw vendor passthrough params,
    and the sorted list of requested filters this provider cannot honour.

    The returned `degraded` list covers only static capability gaps — filters
    absent from `capabilities.supported_filters`. It says nothing about
    value-dependent gaps (e.g. a freshness window the vendor doesn't offer for
    this particular value); those are covered separately by
    `adapter.extra_degraded(effective)`. Callers must merge both lists to get
    the full picture for a given request.
    """
    values = req.model_dump(exclude={"params"})
    passthrough: dict[str, Any] = {}

    for scope in (UNIVERSAL_SCOPE, type_name, instance_name):
        for key, value in req.params.items():
            key_scope, name = _split(key)
            if key_scope != scope:
                continue
            if name in UNIFIED_FIELDS:
                values[name] = value
            elif name in capabilities.passthrough_params and scope != UNIVERSAL_SCOPE:
                passthrough[name] = value

    if capabilities.max_results_cap is not None:
        values["max_results"] = min(values["max_results"], capabilities.max_results_cap)

    effective = EffectiveRequest(**values)

    degraded = sorted(
        name
        for name in FILTER_FIELDS
        if getattr(effective, name) != _field_default(name)
        and name not in capabilities.supported_filters
    )
    return effective, passthrough, degraded


def domain_operators(query: str, include: list[str], exclude: list[str]) -> str:
    """Emulate domain filters with search operators, for vendors that lack them."""
    parts = [query]
    if include:
        parts.append("(" + " OR ".join(f"site:{d}" for d in include) + ")")
    parts.extend(f"-site:{d}" for d in exclude)
    return " ".join(parts)


def build_results(
    rows: Sequence[Mapping[str, Any]],
    limit: int,
    extract: Callable[[Mapping[str, Any], int], SearchResult],
    keep: Callable[[Mapping[str, Any]], bool] = lambda row: bool(row.get("url")),
) -> list[SearchResult]:
    """Filter, then truncate, then rank — in that order.

    The ordering is load-bearing. Truncating first lets an unusable row consume
    a max_results slot and leaves a gap in the rank sequence, both silently.
    """
    usable = [row for row in rows if keep(row)]
    return [extract(row, index + 1) for index, row in enumerate(usable[:limit])]


class Adapter(ABC):
    type_name: ClassVar[str]
    capabilities: ClassVar[Capabilities]

    def __init__(
        self,
        name: str,
        client: httpx.AsyncClient,
        timeout_ms: int = 10_000,
        **options: Any,
    ) -> None:
        self.name = name
        self.client = client
        self.timeout_s = timeout_ms / 1000
        self.options = options

    @abstractmethod
    async def search(
        self, req: EffectiveRequest, passthrough: dict[str, Any]
    ) -> list[SearchResult]:
        """Translate, call, translate back. Raise ProviderError on any failure."""

    def extra_degraded(self, req: EffectiveRequest) -> list[str]:
        """Filters this adapter cannot honour for *this particular* request.

        `capabilities.supported_filters` covers static gaps. This covers
        value-dependent ones — e.g. SearXNG has day/month/year windows but no
        week, so `freshness` is supported in general yet degraded for one value.
        """
        return []

    async def request_json(self, method: str, url: str, **kwargs: Any) -> dict[str, Any]:
        try:
            response = await self.client.request(
                method, url, timeout=self.timeout_s, **kwargs
            )
        except httpx.TimeoutException as exc:
            raise ProviderError(
                self.name, ErrorClass.TRANSIENT, message=f"timeout: {exc}"
            ) from exc
        except httpx.RequestError as exc:
            raise ProviderError(
                self.name, ErrorClass.TRANSIENT, message=f"connection: {exc}"
            ) from exc

        if response.status_code >= 400:
            raise ProviderError(
                self.name,
                self.classify(response),
                status=response.status_code,
                message=response.text[:200],
                retry_after_s=_retry_after(response),
            )

        try:
            body = response.json()
        except ValueError as exc:
            raise ProviderError(
                self.name, ErrorClass.MALFORMED, status=response.status_code,
                message="response body is not JSON",
            ) from exc

        if not isinstance(body, dict):
            raise ProviderError(
                self.name, ErrorClass.MALFORMED, status=response.status_code,
                message="response body is not a JSON object",
            )
        return body

    def classify(self, response: httpx.Response) -> ErrorClass:
        """Override where a vendor needs body inspection to classify (see google_cse)."""
        return classify_status(response.status_code)


def _retry_after(response: httpx.Response) -> float | None:
    raw = response.headers.get("Retry-After")
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None
