"""Platform MCP get_node_spec tool."""
from __future__ import annotations

from vibecanvas_api.services.platform_mcp.tool_runtime import ToolRuntime, tool

from vibecanvas_api.agents.prompts.node_definitions import (
    available_node_types,
    build_node_spec,
    format_node_spec,
)
from vibecanvas_api.agents.tools.decorator import ToolError, tool_output
from vibecanvas_api.agents.tools.render import Rendered, register_render


@register_render("get_node_spec")
def _render_get_node_spec(raw: dict, ctx) -> Rendered:
    node_type = raw.get("node_type") or "unknown"
    content = raw.get("content") or ""
    return Rendered(
        content=content,
        content_type="text/markdown",
        abstract=f"get_node_spec → {node_type}",
        extras={"node_type": node_type},
    )


@tool_output(content_type="text/markdown", tool="get_node_spec")
async def _do_get_node_spec(node_type: str, runtime: ToolRuntime) -> dict:
    node_type = (node_type or "").strip()
    if not node_type:
        raise ToolError(
            "missing_node_type",
            "node_type is required. Use one of: " + ", ".join(available_node_types()),
        )
    try:
        spec = build_node_spec(node_type)
    except KeyError:
        raise ToolError(
            "unknown_node_type",
            f"unknown node_type {node_type!r}. Available node types: "
            + ", ".join(available_node_types()),
        )
    return {"node_type": node_type, "content": format_node_spec(spec)}


@tool(response_format="content_and_artifact")
async def get_node_spec(node_type: str, *, runtime: ToolRuntime) -> str:
    """Return the exact schema and authoring rules for one workflow node type.

    Use this before creating or modifying a node when the current context does
    not already contain that node type's definition. For PromptNode and
    SubAgentNode, also call get_config(scope='global') in the current build turn
    and use one enabled models key verbatim; this spec never supplies a default
    model. The result includes the
    node's purpose, when to use it, when not to use it, constraints,
    node_config field guide, CONFIG_SCHEMA, and examples.

    Args:
        node_type: exact node type name. Supported values are:
            `StartNode`, `EndNode`, `CodeNode`, `PromptNode`,
            `SubAgentNode`, `ParallelStartNode`, `ParallelEndNode`,
            `ConditionNode`, `LoopBeginNode`, `LoopEndNode`, `HTTPRequestNode`,
            `TransformNode`, `TemplateNode`, `TableReadNode`, `TableWriteNode`.

    Returns:
        content = markdown node specification for the requested node type.
    """
    return await _do_get_node_spec(node_type, runtime)
