"""Platform MCP new_version tool."""
from __future__ import annotations

import asyncio

from vibecanvas_api.services.platform_mcp.tool_runtime import ToolRuntime, tool

from vibecanvas_api.agents.tools.decorator import ToolError, tool_output
from vibecanvas_api.agents.tools.render import Rendered, register_render
from vibecanvas_api.authorization.types import (
    Action,
    ConsistencyPreference,
)
from vibecanvas_api.services.platform_mcp.authorization import (
    recheck_platform_workflow_action,
)
from vibecanvas_api.services.platform_mcp.build_tools._target import target_workflow_id


@register_render("new_version")
def _render_new_version(raw: dict, ctx) -> Rendered:
    prev_v = raw.get("prev_major_version", "?")
    new_v = raw.get("new_major_version", "?")
    content = f"Created major version v{new_v}.sv0 (branched from v{prev_v})."
    abstract = f"new_version → v{prev_v} → v{new_v}"
    return Rendered(
        content=content,
        content_type="text/plain",
        abstract=abstract,
        extras={"workflow_id": raw.get("workflow_id")},
    )


@tool_output(content_type="text/plain", tool="new_version")
async def _do_new_version(runtime: ToolRuntime) -> dict:
    ctx = runtime.context
    workflow = ctx.workflow
    wf_id = (
        (workflow.get("__meta__") or {}).get("workflow_id")
        or target_workflow_id(ctx)
    )
    await recheck_platform_workflow_action(
        ctx,
        wf_id,
        Action.UPDATE,
        consistency=ConsistencyPreference.HIGHER_CONSISTENCY,
    )

    def _run():
        if not wf_id:
            raise ToolError("no_workflow", "no active workflow")
        repo = ctx.repo
        if not repo:
            raise ToolError("no_repo", "no repository available")
        prev_v = (workflow.get("__meta__") or {}).get("workflow_version", "?")
        new_v = repo.new_version(wf_id, workflow, note="agent: new_version")
        current = repo.get_current_workflow(wf_id)
        if current:
            ctx.workflow = current
        ctx.workflow_dirty = False
        return {"prev_major_version": prev_v, "new_major_version": new_v, "workflow_id": wf_id}

    try:
        return await asyncio.to_thread(_run)
    except ToolError:
        raise
    except Exception as e:
        raise ToolError("version_failed", str(e))


@tool(response_format="content_and_artifact")
async def new_version(*, runtime: ToolRuntime) -> str:
    """Bump the workflow to a new major version (e.g. v2 → v3).

    Snapshots the current canvas state as v{N+1}.sv0 and moves HEAD to it.
    The previous major remains in history.

    Returns:
        content = confirmation showing the previous and new major version numbers.
    """
    return await _do_new_version(runtime)
