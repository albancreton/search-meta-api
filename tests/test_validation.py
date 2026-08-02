import httpx
import pytest

from search_meta_api.models import SearchRequest
from search_meta_api.providers.base import Adapter, Capabilities, build_results
from search_meta_api.router import ProviderInstance
from search_meta_api.validation import ParamError, validate_params


class FakeTavily(Adapter):
    type_name = "tavily"
    capabilities = Capabilities(
        supported_filters=frozenset({"country"}),
        max_results_cap=20,
        passthrough_params={"search_depth": str, "chunks_per_source": int},
    )

    async def search(self, req, passthrough):
        return []


@pytest.fixture
def instances():
    client = httpx.AsyncClient()
    return [
        ProviderInstance("tavily-a", FakeTavily("tavily-a", client)),
        ProviderInstance("tavily-b", FakeTavily("tavily-b", client)),
    ]


def check(params, instances):
    validate_params(SearchRequest(query="x", params=params), instances)


def test_valid_params_pass(instances):
    check({"*/country": "fr", "tavily/search_depth": "advanced",
           "tavily-b/chunks_per_source": 2}, instances)


def test_unscoped_key_is_rejected(instances):
    with pytest.raises(ParamError, match="must be scoped"):
        check({"country": "fr"}, instances)


def test_unknown_scope_is_rejected(instances):
    with pytest.raises(ParamError, match="unknown scope 'tvaily'"):
        check({"tvaily/search_depth": "advanced"}, instances)


def test_universal_scope_only_accepts_unified_fields(instances):
    with pytest.raises(ParamError, match="not a unified field"):
        check({"*/search_depth": "advanced"}, instances)


def test_unknown_vendor_param_is_rejected(instances):
    with pytest.raises(ParamError, match="no parameter 'serch_depth'"):
        check({"tavily/serch_depth": "advanced"}, instances)


def test_wrong_passthrough_type_is_rejected(instances):
    with pytest.raises(ParamError, match="must be int"):
        check({"tavily/chunks_per_source": "two"}, instances)


def test_scoped_unified_field_is_allowed(instances):
    check({"tavily-b/max_results": 15}, instances)


def test_flat_field_and_universal_twin_together_is_rejected(instances):
    req = SearchRequest(query="x", country="fr", params={"*/country": "de"})
    with pytest.raises(ParamError, match="both as a top-level field"):
        validate_params(req, instances)


def test_flat_field_alone_is_fine(instances):
    validate_params(SearchRequest(query="x", country="fr"), instances)


def test_bad_value_for_a_unified_field_is_caught_before_any_provider_runs(instances):
    with pytest.raises(ParamError, match="invalid value"):
        check({"*/freshness": "fortnight"}, instances)


def test_out_of_range_value_is_caught(instances):
    with pytest.raises(ParamError, match="invalid value"):
        check({"tavily/max_results": 0}, instances)


def test_explicit_top_level_value_equal_to_the_default_still_conflicts_with_universal_twin(
    instances,
):
    """A caller who explicitly sends max_results=10 (the model default) alongside
    '*/max_results' has still declared the same field twice — this must be
    rejected regardless of whether the explicit value happens to match the
    default. Detecting "the caller set this" must use model_fields_set, not a
    comparison against the default value.
    """
    req = SearchRequest(query="x", max_results=10, params={"*/max_results": 5})
    with pytest.raises(ParamError, match="both as a top-level field"):
        validate_params(req, instances)
