from __future__ import annotations

from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

Freshness = Literal["day", "week", "month", "year"]
SafeSearch = Literal["off", "moderate", "strict"]
Topic = Literal["general", "news"]


class UnifiedFilters(BaseModel):
    """The vendor-neutral request surface. Adapters translate this."""

    query: str = Field(min_length=1, max_length=2000)
    max_results: int = Field(default=10, ge=1, le=100)
    include_domains: list[str] = Field(default_factory=list)
    exclude_domains: list[str] = Field(default_factory=list)
    freshness: Freshness | None = None
    start_date: date | None = None
    end_date: date | None = None
    country: str | None = Field(default=None, min_length=2, max_length=2)
    language: str | None = Field(default=None, min_length=2, max_length=2)
    safe_search: SafeSearch | None = None
    topic: Topic = "general"

    @field_validator("country", "language")
    @classmethod
    def _lowercase(cls, v: str | None) -> str | None:
        return v.lower() if v else v


class EffectiveRequest(UnifiedFilters):
    """A UnifiedFilters after namespaced overrides are applied, bound to one provider."""


class SearchRequest(UnifiedFilters):
    params: dict[str, Any] = Field(default_factory=dict)


class SearchResult(BaseModel):
    title: str
    url: str
    snippet: str
    rank: int
    published_date: str | None = None
    score: float | None = None
    favicon: str | None = None


class Attempt(BaseModel):
    provider: str
    outcome: Literal["success", "empty", "error", "skipped"]
    status: int | None = None
    error_class: str | None = None
    reason: str | None = None
    ms: int | None = None
    degraded: list[str] = Field(default_factory=list)


class SearchResponse(BaseModel):
    query: str
    provider: str
    results: list[SearchResult]
    degraded: list[str] = Field(default_factory=list)
    attempts: list[Attempt] = Field(default_factory=list)
    elapsed_ms: int


UNIFIED_FIELDS: frozenset[str] = frozenset(UnifiedFilters.model_fields) - {"query"}
FILTER_FIELDS: frozenset[str] = UNIFIED_FIELDS - {"max_results"}
