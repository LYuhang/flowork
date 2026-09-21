"""Reusable tools resolve without importing a selectable Agent Runtime."""

from __future__ import annotations


def test_get_workflow_tool_resolvable():
    from vibecanvas_api.services.platform_mcp.workflow_tools import get_workflow
    assert get_workflow.name == "get_workflow"
    assert get_workflow.description


def test_read_file_tool_resolvable():
    from vibecanvas_api.agents.tools.fs import read_file
    assert hasattr(read_file, "name")
    assert read_file.name == "read_file"


def test_tools_package_reserves_a_non_empty_surface():
    from vibecanvas_api.agents.tools import builtin_tool_names
    assert builtin_tool_names()


def test_node_execute_is_platform_mcp_only():
    from vibecanvas_api.agents.tools import builtin_tool_names
    from vibecanvas_api.services.platform_mcp.run_tools import RUN_TOOLS

    names = builtin_tool_names()
    assert {"node_execute", "run_workflow"}.isdisjoint(names)
    assert {"node_execute", "run_workflow"} <= {tool.name for tool in RUN_TOOLS}
