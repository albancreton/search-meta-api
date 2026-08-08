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


from search_meta_api.config import Settings as _Settings

SETTINGS_AUTH = _Settings(server={"auth_token": "s3cret"},
                          providers=[{"name": "a", "type": "tavily", "api_key": "k"}])


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
    with pytest.raises(BaseException):  # noqa: B017 -- deliberately broad, see docstring
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
