from __future__ import annotations

from typing import Sequence

from pydantic import ValidationError

from search_meta_api.models import UNIFIED_FIELDS, SearchRequest, UnifiedFilters
from search_meta_api.providers.base import UNIVERSAL_SCOPE, resolve_request
from search_meta_api.router import ProviderInstance


class ParamError(ValueError):
    """A namespaced parameter the service can reject before calling anyone."""


def _flat_is_set(req: SearchRequest, name: str) -> bool:
    default = UnifiedFilters.model_fields[name].get_default(call_default_factory=True)
    return getattr(req, name) != default


def validate_params(req: SearchRequest, instances: Sequence[ProviderInstance]) -> None:
    by_scope = {}
    for instance in instances:
        by_scope[instance.name] = instance.adapter
        by_scope.setdefault(instance.adapter.type_name, instance.adapter)

    for key, value in req.params.items():
        if "/" not in key:
            raise ParamError(
                f"param {key!r} must be scoped as '<scope>/<name>', "
                f"e.g. '*/country' or 'tavily/search_depth'"
            )
        scope, name = key.split("/", 1)

        if scope == UNIVERSAL_SCOPE:
            if name not in UNIFIED_FIELDS:
                raise ParamError(
                    f"'*/{name}' is not a unified field; vendor-specific parameters "
                    f"must be scoped to a provider type or instance"
                )
            if _flat_is_set(req, name):
                raise ParamError(
                    f"{name!r} was given both as a top-level field and as '*/{name}'"
                )
            continue

        adapter = by_scope.get(scope)
        if adapter is None:
            raise ParamError(
                f"unknown scope {scope!r} in param {key!r}; "
                f"known scopes: *, {', '.join(sorted(by_scope))}"
            )
        if name in UNIFIED_FIELDS:
            continue

        declared = adapter.capabilities.passthrough_params.get(name)
        if declared is None:
            raise ParamError(f"{scope!r} has no parameter {name!r}")
        if declared is not object and not isinstance(value, declared):
            raise ParamError(f"param {key!r} must be {declared.__name__}")

    # Dry-run resolution so a bad value surfaces as a 422 rather than as an
    # adapter failure repeated across every provider in the chain.
    for instance in instances:
        try:
            resolve_request(req, instance.name, instance.adapter.type_name,
                            instance.adapter.capabilities)
        except ValidationError as exc:
            raise ParamError(f"invalid value for {instance.name}: {exc}") from exc
