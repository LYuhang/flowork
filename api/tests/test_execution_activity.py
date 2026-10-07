from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock
import asyncio

import pytest
from fastapi import HTTPException, Request
from sqlalchemy import text

from vibecanvas_api.routes import workflow_history as routes
from vibecanvas_api.services.state_notifications import state_changes
from vibecanvas_api.storage.db import session_scope
from vibecanvas_api.storage.workflow_history_repo import WorkflowHistoryRepo
from tests.storage.test_workflow_history import owner, waiting_run


@pytest.mark.asyncio
async def test_execution_notifications_follow_commit_not_rollback(pg_engine):
    tenant, actor, _ = await owner()
    run, approval, event = await waiting_run(tenant, actor)
    async with state_changes('flowork_execution_activity', run) as changed:
        await asyncio.wait_for(changed.wait(), 5)
        changed.clear()
        async with session_scope(tenant_id=tenant) as session:
            repo = WorkflowHistoryRepo(session)
            await repo.persist_events(run, 'generation-a', [{**event, 'type': 'node_event', 'seq': 2}])
            assert not changed.is_set()
        await asyncio.wait_for(changed.wait(), 5)
        changed.clear()
        async with session_scope(tenant_id=tenant) as session:
            await session.execute(text("UPDATE workflow_execution_runs SET status='running' WHERE id=:id"), {'id':run})
            await session.rollback()
        await asyncio.sleep(.1)
        assert not changed.is_set()
        async with session_scope(tenant_id=tenant) as session:
            await session.execute(text("UPDATE workflow_execution_approvals SET status='decision_requested' WHERE execution_id=:id"), {'id':run})
        await asyncio.wait_for(changed.wait(), 5)
        changed.clear()
        async with session_scope(tenant_id=tenant) as session:
            await WorkflowHistoryRepo(session).fail(run, error_code='execution_lost', generation='generation-a')
        await asyncio.wait_for(changed.wait(), 5)


@pytest.mark.asyncio
@pytest.mark.parametrize('assignee,owner_access,source_allowed,exists,expected', [
    (True,False,False,True,True),
    (False,False,True,True,False),
    (False,True,True,True,True),
    (False,True,False,True,False),
    (True,True,True,False,False),
])
async def test_stream_preserves_assignment_and_private_history(monkeypatch, assignee, owner_access, source_allowed, exists, expected):
    from vibecanvas_api.auth import deps, live_identity
    from vibecanvas_api.storage import db
    auth = SimpleNamespace(user_id='viewer', active_organization_id='org', session_id='session', session_generation=1, membership_id=None)
    request = Request({'type':'http','path':'/api/v1/workflow-executions/test/activity','headers':[], 'state':{'stale':'must not persist'}})
    @asynccontextmanager
    async def scope(**kwargs): yield object()
    monkeypatch.setattr(db, 'session_scope', scope)
    monkeypatch.setattr(live_identity, 'resolve_live_authorization_identity', AsyncMock(return_value=auth))
    async def admit(current, fresh, session): assert current.state.__dict__['_state'] == {}
    monkeypatch.setattr(deps, '_admit_shared_resource', admit)
    repo = SimpleNamespace(get=AsyncMock(return_value={'source_type':'workflow','source_id':'wf','initiator_user_id':'other'} if exists else None),
        is_assignee=AsyncMock(return_value=assignee), is_workflow_owner=AsyncMock(return_value=owner_access))
    monkeypatch.setattr(routes, 'WorkflowHistoryRepo', lambda session: repo)
    @asynccontextmanager
    async def authorize(**kwargs):
        assert kwargs['action'].value == 'inspect_runs'
        if not source_allowed: raise HTTPException(403)
        yield
    monkeypatch.setattr(routes, 'authorized_resource_scope', authorize)
    assert await routes._stream_authorized(request,auth,'run') is expected
    monkeypatch.setattr(live_identity, 'resolve_live_authorization_identity', AsyncMock(side_effect=HTTPException(401)))
    assert await routes._stream_authorized(request,auth,'run') is False
