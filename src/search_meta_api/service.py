from __future__ import annotations

from typing import Any

from search_meta_api.models import SearchRequest, SearchResponse
from search_meta_api.providers.base import resolve_request
from search_meta_api.router import ProviderInstance, run_failover
from search_meta_api.validation import validate_params


class AllProvidersFailed(Exception):
    """Every provider errored — nobody even answered emptily.

    Carries the payload verbatim so both surfaces report the same body: the
    HTTP route nests it under `detail`, the MCP tool serialises it into the
    tool error. Neither gets to invent its own shape.
    """

    def __init__(self, payload: dict[str, Any]) -> None:
        super().__init__("all_providers_failed")
        self.payload = payload


async def run_search(state: Any, req: SearchRequest) -> SearchResponse:
    """Walk the configured provider chain and return the first positive result.

    `state` is the FastAPI app's `state`, so the MCP tool and the HTTP route
    read the same settings, provider instances, and breaker.

    `server.request_deadline_ms` is a **best-effort** budget: the router checks
    it before starting each provider attempt, but nothing interrupts an attempt
    already in flight, so a request can overshoot the deadline by up to one
    adapter's own timeout (10s by default).
    """
    validate_params(req, state.instances)

    # An omitted max_results takes the deployment's configured default; an
    # explicit one always wins. This must happen AFTER validate_params:
    # assigning to the model adds the field to model_fields_set, which would
    # make the applied default look like a caller-supplied value and falsely
    # conflict with a '*/max_results' override.
    if "max_results" not in req.model_fields_set:
        req.max_results = state.settings.defaults.max_results

    async def attempt(instance: ProviderInstance):
        adapter = instance.adapter
        effective, passthrough, degraded = resolve_request(
            req, instance.name, adapter.type_name, adapter.capabilities
        )
        degraded = sorted(set(degraded) | set(adapter.extra_degraded(effective)))
        return await adapter.search(effective, passthrough), degraded

    outcome = await run_failover(
        state.instances, attempt, state.breaker,
        deadline_ms=state.settings.server.request_deadline_ms,
    )

    if outcome.winner is None and not outcome.any_answer:
        raise AllProvidersFailed({
            "error": "all_providers_failed",
            "query": req.query,
            "attempts": [a.model_dump() for a in outcome.attempts],
            "elapsed_ms": outcome.elapsed_ms,
        })

    return SearchResponse(
        query=req.query,
        provider=outcome.winner,
        results=outcome.items,
        degraded=outcome.degraded,
        attempts=outcome.attempts,
        elapsed_ms=outcome.elapsed_ms,
        truncated=outcome.truncated,
    )


def describe_providers(state: Any) -> dict[str, Any]:
    """The configured chain, in failover order, with what each can honour."""
    return {
        "providers": [
            {
                "name": i.name,
                "type": i.adapter.type_name,
                "state": str(state.breaker.state(i.name)),
                "supported_filters": sorted(i.adapter.capabilities.supported_filters),
                "max_results_cap": i.adapter.capabilities.max_results_cap,
                "passthrough_params": sorted(i.adapter.capabilities.passthrough_params),
            }
            for i in state.instances
        ]
    }
