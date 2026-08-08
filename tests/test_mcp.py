import asyncio
import contextlib
import json

import httpx2
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from test_app import SETTINGS_NO_AUTH, fail, hit

from search_meta_api.app import create_app
from search_meta_api.config import Settings
from search_meta_api.errors import ErrorClass
from search_meta_api.models import UnifiedFilters

SETTINGS_AUTH = Settings(server={"auth_token": "s3cret"},
                         providers=[{"name": "a", "type": "tavily", "api_key": "k"}])


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
        async with (
            http,
            streamable_http_client("http://test/mcp", http_client=http) as (read, write),
            ClientSession(read, write) as session,
        ):
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
        assert "scope/name" in properties["params"]["description"]
        assert tools["search"].description
        # The bounds must survive into the advertised schema, or an agent
        # discovers them only by making a call that fails. Read from the model
        # rather than restated, so the two cannot drift apart.
        assert properties["query"]["maxLength"] == _bound(UnifiedFilters, "query", "max_length")
        assert properties["query"]["minLength"] == _bound(UnifiedFilters, "query", "min_length")
        integer, = [s for s in properties["max_results"]["anyOf"] if s["type"] == "integer"]
        assert integer["minimum"] == _bound(UnifiedFilters, "max_results", "ge")
        assert integer["maximum"] == _bound(UnifiedFilters, "max_results", "le")
        string, = [s for s in properties["country"]["anyOf"] if s["type"] == "string"]
        assert string["maxLength"] == _bound(UnifiedFilters, "country", "max_length")


def _bound(model, field, constraint):
    """The value of a pydantic constraint as declared on `model`.

    Constraints reach the field as annotated_types markers (`MinLen`, `Ge`, …)
    whose single attribute is named after the marker, not after the `Field`
    keyword that produced them; this maps back from the keyword.
    """
    attribute = {"min_length": "min_length", "max_length": "max_length",
                 "ge": "ge", "le": "le"}[constraint]
    for marker in model.model_fields[field].metadata:
        if hasattr(marker, attribute):
            return getattr(marker, attribute)
    raise AssertionError(f"{model.__name__}.{field} declares no {constraint}")


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


async def test_the_configured_token_is_accepted():
    app = create_app(SETTINGS_AUTH, instances=[hit("a", [("Cats", "https://a.com")])])
    async with mcp_session(app, token="s3cret") as session:
        result = await session.call_tool("search", {"query": "cats"})
    assert result.is_error is False
    assert result.structured_content["provider"] == "a"


async def test_a_wrong_token_is_rejected_with_a_401():
    app = create_app(SETTINGS_AUTH, instances=[hit("a", [])])
    response = await _raw_mcp_post(app, {"Authorization": "Bearer wrong"})
    assert response.status_code == 401


async def test_a_missing_token_is_rejected_with_a_401():
    app = create_app(SETTINGS_AUTH, instances=[hit("a", [])])
    response = await _raw_mcp_post(app, {})
    assert response.status_code == 401


async def test_the_protocol_handshake_cannot_complete_without_a_token():
    """The 401 tests above pin the status; this pins the consequence — no
    session, so no tools. Asserted separately because a `pytest.raises` on
    the session alone would also pass if the endpoint broke outright.
    """
    app = create_app(SETTINGS_AUTH, instances=[hit("a", [])])
    # The client's task group surfaces the transport's failure wrapped, so
    # ExceptionGroup is the type actually raised — narrow enough to fail if the
    # handshake ever starts succeeding, and it will not swallow a CancelledError.
    with pytest.raises(ExceptionGroup):
        async with mcp_session(app, token="wrong"):
            pass


async def test_a_non_ascii_token_is_rejected_rather_than_crashing():
    """A latin-1 header full of non-ASCII bytes must be a clean 401. The HTTP
    surface needed care here because compare_digest raises TypeError on a
    non-ASCII str; the ASGI scope carries raw bytes, so the comparison is
    safe by construction. This test is what keeps it that way.
    """
    app = create_app(SETTINGS_AUTH, instances=[hit("a", [])])
    # httpx2 encodes str header values as ascii and raises client-side on a
    # non-ASCII character, before the request is ever sent. Pre-encoding to
    # latin-1 bytes here is what actually gets the non-ASCII byte onto the
    # wire, matching what the docstring above describes.
    response = await _raw_mcp_post(app, {"Authorization": "Bearer sécret".encode("latin-1")})
    assert response.status_code == 401


async def test_the_endpoint_is_open_when_no_token_is_configured():
    app = create_app(SETTINGS_NO_AUTH, instances=[hit("a", [])])
    async with mcp_session(app) as session:
        assert (await session.list_tools()).tools


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


async def test_list_providers_reaches_the_tool_with_a_valid_token():
    app = create_app(SETTINGS_AUTH, instances=[hit("a", [])])
    async with mcp_session(app, token="s3cret") as session:
        result = await session.call_tool("list_providers", {})
    assert result.is_error is False
    assert [p["name"] for p in result.structured_content["providers"]] == ["a"]


async def test_list_providers_is_refused_without_the_token():
    """The bearer gate wraps the whole ASGI endpoint, so it must refuse a
    `tools/call` naming this specific tool, not just the handshake.
    """
    app = create_app(SETTINGS_AUTH, instances=[hit("a", [])])
    response = await _raw_mcp_post(app, {}, body={
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "list_providers", "arguments": {}},
    })
    assert response.status_code == 401


async def test_the_apps_own_lifespan_makes_mcp_work():
    """Every other test in this file enters the session manager itself, because
    httpx's ASGI transport sends no lifespan events — which means none of them
    exercise the `enter_async_context(...run())` line in app.lifespan, the only
    thing that starts the manager in production. This one drives the real ASGI
    lifespan instead, so removing that line fails here and nowhere else.
    """
    app = create_app(SETTINGS_NO_AUTH, instances=[hit("a", [])])
    # A queue, not a preloaded list: the server asks for the next lifespan
    # message as soon as startup finishes, and handing it the shutdown right
    # away would tear the app down before the request below is ever made.
    inbox: asyncio.Queue = asyncio.Queue()
    received: list[dict] = []
    started = asyncio.Event()

    async def send(message):
        received.append(message)
        started.set()

    task = asyncio.create_task(
        app({"type": "lifespan", "asgi": {"version": "3.0"}}, inbox.get, send))
    await inbox.put({"type": "lifespan.startup"})
    await asyncio.wait_for(started.wait(), timeout=10)
    assert received[0] == {"type": "lifespan.startup.complete"}

    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            headers={"Accept": "application/json, text/event-stream"},
        )
    assert response.status_code == 200
    assert [t["name"] for t in response.json()["result"]["tools"]] == [
        "search", "list_providers"]

    await inbox.put({"type": "lifespan.shutdown"})
    await asyncio.wait_for(task, timeout=10)
    assert received[-1] == {"type": "lifespan.shutdown.complete"}


async def test_a_get_is_refused_promptly_rather_than_held_open():
    """GET is the transport's server-to-client SSE verb. Routed, the SDK opens
    a stream that stateless mode never writes to and never closes, pinning a
    connection per request — unauthenticated, when no token is configured. The
    route must not accept the verb at all.
    """
    app = create_app(SETTINGS_NO_AUTH, instances=[hit("a", [])])
    async with app.state.mcp_session_manager.run():
        transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(transport=transport,
                                      base_url="http://test") as client:
            response = await asyncio.wait_for(
                client.get("/mcp",
                           headers={"Accept": "application/json, text/event-stream"}),
                timeout=10,
            )
    assert response.status_code == 405


async def test_delete_still_reaches_the_mcp_layer():
    """The companion to the GET test: dropping GET from the route must not
    take DELETE with it. Stateless mode has no session to terminate, so the
    answer is a refusal either way — but it comes from the MCP layer.
    """
    app = create_app(SETTINGS_NO_AUTH, instances=[hit("a", [])])
    async with app.state.mcp_session_manager.run():
        transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(transport=transport,
                                      base_url="http://test") as client:
            response = await client.delete("/mcp")
    assert response.status_code == 405
    assert response.json()["error"]


async def test_duplicate_authorization_headers_resolve_the_same_way_on_both_surfaces():
    """A request carrying two Authorization headers must get the same verdict
    from `/mcp` as from `/search`. The MCP gate reads the raw ASGI scope and
    the HTTP gate reads Starlette's Headers; if they disagreed on which
    duplicate wins, each would accept a request the other refuses.
    """
    good, bad = "Bearer s3cret", "Bearer wrong"
    for first, second, expected in ((bad, good, 401), (good, bad, 200)):
        headers = [(b"authorization", first.encode()), (b"authorization", second.encode())]
        app = create_app(SETTINGS_AUTH, instances=[hit("a", [])])
        async with app.state.mcp_session_manager.run():
            transport = httpx2.ASGITransport(app=app)
            async with httpx2.AsyncClient(transport=transport,
                                          base_url="http://test") as client:
                over_mcp = await client.post(
                    "/mcp",
                    json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                    headers=[(b"accept", b"application/json, text/event-stream"), *headers],
                )
                over_http = await client.get("/providers", headers=headers)
        # First-wins on both: the first header decides.
        assert over_mcp.status_code == expected
        assert over_http.status_code == expected


async def _raw_mcp_post(app, headers, body=None):
    """POST to /mcp below the protocol layer, to observe the HTTP status."""
    if body is None:
        body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    async with app.state.mcp_session_manager.run():
        transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(transport=transport,
                                      base_url="http://test") as client:
            return await client.post(
                "/mcp",
                json=body,
                headers={"Accept": "application/json, text/event-stream", **headers},
            )
