"""CLI discovery combines scopes before applying public filters/pagination."""
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid

import pytest
from fastapi import HTTPException
from vibecanvas_api.services.agent_runtime import resource_routes, cli_tasks, cli_deployments


@pytest.mark.asyncio
async def test_discovery_deduplicates_and_rechecks_revoked_shares(monkeypatch):
    local, shared, revoked = [str(uuid.uuid4()) for _ in range(3)]
    scopes = []
    @asynccontextmanager
    async def scope(**kwargs):
        session = object()
        scopes.append(session)
        yield session
    monkeypatch.setattr('vibecanvas_api.storage.db.session_scope', scope)
    monkeypatch.setattr(resource_routes, 'resource_route_params', lambda ctx, session: {})
    monkeypatch.setattr(resource_routes, 'admitted_resource_route_params', AsyncMock(return_value={}))
    monkeypatch.setattr(resource_routes, 'shared_resource_cards', AsyncMock(return_value=[
        SimpleNamespace(resource_id=value) for value in [local, shared, revoked]]))
    page = AsyncMock(side_effect=[{'items': [{'id': local}], 'total': 2},
                                 {'items': [], 'total': 1}])
    get = AsyncMock(side_effect=[{'id': shared}, HTTPException(404)])
    rows = await resource_routes.visible_resource_rows(SimpleNamespace(tenant_id='tenant', username='user'),
        'task', list_page=page, get_resource=get)
    assert {row['id'] for row in rows} == {local, shared}
    assert get.await_count == 2
    assert len(set(scopes)) == 3
    assert page.await_args_list[1].args[1] == 1


@pytest.mark.asyncio
async def test_task_list_filters_shared_and_local_before_paging(monkeypatch):
    rows = [{'id': str(n), 'task_type': kind, 'workflow_id': workflow,
             'status': 'succeeded', 'submitted_at': f'2026-10-0{n}', 'payload': {'name': f'Task {n}'}}
            for n, kind, workflow in [(1, 'scheduled_run', 'wf'), (2, 'batch_exec', 'wf'),
                                      (3, 'scheduled_run', 'other'), (4, 'scheduled_run', 'wf')]]
    monkeypatch.setattr(cli_tasks, 'visible_resource_rows', AsyncMock(return_value=rows))
    args = {'task_type': 'schedule_run', 'workflow_id': 'wf', 'status': 'succeeded', 'limit': 1}
    first = await cli_tasks._read(object(), 'task.list', args, None)
    assert first['tasks'][0]['task_id'] == '4'
    assert first['next_offset'] == 1
    second = await cli_tasks._read(object(), 'task.list', {**args, 'offset': 1}, None)
    assert second['tasks'][0]['task_id'] == '1'
    assert second['next_offset'] is None


@pytest.mark.asyncio
async def test_deployment_list_filters_before_paging(monkeypatch):
    rows = [{'id': str(n), 'wf_id': workflow, 'created_at': f'2026-10-0{n}'}
            for n, workflow in [(1, 'wf'), (2, 'other'), (3, 'wf')]]
    monkeypatch.setattr(cli_deployments, 'visible_resource_rows', AsyncMock(return_value=rows))
    monkeypatch.setattr(cli_deployments, 'deployment_status', lambda row: {
        'deployment_id': row['id'], 'workflow_id': row['wf_id'], 'name': 'Deploy',
        'trigger_type': 'api', 'status': 'enabled'})
    args = {'workflow_id': 'wf', 'limit': 1}
    first = await cli_deployments.read(object(), 'deployment.list', args)
    assert first['deployments'][0]['deployment_id'] == '3'
    assert first['next_offset'] == 1
    second = await cli_deployments.read(object(), 'deployment.list', {**args, 'offset': 1})
    assert second['deployments'][0]['deployment_id'] == '1'
    assert second['next_offset'] is None
