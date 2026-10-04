"""Explicit, stateless Workflow major-version operations."""
from vibecanvas_api.authorization.types import Action, ConsistencyPreference
from vibecanvas_api.flowork_cli.cli import validate_arguments
from vibecanvas_api.services.agent_resources.authorization import (
    _workflow_decision,
    _require_active_chat_write,
    _require_workflow_read,
    _service,
    _workflow_resource,
)
from vibecanvas_api.storage.db import session_scope
from vibecanvas_api.storage.workflow_repo import WorkflowRepo

from .workflow_target import resolve_target


async def workflow_version_command(ctx, operation: str, arguments: dict) -> dict:
    arguments = validate_arguments(operation, arguments)
    workflow_id = arguments["workflow_id"]
    async with session_scope(tenant_id=ctx.tenant_id, user_id=ctx.username) as session:
        repo = WorkflowRepo(session, ctx.username)
        if operation == "workflow.version.list":
            await _require_workflow_read(session, ctx, workflow_id)
            meta = await repo.get_meta(workflow_id)
            if not meta:
                from vibecanvas_api.agents.tools.decorator import ToolError
                raise ToolError("workflow_unavailable", "The workflow is unavailable.")
            majors = await repo.list_major_versions(workflow_id)
            return {"id": workflow_id, "version": f"v{meta['active_major']}.sv{meta['active_sub']}",
                    "versions": [{"major": item["v"], "version": f"v{item['v']}.sv{item['sv']}"} for item in majors]}
        await _require_active_chat_write(session, ctx)
        selection = await resolve_target(session, ctx, workflow_id, arguments["major"], for_update=True)
        await _workflow_decision(session, ctx, workflow_id, action=Action.UPDATE, consistency=ConsistencyPreference.HIGHER_CONSISTENCY)
        major, sub = selection["major"], selection["sub"]
        graph = await repo.get_workflow_at(workflow_id, major, sub)
        new_major = await repo.new_version(workflow_id, graph,
            note=arguments["note"] or "agent: workflow version create", stamp_metadata=True,
            source_version=(major, sub))
        result = {"id": workflow_id, "previous_version": f"v{major}.sv{sub}", "version": f"v{new_major}.sv0"}
    return result
