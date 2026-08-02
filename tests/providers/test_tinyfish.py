from datetime import date
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import respx

from search_meta_api.errors import ErrorClass, ProviderError
from search_meta_api.models import EffectiveRequest
from search_meta_api.providers.tinyfish import TinyFishAdapter

URL = "https://api.search.tinyfish.ai"

BODY = {
    "query": "cats",
    "results": [
        {"position": 1, "site_name": "a.com", "title": "Cats",
         "snippet": "about cats", "url": "https://a.com"},
        {"position": 2, "site_name": "b.com", "title": "More",
         "snippet": "more cats", "url": "https://b.com"},
    ],
    "total_results": 10,
    "page": 0,
}


@pytest.fixture
def adapter(client):
    return TinyFishAdapter("tinyfish", client, timeout_ms=5000, api_key="XXXXX")


def query_of(route) -> dict:
    return parse_qs(urlparse(str(route.calls.last.request.url)).query)


@respx.mock
async def test_sends_api_key_header_and_query(adapter):
    route = respx.get(URL).mock(return_value=httpx.Response(200, json=BODY))
    await adapter.search(EffectiveRequest(query="cats"), {})
    assert route.calls.last.request.headers["x-api-key"] == "XXXXX"
    assert query_of(route)["query"] == ["cats"]


@respx.mock
async def test_parses_results(adapter):
    respx.get(URL).mock(return_value=httpx.Response(200, json=BODY))
    results = await adapter.search(EffectiveRequest(query="cats"), {})
    assert [r.title for r in results] == ["Cats", "More"]
    assert [r.rank for r in results] == [1, 2]
    assert results[0].snippet == "about cats"
    assert results[0].score is None


@respx.mock
async def test_max_results_is_applied_client_side(adapter):
    """TinyFish has no count parameter, so the adapter truncates."""
    route = respx.get(URL).mock(return_value=httpx.Response(200, json=BODY))
    results = await adapter.search(EffectiveRequest(query="cats", max_results=1), {})
    assert len(results) == 1
    assert "max_results" not in query_of(route)


@respx.mock
@pytest.mark.parametrize(
    ("freshness", "minutes"),
    [("day", "1440"), ("week", "10080"), ("month", "43200"), ("year", "525600")],
)
async def test_freshness_becomes_recency_minutes(adapter, freshness, minutes):
    route = respx.get(URL).mock(return_value=httpx.Response(200, json=BODY))
    await adapter.search(EffectiveRequest(query="cats", freshness=freshness), {})
    assert query_of(route)["recency_minutes"] == [minutes]


@respx.mock
async def test_translates_dates_language_and_topic(adapter):
    route = respx.get(URL).mock(return_value=httpx.Response(200, json=BODY))
    await adapter.search(EffectiveRequest(
        query="cats", start_date=date(2026, 1, 1), end_date=date(2026, 2, 1),
        language="fr", topic="news"), {})
    params = query_of(route)
    assert params["after_date"] == ["2026-01-01"]
    assert params["before_date"] == ["2026-02-01"]
    assert params["language"] == ["fr"]
    assert params["domain_type"] == ["news"]


@respx.mock
async def test_passthrough_params_reach_the_query_string(adapter):
    route = respx.get(URL).mock(return_value=httpx.Response(200, json=BODY))
    await adapter.search(EffectiveRequest(query="cats"), {"location": "Paris", "page": 2})
    params = query_of(route)
    assert params["location"] == ["Paris"] and params["page"] == ["2"]


@respx.mock
async def test_empty_results(adapter):
    respx.get(URL).mock(return_value=httpx.Response(200, json={"query": "x", "results": []}))
    assert await adapter.search(EffectiveRequest(query="x"), {}) == []


@respx.mock
async def test_server_error_is_transient(adapter):
    respx.get(URL).mock(return_value=httpx.Response(503, json={}))
    with pytest.raises(ProviderError) as exc:
        await adapter.search(EffectiveRequest(query="x"), {})
    assert exc.value.error_class is ErrorClass.TRANSIENT


def test_capabilities():
    caps = TinyFishAdapter.capabilities
    assert caps.max_results_cap is None
    assert "freshness" in caps.supported_filters
    assert "country" not in caps.supported_filters
    assert "include_domains" not in caps.supported_filters
    assert "location" in caps.passthrough_params


@respx.mock
async def test_unusable_row_does_not_create_a_rank_gap_or_consume_a_truncation_slot(adapter):
    body = {
        "query": "cats",
        "results": [
            {"title": "Cats", "url": "https://a.com", "snippet": "about cats"},
            {"title": "No URL", "snippet": "dropped"},
            {"title": "Dogs", "url": "https://c.com", "snippet": "about dogs"},
        ],
    }
    respx.get(URL).mock(return_value=httpx.Response(200, json=body))
    results = await adapter.search(EffectiveRequest(query="cats", max_results=2), {})
    assert [r.title for r in results] == ["Cats", "Dogs"]
    assert [r.rank for r in results] == [1, 2]
