from __future__ import annotations

import logging
import os
import secrets
from collections.abc import Sequence
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Any

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request

from search_meta_api.breaker import CircuitBreaker
from search_meta_api.config import Settings, load_settings
from search_meta_api.mcp_server import build_mcp_route
from search_meta_api.models import SearchRequest, SearchResponse
from search_meta_api.providers.registry import build_adapter
from search_meta_api.router import ProviderInstance
from search_meta_api.service import AllProvidersFailed, describe_providers, run_search
from search_meta_api.validation import ParamError

logging.basicConfig(level=logging.INFO)


def require_auth(request: Request) -> None:
    token = request.app.state.settings.server.auth_token
    if token is None:
        return
    # Compare as bytes, not str: secrets.compare_digest raises TypeError on a
    # non-ASCII str, and an HTTP header is latin-1 on the wire, so
    # `Authorization: Bearer sécret` from any unauthenticated caller would
    # otherwise be a 500. The token itself is ASCII-only by config validation,
    # so a non-ASCII header simply cannot match — which is the correct answer.
    presented = request.headers.get("Authorization", "").encode("utf-8", "surrogateescape")
    if not secrets.compare_digest(presented, f"Bearer {token}".encode()):
        raise HTTPException(status_code=401, detail="invalid or missing bearer token")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings: Settings = app.state.settings
    async with AsyncExitStack() as stack:
        if app.state.instances is None:
            client = await stack.enter_async_context(httpx.AsyncClient())
            app.state.instances = [
                ProviderInstance(
                    cfg.name,
                    build_adapter(cfg, client, settings.defaults.timeout_ms),
                )
                for cfg in settings.providers
            ]
        # The MCP session manager needs a running task group before it can
        # serve a request, and FastAPI does not propagate lifespan into
        # mounted apps. Without this, every /mcp call fails with
        # "Task group is not initialized".
        await stack.enter_async_context(app.state.mcp_session_manager.run())
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
        """Walk the configured provider chain and return the first positive result."""
        try:
            return await run_search(request.app.state, req)
        except ParamError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except AllProvidersFailed as exc:
            raise HTTPException(status_code=503, detail=exc.payload) from exc

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
        return describe_providers(request.app.state)

    app.router.routes.append(build_mcp_route(app))
    return app


def build() -> FastAPI:
    """Entry point for uvicorn: `uvicorn search_meta_api.app:build --factory`."""
    return create_app(load_settings(os.environ.get("CONFIG_PATH", "config.yaml")))
