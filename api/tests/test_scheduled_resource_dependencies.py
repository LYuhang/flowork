from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from vibecanvas_api.services import service_account_resources as resources


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', [None, 'snapshot', 'identity', 'projection'])
async def test_schedule_resource_refresh_uses_frozen_graph_and_commits_before_projection(monkeypatch, failure):
    from vibecanvas_api.services import workflow_execution_authorization as authorization
    from vibecanvas_api.authorization import projection
    graph = {'worker': {'node_type': 'SubAgentNode', 'node_config': {
        'skills': [{'id': str(uuid4()), 'name': 'audit'}]}}}
    execution = SimpleNamespace(status='running', workflow_id='wf', workflow_snapshot={'workflow': {} if failure == 'snapshot' else graph})
    session = SimpleNamespace(execute=AsyncMock(side_effect=[
        SimpleNamespace(first=lambda: None if failure == 'identity' else object()),
        SimpleNamespace(scalar_one_or_none=lambda: execution)]))
    from vibecanvas_api.storage.repo_tasks import TasksRepo
    monkeypatch.setattr(TasksRepo, 'get_scheduled_execution', AsyncMock(return_value=execution))
    committed = []
    async def authorize(request, capability, *, resolve):
        assert capability.execution_resource_type == 'task_execution'
        assert capability.principal_type == 'service_account'
        result = await resolve(session=session)
        committed.append(True)
        return result
    monkeypatch.setattr(authorization, 'authorize_workflow_execution', authorize)
    delegate = AsyncMock(return_value=('intent',))
    monkeypatch.setattr(resources, 'delegate_new_resources', delegate)
    async def apply(coordinator, mutations):
        assert committed == [True]
        assert mutations == ('intent',)
        if failure == 'projection':
            raise RuntimeError('projection unavailable')
    project = AsyncMock(side_effect=apply)
    monkeypatch.setattr(projection, 'apply_committed_structural_mutations', project)
    client = SimpleNamespace(close=AsyncMock())
    monkeypatch.setattr(resources, 'openfga_client_from_config', lambda: client)
    kwargs = dict(tenant_id=str(uuid4()), user_id=str(uuid4()), workflow_id='wf',
                  execution_id=str(uuid4()), service_account_id=str(uuid4()), generation=1, workflow=graph)
    if failure:
        with pytest.raises((PermissionError, RuntimeError), match={
            'snapshot': 'scheduled_resource_snapshot_mismatch',
            'identity': 'scheduled_resource_identity_unavailable',
            'projection': 'projection unavailable'}[failure]):
            await resources.refresh_scheduled_resources(**kwargs)
    else:
        await resources.refresh_scheduled_resources(**kwargs)
        assert delegate.await_args.kwargs['workflow'] == graph
        project.assert_awaited_once()
    if failure in ('snapshot', 'identity'):
        delegate.assert_not_awaited()
        project.assert_not_awaited()
    client.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_refresh_reads_encrypted_execution_snapshot(app_engine, monkeypatch):
    from tests.test_service_accounts import _seed_identity
    from vibecanvas_api.storage.db import session_scope
    from vibecanvas_api.storage.repo_service_accounts import ServiceAccountsRepo
    from vibecanvas_api.storage.repo_tasks import TasksRepo
    from vibecanvas_api.storage.workflow_repo import WorkflowRepo
    from vibecanvas_api.services import task_snapshots, workflow_execution_authorization
    from vibecanvas_api.authorization import projection
    tenant, user, task, schedule, account, execution = (uuid4() for _ in range(6))
    workflow_id = 'wf-' + uuid4().hex
    graph = {'worker': {'node_type': 'SubAgentNode', 'node_config': {
        'skills': [{'id': str(uuid4()), 'name': 'encrypted audit'}]}}}
    await _seed_identity(app_engine, tenant_id=tenant, user_id=user)
    monkeypatch.setattr(task_snapshots, 'freeze_workflow', AsyncMock(return_value={
        'workflow': graph, 'version': 'v1.sv0'}))
    async with session_scope(str(tenant)) as db:
        await WorkflowRepo(db, str(user)).create_workflow(wf_id=workflow_id, name='test')
        await ServiceAccountsRepo(db).create_for_owner(service_account_id=account,
            tenant_id=tenant, name='test', kind='schedule', owner_resource_type='task',
            owner_resource_id=str(task), created_by=user)
        repo = TasksRepo(db)
        await repo.create_schedule(task_id=task, schedule_id=schedule, tenant_id=tenant,
            user_id=user, workflow_id=workflow_id, name='test', enabled=False,
            schedule_type='interval', cron_expr=None, interval_seconds=86400, timezone='UTC',
            input_preset={}, mount_enabled=False, notification_policy={}, next_run_at=None,
            service_account_id=account)
        await repo.create_scheduled_execution(execution_id=execution, tenant_id=tenant,
            schedule_id=schedule, workflow_id=workflow_id, run_key='test', trigger_type='manual',
            input_snapshot={}, status='running')
    async def authorize(request, capability, *, resolve):
        async with session_scope(str(tenant)) as db:
            return await resolve(session=db)
    monkeypatch.setattr(workflow_execution_authorization, 'authorize_workflow_execution', authorize)
    monkeypatch.setattr(resources, 'openfga_client_from_config', lambda: SimpleNamespace(close=AsyncMock()))
    delegate = AsyncMock(return_value=())
    monkeypatch.setattr(resources, 'delegate_new_resources', delegate)
    monkeypatch.setattr(projection, 'apply_committed_structural_mutations', AsyncMock())
    await resources.refresh_scheduled_resources(tenant_id=str(tenant), user_id=str(user),
        workflow_id=workflow_id, execution_id=str(execution), service_account_id=str(account),
        generation=1, workflow=graph)
    assert delegate.await_args.kwargs['workflow'] == graph
