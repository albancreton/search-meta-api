from __future__ import annotations

import json
import secrets
from datetime import date
from typing import Annotated, Any

from fastapi import FastAPI
from mcp.server.mcpserver import MCPServer
from mcp.server.streamable_http_manager import StreamableHTTPASGIApp
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import Field
from starlette.routing import Route

from search_meta_api.models import (
    Freshness,
    SafeSearch,
    SearchRequest,
    SearchResponse,
    Topic,
)
from search_meta_api.service import AllProvidersFailed, describe_providers, run_search
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

LIST_PROVIDERS_DESCRIPTION = """\
List the configured search providers in failover order. Returns each \
instance's name, adapter type, circuit-breaker state, the unified filters it \
can honour, its result cap, and the vendor-specific passthrough params it \
accepts. Call this before using the `params` argument of `search`, and to \
understand why a search reported a filter as degraded.\
"""

PARAMS_DESCRIPTION = """\
Per-provider overrides, for when a specific vendor needs specific handling. \
Keys are `scope/name`, where scope is `*` (all providers), a provider type \
(e.g. `tavily`), or a configured instance name (e.g. `tavily-b`). Call \
list_providers first to learn which instances exist and which passthrough \
params each accepts. Example: {"*/freshness": "week", \
"tavily/search_depth": "advanced"}.\
"""


class _RequireBearer:
    """The service's bearer check, applied to a raw ASGI app.

    `require_auth` is a FastAPI dependency and cannot guard a route whose
    endpoint is a bare ASGI app, so the rule is reapplied here. Unlike the
    HTTP dependency it needs no encoding care: the ASGI scope carries header
    values as raw bytes, so a non-ASCII Authorization header compares as
    bytes and simply fails to match, which is the correct answer.

    Reads the token per request rather than at build time so it tracks the
    app's settings rather than a snapshot of them.
    """

    def __init__(self, mcp_app: Any, app: FastAPI) -> None:
        self._mcp_app = mcp_app
        self._app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        token = self._app.state.settings.server.auth_token
        if token is not None:
            # First-wins on a duplicated header, matching Starlette's
            # `headers.get` and therefore `require_auth` on the HTTP routes.
            # `dict(scope["headers"])` would be last-wins, which would let each
            # gate accept a request the other refuses.
            presented = next(
                (v for k, v in scope.get("headers") or [] if k == b"authorization"),
                b"",
            )
            if not secrets.compare_digest(presented, f"Bearer {token}".encode()):
                # A plain 401, not a JSON-RPC error: an unauthenticated caller
                # is not yet in a protocol conversation.
                await send({
                    "type": "http.response.start",
                    "status": 401,
                    "headers": [(b"content-type", b"text/plain; charset=utf-8")],
                })
                await send({
                    "type": "http.response.body",
                    "body": b"invalid or missing bearer token",
                })
                return
        await self._mcp_app(scope, receive, send)


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

    # The flat signature restates the bounds `UnifiedFilters` declares, because
    # the advertised JSON schema is built from this signature alone: without
    # them an agent can only learn the limits by making a call that fails.
    # `tests/test_mcp.py` pins these against `models.py` so the two cannot
    # drift apart silently.
    @mcp.tool(description=SEARCH_DESCRIPTION)
    async def search(
        query: Annotated[str, Field(min_length=1, max_length=2000)],
        max_results: Annotated[int | None, Field(ge=1, le=100)] = None,
        include_domains: list[str] | None = None,
        exclude_domains: list[str] | None = None,
        freshness: Freshness | None = None,
        start_date: date | None = None,
        end_date: date | None = None,
        country: Annotated[str | None, Field(min_length=2, max_length=2)] = None,
        language: Annotated[str | None, Field(min_length=2, max_length=2)] = None,
        safe_search: SafeSearch | None = None,
        topic: Topic | None = None,
        params: Annotated[dict[str, Any] | None, Field(description=PARAMS_DESCRIPTION)] = None,
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
        # Known and deliberate divergence from POST /search: an explicit JSON
        # `null` for an optional argument is treated here as if the argument
        # had been omitted, where HTTP answers 422. Telling the two apart would
        # mean threading `model_fields_set` out of the tool signature, and the
        # advertised schema already declares `default: null` for each of these,
        # so a caller sending null is asking for the default. Left as is on
        # purpose — please don't "fix" it without revisiting that trade-off.
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

    @mcp.tool(description=LIST_PROVIDERS_DESCRIPTION)
    async def list_providers() -> dict[str, Any]:
        return describe_providers(app.state)

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
            # requests; this service is reached over TLS and, *when
            # `server.auth_token` is set*, gated by a bearer token no browser
            # holds. That token is what replaces the dropped protection, and it
            # is optional — with no token configured the endpoint's only
            # defence is the network boundary in front of it, so a deployment
            # that leaves the token unset must not expose /mcp to a network
            # where a browser could reach it.
            enable_dns_rebinding_protection=False,
        ),
    )
    app.state.mcp_session_manager = mcp.session_manager

    # DELETE is the transport's session-termination verb. Stateless mode has no
    # session to terminate, but routing it lets the MCP layer answer with a
    # protocol-shaped error instead of FastAPI's bare 405.
    #
    # GET is deliberately NOT routed. It is the transport's server-to-client SSE
    # verb, and the SDK answers it by opening a stream — which, stateless, the
    # server never writes to and never closes, pinning a connection and a task
    # for as long as the client holds on. Since `auth_token` is optional, that
    # would be an unauthenticated connection-hold. Starlette's plain 405 is the
    # honest answer.
    return Route(
        MCP_PATH,
        endpoint=_RequireBearer(StreamableHTTPASGIApp(mcp.session_manager), app),
        methods=["POST", "DELETE"],
    )
