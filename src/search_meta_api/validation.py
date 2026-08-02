from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from pydantic import ValidationError

from search_meta_api.models import UNIFIED_FIELDS, SearchRequest, UnifiedFilters
from search_meta_api.providers.base import UNIVERSAL_SCOPE, resolve_request
from search_meta_api.router import ProviderInstance


class ParamError(ValueError):
    """A namespaced parameter the service can reject before calling anyone."""


def _is_bool_for_int(value: Any, expected: type | None) -> bool:
    """Whether `value` is a bool being offered where an int is required.

    `isinstance(True, int)` is True and pydantic's lax mode coerces True to 1,
    so without this guard `{"*/max_results": true}` is silently accepted and
    becomes `max_results=1`, and `{"brave/offset": true}` reaches the vendor as
    a bool. A bool is never what a caller meant by an integer.
    """
    return expected is int and isinstance(value, bool)


def _check_unified_value(key: str, name: str, value: Any) -> None:
    """Validate one unified-field value on its own, before anything consumes it.

    A `params` entry naming a unified field is not passthrough — it overrides
    that field for the matching providers, and its value is fed straight into
    `resolve_request`. Checking it here means a bad value is a 422 naming the
    exact param, rather than an opaque failure (or, before this existed, a
    TypeError-turned-500) raised deep inside per-provider resolution.
    """
    if _is_bool_for_int(value, UnifiedFilters.model_fields[name].annotation):
        raise ParamError(f"param {key!r} must be int, not bool")
    try:
        UnifiedFilters(**{"query": "probe", name: value})
    except ValidationError as exc:
        raise ParamError(
            f"invalid value for param {key!r}: {exc.errors()[0]['msg']}"
        ) from exc


def _flat_is_set(req: SearchRequest, name: str) -> bool:
    """Whether the caller actually supplied `name` as a top-level field.

    Must check `model_fields_set`, not compare against the field's default:
    a value equal to the default (e.g. an explicit `max_results=10`) is still
    a genuine conflict with a `*/`-scoped twin, and comparing against the
    default would also falsely flag a value the *caller never set* if it was
    assigned onto the model after construction (e.g. applying a configured
    default) — assignment adds the field to `model_fields_set` too, so callers
    of `validate_params` must run it before applying any such default.
    """
    return name in req.model_fields_set


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
            _check_unified_value(key, name, value)
            continue

        adapter = by_scope.get(scope)
        if adapter is None:
            raise ParamError(
                f"unknown scope {scope!r} in param {key!r}; "
                f"known scopes: *, {', '.join(sorted(by_scope))}"
            )
        if name in UNIFIED_FIELDS:
            _check_unified_value(key, name, value)
            continue

        declared = adapter.capabilities.passthrough_params.get(name)
        if declared is None:
            raise ParamError(f"{scope!r} has no parameter {name!r}")
        if declared is not object:
            if _is_bool_for_int(value, declared):
                raise ParamError(f"param {key!r} must be int, not bool")
            if not isinstance(value, declared):
                raise ParamError(f"param {key!r} must be {declared.__name__}")

    # Dry-run resolution so a bad value surfaces as a 422 rather than as an
    # adapter failure repeated across every provider in the chain.
    for instance in instances:
        try:
            resolve_request(req, instance.name, instance.adapter.type_name,
                            instance.adapter.capabilities)
        except ValidationError as exc:
            raise ParamError(f"invalid value for {instance.name}: {exc}") from exc
