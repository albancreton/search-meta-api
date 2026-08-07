from __future__ import annotations

import json
from datetime import date
from typing import Any

from fastapi import FastAPI
from mcp.server.mcpserver import MCPServer
from mcp.server.streamable_http_manager import StreamableHTTPASGIApp
from mcp.server.transport_security import TransportSecuritySettings
from starlette.routing import Route

from search_meta_api.models import (
    Freshness,
    SafeSearch,
    SearchRequest,
    SearchResponse,
    Topic,
)
from search_meta_api.service import AllProvidersFailed, run_search
from search_meta_api.validation import ParamError

MCP_PATH = "/mcp"

SEARCH_DESCRIPTION = """\
Search the web. Walks an ordered chain of third-party search providers and \
returns the first non-empty result set.

Read three fields before trusting `results`:

- `provider` is the instance that answered, or null when every provider was \
asked and none had results.
- `degraded` lists filters the winning provider could not honour. They were \
dropped, not applied — results may be broader than requested.
- `truncated` is true when the chain stopped early because the request \
deadline fired, so providers further down were never asked. An empty \
`results` with `truncated` true does NOT mean the query has no answers.

An empty result set is a legitimate answer, not a failure.\
"""

PARAMS_DESCRIPTION = """\
Per-provider overrides, for when a specific vendor needs specific handling. \
Keys are `scope/name`, where scope is `*` (all providers), a provider type \
(e.g. `tavily`), or a configured instance name (e.g. `tavily-b`). Call \
list_providers first to learn which instances exist and which passthrough \
params each accepts. Example: {"*/freshness": "week", \
"tavily/search_depth": "advanced"}.\
"""


def build_mcp_route(app: FastAPI) -> Route:
    """Build the MCP server and return the route that serves it.

    Takes the FastAPI app so the tools close over its `state` — the same
    settings, provider instances, and breaker the HTTP routes read.
    """
    mcp = MCPServer(
        name="search-meta-api",
        version="0.1.0",
        instructions=(
            "Web search across a failover chain of third-party search "
            "providers. Call list_providers to see which providers are "
            "configured and what each can honour."
        ),
    )

    @mcp.tool(description=SEARCH_DESCRIPTION)
    async def search(
        query: str,
        max_results: int | None = None,
        include_domains: list[str] | None = None,
        exclude_domains: list[str] | None = None,
        freshness: Freshness | None = None,
        start_date: date | None = None,
        end_date: date | None = None,
        country: str | None = None,
        language: str | None = None,
        safe_search: SafeSearch | None = None,
        topic: Topic | None = None,
        params: dict[str, Any] | None = None,
    ) -> SearchResponse:
        # Omitted arguments must stay out of model_fields_set: an argument that
        # merely defaulted to None here would otherwise look caller-supplied
        # and suppress the deployment's configured default (max_results) or
        # falsely conflict with a scoped override.
        fields: dict[str, Any] = {"query": query}
        optional = (
            ("max_results", max_results),
            ("include_domains", include_domains),
            ("exclude_domains", exclude_domains),
            ("freshness", freshness),
            ("start_date", start_date),
            ("end_date", end_date),
            ("country", country),
            ("language", language),
            ("safe_search", safe_search),
            ("topic", topic),
            ("params", params),
        )
        for name, value in optional:
            if value is not None:
                fields[name] = value

        req = SearchRequest(**fields)
        try:
            return await run_search(app.state, req)
        except ParamError as exc:
            raise ValueError(str(exc)) from exc
        except AllProvidersFailed as exc:
            # The tool-error channel is text, so the payload is serialised
            # rather than dropped. The agent needs the attempts to tell a
            # quota-exhausted chain from a misconfigured one.
            raise ValueError(f"all_providers_failed: {json.dumps(exc.payload)}") from exc

    # Constructs the session manager as a side effect; the returned Starlette
    # app is deliberately discarded. Mounting it would put the endpoint at
    # `/mcp/` and answer `/mcp` with a 307 — and the MCP SDK's own client never
    # sets follow_redirects, so httpx's default of False applies and a client
    # configured with the natural URL fails outright.
    mcp.streamable_http_app(
        streamable_http_path=MCP_PATH,
        json_response=True,
        stateless_http=True,
        # Every search is independent, so there is no session worth keeping and
        # nothing breaks if the container restarts mid-conversation.
        transport_security=TransportSecuritySettings(
            # The SDK's default allowlist is localhost-only, which rejects every
            # request arriving through Caddy as `search.local.alban.me`. The
            # check guards a localhost dev server against browser-forged
            # requests; this service is reached over TLS and gated by a bearer
            # token no browser holds.
            enable_dns_rebinding_protection=False,
        ),
    )
    app.state.mcp_session_manager = mcp.session_manager

    # GET and DELETE are the transport's SSE-stream and session-termination
    # verbs. Stateless mode serves neither, but routing them lets the MCP layer
    # answer with a protocol-shaped error instead of FastAPI's bare 405.
    return Route(
        MCP_PATH,
        endpoint=StreamableHTTPASGIApp(mcp.session_manager),
        methods=["POST", "GET", "DELETE"],
    )
