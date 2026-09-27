"""Explicit Workflow targets. Never reads or mutates a Chat selection."""
from vibecanvas_api.agents.tools.decorator import ToolError
from vibecanvas_api.services.agent_resources.authorization import _require_workflow_read
from vibecanvas_api.storage.workflow_repo import WorkflowRepo


async def resolve_target(session, ctx, workflow_id: str, major: str, *, for_update=False):
    if not workflow_id or not major:
        raise ToolError("invalid_arguments", "Specify a workflow ID and --major vN.")
    await _require_workflow_read(session, ctx, workflow_id)
    repo = WorkflowRepo(session, ctx.username)
    meta = await repo.get_meta(workflow_id, for_update=for_update)
    if not meta:
        raise ToolError("workflow_unavailable", "The workflow is unavailable.")
    versions = await repo.list_major_versions(workflow_id)
    target = next((item for item in versions if f"v{item['v']}" == major), None)
    if target is None:
        raise ToolError("version_not_found", "The requested major version does not exist.")
    return {"id": workflow_id, "major": target["v"], "sub": target["sv"], "meta": meta}
