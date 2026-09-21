"""Cross-Runtime workflow discovery tools exposed only through Platform MCP."""

from __future__ import annotations

import json
from copy import deepcopy

from vibecanvas_api.services.platform_mcp.tool_runtime import ToolRuntime, tool

from vibecanvas_api.agents.tools.decorator import ToolError, tool_output
from vibecanvas_api.agents.tools.render import Rendered, register_render
from vibecanvas_api.authorization.types import Action
from vibecanvas_api.services.platform_mcp.authorization import (
    list_authorized_workflows,
    load_authorized_workflow,
)


DEFAULT_WORKFLOW_PATH = "/data/workflow.json"


def _workflow_id(ctx, explicit_workflow_id: str | None = None) -> str:
    workflow_id = str(
        explicit_workflow_id
        or getattr(ctx, "current_workflow_id", "")
        or ""
    ).strip()
    if not workflow_id:
        raise ToolError(
            "no_workflow",
            "No workflow was provided or selected for this chat. Pass an exact "
            "workflow_id returned by list_workflows, or activate /workflow to "
            "select or create one.",
        )
    return workflow_id


def _node_count(workflow: dict) -> int:
    return sum(
        1
        for key, value in workflow.items()
        if isinstance(key, str)
        and not key.startswith("__")
        and isinstance(value, dict)
    )


@register_render("list_workflows")
def _render_list_workflows(raw: dict, ctx) -> Rendered:
    items = raw.get("workflows", [])
    current = raw.get("current_workflow_id") or "none"
    lines = [f"Current workflow: {current}", ""]
    for item in items:
        name = item.get("workflow_name") or item.get("wf_id")
        description = item.get("description") or ""
        status = item.get("status") or "draft"
        lines.append(
            f"- id: {item.get('wf_id')}\n"
            f"  name: {name}\n"
            f"  description: {description or '(empty)'}\n"
            f"  status: {status}\n"
            f"  version: v{item.get('active_major', '?')}.sv"
            f"{item.get('active_sub', '?')}"
        )
    if not items:
        lines.append("No workflows.")
    return Rendered(
        content="\n".join(lines),
        content_type="text/plain",
        abstract=f"list_workflows -> {len(items)} workflow(s)",
    )


@tool_output(content_type="text/plain", tool="list_workflows")
async def _do_list_workflows(runtime: ToolRuntime) -> dict:
    ctx = runtime.context
    items = await list_authorized_workflows(ctx)
    return {
        "current_workflow_id": getattr(ctx, "current_workflow_id", None),
        "workflows": items,
    }


@tool(response_format="content_and_artifact")
async def list_workflows(*, runtime: ToolRuntime) -> str:
    """List workflows available to the user and the current Chat selection."""
    return await _do_list_workflows(runtime)


@register_render("get_workflow")
def _render_get_workflow(raw: dict, ctx) -> Rendered:
    path = raw.get("path") or DEFAULT_WORKFLOW_PATH
    count = raw.get("node_count", "?")
    version = raw.get("version", "?")
    subversion = raw.get("subversion", "?")
    return Rendered(
        content=(
            f"Exported current canvas workflow to {path}\n"
            f"Workflow ID: {raw.get('workflow_id')}\n"
            f"Version: v{version}.sv{subversion}\n"
            f"Nodes: {count}"
        ),
        content_type="text/plain",
        abstract=(
            f"get_workflow → exported {count} node(s) to {path} "
            f"(v{version}.sv{subversion})"
        ),
        extras={"workflow_id": raw.get("workflow_id"), "path": path},
    )


@tool_output(content_type="text/plain", tool="get_workflow")
async def _do_get_workflow(
    runtime: ToolRuntime,
    workflow_path: str = DEFAULT_WORKFLOW_PATH,
    *,
    workflow_id: str | None = None,
) -> dict:
    ctx = runtime.context
    workflow_id = _workflow_id(ctx, workflow_id)
    snapshot = await load_authorized_workflow(
        ctx,
        workflow_id,
        Action.VIEW,
    )
    workflow = snapshot.workflow
    if not isinstance(workflow, dict):
        raise ToolError("bad_workflow", "workflow must be a JSON object")

    stamped = deepcopy(workflow)
    meta = snapshot.meta
    workflow_meta = stamped.setdefault("__meta__", {})
    workflow_meta["workflow_id"] = workflow_id
    if meta:
        workflow_meta["workflow_name"] = meta.get(
            "workflow_name", workflow_meta.get("workflow_name")
        )
        workflow_meta["workflow_version"] = meta.get(
            "active_major", workflow_meta.get("workflow_version")
        )
        workflow_meta["workflow_subversion"] = meta.get(
            "active_sub", workflow_meta.get("workflow_subversion")
        )

    path = (workflow_path or DEFAULT_WORKFLOW_PATH).strip()
    session = await ctx.sandbox_session()
    write_result = await session.write_file(
        path,
        json.dumps(stamped, ensure_ascii=False, indent=2) + "\n",
    )
    if not write_result.get("ok"):
        error = str(write_result.get("error") or "write failed")
        if error == "path_outside_roots":
            raise ToolError("invalid_path", f"path {path!r} is outside the allowed roots")
        raise ToolError("write_failed", f"could not write {path!r}: {error}")

    ctx.workflow = stamped
    return {
        "workflow": stamped,
        "workflow_id": workflow_id,
        "path": path,
        "node_count": _node_count(stamped),
        "version": workflow_meta.get("workflow_version", "?"),
        "subversion": workflow_meta.get("workflow_subversion", "?"),
    }


@tool(response_format="content_and_artifact")
async def get_workflow(
    workflow_id: str | None = None,
    workflow_path: str = DEFAULT_WORKFLOW_PATH,
    *,
    runtime: ToolRuntime,
) -> str:
    """Export an exact or currently selected workflow as sandbox JSON."""
    return await _do_get_workflow(
        runtime,
        workflow_path,
        workflow_id=workflow_id,
    )


WORKFLOW_MCP_TOOLS = [list_workflows, get_workflow]

__all__ = [
    "DEFAULT_WORKFLOW_PATH",
    "WORKFLOW_MCP_TOOLS",
    "get_workflow",
    "list_workflows",
]
