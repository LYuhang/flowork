"""Read only the fixed graph belonging to an already-authorized instance."""
from fastapi import HTTPException
from sqlalchemy import text

from vibecanvas_api.services.task_snapshots import freeze_workflow


def _snapshot_response(snapshot, *, workflow_id, requested_workflow_id, version):
    if workflow_id != requested_workflow_id or snapshot.get("version") != version:
        raise HTTPException(404, "instance_workflow_version_not_found")
    return {"workflow": snapshot["workflow"], "meta": {"workflow_name": workflow_id}}


async def task_workflow_preview(session, user_id, task, schedule, *, workflow_id, version):
    if task.workflow_id != workflow_id:
        raise HTTPException(404, "instance_workflow_version_not_found")
    if task.task_type == "batch_exec":
        snapshot = (task.payload or {}).get("workflow_snapshot") or {}
    elif schedule is not None:
        selected = (schedule.workflow_selector or {}).get("version")
        if selected != version:
            raise HTTPException(404, "instance_workflow_version_not_found")
        snapshot = await freeze_workflow(session, user_id, workflow_id,
            version=selected, workflow_tenant_id=task.workflow_tenant_id)
    else:
        raise HTTPException(404, "instance_workflow_version_not_found")
    return _snapshot_response(snapshot, workflow_id=task.workflow_id,
        requested_workflow_id=workflow_id, version=version)


async def deployment_workflow_preview(session, user_id, deployment, *, workflow_id, version):
    # A served revision can differ from the desired configuration during update.
    # Only a revision actually belonging to this instance can authorize a graph.
    specs = [deployment]
    rows = (await session.execute(text(
        "SELECT spec FROM deployment_runtime_revisions WHERE deployment_id=:id"
    ), {"id": deployment["id"]})).scalars().all()
    specs.extend(rows)
    selected = next((spec for spec in specs
        if spec.get("wf_id") == workflow_id
        and spec.get("version_pin") == "specific"
        and f"v{spec.get('pinned_major')}.sv{spec.get('pinned_sub')}" == version), None)
    if selected is None:
        raise HTTPException(404, "instance_workflow_version_not_found")
    snapshot = await freeze_workflow(session, user_id, workflow_id, version=version,
        workflow_tenant_id=selected.get("workflow_tenant_id"))
    return _snapshot_response(snapshot, workflow_id=workflow_id,
        requested_workflow_id=workflow_id, version=version)
