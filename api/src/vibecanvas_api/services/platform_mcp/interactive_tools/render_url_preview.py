"""Platform MCP ``render_url_preview`` — isolated interactive web pages."""
from __future__ import annotations

from typing import Any

from vibecanvas_api.services.platform_mcp.tool_runtime import ToolRuntime, tool

from vibecanvas_api.agents.tools.decorator import tool_error_boundary
from vibecanvas_api.services.platform_mcp.interactive_tools.render_interactive import (
    _render_view,
)


@tool(response_format="content_and_artifact")
@tool_error_boundary(tool="render_url_preview")
async def render_url_preview(
    url: str,
    title: str = "",
    description: str = "",
    *,
    runtime: ToolRuntime,
) -> tuple[str, dict[str, Any]]:
    """Open an HTTP(S) page in the user's isolated Preview WebView.

    Use this when the result is an external web page that the user should see
    or operate without leaving the conversation. Any HTTP(S) destination is
    accepted; no domain or page-type allowlist is applied. The WebView does not
    receive Flowork authentication tokens or parent-page DOM access. A target
    site can still refuse iframe embedding with its own browser security
    policy, in which case the user can open it in a separate tab.

    Supply the absolute ``url``. ``title`` and ``description`` are optional;
    the Preview uses a neutral title when one is omitted.
    """
    return await _render_view(
        type="url_preview",
        title=title,
        url=url,
        description=description,
        require_human_confirm=False,
        runtime=runtime,
    )
