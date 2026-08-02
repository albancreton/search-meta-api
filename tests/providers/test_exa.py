import json
from datetime import date

import httpx
import pytest
import respx

from search_meta_api.errors import ErrorClass, ProviderError
from search_meta_api.models import EffectiveRequest
from search_meta_api.providers.exa import ExaAdapter

URL = "https://api.exa.ai/search"

BODY = {
    "requestId": "abc",
    "results": [
        {"title": "Cats", "url": "https://a.com", "publishedDate": "2026-01-05T00:00:00.000Z",
         "favicon": "https://a.com/f.ico", "highlights": ["about cats", "more"],
         "highlightScores": [0.88, 0.4]},
        {"title": "More", "url": "https://b.com", "highlights": []},
    ],
    "costDollars": {"total": 0.007},
}


@pytest.fixture
def adapter(client):
    return ExaAdapter("exa", client, timeout_ms=5000, api_key="exa-key")


def sent(route) -> dict:
    return json.loads(route.calls.last.request.content)


@respx.mock
async def test_sends_api_key_header(adapter):
    route = respx.post(URL).mock(return_value=httpx.Response(200, json=BODY))
    await adapter.search(EffectiveRequest(query="cats"), {})
    assert route.calls.last.request.headers["x-api-key"] == "exa-key"


@respx.mock
async def test_always_requests_highlights_so_snippets_exist(adapter):
    route = respx.post(URL).mock(return_value=httpx.Response(200, json=BODY))
    await adapter.search(EffectiveRequest(query="cats"), {})
    assert sent(route)["contents"] == {"highlights": {"maxCharacters": 500}}


@respx.mock
async def test_snippet_comes_from_the_first_highlight(adapter):
    respx.post(URL).mock(return_value=httpx.Response(200, json=BODY))
    results = await adapter.search(EffectiveRequest(query="cats"), {})
    assert results[0].snippet == "about cats"
    assert results[0].score == 0.88
    assert results[0].published_date == "2026-01-05T00:00:00.000Z"
    assert results[0].favicon == "https://a.com/f.ico"
    assert results[1].snippet == ""
    assert results[1].score is None


@respx.mock
async def test_translates_filters(adapter):
    route = respx.post(URL).mock(return_value=httpx.Response(200, json=BODY))
    await adapter.search(EffectiveRequest(
        query="cats", max_results=7, include_domains=["a.com"], exclude_domains=["b.com"],
        start_date=date(2026, 1, 1), end_date=date(2026, 2, 1), country="fr",
        safe_search="strict", topic="news"), {})
    payload = sent(route)
    assert payload["numResults"] == 7
    assert payload["includeDomains"] == ["a.com"]
    assert payload["excludeDomains"] == ["b.com"]
    assert payload["startPublishedDate"] == "2026-01-01T00:00:00.000Z"
    assert payload["endPublishedDate"] == "2026-02-01T00:00:00.000Z"
    assert payload["userLocation"] == "FR"
    assert payload["moderation"] is True
    assert payload["category"] == "news"


@respx.mock
async def test_safe_search_off_disables_moderation(adapter):
    route = respx.post(URL).mock(return_value=httpx.Response(200, json=BODY))
    await adapter.search(EffectiveRequest(query="cats", safe_search="off"), {})
    assert sent(route)["moderation"] is False


@respx.mock
async def test_general_topic_sends_no_category(adapter):
    route = respx.post(URL).mock(return_value=httpx.Response(200, json=BODY))
    await adapter.search(EffectiveRequest(query="cats"), {})
    assert "category" not in sent(route)


@respx.mock
async def test_passthrough_params(adapter):
    route = respx.post(URL).mock(return_value=httpx.Response(200, json=BODY))
    await adapter.search(EffectiveRequest(query="cats"), {"type": "fast"})
    assert sent(route)["type"] == "fast"


@respx.mock
async def test_payment_required_is_quota(adapter):
    respx.post(URL).mock(return_value=httpx.Response(402, json={}))
    with pytest.raises(ProviderError) as exc:
        await adapter.search(EffectiveRequest(query="x"), {})
    assert exc.value.error_class is ErrorClass.QUOTA


@respx.mock
async def test_empty_results(adapter):
    respx.post(URL).mock(return_value=httpx.Response(200, json={"results": []}))
    assert await adapter.search(EffectiveRequest(query="x"), {}) == []


def test_capabilities():
    caps = ExaAdapter.capabilities
    assert caps.max_results_cap == 100
    assert "freshness" not in caps.supported_filters   # no relative window
    assert "language" not in caps.supported_filters
    assert "start_date" in caps.supported_filters
    assert "type" in caps.passthrough_params


@respx.mock
async def test_missing_url_filtered_but_ranks_contiguous(adapter):
    """Rows without url are filtered out; ranks stay contiguous and 1-based."""
    body_with_missing_url = {
        "results": [
            {"title": "Valid", "url": "https://a.com", "highlights": ["snippet1"], "highlightScores": [0.9]},
            {"title": "Invalid", "highlights": ["snippet2"], "highlightScores": [0.8]},  # no url
            {"title": "Valid2", "url": "https://c.com", "highlights": ["snippet3"], "highlightScores": [0.7]},
        ]
    }
    respx.post(URL).mock(return_value=httpx.Response(200, json=body_with_missing_url))
    results = await adapter.search(EffectiveRequest(query="test"), {})
    assert len(results) == 2
    assert results[0].rank == 1
    assert results[0].url == "https://a.com"
    assert results[1].rank == 2
    assert results[1].url == "https://c.com"
