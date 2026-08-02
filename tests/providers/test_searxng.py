from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import respx

from search_meta_api.errors import ErrorClass, ProviderError
from search_meta_api.models import EffectiveRequest
from search_meta_api.providers.searxng import SearxngAdapter

URL = "https://xng.example.test/search"

BODY = {"query": "cats", "results": [
    {"title": "Cats", "url": "https://a.com", "content": "about cats",
     "score": 1.5, "publishedDate": "2026-01-05T00:00:00"},
    {"title": "More", "url": "https://b.com", "content": "more cats", "score": 0.9},
]}


@pytest.fixture
def adapter(client):
    return SearxngAdapter("searxng", client, timeout_ms=5000,
                          base_url="https://xng.example.test")


def query_of(route) -> dict:
    return parse_qs(urlparse(str(route.calls.last.request.url)).query)


@respx.mock
async def test_requests_json_format_from_the_configured_instance(adapter):
    route = respx.get(URL).mock(return_value=httpx.Response(200, json=BODY))
    await adapter.search(EffectiveRequest(query="cats"), {})
    params = query_of(route)
    assert params["q"] == ["cats"]
    assert params["format"] == ["json"]


@respx.mock
async def test_trailing_slash_in_base_url_does_not_double_up(client):
    adapter = SearxngAdapter("searxng", client, base_url="https://xng.example.test/")
    route = respx.get(URL).mock(return_value=httpx.Response(200, json=BODY))
    await adapter.search(EffectiveRequest(query="cats"), {})
    assert str(route.calls.last.request.url).startswith(URL)


@respx.mock
async def test_parses_results_with_score_and_published_date(adapter):
    respx.get(URL).mock(return_value=httpx.Response(200, json=BODY))
    results = await adapter.search(EffectiveRequest(query="cats"), {})
    assert [r.title for r in results] == ["Cats", "More"]
    assert results[0].snippet == "about cats"
    assert results[0].score == 1.5
    assert results[0].published_date == "2026-01-05T00:00:00"
    assert results[1].published_date is None


@respx.mock
async def test_domains_become_operators(adapter):
    route = respx.get(URL).mock(return_value=httpx.Response(200, json=BODY))
    await adapter.search(EffectiveRequest(
        query="cats", include_domains=["a.com"], exclude_domains=["c.com"]), {})
    assert query_of(route)["q"] == ["cats (site:a.com) -site:c.com"]


@respx.mock
@pytest.mark.parametrize("freshness", ["day", "month", "year"])
async def test_supported_time_ranges_pass_through(adapter, freshness):
    route = respx.get(URL).mock(return_value=httpx.Response(200, json=BODY))
    await adapter.search(EffectiveRequest(query="cats", freshness=freshness), {})
    assert query_of(route)["time_range"] == [freshness]


@respx.mock
async def test_week_is_dropped_because_searxng_has_no_week_window(adapter):
    route = respx.get(URL).mock(return_value=httpx.Response(200, json=BODY))
    await adapter.search(EffectiveRequest(query="cats", freshness="week"), {})
    assert "time_range" not in query_of(route)


def test_week_is_reported_as_degraded_but_other_windows_are_not(adapter):
    assert adapter.extra_degraded(EffectiveRequest(query="c", freshness="week")) == ["freshness"]
    assert adapter.extra_degraded(EffectiveRequest(query="c", freshness="day")) == []
    assert adapter.extra_degraded(EffectiveRequest(query="c")) == []


@respx.mock
@pytest.mark.parametrize(("safe_search", "level"),
                         [("off", "0"), ("moderate", "1"), ("strict", "2")])
async def test_safesearch_levels(adapter, safe_search, level):
    route = respx.get(URL).mock(return_value=httpx.Response(200, json=BODY))
    await adapter.search(EffectiveRequest(query="cats", safe_search=safe_search), {})
    assert query_of(route)["safesearch"] == [level]


@respx.mock
async def test_language_and_news_category(adapter):
    route = respx.get(URL).mock(return_value=httpx.Response(200, json=BODY))
    await adapter.search(EffectiveRequest(query="cats", language="fr", topic="news"), {})
    params = query_of(route)
    assert params["language"] == ["fr"]
    assert params["categories"] == ["news"]


@respx.mock
async def test_403_means_json_format_is_disabled_and_classifies_as_auth(adapter):
    respx.get(URL).mock(return_value=httpx.Response(403, json={}))
    with pytest.raises(ProviderError) as exc:
        await adapter.search(EffectiveRequest(query="x"), {})
    assert exc.value.error_class is ErrorClass.AUTH


@respx.mock
async def test_empty_results(adapter):
    respx.get(URL).mock(return_value=httpx.Response(200, json={"results": []}))
    assert await adapter.search(EffectiveRequest(query="x"), {}) == []


def test_capabilities():
    caps = SearxngAdapter.capabilities
    assert caps.max_results_cap is None
    assert "country" not in caps.supported_filters
    assert "freshness" in caps.supported_filters
    assert "pageno" in caps.passthrough_params


@respx.mock
async def test_filter_then_truncate_ordering_with_unusable_row_between_usable(adapter):
    """Regression: filter (remove unusable rows), then truncate, then rank.

    Unusable row (missing url) in the middle should be discarded, leaving
    the first and third rows (now at indices 0 and 1) with ranks [1, 2].
    """
    body = {
        "query": "test",
        "results": [
            {"title": "First", "url": "https://a.com", "content": "first"},
            {"title": "Bad", "content": "no url field at all"},
            {"title": "Third", "url": "https://c.com", "content": "third"},
        ],
    }
    respx.get(URL).mock(return_value=httpx.Response(200, json=body))
    results = await adapter.search(EffectiveRequest(query="test", max_results=2), {})
    assert len(results) == 2
    assert [r.title for r in results] == ["First", "Third"]
    assert [r.rank for r in results] == [1, 2]
