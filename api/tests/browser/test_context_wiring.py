from vibecanvas_api.browser.registry import TransportRegistry
from vibecanvas_api.schemas.chat import MessagePostBody
from vibecanvas_api.services.platform_mcp.tool_runtime import AgentContext


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


def test_transport_registry_replacement_is_connection_fenced() -> None:
    local = TransportRegistry()

    async def old_sender(_raw): ...
    async def new_sender(_raw): ...

    local.register("tenant:user:browser", old_sender)
    local.register("tenant:user:browser", new_sender)
    assert local.unregister("tenant:user:browser", old_sender) is False
    assert local.is_connected("tenant:user:browser") is True
    assert local.unregister("tenant:user:browser", new_sender) is True
