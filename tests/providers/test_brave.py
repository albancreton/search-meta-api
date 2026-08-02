from datetime import date
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import respx

from search_meta_api.errors import ErrorClass, ProviderError
from search_meta_api.models import EffectiveRequest
from search_meta_api.providers.brave import BraveAdapter

URL = "https://api.search.brave.com/res/v1/web/search"

BODY = {
    "query": {"original": "cats", "more_results_available": True},
    "web": {"results": [
        {"title": "Cats", "url": "https://a.com", "description": "about cats",
         "page_age": "2026-01-05T00:00:00"},
        {"title": "More", "url": "https://b.com", "description": "more cats"},
    ]},
}


@pytest.fixture
def adapter(client):
    return BraveAdapter("brave", client, timeout_ms=5000, api_key="BSA-KEY")


def query_of(route) -> dict:
    return parse_qs(urlparse(str(route.calls.last.request.url)).query)


@respx.mock
async def test_sends_subscription_token(adapter):
    route = respx.get(URL).mock(return_value=httpx.Response(200, json=BODY))
    await adapter.search(EffectiveRequest(query="cats"), {})
    assert route.calls.last.request.headers["x-subscription-token"] == "BSA-KEY"


@respx.mock
async def test_parses_results_and_published_date(adapter):
    respx.get(URL).mock(return_value=httpx.Response(200, json=BODY))
    results = await adapter.search(EffectiveRequest(query="cats"), {})
    assert [r.title for r in results] == ["Cats", "More"]
    assert results[0].snippet == "about cats"
    assert results[0].published_date == "2026-01-05T00:00:00"
    assert results[1].published_date is None


@respx.mock
async def test_domains_become_search_operators(adapter):
    route = respx.get(URL).mock(return_value=httpx.Response(200, json=BODY))
    await adapter.search(EffectiveRequest(
        query="cats", include_domains=["a.com", "b.com"], exclude_domains=["c.com"]), {})
    assert query_of(route)["q"] == ["cats (site:a.com OR site:b.com) -site:c.com"]


@respx.mock
@pytest.mark.parametrize(
    ("freshness", "code"),
    [("day", "pd"), ("week", "pw"), ("month", "pm"), ("year", "py")],
)
async def test_relative_freshness_codes(adapter, freshness, code):
    route = respx.get(URL).mock(return_value=httpx.Response(200, json=BODY))
    await adapter.search(EffectiveRequest(query="cats", freshness=freshness), {})
    assert query_of(route)["freshness"] == [code]


@respx.mock
async def test_date_range_overrides_relative_freshness(adapter):
    route = respx.get(URL).mock(return_value=httpx.Response(200, json=BODY))
    await adapter.search(EffectiveRequest(
        query="cats", freshness="day",
        start_date=date(2026, 1, 1), end_date=date(2026, 2, 1)), {})
    assert query_of(route)["freshness"] == ["2026-01-01to2026-02-01"]


@respx.mock
async def test_one_sided_date_range_uses_open_bounds(adapter):
    route = respx.get(URL).mock(return_value=httpx.Response(200, json=BODY))
    await adapter.search(EffectiveRequest(query="cats", start_date=date(2026, 1, 1)), {})
    assert query_of(route)["freshness"] == ["2026-01-01to2999-12-31"]


@respx.mock
async def test_count_country_language_and_safesearch(adapter):
    route = respx.get(URL).mock(return_value=httpx.Response(200, json=BODY))
    await adapter.search(EffectiveRequest(
        query="cats", max_results=7, country="fr", language="fr",
        safe_search="strict"), {})
    params = query_of(route)
    assert params["count"] == ["7"]
    assert params["country"] == ["FR"]        # Brave wants uppercase
    assert params["search_lang"] == ["fr"]    # and lowercase here
    assert params["safesearch"] == ["strict"]


@respx.mock
async def test_missing_web_key_is_an_empty_result_set_not_an_error(adapter):
    respx.get(URL).mock(return_value=httpx.Response(200, json={"query": {"original": "x"}}))
    assert await adapter.search(EffectiveRequest(query="x"), {}) == []


@respx.mock
async def test_rate_limit_is_quota(adapter):
    respx.get(URL).mock(return_value=httpx.Response(429, json={}))
    with pytest.raises(ProviderError) as exc:
        await adapter.search(EffectiveRequest(query="x"), {})
    assert exc.value.error_class is ErrorClass.QUOTA


def test_capabilities():
    caps = BraveAdapter.capabilities
    assert caps.max_results_cap == 20
    assert "include_domains" in caps.supported_filters   # via site: operators
    assert "topic" not in caps.supported_filters          # separate endpoints
    assert "extra_snippets" in caps.passthrough_params


@respx.mock
async def test_unusable_row_does_not_create_a_rank_gap_or_consume_a_truncation_slot(adapter):
    body = {
        "query": {"original": "cats"},
        "web": {"results": [
            {"title": "Cats", "url": "https://a.com", "description": "about cats"},
            {"title": "No URL", "description": "dropped"},
            {"title": "Dogs", "url": "https://c.com", "description": "about dogs"},
        ]},
    }
    respx.get(URL).mock(return_value=httpx.Response(200, json=body))
    results = await adapter.search(EffectiveRequest(query="cats", max_results=2), {})
    assert [r.title for r in results] == ["Cats", "Dogs"]
    assert [r.rank for r in results] == [1, 2]
