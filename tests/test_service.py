import pytest
from test_app import SETTINGS_NO_AUTH, Fake, fail, hit

from search_meta_api.app import create_app
from search_meta_api.config import Settings
from search_meta_api.errors import ErrorClass
from search_meta_api.models import SearchRequest
from search_meta_api.router import ProviderInstance
from search_meta_api.service import AllProvidersFailed, describe_providers, run_search


def state_for(*instances):
    """An app's state without going through the HTTP layer.

    run_search takes `app.state` rather than a bespoke context object so the
    two surfaces read the same settings, instances, and breaker.
    """
    return create_app(SETTINGS_NO_AUTH, instances=list(instances)).state


async def test_run_search_returns_the_first_positive_answer():
    response = await run_search(
        state_for(fail("a", 500), hit("b", [("Cats", "https://a.com")])),
        SearchRequest(query="cats"),
    )
    assert response.provider == "b"
    assert response.results[0].title == "Cats"
    assert [a.outcome for a in response.attempts] == ["error", "success"]


async def test_run_search_applies_the_configured_max_results_default():
    """The default must not land in model_fields_set — an applied default that
    looks caller-supplied would falsely conflict with a '*/max_results'
    override. Asserting via that conflict is the only way to see the
    difference from outside.

    `resolve_request` never mutates `req` itself — it builds a fresh
    per-provider `EffectiveRequest` — so the override is only observable on
    what the adapter actually receives, not on `req.max_results` afterwards.
    """
    settings = Settings(defaults={"max_results": 3},
                        providers=[{"name": "a", "type": "tavily", "api_key": "k"}])
    captured = {}

    class Capturing(Fake):
        async def search(self, req, passthrough):
            captured["max_results"] = req.max_results
            return []

    state = create_app(settings, instances=[ProviderInstance("a", Capturing("a", []))]).state
    req = SearchRequest(query="cats", params={"*/max_results": 5})
    await run_search(state, req)  # must not raise a conflict
    assert captured["max_results"] == 5


async def test_run_search_raises_all_providers_failed_with_the_attempts():
    with pytest.raises(AllProvidersFailed) as caught:
        await run_search(
            state_for(fail("a", 500), fail("b", 429, ErrorClass.QUOTA)),
            SearchRequest(query="cats"),
        )
    payload = caught.value.payload
    assert payload["error"] == "all_providers_failed"
    assert payload["query"] == "cats"
    assert [a["provider"] for a in payload["attempts"]] == ["a", "b"]
    assert payload["elapsed_ms"] >= 0


async def test_run_search_returns_a_winnerless_success_when_everyone_is_empty():
    response = await run_search(state_for(hit("a", []), hit("b", [])),
                                SearchRequest(query="cats"))
    assert response.provider is None
    assert response.results == []


async def test_describe_providers_reports_capabilities_and_breaker_state():
    payload = describe_providers(state_for(hit("a", [])))
    entry = payload["providers"][0]
    assert entry["name"] == "a"
    assert entry["type"] == "tavily"
    assert entry["supported_filters"] == ["country"]
    assert entry["max_results_cap"] == 20
    assert entry["passthrough_params"] == ["chunks_per_source", "search_depth"]
    assert entry["state"]
