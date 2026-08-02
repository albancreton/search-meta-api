import httpx

from search_meta_api.app import create_app
from search_meta_api.config import Settings
from search_meta_api.errors import ErrorClass, ProviderError
from search_meta_api.models import SearchResult
from search_meta_api.providers.base import Adapter, Capabilities
from search_meta_api.router import ProviderInstance

SETTINGS_NO_AUTH = Settings(providers=[{"name": "fake-a", "type": "tavily",
                                        "api_key": "k"}])


class Fake(Adapter):
    type_name = "tavily"
    capabilities = Capabilities(
        supported_filters=frozenset({"country"}),
        max_results_cap=20,
        passthrough_params={"search_depth": str, "chunks_per_source": int},
    )

    def __init__(self, name, outcome):
        super().__init__(name, client=None)
        self.outcome = outcome

    async def search(self, req, passthrough):
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


def hit(name, results):
    return ProviderInstance(name, Fake(name, [
        SearchResult(title=t, url=u, snippet="s", rank=i + 1)
        for i, (t, u) in enumerate(results)
    ]))


def fail(name, status, error_class=ErrorClass.TRANSIENT):
    return ProviderInstance(name, Fake(name, ProviderError(name, error_class, status)))


async def call(app, path="/search", json=None, headers=None, method="POST"):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        return await c.request(method, path, json=json, headers=headers)


async def test_first_success_is_returned():
    app = create_app(SETTINGS_NO_AUTH, instances=[
        fail("a", 500), hit("b", [("Cats", "https://a.com")])])
    response = await call(app, json={"query": "cats"})
    assert response.status_code == 200
    body = response.json()
    assert body["provider"] == "b"
    assert body["results"][0]["title"] == "Cats"
    assert [a["outcome"] for a in body["attempts"]] == ["error", "success"]
    assert body["elapsed_ms"] >= 0


async def test_all_empty_is_a_200_with_no_winner():
    app = create_app(SETTINGS_NO_AUTH, instances=[hit("a", []), hit("b", [])])
    response = await call(app, json={"query": "cats"})
    assert response.status_code == 200
    body = response.json()
    assert body["provider"] is None
    assert body["results"] == []


async def test_dropped_filters_are_reported_even_when_no_provider_had_results():
    """An empty answer is exactly when a caller most needs to know a filter was
    dropped — it may be why the answer was empty.
    """
    app = create_app(SETTINGS_NO_AUTH, instances=[hit("a", []), hit("b", [])])
    body = (await call(app, json={"query": "cats", "language": "fr"})).json()
    assert body["provider"] is None
    assert body["results"] == []
    assert body["degraded"] == ["language"]


async def test_all_failed_is_a_503_carrying_the_attempts():
    app = create_app(SETTINGS_NO_AUTH, instances=[fail("a", 500), fail("b", 401,
                                                                      ErrorClass.AUTH)])
    response = await call(app, json={"query": "cats"})
    assert response.status_code == 503
    detail = response.json()["detail"]
    assert detail["error"] == "all_providers_failed"
    assert [a["provider"] for a in detail["attempts"]] == ["a", "b"]


async def test_degraded_filters_are_reported():
    app = create_app(SETTINGS_NO_AUTH, instances=[hit("a", [("Cats", "https://a.com")])])
    response = await call(app, json={"query": "cats", "language": "fr"})
    body = response.json()
    assert body["degraded"] == ["language"]
    assert body["attempts"][0]["degraded"] == ["language"]


async def test_supported_filter_is_not_degraded():
    app = create_app(SETTINGS_NO_AUTH, instances=[hit("a", [("Cats", "https://a.com")])])
    body = (await call(app, json={"query": "cats", "country": "fr"})).json()
    assert body["degraded"] == []


async def test_bad_namespaced_param_is_422_and_calls_nobody():
    app = create_app(SETTINGS_NO_AUTH, instances=[fail("a", 500)])
    response = await call(app, json={"query": "cats",
                                     "params": {"tavily/serch_depth": "advanced"}})
    assert response.status_code == 422
    assert "no parameter" in response.json()["detail"]


async def test_a_numeric_string_max_results_in_params_is_not_a_500():
    """Regression: `min(value, cap)` on a str raised TypeError, which is not a
    ParamError and not a ValidationError, so it escaped both handlers and became
    an unauthenticated remote 500. Every scope spelling must behave the same.
    """
    for key in ("*/max_results", "tavily/max_results", "a/max_results"):
        app = create_app(SETTINGS_NO_AUTH,
                         instances=[hit("a", [("Cats", "https://a.com")])])
        response = await call(app, json={"query": "cats", "params": {key: "5"}})
        assert response.status_code == 200, key


async def test_an_unparseable_scoped_unified_value_is_a_422_naming_the_param():
    app = create_app(SETTINGS_NO_AUTH, instances=[hit("a", [("Cats", "https://a.com")])])
    response = await call(app, json={"query": "cats",
                                     "params": {"*/max_results": "many"}})
    assert response.status_code == 422
    assert "*/max_results" in response.json()["detail"]


async def test_a_boolean_max_results_in_params_is_rejected_not_silently_coerced():
    app = create_app(SETTINGS_NO_AUTH, instances=[hit("a", [("Cats", "https://a.com")])])
    response = await call(app, json={"query": "cats", "params": {"*/max_results": True}})
    assert response.status_code == 422


async def test_a_boolean_int_passthrough_param_is_rejected():
    app = create_app(SETTINGS_NO_AUTH, instances=[hit("a", [("Cats", "https://a.com")])])
    response = await call(app, json={"query": "cats",
                                     "params": {"tavily/chunks_per_source": True}})
    assert response.status_code == 422


async def test_missing_query_is_422():
    app = create_app(SETTINGS_NO_AUTH, instances=[hit("a", [])])
    assert (await call(app, json={})).status_code == 422


async def test_configured_default_max_results_applies_when_the_caller_omits_it():
    settings = Settings(defaults={"max_results": 3},
                        providers=[{"name": "a", "type": "tavily", "api_key": "k"}])
    captured = {}

    class Capturing(Fake):
        async def search(self, req, passthrough):
            captured["max_results"] = req.max_results
            return []

    app = create_app(settings, instances=[ProviderInstance("a", Capturing("a", []))])
    await call(app, json={"query": "cats"})
    assert captured["max_results"] == 3


async def test_explicit_max_results_beats_the_configured_default():
    settings = Settings(defaults={"max_results": 3},
                        providers=[{"name": "a", "type": "tavily", "api_key": "k"}])
    captured = {}

    class Capturing(Fake):
        async def search(self, req, passthrough):
            captured["max_results"] = req.max_results
            return []

    app = create_app(settings, instances=[ProviderInstance("a", Capturing("a", []))])
    await call(app, json={"query": "cats", "max_results": 7})
    assert captured["max_results"] == 7


async def test_universal_max_results_override_not_falsely_rejected_when_default_isnt_10():
    """Regression: applying the configured default onto req.max_results before
    validate_params ran made the *default* look like a caller-supplied value,
    so a genuine '*/max_results' override was rejected as a duplicate whenever
    the configured default differed from the model default (10).
    """
    settings = Settings(defaults={"max_results": 3},
                        providers=[{"name": "a", "type": "tavily", "api_key": "k"}])
    captured = {}

    class Capturing(Fake):
        async def search(self, req, passthrough):
            captured["max_results"] = req.max_results
            return []

    app = create_app(settings, instances=[ProviderInstance("a", Capturing("a", []))])
    response = await call(app, json={"query": "cats", "params": {"*/max_results": 5}})
    assert response.status_code == 200
    assert captured["max_results"] == 5


async def test_extra_degraded_hook_is_merged_into_response_and_attempt():
    """extra_degraded reports value-dependent gaps for filters that ARE in
    supported_filters (unlike resolve_request's static degraded list), so this
    is the only path that can catch a regression in the merge in app.py.
    """
    class WithExtraDegraded(Fake):
        def extra_degraded(self, req):
            return ["country"]  # "country" IS in Fake.capabilities.supported_filters

    instance = ProviderInstance("a", WithExtraDegraded("a", [
        SearchResult(title="Cats", url="https://a.com", snippet="s", rank=1)
    ]))
    app = create_app(SETTINGS_NO_AUTH, instances=[instance])
    # "language" is a static gap (not in supported_filters); "country" only
    # shows up via extra_degraded. The union must contain both, sorted.
    response = await call(app, json={"query": "cats", "language": "fr"})
    body = response.json()
    assert body["degraded"] == ["country", "language"]
    assert body["attempts"][0]["degraded"] == ["country", "language"]


async def test_auth_is_enforced_when_a_token_is_configured():
    settings = Settings(server={"auth_token": "secret"},
                        providers=[{"name": "a", "type": "tavily", "api_key": "k"}])
    app = create_app(settings, instances=[hit("a", [("Cats", "https://a.com")])])
    assert (await call(app, json={"query": "c"})).status_code == 401
    assert (await call(app, json={"query": "c"},
                       headers={"Authorization": "Bearer wrong"})).status_code == 401
    assert (await call(app, json={"query": "c"},
                       headers={"Authorization": "Bearer secret"})).status_code == 200


async def test_a_non_ascii_authorization_header_is_a_401_not_a_500():
    """secrets.compare_digest raises TypeError on a non-ASCII str, and uvicorn
    decodes headers as latin-1 — so `Authorization: Bearer sécret` gave an
    unauthenticated remote a 500. The bytes are sent latin-1 encoded here to
    mirror exactly what arrives off the wire.
    """
    settings = Settings(server={"auth_token": "secret"},
                        providers=[{"name": "a", "type": "tavily", "api_key": "k"}])
    app = create_app(settings, instances=[hit("a", [("Cats", "https://a.com")])])
    response = await call(app, json={"query": "c"},
                          headers={"Authorization": "Bearer sécret".encode("latin-1")})
    assert response.status_code == 401


async def test_auth_is_disabled_when_no_token_is_configured():
    app = create_app(SETTINGS_NO_AUTH, instances=[hit("a", [("Cats", "https://a.com")])])
    assert (await call(app, json={"query": "c"})).status_code == 200


async def test_health_is_public_and_reports_breaker_state():
    settings = Settings(server={"auth_token": "secret"},
                        providers=[{"name": "a", "type": "tavily", "api_key": "k"}])
    app = create_app(settings, instances=[hit("a", [])])
    response = await call(app, "/health", method="GET")
    assert response.status_code == 200
    assert response.json()["providers"] == [{"name": "a", "state": "closed"}]


async def test_providers_endpoint_lists_order_and_capabilities():
    app = create_app(SETTINGS_NO_AUTH, instances=[hit("a", []), hit("b", [])])
    body = (await call(app, "/providers", method="GET")).json()
    assert [p["name"] for p in body["providers"]] == ["a", "b"]
    assert body["providers"][0]["type"] == "tavily"
    assert "country" in body["providers"][0]["supported_filters"]
