import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import HTTPException

from vibecanvas_api.routes import workflows
from vibecanvas_api.services import state_notifications
from vibecanvas_api.storage.db import session_scope
from vibecanvas_api.storage.workflow_repo import WorkflowRepo
from tests.storage.test_workflow_history import owner


@pytest.mark.asyncio
async def test_workflow_change_notification_is_transactional(pg_engine):
    tenant, actor, _ = await owner()
    identifier = uuid4().hex[:12]
    async with session_scope(tenant_id=tenant) as db:
        await WorkflowRepo(db, actor).create_workflow(wf_id=identifier, name='activity QA')
    async with state_notifications.state_changes('flowork_workflow_activity', identifier) as changed:
        await asyncio.wait_for(changed.wait(), 5)
        changed.clear()
        async with session_scope(tenant_id=tenant) as db:
            await WorkflowRepo(db, actor).commit(identifier, {'node_1': {'node_type':'StartNode'}}, expected_version='v1.sv0')
            assert not changed.is_set()
        await asyncio.wait_for(changed.wait(), 5)
        changed.clear()
        async with session_scope(tenant_id=tenant) as db:
            await WorkflowRepo(db, actor).update_meta(identifier, workflow_name='rolled back')
            await db.rollback()
        await asyncio.sleep(.1)
        assert not changed.is_set()
        async with session_scope(tenant_id=tenant) as db:
            await WorkflowRepo(db, actor).update_meta(identifier, workflow_name='committed')
        await asyncio.wait_for(changed.wait(), 5)


@pytest.mark.asyncio
async def test_workflow_activity_authorizes_and_commits_before_stream(monkeypatch):
    authz=AsyncMock()
    monkeypatch.setattr(workflows,'_authorize_workflow',authz)
    seen=[]
    async def changes(channel, identifier, guard):
        seen.append((channel,identifier));yield True;yield False
    monkeypatch.setattr(state_notifications,'invalidations',changes)
    session=SimpleNamespace(commit=AsyncMock())
    repo=SimpleNamespace(get_meta=AsyncMock(return_value={'id':'wf'}))
    response=await workflows.workflow_activity('wf',SimpleNamespace(),repo,session,SimpleNamespace(),SimpleNamespace())
    assert [item async for item in response.body_iterator]==['event: changed\ndata: {}\n\n',': heartbeat\n\n']
    assert seen==[('flowork_workflow_activity','wf')]
    session.commit.assert_awaited_once();authz.assert_awaited_once()
    monkeypatch.setattr(workflows,'_authorize_workflow',AsyncMock(side_effect=HTTPException(403)))
    with pytest.raises(HTTPException):
        await workflows.workflow_activity('wf',SimpleNamespace(),repo,session,SimpleNamespace(),SimpleNamespace())
