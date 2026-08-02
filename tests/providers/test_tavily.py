import json
from datetime import date

import httpx
import pytest
import respx

from search_meta_api.errors import ErrorClass, ProviderError
from search_meta_api.models import EffectiveRequest
from search_meta_api.providers.tavily import TavilyAdapter

URL = "https://api.tavily.com/search"

BODY = {
    "query": "cats",
    "results": [
        {"title": "Cats", "url": "https://a.com", "content": "about cats",
         "score": 0.91, "favicon": "https://a.com/f.ico"},
        {"title": "More", "url": "https://b.com", "content": "more cats", "score": 0.7},
    ],
    "response_time": 0.64,
}


@pytest.fixture
def adapter(client):
    return TavilyAdapter("tavily-a", client, timeout_ms=5000, api_key="tvly-AAAAA")


def sent(route) -> dict:
    return json.loads(route.calls.last.request.content)


@respx.mock
async def test_sends_bearer_auth_and_query(adapter):
    route = respx.post(URL).mock(return_value=httpx.Response(200, json=BODY))
    await adapter.search(EffectiveRequest(query="cats"), {})
    assert route.calls.last.request.headers["authorization"] == "Bearer tvly-AAAAA"
    assert sent(route)["query"] == "cats"


@respx.mock
async def test_parses_results_into_unified_shape(adapter):
    respx.post(URL).mock(return_value=httpx.Response(200, json=BODY))
    results = await adapter.search(EffectiveRequest(query="cats"), {})
    assert [r.title for r in results] == ["Cats", "More"]
    assert [r.rank for r in results] == [1, 2]
    assert results[0].url == "https://a.com"
    assert results[0].snippet == "about cats"
    assert results[0].score == 0.91
    assert results[0].favicon == "https://a.com/f.ico"
    assert results[1].favicon is None


@respx.mock
async def test_truncates_to_max_results(adapter):
    respx.post(URL).mock(return_value=httpx.Response(200, json=BODY))
    results = await adapter.search(EffectiveRequest(query="cats", max_results=1), {})
    assert len(results) == 1


@respx.mock
async def test_translates_filters(adapter):
    route = respx.post(URL).mock(return_value=httpx.Response(200, json=BODY))
    await adapter.search(EffectiveRequest(
        query="cats", max_results=7, include_domains=["a.com"], exclude_domains=["b.com"],
        freshness="week", start_date=date(2026, 1, 1), end_date=date(2026, 2, 1),
        country="fr", topic="news",
    ), {})
    payload = sent(route)
    assert payload["max_results"] == 7
    assert payload["include_domains"] == ["a.com"]
    assert payload["exclude_domains"] == ["b.com"]
    assert payload["time_range"] == "week"
    assert payload["start_date"] == "2026-01-01"
    assert payload["end_date"] == "2026-02-01"
    assert payload["country"] == "france"       # Tavily wants names, not ISO codes
    assert payload["topic"] == "news"


@respx.mock
async def test_omits_unset_filters(adapter):
    route = respx.post(URL).mock(return_value=httpx.Response(200, json=BODY))
    await adapter.search(EffectiveRequest(query="cats"), {})
    payload = sent(route)
    for absent in ("time_range", "start_date", "end_date", "country",
                   "include_domains", "exclude_domains"):
        assert absent not in payload


@respx.mock
async def test_passthrough_params_are_merged_into_the_payload(adapter):
    route = respx.post(URL).mock(return_value=httpx.Response(200, json=BODY))
    await adapter.search(EffectiveRequest(query="cats"),
                         {"search_depth": "advanced", "chunks_per_source": 2})
    payload = sent(route)
    assert payload["search_depth"] == "advanced"
    assert payload["chunks_per_source"] == 2


@respx.mock
async def test_unusable_row_does_not_create_a_rank_gap_or_consume_a_truncation_slot(adapter):
    body = {
        "query": "cats",
        "results": [
            {"title": "Cats", "url": "https://a.com", "content": "about cats"},
            {"title": "No URL", "content": "dropped"},
            {"title": "Dogs", "url": "https://c.com", "content": "about dogs"},
        ],
    }
    respx.post(URL).mock(return_value=httpx.Response(200, json=body))
    results = await adapter.search(EffectiveRequest(query="cats", max_results=2), {})
    assert [r.title for r in results] == ["Cats", "Dogs"]
    assert [r.rank for r in results] == [1, 2]


@respx.mock
async def test_empty_results_is_an_empty_list_not_an_error(adapter):
    respx.post(URL).mock(return_value=httpx.Response(200, json={"query": "x", "results": []}))
    assert await adapter.search(EffectiveRequest(query="x"), {}) == []


@respx.mock
@pytest.mark.parametrize(
    ("status", "expected"),
    [(401, ErrorClass.AUTH), (429, ErrorClass.QUOTA), (432, ErrorClass.QUOTA),
     (433, ErrorClass.QUOTA), (500, ErrorClass.TRANSIENT)],
)
async def test_error_statuses_classify_correctly(adapter, status, expected):
    respx.post(URL).mock(return_value=httpx.Response(
        status, json={"detail": {"error": "nope"}}))
    with pytest.raises(ProviderError) as exc:
        await adapter.search(EffectiveRequest(query="x"), {})
    assert exc.value.error_class is expected


@respx.mock
async def test_missing_results_key_is_malformed(adapter):
    respx.post(URL).mock(return_value=httpx.Response(200, json={"query": "x"}))
    with pytest.raises(ProviderError) as exc:
        await adapter.search(EffectiveRequest(query="x"), {})
    assert exc.value.error_class is ErrorClass.MALFORMED


def test_capabilities():
    caps = TavilyAdapter.capabilities
    assert caps.max_results_cap == 20
    assert "country" in caps.supported_filters
    assert "language" not in caps.supported_filters
    assert "safe_search" not in caps.supported_filters   # enterprise-gated
    assert "search_depth" in caps.passthrough_params
    # `safe_search` is a unified field name, so it can never become passthrough
    # (see test_no_adapter_advertises_an_unreachable_passthrough_param).
    assert "safe_search" not in caps.passthrough_params
