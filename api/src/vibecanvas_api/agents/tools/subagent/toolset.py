"""Tool adapter for the sandboxed workflow ``SubAgentNode``.

Flowork's reusable tools are described with its own lightweight ``PlatformTool``
type. The workflow node currently executes a bounded LangChain loop, so this
module performs the SDK conversion lazily inside the workflow sandbox.
"""

from __future__ import annotations

from vibecanvas_api.services.agent_runtime.approval import PRE_TOOL_APPROVAL_TOOLS
from vibecanvas_api.services.platform_mcp.tool_runtime import PlatformTool


SUBAGENT_DEFAULT_TOOL_NAMES = (
    "read_file",
    "write_file",
    "edit_file",
    "grep",
    "read_images",
    "web_search",
    "bash",
)


def _to_langchain_tool(definition: PlatformTool):
    from langchain_core.tools import StructuredTool

    return StructuredTool.from_function(
        coroutine=definition.coroutine,
        name=definition.name,
        description=definition.description,
        args_schema=definition.tool_call_schema,
        response_format="content_and_artifact",
    )


def build_agent_subagent_tools() -> list:
    """Return the fixed, reviewed tool surface for a workflow subagent."""
    from vibecanvas_api.agents.tools.fs import FS_TOOLS
    from vibecanvas_api.agents.tools.media import MEDIA_TOOLS
    from vibecanvas_api.agents.tools.sandbox import bash
    from vibecanvas_api.agents.tools.web import WEB_TOOLS

    definitions = [*FS_TOOLS, *MEDIA_TOOLS, *WEB_TOOLS, bash]
    names = tuple(item.name for item in definitions)
    if names != SUBAGENT_DEFAULT_TOOL_NAMES:
        raise RuntimeError(
            "Workflow subagent tool contract changed; review the fixed allowlist "
            f"before enabling it (actual={names!r})"
        )
    forbidden = set(names) & {*PRE_TOOL_APPROVAL_TOOLS, "render_interactive"}
    if forbidden:
        raise RuntimeError(
            "Workflow subagent tools must not create interactive requests: "
            + ", ".join(sorted(forbidden))
        )
    return [_to_langchain_tool(item) for item in definitions]
