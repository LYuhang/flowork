"""Minimal native LangChain tools, built only inside a Workflow sandbox."""
from __future__ import annotations

SUBAGENT_DEFAULT_TOOL_NAMES = ("bash", "web_search", "read_images")


def build_agent_subagent_tools(*, working_dir: str | None = None) -> list:
    """Build a private toolset; no platform MCP/context conversion is needed."""
    from langchain_core.tools import StructuredTool

    from vibecanvas_api.agents.tools.media.read_images import read_images
    from vibecanvas_api.agents.tools.sandbox.bash import bash as run_bash
    from vibecanvas_api.agents.tools.web.web_search import web_search

    async def bash(command: str, timeout_s: float | None = None) -> tuple:
        return await run_bash(command, timeout_s, cwd=working_dir)

    bash.__doc__ = run_bash.__doc__
    tools = [
        StructuredTool.from_function(coroutine=fn, response_format="content_and_artifact")
        for fn in (bash, web_search, read_images)
    ]
    assert tuple(tool.name for tool in tools) == SUBAGENT_DEFAULT_TOOL_NAMES
    return tools
