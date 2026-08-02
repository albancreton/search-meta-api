import json

import httpx
import pytest
import respx

from search_meta_api.errors import ErrorClass, ProviderError
from search_meta_api.models import EffectiveRequest
from search_meta_api.providers.firecrawl import FirecrawlAdapter

URL = "https://api.firecrawl.dev/v2/search"

BODY = {
    "success": True,
    "data": {"web": [
        {"title": "Cats", "description": "about cats", "url": "https://a.com"},
        {"title": "More", "description": "more cats", "url": "https://b.com"},
    ]},
    "creditsUsed": 1,
}


@pytest.fixture
def adapter(client):
    return FirecrawlAdapter("firecrawl", client, timeout_ms=5000, api_key="fc-key")


def sent(route) -> dict:
    return json.loads(route.calls.last.request.content)


@respx.mock
async def test_sends_bearer_auth_and_web_source(adapter):
    route = respx.post(URL).mock(return_value=httpx.Response(200, json=BODY))
    await adapter.search(EffectiveRequest(query="cats"), {})
    assert route.calls.last.request.headers["authorization"] == "Bearer fc-key"
    payload = sent(route)
    assert payload["query"] == "cats"
    assert payload["sources"] == [{"type": "web"}]


@respx.mock
async def test_parses_results(adapter):
    respx.post(URL).mock(return_value=httpx.Response(200, json=BODY))
    results = await adapter.search(EffectiveRequest(query="cats"), {})
    assert [r.title for r in results] == ["Cats", "More"]
    assert results[0].snippet == "about cats"
    assert [r.rank for r in results] == [1, 2]


@respx.mock
async def test_news_topic_switches_the_source_and_reads_the_news_array(adapter):
    respx.post(URL).mock(return_value=httpx.Response(200, json={
        "success": True,
        "data": {"news": [{"title": "N", "snippet": "news snippet",
                           "url": "https://n.com", "date": "2026-01-05"}]},
    }))
    results = await adapter.search(EffectiveRequest(query="cats", topic="news"), {})
    assert results[0].title == "N"
    assert results[0].snippet == "news snippet"
    assert results[0].published_date == "2026-01-05"


@respx.mock
async def test_exclusions_become_query_operators_not_a_parameter(adapter):
    """includeDomains and excludeDomains are mutually exclusive at the vendor."""
    route = respx.post(URL).mock(return_value=httpx.Response(200, json=BODY))
    await adapter.search(EffectiveRequest(
        query="cats", include_domains=["a.com"], exclude_domains=["c.com"]), {})
    payload = sent(route)
    assert payload["includeDomains"] == ["a.com"]
    assert "excludeDomains" not in payload
    assert payload["query"] == "cats -site:c.com"


@respx.mock
@pytest.mark.parametrize(("freshness", "tbs"),
                         [("day", "qdr:d"), ("week", "qdr:w"),
                          ("month", "qdr:m"), ("year", "qdr:y")])
async def test_freshness_becomes_tbs(adapter, freshness, tbs):
    route = respx.post(URL).mock(return_value=httpx.Response(200, json=BODY))
    await adapter.search(EffectiveRequest(query="cats", freshness=freshness), {})
    assert sent(route)["tbs"] == tbs


@respx.mock
async def test_limit_and_country(adapter):
    route = respx.post(URL).mock(return_value=httpx.Response(200, json=BODY))
    await adapter.search(EffectiveRequest(query="cats", max_results=7, country="fr"), {})
    payload = sent(route)
    assert payload["limit"] == 7
    assert payload["country"] == "fr"


@respx.mock
async def test_success_false_on_a_200_is_a_failover_error(adapter):
    respx.post(URL).mock(return_value=httpx.Response(
        200, json={"success": False, "error": "upstream exploded"}))
    with pytest.raises(ProviderError) as exc:
        await adapter.search(EffectiveRequest(query="x"), {})
    assert exc.value.error_class is ErrorClass.MALFORMED


@respx.mock
async def test_timeout_status_is_transient(adapter):
    respx.post(URL).mock(return_value=httpx.Response(408, json={"success": False}))
    with pytest.raises(ProviderError) as exc:
        await adapter.search(EffectiveRequest(query="x"), {})
    assert exc.value.error_class is ErrorClass.TRANSIENT


@respx.mock
async def test_empty_results(adapter):
    respx.post(URL).mock(return_value=httpx.Response(
        200, json={"success": True, "data": {"web": []}}))
    assert await adapter.search(EffectiveRequest(query="x"), {}) == []


def test_capabilities():
    caps = FirecrawlAdapter.capabilities
    assert caps.max_results_cap == 100
    assert "freshness" in caps.supported_filters
    assert "language" not in caps.supported_filters
    assert "start_date" not in caps.supported_filters
    assert "scrapeOptions" in caps.passthrough_params


@respx.mock
async def test_filter_then_truncate_ordering(adapter):
    """Regression: rows without url are filtered, then truncation engages.

    A row with no url sits between two usable rows. With max_results=2,
    both surviving results are the first and third rows, with ranks [1, 2].
    """
    respx.post(URL).mock(return_value=httpx.Response(200, json={
        "success": True,
        "data": {"web": [
            {"title": "First", "description": "first row", "url": "https://a.com"},
            {"title": "Bad", "description": "no url row"},  # no url field
            {"title": "Third", "description": "third row", "url": "https://c.com"},
        ]},
    }))
    results = await adapter.search(EffectiveRequest(query="test", max_results=2), {})
    assert len(results) == 2
    assert [r.title for r in results] == ["First", "Third"]
    assert [r.rank for r in results] == [1, 2]
