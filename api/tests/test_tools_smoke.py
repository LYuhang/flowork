"""Reusable tools resolve without importing a selectable Agent Runtime."""

from __future__ import annotations


def test_render_tools_are_the_complete_platform_surface():
    from vibecanvas_api.services.platform_mcp.invocation import platform_mcp_tool_manifest
    tools = platform_mcp_tool_manifest("interactive")
    assert {tool.name for tool in tools} == {"render_preview", "render_choices"}
    assert all(tool.description for tool in tools)


def test_workflow_tools_are_native_and_noninteractive():
    from langchain_core.tools import StructuredTool
    from vibecanvas_api.agents.tools.subagent.toolset import build_agent_subagent_tools
    tools = build_agent_subagent_tools()
    assert {tool.name for tool in tools} == {"bash", "web_search", "read_images"}
    assert all(isinstance(tool, StructuredTool) for tool in tools)


def test_tools_package_reserves_a_non_empty_surface():
    from vibecanvas_api.agents.tools import builtin_tool_names
    assert builtin_tool_names()


def test_execution_tools_are_retired_from_mcp():
    from vibecanvas_api.agents.tools import builtin_tool_names

    names = builtin_tool_names()
    assert {"node_execute", "run_workflow"}.isdisjoint(names)
