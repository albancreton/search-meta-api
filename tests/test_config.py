import pytest
import yaml

from search_meta_api.config import ConfigError, load_settings

MINIMAL = {
    "server": {"auth_token": "secret"},
    "providers": [
        {"name": "tavily-a", "type": "tavily", "api_key": "tvly-AAAAA"},
        {"name": "tavily-b", "type": "tavily", "api_key": "tvly-BBBBB", "timeout_ms": 15000},
    ],
}


def write(tmp_path, data):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(data))
    return path


def test_loads_providers_in_declaration_order(tmp_path):
    settings = load_settings(write(tmp_path, MINIMAL))
    assert [p.name for p in settings.providers] == ["tavily-a", "tavily-b"]


def test_applies_defaults(tmp_path):
    settings = load_settings(write(tmp_path, MINIMAL))
    assert settings.defaults.timeout_ms == 10000
    assert settings.defaults.max_results == 10
    assert settings.server.request_deadline_ms == 30000
    assert settings.breaker.fault_threshold == 3
    assert settings.breaker.cooldown_ms == 60000
    assert settings.breaker.quota_cooldown_ms == 300000


def test_per_instance_timeout_overrides_the_default(tmp_path):
    settings = load_settings(write(tmp_path, MINIMAL))
    assert settings.providers[0].timeout_ms is None
    assert settings.providers[1].timeout_ms == 15000


def test_auth_token_may_be_absent(tmp_path):
    data = {**MINIMAL, "server": {}}
    assert load_settings(write(tmp_path, data)).server.auth_token is None


def test_rejects_unknown_provider_type(tmp_path):
    data = {**MINIMAL, "providers": [{"name": "x", "type": "altavista", "api_key": "k"}]}
    with pytest.raises(ConfigError, match="unknown provider type"):
        load_settings(write(tmp_path, data))


def test_rejects_duplicate_instance_names(tmp_path):
    data = {**MINIMAL, "providers": [
        {"name": "dup", "type": "tavily", "api_key": "a"},
        {"name": "dup", "type": "tavily", "api_key": "b"},
    ]}
    with pytest.raises(ConfigError, match="duplicate provider name"):
        load_settings(write(tmp_path, data))


def test_rejects_instance_name_colliding_with_a_type_name(tmp_path):
    """Otherwise a `tavily/...` scope prefix would be ambiguous."""
    data = {**MINIMAL, "providers": [{"name": "tavily", "type": "tavily", "api_key": "a"}]}
    with pytest.raises(ConfigError, match="collides with a provider type"):
        load_settings(write(tmp_path, data))


def test_rejects_missing_required_option(tmp_path):
    data = {**MINIMAL, "providers": [{"name": "t", "type": "tavily"}]}
    with pytest.raises(ConfigError, match="requires 'api_key'"):
        load_settings(write(tmp_path, data))


def test_rejects_empty_provider_list(tmp_path):
    with pytest.raises(ConfigError, match="at least one provider"):
        load_settings(write(tmp_path, {**MINIMAL, "providers": []}))


def test_missing_file_is_a_config_error(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_settings(tmp_path / "nope.yaml")


@pytest.mark.xfail(reason="cannot pass until every adapter is registered in Task 14")
def test_example_config_is_valid():
    """The tracked template must always load — it is the onboarding path.

    Marked xfail here and un-marked in Task 14: it is the running tracker for
    the seven adapters still to come, without leaving the suite red meanwhile.
    """
    settings = load_settings("config.example.yaml")
    assert len(settings.providers) == 9
    assert [p.name for p in settings.providers][:2] == ["tavily-a", "tavily-b"]
