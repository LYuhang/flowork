"""File authoring capabilities no longer activate MCP servers."""
from __future__ import annotations

from vibecanvas_api.services.platform_mcp.catalog import BUILTIN_MCP_METADATA
from vibecanvas_api.services.agent_runtime.mcp_host_resolution import platform_mcp_names_for_modes


def test_file_authoring_mcps_are_retired():
    assert set(BUILTIN_MCP_METADATA) == {"interactive"}
    for name in ("diagram", "document"):
        assert name not in BUILTIN_MCP_METADATA
        # CLI receives a private capability; only interactive is an MCP catalog entry.
        assert platform_mcp_names_for_modes([name]) == ["interactive", "cli"]
