from __future__ import annotations

import logging
import os
import secrets
from collections.abc import Sequence
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request

from search_meta_api.breaker import CircuitBreaker
from search_meta_api.config import Settings, load_settings
from search_meta_api.models import SearchRequest, SearchResponse
from search_meta_api.providers.base import resolve_request
from search_meta_api.providers.registry import build_adapter
from search_meta_api.router import ProviderInstance, run_failover
from search_meta_api.validation import ParamError, validate_params

logging.basicConfig(level=logging.INFO)


def require_auth(request: Request) -> None:
    token = request.app.state.settings.server.auth_token
    if token is None:
        return
    presented = request.headers.get("Authorization", "")
    if not secrets.compare_digest(presented, f"Bearer {token}"):
        raise HTTPException(status_code=401, detail="invalid or missing bearer token")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings: Settings = app.state.settings
    if app.state.instances is None:
        async with httpx.AsyncClient() as client:
            app.state.instances = [
                ProviderInstance(
                    cfg.name,
                    build_adapter(cfg, client, settings.defaults.timeout_ms),
                )
                for cfg in settings.providers
            ]
            yield
    else:
        yield


def create_app(
    settings: Settings, instances: Sequence[ProviderInstance] | None = None
) -> FastAPI:
    app = FastAPI(title="search-meta-api", version="0.1.0", lifespan=lifespan)
    app.state.settings = settings
    app.state.instances = list(instances) if instances is not None else None
    app.state.breaker = CircuitBreaker(settings.breaker.to_settings())

    @app.post("/search", response_model=SearchResponse,
              dependencies=[Depends(require_auth)])
    async def search(req: SearchRequest, request: Request) -> SearchResponse:
        """Walk the configured provider chain and return the first positive result.

        `server.request_deadline_ms` is a **best-effort** budget: the router
        checks it before starting each provider attempt, but nothing interrupts
        an attempt already in flight, so a request can overshoot the deadline
        by up to one adapter's own timeout (10s by default).
        """
        state = request.app.state
        try:
            validate_params(req, state.instances)
        except ParamError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        # An omitted max_results takes the deployment's configured default; an
        # explicit one always wins. This must happen AFTER validate_params:
        # assigning to the model adds the field to model_fields_set, which
        # would make the applied default look like a caller-supplied value
        # and falsely conflict with a '*/max_results' override.
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
            raise HTTPException(status_code=503, detail={
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
        )

    @app.get("/health")
    async def health(request: Request) -> dict[str, Any]:
        state = request.app.state
        return {
            "status": "ok",
            "providers": [
                {"name": i.name, "state": str(state.breaker.state(i.name))}
                for i in state.instances
            ],
        }

    @app.get("/providers", dependencies=[Depends(require_auth)])
    async def providers(request: Request) -> dict[str, Any]:
        state = request.app.state
        return {
            "providers": [
                {
                    "name": i.name,
                    "type": i.adapter.type_name,
                    "state": str(state.breaker.state(i.name)),
                    "supported_filters": sorted(i.adapter.capabilities.supported_filters),
                    "max_results_cap": i.adapter.capabilities.max_results_cap,
                    "passthrough_params": sorted(
                        i.adapter.capabilities.passthrough_params),
                }
                for i in state.instances
            ]
        }

    return app


def build() -> FastAPI:
    """Entry point for uvicorn: `uvicorn search_meta_api.app:build --factory`."""
    return create_app(load_settings(os.environ.get("CONFIG_PATH", "config.yaml")))
