"""Platform MCP check_workflow tool."""
from __future__ import annotations

from vibecanvas_api.services.platform_mcp.tool_runtime import ToolRuntime, tool

from vibecanvas_api.agents.tools.decorator import ToolError, tool_output
from vibecanvas_api.agents.tools.render import Rendered, register_render
from vibecanvas_api.services.platform_mcp.build_tools.workflow_file import (
    DEFAULT_WORKFLOW_PATH,
    collect_workflow_warnings,
    read_workflow_file,
    validate_workflow_for_context,
)


@register_render("check_workflow")
def _render_check_workflow(raw: dict, ctx) -> Rendered:
    errors = raw.get("errors") or []
    warnings = raw.get("warnings") or []
    node_count = raw.get("node_count", "?")
    path = raw.get("path") or DEFAULT_WORKFLOW_PATH
    if not errors:
        content = (
            f"Validation passed for {path}.\n"
            f"Nodes: {node_count}"
        )
        if warnings:
            lines = [content, f"Warnings: {len(warnings)}"]
            for w in warnings[:20]:
                nid = w.get("node_id", "?")
                msg = w.get("message", "")
                lines.append(f"  [{nid}] {msg}")
            if len(warnings) > 20:
                lines.append(f"  ... {len(warnings) - 20} more warning(s)")
            content = "\n".join(lines)
        abstract = f"check_workflow → pass {path} ({node_count} nodes)"
    else:
        lines = [f"Validation found {len(errors)} issue(s) in {path}:"]
        for e in errors:
            nid = e.get("node_id", "?")
            msg = e.get("message", "")
            lines.append(f"  [{nid}] {msg}")
        if warnings:
            lines.append(f"Warnings: {len(warnings)}")
            for w in warnings[:20]:
                nid = w.get("node_id", "?")
                msg = w.get("message", "")
                lines.append(f"  [{nid}] {msg}")
            if len(warnings) > 20:
                lines.append(f"  ... {len(warnings) - 20} more warning(s)")
        content = "\n".join(lines)
        abstract = f"check_workflow → {len(errors)} error(s) in {path} ({node_count} nodes)"
    return Rendered(
        content=content,
        content_type="text/plain",
        abstract=abstract,
        extras={"workflow_id": raw.get("workflow_id"), "path": path},
    )


@tool_output(content_type="text/plain", tool="check_workflow")
async def _do_check_workflow(runtime: ToolRuntime, workflow_path: str = DEFAULT_WORKFLOW_PATH) -> dict:
    try:
        path = workflow_path or DEFAULT_WORKFLOW_PATH
        workflow = await read_workflow_file(runtime, path)
        node_count = sum(1 for k, v in workflow.items() if not k.startswith("__") and isinstance(v, dict))
        return {
            "errors": await validate_workflow_for_context(workflow, runtime.context),
            "warnings": collect_workflow_warnings(workflow),
            "node_count": node_count,
            "workflow_id": (workflow.get("__meta__") or {}).get("workflow_id"),
            "path": path,
        }
    except ToolError:
        raise
    except Exception as e:
        raise ToolError("check_failed", str(e))


@tool(response_format="content_and_artifact")
async def check_workflow(workflow_path: str = DEFAULT_WORKFLOW_PATH, *, runtime: ToolRuntime) -> str:
    """Validate a workflow JSON file without changing the canvas.

    The file must contain a JSON object keyed by node ids, with optional
    `__meta__`. The tool checks graph structure and per-node configuration, then
    returns either a pass summary, non-blocking warnings, or explicit validation
    errors.

    Checks:
    - Graph-level: exactly one StartNode, no isolated nodes unreachable from
      Start, no cycles in children edges, parallel/loop node pairs matched,
      input field references resolve to known node names and output fields.
    - Per-node: each node's configuration passes its own schema check.

    Args:
        workflow_path: workflow JSON file to validate.

    Returns:
        content = pass summary, [node_id] validation warnings, or validation errors.
    """
    return await _do_check_workflow(runtime, workflow_path)
