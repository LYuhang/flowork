"""Freeze saved Workflow content for durable tasks (caller authorizes access).

Selectors are resource configuration, never a Chat connection. Old schedules
without a selector explicitly retain their legacy global-HEAD policy.
"""
from copy import deepcopy
import re

from fastapi import HTTPException

from vibecanvas_api.storage.models import WorkflowVersion
from vibecanvas_api.storage.workflow_repo import WorkflowRepo


async def freeze_workflow(session, user_id, workflow_id, *, major=None, version=None):
    repo = WorkflowRepo(session, str(user_id))
    meta = await repo.get_meta(workflow_id)
    if not meta:
        raise HTTPException(404, "Workflow not found.")
    if major and version:
        raise HTTPException(422, "Specify major or version, not both.")
    if version:
        match = re.fullmatch(r"v([1-9][0-9]*)\.sv([0-9]+)", version)
        if not match:
            raise HTTPException(422, "Invalid version; use v2.sv3.")
        parent, sub = map(int, match.groups())
    elif major:
        if not re.fullmatch(r"v[1-9][0-9]*", major):
            raise HTTPException(422, "Invalid major; use v2.")
        choices = await repo.list_major_versions(workflow_id)
        selected = next((item for item in choices if f"v{item['v']}" == major), None)
        if selected is None:
            raise HTTPException(404, "Workflow major not found.")
        parent, sub = selected["v"], selected["sv"]
    else:
        parent, sub = meta["active_major"], meta["active_sub"]
    if await session.get(WorkflowVersion, (workflow_id, parent, sub)) is None:
        raise HTTPException(404, "Workflow version not found.")
    graph = deepcopy(await repo.get_workflow_at(workflow_id, parent, sub))
    graph.setdefault("__meta__", {}).update(workflow_id=workflow_id,
        workflow_version=parent, workflow_subversion=sub)
    return {"version": f"v{parent}.sv{sub}", "workflow": graph}
