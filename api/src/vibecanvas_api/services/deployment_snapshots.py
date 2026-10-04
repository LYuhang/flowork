"""Resolve a Deployment's saved execution graph in its caller's transaction."""
from fastapi import HTTPException

from vibecanvas_api.storage.models import WorkflowVersion
from vibecanvas_api.storage.workflow_repo import WorkflowRepo


async def resolve_workflow(session, user_id, deployment):
    """Read a host-authorized selector or a persisted instance's fixed reference.

    workflow_tenant_id is host-owned metadata, never a user-supplied selector.
    Restore the instance scope before accessing its credentials or writing runs.
    """
    from vibecanvas_api.storage.db import temporary_tenant_scope
    async with temporary_tenant_scope(session, deployment.get("workflow_tenant_id")):
        return await _read_workflow(session, user_id, deployment)


async def _read_workflow(session, user_id, deployment):
    repo = WorkflowRepo(session, str(user_id))
    wf_id = deployment["wf_id"]
    policy = deployment["version_pin"]
    major, sub = deployment.get("pinned_major"), deployment.get("pinned_sub")
    if policy in {"head", "major"}:
        versions = await repo.list_major_versions(wf_id)
        selected = max((v for v in versions if policy == "head" or v["v"] == major), key=lambda v: v["v"], default=None)
        if selected is None:
            raise HTTPException(422, "The requested workflow major does not exist.")
        major, sub = selected["v"], selected["sv"]
    if await session.get(WorkflowVersion, (wf_id, major, sub)) is None:
        raise HTTPException(422, "The requested workflow version does not exist.")
    graph = await repo.get_workflow_at(wf_id, major, sub)
    graph.setdefault("__meta__", {}).update(workflow_id=wf_id, workflow_version=major, workflow_subversion=sub)
    return graph
