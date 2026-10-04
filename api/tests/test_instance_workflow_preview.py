from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from vibecanvas_api.services import instance_workflow_preview as preview


@pytest.mark.asyncio
async def test_batch_preview_reads_frozen_graph_and_rejects_other_versions():
    task = SimpleNamespace(workflow_id="wf", task_type="batch_exec",
        payload={"workflow_snapshot": {"version": "v1.sv2", "workflow": {"node": {"value": "frozen"}}}})
    result = await preview.task_workflow_preview(None, "user", task, None, workflow_id="wf", version="v1.sv2")
    assert result["workflow"]["node"]["value"] == "frozen"
    for wf, version in [("other", "v1.sv2"), ("wf", "v2.sv0")]:
        with pytest.raises(HTTPException) as error:
            await preview.task_workflow_preview(None, "user", task, None, workflow_id=wf, version=version)
        assert error.value.status_code == 404


@pytest.mark.asyncio
async def test_schedule_only_reads_bound_version_and_source_organization(monkeypatch):
    freeze = AsyncMock(return_value={"version": "v1.sv2", "workflow": {}})
    monkeypatch.setattr(preview, "freeze_workflow", freeze)
    task = SimpleNamespace(workflow_id="wf", task_type="scheduled_run", workflow_tenant_id="source-org")
    schedule = SimpleNamespace(workflow_selector={"version": "v1.sv2"})
    with pytest.raises(HTTPException):
        await preview.task_workflow_preview(None, "actor", task, schedule, workflow_id="wf", version="v9.sv0")
    freeze.assert_not_called()
    await preview.task_workflow_preview(None, "actor", task, schedule, workflow_id="wf", version="v1.sv2")
    freeze.assert_awaited_once_with(None, "actor", "wf", version="v1.sv2", workflow_tenant_id="source-org")


@pytest.mark.asyncio
async def test_deployment_uses_only_its_own_revisions(monkeypatch):
    freeze = AsyncMock(return_value={"version": "v1.sv2", "workflow": {}})
    monkeypatch.setattr(preview, "freeze_workflow", freeze)
    revision = dict(wf_id="wf", version_pin="specific", pinned_major=1, pinned_sub=2, workflow_tenant_id="source")
    session = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: [revision]))))
    dep = {**revision, "id": "dep", "pinned_sub": 3}
    with pytest.raises(HTTPException):
        await preview.deployment_workflow_preview(session, "actor", dep, workflow_id="foreign", version="v1.sv2")
    freeze.assert_not_called()
    await preview.deployment_workflow_preview(session, "actor", dep, workflow_id="wf", version="v1.sv2")
    assert session.execute.call_args.args[1] == {"id": "dep"}
    freeze.assert_awaited_once_with(session, "actor", "wf", version="v1.sv2", workflow_tenant_id="source")


@pytest.mark.asyncio
async def test_revision_query_runs_against_real_schema(app_engine, monkeypatch):
    import uuid
    from vibecanvas_api.storage.db import session_scope
    freeze = AsyncMock(return_value={"version": "v1.sv2", "workflow": {}})
    monkeypatch.setattr(preview, "freeze_workflow", freeze)
    dep = dict(id=uuid.uuid4(), wf_id="wf", version_pin="specific",
               pinned_major=1, pinned_sub=2, workflow_tenant_id=str(uuid.uuid4()))
    async with session_scope(tenant_id=str(uuid.uuid4())) as session:
        result = await preview.deployment_workflow_preview(session, "actor", dep,
            workflow_id="wf", version="v1.sv2")
    assert result["workflow"] == {}
    freeze.assert_awaited_once()
