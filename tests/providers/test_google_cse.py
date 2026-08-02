from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import respx

from search_meta_api.errors import ErrorClass, ProviderError
from search_meta_api.models import EffectiveRequest
from search_meta_api.providers.google_cse import GoogleCseAdapter

URL = "https://www.googleapis.com/customsearch/v1"

BODY = {
    "kind": "customsearch#search",
    "items": [
        {"title": "Cats", "link": "https://a.com", "snippet": "about cats"},
        {"title": "More", "link": "https://b.com", "snippet": "more cats"},
    ],
}


def quota_error(reason: str) -> dict:
    return {"error": {"code": 403, "message": "quota", "errors": [{"reason": reason}]}}


@pytest.fixture
def adapter(client):
    return GoogleCseAdapter("google", client, timeout_ms=5000,
                            api_key="g-key", cx="cx-123")


def query_of(route) -> dict:
    return parse_qs(urlparse(str(route.calls.last.request.url)).query)


@respx.mock
async def test_sends_key_and_cx_as_query_params(adapter):
    route = respx.get(URL).mock(return_value=httpx.Response(200, json=BODY))
    await adapter.search(EffectiveRequest(query="cats"), {})
    params = query_of(route)
    assert params["key"] == ["g-key"]
    assert params["cx"] == ["cx-123"]
    assert params["q"] == ["cats"]


@respx.mock
async def test_parses_items_using_link_not_url(adapter):
    respx.get(URL).mock(return_value=httpx.Response(200, json=BODY))
    results = await adapter.search(EffectiveRequest(query="cats"), {})
    assert [r.url for r in results] == ["https://a.com", "https://b.com"]
    assert results[0].snippet == "about cats"
    assert [r.rank for r in results] == [1, 2]


@respx.mock
async def test_max_results_is_capped_at_ten_by_the_vendor(adapter):
    route = respx.get(URL).mock(return_value=httpx.Response(200, json=BODY))
    await adapter.search(EffectiveRequest(query="cats", max_results=10), {})
    assert query_of(route)["num"] == ["10"]
    assert GoogleCseAdapter.capabilities.max_results_cap == 10


@respx.mock
async def test_domains_become_operators_since_sitesearch_takes_only_one(adapter):
    route = respx.get(URL).mock(return_value=httpx.Response(200, json=BODY))
    await adapter.search(EffectiveRequest(
        query="cats", include_domains=["a.com", "b.com"], exclude_domains=["c.com"]), {})
    assert query_of(route)["q"] == ["cats (site:a.com OR site:b.com) -site:c.com"]
    assert "siteSearch" not in query_of(route)


@respx.mock
@pytest.mark.parametrize(("freshness", "restrict"),
                         [("day", "d1"), ("week", "w1"), ("month", "m1"), ("year", "y1")])
async def test_freshness_becomes_date_restrict(adapter, freshness, restrict):
    route = respx.get(URL).mock(return_value=httpx.Response(200, json=BODY))
    await adapter.search(EffectiveRequest(query="cats", freshness=freshness), {})
    assert query_of(route)["dateRestrict"] == [restrict]


@respx.mock
async def test_country_language_and_safesearch(adapter):
    route = respx.get(URL).mock(return_value=httpx.Response(200, json=BODY))
    await adapter.search(EffectiveRequest(
        query="cats", country="fr", language="fr", safe_search="strict"), {})
    params = query_of(route)
    assert params["gl"] == ["fr"]
    assert params["lr"] == ["lang_fr"]
    assert params["safe"] == ["active"]


@respx.mock
async def test_safe_search_off_maps_to_off(adapter):
    route = respx.get(URL).mock(return_value=httpx.Response(200, json=BODY))
    await adapter.search(EffectiveRequest(query="cats", safe_search="off"), {})
    assert query_of(route)["safe"] == ["off"]


@respx.mock
async def test_missing_items_key_means_no_matches(adapter):
    respx.get(URL).mock(return_value=httpx.Response(200, json={"kind": "customsearch#search"}))
    assert await adapter.search(EffectiveRequest(query="x"), {}) == []


@respx.mock
@pytest.mark.parametrize("reason", ["dailyLimitExceeded", "rateLimitExceeded",
                                    "userRateLimitExceeded"])
async def test_403_with_a_quota_reason_is_quota_not_auth(adapter, reason):
    respx.get(URL).mock(return_value=httpx.Response(403, json=quota_error(reason)))
    with pytest.raises(ProviderError) as exc:
        await adapter.search(EffectiveRequest(query="x"), {})
    assert exc.value.error_class is ErrorClass.QUOTA


@respx.mock
async def test_403_without_a_quota_reason_stays_auth(adapter):
    respx.get(URL).mock(return_value=httpx.Response(403, json=quota_error("keyInvalid")))
    with pytest.raises(ProviderError) as exc:
        await adapter.search(EffectiveRequest(query="x"), {})
    assert exc.value.error_class is ErrorClass.AUTH


@respx.mock
async def test_403_with_an_unparseable_body_stays_auth(adapter):
    respx.get(URL).mock(return_value=httpx.Response(403, text="nope"))
    with pytest.raises(ProviderError) as exc:
        await adapter.search(EffectiveRequest(query="x"), {})
    assert exc.value.error_class is ErrorClass.AUTH


def test_capabilities():
    caps = GoogleCseAdapter.capabilities
    assert "start_date" not in caps.supported_filters
    assert "topic" not in caps.supported_filters
    assert "include_domains" in caps.supported_filters


@respx.mock
async def test_regression_filter_then_truncate_with_unusable_row_between(adapter):
    """Verify that filtering happens before truncating, not after.

    A fixture with an unusable row (no `link`) between two usable rows,
    called with `max_results=2`, should return the first and third rows
    with ranks exactly [1, 2].
    """
    body_with_middle_gap = {
        "kind": "customsearch#search",
        "items": [
            {"title": "First", "link": "https://first.com", "snippet": "first result"},
            {"title": "NoLink", "snippet": "missing link field - should be filtered out"},
            {"title": "Third", "link": "https://third.com", "snippet": "third result"},
        ],
    }
    respx.get(URL).mock(return_value=httpx.Response(200, json=body_with_middle_gap))
    results = await adapter.search(EffectiveRequest(query="x", max_results=2), {})

    assert len(results) == 2
    assert results[0].url == "https://first.com"
    assert results[1].url == "https://third.com"
    assert [r.rank for r in results] == [1, 2]
