"""Arrange a locked, explicitly selected saved branch without executing it."""
from copy import deepcopy

from vibecanvas_api.authorization.types import Action, ConsistencyPreference
from vibecanvas_api.storage.db import session_scope
from vibecanvas_api.storage.workflow_repo import WorkflowRepo

from .authorization import _workflow_decision, _require_active_chat_write, _service, _workflow_resource
from .workflow_graph import _auto_tidy_workflow
from .workflow_target import resolve_target


async def layout_workflow(ctx, *, workflow_id: str, major: str) -> dict:
    async with session_scope(tenant_id=ctx.tenant_id, user_id=ctx.username) as session:
        await _require_active_chat_write(session, ctx)
        selection = await resolve_target(session, ctx, workflow_id, major, for_update=True)
        await _workflow_decision(session, ctx, workflow_id, action=Action.UPDATE, consistency=ConsistencyPreference.HIGHER_CONSISTENCY)
        repo = WorkflowRepo(session, ctx.username)
        graph = deepcopy(await repo.get_workflow_at(workflow_id, selection["major"], selection["sub"]))
        moved = _auto_tidy_workflow(graph)
        version = f"v{selection['major']}.sv{selection['sub']}"
        if moved:
            pointer = await repo.commit(workflow_id, graph, note="agent: workflow layout",
                                        target_major=selection["major"], stamp_metadata=True)
            version = f"v{pointer.parent_v}.sv{pointer.sv}"
        result = {"id": workflow_id, "version": version, "changed": bool(moved),
                  "moved_nodes": moved,
                  "message": "Layout saved as a new subversion." if moved else "Layout is already tidy. No new version was created."}
    return result
