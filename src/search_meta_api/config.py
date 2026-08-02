from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, Field, ValidationError

from search_meta_api.breaker import BreakerSettings
from search_meta_api.providers.registry import ADAPTERS, REQUIRED_OPTIONS


class ConfigError(Exception):
    pass


class ServerConfig(BaseModel):
    auth_token: str | None = None
    request_deadline_ms: int = 30_000


class DefaultsConfig(BaseModel):
    timeout_ms: int = 10_000
    max_results: int = 10


class BreakerConfig(BaseModel):
    fault_threshold: int = 3
    cooldown_ms: int = 60_000
    quota_cooldown_ms: int = 300_000

    def to_settings(self) -> BreakerSettings:
        return BreakerSettings(
            fault_threshold=self.fault_threshold,
            cooldown_s=self.cooldown_ms / 1000,
            quota_cooldown_s=self.quota_cooldown_ms / 1000,
        )


class ProviderConfig(BaseModel):
    name: str
    type: str
    api_key: str | None = None
    base_url: str | None = None
    cx: str | None = None
    timeout_ms: int | None = None


class Settings(BaseModel):
    server: ServerConfig = Field(default_factory=ServerConfig)
    defaults: DefaultsConfig = Field(default_factory=DefaultsConfig)
    breaker: BreakerConfig = Field(default_factory=BreakerConfig)
    providers: list[ProviderConfig]


def load_settings(path: str | Path) -> Settings:
    path = Path(path)
    if not path.is_file():
        raise ConfigError(f"config file not found: {path}")

    try:
        raw = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"config file is not valid YAML: {exc}") from exc

    try:
        settings = Settings(**raw)
    except ValidationError as exc:
        raise ConfigError(f"invalid config: {exc}") from exc

    _validate_providers(settings)
    return settings


def _validate_providers(settings: Settings) -> None:
    if not settings.providers:
        raise ConfigError("config must declare at least one provider")

    seen: set[str] = set()
    for provider in settings.providers:
        if provider.type not in ADAPTERS:
            raise ConfigError(
                f"unknown provider type {provider.type!r} for instance {provider.name!r}; "
                f"known types: {', '.join(sorted(ADAPTERS))}"
            )
        if provider.name in seen:
            raise ConfigError(f"duplicate provider name {provider.name!r}")
        if provider.name in ADAPTERS:
            raise ConfigError(
                f"provider name {provider.name!r} collides with a provider type name, "
                "which would make a params scope prefix ambiguous"
            )
        for option in REQUIRED_OPTIONS[provider.type]:
            if getattr(provider, option, None) is None:
                raise ConfigError(
                    f"provider {provider.name!r} of type {provider.type!r} "
                    f"requires {option!r}"
                )
        seen.add(provider.name)
