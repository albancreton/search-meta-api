import pytest
from datetime import date
from pydantic import ValidationError

from search_meta_api.models import (
    FILTER_FIELDS, UNIFIED_FIELDS, EffectiveRequest, SearchRequest, SearchResult,
)


def test_query_is_required_and_non_empty():
    with pytest.raises(ValidationError):
        SearchRequest(query="")


def test_defaults():
    req = SearchRequest(query="hello")
    assert req.max_results == 10
    assert req.topic == "general"
    assert req.include_domains == []
    assert req.freshness is None
    assert req.params == {}


def test_accepts_full_filter_set():
    req = SearchRequest(
        query="hello", max_results=5, include_domains=["a.com"], exclude_domains=["b.com"],
        freshness="week", start_date=date(2026, 1, 1), country="fr", language="fr",
        safe_search="strict", topic="news",
    )
    assert req.freshness == "week"
    assert req.start_date == date(2026, 1, 1)


def test_rejects_unknown_enum_values():
    with pytest.raises(ValidationError):
        SearchRequest(query="hello", freshness="fortnight")
    with pytest.raises(ValidationError):
        SearchRequest(query="hello", safe_search="paranoid")


def test_country_and_language_are_normalised():
    req = SearchRequest(query="hello", country="FR", language="FR")
    assert req.country == "fr"
    assert req.language == "fr"


def test_field_sets():
    assert "max_results" in UNIFIED_FIELDS
    assert "max_results" not in FILTER_FIELDS
    assert "query" not in FILTER_FIELDS
    assert FILTER_FIELDS < UNIFIED_FIELDS


def test_effective_request_has_no_params_field():
    eff = EffectiveRequest(query="hello")
    assert not hasattr(eff, "params")


def test_search_result_nullable_fields_default_to_none():
    r = SearchResult(title="t", url="https://x.com", snippet="s", rank=1)
    assert r.published_date is None and r.score is None and r.favicon is None


def test_search_response_allows_no_winner():
    from search_meta_api.models import SearchResponse
    response = SearchResponse(query="x", results=[], elapsed_ms=12)
    assert response.provider is None
