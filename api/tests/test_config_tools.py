"""The old MCP surface must remain retired."""
import pytest
from vibecanvas_api.services.platform_mcp.invocation import platform_mcp_tool_manifest
from vibecanvas_api.services.agent_runtime.mcp_host_resolution import platform_mcp_names_for_modes


def test_config_mcp_is_retired():
    with pytest.raises(ValueError, match="unknown platform MCP"):
        platform_mcp_tool_manifest("config")
    assert "config" not in platform_mcp_names_for_modes([])
