"""Retired context MCPs are replaced by explicit stateless CLI operations."""
import pytest

from vibecanvas_api.agents.commands.registry import COMMAND_MODES
from vibecanvas_api.agents.tools import builtin_tool_names
from vibecanvas_api.flowork_cli.cli import validate_arguments
from vibecanvas_api.services.platform_mcp.invocation import platform_mcp_tool_manifest


def test_context_tools_retired_from_all_agent_catalogs():
    names = {tool.name for tool in platform_mcp_tool_manifest("interactive")}
    names.update(builtin_tool_names())
    for mode in COMMAND_MODES.values():
        names.update(mode.tools)
    assert not names.intersection({"list_workflows", "set_workflow", "create_workflow", "get_workflow", "update_canvas", "check_workflow", "new_version"})


@pytest.mark.parametrize("server", ["build", "workflow", "config", "deployment", "knowledge"])
def test_retired_business_servers_have_no_manifest(server):
    with pytest.raises(ValueError, match="unknown platform MCP server"):
        platform_mcp_tool_manifest(server)


def test_workflow_cli_requires_an_explicit_resource_not_chat_binding():
    with pytest.raises(ValueError):
        validate_arguments("workflow.download", {"major": "v1"})
    assert validate_arguments("workflow.download", {"workflow_id": "wf_123", "major": "v1"})["workflow_id"] == "wf_123"
