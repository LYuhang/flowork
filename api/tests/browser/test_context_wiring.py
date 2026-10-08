from unittest.mock import AsyncMock

from vibecanvas_api.browser.cluster_registry import TransportRegistry
from vibecanvas_api.schemas.chat import MessagePostBody
from vibecanvas_api.agents.tool_runtime import AgentContext


def test_browser_topology_is_not_part_of_model_context() -> None:
    """Official Playwright receives routing through its private MCP descriptor."""

    forbidden = {
        "browser",
        "browser_id",
        "browser_client_id",
        "browser_window_id",
        "browser_panel_context_id",
        "client_context_id",
    }
    assert forbidden.isdisjoint(MessagePostBody.model_fields)
    assert forbidden.isdisjoint(AgentContext.model_fields)


async def test_transport_registry_replacement_is_connection_fenced(browser_connections) -> None:
    local = TransportRegistry()

    async def old_sender(_raw): ...
    async def new_sender(_raw): ...

    await local.register("tenant:user:browser", old_sender, session_id="session", close=AsyncMock())
    await local.register("tenant:user:browser", new_sender, session_id="session", close=AsyncMock())
    assert await local.unregister("tenant:user:browser", old_sender) is False
    assert await local.is_connected("tenant:user:browser") is True
    assert await local.unregister("tenant:user:browser", new_sender) is True
