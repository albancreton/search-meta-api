import httpx
import pytest
import respx

from search_meta_api.errors import ErrorClass, ProviderError
from search_meta_api.models import SearchRequest, SearchResult
from search_meta_api.providers.base import (
    Adapter,
    Capabilities,
    build_results,
    domain_operators,
    resolve_request,
)

CAPS = Capabilities(
    supported_filters=frozenset({"include_domains", "freshness", "country", "topic"}),
    max_results_cap=20,
    passthrough_params={"search_depth": str, "chunks_per_source": int},
)


def resolve(req):
    return resolve_request(req, instance_name="tavily-b", type_name="tavily", capabilities=CAPS)


def test_flat_fields_pass_through_untouched():
    eff, passthrough, _degraded = resolve(SearchRequest(query="hi", country="fr"))
    assert eff.query == "hi" and eff.country == "fr"
    assert passthrough == {}


def test_universal_scope_overrides_flat_default():
    eff, _, _ = resolve(SearchRequest(query="hi", params={"*/freshness": "week"}))
    assert eff.freshness == "week"


def test_type_scope_beats_universal():
    eff, _, _ = resolve(SearchRequest(
        query="hi", params={"*/max_results": 5, "tavily/max_results": 8}))
    assert eff.max_results == 8


def test_instance_scope_beats_type_scope():
    eff, _, _ = resolve(SearchRequest(
        query="hi", params={"tavily/max_results": 8, "tavily-b/max_results": 15}))
    assert eff.max_results == 15


def test_other_instances_scope_is_ignored():
    eff, passthrough, _ = resolve(SearchRequest(query="hi", params={"tavily-a/max_results": 3}))
    assert eff.max_results == 10
    assert passthrough == {}


def test_max_results_is_clamped_to_the_provider_cap():
    eff, _, degraded = resolve(SearchRequest(query="hi", max_results=100))
    assert eff.max_results == 20
    assert degraded == []


def test_universal_scope_passthrough_param_is_dropped():
    _, passthrough, _ = resolve(SearchRequest(query="hi", params={"*/search_depth": "advanced"}))
    assert passthrough == {}


def test_scoped_vendor_params_become_passthrough():
    _, passthrough, _ = resolve(SearchRequest(
        query="hi", params={"tavily/search_depth": "advanced", "tavily-b/chunks_per_source": 2}))
    assert passthrough == {"search_depth": "advanced", "chunks_per_source": 2}


def test_instance_passthrough_beats_type_passthrough():
    _, passthrough, _ = resolve(SearchRequest(
        query="hi",
        params={"tavily/search_depth": "basic", "tavily-b/search_depth": "advanced"}))
    assert passthrough == {"search_depth": "advanced"}


def test_unsupported_filters_are_reported_as_degraded():
    eff, _, degraded = resolve(SearchRequest(query="hi", language="fr", safe_search="strict"))
    assert degraded == ["language", "safe_search"]
    assert eff.language == "fr"   # left on the request; the adapter simply ignores it


def test_supported_filters_are_not_degraded():
    _, _, degraded = resolve(SearchRequest(query="hi", country="fr", freshness="day"))
    assert degraded == []


def test_unset_filters_are_never_degraded():
    _, _, degraded = resolve(SearchRequest(query="hi"))
    assert degraded == []


def test_default_topic_is_not_treated_as_requested():
    caps = Capabilities(supported_filters=frozenset(), max_results_cap=None)
    _, _, degraded = resolve_request(
        SearchRequest(query="hi", topic="general"), "x", "x", caps)
    assert degraded == []
    _, _, degraded = resolve_request(
        SearchRequest(query="hi", topic="news"), "x", "x", caps)
    assert degraded == ["topic"]


def test_domain_operators_builds_a_google_style_query():
    assert domain_operators("cats", ["a.com", "b.com"], ["c.com"]) == (
        "cats (site:a.com OR site:b.com) -site:c.com")
    assert domain_operators("cats", [], []) == "cats"
    assert domain_operators("cats", ["a.com"], []) == "cats (site:a.com)"


def _extract(row: dict, rank: int) -> SearchResult:
    return SearchResult(
        title=row.get("title") or "", url=row.get("url", ""), snippet="", rank=rank,
    )


def test_build_results_ranks_stay_contiguous_around_an_unusable_row_in_the_middle():
    rows = [{"url": "a", "title": "A"}, {"title": "no url"}, {"url": "c", "title": "C"}]
    results = build_results(rows, 10, _extract)
    assert [r.title for r in results] == ["A", "C"]
    assert [r.rank for r in results] == [1, 2]


def test_build_results_does_not_let_an_unusable_row_consume_a_truncation_slot():
    rows = [{"url": "a", "title": "A"}, {"title": "no url"}, {"url": "c", "title": "C"}]
    results = build_results(rows, 2, _extract)
    assert [r.title for r in results] == ["A", "C"]
    assert [r.rank for r in results] == [1, 2]


def test_build_results_limit_larger_than_row_count_is_harmless():
    rows = [{"url": "a", "title": "A"}]
    results = build_results(rows, 10, _extract)
    assert [r.title for r in results] == ["A"]
    assert [r.rank for r in results] == [1]


def test_build_results_empty_rows_yields_empty_list():
    assert build_results([], 10, _extract) == []


def test_build_results_honours_a_custom_keep_predicate():
    rows = [{"link": "a", "title": "A"}, {"title": "no link"}, {"link": "c", "title": "C"}]

    def extract_link(row: dict, rank: int) -> SearchResult:
        return SearchResult(title=row["title"], url=row["link"], snippet="", rank=rank)

    results = build_results(rows, 10, extract_link, keep=lambda row: bool(row.get("link")))
    assert [r.title for r in results] == ["A", "C"]
    assert [r.rank for r in results] == [1, 2]


def test_extra_degraded_defaults_to_empty():
    probe = _Probe("p", httpx.AsyncClient())
    assert probe.extra_degraded(SearchRequest(query="hi")) == []


def test_extra_degraded_can_be_overridden_for_value_dependent_gaps():
    class Picky(_Probe):
        def extra_degraded(self, req):
            return ["freshness"] if req.freshness == "week" else []

    picky = Picky("p", httpx.AsyncClient())
    assert picky.extra_degraded(SearchRequest(query="hi", freshness="week")) == ["freshness"]
    assert picky.extra_degraded(SearchRequest(query="hi", freshness="day")) == []


class _Probe(Adapter):
    type_name = "probe"
    capabilities = CAPS

    async def search(self, req, passthrough):
        return []


@pytest.fixture
def probe():
    return _Probe("probe-1", httpx.AsyncClient(), timeout_ms=5000)


@respx.mock
async def test_request_json_returns_parsed_body(probe):
    respx.get("https://x.test/s").mock(return_value=httpx.Response(200, json={"ok": True}))
    assert await probe.request_json("GET", "https://x.test/s") == {"ok": True}


@respx.mock
@pytest.mark.parametrize(
    ("status", "expected"),
    [(401, ErrorClass.AUTH), (432, ErrorClass.QUOTA), (503, ErrorClass.TRANSIENT),
     (400, ErrorClass.MALFORMED)],
)
async def test_request_json_classifies_error_statuses(probe, status, expected):
    respx.get("https://x.test/s").mock(return_value=httpx.Response(status, json={}))
    with pytest.raises(ProviderError) as exc:
        await probe.request_json("GET", "https://x.test/s")
    assert exc.value.error_class is expected
    assert exc.value.status == status
    assert exc.value.provider == "probe-1"


@respx.mock
async def test_request_json_reads_retry_after(probe):
    respx.get("https://x.test/s").mock(
        return_value=httpx.Response(429, headers={"Retry-After": "120"}, json={}))
    with pytest.raises(ProviderError) as exc:
        await probe.request_json("GET", "https://x.test/s")
    assert exc.value.retry_after_s == 120.0


@respx.mock
async def test_timeout_becomes_a_transient_error(probe):
    respx.get("https://x.test/s").mock(side_effect=httpx.ConnectTimeout("slow"))
    with pytest.raises(ProviderError) as exc:
        await probe.request_json("GET", "https://x.test/s")
    assert exc.value.error_class is ErrorClass.TRANSIENT


@respx.mock
async def test_connection_error_becomes_a_transient_error(probe):
    respx.get("https://x.test/s").mock(side_effect=httpx.ConnectError("down"))
    with pytest.raises(ProviderError) as exc:
        await probe.request_json("GET", "https://x.test/s")
    assert exc.value.error_class is ErrorClass.TRANSIENT


@respx.mock
async def test_unparseable_body_becomes_malformed(probe):
    respx.get("https://x.test/s").mock(return_value=httpx.Response(200, text="<html>"))
    with pytest.raises(ProviderError) as exc:
        await probe.request_json("GET", "https://x.test/s")
    assert exc.value.error_class is ErrorClass.MALFORMED


@respx.mock
async def test_json_array_body_becomes_malformed(probe):
    respx.get("https://x.test/s").mock(return_value=httpx.Response(200, json=[1, 2, 3]))
    with pytest.raises(ProviderError) as exc:
        await probe.request_json("GET", "https://x.test/s")
    assert exc.value.error_class is ErrorClass.MALFORMED


@respx.mock
async def test_retry_after_http_date_is_ignored(probe):
    respx.get("https://x.test/s").mock(
        return_value=httpx.Response(
            429, headers={"Retry-After": "Wed, 21 Oct 2015 07:28:00 GMT"}, json={}))
    with pytest.raises(ProviderError) as exc:
        await probe.request_json("GET", "https://x.test/s")
    assert exc.value.retry_after_s is None
