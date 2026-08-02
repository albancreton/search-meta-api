import json
from datetime import date

import httpx
import pytest
import respx

from search_meta_api.errors import ErrorClass, ProviderError
from search_meta_api.models import EffectiveRequest
from search_meta_api.providers.linkup import LinkupAdapter

URL = "https://api.linkup.so/v1/search"

BODY = {"results": [
    {"type": "text", "name": "Cats", "url": "https://a.com", "content": "about cats",
     "favicon": "https://a.com/f.ico"},
    {"type": "image", "name": "Cat pic", "url": "https://a.com/cat.png"},
    {"type": "text", "name": "More", "url": "https://b.com", "content": "more cats"},
]}


@pytest.fixture
def adapter(client):
    return LinkupAdapter("linkup", client, timeout_ms=5000, api_key="lk-key")


def sent(route) -> dict:
    return json.loads(route.calls.last.request.content)


@respx.mock
async def test_sends_bearer_auth_and_required_vendor_fields(adapter):
    route = respx.post(URL).mock(return_value=httpx.Response(200, json=BODY))
    await adapter.search(EffectiveRequest(query="cats"), {})
    assert route.calls.last.request.headers["authorization"] == "Bearer lk-key"
    payload = sent(route)
    assert payload["q"] == "cats"
    assert payload["depth"] == "standard"
    assert payload["outputType"] == "searchResults"


@respx.mock
async def test_image_rows_are_dropped_and_ranks_are_contiguous(adapter):
    respx.post(URL).mock(return_value=httpx.Response(200, json=BODY))
    results = await adapter.search(EffectiveRequest(query="cats"), {})
    assert [r.title for r in results] == ["Cats", "More"]
    assert [r.rank for r in results] == [1, 2]
    assert results[0].snippet == "about cats"
    assert results[0].favicon == "https://a.com/f.ico"


@respx.mock
async def test_translates_supported_filters(adapter):
    route = respx.post(URL).mock(return_value=httpx.Response(200, json=BODY))
    await adapter.search(EffectiveRequest(
        query="cats", max_results=7, include_domains=["a.com"], exclude_domains=["b.com"],
        start_date=date(2026, 1, 1), end_date=date(2026, 2, 1)), {})
    payload = sent(route)
    assert payload["maxResults"] == 7
    assert payload["includeDomains"] == ["a.com"]
    assert payload["excludeDomains"] == ["b.com"]
    assert payload["fromDate"] == "2026-01-01"
    assert payload["toDate"] == "2026-02-01"


@respx.mock
async def test_passthrough_overrides_depth(adapter):
    route = respx.post(URL).mock(return_value=httpx.Response(200, json=BODY))
    await adapter.search(EffectiveRequest(query="cats"), {"depth": "deep"})
    assert sent(route)["depth"] == "deep"


@respx.mock
async def test_truncation_counts_text_rows_only(adapter):
    respx.post(URL).mock(return_value=httpx.Response(200, json=BODY))
    results = await adapter.search(EffectiveRequest(query="cats", max_results=1), {})
    assert [r.title for r in results] == ["Cats"]


@respx.mock
@pytest.mark.parametrize(("status", "expected"),
                         [(402, ErrorClass.QUOTA), (429, ErrorClass.QUOTA),
                          (401, ErrorClass.AUTH)])
async def test_error_classification(adapter, status, expected):
    respx.post(URL).mock(return_value=httpx.Response(status, json={}))
    with pytest.raises(ProviderError) as exc:
        await adapter.search(EffectiveRequest(query="x"), {})
    assert exc.value.error_class is expected


@respx.mock
async def test_empty_results(adapter):
    respx.post(URL).mock(return_value=httpx.Response(200, json={"results": []}))
    assert await adapter.search(EffectiveRequest(query="x"), {}) == []


@respx.mock
async def test_filter_then_truncate_ordering(adapter):
    """Regression test: unusable row between usable ones must not consume a slot.

    If truncation happened first, max_results=2 would take the first 2 rows
    (text, image) and skip the 3rd (text). Filtering first ensures we get
    both text rows and ranks remain contiguous [1, 2].
    """
    body = {"results": [
        {"type": "text", "name": "First", "url": "https://a.com", "content": "first"},
        {"type": "image", "name": "Pic", "url": "https://a.com/pic.png"},
        {"type": "text", "name": "Third", "url": "https://c.com", "content": "third"},
    ]}
    respx.post(URL).mock(return_value=httpx.Response(200, json=body))
    results = await adapter.search(EffectiveRequest(query="test", max_results=2), {})
    assert [r.title for r in results] == ["First", "Third"]
    assert [r.rank for r in results] == [1, 2]


def test_capabilities():
    caps = LinkupAdapter.capabilities
    assert caps.supported_filters == frozenset({
        "include_domains", "exclude_domains", "start_date", "end_date"})
    assert caps.max_results_cap is None
    assert "depth" in caps.passthrough_params
