import contextlib
import json

import httpx2
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
