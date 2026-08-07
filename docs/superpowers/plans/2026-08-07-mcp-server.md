# MCP Server Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Expose the existing meta-search service over the Model Context Protocol at `POST /mcp`, in the same process and container, behind the same bearer token.

**Architecture:** A new `service.py` holds the search and provider-listing logic that both surfaces call, so the HTTP routes and the MCP tools cannot drift. `mcp_server.py` builds an `MCPServer`, registers two tools that call `service.py`, and returns a Starlette route that `app.py` appends to its own router. The MCP session manager is entered by `app.py`'s existing lifespan.

**Tech Stack:** Python 3.12+, FastAPI, pydantic v2, `mcp>=2.0` (Python MCP SDK), pytest + pytest-asyncio.

**Spec:** [`docs/superpowers/specs/2026-08-07-mcp-server-design.md`](../specs/2026-08-07-mcp-server-design.md)

## Global Constraints

- **Baseline:** the suite is green at 292 passing tests before Task 1. Run with `.venv/bin/pytest -q`. No task may reduce that count.
- **Dependency:** `mcp>=2.0` — the 2.0 layout is required. The server class is `mcp.server.mcpserver.MCPServer`; the pre-2.0 `mcp.server.fastmcp.FastMCP` does not exist in 2.0.
- **`mcp` 2.0 pulls in `httpx2`**, which coexists with the project's `httpx`. Application code keeps using `httpx`. Only `tests/test_mcp.py` imports `httpx2`, because the MCP client transport requires an `httpx2.AsyncClient`.
- **MCP SDK model attributes are snake_case in 2.0:** `result.is_error`, `result.structured_content`, `tool.output_schema`. The camelCase spellings from 1.x raise `AttributeError`.
- **Endpoint path is exactly `/mcp`** — no trailing slash, no redirect. See Task 2 for why.
- **No new config keys.** Auth reuses `server.auth_token`.
- **Line length 100** (`[tool.ruff] line-length = 100`). Run `.venv/bin/ruff check src tests` before each commit.
- **Comments explain why, not what** — match the density and voice of the existing modules.

---

### Task 1: Extract the shared service layer

Behaviour-preserving refactor. The `/search` and `/providers` handler bodies move into functions that the MCP tools will also call in Task 2. The existing 292 tests are the regression net; a new unit test covers the extracted seam directly.

**Files:**
- Create: `src/search_meta_api/service.py`
- Create: `tests/test_service.py`
- Modify: `src/search_meta_api/app.py` (the `search` and `providers` route bodies)

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces:
  - `service.run_search(state: Any, req: SearchRequest) -> SearchResponse` — raises `validation.ParamError` for a bad `params` entry and `service.AllProvidersFailed` when every provider errored. `state` is a Starlette `State` (`app.state`) carrying `.settings`, `.instances`, `.breaker`.
  - `service.describe_providers(state: Any) -> dict[str, Any]`
  - `service.AllProvidersFailed(Exception)` with a `.payload: dict[str, Any]` attribute.

- [ ] **Step 1: Write the failing test**

Create `tests/test_service.py`:

```python
import pytest

from search_meta_api.app import create_app
from search_meta_api.config import Settings
from search_meta_api.errors import ErrorClass
from search_meta_api.models import SearchRequest
from search_meta_api.service import AllProvidersFailed, describe_providers, run_search
from test_app import SETTINGS_NO_AUTH, fail, hit


def state_for(*instances):
    """An app's state without going through the HTTP layer.

    run_search takes `app.state` rather than a bespoke context object so the
    two surfaces read the same settings, instances, and breaker.
    """
    return create_app(SETTINGS_NO_AUTH, instances=list(instances)).state


async def test_run_search_returns_the_first_positive_answer():
    response = await run_search(
        state_for(fail("a", 500), hit("b", [("Cats", "https://a.com")])),
        SearchRequest(query="cats"),
    )
    assert response.provider == "b"
    assert response.results[0].title == "Cats"
    assert [a.outcome for a in response.attempts] == ["error", "success"]


async def test_run_search_applies_the_configured_max_results_default():
    """The default must not land in model_fields_set — an applied default that
    looks caller-supplied would falsely conflict with a '*/max_results'
    override. Asserting via that conflict is the only way to see the
    difference from outside.
    """
    settings = Settings(defaults={"max_results": 3},
                        providers=[{"name": "a", "type": "tavily", "api_key": "k"}])
    state = create_app(settings, instances=[hit("a", [])]).state
    req = SearchRequest(query="cats", params={"*/max_results": 5})
    await run_search(state, req)  # must not raise a conflict
    assert req.max_results == 5


async def test_run_search_raises_all_providers_failed_with_the_attempts():
    with pytest.raises(AllProvidersFailed) as caught:
        await run_search(
            state_for(fail("a", 500), fail("b", 429, ErrorClass.QUOTA)),
            SearchRequest(query="cats"),
        )
    payload = caught.value.payload
    assert payload["error"] == "all_providers_failed"
    assert payload["query"] == "cats"
    assert [a["provider"] for a in payload["attempts"]] == ["a", "b"]
    assert payload["elapsed_ms"] >= 0


async def test_run_search_returns_a_winnerless_success_when_everyone_is_empty():
    response = await run_search(state_for(hit("a", []), hit("b", [])),
                                SearchRequest(query="cats"))
    assert response.provider is None
    assert response.results == []


async def test_describe_providers_reports_capabilities_and_breaker_state():
    payload = describe_providers(state_for(hit("a", [])))
    entry = payload["providers"][0]
    assert entry["name"] == "a"
    assert entry["type"] == "tavily"
    assert entry["supported_filters"] == ["country"]
    assert entry["max_results_cap"] == 20
    assert entry["passthrough_params"] == ["chunks_per_source", "search_depth"]
    assert entry["state"]
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/pytest tests/test_service.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'search_meta_api.service'`

- [ ] **Step 3: Write the service module**

Create `src/search_meta_api/service.py`:

```python
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
```

- [ ] **Step 4: Rewire the HTTP routes to the service**

In `src/search_meta_api/app.py`, replace the `search` and `providers` route bodies. Update the imports at the top: drop `from search_meta_api.providers.base import resolve_request`, drop `run_failover` from the `router` import (keep `ProviderInstance`), drop `SearchRequest`/`SearchResponse` only if unused (both are still used), and add:

```python
from search_meta_api.service import AllProvidersFailed, describe_providers, run_search
```

Replace the `search` route with:

```python
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
```

Replace the `providers` route with:

```python
    @app.get("/providers", dependencies=[Depends(require_auth)])
    async def providers(request: Request) -> dict[str, Any]:
        return describe_providers(request.app.state)
```

Leave `/health` alone — it reports breaker state only and has no service-layer counterpart.

- [ ] **Step 5: Run the new tests and the full suite**

Run: `.venv/bin/pytest -q`
Expected: PASS — 292 existing + 5 new = 297 passed. If any existing test in `tests/test_app.py` fails, the refactor changed behaviour; fix the refactor, not the test.

- [ ] **Step 6: Lint**

Run: `.venv/bin/ruff check src tests`
Expected: no findings. Remove any import left unused by the extraction.

- [ ] **Step 7: Commit**

```bash
git add src/search_meta_api/service.py src/search_meta_api/app.py tests/test_service.py
git commit -m "refactor: extract search and provider listing into a service layer

The MCP tools need the same logic the HTTP routes run. Sharing one
implementation is what keeps the two surfaces from drifting - in
particular the max_results default, which must be applied after
validate_params or it falsely conflicts with a */max_results override."
```

---

### Task 2: The MCP endpoint and the `search` tool

Adds the dependency, the server module, the route, and the lifespan wiring — the first working MCP endpoint. Unauthenticated for now; Task 3 adds the bearer check.

**Files:**
- Modify: `pyproject.toml` (dependencies)
- Create: `src/search_meta_api/mcp_server.py`
- Create: `tests/test_mcp.py`
- Modify: `src/search_meta_api/app.py` (`lifespan`, `create_app`)

**Interfaces:**
- Consumes: `service.run_search`, `service.AllProvidersFailed`, `validation.ParamError` from Task 1.
- Produces:
  - `mcp_server.build_mcp_route(app: FastAPI) -> starlette.routing.Route` — constructs the `MCPServer`, registers the tools, sets `app.state.mcp_session_manager`, and returns the route to append.
  - `app.state.mcp_session_manager: StreamableHTTPSessionManager` — an async context manager that must be entered before any MCP request is served.

- [ ] **Step 1: Add the dependency**

In `pyproject.toml`, add `"mcp>=2.0",` to the `dependencies` list (after `"pycountry>=24.6",`).

Run: `.venv/bin/pip install -e .`
Then verify the 2.0 layout: `.venv/bin/python -c "from mcp.server.mcpserver import MCPServer; print('ok')"`
Expected: `ok`. If this raises `ModuleNotFoundError`, an older `mcp` resolved — check the installed version with `.venv/bin/pip show mcp`.

- [ ] **Step 2: Write the failing test**

Create `tests/test_mcp.py`:

```python
import contextlib
import json

import httpx2
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from search_meta_api.app import create_app
from search_meta_api.config import Settings
from search_meta_api.errors import ErrorClass
from test_app import SETTINGS_NO_AUTH, fail, hit


@contextlib.asynccontextmanager
async def mcp_session(app, token=None):
    """An initialised MCP client session speaking to `app` in-process.

    The session manager is entered here rather than by the app's lifespan
    because httpx's ASGI transport never sends lifespan events - the same
    reason tests/test_app.py passes provider instances in directly.
    """
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    async with app.state.mcp_session_manager.run():
        http = httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app),
                                  base_url="http://test", headers=headers)
        async with http, streamable_http_client("http://test/mcp",
                                                http_client=http) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session


async def test_tools_list_advertises_search_with_the_flat_schema():
    app = create_app(SETTINGS_NO_AUTH, instances=[hit("a", [])])
    async with mcp_session(app) as session:
        tools = {t.name: t for t in (await session.list_tools()).tools}
        assert "search" in tools
        properties = tools["search"].input_schema["properties"]
        assert {"query", "max_results", "freshness", "country", "params"} <= set(properties)
        assert tools["search"].input_schema["required"] == ["query"]
        assert tools["search"].description


async def test_search_returns_the_same_payload_as_the_http_route():
    """The anti-drift test. One response contract, reached two ways."""
    from test_app import call

    instances = [fail("a", 500), hit("b", [("Cats", "https://a.com")])]
    over_http = await call(create_app(SETTINGS_NO_AUTH, instances=instances),
                           json={"query": "cats"})
    app = create_app(SETTINGS_NO_AUTH, instances=instances)
    async with mcp_session(app) as session:
        result = await session.call_tool("search", {"query": "cats"})

    assert result.is_error is False
    over_mcp = result.structured_content
    assert over_mcp is not None
    for field in ("query", "provider", "results", "degraded", "truncated"):
        assert over_mcp[field] == over_http.json()[field]
    assert [a["outcome"] for a in over_mcp["attempts"]] == ["error", "success"]


async def test_an_empty_answer_everywhere_is_a_success_not_an_error():
    """An empty result set is a legitimate answer. Reporting it as a tool
    error would tell the agent the search broke, and it would retry or give
    up instead of reading `truncated`.
    """
    app = create_app(SETTINGS_NO_AUTH, instances=[hit("a", []), hit("b", [])])
    async with mcp_session(app) as session:
        result = await session.call_tool("search", {"query": "cats"})
    assert result.is_error is False
    assert result.structured_content["results"] == []
    assert result.structured_content["provider"] is None
    assert result.structured_content["truncated"] is False


async def test_a_bad_param_is_a_tool_error_naming_the_param():
    app = create_app(SETTINGS_NO_AUTH, instances=[hit("a", [])])
    async with mcp_session(app) as session:
        result = await session.call_tool(
            "search", {"query": "cats", "params": {"nonesuch/depth": "advanced"}})
    assert result.is_error is True
    assert "nonesuch" in result.content[0].text


async def test_all_providers_failed_is_a_tool_error_carrying_the_attempts():
    """The agent needs to see which providers were tried and why each failed,
    the same way an HTTP caller reads the 503 body.
    """
    app = create_app(SETTINGS_NO_AUTH,
                     instances=[fail("a", 500), fail("b", 429, ErrorClass.QUOTA)])
    async with mcp_session(app) as session:
        result = await session.call_tool("search", {"query": "cats"})
    assert result.is_error is True
    _, _, payload = result.content[0].text.partition("all_providers_failed: ")
    body = json.loads(payload)
    assert body["error"] == "all_providers_failed"
    assert [a["provider"] for a in body["attempts"]] == ["a", "b"]
    assert body["attempts"][1]["error_class"] == "QUOTA"


async def test_the_configured_max_results_default_applies_over_mcp_too():
    settings = Settings(defaults={"max_results": 3},
                        providers=[{"name": "a", "type": "tavily", "api_key": "k"}])
    app = create_app(settings, instances=[hit("a", [])])
    async with mcp_session(app) as session:
        result = await session.call_tool(
            "search", {"query": "cats", "params": {"*/max_results": 5}})
    assert result.is_error is False
```

- [ ] **Step 3: Run the test to verify it fails**

Run: `.venv/bin/pytest tests/test_mcp.py -x -q`
Expected: FAIL — `AttributeError: 'State' object has no attribute 'mcp_session_manager'`

- [ ] **Step 4: Write the MCP server module**

Create `src/search_meta_api/mcp_server.py`:

```python
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
```

- [ ] **Step 5: Wire the route and the lifespan into the app**

In `src/search_meta_api/app.py`, add to the imports:

```python
from contextlib import AsyncExitStack, asynccontextmanager

from search_meta_api.mcp_server import build_mcp_route
```

(`asynccontextmanager` is already imported — extend that line rather than duplicating it.)

Replace the whole `lifespan` function with:

```python
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
```

At the end of `create_app`, immediately before `return app`:

```python
    app.router.routes.append(build_mcp_route(app))
```

- [ ] **Step 6: Run the tests**

Run: `.venv/bin/pytest tests/test_mcp.py -v`
Expected: PASS — 6 tests.

Then the full suite: `.venv/bin/pytest -q`
Expected: 303 passed.

- [ ] **Step 7: Lint and commit**

```bash
.venv/bin/ruff check src tests
git add pyproject.toml src/search_meta_api/mcp_server.py src/search_meta_api/app.py tests/test_mcp.py
git commit -m "feat: serve the search chain over MCP at POST /mcp

Same process, same container, same router - the tool calls run_search
in-process. Mounted as a plain route rather than a sub-app because
mounting puts the endpoint at /mcp/ and 307s /mcp, which the MCP SDK's
own client will not follow."
```

---

### Task 3: Bearer auth on the MCP endpoint

**Files:**
- Modify: `src/search_meta_api/mcp_server.py` (add the wrapper, wrap the endpoint)
- Modify: `tests/test_mcp.py` (add the auth tests)

**Interfaces:**
- Consumes: `build_mcp_route(app)` and the `mcp_session(app, token=None)` test helper from Task 2.
- Produces: no new public names. `build_mcp_route` keeps its signature; the returned route's endpoint is now wrapped.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_mcp.py`:

```python
from mcp.shared.exceptions import MCPError

from search_meta_api.config import Settings as _Settings

SETTINGS_AUTH = _Settings(server={"auth_token": "s3cret"},
                          providers=[{"name": "a", "type": "tavily", "api_key": "k"}])


async def test_the_configured_token_is_accepted():
    app = create_app(SETTINGS_AUTH, instances=[hit("a", [("Cats", "https://a.com")])])
    async with mcp_session(app, token="s3cret") as session:
        result = await session.call_tool("search", {"query": "cats"})
    assert result.is_error is False
    assert result.structured_content["provider"] == "a"


async def test_a_wrong_token_is_rejected_before_the_protocol_starts():
    app = create_app(SETTINGS_AUTH, instances=[hit("a", [])])
    with pytest.raises((MCPError, ExceptionGroup, Exception)):
        async with mcp_session(app, token="wrong"):
            pass


async def test_a_missing_token_is_rejected():
    app = create_app(SETTINGS_AUTH, instances=[hit("a", [])])
    with pytest.raises((MCPError, ExceptionGroup, Exception)):
        async with mcp_session(app):
            pass


async def test_a_non_ascii_token_is_rejected_rather_than_crashing():
    """A latin-1 header full of non-ASCII bytes must be a clean 401. The HTTP
    surface needed care here because compare_digest raises TypeError on a
    non-ASCII str; the ASGI scope carries raw bytes, so the comparison is
    safe by construction. This test is what keeps it that way.
    """
    app = create_app(SETTINGS_AUTH, instances=[hit("a", [])])
    response = await _raw_mcp_post(app, {"Authorization": "Bearer sécret"})
    assert response.status_code == 401


async def test_the_endpoint_is_open_when_no_token_is_configured():
    app = create_app(SETTINGS_NO_AUTH, instances=[hit("a", [])])
    async with mcp_session(app) as session:
        assert (await session.list_tools()).tools


async def _raw_mcp_post(app, headers):
    """POST to /mcp below the protocol layer, to observe the HTTP status."""
    async with app.state.mcp_session_manager.run():
        transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(transport=transport,
                                      base_url="http://test") as client:
            return await client.post(
                "/mcp",
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                headers={"Accept": "application/json, text/event-stream", **headers},
            )
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_mcp.py -k "token" -v`
Expected: FAIL — the wrong-token and missing-token tests fail because the call succeeds, and the non-ASCII test fails with `assert 200 == 401`.

- [ ] **Step 3: Add the auth wrapper**

In `src/search_meta_api/mcp_server.py`, add `import secrets` to the imports and this class above `build_mcp_route`:

```python
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
            headers = dict(scope.get("headers") or [])
            presented = headers.get(b"authorization", b"")
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
```

Then change the returned route's endpoint:

```python
    return Route(
        MCP_PATH,
        endpoint=_RequireBearer(StreamableHTTPASGIApp(mcp.session_manager), app),
        methods=["POST", "GET", "DELETE"],
    )
```

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/pytest tests/test_mcp.py -v`
Expected: PASS — 11 tests.

Then the full suite: `.venv/bin/pytest -q`
Expected: 308 passed.

- [ ] **Step 5: Lint and commit**

```bash
.venv/bin/ruff check src tests
git add src/search_meta_api/mcp_server.py tests/test_mcp.py
git commit -m "feat: gate /mcp behind the same bearer token as /search

One credential, no new config. The ASGI scope carries header values as
raw bytes, so the non-ASCII case that needs explicit handling in the
HTTP dependency is safe here by construction - tested so it stays that way."
```

---

### Task 4: The `list_providers` tool

**Files:**
- Modify: `src/search_meta_api/mcp_server.py` (register the second tool)
- Modify: `tests/test_mcp.py` (add the tests)

**Interfaces:**
- Consumes: `service.describe_providers` from Task 1, `build_mcp_route` from Task 2.
- Produces: an MCP tool named `list_providers` taking no arguments and returning `dict[str, Any]` shaped `{"providers": [...]}`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_mcp.py`:

```python
async def test_list_providers_reports_the_chain_in_failover_order():
    """An agent cannot write a scoped param like `tavily/search_depth` without
    first discovering that a tavily-typed instance exists and accepts it.
    """
    app = create_app(SETTINGS_NO_AUTH, instances=[hit("a", []), hit("b", [])])
    async with mcp_session(app) as session:
        result = await session.call_tool("list_providers", {})
    assert result.is_error is False
    providers = result.structured_content["providers"]
    assert [p["name"] for p in providers] == ["a", "b"]
    assert providers[0]["type"] == "tavily"
    assert providers[0]["supported_filters"] == ["country"]
    assert providers[0]["max_results_cap"] == 20
    assert providers[0]["passthrough_params"] == ["chunks_per_source", "search_depth"]
    assert providers[0]["state"]


async def test_list_providers_is_advertised_with_no_required_arguments():
    app = create_app(SETTINGS_NO_AUTH, instances=[hit("a", [])])
    async with mcp_session(app) as session:
        tools = {t.name: t for t in (await session.list_tools()).tools}
    assert set(tools) == {"search", "list_providers"}
    assert not tools["list_providers"].input_schema.get("required")
    assert tools["list_providers"].description


async def test_list_providers_needs_the_token_too():
    app = create_app(SETTINGS_AUTH, instances=[hit("a", [])])
    async with mcp_session(app, token="s3cret") as session:
        result = await session.call_tool("list_providers", {})
    assert result.is_error is False
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_mcp.py -k list_providers -v`
Expected: FAIL — `Unknown tool: list_providers`

- [ ] **Step 3: Register the tool**

In `src/search_meta_api/mcp_server.py`, add the description constant next to the others:

```python
LIST_PROVIDERS_DESCRIPTION = """\
List the configured search providers in failover order. Returns each \
instance's name, adapter type, circuit-breaker state, the unified filters it \
can honour, its result cap, and the vendor-specific passthrough params it \
accepts. Call this before using the `params` argument of `search`, and to \
understand why a search reported a filter as degraded.\
"""
```

Extend the `service` import to `from search_meta_api.service import AllProvidersFailed, describe_providers, run_search`, and register the tool immediately after the `search` tool:

```python
    @mcp.tool(description=LIST_PROVIDERS_DESCRIPTION)
    async def list_providers() -> dict[str, Any]:
        return describe_providers(app.state)
```

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/pytest tests/test_mcp.py -v`
Expected: PASS — 14 tests.

Then the full suite: `.venv/bin/pytest -q`
Expected: 311 passed.

- [ ] **Step 5: Lint and commit**

```bash
.venv/bin/ruff check src tests
git add src/search_meta_api/mcp_server.py tests/test_mcp.py
git commit -m "feat: add a list_providers MCP tool

Scoped params are unusable without it - an agent cannot name an instance
or a passthrough key it has no way to discover."
```

---

### Task 5: Document the MCP endpoint

**Files:**
- Modify: `README.md`

**Interfaces:**
- Consumes: the finished endpoint from Tasks 2–4.
- Produces: nothing code-facing.

- [ ] **Step 1: Add the README section**

Add a `## MCP server` section after the request/response documentation and before the deployment material. Read the surrounding sections first and match their voice — the README explains *why* a thing behaves as it does, not just that it does.

The section must cover:

````markdown
## MCP server

The same service is reachable over the Model Context Protocol at
`POST /mcp` — same container, same bearer token, same provider chain. The
tool calls the router in-process; there is no second service to deploy.

Register it with any client that speaks remote MCP over streamable HTTP:

```bash
claude mcp add --transport http search https://search.local.alban.me/mcp \
  --header "Authorization: Bearer <your token>"
```

Drop the `--header` if `server.auth_token` is unset — `/mcp` is then open,
exactly as `/search` is.

**The URL has no trailing slash.** `/mcp` is the endpoint; `/mcp/` is not.

Two tools:

- **`search`** — the same flat, typed parameters as `POST /search`, including
  the scoped `params` map, returning the same response body. `degraded` and
  `truncated` mean exactly what they mean over HTTP, and `truncated` is worth
  the same attention: an empty `results` with `truncated` true is not the same
  answer as an empty `results` with `truncated` false.
- **`list_providers`** — the `GET /providers` payload. An agent needs it
  before it can write a scoped param like `tavily/search_depth`.

Two differences from the HTTP surface, both in how failures arrive rather than
what counts as one:

- A `422` (bad `params`) and a `503` (every provider errored) both become MCP
  tool errors. The `503` body is serialised into the error text, so the
  `attempts` array survives.
- Everything else is unchanged — in particular, an empty result set stays a
  success on both surfaces, because it is a legitimate answer.
````

- [ ] **Step 2: Verify the documented command matches the implementation**

Confirm the path in the README matches `MCP_PATH` in `src/search_meta_api/mcp_server.py`, and that the header name matches what `_RequireBearer` reads (`authorization`).

Run: `grep -n 'MCP_PATH = ' src/search_meta_api/mcp_server.py`
Expected: `MCP_PATH = "/mcp"`

- [ ] **Step 3: Run the full suite once more**

Run: `.venv/bin/pytest -q`
Expected: 311 passed.

- [ ] **Step 4: Commit**

```bash
git add README.md
git commit -m "docs: document the MCP endpoint, its two tools, and its error mapping"
```

---

## Notes for the implementer

**One deviation from the spec, already reflected above.** Spec §5 says the auth
check should encode the header with `surrogateescape` before comparing, mirroring
`require_auth`. That care is needed on the HTTP surface because FastAPI hands you
a `str` and `secrets.compare_digest` raises `TypeError` on a non-ASCII `str`. The
ASGI scope carries header values as raw `bytes`, so the wrapper compares bytes
directly and the failure mode cannot occur. Task 3 tests the outcome (a clean
`401`) rather than the mechanism.

**If a test in `tests/test_app.py` breaks during Task 1**, the extraction changed
behaviour. Fix `service.py`; do not adjust the test. Those 292 tests are the only
thing standing between this refactor and a silent regression in failover, breaker,
or degraded-filter handling.

**The `params` ordering invariant is the subtle one.** `max_results` must be
defaulted after `validate_params` runs, and an omitted MCP argument must never
reach `SearchRequest`. Both exist for the same reason: `model_fields_set` is how
the service tells "the caller asked for this" from "this is just the default".
